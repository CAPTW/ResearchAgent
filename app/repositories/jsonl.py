"""Strict JSONL integrity and crash-safe local mutation boundaries.

Authoritative reads are all-or-nothing.  Observational inspection is a
separate, typed operation whose result must expose whether it is complete.
Mutations use the repository's OS-handle lock and validate the current file
while holding that lock; malformed or truncated state is never compacted away.

The lock is advisory and local-host only.  A process that ignores this module
can still race it, and fsync cannot make claims about every filesystem or
power-loss mode.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from app.domain.path_safety import is_reparse_point
from app.repositories.file_locks import FileLockError, FileLockTimeout, file_lock

# Existing JSONL records contain paper metadata and evidence text, but never
# binary PDFs (which have a separate 25 MB bound).  Eight MiB leaves generous
# headroom over the repository's 1 MB JSON evidence-manifest bound while
# preventing an unbounded physical line allocation.
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ISSUES = 20

RecordValidator = Callable[[dict[str, Any]], object]


@dataclass(frozen=True, slots=True)
class JsonlPolicy:
    """Per-artifact parse and validation policy.

    JSONL artifacts currently consumed by the product are object-record logs.
    A caller with a formal domain schema supplies ``record_validator``; a
    validator failure is reported separately from syntax/top-level failures.
    """

    artifact_role: str = "jsonl"
    expected_type: type[Any] | tuple[type[Any], ...] = dict
    record_validator: RecordValidator | None = None
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    max_issues: int = DEFAULT_MAX_ISSUES
    allow_blank_lines: bool = True


@dataclass(frozen=True, slots=True)
class JsonlIssue:
    code: str
    line_number: int


@dataclass(frozen=True, slots=True)
class JsonlReadResult:
    """Explicit observational result; intentionally not list-like."""

    records: tuple[dict[str, Any], ...]
    record_line_numbers: tuple[int, ...]
    complete: bool
    total_line_count: int
    valid_record_count: int
    issue_count: int
    issues: tuple[JsonlIssue, ...]
    first_issue_code: str | None
    first_issue_line_number: int | None
    truncated: bool
    source_digest: str | None


class JsonlIntegrityError(Exception):
    """Stable redacted JSONL failure with no source text or path."""

    code = "jsonl_corrupt"

    def __init__(
        self,
        *,
        line_number: int | None = None,
        issue_count: int = 1,
        cause_code: str | None = None,
    ) -> None:
        self.line_number = line_number
        self.issue_count = max(1, int(issue_count))
        self.cause_code = cause_code
        super().__init__(self.code)


class JsonlCorrupt(JsonlIntegrityError):
    code = "jsonl_corrupt"


class JsonlTruncatedTail(JsonlIntegrityError):
    code = "jsonl_truncated_tail"


class JsonlEncodingInvalid(JsonlIntegrityError):
    code = "jsonl_encoding_invalid"


class JsonlRecordInvalid(JsonlIntegrityError):
    code = "jsonl_record_invalid"


class JsonlSchemaInvalid(JsonlIntegrityError):
    code = "jsonl_schema_invalid"


class JsonlLineTooLarge(JsonlIntegrityError):
    code = "jsonl_line_too_large"


class JsonlAppendBlocked(JsonlIntegrityError):
    code = "jsonl_append_blocked"


class JsonlStateUnavailable(JsonlIntegrityError):
    code = "jsonl_state_unavailable"


class JsonlStateBusy(JsonlIntegrityError):
    code = "jsonl_state_busy"


_ERROR_TYPES: dict[str, type[JsonlIntegrityError]] = {
    JsonlCorrupt.code: JsonlCorrupt,
    JsonlTruncatedTail.code: JsonlTruncatedTail,
    JsonlEncodingInvalid.code: JsonlEncodingInvalid,
    JsonlRecordInvalid.code: JsonlRecordInvalid,
    JsonlSchemaInvalid.code: JsonlSchemaInvalid,
    JsonlLineTooLarge.code: JsonlLineTooLarge,
}


def _issue_error(issue: JsonlIssue) -> JsonlIntegrityError:
    error_type = _ERROR_TYPES.get(issue.code, JsonlCorrupt)
    return error_type(line_number=issue.line_number)


def _reject_non_standard_constant(_value: str) -> None:
    raise ValueError("non_standard_json_constant")


def _validate_record(record: object, policy: JsonlPolicy) -> str | None:
    if not isinstance(record, policy.expected_type):
        return JsonlRecordInvalid.code
    if policy.record_validator is None:
        return None
    try:
        result = policy.record_validator(record)
    except Exception:
        return JsonlSchemaInvalid.code
    if result is False:
        return JsonlSchemaInvalid.code
    return None


def _append_issue(
    issues: list[JsonlIssue],
    *,
    code: str,
    line_number: int,
    policy: JsonlPolicy,
) -> JsonlIssue:
    issue = JsonlIssue(code=code, line_number=line_number)
    if len(issues) < max(0, int(policy.max_issues)):
        issues.append(issue)
    return issue


def _scan_stream(
    handle: BinaryIO,
    *,
    policy: JsonlPolicy,
    stop_on_issue: bool,
) -> JsonlReadResult:
    if policy.max_line_bytes < 1:
        raise JsonlStateUnavailable()
    records: list[dict[str, Any]] = []
    record_line_numbers: list[int] = []
    valid_record_count = 0
    issues: list[JsonlIssue] = []
    issue_count = 0
    total_line_count = 0
    first_issue: JsonlIssue | None = None
    truncated = False
    digest = hashlib.sha256()
    read_bound = int(policy.max_line_bytes) + 3
    while True:
        raw = handle.readline(read_bound)
        if raw == b"":
            break
        digest.update(raw)
        total_line_count += 1
        terminated = raw.endswith(b"\n")
        oversized = False

        if not terminated and len(raw) == read_bound:
            oversized = True
            while True:
                remainder = handle.readline(read_bound)
                if remainder == b"":
                    break
                digest.update(remainder)
                if remainder.endswith(b"\n"):
                    terminated = True
                    break

        content = raw[:-1] if terminated and raw.endswith(b"\n") else raw
        if content.endswith(b"\r"):
            content = content[:-1]
        if len(content) > policy.max_line_bytes:
            oversized = True

        if oversized:
            issue = _append_issue(
                issues,
                code=JsonlLineTooLarge.code,
                line_number=total_line_count,
                policy=policy,
            )
            issue_count += 1
            first_issue = first_issue or issue
            if stop_on_issue:
                break
            continue

        if total_line_count == 1 and content.startswith(b"\xef\xbb\xbf"):
            issue = _append_issue(
                issues,
                code=JsonlEncodingInvalid.code,
                line_number=total_line_count,
                policy=policy,
            )
            issue_count += 1
            first_issue = first_issue or issue
            if stop_on_issue:
                break
            continue

        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            issue = _append_issue(
                issues,
                code=JsonlEncodingInvalid.code,
                line_number=total_line_count,
                policy=policy,
            )
            issue_count += 1
            first_issue = first_issue or issue
            if stop_on_issue:
                break
            continue

        if not text.strip():
            if policy.allow_blank_lines:
                continue
            issue = _append_issue(
                issues,
                code=JsonlRecordInvalid.code,
                line_number=total_line_count,
                policy=policy,
            )
            issue_count += 1
            first_issue = first_issue or issue
            if stop_on_issue:
                break
            continue

        try:
            record = json.loads(text, parse_constant=_reject_non_standard_constant)
        except (json.JSONDecodeError, ValueError):
            code = JsonlTruncatedTail.code if not terminated else JsonlCorrupt.code
            issue = _append_issue(
                issues,
                code=code,
                line_number=total_line_count,
                policy=policy,
            )
            issue_count += 1
            first_issue = first_issue or issue
            truncated = truncated or code == JsonlTruncatedTail.code
            if stop_on_issue:
                break
            continue

        invalid_code = _validate_record(record, policy)
        if invalid_code is not None:
            issue = _append_issue(
                issues,
                code=invalid_code,
                line_number=total_line_count,
                policy=policy,
            )
            issue_count += 1
            first_issue = first_issue or issue
            if stop_on_issue:
                break
            continue

        valid_record_count += 1
        # Observational callers receive an ordered valid prefix, never a
        # stitched dataset that silently bridges over corruption. We continue
        # scanning only to produce bounded, truthful diagnostics.
        if first_issue is None:
            records.append(record)
            record_line_numbers.append(total_line_count)
    return JsonlReadResult(
        records=tuple(records),
        record_line_numbers=tuple(record_line_numbers),
        complete=issue_count == 0,
        total_line_count=total_line_count,
        valid_record_count=valid_record_count,
        issue_count=issue_count,
        issues=tuple(issues),
        first_issue_code=first_issue.code if first_issue else None,
        first_issue_line_number=first_issue.line_number if first_issue else None,
        truncated=truncated,
        source_digest=digest.hexdigest(),
    )


def _read_path(path: Path, *, policy: JsonlPolicy, stop_on_issue: bool) -> JsonlReadResult:
    if not path.exists():
        return JsonlReadResult(
            records=(),
            record_line_numbers=(),
            complete=True,
            total_line_count=0,
            valid_record_count=0,
            issue_count=0,
            issues=(),
            first_issue_code=None,
            first_issue_line_number=None,
            truncated=False,
            source_digest=None,
        )
    try:
        with path.open("rb") as handle:
            return _scan_stream(handle, policy=policy, stop_on_issue=stop_on_issue)
    except JsonlIntegrityError:
        raise
    except OSError as exc:
        raise JsonlStateUnavailable() from exc


def read_jsonl_strict(
    path: Path,
    *,
    policy: JsonlPolicy | None = None,
) -> list[dict[str, Any]]:
    """Read an authoritative JSONL file or raise without returning a prefix."""
    result = _read_path(path, policy=policy or JsonlPolicy(), stop_on_issue=True)
    if not result.complete:
        issue = (
            result.issues[0]
            if result.issues
            else JsonlIssue(
                code=result.first_issue_code or JsonlCorrupt.code,
                line_number=result.first_issue_line_number or 1,
            )
        )
        raise _issue_error(issue)
    return list(result.records)


def inspect_jsonl(
    path: Path,
    *,
    policy: JsonlPolicy | None = None,
) -> JsonlReadResult:
    """Inspect observational JSONL without writing, repairing, or hiding issues."""

    return _read_path(path, policy=policy or JsonlPolicy(), stop_on_issue=False)


def parse_jsonl_bytes_strict(
    payload: bytes,
    *,
    policy: JsonlPolicy | None = None,
) -> list[dict[str, Any]]:
    """Strictly parse an in-memory import/capture using the same policy."""

    result = _scan_stream(
        io.BytesIO(payload),
        policy=policy or JsonlPolicy(),
        stop_on_issue=True,
    )
    if not result.complete:
        issue = (
            result.issues[0]
            if result.issues
            else JsonlIssue(
                code=result.first_issue_code or JsonlCorrupt.code,
                line_number=result.first_issue_line_number or 1,
            )
        )
        raise _issue_error(issue)
    return list(result.records)


def _canonical_record_bytes(record: dict[str, Any], policy: JsonlPolicy) -> bytes:
    invalid_code = _validate_record(record, policy)
    if invalid_code is not None:
        raise _issue_error(JsonlIssue(code=invalid_code, line_number=1))
    try:
        text = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JsonlRecordInvalid(line_number=1) from exc
    encoded = text.encode("utf-8")
    if b"\n" in encoded or b"\r" in encoded:
        raise JsonlRecordInvalid(line_number=1)
    if len(encoded) > policy.max_line_bytes:
        raise JsonlLineTooLarge(line_number=1)
    return encoded


def _reject_unsafe_mutation_path(path: Path) -> None:
    if not path.name or any(part == ".." for part in path.parts):
        raise JsonlAppendBlocked(cause_code="unsafe_path")
    candidates = (path, *path.parents)
    for candidate in candidates:
        if (candidate.exists() or candidate.is_symlink()) and is_reparse_point(candidate):
            raise JsonlAppendBlocked(cause_code="reparse_point")


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _raise_mutation_blocked(exc: JsonlIntegrityError) -> None:
    if isinstance(exc, JsonlStateBusy):
        raise exc
    raise JsonlAppendBlocked(
        line_number=exc.line_number,
        cause_code=exc.code,
    ) from exc


def _write_all(handle: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = handle.write(view)
        if written is None or written <= 0:
            raise OSError("short_write")
        view = view[written:]


def append_jsonl_records(
    path: Path,
    records: list[dict[str, Any]],
    *,
    policy: JsonlPolicy | None = None, mutation_validator: MutationValidator | None = None,
    lock_timeout_seconds: float = 10.0,
) -> Path:
    """Append validated records under one lock, or leave source bytes unchanged."""

    if not records:
        return path
    active_policy = policy or JsonlPolicy()
    _reject_unsafe_mutation_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JsonlAppendBlocked(cause_code=JsonlStateUnavailable.code) from exc

    try:
        with file_lock(
            _lock_path(path),
            timeout_seconds=lock_timeout_seconds,
        ):
            try:
                _validate_mutation(read_jsonl_strict(path, policy=active_policy), records, mutation_validator)
                encoded_records = [
                    _canonical_record_bytes(record, active_policy)
                    for record in records
                ]
            except JsonlIntegrityError as exc:
                _raise_mutation_blocked(exc)

            separator = b""
            try:
                if path.exists() and path.stat().st_size:
                    with path.open("rb") as current:
                        current.seek(-1, os.SEEK_END)
                        if current.read(1) != b"\n":
                            separator = b"\n"
                with path.open("ab", buffering=0) as handle:
                    payload = separator + b"\n".join(encoded_records) + b"\n"
                    _write_all(handle, payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise JsonlAppendBlocked(
                    cause_code=JsonlStateUnavailable.code
                ) from exc
    except FileLockTimeout as exc:
        raise JsonlStateBusy(cause_code="file_lock_timeout") from exc
    except FileLockError as exc:
        raise JsonlAppendBlocked(cause_code=JsonlStateUnavailable.code) from exc
    return path


def append_jsonl_record(
    path: Path,
    record: dict[str, Any],
    *,
    policy: JsonlPolicy | None = None,
    lock_timeout_seconds: float = 10.0,
) -> Path:
    return append_jsonl_records(
        path,
        [record],
        policy=policy,
        lock_timeout_seconds=lock_timeout_seconds,
    )


def replace_jsonl(
    path: Path,
    records: list[dict[str, Any]],
    *,
    policy: JsonlPolicy | None = None, mutation_validator: MutationValidator | None = None,
    lock_timeout_seconds: float = 10.0,
) -> Path:
    """Atomically replace valid JSONL without using corruption as compaction."""
    active_policy = policy or JsonlPolicy()
    _reject_unsafe_mutation_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JsonlAppendBlocked(cause_code=JsonlStateUnavailable.code) from exc

    temp_name: str | None = None
    try:
        with file_lock(
            _lock_path(path),
            timeout_seconds=lock_timeout_seconds,
        ):
            try:
                _validate_mutation(read_jsonl_strict(path, policy=active_policy), records, mutation_validator)
                encoded_records = [
                    _canonical_record_bytes(record, active_policy)
                    for record in records
                ]
            except JsonlIntegrityError as exc:
                _raise_mutation_blocked(exc)

            try:
                with tempfile.NamedTemporaryFile(
                    "wb",
                    delete=False,
                    dir=str(path.parent),
                    suffix=".jsonl.tmp",
                ) as handle:
                    temp_name = handle.name
                    if encoded_records:
                        _write_all(handle, b"\n".join(encoded_records) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
                temp_name = None
            except OSError as exc:
                raise JsonlAppendBlocked(
                    cause_code=JsonlStateUnavailable.code
                ) from exc
    except FileLockTimeout as exc:
        raise JsonlStateBusy(cause_code="file_lock_timeout") from exc
    except FileLockError as exc:
        raise JsonlAppendBlocked(cause_code=JsonlStateUnavailable.code) from exc
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return path


MutationValidator = Callable[[list[dict[str, Any]], list[dict[str, Any]]], object]


def _validate_mutation(
    current_records: list[dict[str, Any]],
    incoming_records: list[dict[str, Any]],
    validator: MutationValidator | None,
) -> None:
    if validator is not None:
        validator(current_records, incoming_records)


def read_jsonl_strict_with_line_numbers(
    path: Path,
    *,
    policy: JsonlPolicy | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Read authoritative records together with their physical JSONL lines."""

    result = _read_path(path, policy=policy or JsonlPolicy(), stop_on_issue=True)
    if not result.complete:
        issue = (
            result.issues[0]
            if result.issues
            else JsonlIssue(
                code=result.first_issue_code or JsonlCorrupt.code,
                line_number=result.first_issue_line_number or 1,
            )
        )
        raise _issue_error(issue)
    return list(zip(result.record_line_numbers, result.records, strict=True))

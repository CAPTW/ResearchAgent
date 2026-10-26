"""Central read-modify-write boundary for shared JSON state files.

The repository stores several pieces of mutable global state as whole JSON
documents (auth sessions, topic profiles, notification settings/history).
``write_json`` already replaces such a file atomically, so a reader never sees
a torn document — but atomicity alone does not prevent a **lost update**: two
callers that each read, mutate and write back their own snapshot silently
discard one another's changes, and a snapshot taken before a delete can bring
the deleted item back.

Every mutation therefore goes through :func:`mutate_json_state`, which

1. takes a crash-safe cross-process lock on the state file,
2. re-reads the *latest* state inside the lock,
3. applies a pure mutation to that latest state,
4. writes the result exactly once, atomically, and only if it changed.

A caller can never hand a stale snapshot to a "save the whole document" helper,
because no such public helper exists here.

Corruption is never repaired silently. Malformed JSON or a wrong top-level type
fails closed with a stable, redacted error and leaves the bytes on disk for an
operator to inspect: treating an unreadable document as "empty" would let the
next write destroy real data.

Reads never create anything. A missing file reads as the caller's declared
default without creating the file, its parent directory, or a lock file, so
GET/readiness/import paths stay free of side effects. Directories are created
at the mutation boundary only.

Scope boundary: local, single-host, advisory locking over one atomic replace.
Not a distributed transaction, not multi-file atomicity, not power-loss proof.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.domain.path_safety import is_reparse_point
from app.repositories.file_locks import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    FileLockError,
    FileLockTimeout,
    file_lock,
)

T = TypeVar("T")

LOCK_SUFFIX = ".lock"

# Stable codes. They never contain a path, raw state, or a parser message.
CODE_BUSY = "shared_state_busy"
CODE_CORRUPT = "shared_state_corrupt"
CODE_SCHEMA_INVALID = "shared_state_schema_invalid"
CODE_WRITE_FAILED = "shared_state_write_failed"


class SharedStateError(RuntimeError):
    """Base class for shared-state failures.

    Carries a stable machine-readable code and the logical state name only —
    never a filesystem path, the state contents, or a parser message.
    """

    code = "shared_state_failed"

    def __init__(self, state_name: str) -> None:
        self.state_name = state_name
        super().__init__(f"{self.code}: {state_name}")


class SharedStateBusy(SharedStateError):
    """Another writer holds the lock; the caller may retry."""

    code = CODE_BUSY


class SharedStateCorrupt(SharedStateError):
    """The state file is not readable as JSON. Never overwritten."""

    code = CODE_CORRUPT


class SharedStateSchemaInvalid(SharedStateError):
    """The state file parses but has the wrong top-level shape. Never overwritten."""

    code = CODE_SCHEMA_INVALID


class SharedStateWriteFailed(SharedStateError):
    """The new state could not be committed; the old state is authoritative."""

    code = CODE_WRITE_FAILED


def lock_path_for(path: Path) -> Path:
    """Hidden sibling lock file, in the same trusted directory as the state."""
    return path.parent / f".{path.name}{LOCK_SUFFIX}"


def _decode(path: Path, *, state_name: str) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SharedStateCorrupt(state_name) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        # A malformed document is reported, never treated as empty: doing so
        # would let the next mutation overwrite recoverable data.
        raise SharedStateCorrupt(state_name) from exc


def _validate(payload: Any, expected_type: type | tuple[type, ...], *, state_name: str) -> Any:
    if not isinstance(payload, expected_type):
        raise SharedStateSchemaInvalid(state_name)
    return payload


def load_json_state(
    path: Path,
    *,
    state_name: str,
    expected_type: type | tuple[type, ...],
    default: Callable[[], T],
) -> T:
    """Read shared state without creating anything.

    A missing file yields ``default()``; no file, parent directory or lock file
    is created, which keeps GET, readiness, import and OpenAPI side-effect free.
    """
    if not path.exists():
        return default()
    payload = _decode(path, state_name=state_name)
    return _validate(payload, expected_type, state_name=state_name)


def _atomic_write(path: Path, payload: Any, *, state_name: str) -> None:
    """Serialize, flush, fsync, then ``os.replace`` as the single commit point.

    Any failure before the replace leaves the old state authoritative. A failure
    to clean up the temporary file after a successful replace does not
    invalidate the commit.
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    handle = -1
    temp_name = ""
    try:
        # A unique temp name per writer: a fixed one collides between concurrent
        # writers (and on Windows that surfaces as a permission error).
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            handle = -1  # ownership transferred to the stream
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if handle != -1:
            try:
                os.close(handle)
            except OSError:
                pass
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise SharedStateWriteFailed(state_name) from exc

    try:
        os.replace(temp_name, path)
    except OSError as exc:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise SharedStateWriteFailed(state_name) from exc


def mutate_json_state(
    path: Path,
    *,
    state_name: str,
    expected_type: type | tuple[type, ...],
    default: Callable[[], Any],
    mutate: Callable[[Any], tuple[Any, T]],
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> T:
    """Apply ``mutate`` to the latest state under an exclusive lock.

    ``mutate`` receives the state as it exists *inside the lock* and returns
    ``(next_state, result)``. It must be a bounded, local computation: no
    network, provider, SMTP or webhook call may run while the lock is held.

    The state is written at most once, and only when it actually changed, so a
    no-op mutation performs zero writes. A mutation that raises leaves the file
    untouched.
    """
    if is_reparse_point(path):
        raise SharedStateSchemaInvalid(state_name)

    # Creating the directory is a mutation-boundary action, never a read one.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SharedStateWriteFailed(state_name) from exc

    try:
        with file_lock(lock_path_for(path), timeout_seconds=timeout_seconds):
            current = load_json_state(
                path, state_name=state_name, expected_type=expected_type, default=default
            )
            before = json.dumps(current, ensure_ascii=False, sort_keys=True)
            next_state, result = mutate(current)
            _validate(next_state, expected_type, state_name=state_name)
            after = json.dumps(next_state, ensure_ascii=False, sort_keys=True)
            if after != before:
                _atomic_write(path, next_state, state_name=state_name)
            return result
    except FileLockTimeout as exc:
        raise SharedStateBusy(state_name) from exc
    except FileLockError as exc:
        raise SharedStateWriteFailed(state_name) from exc

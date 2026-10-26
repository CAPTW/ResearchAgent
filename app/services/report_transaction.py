"""Report mutation transaction primitives: crash-safe lock, journal, CAS.

The authoritative report draft for a run is a single document (``outline.json``)
written through an atomic temp-file + ``os.replace``. That single replace remains
the logical commit point, so a batch of candidates is all-or-nothing.

This module owns the concurrency and crash-recovery concerns around it:

* a per-run **crash-safe** cross-process mutation lock. Ownership is an OS handle
  lock (``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX), not the mere
  existence of a marker file, so a lock held by a process that died is released
  by the OS instead of blocking the run forever.
* a bounded per-run **transaction journal** recording the base and target outline
  digests, so a crash can be classified as "before the commit" or "after the
  commit" and the secondary state finalized idempotently.
* an optimistic **compare-and-swap** on the base outline digest.

The journal is *not* the source of truth for report content: the committed
``outline.json`` and its digest are. The journal records intent only, so an
interrupted finalize can be completed or explicitly refused.

Scope boundary: a local single-host guard over one atomic file replace. Not a
filesystem-wide ACID transaction, not power-loss proof, not distributed.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.domain.path_safety import is_reparse_point, validate_run_id
from app.repositories import file_locks
from app.repositories.files import read_json, write_json
from app.repositories.runs import build_run_paths

LOCK_FILENAME = ".report-mutation.lock"
JOURNAL_FILENAME = ".report-transaction.json"

JOURNAL_SCHEMA_VERSION = 1

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05

# Transaction states.
STATE_PREPARED = "prepared"
STATE_COMMITTED = "committed"
STATE_CLEANED = "cleaned"
STATE_RECOVERY_REQUIRED = "recovery_required"

# Transaction kinds.
KIND_SINGLE_APPLY = "single_apply"
KIND_APPLY_ALL = "apply_all"

# Stable outcome codes (never contain a path, raw content, or traceback).
CODE_RECOVERED = "report_transaction_recovered"
CODE_ABORTED_PRECOMMIT = "report_transaction_aborted_precommit"
CODE_CLEANUP_PENDING = "report_transaction_cleanup_pending"
CODE_RECOVERY_REQUIRED = "report_transaction_recovery_required"
CODE_MANIFEST_INVALID = "report_transaction_manifest_invalid"
CODE_DIGEST_MISMATCH = "report_transaction_digest_mismatch"


class ReportTransactionError(RuntimeError):
    """Base class. Carries a stable code and never a raw path or traceback."""

    code = "report_transaction_failed"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


class ReportApplyValidationFailed(ReportTransactionError):
    code = "report_apply_validation_failed"


class ReportApplyConflict(ReportTransactionError):
    code = "report_apply_conflict"


class ReportTransactionBusy(ReportTransactionError):
    code = "report_transaction_busy"


class ReportTransactionCommitFailed(ReportTransactionError):
    code = "report_transaction_commit_failed"


class ReportTransactionPrepareFailed(ReportTransactionError):
    code = "report_transaction_prepare_failed"


class ReportTransactionRecoveryRequired(ReportTransactionError):
    """Ambiguous or contradictory transaction state; refuse to guess."""

    code = "report_transaction_recovery_required"


# ---------------------------------------------------------------------------
# crash-safe per-run lock
# ---------------------------------------------------------------------------


def _lock_path(run_id: str) -> Path:
    validate_run_id(run_id)
    return build_run_paths(run_id).reports_dir / LOCK_FILENAME


# The OS handle lock primitive lives in app.repositories.file_locks so that the
# session/profile/notification stores share one verified implementation. These
# aliases keep this module's semantics and its call sites unchanged.
_try_acquire_os_lock = file_locks.try_acquire_os_lock
_release_os_lock = file_locks.release_os_lock


@contextmanager
def report_mutation_lock(
    run_id: str,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the per-run report mutation lock, or raise :class:`ReportTransactionBusy`.

    Ownership is an OS handle lock rather than the existence of the marker file,
    so a lock whose holder crashed does not block the run permanently. The lock
    file is intentionally left on disk: its presence means nothing, only the OS
    lock does. A lock held by a live owner is never force-removed, and staleness
    is never inferred from mtime.
    """
    target = _lock_path(run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(target):
        raise ReportApplyValidationFailed("lock_path_reparse_point")

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    handle: int | None = None
    acquired = False
    try:
        try:
            handle = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ReportTransactionCommitFailed("report_mutation_lock_unavailable") from exc
        while True:
            if _try_acquire_os_lock(handle):
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise ReportTransactionBusy("report_mutation_lock_timeout")
            time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        if handle is not None:
            if acquired:
                _release_os_lock(handle)
            try:
                os.close(handle)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# optimistic concurrency
# ---------------------------------------------------------------------------


def ensure_base_unchanged(*, expected_digest: str, actual_digest: str) -> None:
    """Optimistic concurrency guard evaluated immediately before the commit."""
    if str(expected_digest) != str(actual_digest):
        raise ReportApplyConflict("report_base_revision_changed")


# ---------------------------------------------------------------------------
# transaction journal
# ---------------------------------------------------------------------------


def journal_path(run_id: str) -> Path:
    validate_run_id(run_id)
    return build_run_paths(run_id).reports_dir / JOURNAL_FILENAME


def new_transaction_id() -> str:
    return uuid.uuid4().hex


def build_journal(
    *,
    transaction_id: str,
    kind: str,
    run_id: str,
    base_digest: str,
    target_digest: str,
    candidate_ids: list[str],
    editing_session_ids: list[str],
    now: str,
) -> dict[str, Any]:
    """Bounded journal record: identities and digests only, never content."""
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "kind": kind,
        "run_id": run_id,
        "state": STATE_PREPARED,
        "base_digest": base_digest,
        "target_digest": target_digest,
        "candidate_ids": [str(item) for item in candidate_ids],
        "editing_session_ids": [str(item) for item in editing_session_ids if item],
        "created_at": now,
        "updated_at": now,
        "reason_code": None,
    }


def write_journal(run_id: str, journal: dict[str, Any]) -> None:
    """Atomically persist the journal. Raises so the caller can abort before commit."""
    try:
        write_json(journal_path(run_id), journal)
    except OSError as exc:
        raise ReportTransactionPrepareFailed("report_transaction_journal_write_failed") from exc


def read_journal(run_id: str) -> dict[str, Any] | None:
    """Read the journal, or None when absent.

    A malformed or unknown-version journal is reported as recovery-required
    rather than being deleted or guessed at.
    """
    path = journal_path(run_id)
    if not path.exists():
        return None
    try:
        payload = read_json(path, default=None)
    except (OSError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError: a corrupt journal must surface
        # as a redacted recovery-required error, never as a raw parse error.
        raise ReportTransactionRecoveryRequired(CODE_MANIFEST_INVALID) from exc
    if not isinstance(payload, dict):
        raise ReportTransactionRecoveryRequired(CODE_MANIFEST_INVALID)
    if payload.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise ReportTransactionRecoveryRequired(CODE_MANIFEST_INVALID)
    for field in ("transaction_id", "state", "base_digest", "target_digest"):
        if not payload.get(field):
            raise ReportTransactionRecoveryRequired(CODE_MANIFEST_INVALID)
    if payload.get("state") == STATE_RECOVERY_REQUIRED:
        raise ReportTransactionRecoveryRequired(CODE_RECOVERY_REQUIRED)
    return payload


def mark_journal_state(
    run_id: str,
    journal: dict[str, Any],
    state: str,
    *,
    now: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    updated = {**journal, "state": state, "updated_at": now}
    if reason_code is not None:
        updated["reason_code"] = reason_code
    write_journal(run_id, updated)
    return updated


def mark_recovery_required(run_id: str, journal: dict[str, Any], *, now: str, reason_code: str) -> None:
    """Persist a refusal so the next mutation also fails closed.

    Best effort: if this write fails the classification is still enforced in
    memory for the current request.
    """
    try:
        mark_journal_state(run_id, journal, STATE_RECOVERY_REQUIRED, now=now, reason_code=reason_code)
    except ReportTransactionPrepareFailed:
        pass


def clear_journal(run_id: str) -> bool:
    """Best-effort journal removal. Failure never invalidates a commit."""
    try:
        journal_path(run_id).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def classify(*, journal: dict[str, Any], current_digest: str) -> str:
    """Classify an existing transaction against the authoritative outline digest.

    Returns ``finalize_required``, ``aborted_precommit`` or ``recovery_required``.
    The committed outline digest — not the journal state — decides whether the
    commit actually happened.
    """
    base = str(journal.get("base_digest") or "")
    target = str(journal.get("target_digest") or "")
    state = str(journal.get("state") or "")
    current = str(current_digest or "")

    if current == target:
        # The commit landed (whatever the journal state says); finish the
        # secondary finalize idempotently.
        return "finalize_required"
    if current == base:
        if state == STATE_COMMITTED:
            # Journal claims committed while the report is still the old one:
            # contradictory, so refuse to guess.
            return "recovery_required"
        return "aborted_precommit"
    # Neither base nor target: concurrent, manual or unknown mutation.
    return "recovery_required"

"""Durable run-job repository (SQLite, single-node/local-first).

Backs the run executor with an additive ``run_jobs`` table in the existing
scheduler SQLite store, reusing ``connect_scheduler_db`` (WAL + bounded
busy_timeout). Every state change that a worker performs after claiming is
**fenced** by the lease token issued at claim time, so a worker that lost its
lease can never overwrite a newer owner's outcome.

Boundaries deliberately not claimed here: this is a local single-host queue.
It is not a distributed queue and provides no multi-node guarantees.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.bootstrap import connect_scheduler_db
from app.domain.path_safety import validate_run_id

HANDLER_RESEARCH_PIPELINE = "research_pipeline"
PAYLOAD_SCHEMA_VERSION = 1

STATE_QUEUED = "queued"
STATE_LEASED = "leased"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"
STATE_CANCELLED = "cancelled"

ACTIVE_STATES = (STATE_QUEUED, STATE_LEASED, STATE_RUNNING)
TERMINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_INTERRUPTED, STATE_CANCELLED)

# Reason codes are stable, machine-readable and never contain raw input.
REASON_RECOVERY_REQUIRED = "executor_recovery_required"
REASON_LEASE_EXPIRED_REQUEUED = "executor_lease_expired_requeued"
REASON_MAX_ATTEMPTS = "executor_max_attempts_exhausted"
REASON_RUN_RECORD_MISSING = "executor_run_record_missing"
REASON_PIPELINE_FAILED = "executor_pipeline_failed"


class RunJobError(RuntimeError):
    """Base class for durable job errors (never carries raw payload)."""

    code = "run_job_error"


class DuplicateActiveJob(RunJobError):
    code = "run_job_duplicate_active"


class RunJobStoreUnavailable(RunJobError):
    code = "run_job_store_unavailable"


@dataclass(frozen=True)
class RunJob:
    job_id: str
    run_id: str
    handler: str
    state: str
    attempts: int
    max_attempts: int
    available_at: str
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    pipeline_started_at: str | None
    reason_code: str | None
    error_code: str | None
    created_at: str
    updated_at: str
    completed_at: str | None

    @property
    def pipeline_started(self) -> bool:
        return bool(self.pipeline_started_at)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _row_to_job(row: sqlite3.Row) -> RunJob:
    return RunJob(
        job_id=str(row["job_id"]),
        run_id=str(row["run_id"]),
        handler=str(row["handler"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        available_at=str(row["available_at"]),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        pipeline_started_at=row["pipeline_started_at"],
        reason_code=row["reason_code"],
        error_code=row["error_code"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=row["completed_at"],
    )


def _connect() -> sqlite3.Connection:
    try:
        return connect_scheduler_db()
    except sqlite3.DatabaseError as exc:  # pragma: no cover - defensive
        raise RunJobStoreUnavailable("store_unavailable") from exc


def enqueue_run_job(
    run_id: str,
    *,
    handler: str = HANDLER_RESEARCH_PIPELINE,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> RunJob:
    """Durably record a run job. Idempotent per active run_id.

    If an active (non-terminal) job already exists for ``run_id`` the existing
    job is returned instead of creating a second one, so a retried HTTP request
    or a duplicate scheduler trigger cannot produce two executions.
    """
    validate_run_id(run_id)
    moment = now or utc_now()
    stamp = _iso(moment)
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            f"SELECT * FROM run_jobs WHERE run_id = ? AND state IN ({','.join('?' * len(ACTIVE_STATES))})",
            (run_id, *ACTIVE_STATES),
        ).fetchone()
        if existing is not None:
            connection.execute("COMMIT")
            return _row_to_job(existing)
        job_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO run_jobs (
              job_id, run_id, handler, payload_schema_version, state, attempts,
              max_attempts, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (job_id, run_id, handler, PAYLOAD_SCHEMA_VERSION, STATE_QUEUED, int(max_attempts), stamp, stamp, stamp),
        )
        connection.execute("COMMIT")
        row = connection.execute("SELECT * FROM run_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row)
    except sqlite3.IntegrityError as exc:
        connection.execute("ROLLBACK")
        # The partial unique index raced us: an active job already exists.
        row = connection.execute(
            f"SELECT * FROM run_jobs WHERE run_id = ? AND state IN ({','.join('?' * len(ACTIVE_STATES))})",
            (run_id, *ACTIVE_STATES),
        ).fetchone()
        if row is not None:
            return _row_to_job(row)
        raise DuplicateActiveJob("duplicate_active_job") from exc
    except sqlite3.DatabaseError as exc:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise RunJobStoreUnavailable("enqueue_failed") from exc
    finally:
        connection.close()


def claim_next_job(
    *,
    owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> RunJob | None:
    """Atomically claim one available job and issue a fresh fencing token.

    Uses BEGIN IMMEDIATE so two workers can never claim the same row. The
    returned job carries the only lease token accepted for later updates.
    """
    moment = now or utc_now()
    stamp = _iso(moment)
    expires = _iso(moment + timedelta(seconds=int(lease_seconds)))
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM run_jobs
            WHERE state = ? AND available_at <= ?
            ORDER BY created_at ASC, job_id ASC
            LIMIT 1
            """,
            (STATE_QUEUED, stamp),
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        token = secrets.token_hex(16)
        connection.execute(
            """
            UPDATE run_jobs
            SET state = ?, attempts = attempts + 1, lease_owner = ?, lease_token = ?,
                lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
            WHERE job_id = ? AND state = ?
            """,
            (STATE_LEASED, owner, token, expires, stamp, stamp, row["job_id"], STATE_QUEUED),
        )
        connection.execute("COMMIT")
        claimed = connection.execute("SELECT * FROM run_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
        return _row_to_job(claimed)
    except sqlite3.DatabaseError as exc:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise RunJobStoreUnavailable("claim_failed") from exc
    finally:
        connection.close()


def _fenced_update(job_id: str, lease_token: str, sql: str, params: tuple[Any, ...]) -> bool:
    """Apply a token-fenced update. Returns False when the lease was lost."""
    connection = _connect()
    try:
        cursor = connection.execute(sql, params)
        return int(cursor.rowcount or 0) > 0
    except sqlite3.DatabaseError as exc:
        raise RunJobStoreUnavailable("update_failed") from exc
    finally:
        connection.close()


def heartbeat(job_id: str, lease_token: str, *, lease_seconds: int, now: datetime | None = None) -> bool:
    moment = now or utc_now()
    return _fenced_update(
        job_id,
        lease_token,
        """
        UPDATE run_jobs
        SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
        WHERE job_id = ? AND lease_token = ? AND state IN (?, ?)
        """,
        (
            _iso(moment),
            _iso(moment + timedelta(seconds=int(lease_seconds))),
            _iso(moment),
            job_id,
            lease_token,
            STATE_LEASED,
            STATE_RUNNING,
        ),
    )


def mark_pipeline_started(job_id: str, lease_token: str, *, now: datetime | None = None) -> bool:
    """Record the durable started marker before the pipeline is invoked."""
    moment = now or utc_now()
    return _fenced_update(
        job_id,
        lease_token,
        """
        UPDATE run_jobs
        SET state = ?, pipeline_started_at = ?, heartbeat_at = ?, updated_at = ?
        WHERE job_id = ? AND lease_token = ? AND state = ?
        """,
        (STATE_RUNNING, _iso(moment), _iso(moment), _iso(moment), job_id, lease_token, STATE_LEASED),
    )


def complete_job(
    job_id: str,
    lease_token: str,
    *,
    state: str,
    reason_code: str | None = None,
    error_code: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Terminal transition, fenced by lease token. Never leaves an active state."""
    if state not in TERMINAL_STATES:
        raise RunJobError("invalid_terminal_state")
    moment = now or utc_now()
    return _fenced_update(
        job_id,
        lease_token,
        f"""
        UPDATE run_jobs
        SET state = ?, reason_code = ?, error_code = ?, completed_at = ?, updated_at = ?,
            lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
        WHERE job_id = ? AND lease_token = ? AND state IN ({','.join('?' * len(ACTIVE_STATES))})
        """,
        (state, reason_code, error_code, _iso(moment), _iso(moment), job_id, lease_token, *ACTIVE_STATES),
    )


def get_job(job_id: str) -> RunJob | None:
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM run_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row is not None else None
    finally:
        connection.close()


def active_job_for_run(run_id: str) -> RunJob | None:
    connection = _connect()
    try:
        row = connection.execute(
            f"SELECT * FROM run_jobs WHERE run_id = ? AND state IN ({','.join('?' * len(ACTIVE_STATES))})",
            (run_id, *ACTIVE_STATES),
        ).fetchone()
        return _row_to_job(row) if row is not None else None
    finally:
        connection.close()


def list_expired_jobs(*, now: datetime | None = None) -> list[RunJob]:
    moment = now or utc_now()
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM run_jobs
            WHERE state IN (?, ?) AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
            ORDER BY created_at ASC, job_id ASC
            """,
            (STATE_LEASED, STATE_RUNNING, _iso(moment)),
        ).fetchall()
        return [_row_to_job(row) for row in rows]
    finally:
        connection.close()


def recover_expired_job(job: RunJob, *, now: datetime | None = None) -> str:
    """Recover one expired-lease job. Returns the recovery outcome code.

    A job whose pipeline never started can be safely requeued. A job that did
    start is **never** re-executed automatically: it becomes a truthful
    terminal ``interrupted`` with a recovery-required reason, because
    stage-level resume is not implemented.
    """
    moment = now or utc_now()
    stamp = _iso(moment)
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM run_jobs WHERE job_id = ?", (job.job_id,)).fetchone()
        if row is None or str(row["state"]) not in (STATE_LEASED, STATE_RUNNING):
            connection.execute("COMMIT")
            return "noop"
        started = bool(row["pipeline_started_at"])
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])
        if started:
            connection.execute(
                """
                UPDATE run_jobs SET state = ?, reason_code = ?, completed_at = ?, updated_at = ?,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (STATE_INTERRUPTED, REASON_RECOVERY_REQUIRED, stamp, stamp, job.job_id),
            )
            outcome = "interrupted"
        elif attempts >= max_attempts:
            connection.execute(
                """
                UPDATE run_jobs SET state = ?, reason_code = ?, completed_at = ?, updated_at = ?,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (STATE_FAILED, REASON_MAX_ATTEMPTS, stamp, stamp, job.job_id),
            )
            outcome = "max_attempts"
        else:
            connection.execute(
                """
                UPDATE run_jobs SET state = ?, reason_code = ?, available_at = ?, updated_at = ?,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                WHERE job_id = ?
                """,
                (STATE_QUEUED, REASON_LEASE_EXPIRED_REQUEUED, stamp, stamp, job.job_id),
            )
            outcome = "requeued"
        connection.execute("COMMIT")
        return outcome
    except sqlite3.DatabaseError as exc:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise RunJobStoreUnavailable("recover_failed") from exc
    finally:
        connection.close()


def dead_letter_job(job_id: str, *, reason_code: str, now: datetime | None = None) -> bool:
    """Terminate an active job without a lease (reconciliation only)."""
    moment = now or utc_now()
    connection = _connect()
    try:
        cursor = connection.execute(
            f"""
            UPDATE run_jobs
            SET state = ?, reason_code = ?, completed_at = ?, updated_at = ?,
                lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
            WHERE job_id = ? AND state IN ({','.join('?' * len(ACTIVE_STATES))})
            """,
            (STATE_FAILED, reason_code, _iso(moment), _iso(moment), job_id, *ACTIVE_STATES),
        )
        return int(cursor.rowcount or 0) > 0
    finally:
        connection.close()


def list_active_jobs() -> list[RunJob]:
    connection = _connect()
    try:
        rows = connection.execute(
            f"SELECT * FROM run_jobs WHERE state IN ({','.join('?' * len(ACTIVE_STATES))}) ORDER BY created_at ASC",
            ACTIVE_STATES,
        ).fetchall()
        return [_row_to_job(row) for row in rows]
    finally:
        connection.close()

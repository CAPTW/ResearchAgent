"""Central read-only inspection of local runtime stores.

Used by system probes (liveness/readiness/settings/status). Every function here
is observation-only: it never creates a directory or DB file, never runs schema
DDL or a migration, never changes journal mode, never starts a worker, and never
claims or recovers a job. A store that is absent or half-initialised is reported
truthfully (``missing`` / ``uninitialized`` / ``schema_mismatch``) rather than
being bootstrapped as a side effect of being looked at.

Reported statuses are stable machine-readable codes and never include a
filesystem path, DB filename, SQL text, or raw SQLite exception string.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.db.bootstrap import (
    connect_metadata_index,
    connect_scheduler_db_read_only,
    metadata_index_path,
    scheduler_db_path,
    sqlite_tables,
)
from app.db.sqlite_integrity import (
    STATUS_MIGRATION_REQUIRED as FK_STATUS_MIGRATION_REQUIRED,
    STATUS_READY as FK_STATUS_READY,
    STORE_METADATA,
    STORE_SCHEDULER,
    ForeignKeyInventory,
    inspect_required_foreign_keys,
)

# Store status codes.
STATUS_READY = "ready"
STATUS_MISSING = "missing"
STATUS_UNINITIALIZED = "uninitialized"
STATUS_SCHEMA_MISMATCH = "schema_mismatch"
STATUS_UNREADABLE = "unreadable"
STATUS_BUSY = "busy"
STATUS_MALFORMED = "malformed"
STATUS_DISABLED = "disabled"
STATUS_MIGRATION_REQUIRED = "migration_required"

# Stable reason codes surfaced to API consumers.
REASON_METADATA_MISSING = "metadata_store_missing"
REASON_METADATA_UNINITIALIZED = "metadata_store_uninitialized"
REASON_METADATA_SCHEMA_MISMATCH = "metadata_schema_mismatch"
REASON_SCHEDULER_MISSING = "scheduler_store_missing"
REASON_SCHEDULER_UNINITIALIZED = "scheduler_store_uninitialized"
REASON_SCHEDULER_SCHEMA_MISMATCH = "scheduler_schema_mismatch"
REASON_EXECUTOR_UNINITIALIZED = "executor_store_uninitialized"
REASON_METADATA_MIGRATION_REQUIRED = "metadata_fk_migration_required"
REASON_SCHEDULER_MIGRATION_REQUIRED = "scheduler_fk_migration_required"
REASON_STORE_BUSY = "runtime_store_busy"
REASON_STORE_UNREADABLE = "runtime_store_unreadable"
REASON_STORE_MALFORMED = "runtime_store_malformed"

# Minimum tables each store must expose to be considered initialised.
_METADATA_REQUIRED_TABLES = frozenset({"index_state", "runs", "papers"})
_SCHEDULER_REQUIRED_TABLES = frozenset({"scheduler_schedules", "scheduler_queue_items"})
_EXECUTOR_REQUIRED_TABLES = frozenset({"run_jobs"})

_INSPECTION_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RuntimeStoreStatus:
    """Observation of a single runtime store. Carries no path or raw error."""

    name: str
    status: str
    exists: bool
    readable: bool
    schema_compatible: bool | None
    reason: str | None = None
    missing_tables: tuple[str, ...] = field(default_factory=tuple)
    migration_required: bool = False
    declared_foreign_key_count: int | None = None
    expected_foreign_key_count: int | None = None

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_payload(self) -> dict[str, Any]:
        return {
            "store": self.name,
            "status": self.status,
            "exists": self.exists,
            "readable": self.readable,
            "schema_compatible": self.schema_compatible,
            "reason": self.reason,
            "missing_tables": sorted(self.missing_tables),
            "migration_required": self.migration_required,
            "declared_foreign_key_count": self.declared_foreign_key_count,
            "expected_foreign_key_count": self.expected_foreign_key_count,
            "write_probed": False,
        }


def _classify_sqlite_error(exc: sqlite3.Error) -> tuple[str, str]:
    """Map a SQLite failure to (status, reason) without leaking its message."""
    cause = exc.__cause__
    classified = cause if isinstance(cause, sqlite3.Error) else exc
    if isinstance(classified, sqlite3.OperationalError):
        text = str(classified).lower()
        if "locked" in text or "busy" in text:
            return STATUS_BUSY, REASON_STORE_BUSY
        return STATUS_UNREADABLE, REASON_STORE_UNREADABLE
    if isinstance(classified, sqlite3.DatabaseError):
        return STATUS_MALFORMED, REASON_STORE_MALFORMED
    return STATUS_UNREADABLE, REASON_STORE_UNREADABLE


def _inspect_tables(
    name: str,
    *,
    exists: bool,
    required: frozenset[str],
    open_connection,
    missing_reason: str,
    uninitialized_reason: str,
    mismatch_reason: str,
    foreign_key_store: str | None = None,
    migration_reason: str | None = None,
) -> RuntimeStoreStatus:
    if not exists:
        return RuntimeStoreStatus(
            name=name,
            status=STATUS_MISSING,
            exists=False,
            readable=False,
            schema_compatible=None,
            reason=missing_reason,
        )
    connection = None
    foreign_keys: ForeignKeyInventory | None = None
    try:
        connection = open_connection()
        tables = sqlite_tables(connection)
        if foreign_key_store is not None:
            foreign_keys = inspect_required_foreign_keys(
                connection,
                store=foreign_key_store,
            )
    except FileNotFoundError:
        return RuntimeStoreStatus(
            name=name,
            status=STATUS_MISSING,
            exists=False,
            readable=False,
            schema_compatible=None,
            reason=missing_reason,
        )
    except sqlite3.Error as exc:
        status, reason = _classify_sqlite_error(exc)
        return RuntimeStoreStatus(
            name=name,
            status=status,
            exists=True,
            readable=False,
            schema_compatible=None,
            reason=reason,
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    if not tables:
        return RuntimeStoreStatus(
            name=name,
            status=STATUS_UNINITIALIZED,
            exists=True,
            readable=True,
            schema_compatible=False,
            reason=uninitialized_reason,
        )
    missing = tuple(sorted(required - tables))
    if missing:
        if not (required & tables):
            return RuntimeStoreStatus(
                name=name,
                status=STATUS_UNINITIALIZED,
                exists=True,
                readable=True,
                schema_compatible=False,
                reason=uninitialized_reason,
                missing_tables=missing,
            )
        return RuntimeStoreStatus(
            name=name,
            status=STATUS_SCHEMA_MISMATCH,
            exists=True,
            readable=True,
            schema_compatible=False,
            reason=mismatch_reason,
            missing_tables=missing,
        )
    if foreign_keys is not None and foreign_keys.status == FK_STATUS_MIGRATION_REQUIRED:
        return RuntimeStoreStatus(
            name=name,
            status=STATUS_MIGRATION_REQUIRED,
            exists=True,
            readable=True,
            schema_compatible=False,
            reason=migration_reason or mismatch_reason,
            migration_required=True,
            declared_foreign_key_count=foreign_keys.declared_count,
            expected_foreign_key_count=foreign_keys.expected_count,
        )
    if foreign_keys is not None and foreign_keys.status != FK_STATUS_READY:
        return RuntimeStoreStatus(
            name=name,
            status=STATUS_SCHEMA_MISMATCH,
            exists=True,
            readable=True,
            schema_compatible=False,
            reason=mismatch_reason,
            missing_tables=foreign_keys.missing_tables,
            declared_foreign_key_count=foreign_keys.declared_count,
            expected_foreign_key_count=foreign_keys.expected_count,
        )
    return RuntimeStoreStatus(
        name=name,
        status=STATUS_READY,
        exists=True,
        readable=True,
        schema_compatible=True,
        migration_required=False,
        declared_foreign_key_count=(
            foreign_keys.declared_count if foreign_keys is not None else None
        ),
        expected_foreign_key_count=(
            foreign_keys.expected_count if foreign_keys is not None else None
        ),
    )


def inspect_metadata_store() -> RuntimeStoreStatus:
    """Read-only metadata-store observation; never bootstraps the schema."""
    target = metadata_index_path(ensure_parent=False)
    return _inspect_tables(
        "metadata_index",
        exists=target.exists(),
        required=_METADATA_REQUIRED_TABLES,
        # read_only=True uses SQLite mode=ro + query_only and never creates.
        open_connection=lambda: connect_metadata_index(read_only=True),
        missing_reason=REASON_METADATA_MISSING,
        uninitialized_reason=REASON_METADATA_UNINITIALIZED,
        mismatch_reason=REASON_METADATA_SCHEMA_MISMATCH,
        foreign_key_store=STORE_METADATA,
        migration_reason=REASON_METADATA_MIGRATION_REQUIRED,
    )


def inspect_scheduler_store() -> RuntimeStoreStatus:
    """Read-only scheduler-store observation; never runs SCHEDULER_SCHEMA_SQL."""
    target = scheduler_db_path(ensure_parent=False)
    return _inspect_tables(
        "scheduler",
        exists=target.exists(),
        required=_SCHEDULER_REQUIRED_TABLES,
        open_connection=lambda: connect_scheduler_db_read_only(timeout=_INSPECTION_TIMEOUT_SECONDS),
        missing_reason=REASON_SCHEDULER_MISSING,
        uninitialized_reason=REASON_SCHEDULER_UNINITIALIZED,
        mismatch_reason=REASON_SCHEDULER_SCHEMA_MISMATCH,
        foreign_key_store=STORE_SCHEDULER,
        migration_reason=REASON_SCHEDULER_MIGRATION_REQUIRED,
    )


def inspect_executor_store() -> RuntimeStoreStatus:
    """Read-only durable run_jobs observation; never creates the table.

    Performs no enqueue, claim, heartbeat, or reconciliation.
    """
    target = scheduler_db_path(ensure_parent=False)
    return _inspect_tables(
        "run_executor",
        exists=target.exists(),
        required=_EXECUTOR_REQUIRED_TABLES,
        open_connection=lambda: connect_scheduler_db_read_only(timeout=_INSPECTION_TIMEOUT_SECONDS),
        missing_reason=REASON_SCHEDULER_MISSING,
        uninitialized_reason=REASON_EXECUTOR_UNINITIALIZED,
        mismatch_reason=REASON_EXECUTOR_UNINITIALIZED,
        foreign_key_store=STORE_SCHEDULER,
        migration_reason=REASON_SCHEDULER_MIGRATION_REQUIRED,
    )


def inspect_runtime_stores() -> dict[str, Any]:
    """Aggregate read-only observation of every local runtime store."""
    metadata = inspect_metadata_store()
    scheduler = inspect_scheduler_store()
    executor = inspect_executor_store()
    stores = (metadata, scheduler, executor)
    reasons = tuple(store.reason for store in stores if store.reason)
    return {
        "ready": all(store.ready for store in stores),
        "write_probed": False,
        "stores": {store.name: store.to_payload() for store in stores},
        "reasons": list(reasons),
    }

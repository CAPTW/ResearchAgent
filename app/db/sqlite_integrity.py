"""Central SQLite connection and required-foreign-key policy.

Every production SQLite connection is opened here so foreign-key enforcement,
busy timeouts, and read-only query protection are explicit and verified.  The
required-schema inspection is observation-only: it never bootstraps, migrates,
repairs, or changes journal mode.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

STORE_METADATA: Final = "metadata"
STORE_SCHEDULER: Final = "scheduler"
SUPPORTED_STORES: Final = frozenset({STORE_METADATA, STORE_SCHEDULER})

STATUS_READY: Final = "ready"
STATUS_UNINITIALIZED: Final = "uninitialized"
STATUS_MIGRATION_REQUIRED: Final = "migration_required"
STATUS_SCHEMA_MISMATCH: Final = "schema_mismatch"

CODE_FOREIGN_KEYS_DISABLED: Final = "sqlite_foreign_keys_disabled"
CODE_MIGRATION_REQUIRED: Final = "sqlite_fk_migration_required"
CODE_SCHEMA_MISMATCH: Final = "sqlite_schema_mismatch"
CODE_STORE_UNAVAILABLE: Final = "sqlite_store_unavailable"
CODE_POLICY_INVALID: Final = "sqlite_connection_policy_invalid"

DEFAULT_READ_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_METADATA_WRITE_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS: Final = 30.0


class SqliteIntegrityError(sqlite3.DatabaseError):
    """Stable, redacted SQLite policy failure."""

    code = "sqlite_integrity_failed"

    def __init__(self, store: str) -> None:
        self.store = store if store in SUPPORTED_STORES else "sqlite"
        super().__init__(f"{self.code}: {self.store}")


class SqliteForeignKeysDisabled(SqliteIntegrityError):
    code = CODE_FOREIGN_KEYS_DISABLED


class SqliteForeignKeyMigrationRequired(SqliteIntegrityError):
    code = CODE_MIGRATION_REQUIRED


class SqliteSchemaMismatch(SqliteIntegrityError):
    code = CODE_SCHEMA_MISMATCH


class SqliteStoreUnavailable(SqliteIntegrityError):
    code = CODE_STORE_UNAVAILABLE


class SqliteConnectionPolicyInvalid(SqliteIntegrityError):
    code = CODE_POLICY_INVALID


class ManagedSqliteConnection(sqlite3.Connection):
    """A sqlite3 connection whose context manager also closes the handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


@dataclass(frozen=True)
class RequiredForeignKey:
    store: str
    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    on_delete: str = "CASCADE"
    on_update: str = "NO ACTION"

    @property
    def relationship_id(self) -> str:
        child = ",".join(self.child_columns)
        parent = ",".join(self.parent_columns)
        return f"{self.child_table}.{child}->{self.parent_table}.{parent}"

    @property
    def signature(self) -> tuple[object, ...]:
        return (
            self.child_table,
            self.child_columns,
            self.parent_table,
            self.parent_columns,
            self.on_delete,
            self.on_update,
        )


METADATA_REQUIRED_FOREIGN_KEYS: Final = (
    RequiredForeignKey(STORE_METADATA, "run_topics", ("run_id",), "runs", ("run_id",)),
    RequiredForeignKey(STORE_METADATA, "papers", ("run_id",), "runs", ("run_id",)),
    RequiredForeignKey(STORE_METADATA, "evidence", ("run_id",), "runs", ("run_id",)),
    RequiredForeignKey(STORE_METADATA, "report_sections", ("run_id",), "runs", ("run_id",)),
)

SCHEDULER_REQUIRED_FOREIGN_KEYS: Final = (
    RequiredForeignKey(
        STORE_SCHEDULER,
        "scheduler_queue_items",
        ("schedule_id",),
        "scheduler_schedules",
        ("schedule_id",),
    ),
)

REQUIRED_FOREIGN_KEYS: Final = {
    STORE_METADATA: METADATA_REQUIRED_FOREIGN_KEYS,
    STORE_SCHEDULER: SCHEDULER_REQUIRED_FOREIGN_KEYS,
}

REQUIRED_TABLES: Final = {
    STORE_METADATA: frozenset(
        {
            "index_state",
            "runs",
            "run_topics",
            "papers",
            "evidence",
            "report_sections",
            "paper_reviews",
            "ai_review_drafts",
        }
    ),
    STORE_SCHEDULER: frozenset(
        {
            "scheduler_schedules",
            "scheduler_worker_heartbeats",
            "scheduler_queue_items",
            "run_jobs",
        }
    ),
}


@dataclass(frozen=True)
class ObservedForeignKey:
    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    on_delete: str
    on_update: str

    @property
    def signature(self) -> tuple[object, ...]:
        return (
            self.child_table,
            self.child_columns,
            self.parent_table,
            self.parent_columns,
            self.on_delete,
            self.on_update,
        )


@dataclass(frozen=True)
class ForeignKeyInventory:
    store: str
    status: str
    declared_count: int
    expected_count: int
    missing_relationships: tuple[str, ...]
    conflicting_count: int
    unexpected_count: int
    missing_tables: tuple[str, ...]
    invalid_child_column_count: int
    invalid_parent_key_count: int

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def migration_required(self) -> bool:
        return self.status == STATUS_MIGRATION_REQUIRED

    def to_payload(self) -> dict[str, object]:
        return {
            "store": self.store,
            "status": self.status,
            "declared_foreign_key_count": self.declared_count,
            "expected_foreign_key_count": self.expected_count,
            "missing_relationships": list(self.missing_relationships),
            "conflicting_relationship_count": self.conflicting_count,
            "unexpected_relationship_count": self.unexpected_count,
            "missing_tables": list(self.missing_tables),
            "invalid_child_column_count": self.invalid_child_column_count,
            "invalid_parent_key_count": self.invalid_parent_key_count,
            "migration_required": self.migration_required,
        }


def _validate_store(store: str) -> str:
    if store not in SUPPORTED_STORES:
        raise ValueError("unsupported SQLite logical store")
    return store


def _quoted_identifier(identifier: str) -> str:
    if not identifier or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in identifier):
        raise ValueError("unsafe SQLite identifier")
    return f'"{identifier}"'


def _pragma_scalar(connection: sqlite3.Connection, pragma: str) -> int:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("pragma returned no row")
    return int(row[0])


def verify_sqlite_pragmas(
    connection: sqlite3.Connection,
    *,
    store: str,
    read_only: bool,
    busy_timeout_ms: int,
) -> None:
    """Verify the exact connection policy without exposing connection details."""
    _validate_store(store)
    if _pragma_scalar(connection, "foreign_keys") != 1:
        raise SqliteForeignKeysDisabled(store)
    if _pragma_scalar(connection, "busy_timeout") != int(busy_timeout_ms):
        raise SqliteConnectionPolicyInvalid(store)
    query_only = _pragma_scalar(connection, "query_only")
    if query_only != (1 if read_only else 0):
        raise SqliteConnectionPolicyInvalid(store)


def configure_sqlite_read_write(
    connection: sqlite3.Connection,
    *,
    store: str,
    busy_timeout_ms: int,
) -> None:
    """Enable and verify enforcement before any transaction can begin."""
    _validate_store(store)
    if connection.in_transaction:
        raise SqliteConnectionPolicyInvalid(store)
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA foreign_keys = ON")
    verify_sqlite_pragmas(
        connection,
        store=store,
        read_only=False,
        busy_timeout_ms=busy_timeout_ms,
    )


def configure_sqlite_read_only(
    connection: sqlite3.Connection,
    *,
    store: str,
    busy_timeout_ms: int,
) -> None:
    """Enable verified query-only and FK policy on an existing store."""
    _validate_store(store)
    if connection.in_transaction:
        raise SqliteConnectionPolicyInvalid(store)
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    verify_sqlite_pragmas(
        connection,
        store=store,
        read_only=True,
        busy_timeout_ms=busy_timeout_ms,
    )


def open_sqlite_read_write(
    path: Path,
    *,
    store: str,
    timeout_seconds: float,
    isolation_level: str | None = "",
) -> ManagedSqliteConnection:
    """Open a write-capable connection with verified FK enforcement."""
    resolved_store = _validate_store(store)
    connection: ManagedSqliteConnection | None = None
    try:
        connection = sqlite3.connect(
            str(path),
            timeout=float(timeout_seconds),
            isolation_level=isolation_level,
            factory=ManagedSqliteConnection,
        )
        connection.row_factory = sqlite3.Row
        configure_sqlite_read_write(
            connection,
            store=resolved_store,
            busy_timeout_ms=max(1, int(float(timeout_seconds) * 1000)),
        )
        return connection
    except SqliteIntegrityError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise SqliteStoreUnavailable(resolved_store) from exc


def open_sqlite_read_only(
    path: Path,
    *,
    store: str,
    timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> ManagedSqliteConnection:
    """Open an existing file using URI mode=ro and verified query-only policy."""
    resolved_store = _validate_store(store)
    if not path.exists():
        raise FileNotFoundError("SQLite store does not exist")
    connection: ManagedSqliteConnection | None = None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=float(timeout_seconds),
            factory=ManagedSqliteConnection,
        )
        connection.row_factory = sqlite3.Row
        configure_sqlite_read_only(
            connection,
            store=resolved_store,
            busy_timeout_ms=max(1, int(float(timeout_seconds) * 1000)),
        )
        return connection
    except SqliteIntegrityError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise SqliteStoreUnavailable(resolved_store) from exc


def sqlite_user_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _observed_foreign_keys(
    connection: sqlite3.Connection,
    tables: set[str],
) -> tuple[ObservedForeignKey, ...]:
    observed: list[ObservedForeignKey] = []
    for table in sorted(tables):
        rows = connection.execute(
            f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row | tuple[object, ...]]] = {}
        for row in rows:
            grouped.setdefault(int(row[0]), []).append(row)
        for group in grouped.values():
            ordered = sorted(group, key=lambda item: int(item[1]))
            observed.append(
                ObservedForeignKey(
                    child_table=table,
                    child_columns=tuple(str(item[3]) for item in ordered),
                    parent_table=str(ordered[0][2]),
                    parent_columns=tuple(str(item[4]) for item in ordered),
                    on_update=str(ordered[0][5]).upper(),
                    on_delete=str(ordered[0][6]).upper(),
                )
            )
    return tuple(observed)


def _parent_key_is_unique(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> bool:
    table_info = connection.execute(
        f"PRAGMA table_info({_quoted_identifier(table)})"
    ).fetchall()
    primary_key = tuple(
        str(row[1])
        for row in sorted((row for row in table_info if int(row[5]) > 0), key=lambda row: int(row[5]))
    )
    if primary_key == columns:
        return True
    indexes = connection.execute(
        f"PRAGMA index_list({_quoted_identifier(table)})"
    ).fetchall()
    for index in indexes:
        if int(index[2]) != 1:
            continue
        index_name = str(index[1])
        index_columns = tuple(
            str(row[2])
            for row in connection.execute(
                f"PRAGMA index_info({_quoted_identifier(index_name)})"
            ).fetchall()
        )
        if index_columns == columns:
            return True
    return False


def inspect_required_foreign_keys(
    connection: sqlite3.Connection,
    *,
    store: str,
) -> ForeignKeyInventory:
    """Inspect exact required FK declarations without changing the database."""
    resolved_store = _validate_store(store)
    requirements = REQUIRED_FOREIGN_KEYS[resolved_store]
    required_tables = REQUIRED_TABLES[resolved_store]
    tables = sqlite_user_tables(connection)
    if not tables:
        return ForeignKeyInventory(
            store=resolved_store,
            status=STATUS_UNINITIALIZED,
            declared_count=0,
            expected_count=len(requirements),
            missing_relationships=tuple(
                requirement.relationship_id for requirement in requirements
            ),
            conflicting_count=0,
            unexpected_count=0,
            missing_tables=tuple(sorted(required_tables)),
            invalid_child_column_count=0,
            invalid_parent_key_count=0,
        )

    missing_tables = tuple(sorted(required_tables - tables))
    observed = _observed_foreign_keys(connection, tables)
    expected_signatures = {requirement.signature for requirement in requirements}
    observed_signatures = {relationship.signature for relationship in observed}
    missing = tuple(
        requirement.relationship_id
        for requirement in requirements
        if requirement.signature not in observed_signatures
    )
    expected_child_columns = {
        (requirement.child_table, requirement.child_columns)
        for requirement in requirements
    }
    unexpected = [
        relationship
        for relationship in observed
        if relationship.signature not in expected_signatures
    ]
    conflicting = [
        relationship
        for relationship in unexpected
        if (relationship.child_table, relationship.child_columns)
        in expected_child_columns
    ]
    invalid_child_columns = sum(
        1
        for requirement in requirements
        if requirement.child_table in tables
        and not set(requirement.child_columns).issubset(
            {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_quoted_identifier(requirement.child_table)})"
                ).fetchall()
            }
        )
    )
    invalid_parent_keys = sum(
        1
        for requirement in requirements
        if requirement.parent_table in tables
        and not _parent_key_is_unique(
            connection,
            requirement.parent_table,
            requirement.parent_columns,
        )
    )

    scheduler_v2_candidate = bool(
        resolved_store == STORE_SCHEDULER
        and missing_tables == ("run_jobs",)
        and missing
        == (
            SCHEDULER_REQUIRED_FOREIGN_KEYS[0].relationship_id,
        )
        and not unexpected
        and invalid_child_columns == 0
        and invalid_parent_keys == 0
    )

    if (
        missing_tables
        and not scheduler_v2_candidate
        or unexpected
        or invalid_child_columns
        or invalid_parent_keys
    ):
        status = STATUS_SCHEMA_MISMATCH
    elif missing or scheduler_v2_candidate:
        status = STATUS_MIGRATION_REQUIRED
    elif len(observed) != len(requirements):
        status = STATUS_SCHEMA_MISMATCH
    else:
        status = STATUS_READY
    return ForeignKeyInventory(
        store=resolved_store,
        status=status,
        declared_count=len(observed),
        expected_count=len(requirements),
        missing_relationships=missing,
        conflicting_count=len(conflicting),
        unexpected_count=len(unexpected),
        missing_tables=missing_tables,
        invalid_child_column_count=invalid_child_columns,
        invalid_parent_key_count=invalid_parent_keys,
    )


def require_store_write_compatible(
    connection: sqlite3.Connection,
    *,
    store: str,
) -> ForeignKeyInventory:
    """Fail closed before callers can mutate a legacy or mismatched store."""
    inventory = inspect_required_foreign_keys(connection, store=store)
    if inventory.status == STATUS_MIGRATION_REQUIRED:
        raise SqliteForeignKeyMigrationRequired(store)
    if inventory.status != STATUS_READY:
        raise SqliteSchemaMismatch(store)
    return inventory

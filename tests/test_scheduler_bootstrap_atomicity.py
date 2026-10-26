"""Scheduler schema initialization on disposable databases, without workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from app.db import bootstrap
from app.db.sqlite_integrity import (
    DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS,
    SqliteForeignKeyMigrationRequired,
    SqliteSchemaMismatch,
)


TABLES = {
    "scheduler_schedules", "scheduler_worker_heartbeats", "scheduler_queue_items", "run_jobs",
}
INDEXES = {
    "idx_scheduler_due", "idx_scheduler_lease", "idx_scheduler_queue_status",
    "idx_scheduler_queue_schedule", "idx_run_jobs_active_run", "idx_run_jobs_claim", "idx_run_jobs_lease",
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    return bootstrap.scheduler_db_path(ensure_parent=False)


def snapshot(path: Path) -> tuple[list[tuple], dict[str, list[tuple]], str]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        contents = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
            for table in sorted(TABLES & {row[1] for row in schema if row[0] == "table"})
        }
        return schema, contents, str(connection.execute("PRAGMA journal_mode").fetchone()[0])


def seed_existing_rows() -> None:
    with closing(bootstrap.connect_scheduler_db()) as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO scheduler_schedules(schedule_id,profile_id,task_type,updated_at,payload_json) "
            "VALUES ('schedule-1','profile-1','research','2026-10-01T00:00:00Z',?)",
            ('{"title":"합성 스케줄","preserve":"e\\u0301"}',),
        )
        connection.execute(
            "INSERT INTO scheduler_queue_items(queue_item_id,schedule_id,due_at,status,created_at,updated_at,payload_json) "
            "VALUES ('queue-1','schedule-1','due','queued','created','updated','{}')"
        )
        connection.execute(
            "INSERT INTO run_jobs(job_id,run_id,handler,state,available_at,created_at,updated_at) "
            "VALUES ('job-1','run-1','research','queued','available','created','updated')"
        )
        connection.commit()


def test_schema_ddl_uses_one_explicit_transaction(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_events: list[tuple[str, bool]] = []
    statements: list[str] = []
    original = bootstrap.open_sqlite_read_write

    def observed_open(*args, **kwargs):
        connection = original(*args, **kwargs)

        def authorize(action, name, _other, _database, _source):
            if action in {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX} and not name.startswith("sqlite_"):
                create_events.append((name, connection.in_transaction))
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", observed_open)
    with closing(bootstrap.connect_scheduler_db()) as connection:
        assert not connection.in_transaction
        assert connection.isolation_level is None
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == int(DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS * 1000)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {name for name, _active in create_events} == TABLES | INDEXES
    assert all(active for _name, active in create_events), create_events
    transactions = [statement.strip().upper().rstrip(";") for statement in statements
                    if statement.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]
    assert transactions == ["BEGIN DEFERRED", "COMMIT"]


@pytest.mark.parametrize("existing", [False, True])
def test_denied_late_ddl_rolls_back_prior_objects_and_retains_rows(
    store: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    if existing:
        seed_existing_rows()
        # A compatible store can lack non-FK lookup indexes. Rebuilding them
        # must not leave an earlier index committed when a later DDL fails.
        with closing(sqlite3.connect(store, isolation_level=None)) as connection:
            connection.execute("DROP INDEX idx_scheduler_due")
            connection.execute("DROP INDEX idx_run_jobs_claim")
    else:
        sqlite3.connect(store).close()
    before = snapshot(store)
    original = bootstrap.open_sqlite_read_write
    opened: list[sqlite3.Connection] = []
    denied = []

    def denied_open(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)

        def authorize(action, name, _other, _database, _source):
            if action == sqlite3.SQLITE_CREATE_INDEX and name == "idx_run_jobs_claim":
                denied.append(name)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", denied_open)
    with pytest.raises(SqliteSchemaMismatch):
        bootstrap.connect_scheduler_db()
    assert denied == ["idx_run_jobs_claim"]
    assert snapshot(store) == before
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", original)
    with closing(bootstrap.connect_scheduler_db()) as connection:
        assert bootstrap.sqlite_tables(connection) == TABLES
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    if existing:
        assert snapshot(store)[1] == before[1]
    else:
        assert all(not rows for rows in snapshot(store)[1].values())


@pytest.mark.parametrize("error", [SqliteSchemaMismatch, SqliteForeignKeyMigrationRequired, ValueError, KeyboardInterrupt])
def test_failed_postcreation_validation_rolls_back_schema(
    store: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException],
) -> None:
    checked = []
    opened: list[sqlite3.Connection] = []
    original_validation = bootstrap.require_store_write_compatible
    failure = error("scheduler")

    def reject(connection, *, store):
        opened.append(connection)
        checked.append(bootstrap.sqlite_tables(connection))
        raise failure

    monkeypatch.setattr(bootstrap, "require_store_write_compatible", reject)
    try:
        with pytest.raises(error) as caught:
            bootstrap.connect_scheduler_db()
        assert caught.value is failure
        assert checked == [TABLES]
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        assert snapshot(store)[0] == []
    finally:
        for connection in opened:
            connection.close()
    monkeypatch.setattr(bootstrap, "require_store_write_compatible", original_validation)
    with closing(bootstrap.connect_scheduler_db()) as connection:
        assert bootstrap.sqlite_tables(connection) == TABLES
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    schema, rows, mode = snapshot(store)
    assert {row[1] for row in schema} == TABLES | INDEXES
    assert all(not records for records in rows.values()) and mode == "wal"


def test_failed_commit_rolls_back_schema_and_closes_connection(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = bootstrap.open_sqlite_read_write
    opened: list[sqlite3.Connection] = []
    denied = []

    def denied_open(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)

        def authorize(action, name, _other, _database, _source):
            if action == sqlite3.SQLITE_TRANSACTION and name == "COMMIT":
                denied.append(name)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", denied_open)
    try:
        with pytest.raises(SqliteSchemaMismatch):
            bootstrap.connect_scheduler_db()
        assert denied == ["COMMIT"]
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        assert snapshot(store)[0] == []
    finally:
        for connection in opened:
            connection.close()
    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", original)
    with closing(bootstrap.connect_scheduler_db()) as connection:
        assert bootstrap.sqlite_tables(connection) == TABLES
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    schema, rows, mode = snapshot(store)
    assert {row[1] for row in schema} == TABLES | INDEXES
    assert all(not records for records in rows.values()) and mode == "wal"


def test_repeated_bootstrap_preserves_schema_and_existing_records(store: Path) -> None:
    seed_existing_rows()
    before = snapshot(store)
    for _ in range(2):
        with closing(bootstrap.connect_scheduler_db()) as connection:
            assert not connection.in_transaction
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert snapshot(store) == before


def test_complete_schema_remains_readable_while_another_connection_holds_writer(store: Path) -> None:
    seed_existing_rows()
    before = snapshot(store)
    with closing(bootstrap.connect_scheduler_db()) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE scheduler_schedules SET profile_id = 'not-committed' WHERE schedule_id = 'schedule-1'")
        try:
            # Production retains its original 30-second busy timeout. A
            # complete WAL schema needs no DDL write and must remain readable.
            with closing(bootstrap.connect_scheduler_db()) as reader:
                assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == int(DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS * 1000)
                assert reader.execute("SELECT profile_id FROM scheduler_schedules WHERE schedule_id = 'schedule-1'").fetchone()[0] == "profile-1"
                assert writer.in_transaction and not reader.in_transaction
        finally:
            writer.rollback()
    assert snapshot(store) == before


@pytest.mark.parametrize("legacy", [False, True])
def test_incompatible_store_is_rejected_without_schema_or_journal_changes(store: Path, legacy: bool) -> None:
    with closing(sqlite3.connect(store)) as connection:
        if legacy:
            sql = bootstrap.SCHEDULER_SCHEMA_SQL.replace(
                ",\n  FOREIGN KEY (schedule_id) REFERENCES scheduler_schedules(schedule_id)\n"
                "    ON DELETE CASCADE\n    ON UPDATE NO ACTION", "",
            )
            assert sql != bootstrap.SCHEDULER_SCHEMA_SQL
            connection.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        else:
            connection.execute("CREATE TABLE scheduler_schedules(schedule_id TEXT PRIMARY KEY)")
    before = snapshot(store)
    expected = SqliteForeignKeyMigrationRequired if legacy else SqliteSchemaMismatch
    with pytest.raises(expected):
        bootstrap.connect_scheduler_db()
    assert snapshot(store) == before
    with closing(bootstrap.connect_scheduler_db_read_only()) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden(value TEXT)")
    assert snapshot(store) == before


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_bootstrap_connections_finish_with_complete_unchanged_store(
    store: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    if existing:
        seed_existing_rows()
        before = snapshot(store)
    gate = Barrier(3)
    original = bootstrap.open_sqlite_read_write

    def simultaneous_open(*args, **kwargs):
        connection = original(*args, **kwargs)
        try:
            gate.wait(timeout=10)
        except BaseException:
            connection.close()
            raise
        return connection

    def initialize(_):
        with closing(bootstrap.connect_scheduler_db()) as connection:
            assert not connection.in_transaction
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            return bootstrap.sqlite_tables(connection)

    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", simultaneous_open)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(initialize, range(3)))
    assert results == [TABLES] * 3
    if existing:
        assert snapshot(store) == before
    else:
        schema, rows, mode = snapshot(store)
        assert {row[1] for row in schema} == TABLES | INDEXES
        assert all(not records for records in rows.values()) and mode == "wal"

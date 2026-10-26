"""Transient scheduler contention must not claim that the schema is invalid."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from app.db import bootstrap
from app.db.sqlite_integrity import (
    DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS, SqliteSchemaMismatch, SqliteStoreUnavailable,
)
from app.services import topic_schedules
from tests.test_scheduler_bootstrap_atomicity import INDEXES, seed_existing_rows, snapshot


def _prepare_repair() -> Path:
    seed_existing_rows()
    path = bootstrap.scheduler_db_path(ensure_parent=False)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("DROP INDEX idx_scheduler_due")
    return path


@pytest.mark.parametrize("caller", ["connection", "db_backed", "queue_backed"])
def test_real_writer_blocks_index_repair_as_unavailable_then_retry_preserves_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str,
) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    path = _prepare_repair()
    before = snapshot(path)
    original = bootstrap.open_sqlite_read_write
    opened: list[sqlite3.Connection] = []

    def observed_open(*args, **kwargs):
        connection = original(*args, **kwargs)
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == int(
            DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS * 1000
        )
        opened.append(connection)
        return connection

    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", observed_open)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE run_jobs SET state='running' WHERE job_id='job-1'")
        try:
            if caller == "connection":
                with pytest.raises(SqliteStoreUnavailable) as error:
                    bootstrap.connect_scheduler_db()
                assert error.value.code == "sqlite_store_unavailable"
                assert error.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
            else:
                descriptor = topic_schedules.get_scheduler_backend(mode=caller).descriptor()
                assert descriptor["available"] is False
                assert descriptor["read_only"] is True
                assert descriptor["unavailable_reason"] == (
                    f"{caller} unavailable: sqlite_store_unavailable: scheduler"
                )
            assert writer.in_transaction
            assert snapshot(path) == before
        finally:
            writer.rollback()
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    with closing(bootstrap.connect_scheduler_db()) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    after = snapshot(path)
    assert after[1:] == before[1:]
    assert {row[1] for row in after[0] if row[0] == "index"} == INDEXES
    if caller != "connection":
        assert topic_schedules.get_scheduler_backend(mode=caller).descriptor()["available"] is True


def test_schedule_api_exposes_transient_reason_and_recovers_after_writer_releases(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    first = client.get("/topic-schedules")
    assert first.status_code == 200, first.text
    assert first.json()["items"]
    path = bootstrap.scheduler_db_path(ensure_parent=False)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("DROP INDEX idx_scheduler_due")
    before = snapshot(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            response = client.get("/topic-schedules")
            assert response.status_code == 200, response.text
            backend = response.json()["scheduler"]["backend"]
            assert backend["available"] is False
            assert backend["read_only"] is True
            assert backend["unavailable_reason"] == (
                "db_backed unavailable: sqlite_store_unavailable: scheduler"
            )
            assert str(path) not in response.text
            assert writer.in_transaction
            assert snapshot(path) == before
        finally:
            writer.rollback()
    response = client.get("/topic-schedules")
    assert response.status_code == 200, response.text
    assert response.json()["scheduler"]["backend"]["available"] is True
    assert response.json()["scheduler"]["backend"]["unavailable_reason"] is None
    assert snapshot(path)[1] == before[1]


@pytest.mark.parametrize("code,expected", [
    (sqlite3.SQLITE_BUSY, SqliteStoreUnavailable),
    (sqlite3.SQLITE_LOCKED, SqliteStoreUnavailable),
    (sqlite3.SQLITE_BUSY_SNAPSHOT, SqliteStoreUnavailable),
    (sqlite3.SQLITE_LOCKED_SHAREDCACHE, SqliteStoreUnavailable),
    (sqlite3.SQLITE_ERROR, SqliteSchemaMismatch),
    (sqlite3.SQLITE_CORRUPT, SqliteSchemaMismatch),
    (None, SqliteSchemaMismatch),
])
def test_error_category_preserves_extended_contention_and_other_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int | None, expected: type[Exception],
) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    seed_existing_rows()
    path = bootstrap.scheduler_db_path(ensure_parent=False)
    before = snapshot(path)
    error = sqlite3.OperationalError("locked: private diagnostic path")
    if code is not None:
        error.sqlite_errorcode = code
    opened: list[sqlite3.Connection] = []

    def fail_inspection(connection: sqlite3.Connection) -> set[str]:
        opened.append(connection)
        raise error

    monkeypatch.setattr(bootstrap, "sqlite_user_tables", fail_inspection)
    with pytest.raises(expected) as result:
        bootstrap.connect_scheduler_db()
    assert result.value.__cause__ is error
    assert "private" not in str(result.value)
    assert snapshot(path) == before
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")

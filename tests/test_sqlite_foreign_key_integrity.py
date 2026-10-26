from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import bootstrap
from app.db import sqlite_integrity as integrity
from app.db import sqlite_migrations as migrations
from app.ops import runtime_stores
from app.repositories.file_locks import file_lock


RELATIONSHIPS = (
    (integrity.STORE_METADATA, "run_topics"),
    (integrity.STORE_METADATA, "papers"),
    (integrity.STORE_METADATA, "evidence"),
    (integrity.STORE_METADATA, "report_sections"),
    (integrity.STORE_SCHEDULER, "scheduler_queue_items"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _sha256(path),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _strip_required_foreign_keys(schema: str, expected_count: int) -> str:
    legacy, count = re.subn(
        r",\s*FOREIGN KEY \([^)]*\) REFERENCES [^(]+\([^)]*\)"
        r"\s*ON DELETE CASCADE\s*ON UPDATE NO ACTION",
        "",
        schema,
        flags=re.IGNORECASE,
    )
    assert count == expected_count
    return legacy


def _schema_for(store: str, *, legacy: bool) -> str:
    if store == integrity.STORE_METADATA:
        schema = bootstrap.SCHEMA_SQL
        expected_count = 4
    else:
        schema = bootstrap.SCHEDULER_SCHEMA_SQL
        expected_count = 1
    return (
        _strip_required_foreign_keys(schema, expected_count)
        if legacy
        else schema
    )


def _create_database(path: Path, store: str, *, legacy: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_schema_for(store, legacy=legacy))
    finally:
        connection.close()


def _patch_store_paths(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> tuple[Path, Path]:
    storage_root = root / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    return root / "metadata_index.sqlite3", root / "scheduler.sqlite3"


def _insert_run(connection: sqlite3.Connection, run_id: str = "parent-run") -> None:
    connection.execute(
        """
        INSERT INTO runs (
          run_id, topic, topic_slug, status, created_at, updated_at,
          root_run_id, lineage_mode
        ) VALUES (?, 'Topic', 'topic', 'completed',
                  '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z',
                  ?, 'root')
        """,
        (run_id, run_id),
    )


def _insert_schedule(
    connection: sqlite3.Connection,
    schedule_id: str = "parent-schedule",
) -> None:
    connection.execute(
        """
        INSERT INTO scheduler_schedules (
          schedule_id, profile_id, task_type, enabled, deleted,
          scheduler_state, dispatch_attempts, updated_at, payload_json
        ) VALUES (?, 'profile', 'research', 1, 0, 'scheduled', 0,
                  '2026-07-27T00:00:00Z', '{}')
        """,
        (schedule_id,),
    )


def _insert_child(
    connection: sqlite3.Connection,
    table: str,
    parent_id: str,
    *,
    suffix: str = "one",
) -> None:
    if table == "run_topics":
        connection.execute(
            "INSERT INTO run_topics (run_id, topic) VALUES (?, ?)",
            (parent_id, f"topic-{suffix}"),
        )
    elif table == "papers":
        connection.execute(
            """
            INSERT INTO papers (
              run_id, paper_id, title, theme, status
            ) VALUES (?, ?, 'Paper', 'theme', 'selected')
            """,
            (parent_id, f"paper-{suffix}"),
        )
    elif table == "evidence":
        connection.execute(
            """
            INSERT INTO evidence (
              run_id, evidence_id, source_id, paper_id, title,
              source_type, citation_text, snippet
            ) VALUES (?, ?, 'source', 'paper', 'Evidence',
                      'paper', 'citation', 'snippet')
            """,
            (parent_id, f"evidence-{suffix}"),
        )
    elif table == "report_sections":
        connection.execute(
            """
            INSERT INTO report_sections (
              run_id, section_id, title, updated_at
            ) VALUES (?, ?, 'Section', '2026-07-27T00:00:00Z')
            """,
            (parent_id, f"section-{suffix}"),
        )
    elif table == "scheduler_queue_items":
        connection.execute(
            """
            INSERT INTO scheduler_queue_items (
              queue_item_id, schedule_id, due_at, status,
              dispatch_attempts, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, 'queued', 0,
                      '2026-07-27T00:00:00Z',
                      '2026-07-27T00:00:00Z', '{}')
            """,
            (
                f"queue-{suffix}",
                parent_id,
                f"2026-07-27T00:00:0{len(suffix) % 10}Z",
            ),
        )
    else:  # pragma: no cover - protects the test helper itself
        raise AssertionError(f"unsupported table: {table}")


def _insert_parent(connection: sqlite3.Connection, store: str, parent_id: str) -> None:
    if store == integrity.STORE_METADATA:
        _insert_run(connection, parent_id)
    else:
        _insert_schedule(connection, parent_id)


def _parent_table(store: str) -> str:
    return "runs" if store == integrity.STORE_METADATA else "scheduler_schedules"


def _parent_column(store: str) -> str:
    return "run_id" if store == integrity.STORE_METADATA else "schedule_id"


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _schema_snapshot(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') "
            "FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        return tuple(tuple(str(value) for value in row) for row in rows)
    finally:
        connection.close()


def _index_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()


def _content_snapshot(path: Path, tables: tuple[str, ...]) -> dict[str, list[tuple[Any, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: [
                tuple(row)
                for row in connection.execute(
                    f'SELECT rowid, * FROM "{table}" ORDER BY rowid'
                ).fetchall()
            ]
            for table in tables
        }
    finally:
        connection.close()


def _populate_legacy(path: Path, store: str, *, add_schema_objects: bool = False) -> None:
    _create_database(path, store, legacy=True)
    connection = sqlite3.connect(path)
    try:
        if store == integrity.STORE_METADATA:
            _insert_run(connection)
            for _, table in RELATIONSHIPS[:4]:
                _insert_child(connection, table, "parent-run")
            if add_schema_objects:
                connection.executescript(
                    """
                    CREATE TABLE migration_trigger_log (item TEXT NOT NULL);
                    CREATE TRIGGER papers_migration_probe
                    AFTER INSERT ON papers
                    BEGIN
                      INSERT INTO migration_trigger_log (item) VALUES (NEW.paper_id);
                    END;
                    CREATE VIEW papers_migration_view AS
                    SELECT run_id, paper_id FROM papers;
                    """
                )
        else:
            _insert_schedule(connection)
            _insert_child(
                connection,
                "scheduler_queue_items",
                "parent-schedule",
            )
            if add_schema_objects:
                connection.executescript(
                    """
                    CREATE TABLE migration_trigger_log (item TEXT NOT NULL);
                    CREATE TRIGGER queue_migration_probe
                    AFTER INSERT ON scheduler_queue_items
                    BEGIN
                      INSERT INTO migration_trigger_log (item)
                      VALUES (NEW.queue_item_id);
                    END;
                    CREATE VIEW queue_migration_view AS
                    SELECT schedule_id, queue_item_id FROM scheduler_queue_items;
                    """
                )
        connection.commit()
    finally:
        connection.close()


def _pragma(connection: sqlite3.Connection, name: str) -> int:
    return int(connection.execute(f"PRAGMA {name}").fetchone()[0])


def test_fresh_schema_declares_only_the_five_approved_foreign_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    bootstrap.bootstrap_runtime_stores()

    expected = {
        integrity.STORE_METADATA: (metadata_path, 4),
        integrity.STORE_SCHEDULER: (scheduler_path, 1),
    }
    before_schemas = {
        store: _schema_snapshot(path)
        for store, (path, _count) in expected.items()
    }
    for store, (path, expected_count) in expected.items():
        connection = integrity.open_sqlite_read_only(path, store=store)
        try:
            inventory = integrity.inspect_required_foreign_keys(
                connection,
                store=store,
            )
            assert inventory.ready is True
            assert inventory.declared_count == expected_count
            assert inventory.unexpected_count == 0
            assert inventory.invalid_child_column_count == 0
            assert inventory.invalid_parent_key_count == 0
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            connection.close()

    metadata_indexes = _index_names(metadata_path)
    scheduler_indexes = _index_names(scheduler_path)
    assert {"idx_papers_run_score", "idx_evidence_source"} <= metadata_indexes
    assert {
        "idx_scheduler_queue_status",
        "idx_scheduler_queue_schedule",
    } <= scheduler_indexes

    bootstrap.bootstrap_runtime_stores()
    assert {
        store: _schema_snapshot(path)
        for store, (path, _count) in expected.items()
    } == before_schemas


def test_central_connection_policy_is_verified_for_all_four_connection_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_store_paths(monkeypatch, tmp_path)
    bootstrap.bootstrap_runtime_stores()
    cases = (
        (bootstrap.connect_metadata_index(), False, 5_000),
        (bootstrap.connect_metadata_index(read_only=True), True, 5_000),
        (bootstrap.connect_scheduler_db(), False, 30_000),
        (bootstrap.connect_scheduler_db_read_only(), True, 5_000),
    )
    for connection, read_only, timeout_ms in cases:
        try:
            assert _pragma(connection, "foreign_keys") == 1
            assert _pragma(connection, "query_only") == int(read_only)
            assert _pragma(connection, "busy_timeout") == timeout_ms
            assert connection.in_transaction is False
            if read_only:
                with pytest.raises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE forbidden_write (value TEXT)")
        finally:
            connection.close()
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_connection_policy_rejects_configuration_inside_a_transaction(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "transaction.sqlite3")
    try:
        connection.execute("BEGIN")
        with pytest.raises(integrity.SqliteConnectionPolicyInvalid):
            integrity.configure_sqlite_read_write(
                connection,
                store=integrity.STORE_METADATA,
                busy_timeout_ms=1_000,
            )
        assert _pragma(connection, "foreign_keys") == 0
    finally:
        connection.rollback()
        connection.close()


def test_connection_policy_failure_closes_the_failed_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[sqlite3.Connection] = []
    real_connect = integrity.sqlite3.connect

    def capture_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        captured.append(connection)
        return connection

    def reject_policy(*_args, **_kwargs):
        raise integrity.SqliteForeignKeysDisabled(integrity.STORE_METADATA)

    monkeypatch.setattr(integrity.sqlite3, "connect", capture_connect)
    monkeypatch.setattr(integrity, "configure_sqlite_read_write", reject_policy)
    with pytest.raises(integrity.SqliteForeignKeysDisabled):
        integrity.open_sqlite_read_write(
            tmp_path / "closed.sqlite3",
            store=integrity.STORE_METADATA,
            timeout_seconds=1.0,
        )
    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


@pytest.mark.parametrize(
    ("store", "table"),
    RELATIONSHIPS,
    ids=[table for _store, table in RELATIONSHIPS],
)
def test_each_approved_relationship_enforces_insert_cascade_and_no_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
    table: str,
) -> None:
    metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    bootstrap.bootstrap_runtime_stores()
    path = metadata_path if store == integrity.STORE_METADATA else scheduler_path
    connection = integrity.open_sqlite_read_write(
        path,
        store=store,
        timeout_seconds=2.0,
        isolation_level=None,
    )
    try:
        connection.execute("BEGIN")
        _insert_parent(connection, store, "parent")
        _insert_child(connection, table, "parent")
        connection.execute("COMMIT")

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_child(connection, table, "missing", suffix="orphan")
        connection.execute("ROLLBACK")
        assert _row_count(connection, table) == 1

        connection.execute("BEGIN")
        if store == integrity.STORE_METADATA:
            connection.execute(
                "INSERT INTO index_state (key, value) VALUES ('unrelated', 'value')"
            )
        else:
                connection.execute(
                    """
                    INSERT INTO scheduler_worker_heartbeats (
                      worker_id, mode, backend_mode, enabled, running,
                      started_at, heartbeat_at, last_cycle_summary_json
                    ) VALUES (
                      'worker', 'embedded', 'db_backed', 1, 1,
                      'now', 'now', '{}'
                    )
                    """
                )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_child(connection, table, "missing", suffix="transaction")
        connection.execute("ROLLBACK")
        unrelated_count = connection.execute(
            "SELECT COUNT(*) FROM index_state WHERE key = 'unrelated'"
            if store == integrity.STORE_METADATA
            else "SELECT COUNT(*) FROM scheduler_worker_heartbeats "
            "WHERE worker_id = 'worker'"
        ).fetchone()[0]
        assert unrelated_count == 0

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f'UPDATE "{_parent_table(store)}" '
                f'SET "{_parent_column(store)}" = ? '
                f'WHERE "{_parent_column(store)}" = ?',
                ("renamed", "parent"),
            )
        connection.execute("ROLLBACK")

        connection.execute("BEGIN")
        connection.execute(
            f'DELETE FROM "{_parent_table(store)}" '
            f'WHERE "{_parent_column(store)}" = ?',
            ("parent",),
        )
        connection.execute("COMMIT")
        assert _row_count(connection, table) == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "store",
    (integrity.STORE_METADATA, integrity.STORE_SCHEDULER),
)
def test_legacy_schema_allows_read_only_visibility_but_blocks_all_normal_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
) -> None:
    metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    path = metadata_path if store == integrity.STORE_METADATA else scheduler_path
    _create_database(path, store, legacy=True)
    before = _tree_snapshot(tmp_path)

    read_only = (
        bootstrap.connect_metadata_index(read_only=True)
        if store == integrity.STORE_METADATA
        else bootstrap.connect_scheduler_db_read_only()
    )
    try:
        inventory = integrity.inspect_required_foreign_keys(
            read_only,
            store=store,
        )
        assert inventory.status == integrity.STATUS_MIGRATION_REQUIRED
        assert _pragma(read_only, "foreign_keys") == 1
        assert _pragma(read_only, "query_only") == 1
    finally:
        read_only.close()

    with pytest.raises(integrity.SqliteForeignKeyMigrationRequired):
        if store == integrity.STORE_METADATA:
            bootstrap.connect_metadata_index()
        else:
            bootstrap.connect_scheduler_db()
    assert _tree_snapshot(tmp_path) == before


def test_plan_statuses_digest_and_no_write_contract(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _populate_legacy(legacy, integrity.STORE_METADATA)
    before = _tree_snapshot(tmp_path)
    first = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=legacy,
    )
    second = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=legacy,
    )
    assert first.status == integrity.STATUS_MIGRATION_REQUIRED
    assert first.eligible is True
    assert first.plan_digest == second.plan_digest
    assert first.total_orphan_count == 0
    assert first.backup_required is True
    assert _tree_snapshot(tmp_path) == before

    payload = first.to_payload()
    blob = json.dumps(payload, sort_keys=True)
    assert str(tmp_path) not in blob
    assert "SELECT " not in blob
    assert "parent-run" not in blob

    connection = sqlite3.connect(legacy)
    connection.execute(
        "INSERT INTO index_state (key, value) VALUES ('digest-change', 'one')"
    )
    connection.commit()
    connection.close()
    changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=legacy,
    )
    assert changed.plan_digest != first.plan_digest

    missing = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=tmp_path / "missing.sqlite3",
    )
    assert missing.status == migrations.STATUS_MISSING
    assert not (tmp_path / "missing.sqlite3").exists()

    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    assert migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=empty,
    ).status == integrity.STATUS_UNINITIALIZED

    partial = tmp_path / "partial.sqlite3"
    connection = sqlite3.connect(partial)
    connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    connection.close()
    assert migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=partial,
    ).status == integrity.STATUS_SCHEMA_MISMATCH

    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"not a sqlite database")
    assert migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=malformed,
    ).status == migrations.STATUS_MALFORMED


@pytest.mark.parametrize(
    ("store", "expected_rebuilds", "child_tables"),
    (
        (
            integrity.STORE_METADATA,
            4,
            ("run_topics", "papers", "evidence", "report_sections"),
        ),
        (
            integrity.STORE_SCHEDULER,
            1,
            ("scheduler_queue_items",),
        ),
    ),
)
def test_explicit_migration_rebuilds_only_children_and_keeps_backup_and_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
    expected_rebuilds: int,
    child_tables: tuple[str, ...],
) -> None:
    metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    path = metadata_path if store == integrity.STORE_METADATA else scheduler_path
    _populate_legacy(path, store, add_schema_objects=True)
    before_content = _content_snapshot(path, child_tables)
    before_parent_sql = {
        row[1]: row[3]
        for row in _schema_snapshot(path)
        if row[0] == "table"
        and row[1]
        in {"runs", "scheduler_schedules", "run_jobs", "paper_reviews"}
    }
    plan = migrations.plan_sqlite_foreign_key_migration(store=store, path=path)

    result = migrations.apply_sqlite_foreign_key_migration(
        store=store,
        path=path,
        expected_plan_digest=plan.plan_digest,
    )
    assert result.status == "migration_applied"
    assert result.applied is True
    assert result.rebuilt_table_count == expected_rebuilds
    assert result.backup_id
    backup_path = (
        path.parent
        / ".sqlite-fk-backups"
        / f"{result.backup_id}.sqlite3"
    )
    assert backup_path.is_file()
    assert _sha256(backup_path) == result.backup_sha256
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        backup.close()

    assert _content_snapshot(path, child_tables) == before_content
    after_schema = _schema_snapshot(path)
    after_parent_sql = {
        row[1]: row[3]
        for row in after_schema
        if row[0] == "table" and row[1] in before_parent_sql
    }
    assert after_parent_sql == before_parent_sql
    assert not any("__xrml_fk_v1_" in row[1] for row in after_schema)
    backups_before = list((path.parent / ".sqlite-fk-backups").iterdir())
    current = migrations.plan_sqlite_foreign_key_migration(store=store, path=path)
    second = migrations.apply_sqlite_foreign_key_migration(
        store=store,
        path=path,
        expected_plan_digest=current.plan_digest,
    )
    assert second.status == "already_compliant"
    assert second.applied is False
    assert second.rebuilt_table_count == 0
    assert list((path.parent / ".sqlite-fk-backups").iterdir()) == backups_before

    if store == integrity.STORE_METADATA:
        assert any(row[1] == "papers_migration_probe" for row in after_schema)
        assert any(row[1] == "papers_migration_view" for row in after_schema)
        product_connection = bootstrap.connect_metadata_index()
    else:
        assert any(row[1] == "queue_migration_probe" for row in after_schema)
        assert any(row[1] == "queue_migration_view" for row in after_schema)
        product_connection = bootstrap.connect_scheduler_db()
    try:
        inventory = integrity.require_store_write_compatible(
            product_connection,
            store=store,
        )
        assert inventory.declared_count == expected_rebuilds
        assert product_connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert product_connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        product_connection.close()


@pytest.mark.parametrize(
    ("store", "table"),
    RELATIONSHIPS,
    ids=[f"orphan-{table}" for _store, table in RELATIONSHIPS],
)
def test_each_orphan_relation_blocks_before_backup_lock_or_mutation(
    tmp_path: Path,
    store: str,
    table: str,
) -> None:
    path = tmp_path / f"{store}-{table}.sqlite3"
    _create_database(path, store, legacy=True)
    connection = sqlite3.connect(path)
    _insert_child(connection, table, "missing-parent")
    connection.commit()
    connection.close()

    plan = migrations.plan_sqlite_foreign_key_migration(store=store, path=path)
    relation = next(
        item
        for item in integrity.REQUIRED_FOREIGN_KEYS[store]
        if item.child_table == table
    )
    assert dict(plan.orphan_counts)[relation.relationship_id] == 1
    assert plan.total_orphan_count == 1
    assert plan.eligible is False
    before = _tree_snapshot(tmp_path)
    with pytest.raises(migrations.SqliteForeignKeyOrphansDetected):
        migrations.apply_sqlite_foreign_key_migration(
            store=store,
            path=path,
            expected_plan_digest=plan.plan_digest,
        )
    assert _tree_snapshot(tmp_path) == before
    connection = sqlite3.connect(path)
    try:
        assert _row_count(connection, table) == 1
    finally:
        connection.close()
    assert not (path.parent / ".sqlite-fk-backups").exists()
    assert not migrations._migration_lock_path(path).exists()


def test_stale_plan_is_rejected_before_backup_lock_or_schema_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO index_state (key, value) VALUES ('stale', 'changed')"
    )
    connection.commit()
    connection.close()
    before_apply = _tree_snapshot(tmp_path)

    with pytest.raises(migrations.SqliteForeignKeyPlanStale):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_METADATA,
            path=path,
            expected_plan_digest=plan.plan_digest,
        )
    assert _tree_snapshot(tmp_path) == before_apply
    assert not (path.parent / ".sqlite-fk-backups").exists()
    assert not migrations._migration_lock_path(path).exists()


@pytest.mark.parametrize("phase", sorted(migrations.FAILURE_PHASES))
def test_every_migration_failure_phase_rolls_back_all_schema_data_and_objects(
    tmp_path: Path,
    phase: str,
) -> None:
    path = tmp_path / f"rollback-{phase}.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA, add_schema_objects=True)
    tables = (
        "run_topics",
        "papers",
        "evidence",
        "report_sections",
        "migration_trigger_log",
    )
    before_schema = _schema_snapshot(path)
    before_content = _content_snapshot(path, tables)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )

    with pytest.raises(migrations.SqliteMigrationError):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_METADATA,
            path=path,
            expected_plan_digest=plan.plan_digest,
            fail_at=phase,
        )

    assert _schema_snapshot(path) == before_schema
    assert _content_snapshot(path, tables) == before_content
    assert not any(
        "__xrml_fk_v1_" in row[1]
        for row in _schema_snapshot(path)
    )
    connection = integrity.open_sqlite_read_only(
        path,
        store=integrity.STORE_METADATA,
    )
    try:
        inventory = integrity.inspect_required_foreign_keys(
            connection,
            store=integrity.STORE_METADATA,
        )
        assert inventory.status == integrity.STATUS_MIGRATION_REQUIRED
        assert inventory.declared_count == 0
    finally:
        connection.close()
    backups = list((path.parent / ".sqlite-fk-backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert not Path(f"{path}-journal").exists()
    assert not Path(f"{path}-wal").exists()
    with file_lock(migrations._migration_lock_path(path), timeout_seconds=0.1):
        pass


def test_migration_lock_is_bounded_and_stale_marker_does_not_own_the_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "locked.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    lock_path = migrations._migration_lock_path(path)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with file_lock(lock_path, timeout_seconds=1.0):
            acquired.set()
            assert release.wait(timeout=5.0)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=2.0)
    try:
        with pytest.raises(
            migrations.SqliteForeignKeyMigrationBusy
        ) as blocked:
            migrations.apply_sqlite_foreign_key_migration(
                store=integrity.STORE_METADATA,
                path=path,
                expected_plan_digest=plan.plan_digest,
                timeout_seconds=0.05,
            )
        assert blocked.value.phase == migrations.BUSY_PHASE_MIGRATION_LOCK
        assert blocked.value.retryable is True
        assert not (path.parent / ".sqlite-fk-backups").exists()
    finally:
        release.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()

    marker_before = (
        lock_path.stat().st_size,
        lock_path.stat().st_mtime_ns,
        _sha256(lock_path),
    )
    result = migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
        expected_plan_digest=plan.plan_digest,
    )
    assert result.applied is True
    assert lock_path.exists()
    assert (
        lock_path.stat().st_size,
        lock_path.stat().st_mtime_ns,
        _sha256(lock_path),
    ) == marker_before


def test_active_reader_blocks_exclusive_migration_before_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active-reader.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM runs").fetchone()
    try:
        with pytest.raises(
            migrations.SqliteForeignKeyMigrationBusy
        ) as blocked:
            migrations.apply_sqlite_foreign_key_migration(
                store=integrity.STORE_METADATA,
                path=path,
                expected_plan_digest=plan.plan_digest,
                timeout_seconds=0.05,
            )
        assert (
            blocked.value.phase
            == migrations.BUSY_PHASE_EXCLUSIVE_TRANSACTION
        )
        assert blocked.value.retryable is True
        assert not (path.parent / ".sqlite-fk-backups").exists()
    finally:
        reader.rollback()
        reader.close()

    result = migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
        expected_plan_digest=plan.plan_digest,
    )
    assert result.applied is True


def test_two_concurrent_apply_attempts_produce_one_winner_and_one_safe_loser(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    barrier = threading.Barrier(2)

    def attempt() -> str:
        barrier.wait(timeout=3.0)
        try:
            result = migrations.apply_sqlite_foreign_key_migration(
                store=integrity.STORE_METADATA,
                path=path,
                expected_plan_digest=plan.plan_digest,
                timeout_seconds=3.0,
            )
            return result.status
        except integrity.SqliteIntegrityError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(attempt), pool.submit(attempt))
        outcomes = sorted(future.result() for future in futures)
    assert outcomes.count("migration_applied") == 1
    assert outcomes.count(migrations.CODE_PLAN_STALE) == 1
    assert len(list((path.parent / ".sqlite-fk-backups").glob("*.sqlite3"))) == 1


def test_read_only_audit_covers_all_statuses_and_never_writes(
    tmp_path: Path,
) -> None:
    paths: dict[str, Path] = {}

    ready = tmp_path / "ready.sqlite3"
    _create_database(ready, integrity.STORE_METADATA, legacy=False)
    paths[integrity.STATUS_READY] = ready

    legacy = tmp_path / "legacy.sqlite3"
    _populate_legacy(legacy, integrity.STORE_METADATA)
    paths[integrity.STATUS_MIGRATION_REQUIRED] = legacy

    orphan = tmp_path / "orphan.sqlite3"
    _create_database(orphan, integrity.STORE_METADATA, legacy=True)
    connection = sqlite3.connect(orphan)
    _insert_child(connection, "papers", "missing-parent")
    connection.commit()
    connection.close()
    paths[migrations.STATUS_ORPHANS_DETECTED] = orphan

    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"malformed")
    paths[migrations.STATUS_MALFORMED] = malformed

    uninitialized = tmp_path / "uninitialized.sqlite3"
    sqlite3.connect(uninitialized).close()
    paths[integrity.STATUS_UNINITIALIZED] = uninitialized

    mismatch = tmp_path / "mismatch.sqlite3"
    connection = sqlite3.connect(mismatch)
    connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    connection.close()
    paths[integrity.STATUS_SCHEMA_MISMATCH] = mismatch

    busy = tmp_path / "busy.sqlite3"
    _create_database(busy, integrity.STORE_METADATA, legacy=True)
    Path(f"{busy}-journal").write_bytes(b"active sidecar sentinel")
    paths[migrations.STATUS_BUSY] = busy

    for expected_status, path in paths.items():
        before = _tree_snapshot(tmp_path)
        first = migrations.audit_sqlite_integrity(
            store=integrity.STORE_METADATA,
            path=path,
        )
        second = migrations.audit_sqlite_integrity(
            store=integrity.STORE_METADATA,
            path=path,
        )
        assert first.status == expected_status
        assert first.to_payload() == second.to_payload()
        assert first.read_only is True
        assert str(tmp_path) not in json.dumps(first.to_payload(), sort_keys=True)
        assert _tree_snapshot(tmp_path) == before

    missing = tmp_path / "absent.sqlite3"
    missing_audit = migrations.audit_sqlite_integrity(
        store=integrity.STORE_METADATA,
        path=missing,
    )
    assert missing_audit.status == migrations.STATUS_MISSING
    assert missing_audit.complete is False
    assert not missing.exists()


def test_snapshot_audit_does_not_contend_with_an_idle_exclusive_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "writer-busy.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA)
    writer = sqlite3.connect(path)
    writer.execute("BEGIN EXCLUSIVE")
    before = _tree_snapshot(tmp_path)
    try:
        result = migrations.audit_sqlite_integrity(
            store=integrity.STORE_METADATA,
            path=path,
            timeout_seconds=0.05,
        )
        assert result.status == integrity.STATUS_MIGRATION_REQUIRED
        assert result.complete is True
        assert result.source_observation_status == "none"
        assert writer.in_transaction is True
        assert _tree_snapshot(tmp_path) == before
    finally:
        writer.rollback()
        writer.close()


def test_readiness_reports_migration_required_without_running_full_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    _create_database(metadata_path, integrity.STORE_METADATA, legacy=True)
    _create_database(scheduler_path, integrity.STORE_SCHEDULER, legacy=True)
    before = _tree_snapshot(tmp_path)

    monkeypatch.setattr(
        migrations,
        "audit_sqlite_integrity",
        lambda **_kwargs: pytest.fail("readiness must not run full audit"),
    )
    monkeypatch.setattr(
        migrations,
        "plan_sqlite_foreign_key_migration",
        lambda **_kwargs: pytest.fail("readiness must not build a migration plan"),
    )
    result = runtime_stores.inspect_runtime_stores()
    assert result["ready"] is False
    assert result["stores"]["metadata_index"]["status"] == "migration_required"
    assert result["stores"]["scheduler"]["status"] == "migration_required"
    assert result["stores"]["run_executor"]["status"] == "migration_required"
    assert all(
        item["migration_required"]
        for item in result["stores"].values()
    )
    assert _tree_snapshot(tmp_path) == before


class _FakeWorker:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    def start(self) -> None:
        self.events.append(f"{self.name}:start")

    def stop(self) -> None:
        self.events.append(f"{self.name}:stop")

    def snapshot(self) -> dict[str, object]:
        return {"enabled": True, "running": False, "mode": "test"}


def test_legacy_startup_starts_neither_worker_and_exposes_redacted_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    _create_database(metadata_path, integrity.STORE_METADATA, legacy=True)
    _create_database(scheduler_path, integrity.STORE_SCHEDULER, legacy=True)
    events: list[str] = []
    monkeypatch.setattr(
        main_module,
        "build_topic_schedule_worker",
        lambda: _FakeWorker(events, "scheduler"),
    )
    monkeypatch.setattr(
        main_module,
        "build_run_executor_worker",
        lambda: _FakeWorker(events, "executor"),
    )
    application = main_module.create_app()

    @application.get("/_test/sqlite-migration-required")
    def raise_sqlite_error():
        raise integrity.SqliteForeignKeyMigrationRequired(
            integrity.STORE_METADATA
        )

    with TestClient(application) as client:
        response = client.get("/_test/sqlite-migration-required")
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": integrity.CODE_MIGRATION_REQUIRED,
                "store": integrity.STORE_METADATA,
            }
        }
    assert "scheduler:start" not in events
    assert "executor:start" not in events


def test_scheduler_upsert_updates_parent_without_cascading_existing_queue_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.topic_schedules import DbSchedulerBackend

    _metadata_path, scheduler_path = _patch_store_paths(monkeypatch, tmp_path)
    connection = bootstrap.connect_scheduler_db()
    backend = DbSchedulerBackend()
    schedule = {
        "schedule_id": "schedule",
        "profile_id": "profile",
        "task_type": "research",
        "enabled": True,
        "deleted": False,
        "scheduler_state": "scheduled",
        "dispatch_attempts": 0,
        "updated_at": "2026-07-27T00:00:00Z",
    }
    try:
        connection.execute("BEGIN")
        backend._upsert_schedule(connection, schedule)
        _insert_child(
            connection,
            "scheduler_queue_items",
            "schedule",
            suffix="preserved",
        )
        connection.execute("COMMIT")

        updated = {
            **schedule,
            "enabled": False,
            "scheduler_state": "paused",
            "updated_at": "2026-07-27T00:01:00Z",
        }
        connection.execute("BEGIN")
        backend._upsert_schedule(connection, updated)
        connection.execute("COMMIT")
        assert _row_count(connection, "scheduler_queue_items") == 1
        row = connection.execute(
            "SELECT enabled, scheduler_state FROM scheduler_schedules "
            "WHERE schedule_id = 'schedule'"
        ).fetchone()
        assert tuple(row) == (0, "paused")
    finally:
        connection.close()
    assert scheduler_path.exists()


def test_cli_defaults_to_no_write_plan_and_requires_exact_digest_for_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import sqlite_integrity as command

    path = tmp_path / "operator.sqlite3"
    _populate_legacy(path, integrity.STORE_METADATA)
    monkeypatch.setattr(command, "_store_path", lambda _store: path)
    before = _tree_snapshot(tmp_path)
    assert command.main(["--store", "metadata"]) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["operation"] == "plan"
    assert plan_payload["eligible"] is True
    assert _tree_snapshot(tmp_path) == before

    assert command.main(
        ["--store", "metadata", "--apply", "--expected-plan-digest", "bad"]
    ) == 2
    blocked = capsys.readouterr().out
    assert "sqlite_expected_plan_digest_required" in blocked
    assert str(tmp_path) not in blocked
    assert _tree_snapshot(tmp_path) == before

    assert command.main(
        [
            "--store",
            "metadata",
            "--apply",
            "--expected-plan-digest",
            plan_payload["plan_digest"],
        ]
    ) == 0
    applied = capsys.readouterr().out
    assert '"status": "migration_applied"' in applied
    assert str(tmp_path) not in applied


def test_production_source_contains_no_fk_off_direct_bypass_or_public_migration_route() -> None:
    app_root = Path("app")
    direct_connects: list[tuple[str, int]] = []
    for path in sorted(app_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lowered = re.sub(r"\s+", "", source.lower())
        assert "pragmaforeign_keys=off" not in lowered
        assert "pragmaforeign_keys=0" not in lowered
        assert "defer_foreign_keys" not in lowered
        assert "writable_schema" not in lowered
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
                and node.func.attr == "connect"
            ):
                direct_connects.append((path.as_posix(), node.lineno))
    assert {path for path, _line in direct_connects} == {
        "app/db/sqlite_integrity.py",
        "app/db/sqlite_snapshot.py",
    }
    assert len(direct_connects) == 3

    readiness_source = Path("app/ops/runtime_stores.py").read_text(
        encoding="utf-8"
    )
    assert "quick_check" not in readiness_source
    assert "foreign_key_check" not in readiness_source
    assert "_orphan_count" not in readiness_source
    api_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(Path("app/api").rglob("*.py"))
    )
    assert "sqlite_migrations" not in api_source
    assert "apply_sqlite_foreign_key_migration" not in api_source

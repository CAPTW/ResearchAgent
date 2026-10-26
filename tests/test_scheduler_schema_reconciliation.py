from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import pytest

from app.db import bootstrap
from app.db import sqlite_integrity as integrity
from app.db import sqlite_migrations as migrations
from app.repositories.file_locks import file_lock
from app.repositories import run_jobs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v2_legacy_schema() -> str:
    scheduler_only = bootstrap.SCHEDULER_SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS run_jobs",
        1,
    )[0]
    legacy, removed = re.subn(
        r",\s*FOREIGN KEY \([^)]*\) REFERENCES [^(]+\([^)]*\)"
        r"\s*ON DELETE CASCADE\s*ON UPDATE NO ACTION",
        "",
        scheduler_only,
        flags=re.IGNORECASE,
    )
    assert removed == 1
    return legacy


def _v1_legacy_schema() -> str:
    legacy, removed = re.subn(
        r",\s*FOREIGN KEY \([^)]*\) REFERENCES [^(]+\([^)]*\)"
        r"\s*ON DELETE CASCADE\s*ON UPDATE NO ACTION",
        "",
        bootstrap.SCHEDULER_SCHEMA_SQL,
        flags=re.IGNORECASE,
    )
    assert removed == 1
    return legacy


def _create_v2_legacy(
    path: Path,
    *,
    schedules: int = 3,
    heartbeats: int = 4,
    queue_items: int = 0,
    schema: str | None = None,
    orphan: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema or _v2_legacy_schema())
        connection.executemany(
            """
            INSERT INTO scheduler_schedules (
              schedule_id, profile_id, task_type, enabled, deleted,
              scheduler_state, dispatch_attempts, updated_at, payload_json
            ) VALUES (?, ?, 'research', 0, 0, 'paused', 0,
                      '2026-07-28T00:00:00Z', '{}')
            """,
            (
                (f"synthetic-schedule-{index}", f"synthetic-profile-{index}")
                for index in range(schedules)
            ),
        )
        connection.executemany(
            """
            INSERT INTO scheduler_worker_heartbeats (
              worker_id, mode, backend_mode, enabled, running,
              heartbeat_at, last_cycle_summary_json
            ) VALUES (?, 'synthetic', 'db', 0, 0,
                      '2026-07-28T00:00:00Z', '{}')
            """,
            (
                (f"synthetic-worker-{index}",)
                for index in range(heartbeats)
            ),
        )
        for index in range(queue_items):
            schedule_id = (
                "synthetic-missing-schedule"
                if orphan and index == 0
                else f"synthetic-schedule-{index % schedules}"
            )
            connection.execute(
                """
                INSERT INTO scheduler_queue_items (
                  queue_item_id, schedule_id, due_at, status,
                  dispatch_attempts, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, 'queued', 0,
                          '2026-07-28T00:00:00Z',
                          '2026-07-28T00:00:00Z', '{}')
                """,
                (
                    f"synthetic-queue-{index}",
                    schedule_id,
                    f"2026-07-28T00:{index:02d}:00Z",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _create_v1_legacy(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_v1_legacy_schema())
    finally:
        connection.close()


def _create_fresh(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(bootstrap.SCHEDULER_SCHEMA_SQL)
    finally:
        connection.close()


def _schema_snapshot(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            tuple(str(value) for value in row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') "
                "FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        )
    finally:
        connection.close()


def _content_snapshot(path: Path) -> dict[str, tuple[int, str]]:
    connection = sqlite3.connect(path)
    try:
        return migrations._scheduler_v2_content_snapshot(connection)
    finally:
        connection.close()


def _backup_paths(path: Path) -> list[Path]:
    return sorted(
        (path.parent / ".sqlite-fk-backups").glob("*.sqlite3")
    )


def test_v2_plan_is_exact_deterministic_sensitive_and_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scheduler.sqlite3"
    _create_v2_legacy(path, schedules=120, heartbeats=908)

    plans = [
        migrations.plan_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
        )
        for _ in range(5)
    ]
    first = plans[0]
    assert all(plan.to_payload() == first.to_payload() for plan in plans)
    assert first.status == integrity.STATUS_MIGRATION_REQUIRED
    assert (
        first.migration_id
        == migrations.SCHEDULER_SCHEMA_RECONCILIATION_ID
    )
    assert (
        first.schema_classification
        == migrations.SCHEMA_CLASSIFICATION_SCHEDULER_V2
    )
    assert first.current_table_count == 3
    assert first.expected_table_count == 4
    assert first.current_declared_fk_count == 0
    assert first.target_fk_count == 1
    assert first.missing_additive_objects == (
        "run_jobs_table",
        "run_jobs_named_indexes",
    )
    assert first.queue_rebuild_required is True
    assert first.total_orphan_count == 0
    assert first.quick_check_ok is True
    assert first.foreign_key_violation_count == 0
    assert first.schema_compatible is True
    assert first.eligible is True
    assert first.backup_required is True
    assert (
        first.expected_schema_fingerprint
        == "0419ec753b1cc1c16d91414069c0d27356f90d4fe936e541613eedb4e8bbf0d1"
    )

    encoded = json.dumps(first.to_payload(), sort_keys=True)
    assert str(tmp_path) not in encoded
    assert "SELECT " not in encoded
    assert "synthetic-schedule" not in encoded
    assert "synthetic-worker" not in encoded
    assert "synthetic-queue" not in encoded

    stat = path.stat()
    os.utime(
        path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
    )
    mtime_only = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert mtime_only.plan_digest == first.plan_digest

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE scheduler_schedules SET payload_json = '{\"changed\":1}' "
            "WHERE schedule_id = 'synthetic-schedule-0'"
        )
        connection.commit()
    finally:
        connection.close()
    row_changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert row_changed.plan_digest != first.plan_digest

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE INDEX idx_synthetic_plan_change "
            "ON scheduler_schedules(updated_at)"
        )
    finally:
        connection.close()
    schema_changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert schema_changed.plan_digest != row_changed.plan_digest
    assert schema_changed.status == integrity.STATUS_SCHEMA_MISMATCH


def test_v2_stress_apply_preserves_content_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scheduler.sqlite3"
    _create_v2_legacy(path, schedules=120, heartbeats=908)
    before_content = _content_snapshot(path)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )

    result = migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
        expected_plan_digest=plan.plan_digest,
    )
    assert result.status == "migration_applied"
    assert result.applied is True
    assert result.migration_id == migrations.SCHEDULER_SCHEMA_RECONCILIATION_ID
    assert result.rebuilt_table_count == 1
    assert result.backup_id
    backups = _backup_paths(path)
    assert len(backups) == 1
    assert _sha256(backups[0]) == result.backup_sha256

    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        backup.close()

    ready = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert ready.status == integrity.STATUS_READY
    assert (
        ready.schema_classification
        == migrations.SCHEMA_CLASSIFICATION_SCHEDULER_READY
    )
    assert (
        ready.structural_schema_fingerprint
        == ready.expected_schema_fingerprint
        == "0419ec753b1cc1c16d91414069c0d27356f90d4fe936e541613eedb4e8bbf0d1"
    )
    assert _content_snapshot(path) == before_content

    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM run_jobs"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("PRAGMA quick_check").fetchone()[0]
            == "ok"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        index_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {
        "idx_run_jobs_active_run",
        "idx_run_jobs_claim",
        "idx_run_jobs_lease",
    } <= index_names

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    job = run_jobs.enqueue_run_job("synthetic-run")
    duplicate = run_jobs.enqueue_run_job("synthetic-run")
    assert duplicate.job_id == job.job_id
    claimed = run_jobs.claim_next_job(
        owner="synthetic-worker",
        lease_seconds=60,
    )
    assert claimed is not None
    assert run_jobs.heartbeat(
        claimed.job_id,
        claimed.lease_token,
        lease_seconds=60,
    )
    assert run_jobs.complete_job(
        claimed.job_id,
        claimed.lease_token,
        state=run_jobs.STATE_SUCCEEDED,
    )

    before_second_hash = _sha256(path)
    backups_before = tuple(_backup_paths(path))
    current = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    second = migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
        expected_plan_digest=current.plan_digest,
    )
    assert second.status == "already_compliant"
    assert second.applied is False
    assert second.rebuilt_table_count == 0
    assert second.backup_id is None
    assert tuple(_backup_paths(path)) == backups_before
    assert _sha256(path) == before_second_hash


def test_v2_nonempty_queue_preserves_rows_fk_cascade_and_unique(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scheduler.sqlite3"
    _create_v2_legacy(
        path,
        schedules=3,
        heartbeats=4,
        queue_items=3,
    )
    before = _content_snapshot(path)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
        expected_plan_digest=plan.plan_digest,
    )
    assert _content_snapshot(path) == before

    connection = integrity.open_sqlite_read_write(
        path,
        store=integrity.STORE_SCHEDULER,
        timeout_seconds=1.0,
    )
    try:
        assert (
            len(
                connection.execute(
                    "PRAGMA foreign_key_list(scheduler_queue_items)"
                ).fetchall()
            )
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO scheduler_queue_items (
                  queue_item_id, schedule_id, due_at, status,
                  dispatch_attempts, created_at, updated_at, payload_json
                ) VALUES ('duplicate', 'synthetic-schedule-1',
                          '2026-07-28T00:01:00Z', 'queued', 0,
                          '2026-07-28T00:00:00Z',
                          '2026-07-28T00:00:00Z', '{}')
                """
            )
        connection.rollback()
        connection.execute(
            "DELETE FROM scheduler_schedules "
            "WHERE schedule_id = 'synthetic-schedule-0'"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM scheduler_queue_items "
                "WHERE schedule_id = 'synthetic-schedule-0'"
            ).fetchone()[0]
            == 0
        )
        connection.rollback()
    finally:
        connection.close()


def test_ready_v1_v2_and_readiness_classifications_remain_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh" / "scheduler.sqlite3"
    v1 = tmp_path / "v1" / "scheduler.sqlite3"
    v2 = tmp_path / "v2" / "scheduler.sqlite3"
    fresh.parent.mkdir()
    v1.parent.mkdir()
    v2.parent.mkdir()
    _create_fresh(fresh)
    _create_v1_legacy(v1)
    _create_v2_legacy(v2)

    ready_plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=fresh,
    )
    assert ready_plan.status == integrity.STATUS_READY
    assert (
        ready_plan.schema_classification
        == migrations.SCHEMA_CLASSIFICATION_SCHEDULER_READY
    )
    assert (
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=fresh,
            expected_plan_digest=ready_plan.plan_digest,
        ).status
        == "already_compliant"
    )
    assert _backup_paths(fresh) == []

    v1_plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=v1,
    )
    assert v1_plan.migration_id == "scheduler_required_foreign_keys_v1"
    assert (
        v1_plan.schema_classification
        == migrations.SCHEMA_CLASSIFICATION_SCHEDULER_V1
    )
    assert (
        v1_plan.plan_digest
        == "b59b7b409d5456ec62a56ea45cd6afb13d2ea541935490f784fafa24f5d65b3f"
    )

    v2_read = integrity.open_sqlite_read_only(
        v2,
        store=integrity.STORE_SCHEDULER,
    )
    try:
        inventory = integrity.inspect_required_foreign_keys(
            v2_read,
            store=integrity.STORE_SCHEDULER,
        )
    finally:
        v2_read.close()
    assert inventory.status == integrity.STATUS_MIGRATION_REQUIRED
    assert inventory.missing_tables == ("run_jobs",)

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", v2.parent / "runs")
    with pytest.raises(integrity.SqliteForeignKeyMigrationRequired):
        bootstrap.connect_scheduler_db()
    connection = sqlite3.connect(v2)
    try:
        assert (
            connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_jobs'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def _add_partial_run_jobs(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            migrations._authoritative_scheduler_statements()["run_jobs"]
        )
    finally:
        connection.close()


def _add_wrong_run_jobs_column(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        statement = migrations._authoritative_scheduler_statements()[
            "run_jobs"
        ].replace("handler TEXT NOT NULL", "handler_name TEXT NOT NULL")
        connection.execute(statement)
    finally:
        connection.close()


def _add_partial_run_jobs_indexes(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        statements = migrations._authoritative_scheduler_statements()
        connection.execute(statements["run_jobs"])
        connection.execute(statements["idx_run_jobs_active_run"])
    finally:
        connection.close()


def _add_unexpected_trigger(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TRIGGER unexpected_scheduler_trigger
            AFTER INSERT ON scheduler_queue_items
            BEGIN
              SELECT NEW.queue_item_id;
            END
            """
        )
    finally:
        connection.close()


def _drop_parent_table(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE scheduler_schedules")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutate",
    (
        _add_partial_run_jobs,
        _add_wrong_run_jobs_column,
        _add_partial_run_jobs_indexes,
        _add_unexpected_trigger,
        _drop_parent_table,
    ),
)
def test_unsupported_partial_or_conflicting_shapes_fail_before_backup(
    tmp_path: Path,
    mutate: Callable[[Path], None],
) -> None:
    path = tmp_path / f"{mutate.__name__}.sqlite3"
    _create_v2_legacy(path)
    mutate(path)
    before = _sha256(path)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert plan.status == integrity.STATUS_SCHEMA_MISMATCH
    assert plan.schema_compatible is False
    assert plan.eligible is False
    with pytest.raises(integrity.SqliteSchemaMismatch):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
            expected_plan_digest=plan.plan_digest,
        )
    assert _sha256(path) == before
    assert _backup_paths(path) == []
    assert not migrations._migration_lock_path(path).exists()


@pytest.mark.parametrize(
    "schema",
    (
        lambda: _v2_legacy_schema().replace(
            "error_message TEXT,",
            "error_detail TEXT,",
        ),
        lambda: bootstrap.SCHEDULER_SCHEMA_SQL.split(
            "CREATE TABLE IF NOT EXISTS run_jobs",
            1,
        )[0],
        lambda: bootstrap.SCHEDULER_SCHEMA_SQL.split(
            "CREATE TABLE IF NOT EXISTS run_jobs",
            1,
        )[0].replace(
            "ON DELETE CASCADE",
            "ON DELETE RESTRICT",
        ),
    ),
    ids=(
        "queue-column-mismatch",
        "queue-fk-present-run-jobs-absent",
        "conflicting-queue-fk",
    ),
)
def test_unsupported_queue_shapes_fail_closed(
    tmp_path: Path,
    schema: Callable[[], str],
) -> None:
    path = tmp_path / "unsupported.sqlite3"
    _create_v2_legacy(path, schema=schema())
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert plan.status == integrity.STATUS_SCHEMA_MISMATCH
    assert plan.migration_id != migrations.SCHEDULER_SCHEMA_RECONCILIATION_ID
    assert plan.eligible is False
    assert _backup_paths(path) == []


def test_v2_orphan_is_blocked_before_backup_lock_or_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphan.sqlite3"
    _create_v2_legacy(
        path,
        schedules=1,
        queue_items=1,
        orphan=True,
    )
    before_schema = _schema_snapshot(path)
    before_content = _content_snapshot(path)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert plan.status == migrations.STATUS_ORPHANS_DETECTED
    assert plan.total_orphan_count == 1
    assert plan.eligible is False
    with pytest.raises(migrations.SqliteForeignKeyOrphansDetected):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
            expected_plan_digest=plan.plan_digest,
        )
    assert _schema_snapshot(path) == before_schema
    assert _content_snapshot(path) == before_content
    assert _backup_paths(path) == []
    assert not migrations._migration_lock_path(path).exists()


def test_v2_stable_zero_sidecars_are_diagnostic_and_apply_is_blocked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zero-wal.sqlite3"
    _create_v2_legacy(path)
    clean = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"\0" * 32_768)

    blocked_plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert (
        blocked_plan.migration_id
        == migrations.SCHEDULER_SCHEMA_RECONCILIATION_ID
    )
    assert blocked_plan.plan_digest == clean.plan_digest
    assert blocked_plan.eligible is False
    assert (
        blocked_plan.source_observation_status
        == "stable_zero_wal_sidecars"
    )
    with pytest.raises(
        migrations.SqliteForeignKeyMigrationBusy
    ) as blocked_error:
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
            expected_plan_digest=blocked_plan.plan_digest,
        )
    assert (
        blocked_error.value.phase
        == migrations.BUSY_PHASE_SOURCE_REVALIDATION
    )
    assert wal.stat().st_size == 0
    assert shm.stat().st_size == 32_768
    assert _backup_paths(path) == []


@pytest.mark.parametrize(
    "phase",
    sorted(migrations.SCHEDULER_V2_FAILURE_PHASES),
)
def test_every_v2_failure_phase_rolls_back_schema_and_content(
    tmp_path: Path,
    phase: str,
) -> None:
    path = tmp_path / f"rollback-{phase}.sqlite3"
    _create_v2_legacy(
        path,
        schedules=4,
        heartbeats=5,
        queue_items=3,
    )
    before_schema = _schema_snapshot(path)
    before_content = _content_snapshot(path)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )

    with pytest.raises(
        migrations.SqliteMigrationError,
        match=r"^sqlite_fk_migration_failed: scheduler$",
    ):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
            expected_plan_digest=plan.plan_digest,
            fail_at=phase,
        )

    assert _schema_snapshot(path) == before_schema
    assert _content_snapshot(path) == before_content
    assert len(_backup_paths(path)) == 1
    with file_lock(
        migrations._migration_lock_path(path),
        timeout_seconds=0.1,
    ):
        pass
    assert not Path(f"{path}-journal").exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    assert not any(
        "__xrml_schema_v2_" in row[1]
        for row in _schema_snapshot(path)
    )
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'run_jobs'"
            ).fetchone()[0]
            == 0
        )
        assert (
            len(
                connection.execute(
                    "PRAGMA foreign_key_list(scheduler_queue_items)"
                ).fetchall()
            )
            == 0
        )
    finally:
        connection.close()


def test_two_v2_apply_attempts_have_one_winner_and_no_partial_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    _create_v2_legacy(
        path,
        schedules=4,
        heartbeats=5,
        queue_items=3,
    )
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )

    def apply_once() -> str:
        try:
            result = migrations.apply_sqlite_foreign_key_migration(
                store=integrity.STORE_SCHEDULER,
                path=path,
                expected_plan_digest=plan.plan_digest,
                timeout_seconds=2.0,
            )
            return result.status
        except (
            migrations.SqliteForeignKeyMigrationBusy,
            migrations.SqliteForeignKeyPlanStale,
        ) as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _index: apply_once(), range(2)))

    assert outcomes.count("migration_applied") == 1
    assert len(outcomes) == 2
    assert (
        outcomes[0] in {
            "migration_applied",
            migrations.CODE_MIGRATION_BUSY,
            migrations.CODE_PLAN_STALE,
        }
        and outcomes[1]
        in {
            "migration_applied",
            migrations.CODE_MIGRATION_BUSY,
            migrations.CODE_PLAN_STALE,
        }
    )
    final = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert final.status == integrity.STATUS_READY
    assert (
        final.structural_schema_fingerprint
        == final.expected_schema_fingerprint
    )
    assert len(_backup_paths(path)) == 1
    with file_lock(
        migrations._migration_lock_path(path),
        timeout_seconds=0.1,
    ):
        pass
    assert not any(
        "__xrml_schema_v2_" in row[1]
        for row in _schema_snapshot(path)
    )

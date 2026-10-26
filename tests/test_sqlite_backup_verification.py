from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from app.db import bootstrap
from app.db import sqlite_integrity as integrity
from app.db import sqlite_migrations as migrations
from app.db import sqlite_snapshot as snapshots
from app.repositories.file_locks import file_lock


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(path: Path) -> dict[str, bytes]:
    return {
        suffix: sidecar.read_bytes()
        for suffix in ("-journal", "-wal", "-shm")
        if (sidecar := Path(f"{path}{suffix}")).exists()
    }


def _strip_foreign_keys(schema: str, expected: int) -> str:
    legacy, count = re.subn(
        r",\s*FOREIGN KEY \([^)]*\) REFERENCES [^(]+\([^)]*\)"
        r"\s*ON DELETE CASCADE\s*ON UPDATE NO ACTION",
        "",
        schema,
        flags=re.IGNORECASE,
    )
    assert count == expected
    return legacy


def _create_legacy_store(
    path: Path,
    *,
    store: str,
    scheduler_v2: bool = False,
    wal_mode: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if store == integrity.STORE_METADATA:
        schema = _strip_foreign_keys(bootstrap.SCHEMA_SQL, 4)
    elif scheduler_v2:
        scheduler_only = bootstrap.SCHEDULER_SCHEMA_SQL.split(
            "CREATE TABLE IF NOT EXISTS run_jobs",
            1,
        )[0]
        schema = _strip_foreign_keys(scheduler_only, 1)
    else:
        schema = _strip_foreign_keys(bootstrap.SCHEDULER_SCHEMA_SQL, 1)

    connection = sqlite3.connect(path)
    try:
        if wal_mode:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            assert mode is not None and str(mode[0]).lower() == "wal"
        connection.executescript(schema)
        if scheduler_v2:
            connection.executemany(
                """
                INSERT INTO scheduler_schedules (
                  schedule_id, profile_id, task_type, enabled, deleted,
                  scheduler_state, dispatch_attempts, updated_at, payload_json
                ) VALUES (?, ?, 'research', 0, 0, 'paused', 0,
                          '2026-07-28T00:00:00Z', '{}')
                """,
                (
                    (f"synthetic-schedule-{index}", f"profile-{index}")
                    for index in range(120)
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
                    for index in range(908)
                ),
            )
        connection.commit()
        if wal_mode:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    assert _sidecars(path) == {}


def _create_ready_store(path: Path, *, store: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = (
        bootstrap.SCHEMA_SQL
        if store == integrity.STORE_METADATA
        else bootstrap.SCHEDULER_SCHEMA_SQL
    )
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
    finally:
        connection.close()


def _backup_paths(path: Path) -> list[Path]:
    return sorted(
        (path.parent / ".sqlite-fk-backups").glob("*.sqlite3")
    )


def _external_temp_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path.parent / f"{tmp_path.name}-{label}"
    root.mkdir()
    return root


@pytest.fixture
def isolated_backup_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    root = _external_temp_root(tmp_path, "backup-snapshots")
    real_capture = snapshots.capture_stable_sqlite_snapshot

    @contextmanager
    def capture(path: Path) -> Iterator[snapshots.SqliteAuditSnapshot]:
        with real_capture(
            path,
            stability_delay_seconds=0,
            temp_root=root,
        ) as captured:
            yield captured

    monkeypatch.setattr(
        migrations,
        "capture_stable_sqlite_snapshot",
        capture,
    )
    try:
        yield root
    finally:
        assert list(root.iterdir()) == []
        root.rmdir()


@pytest.mark.parametrize(
    ("store", "scheduler_v2", "expected_migration", "expected_fks"),
    (
        (
            integrity.STORE_METADATA,
            False,
            migrations.MIGRATION_IDS[integrity.STORE_METADATA],
            4,
        ),
        (
            integrity.STORE_SCHEDULER,
            False,
            migrations.MIGRATION_IDS[integrity.STORE_SCHEDULER],
            1,
        ),
        (
            integrity.STORE_SCHEDULER,
            True,
            migrations.SCHEDULER_SCHEMA_RECONCILIATION_ID,
            1,
        ),
    ),
)
def test_wal_mode_backup_is_opened_only_through_one_temp_immutable_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_backup_snapshots: Path,
    store: str,
    scheduler_v2: bool,
    expected_migration: str,
    expected_fks: int,
) -> None:
    path = tmp_path / f"{store}.sqlite3"
    _create_legacy_store(
        path,
        store=store,
        scheduler_v2=scheduler_v2,
    )
    real_connect = sqlite3.connect
    real_copy = snapshots._copy_main_database
    real_open = migrations.open_immutable_snapshot
    connect_targets: list[str] = []
    copies: list[tuple[Path, Path]] = []
    immutable_sources: list[Path] = []

    def connect(database, *args, **kwargs):
        connect_targets.append(str(database))
        return real_connect(database, *args, **kwargs)

    def copy(source: Path, destination: Path) -> int:
        copies.append((source.resolve(), destination.resolve()))
        return real_copy(source, destination)

    def open_immutable(snapshot, *, store, timeout_seconds):
        immutable_sources.append(snapshot.source_path.resolve())
        return real_open(
            snapshot,
            store=store,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect)
    monkeypatch.setattr(snapshots, "_copy_main_database", copy)
    monkeypatch.setattr(
        migrations,
        "open_immutable_snapshot",
        open_immutable,
    )

    plan = migrations.plan_sqlite_foreign_key_migration(
        store=store,
        path=path,
    )
    result = migrations.apply_sqlite_foreign_key_migration(
        store=store,
        path=path,
        expected_plan_digest=str(plan.plan_digest),
    )

    assert result.status == "migration_applied"
    assert result.migration_id == expected_migration
    backups = _backup_paths(path)
    assert len(backups) == 1
    backup = backups[0].resolve()
    assert _sidecars(backup) == {}
    backup_copies = [item for item in copies if item[0] == backup]
    backup_opens = [item for item in immutable_sources if item == backup]
    assert len(backup_copies) == 1
    assert len(backup_opens) == 1
    assert not any(
        ".sqlite-fk-backups" in target for target in connect_targets
    )
    assert backup_copies[0][0] != backup_copies[0][1]
    assert backup_copies[0][1].parent != backup.parent

    connection = real_connect(path)
    try:
        declared = sum(
            len(
                connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                ).fetchall()
            )
            for table in integrity.REQUIRED_TABLES[store]
        )
        assert declared == expected_fks
    finally:
        connection.close()

    inspection = tmp_path / f"{store}-backup-inspection.sqlite3"
    shutil.copyfile(backup, inspection)
    inspection_connection = real_connect(
        f"{inspection.resolve().as_uri()}?mode=ro&immutable=1&cache=private",
        uri=True,
    )
    try:
        assert (
            inspection_connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
            == "ok"
        )
    finally:
        inspection_connection.close()
    assert _sidecars(backup) == {}

    backup_copy_count = len(backup_copies)
    backup_open_count = len(backup_opens)
    ready = migrations.plan_sqlite_foreign_key_migration(
        store=store,
        path=path,
    )
    second = migrations.apply_sqlite_foreign_key_migration(
        store=store,
        path=path,
        expected_plan_digest=str(ready.plan_digest),
    )
    assert second.status == "already_compliant"
    assert _backup_paths(path) == backups
    assert len([item for item in copies if item[0] == backup]) == (
        backup_copy_count
    )
    assert len([item for item in immutable_sources if item == backup]) == (
        backup_open_count
    )
    assert list(isolated_backup_snapshots.iterdir()) == []


@pytest.mark.parametrize(
    "store",
    (integrity.STORE_METADATA, integrity.STORE_SCHEDULER),
)
def test_ready_store_never_invokes_backup_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
) -> None:
    path = tmp_path / f"ready-{store}.sqlite3"
    _create_ready_store(path, store=store)
    before = path.read_bytes()
    calls = 0

    def forbidden_verifier(**_kwargs) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("ready stores must not invoke backup verification")

    monkeypatch.setattr(
        migrations,
        "_verify_persistent_backup_from_temporary_snapshot",
        forbidden_verifier,
    )
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=store,
        path=path,
    )
    result = migrations.apply_sqlite_foreign_key_migration(
        store=store,
        path=path,
        expected_plan_digest=str(plan.plan_digest),
    )
    assert result.status == "already_compliant"
    assert calls == 0
    assert path.read_bytes() == before
    assert not (path.parent / ".sqlite-fk-backups").exists()


@pytest.mark.skipif(
    os.name != "nt",
    reason="the historical persistent-backup sidecars were observed on Windows",
)
def test_old_direct_backup_open_reproduces_sidecars_after_production_does_not(
    tmp_path: Path,
    isolated_backup_snapshots: Path,
) -> None:
    source = tmp_path / "wal-legacy.sqlite3"
    _create_legacy_store(
        source,
        store=integrity.STORE_METADATA,
    )
    backup_id, _backup_hash = migrations._create_verified_backup(
        store=integrity.STORE_METADATA,
        path=source,
        migration_id=migrations.MIGRATION_IDS[integrity.STORE_METADATA],
        serialized=source.read_bytes(),
    )
    backup = (
        source.parent
        / ".sqlite-fk-backups"
        / f"{backup_id}.sqlite3"
    )
    assert _sidecars(backup) == {}

    direct = integrity.open_sqlite_read_only(
        backup,
        store=integrity.STORE_METADATA,
    )
    try:
        assert direct.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        direct.close()

    sidecars = _sidecars(backup)
    assert sidecars["-wal"] == b""
    assert len(sidecars["-shm"]) == 32_768
    assert list(isolated_backup_snapshots.iterdir()) == []


def test_preexisting_persistent_backup_sidecars_fail_before_copy_or_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "foreign-backup.sqlite3"
    _create_ready_store(backup, store=integrity.STORE_METADATA)
    wal = Path(f"{backup}-wal")
    shm = Path(f"{backup}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"x" * 32_768)
    before = snapshots.capture_sqlite_source_fingerprint(backup)
    captures = 0
    opens = 0

    def forbidden_capture(*_args, **_kwargs):
        nonlocal captures
        captures += 1
        raise AssertionError("sidecar-present backup must not be copied")

    def forbidden_open(*_args, **_kwargs):
        nonlocal opens
        opens += 1
        raise AssertionError("sidecar-present backup must not be opened")

    monkeypatch.setattr(
        migrations,
        "capture_stable_sqlite_snapshot",
        forbidden_capture,
    )
    monkeypatch.setattr(
        migrations,
        "open_immutable_snapshot",
        forbidden_open,
    )
    with pytest.raises(migrations.SqliteForeignKeyBackupFailed):
        migrations._verify_persistent_backup_from_temporary_snapshot(
            store=integrity.STORE_METADATA,
            backup_path=backup,
            expected_size=backup.stat().st_size,
            expected_sha256=_sha256(backup),
        )
    assert captures == 0
    assert opens == 0
    assert snapshots.capture_sqlite_source_fingerprint(backup) == before


@pytest.mark.parametrize(
    "failure",
    (
        "immutable_open",
        "query_only",
        "foreign_keys",
        "quick_check",
        "close",
        "temp_sidecar",
    ),
)
def test_backup_verification_policy_failures_are_redacted_and_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_backup_snapshots: Path,
    failure: str,
) -> None:
    backup = tmp_path / f"policy-{failure}.sqlite3"
    _create_ready_store(backup, store=integrity.STORE_METADATA)
    before = snapshots.capture_sqlite_source_fingerprint(backup)
    real_open = migrations.open_immutable_snapshot

    def controlled_open(snapshot, *, store, timeout_seconds):
        if failure == "immutable_open":
            raise integrity.SqliteStoreUnavailable(store)
        connection = real_open(
            snapshot,
            store=store,
            timeout_seconds=timeout_seconds,
        )
        if failure == "query_only":
            connection.execute("PRAGMA query_only = OFF")
        elif failure == "foreign_keys":
            connection.execute("PRAGMA foreign_keys = OFF")
        elif failure == "temp_sidecar":
            Path(f"{snapshot.snapshot_path}-wal").write_bytes(b"")
        if failure != "close":
            return connection

        class CloseFailure:
            def execute(self, *args, **kwargs):
                return connection.execute(*args, **kwargs)

            def close(self) -> None:
                connection.close()
                raise sqlite3.OperationalError("synthetic close failure")

        return CloseFailure()

    monkeypatch.setattr(
        migrations,
        "open_immutable_snapshot",
        controlled_open,
    )
    if failure == "quick_check":
        monkeypatch.setattr(migrations, "_quick_check_ok", lambda _connection: False)

    with pytest.raises(
        migrations.SqliteForeignKeyBackupFailed
    ) as blocked:
        migrations._verify_persistent_backup_from_temporary_snapshot(
            store=integrity.STORE_METADATA,
            backup_path=backup,
            expected_size=before.main.size,
            expected_sha256=before.main.sha256,
        )
    assert str(blocked.value) == (
        "sqlite_fk_backup_failed: metadata"
    )
    assert snapshots.capture_sqlite_source_fingerprint(backup) == before
    assert _sidecars(backup) == {}
    assert list(isolated_backup_snapshots.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    (
        "copy",
        "fsync",
        "destination_hash",
        "source_drift",
        "cleanup",
        "final_fingerprint",
    ),
)
def test_backup_copy_and_stability_failures_leave_no_temp_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_backup_snapshots: Path,
    failure: str,
) -> None:
    backup = tmp_path / f"copy-{failure}.sqlite3"
    _create_ready_store(backup, store=integrity.STORE_METADATA)
    before = snapshots.capture_sqlite_source_fingerprint(backup)
    real_copy = snapshots._copy_main_database
    real_cleanup = snapshots._cleanup_snapshot_directory
    real_capture = migrations.capture_sqlite_source_fingerprint

    def controlled_copy(source: Path, destination: Path) -> int:
        if failure == "copy":
            raise snapshots.SqliteSnapshotCopyFailed()
        copied = real_copy(source, destination)
        if failure == "destination_hash":
            with destination.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"not-sqlite")
                handle.flush()
                os.fsync(handle.fileno())
        elif failure == "source_drift":
            with source.open("ab") as handle:
                handle.write(b"source-drift")
                handle.flush()
                os.fsync(handle.fileno())
        return copied

    def fsync_failure(_file_descriptor: int) -> None:
        raise OSError("synthetic fsync failure")

    def cleanup_failure(directory: Path) -> None:
        real_cleanup(directory)
        raise snapshots.SqliteSnapshotCleanupFailed()

    backup_captures = 0

    def capture_with_final_drift(path: Path):
        nonlocal backup_captures
        if path.resolve() == backup.resolve():
            backup_captures += 1
            if failure == "final_fingerprint" and backup_captures == 5:
                stat = path.stat()
                os.utime(
                    path,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
                )
        return real_capture(path)

    monkeypatch.setattr(snapshots, "_copy_main_database", controlled_copy)
    if failure == "fsync":
        monkeypatch.setattr(snapshots.os, "fsync", fsync_failure)
    if failure == "cleanup":
        monkeypatch.setattr(
            snapshots,
            "_cleanup_snapshot_directory",
            cleanup_failure,
        )
    if failure == "final_fingerprint":
        monkeypatch.setattr(
            migrations,
            "capture_sqlite_source_fingerprint",
            capture_with_final_drift,
        )
        monkeypatch.setattr(
            snapshots,
            "capture_sqlite_source_fingerprint",
            capture_with_final_drift,
        )

    with pytest.raises(
        migrations.SqliteForeignKeyBackupFailed
    ) as blocked:
        migrations._verify_persistent_backup_from_temporary_snapshot(
            store=integrity.STORE_METADATA,
            backup_path=backup,
            expected_size=before.main.size,
            expected_sha256=before.main.sha256,
        )
    assert str(blocked.value) == (
        "sqlite_fk_backup_failed: metadata"
    )
    assert backup.exists()
    assert _sidecars(backup) == {}
    if failure not in {"source_drift", "final_fingerprint"}:
        assert snapshots.capture_sqlite_source_fingerprint(backup) == before
    assert list(isolated_backup_snapshots.iterdir()) == []


def test_verifier_failure_rolls_back_before_first_ddl_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scheduler.sqlite3"
    _create_legacy_store(
        path,
        store=integrity.STORE_SCHEDULER,
        scheduler_v2=True,
        wal_mode=False,
    )
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    source_before = path.read_bytes()
    ddl_calls = 0

    def failed_verifier(**_kwargs) -> None:
        raise migrations.SqliteForeignKeyBackupFailed(
            integrity.STORE_SCHEDULER
        )

    def forbidden_ddl(*_args, **_kwargs) -> None:
        nonlocal ddl_calls
        ddl_calls += 1
        raise AssertionError("backup failure must precede DDL")

    monkeypatch.setattr(
        migrations,
        "_verify_persistent_backup_from_temporary_snapshot",
        failed_verifier,
    )
    monkeypatch.setattr(
        migrations,
        "_apply_scheduler_v2_schema",
        forbidden_ddl,
    )
    with pytest.raises(migrations.SqliteForeignKeyBackupFailed):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
            expected_plan_digest=str(plan.plan_digest),
        )
    assert ddl_calls == 0
    assert path.read_bytes() == source_before
    backups = _backup_paths(path)
    assert len(backups) == 1
    assert _sidecars(backups[0]) == {}
    with file_lock(
        migrations._migration_lock_path(path),
        timeout_seconds=0.1,
    ):
        pass


def test_backup_target_collision_never_overwrites_or_verifies_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_backup_snapshots: Path,
) -> None:
    source = tmp_path / "metadata.sqlite3"
    _create_legacy_store(
        source,
        store=integrity.STORE_METADATA,
        wal_mode=False,
    )
    serialized = source.read_bytes()
    verifier_calls = 0
    real_verifier = (
        migrations._verify_persistent_backup_from_temporary_snapshot
    )

    class FixedDatetime:
        @staticmethod
        def now(_timezone):
            return datetime(2026, 7, 29, tzinfo=timezone.utc)

    class FixedUuid:
        hex = "a" * 32

    def observed_verifier(**kwargs) -> None:
        nonlocal verifier_calls
        verifier_calls += 1
        real_verifier(**kwargs)

    monkeypatch.setattr(migrations, "datetime", FixedDatetime)
    monkeypatch.setattr(migrations.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(
        migrations,
        "_verify_persistent_backup_from_temporary_snapshot",
        observed_verifier,
    )
    backup_id, backup_hash = migrations._create_verified_backup(
        store=integrity.STORE_METADATA,
        path=source,
        migration_id=migrations.MIGRATION_IDS[integrity.STORE_METADATA],
        serialized=serialized,
    )
    backup = (
        source.parent
        / ".sqlite-fk-backups"
        / f"{backup_id}.sqlite3"
    )
    before = backup.read_bytes()

    with pytest.raises(migrations.SqliteForeignKeyBackupFailed):
        migrations._create_verified_backup(
            store=integrity.STORE_METADATA,
            path=source,
            migration_id=migrations.MIGRATION_IDS[
                integrity.STORE_METADATA
            ],
            serialized=serialized,
        )
    assert verifier_calls == 1
    assert backup.read_bytes() == before
    assert _sha256(backup) == backup_hash
    assert _sidecars(backup) == {}
    assert list(isolated_backup_snapshots.iterdir()) == []

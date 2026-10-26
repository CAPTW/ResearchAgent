from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from app.cli import sqlite_integrity as sqlite_command
from app.db import bootstrap
from app.db import sqlite_integrity as integrity
from app.db import sqlite_migrations as migrations
from app.db import sqlite_snapshot as snapshots


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
    if not legacy:
        return schema
    return _strip_required_foreign_keys(schema, expected_count)


def _create_database(
    path: Path,
    store: str,
    *,
    legacy: bool = True,
    wal_mode: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        if wal_mode:
            assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.executescript(_schema_for(store, legacy=legacy))
        connection.commit()
        if wal_mode:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()


def _source_fingerprint(path: Path) -> snapshots.SqliteSourceFingerprint:
    return snapshots.capture_sqlite_source_fingerprint(path)


def _external_snapshot_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path.parent / f"{tmp_path.name}-{label}"
    root.mkdir()
    return root


@pytest.fixture
def isolated_plan_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, list[Path]]]:
    root = _external_snapshot_root(tmp_path, "snapshot-root")
    created: list[Path] = []
    real_capture = snapshots.capture_stable_sqlite_snapshot

    @contextmanager
    def capture(path: Path) -> Iterator[snapshots.SqliteAuditSnapshot]:
        with real_capture(
            path,
            stability_delay_seconds=0,
            temp_root=root,
        ) as captured:
            created.append(captured.temp_directory)
            yield captured

    monkeypatch.setattr(
        migrations,
        "capture_stable_sqlite_snapshot",
        capture,
    )
    try:
        yield root, created
    finally:
        assert list(root.iterdir()) == []
        root.rmdir()


@pytest.mark.skipif(
    os.name != "nt",
    reason="the historical SQLite VFS sidecar defect was observed on Windows",
)
def test_direct_wal_mode_audit_reproduces_source_sidecars_but_snapshot_plan_does_not(
    tmp_path: Path,
    isolated_plan_snapshots: tuple[Path, list[Path]],
) -> None:
    path = tmp_path / "scheduler-direct-defect.sqlite3"
    _create_database(
        path,
        integrity.STORE_SCHEDULER,
        legacy=True,
        wal_mode=True,
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE run_jobs")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()

    source = _source_fingerprint(path)
    direct = integrity.open_sqlite_read_only(
        path,
        store=integrity.STORE_SCHEDULER,
    )
    try:
        first = migrations._plan_from_connection(
            direct,
            store=integrity.STORE_SCHEDULER,
            source_size=source.main.size,
            source_fingerprint=source.main.sha256,
            sidecars=(),
            source_observation_status=snapshots.SOURCE_OBSERVATION_NONE,
            snapshot_strategy=None,
            query_only_enabled=True,
        )
    finally:
        direct.close()

    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    assert wal.is_file()
    assert wal.stat().st_size == 0
    assert shm.is_file()
    assert shm.stat().st_size > 0
    observed = snapshots.classify_sqlite_source(_source_fingerprint(path))
    assert observed.status == snapshots.SOURCE_OBSERVATION_STABLE_ZERO_WAL

    before_repaired = _tree_snapshot(tmp_path)
    repaired = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    repeated = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert first.status == integrity.STATUS_MIGRATION_REQUIRED
    assert repaired.status == first.status
    assert repaired.plan_digest == first.plan_digest
    assert repeated.plan_digest == repaired.plan_digest
    assert repaired.source_observation_status == (
        snapshots.SOURCE_OBSERVATION_STABLE_ZERO_WAL
    )
    assert _tree_snapshot(tmp_path) == before_repaired


def test_snapshot_plan_opens_only_the_verified_temp_copy_and_never_writes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_plan_snapshots: tuple[Path, list[Path]],
) -> None:
    snapshot_root, created = isolated_plan_snapshots
    path = tmp_path / "source.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    before = _source_fingerprint(path)
    before_tree = _tree_snapshot(tmp_path)
    real_connect = snapshots.sqlite3.connect
    real_copy = snapshots._copy_main_database
    connects: list[str] = []
    copies: list[tuple[Path, Path]] = []

    def connect(database, *args, **kwargs):
        connects.append(str(database))
        return real_connect(database, *args, **kwargs)

    def copy(source: Path, destination: Path) -> int:
        copies.append((source.resolve(), destination.resolve()))
        return real_copy(source, destination)

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect)
    monkeypatch.setattr(snapshots, "_copy_main_database", copy)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )

    assert plan.status == integrity.STATUS_MIGRATION_REQUIRED
    assert plan.snapshot_strategy == snapshots.SNAPSHOT_STRATEGY
    assert plan.query_only_enabled is True
    assert len(connects) == 1
    assert "immutable=1" in connects[0]
    assert path.resolve().as_uri() not in connects[0]
    assert len(copies) == 1
    assert copies[0][0] == path.resolve()
    assert copies[0][1].parent.parent == snapshot_root.resolve()
    assert copies[0][0] != copies[0][1]
    assert _source_fingerprint(path) == before
    assert _tree_snapshot(tmp_path) == before_tree
    assert len(created) == 1
    assert list(snapshot_root.iterdir()) == []


def test_stable_zero_wal_sidecars_are_diagnostic_only_and_digest_is_semantic(
    tmp_path: Path,
    isolated_plan_snapshots: tuple[Path, list[Path]],
) -> None:
    snapshot_root, _created = isolated_plan_snapshots
    path = tmp_path / "zero-wal.sqlite3"
    _create_database(path, integrity.STORE_SCHEDULER)
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"\0" * 32_768)
    before = _source_fingerprint(path)

    first = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    os.utime(shm, ns=(shm.stat().st_atime_ns, shm.stat().st_mtime_ns + 1_000_000))
    second = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert first.status == integrity.STATUS_MIGRATION_REQUIRED
    assert first.source_observation_status == (
        snapshots.SOURCE_OBSERVATION_STABLE_ZERO_WAL
    )
    assert first.eligible is False
    assert first.backup_required is True
    assert snapshots.CODE_SOURCE_SIDECARS_PRESENT in first.issue_codes
    assert first.plan_digest == second.plan_digest
    assert _source_fingerprint(path).main == before.main
    assert wal.stat().st_size == 0
    assert shm.stat().st_size == 32_768
    assert list(snapshot_root.iterdir()) == []

    with pytest.raises(migrations.SqliteForeignKeyMigrationBusy):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_SCHEDULER,
            path=path,
            expected_plan_digest=first.plan_digest,
        )
    assert not (path.parent / ".sqlite-fk-backups").exists()
    assert not migrations._migration_lock_path(path).exists()

    wal.unlink()
    shm.unlink()
    clean = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert clean.source_observation_status == snapshots.SOURCE_OBSERVATION_NONE
    assert clean.eligible is True
    assert clean.plan_digest == first.plan_digest
    applied = migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
        expected_plan_digest=clean.plan_digest,
    )
    assert applied.applied is True


@pytest.mark.parametrize(
    ("suffix", "content", "observation", "code"),
    (
        (
            "-wal",
            b"uncheckpointed",
            snapshots.SOURCE_OBSERVATION_ACTIVE_WAL,
            snapshots.CODE_ACTIVE_WAL,
        ),
        (
            "-journal",
            b"rollback",
            snapshots.SOURCE_OBSERVATION_ROLLBACK_JOURNAL,
            snapshots.CODE_ROLLBACK_JOURNAL,
        ),
    ),
)
def test_unsafe_sidecars_block_before_copy_or_sqlite_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_plan_snapshots: tuple[Path, list[Path]],
    suffix: str,
    content: bytes,
    observation: str,
    code: str,
) -> None:
    snapshot_root, _created = isolated_plan_snapshots
    path = tmp_path / f"blocked{suffix}.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    Path(f"{path}{suffix}").write_bytes(content)
    before = _tree_snapshot(tmp_path)
    copies = 0
    audits = 0

    def forbidden_copy(*_args, **_kwargs):
        nonlocal copies
        copies += 1
        raise AssertionError("unsafe source must not be copied")

    def forbidden_audit(*_args, **_kwargs):
        nonlocal audits
        audits += 1
        raise AssertionError("unsafe source must not be audited")

    monkeypatch.setattr(snapshots, "_copy_main_database", forbidden_copy)
    monkeypatch.setattr(migrations, "open_immutable_snapshot", forbidden_audit)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )

    assert plan.status == migrations.STATUS_BUSY
    assert plan.source_observation_status == observation
    assert plan.plan_digest is None
    assert code in plan.issue_codes
    assert copies == 0
    assert audits == 0
    assert _tree_snapshot(tmp_path) == before
    assert list(snapshot_root.iterdir()) == []


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_copies"),
    (
        ("main_before_copy", snapshots.CODE_SOURCE_UNSTABLE, 0),
        ("main_during_copy", snapshots.CODE_SOURCE_UNSTABLE, 1),
        ("main_after_copy", snapshots.CODE_SOURCE_UNSTABLE, 1),
        ("sidecar_set", snapshots.CODE_SIDECAR_UNSTABLE, 0),
        ("sidecar_bytes", snapshots.CODE_SIDECAR_UNSTABLE, 0),
    ),
)
def test_unstable_sources_never_produce_a_plan_and_leave_no_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
    expected_copies: int,
) -> None:
    path = tmp_path / f"unstable-{failure}.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    root = _external_snapshot_root(tmp_path, "unstable-snapshots")
    real_capture = snapshots.capture_stable_sqlite_snapshot
    real_copy = snapshots._copy_main_database
    copies = 0
    audits = 0

    if failure == "sidecar_bytes":
        Path(f"{path}-wal").write_bytes(b"")
        Path(f"{path}-shm").write_bytes(b"\0" * 32_768)

    def mutate_between_fingerprints(_delay: float) -> None:
        if failure == "main_before_copy":
            with path.open("ab") as handle:
                handle.write(b"changed-before-copy")
        elif failure == "sidecar_set":
            Path(f"{path}-shm").write_bytes(b"new-sidecar")
        elif failure == "sidecar_bytes":
            with Path(f"{path}-shm").open("r+b") as handle:
                handle.seek(0)
                handle.write(b"x")

    @contextmanager
    def capture(source: Path) -> Iterator[snapshots.SqliteAuditSnapshot]:
        with real_capture(
            source,
            stability_delay_seconds=0,
            temp_root=root,
            sleep=mutate_between_fingerprints,
        ) as captured:
            yield captured

    def injected_copy(source: Path, destination: Path) -> int:
        nonlocal copies
        copies += 1
        if failure == "main_during_copy":
            copied = 0
            with source.open("rb") as reader, destination.open("xb") as writer:
                chunk = reader.read(128)
                writer.write(chunk)
                copied += len(chunk)
                with source.open("ab") as source_writer:
                    source_writer.write(b"changed-during-copy")
                remainder = reader.read()
                writer.write(remainder)
                copied += len(remainder)
                writer.flush()
                os.fsync(writer.fileno())
            return copied
        copied = real_copy(source, destination)
        if failure == "main_after_copy":
            with source.open("ab") as handle:
                handle.write(b"changed-after-copy")
        return copied

    def forbidden_audit(*_args, **_kwargs):
        nonlocal audits
        audits += 1
        raise AssertionError("unstable source must not be audited")

    monkeypatch.setattr(migrations, "capture_stable_sqlite_snapshot", capture)
    monkeypatch.setattr(snapshots, "_copy_main_database", injected_copy)
    monkeypatch.setattr(migrations, "open_immutable_snapshot", forbidden_audit)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )

    assert plan.status == migrations.STATUS_BUSY
    assert plan.plan_digest is None
    assert expected_code in plan.issue_codes
    assert copies == expected_copies
    assert audits == 0
    assert list(root.iterdir()) == []
    root.rmdir()


def test_immutable_connection_is_bounded_to_snapshot_and_creates_no_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "immutable-source.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    root = _external_snapshot_root(tmp_path, "immutable-root")
    real_connect = snapshots.sqlite3.connect
    calls: list[str] = []

    def connect(database, *args, **kwargs):
        calls.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect)
    with snapshots.capture_stable_sqlite_snapshot(
        path,
        stability_delay_seconds=0,
        temp_root=root,
    ) as captured:
        assert not os.path.samefile(path, captured.snapshot_path)
        connection = snapshots.open_immutable_snapshot(
            captured,
            store=integrity.STORE_METADATA,
        )
        try:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden (value TEXT)")
        finally:
            connection.close()
        assert not Path(f"{captured.snapshot_path}-journal").exists()
        assert not Path(f"{captured.snapshot_path}-wal").exists()
        assert not Path(f"{captured.snapshot_path}-shm").exists()
        assert len(calls) == 1
        assert captured.snapshot_path.as_uri() in calls[0]
        assert path.as_uri() not in calls[0]
        assert "mode=ro" in calls[0]
        assert "immutable=1" in calls[0]
        assert "cache=private" in calls[0]
        assert "nolock=1" not in calls[0]
    assert list(root.iterdir()) == []
    root.rmdir()


def test_plan_digest_is_stable_across_snapshots_and_changes_with_semantics(
    tmp_path: Path,
    isolated_plan_snapshots: tuple[Path, list[Path]],
) -> None:
    snapshot_root, created = isolated_plan_snapshots
    path = tmp_path / "deterministic.sqlite3"
    _create_database(path, integrity.STORE_METADATA)

    plans = [
        migrations.plan_sqlite_foreign_key_migration(
            store=integrity.STORE_METADATA,
            path=path,
        )
        for _index in range(5)
    ]
    assert len({plan.plan_digest for plan in plans}) == 1
    assert len({plan.status for plan in plans}) == 1
    assert len({plan.relation_audits for plan in plans}) == 1
    assert len({plan.orphan_counts for plan in plans}) == 1
    assert len({item.name for item in created}) == 5
    assert list(snapshot_root.iterdir()) == []

    initial = plans[0]
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    mtime_only = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert mtime_only.plan_digest == initial.plan_digest

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO index_state (key, value) VALUES ('semantic', 'data')"
        )
        connection.commit()
    finally:
        connection.close()
    data_changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert data_changed.plan_digest != initial.plan_digest

    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE INDEX semantic_probe ON runs(status)")
        connection.commit()
    finally:
        connection.close()
    schema_changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert schema_changed.plan_digest != data_changed.plan_digest

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO papers (run_id, paper_id, title, theme, status)
            VALUES ('missing-parent', 'orphan-paper', 'Paper', 'theme', 'selected')
            """
        )
        connection.commit()
    finally:
        connection.close()
    orphan_changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert orphan_changed.status == migrations.STATUS_ORPHANS_DETECTED
    assert orphan_changed.total_orphan_count == 1
    assert orphan_changed.plan_digest != schema_changed.plan_digest

    compliant = tmp_path / "compliant.sqlite3"
    _create_database(compliant, integrity.STORE_METADATA, legacy=False)
    inventory_changed = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=compliant,
    )
    assert inventory_changed.current_declared_fk_count == 4
    assert inventory_changed.plan_digest != initial.plan_digest


def test_v2_candidate_still_checks_approved_scheduler_relation(
    tmp_path: Path,
    isolated_plan_snapshots: tuple[Path, list[Path]],
) -> None:
    path = tmp_path / "scheduler-mismatch.sqlite3"
    _create_database(path, integrity.STORE_SCHEDULER)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE run_jobs")
        connection.execute(
            """
            INSERT INTO scheduler_queue_items (
              queue_item_id, schedule_id, due_at, status,
              dispatch_attempts, created_at, updated_at, payload_json
            ) VALUES (
              'queue-secret', 'missing-schedule', 'now', 'queued',
              0, 'now', 'now', '{}'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert plan.status == migrations.STATUS_ORPHANS_DETECTED
    assert (
        plan.schema_classification
        == migrations.SCHEMA_CLASSIFICATION_SCHEDULER_V2
    )
    assert plan.schema_compatible is True
    assert plan.eligible is False
    assert len(plan.relation_audits) == 1
    relation = plan.relation_audits[0]
    assert relation.status == migrations.RELATION_CHECKED
    assert relation.orphan_count == 1
    assert plan.total_orphan_count == 1
    payload = json.dumps(plan.to_payload(), sort_keys=True)
    assert "queue-secret" not in payload
    assert "missing-schedule" not in payload
    assert "SELECT " not in payload
    assert str(tmp_path) not in payload


@pytest.mark.parametrize(
    ("ddl", "expected_status"),
    (
        (
            "CREATE TABLE scheduler_schedules "
            "(schedule_id TEXT PRIMARY KEY)",
            migrations.RELATION_CHILD_TABLE_MISSING,
        ),
        (
            """
            CREATE TABLE scheduler_schedules (schedule_id TEXT PRIMARY KEY);
            CREATE TABLE scheduler_queue_items (queue_item_id TEXT PRIMARY KEY);
            """,
            migrations.RELATION_CHILD_COLUMN_MISSING,
        ),
        (
            """
            CREATE TABLE scheduler_queue_items (
              queue_item_id TEXT PRIMARY KEY,
              schedule_id TEXT
            );
            """,
            migrations.RELATION_PARENT_TABLE_MISSING,
        ),
        (
            """
            CREATE TABLE scheduler_schedules (other_id TEXT PRIMARY KEY);
            CREATE TABLE scheduler_queue_items (
              queue_item_id TEXT PRIMARY KEY,
              schedule_id TEXT
            );
            """,
            migrations.RELATION_PARENT_COLUMN_MISSING,
        ),
    ),
)
def test_relation_diagnostics_report_exact_skip_reason(
    tmp_path: Path,
    isolated_plan_snapshots: tuple[Path, list[Path]],
    ddl: str,
    expected_status: str,
) -> None:
    path = tmp_path / f"{expected_status}.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(ddl)
    finally:
        connection.close()
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    assert plan.status == integrity.STATUS_SCHEMA_MISMATCH
    assert plan.relation_audits[0].status == expected_status
    assert plan.relation_audits[0].orphan_count is None
    assert plan.total_orphan_count == 0
    assert plan.eligible is False


@pytest.mark.parametrize(
    ("store", "expected_relations"),
    (
        (integrity.STORE_METADATA, 4),
        (integrity.STORE_SCHEDULER, 1),
    ),
)
def test_metadata_and_scheduler_legacy_plans_remain_deterministic_and_eligible(
    tmp_path: Path,
    isolated_plan_snapshots: tuple[Path, list[Path]],
    store: str,
    expected_relations: int,
) -> None:
    path = tmp_path / f"{store}.sqlite3"
    _create_database(path, store)
    before = _source_fingerprint(path)
    first = migrations.plan_sqlite_foreign_key_migration(store=store, path=path)
    second = migrations.plan_sqlite_foreign_key_migration(store=store, path=path)

    assert first.status == integrity.STATUS_MIGRATION_REQUIRED
    assert first.current_declared_fk_count == 0
    assert first.target_fk_count == expected_relations
    assert len(first.missing_relationships) == expected_relations
    assert len(first.relation_audits) == expected_relations
    assert all(
        item.status == migrations.RELATION_CHECKED
        for item in first.relation_audits
    )
    assert first.total_orphan_count == 0
    assert first.source_observation_status == snapshots.SOURCE_OBSERVATION_NONE
    assert first.eligible is True
    assert first.plan_digest == second.plan_digest
    assert _source_fingerprint(path) == before


def test_snapshot_failures_return_stable_redacted_codes_and_clean_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "copy-failure-source.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    root = _external_snapshot_root(tmp_path, "failure-root")
    real_capture = snapshots.capture_stable_sqlite_snapshot
    real_cleanup = snapshots._cleanup_snapshot_directory

    @contextmanager
    def capture(source: Path) -> Iterator[snapshots.SqliteAuditSnapshot]:
        with real_capture(
            source,
            stability_delay_seconds=0,
            temp_root=root,
        ) as captured:
            yield captured

    monkeypatch.setattr(migrations, "capture_stable_sqlite_snapshot", capture)

    def copy_failure(*_args, **_kwargs):
        raise snapshots.SqliteSnapshotCopyFailed()

    monkeypatch.setattr(snapshots, "_copy_main_database", copy_failure)
    failed_copy = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert failed_copy.plan_digest is None
    assert failed_copy.issue_codes == (snapshots.CODE_COPY_FAILED,)
    assert list(root.iterdir()) == []

    monkeypatch.undo()
    root.mkdir(exist_ok=True)

    @contextmanager
    def capture_for_cleanup(
        source: Path,
    ) -> Iterator[snapshots.SqliteAuditSnapshot]:
        with real_capture(
            source,
            stability_delay_seconds=0,
            temp_root=root,
        ) as captured:
            yield captured

    def cleanup_failure(directory: Path) -> None:
        real_cleanup(directory)
        raise snapshots.SqliteSnapshotCleanupFailed()

    monkeypatch.setattr(
        migrations,
        "capture_stable_sqlite_snapshot",
        capture_for_cleanup,
    )
    monkeypatch.setattr(snapshots, "_cleanup_snapshot_directory", cleanup_failure)
    failed_cleanup = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert failed_cleanup.plan_digest is None
    assert failed_cleanup.issue_codes == (snapshots.CODE_CLEANUP_FAILED,)
    assert list(root.iterdir()) == []
    payload = json.dumps(failed_cleanup.to_payload(), sort_keys=True)
    assert str(tmp_path) not in payload
    assert path.name not in payload
    assert "SELECT " not in payload
    root.rmdir()


def test_snapshot_verification_rejects_a_corrupted_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "verification-source.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    root = _external_snapshot_root(tmp_path, "verification-root")
    real_capture = snapshots.capture_stable_sqlite_snapshot
    real_copy = snapshots._copy_main_database
    audits = 0

    @contextmanager
    def capture(source: Path) -> Iterator[snapshots.SqliteAuditSnapshot]:
        with real_capture(
            source,
            stability_delay_seconds=0,
            temp_root=root,
        ) as captured:
            yield captured

    def corrupt_copy(source: Path, destination: Path) -> int:
        copied = real_copy(source, destination)
        with destination.open("r+b") as handle:
            original = handle.read(1)
            handle.seek(0)
            handle.write(bytes([original[0] ^ 0xFF]))
            handle.flush()
            os.fsync(handle.fileno())
        return copied

    def forbidden_audit(*_args, **_kwargs):
        nonlocal audits
        audits += 1
        raise AssertionError("a corrupt destination must not be audited")

    monkeypatch.setattr(migrations, "capture_stable_sqlite_snapshot", capture)
    monkeypatch.setattr(snapshots, "_copy_main_database", corrupt_copy)
    monkeypatch.setattr(migrations, "open_immutable_snapshot", forbidden_audit)
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
    )
    assert plan.status == migrations.STATUS_UNREADABLE
    assert plan.plan_digest is None
    assert plan.issue_codes == (snapshots.CODE_VERIFICATION_FAILED,)
    assert audits == 0
    assert list(root.iterdir()) == []
    root.rmdir()

    with pytest.raises(snapshots.SqliteSnapshotCopyFailed):
        with snapshots.capture_stable_sqlite_snapshot(
            path,
            stability_delay_seconds=0,
            temp_root=path.parent,
        ):
            pytest.fail("a source-directory snapshot must not be yielded")


def test_cli_plan_is_snapshot_isolated_deterministic_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_plan_snapshots: tuple[Path, list[Path]],
) -> None:
    snapshot_root, _created = isolated_plan_snapshots
    path = tmp_path / "cli.sqlite3"
    _create_database(path, integrity.STORE_METADATA)
    monkeypatch.setattr(sqlite_command, "_store_path", lambda _store: path)
    real_connect = snapshots.sqlite3.connect
    connects: list[str] = []

    def connect(database, *args, **kwargs):
        connects.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect)
    before = _tree_snapshot(tmp_path)
    assert sqlite_command.main(["--store", "metadata", "--plan"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert sqlite_command.main(["--store", "metadata", "--plan"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["plan_digest"] == second["plan_digest"]
    assert first["snapshot_strategy"] == snapshots.SNAPSHOT_STRATEGY
    assert first["source_observation_status"] == snapshots.SOURCE_OBSERVATION_NONE
    assert all("immutable=1" in call for call in connects)
    assert all(path.resolve().as_uri() not in call for call in connects)
    encoded = json.dumps(first, sort_keys=True)
    assert str(tmp_path) not in encoded
    assert path.name not in encoded
    assert "SELECT " not in encoded
    assert _tree_snapshot(tmp_path) == before
    assert list(snapshot_root.iterdir()) == []

    Path(f"{path}-wal").write_bytes(b"active")
    assert sqlite_command.main(["--store", "metadata", "--plan"]) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == migrations.STATUS_BUSY
    assert blocked["source_observation_status"] == (
        snapshots.SOURCE_OBSERVATION_ACTIVE_WAL
    )
    assert blocked["plan_digest"] is None
    assert snapshots.CODE_ACTIVE_WAL in blocked["issue_codes"]
    assert len(connects) == 2
    assert list(snapshot_root.iterdir()) == []


def test_cli_help_import_and_source_guards_do_not_open_live_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("help must not open SQLite")

    monkeypatch.setattr(snapshots.sqlite3, "connect", forbidden_connect)
    with pytest.raises(SystemExit) as raised:
        sqlite_command.main(["--help"])
    assert raised.value.code == 0
    assert "--plan" in capsys.readouterr().out

    app_sources = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(Path("app").rglob("*.py"))
    }
    immutable_occurrences = [
        (path.as_posix(), line_number)
        for path, source in app_sources.items()
        for line_number, line in enumerate(source.splitlines(), start=1)
        if "immutable=1" in line
    ]
    assert len(immutable_occurrences) == 1
    assert immutable_occurrences[0][0] == "app/db/sqlite_snapshot.py"
    assert all("nolock=1" not in source for source in app_sources.values())
    assert "immutable=1" not in app_sources[Path("app/db/sqlite_integrity.py")]

    migration_tree = ast.parse(
        app_sources[Path("app/db/sqlite_migrations.py")]
    )
    plan_node = next(
        node
        for node in migration_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "plan_sqlite_foreign_key_migration"
    )
    called_names = {
        node.func.id
        for node in ast.walk(plan_node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open_sqlite_read_only" not in called_names
    assert "open_sqlite_read_write" not in called_names
    assert "capture_stable_sqlite_snapshot" in called_names
    assert "open_immutable_snapshot" in called_names

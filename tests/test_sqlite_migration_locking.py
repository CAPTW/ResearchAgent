from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.cli import sqlite_integrity as sqlite_cli
from app.db import bootstrap
from app.db import sqlite_integrity as integrity
from app.db import sqlite_migrations as migrations
from app.repositories.file_locks import file_lock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    store: str,
    *,
    exact_scheduler_v2: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if store == integrity.STORE_METADATA:
        schema = _strip_foreign_keys(bootstrap.SCHEMA_SQL, 4)
    elif exact_scheduler_v2:
        scheduler_only = bootstrap.SCHEDULER_SCHEMA_SQL.split(
            "CREATE TABLE IF NOT EXISTS run_jobs",
            1,
        )[0]
        schema = _strip_foreign_keys(scheduler_only, 1)
    else:
        schema = _strip_foreign_keys(
            bootstrap.SCHEDULER_SCHEMA_SQL,
            1,
        )
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
        if exact_scheduler_v2:
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
    finally:
        connection.close()


def _backup_paths(path: Path) -> list[Path]:
    return sorted(
        (path.parent / ".sqlite-fk-backups").glob("*.sqlite3")
    )


def _marker_path(path: Path) -> Path:
    return path.parent / f".{path.name}.fk-migration.lock"


def _minimal_subprocess_env(data_root: Path, temp_root: Path) -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
    )
    environment = {
        key: os.environ[key]
        for key in allowed
        if key in os.environ
    }
    environment.update(
        {
            "ALL_PROXY": "",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "NO_PROXY": "*",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONUTF8": "1",
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "XRML_DATA_DIR": str(data_root),
        }
    )
    return environment


def _run_cli(
    data_root: Path,
    temp_root: Path,
    *,
    store: str,
    operation: str,
    digest: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    arguments = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "app.cli.sqlite_integrity",
        "--store",
        store,
        operation,
    ]
    if digest is not None:
        arguments.extend(("--expected-plan-digest", digest))
    completed = subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        env=_minimal_subprocess_env(data_root, temp_root),
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    lines = [line for line in completed.stdout.splitlines() if line]
    assert len(lines) == 1
    return completed, json.loads(lines[0])


def _scheduler_state(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        return {
            "schedules": int(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduler_schedules"
                ).fetchone()[0]
            ),
            "heartbeats": int(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduler_worker_heartbeats"
                ).fetchone()[0]
            ),
            "queue": int(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduler_queue_items"
                ).fetchone()[0]
            ),
            "run_jobs": "run_jobs" in tables,
            "queue_fk": len(
                connection.execute(
                    "PRAGMA foreign_key_list(scheduler_queue_items)"
                ).fetchall()
            ),
            "quick_check": str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            ),
        }
    finally:
        connection.close()


def test_exact_scheduler_v2_cli_apply_reuses_existing_empty_anchor(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    path = data_root / "scheduler.sqlite3"
    _create_legacy_store(
        path,
        integrity.STORE_SCHEDULER,
        exact_scheduler_v2=True,
    )
    marker = _marker_path(path)
    marker.touch()
    marker_before = (
        marker.stat().st_size,
        marker.stat().st_mtime_ns,
        _sha256(marker),
    )

    planned, plan = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_SCHEDULER,
        operation="--plan",
    )
    assert planned.returncode == 0
    assert planned.stderr == ""
    assert plan["status"] == integrity.STATUS_MIGRATION_REQUIRED
    assert plan["eligible"] is True
    digest = str(plan["plan_digest"])

    applied, result = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_SCHEDULER,
        operation="--apply",
        digest=digest,
    )
    assert applied.returncode == 0
    assert applied.stderr == ""
    assert result["status"] == "migration_applied"
    assert len(_backup_paths(path)) == 1
    assert _scheduler_state(path) == {
        "schedules": 120,
        "heartbeats": 908,
        "queue": 0,
        "run_jobs": True,
        "queue_fk": 1,
        "quick_check": "ok",
    }
    assert marker.exists()
    assert (
        marker.stat().st_size,
        marker.stat().st_mtime_ns,
        _sha256(marker),
    ) == marker_before

    _, current = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_SCHEDULER,
        operation="--plan",
    )
    second, no_op = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_SCHEDULER,
        operation="--apply",
        digest=str(current["plan_digest"]),
    )
    assert second.returncode == 0
    assert second.stderr == ""
    assert no_op["status"] == "already_compliant"
    assert len(_backup_paths(path)) == 1


def test_exact_metadata_cli_apply_reuses_existing_empty_anchor(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    path = data_root / "metadata_index.sqlite3"
    _create_legacy_store(path, integrity.STORE_METADATA)
    marker = _marker_path(path)
    marker.touch()

    _, plan = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_METADATA,
        operation="--plan",
    )
    applied, result = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_METADATA,
        operation="--apply",
        digest=str(plan["plan_digest"]),
    )
    assert applied.returncode == 0
    assert applied.stderr == ""
    assert result["status"] == "migration_applied"
    assert len(_backup_paths(path)) == 1
    connection = sqlite3.connect(path)
    try:
        declared = sum(
            len(
                connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                ).fetchall()
            )
            for table in (
                "run_topics",
                "papers",
                "evidence",
                "report_sections",
            )
        )
    finally:
        connection.close()
    assert declared == 4
    assert marker.exists()
    assert marker.stat().st_size == 0

    _, current = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_METADATA,
        operation="--plan",
    )
    second, no_op = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_METADATA,
        operation="--apply",
        digest=str(current["plan_digest"]),
    )
    assert second.returncode == 0
    assert no_op["status"] == "already_compliant"
    assert len(_backup_paths(path)) == 1


def _start_lock_holder(
    marker: Path,
    *,
    data_root: Path,
    temp_root: Path,
    die: bool,
) -> subprocess.Popen[str]:
    ending = "os._exit(23)" if die else "sys.stdin.readline()"
    code = (
        "from pathlib import Path\n"
        "import os, sys\n"
        "from app.repositories.file_locks import file_lock\n"
        "with file_lock(Path(sys.argv[1]), timeout_seconds=1):\n"
        "    print('LOCKED', flush=True)\n"
        f"    {ending}\n"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            code,
            str(marker),
        ],
        cwd=PROJECT_ROOT,
        env=_minimal_subprocess_env(data_root, temp_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_cli_live_lock_and_process_death_preserve_and_reuse_anchor(
    tmp_path: Path,
) -> None:
    for label, die in (("live", False), ("death", True)):
        data_root = tmp_path / label / "data"
        temp_root = tmp_path / label / "temp"
        temp_root.mkdir(parents=True)
        path = data_root / "metadata_index.sqlite3"
        _create_legacy_store(path, integrity.STORE_METADATA)
        _, plan = _run_cli(
            data_root,
            temp_root,
            store=integrity.STORE_METADATA,
            operation="--plan",
        )
        marker = _marker_path(path)
        holder = _start_lock_holder(
            marker,
            data_root=data_root,
            temp_root=temp_root,
            die=die,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "LOCKED"
            if die:
                assert holder.wait(timeout=5) == 23
            else:
                source_before = _sha256(path)
                blocked, payload = _run_cli(
                    data_root,
                    temp_root,
                    store=integrity.STORE_METADATA,
                    operation="--apply",
                    digest=str(plan["plan_digest"]),
                )
                assert blocked.returncode == 2
                assert blocked.stderr == ""
                assert payload == {
                    "status": "blocked",
                    "code": migrations.CODE_MIGRATION_BUSY,
                    "store": integrity.STORE_METADATA,
                    "phase": migrations.BUSY_PHASE_MIGRATION_LOCK,
                    "retryable": True,
                }
                assert _sha256(path) == source_before
                assert _backup_paths(path) == []
                assert holder.stdin is not None
                holder.stdin.write("\n")
                holder.stdin.flush()
                assert holder.wait(timeout=5) == 0
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)

        assert marker.exists()
        assert marker.stat().st_size == 0
        applied, result = _run_cli(
            data_root,
            temp_root,
            store=integrity.STORE_METADATA,
            operation="--apply",
            digest=str(plan["plan_digest"]),
        )
        assert applied.returncode == 0
        assert result["status"] == "migration_applied"


def test_cli_exclusive_busy_is_distinct_and_next_invocation_succeeds(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    path = data_root / "metadata_index.sqlite3"
    _create_legacy_store(path, integrity.STORE_METADATA)
    _, plan = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_METADATA,
        operation="--plan",
    )
    source_before = _sha256(path)
    code = (
        "import sqlite3, sys\n"
        "connection = sqlite3.connect(sys.argv[1])\n"
        "connection.execute('BEGIN')\n"
        "connection.execute('SELECT COUNT(*) FROM runs').fetchone()\n"
        "print('READING', flush=True)\n"
        "sys.stdin.readline()\n"
        "connection.rollback()\n"
        "connection.close()\n"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            code,
            str(path),
        ],
        cwd=PROJECT_ROOT,
        env=_minimal_subprocess_env(data_root, temp_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READING"
        blocked, payload = _run_cli(
            data_root,
            temp_root,
            store=integrity.STORE_METADATA,
            operation="--apply",
            digest=str(plan["plan_digest"]),
        )
        assert blocked.returncode == 2
        assert blocked.stderr == ""
        assert payload == {
            "status": "blocked",
            "code": migrations.CODE_MIGRATION_BUSY,
            "store": integrity.STORE_METADATA,
            "phase": migrations.BUSY_PHASE_EXCLUSIVE_TRANSACTION,
            "retryable": True,
        }
        assert _sha256(path) == source_before
        assert _backup_paths(path) == []
        assert _marker_path(path).stat().st_size == 0
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=5) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)

    applied, result = _run_cli(
        data_root,
        temp_root,
        store=integrity.STORE_METADATA,
        operation="--apply",
        digest=str(plan["plan_digest"]),
    )
    assert applied.returncode == 0
    assert result["status"] == "migration_applied"


@pytest.mark.parametrize(
    ("store", "exact_scheduler_v2"),
    (
        (integrity.STORE_METADATA, False),
        (integrity.STORE_SCHEDULER, True),
    ),
)
def test_one_cli_apply_acquires_migration_lock_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store: str,
    exact_scheduler_v2: bool,
) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    path = (
        bootstrap.metadata_index_path(ensure_parent=False)
        if store == integrity.STORE_METADATA
        else bootstrap.scheduler_db_path(ensure_parent=False)
    )
    _create_legacy_store(
        path,
        store,
        exact_scheduler_v2=exact_scheduler_v2,
    )
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=store,
        path=path,
    )
    original = migrations.file_lock
    acquisitions: list[str] = []

    @contextlib.contextmanager
    def counted_lock(*args, **kwargs):
        acquisitions.append("acquire")
        with original(*args, **kwargs):
            yield

    monkeypatch.setattr(migrations, "file_lock", counted_lock)
    exit_code = sqlite_cli.main(
        [
            "--store",
            store,
            "--apply",
            "--expected-plan-digest",
            str(plan.plan_digest),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "migration_applied"
    assert acquisitions == ["acquire"]


def test_backup_is_serialized_after_exclusive_and_verified_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scheduler.sqlite3"
    _create_legacy_store(
        path,
        integrity.STORE_SCHEDULER,
        exact_scheduler_v2=True,
    )
    plan = migrations.plan_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
    )
    events: list[str] = []
    original_serialize = migrations._serialize_source_for_backup
    original_backup = migrations._create_verified_backup
    original_apply = migrations._apply_scheduler_v2_schema

    def observed_serialize(connection, *, store):
        assert connection.in_transaction
        events.append("exclusive_transaction")
        return original_serialize(connection, store=store)

    def observed_backup(*, store, path, migration_id, serialized):
        assert events == ["exclusive_transaction"]
        result = original_backup(
            store=store,
            path=path,
            migration_id=migration_id,
            serialized=serialized,
        )
        events.append("backup_verified")
        return result

    def observed_apply(connection, *, fail_at, before_content):
        assert events == [
            "exclusive_transaction",
            "backup_verified",
        ]
        events.append("first_ddl")
        return original_apply(
            connection,
            fail_at=fail_at,
            before_content=before_content,
        )

    monkeypatch.setattr(
        migrations,
        "_serialize_source_for_backup",
        observed_serialize,
    )
    monkeypatch.setattr(
        migrations,
        "_create_verified_backup",
        observed_backup,
    )
    monkeypatch.setattr(
        migrations,
        "_apply_scheduler_v2_schema",
        observed_apply,
    )
    result = migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_SCHEDULER,
        path=path,
        expected_plan_digest=str(plan.plan_digest),
    )
    assert result.status == "migration_applied"
    assert events == [
        "exclusive_transaction",
        "backup_verified",
        "first_ddl",
    ]


class _TrackingConnection(sqlite3.Connection):
    closed: bool

    def close(self) -> None:
        self.closed = True
        super().close()


def test_source_connections_close_on_all_apply_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[_TrackingConnection] = []

    def tracked_open(
        path: Path,
        *,
        store: str,
        timeout_seconds: float,
        isolation_level: str | None = "",
    ) -> _TrackingConnection:
        connection = sqlite3.connect(
            str(path),
            timeout=timeout_seconds,
            isolation_level=isolation_level,
            factory=_TrackingConnection,
        )
        connection.closed = False
        connection.row_factory = sqlite3.Row
        integrity.configure_sqlite_read_write(
            connection,
            store=store,
            busy_timeout_ms=max(1, int(timeout_seconds * 1000)),
        )
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        migrations,
        "open_sqlite_read_write",
        tracked_open,
    )

    def new_store(label: str) -> tuple[Path, migrations.MigrationPlan]:
        path = tmp_path / label / "metadata_index.sqlite3"
        _create_legacy_store(path, integrity.STORE_METADATA)
        plan = migrations.plan_sqlite_foreign_key_migration(
            store=integrity.STORE_METADATA,
            path=path,
        )
        return path, plan

    path, plan = new_store("success")
    migrations.apply_sqlite_foreign_key_migration(
        store=integrity.STORE_METADATA,
        path=path,
        expected_plan_digest=str(plan.plan_digest),
    )
    assert opened and all(connection.closed for connection in opened)
    opened.clear()

    path, plan = new_store("injected")
    with pytest.raises(migrations.SqliteMigrationError):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_METADATA,
            path=path,
            expected_plan_digest=str(plan.plan_digest),
            fail_at="after_first_copy",
        )
    assert opened and all(connection.closed for connection in opened)
    opened.clear()

    path, plan = new_store("backup")
    source_before = _sha256(path)
    original_backup = migrations._create_verified_backup

    def failed_backup(**_kwargs):
        raise migrations.SqliteForeignKeyBackupFailed(
            integrity.STORE_METADATA
        )

    monkeypatch.setattr(
        migrations,
        "_create_verified_backup",
        failed_backup,
    )
    with pytest.raises(migrations.SqliteForeignKeyBackupFailed):
        migrations.apply_sqlite_foreign_key_migration(
            store=integrity.STORE_METADATA,
            path=path,
            expected_plan_digest=str(plan.plan_digest),
        )
    assert _sha256(path) == source_before
    assert _backup_paths(path) == []
    assert opened and all(connection.closed for connection in opened)
    opened.clear()
    monkeypatch.setattr(
        migrations,
        "_create_verified_backup",
        original_backup,
    )

    path, plan = new_store("exclusive")
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
                expected_plan_digest=str(plan.plan_digest),
                timeout_seconds=0.02,
            )
        assert (
            blocked.value.phase
            == migrations.BUSY_PHASE_EXCLUSIVE_TRANSACTION
        )
        assert opened and all(connection.closed for connection in opened)
    finally:
        reader.rollback()
        reader.close()
    opened.clear()

    path, plan = new_store("migration-lock")
    marker = _marker_path(path)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with file_lock(marker, timeout_seconds=1):
            acquired.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    try:
        with pytest.raises(
            migrations.SqliteForeignKeyMigrationBusy
        ) as blocked:
            migrations.apply_sqlite_foreign_key_migration(
                store=integrity.STORE_METADATA,
                path=path,
                expected_plan_digest=str(plan.plan_digest),
                timeout_seconds=0.02,
            )
        assert blocked.value.phase == migrations.BUSY_PHASE_MIGRATION_LOCK
        assert opened == []
    finally:
        release.set()
        holder.join(timeout=2)
    assert not holder.is_alive()


def test_migration_lock_source_guards_and_exact_ignore_patterns() -> None:
    migrations_source = (
        PROJECT_ROOT / "app" / "db" / "sqlite_migrations.py"
    ).read_text(encoding="utf-8")
    lock_source = (
        PROJECT_ROOT / "app" / "repositories" / "file_locks.py"
    ).read_text(encoding="utf-8")
    cli_source = (
        PROJECT_ROOT / "app" / "cli" / "sqlite_integrity.py"
    ).read_text(encoding="utf-8")
    ignore_lines = set(
        (PROJECT_ROOT / ".gitignore").read_text(
            encoding="utf-8"
        ).splitlines()
    )

    assert migrations_source.count("with file_lock(") == 1
    assert "file_lock(" not in cli_source
    assert "O_EXCL" not in lock_source
    assert "unlink(" not in lock_source
    assert "remove(" not in lock_source
    assert "truncate(" not in lock_source
    assert "PRAGMA foreign_keys = OFF" not in migrations_source
    assert "/storage/.scheduler.sqlite3.fk-migration.lock" in ignore_lines
    assert (
        "/storage/.metadata_index.sqlite3.fk-migration.lock"
        in ignore_lines
    )
    assert "*.lock" not in ignore_lines

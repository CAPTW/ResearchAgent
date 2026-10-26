"""Atomic metadata bootstrap regression tests.

All databases live below pytest's OS-temporary root. Spawn workers bind
``XRML_DATA_DIR`` before importing application modules.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest


EXPECTED_TABLES = {
    "index_state",
    "runs",
    "run_topics",
    "papers",
    "evidence",
    "report_sections",
    "paper_reviews",
    "ai_review_drafts",
}
EXPECTED_INDEXES = {
    "idx_runs_topic",
    "idx_runs_status",
    "idx_papers_run_score",
    "idx_papers_theme",
    "idx_evidence_source",
    "idx_paper_reviews_topic",
    "idx_paper_reviews_run",
    "idx_ai_review_drafts_topic",
}
BOOTSTRAP_PHASES = (
    "after_transaction_acquisition",
    "after_first_table",
    "after_mid_schema_object",
    "after_final_ddl",
    "before_schema_verification",
    "before_commit",
)
PROCESS_COUNT = 4


def _race_failure(exc: BaseException) -> str:
    underlying = exc.__cause__ or exc.__context__ or exc
    return "".join(traceback.format_exception(exc)) + (
        f"\nUnderlying: {type(underlying).__name__}: {underlying}; "
        f"sqlite_errorname={getattr(underlying, 'sqlite_errorname', None)}"
    ) + "\n" + "".join(traceback.format_exception(underlying))


def _process_initialize_worker(
    data_root: str,
    barrier,
    results,
    token: int,
) -> None:
    os.environ["XRML_DATA_DIR"] = data_root
    from app.db import bootstrap

    try:
        barrier.wait(timeout=15)
        with bootstrap.connect_metadata_index() as connection:
            connection.execute(
                "INSERT INTO index_state (key, value) VALUES (?, ?)",
                (f"process-{token}", str(token)),
            )
        results.put(("ok", token))
    except BaseException as exc:
        results.put(("error", _race_failure(exc)))


def _process_die_during_bootstrap(data_root: str) -> None:
    os.environ["XRML_DATA_DIR"] = data_root
    from app.db import bootstrap

    def terminate_after_first_table(phase: str) -> None:
        if phase == "after_first_table":
            os._exit(73)

    bootstrap._metadata_bootstrap_checkpoint = terminate_after_first_table
    bootstrap.connect_metadata_index()
    os._exit(74)


def _bind_metadata_root(monkeypatch: pytest.MonkeyPatch, root: Path):
    from app.db import bootstrap

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", root / "runs")
    return bootstrap, root / "metadata_index.sqlite3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(path: Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm")]


def _schema_snapshot(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        foreign_keys = sum(
            len(connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall())
            for table in tables
        )
        return {
            "tables": tables,
            "indexes": indexes,
            "foreign_keys": foreign_keys,
            "quick_check": str(connection.execute("PRAGMA quick_check").fetchone()[0]),
            "foreign_key_check": connection.execute("PRAGMA foreign_key_check").fetchall(),
        }
    finally:
        connection.close()


def _assert_current_schema(path: Path) -> None:
    snapshot = _schema_snapshot(path)
    assert snapshot["tables"] == EXPECTED_TABLES
    assert snapshot["indexes"] == EXPECTED_INDEXES
    assert snapshot["foreign_keys"] == 4
    assert snapshot["quick_check"] == "ok"
    assert snapshot["foreign_key_check"] == []


def _assert_settled(path: Path) -> None:
    assert not any(sidecar.exists() for sidecar in _sidecars(path))


def _join_processes(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        process.join(20)
    alive = [process for process in processes if process.is_alive()]
    for process in alive:
        process.terminate()
        process.join(5)
    assert alive == []


def test_fresh_bootstrap_is_one_atomic_schema_transaction_and_ready_fast_path_has_no_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path)
    phases: list[str] = []
    monkeypatch.setattr(
        bootstrap,
        "_metadata_bootstrap_checkpoint",
        phases.append,
    )

    with bootstrap.connect_metadata_index() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000

    assert phases == list(BOOTSTRAP_PHASES)
    assert len(bootstrap._METADATA_SCHEMA_STATEMENTS) == 16
    _assert_current_schema(path)
    _assert_settled(path)
    before = (path.stat().st_size, _sha256(path))

    phases.clear()
    with bootstrap.connect_metadata_index() as connection:
        assert connection.execute("SELECT COUNT(*) FROM index_state").fetchone()[0] == 0

    assert phases == []
    assert (path.stat().st_size, _sha256(path)) == before
    _assert_settled(path)


def test_in_progress_bootstrap_never_exposes_partial_schema_or_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path)
    first_table_created = threading.Event()
    resume_initializer = threading.Event()
    caller_started = threading.Event()
    results: list[str] = []
    observed_counts: set[int] = set()

    def pause_after_first_table(phase: str) -> None:
        if phase == "after_first_table":
            first_table_created.set()
            assert resume_initializer.wait(10)

    monkeypatch.setattr(
        bootstrap,
        "_metadata_bootstrap_checkpoint",
        pause_after_first_table,
    )

    def initialize() -> None:
        try:
            with bootstrap.connect_metadata_index():
                pass
            results.append("initializer_ok")
        except BaseException as exc:
            results.append(type(exc).__name__)

    def concurrent_caller() -> None:
        caller_started.set()
        try:
            with bootstrap.connect_metadata_index():
                pass
            results.append("caller_ok")
        except BaseException as exc:
            results.append(type(exc).__name__)

    initializer = threading.Thread(target=initialize)
    initializer.start()
    assert first_table_created.wait(10)

    caller = threading.Thread(target=concurrent_caller)
    caller.start()
    assert caller_started.wait(5)
    for _ in range(3):
        observer = sqlite3.connect(path, timeout=0.05)
        try:
            try:
                row = observer.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
                observed_counts.add(int(row[0]))
            except sqlite3.OperationalError:
                pass
        finally:
            observer.close()
    time.sleep(0.05)
    assert caller.is_alive()

    resume_initializer.set()
    initializer.join(10)
    caller.join(10)

    assert not initializer.is_alive()
    assert not caller.is_alive()
    assert sorted(results) == ["caller_ok", "initializer_ok"]
    assert observed_counts <= {0, 8}
    _assert_current_schema(path)
    _assert_settled(path)


def test_ten_thread_initialization_race_is_repeatably_lossless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, _ = _bind_metadata_root(monkeypatch, tmp_path)

    for iteration in range(20):
        data_root = tmp_path / f"thread-{iteration}"
        monkeypatch.setattr(bootstrap, "STORAGE_ROOT", data_root / "runs")
        path = data_root / "metadata_index.sqlite3"
        barrier = threading.Barrier(10)

        def initialize(token: int) -> str:
            barrier.wait(timeout=10)
            started = time.monotonic()
            connected = None
            inserted = None
            try:
                with bootstrap.connect_metadata_index() as connection:
                    connected = time.monotonic() - started
                    connection.execute(
                        "INSERT INTO index_state (key, value) VALUES (?, ?)",
                        (f"thread-{token}", str(token)),
                    )
                    inserted = time.monotonic() - started
                return "ok"
            except BaseException as exc:
                return _race_failure(exc) + f"\nTiming: connected={connected}, inserted={inserted}, elapsed={time.monotonic()-started}"

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(initialize, range(10)))

        assert results == ["ok"] * 10
        _assert_current_schema(path)
        observer = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            assert observer.execute("SELECT COUNT(*) FROM index_state").fetchone()[0] == 10
        finally:
            observer.close()
        _assert_settled(path)


def test_four_spawn_process_initialization_race_is_repeatably_lossless(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")

    for iteration in range(10):
        data_root = tmp_path / f"process-{iteration}"
        barrier = context.Barrier(PROCESS_COUNT)
        results = context.Queue()
        processes = [
            context.Process(
                target=_process_initialize_worker,
                args=(str(data_root), barrier, results, token),
            )
            for token in range(PROCESS_COUNT)
        ]
        for process in processes:
            process.start()
        _join_processes(processes)
        try:
            messages = [results.get(timeout=5) for _ in range(PROCESS_COUNT)]
        except queue.Empty:
            pytest.fail("spawn worker result missing")
        finally:
            results.close()
            results.join_thread()

        assert [process.exitcode for process in processes] == [0] * PROCESS_COUNT
        assert sorted(messages) == [("ok", token) for token in range(PROCESS_COUNT)]
        path = data_root / "metadata_index.sqlite3"
        _assert_current_schema(path)
        observer = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            assert observer.execute("SELECT COUNT(*) FROM index_state").fetchone()[0] == PROCESS_COUNT
        finally:
            observer.close()
        _assert_settled(path)


def test_process_death_before_commit_leaves_no_partial_schema_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = multiprocessing.get_context("spawn")
    data_root = tmp_path / "process-death"
    process = context.Process(
        target=_process_die_during_bootstrap,
        args=(str(data_root),),
    )
    process.start()
    _join_processes([process])
    assert process.exitcode == 73

    path = data_root / "metadata_index.sqlite3"
    recovery = sqlite3.connect(path)
    try:
        committed_tables = recovery.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        recovery.close()
    assert committed_tables == []

    bootstrap, _ = _bind_metadata_root(monkeypatch, data_root)
    with bootstrap.connect_metadata_index():
        pass
    _assert_current_schema(path)
    _assert_settled(path)
    assert not list(data_root.rglob("*bootstrap*"))


@pytest.mark.parametrize("phase", BOOTSTRAP_PHASES)
def test_failure_injection_rolls_back_every_bootstrap_phase(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path)

    class InjectedFailure(RuntimeError):
        pass

    def inject(current_phase: str) -> None:
        if current_phase == phase:
            raise InjectedFailure("injected")

    monkeypatch.setattr(bootstrap, "_metadata_bootstrap_checkpoint", inject)
    with pytest.raises(InjectedFailure, match="injected"):
        bootstrap.connect_metadata_index()

    observer = sqlite3.connect(path)
    try:
        assert observer.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall() == []
    finally:
        observer.close()
    _assert_settled(path)

    monkeypatch.setattr(
        bootstrap,
        "_metadata_bootstrap_checkpoint",
        lambda _phase: None,
    )
    with bootstrap.connect_metadata_index():
        pass
    _assert_current_schema(path)
    _assert_settled(path)


@pytest.mark.parametrize(
    "case",
    (
        "one_table",
        "several_tables",
        "seven_tables",
        "wrong_table",
        "wrong_column",
        "wrong_index",
        "unexpected_table",
    ),
)
def test_existing_noncanonical_schema_fails_closed_without_repair(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path)
    from app.db import sqlite_integrity as integrity

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        if case in {"one_table", "several_tables", "seven_tables"}:
            count = {
                "one_table": 1,
                "several_tables": 4,
                "seven_tables": 7,
            }[case]
            for statement in bootstrap._METADATA_SCHEMA_STATEMENTS[:count]:
                connection.execute(statement)
        elif case == "wrong_table":
            connection.execute("CREATE TABLE wrong_metadata_table (item TEXT)")
        else:
            connection.executescript(bootstrap.SCHEMA_SQL)
            if case == "wrong_column":
                connection.execute(
                    "ALTER TABLE runs RENAME COLUMN topic TO wrong_topic"
                )
            elif case == "wrong_index":
                connection.execute("DROP INDEX idx_runs_topic")
            elif case == "unexpected_table":
                connection.execute("DROP TABLE ai_review_drafts")
                connection.execute(
                    "CREATE TABLE unexpected_metadata_table (item TEXT)"
                )
        connection.commit()
    finally:
        connection.close()

    before = (path.stat().st_size, _sha256(path), _schema_snapshot(path))
    with pytest.raises(integrity.SqliteSchemaMismatch) as exc_info:
        bootstrap.connect_metadata_index()
    assert str(exc_info.value) == "sqlite_schema_mismatch: metadata"
    assert (path.stat().st_size, _sha256(path), _schema_snapshot(path)) == before
    assert not (path.parent / ".sqlite-fk-backups").exists()
    _assert_settled(path)


def test_complete_legacy_metadata_schema_remains_explicit_migration_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path)
    from app.db import sqlite_integrity as integrity

    legacy_schema, count = re.subn(
        r",\s*FOREIGN KEY \([^)]*\) REFERENCES [^(]+\([^)]*\)"
        r"\s*ON DELETE CASCADE\s*ON UPDATE NO ACTION",
        "",
        bootstrap.SCHEMA_SQL,
        flags=re.IGNORECASE,
    )
    assert count == 4
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(legacy_schema)
    finally:
        connection.close()
    before = (path.stat().st_size, _sha256(path), _schema_snapshot(path))

    read_only = bootstrap.connect_metadata_index(read_only=True)
    try:
        inventory = integrity.inspect_required_foreign_keys(
            read_only,
            store=integrity.STORE_METADATA,
        )
    finally:
        read_only.close()
    assert inventory.status == integrity.STATUS_MIGRATION_REQUIRED
    assert inventory.declared_count == 0

    with pytest.raises(integrity.SqliteForeignKeyMigrationRequired) as exc_info:
        bootstrap.connect_metadata_index()
    assert str(exc_info.value) == "sqlite_fk_migration_required: metadata"
    assert (path.stat().st_size, _sha256(path), _schema_snapshot(path)) == before
    assert not (path.parent / ".sqlite-fk-backups").exists()
    _assert_settled(path)


def test_ready_schema_concurrent_connects_use_fast_path_and_preserve_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(bootstrap.SCHEMA_SQL)
    finally:
        connection.close()
    before = (path.stat().st_size, _sha256(path))

    def forbid_bootstrap(_phase: str) -> None:
        raise AssertionError("ready schema entered bootstrap slow path")

    monkeypatch.setattr(
        bootstrap,
        "_metadata_bootstrap_checkpoint",
        forbid_bootstrap,
    )

    def connect_ready(_token: int) -> str:
        with bootstrap.connect_metadata_index() as ready:
            assert ready.execute("SELECT COUNT(*) FROM index_state").fetchone()[0] == 0
        return "ok"

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert list(pool.map(connect_ready, range(10))) == ["ok"] * 10

    assert (path.stat().st_size, _sha256(path)) == before
    with bootstrap.connect_metadata_index() as writable:
        writable.execute(
            "INSERT INTO index_state (key, value) VALUES ('ordinary', 'write')"
        )
    observer = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert observer.execute(
            "SELECT value FROM index_state WHERE key = 'ordinary'"
        ).fetchone()[0] == "write"
    finally:
        observer.close()
    _assert_settled(path)


def test_ready_connection_does_not_repeat_fk_inventory_or_cache_across_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, path = _bind_metadata_root(monkeypatch, tmp_path / "ready-inventory")
    with bootstrap.connect_metadata_index() as connection:
        connection.execute("CREATE TABLE local_extension (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO index_state VALUES ('preserved', '한글 é')")
    before = _sha256(path)
    statements: list[str] = []
    original_open = bootstrap.open_sqlite_read_write

    def traced_open(*args, **kwargs):
        connection = original_open(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(bootstrap, "open_sqlite_read_write", traced_open)
    with bootstrap.connect_metadata_index() as connection:
        assert connection.execute("SELECT value FROM index_state").fetchone()[0] == "한글 é"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    # Profiled repeated SQL, rather than a wall-clock threshold: no timing
    # relaxation or internal helper-call mock can satisfy this connection cost.
    inventory = [sql for sql in statements if sql.startswith("PRAGMA foreign_key_list(")]
    assert inventory and len(inventory) == len(set(inventory)), inventory
    assert any('"local_extension"' in sql for sql in inventory)
    assert _sha256(path) == before

    # A prior valid result must never authorize a newly altered schema. This
    # extension adds an unexpected FK while the canonical eight tables stay intact.
    with closing(sqlite3.connect(path)) as writer, writer:
        writer.execute("CREATE TABLE invalid_extension (run_id TEXT REFERENCES runs(run_id))")
    changed = _sha256(path)
    with pytest.raises(bootstrap.SqliteSchemaMismatch):
        bootstrap.connect_metadata_index()
    assert _sha256(path) == changed
    _assert_settled(path)


def test_metadata_bootstrap_source_has_no_unprotected_executescript() -> None:
    import inspect

    from app.db import bootstrap

    connect_source = inspect.getsource(bootstrap.connect_metadata_index)
    initialize_source = inspect.getsource(bootstrap._initialize_metadata_schema)
    assert "executescript" not in connect_source
    assert "BEGIN EXCLUSIVE" in initialize_source
    assert "connection.execute(statement)" in initialize_source
    assert "connection.commit()" in initialize_source
    assert len(bootstrap._METADATA_SCHEMA_STATEMENTS) == 16

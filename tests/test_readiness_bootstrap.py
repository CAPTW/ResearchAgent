"""Side-effect-free system probes and explicit runtime bootstrap.

Every fixture uses an OS temp data root and synthetic SQLite files. No canonical
``storage/**`` content, provider, OAuth/OIDC, MCP connector, or network is used.
The autouse conftest isolation already redirects the storage roots per test; the
helpers below additionally point the module-level roots at a fresh temp tree so
"missing store" can be observed without a bootstrap having run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import bootstrap
from app.domain import constants
from app.ops import runtime as ops_runtime
from app.ops import runtime_stores
from app.repositories import metadata_index
from app.services import topic_notifications, topic_schedules


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.fixture()
def empty_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pristine data root with nothing bootstrapped in it."""
    root = tmp_path / "empty_data_root"
    root.mkdir()
    storage_root = root / "runs"
    monkeypatch.setattr(constants, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(ops_runtime.constants, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", root / "topic_schedules")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", root / "topic_schedules" / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", root / "topic_schedules" / "worker_status.json")
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_LIBRARY_DIR", root / "topic_notifications")
    monkeypatch.setattr(
        topic_notifications, "NOTIFICATION_DELIVERIES_PATH", root / "topic_notifications" / "deliveries.json"
    )
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_EVENTS_PATH", root / "topic_notifications" / "notifications.json")
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_SETTINGS_PATH", root / "topic_notifications" / "settings.json")
    return root


# ---------------------------------------------------------------------------
# pure inspection: creates nothing
# ---------------------------------------------------------------------------


def test_inspect_runtime_stores_creates_nothing(empty_root: Path) -> None:
    before = _tree(empty_root)
    result = runtime_stores.inspect_runtime_stores()
    assert result["ready"] is False
    assert result["write_probed"] is False
    assert _tree(empty_root) == before == set()


def test_missing_metadata_store_is_reported_not_created(empty_root: Path) -> None:
    status = runtime_stores.inspect_metadata_store()
    assert status.status == runtime_stores.STATUS_MISSING
    assert status.reason == runtime_stores.REASON_METADATA_MISSING
    assert status.exists is False
    assert _tree(empty_root) == set()


def test_missing_scheduler_store_is_reported_not_created(empty_root: Path) -> None:
    status = runtime_stores.inspect_scheduler_store()
    assert status.status == runtime_stores.STATUS_MISSING
    assert _tree(empty_root) == set()


def test_missing_executor_table_is_reported_not_created(empty_root: Path) -> None:
    status = runtime_stores.inspect_executor_store()
    assert status.status == runtime_stores.STATUS_MISSING
    assert _tree(empty_root) == set()


def test_initialized_stores_report_ready(empty_root: Path) -> None:
    bootstrap.bootstrap_runtime_stores()
    result = runtime_stores.inspect_runtime_stores()
    assert result["ready"] is True
    assert result["stores"]["metadata_index"]["status"] == runtime_stores.STATUS_READY
    assert result["stores"]["scheduler"]["status"] == runtime_stores.STATUS_READY
    assert result["stores"]["run_executor"]["status"] == runtime_stores.STATUS_READY


def test_repeated_inspection_does_not_mutate_stores(empty_root: Path) -> None:
    import hashlib

    bootstrap.bootstrap_runtime_stores()

    def fingerprint() -> dict[str, tuple[int, str]]:
        return {
            path.name: (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(empty_root.iterdir())
            if path.is_file()
        }

    runtime_stores.inspect_runtime_stores()
    before = fingerprint()
    for _ in range(5):
        runtime_stores.inspect_runtime_stores()
    # Byte-exact: repeated inspection changes no file and adds no sidecar.
    assert fingerprint() == before


def test_uninitialized_empty_db_file_is_not_migrated(empty_root: Path) -> None:
    target = empty_root / "scheduler.sqlite3"
    sqlite3.connect(str(target)).close()  # empty DB, no tables
    size_before = target.stat().st_size
    status = runtime_stores.inspect_scheduler_store()
    assert status.status == runtime_stores.STATUS_UNINITIALIZED
    assert status.schema_compatible is False
    assert target.stat().st_size == size_before  # no migration happened


def test_partial_schema_reports_schema_mismatch_without_repair(empty_root: Path) -> None:
    target = empty_root / "scheduler.sqlite3"
    connection = sqlite3.connect(str(target))
    connection.execute("CREATE TABLE scheduler_schedules (schedule_id TEXT)")
    connection.commit()
    connection.close()
    status = runtime_stores.inspect_scheduler_store()
    assert status.status == runtime_stores.STATUS_SCHEMA_MISMATCH
    assert "scheduler_queue_items" in status.missing_tables
    # Still missing after inspection: nothing was repaired.
    connection = sqlite3.connect(str(target))
    tables = bootstrap.sqlite_tables(connection)
    connection.close()
    assert "scheduler_queue_items" not in tables


def test_malformed_sqlite_file_is_redacted(empty_root: Path) -> None:
    target = empty_root / "scheduler.sqlite3"
    target.write_bytes(b"this is definitely not a sqlite database")
    status = runtime_stores.inspect_scheduler_store()
    assert status.status in {runtime_stores.STATUS_MALFORMED, runtime_stores.STATUS_UNREADABLE}
    payload = status.to_payload()
    blob = repr(payload)
    assert "not a sqlite" not in blob
    assert str(target) not in blob
    assert "sqlite3" not in payload.get("reason", "")


def test_inspection_status_payload_leaks_no_path_or_sql(empty_root: Path) -> None:
    bootstrap.bootstrap_runtime_stores()
    blob = repr(runtime_stores.inspect_runtime_stores())
    assert str(empty_root) not in blob
    assert ".sqlite3" not in blob
    assert "SELECT" not in blob and "CREATE" not in blob


def test_inspection_uses_no_write_capable_connection(empty_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap.bootstrap_runtime_stores()
    calls: list[str] = []
    real_scheduler = bootstrap.connect_scheduler_db

    def guard_scheduler(*a, **k):
        calls.append("connect_scheduler_db")
        return real_scheduler(*a, **k)

    monkeypatch.setattr(bootstrap, "connect_scheduler_db", guard_scheduler)
    monkeypatch.setattr(runtime_stores, "connect_metadata_index", _forbid_write_metadata)
    runtime_stores.inspect_scheduler_store()
    runtime_stores.inspect_executor_store()
    assert calls == []


def _forbid_write_metadata(*, read_only: bool = False):
    assert read_only is True, "inspection must use the read-only metadata connection"
    return bootstrap.connect_metadata_index(read_only=True)


def test_read_only_connection_rejects_writes(empty_root: Path) -> None:
    bootstrap.bootstrap_runtime_stores()
    connection = bootstrap.connect_scheduler_db_read_only()
    try:
        with pytest.raises(sqlite3.Error):
            connection.execute("CREATE TABLE should_not_exist (x TEXT)")
    finally:
        connection.close()


def test_read_only_connection_missing_file_raises_not_creates(empty_root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        bootstrap.connect_scheduler_db_read_only()
    assert not (empty_root / "scheduler.sqlite3").exists()


# ---------------------------------------------------------------------------
# storage observation: no probe files
# ---------------------------------------------------------------------------


def test_observe_only_storage_status_creates_no_probe_file(empty_root: Path) -> None:
    status = ops_runtime._storage_status(observe_only=True)
    assert status["writable"] is None  # not probed, not guessed
    assert _tree(empty_root) == set()


def test_observe_only_deployment_status_makes_no_writes(empty_root: Path) -> None:
    deployment = ops_runtime.build_deployment_status(
        provider={}, scheduler={}, notification_delivery={}, observe_only=True
    )
    assert deployment["storage"]["writable"] is None
    assert _tree(empty_root) == set()
    # Not-probed must not be reported as a storage failure.
    joined = " ".join(deployment.get("errors", []))
    assert "not writable" not in joined


def test_write_probe_still_available_for_explicit_preflight(empty_root: Path) -> None:
    status = ops_runtime._storage_status(observe_only=False)
    assert status["writable"] is True
    # The probe cleans up after itself.
    leftovers = [p.name for p in empty_root.rglob(".xrml-storage-probe-*")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# metadata observe-only read
# ---------------------------------------------------------------------------


def test_get_index_state_observe_only_does_not_create_db(empty_root: Path) -> None:
    assert metadata_index.get_index_state(observe_only=True) == {}
    assert not (empty_root / "metadata_index.sqlite3").exists()


def test_get_index_state_observe_only_reads_initialized_db(empty_root: Path) -> None:
    bootstrap.bootstrap_runtime_stores()
    metadata_index.touch_index_state("probe_key", "probe_value")
    assert metadata_index.get_index_state(observe_only=True)["probe_key"] == "probe_value"


# ---------------------------------------------------------------------------
# status chains used by probes
# ---------------------------------------------------------------------------


def test_scheduler_runtime_status_creates_nothing(empty_root: Path) -> None:
    topic_schedules.get_scheduler_runtime_status()
    assert _tree(empty_root) == set()


def test_notification_health_summary_creates_nothing(empty_root: Path) -> None:
    topic_notifications.notification_delivery_health_summary()
    assert _tree(empty_root) == set()


def test_default_file_backed_status_never_reaches_db_availability_check(
    empty_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default single-node file-backed mode must not consult the DB backend.

    db_backed/queue_backed availability legitimately opens a write-capable
    connection, so the guarantee for probes is that the default mode never
    reaches it (and therefore never creates the scheduler store).
    """
    monkeypatch.delenv("SCHEDULER_MODE", raising=False)
    monkeypatch.delenv("TOPIC_SCHEDULER_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("TOPIC_SCHEDULER_BACKEND", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        topic_schedules,
        "_db_scheduler_unavailable_reason",
        lambda: called.append("db_check") or None,
    )
    topic_schedules.get_scheduler_runtime_status()
    assert called == []
    assert _tree(empty_root) == set()


# ---------------------------------------------------------------------------
# liveness / readiness endpoints
# ---------------------------------------------------------------------------


def test_liveness_is_inert(empty_root: Path) -> None:
    from app.api.health import get_health

    assert get_health() == {"status": "ok"}
    assert _tree(empty_root) == set()


def test_readiness_on_missing_stores_is_truthful_and_creates_nothing(empty_root: Path) -> None:
    from app.api.health import get_readiness

    payload = get_readiness()
    stores = payload["runtime_stores"]
    assert stores["ready"] is False
    assert stores["stores"]["metadata_index"]["status"] == runtime_stores.STATUS_MISSING
    assert payload["metadata_index_state"] == {}
    assert _tree(empty_root) == set()


def test_missing_stores_block_top_level_readiness_without_writes(empty_root: Path) -> None:
    from app.api.health import get_readiness

    assert get_readiness()["status"] == "not_ready"
    assert _tree(empty_root) == set()


@pytest.mark.parametrize("existing_store", [False, True])
def test_failed_app_bootstrap_is_not_ready_even_if_stores_are_readable(
    empty_root: Path, monkeypatch: pytest.MonkeyPatch, existing_store: bool,
) -> None:
    from fastapi.testclient import TestClient
    from app import main

    if existing_store:
        bootstrap.bootstrap_runtime_stores()
    order: list[str] = []

    def failed_bootstrap():
        raise PermissionError("secret-token at D:/private/research.sqlite3")

    monkeypatch.setattr(main, "bootstrap_runtime_stores", failed_bootstrap)
    monkeypatch.setattr(main, "build_run_executor_worker", lambda: _FakeWorker(order, name="executor"))
    monkeypatch.setattr(main, "build_topic_schedule_worker", lambda: _FakeWorker(order, name="scheduler"))
    application = main.create_app()
    with TestClient(application) as client:
        assert client.get("/health").json() == {"status": "ok"}
        # Client flags cannot turn the server's failed bootstrap into success.
        response = client.get("/health/ready?bootstrap_ok=true")
        assert response.status_code == 200  # Existing diagnostic envelope contract.
        payload = response.json()
        assert payload["status"] == "not_ready"
        assert payload["runtime_bootstrap"] == {"status": "failed"}
        assert payload["runtime_stores"]["ready"] is existing_store
        assert "secret-token" not in response.text and "D:/private" not in response.text
    assert not any(item.endswith(":start") for item in order)


def test_repeated_readiness_after_bootstrap_makes_no_writes(empty_root: Path) -> None:
    from app.api.health import get_readiness

    import hashlib

    bootstrap.bootstrap_runtime_stores()

    def fingerprint() -> dict[str, tuple[int, str]]:
        return {
            path.name: (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(empty_root.iterdir())
            if path.is_file()
        }

    # Take the baseline after one readiness call so any lingering WAL
    # checkpoint from bootstrap itself is already settled; the invariant under
    # test is that readiness never changes bytes.
    assert get_readiness()["runtime_stores"]["ready"] is True
    before = fingerprint()
    for _ in range(3):
        assert get_readiness()["runtime_stores"]["ready"] is True
    assert fingerprint() == before


def test_readiness_does_not_start_worker_or_claim_jobs(empty_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.health import get_readiness
    from app.repositories import run_jobs
    from app.services import run_executor

    bootstrap.bootstrap_runtime_stores()
    events: list[str] = []
    monkeypatch.setattr(run_jobs, "claim_next_job", lambda **k: events.append("claim"))
    monkeypatch.setattr(run_jobs, "recover_expired_job", lambda *a, **k: events.append("recover"))
    monkeypatch.setattr(run_executor, "reconcile", lambda **k: events.append("reconcile"))
    monkeypatch.setattr(run_executor.RunExecutorWorker, "start", lambda self: events.append("start"))
    get_readiness()
    assert events == []


def test_readiness_leaks_no_path_or_sql(empty_root: Path) -> None:
    from app.api.health import get_readiness

    bootstrap.bootstrap_runtime_stores()
    blob = repr(get_readiness()["runtime_stores"])
    assert str(empty_root) not in blob
    assert "SELECT" not in blob


# ---------------------------------------------------------------------------
# explicit bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_creates_required_schema(empty_root: Path) -> None:
    result = bootstrap.bootstrap_runtime_stores()
    assert result["metadata_index"] == "bootstrap_completed"
    assert result["run_executor"] == "bootstrap_completed"
    connection = bootstrap.connect_scheduler_db_read_only()
    try:
        tables = bootstrap.sqlite_tables(connection)
    finally:
        connection.close()
    assert {"scheduler_schedules", "scheduler_queue_items", "run_jobs"} <= tables


def test_bootstrap_is_idempotent(empty_root: Path) -> None:
    bootstrap.bootstrap_runtime_stores()
    db = bootstrap.scheduler_db_path(ensure_parent=False)
    first = db.stat().st_size
    bootstrap.bootstrap_runtime_stores()
    bootstrap.bootstrap_runtime_stores()
    assert db.stat().st_size == first
    assert runtime_stores.inspect_runtime_stores()["ready"] is True


def test_bootstrap_status_has_no_path_or_secret(empty_root: Path) -> None:
    blob = repr(bootstrap.bootstrap_runtime_stores())
    assert str(empty_root) not in blob
    assert ".sqlite3" not in blob


# ---------------------------------------------------------------------------
# lifespan ordering: bootstrap before worker
# ---------------------------------------------------------------------------


def test_app_factory_and_openapi_make_no_writes(empty_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    started: list[str] = []
    monkeypatch.setattr(
        main_module, "build_run_executor_worker", lambda: _FakeWorker(started, name="executor")
    )
    application = main_module.create_app()
    application.openapi()
    assert _tree(empty_root) == set()
    assert started == []  # factory must not start a worker


class _FakeWorker:
    def __init__(self, log: list[str], *, name: str) -> None:
        self._log = log
        self._name = name

    def start(self) -> None:
        self._log.append(f"{self._name}:start")

    def stop(self) -> None:
        self._log.append(f"{self._name}:stop")

    def snapshot(self) -> dict[str, object]:
        return {"mode": "embedded", "enabled": True, "running": False}


def _run_lifespan(main_module, monkeypatch: pytest.MonkeyPatch, started: list[str], *, fail: bool):
    order: list[str] = []

    def bootstrap_hook() -> dict[str, str]:
        order.append("bootstrap")
        if fail:
            raise sqlite3.OperationalError("synthetic bootstrap failure")
        return {"metadata_index": "bootstrap_completed"}

    monkeypatch.setattr(main_module, "bootstrap_runtime_stores", bootstrap_hook)
    monkeypatch.setattr(main_module, "build_run_executor_worker", lambda: _FakeWorker(order, name="executor"))
    monkeypatch.setattr(main_module, "build_topic_schedule_worker", lambda: _FakeWorker(order, name="scheduler"))
    application = main_module.create_app()
    from fastapi.testclient import TestClient

    with TestClient(application) as client:
        client.get("/health")
    started.extend(order)
    return application


def test_worker_starts_only_after_successful_bootstrap(
    empty_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    order: list[str] = []
    _run_lifespan(main_module, monkeypatch, order, fail=False)
    assert "bootstrap" in order
    assert "executor:start" in order
    # Ordering: bootstrap strictly precedes the durable worker start.
    assert order.index("bootstrap") < order.index("executor:start")


def test_failed_bootstrap_starts_no_executor_worker(
    empty_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    order: list[str] = []
    _run_lifespan(main_module, monkeypatch, order, fail=True)
    assert "bootstrap" in order
    assert "executor:start" not in order  # never started after failure


def test_failed_bootstrap_is_recorded_without_leaking_error(
    empty_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    order: list[str] = []
    application = _run_lifespan(main_module, monkeypatch, order, fail=True)
    assert application.state.runtime_bootstrap_ok is False
    blob = repr(application.state.runtime_bootstrap)
    assert "synthetic bootstrap failure" not in blob
    assert str(empty_root) not in blob

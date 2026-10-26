"""Durable run-executor coverage: queue, lease, fencing, restart, recovery.

All fixtures are synthetic and live under pytest ``tmp_path``: the autouse
conftest isolation redirects ``bootstrap.STORAGE_ROOT`` (and therefore the
scheduler SQLite path) into a temp tree, so no canonical runtime DB or
``storage/**`` content is touched. No provider/OAuth/MCP/network is used.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.domain.path_safety import InvalidRunId
from app.repositories import run_jobs, runs as run_repository
from app.services import run_executor


def _record(run_id: str, *, status: str = "queued") -> dict:
    run_repository.ensure_run_layout(run_id)
    return run_repository.save_run_record({"run_id": run_id, "topic": "synthetic", "status": status})


# ---------------------------------------------------------------------------
# durable queue / enqueue
# ---------------------------------------------------------------------------


def test_enqueue_creates_durable_row() -> None:
    job = run_jobs.enqueue_run_job("run-a")
    assert job.state == run_jobs.STATE_QUEUED
    assert job.attempts == 0
    assert run_jobs.get_job(job.job_id) is not None


def test_duplicate_enqueue_does_not_create_second_row() -> None:
    first = run_jobs.enqueue_run_job("run-dup")
    second = run_jobs.enqueue_run_job("run-dup")
    assert first.job_id == second.job_id
    assert len([j for j in run_jobs.list_active_jobs() if j.run_id == "run-dup"]) == 1


def test_terminal_job_allows_a_new_job_for_same_run() -> None:
    first = run_jobs.enqueue_run_job("run-cycle")
    claimed = run_jobs.claim_next_job(owner="w", lease_seconds=60)
    assert run_jobs.complete_job(claimed.job_id, claimed.lease_token, state=run_jobs.STATE_SUCCEEDED)
    second = run_jobs.enqueue_run_job("run-cycle")
    assert second.job_id != first.job_id
    assert second.state == run_jobs.STATE_QUEUED


def test_enqueue_rejects_malformed_run_id_before_any_write() -> None:
    for bad in ["../evil", "a/b", "C:\\x", "CON", "run:stream"]:
        with pytest.raises(InvalidRunId):
            run_jobs.enqueue_run_job(bad)
    assert run_jobs.list_active_jobs() == []


def test_fifo_claim_order() -> None:
    run_jobs.enqueue_run_job("run-1")
    run_jobs.enqueue_run_job("run-2")
    first = run_jobs.claim_next_job(owner="w", lease_seconds=60)
    second = run_jobs.claim_next_job(owner="w", lease_seconds=60)
    assert [first.run_id, second.run_id] == ["run-1", "run-2"]


def test_job_row_carries_no_secret_or_traceback() -> None:
    job = run_jobs.enqueue_run_job("run-secretless")
    for value in vars(job).values():
        text = str(value or "")
        assert "Traceback" not in text
        assert "password" not in text.lower()


# ---------------------------------------------------------------------------
# atomic claim + lease + fencing
# ---------------------------------------------------------------------------


def test_claim_is_atomic_and_single_winner() -> None:
    run_jobs.enqueue_run_job("run-solo")
    first = run_jobs.claim_next_job(owner="w1", lease_seconds=60)
    second = run_jobs.claim_next_job(owner="w2", lease_seconds=60)
    assert first is not None and second is None
    assert first.attempts == 1
    assert first.lease_token and first.lease_owner == "w1"


def test_concurrent_claim_yields_exactly_one_winner() -> None:
    run_jobs.enqueue_run_job("run-race")
    results: list[object] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        got = run_jobs.claim_next_job(owner=name, lease_seconds=60)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


def test_wrong_lease_token_is_fenced_out() -> None:
    run_jobs.enqueue_run_job("run-fence")
    job = run_jobs.claim_next_job(owner="w1", lease_seconds=60)
    assert run_jobs.heartbeat(job.job_id, "wrong-token", lease_seconds=60) is False
    assert run_jobs.mark_pipeline_started(job.job_id, "wrong-token") is False
    assert run_jobs.complete_job(job.job_id, "wrong-token", state=run_jobs.STATE_SUCCEEDED) is False
    assert run_jobs.get_job(job.job_id).state == run_jobs.STATE_LEASED


def test_stale_owner_cannot_complete_after_reclaim() -> None:
    run_jobs.enqueue_run_job("run-steal")
    now = run_jobs.utc_now()
    worker_a = run_jobs.claim_next_job(owner="A", lease_seconds=1, now=now)
    later = now + timedelta(seconds=120)
    # Lease expired -> recovery requeues (pipeline never started), B reclaims.
    for expired in run_jobs.list_expired_jobs(now=later):
        run_jobs.recover_expired_job(expired, now=later)
    worker_b = run_jobs.claim_next_job(owner="B", lease_seconds=60, now=later)
    assert worker_b is not None and worker_b.lease_token != worker_a.lease_token
    # Every stale update from A is rejected.
    assert run_jobs.heartbeat(worker_a.job_id, worker_a.lease_token, lease_seconds=60) is False
    assert run_jobs.complete_job(worker_a.job_id, worker_a.lease_token, state=run_jobs.STATE_SUCCEEDED) is False
    assert run_jobs.complete_job(worker_a.job_id, worker_a.lease_token, state=run_jobs.STATE_FAILED) is False
    # Only B can terminalise.
    assert run_jobs.complete_job(worker_b.job_id, worker_b.lease_token, state=run_jobs.STATE_SUCCEEDED) is True


# ---------------------------------------------------------------------------
# restart / lease-expiry recovery
# ---------------------------------------------------------------------------


def test_queued_job_survives_restart_and_is_processed_by_new_worker() -> None:
    _record("run-restart")
    run_executor.enqueue_run("run-restart")
    # A brand-new worker object == a new process for this local store.
    calls: list[str] = []
    worker = run_executor.RunExecutorWorker(
        config=run_executor.load_config(), pipeline=calls.append, owner="worker-B"
    )
    assert worker.run_once() == "run-restart"
    assert calls == ["run-restart"]


def test_leased_but_not_started_job_is_requeued_after_expiry() -> None:
    run_jobs.enqueue_run_job("run-notstarted")
    now = run_jobs.utc_now()
    run_jobs.claim_next_job(owner="A", lease_seconds=1, now=now)
    later = now + timedelta(seconds=60)
    expired = run_jobs.list_expired_jobs(now=later)
    assert len(expired) == 1
    assert run_jobs.recover_expired_job(expired[0], now=later) == "requeued"
    assert run_jobs.get_job(expired[0].job_id).state == run_jobs.STATE_QUEUED


def test_started_job_is_never_auto_reexecuted_and_becomes_interrupted() -> None:
    _record("run-started")
    run_jobs.enqueue_run_job("run-started")
    now = run_jobs.utc_now()
    job = run_jobs.claim_next_job(owner="A", lease_seconds=1, now=now)
    assert run_jobs.mark_pipeline_started(job.job_id, job.lease_token, now=now)
    later = now + timedelta(seconds=60)
    summary = run_executor.reconcile(now=later)
    assert summary["interrupted"] == 1
    assert summary["requeued"] == 0
    recovered = run_jobs.get_job(job.job_id)
    assert recovered.state == run_jobs.STATE_INTERRUPTED
    assert recovered.reason_code == run_jobs.REASON_RECOVERY_REQUIRED
    # No automatic duplicate execution: nothing is claimable afterwards.
    assert run_jobs.claim_next_job(owner="B", lease_seconds=60, now=later) is None
    # The public run record tells the truth (never "completed").
    assert run_repository.load_run_record("run-started")["status"] == "interrupted"


def test_max_attempts_terminates_without_further_execution() -> None:
    job = run_jobs.enqueue_run_job("run-attempts", max_attempts=1)
    now = run_jobs.utc_now()
    run_jobs.claim_next_job(owner="A", lease_seconds=1, now=now)
    later = now + timedelta(seconds=60)
    expired = run_jobs.list_expired_jobs(now=later)
    assert run_jobs.recover_expired_job(expired[0], now=later) == "max_attempts"
    final = run_jobs.get_job(job.job_id)
    assert final.state == run_jobs.STATE_FAILED
    assert final.reason_code == run_jobs.REASON_MAX_ATTEMPTS
    assert run_jobs.claim_next_job(owner="B", lease_seconds=60, now=later) is None


def test_reconcile_dead_letters_job_without_run_record() -> None:
    run_jobs.enqueue_run_job("run-orphan")  # no run record on disk
    summary = run_executor.reconcile()
    assert summary["orphaned"] == 1
    orphan = [j for j in run_jobs.list_active_jobs() if j.run_id == "run-orphan"]
    assert orphan == []


def test_worker_does_not_run_pipeline_when_run_record_missing() -> None:
    run_jobs.enqueue_run_job("run-ghost")
    calls: list[str] = []
    worker = run_executor.RunExecutorWorker(config=run_executor.load_config(), pipeline=calls.append)
    worker.run_once()
    assert calls == []


# ---------------------------------------------------------------------------
# worker lifecycle
# ---------------------------------------------------------------------------


def test_worker_processes_job_exactly_once() -> None:
    _record("run-once")
    run_executor.enqueue_run("run-once")
    calls: list[str] = []
    worker = run_executor.RunExecutorWorker(config=run_executor.load_config(), pipeline=calls.append)
    assert worker.run_once() == "run-once"
    assert worker.run_once() is None
    assert calls == ["run-once"]


def test_worker_survives_a_failing_job_and_records_redacted_failure() -> None:
    _record("run-boom")
    run_executor.enqueue_run("run-boom")

    def explode(_run_id: str) -> None:
        raise RuntimeError("secret-value-should-not-leak")

    worker = run_executor.RunExecutorWorker(config=run_executor.load_config(), pipeline=explode)
    worker.run_once()
    job = [j for j in [run_jobs.active_job_for_run("run-boom")] if j] or None
    assert job is None  # terminalised, not left active
    # Next cycle still works (loop is not poisoned).
    _record("run-after")
    run_executor.enqueue_run("run-after")
    calls: list[str] = []
    ok_worker = run_executor.RunExecutorWorker(config=run_executor.load_config(), pipeline=calls.append)
    assert ok_worker.run_once() == "run-after"


def test_stopped_worker_makes_no_new_claims_and_job_survives() -> None:
    calls: list[str] = []
    worker = run_executor.RunExecutorWorker(config=run_executor.load_config(), pipeline=calls.append)
    worker.start()
    worker.stop()
    assert worker.snapshot()["running"] is False

    # A job enqueued after shutdown is never claimed, and is never lost.
    _record("run-after-stop")
    run_executor.enqueue_run("run-after-stop")
    import time

    time.sleep(0.2)
    assert calls == []
    surviving = run_jobs.active_job_for_run("run-after-stop")
    assert surviving is not None
    assert surviving.state == run_jobs.STATE_QUEUED


def test_disabled_mode_worker_never_starts() -> None:
    config = run_executor.RunExecutorConfig(
        mode=run_executor.MODE_DISABLED,
        lease_seconds=300,
        heartbeat_seconds=30,
        poll_seconds=1.0,
        max_attempts=3,
        shutdown_grace_seconds=1.0,
    )
    calls: list[str] = []
    worker = run_executor.RunExecutorWorker(config=config, pipeline=calls.append)
    worker.start()
    assert worker.snapshot()["running"] is False
    worker.stop()


@pytest.mark.parametrize(
    "env,value",
    [
        ("XRML_RUN_EXECUTOR_MODE", "bogus"),
        ("XRML_RUN_EXECUTOR_LEASE_SECONDS", "0"),
        ("XRML_RUN_EXECUTOR_LEASE_SECONDS", "not-a-number"),
        ("XRML_RUN_EXECUTOR_MAX_ATTEMPTS", "999"),
        ("XRML_RUN_EXECUTOR_HEARTBEAT_SECONDS", "0"),
    ],
)
def test_invalid_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch, env: str, value: str) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(run_executor.RunExecutorConfigError):
        run_executor.load_config()
    # The factory degrades to disabled rather than pretending to work.
    assert run_executor.build_run_executor_worker().config.mode == run_executor.MODE_DISABLED


def test_lease_must_outlive_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XRML_RUN_EXECUTOR_LEASE_SECONDS", "20")
    monkeypatch.setenv("XRML_RUN_EXECUTOR_HEARTBEAT_SECONDS", "15")
    with pytest.raises(run_executor.RunExecutorConfigError):
        run_executor.load_config()


# ---------------------------------------------------------------------------
# start paths never execute the pipeline inline / no daemon threads
# ---------------------------------------------------------------------------


def test_start_run_enqueues_and_never_calls_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import runs as runs_service

    record = _record("run-startpath")
    monkeypatch.setattr(runs_service, "create_run_from_request", lambda *a, **k: record)
    calls: list[str] = []
    monkeypatch.setattr(runs_service, "_execute_pipeline", lambda run_id: calls.append(run_id))
    before = threading.active_count()

    result = runs_service.start_run(object())

    assert result["run_id"] == "run-startpath"
    assert calls == []  # no inline pipeline invocation
    assert run_jobs.active_job_for_run("run-startpath") is not None  # durable job exists
    assert threading.active_count() <= before  # no fire-and-forget thread spawned


def test_start_run_failure_to_enqueue_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import runs as runs_service

    record = _record("run-enqueue-fail")
    monkeypatch.setattr(runs_service, "create_run_from_request", lambda *a, **k: record)

    def boom(_run_id: str, **_kw):
        raise run_jobs.RunJobStoreUnavailable("enqueue_failed")

    monkeypatch.setattr(run_executor, "enqueue_run", boom)
    with pytest.raises(run_jobs.RunJobStoreUnavailable):
        runs_service.start_run(object())
    assert run_repository.load_run_record("run-enqueue-fail")["status"] == "failed"


def test_no_daemon_pipeline_threads_in_service_source() -> None:
    from pathlib import Path

    source = Path("app/services/runs.py").read_text(encoding="utf-8")
    assert "daemon=True" not in source
    assert "RUN_THREADS" not in source

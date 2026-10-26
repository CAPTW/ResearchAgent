from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event

import pytest

from app.domain import journal_execution as domain
from app.repositories import journal_executions as repo
from tests import test_journal_execution as prior

source = prior.source
completed = prior.completed
execution = prior.execution


def worker(claimed, provider, **kwargs):
    from app.services.journal_worker import JournalAttemptWorker
    return JournalAttemptWorker(claimed, provider=provider, model="offline-model", clock=lambda: prior.NOW,
                                shutdown_grace_seconds=0.01, **kwargs)


def test_stop_before_dispatch_is_durable_and_never_calls_adapter(execution):
    fake = prior.Fake(lambda _: pytest.fail("Unstarted worker must not dispatch"))
    value = worker(prior.claim(execution), fake)
    assert value.stop()
    assert not value.start()
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "FAILED" and current["reason"] == "worker_stopped_before_dispatch"
    assert current["outcome_unknown"] is False and fake.calls == 0
    assert value.stop() and repo.detail(*prior.scope(execution))["current"] == current


def test_stop_while_adapter_blocks_keeps_thread_visible_and_fences_late_response(execution):
    entered, release = Event(), Event()
    def generate(_):
        entered.set()
        assert release.wait(10)
        return prior.response(execution)
    fake = prior.Fake(generate)
    claimed = prior.claim(execution)
    value = worker(claimed, fake)
    try:
        assert value.start() and entered.wait(5)
        assert not value.stop()  # Still running, not a pretend physical abort.
        status = value.snapshot()
        assert status["running"] and status["stopping"] and status["closed"]
        assert claimed.token not in str(status)
        assert not value.start()
        current = repo.detail(*prior.scope(execution))["current"]
        assert current["state"] == "INTERRUPTED" and current["outcome_unknown"]
        assert current["reason"] == "worker_stopped_after_dispatch"
        with pytest.raises(ValueError, match="unknown_outcome"):
            repo.claim(*prior.scope(execution), expected_attempt=1, now=prior.NOW)
    finally:
        release.set()
        assert value.wait(5)
    assert fake.calls == 1 and not value.snapshot()["running"]
    assert repo.detail(*prior.scope(execution))["current"] == current
    assert value.stop() and not value.start()


def test_concurrent_start_and_duplicate_worker_dispatch_once(execution):
    fake = prior.Fake(lambda _: prior.response(execution))
    claimed = prior.claim(execution)
    a, b = worker(claimed, fake), worker(claimed, fake)
    gate = Barrier(6)
    def start(i):
        gate.wait()
        return (a if i % 2 else b).start()
    with ThreadPoolExecutor(6) as pool:
        assert sum(pool.map(start, range(6))) == 2  # At most one local thread per owner.
    assert a.wait(5) and b.wait(5)
    assert fake.calls == 1  # Persistent dispatch fence across owners.
    saved = repo.detail(*prior.scope(execution))
    assert saved["current"]["state"] == "SUCCEEDED"
    assert a.stop() and b.stop() and repo.detail(*prior.scope(execution)) == saved


def test_wrong_token_and_old_attempt_cannot_stop_another_worker(execution):
    claimed = prior.claim(execution)
    assert repo.start(claimed, now=prior.NOW)
    before = repo.detail(*prior.scope(execution))
    for stale in [replace(claimed, token="wrong"), replace(claimed, attempt=0)]:
        assert not repo.abandon(stale, reason="shutdown", now=prior.NOW)
    assert repo.detail(*prior.scope(execution)) == before
    assert repo.abandon(claimed, reason="shutdown", now=prior.NOW)
    second = repo.claim(*prior.scope(execution), expected_attempt=1, acknowledge_unknown=True, now=prior.NOW)
    assert not repo.abandon(claimed, reason="shutdown", now=prior.NOW)
    fake = prior.Fake(lambda _: prior.response(execution))
    fresh = worker(second, fake)
    assert fresh.start() and fresh.wait(5)
    assert repo.detail(*prior.scope(execution))["current"]["state"] == "SUCCEEDED"


def test_cancel_and_expiry_remain_authoritative_during_worker_stop(execution):
    claimed = prior.claim(execution, lease_seconds=5)
    assert repo.start(claimed, now=prior.NOW)
    expired = repo.recover(*prior.scope(execution), now=prior.NOW + timedelta(seconds=6))
    assert not repo.abandon(claimed, reason="shutdown", now=prior.NOW)
    assert repo.detail(*prior.scope(execution)) == expired
    cancelled = repo.cancel(*prior.scope(execution), expected_attempt=1, now=prior.NOW)
    value = worker(claimed, prior.Fake(lambda _: pytest.fail("Cancelled")))
    assert value.stop() and repo.detail(*prior.scope(execution)) == cancelled


def test_adapter_failure_is_redacted_and_never_auto_retries(execution):
    def fail(_):
        raise RuntimeError("secret diagnostic")
    fake = prior.Fake(fail)
    value = worker(prior.claim(execution), fake)
    assert value.start() and value.wait(5)
    assert value.snapshot()["error_code"] == "journal_worker_execution_failed"
    assert "secret diagnostic" not in str(value.snapshot())
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "INTERRUPTED" and current["reason"] == "adapter_error"
    assert not value.start() and fake.calls == 1


def test_failure_before_dispatch_is_a_known_unstarted_attempt(execution):
    fake = prior.Fake(lambda _: pytest.fail("mismatched adapter"))
    fake.provider_name = "wrong"
    value = worker(prior.claim(execution), fake)
    assert value.start() and value.wait(5)
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "FAILED" and current["reason"] == "worker_failed_before_dispatch"
    assert current["outcome_unknown"] is False and fake.calls == 0


def test_completion_storage_error_does_not_masquerade_as_success(execution, monkeypatch):
    def unavailable(*_, **__):
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(repo, "finish", unavailable)
    value = worker(prior.claim(execution), prior.Fake(lambda _: prior.response(execution)))
    assert value.start() and value.wait(5)
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "INTERRUPTED" and current["reason"] == "worker_failed_after_dispatch"
    assert current["result"] is None and value.snapshot()["error_code"] == "journal_worker_execution_failed"


def test_shutdown_storage_error_is_explicit_and_can_be_retried(execution, monkeypatch):
    entered, release = Event(), Event()
    def generate(_):
        entered.set()
        assert release.wait(10)
        return prior.response(execution)
    value = worker(prior.claim(execution), prior.Fake(generate))
    try:
        assert value.start() and entered.wait(5)
        original = repo.abandon
        def unavailable(*_, **__):
            raise OSError("store offline")
        monkeypatch.setattr(repo, "abandon", unavailable)
        with pytest.raises(OSError, match="store offline"):
            value.stop()
        assert value.snapshot()["error_code"] == "journal_worker_store_unavailable"
        assert value.snapshot()["running"] and not value.start()
        monkeypatch.setattr(repo, "abandon", original)
        assert not value.stop()
        assert repo.detail(*prior.scope(execution))["current"]["state"] == "INTERRUPTED"
    finally:
        release.set()
        assert value.wait(5)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_start_failure_releases_owned_lease_and_is_not_retryable_implicitly(execution, monkeypatch, phase):
    import threading
    value = worker(prior.claim(execution), prior.Fake(lambda _: pytest.fail("thread failed")))
    def fail(*_, **__):
        raise RuntimeError("thread unavailable")
    if phase == "construct":
        monkeypatch.setattr(threading, "Thread", fail)
    else:
        monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="journal_worker_start_failed"):
        value.start()
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "FAILED" and not current["outcome_unknown"]
    assert not value.start() and value.stop()


def test_adapter_can_request_stop_without_self_join(execution):
    def generate(_):
        assert not value.stop()
        return prior.response(execution)
    value = worker(prior.claim(execution), prior.Fake(generate))
    assert value.start() and value.wait(5)
    assert value.snapshot()["error_code"] is None
    assert repo.detail(*prior.scope(execution))["current"]["state"] == "INTERRUPTED"


def test_abandon_transaction_rollback_and_finish_race(execution, monkeypatch):
    claimed = prior.claim(execution)
    assert repo.start(claimed, now=prior.NOW)
    before = repo.detail(*prior.scope(execution))
    def fail(_):
        raise RuntimeError("before commit")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        repo.abandon(claimed, reason="shutdown", now=prior.NOW)
    assert repo.detail(*prior.scope(execution)) == before
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    gate = Barrier(2)
    def stop():
        gate.wait()
        return repo.abandon(claimed, reason="shutdown", now=prior.NOW)
    def finish():
        gate.wait()
        return repo.finish(claimed, domain.result(execution["contract"], prior.response(execution)), now=prior.NOW)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(stop), pool.submit(finish)
        assert a.result() != b.result()
    assert repo.detail(*prior.scope(execution))["current"]["sequence"] == 3


@pytest.mark.parametrize("graceful", [False, True])
def test_process_exit_during_call_survives_restart_without_hidden_retry(execution, tmp_path, graceful):
    claimed = prior.claim(execution)
    ticket = tmp_path / "claim.json"
    ticket.write_text(json.dumps(asdict(claimed)), encoding="utf-8")
    script = tmp_path / "worker_process.py"
    script.write_text("""import json,os,sys
from pathlib import Path
from threading import Event
from app.repositories.journal_executions import Claim
from app.services.journal_worker import JournalAttemptWorker
from tests.test_journal_execution import NOW,Fake
entered,release=Event(),Event()
def generate(_):
    entered.set()
    release.wait(60)
claimed=Claim(**json.loads(Path(sys.argv[1]).read_text()))
worker=JournalAttemptWorker(claimed,provider=Fake(generate),model='offline-model',clock=lambda:NOW,shutdown_grace_seconds=0.01)
assert worker.start() and entered.wait(5)
if sys.argv[2]=='graceful':
    assert not worker.stop() and worker.snapshot()['running']
    print('fenced-but-still-draining',flush=True)
else:
    os._exit(78)
""", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(ticket), "graceful" if graceful else "crash"],
                          cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, text=True, timeout=20)
    assert proc.returncode == (0 if graceful else 78), proc.stderr
    current = repo.detail(*prior.scope(execution))["current"]
    if not graceful:
        assert current["state"] == "RUNNING"
        current = repo.recover(*prior.scope(execution), now=prior.NOW + timedelta(seconds=121))["current"]
    assert current["state"] == "INTERRUPTED" and current["outcome_unknown"]
    assert current["attempt"] == 1 and current["result"] is None
    assert current["reason"] == ("worker_stopped_after_dispatch" if graceful else "lease_expired_after_dispatch")


@pytest.mark.parametrize("timeout", [-1, 31, float("nan"), float("inf"), True])
def test_invalid_shutdown_bound_is_rejected_before_start(execution, timeout):
    from app.services.journal_worker import JournalAttemptWorker
    with pytest.raises(ValueError, match="timeout_invalid"):
        JournalAttemptWorker(prior.claim(execution), provider=prior.Fake(lambda _: None), model="offline-model", shutdown_grace_seconds=timeout)

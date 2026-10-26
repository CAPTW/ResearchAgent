"""Real managed threads, only synthetic callbacks and task-isolated storage."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest

from app.services import run_executor


def config():
    return replace(run_executor.load_config(), mode=run_executor.MODE_EMBEDDED,
                   poll_seconds=0.05, shutdown_grace_seconds=0.01)


def test_run_worker_shutdown_timeout_keeps_live_thread_and_blocks_restart(monkeypatch):
    entered, release = Event(), Event()
    calls = []
    worker = run_executor.RunExecutorWorker(config=config(), pipeline=lambda _: None)
    def cycle():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return "synthetic"
    monkeypatch.setattr(worker, "run_once", cycle)
    monkeypatch.setattr(run_executor, "reconcile", lambda **_: {})
    try:
        worker.start()
        assert entered.wait(5)
        original = worker._thread
        worker.stop()
        assert worker.snapshot()["running"] is True
        assert worker.snapshot()["stopping"] is True
        worker.start()
        assert worker._thread is original and len(calls) == 1
    finally:
        release.set()
        worker.stop()
        if original is not None:
            original.join(5)
    assert not worker.snapshot()["running"]
    worker.start()
    worker.stop()
    assert not worker.snapshot()["running"]


def test_concurrent_starts_create_only_one_run_worker(monkeypatch):
    gate, release = Barrier(6), Event()
    entered = Event()
    calls = []
    worker = run_executor.RunExecutorWorker(config=config(), pipeline=lambda _: None)
    def cycle():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return None
    monkeypatch.setattr(worker, "run_once", cycle)
    monkeypatch.setattr(run_executor, "reconcile", lambda **_: {})
    def start(_):
        gate.wait()
        worker.start()
    try:
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(start, range(6)))
        assert entered.wait(5)
        assert calls == [1]
    finally:
        release.set()
        worker.stop()
        if worker._thread is not None:
            worker._thread.join(5)


def test_run_worker_can_request_its_own_stop_without_joining_itself(monkeypatch):
    finished = Event()
    worker = run_executor.RunExecutorWorker(config=config(), pipeline=lambda _: None)
    def cycle():
        worker.stop()
        finished.set()
        return None
    monkeypatch.setattr(worker, "run_once", cycle)
    monkeypatch.setattr(run_executor, "reconcile", lambda **_: {})
    worker.start()
    assert finished.wait(5)
    worker.stop()
    assert not worker.snapshot()["running"]


def test_stopping_a_blocked_run_preserves_next_queued_work(monkeypatch):
    from app.repositories import run_jobs
    from tests.test_run_executor import _record
    entered, release = Event(), Event()
    calls = []
    _record("run-draining")
    _record("run-next")
    run_executor.enqueue_run("run-draining")
    run_executor.enqueue_run("run-next")
    def pipeline(run_id):
        calls.append(run_id)
        entered.set()
        assert release.wait(10)
    worker = run_executor.RunExecutorWorker(config=config(), pipeline=pipeline)
    try:
        worker.start()
        assert entered.wait(5)
        worker.stop()
        worker.start()  # Must not clear the stop signal while old pipeline drains.
        assert calls == ["run-draining"]
        release.set()
        assert worker._thread is not None
        worker._thread.join(5)
        worker.stop()
        assert run_jobs.active_job_for_run("run-next").state == run_jobs.STATE_QUEUED
        assert calls == ["run-draining"]
    finally:
        release.set()
        worker.stop()


def test_run_worker_thread_construction_failure_can_be_retried_explicitly(monkeypatch):
    import threading
    worker = run_executor.RunExecutorWorker(config=config(), pipeline=lambda _: None)
    monkeypatch.setattr(run_executor, "reconcile", lambda **_: {})
    monkeypatch.setattr(worker, "run_once", lambda: None)
    def fail(**_):
        raise RuntimeError("thread unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(threading, "Thread", fail)
        with pytest.raises(RuntimeError, match="thread unavailable"):
            worker.start()
    assert not worker.snapshot()["running"]
    worker.start()
    assert worker.snapshot()["running"]
    worker.stop()
    assert not worker.snapshot()["running"]

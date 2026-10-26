"""Real thread/event lifecycle tests with synthetic cycles and heartbeat sinks."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.jobs import topic_schedule_worker
from app.services import topic_scheduler, topic_schedules


@pytest.fixture
def heartbeat_sink(monkeypatch):
    heartbeats = []
    monkeypatch.setenv("XRML_PROTOTYPE_MODE", "0")
    monkeypatch.setattr(topic_schedules, "record_scheduler_heartbeat", lambda **value: heartbeats.append(value))
    monkeypatch.setattr(topic_schedules, "get_scheduler_runtime_status", lambda **_: {})
    return heartbeats


def blocked_worker(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def cycle(**_):
        entered.set()
        assert release.wait(10), "synthetic cycle was not released"
        return {"executed_schedule_ids": [], "errors": []}

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")
    worker.start()
    assert entered.wait(5), "synthetic cycle did not start"
    thread = worker._thread
    assert thread is not None
    return worker, thread, release


def cleanup(worker, release, *threads):
    worker._stop_event.set()
    release.set()
    for thread in threads:
        if thread is not None:
            thread.join(5)
            assert not thread.is_alive(), "test-owned worker did not exit"
    worker.stop()


def test_bounded_stop_retains_active_worker_and_does_not_report_stopped(monkeypatch, heartbeat_sink):
    worker, original, release = blocked_worker(monkeypatch)
    try:
        worker.stop()  # Real unchanged 1.25-second join budget; cycle is blocked.
        assert original.is_alive()
        assert worker._thread is original
        assert worker._stop_event.is_set()
        assert worker.snapshot()["running"] is True
        assert worker.snapshot()["stopping"] is True
        assert not any(not entry["running"] for entry in heartbeat_sink)
        release.set()
        original.join(5)
        assert not original.is_alive()
        assert heartbeat_sink[-1]["running"] is False
    finally:
        cleanup(worker, release, original)


def test_start_cannot_replace_worker_still_draining_after_stop(monkeypatch, heartbeat_sink):
    worker, original, release = blocked_worker(monkeypatch)
    replacement = None
    try:
        worker.stop()
        worker.start()
        replacement = worker._thread
        assert replacement is original
        assert worker._stop_event.is_set()
    finally:
        cleanup(worker, release, original, replacement if replacement is not original else None)


def test_worker_can_request_its_own_stop_and_reports_exit(monkeypatch, heartbeat_sink):
    requested = threading.Event()
    errors = []
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")

    def cycle(**_):
        try:
            worker.stop()
        except RuntimeError as error:
            errors.append(str(error))
        requested.set()
        return {"executed_schedule_ids": [], "errors": []}

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    worker.start()
    original = worker._thread
    assert original is not None
    try:
        assert requested.wait(5)
        original.join(5)
        assert not original.is_alive()
        assert errors == []
        assert heartbeat_sink[-1]["running"] is False
    finally:
        worker.stop()
        original.join(5)


def test_worker_restarts_only_after_previous_cycle_exits(monkeypatch, heartbeat_sink):
    worker, original, release = blocked_worker(monkeypatch)
    replacement = None
    try:
        worker.stop()
        release.set()
        original.join(5)
        assert not original.is_alive()
        assert heartbeat_sink[-1]["running"] is False
        entered = threading.Event()

        def next_cycle(**_):
            assert not original.is_alive()
            entered.set()
            worker.stop()
            return {"executed_schedule_ids": ["synthetic-restart"], "errors": []}

        monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", next_cycle)
        worker.start()
        replacement = worker._thread
        assert replacement is not None and replacement is not original
        assert entered.wait(5)
        replacement.join(5)
        assert not replacement.is_alive()
        assert heartbeat_sink[-1]["running"] is False
        assert heartbeat_sink[-1]["last_cycle_summary"]["executed_schedule_ids"] == ["synthetic-restart"]
    finally:
        cleanup(worker, release, original, replacement)


def test_concurrent_start_calls_own_one_real_worker(monkeypatch, heartbeat_sink):
    entered, release = threading.Event(), threading.Event()
    callers = threading.Barrier(6)
    cycles = []

    def cycle(**_):
        cycles.append(threading.current_thread())
        entered.set()
        assert release.wait(10)
        return {"executed_schedule_ids": [], "errors": []}

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")

    def start(_):
        callers.wait(5)
        worker.start()

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(start, range(6)))
        assert entered.wait(5)
        assert cycles == [worker._thread]
    finally:
        cleanup(worker, release, *cycles)


def test_external_foreground_stop_waits_for_cycle_before_stopped_heartbeat(monkeypatch, heartbeat_sink):
    entered, release = threading.Event(), threading.Event()
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")

    def cycle(**_):
        entered.set()
        assert release.wait(10)
        return {"executed_schedule_ids": [], "errors": []}

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    worker.start()
    assert worker._thread is None and heartbeat_sink == []
    foreground = threading.Thread(target=worker.run_foreground, name="synthetic-foreground")
    foreground.start()
    try:
        assert entered.wait(5)
        worker.stop()
        assert foreground.is_alive()
        assert worker.snapshot()["running"] is True
        assert worker.snapshot()["stopping"] is True
        assert not any(not entry["running"] for entry in heartbeat_sink)
        worker.run_foreground()  # Another caller cannot enter a second loop.
        release.set()
        foreground.join(5)
        assert not foreground.is_alive()
        assert worker.snapshot()["running"] is False
        assert heartbeat_sink[-1]["running"] is False
    finally:
        cleanup(worker, release, foreground)


def test_foreground_exception_records_stopped_without_hiding_failure(monkeypatch, heartbeat_sink):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")

    def fail(**_):
        raise ValueError("synthetic cycle failure")

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", fail)
    with pytest.raises(ValueError, match="synthetic cycle failure"):
        worker.run_foreground()
    assert worker._stop_event.is_set()
    assert worker.snapshot()["running"] is False
    assert heartbeat_sink[-1]["running"] is False


def test_disabled_worker_remains_idle_and_external_stop_records_stopped(monkeypatch, heartbeat_sink):
    def forbidden(**_):
        raise AssertionError("disabled worker must not run a cycle")

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", forbidden)
    disabled = topic_scheduler.TopicScheduleWorker(enabled=False, tick_seconds=0.25, mode="disabled")
    disabled.start()
    disabled.run_foreground()
    disabled.stop()
    assert disabled._thread is None
    assert all(entry["running"] is False and entry["enabled"] is False for entry in heartbeat_sink)
    external = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")
    external.start()
    external.stop()
    assert heartbeat_sink[-1]["mode"] == "external"
    assert heartbeat_sink[-1]["running"] is False


def test_thread_start_failure_leaves_stopped_state_and_allows_explicit_retry(monkeypatch, heartbeat_sink):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")
    original_start = threading.Thread.start

    def fail_start(thread):
        if thread.name == "topic-schedule-worker":
            raise RuntimeError("synthetic start failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="synthetic start failure"):
        worker.start()
    assert worker._thread is None and worker._stop_event.is_set()
    assert worker.snapshot()["running"] is False
    monkeypatch.setattr(threading.Thread, "start", original_start)

    def cycle(**_):
        worker.stop()
        return {"executed_schedule_ids": [], "errors": []}

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    worker.start()
    original = worker._thread
    assert original is not None
    try:
        original.join(5)
        assert not original.is_alive()
        assert heartbeat_sink[-1]["running"] is False
    finally:
        worker.stop()
        original.join(5)


@pytest.mark.parametrize("foreground", [False, True])
def test_stop_before_concurrent_start_does_not_publish_stopped_for_new_worker(monkeypatch, heartbeat_sink, foreground):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")
    boundary, continue_stop = threading.Event(), threading.Event()
    foreground_registered, continue_foreground = threading.Event(), threading.Event()
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()

    class CoordinatedLock:
        def __enter__(self):
            lock.acquire()

        def __exit__(self, *exc):
            lock.release()
            if threading.current_thread().name == "synthetic-stop-before-start" and not boundary.is_set():
                boundary.set()
                assert continue_stop.wait(5), "concurrent-start boundary was not released"
            if threading.current_thread().name == "synthetic-foreground-start" and not foreground_registered.is_set():
                foreground_registered.set()
                assert continue_foreground.wait(5), "foreground registration boundary was not released"

    def cycle(**_):
        entered.set()
        assert release.wait(10)
        return {"executed_schedule_ids": [], "errors": []}

    monkeypatch.setattr(worker, "_lifecycle_lock", CoordinatedLock())
    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    stopper = threading.Thread(target=worker.stop, name="synthetic-stop-before-start")
    original = None
    stopper.start()
    try:
        assert boundary.wait(5)
        if foreground:
            original = threading.Thread(target=worker.run_foreground, name="synthetic-foreground-start")
            original.start()
        else:
            worker.start()
            original = worker._thread
        assert original is not None
        assert (foreground_registered if foreground else entered).wait(5)
        continue_stop.set()
        stopper.join(5)
        assert not stopper.is_alive()
        assert original.is_alive()
        assert (worker._foreground_thread if foreground else worker._thread) is original
        # Foreground execution preserves a preceding stop request; start()
        # explicitly restarts the background worker by clearing that request.
        assert worker._stop_event.is_set() is foreground
        if foreground:
            assert not entered.is_set()
        assert not any(not entry["running"] for entry in heartbeat_sink)
    finally:
        continue_stop.set()
        continue_foreground.set()
        stopper.join(5)
        cleanup(worker, release, original)


def test_stop_keeps_join_budget_while_final_heartbeat_is_blocked(monkeypatch, heartbeat_sink):
    finishing, release = threading.Event(), threading.Event()
    returned = threading.Event()
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")

    def heartbeat(**value):
        heartbeat_sink.append(value)
        if not value["running"]:
            finishing.set()
            assert release.wait(10), "synthetic final heartbeat was not released"

    def cycle(**_):
        worker.stop()
        return {"executed_schedule_ids": [], "errors": []}

    def stop():
        worker.stop()
        returned.set()

    monkeypatch.setattr(topic_schedules, "record_scheduler_heartbeat", heartbeat)
    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    worker.start()
    original = worker._thread
    assert original is not None
    stopper = threading.Thread(target=stop, name="synthetic-stop-during-heartbeat")
    try:
        assert finishing.wait(5)
        stopper.start()
        assert returned.wait(5), "stop waited beyond its join for heartbeat I/O"
        assert original.is_alive() and worker._thread is original
        assert worker._stop_event.is_set()
    finally:
        release.set()
        original.join(5)
        if stopper.ident is not None:
            stopper.join(5)
        assert not original.is_alive() and not stopper.is_alive()
        worker.stop()


class SignalReentryGuard:
    """Turn same-thread lock recursion into an assertion instead of a hang."""

    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None

    def __enter__(self):
        assert self.owner is not threading.current_thread(), "signal handler reentered worker lock"
        self.lock.acquire()
        self.owner = threading.current_thread()

    def __exit__(self, *exc):
        self.owner = None
        self.lock.release()


@pytest.mark.parametrize("lock_name", ["_lifecycle_lock", "_lock"])
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM"])
def test_cli_signal_handler_only_records_intent_while_worker_lock_is_held(
    monkeypatch, heartbeat_sink, lock_name, signal_name
):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")
    handlers = {}
    monkeypatch.setattr(topic_schedule_worker, "TopicScheduleWorker", lambda **_: worker)
    monkeypatch.setattr(topic_schedule_worker.signal, "signal", lambda number, handler: handlers.setdefault(number, handler))
    monkeypatch.setattr(worker, "_lifecycle_lock", SignalReentryGuard())
    monkeypatch.setattr(worker, "_lock", SignalReentryGuard())
    original_foreground = worker.run_foreground

    def forbidden_cycle(**_):
        raise AssertionError("signal before foreground start must prevent a cycle")

    def foreground():
        with getattr(worker, lock_name):
            handlers[getattr(topic_schedule_worker.signal, signal_name)](None, None)
        assert heartbeat_sink == [], "signal handler must not publish a stopped heartbeat"
        original_foreground()

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", forbidden_cycle)
    monkeypatch.setattr(worker, "run_foreground", foreground)
    assert topic_schedule_worker.main([]) == 0
    assert worker._foreground_thread is None
    assert [entry["running"] for entry in heartbeat_sink] == [False]


def test_cli_signal_handler_does_not_acquire_event_condition(monkeypatch, heartbeat_sink):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")
    handlers = {}
    monkeypatch.setattr(topic_schedule_worker, "TopicScheduleWorker", lambda **_: worker)
    monkeypatch.setattr(topic_schedule_worker.signal, "signal", lambda number, handler: handlers.setdefault(number, handler))
    original_foreground = worker.run_foreground
    original_set = worker._stop_event.set

    def forbidden_set():
        raise AssertionError("signal handler must not enter Event.set's condition lock")

    def forbidden_cycle(**_):
        raise AssertionError("signal before foreground start must prevent a cycle")

    def foreground():
        monkeypatch.setattr(worker._stop_event, "set", forbidden_set)
        try:
            handlers[topic_schedule_worker.signal.SIGINT](None, None)
        finally:
            monkeypatch.setattr(worker._stop_event, "set", original_set)
        assert heartbeat_sink == []
        original_foreground()

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", forbidden_cycle)
    monkeypatch.setattr(worker, "run_foreground", foreground)
    assert topic_schedule_worker.main([]) == 0
    assert [entry["running"] for entry in heartbeat_sink] == [False]


def test_cli_signal_received_during_registration_prevents_first_cycle(monkeypatch, heartbeat_sink):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")
    monkeypatch.setattr(topic_schedule_worker, "TopicScheduleWorker", lambda **_: worker)

    def register(number, handler):
        if number == topic_schedule_worker.signal.SIGTERM:
            handler(number, None)
            assert heartbeat_sink == [], "registration signal must not perform heartbeat I/O"

    def forbidden_cycle(**_):
        raise AssertionError("signal during registration must prevent the first cycle")

    monkeypatch.setattr(topic_schedule_worker.signal, "signal", register)
    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", forbidden_cycle)
    assert topic_schedule_worker.main([]) == 0
    assert [entry["running"] for entry in heartbeat_sink] == [False]


def test_cli_signal_during_cycle_drains_once_without_another_cycle(monkeypatch, heartbeat_sink):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="external")
    handlers = {}
    cycles = []
    monkeypatch.setattr(topic_schedule_worker, "TopicScheduleWorker", lambda **_: worker)
    monkeypatch.setattr(topic_schedule_worker.signal, "signal", lambda number, handler: handlers.setdefault(number, handler))

    def cycle(**value):
        cycles.append(value)
        assert len(cycles) == 1, "signal requested stop must not enter another cycle"
        handlers[topic_schedule_worker.signal.SIGINT](None, None)
        assert heartbeat_sink == [], "signal must let the active cycle finish before final heartbeat"
        assert worker.snapshot()["stopping"] is True
        return {"executed_schedule_ids": ["synthetic-signalled-cycle"], "errors": []}

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    assert topic_schedule_worker.main([]) == 0
    assert len(cycles) == 1 and cycles[0]["recover_orphans"] is True
    assert [entry["running"] for entry in heartbeat_sink] == [True, False]
    assert heartbeat_sink[-1]["last_cycle_summary"]["executed_schedule_ids"] == ["synthetic-signalled-cycle"]


@pytest.mark.parametrize("foreground", [False, True])
def test_stop_intent_during_tick_wait_is_checked_before_next_cycle(monkeypatch, heartbeat_sink, foreground):
    worker = topic_scheduler.TopicScheduleWorker(enabled=True, tick_seconds=0.25, mode="embedded")
    cycles, waits = [], []

    def cycle(**value):
        cycles.append(value)
        assert len(cycles) == 1, "stop intent during wait must prevent the next cycle"
        return {"executed_schedule_ids": [], "errors": []}

    def wait(timeout):
        waits.append(timeout)
        worker.request_stop()
        assert not worker._stop_event.is_set(), "signal intent must not mutate Event"
        return False  # Simulate an unchanged tick wait expiring, not Event wakeup.

    monkeypatch.setattr(topic_schedules, "run_scheduler_cycle", cycle)
    monkeypatch.setattr(worker._stop_event, "wait", wait)
    original = None
    try:
        if foreground:
            worker.run_foreground()
        else:
            worker.start()
            original = worker._thread
            assert original is not None
            original.join(5)
            assert not original.is_alive()
        assert len(cycles) == 1 and waits == [0.25]
        assert [entry["running"] for entry in heartbeat_sink] == [True, False]
    finally:
        worker.stop()
        if original is not None:
            original.join(5)
            assert not original.is_alive()

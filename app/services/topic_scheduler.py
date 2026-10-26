from __future__ import annotations

import os
import socket
import threading
from typing import Any
import uuid

from app.repositories.files import iso_now_utc
from app.services import topic_schedules


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(0.25, float(raw))
    except ValueError:
        return default


class TopicScheduleWorker:
    def __init__(self, *, enabled: bool | None = None, tick_seconds: float | None = None, mode: str | None = None) -> None:
        self.mode = str(mode or os.getenv("TOPIC_SCHEDULER_MODE") or "embedded").strip().lower()
        if self.mode not in {"embedded", "external", "disabled"}:
            self.mode = "embedded"
        env_enabled = _env_flag("TOPIC_SCHEDULER_ENABLED", True) and self.mode != "disabled"
        self.enabled = env_enabled if enabled is None else enabled
        self.tick_seconds = _env_float("TOPIC_SCHEDULER_TICK_SECONDS", 5.0) if tick_seconds is None else max(0.25, tick_seconds)
        self.worker_id = f"{self.mode}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop_requested = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._foreground_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._lock = threading.Lock()
        self._last_cycle_at: str | None = None
        self._last_cycle_summary: dict[str, Any] = {"executed_schedule_ids": [], "errors": []}

    def snapshot(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            running = bool(self._thread and self._thread.is_alive()) or self._foreground_thread is not None
            stopping = running and (self._stop_requested or self._stop_event.is_set())
        with self._lock:
            status = topic_schedules.get_scheduler_runtime_status(
                expected_mode=self.mode,
                embedded_enabled=self.enabled,
            )
            status.update({
                "enabled": self.enabled,
                "running": running,
                "stopping": stopping,
                "mode": self.mode,
                "worker_id": self.worker_id,
                "tick_seconds": self.tick_seconds,
                "last_cycle_at": self._last_cycle_at,
                "last_cycle_summary": dict(self._last_cycle_summary),
            })
            return status

    def run_cycle_now(self, *, recover_orphans: bool = False) -> dict[str, Any]:
        summary = topic_schedules.run_scheduler_cycle(recover_orphans=recover_orphans, worker_id=self.worker_id)
        living_summary: dict[str, Any] | None = None
        if _env_flag("XRML_PROTOTYPE_MODE", False) and _env_flag(
            "XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED", False
        ):
            from app.services import prototype_living_topics

            living_summary = prototype_living_topics.run_scheduler_cycle(worker_id=self.worker_id)
            summary["living_topics"] = living_summary
        with self._lock:
            self._last_cycle_at = iso_now_utc()
            self._last_cycle_summary = {
                "executed_schedule_ids": list(summary.get("executed_schedule_ids", [])),
                "errors": list(summary.get("errors", [])),
                "living_executed_topic_count": len((living_summary or {}).get("executed", [])),
                "living_errors": list((living_summary or {}).get("errors", [])),
            }
            topic_schedules.record_scheduler_heartbeat(
                worker_id=self.worker_id,
                mode=self.mode,
                enabled=self.enabled,
                running=True,
                tick_seconds=self.tick_seconds,
                last_cycle_summary=self._last_cycle_summary,
            )
        return summary

    def _record_stopped(self) -> None:
        with self._lock:
            topic_schedules.record_scheduler_heartbeat(
                worker_id=self.worker_id,
                mode=self.mode,
                enabled=self.enabled,
                running=False,
                tick_seconds=self.tick_seconds,
                last_cycle_summary=self._last_cycle_summary,
            )

    def request_stop(self) -> None:
        """Record signal-handler intent without locks, joins or heartbeat I/O.

        Even Event.set() takes a lock and can reenter an interrupted wait. The
        normal loop observes this flag at its next boundary (at most the current
        tick wait after a cycle); ordinary stop() still wakes that wait directly.
        """
        self._stop_requested = True

    def _run_loop(self) -> None:
        try:
            recover = True
            while not self._stop_requested and not self._stop_event.is_set():
                self.run_cycle_now(recover_orphans=recover)
                recover = False
                if not self._stop_requested:
                    self._stop_event.wait(self.tick_seconds)
        finally:
            # The cycle has drained (or raised). Publish stopped only at this
            # exit boundary, never when a bounded stop join merely timed out.
            self.request_stop()
            with self._lifecycle_lock:
                self._stop_event.set()
            self._record_stopped()

    def start(self) -> None:
        if self.mode == "external":
            return
        with self._lifecycle_lock:
            if (self._thread and self._thread.is_alive()) or self._foreground_thread is not None:
                return  # Includes a worker still draining after the join budget.
            if not self.enabled:
                self._record_stopped()
                return
            self._stop_requested = False
            self._stop_event.clear()
            try:
                self._thread = threading.Thread(target=self._run_loop, name="topic-schedule-worker", daemon=True)
                self._thread.start()
            except Exception:
                self.request_stop()
                self._stop_event.set()
                self._thread = None
                raise

    def stop(self) -> None:
        with self._lifecycle_lock:
            self.request_stop()
            self._stop_event.set()
            thread = self._foreground_thread or self._thread
        # Never hold a lifecycle lock during join, or join the calling worker.
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.tick_seconds + 1.0))
        with self._lifecycle_lock:
            if thread is None and self._thread is None and self._foreground_thread is None:
                self._record_stopped()
            elif thread is not None and self._thread is thread and not thread.is_alive():
                self._thread = None

    def run_foreground(self) -> None:
        with self._lifecycle_lock:
            if (self._thread and self._thread.is_alive()) or self._foreground_thread is not None:
                return
            if not self.enabled:
                self._record_stopped()
                return
            self._foreground_thread = threading.current_thread()
        try:
            recover = True
            while not self._stop_requested and not self._stop_event.is_set():
                self.run_cycle_now(recover_orphans=recover)
                recover = False
                if not self._stop_requested:
                    self._stop_event.wait(self.tick_seconds)
        finally:
            self.request_stop()
            with self._lifecycle_lock:
                self._stop_event.set()
            try:
                self._record_stopped()
            finally:
                with self._lifecycle_lock:
                    self._foreground_thread = None


def build_topic_schedule_worker() -> TopicScheduleWorker:
    return TopicScheduleWorker()

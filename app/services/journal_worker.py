"""Managed lifetime for ONE explicitly supplied journal claim and adapter.

The app may supply an explicitly approved isolated synthetic attempt. No
scheduler or provider factory is consulted. The caller must enforce authority
and budgets before claiming and dispatching work. Injection is neither live
proof. No queue scan, implicit retry, lease renewal or credential lookup occurs.
"""
from datetime import datetime, timezone
import threading
from typing import Any, Callable

from app.repositories import journal_executions as repository
from app.services.journal_execution import execute
from app.services.providers.base import StructuredOutputProvider


def _duration(value: float) -> float:
    if type(value) not in {int, float} or not 0 <= value <= 30:
        raise ValueError("journal_worker_timeout_invalid")
    return float(value)


class JournalAttemptWorker:
    """Single-use owner; stop fences storage but cannot abort blocking transport.

    A draining thread stays referenced and visible until it actually exits.
    A daemon thread permits process exit after bounded join; it is still tracked
    while this process lives. Process death is handled by the existing lease
    expiry contract, with no automatic retry of an unknown external outcome.
    """

    def __init__(self, claimed: repository.Claim, *, provider: StructuredOutputProvider, model: str,
                 shutdown_grace_seconds: float = 2, clock: Callable[[], datetime] | None = None) -> None:
        self._claim = claimed
        self._provider = provider
        self._model = model
        self._grace = _duration(shutdown_grace_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            return dict(execution_id=self._claim.execution_id, attempt=self._claim.attempt,
                        started=self._started, running=running, stopping=running and self._closed,
                        closed=self._closed, error_code=self._error)

    def _set_error(self, code: str) -> None:
        with self._lock:
            self._error = code  # Never store arbitrary adapter diagnostics or the claim token.

    def _abandon(self, reason: str) -> None:
        try:
            repository.abandon(self._claim, reason=reason, now=self._clock())
        except Exception:
            self._set_error("journal_worker_store_unavailable")
            raise  # A failed fence is not reported as a successful shutdown.

    def _run(self) -> None:
        try:
            execute(self._claim, provider=self._provider, model=self._model, clock=self._clock)
        except Exception:
            self._set_error("journal_worker_execution_failed")
            try:
                self._abandon("worker_failed")
            except Exception:
                # Already exposed in snapshot; keep the durable lease recoverable.
                # A worker failure never triggers another adapter call.
                pass

    def start(self) -> bool:
        with self._lock:
            if self._started or self._closed:
                return False
            self._started = True
            try:
                self._thread = threading.Thread(target=self._run, name="journal-attempt-worker", daemon=True)
                self._thread.start()
                return True
            except Exception:
                self._closed = True
                self._thread = None
                self._error = "journal_worker_start_failed"
        # No lifecycle lock across storage: status/stop remain available.
        self._abandon("worker_failed")
        raise RuntimeError("journal_worker_start_failed")

    def wait(self, timeout: float) -> bool:
        duration = _duration(timeout)
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(duration)
        return thread is None or not thread.is_alive()

    def stop(self) -> bool:
        with self._lock:
            self._closed = True
        # Fence BEFORE waiting, so a late result cannot commit during grace.
        try:
            self._abandon("shutdown")
        finally:
            self.wait(self._grace)
        return self.wait(0)

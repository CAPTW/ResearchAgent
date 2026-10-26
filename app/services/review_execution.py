"""Fixed synthetic worker. Persistent fences precede work; no restart resumption."""
from copy import deepcopy
from dataclasses import asdict
import logging
import os
from threading import Lock, Thread
import time
from typing import Any

from app.auth.resource_access import ResourceActor
from app.domain import prepared_review, review_execution as domain
from app.domain.review_preparation import encoded
from app.repositories import review_executions as repository
from app.services import review_preparation
from app.services.offline_environment import require_isolated_data_root
from app.services.providers.base import ProviderRequest
from app.services.review_offline import SyntheticStructuredReviewProvider

logger = logging.getLogger(__name__)


def scope(topic: str, version: str, preparation: str, actor: ResourceActor) -> domain.Scope:
    return domain.Scope(topic, version, preparation, actor.subject or actor.actor_type,
                        tuple(review_preparation._occurrences(topic, version, actor)))


class OfflineExecutionRuntime:
    def __init__(self) -> None:
        self._lock = Lock()
        self._closed = False
        self._threads: dict[Thread, domain.Claim] = {}

    @property
    def enabled(self) -> bool:
        with self._lock:
            return not self._closed

    def dispatch(self, scope: domain.Scope, identity: str, body: domain.Dispatch) -> dict[str, Any]:
        with self._lock:
            self._threads = {thread: claim for thread, claim in self._threads.items() if thread.is_alive()}
            detail, claim = repository.claim(scope, identity, body, enabled=not self._closed)
            if claim is None:
                return detail
            thread = Thread(target=self._run, args=(claim,), name="paper-review-offline", daemon=True)
            self._threads[thread] = claim
            try:
                thread.start()
            except RuntimeError:
                repository.interrupt(claim, "worker_start_failed")
                self._threads.pop(thread, None)
                raise RuntimeError("review_execution_worker_start_failed") from None
            return detail

    def _run(self, claim: domain.Claim) -> None:
        phase = "adapter_failed"
        try:
            with self._lock:
                if self._closed:
                    return
                started = repository.worker_start(claim)
            if started is None:
                return
            contract = started["contract"]["prepared"]
            request = ProviderRequest(**deepcopy(contract["provider_request"]))
            # No connection or transaction is alive across this boundary. The
            # constructor is fixed; a browser flag cannot inject a live adapter.
            response = SyntheticStructuredReviewProvider().generate_json(request)
            phase = "result_invalid"
            if encoded(asdict(request)) != encoded(contract["provider_request"]):
                raise ValueError("review_execution_input_changed")
            record = prepared_review.result(contract, response)
            record["validation"]["execution"] = domain.marker(claim.identity)
            with self._lock:
                if self._closed:
                    return
                received = repository.receive(claim, record)
            if received is not None:
                phase = "publication_failed"
                with self._lock:
                    if not self._closed:
                        repository.publish(claim)
        except Exception:
            logger.warning("review_execution_%s", phase)
            # If reception committed, interrupt is intentionally a no-op. The
            # result is retained for explicit storage retry, without new work.
            try:
                repository.interrupt(claim, "adapter_failed" if phase == "adapter_failed" else "result_invalid")
            except Exception:
                logger.warning("review_execution_failure_state_unavailable")

    def wait_idle(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise TimeoutError("review_execution_worker_still_running")

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            claims = list(self._threads.values())
        failed = False
        for claim in claims:
            try:
                repository.interrupt(claim, "worker_stopped")
            except Exception:
                logger.warning("review_execution_stop_state_unavailable")
                failed = True
        try:
            self.wait_idle(1)
        except TimeoutError:
            # Retain handles. A fence prevents late publication but is not a
            # claim of physical cancellation of a future external process.
            logger.warning("review_execution_worker_still_running")
        if failed:
            raise RuntimeError("review_execution_stop_state_unavailable")


def build_runtime() -> OfflineExecutionRuntime | None:
    if os.environ.get("XRML_REVIEW_EXECUTION_OFFLINE_ENABLED", "0") != "1":
        return None
    require_isolated_data_root("XRML_REVIEW_EXECUTION_OFFLINE", "review_execution_configuration_invalid")
    return OfflineExecutionRuntime()

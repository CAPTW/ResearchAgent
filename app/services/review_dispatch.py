"""Default-deny paper dispatcher. No issuer or runtime discovery.

An explicit trusted server composition supplies a frozen adapter plus an issuer
resolver or a policy for already stored authority. The latter cannot approve or
issue a grant. Browser fields cannot select these dependencies or promote an
offline grant into authority for a live target.
"""
from copy import deepcopy
import logging
from threading import Lock, Thread
import time
from typing import Any, Callable, Protocol

from app.domain import review_authority as authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_preparation import encoded
from app.repositories import review_authorities as authorities, review_executions as repository, review_transport_history as history
from app.services.providers.base import ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest

class PaperTransportProvider(Protocol):
    def manifest(self) -> dict[str, Any]: ...
    def generate_record(self, request: ProviderRequest) -> dict[str, Any]: ...

Resolver = Callable[[dict[str, Any], domain.Control], dict[str, Any]]
StoredAuthorityPolicy = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)


class PaperDispatchRuntime:
    def __init__(self, *, resolver: Resolver | None = None, provider: PaperTransportProvider | None = None,
                 stored_authority_policy: StoredAuthorityPolicy | None = None,
                 owner_first_output_decisions: frozenset[str] = frozenset()) -> None:
        authority.validate_first_output_decisions(owner_first_output_decisions)
        self._resolver, self._provider = resolver, provider
        self._stored_authority_policy = stored_authority_policy
        self._first_output_decisions = owner_first_output_decisions
        self._lock = Lock()
        self._closed = False
        self._threads: dict[Thread, domain.Claim] = {}

    @property
    def enabled(self) -> bool:
        with self._lock:
            return (not self._closed and self._provider is not None
                    and (self._resolver is not None or self._stored_authority_policy is not None))

    def approve(self, scope: domain.Scope, identity: str, body: domain.Control) -> dict[str, Any]:
        if body.action != "approve":
            raise ValueError("paper_review_authority_command_invalid")
        saved = repository.detail(scope, identity, schema=transport.SCHEMA)
        if repository._replay(saved, dict(kind="control", body=body.model_dump())):
            return saved  # Lost response: do not call the issuer again or renew.
        if not self.enabled or self._resolver is None:
            raise ValueError("paper_review_trusted_authority_required")
        record = self._resolver(deepcopy(saved["contract"]), body)
        parsed = authority.read_record(record, saved["contract"])
        authority.require_first_output_decision(authority.first_output_projection(parsed.grant), self._first_output_decisions)
        with self._lock:
            if self._closed:
                raise ValueError("review_execution_disabled")
            return authorities.register(scope, identity, body, record)

    def dispatch(self, scope: domain.Scope, identity: str, body: domain.Dispatch) -> dict[str, Any]:
        with self._lock:
            self._threads = {thread: claim for thread, claim in self._threads.items() if thread.is_alive()}
            saved = repository.detail(scope, identity, schema=transport.SCHEMA)
            if repository._replay(saved, dict(kind="dispatch", body=body.model_dump())):
                return saved
            if (self._closed or self._provider is None
                    or (self._resolver is None and self._stored_authority_policy is None)):
                raise ValueError("paper_review_trusted_authority_required")
            if encoded(saved["contract"]["transport"]) != encoded(self._provider.manifest()):
                raise ValueError("review_execution_transport_mismatch")
            validate_runtime = getattr(self._provider, "validate_local_runtime", None)
        if self._stored_authority_policy is not None:
            # The policy selects an immutable trusted record. claim() still
            # checks that record's exact binding, expiry and revocation in its
            # write transaction, and worker_start() checks again before work.
            self._stored_authority_policy(deepcopy(saved))
        authority.require_first_output_decision(history.detail(scope, identity).get("authority") or {}, self._first_output_decisions)
        # Never hold lifecycle/storage locks while reading runtime files. This
        # does not grant permission, and the adapter rechecks at actual run.
        if validate_runtime is not None:
            validate_runtime()
        with self._lock:
            if self._closed:
                raise ValueError("review_execution_disabled")
            detail, claim = repository.claim(scope, identity, body, enabled=True, schema=transport.SCHEMA)
            if claim is None:
                return detail
            thread = Thread(target=self._run, args=(claim,), name="paper-review-transport", daemon=True)
            self._threads[thread] = claim
            try:
                thread.start()
            except RuntimeError:
                repository.interrupt(claim, "worker_start_failed")
                self._threads.pop(thread, None)
                raise RuntimeError("review_execution_worker_start_failed") from None
            return detail

    def _run(self, claim: domain.Claim) -> None:
        reason = "adapter_failed"
        try:
            with self._lock:
                if self._closed:
                    return
                started = repository.worker_start(claim)
            if started is None:
                return
            assert self._provider is not None
            record = self._provider.generate_record(ProviderRequest(**deepcopy(started["contract"]["prepared"]["provider_request"])))
            reason = "result_invalid"
            with self._lock:
                if self._closed:
                    return
                received = repository.receive(claim, record)
            if received is not None:
                with self._lock:
                    if not self._closed:
                        repository.publish(claim)
        except ProviderReportedFailure as exc:
            self._fail(claim, "provider_" + exc.status)
        except ProviderRefusalError:
            self._fail(claim, "provider_refused")
        except ProviderOutputError:
            self._fail(claim, "output_invalid")
        except Exception:
            self._fail(claim, reason)

    @staticmethod
    def _fail(claim: domain.Claim, reason: str) -> None:
        logger.warning("paper_dispatch_%s", reason)
        try:
            # RESULT_RECEIVED is retained when publication fails. Recovery saves
            # existing bytes without a second process or a renewed approval.
            repository.interrupt(claim, reason)
        except Exception:
            logger.warning("paper_dispatch_failure_state_unavailable")

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
                logger.warning("paper_dispatch_stop_state_unavailable")
                failed = True
        try:
            self.wait_idle(1)
        except TimeoutError:
            logger.warning("paper_dispatch_worker_still_running")
        if failed:
            raise RuntimeError("paper_dispatch_stop_state_unavailable")

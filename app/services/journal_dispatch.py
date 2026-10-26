"""Manual journal admission + tracked worker ownership; default authority denies.

An explicitly selected stored record can supply authority. The app connects only
its isolated synthetic issuer/adapter; no live environment fallback exists. Live composition must
authenticate its approval source before registering records or live activation.
Supplying an adapter/proof alone is not authorization. Resolvers are local/non-live.
"""
from datetime import datetime, timezone
import threading
from typing import Any, Callable

from app.domain.journal_dispatch import AdapterBinding, AuthorityReference, Dispatch, Grant
from app.repositories import journal_dispatch as admission, journal_executions as repository
from app.repositories import journal_authority
from app.services.journal_worker import JournalAttemptWorker, _duration
from app.services.providers.base import StructuredOutputProvider

Authority = Callable[[dict[str, Any], str, AdapterBinding], Grant | AuthorityReference | None]


class StoredJournalAuthority:
    """Explicitly selected server record; never auto-discovers or creates approval."""

    def __init__(self, authorization_id: str, *, clock: Callable[[], datetime] | None = None) -> None:
        self._authorization_id = authorization_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, contract: dict[str, Any], actor: str, binding: AdapterBinding) -> AuthorityReference:
        return journal_authority.resolve(contract["topic_id"], contract["job_id"], contract["execution_id"],
                                         self._authorization_id, actor, binding, now=self._clock())


def _unavailable(contract: dict[str, Any], actor: str, binding: AdapterBinding) -> None:
    return None


class JournalDispatcher:
    def __init__(self, *, provider: StructuredOutputProvider, model: str, binding: AdapterBinding,
                 authority: Authority = _unavailable, clock: Callable[[], datetime] | None = None,
                 shutdown_grace_seconds: float = 2) -> None:
        self._provider, self._model, self._binding = provider, model, binding
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._grace = _duration(shutdown_grace_seconds)
        self._lock = threading.Lock()
        self._closed = False
        self._workers: list[JournalAttemptWorker] = []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(closed=self._closed, workers=[worker.snapshot() for worker in self._workers])

    def dispatch(self, topic: str, job_id: str, execution_id: str, body: Dispatch, actor: str) -> dict[str, Any]:
        scope = (topic, job_id, execution_id)
        replay = admission.replay(*scope, body, actor)
        if replay is not None:
            return replay  # Durable lost-response retry needs no renewed authority or worker.
        with self._lock:
            if self._closed:
                raise ValueError("journal_dispatch_closed")
        saved = repository.detail(*scope)
        spec = saved["contract"]["specification"]
        if self._provider.provider_name != spec["provider_name"] or self._model != spec["model"]:
            raise ValueError("journal_execution_adapter_mismatch")
        validate_binding = getattr(self._provider, "validate_journal_binding", None)
        if validate_binding is not None:
            validate_binding(saved["contract"], self._model, self._binding)
        manifest = getattr(self._provider, "manifest")() if validate_binding is not None else None
        validate_runtime = getattr(self._provider, "validate_local_runtime", None)
        if validate_runtime is not None:
            # Optional read-only local check, outside lifecycle/storage locks.
            # Replay has already returned. The adapter must still check at run.
            validate_runtime()
        grant = self._authority(saved["contract"], actor, self._binding)
        if grant is None:
            raise ValueError("journal_dispatch_authority_unavailable")
        claimed = None
        try:
            with self._lock:
                if self._closed:
                    raise ValueError("journal_dispatch_closed")
                # Admission is a short local transaction. Serialize it with closing
                # so stop cannot return before an untracked new reservation appears.
                _, claimed = admission.admit(*scope, body, actor, grant, self._binding, now=self._clock(), adapter_manifest=manifest)
                if claimed is None:
                    return repository.detail(*scope)
                worker = JournalAttemptWorker(claimed, provider=self._provider, model=self._model,
                                              clock=self._clock, shutdown_grace_seconds=self._grace)
                self._workers = [item for item in self._workers if item.snapshot()["running"]]
                self._workers.append(worker)
                worker.start()
        except Exception:
            if claimed is not None:
                repository.abandon(claimed, reason="worker_failed", now=self._clock())
            raise
        return repository.detail(*scope)

    def stop(self) -> bool:
        with self._lock:
            self._closed = True
            workers = tuple(self._workers)
        failures: list[Exception] = []
        for worker in workers:
            try:
                worker.stop()
            except Exception as exc:
                failures.append(exc)  # Still fence every other owned attempt.
        if failures:
            raise RuntimeError("journal_dispatch_stop_failed") from failures[0]
        return all(not worker.snapshot()["running"] for worker in workers)

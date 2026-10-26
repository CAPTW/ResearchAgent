"""Server-composed Codex V2 dispatch. No discovery, issuer or environment activation.

Production app construction supplies no runtime. A trusted composition must
provide both an explicit runtime and accepted issuer/policy pairs; browser data,
preview/checker observations and isolated offline grants cannot supply them.
"""
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable

from pydantic import Field

from app.domain.journal_dispatch import Dispatch, read_receipt, RegisteredReceipt
from app.domain.temporal_memory import digest
from app.repositories import journal_authority as authority, journal_dispatch as admission, journal_executions as executions
from app.services.journal_dispatch import JournalDispatcher, StoredJournalAuthority
from app.services.providers.codex_journal_process import CliRuntime
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider


class Request(Dispatch):
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_authority_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    def dispatch_request(self) -> Dispatch:
        return Dispatch(request_id=self.request_id, expected_sequence=self.expected_sequence,
                        acknowledge_unknown=self.acknowledge_unknown)


def _actor(saved: dict[str, Any], actor: str) -> None:
    if saved["contract"]["actor_id"] != actor:
        raise ValueError("journal_codex_dispatch_actor_mismatch")


def _replay(saved: dict[str, Any], body: Request) -> None:
    event = next(item for item in saved["events"] if item.get("dispatch", {}).get("request", {}).get("request_id") == body.request_id)
    receipt = read_receipt(event["dispatch"])
    if (not isinstance(receipt, RegisteredReceipt)
            or receipt.authority.authorization_id != body.authorization_id
            or receipt.authority.record_digest != body.expected_authority_digest
            or receipt.binding.command_shape_sha256 != body.expected_binding_digest
            or event.get("adapter_manifest", {}).get("schema_version") != "CODEX_JOURNAL_CLI_V2"):
        raise ValueError("journal_codex_dispatch_binding_changed")


def dispatch(runtime: "CodexJournalRuntime | None", topic: str, job: str, execution: str,
             body: Request, actor: str) -> dict[str, Any]:
    saved = executions.detail(topic, job, execution)
    _actor(saved, actor)
    replay = admission.replay(topic, job, execution, body.dispatch_request(), actor)
    if replay is not None:
        _replay(replay, body)
        return replay  # Readback survives restart, expiry, revocation and disabled runtime.
    if runtime is None:
        raise ValueError("journal_codex_dispatch_disabled")
    return runtime.dispatch(topic, job, execution, body, actor)


def status(runtime: "CodexJournalRuntime | None", topic: str, job: str, execution: str, actor: str) -> dict[str, Any]:
    saved = executions.detail(topic, job, execution)
    _actor(saved, actor)
    result = dict(schema_version="CODEX_JOURNAL_DISPATCH_STATUS_V1", profile="CODEX_JOURNAL_CLI_V2",
        execution_id=execution, actor_id=saved["contract"]["actor_id"], contract_digest=digest(saved["contract"]),
        observed_sequence=saved["current"]["sequence"], enabled=False, reason="journal_codex_dispatch_disabled",
        binding=None, manifest=None, authorities=[], authorities_has_more=False)
    if runtime is None:
        return result
    return runtime.status(saved, actor, result)


class CodexJournalRuntime:
    def __init__(self, *, runtime: CliRuntime, trusted_issuers: frozenset[tuple[str, str]],
                 reasoning_effort: str, timeout_seconds: float, clock: Callable[[], datetime] | None = None) -> None:
        from app.services.journal_offline import ISSUER, POLICY
        if (not trusted_issuers or any(not issuer.strip() or not policy.strip() or issuer == ISSUER or policy == POLICY
                                      for issuer, policy in trusted_issuers)):
            raise ValueError("journal_codex_dispatch_issuer_invalid")
        self._runtime = runtime
        self._trusted = frozenset(trusted_issuers)
        self._effort, self._timeout = reasoning_effort, timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._closed = False
        self._dispatchers: list[JournalDispatcher] = []

    def _provider(self, contract: dict[str, Any]) -> CodexJournalV2Provider:
        if contract["specification"]["provider_name"] != "codex_cli":
            raise ValueError("journal_codex_dispatch_target_mismatch")
        value = CodexJournalV2Provider(contract, runtime=self._runtime,
            reasoning_effort=self._effort, timeout_seconds=self._timeout)
        value.validate_local_runtime()
        return value

    def _record(self, saved: dict[str, Any], provider: CodexJournalV2Provider, actor: str) -> None:
        record = saved["record"]
        if ((record["registered_by"], record["grant"]["policy_version"]) not in self._trusted
                or record["grant"]["actor_id"] != actor or record["binding"] != provider.binding.model_dump()):
            raise ValueError("journal_codex_dispatch_authority_mismatch")

    def status(self, saved: dict[str, Any], actor: str, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return result | dict(reason="journal_dispatch_closed")
        provider = self._provider(saved["contract"])
        contract = saved["contract"]
        scope = (contract["topic_id"], contract["job_id"], contract["execution_id"])
        rows = authority.listing(*scope)
        items = []
        for entry in rows["items"]:
            record = entry["record"]
            try:
                self._record(entry, provider, actor)
            except ValueError:
                state = "MISMATCH"
            else:
                try:
                    authority.resolve(*scope, record["grant"]["authorization_id"], actor, provider.binding, now=self._clock())
                except ValueError as exc:
                    state = {"journal_authority_revoked": "REVOKED", "journal_dispatch_authority_expired": "EXPIRED"}.get(str(exc), "MISMATCH")
                else:
                    attempt = saved["current"]["attempt"] + 1
                    tokens = saved["contract"]["specification"]["max_output_tokens"]
                    state = "BUDGET_EXHAUSTED" if (attempt > record["grant"]["max_attempts"]
                        or attempt * tokens > record["grant"]["max_reserved_output_tokens"]) else "AVAILABLE"
            items.append(dict(authorization_id=record["grant"]["authorization_id"], record_digest=digest(record),
                status=state, expires_at=record["grant"]["expires_at"], issuer=record["registered_by"],
                policy_version=record["grant"]["policy_version"]))
        return result | dict(enabled=True, reason=None, binding=provider.binding.model_dump(), manifest=provider.manifest(),
                             authorities=items, authorities_has_more=rows["total"] > len(items))

    def dispatch(self, topic: str, job: str, execution: str, body: Request, actor: str) -> dict[str, Any]:
        saved = executions.detail(topic, job, execution)
        _actor(saved, actor)
        provider = self._provider(saved["contract"])
        if provider.binding.command_shape_sha256 != body.expected_binding_digest:
            raise ValueError("journal_codex_dispatch_binding_changed")
        entry = authority.detail(topic, job, execution, body.authorization_id, body.expected_authority_digest)
        self._record(entry, provider, actor)
        reference = authority.reference_from(entry["record"])

        def resolve(contract, current_actor, binding):
            # Recheck the same immutable authority, including its revocation, in
            # the admission transaction. Never fall back to an in-memory Grant.
            current = StoredJournalAuthority(body.authorization_id, clock=self._clock)(contract, current_actor, binding)
            if current != reference:
                raise ValueError("journal_authority_changed")
            return current

        with self._lock:
            if self._closed:
                raise ValueError("journal_dispatch_closed")
            controller = JournalDispatcher(provider=provider, model=provider.model, binding=provider.binding,
                                           authority=resolve, clock=self._clock)
            self._dispatchers = [item for item in self._dispatchers if any(worker["running"] for worker in item.snapshot()["workers"])]
            self._dispatchers.append(controller)
            result = controller.dispatch(topic, job, execution, body.dispatch_request(), actor)
            _replay(result, body)  # A concurrent winner must match the HTTP-only binding fields too.
            return result

    def stop(self) -> bool:
        with self._lock:
            self._closed = True
            controllers = tuple(self._dispatchers)
        results, failures = [], []
        for controller in controllers:
            try:
                results.append(controller.stop())
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError("journal_codex_dispatch_stop_failed") from failures[0]
        return all(results)

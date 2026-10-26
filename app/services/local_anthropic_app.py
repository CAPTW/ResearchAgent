"""Local app composition using saved settings and explicit per-request decisions.

Construction and inspection do not decrypt credentials, issue approval or run a
Provider. Production/OIDC composition stays disabled. Each stored request pins
its own model, output limit and credential revision across restart/settings edits.
"""
from threading import RLock
from typing import Any

from pydantic import Field

from app.auth.resource_access import ResourceActor
from app.domain import review_execution as domain
from app.domain.review_preparation import encoded, sha
from app.repositories import local_anthropic_decisions as decisions, review_preparations, review_transport_history as history
from app.services import model_connections, model_credentials
from app.services import review_anthropic_native as native


class OwnerDecision(domain.Dispatch):
    expected_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def decide(runtime, actor: ResourceActor, scope: domain.Scope, identity: str, body: OwnerDecision):
    native._local_actor(actor, scope)
    saved = native._project(scope, history.detail(scope, identity))
    if (saved["contract_sha256"] != body.expected_contract_sha256 or saved["binding_sha256"] != body.expected_binding_sha256):
        raise ValueError("paper_anthropic_native_binding_changed")
    existing = decisions.get(saved["contract"])
    if existing is not None:
        if encoded(existing["decision"]["command"]) != encoded(body.model_dump()):
            raise ValueError("paper_local_owner_decision_conflict")
        return decisions.receipt(existing)
    if not isinstance(runtime, LocalAnthropicAppRuntime):
        raise ValueError("paper_anthropic_native_disabled")
    value = decisions.create(scope, identity, body.model_dump(), lambda: runtime.provider_for(saved["contract"]))
    return decisions.receipt(value)


class LocalAnthropicAppRuntime:
    def __init__(self) -> None:
        self._vault = model_credentials.path().resolve()
        self._lock = RLock()
        self._closed = False
        self._runtimes: dict[tuple[Any, ...], native.AnthropicNativeRuntime] = {}

    def _runtime(self, model: str, revision: int, tokens: int, timeout: float = 60,
                 contract: dict[str, Any] | None = None) -> native.AnthropicNativeRuntime:
        stored = decisions.get(contract) if contract is not None else None
        allowed = frozenset({sha(encoded(stored["decision"]))}) if stored else frozenset()
        key = (model, revision, tokens, timeout, tuple(allowed))
        def source(value):
            found = decisions.get(value)
            return found["record"] if found else None
        with self._lock:
            if self._closed:
                raise ValueError("paper_anthropic_native_closed")
            if key not in self._runtimes:
                self._runtimes[key] = native.AnthropicNativeRuntime(model=model, credential_revision=revision,
                    max_output_tokens=tokens, timeout_seconds=timeout, trusted_issuers=frozenset({(decisions.ISSUER, decisions.POLICY)}),
                    source=source, owner_first_output_decisions=allowed, credential_path=self._vault)
            return self._runtimes[key]

    def _for_preparation(self, preparation):
        budget = preparation.get("analysis_unit", {}).get("budget")
        if budget is not None:
            if budget.get("provider_name") != "anthropic":
                raise ValueError("paper_anthropic_native_target_mismatch")
            model, tokens = budget["model"], budget["max_output_tokens"]
        else:
            model, tokens = model_connections.read()["models"].get("anthropic_api", ""), 2200
        if not model:
            raise ValueError("paper_anthropic_native_model_required")
        try:
            metadata = model_credentials.status("anthropic_api", credential_path=self._vault)
        except model_credentials.CredentialUnavailable:
            raise ValueError("paper_anthropic_native_credential_unavailable") from None
        if (not metadata["storage_available"] or metadata["stored"] is not True
                or type(metadata["revision"]) is not int or metadata["revision"] < 1):
            raise ValueError("paper_anthropic_native_credential_unavailable")
        return self._runtime(model, metadata["revision"], tokens)

    def _for_contract(self, contract):
        native._target(dict(contract=contract))
        return self._runtime(contract["prepared"]["model"], contract["transport"]["credential_binding"]["revision"],
            contract["prepared"]["provider_request"]["max_output_tokens"], contract["transport"]["timeout_seconds"], contract)

    def prepare_provider(self, preparation):
        return self._for_preparation(preparation).prepare_provider(preparation)

    def prepare(self, scope, body, manifest):
        preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
        runtime = self._for_preparation(preparation)
        _, provider = runtime.prepare_provider(preparation)
        if encoded(provider.manifest()) != encoded(manifest):
            raise ValueError("paper_anthropic_native_binding_changed")
        runtime.prepare(scope, body, manifest)

    def provider_for(self, contract, before_send=None):
        return self._for_contract(contract).provider_for(contract, before_send)

    def record(self, actor, saved):
        return self._for_contract(saved["contract"]).record(actor, saved)

    def check_authority(self, saved):
        return self._for_contract(saved["contract"]).check_authority(saved)

    def dispatch(self, scope, identity, saved, body):
        return self._for_contract(saved["contract"]).dispatch(scope, identity, saved, body)

    def decision_allowed(self, saved):
        if saved["current"]["state"] != "READY" or decisions.get(saved["contract"]) is not None:
            return False
        self.provider_for(saved["contract"])
        return True

    def wait_idle(self, timeout):
        with self._lock:
            runtimes = tuple(self._runtimes.values())
        for runtime in runtimes:
            runtime.wait_idle(timeout)

    def stop(self):
        with self._lock:
            self._closed = True
            runtimes = tuple(self._runtimes.values())
        failed = False
        for runtime in runtimes:
            try:
                runtime.stop()
            except RuntimeError:
                failed = True
        if failed:
            raise RuntimeError("paper_anthropic_native_stop_failed")

"""Explicit, immutable OpenAI wire binding for one frozen journal request.

No provider registry, environment credentials, app route or approval issuance.
The manifest identifies bytes/settings, not schema/model live acceptance, account
identity, usage or monetary cost. A server composition still needs real authority
and cost policy before activation; tests use synthetic authority and MockTransport.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any, Callable

import httpx

from app.domain.journal_dispatch import AdapterBinding
from app.domain.temporal_memory import digest
from app.services.journal_dispatch import Authority, JournalDispatcher, _unavailable
from app.services.providers.base import ProviderRequest, ProviderResult, ProviderUnavailableError
from app.services.providers.openai_response import read_response
from app.services.providers.openai_journal_wire import ADAPTER_ID, build_wire, exact_json


@dataclass(frozen=True, init=False)
class OpenAIJournalProvider:
    model: str
    endpoint: str
    timeout_seconds: float
    _api_key: str = field(repr=False)
    _transport: httpx.BaseTransport | None = field(repr=False)
    _contract_digest: str = field(repr=False)
    _request_json: str = field(repr=False)
    _body: bytes = field(repr=False)
    _manifest_json: str = field(repr=False)

    @property
    def provider_name(self) -> str:
        return "openai"

    def __init__(self, contract: dict[str, Any], *, endpoint: str, timeout_seconds: float,
                 api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(api_key, str) or any(c in api_key for c in "\r\n"):
            raise ValueError("journal_openai_configuration_invalid")
        request_json, body, manifest = build_wire(contract, endpoint, timeout_seconds)
        for key, value in dict(model=manifest["model"], endpoint=endpoint, timeout_seconds=float(timeout_seconds),
                               _api_key=api_key.strip(), _transport=transport, _contract_digest=manifest["contract_digest"],
                               _request_json=request_json, _body=body, _manifest_json=exact_json(manifest)).items():
            object.__setattr__(self, key, value)

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def binding(self) -> AdapterBinding:
        return AdapterBinding(surface_id="api", adapter_id=ADAPTER_ID, adapter_version="1",
                              command_shape_sha256=digest(self.manifest()))

    def validate_journal_binding(self, contract: dict[str, Any], model: str, binding: AdapterBinding | None = None) -> None:
        if (digest(contract) != self._contract_digest or model != self.model
                or (binding is not None and binding != self.binding)
                or exact_json(contract["provider_request"]) != self._request_json):
            raise ValueError("journal_execution_adapter_mismatch")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, configured=self.is_configured(), model=self.model,
                    live_call_enabled=False, activation="UNREGISTERED_ADAPTER_CONTRACT", binding=self.binding.model_dump())

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if exact_json(asdict(request)) != self._request_json:
            raise ValueError("journal_execution_adapter_mismatch")
        if not self.is_configured():
            raise ProviderUnavailableError("Explicit journal provider credentials are unavailable.")
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", self.endpoint, headers=headers, content=self._body) as response:
                return read_response(response)


def journal_dispatcher(provider: OpenAIJournalProvider, *, authority: Authority = _unavailable,
                       clock: Callable[[], datetime] | None = None, shutdown_grace_seconds: float = 2) -> JournalDispatcher:
    """Derive the binding from the actual immutable adapter, defaulting to denial."""
    return JournalDispatcher(provider=provider, model=provider.model, binding=provider.binding, authority=authority,
                             clock=clock, shutdown_grace_seconds=shutdown_grace_seconds)

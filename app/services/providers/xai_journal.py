"""Explicit xAI API key Journal adapter; no subscription CLI or live activation.

The fixed wire, request and output are locally validated. An explicit key is not
execution authority, and a reported model/usage is not live or billing proof.
"""
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import json
from typing import Any, Callable

import httpx

from app.domain import journal_execution
from app.domain.journal_dispatch import AdapterBinding
from app.domain.temporal_memory import digest
from app.services.journal_dispatch import Authority, JournalDispatcher, _unavailable
from app.services.providers.base import (
    ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure,
    ProviderRequest, ProviderResult, ProviderUnavailableError,
)
from app.services.providers.openai_response import read_response
from app.services.providers.xai_journal_wire import ADAPTER_ID, ENDPOINT, build_wire, exact_json


@dataclass(frozen=True, init=False)
class XaiJournalProvider:
    model: str
    endpoint: str
    timeout_seconds: float
    _api_key: str = field(repr=False)
    _transport: httpx.BaseTransport | None = field(repr=False)
    _contract_json: str = field(repr=False)
    _contract_digest: str = field(repr=False)
    _request_json: str = field(repr=False)
    _body: bytes = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, api_key: str, timeout_seconds: float,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(api_key, str) or any(ord(char) < 32 or ord(char) > 126 for char in api_key):
            raise ValueError("journal_xai_configuration_invalid")
        request, body, manifest = build_wire(contract, timeout_seconds)
        for key, value in dict(model=manifest["model"], endpoint=ENDPOINT, timeout_seconds=float(timeout_seconds),
            _api_key=api_key.strip(), _transport=transport, _contract_json=exact_json(contract),
            _contract_digest=manifest["contract_digest"], _request_json=request, _body=body,
            _manifest_json=exact_json(manifest)).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return "xai"

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
            live_call_enabled=False, activation="UNREGISTERED_ADAPTER_CONTRACT", binding=self.binding.model_dump(),
            authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        if not self.is_configured():
            raise ProviderUnavailableError("Explicit xAI Journal credentials are unavailable.")

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if exact_json(asdict(request)) != self._request_json:
            raise ValueError("journal_execution_adapter_mismatch")
        self.validate_local_runtime()
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", ENDPOINT, headers=headers, content=self._body) as response:
                    result = replace(read_response(response), provider_name="xai")
        except ProviderReportedFailure:
            raise
        except ProviderRefusalError:
            raise ProviderRefusalError("xAI refused the Journal request.") from None
        except ProviderOutputError:
            raise ProviderOutputError("journal_xai_output_invalid") from None
        except (ProviderError, httpx.HTTPError):
            raise ProviderError("journal_xai_transport_outcome_unknown") from None
        try:
            journal_execution.result(json.loads(self._contract_json), result)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ProviderOutputError("journal_xai_output_invalid") from None
        return result


def journal_dispatcher(provider: XaiJournalProvider, *, authority: Authority = _unavailable,
                       clock: Callable[[], datetime] | None = None, shutdown_grace_seconds: float = 2) -> JournalDispatcher:
    """Derive the actual immutable binding; authority remains default-deny."""
    return JournalDispatcher(provider=provider, model=provider.model, binding=provider.binding, authority=authority,
                             clock=clock, shutdown_grace_seconds=shutdown_grace_seconds)

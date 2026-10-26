"""Explicit, unregistered HTTP binding for one immutable prepared paper request.

No account discovery, app activation, approval issuance or automatic retry.
Qualification uses MockTransport; real use requires separate trusted composition.
"""
from dataclasses import asdict, dataclass, field
import json
from typing import Any

import httpx

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderOutputError, ProviderRequest, ProviderResult, ProviderUnavailableError
from app.services.providers.openai_prepared_review_plan import build_plan, transport_record
from app.services.providers.openai_response import read_response


@dataclass(frozen=True, init=False)
class OpenAIPreparedReviewProvider:
    model: str
    endpoint: str
    timeout_seconds: float
    _api_key: str = field(repr=False)
    _transport: httpx.BaseTransport | None = field(repr=False)
    _contract_json: str = field(repr=False)
    _request_json: str = field(repr=False)
    _body: bytes = field(repr=False)
    _manifest_json: str = field(repr=False)

    @property
    def provider_name(self) -> str:
        return "openai"

    def __init__(self, contract: dict[str, Any], *, endpoint: str, timeout_seconds: float,
                 api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(api_key, str) or any(ord(c) < 32 or ord(c) > 126 for c in api_key):
            raise ValueError("paper_openai_configuration_invalid")
        request_json, body, manifest = build_plan(contract, endpoint=endpoint, timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], endpoint=endpoint, timeout_seconds=float(timeout_seconds),
            _api_key=api_key.strip(), _transport=transport, _contract_json=preserved_json(contract),
            _request_json=request_json, _body=body, _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, configured=self.is_configured(),
            live_call_enabled=False, activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        """Existing dispatcher preclaim hook: local configuration, never a probe."""
        if not self.is_configured():
            raise ProviderUnavailableError("Explicit paper provider credentials are unavailable.")

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if preserved_json(asdict(request)) != self._request_json:
            raise ValueError("paper_openai_adapter_mismatch")
        self.validate_local_runtime()
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", self.endpoint, headers=headers, content=self._body) as response:
                result = read_response(response)
        try:
            prepared_review.result(json.loads(self._contract_json), result)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderOutputError("paper_openai_output_invalid") from None
        return result

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        response = self.generate_json(request)
        return transport_record(json.loads(self._contract_json), response, self.manifest())

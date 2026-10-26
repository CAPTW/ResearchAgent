"""Versioned xAI Responses paper adapter; not subscription OAuth or authority.

The fixed endpoint/identity never reuse an OpenAI credential. Local protocol
qualification does not establish live model, budget, schema or account support.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import json
import math
from typing import Any

import httpx

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderResult, ProviderUnavailableError
from app.services.providers.openai_provider import response_payload
from app.services.providers.openai_response import MAX_RESPONSE_BYTES, read_response

ENDPOINT = "https://api.x.ai/v1/responses"
ADAPTER_ID = "xrml-xai-prepared-review"
MANIFEST_SCHEMA = "XAI_PREPARED_REVIEW_WIRE_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_XAI_HTTP_TRANSPORT_V1"
MODE = "TRUSTED_PAPER_HTTP"


def _schema(value: Any) -> Any:
    # xAI anchors patterns implicitly. Python/Pydantic's nonblank search must
    # not become a one-character fullmatch. Original local schema stays intact.
    if isinstance(value, list):
        return [_schema(item) for item in value]
    if isinstance(value, dict):
        return {key: r"[\s\S]*\S[\s\S]*" if key == "pattern" and item == r"\S" else _schema(item)
                for key, item in value.items()}
    return value


def build_plan(contract: dict[str, Any], *, timeout_seconds: float) -> tuple[str, bytes, dict[str, Any]]:
    frozen = prepared_review.validate_contract(contract)
    if (frozen["provider_name"] != "xai" or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120):
        raise ValueError("paper_xai_configuration_invalid")
    request = ProviderRequest(**deepcopy(frozen["provider_request"]))
    payload = response_payload(request, frozen["model"])
    payload["text"]["format"]["schema"] = _schema(request.schema)
    payload.update(store=False, stream=False, tools=[], truncation="disabled")
    body = preserved_json(payload).encode("utf-8")
    if len(body) > 4_000_000:
        raise ValueError("paper_xai_configuration_invalid")
    manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
        provider_name="xai", endpoint=ENDPOINT, method="POST", model=frozen["model"],
        contract_sha256=sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
        body_sha256=sha(body.decode("utf-8")), body_bytes=len(body), timeout_seconds=float(timeout_seconds),
        max_response_bytes=MAX_RESPONSE_BYTES, schema_compiler="XAI_NONBLANK_V1",
        store=False, stream=False, truncation="disabled", automatic_retry=False, redirects=False, environment_proxy=False,
        output_token_limit_enforced=False, temperature_enforced=False, model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED")
    return preserved_json(frozen["provider_request"]), body, manifest


def _marker(contract: dict[str, Any], raw: str, manifest: dict[str, Any]) -> dict[str, Any]:
    _, _, expected = build_plan(contract, timeout_seconds=manifest["timeout_seconds"])
    if preserved_json(manifest) != preserved_json(expected):
        raise ValueError("paper_xai_transport_invalid")
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", manifest=expected,
        manifest_sha256=sha(preserved_json(expected)), response_sha256=sha(raw),
        authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    try:
        prepared_review.validate_record(record, packet)
        proof = record["validation"]["prepared_execution"]
        marker = record["validation"]["execution"]
        if preserved_json(marker) != preserved_json(_marker(proof["contract"], proof["response"]["raw_text"], marker["manifest"])):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_xai_transport_invalid") from None


@dataclass(frozen=True, init=False)
class XaiPreparedReviewProvider:
    model: str
    timeout_seconds: float
    _api_key: str = field(repr=False)
    _transport: httpx.BaseTransport | None = field(repr=False)
    _contract_json: str = field(repr=False)
    _request_json: str = field(repr=False)
    _body: bytes = field(repr=False)
    _manifest_json: str = field(repr=False)

    @property
    def provider_name(self) -> str:
        return "xai"

    def __init__(self, contract: dict[str, Any], *, api_key: str, timeout_seconds: float,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(api_key, str) or any(ord(c) < 32 or ord(c) > 126 for c in api_key):
            raise ValueError("paper_xai_configuration_invalid")
        request, body, manifest = build_plan(contract, timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _api_key=api_key.strip(),
            _transport=transport, _contract_json=preserved_json(contract), _request_json=request, _body=body,
            _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def describe_status(self) -> dict[str, Any]:
        return dict(provider="xai", model=self.model, configured=self.is_configured(), live_call_enabled=False,
            activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        if not self.is_configured():
            raise ProviderUnavailableError("Explicit xAI credentials are unavailable.")

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if preserved_json(asdict(request)) != self._request_json:
            raise ValueError("paper_xai_adapter_mismatch")
        self.validate_local_runtime()
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", ENDPOINT, headers=headers, content=self._body) as response:
                    # Same Responses envelope grammar, a distinct recorded provider.
                    result = replace(read_response(response), provider_name="xai")
        except httpx.HTTPError:
            raise ProviderError("paper_xai_transport_outcome_unknown") from None
        try:
            prepared_review.result(json.loads(self._contract_json), result)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderOutputError("paper_xai_output_invalid") from None
        return result

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        result = self.generate_json(request)
        contract = json.loads(self._contract_json)
        record = prepared_review.result(contract, result)
        raw = record["validation"]["prepared_execution"]["response"]["raw_text"]
        record["validation"]["execution"] = _marker(contract, raw, self.manifest())
        return record

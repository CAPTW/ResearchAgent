"""V1 paper HTTP bytes and unsigned archive; no credentials or authority.

Reuses the existing Responses V1 serializer, not Journal output or approval.
Keep this profile available for historical reads; semantic changes need V2.
"""
import math
from typing import Any

import httpx

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderRequest, ProviderResult
from app.services.providers.openai_provider import response_payload
from app.services.providers.openai_response import MAX_RESPONSE_BYTES

ADAPTER_ID = "xrml-openai-prepared-review"
MANIFEST_SCHEMA = "OPENAI_PREPARED_REVIEW_WIRE_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_HTTP_TRANSPORT_V1"
MODE = "TRUSTED_PAPER_HTTP"
MAX_REQUEST_BYTES = 4_000_000


def build_plan(contract: dict[str, Any], *, endpoint: str, timeout_seconds: float) -> tuple[str, bytes, dict[str, Any]]:
    try:
        frozen = prepared_review.validate_contract(contract)
        url = httpx.URL(endpoint)
        if (frozen["provider_name"] != "openai"
                or url.scheme != "https" or not url.host or url.username or url.password or url.query or url.fragment
                or not url.path.endswith("/responses") or endpoint != str(url)
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120):
            raise ValueError()
        request = ProviderRequest(**frozen["provider_request"])
        request_json = preserved_json(frozen["provider_request"])
        payload = response_payload(request, frozen["model"])
        payload.update(store=False, background=False, stream=False, truncation="disabled")
        body = preserved_json(payload).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            method="POST", endpoint=endpoint, model=frozen["model"],
            contract_sha256=prepared_review.sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
            body_sha256=prepared_review.sha(body.decode("utf-8")), body_bytes=len(body),
            timeout_seconds=float(timeout_seconds), max_response_bytes=MAX_RESPONSE_BYTES,
            max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
            redirects=False, environment_proxy=False, store=False, background=False, stream=False, truncation="disabled",
            automatic_retry=False, output_token_limit_enforced=False, temperature_enforced=False,
            token_limit_policy="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED")
        return request_json, body, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, httpx.InvalidURL):
        raise ValueError("paper_openai_configuration_invalid") from None


def _marker(contract: dict[str, Any], raw: str, manifest: dict[str, Any]) -> dict[str, Any]:
    _, _, expected = build_plan(contract, endpoint=manifest["endpoint"], timeout_seconds=manifest["timeout_seconds"])
    if preserved_json(manifest) != preserved_json(expected):
        raise ValueError("paper_openai_transport_invalid")
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", manifest=expected,
        manifest_sha256=prepared_review.sha(preserved_json(expected)), response_sha256=prepared_review.sha(raw),
        authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any]) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    raw = record["validation"]["prepared_execution"]["response"]["raw_text"]
    record["validation"]["execution"] = _marker(contract, raw, manifest)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    """Validate the reserved marker on both receipt write and historical read."""
    try:
        prepared_review.validate_record(record, packet)
        validation = record["validation"]
        proof, marker = validation["prepared_execution"], validation["execution"]
        expected = _marker(proof["contract"], proof["response"]["raw_text"], marker["manifest"])
        if preserved_json(marker) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_openai_transport_invalid") from None

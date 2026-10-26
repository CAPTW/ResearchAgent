"""V1 deterministic wire plan. No credentials, transport, clock or approval.

Keep this serializer and limits available for historical reads. Changes to the
wire profile need a new version/binding, never reinterpret a saved V1 manifest.
"""
from dataclasses import asdict
import hashlib
import json
import math
from typing import Any

import httpx

from app.domain.journal_execution import Prepare, SCHEMA
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderRequest
from app.services.providers.openai_provider import response_payload
from app.services.providers.openai_response import MAX_RESPONSE_BYTES

ADAPTER_ID = "xrml-openai-frozen-journal"
MAX_REQUEST_BYTES = 4_000_000


def exact_json(value: Any) -> str:
    # Wire identity preserves Unicode codepoints, without NFC normalization.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def build_wire(contract: dict[str, Any], endpoint: str, timeout_seconds: float) -> tuple[str, bytes, dict[str, Any]]:
    try:
        url = httpx.URL(endpoint)
        if (url.scheme != "https" or not url.host or url.username or url.password or url.query or url.fragment
                or not url.path.endswith("/responses") or endpoint != str(url)
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120):
            raise ValueError()
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValueError("journal_openai_configuration_invalid") from None
    try:
        contract = json.loads(exact_json(contract))  # Detach nested caller-owned values.
        spec = Prepare.model_validate(contract["specification"])
        request = ProviderRequest(**contract["provider_request"])
        if (contract["schema_version"] != SCHEMA or spec.provider_name != "openai"
                or spec.model != spec.model.strip() or request.schema_name != "JOURNAL_PROPOSAL_V1"
                or request.max_output_tokens != spec.max_output_tokens
                or request.schema != contract["packet"]["output_schema"]):
            raise ValueError()
        request_json = exact_json(asdict(request))
        payload = response_payload(request, spec.model)
        payload.update(store=False, background=False, stream=False, truncation="disabled")
        body = exact_json(payload).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError()
        manifest = dict(schema_version="OPENAI_JOURNAL_WIRE_V1", method="POST", endpoint=endpoint,
                        model=spec.model, contract_digest=digest(contract),
                        request_sha256=hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
                        body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body),
                        timeout_seconds=float(timeout_seconds), max_response_bytes=MAX_RESPONSE_BYTES,
                        redirects=False, environment_proxy=False, store=False, background=False, stream=False, truncation="disabled")
        return request_json, body, manifest
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("journal_openai_request_invalid") from None

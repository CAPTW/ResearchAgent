"""Immutable xAI API Journal wire V1; never reinterpret old OpenAI manifests.

No credentials or authority are held here. The compiler translates only xAI's
anchored nonblank pattern and preserves the original local Journal schema.
"""
from dataclasses import asdict
import math
from typing import Any

from app.domain import journal_exchange, journal_execution
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderRequest
from app.services.providers.openai_provider import response_payload
from app.services.providers.openai_response import MAX_RESPONSE_BYTES
from app.services.providers.xai_prepared_review import _schema

ENDPOINT = "https://api.x.ai/v1/responses"
ADAPTER_ID = "xrml-xai-frozen-journal"
MANIFEST_SCHEMA = "XAI_JOURNAL_WIRE_V1"
MAX_REQUEST_BYTES = 4_000_000
exact_json = journal_exchange.preserved_json


def build_wire(contract: dict[str, Any], timeout_seconds: float) -> tuple[str, bytes, dict[str, Any]]:
    """Bind exact local request and transmitted bytes to one explicit API target."""
    import json

    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120):
        raise ValueError("journal_xai_configuration_invalid")
    try:
        frozen = json.loads(exact_json(contract))
        spec = journal_execution.Prepare.model_validate(frozen["specification"])
        packet = frozen["packet"]
        request = ProviderRequest(**frozen["provider_request"])
        if (frozen["schema_version"] != journal_execution.SCHEMA or spec.provider_name != "xai"
                or spec.model != spec.model.strip() or any(ord(char) < 32 or ord(char) == 127 for char in spec.model)
                or frozen["activation"] != "UNREGISTERED_ADAPTER_CONTRACT"
                or packet["schema_version"] not in {journal_exchange.PACKET_SCHEMA, journal_exchange.REVIEW_PACKET_SCHEMA}
                or packet["job_id"] != frozen["job_id"] or packet["topic_id"] != frozen["topic_id"]
                or packet["input_digest"] != spec.expected_input_digest
                or frozen["execution_id"] != "jexec_" + digest([frozen["topic_id"], frozen["job_id"], spec.request_id])
                or packet["packet_id"] != "jpacket_" + digest({key: value for key, value in packet.items() if key != "packet_id"})
                or request.schema != packet["output_schema"] or request.schema != journal_exchange.Proposal.model_json_schema()):
            raise ValueError()
        expected = ProviderRequest(schema_name=journal_exchange.OUTPUT_SCHEMA, schema=packet["output_schema"],
            instructions="\n".join(packet["instructions"]), prompt=journal_exchange.provider_prompt(packet),
            metadata=dict(execution_id=frozen["execution_id"], input_digest=spec.expected_input_digest, packet_id=packet["packet_id"]),
            temperature=0.2, max_output_tokens=spec.max_output_tokens)
        request_json = exact_json(asdict(request))
        if request_json != exact_json(asdict(expected)):
            raise ValueError()
        payload = response_payload(request, spec.model)
        payload["text"]["format"]["schema"] = _schema(request.schema)
        # xAI lists metadata/truncation as compatibility-only unsupported fields.
        # Metadata is bound locally; packet_id is already in the exact prompt.
        payload.pop("metadata")
        payload.update(store=False, stream=False, tools=[])
        body = exact_json(payload).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            provider_name="xai", method="POST", endpoint=ENDPOINT, model=spec.model, contract_digest=digest(frozen),
            request_sha256=journal_exchange.sha(request_json), body_sha256=journal_exchange.sha(body.decode("utf-8")),
            body_bytes=len(body), timeout_seconds=float(timeout_seconds), max_response_bytes=MAX_RESPONSE_BYTES,
            max_journal_output_bytes=journal_exchange.MAX_OUTPUT_BYTES, schema_compiler="XAI_NONBLANK_V1",
            local_schema_sha256=journal_exchange.sha(exact_json(request.schema)),
            wire_schema_sha256=journal_exchange.sha(exact_json(payload["text"]["format"]["schema"])),
            store=False, stream=False, tools=[], automatic_retry=False,
            truncation_enforced=False, request_metadata="LOCAL_BINDING_ONLY_NOT_TRANSMITTED",
            redirects=False, environment_proxy=False, output_token_limit_enforced=False, temperature_enforced=False,
            output_token_scope="VISIBLE_OUTPUT_ONLY", cost_limit_enforced=False,
            token_limit_policy="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
            live_execution="NOT_VERIFIED", billing_route="API_KEY_ACCOUNT_NOT_VERIFIED")
        return request_json, body, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("journal_xai_request_invalid") from None

"""Immutable native Anthropic Messages Journal wire; credentials stay separate."""
from dataclasses import asdict
import json
import math
from typing import Any

from app.domain import journal_exchange as exchange, journal_execution
from app.domain.temporal_memory import digest
from app.services.providers.anthropic_messages import API_VERSION, ENDPOINT, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, SCHEMA_COMPILER, message_payload
from app.services.providers.base import ProviderRequest

ADAPTER_ID = "xrml-anthropic-frozen-journal"
MANIFEST_SCHEMA = "ANTHROPIC_JOURNAL_WIRE_V1"
exact_json = exchange.preserved_json


def build_wire(contract: dict[str, Any], timeout_seconds: float) -> tuple[str, bytes, dict[str, Any]]:
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120):
        raise ValueError("journal_anthropic_configuration_invalid")
    try:
        frozen = json.loads(exact_json(contract))
        spec = journal_execution.Prepare.model_validate(frozen["specification"])
        packet = frozen["packet"]
        request = ProviderRequest(**frozen["provider_request"])
        if (frozen["schema_version"] != journal_execution.SCHEMA or spec.provider_name != "anthropic"
                or spec.model != spec.model.strip() or any(ord(char) < 32 or ord(char) == 127 for char in spec.model)
                or frozen["activation"] != "UNREGISTERED_ADAPTER_CONTRACT"
                or packet["schema_version"] not in {exchange.PACKET_SCHEMA, exchange.REVIEW_PACKET_SCHEMA}
                or packet["job_id"] != frozen["job_id"] or packet["topic_id"] != frozen["topic_id"]
                or packet["input_digest"] != spec.expected_input_digest
                or frozen["execution_id"] != "jexec_" + digest([frozen["topic_id"], frozen["job_id"], spec.request_id])
                or packet["packet_id"] != "jpacket_" + digest({key: value for key, value in packet.items() if key != "packet_id"})
                or request.schema != packet["output_schema"] or request.schema != exchange.Proposal.model_json_schema()):
            raise ValueError()
        instructions = [*exchange.INSTRUCTIONS, *exchange.REVIEW_INSTRUCTIONS] if packet["schema_version"] == exchange.REVIEW_PACKET_SCHEMA else exchange.INSTRUCTIONS
        if packet["instructions"] != instructions:
            raise ValueError()
        expected = ProviderRequest(schema_name=exchange.OUTPUT_SCHEMA, schema=packet["output_schema"],
            instructions="\n".join(instructions), prompt=exchange.provider_prompt(packet),
            metadata=dict(execution_id=frozen["execution_id"], input_digest=spec.expected_input_digest, packet_id=packet["packet_id"]),
            temperature=0.2, max_output_tokens=spec.max_output_tokens)
        request_json = exact_json(asdict(request))
        if request_json != exact_json(asdict(expected)):
            raise ValueError()
        payload = message_payload(request, spec.model)
        body = exact_json(payload).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            provider_name="anthropic", method="POST", endpoint=ENDPOINT, api_version=API_VERSION, model=spec.model,
            contract_digest=digest(frozen), request_sha256=exchange.sha(request_json),
            body_sha256=exchange.sha(body.decode("utf-8")), body_bytes=len(body), timeout_seconds=float(timeout_seconds),
            max_response_bytes=MAX_RESPONSE_BYTES, max_journal_output_bytes=exchange.MAX_OUTPUT_BYTES,
            schema_compiler=SCHEMA_COMPILER, schema_constraints="LOCAL_ORIGINAL_VALIDATION",
            local_schema_sha256=exchange.sha(exact_json(request.schema)),
            wire_schema_sha256=exchange.sha(exact_json(payload["output_config"]["format"]["schema"])),
            stream=False, tools=[], automatic_retry=False, request_metadata="LOCAL_BINDING_ONLY_NOT_TRANSMITTED",
            redirects=False, environment_proxy=False, output_token_limit_enforced=False, temperature_enforced=False,
            temperature_policy="OMITTED_PROVIDER_DEFAULT",
            output_token_scope="MESSAGES_OUTPUT_TOKENS", cost_limit_enforced=False,
            token_limit_policy="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
            live_execution="NOT_VERIFIED", billing_route="API_KEY_ACCOUNT_NOT_VERIFIED",
            raw_output_encoding="CONTENT_TEXT_JSON_UTF8_V1", raw_envelope_retained=False,
            usage_input_semantics="NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE", total_tokens="NOT_REPORTED_NOT_DERIVED")
        return request_json, body, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("journal_anthropic_request_invalid") from None

"""Immutable Journal plan reusing the explicit Grok ACP process profile.

The historical paper agent/client profile remains unchanged. Journal validation
is separate; saved raw JSON is assistant message text, not the ACP transcript.
"""
from dataclasses import asdict
import json
import math
import re
from typing import Any

from app.domain import journal_exchange as exchange, journal_execution, prepared_review
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderRequest
from app.services.providers.codex_journal_plan import MAX_INPUT_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES
from app.services.providers.grok_prepared_review_plan import FIXED_ENV, agent_text, argv
from app.services.providers.grok_prepared_review_protocol import MAX_EVENTS

ADAPTER_ID = "xrml-grok-frozen-journal"
MANIFEST_SCHEMA = "GROK_JOURNAL_ACP_V1"
SURFACE = "grok_cli"
MAX_JOURNAL_OUTPUT_BYTES = min(exchange.MAX_OUTPUT_BYTES, prepared_review.MAX_OUTPUT_BYTES)


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any],
               timeout_seconds: float) -> tuple[str, dict[str, Any]]:
    try:
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120
                or set(runtime_identity) != {"launcher_sha256", "environment_sha256", "workspace_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value) for value in runtime_identity.values())):
            raise ValueError()
        frozen = json.loads(exchange.preserved_json(contract))
        spec = journal_execution.Prepare.model_validate(frozen["specification"])
        packet = frozen["packet"]
        request = ProviderRequest(**frozen["provider_request"])
        if (frozen["schema_version"] != journal_execution.SCHEMA or spec.provider_name != SURFACE
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", spec.model)
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
        request_text = exchange.preserved_json(asdict(request))
        if request_text != exchange.preserved_json(asdict(expected)) or len(request_text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            runtime_profile=MANIFEST_SCHEMA, provider_name=SURFACE, fixed_environment=dict(FIXED_ENV), protocol_version=1,
            contract_digest=digest(frozen), contract_sha256=exchange.sha(exchange.preserved_json(frozen)),
            request_sha256=exchange.sha(request_text), model=spec.model, runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), "{attempt}/paper-agent.md", spec.model),
            prompt_sha256=exchange.sha(request_text), prompt_bytes=len(request_text.encode("utf-8")),
            schema_sha256=exchange.sha(exchange.preserved_json(request.schema)),
            agent_sha256=exchange.sha(agent_text(request.instructions)),
            agent_profile="EXISTING_PAPER_PROFILE_WITH_FROZEN_JOURNAL_INSTRUCTIONS",
            timeout_seconds=float(timeout_seconds), max_stdout_bytes=MAX_STREAM_BYTES, max_stderr_bytes=MAX_STDERR_BYTES,
            max_journal_output_bytes=MAX_JOURNAL_OUTPUT_BYTES, max_events=MAX_EVENTS,
            prompt_count=1, client_capabilities={}, client_info=dict(name="xrml-prepared-paper", version="1"),
            authentication="OFFICIAL_CLI_CACHED_TOKEN_ONLY_NOT_VERIFIED", billing_route="NOT_VERIFIED",
            session_retention="CLI_MANAGED_PROFILE_STORAGE_NOT_ISOLATED", model_selection="ACP_CONFIG_OPTION_MODEL_EXACT",
            structured_output="PROMPT_REQUESTED_LOCAL_VALIDATION_ONLY", completion="CORRELATED_PROMPT_STOP_REASON",
            process_cleanup="HOST_TERMINATES_OWNED_ACP_CHILD_AFTER_TERMINAL",
            automatic_retry=False, xrml_process_relaunch=False, remote_call_count_enforced=False,
            internal_retries="CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks="NOT_ISOLATED_NOT_LIVE_VERIFIED",
            global_config_and_mcp="NOT_ISOLATED_NOT_LIVE_VERIFIED", tool_isolation="NOT_ESTABLISHED",
            output_rejection_prevents_side_effects=False, physical_remote_cancellation="NOT_ESTABLISHED",
            output_token_limit_enforced=False, temperature_enforced=False, cost_limit_enforced=False,
            model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED", live_execution="NOT_VERIFIED",
            raw_journal_encoding="ACP_AGENT_MESSAGE_TEXT_UTF8_V1", journal_result_storage="AGENT_MESSAGE_TEXT_ONLY",
            raw_envelope_retained=False, token_usage="NOT_REPORTED_BY_SUPPORTED_ACP_PROFILE")
        return request_text, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("journal_grok_configuration_invalid") from None

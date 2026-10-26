"""Immutable Journal plan for the existing explicit Antigravity process profile.

The historical paper profile and its agent name/description remain unchanged.
Output checks are retrospective observations, not proof of tool isolation.
"""
from dataclasses import asdict
import json
import math
import re
from typing import Any

from app.domain import journal_exchange as exchange, journal_execution, prepared_review
from app.domain.temporal_memory import digest
from app.services.providers.antigravity_prepared_review_plan import AGENT_NAME, FIXED_ENV, agent_text, argv
from app.services.providers.base import ProviderRequest
from app.services.providers.codex_journal_plan import MAX_INPUT_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES

ADAPTER_ID = "xrml-antigravity-frozen-journal"
MANIFEST_SCHEMA = "ANTIGRAVITY_JOURNAL_CLI_V1"
SURFACE = "antigravity_cli"
MAX_JOURNAL_OUTPUT_BYTES = min(exchange.MAX_OUTPUT_BYTES, prepared_review.MAX_OUTPUT_BYTES)


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any],
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
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
        request_json = exchange.preserved_json(asdict(request))
        stdin = exchange.preserved_json(dict(event="user", message=dict(content=request_json))) + "\n"
        schema = exchange.preserved_json(request.schema)
        agent = agent_text(request.instructions)
        if request_json != exchange.preserved_json(asdict(expected)) or len(stdin.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            runtime_profile=MANIFEST_SCHEMA, provider_name=SURFACE, fixed_environment=dict(FIXED_ENV),
            contract_digest=digest(frozen), contract_sha256=exchange.sha(exchange.preserved_json(frozen)),
            request_sha256=exchange.sha(request_json), model=spec.model, reasoning_effort="low", runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), "{attempt}/output-schema.json", spec.model, float(timeout_seconds)),
            stdin_sha256=exchange.sha(stdin), stdin_bytes=len(stdin.encode("utf-8")), input_turns=1,
            schema_sha256=exchange.sha(schema), agent_sha256=exchange.sha(agent), agent_name=AGENT_NAME,
            agent_profile="EXISTING_PAPER_PROFILE_WITH_FROZEN_JOURNAL_INSTRUCTIONS",
            system_prompt_sha256=exchange.sha(request.instructions), timeout_seconds=float(timeout_seconds),
            max_stdout_bytes=MAX_STREAM_BYTES, max_stderr_bytes=MAX_STDERR_BYTES,
            max_journal_output_bytes=MAX_JOURNAL_OUTPUT_BYTES,
            automatic_retry=False, xrml_process_relaunch=False, remote_call_count_enforced=False,
            internal_retries="CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks="NOT_ISOLATED_NOT_LIVE_VERIFIED",
            global_config_and_mcp="NOT_ISOLATED_NOT_LIVE_VERIFIED", windows_sandbox="REQUESTED_NOT_LIVE_VERIFIED",
            output_token_limit_enforced=False, temperature_enforced=False, cost_limit_enforced=False,
            token_limit_policy="REQUEST_METADATA_ONLY_NOT_ENFORCED",
            tool_isolation="REQUESTED_AND_OUTPUT_CHECKED_NOT_LIVE_VERIFIED", output_rejection_prevents_side_effects=False,
            model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED", authentication="OFFICIAL_CLI_OWNED_NOT_VERIFIED",
            billing_route="NOT_VERIFIED", live_execution="NOT_VERIFIED", step_text="NONCANONICAL_STREAM_TELEMETRY",
            raw_journal_encoding="RESULT_RESPONSE_JSON_UTF8_V1", journal_result_storage="RESULT_RESPONSE_ONLY",
            raw_envelope_retained=False)
        return stdin, schema, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("journal_antigravity_configuration_invalid") from None

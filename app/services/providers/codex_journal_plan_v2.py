"""Immutable V2 Journal plan with exact source/request binding; no local I/O."""
from dataclasses import asdict
import json
from typing import Any

from app.domain import journal_exchange as exchange, journal_execution
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderRequest
from app.services.providers.codex_cli_profile_v2 import argv, profile_metadata
from app.services.providers.codex_journal_plan import ADAPTER_ID, build_plan as build_v1, sha

MANIFEST_SCHEMA = "CODEX_JOURNAL_CLI_V2"
ADAPTER_VERSION = "2"


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any], reasoning_effort: str,
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
    try:
        frozen = json.loads(exchange.preserved_json(contract))
        # Reuse the frozen compiler, bounds, model/effort and identity validation.
        stdin, schema, manifest = build_v1(frozen, runtime_identity=runtime_identity,
            reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
        spec = journal_execution.Prepare.model_validate(frozen["specification"])
        packet = frozen["packet"]
        if (frozen["activation"] != "UNREGISTERED_ADAPTER_CONTRACT"
                or packet["schema_version"] not in {exchange.PACKET_SCHEMA, exchange.REVIEW_PACKET_SCHEMA}
                or packet["job_id"] != frozen["job_id"] or packet["topic_id"] != frozen["topic_id"]
                or packet["input_digest"] != spec.expected_input_digest
                or frozen["execution_id"] != "jexec_" + digest([frozen["topic_id"], frozen["job_id"], spec.request_id])
                or packet["packet_id"] != "jpacket_" + digest({key: value for key, value in packet.items() if key != "packet_id"})
                or packet["output_schema"] != exchange.Proposal.model_json_schema()):
            raise ValueError()
        instructions = ([*exchange.INSTRUCTIONS, *exchange.REVIEW_INSTRUCTIONS]
                        if packet["schema_version"] == exchange.REVIEW_PACKET_SCHEMA else exchange.INSTRUCTIONS)
        expected = ProviderRequest(schema_name=exchange.OUTPUT_SCHEMA, schema=packet["output_schema"],
            instructions="\n".join(instructions), prompt=exchange.provider_prompt(packet),
            metadata=dict(execution_id=frozen["execution_id"], input_digest=spec.expected_input_digest,
                          packet_id=packet["packet_id"]), temperature=0.2, max_output_tokens=spec.max_output_tokens)
        if packet["instructions"] != instructions or stdin != exchange.preserved_json(asdict(expected)):
            raise ValueError()
        manifest.update(profile_metadata())
        manifest.update(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
            contract_sha256=sha(exchange.preserved_json(frozen)), request_sha256=sha(stdin),
            argv_shape=argv(("{explicit_launcher}",), "{attempt_dir}", spec.model, reasoning_effort))
        return stdin, schema, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("journal_codex_configuration_invalid") from None

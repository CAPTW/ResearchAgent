"""V1 adapter contract for one frozen journal request; no provider selection/activation."""
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import journal_exchange as exchange
from app.domain.local_journal import references
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderRequest, ProviderResult

SCHEMA = "FROZEN_JOURNAL_EXECUTION_V1"
RESULT_SCHEMA = "JOURNAL_ADAPTER_RESULT_V1"
ACTIVE = {"LEASED", "RUNNING"}
TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED"}


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    action: Literal["cancel", "recover"]
    expected_sequence: int = Field(ge=0, le=100)


class ControlReceipt(Control):
    schema_version: Literal["JOURNAL_EXECUTION_CONTROL_V1"] = "JOURNAL_EXECUTION_CONTROL_V1"
    actor_id: str = Field(min_length=1, max_length=500, pattern=r"\S")


def validate_control(previous: dict[str, Any], event: dict[str, Any]) -> None:
    """Optional versioned receipt; old worker events remain byte-for-byte valid."""
    if "control" not in event:
        return
    receipt = ControlReceipt.model_validate(event["control"])
    if receipt.model_dump() != event["control"] or receipt.expected_sequence != previous["sequence"]:
        raise ValueError("journal_execution_integrity_invalid")
    if receipt.action == "cancel":
        valid = (event["state"] == "CANCELLED" and event["reason"] == "result_acceptance_cancelled"
                 and event["outcome_unknown"] == (previous["state"] == "RUNNING" or previous["outcome_unknown"]))
    else:
        started = previous["state"] == "RUNNING"
        valid = (previous["state"] in ACTIVE and event["state"] == ("INTERRUPTED" if started else "FAILED")
                 and event["reason"] == ("lease_expired_after_dispatch" if started else "lease_expired_before_dispatch")
                 and event["outcome_unknown"] == started
                 and datetime.fromisoformat(event["at"]) >= datetime.fromisoformat(previous["deadline"]))
    if not valid:
        raise ValueError("journal_execution_integrity_invalid")


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    model: str = Field(min_length=1, max_length=200, pattern=r"\S")
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_output_tokens: int = Field(default=4000, ge=256, le=8000)


def prepare(job: dict[str, Any], body: Prepare, actor: str, created: str) -> dict[str, Any]:
    if not actor.strip():
        raise ValueError("journal_execution_invalid")
    if body.expected_input_digest != job["input_digest"]:
        raise ValueError("journal_execution_input_changed")
    frozen = exchange.packet(job)
    identity = "jexec_" + digest([job["topic_id"], job["job_id"], body.request_id])
    request = ProviderRequest(
        schema_name=exchange.OUTPUT_SCHEMA, schema=frozen["output_schema"],
        instructions="\n".join(frozen["instructions"]),
        # The V1 exchange packet remains unchanged. Execution provenance lives
        # in this separate envelope, not in a rewritten historical packet.
        prompt=exchange.provider_prompt(frozen),
        metadata=dict(execution_id=identity, input_digest=job["input_digest"], packet_id=frozen["packet_id"]),
        temperature=0.2, max_output_tokens=body.max_output_tokens)
    return dict(schema_version=SCHEMA, execution_id=identity, topic_id=job["topic_id"], job_id=job["job_id"],
                actor_id=actor, created_at=created, specification=body.model_dump(), packet=frozen,
                provider_request=asdict(request), activation="UNREGISTERED_ADAPTER_CONTRACT")


def validate_contract(value: dict[str, Any], job: dict[str, Any]) -> None:
    body = Prepare.model_validate(value["specification"])
    packet = value["packet"]
    request = value["provider_request"]
    try:
        exchange.validate_packet_binding(packet, job)
        if packet.get("schema_version") == exchange.REVIEW_PACKET_SCHEMA:
            expected = ProviderRequest(schema_name=exchange.OUTPUT_SCHEMA, schema=packet["output_schema"],
                instructions="\n".join(packet["instructions"]), prompt=exchange.provider_prompt(packet),
                metadata=dict(execution_id=value["execution_id"], input_digest=job["input_digest"], packet_id=packet["packet_id"]),
                temperature=0.2, max_output_tokens=body.max_output_tokens)
            if exchange.preserved_json(request) != exchange.preserved_json(asdict(expected)):
                raise ValueError("journal_execution_integrity_invalid")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("journal_execution_integrity_invalid") from None
    if (value["schema_version"] != SCHEMA or value["job_id"] != job["job_id"] or value["topic_id"] != job["topic_id"]
            or body.expected_input_digest != job["input_digest"]
            or value["execution_id"] != "jexec_" + digest([job["topic_id"], job["job_id"], body.request_id])
            or packet["job_id"] != job["job_id"] or packet["topic_id"] != job["topic_id"]
            or packet["input_digest"] != job["input_digest"] or packet["references"] != references(job)
            or packet["packet_id"] != "jpacket_" + digest({k: v for k, v in packet.items() if k != "packet_id"})
            or request["schema"] != packet["output_schema"] or request["schema_name"] != exchange.OUTPUT_SCHEMA):
        raise ValueError("journal_execution_integrity_invalid")


def result(contract: dict[str, Any], response: ProviderResult) -> dict[str, Any]:
    """Preserve adapter-reported identity, not independent model execution proof."""
    if (response.provider_name != contract["specification"]["provider_name"] or not response.model.strip()
            or len(response.model) > 200 or (response.response_id is not None and
                                           (not isinstance(response.response_id, str) or len(response.response_id) > 200))):
        raise ValueError("journal_execution_output_invalid")
    raw = response.raw_text
    if not isinstance(raw, str):
        raise ValueError("journal_execution_output_invalid")
    proposal = exchange.parse(raw)
    if proposal.model_dump() != response.payload:
        raise ValueError("journal_execution_output_invalid")
    citations = exchange.validate(proposal, contract["packet"])
    # Only bounded usage counts, never arbitrary adapter diagnostics/headers.
    usage_keys = {"input_tokens", "output_tokens", "total_tokens"}
    if response.provider_name == "anthropic":
        usage_keys |= {"cache_creation_input_tokens", "cache_read_input_tokens"}
    usage = {key: val for key, val in response.usage.items()
             if key in usage_keys and type(val) is int and 0 <= val <= 10**9}
    return dict(schema_version=RESULT_SCHEMA, execution_id=contract["execution_id"], packet_id=contract["packet"]["packet_id"],
                raw_json=raw, raw_sha256=exchange.sha(raw), proposal=proposal.model_dump(), citations=citations,
                provider_reported=dict(provider_name=response.provider_name, model=response.model,
                                       response_id=response.response_id, usage=usage),
                validation=dict(binding="VERIFIED", execution="ADAPTER_RETURNED", independent_live_proof="NOT_VERIFIED",
                                semantic_support="NOT_ASSESSED", scientific_assessment="NOT_ASSESSED", contradictions="NOT_ASSESSED"))


def validate_result(value: dict[str, Any], contract: dict[str, Any]) -> None:
    reported = value["provider_reported"]
    rebuilt = result(contract, ProviderResult(**reported, payload=value["proposal"], raw_text=value["raw_json"]))
    if value != rebuilt:
        raise ValueError("journal_execution_integrity_invalid")

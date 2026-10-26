"""Exact, short-lived journal admission records; no authority is created here.

Only a trusted server-side authority may supply a Grant. In-memory proof records
are evidence inputs, not a trusted source by themselves. V2 receipts can bind a
stored server record, rechecked by the repositories; default dispatch still denies.
Tests use synthetic records and must never claim live qualification.
"""
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.temporal_memory import digest
from app.domain.journal_exchange import preserved_json
from app.services.provider_output_class_proof import ProviderProofRecord, verify_provider_proof


class Dispatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_sequence: int = Field(ge=0, le=100)
    acknowledge_unknown: bool = False


class AdapterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    surface_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    adapter_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    adapter_version: str = Field(min_length=1, max_length=100, pattern=r"\S")
    command_shape_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["JOURNAL_DISPATCH_GRANT_V1"] = "JOURNAL_DISPATCH_GRANT_V1"
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    actor_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version: str = Field(min_length=1, max_length=100, pattern=r"\S")
    granted_at: datetime
    expires_at: datetime
    max_attempts: int = Field(ge=1, le=3)
    max_reserved_output_tokens: int = Field(ge=256, le=24000)
    proof: ProviderProofRecord


def validate_grant(grant: Grant, contract: dict[str, Any], actor: str, binding: AdapterBinding,
                   now: datetime) -> None:
    if (grant.actor_id != actor or grant.contract_digest != digest(contract)
            or contract["provider_request"]["schema_name"] != "JOURNAL_PROPOSAL_V1"):
        raise ValueError("journal_dispatch_authority_mismatch")
    if (any(value.tzinfo is None or value.utcoffset() is None for value in (now, grant.granted_at, grant.expires_at))
            or not grant.granted_at <= now < grant.expires_at
            or not timedelta(0) < grant.expires_at - grant.granted_at <= timedelta(minutes=5)):
        raise ValueError("journal_dispatch_authority_expired")
    spec = contract["specification"]
    proof = verify_provider_proof(
        grant.proof, evaluated_at=now, policy_version=grant.policy_version,
        provider_id=spec["provider_name"], surface_id=binding.surface_id,
        stage="report_draft_assist", output_class="report_draft",
        artifact_sha256=digest(contract["provider_request"]), model=spec["model"],
        command_shape_sha256=binding.command_shape_sha256, adapter_id=binding.adapter_id,
        adapter_version=binding.adapter_version)
    if not proof.eligible:
        raise ValueError("journal_dispatch_proof_rejected")


class ReceiptFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request: Dispatch
    actor_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    grant: Grant
    binding: AdapterBinding
    reserved_output_tokens: int = Field(ge=256, le=8000)


class Receipt(ReceiptFields):
    schema_version: Literal["JOURNAL_DISPATCH_RECEIPT_V1"] = "JOURNAL_DISPATCH_RECEIPT_V1"


class AuthorityReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RegisteredReceipt(ReceiptFields):
    schema_version: Literal["JOURNAL_DISPATCH_RECEIPT_V2"] = "JOURNAL_DISPATCH_RECEIPT_V2"
    authority: AuthorityReference


def read_receipt(value: dict[str, Any]) -> Receipt | RegisteredReceipt:
    # Strict JSON parsing accepts only the documented serialized datetime/enum forms.
    model = RegisteredReceipt if value.get("schema_version") == "JOURNAL_DISPATCH_RECEIPT_V2" else Receipt
    result = model.model_validate_json(preserved_json(value))
    if result.model_dump(mode="json") != value:
        raise ValueError("journal_dispatch_receipt_invalid")
    if isinstance(result, RegisteredReceipt) and result.authority.authorization_id != result.grant.authorization_id:
        raise ValueError("journal_dispatch_receipt_invalid")
    return result


def validate_receipt(contract: dict[str, Any], previous: dict[str, Any], event: dict[str, Any]) -> None:
    if "dispatch" not in event:
        if "adapter_manifest" in event:
            raise ValueError("journal_dispatch_receipt_invalid")
        return  # Historical unregistered attempts keep their original contract.
    receipt = read_receipt(event["dispatch"])
    # Optional additive event attachment; historical receipt/contract IDs stay
    # unchanged. This new concrete adapter always requires its exact V1 plan.
    from app.services.providers.openai_journal_wire import ADAPTER_ID, build_wire, exact_json
    from app.services.providers.codex_journal_plan import ADAPTER_ID as CLI_ID, build_plan
    from app.services.providers.codex_journal_plan_v2 import MANIFEST_SCHEMA as CLI_V2_SCHEMA, build_plan as build_cli_v2_plan
    from app.services.providers.xai_journal_wire import ADAPTER_ID as XAI_ID, build_wire as build_xai_wire
    from app.services.providers.anthropic_journal_wire import ADAPTER_ID as ANTHROPIC_ID, build_wire as build_anthropic_wire
    from app.services.providers.claude_journal_plan import ADAPTER_ID as CLAUDE_ID, build_plan as build_claude_plan
    from app.services.providers.antigravity_journal_plan import ADAPTER_ID as AGY_ID, build_plan as build_agy_plan
    from app.services.providers.grok_journal_plan import ADAPTER_ID as GROK_ID, build_plan as build_grok_plan
    manifest = event.get("adapter_manifest")
    if receipt.binding.adapter_id in {ADAPTER_ID, CLI_ID, XAI_ID, CLAUDE_ID, AGY_ID, GROK_ID, ANTHROPIC_ID} or "adapter_manifest" in event:
        if not isinstance(manifest, dict):
            raise ValueError("journal_dispatch_receipt_invalid")
        adapter_version = "1"
        if receipt.binding.adapter_id == ADAPTER_ID:
            _, _, expected = build_wire(contract, manifest["endpoint"], manifest["timeout_seconds"])
            surface = "api"
        elif receipt.binding.adapter_id == CLI_ID:
            if receipt.binding.adapter_version == "1" and manifest.get("schema_version") == "CODEX_JOURNAL_CLI_V1":
                cli_plan = build_plan
            elif receipt.binding.adapter_version == "2" and manifest.get("schema_version") == CLI_V2_SCHEMA:
                cli_plan = build_cli_v2_plan
                adapter_version = "2"
            else:
                raise ValueError("journal_dispatch_receipt_invalid")
            _, _, expected = cli_plan(contract, runtime_identity=manifest["runtime_identity"],
                reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
            surface = "codex_cli"
        elif receipt.binding.adapter_id == XAI_ID:
            _, _, expected = build_xai_wire(contract, manifest["timeout_seconds"])
            surface = "api"
        elif receipt.binding.adapter_id == ANTHROPIC_ID:
            _, _, expected = build_anthropic_wire(contract, manifest["timeout_seconds"])
            surface = "api"
        elif receipt.binding.adapter_id == CLAUDE_ID:
            _, _, expected = build_claude_plan(contract, runtime_identity=manifest["runtime_identity"],
                timeout_seconds=manifest["timeout_seconds"])
            surface = "claude_code"
        elif receipt.binding.adapter_id == GROK_ID:
            _, expected = build_grok_plan(contract, runtime_identity=manifest["runtime_identity"],
                timeout_seconds=manifest["timeout_seconds"])
            surface = "grok_cli"
        elif receipt.binding.adapter_id == AGY_ID:
            _, _, expected = build_agy_plan(contract, runtime_identity=manifest["runtime_identity"],
                timeout_seconds=manifest["timeout_seconds"])
            surface = "antigravity_cli"
        else:
            raise ValueError("journal_dispatch_receipt_invalid")
        if (exact_json(manifest) != exact_json(expected) or digest(manifest) != receipt.binding.command_shape_sha256
                or receipt.binding.surface_id != surface or receipt.binding.adapter_version != adapter_version):
            raise ValueError("journal_dispatch_receipt_invalid")
    validate_grant(receipt.grant, contract, receipt.actor_id, receipt.binding, datetime.fromisoformat(event["at"]))
    tokens = contract["specification"]["max_output_tokens"]
    if (event["state"] != "LEASED" or receipt.request.expected_sequence != previous["sequence"]
            or receipt.reserved_output_tokens != tokens
            or event["attempt"] > receipt.grant.max_attempts
            or event["attempt"] * tokens > receipt.grant.max_reserved_output_tokens
            or event["retry_acknowledged_unknown"] != (previous["outcome_unknown"] and receipt.request.acknowledge_unknown)
            or (previous["outcome_unknown"] and not receipt.request.acknowledge_unknown)):
        raise ValueError("journal_dispatch_receipt_invalid")

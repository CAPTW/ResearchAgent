"""Pure, fail-closed provider-output acceptance authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.services.provider_orchestration import (
    ProviderExecutionRequest,
    ProviderExecutionResultEnvelope,
    ProviderRoutingDecision,
    ProviderSourceIdentity,
)
from app.services.provider_output_class_proof import (
    ProviderProofRecord,
    verify_provider_proof,
)


_ACCEPTANCE_DOMAIN = b"xrml-provider-output-acceptance-v1\0"


@dataclass(frozen=True)
class ProviderOutputAcceptanceDecision:
    acceptance_id: str
    routing_decision_id: str
    request_id: str
    execution_id: str
    proof_id: str
    accepted: bool
    persistence_eligible: bool
    reasons: tuple[str, ...]
    completed_validation_steps: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "acceptanceId": self.acceptance_id,
            "routingDecisionId": self.routing_decision_id,
            "requestId": self.request_id,
            "executionId": self.execution_id,
            "proofId": self.proof_id,
            "accepted": self.accepted,
            "persistenceEligible": self.persistence_eligible,
            "reasons": list(self.reasons),
            "completedValidationSteps": list(self.completed_validation_steps),
        }


def _acceptance_id(
    routing_decision: ProviderRoutingDecision,
    request: ProviderExecutionRequest,
    result: ProviderExecutionResultEnvelope,
    proof_id: str,
    reasons: tuple[str, ...],
) -> str:
    payload = {
        "executionId": result.execution_id,
        "proofId": proof_id,
        "reasons": list(reasons),
        "requestId": request.request_id,
        "routingDecisionId": routing_decision.decision_id,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_ACCEPTANCE_DOMAIN + canonical).hexdigest()


def _decision(
    *,
    routing_decision: ProviderRoutingDecision,
    request: ProviderExecutionRequest,
    result: ProviderExecutionResultEnvelope,
    proof_id: str,
    reasons: tuple[str, ...],
    completed: tuple[str, ...],
) -> ProviderOutputAcceptanceDecision:
    accepted = not reasons
    return ProviderOutputAcceptanceDecision(
        acceptance_id=_acceptance_id(routing_decision, request, result, proof_id, reasons),
        routing_decision_id=routing_decision.decision_id,
        request_id=request.request_id,
        execution_id=result.execution_id,
        proof_id=proof_id,
        accepted=accepted,
        persistence_eligible=accepted,
        reasons=reasons,
        completed_validation_steps=completed,
    )


def _source_reasons(
    expected: tuple[ProviderSourceIdentity, ...],
    actual: tuple[ProviderSourceIdentity, ...],
) -> tuple[str, ...]:
    if not actual:
        return ("source_identity_missing",)
    expected_by_identity = {(item.source_kind, item.source_id): item.source_sha256 for item in expected}
    actual_by_identity = {(item.source_kind, item.source_id): item.source_sha256 for item in actual}
    if expected_by_identity.keys() != actual_by_identity.keys():
        return ("source_identity_mismatch",)
    if expected_by_identity != actual_by_identity:
        return ("source_hash_mismatch",)
    return ()


def evaluate_provider_output_acceptance(
    *,
    routing_decision: ProviderRoutingDecision,
    execution_request: ProviderExecutionRequest,
    result_envelope: ProviderExecutionResultEnvelope,
    proof_record: ProviderProofRecord | None,
    evaluated_at: datetime,
    payload_hash_validator: Callable[[str], bool],
    result_schema_validator: Callable[[ProviderExecutionResultEnvelope], bool],
    source_catalog_validator: Callable[[tuple[ProviderSourceIdentity, ...]], bool],
    evidence_citation_validator: Callable[[tuple[str, ...], tuple[str, ...]], tuple[bool, bool]],
    domain_validator: Callable[[ProviderExecutionResultEnvelope], tuple[bool, bool]],
    redaction_side_effect_validator: Callable[[ProviderExecutionResultEnvelope], tuple[bool, bool]],
) -> ProviderOutputAcceptanceDecision:
    """Validate one result in the mandated order without executing or persisting."""
    request = execution_request
    result = result_envelope
    completed: list[str] = []
    proof_id = proof_record.proof_id if isinstance(proof_record, ProviderProofRecord) else ""

    step = "result_structure_status"
    if result.status != "succeeded":
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("result_unsuccessful",), completed=tuple(completed))
    if result.timed_out:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("result_timeout",), completed=tuple(completed))
    if result.truncated:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("result_truncated",), completed=tuple(completed))
    completed.append(step)

    step = "request_routing_identity"
    if result.request_id != request.request_id:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("request_identity_mismatch",), completed=tuple(completed))
    if request.routing_decision_id != routing_decision.decision_id or result.routing_decision_id != routing_decision.decision_id:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("routing_decision_mismatch",), completed=tuple(completed))
    completed.append(step)

    step = "provider_surface_stage_output_identity"
    if result.provider_id != request.provider_id or request.provider_id != routing_decision.selected_provider_id:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("provider_mismatch",), completed=tuple(completed))
    if result.surface_id != request.surface_id or request.surface_id != routing_decision.selected_surface_id:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("surface_mismatch",), completed=tuple(completed))
    if result.stage != request.stage or request.stage != routing_decision.stage:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("stage_mismatch",), completed=tuple(completed))
    if result.output_class != request.output_class or request.output_class != routing_decision.output_class:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("output_class_mismatch",), completed=tuple(completed))
    completed.append(step)

    step = "proof_identity"
    if not isinstance(proof_record, ProviderProofRecord):
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id="", reasons=("proof_missing",), completed=tuple(completed))
    verification = verify_provider_proof(
        proof_record,
        evaluated_at=evaluated_at,
        policy_version=routing_decision.policy_version,
        provider_id=request.provider_id,
        surface_id=request.surface_id,
        stage=request.stage,
        output_class=request.output_class,
        artifact_sha256=request.artifact_sha256,
        model=request.model,
        command_shape_sha256=request.command_shape_sha256,
        adapter_id=request.adapter_id,
        adapter_version=request.adapter_version,
    )
    proof_reasons = list(verification.reasons)
    if request.proof_id != proof_record.proof_id or result.proof_id != proof_record.proof_id or routing_decision.selected_proof_id != proof_record.proof_id:
        proof_reasons.append("proof_scope_mismatch")
    reason_map = {
        "proof_model_mismatch": "model_mismatch",
        "proof_command_mismatch": "command_shape_mismatch",
        "proof_adapter_mismatch": "adapter_mismatch",
        "proof_hash_mismatch": "proof_scope_mismatch",
    }
    proof_reasons = list(dict.fromkeys(reason_map.get(reason, reason) for reason in proof_reasons))
    if proof_reasons:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_record.proof_id, reasons=tuple(proof_reasons), completed=tuple(completed))
    completed.append(step)

    step = "adapter_model_command_constraints"
    if result.adapter_id != request.adapter_id or result.adapter_version != request.adapter_version:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("adapter_mismatch",), completed=tuple(completed))
    if result.model != request.model:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("model_mismatch",), completed=tuple(completed))
    if result.command_shape_sha256 != request.command_shape_sha256:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("command_shape_mismatch",), completed=tuple(completed))
    completed.append(step)

    step = "source_identities"
    source_reasons = _source_reasons(request.source_identities, result.source_identities)
    if source_reasons:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=source_reasons, completed=tuple(completed))
    if not source_catalog_validator(result.source_identities):
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("source_hash_mismatch",), completed=tuple(completed))
    completed.append(step)

    step = "payload_hash_schema"
    if len(result.payload_sha256) != 64 or not payload_hash_validator(result.payload_sha256):
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("payload_hash_mismatch",), completed=tuple(completed))
    if result.schema_id != request.result_schema_id or not result_schema_validator(result):
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("result_schema_invalid",), completed=tuple(completed))
    completed.append(step)

    step = "evidence_citations"
    evidence_ok, citations_ok = evidence_citation_validator(result.evidence_ids, result.citation_ids)
    if not result.evidence_ids or not evidence_ok:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("evidence_unresolved",), completed=tuple(completed))
    if not result.citation_ids or not citations_ok:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("citation_unresolved",), completed=tuple(completed))
    completed.append(step)

    step = "domain_validation"
    claims_supported, domain_valid = domain_validator(result)
    if not claims_supported:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("unsupported_claims",), completed=tuple(completed))
    if not domain_valid:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("domain_validation_failed",), completed=tuple(completed))
    completed.append(step)

    step = "redaction_side_effect_policy"
    redaction_ok, side_effect_ok = redaction_side_effect_validator(result)
    if not result.redaction_applied or not redaction_ok:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("redaction_policy_failed",), completed=tuple(completed))
    if result.side_effect_detected or not side_effect_ok:
        return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=("side_effect_detected",), completed=tuple(completed))
    completed.append(step)

    return _decision(routing_decision=routing_decision, request=request, result=result, proof_id=proof_id, reasons=(), completed=tuple(completed))

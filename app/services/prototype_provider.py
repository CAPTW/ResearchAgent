"""Development-only Provider controls for the local Prototype.

This module never invokes a Provider.  It exercises the existing Codex SDK
weekly-report schema validator and the canonical output-acceptance authority
with bounded recorded fixtures, then stores only safe digests and decisions.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.services.provider_orchestration import (
    ProviderExecutionRequest,
    ProviderExecutionResultEnvelope,
    ProviderRoutingDecision,
    ProviderSourceIdentity,
)
from app.services.provider_output_acceptance import evaluate_provider_output_acceptance
from app.services.provider_output_class_proof import (
    ProviderProofIdentity,
    ProviderProofLevel,
    ProviderProofRecord,
    compute_provider_proof_id,
)
from app.services.providers import codex_sdk_provider

PROVIDER_ID = "codex"
SURFACE_ID = codex_sdk_provider.PROFILE_ID
STAGE = "report_draft_assist"
OUTPUT_CLASS = "report_draft"
PRODUCT_OUTPUT_CLASS = codex_sdk_provider.WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS
POLICY_VERSION = "prototype-provider-policy-v1"
ACCEPTANCE_POLICY = "exact_output_acceptance_v1"
CONTROL_MODEL = "recorded-control-v1"
ADAPTER_VERSION = "prototype-control-v1"
REQUEST_SCHEMA_ID = "prototype-report-draft-request-v1"
RESULT_SCHEMA_ID = "prototype-report-draft-result-v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def evaluate_development_activation(*, proof_eligible: bool = False) -> dict[str, Any]:
    """Return the exact fail-closed activation state without executing anything."""

    expected = {
        "XRML_PROTOTYPE_PROVIDER_ID": PROVIDER_ID,
        "XRML_PROTOTYPE_PROVIDER_SURFACE": SURFACE_ID,
        "XRML_PROTOTYPE_PROVIDER_STAGE": STAGE,
        "XRML_PROTOTYPE_PROVIDER_OUTPUT_CLASS": OUTPUT_CLASS,
        "XRML_PROTOTYPE_PROVIDER_ACCEPTANCE_POLICY": ACCEPTANCE_POLICY,
    }
    reasons: list[str] = []
    enabled = os.getenv("XRML_PROTOTYPE_PROVIDER_ENABLED", "").strip() == "1"
    if not enabled:
        reasons.append("prototype_provider_disabled")
    for key, value in expected.items():
        if os.getenv(key, "").strip() != value:
            reasons.append(f"exact_activation_mismatch:{key.lower()}")
    if not proof_eligible:
        reasons.append("exact_output_class_proof_missing")
    prototype_mode = os.getenv("XRML_PROTOTYPE_MODE", "").strip() == "1"
    if not prototype_mode:
        reasons.append("prototype_mode_required")
    return {
        "activation_state": "eligible" if not reasons else "disabled",
        "eligible": not reasons,
        "prototype_mode": prototype_mode,
        "provider_id": PROVIDER_ID,
        "surface_id": SURFACE_ID,
        "stage": STAGE,
        "output_class": OUTPUT_CLASS,
        "acceptance_policy": ACCEPTANCE_POLICY,
        "reasons": reasons,
        # A separately authorized lane must still provide the live invocation.
        "live_invocation_authorized": False,
    }


def _evidence_refs(reviews: list[dict[str, Any]]) -> tuple[str, ...]:
    refs: list[str] = []
    for review in reviews:
        paper_id = str(review.get("paper_id") or "").strip()
        spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
        refs.extend(f"{paper_id}-evidence-{index}" for index, _span in enumerate(spans, start=1))
    return tuple(refs)


def _report_context(*, topic_id: str, topic_name: str, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_refs = _evidence_refs(reviews)
    provider_reviews = []
    for index, review in enumerate(reviews, start=1):
        provider_reviews.append(
            {
                "provider_review_id": f"recorded-review-{index}",
                "paper_id": str(review.get("paper_id") or ""),
                "paper_title": str(review.get("title") or "Untitled synthetic paper"),
                "concise_review_summary": str(review.get("why_it_matters") or "Synthetic local review context."),
                "key_findings": list(review.get("key_findings") or ["Synthetic local finding."]),
                "limitations": list(review.get("limitations") or ["Synthetic corpus limitation."]),
                "relevance_to_topic": str(review.get("topic_fit_rationale") or "Synthetic topic fit."),
                "confidence_or_caveats": "Recorded control only; no live Provider was invoked.",
                "provider_family_id": PROVIDER_ID,
                "surface_id": SURFACE_ID,
                "output_class": codex_sdk_provider.PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
                "internal_output_class": codex_sdk_provider.PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
            }
        )
    return {
        "topic_id": topic_id,
        "topic_name": topic_name,
        "report_scope": "provider_review_context_only",
        "source_provider_review_ids": [item["provider_review_id"] for item in provider_reviews],
        "source_paper_ids": [item["paper_id"] for item in provider_reviews],
        "evidence_span_refs_used": list(evidence_refs),
        "proof_artifact_refs_used": ["recorded:prototype-paper-review-proof-v1"],
        "provider_reviews": provider_reviews,
    }


def _accepted_output(context: Mapping[str, Any]) -> dict[str, Any]:
    evidence_refs = [str(item) for item in context["evidence_span_refs_used"]]
    paper_ids = [str(item) for item in context["source_paper_ids"]]
    review_ids = [str(item) for item in context["source_provider_review_ids"]]
    reviews = list(context["provider_reviews"])
    return {
        "output_class": PRODUCT_OUTPUT_CLASS,
        "internal_output_class": OUTPUT_CLASS,
        "provider_family_id": PROVIDER_ID,
        "surface_id": SURFACE_ID,
        "topic_id": context["topic_id"],
        "topic_name": context["topic_name"],
        "report_scope": "provider_review_context_only",
        "source_provider_review_ids": review_ids,
        "source_paper_ids": paper_ids,
        "evidence_span_refs_used": evidence_refs,
        "proof_artifact_refs_used": list(context["proof_artifact_refs_used"]),
        "weekly_report_title": "Recorded-control grounded Weekly Journal draft",
        "executive_summary": "The recorded fixture summarizes only supplied synthetic review evidence.",
        "reviewed_papers_summary": [
            {
                "paper_id": str(review["paper_id"]),
                "paper_title": str(review["paper_title"]),
                "main_takeaway": str(review["key_findings"][0]),
                "evidence_refs": [evidence_refs[index % len(evidence_refs)]],
            }
            for index, review in enumerate(reviews)
        ],
        "cross_paper_synthesis": "The supplied synthetic reviews share evidence-grounded systems findings and explicit caveats.",
        "methodology_or_scope_note": "Recorded fixture validation only; no external research or live Provider call occurred.",
        "limitations": ["The corpus and Provider response are synthetic controls."],
        "recommended_next_research_topics": ["Run a separately authorized bounded live output-class proof."],
        "confidence_or_caveats": "Claims remain bounded to the supplied evidence references.",
        "no_chain_of_thought": True,
    }


def _source_identities(reviews: list[dict[str, Any]]) -> tuple[ProviderSourceIdentity, ...]:
    return tuple(
        ProviderSourceIdentity(
            source_id=str(review.get("paper_id") or ""),
            source_sha256=str(review.get("source_text_sha256") or "") or _sha256(review),
            source_kind="paper_review",
        )
        for review in reviews
    )


def _acceptance_bundle(
    *, context: dict[str, Any], reviews: list[dict[str, Any]], evaluated_at: datetime
) -> tuple[ProviderRoutingDecision, ProviderExecutionRequest, ProviderExecutionResultEnvelope, ProviderProofRecord]:
    artifact_sha256 = _sha256(context)
    command_shape_sha256 = _sha256([SURFACE_ID, STAGE, OUTPUT_CLASS, RESULT_SCHEMA_ID])
    output = _accepted_output(context)
    payload_sha256 = _sha256(output)
    identity = ProviderProofIdentity(
        schema_version="1",
        policy_version=POLICY_VERSION,
        provider_id=PROVIDER_ID,
        surface_id=SURFACE_ID,
        stage=STAGE,
        output_class=OUTPUT_CLASS,
        artifact_sha256=artifact_sha256,
        model=CONTROL_MODEL,
        command_shape_sha256=command_shape_sha256,
        adapter_id=SURFACE_ID,
        adapter_version=ADAPTER_VERSION,
    )
    proof = ProviderProofRecord(
        proof_id=compute_provider_proof_id(identity),
        identity=identity,
        level=ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED,
        accepted_result_sha256=payload_sha256,
        accepted_at=evaluated_at - timedelta(minutes=1),
        expires_at=evaluated_at + timedelta(days=1),
        revoked_at=None,
        limitations=("recorded_control_only", "not_live_authority"),
        accepted=True,
    )
    decision = ProviderRoutingDecision(
        provider_id=PROVIDER_ID,
        stage=STAGE,
        requested_capability="assist_report_draft",
        output_class=OUTPUT_CLASS,
        routing_mode="recorded_control",
        allowed=True,
        blocked_reason=None,
        required_proof_level="exact_output_class_verified",
        current_proof_level="exact_output_class_verified",
        deterministic_fallback=True,
        fallback_provider=None,
        warnings=("Recorded control only; no Provider invocation occurred.",),
        redacted_command_preview=None,
        proof_artifact_refs=(),
        decision_id=f"prototype-decision-{artifact_sha256[:16]}",
        routing_key_sha256=_sha256([PROVIDER_ID, SURFACE_ID, STAGE, OUTPUT_CLASS, artifact_sha256]),
        policy_version=POLICY_VERSION,
        requested_mode="recorded_control",
        effective_mode="recorded_control",
        eligibility=(),
        total_scaled_weight=1_000_000,
        bucket=0,
        selected_provider_id=PROVIDER_ID,
        selected_surface_id=SURFACE_ID,
        selected_proof_id=proof.proof_id,
        selected_identity="recorded_control",
        reasons=(),
        execution_allowed=False,
        output_acceptance_required=True,
        persistence_eligible=False,
    )
    sources = _source_identities(reviews)
    request = ProviderExecutionRequest(
        request_id=f"prototype-request-{artifact_sha256[:16]}",
        routing_decision_id=decision.decision_id,
        provider_id=PROVIDER_ID,
        surface_id=SURFACE_ID,
        stage=STAGE,
        output_class=OUTPUT_CLASS,
        proof_id=proof.proof_id,
        adapter_id=SURFACE_ID,
        adapter_version=ADAPTER_VERSION,
        model=CONTROL_MODEL,
        command_shape_sha256=command_shape_sha256,
        artifact_sha256=artifact_sha256,
        source_identities=sources,
        request_schema_id=REQUEST_SCHEMA_ID,
        result_schema_id=RESULT_SCHEMA_ID,
        timeout_seconds=30,
    )
    result = ProviderExecutionResultEnvelope(
        execution_id=f"prototype-execution-{payload_sha256[:16]}",
        request_id=request.request_id,
        routing_decision_id=decision.decision_id,
        proof_id=proof.proof_id,
        provider_id=PROVIDER_ID,
        surface_id=SURFACE_ID,
        stage=STAGE,
        output_class=OUTPUT_CLASS,
        status="succeeded",
        adapter_id=SURFACE_ID,
        adapter_version=ADAPTER_VERSION,
        model=CONTROL_MODEL,
        command_shape_sha256=command_shape_sha256,
        payload_sha256=payload_sha256,
        schema_id=RESULT_SCHEMA_ID,
        source_identities=sources,
        evidence_ids=tuple(str(item) for item in context["evidence_span_refs_used"]),
        citation_ids=tuple(f"citation:{item}" for item in context["evidence_span_refs_used"]),
        timed_out=False,
        truncated=False,
        redaction_applied=True,
        side_effect_detected=False,
    )
    return decision, request, result, proof


def _with_digest(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "record_digest": _sha256(record)}


def _control_record(
    *,
    name: str,
    run_id: str,
    output: Any,
    context: dict[str, Any],
    reviews: list[dict[str, Any]],
    evaluated_at: datetime,
    result_changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision, request, result, proof = _acceptance_bundle(context=context, reviews=reviews, evaluated_at=evaluated_at)
    if result_changes:
        result = replace(result, **result_changes)
    validation = codex_sdk_provider.validate_codex_sdk_weekly_report_output(output, context)
    reasons = tuple(str(item) for item in validation.get("reasonCodes") or [])
    acceptance = None
    if validation.get("valid"):
        expected_payload_sha256 = _sha256(output)
        known_evidence = set(str(item) for item in context["evidence_span_refs_used"])
        known_citations = {f"citation:{item}" for item in known_evidence}
        acceptance = evaluate_provider_output_acceptance(
            routing_decision=decision,
            execution_request=request,
            result_envelope=result,
            proof_record=proof,
            evaluated_at=evaluated_at,
            payload_hash_validator=lambda digest: digest == expected_payload_sha256,
            result_schema_validator=lambda envelope: envelope.schema_id == RESULT_SCHEMA_ID,
            source_catalog_validator=lambda identities: identities == request.source_identities,
            evidence_citation_validator=lambda evidence, citations: (
                bool(evidence) and set(evidence).issubset(known_evidence),
                bool(citations) and set(citations).issubset(known_citations),
            ),
            domain_validator=lambda _envelope: (True, True),
            redaction_side_effect_validator=lambda envelope: (
                envelope.redaction_applied,
                not envelope.side_effect_detected,
            ),
        )
        reasons = acceptance.reasons
    accepted = bool(acceptance and acceptance.accepted)
    completed_at = evaluated_at.isoformat()
    raw_digest = _sha256(output)
    return _with_digest(
        {
            "schema_version": "prototype_provider_execution_v1",
            "control_name": name,
            "execution_kind": "recorded_control",
            "run_id": run_id,
            "stage": STAGE,
            "provider_id": PROVIDER_ID,
            "execution_surface": SURFACE_ID,
            "model_or_backend": CONTROL_MODEL,
            "output_class": OUTPUT_CLASS,
            "route_decision": decision.decision_id,
            "activation_state": "task_local_recorded_control",
            "invocation_attempted": False,
            "exit_status": "recorded_success" if accepted else "recorded_rejected",
            "accepted": accepted,
            "rejected": not accepted,
            "fallback_used": not accepted,
            "fallback_reason": None if accepted else (reasons[0] if reasons else "recorded_control_rejected"),
            "request_digest": _sha256(request.to_dict()),
            "raw_output_digest": raw_digest,
            "accepted_output_digest": raw_digest if accepted else None,
            "created_at": completed_at,
            "completed_at": completed_at,
            "error_category": None if accepted else (reasons[0] if reasons else "recorded_control_rejected"),
            "acceptance_id": acceptance.acceptance_id if acceptance is not None else None,
            "acceptance_reasons": list(reasons),
            "trusted_persistence_applied": False,
            "live_provider_invoked": False,
        }
    )


def build_recorded_control_bundle(
    *, run_id: str, topic_id: str, topic_name: str, reviews: list[dict[str, Any]], now: datetime | None = None
) -> dict[str, Any]:
    """Build the fallback decision and exactly five required recorded controls."""

    evaluated_at = now or datetime.now(timezone.utc)
    context = _report_context(topic_id=topic_id, topic_name=topic_name, reviews=reviews)
    accepted_output = _accepted_output(context)
    activation = evaluate_development_activation(proof_eligible=False)
    stamp = evaluated_at.isoformat()
    fallback_reason = str((activation.get("reasons") or ["prototype_provider_disabled"])[0])
    fallback = _with_digest(
        {
            "schema_version": "prototype_provider_execution_v1",
            "control_name": "deterministic_fallback",
            "execution_kind": "fallback_decision",
            "run_id": run_id,
            "stage": STAGE,
            "provider_id": PROVIDER_ID,
            "execution_surface": SURFACE_ID,
            "model_or_backend": "deterministic_local",
            "output_class": OUTPUT_CLASS,
            "route_decision": "deterministic_local_fallback",
            "activation_state": activation["activation_state"],
            "invocation_attempted": False,
            "exit_status": "not_invoked",
            "accepted": False,
            "rejected": False,
            "fallback_used": True,
            "fallback_reason": fallback_reason,
            "request_digest": _sha256(context),
            "raw_output_digest": None,
            "accepted_output_digest": None,
            "created_at": stamp,
            "completed_at": stamp,
            "error_category": None,
            "trusted_persistence_applied": False,
            "live_provider_invoked": False,
        }
    )

    malformed_output: Any = {"output_class": PRODUCT_OUTPUT_CLASS}
    wrong_stage_changes = {"stage": "triage_assist", "output_class": "triage_annotations"}
    unknown_output = json.loads(json.dumps(accepted_output))
    unknown_output["evidence_span_refs_used"] = ["unknown-evidence-reference"]
    unknown_output["reviewed_papers_summary"][0]["evidence_refs"] = ["unknown-evidence-reference"]

    controls = [
        _control_record(
            name="accepted_output",
            run_id=run_id,
            output=accepted_output,
            context=context,
            reviews=reviews,
            evaluated_at=evaluated_at,
        ),
        _control_record(
            name="malformed_output",
            run_id=run_id,
            output=malformed_output,
            context=context,
            reviews=reviews,
            evaluated_at=evaluated_at,
        ),
        _control_record(
            name="wrong_stage_output_class",
            run_id=run_id,
            output=accepted_output,
            context=context,
            reviews=reviews,
            evaluated_at=evaluated_at,
            result_changes=wrong_stage_changes,
        ),
        _control_record(
            name="unknown_evidence_reference",
            run_id=run_id,
            output=unknown_output,
            context=context,
            reviews=reviews,
            evaluated_at=evaluated_at,
        ),
        _control_record(
            name="provider_failure",
            run_id=run_id,
            output=accepted_output,
            context=context,
            reviews=reviews,
            evaluated_at=evaluated_at,
            result_changes={"status": "failed"},
        ),
    ]
    expected = {
        "accepted_output": True,
        "malformed_output": False,
        "wrong_stage_output_class": False,
        "unknown_evidence_reference": False,
        "provider_failure": False,
    }
    passed = sum(record["accepted"] is expected[record["control_name"]] for record in controls)
    body = {
        "schema_version": "prototype_provider_control_bundle_v1",
        "run_id": run_id,
        "provider_id": PROVIDER_ID,
        "surface_id": SURFACE_ID,
        "stage": STAGE,
        "output_class": OUTPUT_CLASS,
        "live_provider_invoked": False,
        "activation": activation,
        "fallback_decision": fallback,
        "controls": controls,
        "control_total": len(controls),
        "control_passed": passed,
        "control_failed": len(controls) - passed,
        "accepted_control_passed": bool(controls[0]["accepted"]),
        "rejection_controls_passed": all(not record["accepted"] for record in controls[1:]),
        "trusted_provider_output_persisted": False,
    }
    return {**body, "record_digest": _sha256(body)}

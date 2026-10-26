from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

from app.db.bootstrap import connect_metadata_index, metadata_index_path
from app.repositories import paper_reviews
from app.repositories.files import iso_now_utc, slugify, write_json, write_text
from app.repositories.runs import build_run_paths, load_run_record
from app.services import report_quality
from app.services.providers.codex_sdk_provider import (
    validate_codex_sdk_weekly_report_output,
    validate_codex_sdk_weekly_report_output_class_proof_artifact_contract,
)


PROOF_ARTIFACT_KIND = "provider_output_class_proof"
PROOF_MODE = "output_class_live"
PROVIDER_FAMILY_ID = "codex"
PROVIDER_SURFACE_ID = "codex_sdk_python"
PRODUCT_OUTPUT_CLASS = "weekly_report"
INTERNAL_OUTPUT_CLASS = "report_draft"
REPORT_SOURCE = "provider_output_class_proof"
REPORT_SCOPE = "provider_review_context_only"
REAL_APPLY_BLOCKED_STATUS = "real_apply_blocked_pending_owner_approval"
REAL_APPLY_APPLIED_STATUS = "real_apply_applied"
REAL_APPLY_ALREADY_APPLIED_STATUS = "real_apply_already_applied"
REAL_APPLY_CONFLICT_STATUS = "real_apply_conflict"
APPLY_STATUS_DRY_RUN_ONLY = "dry_run_only"
REPORT_ARTIFACT_FILENAME = "provider-weekly-report-draft.md"
REQUIRED_SOURCE_PROVIDER_REVIEW_ID = "provider-review-ce78f0c10898f231"
REQUIRED_SOURCE_PAPER_ID = "2601.07571v1"

SECRET_VALUE_MARKERS: tuple[str, ...] = (
    "Bearer ",
    "Authorization:",
    "BEGIN ",
    "sk-",
    "ya29.",
    "access_token=",
    "refresh_token=",
    "client_secret=",
    "api_key=",
    "C:\\",
    "C:/Users/",
    "/Users/",
    "/home/",
    ".codex/",
    "CODEX_HOME",
    "auth.json",
    "session.json",
    "browser/cookies",
    "browser/session",
    "keychain",
    "credential-store",
)
CHAIN_OF_THOUGHT_MARKERS: tuple[str, ...] = (
    "chain-of-thought",
    "chain_of_thought",
    "hidden reasoning",
    "internal reasoning",
    "step-by-step reasoning",
    "thought process",
)


class ProviderReportIntegrationError(ValueError):
    def __init__(self, reason_codes: list[str]) -> None:
        super().__init__(", ".join(reason_codes))
        self.reason_codes = list(dict.fromkeys(reason_codes))


@dataclass(frozen=True)
class ProviderReportSourceReviewRef:
    provider_review_id: str
    paper_id: str
    paper_title: str
    provider_family_id: str
    provider_surface_id: str
    output_class: str
    internal_output_class: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerReviewId": self.provider_review_id,
            "paperId": self.paper_id,
            "paperTitle": self.paper_title,
            "providerFamilyId": self.provider_family_id,
            "providerSurfaceId": self.provider_surface_id,
            "outputClass": self.output_class,
            "internalOutputClass": self.internal_output_class,
        }


@dataclass(frozen=True)
class ProviderWeeklyReportProofRecord:
    artifact_ref: str
    timestamp: str | None
    run_id: str | None
    provider_family_id: str
    provider_surface_id: str
    execution_surface_type: str
    adapter_kind: str
    auth_strategy_used: str
    proof_mode: str
    output_class: str
    internal_output_class: str
    current_proof_status: str
    topic_id: str
    topic_name: str
    report_scope: str
    source_provider_review_ids: tuple[str, ...]
    source_paper_ids: tuple[str, ...]
    source_provider_reviews: tuple[ProviderReportSourceReviewRef, ...]
    evidence_span_refs_used: tuple[str, ...]
    proof_artifact_refs_used: tuple[str, ...]
    redacted_provider_output: Mapping[str, Any]
    provider_sdk_run_count: int
    provider_call_count: int
    provider_command_count: int
    research_output_allowed: bool
    report_draft_allowed: bool
    report_persistence_in_scope: bool
    db_write_in_scope: bool
    production_ready: bool
    external_beta_ready: bool

    def to_safe_status(self) -> dict[str, Any]:
        return {
            "artifactKind": PROOF_ARTIFACT_KIND,
            "artifactRef": self.artifact_ref,
            "proofMode": self.proof_mode,
            "providerFamilyId": self.provider_family_id,
            "providerSurfaceId": self.provider_surface_id,
            "executionSurfaceType": self.execution_surface_type,
            "adapterKind": self.adapter_kind,
            "authStrategyUsed": self.auth_strategy_used,
            "outputClass": self.output_class,
            "internalOutputClass": self.internal_output_class,
            "currentProofStatus": self.current_proof_status,
            "topicId": self.topic_id,
            "reportScope": self.report_scope,
            "sourceProviderReviewIds": list(self.source_provider_review_ids),
            "sourcePaperIds": list(self.source_paper_ids),
            "evidenceSpanCount": len(self.evidence_span_refs_used),
            "evidenceSpanRefsUsed": list(self.evidence_span_refs_used),
            "proofArtifactRefsUsed": list(self.proof_artifact_refs_used),
            "providerSdkRunCount": self.provider_sdk_run_count,
            "providerCallCount": self.provider_call_count,
            "providerCommandCount": self.provider_command_count,
            "researchOutputAllowed": self.research_output_allowed,
            "reportDraftAllowed": self.report_draft_allowed,
            "reportPersistenceInScope": self.report_persistence_in_scope,
            "dbWriteInScope": self.db_write_in_scope,
            "productionReady": self.production_ready,
            "externalBetaReady": self.external_beta_ready,
        }


@dataclass(frozen=True)
class NormalizedProviderReportDraft:
    provider_report_draft_id: str
    provider_family_id: str
    provider_surface_id: str
    proof_artifact_ref: str
    proof_mode: str
    output_class: str
    internal_output_class: str
    topic_id: str
    topic_name: str
    report_scope: str
    source_provider_review_ids: tuple[str, ...]
    source_paper_ids: tuple[str, ...]
    evidence_span_refs_used: tuple[str, ...]
    proof_artifact_refs_used: tuple[str, ...]
    weekly_report_title: str
    executive_summary: str
    reviewed_papers_summary: tuple[dict[str, Any], ...]
    cross_paper_synthesis: str
    methodology_or_scope_note: str
    limitations: tuple[str, ...]
    recommended_next_research_topics: tuple[str, ...]
    confidence_or_caveats: str
    report_source: str
    provider_auth_is_app_oauth_oidc: bool
    local_mcp_is_public_connector: bool
    production_ready: bool
    external_beta_ready: bool
    report_persistence_in_scope: bool
    db_write_in_scope: bool
    no_chain_of_thought: bool
    redaction_status: str
    apply_status: str
    repository_payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_report_draft_id": self.provider_report_draft_id,
            "provider_family_id": self.provider_family_id,
            "provider_surface_id": self.provider_surface_id,
            "proof_artifact_ref": self.proof_artifact_ref,
            "proof_mode": self.proof_mode,
            "output_class": self.output_class,
            "internal_output_class": self.internal_output_class,
            "topic_id": self.topic_id,
            "topic_name": self.topic_name,
            "report_scope": self.report_scope,
            "source_provider_review_ids": list(self.source_provider_review_ids),
            "source_paper_ids": list(self.source_paper_ids),
            "evidence_span_refs_used": list(self.evidence_span_refs_used),
            "proof_artifact_refs_used": list(self.proof_artifact_refs_used),
            "weekly_report_title": self.weekly_report_title,
            "executive_summary": self.executive_summary,
            "reviewed_papers_summary": [dict(item) for item in self.reviewed_papers_summary],
            "cross_paper_synthesis": self.cross_paper_synthesis,
            "methodology_or_scope_note": self.methodology_or_scope_note,
            "limitations": list(self.limitations),
            "recommended_next_research_topics": list(self.recommended_next_research_topics),
            "confidence_or_caveats": self.confidence_or_caveats,
            "report_source": self.report_source,
            "provider_auth_is_app_oauth_oidc": self.provider_auth_is_app_oauth_oidc,
            "local_mcp_is_public_connector": self.local_mcp_is_public_connector,
            "production_ready": self.production_ready,
            "external_beta_ready": self.external_beta_ready,
            "report_persistence_in_scope": self.report_persistence_in_scope,
            "db_write_in_scope": self.db_write_in_scope,
            "no_chain_of_thought": self.no_chain_of_thought,
            "redaction_status": self.redaction_status,
            "apply_status": self.apply_status,
            "repository_payload": dict(self.repository_payload),
        }


def _dedupe(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _walk_values(payload: Any) -> list[str]:
    values: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)
        else:
            values.append(str(node or ""))

    _walk(payload)
    return values


def _has_secret_like_value(payload: Any) -> bool:
    return any(marker.lower() in value.lower() for value in _walk_values(payload) for marker in SECRET_VALUE_MARKERS)


def _has_chain_of_thought_like_value(payload: Any) -> bool:
    return any(marker in value.lower() for value in _walk_values(payload) for marker in CHAIN_OF_THOUGHT_MARKERS)


def _has_positive_persistence_or_readiness_claim(payload: Any) -> bool:
    claim_markers = (
        "persisted report",
        "saved report",
        "db write completed",
        "database write completed",
        "production ready",
        "external beta ready",
        "released",
        "deployed",
        "publicly available",
    )
    negation_markers = (
        "not ",
        "no ",
        "never ",
        "without ",
        "does not ",
        "do not ",
        "cannot ",
        "not in scope",
        "out of scope",
        "not persisted",
        "not production-ready",
        "not production ready",
        "not external beta-ready",
        "not external beta ready",
        "not released",
        "not deployed",
        "not publicly available",
    )
    for value in _walk_values(payload):
        lowered = value.lower()
        for marker in claim_markers:
            index = lowered.find(marker)
            if index < 0:
                continue
            segment = lowered[max(0, index - 64) : min(len(lowered), index + len(marker) + 64)]
            if any(negation in segment for negation in negation_markers):
                continue
            return True
    return False


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return _dedupe(tuple(str(item).strip() for item in value if str(item).strip()))


def _artifact_ref_from_path(path: Path, artifact: Mapping[str, Any]) -> str:
    artifact_ref = _as_str(artifact.get("artifactRef"))
    if artifact_ref:
        return artifact_ref.replace("\\", "/")
    normalized = path.as_posix()
    marker = "output/provider-output-class-proof/"
    if marker in normalized:
        return normalized[normalized.index(marker) :]
    return path.name


def _report_context_from_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "topic_id": artifact.get("topicId"),
        "topic_name": artifact.get("topicName"),
        "report_scope": artifact.get("reportScope"),
        "source_provider_review_ids": list(artifact.get("sourceProviderReviewIds") or []),
        "source_paper_ids": list(artifact.get("sourcePaperIds") or []),
        "evidence_span_refs_used": list(artifact.get("evidenceSpanRefsUsed") or []),
        "proof_artifact_refs_used": list(artifact.get("proofArtifactRefsUsed") or []),
        "provider_reviews": list(artifact.get("sourceProviderReviews") or []),
    }


def _report_draft_id(record: ProviderWeeklyReportProofRecord) -> str:
    seed = "|".join(
        [
            record.provider_family_id,
            record.provider_surface_id,
            record.output_class,
            record.internal_output_class,
            record.topic_id,
            ",".join(record.source_provider_review_ids),
            record.artifact_ref,
        ]
    )
    return "provider-report-draft-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _source_hash(draft: NormalizedProviderReportDraft) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "artifactRef": draft.proof_artifact_ref,
                "evidenceSpanRefsUsed": list(draft.evidence_span_refs_used),
                "sourcePaperIds": list(draft.source_paper_ids),
                "sourceProviderReviewIds": list(draft.source_provider_review_ids),
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _source_reviews(artifact: Mapping[str, Any]) -> tuple[ProviderReportSourceReviewRef, ...]:
    refs: list[ProviderReportSourceReviewRef] = []
    for item in artifact.get("sourceProviderReviews") or []:
        if not isinstance(item, Mapping):
            continue
        refs.append(
            ProviderReportSourceReviewRef(
                provider_review_id=_as_str(item.get("provider_review_id")),
                paper_id=_as_str(item.get("paper_id")),
                paper_title=_as_str(item.get("paper_title")),
                provider_family_id=_as_str(item.get("provider_family_id") or PROVIDER_FAMILY_ID),
                provider_surface_id=_as_str(item.get("surface_id") or PROVIDER_SURFACE_ID),
                output_class=_as_str(item.get("output_class") or "paper_review"),
                internal_output_class=_as_str(item.get("internal_output_class") or "research_output"),
            )
        )
    return tuple(refs)


def _validate_artifact_for_integration(artifact: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if artifact.get("artifactKind") == "provider_tiny_proof":
        reasons.append("provider_tiny_proof_rejected_for_report_integration")
    fixed = {
        "artifactKind": PROOF_ARTIFACT_KIND,
        "proofMode": PROOF_MODE,
        "productOutputClass": PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": INTERNAL_OUTPUT_CLASS,
        "acceptedOutputClassProof": True,
        "outputClassProof": True,
        "researchOutputAllowed": False,
        "reportDraftAllowed": True,
        "reportPersistenceInScope": False,
        "dbWriteInScope": False,
        "no_chain_of_thought": True,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "productionReady": False,
        "externalBetaReady": False,
    }
    for key, expected in fixed.items():
        if artifact.get(key) != expected:
            reasons.append(f"field_mismatch:{key}")
    if artifact.get("providerFamilyId") != PROVIDER_FAMILY_ID:
        reasons.append("provider_family_mismatch")
    if artifact.get("surfaceId") != PROVIDER_SURFACE_ID:
        reasons.append("provider_surface_mismatch")
    source_review_ids = _as_tuple(artifact.get("sourceProviderReviewIds"))
    source_paper_ids = _as_tuple(artifact.get("sourcePaperIds"))
    evidence_refs = _as_tuple(artifact.get("evidenceSpanRefsUsed"))
    proof_refs = _as_tuple(artifact.get("proofArtifactRefsUsed"))
    if not source_review_ids:
        reasons.append("missing_source_provider_review_ids")
    if REQUIRED_SOURCE_PROVIDER_REVIEW_ID not in source_review_ids:
        reasons.append("required_source_provider_review_missing")
    if not source_paper_ids:
        reasons.append("missing_source_paper_ids")
    if REQUIRED_SOURCE_PAPER_ID not in source_paper_ids:
        reasons.append("required_source_paper_missing")
    if int(artifact.get("evidenceSpanCount") or 0) <= 0 or not evidence_refs:
        reasons.append("missing_evidence_refs")
    if not proof_refs:
        reasons.append("missing_proof_artifact_refs_used")
    elif not any(ref.endswith("codex_sdk_paper_review_output_class_live_proof.json") for ref in proof_refs):
        reasons.append("missing_paper_review_proof_artifact_ref")
    if artifact.get("providerCommandCount") != 0:
        reasons.append("provider_command_count_must_be_zero")
    output = artifact.get("redactedProviderOutput") or {}
    if not isinstance(output, Mapping):
        reasons.append("provider_output_not_object")
    else:
        output_validation = validate_codex_sdk_weekly_report_output(output, _report_context_from_artifact(artifact))
        if not output_validation.get("valid"):
            reasons.append("provider_output_not_valid_weekly_report_json")
            reasons.extend(str(reason) for reason in output_validation.get("reasonCodes", []))
    contract_validation = validate_codex_sdk_weekly_report_output_class_proof_artifact_contract(artifact)
    if not contract_validation.get("valid"):
        reasons.append("codex_sdk_weekly_report_artifact_contract_rejected")
        reasons.extend(str(reason) for reason in contract_validation.get("reasonCodes", []))
    if _has_secret_like_value(artifact):
        reasons.append("secret_like_content_detected")
    if _has_chain_of_thought_like_value(output):
        reasons.append("chain_of_thought_like_content_detected")
    if _has_positive_persistence_or_readiness_claim(artifact):
        reasons.append("positive_report_persistence_or_readiness_claim")
    return list(dict.fromkeys(reasons))


def load_provider_weekly_report_output_class_proof(path: str | Path) -> ProviderWeeklyReportProofRecord:
    artifact_path = Path(path)
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderReportIntegrationError(["proof_artifact_unreadable"]) from exc
    if not isinstance(artifact, dict):
        raise ProviderReportIntegrationError(["proof_artifact_not_object"])
    reasons = _validate_artifact_for_integration(artifact)
    if reasons:
        raise ProviderReportIntegrationError(reasons)
    output = artifact.get("redactedProviderOutput") or {}
    if not isinstance(output, Mapping):
        raise ProviderReportIntegrationError(["provider_output_not_object"])
    return ProviderWeeklyReportProofRecord(
        artifact_ref=_artifact_ref_from_path(artifact_path, artifact),
        timestamp=artifact.get("timestamp"),
        run_id=artifact.get("runId"),
        provider_family_id=_as_str(artifact.get("providerFamilyId")),
        provider_surface_id=_as_str(artifact.get("surfaceId")),
        execution_surface_type=_as_str(artifact.get("executionSurfaceType")),
        adapter_kind=_as_str(artifact.get("adapterKind")),
        auth_strategy_used=_as_str(artifact.get("authStrategyUsed")),
        proof_mode=_as_str(artifact.get("proofMode")),
        output_class=_as_str(artifact.get("productOutputClass")),
        internal_output_class=_as_str(artifact.get("internalOutputClass")),
        current_proof_status=_as_str(artifact.get("currentProofStatus")),
        topic_id=_as_str(artifact.get("topicId")),
        topic_name=_as_str(artifact.get("topicName")),
        report_scope=_as_str(artifact.get("reportScope")),
        source_provider_review_ids=_as_tuple(artifact.get("sourceProviderReviewIds")),
        source_paper_ids=_as_tuple(artifact.get("sourcePaperIds")),
        source_provider_reviews=_source_reviews(artifact),
        evidence_span_refs_used=_as_tuple(artifact.get("evidenceSpanRefsUsed")),
        proof_artifact_refs_used=_as_tuple(artifact.get("proofArtifactRefsUsed")),
        redacted_provider_output=dict(output),
        provider_sdk_run_count=int(artifact.get("providerSdkRunCount") or 0),
        provider_call_count=int(artifact.get("providerCallCount") or 0),
        provider_command_count=int(artifact.get("providerCommandCount") or 0),
        research_output_allowed=bool(artifact.get("researchOutputAllowed")),
        report_draft_allowed=bool(artifact.get("reportDraftAllowed")),
        report_persistence_in_scope=bool(artifact.get("reportPersistenceInScope")),
        db_write_in_scope=bool(artifact.get("dbWriteInScope")),
        production_ready=bool(artifact.get("productionReady")),
        external_beta_ready=bool(artifact.get("externalBetaReady")),
    )


def validate_provider_weekly_report_proof_for_integration(record: ProviderWeeklyReportProofRecord) -> dict[str, Any]:
    reasons: list[str] = []
    if record.provider_family_id != PROVIDER_FAMILY_ID:
        reasons.append("provider_family_mismatch")
    if record.provider_surface_id != PROVIDER_SURFACE_ID:
        reasons.append("provider_surface_mismatch")
    if record.output_class != PRODUCT_OUTPUT_CLASS or record.internal_output_class != INTERNAL_OUTPUT_CLASS:
        reasons.append("output_class_mismatch")
    if record.proof_mode != PROOF_MODE:
        reasons.append("proof_mode_not_output_class_live")
    if record.report_scope != REPORT_SCOPE:
        reasons.append("report_scope_mismatch")
    if not record.report_draft_allowed or record.research_output_allowed:
        reasons.append("output_class_gate_mismatch")
    if record.report_persistence_in_scope or record.db_write_in_scope:
        reasons.append("report_persistence_or_db_write_in_scope")
    if record.production_ready or record.external_beta_ready:
        reasons.append("production_or_external_beta_claim")
    if not record.source_provider_review_ids:
        reasons.append("missing_source_provider_review_ids")
    if REQUIRED_SOURCE_PROVIDER_REVIEW_ID not in record.source_provider_review_ids:
        reasons.append("required_source_provider_review_missing")
    if REQUIRED_SOURCE_PAPER_ID not in record.source_paper_ids:
        reasons.append("required_source_paper_missing")
    if not record.evidence_span_refs_used:
        reasons.append("missing_evidence_refs")
    if not record.proof_artifact_refs_used:
        reasons.append("missing_proof_artifact_refs_used")
    if record.provider_command_count != 0:
        reasons.append("provider_command_count_must_be_zero")
    if _has_secret_like_value(record.to_safe_status()) or _has_secret_like_value(record.redacted_provider_output):
        reasons.append("secret_like_content_detected")
    if _has_chain_of_thought_like_value(record.redacted_provider_output):
        reasons.append("chain_of_thought_like_content_detected")
    if _has_positive_persistence_or_readiness_claim(record.redacted_provider_output):
        reasons.append("positive_report_persistence_or_readiness_claim")
    return {"valid": not reasons, "reasonCodes": list(dict.fromkeys(reasons))}


def normalize_provider_weekly_report_for_storage(record: ProviderWeeklyReportProofRecord) -> NormalizedProviderReportDraft:
    validation = validate_provider_weekly_report_proof_for_integration(record)
    if not validation["valid"]:
        raise ProviderReportIntegrationError(validation["reasonCodes"])
    output = record.redacted_provider_output
    provider_report_draft_id = _report_draft_id(record)
    reviewed_papers = tuple(dict(item) for item in output.get("reviewed_papers_summary") or [] if isinstance(item, Mapping))
    repository_payload = {
        "provider_report_draft_id": provider_report_draft_id,
        "topic_id": record.topic_id,
        "topic_name": record.topic_name,
        "title": _as_str(output.get("weekly_report_title")),
        "status": "draft_proof_available_not_persisted",
        "report_source": REPORT_SOURCE,
        "report_scope": REPORT_SCOPE,
        "provider_family_id": PROVIDER_FAMILY_ID,
        "provider_surface_id": PROVIDER_SURFACE_ID,
        "proof_artifact_ref": record.artifact_ref,
        "proof_mode": PROOF_MODE,
        "output_class": PRODUCT_OUTPUT_CLASS,
        "internal_output_class": INTERNAL_OUTPUT_CLASS,
        "source_provider_review_ids": list(record.source_provider_review_ids),
        "source_paper_ids": list(record.source_paper_ids),
        "evidence_span_refs_used": list(record.evidence_span_refs_used),
        "proof_artifact_refs_used": list(record.proof_artifact_refs_used),
        "created_from_proof_at": record.timestamp or iso_now_utc(),
        "report_persistence_in_scope": False,
        "db_write_in_scope": False,
        "production_ready": False,
        "external_beta_ready": False,
    }
    normalized = NormalizedProviderReportDraft(
        provider_report_draft_id=provider_report_draft_id,
        provider_family_id=PROVIDER_FAMILY_ID,
        provider_surface_id=PROVIDER_SURFACE_ID,
        proof_artifact_ref=record.artifact_ref,
        proof_mode=PROOF_MODE,
        output_class=PRODUCT_OUTPUT_CLASS,
        internal_output_class=INTERNAL_OUTPUT_CLASS,
        topic_id=record.topic_id,
        topic_name=record.topic_name,
        report_scope=REPORT_SCOPE,
        source_provider_review_ids=record.source_provider_review_ids,
        source_paper_ids=record.source_paper_ids,
        evidence_span_refs_used=record.evidence_span_refs_used,
        proof_artifact_refs_used=record.proof_artifact_refs_used,
        weekly_report_title=_as_str(output.get("weekly_report_title")),
        executive_summary=_as_str(output.get("executive_summary")),
        reviewed_papers_summary=reviewed_papers,
        cross_paper_synthesis=_as_str(output.get("cross_paper_synthesis")),
        methodology_or_scope_note=_as_str(output.get("methodology_or_scope_note")),
        limitations=_as_tuple(output.get("limitations")),
        recommended_next_research_topics=_as_tuple(output.get("recommended_next_research_topics")),
        confidence_or_caveats=_as_str(output.get("confidence_or_caveats")),
        report_source=REPORT_SOURCE,
        provider_auth_is_app_oauth_oidc=False,
        local_mcp_is_public_connector=False,
        production_ready=False,
        external_beta_ready=False,
        report_persistence_in_scope=False,
        db_write_in_scope=False,
        no_chain_of_thought=True,
        redaction_status="passed",
        apply_status=APPLY_STATUS_DRY_RUN_ONLY,
        repository_payload=repository_payload,
    )
    if _has_secret_like_value(normalized.to_dict()):
        raise ProviderReportIntegrationError(["secret_like_content_detected"])
    if _has_chain_of_thought_like_value(normalized.to_dict()):
        raise ProviderReportIntegrationError(["chain_of_thought_like_content_detected"])
    return normalized


def plan_provider_report_db_apply(
    record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft,
    *,
    mode: str = "dry_run",
) -> dict[str, Any]:
    normalized = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    known_mode = mode in {"dry_run", "temp_apply"}
    status = "dry_run_ready" if mode == "dry_run" else "temp_apply_ready" if mode == "temp_apply" else "rejected_artifact"
    return {
        "mode": mode,
        "status": status,
        "knownMode": known_mode,
        "applied": False,
        "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
        "targetEntities": [
            "report_draft",
            "report_sections",
            "provider_report_sources",
            "provider_proof_artifact_refs",
            "report_quality_context",
        ],
        "wouldCreateReportDraftRow": known_mode,
        "wouldAttachSourceProviderReviewIds": known_mode,
        "wouldAttachSourcePaperEvidenceRefs": known_mode,
        "wouldLinkWeeklyProofArtifactRef": known_mode,
        "wouldPreservePaperReviewProofArtifactRef": known_mode,
        "wouldMarkProviderProvenance": known_mode,
        "wouldPersistReportArtifact": False,
        "wouldWriteRealDb": False,
        "wouldMutateStorageRuns": False,
        "providerReportDraftId": normalized.provider_report_draft_id,
        "topicId": normalized.topic_id,
        "sourceProviderReviewIds": list(normalized.source_provider_review_ids),
        "sourcePaperIds": list(normalized.source_paper_ids),
        "proofArtifactRef": normalized.proof_artifact_ref,
        "reasonCodes": [
            "provider_report_integration_plan",
            "report_draft_output_class_proof_present",
            "real_report_persistence_blocked_pending_owner_approval",
        ],
    }


def _report_scope_lines(draft: NormalizedProviderReportDraft, *, persisted: bool) -> list[str]:
    persistence_line = (
        "- persisted local/internal report draft from accepted output-class proof"
        if persisted
        else "- report persistence still blocked pending owner approval"
    )
    return [
        "- provider-backed weekly report draft proof available",
        persistence_line,
        "- not production-ready; not external beta-ready",
        f"- reviewed count: {len(draft.source_provider_review_ids)} DB-applied provider paper review",
        f"- source provider review ids: {', '.join(draft.source_provider_review_ids) or '-'}",
        f"- source paper ids: {', '.join(draft.source_paper_ids) or '-'}",
        f"- evidence span refs: {', '.join(draft.evidence_span_refs_used) or '-'}",
        (
            "- full-text reviewed coverage: 1/1 within this provider proof context; "
            "all collected papers were not read in full."
        ),
        "- Coverage limitation: this is not a complete literature survey and does not claim full weekly coverage of XR/AR literature.",
        "- No additional external retrieval or provider analysis was performed during the disclosure patch.",
        "- App OAuth/OIDC and public MCP connector proof are separate and not in scope.",
        "- Deterministic report_quality is a local rubric, not a journal-grade guarantee.",
        f"- proof artifact ref: {draft.proof_artifact_ref}",
    ]


def build_provider_report_preview_markdown(
    record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft,
    *,
    persisted: bool = False,
) -> str:
    draft = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    source_hash = _source_hash(draft)
    evidence_refs = ", ".join(draft.evidence_span_refs_used) or "-"
    paper_rows = []
    for item in draft.reviewed_papers_summary:
        paper_rows.append(
            "| {paper_id} | {title} | reviewed | {source_hash} | {evidence_refs} |".format(
                paper_id=str(item.get("paper_id") or ""),
                title=str(item.get("paper_title") or "").replace("|", "\\|"),
                source_hash=source_hash,
                evidence_refs=", ".join(str(ref) for ref in item.get("evidence_refs") or []) or evidence_refs,
            )
        )
    if not paper_rows:
        paper_rows.append("| - | - | reviewed | {source_hash} | {evidence_refs} |".format(source_hash=source_hash, evidence_refs=evidence_refs))
    limitations = "\n".join(f"- {item}" for item in draft.limitations) or "- Scope is limited to the accepted provider proof context."
    topics = "\n".join(f"- {item}" for item in draft.recommended_next_research_topics) or "- No next topics supplied."
    return "\n".join(
        [
            f"# {draft.weekly_report_title}",
            "",
            "## Executive Summary",
            "",
            draft.executive_summary,
            "",
            "## Report Scope",
            "",
            *_report_scope_lines(draft, persisted=persisted),
            "",
            "## Reviewed Papers",
            "",
            "| paper_id | title | full_text_status | source_text_sha256 | evidence_refs |",
            "| --- | --- | --- | --- | --- |",
            *paper_rows,
            "",
            "## Cross-paper Synthesis",
            "",
            draft.cross_paper_synthesis,
            "",
            "## Methodology and Limitations",
            "",
            draft.methodology_or_scope_note,
            "",
            limitations,
            "",
            "## Research Gaps / Next Topic Candidates",
            "",
            topics,
            "",
            "## Evidence Appendix",
            "",
            f"- evidence refs used: {evidence_refs}",
            f"- paper review proof refs used: {', '.join(draft.proof_artifact_refs_used) or '-'}",
            f"- confidence/caveats: {draft.confidence_or_caveats}",
        ]
    ).strip()


def build_provider_report_preview_context(record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft) -> dict[str, Any]:
    draft = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    return {
        "status": "provider_weekly_report_draft_proof_available",
        "providerReportDraftCount": 1,
        "sourceProviderReviewCount": len(draft.source_provider_review_ids),
        "evidenceSpanCount": len(draft.evidence_span_refs_used),
        "providerFamilyId": draft.provider_family_id,
        "providerSurfaceId": draft.provider_surface_id,
        "topicId": draft.topic_id,
        "sourcePaperIds": list(draft.source_paper_ids),
        "sourceProviderReviewIds": list(draft.source_provider_review_ids),
        "evidenceSpanRefsUsed": list(draft.evidence_span_refs_used),
        "proofArtifactRefs": [draft.proof_artifact_ref, *list(draft.proof_artifact_refs_used)],
        "outputClass": draft.output_class,
        "internalOutputClass": draft.internal_output_class,
        "reportPersistenceStatus": REAL_APPLY_BLOCKED_STATUS,
        "reportDraftAllowed": True,
        "reportPersistenceInScope": False,
        "dbWriteInScope": False,
        "messages": [
            "provider-backed weekly report draft proof available",
            "report persistence still blocked pending owner approval",
            "not production/external beta-ready",
        ],
        "reasonCodes": [
            "provider_weekly_report_proof_available",
            "report_draft_allowed_scoped_to_output_class_proof",
            "real_report_persistence_blocked_pending_owner_approval",
        ],
    }


def _quality_review_rows(draft: NormalizedProviderReportDraft) -> list[dict[str, Any]]:
    source_hash = _source_hash(draft)
    return [
        {
            "paper_id": paper_id,
            "title": next(
                (
                    str(item.get("paper_title") or "")
                    for item in draft.reviewed_papers_summary
                    if str(item.get("paper_id") or "") == paper_id
                ),
                paper_id,
            ),
            "full_text_status": "reviewed",
            "source_text_sha256": source_hash,
            "text_char_count": 0,
            "evidence_spans": [
                {
                    "span_id": ref,
                    "label": ref,
                    "text": "provider weekly report proof cited this evidence ref",
                    "proof_artifact_ref": draft.proof_artifact_ref,
                }
                for ref in draft.evidence_span_refs_used
            ],
        }
        for paper_id in draft.source_paper_ids
    ]


def build_provider_report_quality_context(record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft) -> dict[str, Any]:
    draft = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    markdown = build_provider_report_preview_markdown(draft)
    reviews = _quality_review_rows(draft)
    quality = report_quality.evaluate_weekly_journal_quality(
        markdown,
        metadata={
            "topic": draft.topic_id,
            "reviewed_count": len(reviews),
            "total_count": len(reviews),
        },
        reviews=reviews,
        provider_provenance={"realProviderAcceptedCount": len(draft.source_provider_review_ids)},
        expected_topic=draft.topic_id,
        evaluated_at=iso_now_utc(),
    )
    return {
        "status": "deterministic_report_quality_evaluated",
        "evaluationMethod": quality.get("evaluation_method"),
        "rubricVersion": quality.get("rubric_version"),
        "overallStatus": quality.get("overall_status"),
        "h005TotalScore": quality.get("h005_total_score"),
        "h005MaxScore": quality.get("h005_max_score"),
        "criticalFailFlags": list(quality.get("critical_fail_flags") or []),
        "failureReasons": list(quality.get("failure_reasons") or []),
        "warningReasons": list(quality.get("warning_reasons") or [])[:8],
        "deterministicLocalRubricOnly": True,
        "providerCallPerformed": False,
        "realReportPersistencePerformed": False,
        "reasonCodes": ["deterministic_local_report_quality_context_only"],
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _temp_target_allowed(path: Path) -> bool:
    resolved = path.resolve()
    protected_storage = (_repo_root() / "storage").resolve()
    return protected_storage not in resolved.parents and resolved != protected_storage


def apply_provider_report_to_temp_storage(
    record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft,
    *,
    target_dir: str | Path,
) -> dict[str, Any]:
    draft = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    target = Path(target_dir)
    if not _temp_target_allowed(target):
        raise ProviderReportIntegrationError(["temp_storage_target_must_not_be_repo_storage"])
    target.mkdir(parents=True, exist_ok=True)
    draft_path = target / "provider-report-draft.json"
    preview_path = target / "provider-report-preview.md"
    draft_path.write_text(json.dumps(draft.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    preview_path.write_text(build_provider_report_preview_markdown(draft) + "\n", encoding="utf-8")
    readback = json.loads(draft_path.read_text(encoding="utf-8"))
    return {
        "status": "temp_apply_succeeded",
        "applied": True,
        "tempStorageOnly": True,
        "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
        "providerReportDraftId": draft.provider_report_draft_id,
        "draftPath": str(draft_path),
        "previewPath": str(preview_path),
        "readbackMatches": readback.get("provider_report_draft_id") == draft.provider_report_draft_id,
        "reasonCodes": ["temp_storage_apply_succeeded", "real_report_persistence_blocked_pending_owner_approval"],
    }


def _relative_to_repo(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(_repo_root().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_storage_paths(draft: NormalizedProviderReportDraft) -> dict[str, Path]:
    paths = build_run_paths(draft.provider_report_draft_id)
    return {
        "runDir": paths.run_dir,
        "reportsDir": paths.reports_dir,
        "runJsonPath": paths.run_json_path,
        "reportPath": paths.reports_dir / REPORT_ARTIFACT_FILENAME,
    }


def _report_section_rows(draft: NormalizedProviderReportDraft, *, updated_at: str) -> list[dict[str, Any]]:
    return [
        {
            "section_id": "executive-summary",
            "title": "Executive Summary",
            "claim_count": 1 if draft.executive_summary else 0,
            "citation_count": 0,
            "updated_at": updated_at,
        },
        {
            "section_id": "report-scope",
            "title": "Report Scope",
            "claim_count": len(draft.source_provider_review_ids),
            "citation_count": len(draft.proof_artifact_refs_used) + 1,
            "updated_at": updated_at,
        },
        {
            "section_id": "reviewed-papers",
            "title": "Reviewed Papers",
            "claim_count": len(draft.reviewed_papers_summary),
            "citation_count": len(draft.evidence_span_refs_used),
            "updated_at": updated_at,
        },
        {
            "section_id": "cross-paper-synthesis",
            "title": "Cross-paper Synthesis",
            "claim_count": 1 if draft.cross_paper_synthesis else 0,
            "citation_count": len(draft.evidence_span_refs_used),
            "updated_at": updated_at,
        },
        {
            "section_id": "methodology-and-limitations",
            "title": "Methodology and Limitations",
            "claim_count": len(draft.limitations) + (1 if draft.methodology_or_scope_note else 0),
            "citation_count": 0,
            "updated_at": updated_at,
        },
        {
            "section_id": "research-gaps-next-topics",
            "title": "Research Gaps / Next Topic Candidates",
            "claim_count": len(draft.recommended_next_research_topics),
            "citation_count": len(draft.evidence_span_refs_used),
            "updated_at": updated_at,
        },
        {
            "section_id": "evidence-appendix",
            "title": "Evidence Appendix",
            "claim_count": len(draft.evidence_span_refs_used),
            "citation_count": len(draft.evidence_span_refs_used) + len(draft.proof_artifact_refs_used),
            "updated_at": updated_at,
        },
    ]


def _expected_run_record(draft: NormalizedProviderReportDraft, *, created_at: str) -> dict[str, Any]:
    return {
        "run_id": draft.provider_report_draft_id,
        "topic": draft.topic_name or draft.topic_id,
        "status": "completed",
        "source": f"{REPORT_SOURCE}:weekly_report",
        "write_legacy": False,
        "created_at": created_at,
        "updated_at": created_at,
        "started_at": created_at,
        "finished_at": created_at,
        "request": {
            "kind": PRODUCT_OUTPUT_CLASS,
            "task_type": PRODUCT_OUTPUT_CLASS,
            "provider_report_draft_id": draft.provider_report_draft_id,
            "provider_family_id": draft.provider_family_id,
            "provider_surface_id": draft.provider_surface_id,
            "proof_artifact_ref": draft.proof_artifact_ref,
            "source_provider_review_ids": list(draft.source_provider_review_ids),
            "source_paper_ids": list(draft.source_paper_ids),
            "evidence_span_refs_used": list(draft.evidence_span_refs_used),
        },
        "steps": [
            {
                "name": "provider_weekly_report_real_apply",
                "status": "completed",
                "output_hint": f"run:reports/{REPORT_ARTIFACT_FILENAME}",
                "note": "Accepted provider weekly report proof persisted without provider execution.",
            }
        ],
        "artifacts": {
            "report_markdown": f"run:reports/{REPORT_ARTIFACT_FILENAME}",
            "provider_output_class_proof": draft.proof_artifact_ref,
        },
        "counts": {
            "provider_report_draft_count": 1,
            "source_provider_review_count": len(draft.source_provider_review_ids),
            "source_paper_count": len(draft.source_paper_ids),
            "evidence_span_count": len(draft.evidence_span_refs_used),
            "report_section_count": len(_report_section_rows(draft, updated_at=created_at)),
        },
        "warnings": [
            "Provider-backed weekly report draft persisted from an accepted output-class proof only.",
            "This is not production-ready or external beta-ready.",
        ],
        "error": None,
        "legacy_compatibility": {},
        "compare_ready": {},
        "rerun_context": {},
        "owner_user_id": None,
        "owner_assigned_at": None,
        "owner_assigned_by": None,
        "ownership_source": None,
        "topic_profile_id": draft.topic_id,
        "schedule_context": {
            "task_type": PRODUCT_OUTPUT_CLASS,
            "provider_report_draft_id": draft.provider_report_draft_id,
        },
        "lineage": {
            "root_run_id": draft.provider_report_draft_id,
            "parent_run_id": None,
            "lineage_mode": "provider_report_output_class_apply",
            "source_paper_ids": list(draft.source_paper_ids),
            "created_from_profile_snapshot": False,
            "compare_baseline_run_id": None,
            "branch_reason": "accepted_provider_weekly_report_output_class_proof",
            "created_at": created_at,
            "notes": "Provider report draft persisted from accepted Codex SDK weekly report proof.",
        },
        "provider_report": {
            "provider_report_draft_id": draft.provider_report_draft_id,
            "provider_family_id": draft.provider_family_id,
            "provider_surface_id": draft.provider_surface_id,
            "proof_artifact_ref": draft.proof_artifact_ref,
            "proof_artifact_refs_used": list(draft.proof_artifact_refs_used),
            "source_provider_review_ids": list(draft.source_provider_review_ids),
            "source_paper_ids": list(draft.source_paper_ids),
            "evidence_span_refs_used": list(draft.evidence_span_refs_used),
            "report_source": draft.report_source,
            "output_class": draft.output_class,
            "internal_output_class": draft.internal_output_class,
            "provider_auth_is_app_oauth_oidc": False,
            "local_mcp_is_public_connector": False,
            "production_ready": False,
            "external_beta_ready": False,
            "real_apply_status": REAL_APPLY_APPLIED_STATUS,
        },
    }


def _run_record_matches_expected(run: Mapping[str, Any] | None, draft: NormalizedProviderReportDraft) -> bool:
    if not run:
        return False
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), Mapping) else {}
    request = run.get("request") if isinstance(run.get("request"), Mapping) else {}
    provider_report = run.get("provider_report") if isinstance(run.get("provider_report"), Mapping) else {}
    return all(
        [
            run.get("run_id") == draft.provider_report_draft_id,
            run.get("status") == "completed",
            run.get("source") == f"{REPORT_SOURCE}:weekly_report",
            run.get("topic_profile_id") == draft.topic_id,
            artifacts.get("report_markdown") == f"run:reports/{REPORT_ARTIFACT_FILENAME}"
            or str(artifacts.get("report_markdown") or "").endswith(f"/{draft.provider_report_draft_id}/reports/{REPORT_ARTIFACT_FILENAME}"),
            artifacts.get("provider_output_class_proof") == draft.proof_artifact_ref,
            request.get("provider_report_draft_id") == draft.provider_report_draft_id,
            provider_report.get("proof_artifact_ref") == draft.proof_artifact_ref,
            provider_report.get("source_provider_review_ids") == list(draft.source_provider_review_ids),
            provider_report.get("source_paper_ids") == list(draft.source_paper_ids),
            provider_report.get("evidence_span_refs_used") == list(draft.evidence_span_refs_used),
            provider_report.get("production_ready") is False,
            provider_report.get("external_beta_ready") is False,
        ]
    )


def _row_evidence_span_refs(row: Mapping[str, Any] | None) -> list[str]:
    if not row:
        return []
    spans = row.get("evidence_spans") or []
    if not isinstance(spans, list):
        return []
    return [
        str(span.get("span_id") or span.get("ref") or "").strip()
        for span in spans
        if isinstance(span, Mapping) and str(span.get("span_id") or span.get("ref") or "").strip()
    ]


def _row_proof_refs(row: Mapping[str, Any] | None) -> list[str]:
    if not row:
        return []
    spans = row.get("evidence_spans") or []
    if not isinstance(spans, list):
        return []
    return list(
        dict.fromkeys(
            str(span.get("proof_artifact_ref") or "").strip()
            for span in spans
            if isinstance(span, Mapping) and str(span.get("proof_artifact_ref") or "").strip()
        )
    )


def _source_provider_review_status(draft: NormalizedProviderReportDraft) -> dict[str, Any]:
    row = paper_reviews.get_review(
        draft.topic_id,
        REQUIRED_SOURCE_PROVIDER_REVIEW_ID,
        REQUIRED_SOURCE_PAPER_ID,
        read_only=True,
    )
    evidence_refs = _row_evidence_span_refs(row)
    proof_refs = _row_proof_refs(row)
    matches = bool(
        row
        and row.get("source_provider") == PROVIDER_FAMILY_ID
        and row.get("review_model") == f"{PROVIDER_SURFACE_ID}/{PROOF_MODE}"
        and row.get("reviewer") == f"provider:{PROVIDER_SURFACE_ID}"
        and row.get("review_source_type") == REPORT_SOURCE
        and row.get("full_text_status") == "reviewed"
        and REQUIRED_SOURCE_PROVIDER_REVIEW_ID in draft.source_provider_review_ids
        and REQUIRED_SOURCE_PAPER_ID in draft.source_paper_ids
        and all(ref in evidence_refs for ref in draft.evidence_span_refs_used)
        and all(ref in proof_refs for ref in draft.proof_artifact_refs_used)
    )
    return {
        "status": "matched" if matches else "missing" if row is None else "mismatch",
        "matches": matches,
        "providerReviewId": REQUIRED_SOURCE_PROVIDER_REVIEW_ID,
        "topicId": draft.topic_id,
        "paperId": REQUIRED_SOURCE_PAPER_ID,
        "evidenceSpanRefsUsed": evidence_refs,
        "proofArtifactRefs": proof_refs,
        "reasonCodes": ["provider_review_source_readback_verified"] if matches else ["PROVIDER_REVIEW_SOURCE_MISSING" if row is None else "PROVIDER_REVIEW_SOURCE_MISMATCH"],
    }


def validate_provider_report_real_db_applicability(
    draft: NormalizedProviderReportDraft,
) -> dict[str, Any]:
    metadata_path = metadata_index_path(ensure_parent=False)
    reasons: list[str] = []
    source_status = _source_provider_review_status(draft)
    if not source_status["matches"]:
        reasons.extend(str(reason) for reason in source_status.get("reasonCodes") or [])

    with connect_metadata_index(read_only=True) as connection:
        table_names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        runs_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        report_sections_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(report_sections)").fetchall()
        }
    required_tables = {"runs", "run_topics", "report_sections"}
    required_run_columns = {"run_id", "topic", "topic_slug", "status", "report_path"}
    required_section_columns = {"run_id", "section_id", "title", "claim_count", "citation_count", "updated_at"}
    schema_path_available = (
        required_tables.issubset(table_names)
        and required_run_columns.issubset(runs_columns)
        and required_section_columns.issubset(report_sections_columns)
    )
    if not schema_path_available:
        reasons.append("REPORT_SCHEMA_MAPPING_MISSING")

    paths = _report_storage_paths(draft)
    report_path = paths["reportPath"].resolve()
    storage_root = build_run_paths(draft.provider_report_draft_id).run_dir.parent.resolve()
    try:
        report_path.relative_to(storage_root)
    except ValueError:
        reasons.append("WEEKLY_PROOF_ARTIFACT_NOT_REAL_REPORT_APPLICABLE")
        reasons.append("report_artifact_target_outside_storage_runs")

    return {
        "valid": not reasons,
        "metadataIndexExists": metadata_path.exists(),
        "schemaPathAvailable": schema_path_available,
        "acceptedExistingSchemaPath": schema_path_available,
        "sourceProviderReviewStatus": source_status,
        "reportArtifactPath": _relative_to_repo(paths["reportPath"]),
        "runJsonPath": _relative_to_repo(paths["runJsonPath"]),
        "reasonCodes": _dedupe(reasons)
        if reasons
        else (
            "provider_review_source_readback_verified",
            "runs_report_sections_schema_path_available",
            "report_artifact_storage_path_available",
        ),
    }


def _db_report_status(draft: NormalizedProviderReportDraft) -> dict[str, Any]:
    with connect_metadata_index(read_only=True) as connection:
        run_row = connection.execute(
            """
            SELECT run_id, topic, topic_slug, status, root_run_id, lineage_mode,
                   report_section_count, report_path
            FROM runs
            WHERE run_id = ?
            """,
            (draft.provider_report_draft_id,),
        ).fetchone()
        topic_row = connection.execute(
            "SELECT run_id, topic FROM run_topics WHERE run_id = ? AND topic = ?",
            (draft.provider_report_draft_id, draft.topic_name or draft.topic_id),
        ).fetchone()
        section_rows = connection.execute(
            "SELECT section_id, title, claim_count, citation_count FROM report_sections WHERE run_id = ? ORDER BY section_id",
            (draft.provider_report_draft_id,),
        ).fetchall()
    return {
        "runRow": dict(run_row) if run_row else None,
        "topicRow": dict(topic_row) if topic_row else None,
        "sectionRows": [dict(row) for row in section_rows],
    }


def _read_raw_run_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_report_readback_summary(
    draft: NormalizedProviderReportDraft,
    *,
    exact: bool,
    conflict: bool,
) -> dict[str, Any]:
    paths = _report_storage_paths(draft)
    db_status = _db_report_status(draft)
    raw_run = _read_raw_run_record(paths["runJsonPath"])
    report_exists = paths["reportPath"].is_file()
    return {
        "providerReportDraftId": draft.provider_report_draft_id,
        "topicId": draft.topic_id,
        "sourceProviderReviewIds": list(draft.source_provider_review_ids),
        "sourcePaperIds": list(draft.source_paper_ids),
        "evidenceSpanRefsUsed": list(draft.evidence_span_refs_used),
        "proofArtifactRefs": [draft.proof_artifact_ref, *list(draft.proof_artifact_refs_used)],
        "reportArtifactPath": _relative_to_repo(paths["reportPath"]),
        "runJsonPath": _relative_to_repo(paths["runJsonPath"]),
        "reportArtifactExists": report_exists,
        "reportArtifactSha256": _sha256_file(paths["reportPath"]),
        "runJsonExists": paths["runJsonPath"].is_file(),
        "dbRunExists": db_status["runRow"] is not None,
        "dbReportSectionCount": len(db_status["sectionRows"]),
        "runRecordMatches": _run_record_matches_expected(raw_run, draft),
        "exactReportMatches": exact,
        "conflict": conflict,
    }


def read_provider_report_real_db_status(draft: NormalizedProviderReportDraft) -> dict[str, Any]:
    paths = _report_storage_paths(draft)
    expected_markdown = build_provider_report_preview_markdown(draft, persisted=True) + "\n"
    report_exists = paths["reportPath"].is_file()
    report_matches = report_exists and paths["reportPath"].read_text(encoding="utf-8", errors="ignore") == expected_markdown
    raw_run = _read_raw_run_record(paths["runJsonPath"])
    run_matches = _run_record_matches_expected(raw_run, draft)
    db_status = _db_report_status(draft)
    section_count = len(db_status["sectionRows"])
    db_matches = bool(
        db_status["runRow"]
        and db_status["topicRow"]
        and db_status["runRow"].get("status") == "completed"
        and db_status["runRow"].get("report_path") == _relative_to_repo(paths["reportPath"])
        and int(db_status["runRow"].get("report_section_count") or 0) == section_count
        and section_count == len(_report_section_rows(draft, updated_at=iso_now_utc()))
    )
    any_existing = report_exists or paths["runJsonPath"].exists() or db_status["runRow"] is not None or bool(db_status["sectionRows"])
    exact = report_matches and run_matches and db_matches
    conflict = any_existing and not exact
    if conflict:
        real_apply_status = REAL_APPLY_CONFLICT_STATUS
        status = "conflict"
        reasons = ["PROVIDER_REPORT_DRAFT_CONFLICT"]
    elif exact:
        real_apply_status = REAL_APPLY_ALREADY_APPLIED_STATUS
        status = "already_applied"
        reasons = ["PROVIDER_REPORT_DRAFT_ALREADY_APPLIED", "provider_report_draft_readback_verified"]
    else:
        real_apply_status = REAL_APPLY_BLOCKED_STATUS
        status = "missing"
        reasons = ["provider_report_draft_not_yet_applied"]
    return {
        "status": status,
        "realApplyStatus": real_apply_status,
        "providerReportDraftId": draft.provider_report_draft_id,
        "exactReportMatches": exact,
        "conflict": conflict,
        "readback": _safe_report_readback_summary(draft, exact=exact, conflict=conflict),
        "reasonCodes": reasons,
    }


def create_provider_report_persistence_backup(
    draft: NormalizedProviderReportDraft,
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    source = metadata_index_path(ensure_parent=False)
    if not source.is_file():
        raise ProviderReportIntegrationError(["REPORT_BACKUP_FAILED", "metadata_index_missing"])
    safe_timestamp = "".join(char if char.isalnum() else "-" for char in (timestamp or iso_now_utc())).strip("-")
    backup_dir = source.parent / "backups" / f"provider-report-real-apply-{safe_timestamp}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REPORT_BACKUP_FAILED"]) from exc
    db_target = backup_dir / "metadata_index-pre-provider-report-apply.sqlite3"
    try:
        shutil.copy2(source, db_target)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REPORT_BACKUP_FAILED"]) from exc
    paths = _report_storage_paths(draft)
    manifest = {
        "schemaVersion": "provider-report-real-apply-backup-v1",
        "providerReportDraftId": draft.provider_report_draft_id,
        "createdAt": iso_now_utc(),
        "metadataIndexSource": _relative_to_repo(source),
        "metadataIndexBackup": _relative_to_repo(db_target),
        "targets": {
            "runJson": {
                "path": _relative_to_repo(paths["runJsonPath"]),
                "existsBeforeApply": paths["runJsonPath"].exists(),
                "sha256BeforeApply": _sha256_file(paths["runJsonPath"]),
            },
            "reportArtifact": {
                "path": _relative_to_repo(paths["reportPath"]),
                "existsBeforeApply": paths["reportPath"].exists(),
                "sha256BeforeApply": _sha256_file(paths["reportPath"]),
            },
        },
    }
    manifest_path = backup_dir / "report-target-pre-apply-manifest.json"
    try:
        write_json(manifest_path, manifest)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REPORT_BACKUP_FAILED"]) from exc
    return {
        "backupDir": _relative_to_repo(backup_dir),
        "metadataBackupPath": _relative_to_repo(db_target),
        "targetManifestPath": _relative_to_repo(manifest_path),
        "created": True,
    }


def create_provider_report_quality_disclosure_backup(
    draft: NormalizedProviderReportDraft,
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    paths = _report_storage_paths(draft)
    if not paths["runJsonPath"].is_file() or not paths["reportPath"].is_file():
        raise ProviderReportIntegrationError(["PROVIDER_REPORT_DRAFT_MISSING"])
    safe_timestamp = "".join(char if char.isalnum() else "-" for char in (timestamp or iso_now_utc())).strip("-")
    backup_dir = metadata_index_path(ensure_parent=False).parent / "backups" / f"provider-report-quality-disclosure-fix-{safe_timestamp}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)
        run_backup = backup_dir / "run-pre-quality-disclosure.json"
        report_backup = backup_dir / "provider-weekly-report-draft-pre-quality-disclosure.md"
        shutil.copy2(paths["runJsonPath"], run_backup)
        shutil.copy2(paths["reportPath"], report_backup)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REPORT_BACKUP_FAILED"]) from exc

    manifest = {
        "schemaVersion": "provider-report-quality-disclosure-fix-backup-v1",
        "providerReportDraftId": draft.provider_report_draft_id,
        "createdAt": iso_now_utc(),
        "targets": {
            "runJson": {
                "path": _relative_to_repo(paths["runJsonPath"]),
                "backupPath": _relative_to_repo(run_backup),
                "sha256BeforePatch": _sha256_file(paths["runJsonPath"]),
            },
            "reportArtifact": {
                "path": _relative_to_repo(paths["reportPath"]),
                "backupPath": _relative_to_repo(report_backup),
                "sha256BeforePatch": _sha256_file(paths["reportPath"]),
            },
        },
        "metadataIndex": {
            "path": _relative_to_repo(metadata_index_path(ensure_parent=False)),
            "sha256BeforePatch": _sha256_file(metadata_index_path(ensure_parent=False)),
            "mutatedByPatch": False,
        },
    }
    manifest_path = backup_dir / "quality-disclosure-pre-patch-manifest.json"
    try:
        write_json(manifest_path, manifest)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REPORT_BACKUP_FAILED"]) from exc
    return {
        "backupDir": _relative_to_repo(backup_dir),
        "runJsonBackupPath": _relative_to_repo(run_backup),
        "reportArtifactBackupPath": _relative_to_repo(report_backup),
        "targetManifestPath": _relative_to_repo(manifest_path),
        "created": True,
    }


def _provider_report_quality_for_markdown(draft: NormalizedProviderReportDraft, markdown: str) -> dict[str, Any]:
    source_review = paper_reviews.get_review(
        draft.topic_id,
        REQUIRED_SOURCE_PROVIDER_REVIEW_ID,
        REQUIRED_SOURCE_PAPER_ID,
        read_only=True,
    )
    reviews = [source_review] if source_review else []
    return report_quality.evaluate_weekly_journal_quality(
        markdown,
        metadata={
            "topic": draft.topic_id,
            "reviewed_count": len(draft.source_provider_review_ids),
            "total_count": max(1, len(draft.source_provider_review_ids)),
        },
        reviews=reviews,
        provider_provenance={"realProviderAcceptedCount": len(draft.source_provider_review_ids)},
        expected_topic=draft.topic_id,
        evaluated_at=iso_now_utc(),
    )


def _quality_summary(quality: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "overallStatus": quality.get("overall_status"),
        "overallScore": quality.get("overall_score"),
        "h005TotalScore": quality.get("h005_total_score"),
        "h005MaxScore": quality.get("h005_max_score"),
        "failureReasons": list(quality.get("failure_reasons") or []),
        "warningReasons": list(quality.get("warning_reasons") or []),
        "criticalFailFlags": list(quality.get("critical_fail_flags") or []),
        "coverageDisclosureDetected": bool(quality.get("coverage_disclosure_detected")),
    }


def apply_provider_report_quality_disclosure_patch(
    record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft,
    *,
    backup_timestamp: str | None = None,
) -> dict[str, Any]:
    draft = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    if not validate_provider_report_real_db_applicability(draft)["valid"]:
        raise ProviderReportIntegrationError(["PROVIDER_REPORT_DRAFT_READBACK_MISMATCH"])
    paths = _report_storage_paths(draft)
    if not paths["runJsonPath"].is_file() or not paths["reportPath"].is_file():
        raise ProviderReportIntegrationError(["PROVIDER_REPORT_DRAFT_MISSING"])

    before_markdown = paths["reportPath"].read_text(encoding="utf-8")
    before_quality = _provider_report_quality_for_markdown(draft, before_markdown)
    before_failures = set(before_quality.get("failure_reasons") or [])
    before_hash = _sha256_file(paths["reportPath"])
    target_markdown = build_provider_report_preview_markdown(draft, persisted=True) + "\n"
    if before_markdown == target_markdown and "missing_coverage_disclosure" not in before_failures:
        return {
            "status": "already_disclosed",
            "patched": False,
            "providerReportDraftId": draft.provider_report_draft_id,
            "backup": None,
            "beforeReportSha256": before_hash,
            "afterReportSha256": before_hash,
            "beforeQuality": _quality_summary(before_quality),
            "afterQuality": _quality_summary(before_quality),
            "readback": read_provider_report_real_db_status(draft),
            "reasonCodes": ["provider_report_quality_disclosure_already_present"],
        }
    if "missing_coverage_disclosure" not in before_failures:
        raise ProviderReportIntegrationError(["REPORT_QUALITY_MISSING_COVERAGE_DISCLOSURE_NOT_REPRODUCED"])

    backup = create_provider_report_quality_disclosure_backup(draft, timestamp=backup_timestamp)
    try:
        write_text(paths["reportPath"], target_markdown)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REAL_REPORT_DISCLOSURE_PATCH_FAILED"]) from exc

    after_markdown = paths["reportPath"].read_text(encoding="utf-8")
    after_quality = _provider_report_quality_for_markdown(draft, after_markdown)
    after_failures = set(after_quality.get("failure_reasons") or [])
    if "missing_coverage_disclosure" in after_failures:
        raise ProviderReportIntegrationError(["REPORT_QUALITY_STILL_FAILS_MISSING_COVERAGE_DISCLOSURE"])
    if after_quality.get("critical_fail_flags"):
        raise ProviderReportIntegrationError(["REAL_REPORT_DISCLOSURE_PATCH_FAILED"])
    readback = read_provider_report_real_db_status(draft)
    if not readback.get("exactReportMatches"):
        raise ProviderReportIntegrationError(["POST_PATCH_READBACK_FAILED"])
    return {
        "status": "quality_disclosure_patched",
        "patched": True,
        "providerReportDraftId": draft.provider_report_draft_id,
        "backup": backup,
        "targets": {
            "reportArtifact": _relative_to_repo(paths["reportPath"]),
            "runJson": _relative_to_repo(paths["runJsonPath"]),
            "metadataIndex": _relative_to_repo(metadata_index_path(ensure_parent=False)),
        },
        "beforeReportSha256": before_hash,
        "afterReportSha256": _sha256_file(paths["reportPath"]),
        "beforeQuality": _quality_summary(before_quality),
        "afterQuality": _quality_summary(after_quality),
        "readback": readback,
        "reasonCodes": ["provider_report_quality_disclosure_patched", "missing_coverage_disclosure_resolved"],
    }


def _insert_provider_report_metadata(
    draft: NormalizedProviderReportDraft,
    *,
    report_path: Path,
    created_at: str,
) -> None:
    sections = _report_section_rows(draft, updated_at=created_at)
    topic = draft.topic_name or draft.topic_id
    with connect_metadata_index() as connection:
        connection.execute(
            """
            INSERT INTO runs (
              run_id, topic, topic_slug, status, created_at, updated_at, started_at, finished_at,
              root_run_id, parent_run_id, lineage_mode, preview_hash, triage_items, reviewed_items,
              evidence_count, claim_count, report_section_count, research_plan_path, report_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              topic = excluded.topic,
              topic_slug = excluded.topic_slug,
              status = excluded.status,
              updated_at = excluded.updated_at,
              finished_at = excluded.finished_at,
              lineage_mode = excluded.lineage_mode,
              preview_hash = excluded.preview_hash,
              triage_items = excluded.triage_items,
              reviewed_items = excluded.reviewed_items,
              evidence_count = excluded.evidence_count,
              claim_count = excluded.claim_count,
              report_section_count = excluded.report_section_count,
              research_plan_path = excluded.research_plan_path,
              report_path = excluded.report_path
            """,
            (
                draft.provider_report_draft_id,
                topic,
                slugify(topic),
                "completed",
                created_at,
                created_at,
                created_at,
                created_at,
                draft.provider_report_draft_id,
                None,
                "provider_report_output_class_apply",
                hashlib.sha1(build_provider_report_preview_markdown(draft, persisted=True).encode("utf-8")).hexdigest()[:12],
                0,
                len(draft.source_provider_review_ids),
                len(draft.evidence_span_refs_used),
                sum(int(section["claim_count"]) for section in sections),
                len(sections),
                None,
                _relative_to_repo(report_path),
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO run_topics (run_id, topic) VALUES (?, ?)",
            (draft.provider_report_draft_id, topic),
        )
        connection.execute("DELETE FROM report_sections WHERE run_id = ?", (draft.provider_report_draft_id,))
        for section in sections:
            connection.execute(
                """
                INSERT INTO report_sections (run_id, section_id, title, claim_count, citation_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.provider_report_draft_id,
                    section["section_id"],
                    section["title"],
                    int(section["claim_count"]),
                    int(section["citation_count"]),
                    section["updated_at"],
                ),
            )


def apply_provider_report_to_real_storage(
    record: ProviderWeeklyReportProofRecord | NormalizedProviderReportDraft,
    *,
    backup_timestamp: str | None = None,
) -> dict[str, Any]:
    draft = record if isinstance(record, NormalizedProviderReportDraft) else normalize_provider_weekly_report_for_storage(record)
    applicability = validate_provider_report_real_db_applicability(draft)
    if not applicability["valid"]:
        raise ProviderReportIntegrationError(list(applicability["reasonCodes"]))

    before_status = read_provider_report_real_db_status(draft)
    if before_status["conflict"]:
        raise ProviderReportIntegrationError(["PROVIDER_REPORT_DRAFT_CONFLICT"])
    if before_status["exactReportMatches"]:
        return {
            "status": "already_applied",
            "applied": False,
            "providerReportDraftId": draft.provider_report_draft_id,
            "backup": None,
            "readback": before_status,
            "applicability": applicability,
            "realApplyStatus": REAL_APPLY_ALREADY_APPLIED_STATUS,
            "reasonCodes": ["PROVIDER_REPORT_DRAFT_ALREADY_APPLIED", "provider_report_draft_readback_verified"],
        }

    backup = create_provider_report_persistence_backup(draft, timestamp=backup_timestamp)
    created_at = iso_now_utc()
    paths = _report_storage_paths(draft)
    try:
        write_text(paths["reportPath"], build_provider_report_preview_markdown(draft, persisted=True) + "\n")
        write_json(paths["runJsonPath"], _expected_run_record(draft, created_at=created_at))
        _insert_provider_report_metadata(draft, report_path=paths["reportPath"], created_at=created_at)
        load_run_record(draft.provider_report_draft_id)
    except OSError as exc:
        raise ProviderReportIntegrationError(["REAL_REPORT_APPLY_FAILED"]) from exc

    after_status = read_provider_report_real_db_status(draft)
    if not after_status["exactReportMatches"]:
        raise ProviderReportIntegrationError(["POST_APPLY_REPORT_READBACK_FAILED"])
    return {
        "status": "real_apply_applied",
        "applied": True,
        "providerReportDraftId": draft.provider_report_draft_id,
        "backup": backup,
        "persistenceTargets": {
            "metadataIndex": _relative_to_repo(metadata_index_path(ensure_parent=False)),
            "runJson": _relative_to_repo(paths["runJsonPath"]),
            "reportArtifact": _relative_to_repo(paths["reportPath"]),
        },
        "readback": after_status,
        "applicability": applicability,
        "realApplyStatus": REAL_APPLY_APPLIED_STATUS,
        "reasonCodes": ["provider_report_real_apply_completed", "provider_report_draft_readback_verified"],
    }


def build_provider_report_real_db_report_context(draft: NormalizedProviderReportDraft) -> dict[str, Any]:
    status = read_provider_report_real_db_status(draft)
    exact = bool(status.get("exactReportMatches"))
    return {
        "status": "provider_weekly_report_draft_real_storage_available" if exact else "provider_weekly_report_draft_real_storage_missing",
        "providerReportDraftCount": 1 if exact else 0,
        "providerReportDraftId": draft.provider_report_draft_id,
        "sourceProviderReviewCount": len(draft.source_provider_review_ids) if exact else 0,
        "sourceProviderReviewIds": list(draft.source_provider_review_ids) if exact else [],
        "sourcePaperIds": list(draft.source_paper_ids) if exact else [],
        "evidenceSpanRefsUsed": list(draft.evidence_span_refs_used) if exact else [],
        "proofArtifactRefs": [draft.proof_artifact_ref, *list(draft.proof_artifact_refs_used)] if exact else [],
        "reportPersistenceStatus": status.get("realApplyStatus"),
        "reportDraftAllowed": True,
        "providerAuthIsAppOauthOidc": False,
        "localMcpIsPublicConnector": False,
        "productionReady": False,
        "externalBetaReady": False,
        "reasonCodes": [
            "provider_report_real_storage_readback_verified" if exact else "provider_report_real_storage_missing",
            "report_draft_allowed_scoped_to_output_class_proof",
            "production_external_beta_no_go",
        ],
    }


def build_provider_report_integration_status(
    path: str | Path,
    *,
    include_real_db_status: bool = True,
) -> dict[str, Any]:
    try:
        record = load_provider_weekly_report_output_class_proof(path)
        normalized = normalize_provider_weekly_report_for_storage(record)
    except ProviderReportIntegrationError as exc:
        return {
            "status": "rejected_artifact",
            "normalizedProviderReportDraftAvailable": False,
            "dryRunApplyPlan": {"status": "rejected_artifact", "applied": False},
            "previewContext": {"status": "unavailable"},
            "qualityContext": {"status": "unavailable"},
            "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
            "reportDraftAllowed": False,
            "reasonCodes": exc.reason_codes,
        }
    plan = plan_provider_report_db_apply(normalized)
    preview = build_provider_report_preview_context(normalized)
    quality = build_provider_report_quality_context(normalized)
    try:
        if include_real_db_status:
            real_status = read_provider_report_real_db_status(normalized)
        else:
            real_status = {
                "status": "not_checked",
                "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
                "reasonCodes": ["provider_report_real_db_status_not_checked"],
            }
    except Exception:
        real_status = {
            "status": "unknown",
            "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
            "reasonCodes": ["provider_report_real_db_status_unavailable"],
        }
    real_apply_status = str(real_status.get("realApplyStatus") or REAL_APPLY_BLOCKED_STATUS)
    status = REAL_APPLY_APPLIED_STATUS if real_apply_status in {REAL_APPLY_APPLIED_STATUS, REAL_APPLY_ALREADY_APPLIED_STATUS} else "dry_run_ready"
    if status == REAL_APPLY_APPLIED_STATUS:
        preview = build_provider_report_real_db_report_context(normalized)
    return {
        "status": status,
        "normalizedProviderReportDraftAvailable": True,
        "providerReportDraftId": normalized.provider_report_draft_id,
        "topicId": normalized.topic_id,
        "sourceProviderReviewIds": list(normalized.source_provider_review_ids),
        "sourcePaperIds": list(normalized.source_paper_ids),
        "proofArtifactRef": normalized.proof_artifact_ref,
        "reportSource": normalized.report_source,
        "dryRunApplyPlan": plan,
        "previewContext": preview,
        "qualityContext": quality,
        "realDbStatus": real_status,
        "realApplyStatus": real_apply_status,
        "reportDraftAllowed": True,
        "researchOutputAllowed": False,
        "reportPersistenceInScope": status == REAL_APPLY_APPLIED_STATUS,
        "dbWriteInScope": status == REAL_APPLY_APPLIED_STATUS,
        "providerAuthIsAppOauthOidc": False,
        "localMcpIsPublicConnector": False,
        "productionReady": False,
        "externalBetaReady": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "reasonCodes": [
            "provider_report_integration_dry_run_ready",
            "weekly_report_output_class_proof_present",
            "provider_report_real_storage_applied" if status == REAL_APPLY_APPLIED_STATUS else "real_report_persistence_blocked_pending_owner_approval",
            *list(real_status.get("reasonCodes") or []),
        ],
    }

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from app.db.bootstrap import (
    SCHEMA_SQL,
    connect_metadata_index,
    metadata_index_path,
)
from app.db.sqlite_integrity import (
    DEFAULT_METADATA_WRITE_TIMEOUT_SECONDS,
    STORE_METADATA,
    open_sqlite_read_write,
    require_store_write_compatible,
    sqlite_user_tables,
)
from app.repositories import artifact_refs, paper_reviews
from app.repositories.files import iso_now_utc
from app.services.providers.codex_sdk_provider import (
    validate_codex_sdk_paper_review_output_class_proof_artifact_contract,
)


PROOF_ARTIFACT_KIND = "provider_output_class_proof"
PROOF_MODE = "output_class_live"
PROVIDER_FAMILY_ID = "codex"
PROVIDER_SURFACE_ID = "codex_sdk_python"
PRODUCT_OUTPUT_CLASS = "paper_review"
INTERNAL_OUTPUT_CLASS = "research_output"
REVIEW_SOURCE = "provider_output_class_proof"
REAL_APPLY_BLOCKED_STATUS = "real_apply_blocked_pending_owner_approval"
REAL_APPLY_APPLIED_STATUS = "real_apply_applied"
REAL_APPLY_ALREADY_APPLIED_STATUS = "real_apply_already_applied"
REAL_APPLY_CONFLICT_STATUS = "real_apply_conflict"

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


class ProviderReviewIntegrationError(ValueError):
    def __init__(self, reason_codes: list[str]) -> None:
        super().__init__(", ".join(reason_codes))
        self.reason_codes = list(dict.fromkeys(reason_codes))


@dataclass(frozen=True)
class ProviderReviewEvidenceRef:
    ref: str
    evidence_source_ref: str
    text_preview: str
    char_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "evidenceSourceRef": self.evidence_source_ref,
            "textPreview": self.text_preview,
            "charCount": self.char_count,
        }


@dataclass(frozen=True)
class ProviderPaperReviewProofRecord:
    artifact_ref: str
    timestamp: str | None
    run_id: str | None
    provider_family_id: str
    provider_surface_id: str
    execution_surface_type: str
    adapter_kind: str
    proof_mode: str
    output_class: str
    internal_output_class: str
    current_proof_status: str
    topic_id: str
    topic_name: str
    paper_id: str
    paper_title: str
    evidence_source_ref: str
    evidence_refs: tuple[ProviderReviewEvidenceRef, ...]
    evidence_span_refs_used: tuple[str, ...]
    redacted_provider_output: Mapping[str, Any]
    provider_sdk_run_count: int
    provider_call_count: int
    provider_command_count: int
    research_output_allowed: bool
    report_draft_allowed: bool

    def to_safe_status(self) -> dict[str, Any]:
        return {
            "artifactKind": PROOF_ARTIFACT_KIND,
            "artifactRef": self.artifact_ref,
            "proofMode": self.proof_mode,
            "providerFamilyId": self.provider_family_id,
            "providerSurfaceId": self.provider_surface_id,
            "executionSurfaceType": self.execution_surface_type,
            "adapterKind": self.adapter_kind,
            "outputClass": self.output_class,
            "internalOutputClass": self.internal_output_class,
            "currentProofStatus": self.current_proof_status,
            "topicId": self.topic_id,
            "paperId": self.paper_id,
            "evidenceSpanCount": len(self.evidence_refs),
            "evidenceSpanRefsUsed": list(self.evidence_span_refs_used),
            "providerSdkRunCount": self.provider_sdk_run_count,
            "providerCallCount": self.provider_call_count,
            "providerCommandCount": self.provider_command_count,
            "researchOutputAllowed": self.research_output_allowed,
            "reportDraftAllowed": self.report_draft_allowed,
        }


@dataclass(frozen=True)
class NormalizedProviderReview:
    provider_review_id: str
    provider_family_id: str
    provider_surface_id: str
    proof_artifact_ref: str
    proof_mode: str
    output_class: str
    internal_output_class: str
    topic_id: str
    topic_name: str
    paper_id: str
    paper_title: str
    evidence_source_ref: str
    evidence_span_refs_used: tuple[str, ...]
    evidence_span_count: int
    concise_review_summary: str
    methodology_or_core_approach: str
    key_findings: tuple[str, ...]
    limitations: tuple[str, ...]
    relevance_to_topic: str
    follow_up_topic_suggestions: tuple[str, ...]
    confidence_or_caveats: str
    review_source: str
    provider_auth_is_app_oauth_oidc: bool
    local_mcp_is_public_connector: bool
    production_ready: bool
    external_beta_ready: bool
    no_chain_of_thought: bool
    redaction_status: str
    apply_status: str
    repository_payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_review_id": self.provider_review_id,
            "provider_family_id": self.provider_family_id,
            "provider_surface_id": self.provider_surface_id,
            "proof_artifact_ref": self.proof_artifact_ref,
            "proof_mode": self.proof_mode,
            "output_class": self.output_class,
            "internal_output_class": self.internal_output_class,
            "topic_id": self.topic_id,
            "topic_name": self.topic_name,
            "paper_id": self.paper_id,
            "paper_title": self.paper_title,
            "evidence_source_ref": self.evidence_source_ref,
            "evidence_span_refs_used": list(self.evidence_span_refs_used),
            "evidence_span_count": self.evidence_span_count,
            "concise_review_summary": self.concise_review_summary,
            "methodology_or_core_approach": self.methodology_or_core_approach,
            "key_findings": list(self.key_findings),
            "limitations": list(self.limitations),
            "relevance_to_topic": self.relevance_to_topic,
            "follow_up_topic_suggestions": list(self.follow_up_topic_suggestions),
            "confidence_or_caveats": self.confidence_or_caveats,
            "review_source": self.review_source,
            "provider_auth_is_app_oauth_oidc": self.provider_auth_is_app_oauth_oidc,
            "local_mcp_is_public_connector": self.local_mcp_is_public_connector,
            "production_ready": self.production_ready,
            "external_beta_ready": self.external_beta_ready,
            "no_chain_of_thought": self.no_chain_of_thought,
            "redaction_status": self.redaction_status,
            "apply_status": self.apply_status,
            "repository_payload": dict(self.repository_payload),
        }


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))


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


def _as_nonempty_str(value: Any) -> str:
    return str(value or "").strip()


def _as_nonempty_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _artifact_ref_from_path(path: Path, artifact: Mapping[str, Any]) -> str:
    artifact_ref = _as_nonempty_str(artifact.get("artifactRef"))
    if artifact_ref:
        return artifact_ref.replace("\\", "/")
    normalized = path.as_posix()
    marker = "output/provider-output-class-proof/"
    if marker in normalized:
        return normalized[normalized.index(marker) :]
    return path.name


def _review_id(record: ProviderPaperReviewProofRecord) -> str:
    seed = "|".join(
        [
            record.provider_family_id,
            record.provider_surface_id,
            record.output_class,
            record.internal_output_class,
            record.topic_id,
            record.paper_id,
            record.artifact_ref,
        ]
    )
    return "provider-review-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _source_hash(record: ProviderPaperReviewProofRecord) -> str:
    seed = json.dumps(
        {
            "artifactRef": record.artifact_ref,
            "evidenceRefs": [item.to_dict() for item in record.evidence_refs],
            "paperId": record.paper_id,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _validate_artifact_for_integration(artifact: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if artifact.get("artifactKind") != PROOF_ARTIFACT_KIND:
        reasons.append("artifact_kind_not_provider_output_class_proof")
    if artifact.get("artifactKind") == "provider_tiny_proof":
        reasons.append("provider_tiny_proof_rejected_for_review_integration")
    if artifact.get("proofMode") != PROOF_MODE:
        reasons.append("proof_mode_not_output_class_live")
    if artifact.get("productOutputClass") != PRODUCT_OUTPUT_CLASS:
        reasons.append("not_paper_review_output_class")
    if artifact.get("internalOutputClass") != INTERNAL_OUTPUT_CLASS:
        reasons.append("not_research_output_internal_class")
    fixed = {
        "acceptedOutputClassProof": True,
        "outputClassProof": True,
        "researchOutputAllowed": True,
        "reportDraftAllowed": False,
        "no_chain_of_thought": True,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
    }
    for key, expected in fixed.items():
        if artifact.get(key) != expected:
            reasons.append(f"field_mismatch:{key}")
    if int(artifact.get("evidenceSpanCount") or 0) <= 0:
        reasons.append("missing_evidence_spans")
    if not artifact.get("evidenceSpanRefsUsed"):
        reasons.append("missing_evidence_span_refs_used")
    if artifact.get("weeklyReportProofStatus") != "missing":
        reasons.append("weekly_report_proof_must_remain_missing")
    if artifact.get("providerCommandCount") != 0:
        reasons.append("provider_command_count_must_be_zero")
    if _has_secret_like_value(artifact.get("redactedProviderOutput") or {}):
        reasons.append("secret_like_content_detected")
    if _has_secret_like_value(artifact.get("evidenceSpans") or []):
        reasons.append("secret_like_content_detected")
    if _has_chain_of_thought_like_value(artifact.get("redactedProviderOutput") or {}):
        reasons.append("chain_of_thought_like_content_detected")
    codex_validation = validate_codex_sdk_paper_review_output_class_proof_artifact_contract(artifact)
    if not codex_validation.get("valid"):
        reasons.append("codex_sdk_output_class_artifact_contract_rejected")
        reasons.extend(str(reason) for reason in codex_validation.get("reasonCodes", []))
    return _dedupe(reasons)


def load_provider_paper_review_output_class_proof(path: str | Path) -> ProviderPaperReviewProofRecord:
    artifact_path = Path(path)
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderReviewIntegrationError(["proof_artifact_unreadable"]) from exc
    if not isinstance(artifact, dict):
        raise ProviderReviewIntegrationError(["proof_artifact_not_object"])
    reasons = _validate_artifact_for_integration(artifact)
    if reasons:
        raise ProviderReviewIntegrationError(reasons)

    evidence_refs: list[ProviderReviewEvidenceRef] = []
    for span in artifact.get("evidenceSpans") or []:
        if not isinstance(span, Mapping):
            continue
        ref = _as_nonempty_str(span.get("ref"))
        text_preview = _as_nonempty_str(span.get("redactedExcerpt"))
        if not ref or not text_preview:
            continue
        evidence_refs.append(
            ProviderReviewEvidenceRef(
                ref=ref,
                evidence_source_ref=_as_nonempty_str(artifact.get("evidenceSourceRef")),
                text_preview=text_preview,
                char_count=int(span.get("charCount") or len(text_preview)),
            )
        )
    if not evidence_refs:
        raise ProviderReviewIntegrationError(["missing_evidence_spans"])
    output = artifact.get("redactedProviderOutput") or {}
    if not isinstance(output, Mapping):
        raise ProviderReviewIntegrationError(["provider_output_not_object"])
    return ProviderPaperReviewProofRecord(
        artifact_ref=_artifact_ref_from_path(artifact_path, artifact),
        timestamp=artifact.get("timestamp"),
        run_id=artifact.get("runId"),
        provider_family_id=_as_nonempty_str(artifact.get("providerFamilyId")),
        provider_surface_id=_as_nonempty_str(artifact.get("surfaceId")),
        execution_surface_type=_as_nonempty_str(artifact.get("executionSurfaceType")),
        adapter_kind=_as_nonempty_str(artifact.get("adapterKind")),
        proof_mode=_as_nonempty_str(artifact.get("proofMode")),
        output_class=_as_nonempty_str(artifact.get("productOutputClass")),
        internal_output_class=_as_nonempty_str(artifact.get("internalOutputClass")),
        current_proof_status=_as_nonempty_str(artifact.get("currentProofStatus")),
        topic_id=_as_nonempty_str(artifact.get("topicId")),
        topic_name=_as_nonempty_str(artifact.get("topicName")),
        paper_id=_as_nonempty_str(artifact.get("paperId")),
        paper_title=_as_nonempty_str(artifact.get("paperTitle")),
        evidence_source_ref=_as_nonempty_str(artifact.get("evidenceSourceRef")),
        evidence_refs=tuple(evidence_refs),
        evidence_span_refs_used=_as_nonempty_list(artifact.get("evidenceSpanRefsUsed")),
        redacted_provider_output=dict(output),
        provider_sdk_run_count=int(artifact.get("providerSdkRunCount") or 0),
        provider_call_count=int(artifact.get("providerCallCount") or 0),
        provider_command_count=int(artifact.get("providerCommandCount") or 0),
        research_output_allowed=bool(artifact.get("researchOutputAllowed")),
        report_draft_allowed=bool(artifact.get("reportDraftAllowed")),
    )


def validate_provider_paper_review_proof_for_integration(record: ProviderPaperReviewProofRecord) -> dict[str, Any]:
    reasons: list[str] = []
    if record.provider_family_id != PROVIDER_FAMILY_ID:
        reasons.append("provider_family_mismatch")
    if record.provider_surface_id != PROVIDER_SURFACE_ID:
        reasons.append("provider_surface_mismatch")
    if record.output_class != PRODUCT_OUTPUT_CLASS or record.internal_output_class != INTERNAL_OUTPUT_CLASS:
        reasons.append("output_class_mismatch")
    if record.proof_mode != PROOF_MODE:
        reasons.append("proof_mode_not_output_class_live")
    if not record.research_output_allowed or record.report_draft_allowed:
        reasons.append("output_class_gate_mismatch")
    if not record.evidence_refs or not record.evidence_span_refs_used:
        reasons.append("missing_evidence_spans")
    if record.provider_command_count != 0:
        reasons.append("provider_command_count_must_be_zero")
    if _has_secret_like_value(record.to_safe_status()):
        reasons.append("secret_like_content_detected")
    if _has_secret_like_value(record.redacted_provider_output):
        reasons.append("secret_like_content_detected")
    if _has_chain_of_thought_like_value(record.redacted_provider_output):
        reasons.append("chain_of_thought_like_content_detected")
    return {"valid": not reasons, "reasonCodes": _dedupe(reasons)}


def normalize_provider_paper_review_for_storage(record: ProviderPaperReviewProofRecord) -> NormalizedProviderReview:
    validation = validate_provider_paper_review_proof_for_integration(record)
    if not validation["valid"]:
        raise ProviderReviewIntegrationError(validation["reasonCodes"])
    output = record.redacted_provider_output
    provider_review_id = _review_id(record)
    reviewed_at = _as_nonempty_str(record.timestamp) or iso_now_utc()
    evidence_spans = [
        {
            "span_id": ref.ref,
            "label": ref.ref,
            "text": ref.text_preview,
            "source_path": record.evidence_source_ref,
            "provider_review_id": provider_review_id,
            "proof_artifact_ref": record.artifact_ref,
        }
        for ref in record.evidence_refs
    ]
    repository_payload = {
        "topic_id": record.topic_id,
        "run_id": provider_review_id,
        "paper_id": record.paper_id,
        "title": record.paper_title,
        "source_provider": PROVIDER_FAMILY_ID,
        "abstract_text": None,
        "text_excerpt": _as_nonempty_str(output.get("concise_review_summary")),
        "full_text_status": "reviewed",
        "relevance_score": 100.0,
        "novelty_score": 0.0,
        "evidence_strength": 100.0,
        "method_summary": _as_nonempty_str(output.get("methodology_or_core_approach")),
        "key_findings": list(_as_nonempty_list(output.get("key_findings"))),
        "limitations": list(_as_nonempty_list(output.get("limitations"))),
        "why_it_matters": _as_nonempty_str(output.get("concise_review_summary")),
        "topic_fit_rationale": _as_nonempty_str(output.get("relevance_to_topic")),
        "review_model": f"{PROVIDER_SURFACE_ID}/{PROOF_MODE}",
        "reviewer": f"provider:{PROVIDER_SURFACE_ID}",
        "reviewed_at": reviewed_at,
        "source_text_sha256": _source_hash(record),
        "full_text_artifact_path": record.evidence_source_ref,
        "text_char_count": sum(ref.char_count for ref in record.evidence_refs),
        "evidence_spans": evidence_spans,
        "review_source_type": REVIEW_SOURCE,
        "full_text_reviewed_at": reviewed_at,
    }
    normalized = NormalizedProviderReview(
        provider_review_id=provider_review_id,
        provider_family_id=PROVIDER_FAMILY_ID,
        provider_surface_id=PROVIDER_SURFACE_ID,
        proof_artifact_ref=record.artifact_ref,
        proof_mode=PROOF_MODE,
        output_class=PRODUCT_OUTPUT_CLASS,
        internal_output_class=INTERNAL_OUTPUT_CLASS,
        topic_id=record.topic_id,
        topic_name=record.topic_name,
        paper_id=record.paper_id,
        paper_title=record.paper_title,
        evidence_source_ref=record.evidence_source_ref,
        evidence_span_refs_used=record.evidence_span_refs_used,
        evidence_span_count=len(record.evidence_refs),
        concise_review_summary=_as_nonempty_str(output.get("concise_review_summary")),
        methodology_or_core_approach=_as_nonempty_str(output.get("methodology_or_core_approach")),
        key_findings=_as_nonempty_list(output.get("key_findings")),
        limitations=_as_nonempty_list(output.get("limitations")),
        relevance_to_topic=_as_nonempty_str(output.get("relevance_to_topic")),
        follow_up_topic_suggestions=_as_nonempty_list(output.get("follow_up_topic_suggestions")),
        confidence_or_caveats=_as_nonempty_str(output.get("confidence_or_caveats")),
        review_source=REVIEW_SOURCE,
        provider_auth_is_app_oauth_oidc=False,
        local_mcp_is_public_connector=False,
        production_ready=False,
        external_beta_ready=False,
        no_chain_of_thought=True,
        redaction_status="passed",
        apply_status="dry_run_only",
        repository_payload=repository_payload,
    )
    if _has_secret_like_value(normalized.to_dict()):
        raise ProviderReviewIntegrationError(["secret_like_content_detected"])
    return normalized


def plan_provider_review_db_apply(
    record: ProviderPaperReviewProofRecord | NormalizedProviderReview,
    *,
    mode: str = "dry_run",
) -> dict[str, Any]:
    normalized = record if isinstance(record, NormalizedProviderReview) else normalize_provider_paper_review_for_storage(record)
    known_mode = mode in {"dry_run", "temp_apply"}
    status = "dry_run_ready" if mode == "dry_run" else "temp_apply_ready" if mode == "temp_apply" else "rejected_artifact"
    return {
        "mode": mode,
        "status": status,
        "knownMode": known_mode,
        "applied": False,
        "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
        "targetEntities": ["paper_reviews", "paper_reviews.evidence_spans_json", "report_context"],
        "wouldCreateOrUpdatePaperReviewRow": known_mode,
        "wouldAttachEvidenceSpans": known_mode,
        "wouldLinkProofArtifactRef": known_mode,
        "wouldUpdateRunReportContext": False,
        "wouldMarkProviderProofProvenance": known_mode,
        "topicId": normalized.topic_id,
        "paperId": normalized.paper_id,
        "providerReviewId": normalized.provider_review_id,
        "proofArtifactRef": normalized.proof_artifact_ref,
        "reasonCodes": [
            "provider_review_integration_plan",
            "real_db_apply_blocked_pending_owner_approval",
            "weekly_report_output_class_proof_missing",
        ],
    }


def _json_list(value: Any) -> str:
    if not isinstance(value, list):
        value = [str(value)] if str(value or "").strip() else []
    return json.dumps([str(item) for item in value if str(item).strip()], ensure_ascii=False)


def _json_payload(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _relative_to_repo(path: Path) -> str:
    try:
        return path.resolve().relative_to(_repo_root().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _evidence_ref_path(ref: str, *, repo_root: Path | None = None) -> Path:
    root = (repo_root or _repo_root()).resolve()
    normalized = str(ref or "").replace("\\", "/").strip()
    return (root / normalized).resolve()


def _evidence_ref_is_project_relative_file(ref: str, *, repo_root: Path | None = None) -> bool:
    normalized = str(ref or "").replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or normalized.startswith("~"):
        return False
    if ":" in normalized.split("/", 1)[0]:
        return False
    path = _evidence_ref_path(normalized, repo_root=repo_root)
    root = (repo_root or _repo_root()).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path.is_file()


def _expected_full_text_artifact_path(review: NormalizedProviderReview) -> str | None:
    return artifact_refs.normalize_path_reference(review.evidence_source_ref, run_id=review.provider_review_id)


def _expected_evidence_span_refs(review: NormalizedProviderReview) -> list[str]:
    return [str(ref).strip() for ref in review.evidence_span_refs_used if str(ref).strip()]


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


def _safe_readback_summary(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "topicId": row.get("topic_id"),
        "providerReviewId": row.get("run_id"),
        "paperId": row.get("paper_id"),
        "title": row.get("title"),
        "sourceProvider": row.get("source_provider"),
        "reviewModel": row.get("review_model"),
        "reviewer": row.get("reviewer"),
        "reviewSourceType": row.get("review_source_type"),
        "fullTextStatus": row.get("full_text_status"),
        "fullTextArtifactPath": row.get("full_text_artifact_path"),
        "sourceTextSha256": row.get("source_text_sha256"),
        "textCharCount": row.get("text_char_count"),
        "evidenceSpanRefsUsed": _row_evidence_span_refs(row),
        "proofArtifactRefs": _row_proof_refs(row),
    }


def _provider_review_matches_expected(row: Mapping[str, Any] | None, review: NormalizedProviderReview) -> bool:
    if not row:
        return False
    expected = review.repository_payload
    expected_spans = _expected_evidence_span_refs(review)
    row_spans = _row_evidence_span_refs(row)
    return all(
        [
            row.get("topic_id") == review.topic_id,
            row.get("run_id") == review.provider_review_id,
            row.get("paper_id") == review.paper_id,
            row.get("title") == review.paper_title,
            row.get("source_provider") == PROVIDER_FAMILY_ID,
            row.get("review_model") == f"{PROVIDER_SURFACE_ID}/{PROOF_MODE}",
            row.get("reviewer") == f"provider:{PROVIDER_SURFACE_ID}",
            row.get("review_source_type") == REVIEW_SOURCE,
            row.get("full_text_status") == "reviewed",
            row.get("source_text_sha256") == expected.get("source_text_sha256"),
            row.get("full_text_artifact_path") == _expected_full_text_artifact_path(review),
            int(row.get("text_char_count") or 0) == int(expected.get("text_char_count") or 0),
            row_spans == expected_spans,
            _row_proof_refs(row) == [review.proof_artifact_ref],
        ]
    )


def _temp_db_path_allowed(path: Path) -> bool:
    resolved = path.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    protected_storage = (repo_root / "storage").resolve()
    return protected_storage not in resolved.parents and resolved != protected_storage


def apply_provider_review_to_temp_db(
    review: NormalizedProviderReview,
    *,
    db_path: str | Path,
) -> dict[str, Any]:
    target = Path(db_path)
    if not _temp_db_path_allowed(target):
        raise ProviderReviewIntegrationError(["temp_db_target_must_not_be_repo_storage"])
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(review.repository_payload)
    db_payload = {
        "topic_id": payload["topic_id"],
        "run_id": payload["run_id"],
        "paper_id": payload["paper_id"],
        "title": payload["title"],
        "source_provider": payload.get("source_provider"),
        "authors_json": "[]",
        "published_at": None,
        "abstract_text": payload.get("abstract_text"),
        "text_excerpt": payload.get("text_excerpt"),
        "full_text_status": payload.get("full_text_status"),
        "relevance_score": float(payload.get("relevance_score") or 0.0),
        "novelty_score": float(payload.get("novelty_score") or 0.0),
        "evidence_strength": float(payload.get("evidence_strength") or 0.0),
        "method_summary": payload.get("method_summary"),
        "key_findings_json": _json_list(payload.get("key_findings")),
        "limitations_json": _json_list(payload.get("limitations")),
        "why_it_matters": payload.get("why_it_matters"),
        "topic_fit_rationale": payload.get("topic_fit_rationale"),
        "review_model": payload.get("review_model"),
        "reviewer": payload.get("reviewer"),
        "reviewed_at": payload.get("reviewed_at"),
        "source_text_sha256": payload.get("source_text_sha256"),
        "full_text_artifact_path": payload.get("full_text_artifact_path"),
        "text_char_count": int(payload.get("text_char_count") or 0),
        "evidence_spans_json": _json_payload(payload.get("evidence_spans")),
        "review_source_type": payload.get("review_source_type"),
        "full_text_reviewed_at": payload.get("full_text_reviewed_at"),
    }
    with open_sqlite_read_write(
        target,
        store=STORE_METADATA,
        timeout_seconds=DEFAULT_METADATA_WRITE_TIMEOUT_SECONDS,
    ) as connection:
        if sqlite_user_tables(connection):
            require_store_write_compatible(connection, store=STORE_METADATA)
        connection.executescript(SCHEMA_SQL)
        require_store_write_compatible(connection, store=STORE_METADATA)
        connection.execute(
            """
            INSERT INTO paper_reviews (
              topic_id, run_id, paper_id, title, source_provider, authors_json, published_at,
              abstract_text, text_excerpt, full_text_status, relevance_score, novelty_score,
              evidence_strength, method_summary, key_findings_json, limitations_json,
              why_it_matters, topic_fit_rationale, review_model, reviewer, reviewed_at,
              source_text_sha256, full_text_artifact_path, text_char_count, evidence_spans_json,
              review_source_type, full_text_reviewed_at
            ) VALUES (
              :topic_id, :run_id, :paper_id, :title, :source_provider, :authors_json, :published_at,
              :abstract_text, :text_excerpt, :full_text_status, :relevance_score, :novelty_score,
              :evidence_strength, :method_summary, :key_findings_json, :limitations_json,
              :why_it_matters, :topic_fit_rationale, :review_model, :reviewer, :reviewed_at,
              :source_text_sha256, :full_text_artifact_path, :text_char_count, :evidence_spans_json,
              :review_source_type, :full_text_reviewed_at
            )
            """,
            db_payload,
        )
        row = connection.execute(
            """
            SELECT topic_id, run_id, paper_id, source_provider, review_model, reviewer,
                   source_text_sha256, full_text_artifact_path, evidence_spans_json,
                   review_source_type
            FROM paper_reviews
            WHERE topic_id = ? AND run_id = ? AND paper_id = ?
            """,
            (review.topic_id, review.provider_review_id, review.paper_id),
        ).fetchone()
    return {
        "status": "temp_apply_succeeded",
        "applied": True,
        "tempDbPath": str(target),
        "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
        "row": dict(row) if row else None,
        "reasonCodes": ["temp_db_apply_succeeded", "real_db_apply_blocked_pending_owner_approval"],
    }


def validate_provider_review_real_db_applicability(
    review: NormalizedProviderReview,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root().resolve()
    metadata_path = metadata_index_path(ensure_parent=False)
    with connect_metadata_index(read_only=True) as connection:
        paper_rows = connection.execute(
            """
            SELECT run_id, paper_id, title, theme, status
            FROM papers
            WHERE paper_id = ?
            ORDER BY run_id DESC
            LIMIT 20
            """,
            (review.paper_id,),
        ).fetchall()
        exact_topic_rows = connection.execute(
            "SELECT run_id, topic FROM run_topics WHERE topic = ? OR topic = ? LIMIT 20",
            (review.topic_id, review.topic_name),
        ).fetchall()
        table_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(paper_reviews)").fetchall()
        }
    paper_titles = [str(row["title"] or "") for row in paper_rows]
    paper_mapped = bool(paper_rows)
    if not paper_mapped:
        reasons.append("PROOF_ARTIFACT_NOT_REAL_DB_APPLICABLE")
        reasons.append("paper_id_missing_from_metadata_index")
    elif review.paper_title not in paper_titles:
        reasons.append("paper_title_differs_from_metadata_index")

    required_columns = {"topic_id", "run_id", "paper_id", "evidence_spans_json", "review_source_type"}
    schema_path_available = required_columns.issubset(table_columns)
    if not schema_path_available:
        reasons.append("DB_SCHEMA_MAPPING_MISSING")
    topic_exact = bool(exact_topic_rows)
    topic_mapping = "existing_run_topic" if topic_exact else "paper_reviews_topic_id_schema_path"

    evidence_ok = _evidence_ref_is_project_relative_file(review.evidence_source_ref, repo_root=root)
    if not evidence_ok:
        reasons.append("PROOF_ARTIFACT_NOT_REAL_DB_APPLICABLE")
        reasons.append("evidence_source_ref_not_project_relative_file")

    valid = paper_mapped and schema_path_available and evidence_ok
    return {
        "valid": valid,
        "metadataIndexExists": metadata_path.exists(),
        "paperMappedToMetadataIndex": paper_mapped,
        "paperRowCount": len(paper_rows),
        "topicMapping": topic_mapping,
        "topicExactMatchCount": len(exact_topic_rows),
        "evidenceSourceRef": review.evidence_source_ref,
        "evidenceSourceExists": evidence_ok,
        "schemaPathAvailable": schema_path_available,
        "acceptedExistingSchemaPath": schema_path_available,
        "reasonCodes": _dedupe(reasons)
        if reasons
        else [
            "paper_maps_to_existing_metadata_index",
            "evidence_source_ref_project_relative_file_exists",
            f"topic_mapping:{topic_mapping}",
            "paper_reviews_schema_path_available",
        ],
    }


def read_provider_review_real_db_status(review: NormalizedProviderReview) -> dict[str, Any]:
    existing = paper_reviews.get_review(
        review.topic_id,
        review.provider_review_id,
        review.paper_id,
        read_only=True,
    )
    same_paper_reviews = paper_reviews.list_reviews(
        topic_id=review.topic_id,
        paper_ids=[review.paper_id],
        limit=1000,
        read_only=True,
    )
    conflicting = [
        row
        for row in same_paper_reviews
        if row.get("run_id") != review.provider_review_id
        and (
            row.get("review_source_type") == REVIEW_SOURCE
            or row.get("reviewer") == f"provider:{PROVIDER_SURFACE_ID}"
            or row.get("source_provider") == PROVIDER_FAMILY_ID
        )
    ]
    exact_match = _provider_review_matches_expected(existing, review)
    exact_conflict = existing is not None and not exact_match
    conflict = exact_conflict or bool(conflicting)
    if conflict:
        real_apply_status = REAL_APPLY_CONFLICT_STATUS
        status = "conflict"
        reasons = ["PROVIDER_REVIEW_CONFLICT"]
    elif exact_match:
        real_apply_status = REAL_APPLY_ALREADY_APPLIED_STATUS
        status = "already_applied"
        reasons = ["PROVIDER_REVIEW_ALREADY_APPLIED", "provider_review_readback_verified"]
    else:
        real_apply_status = REAL_APPLY_BLOCKED_STATUS
        status = "missing"
        reasons = ["provider_review_not_yet_applied"]
    return {
        "status": status,
        "realApplyStatus": real_apply_status,
        "providerReviewId": review.provider_review_id,
        "topicId": review.topic_id,
        "paperId": review.paper_id,
        "exactReviewExists": existing is not None,
        "exactReviewMatches": exact_match,
        "conflict": conflict,
        "conflictCount": len(conflicting) + (1 if exact_conflict else 0),
        "row": _safe_readback_summary(existing),
        "reasonCodes": reasons,
    }


def create_provider_review_metadata_backup(*, timestamp: str | None = None) -> dict[str, Any]:
    source = metadata_index_path(ensure_parent=False)
    if not source.is_file():
        raise ProviderReviewIntegrationError(["DB_BACKUP_FAILED", "metadata_index_missing"])
    safe_timestamp = "".join(
        char if char.isalnum() else "-"
        for char in (timestamp or iso_now_utc())
    ).strip("-")
    backup_dir = source.parent / "backups" / f"provider-review-real-apply-{safe_timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    target = backup_dir / "metadata_index-pre-provider-review-apply.sqlite3"
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise ProviderReviewIntegrationError(["DB_BACKUP_FAILED"]) from exc
    return {
        "backupDir": _relative_to_repo(backup_dir),
        "backupPath": _relative_to_repo(target),
        "sourcePath": _relative_to_repo(source),
        "created": True,
    }


def apply_provider_review_to_real_db(
    review: NormalizedProviderReview,
    *,
    repo_root: str | Path | None = None,
    backup_timestamp: str | None = None,
) -> dict[str, Any]:
    applicability = validate_provider_review_real_db_applicability(review, repo_root=repo_root)
    if not applicability["valid"]:
        raise ProviderReviewIntegrationError(list(applicability["reasonCodes"]))

    before_status = read_provider_review_real_db_status(review)
    if before_status["conflict"]:
        raise ProviderReviewIntegrationError(["PROVIDER_REVIEW_CONFLICT"])
    if before_status["exactReviewMatches"]:
        return {
            "status": "already_applied",
            "applied": False,
            "providerReviewId": review.provider_review_id,
            "backup": None,
            "readback": before_status,
            "applicability": applicability,
            "realApplyStatus": REAL_APPLY_ALREADY_APPLIED_STATUS,
            "reasonCodes": ["PROVIDER_REVIEW_ALREADY_APPLIED", "provider_review_readback_verified"],
        }

    backup = create_provider_review_metadata_backup(timestamp=backup_timestamp)
    stored = paper_reviews.upsert_review(dict(review.repository_payload))
    after_status = read_provider_review_real_db_status(review)
    if not after_status["exactReviewMatches"]:
        raise ProviderReviewIntegrationError(["POST_APPLY_READBACK_FAILED"])
    return {
        "status": "real_apply_applied",
        "applied": True,
        "providerReviewId": review.provider_review_id,
        "backup": backup,
        "stored": _safe_readback_summary(stored),
        "readback": after_status,
        "applicability": applicability,
        "realApplyStatus": REAL_APPLY_APPLIED_STATUS,
        "reasonCodes": ["provider_review_real_db_apply_completed", "provider_review_readback_verified"],
    }


def build_provider_review_real_db_report_context(review: NormalizedProviderReview) -> dict[str, Any]:
    status = read_provider_review_real_db_status(review)
    row = status.get("row") if status.get("exactReviewMatches") else None
    provider_review_count = 1 if row else 0
    return {
        "status": "provider_backed_paper_review_real_db_available" if row else "provider_backed_paper_review_real_db_missing",
        "providerReviewCount": provider_review_count,
        "providerReviewId": review.provider_review_id,
        "topicId": review.topic_id,
        "paperIds": [review.paper_id] if row else [],
        "providerFamilyId": review.provider_family_id,
        "providerSurfaceId": review.provider_surface_id,
        "evidenceSpanRefsUsed": list(row.get("evidenceSpanRefsUsed") or []) if row else [],
        "proofArtifactRefs": list(row.get("proofArtifactRefs") or []) if row else [],
        "reportDraftAllowed": False,
        "providerWeeklyReportBlocked": True,
        "reasonCodes": [
            "provider_review_real_db_readback_verified" if row else "provider_review_real_db_missing",
            "weekly_report_output_class_proof_missing",
            "provider_report_generation_not_run",
        ],
    }


def build_provider_review_report_context(record: ProviderPaperReviewProofRecord | NormalizedProviderReview) -> dict[str, Any]:
    normalized = record if isinstance(record, NormalizedProviderReview) else normalize_provider_paper_review_for_storage(record)
    return {
        "status": "provider_backed_paper_review_evidence_available",
        "providerReviewCount": 1,
        "evidenceSpanCount": normalized.evidence_span_count,
        "providerFamilyId": normalized.provider_family_id,
        "providerSurfaceId": normalized.provider_surface_id,
        "paperIds": [normalized.paper_id],
        "proofArtifactRefs": [normalized.proof_artifact_ref],
        "outputClass": normalized.output_class,
        "internalOutputClass": normalized.internal_output_class,
        "reportDraftAllowed": False,
        "providerWeeklyReportBlocked": True,
        "messages": [
            "provider-backed paper review evidence available",
            "provider-backed weekly report still blocked",
        ],
        "reasonCodes": [
            "provider_paper_review_context_ready",
            "weekly_report_output_class_proof_missing",
            "provider_report_generation_not_run",
        ],
    }


def build_provider_review_integration_status(
    path: str | Path,
    *,
    include_real_db_status: bool = True,
) -> dict[str, Any]:
    try:
        record = load_provider_paper_review_output_class_proof(path)
        normalized = normalize_provider_paper_review_for_storage(record)
    except ProviderReviewIntegrationError as exc:
        return {
            "status": "rejected_artifact",
            "normalizedProviderReviewAvailable": False,
            "dryRunApplyPlan": {"status": "rejected_artifact", "applied": False},
            "reportContext": {"status": "unavailable"},
            "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
            "reasonCodes": exc.reason_codes,
        }
    plan = plan_provider_review_db_apply(normalized)
    context = build_provider_review_report_context(normalized)
    try:
        if include_real_db_status:
            real_status = read_provider_review_real_db_status(normalized)
        else:
            real_status = {
                "status": "not_checked",
                "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
                "reasonCodes": ["provider_review_real_db_status_not_checked"],
            }
    except (FileNotFoundError, sqlite3.DatabaseError, ProviderReviewIntegrationError):
        real_status = {
            "status": "unknown",
            "realApplyStatus": REAL_APPLY_BLOCKED_STATUS,
            "reasonCodes": ["provider_review_real_db_status_unavailable"],
        }
    real_apply_status = str(real_status.get("realApplyStatus") or REAL_APPLY_BLOCKED_STATUS)
    status = REAL_APPLY_APPLIED_STATUS if real_apply_status in {REAL_APPLY_APPLIED_STATUS, REAL_APPLY_ALREADY_APPLIED_STATUS} else plan["status"]
    return {
        "status": status,
        "normalizedProviderReviewAvailable": True,
        "providerReviewId": normalized.provider_review_id,
        "topicId": normalized.topic_id,
        "paperId": normalized.paper_id,
        "proofArtifactRef": normalized.proof_artifact_ref,
        "reviewSource": normalized.review_source,
        "dryRunApplyPlan": plan,
        "reportContext": context,
        "realDbStatus": real_status,
        "realApplyStatus": real_apply_status,
        "researchOutputAllowed": True,
        "reportDraftAllowed": False,
        "providerAuthIsAppOauthOidc": False,
        "localMcpIsPublicConnector": False,
        "productionReady": False,
        "externalBetaReady": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "reasonCodes": [
            "provider_review_integration_dry_run_ready",
            "provider_review_real_db_applied" if status == REAL_APPLY_APPLIED_STATUS else "real_db_apply_blocked_pending_owner_approval",
            "weekly_report_output_class_proof_missing",
            *list(real_status.get("reasonCodes") or []),
        ],
    }

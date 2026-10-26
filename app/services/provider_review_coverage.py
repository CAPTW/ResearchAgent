from __future__ import annotations

import re
from typing import Any

from app.repositories import ai_review_drafts, paper_reviews

ORIGIN_BUCKETS = (
    "deterministic",
    "manual_import",
    "stub_synthetic",
    "command_fixture_synthetic",
    "command_provider_operator_accepted",
    "unknown",
)


def empty_origin_counts() -> dict[str, int]:
    return {bucket: 0 for bucket in ORIGIN_BUCKETS}


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def draft_key(value: dict[str, Any]) -> str:
    return f"{value.get('paper_id') or ''}::{value.get('source_text_sha256') or ''}"


def provider_metadata(draft: dict[str, Any] | None) -> dict[str, Any]:
    if not draft:
        return {}
    validation = draft.get("validation") if isinstance(draft.get("validation"), dict) else {}
    return validation.get("provider") if isinstance(validation.get("provider"), dict) else {}


def origin_bucket(draft_or_provider: dict[str, Any] | None) -> str:
    provider = draft_or_provider or {}
    if "validation" in provider or "draft" in provider:
        provider = provider_metadata(provider)
    origin = str(provider.get("origin") or "").strip()
    name = str(provider.get("name") or "").strip()
    mode = str(provider.get("mode") or "").strip()
    profile_role = str(provider.get("profile_role") or provider.get("provider_profile_role") or "").strip()

    if origin == "stub" or name == "stub" or mode == "stub":
        return "stub_synthetic"
    if origin == "command_fixture" or profile_role == "fixture" or mode in {"fixture", "fixture_command"}:
        return "command_fixture_synthetic"
    if origin in {"real_provider", "live_command"} or mode == "live_command":
        if bool(provider.get("operator_accepted")):
            return "command_provider_operator_accepted"
        return "unknown"
    if origin in {"manual_import", "manual_command_import"} or name == "manual_import" or mode == "manual_import" or not provider:
        return "manual_import"
    if origin == "deterministic":
        return "deterministic"
    return "unknown"


def _is_provider_orchestrated(draft: dict[str, Any]) -> bool:
    provider = provider_metadata(draft)
    if not provider:
        return False
    bucket = origin_bucket(provider)
    return bucket not in {"deterministic", "manual_import"}


def _is_m4_validated_accepted_draft(draft: dict[str, Any]) -> bool:
    validation = draft.get("validation") if isinstance(draft.get("validation"), dict) else {}
    body = draft.get("draft") if isinstance(draft.get("draft"), dict) else {}
    cited_ids = body.get("cited_evidence_span_ids") if isinstance(body.get("cited_evidence_span_ids"), list) else []
    return (
        draft.get("status") == "accepted"
        and validation.get("status") == "accepted"
        and draft.get("review_level") == "full_text_grounded"
        and body.get("review_level") == "full_text_grounded"
        and bool(str(draft.get("source_text_sha256") or "").strip())
        and body.get("source_text_sha256") == draft.get("source_text_sha256")
        and bool(cited_ids)
    )


def _reviewed_keys(reviews: list[dict[str, Any]]) -> set[str]:
    return {
        draft_key(review)
        for review in reviews
        if str(review.get("full_text_status") or "") == "reviewed" and str(review.get("source_text_sha256") or "").strip()
    }


def accepted_drafts_by_review_key(
    topic_id: str,
    *,
    reviews: list[dict[str, Any]] | None = None,
    all_drafts: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    selected_reviews = reviews if reviews is not None else paper_reviews.list_reviews(topic_id=topic_id, limit=100_000)
    reviewed_keys = _reviewed_keys(selected_reviews)
    selected: dict[str, dict[str, Any]] = {}
    drafts = all_drafts if all_drafts is not None else ai_review_drafts.list_drafts(topic_id=topic_id, status="accepted", limit=100_000)
    for draft in drafts:
        if draft.get("status") != "accepted":
            continue
        key = draft_key(draft)
        if key in reviewed_keys and key not in selected:
            selected[key] = draft
    return selected


def build_provider_review_coverage_summary(
    topic_id: str,
    *,
    full_text_reviewed_denominator: int | None = None,
    reviews: list[dict[str, Any]] | None = None,
    all_drafts: list[dict[str, Any]] | None = None,
    accepted_by_review_key: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_reviews = reviews if reviews is not None else paper_reviews.list_reviews(topic_id=topic_id, limit=100_000)
    drafts = all_drafts if all_drafts is not None else ai_review_drafts.list_drafts(topic_id=topic_id, limit=100_000)
    accepted = accepted_by_review_key or accepted_drafts_by_review_key(topic_id, reviews=selected_reviews, all_drafts=drafts)
    accepted_by_origin = empty_origin_counts()
    needs_review_by_origin = empty_origin_counts()
    rejected_by_origin = empty_origin_counts()

    for draft in accepted.values():
        accepted_by_origin[origin_bucket(draft)] += 1
    for draft in drafts:
        status = str(draft.get("status") or "")
        if status == "needs_review":
            needs_review_by_origin[origin_bucket(draft)] += 1
        elif status == "rejected":
            rejected_by_origin[origin_bucket(draft)] += 1

    reviewed_denominator = int(full_text_reviewed_denominator) if full_text_reviewed_denominator is not None else len(_reviewed_keys(selected_reviews))
    reviewed_denominator = max(0, reviewed_denominator)
    fixture_synthetic_count = accepted_by_origin["stub_synthetic"] + accepted_by_origin["command_fixture_synthetic"]
    real_provider_count = accepted_by_origin["command_provider_operator_accepted"]
    provider_orchestrated_count = sum(1 for draft in accepted.values() if _is_provider_orchestrated(draft))
    reason_codes: list[str] = []
    warnings: list[str] = []
    coverage_warning = ""
    if fixture_synthetic_count:
        reason_codes.append("synthetic_provider_drafts_present")
        coverage_warning = "Synthetic provider drafts are test artifacts and do not imply real external AI use."
        warnings.append(coverage_warning)
    if accepted:
        reason_codes.append("accepted_provider_drafts_require_m4_validation")
        warnings.append("Accepted provider drafts still require M4 validation before reports or recommendations use them.")
    if provider_orchestrated_count and real_provider_count == 0:
        reason_codes.append("real_provider_accepted_count_zero")

    return {
        "acceptedAiGroundedDraftCount": len(accepted),
        "acceptedByOrigin": accepted_by_origin,
        "needsReviewByOrigin": needs_review_by_origin,
        "rejectedByOrigin": rejected_by_origin,
        "fixtureSyntheticCount": fixture_synthetic_count,
        "realProviderAcceptedCount": real_provider_count,
        "providerOrchestratedAcceptedCount": provider_orchestrated_count,
        "providerOrchestratedSyntheticCount": fixture_synthetic_count,
        "fullTextReviewedDenominator": reviewed_denominator,
        "coverageWarning": coverage_warning,
        "reasonCodes": _unique(reason_codes),
        "warnings": _unique(warnings),
    }


def _recommendation_evidence_refs(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    refs: list[dict[str, Any]] = []
    for section in ("gap_candidates", "new_topic_candidates"):
        candidates = payload.get(section)
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for ref_field in ("supporting_evidence_refs", "limitation_refs"):
                candidate_refs = candidate.get(ref_field)
                if not isinstance(candidate_refs, list):
                    continue
                refs.extend(ref for ref in candidate_refs if isinstance(ref, dict))
    return refs


def _accepted_ref_supported(ref: dict[str, Any], accepted: dict[str, dict[str, Any]]) -> bool:
    paper_id = str(ref.get("paper_id") or "").strip()
    source_hash = str(ref.get("source_text_sha256") or "").strip()
    if not paper_id:
        return True
    for draft in accepted.values():
        if str(draft.get("paper_id") or "") != paper_id:
            continue
        if source_hash and str(draft.get("source_text_sha256") or "") != source_hash:
            continue
        return True
    return False


def _accepted_ref_has_nonaccepted_draft(ref: dict[str, Any], drafts: list[dict[str, Any]]) -> bool:
    paper_id = str(ref.get("paper_id") or "").strip()
    source_hash = str(ref.get("source_text_sha256") or "").strip()
    if not paper_id:
        return False
    for draft in drafts:
        if str(draft.get("paper_id") or "") != paper_id:
            continue
        if source_hash and str(draft.get("source_text_sha256") or "") != source_hash:
            continue
        if str(draft.get("status") or "") in {"needs_review", "rejected"}:
            return True
    return False


def _dashboard_mismatch_count(source: dict[str, Any], dashboard_summary: dict[str, Any] | None) -> int:
    if not isinstance(dashboard_summary, dict):
        return 0
    compared_fields = (
        "acceptedAiGroundedDraftCount",
        "fixtureSyntheticCount",
        "realProviderAcceptedCount",
        "providerOrchestratedAcceptedCount",
        "providerOrchestratedSyntheticCount",
        "fullTextReviewedDenominator",
        "acceptedByOrigin",
        "needsReviewByOrigin",
        "rejectedByOrigin",
    )
    return sum(1 for field in compared_fields if dashboard_summary.get(field) != source.get(field))


def _report_mismatch_count(source: dict[str, Any], report_markdown: str | None) -> int:
    if not isinstance(report_markdown, str) or not report_markdown.strip():
        return 0
    expected_patterns = (
        (r"Provider-orchestrated accepted draft coverage is\s+(\d+)\s*/", "providerOrchestratedAcceptedCount"),
        (r"Synthetic provider accepted drafts:\s+(\d+)", "fixtureSyntheticCount"),
        (r"Operator-accepted real provider accepted drafts:\s+(\d+)", "realProviderAcceptedCount"),
    )
    mismatch_count = 0
    for pattern, field in expected_patterns:
        match = re.search(pattern, report_markdown)
        if match and int(match.group(1)) != int(source.get(field) or 0):
            mismatch_count += 1
    return mismatch_count


def build_provider_provenance_audit(
    topic_id: str,
    *,
    dashboard_summary: dict[str, Any] | None = None,
    recommendation_payload: dict[str, Any] | None = None,
    report_markdown: str | None = None,
    reviews: list[dict[str, Any]] | None = None,
    all_drafts: list[dict[str, Any]] | None = None,
    coverage_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_reviews = reviews if reviews is not None else paper_reviews.list_reviews(topic_id=topic_id, limit=100_000)
    drafts = all_drafts if all_drafts is not None else ai_review_drafts.list_drafts(topic_id=topic_id, limit=100_000)
    accepted = accepted_drafts_by_review_key(topic_id, reviews=selected_reviews, all_drafts=drafts)
    source_summary = coverage_summary or build_provider_review_coverage_summary(
        topic_id,
        reviews=selected_reviews,
        all_drafts=drafts,
        accepted_by_review_key=accepted,
    )

    dashboard_mismatch_count = _dashboard_mismatch_count(source_summary, dashboard_summary)
    report_mismatch_count = _report_mismatch_count(source_summary, report_markdown)
    mismatch_count = dashboard_mismatch_count + report_mismatch_count
    unknown_origin_count = sum(1 for draft in accepted.values() if origin_bucket(draft) == "unknown")
    missing_m4_validation_count = sum(1 for draft in accepted.values() if not _is_m4_validated_accepted_draft(draft))
    leak_count = 0
    for ref in _recommendation_evidence_refs(recommendation_payload):
        if ref.get("review_level") not in {"accepted_ai_grounded", "accepted_ai_grounded_synthetic"}:
            continue
        if _accepted_ref_supported(ref, accepted):
            continue
        if _accepted_ref_has_nonaccepted_draft(ref, drafts):
            leak_count += 1
        else:
            leak_count += 1

    synthetic_count = int(source_summary.get("fixtureSyntheticCount") or 0)
    real_provider_count = int(source_summary.get("realProviderAcceptedCount") or 0)
    reason_codes: list[str] = []
    warnings: list[str] = []
    if dashboard_mismatch_count:
        reason_codes.append("dashboard_provider_coverage_mismatch")
        warnings.append("Dashboard provider coverage fields differ from repository-derived provenance counts.")
    if report_mismatch_count:
        reason_codes.append("report_provider_coverage_mismatch")
        warnings.append("Report provider coverage wording differs from repository-derived provenance counts.")
    if unknown_origin_count:
        reason_codes.append("unknown_provider_origin")
        warnings.append("Accepted provider drafts include unknown origin metadata that requires operator review.")
    if leak_count:
        reason_codes.append("rejected_or_needs_review_recommendation_leak")
        warnings.append("Recommendation evidence references accepted AI-grounded support that is not backed by an accepted draft.")
    if missing_m4_validation_count:
        reason_codes.append("accepted_provider_draft_missing_m4_validation")
        warnings.append("Accepted provider drafts must retain M4 grounded validation metadata.")
    if synthetic_count:
        reason_codes.append("synthetic_provider_evidence_guardrail_active")
        warnings.append("Synthetic fixture/stub provider evidence remains local test evidence and cannot stand in for real provider output.")

    failed_count = mismatch_count + leak_count + missing_m4_validation_count
    if failed_count:
        audit_status = "failed"
    elif unknown_origin_count:
        audit_status = "warnings"
    else:
        audit_status = "valid"
        reason_codes.append("provider_provenance_consistent")

    return {
        "auditStatus": audit_status,
        "checkedCount": len(accepted),
        "mismatchCount": mismatch_count,
        "syntheticCount": synthetic_count,
        "realProviderCount": real_provider_count,
        "unknownOriginCount": unknown_origin_count,
        "rejectedOrNeedsReviewLeakCount": leak_count,
        "missingM4ValidationCount": missing_m4_validation_count,
        "reasonCodes": _unique(reason_codes),
        "warnings": _unique(warnings),
    }

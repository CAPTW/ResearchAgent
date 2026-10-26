from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews
from app.schemas.recommendations import (
    NewTopicCandidate,
    RecommendationEvidenceRef,
    RecommendationFollowupExecutionActionItem,
    RecommendationFollowupExecutionSummary,
    RecommendationFollowupCandidate,
    RecommendationFollowupPlanItem,
    RecommendationFollowupPlanSummary,
    RecommendationQualityComparison,
    RecommendationQualityAuditItem,
    RecommendationQualityAuditSummary,
    RecommendationRunSummary,
    ResearchGapCandidate,
)
from app.services import paper_review, pdf_acquisition, provider_review_coverage, topic_profiles

THEMES: dict[str, tuple[str, ...]] = {
    "rag-evaluation": ("rag", "retrieval augmented generation", "retrieval", "evaluation", "benchmark"),
    "agent-tool-use": ("agent", "tool use", "tool", "orchestration"),
    "alignment-risk": ("alignment", "safety", "risk", "limitation"),
    "reasoning-assessment": ("reasoning", "assessment", "evaluation", "benchmark"),
    "data-coverage": ("dataset", "data", "coverage", "domain"),
    "multimodal-llm": ("multimodal", "vision", "image", "video"),
}

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
IMPACT_ORDER = {"low": 0, "medium": 1, "high": 2}
STOPWORDS = {
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "how",
    "into",
    "not",
    "the",
    "this",
    "that",
    "with",
    "without",
    "across",
    "current",
    "evidence",
    "paper",
    "papers",
    "research",
    "topic",
}
H006_CATEGORY_MAX_SCORE = 2
H006_CATEGORIES = [
    "Provenance completeness",
    "Evidence support",
    "Speculation honesty",
    "Gap rationale quality",
    "Actionability",
    "Citation and paper traceability",
    "Coverage awareness",
    "Diversity and non-duplication",
    "Provider truthfulness",
    "Safety of downstream use",
]
H006_READY_SCORE = H006_CATEGORY_MAX_SCORE * len(H006_CATEGORIES)
H006_WARNING_SCORE = 16
H006_STRONG_REVIEW_LEVELS = {
    "full_text_reviewed",
    "deterministic_full_text",
    "accepted_ai_grounded",
    "accepted_ai_grounded_synthetic",
}
H006_PROVIDER_OVERCLAIM_PATTERNS = [
    r"\b(gemini|codex|claude|antigravity)\s+generated\b",
    r"\b(provider|ai)\s+generated\s+(recommendation|gap|topic|research agenda)\b",
    r"\blive provider verified\b",
    r"\bai discovered\b",
]
H006_NOVELTY_OVERCLAIM_PATTERNS = [
    r"\bnovel breakthrough\b",
    r"\bunexplored by all prior work\b",
    r"\bnever studied\b",
    r"\bfirst ever\b",
    r"\bcompletely new\b",
]
H006_VERIFIED_FACT_PATTERNS = [
    r"\bconfirmed gap\b",
    r"\bverified gap\b",
    r"\bproves\b",
    r"\bguaranteed\b",
]
H006_FULL_TEXT_OVERCLAIM_PATTERNS = [
    r"\bfull[- ]text supported\b",
    r"\bread in full\b",
    r"\bfull[- ]text reviewed evidence\b",
    r"전문\s*(검토|근거|읽)",
]
H006_CAUTION_MARKERS = [
    "preliminary",
    "hypothesis",
    "candidate",
    "follow-up",
    "requires",
    "requiring",
    "not exhaustive",
    "not a comprehensive",
    "manual validation",
    "future monitoring",
    "cautious",
    "proposal",
    "direction",
    "검증",
    "후속",
    "가설",
    "후보",
    "예비",
]
H006_ACTION_MARKERS = [
    "follow-up",
    "compare",
    "evaluate",
    "validate",
    "monitor",
    "increase",
    "inspect",
    "method",
    "research question",
    "required_follow_up_evidence",
]
H006_HIGH_STAKES_PATTERNS = [
    r"\bclinical\b",
    r"\bpatient\b",
    r"\bmedical\b",
    r"\btherapy\b",
    r"\btreatment\b",
    r"\blegal\b",
    r"\bfinancial\b",
    r"\binvestment\b",
    r"환자",
    r"의료",
    r"치료",
    r"법률",
    r"투자",
]
H006_SECRET_PATTERNS = [
    r"\bapi[_-]?key\b",
    r"\btoken\b",
    r"\bsecret\b",
    r"\bcredential\b",
]


class RecommendationReadOnlySafetyError(RuntimeError):
    def __init__(self, message: str, *, sidecars: dict[str, dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.sidecars = sidecars or {}


def _assert_recommendation_read_safe(*, read_only: bool) -> dict[str, dict[str, object]]:
    sidecars = bootstrap.metadata_index_sidecar_status()
    active = {name: status for name, status in sidecars.items() if status.get("exists")}
    if read_only and active:
        raise RecommendationReadOnlySafetyError(
            "recommendation dry-run/audit refused because metadata index sidecar files are present",
            sidecars=sidecars,
        )
    return sidecars


def _review_identity(review: dict[str, Any]) -> str:
    return str(review.get("paper_id") or "").strip().lower()


def _review_rank_key(review: dict[str, Any]) -> tuple[float, float, str, str]:
    return (
        float(review.get("relevance_score") or 0),
        float(review.get("evidence_strength") or 0),
        str(review.get("published_at") or ""),
        str(review.get("reviewed_at") or ""),
    )


def _dedupe_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for review in reviews:
        key = _review_identity(review)
        if not key:
            continue
        existing = selected.get(key)
        if existing is None or _review_rank_key(review) > _review_rank_key(existing):
            selected[key] = review
    return list(selected.values())


def _review_text(review: dict[str, Any], ai_draft: dict[str, Any] | None = None) -> str:
    pieces: list[str] = [
        str(review.get("title") or ""),
        str(review.get("abstract_text") or ""),
        str(review.get("method_summary") or ""),
        str(review.get("why_it_matters") or ""),
        str(review.get("topic_fit_rationale") or ""),
        " ".join(str(value) for value in review.get("key_findings") or []),
        " ".join(str(value) for value in review.get("limitations") or []),
    ]
    if ai_draft:
        draft = ai_draft.get("draft") if isinstance(ai_draft.get("draft"), dict) else {}
        pieces.extend(
            [
                str(draft.get("contribution_summary") or ""),
                str(draft.get("method_summary") or ""),
                str(draft.get("evidence_summary") or ""),
                str(draft.get("relevance_to_topic") or ""),
                str(draft.get("practical_implications") or ""),
                " ".join(str(value) for value in draft.get("limitations") or []),
                " ".join(str(value) for value in draft.get("follow_up_questions") or []),
                " ".join(str(value) for value in draft.get("recommended_tags") or []),
            ]
        )
    return " ".join(pieces).lower()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "candidate"


def _bounded_basis(value: str, *, max_chars: int = 420) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _accepted_ai_drafts_for_reviews(
    topic_id: str,
    reviews: list[dict[str, Any]],
    *,
    read_only: bool = True,
) -> dict[str, dict[str, Any]]:
    paper_ids = [str(review.get("paper_id") or "") for review in reviews if str(review.get("paper_id") or "")]
    if not paper_ids:
        return {}
    selected: dict[str, dict[str, Any]] = {}
    for draft in ai_review_drafts.list_drafts(
        topic_id=topic_id,
        paper_ids=paper_ids,
        status="accepted",
        limit=1000,
        read_only=read_only,
    ):
        key = f"{draft.get('paper_id') or ''}::{draft.get('source_text_sha256') or ''}"
        if key.endswith("::") or key in selected:
            continue
        selected[key] = draft
    return selected


def _draft_for_review(review: dict[str, Any], ai_drafts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if str(review.get("full_text_status") or "") != "reviewed":
        return None
    return ai_drafts.get(f"{review.get('paper_id') or ''}::{review.get('source_text_sha256') or ''}")


def _provider_origin(ai_draft: dict[str, Any] | None) -> str | None:
    if not ai_draft:
        return None
    return provider_review_coverage.origin_bucket(ai_draft)


def _is_synthetic_ai_draft(ai_draft: dict[str, Any] | None) -> bool:
    return _provider_origin(ai_draft) in {"stub_synthetic", "command_fixture_synthetic"}


def _first_span(review: dict[str, Any]) -> dict[str, Any] | None:
    spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
    for span in spans:
        if isinstance(span, dict) and str(span.get("text") or "").strip():
            return span
    return None


def _span_id(span: dict[str, Any] | None) -> str | None:
    if not span:
        return None
    for key in ("span_id", "id", "label"):
        value = str(span.get(key) or "").strip()
        if value:
            return value
    return None


def _evidence_ref(review: dict[str, Any], ai_draft: dict[str, Any] | None, *, include_abstract_only: bool) -> RecommendationEvidenceRef | None:
    paper_id = str(review.get("paper_id") or "").strip()
    status = str(review.get("full_text_status") or "unavailable")
    source_hash = str(review.get("source_text_sha256") or "").strip() or None
    if ai_draft:
        draft = ai_draft.get("draft") if isinstance(ai_draft.get("draft"), dict) else {}
        cited_ids = [str(value) for value in draft.get("cited_evidence_span_ids") or [] if str(value).strip()]
        return RecommendationEvidenceRef(
            paper_id=paper_id,
            source_text_sha256=source_hash,
            evidence_span_id=cited_ids[0] if cited_ids else _span_id(_first_span(review)),
            review_level="accepted_ai_grounded_synthetic" if _is_synthetic_ai_draft(ai_draft) else "accepted_ai_grounded",
            provider_origin=_provider_origin(ai_draft),
            quoted_or_summarized_basis=_bounded_basis(
                str(draft.get("evidence_summary") or draft.get("contribution_summary") or review.get("text_excerpt") or review.get("abstract_text") or "")
            ),
        )
    if status == "reviewed":
        span = _first_span(review)
        return RecommendationEvidenceRef(
            paper_id=paper_id,
            source_text_sha256=source_hash,
            evidence_span_id=_span_id(span),
            review_level="deterministic_full_text",
            quoted_or_summarized_basis=_bounded_basis(str((span or {}).get("text") or review.get("text_excerpt") or "")),
        )
    if status == "abstract_only" and include_abstract_only:
        return RecommendationEvidenceRef(
            paper_id=paper_id,
            review_level="abstract_only",
            quoted_or_summarized_basis=_bounded_basis(str(review.get("abstract_text") or review.get("title") or "")),
        )
    return None


def _coverage_warning(full_text_reviewed_count: int, topic_relevant_unique_count: int, ai_grounded_count: int) -> str | None:
    denominator = int(topic_relevant_unique_count or 0)
    if denominator <= 0:
        return "preliminary: topic-relevant denominator is unavailable, so recommendation coverage is uncertain."
    ratio = full_text_reviewed_count / denominator
    if ratio < 0.15:
        return (
            f"preliminary: full-text reviewed coverage is {full_text_reviewed_count} / "
            f"{denominator} topic-relevant unique papers; recommendations are not exhaustive."
        )
    if full_text_reviewed_count and ai_grounded_count / full_text_reviewed_count < 0.5:
        return (
            f"preliminary: AI-grounded accepted draft coverage is {ai_grounded_count} / "
            f"{full_text_reviewed_count} full-text reviewed papers."
        )
    return None


def _themes_for_review(review: dict[str, Any], ai_draft: dict[str, Any] | None) -> set[str]:
    text = _review_text(review, ai_draft)
    found = {
        theme
        for theme, terms in THEMES.items()
        if any(term in text for term in terms)
    }
    return found or {"general-llm-monitoring"}


def _confidence(
    *,
    refs: list[RecommendationEvidenceRef],
    supporting_count: int,
    coverage_warning: str | None,
) -> tuple[str, float]:
    strong_levels = {"full_text_reviewed", "deterministic_full_text", "accepted_ai_grounded", "accepted_ai_grounded_synthetic"}
    strong_count = sum(1 for ref in refs if ref.review_level in strong_levels)
    ai_count = sum(1 for ref in refs if ref.review_level == "accepted_ai_grounded")
    synthetic_ai_count = sum(1 for ref in refs if ref.review_level == "accepted_ai_grounded_synthetic")
    abstract_count = sum(1 for ref in refs if ref.review_level == "abstract_only")
    score = 0.2 + min(0.25, supporting_count * 0.08) + min(0.25, strong_count * 0.09) + min(0.2, ai_count * 0.12)
    if synthetic_ai_count and not ai_count:
        score = min(score, 0.69)
    if abstract_count and not strong_count:
        score = min(score, 0.39)
    if coverage_warning:
        score = min(score, 0.69)
    score = round(min(score, 0.95), 2)
    if score >= 0.75 and strong_count >= 2 and ai_count >= 1 and not coverage_warning:
        return "high", score
    if score >= 0.45 and strong_count >= 1:
        return "medium", score
    return "low", min(score, 0.44)


def _source_basis(refs: list[RecommendationEvidenceRef]) -> str:
    levels = {ref.review_level for ref in refs}
    if "accepted_ai_grounded" in levels and len(levels) == 1:
        return "accepted_ai_grounded"
    if "accepted_ai_grounded" in levels:
        return "mixed"
    if "accepted_ai_grounded_synthetic" in levels and len(levels) == 1:
        return "accepted_ai_grounded_synthetic"
    if "accepted_ai_grounded_synthetic" in levels:
        return "mixed"
    if levels.intersection({"full_text_reviewed", "deterministic_full_text"}):
        return "full_text_evidence"
    return "deterministic_review"


def _topic_relevant_denominator(topic_profile: dict[str, Any], fallback: int, *, read_only: bool = True) -> int:
    try:
        coverage = paper_review.review_coverage_for_rows(
            topic_profile,
            paper_review._topic_rows(topic_profile),
            read_only=read_only,
        )
        return int(coverage.get("topicRelevantUniqueCount") or fallback)
    except Exception:
        return fallback


def _summary(topic_id: str, gaps: list[ResearchGapCandidate], topics: list[NewTopicCandidate], refs: list[RecommendationEvidenceRef], *, full_text_reviewed_count: int, topic_relevant_unique_count: int, ai_grounded_count: int, exported_count: int = 0) -> dict[str, Any]:
    confidence_counts = Counter([candidate.confidence for candidate in [*gaps, *topics]])
    unique_refs = {
        (ref.paper_id, ref.review_level, ref.source_text_sha256 or "", ref.evidence_span_id or ""): ref
        for ref in refs
    }
    evidence_refs = list(unique_refs.values())
    evidence_papers = {ref.paper_id for ref in evidence_refs}
    return RecommendationRunSummary(
        topic_id=topic_id,
        candidate_gap_count=len(gaps),
        candidate_topic_count=len(topics),
        exported_count=exported_count,
        low_confidence_count=confidence_counts.get("low", 0),
        medium_confidence_count=confidence_counts.get("medium", 0),
        high_confidence_count=confidence_counts.get("high", 0),
        evidence_paper_count=len(evidence_papers),
        full_text_evidence_count=sum(
            1
            for ref in evidence_refs
            if ref.review_level in {"full_text_reviewed", "deterministic_full_text", "accepted_ai_grounded", "accepted_ai_grounded_synthetic"}
        ),
        accepted_ai_grounded_evidence_count=sum(1 for ref in evidence_refs if ref.review_level == "accepted_ai_grounded"),
        accepted_ai_grounded_synthetic_evidence_count=sum(1 for ref in evidence_refs if ref.review_level == "accepted_ai_grounded_synthetic"),
        abstract_only_evidence_count=sum(1 for ref in evidence_refs if ref.review_level == "abstract_only"),
        coverage_warning_count=sum(1 for candidate in [*gaps, *topics] if candidate.coverage_warning),
        full_text_reviewed_count=full_text_reviewed_count,
        topic_relevant_unique_count=topic_relevant_unique_count,
        ai_grounded_accepted_count=ai_grounded_count,
    ).model_dump()


def _recommendation_items(payload: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    items: list[tuple[str, str, dict[str, Any]]] = []
    for gap in payload.get("gap_candidates") or []:
        if isinstance(gap, dict):
            recommendation_id = str(gap.get("gap_id") or "").strip()
            if recommendation_id:
                items.append((recommendation_id, "gap", gap))
    for topic in payload.get("new_topic_candidates") or []:
        if isinstance(topic, dict):
            recommendation_id = str(topic.get("topic_candidate_id") or "").strip()
            if recommendation_id:
                items.append((recommendation_id, "new_topic", topic))
    return items


def _confidence_reasons(
    *,
    supporting_paper_count: int,
    full_text_count: int,
    ai_count: int,
    abstract_count: int,
    coverage_warning: str | None,
) -> list[str]:
    reasons: list[str] = []
    if supporting_paper_count >= 2:
        reasons.append("multiple_supporting_papers")
    elif supporting_paper_count == 1:
        reasons.append("single_supporting_paper")
    if full_text_count:
        reasons.append("full_text_support")
    if ai_count:
        reasons.append("accepted_ai_grounded_support")
    if abstract_count:
        reasons.append("abstract_only_support_present")
    if coverage_warning:
        reasons.append("low_coverage_disclosed")
    return reasons


def audit_recommendation_quality(payload: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    confidence_counts: Counter[str] = Counter()
    for recommendation_id, recommendation_type, candidate in _recommendation_items(payload):
        refs = candidate.get("supporting_evidence_refs") if isinstance(candidate.get("supporting_evidence_refs"), list) else []
        supporting_ids = {str(value) for value in candidate.get("supporting_paper_ids") or [] if str(value).strip()}
        for ref in refs:
            if isinstance(ref, dict) and str(ref.get("paper_id") or "").strip():
                supporting_ids.add(str(ref.get("paper_id")))

        full_text_count = sum(
            1
            for ref in refs
            if isinstance(ref, dict) and ref.get("review_level") in {"full_text_reviewed", "deterministic_full_text"}
        )
        ai_count = sum(1 for ref in refs if isinstance(ref, dict) and ref.get("review_level") == "accepted_ai_grounded")
        synthetic_ai_count = sum(1 for ref in refs if isinstance(ref, dict) and ref.get("review_level") == "accepted_ai_grounded_synthetic")
        abstract_count = sum(1 for ref in refs if isinstance(ref, dict) and ref.get("review_level") == "abstract_only")
        missing_span_count = sum(
            1
            for ref in refs
            if isinstance(ref, dict)
            and ref.get("review_level") in {"full_text_reviewed", "deterministic_full_text", "accepted_ai_grounded", "accepted_ai_grounded_synthetic"}
            and not str(ref.get("evidence_span_id") or "").strip()
        )
        missing_hash_count = sum(
            1
            for ref in refs
            if isinstance(ref, dict)
            and ref.get("review_level") in {"full_text_reviewed", "deterministic_full_text", "accepted_ai_grounded", "accepted_ai_grounded_synthetic"}
            and not str(ref.get("source_text_sha256") or "").strip()
        )
        confidence = str(candidate.get("confidence") or "low")
        if confidence not in CONFIDENCE_ORDER:
            confidence = "low"
        confidence_counts[confidence] += 1
        coverage_warning = str(candidate.get("coverage_warning") or payload.get("coverage_warning") or "").strip() or None
        downgrade_reasons: list[str] = []
        weak_flags: list[str] = []
        if abstract_count:
            downgrade_reasons.append("abstract_only_support")
            weak_flags.append("abstract_only_support")
        if missing_span_count:
            downgrade_reasons.append("missing_evidence_span")
            weak_flags.append("missing_evidence_span")
        if missing_hash_count:
            downgrade_reasons.append("missing_source_hash")
            weak_flags.append("missing_source_hash")
        if coverage_warning:
            downgrade_reasons.append("low_coverage")
            weak_flags.append("low_coverage")
        if len(supporting_ids) < 2:
            downgrade_reasons.append("single_supporting_paper")
        if synthetic_ai_count and ai_count == 0:
            downgrade_reasons.append("synthetic_ai_grounded_support_only")
        if full_text_count + ai_count + synthetic_ai_count == 0:
            downgrade_reasons.append("no_strong_evidence")
            weak_flags.append("no_strong_evidence")
        elif ai_count == 0 and synthetic_ai_count == 0:
            downgrade_reasons.append("no_ai_grounded_support")

        needs_follow_up = bool(downgrade_reasons or confidence != "high")
        item = RecommendationQualityAuditItem(
            recommendation_id=recommendation_id,
            recommendation_type=recommendation_type,  # type: ignore[arg-type]
            evidence_ref_count=len(refs),
            supporting_paper_count=len(supporting_ids),
            full_text_support_count=full_text_count,
            ai_grounded_support_count=ai_count + synthetic_ai_count,
            abstract_only_support_count=abstract_count,
            missing_evidence_span_count=missing_span_count,
            missing_source_hash_count=missing_hash_count,
            confidence=confidence,  # type: ignore[arg-type]
            confidence_score=float(candidate.get("confidence_score") or 0),
            confidence_reason_codes=_confidence_reasons(
                supporting_paper_count=len(supporting_ids),
                full_text_count=full_text_count,
                ai_count=ai_count + synthetic_ai_count,
                abstract_count=abstract_count,
                coverage_warning=coverage_warning,
            ),
            downgrade_reason_codes=sorted(set(downgrade_reasons)),
            coverage_warning=coverage_warning,
            unsupported_or_weak_claim_flags=sorted(set(weak_flags)),
            needs_follow_up=needs_follow_up,
        ).model_dump()
        items.append(item)

    recommendation_count = len(items)
    support_total = sum(int(item["supporting_paper_count"]) for item in items)
    summary = RecommendationQualityAuditSummary(
        recommendation_count=recommendation_count,
        weak_recommendation_count=sum(
            1
            for item in items
            if item["unsupported_or_weak_claim_flags"] or item["confidence"] == "low" or item["coverage_warning"]
        ),
        needs_follow_up_count=sum(1 for item in items if item["needs_follow_up"]),
        high_confidence_count=confidence_counts.get("high", 0),
        medium_confidence_count=confidence_counts.get("medium", 0),
        low_confidence_count=confidence_counts.get("low", 0),
        average_supporting_paper_count=round(support_total / recommendation_count, 2) if recommendation_count else 0.0,
        full_text_support_total=sum(int(item["full_text_support_count"]) for item in items),
        ai_grounded_support_total=sum(int(item["ai_grounded_support_count"]) for item in items),
        abstract_only_support_total=sum(int(item["abstract_only_support_count"]) for item in items),
        coverage_warning_count=sum(1 for item in items if item["coverage_warning"]),
    ).model_dump()
    return {
        "mode": "recommendation_quality_audit",
        "topic_id": payload.get("topic_id"),
        "summary": summary,
        "items": items,
    }


def _h006_unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item).strip()))


def _h006_category(score: int, reasons: list[str] | None = None) -> dict[str, Any]:
    normalized = max(0, min(H006_CATEGORY_MAX_SCORE, int(score)))
    reason_list = _h006_unique(reasons or [])
    if normalized == 0:
        status = "fail"
    elif normalized == H006_CATEGORY_MAX_SCORE and not reason_list:
        status = "pass"
    else:
        status = "warning"
    return {
        "score": normalized,
        "max_score": H006_CATEGORY_MAX_SCORE,
        "status": status,
        "reasons": reason_list,
    }


def _h006_line_negates_claim(line: str) -> bool:
    low = line.lower()
    return any(
        marker in low
        for marker in [
            "not ",
            "no ",
            "without ",
            "does not",
            "do not",
            "not proven",
            "not verified",
            "아님",
            "아니",
            "않",
            "없",
            "미검증",
        ]
    )


def _h006_has_pattern(text: str, patterns: list[str]) -> bool:
    for line in str(text or "").splitlines() or [str(text or "")]:
        if _h006_line_negates_claim(line):
            continue
        low = line.lower()
        if any(re.search(pattern, low) for pattern in patterns):
            return True
    return False


def _h006_text(candidate: Mapping[str, Any]) -> str:
    fields = [
        "title",
        "gap_statement",
        "novelty_rationale",
        "problem_statement",
        "why_now",
        "proposed_research_question",
        "suggested_method_direction",
        "expected_contribution",
        "differentiation_claim",
        "coverage_warning",
        "source_basis",
    ]
    pieces = [str(candidate.get(field) or "") for field in fields]
    for key in ("risk_notes", "required_follow_up_evidence", "nearest_existing_work"):
        value = candidate.get(key)
        if isinstance(value, list):
            pieces.extend(str(item) for item in value)
    refs = candidate.get("supporting_evidence_refs") if isinstance(candidate.get("supporting_evidence_refs"), list) else []
    for ref in refs:
        if isinstance(ref, Mapping):
            pieces.append(str(ref.get("quoted_or_summarized_basis") or ""))
    return "\n".join(piece for piece in pieces if piece)


def _h006_normalized_signature(candidate: Mapping[str, Any], recommendation_type: str) -> str:
    if recommendation_type == "gap":
        basis = f"{candidate.get('title') or ''} {candidate.get('gap_statement') or ''}"
    else:
        basis = f"{candidate.get('title') or ''} {candidate.get('problem_statement') or ''}"
    tokens = sorted(_tokens(str(basis)))
    return " ".join(tokens[:18]) or _slug(str(candidate.get("title") or "candidate"))


def _h006_known_paper_map(known_papers: Any) -> dict[str, dict[str, Any]]:
    if known_papers is None:
        return {}
    rows: list[Any]
    if isinstance(known_papers, Mapping):
        rows = [
            {**value, "paper_id": key} if isinstance(value, dict) and "paper_id" not in value else value
            for key, value in known_papers.items()
        ]
    elif isinstance(known_papers, list):
        rows = known_papers
    else:
        rows = []
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        paper_id = str(row.get("paper_id") or "").strip()
        if paper_id:
            mapped[paper_id] = dict(row)
    return mapped


def _h006_refs(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = candidate.get("supporting_evidence_refs")
    if not isinstance(refs, list):
        return []
    return [dict(ref) for ref in refs if isinstance(ref, Mapping)]


def _h006_supporting_ids(candidate: Mapping[str, Any], refs: list[dict[str, Any]]) -> list[str]:
    ids = [str(value).strip() for value in candidate.get("supporting_paper_ids") or [] if str(value).strip()]
    for ref in refs:
        ref_id = str(ref.get("paper_id") or "").strip()
        if ref_id:
            ids.append(ref_id)
    return _h006_unique(ids)


def _h006_strong_ref(ref: Mapping[str, Any]) -> bool:
    return str(ref.get("review_level") or "") in H006_STRONG_REVIEW_LEVELS


def _h006_ref_has_trace(ref: Mapping[str, Any]) -> bool:
    if not _h006_strong_ref(ref):
        return True
    return bool(str(ref.get("source_text_sha256") or "").strip() and str(ref.get("evidence_span_id") or "").strip())


def _h006_known_review_has_trace(review: Mapping[str, Any]) -> bool:
    status = str(review.get("full_text_status") or "")
    if status != "reviewed":
        return True
    spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
    return bool(str(review.get("source_text_sha256") or "").strip() and spans)


def _h006_required_rationale_fields(recommendation_type: str) -> tuple[str, ...]:
    if recommendation_type == "gap":
        return ("title", "gap_statement", "novelty_rationale")
    return (
        "title",
        "problem_statement",
        "why_now",
        "proposed_research_question",
        "differentiation_claim",
    )


def _h006_actionable(candidate: Mapping[str, Any], recommendation_type: str) -> bool:
    if recommendation_type == "new_topic":
        required = ["proposed_research_question", "suggested_method_direction", "required_follow_up_evidence"]
        return all(bool(candidate.get(field)) for field in required)
    text = _h006_text(candidate).lower()
    return bool(str(candidate.get("novelty_rationale") or "").strip() and any(marker in text for marker in H006_ACTION_MARKERS))


def _h006_has_caution(candidate: Mapping[str, Any]) -> bool:
    text = _h006_text(candidate).lower()
    return any(marker in text for marker in H006_CAUTION_MARKERS)


def _h006_coverage_needs_disclosure(payload: Mapping[str, Any]) -> bool:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    try:
        reviewed = int(summary.get("full_text_reviewed_count") or 0)
        denominator = int(summary.get("topic_relevant_unique_count") or 0)
        ai_grounded = int(summary.get("ai_grounded_accepted_count") or 0)
    except (TypeError, ValueError):
        return False
    if denominator and reviewed / denominator < 0.5:
        return True
    if reviewed and ai_grounded / reviewed < 0.5:
        return True
    return False


def evaluate_recommendation_quality(payload: dict[str, Any], *, known_papers: Any = None) -> dict[str, Any]:
    known_by_id = _h006_known_paper_map(known_papers)
    items: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    category_reasons: dict[str, list[str]] = {category: [] for category in H006_CATEGORIES}
    critical_flags: list[str] = []
    signatures: dict[str, str] = {}
    referenced_paper_ids: list[str] = []

    def add_issue(
        reason: str,
        *,
        recommendation_id: str | None = None,
        category: str | None = None,
        severity: str = "warning",
        evidence: str | None = None,
    ) -> None:
        if severity == "critical":
            critical_flags.append(reason)
        if category:
            category_reasons[category].append(reason)
        issues.append(
            {
                "recommendation_id": recommendation_id,
                "reason": reason,
                "severity": severity,
                "category": category,
                "evidence": evidence,
            }
        )

    recommendation_items = _recommendation_items(payload)
    if not recommendation_items:
        add_issue(
            "recommendation_payload_empty",
            category="Provenance completeness",
            severity="critical",
            evidence="gap_candidates/new_topic_candidates empty or missing recommendation IDs",
        )

    for recommendation_id, recommendation_type, candidate in recommendation_items:
        refs = _h006_refs(candidate)
        supporting_ids = _h006_supporting_ids(candidate, refs)
        referenced_paper_ids.extend(supporting_ids)
        text = _h006_text(candidate)
        strong_refs = [ref for ref in refs if _h006_strong_ref(ref)]
        abstract_refs = [ref for ref in refs if str(ref.get("review_level") or "") == "abstract_only"]
        item_flags: list[str] = []
        item_warnings: list[str] = []

        if not supporting_ids:
            reason = "recommendation_missing_supporting_paper"
            add_issue(reason, recommendation_id=recommendation_id, category="Provenance completeness", severity="critical")
            item_flags.append(reason)
        if not refs:
            reason = "recommendation_missing_evidence_ref"
            add_issue(reason, recommendation_id=recommendation_id, category="Evidence support", severity="critical")
            item_flags.append(reason)
        for paper_id in supporting_ids:
            if known_by_id and paper_id not in known_by_id:
                reason = "supporting_paper_unknown"
                add_issue(reason, recommendation_id=recommendation_id, category="Citation and paper traceability", severity="critical", evidence=paper_id)
                item_flags.append(reason)
        for ref in refs:
            ref_id = str(ref.get("paper_id") or "").strip()
            if ref_id and ref_id not in supporting_ids:
                reason = "evidence_ref_not_in_supporting_papers"
                add_issue(reason, recommendation_id=recommendation_id, category="Citation and paper traceability", severity="warning", evidence=ref_id)
                item_warnings.append(reason)
            if not str(ref.get("quoted_or_summarized_basis") or "").strip():
                reason = "evidence_ref_missing_basis"
                add_issue(reason, recommendation_id=recommendation_id, category="Provenance completeness", severity="warning", evidence=ref_id or None)
                item_warnings.append(reason)
            if _h006_strong_ref(ref) and not str(ref.get("source_text_sha256") or "").strip():
                reason = "reviewed_support_missing_source_text_sha256"
                add_issue(reason, recommendation_id=recommendation_id, category="Evidence support", severity="critical", evidence=ref_id or None)
                item_flags.append(reason)
            if _h006_strong_ref(ref) and not str(ref.get("evidence_span_id") or "").strip():
                reason = "reviewed_support_missing_evidence_span"
                add_issue(reason, recommendation_id=recommendation_id, category="Evidence support", severity="critical", evidence=ref_id or None)
                item_flags.append(reason)
        if known_by_id:
            for paper_id in supporting_ids:
                review = known_by_id.get(paper_id)
                if review and not _h006_known_review_has_trace(review):
                    reason = "known_reviewed_paper_missing_trace"
                    add_issue(reason, recommendation_id=recommendation_id, category="Evidence support", severity="critical", evidence=paper_id)
                    item_flags.append(reason)

        if not strong_refs:
            reason = "recommendation_lacks_strong_evidence"
            add_issue(reason, recommendation_id=recommendation_id, category="Evidence support", severity="warning")
            item_warnings.append(reason)
        if abstract_refs and not _h006_has_caution(candidate):
            reason = "abstract_only_support_without_preliminary_label"
            add_issue(reason, recommendation_id=recommendation_id, category="Speculation honesty", severity="warning")
            item_warnings.append(reason)
        if abstract_refs and not strong_refs and _h006_has_pattern(text, H006_FULL_TEXT_OVERCLAIM_PATTERNS):
            reason = "full_text_support_claimed_for_abstract_only"
            add_issue(reason, recommendation_id=recommendation_id, category="Evidence support", severity="critical")
            item_flags.append(reason)

        missing_rationale_fields = [
            field
            for field in _h006_required_rationale_fields(recommendation_type)
            if not str(candidate.get(field) or "").strip()
        ]
        if missing_rationale_fields:
            reason = "recommendation_missing_gap_rationale"
            add_issue(
                reason,
                recommendation_id=recommendation_id,
                category="Gap rationale quality",
                severity="warning",
                evidence=",".join(missing_rationale_fields),
            )
            item_warnings.append(reason)
        if not _h006_actionable(candidate, recommendation_type):
            reason = "recommendation_missing_actionable_followup"
            add_issue(reason, recommendation_id=recommendation_id, category="Actionability", severity="warning")
            item_warnings.append(reason)
        if not _h006_has_caution(candidate):
            reason = "recommendation_missing_speculation_label"
            add_issue(reason, recommendation_id=recommendation_id, category="Speculation honesty", severity="warning")
            item_warnings.append(reason)

        if _h006_has_pattern(text, H006_PROVIDER_OVERCLAIM_PATTERNS):
            reason = "provider_generated_recommendation_claim"
            add_issue(reason, recommendation_id=recommendation_id, category="Provider truthfulness", severity="critical")
            item_flags.append(reason)
        if _h006_has_pattern(text, H006_NOVELTY_OVERCLAIM_PATTERNS):
            reason = "novelty_claim_without_support"
            add_issue(reason, recommendation_id=recommendation_id, category="Speculation honesty", severity="critical")
            item_flags.append(reason)
        if _h006_has_pattern(text, H006_VERIFIED_FACT_PATTERNS):
            reason = "recommendation_presented_as_verified_fact"
            add_issue(reason, recommendation_id=recommendation_id, category="Speculation honesty", severity="critical")
            item_flags.append(reason)

        if _h006_has_pattern(text, H006_HIGH_STAKES_PATTERNS) and not _h006_has_caution(candidate):
            reason = "unsupported_high_stakes_claim"
            add_issue(reason, recommendation_id=recommendation_id, category="Safety of downstream use", severity="critical")
            item_flags.append(reason)
        if _h006_has_pattern(text, H006_SECRET_PATTERNS):
            reason = "recommendation_mentions_secret_material"
            add_issue(reason, recommendation_id=recommendation_id, category="Safety of downstream use", severity="critical")
            item_flags.append(reason)

        signature = _h006_normalized_signature(candidate, recommendation_type)
        if signature in signatures:
            reason = "duplicate_recommendation"
            add_issue(
                reason,
                recommendation_id=recommendation_id,
                category="Diversity and non-duplication",
                severity="critical",
                evidence=f"{signatures[signature]}::{recommendation_id}",
            )
            item_flags.append(reason)
        signatures[signature] = recommendation_id

        items.append(
            {
                "recommendation_id": recommendation_id,
                "recommendation_type": recommendation_type,
                "supporting_paper_ids": supporting_ids,
                "evidence_ref_count": len(refs),
                "strong_evidence_ref_count": len(strong_refs),
                "abstract_only_ref_count": len(abstract_refs),
                "critical_fail_flags": _h006_unique(item_flags),
                "warning_flags": _h006_unique(item_warnings),
                "has_speculation_label": _h006_has_caution(candidate),
                "has_actionable_followup": _h006_actionable(candidate, recommendation_type),
            }
        )

    coverage_needs_disclosure = _h006_coverage_needs_disclosure(payload)
    if coverage_needs_disclosure:
        missing_coverage_ids = []
        for recommendation_id, _recommendation_type, candidate in recommendation_items:
            candidate_warning = str(candidate.get("coverage_warning") or payload.get("coverage_warning") or "").strip()
            if not candidate_warning:
                missing_coverage_ids.append(recommendation_id)
        if missing_coverage_ids:
            add_issue(
                "low_coverage_without_disclosure",
                category="Coverage awareness",
                severity="warning",
                evidence=",".join(missing_coverage_ids),
            )

    if recommendation_items and not category_reasons["Coverage awareness"]:
        category_reasons["Coverage awareness"] = []
    category_scores: dict[str, dict[str, Any]] = {}
    for category in H006_CATEGORIES:
        reasons = _h006_unique(category_reasons[category])
        if any(issue["category"] == category and issue["severity"] == "critical" for issue in issues):
            score = 0
        elif reasons:
            score = 1
        else:
            score = H006_CATEGORY_MAX_SCORE
        category_scores[category] = _h006_category(score, reasons)

    total_score = sum(int(score["score"]) for score in category_scores.values())
    max_score = H006_READY_SCORE
    unique_critical_flags = _h006_unique(critical_flags)
    if unique_critical_flags:
        classification = "blocked_truthfulness_issue"
    elif total_score == max_score and recommendation_items:
        classification = "recommendation_ready_local_rc"
    elif total_score >= H006_WARNING_SCORE and recommendation_items:
        classification = "usable_with_warnings"
    else:
        classification = "needs_revision"

    return {
        "mode": "h006_recommendation_quality_evaluation",
        "topic_id": payload.get("topic_id"),
        "recommendation_quality_classification": classification,
        "h006_total_score": total_score,
        "h006_max_score": max_score,
        "h006_warning_score": H006_WARNING_SCORE,
        "category_scores": category_scores,
        "critical_fail_flags": unique_critical_flags,
        "issue_count": len(issues),
        "issues": issues,
        "items": items,
        "summary": {
            "recommendation_count": len(recommendation_items),
            "known_paper_count": len(known_by_id),
            "referenced_paper_count": len(set(referenced_paper_ids)),
            "coverage_needs_disclosure": coverage_needs_disclosure,
            "duplicate_recommendation_count": sum(1 for issue in issues if issue["reason"] == "duplicate_recommendation"),
            "abstract_only_recommendation_count": sum(1 for item in items if int(item["abstract_only_ref_count"]) > 0),
            "strong_evidence_recommendation_count": sum(1 for item in items if int(item["strong_evidence_ref_count"]) > 0),
        },
    }


def _tokens(value: str) -> set[str]:
    tokens = {token for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", value.lower()) if token not in STOPWORDS}
    expanded: set[str] = set(tokens)
    text = value.lower()
    for theme, terms in THEMES.items():
        if any(term in text for term in terms):
            expanded.add(theme)
            expanded.update(term for term in terms if len(term) >= 3)
    return expanded


def _recommendation_terms(candidate: dict[str, Any]) -> set[str]:
    pieces = [
        str(candidate.get("title") or ""),
        str(candidate.get("gap_statement") or ""),
        str(candidate.get("problem_statement") or ""),
        str(candidate.get("proposed_research_question") or ""),
        str(candidate.get("suggested_method_direction") or ""),
        " ".join(str(value) for value in candidate.get("supporting_paper_ids") or []),
    ]
    for ref in candidate.get("supporting_evidence_refs") or []:
        if isinstance(ref, dict):
            pieces.append(str(ref.get("quoted_or_summarized_basis") or ""))
    return _tokens(" ".join(pieces))


def _candidate_terms(candidate: dict[str, Any]) -> set[str]:
    return _tokens(
        " ".join(
            [
                str(candidate.get("title") or ""),
                str(candidate.get("abstract_text") or candidate.get("abstract") or ""),
                str(candidate.get("topic_fit_rationale") or ""),
            ]
        )
    )


def _existing_review_maps(
    topic_id: str,
    *,
    read_only: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    reviews = _dedupe_reviews(paper_reviews.list_reviews(topic_id=topic_id, limit=100_000, read_only=read_only))
    by_normalized = {paper_review.normalize_paper_id(review.get("paper_id")): review for review in reviews}
    return by_normalized, _accepted_ai_drafts_for_reviews(topic_id, reviews, read_only=read_only)


def _candidate_rows_for_topic(topic_id: str) -> list[dict[str, Any]]:
    try:
        consolidated = paper_review.consolidate_topic_paper_candidates(topic_id, limit=1000)
    except Exception:
        return []
    rows = consolidated.get("candidates") if isinstance(consolidated.get("candidates"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _impact_for_candidate(current_confidence: str, *, has_pdf_url: bool, overlap_count: int) -> str:
    if current_confidence == "low" and has_pdf_url and overlap_count:
        return "high"
    if has_pdf_url or overlap_count >= 2:
        return "medium"
    return "low"


def _best_impact(values: list[str]) -> str:
    if not values:
        return "low"
    return max(values, key=lambda value: IMPACT_ORDER.get(value, 0))


def plan_recommendation_followup(
    topic_id: str,
    recommendation_payload: dict[str, Any],
    *,
    limit: int = 10,
    candidate_rows: list[dict[str, Any]] | None = None,
    read_only: bool = True,
) -> dict[str, Any]:
    resolved_topic_id = str(recommendation_payload.get("topic_id") or topic_id)
    candidates = list(candidate_rows) if candidate_rows is not None else _candidate_rows_for_topic(resolved_topic_id)
    existing_by_normalized, ai_drafts = _existing_review_maps(resolved_topic_id, read_only=read_only)
    plans: list[dict[str, Any]] = []
    blocked_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    impact_counts: Counter[str] = Counter()

    for recommendation_id, recommendation_type, recommendation in _recommendation_items(recommendation_payload):
        terms = _recommendation_terms(recommendation)
        current_confidence = str(recommendation.get("confidence") or "low")
        if current_confidence not in CONFIDENCE_ORDER:
            current_confidence = "low"
        detailed: list[dict[str, Any]] = []
        excluded_reviewed = 0
        for row in candidates:
            paper_id = str(row.get("paper_id") or "").strip()
            normalized_id = paper_review.normalize_paper_id(row.get("normalized_paper_id") or paper_id)
            if not paper_id or not normalized_id:
                continue
            existing_review = existing_by_normalized.get(normalized_id)
            current_status = str(
                (existing_review or {}).get("full_text_status")
                or row.get("current_full_text_status")
                or "unavailable"
            )
            if current_status == "reviewed":
                excluded_reviewed += 1
                continue
            candidate_terms = _candidate_terms(row)
            overlap = sorted(terms & candidate_terms)
            relevance = float(row.get("topic_relevance_score") or row.get("relevance_score") or 0)
            if not overlap and relevance < 60:
                continue
            pdf_url = str(row.get("pdf_url") or "").strip()
            has_pdf_url = bool(pdf_url)
            reason_codes = []
            if overlap:
                reason_codes.append("related_theme_match")
            if relevance >= 60:
                reason_codes.append("topic_relevant")
            if has_pdf_url:
                reason_codes.append("trusted_pdf_url")
            if existing_review:
                reason_codes.append(f"existing_{current_status}_review")
            blocked_reason = None if has_pdf_url else "missing_pdf_url"
            if blocked_reason:
                blocked_counts[blocked_reason] += 1
            for reason in reason_codes:
                reason_counts[reason] += 1
            ai_status = "accepted" if existing_review and _draft_for_review(existing_review, ai_drafts) else "none"
            impact = _impact_for_candidate(current_confidence, has_pdf_url=has_pdf_url, overlap_count=len(overlap))
            detailed.append(
                {
                    "paper_id": paper_id,
                    "title": str(row.get("title") or ""),
                    "candidate_reason_codes": reason_codes,
                    "has_pdf_url": has_pdf_url,
                    "current_full_text_status": current_status,
                    "current_ai_grounded_status": ai_status,
                    "acquisition_priority": 0,
                    "expected_confidence_impact": impact,
                    "blocked_reason": blocked_reason,
                    "pdf_url": pdf_url or None,
                    "relevance_score": relevance,
                    "_sort": (
                        1 if has_pdf_url else 0,
                        len(overlap),
                        relevance,
                        str(row.get("published_at") or ""),
                        paper_id,
                    ),
                }
            )
        detailed.sort(key=lambda item: item["_sort"], reverse=True)
        limited: list[dict[str, Any]] = []
        for rank, item in enumerate(detailed[: max(0, int(limit))], start=1):
            public_item = {key: value for key, value in item.items() if not key.startswith("_")}
            public_item["acquisition_priority"] = rank
            candidate_model = RecommendationFollowupCandidate(**public_item).model_dump()
            limited.append(candidate_model)
            impact_counts[candidate_model["expected_confidence_impact"]] += 1
        aggregate_reasons = sorted({reason for item in limited for reason in item["candidate_reason_codes"]})
        statuses = sorted({item["current_full_text_status"] for item in limited})
        ai_statuses = sorted({item["current_ai_grounded_status"] for item in limited})
        blocked = sorted({item["blocked_reason"] for item in limited if item.get("blocked_reason")})
        desired = [
            "download or reuse trusted PDFs for related unreviewed candidates",
            "extract local text and store evidence spans before raising confidence",
            "generate accepted AI-grounded drafts only after M4 validation",
        ]
        if excluded_reviewed:
            reason_counts["excluded_already_full_text_reviewed"] += excluded_reviewed
        plan = RecommendationFollowupPlanItem(
            recommendation_id=recommendation_id,
            target_gap_or_topic_id=recommendation_id,
            current_confidence=current_confidence,  # type: ignore[arg-type]
            desired_next_evidence=desired,
            candidate_paper_ids=[item["paper_id"] for item in limited],
            candidate_titles=[item["title"] for item in limited],
            candidate_reason_codes=aggregate_reasons,
            has_pdf_url=any(item["has_pdf_url"] for item in limited),
            current_full_text_status="mixed" if len(statuses) > 1 else (statuses[0] if statuses else "none"),
            current_ai_grounded_status="mixed" if len(ai_statuses) > 1 else (ai_statuses[0] if ai_statuses else "none"),
            acquisition_priority=int(limited[0]["acquisition_priority"]) if limited else None,
            expected_confidence_impact=_best_impact([item["expected_confidence_impact"] for item in limited]),  # type: ignore[arg-type]
            blocked_reason=", ".join(blocked) if blocked else None,
            candidates=limited,
        ).model_dump()
        plans.append(plan)

    summary = RecommendationFollowupPlanSummary(
        recommendation_count=len(_recommendation_items(recommendation_payload)),
        followup_plan_count=len(plans),
        followup_candidate_count=sum(len(plan["candidates"]) for plan in plans),
        blocked_reason_counts=dict(sorted(blocked_counts.items())),
        candidate_reason_counts=dict(sorted(reason_counts.items())),
        high_impact_count=impact_counts.get("high", 0),
        medium_impact_count=impact_counts.get("medium", 0),
        low_impact_count=impact_counts.get("low", 0),
    ).model_dump()
    return {
        "mode": "recommendation_followup_plan",
        "topic_id": resolved_topic_id,
        "summary": summary,
        "plans": plans,
    }


def _impact_allowed(value: str, minimum: str) -> bool:
    return IMPACT_ORDER.get(value, 0) >= IMPACT_ORDER.get(minimum, 0)


def _candidate_current_state(row: dict[str, Any], existing: dict[str, Any] | None) -> tuple[str, str, str | None]:
    current_status = str(
        (existing or {}).get("full_text_status")
        or row.get("current_full_text_status")
        or "unavailable"
    )
    local_state, local_path = paper_review._candidate_local_artifact_state(row)
    if current_status == "unavailable" and local_state:
        current_status = "full_text_available"
    current_review_status = "reviewed" if existing else "unreviewed"
    return current_status, current_review_status, local_path


def _internal_candidate_rows_for_topic(topic_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    consolidated = paper_review._consolidate_rows(profile, paper_review._topic_rows(profile), limit=1000)
    return profile, [row for row in consolidated.get("candidates", []) if isinstance(row, dict)]


def _followup_execution_row_sort(item: dict[str, Any]) -> tuple[int, int, float, str, str]:
    return (
        1 if item.get("_has_pdf_url") else 0,
        int(item.get("_overlap_count") or 0),
        float(item.get("relevance_score") or 0),
        str(item.get("_published_at") or ""),
        str(item.get("paper_id") or ""),
    )


def plan_recommendation_followup_execution(
    topic_id: str,
    recommendation_payload: dict[str, Any],
    *,
    limit: int = 10,
    candidate_rows: list[dict[str, Any]] | None = None,
    allowed_domains: list[str] | None = None,
    provider: str = "arxiv",
    fetch_missing_pdf: bool = False,
    extract_after_fetch: bool = False,
    download_limit: int = 0,
    review_limit: int | None = None,
    min_impact: str = "low",
    read_only: bool = True,
) -> dict[str, Any]:
    resolved_topic_id = str(recommendation_payload.get("topic_id") or topic_id)
    candidates = list(candidate_rows) if candidate_rows is not None else _internal_candidate_rows_for_topic(resolved_topic_id)[1]
    existing_by_normalized, _ai_drafts = _existing_review_maps(resolved_topic_id, read_only=read_only)
    expected_provider = str(provider or "").strip().lower()
    max_downloads = max(0, int(download_limit or 0))
    max_reviews = None if review_limit is None else max(0, int(review_limit or 0))
    planned_downloads = 0
    planned_reviews = 0
    seen_normalized: set[str] = set()
    actions: list[dict[str, Any]] = []
    action_counts: Counter[str] = Counter()

    for recommendation_id, _recommendation_type, recommendation in _recommendation_items(recommendation_payload):
        terms = _recommendation_terms(recommendation)
        current_confidence = str(recommendation.get("confidence") or "low")
        if current_confidence not in CONFIDENCE_ORDER:
            current_confidence = "low"
        detailed: list[dict[str, Any]] = []
        for row in candidates:
            paper_id = str(row.get("paper_id") or "").strip()
            normalized_id = paper_review.normalize_paper_id(row.get("normalized_paper_id") or paper_id)
            if not paper_id or not normalized_id:
                continue
            candidate_terms = _candidate_terms(row)
            overlap = sorted(terms & candidate_terms)
            relevance = float(row.get("topic_relevance_score") or row.get("relevance_score") or 0)
            if not overlap and relevance < 60:
                continue
            existing = existing_by_normalized.get(normalized_id)
            current_status, current_review_status, local_path = _candidate_current_state(row, existing)
            pdf_url = str(row.get("pdf_url") or "").strip()
            has_pdf_url = bool(pdf_url)
            impact = _impact_for_candidate(current_confidence, has_pdf_url=has_pdf_url, overlap_count=len(overlap))
            detailed.append(
                {
                    **row,
                    "_recommendation_id": recommendation_id,
                    "_target_gap_or_topic_id": recommendation_id,
                    "_current_status": current_status,
                    "_current_review_status": current_review_status,
                    "_local_artifact_path": local_path,
                    "_has_pdf_url": has_pdf_url,
                    "_impact": impact,
                    "_overlap_count": len(overlap),
                    "_published_at": row.get("published_at") or row.get("published") or "",
                    "paper_id": paper_id,
                    "normalized_paper_id": normalized_id,
                    "relevance_score": relevance,
                }
            )
        detailed.sort(key=_followup_execution_row_sort, reverse=True)

        for row in detailed[: max(0, int(limit))]:
            paper_id = str(row.get("paper_id") or "")
            normalized_id = str(row.get("normalized_paper_id") or "")
            pdf_url = str(row.get("pdf_url") or "").strip()
            domain = paper_review._pdf_url_domain(pdf_url)
            allowed = bool(pdf_url and paper_review._domain_allowed_for_review(pdf_url, allowed_domains))
            source_provider = str(row.get("source_provider") or "").strip().lower()
            current_status = str(row.get("_current_status") or "unavailable")
            local_path = row.get("_local_artifact_path")
            local_state, _local_artifact_state_path = paper_review._candidate_local_artifact_state(row)
            impact = str(row.get("_impact") or "low")
            reason_codes: list[str] = []
            planned_action = "blocked"

            if normalized_id in seen_normalized:
                planned_action = "skip_blocked"
                reason_codes.append("duplicate_followup_candidate")
            elif not _impact_allowed(impact, min_impact):
                planned_action = "skip_blocked"
                reason_codes.append("below_min_impact")
            elif current_status == "reviewed":
                planned_action = "skip_existing_reviewed"
                reason_codes.append("already_full_text_reviewed")
            elif expected_provider and source_provider and source_provider != expected_provider:
                planned_action = "skip_blocked"
                reason_codes.append("provider_mismatch")
            elif not pdf_url:
                planned_action = "skip_no_pdf_url"
                reason_codes.append("missing_pdf_url")
            elif not allowed:
                planned_action = "skip_blocked"
                reason_codes.append("domain_not_allowed")
            elif local_state == "local_text_exists":
                if max_reviews is not None and planned_reviews >= max_reviews:
                    planned_action = "skip_blocked"
                    reason_codes.append("review_limit_reached")
                else:
                    planned_action = "reuse_existing_artifact"
                    reason_codes.append("existing_extracted_text")
                    planned_reviews += 1
            elif local_state == "local_pdf_exists":
                if not extract_after_fetch:
                    planned_action = "skip_blocked"
                    reason_codes.append("extract_after_fetch_required")
                elif max_reviews is not None and planned_reviews >= max_reviews:
                    planned_action = "skip_blocked"
                    reason_codes.append("review_limit_reached")
                else:
                    planned_action = "reuse_existing_artifact"
                    reason_codes.append("existing_pdf_extract")
                    planned_reviews += 1
            elif not fetch_missing_pdf:
                planned_action = "skip_blocked"
                reason_codes.append("fetch_missing_pdf_required")
            elif not extract_after_fetch:
                planned_action = "skip_blocked"
                reason_codes.append("extract_after_fetch_required")
            elif max_downloads <= 0:
                planned_action = "skip_blocked"
                reason_codes.append("download_limit_required")
            elif planned_downloads >= max_downloads:
                planned_action = "skip_blocked"
                reason_codes.append("download_limit_reached")
            elif max_reviews is not None and planned_reviews >= max_reviews:
                planned_action = "skip_blocked"
                reason_codes.append("review_limit_reached")
            else:
                planned_action = "download_extract_review"
                reason_codes.extend(["trusted_pdf_url", "explicit_fetch_required", "local_extraction_required"])
                planned_downloads += 1
                planned_reviews += 1

            if normalized_id:
                seen_normalized.add(normalized_id)
            action_counts[planned_action] += 1
            action = RecommendationFollowupExecutionActionItem(
                recommendation_id=str(row.get("_recommendation_id") or recommendation_id),
                target_gap_or_topic_id=str(row.get("_target_gap_or_topic_id") or recommendation_id),
                paper_id=paper_id,
                normalized_paper_id=normalized_id,
                title=str(row.get("title") or ""),
                pdf_url=pdf_url or None,
                pdf_url_domain=domain,
                allowed_domain_match=allowed,
                current_full_text_status=current_status,
                current_review_status=str(row.get("_current_review_status") or "unreviewed"),
                acquisition_priority=len(actions) + 1,
                expected_confidence_impact=impact,  # type: ignore[arg-type]
                planned_action=planned_action,  # type: ignore[arg-type]
                reason_codes=reason_codes,
                relevance_score=float(row.get("relevance_score") or 0),
                source_provider=str(row.get("source_provider") or "") or None,
                local_artifact_path=str(local_path) if local_path else None,
                previous_acquisition_status=paper_review._candidate_previous_acquisition_status(row),
            ).model_dump()
            actions.append(action)

    planned_followup_count = action_counts.get("download_extract_review", 0) + action_counts.get("reuse_existing_artifact", 0)
    summary = RecommendationFollowupExecutionSummary(
        planned_followup_count=planned_followup_count,
        skipped_existing_reviewed_count=action_counts.get("skip_existing_reviewed", 0),
        skipped_no_pdf_url_count=action_counts.get("skip_no_pdf_url", 0),
        skipped_blocked_count=action_counts.get("skip_blocked", 0) + action_counts.get("blocked", 0),
        full_text_reviewed_count_before=int((recommendation_payload.get("summary") or {}).get("full_text_reviewed_count") or 0),
        full_text_reviewed_count_after=int((recommendation_payload.get("summary") or {}).get("full_text_reviewed_count") or 0),
    ).model_dump()
    summary.update(
        {
            "action_count": len(actions),
            "download_limit": max_downloads,
            "review_limit": max_reviews,
            "min_impact": min_impact,
        }
    )
    return {
        "mode": "recommendation_followup_execution_plan",
        "topic_id": resolved_topic_id,
        "dry_run": True,
        "summary": summary,
        "actions": actions,
    }


def _audit_item_map(audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("recommendation_id") or ""): item
        for item in audit.get("items", [])
        if isinstance(item, dict) and str(item.get("recommendation_id") or "")
    }


def _confidence_distribution(audit: dict[str, Any]) -> dict[str, int]:
    summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    return {
        "low": int(summary.get("low_confidence_count") or 0),
        "medium": int(summary.get("medium_confidence_count") or 0),
        "high": int(summary.get("high_confidence_count") or 0),
    }


def compare_recommendation_quality(before_payload: dict[str, Any], after_payload: dict[str, Any]) -> dict[str, Any]:
    before_audit = audit_recommendation_quality(before_payload)
    after_audit = audit_recommendation_quality(after_payload)
    before_summary = before_payload.get("summary") if isinstance(before_payload.get("summary"), dict) else {}
    after_summary = after_payload.get("summary") if isinstance(after_payload.get("summary"), dict) else {}
    before_items = _audit_item_map(before_audit)
    after_items = _audit_item_map(after_audit)
    improved: list[str] = []
    unchanged: list[str] = []
    for recommendation_id, after_item in after_items.items():
        before_item = before_items.get(recommendation_id)
        if not before_item:
            unchanged.append(recommendation_id)
            continue
        before_strength = int(before_item.get("full_text_support_count") or 0) + int(before_item.get("ai_grounded_support_count") or 0)
        after_strength = int(after_item.get("full_text_support_count") or 0) + int(after_item.get("ai_grounded_support_count") or 0)
        before_flags = len(before_item.get("unsupported_or_weak_claim_flags") or [])
        after_flags = len(after_item.get("unsupported_or_weak_claim_flags") or [])
        confidence_improved = CONFIDENCE_ORDER.get(str(after_item.get("confidence") or "low"), 0) > CONFIDENCE_ORDER.get(
            str(before_item.get("confidence") or "low"),
            0,
        )
        if after_strength > before_strength or after_flags < before_flags or confidence_improved:
            improved.append(recommendation_id)
        else:
            unchanged.append(recommendation_id)

    comparison = RecommendationQualityComparison(
        new_reviews_created=int(after_summary.get("full_text_reviewed_count") or 0) > int(before_summary.get("full_text_reviewed_count") or 0),
        full_text_reviewed_count_before=int(before_summary.get("full_text_reviewed_count") or 0),
        full_text_reviewed_count_after=int(after_summary.get("full_text_reviewed_count") or 0),
        ai_grounded_accepted_count_before=int(before_summary.get("ai_grounded_accepted_count") or 0),
        ai_grounded_accepted_count_after=int(after_summary.get("ai_grounded_accepted_count") or 0),
        weak_recommendation_count_before=int((before_audit.get("summary") or {}).get("weak_recommendation_count") or 0),
        weak_recommendation_count_after=int((after_audit.get("summary") or {}).get("weak_recommendation_count") or 0),
        needs_follow_up_count_before=int((before_audit.get("summary") or {}).get("needs_follow_up_count") or 0),
        needs_follow_up_count_after=int((after_audit.get("summary") or {}).get("needs_follow_up_count") or 0),
        confidence_distribution_before=_confidence_distribution(before_audit),
        confidence_distribution_after=_confidence_distribution(after_audit),
        evidence_ref_count_before=sum(int(item.get("evidence_ref_count") or 0) for item in before_items.values()),
        evidence_ref_count_after=sum(int(item.get("evidence_ref_count") or 0) for item in after_items.values()),
        recommendations_improved=sorted(improved),
        recommendations_unchanged=sorted(unchanged),
        recommendations_still_blocked=sorted(
            str(item.get("recommendation_id"))
            for item in after_items.values()
            if item.get("needs_follow_up")
        ),
    ).model_dump()
    return comparison


def execute_recommendation_followup(
    topic_id: str,
    recommendation_payload: dict[str, Any],
    *,
    dry_run: bool = False,
    limit: int = 10,
    allowed_domains: list[str] | None = None,
    provider: str = "arxiv",
    fetch_missing_pdf: bool = False,
    extract_after_fetch: bool = False,
    download_limit: int = 0,
    review_limit: int | None = None,
    min_impact: str = "low",
    max_pdf_bytes: int = pdf_acquisition.DEFAULT_MAX_PDF_BYTES,
    download_timeout: int = pdf_acquisition.DEFAULT_TIMEOUT_SECONDS,
    max_text_chars: int = 24000,
    allow_bounded_pdf_fetch: bool = False,
    env: Mapping[str, str] | None = None,
    compare_quality: bool = False,
    candidate_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_topic_id = str(recommendation_payload.get("topic_id") or topic_id)
    _profile, internal_candidates = _internal_candidate_rows_for_topic(resolved_topic_id)
    plan_candidates = candidate_rows if candidate_rows is not None else internal_candidates
    execution = plan_recommendation_followup_execution(
        resolved_topic_id,
        recommendation_payload,
        limit=limit,
        candidate_rows=plan_candidates,
        allowed_domains=allowed_domains,
        provider=provider,
        fetch_missing_pdf=fetch_missing_pdf,
        extract_after_fetch=extract_after_fetch,
        download_limit=download_limit,
        review_limit=review_limit,
        min_impact=min_impact,
        read_only=dry_run,
    )
    execution["mode"] = "recommendation_followup_execution"
    execution["dry_run"] = bool(dry_run)
    before_payload = recommendation_payload

    if dry_run:
        if compare_quality:
            execution["recommendation_quality_comparison"] = compare_recommendation_quality(before_payload, before_payload)
        return execution

    candidate_source = plan_candidates if all(isinstance(row, dict) and "_run" in row and "_item" in row for row in plan_candidates) else internal_candidates
    candidates_by_normalized = {
        str(row.get("normalized_paper_id") or ""): row
        for row in candidate_source
        if isinstance(row, dict) and str(row.get("normalized_paper_id") or "")
    }
    actionable_normalized = [
        str(action.get("normalized_paper_id") or "")
        for action in execution["actions"]
        if action.get("planned_action") in {"download_extract_review", "reuse_existing_artifact"}
    ]
    selected_candidates = [candidates_by_normalized[value] for value in actionable_normalized if value in candidates_by_normalized]
    pdf_result = {
        "downloaded_count": 0,
        "extracted_count": 0,
        "rejected_count": 0,
        "failed_count": 0,
        "extraction_failed_count": 0,
        "items": [],
        "rejected": [],
        "audit_summary": {"rejected_by_reason": {}, "failed_by_reason": {}, "extraction_failed_by_reason": {}},
    }
    if selected_candidates:
        live_fetch_gate = pdf_acquisition.evaluate_bounded_pdf_fetch_gate(
            requested=fetch_missing_pdf,
            dry_run=dry_run,
            allow_bounded_pdf_fetch=allow_bounded_pdf_fetch,
            env=env,
            download_limit=download_limit,
            allowed_domains=allowed_domains,
            max_bytes=max_pdf_bytes,
            timeout_seconds=download_timeout,
        )
        pdf_result = pdf_acquisition.acquire_candidate_pdfs(
            selected_candidates,
            topic_id=resolved_topic_id,
            provider=provider,
            allowed_domains=allowed_domains,
            download_limit=download_limit,
            dry_run=dry_run,
            extract_after_fetch=extract_after_fetch,
            max_bytes=max_pdf_bytes,
            timeout_seconds=download_timeout,
            max_text_chars=max_text_chars,
            allow_network_fetch=bool(live_fetch_gate["live_fetch_allowed"]),
        )
        pdf_result["live_fetch_gate"] = live_fetch_gate
        pdf_result["live_fetch_allowed"] = bool(live_fetch_gate["live_fetch_allowed"])
        pdf_result["live_fetch_blocked_count"] = 0
        if fetch_missing_pdf and not live_fetch_gate["live_fetch_allowed"]:
            pdf_result["mode"] = "live_fetch_blocked"
            pdf_result["status"] = "live_fetch_blocked"
            pdf_result["live_fetch_blocked_count"] = int(pdf_result.get("planned_count") or 0)
            pdf_result["note"] = (
                "Live PDF fetch was blocked by bounded-fetch gates; recommendation follow-up stayed in plan-only mode."
            )

    action_by_normalized = {
        str(action.get("normalized_paper_id") or ""): action
        for action in execution["actions"]
        if str(action.get("normalized_paper_id") or "")
    }
    for item in [*(pdf_result.get("items") or []), *(pdf_result.get("rejected") or [])]:
        normalized_id = str(item.get("normalized_paper_id") or "")
        action = action_by_normalized.get(normalized_id)
        if not action:
            continue
        action["pdf_acquisition_status"] = item.get("status")
        action["pdf_acquisition_reason"] = item.get("reason")
        action["local_pdf_path"] = item.get("local_pdf_path")
        extraction = item.get("extraction") if isinstance(item.get("extraction"), dict) else {}
        if extraction:
            action["extraction_status"] = extraction.get("status")
            action["text_artifact_path"] = extraction.get("text_path")
            action["extraction_quality"] = extraction.get("quality")

    existing_by_normalized = paper_review._existing_reviews_by_normalized_paper_id(resolved_topic_id, read_only=dry_run)
    reviewed_from_followup_count = 0
    for candidate in selected_candidates:
        normalized_id = str(candidate.get("normalized_paper_id") or "")
        existing = existing_by_normalized.get(normalized_id)
        generated = paper_review.generate_deterministic_review(
            topic_profile=_profile,
            run=candidate["_run"],
            item=candidate["_item"],
            use_full_text=True,
            max_text_chars=max_text_chars,
            prefer_existing_text=True,
            extract_existing_pdf=extract_after_fetch,
        )
        if existing is not None and paper_review._status_rank(existing) > paper_review._status_rank(generated):
            review = existing
        elif existing is not None and paper_review._review_is_at_least_as_complete(existing, generated):
            review = existing
        else:
            review = paper_reviews.upsert_review(generated)
        action = action_by_normalized.get(normalized_id)
        status = str(review.get("full_text_status") or "unavailable")
        if action:
            action["actual_full_text_status"] = status
            action["source_text_sha256"] = review.get("source_text_sha256") if status == "reviewed" else None
            spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
            action["evidence_span_count"] = len(spans) if status == "reviewed" else 0
            if status == "reviewed" and "reviewed" not in action["reason_codes"]:
                action["reason_codes"].append("reviewed")
        if status == "reviewed" and (existing is None or str(existing.get("full_text_status") or "") != "reviewed"):
            reviewed_from_followup_count += 1

    after_payload = build_recommendations_for_topic(
        resolved_topic_id,
        limit=len(recommendation_payload.get("gap_candidates") or []) or 5,
        read_only=dry_run,
    )
    audit_summary = pdf_result.get("audit_summary") if isinstance(pdf_result.get("audit_summary"), dict) else {}
    execution["summary"].update(
        {
            "downloaded_count": int(pdf_result.get("downloaded_count") or 0),
            "extracted_count": int(pdf_result.get("extracted_count") or 0),
            "reviewed_from_followup_count": reviewed_from_followup_count,
            "rejected_count_by_reason": dict(audit_summary.get("rejected_by_reason") or {}),
            "failed_count_by_reason": dict(audit_summary.get("failed_by_reason") or {}),
            "extraction_failed_count_by_reason": dict(audit_summary.get("extraction_failed_by_reason") or {}),
            "full_text_reviewed_count_before": int((before_payload.get("summary") or {}).get("full_text_reviewed_count") or 0),
            "full_text_reviewed_count_after": int((after_payload.get("summary") or {}).get("full_text_reviewed_count") or 0),
        }
    )
    execution["pdf_acquisition"] = pdf_result
    if compare_quality:
        execution["recommendation_quality_comparison"] = compare_recommendation_quality(before_payload, after_payload)
        execution["recommendation_payload_after"] = after_payload
    return execution


def _write_json(path: str | Path | None, payload: dict[str, Any], *, dry_run: bool) -> bool:
    if not path or dry_run:
        return False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return True


def write_recommendation_outputs(
    topic_id: str,
    output_path: str | Path | None,
    *,
    limit: int,
    min_confidence: str = "low",
    include_abstract_only: bool = True,
    dry_run: bool = False,
    include_audit: bool = False,
    audit_output_path: str | Path | None = None,
    include_followup: bool = False,
    followup_output_path: str | Path | None = None,
    followup_limit: int = 10,
    include_followup_execution: bool = False,
    followup_execution_output_path: str | Path | None = None,
    followup_download_limit: int = 0,
    followup_review_limit: int | None = None,
    followup_min_impact: str = "low",
    fetch_missing_pdf: bool = False,
    provider: str = "arxiv",
    allow_domains: list[str] | None = None,
    extract_after_fetch: bool = False,
    max_pdf_bytes: int = pdf_acquisition.DEFAULT_MAX_PDF_BYTES,
    download_timeout: int = pdf_acquisition.DEFAULT_TIMEOUT_SECONDS,
    max_text_chars: int = 24000,
    allow_bounded_pdf_fetch: bool = False,
    compare_recommendation_quality: bool = False,
) -> dict[str, Any]:
    read_only_generation = not include_followup_execution or dry_run
    sidecars = _assert_recommendation_read_safe(read_only=read_only_generation)
    payload = build_recommendations_for_topic(
        topic_id,
        limit=limit,
        min_confidence=min_confidence,
        include_abstract_only=include_abstract_only,
        read_only=read_only_generation,
    )
    if include_audit:
        payload["recommendation_audit"] = audit_recommendation_quality(payload)
    if include_followup:
        payload["recommendation_followup"] = plan_recommendation_followup(
            topic_id,
            payload,
            limit=followup_limit,
            read_only=read_only_generation,
        )
    execution_payload: dict[str, Any] | None = None
    if include_followup_execution:
        execution_payload = execute_recommendation_followup(
            topic_id,
            payload,
            dry_run=dry_run,
            limit=followup_limit,
            allowed_domains=allow_domains,
            provider=provider,
            fetch_missing_pdf=fetch_missing_pdf,
            extract_after_fetch=extract_after_fetch,
            download_limit=followup_download_limit,
            review_limit=followup_review_limit,
            min_impact=followup_min_impact,
            max_pdf_bytes=max_pdf_bytes,
            download_timeout=download_timeout,
            max_text_chars=max_text_chars,
            allow_bounded_pdf_fetch=allow_bounded_pdf_fetch,
            compare_quality=compare_recommendation_quality,
        )
        after_payload = execution_payload.pop("recommendation_payload_after", None)
        if isinstance(after_payload, dict):
            payload = after_payload
            if include_audit:
                payload["recommendation_audit"] = audit_recommendation_quality(payload)
            if include_followup:
                payload["recommendation_followup"] = plan_recommendation_followup(
                    topic_id,
                    payload,
                    limit=followup_limit,
                    read_only=read_only_generation,
                )
        payload["recommendation_followup_execution"] = execution_payload
        if "recommendation_quality_comparison" in execution_payload:
            payload["recommendation_quality_comparison"] = execution_payload["recommendation_quality_comparison"]

    expected_exported_count = len(payload["gap_candidates"]) + len(payload["new_topic_candidates"])
    payload["dry_run"] = bool(dry_run)
    payload["db_read_only"] = bool(read_only_generation)
    payload["sidecar_guard"] = {"checked": True, "blocked": False, "sidecars": sidecars}
    payload["output_path"] = str(output_path) if output_path is not None else None
    payload["audit_output_path"] = str(audit_output_path) if audit_output_path is not None else None
    payload["followup_output_path"] = str(followup_output_path) if followup_output_path is not None else None
    payload["followup_execution_output_path"] = str(followup_execution_output_path) if followup_execution_output_path is not None else None
    payload["summary"]["exported_count"] = expected_exported_count if output_path and not dry_run else 0
    if include_audit:
        payload["recommendation_audit"]["summary"]["exported_count"] = (
            len(payload["recommendation_audit"].get("items", [])) if audit_output_path and not dry_run else 0
        )
    if include_followup:
        payload["recommendation_followup"]["summary"]["exported_count"] = (
            len(payload["recommendation_followup"].get("plans", [])) if followup_output_path and not dry_run else 0
        )

    written = _write_json(output_path, payload, dry_run=dry_run)
    audit_written = _write_json(
        audit_output_path,
        payload.get("recommendation_audit") if isinstance(payload.get("recommendation_audit"), dict) else {},
        dry_run=dry_run or not include_audit,
    )
    followup_written = _write_json(
        followup_output_path,
        payload.get("recommendation_followup") if isinstance(payload.get("recommendation_followup"), dict) else {},
        dry_run=dry_run or not include_followup,
    )
    followup_execution_written = _write_json(
        followup_execution_output_path,
        payload.get("recommendation_followup_execution") if isinstance(payload.get("recommendation_followup_execution"), dict) else {},
        dry_run=dry_run or not include_followup_execution,
    )
    payload["written"] = written
    payload["audit_written"] = audit_written
    payload["followup_written"] = followup_written
    payload["followup_execution_written"] = followup_execution_written
    payload["writes"] = bool(written or audit_written or followup_written or followup_execution_written)
    payload["summary"]["exported_count"] = expected_exported_count if written else 0
    if include_audit:
        payload["recommendation_audit"]["summary"]["exported_count"] = len(payload["recommendation_audit"].get("items", [])) if audit_written else 0
    if include_followup:
        payload["recommendation_followup"]["summary"]["exported_count"] = len(payload["recommendation_followup"].get("plans", [])) if followup_written else 0
    if include_followup_execution:
        payload["recommendation_followup_execution"]["summary"]["exported_count"] = (
            len(payload["recommendation_followup_execution"].get("actions", [])) if followup_execution_written else 0
        )
    return payload


def build_recommendations_for_topic(
    topic_id: str,
    *,
    limit: int = 5,
    min_confidence: str = "low",
    include_abstract_only: bool = True,
    topic_relevant_unique_count: int | None = None,
    read_only: bool = True,
) -> dict[str, Any]:
    sidecars = _assert_recommendation_read_safe(read_only=read_only)
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    resolved_topic_id = str(profile.get("profile_id") or topic_id).strip()
    reviews = _dedupe_reviews(paper_reviews.list_reviews(topic_id=resolved_topic_id, limit=100_000, read_only=read_only))
    reviews.sort(key=_review_rank_key, reverse=True)
    ai_drafts = _accepted_ai_drafts_for_reviews(resolved_topic_id, reviews, read_only=read_only)
    denominator = int(topic_relevant_unique_count or _topic_relevant_denominator(profile, len(reviews), read_only=read_only))
    full_text_reviewed_count = sum(1 for review in reviews if str(review.get("full_text_status") or "") == "reviewed")
    ai_grounded_count = sum(1 for review in reviews if _draft_for_review(review, ai_drafts) is not None)
    warning = _coverage_warning(full_text_reviewed_count, denominator, ai_grounded_count)

    grouped: dict[str, list[tuple[dict[str, Any], RecommendationEvidenceRef]]] = defaultdict(list)
    all_refs: list[RecommendationEvidenceRef] = []
    for review in reviews:
        ai_draft = _draft_for_review(review, ai_drafts)
        ref = _evidence_ref(review, ai_draft, include_abstract_only=include_abstract_only)
        if ref is None:
            continue
        all_refs.append(ref)
        for theme in _themes_for_review(review, ai_draft):
            grouped[theme].append((review, ref))

    gaps: list[ResearchGapCandidate] = []
    topics: list[NewTopicCandidate] = []
    min_rank = CONFIDENCE_ORDER.get(str(min_confidence or "low"), 0)
    topic_title = str(profile.get("title") or profile.get("topic") or resolved_topic_id)
    for theme in sorted(grouped):
        rows = sorted(
            grouped[theme],
            key=lambda pair: (
                float(pair[0].get("relevance_score") or 0),
                1 if pair[1].review_level == "accepted_ai_grounded" else 0,
                1 if pair[1].review_level == "accepted_ai_grounded_synthetic" else 0,
                1 if pair[1].review_level in {"full_text_reviewed", "deterministic_full_text"} else 0,
                str(pair[0].get("published_at") or ""),
                str(pair[0].get("paper_id") or ""),
            ),
            reverse=True,
        )
        selected = rows[:3]
        refs = [ref for _, ref in selected]
        paper_ids = [str(review.get("paper_id") or "") for review, _ in selected]
        confidence, score = _confidence(refs=refs, supporting_count=len(paper_ids), coverage_warning=warning)
        if CONFIDENCE_ORDER[confidence] < min_rank:
            continue
        theme_label = theme.replace("-", " ")
        gap = ResearchGapCandidate(
            gap_id=f"gap-{_slug(theme)}",
            topic_id=resolved_topic_id,
            title=f"{theme_label.title()} evidence gap",
            gap_statement=(
                f"Stored {topic_title} reviews repeatedly surface {theme_label} signals, "
                "but the current evidence base is too small to make an exhaustive claim."
            ),
            supporting_paper_ids=paper_ids,
            supporting_evidence_refs=refs,
            limitation_refs=refs[:2],
            source_basis=_source_basis(refs),
            confidence=confidence,
            confidence_score=score,
            coverage_warning=warning,
            novelty_rationale=f"Follow-up work should compare how {theme_label} claims hold across better-reviewed papers.",
            risk_notes=[
                "Recommendation is deterministic and evidence-linked, not a comprehensive literature survey.",
                "Abstract-only evidence is treated as weak support.",
            ],
        )
        topic = NewTopicCandidate(
            topic_candidate_id=f"topic-{_slug(theme)}",
            title=f"{theme_label.title()} for {topic_title}",
            problem_statement=gap.gap_statement,
            why_now="The local review store contains repeated signals and explicit limitations, but coverage remains limited.",
            proposed_research_question=f"How can {theme_label} be evaluated with stronger evidence across {topic_title} papers?",
            suggested_method_direction="Start with evidence-linked review expansion, then compare methods using documented limitations and spans.",
            supporting_paper_ids=paper_ids,
            supporting_evidence_refs=refs,
            expected_contribution="A cautious topic candidate for future monitoring and manual validation.",
            nearest_existing_work=paper_ids,
            differentiation_claim="Differentiation is a hypothesis requiring more full-text reviewed evidence before publication-level claims.",
            confidence=confidence,
            confidence_score=score,
            coverage_warning=warning,
            required_follow_up_evidence=[
                "Increase full-text reviewed coverage.",
                "Validate accepted AI-grounded drafts against evidence spans.",
                "Check whether similar gaps appear across newer papers.",
            ],
        )
        gaps.append(gap)
        topics.append(topic)

    gaps.sort(key=lambda item: (item.confidence_score, len(item.supporting_paper_ids), item.gap_id), reverse=True)
    topics.sort(key=lambda item: (item.confidence_score, len(item.supporting_paper_ids), item.topic_candidate_id), reverse=True)
    gaps = gaps[: max(0, int(limit))]
    topics = topics[: max(0, int(limit))]
    used_refs = [ref for candidate in gaps for ref in candidate.supporting_evidence_refs]
    return {
        "mode": "research_recommendation_generation",
        "topic_id": resolved_topic_id,
        "topic_title": topic_title,
        "recommendation_version": "research-recommendation-baseline-v1",
        "summary": _summary(
            resolved_topic_id,
            gaps,
            topics,
            used_refs,
            full_text_reviewed_count=full_text_reviewed_count,
            topic_relevant_unique_count=denominator,
            ai_grounded_count=ai_grounded_count,
        ),
        "gap_candidates": [gap.model_dump() for gap in gaps],
        "new_topic_candidates": [topic.model_dump() for topic in topics],
        "coverage_warning": warning,
        "db_read_only": bool(read_only),
        "sidecar_guard": {"checked": True, "blocked": False, "sidecars": sidecars},
    }


def write_recommendations(
    topic_id: str,
    output_path: str | Path | None,
    *,
    limit: int,
    min_confidence: str = "low",
    include_abstract_only: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    return write_recommendation_outputs(
        topic_id,
        output_path,
        limit=limit,
        min_confidence=min_confidence,
        include_abstract_only=include_abstract_only,
        dry_run=dry_run,
    )

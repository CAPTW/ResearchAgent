from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ConfidenceLabel = Literal["low", "medium", "high"]
ReviewEvidenceLevel = Literal[
    "abstract_only",
    "full_text_reviewed",
    "deterministic_full_text",
    "accepted_ai_grounded",
    "accepted_ai_grounded_synthetic",
]
SourceBasis = Literal[
    "deterministic_review",
    "full_text_evidence",
    "accepted_ai_grounded",
    "accepted_ai_grounded_synthetic",
    "mixed",
]
ExpectedImpact = Literal["low", "medium", "high"]
FollowupExecutionAction = Literal[
    "skip_existing_reviewed",
    "skip_no_pdf_url",
    "skip_blocked",
    "download_extract_review",
    "reuse_existing_artifact",
    "blocked",
]


class RecommendationEvidenceRef(BaseModel):
    paper_id: str
    source_text_sha256: str | None = None
    evidence_span_id: str | None = None
    review_level: ReviewEvidenceLevel
    provider_origin: str | None = None
    quoted_or_summarized_basis: str = Field(min_length=1, max_length=700)


def _strong_evidence_count(refs: list[RecommendationEvidenceRef]) -> int:
    return sum(
        1
        for ref in refs
        if ref.review_level in {"full_text_reviewed", "deterministic_full_text", "accepted_ai_grounded", "accepted_ai_grounded_synthetic"}
    )


def _non_synthetic_strong_evidence_count(refs: list[RecommendationEvidenceRef]) -> int:
    return sum(1 for ref in refs if ref.review_level in {"full_text_reviewed", "deterministic_full_text", "accepted_ai_grounded"})


class ResearchGapCandidate(BaseModel):
    gap_id: str
    topic_id: str
    title: str
    gap_statement: str
    supporting_paper_ids: list[str] = Field(min_length=1)
    supporting_evidence_refs: list[RecommendationEvidenceRef] = Field(min_length=1)
    limitation_refs: list[RecommendationEvidenceRef] = Field(default_factory=list)
    source_basis: SourceBasis
    confidence: ConfidenceLabel
    confidence_score: float = Field(ge=0, le=1)
    coverage_warning: str | None = None
    novelty_rationale: str
    risk_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_evidence_strength(self) -> "ResearchGapCandidate":
        if self.confidence == "high" and _strong_evidence_count(self.supporting_evidence_refs) < 2:
            raise ValueError("high confidence recommendations require multiple strong evidence refs")
        if self.confidence == "high" and _non_synthetic_strong_evidence_count(self.supporting_evidence_refs) == 0:
            raise ValueError("high confidence recommendations require non-synthetic strong evidence")
        return self


class NewTopicCandidate(BaseModel):
    topic_candidate_id: str
    title: str
    problem_statement: str
    why_now: str
    proposed_research_question: str
    suggested_method_direction: str
    supporting_paper_ids: list[str] = Field(min_length=1)
    supporting_evidence_refs: list[RecommendationEvidenceRef] = Field(min_length=1)
    expected_contribution: str
    nearest_existing_work: list[str] = Field(default_factory=list)
    differentiation_claim: str
    confidence: ConfidenceLabel
    confidence_score: float = Field(ge=0, le=1)
    coverage_warning: str | None = None
    required_follow_up_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_evidence_strength(self) -> "NewTopicCandidate":
        if self.confidence == "high" and _strong_evidence_count(self.supporting_evidence_refs) < 2:
            raise ValueError("high confidence topic candidates require multiple strong evidence refs")
        if self.confidence == "high" and _non_synthetic_strong_evidence_count(self.supporting_evidence_refs) == 0:
            raise ValueError("high confidence topic candidates require non-synthetic strong evidence")
        return self


class RecommendationRunSummary(BaseModel):
    topic_id: str
    candidate_gap_count: int = 0
    candidate_topic_count: int = 0
    exported_count: int = 0
    low_confidence_count: int = 0
    medium_confidence_count: int = 0
    high_confidence_count: int = 0
    evidence_paper_count: int = 0
    full_text_evidence_count: int = 0
    accepted_ai_grounded_evidence_count: int = 0
    accepted_ai_grounded_synthetic_evidence_count: int = 0
    abstract_only_evidence_count: int = 0
    coverage_warning_count: int = 0
    full_text_reviewed_count: int = 0
    topic_relevant_unique_count: int = 0
    ai_grounded_accepted_count: int = 0


class RecommendationQualityAuditItem(BaseModel):
    recommendation_id: str
    recommendation_type: Literal["gap", "new_topic"]
    evidence_ref_count: int = 0
    supporting_paper_count: int = 0
    full_text_support_count: int = 0
    ai_grounded_support_count: int = 0
    abstract_only_support_count: int = 0
    missing_evidence_span_count: int = 0
    missing_source_hash_count: int = 0
    confidence: ConfidenceLabel
    confidence_score: float = Field(ge=0, le=1)
    confidence_reason_codes: list[str] = Field(default_factory=list)
    downgrade_reason_codes: list[str] = Field(default_factory=list)
    coverage_warning: str | None = None
    unsupported_or_weak_claim_flags: list[str] = Field(default_factory=list)
    needs_follow_up: bool = False


class RecommendationQualityAuditSummary(BaseModel):
    recommendation_count: int = 0
    weak_recommendation_count: int = 0
    needs_follow_up_count: int = 0
    high_confidence_count: int = 0
    medium_confidence_count: int = 0
    low_confidence_count: int = 0
    average_supporting_paper_count: float = 0.0
    full_text_support_total: int = 0
    ai_grounded_support_total: int = 0
    abstract_only_support_total: int = 0
    coverage_warning_count: int = 0


class RecommendationFollowupCandidate(BaseModel):
    paper_id: str
    title: str
    candidate_reason_codes: list[str] = Field(default_factory=list)
    has_pdf_url: bool = False
    current_full_text_status: str = "unavailable"
    current_ai_grounded_status: str = "none"
    acquisition_priority: int
    expected_confidence_impact: ExpectedImpact
    blocked_reason: str | None = None
    pdf_url: str | None = None
    relevance_score: float = 0.0


class RecommendationFollowupPlanItem(BaseModel):
    recommendation_id: str
    target_gap_or_topic_id: str
    current_confidence: ConfidenceLabel
    desired_next_evidence: list[str] = Field(default_factory=list)
    candidate_paper_ids: list[str] = Field(default_factory=list)
    candidate_titles: list[str] = Field(default_factory=list)
    candidate_reason_codes: list[str] = Field(default_factory=list)
    has_pdf_url: bool = False
    current_full_text_status: str = "none"
    current_ai_grounded_status: str = "none"
    acquisition_priority: int | None = None
    expected_confidence_impact: ExpectedImpact = "low"
    blocked_reason: str | None = None
    candidates: list[RecommendationFollowupCandidate] = Field(default_factory=list)


class RecommendationFollowupPlanSummary(BaseModel):
    recommendation_count: int = 0
    followup_plan_count: int = 0
    followup_candidate_count: int = 0
    blocked_reason_counts: dict[str, int] = Field(default_factory=dict)
    candidate_reason_counts: dict[str, int] = Field(default_factory=dict)
    high_impact_count: int = 0
    medium_impact_count: int = 0
    low_impact_count: int = 0


class RecommendationFollowupExecutionActionItem(BaseModel):
    recommendation_id: str
    target_gap_or_topic_id: str
    paper_id: str
    normalized_paper_id: str | None = None
    title: str
    pdf_url: str | None = None
    pdf_url_domain: str | None = None
    allowed_domain_match: bool = False
    current_full_text_status: str = "unavailable"
    current_review_status: str = "unreviewed"
    acquisition_priority: int
    expected_confidence_impact: ExpectedImpact = "low"
    planned_action: FollowupExecutionAction
    reason_codes: list[str] = Field(default_factory=list)
    relevance_score: float = 0.0
    source_provider: str | None = None
    local_artifact_path: str | None = None
    previous_acquisition_status: str | None = None
    source_text_sha256: str | None = None
    evidence_span_count: int = 0


class RecommendationFollowupExecutionSummary(BaseModel):
    planned_followup_count: int = 0
    skipped_existing_reviewed_count: int = 0
    skipped_no_pdf_url_count: int = 0
    skipped_blocked_count: int = 0
    downloaded_count: int = 0
    extracted_count: int = 0
    reviewed_from_followup_count: int = 0
    rejected_count_by_reason: dict[str, int] = Field(default_factory=dict)
    failed_count_by_reason: dict[str, int] = Field(default_factory=dict)
    extraction_failed_count_by_reason: dict[str, int] = Field(default_factory=dict)
    full_text_reviewed_count_before: int = 0
    full_text_reviewed_count_after: int = 0


class RecommendationQualityComparison(BaseModel):
    new_reviews_created: bool = False
    full_text_reviewed_count_before: int = 0
    full_text_reviewed_count_after: int = 0
    ai_grounded_accepted_count_before: int = 0
    ai_grounded_accepted_count_after: int = 0
    weak_recommendation_count_before: int = 0
    weak_recommendation_count_after: int = 0
    needs_follow_up_count_before: int = 0
    needs_follow_up_count_after: int = 0
    confidence_distribution_before: dict[str, int] = Field(default_factory=dict)
    confidence_distribution_after: dict[str, int] = Field(default_factory=dict)
    evidence_ref_count_before: int = 0
    evidence_ref_count_after: int = 0
    recommendations_improved: list[str] = Field(default_factory=list)
    recommendations_unchanged: list[str] = Field(default_factory=list)
    recommendations_still_blocked: list[str] = Field(default_factory=list)

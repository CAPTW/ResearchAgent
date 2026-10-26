from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AIReviewEvidenceSpan(BaseModel):
    span_id: str
    label: str | None = None
    text: str
    source_path: str | None = None
    page_number: int | None = None
    char_start: int | None = None
    char_end: int | None = None


class AIReviewTextExcerpt(BaseModel):
    source: str
    text: str
    char_count: int


class AIReviewPacket(BaseModel):
    packet_version: str = "ai-review-packet-v1"
    paper_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    published_at: str | None = None
    topic_id: str
    topic_name: str
    relevance_score: float = 0.0
    abstract: str | None = None
    full_text_status: str
    review_level: Literal["abstract_only", "full_text_grounded"]
    source_text_sha256: str | None = None
    extraction_quality: dict = Field(default_factory=dict)
    evidence_spans: list[AIReviewEvidenceSpan] = Field(default_factory=list)
    selected_text_excerpts: list[AIReviewTextExcerpt] = Field(default_factory=list)
    existing_review: dict = Field(default_factory=dict)
    citation_metadata: dict = Field(default_factory=dict)
    provider_output_contract: dict = Field(default_factory=dict)


class AIReviewDraft(BaseModel):
    paper_id: str
    source_text_sha256: str | None = None
    contribution_summary: str
    method_summary: str
    evidence_summary: str
    limitations: list[str] = Field(default_factory=list)
    relevance_to_topic: str
    novelty_signal: str
    practical_implications: str
    follow_up_questions: list[str] = Field(default_factory=list)
    recommended_tags: list[str] = Field(default_factory=list)
    cited_evidence_span_ids: list[str] = Field(default_factory=list)
    confidence: float
    unsupported_claims: list[str] = Field(default_factory=list)
    review_level: Literal["abstract_only", "full_text_grounded"]

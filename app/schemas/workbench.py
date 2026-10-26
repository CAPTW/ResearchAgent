from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceLocationResponse(BaseModel):
    page: int | None = None
    section: str | None = None
    reference: str | None = None


class EvidenceItemResponse(BaseModel):
    evidence_id: str
    source_id: str | None = None
    paper_id: str | None = None
    title: str | None = None
    source_type: str
    snippet: str
    summary: str | None = None
    location: EvidenceLocationResponse = Field(default_factory=EvidenceLocationResponse)
    quality_hint: str | None = None
    confidence: float | None = None
    artifact_path: str | None = None
    citation_text: str | None = None


class ReviewArtifactsResponse(BaseModel):
    review_json: str | None = None
    note_markdown: str | None = None
    pdf: str | None = None
    text_txt: str | None = None
    pages_json: str | None = None


class ReviewResponse(BaseModel):
    run_id: str
    paper_id: str
    status: Literal["complete", "partial", "missing"] = "missing"
    completeness: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)
    title: str = ""
    theme: str | None = None
    one_liner: str | None = None
    why_it_matters: list[str] = Field(default_factory=list)
    key_contributions: list[str] = Field(default_factory=list)
    xr_takeaways: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    lab_actions: list[str] = Field(default_factory=list)
    confidence: float | None = None
    method: dict[str, str | list[str]] = Field(default_factory=dict)
    experiments: dict[str, str | list[str]] = Field(default_factory=dict)
    evidence: list[EvidenceItemResponse] = Field(default_factory=list)
    artifacts: ReviewArtifactsResponse = Field(default_factory=ReviewArtifactsResponse)
    raw_path: str | None = None


class ScoreBreakdownResponse(BaseModel):
    primary_hits: list[str] = Field(default_factory=list)
    secondary_hits: list[str] = Field(default_factory=list)
    negative_hits: list[str] = Field(default_factory=list)
    primary_points: int = 0
    secondary_points: int = 0
    negative_points: int = 0
    xr_bonus: int = 0
    realtime_bonus: int = 0
    pdf_bonus: int = 0
    final_score: int = 0


class DedupClusterResponse(BaseModel):
    cluster_id: str
    cluster_size: int = 1
    is_primary: bool = True
    primary_paper_id: str | None = None
    members: list[str] = Field(default_factory=list)


class PaperArtifactsResponse(BaseModel):
    note_markdown: str | None = None
    review_json: str | None = None
    pdf: str | None = None
    text_txt: str | None = None
    pages_json: str | None = None


class PaperReviewActionResponse(BaseModel):
    decision: Literal["unreviewed", "keep", "dismiss", "save"] = "unreviewed"
    pinned: bool = False
    note: str = ""
    updated_at: str | None = None


class PaperReviewActionUpdateRequest(BaseModel):
    decision: Literal["unreviewed", "keep", "dismiss", "save"] | None = None
    pinned: bool | None = None
    note: str | None = None


class PaperListItemResponse(BaseModel):
    paper_id: str
    title: str
    source_provider: str | None = None
    score: int = 0
    score_band: Literal["high", "medium", "low"] = "low"
    theme: str = "Uncategorized"
    published: str | None = None
    entry_url: str | None = None
    pdf_url: str | None = None
    status: str = "unreviewed"
    abstract_excerpt: str = ""
    score_breakdown: ScoreBreakdownResponse = Field(default_factory=ScoreBreakdownResponse)
    dedup: DedupClusterResponse
    review_action: PaperReviewActionResponse = Field(default_factory=PaperReviewActionResponse)
    # Ranking emits both textual hit lists and numeric score components.
    # Preserve that structured JSON shape instead of rejecting valid runs at
    # the API response boundary.
    triage: dict[str, Any] = Field(default_factory=dict)
    artifacts: PaperArtifactsResponse = Field(default_factory=PaperArtifactsResponse)


class CountOptionResponse(BaseModel):
    value: str
    count: int


class PaperFacetsResponse(BaseModel):
    themes: list[CountOptionResponse] = Field(default_factory=list)
    statuses: list[CountOptionResponse] = Field(default_factory=list)
    score_bands: list[CountOptionResponse] = Field(default_factory=list)
    providers: list[CountOptionResponse] = Field(default_factory=list)
    duplicate_clusters: int = 0


class PaperListResponse(BaseModel):
    run_id: str
    items: list[PaperListItemResponse]
    page: int = 1
    page_size: int = 25
    total: int = 0
    total_pages: int = 0
    q: str = ""
    status: str | None = None
    theme: str | None = None
    score_band: str | None = None
    provider: str | None = None
    dedup: str = "all"
    facets: PaperFacetsResponse = Field(default_factory=PaperFacetsResponse)


class RelatedPaperResponse(BaseModel):
    paper_id: str
    title: str
    score: int = 0
    theme: str = "Uncategorized"
    relation: str = "related"


class PaperDetailResponse(BaseModel):
    run_id: str
    paper: PaperListItemResponse
    abstract: str = ""
    note_content: str | None = None
    note_path: str | None = None
    ranking_explanation: list[str] = Field(default_factory=list)
    related_papers: list[RelatedPaperResponse] = Field(default_factory=list)


class OutlineSectionResponse(BaseModel):
    section_id: str
    title: str
    kind: Literal["markdown", "papers"] = "markdown"
    body_markdown: str = ""
    paper_ids: list[str] = Field(default_factory=list)
    compare_notes: list[str] = Field(default_factory=list)
    provenance: list[dict[str, str | list[str] | None]] = Field(default_factory=list)
    claims: list[dict[str, object]] = Field(default_factory=list)


class OutlineResponse(BaseModel):
    run_id: str
    path: str
    updated_at: str
    sections: list[OutlineSectionResponse]
    applied_actions: list[dict[str, str | list[str] | None]] = Field(default_factory=list)


class OutlineUpdateRequest(BaseModel):
    sections: list[OutlineSectionResponse]


class PreviewResponse(BaseModel):
    run_id: str
    path: str
    content: str
    generated_at: str
    quality: dict[str, Any] | None = None


class ExportArtifactResponse(BaseModel):
    export_id: str
    artifact_id: str | None = None
    report_id: str | None = None
    version_id: str | None = None
    topic_id: str | None = None
    run_id: str | None = None
    format: str
    mime_type: str | None = None
    file_name: str | None = None
    output: str
    storage_path: str | None = None
    created_at: str
    size_bytes: int | None = None
    sha256: str | None = None
    source_markdown_version_id: str | None = None
    source_markdown_hash: str | None = None
    renderer: str | None = None
    render_options: dict[str, object] = Field(default_factory=dict)
    status: Literal["ready", "completed", "failed", "unavailable"] = "completed"
    error_message: str | None = None
    immutable: bool = False
    provenance_summary: dict[str, object] = Field(default_factory=dict)


class ExportFormatCapabilityResponse(BaseModel):
    format: str
    available: bool = True
    status: Literal["available", "unavailable", "degraded"] = "available"
    reason: str | None = None
    renderer: str | None = None
    template_id: str | None = None
    meaning: str | None = None
    source_pdf_handling: str | None = None


class ExportListResponse(BaseModel):
    run_id: str
    items: list[ExportArtifactResponse] = Field(default_factory=list)
    capabilities: dict[str, ExportFormatCapabilityResponse] = Field(default_factory=dict)


class CitationTraceItemResponse(BaseModel):
    citation_id: str
    section_id: str
    section_title: str
    claim_id: str | None = None
    claim_text: str | None = None
    evidence_id: str | None = None
    source_id: str | None = None
    paper_id: str | None = None
    paper_title: str | None = None
    artifact_path: str | None = None
    citation_text: str | None = None
    snippet: str | None = None
    provenance: dict[str, object] = Field(default_factory=dict)


class CitationTraceResponse(BaseModel):
    run_id: str
    generated_at: str
    source_outline_path: str | None = None
    source_preview_path: str | None = None
    items: list[CitationTraceItemResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

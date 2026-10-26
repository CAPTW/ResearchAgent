from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.runs import RunCreateRequest


class McpSearchPapersRequest(BaseModel):
    run_id: str
    q: str = ""
    status: str | None = None
    theme: str | None = None
    score_band: str | None = None
    dedup: str = "all"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)


class McpPaperSummaryResponse(BaseModel):
    paper_id: str
    title: str
    score: int = 0
    score_band: str = "low"
    theme: str = "Uncategorized"
    published: str | None = None
    entry_url: str | None = None
    pdf_url: str | None = None
    status: str = "unreviewed"
    abstract_excerpt: str = ""


class McpSearchPapersResponse(BaseModel):
    run_id: str
    total: int = 0
    page: int = 1
    page_size: int = 25
    query: str = ""
    items: list[McpPaperSummaryResponse] = Field(default_factory=list)


class McpFetchPaperDetailsRequest(BaseModel):
    run_id: str
    paper_id: str
    include_review: bool = True


class McpRelatedPaperResponse(BaseModel):
    paper_id: str
    title: str
    score: int = 0
    theme: str = "Uncategorized"
    relation: str = "related"


class McpReviewSummaryResponse(BaseModel):
    status: str = "missing"
    completeness: float = 0.0
    one_liner: str | None = None
    why_it_matters: list[str] = Field(default_factory=list)
    key_contributions: list[str] = Field(default_factory=list)
    xr_takeaways: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    lab_actions: list[str] = Field(default_factory=list)
    confidence: float | None = None
    evidence_count: int = 0


class McpPaperDetailsResponse(BaseModel):
    run_id: str
    paper: McpPaperSummaryResponse
    abstract: str = ""
    note_available: bool = False
    ranking_explanation: list[str] = Field(default_factory=list)
    related_papers: list[McpRelatedPaperResponse] = Field(default_factory=list)
    review: McpReviewSummaryResponse | None = None


class McpGetRunStatusRequest(BaseModel):
    run_id: str


class McpRunStatusResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    rerun_context: dict[str, Any] = Field(default_factory=dict)


class McpOutlineSectionSummaryResponse(BaseModel):
    section_id: str
    title: str
    kind: str
    paper_ids: list[str] = Field(default_factory=list)
    compare_notes_count: int = 0


class McpGetReportPreviewRequest(BaseModel):
    run_id: str


class McpReportPreviewResponse(BaseModel):
    run_id: str
    preview_content: str
    generated_at: str
    sections: list[McpOutlineSectionSummaryResponse] = Field(default_factory=list)


class McpStartResearchRunRequest(RunCreateRequest):
    pass


class McpStartResearchRunResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str

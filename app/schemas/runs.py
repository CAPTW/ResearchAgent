from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SourceProviderLiteral = Literal["arxiv", "openalex", "crossref"]


class RunCreateRequest(BaseModel):
    topic: str = Field(default="XR/AR × ML (Image/Video)")
    queries: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    max_results: int | None = Field(default=None, ge=1, le=200)
    source_provider: SourceProviderLiteral = "arxiv"
    arxiv_sort_by: Literal["submittedDate", "lastUpdatedDate", "relevance"] = "submittedDate"
    arxiv_sort_order: Literal["ascending", "descending"] = "descending"
    source_settings: dict[str, Any] = Field(default_factory=dict)
    top_papers: int = Field(default=3, ge=1, le=20)
    weekly_top: int = Field(default=15, ge=1, le=50)
    skip_scout: bool = False
    skip_summary: bool = False
    skip_synthesis: bool = False
    experiment: bool = Field(
        default=False,
        description=(
            "Generate a non-executable research design proposal scaffold. "
            "This does not run or validate an experiment."
        ),
    )
    rank: int = Field(default=1, ge=1, le=50)
    acquisition_language: str = "any"
    review_report_language: str = "en"
    acquisition_start_date: str | None = None
    acquisition_end_date: str | None = None


class RunExportRequest(BaseModel):
    format: Literal["report_markdown", "report_pdf", "journal_pdf"] = "report_markdown"

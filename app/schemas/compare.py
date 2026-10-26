from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


LineageMode = Literal[
    "standalone",
    "full_rerun_from_profile",
    "branch_from_selected_papers",
    "rerun_with_profile_overrides",
]
RerunMode = Literal[
    "full_rerun_from_profile",
    "branch_from_selected_papers",
    "rerun_with_profile_overrides",
]
SelectionSource = Literal["manual", "compare_bucket", "preset", "suggested"]
CompareView = Literal["papers", "themes", "report", "exports", "lineage", "rerun", "preset"]


class RunLineageRecordResponse(BaseModel):
    parent_run_id: str | None = None
    root_run_id: str
    lineage_mode: LineageMode = "standalone"
    source_paper_ids: list[str] = Field(default_factory=list)
    created_from_profile_snapshot: bool = False
    compare_baseline_run_id: str | None = None
    branch_reason: str | None = None
    created_at: str
    notes: str = ""


class LineageRunSummaryResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str
    finished_at: str | None = None
    lineage_mode: LineageMode = "standalone"


class RunLineageResponse(BaseModel):
    run_id: str
    lineage: RunLineageRecordResponse
    parent: LineageRunSummaryResponse | None = None
    root: LineageRunSummaryResponse
    children: list[LineageRunSummaryResponse] = Field(default_factory=list)
    same_root_runs: list[LineageRunSummaryResponse] = Field(default_factory=list)


class CompareCandidateResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str
    triage_items: int = 0
    compare_ready_available: bool = False
    relation: str = "unrelated"
    same_root: bool = False
    preview_hash: str | None = None
    parent_run_id: str | None = None
    root_run_id: str


class CompareCandidatesResponse(BaseModel):
    run_id: str
    items: list[CompareCandidateResponse] = Field(default_factory=list)


class RunCompareSideResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    triage_items: int = 0
    reviewed_items: int = 0
    preview_hash: str | None = None
    preview_hash_source: str | None = None
    outline_sections: int = 0
    exports: list[str] = Field(default_factory=list)
    lineage: RunLineageRecordResponse


class PaperDiffItemResponse(BaseModel):
    paper_id: str
    title: str
    theme: str = "Uncategorized"
    status: Literal["added", "removed", "common"] = "common"
    left_score: int | None = None
    right_score: int | None = None
    score_delta: int = 0
    left_rank: int | None = None
    right_rank: int | None = None
    rank_delta: int | None = None
    changed: bool = False
    present_in_left: bool = False
    present_in_right: bool = False


class ThemeDeltaResponse(BaseModel):
    theme: str
    left_count: int = 0
    right_count: int = 0
    delta: int = 0


class ScoreDistributionDeltaResponse(BaseModel):
    band: Literal["high", "medium", "low", "unscored"]
    left_count: int = 0
    right_count: int = 0
    delta: int = 0


class KeywordMovementResponse(BaseModel):
    keyword: str
    left_count: int = 0
    right_count: int = 0
    delta: int = 0
    left_sample_paper_ids: list[str] = Field(default_factory=list)
    right_sample_paper_ids: list[str] = Field(default_factory=list)


class ProviderContributionDeltaResponse(BaseModel):
    provider: str
    left_count: int = 0
    right_count: int = 0
    delta: int = 0
    left_sample_paper_ids: list[str] = Field(default_factory=list)
    right_sample_paper_ids: list[str] = Field(default_factory=list)


class CompareReportStatusResponse(BaseModel):
    left_available: bool = False
    right_available: bool = False
    left_status: str = "missing"
    right_status: str = "missing"
    left_updated_at: str | None = None
    right_updated_at: str | None = None
    left_artifact_path: str | None = None
    right_artifact_path: str | None = None


class CompareProvenanceResponse(BaseModel):
    source_run_ids: list[str] = Field(default_factory=list)
    computed_at: str
    definitions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OutlineSectionSummaryResponse(BaseModel):
    section_id: str
    title: str
    kind: str = "markdown"
    paper_ids: list[str] = Field(default_factory=list)
    body_hash: str | None = None


class OutlineSectionDeltaResponse(BaseModel):
    section_id: str
    title: str
    status: Literal["added", "removed", "changed", "unchanged"] = "unchanged"
    left_kind: str | None = None
    right_kind: str | None = None
    left_paper_count: int = 0
    right_paper_count: int = 0


class ExportFormatDiffResponse(BaseModel):
    format: str
    left_available: bool = False
    right_available: bool = False
    left_status: str | None = None
    right_status: str | None = None


class CompareSummaryResponse(BaseModel):
    relation: str
    same_root: bool = False
    preview_changed: bool | None = None
    added_papers: int = 0
    removed_papers: int = 0
    common_papers: int = 0
    changed_common_papers: int = 0
    unchanged_common_papers: int = 0
    theme_delta_count: int = 0
    outline_changed: bool = False


class PaperDiffGroupsResponse(BaseModel):
    added: list[PaperDiffItemResponse] = Field(default_factory=list)
    removed: list[PaperDiffItemResponse] = Field(default_factory=list)
    common: list[PaperDiffItemResponse] = Field(default_factory=list)


class CompareFilterContextResponse(BaseModel):
    paper_query: str | None = None
    theme: str | None = None
    status: str | None = None
    source_side: Literal["left", "right", "both"] = "both"
    bucket_id: str | None = None
    bucket_label: str | None = None


class CompareRerunBucketResponse(BaseModel):
    bucket_id: str
    label: str
    description: str
    source_run_id: str
    compare_partner_run_id: str
    mode: RerunMode = "branch_from_selected_papers"
    selected_paper_ids: list[str] = Field(default_factory=list)
    selection_count: int = 0
    available: bool = True
    empty_reason: str | None = None
    suggested_note: str | None = None
    profile_overrides: "RunProfileOverridesRequest | None" = None
    compare_filters: CompareFilterContextResponse | None = None


class ArtifactFallbackResponse(BaseModel):
    run_side: Literal["left", "right"]
    artifact_kind: str
    available: bool = False
    used: bool = False
    path: str | None = None
    reason: str | None = None


class CompareReportSummaryDiffResponse(BaseModel):
    left_source: str | None = None
    right_source: str | None = None
    left_hash: str | None = None
    right_hash: str | None = None
    hash_changed: bool | None = None
    completeness: Literal["full", "partial", "outline_only", "metadata_only", "unavailable"] = "unavailable"
    confidence: float = 0.0
    added_sections: int = 0
    removed_sections: int = 0
    changed_sections: int = 0
    unchanged_sections: int = 0
    order_changed_sections: int = 0
    citation_delta_count: int = 0
    missing_explanations: list[str] = Field(default_factory=list)


class ReportSectionDiffResponse(BaseModel):
    section_key: str
    display_title: str
    status: Literal["added", "removed", "changed", "unchanged"] = "unchanged"
    left_title: str | None = None
    right_title: str | None = None
    left_order: int | None = None
    right_order: int | None = None
    title_changed: bool = False
    order_changed: bool = False
    body_changed: bool = False
    body_similarity: float | None = None
    left_excerpt: str | None = None
    right_excerpt: str | None = None
    left_citation_count: int = 0
    right_citation_count: int = 0
    citation_delta_count: int = 0
    left_paper_ids: list[str] = Field(default_factory=list)
    right_paper_ids: list[str] = Field(default_factory=list)
    change_summary: list[str] = Field(default_factory=list)


class CitationDiffResponse(BaseModel):
    identifier: str
    kind: Literal["paper_id", "url", "reference"]
    status: Literal["added", "removed", "changed", "unchanged"] = "unchanged"
    left_count: int = 0
    right_count: int = 0
    delta: int = 0
    left_sections: list[str] = Field(default_factory=list)
    right_sections: list[str] = Field(default_factory=list)


class CompareReportResponse(BaseModel):
    left_outline: list[OutlineSectionSummaryResponse] = Field(default_factory=list)
    right_outline: list[OutlineSectionSummaryResponse] = Field(default_factory=list)
    outline_deltas: list[OutlineSectionDeltaResponse] = Field(default_factory=list)
    left_preview_hash: str | None = None
    right_preview_hash: str | None = None
    left_preview_hash_source: str | None = None
    right_preview_hash_source: str | None = None
    report_summary_diff: CompareReportSummaryDiffResponse = Field(default_factory=CompareReportSummaryDiffResponse)
    artifact_fallbacks: list[ArtifactFallbackResponse] = Field(default_factory=list)


class CompareReportDetailResponse(BaseModel):
    left_run_id: str
    right_run_id: str
    report_summary_diff: CompareReportSummaryDiffResponse
    section_diffs: list[ReportSectionDiffResponse] = Field(default_factory=list)
    citation_diffs: list[CitationDiffResponse] = Field(default_factory=list)
    export_diffs: list[ExportFormatDiffResponse] = Field(default_factory=list)
    artifact_fallbacks: list[ArtifactFallbackResponse] = Field(default_factory=list)


class CompareContextSummaryResponse(BaseModel):
    source_run_id: str
    compare_partner_run_id: str | None = None
    relation: str | None = None
    same_root: bool = False


class CompareContextBannerResponse(BaseModel):
    title: str
    message: str
    relation: str | None = None
    bucket_label: str | None = None
    preset_name: str | None = None
    focus_theme: str | None = None
    focus_section_title: str | None = None


class CompareContextSectionSeedResponse(BaseModel):
    section_id: str
    section_title: str
    source_side: Literal["left", "right"]
    available: bool = False
    matched_outline_section_id: str | None = None
    body_markdown: str | None = None
    excerpt: str | None = None
    paper_ids: list[str] = Field(default_factory=list)
    change_summary: list[str] = Field(default_factory=list)


class RunCompareContextResponse(BaseModel):
    run_id: str
    editing_session_id: str | None = None
    editing_session_title: str | None = None
    editing_session_stale: bool = False
    workspace_view_id: str | None = None
    workspace_view_title: str | None = None
    compare_partner_run_id: str | None = None
    compare_mode: str | None = None
    compare_view: CompareView | None = None
    selection_bucket_id: str | None = None
    preset_id: str | None = None
    selection_source: SelectionSource | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    active_pane: Literal["paper", "report"] = "paper"
    effective_filters: CompareFilterContextResponse = Field(default_factory=CompareFilterContextResponse)
    selected_paper_ids: list[str] = Field(default_factory=list)
    focus_paper_id: str | None = None
    focus_theme: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    banner: CompareContextBannerResponse | None = None
    summary: CompareContextSummaryResponse
    pending_section_seed: CompareContextSectionSeedResponse | None = None
    warnings: list[str] = Field(default_factory=list)


class CompareLineageBundleResponse(BaseModel):
    relation: str
    same_root: bool = False
    left: RunLineageResponse
    right: RunLineageResponse


class RunCompareResponse(BaseModel):
    left: RunCompareSideResponse
    right: RunCompareSideResponse
    summary: CompareSummaryResponse
    papers: PaperDiffGroupsResponse
    themes: list[ThemeDeltaResponse] = Field(default_factory=list)
    score_distribution: list[ScoreDistributionDeltaResponse] = Field(default_factory=list)
    keyword_movement: list[KeywordMovementResponse] = Field(default_factory=list)
    provider_contribution: list[ProviderContributionDeltaResponse] = Field(default_factory=list)
    report_status: CompareReportStatusResponse
    report: CompareReportResponse
    exports: list[ExportFormatDiffResponse] = Field(default_factory=list)
    lineage: CompareLineageBundleResponse
    rerun_buckets: list[CompareRerunBucketResponse] = Field(default_factory=list)
    provenance: CompareProvenanceResponse


class RunProfileOverridesRequest(BaseModel):
    topic: str | None = None
    queries: list[str] | None = None
    categories: list[str] | None = None
    max_results: int | None = Field(default=None, ge=1, le=200)
    top_papers: int | None = Field(default=None, ge=1, le=20)
    weekly_top: int | None = Field(default=None, ge=1, le=50)
    skip_summary: bool | None = None
    skip_synthesis: bool | None = None
    experiment: bool | None = None
    rank: int | None = Field(default=None, ge=1, le=50)


class RunRerunRequest(BaseModel):
    mode: RerunMode
    selected_paper_ids: list[str] = Field(default_factory=list)
    profile_overrides: RunProfileOverridesRequest | None = None
    note: str | None = None
    compare_baseline_run_id: str | None = None
    skip_retrieval_when_branching: bool = True
    preset_id: str | None = None
    selection_bucket_id: str | None = None
    selection_bucket_label: str | None = None
    selection_source: SelectionSource | None = None


class RunRerunContextResponse(BaseModel):
    compare_baseline_run_id: str | None = None
    preset_id: str | None = None
    selection_source: SelectionSource | None = None
    selection_bucket_id: str | None = None
    selection_bucket_label: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)


class RunRerunResponse(BaseModel):
    child_run_id: str
    status: str
    lineage: RunLineageRecordResponse
    execution_plan: list[str] = Field(default_factory=list)
    message: str
    rerun_context: RunRerunContextResponse | None = None


class RerunPresetLineageHintResponse(BaseModel):
    compare_baseline_run_id: str | None = None
    selection_source: SelectionSource | None = None
    selection_bucket_id: str | None = None
    selection_bucket_label: str | None = None


class RerunPresetResponse(BaseModel):
    preset_id: str
    source_run_id: str
    compare_partner_run_id: str | None = None
    preset_name: str
    preset_type: Literal["compare_bucket", "manual_selection", "profile_override", "full_rerun"] = "manual_selection"
    mode: RerunMode = "branch_from_selected_papers"
    selected_paper_ids: list[str] = Field(default_factory=list)
    profile_overrides: RunProfileOverridesRequest | None = None
    compare_filters: CompareFilterContextResponse | None = None
    note: str | None = None
    created_at: str
    created_from_compare: bool = False
    lineage_hint: RerunPresetLineageHintResponse = Field(default_factory=RerunPresetLineageHintResponse)


class RerunPresetCreateRequest(BaseModel):
    preset_name: str = Field(min_length=1, max_length=120)
    preset_type: Literal["compare_bucket", "manual_selection", "profile_override", "full_rerun"] = "manual_selection"
    mode: RerunMode = "branch_from_selected_papers"
    compare_partner_run_id: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)
    profile_overrides: RunProfileOverridesRequest | None = None
    compare_filters: CompareFilterContextResponse | None = None
    note: str | None = None
    created_from_compare: bool = False
    lineage_hint: RerunPresetLineageHintResponse | None = None


class RerunPresetListResponse(BaseModel):
    run_id: str
    items: list[RerunPresetResponse] = Field(default_factory=list)


class RerunPresetDeleteResponse(BaseModel):
    run_id: str
    preset_id: str
    status: Literal["deleted"] = "deleted"

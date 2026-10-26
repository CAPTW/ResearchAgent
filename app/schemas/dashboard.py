from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import AuthFoundationStatusResponse
from app.schemas.recommendations import RecommendationEvidenceRef

SourceProviderLiteral = Literal["arxiv", "openalex", "crossref"]
SourceProviderSortLiteral = Literal[
    "submittedDate",
    "lastUpdatedDate",
    "relevance",
    "publication_date",
    "cited_by_count",
    "published",
    "deposited",
    "is-referenced-by-count",
]


class DashboardCountResponse(BaseModel):
    runs: int = 0
    topics: int = 0
    papers: int = 0
    evidence: int = 0
    report_sections: int = 0


class StatusCountResponse(BaseModel):
    status: str
    count: int


class TopicCountResponse(BaseModel):
    topic: str
    count: int


class ThemeCountResponse(BaseModel):
    theme: str
    count: int


class DashboardRunSummaryResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str
    updated_at: str
    triage_items: int = 0
    reviewed_items: int = 0
    evidence_count: int = 0
    claim_count: int = 0


class DashboardPaperSummaryResponse(BaseModel):
    run_id: str
    paper_id: str
    title: str
    theme: str
    score: int = 0
    status: str
    published: str | None = None


def _default_arxiv_source_provider_config() -> dict[str, Any]:
    return {
        "provider": "arxiv",
        "enabled": True,
        "priority": 1,
        "queries": [],
        "categories": [],
        "maxResults": 60,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }


class TopicSourceProviderConfigResponse(BaseModel):
    provider: str = "arxiv"
    enabled: bool = True
    priority: int = Field(default=1, ge=1, le=10)
    queries: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    maxResults: int = Field(default=60, ge=1, le=200)
    sortBy: SourceProviderSortLiteral = "submittedDate"
    sortOrder: Literal["ascending", "descending"] = "descending"


class TopicSourceSettingsResponse(BaseModel):
    defaultProvider: str = "arxiv"
    enabledProviders: list[str] = Field(default_factory=lambda: ["arxiv"])
    providers: dict[str, TopicSourceProviderConfigResponse] = Field(
        default_factory=lambda: {"arxiv": TopicSourceProviderConfigResponse(**_default_arxiv_source_provider_config())}
    )


class TopicSourceStatusResponse(BaseModel):
    provider: str = "arxiv"
    label: str = "arXiv"
    displayName: str | None = None
    description: str | None = None
    category: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    supported: bool = True
    available: bool = True
    configured: bool = True
    state: Literal["available", "unavailable_missing_env", "unavailable_not_implemented", "degraded"] = "available"
    enabled: bool = True
    default: bool = True
    priority: int = 1
    requiresCredentials: bool = False
    statusLabel: str = "사용 가능"
    unavailableReason: str | None = None
    supportedQueryFields: list[str] = Field(default_factory=list)
    lastCheckedAt: str | None = None
    lastSuccessfulCollectionAt: str | None = None


class ApiSourceContributionResponse(BaseModel):
    provider: str = "arxiv"
    label: str = "arXiv"
    paperCount: int = 0
    lastSuccessfulCollectionAt: str | None = None


class ApiSourceSummaryResponse(BaseModel):
    defaultProvider: str = "arxiv"
    enabledProviders: list[str] = Field(default_factory=lambda: ["arxiv"])
    statuses: list[TopicSourceStatusResponse] = Field(default_factory=list)
    recentContributions: list[ApiSourceContributionResponse] = Field(default_factory=list)


class SourceProviderRegistryItemResponse(BaseModel):
    provider: str
    label: str
    displayName: str
    description: str
    category: str
    capabilities: dict[str, bool] = Field(default_factory=dict)
    requiresCredentials: bool = False
    configured: bool = False
    supported: bool = False
    available: bool = False
    state: Literal["available", "unavailable_missing_env", "unavailable_not_implemented", "degraded"] = "unavailable_not_implemented"
    statusLabel: str
    unavailableReason: str | None = None
    supportedQueryFields: list[str] = Field(default_factory=list)
    lastCheckedAt: str | None = None


class SourceProviderRegistryResponse(BaseModel):
    checkedAt: str
    items: list[SourceProviderRegistryItemResponse] = Field(default_factory=list)


class DashboardOverviewResponse(BaseModel):
    totals: DashboardCountResponse = Field(default_factory=DashboardCountResponse)
    status_counts: list[StatusCountResponse] = Field(default_factory=list)
    top_topics: list[TopicCountResponse] = Field(default_factory=list)
    top_themes: list[ThemeCountResponse] = Field(default_factory=list)
    recent_runs: list[DashboardRunSummaryResponse] = Field(default_factory=list)
    index_state: dict[str, str] = Field(default_factory=dict)


class DashboardRunSearchResponse(BaseModel):
    items: list[DashboardRunSummaryResponse] = Field(default_factory=list)


class DashboardPaperSearchResponse(BaseModel):
    items: list[DashboardPaperSummaryResponse] = Field(default_factory=list)


class TopicProfileResponse(BaseModel):
    profile_id: str
    title: str
    description: str | None = None
    topic: str
    queries: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    max_results: int = 60
    source_provider: SourceProviderLiteral = "arxiv"
    arxiv_sort_by: Literal["submittedDate", "lastUpdatedDate", "relevance"] = "submittedDate"
    arxiv_sort_order: Literal["ascending", "descending"] = "descending"
    source_settings: TopicSourceSettingsResponse = Field(default_factory=TopicSourceSettingsResponse)
    source_statuses: list[TopicSourceStatusResponse] = Field(default_factory=list)
    top_papers: int = 3
    weekly_top: int = 15
    is_favorite: bool = False
    status: Literal["active", "archived"] = "active"
    archived_at: str | None = None
    default_collection_cadence: str = "daily_0900"
    default_report_cadence: str = "weekly_monday_1000"
    acquisition_language: str = "any"
    review_report_language: str = "en"
    acquisition_start_date: str | None = None
    acquisition_end_date: str | None = None
    created_at: str
    updated_at: str


class TopicProfilesResponse(BaseModel):
    items: list[TopicProfileResponse] = Field(default_factory=list)


class TopicProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    topic: str
    queries: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    max_results: int = Field(default=60, ge=1, le=200)
    source_provider: SourceProviderLiteral = "arxiv"
    arxiv_sort_by: Literal["submittedDate", "lastUpdatedDate", "relevance"] = "submittedDate"
    arxiv_sort_order: Literal["ascending", "descending"] = "descending"
    source_settings: TopicSourceSettingsResponse | None = None
    top_papers: int = Field(default=3, ge=1, le=20)
    weekly_top: int = Field(default=15, ge=1, le=50)
    is_favorite: bool = False
    default_collection_cadence: str = "daily_0900"
    default_report_cadence: str = "weekly_monday_1000"
    acquisition_language: str = "any"
    review_report_language: str = "en"
    acquisition_start_date: str | None = None
    acquisition_end_date: str | None = None


class TopicProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    topic: str | None = None
    queries: list[str] | None = None
    categories: list[str] | None = None
    max_results: int | None = Field(default=None, ge=1, le=200)
    source_provider: SourceProviderLiteral | None = None
    arxiv_sort_by: Literal["submittedDate", "lastUpdatedDate", "relevance"] | None = None
    arxiv_sort_order: Literal["ascending", "descending"] | None = None
    source_settings: TopicSourceSettingsResponse | None = None
    top_papers: int | None = Field(default=None, ge=1, le=20)
    weekly_top: int | None = Field(default=None, ge=1, le=50)
    is_favorite: bool | None = None
    status: Literal["active", "archived"] | None = None
    default_collection_cadence: str | None = None
    default_report_cadence: str | None = None
    acquisition_language: str | None = None
    review_report_language: str | None = None
    acquisition_start_date: str | None = None
    acquisition_end_date: str | None = None


class TopicProfileDeleteResponse(BaseModel):
    profile_id: str
    status: Literal["deleted"] = "deleted"


class DashboardStartRunsRequest(BaseModel):
    profile_ids: list[str] = Field(default_factory=list)


class DashboardStartedRunResponse(BaseModel):
    profile_id: str
    run_id: str
    topic: str
    status: str


class DashboardStartRunsResponse(BaseModel):
    items: list[DashboardStartedRunResponse] = Field(default_factory=list)


class DashboardMetricPointResponse(BaseModel):
    label: str
    count: int = 0


class DashboardTrendPointResponse(BaseModel):
    label: str
    count: int = 0


class DashboardKeywordResponse(BaseModel):
    keyword: str
    count: int = 0


class DashboardInstitutionResponse(BaseModel):
    name: str
    count: int = 0


class DashboardReportSummaryResponse(BaseModel):
    run_id: str
    title: str
    path: str | None = None
    created_at: str
    status: str


class TopicDashboardDistributionsResponse(BaseModel):
    themes: list[DashboardMetricPointResponse] = Field(default_factory=list)
    statuses: list[DashboardMetricPointResponse] = Field(default_factory=list)


class TopicDashboardResponse(BaseModel):
    selected_profile_id: str | None = None
    topic: TopicProfileResponse | None = None
    totals: DashboardCountResponse = Field(default_factory=DashboardCountResponse)
    recent_runs: list[DashboardRunSummaryResponse] = Field(default_factory=list)
    latest_papers: list[DashboardPaperSummaryResponse] = Field(default_factory=list)
    hot_keywords: list[DashboardKeywordResponse] = Field(default_factory=list)
    trend: list[DashboardTrendPointResponse] = Field(default_factory=list)
    distributions: TopicDashboardDistributionsResponse = Field(default_factory=TopicDashboardDistributionsResponse)
    institution_rankings: list[DashboardInstitutionResponse] = Field(default_factory=list)
    recent_reports: list[DashboardReportSummaryResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TopicSchedulePipelineStepResponse(BaseModel):
    step_id: str
    label: str
    description: str


class TopicScheduleResponse(BaseModel):
    schedule_id: str
    profile_id: str
    title: str
    topic: str
    task_type: str = "collect_analyze"
    kind: str = "default"
    enabled: bool = False
    cadence: str
    cadence_label: str
    timezone: str = "Asia/Seoul"
    timezone_error: str | None = None
    pipeline: list[TopicSchedulePipelineStepResponse] = Field(default_factory=list)
    last_run_id: str | None = None
    last_status: str | None = None
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_message: str | None = None
    last_scheduled_at: str | None = None
    next_run_at: str | None = None
    next_run_hint: str | None = None
    current_run_id: str | None = None
    current_run_status: str | None = None
    scheduler_state: str = "paused"
    last_error: str | None = None
    last_trigger_source: str | None = None
    last_worker_id: str | None = None
    dispatch_attempts: int = 0
    lock_owner: str | None = None
    lock_acquired_at: str | None = None
    lock_expires_at: str | None = None
    last_recovery_at: str | None = None
    last_recovery_message: str | None = None
    created_at: str
    updated_at: str


class TopicSchedulerStatusResponse(BaseModel):
    mode: str = "embedded"
    configured_mode: str | None = None
    configured_mode_raw: str | None = None
    configured_mode_valid: bool = True
    enabled: bool = True
    running: bool = False
    status: str = "stale"
    runtime_mode: str = "single_node_file_backed"
    runtime_mode_status: str = "degraded"
    runtime_mode_label: str | None = None
    storage_backend: str | None = None
    configured_runtime_mode: str | None = None
    configured_runtime_mode_raw: str | None = None
    configured_runtime_mode_source: str | None = None
    configured_runtime_mode_valid: bool = True
    distributed_ready: bool = False
    multi_node_dispatch_safe: bool = False
    backend: dict[str, Any] = Field(default_factory=dict)
    available_runtime_modes: dict[str, Any] = Field(default_factory=dict)
    architecture_note: str | None = None
    dispatch_safety: dict[str, Any] = Field(default_factory=dict)
    requires_external_worker: bool = False
    operator_message: str | None = None
    worker_id: str | None = None
    pid: int | None = None
    thread: str | None = None
    tick_seconds: float | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    heartbeat_age_seconds: float | None = None
    stale: bool = True
    last_cycle_summary: dict[str, Any] = Field(default_factory=dict)
    currently_leased_task_count: int = 0
    due_task_count: int = 0
    lease_health: dict[str, Any] = Field(default_factory=dict)
    last_dispatch_at: str | None = None
    last_dispatch_schedule_id: str | None = None
    last_dispatch_worker_id: str | None = None
    last_success_at: str | None = None
    last_success_schedule_id: str | None = None
    last_failure_at: str | None = None
    last_failure_schedule_id: str | None = None
    last_failure_message: str | None = None
    orphan_recovery_count: int = 0
    last_orphan_recovery_at: str | None = None
    last_orphan_recovery_message: str | None = None
    lock_timeout_seconds: float | None = None
    dispatch_lease_seconds: int | None = None
    store_lock: dict[str, Any] = Field(default_factory=dict)
    schedule_summary: dict[str, Any] = Field(default_factory=dict)


class TopicSchedulesResponse(BaseModel):
    items: list[TopicScheduleResponse] = Field(default_factory=list)
    scheduler: TopicSchedulerStatusResponse | None = None
    degraded: bool = False
    read_only: bool = False
    warnings: list[str] = Field(default_factory=list)


class TopicScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    title: str | None = None
    task_type: str = "collect_analyze"
    cadence: str
    enabled: bool = False


class TopicScheduleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    task_type: str | None = None
    enabled: bool | None = None
    cadence: str | None = None


class TopicScheduleDeleteResponse(BaseModel):
    schedule_id: str
    status: Literal["deleted"] = "deleted"


class TopicScheduleRunNowResponse(BaseModel):
    schedule: TopicScheduleResponse
    run: DashboardStartedRunResponse


class DashboardTopicActionRequest(BaseModel):
    profile_id: str
    action: Literal["collect", "analyze", "report", "full_pipeline"] = "full_pipeline"


class DashboardTopicActionResponse(BaseModel):
    profile_id: str
    action: Literal["collect", "analyze", "report", "full_pipeline"]
    run_id: str
    topic: str
    status: str
    message: str
    execution_plan: list[str] = Field(default_factory=list)


class ApiDashboardTopicResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    updatedAt: str
    isFavorite: bool = False
    status: Literal["active", "archived"] = "active"


class ApiTopicsResponse(BaseModel):
    items: list[ApiDashboardTopicResponse] = Field(default_factory=list)


class ApiTopicsOverviewSummaryResponse(BaseModel):
    topicCount: int = 0
    collectedPapers: int = 0
    fullTextReviewedCount: int = 0


class ApiTopicOverviewItemResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    updatedAt: str
    isFavorite: bool = False
    collectedPapers: int = 0
    collectedDelta: float = 0
    collectedSparkline: list[float] = Field(default_factory=list)
    corePapers: int = 0
    coveragePercentage: float = 0
    fullTextReviewedCount: int = 0
    totalPapers: int = 0
    trendLabel: str = ""
    trendDeltaPercent: float = 0
    degraded: bool = False


class ApiTopicsOverviewResponse(BaseModel):
    summary: ApiTopicsOverviewSummaryResponse = Field(default_factory=ApiTopicsOverviewSummaryResponse)
    items: list[ApiTopicOverviewItemResponse] = Field(default_factory=list)


class ApiMetricSeriesResponse(BaseModel):
    value: float = 0
    delta: float = 0
    sparkline: list[float] = Field(default_factory=list)


class ApiTrendIndexMetricResponse(BaseModel):
    label: str = "0"
    deltaPercent: float = 0
    sparkline: list[float] = Field(default_factory=list)


class ApiDashboardMetricsResponse(BaseModel):
    collectedPapers: ApiMetricSeriesResponse = Field(default_factory=ApiMetricSeriesResponse)
    corePapers: ApiMetricSeriesResponse = Field(default_factory=ApiMetricSeriesResponse)
    averageImpact: ApiMetricSeriesResponse = Field(default_factory=ApiMetricSeriesResponse)
    totalCitations: ApiMetricSeriesResponse = Field(default_factory=ApiMetricSeriesResponse)
    trendIndex: ApiTrendIndexMetricResponse = Field(default_factory=ApiTrendIndexMetricResponse)


class ApiDashboardTrendsResponse(BaseModel):
    labels: list[str] = Field(default_factory=list)
    totalPapers: list[int] = Field(default_factory=list)
    corePapers: list[int] = Field(default_factory=list)
    citations: list[int] = Field(default_factory=list)


class ApiHotKeywordResponse(BaseModel):
    rank: int
    keyword: str
    score: float = 0
    paperCount: int = 0
    sourceRunIds: list[str] = Field(default_factory=list)
    explanation: str | None = None


class ApiScheduledTaskResponse(BaseModel):
    id: str
    title: str
    scheduleLabel: str
    enabled: bool = False
    taskType: str
    lastRunAt: str | None = None
    nextRunAt: str | None = None
    timezoneError: str | None = None
    schedulerState: str = "paused"
    currentRunStatus: str | None = None
    lastStatus: str | None = None
    lastError: str | None = None


class ApiLatestCorePaperResponse(BaseModel):
    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    publishedAt: str | None = None
    citations: int = 0
    impactScore: float = 0
    sourceUrl: str | None = None
    sourceProvider: str | None = None
    runId: str | None = None
    rationale: str | None = None
    saved: bool = True


class ApiTopicPaperResponse(BaseModel):
    id: str
    runId: str
    title: str
    authors: list[str] = Field(default_factory=list)
    publishedAt: str | None = None
    citations: int = 0
    impactScore: float = 0
    sourceUrl: str | None = None
    sourceProvider: str | None = None
    saved: bool = True
    theme: str | None = None
    status: str | None = None


class ApiTopicPapersResponse(BaseModel):
    topic: ApiDashboardTopicResponse | None = None
    items: list[ApiTopicPaperResponse] = Field(default_factory=list)
    total: int = 0
    q: str = ""
    providerFilter: str | None = None
    providerCounts: list[ApiSourceContributionResponse] = Field(default_factory=list)
    provenance: "ApiRuntimeProvenanceResponse | None" = None
    warnings: list[str] = Field(default_factory=list)


class ApiFieldDistributionResponse(BaseModel):
    label: str
    count: int = 0
    percentage: float = 0
    sampleTitles: list[str] = Field(default_factory=list)
    samplePaperIds: list[str] = Field(default_factory=list)
    sourceRunIds: list[str] = Field(default_factory=list)
    emptyReason: str | None = None


class ApiInstitutionDistributionResponse(BaseModel):
    name: str
    count: int = 0
    canonicalName: str | None = None
    rawVariants: list[str] = Field(default_factory=list)
    sampleTitles: list[str] = Field(default_factory=list)
    samplePaperIds: list[str] = Field(default_factory=list)
    sourceRunIds: list[str] = Field(default_factory=list)
    emptyReason: str | None = None


class ApiRecentReportResponse(BaseModel):
    id: str
    runId: str | None = None
    title: str
    createdAt: str
    updatedAt: str | None = None
    status: Literal["queued", "running", "completed", "failed", "artifact_unavailable"] = "queued"
    downloadUrl: str | None = None
    error: str | None = None


class ApiReviewCoverageResponse(BaseModel):
    totalPapers: int = 0
    reviewedCount: int = 0
    abstractOnlyCount: int = 0
    fullTextReviewedCount: int = 0
    coveragePercentage: float = 0
    rawIndexedPaperCount: int = 0
    uniqueCandidateCount: int = 0
    topicRelevantUniqueCount: int = 0
    reviewedUniqueCount: int = 0
    abstractOnlyUniqueCount: int = 0
    fullTextAvailableUniqueCount: int = 0
    fullTextReviewedUniqueCount: int = 0


def _provider_origin_count_defaults() -> dict[str, int]:
    return {
        "deterministic": 0,
        "manual_import": 0,
        "stub_synthetic": 0,
        "command_fixture_synthetic": 0,
        "command_provider_operator_accepted": 0,
        "unknown": 0,
    }


class ApiProviderProvenanceAuditResponse(BaseModel):
    auditStatus: str = "valid"
    checkedCount: int = 0
    mismatchCount: int = 0
    syntheticCount: int = 0
    realProviderCount: int = 0
    unknownOriginCount: int = 0
    rejectedOrNeedsReviewLeakCount: int = 0
    missingM4ValidationCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiProviderReviewCoverageResponse(BaseModel):
    acceptedAiGroundedDraftCount: int = 0
    acceptedByOrigin: dict[str, int] = Field(default_factory=_provider_origin_count_defaults)
    needsReviewByOrigin: dict[str, int] = Field(default_factory=_provider_origin_count_defaults)
    rejectedByOrigin: dict[str, int] = Field(default_factory=_provider_origin_count_defaults)
    fixtureSyntheticCount: int = 0
    realProviderAcceptedCount: int = 0
    providerOrchestratedAcceptedCount: int = 0
    providerOrchestratedSyntheticCount: int = 0
    fullTextReviewedDenominator: int = 0
    coverageWarning: str = ""
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provenanceAudit: ApiProviderProvenanceAuditResponse = Field(default_factory=ApiProviderProvenanceAuditResponse)


class ApiResearchOperationsCoverageResponse(BaseModel):
    fullTextReviewedUniqueCount: int = 0
    topicRelevantUniqueCount: int = 0
    fullTextCoverageRatio: float = 0
    aiGroundedAcceptedCount: int = 0
    aiGroundedCoverageRatio: float = 0
    providerGeneratedAcceptedCount: int = 0
    providerReviewCoverage: ApiProviderReviewCoverageResponse = Field(default_factory=ApiProviderReviewCoverageResponse)


class ApiResearchOperationsRecommendationResponse(BaseModel):
    recommendationGapCandidateCount: int = 0
    recommendationTopicCandidateCount: int = 0
    recommendationWeakCount: int = 0
    recommendationNeedsFollowupCount: int = 0
    recommendationCoverageWarningCount: int = 0
    preliminary: bool = False


class ApiResearchOperationsFollowupResponse(BaseModel):
    followupPlannedCount: int = 0
    followupExecutedCount: int = 0
    followupReviewedCount: int = 0
    skippedExistingReviewedCount: int = 0
    skippedNoPdfUrlCount: int = 0
    skippedBlockedCount: int = 0


class ApiResearchOperationsProviderResponse(BaseModel):
    generatedCount: int = 0
    acceptedCount: int = 0
    needsReviewCount: int = 0
    rejectedCount: int = 0
    failedCount: int = 0
    latestProviderOrigin: str | None = None


class ApiResearchOperationsGuidanceResponse(BaseModel):
    recommendationDryRunCommand: str = ""
    recommendationAuditCommand: str = ""
    followupPlanCommand: str = ""
    followupExecutionDryRunCommand: str = ""


class ApiResearchOperationsRCAuditCheckResponse(BaseModel):
    checkId: str = ""
    category: str = ""
    status: Literal["passed", "warning", "failed"] = "passed"
    label: str = ""
    summary: str = ""
    reasonCodes: list[str] = Field(default_factory=list)


class ApiResearchOperationsRCSurfaceSummaryResponse(BaseModel):
    surfaceId: str = ""
    status: Literal["valid", "warnings", "failed"] = "valid"
    summary: str = ""
    reasonCodes: list[str] = Field(default_factory=list)


class ApiResearchOperationsRCArtifactBoundarySummaryResponse(BaseModel):
    boundaryStatus: Literal["valid", "warnings", "failed"] = "valid"
    knownRuntimeUntrackedCount: int = 0
    unexpectedRuntimeTrackedCount: int = 0
    runtimeArtifactWarningCount: int = 0
    acceptedRuntimeWarningCount: int = 0
    blockingReleaseWarningCount: int = 0
    warningClassifications: list[dict[str, Any]] = Field(default_factory=list)
    knownRuntimeArtifacts: list[str] = Field(default_factory=list)
    unexpectedTrackedPaths: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiResearchOperationsRCAuditResponse(BaseModel):
    auditStatus: Literal["valid", "warnings", "failed"] = "valid"
    checkCount: int = 0
    failedCheckCount: int = 0
    warningCheckCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    surfaceSummaries: list[ApiResearchOperationsRCSurfaceSummaryResponse] = Field(default_factory=list)
    artifactBoundary: ApiResearchOperationsRCArtifactBoundarySummaryResponse = Field(default_factory=ApiResearchOperationsRCArtifactBoundarySummaryResponse)
    checks: list[ApiResearchOperationsRCAuditCheckResponse] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)


class ApiResearchOperationsReleaseFreezeResponse(BaseModel):
    releaseStatus: Literal["ready", "ready_with_warnings", "blocked"] = "ready"
    rcAuditStatus: str = "valid"
    validationMatrixSummary: dict[str, Any] = Field(default_factory=dict)
    coverageSummary: dict[str, Any] = Field(default_factory=dict)
    providerProvenanceSummary: dict[str, Any] = Field(default_factory=dict)
    recommendationSummary: dict[str, Any] = Field(default_factory=dict)
    oauthProviderSafetySummary: dict[str, Any] = Field(default_factory=dict)
    rcAuditSummary: dict[str, Any] = Field(default_factory=dict)
    artifactBoundarySummary: dict[str, Any] = Field(default_factory=dict)
    knownWarnings: list[dict[str, Any]] = Field(default_factory=list)
    knownWarningCount: int = 0
    blockingIssueCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)


class ApiTopicConfigurationResponse(BaseModel):
    topicId: str = ""
    displayName: str = ""
    queryTerms: list[str] = Field(default_factory=list)
    includeKeywords: list[str] = Field(default_factory=list)
    excludeKeywords: list[str] = Field(default_factory=list)
    minRelevance: float = Field(default=60, ge=0, le=100)
    defaultUniqueLimit: int = Field(default=50, ge=1, le=500)
    defaultAllowDomains: list[str] = Field(default_factory=lambda: ["arxiv.org"])
    defaultDownloadLimit: int = Field(default=5, ge=0, le=50)
    defaultMaxPdfBytes: int = Field(default=25_000_000, ge=1024, le=100_000_000)
    recommendationLimit: int = Field(default=5, ge=1, le=50)
    followupLimit: int = Field(default=10, ge=1, le=100)
    providerModeDefault: Literal["none", "stub", "command_dry_run"] = "none"
    liveActionsEnabled: bool = False
    notes: list[str] = Field(default_factory=list)
    acquisitionLanguage: str = "any"
    reviewReportLanguage: str = "en"
    acquisitionStartDate: str | None = None
    acquisitionEndDate: str | None = None
    validationWarnings: list[str] = Field(default_factory=list)


class ApiOperationsRunPlanResponse(BaseModel):
    planId: str
    topicId: str
    description: str
    riskLevel: Literal["read_only", "dry_run", "write_requires_cli"] = "dry_run"
    copyableCliCommand: str
    requiredSafetyFlags: list[str] = Field(default_factory=list)
    writesFiles: bool = False
    callsNetwork: bool = False
    callsProvider: bool = False
    requiresExplicitOperatorAction: bool = True
    previewOnly: bool = True
    serverExecutesCommand: bool = False
    warnings: list[str] = Field(default_factory=list)


class ApiProviderConfigurationResponse(BaseModel):
    providerId: str = ""
    displayName: str = ""
    providerType: Literal["stub", "command"] = "stub"
    profileRole: Literal["stub", "generic_command", "fixture", "real_example"] = "stub"
    executionSurface: Literal["sdk", "cli", "api", "mcp"] = "sdk"
    authMode: Literal[
        "api_key",
        "google_adc",
        "cli_oauth",
        "chatgpt_signin",
        "claude_code_login",
        "external_cli_auth",
        "oidc",
        "none",
        "unsupported",
        "unknown",
    ] = "none"
    credentialPolicy: list[
        Literal["external_only", "env_presence_only", "operator_profile", "no_token_storage", "no_cache_inspection"]
    ] = Field(default_factory=lambda: ["no_token_storage"])
    status: Literal[
        "configured_not_verified",
        "provider_smoke_only",
        "non_live_available",
        "unsupported",
        "policy_sensitive",
        "live_verified_tiny",
        "live_verified",
    ] = "configured_not_verified"
    proofArtifactRequired: bool = False
    allowedCommands: list[list[str]] = Field(default_factory=list)
    forbiddenCredentialPaths: list[str] = Field(default_factory=list)
    statusAfterProofCandidate: Literal["live_verified_tiny", "live_verified"] = "live_verified_tiny"
    researchOutputAllowed: bool = False
    credentialBoundaryDescription: str = ""
    operatorProfileSecretFree: bool = False
    enabled: bool = True
    defaultMode: Literal["stub", "command_dry_run"] = "stub"
    commandArgvTemplate: list[str] = Field(default_factory=list)
    workingDirectory: str | None = None
    timeoutSeconds: int = Field(default=30, ge=1, le=300)
    maxOutputChars: int = Field(default=20000, ge=1000, le=100000)
    maxPacketChars: int = Field(default=12000, ge=1000, le=100000)
    allowedTopicIds: list[str] = Field(default_factory=list)
    allowedReviewLevels: list[Literal["abstract_only", "full_text_grounded"]] = Field(default_factory=lambda: ["full_text_grounded"])
    allowLiveCommand: bool = False
    requiresOperatorConfirmation: bool = True
    requiresOperatorAcceptance: bool = True
    notes: list[str] = Field(default_factory=list)
    validationWarnings: list[str] = Field(default_factory=list)


class ApiProviderCommandPreviewResponse(BaseModel):
    argvRedacted: list[str] = Field(default_factory=list)
    executableLabel: str = ""
    argsRedacted: bool = False
    profilePathRedacted: bool = False
    credentialValuesPrinted: bool = False
    liveExecutionAllowed: bool = False


class ApiProviderAuthBridgeIssueResponse(BaseModel):
    code: str = ""
    message: str = ""
    field: str = ""


class ApiProviderAuthBridgeResponse(BaseModel):
    providerId: str = ""
    executionSurface: str = ""
    authMode: str = ""
    credentialPolicy: list[str] = Field(default_factory=list)
    currentStatus: str = "configured_not_verified"
    readinessLabel: str = "configured_not_verified"
    authBoundary: str = ""
    commandPreview: ApiProviderCommandPreviewResponse = Field(default_factory=ApiProviderCommandPreviewResponse)
    redactionApplied: bool = False
    proofArtifactRequired: bool = False
    proofArtifactPresent: bool = False
    researchOutputAllowed: bool = False
    outputAllowed: bool = False
    blockedReason: str = ""
    nextRequiredGate: str = ""
    issues: list[ApiProviderAuthBridgeIssueResponse] = Field(default_factory=list)


class ApiProviderReadinessResponse(BaseModel):
    providerId: str = ""
    displayName: str = ""
    providerType: Literal["stub", "command"] = "stub"
    profileRole: Literal["stub", "generic_command", "fixture", "real_example"] = "stub"
    enabled: bool = True
    readinessStatus: Literal[
        "ready",
        "ready_stub",
        "ready_fixture",
        "not_configured",
        "disabled",
        "command_missing",
        "unsafe_config",
        "dry_run_only",
        "example_profile_configured",
        "operator_acceptance_required",
        "live_disabled",
    ] = "not_configured"
    profileId: str = ""
    providerAlias: str = ""
    canonicalStatus: Literal[
        "live_verified_tiny",
        "live_verified",
        "configured_not_verified",
        "non_live_available",
        "unsupported",
        "policy_sensitive",
        "dry_run_verified",
        "configured_but_not_available",
        "environment_blocked_binary_missing",
        "environment_blocked_live_gate_missing",
        "environment_blocked_command_unknown",
        "unsafe_to_probe",
        "mock_only",
        "docs_only",
        "not_supported",
    ] = "not_supported"
    executionSurface: Literal["sdk", "cli", "api", "mcp"] = "sdk"
    authMode: str = ""
    credentialPolicy: list[str] = Field(default_factory=list)
    status: str = "configured_not_verified"
    proofArtifactRequired: bool = False
    allowedCommands: list[list[str]] = Field(default_factory=list)
    forbiddenCredentialPaths: list[str] = Field(default_factory=list)
    statusAfterProofCandidate: str = "live_verified_tiny"
    credentialBoundaryDescription: str = ""
    proofLevel: str = "not_verified"
    binaryStatus: Literal["found", "missing", "not_applicable", "unknown"] = "unknown"
    versionStatus: str = "not_checked"
    dryRunStatus: str = "not_applicable"
    liveProofStatus: str = "not_verified"
    liveGateStatus: str = "disabled"
    routingLabel: Literal[
        "provider_smoke_only",
        "not_allowed_for_research_output",
        "setup_required",
        "operator_approval_required",
        "live_proof_required",
        "research_output_blocked",
    ] = "not_allowed_for_research_output"
    researchOutputAllowed: bool = False
    reportGenerationAllowed: bool = False
    orchestrationAllowed: bool = False
    setupRequired: bool = False
    operatorActionRequired: bool = False
    statusMessage: str = ""
    evidence: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    safeModesAvailable: list[str] = Field(default_factory=list)
    liveAllowed: bool = False
    operatorAcceptanceRequired: bool = False
    commandPreviewRedacted: list[str] = Field(default_factory=list)
    authBridge: ApiProviderAuthBridgeResponse | None = None
    warnings: list[str] = Field(default_factory=list)
    configuration: ApiProviderConfigurationResponse = Field(default_factory=ApiProviderConfigurationResponse)


class ApiProviderReadinessListResponse(BaseModel):
    generatedAt: str = ""
    items: list[ApiProviderReadinessResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiProviderConfigurationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    providerId: str | None = None
    displayName: str | None = None
    providerType: str | None = None
    profileRole: str | None = None
    enabled: bool | None = None
    defaultMode: str | None = None
    commandArgvTemplate: list[str] | str | None = None
    workingDirectory: str | None = None
    timeoutSeconds: int | str | None = None
    maxOutputChars: int | str | None = None
    maxPacketChars: int | str | None = None
    allowedTopicIds: list[str] | str | None = None
    allowedReviewLevels: list[str] | str | None = None
    allowLiveCommand: bool | None = None
    requiresOperatorConfirmation: bool | None = None
    requiresOperatorAcceptance: bool | None = None
    notes: list[str] | str | None = None


class ApiProviderConfigurationValidationResponse(BaseModel):
    providerId: str = ""
    validationStatus: Literal["valid", "invalid"] = "valid"
    providerConfiguration: ApiProviderConfigurationResponse = Field(default_factory=ApiProviderConfigurationResponse)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    configHash: str = ""


class ApiOperationsPlannerResponse(BaseModel):
    topicConfiguration: ApiTopicConfigurationResponse = Field(default_factory=ApiTopicConfigurationResponse)
    runPlans: list[ApiOperationsRunPlanResponse] = Field(default_factory=list)
    providerReadiness: list[ApiProviderReadinessResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiTopicConfigurationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    topicId: str | None = None
    displayName: str | None = None
    queryTerms: list[str] | str | None = None
    includeKeywords: list[str] | str | None = None
    excludeKeywords: list[str] | str | None = None
    minRelevance: float | str | None = None
    defaultUniqueLimit: int | str | None = None
    defaultAllowDomains: list[str] | str | None = None
    defaultDownloadLimit: int | str | None = None
    defaultMaxPdfBytes: int | str | None = None
    recommendationLimit: int | str | None = None
    followupLimit: int | str | None = None
    providerModeDefault: str | None = None
    liveActionsEnabled: bool | None = None
    notes: list[str] | str | None = None
    acquisitionLanguage: str | None = None
    reviewReportLanguage: str | None = None
    acquisitionStartDate: str | None = None
    acquisitionEndDate: str | None = None


class ApiTopicConfigurationDiffItemResponse(BaseModel):
    field: str
    currentValue: Any | None = None
    draftValue: Any | None = None


class ApiTopicConfigurationValidationResponse(BaseModel):
    topicId: str
    validationStatus: Literal["valid", "invalid", "saved"] = "valid"
    topicConfiguration: ApiTopicConfigurationResponse = Field(default_factory=ApiTopicConfigurationResponse)
    changedFields: list[str] = Field(default_factory=list)
    diff: list[ApiTopicConfigurationDiffItemResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    configHash: str = ""
    savedPath: str | None = None
    operationsPlanner: ApiOperationsPlannerResponse = Field(default_factory=ApiOperationsPlannerResponse)


class ApiTopicConfigurationListItemResponse(BaseModel):
    topicId: str
    topicConfiguration: ApiTopicConfigurationResponse = Field(default_factory=ApiTopicConfigurationResponse)
    configHash: str = ""
    warnings: list[str] = Field(default_factory=list)


class ApiTopicConfigurationsResponse(BaseModel):
    items: list[ApiTopicConfigurationListItemResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiResearchOperationsResponse(BaseModel):
    coverage: ApiResearchOperationsCoverageResponse = Field(default_factory=ApiResearchOperationsCoverageResponse)
    recommendations: ApiResearchOperationsRecommendationResponse = Field(default_factory=ApiResearchOperationsRecommendationResponse)
    followup: ApiResearchOperationsFollowupResponse = Field(default_factory=ApiResearchOperationsFollowupResponse)
    provider: ApiResearchOperationsProviderResponse = Field(default_factory=ApiResearchOperationsProviderResponse)
    rcAudit: ApiResearchOperationsRCAuditResponse = Field(default_factory=ApiResearchOperationsRCAuditResponse)
    releaseFreeze: ApiResearchOperationsReleaseFreezeResponse = Field(default_factory=ApiResearchOperationsReleaseFreezeResponse)
    auth: AuthFoundationStatusResponse = Field(default_factory=AuthFoundationStatusResponse)
    latestRecommendationAuditSummary: dict[str, Any] | None = None
    latestRecommendationFollowupPlanSummary: dict[str, Any] | None = None
    latestFollowupExecutionSummary: dict[str, Any] | None = None
    latestProviderRunSummary: dict[str, Any] | None = None
    latestReportPath: str | None = None
    guidance: ApiResearchOperationsGuidanceResponse = Field(default_factory=ApiResearchOperationsGuidanceResponse)
    operationsPlanner: ApiOperationsPlannerResponse = Field(default_factory=ApiOperationsPlannerResponse)
    warnings: list[str] = Field(default_factory=list)


class ApiReviewRefreshRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=500)
    unique_limit: int | None = Field(default=None, ge=1, le=500)
    window_days: int | None = Field(default=None, ge=1, le=3650)
    min_relevance: float | None = Field(default=None, ge=0, le=100)
    dry_run: bool = False
    force: bool = False
    use_full_text: bool = False
    max_text_chars: int = Field(default=24000, ge=100, le=100000)
    prefer_existing_text: bool = True
    extract_existing_pdf: bool = False
    fetch_missing_pdf: bool = False
    download_limit: int = Field(default=0, ge=0, le=50)
    provider: str = "arxiv"
    allow_domains: list[str] | None = None
    max_pdf_bytes: int = Field(default=25_000_000, ge=1024, le=100_000_000)
    download_timeout: int = Field(default=30, ge=1, le=300)
    extract_after_fetch: bool = False


class ApiReviewRefreshResponse(BaseModel):
    topic_id: str
    force: bool = False
    dry_run: bool = False
    limit: int | None = None
    unique_limit: int | None = None
    window_days: int | None = None
    min_relevance: float | None = None
    use_full_text: bool = False
    max_text_chars: int = 24000
    prefer_existing_text: bool = True
    extract_existing_pdf: bool = False
    fetch_missing_pdf: bool = False
    download_limit: int = 0
    provider: str = "arxiv"
    allow_domains: list[str] = Field(default_factory=list)
    max_pdf_bytes: int = 25_000_000
    download_timeout: int = 30
    extract_after_fetch: bool = False
    raw_discovered_count: int = 0
    unique_discovered_count: int = 0
    duplicate_row_count: int = 0
    raw_reviewed_count: int = 0
    unique_reviewed_count: int = 0
    raw_skipped_count: int = 0
    unique_skipped_count: int = 0
    discovered_count: int = 0
    reviewed_count: int = 0
    skipped_count: int = 0
    abstract_only_count: int = 0
    unavailable_count: int = 0
    full_text_available_count: int = 0
    full_text_reviewed_count: int = 0
    reviewed_from_text_count: int = 0
    reviewed_from_pages_count: int = 0
    reviewed_from_pdf_extract_count: int = 0
    pdf_planned_count: int = 0
    pdf_downloaded_count: int = 0
    pdf_rejected_count: int = 0
    pdf_failed_count: int = 0
    pdf_extracted_count: int = 0
    pdf_extraction_failed_count: int = 0
    extracted_text_count: int = 0
    extracted_page_count: int = 0
    low_relevance_unique_count: int = 0
    low_relevance_count: int = 0
    report_eligible_unique_count: int = 0
    source_run_ids: list[str] = Field(default_factory=list)
    prioritization: dict[str, Any] = Field(default_factory=dict)
    audit_summary: dict[str, Any] = Field(default_factory=dict)
    pdf_acquisition: dict[str, Any] = Field(default_factory=dict)


class ApiReportExportArtifactResponse(BaseModel):
    export_id: str
    artifact_id: str | None = None
    report_id: str | None = None
    version_id: str | None = None
    topic_id: str | None = None
    run_id: str | None = None
    format: str
    mime_type: str | None = None
    file_name: str | None = None
    output: str | None = None
    storage_path: str | None = None
    created_at: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    source_markdown_version_id: str | None = None
    source_markdown_hash: str | None = None
    renderer: str | None = None
    status: Literal["ready", "completed", "failed", "unavailable"] = "unavailable"
    error_message: str | None = None
    download_available: bool = False


class ApiReportExportArtifactSummaryResponse(BaseModel):
    total: int = 0
    downloadable: int = 0
    unavailable: int = 0
    formats: list[str] = Field(default_factory=list)


class ApiReportHubItemResponse(BaseModel):
    id: str
    runId: str
    topicId: str | None = None
    topicName: str
    title: str
    createdAt: str
    updatedAt: str | None = None
    status: Literal["queued", "running", "completed", "failed", "artifact_unavailable"] = "queued"
    downloadUrl: str | None = None
    error: str | None = None
    sourceProviders: list[str] = Field(default_factory=list)
    reportQuality: dict[str, Any] | None = None
    completedVersionCount: int = 0
    latestCompletedVersionId: str | None = None
    latestCompletedVersionAt: str | None = None
    latestCompletedVersionHash: str | None = None
    exportArtifacts: list[ApiReportExportArtifactResponse] = Field(default_factory=list)
    exportArtifactSummary: ApiReportExportArtifactSummaryResponse = Field(default_factory=ApiReportExportArtifactSummaryResponse)


class ApiReportsResponse(BaseModel):
    items: list[ApiReportHubItemResponse] = Field(default_factory=list)
    total: int = 0
    provenance: "ApiRuntimeProvenanceResponse | None" = None
    warnings: list[str] = Field(default_factory=list)


class ApiTopicRecommendationItemResponse(BaseModel):
    id: str
    versionId: str | None = None
    type: Literal["new_topic", "gap"]
    title: str
    question: str = ""
    whyNow: str = ""
    methodDirection: str = ""
    expectedContribution: str = ""
    gapStatement: str = ""
    noveltyRationale: str = ""
    confidence: str = ""
    supportingPaperCount: int = 0
    suggestedQueries: list[str] = Field(default_factory=list)
    actionable: bool = False
    supportingPaperIds: list[str] = Field(default_factory=list)
    evidenceRefs: list[RecommendationEvidenceRef] = Field(default_factory=list)
    coverageWarning: str | None = None
    riskNotes: list[str] = Field(default_factory=list)
    requiredFollowUpEvidence: list[str] = Field(default_factory=list)
    problemStatement: str = ""
    differentiationClaim: str = ""
    nearestExistingWork: list[str] = Field(default_factory=list)
    limitationRefs: list[RecommendationEvidenceRef] = Field(default_factory=list)
    sourceBasis: str = ""


class ApiTopicRecommendationsResponse(BaseModel):
    topicId: str
    items: list[ApiTopicRecommendationItemResponse] = Field(default_factory=list)
    newTopicCount: int = 0
    gapCount: int = 0
    coverageWarning: str | None = None


class ApiRuntimeProvenanceResponse(BaseModel):
    surfaceId: str
    label: str
    mode: Literal["persisted_artifact", "runtime_adapter"]
    definition: str
    computedAt: str
    sourceRunIds: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiAnalyticsSourceRunResponse(BaseModel):
    runId: str
    status: str
    createdAt: str
    finishedAt: str | None = None


class ApiAnalyticsCoverageResponse(BaseModel):
    coverageId: str
    label: str
    covered: int = 0
    total: int = 0
    percentage: float = 0
    emptyReason: str | None = None


class ApiAnalyticsMetricDefinitionResponse(BaseModel):
    metricId: str
    label: str
    definition: str


class ApiAnalyticsFreshnessResponse(BaseModel):
    status: str
    label: str
    computedAt: str
    lastAnalysisAt: str | None = None
    newestSourceRunAt: str | None = None
    sourceRunCount: int = 0
    sourcePaperCount: int = 0
    staleReason: str | None = None


class ApiAnalyticsDerivationResponse(BaseModel):
    surfaceId: str
    label: str
    mode: Literal["persisted_artifact", "runtime_adapter"]
    definition: str
    computedAt: str
    sourceRunIds: list[str] = Field(default_factory=list)


class ApiAnalyticsTrendPointResponse(BaseModel):
    label: str
    totalPapers: int = 0
    corePapers: int = 0
    citations: int = 0
    averageImpact: float = 0
    sourceRunIds: list[str] = Field(default_factory=list)
    bucketMode: str = "published_month"
    emptyReason: str | None = None


class ApiAnalyticsTrendDetailResponse(BaseModel):
    bucketMode: str = "published_month"
    summary: str
    points: list[ApiAnalyticsTrendPointResponse] = Field(default_factory=list)


class ApiAnalyticsKeywordDetailResponse(BaseModel):
    keyword: str
    score: float = 0
    paperCount: int = 0
    sampleTitles: list[str] = Field(default_factory=list)
    samplePaperIds: list[str] = Field(default_factory=list)
    sourceRunIds: list[str] = Field(default_factory=list)
    rawVariants: list[str] = Field(default_factory=list)
    explanation: str


class ApiAnalyticsFieldDetailResponse(BaseModel):
    label: str
    count: int = 0
    percentage: float = 0
    sampleTitles: list[str] = Field(default_factory=list)
    samplePaperIds: list[str] = Field(default_factory=list)
    sourceRunIds: list[str] = Field(default_factory=list)
    emptyReason: str | None = None


class ApiAnalyticsInstitutionDetailResponse(BaseModel):
    name: str
    count: int = 0
    canonicalName: str | None = None
    rawVariants: list[str] = Field(default_factory=list)
    sampleTitles: list[str] = Field(default_factory=list)
    samplePaperIds: list[str] = Field(default_factory=list)
    sourceRunIds: list[str] = Field(default_factory=list)
    emptyReason: str | None = None


class ApiAnalyticsCorePaperDetailResponse(BaseModel):
    paperId: str
    title: str
    runId: str | None = None
    publishedAt: str | None = None
    citations: int = 0
    impactScore: float = 0
    rationale: str
    sourceUrl: str | None = None


class ApiTopicAnalyticsResponse(BaseModel):
    profileId: str
    artifactVersion: str = "topic-analytics-v1"
    computedAt: str
    generatedAt: str
    lastAnalysisAt: str | None = None
    freshness: ApiAnalyticsFreshnessResponse | None = None
    sourceRuns: list[ApiAnalyticsSourceRunResponse] = Field(default_factory=list)
    coverage: list[ApiAnalyticsCoverageResponse] = Field(default_factory=list)
    metricDefinitions: list[ApiAnalyticsMetricDefinitionResponse] = Field(default_factory=list)
    derivations: list[ApiAnalyticsDerivationResponse] = Field(default_factory=list)
    trendDetail: ApiAnalyticsTrendDetailResponse
    keywordDetails: list[ApiAnalyticsKeywordDetailResponse] = Field(default_factory=list)
    fieldDetails: list[ApiAnalyticsFieldDetailResponse] = Field(default_factory=list)
    institutionDetails: list[ApiAnalyticsInstitutionDetailResponse] = Field(default_factory=list)
    corePaperDetails: list[ApiAnalyticsCorePaperDetailResponse] = Field(default_factory=list)


class ApiScheduleReadStatusResponse(BaseModel):
    state: Literal["ready", "defaults", "unavailable"]
    warnings: list[str] = Field(default_factory=list)


class ApiTopicDashboardResponse(BaseModel):
    topic: ApiDashboardTopicResponse | None = None
    sourceSummary: ApiSourceSummaryResponse | None = None
    metrics: ApiDashboardMetricsResponse = Field(default_factory=ApiDashboardMetricsResponse)
    trends: ApiDashboardTrendsResponse = Field(default_factory=ApiDashboardTrendsResponse)
    hotKeywords: list[ApiHotKeywordResponse] = Field(default_factory=list)
    scheduledTasks: list[ApiScheduledTaskResponse] = Field(default_factory=list)
    scheduleStatus: ApiScheduleReadStatusResponse | None = None
    latestCorePapers: list[ApiLatestCorePaperResponse] = Field(default_factory=list)
    fieldDistribution: list[ApiFieldDistributionResponse] = Field(default_factory=list)
    institutionDistribution: list[ApiInstitutionDistributionResponse] = Field(default_factory=list)
    recentReports: list[ApiRecentReportResponse] = Field(default_factory=list)
    reviewCoverage: ApiReviewCoverageResponse = Field(default_factory=ApiReviewCoverageResponse)
    researchOperations: ApiResearchOperationsResponse = Field(default_factory=ApiResearchOperationsResponse)
    analytics: ApiTopicAnalyticsResponse | None = None
    warnings: list[str] = Field(default_factory=list)


class TopicNotificationChannelResponse(BaseModel):
    channel_id: str
    label: str
    description: str
    available: bool = True
    enabled: bool = False
    state: Literal[
        "available",
        "unavailable_missing_env",
        "misconfigured",
        "failed_delivery",
        "connection_failed",
        "delivery_failed",
        "disabled",
    ] = "available"
    readiness_state: str = "available"
    status_label: str
    destination: str = ""
    destination_label: str = ""
    unavailable_reason: str | None = None
    supports_test: bool = False


class TopicNotificationRuleResponse(BaseModel):
    rule_id: str
    label: str
    description: str
    enabled: bool = False


class TopicNotificationSettingsResponse(BaseModel):
    profile_id: str
    topic_id: str
    topic_name: str
    in_app_enabled: bool = True
    channels: list[TopicNotificationChannelResponse] = Field(default_factory=list)
    rules: list[TopicNotificationRuleResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class TopicNotificationRuleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    enabled: bool


class TopicNotificationChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: Literal["email", "webhook"]
    enabled: bool = False
    destination: str = Field(default="", max_length=2048)


class TopicNotificationSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_app_enabled: bool | None = None
    channels: list[TopicNotificationChannelUpdateRequest] = Field(default_factory=list)
    rules: list[TopicNotificationRuleUpdateRequest] = Field(default_factory=list)


class TopicNotificationItemResponse(BaseModel):
    notification_id: str
    profile_id: str
    topic_name: str
    rule_id: str
    event_type: str
    severity: Literal["info", "success", "warning", "error"] = "info"
    title: str
    message: str
    created_at: str
    run_id: str | None = None
    schedule_id: str | None = None
    download_url: str | None = None


class TopicNotificationsResponse(BaseModel):
    profile_id: str
    topic_name: str
    items: list[TopicNotificationItemResponse] = Field(default_factory=list)
    total: int = 0


class TopicNotificationDeliveryResponse(BaseModel):
    delivery_id: str
    notification_id: str | None = None
    profile_id: str
    topic_name: str
    channel_id: str
    rule_id: str
    event_type: str
    state: Literal[
        "available",
        "unavailable_missing_env",
        "misconfigured",
        "failed_delivery",
        "connection_failed",
        "delivery_failed",
        "disabled",
    ] = "failed_delivery"
    status: Literal["sent", "failed", "disabled"] = "failed"
    destination_label: str = ""
    status_code: int | None = None
    error_code: str | None = None
    message: str = ""
    attempts: int = 1
    attempted: bool = True
    created_at: str
    updated_at: str


class TopicNotificationDeliveriesResponse(BaseModel):
    profile_id: str
    topic_name: str
    items: list[TopicNotificationDeliveryResponse] = Field(default_factory=list)
    total: int = 0


class TopicNotificationTestDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: Literal["email", "webhook"]


class TopicNotificationTestDeliveryResponse(BaseModel):
    profile_id: str
    channel_id: Literal["email", "webhook"]
    deliveries: list[TopicNotificationDeliveryResponse] = Field(default_factory=list)


class SettingsProviderStatusResponse(BaseModel):
    provider: str | None = None
    provider_mode: str
    configured: bool = False
    live_call_enabled: bool = False
    canonicalStatus: str = "not_supported"
    routingLabel: str = "not_allowed_for_research_output"
    researchOutputAllowed: bool = False
    reportGenerationAllowed: bool = False
    orchestrationAllowed: bool = False
    statusMessage: str = ""
    model: str | None = None
    api_base: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SettingsMcpStatusResponse(BaseModel):
    endpoint: str | None = None
    resource_metadata_url: str | None = None
    transport: Literal["streamable-http"] = "streamable-http"
    protocol_version: str
    auth_required: bool = False
    public_https: bool = False
    cli_ready: bool = False
    chatgpt_ready: bool = False
    readiness_label: Literal[
        "not_configured",
        "local_cli_only",
        "oidc_http_only",
        "oidc_missing_audience",
        "oauth_ready",
    ] = "not_configured"
    authorization_servers: list[str] = Field(default_factory=list)
    public_tools: list[str] = Field(default_factory=list)
    protected_read_tools: list[str] = Field(default_factory=list)
    protected_write_tools: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SettingsOpsStatusResponse(BaseModel):
    generated_at: str
    run_counts: dict[str, int] = Field(default_factory=dict)
    latest_run_id: str | None = None
    latest_run_status: str | None = None
    latest_run_updated_at: str | None = None
    last_successful_run_id: str | None = None
    last_successful_run_at: str | None = None
    last_failed_run_id: str | None = None
    last_failed_run_at: str | None = None
    last_failure_message: str | None = None
    scheduler: dict[str, Any] = Field(default_factory=dict)
    notification_delivery: dict[str, Any] = Field(default_factory=dict)


class ReportDraftAssistSelectRequest(BaseModel):
    provider_id: Literal["deterministic_local", "codex"]


class SettingsStatusResponse(BaseModel):
    health_status: str = "ok"
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    authenticated: bool = True
    product_session_backend: Literal["local_internal", "server_side_file"] = "local_internal"
    auth_warnings: list[str] = Field(default_factory=list)
    authFoundation: AuthFoundationStatusResponse = Field(default_factory=AuthFoundationStatusResponse)
    appOauthOidcDecision: dict[str, Any] = Field(default_factory=dict)
    mcp_url: str | None = None
    provider: SettingsProviderStatusResponse
    providerProofState: dict[str, Any] = Field(default_factory=dict)
    reportDraftAssist: dict[str, Any] = Field(default_factory=dict)
    mcp: SettingsMcpStatusResponse
    deployment: dict[str, Any] = Field(default_factory=dict)
    metadata_index_state: dict[str, str] = Field(default_factory=dict)
    ops: SettingsOpsStatusResponse
    stitch_status: Literal["not_integrated", "seam_prepared", "integrated"] = "not_integrated"

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TopicKind = Literal["synthetic_demo", "real_open_access", "local_empty"]
SourcePolicy = Literal["synthetic_fixture", "pmc_open_access", "local_only_no_external_source"]
ProviderPosture = Literal[
    "deterministic_only",
    "prototype_provider_capability_available_but_disabled",
]
CurationState = Literal["included", "excluded", "hold"]
CurationReason = Literal[
    "relevant_core",
    "relevant_context",
    "duplicate_work",
    "out_of_scope",
    "insufficient_full_text",
    "license_not_accepted",
    "article_type_not_eligible",
    "quality_or_relevance_hold",
    "manual_owner_decision",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TopicCreateRequest(_StrictModel):
    display_name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    research_question: str = Field(default="", max_length=500)
    topic_kind: TopicKind
    source_policy: SourcePolicy
    language: str = Field(default="en", min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    publication_date_cutoff: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    license_policy: str = Field(default="local_only", min_length=1, max_length=80)
    refresh_mode: str = Field(default="manual_only", min_length=1, max_length=80)


class TopicSettingsUpdateRequest(_StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    research_question: str | None = Field(default=None, max_length=500)
    source_policy: SourcePolicy | None = None
    language: str | None = Field(default=None, min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    publication_date_cutoff: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    license_policy: str | None = Field(default=None, min_length=1, max_length=80)
    refresh_mode: str | None = Field(default=None, min_length=1, max_length=80)
    provider_posture: ProviderPosture | None = None
    archived: bool | None = None


class CurationPreviewRequest(_StrictModel):
    source_snapshot_id: str = Field(min_length=8, max_length=160)
    corpus_item_id: str = Field(min_length=1, max_length=180)
    requested_state: CurationState
    reason_code: CurationReason
    note: str | None = Field(default=None, max_length=200)


class ChangesetDecisionRequest(_StrictModel):
    confirmed: bool


class ProjectBundleExportPreviewRequest(_StrictModel):
    selected_topic_ids: list[str] | None = Field(default=None, max_length=256)


class ProjectBundleExportConfirmRequest(ProjectBundleExportPreviewRequest):
    preview_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    confirmed: bool


class ProjectBundleImportConfirmRequest(_StrictModel):
    preview_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    confirmed: bool


class TopicRunStartRequest(_StrictModel):
    snapshot_id: str | None = Field(default=None, min_length=8, max_length=160)
    confirmed: bool = True


class RefreshStartRequest(_StrictModel):
    confirmed: bool


class InitialAcquisitionRequest(_StrictModel):
    confirmed: bool
    network_enabled: bool


class LivingScheduleRequest(_StrictModel):
    enabled: bool = False
    cadence: Literal["disabled", "manual", "daily", "weekly", "monthly"] = "disabled"
    time_zone: str = Field(default="UTC", min_length=1, max_length=80)
    local_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    network_policy: Literal[
        "disabled",
        "manual_official_source_only",
        "scheduled_official_source_only",
    ] = "disabled"
    briefing_policy: Literal[
        "no_change_and_material_changes",
        "material_changes_only",
    ] = "no_change_and_material_changes"


class LivingScheduleDispatchRequest(_StrictModel):
    confirmed: bool


class CurationDeltaBriefingRequest(_StrictModel):
    before_snapshot_id: str = Field(min_length=8, max_length=180)
    after_snapshot_id: str = Field(min_length=8, max_length=180)


class AttentionActionRequest(_StrictModel):
    change_event_id: str = Field(min_length=8, max_length=180)
    action: Literal["acknowledge", "dismiss"]


class GapAnalysisRequest(_StrictModel):
    snapshot_id: str = Field(min_length=8, max_length=180)


class GapAnalysisComparisonRequest(_StrictModel):
    before_analysis_id: str = Field(min_length=8, max_length=180)
    after_analysis_id: str = Field(min_length=8, max_length=180)


class RecommendationComparisonRequest(_StrictModel):
    recommendation_ids: list[str] = Field(min_length=2, max_length=5)


class RecommendationDecisionRequest(_StrictModel):
    requested_status: Literal["accepted", "held", "rejected"]
    reason_code: Literal[
        "evidence_basis_sufficient_for_follow_up",
        "high_practical_relevance",
        "clear_falsifiable_question",
        "needs_more_evidence",
        "scope_too_broad",
        "feasibility_uncertain",
        "close_current_corpus_collision",
        "insufficient_measurement_definition",
        "low_significance_within_current_scope",
        "manual_owner_decision",
    ]
    bounded_note: str | None = Field(default=None, max_length=240)


class RecommendationPromotionRequest(_StrictModel):
    confirmed: bool

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from app.schemas.workbench import OutlineResponse, PreviewResponse


WorkspaceActionType = Literal[
    "insert_section_before",
    "insert_section_after",
    "append_to_target_section",
    "replace_section_draft",
    "create_candidate_section",
    "attach_compare_note",
    "apply_theme_outline_suggestion",
    "apply_paper_seed_suggestion",
]

WorkspaceMergeIntent = Literal[
    "replace_target_section",
    "insert_before_target",
    "insert_after_target",
    "append_to_target_section",
    "create_candidate_section",
    "attach_compare_note_only",
]

WorkspaceConflictKind = Literal[
    "missing_target_section",
    "missing_source_section",
    "weak_anchor_match",
    "ambiguous_target_match",
    "duplicate_target_mutation",
    "destructive_replace_requires_confirmation",
    "stale_outline_hash",
    "citation_overlap_or_reference_risk",
    "target_order_instability",
    "source_artifact_missing",
    "dropped_dependency",
    "recoverable_by_rebase",
]

WorkspaceConflictSeverity = Literal["info", "warn", "blocker", "recoverable", "confirmation_required"]
WorkspaceRecoveryMode = Literal["refresh_session_metadata", "rebase_staged_candidates", "discard_staged_candidates", "none"]
WorkspaceCandidateStatus = Literal["safe", "warn", "blocked", "recoverable_by_rebase", "destructive_confirmation_required", "dropped"]


class WorkspaceActionPayloadResponse(BaseModel):
    title: str | None = None
    body_markdown: str | None = None
    paper_ids: list[str] = Field(default_factory=list)
    compare_note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceActionRequest(BaseModel):
    editing_session_id: str | None = None
    workspace_view_id: str | None = None
    action_type: WorkspaceActionType
    source_run_id: str | None = None
    compare_partner_run_id: str | None = None
    source_section_id: str | None = None
    source_section_title: str | None = None
    target_section_id: str | None = None
    target_position: Literal["before", "after", "append", "replace", "note"] | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    source_bucket_id: str | None = None
    source_theme: str | None = None
    source_paper_ids: list[str] = Field(default_factory=list)
    preset_id: str | None = None
    selection_source: str | None = None
    payload: WorkspaceActionPayloadResponse = Field(default_factory=WorkspaceActionPayloadResponse)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceActionPreviewRequest(WorkspaceActionRequest):
    pass


class WorkspaceActionApplyRequest(WorkspaceActionRequest):
    confirm_destructive: bool = False


class WorkspaceActionTargetResponse(BaseModel):
    target_section_id: str | None = None
    target_section_title: str | None = None
    target_index: int | None = None
    destructive: bool = False


class WorkspaceActionPreviewResponse(BaseModel):
    run_id: str
    action_id: str
    action_type: WorkspaceActionType
    preview_ready: bool = False
    effective_target: WorkspaceActionTargetResponse = Field(default_factory=WorkspaceActionTargetResponse)
    proposed_section: dict[str, Any] | None = None
    proposed_compare_note: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)
    preview_blocks: list["WorkspacePreviewBlockResponse"] = Field(default_factory=list)
    changed_block_count: int = 0
    citation_delta: "WorkspaceCitationDeltaResponse" = Field(default_factory=lambda: WorkspaceCitationDeltaResponse())
    destructive: bool = False
    requires_confirmation: bool = False
    completeness: Literal["full", "partial", "coarse", "metadata_only"] = "coarse"
    structured_patch: "StructuredPatchResponse | None" = None
    conflict_details: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    candidate_conflict_summary: "WorkspaceCandidateConflictSummaryResponse" = Field(default_factory=lambda: WorkspaceCandidateConflictSummaryResponse())


class WorkspaceActionApplyResponse(BaseModel):
    run_id: str
    action_id: str
    action_type: WorkspaceActionType
    applied: bool = False
    message: str
    warnings: list[str] = Field(default_factory=list)
    outline: OutlineResponse
    preview: PreviewResponse


class WorkspacePreviewBlockResponse(BaseModel):
    block_id: str
    block_type: Literal["unchanged", "added", "removed", "replaced", "context"] = "context"
    label: str
    kind: Literal["source", "target", "proposed", "note", "summary", "patch"] = "summary"
    content: str
    before_text: str | None = None
    after_text: str | None = None
    before_excerpt: str | None = None
    after_excerpt: str | None = None
    anchor_kind: Literal["section", "paragraph", "note", "summary"] = "section"
    anchor_value: str | None = None
    related_citation_keys: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceCitationDeltaResponse(BaseModel):
    left_count: int = 0
    right_count: int = 0
    delta: int = 0
    notes: list[str] = Field(default_factory=list)


class PatchAnchorResponse(BaseModel):
    anchor_kind: Literal["section_boundary", "paragraph_index", "sentence_window", "citation_key", "note_slot", "fallback"] = "fallback"
    anchor_value: str | None = None
    matched: bool = False
    confidence: float = 0.0
    fallback_reason: str | None = None


class WorkspaceTargetResolutionResponse(BaseModel):
    original_target_section_id: str | None = None
    resolved_target_section_id: str | None = None
    resolution_mode: Literal["exact", "retargeted", "title_match", "index_shift", "fallback", "missing"] = "fallback"
    resolution_confidence: float = 0.0
    retarget_warnings: list[str] = Field(default_factory=list)


class WorkspaceConflictDetailResponse(BaseModel):
    conflict_id: str
    candidate_id: str | None = None
    conflict_kind: WorkspaceConflictKind
    severity: WorkspaceConflictSeverity = "warn"
    title: str
    message: str
    related_section_ids: list[str] = Field(default_factory=list)
    related_candidate_ids: list[str] = Field(default_factory=list)
    related_paper_ids: list[str] = Field(default_factory=list)
    anchor_summary: str | None = None
    target_resolution_summary: str | None = None
    suggested_recovery_mode: WorkspaceRecoveryMode = "none"
    suggested_manual_actions: list[str] = Field(default_factory=list)
    can_recover_by_rebase: bool = False
    can_proceed_with_confirmation: bool = False
    warnings: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class WorkspaceCandidateConflictSummaryResponse(BaseModel):
    candidate_id: str | None = None
    status: WorkspaceCandidateStatus = "safe"
    total_conflicts: int = 0
    highest_severity: WorkspaceConflictSeverity = "info"
    severity_counts: dict[str, int] = Field(default_factory=dict)
    affected_section_ids: list[str] = Field(default_factory=list)
    related_paper_ids: list[str] = Field(default_factory=list)
    can_apply: bool = True
    can_recover_by_rebase: bool = False
    can_proceed_with_confirmation: bool = False
    suggested_recovery_mode: WorkspaceRecoveryMode = "none"
    suggested_manual_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceRecoveryConflictDeltaResponse(BaseModel):
    candidate_id: str
    original_status: WorkspaceCandidateStatus = "safe"
    projected_status: WorkspaceCandidateStatus = "safe"
    original_conflicts: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    projected_conflicts: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    resolved_conflict_ids: list[str] = Field(default_factory=list)
    introduced_conflict_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceRebaseDiffResponse(BaseModel):
    candidate_id: str
    recovery_mode: WorkspaceRecoveryMode = "none"
    comparison_precision: Literal["fine_grained", "section_level", "coarse"] = "coarse"
    original_target_resolution: WorkspaceTargetResolutionResponse = Field(default_factory=WorkspaceTargetResolutionResponse)
    projected_target_resolution: WorkspaceTargetResolutionResponse = Field(default_factory=WorkspaceTargetResolutionResponse)
    original_patch: "StructuredPatchResponse | None" = None
    projected_patch: "StructuredPatchResponse | None" = None
    original_conflicts: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    projected_conflicts: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    original_status: WorkspaceCandidateStatus = "safe"
    projected_status: WorkspaceCandidateStatus = "safe"
    confidence_delta: float = 0.0
    warnings_before: list[str] = Field(default_factory=list)
    warnings_after: list[str] = Field(default_factory=list)
    dropped_blocks: int = 0
    added_blocks: int = 0
    changed_anchor_count: int = 0
    affected_sections_before: list[str] = Field(default_factory=list)
    affected_sections_after: list[str] = Field(default_factory=list)
    projected_can_apply: bool = False
    projected_requires_confirmation: bool = False
    warnings: list[str] = Field(default_factory=list)


class WorkspaceRouteRestoreHintsResponse(BaseModel):
    compare_view: Literal["papers", "themes", "report", "exports", "lineage", "rerun", "preset"] | None = None
    active_pane: Literal["paper", "report"] = "report"
    editing_session_id: str | None = None
    compare_partner_run_id: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    focus_paper_id: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)
    selection_source: Literal["manual", "compare_bucket", "preset", "suggested"] | None = None
    warnings: list[str] = Field(default_factory=list)


class StructuredPatchBlockResponse(BaseModel):
    block_id: str
    operation: Literal["context", "add", "remove", "replace", "append", "note"] = "context"
    anchor_before: PatchAnchorResponse = Field(default_factory=PatchAnchorResponse)
    anchor_after: PatchAnchorResponse = Field(default_factory=PatchAnchorResponse)
    before_text: str | None = None
    after_text: str | None = None
    before_excerpt: str | None = None
    after_excerpt: str | None = None
    related_citation_keys: list[str] = Field(default_factory=list)
    related_paper_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class StructuredPatchResponse(BaseModel):
    candidate_id: str | None = None
    merge_intent: WorkspaceMergeIntent
    source_run_id: str | None = None
    compare_partner_run_id: str | None = None
    source_section_id: str | None = None
    source_section_title: str | None = None
    target_section_id: str | None = None
    target_section_title: str | None = None
    patch_scope: Literal["section", "body", "note", "metadata"] = "section"
    destructive: bool = False
    requires_confirmation: bool = False
    completeness: Literal["full", "partial", "coarse", "metadata_only"] = "coarse"
    provenance: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    conflict_flags: list[str] = Field(default_factory=list)
    conflict_details: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    candidate_conflict_summary: WorkspaceCandidateConflictSummaryResponse = Field(default_factory=WorkspaceCandidateConflictSummaryResponse)
    target_resolution: WorkspaceTargetResolutionResponse = Field(default_factory=WorkspaceTargetResolutionResponse)
    blocks: list[StructuredPatchBlockResponse] = Field(default_factory=list)
    created_at: str | None = None


class MergeCandidateResponse(BaseModel):
    candidate_id: str
    editing_session_id: str | None = None
    source_run_id: str
    compare_partner_run_id: str | None = None
    workspace_view_id: str | None = None
    source_section_id: str | None = None
    source_section_title: str | None = None
    source_bucket_id: str | None = None
    source_theme: str | None = None
    source_paper_ids: list[str] = Field(default_factory=list)
    target_section_id: str | None = None
    target_section_title: str | None = None
    target_index: int | None = None
    merge_intent: WorkspaceMergeIntent
    preview_blocks: list[WorkspacePreviewBlockResponse] = Field(default_factory=list)
    changed_block_count: int = 0
    citation_delta: WorkspaceCitationDeltaResponse = Field(default_factory=WorkspaceCitationDeltaResponse)
    warnings: list[str] = Field(default_factory=list)
    conflict_flags: list[str] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
    destructive: bool = False
    requires_confirmation: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)
    completeness: Literal["full", "partial", "coarse", "metadata_only"] = "coarse"
    structured_patch: StructuredPatchResponse | None = None
    conflict_summary: WorkspaceCandidateConflictSummaryResponse = Field(default_factory=WorkspaceCandidateConflictSummaryResponse)
    request: WorkspaceActionRequest
    created_at: str
    order: int = 0


class WorkspaceStagedActionsResponse(BaseModel):
    run_id: str
    editing_session_id: str | None = None
    items: list[MergeCandidateResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceActionStageRequest(WorkspaceActionRequest):
    pass


class WorkspaceActionApplyStagedRequest(BaseModel):
    editing_session_id: str | None = None
    action_ids: list[str] = Field(default_factory=list)
    apply_all: bool = False
    confirm_destructive_action_ids: list[str] = Field(default_factory=list)


class WorkspaceActionApplyStagedResponse(BaseModel):
    run_id: str
    editing_session_id: str | None = None
    applied_count: int = 0
    applied_action_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    status: Literal["applied", "blocked", "partial", "noop"] = "noop"
    outline: OutlineResponse
    preview: PreviewResponse


class WorkspaceActionReorderRequest(BaseModel):
    editing_session_id: str | None = None
    ordered_action_ids: list[str] = Field(default_factory=list)


class WorkspaceSuggestionResponse(BaseModel):
    suggestion_id: str
    suggestion_type: Literal["report_delta", "theme_delta", "paper_seed", "rerun_context"] = "report_delta"
    title: str
    description: str
    action_seed: WorkspaceActionRequest
    related_paper_ids: list[str] = Field(default_factory=list)
    focus_theme: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    warnings: list[str] = Field(default_factory=list)


class WorkspaceSuggestionsResponse(BaseModel):
    run_id: str
    compare_partner_run_id: str | None = None
    items: list[WorkspaceSuggestionResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceViewActionSeedResponse(BaseModel):
    action_type: WorkspaceActionType
    source_section_id: str | None = None
    source_section_title: str | None = None
    target_section_id: str | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    source_bucket_id: str | None = None
    source_theme: str | None = None
    source_paper_ids: list[str] = Field(default_factory=list)
    payload: WorkspaceActionPayloadResponse = Field(default_factory=WorkspaceActionPayloadResponse)


class WorkspaceViewResponse(BaseModel):
    view_id: str
    run_id: str
    compare_partner_run_id: str | None = None
    title: str
    description: str | None = None
    active_compare_view: str | None = None
    compare_mode: str | None = None
    selection_bucket_id: str | None = None
    preset_id: str | None = None
    focus_theme: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    selection_source: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)
    pending_action_seed: WorkspaceViewActionSeedResponse | None = None
    report_tab_state: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class WorkspaceViewCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    compare_partner_run_id: str | None = None
    active_compare_view: str | None = None
    compare_mode: str | None = None
    selection_bucket_id: str | None = None
    preset_id: str | None = None
    focus_theme: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    selection_source: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)
    pending_action_seed: WorkspaceViewActionSeedResponse | None = None
    report_tab_state: dict[str, Any] = Field(default_factory=dict)


class WorkspaceViewsResponse(BaseModel):
    run_id: str
    items: list[WorkspaceViewResponse] = Field(default_factory=list)


class WorkspaceViewDeleteResponse(BaseModel):
    run_id: str
    view_id: str
    status: Literal["deleted"] = "deleted"


class EditingSessionResponse(BaseModel):
    session_id: str
    run_id: str
    title: str
    description: str | None = None
    workspace_view_id: str | None = None
    compare_partner_run_id: str | None = None
    active_compare_view: str | None = None
    compare_mode: str | None = None
    selection_bucket_id: str | None = None
    preset_id: str | None = None
    focus_theme: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    selection_source: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)
    staged_action_ids: list[str] = Field(default_factory=list)
    pending_merge_order: list[str] = Field(default_factory=list)
    report_tab_state: dict[str, Any] = Field(default_factory=dict)
    outline_draft_hash: str | None = None
    current_outline_draft_hash: str | None = None
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class EditingSessionCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    workspace_view_id: str | None = None
    compare_partner_run_id: str | None = None
    active_compare_view: str | None = None
    compare_mode: str | None = None
    selection_bucket_id: str | None = None
    preset_id: str | None = None
    focus_theme: str | None = None
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    preferred_report_side: Literal["left", "right", "both"] = "both"
    selection_source: str | None = None
    selected_paper_ids: list[str] = Field(default_factory=list)
    staged_action_ids: list[str] = Field(default_factory=list)
    pending_merge_order: list[str] = Field(default_factory=list)
    report_tab_state: dict[str, Any] = Field(default_factory=dict)
    outline_draft_hash: str | None = None


class EditingSessionsResponse(BaseModel):
    run_id: str
    items: list[EditingSessionResponse] = Field(default_factory=list)


class EditingSessionDeleteResponse(BaseModel):
    run_id: str
    session_id: str
    status: Literal["deleted"] = "deleted"


class WorkspaceApplyStagedPreflightRequest(BaseModel):
    editing_session_id: str | None = None
    action_ids: list[str] = Field(default_factory=list)
    apply_all: bool = False
    confirm_destructive_action_ids: list[str] = Field(default_factory=list)


class WorkspaceApplyStagedPreflightCandidateResponse(BaseModel):
    candidate_id: str
    merge_intent: WorkspaceMergeIntent
    target_section_id: str | None = None
    target_section_title: str | None = None
    target_index: int | None = None
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    destructive: bool = False
    requires_confirmation: bool = False
    changed_block_count: int = 0
    status: WorkspaceCandidateStatus = "safe"
    structured_patch: StructuredPatchResponse | None = None
    conflict_summary: WorkspaceCandidateConflictSummaryResponse = Field(default_factory=WorkspaceCandidateConflictSummaryResponse)


class WorkspacePreflightGroupResponse(BaseModel):
    group_key: str
    label: str
    severity: Literal["info", "warning", "blocker"] = "warning"
    reason: str
    candidate_ids: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    candidate_summaries: list[WorkspaceCandidateConflictSummaryResponse] = Field(default_factory=list)
    conflicts: list[WorkspaceConflictDetailResponse] = Field(default_factory=list)
    affected_section_ids: list[str] = Field(default_factory=list)
    related_paper_ids: list[str] = Field(default_factory=list)
    suggested_recovery_mode: WorkspaceRecoveryMode = "none"
    suggested_manual_actions: list[str] = Field(default_factory=list)


class WorkspacePreflightQueueConflictGroupResponse(BaseModel):
    conflict_type: Literal["duplicate_target_mutation", "stale_outline_hash", "missing_target_reference", "missing_source_reference", "reorder_risk", "destructive_confirmation"] = "duplicate_target_mutation"
    target_section_id: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    message: str


class WorkspaceRecommendedRecoveryPlanResponse(BaseModel):
    mode: WorkspaceRecoveryMode = "none"
    summary: str
    candidate_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkspaceApplyStagedPreflightResponse(BaseModel):
    run_id: str
    editing_session_id: str | None = None
    current_outline_hash: str | None = None
    session_outline_hash: str | None = None
    is_stale: bool = False
    status: Literal["ready", "ready_with_warnings", "blocked"] = "ready"
    candidate_order: list[str] = Field(default_factory=list)
    affected_section_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    destructive_candidates: list[str] = Field(default_factory=list)
    confirmation_required_ids: list[str] = Field(default_factory=list)
    safe_candidate_ids: list[str] = Field(default_factory=list)
    warn_candidate_ids: list[str] = Field(default_factory=list)
    blocked_candidate_ids: list[str] = Field(default_factory=list)
    rebaseable_candidate_ids: list[str] = Field(default_factory=list)
    grouped_warnings: list[WorkspacePreflightGroupResponse] = Field(default_factory=list)
    grouped_blockers: list[WorkspacePreflightGroupResponse] = Field(default_factory=list)
    queue_conflict_groups: list[WorkspacePreflightQueueConflictGroupResponse] = Field(default_factory=list)
    can_apply_all: bool = False
    recommended_recovery_mode: WorkspaceRecoveryMode = "none"
    recommended_recovery_plan: WorkspaceRecommendedRecoveryPlanResponse = Field(default_factory=lambda: WorkspaceRecommendedRecoveryPlanResponse(summary="No recovery is required."))
    items: list[WorkspaceApplyStagedPreflightCandidateResponse] = Field(default_factory=list)


class EditingSessionStalenessResponse(BaseModel):
    run_id: str
    session_id: str
    outline_hash_changed: bool = False
    missing_target_refs: list[str] = Field(default_factory=list)
    missing_source_refs: list[str] = Field(default_factory=list)
    reorder_risk: list[str] = Field(default_factory=list)
    deleted_staged_dependencies: list[str] = Field(default_factory=list)
    stale_reasons: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    can_rebase: bool = False
    can_refresh: bool = True
    can_discard_queue: bool = False
    warnings: list[str] = Field(default_factory=list)


class EditingSessionRecoverRequest(BaseModel):
    mode: Literal["refresh_session_metadata", "rebase_staged_candidates", "discard_staged_candidates"]


class EditingSessionRecoveryCandidateResultResponse(BaseModel):
    candidate_id: str
    merge_intent: WorkspaceMergeIntent
    result: Literal["preserved", "rebased", "dropped"] = "preserved"
    original_target_section_id: str | None = None
    resolved_target_section_id: str | None = None
    original_status: WorkspaceCandidateStatus = "safe"
    projected_status: WorkspaceCandidateStatus = "safe"
    target_resolution: WorkspaceTargetResolutionResponse = Field(default_factory=WorkspaceTargetResolutionResponse)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    original_structured_patch: StructuredPatchResponse | None = None
    structured_patch: StructuredPatchResponse | None = None
    original_conflict_summary: WorkspaceCandidateConflictSummaryResponse = Field(default_factory=WorkspaceCandidateConflictSummaryResponse)
    projected_conflict_summary: WorkspaceCandidateConflictSummaryResponse = Field(default_factory=WorkspaceCandidateConflictSummaryResponse)
    recovery_conflict_delta: WorkspaceRecoveryConflictDeltaResponse = Field(default_factory=lambda: WorkspaceRecoveryConflictDeltaResponse(candidate_id=""))
    rebase_diff: WorkspaceRebaseDiffResponse | None = None


class EditingSessionRecoveryPreviewResponse(BaseModel):
    run_id: str
    session_id: str
    recovery_mode: Literal["refresh_session_metadata", "rebase_staged_candidates", "discard_staged_candidates"]
    outline_hash_changed: bool = False
    candidate_results: list[EditingSessionRecoveryCandidateResultResponse] = Field(default_factory=list)
    preserved_candidate_ids: list[str] = Field(default_factory=list)
    rebased_candidate_ids: list[str] = Field(default_factory=list)
    dropped_candidate_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    projected_can_apply_all: bool = False
    projected_preflight_summary: WorkspaceApplyStagedPreflightResponse


class EditingSessionRecoverResponse(BaseModel):
    run_id: str
    session_id: str
    mode: Literal["refresh_session_metadata", "rebase_staged_candidates", "discard_staged_candidates"]
    recovered_candidate_ids: list[str] = Field(default_factory=list)
    discarded_candidate_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    session: EditingSessionResponse
    staged: WorkspaceStagedActionsResponse
    staleness: EditingSessionStalenessResponse


class OutlineActionHistoryAffectedSectionResponse(BaseModel):
    section_id: str
    title: str
    before_excerpt: str | None = None
    after_excerpt: str | None = None
    before_paper_ids: list[str] = Field(default_factory=list)
    after_paper_ids: list[str] = Field(default_factory=list)
    changed_block_count: int = 0
    completeness: Literal["full", "partial", "coarse", "metadata_only"] = "coarse"
    preview_blocks: list[WorkspacePreviewBlockResponse] = Field(default_factory=list)
    structured_patch: StructuredPatchResponse | None = None
    related_paper_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OutlineActionHistoryEntryResponse(BaseModel):
    action_id: str
    candidate_id: str | None = None
    editing_session_id: str | None = None
    merge_intent: WorkspaceMergeIntent
    source_run_id: str | None = None
    compare_partner_run_id: str | None = None
    source_section_id: str | None = None
    target_section_id: str | None = None
    timestamp: str
    outcome: Literal["applied", "partial", "blocked"] = "applied"
    warnings: list[str] = Field(default_factory=list)
    before_outline_hash: str | None = None
    after_outline_hash: str | None = None
    affected_sections: list[OutlineActionHistoryAffectedSectionResponse] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    preview_summary: list[str] = Field(default_factory=list)


class OutlineActionHistoryResponse(BaseModel):
    run_id: str
    items: list[OutlineActionHistoryEntryResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OutlineActionHistoryDiffResponse(BaseModel):
    run_id: str
    from_action_id: str | None = None
    to_action_id: str | None = None
    ready: bool = False
    mode: Literal["single_action", "between_actions", "action_vs_current_draft"] = "single_action"
    items: list[OutlineActionHistoryAffectedSectionResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OutlineActionHistoryFocusContextResponse(BaseModel):
    run_id: str
    action_id: str
    ready: bool = False
    editing_session_id: str | None = None
    compare_partner_run_id: str | None = None
    source_run_id: str | None = None
    affected_section_ids: list[str] = Field(default_factory=list)
    focus_section_id: str | None = None
    focus_section_title: str | None = None
    related_paper_ids: list[str] = Field(default_factory=list)
    focus_paper_id: str | None = None
    selection_source: Literal["manual", "compare_bucket", "preset", "suggested"] | None = None
    provenance_summary: list[str] = Field(default_factory=list)
    source_artifacts_available: bool = False
    route_restore_hints: WorkspaceRouteRestoreHintsResponse = Field(default_factory=WorkspaceRouteRestoreHintsResponse)
    warnings: list[str] = Field(default_factory=list)

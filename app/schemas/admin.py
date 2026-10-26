from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


OwnershipSource = Literal["run_creator", "admin_claim", "admin_assign", "lineage_claim"]
LegacyRunStatusFilter = Literal["unowned", "all"]
LegacyMigrationMode = Literal["claim_to_self", "assign_to_user"]
LegacyRunPreviewAction = Literal["claim", "assign", "skip_already_owned", "already_owned", "unchanged", "missing"]
OwnershipAuditBucketType = Literal[
    "unowned_runs",
    "mixed_ownership_lineage_families",
    "owned_runs_missing_ownership_source",
    "owned_runs_missing_owner_assigned_at",
    "owned_runs_missing_owner_assigned_by",
    "lineage_incomplete_families",
]
OwnershipAuditSeverity = Literal["info", "warn", "critical"]


class LegacyRunLineageSummaryResponse(BaseModel):
    parent_run_id: str | None = None
    root_run_id: str
    child_run_ids: list[str] = Field(default_factory=list)
    same_root_run_ids: list[str] = Field(default_factory=list)
    family_size: int = 1


class LegacyRunSummaryResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    created_at: str
    updated_at: str
    owner_user_id: str | None = None
    owner_assigned_at: str | None = None
    owner_assigned_by: str | None = None
    ownership_source: OwnershipSource | None = None
    is_unowned: bool
    lineage: LegacyRunLineageSummaryResponse


class LegacyRunsListResponse(BaseModel):
    status: LegacyRunStatusFilter = "unowned"
    q: str | None = None
    items: list[LegacyRunSummaryResponse] = Field(default_factory=list)
    total_count: int = 0
    unowned_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class LegacyRunsMigrationRequest(BaseModel):
    run_ids: list[str] = Field(default_factory=list, min_length=1)
    mode: LegacyMigrationMode = "claim_to_self"
    target_user_id: str | None = None
    include_lineage_family: bool = False
    skip_already_owned: bool = True

    @model_validator(mode="after")
    def _validate_target_user(self) -> "LegacyRunsMigrationRequest":
        if self.mode == "assign_to_user" and not str(self.target_user_id or "").strip():
            raise ValueError("assign_to_user mode requires target_user_id")
        return self


class LegacyOwnershipPreviewResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    action: LegacyRunPreviewAction
    explicitly_requested: bool
    included_via_lineage: bool
    current_owner_user_id: str | None = None
    projected_owner_user_id: str | None = None
    current_ownership_source: OwnershipSource | None = None
    projected_ownership_source: OwnershipSource | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class LegacyRunsMigrationResponse(BaseModel):
    requested_run_ids: list[str] = Field(default_factory=list)
    target_user_id: str | None = None
    effective_run_ids: list[str] = Field(default_factory=list)
    included_lineage_run_ids: list[str] = Field(default_factory=list)
    already_owned_run_ids: list[str] = Field(default_factory=list)
    missing_run_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    would_change_count: int = 0
    changed_count: int = 0
    ownership_preview: list[LegacyOwnershipPreviewResponse] = Field(default_factory=list)
    lineage_warnings: list[str] = Field(default_factory=list)


class OwnershipAuditProblemBucketResponse(BaseModel):
    bucket_id: str
    bucket_type: OwnershipAuditBucketType
    count: int
    sample_run_ids: list[str] = Field(default_factory=list)
    sample_root_run_ids: list[str] = Field(default_factory=list)
    explanation: str
    severity: OwnershipAuditSeverity
    recommended_action: str


class OwnershipAuditLineageStateCountsResponse(BaseModel):
    fully_unowned_family_count: int = 0
    fully_owned_single_owner_family_count: int = 0
    mixed_owned_and_unowned_family_count: int = 0
    mixed_multiple_owner_family_count: int = 0
    lineage_incomplete_family_count: int = 0


class OwnershipAuditResponse(BaseModel):
    generated_at: str
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    ownership_audit_version: str = "m12.0"
    total_runs: int = 0
    unowned_run_count: int = 0
    mixed_ownership_lineage_family_count: int = 0
    owned_runs_missing_ownership_source_count: int = 0
    owned_runs_missing_owner_assigned_at_count: int = 0
    owned_runs_missing_owner_assigned_by_count: int = 0
    lineage_incomplete_family_count: int = 0
    lineage_family_state_counts: OwnershipAuditLineageStateCountsResponse = Field(
        default_factory=OwnershipAuditLineageStateCountsResponse
    )
    problem_buckets: list[OwnershipAuditProblemBucketResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)

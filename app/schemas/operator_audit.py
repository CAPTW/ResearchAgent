from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

OperatorAuditActionType = Literal[
    "view_dashboard",
    "validate_topic_config",
    "save_topic_config",
    "validate_provider_config",
    "auth_metadata_validate",
    "audit_summary_view",
    "audit_replay_check",
    "oauth_authorization_preview",
    "oauth_authorization_prepare",
    "oauth_callback_dry_run",
    "oauth_state_validate",
    "oauth_token_exchange_status",
    "oauth_mock_exchange_validate",
    "oauth_account_link_preview",
    "oauth_account_link_save_mock",
    "oauth_account_link_list",
    "oauth_account_link_health",
    "oauth_account_link_revoke_placeholder",
    "oauth_session_binding_preview",
    "oauth_session_capability_policy",
    "oauth_flow_timeline_status",
    "oauth_live_exchange_readiness",
    "oauth_readiness_remediation",
    "oauth_preflight_package",
    "oauth_operator_acceptance_checklist",
    "oauth_live_exchange_pilot_status",
    "oauth_live_pilot_evidence_package",
    "oauth_live_pilot_replay_verification",
    "oauth_real_provider_smoke_status",
    "oauth_real_provider_smoke",
    "oauth_smoke_result_summary",
    "oauth_smoke_evidence_freeze_preview",
    "oauth_smoke_replay_verification",
    "provider_orchestration_safety_matrix",
    "provider_orchestration_dry_run_plan",
    "provider_orchestration_replay_verification",
    "provider_orchestration_fixture_execution_preview",
    "provider_orchestration_fixture_result_replay",
    "provider_orchestration_fixture_result_summary",
    "oauth_live_exchange_pilot",
    "oauth_live_exchange_disabled",
    "provider_readiness_check",
    "prepare_provider_manual_run",
    "provider_acceptance_smoke",
    "provider_risk_acceptance",
    "secret_policy_rejection",
    "capability_denied",
    "capability_allowed",
]
OperatorAuditDecision = Literal["allowed", "denied", "preview_only", "dry_run_only", "rejected"]
OperatorAuditRiskLevel = Literal["read_only", "config_write", "provider_prepare", "provider_live_cli", "oauth_live_pilot", "secret_policy"]


class OperatorAuditEventResponse(BaseModel):
    auditEventId: str
    timestamp: str
    operatorId: str = "unknown"
    authMode: str = "disabled"
    actionType: OperatorAuditActionType
    capabilityRequired: str | None = None
    capabilityPresent: bool | None = None
    decision: OperatorAuditDecision
    reasonCodes: list[str] = Field(default_factory=list)
    targetType: str | None = None
    targetId: str | None = None
    riskLevel: OperatorAuditRiskLevel = "read_only"
    providerConfigId: str | None = None
    providerOrigin: str | None = None
    secretPresent: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorAuditEventSummaryResponse(BaseModel):
    auditEventId: str
    actionType: str
    decision: str
    targetType: str | None = None
    targetId: str | None = None
    riskLevel: str
    reasonCodes: list[str] = Field(default_factory=list)


class OperatorAuditSummaryResponse(BaseModel):
    totalEvents: int = 0
    allowedCount: int = 0
    deniedCount: int = 0
    secretPolicyRejectionCount: int = 0
    providerRiskAcceptanceCount: int = 0
    configWriteCount: int = 0
    latestEventSummaries: list[OperatorAuditEventSummaryResponse] = Field(default_factory=list)


class OperatorAuditReplaySummaryResponse(BaseModel):
    auditLogStatus: Literal["unavailable", "empty", "valid", "warnings", "errors"] = "empty"
    validEventCount: int = 0
    invalidEventCount: int = 0
    warningCount: int = 0
    secretLikeViolationCount: int = 0
    missingRequiredFieldCount: int = 0
    inconsistentCapabilityCount: int = 0
    latestCheckedEventId: str | None = None
    reasonCounts: dict[str, int] = Field(default_factory=dict)

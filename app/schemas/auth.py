from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.operator_audit import OperatorAuditReplaySummaryResponse, OperatorAuditSummaryResponse

AuthFoundationMode = Literal["disabled", "local_operator", "oauth_ready"]
OperatorRole = Literal["viewer", "operator", "local_operator", "admin"]
OAuthAccountLinkSource = Literal["mock_exchange_validation", "dry_run_preview", "future_live_exchange"]
OAuthAccountLinkStatus = Literal["preview", "linked_mock", "linked_live_pilot", "disabled", "invalid", "revoked_placeholder"]
OAuthAccountLinkHealthStatus = Literal["none", "mock_linked", "live_pilot_linked", "disabled", "invalid"]
OAuthSessionBindingStatus = Literal["unbound", "local_mock_bound", "disabled"]
OAuthAccountLinkStoreHealthStatus = Literal["empty", "valid", "warnings", "errors"]
OAuthAccountLinkInfluence = Literal["display_only", "none", "future_policy_placeholder"]
OAuthFlowTimelineStatus = Literal["empty", "metadata_only", "mock_linked", "disabled"]
OAuthFlowTimelineStepStatus = Literal["available", "disabled", "metadata_only", "mock_only"]
OAuthLiveExchangeReadinessStatus = Literal["ready_for_future_live_exchange", "blocked", "warnings_only", "disabled"]
OAuthLiveExchangeReadinessCheckStatus = Literal["passed", "failed", "warning", "disabled"]
OAuthReadinessRemediationSeverity = Literal["info", "warning", "blocking"]
OAuthReadinessRemediationCategory = Literal[
    "metadata",
    "env_var",
    "redirect_uri",
    "state_store",
    "audit_health",
    "account_link_health",
    "session_policy",
    "dashboard_safety",
    "future_live_exchange",
]
OAuthReadinessRemediationStatus = Literal["clear", "warnings", "blocking"]
OAuthPreflightCheckStatus = Literal["passed", "warning", "blocking", "info"]
OAuthPreflightStatus = Literal["ready", "blocked", "warnings", "disabled"]
OAuthLiveExchangePilotGateStatus = Literal["passed", "blocked", "dry_run_only", "failed"]
OAuthLiveExchangePilotStatus = Literal["disabled_by_default", "available_explicit_api_only"]
OAuthLivePilotLeakCheckStatus = Literal["not_run", "clean", "redacted", "blocked"]
OAuthLivePilotEvidenceItemStatus = Literal["present", "warning", "missing", "failed"]
OAuthLivePilotReplayVerificationStatus = Literal["valid", "warnings", "failed"]
OAuthRealProviderSmokeStatus = Literal["not_configured", "skipped", "ready_for_operator_api", "completed_redacted", "blocked", "failed", "dry_run_only"]
OAuthSmokeCapturedStatus = Literal["no_result", "skipped_not_configured", "blocked", "completed_mocked", "completed_real_redacted", "failed_redacted"]
OAuthSmokeFreezeStatus = Literal["valid", "warnings", "failed"]
OAuthReadOnlyProofLevel = Literal[
    "disabled_default",
    "config_missing",
    "local_metadata_valid",
    "mock_exchange_passed",
    "public_https_missing",
    "idp_missing",
    "live_smoke_blocked",
    "read_only_connector_smoke_passed",
    "failed",
]
OAuthReadOnlyProofStatus = Literal["safe", "passed", "blocked", "failed"]
ProviderOrchestrationAvailability = Literal["available", "preview_only", "cli_only", "blocked", "future_disabled"]
ProviderOrchestrationMatrixStatus = Literal["valid", "warnings", "blocked"]
ProviderOrchestrationWorkflow = Literal[
    "review_with_stub_provider",
    "review_with_fixture_command",
    "manual_provider_package_import",
    "command_provider_dry_run",
    "command_provider_live_cli_preview",
    "real_provider_review_future_disabled",
]
ProviderOrchestrationPlannedMode = Literal[
    "read_only",
    "dry_run",
    "fixture_only",
    "manual_import",
    "cli_only",
    "blocked",
    "future_disabled",
]
ProviderOrchestrationReplayStatus = Literal["valid", "warnings", "failed"]
ProviderOrchestrationExecutionStatus = Literal["executed_fixture", "preview_only", "skipped", "blocked", "rejected"]
OperatorCapability = Literal[
    "view_dashboard",
    "edit_topic_config",
    "preview_run_plan",
    "prepare_provider_manual_run",
    "run_fixture_provider",
    "run_real_provider_cli",
    "accept_provider_risk",
    "save_provider_config",
    "view_provider_readiness",
]


class AuthUserResponse(BaseModel):
    user_id: str
    display_name: str
    email: str | None = None
    auth_source: Literal["disabled", "oidc"] = "disabled"


class AuthSessionResponse(BaseModel):
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    authenticated: bool = True
    is_admin: bool = False
    user: AuthUserResponse | None = None
    login_url: str | None = None
    logout_url: str | None = None
    mcp_url: str | None = None
    mcp_resource_metadata_url: str | None = None
    product_auth_server_side_only: bool = True
    provider_auth_server_side_only: bool = True
    product_session_backend: Literal["local_internal", "server_side_file"] = "local_internal"
    session_configuration_status: Literal["not_required", "valid", "invalid"] = "not_required"
    session_configuration_reason_code: (
        Literal[
            "session_secret_missing",
            "session_secret_too_short",
            "session_secret_disallowed",
        ]
        | None
    ) = None
    warnings: list[str] = Field(default_factory=list)


class LogoutResponse(BaseModel):
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    logged_out: bool = True


class OperatorIdentityResponse(BaseModel):
    operatorId: str = "local-internal-user"
    displayName: str = "Local Internal Mode"
    role: OperatorRole = "local_operator"
    capabilities: list[OperatorCapability] = Field(default_factory=list)


class OAuthProviderMetadataResponse(BaseModel):
    providerId: str = ""
    displayName: str = ""
    issuer: str | None = None
    authorizationUrl: str | None = None
    tokenUrl: str | None = None
    scopes: list[str] = Field(default_factory=list)
    clientIdEnvVar: str | None = None
    clientSecretEnvVar: str | None = None
    enabled: bool = False
    notes: list[str] = Field(default_factory=list)
    tokenStorageEnabled: bool = False
    liveTokenExchangeEnabled: bool = False


class SecretPolicyValidationResponse(BaseModel):
    valid: bool = True
    rejectedFields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)


class OAuthProviderMetadataDraftRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    providerId: str | None = None
    displayName: str | None = None
    issuer: str | None = None
    authorizationUrl: str | None = None
    tokenUrl: str | None = None
    scopes: list[str] | str | None = None
    clientIdEnvVar: str | None = None
    clientSecretEnvVar: str | None = None
    enabled: bool | None = None
    notes: list[str] | str | None = None


class OAuthProviderMetadataValidationResponse(BaseModel):
    providerId: str = ""
    validationStatus: Literal["valid", "invalid"] = "valid"
    oauthProviderMetadata: OAuthProviderMetadataResponse = Field(default_factory=OAuthProviderMetadataResponse)
    secretPolicy: SecretPolicyValidationResponse = Field(default_factory=SecretPolicyValidationResponse)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    configHash: str = ""


class OAuthPkceChallengeResponse(BaseModel):
    codeChallenge: str = ""
    codeChallengeMethod: Literal["S256"] = "S256"


class OAuthAuthorizationUrlRequest(BaseModel):
    oauthProviderMetadata: OAuthProviderMetadataDraftRequest = Field(default_factory=OAuthProviderMetadataDraftRequest)
    redirectUri: str
    ttlSeconds: int = Field(default=600, ge=60, le=3600)


class OAuthAuthorizationUrlResponse(BaseModel):
    providerId: str = ""
    validationStatus: Literal["preview", "prepared", "invalid"] = "preview"
    authorizationUrl: str | None = None
    redirectUri: str = ""
    scopes: list[str] = Field(default_factory=list)
    state: str = ""
    nonce: str = ""
    pkce: OAuthPkceChallengeResponse = Field(default_factory=OAuthPkceChallengeResponse)
    createdAt: str = ""
    expiresAt: str = ""
    correlationId: str = ""
    stateStored: bool = False
    clientIdStatus: Literal["resolved", "missing_env"] = "missing_env"
    tokenExchangeEnabled: bool = False
    tokenPersistenceEnabled: bool = False
    externalAuthCallPerformed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class OAuthCallbackDryRunRequest(BaseModel):
    providerId: str
    state: str | None = None
    code: str | None = None
    error: str | None = None
    redirectUri: str | None = None


class OAuthCallbackDryRunResponse(BaseModel):
    providerId: str = ""
    acceptedForTokenExchange: bool = False
    tokenExchangePerformed: bool = False
    stateStatus: Literal["valid", "missing", "expired", "replayed", "unknown"] = "unknown"
    correlationId: str | None = None
    secretPresent: bool = False
    externalAuthCallPerformed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthDryRunStatusResponse(BaseModel):
    oauthMode: Literal["metadata_only", "dry_run_ready"] = "metadata_only"
    tokenExchangeEnabled: bool = False
    tokenPersistenceEnabled: bool = False
    externalAuthCallsEnabled: bool = False
    stateStoreAvailable: bool = False
    pendingStateCount: int = 0
    expiredStateCount: int = 0
    usedStateCount: int = 0
    malformedStateCount: int = 0
    stateStorage: Literal["local_ignored"] = "local_ignored"
    warnings: list[str] = Field(default_factory=list)


class OAuthReadOnlyProofResponse(BaseModel):
    proofLevel: OAuthReadOnlyProofLevel = "disabled_default"
    status: OAuthReadOnlyProofStatus = "safe"
    authMode: Literal["disabled", "oidc"] = "disabled"
    issuer: str | None = None
    resourceServerUrl: str | None = None
    mcpBaseUrl: str | None = None
    publicHttpsReady: bool = False
    idpConfigured: bool = False
    metadataValid: bool = False
    metadataRequested: bool = False
    mockExchangeRequested: bool = False
    mockExchangePassed: bool = False
    liveSmokeRequested: bool = False
    liveSmokeAllowed: bool = False
    readOnlyRoutesChecked: list[str] = Field(default_factory=list)
    protectedWriteRoutesPresent: list[str] = Field(default_factory=list)
    writeRoutesExposed: bool = False
    tokenStorageEnabled: bool = False
    secretStorageDetected: bool = False
    dashboardTriggerAllowed: bool = False
    reason: str = ""
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    createdAt: str = ""

    @field_validator("writeRoutesExposed", "tokenStorageEnabled", "secretStorageDetected", "dashboardTriggerAllowed")
    @classmethod
    def _oauth_readonly_proof_forbidden_flags_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth read-only proof must not expose write routes, store secrets/tokens, or allow dashboard live triggers.")
        return False


class OAuthTokenExchangeStatusResponse(BaseModel):
    exchangeMode: Literal["mock_only", "disabled", "live_not_supported"] = "mock_only"
    mockExchangeSupported: bool = True
    liveExchangeSupported: bool = False
    tokenStorageEnabled: bool = False
    accountLinkPreviewEnabled: bool = True
    externalAuthCallsEnabled: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthTokenExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    providerId: str
    correlationId: str | None = None
    authorizationCodePresent: bool = False
    stateStatus: Literal["valid", "missing", "expired", "replayed", "unknown"] = "unknown"
    redirectUri: str | None = None
    codeVerifierPresent: bool = False
    exchangeMode: Literal["mock_only", "disabled", "live_not_supported"] = "mock_only"
    mockTokenResponse: dict[str, Any] | None = None


class OAuthTokenValidationSummaryResponse(BaseModel):
    valid: bool = False
    tokenResponsePresent: bool = False
    accessTokenPresent: bool = False
    refreshTokenPresent: bool = False
    idTokenPresent: bool = False
    expiresIn: int | None = None
    scopes: list[str] = Field(default_factory=list)
    subjectHash: str | None = None
    reasonCodes: list[str] = Field(default_factory=list)


class OAuthAccountLinkPreviewResponse(BaseModel):
    providerId: str = ""
    subjectHash: str | None = None
    scopes: list[str] = Field(default_factory=list)
    linkedAt: str | None = None
    tokenStorageEnabled: bool = False
    linkStored: bool = False


class OAuthAccountLinkMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    accountLinkId: str | None = None
    providerId: str
    operatorId: str | None = None
    subjectHash: str | None = None
    displaySubjectHint: str | None = None
    scopes: list[str] | str | None = None
    linkedAt: str | None = None
    lastValidatedAt: str | None = None
    tokenStorageEnabled: bool = False
    liveTokenExchangePerformed: bool = False
    source: OAuthAccountLinkSource = "mock_exchange_validation"
    status: OAuthAccountLinkStatus | None = None
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthAccountLinkMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accountLinkId: str
    providerId: str
    operatorId: str
    subjectHash: str | None = None
    displaySubjectHint: str | None = None
    scopes: list[str] = Field(default_factory=list)
    linkedAt: str | None = None
    lastValidatedAt: str | None = None
    tokenStorageEnabled: bool = False
    liveTokenExchangePerformed: bool = False
    source: OAuthAccountLinkSource = "mock_exchange_validation"
    status: OAuthAccountLinkStatus = "preview"
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("tokenStorageEnabled", "liveTokenExchangePerformed")
    @classmethod
    def _oauth_account_link_flags_must_remain_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth account link metadata must not enable token storage or live token exchange.")
        return False


class OAuthAccountLinkHealthResponse(BaseModel):
    status: OAuthAccountLinkHealthStatus = "none"
    linkedAccountCount: int = 0
    activeLinkCount: int = 0
    mockLinkCount: int = 0
    liveLinkCount: int = 0
    invalidLinkCount: int = 0
    revokedPlaceholderCount: int = 0
    tokenLeakViolationCount: int = 0
    liveExchangeViolationCount: int = 0
    duplicateLinkCount: int = 0
    healthStatus: OAuthAccountLinkStoreHealthStatus = "empty"
    tokenStorageEnabled: bool = False
    latestLinkedAt: str | None = None
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthSessionCapabilityPolicyResponse(BaseModel):
    effectiveCapabilities: list[OperatorCapability] = Field(default_factory=list)
    capabilitySources: dict[str, list[str]] = Field(default_factory=dict)
    deniedByPolicy: list[str] = Field(default_factory=list)
    accountLinkInfluence: OAuthAccountLinkInfluence = "none"
    accountLinkCount: int = 0
    accountLinkGrantsProviderExecution: bool = False
    providerExecution: Literal["cli_only_acceptance_gated"] = "cli_only_acceptance_gated"
    tokenStorageEnabled: bool = False
    liveTokenExchangePerformed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthSessionBindingResponse(BaseModel):
    bindingStatus: OAuthSessionBindingStatus = "unbound"
    operatorId: str = "anonymous"
    role: OperatorRole = "local_operator"
    providerIds: list[str] = Field(default_factory=list)
    accountLinkCount: int = 0
    capabilities: list[OperatorCapability] = Field(default_factory=list)
    accountLinkGrantsProviderExecution: bool = False
    providerExecution: Literal["cli_only_acceptance_gated"] = "cli_only_acceptance_gated"
    tokenStorageEnabled: bool = False
    liveTokenExchangePerformed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthAccountLinkOperationResponse(BaseModel):
    accepted: bool = False
    validationStatus: Literal["preview", "saved", "invalid"] = "preview"
    accountLink: OAuthAccountLinkMetadataResponse | None = None
    health: OAuthAccountLinkHealthResponse = Field(default_factory=OAuthAccountLinkHealthResponse)
    sessionBinding: OAuthSessionBindingResponse = Field(default_factory=OAuthSessionBindingResponse)
    linkStored: bool = False
    tokenStorageEnabled: bool = False
    liveTokenExchangePerformed: bool = False
    externalAuthCallPerformed: bool = False
    tokenRevocationPerformed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthAccountLinkListResponse(BaseModel):
    items: list[OAuthAccountLinkMetadataResponse] = Field(default_factory=list)
    health: OAuthAccountLinkHealthResponse = Field(default_factory=OAuthAccountLinkHealthResponse)


class OAuthTokenRedactionSummaryResponse(BaseModel):
    redactedFields: list[str] = Field(default_factory=list)
    redactedTokenFieldCount: int = 0
    authorizationCodeRedacted: bool = True
    rawTokenStored: bool = False


class OAuthTokenExchangeResponse(BaseModel):
    exchangePerformed: bool = False
    tokenExchangePerformed: bool = False
    exchangeMode: Literal["mock_only", "disabled", "live_not_supported"] = "mock_only"
    providerId: str = ""
    correlationId: str | None = None
    accepted: bool = False
    tokenResponsePresent: bool = False
    accessTokenPresent: bool = False
    refreshTokenPresent: bool = False
    idTokenPresent: bool = False
    expiresIn: int | None = None
    scopes: list[str] = Field(default_factory=list)
    subjectHash: str | None = None
    rawTokenStored: bool = False
    validationSummary: OAuthTokenValidationSummaryResponse = Field(default_factory=OAuthTokenValidationSummaryResponse)
    accountLinkPreview: OAuthAccountLinkPreviewResponse = Field(default_factory=OAuthAccountLinkPreviewResponse)
    redactionSummary: OAuthTokenRedactionSummaryResponse = Field(default_factory=OAuthTokenRedactionSummaryResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthFlowTimelineStepResponse(BaseModel):
    stepId: str
    label: str
    status: OAuthFlowTimelineStepStatus
    tokenStorageEnabled: bool = False
    liveTokenExchangeEnabled: bool = False
    externalAuthCallPerformed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthFlowTimelineResponse(BaseModel):
    status: OAuthFlowTimelineStatus = "metadata_only"
    tokenStorageEnabled: bool = False
    realTokenExchangeEnabled: bool = False
    externalAuthCallsEnabled: bool = False
    steps: list[OAuthFlowTimelineStepResponse] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthLiveExchangeReadinessCheckResponse(BaseModel):
    checkId: str
    label: str
    status: OAuthLiveExchangeReadinessCheckStatus
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthLiveExchangeReadinessRequest(BaseModel):
    oauthProviderMetadata: OAuthProviderMetadataDraftRequest = Field(default_factory=OAuthProviderMetadataDraftRequest)
    redirectUri: str | None = None


class OAuthLiveExchangeReadinessResponse(BaseModel):
    readinessId: str = "oauth-live-exchange-readiness-oauth-provider"
    providerId: str = "oauth-provider"
    readinessStatus: OAuthLiveExchangeReadinessStatus = "blocked"
    liveExchangeEnabled: bool = False
    tokenStorageEnabled: bool = False
    externalAuthCallPerformed: bool = False
    checks: list[OAuthLiveExchangeReadinessCheckResponse] = Field(default_factory=list)
    failedChecks: list[str] = Field(default_factory=list)
    warningChecks: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)
    forbiddenActions: list[str] = Field(default_factory=list)

    @field_validator("liveExchangeEnabled", "tokenStorageEnabled", "externalAuthCallPerformed")
    @classmethod
    def _oauth_live_readiness_flags_must_remain_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live exchange readiness must not enable live exchange, token storage, or external auth calls.")
        return False


class OAuthReadinessRemediationItemResponse(BaseModel):
    remediationId: str
    providerId: str = "oauth-provider"
    readinessCheck: str
    reasonCode: str
    severity: OAuthReadinessRemediationSeverity = "info"
    category: OAuthReadinessRemediationCategory = "future_live_exchange"
    operatorAction: str
    safeCommandOrConfigHint: str | None = None
    envVarNames: list[str] = Field(default_factory=list)
    docsAnchor: str = ""
    forbiddenActions: list[str] = Field(default_factory=list)
    canBeResolvedWithoutCodeChange: bool = True
    requiresRestart: bool = False
    secretHandlingNote: str = (
        "Env var values, OAuth tokens, authorization codes, API keys, and provider credentials must stay out of committed "
        "files, API responses, audit logs, and account-link metadata."
    )


class OAuthReadinessRemediationSummaryResponse(BaseModel):
    providerId: str = "oauth-provider"
    readinessStatus: OAuthLiveExchangeReadinessStatus = "blocked"
    remediationStatus: OAuthReadinessRemediationStatus = "blocking"
    blockingRemediationCount: int = 0
    warningRemediationCount: int = 0
    infoRemediationCount: int = 0
    liveExchangeEnabled: bool = False
    tokenStorageEnabled: bool = False
    items: list[OAuthReadinessRemediationItemResponse] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)
    forbiddenActions: list[str] = Field(default_factory=list)

    @field_validator("liveExchangeEnabled", "tokenStorageEnabled")
    @classmethod
    def _oauth_remediation_flags_must_remain_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth readiness remediation must not enable live exchange or token storage.")
        return False


class OAuthPreflightCheckResponse(BaseModel):
    checkId: str
    label: str
    status: OAuthPreflightCheckStatus = "info"
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthPreflightSummaryResponse(BaseModel):
    providerId: str = "oauth-provider"
    preflightStatus: OAuthPreflightStatus = "blocked"
    blockingCheckCount: int = 0
    warningCheckCount: int = 0
    requiredConfirmationCount: int = 0
    acceptanceReady: bool = False
    tokenStorageEnabled: bool = False
    liveExchangeEnabled: bool = False

    @field_validator("tokenStorageEnabled", "liveExchangeEnabled")
    @classmethod
    def _oauth_preflight_summary_flags_must_remain_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth preflight summary must not enable live exchange or token storage.")
        return False


class OAuthPreflightPackageResponse(BaseModel):
    preflightId: str = "oauth-preflight-oauth-provider"
    providerId: str = "oauth-provider"
    generatedAt: str = ""
    readinessStatus: OAuthLiveExchangeReadinessStatus = "blocked"
    remediationSummary: OAuthReadinessRemediationSummaryResponse = Field(default_factory=OAuthReadinessRemediationSummaryResponse)
    auditHealthStatus: Literal["unavailable", "empty", "valid", "warnings", "errors"] = "empty"
    accountLinkHealthStatus: OAuthAccountLinkStoreHealthStatus = "empty"
    tokenStorageEnabled: bool = False
    liveExchangeEnabled: bool = False
    envVarNames: list[str] = Field(default_factory=list)
    envVarValuesExposed: bool = False
    checks: list[OAuthPreflightCheckResponse] = Field(default_factory=list)
    summary: OAuthPreflightSummaryResponse = Field(default_factory=OAuthPreflightSummaryResponse)
    forbiddenActions: list[str] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)
    requiredOperatorConfirmations: list[str] = Field(default_factory=list)
    acceptanceReady: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("tokenStorageEnabled", "liveExchangeEnabled", "envVarValuesExposed")
    @classmethod
    def _oauth_preflight_flags_must_remain_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth preflight package must not enable live exchange, token storage, or env var value exposure.")
        return False


class OAuthOperatorAcceptanceChecklistRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    providerId: str = "oauth-provider"
    confirmedNoSecretCommit: bool = False
    confirmedEnvVarsConfigured: bool = False
    confirmedRedirectUriRegistered: bool = False
    confirmedAuditHealthReviewed: bool = False
    confirmedTokenStoragePolicy: bool = False
    confirmedDashboardExecutionDisabled: bool = False
    confirmedProviderAcceptanceGateIndependent: bool = False
    acceptedByOperatorId: str | None = None
    acceptedAt: str | None = None


class OAuthOperatorAcceptanceChecklistResponse(BaseModel):
    providerId: str = "oauth-provider"
    confirmedNoSecretCommit: bool = False
    confirmedEnvVarsConfigured: bool = False
    confirmedRedirectUriRegistered: bool = False
    confirmedAuditHealthReviewed: bool = False
    confirmedTokenStoragePolicy: bool = False
    confirmedDashboardExecutionDisabled: bool = False
    confirmedProviderAcceptanceGateIndependent: bool = False
    acceptedByOperatorId: str | None = None
    acceptedAt: str | None = None
    acceptanceValid: bool = False
    missingConfirmations: list[str] = Field(default_factory=list)
    liveExchangeStillDisabled: bool = True
    liveExchangeEnabled: bool = False
    tokenStorageEnabled: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("liveExchangeStillDisabled")
    @classmethod
    def _oauth_checklist_live_exchange_stays_disabled(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth checklist validation must report live exchange as still disabled.")
        return True

    @field_validator("liveExchangeEnabled", "tokenStorageEnabled")
    @classmethod
    def _oauth_checklist_flags_must_remain_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth checklist validation must not enable live exchange or token storage.")
        return False


class OAuthLiveExchangePilotRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    providerId: str = "oauth-provider"
    correlationId: str | None = None
    oauthProviderMetadata: OAuthProviderMetadataDraftRequest = Field(default_factory=OAuthProviderMetadataDraftRequest)
    redirectUri: str | None = None
    authorizationCodePresent: bool = False
    authorizationCode: str | None = None
    stateId: str | None = None
    stateStatus: Literal["valid", "missing", "expired", "replayed", "unknown"] = "unknown"
    codeVerifierPresent: bool = False
    codeVerifier: str | None = None
    preflightId: str | None = None
    checklistId: str | None = None
    checklist: OAuthOperatorAcceptanceChecklistRequest = Field(default_factory=OAuthOperatorAcceptanceChecklistRequest)
    operatorId: str | None = None
    operatorAcceptancePresent: bool = False
    liveExchangeRequested: bool = False
    tokenStorageRequested: bool = False
    dryRunOnly: bool = True
    dashboardExecutionContext: bool = False


class OAuthLiveExchangeGateSummaryResponse(BaseModel):
    gateStatus: OAuthLiveExchangePilotGateStatus = "blocked"
    providerId: str = "oauth-provider"
    correlationId: str | None = None
    tokenEndpointHttps: bool = False
    preflightValid: bool = False
    checklistValid: bool = False
    operatorAcceptancePresent: bool = False
    liveExchangeRequested: bool = False
    authorizationCodePresent: bool = False
    stateAccepted: bool = False
    codeVerifierPresent: bool = False
    tokenStorageEnabled: bool = False
    dashboardExecutionContext: bool = False
    failedChecks: list[str] = Field(default_factory=list)
    warningChecks: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("tokenStorageEnabled", "dashboardExecutionContext")
    @classmethod
    def _oauth_live_gate_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live pilot gate must not enable token storage or dashboard execution.")
        return False


class OAuthLiveExchangeRedactionSummaryResponse(BaseModel):
    redactedFields: list[str] = Field(default_factory=list)
    redactedTokenFieldCount: int = 0
    leakDetected: bool = False
    leakCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    warningCount: int = 0
    authorizationCodeRedacted: bool = True
    rawTokenStored: bool = False
    tokenValuesReturned: bool = False
    tokenValuesAudited: bool = False

    @field_validator("authorizationCodeRedacted")
    @classmethod
    def _oauth_live_authorization_code_redacted(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth authorization codes must be treated as redacted.")
        return True

    @field_validator("rawTokenStored", "tokenValuesReturned", "tokenValuesAudited")
    @classmethod
    def _oauth_live_redaction_flags_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live pilot must not store, return, or audit token values.")
        return False


class OAuthLiveExchangePilotResultResponse(BaseModel):
    liveExchangePerformed: bool = False
    providerId: str = "oauth-provider"
    correlationId: str | None = None
    gateStatus: OAuthLiveExchangePilotGateStatus = "blocked"
    tokenEndpointCalled: bool = False
    tokenResponsePresent: bool = False
    accessTokenPresent: bool = False
    refreshTokenPresent: bool = False
    idTokenPresent: bool = False
    rawTokenStored: bool = False
    tokenValuesReturned: bool = False
    tokenValuesAudited: bool = False
    accountLinkCreated: bool = False
    accountLinkMetadataOnly: bool = True
    accountLink: OAuthAccountLinkMetadataResponse | None = None
    gateSummary: OAuthLiveExchangeGateSummaryResponse = Field(default_factory=OAuthLiveExchangeGateSummaryResponse)
    redactionSummary: OAuthLiveExchangeRedactionSummaryResponse = Field(default_factory=OAuthLiveExchangeRedactionSummaryResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("rawTokenStored", "tokenValuesReturned", "tokenValuesAudited")
    @classmethod
    def _oauth_live_pilot_secret_flags_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live pilot must not store, return, or audit token values.")
        return False

    @field_validator("accountLinkMetadataOnly")
    @classmethod
    def _oauth_live_pilot_account_link_metadata_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth live pilot account link output must remain metadata-only.")
        return True


class OAuthLiveExchangePilotStatusResponse(BaseModel):
    pilotStatus: OAuthLiveExchangePilotStatus = "disabled_by_default"
    liveExchangeEnabledByDefault: bool = False
    tokenStorageEnabled: bool = False
    dashboardExecutionEnabled: bool = False
    tokenStorageStillDisabled: bool = True
    dashboardExecutionStillDisabled: bool = True
    explicitOperatorAcceptanceRequired: bool = True
    tokenValuesReturned: bool = False
    tokenValuesAudited: bool = False
    livePilotLeakCheckStatus: OAuthLivePilotLeakCheckStatus = "not_run"
    latestRedactionSummary: OAuthLiveExchangeRedactionSummaryResponse = Field(default_factory=OAuthLiveExchangeRedactionSummaryResponse)
    lastPilotStatus: str | None = None
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("liveExchangeEnabledByDefault", "tokenStorageEnabled", "dashboardExecutionEnabled", "tokenValuesReturned", "tokenValuesAudited")
    @classmethod
    def _oauth_live_pilot_status_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live pilot status must keep default exchange, token storage, dashboard execution, and token value exposure disabled.")
        return False

    @field_validator("explicitOperatorAcceptanceRequired", "tokenStorageStillDisabled", "dashboardExecutionStillDisabled")
    @classmethod
    def _oauth_live_pilot_status_requires_safety_flags(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth live pilot requires explicit acceptance and disabled token storage/dashboard execution.")
        return True


class OAuthLivePilotEvidenceItemResponse(BaseModel):
    itemId: str
    category: str
    label: str
    status: OAuthLivePilotEvidenceItemStatus = "present"
    summary: str = ""
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthLivePilotReplayVerificationResponse(BaseModel):
    verificationId: str = "oauth-live-pilot-replay-oauth-provider"
    providerId: str = "oauth-provider"
    verificationStatus: OAuthLivePilotReplayVerificationStatus = "failed"
    failedChecks: list[str] = Field(default_factory=list)
    warningChecks: list[str] = Field(default_factory=list)
    leakCount: int = 0
    secretLikeViolationCount: int = 0
    missingRequiredEvidenceCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    redactedPaths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthLivePilotEvidenceSummaryResponse(BaseModel):
    providerId: str = "oauth-provider"
    readinessStatus: OAuthLiveExchangeReadinessStatus = "blocked"
    preflightStatus: OAuthPreflightStatus = "blocked"
    checklistValid: bool = False
    auditHealthStatus: Literal["unavailable", "empty", "valid", "warnings", "errors"] = "empty"
    accountLinkHealthStatus: OAuthAccountLinkStoreHealthStatus = "empty"
    leakCheckStatus: Literal["clean", "warnings", "failed"] = "clean"
    gateStatus: OAuthLiveExchangePilotGateStatus = "dry_run_only"
    verificationStatus: OAuthLivePilotReplayVerificationStatus = "failed"
    tokenStorageEnabled: bool = False
    dashboardExecutionEnabled: bool = False
    livePilotEnabledDefault: bool = False

    @field_validator("tokenStorageEnabled", "dashboardExecutionEnabled", "livePilotEnabledDefault")
    @classmethod
    def _oauth_live_evidence_summary_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live pilot evidence summary must keep live pilot, token storage, and dashboard execution disabled.")
        return False


class OAuthLivePilotEvidencePackageResponse(BaseModel):
    packageId: str = "oauth-live-pilot-evidence-oauth-provider"
    providerId: str = "oauth-provider"
    generatedAt: str = ""
    readinessStatus: OAuthLiveExchangeReadinessStatus = "blocked"
    remediationSummary: OAuthReadinessRemediationSummaryResponse = Field(default_factory=OAuthReadinessRemediationSummaryResponse)
    preflightSummary: OAuthPreflightSummaryResponse = Field(default_factory=OAuthPreflightSummaryResponse)
    checklistSummary: OAuthOperatorAcceptanceChecklistResponse = Field(default_factory=OAuthOperatorAcceptanceChecklistResponse)
    auditHealthStatus: Literal["unavailable", "empty", "valid", "warnings", "errors"] = "empty"
    accountLinkHealthStatus: OAuthAccountLinkStoreHealthStatus = "empty"
    leakCheckStatus: Literal["clean", "warnings", "failed"] = "clean"
    gateStatus: OAuthLiveExchangePilotGateStatus = "dry_run_only"
    livePilotEnabledDefault: bool = False
    dashboardExecutionEnabled: bool = False
    tokenStorageEnabled: bool = False
    tokenValuesPresent: bool = False
    secretValuesPresent: bool = False
    externalCallPerformed: bool = False
    envVarNames: list[str] = Field(default_factory=list)
    envVarValuesExposed: bool = False
    evidenceItems: list[OAuthLivePilotEvidenceItemResponse] = Field(default_factory=list)
    replayVerification: OAuthLivePilotReplayVerificationResponse = Field(default_factory=OAuthLivePilotReplayVerificationResponse)
    summary: OAuthLivePilotEvidenceSummaryResponse = Field(default_factory=OAuthLivePilotEvidenceSummaryResponse)
    forbiddenActions: list[str] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "livePilotEnabledDefault",
        "dashboardExecutionEnabled",
        "tokenStorageEnabled",
        "tokenValuesPresent",
        "secretValuesPresent",
        "externalCallPerformed",
        "envVarValuesExposed",
    )
    @classmethod
    def _oauth_live_evidence_secret_flags_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth live pilot evidence package must not expose secrets, perform calls, or enable live/token/dashboard execution.")
        return False


class OAuthRealProviderSmokeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    providerId: str = "oauth-provider"
    operatorId: str | None = None
    oauthProviderMetadata: OAuthProviderMetadataDraftRequest = Field(default_factory=OAuthProviderMetadataDraftRequest)
    redirectUri: str | None = None
    authorizationCodePresent: bool = False
    authorizationCode: str | None = None
    stateStatus: Literal["valid", "missing", "expired", "replayed", "unknown"] = "unknown"
    codeVerifierPresent: bool = False
    codeVerifier: str | None = None
    testProviderConfigured: bool = False
    explicitOperatorAcceptance: bool = False
    operatorAcceptancePresent: bool = False
    liveExchangeRequested: bool = False
    tokenStorageRequested: bool = False
    dashboardTriggered: bool = False
    dryRunOnly: bool = True
    checklist: OAuthOperatorAcceptanceChecklistRequest = Field(default_factory=OAuthOperatorAcceptanceChecklistRequest)
    evidencePackage: dict[str, Any] = Field(default_factory=dict)


class OAuthRealProviderSmokeResultResponse(BaseModel):
    smokeStatus: OAuthRealProviderSmokeStatus = "blocked"
    providerId: str = "oauth-provider"
    operatorId: str | None = None
    explicitOperatorAcceptance: bool = False
    liveExchangeRequested: bool = False
    tokenStorageRequested: bool = False
    externalAuthCallPerformed: bool = False
    metadataLinkCreated: bool = False
    rawTokenStored: bool = False
    tokenValuesReturned: bool = False
    tokenValuesAudited: bool = False
    dashboardTriggered: bool = False
    accountLinkMetadataOnly: bool = True
    accountLink: OAuthAccountLinkMetadataResponse | None = None
    evidenceReplayStatus: OAuthLivePilotReplayVerificationStatus = "failed"
    pilotGateStatus: OAuthLiveExchangePilotGateStatus = "blocked"
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("tokenStorageRequested", "rawTokenStored", "tokenValuesReturned", "tokenValuesAudited", "dashboardTriggered")
    @classmethod
    def _oauth_real_smoke_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth real provider smoke must not request token storage, expose token values, or be dashboard-triggered.")
        return False

    @field_validator("accountLinkMetadataOnly")
    @classmethod
    def _oauth_real_smoke_account_link_metadata_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth real provider smoke account-link output must remain metadata-only.")
        return True


class OAuthRealProviderSmokeStatusResponse(BaseModel):
    smokeStatus: OAuthRealProviderSmokeStatus = "not_configured"
    liveSmokeEnabledByDefault: bool = False
    tokenStorageEnabled: bool = False
    dashboardExecutionEnabled: bool = False
    externalAuthCallPerformed: bool = False
    latestSmokeEvidence: Literal["none", "redacted"] = "none"
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("liveSmokeEnabledByDefault", "tokenStorageEnabled", "dashboardExecutionEnabled", "externalAuthCallPerformed")
    @classmethod
    def _oauth_real_smoke_status_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth real provider smoke status must remain disabled by default and dashboard-inaccessible.")
        return False


class OAuthSmokeResultRecordResponse(BaseModel):
    smokeResultId: str = "oauth-smoke-result-oauth-provider"
    providerId: str = "oauth-provider"
    operatorId: str | None = None
    smokeStatus: OAuthSmokeCapturedStatus = "no_result"
    externalAuthCallPerformed: bool = False
    mockedHttpUsed: bool = False
    accountLinkCreated: bool = False
    metadataOnlyLink: bool = True
    tokenValuesPresent: bool = False
    secretValuesPresent: bool = False
    rawResponseStored: bool = False
    replayVerificationStatus: OAuthLivePilotReplayVerificationStatus = "failed"
    evidencePackageId: str | None = None
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    createdAt: str = ""

    @field_validator("tokenValuesPresent", "secretValuesPresent", "rawResponseStored")
    @classmethod
    def _oauth_smoke_record_secret_flags_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth smoke result records must not contain token, secret, or raw response values.")
        return False

    @field_validator("metadataOnlyLink")
    @classmethod
    def _oauth_smoke_record_metadata_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth smoke result account-link output must remain metadata-only.")
        return True


class OAuthSmokeReplaySummaryResponse(BaseModel):
    replaySummaryId: str = "oauth-smoke-replay-oauth-provider"
    providerId: str = "oauth-provider"
    evidencePackageId: str | None = None
    verificationStatus: OAuthLivePilotReplayVerificationStatus = "failed"
    leakCount: int = 0
    secretLikeViolationCount: int = 0
    failedChecks: list[str] = Field(default_factory=list)
    warningChecks: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    redactedPaths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OAuthSmokeEvidenceFreezeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    smokeResult: dict[str, Any] = Field(default_factory=dict)
    evidencePackage: dict[str, Any] = Field(default_factory=dict)
    mockedHttpUsed: bool = False
    previewOnly: bool = True
    exportRequested: bool = False


class OAuthSmokeEvidenceFreezeResponse(BaseModel):
    freezeId: str = "oauth-smoke-freeze-oauth-provider"
    providerId: str = "oauth-provider"
    generatedAt: str = ""
    freezeStatus: OAuthSmokeFreezeStatus = "failed"
    previewOnly: bool = True
    exportWritten: bool = False
    smokeResult: OAuthSmokeResultRecordResponse = Field(default_factory=OAuthSmokeResultRecordResponse)
    replaySummary: OAuthSmokeReplaySummaryResponse = Field(default_factory=OAuthSmokeReplaySummaryResponse)
    evidencePackageId: str | None = None
    tokenStorageEnabled: bool = False
    dashboardExecutionEnabled: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("previewOnly")
    @classmethod
    def _oauth_smoke_freeze_preview_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth smoke evidence freeze is preview-only unless an explicit ignored export is implemented.")
        return True

    @field_validator("exportWritten", "tokenStorageEnabled", "dashboardExecutionEnabled")
    @classmethod
    def _oauth_smoke_freeze_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth smoke evidence freeze preview must not write exports, store tokens, or enable dashboard execution.")
        return False


class OAuthSmokeResultSummaryResponse(BaseModel):
    providerId: str = "oauth-provider"
    latestSmokeStatus: OAuthSmokeCapturedStatus = "no_result"
    latestResultId: str | None = None
    replayVerificationStatus: OAuthLivePilotReplayVerificationStatus = "valid"
    accountLinkCreated: bool = False
    metadataOnlyLink: bool = True
    tokenStorageEnabled: bool = False
    dashboardExecutionEnabled: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("tokenStorageEnabled", "dashboardExecutionEnabled")
    @classmethod
    def _oauth_smoke_summary_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("OAuth smoke result summary must keep token storage and dashboard execution disabled.")
        return False

    @field_validator("metadataOnlyLink")
    @classmethod
    def _oauth_smoke_summary_metadata_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("OAuth smoke result summary must remain metadata-only.")
        return True


class ProviderOrchestrationActionResponse(BaseModel):
    actionId: str
    actionLabel: str
    providerType: str = "unknown"
    availability: ProviderOrchestrationAvailability = "blocked"
    requiredCapabilities: list[str] = Field(default_factory=list)
    requiredOperatorAcceptance: bool = False
    requiresM4Validation: bool = True
    requiresPreflight: bool = False
    requiresEvidenceReplay: bool = False
    dashboardAllowed: bool = False
    externalCallPossible: bool = False
    tokenStorageAllowed: bool = False
    defaultEnabled: bool = False
    reasonCodes: list[str] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("dashboardAllowed", "tokenStorageAllowed")
    @classmethod
    def _provider_orchestration_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration matrix rows must not allow dashboard execution or token storage.")
        return False


class ProviderActionGateSummaryResponse(BaseModel):
    actionId: str = ""
    availability: ProviderOrchestrationAvailability = "blocked"
    gateStatus: Literal["available", "preview_only", "cli_only", "blocked", "future_disabled"] = "blocked"
    requiredCapabilities: list[str] = Field(default_factory=list)
    missingCapabilities: list[str] = Field(default_factory=list)
    requiredOperatorAcceptance: bool = False
    requiresM4Validation: bool = True
    dashboardAllowed: bool = False
    reasonCodes: list[str] = Field(default_factory=list)

    @field_validator("dashboardAllowed")
    @classmethod
    def _provider_action_gate_dashboard_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider action gate summaries must keep dashboard execution disabled.")
        return False


class ProviderOrchestrationReadinessSummaryResponse(BaseModel):
    matrixStatus: ProviderOrchestrationMatrixStatus = "warnings"
    availableCount: int = 0
    previewOnlyCount: int = 0
    cliOnlyCount: int = 0
    blockedCount: int = 0
    futureDisabledCount: int = 0
    dashboardExecutionAllowed: bool = False
    tokenStorageAllowed: bool = False
    safeNextSteps: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("dashboardExecutionAllowed", "tokenStorageAllowed")
    @classmethod
    def _provider_readiness_summary_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration readiness summaries must keep dashboard execution and token storage disabled.")
        return False


class ProviderOrchestrationSafetyMatrixResponse(BaseModel):
    matrixId: str = "provider-orchestration-safety-matrix"
    generatedAt: str = ""
    matrixStatus: ProviderOrchestrationMatrixStatus = "warnings"
    actionCount: int = 0
    availableCount: int = 0
    previewOnlyCount: int = 0
    cliOnlyCount: int = 0
    blockedCount: int = 0
    futureDisabledCount: int = 0
    dashboardExecutionAllowed: bool = False
    externalCallPossibleCount: int = 0
    tokenStorageAllowed: bool = False
    accountLinkInfluence: OAuthAccountLinkInfluence = "none"
    m4ValidationRequired: bool = True
    providerAcceptanceRequired: bool = True
    effectiveCapabilities: list[str] = Field(default_factory=list)
    actions: list[ProviderOrchestrationActionResponse] = Field(default_factory=list)
    actionGateSummaries: list[ProviderActionGateSummaryResponse] = Field(default_factory=list)
    summary: ProviderOrchestrationReadinessSummaryResponse = Field(default_factory=ProviderOrchestrationReadinessSummaryResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("dashboardExecutionAllowed", "tokenStorageAllowed")
    @classmethod
    def _provider_matrix_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration matrix must keep dashboard execution and token storage disabled.")
        return False


class ProviderOrchestrationDryRunPlanRequest(BaseModel):
    workflowType: ProviderOrchestrationWorkflow = "review_with_stub_provider"
    topicId: str | None = None
    providerConfigId: str | None = None


class ProviderOrchestrationPlanStepResponse(BaseModel):
    stepId: str
    actionId: str
    actionLabel: str
    providerType: str = "unknown"
    plannedMode: ProviderOrchestrationPlannedMode = "blocked"
    requiredCapabilities: list[str] = Field(default_factory=list)
    requiredAcceptance: bool = False
    requiresM4Validation: bool = True
    requiresOAuthPreflight: bool = False
    requiresEvidenceReplay: bool = False
    dashboardExecutionAllowed: bool = False
    externalCallPlanned: bool = False
    tokenStorageAllowed: bool = False
    expectedArtifacts: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    safeNextSteps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("dashboardExecutionAllowed", "externalCallPlanned", "tokenStorageAllowed")
    @classmethod
    def _provider_plan_step_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration dry-run steps must not plan dashboard execution, external calls, or token storage.")
        return False


class ProviderOrchestrationReplayResultResponse(BaseModel):
    replayId: str = "provider-orchestration-plan-replay"
    replayStatus: ProviderOrchestrationReplayStatus = "failed"
    failedChecks: list[str] = Field(default_factory=list)
    warningChecks: list[str] = Field(default_factory=list)
    forbiddenActionCount: int = 0
    missingGateCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    redactedPaths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProviderOrchestrationPlanSummaryResponse(BaseModel):
    planId: str = "provider-orchestration-plan-review-with-stub-provider"
    workflowType: ProviderOrchestrationWorkflow = "review_with_stub_provider"
    topicId: str | None = None
    providerConfigId: str | None = None
    stepCount: int = 0
    blockedStepCount: int = 0
    cliOnlyStepCount: int = 0
    previewOnlyStepCount: int = 0
    externalCallPlanned: bool = False
    dashboardExecutionPlanned: bool = False
    tokenStoragePlanned: bool = False
    replayStatus: ProviderOrchestrationReplayStatus = "valid"
    forbiddenActionCount: int = 0
    missingGateCount: int = 0
    safeNextSteps: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("externalCallPlanned", "dashboardExecutionPlanned", "tokenStoragePlanned")
    @classmethod
    def _provider_plan_summary_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration plan summaries must remain preview-only.")
        return False


class ProviderOrchestrationDryRunPlanResponse(BaseModel):
    planId: str = "provider-orchestration-plan-review-with-stub-provider"
    workflowType: ProviderOrchestrationWorkflow = "review_with_stub_provider"
    topicId: str | None = None
    providerConfigId: str | None = None
    generatedAt: str = ""
    steps: list[ProviderOrchestrationPlanStepResponse] = Field(default_factory=list)
    blockedStepCount: int = 0
    cliOnlyStepCount: int = 0
    previewOnlyStepCount: int = 0
    externalCallPlanned: bool = False
    dashboardExecutionPlanned: bool = False
    tokenStoragePlanned: bool = False
    replaySummary: ProviderOrchestrationReplayResultResponse = Field(default_factory=ProviderOrchestrationReplayResultResponse)
    summary: ProviderOrchestrationPlanSummaryResponse = Field(default_factory=ProviderOrchestrationPlanSummaryResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("externalCallPlanned", "dashboardExecutionPlanned", "tokenStoragePlanned")
    @classmethod
    def _provider_plan_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration dry-run plans must not plan external calls, dashboard execution, or token storage.")
        return False


class ProviderOrchestrationExecutionRequest(BaseModel):
    workflowType: ProviderOrchestrationWorkflow = "review_with_stub_provider"
    topicId: str | None = None
    providerConfigId: str | None = None
    fixtureOnly: bool = True
    dryRun: bool = True
    forceRefresh: bool = False
    limit: int = 1

    @field_validator("fixtureOnly")
    @classmethod
    def _execution_request_fixture_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Provider orchestration execution must be fixture-only.")
        return True


class ProviderOrchestrationExecutedStepResponse(BaseModel):
    stepId: str
    actionId: str
    actionLabel: str = ""
    providerType: str = "unknown"
    executionStatus: ProviderOrchestrationExecutionStatus = "skipped"
    fixtureOnly: bool = True
    externalCallPerformed: bool = False
    dashboardTriggered: bool = False
    tokenStorageUsed: bool = False
    m4ValidationApplied: bool = True
    acceptedDraftCount: int = 0
    needsReviewCount: int = 0
    rejectedCount: int = 0
    skippedExistingAcceptedCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("fixtureOnly")
    @classmethod
    def _executed_step_fixture_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Executed orchestration steps must remain fixture-only.")
        return True

    @field_validator("externalCallPerformed", "dashboardTriggered", "tokenStorageUsed")
    @classmethod
    def _executed_step_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Executed orchestration steps must not perform external calls, dashboard execution, or token storage.")
        return False


class ProviderOrchestrationExecutionReplayResponse(BaseModel):
    replayId: str = "provider-orchestration-execution-replay"
    replayStatus: ProviderOrchestrationReplayStatus = "failed"
    failedChecks: list[str] = Field(default_factory=list)
    warningChecks: list[str] = Field(default_factory=list)
    unsafeExecutionCount: int = 0
    missingValidationCount: int = 0
    secretLikeViolationCount: int = 0
    reasonCodes: list[str] = Field(default_factory=list)
    redactedPaths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProviderOrchestrationExecutionResultResponse(BaseModel):
    executionId: str = "provider-orchestration-fixture-execution-review-with-stub-provider"
    planId: str = "provider-orchestration-plan-review-with-stub-provider"
    workflowType: ProviderOrchestrationWorkflow = "review_with_stub_provider"
    topicId: str | None = None
    providerConfigId: str | None = None
    generatedAt: str = ""
    fixtureOnly: bool = True
    dryRun: bool = True
    externalCallPerformed: bool = False
    dashboardTriggered: bool = False
    tokenStorageUsed: bool = False
    stepsExecuted: int = 0
    stepsSkipped: int = 0
    acceptedDraftCount: int = 0
    needsReviewCount: int = 0
    rejectedCount: int = 0
    skippedExistingAcceptedCount: int = 0
    m4ValidationApplied: bool = True
    resultReplayStatus: ProviderOrchestrationReplayStatus = "valid"
    executedSteps: list[ProviderOrchestrationExecutedStepResponse] = Field(default_factory=list)
    replaySummary: ProviderOrchestrationExecutionReplayResponse = Field(default_factory=ProviderOrchestrationExecutionReplayResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("fixtureOnly")
    @classmethod
    def _execution_result_fixture_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Provider orchestration execution results must remain fixture-only.")
        return True

    @field_validator("externalCallPerformed", "dashboardTriggered", "tokenStorageUsed")
    @classmethod
    def _execution_result_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration execution results must not include external calls, dashboard execution, or token storage.")
        return False


class ProviderOrchestrationResultFreezeResponse(BaseModel):
    freezeId: str = "provider-orchestration-result-freeze-preview"
    previewOnly: bool = True
    exportWritten: bool = False
    executionResult: ProviderOrchestrationExecutionResultResponse = Field(default_factory=ProviderOrchestrationExecutionResultResponse)
    replaySummary: ProviderOrchestrationExecutionReplayResponse = Field(default_factory=ProviderOrchestrationExecutionReplayResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("previewOnly")
    @classmethod
    def _freeze_preview_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Provider orchestration result freeze writes must be explicit; preview remains read-only.")
        return True

    @field_validator("exportWritten")
    @classmethod
    def _freeze_export_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration result freeze preview must not write export artifacts.")
        return False


class ProviderOrchestrationExecutionSummaryResponse(BaseModel):
    executionId: str = "provider-orchestration-fixture-execution-review-with-stub-provider"
    planId: str = "provider-orchestration-plan-review-with-stub-provider"
    workflowType: ProviderOrchestrationWorkflow = "review_with_stub_provider"
    fixtureOnly: bool = True
    dryRun: bool = True
    externalCallPerformed: bool = False
    dashboardTriggered: bool = False
    tokenStorageUsed: bool = False
    stepsExecuted: int = 0
    stepsSkipped: int = 0
    acceptedDraftCount: int = 0
    needsReviewCount: int = 0
    rejectedCount: int = 0
    skippedExistingAcceptedCount: int = 0
    m4ValidationApplied: bool = True
    resultReplayStatus: ProviderOrchestrationReplayStatus = "valid"
    unsafeExecutionCount: int = 0
    missingValidationCount: int = 0
    safeNextSteps: list[str] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("fixtureOnly")
    @classmethod
    def _execution_summary_fixture_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Provider orchestration execution summaries must remain fixture-only.")
        return True

    @field_validator("externalCallPerformed", "dashboardTriggered", "tokenStorageUsed")
    @classmethod
    def _execution_summary_disabled_flags(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Provider orchestration execution summaries must not include external calls, dashboard execution, or token storage.")
        return False


class AuthFoundationStatusResponse(BaseModel):
    authMode: AuthFoundationMode = "local_operator"
    legacyAuthMode: Literal["disabled", "oidc"] = "disabled"
    oauthReady: bool = False
    tokenStorageEnabled: bool = False
    providerLiveExecution: Literal["cli_only_acceptance_gated"] = "cli_only_acceptance_gated"
    operator: OperatorIdentityResponse = Field(default_factory=OperatorIdentityResponse)
    oauthProviders: list[OAuthProviderMetadataResponse] = Field(default_factory=list)
    oauthDryRun: OAuthDryRunStatusResponse = Field(default_factory=OAuthDryRunStatusResponse)
    oauthTokenExchange: OAuthTokenExchangeStatusResponse = Field(default_factory=OAuthTokenExchangeStatusResponse)
    accountLinkStatus: OAuthAccountLinkHealthResponse = Field(default_factory=OAuthAccountLinkHealthResponse)
    sessionBinding: OAuthSessionBindingResponse = Field(default_factory=OAuthSessionBindingResponse)
    sessionCapabilityPolicy: OAuthSessionCapabilityPolicyResponse = Field(default_factory=OAuthSessionCapabilityPolicyResponse)
    oauthFlowTimeline: OAuthFlowTimelineResponse = Field(default_factory=OAuthFlowTimelineResponse)
    oauthLiveExchangeReadiness: OAuthLiveExchangeReadinessResponse = Field(default_factory=OAuthLiveExchangeReadinessResponse)
    oauthReadinessRemediation: OAuthReadinessRemediationSummaryResponse = Field(default_factory=OAuthReadinessRemediationSummaryResponse)
    oauthPreflightPackage: OAuthPreflightPackageResponse = Field(default_factory=OAuthPreflightPackageResponse)
    oauthLiveExchangePilot: OAuthLiveExchangePilotStatusResponse = Field(default_factory=OAuthLiveExchangePilotStatusResponse)
    oauthLivePilotEvidencePackage: OAuthLivePilotEvidencePackageResponse = Field(default_factory=OAuthLivePilotEvidencePackageResponse)
    oauthRealProviderSmoke: OAuthRealProviderSmokeStatusResponse = Field(default_factory=OAuthRealProviderSmokeStatusResponse)
    oauthSmokeResultSummary: OAuthSmokeResultSummaryResponse = Field(default_factory=OAuthSmokeResultSummaryResponse)
    providerOrchestrationSafetyMatrix: ProviderOrchestrationSafetyMatrixResponse = Field(default_factory=ProviderOrchestrationSafetyMatrixResponse)
    providerOrchestrationPlanSummary: ProviderOrchestrationPlanSummaryResponse = Field(default_factory=ProviderOrchestrationPlanSummaryResponse)
    providerOrchestrationExecutionSummary: ProviderOrchestrationExecutionSummaryResponse = Field(default_factory=ProviderOrchestrationExecutionSummaryResponse)
    operatorAuditSummary: OperatorAuditSummaryResponse = Field(default_factory=OperatorAuditSummaryResponse)
    operatorAuditHealth: OperatorAuditReplaySummaryResponse = Field(default_factory=OperatorAuditReplaySummaryResponse)
    reasonCodes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

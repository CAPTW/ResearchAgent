"""Non-live public MCP connector proof planning status.

This module deliberately does not read local MCP config contents, execute MCP
tools, start local stdio servers, call providers, or inspect credentials. It
only exposes labels, booleans, required future proof fields, and reason codes.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


PUBLIC_MCP_CONNECTOR_PROOF_STATUS = "planned"
PUBLIC_MCP_REQUIRED_PROOF_MODE = "mcp_public_read_only_live"
PUBLIC_MCP_FUTURE_PRECONDITION_PROMPT = "PUBLIC_MCP_CONNECTOR_PROOF_PRECONDITION_APPLY_NONLIVE"
PUBLIC_MCP_FUTURE_LIVE_PROMPT = "PUBLIC_MCP_CONNECTOR_READ_ONLY_PROOF_OWNER_APPROVED_LIVE"
PUBLIC_MCP_ENDPOINT_POLICY_STATUS = "owner_decision_required"
PUBLIC_MCP_AUTH_POLICY_STATUS = "owner_decision_required"
PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE = "local_tunnel_for_proof_only"
PUBLIC_MCP_RECOMMENDED_AUTH_MODE = "bearer_read_only_proof_credential"
PUBLIC_MCP_ENDPOINT_STOP_REASON = "PUBLIC_ENDPOINT_NOT_HTTPS"
PUBLIC_MCP_TUNNEL_PRECONDITION_PROMPT = "PUBLIC_MCP_TUNNEL_READONLY_PROOF_PRECONDITION_NONLIVE"
PUBLIC_MCP_TUNNEL_FUTURE_LIVE_PROMPT = "PUBLIC_MCP_TUNNEL_READONLY_LIVE_PROOF_OWNER_APPROVED"
PUBLIC_MCP_HOSTED_ENDPOINT_PROMPT = "PUBLIC_ENDPOINT_DEPLOYMENT_DECISION_NONLIVE"
PUBLIC_MCP_AUTH_IMPLEMENTATION_PROMPT = "PUBLIC_MCP_AUTH_POLICY_IMPLEMENTATION_NONLIVE"
PUBLIC_MCP_TUNNEL_STOP_REASON = "OWNER_TUNNEL_DECISION_REQUIRED"

PUBLIC_MCP_TUNNEL_REQUIRED_ENV_NAMES = (
    "XRML_PUBLIC_MCP_PROOF_APP_BASE_URL",
    "XRML_PUBLIC_MCP_PROOF_MCP_URL",
    "XRML_PUBLIC_MCP_PROOF_ENDPOINT_MODE",
    "XRML_PUBLIC_MCP_PROOF_APPROVED",
    "XRML_PUBLIC_MCP_PROOF_MAX_CALLS",
    "XRML_PUBLIC_MCP_PROOF_TOOL_CLASS",
    "XRML_PUBLIC_MCP_PROOF_BEARER_TOKEN",
)

PUBLIC_MCP_TUNNEL_PROVIDER_OPTIONS = (
    "secure_mcp_tunnel",
    "ngrok",
    "cloudflare_tunnel",
    "other_https_tunnel",
)

READ_ONLY_TOOL_CANDIDATES = (
    "search_papers",
    "fetch_paper_details",
    "get_run_status",
    "get_report_preview",
)

REQUIRED_FUTURE_PRECONDITIONS = (
    "public_https_app_base_url_or_owner_approved_tunnel",
    "reachable_app_endpoint",
    "reachable_mcp_endpoint",
    "protected_resource_metadata_reachable",
    "auth_bearer_or_session_scope_policy",
    "unauthenticated_challenge_observed",
    "authenticated_read_only_mcp_call_observed",
    "proof_credentials_injected_without_printing",
    "read_only_tool_response_redacted",
    "no_database_or_runtime_mutation",
    "no_provider_call_required",
    "no_local_mcp_config_reliance",
    "no_production_external_beta_claim",
)

FUTURE_ARTIFACT_REQUIRED_FIELDS = (
    "artifactKind",
    "proofMode",
    "endpointMode",
    "mcpEndpoint",
    "appBaseUrl",
    "tunnelProviderLabel",
    "authModeUsed",
    "unauthenticatedChallengeObserved",
    "authenticatedReadOnlyCallObserved",
    "authenticatedCallCount",
    "toolName",
    "toolInputClass",
    "readOnlyToolClass",
    "toolOutputRedacted",
    "dbMutationObserved",
    "providerCallPerformed",
    "appOauthOidcProofInScope",
    "providerAuthInScope",
    "localMcpConfigInScope",
    "publicConnectorProofInScope",
    "productionReady",
    "externalBetaReady",
    "noSecretInspection",
    "noTokenCacheInspection",
    "noCredentialPathDisclosure",
    "secretScanPassed",
    "reasonCodes",
    "notInScope",
)

DEFAULT_MISSING_PRECONDITIONS = (
    "public_https_app_base_url",
    "public_mcp_endpoint_reachable",
    "public_app_endpoint_reachable",
    "auth_policy_defined",
    "protected_resource_metadata_available",
    "unauthenticated_challenge_not_verified",
    "authenticated_read_only_call_not_verified",
    "proof_artifact_missing",
    "owner_live_approval_missing",
    "public_mcp_connector_not_proven",
)

REQUIRED_NOT_IN_SCOPE = (
    "provider_output_not_in_scope",
    "app_oauth_oidc_not_in_scope",
    "production_external_beta_not_in_scope",
    "local_mcp_config_not_in_scope",
)

SECRET_LIKE_VALUE_MARKERS = (
    ".mcp.json",
    "CODEX_HOME",
    ".codex",
    "auth.json",
    "session.json",
    "C:/Users",
    "C:\\Users",
    "sk-",
    "super-secret",
    "client-secret",
    "access_token",
    "refresh_token",
    "id_token",
    "Authorization:",
    "Bearer ",
)

ENDPOINT_POLICY_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "endpointMode": "local_tunnel_for_proof_only",
        "label": "Local tunnel for proof only",
        "examples": ["secure_mcp_tunnel", "ngrok", "cloudflare_tunnel"],
        "publicHttpsAvailable": False,
        "mcpEndpointAvailable": False,
        "deploymentRequired": False,
        "tunnelExecutionRequired": True,
        "liveProofRunnable": False,
        "productionReady": False,
        "externalBetaReady": False,
        "risk": "public_exposure_limited_lifecycle_auth_required",
        "ownerDecisionRequired": True,
        "recommendedForFirstProof": True,
        "nextPrompt": PUBLIC_MCP_TUNNEL_PRECONDITION_PROMPT,
    },
    {
        "endpointMode": "staging_public_hosted_endpoint",
        "label": "Hosted public endpoint for proof",
        "examples": ["preview_https_app", "staging_https_app"],
        "publicHttpsAvailable": False,
        "mcpEndpointAvailable": False,
        "deploymentRequired": True,
        "tunnelExecutionRequired": False,
        "liveProofRunnable": False,
        "productionReady": False,
        "externalBetaReady": False,
        "risk": "release_security_and_auth_policy_decision_required",
        "ownerDecisionRequired": True,
        "recommendedForFirstProof": False,
        "nextPrompt": PUBLIC_MCP_HOSTED_ENDPOINT_PROMPT,
    },
    {
        "endpointMode": "production_endpoint",
        "label": "Production endpoint",
        "examples": ["production_https_app"],
        "publicHttpsAvailable": False,
        "mcpEndpointAvailable": False,
        "deploymentRequired": True,
        "tunnelExecutionRequired": False,
        "liveProofRunnable": False,
        "productionReady": False,
        "externalBetaReady": False,
        "risk": "blocked_by_local_internal_rc_no_go",
        "blockedReason": "production_external_beta_no_go",
        "ownerDecisionRequired": True,
        "recommendedForFirstProof": False,
        "nextPrompt": "PUBLIC_RELEASE_OWNER_DECISION",
    },
    {
        "endpointMode": "defer_public_connector_proof",
        "label": "Defer public connector proof",
        "examples": ["keep_local_internal_workbench"],
        "publicHttpsAvailable": False,
        "mcpEndpointAvailable": False,
        "deploymentRequired": False,
        "tunnelExecutionRequired": False,
        "liveProofRunnable": False,
        "productionReady": False,
        "externalBetaReady": False,
        "risk": "public_connector_remains_unproven",
        "ownerDecisionRequired": True,
        "recommendedForFirstProof": False,
        "nextPrompt": "PUBLIC_MCP_CONNECTOR_PROOF_DEFER_DECISION_NONLIVE",
    },
)

AUTH_POLICY_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "authMode": "bearer_read_only_proof_credential",
        "label": "Bearer read-only proof credential",
        "recommendedForFirstProof": True,
        "requiresAppOauthOidcProof": False,
        "requiresTokenExchange": False,
        "tokenValueSerialized": False,
        "tokenValuePrinted": False,
        "tokenValueHashed": False,
        "allowedForPublicProof": True,
        "readOnlyScopeRequired": True,
        "ownerDecisionRequired": True,
        "nextPrompt": PUBLIC_MCP_AUTH_IMPLEMENTATION_PROMPT,
    },
    {
        "authMode": "session_based_auth",
        "label": "App session based auth",
        "recommendedForFirstProof": False,
        "requiresAppOauthOidcProof": True,
        "requiresTokenExchange": True,
        "tokenValueSerialized": False,
        "tokenValuePrinted": False,
        "tokenValueHashed": False,
        "allowedForPublicProof": True,
        "readOnlyScopeRequired": True,
        "ownerDecisionRequired": True,
        "nextPrompt": "APP_OAUTH_OIDC_PROOF_PRECONDITION_NONLIVE",
    },
    {
        "authMode": "oauth_oidc_connector_auth",
        "label": "OAuth/OIDC connector auth",
        "recommendedForFirstProof": False,
        "requiresAppOauthOidcProof": True,
        "requiresTokenExchange": True,
        "tokenValueSerialized": False,
        "tokenValuePrinted": False,
        "tokenValueHashed": False,
        "allowedForPublicProof": True,
        "readOnlyScopeRequired": True,
        "ownerDecisionRequired": True,
        "nextPrompt": "APP_OAUTH_OIDC_PROOF_PRECONDITION_NONLIVE",
    },
    {
        "authMode": "no_auth",
        "label": "No auth",
        "recommendedForFirstProof": False,
        "requiresAppOauthOidcProof": False,
        "requiresTokenExchange": False,
        "tokenValueSerialized": False,
        "tokenValuePrinted": False,
        "tokenValueHashed": False,
        "allowedForPublicProof": False,
        "readOnlyScopeRequired": False,
        "rejectedByDefault": True,
        "blockedReason": "public_mcp_noauth_rejected",
        "ownerDecisionRequired": True,
        "nextPrompt": "PUBLIC_MCP_AUTH_POLICY_IMPLEMENTATION_NONLIVE",
    },
)


@dataclass(frozen=True)
class PublicMcpConnectorPreconditionInput:
    """Safe booleans for future public MCP proof readiness evaluation."""

    public_https_app_base_url: bool = False
    public_mcp_endpoint_reachable: bool = False
    public_app_endpoint_reachable: bool = False
    auth_policy_defined: bool = False
    protected_resource_metadata_available: bool = False
    unauthenticated_challenge_verified: bool = False
    authenticated_read_only_call_verified: bool = False
    proof_artifact_present: bool = False
    owner_live_approval_present: bool = False
    app_oauth_oidc_decision: str = "deferred"
    app_oauth_oidc_proof_required: bool = False


@dataclass(frozen=True)
class PublicMcpTunnelReadOnlyProofRequest:
    """Owner-supplied booleans/labels for a future tunnel-only live proof."""

    approved: bool = False
    endpointMode: str = ""
    appBaseUrl: str = ""
    mcpUrl: str = ""
    maxCalls: int = 0
    authMode: str = ""
    readOnlyToolClass: str = ""
    allowDbMutation: bool = False
    allowProviderCall: bool = False
    allowProductionActivation: bool = False
    allowExternalBetaClaim: bool = False
    inspectActualMcpJson: bool = False
    inspectActualEnv: bool = False
    appOauthOidcProofInScope: bool = False
    productionReady: bool = False
    externalBetaReady: bool = False


PublicMcpConnectorPreconditionStatus = dict[str, Any]
PublicMcpConnectorProofArtifactContract = dict[str, Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _path_exists(path: Path) -> bool:
    """Return path existence without reading file contents."""

    try:
        return path.exists()
    except OSError:
        return False


def _coerce_tunnel_request(value: Mapping[str, Any] | PublicMcpTunnelReadOnlyProofRequest) -> PublicMcpTunnelReadOnlyProofRequest:
    if isinstance(value, PublicMcpTunnelReadOnlyProofRequest):
        return value
    return PublicMcpTunnelReadOnlyProofRequest(
        approved=bool(value.get("approved", False)),
        endpointMode=str(value.get("endpointMode") or ""),
        appBaseUrl=str(value.get("appBaseUrl") or ""),
        mcpUrl=str(value.get("mcpUrl") or ""),
        maxCalls=int(value.get("maxCalls") or 0),
        authMode=str(value.get("authMode") or ""),
        readOnlyToolClass=str(value.get("readOnlyToolClass") or ""),
        allowDbMutation=bool(value.get("allowDbMutation", False)),
        allowProviderCall=bool(value.get("allowProviderCall", False)),
        allowProductionActivation=bool(value.get("allowProductionActivation", False)),
        allowExternalBetaClaim=bool(value.get("allowExternalBetaClaim", False)),
        inspectActualMcpJson=bool(value.get("inspectActualMcpJson", False)),
        inspectActualEnv=bool(value.get("inspectActualEnv", False)),
        appOauthOidcProofInScope=bool(value.get("appOauthOidcProofInScope", False)),
        productionReady=bool(value.get("productionReady", False)),
        externalBetaReady=bool(value.get("externalBetaReady", False)),
    )


def _public_https_url_reasons(field_name: str, url: str, *, require_mcp_path: bool = False) -> list[str]:
    parsed = urlparse(url or "")
    reasons: list[str] = []
    if parsed.scheme != "https":
        reasons.append(f"{field_name}_must_be_https")
    if not parsed.netloc:
        reasons.append(f"{field_name}_must_have_host")
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.startswith("127."):
        reasons.append(f"{field_name}_must_not_be_loopback")
    if require_mcp_path and "/mcp" not in (parsed.path or ""):
        reasons.append(f"{field_name}_must_target_mcp_path")
    return reasons


def future_public_mcp_artifact_contract() -> dict[str, Any]:
    """Return the future live proof artifact schema expectations."""

    return {
        "artifactKind": "public_mcp_connector_proof",
        "proofMode": PUBLIC_MCP_REQUIRED_PROOF_MODE,
        "requiredFields": list(FUTURE_ARTIFACT_REQUIRED_FIELDS),
        "requiredBooleans": {
            "unauthenticatedChallengeObserved": True,
            "authenticatedReadOnlyCallObserved": True,
            "authenticatedCallCount": 1,
            "toolOutputRedacted": True,
            "dbMutationObserved": False,
            "providerCallPerformed": False,
            "appOauthOidcProofInScope": False,
            "providerAuthInScope": False,
            "localMcpConfigInScope": False,
            "publicConnectorProofInScope": True,
            "productionReady": False,
            "externalBetaReady": False,
            "noSecretInspection": True,
            "noTokenCacheInspection": True,
            "noCredentialPathDisclosure": True,
            "secretScanPassed": True,
        },
        "requiredValues": {
            "endpointMode": PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE,
            "authModeUsed": PUBLIC_MCP_RECOMMENDED_AUTH_MODE,
            "readOnlyToolClass": "read_only_tiny",
        },
        "allowedToolInputClass": "read_only_tiny",
        "readOnlyToolCandidates": list(READ_ONLY_TOOL_CANDIDATES),
        "notInScope": list(REQUIRED_NOT_IN_SCOPE),
    }


def build_public_mcp_tunnel_readonly_precondition_plan() -> dict[str, Any]:
    """Return the future local-tunnel public MCP proof contract without running it."""

    return {
        "endpointMode": PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE,
        "endpointModeStatus": PUBLIC_MCP_ENDPOINT_POLICY_STATUS,
        "tunnelProviderOptions": list(PUBLIC_MCP_TUNNEL_PROVIDER_OPTIONS),
        "tunnelExecutionAllowed": False,
        "publicHttpsRequired": True,
        "publicAppBaseUrlRequired": True,
        "publicMcpEndpointRequired": True,
        "localAppPortRequired": True,
        "localMcpRouteRequired": True,
        "publicEndpointDeploymentRequired": False,
        "productionReady": False,
        "externalBetaReady": False,
        "proofPurpose": "read_only_public_connector_preflight",
        "runnableNow": False,
        "liveExecutionAllowed": False,
        "stopReason": PUBLIC_MCP_ENDPOINT_STOP_REASON,
        "secondaryStopReason": PUBLIC_MCP_TUNNEL_STOP_REASON,
        "futureEnvNames": list(PUBLIC_MCP_TUNNEL_REQUIRED_ENV_NAMES),
        "endpointUrlContract": {
            "appBaseUrlEnv": "XRML_PUBLIC_MCP_PROOF_APP_BASE_URL",
            "mcpUrlEnv": "XRML_PUBLIC_MCP_PROOF_MCP_URL",
            "endpointModeEnv": "XRML_PUBLIC_MCP_PROOF_ENDPOINT_MODE",
            "ownerApprovalEnv": "XRML_PUBLIC_MCP_PROOF_APPROVED",
            "maxCallsEnv": "XRML_PUBLIC_MCP_PROOF_MAX_CALLS",
            "toolClassEnv": "XRML_PUBLIC_MCP_PROOF_TOOL_CLASS",
            "requiredEndpointMode": PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE,
            "requiredScheme": "https",
            "mustBePublicTunnelUrl": True,
            "mustNotBeLoopback": True,
            "mcpPathRequired": "/mcp",
            "exampleAppBaseUrl": "https://<owner-approved-tunnel-host>",
            "exampleMcpEndpoint": "https://<owner-approved-tunnel-host>/mcp",
            "urlValuesReadNow": False,
            "urlReachabilityCheckedNow": False,
        },
        "localAppBindingContract": {
            "loopbackAppBindingOnly": True,
            "tunnelForwardsToLocalApp": True,
            "productionOrStagingDeploymentAllowed": False,
            "localAppStartAllowedNow": False,
            "localMcpToolExecutionAllowedNow": False,
            "runtimeGuardRequired": True,
            "disposableOverlayRecommended": True,
            "readOnlyOnly": True,
        },
        "readOnlyBearerCredentialPolicy": {
            "authMode": PUBLIC_MCP_RECOMMENDED_AUTH_MODE,
            "credentialEnvName": "XRML_PUBLIC_MCP_PROOF_BEARER_TOKEN",
            "credentialSuppliedExternally": True,
            "presenceOnlyCheck": True,
            "tokenValuePrinted": False,
            "tokenValueSerialized": False,
            "tokenValueHashed": False,
            "tokenValueSliced": False,
            "credentialStoredInArtifact": False,
            "credentialPathDisclosureAllowed": False,
            "actualEnvFileInspectionAllowed": False,
            "actualMcpConfigInspectionAllowed": False,
            "tokenCacheInspectionAllowed": False,
            "rejectedAuthModes": ["no_auth", "session_based_auth", "oauth_oidc_connector_auth"],
        },
        "protectedResourceMetadataPlan": {
            "required": True,
            "endpointPath": "/.well-known/oauth-protected-resource/mcp",
            "publicOriginRequired": True,
            "canonicalResourceMustMatchMcpOrigin": True,
            "authorizationServersRequiredForOauthOidc": False,
            "scopesSupported": ["research.read"],
            "noSecretLeakage": True,
            "status": "planned_not_verified",
        },
        "unauthenticatedChallengePlan": {
            "required": True,
            "expectedStatusCode": 401,
            "expectedHeader": "WWW-Authenticate",
            "expectedErrorClass": "auth_challenge",
            "countsAsAuthenticatedCall": False,
            "dbMutationAllowed": False,
            "providerCallAllowed": False,
            "status": "planned_not_verified",
        },
        "readOnlyToolContract": {
            "preferredToolName": "get_run_status",
            "preferredToolClass": "read_only_tiny",
            "allowedToolNames": list(READ_ONLY_TOOL_CANDIDATES),
            "rejectedToolNames": ["start_research_run"],
            "authenticatedCallCount": 1,
            "maxAuthenticatedCalls": 1,
            "dbMutationAllowed": False,
            "providerCallAllowed": False,
            "reportGenerationAllowed": False,
            "topicAnalyticsRefreshAllowed": False,
            "schedulerMutationAllowed": False,
            "storageRunsMutationAllowed": False,
            "outputRedactionRequired": True,
            "runtimeGuardRequired": True,
        },
        "missingPreconditions": [
            "owner_approved_https_tunnel_app_base_url",
            "owner_approved_https_tunnel_mcp_url",
            "owner_live_approval_missing",
            "bearer_read_only_proof_credential_presence_unverified",
            "protected_resource_metadata_not_verified",
            "unauthenticated_challenge_not_verified",
            "authenticated_read_only_call_not_verified",
            "proof_artifact_missing",
        ],
        "futureLiveProofPrompt": PUBLIC_MCP_TUNNEL_FUTURE_LIVE_PROMPT,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "wouldStartTunnel": False,
        "wouldCallPublicMcp": False,
        "wouldExecuteLocalMcpTool": False,
        "wouldExecuteProvider": False,
        "reasonCodes": [
            "public_mcp_tunnel_readonly_precondition_nonlive",
            "local_tunnel_for_proof_only_owner_decision_required",
            "public_https_tunnel_urls_required",
            "bearer_read_only_proof_credential_required",
            "authenticated_read_only_mcp_proof_required",
            "production_external_beta_no_go",
        ],
    }


def build_public_mcp_tunnel_readonly_future_handoff_summary() -> dict[str, Any]:
    """Return safe metadata for a future owner-approved tunnel proof prompt."""

    return {
        "nextLiveProofPrompt": PUBLIC_MCP_TUNNEL_FUTURE_LIVE_PROMPT,
        "endpointMode": PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE,
        "authMode": PUBLIC_MCP_RECOMMENDED_AUTH_MODE,
        "requiredEnvNames": list(PUBLIC_MCP_TUNNEL_REQUIRED_ENV_NAMES),
        "requiredConstraints": {
            "appBaseUrl": "https_public_non_loopback_tunnel_url",
            "mcpEndpoint": "https_public_non_loopback_tunnel_url_with_mcp_path",
            "maxAuthenticatedCalls": 1,
            "toolClass": "read_only_tiny",
            "providerCallsAllowed": False,
            "dbMutationAllowed": False,
            "productionOrStagingActivationAllowed": False,
        },
        "artifactContract": future_public_mcp_artifact_contract(),
        "notInScope": [
            "provider_call",
            "app_oauth_oidc_live_flow",
            "local_mcp_config",
            "production_external_beta",
        ],
    }


def validate_public_mcp_tunnel_readonly_live_proof_request(
    request: Mapping[str, Any] | PublicMcpTunnelReadOnlyProofRequest,
) -> dict[str, Any]:
    """Validate future live-proof request metadata without touching env or network."""

    current = _coerce_tunnel_request(request)
    reasons: list[str] = []
    if current.approved is not True:
        reasons.append("owner_live_approval_required")
    if current.endpointMode != PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE:
        reasons.append("endpoint_mode_must_be_local_tunnel_for_proof_only")
    reasons.extend(_public_https_url_reasons("appBaseUrl", current.appBaseUrl))
    reasons.extend(_public_https_url_reasons("mcpUrl", current.mcpUrl, require_mcp_path=True))
    if current.maxCalls != 1:
        reasons.append("max_authenticated_calls_must_be_1")
    if current.authMode != PUBLIC_MCP_RECOMMENDED_AUTH_MODE:
        reasons.append("auth_mode_must_be_bearer_read_only_proof_credential")
    if current.authMode == "no_auth":
        reasons.append("no_auth_rejected_for_public_mcp_proof")
    if current.readOnlyToolClass != "read_only_tiny":
        reasons.append("read_only_tool_class_must_be_read_only_tiny")
    if current.allowDbMutation:
        reasons.append("db_mutation_must_be_disallowed")
    if current.allowProviderCall:
        reasons.append("provider_call_must_be_disallowed")
    if current.allowProductionActivation:
        reasons.append("production_activation_must_be_disallowed")
    if current.allowExternalBetaClaim:
        reasons.append("external_beta_claim_must_be_disallowed")
    if current.inspectActualMcpJson:
        reasons.append("actual_mcp_config_inspection_must_be_disallowed")
    if current.inspectActualEnv:
        reasons.append("actual_env_file_inspection_must_be_disallowed")
    if current.appOauthOidcProofInScope:
        reasons.append("app_oauth_oidc_proof_must_be_out_of_scope")
    if current.productionReady:
        reasons.append("production_ready_must_be_false")
    if current.externalBetaReady:
        reasons.append("external_beta_ready_must_be_false")

    accepted = not reasons
    return {
        "accepted": accepted,
        "endpointMode": current.endpointMode,
        "authMode": current.authMode,
        "readOnlyToolClass": current.readOnlyToolClass,
        "maxCalls": current.maxCalls,
        "productionReady": current.productionReady,
        "externalBetaReady": current.externalBetaReady,
        "reasonCodes": reasons if reasons else ["public_mcp_tunnel_readonly_live_request_contract_accepted"],
    }


def evaluate_public_mcp_connector_preconditions(
    inputs: PublicMcpConnectorPreconditionInput | None = None,
) -> PublicMcpConnectorPreconditionStatus:
    """Evaluate non-live public MCP proof readiness from explicit booleans only."""

    current = inputs or PublicMcpConnectorPreconditionInput()
    missing: list[str] = []
    if not current.public_https_app_base_url:
        missing.append("public_https_app_base_url")
    if not current.public_mcp_endpoint_reachable:
        missing.append("public_mcp_endpoint_reachable")
    if not current.public_app_endpoint_reachable:
        missing.append("public_app_endpoint_reachable")
    if not current.auth_policy_defined:
        missing.append("auth_policy_defined")
    if not current.protected_resource_metadata_available:
        missing.append("protected_resource_metadata_available")
    if not current.unauthenticated_challenge_verified:
        missing.append("unauthenticated_challenge_not_verified")
    if not current.authenticated_read_only_call_verified:
        missing.append("authenticated_read_only_call_not_verified")
    if not current.proof_artifact_present:
        missing.append("proof_artifact_missing")
    if not current.owner_live_approval_present:
        missing.append("owner_live_approval_missing")
    missing.append("public_mcp_connector_not_proven")

    return {
        "status": "planned_not_proven",
        "publicMcpConnectorProofStatus": PUBLIC_MCP_CONNECTOR_PROOF_STATUS,
        "publicMcpConnectorProofSatisfied": False,
        "liveProofRequired": True,
        "liveExecutionAllowed": False,
        "appOauthOidcDecision": current.app_oauth_oidc_decision,
        "appOauthOidcProofRequired": bool(current.app_oauth_oidc_proof_required),
        "appLoginSessionProofSatisfied": False,
        "missingPreconditions": missing,
        "reasonCodes": [
            "public_mcp_connector_proof_planned_nonlive",
            "public_mcp_connector_not_proven",
            "app_oauth_oidc_deferred"
            if current.app_oauth_oidc_decision == "deferred"
            else "app_oauth_oidc_owner_decision_required",
            "owner_live_approval_required",
            "authenticated_read_only_mcp_proof_required",
            "production_external_beta_no_go",
        ],
    }


def describe_public_mcp_connector_preconditions() -> PublicMcpConnectorPreconditionStatus:
    """Return the default fail-closed precondition status."""

    return evaluate_public_mcp_connector_preconditions()


def build_public_mcp_endpoint_auth_policy_plan() -> dict[str, Any]:
    """Return the non-live endpoint and auth policy decision matrix."""

    return {
        "endpointPolicyStatus": PUBLIC_MCP_ENDPOINT_POLICY_STATUS,
        "recommendedEndpointMode": PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE,
        "endpointOptions": [dict(option) for option in ENDPOINT_POLICY_OPTIONS],
        "authPolicyStatus": PUBLIC_MCP_AUTH_POLICY_STATUS,
        "recommendedAuthMode": PUBLIC_MCP_RECOMMENDED_AUTH_MODE,
        "authPolicyOptions": [dict(option) for option in AUTH_POLICY_OPTIONS],
        "protectedResourceMetadataPlan": {
            "required": True,
            "endpointPath": "/.well-known/oauth-protected-resource/mcp",
            "canonicalResourceMustMatchMcpOrigin": True,
            "authorizationServersRequiredForOauthOidc": True,
            "scopesSupported": ["research.read"],
            "bearerMethodsSupported": ["header"],
            "appOauthOidcRequiredForBearerFirstProof": False,
            "publicHttpsRequired": True,
            "status": "planned_not_verified",
        },
        "unauthenticatedChallengePlan": {
            "required": True,
            "expectedStatusCode": 401,
            "expectedHeader": "WWW-Authenticate",
            "requiresProtectedResourceMetadataReference": True,
            "noSecretLeakage": True,
            "dbMutationAllowed": False,
            "providerCallAllowed": False,
            "status": "planned_not_verified",
        },
        "authenticatedReadOnlyCallPlan": {
            "required": True,
            "callCount": 1,
            "toolInputClass": "read_only_tiny",
            "toolCandidates": list(READ_ONLY_TOOL_CANDIDATES),
            "providerCallAllowed": False,
            "dbMutationAllowed": False,
            "reportGenerationAllowed": False,
            "outputRedactionRequired": True,
            "runtimeGuardRequired": True,
            "status": "planned_not_verified",
        },
        "proofCredentialPolicy": {
            "credentialSuppliedExternally": True,
            "envFileInspectionAllowed": False,
            "actualMcpJsonInspectionAllowed": False,
            "tokenCacheInspectionAllowed": False,
            "tokenValuePrinted": False,
            "tokenValueSerialized": False,
            "tokenValueHashed": False,
            "credentialPathDisclosureAllowed": False,
            "presenceOnlyChecks": True,
            "artifactAuthRouteLabelOnly": True,
        },
        "runnableNow": False,
        "stopReason": PUBLIC_MCP_ENDPOINT_STOP_REASON,
        "publicMcpConnectorProofSatisfied": False,
        "liveExecutionAllowed": False,
        "productionReady": False,
        "externalBetaReady": False,
        "appOauthOidcDecision": "deferred",
        "localMcpSatisfiesPublicConnector": False,
        "providerAuthSatisfiesPublicMcp": False,
        "reasonCodes": [
            "public_mcp_endpoint_auth_policy_planned_nonlive",
            "public_endpoint_not_https",
            "owner_endpoint_decision_required",
            "owner_auth_policy_decision_required",
            "bearer_read_only_proof_credential_recommended_for_first_proof",
            "app_oauth_oidc_deferred",
            "production_external_beta_no_go",
        ],
    }


def build_public_mcp_connector_future_handoff_summary() -> dict[str, Any]:
    """Return safe future-prompt metadata for an owner-approved live proof."""

    return {
        "nextPreconditionPrompt": PUBLIC_MCP_FUTURE_PRECONDITION_PROMPT,
        "nextLiveProofPrompt": PUBLIC_MCP_FUTURE_LIVE_PROMPT,
        "recommendedEndpointMode": PUBLIC_MCP_RECOMMENDED_ENDPOINT_MODE,
        "endpointPolicyPrompt": PUBLIC_MCP_TUNNEL_PRECONDITION_PROMPT,
        "recommendedAuthMode": PUBLIC_MCP_RECOMMENDED_AUTH_MODE,
        "authPolicyPrompt": PUBLIC_MCP_AUTH_IMPLEMENTATION_PROMPT,
        "requiredFutureInputs": list(REQUIRED_FUTURE_PRECONDITIONS),
        "requiredMissingPreconditions": list(DEFAULT_MISSING_PRECONDITIONS),
        "artifactContract": future_public_mcp_artifact_contract(),
        "tunnelProofHandoff": build_public_mcp_tunnel_readonly_future_handoff_summary(),
        "notInScope": [
            "provider_call",
            "app_oauth_oidc_live_flow",
            "local_mcp_config",
            "production_external_beta",
        ],
    }


def build_public_mcp_connector_proof_plan_status(repo_root: Path | None = None) -> dict[str, Any]:
    """Build a safe non-live public MCP proof planning payload."""

    root = (repo_root or _repo_root()).resolve()
    example_available = _path_exists(root / ".mcp.json.example")
    local_stdio_available = _path_exists(root / "tools" / "local_research_mcp_server.py")
    preconditions = evaluate_public_mcp_connector_preconditions()
    endpoint_auth_plan = build_public_mcp_endpoint_auth_policy_plan()
    tunnel_plan = build_public_mcp_tunnel_readonly_precondition_plan()
    return {
        "publicMcpConnectorProofStatus": PUBLIC_MCP_CONNECTOR_PROOF_STATUS,
        "publicMcpConnectorProofSatisfied": False,
        "status": preconditions["status"],
        "proofModeRequired": PUBLIC_MCP_REQUIRED_PROOF_MODE,
        "futurePreconditionPrompt": PUBLIC_MCP_FUTURE_PRECONDITION_PROMPT,
        "futureLiveProofPrompt": PUBLIC_MCP_FUTURE_LIVE_PROMPT,
        "futureTunnelLiveProofPrompt": PUBLIC_MCP_TUNNEL_FUTURE_LIVE_PROMPT,
        "endpointPolicyStatus": endpoint_auth_plan["endpointPolicyStatus"],
        "recommendedEndpointMode": endpoint_auth_plan["recommendedEndpointMode"],
        "endpointOptions": endpoint_auth_plan["endpointOptions"],
        "authPolicyStatus": endpoint_auth_plan["authPolicyStatus"],
        "recommendedAuthMode": endpoint_auth_plan["recommendedAuthMode"],
        "authPolicyOptions": endpoint_auth_plan["authPolicyOptions"],
        "protectedResourceMetadataPlan": endpoint_auth_plan["protectedResourceMetadataPlan"],
        "unauthenticatedChallengePlan": endpoint_auth_plan["unauthenticatedChallengePlan"],
        "authenticatedReadOnlyCallPlan": endpoint_auth_plan["authenticatedReadOnlyCallPlan"],
        "proofCredentialPolicy": endpoint_auth_plan["proofCredentialPolicy"],
        "tunnelProofPreconditions": tunnel_plan,
        "futureTunnelHandoffSummary": build_public_mcp_tunnel_readonly_future_handoff_summary(),
        "runnableNow": endpoint_auth_plan["runnableNow"],
        "stopReason": endpoint_auth_plan["stopReason"],
        "localMcpConfigStatus": {
            "exampleReferenceStatus": "portable_reference_available" if example_available else "portable_reference_missing",
            "actualConfigPolicy": "local_actual_ignored",
            "actualConfigRequiredForPublicProof": False,
            "actualConfigContentInspected": False,
            "localStdioStatus": "available" if local_stdio_available else "missing",
            "localOnly": True,
        },
        "surfaceInventory": [
            {
                "surfaceId": "local_stdio_mcp",
                "classification": "local_stdio_only",
                "executionPerformed": False,
                "publicConnectorProof": False,
            },
            {
                "surfaceId": "fastapi_streamable_http_mcp",
                "classification": "public_connector_candidate",
                "executionPerformed": False,
                "publicConnectorProof": False,
            },
            {
                "surfaceId": "chatgpt_public_mcp_connector",
                "classification": "public_connector_unproven",
                "executionPerformed": False,
                "publicConnectorProof": False,
            },
        ],
        "publicEndpointRequired": True,
        "publicHttpsRequired": True,
        "mcpEndpointRequired": True,
        "protectedResourceMetadataRequired": True,
        "unauthenticatedChallengeRequired": True,
        "authenticatedReadOnlyCallRequired": True,
        "authRequired": True,
        "authPolicyRequired": True,
        "bearerOrSessionPolicyRequired": True,
        "appOauthOidcDecision": "deferred",
        "appOauthOidcRequiredForCurrentPlan": False,
        "appOauthOidcProofRequired": False,
        "ownerDecisionRequiredForPublicAuth": True,
        "providerAuthSatisfiesPublicMcp": False,
        "localMcpSatisfiesPublicConnector": False,
        "publicConnectorRequiresActualMcpJson": False,
        "providerCallRequired": False,
        "dbMutationAllowed": False,
        "productionReady": False,
        "externalBetaReady": False,
        "liveExecutionAllowed": False,
        "preconditionStatus": preconditions,
        "missingPreconditions": list(preconditions["missingPreconditions"]),
        "futurePreconditions": list(REQUIRED_FUTURE_PRECONDITIONS),
        "futureArtifactContract": future_public_mcp_artifact_contract(),
        "futureHandoffSummary": build_public_mcp_connector_future_handoff_summary(),
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "wouldExecuteMcpTool": False,
        "wouldExecuteProvider": False,
        "wouldStartLocalMcpServer": False,
        "wouldCallNetwork": False,
        "reasonCodes": [
            "public_mcp_connector_proof_planned_nonlive",
            "public_mcp_connector_proof_missing",
            "public_mcp_endpoint_auth_policy_planned_nonlive",
            "public_endpoint_not_https",
            "bearer_read_only_proof_credential_recommended_for_first_proof",
            "local_tunnel_for_proof_only_owner_decision_required",
            "local_mcp_config_not_public_connector_proof",
            "provider_auth_not_public_mcp_proof",
            "app_oauth_oidc_deferred",
            "public_https_endpoint_required",
            "authenticated_read_only_mcp_proof_required",
            "production_external_beta_no_go",
            "no_live_mcp_call_run",
        ],
    }


def _iter_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_iter_string_values(item))
        return strings
    if isinstance(value, (list, tuple)):
        strings = []
        for item in value:
            strings.extend(_iter_string_values(item))
        return strings
    return []


def _secret_like_value_reasons(artifact: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for text in _iter_string_values(artifact):
        for marker in SECRET_LIKE_VALUE_MARKERS:
            if marker.lower() in text.lower():
                reasons.append("secret_like_content_detected")
                return reasons
    try:
        serialized = json.dumps(artifact, sort_keys=True)
    except TypeError:
        serialized = str(artifact)
    if ".mcp.json" in serialized:
        reasons.append("actual_mcp_json_reference_detected")
    return reasons


def validate_public_mcp_connector_proof_artifact_contract(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a future public MCP proof artifact without accepting partial proofs."""

    reasons: list[str] = []
    if artifact.get("artifactKind") != "public_mcp_connector_proof":
        reasons.append("invalid_artifact_kind")
    if artifact.get("proofMode") != PUBLIC_MCP_REQUIRED_PROOF_MODE:
        reasons.append("invalid_proof_mode")
    for field in FUTURE_ARTIFACT_REQUIRED_FIELDS:
        if field not in artifact:
            reasons.append(f"missing_{field}")

    required_booleans = future_public_mcp_artifact_contract()["requiredBooleans"]
    for field, expected in required_booleans.items():
        if field == "authenticatedCallCount":
            if artifact.get(field) != expected:
                reasons.append("authenticatedCallCount_must_be_1")
            continue
        if artifact.get(field) is not expected:
            reasons.append(f"{field}_must_be_{str(expected).lower()}")

    required_values = future_public_mcp_artifact_contract()["requiredValues"]
    for field, expected in required_values.items():
        if artifact.get(field) != expected:
            reasons.append(f"{field}_must_be_{expected}")
    if artifact.get("toolInputClass") != "read_only_tiny":
        reasons.append("tool_input_class_must_be_read_only_tiny")
    if artifact.get("toolName") not in READ_ONLY_TOOL_CANDIDATES:
        reasons.append("tool_name_not_read_only_candidate")
    not_in_scope = set(str(item) for item in artifact.get("notInScope") or [])
    for value in REQUIRED_NOT_IN_SCOPE:
        if value not in not_in_scope:
            reasons.append(f"missing_not_in_scope_{value}")
    reasons.extend(_secret_like_value_reasons(artifact))

    accepted = not reasons
    return {
        "accepted": accepted,
        "artifactKind": artifact.get("artifactKind"),
        "proofMode": artifact.get("proofMode"),
        "publicConnectorProofInScope": bool(artifact.get("publicConnectorProofInScope")),
        "reasonCodes": reasons if reasons else ["public_mcp_connector_proof_contract_accepted"],
    }


def validate_public_mcp_connector_proof_artifact_shape(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility alias for the future proof artifact validator."""

    return validate_public_mcp_connector_proof_artifact_contract(artifact)

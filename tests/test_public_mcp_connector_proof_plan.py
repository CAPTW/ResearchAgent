from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest


def test_public_mcp_connector_plan_is_non_live_and_config_content_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".mcp.json").write_text('{"token":"super-secret-local-mcp"}', encoding="utf-8")
    (tmp_path / ".mcp.json.example").write_text("{}", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "local_research_mcp_server.py").write_text("# placeholder\n", encoding="utf-8")

    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("must not read config contents"))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run subprocess commands"))
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("must not open network connections"),
    )

    from app.services.public_mcp_connector_proof_plan import build_public_mcp_connector_proof_plan_status

    status = build_public_mcp_connector_proof_plan_status(repo_root=tmp_path)

    assert status["publicMcpConnectorProofStatus"] == "planned"
    assert status["publicMcpConnectorProofSatisfied"] is False
    assert status["localMcpConfigStatus"]["exampleReferenceStatus"] == "portable_reference_available"
    assert status["localMcpConfigStatus"]["actualConfigPolicy"] == "local_actual_ignored"
    assert status["localMcpConfigStatus"]["actualConfigRequiredForPublicProof"] is False
    assert status["localMcpConfigStatus"]["actualConfigContentInspected"] is False
    assert status["localMcpSatisfiesPublicConnector"] is False
    assert status["providerAuthSatisfiesPublicMcp"] is False
    assert status["appOauthOidcDecision"] == "deferred"
    assert status["appOauthOidcProofRequired"] is False
    assert status["publicConnectorRequiresActualMcpJson"] is False
    assert status["status"] == "planned_not_proven"
    assert status["endpointPolicyStatus"] == "owner_decision_required"
    assert status["recommendedEndpointMode"] == "local_tunnel_for_proof_only"
    assert status["authPolicyStatus"] == "owner_decision_required"
    assert status["recommendedAuthMode"] == "bearer_read_only_proof_credential"
    assert status["runnableNow"] is False
    assert status["stopReason"] == "PUBLIC_ENDPOINT_NOT_HTTPS"
    assert status["protectedResourceMetadataPlan"]["required"] is True
    assert status["unauthenticatedChallengePlan"]["expectedHeader"] == "WWW-Authenticate"
    assert status["authenticatedReadOnlyCallPlan"]["callCount"] == 1
    assert status["proofCredentialPolicy"]["envFileInspectionAllowed"] is False
    assert status["proofCredentialPolicy"]["actualMcpJsonInspectionAllowed"] is False
    assert status["proofCredentialPolicy"]["tokenValueSerialized"] is False
    tunnel = status["tunnelProofPreconditions"]
    assert tunnel["endpointMode"] == "local_tunnel_for_proof_only"
    assert tunnel["tunnelExecutionAllowed"] is False
    assert tunnel["publicEndpointDeploymentRequired"] is False
    assert tunnel["futureLiveProofPrompt"] == "PUBLIC_MCP_TUNNEL_READONLY_LIVE_PROOF_OWNER_APPROVED"
    assert "XRML_PUBLIC_MCP_PROOF_APP_BASE_URL" in tunnel["futureEnvNames"]
    assert "XRML_PUBLIC_MCP_PROOF_BEARER_TOKEN" in tunnel["futureEnvNames"]
    assert tunnel["endpointUrlContract"]["requiredScheme"] == "https"
    assert tunnel["endpointUrlContract"]["mustNotBeLoopback"] is True
    assert tunnel["readOnlyBearerCredentialPolicy"]["tokenValuePrinted"] is False
    assert tunnel["readOnlyBearerCredentialPolicy"]["actualEnvFileInspectionAllowed"] is False
    assert tunnel["readOnlyBearerCredentialPolicy"]["actualMcpConfigInspectionAllowed"] is False
    assert tunnel["readOnlyToolContract"]["preferredToolName"] == "get_run_status"
    assert tunnel["readOnlyToolContract"]["preferredToolClass"] == "read_only_tiny"
    assert "start_research_run" in tunnel["readOnlyToolContract"]["rejectedToolNames"]
    assert tunnel["wouldStartTunnel"] is False
    assert tunnel["wouldCallPublicMcp"] is False
    assert status["productionReady"] is False
    assert status["externalBetaReady"] is False
    assert status["liveExecutionAllowed"] is False
    assert status["wouldExecuteMcpTool"] is False
    assert status["wouldExecuteProvider"] is False
    assert status["wouldStartLocalMcpServer"] is False
    assert status["wouldCallNetwork"] is False
    assert {
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
    }.issubset(set(status["missingPreconditions"]))

    serialized = json.dumps(status, sort_keys=True)
    assert "super-secret-local-mcp" not in serialized
    assert ".mcp.json" not in serialized
    assert "CODEX_HOME" not in serialized


def test_endpoint_policy_options_are_non_live_and_no_production_default() -> None:
    from app.services.public_mcp_connector_proof_plan import build_public_mcp_connector_proof_plan_status

    status = build_public_mcp_connector_proof_plan_status()
    options = {option["endpointMode"]: option for option in status["endpointOptions"]}

    assert {
        "local_tunnel_for_proof_only",
        "staging_public_hosted_endpoint",
        "production_endpoint",
        "defer_public_connector_proof",
    }.issubset(options)
    assert options["local_tunnel_for_proof_only"]["recommendedForFirstProof"] is True
    assert options["local_tunnel_for_proof_only"]["liveProofRunnable"] is False
    assert options["staging_public_hosted_endpoint"]["deploymentRequired"] is True
    assert options["production_endpoint"]["productionReady"] is False
    assert options["production_endpoint"]["externalBetaReady"] is False
    assert options["production_endpoint"]["recommendedForFirstProof"] is False
    assert options["defer_public_connector_proof"]["liveProofRunnable"] is False
    assert all(option["liveProofRunnable"] is False for option in options.values())


def test_auth_policy_options_keep_bearer_first_proof_separate_from_app_oauth() -> None:
    from app.services.public_mcp_connector_proof_plan import build_public_mcp_connector_proof_plan_status

    status = build_public_mcp_connector_proof_plan_status()
    options = {option["authMode"]: option for option in status["authPolicyOptions"]}

    assert {
        "bearer_read_only_proof_credential",
        "session_based_auth",
        "oauth_oidc_connector_auth",
        "no_auth",
    }.issubset(options)
    bearer = options["bearer_read_only_proof_credential"]
    assert bearer["recommendedForFirstProof"] is True
    assert bearer["requiresAppOauthOidcProof"] is False
    assert bearer["requiresTokenExchange"] is False
    assert bearer["tokenValuePrinted"] is False
    assert bearer["tokenValueSerialized"] is False
    assert bearer["tokenValueHashed"] is False
    assert bearer["readOnlyScopeRequired"] is True
    assert options["session_based_auth"]["requiresAppOauthOidcProof"] is True
    assert options["oauth_oidc_connector_auth"]["requiresAppOauthOidcProof"] is True
    assert options["no_auth"]["allowedForPublicProof"] is False
    assert options["no_auth"]["rejectedByDefault"] is True


def test_public_mcp_plan_includes_challenge_metadata_and_one_call_read_only_contract() -> None:
    from app.services.public_mcp_connector_proof_plan import build_public_mcp_connector_proof_plan_status

    status = build_public_mcp_connector_proof_plan_status()

    metadata = status["protectedResourceMetadataPlan"]
    challenge = status["unauthenticatedChallengePlan"]
    authenticated_call = status["authenticatedReadOnlyCallPlan"]

    assert metadata["endpointPath"] == "/.well-known/oauth-protected-resource/mcp"
    assert metadata["publicHttpsRequired"] is True
    assert metadata["appOauthOidcRequiredForBearerFirstProof"] is False
    assert challenge["required"] is True
    assert challenge["expectedStatusCode"] == 401
    assert challenge["expectedHeader"] == "WWW-Authenticate"
    assert challenge["dbMutationAllowed"] is False
    assert challenge["providerCallAllowed"] is False
    assert authenticated_call["required"] is True
    assert authenticated_call["callCount"] == 1
    assert authenticated_call["toolInputClass"] == "read_only_tiny"
    assert authenticated_call["providerCallAllowed"] is False
    assert authenticated_call["dbMutationAllowed"] is False
    assert authenticated_call["reportGenerationAllowed"] is False
    assert authenticated_call["outputRedactionRequired"] is True
    assert authenticated_call["runtimeGuardRequired"] is True


def test_tunnel_readonly_preconditions_define_endpoint_auth_and_tool_contracts() -> None:
    from app.services.public_mcp_connector_proof_plan import build_public_mcp_tunnel_readonly_precondition_plan

    plan = build_public_mcp_tunnel_readonly_precondition_plan()

    assert plan["runnableNow"] is False
    assert plan["stopReason"] == "PUBLIC_ENDPOINT_NOT_HTTPS"
    assert plan["secondaryStopReason"] == "OWNER_TUNNEL_DECISION_REQUIRED"
    assert plan["endpointUrlContract"]["appBaseUrlEnv"] == "XRML_PUBLIC_MCP_PROOF_APP_BASE_URL"
    assert plan["endpointUrlContract"]["mcpUrlEnv"] == "XRML_PUBLIC_MCP_PROOF_MCP_URL"
    assert plan["endpointUrlContract"]["exampleMcpEndpoint"] == "https://<owner-approved-tunnel-host>/mcp"
    assert plan["localAppBindingContract"]["productionOrStagingDeploymentAllowed"] is False
    assert plan["localAppBindingContract"]["localAppStartAllowedNow"] is False
    assert plan["readOnlyBearerCredentialPolicy"]["authMode"] == "bearer_read_only_proof_credential"
    assert plan["readOnlyBearerCredentialPolicy"]["presenceOnlyCheck"] is True
    assert plan["readOnlyBearerCredentialPolicy"]["tokenValueSerialized"] is False
    assert plan["protectedResourceMetadataPlan"]["endpointPath"] == "/.well-known/oauth-protected-resource/mcp"
    assert plan["unauthenticatedChallengePlan"]["expectedStatusCode"] == 401
    assert plan["readOnlyToolContract"]["maxAuthenticatedCalls"] == 1
    assert plan["readOnlyToolContract"]["providerCallAllowed"] is False
    assert plan["readOnlyToolContract"]["dbMutationAllowed"] is False


def test_tunnel_live_request_validator_accepts_safe_one_call_bearer_request() -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_tunnel_readonly_live_proof_request

    result = validate_public_mcp_tunnel_readonly_live_proof_request(
        {
            "approved": True,
            "endpointMode": "local_tunnel_for_proof_only",
            "appBaseUrl": "https://owner-approved.example",
            "mcpUrl": "https://owner-approved.example/mcp",
            "maxCalls": 1,
            "authMode": "bearer_read_only_proof_credential",
            "readOnlyToolClass": "read_only_tiny",
        }
    )

    assert result["accepted"] is True
    assert result["reasonCodes"] == ["public_mcp_tunnel_readonly_live_request_contract_accepted"]


def test_tunnel_live_request_validator_rejects_local_http_noauth_and_multi_call() -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_tunnel_readonly_live_proof_request

    result = validate_public_mcp_tunnel_readonly_live_proof_request(
        {
            "approved": False,
            "endpointMode": "local_tunnel_for_proof_only",
            "appBaseUrl": "http://127.0.0.1:8000",
            "mcpUrl": "http://localhost:8000/not-mcp",
            "maxCalls": 2,
            "authMode": "no_auth",
            "readOnlyToolClass": "write_tool",
        }
    )

    assert result["accepted"] is False
    assert "owner_live_approval_required" in result["reasonCodes"]
    assert "appBaseUrl_must_be_https" in result["reasonCodes"]
    assert "appBaseUrl_must_not_be_loopback" in result["reasonCodes"]
    assert "mcpUrl_must_be_https" in result["reasonCodes"]
    assert "mcpUrl_must_not_be_loopback" in result["reasonCodes"]
    assert "mcpUrl_must_target_mcp_path" in result["reasonCodes"]
    assert "max_authenticated_calls_must_be_1" in result["reasonCodes"]
    assert "auth_mode_must_be_bearer_read_only_proof_credential" in result["reasonCodes"]
    assert "no_auth_rejected_for_public_mcp_proof" in result["reasonCodes"]
    assert "read_only_tool_class_must_be_read_only_tiny" in result["reasonCodes"]


def test_tunnel_live_request_validator_rejects_mutation_and_secret_inspection_flags() -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_tunnel_readonly_live_proof_request

    result = validate_public_mcp_tunnel_readonly_live_proof_request(
        {
            "approved": True,
            "endpointMode": "local_tunnel_for_proof_only",
            "appBaseUrl": "https://owner-approved.example",
            "mcpUrl": "https://owner-approved.example/mcp",
            "maxCalls": 1,
            "authMode": "bearer_read_only_proof_credential",
            "readOnlyToolClass": "read_only_tiny",
            "allowDbMutation": True,
            "allowProviderCall": True,
            "allowProductionActivation": True,
            "allowExternalBetaClaim": True,
            "inspectActualMcpJson": True,
            "inspectActualEnv": True,
            "appOauthOidcProofInScope": True,
            "productionReady": True,
            "externalBetaReady": True,
        }
    )

    assert result["accepted"] is False
    assert "db_mutation_must_be_disallowed" in result["reasonCodes"]
    assert "provider_call_must_be_disallowed" in result["reasonCodes"]
    assert "production_activation_must_be_disallowed" in result["reasonCodes"]
    assert "external_beta_claim_must_be_disallowed" in result["reasonCodes"]
    assert "actual_mcp_config_inspection_must_be_disallowed" in result["reasonCodes"]
    assert "actual_env_file_inspection_must_be_disallowed" in result["reasonCodes"]
    assert "app_oauth_oidc_proof_must_be_out_of_scope" in result["reasonCodes"]
    assert "production_ready_must_be_false" in result["reasonCodes"]
    assert "external_beta_ready_must_be_false" in result["reasonCodes"]


def _valid_public_mcp_artifact() -> dict[str, object]:
    return {
        "artifactKind": "public_mcp_connector_proof",
        "proofMode": "mcp_public_read_only_live",
        "endpointMode": "local_tunnel_for_proof_only",
        "mcpEndpoint": "https://example.invalid/mcp",
        "appBaseUrl": "https://example.invalid/app",
        "tunnelProviderLabel": "owner_approved_https_tunnel",
        "authModeUsed": "bearer_read_only_proof_credential",
        "unauthenticatedChallengeObserved": True,
        "authenticatedReadOnlyCallObserved": True,
        "authenticatedCallCount": 1,
        "toolName": "get_run_status",
        "toolInputClass": "read_only_tiny",
        "readOnlyToolClass": "read_only_tiny",
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
        "reasonCodes": ["public_mcp_connector_read_only_live_proof"],
        "notInScope": [
            "provider_output_not_in_scope",
            "app_oauth_oidc_not_in_scope",
            "production_external_beta_not_in_scope",
            "local_mcp_config_not_in_scope",
        ],
    }


def test_public_mcp_artifact_contract_accepts_valid_redacted_read_only_shape() -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_connector_proof_artifact_shape

    result = validate_public_mcp_connector_proof_artifact_shape(_valid_public_mcp_artifact())

    assert result["accepted"] is True
    assert result["reasonCodes"] == ["public_mcp_connector_proof_contract_accepted"]


def test_public_mcp_artifact_contract_rejects_missing_required_live_observations() -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_connector_proof_artifact_contract

    missing_challenge = {**_valid_public_mcp_artifact(), "unauthenticatedChallengeObserved": False}
    missing_authenticated_call = {**missing_challenge, "unauthenticatedChallengeObserved": True, "authenticatedReadOnlyCallObserved": False}

    challenge_result = validate_public_mcp_connector_proof_artifact_contract(missing_challenge)
    authenticated_result = validate_public_mcp_connector_proof_artifact_contract(missing_authenticated_call)

    assert challenge_result["accepted"] is False
    assert "unauthenticatedChallengeObserved_must_be_true" in challenge_result["reasonCodes"]
    assert authenticated_result["accepted"] is False
    assert "authenticatedReadOnlyCallObserved_must_be_true" in authenticated_result["reasonCodes"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("dbMutationObserved", True, "dbMutationObserved_must_be_false"),
        ("providerCallPerformed", True, "providerCallPerformed_must_be_false"),
        ("productionReady", True, "productionReady_must_be_false"),
        ("externalBetaReady", True, "externalBetaReady_must_be_false"),
        ("localMcpConfigInScope", True, "localMcpConfigInScope_must_be_false"),
        ("toolOutputRedacted", False, "toolOutputRedacted_must_be_true"),
        ("authenticatedCallCount", 2, "authenticatedCallCount_must_be_1"),
    ],
)
def test_public_mcp_artifact_contract_rejects_overclaims_and_mutating_proofs(
    field: str,
    value: object,
    reason: str,
) -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_connector_proof_artifact_contract

    result = validate_public_mcp_connector_proof_artifact_contract({**_valid_public_mcp_artifact(), field: value})

    assert result["accepted"] is False
    assert reason in result["reasonCodes"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("endpointMode", "production_endpoint", "endpointMode_must_be_local_tunnel_for_proof_only"),
        ("authModeUsed", "no_auth", "authModeUsed_must_be_bearer_read_only_proof_credential"),
        ("readOnlyToolClass", "write_tool", "readOnlyToolClass_must_be_read_only_tiny"),
    ],
)
def test_public_mcp_artifact_contract_rejects_wrong_tunnel_auth_or_tool_class(
    field: str,
    value: object,
    reason: str,
) -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_connector_proof_artifact_contract

    result = validate_public_mcp_connector_proof_artifact_contract({**_valid_public_mcp_artifact(), field: value})

    assert result["accepted"] is False
    assert reason in result["reasonCodes"]


def test_public_mcp_artifact_contract_rejects_secret_like_or_actual_mcp_config_refs() -> None:
    from app.services.public_mcp_connector_proof_plan import validate_public_mcp_connector_proof_artifact_contract

    secret_result = validate_public_mcp_connector_proof_artifact_contract(
        {**_valid_public_mcp_artifact(), "toolOutputPreview": "sk-secret-value"}
    )
    mcp_config_result = validate_public_mcp_connector_proof_artifact_contract(
        {**_valid_public_mcp_artifact(), "operatorNote": "loaded from .mcp.json"}
    )

    assert secret_result["accepted"] is False
    assert "secret_like_content_detected" in secret_result["reasonCodes"]
    assert mcp_config_result["accepted"] is False
    assert (
        "secret_like_content_detected" in mcp_config_result["reasonCodes"]
        or "actual_mcp_json_reference_detected" in mcp_config_result["reasonCodes"]
    )


def test_provider_proof_state_embeds_public_mcp_plan_without_public_proof(tmp_path: Path) -> None:
    from app.services.provider_proof_state import build_provider_proof_state_status

    status = build_provider_proof_state_status(repo_root=tmp_path)
    summary = status["mcpConnectorStatusSummary"]

    assert summary["publicMcpConnectorProofStatus"] == "planned"
    assert summary["publicMcpConnectorProofSatisfied"] is False
    assert summary["localMcpIsPublicConnector"] is False
    assert summary["localMcpSatisfiesPublicConnector"] is False
    assert summary["providerAuthSatisfiesPublicMcp"] is False
    assert summary["status"] == "planned_not_proven"
    assert summary["proofModeRequired"] == "mcp_public_read_only_live"
    assert summary["endpointPolicyStatus"] == "owner_decision_required"
    assert summary["recommendedEndpointMode"] == "local_tunnel_for_proof_only"
    assert summary["authPolicyStatus"] == "owner_decision_required"
    assert summary["recommendedAuthMode"] == "bearer_read_only_proof_credential"
    assert summary["runnableNow"] is False
    assert summary["stopReason"] == "PUBLIC_ENDPOINT_NOT_HTTPS"
    assert summary["protectedResourceMetadataPlan"]["publicHttpsRequired"] is True
    assert summary["unauthenticatedChallengePlan"]["expectedStatusCode"] == 401
    assert summary["authenticatedReadOnlyCallPlan"]["callCount"] == 1
    assert summary["proofCredentialPolicy"]["tokenValuePrinted"] is False
    assert summary["tunnelProofPreconditions"]["endpointMode"] == "local_tunnel_for_proof_only"
    assert summary["tunnelProofPreconditions"]["readOnlyToolContract"]["preferredToolName"] == "get_run_status"
    assert summary["futureTunnelHandoffSummary"]["nextLiveProofPrompt"] == (
        "PUBLIC_MCP_TUNNEL_READONLY_LIVE_PROOF_OWNER_APPROVED"
    )
    assert summary["localMcpConfigStatus"]["actualConfigPolicy"] == "local_actual_ignored"
    assert summary["publicConnectorRequiresActualMcpJson"] is False
    assert "public_https_app_base_url" in summary["missingPreconditions"]
    assert "authenticated_read_only_call_not_verified" in summary["missingPreconditions"]
    assert status["productionReadinessSummary"]["productionReady"] is False
    assert status["productionReadinessSummary"]["externalBetaReady"] is False

    serialized = json.dumps(status, sort_keys=True)
    assert ".mcp.json" not in serialized
    assert "CODEX_HOME" not in serialized

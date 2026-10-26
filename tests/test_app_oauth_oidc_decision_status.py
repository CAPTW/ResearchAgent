import builtins
import json

from app.auth.config import AuthSettings
from app.services.app_oauth_oidc_decision import build_app_oauth_oidc_decision_status
from app.services.provider_weighted_routing_promotion import describe_provider_routing_promotion_status


def test_app_oauth_oidc_decision_defaults_to_deferred_local_internal() -> None:
    status = build_app_oauth_oidc_decision_status(AuthSettings(auth_mode="disabled"))

    assert status["appOauthOidcDecision"] == "app_oauth_oidc_deferred"
    assert status["appOauthOidcDecisionLabel"] == "deferred"
    assert status["appOauthOidcProofStatus"] == "not_in_scope"
    assert status["authMode"] == "disabled"
    assert status["currentAction"] == "defer"
    assert status["localInternalWorkbench"] is True
    assert status["appLoginSessionProofSatisfied"] is False
    assert status["authCodeExchangeProofSatisfied"] is False
    assert status["sessionCookieProofSatisfied"] is False
    assert status["providerAuthSatisfiesAppOauthOidc"] is False
    assert status["codexSdkProviderAuthSatisfiesAppLoginSession"] is False
    assert status["publicMcpConnectorProofSatisfied"] is False
    assert status["productionReady"] is False
    assert status["externalBetaReady"] is False
    assert "APP_OAUTH_OIDC_PROOF_PRECONDITION_NONLIVE" == status["futureProofPrompt"]
    assert "auth_code_exchange" in status["requiredFutureProof"]
    assert "SESSION_SECRET" in status["requiredEnvNames"]


def test_configured_oidc_metadata_is_still_not_app_session_proof_and_redacts_secret_values() -> None:
    settings = AuthSettings(
        auth_mode="oidc",
        app_base_url="https://research.example.com",
        oidc_issuer="https://issuer.example.com",
        oidc_client_id="client-id-value",
        oidc_client_secret="do-not-serialize-client-secret",
        session_secret="do-not-serialize-session-secret",
    )

    status = build_app_oauth_oidc_decision_status(settings)
    serialized = json.dumps(status, sort_keys=True)

    assert status["appOauthOidcDecision"] == "app_oauth_oidc_deferred"
    assert status["appOauthOidcProofStatus"] == "configured_not_verified"
    assert status["appLoginSessionProofSatisfied"] is False
    assert status["providerAuthSatisfiesAppOauthOidc"] is False
    assert "do-not-serialize-client-secret" not in serialized
    assert "do-not-serialize-session-secret" not in serialized
    assert "client-id-value" not in serialized
    assert "https://issuer.example.com" not in serialized
    assert "https://research.example.com" not in serialized


def test_decision_status_does_not_read_env_or_mcp_files(monkeypatch) -> None:
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = str(file).replace("\\", "/")
        if path.endswith(".env") or path.endswith(".mcp.json"):
            raise AssertionError("decision status must not read actual env or mcp files")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    status = build_app_oauth_oidc_decision_status(AuthSettings(auth_mode="disabled"))

    assert status["noSecretInspection"] is True
    assert status["noTokenCacheInspection"] is True
    assert status["noCredentialPathDisclosure"] is True


def test_provider_routing_policy_remains_non_live_and_separate_from_app_oauth() -> None:
    routing = describe_provider_routing_promotion_status({})

    assert routing["liveExecutionAllowed"] is False
    assert routing["wouldExecuteProvider"] is False
    assert routing["providerCallCountPlanned"] == 0
    assert routing["providerAuthIsAppOauthOidc"] is False
    assert routing["localMcpIsPublicConnector"] is False
    assert routing["productionReady"] is False
    assert routing["externalBetaReady"] is False


def test_settings_status_includes_app_oauth_decision_without_overclaim(monkeypatch) -> None:
    from app.api import dashboard
    from app.schemas.auth import AuthFoundationStatusResponse
    from app.schemas.dashboard import SettingsStatusResponse

    decision = build_app_oauth_oidc_decision_status(AuthSettings(auth_mode="disabled"))
    proof_state = {
        "appOauthOidcStatusSummary": {"providerAuthIsAppOauthOidc": False, "status": "separate_scope"},
        "mcpConnectorStatusSummary": {"localMcpIsPublicConnector": False, "status": "separate_scope"},
        "productionReadinessSummary": {"productionReady": False, "externalBetaReady": False},
        "routingPromotionPolicy": {
            "policyMode": "planning_only",
            "liveExecutionAllowed": False,
            "wouldExecuteProvider": False,
        },
    }

    monkeypatch.setattr(
        dashboard,
        "build_auth_me_response",
        lambda request: {
            "auth_mode": "disabled",
            "authenticated": True,
            "product_session_backend": "local_internal",
            "warnings": [],
            "mcp_url": None,
        },
    )
    monkeypatch.setattr(dashboard, "get_optional_product_user", lambda request: None)
    monkeypatch.setattr(
        dashboard.auth_foundation,
        "build_auth_foundation_status",
        lambda user=None: AuthFoundationStatusResponse(),
    )
    monkeypatch.setattr(
        dashboard.app_oauth_oidc_decision,
        "build_app_oauth_oidc_decision_status",
        lambda: decision,
    )
    monkeypatch.setattr(
        dashboard,
        "describe_mcp_oauth_readiness",
        lambda: {
            "transport": "streamable-http",
            "protocol_version": "2025-03-26",
            "auth_required": False,
            "public_https": False,
            "cli_ready": True,
            "chatgpt_ready": False,
            "readiness_label": "local_cli_only",
            "authorization_servers": [],
            "public_tools": [],
            "protected_read_tools": [],
            "protected_write_tools": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(dashboard, "get_mcp_tool_access_summary", lambda: {})
    monkeypatch.setattr(
        dashboard,
        "describe_provider_status",
        lambda: {
            "provider": "codex",
            "provider_mode": "configured_not_verified",
            "configured": True,
            "live_call_enabled": False,
            "researchOutputAllowed": False,
            "reportGenerationAllowed": False,
            "warnings": [],
        },
    )
    monkeypatch.setattr(dashboard, "build_provider_proof_state_status", lambda: proof_state)
    monkeypatch.setattr(dashboard.topic_schedules, "get_scheduler_runtime_status", lambda: {})
    monkeypatch.setattr(dashboard.topic_notifications, "notification_delivery_health_summary", lambda: {})
    monkeypatch.setattr(
        dashboard,
        "build_deployment_status",
        lambda provider, scheduler, notification_delivery: {
            "auth": {"mode": "disabled", "oidc_readiness_status": "disabled_dev_mode"},
            "readiness": "degraded",
        },
    )
    monkeypatch.setattr(dashboard, "list_runs", lambda limit=200: [])
    monkeypatch.setattr(dashboard.metadata_index, "get_index_state", lambda: {})
    monkeypatch.setattr(dashboard, "iso_now_utc", lambda: "2026-07-06T00:00:00+00:00")

    validated = SettingsStatusResponse.model_validate(dashboard.get_settings_status(object()))  # type: ignore[arg-type]
    serialized = json.dumps(validated.model_dump(), sort_keys=True)

    assert validated.appOauthOidcDecision["appOauthOidcDecision"] == "app_oauth_oidc_deferred"
    assert validated.appOauthOidcDecision["appOauthOidcProofStatus"] == "not_in_scope"
    assert validated.appOauthOidcDecision["providerAuthSatisfiesAppOauthOidc"] is False
    assert validated.appOauthOidcDecision["appLoginSessionProofSatisfied"] is False
    assert validated.providerProofState["routingPromotionPolicy"]["liveExecutionAllowed"] is False
    assert "OIDC_CLIENT_SECRET" in serialized
    assert "SESSION_SECRET" in serialized
    assert "sk-" not in serialized
    assert "access_token" not in serialized
    assert "id_token" not in serialized
    assert "refresh_token" not in serialized


"""Non-live App OAuth/OIDC scope decision status.

This helper keeps App OAuth/OIDC user login/session proof separate from provider
auth used by AI backend execution. It reports only booleans, env var names, and
reason codes; it does not read env files, tokens, sessions, caches, or secrets.
"""

from __future__ import annotations

from typing import Any

from app.auth.config import AuthSettings, get_auth_settings


APP_OAUTH_OIDC_DECISION = "app_oauth_oidc_deferred"
APP_OAUTH_OIDC_PROOF_STATUS_DEFERRED = "not_in_scope"
FUTURE_PROOF_PROMPT = "APP_OAUTH_OIDC_PROOF_PRECONDITION_NONLIVE"

REQUIRED_FUTURE_PROOF_STEPS = (
    "public_app_base_url",
    "oidc_issuer",
    "oidc_client_id",
    "oidc_client_secret_present",
    "session_secret_present",
    "redirect_uri_registered",
    "auth_code_exchange",
    "session_cookie_validation",
    "logout_session_invalidation",
)

REQUIRED_ENV_NAMES = (
    "AUTH_MODE",
    "APP_BASE_URL",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "SESSION_SECRET",
    "OIDC_REDIRECT_URI",
)


def _app_oauth_oidc_proof_status(settings: AuthSettings) -> str:
    if not settings.auth_enabled:
        return APP_OAUTH_OIDC_PROOF_STATUS_DEFERRED
    configured = bool(
        settings.app_base_url
        and settings.oidc_issuer
        and settings.oidc_client_id
        and settings.oidc_client_secret
        and settings.session_secret
    )
    return "configured_not_verified" if configured else "proof_required"


def build_app_oauth_oidc_decision_status(settings: AuthSettings | None = None) -> dict[str, Any]:
    """Return the current non-live App OAuth/OIDC product-scope decision."""

    resolved = settings or get_auth_settings()
    proof_status = _app_oauth_oidc_proof_status(resolved)
    local_internal = not resolved.auth_enabled
    return {
        "appOauthOidcDecision": APP_OAUTH_OIDC_DECISION,
        "appOauthOidcDecisionLabel": "deferred",
        "appOauthOidcProofStatus": proof_status,
        "authMode": resolved.auth_mode,
        "currentAction": "defer",
        "localInternalWorkbench": local_internal,
        "appLoginSessionProofSatisfied": False,
        "authCodeExchangeProofSatisfied": False,
        "sessionCookieProofSatisfied": False,
        "providerAuthSatisfiesAppOauthOidc": False,
        "codexSdkProviderAuthSatisfiesAppLoginSession": False,
        "publicMcpConnectorProofSatisfied": False,
        "publicHttpsRequiredForOidc": True,
        "requiredFutureProof": list(REQUIRED_FUTURE_PROOF_STEPS),
        "requiredEnvNames": list(REQUIRED_ENV_NAMES),
        "futureProofPrompt": FUTURE_PROOF_PROMPT,
        "productionReady": False,
        "externalBetaReady": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "reasonCodes": [
            "app_oauth_oidc_deferred",
            "local_internal_workbench_scope" if local_internal else "app_oauth_oidc_config_not_proven",
            "provider_auth_not_app_oauth_oidc",
            "codex_sdk_provider_auth_not_app_login_session",
            "oauth_auth_code_session_proof_missing",
            "public_mcp_connector_proof_missing",
            "production_external_beta_no_go",
            "no_oauth_live_flow_run",
        ],
    }


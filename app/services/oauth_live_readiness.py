from __future__ import annotations

import hashlib
import os
from typing import Any
from urllib.parse import urlparse

from app.repositories.files import iso_now_utc
from app.schemas.auth import (
    OAuthLiveExchangeReadinessResponse,
    OAuthOperatorAcceptanceChecklistResponse,
    OAuthPreflightPackageResponse,
    OAuthReadinessRemediationSummaryResponse,
)
from app.services import auth_foundation, oauth_account_links, oauth_dry_run, oauth_token_exchange, operator_audit

READINESS_CHECKS = (
    ("provider_metadata_complete", "Provider metadata complete"),
    ("authorization_url_valid", "Authorization URL valid"),
    ("token_url_present", "Token URL present"),
    ("redirect_uri_valid", "Redirect URI valid"),
    ("client_id_env_var_present", "Client ID env var present"),
    ("client_secret_env_var_present_or_not_required", "Client secret env var present or not required"),
    ("state_store_healthy", "State store healthy"),
    ("pkce_supported", "PKCE supported"),
    ("callback_dry_run_supported", "Callback dry-run supported"),
    ("token_exchange_contract_available", "Token exchange contract available"),
    ("token_redaction_available", "Token redaction available"),
    ("audit_health_valid", "Audit health valid"),
    ("account_link_health_valid", "Account link health valid"),
    ("session_policy_valid", "Session policy valid"),
    ("operator_capability_required", "Operator capability required"),
    ("operator_acceptance_required", "Operator acceptance required"),
    ("dashboard_execution_disabled", "Dashboard execution disabled"),
)

UNSAFE_POLICY_CAPABILITIES = {"run_real_provider_cli", "accept_provider_risk"}
UNSAFE_POLICY_DENIALS = {"run_real_provider_cli", "accept_provider_risk", "provider_live_execution", "oauth_live_token_exchange"}
REMEDIATION_SEVERITY_ORDER = {"blocking": 0, "warning": 1, "info": 2}
REMEDIATION_CHECK_ORDER = {check_id: index for index, (check_id, _label) in enumerate(READINESS_CHECKS)}
REMEDIATION_FORBIDDEN_ACTIONS = [
    "Do not perform live token exchange in M11.1.",
    "Do not store access tokens, refresh tokens, ID tokens, authorization codes, API keys, or provider credentials.",
    "Do not add dashboard login, token exchange, or provider execution buttons.",
    "Do not let account links grant provider execution or risk acceptance.",
]
SECRET_HANDLING_NOTE = (
    "Env var values, OAuth tokens, authorization codes, API keys, and provider credentials must stay out of committed "
    "files, API responses, audit logs, and account-link metadata."
)
PREFLIGHT_FORBIDDEN_ACTIONS = [
    "Do not perform live token exchange in M11.2.",
    "Do not store access tokens, refresh tokens, ID tokens, authorization codes, API keys, or provider credentials.",
    "Do not add dashboard login, token exchange, or provider execution buttons.",
    "Do not let account links grant provider execution or risk acceptance.",
]
REQUIRED_OPERATOR_CONFIRMATIONS = [
    "confirmed_no_secret_commit",
    "confirmed_env_vars_configured",
    "confirmed_redirect_uri_registered",
    "confirmed_audit_health_reviewed",
    "confirmed_token_storage_policy",
    "confirmed_dashboard_execution_disabled",
    "confirmed_provider_acceptance_gate_independent",
]
CHECKLIST_CONFIRMATION_FIELDS = {
    "confirmed_no_secret_commit": "confirmedNoSecretCommit",
    "confirmed_env_vars_configured": "confirmedEnvVarsConfigured",
    "confirmed_redirect_uri_registered": "confirmedRedirectUriRegistered",
    "confirmed_audit_health_reviewed": "confirmedAuditHealthReviewed",
    "confirmed_token_storage_policy": "confirmedTokenStoragePolicy",
    "confirmed_dashboard_execution_disabled": "confirmedDashboardExecutionDisabled",
    "confirmed_provider_acceptance_gate_independent": "confirmedProviderAcceptanceGateIndependent",
}


def _stable_readiness_id(provider_id: str) -> str:
    digest = hashlib.sha256(str(provider_id or "oauth-provider").encode("utf-8")).hexdigest()[:16]
    return f"oauth-readiness-{digest}"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item).strip()))


def _stable_remediation_id(provider_id: str, check_id: str, reason_code: str) -> str:
    safe_provider = str(provider_id or "oauth-provider").strip().lower().replace("_", "-")
    safe_check = str(check_id or "readiness").strip().lower().replace("_", "-")
    safe_reason = str(reason_code or "remediation").strip().lower().replace("_", "-")
    digest = hashlib.sha256(f"{safe_provider}:{safe_check}:{safe_reason}".encode("utf-8")).hexdigest()[:8]
    return f"remediation-{safe_provider}-{safe_check}-{safe_reason}-{digest}"


def _stable_preflight_id(provider_id: str) -> str:
    digest = hashlib.sha256(str(provider_id or "oauth-provider").encode("utf-8")).hexdigest()[:16]
    return f"oauth-preflight-{digest}"


def _check(check_id: str, label: str, status: str, reason_codes: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "checkId": check_id,
        "label": label,
        "status": status,
        "reasonCodes": _unique(reason_codes or []),
        "warnings": _unique(warnings or []),
    }


def _https_url_present(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _redirect_uri_valid(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def _env_var_present(env_var_name: str | None) -> bool:
    name = str(env_var_name or "").strip()
    return bool(name and name in os.environ)


def _state_store_check() -> dict[str, Any]:
    status = oauth_dry_run.build_oauth_dry_run_status()
    if status.get("malformedStateCount"):
        return _check(
            "state_store_healthy",
            "State store healthy",
            "failed",
            ["malformed_oauth_state_records"],
            ["OAuth dry-run state contains malformed records; future live exchange must stay blocked."],
        )
    if status.get("expiredStateCount"):
        return _check(
            "state_store_healthy",
            "State store healthy",
            "warning",
            ["expired_oauth_state_records"],
            ["Expired OAuth dry-run state exists; it is ignored for readiness."],
        )
    return _check("state_store_healthy", "State store healthy", "passed", ["state_store_metadata_only"])


def _audit_health_check(audit_health: dict[str, Any]) -> dict[str, Any]:
    status = str(audit_health.get("auditLogStatus") or "empty")
    if status == "errors":
        return _check(
            "audit_health_valid",
            "Audit health valid",
            "failed",
            ["audit_health_errors"],
            ["Audit replay reports errors; live exchange readiness is blocked."],
        )
    if status == "warnings":
        return _check(
            "audit_health_valid",
            "Audit health valid",
            "warning",
            ["audit_health_warnings"],
            ["Audit replay reports warnings; review before future live exchange enablement."],
        )
    return _check("audit_health_valid", "Audit health valid", "passed", ["audit_health_valid"])


def _account_link_health_check(account_link_health: dict[str, Any]) -> dict[str, Any]:
    status = str(account_link_health.get("healthStatus") or "empty")
    if status == "errors":
        return _check(
            "account_link_health_valid",
            "Account link health valid",
            "failed",
            ["account_link_health_error"],
            ["Account link health found token/live-exchange violations or invalid metadata."],
        )
    if status == "warnings":
        return _check(
            "account_link_health_valid",
            "Account link health valid",
            "warning",
            ["account_link_health_warnings"],
            ["Account link health has inactive or duplicate metadata; invalid links are ignored."],
        )
    return _check("account_link_health_valid", "Account link health valid", "passed", ["account_link_health_valid"])


def _session_policy_check(session_policy: dict[str, Any]) -> dict[str, Any]:
    denied = set(str(item) for item in session_policy.get("deniedByPolicy") or [])
    sources = session_policy.get("capabilitySources") if isinstance(session_policy.get("capabilitySources"), dict) else {}
    unsafe_source_grants = [
        capability
        for capability in UNSAFE_POLICY_CAPABILITIES
        if any("account_link" in str(source) for source in sources.get(capability, []))
    ]
    unsafe_effective = bool(session_policy.get("accountLinkGrantsProviderExecution")) or bool(session_policy.get("tokenStorageEnabled")) or bool(
        session_policy.get("liveTokenExchangePerformed")
    )
    unsafe_influence = str(session_policy.get("accountLinkInfluence") or "none") not in {"none", "display_only"}
    missing_denials = sorted(UNSAFE_POLICY_DENIALS - denied)
    if unsafe_effective or unsafe_source_grants or unsafe_influence or missing_denials:
        return _check(
            "session_policy_valid",
            "Session policy valid",
            "failed",
            ["unsafe_session_capability_policy"],
            ["Account-link state must not grant provider execution, risk acceptance, token exchange, or token storage."],
        )
    return _check("session_policy_valid", "Session policy valid", "passed", ["session_policy_display_only"])


def _metadata_checks(validation: dict[str, Any], metadata: dict[str, Any], redirect_uri: str | None) -> list[dict[str, Any]]:
    errors = list(validation.get("errors") or [])
    warnings = list(validation.get("warnings") or [])
    provider_complete = (
        validation.get("validationStatus") == "valid"
        and bool(str(metadata.get("providerId") or "").strip())
        and _https_url_present(metadata.get("authorizationUrl"))
        and _https_url_present(metadata.get("tokenUrl"))
        and bool(str(metadata.get("clientIdEnvVar") or "").strip())
    )
    client_id_env_var = str(metadata.get("clientIdEnvVar") or "").strip() or None
    client_secret_env_var = str(metadata.get("clientSecretEnvVar") or "").strip() or None
    checks = [
        _check(
            "provider_metadata_complete",
            "Provider metadata complete",
            "passed" if provider_complete else "failed",
            ["provider_metadata_complete"] if provider_complete else ["incomplete_provider_metadata"],
            [] if provider_complete else ["Provider metadata must include safe provider ID, HTTPS endpoints, and client ID env-var reference."],
        ),
        _check(
            "authorization_url_valid",
            "Authorization URL valid",
            "passed" if _https_url_present(metadata.get("authorizationUrl")) else "failed",
            ["authorization_url_valid"] if _https_url_present(metadata.get("authorizationUrl")) else ["missing_or_invalid_authorization_url"],
        ),
        _check(
            "token_url_present",
            "Token URL present",
            "passed" if _https_url_present(metadata.get("tokenUrl")) else "failed",
            ["token_url_present"] if _https_url_present(metadata.get("tokenUrl")) else ["missing_or_invalid_token_url"],
        ),
        _check(
            "redirect_uri_valid",
            "Redirect URI valid",
            "passed" if _redirect_uri_valid(redirect_uri) else "failed",
            ["redirect_uri_valid"] if _redirect_uri_valid(redirect_uri) else ["invalid_redirect_uri"],
            [] if _redirect_uri_valid(redirect_uri) else ["Redirect URI must be HTTPS or localhost HTTP for local dry-run readiness."],
        ),
        _check(
            "client_id_env_var_present",
            "Client ID env var present",
            "passed" if _env_var_present(client_id_env_var) else "failed",
            ["client_id_env_var_present"] if _env_var_present(client_id_env_var) else ["missing_client_id_env_var"],
            [] if _env_var_present(client_id_env_var) else [f"{client_id_env_var or 'client ID env var'} is not set; the value was not inspected."],
        ),
        _check(
            "client_secret_env_var_present_or_not_required",
            "Client secret env var present or not required",
            "passed" if not client_secret_env_var or _env_var_present(client_secret_env_var) else "failed",
            (
                ["client_secret_not_required_for_public_pkce_client"]
                if not client_secret_env_var
                else ["client_secret_env_var_present"]
                if _env_var_present(client_secret_env_var)
                else ["missing_client_secret_env_var"]
            ),
            [] if not client_secret_env_var or _env_var_present(client_secret_env_var) else [f"{client_secret_env_var} is not set; the value was not inspected."],
        ),
    ]
    if errors:
        checks[0]["reasonCodes"] = _unique([*checks[0]["reasonCodes"], "invalid_provider_metadata"])
        checks[0]["warnings"] = _unique([*checks[0]["warnings"], *errors])
    if warnings and provider_complete:
        checks[0]["warnings"] = _unique([*checks[0]["warnings"], *warnings])
    return checks


def _contract_checks(token_status: dict[str, Any]) -> list[dict[str, Any]]:
    contract_available = bool(token_status.get("mockExchangeSupported")) and token_status.get("tokenStorageEnabled") is False
    redaction_available = bool(token_status.get("accountLinkPreviewEnabled")) and token_status.get("tokenStorageEnabled") is False
    return [
        _check(
            "pkce_supported",
            "PKCE supported",
            "passed",
            ["pkce_s256_supported"],
        ),
        _check(
            "callback_dry_run_supported",
            "Callback dry-run supported",
            "passed",
            ["callback_dry_run_supported"],
        ),
        _check(
            "token_exchange_contract_available",
            "Token exchange contract available",
            "passed" if contract_available else "failed",
            ["token_exchange_contract_available"] if contract_available else ["token_exchange_contract_missing"],
        ),
        _check(
            "token_redaction_available",
            "Token redaction available",
            "passed" if redaction_available else "failed",
            ["token_redaction_available"] if redaction_available else ["token_redaction_missing"],
        ),
    ]


def _policy_requirement_checks(operator_identity: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities = set(str(item) for item in operator_identity.get("capabilities") or [])
    return [
        _check(
            "operator_capability_required",
            "Operator capability required",
            "passed",
            ["future_live_exchange_requires_explicit_capability"],
            [
                "Future live token exchange will require an explicit operator capability; account links do not grant it.",
            ],
        ),
        _check(
            "operator_acceptance_required",
            "Operator acceptance required",
            "passed",
            ["provider_acceptance_gate_preserved"],
            [
                "Provider execution remains independent and requires CLI acceptance gates.",
            ],
        ),
        _check(
            "dashboard_execution_disabled",
            "Dashboard execution disabled",
            "passed",
            ["dashboard_execution_disabled"],
            [
                "Dashboard may display readiness only; it cannot login, exchange tokens, or execute providers.",
            ]
            if "run_real_provider_cli" in capabilities
            else [],
        ),
    ]


def _metadata_env_names(provider_metadata: dict[str, Any] | None) -> dict[str, str | None]:
    validation = auth_foundation.validate_oauth_provider_metadata(provider_metadata or {})
    metadata = validation.get("oauthProviderMetadata") or {}
    return {
        "providerId": str(metadata.get("providerId") or "oauth-provider"),
        "clientIdEnvVar": str(metadata.get("clientIdEnvVar") or "").strip() or None,
        "clientSecretEnvVar": str(metadata.get("clientSecretEnvVar") or "").strip() or None,
    }


def _env_var_remediation(
    *,
    provider_id: str,
    check_id: str,
    reason_code: str,
    env_var_name: str | None,
    label: str,
) -> dict[str, Any]:
    env_names = [env_var_name] if env_var_name else []
    if env_var_name:
        operator_action = f"Set {env_var_name} outside persisted config."
        safe_hint = f"$env:{env_var_name}='<set outside git>'"
    else:
        operator_action = f"Configure a {label} env var name, then set that env var outside persisted config."
        safe_hint = None
    return {
        "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
        "providerId": provider_id,
        "readinessCheck": check_id,
        "reasonCode": reason_code,
        "severity": "blocking",
        "category": "env_var",
        "operatorAction": operator_action,
        "safeCommandOrConfigHint": safe_hint,
        "envVarNames": env_names,
        "docsAnchor": "env-vars",
        "forbiddenActions": [
            "Do not commit OAuth env var values.",
            "Do not paste OAuth client secrets into provider metadata, audit events, account links, or docs.",
        ],
        "canBeResolvedWithoutCodeChange": True,
        "requiresRestart": True,
        "secretHandlingNote": SECRET_HANDLING_NOTE,
    }


def _remediation_for_reason(
    *,
    provider_id: str,
    check_id: str,
    reason_code: str,
    check_status: str,
    env_names: dict[str, str | None],
) -> dict[str, Any] | None:
    if reason_code == "missing_client_id_env_var":
        return _env_var_remediation(
            provider_id=provider_id,
            check_id=check_id,
            reason_code=reason_code,
            env_var_name=env_names.get("clientIdEnvVar"),
            label="client ID",
        )
    if reason_code == "missing_client_secret_env_var":
        return _env_var_remediation(
            provider_id=provider_id,
            check_id=check_id,
            reason_code=reason_code,
            env_var_name=env_names.get("clientSecretEnvVar"),
            label="client secret",
        )
    if reason_code == "invalid_redirect_uri":
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "blocking",
            "category": "redirect_uri",
            "operatorAction": "Configure a redirect URI that uses HTTPS, or localhost HTTP for local dry-run validation only.",
            "safeCommandOrConfigHint": "https://<dashboard-origin>/auth/callback",
            "envVarNames": [],
            "docsAnchor": "redirect-uri",
            "forbiddenActions": ["Do not use javascript:, file:, data:, or non-local HTTP redirect URIs."],
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code in {"incomplete_provider_metadata", "invalid_provider_metadata", "missing_or_invalid_authorization_url", "missing_or_invalid_token_url"}:
        field = {
            "missing_or_invalid_authorization_url": "authorization URL",
            "missing_or_invalid_token_url": "token URL",
        }.get(reason_code, "OAuth provider metadata")
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "blocking",
            "category": "metadata",
            "operatorAction": f"Complete {field} with stable provider IDs, HTTPS endpoints, and env-var references only.",
            "safeCommandOrConfigHint": None,
            "envVarNames": _unique([name for name in (env_names.get("clientIdEnvVar"), env_names.get("clientSecretEnvVar")) if name]),
            "docsAnchor": "provider-metadata",
            "forbiddenActions": ["Do not place OAuth client secrets, tokens, or authorization codes in provider metadata."],
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code == "malformed_oauth_state_records":
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "blocking",
            "category": "state_store",
            "operatorAction": "Regenerate OAuth dry-run state with the prepare endpoint after clearing malformed local ignored state records.",
            "safeCommandOrConfigHint": "POST /auth/oauth/authorization/prepare",
            "envVarNames": [],
            "docsAnchor": "state-pkce",
            "forbiddenActions": ["Do not store authorization codes or token endpoint responses in the state store."],
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code == "expired_oauth_state_records":
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "warning",
            "category": "state_store",
            "operatorAction": "Refresh OAuth dry-run state before future enablement; expired local state is ignored.",
            "safeCommandOrConfigHint": "POST /auth/oauth/authorization/prepare",
            "envVarNames": [],
            "docsAnchor": "state-pkce",
            "forbiddenActions": ["Do not reuse expired OAuth state for token exchange."],
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code in {"audit_health_errors", "audit_health_warnings"}:
        severity = "blocking" if reason_code == "audit_health_errors" else "warning"
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": severity,
            "category": "audit_health",
            "operatorAction": "Run audit replay validation and resolve malformed, secret-like, or inconsistent audit records before future enablement.",
            "safeCommandOrConfigHint": ".\\.venv\\Scripts\\python.exe -m pytest tests/test_auth_foundation.py -q",
            "envVarNames": [],
            "docsAnchor": "audit-health",
            "forbiddenActions": ["Do not edit audit records to include token values, authorization codes, or provider credentials."],
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code in {"account_link_health_error", "account_link_health_warnings"}:
        severity = "blocking" if reason_code == "account_link_health_error" else "warning"
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": severity,
            "category": "account_link_health",
            "operatorAction": "Validate metadata-only account links and remove token-like, live-exchange, duplicate, invalid, or revoked-placeholder records from active binding.",
            "safeCommandOrConfigHint": "GET /auth/oauth/account-links/health",
            "envVarNames": [],
            "docsAnchor": "account-link-health",
            "forbiddenActions": ["Do not add access tokens, refresh tokens, ID tokens, authorization codes, or raw subjects to account-link metadata."],
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code == "unsafe_session_capability_policy":
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "blocking",
            "category": "session_policy",
            "operatorAction": "Restore the session policy so account-link state is display-only and never grants provider execution, risk acceptance, token storage, or token exchange.",
            "safeCommandOrConfigHint": None,
            "envVarNames": [],
            "docsAnchor": "session-policy",
            "forbiddenActions": REMEDIATION_FORBIDDEN_ACTIONS,
            "canBeResolvedWithoutCodeChange": False,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code in {"token_exchange_contract_missing", "token_redaction_missing"}:
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "blocking",
            "category": "future_live_exchange",
            "operatorAction": "Restore the local token-exchange contract and redaction checks before considering any future live exchange milestone.",
            "safeCommandOrConfigHint": None,
            "envVarNames": [],
            "docsAnchor": "future-boundary",
            "forbiddenActions": REMEDIATION_FORBIDDEN_ACTIONS,
            "canBeResolvedWithoutCodeChange": False,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code in {"live_token_exchange_disabled", "token_storage_disabled", "external_auth_calls_disabled"}:
        labels = {
            "live_token_exchange_disabled": "Live token exchange is intentionally disabled until a future milestone.",
            "token_storage_disabled": "Token storage is intentionally disabled; readiness remediation must not add token persistence.",
            "external_auth_calls_disabled": "External auth calls are intentionally disabled in the readiness gate.",
        }
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "info",
            "category": "future_live_exchange",
            "operatorAction": labels[reason_code],
            "safeCommandOrConfigHint": None,
            "envVarNames": [],
            "docsAnchor": "future-boundary",
            "forbiddenActions": REMEDIATION_FORBIDDEN_ACTIONS,
            "canBeResolvedWithoutCodeChange": False,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if reason_code in {"future_live_exchange_requires_explicit_capability", "provider_acceptance_gate_preserved", "dashboard_execution_disabled"}:
        category = "dashboard_safety" if reason_code == "dashboard_execution_disabled" else "future_live_exchange"
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "info",
            "category": category,
            "operatorAction": "Keep provider execution and future live OAuth enablement behind explicit operator capabilities and CLI acceptance gates.",
            "safeCommandOrConfigHint": None,
            "envVarNames": [],
            "docsAnchor": "future-boundary",
            "forbiddenActions": REMEDIATION_FORBIDDEN_ACTIONS,
            "canBeResolvedWithoutCodeChange": False,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    if check_status in {"failed", "warning"}:
        return {
            "remediationId": _stable_remediation_id(provider_id, check_id, reason_code),
            "providerId": provider_id,
            "readinessCheck": check_id,
            "reasonCode": reason_code,
            "severity": "blocking" if check_status == "failed" else "warning",
            "category": "future_live_exchange",
            "operatorAction": "Review the readiness check and resolve it before any future live OAuth exchange milestone.",
            "safeCommandOrConfigHint": None,
            "envVarNames": [],
            "docsAnchor": "readiness-checks",
            "forbiddenActions": REMEDIATION_FORBIDDEN_ACTIONS,
            "canBeResolvedWithoutCodeChange": True,
            "requiresRestart": False,
            "secretHandlingNote": SECRET_HANDLING_NOTE,
        }
    return None


def _sort_remediation_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            REMEDIATION_SEVERITY_ORDER.get(str(item.get("severity")), 99),
            REMEDIATION_CHECK_ORDER.get(str(item.get("readinessCheck")), 99),
            str(item.get("reasonCode") or ""),
        ),
    )


def _remediation_summary_status(items: list[dict[str, Any]]) -> str:
    if any(item.get("severity") == "blocking" for item in items):
        return "blocking"
    if any(item.get("severity") == "warning" for item in items):
        return "warnings"
    return "clear"


def build_readiness_remediation(
    *,
    readiness: dict[str, Any] | None = None,
    provider_metadata: dict[str, Any] | None = None,
    redirect_uri: str | None = None,
    operator_identity: dict[str, Any] | None = None,
    audit_health: dict[str, Any] | None = None,
    account_link_health: dict[str, Any] | None = None,
    session_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_readiness = readiness or build_live_exchange_readiness(
        provider_metadata=provider_metadata,
        redirect_uri=redirect_uri,
        operator_identity=operator_identity,
        audit_health=audit_health,
        account_link_health=account_link_health,
        session_policy=session_policy,
    )
    env_names = _metadata_env_names(provider_metadata)
    provider_id = str(resolved_readiness.get("providerId") or env_names.get("providerId") or "oauth-provider")

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for check in resolved_readiness.get("checks") or []:
        check_id = str(check.get("checkId") or "readiness")
        check_status = str(check.get("status") or "passed")
        if check_status not in {"failed", "warning"}:
            continue
        for reason_code in check.get("reasonCodes") or []:
            reason_text = str(reason_code)
            key = (check_id, reason_text)
            if key in seen:
                continue
            item = _remediation_for_reason(
                provider_id=provider_id,
                check_id=check_id,
                reason_code=reason_text,
                check_status=check_status,
                env_names=env_names,
            )
            if item:
                items.append(item)
                seen.add(key)

    for reason_code in ("live_token_exchange_disabled", "token_storage_disabled", "external_auth_calls_disabled"):
        if reason_code not in set(resolved_readiness.get("reasonCodes") or []):
            continue
        key = ("future_live_exchange", reason_code)
        if key in seen:
            continue
        item = _remediation_for_reason(
            provider_id=provider_id,
            check_id="future_live_exchange",
            reason_code=reason_code,
            check_status="passed",
            env_names=env_names,
        )
        if item:
            items.append(item)
            seen.add(key)

    items = _sort_remediation_items(items)
    blocking_count = sum(1 for item in items if item.get("severity") == "blocking")
    warning_count = sum(1 for item in items if item.get("severity") == "warning")
    info_count = sum(1 for item in items if item.get("severity") == "info")
    safe_next_steps = _unique(
        [
            item["operatorAction"]
            for item in items
            if item.get("severity") in {"blocking", "warning"} and item.get("operatorAction")
        ]
    )
    if not safe_next_steps:
        safe_next_steps = [
            "Keep OAuth live exchange and token storage disabled until a future milestone explicitly enables them.",
        ]
    result = OAuthReadinessRemediationSummaryResponse.model_validate(
        {
            "providerId": provider_id,
            "readinessStatus": resolved_readiness.get("readinessStatus") or "blocked",
            "remediationStatus": _remediation_summary_status(items),
            "blockingRemediationCount": blocking_count,
            "warningRemediationCount": warning_count,
            "infoRemediationCount": info_count,
            "liveExchangeEnabled": False,
            "tokenStorageEnabled": False,
            "items": items,
            "safeNextSteps": safe_next_steps[:5],
            "forbiddenActions": REMEDIATION_FORBIDDEN_ACTIONS,
        }
    ).model_dump(mode="json")
    return result


def _preflight_check(check_id: str, label: str, status: str, reason_codes: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "checkId": check_id,
        "label": label,
        "status": status,
        "reasonCodes": _unique(reason_codes or []),
        "warnings": _unique(warnings or []),
    }


def _preflight_status_from_health(value: str) -> str:
    if value == "errors":
        return "blocking"
    if value == "warnings":
        return "warning"
    return "passed"


def _preflight_summary_status(checks: list[dict[str, Any]]) -> str:
    if any(check.get("status") == "blocking" for check in checks):
        return "blocked"
    if any(check.get("status") == "warning" for check in checks):
        return "warnings"
    return "ready"


def _session_policy_for_account_link_health(
    identity: dict[str, Any],
    health: dict[str, Any],
) -> dict[str, Any]:
    # Health inspection may observe corrupt/unsafe records, but the capability
    # policy must never consume that partial prefix. Treat the link set as
    # unavailable and preserve operator-role-only permissions.
    account_links = [] if health.get("healthStatus") == "errors" else None
    return oauth_account_links.build_session_capability_policy(
        identity,
        account_links=account_links,
    )


def build_preflight_package(
    *,
    provider_metadata: dict[str, Any] | None = None,
    redirect_uri: str | None = None,
    operator_identity: dict[str, Any] | None = None,
    audit_health: dict[str, Any] | None = None,
    account_link_health: dict[str, Any] | None = None,
    session_policy: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    remediation: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    identity = operator_identity or auth_foundation.operator_identity_for_user(None)
    resolved_audit_health = audit_health or operator_audit.validate_audit_log()
    resolved_account_link_health = account_link_health or oauth_account_links.build_account_link_health_summary()
    resolved_session_policy = session_policy or _session_policy_for_account_link_health(
        identity,
        resolved_account_link_health,
    )
    resolved_readiness = readiness or build_live_exchange_readiness(
        provider_metadata=provider_metadata,
        redirect_uri=redirect_uri,
        operator_identity=identity,
        audit_health=resolved_audit_health,
        account_link_health=resolved_account_link_health,
        session_policy=resolved_session_policy,
    )
    resolved_remediation = remediation or build_readiness_remediation(
        readiness=resolved_readiness,
        provider_metadata=provider_metadata,
    )
    env_names = _metadata_env_names(provider_metadata)
    provider_id = str(resolved_readiness.get("providerId") or resolved_remediation.get("providerId") or env_names.get("providerId") or "oauth-provider")
    env_var_names = _unique([name for name in (env_names.get("clientIdEnvVar"), env_names.get("clientSecretEnvVar")) if name])
    token_status = oauth_token_exchange.build_token_exchange_status()
    audit_status = str(resolved_audit_health.get("auditLogStatus") or "empty")
    account_link_status = str(resolved_account_link_health.get("healthStatus") or "empty")
    readiness_status = str(resolved_readiness.get("readinessStatus") or "blocked")
    remediation_status = str(resolved_remediation.get("remediationStatus") or "blocking")
    session_policy_safe = (
        resolved_session_policy.get("accountLinkGrantsProviderExecution") is False
        and resolved_session_policy.get("tokenStorageEnabled") is False
        and resolved_session_policy.get("liveTokenExchangePerformed") is False
        and str(resolved_session_policy.get("accountLinkInfluence") or "none") in {"none", "display_only"}
    )
    checks = [
        _preflight_check(
            "readiness_gate",
            "Readiness gate",
            "blocking" if readiness_status == "blocked" else "warning" if readiness_status == "warnings_only" else "passed",
            list(resolved_readiness.get("reasonCodes") or []),
        ),
        _preflight_check(
            "remediation_summary",
            "Remediation summary",
            "blocking" if remediation_status == "blocking" else "warning" if remediation_status == "warnings" else "passed",
            ["readiness_remediation_reviewed"],
        ),
        _preflight_check(
            "audit_health",
            "Audit health",
            _preflight_status_from_health(audit_status),
            ["audit_health_review_required" if audit_status in {"warnings", "errors"} else "audit_health_available"],
        ),
        _preflight_check(
            "account_link_health",
            "Account-link health",
            _preflight_status_from_health(account_link_status),
            ["account_link_health_review_required" if account_link_status in {"warnings", "errors"} else "account_link_health_available"],
        ),
        _preflight_check(
            "session_policy",
            "Session capability policy",
            "passed" if session_policy_safe else "blocking",
            ["session_policy_display_only" if session_policy_safe else "unsafe_session_capability_policy"],
        ),
        _preflight_check(
            "token_storage_disabled",
            "Token storage disabled",
            "passed" if token_status.get("tokenStorageEnabled") is False else "blocking",
            ["token_storage_disabled"],
        ),
        _preflight_check(
            "live_exchange_disabled",
            "Live exchange disabled",
            "passed" if token_status.get("liveExchangeSupported") is False else "blocking",
            ["live_token_exchange_disabled"],
        ),
        _preflight_check(
            "env_var_values_not_exposed",
            "Env var values not exposed",
            "passed",
            ["env_var_names_only"],
        ),
        _preflight_check(
            "dashboard_execution_disabled",
            "Dashboard execution disabled",
            "passed",
            ["dashboard_execution_disabled"],
        ),
    ]
    blocking_count = sum(1 for check in checks if check.get("status") == "blocking")
    warning_count = sum(1 for check in checks if check.get("status") == "warning")
    acceptance_ready = blocking_count == 0
    safe_next_steps = _unique(
        [
            *list(resolved_remediation.get("safeNextSteps") or []),
            "Review the operator acceptance checklist before future enablement.",
            "Keep live OAuth token exchange and token storage disabled until a future milestone.",
        ]
    )[:6]
    reason_codes = _unique(
        [
            "oauth_preflight_package_read_only",
            "operator_acceptance_checklist_required",
            "token_storage_disabled",
            "live_token_exchange_disabled",
            *list(resolved_readiness.get("reasonCodes") or []),
            *list(resolved_remediation.get("reasonCodes") or []),
        ]
    )
    result = OAuthPreflightPackageResponse.model_validate(
        {
            "preflightId": _stable_preflight_id(provider_id),
            "providerId": provider_id,
            "generatedAt": now or iso_now_utc(),
            "readinessStatus": readiness_status,
            "remediationSummary": resolved_remediation,
            "auditHealthStatus": audit_status,
            "accountLinkHealthStatus": account_link_status,
            "tokenStorageEnabled": False,
            "liveExchangeEnabled": False,
            "envVarNames": env_var_names,
            "envVarValuesExposed": False,
            "checks": checks,
            "summary": {
                "providerId": provider_id,
                "preflightStatus": _preflight_summary_status(checks),
                "blockingCheckCount": blocking_count,
                "warningCheckCount": warning_count,
                "requiredConfirmationCount": len(REQUIRED_OPERATOR_CONFIRMATIONS),
                "acceptanceReady": acceptance_ready,
                "tokenStorageEnabled": False,
                "liveExchangeEnabled": False,
            },
            "forbiddenActions": PREFLIGHT_FORBIDDEN_ACTIONS,
            "safeNextSteps": safe_next_steps,
            "requiredOperatorConfirmations": REQUIRED_OPERATOR_CONFIRMATIONS,
            "acceptanceReady": acceptance_ready,
            "reasonCodes": reason_codes,
            "warnings": [
                "OAuth preflight package is read-only and does not enable live token exchange.",
                "Operator checklist confirmation is advisory until a future milestone implements live exchange.",
            ],
        }
    ).model_dump(mode="json")
    return result


def validate_operator_acceptance_checklist(
    payload: dict[str, Any],
    *,
    operator_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_payload = dict(payload or {})
    provider_id = str(request_payload.get("providerId") or request_payload.get("provider_id") or "oauth-provider")
    operator_id = request_payload.get("acceptedByOperatorId") or request_payload.get("accepted_by_operator_id")
    if operator_id is None and operator_identity:
        operator_id = operator_identity.get("operatorId")
    missing_confirmations = [
        confirmation
        for confirmation, field_name in CHECKLIST_CONFIRMATION_FIELDS.items()
        if request_payload.get(field_name) is not True
    ]
    acceptance_valid = not missing_confirmations
    reason_codes = ["checklist_preview_only", "live_exchange_still_disabled", "token_storage_disabled"]
    reason_codes.append("operator_acceptance_checklist_complete" if acceptance_valid else "missing_operator_confirmations")
    warnings = [
        "Checklist validation is preview-only and does not enable live OAuth token exchange.",
        "Token storage remains disabled even when every operator confirmation is present.",
    ]
    result = OAuthOperatorAcceptanceChecklistResponse.model_validate(
        {
            "providerId": provider_id,
            "confirmedNoSecretCommit": request_payload.get("confirmedNoSecretCommit") is True,
            "confirmedEnvVarsConfigured": request_payload.get("confirmedEnvVarsConfigured") is True,
            "confirmedRedirectUriRegistered": request_payload.get("confirmedRedirectUriRegistered") is True,
            "confirmedAuditHealthReviewed": request_payload.get("confirmedAuditHealthReviewed") is True,
            "confirmedTokenStoragePolicy": request_payload.get("confirmedTokenStoragePolicy") is True,
            "confirmedDashboardExecutionDisabled": request_payload.get("confirmedDashboardExecutionDisabled") is True,
            "confirmedProviderAcceptanceGateIndependent": request_payload.get("confirmedProviderAcceptanceGateIndependent") is True,
            "acceptedByOperatorId": str(operator_id) if operator_id else None,
            "acceptedAt": request_payload.get("acceptedAt") or request_payload.get("accepted_at"),
            "acceptanceValid": acceptance_valid,
            "missingConfirmations": missing_confirmations,
            "liveExchangeStillDisabled": True,
            "liveExchangeEnabled": False,
            "tokenStorageEnabled": False,
            "reasonCodes": reason_codes,
            "warnings": warnings,
        }
    ).model_dump(mode="json")
    return result


def build_live_exchange_readiness(
    *,
    provider_metadata: dict[str, Any] | None = None,
    redirect_uri: str | None = None,
    operator_identity: dict[str, Any] | None = None,
    audit_health: dict[str, Any] | None = None,
    account_link_health: dict[str, Any] | None = None,
    session_policy: dict[str, Any] | None = None,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
) -> dict[str, Any]:
    identity = operator_identity or auth_foundation.operator_identity_for_user(None)
    validation = auth_foundation.validate_oauth_provider_metadata(provider_metadata or {})
    metadata = validation["oauthProviderMetadata"]
    provider_id = str(metadata.get("providerId") or "oauth-provider")
    token_status = oauth_token_exchange.build_token_exchange_status()
    resolved_audit_health = audit_health or operator_audit.validate_audit_log()
    resolved_account_link_health = account_link_health or oauth_account_links.build_account_link_health_summary()
    resolved_session_policy = session_policy or _session_policy_for_account_link_health(
        identity,
        resolved_account_link_health,
    )

    checks: list[dict[str, Any]] = [
        *_metadata_checks(validation, metadata, redirect_uri),
        _state_store_check(),
        *_contract_checks(token_status),
        _audit_health_check(resolved_audit_health),
        _account_link_health_check(resolved_account_link_health),
        _session_policy_check(resolved_session_policy),
        *_policy_requirement_checks(identity),
    ]
    known_labels = dict(READINESS_CHECKS)
    for item in checks:
        item["label"] = known_labels.get(item["checkId"], item["label"])

    failed_checks = [item["checkId"] for item in checks if item["status"] == "failed"]
    warning_checks = [item["checkId"] for item in checks if item["status"] == "warning"]
    reason_codes = _unique(
        [
            "live_token_exchange_disabled",
            "token_storage_disabled",
            "external_auth_calls_disabled",
            *[reason for item in checks for reason in item.get("reasonCodes", [])],
        ]
    )
    if failed_checks:
        readiness_status = "blocked"
    elif warning_checks:
        readiness_status = "warnings_only"
    else:
        readiness_status = "ready_for_future_live_exchange"

    result = OAuthLiveExchangeReadinessResponse.model_validate(
        {
            "readinessId": _stable_readiness_id(provider_id),
            "providerId": provider_id,
            "readinessStatus": readiness_status,
            "liveExchangeEnabled": False,
            "tokenStorageEnabled": False,
            "externalAuthCallPerformed": False,
            "checks": checks,
            "failedChecks": failed_checks,
            "warningChecks": warning_checks,
            "reasonCodes": reason_codes,
            "safeNextSteps": [
                "Configure complete OAuth metadata with HTTPS endpoints and env-var references only.",
                "Set required OAuth env vars outside persisted config before future enablement.",
                "Run authorization URL and callback dry-runs before any future live exchange implementation.",
                "Keep provider execution CLI-only and acceptance-gated.",
            ],
            "forbiddenActions": [
                "Do not perform live token exchange in M11.",
                "Do not store access tokens, refresh tokens, ID tokens, authorization codes, API keys, or provider credentials.",
                "Do not add dashboard login, token exchange, or provider execution buttons.",
                "Do not let account links grant provider execution or risk acceptance.",
            ],
        }
    ).model_dump(mode="json")

    if audit:
        operator_audit.record_event(
            action_type="oauth_live_exchange_readiness",
            decision="rejected" if failed_checks else "preview_only",
            operator_id=operator_id,
            auth_mode=auth_mode,
            capability_required="view_dashboard",
            capability_present=capability_present,
            reason_codes=result["reasonCodes"],
            target_type="oauth_provider",
            target_id=provider_id,
            risk_level="read_only",
            metadata={
                "providerId": provider_id,
                "readinessStatus": result["readinessStatus"],
                "failedChecks": result["failedChecks"],
                "warningChecks": result["warningChecks"],
                "clientIdEnvVar": metadata.get("clientIdEnvVar"),
                "clientSecretEnvVar": metadata.get("clientSecretEnvVar"),
                "liveExchangeEnabled": False,
                "tokenStorageEnabled": False,
                "externalAuthCallPerformed": False,
            },
        )
    return result

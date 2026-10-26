from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from app.auth.config import AuthSettings, get_auth_settings
from app.auth.security_types import ProductUser
from app.services import operator_audit

AUTH_CAPABILITIES = (
    "view_dashboard",
    "edit_topic_config",
    "preview_run_plan",
    "prepare_provider_manual_run",
    "run_fixture_provider",
    "run_real_provider_cli",
    "accept_provider_risk",
    "save_provider_config",
    "view_provider_readiness",
)

ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "viewer": ("view_dashboard", "preview_run_plan", "view_provider_readiness"),
    "operator": (
        "view_dashboard",
        "preview_run_plan",
        "prepare_provider_manual_run",
        "run_fixture_provider",
        "view_provider_readiness",
    ),
    "local_operator": (
        "view_dashboard",
        "edit_topic_config",
        "preview_run_plan",
        "prepare_provider_manual_run",
        "run_fixture_provider",
        "view_provider_readiness",
    ),
    "admin": AUTH_CAPABILITIES,
}

PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_VAR_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")
SECRET_KEY_PARTS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "credential",
    "authorization",
    "bearer",
    "private_key",
)
ALLOWED_SECRET_ENV_REFERENCE_FIELDS = {
    "clientIdEnvVar",
    "clientSecretEnvVar",
    "client_id_env_var",
    "client_secret_env_var",
}
SAFE_OAUTH_METADATA_FIELDS = {
    "authorization_url",
    "authorizationurl",
    "token_url",
    "tokenurl",
}
SECRET_VALUE_PREFIXES = ("sk-", "ya29.", "ghp_", "xoxb-", "xoxp-", "Bearer ")


def _snake_to_camel(raw: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "provider_id": "providerId",
        "display_name": "displayName",
        "authorization_url": "authorizationUrl",
        "token_url": "tokenUrl",
        "client_id_env_var": "clientIdEnvVar",
        "client_secret_env_var": "clientSecretEnvVar",
    }
    return {mapping.get(key, key): value for key, value in raw.items()}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = (
        value.splitlines()
        if isinstance(value, str) and "\n" in value
        else value.split(",")
        if isinstance(value, str)
        else value
    )
    if not isinstance(raw_items, list):
        raw_items = [raw_items]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _normalized_key(value: str) -> str:
    return value.replace("-", "_").replace(".", "_").lower()


def _is_env_reference_field(path: str) -> bool:
    field = path.rsplit(".", 1)[-1]
    return field in ALLOWED_SECRET_ENV_REFERENCE_FIELDS or _normalized_key(field).endswith("env_var")


def _looks_like_secret_key(path: str) -> bool:
    field = _normalized_key(path.rsplit(".", 1)[-1])
    if _is_env_reference_field(path) or field in SAFE_OAUTH_METADATA_FIELDS:
        return False
    return any(part in field for part in SECRET_KEY_PARTS)


def _looks_like_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if any(text.startswith(prefix) for prefix in SECRET_VALUE_PREFIXES):
        return True
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text):
        return True
    return False


def _safe_env_var_reference(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return None if _looks_like_secret_value(text) else text


def _secret_policy_scan(value: Any, *, prefix: str = "") -> list[tuple[str, str]]:
    rejected: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            text_key = str(key)
            path = f"{prefix}.{text_key}" if prefix else text_key
            if _looks_like_secret_key(path):
                rejected.append((path, "secret_like_field"))
            if _looks_like_secret_value(child):
                rejected.append((path, "secret_like_value"))
            rejected.extend(_secret_policy_scan(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rejected.extend(_secret_policy_scan(child, prefix=f"{prefix}[{index}]"))
    return rejected


def validate_secret_policy(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    rejected = _secret_policy_scan(payload or {})
    rejected_fields: list[str] = []
    reason_codes: list[str] = []
    for path, reason in rejected:
        if path not in rejected_fields:
            rejected_fields.append(path)
        if reason not in reason_codes:
            reason_codes.append(reason)
    warnings = ["OAuth metadata may reference env var names, but literal secrets/tokens are rejected."] if rejected_fields else []
    return {
        "valid": not rejected_fields,
        "rejectedFields": rejected_fields,
        "warnings": warnings,
        "reasonCodes": reason_codes,
    }


def _config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_oauth_provider_metadata(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _snake_to_camel(payload or {})
    provider_id = str(raw.get("providerId") or "oauth-provider").strip().lower()
    display_name = str(raw.get("displayName") or provider_id).strip() or provider_id
    metadata = {
        "providerId": provider_id,
        "displayName": display_name,
        "issuer": str(raw.get("issuer")).strip() if raw.get("issuer") else None,
        "authorizationUrl": str(raw.get("authorizationUrl")).strip() if raw.get("authorizationUrl") else None,
        "tokenUrl": str(raw.get("tokenUrl")).strip() if raw.get("tokenUrl") else None,
        "scopes": _string_list(raw.get("scopes") or ["openid", "profile", "email"]),
        "clientIdEnvVar": _safe_env_var_reference(raw.get("clientIdEnvVar")),
        "clientSecretEnvVar": _safe_env_var_reference(raw.get("clientSecretEnvVar")),
        "enabled": bool(raw.get("enabled", False)),
        "notes": _string_list(raw.get("notes")),
        "tokenStorageEnabled": False,
        "liveTokenExchangeEnabled": False,
    }
    return metadata


def validate_oauth_provider_metadata(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_payload = payload or {}
    metadata = normalize_oauth_provider_metadata(raw_payload)
    secret_policy = validate_secret_policy(raw_payload)
    errors: list[str] = []
    warnings: list[str] = []

    if not PROVIDER_ID_PATTERN.fullmatch(metadata["providerId"]):
        errors.append("provider_id must be stable and safe for file/URL usage.")
    for field in ("issuer", "authorizationUrl", "tokenUrl"):
        value = metadata.get(field)
        if value and not str(value).startswith("https://"):
            errors.append(f"{field} must use https:// metadata URLs.")
    for field in ("clientIdEnvVar", "clientSecretEnvVar"):
        value = metadata.get(field)
        if value and not ENV_VAR_PATTERN.fullmatch(str(value)):
            errors.append(f"{field} must be an uppercase environment variable name.")
    if secret_policy["rejectedFields"]:
        errors.append("literal OAuth secrets, tokens, API keys, or credentials are not accepted.")
    if metadata["enabled"]:
        warnings.append("enabled OAuth metadata remains metadata-only; no token exchange or token persistence is enabled.")
    warnings.append("OAuth client secrets must stay in environment variables outside persisted app config.")

    return {
        "providerId": metadata["providerId"],
        "validationStatus": "invalid" if errors else "valid",
        "oauthProviderMetadata": metadata,
        "secretPolicy": secret_policy,
        "warnings": list(dict.fromkeys([*warnings, *secret_policy.get("warnings", [])])),
        "errors": list(dict.fromkeys(errors)),
        "configHash": _config_hash(metadata),
    }


def _role_for_user(user: ProductUser | None, settings: AuthSettings) -> str:
    if not settings.auth_enabled:
        return "local_operator"
    if user is None:
        return "viewer"
    if user.is_admin:
        return "admin"
    return "viewer"


def operator_identity_for_user(user: ProductUser | None, *, settings: AuthSettings | None = None) -> dict[str, Any]:
    resolved_settings = settings or get_auth_settings()
    role = _role_for_user(user, resolved_settings)
    operator_id = user.user_id if user else "anonymous"
    display_name = user.display_name if user else "Unauthenticated Viewer"
    return {
        "operatorId": operator_id,
        "displayName": display_name,
        "role": role,
        "capabilities": list(ROLE_CAPABILITIES[role]),
    }


def has_capability(user: ProductUser | None, capability: str, *, settings: AuthSettings | None = None) -> bool:
    identity = operator_identity_for_user(user, settings=settings)
    return capability in set(identity["capabilities"])


def require_capability(user: ProductUser | None, capability: str, *, settings: AuthSettings | None = None) -> ProductUser:
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not has_capability(user, capability, settings=settings):
        raise HTTPException(status_code=403, detail=f"Required capability missing: {capability}")
    return user


def build_auth_foundation_status(
    *,
    user: ProductUser | None = None,
    settings: AuthSettings | None = None,
    oauth_providers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.services import (
        oauth_account_links,
        oauth_dry_run,
        oauth_live_evidence,
        oauth_live_readiness,
        oauth_smoke_results,
        oauth_token_exchange,
        provider_orchestration,
    )

    resolved_settings = settings or get_auth_settings()
    operator_identity = operator_identity_for_user(user, settings=resolved_settings)
    oauth_provider_results = [
        validate_oauth_provider_metadata(provider)["oauthProviderMetadata"] for provider in (oauth_providers or [])
    ]
    oauth_ready = bool(resolved_settings.auth_enabled and resolved_settings.oidc_issuer and resolved_settings.oidc_client_id)
    if oauth_ready:
        auth_mode = "oauth_ready"
    elif resolved_settings.auth_enabled:
        auth_mode = "disabled"
    else:
        auth_mode = "local_operator"
    warnings: list[str] = [
        "Token persistence is disabled; OAuth metadata may reference env vars only.",
        "Provider live execution remains CLI-only and requires explicit operator acceptance.",
    ]
    if not resolved_settings.auth_enabled:
        warnings.append("Local operator mode is intended for single-user local workflows.")
    account_link_status = oauth_account_links.build_account_link_health_summary()
    session_binding = oauth_account_links.build_session_binding_preview(operator_identity)
    session_policy = oauth_account_links.build_session_capability_policy(operator_identity)
    audit_health = operator_audit.validate_audit_log()
    live_readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=oauth_provider_results[0] if oauth_provider_results else None,
        operator_identity=operator_identity,
        audit_health=audit_health,
        account_link_health=account_link_status,
        session_policy=session_policy,
    )
    readiness_remediation = oauth_live_readiness.build_readiness_remediation(
        readiness=live_readiness,
        provider_metadata=oauth_provider_results[0] if oauth_provider_results else None,
    )
    preflight_package = oauth_live_readiness.build_preflight_package(
        provider_metadata=oauth_provider_results[0] if oauth_provider_results else None,
        operator_identity=operator_identity,
        audit_health=audit_health,
        account_link_health=account_link_status,
        session_policy=session_policy,
        readiness=live_readiness,
        remediation=readiness_remediation,
    )
    evidence_package = oauth_live_evidence.build_live_pilot_evidence_package(
        provider_metadata=oauth_provider_results[0] if oauth_provider_results else None,
        operator_identity=operator_identity,
        audit_health=audit_health,
        account_link_health=account_link_status,
        readiness=live_readiness,
        remediation=readiness_remediation,
        preflight=preflight_package,
    )
    smoke_result_summary = oauth_smoke_results.build_smoke_result_summary()
    provider_orchestration_matrix = provider_orchestration.build_provider_orchestration_safety_matrix(
        operator_identity=operator_identity,
        account_link_health=account_link_status,
        session_policy=session_policy,
        smoke_result_summary=smoke_result_summary,
    )
    provider_orchestration_plan = provider_orchestration.build_provider_orchestration_dry_run_plan(
        workflow_type="review_with_stub_provider",
        operator_identity=operator_identity,
        safety_matrix=provider_orchestration_matrix,
    )
    return {
        "authMode": auth_mode,
        "legacyAuthMode": resolved_settings.auth_mode,
        "oauthReady": oauth_ready,
        "tokenStorageEnabled": False,
        "providerLiveExecution": "cli_only_acceptance_gated",
        "operator": operator_identity,
        "oauthProviders": deepcopy(oauth_provider_results),
        "oauthDryRun": oauth_dry_run.build_oauth_dry_run_status(),
        "oauthTokenExchange": oauth_token_exchange.build_token_exchange_status(),
        "accountLinkStatus": account_link_status,
        "sessionBinding": session_binding,
        "sessionCapabilityPolicy": session_policy,
        "oauthFlowTimeline": oauth_account_links.build_oauth_flow_timeline(),
        "oauthLiveExchangeReadiness": live_readiness,
        "oauthReadinessRemediation": readiness_remediation,
        "oauthPreflightPackage": preflight_package,
        "oauthLiveExchangePilot": oauth_token_exchange.build_live_exchange_pilot_status(),
        "oauthLivePilotEvidencePackage": evidence_package,
        "oauthRealProviderSmoke": oauth_token_exchange.build_real_provider_smoke_status(),
        "oauthSmokeResultSummary": smoke_result_summary,
        "providerOrchestrationSafetyMatrix": provider_orchestration_matrix,
        "providerOrchestrationPlanSummary": provider_orchestration.build_provider_orchestration_plan_summary(provider_orchestration_plan),
        "providerOrchestrationExecutionSummary": provider_orchestration.build_provider_orchestration_execution_summary(operator_identity=operator_identity),
        "operatorAuditSummary": operator_audit.summarize_events(),
        "operatorAuditHealth": audit_health,
        "reasonCodes": ["token_storage_disabled", "provider_acceptance_gate_preserved"],
        "warnings": warnings,
    }

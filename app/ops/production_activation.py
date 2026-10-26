from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
import smtplib
from typing import Any

import httpx

from app.auth.config import clear_auth_settings_cache, get_auth_settings
from app.ops.runtime import build_deployment_status
from app.services import topic_notifications, topic_schedules
from app.services.providers import describe_provider_status

SECRET_ENV_KEYS = {
    "SESSION_SECRET",
    "OIDC_CLIENT_SECRET",
    "NOTIFICATION_EMAIL_SMTP_PASSWORD",
    "SMTP_PASSWORD",
    "OPENAI_API_KEY",
}


@dataclass
class ProductionActivationResult:
    ok: bool
    generated_at: str
    profile: str
    require_oidc: bool
    require_smtp: bool
    strict: bool
    fail_on_warnings: bool
    deployment_readiness: str
    checks: dict[str, Any]
    warnings: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_state(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        return "missing"
    if not value.strip():
        return "blank"
    return "set"


def _secret_safe_env_states() -> dict[str, str]:
    names = [
        "AUTH_MODE",
        "APP_BASE_URL",
        "WEB_APP_BASE_PATH",
        "XRML_ENVIRONMENT",
        "XRML_DEPLOYMENT_PROFILE",
        "XRML_SHARED_DEPLOYMENT",
        "XRML_STRICT_ENV_VALIDATION",
        "XRML_DATA_DIR",
        "SESSION_SECRET",
        "COOKIE_SECURE",
        "COOKIE_SAMESITE",
        "TRUST_PROXY_HEADERS",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_AUDIENCE",
        "OIDC_SCOPES",
        "NOTIFICATION_EMAIL_SMTP_HOST",
        "NOTIFICATION_EMAIL_SMTP_PORT",
        "NOTIFICATION_EMAIL_SMTP_USERNAME",
        "NOTIFICATION_EMAIL_SMTP_PASSWORD",
        "NOTIFICATION_EMAIL_SMTP_TLS",
        "NOTIFICATION_EMAIL_SMTP_SSL",
        "NOTIFICATION_EMAIL_FROM",
        "NOTIFICATION_WEBHOOK_ENABLED",
        "SCHEDULER_MODE",
        "TOPIC_SCHEDULER_MODE",
        "TOPIC_SCHEDULER_ENABLED",
        "TOPIC_SCHEDULER_TICK_SECONDS",
        "SCHEDULER_DISTRIBUTED",
        "SCHEDULER_DISTRIBUTED_MODE",
    ]
    return {name: _env_state(name) for name in names}


def _build_deployment_snapshot() -> tuple[dict[str, Any] | None, list[str]]:
    try:
        deployment = build_deployment_status(
            provider=describe_provider_status(),
            scheduler=topic_schedules.get_scheduler_runtime_status(),
            notification_delivery=topic_notifications.notification_delivery_health_summary(),
        )
        return deployment, []
    except Exception as exc:
        return None, [f"Deployment config could not be evaluated: {exc}"]


def _auth_check(deployment: dict[str, Any] | None) -> dict[str, Any]:
    if deployment:
        auth = deployment.get("auth") if isinstance(deployment.get("auth"), dict) else {}
        return {
            "mode": auth.get("mode") or os.getenv("AUTH_MODE", "disabled"),
            "auth_enabled": bool(auth.get("auth_enabled", False)),
            "oidc_configured": bool(auth.get("oidc_configured", False)),
            "oidc_readiness_status": auth.get("oidc_readiness_status") or "unknown",
            "oidc_missing_env": list(auth.get("oidc_missing_env") or []),
            "oidc_invalid_config": list(auth.get("oidc_invalid_config") or []),
            "session_secret_validation_status": auth.get("session_secret_validation_status") or "invalid",
            "session_secret_reason_code": auth.get("session_secret_reason_code"),
            "oidc_audience_configured": bool(auth.get("oidc_audience_configured", False)),
            "mcp_scope_status": auth.get("mcp_scope_status") or "unknown",
            "deployment_safe": bool(auth.get("deployment_safe", False)),
            "deployment_safety_status": auth.get("deployment_safety_status") or "unknown",
            "callback_url_configured": bool(auth.get("callback_url")),
            "mcp_auth_required": bool(auth.get("mcp_auth_required", False)),
            "mcp_chatgpt_ready": bool(auth.get("mcp_chatgpt_ready", False)),
            "warnings": list(auth.get("warnings") or []),
            "errors": list(auth.get("errors") or []),
        }
    return {
        "mode": os.getenv("AUTH_MODE", "disabled"),
        "auth_enabled": os.getenv("AUTH_MODE", "disabled").strip().lower() == "oidc",
        "oidc_configured": False,
        "oidc_readiness_status": "unknown",
        "oidc_missing_env": [],
        "oidc_invalid_config": [],
        "session_secret_validation_status": "invalid",
        "session_secret_reason_code": "session_secret_missing",
        "oidc_audience_configured": False,
        "mcp_scope_status": "unknown",
        "deployment_safe": False,
        "deployment_safety_status": "blocked",
        "callback_url_configured": bool(os.getenv("APP_BASE_URL")),
        "mcp_auth_required": os.getenv("AUTH_MODE", "disabled").strip().lower() == "oidc",
        "mcp_chatgpt_ready": False,
        "warnings": [],
        "errors": [],
    }


def _smtp_check() -> dict[str, Any]:
    summary = topic_notifications.notification_delivery_health_summary()
    return {
        "state": summary.get("smtp_state"),
        "readiness_state": summary.get("smtp_readiness_state"),
        "available": bool(summary.get("smtp_available", False)),
        "configured": bool(summary.get("smtp_configured", False)),
        "missing_env": list(summary.get("smtp_missing_env") or []),
        "invalid_config": list(summary.get("smtp_invalid_config") or []),
        "tls_mode": summary.get("smtp_tls_mode"),
        "unavailable_reason": summary.get("smtp_unavailable_reason"),
        "webhook_available": bool(summary.get("webhook_available", False)),
        "webhook_unavailable_reason": summary.get("webhook_unavailable_reason"),
    }


def _oidc_discovery_smoke() -> dict[str, Any]:
    clear_auth_settings_cache()
    try:
        settings = get_auth_settings()
    except Exception as exc:
        return {"ok": False, "status": "config_error", "error": str(exc)}
    if not settings.auth_enabled:
        return {"ok": False, "status": "disabled", "error": "AUTH_MODE is not oidc."}
    if not settings.oidc_metadata_url:
        return {"ok": False, "status": "missing_metadata_url", "error": "OIDC metadata URL is not configured."}
    try:
        response = httpx.get(settings.oidc_metadata_url, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {"ok": False, "status": "request_failed", "error": str(exc)}
    required = {"authorization_endpoint", "token_endpoint", "jwks_uri"}
    missing = sorted(required - set(payload.keys()))
    return {
        "ok": not missing,
        "status": "ok" if not missing else "invalid_metadata",
        "missing_fields": missing,
    }


def _smtp_connectivity_smoke() -> dict[str, Any]:
    config, state, error = topic_notifications._smtp_config_with_state()  # noqa: SLF001 - existing internal config validator.
    if state != topic_notifications.EMAIL_CHANNEL_AVAILABLE or not config:
        readiness = topic_notifications.smtp_config_diagnostics().get("readiness_state")
        return {"ok": False, "status": readiness or state, "channel_state": state, "error": error}
    try:
        if config["use_ssl"]:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(config["host"], config["port"], timeout=10)
        else:
            smtp = smtplib.SMTP(config["host"], config["port"], timeout=10)
        with smtp:
            smtp.ehlo()
            if config["use_tls"]:
                smtp.starttls()
                smtp.ehlo()
            if config.get("username"):
                smtp.login(str(config["username"]), str(config["password"]))
            code, _message = smtp.noop()
    except Exception as exc:
        return {"ok": False, "status": "connection_failed", "error": str(exc)}
    return {"ok": 200 <= int(code) < 400, "status": "ok", "smtp_code": int(code)}


def _redaction_check(payload: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    leaked = []
    for name in SECRET_ENV_KEYS:
        value = os.getenv(name)
        if value and value.strip().lower() not in {"set", "missing", "blank", "true", "false", "0", "1"} and value in rendered:
            leaked.append(name)
    return {"secret_values_exposed": leaked, "secret_values_redacted": not leaked}


def build_production_activation_result(
    *,
    require_oidc: bool = False,
    require_smtp: bool = False,
    strict: bool = False,
    fail_on_warnings: bool = False,
    check_oidc_discovery: bool = False,
    check_smtp_connectivity: bool = False,
    profile: str | None = None,
) -> ProductionActivationResult:
    clear_auth_settings_cache()
    deployment, deployment_errors = _build_deployment_snapshot()
    auth = _auth_check(deployment)
    smtp = _smtp_check()
    warnings: list[str] = []
    errors: list[str] = []

    deployment_readiness = "not_evaluated"
    if deployment:
        deployment_readiness = str(deployment.get("readiness") or "unknown")
        warnings.extend(str(item) for item in deployment.get("warnings") or [])
        errors.extend(str(item) for item in deployment.get("errors") or [])
    errors.extend(deployment_errors)

    if require_oidc:
        if auth.get("mode") != "oidc":
            errors.append("Production activation requires AUTH_MODE=oidc.")
        if not auth.get("oidc_configured"):
            errors.append("Production activation requires configured OIDC issuer/client/session env.")
        if not auth.get("deployment_safe"):
            errors.append("OIDC deployment safety check is not passing.")

    if require_smtp and smtp.get("state") != topic_notifications.EMAIL_CHANNEL_AVAILABLE:
        errors.append(f"SMTP activation requires available SMTP env; current state is {smtp.get('state')}.")

    checks: dict[str, Any] = {
        "env": _secret_safe_env_states(),
        "auth": auth,
        "smtp": smtp,
        "deployment": {
            "evaluated": deployment is not None,
            "readiness": deployment_readiness,
            "shared_deployment": bool(deployment.get("shared_deployment")) if deployment else None,
            "strict_env_validation": bool(deployment.get("strict_env_validation")) if deployment else strict,
            "storage_writable": bool(deployment.get("storage", {}).get("writable")) if deployment else None,
            "scheduler_runtime_mode": deployment.get("scheduler_runtime_mode") if deployment else None,
            "distributed_scheduler_supported": bool(deployment.get("distributed_scheduler_supported")) if deployment else False,
        },
    }

    if check_oidc_discovery:
        checks["oidc_discovery_smoke"] = _oidc_discovery_smoke()
        if not checks["oidc_discovery_smoke"].get("ok"):
            errors.append("OIDC discovery smoke check failed.")

    if check_smtp_connectivity:
        checks["smtp_connectivity_smoke"] = _smtp_connectivity_smoke()
        if not checks["smtp_connectivity_smoke"].get("ok"):
            errors.append("SMTP connectivity smoke check failed.")

    redaction_source = {"checks": checks, "warnings": warnings, "errors": errors}
    checks["secret_redaction"] = _redaction_check(redaction_source)
    if checks["secret_redaction"]["secret_values_exposed"]:
        errors.append("Secret values were detected in activation output.")

    if strict and deployment and deployment.get("readiness") == "not_ready":
        errors.append("Strict production activation requires deployment readiness not_ready to be resolved.")
    if fail_on_warnings and warnings:
        errors.append("Activation warnings are treated as failures by --fail-on-warnings.")

    deduped_warnings = list(dict.fromkeys(item for item in warnings if item))
    deduped_errors = list(dict.fromkeys(item for item in errors if item))

    return ProductionActivationResult(
        ok=not deduped_errors,
        generated_at=datetime.now(UTC).isoformat(),
        profile=profile or str(os.getenv("XRML_DEPLOYMENT_PROFILE") or "single-service"),
        require_oidc=require_oidc,
        require_smtp=require_smtp,
        strict=strict,
        fail_on_warnings=fail_on_warnings,
        deployment_readiness=deployment_readiness,
        checks=checks,
        warnings=deduped_warnings,
        errors=deduped_errors,
    )

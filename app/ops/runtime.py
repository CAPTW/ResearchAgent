from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any
import uuid
from urllib.parse import urlparse

from app.auth.config import get_auth_settings, validate_session_secret
from app.db.bootstrap import metadata_index_path, scheduler_db_path
from app.domain import constants
from app.repositories.files import iso_now_utc

SHARED_DEPLOYMENT_ENVIRONMENTS = {"prod", "production", "staging"}
SHARED_DEPLOYMENT_PREFIXES = ("prod-", "prod_", "production-", "production_", "staging-", "staging_")
SUPPORTED_DEPLOYMENT_PROFILES = {"single-service", "render-single-service"}
SUPPORTED_SCHEDULER_MODES = {"embedded", "external", "disabled"}
ENV_CONTRACT_VERSION = "deployment-env-v1"
def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_value_state(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        return "missing"
    if not value.strip():
        return "blank"
    return "set"


def _raw_env_value(name: str, default: str) -> str:
    return str(os.getenv(name) or default).strip().lower()


def _is_shared_environment_name(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in SHARED_DEPLOYMENT_ENVIRONMENTS or normalized.startswith(SHARED_DEPLOYMENT_PREFIXES)


def _is_absolute_http_url(value: str | None) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_https_url(value: str | None) -> bool:
    return urlparse(str(value or "").strip()).scheme == "https"


def _split_scope_string(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").replace(",", " ").split() if part.strip()]


def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
    }


def _observe_directory(path: Path) -> dict[str, Any]:
    """Observation-only directory status used by GET/system probes.

    Creates nothing: no mkdir, no probe file, no atomic-replace attempt. Write
    capability is deliberately reported as not probed rather than guessed, so a
    readiness response can never claim verified writability it did not prove.
    """
    exists = path.exists()
    return {
        "path": str(path),
        "existed_before_probe": exists,
        "exists": exists,
        "writable": None,
        "write_probed": False,
        "atomic_replace_supported": None,
        "error": None,
    }


def _probe_directory(path: Path, *, create: bool = True) -> dict[str, Any]:
    existed_before = path.exists()
    writable = False
    atomic_replace_supported = False
    error: str | None = None
    probe_token = f"{os.getpid()}-{uuid.uuid4().hex}"
    probe = path / f".xrml-storage-probe-{probe_token}"
    replacement = path / f".xrml-storage-probe-{probe_token}.replace"
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        probe.write_text("initial", encoding="utf-8")
        replacement.write_text("replacement", encoding="utf-8")
        os.replace(replacement, probe)
        atomic_replace_supported = probe.read_text(encoding="utf-8") == "replacement"
        probe.unlink(missing_ok=True)
        replacement.unlink(missing_ok=True)
        writable = True
    except OSError as exc:
        error = str(exc)
        try:
            probe.unlink(missing_ok=True)
            replacement.unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "path": str(path),
        "existed_before_probe": existed_before,
        "exists": path.exists(),
        "writable": writable,
        "atomic_replace_supported": atomic_replace_supported,
        "error": error,
    }


def _storage_layout(root: Path, runs_root: Path) -> dict[str, Any]:
    return {
        "data_root": str(root),
        "runs": str(runs_root),
        "topic_profiles": str(root / "topic_profiles"),
        "topic_schedules": str(root / "topic_schedules"),
        "scheduler_db": str(root / "scheduler.sqlite3"),
        "scheduler_queue_state": str(root / "scheduler.sqlite3"),
        "metadata_index": str(root / "metadata_index.sqlite3"),
        "auth_sessions": str(root / "auth" / "product_sessions.json"),
        "topic_notifications": str(root / "topic_notifications"),
        "topic_analytics": str(root / "topic_analytics"),
        "run_events": str(runs_root / "<run_id>" / "events.jsonl"),
        "paper_review_actions": str(runs_root / "<run_id>" / "paper_review_actions.json"),
        "paper_reviews": str(runs_root / "<run_id>" / "reviews"),
        "report_artifacts": str(runs_root / "<run_id>" / "reports"),
        "report_versions": str(runs_root / "<run_id>" / "reports" / "versions"),
        "export_artifacts": str(runs_root / "<run_id>" / "reports" / "exports.json"),
        "provider_artifacts": str(runs_root / "<run_id>" / "raw"),
        "validation_output_artifacts": str(constants.PROJECT_ROOT / "output" / "release-validation"),
        "playwright_truth_artifacts": str(constants.PROJECT_ROOT / "output" / "playwright" / "truth-pass"),
        "package_output_artifacts": str(constants.PROJECT_ROOT / "output" / "packages"),
    }


def _build_auth_status(*, shared_deployment: bool) -> tuple[dict[str, Any], list[str], list[str]]:
    settings = get_auth_settings()
    warnings: list[str] = []
    errors: list[str] = []
    callback_url = f"{settings.normalized_app_base_url}/auth/callback"
    issuer_valid = not settings.oidc_issuer or _is_absolute_http_url(settings.oidc_issuer)
    app_base_url_valid = _is_absolute_http_url(settings.normalized_app_base_url)
    session_secret_validation = validate_session_secret(settings.session_secret, required=settings.auth_enabled)
    session_secret_strong = session_secret_validation.status == "valid"
    oidc_missing_env = [
        name
        for name, value in (
            ("SESSION_SECRET", settings.session_secret),
            ("APP_BASE_URL", settings.app_base_url),
            ("OIDC_ISSUER", settings.oidc_issuer),
            ("OIDC_CLIENT_ID", settings.oidc_client_id),
            ("OIDC_CLIENT_SECRET", settings.oidc_client_secret),
        )
        if not str(value or "").strip()
    ]
    oidc_invalid_config: list[str] = []

    if settings.auth_enabled:
        if oidc_missing_env:
            errors.append("OIDC mode requires: " + ", ".join(oidc_missing_env))
        if not app_base_url_valid:
            errors.append("APP_BASE_URL must be an absolute http(s) URL.")
            oidc_invalid_config.append("APP_BASE_URL")
        if not issuer_valid:
            errors.append("OIDC_ISSUER must be an absolute http(s) URL.")
            oidc_invalid_config.append("OIDC_ISSUER")
        if shared_deployment and not _is_https_url(settings.normalized_app_base_url):
            errors.append("Shared deployment profiles must use an HTTPS APP_BASE_URL for OIDC redirects and cookies.")
            oidc_invalid_config.append("APP_BASE_URL")
        if shared_deployment and settings.oidc_issuer and not _is_https_url(settings.oidc_issuer):
            errors.append("Shared deployment profiles must use an HTTPS OIDC_ISSUER.")
            oidc_invalid_config.append("OIDC_ISSUER")
        if not settings.resolved_cookie_secure and _is_https_url(settings.normalized_app_base_url):
            message = "APP_BASE_URL is HTTPS but secure cookies are disabled."
            if shared_deployment:
                errors.append(message)
                oidc_invalid_config.append("COOKIE_SECURE")
            else:
                warnings.append(message)
        if not session_secret_validation.valid:
            errors.append("SESSION_SECRET does not satisfy the OIDC session security policy.")
            oidc_invalid_config.append("SESSION_SECRET")
        if not settings.oidc_audience:
            warnings.append("OIDC_AUDIENCE is missing; MCP bearer-token validation is not ChatGPT-ready.")
    else:
        warnings.append("AUTH_MODE=disabled is local/internal single-user mode, not a shared deployment mode.")
        if shared_deployment:
            errors.append("Shared deployment profiles must use AUTH_MODE=oidc.")

    oidc_configured = bool(
        settings.auth_enabled
        and app_base_url_valid
        and issuer_valid
        and session_secret_strong
        and settings.oidc_issuer
        and settings.oidc_client_id
        and settings.oidc_client_secret
    )
    if not settings.auth_enabled:
        oidc_readiness_status = "unsafe_production_disabled" if shared_deployment else "disabled_dev_mode"
    elif oidc_missing_env:
        oidc_readiness_status = "missing_env"
    elif oidc_invalid_config or errors:
        oidc_readiness_status = "invalid_config"
    else:
        oidc_readiness_status = "configured_not_verified"
    mcp_chatgpt_ready = bool(settings.auth_enabled and settings.oidc_audience and _is_https_url(settings.public_mcp_url))
    mcp_scope_status = (
        "local_disabled"
        if not settings.auth_enabled
        else "audience_configured"
        if settings.oidc_audience
        else "missing_audience"
    )
    if errors:
        safety_status = "unsafe"
    elif settings.auth_enabled and warnings:
        safety_status = "degraded"
    elif settings.auth_enabled:
        safety_status = "ready"
    else:
        safety_status = "local_internal_only"

    return (
        {
            "mode": settings.auth_mode,
            "auth_enabled": settings.auth_enabled,
            "shared_deployment": shared_deployment,
            "deployment_safe": not errors,
            "deployment_safety_status": safety_status,
            "oidc_readiness_status": oidc_readiness_status,
            "oidc_configured": oidc_configured,
            "oidc_missing_env": list(dict.fromkeys(oidc_missing_env)),
            "oidc_invalid_config": list(dict.fromkeys(oidc_invalid_config)),
            "oidc_audience_configured": bool(settings.oidc_audience),
            "oidc_scopes": _split_scope_string(settings.oidc_scopes),
            "issuer_configured": bool(settings.oidc_issuer),
            "issuer_valid": issuer_valid,
            "client_id_configured": bool(settings.oidc_client_id),
            "client_secret_configured": bool(settings.oidc_client_secret),
            "session_secret_configured": bool(str(settings.session_secret or "").strip()),
            "session_secret_strong": session_secret_strong,
            "session_secret_validation_status": session_secret_validation.status,
            "session_secret_reason_code": session_secret_validation.reason_code,
            "app_base_url_valid": app_base_url_valid,
            "app_base_url": settings.normalized_app_base_url,
            "callback_url": callback_url,
            "secure_cookies": settings.resolved_cookie_secure,
            "cookie_samesite": settings.cookie_samesite,
            "mcp_enabled": True,
            "mcp_auth_required": settings.auth_enabled,
            "mcp_chatgpt_ready": mcp_chatgpt_ready,
            "mcp_scope_status": mcp_scope_status,
            "warnings": list(dict.fromkeys(item for item in warnings if item)),
            "errors": list(dict.fromkeys(item for item in errors if item)),
        },
        warnings,
        errors,
    )


def _storage_status(*, observe_only: bool = False) -> dict[str, Any]:
    root = constants.STORAGE_ROOT.parent
    runs_root = constants.STORAGE_ROOT
    configured_data_dir = str(os.getenv("XRML_DATA_DIR") or "").strip()
    if observe_only:
        root_probe = _observe_directory(root)
        runs_probe = _observe_directory(runs_root)
    else:
        root_probe = _probe_directory(root)
        runs_probe = _probe_directory(runs_root)
    if observe_only:
        # Not probed: report unknown rather than guessing either way.
        writable = None
        atomic_replace_supported = None
    else:
        writable = bool(root_probe["writable"] and runs_probe["writable"])
        atomic_replace_supported = bool(root_probe["atomic_replace_supported"] and runs_probe["atomic_replace_supported"])
    errors = [str(item["error"]) for item in (root_probe, runs_probe) if item.get("error")]
    return {
        "data_root": str(root),
        "runs_root": str(runs_root),
        "artifact_storage_root": str(runs_root),
        "configured_data_dir": bool(configured_data_dir),
        "storage_mode": "xrml_data_dir" if configured_data_dir else "repo_local_default",
        "writable": writable,
        "atomic_replace_supported": atomic_replace_supported,
        "windows_path_safe": os.name != "nt" or len(str(root)) < 240,
        "layout": _storage_layout(root, runs_root),
        "checks": {
            "data_root": root_probe,
            "runs_root": runs_probe,
            "missing_directories_before_probe": [
                key
                for key, value in {
                    "data_root": root_probe,
                    "runs_root": runs_probe,
                }.items()
                if not value.get("existed_before_probe")
            ],
        },
        "error": "; ".join(errors) if errors else None,
    }


def _sqlite_storage_status(*, observe_only: bool = False) -> dict[str, Any]:
    # ensure_parent=False in observe mode so path resolution alone never mkdirs.
    scheduler_path = scheduler_db_path(ensure_parent=not observe_only)
    metadata_path = metadata_index_path(ensure_parent=not observe_only)
    parent = scheduler_path.parent
    parent_probe = _observe_directory(parent) if observe_only else _probe_directory(parent)
    return {
        "scheduler_db_path": str(scheduler_path),
        "metadata_index_path": str(metadata_path),
        "parent_writable": None if observe_only else bool(parent_probe["writable"]),
        "atomic_replace_supported": None if observe_only else bool(parent_probe["atomic_replace_supported"]),
        "paths": {
            "scheduler_db": _path_status(scheduler_path),
            "metadata_index": _path_status(metadata_path),
            "parent": parent_probe,
        },
        "error": parent_probe["error"],
    }


def _latest_release_validation_status() -> dict[str, Any]:
    validation_root = constants.PROJECT_ROOT / "output" / "release-validation"
    expected_warnings = [
        "Vite may emit the existing chunk-size warning during production build.",
        "SMTP is expected to report unavailable when SMTP env is not configured.",
        "Cross-node distributed scheduler support is intentionally gated unless a distributed backend is configured.",
    ]
    summaries = sorted(
        validation_root.glob("*/validation-summary.json"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    if not summaries:
        return {
            "available": False,
            "status": "unknown",
            "created_at": None,
            "log_dir": str(validation_root),
            "with_playwright": False,
            "playwright_screenshot_dir": str(constants.PROJECT_ROOT / "output" / "playwright" / "truth-pass"),
            "step_count": 0,
            "failed_step_count": 0,
            "latest_summary_path": None,
            "expected_warnings": expected_warnings,
        }

    summary_path = summaries[0]
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "status": "unreadable",
            "created_at": None,
            "log_dir": str(summary_path.parent),
            "with_playwright": False,
            "playwright_screenshot_dir": str(constants.PROJECT_ROOT / "output" / "playwright" / "truth-pass"),
            "step_count": 0,
            "failed_step_count": 0,
            "latest_summary_path": str(summary_path),
            "error": str(exc),
            "expected_warnings": expected_warnings,
        }

    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    failed_steps = [
        step
        for step in steps
        if isinstance(step, dict) and (step.get("status") != "passed" or int(step.get("exitCode") or 0) != 0)
    ]
    return {
        "available": True,
        "status": str(payload.get("status") or "unknown"),
        "created_at": payload.get("createdAt"),
        "log_dir": str(payload.get("logDir") or summary_path.parent),
        "with_playwright": bool(payload.get("withPlaywright")),
        "playwright_screenshot_dir": str(
            payload.get("screenshotDir") or constants.PROJECT_ROOT / "output" / "playwright" / "truth-pass"
        ),
        "step_count": len(steps),
        "failed_step_count": len(failed_steps),
        "latest_summary_path": str(summary_path),
        "expected_warnings": [str(item) for item in payload.get("expectedWarnings") or expected_warnings],
    }


def _frontend_status(settings: Any) -> dict[str, Any]:
    web_dist_root = constants.PROJECT_ROOT / "web" / "dist"
    return {
        "app_base_url": settings.normalized_app_base_url,
        "web_app_base_path": settings.normalized_web_app_base_path,
        "public_web_app_url": settings.public_web_app_url,
        "api_base_url": settings.normalized_app_base_url,
        "same_origin_api": True,
        "web_dist_present": web_dist_root.exists(),
        "serving_mode": "fastapi_static_spa" if web_dist_root.exists() else "dev_separate_vite_or_unbuilt_spa",
    }


def _env_contract_reference() -> dict[str, Any]:
    return {
        "version": ENV_CONTRACT_VERSION,
        "required_for_local": [
            "AUTH_MODE=disabled",
            "APP_BASE_URL=http://127.0.0.1:8000",
            "WEB_APP_BASE_PATH=/app",
            "XRML_ENVIRONMENT=local",
        ],
        "required_for_shared_oidc": [
            "AUTH_MODE=oidc",
            "SESSION_SECRET",
            "APP_BASE_URL",
            "WEB_APP_BASE_PATH",
            "OIDC_ISSUER",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "XRML_DATA_DIR",
            "XRML_STRICT_ENV_VALIDATION=true",
            "COOKIE_SECURE=true",
        ],
        "required_for_smtp": [
            "NOTIFICATION_EMAIL_SMTP_HOST",
            "NOTIFICATION_EMAIL_SMTP_PORT",
            "NOTIFICATION_EMAIL_FROM",
            "NOTIFICATION_EMAIL_SMTP_USERNAME and NOTIFICATION_EMAIL_SMTP_PASSWORD when SMTP auth is required",
            "NOTIFICATION_EMAIL_SMTP_TLS or NOTIFICATION_EMAIL_SMTP_SSL, not both",
        ],
        "webhook_variables": [
            "NOTIFICATION_WEBHOOK_ENABLED",
            "NOTIFICATION_WEBHOOK_ALLOWED_HOSTS",
            "NOTIFICATION_WEBHOOK_ALLOWED_PORTS",
        ],
        "scheduler_variables": [
            "TOPIC_SCHEDULER_MODE",
            "SCHEDULER_MODE",
            "TOPIC_SCHEDULER_ENABLED",
            "TOPIC_SCHEDULER_TICK_SECONDS",
            "SCHEDULER_DISTRIBUTED",
            "SCHEDULER_DISTRIBUTED_MODE",
        ],
        "storage_variables": [
            "XRML_DATA_DIR",
        ],
        "frontend_variables": [
            "APP_BASE_URL",
            "WEB_APP_BASE_PATH",
        ],
        "mcp_variables": [
            "OIDC_AUDIENCE",
            "OIDC_SCOPES",
        ],
        "optional_integrations": [
            "OIDC_AUDIENCE",
            "OIDC_SCOPES",
            "model provider credentials",
            "NOTIFICATION_EMAIL_SMTP_*",
            "NOTIFICATION_WEBHOOK_ENABLED",
            "ADMIN_USER_IDS",
            "ADMIN_USER_EMAILS",
        ],
        "unsafe_combinations": [
            "shared/prod/staging deployment with AUTH_MODE=disabled",
            "AUTH_MODE=oidc without OIDC issuer/client/session settings",
            "HTTPS APP_BASE_URL with insecure cookies",
            "shared deployment without XRML_DATA_DIR durable storage",
            "invalid scheduler lifecycle/runtime mode",
            "SCHEDULER_MODE=db_backed requires a writable shared SQLite scheduler DB path",
            "SCHEDULER_MODE=queue_backed requires a writable shared SQLite scheduler DB path",
            "SCHEDULER_DISTRIBUTED=true before cross-node backend support exists",
            "SMTP channel enabled without NOTIFICATION_EMAIL_SMTP_HOST and NOTIFICATION_EMAIL_FROM",
            "SMTP username without password or password without username",
            "SMTP TLS and SSL enabled together",
        ],
    }


def _config_health(
    *,
    settings: Any,
    auth_status: dict[str, Any],
    storage: dict[str, Any],
    sqlite_storage: dict[str, Any],
    scheduler: dict[str, Any],
    notification_delivery: dict[str, Any],
    provider: dict[str, Any],
    shared_environment: bool,
    release_validation: dict[str, Any],
) -> dict[str, Any]:
    scheduler_backend = scheduler.get("backend") if isinstance(scheduler.get("backend"), dict) else {}
    return {
        "contract_version": ENV_CONTRACT_VERSION,
        "auth": {
            "mode": settings.auth_mode,
            "deployment_safety_status": auth_status.get("deployment_safety_status"),
            "oidc_readiness_status": auth_status.get("oidc_readiness_status"),
            "oidc_configured": auth_status.get("oidc_configured"),
            "oidc_missing_env": auth_status.get("oidc_missing_env"),
            "oidc_invalid_config": auth_status.get("oidc_invalid_config"),
            "session_secret": _env_value_state("SESSION_SECRET"),
            "session_secret_validation_status": auth_status.get("session_secret_validation_status"),
            "session_secret_reason_code": auth_status.get("session_secret_reason_code"),
            "oidc_issuer": _env_value_state("OIDC_ISSUER"),
            "oidc_client_id": _env_value_state("OIDC_CLIENT_ID"),
            "oidc_client_secret": _env_value_state("OIDC_CLIENT_SECRET"),
            "oidc_audience": _env_value_state("OIDC_AUDIENCE"),
            "oidc_scopes": _env_value_state("OIDC_SCOPES"),
            "mcp_scope_status": auth_status.get("mcp_scope_status"),
        },
        "frontend": _frontend_status(settings),
        "storage": {
            "configured_data_dir": storage["configured_data_dir"],
            "storage_mode": storage["storage_mode"],
            "data_root": storage["data_root"],
            "artifact_storage_root": storage["artifact_storage_root"],
            "writable": storage["writable"],
            "atomic_replace_supported": storage.get("atomic_replace_supported"),
            "windows_path_safe": storage.get("windows_path_safe"),
            "layout": storage.get("layout"),
            "checks": storage.get("checks"),
            "shared_requires_xrml_data_dir": shared_environment,
        },
        "sqlite": {
            "scheduler_db_path": sqlite_storage["scheduler_db_path"],
            "metadata_index_path": sqlite_storage["metadata_index_path"],
            "parent_writable": sqlite_storage["parent_writable"],
            "atomic_replace_supported": sqlite_storage.get("atomic_replace_supported"),
        },
        "scheduler": {
            "lifecycle_mode": scheduler.get("configured_mode") or scheduler.get("mode"),
            "lifecycle_mode_valid": scheduler.get("configured_mode_valid", True),
            "runtime_mode": scheduler.get("runtime_mode"),
            "runtime_mode_valid": scheduler_backend.get("configured_runtime_mode_valid", scheduler.get("configured_runtime_mode_valid", True)),
            "storage_backend": scheduler.get("storage_backend"),
            "queue_backend_status": scheduler.get("queue_backend_status"),
            "distributed_requested": scheduler.get("distributed_requested", False),
            "distributed_supported": scheduler.get("distributed_ready", False),
            "backend_available": scheduler_backend.get("available", True),
            "backend_read_only": scheduler_backend.get("read_only", False),
        },
        "notification_delivery": {
            "smtp_state": notification_delivery.get("smtp_state"),
            "smtp_readiness_state": notification_delivery.get("smtp_readiness_state"),
            "smtp_available": notification_delivery.get("smtp_available"),
            "smtp_configured": notification_delivery.get("smtp_configured"),
            "smtp_tls_mode": notification_delivery.get("smtp_tls_mode"),
            "webhook_available": notification_delivery.get("webhook_available"),
        },
        "provider": {
            "provider": provider.get("provider"),
            "provider_mode": provider.get("provider_mode"),
            "configured": provider.get("configured"),
            "live_call_enabled": provider.get("live_call_enabled"),
            "canonicalStatus": provider.get("canonicalStatus"),
            "routingLabel": provider.get("routingLabel"),
            "researchOutputAllowed": provider.get("researchOutputAllowed"),
            "reportGenerationAllowed": provider.get("reportGenerationAllowed"),
            "orchestrationAllowed": provider.get("orchestrationAllowed"),
        },
        "release_validation": {
            "available": release_validation.get("available"),
            "status": release_validation.get("status"),
            "created_at": release_validation.get("created_at"),
            "with_playwright": release_validation.get("with_playwright"),
            "step_count": release_validation.get("step_count"),
            "failed_step_count": release_validation.get("failed_step_count"),
            "expected_warnings": release_validation.get("expected_warnings"),
        },
        "contract": _env_contract_reference(),
    }


def build_deployment_status(
    *,
    provider: dict[str, Any],
    scheduler: dict[str, Any],
    notification_delivery: dict[str, Any],
    observe_only: bool = False,
) -> dict[str, Any]:
    """Build deployment status.

    ``observe_only=True`` is the contract for GET/system probes: storage is
    observed without creating directories or temporary write-probe files, and
    write capability is reported as not probed instead of being asserted.
    """
    settings = get_auth_settings()
    storage = _storage_status(observe_only=observe_only)
    sqlite_storage = _sqlite_storage_status(observe_only=observe_only)
    release_validation = _latest_release_validation_status()
    environment = _raw_env_value("XRML_ENVIRONMENT", "local") or "local"
    deployment_profile = _raw_env_value("XRML_DEPLOYMENT_PROFILE", "single-service") or "single-service"
    raw_scheduler_mode = _raw_env_value(
        "TOPIC_SCHEDULER_MODE",
        str(scheduler.get("configured_mode") or scheduler.get("mode") or "embedded"),
    )
    scheduler_mode = str(scheduler.get("configured_mode") or scheduler.get("mode") or "embedded")
    scheduler_mode_valid = raw_scheduler_mode in SUPPORTED_SCHEDULER_MODES
    scheduler_backend = scheduler.get("backend") if isinstance(scheduler.get("backend"), dict) else {}
    raw_scheduler_runtime_mode = str(
        scheduler_backend.get("configured_runtime_mode_raw")
        or scheduler.get("configured_runtime_mode_raw")
        or os.getenv("SCHEDULER_MODE")
        or os.getenv("TOPIC_SCHEDULER_RUNTIME_MODE")
        or os.getenv("TOPIC_SCHEDULER_BACKEND")
        or scheduler.get("runtime_mode")
        or "single_node_file_backed"
    )
    scheduler_runtime_mode_valid = bool(
        scheduler_backend.get("configured_runtime_mode_valid", scheduler.get("configured_runtime_mode_valid", True))
    )
    warnings: list[str] = []
    errors: list[str] = []
    shared_environment = _env_flag("XRML_SHARED_DEPLOYMENT", False) or _is_shared_environment_name(environment)
    auth_status, auth_warnings, auth_errors = _build_auth_status(shared_deployment=shared_environment)
    warnings.extend(auth_warnings)
    errors.extend(auth_errors)

    # observe_only leaves "writable" as None (not probed): absence of proof is
    # not proof of failure, so no error is fabricated in that mode.
    if storage["writable"] is False:
        errors.append("Runtime storage is not writable; runs, sessions, notifications, and schedules cannot persist.")
    if storage.get("atomic_replace_supported") is False:
        errors.append("Runtime storage does not support atomic replace; JSON artifact writes are unsafe.")
    if not storage.get("windows_path_safe", True):
        warnings.append("Runtime storage path is long on Windows; use a shorter XRML_DATA_DIR if file operations fail.")
    if sqlite_storage["parent_writable"] is False:
        errors.append("SQLite storage root is not writable; metadata and scheduler state cannot persist.")
    if sqlite_storage.get("atomic_replace_supported") is False:
        errors.append("SQLite storage root does not support atomic replace; scheduler and metadata portability is unsafe.")
    if shared_environment and not storage["configured_data_dir"]:
        errors.append("Shared deployment profiles must set XRML_DATA_DIR to a durable storage path.")

    if deployment_profile not in SUPPORTED_DEPLOYMENT_PROFILES:
        message = f"Unknown deployment profile '{deployment_profile}'; supported baseline is single-service."
        if shared_environment:
            errors.append(message)
        else:
            warnings.append(message)

    if not scheduler_mode_valid:
        message = (
            f"Invalid TOPIC_SCHEDULER_MODE '{raw_scheduler_mode}'; "
            "supported values are embedded, external, or disabled."
        )
        if shared_environment:
            errors.append(message)
        else:
            warnings.append(message)
    if scheduler_mode == "external" and scheduler.get("requires_external_worker") and not scheduler.get("running"):
        message = "Scheduler is external mode and requires a running worker process."
        if shared_environment:
            errors.append(message)
        else:
            warnings.append(message)
    if scheduler_mode == "embedded":
        warnings.append("Scheduler is embedded single-node mode; it is not distributed multi-node scheduling.")
    scheduler_distributed_requested = bool(scheduler.get("distributed_requested", False))
    scheduler_distributed_support_status = str(scheduler.get("distributed_support_status") or "gated")
    if scheduler_distributed_requested and not scheduler.get("multi_node_dispatch_safe", False):
        message = "distributed multi-node scheduler is not implemented for this runtime."
        if shared_environment:
            errors.append(message)
        else:
            warnings.append(message)
    if not scheduler_runtime_mode_valid:
        source = str(
            scheduler_backend.get("configured_runtime_mode_source")
            or scheduler.get("configured_runtime_mode_source")
            or "SCHEDULER_MODE"
        )
        message = (
            f"Invalid {source} '{raw_scheduler_runtime_mode}'; supported scheduler runtime modes are "
            "single_node_file_backed, db_backed, or queue_backed."
        )
        if shared_environment:
            errors.append(message)
        else:
            warnings.append(message)
    if scheduler_backend and not scheduler_backend.get("available", True):
        reason = scheduler_backend.get("unavailable_reason") or "Scheduler runtime backend is unavailable."
        message = str(reason)
        if shared_environment:
            errors.append(message)
        else:
            warnings.append(message)

    if provider.get("provider_mode") in {"fallback_only", "unknown", "disabled"} or not bool(provider.get("researchOutputAllowed", False)):
        warnings.extend(str(item) for item in provider.get("warnings") or [])

    if not notification_delivery.get("smtp_available", False):
        reason = notification_delivery.get("smtp_unavailable_reason") or "SMTP is not configured."
        warnings.append(str(reason))
    if not notification_delivery.get("webhook_available", False):
        reason = notification_delivery.get("webhook_unavailable_reason") or "Webhook delivery is unavailable."
        warnings.append(str(reason))

    readiness = "ready"
    if errors:
        readiness = "not_ready"
    elif warnings:
        readiness = "degraded"

    return {
        "generated_at": iso_now_utc(),
        "environment": environment,
        "deployment_profile": deployment_profile,
        "shared_deployment": shared_environment,
        "readiness": readiness,
        "app_base_url": settings.normalized_app_base_url,
        "web_app_base_path": settings.normalized_web_app_base_path,
        "auth": auth_status,
        "storage": storage,
        "sqlite": sqlite_storage,
        "frontend": _frontend_status(settings),
        "config_health": _config_health(
            settings=settings,
            auth_status=auth_status,
            storage=storage,
            sqlite_storage=sqlite_storage,
            scheduler=scheduler,
            notification_delivery=notification_delivery,
            provider=provider,
            shared_environment=shared_environment,
            release_validation=release_validation,
        ),
        "env": {
            "AUTH_MODE": settings.auth_mode,
            "XRML_SHARED_DEPLOYMENT": str(os.getenv("XRML_SHARED_DEPLOYMENT") or "").strip().lower(),
            "XRML_PROVIDER": (os.getenv("XRML_PROVIDER") or "openai").strip().lower() or "openai",
            "TOPIC_SCHEDULER_MODE": scheduler_mode,
            "TOPIC_SCHEDULER_MODE_RAW": raw_scheduler_mode,
            "TOPIC_SCHEDULER_MODE_VALID": scheduler_mode_valid,
            "SCHEDULER_MODE": str(
                scheduler_backend.get("configured_runtime_mode") or scheduler.get("configured_runtime_mode") or scheduler.get("runtime_mode")
            ),
            "SCHEDULER_MODE_RAW": raw_scheduler_runtime_mode,
            "SCHEDULER_MODE_VALID": scheduler_runtime_mode_valid,
            "SCHEDULER_MODE_SOURCE": str(
                scheduler_backend.get("configured_runtime_mode_source")
                or scheduler.get("configured_runtime_mode_source")
                or "default"
            ),
            "TOPIC_SCHEDULER_ENABLED": str(os.getenv("TOPIC_SCHEDULER_ENABLED") or "true").strip().lower(),
            "NOTIFICATION_EMAIL_SMTP_HOST": _env_value_state("NOTIFICATION_EMAIL_SMTP_HOST"),
            "NOTIFICATION_EMAIL_FROM": _env_value_state("NOTIFICATION_EMAIL_FROM"),
            "NOTIFICATION_WEBHOOK_ENABLED": str(os.getenv("NOTIFICATION_WEBHOOK_ENABLED") or "true").strip().lower(),
            "LOG_FORMAT": (os.getenv("LOG_FORMAT") or "plain").strip().lower() or "plain",
        },
        "scheduler_architecture": str(scheduler.get("runtime_mode") or "single_node_file_backed"),
        "scheduler_runtime_mode": str(scheduler.get("runtime_mode") or "single_node_file_backed"),
        "scheduler_runtime_mode_status": str(scheduler.get("runtime_mode_status") or "degraded"),
        "scheduler_runtime_mode_valid": scheduler_runtime_mode_valid,
        "scheduler_storage_backend": str(scheduler.get("storage_backend") or "file_json"),
        "scheduler_distributed_requested": scheduler_distributed_requested,
        "scheduler_distributed_support_status": scheduler_distributed_support_status,
        "scheduler_architecture_note": str(
            scheduler.get("architecture_note")
            or "Single-node file-backed scheduler; distributed DB/queue-backed dispatch is not implemented."
        ),
        "single_service_deployable": True,
        "distributed_scheduler_supported": bool(scheduler.get("distributed_ready", False)),
        "strict_env_validation": _env_flag("XRML_STRICT_ENV_VALIDATION", False),
        "release_validation": release_validation,
        "warnings": list(dict.fromkeys(item for item in warnings if item)),
        "errors": list(dict.fromkeys(item for item in errors if item)),
    }


def enforce_strict_environment_if_requested(deployment: dict[str, Any]) -> None:
    if not deployment.get("strict_env_validation"):
        return
    errors = list(deployment.get("errors") or [])
    if errors:
        raise RuntimeError("Strict environment validation failed: " + "; ".join(str(item) for item in errors))

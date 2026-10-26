from __future__ import annotations

import os
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from app.auth.config import AuthSettings, get_auth_settings
from app.repositories.files import iso_now_utc
from app.services import oauth_token_exchange

LIVE_OAUTH_SMOKE_ENV_VAR = "ALLOW_LIVE_OAUTH_SMOKE"

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:ACCESS_TOKEN|REFRESH_TOKEN|ID_TOKEN|CLIENT_SECRET|API_KEY|TOKEN|SECRET)\s*[:=]\s*)[^\s]+"),
    re.compile(r"ya29\.[A-Za-z0-9_\-.]+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _redact_text(value: str) -> tuple[str, bool]:
    text = str(value or "")
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if "authorization" in pattern.pattern.lower():
            redacted = pattern.sub(r"\1<redacted>", redacted)
        elif pattern.pattern.startswith("(?i)\\bBearer"):
            redacted = pattern.sub("Bearer <redacted>", redacted)
        elif "ACCESS_TOKEN" in pattern.pattern or "TOKEN" in pattern.pattern or "SECRET" in pattern.pattern:
            redacted = pattern.sub(r"\1<redacted>", redacted)
        else:
            redacted = pattern.sub("<redacted>", redacted)
    return redacted, redacted != text


def _safe_reason(value: str) -> str:
    return _redact_text(value)[0]


def _valid_url(value: str | None, *, require_https: bool = False) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return False
    if require_https and parsed.scheme != "https":
        return False
    return parsed.scheme in {"http", "https"}


def _public_https_ready(settings: AuthSettings) -> bool:
    parsed = urlparse(settings.normalized_app_base_url)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}


def _idp_configured(settings: AuthSettings) -> bool:
    return (
        settings.auth_enabled
        and _valid_url(settings.oidc_issuer, require_https=True)
        and bool(str(settings.oidc_client_id or "").strip())
        and bool(str(settings.oidc_client_secret or "").strip())
        and bool(str(settings.oidc_audience or "").strip())
    )


def _mcp_route_summary() -> tuple[list[str], list[str]]:
    from app.api.mcp import MCP_TOOLS
    from app.auth.security import RESEARCH_READ_SCOPE, RESEARCH_WRITE_SCOPE

    read_only_routes = sorted(
        name
        for name, config in MCP_TOOLS.items()
        if config.get("required_scope") == RESEARCH_READ_SCOPE and bool(config.get("read_only_hint"))
    )
    write_routes = sorted(name for name, config in MCP_TOOLS.items() if config.get("required_scope") == RESEARCH_WRITE_SCOPE)
    return read_only_routes, write_routes


def _mock_exchange_summary() -> tuple[bool, list[str], list[str]]:
    result = oauth_token_exchange.validate_mock_token_exchange(
        {
            "providerId": "codex-oauth",
            "authorizationCodePresent": True,
            "codeVerifierPresent": True,
            "stateStatus": "valid",
            "mockTokenResponse": {
                "access_token": "ya29.mock-access-token",
                "refresh_token": "refresh-token-secret",
                "id_token": "id-token-secret",
                "scope": "research.read",
                "sub": "operator-subject",
                "expires_in": 3600,
            },
        }
    )
    accepted = bool(result.get("accepted"))
    reason_codes = list(result.get("reasonCodes") or [])
    warnings = list(result.get("warnings") or [])
    return accepted, reason_codes, warnings


def _base_result(
    *,
    settings: AuthSettings,
    proof_level: str,
    status: str,
    metadata_valid: bool,
    reason: str,
    reason_codes: list[str] | None = None,
    warnings: list[str] | None = None,
    metadata: bool = False,
    mock_exchange_requested: bool = False,
    mock_exchange_passed: bool = False,
    live_smoke_requested: bool = False,
    live_smoke_allowed: bool = False,
) -> dict[str, Any]:
    read_only_routes, write_routes = _mcp_route_summary()
    return {
        "proofLevel": proof_level,
        "status": status,
        "authMode": settings.auth_mode,
        "issuer": settings.oidc_issuer if _valid_url(settings.oidc_issuer, require_https=True) else None,
        "resourceServerUrl": settings.public_mcp_protected_resource_metadata_url if _valid_url(settings.public_mcp_protected_resource_metadata_url) else None,
        "mcpBaseUrl": settings.public_mcp_url if _valid_url(settings.public_mcp_url) else None,
        "publicHttpsReady": _public_https_ready(settings),
        "idpConfigured": _idp_configured(settings),
        "metadataValid": bool(metadata_valid),
        "metadataRequested": bool(metadata),
        "mockExchangeRequested": bool(mock_exchange_requested),
        "mockExchangePassed": bool(mock_exchange_passed),
        "liveSmokeRequested": bool(live_smoke_requested),
        "liveSmokeAllowed": bool(live_smoke_allowed),
        "readOnlyRoutesChecked": read_only_routes,
        "protectedWriteRoutesPresent": write_routes,
        "writeRoutesExposed": False,
        "tokenStorageEnabled": False,
        "secretStorageDetected": False,
        "dashboardTriggerAllowed": False,
        "reason": _safe_reason(reason),
        "reasonCodes": _unique(reason_codes or []),
        "warnings": _unique([_safe_reason(item) for item in (warnings or [])]),
        "createdAt": iso_now_utc(),
    }


def build_oauth_readonly_proof_result(
    *,
    settings: AuthSettings | None = None,
    metadata: bool = False,
    mock_exchange: bool = False,
    allow_live_oauth_smoke: bool = False,
    env: Mapping[str, str] | None = None,
    connector_probe_passed: bool | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_auth_settings()
    base_url_valid = _valid_url(resolved_settings.normalized_app_base_url)
    issuer_present = bool(str(resolved_settings.oidc_issuer or "").strip())
    issuer_valid = not issuer_present or _valid_url(resolved_settings.oidc_issuer, require_https=True)
    resource_valid = _valid_url(resolved_settings.public_mcp_protected_resource_metadata_url)
    mcp_valid = _valid_url(resolved_settings.public_mcp_url)
    read_only_routes, _write_routes = _mcp_route_summary()
    metadata_valid = base_url_valid and issuer_valid and resource_valid and mcp_valid and bool(read_only_routes)
    live_requested = bool(allow_live_oauth_smoke)
    env_allowed = str((env or os.environ).get(LIVE_OAUTH_SMOKE_ENV_VAR, "")).strip() == "1"

    if not metadata_valid:
        reason_codes = []
        if not base_url_valid:
            reason_codes.append("invalid_app_base_url")
        if not issuer_valid:
            reason_codes.append("invalid_oidc_issuer")
        if not resource_valid:
            reason_codes.append("invalid_resource_metadata_url")
        if not mcp_valid:
            reason_codes.append("invalid_mcp_url")
        if not read_only_routes:
            reason_codes.append("missing_read_only_mcp_routes")
        return _base_result(
            settings=resolved_settings,
            proof_level="failed",
            status="failed",
            metadata_valid=False,
            reason="OAuth/MCP local metadata validation failed.",
            reason_codes=reason_codes,
            warnings=["Metadata validation failed without reading or storing secret values."],
            metadata=metadata,
            mock_exchange_requested=mock_exchange,
            live_smoke_requested=live_requested,
        )

    if live_requested:
        if not env_allowed:
            return _base_result(
                settings=resolved_settings,
                proof_level="live_smoke_blocked",
                status="blocked",
                metadata_valid=True,
                reason=f"{LIVE_OAUTH_SMOKE_ENV_VAR}=1 is required for live OAuth connector smoke.",
                reason_codes=["live_oauth_smoke_env_required", "dashboard_live_oauth_forbidden"],
                warnings=["Live read-only OAuth connector smoke was blocked before any exchange or connector call."],
                metadata=metadata,
                mock_exchange_requested=mock_exchange,
                live_smoke_requested=True,
                live_smoke_allowed=False,
            )
        if not _public_https_ready(resolved_settings):
            return _base_result(
                settings=resolved_settings,
                proof_level="public_https_missing",
                status="blocked",
                metadata_valid=True,
                reason="Public HTTPS APP_BASE_URL is required for live ChatGPT/Codex connector smoke.",
                reason_codes=["public_https_required", "live_oauth_smoke_not_attempted"],
                metadata=metadata,
                mock_exchange_requested=mock_exchange,
                live_smoke_requested=True,
                live_smoke_allowed=False,
            )
        if not _idp_configured(resolved_settings):
            return _base_result(
                settings=resolved_settings,
                proof_level="idp_missing",
                status="blocked",
                metadata_valid=True,
                reason="OIDC issuer, client id, client secret, and audience must be configured for live connector smoke.",
                reason_codes=["idp_configuration_required", "live_oauth_smoke_not_attempted"],
                metadata=metadata,
                mock_exchange_requested=mock_exchange,
                live_smoke_requested=True,
                live_smoke_allowed=False,
            )
        if connector_probe_passed is True:
            return _base_result(
                settings=resolved_settings,
                proof_level="read_only_connector_smoke_passed",
                status="passed",
                metadata_valid=True,
                reason="Explicit read-only connector smoke probe passed.",
                reason_codes=["read_only_connector_smoke_passed", "token_storage_disabled", "dashboard_live_oauth_forbidden"],
                metadata=metadata,
                mock_exchange_requested=mock_exchange,
                live_smoke_requested=True,
                live_smoke_allowed=True,
            )
        if connector_probe_passed is False:
            return _base_result(
                settings=resolved_settings,
                proof_level="failed",
                status="failed",
                metadata_valid=True,
                reason="Explicit read-only connector smoke probe failed.",
                reason_codes=["read_only_connector_smoke_failed"],
                metadata=metadata,
                mock_exchange_requested=mock_exchange,
                live_smoke_requested=True,
                live_smoke_allowed=True,
            )
        return _base_result(
            settings=resolved_settings,
            proof_level="live_smoke_blocked",
            status="blocked",
            metadata_valid=True,
            reason="Live prerequisites are present, but no read-only connector probe was supplied.",
            reason_codes=["read_only_connector_probe_not_configured", "live_oauth_smoke_not_attempted"],
            warnings=["Do not label public HTTPS and IdP readiness as live connector proof without an actual read-only connector smoke."],
            metadata=metadata,
            mock_exchange_requested=mock_exchange,
            live_smoke_requested=True,
            live_smoke_allowed=False,
        )

    if mock_exchange:
        passed, reason_codes, warnings = _mock_exchange_summary()
        return _base_result(
            settings=resolved_settings,
            proof_level="mock_exchange_passed" if passed else "failed",
            status="passed" if passed else "failed",
            metadata_valid=True,
            reason="Mock exchange validated locally; this is not live connector proof." if passed else "Mock exchange validation failed.",
            reason_codes=[
                "mock_exchange_not_live_connector_proof",
                "token_storage_disabled",
                *reason_codes,
            ],
            warnings=warnings,
            metadata=metadata,
            mock_exchange_requested=True,
            mock_exchange_passed=passed,
            live_smoke_requested=False,
        )

    if metadata:
        if resolved_settings.auth_enabled and not _idp_configured(resolved_settings):
            return _base_result(
                settings=resolved_settings,
                proof_level="config_missing",
                status="blocked",
                metadata_valid=True,
                reason="OIDC mode is enabled, but IdP prerequisites are incomplete.",
                reason_codes=["idp_configuration_required", "local_metadata_valid", "live_oauth_smoke_not_attempted"],
                warnings=["Local MCP metadata is valid, but live ChatGPT/Codex connector proof still needs OIDC issuer, client id, client secret, and audience."],
                metadata=True,
            )
        return _base_result(
            settings=resolved_settings,
            proof_level="local_metadata_valid",
            status="passed",
            metadata_valid=True,
            reason="Local OAuth/MCP metadata and read-only route inventory are valid; this is not live connector proof.",
            reason_codes=["local_metadata_valid", "mock_exchange_not_run", "live_oauth_smoke_not_attempted"],
            metadata=True,
        )

    return _base_result(
        settings=resolved_settings,
        proof_level="disabled_default" if not resolved_settings.auth_enabled else "local_metadata_valid",
        status="safe" if not resolved_settings.auth_enabled else "passed",
        metadata_valid=True,
        reason=(
            "AUTH_MODE=disabled is the default local-only mode; live OAuth connector smoke is disabled."
            if not resolved_settings.auth_enabled
            else "OIDC metadata is locally configured; live connector smoke was not requested."
        ),
        reason_codes=["oauth_disabled_default", "live_oauth_smoke_not_attempted", "token_storage_disabled"]
        if not resolved_settings.auth_enabled
        else ["local_metadata_valid", "live_oauth_smoke_not_attempted", "token_storage_disabled"],
    )

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SESSION_SECRET_MIN_BYTES = 32
DISALLOWED_SESSION_SECRETS = frozenset(
    {
        "change-me",
        "change-me-for-shared-deployments",
        "changeme",
        "default",
        "dev-secret",
        "development-secret",
        "secret",
        "test-secret",
        "test-session-secret",
        "xrml-local-internal-session",
    }
)


@dataclass(frozen=True)
class SessionSecretValidation:
    status: Literal["not_required", "valid", "invalid"]
    reason_code: Literal[
        "session_secret_missing",
        "session_secret_too_short",
        "session_secret_disallowed",
    ] | None

    @property
    def valid(self) -> bool:
        return self.status in {"not_required", "valid"}


class AuthConfigurationError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def validate_session_secret(value: str | None, *, required: bool) -> SessionSecretValidation:
    if not required:
        return SessionSecretValidation(status="not_required", reason_code=None)
    if value is None or not value.strip():
        return SessionSecretValidation(status="invalid", reason_code="session_secret_missing")
    if value.strip().casefold() in DISALLOWED_SESSION_SECRETS:
        return SessionSecretValidation(status="invalid", reason_code="session_secret_disallowed")
    if len(value.encode("utf-8")) < SESSION_SECRET_MIN_BYTES:
        return SessionSecretValidation(status="invalid", reason_code="session_secret_too_short")
    return SessionSecretValidation(status="valid", reason_code=None)


def require_valid_session_secret(settings: "AuthSettings") -> str:
    validation = validate_session_secret(settings.session_secret, required=True)
    if not validation.valid:
        messages = {
            "session_secret_missing": "OIDC session configuration is missing a session secret.",
            "session_secret_too_short": "OIDC session configuration requires a stronger session secret.",
            "session_secret_disallowed": "OIDC session configuration uses a disallowed session secret.",
        }
        reason_code = validation.reason_code or "session_secret_invalid"
        raise AuthConfigurationError(reason_code, messages.get(reason_code, "OIDC session configuration is invalid."))
    assert settings.session_secret is not None
    return settings.session_secret


def _split_csv_env(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


class AuthSettings(BaseModel):
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    session_secret: str | None = None
    app_base_url: str = "http://127.0.0.1:8000"
    web_app_base_path: str = "/app"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_audience: str | None = None
    oidc_scopes: str = "openid profile email"
    session_cookie_name: str = "xrml_session"
    auth_state_cookie_name: str = "xrml_auth_state"
    cookie_secure: bool | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    trust_proxy_headers: bool = False
    allowed_origins: tuple[str, ...] = ()
    product_session_ttl_seconds: int = Field(default=60 * 60 * 24 * 7, ge=300)
    auth_state_ttl_seconds: int = Field(default=60 * 10, ge=60)
    mcp_protocol_version: str = "2025-03-26"
    admin_user_ids: tuple[str, ...] = ()
    admin_user_emails: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_oidc_requirements(self) -> "AuthSettings":
        if self.cookie_samesite == "none" and not self.resolved_cookie_secure:
            raise ValueError("COOKIE_SAMESITE=none requires secure cookies.")
        return self

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode == "oidc"

    @property
    def normalized_app_base_url(self) -> str:
        return self.app_base_url.rstrip("/")

    @property
    def resolved_cookie_secure(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.normalized_app_base_url.startswith("https://")

    @property
    def secure_cookies(self) -> bool:
        return self.resolved_cookie_secure

    @property
    def normalized_web_app_base_path(self) -> str:
        value = (self.web_app_base_path or "/app").strip() or "/app"
        if not value.startswith("/"):
            value = f"/{value}"
        return value.rstrip("/") or "/"

    @property
    def normalized_allowed_origins(self) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in self.allowed_origins:
            cleaned = value.strip().rstrip("/")
            if cleaned:
                normalized.append(cleaned)
        return tuple(dict.fromkeys(normalized))

    @property
    def public_web_app_url(self) -> str:
        return f"{self.normalized_app_base_url}{self.normalized_web_app_base_path}"

    @property
    def public_mcp_url(self) -> str:
        return f"{self.normalized_app_base_url}/mcp"

    @property
    def public_mcp_protected_resource_metadata_url(self) -> str:
        return f"{self.normalized_app_base_url}/.well-known/oauth-protected-resource/mcp"

    @property
    def oidc_metadata_url(self) -> str | None:
        if not self.oidc_issuer:
            return None
        issuer = self.oidc_issuer.rstrip("/")
        if issuer.endswith("/.well-known/openid-configuration"):
            return issuer
        return f"{issuer}/.well-known/openid-configuration"

    @classmethod
    def from_environment(cls) -> "AuthSettings":
        return cls(
            auth_mode=str(os.getenv("AUTH_MODE", "disabled") or "disabled").strip().lower(),
            session_secret=os.getenv("SESSION_SECRET"),
            app_base_url=os.getenv("APP_BASE_URL") or "http://127.0.0.1:8000",
            web_app_base_path=os.getenv("WEB_APP_BASE_PATH") or "/app",
            oidc_issuer=os.getenv("OIDC_ISSUER"),
            oidc_client_id=os.getenv("OIDC_CLIENT_ID"),
            oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET"),
            oidc_audience=os.getenv("OIDC_AUDIENCE"),
            oidc_scopes=os.getenv("OIDC_SCOPES") or "openid profile email",
            cookie_secure=(
                None
                if os.getenv("COOKIE_SECURE") is None
                else str(os.getenv("COOKIE_SECURE")).strip().lower() in {"1", "true", "yes", "on"}
            ),
            cookie_samesite=str(os.getenv("COOKIE_SAMESITE", "lax") or "lax").strip().lower(),
            trust_proxy_headers=str(os.getenv("TRUST_PROXY_HEADERS", "")).strip().lower() in {"1", "true", "yes", "on"},
            allowed_origins=_split_csv_env(os.getenv("ALLOWED_ORIGINS")),
            admin_user_ids=_split_csv_env(os.getenv("ADMIN_USER_IDS")),
            admin_user_emails=_split_csv_env(os.getenv("ADMIN_USER_EMAILS")),
        )


@lru_cache(maxsize=1)
def get_auth_settings() -> AuthSettings:
    return AuthSettings.from_environment()


def clear_auth_settings_cache() -> None:
    get_auth_settings.cache_clear()

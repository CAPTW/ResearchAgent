from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, BadTimeSignature, URLSafeTimedSerializer

from app.auth.config import (
    AuthConfigurationError,
    AuthSettings,
    get_auth_settings,
    require_valid_session_secret,
    validate_session_secret,
)
from app.auth.return_targets import canonical_return_target
from app.auth.security_types import ProductUser
from app.auth.sessions import (
    create_product_session,
    is_valid_product_session_id,
    read_product_session,
    revoke_product_session,
)
from app.repositories.runs import load_run_record

if TYPE_CHECKING:
    import jwt

RESEARCH_READ_SCOPE = "research.read"
RESEARCH_WRITE_SCOPE = "research.write"

_OIDC_METADATA_CACHE: dict[str, dict[str, Any]] = {}
_OIDC_JWKS_CLIENTS: dict[str, jwt.PyJWKClient] = {}


@dataclass(frozen=True)
class McpPrincipal:
    user_id: str
    email: str | None
    scopes: set[str]
    claims: dict[str, Any]


@dataclass(frozen=True)
class McpAuthChallenge:
    status_code: int
    error_code: str
    required_scope: str
    message: str
    resource_metadata: str
    www_authenticate: str

    def payload(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "message": self.message,
            "required_scope": self.required_scope,
            "resource_metadata": self.resource_metadata,
        }

    def meta(self) -> dict[str, list[str]]:
        return {"mcp/www_authenticate": [self.www_authenticate]}


def clear_auth_runtime_caches() -> None:
    _OIDC_METADATA_CACHE.clear()
    _OIDC_JWKS_CLIENTS.clear()


def _cookie_serializer(settings: AuthSettings, *, salt: str) -> URLSafeTimedSerializer:
    secret = require_valid_session_secret(settings)
    return URLSafeTimedSerializer(secret_key=secret, salt=salt)


def _dump_signed_cookie(settings: AuthSettings, *, salt: str, payload: dict[str, Any]) -> str:
    return _cookie_serializer(settings, salt=salt).dumps(payload)


def _load_signed_cookie(
    settings: AuthSettings,
    *,
    salt: str,
    value: str | None,
    max_age: int,
) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        payload = _cookie_serializer(settings, salt=salt).loads(value, max_age=max_age)
    except (BadSignature, BadTimeSignature):
        return None
    return payload if isinstance(payload, dict) else None


def get_local_product_user() -> ProductUser:
    return ProductUser(
        user_id="local-internal-user",
        display_name="Local Internal Mode",
        email=None,
        auth_source="disabled",
        is_admin=True,
    )


def _is_admin_identity(*, user_id: str, email: str | None, settings: AuthSettings | None = None) -> bool:
    resolved_settings = settings or get_auth_settings()
    normalized_admin_ids = {value.strip() for value in resolved_settings.admin_user_ids if value.strip()}
    normalized_admin_emails = {
        value.strip().lower() for value in resolved_settings.admin_user_emails if value.strip()
    }
    if user_id in normalized_admin_ids:
        return True
    if email and email.strip().lower() in normalized_admin_emails:
        return True
    return False


def build_product_user_from_claims(claims: dict[str, Any], *, auth_source: str) -> ProductUser:
    user_id = str(claims.get("sub") or claims.get("user_id") or "").strip()
    if not user_id:
        raise RuntimeError("Authenticated identity is missing a stable subject.")
    display_name = (
        str(claims.get("name") or claims.get("preferred_username") or claims.get("email") or user_id).strip() or user_id
    )
    email = str(claims.get("email")).strip() if claims.get("email") else None
    return ProductUser(
        user_id=user_id,
        display_name=display_name,
        email=email,
        auth_source=auth_source,
        is_admin=_is_admin_identity(user_id=user_id, email=email),
    )


def encode_product_session_cookie(user: ProductUser, settings: AuthSettings | None = None) -> str:
    resolved_settings = settings or get_auth_settings()
    require_valid_session_secret(resolved_settings)
    session_id = create_product_session(user, settings=resolved_settings)
    return _dump_signed_cookie(
        resolved_settings,
        salt="product-session",
        payload={"sid": session_id},
    )


def set_product_session_cookie(response: Response, user: ProductUser, settings: AuthSettings | None = None) -> None:
    resolved_settings = settings or get_auth_settings()
    response.set_cookie(
        key=resolved_settings.session_cookie_name,
        value=encode_product_session_cookie(user, resolved_settings),
        httponly=True,
        secure=resolved_settings.resolved_cookie_secure,
        samesite=resolved_settings.cookie_samesite,
        max_age=resolved_settings.product_session_ttl_seconds,
        path="/",
    )


def clear_product_session_cookie(response: Response, settings: AuthSettings | None = None) -> None:
    resolved_settings = settings or get_auth_settings()
    response.delete_cookie(
        key=resolved_settings.session_cookie_name,
        httponly=True,
        secure=resolved_settings.resolved_cookie_secure,
        samesite=resolved_settings.cookie_samesite,
        path="/",
    )


def _session_id_from_product_cookie(request: Request, settings: AuthSettings | None = None) -> str | None:
    resolved_settings = settings or get_auth_settings()
    payload = _load_signed_cookie(
        resolved_settings,
        salt="product-session",
        value=request.cookies.get(resolved_settings.session_cookie_name),
        max_age=resolved_settings.product_session_ttl_seconds,
    )
    if not isinstance(payload, dict) or set(payload) != {"sid"}:
        return None
    session_id = payload.get("sid")
    return session_id if is_valid_product_session_id(session_id) else None


def revoke_current_product_session(request: Request, settings: AuthSettings | None = None) -> bool:
    try:
        session_id = _session_id_from_product_cookie(request, settings=settings)
    except AuthConfigurationError:
        return False
    if not session_id:
        return False
    return revoke_product_session(session_id)


def clear_auth_state_cookie(response: Response, settings: AuthSettings | None = None) -> None:
    resolved_settings = settings or get_auth_settings()
    response.delete_cookie(
        key=resolved_settings.auth_state_cookie_name,
        httponly=True,
        secure=resolved_settings.resolved_cookie_secure,
        samesite=resolved_settings.cookie_samesite,
        path="/",
    )


def read_product_session_user(request: Request, settings: AuthSettings | None = None) -> ProductUser | None:
    resolved_settings = settings or get_auth_settings()
    if not resolved_settings.auth_enabled:
        return get_local_product_user()
    try:
        session_id = _session_id_from_product_cookie(request, settings=resolved_settings)
    except AuthConfigurationError:
        return None
    if not session_id:
        return None
    return read_product_session(session_id, settings=resolved_settings)


def get_optional_product_user(request: Request) -> ProductUser | None:
    return read_product_session_user(request, settings=get_auth_settings())


def require_product_user(request: Request) -> ProductUser:
    settings = get_auth_settings()
    if settings.auth_enabled:
        try:
            require_valid_session_secret(settings)
        except AuthConfigurationError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.reason_code, "message": str(exc)},
            ) from exc
    user = get_optional_product_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_admin_user(request: Request) -> ProductUser:
    user = require_product_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def _oidc_metadata(settings: AuthSettings) -> dict[str, Any]:
    metadata_url = settings.oidc_metadata_url
    if not metadata_url:
        raise RuntimeError("OIDC metadata URL is not configured.")
    cached = _OIDC_METADATA_CACHE.get(metadata_url)
    if cached is not None:
        return cached
    response = httpx.get(metadata_url, timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("OIDC discovery document is invalid.")
    _OIDC_METADATA_CACHE[metadata_url] = payload
    return payload


def _oidc_jwks_client(settings: AuthSettings) -> jwt.PyJWKClient:
    # Local auth never verifies OIDC tokens. Avoid loading its native crypto
    # libraries on desktop startup; retain the same verifier when OIDC is used.
    import jwt

    metadata = _oidc_metadata(settings)
    jwks_uri = str(metadata.get("jwks_uri") or "").strip()
    if not jwks_uri:
        raise RuntimeError("OIDC discovery document is missing jwks_uri.")
    client = _OIDC_JWKS_CLIENTS.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri)
        _OIDC_JWKS_CLIENTS[jwks_uri] = client
    return client


def _jwt_algorithms() -> list[str]:
    return ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]


def _decode_oidc_token(
    token: str,
    *,
    settings: AuthSettings,
    audience: str | None,
    nonce: str | None = None,
) -> dict[str, Any]:
    import jwt

    metadata = _oidc_metadata(settings)
    signing_key = _oidc_jwks_client(settings).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=_jwt_algorithms(),
        issuer=metadata.get("issuer") or settings.oidc_issuer,
        audience=audience,
        options={"verify_aud": bool(audience)},
    )
    if nonce is not None and claims.get("nonce") != nonce:
        raise RuntimeError("OIDC callback nonce validation failed.")
    return claims


def build_oidc_login_redirect(request: Request, *, return_to: str | None = None) -> RedirectResponse:
    settings = get_auth_settings()
    safe_return_target = canonical_return_target(return_to, settings=settings)
    if not settings.auth_enabled:
        return RedirectResponse(url=safe_return_target, status_code=302)

    require_valid_session_secret(settings)
    metadata = _oidc_metadata(settings)
    authorization_endpoint = str(metadata.get("authorization_endpoint") or "").strip()
    if not authorization_endpoint:
        raise RuntimeError("OIDC discovery document is missing authorization_endpoint.")

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    redirect_uri = f"{settings.normalized_app_base_url}/auth/callback"
    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "scope": settings.oidc_scopes,
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
    }
    if settings.oidc_audience:
        params["audience"] = settings.oidc_audience

    response = RedirectResponse(url=f"{authorization_endpoint}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        key=settings.auth_state_cookie_name,
        value=_dump_signed_cookie(
            settings,
            salt="auth-state",
            payload={
                "state": state,
                "nonce": nonce,
                "return_to": safe_return_target,
            },
        ),
        httponly=True,
        secure=settings.resolved_cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.auth_state_ttl_seconds,
        path="/",
    )
    return response


def _exchange_oidc_code(code: str, *, settings: AuthSettings) -> dict[str, Any]:
    metadata = _oidc_metadata(settings)
    token_endpoint = str(metadata.get("token_endpoint") or "").strip()
    if not token_endpoint:
        raise RuntimeError("OIDC discovery document is missing token_endpoint.")
    response = httpx.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{settings.normalized_app_base_url}/auth/callback",
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        },
        headers={"Accept": "application/json"},
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("OIDC token response is invalid.")
    return payload


def build_product_user_from_token_response(
    token_response: dict[str, Any],
    *,
    settings: AuthSettings,
    nonce: str,
) -> ProductUser:
    id_token = str(token_response.get("id_token") or "").strip()
    if not id_token:
        raise RuntimeError("OIDC token response did not include id_token.")
    claims = _decode_oidc_token(id_token, settings=settings, audience=settings.oidc_client_id, nonce=nonce)
    return build_product_user_from_claims(claims, auth_source="oidc")


def complete_oidc_callback(request: Request, *, code: str | None, state: str | None) -> RedirectResponse:
    settings = get_auth_settings()
    if not settings.auth_enabled:
        raise HTTPException(status_code=400, detail="OIDC mode is not enabled.")

    require_valid_session_secret(settings)
    pending = _load_signed_cookie(
        settings,
        salt="auth-state",
        value=request.cookies.get(settings.auth_state_cookie_name),
        max_age=settings.auth_state_ttl_seconds,
    )
    if pending is None:
        raise HTTPException(status_code=400, detail="OIDC login state is missing or expired.")
    if not code or not state or pending.get("state") != state:
        raise HTTPException(status_code=400, detail="OIDC callback state validation failed.")

    token_response = _exchange_oidc_code(code, settings=settings)
    user = build_product_user_from_token_response(token_response, settings=settings, nonce=str(pending.get("nonce") or ""))

    return_target = canonical_return_target(
        pending.get("return_to") if isinstance(pending.get("return_to"), str) else None,
        settings=settings,
    )
    revoke_current_product_session(request, settings=settings)
    response = RedirectResponse(url=return_target, status_code=302)
    set_product_session_cookie(response, user, settings)
    clear_auth_state_cookie(response, settings)
    return response


def build_auth_me_response(request: Request) -> dict[str, Any]:
    settings = get_auth_settings()
    user = read_product_session_user(request, settings=settings)
    warnings: list[str] = []
    session_configuration = validate_session_secret(settings.session_secret, required=settings.auth_enabled)
    if not settings.auth_enabled:
        warnings.append("AUTH_MODE=disabled is an explicit internal single-user mode. Do not use it for shared deployments.")
    elif not session_configuration.valid:
        warnings.append("OIDC session configuration is invalid; authenticated sessions are unavailable.")
    return {
        "auth_mode": settings.auth_mode,
        "authenticated": user is not None,
        "is_admin": bool(user.is_admin) if user else False,
        "user": (
            {
                "user_id": user.user_id,
                "display_name": user.display_name,
                "email": user.email,
                "auth_source": user.auth_source,
            }
            if user
            else None
        ),
        "login_url": (
            f"{settings.normalized_app_base_url}/auth/login"
            if settings.auth_enabled and session_configuration.valid
            else None
        ),
        "logout_url": f"{settings.normalized_app_base_url}/auth/logout",
        "mcp_url": settings.public_mcp_url,
        "mcp_resource_metadata_url": settings.public_mcp_protected_resource_metadata_url,
        "product_auth_server_side_only": True,
        "provider_auth_server_side_only": True,
        "product_session_backend": "local_internal" if not settings.auth_enabled else "server_side_file",
        "session_configuration_status": session_configuration.status,
        "session_configuration_reason_code": session_configuration.reason_code,
        "warnings": warnings,
    }


def _is_run_visible_to_user(run: dict[str, Any], *, user_id: str) -> bool:
    owner_user_id = str(run.get("owner_user_id") or "").strip()
    if not owner_user_id:
        return False
    return owner_user_id == user_id


def list_runs_for_request(request: Request, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = get_auth_settings()
    if not settings.auth_enabled:
        return runs
    user = require_product_user(request)
    return [run for run in runs if _is_run_visible_to_user(run, user_id=user.user_id)]


def ensure_run_access(request: Request, run_id: str) -> ProductUser:
    user = require_product_user(request)
    run = load_run_record(run_id)
    if get_auth_settings().auth_enabled and not _is_run_visible_to_user(run, user_id=user.user_id):
        raise HTTPException(status_code=403, detail="You do not have access to this run.")
    return user


def ensure_runs_access(request: Request, run_ids: list[str]) -> ProductUser:
    user = require_product_user(request)
    if not get_auth_settings().auth_enabled:
        return user
    for run_id in run_ids:
        run = load_run_record(run_id)
        if not _is_run_visible_to_user(run, user_id=user.user_id):
            raise HTTPException(status_code=403, detail="You do not have access to one or more requested runs.")
    return user


def require_run_read_access(run_id: str, request: Request) -> ProductUser:
    return ensure_run_access(request, run_id)


def require_run_write_access(run_id: str, request: Request) -> ProductUser:
    return ensure_run_access(request, run_id)


def owner_user_id_for_request(user: ProductUser | None) -> str | None:
    if user is None or not get_auth_settings().auth_enabled:
        return None
    return user.user_id


def _extract_scopes(claims: dict[str, Any]) -> set[str]:
    scopes: set[str] = set()
    scope_value = claims.get("scope")
    if isinstance(scope_value, str):
        scopes.update(part.strip() for part in scope_value.split(" ") if part.strip())
    scp_value = claims.get("scp")
    if isinstance(scp_value, list):
        scopes.update(str(item).strip() for item in scp_value if str(item).strip())
    elif isinstance(scp_value, str):
        scopes.update(part.strip() for part in scp_value.split(" ") if part.strip())
    return scopes


def _decode_mcp_access_token(token: str, settings: AuthSettings) -> dict[str, Any]:
    audience = str(settings.oidc_audience or "").strip()
    if not audience:
        raise RuntimeError("OIDC_AUDIENCE must be configured for MCP access token validation.")
    return _decode_oidc_token(token, settings=settings, audience=audience)


def _mcp_resource_metadata_url(settings: AuthSettings) -> str:
    return settings.public_mcp_protected_resource_metadata_url


def build_mcp_security_schemes(required_scope: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "oauth2",
            "description": "OIDC bearer token required for protected MCP tools.",
            "scopes": [required_scope],
        }
    ]


def build_mcp_protected_resource_metadata() -> dict[str, Any]:
    settings = get_auth_settings()
    return {
        "resource": settings.public_mcp_url,
        "resource_name": "XRML Research Agent MCP",
        "resource_documentation": f"{settings.public_web_app_url}/",
        "authorization_servers": [settings.oidc_issuer] if settings.auth_enabled and settings.oidc_issuer else [],
        "scopes_supported": [RESEARCH_READ_SCOPE, RESEARCH_WRITE_SCOPE],
        "bearer_methods_supported": ["header"],
        "authentication_required": settings.auth_enabled,
    }


def describe_mcp_oauth_readiness() -> dict[str, Any]:
    settings = get_auth_settings()
    warnings: list[str] = []
    authorization_servers = [settings.oidc_issuer] if settings.auth_enabled and settings.oidc_issuer else []
    public_https = settings.public_mcp_url.startswith("https://")

    if not settings.public_mcp_url:
        readiness_label = "not_configured"
        cli_ready = False
        chatgpt_ready = False
        warnings.append("MCP endpoint is not configured.")
    elif not settings.auth_enabled:
        readiness_label = "local_cli_only"
        cli_ready = True
        chatgpt_ready = False
        warnings.append("ChatGPT Developer Mode requires AUTH_MODE=oidc and a public HTTPS /mcp origin.")
    elif not public_https:
        readiness_label = "oidc_http_only"
        cli_ready = True
        chatgpt_ready = False
        warnings.append("ChatGPT Developer Mode requires a public HTTPS /mcp endpoint.")
    elif not str(settings.oidc_audience or "").strip():
        readiness_label = "oidc_missing_audience"
        cli_ready = True
        chatgpt_ready = False
        warnings.append("Set OIDC_AUDIENCE so MCP access tokens can be audience-validated.")
    else:
        readiness_label = "oauth_ready"
        cli_ready = True
        chatgpt_ready = True
        warnings.append(
            "oauth_ready means the MCP server is configured for OAuth. Live Codex CLI and ChatGPT login still depend on a reachable public deployment plus external IdP/client-registration approval."
        )

    return {
        "endpoint": settings.public_mcp_url,
        "resource_metadata_url": settings.public_mcp_protected_resource_metadata_url,
        "transport": "streamable-http",
        "protocol_version": settings.mcp_protocol_version,
        "auth_required": settings.auth_enabled,
        "public_https": public_https,
        "cli_ready": cli_ready,
        "chatgpt_ready": chatgpt_ready,
        "readiness_label": readiness_label,
        "authorization_servers": authorization_servers,
        "warnings": warnings,
    }


def build_mcp_auth_challenge(
    *,
    status_code: int,
    required_scope: str,
    message: str,
    error_code: str,
) -> McpAuthChallenge:
    settings = get_auth_settings()
    resource_metadata = _mcp_resource_metadata_url(settings)
    www_authenticate = (
        f'Bearer realm="xrml-mcp", error="{error_code}", scope="{required_scope}", '
        f'resource_metadata="{resource_metadata}", error_description="{message}"'
    )
    return McpAuthChallenge(
        status_code=status_code,
        error_code=error_code,
        required_scope=required_scope,
        message=message,
        resource_metadata=resource_metadata,
        www_authenticate=www_authenticate,
    )


def mcp_auth_error_response(
    *,
    status_code: int,
    required_scope: str,
    message: str,
    error_code: str,
) -> JSONResponse:
    challenge = build_mcp_auth_challenge(
        status_code=status_code,
        required_scope=required_scope,
        message=message,
        error_code=error_code,
    )
    return JSONResponse(
        status_code=challenge.status_code,
        content={**challenge.payload(), "_meta": challenge.meta()},
        headers={"WWW-Authenticate": challenge.www_authenticate},
    )


def require_mcp_principal(request: Request, *, required_scope: str) -> McpPrincipal | McpAuthChallenge:
    settings = get_auth_settings()
    if not settings.auth_enabled:
        return McpPrincipal(
            user_id="local-internal-user",
            email=None,
            scopes={RESEARCH_READ_SCOPE, RESEARCH_WRITE_SCOPE},
            claims={"sub": "local-internal-user"},
        )

    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return build_mcp_auth_challenge(
            status_code=401,
            required_scope=required_scope,
            message="Bearer token required for this MCP tool.",
            error_code="invalid_token",
        )

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return build_mcp_auth_challenge(
            status_code=401,
            required_scope=required_scope,
            message="Bearer token required for this MCP tool.",
            error_code="invalid_token",
        )

    try:
        claims = _decode_mcp_access_token(token, settings)
    except Exception:
        return build_mcp_auth_challenge(
            status_code=401,
            required_scope=required_scope,
            message="Bearer token could not be verified.",
            error_code="invalid_token",
        )

    scopes = _extract_scopes(claims)
    if required_scope not in scopes:
        return build_mcp_auth_challenge(
            status_code=403,
            required_scope=required_scope,
            message=f"The bearer token is missing the required scope: {required_scope}.",
            error_code="insufficient_scope",
        )

    user = build_product_user_from_claims(claims, auth_source="oidc")
    return McpPrincipal(user_id=user.user_id, email=user.email, scopes=scopes, claims=claims)


def ensure_mcp_run_access(principal: McpPrincipal, run_id: str) -> None:
    run = load_run_record(run_id)
    if get_auth_settings().auth_enabled and not _is_run_visible_to_user(run, user_id=principal.user_id):
        raise HTTPException(status_code=403, detail="MCP token does not have access to this run.")

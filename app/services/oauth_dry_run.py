from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.repositories.files import iso_now_utc
from app.repositories.jsonl import (
    JsonlPolicy,
    append_jsonl_record,
    read_jsonl_strict,
    replace_jsonl,
)
from app.services import auth_foundation, operator_audit

OAUTH_STATE_PATH = Path("output") / "oauth-dry-run" / "oauth-states.jsonl"
DEFAULT_TTL_SECONDS = 10 * 60
MAX_TTL_SECONDS = 60 * 60
MIN_TTL_SECONDS = 60


def _validate_state_record(record: dict[str, Any]) -> None:
    required_strings = ("providerId", "state", "expiresAt")
    if any(not isinstance(record.get(field), str) or not record[field] for field in required_strings):
        raise ValueError("oauth_state_schema_invalid")
    if record.get("usedAt") is not None and not isinstance(record.get("usedAt"), str):
        raise ValueError("oauth_state_schema_invalid")
    if not isinstance(record.get("tokenExchangePerformed"), bool):
        raise ValueError("oauth_state_schema_invalid")


_OAUTH_STATE_POLICY = JsonlPolicy(
    artifact_role="oauth_state",
    record_validator=_validate_state_record,
)


def _token_urlsafe(length: int) -> str:
    return secrets.token_urlsafe(length)


def _parse_instant(value: str | None) -> datetime:
    text = value or iso_now_utc()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _clamped_ttl(ttl_seconds: int | None) -> int:
    try:
        value = int(ttl_seconds or DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, value))


def _state_records() -> list[dict[str, Any]]:
    return read_jsonl_strict(OAUTH_STATE_PATH, policy=_OAUTH_STATE_POLICY)


def _write_state_records(records: list[dict[str, Any]]) -> None:
    replace_jsonl(OAUTH_STATE_PATH, records, policy=_OAUTH_STATE_POLICY)


def _append_state(record: dict[str, Any]) -> None:
    append_jsonl_record(OAUTH_STATE_PATH, record, policy=_OAUTH_STATE_POLICY)


def _find_state(state: str | None) -> tuple[int | None, dict[str, Any] | None]:
    if not state:
        return None, None
    for index, record in enumerate(_state_records()):
        if record.get("state") == state:
            return index, record
    return None, None


def _client_id_from_env(metadata: dict[str, Any]) -> tuple[str, str, list[str], list[str]]:
    env_var = str(metadata.get("clientIdEnvVar") or "").strip()
    if not env_var:
        return "missing-client-id", "missing_env", ["missing_client_id_env_var"], [
            "OAuth dry-run authorization URLs use a placeholder client_id until clientIdEnvVar is configured."
        ]
    value = os.getenv(env_var)
    if value:
        return value, "resolved", [], []
    return f"missing-env-{env_var}", "missing_env", ["missing_client_id_env_var"], [
        f"{env_var} is not set; authorization URL was built with a safe placeholder client_id."
    ]


def build_oauth_dry_run_status(*, now: str | None = None) -> dict[str, Any]:
    checked_at = _parse_instant(now)
    pending = 0
    expired = 0
    used = 0
    malformed = 0
    for record in _state_records():
        try:
            expires_at = _parse_instant(str(record.get("expiresAt") or ""))
        except (TypeError, ValueError):
            malformed += 1
            continue
        if record.get("usedAt"):
            used += 1
        elif expires_at < checked_at:
            expired += 1
        else:
            pending += 1
    status = "dry_run_ready" if OAUTH_STATE_PATH.exists() else "metadata_only"
    return {
        "oauthMode": status,
        "tokenExchangeEnabled": False,
        "tokenPersistenceEnabled": False,
        "externalAuthCallsEnabled": False,
        "stateStoreAvailable": OAUTH_STATE_PATH.exists(),
        "pendingStateCount": pending,
        "expiredStateCount": expired,
        "usedStateCount": used,
        "malformedStateCount": malformed,
        "stateStorage": "local_ignored",
        "warnings": [
            "OAuth dry-run validates authorization state and callbacks only; token exchange and token persistence are disabled."
        ],
    }


def build_authorization_url(
    oauth_provider_metadata: dict[str, Any],
    *,
    redirect_uri: str,
    create_state: bool = False,
    ttl_seconds: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    validation = auth_foundation.validate_oauth_provider_metadata(oauth_provider_metadata)
    metadata = validation["oauthProviderMetadata"]
    created_at = _parse_instant(now)
    ttl = _clamped_ttl(ttl_seconds)
    expires_at = created_at + timedelta(seconds=ttl)
    reason_codes: list[str] = []
    warnings: list[str] = list(validation.get("warnings") or [])
    errors: list[str] = list(validation.get("errors") or [])

    authorization_endpoint = str(metadata.get("authorizationUrl") or "").strip()
    if not authorization_endpoint:
        errors.append("authorizationUrl is required for OAuth dry-run authorization URL generation.")
        reason_codes.append("missing_authorization_url")
    client_id, client_id_status, client_reasons, client_warnings = _client_id_from_env(metadata)
    reason_codes.extend(client_reasons)
    warnings.extend(client_warnings)

    state = _token_urlsafe(32)
    nonce = _token_urlsafe(32)
    verifier = _token_urlsafe(48)
    challenge = _pkce_challenge(verifier)
    correlation_seed = f"{metadata.get('providerId')}:{state}"
    correlation_id = f"oauth-dry-run-{_sha256(correlation_seed)[:16]}"
    scopes = list(metadata.get("scopes") or ["openid", "profile", "email"])
    if not scopes:
        scopes = ["openid", "profile", "email"]

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": " ".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorization_url = f"{authorization_endpoint}?{urlencode(params)}" if authorization_endpoint else None
    validation_status = "invalid" if errors else "prepared" if create_state else "preview"

    if create_state and not errors:
        _append_state(
            {
                "providerId": metadata.get("providerId"),
                "state": state,
                "nonce": nonce,
                "redirectUri": redirect_uri,
                "scopes": scopes,
                "codeVerifierSha256": _sha256(verifier),
                "codeChallenge": challenge,
                "codeChallengeMethod": "S256",
                "createdAt": _iso(created_at),
                "expiresAt": _iso(expires_at),
                "correlationId": correlation_id,
                "usedAt": None,
                "tokenExchangePerformed": False,
            }
        )

    return {
        "providerId": metadata.get("providerId"),
        "validationStatus": validation_status,
        "authorizationUrl": authorization_url,
        "redirectUri": redirect_uri,
        "scopes": scopes,
        "state": state,
        "nonce": nonce,
        "pkce": {"codeChallenge": challenge, "codeChallengeMethod": "S256"},
        "createdAt": _iso(created_at),
        "expiresAt": _iso(expires_at),
        "correlationId": correlation_id,
        "stateStored": bool(create_state and not errors),
        "clientIdStatus": client_id_status,
        "tokenExchangeEnabled": False,
        "tokenPersistenceEnabled": False,
        "externalAuthCallPerformed": False,
        "reasonCodes": list(dict.fromkeys(reason_codes)),
        "warnings": list(dict.fromkeys(warnings)),
        "errors": list(dict.fromkeys(errors)),
    }


def _mark_state_used(state: str, used_at: str) -> None:
    records = _state_records()
    for record in records:
        if record.get("state") == state:
            record["usedAt"] = used_at
            record["tokenExchangePerformed"] = False
            break
    _write_state_records(records)


def validate_callback_dry_run(
    *,
    provider_id: str,
    state: str | None,
    code: str | None = None,
    error: str | None = None,
    redirect_uri: str | None = None,
    now: str | None = None,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
) -> dict[str, Any]:
    checked_at = _parse_instant(now)
    _index, record = _find_state(state)
    reason_codes: list[str] = []
    warnings: list[str] = []
    state_status = "valid"
    accepted = False
    correlation_id = record.get("correlationId") if record else None

    if not state or record is None:
        state_status = "missing"
        reason_codes.append("missing_state")
    elif record.get("providerId") != provider_id:
        state_status = "unknown"
        reason_codes.append("provider_mismatch")
    elif record.get("usedAt"):
        state_status = "replayed"
        reason_codes.append("replayed_state")
    else:
        expires_at = _parse_instant(str(record.get("expiresAt") or ""))
        if expires_at < checked_at:
            state_status = "expired"
            reason_codes.append("expired_state")
        elif redirect_uri and record.get("redirectUri") and redirect_uri != record.get("redirectUri"):
            reason_codes.append("redirect_uri_mismatch")
        elif error:
            reason_codes.append("provider_error")
            warnings.append("OAuth provider returned an error; no token exchange was attempted.")
        elif not code:
            reason_codes.append("missing_code")
        else:
            accepted = True

    if accepted and state:
        _mark_state_used(state, _iso(checked_at))

    decision = "allowed" if accepted else "rejected"
    if audit:
        operator_audit.record_event(
            action_type="oauth_callback_dry_run",
            decision=decision,
            operator_id=operator_id,
            auth_mode=auth_mode,
            capability_required="view_dashboard",
            capability_present=capability_present,
            reason_codes=reason_codes,
            target_type="oauth_provider",
            target_id=provider_id,
            risk_level="read_only",
            metadata={
                "providerId": provider_id,
                "stateStatus": state_status,
                "codePresent": bool(code),
                "errorPresent": bool(error),
                "acceptedForTokenExchange": accepted,
                "tokenExchangePerformed": False,
                "correlationId": correlation_id,
            },
        )

    return {
        "providerId": provider_id,
        "acceptedForTokenExchange": accepted,
        "tokenExchangePerformed": False,
        "stateStatus": state_status,
        "correlationId": correlation_id,
        "secretPresent": False,
        "externalAuthCallPerformed": False,
        "reasonCodes": list(dict.fromkeys(reason_codes)),
        "warnings": list(dict.fromkeys(warnings)),
    }

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from app.repositories.files import iso_now_utc
from app.schemas.auth import (
    OAuthLiveExchangeGateSummaryResponse,
    OAuthLiveExchangePilotResultResponse,
    OAuthLiveExchangePilotStatusResponse,
    OAuthLiveExchangeRedactionSummaryResponse,
    OAuthRealProviderSmokeResultResponse,
    OAuthRealProviderSmokeStatusResponse,
)
from app.services import operator_audit

TOKEN_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "accessToken": ("accessToken", "access_token"),
    "refreshToken": ("refreshToken", "refresh_token"),
    "idToken": ("idToken", "id_token"),
}
TOKEN_FIELD_LABELS = {
    "accessToken": "access_token",
    "refreshToken": "refresh_token",
    "idToken": "id_token",
}
SECRET_VALUE_PREFIXES = ("sk-", "ya29.", "ghp_", "xoxb-", "xoxp-", "Bearer ")
SAFE_REFERENCE_KEY_SUFFIXES = (
    "envvar",
    "env_var",
    "sha256",
    "hash",
    "digest",
    "present",
    "enabled",
    "returned",
    "audited",
    "stored",
    "count",
    "note",
    "notes",
)


def _normalized_key(value: str) -> str:
    return value.replace("-", "_").replace(".", "_").lower()


def _compact_key(value: str) -> str:
    return _normalized_key(value).replace("_", "")


def _safe_field_context(key: str) -> bool:
    normalized = _normalized_key(key)
    compact = normalized.replace("_", "")
    return normalized.endswith(SAFE_REFERENCE_KEY_SUFFIXES) or compact.endswith("envvar")


def _jwt_like(value: str) -> bool:
    return bool(re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value.strip()))


def _string_secret_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("Bearer "):
        return "bearer_token_like_value"
    if text.startswith("ya29."):
        return "access_token_like_value"
    if _jwt_like(text):
        return "id_token_like_value"
    if any(text.startswith(prefix) for prefix in SECRET_VALUE_PREFIXES):
        return "token_like_value"
    return None


def _field_secret_reason(key: str, value: Any) -> str | None:
    if value in (None, "", False, "<redacted>") or isinstance(value, bool):
        return None
    if _safe_field_context(key):
        return None
    compact = _compact_key(key)
    if compact in {"accesstoken"}:
        return "access_token_like_value"
    if compact in {"refreshtoken"}:
        return "refresh_token_like_value"
    if compact in {"idtoken"}:
        return "id_token_like_value"
    if compact in {"authorizationcode", "authcode"}:
        return "authorization_code_field"
    if compact in {"clientsecret", "oauthclientsecret"}:
        return "client_secret_field"
    if compact in {"apikey", "providerapikey"}:
        return "api_key_field"
    if compact in {"rawtokenresponse", "rawtokenresponsebody", "tokenresponsebody", "rawresponsebody"}:
        return "raw_token_response_field"
    if "secret" in compact or "credential" in compact or "bearer" in compact:
        return "token_like_field"
    return None


def scan_oauth_live_pilot_leaks(value: Any) -> dict[str, Any]:
    reason_codes: list[str] = []
    redacted_paths: list[str] = []

    def add(reason: str, path: str) -> None:
        reason_codes.append(reason)
        redacted_paths.append(path)

    def walk(current: Any, path: str, *, key: str = "") -> None:
        field_reason = _field_secret_reason(key, current) if key else None
        if field_reason:
            add(field_reason, path)
        value_reason = _string_secret_reason(current)
        if value_reason:
            add(value_reason, path)
        if isinstance(current, dict):
            for child_key, child_value in current.items():
                child_path = f"{path}.{child_key}" if path else f"$.{child_key}"
                walk(child_value, child_path, key=str(child_key))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                walk(item, f"{path}[{index}]", key=key)

    walk(value, "$")
    unique_reasons = _unique(reason_codes)
    unique_paths = _unique(redacted_paths)
    return {
        "leakDetected": bool(unique_paths),
        "leakCount": len(unique_paths),
        "reasonCodes": unique_reasons,
        "redactedPaths": unique_paths,
        "warningCount": len(unique_paths),
    }


def _first(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _present(value: Any) -> bool:
    return bool(str(value or "").strip())


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value.split(" ") if isinstance(value, str) else value
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


def _token_value(token_response: dict[str, Any], canonical_key: str) -> Any:
    return _first(token_response, *TOKEN_FIELD_ALIASES[canonical_key])


def _subject_value(token_response: dict[str, Any]) -> str | None:
    direct = _first(token_response, "subject", "sub")
    if direct:
        if isinstance(direct, dict | list | tuple | set):
            return None
        text = str(direct).strip()
        return None if _string_secret_reason(text) else text
    claims = _first(token_response, "idTokenClaims", "id_token_claims", default={})
    if isinstance(claims, dict):
        claim_subject = _first(claims, "sub", "subject")
        if claim_subject:
            if isinstance(claim_subject, dict | list | tuple | set):
                return None
            text = str(claim_subject).strip()
            return None if _string_secret_reason(text) else text
    return None


def _scopes(token_response: dict[str, Any], request_scopes: Any = None) -> list[str]:
    raw = _first(token_response, "scope", "scopes", default=request_scopes)
    return _string_list(raw)


def _redaction_summary(token_response: dict[str, Any]) -> dict[str, Any]:
    redacted_fields = [
        TOKEN_FIELD_LABELS[canonical]
        for canonical in TOKEN_FIELD_ALIASES
        if _present(_token_value(token_response, canonical))
    ]
    return {
        "redactedFields": redacted_fields,
        "redactedTokenFieldCount": len(redacted_fields),
        "authorizationCodeRedacted": True,
        "rawTokenStored": False,
    }


def build_token_exchange_status() -> dict[str, Any]:
    return {
        "exchangeMode": "mock_only",
        "mockExchangeSupported": True,
        "liveExchangeSupported": False,
        "tokenStorageEnabled": False,
        "accountLinkPreviewEnabled": True,
        "externalAuthCallsEnabled": False,
        "reasonCodes": ["live_token_exchange_disabled", "token_storage_disabled", "mock_validation_only"],
        "warnings": [
            "OAuth token exchange is mock-validation only; live token endpoint calls and token persistence are disabled."
        ],
    }


def _base_response(payload: dict[str, Any], *, exchange_mode: str | None = None) -> dict[str, Any]:
    return {
        "exchangePerformed": False,
        "tokenExchangePerformed": False,
        "exchangeMode": exchange_mode or str(_first(payload, "exchangeMode", "exchange_mode", default="mock_only")),
        "providerId": str(_first(payload, "providerId", "provider_id", default="oauth-provider")),
        "correlationId": _first(payload, "correlationId", "correlation_id"),
        "accepted": False,
        "tokenResponsePresent": False,
        "accessTokenPresent": False,
        "refreshTokenPresent": False,
        "idTokenPresent": False,
        "expiresIn": None,
        "scopes": [],
        "subjectHash": None,
        "rawTokenStored": False,
        "validationSummary": {
            "valid": False,
            "tokenResponsePresent": False,
            "accessTokenPresent": False,
            "refreshTokenPresent": False,
            "idTokenPresent": False,
            "expiresIn": None,
            "scopes": [],
            "subjectHash": None,
            "reasonCodes": [],
        },
        "accountLinkPreview": {
            "providerId": str(_first(payload, "providerId", "provider_id", default="oauth-provider")),
            "subjectHash": None,
            "scopes": [],
            "linkedAt": None,
            "tokenStorageEnabled": False,
            "linkStored": False,
        },
        "redactionSummary": {
            "redactedFields": [],
            "redactedTokenFieldCount": 0,
            "authorizationCodeRedacted": True,
            "rawTokenStored": False,
        },
        "reasonCodes": [],
        "warnings": [],
    }


def validate_mock_token_exchange(
    payload: dict[str, Any],
    *,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
    now: str | None = None,
) -> dict[str, Any]:
    response = _base_response(payload, exchange_mode="mock_only")
    token_response = _first(payload, "mockTokenResponse", "mock_token_response", default={})
    if not isinstance(token_response, dict):
        token_response = {}
    authorization_code_present = bool(_first(payload, "authorizationCodePresent", "authorization_code_present", default=False))
    code_verifier_present = bool(_first(payload, "codeVerifierPresent", "code_verifier_present", default=False))
    state_status = str(_first(payload, "stateStatus", "state_status", default="unknown"))
    reason_codes: list[str] = []
    warnings = ["Mock token exchange validates response shape only; no external token endpoint was called."]

    if state_status != "valid":
        reason_codes.append("invalid_state_status")
    if not authorization_code_present:
        reason_codes.append("missing_authorization_code")
    if not code_verifier_present:
        reason_codes.append("missing_code_verifier")
    if not token_response:
        reason_codes.append("missing_token_response")

    access_token_present = _present(_token_value(token_response, "accessToken"))
    refresh_token_present = _present(_token_value(token_response, "refreshToken"))
    id_token_present = _present(_token_value(token_response, "idToken"))
    if token_response and not (access_token_present or id_token_present):
        reason_codes.append("missing_access_or_id_token")

    scopes = _scopes(token_response, _first(payload, "scopes"))
    subject = _subject_value(token_response)
    subject_hash = _sha256(subject) if subject else None
    expires_in = _int_or_none(_first(token_response, "expiresIn", "expires_in"))
    redaction_summary = _redaction_summary(token_response)
    accepted = not reason_codes
    linked_at = now or iso_now_utc()

    response.update(
        {
            "exchangePerformed": accepted,
            "tokenExchangePerformed": False,
            "accepted": accepted,
            "tokenResponsePresent": bool(token_response),
            "accessTokenPresent": access_token_present,
            "refreshTokenPresent": refresh_token_present,
            "idTokenPresent": id_token_present,
            "expiresIn": expires_in,
            "scopes": scopes,
            "subjectHash": subject_hash,
            "rawTokenStored": False,
            "validationSummary": {
                "valid": accepted,
                "tokenResponsePresent": bool(token_response),
                "accessTokenPresent": access_token_present,
                "refreshTokenPresent": refresh_token_present,
                "idTokenPresent": id_token_present,
                "expiresIn": expires_in,
                "scopes": scopes,
                "subjectHash": subject_hash,
                "reasonCodes": reason_codes,
            },
            "accountLinkPreview": {
                "providerId": response["providerId"],
                "subjectHash": subject_hash,
                "scopes": scopes,
                "linkedAt": linked_at if accepted else None,
                "tokenStorageEnabled": False,
                "linkStored": False,
            },
            "redactionSummary": redaction_summary,
            "reasonCodes": reason_codes,
            "warnings": warnings,
        }
    )
    if audit:
        operator_audit.record_event(
            action_type="oauth_mock_exchange_validate",
            decision="allowed" if accepted else "rejected",
            operator_id=operator_id,
            auth_mode=auth_mode,
            capability_required="view_dashboard",
            capability_present=capability_present,
            reason_codes=reason_codes,
            target_type="oauth_provider",
            target_id=response["providerId"],
            risk_level="read_only",
            metadata={
                "providerId": response["providerId"],
                "correlationId": response["correlationId"],
                "tokenResponsePresent": bool(token_response),
                "accessTokenPresent": access_token_present,
                "refreshTokenPresent": refresh_token_present,
                "idTokenPresent": id_token_present,
                "redactedFields": redaction_summary["redactedFields"],
                "subjectHash": subject_hash,
                "rawTokenStored": False,
                "tokenExchangePerformed": False,
                "accountLinkPreviewOnly": True,
            },
        )
    return response


def live_token_exchange_disabled(
    payload: dict[str, Any],
    *,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
) -> dict[str, Any]:
    response = _base_response(payload, exchange_mode="live_not_supported")
    response.update(
        {
            "accepted": False,
            "exchangePerformed": False,
            "tokenExchangePerformed": False,
            "rawTokenStored": False,
            "reasonCodes": ["live_token_exchange_disabled"],
            "warnings": ["Live OAuth token exchange is gated and disabled by default; a fail-closed operator-approval pilot exists but is not enabled in this configuration."],
            "validationSummary": {
                **response["validationSummary"],
                "reasonCodes": ["live_token_exchange_disabled"],
            },
        }
    )
    if audit:
        operator_audit.record_event(
            action_type="oauth_live_exchange_disabled",
            decision="rejected",
            operator_id=operator_id,
            auth_mode=auth_mode,
            capability_required="view_dashboard",
            capability_present=capability_present,
            reason_codes=["live_token_exchange_disabled"],
            target_type="oauth_provider",
            target_id=response["providerId"],
            risk_level="read_only",
            metadata={
                "providerId": response["providerId"],
                "correlationId": response["correlationId"],
                "tokenExchangePerformed": False,
                "rawTokenStored": False,
            },
        )
    return response


LIVE_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5
LIVE_TOKEN_EXCHANGE_MAX_RESPONSE_BYTES = 32_768


def build_live_exchange_pilot_status() -> dict[str, Any]:
    return OAuthLiveExchangePilotStatusResponse.model_validate(
        {
            "pilotStatus": "disabled_by_default",
            "liveExchangeEnabledByDefault": False,
            "tokenStorageEnabled": False,
            "dashboardExecutionEnabled": False,
            "tokenStorageStillDisabled": True,
            "dashboardExecutionStillDisabled": True,
            "explicitOperatorAcceptanceRequired": True,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "livePilotLeakCheckStatus": "not_run",
            "latestRedactionSummary": _empty_live_redaction_summary(),
            "lastPilotStatus": None,
            "reasonCodes": [
                "live_exchange_pilot_disabled_by_default",
                "explicit_operator_acceptance_required",
                "token_storage_disabled",
                "dashboard_execution_disabled",
                "live_pilot_leak_check_not_run",
            ],
            "warnings": [
                "Live OAuth token exchange pilot is disabled by default and available only through explicit API/CLI acceptance.",
                "Dashboard remains read-only; it cannot trigger login, token exchange, or provider execution.",
            ],
        }
    ).model_dump(mode="json")


def build_real_provider_smoke_status() -> dict[str, Any]:
    return OAuthRealProviderSmokeStatusResponse.model_validate(
        {
            "smokeStatus": "not_configured",
            "liveSmokeEnabledByDefault": False,
            "tokenStorageEnabled": False,
            "dashboardExecutionEnabled": False,
            "externalAuthCallPerformed": False,
            "latestSmokeEvidence": "none",
            "reasonCodes": [
                "real_provider_smoke_disabled_by_default",
                "real_provider_smoke_requires_test_provider_config",
                "explicit_operator_acceptance_required",
                "token_storage_disabled",
                "dashboard_execution_disabled",
            ],
            "warnings": [
                "Optional real OAuth provider smoke is disabled by default and available only through explicit operator-accepted API/CLI use.",
                "Dashboard remains read-only; it cannot trigger OAuth login, token exchange, smoke, or provider execution.",
            ],
        }
    ).model_dump(mode="json")


def _https_endpoint(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def _bool_payload(payload: dict[str, Any], *keys: str, default: bool = False) -> bool:
    value = _first(payload, *keys, default=default)
    return value is True


def _request_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = _first(payload, "oauthProviderMetadata", "oauth_provider_metadata", default={})
    return metadata if isinstance(metadata, dict) else {}


def _request_checklist(payload: dict[str, Any]) -> dict[str, Any]:
    checklist = _first(payload, "checklist", default={})
    return checklist if isinstance(checklist, dict) else {}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _redaction_summary_for_live(token_response: dict[str, Any]) -> dict[str, Any]:
    summary = _redaction_summary(token_response)
    scan = scan_oauth_live_pilot_leaks(token_response)
    reason_codes = list(scan.get("reasonCodes") or [])
    if scan.get("leakDetected"):
        reason_codes.append("nested_token_response_leak_detected")
    summary.update(
        {
            "redactedFields": _unique([*summary.get("redactedFields", []), *list(scan.get("redactedPaths") or [])]),
            "leakDetected": bool(scan.get("leakDetected")),
            "leakCount": int(scan.get("leakCount") or 0),
            "reasonCodes": _unique(reason_codes),
            "warningCount": int(scan.get("warningCount") or 0),
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
        }
    )
    return OAuthLiveExchangeRedactionSummaryResponse.model_validate(summary).model_dump(mode="json")


def _empty_live_redaction_summary() -> dict[str, Any]:
    return OAuthLiveExchangeRedactionSummaryResponse.model_validate({}).model_dump(mode="json")


def _base_live_pilot_result(
    payload: dict[str, Any],
    *,
    gate_summary: dict[str, Any],
    reason_codes: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    provider_id = str(gate_summary.get("providerId") or _first(payload, "providerId", "provider_id", default="oauth-provider"))
    return OAuthLiveExchangePilotResultResponse.model_validate(
        {
            "liveExchangePerformed": False,
            "providerId": provider_id,
            "correlationId": _first(payload, "correlationId", "correlation_id"),
            "gateStatus": gate_summary.get("gateStatus", "blocked"),
            "tokenEndpointCalled": False,
            "tokenResponsePresent": False,
            "accessTokenPresent": False,
            "refreshTokenPresent": False,
            "idTokenPresent": False,
            "rawTokenStored": False,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "accountLinkCreated": False,
            "accountLinkMetadataOnly": True,
            "accountLink": None,
            "gateSummary": gate_summary,
            "redactionSummary": _empty_live_redaction_summary(),
            "reasonCodes": _unique(reason_codes or list(gate_summary.get("reasonCodes") or [])),
            "warnings": warnings
            or [
                "Live exchange pilot is fail-closed and never stores, returns, audits, or displays token values.",
                "Provider execution gates remain independent from OAuth account links.",
            ],
        }
    ).model_dump(mode="json")


def _build_live_exchange_gate(payload: dict[str, Any], *, operator_identity: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from app.services import action_capabilities, auth_foundation, oauth_account_links, oauth_live_readiness

    identity = operator_identity or auth_foundation.operator_identity_for_user(None)
    metadata_payload = _request_metadata(payload)
    validation = auth_foundation.validate_oauth_provider_metadata(metadata_payload)
    metadata = validation.get("oauthProviderMetadata") or {}
    provider_id = str(metadata.get("providerId") or _first(payload, "providerId", "provider_id", default="oauth-provider"))
    redirect_uri = _first(payload, "redirectUri", "redirect_uri")
    account_link_health = oauth_account_links.build_account_link_health_summary()
    session_policy = oauth_account_links.build_session_capability_policy(identity)
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=metadata_payload,
        redirect_uri=redirect_uri,
        operator_identity=identity,
        account_link_health=account_link_health,
        session_policy=session_policy,
    )
    remediation = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=metadata_payload)
    preflight = oauth_live_readiness.build_preflight_package(
        provider_metadata=metadata_payload,
        redirect_uri=redirect_uri,
        operator_identity=identity,
        account_link_health=account_link_health,
        session_policy=session_policy,
        readiness=readiness,
        remediation=remediation,
    )
    checklist = oauth_live_readiness.validate_operator_acceptance_checklist(_request_checklist(payload), operator_identity=identity)
    capability = action_capabilities.evaluate_action_capability(
        "oauth_live_exchange_pilot",
        operator_capabilities=identity.get("capabilities") or [],
        fallback_risk_level="oauth_live_pilot",
    )
    token_url = metadata.get("tokenUrl")
    token_endpoint_https = _https_endpoint(token_url)
    failed_checks: list[str] = []
    reason_codes: list[str] = ["token_storage_disabled", "live_exchange_pilot_explicit_only"]
    warnings: list[str] = [
        "Live exchange pilot is disabled by default; this path is available only for explicit operator-accepted API/CLI use.",
        "Token values and authorization codes are never persisted, returned, or audited.",
    ]

    if _bool_payload(payload, "dryRunOnly", "dry_run_only", default=True):
        failed_checks.append("dry_run_only")
        reason_codes.append("dry_run_only")
        gate_status = "dry_run_only"
    else:
        gate_status = "blocked"

    if not _bool_payload(payload, "liveExchangeRequested", "live_exchange_requested"):
        failed_checks.append("live_exchange_requested")
        reason_codes.append("missing_live_exchange_request")
    if not _bool_payload(payload, "operatorAcceptancePresent", "operator_acceptance_present"):
        failed_checks.append("operator_acceptance_present")
        reason_codes.append("missing_operator_acceptance")
    if _bool_payload(payload, "tokenStorageRequested", "token_storage_requested"):
        failed_checks.append("token_storage_requested")
        reason_codes.append("token_storage_requested")
    if _bool_payload(payload, "dashboardExecutionContext", "dashboard_execution_context"):
        failed_checks.append("dashboard_execution_context")
        reason_codes.append("dashboard_execution_context_forbidden")
    if str(_first(payload, "stateStatus", "state_status", default="unknown")) != "valid":
        failed_checks.append("callback_state")
        reason_codes.append("callback_state_not_accepted")
    if not _bool_payload(payload, "authorizationCodePresent", "authorization_code_present"):
        failed_checks.append("authorization_code_present")
        reason_codes.append("missing_authorization_code")
    if not _bool_payload(payload, "codeVerifierPresent", "code_verifier_present"):
        failed_checks.append("code_verifier_present")
        reason_codes.append("missing_code_verifier")
    if not token_endpoint_https:
        failed_checks.append("token_endpoint_https")
        reason_codes.append("non_https_token_endpoint")

    readiness_status = str(readiness.get("readinessStatus") or "blocked")
    preflight_status = str((preflight.get("summary") or {}).get("preflightStatus") or "blocked")
    if readiness_status not in {"ready_for_future_live_exchange", "warnings_only"}:
        failed_checks.append("readiness_gate")
        reason_codes.append("readiness_gate_not_ready")
    if preflight_status not in {"ready", "warnings"}:
        failed_checks.append("preflight_package")
        reason_codes.append("preflight_not_ready")
    if not checklist.get("acceptanceValid"):
        failed_checks.append("operator_checklist")
        reason_codes.append("operator_checklist_invalid")
    if not capability.get("allowed"):
        failed_checks.append("operator_capability")
        reason_codes.extend(capability.get("reasonCodes") or ["missing_capability"])
    if account_link_health.get("healthStatus") not in {"empty", "valid"}:
        failed_checks.append("account_link_health")
        reason_codes.append("account_link_health_not_safe")
    if session_policy.get("accountLinkGrantsProviderExecution") is not False or str(session_policy.get("accountLinkInfluence") or "none") not in {
        "none",
        "display_only",
    }:
        failed_checks.append("session_policy")
        reason_codes.append("unsafe_session_capability_policy")

    if not failed_checks:
        gate_status = "passed"
    elif gate_status != "dry_run_only":
        gate_status = "blocked"

    gate_summary = OAuthLiveExchangeGateSummaryResponse.model_validate(
        {
            "gateStatus": gate_status,
            "providerId": provider_id,
            "correlationId": _first(payload, "correlationId", "correlation_id"),
            "tokenEndpointHttps": token_endpoint_https,
            "preflightValid": preflight_status in {"ready", "warnings"},
            "checklistValid": bool(checklist.get("acceptanceValid")),
            "operatorAcceptancePresent": _bool_payload(payload, "operatorAcceptancePresent", "operator_acceptance_present"),
            "liveExchangeRequested": _bool_payload(payload, "liveExchangeRequested", "live_exchange_requested"),
            "authorizationCodePresent": _bool_payload(payload, "authorizationCodePresent", "authorization_code_present"),
            "stateAccepted": str(_first(payload, "stateStatus", "state_status", default="unknown")) == "valid",
            "codeVerifierPresent": _bool_payload(payload, "codeVerifierPresent", "code_verifier_present"),
            "tokenStorageEnabled": False,
            "dashboardExecutionContext": False,
            "failedChecks": _unique(failed_checks),
            "warningChecks": list(preflight.get("summary", {}).get("warningCheckCount") and ["preflight_warnings"] or []),
            "reasonCodes": _unique(reason_codes),
            "warnings": warnings,
        }
    ).model_dump(mode="json")
    return gate_summary, metadata, checklist


def build_live_exchange_gate_preview(payload: dict[str, Any], *, operator_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    request_payload = {**dict(payload or {}), "dryRunOnly": True}
    gate_summary, _metadata, _checklist = _build_live_exchange_gate(request_payload, operator_identity=operator_identity)
    return gate_summary


def _default_live_token_http_client(request: dict[str, Any]) -> dict[str, Any]:
    token_url = str(request.get("tokenUrl") or "")
    if not _https_endpoint(token_url):
        return {"statusCode": 0, "json": {}, "error": "non_https_token_endpoint"}
    body = urlencode(request.get("form") or {}).encode("utf-8")
    http_request = Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    timeout_seconds = int(request.get("timeoutSeconds") or LIVE_TOKEN_EXCHANGE_TIMEOUT_SECONDS)
    max_response_bytes = int(request.get("maxResponseBytes") or LIVE_TOKEN_EXCHANGE_MAX_RESPONSE_BYTES)
    content_type = ""
    try:
        with urlopen(http_request, timeout=timeout_seconds) as response:
            raw = response.read(max_response_bytes + 1)
            status_code = int(getattr(response, "status", 200))
            content_type = _response_content_type(response)
    except HTTPError as exc:
        raw = exc.read(max_response_bytes + 1)
        status_code = int(exc.code)
        content_type = _response_content_type(exc)
    except TimeoutError:
        return {"statusCode": 0, "json": {}, "error": "token_endpoint_timeout"}
    except URLError:
        return {"statusCode": 0, "json": {}, "error": "token_endpoint_unreachable"}
    if len(raw) > max_response_bytes:
        return {"statusCode": status_code, "json": {}, "error": "token_response_too_large"}
    if raw and content_type and not _json_content_type(content_type):
        return {"statusCode": status_code, "json": {}, "error": "unexpected_token_response_content_type"}
    try:
        parsed = json.loads(raw.decode("utf-8") if raw else "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"statusCode": status_code, "json": {}, "error": "token_response_not_json"}
    if not isinstance(parsed, dict):
        return {"statusCode": status_code, "json": {}, "error": "token_response_not_object"}
    return {"statusCode": status_code, "json": parsed if isinstance(parsed, dict) else {}}


LIVE_TOKEN_HTTP_CLIENT = _default_live_token_http_client


def _token_client_request(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    client_id_name = str(metadata.get("clientIdEnvVar") or "").strip()
    client_secret_name = str(metadata.get("clientSecretEnvVar") or "").strip()
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": str(_first(payload, "authorizationCode", "authorization_code", default="")),
        "redirect_uri": str(_first(payload, "redirectUri", "redirect_uri", default="")),
        "client_id": os.environ.get(client_id_name, "") if client_id_name else "",
        "code_verifier": str(_first(payload, "codeVerifier", "code_verifier", default="")),
    }
    if client_secret_name:
        form["client_secret"] = os.environ.get(client_secret_name, "")
    return {
        "tokenUrl": metadata.get("tokenUrl"),
        "form": form,
        "timeoutSeconds": LIVE_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
        "maxResponseBytes": LIVE_TOKEN_EXCHANGE_MAX_RESPONSE_BYTES,
    }


def _response_content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("Content-Type") or headers.get("content-type")
            if value:
                return str(value)
        except AttributeError:
            pass
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        value = getheader("Content-Type") or getheader("content-type")
        if value:
            return str(value)
    return ""


def _json_content_type(value: str) -> bool:
    content_type = value.split(";", 1)[0].strip().lower()
    return content_type == "application/json" or content_type.endswith("+json")


def _audit_live_pilot(
    result: dict[str, Any],
    *,
    operator_id: str,
    auth_mode: str,
    capability_present: bool | None,
) -> None:
    operator_audit.record_event(
        action_type="oauth_live_exchange_pilot",
        decision="allowed" if result.get("gateStatus") == "passed" and result.get("liveExchangePerformed") else "rejected",
        operator_id=operator_id,
        auth_mode=auth_mode,
        capability_required="edit_topic_config",
        capability_present=capability_present,
        reason_codes=list(result.get("reasonCodes") or []),
        target_type="oauth_provider",
        target_id=result.get("providerId"),
        risk_level="oauth_live_pilot",
        metadata={
            "providerId": result.get("providerId"),
            "correlationId": result.get("correlationId"),
            "gateStatus": result.get("gateStatus"),
            "tokenEndpointCalled": bool(result.get("tokenEndpointCalled")),
            "tokenResponsePresent": bool(result.get("tokenResponsePresent")),
            "accessTokenPresent": bool(result.get("accessTokenPresent")),
            "refreshTokenPresent": bool(result.get("refreshTokenPresent")),
            "idTokenPresent": bool(result.get("idTokenPresent")),
            "redactedFields": list((result.get("redactionSummary") or {}).get("redactedFields") or []),
            "rawTokenStored": False,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "accountLinkCreated": bool(result.get("accountLinkCreated")),
            "accountLinkMetadataOnly": True,
            "accountLinkId": (result.get("accountLink") or {}).get("accountLinkId") if result.get("accountLink") else None,
        },
    )


def perform_live_exchange_pilot(
    payload: dict[str, Any],
    *,
    http_client: Any | None = None,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    from app.services import oauth_account_links

    request_payload = dict(payload or {})
    gate_summary, metadata, _checklist = _build_live_exchange_gate(request_payload, operator_identity=operator_identity)
    if gate_summary["gateStatus"] != "passed":
        result = _base_live_pilot_result(request_payload, gate_summary=gate_summary)
        if audit and gate_summary["gateStatus"] != "dry_run_only":
            _audit_live_pilot(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    client = http_client or LIVE_TOKEN_HTTP_CLIENT
    token_result = client(_token_client_request(request_payload, metadata))
    status_code = int(token_result.get("statusCode") or token_result.get("status_code") or 0)
    token_response = token_result.get("json") if isinstance(token_result.get("json"), dict) else {}
    if status_code < 200 or status_code >= 300 or not isinstance(token_response, dict):
        reason_codes = _unique([*gate_summary.get("reasonCodes", []), "token_endpoint_error"])
        if token_result.get("error"):
            reason_codes.append(str(token_result["error"]))
        result = _base_live_pilot_result(
            request_payload,
            gate_summary={**gate_summary, "gateStatus": "failed", "reasonCodes": reason_codes},
            reason_codes=reason_codes,
            warnings=["Token endpoint returned a non-2xx or invalid response; raw response body was not returned or audited."],
        )
        result["tokenEndpointCalled"] = True
        if audit:
            _audit_live_pilot(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return OAuthLiveExchangePilotResultResponse.model_validate(result).model_dump(mode="json")

    access_token_present = _present(_token_value(token_response, "accessToken"))
    refresh_token_present = _present(_token_value(token_response, "refreshToken"))
    id_token_present = _present(_token_value(token_response, "idToken"))
    redaction_summary = _redaction_summary_for_live(token_response)
    if not access_token_present:
        reason_codes = _unique(
            [
                *gate_summary.get("reasonCodes", []),
                "token_endpoint_error",
                "missing_access_token",
                "token_storage_disabled",
            ]
        )
        result = _base_live_pilot_result(
            request_payload,
            gate_summary={**gate_summary, "gateStatus": "failed", "reasonCodes": reason_codes},
            reason_codes=reason_codes,
            warnings=["Token endpoint response did not include an access token; no account link was created."],
        )
        result.update(
            {
                "gateStatus": "failed",
                "tokenEndpointCalled": True,
                "tokenResponsePresent": bool(token_response),
                "accessTokenPresent": access_token_present,
                "refreshTokenPresent": refresh_token_present,
                "idTokenPresent": id_token_present,
                "redactionSummary": redaction_summary,
            }
        )
        if audit:
            _audit_live_pilot(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return OAuthLiveExchangePilotResultResponse.model_validate(result).model_dump(mode="json")

    scopes = _scopes(token_response, metadata.get("scopes"))
    subject = _subject_value(token_response)
    subject_hash = _sha256(subject) if subject else None
    reason_codes = _unique([*gate_summary.get("reasonCodes", []), "live_exchange_pilot_tokens_redacted", "token_storage_disabled"])
    if redaction_summary.get("leakDetected"):
        reason_codes.append("secret_like_token_response_field_redacted")
    account_link = None
    account_link_created = False
    if subject_hash:
        link_result = oauth_account_links.save_future_live_account_link_metadata(
            {
                "providerId": metadata.get("providerId") or request_payload.get("providerId"),
                "operatorId": _first(request_payload, "operatorId", "operator_id", default=operator_id),
                "subjectHash": subject_hash,
                "scopes": scopes,
                "reasonCodes": ["token_storage_disabled", "live_exchange_pilot_metadata_only"],
                "warnings": ["Live exchange pilot token values were redacted and discarded before metadata-only account link storage."],
            },
            operator_identity=operator_identity,
            now=now,
        )
        if link_result.get("accepted"):
            account_link = link_result.get("accountLink")
            account_link_created = True
            reason_codes.append("account_link_metadata_created")
    else:
        reason_codes.append("missing_subject_hash_for_account_link")

    result = OAuthLiveExchangePilotResultResponse.model_validate(
        {
            "liveExchangePerformed": True,
            "providerId": str(metadata.get("providerId") or _first(request_payload, "providerId", "provider_id", default="oauth-provider")),
            "correlationId": _first(request_payload, "correlationId", "correlation_id"),
            "gateStatus": "passed",
            "tokenEndpointCalled": True,
            "tokenResponsePresent": True,
            "accessTokenPresent": access_token_present,
            "refreshTokenPresent": refresh_token_present,
            "idTokenPresent": id_token_present,
            "rawTokenStored": False,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "accountLinkCreated": account_link_created,
            "accountLinkMetadataOnly": True,
            "accountLink": account_link,
            "gateSummary": gate_summary,
            "redactionSummary": redaction_summary,
            "reasonCodes": reason_codes,
            "warnings": [
                "Live exchange pilot completed with token values redacted and discarded in memory.",
                "OAuth account link output is metadata-only and does not grant provider execution or risk acceptance.",
            ],
        }
    ).model_dump(mode="json")
    if audit:
        _audit_live_pilot(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
    return result


def _base_real_smoke_result(
    payload: dict[str, Any],
    *,
    smoke_status: str,
    reason_codes: list[str],
    warnings: list[str] | None = None,
    evidence_replay_status: str = "failed",
    pilot_gate_status: str = "blocked",
    external_auth_call_performed: bool = False,
    metadata_link_created: bool = False,
    account_link: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return OAuthRealProviderSmokeResultResponse.model_validate(
        {
            "smokeStatus": smoke_status,
            "providerId": str(_first(payload, "providerId", "provider_id", default="oauth-provider")),
            "operatorId": _first(payload, "operatorId", "operator_id"),
            "explicitOperatorAcceptance": _bool_payload(payload, "explicitOperatorAcceptance", "explicit_operator_acceptance"),
            "liveExchangeRequested": _bool_payload(payload, "liveExchangeRequested", "live_exchange_requested"),
            "tokenStorageRequested": False,
            "externalAuthCallPerformed": external_auth_call_performed,
            "metadataLinkCreated": metadata_link_created,
            "rawTokenStored": False,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "dashboardTriggered": False,
            "accountLinkMetadataOnly": True,
            "accountLink": account_link,
            "evidenceReplayStatus": evidence_replay_status,
            "pilotGateStatus": pilot_gate_status,
            "reasonCodes": _unique(reason_codes),
            "warnings": warnings
            or [
                "Optional real OAuth provider smoke is fail-closed and never stores, returns, audits, or displays token values.",
                "Dashboard remains read-only and cannot trigger real provider smoke.",
            ],
        }
    ).model_dump(mode="json")


def _audit_real_provider_smoke(
    result: dict[str, Any],
    *,
    operator_id: str,
    auth_mode: str,
    capability_present: bool | None,
) -> None:
    operator_audit.record_event(
        action_type="oauth_real_provider_smoke",
        decision="allowed" if result.get("smokeStatus") == "completed_redacted" else "rejected",
        operator_id=operator_id,
        auth_mode=auth_mode,
        capability_required="edit_topic_config",
        capability_present=capability_present,
        reason_codes=list(result.get("reasonCodes") or []),
        target_type="oauth_provider",
        target_id=result.get("providerId"),
        risk_level="oauth_live_pilot",
        metadata={
            "providerId": result.get("providerId"),
            "smokeStatus": result.get("smokeStatus"),
            "evidenceReplayStatus": result.get("evidenceReplayStatus"),
            "pilotGateStatus": result.get("pilotGateStatus"),
            "externalAuthCallPerformed": bool(result.get("externalAuthCallPerformed")),
            "metadataLinkCreated": bool(result.get("metadataLinkCreated")),
            "rawTokenStored": False,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "accountLinkMetadataOnly": True,
            "accountLinkId": (result.get("accountLink") or {}).get("accountLinkId") if result.get("accountLink") else None,
        },
    )


def perform_real_provider_smoke(
    payload: dict[str, Any],
    *,
    http_client: Any | None = None,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    from app.services import oauth_live_evidence

    request_payload = dict(payload or {})
    metadata_payload = _request_metadata(request_payload)
    reason_codes = [
        "real_provider_smoke_optional",
        "token_storage_disabled",
        "dashboard_execution_disabled",
    ]

    if not _bool_payload(request_payload, "testProviderConfigured", "test_provider_configured") or not metadata_payload.get("tokenUrl"):
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="skipped",
            reason_codes=[*reason_codes, "real_provider_smoke_not_configured"],
            warnings=["No explicit test OAuth provider configuration was supplied; real provider smoke was skipped."],
            evidence_replay_status="warnings",
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    if _bool_payload(request_payload, "dashboardTriggered", "dashboard_triggered", "dashboardExecutionContext", "dashboard_execution_context"):
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="blocked",
            reason_codes=[*reason_codes, "dashboard_triggered_forbidden"],
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    if _bool_payload(request_payload, "tokenStorageRequested", "token_storage_requested"):
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="blocked",
            reason_codes=[*reason_codes, "token_storage_requested"],
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    if not _bool_payload(request_payload, "explicitOperatorAcceptance", "explicit_operator_acceptance") or not _bool_payload(
        request_payload, "operatorAcceptancePresent", "operator_acceptance_present"
    ):
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="blocked",
            reason_codes=[*reason_codes, "missing_explicit_operator_acceptance"],
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    if not _bool_payload(request_payload, "liveExchangeRequested", "live_exchange_requested"):
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="blocked",
            reason_codes=[*reason_codes, "missing_live_exchange_request"],
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    evidence_package = _first(request_payload, "evidencePackage", "evidence_package", default={})
    evidence_replay = oauth_live_evidence.verify_live_pilot_evidence_package(evidence_package if isinstance(evidence_package, dict) else {})
    evidence_replay_status = str(evidence_replay.get("verificationStatus") or "failed")
    if evidence_replay_status != "valid":
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="blocked",
            reason_codes=[*reason_codes, "evidence_replay_not_valid", *list(evidence_replay.get("reasonCodes") or [])],
            evidence_replay_status=evidence_replay_status,
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    if _bool_payload(request_payload, "dryRunOnly", "dry_run_only", default=True):
        result = _base_real_smoke_result(
            request_payload,
            smoke_status="dry_run_only",
            reason_codes=[*reason_codes, "dry_run_only"],
            evidence_replay_status=evidence_replay_status,
            pilot_gate_status="dry_run_only",
            warnings=["Real provider smoke dry-run completed without calling the token endpoint."],
        )
        if audit:
            _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
        return result

    pilot_payload = {
        **request_payload,
        "operatorAcceptancePresent": True,
        "liveExchangeRequested": True,
        "tokenStorageRequested": False,
        "dashboardExecutionContext": False,
        "dryRunOnly": False,
    }
    pilot_result = perform_live_exchange_pilot(
        pilot_payload,
        http_client=http_client,
        audit=audit,
        operator_id=operator_id,
        auth_mode=auth_mode,
        capability_present=capability_present,
        operator_identity=operator_identity,
        now=now,
    )
    smoke_status = "completed_redacted" if pilot_result.get("liveExchangePerformed") else "failed"
    result = _base_real_smoke_result(
        request_payload,
        smoke_status=smoke_status,
        reason_codes=[
            *reason_codes,
            "real_provider_smoke_completed_redacted" if smoke_status == "completed_redacted" else "real_provider_smoke_failed",
            *list(pilot_result.get("reasonCodes") or []),
        ],
        evidence_replay_status=evidence_replay_status,
        pilot_gate_status=str(pilot_result.get("gateStatus") or "failed"),
        external_auth_call_performed=bool(pilot_result.get("tokenEndpointCalled")),
        metadata_link_created=bool(pilot_result.get("accountLinkCreated")),
        account_link=pilot_result.get("accountLink"),
        warnings=[
            "Optional real OAuth provider smoke result is redacted; token values and authorization code were discarded.",
            "Metadata-only account link does not grant provider execution or risk acceptance.",
        ],
    )
    result_scan = scan_oauth_live_pilot_leaks(result)
    if result_scan.get("leakDetected"):
        result["smokeStatus"] = "failed"
        result["reasonCodes"] = _unique([*result.get("reasonCodes", []), "real_smoke_result_leak_detected", *list(result_scan.get("reasonCodes") or [])])
    if audit:
        _audit_real_provider_smoke(result, operator_id=operator_id, auth_mode=auth_mode, capability_present=capability_present)
    return OAuthRealProviderSmokeResultResponse.model_validate(result).model_dump(mode="json")

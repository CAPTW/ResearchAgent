from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc
from app.repositories.jsonl import (
    JsonlPolicy,
    inspect_jsonl,
    read_jsonl_strict,
    replace_jsonl,
)
from app.schemas.auth import (
    OAuthAccountLinkHealthResponse,
    OAuthAccountLinkMetadataResponse,
    OAuthFlowTimelineResponse,
    OAuthSessionBindingResponse,
    OAuthSessionCapabilityPolicyResponse,
)
from app.services import operator_audit

ACCOUNT_LINK_STORE_PATH = Path("output") / "oauth-account-links" / "account-links.jsonl"

TOKEN_LIKE_KEY_PARTS = (
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "authorization_code",
    "authorizationcode",
    "client_secret",
    "clientsecret",
    "api_key",
    "apikey",
    "credential",
    "password",
    "private_key",
    "bearer",
)
RAW_SUBJECT_KEYS = {"subject", "sub", "raw_subject", "rawsubject"}
TOKEN_VALUE_PREFIXES = ("ya29.", "sk-", "ghp_", "xoxb-", "xoxp-", "Bearer ")
UNSAFE_ENABLED_FLAG_KEYS = {
    "tokenstorageenabled",
    "token_storage_enabled",
    "livetokenexchangeperformed",
    "live_token_exchange_performed",
}
ACCOUNT_LINK_HEALTH_ERROR_REASONS = {
    "token_like_account_link_field",
    "token_storage_enabled_violation",
    "live_exchange_performed_violation",
    "unsafe_account_link_flag_rejected",
    "invalid_account_link_schema",
    "missing_provider_id",
    "missing_operator_id",
    "missing_subject_hash",
}
ACCOUNT_LINK_POLICY_DENIALS = (
    "run_real_provider_cli",
    "accept_provider_risk",
    "provider_live_execution",
    "oauth_live_token_exchange",
)


def _normalized_key(value: str) -> str:
    return value.replace("-", "_").replace(".", "_").lower()


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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_account_link_id(provider_id: str, operator_id: str, subject_hash: str | None) -> str:
    seed = f"{provider_id}:{operator_id}:{subject_hash or 'missing-subject-hash'}"
    return f"oauth-link-{_sha256(seed)[:16]}"


def _looks_like_token_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if any(text.startswith(prefix) for prefix in TOKEN_VALUE_PREFIXES):
        return True
    return bool(re.fullmatch(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}", text))


def _forbidden_payload_paths(value: Any, *, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            normalized = _normalized_key(key).replace("_", "")
            if normalized in RAW_SUBJECT_KEYS or any(part.replace("_", "") in normalized for part in TOKEN_LIKE_KEY_PARTS):
                paths.append(path)
            if _looks_like_token_value(child):
                paths.append(path)
            paths.extend(_forbidden_payload_paths(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_forbidden_payload_paths(child, prefix=f"{prefix}[{index}]"))
    return list(dict.fromkeys(paths))


def _unsafe_enabled_flag_paths(value: Any, *, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            normalized = _normalized_key(key)
            compact = normalized.replace("_", "")
            if (normalized in UNSAFE_ENABLED_FLAG_KEYS or compact in UNSAFE_ENABLED_FLAG_KEYS) and child is True:
                paths.append(path)
            paths.extend(_unsafe_enabled_flag_paths(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_unsafe_enabled_flag_paths(child, prefix=f"{prefix}[{index}]"))
    return list(dict.fromkeys(paths))


def _payload_policy_violations(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    token_like_paths = _forbidden_payload_paths(payload)
    unsafe_flag_paths = _unsafe_enabled_flag_paths(payload)
    reason_codes: list[str] = []
    if token_like_paths:
        reason_codes.append("token_like_field_rejected")
    if unsafe_flag_paths:
        reason_codes.append("unsafe_account_link_flag_rejected")
    return list(dict.fromkeys([*token_like_paths, *unsafe_flag_paths])), reason_codes


def _safe_rejection_metadata(value: Any, *, key: str = "") -> Any:
    normalized = _normalized_key(key).replace("_", "")
    if normalized in RAW_SUBJECT_KEYS:
        return "<redacted>"
    if normalized in {"displaysubjecthint", "display_subject_hint"}:
        return _redact_subject_hint(value)
    if any(part.replace("_", "") in normalized for part in TOKEN_LIKE_KEY_PARTS) or _looks_like_token_value(value):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _safe_rejection_metadata(child_value, key=str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_safe_rejection_metadata(item, key=key) for item in value]
    return value


def _redact_subject_hint(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "@" in text:
        local, domain = text.split("@", 1)
        local_hint = local[:1] if local else "*"
        return f"{local_hint}***@{domain}"
    if len(text) <= 4:
        return "***"
    return f"{text[:4]}...<redacted>"


def _metadata_from_payload(
    payload: dict[str, Any],
    *,
    status: str,
    link_stored: bool,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or iso_now_utc()
    provider_id = str(payload.get("providerId") or payload.get("provider_id") or "oauth-provider").strip().lower()
    operator_id = str(payload.get("operatorId") or payload.get("operator_id") or "anonymous").strip() or "anonymous"
    subject_hash = payload.get("subjectHash") or payload.get("subject_hash")
    subject_hash_text = str(subject_hash).strip() if subject_hash else None
    source = str(payload.get("source") or "mock_exchange_validation")
    scopes = _string_list(payload.get("scopes"))
    reason_codes = _string_list(payload.get("reasonCodes") or payload.get("reason_codes"))
    warnings = _string_list(payload.get("warnings"))
    if "token_storage_disabled" not in reason_codes:
        reason_codes.append("token_storage_disabled")
    if source == "future_live_exchange":
        if "live_exchange_pilot_metadata_only" not in reason_codes:
            reason_codes.append("live_exchange_pilot_metadata_only")
    elif "live_token_exchange_disabled" not in reason_codes:
        reason_codes.append("live_token_exchange_disabled")
    if not warnings:
        warnings.append("OAuth account links persist metadata only; tokens, codes, and provider credentials are not stored.")
    metadata = {
        "accountLinkId": payload.get("accountLinkId")
        or payload.get("account_link_id")
        or _stable_account_link_id(provider_id, operator_id, subject_hash_text),
        "providerId": provider_id,
        "operatorId": operator_id,
        "subjectHash": subject_hash_text,
        "displaySubjectHint": _redact_subject_hint(payload.get("displaySubjectHint") or payload.get("display_subject_hint")),
        "scopes": scopes,
        "linkedAt": payload.get("linkedAt") or payload.get("linked_at") or (timestamp if link_stored else None),
        "lastValidatedAt": payload.get("lastValidatedAt") or payload.get("last_validated_at") or timestamp,
        "tokenStorageEnabled": False,
        "liveTokenExchangePerformed": False,
        "source": source if source in {"mock_exchange_validation", "dry_run_preview", "future_live_exchange"} else "mock_exchange_validation",
        "status": status,
        "reasonCodes": reason_codes,
        "warnings": warnings,
    }
    return OAuthAccountLinkMetadataResponse.model_validate(metadata).model_dump(mode="json")


def _read_records() -> list[dict[str, Any]]:
    return read_jsonl_strict(
        ACCOUNT_LINK_STORE_PATH,
        policy=JsonlPolicy(
            artifact_role="oauth_account_links",
            record_validator=_validate_authoritative_account_link_record,
        ),
    )


def _write_records(records: list[dict[str, Any]]) -> None:
    replace_jsonl(
        ACCOUNT_LINK_STORE_PATH,
        records,
        policy=JsonlPolicy(
            artifact_role="oauth_account_links",
            record_validator=_validate_authoritative_account_link_record,
        ),
    )


def build_account_link_preview(
    payload: dict[str, Any],
    *,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    link_payload = dict(payload)
    if operator_identity:
        link_payload["operatorId"] = operator_identity.get("operatorId")
    forbidden_paths, violation_reason_codes = _payload_policy_violations(link_payload)
    status = "invalid" if forbidden_paths else "preview"
    reason_codes = violation_reason_codes or ["preview_only", "token_storage_disabled"]
    account_link = _metadata_from_payload({**link_payload, "reasonCodes": reason_codes}, status=status, link_stored=False, now=now)
    return {
        "accepted": not forbidden_paths,
        "validationStatus": "invalid" if forbidden_paths else "preview",
        "accountLink": account_link,
        "health": build_account_link_health_summary(),
        "sessionBinding": build_session_binding_preview(operator_identity or {"operatorId": account_link["operatorId"], "role": "local_operator", "capabilities": []}),
        "linkStored": False,
        "tokenStorageEnabled": False,
        "liveTokenExchangePerformed": False,
        "externalAuthCallPerformed": False,
        "tokenRevocationPerformed": False,
        "reasonCodes": reason_codes,
        "warnings": account_link["warnings"],
    }


def _audit_secret_policy_rejection(
    *,
    payload: dict[str, Any],
    rejected_fields: list[str],
    reason_codes: list[str],
    operator_id: str,
    auth_mode: str,
    capability_present: bool | None = True,
) -> None:
    provider_id = str(payload.get("providerId") or payload.get("provider_id") or "oauth-provider")
    operator_audit.record_event(
        action_type="secret_policy_rejection",
        decision="rejected",
        operator_id=operator_id,
        auth_mode=auth_mode,
        capability_required="view_dashboard",
        capability_present=capability_present,
        reason_codes=reason_codes,
        target_type="oauth_account_link",
        target_id=provider_id,
        risk_level="secret_policy",
        metadata={
            "providerId": provider_id,
            "rejectedFields": rejected_fields,
            "metadata": _safe_rejection_metadata(payload),
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
        },
    )


def save_mock_account_link(
    payload: dict[str, Any],
    *,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    link_payload = dict(payload)
    if operator_identity:
        link_payload["operatorId"] = operator_identity.get("operatorId")
    link_payload["source"] = "mock_exchange_validation"
    forbidden_paths, violation_reason_codes = _payload_policy_violations(link_payload)
    if forbidden_paths:
        if audit:
            _audit_secret_policy_rejection(
                payload=link_payload,
                rejected_fields=forbidden_paths,
                reason_codes=violation_reason_codes,
                operator_id=operator_id,
                auth_mode=auth_mode,
                capability_present=capability_present,
            )
        invalid_link = _metadata_from_payload(
            {**link_payload, "reasonCodes": violation_reason_codes},
            status="invalid",
            link_stored=False,
            now=now,
        )
        return {
            "accepted": False,
            "validationStatus": "invalid",
            "accountLink": invalid_link,
            "health": build_account_link_health_summary(),
            "sessionBinding": build_session_binding_preview(
                operator_identity or {"operatorId": invalid_link["operatorId"], "role": "local_operator", "capabilities": []}
            ),
            "linkStored": False,
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "externalAuthCallPerformed": False,
            "tokenRevocationPerformed": False,
            "reasonCodes": violation_reason_codes,
            "warnings": ["Unsafe account link input was rejected; no account link metadata was stored."],
        }

    account_link = _metadata_from_payload(link_payload, status="linked_mock", link_stored=True, now=now)
    records = _read_records()
    records = [
        record
        for record in records
        if str(record.get("accountLinkId") or "") != account_link["accountLinkId"]
        and not (
            record.get("providerId") == account_link["providerId"]
            and record.get("operatorId") == account_link["operatorId"]
            and record.get("subjectHash") == account_link["subjectHash"]
        )
    ]
    records.append(account_link)
    _write_records(records)
    if audit:
        operator_audit.record_event(
            action_type="oauth_account_link_save_mock",
            decision="allowed",
            operator_id=operator_id,
            auth_mode=auth_mode,
            capability_required="edit_topic_config",
            capability_present=capability_present,
            reason_codes=[],
            target_type="oauth_account_link",
            target_id=account_link["accountLinkId"],
            risk_level="config_write",
            metadata={
                "accountLinkId": account_link["accountLinkId"],
                "providerId": account_link["providerId"],
                "operatorId": account_link["operatorId"],
                "subjectHash": account_link["subjectHash"],
                "scopeCount": len(account_link["scopes"]),
                "tokenStorageEnabled": False,
                "liveTokenExchangePerformed": False,
                "externalAuthCallPerformed": False,
            },
        )
    return {
        "accepted": True,
        "validationStatus": "saved",
        "accountLink": account_link,
        "health": build_account_link_health_summary(),
        "sessionBinding": build_session_binding_preview(
            operator_identity or {"operatorId": account_link["operatorId"], "role": "local_operator", "capabilities": []}
        ),
        "linkStored": True,
        "tokenStorageEnabled": False,
        "liveTokenExchangePerformed": False,
        "externalAuthCallPerformed": False,
        "tokenRevocationPerformed": False,
        "reasonCodes": [],
        "warnings": account_link["warnings"],
    }


def save_future_live_account_link_metadata(
    payload: dict[str, Any],
    *,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    link_payload = dict(payload)
    if operator_identity:
        link_payload["operatorId"] = operator_identity.get("operatorId")
    link_payload["source"] = "future_live_exchange"
    forbidden_paths, violation_reason_codes = _payload_policy_violations(link_payload)
    if forbidden_paths:
        invalid_link = _metadata_from_payload(
            {**link_payload, "reasonCodes": violation_reason_codes},
            status="invalid",
            link_stored=False,
            now=now,
        )
        return {
            "accepted": False,
            "validationStatus": "invalid",
            "accountLink": invalid_link,
            "health": build_account_link_health_summary(),
            "sessionBinding": build_session_binding_preview(
                operator_identity or {"operatorId": invalid_link["operatorId"], "role": "local_operator", "capabilities": []}
            ),
            "linkStored": False,
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "externalAuthCallPerformed": False,
            "tokenRevocationPerformed": False,
            "reasonCodes": violation_reason_codes,
            "warnings": ["Unsafe account link input was rejected; no account link metadata was stored."],
        }

    account_link = _metadata_from_payload(
        {
            **link_payload,
            "reasonCodes": _unique_reasons(link_payload.get("reasonCodes"), ["token_storage_disabled", "live_exchange_pilot_metadata_only"]),
            "warnings": _unique_reasons(
                link_payload.get("warnings"),
                ["Live exchange pilot account links persist metadata only; token values are discarded before storage."],
            ),
        },
        status="linked_live_pilot",
        link_stored=True,
        now=now,
    )
    records = _read_records()
    records = [
        record
        for record in records
        if str(record.get("accountLinkId") or "") != account_link["accountLinkId"]
        and not (
            record.get("providerId") == account_link["providerId"]
            and record.get("operatorId") == account_link["operatorId"]
            and record.get("subjectHash") == account_link["subjectHash"]
        )
    ]
    records.append(account_link)
    _write_records(records)
    return {
        "accepted": True,
        "validationStatus": "saved",
        "accountLink": account_link,
        "health": build_account_link_health_summary(),
        "sessionBinding": build_session_binding_preview(
            operator_identity or {"operatorId": account_link["operatorId"], "role": "local_operator", "capabilities": []}
        ),
        "linkStored": True,
        "tokenStorageEnabled": False,
        "liveTokenExchangePerformed": False,
        "externalAuthCallPerformed": False,
        "tokenRevocationPerformed": False,
        "reasonCodes": [],
        "warnings": account_link["warnings"],
    }


def _unique_reasons(value: Any, extra: list[str]) -> list[str]:
    return list(dict.fromkeys([*_string_list(value), *extra]))


def _account_link_record_validation(record: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], bool, bool]:
    reason_codes: list[str] = []
    token_like_paths = _forbidden_payload_paths(record)
    unsafe_flag_paths = _unsafe_enabled_flag_paths(record)
    token_leak = bool(token_like_paths)
    live_exchange_violation = bool(record.get("liveTokenExchangePerformed") is True or record.get("live_token_exchange_performed") is True)
    token_storage_violation = bool(record.get("tokenStorageEnabled") is True or record.get("token_storage_enabled") is True)

    if token_like_paths:
        reason_codes.append("token_like_account_link_field")
    if token_storage_violation:
        reason_codes.append("token_storage_enabled_violation")
    if live_exchange_violation:
        reason_codes.append("live_exchange_performed_violation")
    if unsafe_flag_paths:
        reason_codes.append("unsafe_account_link_flag_rejected")

    try:
        metadata = OAuthAccountLinkMetadataResponse.model_validate(record).model_dump(mode="json")
    except ValueError:
        reason_codes.append("invalid_account_link_schema")
        return None, list(dict.fromkeys(reason_codes)), token_leak, live_exchange_violation

    if not str(metadata.get("providerId") or "").strip():
        reason_codes.append("missing_provider_id")
    if not str(metadata.get("operatorId") or "").strip():
        reason_codes.append("missing_operator_id")
    if metadata.get("status") in {"linked_mock", "linked_live_pilot", "revoked_placeholder"} and not str(metadata.get("subjectHash") or "").strip():
        reason_codes.append("missing_subject_hash")
    return metadata, list(dict.fromkeys(reason_codes)), token_leak, live_exchange_violation


def _has_health_error(reason_codes: list[str]) -> bool:
    return any(reason in ACCOUNT_LINK_HEALTH_ERROR_REASONS for reason in reason_codes)


def _validate_authoritative_account_link_record(record: dict[str, Any]) -> None:
    metadata, reason_codes, _token_leak, _live_violation = _account_link_record_validation(record)
    if metadata is None or _has_health_error(reason_codes):
        raise ValueError("oauth_account_link_schema_invalid")


def _active_account_links(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        metadata, reason_codes, _token_leak, _live_violation = _account_link_record_validation(record)
        if metadata is None or _has_health_error(reason_codes) or metadata.get("status") not in {"linked_mock", "linked_live_pilot"}:
            continue
        key = (str(metadata.get("providerId") or ""), str(metadata.get("operatorId") or ""), str(metadata.get("subjectHash") or ""))
        if key in seen:
            continue
        seen.add(key)
        active.append(metadata)
    return active


def list_account_links() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for record in _read_records():
        metadata, reason_codes, _token_leak, _live_violation = _account_link_record_validation(record)
        if metadata is None or _has_health_error(reason_codes):
            continue
        items.append(metadata)
    items.sort(key=lambda item: (str(item.get("providerId") or ""), str(item.get("accountLinkId") or "")))
    return {"items": items, "health": build_account_link_health_summary()}


def build_account_link_health_summary(*, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    integrity_issue_count = 0
    integrity_issue_codes: list[str] = []
    if items is not None:
        records = items
    elif ACCOUNT_LINK_STORE_PATH.exists():
        inspection = inspect_jsonl(
            ACCOUNT_LINK_STORE_PATH,
            policy=JsonlPolicy(artifact_role="oauth_account_links_health"),
        )
        records = list(inspection.records)
        integrity_issue_count = inspection.issue_count
        integrity_issue_codes = [
            issue.code
            for issue in inspection.issues
        ]
    else:
        records = []
    invalid_count = 0
    revoked_count = 0
    token_leak_count = 0
    live_exchange_violation_count = 0
    duplicate_count = 0
    reason_codes = [
        "token_storage_disabled",
        *integrity_issue_codes,
    ]
    if integrity_issue_count:
        reason_codes.append("jsonl_repair_required")
        invalid_count += integrity_issue_count
    active_items: list[dict[str, Any]] = []
    seen_active: set[tuple[str, str, str]] = set()

    for record in records:
        metadata, record_reasons, token_leak, live_violation = _account_link_record_validation(record)
        reason_codes.extend(record_reasons)
        if token_leak:
            token_leak_count += 1
        if live_violation:
            live_exchange_violation_count += 1
        if metadata is None or _has_health_error(record_reasons):
            invalid_count += 1
            continue
        if metadata.get("status") == "revoked_placeholder":
            revoked_count += 1
            reason_codes.append("revoked_placeholder_present")
            continue
        if metadata.get("status") in {"linked_mock", "linked_live_pilot"}:
            key = (str(metadata.get("providerId") or ""), str(metadata.get("operatorId") or ""), str(metadata.get("subjectHash") or ""))
            if key in seen_active:
                duplicate_count += 1
                reason_codes.append("duplicate_account_link")
                continue
            seen_active.add(key)
            active_items.append(metadata)

    latest_linked_at = max((str(item.get("linkedAt") or "") for item in active_items), default="") or None
    mock_count = sum(1 for item in active_items if item.get("source") == "mock_exchange_validation")
    live_count = sum(1 for item in active_items if item.get("source") == "future_live_exchange")
    if live_count == 0:
        reason_codes.append("live_link_count_zero")
    if integrity_issue_count:
        health_status = "errors"
    elif not records:
        health_status = "empty"
    elif invalid_count or token_leak_count or live_exchange_violation_count:
        health_status = "errors"
    elif revoked_count or duplicate_count:
        health_status = "warnings"
    else:
        health_status = "valid"
    if live_count:
        status = "live_pilot_linked"
        reason_codes.append("live_pilot_account_link_present")
    elif mock_count:
        status = "mock_linked"
        reason_codes.append("mock_account_link_present")
    elif health_status == "errors":
        status = "invalid"
    elif revoked_count:
        status = "disabled"
    else:
        status = "none"
        reason_codes.append("no_account_links")

    warnings = [
        "OAuth account links are metadata-only; tokens, codes, and provider credentials are not persisted.",
        "Live account links remain disabled.",
    ]
    if health_status == "errors":
        warnings.append("Account link health found unsafe or invalid metadata; invalid links are ignored for session binding.")
    elif health_status == "warnings":
        warnings.append("Account link health found inactive or duplicate metadata; only deterministic active links affect display status.")

    return OAuthAccountLinkHealthResponse.model_validate(
        {
            "status": status,
            "linkedAccountCount": len(active_items),
            "activeLinkCount": len(active_items),
            "mockLinkCount": mock_count,
            "liveLinkCount": live_count,
            "invalidLinkCount": invalid_count,
            "revokedPlaceholderCount": revoked_count,
            "tokenLeakViolationCount": token_leak_count,
            "liveExchangeViolationCount": live_exchange_violation_count,
            "duplicateLinkCount": duplicate_count,
            "healthStatus": health_status,
            "tokenStorageEnabled": False,
            "latestLinkedAt": latest_linked_at,
            "reasonCodes": list(dict.fromkeys(reason_codes)),
            "warnings": warnings,
        }
    ).model_dump(mode="json")


def build_session_binding_preview(operator_identity: dict[str, Any], *, account_links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    operator_id = str(operator_identity.get("operatorId") or "anonymous")
    links = account_links if account_links is not None else list_account_links()["items"]
    linked = [item for item in _active_account_links(links) if item.get("operatorId") == operator_id]
    provider_ids = sorted({str(item.get("providerId")) for item in linked if item.get("providerId")})
    binding_status = "local_mock_bound" if linked else "unbound"
    reason_codes = ["metadata_only_binding", "provider_acceptance_gate_preserved"]
    if not linked:
        reason_codes.append("no_account_links")
    return OAuthSessionBindingResponse.model_validate(
        {
            "bindingStatus": binding_status,
            "operatorId": operator_id,
            "role": str(operator_identity.get("role") or "local_operator"),
            "providerIds": provider_ids,
            "accountLinkCount": len(linked),
            "capabilities": list(operator_identity.get("capabilities") or []),
            "accountLinkGrantsProviderExecution": False,
            "providerExecution": "cli_only_acceptance_gated",
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "reasonCodes": reason_codes,
            "warnings": [
                "Session binding is local/mock metadata only.",
                "Account links do not grant live provider execution or bypass provider acceptance gates.",
            ],
        }
    ).model_dump(mode="json")


def build_session_capability_policy(operator_identity: dict[str, Any], *, account_links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    role = str(operator_identity.get("role") or "local_operator")
    capabilities = list(dict.fromkeys(str(capability) for capability in (operator_identity.get("capabilities") or [])))
    links = account_links if account_links is not None else list_account_links()["items"]
    operator_id = str(operator_identity.get("operatorId") or "anonymous")
    linked = [item for item in _active_account_links(links) if item.get("operatorId") == operator_id]
    account_link_influence = "display_only" if linked else "none"
    capability_sources = {capability: [f"operator_role:{role}"] for capability in capabilities}
    reason_codes = ["operator_role_capabilities", "provider_acceptance_gate_preserved", "live_token_exchange_disabled"]
    reason_codes.append("account_link_display_only" if linked else "no_account_links")
    return OAuthSessionCapabilityPolicyResponse.model_validate(
        {
            "effectiveCapabilities": capabilities,
            "capabilitySources": capability_sources,
            "deniedByPolicy": list(ACCOUNT_LINK_POLICY_DENIALS),
            "accountLinkInfluence": account_link_influence,
            "accountLinkCount": len(linked),
            "accountLinkGrantsProviderExecution": False,
            "providerExecution": "cli_only_acceptance_gated",
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "reasonCodes": reason_codes,
            "warnings": [
                "Account links only affect display/readiness status.",
                "Operator role capabilities remain the source of action permissions.",
                "Provider live execution still requires CLI flags and provider risk acceptance.",
            ],
        }
    ).model_dump(mode="json")


def build_oauth_flow_timeline() -> dict[str, Any]:
    health = build_account_link_health_summary()
    status = "mock_linked" if health.get("activeLinkCount") else "metadata_only"
    steps = [
        {
            "stepId": "metadata_configured",
            "label": "Metadata configured",
            "status": "available",
            "reasonCodes": ["metadata_only"],
        },
        {
            "stepId": "authorization_url_dry_run",
            "label": "Auth URL dry-run",
            "status": "available",
            "reasonCodes": ["dry_run_only"],
        },
        {
            "stepId": "callback_dry_run",
            "label": "Callback dry-run",
            "status": "available",
            "reasonCodes": ["dry_run_only"],
        },
        {
            "stepId": "mock_token_validation",
            "label": "Mock token validation",
            "status": "mock_only",
            "reasonCodes": ["mock_validation_only"],
        },
        {
            "stepId": "metadata_only_account_link",
            "label": "Metadata-only link",
            "status": "available" if health.get("activeLinkCount") else "metadata_only",
            "reasonCodes": ["metadata_only_binding"],
        },
        {
            "stepId": "real_token_exchange_disabled",
            "label": "Real token exchange disabled",
            "status": "disabled",
            "reasonCodes": ["live_token_exchange_disabled"],
        },
    ]
    return OAuthFlowTimelineResponse.model_validate(
        {
            "status": status,
            "tokenStorageEnabled": False,
            "realTokenExchangeEnabled": False,
            "externalAuthCallsEnabled": False,
            "steps": steps,
            "reasonCodes": ["token_storage_disabled", "live_token_exchange_disabled", "external_auth_calls_disabled"],
            "warnings": ["OAuth flow remains local/mock only; real token exchange and token persistence are disabled."],
        }
    ).model_dump(mode="json")


def revoke_account_link_placeholder(
    account_link_id: str,
    *,
    audit: bool = False,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_present: bool | None = True,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or iso_now_utc()
    records = _read_records()
    updated_records: list[dict[str, Any]] = []
    revoked_link: dict[str, Any] | None = None
    target_id = str(account_link_id or "")
    for record in records:
        metadata, reason_codes, _token_leak, _live_violation = _account_link_record_validation(record)
        if metadata is None or _has_health_error(reason_codes) or str(metadata.get("accountLinkId") or "") != target_id:
            updated_records.append(record)
            continue
        updated_reason_codes = _string_list(metadata.get("reasonCodes"))
        for reason in ("revoked_placeholder", "token_storage_disabled", "live_token_exchange_disabled", "no_external_revocation_call"):
            if reason not in updated_reason_codes:
                updated_reason_codes.append(reason)
        updated_warnings = _string_list(metadata.get("warnings"))
        if "Revocation is a local metadata placeholder only; no external provider revocation call is made." not in updated_warnings:
            updated_warnings.append("Revocation is a local metadata placeholder only; no external provider revocation call is made.")
        revoked_link = OAuthAccountLinkMetadataResponse.model_validate(
            {
                **metadata,
                "status": "revoked_placeholder",
                "lastValidatedAt": timestamp,
                "tokenStorageEnabled": False,
                "liveTokenExchangePerformed": False,
                "reasonCodes": updated_reason_codes,
                "warnings": updated_warnings,
            }
        ).model_dump(mode="json")
        updated_records.append(revoked_link)

    if revoked_link is None:
        if audit:
            operator_audit.record_event(
                action_type="oauth_account_link_revoke_placeholder",
                decision="rejected",
                operator_id=operator_id,
                auth_mode=auth_mode,
                capability_required="edit_topic_config",
                capability_present=capability_present,
                reason_codes=["account_link_not_found"],
                target_type="oauth_account_link",
                target_id=target_id,
                risk_level="config_write",
                metadata={
                    "accountLinkId": target_id,
                    "externalAuthCallPerformed": False,
                    "tokenRevocationPerformed": False,
                    "tokenStorageEnabled": False,
                    "liveTokenExchangePerformed": False,
                },
            )
        identity = operator_identity or {"operatorId": operator_id or "anonymous", "role": "local_operator", "capabilities": []}
        return {
            "accepted": False,
            "validationStatus": "invalid",
            "accountLink": None,
            "health": build_account_link_health_summary(),
            "sessionBinding": build_session_binding_preview(identity),
            "linkStored": False,
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "externalAuthCallPerformed": False,
            "tokenRevocationPerformed": False,
            "reasonCodes": ["account_link_not_found"],
            "warnings": ["Account link was not found; no metadata was changed."],
        }

    _write_records(updated_records)
    if audit:
        operator_audit.record_event(
            action_type="oauth_account_link_revoke_placeholder",
            decision="allowed",
            operator_id=operator_id,
            auth_mode=auth_mode,
            capability_required="edit_topic_config",
            capability_present=capability_present,
            reason_codes=["revoked_placeholder", "no_external_revocation_call"],
            target_type="oauth_account_link",
            target_id=revoked_link["accountLinkId"],
            risk_level="config_write",
            metadata={
                "accountLinkId": revoked_link["accountLinkId"],
                "providerId": revoked_link["providerId"],
                "operatorId": revoked_link["operatorId"],
                "externalAuthCallPerformed": False,
                "tokenRevocationPerformed": False,
                "tokenStorageEnabled": False,
                "liveTokenExchangePerformed": False,
            },
        )
    identity = operator_identity or {"operatorId": revoked_link["operatorId"], "role": "local_operator", "capabilities": []}
    return {
        "accepted": True,
        "validationStatus": "saved",
        "accountLink": revoked_link,
        "health": build_account_link_health_summary(),
        "sessionBinding": build_session_binding_preview(identity),
        "linkStored": True,
        "tokenStorageEnabled": False,
        "liveTokenExchangePerformed": False,
        "externalAuthCallPerformed": False,
        "tokenRevocationPerformed": False,
        "reasonCodes": ["revoked_placeholder", "no_external_revocation_call"],
        "warnings": revoked_link["warnings"],
    }

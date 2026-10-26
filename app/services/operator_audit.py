from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc
from app.repositories.jsonl import (
    JsonlPolicy,
    append_jsonl_record,
    inspect_jsonl,
    read_jsonl_strict,
)
from app.schemas.operator_audit import (
    OperatorAuditEventResponse,
    OperatorAuditReplaySummaryResponse,
    OperatorAuditSummaryResponse,
)
from app.services import action_capabilities

OPERATOR_AUDIT_PATH_ENV = "XRML_OPERATOR_AUDIT_PATH"
_DEFAULT_AUDIT_LOG_PATH = Path("output") / "operator-audit" / "operator-audit.jsonl"
# Production default. Kept as a module attribute so existing tests that monkeypatch it
# (an explicit in-process override) keep working; see _audit_log_path().
AUDIT_LOG_PATH = _DEFAULT_AUDIT_LOG_PATH


def _validate_audit_event_record(record: dict[str, Any]) -> None:
    OperatorAuditEventResponse.model_validate(record)


_AUDIT_EVENT_POLICY = JsonlPolicy(
    artifact_role="operator_audit",
    record_validator=_validate_audit_event_record,
)


def _audit_log_path() -> Path:
    """Resolve the operator-audit log target (lazily, per call).

    Precedence: an explicit in-process override of ``AUDIT_LOG_PATH`` (e.g. a test
    monkeypatch) wins; otherwise ``XRML_OPERATOR_AUDIT_PATH`` (set by the pytest isolation
    fixture and inherited by subprocesses) redirects writes/reads to a temp path; otherwise
    the production default. Production behavior is unchanged when neither is set.
    """
    if AUDIT_LOG_PATH is not _DEFAULT_AUDIT_LOG_PATH:
        return AUDIT_LOG_PATH
    override = os.environ.get(OPERATOR_AUDIT_PATH_ENV, "").strip()
    return Path(override) if override else AUDIT_LOG_PATH

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
SECRET_VALUE_PREFIXES = ("sk-", "ya29.", "ghp_", "xoxb-", "xoxp-", "Bearer ")
SAFE_REFERENCE_KEY_SUFFIXES = ("envvar", "env_var", "sha256", "hash", "digest")
MAX_METADATA_ITEMS = 20
MAX_METADATA_STRING_CHARS = 240
FORBIDDEN_METADATA_KEYS = {
    "packet",
    "packet_payload",
    "packetpayload",
    "full_text",
    "fulltext",
    "source_text",
    "sourcetext",
    "stdout",
    "stderr",
    "command_output",
    "commandoutput",
}


def _normalized_key(value: str) -> str:
    return value.replace("-", "_").replace(".", "_").lower()


def _looks_like_secret_key(key: str) -> bool:
    normalized = _normalized_key(key)
    compact = normalized.replace("_", "")
    if normalized.endswith(SAFE_REFERENCE_KEY_SUFFIXES) or compact.endswith("envvar"):
        return False
    return any(part in normalized for part in SECRET_KEY_PARTS)


def _looks_like_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if any(text.startswith(prefix) for prefix in SECRET_VALUE_PREFIXES):
        return True
    return bool(re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text))


def _bounded_string(value: str) -> str:
    if len(value) <= MAX_METADATA_STRING_CHARS:
        return value
    return f"{value[:MAX_METADATA_STRING_CHARS]}...<truncated>"


def _redact_metadata(value: Any, *, key: str = "") -> Any:
    if _looks_like_secret_key(key) or _looks_like_secret_value(value):
        return "<redacted>"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                redacted["<truncated>"] = True
                break
            redacted[str(child_key)] = _redact_metadata(child_value, key=str(child_key))
        return redacted
    if isinstance(value, list):
        items = [_redact_metadata(item, key=key) for item in value[:MAX_METADATA_ITEMS]]
        if len(value) > MAX_METADATA_ITEMS:
            items.append("<truncated>")
        return items
    if isinstance(value, str):
        return _bounded_string(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _bounded_string(str(value))


def _increment(counter: dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def _metadata_violations(value: Any, *, key: str = "") -> tuple[list[str], list[str]]:
    secret_reasons: list[str] = []
    warning_reasons: list[str] = []
    normalized = _normalized_key(key).replace("_", "")
    if normalized in FORBIDDEN_METADATA_KEYS:
        warning_reasons.append("forbidden_audit_metadata_field")
    if _looks_like_secret_key(key):
        if value not in (None, "", False, "<redacted>"):
            secret_reasons.append("secret_like_audit_metadata")
    if _looks_like_secret_value(value):
        secret_reasons.append("secret_like_audit_metadata")
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            child_secret, child_warning = _metadata_violations(child_value, key=str(child_key))
            secret_reasons.extend(child_secret)
            warning_reasons.extend(child_warning)
    elif isinstance(value, list):
        for item in value:
            child_secret, child_warning = _metadata_violations(item, key=key)
            secret_reasons.extend(child_secret)
            warning_reasons.extend(child_warning)
    return secret_reasons, warning_reasons


def _capability_consistency_reasons(event: dict[str, Any]) -> list[str]:
    requirement = action_capabilities.action_requirement(str(event.get("actionType") or ""))
    reasons: list[str] = []
    if requirement is None:
        if event.get("riskLevel") != "read_only":
            reasons.append("missing_action_requirement")
        return reasons
    expected_capability = requirement.get("capabilityRequired")
    actual_capability = event.get("capabilityRequired")
    if actual_capability and expected_capability and actual_capability != expected_capability:
        reasons.append("capability_requirement_mismatch")
    expected_risk = requirement.get("riskLevel")
    if expected_risk and event.get("riskLevel") and event.get("riskLevel") != expected_risk:
        reasons.append("risk_level_mismatch")
    if event.get("decision") == "allowed" and event.get("capabilityPresent") is False:
        reasons.append("allowed_without_capability")
    return reasons


def _stable_event_id(payload: dict[str, Any]) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"audit-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:16]}"


def record_event(
    *,
    action_type: str,
    decision: str,
    operator_id: str = "unknown",
    auth_mode: str = "disabled",
    capability_required: str | None = None,
    capability_present: bool | None = None,
    reason_codes: list[str] | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    risk_level: str = "read_only",
    provider_config_id: str | None = None,
    provider_origin: str | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    payload = {
        "timestamp": timestamp or iso_now_utc(),
        "operatorId": str(operator_id or "unknown"),
        "authMode": str(auth_mode or "disabled"),
        "actionType": action_type,
        "capabilityRequired": capability_required,
        "capabilityPresent": capability_present,
        "decision": decision,
        "reasonCodes": list(dict.fromkeys(str(reason) for reason in (reason_codes or []) if str(reason).strip())),
        "targetType": target_type,
        "targetId": str(target_id) if target_id is not None else None,
        "riskLevel": risk_level,
        "providerConfigId": str(provider_config_id) if provider_config_id is not None else None,
        "providerOrigin": str(provider_origin) if provider_origin is not None else None,
        "secretPresent": False,
        "metadata": _redact_metadata(metadata or {}),
    }
    event = {"auditEventId": _stable_event_id(payload), **payload}
    validated = OperatorAuditEventResponse.model_validate(event).model_dump(mode="json")
    if write:
        append_jsonl_record(
            _audit_log_path(),
            validated,
            policy=_AUDIT_EVENT_POLICY,
        )
    return validated


def list_events(*, limit: int | None = None) -> list[dict[str, Any]]:
    path = _audit_log_path()
    events = [
        OperatorAuditEventResponse.model_validate(event).model_dump(mode="json")
        for event in read_jsonl_strict(path, policy=_AUDIT_EVENT_POLICY)
    ]
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def summarize_events(*, latest_limit: int = 5) -> dict[str, Any]:
    events = list_events()
    latest_events = list(reversed(events[-max(0, latest_limit) :])) if latest_limit else []
    summary = {
        "totalEvents": len(events),
        "allowedCount": sum(1 for event in events if event.get("decision") == "allowed"),
        "deniedCount": sum(1 for event in events if event.get("decision") == "denied"),
        "secretPolicyRejectionCount": sum(1 for event in events if event.get("actionType") == "secret_policy_rejection"),
        "providerRiskAcceptanceCount": sum(1 for event in events if event.get("actionType") == "provider_risk_acceptance"),
        "configWriteCount": sum(1 for event in events if event.get("riskLevel") == "config_write" and event.get("decision") == "allowed"),
        "latestEventSummaries": [
            {
                "auditEventId": event.get("auditEventId"),
                "actionType": event.get("actionType"),
                "decision": event.get("decision"),
                "targetType": event.get("targetType"),
                "targetId": event.get("targetId"),
                "riskLevel": event.get("riskLevel"),
                "reasonCodes": event.get("reasonCodes") or [],
            }
            for event in latest_events
        ],
    }
    return OperatorAuditSummaryResponse.model_validate(summary).model_dump(mode="json")


def validate_audit_log() -> dict[str, Any]:
    path = _audit_log_path()
    if not path.exists():
        return OperatorAuditReplaySummaryResponse().model_dump(mode="json")

    valid_event_count = 0
    invalid_event_count = 0
    warning_count = 0
    secret_like_violation_count = 0
    missing_required_field_count = 0
    inconsistent_capability_count = 0
    latest_checked_event_id: str | None = None
    reason_counts: dict[str, int] = {}
    required_fields = {"auditEventId", "timestamp", "operatorId", "authMode", "actionType", "decision", "riskLevel", "secretPresent", "metadata"}

    inspection = inspect_jsonl(
        path,
        policy=JsonlPolicy(artifact_role="operator_audit_observation"),
    )
    invalid_event_count += inspection.issue_count
    for issue in inspection.issues:
        _increment(reason_counts, issue.code)
    if inspection.issue_count > len(inspection.issues):
        _increment(reason_counts, "jsonl_additional_issues")

    for raw_event in inspection.records:
        latest_checked_event_id = str(raw_event.get("auditEventId") or latest_checked_event_id or "")
        missing_fields = sorted(required_fields - set(raw_event.keys()))
        if missing_fields:
            invalid_event_count += 1
            missing_required_field_count += 1
            _increment(reason_counts, "missing_required_field")
            continue
        try:
            event = OperatorAuditEventResponse.model_validate(raw_event).model_dump(mode="json")
        except ValueError:
            invalid_event_count += 1
            _increment(reason_counts, "invalid_event_schema")
            continue

        event_invalid = False
        secret_reasons, warning_reasons = _metadata_violations(event.get("metadata") or {})
        if secret_reasons:
            event_invalid = True
            secret_like_violation_count += 1
            for reason in dict.fromkeys(secret_reasons):
                _increment(reason_counts, reason)
        if warning_reasons:
            warning_count += 1
            for reason in dict.fromkeys(warning_reasons):
                _increment(reason_counts, reason)
        consistency_reasons = _capability_consistency_reasons(event)
        if consistency_reasons:
            event_invalid = True
            inconsistent_capability_count += 1
            for reason in consistency_reasons:
                _increment(reason_counts, reason)

        if event_invalid:
            invalid_event_count += 1
        else:
            valid_event_count += 1

    if valid_event_count == 0 and invalid_event_count == 0 and warning_count == 0:
        status = "empty"
    elif invalid_event_count or secret_like_violation_count or inconsistent_capability_count or missing_required_field_count:
        status = "errors"
    elif warning_count:
        status = "warnings"
    else:
        status = "valid"

    return OperatorAuditReplaySummaryResponse.model_validate(
        {
            "auditLogStatus": status,
            "validEventCount": valid_event_count,
            "invalidEventCount": invalid_event_count,
            "warningCount": warning_count,
            "secretLikeViolationCount": secret_like_violation_count,
            "missingRequiredFieldCount": missing_required_field_count,
            "inconsistentCapabilityCount": inconsistent_capability_count,
            "latestCheckedEventId": latest_checked_event_id or None,
            "reasonCounts": reason_counts,
        }
    ).model_dump(mode="json")

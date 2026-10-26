from __future__ import annotations

import hashlib
import json
from typing import Any

from app.repositories.files import iso_now_utc
from app.schemas.auth import (
    OAuthLivePilotEvidencePackageResponse,
    OAuthLivePilotEvidenceSummaryResponse,
    OAuthLivePilotReplayVerificationResponse,
)
from app.services import auth_foundation, oauth_account_links, oauth_live_readiness, oauth_token_exchange, operator_audit

FORBIDDEN_ACTIONS = [
    "Do not perform live token exchange from the dashboard.",
    "Do not store OAuth access tokens, refresh tokens, ID tokens, authorization codes, API keys, or provider credentials.",
    "Do not persist raw token endpoint request or response bodies.",
    "Do not let OAuth account links grant provider execution or risk acceptance.",
]
REQUIRED_EVIDENCE_FIELDS = [
    "readinessStatus",
    "preflightSummary",
    "checklistSummary",
    "auditHealthStatus",
    "accountLinkHealthStatus",
    "leakCheckStatus",
    "gateStatus",
    "evidenceItems",
]


def _stable_id(prefix: str, provider_id: str) -> str:
    digest = hashlib.sha256(str(provider_id or "oauth-provider").encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _evidence_item(
    item_id: str,
    category: str,
    label: str,
    status: str,
    summary: str,
    reason_codes: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "itemId": item_id,
        "category": category,
        "label": label,
        "status": status,
        "summary": summary,
        "reasonCodes": _unique(reason_codes or []),
        "warnings": _unique(warnings or []),
    }


def _status_from_health(value: str) -> str:
    if value in {"errors", "invalid"}:
        return "failed"
    if value in {"warnings", "unavailable"}:
        return "warning"
    return "present"


def _metadata_env_var_names(provider_metadata: dict[str, Any] | None) -> list[str]:
    metadata = provider_metadata or {}
    names = [
        metadata.get("clientIdEnvVar") or metadata.get("client_id_env_var"),
        metadata.get("clientSecretEnvVar") or metadata.get("client_secret_env_var"),
    ]
    return _unique([str(name).strip() for name in names if str(name or "").strip()])


def _minimal_gate_payload(
    provider_id: str,
    provider_metadata: dict[str, Any] | None,
    redirect_uri: str | None,
    checklist: dict[str, Any],
) -> dict[str, Any]:
    return {
        "providerId": provider_id,
        "correlationId": "evidence-package-preview",
        "oauthProviderMetadata": provider_metadata or {},
        "redirectUri": redirect_uri,
        "authorizationCodePresent": False,
        "stateStatus": "unknown",
        "codeVerifierPresent": False,
        "operatorAcceptancePresent": False,
        "liveExchangeRequested": False,
        "tokenStorageRequested": False,
        "dryRunOnly": True,
        "dashboardExecutionContext": False,
        "checklist": checklist,
    }


def _verification_status(failed_checks: list[str], warning_checks: list[str]) -> str:
    if failed_checks:
        return "failed"
    if warning_checks:
        return "warnings"
    return "valid"


def verify_live_pilot_evidence_package(package: dict[str, Any] | None) -> dict[str, Any]:
    evidence = dict(package or {})
    provider_id = str(evidence.get("providerId") or "oauth-provider")
    failed_checks: list[str] = []
    warning_checks: list[str] = []
    reason_codes: list[str] = ["oauth_live_pilot_replay_verification_read_only"]
    warnings: list[str] = []

    missing_required = [
        field
        for field in REQUIRED_EVIDENCE_FIELDS
        if field not in evidence or evidence.get(field) in (None, "", [], {})
    ]
    if missing_required:
        failed_checks.append("required_evidence_present")
        reason_codes.append("missing_required_evidence")

    disabled_flag_checks = {
        "token_values_absent": evidence.get("tokenValuesPresent") is False,
        "secret_values_absent": evidence.get("secretValuesPresent") is False,
        "token_storage_disabled": evidence.get("tokenStorageEnabled") is False,
        "live_pilot_disabled_by_default": evidence.get("livePilotEnabledDefault") is False,
        "dashboard_execution_disabled": evidence.get("dashboardExecutionEnabled") is False,
        "external_call_absent": evidence.get("externalCallPerformed") is False,
        "env_var_values_not_exposed": evidence.get("envVarValuesExposed") is False,
    }
    for check_id, passed in disabled_flag_checks.items():
        if not passed:
            failed_checks.append(check_id)
            reason_codes.append(f"{check_id}_violation")

    scan = oauth_token_exchange.scan_oauth_live_pilot_leaks(evidence)
    if scan.get("leakDetected"):
        failed_checks.append("leak_scan_clean")
        reason_codes.extend(scan.get("reasonCodes") or [])
        reason_codes.append("evidence_package_leak_detected")

    audit_status = str(evidence.get("auditHealthStatus") or "empty")
    account_link_status = str(evidence.get("accountLinkHealthStatus") or "empty")
    readiness_status = str(evidence.get("readinessStatus") or "blocked")
    preflight_status = str((evidence.get("preflightSummary") or {}).get("preflightStatus") or "")
    checklist_valid = bool((evidence.get("checklistSummary") or {}).get("acceptanceValid"))

    if audit_status in {"warnings", "errors", "unavailable"}:
        warning_checks.append("audit_health_review")
        reason_codes.append("audit_health_warning_surfaced")
    if account_link_status in {"warnings", "errors", "invalid"}:
        warning_checks.append("account_link_health_review")
        reason_codes.append("account_link_health_warning_surfaced")
    if readiness_status == "blocked":
        warning_checks.append("readiness_not_ready")
        reason_codes.append("readiness_status_surfaced")
    if preflight_status == "blocked":
        warning_checks.append("preflight_not_ready")
        reason_codes.append("preflight_status_surfaced")
    if not checklist_valid:
        warning_checks.append("checklist_not_accepted")
        reason_codes.append("checklist_status_surfaced")

    if failed_checks:
        warnings.append("OAuth live pilot evidence replay found failed checks; inspect reason codes and redacted paths.")
    elif warning_checks:
        warnings.append("OAuth live pilot evidence replay found warnings only; live pilot remains disabled by default.")
    else:
        reason_codes.append("replay_verification_clean")

    return OAuthLivePilotReplayVerificationResponse.model_validate(
        {
            "verificationId": _stable_id("oauth-live-pilot-replay", provider_id),
            "providerId": provider_id,
            "verificationStatus": _verification_status(failed_checks, warning_checks),
            "failedChecks": _unique(failed_checks),
            "warningChecks": _unique(warning_checks),
            "leakCount": int(scan.get("leakCount") or 0),
            "secretLikeViolationCount": int(scan.get("leakCount") or 0),
            "missingRequiredEvidenceCount": len(missing_required),
            "reasonCodes": _unique(reason_codes),
            "redactedPaths": list(scan.get("redactedPaths") or []),
            "warnings": warnings,
        }
    ).model_dump(mode="json")


def build_live_pilot_evidence_package(
    *,
    provider_metadata: dict[str, Any] | None = None,
    redirect_uri: str | None = None,
    operator_identity: dict[str, Any] | None = None,
    audit_health: dict[str, Any] | None = None,
    account_link_health: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    remediation: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
    checklist: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    identity = operator_identity or auth_foundation.operator_identity_for_user(None)
    resolved_audit_health = audit_health or operator_audit.validate_audit_log()
    resolved_account_link_health = account_link_health or oauth_account_links.build_account_link_health_summary()
    session_policy = oauth_account_links.build_session_capability_policy(identity)
    resolved_readiness = readiness or oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=provider_metadata,
        redirect_uri=redirect_uri,
        operator_identity=identity,
        audit_health=resolved_audit_health,
        account_link_health=resolved_account_link_health,
        session_policy=session_policy,
    )
    resolved_remediation = remediation or oauth_live_readiness.build_readiness_remediation(
        readiness=resolved_readiness,
        provider_metadata=provider_metadata,
    )
    resolved_preflight = preflight or oauth_live_readiness.build_preflight_package(
        provider_metadata=provider_metadata,
        redirect_uri=redirect_uri,
        operator_identity=identity,
        audit_health=resolved_audit_health,
        account_link_health=resolved_account_link_health,
        session_policy=session_policy,
        readiness=resolved_readiness,
        remediation=resolved_remediation,
    )
    provider_id = str(
        resolved_preflight.get("providerId")
        or resolved_readiness.get("providerId")
        or (provider_metadata or {}).get("providerId")
        or "oauth-provider"
    )
    resolved_checklist = checklist or oauth_live_readiness.validate_operator_acceptance_checklist(
        {"providerId": provider_id},
        operator_identity=identity,
    )
    gate_summary = oauth_token_exchange.build_live_exchange_gate_preview(
        _minimal_gate_payload(provider_id, provider_metadata, redirect_uri, resolved_checklist),
        operator_identity=identity,
    )
    audit_status = str(resolved_audit_health.get("auditLogStatus") or "empty")
    account_link_status = str(resolved_account_link_health.get("healthStatus") or "empty")
    preflight_summary = resolved_preflight.get("summary") or {}
    leak_scan = oauth_token_exchange.scan_oauth_live_pilot_leaks(
        {
            "readinessStatus": resolved_readiness.get("readinessStatus"),
            "preflightSummary": preflight_summary,
            "checklistSummary": resolved_checklist,
            "auditHealthStatus": audit_status,
            "accountLinkHealthStatus": account_link_status,
            "gateStatus": gate_summary.get("gateStatus"),
            "envVarNames": _metadata_env_var_names(provider_metadata),
        }
    )
    leak_check_status = "failed" if leak_scan.get("leakDetected") else "clean"
    evidence_items = [
        _evidence_item(
            "readiness_result",
            "readiness",
            "OAuth live readiness",
            "warning" if resolved_readiness.get("readinessStatus") == "blocked" else "present",
            str(resolved_readiness.get("readinessStatus") or "blocked"),
            list(resolved_readiness.get("reasonCodes") or []),
        ),
        _evidence_item(
            "preflight_result",
            "preflight",
            "OAuth preflight package",
            "warning" if preflight_summary.get("preflightStatus") == "blocked" else "present",
            str(preflight_summary.get("preflightStatus") or "blocked"),
            list(resolved_preflight.get("reasonCodes") or []),
        ),
        _evidence_item(
            "checklist_validation",
            "checklist",
            "Operator checklist",
            "present" if resolved_checklist.get("acceptanceValid") else "warning",
            "accepted" if resolved_checklist.get("acceptanceValid") else "missing confirmations",
            list(resolved_checklist.get("reasonCodes") or []),
        ),
        _evidence_item(
            "audit_health",
            "audit_health",
            "Audit health",
            _status_from_health(audit_status),
            audit_status,
            ["audit_health_status_surfaced"],
        ),
        _evidence_item(
            "account_link_health",
            "account_link_health",
            "Account-link health",
            _status_from_health(account_link_status),
            account_link_status,
            ["account_link_health_status_surfaced"],
        ),
        _evidence_item(
            "leak_scan",
            "leak_check",
            "Leak scan",
            "failed" if leak_scan.get("leakDetected") else "present",
            f"leaks {int(leak_scan.get('leakCount') or 0)}",
            list(leak_scan.get("reasonCodes") or ["leak_scan_clean"]),
        ),
        _evidence_item(
            "live_pilot_gate",
            "gate",
            "Live pilot gate",
            "present",
            str(gate_summary.get("gateStatus") or "dry_run_only"),
            list(gate_summary.get("reasonCodes") or []),
        ),
        _evidence_item(
            "disabled_policy",
            "policy",
            "Disabled policy",
            "present",
            "live pilot, token storage, and dashboard execution disabled",
            ["live_pilot_disabled_by_default", "token_storage_disabled", "dashboard_execution_disabled"],
        ),
    ]
    package_without_replay: dict[str, Any] = {
        "packageId": _stable_id("oauth-live-pilot-evidence", provider_id),
        "providerId": provider_id,
        "generatedAt": now or iso_now_utc(),
        "readinessStatus": resolved_readiness.get("readinessStatus") or "blocked",
        "remediationSummary": resolved_remediation,
        "preflightSummary": preflight_summary,
        "checklistSummary": resolved_checklist,
        "auditHealthStatus": audit_status,
        "accountLinkHealthStatus": account_link_status,
        "leakCheckStatus": leak_check_status,
        "gateStatus": gate_summary.get("gateStatus") or "dry_run_only",
        "livePilotEnabledDefault": False,
        "dashboardExecutionEnabled": False,
        "tokenStorageEnabled": False,
        "tokenValuesPresent": False,
        "secretValuesPresent": False,
        "externalCallPerformed": False,
        "envVarNames": _metadata_env_var_names(provider_metadata),
        "envVarValuesExposed": False,
        "evidenceItems": evidence_items,
        "forbiddenActions": FORBIDDEN_ACTIONS,
        "safeNextSteps": _unique(
            [
                *list(resolved_preflight.get("safeNextSteps") or []),
                "Review the secret-free evidence package before any future live OAuth smoke.",
            ]
        )[:6],
        "reasonCodes": _unique(
            [
                "oauth_live_pilot_evidence_package_preview_only",
                "no_external_call_performed",
                "token_storage_disabled",
                "live_pilot_disabled_by_default",
                "dashboard_execution_disabled",
                *list(resolved_readiness.get("reasonCodes") or []),
            ]
        ),
        "warnings": [
            "OAuth live pilot evidence package is read-only and does not perform token exchange.",
            "Env var names may be shown; values are never included.",
        ],
    }
    verification = verify_live_pilot_evidence_package(package_without_replay)
    summary = OAuthLivePilotEvidenceSummaryResponse.model_validate(
        {
            "providerId": provider_id,
            "readinessStatus": package_without_replay["readinessStatus"],
            "preflightStatus": preflight_summary.get("preflightStatus") or "blocked",
            "checklistValid": bool(resolved_checklist.get("acceptanceValid")),
            "auditHealthStatus": audit_status,
            "accountLinkHealthStatus": account_link_status,
            "leakCheckStatus": leak_check_status,
            "gateStatus": package_without_replay["gateStatus"],
            "verificationStatus": verification["verificationStatus"],
            "tokenStorageEnabled": False,
            "dashboardExecutionEnabled": False,
            "livePilotEnabledDefault": False,
        }
    ).model_dump(mode="json")
    package_without_replay["replayVerification"] = verification
    package_without_replay["summary"] = summary
    return OAuthLivePilotEvidencePackageResponse.model_validate(package_without_replay).model_dump(mode="json")


def package_json_text(package: dict[str, Any]) -> str:
    return json.dumps(package, ensure_ascii=False, sort_keys=True)

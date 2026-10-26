from __future__ import annotations

import hashlib
from typing import Any

from app.repositories.files import iso_now_utc
from app.schemas.auth import (
    OAuthSmokeEvidenceFreezeResponse,
    OAuthSmokeReplaySummaryResponse,
    OAuthSmokeResultRecordResponse,
    OAuthSmokeResultSummaryResponse,
)
from app.services import oauth_live_evidence, oauth_token_exchange


def _stable_id(prefix: str, provider_id: str, *parts: Any) -> str:
    raw = "|".join([str(provider_id or "oauth-provider"), *(str(part or "") for part in parts)])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _captured_status(smoke_result: dict[str, Any], *, mocked_http_used: bool) -> str:
    smoke_status = str(smoke_result.get("smokeStatus") or "blocked")
    reason_codes = set(str(code) for code in smoke_result.get("reasonCodes") or [])
    if smoke_status == "skipped" or "real_provider_smoke_not_configured" in reason_codes:
        return "skipped_not_configured"
    if smoke_status == "completed_redacted":
        return "completed_mocked" if mocked_http_used else "completed_real_redacted"
    if smoke_status == "failed":
        return "failed_redacted"
    return "blocked"


def _freeze_status(replay_status: str, leak_detected: bool) -> str:
    if leak_detected or replay_status == "failed":
        return "failed"
    if replay_status == "warnings":
        return "warnings"
    return "valid"


def build_smoke_result_summary(record: dict[str, Any] | None = None) -> dict[str, Any]:
    if not record:
        return OAuthSmokeResultSummaryResponse.model_validate(
            {
                "providerId": "oauth-provider",
                "latestSmokeStatus": "no_result",
                "latestResultId": None,
                "replayVerificationStatus": "valid",
                "accountLinkCreated": False,
                "metadataOnlyLink": True,
                "tokenStorageEnabled": False,
                "dashboardExecutionEnabled": False,
                "reasonCodes": ["no_smoke_result_captured", "token_storage_disabled", "dashboard_execution_disabled"],
                "warnings": ["No OAuth smoke result has been captured in this read-only preview context."],
            }
        ).model_dump(mode="json")
    return OAuthSmokeResultSummaryResponse.model_validate(
        {
            "providerId": record.get("providerId") or "oauth-provider",
            "latestSmokeStatus": record.get("smokeStatus") or "no_result",
            "latestResultId": record.get("smokeResultId"),
            "replayVerificationStatus": record.get("replayVerificationStatus") or "failed",
            "accountLinkCreated": bool(record.get("accountLinkCreated")),
            "metadataOnlyLink": True,
            "tokenStorageEnabled": False,
            "dashboardExecutionEnabled": False,
            "reasonCodes": list(record.get("reasonCodes") or []),
            "warnings": list(record.get("warnings") or []),
        }
    ).model_dump(mode="json")


def capture_smoke_result_record(
    smoke_result: dict[str, Any] | None,
    *,
    evidence_package: dict[str, Any] | None = None,
    mocked_http_used: bool = False,
    replay_status: str | None = None,
    leak_detected: bool = False,
    leak_reason_codes: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    result = dict(smoke_result or {})
    provider_id = str(result.get("providerId") or (evidence_package or {}).get("providerId") or "oauth-provider")
    status = _captured_status(result, mocked_http_used=mocked_http_used)
    reason_codes = _unique(
        [
            "smoke_result_capture_secret_free",
            f"smoke_result_{status}",
            "token_storage_disabled",
            "dashboard_execution_disabled",
            *list(result.get("reasonCodes") or []),
            *(leak_reason_codes or []),
        ]
    )
    if leak_detected:
        reason_codes.append("smoke_result_leak_detected")
    return OAuthSmokeResultRecordResponse.model_validate(
        {
            "smokeResultId": _stable_id("oauth-smoke-result", provider_id, status, result.get("pilotGateStatus")),
            "providerId": provider_id,
            "operatorId": result.get("operatorId"),
            "smokeStatus": status,
            "externalAuthCallPerformed": bool(result.get("externalAuthCallPerformed")),
            "mockedHttpUsed": bool(mocked_http_used),
            "accountLinkCreated": bool(result.get("metadataLinkCreated") or result.get("accountLinkCreated")),
            "metadataOnlyLink": True,
            "tokenValuesPresent": False,
            "secretValuesPresent": False,
            "rawResponseStored": False,
            "replayVerificationStatus": replay_status or str(result.get("evidenceReplayStatus") or "failed"),
            "evidencePackageId": (evidence_package or {}).get("packageId"),
            "reasonCodes": _unique(reason_codes),
            "warnings": _unique(
                [
                    "OAuth smoke result capture is metadata-only and contains no token, code, secret, or raw response values.",
                    *list(result.get("warnings") or []),
                ]
            ),
            "createdAt": now or iso_now_utc(),
        }
    ).model_dump(mode="json")


def preview_smoke_replay(payload: dict[str, Any] | None) -> dict[str, Any]:
    request = dict(payload or {})
    smoke_result = request.get("smokeResult") if isinstance(request.get("smokeResult"), dict) else {}
    evidence_package = request.get("evidencePackage") if isinstance(request.get("evidencePackage"), dict) else {}
    provider_id = str(smoke_result.get("providerId") or evidence_package.get("providerId") or "oauth-provider")
    evidence_replay = oauth_live_evidence.verify_live_pilot_evidence_package(evidence_package)
    smoke_scan = oauth_token_exchange.scan_oauth_live_pilot_leaks(smoke_result)
    failed_checks = list(evidence_replay.get("failedChecks") or [])
    warning_checks = list(evidence_replay.get("warningChecks") or [])
    reason_codes = list(evidence_replay.get("reasonCodes") or [])
    warnings = list(evidence_replay.get("warnings") or [])
    if smoke_scan.get("leakDetected"):
        failed_checks.append("smoke_result_leak_scan")
        reason_codes.extend(smoke_scan.get("reasonCodes") or [])
        reason_codes.append("smoke_result_leak_detected")
        warnings.append("OAuth smoke result replay detected token-like fields; values were not echoed.")
    verification_status = "failed" if failed_checks else str(evidence_replay.get("verificationStatus") or "failed")
    return OAuthSmokeReplaySummaryResponse.model_validate(
        {
            "replaySummaryId": _stable_id("oauth-smoke-replay", provider_id, evidence_package.get("packageId")),
            "providerId": provider_id,
            "evidencePackageId": evidence_package.get("packageId"),
            "verificationStatus": verification_status,
            "leakCount": int(evidence_replay.get("leakCount") or 0) + int(smoke_scan.get("leakCount") or 0),
            "secretLikeViolationCount": int(evidence_replay.get("secretLikeViolationCount") or 0) + int(smoke_scan.get("leakCount") or 0),
            "failedChecks": _unique(failed_checks),
            "warningChecks": _unique(warning_checks),
            "reasonCodes": _unique(reason_codes),
            "redactedPaths": _unique([*list(evidence_replay.get("redactedPaths") or []), *list(smoke_scan.get("redactedPaths") or [])]),
            "warnings": _unique(warnings),
        }
    ).model_dump(mode="json")


def preview_smoke_evidence_freeze(payload: dict[str, Any] | None) -> dict[str, Any]:
    request = dict(payload or {})
    smoke_result = request.get("smokeResult") if isinstance(request.get("smokeResult"), dict) else {}
    evidence_package = request.get("evidencePackage") if isinstance(request.get("evidencePackage"), dict) else {}
    mocked_http_used = bool(request.get("mockedHttpUsed"))
    replay_summary = preview_smoke_replay(request)
    replay_status = str(replay_summary.get("verificationStatus") or "failed")
    leak_detected = int(replay_summary.get("leakCount") or 0) > 0
    record = capture_smoke_result_record(
        smoke_result,
        evidence_package=evidence_package,
        mocked_http_used=mocked_http_used,
        replay_status=replay_status,
        leak_detected=leak_detected,
        leak_reason_codes=list(replay_summary.get("reasonCodes") or []),
    )
    status = _freeze_status(replay_status, leak_detected)
    reason_codes = _unique(
        [
            "oauth_smoke_evidence_freeze_preview_only",
            "token_storage_disabled",
            "dashboard_execution_disabled",
            "raw_response_not_stored",
            *list(record.get("reasonCodes") or []),
            *list(replay_summary.get("reasonCodes") or []),
        ]
    )
    if leak_detected:
        reason_codes.append("smoke_result_leak_detected")
    return OAuthSmokeEvidenceFreezeResponse.model_validate(
        {
            "freezeId": _stable_id("oauth-smoke-freeze", record.get("providerId"), record.get("smokeResultId"), evidence_package.get("packageId")),
            "providerId": record.get("providerId") or "oauth-provider",
            "generatedAt": iso_now_utc(),
            "freezeStatus": status,
            "previewOnly": True,
            "exportWritten": False,
            "smokeResult": record,
            "replaySummary": replay_summary,
            "evidencePackageId": evidence_package.get("packageId"),
            "tokenStorageEnabled": False,
            "dashboardExecutionEnabled": False,
            "reasonCodes": _unique(reason_codes),
            "warnings": _unique(
                [
                    "OAuth smoke evidence freeze preview writes nothing and stores no token, code, secret, or raw response values.",
                    *list(record.get("warnings") or []),
                    *list(replay_summary.get("warnings") or []),
                ]
            ),
        }
    ).model_dump(mode="json")

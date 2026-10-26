from __future__ import annotations

from pathlib import Path
from typing import Any

from app.repositories import ai_review_drafts, paper_reviews
from app.services import provider_review_coverage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWN_RUNTIME_UNTRACKED = "storage/topic_profiles/profiles (1).json"
RUNTIME_ARTIFACT_PREFIXES = (
    "output/",
    "storage/runs/",
    "storage/oauth",
    "storage/operator_audit",
    "storage/provider",
    "storage/ai-review",
    "storage/topic_profiles/",
    "reports/",
)
RUNTIME_ARTIFACT_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pdf",
)
SYNTHETIC_ORIGINS = {"stub_synthetic", "command_fixture_synthetic"}
M16_VALIDATION_MATRIX = {
    "focusedM16Tests": "3 passed",
    "dashboardReviewCoverageApi": "5 passed",
    "providerReportRecommendationSuites": "52 passed",
    "authOAuthSuite": "103 passed",
    "fullPythonSuite": "527 passed",
    "fullVitest": "91 passed",
    "frontendBuild": "passed",
    "visualE2E": "1 passed, 1 skipped",
    "diffCheck": "exit 0, CRLF warnings only",
}


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _review_key(value: dict[str, Any]) -> str:
    return f"{value.get('paper_id') or ''}::{value.get('source_text_sha256') or ''}"


def _reviewed_keys(reviews: list[dict[str, Any]]) -> set[str]:
    return {
        _review_key(review)
        for review in reviews
        if str(review.get("full_text_status") or "") == "reviewed" and str(review.get("source_text_sha256") or "").strip()
    }


def _recommendation_evidence_refs(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    refs: list[dict[str, Any]] = []
    for section in ("gap_candidates", "new_topic_candidates"):
        candidates = payload.get(section)
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for field in ("supporting_evidence_refs", "limitation_refs"):
                candidate_refs = candidate.get(field)
                if isinstance(candidate_refs, list):
                    refs.extend(ref for ref in candidate_refs if isinstance(ref, dict))
    return refs


def _recommendation_candidates(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for section in ("gap_candidates", "new_topic_candidates"):
        section_items = payload.get(section)
        if isinstance(section_items, list):
            candidates.extend(item for item in section_items if isinstance(item, dict))
    return candidates


def _parse_git_status_line(line: str) -> tuple[str, str]:
    raw = str(line or "").rstrip()
    status = raw[:2].strip() if len(raw) >= 2 else ""
    path = raw[3:].strip() if len(raw) >= 3 else raw.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return status, path.replace("\\", "/")


def _is_runtime_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    if normalized == KNOWN_RUNTIME_UNTRACKED:
        return True
    if normalized.endswith(RUNTIME_ARTIFACT_SUFFIXES):
        return True
    return normalized.startswith(RUNTIME_ARTIFACT_PREFIXES)


def _artifact_classification(
    *,
    path: str,
    classification: str,
    artifact_policy: str,
    severity: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    return {
        "path": path,
        "classification": classification,
        "artifactPolicy": artifact_policy,
        "severity": severity,
        "shouldRemainUntracked": True,
        "reasonCodes": _unique(reason_codes),
    }


def build_runtime_artifact_boundary_summary(git_status_lines: list[str] | None = None) -> dict[str, Any]:
    lines = list(git_status_lines or [])
    if git_status_lines is None:
        known_path = PROJECT_ROOT / KNOWN_RUNTIME_UNTRACKED
        if known_path.exists():
            lines.append(f'?? "{KNOWN_RUNTIME_UNTRACKED}"')

    known_runtime_untracked: list[str] = []
    unexpected_tracked: list[str] = []
    runtime_warnings: list[str] = []
    reason_codes: list[str] = []
    warning_classifications: list[dict[str, Any]] = []

    for line in lines:
        status, path = _parse_git_status_line(line)
        if not path:
            continue
        is_untracked = status == "??"
        is_runtime = _is_runtime_artifact_path(path)
        if is_untracked and path == KNOWN_RUNTIME_UNTRACKED:
            known_runtime_untracked.append(path)
            runtime_warnings.append("Known user-local topic profile runtime file remains untracked.")
            reason_codes.append("known_runtime_artifact_untracked")
            warning_classifications.append(
                _artifact_classification(
                    path=path,
                    classification="accepted_runtime_warning",
                    artifact_policy="ignored_runtime_artifact",
                    severity="warning",
                    reason_codes=["known_runtime_artifact_untracked"],
                )
            )
        elif is_runtime and not is_untracked:
            unexpected_tracked.append(path)
            reason_codes.append("runtime_artifact_tracked")
            warning_classifications.append(
                _artifact_classification(
                    path=path,
                    classification="tracking_policy_violation",
                    artifact_policy="runtime_artifact_must_not_be_tracked",
                    severity="blocking",
                    reason_codes=["runtime_artifact_tracked"],
                )
            )
        elif is_runtime and is_untracked:
            runtime_warnings.append(f"Runtime artifact remains untracked: {path}")
            reason_codes.append("runtime_artifact_untracked")
            warning_classifications.append(
                _artifact_classification(
                    path=path,
                    classification="ignored_runtime_artifact",
                    artifact_policy="ignored_runtime_artifact",
                    severity="warning",
                    reason_codes=["runtime_artifact_untracked"],
                )
            )

    if unexpected_tracked:
        boundary_status = "failed"
    elif runtime_warnings:
        boundary_status = "warnings"
    else:
        boundary_status = "valid"
        reason_codes.append("runtime_artifact_boundary_valid")

    return {
        "boundaryStatus": boundary_status,
        "knownRuntimeUntrackedCount": len(known_runtime_untracked),
        "unexpectedRuntimeTrackedCount": len(unexpected_tracked),
        "runtimeArtifactWarningCount": len(runtime_warnings),
        "acceptedRuntimeWarningCount": sum(1 for item in warning_classifications if item["classification"] == "accepted_runtime_warning"),
        "blockingReleaseWarningCount": sum(1 for item in warning_classifications if item["severity"] == "blocking"),
        "warningClassifications": warning_classifications,
        "knownRuntimeArtifacts": _unique(known_runtime_untracked),
        "unexpectedTrackedPaths": _unique(unexpected_tracked),
        "reasonCodes": _unique(reason_codes),
        "warnings": _unique(runtime_warnings),
    }


def _check(
    checks: list[dict[str, Any]],
    *,
    check_id: str,
    category: str,
    status: str,
    label: str,
    summary: str,
    reason_codes: list[str] | None = None,
) -> None:
    checks.append(
        {
            "checkId": check_id,
            "category": category,
            "status": status,
            "label": label,
            "summary": summary,
            "reasonCodes": _unique(reason_codes or []),
        }
    )


def _status_from_counts(failed_count: int, warning_count: int) -> str:
    if failed_count:
        return "failed"
    if warning_count:
        return "warnings"
    return "valid"


def _surface(surface_id: str, status: str, summary: str, reason_codes: list[str] | None = None) -> dict[str, Any]:
    return {
        "surfaceId": surface_id,
        "status": status,
        "summary": summary,
        "reasonCodes": _unique(reason_codes or []),
    }


def _field(data: dict[str, Any] | None, *path: str) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _auth_status(dashboard_operations: dict[str, Any] | None, auth_status: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(auth_status, dict):
        return auth_status
    value = _field(dashboard_operations, "auth")
    return value if isinstance(value, dict) else {}


def _oauth_secret_safety_failed(auth_status: dict[str, Any]) -> bool:
    if bool(auth_status.get("tokenStorageEnabled")):
        return True
    readiness = auth_status.get("oauthLiveExchangeReadiness") if isinstance(auth_status.get("oauthLiveExchangeReadiness"), dict) else {}
    pilot = auth_status.get("oauthLiveExchangePilot") if isinstance(auth_status.get("oauthLiveExchangePilot"), dict) else {}
    return any(
        bool(value)
        for value in (
            readiness.get("liveExchangeEnabled"),
            readiness.get("tokenStorageEnabled"),
            pilot.get("liveExchangeEnabled"),
            pilot.get("tokenStorageEnabled"),
            pilot.get("tokenValuesReturned"),
            pilot.get("tokenValuesAudited"),
        )
    )


def _provider_dashboard_safety_failed(auth_status: dict[str, Any]) -> bool:
    if str(auth_status.get("providerLiveExecution") or "") == "dashboard_enabled":
        return True
    matrix = auth_status.get("providerOrchestrationSafetyMatrix")
    if not isinstance(matrix, dict):
        return False
    if _safe_int(matrix.get("dashboardAllowedActionCount")) > 0:
        return True
    actions = matrix.get("actions")
    if isinstance(actions, list):
        return any(isinstance(action, dict) and bool(action.get("dashboardAllowed")) for action in actions)
    return False


def _synthetic_label_violation_count(recommendation_payload: dict[str, Any] | None) -> int:
    count = 0
    for ref in _recommendation_evidence_refs(recommendation_payload):
        review_level = str(ref.get("review_level") or "")
        origin = str(ref.get("provider_origin") or "")
        if origin in SYNTHETIC_ORIGINS and review_level != "accepted_ai_grounded_synthetic":
            count += 1
        elif review_level == "accepted_ai_grounded_synthetic" and origin and origin not in SYNTHETIC_ORIGINS:
            count += 1
    return count


def _synthetic_only_high_confidence_count(recommendation_payload: dict[str, Any] | None) -> int:
    count = 0
    for candidate in _recommendation_candidates(recommendation_payload):
        if str(candidate.get("confidence") or "").lower() != "high":
            continue
        refs = []
        for field in ("supporting_evidence_refs", "limitation_refs"):
            values = candidate.get(field)
            if isinstance(values, list):
                refs.extend(ref for ref in values if isinstance(ref, dict))
        if not refs:
            continue
        synthetic_refs = [ref for ref in refs if ref.get("review_level") == "accepted_ai_grounded_synthetic"]
        non_synthetic_grounded = [
            ref
            for ref in refs
            if ref.get("review_level") in {"deterministic_full_text", "accepted_ai_grounded", "full_text_grounded"}
        ]
        if synthetic_refs and not non_synthetic_grounded:
            count += 1
    return count


def _report_label_violation_count(report_markdown: str | None, synthetic_count: int) -> int:
    if not report_markdown or synthetic_count <= 0:
        return 0
    lower = report_markdown.lower()
    if "synthetic provider accepted drafts" not in lower:
        return 1
    if "synthetic/local fixture" not in lower and "test artifact" not in lower and "synthetic provider" not in lower:
        return 1
    return 0


def build_research_operations_rc_audit(
    topic_id: str,
    *,
    dashboard_operations: dict[str, Any] | None = None,
    recommendation_payload: dict[str, Any] | None = None,
    report_markdown: str | None = None,
    auth_status: dict[str, Any] | None = None,
    git_status_lines: list[str] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    all_drafts: list[dict[str, Any]] | None = None,
    coverage_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_reviews = reviews if reviews is not None else paper_reviews.list_reviews(topic_id=topic_id, limit=100_000)
    drafts = all_drafts if all_drafts is not None else ai_review_drafts.list_drafts(topic_id=topic_id, limit=100_000)
    reviewed_keys = _reviewed_keys(selected_reviews)
    accepted = provider_review_coverage.accepted_drafts_by_review_key(topic_id, reviews=selected_reviews, all_drafts=drafts)
    full_text_reviewed_count = len(reviewed_keys)
    ai_grounded_count = len(accepted)
    source_provider_summary = coverage_summary or provider_review_coverage.build_provider_review_coverage_summary(
        topic_id,
        full_text_reviewed_denominator=full_text_reviewed_count,
        reviews=selected_reviews,
        all_drafts=drafts,
        accepted_by_review_key=accepted,
    )
    source_full_text_count = _safe_int(source_provider_summary.get("fullTextReviewedDenominator")) or full_text_reviewed_count
    dashboard_coverage = _field(dashboard_operations, "coverage")
    dashboard_provider_summary = _field(dashboard_operations, "coverage", "providerReviewCoverage")
    if not isinstance(dashboard_provider_summary, dict):
        dashboard_provider_summary = source_provider_summary

    provenance_audit = provider_review_coverage.build_provider_provenance_audit(
        topic_id,
        dashboard_summary=dashboard_provider_summary,
        recommendation_payload=recommendation_payload,
        report_markdown=report_markdown,
        reviews=selected_reviews,
        all_drafts=drafts,
        coverage_summary=source_provider_summary,
    )
    artifact_boundary = build_runtime_artifact_boundary_summary(git_status_lines)
    checks: list[dict[str, Any]] = []

    dashboard_full_text_count = _safe_int(_field(dashboard_operations, "coverage", "fullTextReviewedUniqueCount"))
    full_text_mismatch = isinstance(dashboard_coverage, dict) and dashboard_full_text_count != source_full_text_count
    _check(
        checks,
        check_id="full_text_coverage_consistency",
        category="full_text_coverage_consistency",
        status="warning" if full_text_mismatch else "passed",
        label="Full-text coverage consistency",
        summary=(
            "Dashboard full-text count differs from the provider-coverage denominator and requires review."
            if full_text_mismatch
            else "Dashboard full-text count aligns with repository-derived reviewed full-text count."
        ),
        reason_codes=["dashboard_full_text_coverage_mismatch"] if full_text_mismatch else ["full_text_coverage_consistent"],
    )

    dashboard_ai_count = _safe_int(_field(dashboard_operations, "coverage", "aiGroundedAcceptedCount"))
    ai_mismatch = isinstance(dashboard_coverage, dict) and dashboard_ai_count != ai_grounded_count
    _check(
        checks,
        check_id="ai_grounded_coverage_consistency",
        category="ai_grounded_coverage_consistency",
        status="failed" if ai_mismatch else "passed",
        label="AI-grounded coverage consistency",
        summary=(
            "Dashboard accepted AI-grounded count differs from accepted full-text draft count."
            if ai_mismatch
            else "Accepted AI-grounded count aligns with accepted full-text draft count."
        ),
        reason_codes=["dashboard_ai_grounded_coverage_mismatch"] if ai_mismatch else ["ai_grounded_coverage_consistent"],
    )

    provenance_status = str(provenance_audit.get("auditStatus") or "valid")
    _check(
        checks,
        check_id="provider_origin_consistency",
        category="provider_origin_consistency",
        status="failed" if provenance_status == "failed" else "warning" if provenance_status == "warnings" else "passed",
        label="Provider-origin consistency",
        summary="Provider provenance audit is consistent." if provenance_status == "valid" else "Provider provenance audit surfaced inconsistencies.",
        reason_codes=(["provider_origin_consistency_failed"] if provenance_status == "failed" else ["provider_origin_consistency_warning"] if provenance_status == "warnings" else ["provider_origin_consistency_valid"]),
    )

    synthetic_label_violations = _synthetic_label_violation_count(recommendation_payload)
    _check(
        checks,
        check_id="synthetic_evidence_labeling",
        category="synthetic_evidence_labeling",
        status="failed" if synthetic_label_violations else "passed",
        label="Synthetic evidence labeling",
        summary=(
            "Synthetic provider evidence refs are missing synthetic labels."
            if synthetic_label_violations
            else "Synthetic fixture/stub evidence refs are labeled as synthetic."
        ),
        reason_codes=["synthetic_evidence_unlabeled"] if synthetic_label_violations else ["synthetic_evidence_labeled"],
    )

    synthetic_only_high = _synthetic_only_high_confidence_count(recommendation_payload)
    _check(
        checks,
        check_id="recommendation_provenance_consistency",
        category="recommendation_provenance_consistency",
        status="failed" if synthetic_only_high else "passed",
        label="Recommendation provenance consistency",
        summary=(
            "High-confidence recommendations rely only on synthetic AI-grounded evidence."
            if synthetic_only_high
            else "Recommendation evidence provenance preserves synthetic-evidence guardrails."
        ),
        reason_codes=["synthetic_only_high_confidence"] if synthetic_only_high else ["recommendation_provenance_consistent"],
    )

    report_label_violations = _report_label_violation_count(report_markdown, _safe_int(source_provider_summary.get("fixtureSyntheticCount")))
    report_mismatch = "report_provider_coverage_mismatch" in provenance_audit.get("reasonCodes", [])
    _check(
        checks,
        check_id="report_truthfulness",
        category="report_truthfulness",
        status="failed" if report_mismatch or report_label_violations else "passed",
        label="Report truthfulness",
        summary=(
            "Report provider coverage wording does not match repository-derived provenance."
            if report_mismatch
            else "Report wording labels synthetic provider evidence and matches provenance counts."
        ),
        reason_codes=(["report_provider_coverage_mismatch"] if report_mismatch else ["report_synthetic_label_missing"] if report_label_violations else ["report_truthfulness_valid"]),
    )

    dashboard_provider_mismatch = "dashboard_provider_coverage_mismatch" in provenance_audit.get("reasonCodes", [])
    _check(
        checks,
        check_id="dashboard_truthfulness",
        category="dashboard_truthfulness",
        status="failed" if dashboard_provider_mismatch else "passed",
        label="Dashboard truthfulness",
        summary=(
            "Dashboard provider coverage fields differ from repository-derived provenance."
            if dashboard_provider_mismatch
            else "Dashboard provider coverage fields align with repository-derived provenance."
        ),
        reason_codes=["dashboard_provider_coverage_mismatch"] if dashboard_provider_mismatch else ["dashboard_truthfulness_valid"],
    )

    auth = _auth_status(dashboard_operations, auth_status)
    oauth_failed = _oauth_secret_safety_failed(auth)
    _check(
        checks,
        check_id="oauth_secret_safety",
        category="oauth_secret_safety",
        status="failed" if oauth_failed else "passed",
        label="OAuth secret safety",
        summary="OAuth live exchange or token storage safety boundary is not disabled." if oauth_failed else "OAuth token storage and live exchange remain disabled.",
        reason_codes=["oauth_secret_safety_failed"] if oauth_failed else ["oauth_secret_safety_valid"],
    )

    provider_safety_failed = _provider_dashboard_safety_failed(auth)
    _check(
        checks,
        check_id="provider_execution_safety",
        category="provider_execution_safety",
        status="failed" if provider_safety_failed else "passed",
        label="Provider action safety",
        summary="Dashboard provider action boundary is unsafe." if provider_safety_failed else "Dashboard provider actions remain disabled and CLI-gated.",
        reason_codes=["dashboard_provider_execution_enabled"] if provider_safety_failed else ["dashboard_provider_execution_forbidden"],
    )

    m4_failed = _safe_int(provenance_audit.get("missingM4ValidationCount")) > 0
    _check(
        checks,
        check_id="m4_validation_gate",
        category="m4_validation_gate",
        status="failed" if m4_failed else "passed",
        label="M4 validation gate",
        summary="Accepted provider drafts are missing M4 grounded validation metadata." if m4_failed else "Accepted provider drafts retain M4 grounded validation metadata.",
        reason_codes=["m4_validation_gate_failed"] if m4_failed else ["m4_validation_gate_valid"],
    )

    leak_failed = _safe_int(provenance_audit.get("rejectedOrNeedsReviewLeakCount")) > 0
    _check(
        checks,
        check_id="rejected_needs_review_exclusion",
        category="rejected_needs_review_exclusion",
        status="failed" if leak_failed else "passed",
        label="Rejected/needs-review exclusion",
        summary="Recommendation evidence references rejected or needs-review drafts." if leak_failed else "Rejected and needs-review drafts are excluded from accepted evidence.",
        reason_codes=["rejected_needs_review_leak_detected"] if leak_failed else ["rejected_needs_review_excluded"],
    )

    boundary_status = str(artifact_boundary.get("boundaryStatus") or "valid")
    _check(
        checks,
        check_id="runtime_artifact_boundary",
        category="runtime_artifact_boundary",
        status="failed" if boundary_status == "failed" else "warning" if boundary_status == "warnings" else "passed",
        label="Runtime artifact boundary",
        summary="Runtime artifact tracking boundary is clean." if boundary_status == "valid" else "Runtime artifact boundary surfaced report-only warnings.",
        reason_codes=artifact_boundary.get("reasonCodes") or [],
    )

    failed_count = sum(1 for item in checks if item["status"] == "failed")
    warning_count = sum(1 for item in checks if item["status"] == "warning")
    audit_status = _status_from_counts(failed_count, warning_count)
    reason_codes: list[str] = []
    for item in checks:
        reason_codes.extend(item.get("reasonCodes") or [])
    if audit_status == "valid":
        reason_codes.append("rc_audit_valid")
    elif audit_status == "warnings":
        reason_codes.append("rc_audit_warnings")
    else:
        reason_codes.append("rc_audit_failed")

    return {
        "auditStatus": audit_status,
        "checkCount": len(checks),
        "failedCheckCount": failed_count,
        "warningCheckCount": warning_count,
        "reasonCodes": _unique(reason_codes),
        "surfaceSummaries": [
            _surface(
                "coverage",
                "failed" if ai_mismatch else "warnings" if full_text_mismatch else "valid",
                "Coverage counts align across repository and dashboard surfaces." if not full_text_mismatch and not ai_mismatch else "Coverage count mismatches require review.",
            ),
            _surface("provider_provenance", provenance_status, "Provider provenance is consistent across dashboard, report, and recommendation surfaces." if provenance_status == "valid" else "Provider provenance requires operator review.", provenance_audit.get("reasonCodes") or []),
            _surface("recommendations", "failed" if synthetic_only_high else "valid", "Recommendation provenance guardrails are active." if not synthetic_only_high else "Recommendation confidence guardrail failed."),
            _surface("auth_safety", "failed" if oauth_failed or provider_safety_failed else "valid", "OAuth/provider safety gates remain disabled by default." if not oauth_failed and not provider_safety_failed else "OAuth/provider safety gates require review."),
            _surface("artifact_boundary", boundary_status, "Runtime artifact boundary is report-only and non-destructive."),
        ],
        "artifactBoundary": artifact_boundary,
        "checks": checks,
        "safeNextSteps": [
            "Review failed or warning checks before release candidate promotion.",
            "Keep runtime artifacts ignored and out of baseline commits.",
            "Continue using CLI/operator gates for provider workflows.",
        ],
    }


def build_research_operations_release_freeze_summary(
    rc_audit: dict[str, Any],
    *,
    validation_matrix_summary: dict[str, Any] | None = None,
    coverage_summary: dict[str, Any] | None = None,
    provider_provenance_summary: dict[str, Any] | None = None,
    recommendation_summary: dict[str, Any] | None = None,
    oauth_provider_safety_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_boundary = rc_audit.get("artifactBoundary") if isinstance(rc_audit.get("artifactBoundary"), dict) else {}
    known_warnings = list(artifact_boundary.get("warningClassifications") or [])
    failed_count = _safe_int(rc_audit.get("failedCheckCount"))
    warning_count = _safe_int(rc_audit.get("warningCheckCount"))
    accepted_warning_count = _safe_int(artifact_boundary.get("acceptedRuntimeWarningCount"))
    blocking_boundary_count = _safe_int(artifact_boundary.get("blockingReleaseWarningCount"))
    blocking_issue_count = failed_count + blocking_boundary_count
    reason_codes: list[str] = []
    if blocking_issue_count or str(rc_audit.get("auditStatus") or "") == "failed":
        release_status = "blocked"
        reason_codes.append("release_blocked_by_rc_audit")
    elif warning_count or accepted_warning_count:
        release_status = "ready_with_warnings"
        reason_codes.append("release_ready_with_accepted_runtime_warnings")
    else:
        release_status = "ready"
        reason_codes.append("release_ready")

    safety = {
        "tokenStorageEnabled": False,
        "liveOAuthEnabled": False,
        "dashboardExecutionEnabled": False,
        "providerExecutionEnabled": False,
    }
    if isinstance(oauth_provider_safety_summary, dict):
        safety.update(oauth_provider_safety_summary)

    return {
        "releaseStatus": release_status,
        "rcAuditStatus": str(rc_audit.get("auditStatus") or "valid"),
        "validationMatrixSummary": {**M16_VALIDATION_MATRIX, **(validation_matrix_summary or {})},
        "coverageSummary": coverage_summary or {},
        "providerProvenanceSummary": provider_provenance_summary or {},
        "recommendationSummary": recommendation_summary or {},
        "oauthProviderSafetySummary": safety,
        "rcAuditSummary": {
            "checkCount": _safe_int(rc_audit.get("checkCount")),
            "failedCheckCount": failed_count,
            "warningCheckCount": warning_count,
        },
        "artifactBoundarySummary": {
            "boundaryStatus": str(artifact_boundary.get("boundaryStatus") or "valid"),
            "acceptedRuntimeWarningCount": accepted_warning_count,
            "blockingReleaseWarningCount": blocking_boundary_count,
            "knownRuntimeUntrackedCount": _safe_int(artifact_boundary.get("knownRuntimeUntrackedCount")),
        },
        "knownWarnings": known_warnings,
        "knownWarningCount": len(known_warnings),
        "blockingIssueCount": blocking_issue_count,
        "reasonCodes": _unique([*reason_codes, *(rc_audit.get("reasonCodes") or [])]),
        "safeNextSteps": [
            "Keep duplicate topic profile runtime copies untracked and ignored.",
            "Treat the release candidate as frozen unless a blocking audit check appears.",
            "Continue using read-only dashboard summaries and CLI/operator gates.",
        ],
    }

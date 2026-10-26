from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from app.services import evidence_manifest_verifier, paper_review

BOUNDED_FETCH_ENV_VAR = "ALLOW_BOUNDED_PDF_FETCH"
REPLAY_STATUSES = [
    "manifest_loaded",
    "manifest_invalid",
    "local_already_verified",
    "replay_required",
    "replay_blocked_operator_gate",
    "replay_blocked_env_gate",
    "replay_dry_run_planned",
    "replay_candidate_mismatch",
    "replay_pdf_rejected",
    "replay_pdf_downloaded",
    "replay_pdf_hash_mismatch",
    "replay_extracted",
    "replay_extraction_failed",
    "replay_reviewed",
    "replay_review_validation_failed",
    "replay_verified",
    "failed",
]


RefreshFunc = Callable[..., dict[str, Any]]
VerifyFunc = Callable[..., dict[str, Any]]


def _manifest_or_error(path: str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        return None, ["manifest_not_found"]
    try:
        manifest = evidence_manifest_verifier.load_manifest(manifest_path)
    except Exception as exc:
        return None, [f"manifest_parse_error:{type(exc).__name__}"]
    return manifest, evidence_manifest_verifier.validate_manifest_payload(manifest)


def _known_exclusion_ids(manifest: dict[str, Any]) -> list[str]:
    return [
        str(exclusion.get("paper_id") or "").strip()
        for exclusion in manifest.get("known_exclusions") or []
        if str(exclusion.get("paper_id") or "").strip()
    ]


def _acquisition(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest.get("acquisition") if isinstance(manifest.get("acquisition"), dict) else {}


def _effective_bounds(
    manifest: dict[str, Any],
    *,
    download_limit: int | None = None,
    max_pdf_bytes: int | None = None,
    timeout_seconds: int | None = None,
    allow_domain: str | None = None,
) -> dict[str, Any]:
    acquisition = _acquisition(manifest)
    manifest_limit = int(acquisition.get("download_limit") or 1)
    manifest_max_bytes = int(acquisition.get("max_pdf_bytes") or 25_000_000)
    manifest_timeout = int(acquisition.get("timeout_seconds") or 30)
    manifest_domain = str(acquisition.get("allow_domain") or manifest.get("pdf_domain") or "arxiv.org").strip()
    return {
        "download_limit": int(download_limit if download_limit is not None else manifest_limit),
        "max_pdf_bytes": int(max_pdf_bytes if max_pdf_bytes is not None else manifest_max_bytes),
        "download_timeout": int(timeout_seconds if timeout_seconds is not None else manifest_timeout),
        "allow_domain": str(allow_domain or manifest_domain).strip(),
        "manifest_download_limit": manifest_limit,
        "manifest_max_pdf_bytes": manifest_max_bytes,
        "manifest_download_timeout": manifest_timeout,
    }


def _bounded_parameters(manifest: dict[str, Any], bounds: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic_id": manifest.get("topic_id"),
        "paper_id": manifest.get("paper_id"),
        "pdf_url": manifest.get("pdf_url"),
        "allow_domain": bounds["allow_domain"],
        "download_limit": bounds["download_limit"],
        "max_pdf_bytes": bounds["max_pdf_bytes"],
        "download_timeout": bounds["download_timeout"],
        "exclude_paper_ids": _known_exclusion_ids(manifest),
        "expected_pdf_sha256": manifest.get("pdf_sha256"),
        "expected_source_text_sha256": manifest.get("source_text_sha256"),
    }


def _base_result(
    status: str,
    *,
    manifest_path: str | Path,
    manifest: dict[str, Any] | None = None,
    bounds: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    manifest = manifest or {}
    bounds = bounds or _effective_bounds(manifest) if manifest else {}
    return {
        "status": status,
        "manifest_path": str(manifest_path),
        "paper_id": manifest.get("paper_id"),
        "topic_id": manifest.get("topic_id"),
        "target_pdf_url": manifest.get("pdf_url"),
        "replay_required": status
        in {
            "replay_required",
            "replay_blocked_operator_gate",
            "replay_blocked_env_gate",
            "replay_dry_run_planned",
            "replay_candidate_mismatch",
            "replay_pdf_rejected",
            "replay_pdf_downloaded",
            "replay_pdf_hash_mismatch",
            "replay_extracted",
            "replay_extraction_failed",
            "replay_reviewed",
            "replay_review_validation_failed",
            "failed",
        },
        "write_attempted": False,
        "network_attempted": False,
        "local_verified_before": False,
        "local_verified_after": False,
        "safe_to_claim_reviewed": False,
        "safe_to_claim_clean_clone_reproducible": False,
        "reasons": reasons or [],
        "supported_statuses": REPLAY_STATUSES,
        "bounded_parameters": _bounded_parameters(manifest, bounds) if manifest and bounds else {},
        "planned_command": None,
    }


def _validate_replay_manifest(manifest: dict[str, Any], bounds: dict[str, Any]) -> list[str]:
    reasons = evidence_manifest_verifier.validate_manifest_payload(manifest)
    parsed = urlparse(str(manifest.get("pdf_url") or ""))
    allow_domain = str(bounds["allow_domain"] or "").lower()
    manifest_domain = str(manifest.get("pdf_domain") or "").lower()
    if parsed.netloc.lower() != allow_domain or manifest_domain != allow_domain:
        reasons.append("domain_not_allowlisted")
    if str(manifest.get("paper_id") or "").strip() in set(_known_exclusion_ids(manifest)):
        reasons.append("target_is_known_excluded")
    if int(bounds["download_limit"]) != 1:
        reasons.append("download_limit_must_be_1")
    if int(bounds["max_pdf_bytes"]) > int(bounds["manifest_max_pdf_bytes"]):
        reasons.append("max_pdf_bytes_looser_than_manifest")
    if int(bounds["download_timeout"]) > int(bounds["manifest_download_timeout"]):
        reasons.append("download_timeout_looser_than_manifest")
    if int(bounds["max_pdf_bytes"]) <= 0:
        reasons.append("max_pdf_bytes_required")
    if int(bounds["download_timeout"]) <= 0:
        reasons.append("download_timeout_required")
    return sorted(set(reasons))


def _refresh_kwargs(manifest: dict[str, Any], bounds: dict[str, Any], *, dry_run: bool, env: Mapping[str, str] | None) -> dict[str, Any]:
    paper_id = str(manifest["paper_id"])
    return {
        "unique_limit": 50,
        "dry_run": dry_run,
        "use_full_text": True,
        "fetch_missing_pdf": True,
        "download_limit": int(bounds["download_limit"]),
        "provider": "arxiv",
        "allow_domains": [str(bounds["allow_domain"])],
        "max_pdf_bytes": int(bounds["max_pdf_bytes"]),
        "download_timeout": int(bounds["download_timeout"]),
        "extract_after_fetch": True,
        "allow_bounded_pdf_fetch": not dry_run,
        "exclude_paper_ids": _known_exclusion_ids(manifest),
        "target_paper_id": paper_id,
        "expected_pdf_sha256_by_paper_id": {paper_id: str(manifest.get("pdf_sha256") or "")},
        "expected_source_text_sha256_by_paper_id": {paper_id: str(manifest.get("source_text_sha256") or "")},
        "env": env,
    }


def _plan_reasons(summary: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    plan_summary = summary.get("prioritization", {}).get("summary", {})
    if plan_summary.get("target_found") is not True:
        reasons.append("target_not_found")
    if int(plan_summary.get("planned_download_count") or 0) != 1:
        reasons.append("planned_download_count_not_one")
    target_entries = [
        candidate
        for candidate in summary.get("prioritization", {}).get("candidates", [])
        if str(candidate.get("paper_id") or "") == str(manifest.get("paper_id") or "")
    ]
    if len(target_entries) != 1:
        reasons.append("target_candidate_count_not_one")
    else:
        target = target_entries[0]
        if target.get("is_acquisition_candidate") is not True:
            reasons.append("target_not_acquisition_candidate")
        if str(target.get("pdf_url") or "") != str(manifest.get("pdf_url") or ""):
            reasons.append("target_pdf_url_mismatch")
        if str(target.get("pdf_url_domain") or "") != str(manifest.get("pdf_domain") or ""):
            reasons.append("target_pdf_domain_mismatch")
        if str(target.get("planned_action") or "") != "download_pdf":
            reasons.append("target_planned_action_mismatch")
    for candidate in summary.get("prioritization", {}).get("candidates", []):
        if str(candidate.get("paper_id") or "") in set(_known_exclusion_ids(manifest)) and candidate.get("is_acquisition_candidate"):
            reasons.append("known_exclusion_selected")
    return sorted(set(reasons))


def _status_from_refresh(summary: dict[str, Any]) -> tuple[str, list[str]]:
    acquisition = summary.get("pdf_acquisition") or {}
    rejected_reasons = (acquisition.get("audit_summary") or {}).get("rejected_by_reason") or {}
    failed_reasons = (acquisition.get("audit_summary") or {}).get("failed_by_reason") or {}
    extraction_failed_reasons = (acquisition.get("audit_summary") or {}).get("extraction_failed_by_reason") or {}
    if rejected_reasons.get("pdf_hash_mismatch"):
        return "replay_pdf_hash_mismatch", ["pdf_hash_mismatch"]
    if rejected_reasons:
        return "replay_pdf_rejected", [f"pdf_rejected:{reason}" for reason in sorted(rejected_reasons)]
    if extraction_failed_reasons.get("source_text_hash_mismatch"):
        return "replay_review_validation_failed", ["source_text_sha256_mismatch"]
    if int(acquisition.get("extraction_failed_count") or 0) > 0:
        return "replay_extraction_failed", [f"extraction_failed:{reason}" for reason in sorted(extraction_failed_reasons or {"unknown": 1})]
    if failed_reasons:
        return "failed", [f"pdf_failed:{reason}" for reason in sorted(failed_reasons)]
    if int(acquisition.get("downloaded_count") or 0) > 0:
        return "replay_reviewed", []
    return "replay_review_validation_failed", ["replay_did_not_download_or_verify"]


def replay_manifest(
    manifest_path: str | Path,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    download_limit: int | None = None,
    max_pdf_bytes: int | None = None,
    timeout_seconds: int | None = None,
    allow_domain: str | None = None,
    refresh_func: RefreshFunc = paper_review.refresh_reviews_for_topic,
    verify_func: VerifyFunc = evidence_manifest_verifier.verify_manifest,
) -> dict[str, Any]:
    manifest, load_reasons = _manifest_or_error(manifest_path)
    if manifest is None:
        return _base_result("manifest_invalid", manifest_path=manifest_path, reasons=load_reasons)
    bounds = _effective_bounds(
        manifest,
        download_limit=download_limit,
        max_pdf_bytes=max_pdf_bytes,
        timeout_seconds=timeout_seconds,
        allow_domain=allow_domain,
    )
    reasons = _validate_replay_manifest(manifest, bounds)
    if reasons:
        return _base_result("manifest_invalid", manifest_path=manifest_path, manifest=manifest, bounds=bounds, reasons=reasons)

    before = verify_func(manifest_path)
    if before.get("status") == "verified":
        result = _base_result("local_already_verified", manifest_path=manifest_path, manifest=manifest, bounds=bounds)
        result.update(
            {
                "replay_required": False,
                "local_verified_before": True,
                "local_verified_after": True,
                "safe_to_claim_reviewed": True,
                "verification_before": before,
                "verification_after": before,
            }
        )
        return result

    plan = evidence_manifest_verifier.build_replay_plan(manifest_path)
    if dry_run:
        summary = refresh_func(
            str(manifest["topic_id"]),
            **_refresh_kwargs(manifest, bounds, dry_run=True, env=env),
        )
        plan_failures = _plan_reasons(summary, manifest)
        status = "replay_candidate_mismatch" if plan_failures else "replay_dry_run_planned"
        result = _base_result(status, manifest_path=manifest_path, manifest=manifest, bounds=bounds, reasons=plan_failures)
        result.update(
            {
                "local_verified_before": False,
                "local_verified_after": False,
                "planned_command": plan.get("command"),
                "dry_run_plan": summary,
                "replay_required": True,
            }
        )
        return result

    effective_env = env if env is not None else os.environ
    if str(effective_env.get(BOUNDED_FETCH_ENV_VAR, "")).strip() != "1":
        result = _base_result("replay_blocked_env_gate", manifest_path=manifest_path, manifest=manifest, bounds=bounds, reasons=["env_var_required"])
        result["planned_command"] = plan.get("command")
        result["verification_before"] = before
        return result

    dry_summary = refresh_func(
        str(manifest["topic_id"]),
        **_refresh_kwargs(manifest, bounds, dry_run=True, env=effective_env),
    )
    plan_failures = _plan_reasons(dry_summary, manifest)
    if plan_failures:
        result = _base_result("replay_candidate_mismatch", manifest_path=manifest_path, manifest=manifest, bounds=bounds, reasons=plan_failures)
        result["dry_run_plan"] = dry_summary
        result["planned_command"] = plan.get("command")
        result["verification_before"] = before
        return result

    write_summary = refresh_func(
        str(manifest["topic_id"]),
        **_refresh_kwargs(manifest, bounds, dry_run=False, env=effective_env),
    )
    interim_status, interim_reasons = _status_from_refresh(write_summary)
    after = verify_func(manifest_path)
    if after.get("status") == "verified":
        result = _base_result("replay_verified", manifest_path=manifest_path, manifest=manifest, bounds=bounds)
        result.update(
            {
                "local_verified_before": False,
                "local_verified_after": True,
                "safe_to_claim_reviewed": True,
                "write_attempted": True,
                "network_attempted": True,
                "replay_required": False,
                "dry_run_plan": dry_summary,
                "write_summary": write_summary,
                "verification_before": before,
                "verification_after": after,
            }
        )
        return result

    result = _base_result(interim_status, manifest_path=manifest_path, manifest=manifest, bounds=bounds, reasons=interim_reasons or after.get("reasons") or [])
    result.update(
        {
            "write_attempted": True,
            "network_attempted": True,
            "dry_run_plan": dry_summary,
            "write_summary": write_summary,
            "verification_before": before,
            "verification_after": after,
        }
    )
    return result

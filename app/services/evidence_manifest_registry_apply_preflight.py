from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain import constants
from app.services import (
    evidence_manifest_registry,
    evidence_manifest_registry_commit_guard,
    evidence_manifest_registry_write,
)

TARGET_REGISTRY_PATH = "data/evidence_manifests/registry.json"
TRACKED_WRITE_ENV_GATE = "ALLOW_TRACKED_REGISTRY_WRITE"
TRACKED_WRITE_ENV_VALUE = "1"

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "production_apply_proposal_missing": ("Proposal file does not exist.", True, True, "Generate a fresh temp proposal."),
    "production_apply_proposal_invalid": ("Proposal is invalid for production apply preflight.", True, True, "Regenerate a valid temp proposal."),
    "production_apply_proposal_timestamp_missing": ("Proposal generated timestamp is missing.", True, True, "Regenerate a fresh temp proposal with proposal_generated_at."),
    "production_apply_proposal_timestamp_invalid": ("Proposal generated timestamp is invalid.", True, True, "Regenerate a proposal with a valid timezone-aware timestamp."),
    "production_apply_proposal_stale": ("Proposal is stale for this preflight.", True, True, "Regenerate the proposal in the current session."),
    "production_apply_proposal_inside_repo": ("Proposal file is inside the repository.", True, True, "Keep proposal files in a temp location outside the repository."),
    "production_apply_proposal_in_registry_dir": ("Proposal file is under data/evidence_manifests.", True, True, "Keep proposal files outside the registry directory."),
    "production_apply_proposal_symlink_disallowed": ("Proposal path uses a symlink.", True, True, "Use a direct temp proposal path without symlinks."),
    "production_apply_proposal_path_traversal": ("Proposal path contains traversal.", True, True, "Use a normalized temp proposal path."),
    "production_apply_manifest_hash_stale": ("A proposed registry manifest hash no longer matches current manifest bytes.", True, True, "Regenerate the proposal after manifest verification."),
    "production_apply_registry_already_exists": ("Target registry already exists.", True, True, "Stop or use a separately approved overwrite phase."),
    "production_apply_overwrite_not_approved": ("Registry overwrite is not approved in this no-write phase.", True, True, "Keep this phase no-write and review overwrite policy later."),
    "production_apply_dry_run_not_ready": ("Existing registry writer dry-run did not report readiness.", True, True, "Fix dry-run blockers and rerun preflight."),
    "production_apply_commit_guard_blocked": ("Commit guard blocked production apply preflight.", True, True, "Fix commit guard blockers and rerun preflight."),
    "production_apply_commit_guard_warning_unaccepted": ("Commit guard warning was not acknowledged.", True, True, "Review and acknowledge warnings before rerunning."),
    "production_apply_known_untracked_unacknowledged": ("Known duplicate untracked files were not acknowledged.", True, True, "Acknowledge or remove known duplicate untracked files."),
    "production_apply_dirty_worktree": ("Tracked working tree modifications are present.", True, True, "Commit, stash, or remove unrelated tracked changes."),
    "production_apply_runtime_artifact_present": ("Runtime artifact is present in the guarded commit scope.", True, True, "Remove or unstage runtime artifacts."),
    "production_apply_backup_policy_missing": ("Backup policy acknowledgement is missing.", True, True, "Review and acknowledge backup policy."),
    "production_apply_rollback_plan_missing": ("Rollback plan acknowledgement is missing.", True, True, "Review and acknowledge rollback plan."),
    "production_apply_post_write_diff_review_missing": ("Post-write diff review acknowledgement is missing.", True, True, "Review and acknowledge post-write diff policy."),
    "production_apply_commit_plan_missing": ("Commit plan acknowledgement is missing.", True, True, "Review and acknowledge commit policy."),
    "production_apply_push_deferred_missing": ("Push deferral acknowledgement is missing.", True, True, "Acknowledge that push is deferred."),
    "production_apply_operator_flag_required": ("Operator approval flag is missing.", True, True, "Pass the operator approval flag after review."),
    "production_apply_env_gate_required": ("ALLOW_TRACKED_REGISTRY_WRITE=1 is missing.", True, True, "Set the env gate only for the no-write preflight."),
    "production_apply_confirmation_mismatch": ("Registry confirmation path does not exactly match the target.", True, True, "Use exact data/evidence_manifests/registry.json."),
    "production_apply_dashboard_trigger_forbidden": ("Dashboard/API trigger is forbidden.", True, False, "Use the CLI-only preflight."),
    "production_apply_live_replay_forbidden": ("Live replay is forbidden.", True, False, "Do not run live replay in apply preflight."),
    "production_apply_provider_oauth_forbidden": ("Provider/OAuth/external AI is forbidden.", True, False, "Keep provider/OAuth/external AI disabled."),
    "unknown_error": ("Unexpected production apply preflight error.", True, True, "Stop and diagnose before retrying."),
}


def _reason(code: str, **context: Any) -> dict[str, Any]:
    message, blocking, operator_fixable, safe_next_action = _REASON_META.get(code, _REASON_META["unknown_error"])
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "blocking": blocking,
        "operator_fixable": operator_fixable,
        "safe_next_action": safe_next_action,
    }
    payload.update({key: value for key, value in context.items() if value is not None})
    return payload


def _add_reason(report: dict[str, Any], code: str, **context: Any) -> None:
    key = (code, context.get("file_path"), context.get("registry_path"))
    seen = report.setdefault("_reason_keys", set())
    if key in seen:
        return
    seen.add(key)
    report["reasons"].append(_reason(code, **context))


def _normalize_path(value: str | Path | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = Path(text).as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _has_traversal(path: Path) -> bool:
    return any(part == ".." for part in path.parts)


def _has_symlink_component(path: Path, root: Path | None = None) -> bool:
    current = path
    while True:
        try:
            if current.exists() and current.is_symlink():
                return True
        except OSError:
            return True
        if current.parent == current or (root is not None and current == root):
            return False
        current = current.parent


def _resolve_project_path(raw_path: str | Path, *, project_root: Path) -> Path:
    path = Path(str(raw_path))
    return path if path.is_absolute() else project_root / path


def _read_proposal(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "production_apply_proposal_missing"
    except json.JSONDecodeError:
        return None, "production_apply_proposal_invalid"
    except OSError:
        return None, "unknown_error"
    if not isinstance(payload, dict):
        return None, "production_apply_proposal_invalid"
    return payload, None


def _parse_generated_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _proposal_age_minutes(generated_at_value: Any) -> float | None:
    generated_at = _parse_generated_at(generated_at_value)
    if generated_at is None:
        return None
    age = datetime.now(UTC) - generated_at
    return round(age.total_seconds() / 60, 3)


def _check_proposed_registry(proposed_registry: dict[str, Any], *, topic_id: str | None, project_root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="xrml-apply-preflight-registry-check-") as temp_dir:
        registry_path = Path(temp_dir) / "proposed-registry.json"
        registry_path.write_text(json.dumps(proposed_registry, ensure_ascii=False, indent=2), encoding="utf-8")
        result = evidence_manifest_registry.check_manifest_registry(registry_path, topic_id=topic_id, project_root=project_root)
    sanitized = deepcopy(result)
    sanitized["registry_path"] = "temp:proposed-registry.json"
    return sanitized


def _manifest_hashes_fresh(proposed_registry: dict[str, Any], *, project_root: Path, report: dict[str, Any]) -> bool:
    entries = proposed_registry.get("manifest_entries") if isinstance(proposed_registry.get("manifest_entries"), list) else []
    fresh = True
    report["manifest_entry_count"] = len(entries)
    report["fresh_manifest_entries"] = 0
    report["stale_manifest_entries"] = []
    for entry in entries:
        if not isinstance(entry, dict):
            fresh = False
            _add_reason(report, "production_apply_manifest_hash_stale")
            report["stale_manifest_entries"].append(
                {
                    "paper_id": "",
                    "manifest_path": "",
                    "expected_sha256": "",
                    "actual_sha256": None,
                    "status": "invalid_entry",
                }
            )
            continue
        raw_manifest_path = str(entry.get("manifest_path") or "")
        manifest_path = _resolve_project_path(raw_manifest_path, project_root=project_root)
        try:
            actual = evidence_manifest_registry._raw_sha256(manifest_path)  # noqa: SLF001 - shared checker hashing policy
        except OSError:
            actual = None
        expected = str(entry.get("manifest_sha256") or "").strip()
        if not actual or actual.lower() != expected.lower():
            fresh = False
            _add_reason(
                report,
                "production_apply_manifest_hash_stale",
                file_path=raw_manifest_path,
            )
            report["stale_manifest_entries"].append(
                {
                    "paper_id": str(entry.get("paper_id") or ""),
                    "manifest_path": raw_manifest_path,
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                    "status": "missing" if actual is None else "stale",
                }
            )
        else:
            report["fresh_manifest_entries"] += 1
    return fresh and bool(entries)


def _proposal_safety_flags_valid(proposal: dict[str, Any], report: dict[str, Any]) -> None:
    safety_flags = proposal.get("safety_flags") if isinstance(proposal.get("safety_flags"), dict) else {}
    if safety_flags.get("live_replay_executed") is True:
        _add_reason(report, "production_apply_live_replay_forbidden")
    if safety_flags.get("provider_oauth_external_ai_used") is True:
        _add_reason(report, "production_apply_provider_oauth_forbidden")
    if safety_flags.get("dashboard_trigger_used") is True:
        _add_reason(report, "production_apply_dashboard_trigger_forbidden")
    expected = {
        "proposal_only": True,
        "tracked_registry_written": False,
        "live_replay_executed": False,
        "provider_oauth_external_ai_used": False,
        "dashboard_trigger_used": False,
    }
    if any(safety_flags.get(key) is not value for key, value in expected.items()):
        _add_reason(report, "production_apply_proposal_invalid")


def _base_report(
    *,
    proposal_path: str | Path,
    registry_path: str | Path,
    topic_id: str | None,
    operator_approved: bool,
    env_gate_value: str | None,
    confirm_registry_path: str | Path | None,
    root: Path,
) -> dict[str, Any]:
    actual_env_value = env_gate_value if env_gate_value is not None else os.getenv(TRACKED_WRITE_ENV_GATE)
    confirm_matched = _normalize_path(confirm_registry_path) == TARGET_REGISTRY_PATH
    return {
        "status": "failed",
        "proposal_path": str(proposal_path),
        "proposal_generated_at": None,
        "proposal_in_repo": False,
        "proposal_under_registry_dir": False,
        "proposal_path_is_symlink": False,
        "proposal_path_has_traversal": False,
        "max_proposal_age_minutes": None,
        "registry_path": _normalize_path(registry_path),
        "topic_id": topic_id,
        "project_root": str(root),
        "proposal_age_minutes": None,
        "proposal_fresh": False,
        "registry_absent": False,
        "registry_check_status": None,
        "registry_check_report": None,
        "manifest_hashes_fresh": False,
        "manifest_entry_count": 0,
        "fresh_manifest_entries": 0,
        "stale_manifest_entries": [],
        "writer_dry_run_status": None,
        "writer_dry_run_report": None,
        "commit_guard_status": None,
        "commit_guard_report": None,
        "commit_guard_reasons": [],
        "known_untracked_files": [],
        "operator_flag_present": bool(operator_approved),
        "env_gate_present": actual_env_value == TRACKED_WRITE_ENV_VALUE,
        "confirm_path_matched": confirm_matched,
        "acknowledgement_status": {},
        "known_untracked_acknowledged": False,
        "commit_guard_warnings_acknowledged": False,
        "backup_policy_acknowledged": False,
        "rollback_plan_acknowledged": False,
        "post_write_diff_review_acknowledged": False,
        "commit_plan_acknowledged": False,
        "push_deferred_acknowledged": False,
        "preflight_ready": False,
        "eligible_for_operator_review": False,
        "safe_to_run_future_apply": False,
        "would_apply": False,
        "temp_harness_apply_attempted": False,
        "registry_written": False,
        "write_attempted": False,
        "db_storage_write_attempted": False,
        "network_attempted": False,
        "live_replay_executed": False,
        "provider_oauth_external_ai_used": False,
        "dashboard_trigger_used": False,
        "safe_to_claim_real_registry_exists": False,
        "safe_to_claim_registry_lifecycle_complete": False,
        "reasons": [],
    }


def _map_writer_reasons(report: dict[str, Any], writer_report: dict[str, Any]) -> None:
    for reason in writer_report.get("reasons", []):
        code = reason.get("code")
        if code == "registry_write_operator_flag_required":
            _add_reason(report, "production_apply_operator_flag_required")
        elif code == "registry_write_env_gate_required":
            _add_reason(report, "production_apply_env_gate_required")
        elif code == "registry_write_confirmation_mismatch":
            _add_reason(report, "production_apply_confirmation_mismatch")
        elif code == "registry_write_live_replay_forbidden":
            _add_reason(report, "production_apply_live_replay_forbidden")
        elif code == "registry_write_dashboard_trigger_forbidden":
            _add_reason(report, "production_apply_dashboard_trigger_forbidden")
        elif str(code or "").startswith("registry_write_proposal_"):
            _add_reason(report, "production_apply_proposal_invalid")


def _map_commit_guard(report: dict[str, Any], guard_report: dict[str, Any]) -> None:
    for reason in guard_report.get("reasons", []):
        code = reason.get("code")
        if code == "registry_commit_guard_known_duplicate_file_present":
            continue
        if code == "registry_commit_guard_unexpected_tracked_modification":
            _add_reason(report, "production_apply_dirty_worktree", file_path=reason.get("file_path"))
        if code in {
            "registry_commit_guard_forbidden_runtime_artifact",
            "registry_commit_guard_ignored_runtime_artifact_staged",
        }:
            _add_reason(report, "production_apply_runtime_artifact_present", file_path=reason.get("file_path"))
    if guard_report.get("status") == "registry_commit_guard_blocked":
        _add_reason(report, "production_apply_commit_guard_blocked")


def run_production_apply_preflight(
    *,
    proposal_path: str | Path,
    registry_path: str | Path = TARGET_REGISTRY_PATH,
    topic_id: str | None = None,
    operator_approved: bool = False,
    confirm_registry_path: str | Path | None = None,
    env_gate_value: str | None = None,
    max_proposal_age_minutes: int = 60,
    require_current_session: bool = True,
    acknowledge_known_untracked: bool = False,
    acknowledge_commit_guard_warnings: bool = False,
    backup_policy_acknowledged: bool = False,
    rollback_plan_acknowledged: bool = False,
    post_write_diff_review_acknowledged: bool = False,
    commit_plan_acknowledged: bool = False,
    push_deferred_acknowledged: bool = False,
    strict: bool = False,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    del require_current_session
    root = Path(project_root or constants.PROJECT_ROOT).resolve()
    report = _base_report(
        proposal_path=proposal_path,
        registry_path=registry_path,
        topic_id=topic_id,
        operator_approved=operator_approved,
        env_gate_value=env_gate_value,
        confirm_registry_path=confirm_registry_path,
        root=root,
    )
    report.update(
        {
            "max_proposal_age_minutes": max_proposal_age_minutes,
            "known_untracked_acknowledged": bool(acknowledge_known_untracked),
            "commit_guard_warnings_acknowledged": bool(acknowledge_commit_guard_warnings),
            "backup_policy_acknowledged": bool(backup_policy_acknowledged),
            "rollback_plan_acknowledged": bool(rollback_plan_acknowledged),
            "post_write_diff_review_acknowledged": bool(post_write_diff_review_acknowledged),
            "commit_plan_acknowledged": bool(commit_plan_acknowledged),
            "push_deferred_acknowledged": bool(push_deferred_acknowledged),
            "acknowledgement_status": {
                "known_untracked": bool(acknowledge_known_untracked),
                "commit_guard_warnings": bool(acknowledge_commit_guard_warnings),
                "backup_policy": bool(backup_policy_acknowledged),
                "rollback_plan": bool(rollback_plan_acknowledged),
                "post_write_diff_review": bool(post_write_diff_review_acknowledged),
                "commit_plan": bool(commit_plan_acknowledged),
                "push_deferred": bool(push_deferred_acknowledged),
            },
        }
    )

    try:
        if _normalize_path(registry_path) != TARGET_REGISTRY_PATH:
            _add_reason(report, "production_apply_confirmation_mismatch", registry_path=str(registry_path))
        if not report["operator_flag_present"]:
            _add_reason(report, "production_apply_operator_flag_required")
        if not report["env_gate_present"]:
            _add_reason(report, "production_apply_env_gate_required")
        if not report["confirm_path_matched"]:
            _add_reason(report, "production_apply_confirmation_mismatch", confirm_registry_path=str(confirm_registry_path))

        target_registry = _resolve_project_path(TARGET_REGISTRY_PATH, project_root=root)
        report["registry_absent"] = not target_registry.exists()
        if not report["registry_absent"]:
            _add_reason(report, "production_apply_registry_already_exists", registry_path=str(target_registry))
            _add_reason(report, "production_apply_overwrite_not_approved", registry_path=str(target_registry))

        raw_proposal_file = Path(str(proposal_path))
        report["proposal_path_has_traversal"] = _has_traversal(raw_proposal_file)
        if report["proposal_path_has_traversal"]:
            _add_reason(report, "production_apply_proposal_path_traversal", proposal_path=str(proposal_path))
        proposal_file = raw_proposal_file.resolve(strict=False)
        registry_dir = (root / "data" / "evidence_manifests").resolve(strict=False)
        report["proposal_in_repo"] = _inside(proposal_file, root)
        report["proposal_under_registry_dir"] = _inside(proposal_file, registry_dir)
        report["proposal_path_is_symlink"] = _has_symlink_component(raw_proposal_file)
        if report["proposal_in_repo"]:
            _add_reason(report, "production_apply_proposal_inside_repo", proposal_path=str(proposal_file))
        if report["proposal_under_registry_dir"]:
            _add_reason(report, "production_apply_proposal_in_registry_dir", proposal_path=str(proposal_file))
        if report["proposal_path_is_symlink"]:
            _add_reason(report, "production_apply_proposal_symlink_disallowed", proposal_path=str(raw_proposal_file))
        proposal, read_error = _read_proposal(proposal_file)
        if read_error:
            _add_reason(report, read_error)
            proposal = None

        if proposal is not None:
            report["topic_id"] = topic_id or str(
                (proposal.get("proposed_registry") if isinstance(proposal.get("proposed_registry"), dict) else {}).get("topic_id") or ""
            ) or topic_id
            generated_at_present = "proposal_generated_at" in proposal
            generated_at_raw = proposal.get("proposal_generated_at")
            report["proposal_generated_at"] = generated_at_raw if generated_at_present else None
            age = _proposal_age_minutes(generated_at_raw)
            report["proposal_age_minutes"] = age
            report["proposal_fresh"] = age is not None and 0 <= age <= max_proposal_age_minutes
            if not generated_at_present:
                _add_reason(report, "production_apply_proposal_timestamp_missing")
            elif age is None:
                _add_reason(report, "production_apply_proposal_timestamp_invalid")
            if not report["proposal_fresh"]:
                _add_reason(report, "production_apply_proposal_stale")
            _proposal_safety_flags_valid(proposal, report)
            if proposal.get("proposal_kind") != "evidence_manifest_registry_proposal" or proposal.get("proposal_status") not in {"proposal_generated", "proposal_verified"}:
                _add_reason(report, "production_apply_proposal_invalid")

            proposed_registry = proposal.get("proposed_registry")
            if isinstance(proposed_registry, dict):
                registry_check_report = _check_proposed_registry(proposed_registry, topic_id=report["topic_id"], project_root=root)
                report["registry_check_report"] = registry_check_report
                report["registry_check_status"] = registry_check_report.get("status")
                if registry_check_report.get("status") != "registry_verified":
                    _add_reason(report, "production_apply_dry_run_not_ready")
                report["manifest_hashes_fresh"] = _manifest_hashes_fresh(proposed_registry, project_root=root, report=report)
            else:
                _add_reason(report, "production_apply_proposal_invalid")

        writer_report = evidence_manifest_registry_write.validate_registry_write_dry_run(
            proposal_path=proposal_path,
            registry_path=registry_path,
            topic_id=report["topic_id"],
            dry_run=True,
            operator_approved=operator_approved,
            env_gate_value=env_gate_value,
            confirm_registry_path=confirm_registry_path,
            project_root=root,
            strict=strict,
        )
        report["writer_dry_run_report"] = writer_report
        report["writer_dry_run_status"] = writer_report.get("status")
        if writer_report.get("status") != "registry_write_dry_run_ready":
            _add_reason(report, "production_apply_dry_run_not_ready")
            _map_writer_reasons(report, writer_report)

        guard_report = evidence_manifest_registry_commit_guard.check_registry_commit_guard(
            repo_root=root,
            expected_registry_path=TARGET_REGISTRY_PATH,
            mode="pre_apply",
            allow_known_untracked=True,
            strict=strict,
        )
        report["commit_guard_report"] = guard_report
        report["commit_guard_status"] = guard_report.get("status")
        report["commit_guard_reasons"] = guard_report.get("reasons", [])
        _map_commit_guard(report, guard_report)

        report["known_untracked_files"] = [
            str(reason.get("file_path") or "")
            for reason in guard_report.get("reasons", [])
            if reason.get("code") == "registry_commit_guard_known_duplicate_file_present" and reason.get("file_path")
        ]
        known_duplicate_present = any(
            reason.get("code") == "registry_commit_guard_known_duplicate_file_present"
            for reason in guard_report.get("reasons", [])
        )
        if known_duplicate_present and not acknowledge_known_untracked:
            _add_reason(report, "production_apply_known_untracked_unacknowledged")
        if guard_report.get("status") == "registry_commit_guard_warning" and not acknowledge_commit_guard_warnings:
            _add_reason(report, "production_apply_commit_guard_warning_unaccepted")

        if not backup_policy_acknowledged:
            _add_reason(report, "production_apply_backup_policy_missing")
        if not rollback_plan_acknowledged:
            _add_reason(report, "production_apply_rollback_plan_missing")
        if not post_write_diff_review_acknowledged:
            _add_reason(report, "production_apply_post_write_diff_review_missing")
        if not commit_plan_acknowledged:
            _add_reason(report, "production_apply_commit_plan_missing")
        if not push_deferred_acknowledged:
            _add_reason(report, "production_apply_push_deferred_missing")

        blocking_reasons = [reason for reason in report["reasons"] if reason.get("blocking") is True]
        if blocking_reasons:
            report["status"] = "production_apply_preflight_blocked"
        else:
            report["status"] = "production_apply_preflight_ready"
            report["preflight_ready"] = True
            report["eligible_for_operator_review"] = True
        report.pop("_reason_keys", None)
        return report
    except Exception as exc:  # pragma: no cover - defensive report boundary
        _add_reason(report, "unknown_error", error=exc.__class__.__name__)
        report["status"] = "production_apply_preflight_failed"
        report.pop("_reason_keys", None)
        return report

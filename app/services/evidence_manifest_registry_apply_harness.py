from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.domain import constants
from app.services import (
    evidence_manifest_registry,
    evidence_manifest_registry_commit_guard,
    evidence_manifest_registry_write,
)

TARGET_REGISTRY_PATH = "data/evidence_manifests/registry.json"
TEMP_HARNESS_ENV_GATE = "ALLOW_TEMP_REGISTRY_APPLY_TEST_HARNESS"
TEMP_HARNESS_ENV_VALUE = "1"

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "registry_apply_harness_env_gate_required": ("Temp apply harness requires ALLOW_TEMP_REGISTRY_APPLY_TEST_HARNESS=1.", True, True, "Set the harness env gate only for isolated temp repo tests."),
    "registry_apply_harness_real_repo_blocked": ("Temp apply harness cannot run against the real project root.", True, True, "Create an isolated temp git repo and rerun the harness."),
    "registry_apply_harness_temp_root_required": ("Temp apply harness requires an explicit temp project root.", True, True, "Pass --temp-project-root pointing to an isolated temp git repo."),
    "registry_apply_harness_git_repo_required": ("Temp apply harness root must be a git repository root.", True, True, "Initialize a temp git repo and rerun the harness."),
    "registry_apply_operator_flag_required": ("Temp apply harness lacks operator approval flag.", True, True, "Rerun only after explicit operator approval."),
    "registry_apply_confirmation_mismatch": ("Confirm path does not exactly match the harness registry target.", True, True, "Use exact data/evidence_manifests/registry.json."),
    "registry_apply_proposal_invalid": ("Registry proposal is invalid for harness apply.", True, True, "Regenerate or repair the proposal before rerunning."),
    "registry_apply_registry_check_failed": ("Registry checker failed before harness apply.", True, True, "Review checker reasons and repair the proposal or manifests."),
    "registry_apply_existing_registry_requires_overwrite": ("Existing temp registry requires explicit overwrite permission.", True, True, "Rerun with overwrite enabled if replacement is intended."),
    "registry_apply_candidate_write_failed": ("Harness candidate registry file could not be written.", True, True, "Inspect temp repo filesystem permissions."),
    "registry_apply_candidate_check_failed": ("Harness candidate registry check failed before replace.", True, True, "Repair the proposed registry before retrying."),
    "registry_apply_atomic_replace_failed": ("Harness atomic replace failed.", True, True, "Inspect locks, paths, and filesystem state before retrying."),
    "registry_apply_post_check_failed": ("Harness post-write registry check failed.", True, True, "Rollback and inspect the written registry."),
    "registry_apply_rollback_required": ("Harness write failure requires rollback.", True, True, "Restore the prior temp registry or remove the failed target."),
    "registry_apply_rollback_failed": ("Harness rollback failed.", True, True, "Manually inspect and restore the temp repo registry."),
    "registry_apply_live_replay_forbidden": ("Harness apply must not run live replay.", True, True, "Use proposal-only harness input."),
    "registry_apply_dashboard_trigger_forbidden": ("Harness apply must not be triggered through dashboard/API.", True, True, "Use the harness CLI only."),
    "registry_apply_commit_guard_blocked": ("Temp repo commit guard blocked harness apply.", True, True, "Clean the temp repo and rerun the harness."),
    "unknown_error": ("Unexpected temp apply harness error.", True, True, "Stop and diagnose before retrying."),
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
    key = (code, json.dumps(context, sort_keys=True, default=str))
    seen = report.setdefault("_reason_keys", set())
    if key in seen:
        return
    seen.add(key)
    report["reasons"].append(_reason(code, **context))


def _base_report(
    *,
    proposal_path: str | Path,
    temp_project_root: str | Path | None,
    registry_path: str | Path,
    topic_id: str | None,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "proposal_path": str(proposal_path),
        "temp_project_root": str(temp_project_root or ""),
        "registry_path": str(registry_path),
        "topic_id": topic_id,
        "operator_flag_present": False,
        "env_gate_present": False,
        "confirm_path_matched": False,
        "registry_written": False,
        "write_attempted": False,
        "candidate_written": False,
        "candidate_path": None,
        "candidate_check_status": None,
        "atomic_replace_attempted": False,
        "atomic_replace_succeeded": False,
        "backup_created": False,
        "backup_path": None,
        "rollback_attempted": False,
        "rollback_succeeded": False,
        "post_write_check_status": None,
        "pre_apply_guard_status": None,
        "network_attempted": False,
        "db_storage_write_attempted": False,
        "live_replay_executed": False,
        "provider_oauth_external_ai_used": False,
        "dashboard_trigger_used": False,
        "safe_to_claim_real_project_registry_exists": False,
        "safe_to_claim_harness_apply_verified": False,
        "safe_to_claim_registry_lifecycle_complete": False,
        "reasons": [],
    }


def _normalize_relative(path: str | Path | None) -> str:
    return Path(str(path or "")).as_posix()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _resolve_registry_path(raw_path: str | Path, *, temp_root: Path) -> tuple[str | None, Path | None, str | None]:
    raw = Path(str(raw_path or ""))
    if raw.is_absolute() or any(part == ".." for part in raw.parts):
        return None, None, "registry_apply_confirmation_mismatch"
    candidate = temp_root / raw
    if not _inside(candidate, temp_root):
        return None, candidate, "registry_apply_confirmation_mismatch"
    try:
        relative = candidate.resolve(strict=False).relative_to(temp_root.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        return None, candidate, "registry_apply_confirmation_mismatch"
    if relative != TARGET_REGISTRY_PATH:
        return relative, candidate, "registry_apply_confirmation_mismatch"
    return relative, candidate, None


def _temp_root_allowed(root: Path) -> bool:
    temp_roots = {Path(tempfile.gettempdir()).resolve(strict=False)}
    for env_name in ("TEMP", "TMP", "TMPDIR"):
        env_value = os.getenv(env_name)
        if env_value:
            temp_roots.add(Path(env_value).resolve(strict=False))
    return any(_inside(root, temp_root) for temp_root in temp_roots)


def _git_root(root: Path) -> tuple[Path | None, str | None]:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip() or "git rev-parse failed"
    return Path(result.stdout.strip()).resolve(strict=False), None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass


def _candidate_path(target_path: Path) -> Path:
    return target_path.with_name(f".{target_path.name}.candidate-{uuid.uuid4().hex}.tmp")


def _backup_path(temp_root: Path) -> Path:
    return temp_root / ".registry_apply_harness_backups" / f"registry-{uuid.uuid4().hex}.json"


def _load_and_validate_proposal(proposal_path: str | Path, report: dict[str, Any]) -> dict[str, Any] | None:
    proposal, read_error = evidence_manifest_registry_write._read_proposal(Path(proposal_path))  # type: ignore[attr-defined]
    if read_error or not isinstance(proposal, dict):
        _add_reason(report, "registry_apply_proposal_invalid", proposal_reason=read_error)
        return None
    proposal_report = {"reasons": []}
    proposed_registry = evidence_manifest_registry_write._validate_proposal(proposal, proposal_report)  # type: ignore[attr-defined]
    if proposal_report["reasons"] or not isinstance(proposed_registry, dict):
        _add_reason(
            report,
            "registry_apply_proposal_invalid",
            proposal_reason_codes=[reason.get("code") for reason in proposal_report["reasons"]],
        )
        return None
    if not report.get("topic_id"):
        report["topic_id"] = evidence_manifest_registry_write._infer_topic_id(proposal, None)  # type: ignore[attr-defined]
    return proposed_registry


def _check_registry(registry_path: Path, *, topic_id: str | None, temp_root: Path) -> dict[str, Any]:
    return evidence_manifest_registry.check_manifest_registry(registry_path, topic_id=topic_id, project_root=temp_root)


def _rollback(
    report: dict[str, Any],
    *,
    target_path: Path,
    backup_path: Path | None,
    had_existing_registry: bool,
    topic_id: str | None,
    temp_root: Path,
) -> None:
    report["rollback_attempted"] = True
    if had_existing_registry and backup_path is not None and backup_path.exists():
        try:
            os.replace(backup_path, target_path)
        except OSError:
            _add_reason(report, "registry_apply_rollback_failed")
            report["rollback_succeeded"] = False
            return
        rollback_check = _check_registry(target_path, topic_id=topic_id, temp_root=temp_root)
        if rollback_check.get("status") == "registry_verified":
            report["rollback_succeeded"] = True
        else:
            report["rollback_succeeded"] = False
            _add_reason(report, "registry_apply_rollback_failed")
        return
    try:
        if target_path.exists():
            target_path.unlink()
    except OSError:
        _add_reason(report, "registry_apply_rollback_failed")
        report["rollback_succeeded"] = False
        return
    report["rollback_succeeded"] = not target_path.exists()
    if not report["rollback_succeeded"]:
        _add_reason(report, "registry_apply_rollback_failed")


def apply_registry_in_temp_harness(
    *,
    proposal_path: str | Path,
    temp_project_root: str | Path | None,
    registry_path: str | Path = TARGET_REGISTRY_PATH,
    topic_id: str | None = None,
    allow_existing_registry: bool = False,
    backup_existing_registry: bool = True,
    operator_approved: bool = False,
    confirm_registry_path: str | Path | None = None,
    env_gate_value: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    del strict
    report = _base_report(
        proposal_path=proposal_path,
        temp_project_root=temp_project_root,
        registry_path=registry_path,
        topic_id=topic_id,
    )
    report["operator_flag_present"] = bool(operator_approved)
    actual_env_value = env_gate_value if env_gate_value is not None else os.getenv(TEMP_HARNESS_ENV_GATE)
    report["env_gate_present"] = actual_env_value == TEMP_HARNESS_ENV_VALUE

    if not temp_project_root or not str(temp_project_root).strip():
        _add_reason(report, "registry_apply_harness_temp_root_required")
    else:
        temp_root = Path(temp_project_root).resolve(strict=False)
        report["temp_project_root"] = str(temp_root)
        real_root = constants.PROJECT_ROOT.resolve(strict=False)
        if temp_root == real_root:
            _add_reason(report, "registry_apply_harness_real_repo_blocked")
        if not _temp_root_allowed(temp_root):
            _add_reason(report, "registry_apply_harness_temp_root_required", temp_project_root=str(temp_root))
        git_root, git_error = _git_root(temp_root) if temp_root.exists() else (None, "missing root")
        if git_error or git_root != temp_root:
            _add_reason(report, "registry_apply_harness_git_repo_required", git_error=git_error)
    if not report["env_gate_present"]:
        _add_reason(report, "registry_apply_harness_env_gate_required")
    if not report["operator_flag_present"]:
        _add_reason(report, "registry_apply_operator_flag_required")

    temp_root = Path(report["temp_project_root"]).resolve(strict=False) if report["temp_project_root"] else None
    target_path: Path | None = None
    if temp_root is not None:
        registry_relative, target_candidate, registry_error = _resolve_registry_path(registry_path, temp_root=temp_root)
        if registry_relative:
            report["registry_path"] = registry_relative
        target_path = target_candidate
        if registry_error:
            _add_reason(report, registry_error, registry_path=str(registry_path))
        confirm_relative, _, confirm_error = _resolve_registry_path(confirm_registry_path, temp_root=temp_root)
        report["confirm_path_matched"] = confirm_relative == TARGET_REGISTRY_PATH and confirm_error is None
        if not report["confirm_path_matched"]:
            _add_reason(report, "registry_apply_confirmation_mismatch", confirm_registry_path=str(confirm_registry_path or ""))

    if report["reasons"]:
        report["status"] = "registry_apply_harness_blocked"
        report.pop("_reason_keys", None)
        return report

    assert temp_root is not None
    assert target_path is not None

    proposed_registry = _load_and_validate_proposal(proposal_path, report)
    if proposed_registry is None:
        report["status"] = "registry_apply_harness_blocked"
        report.pop("_reason_keys", None)
        return report

    had_existing_registry = target_path.exists()
    if had_existing_registry and not allow_existing_registry:
        _add_reason(report, "registry_apply_existing_registry_requires_overwrite", registry_path=str(target_path))
        report["status"] = "registry_apply_harness_blocked"
        report.pop("_reason_keys", None)
        return report

    guard_report = evidence_manifest_registry_commit_guard.check_registry_commit_guard(
        repo_root=temp_root,
        expected_registry_path=TARGET_REGISTRY_PATH,
        mode="pre_apply",
        allow_registry_file=had_existing_registry and allow_existing_registry,
    )
    report["pre_apply_guard_status"] = guard_report.get("status")
    if guard_report.get("status") not in {"registry_commit_guard_passed", "registry_commit_guard_warning"}:
        _add_reason(report, "registry_apply_commit_guard_blocked", guard_status=guard_report.get("status"))
        report["status"] = "registry_apply_harness_blocked"
        report.pop("_reason_keys", None)
        return report

    candidate_path = _candidate_path(target_path)
    report["candidate_path"] = str(candidate_path)
    try:
        report["write_attempted"] = True
        _write_json_file(candidate_path, proposed_registry)
        report["candidate_written"] = True
    except OSError:
        _add_reason(report, "registry_apply_candidate_write_failed", candidate_path=str(candidate_path))
        report["status"] = "registry_apply_harness_failed"
        report.pop("_reason_keys", None)
        return report

    candidate_check = _check_registry(candidate_path, topic_id=report["topic_id"], temp_root=temp_root)
    report["candidate_check_status"] = candidate_check.get("status")
    if candidate_check.get("status") != "registry_verified":
        _add_reason(report, "registry_apply_candidate_check_failed")
        try:
            candidate_path.unlink(missing_ok=True)
        except OSError:
            pass
        report["status"] = "registry_apply_harness_failed"
        report.pop("_reason_keys", None)
        return report

    backup_file: Path | None = None
    if had_existing_registry and backup_existing_registry:
        backup_file = _backup_path(temp_root)
        try:
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, backup_file)
            report["backup_created"] = True
            report["backup_path"] = str(backup_file)
        except OSError:
            _add_reason(report, "registry_apply_rollback_failed")
            report["status"] = "registry_apply_harness_failed"
            report.pop("_reason_keys", None)
            return report

    report["atomic_replace_attempted"] = True
    try:
        os.replace(candidate_path, target_path)
        report["atomic_replace_succeeded"] = True
    except OSError:
        _add_reason(report, "registry_apply_atomic_replace_failed")
        try:
            candidate_path.unlink(missing_ok=True)
        except OSError:
            pass
        report["status"] = "registry_apply_harness_failed"
        report.pop("_reason_keys", None)
        return report

    post_check = _check_registry(target_path, topic_id=report["topic_id"], temp_root=temp_root)
    report["post_write_check_status"] = post_check.get("status")
    if post_check.get("status") != "registry_verified":
        _add_reason(report, "registry_apply_post_check_failed")
        _add_reason(report, "registry_apply_rollback_required")
        _rollback(
            report,
            target_path=target_path,
            backup_path=backup_file,
            had_existing_registry=had_existing_registry,
            topic_id=report["topic_id"],
            temp_root=temp_root,
        )
        report["status"] = "registry_apply_harness_rolled_back" if report["rollback_succeeded"] else "registry_apply_harness_failed"
        report.pop("_reason_keys", None)
        return report

    report["status"] = "registry_apply_harness_applied"
    report["registry_written"] = True
    report["safe_to_claim_harness_apply_verified"] = True
    report.pop("_reason_keys", None)
    return report

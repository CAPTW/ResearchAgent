from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.domain import constants
from app.services import evidence_manifest_registry

TARGET_REGISTRY_PATH = "data/evidence_manifests/registry.json"
TRACKED_WRITE_ENV_GATE = "ALLOW_TRACKED_REGISTRY_WRITE"
TRACKED_WRITE_ENV_VALUE = "1"
SUPPORTED_PROPOSAL_SCHEMA_VERSION = 1

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "registry_write_operator_flag_required": ("Apply-readiness dry-run lacks --operator-approve-tracked-registry-write.", True, True, "Rerun only after explicit operator approval."),
    "registry_write_env_gate_required": ("Apply-readiness dry-run lacks ALLOW_TRACKED_REGISTRY_WRITE=1.", True, True, "Set env gate only for the approved dry-run readiness check."),
    "registry_write_confirmation_mismatch": ("Confirm path does not exactly match the target registry path.", True, True, "Use exact data/evidence_manifests/registry.json."),
    "registry_write_proposal_missing": ("Proposal file does not exist.", True, True, "Provide an existing proposal file."),
    "registry_write_proposal_invalid_json": ("Proposal JSON is malformed.", True, True, "Fix or regenerate proposal."),
    "registry_write_proposal_not_object": ("Proposal JSON root is not an object.", True, True, "Regenerate proposal as a JSON object."),
    "registry_write_proposal_schema_unsupported": ("Proposal schema version is unsupported.", True, True, "Regenerate proposal with a supported schema."),
    "registry_write_proposal_kind_mismatch": ("Proposal kind is not evidence_manifest_registry_proposal.", True, True, "Use a valid registry proposal."),
    "registry_write_proposal_status_invalid": ("Proposal status is not generated or verified.", True, True, "Regenerate or verify proposal."),
    "registry_write_proposal_not_verified": ("Proposal verification reports are missing or not verified.", True, True, "Run proposal generation and registry-check again."),
    "registry_write_proposal_safety_flag_invalid": ("Proposal safety flags do not preserve the review-only boundary.", True, True, "Regenerate proposal with safe flags."),
    "registry_write_target_path_mismatch": ("Proposal or CLI target path does not match the tracked registry path.", True, True, "Use matching data/evidence_manifests/registry.json paths."),
    "registry_write_forbidden_output_path": ("Registry output path resolves unsafely.", True, True, "Use the exact tracked registry path inside the project root."),
    "registry_write_forbidden_content": ("Proposal or proposed registry contains forbidden raw artifact content.", True, True, "Remove PDF bytes, full text, pages content, or raw artifacts and regenerate."),
    "registry_write_secret_like_value": ("Proposal or proposed registry contains a secret-like key or value.", True, True, "Remove secrets and rotate credentials if needed."),
    "registry_write_runtime_artifact_detected": ("Proposal or proposed registry embeds runtime DB/storage/PDF/text/pages artifact data.", True, True, "Remove runtime artifact data and regenerate."),
    "registry_write_live_replay_forbidden": ("Proposal indicates live replay execution.", True, True, "Separate live replay into an approved phase."),
    "registry_write_dashboard_trigger_forbidden": ("Proposal indicates dashboard/API trigger use.", True, True, "Use CLI-only operator workflow."),
    "registry_write_existing_registry_requires_overwrite_flag": ("Existing registry would be overwritten; apply/update is not implemented.", True, True, "Review existing registry and wait for a separately approved apply phase."),
    "registry_write_post_check_failed": ("Immediate read-only registry-check failed for proposed_registry.", True, True, "Review registry checker reasons and regenerate proposal."),
    "registry_write_apply_not_implemented": ("Tracked registry apply mode is not implemented in M28 P1.", True, True, "Rerun with --dry-run or wait for a separate apply phase."),
    "unknown_error": ("Unexpected registry write dry-run error.", True, True, "Stop and diagnose before retrying."),
}

_FULL_TEXT_KEYS = {"full_text", "full_text_content", "extracted_text"}
_PDF_CONTENT_KEYS = {"pdf_bytes", "pdf_content", "raw_pdf"}
_PAGES_CONTENT_KEYS = {"pages_content", "page_text", "page_texts", "pages_json"}
_SECRET_KEYS = {"secret", "token", "api_key", "apikey", "password", "private_key", "credential"}
_RUNTIME_KEYS = {"db_path", "database_path", "storage_path", "runtime_path", "pdf_path", "text_path", "pages_path"}
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-(?:live|test|proj)?-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}\b", re.IGNORECASE),
]


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
    if code not in {reason["code"] for reason in report["reasons"]}:
        report["reasons"].append(_reason(code, **context))


def _base_report(proposal_path: str | Path, registry_path: str | Path, topic_id: str | None, dry_run: bool) -> dict[str, Any]:
    return {
        "status": "failed",
        "proposal_path": str(proposal_path),
        "registry_path": str(registry_path),
        "topic_id": topic_id,
        "dry_run": bool(dry_run),
        "would_write": False,
        "registry_written": False,
        "write_attempted": False,
        "db_storage_write_attempted": False,
        "network_attempted": False,
        "live_replay_executed": False,
        "provider_oauth_external_ai_used": False,
        "dashboard_trigger_used": False,
        "operator_flag_present": False,
        "env_gate_present": False,
        "confirm_path_matched": False,
        "registry_check_status": None,
        "registry_check_report": None,
        "safe_to_write_tracked_registry": False,
        "safe_to_claim_tracked_registry_exists": False,
        "safe_to_claim_registry_lifecycle_complete": False,
        "safe_to_claim_clean_clone_reproducible": False,
        "reasons": [],
    }


def _has_traversal(path: Path) -> bool:
    return any(part == ".." for part in path.parts)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while True:
        try:
            if current.exists() and current.is_symlink():
                return True
        except OSError:
            return True
        if current == root or current.parent == current:
            return False
        current = current.parent


def _relative_registry_path(raw_path: str | Path | None, *, project_root: Path) -> tuple[str | None, Path | None, str | None]:
    if raw_path is None or not str(raw_path).strip():
        return None, None, "registry_write_target_path_mismatch"
    raw = Path(str(raw_path))
    if _has_traversal(raw):
        return None, None, "registry_write_forbidden_output_path"
    candidate = raw if raw.is_absolute() else project_root / raw
    if not _inside(candidate, project_root):
        return None, candidate, "registry_write_forbidden_output_path"
    if _has_symlink_component(candidate, project_root):
        return None, candidate, "registry_write_forbidden_output_path"
    try:
        relative = candidate.resolve(strict=False).relative_to(project_root.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        return None, candidate, "registry_write_forbidden_output_path"
    if relative != TARGET_REGISTRY_PATH:
        return relative, candidate, "registry_write_target_path_mismatch"
    return relative, candidate, None


def _proposal_target_matches(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return Path(text).as_posix() == TARGET_REGISTRY_PATH


def _read_proposal(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "registry_write_proposal_missing"
    except json.JSONDecodeError:
        return None, "registry_write_proposal_invalid_json"
    except OSError:
        return None, "unknown_error"
    if not isinstance(payload, dict):
        return None, "registry_write_proposal_not_object"
    return payload, None


def _runtime_artifact_text(value: str) -> bool:
    text = value.replace("\\", "/").lower()
    if any(marker in text for marker in ("storage/", "output/", "data/papers/", "notes/papers/")):
        return True
    return text.endswith((".sqlite", ".sqlite3", ".db"))


def _walk_safety(value: Any, report: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _FULL_TEXT_KEYS or lowered in _PDF_CONTENT_KEYS or lowered in _PAGES_CONTENT_KEYS:
                _add_reason(report, "registry_write_forbidden_content")
            if lowered in _SECRET_KEYS or any(secret_key in lowered for secret_key in _SECRET_KEYS):
                _add_reason(report, "registry_write_secret_like_value")
            if lowered in _RUNTIME_KEYS:
                _add_reason(report, "registry_write_runtime_artifact_detected")
            _walk_safety(item, report)
    elif isinstance(value, list):
        for item in value:
            _walk_safety(item, report)
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            _add_reason(report, "registry_write_secret_like_value")
        if _runtime_artifact_text(value):
            _add_reason(report, "registry_write_runtime_artifact_detected")


def _validate_proposal(proposal: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    if proposal.get("proposal_schema_version") != SUPPORTED_PROPOSAL_SCHEMA_VERSION:
        _add_reason(report, "registry_write_proposal_schema_unsupported")
    if proposal.get("proposal_kind") != "evidence_manifest_registry_proposal":
        _add_reason(report, "registry_write_proposal_kind_mismatch")
    if proposal.get("proposal_status") not in {"proposal_generated", "proposal_verified"}:
        _add_reason(report, "registry_write_proposal_status_invalid")
    if not _proposal_target_matches(proposal.get("target_registry_path")):
        _add_reason(report, "registry_write_target_path_mismatch")
    if proposal.get("target_registry_write_allowed") is not False:
        _add_reason(report, "registry_write_proposal_safety_flag_invalid")

    proposed_registry = proposal.get("proposed_registry")
    if not isinstance(proposed_registry, dict):
        _add_reason(report, "registry_write_proposal_not_verified")
        proposed_registry = None

    admission_report = proposal.get("admission_report") if isinstance(proposal.get("admission_report"), dict) else {}
    generator_report = proposal.get("generator_report") if isinstance(proposal.get("generator_report"), dict) else {}
    registry_check_report = proposal.get("registry_check_report") if isinstance(proposal.get("registry_check_report"), dict) else {}
    if admission_report.get("status") != "admitted":
        _add_reason(report, "registry_write_proposal_not_verified")
    if generator_report.get("status") != "registry_generate_dry_run_planned":
        _add_reason(report, "registry_write_proposal_not_verified")
    if registry_check_report.get("status") != "registry_verified":
        _add_reason(report, "registry_write_proposal_not_verified")

    safety_flags = proposal.get("safety_flags")
    expected_flags = {
        "proposal_only": True,
        "tracked_registry_written": False,
        "live_replay_executed": False,
        "provider_oauth_external_ai_used": False,
        "dashboard_trigger_used": False,
    }
    if not isinstance(safety_flags, dict) or any(safety_flags.get(key) is not expected for key, expected in expected_flags.items()):
        _add_reason(report, "registry_write_proposal_safety_flag_invalid")
    else:
        if safety_flags.get("live_replay_executed") is True:
            _add_reason(report, "registry_write_live_replay_forbidden")
        if safety_flags.get("dashboard_trigger_used") is True:
            _add_reason(report, "registry_write_dashboard_trigger_forbidden")

    _walk_safety(proposal, report)
    if isinstance(proposed_registry, dict):
        _walk_safety(proposed_registry, report)
    return proposed_registry


def _infer_topic_id(proposal: dict[str, Any], fallback: str | None) -> str | None:
    if fallback:
        return fallback
    proposed_registry = proposal.get("proposed_registry") if isinstance(proposal.get("proposed_registry"), dict) else {}
    topic_id = str(proposed_registry.get("topic_id") or "").strip()
    if topic_id:
        return topic_id
    admission_report = proposal.get("admission_report") if isinstance(proposal.get("admission_report"), dict) else {}
    return str(admission_report.get("topic_id") or "").strip() or None


def _check_proposed_registry(proposed_registry: dict[str, Any], *, topic_id: str | None, project_root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="xrml-registry-write-check-") as temp_dir:
        registry_path = Path(temp_dir) / "proposed-registry.json"
        registry_path.write_text(json.dumps(proposed_registry, ensure_ascii=False, indent=2), encoding="utf-8")
        result = evidence_manifest_registry.check_manifest_registry(registry_path, topic_id=topic_id, project_root=project_root)
    sanitized = deepcopy(result)
    sanitized["registry_path"] = "temp:proposed-registry.json"
    return sanitized


def _has_proposal_invalid_reason(report: dict[str, Any]) -> bool:
    invalid_prefixes = (
        "registry_write_proposal_",
        "registry_write_forbidden_content",
        "registry_write_secret_like_value",
        "registry_write_runtime_artifact_detected",
        "registry_write_live_replay_forbidden",
        "registry_write_dashboard_trigger_forbidden",
    )
    return any(reason["code"].startswith(invalid_prefixes[0]) or reason["code"] in invalid_prefixes[1:] for reason in report["reasons"])


def validate_registry_write_dry_run(
    *,
    proposal_path: str | Path,
    registry_path: str | Path,
    topic_id: str | None = None,
    dry_run: bool = True,
    operator_approved: bool = False,
    env_gate_value: str | None = None,
    confirm_registry_path: str | Path | None = None,
    project_root: str | Path | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    del strict
    root = Path(project_root or constants.PROJECT_ROOT).resolve()
    report = _base_report(proposal_path, registry_path, topic_id, dry_run)
    report["operator_flag_present"] = bool(operator_approved)
    actual_env_value = env_gate_value if env_gate_value is not None else os.getenv(TRACKED_WRITE_ENV_GATE)
    report["env_gate_present"] = actual_env_value == TRACKED_WRITE_ENV_VALUE

    if not dry_run:
        report["status"] = "registry_write_apply_not_implemented"
        _add_reason(report, "registry_write_apply_not_implemented")
        return report

    proposal, read_error = _read_proposal(Path(proposal_path))
    if read_error:
        report["status"] = "registry_write_proposal_invalid"
        _add_reason(report, read_error)
        return report

    assert proposal is not None
    report["topic_id"] = _infer_topic_id(proposal, topic_id)
    proposed_registry = _validate_proposal(proposal, report)

    registry_relative, registry_candidate, registry_error = _relative_registry_path(registry_path, project_root=root)
    if registry_relative:
        report["registry_path"] = registry_relative
    if registry_error:
        _add_reason(report, registry_error, registry_path=str(registry_path))
    if registry_candidate is not None and registry_candidate.exists():
        _add_reason(report, "registry_write_existing_registry_requires_overwrite_flag", registry_path=str(registry_candidate))

    confirm_relative, _, confirm_error = _relative_registry_path(confirm_registry_path, project_root=root)
    report["confirm_path_matched"] = confirm_relative == TARGET_REGISTRY_PATH and confirm_error is None
    if not report["confirm_path_matched"]:
        _add_reason(report, "registry_write_confirmation_mismatch", confirm_registry_path=str(confirm_registry_path) if confirm_registry_path is not None else None)
    if not report["operator_flag_present"]:
        _add_reason(report, "registry_write_operator_flag_required")
    if not report["env_gate_present"]:
        _add_reason(report, "registry_write_env_gate_required")

    if report["reasons"]:
        report["status"] = "registry_write_proposal_invalid" if _has_proposal_invalid_reason(report) else "registry_write_blocked"
        return report

    if not isinstance(proposed_registry, dict):
        report["status"] = "registry_write_proposal_invalid"
        _add_reason(report, "registry_write_proposal_not_verified")
        return report

    registry_check_report = _check_proposed_registry(proposed_registry, topic_id=report["topic_id"], project_root=root)
    report["registry_check_report"] = registry_check_report
    report["registry_check_status"] = registry_check_report.get("status")
    if registry_check_report.get("status") != "registry_verified":
        report["status"] = "registry_write_registry_check_failed"
        _add_reason(report, "registry_write_post_check_failed")
        return report

    report["status"] = "registry_write_dry_run_ready"
    report["would_write"] = True
    return report

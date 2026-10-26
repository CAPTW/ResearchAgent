from __future__ import annotations

import json
import re
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from app.domain import constants
from app.services import evidence_manifest_registry, evidence_manifest_registry_generate

PROPOSAL_SCHEMA_VERSION = 1
DEFAULT_GENERATED_BY_PHASE = "M27_P1_REGISTRY_PROPOSAL_WRITER_DRY_RUN"
TARGET_REGISTRY_PATH = "data/evidence_manifests/registry.json"

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "proposal_output_path_required": ("No proposal output path or temp output mode was supplied.", True, True, "Use --temp-output or select an approved non-registry output path."),
    "proposal_output_path_forbidden": ("Proposal output path is in a forbidden location.", True, True, "Choose a temp path or approved non-registry path outside runtime folders."),
    "proposal_output_path_inside_registry_location": ("Proposal output path targets the registry location.", True, True, "Use a non-registry proposal path."),
    "proposal_output_path_traversal": ("Proposal output path contains traversal.", True, True, "Use a normalized direct output path."),
    "proposal_output_path_symlink_disallowed": ("Proposal output path or parent is a symlink.", True, True, "Use a direct non-symlink output path."),
    "proposal_output_already_exists": ("Proposal output already exists and overwrite was not approved.", True, True, "Choose a new output path or explicitly allow overwrite."),
    "proposal_generation_admission_failed": ("Registry generation failed because admission rejected the manifest set.", True, True, "Fix admission failures before writing a proposal."),
    "proposal_registry_generation_failed": ("Registry generation failed before a proposal could be written.", True, True, "Fix generation errors before writing a proposal."),
    "proposal_registry_check_failed": ("Read-only registry check failed for the proposed registry.", True, True, "Review registry checker reasons and regenerate."),
    "proposal_contains_forbidden_content": ("Proposal contains forbidden raw artifact content.", True, True, "Remove PDF bytes, full text, pages content, or runtime artifact content."),
    "proposal_contains_secret_like_value": ("Proposal contains a secret-like key or value.", True, True, "Remove secrets and rotate credentials if needed."),
    "proposal_write_failed": ("Proposal file write failed.", True, True, "Fix output path permissions and retry."),
    "proposal_temp_write_failed": ("Temp proposal file write failed.", True, True, "Fix temp directory availability and retry."),
    "proposal_target_registry_write_forbidden": ("Proposal attempted to allow tracked registry write.", True, True, "Keep target_registry_write_allowed=false."),
    "proposal_runtime_artifact_detected": ("Proposal references forbidden runtime artifact paths.", True, True, "Remove runtime DB/storage/PDF/text/pages artifact references."),
    "unknown_error": ("Unexpected proposal writer error.", True, True, "Stop and diagnose before retrying."),
}

_FULL_TEXT_KEYS = {"full_text", "full_text_content", "extracted_text"}
_PDF_CONTENT_KEYS = {"pdf_bytes", "pdf_content", "raw_pdf"}
_PAGES_CONTENT_KEYS = {"pages_content", "page_text", "page_texts", "pages_json"}
_SECRET_KEYS = {"secret", "token", "api_key", "apikey", "password", "private_key", "credential"}
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-(?:live|test|proj)?-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}\b", re.IGNORECASE),
]


def _reason(code: str, **context: Any) -> dict[str, Any]:
    message, operator_fixable, blocks_proposal, safe_next_action = _REASON_META.get(code, _REASON_META["unknown_error"])
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "blocks_proposal": blocks_proposal,
        "operator_fixable": operator_fixable,
        "safe_next_action": safe_next_action,
    }
    payload.update({key: value for key, value in context.items() if value is not None})
    return payload


def _base_report(topic_id: str, manifest_paths: Sequence[str | Path]) -> dict[str, Any]:
    return {
        "status": "failed",
        "topic_id": topic_id,
        "manifest_count": len(manifest_paths),
        "proposal_path": None,
        "proposal_status": None,
        "proposal_id": None,
        "output_path": None,
        "output_path_type": None,
        "proposal_written": False,
        "tracked_registry_written": False,
        "target_registry_write_allowed": False,
        "registry_check_status": None,
        "network_attempted": False,
        "write_attempted": False,
        "db_storage_write_attempted": False,
        "live_replay_executed": False,
        "safe_to_claim_registry_lifecycle_complete": False,
        "safe_to_claim_tracked_registry_exists": False,
        "safe_to_claim_clean_clone_reproducible": False,
        "safe_to_claim_arbitrary_manifest_automation": False,
        "proposal": None,
        "generator_report": None,
        "registry_check_report": None,
        "reasons": [],
    }


def _add_reason(report: dict[str, Any], code: str, **context: Any) -> None:
    if code not in {reason["code"] for reason in report["reasons"]}:
        report["reasons"].append(_reason(code, **context))


def _has_traversal(path: Path) -> bool:
    return any(part == ".." for part in path.parts)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    if path.exists() and path.is_symlink():
        return True
    for parent in path.parents:
        try:
            if parent.exists() and parent.is_symlink():
                return True
        except OSError:
            return True
    return False


def _resolve_explicit_output_path(raw_path: str | Path, *, project_root: Path) -> tuple[Path | None, str | None]:
    raw = Path(raw_path)
    if _has_traversal(raw):
        return None, "proposal_output_path_traversal"
    path = raw if raw.is_absolute() else project_root / raw
    expanded = path.expanduser()
    if _has_symlink_component(expanded):
        return expanded.resolve(strict=False), "proposal_output_path_symlink_disallowed"
    resolved = expanded.resolve(strict=False)

    registry_dir = project_root / "data" / "evidence_manifests"
    if _inside(resolved, registry_dir):
        return resolved, "proposal_output_path_inside_registry_location"
    for forbidden in (project_root / "storage", project_root / "output", project_root / ".git"):
        if _inside(resolved, forbidden):
            return resolved, "proposal_output_path_forbidden"
    if _inside(resolved, project_root):
        return resolved, "proposal_output_path_forbidden"
    if _has_symlink_component(resolved):
        return resolved, "proposal_output_path_symlink_disallowed"
    return resolved, None


def _proposal_id(topic_id: str) -> str:
    stamp = datetime.now(UTC).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")
    return f"{topic_id}-m27-p1-{stamp}"


def _proposal_path_for_temp(topic_id: str, proposal_id: str) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="xrml-registry-proposal-"))
    safe_topic = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in topic_id)
    safe_id = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in proposal_id)
    return temp_dir / f"{safe_topic}-{safe_id}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _generator_report_for_proposal(generator_report: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(generator_report)
    payload.pop("proposed_registry", None)
    payload.pop("admission_report", None)
    return payload


def _check_proposed_registry(proposed_registry: dict[str, Any], *, topic_id: str, project_root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="xrml-proposed-registry-check-") as temp_dir:
        registry_path = Path(temp_dir) / "proposed-registry.json"
        _write_json(registry_path, proposed_registry)
        result = evidence_manifest_registry.check_manifest_registry(registry_path, topic_id=topic_id, project_root=project_root)
    sanitized = deepcopy(result)
    sanitized["registry_path"] = "temp:proposed-registry.json"
    return sanitized


def _walk_proposal_safety(value: Any, report: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _FULL_TEXT_KEYS or lowered in _PDF_CONTENT_KEYS or lowered in _PAGES_CONTENT_KEYS:
                _add_reason(report, "proposal_contains_forbidden_content")
            if lowered in _SECRET_KEYS or any(secret_key in lowered for secret_key in _SECRET_KEYS):
                _add_reason(report, "proposal_contains_secret_like_value")
            _walk_proposal_safety(item, report)
    elif isinstance(value, list):
        for item in value:
            _walk_proposal_safety(item, report)
    elif isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        _add_reason(report, "proposal_contains_secret_like_value")


def _build_proposal(
    *,
    proposal_id: str,
    generated_by_phase: str,
    proposed_registry: dict[str, Any],
    admission_report: dict[str, Any],
    generator_report: dict[str, Any],
    registry_check_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "proposal_kind": "evidence_manifest_registry_proposal",
        "proposal_generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "proposal_generated_by_phase": generated_by_phase,
        "proposal_status": "proposal_generated",
        "target_registry_path": TARGET_REGISTRY_PATH,
        "target_registry_write_allowed": False,
        "requires_operator_review": True,
        "proposed_registry": proposed_registry,
        "admission_report": admission_report,
        "generator_report": _generator_report_for_proposal(generator_report),
        "registry_check_report": registry_check_report,
        "safety_flags": {
            "proposal_only": True,
            "tracked_registry_written": False,
            "live_replay_executed": False,
            "provider_oauth_external_ai_used": False,
            "dashboard_trigger_used": False,
            "contains_runtime_artifacts": False,
        },
        "notes": "",
    }


def write_registry_proposal(
    manifest_paths: Sequence[str | Path],
    *,
    topic_id: str,
    output_path: str | Path | None = None,
    temp_output: bool = False,
    allow_output_overwrite: bool = False,
    generated_by_phase: str = DEFAULT_GENERATED_BY_PHASE,
    proposal_id: str | None = None,
    project_root: str | Path | None = None,
    strict: bool = False,
    allow_untracked_manifest: bool = False,
    require_local_verified: bool = True,
) -> dict[str, Any]:
    root = Path(project_root or constants.PROJECT_ROOT).resolve()
    report = _base_report(topic_id, manifest_paths)

    if bool(output_path) == bool(temp_output):
        report["status"] = "proposal_rejected"
        _add_reason(report, "proposal_output_path_required")
        return report

    resolved_output: Path | None = None
    output_path_type = "temp" if temp_output else "explicit_non_registry"
    report["output_path_type"] = output_path_type

    if output_path:
        resolved_output, output_error = _resolve_explicit_output_path(output_path, project_root=root)
        report["output_path"] = str(resolved_output or output_path)
        if output_error:
            report["status"] = "proposal_rejected"
            _add_reason(report, output_error, output_path=str(output_path))
            return report
        if resolved_output.exists() and not allow_output_overwrite:
            report["status"] = "proposal_rejected"
            _add_reason(report, "proposal_output_already_exists", output_path=str(resolved_output))
            return report

    try:
        generator_report = evidence_manifest_registry_generate.generate_registry_dry_run(
            manifest_paths,
            topic_id=topic_id,
            generated_by_phase=generated_by_phase,
            project_root=root,
            strict=strict,
            allow_untracked_manifest=allow_untracked_manifest,
            require_local_verified=require_local_verified,
        )
        report["generator_report"] = generator_report
        if generator_report.get("status") != "registry_generate_dry_run_planned":
            report["status"] = "proposal_generation_failed"
            code = "proposal_generation_admission_failed" if generator_report.get("status") == "registry_generation_admission_failed" else "proposal_registry_generation_failed"
            _add_reason(report, code)
            return report

        proposed_registry = generator_report.get("proposed_registry")
        if not isinstance(proposed_registry, dict):
            report["status"] = "proposal_generation_failed"
            _add_reason(report, "proposal_registry_generation_failed")
            return report

        registry_check_report = _check_proposed_registry(proposed_registry, topic_id=topic_id, project_root=root)
        report["registry_check_report"] = registry_check_report
        report["registry_check_status"] = registry_check_report.get("status")
        if registry_check_report.get("status") != "registry_verified":
            report["status"] = "proposal_rejected"
            _add_reason(report, "proposal_registry_check_failed")
            return report

        actual_proposal_id = proposal_id or _proposal_id(topic_id)
        proposal = _build_proposal(
            proposal_id=actual_proposal_id,
            generated_by_phase=generated_by_phase,
            proposed_registry=proposed_registry,
            admission_report=generator_report.get("admission_report") or {},
            generator_report=generator_report,
            registry_check_report=registry_check_report,
        )
        if proposal.get("target_registry_write_allowed") is not False:
            report["status"] = "proposal_rejected"
            _add_reason(report, "proposal_target_registry_write_forbidden")
            return report
        _walk_proposal_safety(proposal, report)
        if report["reasons"]:
            report["status"] = "proposal_rejected"
            return report

        if temp_output:
            resolved_output = _proposal_path_for_temp(topic_id, actual_proposal_id)
            report["output_path"] = str(resolved_output)
        assert resolved_output is not None

        try:
            _write_json(resolved_output, proposal)
        except OSError:
            report["status"] = "proposal_rejected"
            _add_reason(report, "proposal_temp_write_failed" if temp_output else "proposal_write_failed")
            return report

        report.update(
            {
                "status": "proposal_written",
                "proposal_path": str(resolved_output),
                "proposal_status": "proposal_generated",
                "proposal_id": actual_proposal_id,
                "output_path": str(resolved_output),
                "proposal_written": True,
                "write_attempted": True,
                "proposal": proposal,
            }
        )
        return report
    except Exception as exc:  # pragma: no cover - defensive report boundary
        report["status"] = "failed"
        _add_reason(report, "unknown_error", error=exc.__class__.__name__)
        return report

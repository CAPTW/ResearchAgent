from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from app.domain import constants
from app.services import evidence_manifest_admission

REGISTRY_SCHEMA_VERSION = 1
POLICY_VERSION = "m26.p1"
DEFAULT_GENERATED_BY_PHASE = "M26_P1_REGISTRY_GENERATOR_DRY_RUN"

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "registry_generation_admission_failed": (
        "Admission checker rejected one or more manifests.",
        True,
        True,
        "Fix admission rejections before generating a registry proposal.",
    ),
    "registry_generation_empty_manifest_set": (
        "No manifests were supplied for registry generation.",
        True,
        True,
        "Supply at least one admitted manifest.",
    ),
    "registry_generation_manifest_missing": (
        "A manifest disappeared before registry generation.",
        True,
        True,
        "Restore the manifest and rerun admission.",
    ),
    "registry_generation_manifest_hash_failed": (
        "Manifest raw-byte SHA256 could not be computed.",
        True,
        True,
        "Check filesystem permissions and rerun generation.",
    ),
    "registry_generation_forbidden_content": (
        "A proposed registry entry would contain forbidden content.",
        True,
        True,
        "Remove forbidden content before generating a registry proposal.",
    ),
    "registry_generation_manifest_parse_failed": (
        "Manifest JSON could not be parsed after admission.",
        True,
        True,
        "Fix manifest JSON and rerun admission.",
    ),
    "unknown_error": (
        "Unexpected registry generation error.",
        True,
        True,
        "Stop and diagnose before retrying.",
    ),
}


def _reason(code: str, **context: Any) -> dict[str, Any]:
    message, operator_fixable, blocks_generation, safe_next_action = _REASON_META.get(code, _REASON_META["unknown_error"])
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "blocks_generation": blocks_generation,
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
        "generated_entry_count": 0,
        "proposed_registry": None,
        "admission_report": None,
        "network_attempted": False,
        "write_attempted": False,
        "registry_written": False,
        "output_path": None,
        "safe_to_write_tracked_registry": False,
        "safe_to_claim_registry_lifecycle_complete": False,
        "safe_to_claim_clean_clone_reproducible": False,
        "safe_to_claim_arbitrary_manifest_automation": False,
        "reasons": [],
    }


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entries_sha256(entries: list[dict[str, Any]]) -> str:
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest_not_object")
    return payload


def _evidence_span_count(manifest: dict[str, Any], admission_item: dict[str, Any]) -> int:
    local_verification = admission_item.get("local_verification") if isinstance(admission_item.get("local_verification"), dict) else {}
    local_count = local_verification.get("local_evidence_span_count")
    if isinstance(local_count, int):
        return local_count
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    try:
        return int(review.get("evidence_span_count") or 0)
    except (TypeError, ValueError):
        return 0


def _registry_entry(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    admission_item: dict[str, Any],
    manifest_sha256: str,
    generated_by_phase: str,
) -> dict[str, Any]:
    return {
        "paper_id": manifest.get("paper_id"),
        "manifest_path": manifest_path.as_posix(),
        "manifest_sha256": manifest_sha256,
        "manifest_kind": manifest.get("manifest_kind"),
        "manifest_schema_version": manifest.get("schema_version"),
        "title": manifest.get("title"),
        "pdf_url": manifest.get("pdf_url"),
        "pdf_sha256": manifest.get("pdf_sha256"),
        "source_text_sha256": manifest.get("source_text_sha256"),
        "artifact_ref": manifest.get("artifact_ref"),
        "evidence_span_count": _evidence_span_count(manifest, admission_item),
        "admitted_status": "admitted",
        "last_verified_status": "verified" if admission_item.get("local_verification") else None,
        "portability_flags": manifest.get("portability") if isinstance(manifest.get("portability"), dict) else {},
        "known_exclusions": manifest.get("known_exclusions") if isinstance(manifest.get("known_exclusions"), list) else [],
        "lifecycle_status": "registry_proposed",
        "created_by_phase": generated_by_phase,
        "updated_by_phase": None,
        "notes": "",
    }


def generate_registry_dry_run(
    manifest_paths: Sequence[str | Path],
    *,
    topic_id: str,
    generated_by_phase: str = DEFAULT_GENERATED_BY_PHASE,
    registry_id: str | None = None,
    project_root: str | Path | None = None,
    strict: bool = False,
    allow_untracked_manifest: bool = False,
    require_local_verified: bool = True,
    include_notes: bool = True,
) -> dict[str, Any]:
    del include_notes
    root = Path(project_root or constants.PROJECT_ROOT).resolve()
    report = _base_report(topic_id, manifest_paths)

    if not manifest_paths:
        report["reasons"].append(_reason("registry_generation_empty_manifest_set"))
        return report

    try:
        admission_report = evidence_manifest_admission.check_batch_admission(
            manifest_paths,
            topic_id=topic_id,
            require_tracked=not allow_untracked_manifest,
            require_local_verified=require_local_verified,
            project_root=root,
            strict=strict,
        )
        report["admission_report"] = admission_report
        if admission_report.get("status") != "admitted":
            report["status"] = "registry_generation_admission_failed"
            report["reasons"].append(_reason("registry_generation_admission_failed"))
            return report

        entries: list[dict[str, Any]] = []
        for admission_item in admission_report.get("manifests", []):
            raw_manifest_path = Path(str(admission_item.get("manifest_path") or ""))
            manifest_path = raw_manifest_path if raw_manifest_path.is_absolute() else root / raw_manifest_path
            try:
                manifest_sha256 = _raw_sha256(manifest_path)
            except FileNotFoundError:
                report["reasons"].append(_reason("registry_generation_manifest_missing", manifest_path=str(raw_manifest_path)))
                return report
            except OSError:
                report["reasons"].append(_reason("registry_generation_manifest_hash_failed", manifest_path=str(raw_manifest_path)))
                return report
            try:
                manifest = _load_manifest(manifest_path)
            except (json.JSONDecodeError, ValueError, OSError):
                report["reasons"].append(_reason("registry_generation_manifest_parse_failed", manifest_path=str(raw_manifest_path)))
                return report
            entries.append(
                _registry_entry(
                    manifest_path=raw_manifest_path,
                    manifest=manifest,
                    admission_item=admission_item,
                    manifest_sha256=manifest_sha256,
                    generated_by_phase=generated_by_phase,
                )
            )

        proposed_registry = {
            "registry_schema_version": REGISTRY_SCHEMA_VERSION,
            "registry_id": registry_id or f"{topic_id}-m26-p1-dry-run",
            "topic_id": topic_id,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "generated_by_phase": generated_by_phase,
            "source_manifest_count": len(entries),
            "policy_version": POLICY_VERSION,
            "hashing_policy": {
                "manifest_hash": "raw_sha256",
                "canonical_json": False,
            },
            "manifest_entries": entries,
            "aggregate_hashes": {
                "manifest_count": len(entries),
                "entries_sha256": _entries_sha256(entries),
            },
            "notes": "",
        }
        report.update(
            {
                "status": "registry_generate_dry_run_planned",
                "generated_entry_count": len(entries),
                "proposed_registry": proposed_registry,
            }
        )
        return report
    except Exception as exc:  # pragma: no cover - defensive report boundary
        report["reasons"].append(_reason("unknown_error", error=exc.__class__.__name__))
        return report

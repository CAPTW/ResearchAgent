from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.domain import constants

REGISTRY_SCHEMA_VERSION = 1
SUPPORTED_MANIFEST_SCHEMA_VERSION = 1
EXPECTED_MANIFEST_KIND = "paper_full_text_evidence_replay"

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "manifest_registry_missing": ("Registry file does not exist.", True, True, "Select an existing registry file or generate one in a separate approved phase."),
    "registry_invalid_json": ("Registry JSON is malformed.", True, True, "Fix registry JSON before checking it."),
    "registry_not_object": ("Registry JSON root is not an object.", True, True, "Regenerate the registry as a JSON object."),
    "registry_schema_unsupported": ("Registry schema version is unsupported.", True, True, "Use registry_schema_version 1 or migrate in a separate phase."),
    "registry_topic_mismatch": ("Registry topic does not match the requested topic.", True, True, "Use a registry for the requested topic or correct topic_id."),
    "registry_contains_forbidden_content": ("Registry embeds forbidden content.", True, True, "Remove PDF bytes, full text, pages content, or other raw artifacts."),
    "registry_contains_secret_like_value": ("Registry contains a secret-like key or value.", True, True, "Remove secrets and rotate credentials if needed."),
    "manifest_not_in_registry": ("Expected manifest is not listed in the registry.", True, True, "Add the manifest in an explicit registry update phase."),
    "manifest_hash_mismatch": ("Manifest raw-byte SHA256 does not match the registry entry.", True, True, "Review manifest drift and update the registry only after approval."),
    "manifest_registry_duplicate_paper": ("Registry contains duplicate paper_id entries.", True, True, "Remove duplicate registry entries."),
    "manifest_registry_entry_stale": ("Registry entry appears stale.", True, True, "Recheck the manifest and update the registry only after approval."),
    "manifest_registry_entry_missing_hash": ("Registry entry is missing a valid manifest_sha256.", True, True, "Compute raw-byte manifest_sha256 in a separate approved phase."),
    "manifest_registry_topic_mismatch": ("Registry entry topic does not match the registry topic.", True, True, "Fix the entry topic metadata."),
    "manifest_registry_schema_unsupported": ("Registry entry references an unsupported manifest schema.", True, True, "Migrate the manifest in a separate phase."),
    "registry_manifest_missing": ("Registry entry manifest file does not exist.", True, True, "Restore the manifest file or remove the registry entry."),
    "registry_manifest_path_outside_project": ("Registry entry manifest path resolves outside the project root.", True, True, "Use a project-relative tracked manifest path."),
    "registry_manifest_symlink_disallowed": ("Registry entry manifest path is a symlink.", True, True, "Use the resolved manifest file directly."),
    "registry_manifest_invalid_json": ("Registry entry manifest JSON is malformed or not an object.", True, True, "Fix the manifest JSON before registry use."),
    "registry_manifest_field_mismatch": ("Registry entry fields do not match the manifest.", True, True, "Review manifest drift and update the registry only after approval."),
    "registry_update_required": ("Registry update is required before this registry can be trusted.", True, True, "Prepare a reviewed registry update in a separate phase."),
    "unknown_error": ("Unexpected registry checker error.", True, True, "Stop and diagnose before retrying."),
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
    message, operator_fixable, blocks_registry, safe_next_action = _REASON_META.get(code, _REASON_META["unknown_error"])
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "blocks_registry": blocks_registry,
        "operator_fixable": operator_fixable,
        "safe_next_action": safe_next_action,
    }
    payload.update({key: value for key, value in context.items() if value is not None})
    return payload


def _add_reason(reasons: list[dict[str, Any]], code: str, **context: Any) -> None:
    if code not in {reason["code"] for reason in reasons}:
        reasons.append(_reason(code, **context))


def _valid_sha(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path_is_inside_root(path: Path, project_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(project_root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _resolve_manifest_path(raw_path: Any, project_root: Path) -> tuple[Path, str | None]:
    text = str(raw_path or "").strip()
    if not text:
        return project_root, "registry_manifest_path_outside_project"
    path = Path(text)
    candidate = path if path.is_absolute() else project_root / path
    if not _path_is_inside_root(candidate, project_root):
        return candidate, "registry_manifest_path_outside_project"
    if candidate.is_symlink():
        return candidate, "registry_manifest_symlink_disallowed"
    return candidate, None


def _read_json_object(path: Path, invalid_code: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None, "manifest_registry_missing"
    except json.JSONDecodeError:
        return None, invalid_code
    except OSError:
        return None, "unknown_error"
    if not isinstance(payload, dict):
        return None, "registry_not_object" if invalid_code == "registry_invalid_json" else invalid_code
    return payload, None


def _walk_registry_safety(value: Any, reasons: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _FULL_TEXT_KEYS or lowered in _PDF_CONTENT_KEYS or lowered in _PAGES_CONTENT_KEYS:
                _add_reason(reasons, "registry_contains_forbidden_content")
            if lowered in _SECRET_KEYS or any(secret_key in lowered for secret_key in _SECRET_KEYS):
                _add_reason(reasons, "registry_contains_secret_like_value")
            _walk_registry_safety(item, reasons)
    elif isinstance(value, list):
        for item in value:
            _walk_registry_safety(item, reasons)
    elif isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        _add_reason(reasons, "registry_contains_secret_like_value")


def _empty_entry(entry: dict[str, Any] | None, index: int) -> dict[str, Any]:
    entry = entry or {}
    return {
        "index": index,
        "paper_id": str(entry.get("paper_id") or ""),
        "manifest_path": str(entry.get("manifest_path") or ""),
        "manifest_exists": False,
        "manifest_sha256_expected": str(entry.get("manifest_sha256") or ""),
        "manifest_sha256_actual": None,
        "manifest_hash_matched": False,
        "manifest_kind_matched": False,
        "schema_version_matched": False,
        "topic_matched": False,
        "pdf_hash_matched": False,
        "source_text_hash_matched": False,
        "artifact_ref_matched": False,
        "status": "rejected",
        "reasons": [],
    }


def _compare_manifest_fields(entry: dict[str, Any], manifest: dict[str, Any], result: dict[str, Any]) -> None:
    expected_schema = entry.get("manifest_schema_version")
    result["manifest_kind_matched"] = entry.get("manifest_kind") == manifest.get("manifest_kind")
    result["schema_version_matched"] = expected_schema == manifest.get("schema_version")
    result["topic_matched"] = entry.get("topic_id", manifest.get("topic_id")) == manifest.get("topic_id")
    result["pdf_hash_matched"] = entry.get("pdf_sha256") == manifest.get("pdf_sha256")
    result["source_text_hash_matched"] = entry.get("source_text_sha256") == manifest.get("source_text_sha256")
    result["artifact_ref_matched"] = entry.get("artifact_ref") == manifest.get("artifact_ref")
    paper_id_matched = entry.get("paper_id") == manifest.get("paper_id")
    if not all(
        [
            paper_id_matched,
            result["manifest_kind_matched"],
            result["schema_version_matched"],
            result["topic_matched"],
            result["pdf_hash_matched"],
            result["source_text_hash_matched"],
            result["artifact_ref_matched"],
        ]
    ):
        _add_reason(result["reasons"], "registry_manifest_field_mismatch")


def _status_for_entry(reasons: list[dict[str, Any]]) -> str:
    codes = {reason["code"] for reason in reasons}
    if not codes:
        return "verified"
    if "registry_manifest_missing" in codes:
        return "missing"
    if {"manifest_hash_mismatch", "registry_manifest_field_mismatch"} & codes:
        return "drifted"
    return "rejected"


def _process_entry(entry: dict[str, Any], *, index: int, project_root: Path, registry_topic: str | None, duplicate_papers: set[str]) -> dict[str, Any]:
    result = _empty_entry(entry, index)
    paper_id = str(entry.get("paper_id") or "").strip()
    manifest_path, path_error = _resolve_manifest_path(entry.get("manifest_path"), project_root)

    if paper_id in duplicate_papers:
        _add_reason(result["reasons"], "manifest_registry_duplicate_paper")
    if not _valid_sha(entry.get("manifest_sha256")):
        _add_reason(result["reasons"], "manifest_registry_entry_missing_hash")
    if entry.get("topic_id") not in {None, registry_topic}:
        _add_reason(result["reasons"], "manifest_registry_topic_mismatch")
    if entry.get("manifest_schema_version") != SUPPORTED_MANIFEST_SCHEMA_VERSION:
        _add_reason(result["reasons"], "manifest_registry_schema_unsupported")
    if entry.get("admitted_status") not in {None, "admitted"} or entry.get("last_verified_status") not in {None, "verified"}:
        _add_reason(result["reasons"], "manifest_registry_entry_stale")

    if path_error:
        _add_reason(result["reasons"], path_error)
        result["status"] = _status_for_entry(result["reasons"])
        return result

    result["manifest_path"] = str(entry.get("manifest_path") or "")
    if not manifest_path.exists():
        _add_reason(result["reasons"], "registry_manifest_missing")
        result["status"] = _status_for_entry(result["reasons"])
        return result

    result["manifest_exists"] = True
    try:
        actual_sha = _raw_sha256(manifest_path)
    except OSError:
        _add_reason(result["reasons"], "unknown_error")
        result["status"] = _status_for_entry(result["reasons"])
        return result

    result["manifest_sha256_actual"] = actual_sha
    result["manifest_hash_matched"] = str(entry.get("manifest_sha256") or "").lower() == actual_sha
    if not result["manifest_hash_matched"]:
        _add_reason(result["reasons"], "manifest_hash_mismatch")
        _add_reason(result["reasons"], "registry_update_required")

    manifest, read_error = _read_json_object(manifest_path, "registry_manifest_invalid_json")
    if read_error:
        _add_reason(result["reasons"], "registry_manifest_invalid_json" if read_error == "registry_manifest_invalid_json" else read_error)
        result["status"] = _status_for_entry(result["reasons"])
        return result

    _compare_manifest_fields(entry, manifest or {}, result)
    if any(reason["code"] == "registry_manifest_field_mismatch" for reason in result["reasons"]):
        _add_reason(result["reasons"], "registry_update_required")
    result["status"] = _status_for_entry(result["reasons"])
    return result


def _final_status(top_reasons: list[dict[str, Any]], entries: list[dict[str, Any]], *, missing_registry: bool = False) -> str:
    if missing_registry:
        return "registry_missing"
    if top_reasons or any(entry["status"] in {"rejected", "missing", "drifted"} for entry in entries):
        return "registry_rejected"
    if any(entry["status"] == "warning" for entry in entries):
        return "registry_warning"
    return "registry_verified"


def _base_report(registry_path: str | Path, topic_id: str | None) -> dict[str, Any]:
    return {
        "status": "failed",
        "registry_path": str(registry_path),
        "registry_schema_version": None,
        "topic_id": topic_id,
        "entry_count": 0,
        "verified_count": 0,
        "rejected_count": 0,
        "warning_count": 0,
        "drift_count": 0,
        "duplicate_count": 0,
        "missing_manifest_count": 0,
        "safe_to_use_registry_for_admission": False,
        "safe_to_claim_registry_integrity": False,
        "safe_to_claim_clean_clone_reproducible": False,
        "safe_to_claim_arbitrary_manifest_automation": False,
        "network_attempted": False,
        "write_attempted": False,
        "entries": [],
        "reasons": [],
    }


def check_manifest_registry(
    registry_path: str | Path,
    *,
    topic_id: str | None = None,
    project_root: str | Path | None = None,
    strict: bool = False,
    allow_untracked_registry: bool = False,
) -> dict[str, Any]:
    del strict, allow_untracked_registry
    root = Path(project_root or constants.PROJECT_ROOT).resolve()
    report = _base_report(registry_path, topic_id)

    try:
        registry, read_error = _read_json_object(Path(registry_path), "registry_invalid_json")
        if read_error:
            code = "manifest_registry_missing" if read_error == "manifest_registry_missing" else read_error
            _add_reason(report["reasons"], code)
            report["status"] = _final_status(report["reasons"], [], missing_registry=code == "manifest_registry_missing")
            return report

        registry = registry or {}
        report["registry_schema_version"] = registry.get("registry_schema_version")
        report["topic_id"] = registry.get("topic_id")
        _walk_registry_safety(registry, report["reasons"])

        if registry.get("registry_schema_version") != REGISTRY_SCHEMA_VERSION:
            _add_reason(report["reasons"], "registry_schema_unsupported")
        if topic_id and registry.get("topic_id") != topic_id:
            _add_reason(report["reasons"], "registry_topic_mismatch")

        entries_raw = registry.get("manifest_entries")
        entries = entries_raw if isinstance(entries_raw, list) else []
        if not isinstance(entries_raw, list):
            _add_reason(report["reasons"], "registry_schema_unsupported")

        paper_counts = Counter(str(entry.get("paper_id") or "").strip() for entry in entries if isinstance(entry, dict))
        duplicate_papers = {paper_id for paper_id, count in paper_counts.items() if paper_id and count > 1}
        if duplicate_papers:
            _add_reason(report["reasons"], "manifest_registry_duplicate_paper")

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                entry_result = _empty_entry({}, index)
                _add_reason(entry_result["reasons"], "registry_schema_unsupported")
            else:
                entry_result = _process_entry(
                    entry,
                    index=index,
                    project_root=root,
                    registry_topic=registry.get("topic_id"),
                    duplicate_papers=duplicate_papers,
                )
            report["entries"].append(entry_result)
            for reason in entry_result["reasons"]:
                _add_reason(report["reasons"], reason["code"])

        report["entry_count"] = len(report["entries"])
        report["verified_count"] = sum(1 for entry in report["entries"] if entry["status"] == "verified")
        report["warning_count"] = sum(1 for entry in report["entries"] if entry["status"] == "warning")
        report["drift_count"] = sum(
            1
            for entry in report["entries"]
            if any(reason["code"] in {"manifest_hash_mismatch", "registry_manifest_field_mismatch"} for reason in entry["reasons"])
        )
        report["duplicate_count"] = sum(
            1 for entry in report["entries"] if any(reason["code"] == "manifest_registry_duplicate_paper" for reason in entry["reasons"])
        )
        report["missing_manifest_count"] = sum(1 for entry in report["entries"] if entry["status"] == "missing")
        report["rejected_count"] = sum(1 for entry in report["entries"] if entry["status"] in {"rejected", "missing", "drifted"})
        report["status"] = _final_status(report["reasons"], report["entries"])
        report["safe_to_use_registry_for_admission"] = report["status"] == "registry_verified"
        report["safe_to_claim_registry_integrity"] = report["status"] == "registry_verified"
        return report
    except Exception as exc:  # pragma: no cover - defensive report boundary
        _add_reason(report["reasons"], "unknown_error", error=exc.__class__.__name__)
        report["status"] = "failed"
        return report

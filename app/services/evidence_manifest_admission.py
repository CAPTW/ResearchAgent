from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

from app.domain import constants
from app.services import evidence_manifest_verifier

EXPECTED_MANIFEST_KIND = "paper_full_text_evidence_replay"
SUPPORTED_SCHEMA_VERSION = 1
MAX_PDF_BYTES = 25_000_000
MAX_TIMEOUT_SECONDS = 30
MAX_MANIFEST_BYTES = 1_000_000
ALLOW_DOMAIN = "arxiv.org"

VerifyFunc = Callable[..., dict[str, Any]]
PlanFunc = Callable[[str | Path], dict[str, Any]]


_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "manifest_missing": ("Manifest path does not exist.", True, True, "Select the correct path or create the manifest in a separate approved phase."),
    "manifest_invalid_json": ("Manifest JSON is malformed.", True, True, "Fix manifest JSON before admission review."),
    "manifest_not_object": ("Manifest JSON root is not an object.", True, True, "Regenerate the manifest as a JSON object."),
    "manifest_path_outside_project": ("Manifest path resolves outside the approved project root.", True, True, "Move the manifest under the project root or use an explicit approved path policy."),
    "manifest_symlink_disallowed": ("Manifest path is a symlink, which is disallowed by default.", True, True, "Use the resolved tracked manifest file directly."),
    "manifest_file_too_large": ("Manifest file exceeds the admission size limit.", True, True, "Regenerate a compact metadata-only manifest."),
    "manifest_untracked_or_unapproved": ("Manifest is not tracked and was not explicitly approved.", True, True, "Track the manifest or rerun with explicit untracked approval."),
    "schema_version_unsupported": ("Manifest schema version is unsupported.", True, True, "Update the manifest schema in a separate manifest-authoring phase."),
    "manifest_kind_mismatch": ("Manifest kind is not paper_full_text_evidence_replay.", True, True, "Use the supported replay manifest kind."),
    "topic_mismatch": ("Manifest topic does not match the batch topic.", True, True, "Move it to the correct batch or fix topic_id."),
    "paper_id_missing": ("Manifest paper_id is missing.", True, True, "Add the correct paper_id."),
    "duplicate_paper_in_batch": ("The same paper_id appears more than once in the batch.", True, True, "Remove duplicate papers before replay planning."),
    "non_arxiv_domain": ("Manifest PDF domain or acquisition domain is not arxiv.org.", True, True, "Use an arXiv HTTPS PDF URL and arxiv.org acquisition domain."),
    "non_https_pdf_url": ("Manifest PDF URL is not HTTPS.", True, True, "Replace the PDF URL with an HTTPS URL."),
    "missing_pdf_sha256": ("PDF SHA256 is missing or invalid.", True, True, "Create the hash in a separate approved bounded fetch phase."),
    "missing_source_text_sha256": ("Source text SHA256 is missing or invalid.", True, True, "Create the source text hash after extraction validation."),
    "missing_evidence_spans": ("Evidence span count is missing or less than one.", True, True, "Add validated evidence span metadata."),
    "missing_artifact_ref": ("Portable source artifact reference is missing.", True, True, "Add a portable run: artifact reference."),
    "unsafe_artifact_ref": ("Artifact reference is not portable or safe.", True, True, "Use a safe portable run: artifact reference."),
    "contains_full_text": ("Manifest embeds full text or excerpt content.", True, True, "Remove text content and keep hashes/metadata only."),
    "contains_pdf_bytes": ("Manifest embeds PDF bytes or raw PDF content.", True, True, "Remove PDF content and keep hashes only."),
    "contains_pages_content": ("Manifest embeds pages JSON content.", True, True, "Keep only pages artifact refs or page hashes."),
    "contains_secret_like_value": ("Manifest contains a secret-like key or value.", True, True, "Remove secrets and rotate if necessary."),
    "missing_bounded_provenance": ("Bounded acquisition provenance is missing.", True, True, "Add bounded acquisition command and limits metadata."),
    "max_pdf_bytes_too_large": ("max_pdf_bytes exceeds policy.", True, True, "Lower max_pdf_bytes to 25000000 or less."),
    "download_limit_not_one": ("download_limit is not one.", True, True, "Set download_limit to 1."),
    "operator_gate_missing": ("operator_gate_required is missing or false.", True, True, "Add the operator gate requirement."),
    "local_verification_failed": ("Local manifest verification failed.", True, True, "Fix local evidence state or exclude the manifest."),
    "plan_replay_not_print_only": ("Replay plan is not print-only.", False, True, "Stop and fix planning behavior before batch use."),
    "unknown_field": ("Manifest contains an unknown top-level field.", True, False, "Review the field and add it to policy only if it is safe metadata."),
    "unknown_error": ("Unexpected admission checker error.", False, True, "Stop, preserve evidence, and diagnose before retry."),
}

_ALLOWED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "manifest_kind",
    "topic_id",
    "paper_id",
    "title",
    "pdf_url",
    "pdf_domain",
    "pdf_sha256",
    "source_text_sha256",
    "artifact_ref",
    "pages_artifact_ref",
    "pdf_artifact_ref",
    "extraction",
    "review",
    "acquisition",
    "known_already_verified",
    "selection_exclusions",
    "known_exclusions",
    "coverage",
    "portability",
    "created_from_commit",
    "notes",
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


def _reason(code: str) -> dict[str, Any]:
    message, operator_fixable, blocks_batch, safe_next_action = _REASON_META.get(code, _REASON_META["unknown_error"])
    return {
        "code": code,
        "message": message,
        "blocks_batch": blocks_batch,
        "operator_fixable": operator_fixable,
        "safe_next_action": safe_next_action,
    }


def _add_reason(reasons: list[dict[str, Any]], code: str) -> None:
    if code not in {reason["code"] for reason in reasons}:
        reasons.append(_reason(code))


def _valid_sha(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None, "manifest_file_too_large"
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None, "manifest_missing"
    except json.JSONDecodeError:
        return None, "manifest_invalid_json"
    except OSError:
        return None, "unknown_error"
    if not isinstance(payload, dict):
        return None, "manifest_not_object"
    return payload, None


def _path_is_inside_root(path: Path, *, project_root: Path) -> bool:
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    return True


def _is_tracked(path: Path, *, project_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--error-unmatch", "--", relative.as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _safe_artifact_ref(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text.startswith("run:"):
        return False
    relative = text[len("run:") :]
    if not relative or relative.startswith("/") or "://" in relative:
        return False
    parts = [part for part in relative.split("/") if part not in {"", "."}]
    return bool(parts) and all(part != ".." and not part.endswith(":") for part in parts)


def _walk_content_safety(value: Any, reasons: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _FULL_TEXT_KEYS:
                _add_reason(reasons, "contains_full_text")
            if lowered in _PDF_CONTENT_KEYS:
                _add_reason(reasons, "contains_pdf_bytes")
            if lowered in _PAGES_CONTENT_KEYS:
                _add_reason(reasons, "contains_pages_content")
            if lowered in _SECRET_KEYS or any(secret_key in lowered for secret_key in _SECRET_KEYS):
                _add_reason(reasons, "contains_secret_like_value")
            _walk_content_safety(item, reasons)
    elif isinstance(value, list):
        for item in value:
            _walk_content_safety(item, reasons)
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            _add_reason(reasons, "contains_secret_like_value")


def _validate_unknown_top_level_fields(manifest: dict[str, Any], warnings: list[dict[str, Any]]) -> None:
    for key in manifest:
        if key not in _ALLOWED_TOP_LEVEL_FIELDS:
            _add_reason(warnings, "unknown_field")
            return


def _validate_identity(manifest: dict[str, Any], *, topic_id: str, reasons: list[dict[str, Any]]) -> None:
    if manifest.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        _add_reason(reasons, "schema_version_unsupported")
    if manifest.get("manifest_kind") != EXPECTED_MANIFEST_KIND:
        _add_reason(reasons, "manifest_kind_mismatch")
    if str(manifest.get("topic_id") or "") != str(topic_id):
        _add_reason(reasons, "topic_mismatch")
    if not str(manifest.get("paper_id") or "").strip():
        _add_reason(reasons, "paper_id_missing")
    if not str(manifest.get("title") or "").strip():
        _add_reason(reasons, "unknown_error")

    pdf_url = str(manifest.get("pdf_url") or "").strip()
    parsed = urlparse(pdf_url)
    if not pdf_url:
        _add_reason(reasons, "non_https_pdf_url")
    elif parsed.scheme != "https":
        _add_reason(reasons, "non_https_pdf_url")
    domain = str(manifest.get("pdf_domain") or "").strip().lower()
    if domain != ALLOW_DOMAIN or (parsed.netloc and parsed.netloc.lower() != ALLOW_DOMAIN):
        _add_reason(reasons, "non_arxiv_domain")


def _validate_hash_evidence(manifest: dict[str, Any], reasons: list[dict[str, Any]]) -> None:
    if not _valid_sha(manifest.get("pdf_sha256")):
        _add_reason(reasons, "missing_pdf_sha256")
    if not _valid_sha(manifest.get("source_text_sha256")):
        _add_reason(reasons, "missing_source_text_sha256")
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    if int(review.get("evidence_span_count") or 0) < 1:
        _add_reason(reasons, "missing_evidence_spans")
    if not str(manifest.get("artifact_ref") or "").strip():
        _add_reason(reasons, "missing_artifact_ref")
    elif not _safe_artifact_ref(manifest.get("artifact_ref")):
        _add_reason(reasons, "unsafe_artifact_ref")
    extraction = manifest.get("extraction") if isinstance(manifest.get("extraction"), dict) else {}
    page_count = _as_int(extraction.get("page_count"))
    extracted_chars = _as_int(extraction.get("extracted_chars"))
    if page_count is None or page_count <= 0 or extracted_chars is None or extracted_chars <= 0:
        _add_reason(reasons, "missing_evidence_spans")


def _validate_acquisition(manifest: dict[str, Any], reasons: list[dict[str, Any]]) -> None:
    acquisition = manifest.get("acquisition") if isinstance(manifest.get("acquisition"), dict) else {}
    if not acquisition or not str(acquisition.get("command_provenance") or "").strip() or not str(acquisition.get("allow_domain") or "").strip():
        _add_reason(reasons, "missing_bounded_provenance")
    if str(acquisition.get("allow_domain") or "").strip().lower() != ALLOW_DOMAIN:
        _add_reason(reasons, "non_arxiv_domain")
    download_limit = _as_int(acquisition.get("download_limit"))
    max_pdf_bytes = _as_int(acquisition.get("max_pdf_bytes"))
    timeout_seconds = _as_int(acquisition.get("timeout_seconds"))
    if download_limit != 1:
        _add_reason(reasons, "download_limit_not_one")
    if max_pdf_bytes is None or max_pdf_bytes > MAX_PDF_BYTES or max_pdf_bytes <= 0:
        _add_reason(reasons, "max_pdf_bytes_too_large")
    if timeout_seconds is None or timeout_seconds > MAX_TIMEOUT_SECONDS or timeout_seconds <= 0:
        _add_reason(reasons, "missing_bounded_provenance")
    if acquisition.get("operator_gate_required") is not True:
        _add_reason(reasons, "operator_gate_missing")


def _validate_portability(manifest: dict[str, Any], reasons: list[dict[str, Any]]) -> None:
    portability = manifest.get("portability") if isinstance(manifest.get("portability"), dict) else {}
    if portability.get("do_not_commit_pdf") is not True or portability.get("do_not_commit_full_text") is not True:
        _add_reason(reasons, "missing_bounded_provenance")
    if portability.get("replay_requires_bounded_network") is not True:
        _add_reason(reasons, "missing_bounded_provenance")
    if manifest.get("safe_to_claim_clean_clone_reproducible") is True:
        _add_reason(reasons, "local_verification_failed")


def _verify_local(
    path: Path,
    *,
    topic_id: str,
    strict: bool,
    verify_func: VerifyFunc,
    reasons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        result = verify_func(str(path), topic_id=topic_id, strict=strict)
    except Exception:
        _add_reason(reasons, "local_verification_failed")
        return None
    if (
        result.get("status") != "verified"
        or result.get("local_status") != "reviewed"
        or result.get("safe_to_claim_local_verified") is not True
        or result.get("safe_to_claim_clean_clone_reproducible") is not False
    ):
        _add_reason(reasons, "local_verification_failed")
    return result


def _check_plan(path: Path, *, plan_func: PlanFunc, reasons: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        plan = plan_func(str(path))
    except Exception:
        _add_reason(reasons, "plan_replay_not_print_only")
        return None
    if (
        plan.get("status") != "plan_only"
        or plan.get("runs_network") is True
        or plan.get("writes_storage_or_db") is True
        or plan.get("safe_to_run_automatically") is True
    ):
        _add_reason(reasons, "plan_replay_not_print_only")
    return plan


def check_batch_admission(
    manifest_paths: Sequence[str | Path],
    *,
    topic_id: str,
    require_tracked: bool = True,
    require_local_verified: bool = True,
    project_root: str | Path | None = None,
    strict: bool = False,
    verify_func: VerifyFunc = evidence_manifest_verifier.verify_manifest,
    plan_func: PlanFunc = evidence_manifest_verifier.build_replay_plan,
) -> dict[str, Any]:
    root = Path(project_root or constants.PROJECT_ROOT).resolve()
    paths = [Path(path) for path in manifest_paths]
    loaded: list[tuple[Path, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]] = []
    paper_counts: Counter[str] = Counter()

    for path in paths:
        reasons: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if path.exists() and path.is_symlink():
            _add_reason(reasons, "manifest_symlink_disallowed")
        manifest, load_error = _read_manifest(path)
        if load_error:
            _add_reason(reasons, load_error)
        if manifest is not None:
            paper = str(manifest.get("paper_id") or "").strip()
            if paper:
                paper_counts[paper] += 1
        loaded.append((path, manifest, reasons, warnings))

    manifest_results: list[dict[str, Any]] = []
    aggregate_rejections: list[dict[str, Any]] = []
    aggregate_warnings: list[dict[str, Any]] = []

    for index, (path, manifest, reasons, warnings) in enumerate(loaded):
        tracked = _is_tracked(path, project_root=root) if path.exists() else False
        if path.exists() and require_tracked and not _path_is_inside_root(path, project_root=root):
            _add_reason(reasons, "manifest_path_outside_project")
        if require_tracked and not tracked:
            _add_reason(reasons, "manifest_untracked_or_unapproved")

        local_verification: dict[str, Any] | None = None
        replay_plan: dict[str, Any] | None = None
        if manifest is not None:
            _validate_identity(manifest, topic_id=topic_id, reasons=reasons)
            paper = str(manifest.get("paper_id") or "").strip()
            if paper and paper_counts[paper] > 1:
                _add_reason(reasons, "duplicate_paper_in_batch")
            _validate_hash_evidence(manifest, reasons)
            _walk_content_safety(manifest, reasons)
            _validate_unknown_top_level_fields(manifest, warnings)
            _validate_acquisition(manifest, reasons)
            _validate_portability(manifest, reasons)
            if not reasons:
                if require_local_verified:
                    local_verification = _verify_local(path, topic_id=topic_id, strict=strict, verify_func=verify_func, reasons=reasons)
                replay_plan = _check_plan(path, plan_func=plan_func, reasons=reasons)

        manifest_status = "rejected" if reasons else "warning" if warnings else "admitted"
        item_rejections = [
            {
                **reason,
                "manifest_index": index,
                "manifest_path": str(path),
                "paper_id": manifest.get("paper_id") if manifest else None,
            }
            for reason in reasons
        ]
        item_warnings = [
            {
                **warning,
                "manifest_index": index,
                "manifest_path": str(path),
                "paper_id": manifest.get("paper_id") if manifest else None,
            }
            for warning in warnings
        ]
        aggregate_rejections.extend(item_rejections)
        aggregate_warnings.extend(item_warnings)
        manifest_results.append(
            {
                "index": index,
                "manifest_path": str(path),
                "status": manifest_status,
                "paper_id": manifest.get("paper_id") if manifest else None,
                "topic_id": manifest.get("topic_id") if manifest else None,
                "schema_version": manifest.get("schema_version") if manifest else None,
                "manifest_kind": manifest.get("manifest_kind") if manifest else None,
                "tracked": tracked,
                "reasons": item_rejections,
                "warnings": item_warnings,
                "local_verification": local_verification,
                "plan_replay": replay_plan,
            }
        )

    rejected_count = sum(1 for item in manifest_results if item["status"] == "rejected")
    warning_count = len(aggregate_warnings)
    admitted_count = sum(1 for item in manifest_results if item["status"] in {"admitted", "warning"})
    status = "admitted" if rejected_count == 0 else "rejected"
    return {
        "status": status,
        "topic_id": topic_id,
        "batch_size": len(paths),
        "admitted_count": admitted_count,
        "rejected_count": rejected_count,
        "warning_count": warning_count,
        "safe_to_run_dry_run_batch": status == "admitted",
        "safe_to_run_live_batch": False,
        "safe_to_claim_arbitrary_manifest_automation": False,
        "manifests": manifest_results,
        "rejections": aggregate_rejections,
        "warnings": aggregate_warnings,
        "reasons": sorted({reason["code"] for reason in aggregate_rejections}),
        "read_only": True,
        "network_attempted": False,
        "write_attempted": False,
    }

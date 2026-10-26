from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.db import bootstrap
from app.repositories import artifact_refs

EXPECTED_MANIFEST_KIND = "paper_full_text_evidence_replay"
FORBIDDEN_MANIFEST_FIELDS = {
    "full_text",
    "full_text_content",
    "extracted_text",
    "pages_content",
    "page_text",
    "page_texts",
    "pdf_bytes",
    "pdf_content",
    "raw_pdf",
    "secret",
    "token",
    "api_key",
}
HASH_HEX_LENGTH = 64


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decode_json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    return payload


def load_manifest(path: str | Path) -> dict[str, Any]:
    return _read_json(Path(path))


def _walk_forbidden_fields(value: Any, *, prefix: str = "") -> list[str]:
    reasons: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in FORBIDDEN_MANIFEST_FIELDS:
                reasons.append(f"forbidden_manifest_field:{key_text}")
            reasons.extend(_walk_forbidden_fields(item, prefix=f"{prefix}.{key_text}" if prefix else key_text))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reasons.extend(_walk_forbidden_fields(item, prefix=f"{prefix}[{index}]"))
    return reasons


def _valid_sha(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == HASH_HEX_LENGTH and all(char in "0123456789abcdefABCDEF" for char in text)


def _validate_manifest_payload(
    manifest: dict[str, Any],
    *,
    topic_id: str | None = None,
    paper_id: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    if manifest.get("schema_version") != 1:
        reasons.append("unsupported_schema_version")
    if manifest.get("manifest_kind") != EXPECTED_MANIFEST_KIND:
        reasons.append("invalid_manifest_kind")
    for key in ("topic_id", "paper_id", "title", "pdf_url", "pdf_domain", "source_text_sha256", "artifact_ref"):
        if not str(manifest.get(key) or "").strip():
            reasons.append(f"missing_required_field:{key}")
    if not _valid_sha(manifest.get("source_text_sha256")):
        reasons.append("invalid_source_text_sha256")
    if manifest.get("pdf_sha256") and not _valid_sha(manifest.get("pdf_sha256")):
        reasons.append("invalid_pdf_sha256")
    pdf_url = str(manifest.get("pdf_url") or "").strip()
    if pdf_url:
        parsed = urlparse(pdf_url)
        if parsed.scheme != "https":
            reasons.append("pdf_url_must_be_https")
        expected_domain = str(manifest.get("pdf_domain") or "").strip().lower()
        if expected_domain and parsed.netloc.lower() != expected_domain:
            reasons.append("pdf_domain_mismatch")
    review = manifest.get("review")
    if not isinstance(review, dict):
        reasons.append("missing_review_block")
    else:
        if str(review.get("expected_status") or "").strip() != "reviewed":
            reasons.append("invalid_expected_status")
        if int(review.get("evidence_span_count") or 0) < 1:
            reasons.append("invalid_evidence_span_count")
    portability = manifest.get("portability")
    if not isinstance(portability, dict):
        reasons.append("missing_portability_block")
    elif portability.get("do_not_commit_pdf") is not True or portability.get("do_not_commit_full_text") is not True:
        reasons.append("unsafe_portability_policy")
    if topic_id and str(manifest.get("topic_id") or "") != str(topic_id):
        reasons.append("topic_id_mismatch")
    if paper_id and str(manifest.get("paper_id") or "") != str(paper_id):
        reasons.append("paper_id_mismatch")
    reasons.extend(_walk_forbidden_fields(manifest))
    return sorted(set(reasons))


def validate_manifest_payload(
    manifest: dict[str, Any],
    *,
    topic_id: str | None = None,
    paper_id: str | None = None,
) -> list[str]:
    return _validate_manifest_payload(manifest, topic_id=topic_id, paper_id=paper_id)


def _base_result(
    *,
    status: str,
    manifest: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    manifest = manifest or {}
    return {
        "status": status,
        "paper_id": manifest.get("paper_id"),
        "topic_id": manifest.get("topic_id"),
        "checks": {"passed": [], "failed": []},
        "local_status": None,
        "expected_status": (manifest.get("review") or {}).get("expected_status"),
        "local_source_text_sha256": None,
        "expected_source_text_sha256": manifest.get("source_text_sha256"),
        "local_evidence_span_count": 0,
        "expected_evidence_span_count": int((manifest.get("review") or {}).get("evidence_span_count") or 0),
        "artifact_ref": manifest.get("artifact_ref"),
        "artifact_resolved": False,
        "pdf_artifact_ref": manifest.get("pdf_artifact_ref"),
        "pdf_artifact_resolved": False,
        "replay_required": status
        in {
            "missing_manifest",
            "missing_review_row",
            "missing_artifact",
            "artifact_hash_mismatch",
            "pdf_hash_mismatch",
            "replay_required",
        },
        "safe_to_claim_local_verified": False,
        "safe_to_claim_clean_clone_reproducible": False,
        "reasons": reasons or [],
    }


def _metadata_readonly_connection() -> sqlite3.Connection | None:
    try:
        return bootstrap.connect_metadata_index(read_only=True)
    except (FileNotFoundError, sqlite3.Error):
        return None


def _fetch_review(connection: sqlite3.Connection, topic_id: str, paper_id: str) -> dict[str, Any] | None:
    try:
        row = connection.execute(
            """
            SELECT *
            FROM paper_reviews
            WHERE topic_id = ? AND paper_id = ?
            ORDER BY
              CASE full_text_status WHEN 'reviewed' THEN 0 WHEN 'full_text_available' THEN 1 ELSE 2 END,
              reviewed_at DESC
            LIMIT 1
            """,
            (topic_id, paper_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    data = dict(row)
    data["evidence_spans"] = _decode_json(data.get("evidence_spans_json"), [])
    return data


def _add_pass(result: dict[str, Any], check: str) -> None:
    result["checks"]["passed"].append(check)


def _add_fail(result: dict[str, Any], check: str, reason: str) -> None:
    result["checks"]["failed"].append(check)
    if reason not in result["reasons"]:
        result["reasons"].append(reason)


def _finalize_failure(result: dict[str, Any], status: str) -> dict[str, Any]:
    result["status"] = status
    result["safe_to_claim_local_verified"] = False
    result["safe_to_claim_clean_clone_reproducible"] = False
    result["replay_required"] = status in {
        "missing_review_row",
        "missing_artifact",
        "artifact_hash_mismatch",
        "pdf_hash_mismatch",
        "replay_required",
    }
    return result


def verify_manifest(
    manifest_path: str | Path,
    *,
    topic_id: str | None = None,
    paper_id: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    path = Path(manifest_path)
    if not path.exists():
        return _base_result(status="missing_manifest", reasons=["manifest_not_found"])
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _base_result(status="invalid_manifest", reasons=[f"manifest_parse_error:{type(exc).__name__}"])

    manifest_reasons = _validate_manifest_payload(manifest, topic_id=topic_id, paper_id=paper_id)
    if manifest_reasons:
        return _base_result(status="invalid_manifest", manifest=manifest, reasons=manifest_reasons)

    result = _base_result(status="cannot_determine", manifest=manifest)
    topic = str(manifest["topic_id"])
    paper = str(manifest["paper_id"])
    connection = _metadata_readonly_connection()
    if connection is None:
        _add_fail(result, "review_row", "metadata_index_unavailable")
        return _finalize_failure(result, "missing_review_row")
    try:
        review = _fetch_review(connection, topic, paper)
        if review is None:
            _add_fail(result, "review_row", "review_row_not_found")
            return _finalize_failure(result, "missing_review_row")
        _add_pass(result, "review_row")
        result["local_status"] = review.get("full_text_status")
        result["local_source_text_sha256"] = review.get("source_text_sha256")
        spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
        result["local_evidence_span_count"] = len(spans)

        expected_status = str((manifest.get("review") or {}).get("expected_status") or "")
        if str(review.get("full_text_status") or "") != expected_status:
            _add_fail(result, "review_status", "review_status_mismatch")
            return _finalize_failure(result, "status_mismatch")
        _add_pass(result, "review_status")

        expected_hash = str(manifest.get("source_text_sha256") or "")
        if str(review.get("source_text_sha256") or "") != expected_hash:
            _add_fail(result, "review_source_text_sha256", "source_text_sha256_mismatch")
            return _finalize_failure(result, "hash_mismatch")
        _add_pass(result, "review_source_text_sha256")

        expected_span_count = int((manifest.get("review") or {}).get("evidence_span_count") or 0)
        if len(spans) < expected_span_count:
            _add_fail(result, "evidence_spans", "insufficient_evidence_spans")
            return _finalize_failure(result, "missing_evidence_spans")
        _add_pass(result, "evidence_spans")

        artifact_ref = str(manifest.get("artifact_ref") or "")
        artifact = artifact_refs.resolve_artifact_reference(artifact_ref, run_id=str(review.get("run_id") or ""))
        result["artifact_resolved"] = bool(artifact.is_safe and artifact.exists)
        if not artifact.is_safe or not artifact.exists or not artifact.resolved_path:
            _add_fail(result, "artifact_ref", "artifact_missing_or_unsafe")
            return _finalize_failure(result, "missing_artifact")
        _add_pass(result, "artifact_ref")

        artifact_text = Path(artifact.resolved_path).read_text(encoding="utf-8")
        artifact_hash = _sha256_text(artifact_text)
        if artifact_hash != expected_hash:
            result["local_artifact_sha256"] = artifact_hash
            _add_fail(result, "artifact_source_text_sha256", "artifact_source_text_sha256_mismatch")
            return _finalize_failure(result, "artifact_hash_mismatch")
        result["local_artifact_sha256"] = artifact_hash
        _add_pass(result, "artifact_source_text_sha256")

        pdf_ref = str(manifest.get("pdf_artifact_ref") or "").strip()
        if pdf_ref:
            pdf = artifact_refs.resolve_artifact_reference(pdf_ref, run_id=str(review.get("run_id") or ""))
            result["pdf_artifact_resolved"] = bool(pdf.is_safe and pdf.exists)
            if pdf.is_safe and pdf.exists and pdf.resolved_path:
                pdf_hash = _sha256_bytes(Path(pdf.resolved_path).read_bytes())
                result["local_pdf_sha256"] = pdf_hash
                if pdf_hash != str(manifest.get("pdf_sha256") or ""):
                    _add_fail(result, "pdf_sha256", "pdf_sha256_mismatch")
                    return _finalize_failure(result, "pdf_hash_mismatch")
                _add_pass(result, "pdf_sha256")

        for exclusion in manifest.get("known_exclusions") or []:
            excluded_id = str(exclusion.get("paper_id") or "").strip()
            if not excluded_id:
                continue
            excluded_review = _fetch_review(connection, topic, excluded_id)
            if excluded_review is not None and str(excluded_review.get("full_text_status") or "") == "reviewed":
                _add_fail(result, "known_exclusions", f"known_exclusion_reviewed:{excluded_id}")
                return _finalize_failure(result, "cannot_determine")
        _add_pass(result, "known_exclusions")
    finally:
        connection.close()

    result["status"] = "verified"
    result["safe_to_claim_local_verified"] = True
    result["safe_to_claim_clean_clone_reproducible"] = False
    result["replay_required"] = False
    if strict and result["checks"]["failed"]:
        result["status"] = "cannot_determine"
        result["safe_to_claim_local_verified"] = False
    return result


def build_replay_plan(manifest_path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    reasons = _validate_manifest_payload(manifest)
    if reasons:
        return {
            "status": "invalid_manifest",
            "reasons": reasons,
            "safe_to_run_automatically": False,
            "runs_network": False,
        }
    acquisition = manifest.get("acquisition") or {}
    excluded_args: list[str] = []
    for exclusion in manifest.get("known_exclusions") or []:
        excluded_id = str(exclusion.get("paper_id") or "").strip()
        if excluded_id:
            excluded_args.extend(["--exclude-paper-id", excluded_id])
    args = [
        ".\\.venv\\Scripts\\python.exe",
        "-m",
        "app.cli.review_refresh",
        "--topic",
        str(manifest["topic_id"]),
        "--unique-limit",
        "50",
        "--use-full-text",
        "--fetch-missing-pdf",
        "--download-limit",
        str(acquisition.get("download_limit") or 1),
        "--allow-domain",
        str(acquisition.get("allow_domain") or manifest.get("pdf_domain") or "arxiv.org"),
        "--extract-after-fetch",
        "--max-pdf-bytes",
        str(acquisition.get("max_pdf_bytes") or 25000000),
        "--download-timeout",
        str(acquisition.get("timeout_seconds") or 30),
        "--target-paper-id",
        str(manifest["paper_id"]),
        *excluded_args,
        "--allow-bounded-pdf-fetch",
    ]
    return {
        "status": "plan_only",
        "paper_id": manifest["paper_id"],
        "topic_id": manifest["topic_id"],
        "command": " ".join(args),
        "requires_operator_approval": True,
        "requires_env": "ALLOW_BOUNDED_PDF_FETCH=1",
        "runs_network": False,
        "writes_storage_or_db": False,
        "safe_to_run_automatically": False,
        "notes": [
            "This plan does not run network or set ALLOW_BOUNDED_PDF_FETCH.",
            "A future replay would require explicit operator approval and ALLOW_BOUNDED_PDF_FETCH=1.",
            "Local verification is not clean-clone offline reproducibility.",
        ],
    }

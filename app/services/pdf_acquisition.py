from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, TypeGuard

from app.domain import constants
from app.domain.path_safety import PathSafetyError, is_reparse_point, resolve_within_root
from app.repositories import artifact_refs, runs as run_repository
from app.repositories.files import ensure_dir, safe_filename, write_json, write_text
from app.repositories.file_locks import FileLockError, file_lock
from app.services import research_transport, reviews as legacy_reviews

DEFAULT_ALLOWED_DOMAINS = ["arxiv.org"]
DEFAULT_MAX_PDF_BYTES = 25_000_000
DEFAULT_TIMEOUT_SECONDS = 30
BOUNDED_PDF_FETCH_ENV_VAR = "ALLOW_BOUNDED_PDF_FETCH"
MIN_EXTRACTED_TEXT_CHARS = 120
MIN_AVERAGE_EVIDENCE_SPAN_CHARS = 40
EXTRACTION_PROVENANCE_SCHEMA = "PDF_EXTRACTION_PROVENANCE_V1"
EXTRACTION_PROVENANCE_ERROR = "extraction_provenance_invalid"


class _ExtractedPages(list[dict[str, Any]]):
    """A list-compatible result with observations unavailable to legacy callers."""

    observation: dict[str, Any]

    def __init__(self) -> None:
        super().__init__()
        self.observation = {}


def _provenance_integer(value: Any, *, minimum: int = 0) -> TypeGuard[int]:
    return type(value) is int and value >= minimum


def _provenance_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _provenance_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def validate_extraction_provenance(value: Any) -> dict[str, Any]:
    """Validate saved observations, without accessing or inferring source files."""
    try:
        if not _provenance_keys(value, {"schema_version", "source_pdf", "extractor", "max_chars",
                "document_page_count", "visited_page_numbers", "retained_page_numbers", "truncation",
                "document_text_chars", "outputs"}) or value["schema_version"] != EXTRACTION_PROVENANCE_SCHEMA:
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        source, engine, outputs = value["source_pdf"], value["extractor"], value["outputs"]
        if (not _provenance_keys(source, {"sha256", "bytes"}) or not _provenance_hash(source["sha256"])
                or not _provenance_integer(source["bytes"], minimum=1)):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if (not _provenance_keys(engine, {"name", "version", "normalization"})
                or not isinstance(engine["name"], str) or not engine["name"].strip()
                or engine["normalization"] != "XRML_WHITESPACE_V1"
                or (engine["version"] is not None and (not isinstance(engine["version"], str) or not engine["version"].strip()))):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if (not _provenance_keys(outputs, {"text_sha256", "text_bytes", "pages_sha256", "pages_bytes"})
                or any(not _provenance_hash(outputs[key]) for key in ("text_sha256", "pages_sha256"))
                or any(not _provenance_integer(outputs[key], minimum=1) for key in ("text_bytes", "pages_bytes"))
                or not _provenance_integer(value["max_chars"], minimum=1)):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        total, document_chars = value["document_page_count"], value["document_text_chars"]
        visited, retained = value["visited_page_numbers"], value["retained_page_numbers"]
        if total is not None and not _provenance_integer(total, minimum=1):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        for numbers in (visited, retained):
            if (not isinstance(numbers, list) or any(not _provenance_integer(page, minimum=1) for page in numbers)
                    or numbers != sorted(set(numbers)) or (total is not None and any(page > total for page in numbers))):
                raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if not retained or value["truncation"] not in {"TRUNCATED", "NOT_OBSERVED", "UNKNOWN"}:
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if value["truncation"] != "UNKNOWN" and (not visited or not set(retained).issubset(visited)):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if visited and visited != list(range(1, len(visited) + 1)):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if value["truncation"] == "NOT_OBSERVED" and total is not None and len(visited) != total:
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if document_chars is not None and (not _provenance_integer(document_chars, minimum=1)
                or total is None or len(visited) != total or value["truncation"] == "UNKNOWN"):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError(EXTRACTION_PROVENANCE_ERROR) from None


def _file_provenance(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        result[key] = item
    return result


def read_extraction_provenance(text_path: Path, pages_path: Path, pdf_path: Path) -> dict[str, Any] | None:
    """Read a marker only after checking the caller-supplied pair and PDF bytes.

    An absent marker means legacy/unfinished observation, never complete
    extraction. No path from marker content is used for filesystem access.
    """
    marker = text_path.with_suffix(".extraction.json")
    pending = text_path.with_suffix(".extraction.pending.json")
    try:
        if any(is_reparse_point(path) for path in (text_path, pages_path, marker, pending)):
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if not marker.exists():
            if pending.exists():
                raise ValueError(EXTRACTION_PROVENANCE_ERROR)
            return None
        value = validate_extraction_provenance(json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object))
        if pending.exists():
            pending_value = validate_extraction_provenance(json.loads(pending.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object))
            if pending_value != value:
                raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        if _file_provenance(pdf_path) != value["source_pdf"]:
            raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        for name, path in (("text", text_path), ("pages", pages_path)):
            actual = _file_provenance(path)
            if any(actual[key] != value["outputs"][f"{name}_{key}"] for key in ("sha256", "bytes")):
                raise ValueError(EXTRACTION_PROVENANCE_ERROR)
        return value
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ValueError(EXTRACTION_PROVENANCE_ERROR) from None


def _artifact_ref(path: Path | None, *, run_id: str | None = None) -> str | None:
    if path is None:
        return None
    portable = artifact_refs.normalize_path_to_ref(
        path,
        repo_root=constants.PROJECT_ROOT,
        storage_roots=[run_repository.STORAGE_ROOT],
        run_id=run_id,
    )
    if portable and artifact_refs.is_portable_ref(portable):
        return portable
    return None


def _redacted_url(value: str | None) -> str | None:
    return research_transport.redact_retrieval_url(value)


def _normalize_text(value: str) -> str:
    text = str(value or "").replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


def _allowed_domains(values: list[str] | None) -> list[str]:
    source = values or list(DEFAULT_ALLOWED_DOMAINS)
    try:
        return sorted(
            {
                research_transport.normalize_retrieval_hostname(str(value or ""))
                for value in source
                if str(value or "").strip()
            }
        )
    except research_transport.RetrievalTransportError:
        return []


def _domain_allowed(url: str, allowed_domains: list[str] | None) -> bool:
    allowed = _allowed_domains(allowed_domains)
    if not allowed:
        return False
    try:
        policy = research_transport.ResearchTransportPolicy.create(allowed)
        research_transport.validate_retrieval_url(str(url or ""), policy)
    except research_transport.RetrievalTransportError:
        return False
    return True


def evaluate_bounded_pdf_fetch_gate(
    *,
    requested: bool,
    dry_run: bool,
    allow_bounded_pdf_fetch: bool = False,
    env: Mapping[str, str] | None = None,
    download_limit: int = 0,
    allowed_domains: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    reason_codes: list[str] = []
    if not requested:
        reason_codes.append("pdf_fetch_not_requested")
    if dry_run:
        reason_codes.append("dry_run_plan_only")
    if not allow_bounded_pdf_fetch:
        reason_codes.append("operator_flag_required")
    if str((env or os.environ).get(BOUNDED_PDF_FETCH_ENV_VAR, "")).strip() != "1":
        reason_codes.append("env_var_required")
    if int(download_limit or 0) <= 0:
        reason_codes.append("download_limit_required")
    if not _allowed_domains(allowed_domains):
        reason_codes.append("allowed_domain_required")
    if int(max_bytes or 0) <= 0:
        reason_codes.append("max_pdf_bytes_required")
    if int(timeout_seconds or 0) <= 0:
        reason_codes.append("download_timeout_required")

    live_fetch_allowed = (
        bool(requested)
        and not bool(dry_run)
        and bool(allow_bounded_pdf_fetch)
        and str((env or os.environ).get(BOUNDED_PDF_FETCH_ENV_VAR, "")).strip() == "1"
        and int(download_limit or 0) > 0
        and bool(_allowed_domains(allowed_domains))
        and int(max_bytes or 0) > 0
        and int(timeout_seconds or 0) > 0
    )
    return {
        "requested": bool(requested),
        "dry_run": bool(dry_run),
        "live_fetch_allowed": live_fetch_allowed,
        "operator_flag_present": bool(allow_bounded_pdf_fetch),
        "env_var": BOUNDED_PDF_FETCH_ENV_VAR,
        "env_var_present": str((env or os.environ).get(BOUNDED_PDF_FETCH_ENV_VAR, "")).strip() == "1",
        "download_limit": int(download_limit or 0),
        "allowed_domains": _allowed_domains(allowed_domains),
        "max_pdf_bytes": int(max_bytes or 0),
        "download_timeout": int(timeout_seconds or 0),
        "reason_codes": [] if live_fetch_allowed else list(dict.fromkeys(reason_codes)),
    }


def _target_pdf_path(paper_id: str, run_id: str | None) -> Path:
    filename = f"{safe_filename(str(paper_id or '').strip()) or 'paper'}.pdf"
    if run_id:
        run_repository.build_run_paths(str(run_id))
        return resolve_within_root(
            run_repository.STORAGE_ROOT,
            f"{run_id}/raw/{filename}",
        )
    return resolve_within_root(
        constants.PROJECT_ROOT,
        f"data/papers/pdf/{filename}",
    )


def _base_result(
    *,
    status: str,
    reason: str,
    paper_id: str,
    source_url: str | None,
    local_pdf_path: Path | None,
    run_id: str | None = None,
    content_type: str | None = None,
    bytes_written: int = 0,
    sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "paper_id": paper_id,
        "source_url": _redacted_url(source_url),
        "local_pdf_path": _artifact_ref(local_pdf_path, run_id=run_id),
        "bytes_written": bytes_written,
        "sha256": sha256,
        "content_type": content_type,
    }


def _pdf_transport_client(
    allowed_domains: list[str],
    *,
    timeout_seconds: int,
) -> research_transport.ResearchHttpClient:
    policy = research_transport.ResearchTransportPolicy.create(
        allowed_domains,
        total_timeout_seconds=float(timeout_seconds),
    )
    return research_transport.ResearchHttpClient(policy)


def acquire_pdf(
    *,
    paper_id: str,
    pdf_url: str | None,
    topic_id: str,
    run_id: str | None = None,
    allowed_domains: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    del topic_id
    paper_id = str(paper_id or "").strip()
    source_url = str(pdf_url or "").strip()
    try:
        target_path = _target_pdf_path(paper_id, run_id)
    except PathSafetyError:
        return _base_result(
            status="rejected",
            reason="invalid_artifact_path",
            paper_id=paper_id,
            source_url=source_url or None,
            local_pdf_path=None,
            run_id=run_id,
        )
    if not paper_id:
        return _base_result(
            status="rejected",
            reason="missing_paper_id",
            paper_id=paper_id,
            source_url=source_url or None,
            local_pdf_path=target_path,
            run_id=run_id,
        )
    if not source_url:
        return _base_result(
            status="rejected",
            reason="missing_pdf_url",
            paper_id=paper_id,
            source_url=None,
            local_pdf_path=target_path,
            run_id=run_id,
        )
    allowed = _allowed_domains(allowed_domains)
    try:
        if not allowed:
            raise research_transport.RetrievalUrlRejected(
                "retrieval_url_rejected",
                "PDF destination allowlist is invalid.",
            )
        policy = research_transport.ResearchTransportPolicy.create(allowed)
        research_transport.validate_retrieval_url(source_url, policy)
    except research_transport.RetrievalTransportError as exc:
        reason = "non_https_url" if exc.code == "retrieval_https_required" else "domain_not_allowed"
        return _base_result(
            status="rejected",
            reason=reason,
            paper_id=paper_id,
            source_url=source_url,
            local_pdf_path=target_path,
            run_id=run_id,
        )
    if dry_run:
        return _base_result(
            status="planned",
            reason="dry_run",
            paper_id=paper_id,
            source_url=source_url,
            local_pdf_path=target_path,
            run_id=run_id,
        )
    if target_path.exists():
        digest = artifact_refs.compute_sha256(target_path)
        return _base_result(
            status="skipped",
            reason="local_pdf_exists",
            paper_id=paper_id,
            source_url=source_url,
            local_pdf_path=target_path,
            run_id=run_id,
            bytes_written=target_path.stat().st_size,
            sha256=digest,
            content_type="application/pdf",
        )

    try:
        client = _pdf_transport_client(allowed, timeout_seconds=int(timeout_seconds))
        downloaded = client.download_pdf(
            source_url,
            target_path=target_path,
            max_bytes=int(max_bytes),
            expected_sha256=expected_sha256,
        )
    except research_transport.RetrievalTransportError as exc:
        reason = {
            "pdf_download_too_large": "file_too_large",
            "pdf_content_invalid": "not_pdf",
            "pdf_hash_mismatch": "pdf_hash_mismatch",
            "retrieval_url_rejected": "domain_not_allowed",
            "retrieval_https_required": "non_https_url",
            "retrieval_destination_unsafe": "unsafe_destination",
            "retrieval_redirect_rejected": "redirect_rejected",
            "retrieval_redirect_limit": "redirect_rejected",
            "retrieval_timeout": "download_error",
            "retrieval_retry_exhausted": "download_error",
        }.get(exc.code, "download_error")
        status = "rejected" if reason in {
            "file_too_large",
            "not_pdf",
            "pdf_hash_mismatch",
            "domain_not_allowed",
            "non_https_url",
            "unsafe_destination",
            "redirect_rejected",
        } else "failed"
        return {
            **_base_result(
                status=status,
                reason=reason,
                paper_id=paper_id,
                source_url=source_url,
                local_pdf_path=target_path,
                run_id=run_id,
            ),
            "error_code": exc.code,
            "error": exc.public_message,
        }

    return _base_result(
        status="downloaded",
        reason="ok",
        paper_id=paper_id,
        source_url=source_url,
        local_pdf_path=target_path,
        run_id=run_id,
        bytes_written=downloaded.bytes_written,
        sha256=downloaded.sha256,
        content_type=downloaded.content_type,
    )


def _extract_pdf_pages_with_fitz(path: Path, max_text_chars: int = 24000) -> list[dict[str, Any]]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise RuntimeError("pymupdf_unavailable") from exc

    pages = _ExtractedPages()
    max_chars = max(1, int(max_text_chars or 24000))
    used_chars = 0
    visited: list[int] = []
    complete_parts: list[str] = []
    truncated = False
    with fitz.open(str(path)) as doc:
        total_pages = doc.page_count
        for index in range(doc.page_count):
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            text = _normalize_text(doc.load_page(index).get_text("text") or "")
            visited.append(index + 1)
            if not text:
                continue
            complete_parts.append(text)
            used = text[:remaining]
            truncated = truncated or len(used) < len(text)
            pages.append({"page_number": index + 1, "text": used, "extraction_method": "pymupdf"})
            used_chars += len(used) + 1
    pages.observation = {
        "extractor": {"name": "pymupdf", "version": str(getattr(fitz, "VersionBind", "")) or None,
                      "normalization": "XRML_WHITESPACE_V1"},
        "document_page_count": total_pages, "visited_page_numbers": visited,
        "truncation": "TRUNCATED" if truncated or len(visited) < total_pages else "NOT_OBSERVED",
        "document_text_chars": len(" ".join(complete_parts)) if len(visited) == total_pages else None,
    }
    return pages


def _extract_pdf_pages_with_pdftotext(path: Path, max_text_chars: int = 24000) -> list[dict[str, Any]]:
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError("pdftotext_unavailable")
    try:
        completed = subprocess.run(
            [executable, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("pdftotext_failed") from exc
    if completed.returncode != 0:
        raise RuntimeError("pdftotext_failed")

    pages = _ExtractedPages()
    max_chars = max(1, int(max_text_chars or 24000))
    used_chars = 0
    segments = str(completed.stdout or "").split("\f")
    if len(segments) > 1 and not segments[-1].strip():
        segments.pop()  # Terminal form feed is not another observed PDF page.
    visited: list[int] = []
    truncated = False
    for index, page_text in enumerate(segments, start=1):
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        text = _normalize_text(page_text)
        visited.append(index)
        if not text:
            continue
        used = text[:remaining]
        truncated = truncated or len(used) < len(text)
        pages.append({"page_number": index, "text": used, "extraction_method": "pdftotext"})
        used_chars += len(used) + 1
    if not pages:
        raise RuntimeError("pdftotext_no_text")
    pages.observation = {
        "extractor": {"name": "pdftotext", "version": None, "normalization": "XRML_WHITESPACE_V1"},
        "document_page_count": None, "visited_page_numbers": visited,
        "truncation": "TRUNCATED" if truncated or len(visited) < len(segments) else "NOT_OBSERVED",
        # Output segments do not establish the physical PDF page/text totals.
        "document_text_chars": None,
    }
    return pages


def _extract_pdf_pages(path: Path, max_text_chars: int = 24000) -> list[dict[str, Any]]:
    errors: list[str] = []
    for extractor in (_extract_pdf_pages_with_fitz, _extract_pdf_pages_with_pdftotext):
        try:
            return extractor(path, max_text_chars=max_text_chars)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors) or "pdf_extract_unavailable")


def _extract_observed_pdf(path: Path, max_text_chars: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Give either engine an app-owned snapshot of the exact hashed PDF bytes."""
    # Use the configured temp root, outside article inventories. In disposable
    # development this is explicitly a D: task directory.
    with tempfile.TemporaryDirectory(prefix="xrml-extract-") as directory:
        temporary = Path(directory) / "input.pdf"
        digest = hashlib.sha256()
        size = 0
        with temporary.open("wb") as target:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        pages = _extract_pdf_pages(temporary, max_text_chars=max_text_chars)
        return pages, {"sha256": digest.hexdigest(), "bytes": size}


def _json_file_bytes(payload: dict[str, Any]) -> bytes:
    # Match the existing write_json -> text-mode write_text bytes on this OS.
    return json.dumps(payload, ensure_ascii=False, indent=2).replace("\n", os.linesep).encode("utf-8")


def _publish_extraction(
    *, source_path: Path, source: dict[str, Any], text_path: Path, pages_path: Path,
    text: str, pages_payload: dict[str, Any], manifest: dict[str, Any],
) -> str | None:
    marker = text_path.with_suffix(".extraction.json")
    pending = text_path.with_suffix(".extraction.pending.json")
    marker_bytes = _json_file_bytes(manifest)
    expected = {text_path: text.encode("utf-8"), pages_path: _json_file_bytes(pages_payload), marker: marker_bytes, pending: marker_bytes}
    lock_path = marker.with_suffix(marker.suffix + ".lock")
    guarded = (*expected, lock_path)
    try:
        if any(is_reparse_point(path) for path in guarded):
            return "extraction_artifact_conflict"
        with file_lock(lock_path):
            if any(is_reparse_point(path) for path in guarded):
                return "extraction_artifact_conflict"
            # Check every pre-existing output before changing any output.
            if any(path.exists() and (not path.is_file() or path.read_bytes() != content) for path, content in expected.items()):
                return "extraction_artifact_conflict"
            if _file_provenance(source_path) != source:
                return "source_pdf_changed"
            # A crash between output writes remains distinguishable from a
            # legacy artifact without provenance. Identical retries resume it.
            if not marker.exists() and not pending.exists():
                write_json(pending, manifest)
            if not text_path.exists():
                write_text(text_path, text)
            if not pages_path.exists():
                write_json(pages_path, pages_payload)
            if any(path.read_bytes() != expected[path] for path in (text_path, pages_path)):
                return "extraction_artifact_write_failed"
            if _file_provenance(source_path) != source:
                return "source_pdf_changed"
            # Individual files are atomic replacements; only a valid last
            # marker means the pair was observed complete. This is not a
            # multi-file transaction or a promise of power-loss durability.
            if not marker.exists():
                write_json(marker, manifest)
            read_extraction_provenance(text_path, pages_path, source_path)
            if pending.exists():
                pending.unlink()
        return None
    except (OSError, FileLockError):
        return "extraction_artifact_write_failed"
    except ValueError:
        return EXTRACTION_PROVENANCE_ERROR


def _quality_summary(
    *,
    joined_text: str,
    pages: list[dict[str, Any]],
    extraction_method: str,
    min_text_chars: int = MIN_EXTRACTED_TEXT_CHARS,
) -> dict[str, Any]:
    evidence_lengths = [min(360, len(str(page.get("text") or "").strip())) for page in pages if str(page.get("text") or "").strip()]
    evidence_span_count = len(evidence_lengths)
    average_length = round(sum(evidence_lengths) / evidence_span_count, 1) if evidence_span_count else 0.0
    text_sha256 = hashlib.sha256(joined_text.encode("utf-8", errors="ignore")).hexdigest() if joined_text else None
    reason = "ok"
    status = "ok"
    if not joined_text:
        status = "failed"
        reason = "no_extractable_text"
    elif len(joined_text) < max(1, int(min_text_chars)):
        status = "failed"
        reason = "weak_extracted_text"
    elif evidence_span_count == 0:
        status = "failed"
        reason = "no_evidence_span"
    elif average_length < MIN_AVERAGE_EVIDENCE_SPAN_CHARS:
        status = "failed"
        reason = "weak_evidence_span"
    return {
        "status": status,
        "reason": reason,
        "character_count": len(joined_text),
        "page_count": len(pages),
        "non_empty_page_count": sum(1 for page in pages if str(page.get("text") or "").strip()),
        "text_sha256": text_sha256,
        "extraction_method": extraction_method,
        "evidence_span_count": evidence_span_count,
        "average_evidence_span_length": average_length,
        "min_text_chars": max(1, int(min_text_chars)),
        "min_average_evidence_span_chars": MIN_AVERAGE_EVIDENCE_SPAN_CHARS,
    }


def extract_pdf_to_text_artifacts(
    *,
    pdf_path: Path | str,
    paper_id: str,
    max_text_chars: int = 24000,
    output_dir: Path | None = None,
    expected_text_sha256: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    path = Path(pdf_path)
    pdf_ref = _artifact_ref(path, run_id=run_id)
    if not path.is_file():
        return {
            "status": "failed",
            "reason": "missing_pdf",
            "pdf_path": pdf_ref,
            "text_path": None,
            "pages_path": None,
            "text_char_count": 0,
            "page_count": 0,
            "quality": _quality_summary(joined_text="", pages=[], extraction_method="unavailable"),
        }
    try:
        pages, source_pdf = _extract_observed_pdf(path, max_text_chars)
    except Exception:
        return {
            "status": "failed",
            "reason": "extract_error",
            "pdf_path": pdf_ref,
            "text_path": None,
            "pages_path": None,
            "text_char_count": 0,
            "page_count": 0,
            "error": "PDF text extraction failed safely.",
            "quality": _quality_summary(joined_text="", pages=[], extraction_method="unavailable"),
        }

    normalized_pages: list[dict[str, Any]] = []
    parts: list[str] = []
    used_chars = 0
    max_chars = max(1, int(max_text_chars or 24000))
    for index, page in enumerate(pages, start=1):
        text = _normalize_text(str(page.get("text") or ""))
        if not text:
            continue
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        used = text[:remaining]
        page_number = int(page.get("page_number") or page.get("page") or index)
        extraction_method = str(page.get("extraction_method") or "").strip() or "mock"
        normalized_pages.append({"page": page_number, "text": used, "extraction_method": extraction_method})
        parts.append(used)
        used_chars += len(used) + 1
    joined = _normalize_text(" ".join(parts))
    extraction_method = str(normalized_pages[0].get("extraction_method") or "unknown") if normalized_pages else "unknown"
    quality = _quality_summary(joined_text=joined, pages=normalized_pages, extraction_method=extraction_method)
    if not joined:
        return {
            "status": "failed",
            "reason": "no_extractable_text",
            "pdf_path": pdf_ref,
            "text_path": None,
            "pages_path": None,
            "text_char_count": 0,
            "page_count": 0,
            "quality": quality,
        }
    if quality["status"] != "ok":
        return {
            "status": "failed",
            "reason": quality["reason"],
            "pdf_path": pdf_ref,
            "text_path": None,
            "pages_path": None,
            "text_char_count": len(joined),
            "page_count": len(normalized_pages),
            "quality": quality,
        }
    text_sha256 = hashlib.sha256(joined.encode("utf-8", errors="ignore")).hexdigest()
    expected_hash = str(expected_text_sha256 or "").strip().lower()
    if expected_hash and text_sha256.lower() != expected_hash:
        return {
            "status": "failed",
            "reason": "source_text_hash_mismatch",
            "pdf_path": pdf_ref,
            "text_path": None,
            "pages_path": None,
            "text_char_count": len(joined),
            "page_count": len(normalized_pages),
            "expected_source_text_sha256": expected_hash,
            "actual_source_text_sha256": text_sha256,
            "quality": quality,
        }

    target_dir = output_dir or path.parent
    ensure_dir(target_dir)
    base = safe_filename(str(paper_id or path.stem)) or path.stem
    text_path = target_dir / f"{base}.txt"
    pages_path = target_dir / f"{base}.pages.json"
    pages_payload = {"paper_id": paper_id, "source_pdf_path": pdf_ref, "pages": normalized_pages}
    text_bytes, pages_bytes = joined.encode("utf-8"), _json_file_bytes(pages_payload)
    observation = getattr(pages, "observation", {}) or {
        "extractor": {"name": extraction_method, "version": None, "normalization": "XRML_WHITESPACE_V1"},
        "document_page_count": None, "visited_page_numbers": [], "truncation": "UNKNOWN", "document_text_chars": None,
    }
    manifest = validate_extraction_provenance({
        "schema_version": EXTRACTION_PROVENANCE_SCHEMA, "source_pdf": source_pdf,
        **observation, "max_chars": max_chars,
        "retained_page_numbers": [page["page"] for page in normalized_pages],
        "outputs": {"text_sha256": hashlib.sha256(text_bytes).hexdigest(), "text_bytes": len(text_bytes),
                    "pages_sha256": hashlib.sha256(pages_bytes).hexdigest(), "pages_bytes": len(pages_bytes)},
    })
    publish_error = _publish_extraction(
        source_path=path, source=source_pdf, text_path=text_path, pages_path=pages_path,
        text=joined, pages_payload=pages_payload, manifest=manifest,
    )
    if publish_error:
        return {"status": "conflict" if publish_error == "extraction_artifact_conflict" else "failed",
                "reason": publish_error, "pdf_path": pdf_ref, "text_path": None, "pages_path": None,
                "text_char_count": len(joined), "page_count": len(normalized_pages), "quality": quality}
    return {
        "status": "extracted",
        "reason": "ok",
        "pdf_path": pdf_ref,
        "text_path": _artifact_ref(text_path, run_id=run_id),
        "pages_path": _artifact_ref(pages_path, run_id=run_id),
        "text_char_count": len(joined),
        "page_count": len(normalized_pages),
        "quality": quality,
        "extraction_provenance": manifest,
    }


def _candidate_value(candidate: dict[str, Any], *keys: str) -> str:
    item = candidate.get("_item") if isinstance(candidate.get("_item"), dict) else {}
    run = candidate.get("_run") if isinstance(candidate.get("_run"), dict) else {}
    for key in keys:
        for source in (candidate, item, run):
            value = source.get(key) if isinstance(source, dict) else None
            text = str(value or "").strip()
            if text:
                return text
    return ""


def acquire_candidate_pdfs(
    candidates: list[dict[str, Any]],
    *,
    topic_id: str,
    provider: str = "arxiv",
    allowed_domains: list[str] | None = None,
    download_limit: int = 0,
    dry_run: bool = False,
    extract_after_fetch: bool = False,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_text_chars: int = 24000,
    allow_network_fetch: bool = True,
    expected_pdf_sha256_by_paper_id: dict[str, str] | None = None,
    expected_source_text_sha256_by_paper_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    allowed = _allowed_domains(allowed_domains)
    expected_provider = str(provider or "").strip().lower()
    max_downloads = max(0, int(download_limit or 0))
    items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    downloaded_count = 0
    failed_count = 0
    extracted_count = 0
    extraction_failed_count = 0
    extracted_text_count = 0
    extracted_page_count = 0
    extracted_paper_ids: list[str] = []
    rejected_by_reason: dict[str, int] = {}
    failed_by_reason: dict[str, int] = {}
    extraction_failed_by_reason: dict[str, int] = {}
    skipped_existing_artifact_count = 0
    expected_pdf_sha256_by_paper_id = {
        str(key or "").strip().lower(): str(value or "").strip().lower()
        for key, value in (expected_pdf_sha256_by_paper_id or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    expected_source_text_sha256_by_paper_id = {
        str(key or "").strip().lower(): str(value or "").strip().lower()
        for key, value in (expected_source_text_sha256_by_paper_id or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }

    def count_reason(target: dict[str, int], reason: str | None) -> None:
        key = str(reason or "unknown")
        target[key] = target.get(key, 0) + 1

    def build_audit_summary(
        *,
        planned_count: int = 0,
        rejected_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "candidate_count": len(candidates),
            "missing_pdf_url_count": rejected_by_reason.get("missing_pdf_url", 0),
            "planned_count": planned_count,
            "downloaded_count": downloaded_count,
            "rejected_count": rejected_count,
            "rejected_by_reason": dict(sorted(rejected_by_reason.items())),
            "failed_count": failed_count,
            "failed_by_reason": dict(sorted(failed_by_reason.items())),
            "extracted_count": extracted_count,
            "extraction_failed_count": extraction_failed_count,
            "extraction_failed_by_reason": dict(sorted(extraction_failed_by_reason.items())),
            "full_text_available_count": 0,
            "full_text_reviewed_count": 0,
            "reviewed_from_pdf_extract_count": 0,
            "skipped_already_reviewed_count": 0,
            "skipped_existing_artifact_count": skipped_existing_artifact_count,
            "no_evidence_span_count": 0,
            "weak_evidence_span_count": 0,
            "high_failure_rate_warning": None,
        }

    def expected_hash(mapping: dict[str, str], *, paper_id: str, candidate: dict[str, Any]) -> str:
        keys = [
            paper_id,
            str(candidate.get("normalized_paper_id") or ""),
            _candidate_value(candidate, "paper_id"),
            _candidate_value(candidate, "entry_id"),
        ]
        for key in keys:
            lookup = str(key or "").strip().lower()
            if lookup and lookup in mapping:
                return mapping[lookup]
        return ""

    if max_downloads <= 0:
        rejected_by_reason["download_limit_required"] = len(candidates)
        return {
            "mode": "plan_only" if dry_run else "download",
            "requested": True,
            "provider": expected_provider or None,
            "allow_domains": allowed,
            "download_limit": max_downloads,
            "extract_after_fetch": bool(extract_after_fetch),
            "planned_count": 0,
            "downloaded_count": 0,
            "extracted_count": 0,
            "rejected_count": len(candidates),
            "failed_count": 0,
            "extraction_failed_count": 0,
            "extracted_text_count": 0,
            "extracted_page_count": 0,
            "items": [],
            "rejected": [
                {
                    "paper_id": _candidate_value(candidate, "paper_id", "entry_id", "pdf_url", "title"),
                    "source_url": _redacted_url(_candidate_value(candidate, "pdf_url") or None),
                    "reason": "download_limit_required",
                }
                for candidate in candidates
            ],
            "extracted_normalized_paper_ids": [],
            "audit_summary": build_audit_summary(planned_count=0, rejected_count=len(candidates)),
            "note": "No PDFs were planned or downloaded because download_limit was not greater than zero.",
        }

    for candidate in candidates:
        paper_id = _candidate_value(candidate, "paper_id", "entry_id", "pdf_url", "title")
        run_id = _candidate_value(candidate, "best_run_id", "run_id")
        source_provider = _candidate_value(candidate, "source_provider", "provider", "source").lower() or "arxiv"
        pdf_url = _candidate_value(candidate, "pdf_url")

        if expected_provider and source_provider and source_provider != expected_provider:
            rejected.append(
                {
                    "paper_id": paper_id,
                    "source_url": _redacted_url(pdf_url or None),
                    "reason": "provider_mismatch",
                }
            )
            count_reason(rejected_by_reason, "provider_mismatch")
            continue
        if not pdf_url:
            rejected.append({"paper_id": paper_id, "source_url": None, "reason": "missing_pdf_url"})
            count_reason(rejected_by_reason, "missing_pdf_url")
            continue
        artifacts = legacy_reviews.resolve_paper_artifacts(run_id, paper_id) if run_id else {}
        existing_text = artifacts.get("text_txt") or artifacts.get("pages_json")
        if existing_text and Path(existing_text).exists():
            skipped_existing_artifact_count += 1
            items.append(
                {
                    "paper_id": paper_id,
                    "normalized_paper_id": str(candidate.get("normalized_paper_id") or ""),
                    "title": str(candidate.get("title") or "").strip(),
                    "source_url": _redacted_url(pdf_url),
                    "local_pdf_path": _artifact_ref(Path(str(artifacts.get("pdf"))), run_id=run_id)
                    if artifacts.get("pdf")
                    else None,
                    "status": "skipped",
                    "reason": "local_text_exists",
                    "text_artifact_path": _artifact_ref(Path(existing_text), run_id=run_id),
                    "provider": expected_provider or source_provider,
                    "extract_after_fetch": bool(extract_after_fetch),
                }
            )
            continue
        existing_pdf = artifacts.get("pdf")
        if existing_pdf and Path(existing_pdf).exists():
            expected_pdf_hash = expected_hash(expected_pdf_sha256_by_paper_id, paper_id=paper_id, candidate=candidate)
            existing_pdf_hash = artifact_refs.compute_sha256(Path(existing_pdf))
            if expected_pdf_hash and existing_pdf_hash.lower() != expected_pdf_hash:
                rejected.append(
                    {
                        "paper_id": paper_id,
                        "source_url": _redacted_url(pdf_url),
                        "local_pdf_path": _artifact_ref(Path(existing_pdf), run_id=run_id),
                        "reason": "pdf_hash_mismatch",
                        "expected_pdf_sha256": expected_pdf_hash,
                        "actual_pdf_sha256": existing_pdf_hash,
                    }
                )
                count_reason(rejected_by_reason, "pdf_hash_mismatch")
                continue
            skipped_existing_artifact_count += 1
            existing_item = {
                "paper_id": paper_id,
                "normalized_paper_id": str(candidate.get("normalized_paper_id") or ""),
                "title": str(candidate.get("title") or "").strip(),
                "source_url": _redacted_url(pdf_url),
                "local_pdf_path": _artifact_ref(Path(existing_pdf), run_id=run_id),
                "status": "skipped",
                "reason": "local_pdf_exists",
                "provider": expected_provider or source_provider,
                "extract_after_fetch": bool(extract_after_fetch),
                "dry_run": bool(dry_run),
            }
            if extract_after_fetch and not dry_run:
                extraction = extract_pdf_to_text_artifacts(
                    pdf_path=Path(existing_pdf),
                    paper_id=paper_id,
                    max_text_chars=max_text_chars,
                    run_id=run_id,
                    expected_text_sha256=expected_hash(
                        expected_source_text_sha256_by_paper_id,
                        paper_id=paper_id,
                        candidate=candidate,
                    ),
                )
                existing_item["extraction"] = extraction
                if extraction["status"] == "extracted":
                    extracted_count += 1
                    extracted_text_count += 1
                    extracted_page_count += int(extraction.get("page_count") or 0)
                    extracted_paper_ids.append(str(candidate.get("normalized_paper_id") or paper_id))
                else:
                    extraction_failed_count += 1
                    count_reason(extraction_failed_by_reason, extraction.get("reason"))
            items.append(existing_item)
            continue
        if len([item for item in items if item.get("status") in {"planned", "downloaded"}]) >= max_downloads:
            continue

        expected_pdf_hash = expected_hash(
            expected_pdf_sha256_by_paper_id,
            paper_id=paper_id,
            candidate=candidate,
        )
        acquired = acquire_pdf(
            paper_id=paper_id,
            pdf_url=pdf_url,
            topic_id=topic_id,
            run_id=run_id,
            allowed_domains=allowed,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            dry_run=bool(dry_run or not allow_network_fetch),
            expected_sha256=expected_pdf_hash,
        )
        acquired.update(
            {
                "normalized_paper_id": str(candidate.get("normalized_paper_id") or ""),
                "title": str(candidate.get("title") or "").strip(),
                "provider": expected_provider or source_provider,
                "extract_after_fetch": bool(extract_after_fetch),
            }
        )
        if acquired["status"] == "planned":
            items.append(acquired)
            continue
        if acquired["status"] == "downloaded":
            actual_pdf_hash = str(acquired.get("sha256") or "").strip().lower()
            if expected_pdf_hash and actual_pdf_hash != expected_pdf_hash:
                rejected_item = {
                    **acquired,
                    "status": "rejected",
                    "reason": "pdf_hash_mismatch",
                    "expected_pdf_sha256": expected_pdf_hash,
                    "actual_pdf_sha256": actual_pdf_hash,
                }
                rejected.append(rejected_item)
                count_reason(rejected_by_reason, "pdf_hash_mismatch")
                continue
            downloaded_count += 1
            if extract_after_fetch:
                extraction = extract_pdf_to_text_artifacts(
                    pdf_path=_target_pdf_path(paper_id, run_id),
                    paper_id=paper_id,
                    max_text_chars=max_text_chars,
                    run_id=run_id,
                    expected_text_sha256=expected_hash(
                        expected_source_text_sha256_by_paper_id,
                        paper_id=paper_id,
                        candidate=candidate,
                    ),
                )
                acquired["extraction"] = extraction
                if extraction["status"] == "extracted":
                    extracted_count += 1
                    extracted_text_count += 1
                    extracted_page_count += int(extraction.get("page_count") or 0)
                    extracted_paper_ids.append(str(candidate.get("normalized_paper_id") or paper_id))
                else:
                    extraction_failed_count += 1
                    count_reason(extraction_failed_by_reason, extraction.get("reason"))
            items.append(acquired)
            continue
        if acquired["status"] == "failed":
            failed_count += 1
            count_reason(failed_by_reason, acquired.get("reason"))
        if acquired["status"] == "rejected":
            rejected.append(acquired)
            count_reason(rejected_by_reason, acquired.get("reason"))
        else:
            items.append(acquired)

    planned_count = sum(1 for item in items if item.get("status") == "planned")
    return {
        "mode": "plan_only" if dry_run else "download",
        "requested": True,
        "provider": expected_provider or None,
        "allow_domains": allowed,
        "download_limit": max_downloads,
        "extract_after_fetch": bool(extract_after_fetch),
        "planned_count": planned_count,
        "downloaded_count": downloaded_count,
        "extracted_count": extracted_count,
        "rejected_count": len(rejected),
        "failed_count": failed_count,
        "extraction_failed_count": extraction_failed_count,
        "extracted_text_count": extracted_text_count,
        "extracted_page_count": extracted_page_count,
        "items": items,
        "rejected": rejected,
        "extracted_normalized_paper_ids": extracted_paper_ids,
        "audit_summary": build_audit_summary(planned_count=planned_count, rejected_count=len(rejected)),
        "note": "Dry-run only; no network download or file writes were performed."
        if dry_run
        else "Live PDF fetch was blocked by bounded-fetch gates; local artifact reuse/extraction remained available."
        if not allow_network_fetch
        else "Explicit opt-in PDF acquisition was executed with domain, count, size, and content validation.",
    }

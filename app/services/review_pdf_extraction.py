"""Explicit local PDF extraction. No acquisition, model call or review mutation."""
import hashlib
import json
from pathlib import Path
import stat
from typing import Any

from app.auth.resource_access import ResourceActor
from app.db import bootstrap
from app.domain import review_pdf_extraction as domain, review_preparation as preparation
from app.domain.path_safety import is_reparse_point, validate_run_id
from app.repositories import review_pdf_extractions as repository
from app.repositories.files import iso_now_utc, safe_filename
from app.services import paper_bank, pdf_acquisition, review_input_history


def _occurrences(topic: str, version: str, actor: ResourceActor) -> list[tuple[str, str]]:
    try:
        return review_input_history.visible_occurrences(topic, version, actor)
    except (FileNotFoundError, paper_bank.PaperVersionNotFound):
        raise KeyError("review_pdf_extraction_not_found") from None


def _guard(path: Path, maximum: int) -> tuple[int, int, int, int, int]:
    if any(is_reparse_point(part) for part in (*reversed(path.parents), path)):
        raise ValueError("review_pdf_source_invalid")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise KeyError("review_pdf_source_missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size < 1:
        raise ValueError("review_pdf_source_invalid")
    if info.st_size > maximum:
        raise ValueError("review_pdf_source_too_large")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino


def _pdf(run: str, paper: str) -> tuple[Path, dict[str, Any]]:
    validate_run_id(run)
    basename = safe_filename(paper)
    if not basename or basename in {".", ".."}:
        raise ValueError("review_pdf_source_invalid")
    reference = "run:raw/" + basename + ".pdf"
    path = bootstrap.STORAGE_ROOT / run / reference[4:]
    before = _guard(path, domain.MAX_PDF_BYTES)
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as reader:
        header = reader.read(5)
        if header != b"%PDF-":
            raise ValueError("review_pdf_source_invalid")
        digest.update(header)
        size = len(header)
        while chunk := reader.read(1024 * 1024):
            size += len(chunk)
            if size > domain.MAX_PDF_BYTES:
                raise ValueError("review_pdf_source_too_large")
            digest.update(chunk)
    if _guard(path, domain.MAX_PDF_BYTES) != before or size != before[0]:
        raise ValueError("review_pdf_source_changed")
    return path, dict(sha256=digest.hexdigest(), bytes=size, artifact_ref=reference)


def _output_directory(run: str, extraction_id: str) -> Path:
    root = bootstrap.STORAGE_ROOT / run
    directory = root / "extractions" / extraction_id
    if any(is_reparse_point(part) for part in (*reversed(directory.parents), directory)):
        raise ValueError("review_pdf_source_invalid")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _preflight_pdf(path: Path) -> None:
    # This new interactive operation requires a physical-page bound. The old
    # extraction helper and its independently observed fallback stay unchanged.
    try:
        import fitz  # type: ignore
    except ImportError:
        raise ValueError("review_pdf_engine_unavailable") from None
    try:
        with fitz.open(str(path)) as document:
            if getattr(document, "needs_pass", False):
                raise ValueError("review_pdf_encrypted")
            total = document.page_count
            if type(total) is not int or total < 1:
                raise ValueError("review_pdf_source_invalid")
            if total > domain.MAX_DOCUMENT_PAGES:
                raise ValueError("review_pdf_page_limit_exceeded")
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) in {
            "review_pdf_encrypted", "review_pdf_source_invalid", "review_pdf_page_limit_exceeded",
        }:
            raise
        raise ValueError("review_pdf_extraction_failed") from None


def _has_primary_pdf_binding(topic: str, run: str, paper: str) -> bool:
    """A historical explicit PDF link still matters when an attachment exists."""
    from app.repositories import artifact_refs, paper_reviews

    review = paper_reviews.get_review(topic, run, paper, read_only=True)
    if review is None or not review.get("full_text_artifact_path"):
        return False
    observed = artifact_refs.resolve_artifact_reference(str(review["full_text_artifact_path"]), run_id=run)
    return observed.is_safe and observed.portable_ref == "run:raw/" + safe_filename(paper) + ".pdf"


def inventory(topic: str, version: str, actor: ResourceActor) -> dict[str, Any]:
    from app.repositories import review_pdf_attachments
    from app.services import review_pdf_attachment
    try:
        _, paper_item = review_input_history.visible_paper(topic, version, actor)
    except (FileNotFoundError, paper_bank.PaperVersionNotFound):
        raise KeyError("review_pdf_extraction_not_found") from None
    occurrences = list(paper_item["_reviewOccurrenceKeys"])
    canonical = {(row["runId"], row["paperId"]) for row in paper_item["compatibility"]["occurrences"]}
    records = repository.listing(topic, version, occurrences)
    attachments = review_pdf_attachments.listing(topic, version, occurrences)
    attached = {(row["run_id"], row["paper_id"]) for row in attachments}
    retained_primary = {(row["source"]["run_id"], row["source"]["paper_id"])
                        for row in records if row["schema_version"] == domain.SCHEMA}
    pdfs, unavailable = [], []
    for run, paper in sorted(set(occurrences)):
        try:
            _, observed = _pdf(run, paper)
        except (KeyError, ValueError) as exc:
            reason = str(exc.args[0])
            if reason not in {"review_pdf_source_missing", "review_pdf_source_invalid", "review_pdf_source_too_large", "review_pdf_source_changed"}:
                raise
            # Lookup aliases are not independent collected PDF observations.
            # Do still discover an actual legacy PDF under any visible alias,
            # and preserve every invalid/changed source and explicit old link.
            # A content-addressed attachment does not promise a second raw PDF.
            key = (run, paper)
            if (reason == "review_pdf_source_missing" and key not in retained_primary
                    and not (key in canonical and key not in attached)
                    and not _has_primary_pdf_binding(topic, run, paper)):
                continue
            unavailable.append(dict(diagnostic_id=domain.sha(domain.encoded([run, paper])), run_id=run, reason=reason))
        else:
            pdfs.append(dict(run_id=run, paper_id=paper, pdf_sha256=observed["sha256"], pdf_bytes=observed["bytes"]))
    for attachment in attachments:
        try:
            review_pdf_attachment.observe(attachment)
        except (KeyError, ValueError) as exc:
            unavailable.append(dict(diagnostic_id=attachment["attachment_id"], run_id=attachment["run_id"], reason=str(exc.args[0])))
        else:
            pdfs.append({key: attachment[key] for key in ("run_id", "paper_id", "pdf_sha256", "pdf_bytes", "attachment_id")})
    return dict(schema_version="PAPER_PDF_EXTRACTION_INVENTORY_V1", pdfs=pdfs, extractions=records,
        unavailable=unavailable, limits=dict(max_pdf_bytes=domain.MAX_PDF_BYTES,
            max_source_chars=preparation.MAX_SOURCE_CHARS, max_artifact_bytes=preparation.MAX_ARTIFACT_BYTES,
            max_document_pages=domain.MAX_DOCUMENT_PAGES), state="NOT_EXECUTED")


def detail(topic: str, version: str, extraction_id: str, actor: ResourceActor) -> dict[str, Any]:
    return repository.detail(topic, version, extraction_id, _occurrences(topic, version, actor))


def _page_content(raw: bytes, pdf_reference: str) -> tuple[str, list[dict[str, int]]]:
    """Interpret the existing extractor artifact without inventing page bounds."""
    from app.services import review_preparation

    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"),
                             object_pairs_hook=review_preparation._unique_pairs,
                             parse_constant=review_preparation._reject_constant)
        if (not isinstance(payload, dict) or set(payload) != {"paper_id", "source_pdf_path", "pages"}
                or payload["paper_id"] != "document" or payload["source_pdf_path"] != pdf_reference
                or not isinstance(payload["pages"], list) or not 0 < len(payload["pages"]) <= domain.MAX_DOCUMENT_PAGES):
            raise ValueError()
        cursor, previous_page, ranges, parts = 0, 0, [], []
        for page in payload["pages"]:
            if (not isinstance(page, dict) or set(page) != {"page", "text", "extraction_method"}
                    or type(page["page"]) is not int or not previous_page < page["page"] <= domain.MAX_DOCUMENT_PAGES
                    or not isinstance(page["text"], str) or not isinstance(page["extraction_method"], str)
                    or not page["extraction_method"].strip()):
                raise ValueError()
            text = pdf_acquisition._normalize_text(page["text"])
            if not text:
                raise ValueError()
            text.encode("utf-8", errors="strict")
            ranges.append(dict(page_number=page["page"], start=cursor, end=cursor + len(text), chars=len(text)))
            parts.append(text)
            cursor += len(text) + 1
            previous_page = page["page"]
        return " ".join(parts), ranges
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError(domain.ERROR) from None


def read_text(record: dict[str, Any]) -> str:
    """Reopen the exact saved text/page pair without requiring the original PDF."""
    from app.services import review_preparation

    record = domain.validate(record)
    source = record["source"]
    path, _ = review_preparation._source_path(source["artifact_ref"], source["run_id"])
    before = review_preparation._guard(path)
    with path.open("rb") as reader:
        raw = reader.read(preparation.MAX_ARTIFACT_BYTES + 1)
    if (len(raw) != source["artifact_bytes"] or hashlib.sha256(raw).hexdigest() != source["artifact_sha256"]
            or review_preparation._guard(path) != before):
        raise ValueError("review_preparation_source_changed")
    try:
        text = raw.decode("utf-8", errors="strict")
        if pdf_acquisition._normalize_text(text) != text or len(text) != source["source_chars"]:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ValueError("review_preparation_source_invalid") from None
    # Page coverage is meaningful only when the recorded bounds also match the
    # preserved page bodies. A rehashed, internally consistent DB row alone is
    # not proof of where a physical page starts in the frozen text.
    reference = f"run:extractions/{record['extraction_id']}/document.pages.json"
    try:
        pages_path, _ = review_preparation._source_path(reference, source["run_id"])
        before_pages = review_preparation._guard(pages_path)
        with pages_path.open("rb") as reader:
            raw_pages = reader.read(preparation.MAX_ARTIFACT_BYTES + 1)
        outputs = record["provenance"]["outputs"]
        if (len(raw_pages) != outputs["pages_bytes"] or hashlib.sha256(raw_pages).hexdigest() != outputs["pages_sha256"]
                or review_preparation._guard(pages_path) != before_pages):
            raise ValueError()
    except FileNotFoundError:
        raise KeyError("review_preparation_not_found") from None
    except ValueError:
        raise ValueError(domain.ERROR) from None
    page_text, ranges = _page_content(raw_pages, record["pdf"]["artifact_ref"])
    if page_text != text or ranges != record["page_ranges"]:
        raise ValueError(domain.ERROR)
    return text


def extract(topic: str, version: str, body: domain.Extract, actor: ResourceActor) -> dict[str, Any]:
    body = domain.Extract.model_validate(body.model_dump(), strict=True)
    occurrences = _occurrences(topic, version, actor)
    if (body.run_id, body.paper_id) not in occurrences:
        raise KeyError("review_pdf_extraction_not_found")
    identity = domain.identity(topic, version, body)
    try:
        old = repository.detail(topic, version, identity, occurrences)
    except KeyError:
        old = None
    if old is not None:
        return dict(extraction=old, created=False)
    if body.attachment_id:
        from app.repositories import review_pdf_attachments
        from app.services import review_pdf_attachment
        attachment = review_pdf_attachments.detail(topic, version, body.attachment_id, occurrences)
        if (attachment["run_id"], attachment["paper_id"]) != (body.run_id, body.paper_id):
            raise KeyError("review_pdf_extraction_not_found")
        path, pdf = review_pdf_attachment.observe(attachment)
    else:
        path, pdf = _pdf(body.run_id, body.paper_id)
    if pdf["sha256"] != body.expected_pdf_sha256:
        raise ValueError("review_pdf_source_changed")
    _preflight_pdf(path)
    directory = _output_directory(body.run_id, identity)
    result = pdf_acquisition.extract_pdf_to_text_artifacts(pdf_path=path, paper_id="document",
        max_text_chars=preparation.MAX_SOURCE_CHARS, output_dir=directory, run_id=body.run_id)
    if result["status"] != "extracted":
        if result["reason"] == "source_pdf_changed":
            raise ValueError("review_pdf_source_changed")
        raise ValueError("review_pdf_extraction_conflict" if result["status"] == "conflict" else "review_pdf_extraction_failed")
    provenance = result["extraction_provenance"]
    if provenance["source_pdf"] != {key: pdf[key] for key in ("sha256", "bytes")}:
        raise ValueError("review_pdf_source_changed")
    text_path, pages_path = directory / "document.txt", directory / "document.pages.json"
    _guard(text_path, preparation.MAX_ARTIFACT_BYTES)
    _guard(pages_path, preparation.MAX_ARTIFACT_BYTES)
    raw_pages = pages_path.read_bytes()
    if (hashlib.sha256(raw_pages).hexdigest() != provenance["outputs"]["pages_sha256"]
            or len(raw_pages) != provenance["outputs"]["pages_bytes"]):
        raise ValueError("review_pdf_extraction_conflict")
    full_text, ranges = _page_content(raw_pages, pdf["artifact_ref"])
    source = dict(run_id=body.run_id, paper_id=body.paper_id, source_chars=len(full_text), source_kind="text",
        artifact_sha256=provenance["outputs"]["text_sha256"], artifact_bytes=provenance["outputs"]["text_bytes"],
        artifact_ref=domain.text_reference(identity), extraction_truncation=provenance["truncation"])
    source["source_digest"] = preparation.source_digest(source)
    total = provenance["document_page_count"]
    if total is not None and total > domain.MAX_DOCUMENT_PAGES:
        raise ValueError("review_pdf_page_limit_exceeded")
    value = domain.validate(dict(schema_version=domain.ATTACHED_SCHEMA if body.attachment_id else domain.SCHEMA, extraction_id=identity, topic_id=topic, paper_version_id=version,
        created_by=actor.subject or actor.actor_type, created_at=iso_now_utc(), policy_version=domain.POLICY,
        pdf=pdf, provenance=provenance, source=source, page_ranges=ranges,
        no_text_page_numbers=sorted(set(provenance["visited_page_numbers"]) - set(provenance["retained_page_numbers"])),
        unvisited_page_numbers=sorted(set(range(1, total + 1)) - set(provenance["visited_page_numbers"])) if total is not None else None,
        execution=dict(started=False, full_document_review="NOT_ESTABLISHED"), scientific_support="NOT_ASSESSED"))
    if read_text(value) != full_text:
        raise ValueError("review_pdf_extraction_conflict")
    # File publication is recoverable separately; no SQLite transaction is held
    # while parsing a PDF. Only a fully validated pair can gain an immutable link.
    saved, created = repository.save(value, occurrences)
    return dict(extraction=saved, created=created)

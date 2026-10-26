"""Bounded local uploads; no fetching, review replacement or provider work."""
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any

from app.auth.resource_access import ResourceActor
from app.db import bootstrap
from app.domain import review_pdf_attachment as domain, review_pdf_extraction as extraction
from app.domain.path_safety import is_reparse_point, validate_run_id
from app.repositories import review_pdf_attachments as repository
from app.repositories.files import iso_now_utc
from app.repositories.file_locks import file_lock
from app.services import review_input_history


def _targets(topic: str, version: str, actor: ResourceActor) -> list[tuple[str, str]]:
    _, item = review_input_history.visible_paper(topic, version, actor)
    visible = set(item["_reviewOccurrenceKeys"])
    # Historical aliases can contain private paths. New bindings use canonical
    # occurrences, not a list of indistinguishable historical aliases.
    return sorted({(row["runId"], row["paperId"]) for row in item["compatibility"]["occurrences"]
                   if (row["runId"], row["paperId"]) in visible})


def authorize(topic: str, version: str, target: str, actor: ResourceActor) -> tuple[str, str]:
    for run, paper in _targets(topic, version, actor):
        if domain.target_id(topic, version, run, paper) == target:
            return run, paper
    raise KeyError("review_pdf_attachment_not_found")


def listing(topic: str, version: str, actor: ResourceActor) -> dict[str, Any]:
    occurrences = review_input_history.visible_occurrences(topic, version, actor)
    return dict(schema_version="PAPER_PDF_ATTACHMENTS_V1", topic_id=topic, paper_version_id=version,
        targets=[dict(target_id=domain.target_id(topic, version, run, paper), run_id=run)
                 for run, paper in _targets(topic, version, actor)],
        attachments=repository.listing(topic, version, occurrences), max_pdf_bytes=extraction.MAX_PDF_BYTES)


def _path(run: str, digest: str) -> Path:
    validate_run_id(run)
    path = bootstrap.STORAGE_ROOT / run / domain.reference(digest)[4:]
    if any(is_reparse_point(part) for part in (*reversed(path.parents), path)):
        raise ValueError("review_pdf_source_invalid")
    return path


def observe(record: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    from app.services import review_pdf_extraction

    value = domain.validate(record)
    path = _path(value["run_id"], value["pdf_sha256"])
    before = review_pdf_extraction._guard(path, extraction.MAX_PDF_BYTES)
    with path.open("rb") as reader:
        raw = reader.read(extraction.MAX_PDF_BYTES + 1)
    if (len(raw) != value["pdf_bytes"] or hashlib.sha256(raw).hexdigest() != value["pdf_sha256"]
            or review_pdf_extraction._guard(path, extraction.MAX_PDF_BYTES) != before):
        raise ValueError("review_pdf_source_changed")
    return path, dict(sha256=value["pdf_sha256"], bytes=value["pdf_bytes"], artifact_ref=value["artifact_ref"],
                     attachment_id=value["attachment_id"])


def attach(topic: str, version: str, target: str, digest: str, raw: bytes, actor: ResourceActor) -> dict[str, Any]:
    from app.services import review_pdf_extraction

    run, paper = authorize(topic, version, target, actor)
    path = _path(run, digest)
    if not 0 < len(raw) <= extraction.MAX_PDF_BYTES:
        raise ValueError("review_pdf_source_too_large")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("review_pdf_source_changed")
    if not raw.startswith(b"%PDF-"):
        raise ValueError("review_pdf_source_invalid")
    record = domain.validate(dict(schema_version=domain.SCHEMA, attachment_id=domain.identity(target, digest),
        topic_id=topic, paper_version_id=version, target_id=target, run_id=run, paper_id=paper,
        pdf_sha256=digest, pdf_bytes=len(raw), artifact_ref=domain.reference(digest),
        created_at=iso_now_utc(), created_by=actor.subject or actor.actor_type,
        binding="USER_SELECTED_NOT_IDENTITY_VERIFIED", state="ATTACHED_NOT_EXTRACTED_OR_REVIEWED"))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    if is_reparse_point(lock) or (lock.exists() and (not lock.is_file() or lock.stat().st_nlink != 1)):
        raise ValueError("review_pdf_source_invalid")
    with file_lock(lock):
        _path(run, digest)  # Recheck after waiting for the app's publication lock.
        if path.exists():
            observe(record)  # Conflict must never overwrite existing bytes.
            review_pdf_extraction._preflight_pdf(path)
        else:
            # The final path is never visible partially written. Cooperating app
            # writers hold this lock; hostile host path swaps remain out of scope.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as writer:
                    temporary = Path(writer.name)
                    writer.write(raw)
                    writer.flush()
                    os.fsync(writer.fileno())
                review_pdf_extraction._preflight_pdf(temporary)
                authorize(topic, version, target, actor)
                _path(run, digest)
                if path.exists():
                    raise ValueError("review_pdf_source_changed")
                temporary.rename(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        observe(record)
        # No DB link until full bytes are published. A DB failure leaves a
        # reusable content-addressed file; an identical explicit retry recovers.
        if authorize(topic, version, target, actor) != (run, paper):
            raise KeyError("review_pdf_attachment_not_found")
        occurrences = review_input_history.visible_occurrences(topic, version, actor)
        saved, created = repository.save(record, occurrences)
    return dict(attachment=saved, created=created)

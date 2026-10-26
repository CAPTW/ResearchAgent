"""Prepare ranges from an already-linked local extraction; never run a Provider."""
import hashlib
import json
from pathlib import Path
import stat
from typing import Any

from app.auth.resource_access import ResourceActor
from app.db import bootstrap
from app.domain import review_preparation as domain
from app.domain.path_safety import is_reparse_point, validate_run_id
from app.repositories import artifact_refs, paper_reviews, review_preparations as repository
from app.services import paper_bank, paper_review, review_input_history


class _NoLinkedSource(KeyError):
    """A lookup alias with no linked extraction is not a missing artifact."""


def _unavailable_source(run: str, paper: str, reason: str, *, extraction_id: str | None = None) -> dict[str, str]:
    # Legacy lookup aliases can contain local paths. Keep them out of this new
    # diagnostic surface without changing any selectable or persisted identity.
    identity = [run, paper] if extraction_id is None else [run, paper, extraction_id]
    diagnostic_id = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("utf-8")).hexdigest()
    return dict(diagnostic_id=diagnostic_id, run_id=run, reason=reason,
                **({"extraction_id": extraction_id} if extraction_id is not None else {}))


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("nonstandard JSON number")


def _pages_text(raw: bytes) -> str:
    payload = json.loads(raw.decode("utf-8-sig", errors="strict"),
                         object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    pages = payload.get("pages") if isinstance(payload, dict) else payload
    if not isinstance(pages, list):
        raise ValueError("pages must be a list")
    selected = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("page must be an object")
        if any(key in page and not isinstance(page[key], str) for key in ("text", "content")):
            raise ValueError("page text must be a string")
        text, content = page.get("text", ""), page.get("content", "")
        if text and content and text != content:
            raise ValueError("conflicting page text")
        normalized = paper_review._normalize_text(text or content)
        if normalized:
            selected.append(normalized)
    return " ".join(selected)


def _occurrences(topic: str, version: str, actor: ResourceActor) -> list[tuple[str, str]]:
    try:
        return review_input_history.visible_occurrences(topic, version, actor)
    except (FileNotFoundError, paper_bank.PaperVersionNotFound):
        raise KeyError("review_preparation_not_found") from None


def _guard(path: Path) -> tuple[int, int, int, int, int]:
    for part in (*reversed(path.parents), path):
        if is_reparse_point(part):
            raise ValueError("review_preparation_source_invalid")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise KeyError("review_preparation_not_found") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("review_preparation_source_invalid")
    if info.st_size > domain.MAX_ARTIFACT_BYTES:
        raise ValueError("review_preparation_source_too_large")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino


def _source_path(reference: str, run: str) -> tuple[Path, str]:
    try:
        validate_run_id(run)
        resolved = artifact_refs.resolve_artifact_reference(reference, run_id=run)
        if not resolved.is_safe or not resolved.resolved_path:
            raise ValueError("review_preparation_source_invalid")
        path = Path(resolved.resolved_path)
        root = bootstrap.STORAGE_ROOT / run
        relative = path.relative_to(root.resolve())
        lexical = root / relative
        if reference.startswith("run:"):
            lexical = root / reference[4:]
        elif reference.startswith("storage:"):
            lexical = bootstrap.STORAGE_ROOT.parent / reference[8:]
        elif Path(reference).is_absolute() and Path(reference).exists():
            lexical = Path(reference)
        _guard(lexical)
        return path, "run:" + relative.as_posix()
    except FileNotFoundError:
        raise KeyError("review_preparation_not_found") from None
    except ValueError as exc:
        if str(exc).startswith("review_preparation_"):
            raise
        raise ValueError("review_preparation_source_invalid") from None
    except Exception as exc:
        from app.domain.path_safety import PathSafetyError
        if isinstance(exc, PathSafetyError):
            raise ValueError("review_preparation_source_invalid") from None
        raise


def _observe(topic: str, run: str, paper: str) -> tuple[dict[str, Any], str]:
    review = paper_reviews.get_review(topic, run, paper, read_only=True)
    if review is None or not review.get("full_text_artifact_path"):
        raise _NoLinkedSource("review_preparation_not_found")
    reference = str(review["full_text_artifact_path"])
    lowered = reference.lower()
    kind = "pages" if lowered.endswith(".pages.json") else "text" if lowered.endswith(".txt") else None
    if kind is None:
        raise ValueError("review_preparation_source_unsupported")
    path, portable = _source_path(reference, run)
    before = _guard(path)
    try:
        with path.open("rb") as reader:
            raw = reader.read(domain.MAX_ARTIFACT_BYTES + 1)
    except FileNotFoundError:
        raise KeyError("review_preparation_not_found") from None
    if len(raw) > domain.MAX_ARTIFACT_BYTES:
        raise ValueError("review_preparation_source_too_large")
    artifact_hash = hashlib.sha256(raw).hexdigest()
    provenance = review.get("extraction_provenance")
    if provenance and (provenance["artifact"]["sha256"] != artifact_hash or provenance["artifact"]["bytes"] != len(raw)):
        raise ValueError("review_preparation_source_changed")
    # A new preparation rejects undecodable bytes; historical review records
    # retain their older UTF8_ERRORS_IGNORE semantics unchanged.
    try:
        if kind == "text":
            text = paper_review._normalize_text(raw.decode("utf-8", errors="strict"))
        else:
            text = _pages_text(raw)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("review_preparation_source_invalid") from None
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("review_preparation_source_invalid") from None
    if len(text) > domain.MAX_SOURCE_CHARS:
        raise ValueError("review_preparation_source_too_large")
    if not text:
        raise ValueError("review_preparation_source_invalid")
    # Only the chosen linked artifact is read. The repository has validated
    # any stored extraction observation, whose selected artifact bytes above
    # must match; this does not reverify the original PDF or sibling files.
    pdf = provenance.get("pdf_extraction") if provenance else None
    if _guard(path) != before:
        raise ValueError("review_preparation_source_changed")
    source = dict(run_id=run, paper_id=paper, source_chars=len(text), source_kind=kind,
                  artifact_sha256=artifact_hash, artifact_bytes=len(raw), artifact_ref=portable,
                  extraction_truncation=pdf["truncation"] if pdf else "UNKNOWN")
    source["source_digest"] = domain.source_digest(source)
    return source, text


def _observe_selected(topic: str, version: str, run: str, paper: str,
                      expected_source_digest: str) -> tuple[dict[str, Any], str]:
    from app.repositories import review_pdf_extractions
    from app.services import review_pdf_extraction

    # An exact new link remains selectable even if the unrelated legacy link
    # has disappeared. A damaged selected link never falls back to old text.
    records = review_pdf_extractions.listing(topic, version, [(run, paper)])
    for record in records:
        if record["source"]["source_digest"] == expected_source_digest:
            return record["source"], review_pdf_extraction.read_text(record)
    return _observe(topic, run, paper)


def sources(topic: str, version: str, actor: ResourceActor) -> dict[str, Any]:
    from app.repositories import review_pdf_extractions
    from app.services import review_pdf_extraction

    items = []
    unavailable = []
    occurrences = _occurrences(topic, version, actor)
    for run, paper in sorted(set(occurrences)):
        try:
            source, _ = _observe(topic, run, paper)
        except _NoLinkedSource:
            continue
        except KeyError as exc:
            if exc.args != ("review_preparation_not_found",):
                raise
            unavailable.append(_unavailable_source(run, paper, "review_preparation_not_found"))
        except ValueError as exc:
            # Only source observation failures are local to this occurrence.
            # Stored-record corruption and unknown errors must remain visible.
            if str(exc) not in {"review_preparation_source_unsupported", "review_preparation_source_changed",
                                "review_preparation_source_invalid", "review_preparation_source_too_large"}:
                raise
            unavailable.append(_unavailable_source(run, paper, str(exc)))
        else:
            items.append({key: value for key, value in source.items() if key != "artifact_ref"})
    for record in review_pdf_extractions.listing(topic, version, occurrences):
        source = record["source"]
        try:
            review_pdf_extraction.read_text(record)
        except KeyError:
            unavailable.append(_unavailable_source(source["run_id"], source["paper_id"], "review_preparation_not_found",
                                                   extraction_id=record["extraction_id"]))
        except ValueError as exc:
            if str(exc) not in {"review_preparation_source_changed", "review_preparation_source_invalid", "review_preparation_source_too_large"}:
                raise
            unavailable.append(_unavailable_source(source["run_id"], source["paper_id"], str(exc),
                                                   extraction_id=record["extraction_id"]))
        else:
            items.append({**{key: value for key, value in source.items() if key != "artifact_ref"},
                          "extraction_id": record["extraction_id"]})
    return dict(sources=items, unavailable_sources=unavailable, limits=dict(max_selected_chars=domain.MAX_SELECTED_CHARS), state="NOT_EXECUTED",
                **({"reason": "review_preparation_no_supported_source"} if not items else {}))


def preview(topic: str, version: str, body: domain.Preview, actor: ResourceActor) -> dict[str, Any]:
    """Read one bounded window without preparing, executing, or changing records."""
    body = domain.Preview.model_validate(body.model_dump(), strict=True)
    if (body.run_id, body.paper_id) not in _occurrences(topic, version, actor):
        raise KeyError("review_preparation_not_found")
    source, text = _observe_selected(topic, version, body.run_id, body.paper_id, body.expected_source_digest)
    if source["source_digest"] != body.expected_source_digest:
        raise ValueError("review_preparation_source_changed")
    if body.start >= len(text):
        raise ValueError("review_preparation_range_invalid")
    end = min(body.start + body.length, len(text))
    selected = text[body.start:end]
    return dict(
        schema_version="PAPER_REVIEW_PREVIEW_V1",
        source={key: value for key, value in source.items() if key != "artifact_ref"},
        window=dict(start=body.start, end=end, text=selected, chars=len(selected)),
        normalization=domain.NORMALIZATION,
        decoding="UTF8_SIG_STRICT" if source["source_kind"] == "pages" else "UTF8_STRICT",
        state="NOT_EXECUTED",
    )


def prepare(topic: str, version: str, body: domain.Prepare, actor: ResourceActor) -> dict[str, Any]:
    body = domain.Prepare.model_validate(body.model_dump(), strict=True)
    try:
        profile, paper = review_input_history.visible_paper(topic, version, actor)
    except (FileNotFoundError, paper_bank.PaperVersionNotFound):
        raise KeyError("review_preparation_not_found") from None
    occurrences = list(paper["_reviewOccurrenceKeys"])
    if (body.run_id, body.paper_id) not in occurrences:
        raise KeyError("review_preparation_not_found")
    def capture() -> tuple[dict[str, Any], str]:
        source, text = _observe_selected(topic, version, body.run_id, body.paper_id, body.expected_source_digest)
        if source["source_digest"] != body.expected_source_digest:
            raise ValueError("review_preparation_source_changed")
        return source, text
    from app.domain.review_context import capture as capture_context
    return repository.save(topic, version, actor.subject or actor.actor_type, body, occurrences, capture,
                           context_factory=lambda: capture_context(profile, paper))


def listing(topic: str, version: str, actor: ResourceActor, *, page: int = 1, page_size: int = 10) -> dict[str, Any]:
    return repository.listing(topic, version, _occurrences(topic, version, actor), page, page_size)


def detail(topic: str, version: str, preparation_id: str, actor: ResourceActor) -> dict[str, Any]:
    return repository.detail(topic, version, preparation_id, _occurrences(topic, version, actor))


def coverage(topic: str, version: str, preparation_id: str, actor: ResourceActor) -> dict[str, Any]:
    from app.repositories import review_source_coverage
    return review_source_coverage.overview(topic, version, preparation_id, _occurrences(topic, version, actor))

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from app.domain.review_extraction import VERSION as EXTRACTION_VERSION, validate_provenance
from app.domain.topic_membership import run_matches_topic
from app.repositories import artifact_refs, metadata_index, paper_reviews
from app.repositories.files import iso_now_utc
from app.repositories.runs import list_run_records, load_run_record
from app.services import pdf_acquisition, research_transport, reviews as legacy_reviews
from app.services import topic_profiles, workbench

APPROVED_SHORT_ACRONYMS = {"AI", "ML", "RL", "LLM", "RAG", "NLP"}
DENIED_FRAGMENT_KEYWORDS = {"ar", "vr", "xr", "mr", "vio", "imu", "hmd"}
GENERIC_KEYWORDS = {
    "paper",
    "study",
    "method",
    "model",
    "models",
    "data",
    "dataset",
    "datasets",
    "system",
    "results",
}
QUERY_OPERATORS = {"or", "and", "not"}
LOW_RELEVANCE_THRESHOLD = 25.0
FULL_TEXT_STATUS_RANK = {
    "unavailable": 0,
    "abstract_only": 1,
    "full_text_available": 2,
    "reviewed": 3,
}


def canonical_keyword(value: Any) -> str:
    text = re.sub(r"[\-_]+", " ", str(value or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "llms": "large language model",
        "llm": "large language model",
        "large language models": "large language model",
        "large language model": "large language model",
        "rag": "retrieval augmented generation",
        "retrieval augmented generation": "retrieval augmented generation",
        "retrieval augmented": "retrieval augmented generation",
    }
    return aliases.get(text, text)


def is_quality_keyword(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if raw.upper() in APPROVED_SHORT_ACRONYMS:
        return True
    canonical = canonical_keyword(raw)
    if canonical in DENIED_FRAGMENT_KEYWORDS or canonical in GENERIC_KEYWORDS:
        return False
    if len(canonical) <= 2:
        return False
    if len(canonical) == 3 and canonical.isalpha() and canonical not in {
        "rag",
        "nlp",
        "llm",
    }:
        return False
    return bool(re.search(r"[a-zA-Z가-힣]", canonical))


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for quoted in re.findall(r'"([^"]+)"', query):
        terms.append(quoted)
    cleaned = re.sub(r'"[^"]+"', " ", query)
    for token in re.split(r"[^A-Za-z0-9가-힣]+", cleaned):
        token = token.strip()
        if not token or token.lower() in QUERY_OPERATORS:
            continue
        terms.append(token)
    return terms


def topic_terms(topic_profile: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (topic_profile.get("title"), topic_profile.get("topic")):
        if value:
            values.append(str(value))
    for query in topic_profile.get("queries") or []:
        values.extend(_query_terms(str(query)))
    for category in topic_profile.get("categories") or []:
        values.append(str(category))

    normalized: list[str] = []
    for value in values:
        canonical = canonical_keyword(value)
        if not is_quality_keyword(canonical):
            continue
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _paper_blob(item: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    review = review or {}
    fields = [
        item.get("title"),
        item.get("abstract"),
        item.get("summary"),
        item.get("theme"),
        review.get("title"),
        review.get("abstract_text"),
        review.get("text_excerpt"),
        review.get("method_summary"),
        review.get("why_it_matters"),
        review.get("topic_fit_rationale"),
        " ".join(review.get("key_findings") or []),
        " ".join(review.get("limitations") or []),
    ]
    return canonical_keyword(" ".join(str(field or "") for field in fields))


def score_topic_relevance(topic_profile: dict[str, Any], item: dict[str, Any], review: dict[str, Any] | None = None) -> float:
    blob = _paper_blob(item, review)
    score = 0.0
    for term in topic_terms(topic_profile):
        if not term:
            continue
        if term in blob:
            if " " in term:
                score += 24
            elif term.upper() in APPROVED_SHORT_ACRONYMS or term in {"rag", "llm", "nlp"}:
                score += 18
            else:
                score += 10
    title_blob = canonical_keyword(str(item.get("title") or review.get("title") if review else item.get("title") or ""))
    if any(term in title_blob for term in topic_terms(topic_profile) if " " in term):
        score += 15
    return round(min(100.0, score), 1)


def phrase_keywords_for_item(topic_profile: dict[str, Any], item: dict[str, Any], review: dict[str, Any] | None = None) -> list[str]:
    blob = _paper_blob(item, review)
    phrases: list[str] = []
    for term in topic_terms(topic_profile):
        if term in blob and is_quality_keyword(term) and term not in phrases:
            phrases.append(term)
    return phrases


def _artifact_exists(value: str | None) -> bool:
    return bool(value and Path(value).exists())


def _full_text_status(run_id: str, item: dict[str, Any]) -> str:
    paper_id = _paper_id(item)
    if not paper_id:
        return "unavailable"
    artifacts = legacy_reviews.resolve_paper_artifacts(run_id, paper_id)
    if _artifact_exists(artifacts.get("text_txt")) or _artifact_exists(artifacts.get("pages_json")):
        return "full_text_available"
    if _artifact_exists(artifacts.get("pdf")):
        return "full_text_available"
    if str(item.get("abstract") or "").strip():
        return "abstract_only"
    return "unavailable"


def _normalize_text(value: str) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()


def _extraction_observation(raw: bytes, kind: str, text: str, maximum: int, available: int | None) -> dict[str, Any]:
    return {
        "schema_version": EXTRACTION_VERSION,
        "artifact": {"kind": kind, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
        "normalization": "XRML_WHITESPACE_V1",
        "decoding": "PDF_ENGINE" if kind == "pdf_extract" else "UTF8_ERRORS_IGNORE",
        "selection": {
            "sha256": _sha256_text(text), "chars": len(text), "max_chars": max(1, int(maximum)),
            "available_chars": available,
            "truncation": "UNKNOWN" if available is None else "TRUNCATED" if available > len(text) else "NOT_OBSERVED",
        },
        "pdf_extraction": None,
    }


def _read_text_file(path: Path, max_text_chars: int) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    text = _normalize_text(raw.decode("utf-8", errors="ignore"))
    if not text:
        return None
    used = text[: max(1, int(max_text_chars))].rstrip()
    return {
        "source_type": "text",
        "source_path": str(path),
        "text": used,
        "extraction_provenance": _extraction_observation(raw, "text", used, max_text_chars, len(text)),
        "spans": [
            {
                "label": "full_text_excerpt_1",
                "text": used[: min(420, len(used))],
                "source_path": str(path),
                "page_number": None,
                "char_start": 0,
                "char_end": min(420, len(used)),
            }
        ],
    }


def _page_number(page: dict[str, Any], fallback: int) -> int:
    for key in ("page", "page_number", "number"):
        try:
            value = int(page.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return fallback


def _read_pages_file(path: Path, max_text_chars: int, *, raw_bytes: bytes | None = None) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_bytes() if raw_bytes is None else raw_bytes
    try:
        payload = json.loads(raw.decode("utf-8-sig", errors="ignore"))
    except json.JSONDecodeError:
        return None
    raw_pages = payload.get("pages") if isinstance(payload, dict) else payload
    if not isinstance(raw_pages, list):
        return None
    available = len(" ".join(
        text for page in raw_pages if isinstance(page, dict)
        if (text := _normalize_text(page.get("text") or page.get("content") or ""))
    ))
    parts: list[str] = []
    spans: list[dict[str, Any]] = []
    cursor = 0
    max_chars = max(1, int(max_text_chars))
    for index, page in enumerate(raw_pages, start=1):
        if not isinstance(page, dict):
            continue
        text = _normalize_text(str(page.get("text") or page.get("content") or ""))
        if not text:
            continue
        page_no = _page_number(page, index)
        remaining = max_chars - cursor
        if remaining <= 0:
            break
        used = text[:remaining].rstrip()
        parts.append(used)
        span_text = used[: min(360, len(used))]
        spans.append(
            {
                "label": f"full_text_page_{page_no}",
                "text": span_text,
                "source_path": str(path),
                "page_number": page_no,
                "char_start": cursor,
                "char_end": cursor + len(span_text),
            }
        )
        cursor += len(used) + 1
    joined = _normalize_text(" ".join(parts))
    if not joined:
        return None
    return {"source_type": "pages", "source_path": str(path), "text": joined, "spans": spans,
            "extraction_provenance": _extraction_observation(raw, "pages", joined, max_text_chars, available)}


def _extract_existing_pdf(path: Path, max_text_chars: int) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        import fitz  # type: ignore
    except Exception:
        return None
    parts: list[str] = []
    spans: list[dict[str, Any]] = []
    cursor = 0
    max_chars = max(1, int(max_text_chars))
    raw = path.read_bytes()
    doc = None
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        for index in range(doc.page_count):
            remaining = max_chars - cursor
            if remaining <= 0:
                break
            page_text = _normalize_text(doc.load_page(index).get_text("text") or "")
            if not page_text:
                continue
            used = page_text[:remaining]
            parts.append(used)
            span_text = used[: min(360, len(used))]
            spans.append(
                {
                    "label": f"full_text_pdf_page_{index + 1}",
                    "text": span_text,
                    "source_path": str(path),
                    "page_number": index + 1,
                    "char_start": cursor,
                    "char_end": cursor + len(span_text),
                }
            )
            cursor += len(used) + 1
    except Exception:
        return None
    finally:
        if doc is not None:
            doc.close()
    joined = _normalize_text(" ".join(parts))
    if not joined:
        return None
    if path.read_bytes() != raw:
        raise ValueError("extraction_artifact_changed_during_read")
    return {"source_type": "pdf_extract", "source_path": str(path), "text": joined, "spans": spans,
            "extraction_provenance": _extraction_observation(raw, "pdf_extract", joined, max_text_chars, None)}


def read_local_full_text_evidence(
    *,
    run_id: str,
    paper_id: str,
    max_text_chars: int = 24000,
    prefer_existing_text: bool = True,
    extract_existing_pdf: bool = False,
    text_source_run_id: str | None = None,
) -> dict[str, Any]:
    source_run_id = str(text_source_run_id or "").strip() or str(run_id).strip()
    artifacts = legacy_reviews.resolve_paper_artifacts(source_run_id, paper_id)
    candidates: list[tuple[str, str | None]] = []
    if prefer_existing_text:
        candidates.extend([("text", artifacts.get("text_txt")), ("pages", artifacts.get("pages_json"))])
    else:
        candidates.extend([("pages", artifacts.get("pages_json")), ("text", artifacts.get("text_txt"))])

    def _portable_artifact_path(path_value: str, *, target_run_id: str) -> str | None:
        if not path_value:
            return None
        return artifact_refs.normalize_path_reference(path_value, run_id=target_run_id if target_run_id == source_run_id else None)

    def _observed_pdf(kind: str, path: Path) -> dict[str, Any] | None:
        base = path.with_name(path.name.removesuffix(".pages.json")) if kind == "pages" else path.with_suffix("")
        # Append suffixes: paper IDs can contain dots. A present marker with a
        # missing/invalid member must fail before any text/PDF fallback.
        pdf_value, text_value, pages_value = artifacts.get("pdf"), artifacts.get("text_txt"), artifacts.get("pages_json")
        pdf = Path(pdf_value) if pdf_value else base.with_name(base.name + ".pdf")
        text = Path(text_value) if text_value else base.with_name(base.name + ".txt")
        pages = Path(pages_value) if pages_value else base.with_name(base.name + ".pages.json")
        return pdf_acquisition.read_extraction_provenance(text, pages, pdf)

    resolved_pdf = artifacts.get("pdf")
    if resolved_pdf:
        _observed_pdf("pdf", Path(resolved_pdf))
    for kind, path_value in candidates:
        if not path_value:
            continue
        path = Path(path_value)
        pdf_observation = _observed_pdf(kind, path)
        result = _read_text_file(path, max_text_chars) if kind == "text" else _read_pages_file(path, max_text_chars)
        if result is not None:
            provenance = result["extraction_provenance"]
            provenance["pdf_extraction"] = pdf_observation
            validate_provenance(provenance, text_sha256=_sha256_text(result["text"]), text_chars=len(result["text"]))
            return {
                "text_excerpt": result["text"],
                "text_char_count": len(result["text"]),
                "source_text_sha256": _sha256_text(result["text"]),
                "evidence_spans": result["spans"],
                "full_text_status": "reviewed",
                "review_source_type": "full_text",
                "full_text_artifact_path": _portable_artifact_path(result["source_path"], target_run_id=run_id),
                "full_text_source_kind": result["source_type"],
                "artifact_source_run_id": source_run_id,
                "artifact_source_path": result["source_path"],
                "extraction_provenance": provenance,
            }

    pdf_path = artifacts.get("pdf")
    if pdf_path:
        pdf_result = _extract_existing_pdf(Path(pdf_path), max_text_chars) if extract_existing_pdf else None
        if pdf_result is not None:
            return {
                "text_excerpt": pdf_result["text"],
                "text_char_count": len(pdf_result["text"]),
                "source_text_sha256": _sha256_text(pdf_result["text"]),
                "evidence_spans": pdf_result["spans"],
                "full_text_status": "reviewed",
                "review_source_type": "full_text",
                "full_text_artifact_path": _portable_artifact_path(pdf_result["source_path"], target_run_id=run_id),
                "full_text_source_kind": "pdf_extract",
                "artifact_source_run_id": source_run_id,
                "artifact_source_path": pdf_result["source_path"],
                "extraction_provenance": pdf_result["extraction_provenance"],
            }
        if Path(pdf_path).exists():
            return {
                "text_excerpt": "",
                "text_char_count": 0,
                "source_text_sha256": None,
                "evidence_spans": [],
                "full_text_status": "full_text_available",
                "review_source_type": "abstract",
                "full_text_artifact_path": _portable_artifact_path(str(pdf_path), target_run_id=run_id),
                "full_text_source_kind": "pdf",
                "artifact_source_run_id": source_run_id,
                "artifact_source_path": str(pdf_path),
            }

    return {
        "text_excerpt": "",
        "text_char_count": 0,
        "source_text_sha256": None,
        "evidence_spans": [],
        "full_text_status": "unavailable",
        "review_source_type": "abstract",
        "full_text_artifact_path": None,
        "full_text_source_kind": None,
        "artifact_source_run_id": source_run_id,
        "artifact_source_path": None,
    }


def _first_sentence(text: str, max_len: int = 260) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    return re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0][:max_len]


def _sha256(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="ignore"))
        digest.update(b"\n")
    return digest.hexdigest()


def normalize_paper_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.removeprefix("arxiv:")
    text = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", text)
    text = text.removesuffix(".pdf")
    match = re.match(r"(?P<base>\d{4}\.\d+)(?:v\d+)?$", text)
    if match:
        return match.group("base")
    return re.sub(r"\s+", " ", text)


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sort_datetime(value: Any) -> datetime:
    return _parse_datetime(value) or datetime.min.replace(tzinfo=timezone.utc)


def _row_timestamp(run: dict[str, Any], item: dict[str, Any]) -> datetime:
    return max(
        _sort_datetime(item.get("published") or item.get("published_at") or item.get("updated")),
        _sort_datetime(run.get("finished_at") or run.get("updated_at") or run.get("created_at")),
    )


def _within_window(run: dict[str, Any], item: dict[str, Any], window_days: int | None) -> bool:
    if window_days is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(window_days)))
    return _row_timestamp(run, item) >= cutoff


def _source_provider_for_row(run: dict[str, Any], item: dict[str, Any]) -> str | None:
    for key in ("source_provider", "provider", "source"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    source_settings = request.get("source_settings") if isinstance(request.get("source_settings"), dict) else {}
    return str(source_settings.get("defaultProvider") or request.get("source_provider") or "").strip() or None


def _abstract_for_row(item: dict[str, Any]) -> str:
    return str(item.get("abstract") or item.get("abstract_text") or item.get("summary") or "").strip()


def _published_for_row(item: dict[str, Any]) -> str | None:
    return str(item.get("published") or item.get("published_at") or item.get("updated") or "").strip() or None


def _candidate_rank_key(candidate: dict[str, Any]) -> tuple[float, int, datetime, datetime]:
    return (
        float(candidate.get("topic_relevance_score") or 0),
        len(str(candidate.get("abstract_text") or "")),
        _sort_datetime(candidate.get("published_at")),
        _sort_datetime(candidate.get("_run_created_at")),
    )


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if not key.startswith("_")}


def _candidate_from_row(topic_profile: dict[str, Any], run: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    paper_id = _paper_id(item)
    normalized_paper_id = normalize_paper_id(paper_id)
    enriched_item = dict(item)
    enriched_item["paper_id"] = paper_id
    return {
        "paper_id": paper_id,
        "normalized_paper_id": normalized_paper_id,
        "title": str(item.get("title") or "Untitled paper").strip() or "Untitled paper",
        "source_provider": _source_provider_for_row(run, item),
        "best_run_id": str(run.get("run_id") or "").strip(),
        "seen_run_ids": [str(run.get("run_id") or "").strip()],
        "seen_count": 1,
        "published_at": _published_for_row(item),
        "abstract_text": _abstract_for_row(item),
        "pdf_url": str(item.get("pdf_url") or "").strip() or None,
        "topic_relevance_score": score_topic_relevance(topic_profile, enriched_item),
        "duplicate_count": 0,
        "_run": run,
        "_item": enriched_item,
        "_run_created_at": str(run.get("created_at") or ""),
    }


def _consolidate_rows(
    topic_profile: dict[str, Any],
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    window_days: int | None = None,
    min_relevance: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    raw_candidates: list[dict[str, Any]] = []
    for run, item in rows:
        if not _within_window(run, item, window_days):
            continue
        candidate = _candidate_from_row(topic_profile, run, item)
        if candidate["normalized_paper_id"]:
            raw_candidates.append(candidate)

    merged: dict[str, dict[str, Any]] = {}
    for candidate in raw_candidates:
        key = candidate["normalized_paper_id"]
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        seen_run_ids = list(existing["seen_run_ids"])
        for run_id in candidate["seen_run_ids"]:
            if run_id and run_id not in seen_run_ids:
                seen_run_ids.append(run_id)
        seen_count = int(existing.get("seen_count") or 0) + int(candidate.get("seen_count") or 0)
        if _candidate_rank_key(candidate) > _candidate_rank_key(existing):
            winner = candidate
        else:
            winner = existing
        merged[key] = {
            **winner,
            "seen_run_ids": sorted(seen_run_ids),
            "seen_count": seen_count,
            "duplicate_count": max(0, seen_count - 1),
        }

    all_unique = sorted(merged.values(), key=_candidate_rank_key, reverse=True)
    threshold_for_low = LOW_RELEVANCE_THRESHOLD if min_relevance is None else float(min_relevance)
    low_relevance_unique_count = sum(
        1 for candidate in all_unique if float(candidate.get("topic_relevance_score") or 0) < threshold_for_low
    )
    filtered = [
        candidate
        for candidate in all_unique
        if min_relevance is None or float(candidate.get("topic_relevance_score") or 0) >= float(min_relevance)
    ]
    if limit is not None:
        filtered = filtered[: max(0, int(limit))]
    return {
        "raw_row_count": len(raw_candidates),
        "unique_unfiltered_count": len(all_unique),
        "unique_candidate_count": len(filtered),
        "duplicate_row_count": max(0, len(raw_candidates) - len(all_unique)),
        "low_relevance_unique_count": low_relevance_unique_count,
        "candidates": filtered,
    }


def _topic_rows(topic_profile: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in _topic_run_records(topic_profile):
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            continue
        try:
            for item in workbench.load_run_triage_items(run_id):
                rows.append((run, item))
        except FileNotFoundError:
            continue
    return rows


def consolidate_topic_paper_candidates(
    topic_id: str,
    *,
    window_days: int | None = None,
    min_relevance: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    result = _consolidate_rows(
        profile,
        _topic_rows(profile),
        window_days=window_days,
        min_relevance=min_relevance,
        limit=limit,
    )
    return {
        "topic_id": str(profile.get("profile_id") or topic_id),
        "raw_row_count": result["raw_row_count"],
        "unique_candidate_count": result["unique_candidate_count"],
        "duplicate_row_count": result["duplicate_row_count"],
        "low_relevance_unique_count": result["low_relevance_unique_count"],
        "candidates": [_public_candidate(candidate) for candidate in result["candidates"]],
    }


def _existing_reviews_by_normalized_paper_id(topic_id: str, *, read_only: bool = False) -> dict[str, dict[str, Any]]:
    reviews = paper_reviews.list_reviews(topic_id=topic_id, limit=100_000, read_only=read_only)
    selected: dict[str, dict[str, Any]] = {}
    for review in reviews:
        key = normalize_paper_id(review.get("paper_id"))
        if not key:
            continue
        existing = selected.get(key)
        if existing is None or (
            float(review.get("relevance_score") or 0),
            float(review.get("evidence_strength") or 0),
            str(review.get("reviewed_at") or ""),
        ) > (
            float(existing.get("relevance_score") or 0),
            float(existing.get("evidence_strength") or 0),
            str(existing.get("reviewed_at") or ""),
        ):
            selected[key] = review
    return selected


def _status_rank(review: dict[str, Any] | None) -> int:
    if not review:
        return 0
    return FULL_TEXT_STATUS_RANK.get(str(review.get("full_text_status") or "unavailable"), 0)


def _review_is_at_least_as_complete(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return _status_rank(existing) >= _status_rank(candidate)


def _pdf_url_domain(value: str | None) -> str | None:
    try:
        host = str(urlparse(str(value or "")).hostname or "")
        return research_transport.normalize_retrieval_hostname(host)
    except (ValueError, research_transport.RetrievalTransportError):
        return None


def _domain_allowed_for_review(value: str | None, allowed_domains: list[str] | None) -> bool:
    return pdf_acquisition._domain_allowed(str(value or ""), allowed_domains)


def _normalized_paper_id_filter(values: list[str] | None) -> set[str]:
    return {normalized for value in values or [] if (normalized := normalize_paper_id(value))}


def _candidate_previous_acquisition_status(candidate: dict[str, Any]) -> str | None:
    item = candidate.get("_item") if isinstance(candidate.get("_item"), dict) else {}
    status = str(
        item.get("pdf_acquisition_status")
        or item.get("acquisition_status")
        or item.get("download_status")
        or ""
    ).strip()
    reason = str(
        item.get("pdf_rejection_reason")
        or item.get("acquisition_reason")
        or item.get("download_error")
        or item.get("rejection_reason")
        or ""
    ).strip()
    if status and reason:
        return f"{status}:{reason}"
    return status or reason or None


def _candidate_local_artifact_state(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    run_id = str(candidate.get("best_run_id") or "").strip()
    paper_id = str(candidate.get("paper_id") or "").strip()
    if not run_id or not paper_id:
        return None, None
    artifacts = legacy_reviews.resolve_paper_artifacts(run_id, paper_id)
    for key, state in (("text_txt", "local_text_exists"), ("pages_json", "local_text_exists"), ("pdf", "local_pdf_exists")):
        path_value = artifacts.get(key)
        if path_value and Path(path_value).exists():
            return state, pdf_acquisition._artifact_ref(Path(path_value), run_id=run_id)
    return None, None


def _prioritize_full_text_candidates(
    *,
    topic_id: str,
    candidates: list[dict[str, Any]],
    provider: str = "arxiv",
    allowed_domains: list[str] | None = None,
    download_limit: int = 0,
    exclude_paper_ids: list[str] | None = None,
    target_paper_id: str | None = None,
) -> dict[str, Any]:
    existing_by_normalized = _existing_reviews_by_normalized_paper_id(topic_id)
    expected_provider = str(provider or "").strip().lower()
    excluded_ids = _normalized_paper_id_filter(exclude_paper_ids)
    target_id = normalize_paper_id(target_paper_id)
    target_seen = False
    entries: list[dict[str, Any]] = []
    acquisition_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    skip_counts: Counter[str] = Counter()
    already_downloaded_or_extracted_count = 0

    for candidate in candidates:
        normalized_id = str(candidate.get("normalized_paper_id") or "").strip()
        if target_id and normalized_id == target_id:
            target_seen = True
        existing = existing_by_normalized.get(normalized_id)
        current_status = str(existing.get("full_text_status") if existing else "unavailable") if existing else "unavailable"
        local_state, local_artifact_path = _candidate_local_artifact_state(candidate)
        if current_status == "unavailable" and local_state:
            current_status = "full_text_available"
        if local_state:
            already_downloaded_or_extracted_count += 1

        source_provider = str(candidate.get("source_provider") or "").strip().lower()
        pdf_url = str(candidate.get("pdf_url") or "").strip()
        domain = _pdf_url_domain(pdf_url)
        previous_status = _candidate_previous_acquisition_status(candidate)
        skip_reason: str | None = None
        planned_action = "download_pdf"

        if normalized_id in excluded_ids:
            skip_reason = "excluded_paper_id"
        elif target_id and normalized_id != target_id:
            skip_reason = "target_paper_id_mismatch"
        elif current_status == "reviewed":
            skip_reason = "already_full_text_reviewed"
        elif expected_provider and source_provider and source_provider != expected_provider:
            skip_reason = "provider_mismatch"
        elif not pdf_url:
            skip_reason = "missing_pdf_url"
        elif not _domain_allowed_for_review(pdf_url, allowed_domains):
            skip_reason = "domain_not_allowed"
        elif local_state == "local_text_exists":
            skip_reason = "local_text_exists"
        elif local_state == "local_pdf_exists":
            planned_action = "extract_existing_pdf"

        is_candidate = skip_reason is None
        if skip_reason:
            skip_counts[skip_reason] += 1
        entry = {
            "rank": None,
            "paper_id": str(candidate.get("paper_id") or ""),
            "normalized_paper_id": normalized_id,
            "title": str(candidate.get("title") or ""),
            "relevance_score": float(candidate.get("topic_relevance_score") or 0),
            "published_at": candidate.get("published_at"),
            "pdf_url": pdf_url or None,
            "pdf_url_domain": domain,
            "source_provider": candidate.get("source_provider"),
            "current_full_text_status": current_status,
            "previous_acquisition_status": previous_status,
            "skip_reason": skip_reason,
            "is_acquisition_candidate": is_candidate,
            "planned_action": planned_action if is_candidate else None,
            "local_artifact_path": local_artifact_path,
            "_candidate": candidate,
        }
        entries.append(entry)
        if is_candidate:
            acquisition_pairs.append((entry, candidate))

    acquisition_pairs.sort(key=lambda pair: _candidate_rank_key(pair[1]), reverse=True)
    for rank, (entry, _candidate) in enumerate(acquisition_pairs, start=1):
        entry["rank"] = rank
    entries.sort(
        key=lambda entry: (
            0 if entry["is_acquisition_candidate"] else 1,
            int(entry["rank"] or 1_000_000),
            -float(entry.get("relevance_score") or 0),
            str(entry.get("paper_id") or ""),
        )
    )
    acquisition_candidates = [candidate for _entry, candidate in acquisition_pairs]
    download_candidates = [entry for entry, _candidate in acquisition_pairs if entry.get("planned_action") == "download_pdf"]
    existing_pdf_candidates = [entry for entry, _candidate in acquisition_pairs if entry.get("planned_action") == "extract_existing_pdf"]
    max_downloads = max(0, int(download_limit or 0))
    public_entries = [{key: value for key, value in entry.items() if not key.startswith("_")} for entry in entries]
    target_status = None
    if target_id:
        target_status = "target_found" if target_seen else "no_candidate_found"
    return {
        "topic_id": topic_id,
        "candidates": public_entries,
        "summary": {
            "candidate_count": len(candidates),
            "acquisition_candidate_count": len(acquisition_candidates),
            "planned_download_count": min(len(download_candidates), max_downloads) if max_downloads else len(download_candidates),
            "planned_existing_pdf_extract_count": len(existing_pdf_candidates),
            "already_reviewed_count": skip_counts.get("already_full_text_reviewed", 0),
            "excluded_count": skip_counts.get("excluded_paper_id", 0),
            "target_paper_id": str(target_paper_id or "").strip() or None,
            "target_normalized_paper_id": target_id or None,
            "target_found": bool(target_seen) if target_id else None,
            "target_status": target_status,
            "already_downloaded_or_extracted_count": already_downloaded_or_extracted_count,
            "skip_reason_counts": dict(sorted(skip_counts.items())),
        },
        "_acquisition_candidates": acquisition_candidates,
    }


def prioritize_full_text_acquisition_candidates(
    topic_id: str,
    *,
    window_days: int | None = None,
    min_relevance: float | None = None,
    unique_limit: int | None = None,
    provider: str = "arxiv",
    allowed_domains: list[str] | None = None,
    download_limit: int = 0,
    exclude_paper_ids: list[str] | None = None,
    target_paper_id: str | None = None,
) -> dict[str, Any]:
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    consolidated = _consolidate_rows(
        profile,
        _topic_rows(profile),
        window_days=window_days,
        min_relevance=min_relevance,
        limit=unique_limit,
    )
    result = _prioritize_full_text_candidates(
        topic_id=str(profile.get("profile_id") or topic_id),
        candidates=consolidated["candidates"],
        provider=provider,
        allowed_domains=allowed_domains,
        download_limit=download_limit,
        exclude_paper_ids=exclude_paper_ids,
        target_paper_id=target_paper_id,
    )
    result.pop("_acquisition_candidates", None)
    result["raw_discovered_count"] = consolidated["raw_row_count"]
    result["unique_discovered_count"] = len(consolidated["candidates"])
    result["duplicate_row_count"] = consolidated["duplicate_row_count"]
    return result


def _read_validation_artifact(path_value: str | Path, *, run_id: str | None = None, observed_bytes: bytes | None = None) -> tuple[str | None, str | None]:
    resolved = artifact_refs.resolve_artifact_reference(str(path_value), run_id=run_id)
    if not resolved.is_safe:
        return None, "unsafe_artifact_reference"
    if not resolved.resolved_path:
        return None, "missing_artifact_file"
    path = Path(resolved.resolved_path)
    if not path.exists():
        return None, "missing_artifact_file"
    raw = path.read_bytes() if observed_bytes is None else observed_bytes
    if path.suffix.lower() == ".txt":
        text = _normalize_text(raw.decode("utf-8", errors="ignore"))
        return text, None
    if path.suffix.lower() == ".json" and path.name.endswith(".pages.json"):
        # The review's historical cap is not necessarily 100k. JSON file bytes
        # bound the normalized character count, so validate all available page
        # text rather than imposing an unrelated second truncation limit.
        parsed = _read_pages_file(path, max(1, len(raw)), raw_bytes=raw)
        return (parsed or {}).get("text"), None if parsed else "empty_extracted_text"
    if path.suffix.lower() == ".pdf":
        return None, "reviewed_pdf_artifact_not_revalidatable"
    return None, "unsupported_artifact_type"


def validate_reviewed_artifact_content(review: dict[str, Any]) -> dict[str, Any]:
    status = str(review.get("full_text_status") or "unavailable")
    run_id = str(review.get("run_id") or "")
    artifact_path = str(review.get("full_text_artifact_path") or "").strip()
    spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
    expected_hash = str(review.get("source_text_sha256") or "").strip()
    result: dict[str, Any] = {
        "valid": False,
        "reason": "not_reviewed",
        "status": status,
        "expected_source_text_sha256": expected_hash or None,
        "actual_source_text_sha256": None,
        "artifact_path": artifact_path or None,
        "evidence_span_count": len(spans),
        "validated_scope": None,
        "validated_char_count": 0,
        # Legacy hashes certify normalized text, not original artifact bytes or
        # extraction completeness. Even full equality cannot establish those.
        "complete_document_integrity": "NOT_ESTABLISHED",
        "artifact_bytes_integrity": "NOT_ESTABLISHED",
    }
    if status != "reviewed":
        return result
    if not expected_hash:
        result["reason"] = "reviewed_missing_source_text_sha256"
        return result
    if not spans:
        result["reason"] = "reviewed_missing_evidence_spans"
        return result
    if not artifact_path:
        result["reason"] = "missing_artifact_path"
        return result
    observed_bytes = None
    if review.get("extraction_provenance") is not None:
        preserved_text = str(review.get("text_excerpt") or "")
        try:
            provenance = validate_provenance(review["extraction_provenance"], text_sha256=_sha256_text(preserved_text), text_chars=len(preserved_text))
            if expected_hash != _sha256_text(preserved_text) or review.get("text_char_count") != len(preserved_text):
                raise ValueError("review_extraction_provenance_invalid")
            resolved = artifact_refs.resolve_artifact_reference(artifact_path, run_id=run_id)
            if not resolved.is_safe or not resolved.resolved_path:
                result["reason"] = "unsafe_artifact_reference" if not resolved.is_safe else "missing_artifact_file"
                return result
            raw = Path(resolved.resolved_path).read_bytes()
            artifact = provenance["artifact"]
            if artifact["sha256"] != hashlib.sha256(raw).hexdigest() or artifact["bytes"] != len(raw):
                result["reason"] = "extraction_artifact_bytes_mismatch"
                return result
            result["artifact_bytes_integrity"] = "VERIFIED"
            observed_bytes = raw
            if artifact["kind"] == "pdf_extract":
                result.update(valid=len(preserved_text) >= pdf_acquisition.MIN_EXTRACTED_TEXT_CHARS,
                              reason="valid_reviewed_evidence" if len(preserved_text) >= pdf_acquisition.MIN_EXTRACTED_TEXT_CHARS else "too_short_extracted_text",
                              validated_scope="artifact_bytes", validated_char_count=len(preserved_text))
                return result
        except (ValueError, OSError) as exc:
            result["reason"] = "review_extraction_provenance_invalid" if isinstance(exc, ValueError) else "missing_artifact_file"
            return result
    text, reason = _read_validation_artifact(artifact_path, run_id=run_id, observed_bytes=observed_bytes)
    if reason:
        result["reason"] = reason
        return result
    if not text:
        result["reason"] = "empty_extracted_text"
        return result
    if len(text) < pdf_acquisition.MIN_EXTRACTED_TEXT_CHARS:
        result["reason"] = "too_short_extracted_text"
        return result
    actual_hash = _sha256_text(text)
    result["actual_source_text_sha256"] = actual_hash
    if expected_hash != actual_hash:
        # Historical generation hashes exactly the captured normalized prefix.
        # Its preserved text, explicit length and hash must agree before trying
        # that scope; never infer a cap such as 24k from a mismatched artifact.
        stored = review.get("text_excerpt")
        stored_length = review.get("text_char_count")
        if (not isinstance(stored, str) or type(stored_length) is not int
                or stored_length != len(stored) or _sha256_text(stored) != expected_hash):
            result["reason"] = "sha256_mismatch"
            return result
        if stored_length < pdf_acquisition.MIN_EXTRACTED_TEXT_CHARS:
            result["reason"] = "too_short_extracted_text"
            return result
        prefix = text[:stored_length]
        result["actual_source_text_sha256"] = _sha256_text(prefix)
        if prefix != stored:
            result["reason"] = "sha256_mismatch"
            return result
        result["validated_scope"] = "normalized_prefix"
        result["validated_char_count"] = stored_length
    else:
        result["validated_scope"] = "normalized_artifact"
        result["validated_char_count"] = len(text)
    result["valid"] = True
    result["reason"] = "valid_reviewed_evidence"
    return result


def _repair_action_for_issue(issue: dict[str, Any]) -> dict[str, Any]:
    reason = str(issue.get("reason") or "")
    if reason in {"missing_artifact_file", "empty_extracted_text", "too_short_extracted_text", "reviewed_missing_evidence_spans"}:
        action = "re_extract_or_refresh_review"
    elif reason == "sha256_mismatch":
        action = "refresh_hash_metadata"
    elif reason == "stale_full_text_available":
        action = "mark_needs_attention"
    elif reason == "duplicate_local_artifact":
        action = "ignore_duplicate_artifact"
    else:
        action = "inspect_review"
    return {
        "paper_id": issue.get("paper_id"),
        "run_id": issue.get("run_id"),
        "reason": reason,
        "action": action,
        "writes": False,
    }


def validate_evidence_store_for_topic(
    topic_id: str,
    *,
    include_repair_plan: bool = False,
) -> dict[str, Any]:
    resolved_topic_id = str(topic_profiles.get_topic_profile(str(topic_id or "").strip()).get("profile_id") or topic_id)
    reviews = paper_reviews.list_reviews(topic_id=resolved_topic_id, limit=100_000, read_only=True)
    issues: list[dict[str, Any]] = []
    artifact_paths_by_paper: dict[str, set[str]] = {}
    valid_count = 0

    def add_issue(review: dict[str, Any], reason: str, severity: str, detail: str | None = None) -> None:
        issues.append(
            {
                "paper_id": review.get("paper_id"),
                "run_id": review.get("run_id"),
                "full_text_status": review.get("full_text_status"),
                "reason": reason,
                "severity": severity,
                "detail": detail,
            }
        )

    for review in reviews:
        review_issue_count = len(issues)
        status = str(review.get("full_text_status") or "unavailable")
        artifact_path = str(review.get("full_text_artifact_path") or "").strip()
        normalized_id = normalize_paper_id(review.get("paper_id"))
        if artifact_path:
            artifact_paths_by_paper.setdefault(normalized_id, set()).add(artifact_path)
        if status == "reviewed":
            structural: list[str] = []
            if not review.get("source_text_sha256"):
                structural.append("reviewed_missing_source_text_sha256")
            if not isinstance(review.get("evidence_spans"), list) or not review["evidence_spans"]:
                structural.append("reviewed_missing_evidence_spans")
            if not artifact_path:
                structural.append("missing_artifact_path")
            for reason in structural:
                add_issue(review, reason, "error")
            validation = validate_reviewed_artifact_content(review)
            if not validation["valid"] and validation["reason"] not in structural:
                reason = str(validation["reason"])
                add_issue(review, reason, "warning" if reason == "reviewed_pdf_artifact_not_revalidatable" else "error")
            if structural and artifact_path:
                # The store diagnostic reports independent missing metadata and
                # file failures; do not lose them to the single-result validator.
                text, artifact_reason = _read_validation_artifact(artifact_path, run_id=str(review.get("run_id") or ""))
                if artifact_reason:
                    add_issue(review, artifact_reason, "warning" if artifact_reason == "reviewed_pdf_artifact_not_revalidatable" else "error")
                elif not text or len(text) < pdf_acquisition.MIN_EXTRACTED_TEXT_CHARS:
                    add_issue(review, "empty_extracted_text" if not text else "too_short_extracted_text", "error")
        elif status == "full_text_available":
            resolved = artifact_refs.resolve_artifact_reference(artifact_path, run_id=str(review.get("run_id") or ""))
            if artifact_path and (not resolved.is_safe or not resolved.exists):
                add_issue(review, "stale_full_text_available", "warning")
            elif not artifact_path:
                add_issue(review, "stale_full_text_available", "warning")
        if len(issues) == review_issue_count:
            valid_count += 1

    for paper_id, paths in artifact_paths_by_paper.items():
        if paper_id and len(paths) > 1:
            issues.append(
                {
                    "paper_id": paper_id,
                    "run_id": None,
                    "full_text_status": None,
                    "reason": "duplicate_local_artifact",
                    "severity": "warning",
                    "detail": "; ".join(sorted(paths)),
                }
            )

    reason_counts = Counter(str(issue["reason"]) for issue in issues)
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    affected_paper_ids = sorted({str(issue.get("paper_id") or "") for issue in issues if str(issue.get("paper_id") or "")})
    result = {
        "topic_id": resolved_topic_id,
        "mode": "report_only",
        "review_count": len(reviews),
        "valid_count": valid_count,
        "warning_count": warning_count,
        "error_count": error_count,
        "reason_counts": dict(sorted(reason_counts.items())),
        "affected_paper_ids": affected_paper_ids,
        "issues": issues,
    }
    if include_repair_plan:
        result["repair_plan"] = {
            "mode": "plan_only",
            "writes": False,
            "actions": [_repair_action_for_issue(issue) for issue in issues],
        }
    return result


def review_coverage_for_rows(
    topic_profile: dict[str, Any],
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    min_relevance: float = LOW_RELEVANCE_THRESHOLD,
    read_only: bool = False,
) -> dict[str, Any]:
    topic_id = str(topic_profile.get("profile_id") or "").strip()
    consolidated = _consolidate_rows(topic_profile, rows)
    candidates = consolidated["candidates"]
    relevant = [
        candidate
        for candidate in candidates
        if float(candidate.get("topic_relevance_score") or 0) >= float(min_relevance)
    ]
    denominator_candidates = relevant or candidates
    existing_by_normalized = _existing_reviews_by_normalized_paper_id(topic_id, read_only=read_only)
    reviewed = 0
    abstract_only = 0
    full_text_available = 0
    full_text_reviewed = 0
    for candidate in denominator_candidates:
        review = existing_by_normalized.get(str(candidate.get("normalized_paper_id") or ""))
        if review is None:
            continue
        reviewed += 1
        status = str(review.get("full_text_status") or "unavailable")
        if status == "abstract_only":
            abstract_only += 1
        elif status == "full_text_available":
            full_text_available += 1
        elif status == "reviewed":
            full_text_reviewed += 1
    denominator = len(denominator_candidates)
    return {
        "totalPapers": denominator,
        "reviewedCount": reviewed,
        "abstractOnlyCount": abstract_only,
        "fullTextReviewedCount": full_text_available + full_text_reviewed,
        "coveragePercentage": round((reviewed / denominator) * 100, 1) if denominator else 0.0,
        "rawIndexedPaperCount": consolidated["raw_row_count"],
        "uniqueCandidateCount": consolidated["unique_unfiltered_count"],
        "topicRelevantUniqueCount": len(relevant),
        "reviewedUniqueCount": reviewed,
        "abstractOnlyUniqueCount": abstract_only,
        "fullTextAvailableUniqueCount": full_text_available,
        "fullTextReviewedUniqueCount": full_text_reviewed,
    }


def generate_deterministic_review(
    *,
    topic_profile: dict[str, Any],
    run: dict[str, Any],
    item: dict[str, Any],
    use_full_text: bool = False,
    max_text_chars: int = 24000,
    prefer_existing_text: bool = True,
    extract_existing_pdf: bool = False,
    text_source_run_id: str | None = None,
) -> dict[str, Any]:
    run_id = str(run.get("run_id") or "").strip()
    topic_id = str(topic_profile.get("profile_id") or "").strip() or str(run.get("topic_profile_id") or "").strip()
    paper_id = _paper_id(item)
    title = str(item.get("title") or "Untitled paper").strip() or "Untitled paper"
    abstract = str(item.get("abstract") or item.get("summary") or "").strip()
    status = _full_text_status(run_id, item)
    relevance = score_topic_relevance(topic_profile, item)
    excerpt = _first_sentence(abstract)
    evidence_source_run_id = str(text_source_run_id or "").strip() or run_id
    text_evidence = read_local_full_text_evidence(
        run_id=run_id,
        paper_id=paper_id,
        max_text_chars=max_text_chars,
        prefer_existing_text=prefer_existing_text,
        extract_existing_pdf=extract_existing_pdf,
        text_source_run_id=evidence_source_run_id,
    )
    if use_full_text and text_evidence.get("full_text_status") == "reviewed":
        status = "reviewed"
        excerpt = str(text_evidence.get("text_excerpt") or "")
    elif status == "full_text_available" and not use_full_text:
        # Local full text exists, but this refresh did not read it.
        text_evidence = {
            **text_evidence,
            "full_text_status": "full_text_available",
            "review_source_type": "abstract",
            "text_excerpt": "",
            "text_char_count": 0,
            "source_text_sha256": None,
            "evidence_spans": [],
        }
    source_provider = str(item.get("source_provider") or item.get("source") or "arxiv").strip() or "arxiv"
    status_label = {
        "abstract_only": "초록",
        "full_text_available": "로컬 전문 가용",
        "reviewed": "로컬 전문 본문",
        "unavailable": "메타데이터",
    }[status]
    evidence_spans = list(text_evidence.get("evidence_spans") or []) if status == "reviewed" else []
    review_source_type = "full_text" if status == "reviewed" else "abstract"
    source_text = excerpt if status == "reviewed" else abstract
    finding_excerpt = _first_sentence(excerpt, max_len=280) if status == "reviewed" else excerpt

    return {
        "topic_id": topic_id,
        "run_id": run_id,
        "paper_id": paper_id,
        "title": title,
        "source_provider": source_provider,
        "authors": item.get("authors") or item.get("author_names") or [],
        "published_at": item.get("published") or item.get("published_at") or item.get("updated"),
        "abstract_text": abstract,
        "text_excerpt": excerpt,
        "full_text_status": status,
        "relevance_score": relevance,
        "novelty_score": min(100.0, 45.0 + relevance * 0.35),
        "evidence_strength": 75.0 if status == "reviewed" else 55.0 if status == "full_text_available" else 45.0 if status == "abstract_only" else 20.0,
        "method_summary": f"{status_label} 기준으로 제목과 요약 신호를 정리한 결정론적 리뷰입니다.",
        "key_findings": [
            (
                f"본문 근거: {evidence_spans[0]['text'][:180]}"
                if evidence_spans
                else f"{title}은(는) {', '.join(phrase_keywords_for_item(topic_profile, item)[:3]) or '주제 관련 신호'}를 포함합니다."
            ),
            finding_excerpt or "초록/본문 텍스트가 부족해 핵심 발견은 제한적으로만 요약됩니다.",
        ],
        "limitations": [
            "로컬 전문 텍스트를 읽고 해시한 리뷰입니다." if status == "reviewed" else "전문 텍스트가 처리되지 않은 경우 초록/메타데이터 기반 판단입니다.",
            "외부 AI 호출 없이 생성한 휴리스틱 baseline 리뷰입니다.",
        ],
        "why_it_matters": "주제별 보고서가 논문 단위 근거와 검토 범위를 명시하도록 하는 기본 입력입니다.",
        "topic_fit_rationale": f"주제 관련도 {relevance:.1f}점: 프로필 쿼리/제목/초록 키워드 일치도를 기준으로 계산했습니다.",
        "review_model": "deterministic-local-full-text-v1" if status == "reviewed" else "deterministic-baseline-v1",
        "reviewer": "deterministic-local-full-text" if status == "reviewed" else "deterministic-baseline",
        "source_text_sha256": str(text_evidence.get("source_text_sha256") or "").strip() or _sha256(title, source_text, excerpt),
        "full_text_artifact_path": text_evidence.get("full_text_artifact_path"),
        "text_char_count": int(text_evidence.get("text_char_count") or 0) if status == "reviewed" else 0,
        "evidence_spans": evidence_spans,
        "review_source_type": review_source_type,
        "full_text_reviewed_at": iso_now_utc() if status == "reviewed" else None,
        "extraction_provenance": text_evidence.get("extraction_provenance") if status == "reviewed" else None,
    }


def refresh_reviews_for_run(
    run_id: str,
    *,
    topic_profile: dict[str, Any],
    limit: int | None = None,
    use_full_text: bool = False,
    max_text_chars: int = 24000,
    prefer_existing_text: bool = True,
    extract_existing_pdf: bool = False,
) -> dict[str, Any]:
    run = load_run_record(run_id)
    items = workbench.load_run_triage_items(run_id)
    if limit is not None:
        items = items[: max(0, int(limit))]
    upserted = 0
    for item in items:
        paper_reviews.upsert_review(
            generate_deterministic_review(
                topic_profile=topic_profile,
                run=run,
                item=item,
                use_full_text=use_full_text,
                max_text_chars=max_text_chars,
                prefer_existing_text=prefer_existing_text,
                extract_existing_pdf=extract_existing_pdf,
            )
        )
        upserted += 1
    return {"run_id": run_id, "upserted": upserted}


def _sort_stamp(value: Any) -> str:
    return str(value or "")


def _topic_run_records(topic_profile: dict[str, Any]) -> list[dict[str, Any]]:
    topic_id = str(topic_profile.get("profile_id") or "").strip()
    topic = str(topic_profile.get("topic") or topic_profile.get("title") or "").strip()
    runs = [
        run
        for run in list_run_records(limit=None)
        if run_matches_topic(run, topic, topic_id)
    ]
    runs.sort(
        key=lambda run: (
            _sort_stamp(run.get("finished_at") or run.get("updated_at") or run.get("created_at")),
            _sort_stamp(run.get("created_at")),
        ),
        reverse=True,
    )
    return runs


def _paper_id(item: dict[str, Any]) -> str:
    for key in ("paper_id", "occurrence_id", "entry_id", "entry_url", "pdf_url", "title"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    return _sha256(str(item))[:16]


def _candidate_sort_key(topic_profile: dict[str, Any], run: dict[str, Any], item: dict[str, Any]) -> tuple[float, float, str, str]:
    return (
        score_topic_relevance(topic_profile, item),
        float(item.get("score", 0) or 0),
        _sort_stamp(item.get("published") or item.get("updated")),
        _sort_stamp(run.get("created_at")),
    )


def _review_candidates(
    topic_profile: dict[str, Any],
    *,
    limit: int | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for run in _topic_run_records(topic_profile):
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            continue
        try:
            items = workbench.load_run_triage_items(run_id)
        except FileNotFoundError:
            continue
        for item in items:
            paper_id = _paper_id(item)
            key = (run_id, paper_id)
            if key in seen:
                continue
            seen.add(key)
            enriched = dict(item)
            enriched["paper_id"] = paper_id
            candidates.append((run, enriched))

    candidates.sort(key=lambda pair: _candidate_sort_key(topic_profile, pair[0], pair[1]), reverse=True)
    if limit is not None:
        candidates = candidates[: max(0, int(limit))]
    return candidates


def refresh_reviews_for_topic(
    topic_id: str,
    limit: int | None = None,
    force: bool = False,
    *,
    dry_run: bool = False,
    window_days: int | None = None,
    min_relevance: float | None = None,
    unique_limit: int | None = None,
    use_full_text: bool = False,
    max_text_chars: int = 24000,
    prefer_existing_text: bool = True,
    extract_existing_pdf: bool = False,
    fetch_missing_pdf: bool = False,
    download_limit: int = 0,
    provider: str = "arxiv",
    allow_domains: list[str] | None = None,
    max_pdf_bytes: int = pdf_acquisition.DEFAULT_MAX_PDF_BYTES,
    download_timeout: int = pdf_acquisition.DEFAULT_TIMEOUT_SECONDS,
    extract_after_fetch: bool = False,
    allow_bounded_pdf_fetch: bool = False,
    exclude_paper_ids: list[str] | None = None,
    target_paper_id: str | None = None,
    expected_pdf_sha256_by_paper_id: dict[str, str] | None = None,
    expected_source_text_sha256_by_paper_id: dict[str, str] | None = None,
    text_source_run_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    resolved_topic_id = str(profile.get("profile_id") or topic_id).strip()
    metadata_index.ensure_index_ready()

    resolved_text_source_run_id = str(text_source_run_id or "").strip() or None
    effective_unique_limit = unique_limit if unique_limit is not None else limit
    target_normalized_paper_id = normalize_paper_id(target_paper_id)
    consolidation_limit = effective_unique_limit if not target_normalized_paper_id else None
    consolidated = _consolidate_rows(
        profile,
        _topic_rows(profile),
        window_days=window_days,
        min_relevance=min_relevance,
        limit=consolidation_limit,
    )
    candidates = consolidated["candidates"]
    if resolved_text_source_run_id and not target_normalized_paper_id:
        raise ValueError("--text-source-run-id requires --target-paper-id")
    summary = {
        "topic_id": resolved_topic_id,
        "force": bool(force),
        "dry_run": bool(dry_run),
        "limit": limit,
        "unique_limit": effective_unique_limit,
        "window_days": window_days,
        "min_relevance": min_relevance,
        "use_full_text": bool(use_full_text),
        "max_text_chars": int(max_text_chars),
        "prefer_existing_text": bool(prefer_existing_text),
        "extract_existing_pdf": bool(extract_existing_pdf),
        "fetch_missing_pdf": bool(fetch_missing_pdf),
        "download_limit": int(download_limit or 0),
        "provider": str(provider or "arxiv"),
        "allow_domains": allow_domains or ["arxiv.org"],
        "max_pdf_bytes": int(max_pdf_bytes or pdf_acquisition.DEFAULT_MAX_PDF_BYTES),
        "download_timeout": int(download_timeout or pdf_acquisition.DEFAULT_TIMEOUT_SECONDS),
        "extract_after_fetch": bool(extract_after_fetch),
        "allow_bounded_pdf_fetch": bool(allow_bounded_pdf_fetch),
        "exclude_paper_ids": list(exclude_paper_ids or []),
        "text_source_run_id": resolved_text_source_run_id,
        "target_paper_id": str(target_paper_id or "").strip() or None,
        "raw_discovered_count": consolidated["raw_row_count"],
        "unique_discovered_count": len(candidates),
        "duplicate_row_count": consolidated["duplicate_row_count"],
        "raw_reviewed_count": 0,
        "unique_reviewed_count": 0,
        "raw_skipped_count": 0,
        "unique_skipped_count": 0,
        "abstract_only_count": 0,
        "unavailable_count": 0,
        "full_text_available_count": 0,
        "full_text_reviewed_count": 0,
        "reviewed_from_text_count": 0,
        "reviewed_from_pages_count": 0,
        "reviewed_from_pdf_extract_count": 0,
        "pdf_planned_count": 0,
        "pdf_downloaded_count": 0,
        "pdf_rejected_count": 0,
        "pdf_failed_count": 0,
        "pdf_extracted_count": 0,
        "pdf_extraction_failed_count": 0,
        "extracted_text_count": 0,
        "extracted_page_count": 0,
        "low_relevance_unique_count": consolidated["low_relevance_unique_count"],
        "report_eligible_unique_count": 0,
        "source_run_ids": [],
        "prioritization": {
            "topic_id": resolved_topic_id,
            "candidates": [],
            "summary": {
                "candidate_count": len(candidates),
                "acquisition_candidate_count": 0,
                "planned_download_count": 0,
                "planned_existing_pdf_extract_count": 0,
                "already_reviewed_count": 0,
                "excluded_count": 0,
                "target_paper_id": str(target_paper_id or "").strip() or None,
                "target_normalized_paper_id": target_normalized_paper_id or None,
                "target_found": None if not target_normalized_paper_id else False,
                "target_status": None if not target_normalized_paper_id else "no_candidate_found",
                "already_downloaded_or_extracted_count": 0,
                "skip_reason_counts": {},
            },
        },
        "audit_summary": {
            "candidate_count": len(candidates),
            "missing_pdf_url_count": 0,
            "planned_count": 0,
            "downloaded_count": 0,
            "rejected_count": 0,
            "rejected_by_reason": {},
            "failed_count": 0,
            "failed_by_reason": {},
            "extracted_count": 0,
            "extraction_failed_count": 0,
            "extraction_failed_by_reason": {},
            "full_text_available_count": 0,
            "full_text_reviewed_count": 0,
            "reviewed_from_pdf_extract_count": 0,
            "skipped_already_reviewed_count": 0,
            "skipped_existing_artifact_count": 0,
            "no_evidence_span_count": 0,
            "weak_evidence_span_count": 0,
            "high_failure_rate_warning": None,
        },
        "pdf_acquisition": {
            "mode": "disabled",
            "status": "disabled",
            "requested": False,
            "live_fetch_allowed": False,
            "live_fetch_blocked_count": 0,
            "live_fetch_gate": {},
            "planned_count": 0,
            "downloaded_count": 0,
            "extracted_count": 0,
            "rejected_count": 0,
            "failed_count": 0,
            "extraction_failed_count": 0,
            "extracted_text_count": 0,
            "extracted_page_count": 0,
            "items": [],
            "rejected": [],
            "extracted_normalized_paper_ids": [],
        },
    }
    prioritization = _prioritize_full_text_candidates(
        topic_id=resolved_topic_id,
        candidates=candidates,
        provider=provider,
        allowed_domains=allow_domains,
        download_limit=download_limit,
        exclude_paper_ids=exclude_paper_ids,
        target_paper_id=target_paper_id,
    )
    summary["prioritization"] = {
        key: value for key, value in prioritization.items() if not key.startswith("_")
    }
    if fetch_missing_pdf:
        live_fetch_gate = pdf_acquisition.evaluate_bounded_pdf_fetch_gate(
            requested=True,
            dry_run=dry_run,
            allow_bounded_pdf_fetch=allow_bounded_pdf_fetch,
            env=env,
            download_limit=download_limit,
            allowed_domains=allow_domains,
            max_bytes=max_pdf_bytes,
            timeout_seconds=download_timeout,
        )
        summary["pdf_acquisition"] = pdf_acquisition.acquire_candidate_pdfs(
            prioritization.get("_acquisition_candidates", candidates),
            topic_id=resolved_topic_id,
            provider=provider,
            allowed_domains=allow_domains,
            download_limit=download_limit,
            dry_run=dry_run,
            extract_after_fetch=extract_after_fetch,
            max_bytes=max_pdf_bytes,
            timeout_seconds=download_timeout,
            max_text_chars=max_text_chars,
            allow_network_fetch=bool(live_fetch_gate["live_fetch_allowed"]),
            expected_pdf_sha256_by_paper_id=expected_pdf_sha256_by_paper_id,
            expected_source_text_sha256_by_paper_id=expected_source_text_sha256_by_paper_id,
        )
        summary["pdf_acquisition"]["live_fetch_gate"] = live_fetch_gate
        summary["pdf_acquisition"]["live_fetch_allowed"] = bool(live_fetch_gate["live_fetch_allowed"])
        summary["pdf_acquisition"].setdefault("status", summary["pdf_acquisition"].get("mode"))
        summary["pdf_acquisition"]["live_fetch_blocked_count"] = 0
        if not dry_run and not live_fetch_gate["live_fetch_allowed"]:
            planned_count = int(summary["pdf_acquisition"].get("planned_count") or 0)
            summary["pdf_acquisition"]["mode"] = "live_fetch_blocked"
            summary["pdf_acquisition"]["status"] = "live_fetch_blocked"
            summary["pdf_acquisition"]["live_fetch_blocked_count"] = planned_count
            summary["pdf_acquisition"]["note"] = (
                "Live PDF fetch was blocked by bounded-fetch gates; candidate selection was computed as a dry-run plan only."
            )
        rejected_by_reason = dict(
            (summary["pdf_acquisition"].get("audit_summary") or {}).get("rejected_by_reason") or {}
        )
        prioritization_rejections = []
        for candidate_preview in summary["prioritization"].get("candidates", []):
            skip_reason = str(candidate_preview.get("skip_reason") or "")
            if skip_reason not in {"missing_pdf_url", "domain_not_allowed", "provider_mismatch"}:
                continue
            rejected_by_reason[skip_reason] = int(rejected_by_reason.get(skip_reason) or 0) + 1
            prioritization_rejections.append(
                {
                    "paper_id": candidate_preview.get("paper_id"),
                    "source_url": research_transport.redact_retrieval_url(candidate_preview.get("pdf_url")),
                    "reason": skip_reason,
                    "source": "prioritization",
                }
            )
        if prioritization_rejections:
            summary["pdf_acquisition"].setdefault("rejected", []).extend(prioritization_rejections)
            summary["pdf_acquisition"]["rejected_count"] = int(summary["pdf_acquisition"].get("rejected_count") or 0) + len(
                prioritization_rejections
            )
            summary["pdf_acquisition"].setdefault("audit_summary", {})["rejected_count"] = summary["pdf_acquisition"]["rejected_count"]
            summary["pdf_acquisition"]["audit_summary"]["rejected_by_reason"] = dict(sorted(rejected_by_reason.items()))
            summary["pdf_acquisition"]["audit_summary"]["missing_pdf_url_count"] = rejected_by_reason.get("missing_pdf_url", 0)
        summary["pdf_acquisition"].setdefault("audit_summary", {})["candidate_count"] = len(candidates)
        summary["pdf_acquisition"]["audit_summary"]["skipped_existing_artifact_count"] = max(
            int(summary["pdf_acquisition"]["audit_summary"].get("skipped_existing_artifact_count") or 0),
            int(summary["prioritization"].get("summary", {}).get("already_downloaded_or_extracted_count") or 0),
        )
        summary["pdf_planned_count"] = int(summary["pdf_acquisition"].get("planned_count") or 0)
        summary["pdf_downloaded_count"] = int(summary["pdf_acquisition"].get("downloaded_count") or 0)
        summary["pdf_rejected_count"] = int(summary["pdf_acquisition"].get("rejected_count") or 0)
        summary["pdf_failed_count"] = int(summary["pdf_acquisition"].get("failed_count") or 0)
        summary["pdf_extracted_count"] = int(summary["pdf_acquisition"].get("extracted_count") or 0)
        summary["pdf_extraction_failed_count"] = int(summary["pdf_acquisition"].get("extraction_failed_count") or 0)
        summary["extracted_text_count"] = int(summary["pdf_acquisition"].get("extracted_text_count") or 0)
        summary["extracted_page_count"] = int(summary["pdf_acquisition"].get("extracted_page_count") or 0)
        summary["audit_summary"].update(summary["pdf_acquisition"].get("audit_summary") or {})
    pdf_extracted_normalized = {
        str(value or "")
        for value in summary["pdf_acquisition"].get("extracted_normalized_paper_ids", [])
        if str(value or "")
    }
    source_run_ids: list[str] = []
    existing_by_normalized = _existing_reviews_by_normalized_paper_id(resolved_topic_id)

    for candidate in candidates:
        candidate_normalized_id = str(candidate.get("normalized_paper_id") or "")
        if target_normalized_paper_id and candidate_normalized_id != target_normalized_paper_id:
            continue
        run = candidate["_run"]
        item = candidate["_item"]
        run_id = str(candidate.get("best_run_id") or run.get("run_id") or "").strip()
        for source_run_id in candidate.get("seen_run_ids", []):
            if source_run_id and source_run_id not in source_run_ids:
                source_run_ids.append(source_run_id)
        if run_id and run_id not in source_run_ids:
            source_run_ids.append(run_id)

        existing = existing_by_normalized.get(str(candidate.get("normalized_paper_id") or ""))
        generated = generate_deterministic_review(
            topic_profile=profile,
            run=run,
            item=item,
            use_full_text=use_full_text,
            max_text_chars=max_text_chars,
            prefer_existing_text=prefer_existing_text,
            extract_existing_pdf=extract_existing_pdf,
            text_source_run_id=resolved_text_source_run_id,
        )
        if existing is not None and _status_rank(existing) > _status_rank(generated):
            review = existing
            summary["unique_skipped_count"] += 1
            summary["raw_skipped_count"] += int(candidate.get("seen_count") or 1)
            summary["audit_summary"]["skipped_already_reviewed_count"] += 1
        elif existing is not None and not force and _review_is_at_least_as_complete(existing, generated):
            review = existing
            summary["unique_skipped_count"] += 1
            summary["raw_skipped_count"] += int(candidate.get("seen_count") or 1)
            summary["audit_summary"]["skipped_already_reviewed_count"] += 1
        else:
            review = generated if dry_run else paper_reviews.upsert_review(generated)
            summary["unique_reviewed_count"] += 1
            summary["raw_reviewed_count"] += int(candidate.get("seen_count") or 1)

        status = str(review.get("full_text_status") or "unavailable")
        if status == "abstract_only":
            summary["abstract_only_count"] += 1
        elif status == "unavailable":
            summary["unavailable_count"] += 1
        elif status == "full_text_available":
            summary["full_text_available_count"] += 1
        elif status == "reviewed":
            summary["full_text_reviewed_count"] += 1
            spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
            span_lengths = [len(str(span.get("text") or "").strip()) for span in spans if isinstance(span, dict)]
            if not review.get("source_text_sha256") or not span_lengths:
                summary["audit_summary"]["no_evidence_span_count"] += 1
            elif sum(span_lengths) / len(span_lengths) < pdf_acquisition.MIN_AVERAGE_EVIDENCE_SPAN_CHARS:
                summary["audit_summary"]["weak_evidence_span_count"] += 1
            artifact_path = str(review.get("full_text_artifact_path") or "")
            if candidate_normalized_id in pdf_extracted_normalized:
                summary["reviewed_from_pdf_extract_count"] += 1
            elif artifact_path.endswith(".pages.json"):
                summary["reviewed_from_pages_count"] += 1
            elif artifact_path.endswith(".txt"):
                summary["reviewed_from_text_count"] += 1
            elif artifact_path.endswith(".pdf"):
                summary["reviewed_from_pdf_extract_count"] += 1
        if status in {"abstract_only", "full_text_available", "reviewed"}:
            summary["report_eligible_unique_count"] += 1

    summary["source_run_ids"] = source_run_ids
    summary["audit_summary"]["full_text_available_count"] = summary["full_text_available_count"]
    summary["audit_summary"]["full_text_reviewed_count"] = summary["full_text_reviewed_count"]
    summary["audit_summary"]["reviewed_from_pdf_extract_count"] = summary["reviewed_from_pdf_extract_count"]
    failure_events = (
        int(summary["audit_summary"].get("rejected_count") or 0)
        + int(summary["audit_summary"].get("failed_count") or 0)
        + int(summary["audit_summary"].get("extraction_failed_count") or 0)
    )
    candidate_count = max(1, int(summary["audit_summary"].get("candidate_count") or len(candidates) or 1))
    if fetch_missing_pdf and failure_events and failure_events / candidate_count >= 0.5:
        summary["audit_summary"]["high_failure_rate_warning"] = (
            f"PDF acquisition/extraction warning: {failure_events}/{candidate_count} candidates were rejected, failed, or failed extraction."
        )
    summary["discovered_count"] = summary["unique_discovered_count"]
    summary["reviewed_count"] = summary["unique_reviewed_count"]
    summary["skipped_count"] = summary["unique_skipped_count"]
    summary["low_relevance_count"] = summary["low_relevance_unique_count"]
    if not dry_run:
        metadata_index.touch_index_state("last_paper_review_refresh_at")
    return summary

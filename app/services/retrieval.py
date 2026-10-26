from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from app.domain.constants import LEGACY_QUEUE_ROOT, LEGACY_RAW_ROOT
from app.repositories import artifact_refs
from app.repositories.files import append_jsonl, read_jsonl, write_jsonl
from app.services import research_transport

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
METADATA_UPSTREAM_HOSTS = frozenset(
    {
        "export.arxiv.org",
        "api.openalex.org",
        "api.crossref.org",
    }
)
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def arxiv_id_from_entry_id(entry_id: str) -> str:
    if not entry_id:
        return ""
    match = re.search(r"/abs/([^/]+)$", entry_id)
    return match.group(1) if match else entry_id


def fetch_atom(url: str, timeout: int = 30) -> str:
    client = _metadata_client(url, timeout=timeout)
    response = client.fetch_metadata(
        url,
        accept="application/atom+xml, application/xml",
    )
    return response.body.decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 30) -> dict[str, Any]:
    client = _metadata_client(url, timeout=timeout)
    response = client.fetch_metadata(url, accept="application/json")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise research_transport.RetrievalTransportError(
            "retrieval_payload_invalid",
            "Retrieval metadata payload was invalid.",
        ) from None
    if not isinstance(payload, dict):
        raise research_transport.RetrievalTransportError(
            "retrieval_payload_invalid",
            "Retrieval metadata payload was invalid.",
        )
    return payload


def _metadata_client(url: str, *, timeout: int) -> research_transport.ResearchHttpClient:
    try:
        hostname = research_transport.normalize_retrieval_hostname(
            str(urllib.parse.urlsplit(str(url or "")).hostname or "")
        )
    except (ValueError, research_transport.RetrievalTransportError):
        raise research_transport.RetrievalUrlRejected(
            "retrieval_url_rejected",
            "Metadata retrieval URL was rejected.",
        ) from None
    if hostname not in METADATA_UPSTREAM_HOSTS:
        raise research_transport.RetrievalUrlRejected(
            "retrieval_url_rejected",
            "Metadata retrieval destination is not approved.",
        )
    policy = research_transport.ResearchTransportPolicy.create(
        {hostname},
        total_timeout_seconds=float(timeout),
    )
    return research_transport.ResearchHttpClient(policy)


def _safe_failure(provider: str, exc: BaseException) -> dict[str, str]:
    safe_provider = str(provider or "").strip().lower()
    if safe_provider not in {"arxiv", "openalex", "crossref"}:
        safe_provider = "unsupported"
    if isinstance(exc, research_transport.RetrievalTransportError):
        code = exc.code
        message = exc.public_message
    elif isinstance(exc, (ET.ParseError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, KeyError)):
        code = "retrieval_payload_invalid"
        message = "Retrieval metadata payload was invalid."
    else:
        code = "retrieval_upstream_error"
        message = "Retrieval upstream failed safely."
    return {
        "provider": safe_provider,
        "query": "<redacted>",
        "error_code": code,
        "error": message,
    }


def _portable_output_ref(path: Path) -> str:
    normalized = artifact_refs.normalize_path_reference(str(path))
    if normalized and artifact_refs.is_portable_ref(normalized):
        return normalized
    return f"artifact:{path.name}"


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = normalize_ws(str(item or ""))
            if text:
                return text
        return ""
    return normalize_ws(str(value or ""))


def build_query(categories: list[str], query_text: str) -> str:
    parts: list[str] = []
    if categories:
        category_query = " OR ".join(f"cat:{category}" for category in categories)
        parts.append(f"({category_query})")
    if query_text:
        parts.append(f"({query_text})")
    return " AND ".join(parts) if parts else "all:electron"


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    if root.tag != f"{{{NS['atom']}}}feed":
        raise ValueError("invalid_atom_feed")
    entries: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=NS)
        paper_id = arxiv_id_from_entry_id(entry_id)
        title = normalize_ws(entry.findtext("atom:title", default="", namespaces=NS))
        summary = normalize_ws(entry.findtext("atom:summary", default="", namespaces=NS))
        published = normalize_ws(entry.findtext("atom:published", default="", namespaces=NS))
        updated = normalize_ws(entry.findtext("atom:updated", default="", namespaces=NS))

        authors: list[str] = []
        for author in entry.findall("atom:author", NS):
            name = normalize_ws(author.findtext("atom:name", default="", namespaces=NS))
            if name:
                authors.append(name)

        categories: list[str] = []
        for category in entry.findall("atom:category", NS):
            term = category.attrib.get("term", "")
            if term:
                categories.append(term)

        doi = ""
        doi_node = entry.find("arxiv:doi", NS)
        if doi_node is not None and doi_node.text:
            doi = normalize_ws(doi_node.text)

        entry_url = entry_id
        pdf_url = ""
        for link in entry.findall("atom:link", NS):
            rel = link.attrib.get("rel", "")
            href = link.attrib.get("href", "")
            link_type = link.attrib.get("type", "")
            title_attr = link.attrib.get("title", "")
            if rel == "alternate" and href:
                entry_url = href
            if (title_attr == "pdf" or link_type == "application/pdf") and href:
                pdf_url = href

        entries.append(
            {
                "source": "arxiv",
                "source_provider": "arxiv",
                "paper_id": paper_id,
                "entry_id": entry_id,
                "entry_url": entry_url,
                "pdf_url": pdf_url,
                "title": title,
                "abstract": summary,
                "authors": authors,
                "categories": categories,
                "published": published,
                "updated": updated,
                "doi": doi,
            }
        )
    return entries


def _openalex_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for token, indexes in inverted_index.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            try:
                positions.append((int(index), str(token)))
            except (TypeError, ValueError):
                continue
    return normalize_ws(" ".join(token for _, token in sorted(positions)))


def _openalex_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.rstrip("/").split("/")[-1] if "/" in text else text


def _openalex_location_url(work: dict[str, Any]) -> tuple[str, str]:
    primary = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    landing = str(primary.get("landing_page_url") or "").strip()
    pdf = str(primary.get("pdf_url") or "").strip()
    locations = work.get("locations") if isinstance(work.get("locations"), list) else []
    for location in locations:
        if not isinstance(location, dict):
            continue
        landing = landing or str(location.get("landing_page_url") or "").strip()
        pdf = pdf or str(location.get("pdf_url") or "").strip()
        if landing and pdf:
            break
    return landing or str(work.get("id") or "").strip(), pdf


def parse_openalex_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload.get("results"), list):
        raise ValueError("invalid_openalex_payload")
    entries: list[dict[str, Any]] = []
    for work in payload.get("results", []):
        if not isinstance(work, dict):
            continue
        work_id = _openalex_id(work.get("id"))
        doi = str(work.get("doi") or "").strip()
        paper_id = doi or work_id
        if not paper_id:
            continue
        authors: list[dict[str, Any]] = []
        author_names: list[str] = []
        institutions: list[str] = []
        for authorship in work.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
            author_name = normalize_ws(str(author.get("display_name") or ""))
            author_institutions: list[str] = []
            for institution in authorship.get("institutions") or []:
                if isinstance(institution, dict):
                    name = normalize_ws(str(institution.get("display_name") or ""))
                    if name:
                        institutions.append(name)
                        author_institutions.append(name)
            if author_name:
                author_names.append(author_name)
                authors.append({"name": author_name, "institutions": author_institutions})

        concepts = [
            normalize_ws(str(concept.get("display_name") or ""))
            for concept in (work.get("concepts") or [])
            if isinstance(concept, dict) and concept.get("display_name")
        ]
        entry_url, pdf_url = _openalex_location_url(work)
        entries.append(
            {
                "source": "openalex",
                "source_provider": "openalex",
                "paper_id": f"openalex:{paper_id}",
                "entry_id": str(work.get("id") or ""),
                "entry_url": entry_url,
                "pdf_url": pdf_url,
                "title": normalize_ws(str(work.get("display_name") or work.get("title") or "")),
                "abstract": _openalex_abstract(work.get("abstract_inverted_index")),
                "authors": authors or author_names,
                "institutions": sorted(set(institutions)),
                "categories": concepts[:10],
                "published": str(work.get("publication_date") or ""),
                "updated": str(work.get("updated_date") or ""),
                "doi": doi,
                "citations": int(work.get("cited_by_count") or 0),
                "openalex": {
                    "id": work.get("id"),
                    "cited_by_count": int(work.get("cited_by_count") or 0),
                    "publication_year": work.get("publication_year"),
                    "type": work.get("type"),
                },
            }
        )
    return entries


def _strip_markup(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_ws(unescape(text))


def _crossref_date(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    date_time = normalize_ws(str(value.get("date-time") or ""))
    if date_time:
        return date_time
    date_parts = value.get("date-parts")
    if not isinstance(date_parts, list) or not date_parts:
        return ""
    first = date_parts[0]
    if not isinstance(first, list) or not first:
        return ""
    try:
        year = int(first[0])
        month = int(first[1]) if len(first) > 1 else 1
        day = int(first[2]) if len(first) > 2 else 1
    except (TypeError, ValueError):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _crossref_published(work: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        published = _crossref_date(work.get(key))
        if published:
            return published
    return ""


def _crossref_pdf_url(work: dict[str, Any]) -> str:
    for link in work.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = normalize_ws(str(link.get("URL") or link.get("url") or ""))
        content_type = str(link.get("content-type") or "").lower()
        if url and ("pdf" in content_type or url.lower().endswith(".pdf")):
            return url
    return ""


def _crossref_author(author: dict[str, Any]) -> tuple[str, list[str]]:
    name = normalize_ws(str(author.get("name") or ""))
    if not name:
        given = normalize_ws(str(author.get("given") or ""))
        family = normalize_ws(str(author.get("family") or ""))
        name = normalize_ws(f"{given} {family}")
    institutions: list[str] = []
    for affiliation in author.get("affiliation") or []:
        if isinstance(affiliation, dict):
            institution = normalize_ws(str(affiliation.get("name") or ""))
            if institution:
                institutions.append(institution)
    return name, institutions


def parse_crossref_results(
    payload: dict[str, Any],
    *,
    fetched_at: str,
    query: str,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise ValueError("invalid_crossref_payload")
    results = message.get("items") if isinstance(message.get("items"), list) else []
    entries: list[dict[str, Any]] = []
    for work in results:
        if not isinstance(work, dict):
            continue
        doi = normalize_ws(str(work.get("DOI") or work.get("doi") or ""))
        if not doi:
            continue
        provider_record_id = doi
        paper_id = f"crossref:{doi.lower()}"
        entry_url = normalize_ws(str(work.get("URL") or "")) or f"https://doi.org/{doi}"
        authors: list[dict[str, Any]] = []
        institution_values: list[str] = []
        for author in work.get("author") or []:
            if not isinstance(author, dict):
                continue
            author_name, institutions = _crossref_author(author)
            if not author_name:
                continue
            authors.append({"name": author_name, "institutions": institutions})
            institution_values.extend(institutions)
        subjects = [_first_text(item) for item in work.get("subject") or []]
        containers = [_first_text(item) for item in work.get("container-title") or []]
        categories = [item for item in [*subjects, *containers] if item]
        citations = work.get("is-referenced-by-count")
        try:
            citation_count = int(citations or 0)
        except (TypeError, ValueError):
            citation_count = 0
        entries.append(
            {
                "source": "crossref",
                "source_provider": "crossref",
                "paper_id": paper_id,
                "provider_record_id": provider_record_id,
                "entry_id": provider_record_id,
                "entry_url": entry_url,
                "pdf_url": _crossref_pdf_url(work),
                "title": _first_text(work.get("title")),
                "abstract": _strip_markup(work.get("abstract")),
                "authors": authors,
                "institutions": sorted(set(institution_values)),
                "categories": categories[:10],
                "published": _crossref_published(work),
                "updated": _crossref_date(work.get("deposited")) or _crossref_date(work.get("indexed")),
                "doi": doi,
                "citations": citation_count,
                "fetched_at": fetched_at,
                "query": query,
                "query_profile": dict(profile),
                "raw_metadata_ref": f"crossref:{provider_record_id}",
                "crossref": {
                    "doi": doi,
                    "type": work.get("type"),
                    "publisher": work.get("publisher"),
                    "is_referenced_by_count": citation_count,
                },
            }
        )
    return entries


def _load_id_set(path: Path, key: str = "paper_id") -> set[str]:
    values: set[str] = set()
    for item in read_jsonl(path):
        value = item.get(key)
        if value:
            values.add(str(value))
    return values


def _annotate_keyword_hits(entries: list[dict[str, Any]], keywords: dict[str, Any]) -> None:
    primary = [item.lower() for item in keywords.get("primary", [])]
    secondary = [item.lower() for item in keywords.get("secondary", [])]
    for entry in entries:
        blob = f"{entry.get('title', '')} {entry.get('abstract', '')}".lower()
        entry["keyword_hits"] = {
            "primary": [item for item in primary if item in blob][:25],
            "secondary": [item for item in secondary if item in blob][:25],
        }
        entry["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()


def _mirror_legacy_outputs(entries: list[dict[str, Any]], date_tag: str) -> dict[str, str]:
    legacy_raw_path = LEGACY_RAW_ROOT / f"sources_{date_tag}.jsonl"
    legacy_inbox_path = LEGACY_QUEUE_ROOT / "inbox.jsonl"
    existing_ids = _load_id_set(legacy_inbox_path)
    fresh_entries = [entry for entry in entries if entry.get("paper_id") and entry["paper_id"] not in existing_ids]
    if fresh_entries:
        append_jsonl(legacy_raw_path, fresh_entries)
        append_jsonl(legacy_inbox_path, fresh_entries)
    return {
        "raw_file": _portable_output_ref(legacy_raw_path),
        "inbox_file": _portable_output_ref(legacy_inbox_path),
    }


def retrieve_from_profile(
    *,
    profile: dict[str, Any],
    raw_path: Path,
    inbox_path: Path,
    sleep_seconds: float = 3.0,
    mirror_legacy: bool = False,
) -> dict[str, Any]:
    sources = profile["sources"]
    keywords = profile["keywords"]

    existing_ids = _load_id_set(inbox_path)
    all_new: list[dict[str, Any]] = []
    failed_queries: list[dict[str, str]] = []
    providers_run: dict[str, int] = {}
    queries_attempted = 0

    provider_items = sorted(
        ((provider, config) for provider, config in sources.items() if isinstance(config, dict)),
        key=lambda item: (int(item[1].get("priority", 1) or 1), item[0]),
    )

    for provider, config in provider_items:
        queries = list(config.get("queries", []))
        if provider == "arxiv":
            categories = list(config.get("categories", []))
            max_results = int(config.get("max_results", 50))
            sort_by = config.get("sort_by", "submittedDate")
            sort_order = config.get("sort_order", "descending")
            for index, query in enumerate(queries):
                queries_attempted += 1
                search_query = build_query(categories, query)
                params = {
                    "search_query": search_query,
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": sort_by,
                    "sortOrder": sort_order,
                }
                url = ARXIV_API + "?" + urllib.parse.urlencode(params)
                try:
                    xml_text = fetch_atom(url)
                    entries = parse_feed(xml_text)
                except Exception as exc:  # pragma: no cover
                    failed_queries.append(_safe_failure(provider, exc))
                    continue

                _annotate_keyword_hits(entries, keywords)
                new_entries = [entry for entry in entries if entry.get("paper_id") and entry["paper_id"] not in existing_ids]
                for entry in new_entries:
                    existing_ids.add(entry["paper_id"])
                all_new.extend(new_entries)
                providers_run[provider] = providers_run.get(provider, 0) + 1

                if index < len(queries) - 1 and sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            continue

        if provider == "openalex":
            max_results = int(config.get("max_results", 25))
            sort_by = str(config.get("sort_by") or "relevance")
            sort_order = str(config.get("sort_order") or "descending")
            openalex_sort = {
                "publication_date": "publication_date",
                "cited_by_count": "cited_by_count",
            }.get(sort_by)
            if openalex_sort and sort_order == "descending":
                openalex_sort = f"{openalex_sort}:desc"
            elif openalex_sort and sort_order == "ascending":
                openalex_sort = f"{openalex_sort}:asc"
            for index, query in enumerate(queries):
                queries_attempted += 1
                params: dict[str, Any] = {
                    "search": query,
                    "per-page": max(1, min(200, max_results)),
                }
                if openalex_sort:
                    params["sort"] = openalex_sort
                contact_email = os.environ.get("OPENALEX_CONTACT_EMAIL")
                if contact_email:
                    params["mailto"] = contact_email
                url = OPENALEX_API + "?" + urllib.parse.urlencode(params)
                try:
                    payload = fetch_json(url)
                    entries = parse_openalex_results(payload)
                except Exception as exc:  # pragma: no cover
                    failed_queries.append(_safe_failure(provider, exc))
                    continue

                _annotate_keyword_hits(entries, keywords)
                new_entries = [entry for entry in entries if entry.get("paper_id") and entry["paper_id"] not in existing_ids]
                for entry in new_entries:
                    existing_ids.add(entry["paper_id"])
                all_new.extend(new_entries)
                providers_run[provider] = providers_run.get(provider, 0) + 1

                if index < len(queries) - 1 and sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            continue

        if provider == "crossref":
            max_results = int(config.get("max_results", 25))
            sort_by = str(config.get("sort_by") or "relevance")
            sort_order = str(config.get("sort_order") or "descending")
            crossref_sort = {
                "published": "published",
                "deposited": "deposited",
                "is-referenced-by-count": "is-referenced-by-count",
            }.get(sort_by)
            for index, query in enumerate(queries):
                queries_attempted += 1
                params: dict[str, Any] = {
                    "query": query,
                    "rows": max(1, min(200, max_results)),
                }
                if crossref_sort:
                    params["sort"] = crossref_sort
                    params["order"] = "asc" if sort_order == "ascending" else "desc"
                contact_email = os.environ.get("CROSSREF_CONTACT_EMAIL") or os.environ.get("OPENALEX_CONTACT_EMAIL") or os.environ.get("ARXIV_CONTACT_EMAIL")
                if contact_email:
                    params["mailto"] = contact_email
                url = CROSSREF_API + "?" + urllib.parse.urlencode(params)
                query_profile = {
                    "max_results": max_results,
                    "sort_by": sort_by,
                    "sort_order": sort_order,
                    "priority": config.get("priority"),
                }
                try:
                    fetched_at = datetime.now(timezone.utc).isoformat()
                    payload = fetch_json(url)
                    entries = parse_crossref_results(payload, fetched_at=fetched_at, query=query, profile=query_profile)
                except Exception as exc:  # pragma: no cover
                    failed_queries.append(_safe_failure(provider, exc))
                    continue

                _annotate_keyword_hits(entries, keywords)
                new_entries = [entry for entry in entries if entry.get("paper_id") and entry["paper_id"] not in existing_ids]
                for entry in new_entries:
                    existing_ids.add(entry["paper_id"])
                all_new.extend(new_entries)
                providers_run[provider] = providers_run.get(provider, 0) + 1

                if index < len(queries) - 1 and sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            continue

        queries_attempted += 1
        failed_queries.append(
            {
                "provider": "unsupported",
                "query": "<redacted>",
                "error_code": "retrieval_provider_unsupported",
                "error": "Retrieval metadata provider is not supported.",
            }
        )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in all_new:
        paper_id = entry.get("paper_id")
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        deduped.append(entry)

    write_jsonl(raw_path, deduped)
    write_jsonl(inbox_path, deduped)

    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    legacy_paths: dict[str, str] = {}
    legacy_mirror_errors: list[str] = []
    if mirror_legacy:
        try:
            legacy_paths = _mirror_legacy_outputs(deduped, date_tag)
        except OSError:
            legacy_mirror_errors.append("retrieval_artifact_mirror_failed")

    successful_queries = sum(providers_run.values())
    status = "ok"
    if failed_queries:
        status = "failed" if queries_attempted > 0 and successful_queries == 0 else "partial"
    return {
        "status": status,
        "new_papers": len(deduped),
        "raw_file": _portable_output_ref(raw_path),
        "inbox_file": _portable_output_ref(inbox_path),
        "failed_queries": failed_queries,
        "queries_run": successful_queries,
        "queries_attempted": queries_attempted,
        "providers_run": providers_run,
        "legacy_paths": legacy_paths,
        "legacy_mirror_errors": legacy_mirror_errors,
    }


def retrieve_from_files(
    *,
    config_path: Path,
    keywords_path: Path,
    raw_path: Path,
    inbox_path: Path,
    sleep_seconds: float = 3.0,
    mirror_legacy: bool = False,
) -> dict[str, Any]:
    profile = {
        "topic": "XR/AR × ML (Image/Video)",
        "sources": _load_json(config_path),
        "keywords": _load_json(keywords_path),
    }
    return retrieve_from_profile(
        profile=profile,
        raw_path=raw_path,
        inbox_path=inbox_path,
        sleep_seconds=sleep_seconds,
        mirror_legacy=mirror_legacy,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/sources.json")
    parser.add_argument("--keywords", default="config/keywords.json")
    parser.add_argument("--sleep", type=float, default=3.0, help="seconds between API calls")
    args = parser.parse_args(argv)

    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    result = retrieve_from_files(
        config_path=Path(args.config),
        keywords_path=Path(args.keywords),
        raw_path=LEGACY_RAW_ROOT / f"arxiv_{date_tag}.jsonl",
        inbox_path=LEGACY_QUEUE_ROOT / "inbox.jsonl",
        sleep_seconds=args.sleep,
        mirror_legacy=False,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

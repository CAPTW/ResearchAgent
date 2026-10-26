"""Bounded PMC Open Access ingestion for the isolated Prototype v0.3 lane.

The module deliberately keeps the network, rights, full-text, review, evidence,
report, and replay boundaries together.  It is not a general scholarly search
client: the topic, query, hosts, licenses, candidate count, and output surface
are fixed by the Prototype contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.domain.constants import PROJECT_ROOT
from app.repositories.files import iso_now_utc, safe_filename
from app.repositories.runs import build_run_paths, create_run_record, list_run_records, load_run_record, save_run_record
from app.services import paper_occurrences, prototype, reporting
from app.services.research_transport import (
    MetadataFetchResult,
    ResearchHttpClient,
    ResearchTransportPolicy,
    RetrievalTransportError,
    RetrievalUrlRejected,
    validate_retrieval_url,
)

REAL_CORPUS_SCHEMA = "prototype_real_open_access_corpus_v1"
PUBLIC_RECORD_SCHEMA = "prototype_real_corpus_public_record_v1"
RIGHTS_SCHEMA = "prototype_article_rights_v1"
ARTIFACT_SCHEMA = "prototype_full_text_artifact_v1"
PARSED_SCHEMA = "prototype_jats_document_v1"
REVIEW_SCHEMA = "prototype_real_full_text_review_v1"
CACHE_SCHEMA = "prototype_real_corpus_cache_v1"
REPLAY_SCHEMA = "prototype_real_corpus_replay_v1"

TOPIC = "Virtual and augmented reality interventions for rehabilitation and clinical training"
QUERY_ID = "pmc-xr-rehabilitation-clinical-training-v1"
QUERY_CUTOFF = "2025-12-31"
QUERY = (
    '(("virtual reality"[Title/Abstract] OR "augmented reality"[Title/Abstract]) '
    'AND (rehabilitation[Title/Abstract] OR "clinical training"[Title/Abstract] '
    'OR "medical education"[Title/Abstract]) AND "open access"[filter] '
    'AND english[lang] AND ("1900/01/01"[PDAT] : "2025/12/31"[PDAT]) '
    'AND (journal article[pt] OR review[pt]))'
)
QUERY_SHA256 = hashlib.sha256(QUERY.encode("utf-8")).hexdigest()
SELECTION_ALGORITHM_VERSION = "pmc-open-access-lexical-selection-v1"
RIGHTS_POLICY_VERSION = "prototype-cc0-cc-by-only-v1"
PARSER_VERSION = "bounded-jats-body-parser-v1"
REVIEW_METHOD = "DETERMINISTIC_REAL_FULL_TEXT_PROTOTYPE"
SOURCE = "PMC_OPEN_ACCESS_SUBSET"

EUTILS_HOST = "eutils.ncbi.nlm.nih.gov"
PMC_HOST = "pmc.ncbi.nlm.nih.gov"
ALLOWED_HOSTS = frozenset({EUTILS_HOST, PMC_HOST})
ESEARCH_ENDPOINT = f"https://{EUTILS_HOST}/entrez/eutils/esearch.fcgi"
ESUMMARY_ENDPOINT = f"https://{EUTILS_HOST}/entrez/eutils/esummary.fcgi"
PMC_OAI_ENDPOINT = f"https://{PMC_HOST}/api/oai/v1/mh/"

MAX_HTTP_REQUESTS = 40
MAX_CANDIDATES = 20
MAX_ACCEPTED_ARTICLES = 4
MIN_PASS_ARTICLES = 3
MAX_METADATA_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FULL_TEXT_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_DOWNLOADED_BYTES = 28 * 1024 * 1024
REQUEST_INTERVAL_SECONDS = 0.35
NETWORK_TIMEOUT_SECONDS = 30.0
MAX_EVIDENCE_EXCERPT_CHARS = 420
MIN_USABLE_PARAGRAPHS = 3
MIN_USABLE_BODY_CHARS = 500

REPORT_FILE_NAME = "real-corpus-weekly-journal.md"
PUBLIC_RECORD_FILE_NAME = "real-corpus-record.json"
MANIFEST_FILE_NAME = "real-corpus-manifest.json"

PROGRESS_STAGES = (
    "discovery",
    "rights_verification",
    "acquisition",
    "parsing",
    "review",
    "evidence_extraction",
    "report_generation",
    "export_ready",
)

_LICENSE_URL_RE = re.compile(r"https?://creativecommons\.org/(?:licenses|publicdomain)/[^\s<>\"]+", re.I)
_DATE_RE = re.compile(r"(?P<year>19\d{2}|20\d{2})(?:[-/ ](?P<month>\d{1,2}|[A-Za-z]{3,9}))?(?:[-/ ](?P<day>\d{1,2}))?")
_CORRECTION_TERMS = (
    "retraction",
    "retracted article",
    "expression of concern",
    "correction to",
    "corrigendum",
    "erratum",
)
_EXCLUDED_BODY_TAGS = {
    "ack",
    "app-group",
    "author-notes",
    "fn-group",
    "funding-group",
    "glossary",
    "notes",
    "permissions",
    "ref-list",
}
_EXCLUDED_SECTION_TERMS = (
    "acknowledg",
    "author contribution",
    "conflict of interest",
    "competing interest",
    "funding",
    "reference",
)
_INTERVENTION_TERMS = (
    "virtual reality",
    "augmented reality",
    "immersive",
    "intervention",
    "training",
    "rehabilitation",
    "protocol",
    "participants",
    "randomized",
)
_FINDING_TERMS = (
    "result",
    "improved",
    "increase",
    "decrease",
    "significant",
    "outcome",
    "effect",
    "associated",
    "performance",
)
_LIMITATION_TERMS = (
    "limitation",
    "however",
    "uncertain",
    "caution",
    "small sample",
    "may not",
    "cannot",
    "future research",
    "further study",
    "mixed",
    "no significant",
)


class RealCorpusError(RuntimeError):
    """Stable public failure category; never carry response bodies or paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _with_record_digest(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("record_digest", None)
    payload["record_digest"] = _sha256_bytes(_canonical_bytes(payload))
    return payload


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _normalized_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _element_text(element: ET.Element | None) -> str:
    return _normalized_text(" ".join(element.itertext())) if element is not None else ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _first_descendant(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element.iter() if _local_name(item.tag) == name), None)


def _descendant_texts(element: ET.Element, name: str) -> list[str]:
    return [text for item in element.iter() if _local_name(item.tag) == name and (text := _element_text(item))]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_disposable_target(target: Path, run_id: str) -> Path:
    run_root = build_run_paths(run_id).run_dir.resolve()
    resolved = target.resolve(strict=False)
    if not _is_within(resolved, run_root):
        raise RealCorpusError("real_corpus_artifact_path_rejected")
    project_root = PROJECT_ROOT.resolve()
    if _is_within(resolved, project_root) or _is_within(project_root, resolved):
        raise RealCorpusError("real_corpus_repository_path_rejected")
    return resolved


def _atomic_write_bytes(path: Path, content: bytes, *, run_id: str) -> None:
    target = _require_disposable_target(path, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(target.parent), suffix=".part") as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Any, *, run_id: str) -> None:
    _atomic_write_bytes(path, _canonical_bytes(payload) + b"\n", run_id=run_id)


def _portable_ref(run_id: str, path: Path) -> str:
    relative = _require_disposable_target(path, run_id).relative_to(build_run_paths(run_id).run_dir.resolve())
    return f"run:{relative.as_posix()}"


def _resolve_portable_ref(run_id: str, reference: str) -> Path:
    if not str(reference).startswith("run:"):
        raise RealCorpusError("real_corpus_cache_reference_invalid")
    relative = str(reference)[4:]
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise RealCorpusError("real_corpus_cache_reference_invalid")
    return _require_disposable_target(build_run_paths(run_id).run_dir / Path(relative), run_id)


def network_enabled() -> bool:
    return os.getenv("XRML_PROTOTYPE_REAL_CORPUS_NETWORK", "").strip() == "1"


def _validate_endpoint(url: str, policy: ResearchTransportPolicy) -> str:
    validated = validate_retrieval_url(url, policy)
    parsed = urllib.parse.urlsplit(validated.url)
    if validated.hostname == EUTILS_HOST:
        if parsed.path not in {"/entrez/eutils/esearch.fcgi", "/entrez/eutils/esummary.fcgi"}:
            raise RetrievalUrlRejected("retrieval_url_rejected", "NCBI E-utilities path is not allowlisted.")
    elif validated.hostname == PMC_HOST:
        if parsed.path != "/api/oai/v1/mh/":
            raise RetrievalUrlRejected("retrieval_url_rejected", "PMC OAI-PMH path is not allowlisted.")
    else:  # pragma: no cover - validate_retrieval_url already enforces this.
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval destination is not allowlisted.")
    return validated.url


class PmcNetworkClient:
    """Sequential, no-retry client with a safe request ledger."""

    def __init__(
        self,
        http_client: ResearchHttpClient | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = ResearchTransportPolicy.create(
            ALLOWED_HOSTS,
            total_timeout_seconds=NETWORK_TIMEOUT_SECONDS,
            max_attempts=1,
            max_redirects=0,
            initial_backoff_seconds=0,
        )
        self.http_client = http_client or ResearchHttpClient(self.policy)
        self.sleeper = sleeper
        self.clock = clock
        self.request_records: list[dict[str, Any]] = []
        self.total_downloaded_bytes = 0
        self._last_request_at: float | None = None

    @property
    def request_count(self) -> int:
        return len(self.request_records)

    def fetch(self, url: str, *, purpose: str, max_bytes: int, accept: str) -> MetadataFetchResult:
        canonical_url = _validate_endpoint(url, self.policy)
        if self.request_count >= MAX_HTTP_REQUESTS:
            raise RealCorpusError("real_corpus_http_request_limit")
        now = self.clock()
        if self._last_request_at is not None:
            remaining = REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
        parsed = urllib.parse.urlsplit(canonical_url)
        endpoint_id = "NCBI_EUTILS" if parsed.hostname == EUTILS_HOST else "PMC_OAI_V1"
        started_at = iso_now_utc()
        record: dict[str, Any] = {
            "request_number": self.request_count + 1,
            "purpose": purpose,
            "endpoint_id": endpoint_id,
            "status": "attempted",
            "status_code": None,
            "byte_count": 0,
            "response_sha256": None,
            "started_at": started_at,
        }
        self.request_records.append(record)
        self._last_request_at = self.clock()
        try:
            result = self.http_client.fetch_metadata(canonical_url, accept=accept, max_bytes=max_bytes)
        except RetrievalTransportError as exc:
            record.update({"status": "failed", "error_category": exc.code})
            raise
        body_bytes = len(result.body)
        if self.total_downloaded_bytes + body_bytes > MAX_TOTAL_DOWNLOADED_BYTES:
            record.update({"status": "rejected", "error_category": "real_corpus_total_download_limit"})
            raise RealCorpusError("real_corpus_total_download_limit")
        self.total_downloaded_bytes += body_bytes
        record.update(
            {
                "status": "completed",
                "status_code": result.status_code,
                "byte_count": body_bytes,
                "response_sha256": _sha256_bytes(result.body),
            }
        )
        return result


def _query_url(endpoint: str, params: dict[str, str | int]) -> str:
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


def _parse_json(body: bytes, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RealCorpusError(code) from None
    if not isinstance(payload, dict):
        raise RealCorpusError(code)
    return payload


def parse_esearch(body: bytes) -> tuple[int, list[str]]:
    payload = _parse_json(body, "real_corpus_discovery_payload_invalid")
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise RealCorpusError("real_corpus_discovery_payload_invalid")
    ids = [str(value) for value in result.get("idlist", []) if str(value).isdigit()]
    ids = ids[:MAX_CANDIDATES]
    try:
        total = int(result.get("count") or len(ids))
    except (TypeError, ValueError):
        total = len(ids)
    return total, ids


def _month_number(value: str | None) -> int:
    if not value:
        return 1
    if str(value).isdigit():
        return max(1, min(12, int(value)))
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return months.get(str(value).lower()[:3], 1)


def normalize_publication_date(value: str) -> str:
    match = _DATE_RE.search(str(value or ""))
    if not match:
        return "1900-01-01"
    year = int(match.group("year"))
    month = _month_number(match.group("month"))
    raw_day = match.group("day")
    day = max(1, min(28, int(raw_day))) if raw_day and raw_day.isdigit() else 1
    return date(year, month, day).isoformat()


def _article_ids(summary: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in summary.get("articleids", []) or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("idtype") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if key and value:
            values[key] = value
    return values


def normalize_pmcid(value: str) -> str:
    raw = str(value or "").strip().upper()
    digits = re.sub(r"\D", "", raw)
    return f"PMC{digits}" if digits else ""


def _candidate_relevance(candidate: dict[str, Any]) -> int:
    blob = f"{candidate.get('title', '')} {candidate.get('article_type', '')}".lower()
    score = 0
    for term, points in (
        ("virtual reality", 8),
        ("augmented reality", 8),
        ("rehabilitation", 6),
        ("clinical training", 6),
        ("medical education", 6),
        ("therapy", 3),
        ("patient", 2),
        ("randomized", 2),
    ):
        if term in blob:
            score += points
    return score


def parse_esummary(body: bytes, expected_ids: list[str]) -> list[dict[str, Any]]:
    payload = _parse_json(body, "real_corpus_summary_payload_invalid")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RealCorpusError("real_corpus_summary_payload_invalid")
    candidates: list[dict[str, Any]] = []
    for uid in expected_ids[:MAX_CANDIDATES]:
        summary = result.get(uid)
        if not isinstance(summary, dict):
            continue
        identifiers = _article_ids(summary)
        pmcid = normalize_pmcid(identifiers.get("pmcid") or uid)
        if not pmcid:
            continue
        authors = [str(item.get("name") or "") for item in summary.get("authors", []) if isinstance(item, dict)]
        languages = [str(item).lower() for item in summary.get("lang", []) or []]
        candidate = {
            "uid": uid,
            "pmcid": pmcid,
            "pmid": identifiers.get("pmid") or None,
            "doi": identifiers.get("doi") or None,
            "title": _normalized_text(str(summary.get("title") or "")),
            "authors": [author for author in authors if author],
            "journal": _normalized_text(str(summary.get("fulljournalname") or "")),
            "publication_date": normalize_publication_date(str(summary.get("pubdate") or summary.get("epubdate") or "")),
            "article_type": _normalized_text(str(summary.get("doctype") or "journal article")),
            "language": "English" if not languages or any(value in {"eng", "english"} for value in languages) else ",".join(languages),
        }
        candidate["relevance_score"] = _candidate_relevance(candidate)
        candidates.append(candidate)
    return candidates


def _parse_xml(body: bytes, code: str) -> ET.Element:
    if not body.strip() or not body.rstrip().endswith(b">"):
        raise RealCorpusError(code)
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise RealCorpusError(code) from None


def _oai_error(root: ET.Element) -> str | None:
    node = next((item for item in root.iter() if _local_name(item.tag) == "error"), None)
    return str(node.attrib.get("code") or "oai_error") if node is not None else None


def classify_license(values: list[str]) -> dict[str, Any]:
    normalized = [_normalized_text(value) for value in values if _normalized_text(value)]
    statement = " | ".join(sorted(dict.fromkeys(normalized)))
    lowered = statement.lower()
    urls = [match.group(0).rstrip(".,);]") for match in _LICENSE_URL_RE.finditer(statement)]
    disallowed = any(
        marker in lowered
        for marker in (
            "/by-nc",
            "/by-nd",
            "/by-sa",
            "cc by-nc",
            "cc by-nd",
            "cc by-sa",
            "noncommercial",
            "no derivatives",
            "sharealike",
        )
    )
    license_type: str | None = None
    canonical_url: str | None = None
    if not disallowed and (
        "creativecommons.org/publicdomain/zero" in lowered or re.search(r"\bcc0\b", lowered)
    ):
        license_type = "CC0"
        canonical_url = next((url for url in urls if "/publicdomain/zero/" in url.lower()), "https://creativecommons.org/publicdomain/zero/1.0/")
    elif not disallowed and (
        re.search(r"creativecommons\.org/licenses/by/(?:[0-9.]+/?)?", lowered)
        or re.search(r"\bcc by(?:\s+[1-9](?:\.\d)?)?\b", lowered)
        or "creative commons attribution" in lowered
    ):
        license_type = "CC BY"
        canonical_url = next((url for url in urls if "/licenses/by/" in url.lower()), "https://creativecommons.org/licenses/by/4.0/")
    rejection = None
    if disallowed:
        rejection = "license_not_in_narrow_allowlist"
    elif not statement:
        rejection = "license_missing"
    elif license_type is None:
        rejection = "license_ambiguous_or_custom"
    return {
        "license_type": license_type,
        "canonical_license_url": canonical_url,
        "license_statement_digest": _sha256_text(statement),
        "reuse_allowed": license_type in {"CC0", "CC BY"},
        "acceptance_reason": "narrow_allowlist_match" if license_type else None,
        "rejection_reason": rejection,
    }


def parse_oai_dc(body: bytes, candidate: dict[str, Any], *, retrieved_at: str) -> dict[str, Any]:
    root = _parse_xml(body, "real_corpus_rights_xml_invalid")
    if _oai_error(root):
        raise RealCorpusError("real_corpus_rights_record_unavailable")
    rights_values = _descendant_texts(root, "rights")
    identity_values = _descendant_texts(root, "identifier")
    title_values = _descendant_texts(root, "title")
    identity_blob = " ".join(identity_values).upper()
    if candidate["pmcid"] not in identity_blob and str(candidate["uid"]) not in identity_blob:
        raise RealCorpusError("real_corpus_rights_identity_mismatch")
    classification = classify_license(rights_values)
    metadata_basis = {
        "pmcid": candidate["pmcid"],
        "pmid": candidate.get("pmid"),
        "doi": candidate.get("doi"),
        "title": title_values[0] if title_values else candidate.get("title"),
        "rights_digests": sorted(_sha256_text(value) for value in rights_values),
    }
    return _with_record_digest(
        {
            "schema_version": RIGHTS_SCHEMA,
            "rights_record_id": f"rights-{_sha256_bytes(_canonical_bytes(metadata_basis))[:16]}",
            "pmcid": candidate["pmcid"],
            "pmid": candidate.get("pmid"),
            "doi": candidate.get("doi"),
            "source": SOURCE,
            **classification,
            "rights_metadata_source": "PMC_OAI_V1_OAI_DC",
            "rights_retrieval_timestamp": retrieved_at,
            "acceptance_policy_version": RIGHTS_POLICY_VERSION,
            "article_content_digest": None,
            "metadata_digest": _sha256_bytes(_canonical_bytes(metadata_basis)),
        }
    )


def _front_metadata(article: ET.Element) -> dict[str, Any]:
    article_meta = _first_descendant(article, "article-meta")
    if article_meta is None:
        article_meta = article
    identifiers: dict[str, str] = {}
    for node in article_meta.iter():
        if _local_name(node.tag) != "article-id":
            continue
        kind = str(node.attrib.get("pub-id-type") or "").lower()
        value = _element_text(node)
        if kind and value:
            identifiers[kind] = value
    title_node = _first_descendant(article_meta, "article-title")
    journal_node = _first_descendant(article, "journal-title")
    authors: list[str] = []
    for contrib in article_meta.iter():
        if _local_name(contrib.tag) != "contrib" or str(contrib.attrib.get("contrib-type") or "author") != "author":
            continue
        surname = _element_text(_first_descendant(contrib, "surname"))
        given = _element_text(_first_descendant(contrib, "given-names"))
        collective = _element_text(_first_descendant(contrib, "collab"))
        name = _normalized_text(" ".join(part for part in (given, surname) if part)) or collective
        if name:
            authors.append(name)
    publication_date = "1900-01-01"
    for pub_date in article_meta.iter():
        if _local_name(pub_date.tag) != "pub-date":
            continue
        year = _element_text(_first_descendant(pub_date, "year"))
        if year:
            month = _element_text(_first_descendant(pub_date, "month")) or "1"
            day = _element_text(_first_descendant(pub_date, "day")) or "1"
            publication_date = normalize_publication_date(f"{year}-{month}-{day}")
            break
    abstract_node = _first_descendant(article_meta, "abstract")
    return {
        "pmcid": normalize_pmcid(identifiers.get("pmc") or identifiers.get("pmcid") or ""),
        "pmid": identifiers.get("pmid") or None,
        "doi": identifiers.get("doi") or None,
        "title": _element_text(title_node),
        "journal": _element_text(journal_node),
        "authors": authors,
        "publication_date": publication_date,
        "abstract": _element_text(abstract_node),
    }


def _license_values_from_article(article: ET.Element) -> list[str]:
    values: list[str] = []
    for node in article.iter():
        if _local_name(node.tag) != "license":
            continue
        text = _element_text(node)
        href = next((str(value) for key, value in node.attrib.items() if _local_name(key) == "href"), "")
        if href:
            values.append(href)
        if text:
            values.append(text)
    return values


def _article_is_excluded(article: ET.Element, metadata: dict[str, Any]) -> bool:
    article_type = str(article.attrib.get("article-type") or "").lower()
    title = str(metadata.get("title") or "").lower()
    related_types = " ".join(
        str(node.attrib.get("related-article-type") or "").lower()
        for node in article.iter()
        if _local_name(node.tag) == "related-article"
    )
    return any(term in f"{article_type} {title} {related_types}" for term in _CORRECTION_TERMS)


def _section_title(node: ET.Element) -> str:
    return next((_element_text(child) for child in list(node) if _local_name(child.tag) == "title"), "")


def _parse_body(article: ET.Element, pmcid: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    body = _first_descendant(article, "body")
    if body is None:
        return [], []
    sections: dict[str, dict[str, Any]] = {}
    paragraphs: list[dict[str, Any]] = []

    def walk(node: ET.Element, path: tuple[str, ...]) -> None:
        tag = _local_name(node.tag)
        if tag in _EXCLUDED_BODY_TAGS:
            return
        current_path = path
        if tag == "sec":
            title = _section_title(node) or f"Untitled section {len(sections) + 1}"
            if any(term in title.lower() for term in _EXCLUDED_SECTION_TERMS):
                return
            current_path = (*path, title)
            section_path = " / ".join(current_path)
            section_id = f"sec-{_sha256_text(f'{pmcid}|{section_path}')[:16]}"
            sections.setdefault(
                section_id,
                {
                    "section_id": section_id,
                    "title": title,
                    "section_path": list(current_path),
                    "hierarchy_depth": len(current_path),
                },
            )
        if tag == "p":
            text = _element_text(node)
            if len(text) >= 40:
                effective_path = current_path or ("Article body",)
                section_path = " / ".join(effective_path)
                section_id = f"sec-{_sha256_text(f'{pmcid}|{section_path}')[:16]}"
                sections.setdefault(
                    section_id,
                    {
                        "section_id": section_id,
                        "title": effective_path[-1],
                        "section_path": list(effective_path),
                        "hierarchy_depth": len(effective_path),
                    },
                )
                ordinal = len(paragraphs) + 1
                digest = _sha256_text(text)
                paragraphs.append(
                    {
                        "paragraph_id": f"para-{_sha256_text(f'{pmcid}|{section_path}|{ordinal}|{digest}')[:20]}",
                        "section_id": section_id,
                        "section_path": list(effective_path),
                        "paragraph_ordinal": ordinal,
                        "normalized_paragraph_digest": digest,
                        "text": text,
                    }
                )
            return
        for child in list(node):
            if _local_name(child.tag) == "title":
                continue
            walk(child, current_path)

    walk(body, ())
    return list(sections.values()), paragraphs


def parse_jats_record(body: bytes, candidate: dict[str, Any], rights: dict[str, Any]) -> dict[str, Any]:
    root = _parse_xml(body, "real_corpus_full_text_xml_invalid")
    if _oai_error(root):
        raise RealCorpusError("real_corpus_full_text_unavailable")
    article = next((item for item in root.iter() if _local_name(item.tag) == "article"), None)
    if article is None:
        raise RealCorpusError("real_corpus_full_text_article_missing")
    metadata = _front_metadata(article)
    if metadata["pmcid"] != candidate["pmcid"]:
        raise RealCorpusError("real_corpus_full_text_identity_mismatch")
    jats_license = classify_license(_license_values_from_article(article))
    if not jats_license["reuse_allowed"]:
        raise RealCorpusError("real_corpus_full_text_license_rejected")
    if jats_license["license_type"] != rights["license_type"]:
        raise RealCorpusError("real_corpus_full_text_rights_mismatch")
    if _article_is_excluded(article, metadata):
        raise RealCorpusError("real_corpus_article_type_rejected")
    if metadata["publication_date"] > QUERY_CUTOFF:
        raise RealCorpusError("real_corpus_date_cutoff_rejected")
    sections, paragraphs = _parse_body(article, candidate["pmcid"])
    body_chars = sum(len(item["text"]) for item in paragraphs)
    if len(paragraphs) < MIN_USABLE_PARAGRAPHS or body_chars < MIN_USABLE_BODY_CHARS:
        raise RealCorpusError("real_corpus_body_text_insufficient")
    normalized_jats = ET.tostring(article, encoding="utf-8", method="xml")
    content_digest = _sha256_bytes(normalized_jats)
    identity_basis = {
        "pmcid": metadata["pmcid"],
        "pmid": metadata["pmid"],
        "doi": metadata["doi"],
        "title": metadata["title"],
        "publication_date": metadata["publication_date"],
        "content_digest": content_digest,
    }
    reference_count = sum(1 for node in article.iter() if _local_name(node.tag) == "ref")
    return {
        "schema_version": PARSED_SCHEMA,
        "parser_version": PARSER_VERSION,
        **metadata,
        "article_type": str(article.attrib.get("article-type") or "journal-article"),
        "article_type_flags": {"correction_or_retraction": False},
        "license_type": rights["license_type"],
        "license_statement_digest": jats_license["license_statement_digest"],
        "sections": sections,
        "paragraphs": paragraphs,
        "usable_section_count": len(sections),
        "usable_paragraph_count": len(paragraphs),
        "usable_body_char_count": body_chars,
        "reference_count": reference_count,
        "normalized_jats_sha256": content_digest,
        "article_identity_digest": _sha256_bytes(_canonical_bytes(identity_basis)),
    }


def _paragraph_score(paragraph: dict[str, Any], terms: tuple[str, ...], section_hints: tuple[str, ...]) -> int:
    text = str(paragraph.get("text") or "").lower()
    section = " ".join(paragraph.get("section_path") or []).lower()
    return sum(3 for term in terms if term in text) + sum(4 for term in section_hints if term in section)


def _bounded_excerpt(text: str, limit: int = MAX_EVIDENCE_EXCERPT_CHARS) -> str:
    normalized = _normalized_text(text)
    if len(normalized) <= limit:
        return normalized
    candidate = normalized[: limit + 1]
    boundary = max(candidate.rfind(". "), candidate.rfind("; "), candidate.rfind(", "), candidate.rfind(" "))
    return candidate[: boundary if boundary >= limit // 2 else limit].rstrip(" ,;") + "…"


def _first_sentence(text: str, limit: int = 300) -> str:
    excerpt = _bounded_excerpt(text, limit)
    match = re.search(r"(?<=[.!?])\s", excerpt)
    return excerpt[: match.start() + 1] if match else excerpt


def extract_evidence(run_id: str, parsed: dict[str, Any], artifact_id: str) -> list[dict[str, Any]]:
    paragraphs = list(parsed.get("paragraphs") or [])
    category_specs = (
        ("intervention_method", _INTERVENTION_TERMS, ("method", "materials", "intervention", "procedure")),
        ("principal_finding", _FINDING_TERMS, ("result", "finding", "outcome")),
        ("limitation_uncertainty", _LIMITATION_TERMS, ("limitation", "discussion", "strength", "future")),
    )
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for category, terms, section_hints in category_specs:
        ranked = sorted(
            paragraphs,
            key=lambda item: (
                -_paragraph_score(item, terms, section_hints),
                int(item.get("paragraph_ordinal") or 0),
                str(item.get("paragraph_id") or ""),
            ),
        )
        chosen = next((item for item in ranked if item["paragraph_id"] not in used), None)
        if chosen is None:
            raise RealCorpusError("real_corpus_evidence_coverage_incomplete")
        if category == "limitation_uncertainty" and _paragraph_score(chosen, terms, section_hints) <= 0:
            raise RealCorpusError("real_corpus_limitation_evidence_missing")
        used.add(chosen["paragraph_id"])
        excerpt = _bounded_excerpt(str(chosen["text"]))
        evidence_basis = {
            "run_id": run_id,
            "pmcid": parsed["pmcid"],
            "content_digest": parsed["normalized_jats_sha256"],
            "paragraph_id": chosen["paragraph_id"],
            "category": category,
        }
        selected.append(
            {
                "evidence_id": f"evidence-{_sha256_bytes(_canonical_bytes(evidence_basis))[:18]}",
                "run_id": run_id,
                "paper_id": parsed["pmcid"],
                "full_text_artifact_id": artifact_id,
                "source_content_digest": parsed["normalized_jats_sha256"],
                "section_id": chosen["section_id"],
                "paragraph_id": chosen["paragraph_id"],
                "section_path": chosen["section_path"],
                "paragraph_ordinal": chosen["paragraph_ordinal"],
                "normalized_quote": excerpt,
                "start_offset": 0,
                "end_offset": len(excerpt.rstrip("…")),
                "evidence_type": "body_full_text",
                "claim_category": category,
                "confidence_strength": "moderate",
                "extraction_method": REVIEW_METHOD,
                "created_timestamp": iso_now_utc(),
                "stale": False,
            }
        )
    return selected


def build_review(run_id: str, parsed: dict[str, Any], artifact_id: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    by_category = {item["claim_category"]: item for item in evidence}
    method = _first_sentence(by_category["intervention_method"]["normalized_quote"])
    finding = _first_sentence(by_category["principal_finding"]["normalized_quote"])
    limitation = _first_sentence(by_category["limitation_uncertainty"]["normalized_quote"])
    review_identity = f"{run_id}|{parsed['pmcid']}|{parsed['normalized_jats_sha256']}"
    return {
        "schema_version": REVIEW_SCHEMA,
        "review_id": f"review-{_sha256_text(review_identity)[:18]}",
        "run_id": run_id,
        "paper_id": parsed["pmcid"],
        "full_text_artifact_id": artifact_id,
        "source_content_digest": parsed["normalized_jats_sha256"],
        "parser_version": PARSER_VERSION,
        "review_method": REVIEW_METHOD,
        "reviewed_section_count": parsed["usable_section_count"],
        "reviewed_paragraph_count": parsed["usable_paragraph_count"],
        "method_or_intervention_summary": method,
        "population_context_summary": method,
        "outcome_findings_summary": finding,
        "limitations": [limitation],
        "relevance_to_topic": "The article matched the fixed VR/AR rehabilitation or clinical-training query.",
        "uncertainty_notes": [limitation],
        "body_coverage_summary": (
            f"Reviewed {parsed['usable_paragraph_count']} usable body paragraphs across "
            f"{parsed['usable_section_count']} sections."
        ),
        "evidence_span_ids": [item["evidence_id"] for item in evidence],
        "created_timestamp": iso_now_utc(),
        "stale": False,
    }


def evidence_is_stale(evidence: dict[str, Any], current_content_digest: str) -> bool:
    return bool(evidence.get("stale")) or str(evidence.get("source_content_digest") or "") != str(
        current_content_digest or ""
    )


def validate_report_claims(
    run_id: str,
    claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, int]:
    evidence_by_id = {str(item.get("evidence_id") or ""): item for item in evidence if item.get("evidence_id")}
    unsupported = 0
    unknown = 0
    cross_run = 0
    stale = 0
    for claim in claims:
        if str(claim.get("claim_type") or "") != "grounded":
            continue
        evidence_ids = [str(value) for value in claim.get("evidence_ids") or [] if str(value)]
        if not evidence_ids:
            unsupported += 1
            continue
        for evidence_id in evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                unknown += 1
                continue
            if str(item.get("run_id") or "") != run_id:
                cross_run += 1
            if item.get("stale"):
                stale += 1
    return {
        "unsupported_grounded_claims": unsupported,
        "unknown_evidence_ids": unknown,
        "cross_run_evidence_ids": cross_run,
        "stale_evidence_references": stale,
    }


def _review_payload_for_workbench(
    run_id: str,
    parsed: dict[str, Any],
    rights: dict[str, Any],
    review: dict[str, Any],
    evidence: list[dict[str, Any]],
    parsed_ref: str,
) -> dict[str, Any]:
    findings = [review["outcome_findings_summary"], review["method_or_intervention_summary"]]
    return {
        "paper_id": parsed["pmcid"],
        "title": parsed["title"],
        "theme": "Real PMC open-access corpus",
        "read_level": "REAL_FULL_TEXT_REVIEWED",
        "links": {
            "entry_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{parsed['pmcid']}/",
            "doi": parsed.get("doi") or "",
            "license_url": rights["canonical_license_url"],
        },
        "review_ko": {
            "one_liner": review["outcome_findings_summary"],
            "why_it_matters": [review["relevance_to_topic"], review["body_coverage_summary"]],
            "key_contributions": findings,
            "method": {
                "problem": review["method_or_intervention_summary"],
                "inputs_outputs": "Parsed JATS body paragraphs to bounded evidence spans.",
                "model": REVIEW_METHOD,
                "training": "No model training or Provider invocation.",
                "inference": "Deterministic local lexical extraction from accepted body text.",
                "system_constraints": ["Official PMC OAI-PMH", rights["license_type"], "Disposable local runtime"],
            },
            "experiments": {
                "datasets": [parsed["pmcid"]],
                "metrics": ["Body paragraph coverage", "Claim-to-evidence coverage"],
                "baselines": ["Deterministic full-text review"],
                "key_results": [review["outcome_findings_summary"]],
                "runtime_latency": "Not evaluated.",
                "hardware": "Not applicable.",
            },
            "xr_takeaways": [review["relevance_to_topic"]],
            "limitations": review["limitations"],
            "reproducibility": {
                "code": "Prototype v0.3 deterministic pipeline",
                "weights": "None",
                "license": rights["license_type"],
                "notes": "Provider review not used.",
            },
            "lab_actions": ["Inspect the linked body evidence.", "Compare findings across accepted papers."],
            "confidence": 0.8,
            "evidence": [
                {
                    "page": None,
                    "section": " / ".join(item["section_path"]),
                    "reference": item["evidence_id"],
                    "note": item["normalized_quote"],
                    "summary": f"Real body evidence: {item['claim_category'].replace('_', ' ')}.",
                    "source_type": "real_full_text_body",
                    "quality_hint": REVIEW_METHOD,
                    "confidence": 0.8,
                    "artifact_path": parsed_ref,
                }
                for item in evidence
            ],
        },
        "generated_at_utc": iso_now_utc(),
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, str]:
    date_number = int(str(candidate.get("publication_date") or "1900-01-01").replace("-", ""))
    return (-int(candidate.get("relevance_score") or 0), -date_number, str(candidate.get("pmcid") or ""))


def _candidate_is_eligible(candidate: dict[str, Any]) -> str | None:
    if not candidate.get("pmcid"):
        return "pmcid_missing"
    if str(candidate.get("language") or "").lower() != "english":
        return "non_english"
    if str(candidate.get("publication_date") or "1900-01-01") > QUERY_CUTOFF:
        return "after_date_cutoff"
    title_and_type = f"{candidate.get('title', '')} {candidate.get('article_type', '')}".lower()
    if any(term in title_and_type for term in _CORRECTION_TERMS):
        return "correction_retraction_or_concern"
    if not str(candidate.get("title") or "").strip():
        return "title_missing"
    return None


def candidate_identity_rejection(
    candidate: dict[str, Any],
    seen_pmcids: set[str],
    seen_works: set[str],
) -> str | None:
    pmcid = str(candidate.get("pmcid") or "")
    work_key = str(candidate.get("doi") or candidate.get("title") or "").strip().lower()
    if pmcid in seen_pmcids:
        return "duplicate_pmcid"
    if work_key and work_key in seen_works:
        return "duplicate_work_version"
    seen_pmcids.add(pmcid)
    if work_key:
        seen_works.add(work_key)
    return None


def _oai_url(candidate: dict[str, Any], prefix: str) -> str:
    identifier = f"oai:pubmedcentral.nih.gov:{str(candidate['uid'])}"
    return _query_url(
        PMC_OAI_ENDPOINT,
        {"verb": "GetRecord", "identifier": identifier, "metadataPrefix": prefix},
    )


def _make_step(name: str, status: str = "pending", note: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "note": note, "updated_at": iso_now_utc()}


def _set_step(run: dict[str, Any], name: str, status: str, note: str) -> None:
    steps = list(run.get("steps") or [])
    replacement = _make_step(name, status, note)
    for index, item in enumerate(steps):
        if item.get("name") == name:
            steps[index] = replacement
            break
    else:
        steps.append(replacement)
    run["steps"] = steps
    save_run_record(run)


def _provider_summary() -> dict[str, Any]:
    return {
        "provider_id": None,
        "surface_id": None,
        "stage": "report_draft_assist",
        "output_class": "report_draft",
        "route_status": "disabled_for_real_corpus_proof",
        "fallback_used": True,
        "fallback_reason": "deterministic_real_full_text_required",
        "live_provider_invoked": False,
        "accepted_control": "not_invoked",
        "rejection_controls": "preserved",
        "control_total": 0,
        "control_passed": 0,
    }


def create_real_corpus_run(*, owner_user_id: str | None) -> dict[str, Any]:
    prototype.require_prototype_mode()
    if not network_enabled():
        raise RealCorpusError("real_corpus_network_disabled")
    request_payload = {
        "topic": TOPIC,
        "query_id": QUERY_ID,
        "query_sha256": QUERY_SHA256,
        "query_cutoff": QUERY_CUTOFF,
        "source": SOURCE,
        "max_candidates": MAX_CANDIDATES,
        "max_articles": MAX_ACCEPTED_ARTICLES,
        "license_policy": ["CC0", "CC BY"],
        "provider_mode": "disabled",
    }
    run = create_run_record(
        topic=TOPIC,
        request_payload=request_payload,
        source="prototype_v0_3:pmc_open_access_full_text",
        write_legacy=False,
        owner_user_id=owner_user_id,
    )
    run["status"] = "running"
    run["started_at"] = iso_now_utc()
    run["topic_profile_id"] = "prototype-pmc-open-access-v1"
    run["steps"] = [_make_step(stage) for stage in PROGRESS_STAGES]
    run["prototype"] = {
        "schema_version": REAL_CORPUS_SCHEMA,
        "label": "Prototype v0.3 · real PMC open-access full text",
        "corpus_kind": "real_open_access",
        "corpus_count": 0,
        "pipeline_status": "running",
        "report_status": "pending",
        "reviewed_paper_count": 0,
        "evidence_span_count": 0,
        "provider": _provider_summary(),
        "real_corpus": {
            "source": SOURCE,
            "query_id": QUERY_ID,
            "network_request_count": 0,
            "provider_call_count": 0,
            "certification_status": "in_progress",
        },
    }
    save_run_record(run)
    return run


def _build_claims(run_id: str, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for article in articles:
        for evidence in article["evidence"]:
            claim_text = _first_sentence(evidence["normalized_quote"])
            basis = {
                "run_id": run_id,
                "evidence_id": evidence["evidence_id"],
                "claim_text": claim_text,
            }
            claims.append(
                {
                    "claim_id": f"claim-{_sha256_bytes(_canonical_bytes(basis))[:18]}",
                    "claim_type": "grounded",
                    "claim_category": evidence["claim_category"],
                    "claim_text": claim_text,
                    "evidence_ids": [evidence["evidence_id"]],
                    "paper_ids": [article["paper"]["pmcid"]],
                    "source_coverage": "real_body_paragraph",
                    "confidence_strength": evidence["confidence_strength"],
                }
            )
    return claims


def _claim_for_outline(claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    citations: list[dict[str, Any]] = []
    for evidence_id in claim["evidence_ids"]:
        evidence = evidence_by_id[evidence_id]
        citations.append(
            {
                "evidence_id": evidence_id,
                "source_id": evidence["paper_id"],
                "paper_id": evidence["paper_id"],
                "artifact_path": f"run:raw/real-corpus/{evidence['paper_id']}/parsed.json",
                "citation_text": " / ".join(evidence["section_path"]),
                "snippet": evidence["normalized_quote"],
            }
        )
    return {"claim_id": claim["claim_id"], "claim_text": claim["claim_text"], "citations": citations}


def build_report(run_id: str, articles: list[dict[str, Any]], network: PmcNetworkClient) -> dict[str, Any]:
    if any(
        article.get("rights", {}).get("license_type") not in {"CC0", "CC BY"}
        or not article.get("rights", {}).get("reuse_allowed")
        for article in articles
    ):
        raise RealCorpusError("real_corpus_report_license_boundary")
    claims = _build_claims(run_id, articles)
    evidence = [item for article in articles for item in article["evidence"]]
    validation = validate_report_claims(run_id, claims, evidence)
    if any(validation.values()):
        raise RealCorpusError("real_corpus_report_evidence_boundary")
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    paper_ids = [article["paper"]["pmcid"] for article in articles]
    grounded_by_category: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        grounded_by_category.setdefault(claim["claim_category"], []).append(claim)

    source_lines = [
        f"- {article['paper']['title']} (`{article['paper']['pmcid']}`) — {article['rights']['license_type']}"
        for article in articles
    ]
    section_specs: list[tuple[str, str, list[dict[str, Any]], list[str]]] = [
        ("topic-scope", "Research topic and scope", [], [f"Fixed topic: {TOPIC}", f"Query ID: `{QUERY_ID}`; cutoff: `{QUERY_CUTOFF}`."]),
        (
            "corpus-acquisition",
            "Corpus and acquisition statement",
            [],
            [
                f"Real corpus source: {SOURCE} through official NCBI services only.",
                f"Accepted articles: {len(articles)}; HTTP requests: {network.request_count}; Provider calls: 0.",
            ],
        ),
        ("executive-summary", "Executive summary", claims[: min(3, len(claims))], ["Grounded statements below link to accepted body evidence."]),
        ("literature-overview", "Reviewed literature overview", [], source_lines),
        ("interventions", "Intervention or system categories", grounded_by_category.get("intervention_method", []), ["Claims are deterministic excerpts or bounded sentence-level renderings of body evidence."]),
        ("principal-findings", "Principal findings", grounded_by_category.get("principal_finding", []), ["Findings remain scoped to each selected article."]),
        ("cross-paper-themes", "Cross-paper themes", [], ["[inference] The fixed corpus contains multiple VR/AR rehabilitation or clinical-training contexts; this small selection does not establish field-wide consensus."]),
        ("differences", "Differences or contradictions", [], ["[uncertainty] Study contexts and outcome definitions differ, so findings should not be treated as directly interchangeable."]),
        ("limitations", "Limitations and uncertainties", grounded_by_category.get("limitation_uncertainty", []), ["At least one qualification or limitation is retained from every accepted paper."]),
        ("conclusions", "Evidence-grounded conclusions", claims[-min(3, len(claims)) :], ["Conclusions are bounded to the linked current-run evidence."]),
        ("next-questions", "Recommended next research questions", [], ["[proposal] Compare intervention design, population, and outcome measures across a larger license-compatible corpus."]),
        ("license-appendix", "Corpus/license appendix", [], source_lines),
        (
            "references-provenance",
            "References and provenance",
            [],
            [
                *[
                    f"- {article['paper']['pmcid']} · DOI {article['paper'].get('doi') or 'not available'} · "
                    f"content `{article['artifact']['normalized_jats_sha256'][:16]}` · rights `{article['rights']['record_digest'][:16]}`"
                    for article in articles
                ],
                "Source acknowledgement: metadata and reusable JATS full text were retrieved from NCBI/PMC; no endorsement is implied.",
            ],
        ),
    ]
    outline_sections: list[dict[str, Any]] = []
    markdown_lines = ["# Weekly Journal — Real PMC open-access evidence", ""]
    for section_id, title, section_claims, body_lines in section_specs:
        markdown_lines.extend([f"## {title}", ""])
        for line in body_lines:
            markdown_lines.extend([line, ""])
        for claim in section_claims:
            evidence_ids = ", ".join(claim["evidence_ids"])
            markdown_lines.append(f"- [grounded · `{claim['claim_id']}`] {claim['claim_text']} (evidence: `{evidence_ids}`)")
        if section_claims:
            markdown_lines.append("")
        outline_sections.append(
            {
                "section_id": section_id,
                "title": title,
                "kind": "markdown",
                "body_markdown": "\n\n".join(body_lines),
                "paper_ids": sorted({paper_id for claim in section_claims for paper_id in claim["paper_ids"]}),
                "claims": [_claim_for_outline(claim, evidence_by_id) for claim in section_claims],
                "provenance": [
                    {
                        "source_run_id": run_id,
                        "source": SOURCE,
                        "review_method": REVIEW_METHOD,
                        "paper_ids": sorted({paper_id for claim in section_claims for paper_id in claim["paper_ids"]}),
                    }
                ]
                if section_claims
                else [],
            }
        )
    markdown = "\n".join(markdown_lines).rstrip() + "\n"
    report_digest = _sha256_text(markdown)
    report_id = f"real-report-{report_digest[:16]}"
    report_version_id = f"real-report-v1-{report_digest[:12]}"
    return {
        "report_id": report_id,
        "report_version_id": report_version_id,
        "generation_mode": REVIEW_METHOD,
        "markdown": markdown,
        "report_digest": report_digest,
        "outline_sections": outline_sections,
        "claims": claims,
        "grounded_claim_count": len(claims),
        "inference_count": 1,
        "proposal_count": 1,
        "uncertainty_count": 1,
        "unsupported_grounded_claims": validation["unsupported_grounded_claims"],
        "unknown_evidence_ids": validation["unknown_evidence_ids"],
        "cross_run_evidence_ids": validation["cross_run_evidence_ids"],
        "abstract_only_body_claims": 0,
        "license_rejected_article_references": 0,
        "stale_evidence_references": validation["stale_evidence_references"],
        "claim_to_evidence_coverage": 1.0 if claims else 0.0,
        "license_appendix": True,
        "paper_ids": paper_ids,
    }


def _public_article(article: dict[str, Any]) -> dict[str, Any]:
    parsed = article["parsed"]
    return {
        "pmcid": parsed["pmcid"],
        "pmid": parsed.get("pmid"),
        "doi": parsed.get("doi"),
        "title": parsed["title"],
        "authors": parsed["authors"],
        "journal": parsed["journal"],
        "publication_date": parsed["publication_date"],
        "license_type": article["rights"]["license_type"],
        "license_url": article["rights"]["canonical_license_url"],
        "rights_accepted": True,
        "full_text_retrieved": True,
        "content_digest": parsed["normalized_jats_sha256"],
        "article_identity_digest": parsed["article_identity_digest"],
        "usable_section_count": parsed["usable_section_count"],
        "usable_paragraph_count": parsed["usable_paragraph_count"],
        "review_method": REVIEW_METHOD,
        "review_id": article["review"]["review_id"],
        "evidence_ids": [item["evidence_id"] for item in article["evidence"]],
        "retrieval_result": "accepted",
    }


def _write_manifest(run_id: str, record: dict[str, Any]) -> str:
    path = build_run_paths(run_id).inputs_dir / MANIFEST_FILE_NAME
    manifest = {
        "schema_version": "prototype_real_corpus_manifest_v1",
        "run_id": run_id,
        "query_id": QUERY_ID,
        "query_sha256": QUERY_SHA256,
        "query_cutoff": QUERY_CUTOFF,
        "selection_algorithm_version": SELECTION_ALGORITHM_VERSION,
        "selected_articles": [
            {
                key: article.get(key)
                for key in (
                    "pmcid",
                    "pmid",
                    "doi",
                    "title",
                    "authors",
                    "journal",
                    "publication_date",
                    "license_type",
                    "license_url",
                    "content_digest",
                    "article_identity_digest",
                )
            }
            for article in record["articles"]
        ],
    }
    manifest = _with_record_digest(manifest)
    _atomic_write_json(path, manifest, run_id=run_id)
    return _portable_ref(run_id, path)


def _persist_article(
    run_id: str,
    candidate: dict[str, Any],
    rights: dict[str, Any],
    response: MetadataFetchResult,
) -> dict[str, Any]:
    parsed = parse_jats_record(response.body, candidate, rights)
    article_dir = build_run_paths(run_id).raw_dir / "real-corpus" / candidate["pmcid"]
    source_path = article_dir / "source.xml"
    parsed_path = article_dir / "parsed.json"
    rights_path = article_dir / "rights.json"
    artifact_path = article_dir / "artifact.json"
    cache_dir = build_run_paths(run_id).raw_dir / "real-corpus" / "cache"

    raw_sha = _sha256_bytes(response.body)
    rights = _with_record_digest({**rights, "article_content_digest": parsed["normalized_jats_sha256"]})
    artifact_basis = {
        "run_id": run_id,
        "pmcid": candidate["pmcid"],
        "raw_sha256": raw_sha,
        "content_sha256": parsed["normalized_jats_sha256"],
        "rights_digest": rights["record_digest"],
    }
    artifact_id = f"fulltext-{_sha256_bytes(_canonical_bytes(artifact_basis))[:18]}"
    artifact = _with_record_digest(
        {
            "schema_version": ARTIFACT_SCHEMA,
            "artifact_id": artifact_id,
            "run_id": run_id,
            "pmcid": candidate["pmcid"],
            "source_endpoint_id": "PMC_OAI_V1_PMC",
            "retrieval_timestamp": iso_now_utc(),
            "response_byte_count": len(response.body),
            "raw_response_sha256": raw_sha,
            "normalized_jats_sha256": parsed["normalized_jats_sha256"],
            "parser_version": PARSER_VERSION,
            "article_identity_digest": parsed["article_identity_digest"],
            "license_record_id": rights["rights_record_id"],
            "local_portable_artifact_reference": f"run:raw/real-corpus/{candidate['pmcid']}/source.xml",
            "content_state": "validated",
            "parse_state": "parsed",
            "review_state": "pending",
            "error_category": None,
        }
    )
    _atomic_write_bytes(source_path, response.body, run_id=run_id)
    _atomic_write_json(parsed_path, parsed, run_id=run_id)
    _atomic_write_json(rights_path, rights, run_id=run_id)
    _atomic_write_json(artifact_path, artifact, run_id=run_id)

    parsed_bytes = parsed_path.read_bytes()
    cache_basis = {
        "pmcid": candidate["pmcid"],
        "source_endpoint": "PMC_OAI_V1_PMC",
        "raw_digest": raw_sha,
        "normalized_content_digest": parsed["normalized_jats_sha256"],
        "license_record_digest": rights["record_digest"],
        "parser_version": PARSER_VERSION,
    }
    cache_key = _sha256_bytes(_canonical_bytes(cache_basis))
    cache_record = _with_record_digest(
        {
            "schema_version": CACHE_SCHEMA,
            "cache_key": cache_key,
            **cache_basis,
            "source_artifact_ref": _portable_ref(run_id, source_path),
            "parsed_artifact_ref": _portable_ref(run_id, parsed_path),
            "parsed_artifact_sha256": _sha256_bytes(parsed_bytes),
            "artifact_record_ref": _portable_ref(run_id, artifact_path),
            "rights_record_ref": _portable_ref(run_id, rights_path),
            "complete": True,
        }
    )
    cache_path = cache_dir / f"{cache_key}.json"
    _atomic_write_json(cache_path, cache_record, run_id=run_id)

    evidence = extract_evidence(run_id, parsed, artifact_id)
    review = build_review(run_id, parsed, artifact_id, evidence)
    review_path = build_run_paths(run_id).reviews_dir / f"{safe_filename(candidate['pmcid'])}.json"
    workbench_review = _review_payload_for_workbench(
        run_id,
        parsed,
        rights,
        review,
        evidence,
        _portable_ref(run_id, parsed_path),
    )
    _atomic_write_json(review_path, workbench_review, run_id=run_id)
    artifact = _with_record_digest({**artifact, "review_state": "reviewed"})
    _atomic_write_json(artifact_path, artifact, run_id=run_id)
    return {
        "paper": candidate,
        "rights": rights,
        "parsed": parsed,
        "artifact": artifact,
        "review": review,
        "evidence": evidence,
        "cache": cache_record,
        "cache_ref": _portable_ref(run_id, cache_path),
    }


def _triage_item(article: dict[str, Any]) -> dict[str, Any]:
    parsed = article["parsed"]
    return {
        "paper_id": parsed["pmcid"],
        "entry_id": f"pmc:{parsed['pmcid']}",
        "title": parsed["title"],
        "abstract": parsed["abstract"],
        "authors": parsed["authors"],
        "published": parsed["publication_date"],
        "source_provider": "pmc_open_access",
        "source": SOURCE,
        "entry_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{parsed['pmcid']}/",
        "pdf_url": "",
        "doi": parsed.get("doi") or "",
        "score": int(article["paper"].get("relevance_score") or 0),
        "theme": "VR/AR rehabilitation and clinical training",
        "triage": {
            "hits_primary": ["virtual reality or augmented reality", "rehabilitation or clinical training"],
            "hits_secondary": [article["rights"]["license_type"], "real full text"],
            "hits_negative": [],
        },
        "license_type": article["rights"]["license_type"],
        "content_digest": parsed["normalized_jats_sha256"],
    }


def _public_record_path(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / PUBLIC_RECORD_FILE_NAME


def _persist_public_record(run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    finalized = _with_record_digest(record)
    _atomic_write_json(_public_record_path(run_id), finalized, run_id=run_id)
    return finalized


def _failure_record(run: dict[str, Any], client: PmcNetworkClient, code: str, rejected: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": PUBLIC_RECORD_SCHEMA,
        "run_id": run["run_id"],
        "topic": TOPIC,
        "query": {
            "query_id": QUERY_ID,
            "normalized_query": QUERY,
            "query_sha256": QUERY_SHA256,
            "date_cutoff": QUERY_CUTOFF,
            "selection_algorithm_version": SELECTION_ALGORITHM_VERSION,
        },
        "source": SOURCE,
        "status": "incomplete",
        "failure_category": code,
        "discovery_result_count": 0,
        "discovered_candidate_count": 0,
        "license_eligible_count": 0,
        "selected_article_count": 0,
        "articles": [],
        "rejected_candidates": rejected,
        "evidence": [],
        "claims": [],
        "report": None,
        "network": {
            "request_count": client.request_count,
            "total_downloaded_bytes": client.total_downloaded_bytes,
            "retries": 0,
            "non_allowlisted_requests": 0,
            "request_records": client.request_records,
        },
        "provider_calls": 0,
        "replay": None,
    }


def execute_real_corpus_run(run_id: str, *, client: PmcNetworkClient | None = None) -> dict[str, Any]:
    run = load_run_record(run_id)
    prototype_record = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    if prototype_record.get("schema_version") != REAL_CORPUS_SCHEMA:
        raise RealCorpusError("real_corpus_run_tag_missing")
    network = client or PmcNetworkClient()
    rejected: list[dict[str, Any]] = []
    discovery_count = 0
    candidates: list[dict[str, Any]] = []
    rights_eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    articles: list[dict[str, Any]] = []
    try:
        _set_step(run, "discovery", "running", "Querying the fixed PMC Open Access topic.")
        discovery = network.fetch(
            _query_url(
                ESEARCH_ENDPOINT,
                {"db": "pmc", "term": QUERY, "retmode": "json", "retmax": MAX_CANDIDATES, "sort": "relevance"},
            ),
            purpose="fixed_query_discovery",
            max_bytes=MAX_METADATA_RESPONSE_BYTES,
            accept="application/json",
        )
        discovery_count, candidate_ids = parse_esearch(discovery.body)
        if not candidate_ids:
            raise RealCorpusError("real_corpus_no_discovery_candidates")
        summary = network.fetch(
            _query_url(
                ESUMMARY_ENDPOINT,
                {"db": "pmc", "id": ",".join(candidate_ids), "retmode": "json", "version": "2.0"},
            ),
            purpose="bounded_citation_metadata",
            max_bytes=MAX_METADATA_RESPONSE_BYTES,
            accept="application/json",
        )
        candidates = parse_esummary(summary.body, candidate_ids)[:MAX_CANDIDATES]
        _set_step(run, "discovery", "completed", f"Frozen {len(candidates)} metadata candidates.")

        _set_step(run, "rights_verification", "running", "Checking machine-readable PMC OAI rights.")
        seen_pmcids: set[str] = set()
        seen_works: set[str] = set()
        for candidate in candidates:
            reason = _candidate_is_eligible(candidate)
            if reason is None:
                reason = candidate_identity_rejection(candidate, seen_pmcids, seen_works)
            if reason:
                rejected.append({"pmcid": candidate.get("pmcid"), "reason": reason})
                continue
            rights_response = network.fetch(
                _oai_url(candidate, "oai_dc"),
                purpose=f"rights_metadata:{candidate['pmcid']}",
                max_bytes=MAX_METADATA_RESPONSE_BYTES,
                accept="application/xml,text/xml",
            )
            try:
                rights = parse_oai_dc(rights_response.body, candidate, retrieved_at=iso_now_utc())
            except RealCorpusError as exc:
                rejected.append({"pmcid": candidate["pmcid"], "reason": exc.code})
                continue
            if not rights["reuse_allowed"]:
                rejected.append({"pmcid": candidate["pmcid"], "reason": rights["rejection_reason"]})
                continue
            rights_eligible.append((candidate, rights))
        rights_eligible.sort(key=lambda item: _candidate_sort_key(item[0]))
        _set_step(run, "rights_verification", "completed", f"Accepted rights for {len(rights_eligible)} candidates.")

        _set_step(run, "acquisition", "running", "Retrieving selected reusable JATS records.")
        for candidate, rights in rights_eligible:
            if len(articles) >= MAX_ACCEPTED_ARTICLES:
                break
            full_response = network.fetch(
                _oai_url(candidate, "pmc"),
                purpose=f"jats_full_text:{candidate['pmcid']}",
                max_bytes=MAX_FULL_TEXT_RESPONSE_BYTES,
                accept="application/xml,text/xml",
            )
            try:
                article = _persist_article(run_id, candidate, rights, full_response)
            except RealCorpusError as exc:
                rejected.append({"pmcid": candidate["pmcid"], "reason": exc.code})
                continue
            articles.append(article)
        _set_step(run, "acquisition", "completed", f"Accepted {len(articles)} complete JATS artifacts.")
    except (RealCorpusError, RetrievalTransportError) as exc:
        code = exc.code if isinstance(exc, (RealCorpusError, RetrievalTransportError)) else "real_corpus_acquisition_failed"
        failed = _persist_public_record(run_id, _failure_record(run, network, code, rejected))
        run = load_run_record(run_id)
        run["status"] = "failed"
        run["finished_at"] = iso_now_utc()
        run["error"] = code
        run["prototype"] = {
            **prototype_record,
            "pipeline_status": "incomplete",
            "report_status": "unavailable",
            "real_corpus": {
                "source": SOURCE,
                "query_id": QUERY_ID,
                "network_request_count": network.request_count,
                "provider_call_count": 0,
                "certification_status": "official_source_unavailable",
                "failure_category": code,
                "record_digest": failed["record_digest"],
            },
        }
        save_run_record(run)
        return failed

    _set_step(run, "parsing", "completed", f"Parsed {len(articles)} article bodies.")
    _set_step(run, "review", "completed", f"Created {len(articles)} deterministic full-text reviews.")
    evidence = [item for article in articles for item in article["evidence"]]
    _set_step(run, "evidence_extraction", "completed", f"Created {len(evidence)} current-run evidence spans.")

    triage_path = build_run_paths(run_id).triage_dir / "triage.jsonl"
    paper_occurrences.write_triage_records(triage_path, [_triage_item(article) for article in articles])
    report: dict[str, Any] | None = None
    if articles:
        report = build_report(run_id, articles, network)
        report_path = build_run_paths(run_id).reports_dir / REPORT_FILE_NAME
        _atomic_write_bytes(report_path, report["markdown"].encode("utf-8"), run_id=run_id)
        reporting.save_outline(run_id, report["outline_sections"])
        _set_step(run, "report_generation", "completed", f"Generated {report['grounded_claim_count']} grounded claims.")
        _set_step(run, "export_ready", "completed", "Portable preview and report export are ready.")
    else:
        _set_step(run, "report_generation", "failed", "No accepted full text was available for a report.")
        _set_step(run, "export_ready", "failed", "Report export is unavailable.")

    category_counts = Counter(item["claim_category"] for item in evidence)
    record = {
        "schema_version": PUBLIC_RECORD_SCHEMA,
        "run_id": run_id,
        "topic": TOPIC,
        "query": {
            "query_id": QUERY_ID,
            "normalized_query": QUERY,
            "query_sha256": QUERY_SHA256,
            "date_cutoff": QUERY_CUTOFF,
            "discovery_timestamp": run.get("started_at"),
            "result_count": discovery_count,
            "selected_article_ids": [article["parsed"]["pmcid"] for article in articles],
            "selection_algorithm_version": SELECTION_ALGORITHM_VERSION,
        },
        "source": SOURCE,
        "status": "complete" if len(articles) >= MIN_PASS_ARTICLES else "incomplete",
        "failure_category": None if len(articles) >= MIN_PASS_ARTICLES else "insufficient_eligible_real_articles",
        "discovery_result_count": discovery_count,
        "discovered_candidate_count": len(candidates),
        "license_eligible_count": len(rights_eligible),
        "selected_article_count": len(articles),
        "articles": [_public_article(article) for article in articles],
        "rejected_candidates": rejected,
        "evidence": evidence,
        "evidence_category_counts": dict(category_counts),
        "limitation_uncertainty_evidence_present": bool(category_counts.get("limitation_uncertainty")),
        "invalid_or_stale_evidence_count": sum(1 for item in evidence if item.get("stale")),
        "review_count": len(articles),
        "usable_section_count": sum(article["parsed"]["usable_section_count"] for article in articles),
        "usable_paragraph_count": sum(article["parsed"]["usable_paragraph_count"] for article in articles),
        "claims": report["claims"] if report else [],
        "report": {
            key: report[key]
            for key in (
                "report_id",
                "report_version_id",
                "generation_mode",
                "report_digest",
                "grounded_claim_count",
                "inference_count",
                "proposal_count",
                "uncertainty_count",
                "unsupported_grounded_claims",
                "unknown_evidence_ids",
                "cross_run_evidence_ids",
                "abstract_only_body_claims",
                "license_rejected_article_references",
                "stale_evidence_references",
                "claim_to_evidence_coverage",
                "license_appendix",
            )
        }
        if report
        else None,
        "network": {
            "request_count": network.request_count,
            "total_downloaded_bytes": network.total_downloaded_bytes,
            "retries": 0,
            "non_allowlisted_requests": 0,
            "request_records": network.request_records,
        },
        "provider_calls": 0,
        "replay": None,
    }
    record = _persist_public_record(run_id, record)
    manifest_ref = _write_manifest(run_id, record)

    run = load_run_record(run_id)
    accepted_enough = len(articles) >= MIN_PASS_ARTICLES
    run["status"] = "completed" if accepted_enough else "failed"
    run["finished_at"] = iso_now_utc()
    run["error"] = None if accepted_enough else "insufficient_eligible_real_articles"
    run.setdefault("counts", {}).update(
        {
            "triage_items": len(articles),
            "reviewed_items": len(articles),
            "full_text_reviewed_items": len(articles),
            "evidence_spans": len(evidence),
            "real_corpus_items": len(articles),
        }
    )
    run.setdefault("artifacts", {}).update(
        {
            "triage_jsonl": str(triage_path),
            "report_markdown": str(build_run_paths(run_id).reports_dir / REPORT_FILE_NAME) if report else None,
            "outline_json": str(build_run_paths(run_id).reports_dir / reporting.OUTLINE_FILENAME) if report else None,
            "preview_markdown": str(build_run_paths(run_id).reports_dir / reporting.PREVIEW_FILENAME) if report else None,
            "real_corpus_record": str(_public_record_path(run_id)),
            "real_corpus_manifest": manifest_ref,
        }
    )
    quality = {
        "status": "ready" if accepted_enough else "partial",
        "overall_score": 100 if accepted_enough else max(0, len(articles) * 25),
        "claim_to_evidence_coverage": report["claim_to_evidence_coverage"] if report else 0.0,
        "unsupported_grounded_claims": report["unsupported_grounded_claims"] if report else 0,
        "review_method": REVIEW_METHOD,
    }
    run["report_quality"] = quality
    run["prototype"] = {
        **run["prototype"],
        "corpus_count": len(articles),
        "pipeline_status": "completed" if accepted_enough else "incomplete",
        "report_status": "ready" if report else "unavailable",
        "reviewed_paper_count": len(articles),
        "evidence_span_count": len(evidence),
        "report_quality": quality,
        "provider": _provider_summary(),
        "real_corpus": {
            "source": SOURCE,
            "query_id": QUERY_ID,
            "discovered_candidate_count": len(candidates),
            "license_eligible_count": len(rights_eligible),
            "selected_article_count": len(articles),
            "network_request_count": network.request_count,
            "total_downloaded_bytes": network.total_downloaded_bytes,
            "provider_call_count": 0,
            "certification_status": "complete" if accepted_enough else "insufficient_articles",
            "record_digest": record["record_digest"],
            "replay_status": "not_run",
        },
    }
    save_run_record(run)
    return record


def start_real_corpus_run(*, owner_user_id: str | None) -> dict[str, Any]:
    run = create_real_corpus_run(owner_user_id=owner_user_id)
    execute_real_corpus_run(run["run_id"])
    return real_corpus_run_summary(load_run_record(run["run_id"]))


def create_curated_real_corpus_run(
    *,
    source_run_id: str,
    selected_pmcids: list[str],
    owner_user_id: str | None,
    workspace_context: dict[str, Any],
) -> dict[str, Any]:
    """Create a zero-network deterministic run from a prior accepted v0.3 corpus.

    Source XML remains inside the disposable runtime.  It is reparsed into the
    new run so every review, evidence ID, claim, and report belongs to the new
    run rather than borrowing a parent-run evidence identity.
    """

    prototype.require_prototype_mode()
    source_record = load_public_record(source_run_id)
    selected = list(dict.fromkeys(str(value or "").strip() for value in selected_pmcids if str(value or "").strip()))
    if len(selected) < MIN_PASS_ARTICLES or len(selected) > MAX_ACCEPTED_ARTICLES:
        raise RealCorpusError("real_corpus_curated_selection_invalid")
    source_articles = {
        str(article.get("pmcid") or ""): article
        for article in source_record.get("articles") or []
        if isinstance(article, dict) and article.get("pmcid")
    }
    if set(selected) - set(source_articles):
        raise RealCorpusError("real_corpus_curated_selection_unknown")
    if any(
        source_articles[pmcid].get("license_type") not in {"CC0", "CC BY"}
        or not source_articles[pmcid].get("rights_accepted")
        or not source_articles[pmcid].get("full_text_retrieved")
        for pmcid in selected
    ):
        raise RealCorpusError("real_corpus_curated_selection_untrusted")

    request_payload = {
        "topic": TOPIC,
        "query_id": QUERY_ID,
        "query_sha256": QUERY_SHA256,
        "query_cutoff": QUERY_CUTOFF,
        "source": SOURCE,
        "selected_pmcids": selected,
        "source_run_id": source_run_id,
        "provider_mode": "disabled",
        "network_mode": "cached_source_only",
    }
    parent_run_id = str(workspace_context.get("parent_run_id") or source_run_id)
    run = create_run_record(
        topic=TOPIC,
        request_payload=request_payload,
        source="prototype_v0_4:curated_snapshot",
        write_legacy=False,
        lineage={
            "parent_run_id": parent_run_id,
            "root_run_id": (load_run_record(parent_run_id).get("lineage") or {}).get("root_run_id") or parent_run_id,
            "lineage_mode": "curated_corpus_snapshot",
            "source_paper_ids": selected,
            "created_from_profile_snapshot": True,
            "compare_baseline_run_id": parent_run_id,
            "branch_reason": "corpus_curation",
            "created_at": iso_now_utc(),
            "notes": "Deterministic zero-network run from an immutable corpus snapshot.",
        },
        owner_user_id=owner_user_id,
    )
    run_id = str(run["run_id"])
    run["status"] = "running"
    run["started_at"] = iso_now_utc()
    run["topic_profile_id"] = str(workspace_context.get("topic_id") or "prototype-pmc-open-access-v1")
    run["steps"] = [_make_step(stage) for stage in PROGRESS_STAGES]
    run["prototype"] = {
        "schema_version": REAL_CORPUS_SCHEMA,
        "label": "Prototype v0.4 · curated real PMC snapshot",
        "corpus_kind": "real_open_access",
        "corpus_count": len(selected),
        "pipeline_status": "running",
        "report_status": "pending",
        "reviewed_paper_count": 0,
        "evidence_span_count": 0,
        "provider": _provider_summary(),
        "real_corpus": {
            "source": SOURCE,
            "query_id": QUERY_ID,
            "network_request_count": 0,
            "provider_call_count": 0,
            "certification_status": "cached_curated_run_in_progress",
            "source_run_id": source_run_id,
        },
    }
    save_run_record(run)

    try:
        articles: list[dict[str, Any]] = []
        source_paths = build_run_paths(source_run_id)
        for pmcid in selected:
            source_dir = source_paths.raw_dir / "real-corpus" / pmcid
            source_path = source_dir / "source.xml"
            rights_path = source_dir / "rights.json"
            if not source_path.is_file() or not rights_path.is_file():
                raise RealCorpusError("real_corpus_curated_source_artifact_missing")
            try:
                rights = json.loads(rights_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise RealCorpusError("real_corpus_curated_rights_invalid") from None
            if rights.get("record_digest") != _with_record_digest(rights).get("record_digest"):
                raise RealCorpusError("real_corpus_curated_rights_digest_mismatch")
            public = source_articles[pmcid]
            candidate = {
                "uid": pmcid.removeprefix("PMC"),
                "pmcid": pmcid,
                "pmid": public.get("pmid"),
                "doi": public.get("doi"),
                "title": public.get("title"),
                "publication_date": public.get("publication_date"),
                "language": "English",
                "article_type": "Journal Article",
                "authors": public.get("authors") or [],
                "journal": public.get("journal") or "",
                "relevance_score": 0,
            }
            articles.append(
                _persist_article(
                    run_id,
                    candidate,
                    rights,
                    MetadataFetchResult(
                        body=source_path.read_bytes(),
                        content_type="application/xml",
                        status_code=200,
                    ),
                )
            )

        evidence = [item for article in articles for item in article["evidence"]]
        evidence_set_digest = _sha256_bytes(
            _canonical_bytes(
                sorted(
                    (
                        {
                            "evidence_id": item["evidence_id"],
                            "paper_id": item["paper_id"],
                            "source_content_digest": item["source_content_digest"],
                        }
                        for item in evidence
                    ),
                    key=lambda item: item["evidence_id"],
                )
            )
        )
        network = PmcNetworkClient()
        report = build_report(run_id, articles, network)
        report["evidence_set_digest"] = evidence_set_digest
        report_path = build_run_paths(run_id).reports_dir / REPORT_FILE_NAME
        _atomic_write_bytes(report_path, report["markdown"].encode("utf-8"), run_id=run_id)
        reporting.save_outline(run_id, report["outline_sections"])
        triage_path = build_run_paths(run_id).triage_dir / "triage.jsonl"
        paper_occurrences.write_triage_records(triage_path, [_triage_item(article) for article in articles])

        for stage in PROGRESS_STAGES:
            _set_step(run, stage, "completed", "Completed from the selected immutable corpus snapshot.")
        run = load_run_record(run_id)
        run["status"] = "completed"
        run["finished_at"] = iso_now_utc()
        run["error"] = None
        run.setdefault("counts", {}).update(
            {
                "triage_items": len(articles),
                "reviewed_items": len(articles),
                "full_text_reviewed_items": len(articles),
                "evidence_spans": len(evidence),
                "grounded_claims": len(report["claims"]),
                "real_corpus_items": len(articles),
            }
        )
        run.setdefault("artifacts", {}).update(
            {
                "triage_jsonl": str(triage_path),
                "report_markdown": str(report_path),
                "outline_json": str(build_run_paths(run_id).reports_dir / reporting.OUTLINE_FILENAME),
                "preview_markdown": str(build_run_paths(run_id).reports_dir / reporting.PREVIEW_FILENAME),
            }
        )
        quality = {
            "status": "ready",
            "overall_score": 100,
            "claim_to_evidence_coverage": report["claim_to_evidence_coverage"],
            "unsupported_grounded_claims": report["unsupported_grounded_claims"],
            "review_method": REVIEW_METHOD,
        }
        run["report_quality"] = quality
        run["workspace"] = {
            **{
                key: workspace_context.get(key)
                for key in (
                    "schema_version",
                    "topic_id",
                    "snapshot_id",
                    "settings_version",
                    "settings_digest",
                    "source_manifest_digest",
                )
            },
            "evidence_set_digest": evidence_set_digest,
            "generation_mode": REVIEW_METHOD,
            "provider_calls": 0,
        }
        run["prototype"] = {
            **run["prototype"],
            "pipeline_status": "completed",
            "report_status": "ready",
            "reviewed_paper_count": len(articles),
            "evidence_span_count": len(evidence),
            "report_quality": quality,
            "provider": _provider_summary(),
            "real_corpus": {
                "source": SOURCE,
                "query_id": QUERY_ID,
                "selected_article_count": len(articles),
                "network_request_count": 0,
                "total_downloaded_bytes": 0,
                "provider_call_count": 0,
                "certification_status": "cached_curated_run_complete",
                "source_run_id": source_run_id,
            },
        }
        save_run_record(run)

        from app.services import report_versions

        version = report_versions.record_completed_report_version(
            run_id,
            source="curated_snapshot_deterministic_report",
            source_artifact_path=str(report_path),
            content=report["markdown"],
            generation_metadata={
                "generation_mode": REVIEW_METHOD,
                "evidence_references": [item["evidence_id"] for item in evidence],
                "display_label": "Deterministic curated snapshot report",
            },
        )
        if not isinstance(version, dict):
            raise RealCorpusError("real_corpus_curated_report_version_missing")
        report["report_version_id"] = version["version_id"]
        record = {
            "schema_version": PUBLIC_RECORD_SCHEMA,
            "run_id": run_id,
            "topic": TOPIC,
            "query": {
                "query_id": QUERY_ID,
                "normalized_query": QUERY,
                "query_sha256": QUERY_SHA256,
                "date_cutoff": QUERY_CUTOFF,
                "discovery_timestamp": source_record.get("query", {}).get("discovery_timestamp"),
                "result_count": source_record.get("query", {}).get("result_count"),
                "selected_article_ids": [article["parsed"]["pmcid"] for article in articles],
                "selection_algorithm_version": SELECTION_ALGORITHM_VERSION,
            },
            "source": SOURCE,
            "status": "complete",
            "failure_category": None,
            "discovery_result_count": int(source_record.get("discovery_result_count") or 0),
            "discovered_candidate_count": int(source_record.get("discovered_candidate_count") or 0),
            "license_eligible_count": len(articles),
            "selected_article_count": len(articles),
            "articles": [_public_article(article) for article in articles],
            "rejected_candidates": [],
            "evidence": evidence,
            "evidence_category_counts": dict(Counter(item["claim_category"] for item in evidence)),
            "limitation_uncertainty_evidence_present": any(
                item["claim_category"] == "limitation_uncertainty" for item in evidence
            ),
            "invalid_or_stale_evidence_count": 0,
            "review_count": len(articles),
            "usable_section_count": sum(article["parsed"]["usable_section_count"] for article in articles),
            "usable_paragraph_count": sum(article["parsed"]["usable_paragraph_count"] for article in articles),
            "claims": report["claims"],
            "report": {
                key: report[key]
                for key in (
                    "report_id",
                    "report_version_id",
                    "generation_mode",
                    "report_digest",
                    "evidence_set_digest",
                    "grounded_claim_count",
                    "inference_count",
                    "proposal_count",
                    "uncertainty_count",
                    "unsupported_grounded_claims",
                    "unknown_evidence_ids",
                    "cross_run_evidence_ids",
                    "abstract_only_body_claims",
                    "license_rejected_article_references",
                    "stale_evidence_references",
                    "claim_to_evidence_coverage",
                    "license_appendix",
                )
            },
            "network": {
                "request_count": 0,
                "total_downloaded_bytes": 0,
                "retries": 0,
                "non_allowlisted_requests": 0,
                "request_records": [],
            },
            "provider_calls": 0,
            "source_run_id": source_run_id,
            "snapshot_id": workspace_context.get("snapshot_id"),
            "replay": None,
        }
        record = _persist_public_record(run_id, record)
        manifest_ref = _write_manifest(run_id, record)
        run = load_run_record(run_id)
        run["artifacts"]["real_corpus_record"] = str(_public_record_path(run_id))
        run["artifacts"]["real_corpus_manifest"] = manifest_ref
        run["workspace"]["report_version_id"] = version["version_id"]
        run["prototype"]["real_corpus"]["record_digest"] = record["record_digest"]
        save_run_record(run)
        return {
            **real_corpus_run_summary(run),
            "report_version_id": version["version_id"],
            "snapshot_id": workspace_context.get("snapshot_id"),
            "evidence_set_digest": evidence_set_digest,
        }
    except Exception as exc:
        failed = load_run_record(run_id)
        failed["status"] = "failed"
        failed["finished_at"] = iso_now_utc()
        failed["error"] = getattr(exc, "code", "real_corpus_curated_run_failed")
        failed["prototype"] = {
            **(failed.get("prototype") or {}),
            "pipeline_status": "incomplete",
            "report_status": "unavailable",
        }
        save_run_record(failed)
        if isinstance(exc, RealCorpusError):
            raise
        raise RealCorpusError("real_corpus_curated_run_failed") from exc


def check_for_corpus_updates(*, client: PmcNetworkClient | None = None) -> dict[str, Any]:
    """Run the fixed connector's metadata-and-rights refresh probe without a report."""

    prototype.require_prototype_mode()
    if not network_enabled():
        raise RealCorpusError("real_corpus_network_disabled")
    network = client or PmcNetworkClient()
    discovery = network.fetch(
        _query_url(
            ESEARCH_ENDPOINT,
            {"db": "pmc", "term": QUERY, "retmode": "json", "retmax": MAX_CANDIDATES, "sort": "relevance"},
        ),
        purpose="fixed_query_refresh_discovery",
        max_bytes=MAX_METADATA_RESPONSE_BYTES,
        accept="application/json",
    )
    discovery_count, candidate_ids = parse_esearch(discovery.body)
    if not candidate_ids:
        raise RealCorpusError("real_corpus_no_discovery_candidates")
    summary = network.fetch(
        _query_url(
            ESUMMARY_ENDPOINT,
            {"db": "pmc", "id": ",".join(candidate_ids), "retmode": "json", "version": "2.0"},
        ),
        purpose="bounded_refresh_metadata",
        max_bytes=MAX_METADATA_RESPONSE_BYTES,
        accept="application/json",
    )
    candidates = parse_esummary(summary.body, candidate_ids)[:MAX_CANDIDATES]
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_pmcids: set[str] = set()
    seen_works: set[str] = set()
    for candidate in candidates:
        rejection = _candidate_is_eligible(candidate) or candidate_identity_rejection(candidate, seen_pmcids, seen_works)
        if rejection:
            continue
        rights_response = network.fetch(
            _oai_url(candidate, "oai_dc"),
            purpose=f"refresh_rights_metadata:{candidate['pmcid']}",
            max_bytes=MAX_METADATA_RESPONSE_BYTES,
            accept="application/xml,text/xml",
        )
        try:
            rights = parse_oai_dc(rights_response.body, candidate, retrieved_at=iso_now_utc())
        except RealCorpusError:
            continue
        if rights["reuse_allowed"]:
            eligible.append((candidate, rights))
    eligible.sort(key=lambda pair: _candidate_sort_key(pair[0]))
    selected = eligible[:MAX_ACCEPTED_ARTICLES]
    return {
        "query_id": QUERY_ID,
        "query_digest": QUERY_SHA256,
        "candidate_count": len(candidates),
        "discovery_result_count": discovery_count,
        "license_eligible_count": len(eligible),
        "articles": [
            {
                "pmcid": candidate["pmcid"],
                "pmid": candidate.get("pmid"),
                "doi": candidate.get("doi"),
                "title": candidate["title"],
                "publication_date": candidate["publication_date"],
                "license_type": rights["license_type"],
                "license_url": rights["canonical_license_url"],
                "rights_accepted": True,
            }
            for candidate, rights in selected
        ],
        "network": {
            "request_count": network.request_count,
            "total_downloaded_bytes": network.total_downloaded_bytes,
            "retries": 0,
            "non_allowlisted_requests": 0,
            "request_records": network.request_records,
        },
        "provider_calls": 0,
    }


def load_public_record(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    prototype_record = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    if prototype_record.get("schema_version") != REAL_CORPUS_SCHEMA:
        raise RealCorpusError("real_corpus_run_tag_missing")
    path = _public_record_path(run_id)
    if not path.is_file():
        raise RealCorpusError("real_corpus_record_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RealCorpusError("real_corpus_record_invalid") from None
    expected = _with_record_digest(payload)["record_digest"]
    if payload.get("record_digest") != expected:
        raise RealCorpusError("real_corpus_record_digest_mismatch")
    return payload


def get_evidence_detail(run_id: str, evidence_id: str) -> dict[str, Any]:
    record = load_public_record(run_id)
    evidence = next((item for item in record.get("evidence", []) if item.get("evidence_id") == evidence_id), None)
    if not isinstance(evidence, dict):
        raise RealCorpusError("real_corpus_evidence_not_found")
    article = next((item for item in record.get("articles", []) if item.get("pmcid") == evidence.get("paper_id")), None)
    if not isinstance(article, dict):
        raise RealCorpusError("real_corpus_evidence_paper_missing")
    return {
        "run_id": run_id,
        "evidence_id": evidence["evidence_id"],
        "paper_id": evidence["paper_id"],
        "full_text_artifact_id": evidence["full_text_artifact_id"],
        "source_content_digest": evidence["source_content_digest"],
        "section_id": evidence["section_id"],
        "section_path": evidence["section_path"],
        "paragraph_id": evidence["paragraph_id"],
        "paragraph_ordinal": evidence["paragraph_ordinal"],
        "bounded_source_excerpt": evidence["normalized_quote"],
        "claim_category": evidence["claim_category"],
        "stale": evidence["stale"],
        "paper": {
            key: article.get(key)
            for key in ("title", "pmcid", "pmid", "doi", "license_type", "license_url", "content_digest", "review_method")
        },
        "source_kind": "real_pmc_open_access_full_text",
    }


def replay_real_corpus_run(run_id: str) -> dict[str, Any]:
    record = load_public_record(run_id)
    if int(record.get("selected_article_count") or 0) < 1 or not isinstance(record.get("report"), dict):
        raise RealCorpusError("real_corpus_replay_source_incomplete")
    run = load_run_record(run_id)
    source_identities: list[dict[str, Any]] = []
    cache_root = build_run_paths(run_id).raw_dir / "real-corpus" / "cache"
    cache_files = sorted(cache_root.glob("*.json"))
    if len(cache_files) != int(record["selected_article_count"]):
        raise RealCorpusError("real_corpus_cache_count_mismatch")
    for cache_path in cache_files:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RealCorpusError("real_corpus_cache_corrupt") from None
        if cache.get("record_digest") != _with_record_digest(cache)["record_digest"] or not cache.get("complete"):
            raise RealCorpusError("real_corpus_cache_corrupt")
        source_path = _resolve_portable_ref(run_id, cache["source_artifact_ref"])
        parsed_path = _resolve_portable_ref(run_id, cache["parsed_artifact_ref"])
        if not source_path.is_file() or not parsed_path.is_file():
            raise RealCorpusError("real_corpus_cache_item_missing")
        if _sha256_bytes(source_path.read_bytes()) != cache["raw_digest"]:
            raise RealCorpusError("real_corpus_cache_source_digest_mismatch")
        if _sha256_bytes(parsed_path.read_bytes()) != cache["parsed_artifact_sha256"]:
            raise RealCorpusError("real_corpus_cache_parsed_digest_mismatch")
        source_identities.append(
            {
                "pmcid": cache["pmcid"],
                "raw_digest": cache["raw_digest"],
                "content_digest": cache["normalized_content_digest"],
                "license_record_digest": cache["license_record_digest"],
            }
        )
    report_path = build_run_paths(run_id).reports_dir / REPORT_FILE_NAME
    if not report_path.is_file() or _sha256_bytes(report_path.read_bytes()) != record["report"]["report_digest"]:
        raise RealCorpusError("real_corpus_replay_report_digest_mismatch")
    replay = _with_record_digest(
        {
            "schema_version": REPLAY_SCHEMA,
            "run_id": run_id,
            "status": "passed",
            "network_enabled": False,
            "network_request_count": 0,
            "selected_article_ids": sorted(item["pmcid"] for item in source_identities),
            "source_identities": sorted(source_identities, key=lambda item: item["pmcid"]),
            "evidence_identity_count": len(record["evidence"]),
            "report_digest": record["report"]["report_digest"],
            "allowed_metadata_difference": "replay_timestamp_only",
            "replayed_at": iso_now_utc(),
        }
    )
    updated_record = _persist_public_record(run_id, {**record, "replay": replay})
    run["prototype"]["real_corpus"] = {
        **run["prototype"]["real_corpus"],
        "replay_status": "passed",
        "replay_network_request_count": 0,
        "record_digest": updated_record["record_digest"],
    }
    save_run_record(run)
    return replay


def real_corpus_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    prototype_record = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    real = prototype_record.get("real_corpus") if isinstance(prototype_record.get("real_corpus"), dict) else {}
    return {
        "run_id": str(run.get("run_id") or ""),
        "topic_profile_id": run.get("topic_profile_id"),
        "topic": str(run.get("topic") or TOPIC),
        "status": str(run.get("status") or "unknown"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "lineage": run.get("lineage") or {},
        "counts": {
            "papers": int((run.get("counts") or {}).get("real_corpus_items") or 0),
            "reviewed": int((run.get("counts") or {}).get("reviewed_items") or 0),
            "evidence_spans": int((run.get("counts") or {}).get("evidence_spans") or 0),
        },
        "report_status": prototype_record.get("report_status") or "pending",
        "provider": prototype_record.get("provider") or _provider_summary(),
        "report_quality": run.get("report_quality") or prototype_record.get("report_quality") or {},
        "real_corpus": real,
    }


def describe_real_corpus() -> dict[str, Any]:
    prototype_status = prototype.prototype_mode_status()
    latest: dict[str, Any] | None = None
    if prototype_status["enabled"]:
        runs = [
            run
            for run in list_run_records(limit=None)
            if isinstance(run.get("prototype"), dict) and run["prototype"].get("schema_version") == REAL_CORPUS_SCHEMA
        ]
        latest = real_corpus_run_summary(runs[0]) if runs else None
    return {
        "schema_version": REAL_CORPUS_SCHEMA,
        "available": bool(prototype_status["enabled"]),
        "network_enabled": bool(prototype_status["enabled"] and network_enabled()),
        "external_network_default": "disabled",
        "topic": TOPIC,
        "query_id": QUERY_ID,
        "query_cutoff": QUERY_CUTOFF,
        "source": "PMC Open Access",
        "allowed_hosts": sorted(ALLOWED_HOSTS),
        "maximum_articles": MAX_ACCEPTED_ARTICLES,
        "maximum_candidates": MAX_CANDIDATES,
        "maximum_http_requests": MAX_HTTP_REQUESTS,
        "license_policy": ["CC0", "CC BY"],
        "live_provider": "disabled",
        "provider_calls": 0,
        "deterministic_fallback_available": True,
        "latest_run": latest,
    }

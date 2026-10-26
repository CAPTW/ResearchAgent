from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import date, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import quote, urlparse

from app.auth.resource_access import ResourceActor
from app.domain import constants
from app.repositories import paper_collections, paper_reviews
from app.repositories.files import ensure_dir, iso_now_utc, safe_filename, slugify, write_json
from app.repositories.shared_state import SharedStateError
from app.services import paper_occurrences, topic_dashboard, topic_profiles

SUPPORTED_SORTS = {"published_desc", "title_asc", "provider_asc", "review_state"}
SUPPORTED_REVIEW_STATES = {
    "not_reviewed",
    "abstract_reviewed",
    "full_text_available",
    "full_text_reviewed",
}
SUPPORTED_ACCESS_STATES = {"open_access", "restricted", "declared", "unknown"}
PAPER_BANK_EXPORT_ROOT: Path | None = None
PAPER_BANK_EXPORT_COLUMNS = (
    "paper_version_id",
    "canonical_paper_id",
    "title",
    "authors",
    "provider",
    "publication_date",
    "publication_year",
    "review_state",
    "access_state",
    "license",
    "review_summary_state",
    "full_text_state",
    "evidence_count",
    "source_url",
)
ARXIV_ID = re.compile(r"(?<!\d)(?P<base>\d{4}\.\d{4,5})(?P<version>v\d+)?", re.IGNORECASE)
DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


class PaperBankError(RuntimeError):
    code = "paper_bank_failed"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(self.code if not detail else f"{self.code}: {detail}")


class PaperBankUnavailable(PaperBankError):
    code = "paper_bank_unavailable"


class PaperVersionNotFound(PaperBankError):
    code = "paper_version_not_found"


class PaperBankQueryInvalid(PaperBankError):
    code = "paper_bank_query_invalid"


# A narrow alias keeps catch clauses readable while preserving the concrete
# domain exceptions raised by the collection repository.
PaperCollectionErrorAlias = paper_collections.PaperCollectionError


def _digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _stable_id(prefix: str, identity: str) -> str:
    return f"{prefix}-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _load_topic_profile(topic_id: str, actor: ResourceActor | None = None) -> dict[str, Any]:
    return topic_profiles.get_topic_profile(topic_id, actor=actor)


def _load_topic_discoveries(
    profile: dict[str, Any],
    actor: ResourceActor | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    # Reuse the current run visibility and triage loading boundary. The facade
    # changes identity semantics without reissuing or rewriting run-paper IDs.
    return topic_dashboard._topic_items(str(profile.get("topic") or ""),
        profile_id=str(profile["profile_id"]), max_runs=1_000, actor=actor)


def _load_topic_reviews(topic_id: str) -> list[dict[str, Any]]:
    return paper_reviews.list_reviews(topic_id=topic_id, limit=10_000, read_only=True)


def _authors(value: Any) -> list[str]:
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                result.append(name)
        return result
    if isinstance(value, str):
        separator = ";" if ";" in value else "," if "," in value else None
        return [part.strip() for part in value.split(separator) if part.strip()] if separator else ([value.strip()] if value.strip() else [])
    return []


def _provider(run: dict[str, Any], item: dict[str, Any]) -> str:
    direct = str(item.get("source_provider") or item.get("source") or "").strip().lower()
    if direct:
        return direct
    raw_request = run.get("request")
    raw_profile = run.get("profile")
    request: dict[str, Any] = raw_request if isinstance(raw_request, dict) else {}
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    raw_source_settings = request.get("source_settings")
    source_settings: dict[str, Any] = raw_source_settings if isinstance(raw_source_settings, dict) else {}
    return str(
        source_settings.get("defaultProvider")
        or request.get("source_provider")
        or profile.get("source_provider")
        or "unknown"
    ).strip().lower() or "unknown"


def _normalize_doi(value: Any) -> str | None:
    text = DOI_PREFIX.sub("", str(value or "").strip()).strip().lower().rstrip(".")
    return text if text.startswith("10.") and "/" in text else None


def _arxiv_identity(item: dict[str, Any]) -> tuple[str, str] | None:
    for value in (item.get("paper_id"), item.get("entry_id"), item.get("entry_url"), item.get("pdf_url")):
        match = ARXIV_ID.search(str(value or ""))
        if match:
            base = match.group("base").lower()
            return base, str(match.group("version") or "unversioned").lower()
    return None


def _explicit_version(item: dict[str, Any]) -> str | None:
    for key in ("version", "revision", "version_id", "paper_version_id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value.casefold()
    return None


def _fallback_occurrence_anchor(item: dict[str, Any]) -> str:
    return paper_occurrences.identity_anchor(item)


def _fallback_occurrence_paper_id(item: dict[str, Any]) -> str:
    return paper_occurrences.public_compatibility_id(item)


def _fallback_keys(run: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    run_id = str(run.get("run_id") or "unknown-run")
    occurrence_id = _fallback_occurrence_anchor(item)
    work_digest = _digest(
        {
            "domain": "xrml-paper-work-fallback-v3",
            "occurrenceId": occurrence_id,
            "runId": run_id,
        }
    )
    version_digest = _digest(
        {
            "domain": "xrml-paper-version-fallback-v3",
            "occurrenceId": occurrence_id,
            "runId": run_id,
            "version": _explicit_version(item) or "unversioned",
        }
    )
    return (
        f"observation:{run_id}:{work_digest}",
        f"observation-version:{run_id}:{version_digest}",
    )


def _identity(run: dict[str, Any], item: dict[str, Any]) -> dict[str, str | None]:
    provider = _provider(run, item)
    raw_id = str(item.get("paper_id") or item.get("id") or item.get("entry_id") or "").strip()
    doi = _normalize_doi(item.get("doi"))
    if not doi:
        entry_url = str(item.get("entry_url") or item.get("entry_id") or "")
        if "doi.org/" in entry_url.lower():
            doi = _normalize_doi(entry_url)
    arxiv = _arxiv_identity(item) if provider == "arxiv" else None
    if doi:
        work_key = f"doi:{doi}"
        version_token = _explicit_version(item) or f"{provider}:{raw_id.casefold() or doi}"
        version_key = f"{work_key}:version:{version_token}"
        native_id = raw_id or doi
    elif arxiv:
        base, version = arxiv
        work_key = f"arxiv:{base}"
        version_key = f"{work_key}:{version}"
        native_id = raw_id or f"{base}{'' if version == 'unversioned' else version}"
    elif raw_id:
        native_id = raw_id
        work_key = f"provider:{provider}:{raw_id.casefold()}"
        version_key = f"{work_key}:version:{_explicit_version(item) or raw_id.casefold()}"
    else:
        native_id = None
        work_key, version_key = _fallback_keys(run, item)
    return {
        "provider": provider,
        "nativeId": native_id,
        "doi": doi,
        "workKey": work_key,
        "versionKey": version_key,
    }


def _compatibility_paper_id(item: dict[str, Any], identity: dict[str, str | None]) -> str:
    paper_id = str(item.get("paper_id") or item.get("id") or "").strip()
    if paper_id or identity["nativeId"] is not None:
        return paper_id
    return _fallback_occurrence_paper_id(item)


def _safe_source_url(item: dict[str, Any]) -> tuple[str | None, str]:
    for raw_value in (item.get("entry_url"), item.get("pdf_url"), item.get("entry_id")):
        value = str(raw_value or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme.lower() == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return value, "available"
    return None, "unavailable"


def _publication_year(value: Any) -> int | None:
    """Return the year from the canonical persisted publication value only."""
    if isinstance(value, datetime):
        return value.year
    if isinstance(value, date):
        return value.year
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
        else:
            parsed_date = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None
    return parsed_date.year


def _year_query(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized == "unknown":
        return "unknown"
    if not re.fullmatch(r"\d{4}", normalized):
        raise PaperBankQueryInvalid("year_invalid")
    year = int(normalized)
    if year < 1 or year > 9999:
        raise PaperBankQueryInvalid("year_invalid")
    return str(year)


def _access_query(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in SUPPORTED_ACCESS_STATES:
        raise PaperBankQueryInvalid("open_access_invalid")
    return normalized


def _metadata_digest(item: dict[str, Any], identity: dict[str, str | None]) -> str:
    return _digest(
        {
            "identity": identity,
            "title": str(item.get("title") or "").strip(),
            "authors": _authors(item.get("authors")),
            "published": item.get("published"),
            "updated": item.get("updated"),
            "abstract": str(item.get("abstract") or "").strip(),
            "sourceUrl": _safe_source_url(item)[0],
            "license": item.get("license"),
            "rights": item.get("rights"),
            "openAccess": item.get("open_access") if "open_access" in item else item.get("is_open_access"),
        }
    )


def _review_state(review: dict[str, Any] | None, *, unavailable: bool) -> str:
    if unavailable:
        return "unavailable"
    if review is None:
        return "not_reviewed"
    status = str(review.get("full_text_status") or "unavailable")
    if status == "reviewed":
        return "full_text_reviewed"
    if status == "full_text_available":
        return "full_text_available"
    return "abstract_reviewed"


def _review_rank(review: dict[str, Any]) -> tuple[int, str]:
    rank = {
        "unavailable": 0,
        "abstract_only": 1,
        "full_text_available": 2,
        "reviewed": 3,
    }.get(str(review.get("full_text_status") or ""), 0)
    return rank, str(review.get("reviewed_at") or "")


def _project_catalog(
    discoveries: list[tuple[dict[str, Any], dict[str, Any]]],
    reviews: list[dict[str, Any]],
    *,
    review_unavailable: bool,
    collection_membership: dict[str, set[str]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], dict[str, str | None]]]] = {}
    seen_occurrences: set[tuple[str, str]] = set()
    for run, item in discoveries:
        identity = _identity(run, item)
        if "occurrence_id" in item or identity["nativeId"] is None:
            occurrence_id = paper_occurrences.require_authority(item)
            occurrence_key = (str(run.get("run_id") or ""), occurrence_id)
            if occurrence_key in seen_occurrences:
                raise paper_occurrences.OccurrenceIdentityDuplicate(occurrence_id)
            seen_occurrences.add(occurrence_key)
        group_key = (str(identity["workKey"]), str(identity["versionKey"]))
        grouped.setdefault(group_key, []).append((run, item, identity))

    review_map = {
        (str(review.get("run_id") or ""), str(review.get("paper_id") or "")): review
        for review in reviews
    }
    alias_versions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (_work_key, candidate_version_key), occurrences in grouped.items():
        for item_run, item, _item_identity in occurrences:
            run_id = str(item_run.get("run_id") or "")
            for alias in paper_occurrences.internal_legacy_aliases(item):
                alias_versions[(run_id, alias)].add(candidate_version_key)
    items: list[dict[str, Any]] = []
    for (work_key, version_key), occurrences in sorted(grouped.items()):
        occurrences.sort(
            key=lambda row: (
                str(row[0].get("created_at") or ""),
                str(row[1].get("updated") or row[1].get("published") or ""),
                str(row[0].get("run_id") or ""),
                str(row[1].get("paper_id") or ""),
            ),
            reverse=True,
        )
        run, raw_item, identity = occurrences[0]
        canonical_paper_id = _stable_id("paper", work_key)
        paper_version_id = _stable_id("version", version_key)
        occurrence_payload: list[dict[str, Any]] = []
        matching_reviews: list[dict[str, Any]] = []
        review_occurrence_keys: set[tuple[str, str]] = set()
        for item_run, item, item_identity in occurrences:
            run_id = str(item_run.get("run_id") or "")
            compatibility_id = _compatibility_paper_id(item, item_identity)
            occurrence_payload.append(
                {
                    "runId": run_id,
                    "paperId": compatibility_id,
                    "metadataDigest": _metadata_digest(item, item_identity),
                    "observedAt": str(item_run.get("created_at") or "") or None,
                }
            )
            aliases = (compatibility_id, *paper_occurrences.internal_legacy_aliases(item))
            for alias in dict.fromkeys(aliases):
                occurrence_key = (run_id, alias)
                if alias_versions.get(occurrence_key, {version_key}) != {version_key}:
                    continue
                review_occurrence_keys.add(occurrence_key)
                if occurrence_key in review_map:
                    matching_reviews.append(review_map[occurrence_key])
        selected_review = max(matching_reviews, key=_review_rank) if matching_reviews else None
        review_state = _review_state(selected_review, unavailable=review_unavailable)
        source_url, source_url_state = _safe_source_url(raw_item)
        rights = _rights(raw_item)
        publication_year = _publication_year(raw_item.get("published"))
        collection_ids = sorted(
            collection_id
            for collection_id, version_ids in collection_membership.items()
            if paper_version_id in version_ids
        )
        items.append(
            {
                "canonicalPaperId": canonical_paper_id,
                "paperVersionId": paper_version_id,
                "sourceIdentity": {
                    "provider": identity["provider"],
                    "nativeId": identity["nativeId"],
                    "doi": identity["doi"],
                },
                "compatibility": {
                    "runId": str(run.get("run_id") or ""),
                    "paperId": _compatibility_paper_id(raw_item, identity),
                    "occurrences": occurrence_payload,
                },
                "metadataDigest": _metadata_digest(raw_item, identity),
                "title": str(raw_item.get("title") or "").strip() or "Untitled paper",
                "authors": _authors(raw_item.get("authors")),
                "publishedAt": raw_item.get("published") or raw_item.get("updated"),
                "sourceProvider": identity["provider"],
                "sourceUrl": source_url,
                "sourceUrlState": source_url_state,
                "theme": str(raw_item.get("theme") or "").strip() or None,
                "publicationYear": publication_year,
                "publicationYearState": "known" if publication_year is not None else "unknown",
                "accessState": rights["state"],
                "accessLabel": rights["label"],
                "review": {
                    "state": review_state,
                    "updatedAt": str(selected_review.get("reviewed_at") or "") or None if selected_review else None,
                    "evidenceCount": len(selected_review.get("evidence_spans") or []) if selected_review else 0,
                },
                "collectionIds": collection_ids,
                "_raw": raw_item,
                "_selectedReview": selected_review,
                "_reviewOccurrenceKeys": sorted(review_occurrence_keys),
                "_run": run,
            }
        )
    return items


def _matches_query(item: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    source_identity = item["sourceIdentity"]
    haystack = " ".join(
        [
            item["title"],
            " ".join(item["authors"]),
            str(item.get("theme") or ""),
            str(item.get("sourceProvider") or ""),
            str(source_identity.get("nativeId") or ""),
            str(source_identity.get("doi") or ""),
            str(item["_raw"].get("abstract") or ""),
        ]
    ).casefold()
    return query.casefold() in haystack


def _sort_items(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort not in SUPPORTED_SORTS:
        raise PaperBankQueryInvalid("sort_unsupported")
    if sort == "title_asc":
        return sorted(items, key=lambda item: (item["title"].casefold(), item["paperVersionId"]))
    if sort == "provider_asc":
        return sorted(
            items,
            key=lambda item: (str(item["sourceProvider"]), item["title"].casefold(), item["paperVersionId"]),
        )
    if sort == "review_state":
        return sorted(
            items,
            key=lambda item: (item["review"]["state"], item["title"].casefold(), item["paperVersionId"]),
        )
    return sorted(
        items,
        key=lambda item: (str(item.get("publishedAt") or ""), item["paperVersionId"]),
        reverse=True,
    )


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _resource_state(*, empty: bool = False, reason: str | None = None) -> dict[str, Any]:
    return {"state": "unavailable" if reason else "empty" if empty else "available", "reason": reason}


def _load_catalog(
    topic_id: str,
    *,
    actor: ResourceActor | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, set[str]], str | None]:
    try:
        profile = _load_topic_profile(topic_id, actor=actor)
        discoveries = _load_topic_discoveries(profile, actor=actor)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PaperBankUnavailable("paper_bank_source_unavailable") from exc

    review_unavailable = False
    try:
        reviews = _load_topic_reviews(topic_id)
    except (OSError, sqlite3.Error):
        reviews = []
        review_unavailable = True

    collection_reason: str | None = None
    try:
        membership = paper_collections.collection_membership(topic_id)
    except (PaperCollectionErrorAlias, SharedStateError):
        membership = {}
        collection_reason = "paper_collection_store_unavailable"

    try:
        catalog = _project_catalog(
            discoveries,
            reviews,
            review_unavailable=review_unavailable,
            collection_membership=membership,
        )
    except paper_occurrences.OccurrenceIdentityInvalid as exc:
        raise PaperBankUnavailable(exc.code) from exc
    return catalog, review_unavailable, membership, collection_reason


def _filtered_items(
    catalog: list[dict[str, Any]],
    *,
    q: str,
    provider: str | None,
    collection_id: str | None,
    review_state: str | None,
    year: str | None,
    access_state: str | None,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded = exclude or set()
    filtered = [item for item in catalog if _matches_query(item, q)]
    if provider and "provider" not in excluded:
        filtered = [item for item in filtered if item["sourceProvider"] == provider]
    if collection_id and "collection" not in excluded:
        filtered = [item for item in filtered if collection_id in item["collectionIds"]]
    if review_state and "review" not in excluded:
        filtered = [item for item in filtered if item["review"]["state"] == review_state]
    if year and "year" not in excluded:
        filtered = [
            item
            for item in filtered
            if (str(item["publicationYear"]) if item["publicationYear"] is not None else "unknown") == year
        ]
    if access_state and "access" not in excluded:
        filtered = [item for item in filtered if item["accessState"] == access_state]
    return filtered


def _facet_response(
    values: list[str],
    *,
    selected: str | None,
    labels: dict[str, str],
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    if unavailable_reason:
        return {
            "state": "unavailable",
            "options": [],
            "selected": selected,
            "unknownCount": 0,
            "reason": unavailable_reason,
        }
    counts = Counter(values)
    ordered = [value for value in labels if value in counts]
    if "unknown" in counts and "unknown" not in ordered:
        ordered.append("unknown")
    options = [
        {"value": value, "label": labels.get(value, value), "count": counts[value]}
        for value in ordered
    ]
    return {
        "state": "available" if options else "empty",
        "options": options,
        "selected": selected,
        "unknownCount": counts.get("unknown", 0),
        "reason": None,
    }


def _build_facets(
    catalog: list[dict[str, Any]],
    *,
    q: str,
    provider: str | None,
    collection_id: str | None,
    review_state: str | None,
    year: str | None,
    access_state: str | None,
    review_unavailable: bool,
) -> dict[str, Any]:
    base = _filtered_items(
        catalog,
        q=q,
        provider=provider,
        collection_id=collection_id,
        review_state=review_state,
        year=year,
        access_state=access_state,
    )
    year_items = _filtered_items(
        catalog,
        q=q,
        provider=provider,
        collection_id=collection_id,
        review_state=review_state,
        year=year,
        access_state=access_state,
        exclude={"year"},
    )
    review_items = _filtered_items(
        catalog,
        q=q,
        provider=provider,
        collection_id=collection_id,
        review_state=review_state,
        year=year,
        access_state=access_state,
        exclude={"review"},
    )
    access_items = _filtered_items(
        catalog,
        q=q,
        provider=provider,
        collection_id=collection_id,
        review_state=review_state,
        year=year,
        access_state=access_state,
        exclude={"access"},
    )
    year_values = [str(item["publicationYear"]) if item["publicationYear"] is not None else "unknown" for item in year_items]
    year_labels = {value: value for value in sorted({value for value in year_values if value != "unknown"}, reverse=True)}
    year_labels["unknown"] = "알 수 없음"
    review_labels = {
        "not_reviewed": "미검토",
        "abstract_reviewed": "초록 검토",
        "full_text_available": "전문 사용 가능",
        "full_text_reviewed": "전문 검토 완료",
    }
    access_labels = {
        "open_access": "Open access",
        "restricted": "Restricted",
        "declared": "Declared rights",
        "unknown": "Unknown rights",
    }
    year_facet = _facet_response(year_values, selected=year, labels=year_labels)
    review_facet = _facet_response(
        [str(item["review"]["state"]) for item in review_items],
        selected=review_state,
        labels=review_labels,
        unavailable_reason="paper_review_store_unavailable" if review_unavailable else None,
    )
    access_facet = _facet_response(
        [str(item["accessState"]) for item in access_items],
        selected=access_state,
        labels=access_labels,
    )
    # ``rights`` is an explicit synonym of ``access`` so API clients can use
    # either domain term without changing the source-backed state contract.
    return {
        "year": year_facet,
        "reviewLevel": review_facet,
        "access": access_facet,
        "rights": access_facet,
        "_filteredCount": len(base),
    }


def list_topic_papers(
    topic_id: str,
    *,
    q: str = "",
    provider: str | None = None,
    collection_id: str | None = None,
    review_state: str | None = None,
    year: str | None = None,
    open_access: str | None = None,
    sort: str = "published_desc",
    page: int = 1,
    page_size: int = 25,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    if review_state and review_state not in SUPPORTED_REVIEW_STATES:
        raise PaperBankQueryInvalid("review_state_unsupported")
    normalized_year = _year_query(year)
    normalized_access = _access_query(open_access)
    normalized_provider = provider.strip().lower() if provider else None
    if sort not in SUPPORTED_SORTS:
        raise PaperBankQueryInvalid("sort_unsupported")
    if page < 1 or page_size < 1 or page_size > 100:
        raise PaperBankQueryInvalid("pagination_invalid")
    catalog, review_unavailable, membership, collection_reason = _load_catalog(topic_id, actor=actor)
    if collection_id and collection_reason:
        raise PaperBankUnavailable(collection_reason)
    if collection_id and collection_id not in membership:
        raise PaperBankQueryInvalid("collection_not_found")

    provider_counts = Counter(str(item.get("sourceProvider") or "unknown") for item in catalog)
    normalized_query = q.strip()
    filtered = _filtered_items(
        catalog,
        q=normalized_query,
        provider=normalized_provider,
        collection_id=collection_id,
        review_state=review_state,
        year=normalized_year,
        access_state=normalized_access,
    )
    filtered = _sort_items(filtered, sort)
    total = len(filtered)
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    page_items = filtered[start : start + page_size]
    canonical_query = {
        "topicId": topic_id,
        "q": normalized_query,
        "provider": normalized_provider,
        "collectionId": collection_id or None,
        "year": normalized_year,
        "reviewState": review_state or None,
        "openAccess": normalized_access,
        "sort": sort,
        "page": page,
        "pageSize": page_size,
    }
    review_summary = None
    if not review_unavailable:
        states = Counter(item["review"]["state"] for item in filtered)
        review_summary = {
            "notReviewed": states["not_reviewed"],
            "abstractReviewed": states["abstract_reviewed"],
            "fullTextAvailable": states["full_text_available"],
            "fullTextReviewed": states["full_text_reviewed"],
        }
    return {
        "topicId": topic_id,
        "items": [_public_item(item) for item in page_items],
        "total": total,
        "returnedCount": len(page_items),
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "canonicalQuery": canonical_query,
        "queryDigest": _digest(canonical_query),
        "providerCounts": [
            {"provider": key, "label": key if key != "arxiv" else "arXiv", "paperCount": count}
            for key, count in sorted(provider_counts.items())
        ],
        "reviewResource": _resource_state(reason="paper_review_store_unavailable" if review_unavailable else None),
        "reviewSummary": review_summary,
        "collectionResource": _resource_state(reason=collection_reason),
        "resource": _resource_state(empty=not catalog),
        "facets": {
            key: value for key, value in _build_facets(
                catalog,
                q=normalized_query,
                provider=normalized_provider,
                collection_id=collection_id,
                review_state=review_state,
                year=normalized_year,
                access_state=normalized_access,
                review_unavailable=review_unavailable,
            ).items() if not key.startswith("_")
        },
        "unsupportedFilters": [
            {"id": "paper_type", "state": "unavailable", "reason": "source-backed 문서 유형이 저장되지 않았습니다."},
            {"id": "citation", "state": "unavailable", "reason": "관측된 인용 계약이 없습니다."},
            {"id": "impact", "state": "unavailable", "reason": "영향력 계약은 M3 범위입니다."},
        ],
    }


def _rights(raw_item: dict[str, Any]) -> dict[str, Any]:
    explicit_open = raw_item.get("open_access") if "open_access" in raw_item else raw_item.get("is_open_access")
    label = str(raw_item.get("rights") or raw_item.get("license") or "").strip() or None
    if explicit_open is True:
        return {"state": "open_access", "label": label}
    if explicit_open is False:
        return {"state": "restricted", "label": label}
    if label:
        return {"state": "declared", "label": label}
    return {"state": "unknown", "label": None}


def _evidence(
    review: dict[str, Any] | None,
    *,
    topic_id: str,
    paper_version_id: str,
) -> list[dict[str, Any]]:
    if not review:
        return []
    result: list[dict[str, Any]] = []
    for index, span in enumerate(review.get("evidence_spans") or []):
        if not isinstance(span, dict):
            continue
        explicit_evidence_id = str(
            span.get("evidence_span_id") or span.get("span_id") or span.get("evidence_id") or ""
        ).strip()
        evidence_id = explicit_evidence_id or f"span-{index + 1}"
        locator = span.get("locator") if isinstance(span.get("locator"), dict) else None
        content_digest = str(
            span.get("content_sha256") or span.get("text_sha256") or span.get("content_digest") or ""
        ).strip() or None
        # Only an explicitly persisted evidence identity is deep-linkable. An
        # index fallback remains visible but cannot silently redirect to an
        # arbitrary first span.
        destination = None
        destination_state = "unavailable"
        if explicit_evidence_id and review.get("run_id") and content_digest:
            destination = (
                f"/topics/{quote(topic_id, safe='')}/papers"
                f"?paper={quote(paper_version_id, safe='')}&evidence={quote(evidence_id, safe='')}"
            )
            destination_state = "available"
        result.append(
            {
                "id": evidence_id,
                "locator": locator,
                "contentDigest": content_digest,
                "text": str(span.get("text") or span.get("snippet") or "").strip() or None,
                "destination": destination,
                "destinationState": destination_state,
            }
        )
    return result


def get_topic_paper_detail(
    topic_id: str,
    paper_version_id: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    listing = list_topic_papers(topic_id, page_size=100, actor=actor)
    total_pages = int(listing["totalPages"])
    items = list(listing["items"])
    for page in range(2, total_pages + 1):
        items.extend(list_topic_papers(topic_id, page=page, page_size=100, actor=actor)["items"])
    public_item = next((item for item in items if item["paperVersionId"] == paper_version_id), None)
    if public_item is None:
        raise PaperVersionNotFound(paper_version_id)

    profile = _load_topic_profile(topic_id, actor=actor)
    discoveries = _load_topic_discoveries(profile, actor=actor)
    try:
        reviews = _load_topic_reviews(topic_id)
        review_unavailable = False
    except (OSError, sqlite3.Error):
        reviews = []
        review_unavailable = True
    try:
        membership = paper_collections.collection_membership(topic_id)
    except (PaperCollectionErrorAlias, SharedStateError):
        membership = {}
    catalog = _project_catalog(
        discoveries,
        reviews,
        review_unavailable=review_unavailable,
        collection_membership=membership,
    )
    internal = next(item for item in catalog if item["paperVersionId"] == paper_version_id)
    raw_item = internal["_raw"]
    review = internal["_selectedReview"]
    full_text_status = "unavailable" if review is None else str(review.get("full_text_status") or "unavailable")
    evidence = _evidence(
        review,
        topic_id=topic_id,
        paper_version_id=paper_version_id,
    )
    return {
        "topicId": topic_id,
        **public_item,
        "abstract": str(raw_item.get("abstract") or "").strip() or None,
        "abstractKind": "provider_abstract" if str(raw_item.get("abstract") or "").strip() else "unavailable",
        "fullText": {
            "state": full_text_status,
            "artifactRef": str(review.get("full_text_artifact_path") or "").strip() or None if review else None,
            "contentDigest": str(review.get("source_text_sha256") or "").strip() or None if review else None,
        },
        "rights": _rights(raw_item),
        "evidence": evidence,
        "evidenceResource": _resource_state(
            empty=not evidence,
            reason="paper_review_store_unavailable" if review_unavailable else None,
        ),
        "provenance": {
            "runIds": sorted(
                {occurrence["runId"] for occurrence in internal["compatibility"]["occurrences"] if occurrence["runId"]}
            ),
            "observedAt": internal["compatibility"]["occurrences"][0].get("observedAt"),
        },
    }


def _paper_bank_export_root() -> Path:
    configured = PAPER_BANK_EXPORT_ROOT
    return (Path(configured) if configured is not None else constants.STORAGE_ROOT.parent / "paper_bank_exports").resolve()


def _paper_bank_export_manifest_path(topic_id: str, export_id: str) -> Path:
    return _paper_bank_export_root() / slugify(topic_id) / export_id / "manifest.json"


def _csv_safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return f"'{text}"
    return text


def _export_row(item: dict[str, Any]) -> list[str]:
    raw = item["_raw"]
    review = item.get("_selectedReview") or {}
    rights = _rights(raw)
    publication_date = str(raw.get("published") or "").strip()
    publication_year = str(item["publicationYear"]) if item["publicationYear"] is not None else "unknown"
    return [
        _csv_safe_cell(item["paperVersionId"]),
        _csv_safe_cell(item["canonicalPaperId"]),
        _csv_safe_cell(item["title"]),
        _csv_safe_cell("; ".join(item["authors"])),
        _csv_safe_cell(item["sourceProvider"]),
        _csv_safe_cell(publication_date),
        _csv_safe_cell(publication_year),
        _csv_safe_cell(item["review"]["state"]),
        _csv_safe_cell(rights["state"]),
        _csv_safe_cell(raw.get("license") or raw.get("rights") or ""),
        _csv_safe_cell(str(review.get("review_source_type") or "unavailable")),
        _csv_safe_cell(str(review.get("full_text_status") or "unavailable")),
        _csv_safe_cell(item["review"]["evidenceCount"]),
        _csv_safe_cell(item["sourceUrl"] or ""),
    ]


def _write_export_bytes(path: Path, payload: bytes) -> None:
    """Publish bytes without platform newline translation."""
    ensure_dir(path.parent)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PaperBankUnavailable("paper_bank_export_write_failed") from exc


def export_topic_papers_csv(
    topic_id: str,
    *,
    q: str = "",
    provider: str | None = None,
    collection_id: str | None = None,
    year: str | None = None,
    review_state: str | None = None,
    open_access: str | None = None,
    sort: str = "published_desc",
    page: int = 1,
    page_size: int = 25,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    if review_state and review_state not in SUPPORTED_REVIEW_STATES:
        raise PaperBankQueryInvalid("review_state_unsupported")
    if sort not in SUPPORTED_SORTS:
        raise PaperBankQueryInvalid("sort_unsupported")
    if page < 1 or page_size < 1 or page_size > 100:
        raise PaperBankQueryInvalid("pagination_invalid")
    normalized_year = _year_query(year)
    normalized_access = _access_query(open_access)
    normalized_provider = provider.strip().lower() if provider else None
    normalized_query = q.strip()
    catalog, review_unavailable, membership, collection_reason = _load_catalog(topic_id, actor=actor)
    if collection_id and collection_reason:
        raise PaperBankUnavailable(collection_reason)
    if collection_id and collection_id not in membership:
        raise PaperBankQueryInvalid("collection_not_found")
    filtered = _filtered_items(
        catalog,
        q=normalized_query,
        provider=normalized_provider,
        collection_id=collection_id,
        review_state=review_state,
        year=normalized_year,
        access_state=normalized_access,
    )
    ordered = _sort_items(filtered, sort)
    canonical_query = {
        "topicId": topic_id,
        "q": normalized_query,
        "provider": normalized_provider,
        "collectionId": collection_id or None,
        "year": normalized_year,
        "reviewState": review_state or None,
        "openAccess": normalized_access,
        "sort": sort,
        "page": page,
        "pageSize": page_size,
    }
    query_digest = _digest(canonical_query)
    generated_at = iso_now_utc()
    export_id = f"paper-bank-{query_digest[:16]}-{hashlib.sha256(generated_at.encode()).hexdigest()[:8]}"
    file_name = safe_filename(f"paper-bank-{slugify(topic_id)}-{export_id}.csv") or "paper-bank-export.csv"
    root = _paper_bank_export_root()
    artifact_path = root / slugify(topic_id) / export_id / file_name
    try:
        artifact_path.resolve().relative_to(root)
    except ValueError as exc:
        raise PaperBankUnavailable("paper_bank_export_path_invalid") from exc

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(PAPER_BANK_EXPORT_COLUMNS)
    for item in ordered:
        writer.writerow(_export_row(item))
    csv_text = buffer.getvalue()
    csv_bytes = csv_text.encode("utf-8")
    _write_export_bytes(artifact_path, csv_bytes)
    content_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    artifact_ref = "/".join(("paper_bank_exports", slugify(topic_id), export_id, file_name))
    result = {
        "exportId": export_id,
        "topicId": topic_id,
        "canonicalQuery": canonical_query,
        "queryDigest": query_digest,
        "rowCount": len(ordered),
        "paperVersionIds": [str(item["paperVersionId"]) for item in ordered],
        "generatedAt": generated_at,
        "format": "csv",
        "mediaType": "text/csv; charset=utf-8",
        "fileName": file_name,
        "contentSha256": content_sha256,
        "artifactRef": artifact_ref,
        "downloadUrl": (
            f"/api/topics/{quote(topic_id, safe='')}/paper-bank/exports/"
            f"{quote(export_id, safe='')}/download"
        ),
        "reviewResourceState": "unavailable" if review_unavailable else "available",
    }
    write_json(_paper_bank_export_manifest_path(topic_id, export_id), result)
    return {key: value for key, value in result.items() if key != "reviewResourceState"}


def get_paper_bank_export(topic_id: str, export_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"paper-bank-[a-f0-9]{16}-[a-f0-9]{8}", export_id):
        raise PaperVersionNotFound("paper_bank_export_not_found")
    manifest_path = _paper_bank_export_manifest_path(topic_id, export_id)
    if not manifest_path.exists():
        raise PaperVersionNotFound("paper_bank_export_not_found")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PaperBankUnavailable("paper_bank_export_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("topicId") != topic_id:
        raise PaperBankUnavailable("paper_bank_export_topic_mismatch")
    artifact_ref = str(payload.get("artifactRef") or "")
    parts = artifact_ref.split("/")
    if len(parts) != 4 or parts[0] != "paper_bank_exports" or parts[1] != slugify(topic_id) or parts[2] != export_id:
        raise PaperBankUnavailable("paper_bank_export_path_invalid")
    root = _paper_bank_export_root()
    artifact_path = root.joinpath(*parts[1:])
    try:
        artifact_path.resolve().relative_to(root)
    except ValueError as exc:
        raise PaperBankUnavailable("paper_bank_export_path_invalid") from exc
    if not artifact_path.is_file():
        raise PaperBankUnavailable("paper_bank_export_missing")
    try:
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PaperBankUnavailable("paper_bank_export_unavailable") from exc
    if digest != payload.get("contentSha256"):
        raise PaperBankUnavailable("paper_bank_export_digest_mismatch")
    return payload


def resolve_paper_bank_export_download(topic_id: str, export_id: str) -> tuple[Path, dict[str, Any]]:
    payload = get_paper_bank_export(topic_id, export_id)
    artifact_ref = str(payload["artifactRef"])
    root = _paper_bank_export_root()
    path = root.joinpath(*artifact_ref.split("/")[1:])
    return path, payload


def resolve_paper_versions(
    topic_id: str,
    paper_version_ids: list[Any],
    *,
    actor: ResourceActor | None = None,
) -> tuple[list[dict[str, str]], int]:
    if not isinstance(paper_version_ids, list) or not paper_version_ids:
        raise PaperBankQueryInvalid("bulk_save_paper_version_ids_empty")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in paper_version_ids:
        if not isinstance(raw_id, str) or not raw_id.strip() or len(raw_id.strip()) > 200:
            raise PaperBankQueryInvalid("bulk_save_paper_version_id_invalid")
        value = raw_id.strip()
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    catalog, _review_unavailable, _membership, _collection_reason = _load_catalog(topic_id, actor=actor)
    by_version = {str(item["paperVersionId"]): item for item in catalog}
    if any(value not in by_version for value in normalized):
        raise PaperBankQueryInvalid("bulk_save_paper_version_id_not_found")
    return [
        {
            "canonicalPaperId": str(by_version[value]["canonicalPaperId"]),
            "paperVersionId": value,
        }
        for value in normalized
    ], len(normalized)

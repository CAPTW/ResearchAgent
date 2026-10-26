from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from app.repositories import evidence_search as repository
from app.services import evidence_search, paper_bank, paper_occurrences, topic_profiles

SEARCH_KINDS = ("topic", "paper", "evidence", "run")
UNAVAILABLE_REASONS = {
    "citation": "UNAVAILABLE_WITH_REASON_CITATION_OBSERVATION_AUTHORITY_NOT_IMPLEMENTED",
    "document_type": "UNAVAILABLE_WITH_REASON_SOURCE_BACKED_DOCUMENT_TYPE_NOT_PERSISTED",
}
_KIND_ORDER = {kind: index for index, kind in enumerate(SEARCH_KINDS)}


class SearchQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PaperProjectionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_text(value: Any, maximum: int = 240) -> str:
    text = evidence_search.normalize_query(str(value or ""))
    return text[:maximum] + ("…" if len(text) > maximum else "")


def _result_id(kind: str, identity: str) -> str:
    return f"{kind}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def _topic_id_for_name(name: str, profiles: Iterable[dict[str, Any]]) -> str | None:
    normalized = evidence_search.normalize_query(name)
    for profile in profiles:
        profile_id = str(profile.get("profile_id") or profile.get("id") or "").strip()
        values = (profile_id, profile.get("title"), profile.get("topic"), profile.get("name"))
        if normalized and normalized in {evidence_search.normalize_query(value) for value in values if value}:
            return profile_id or None
    return None


def _match(value: Any, normalized_query: str, tokens: tuple[str, ...]) -> tuple[int, str, list[str]] | None:
    text = evidence_search.normalize_query(value)
    if not text:
        return None
    if not normalized_query:
        return 9, "browse", []
    if text == normalized_query:
        return 1, "exact_label", ["label"]
    if normalized_query and text.startswith(normalized_query):
        return 2, "prefix", ["label"]
    if tokens and all(token in text for token in tokens):
        return 3, "all_tokens", ["label"]
    if normalized_query in text:
        return 4, "substring", ["label"]
    return None


class GlobalSearchService:
    """Deterministic federation over already persisted local projections."""

    def __init__(self, repository_factory=repository.EvidenceSearchRepository) -> None:
        self._repository_factory = repository_factory
        self._auth_enabled = False
        self._user_id: str | None = None
        self._topic_profile_cache: list[dict[str, Any]] | None = None
        self._paper_version_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}

    def _topic_profiles(self) -> list[dict[str, Any]]:
        if self._topic_profile_cache is None:
            try:
                payload = topic_profiles.list_topic_profiles()
                self._topic_profile_cache = [
                    dict(item) for item in payload.get("items", []) if isinstance(item, dict)
                ]
            except Exception:
                self._topic_profile_cache = []
        return self._topic_profile_cache

    def _search_bundle(self) -> repository.SearchBundle:
        visible_run_ids = evidence_search.EvidenceSearchService()._visible_run_ids(
            auth_enabled=self._auth_enabled,
            user_id=self._user_id,
        )
        return self._repository_factory().search(
            repository.RepositorySearchCriteria(visibleRunIds=visible_run_ids, occurrenceLimit=5)
        )

    def _paper_projection(self, topic_id: str, query: str) -> dict[tuple[str, str], dict[str, Any]]:
        if topic_id in self._paper_version_cache:
            return self._paper_version_cache[topic_id]
        mapping: dict[tuple[str, str], dict[str, Any]] = {}
        versions: dict[str, str] = {}
        total: int | None = None
        total_pages = 1
        page = 1
        while page <= max(1, total_pages):
            listing = paper_bank.list_topic_papers(topic_id, q=query, page=page, page_size=100)
            numbers = [listing.get(key) for key in ("page", "pageSize", "total", "totalPages", "returnedCount")]
            items = listing.get("items")
            if (
                any(type(value) is not int for value in numbers)
                or listing.get("topicId") != topic_id
                or numbers[0] != page or numbers[1] != 100
                or not isinstance(items, list)
            ):
                raise PaperProjectionError("global_search_paper_projection_pagination_invalid")
            count = listing["total"]
            pages = listing["totalPages"]
            expected_count = min(100, max(0, count - (page - 1) * 100))
            if (
                count < 0 or pages != (count + 99) // 100
                or listing["returnedCount"] != len(items) or len(items) != expected_count
                or (total is not None and (count != total or pages != total_pages))
            ):
                raise PaperProjectionError("global_search_paper_projection_pagination_invalid")
            total, total_pages = count, pages
            for item in items:
                if not isinstance(item, dict):
                    raise PaperProjectionError("global_search_paper_projection_identity_invalid")
                version_id = item.get("paperVersionId")
                canonical_id = item.get("canonicalPaperId")
                compatibility = item.get("compatibility")
                if (
                    not isinstance(version_id, str) or not re.fullmatch(r"version-[0-9a-f]{24}", version_id)
                    or not isinstance(canonical_id, str) or not re.fullmatch(r"paper-[0-9a-f]{24}", canonical_id)
                    or not isinstance(compatibility, dict)
                    or not isinstance(compatibility.get("occurrences"), list)
                ):
                    raise PaperProjectionError("global_search_paper_projection_identity_invalid")
                fingerprint = _digest(item)
                if version_id in versions and versions[version_id] != fingerprint:
                    raise PaperProjectionError("global_search_paper_projection_identity_conflict")
                versions[version_id] = fingerprint
                for occurrence in [compatibility, *compatibility["occurrences"]]:
                    if not isinstance(occurrence, dict):
                        raise PaperProjectionError("global_search_paper_projection_identity_invalid")
                    run_id, paper_id = occurrence.get("runId"), occurrence.get("paperId")
                    if not isinstance(run_id, str) or not run_id or not isinstance(paper_id, str) or not paper_id:
                        raise PaperProjectionError("global_search_paper_projection_identity_invalid")
                    key = (run_id, paper_id)
                    if key in mapping and mapping[key]["paperVersionId"] != version_id:
                        raise PaperProjectionError("global_search_paper_projection_identity_conflict")
                    mapping[key] = item
            page += 1
        # Publish only after every page and identity has passed validation.
        self._paper_version_cache[topic_id] = mapping
        return mapping

    def _canonical_paper(self, topic_id: str, run_id: str, paper_id: str, query: str) -> dict[str, Any] | None:
        mapping = self._paper_projection(topic_id, query)
        if not mapping:
            return None
        item = mapping.get((run_id, paper_id))
        if item is None:
            raise PaperProjectionError("global_search_paper_projection_identity_missing")
        return item

    @staticmethod
    def _normalise_kinds(kinds: Iterable[str] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
        raw = tuple(dict.fromkeys(str(kind).strip().casefold() for kind in (kinds or SEARCH_KINDS) if str(kind).strip()))
        if not raw:
            return SEARCH_KINDS, ()
        invalid = tuple(kind for kind in raw if kind not in SEARCH_KINDS)
        return tuple(kind for kind in SEARCH_KINDS if kind in raw), invalid

    def search(
        self,
        raw_query: str | None = "",
        *,
        topic_ids: Iterable[str] = (),
        kinds: Iterable[str] | None = None,
        limit: int = 25,
        auth_enabled: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        raw = str(raw_query or "")
        if len(raw) > 200:
            raise SearchQueryError("query_too_long", "The query may contain at most 200 characters.")
        if not 1 <= int(limit) <= 100:
            raise SearchQueryError("invalid_limit", "The result limit must be between 1 and 100.")
        requested, unsupported = self._normalise_kinds(kinds)
        requested_raw = tuple(dict.fromkeys(str(kind).strip().casefold() for kind in (kinds or SEARCH_KINDS) if str(kind).strip())) or SEARCH_KINDS
        normalized = evidence_search.normalize_query(raw)
        tokens = evidence_search.tokenize_query(normalized)
        scoped_topics = tuple(dict.fromkeys(str(topic).strip() for topic in topic_ids if str(topic).strip()))
        self._auth_enabled = auth_enabled
        self._user_id = user_id
        self._paper_version_cache.clear()
        profiles = self._topic_profiles()
        profile_by_id = {str(profile.get("profile_id") or profile.get("id") or ""): profile for profile in profiles}
        if scoped_topics:
            profiles = [profile for profile in profiles if str(profile.get("profile_id") or profile.get("id") or "") in scoped_topics]
            profile_by_id = {str(profile.get("profile_id") or profile.get("id") or ""): profile for profile in profiles}
        try:
            bundle = self._search_bundle()
        except Exception as exc:
            # Keep the response typed and explicit when a projection is not
            # readable; callers can show unavailable rather than a false zero.
            bundle = None
            bundle_error = str(exc.__class__.__name__)
        else:
            bundle_error = ""

        candidates: list[tuple[tuple[int, int, int, str, str], dict[str, Any]]] = []
        paper_error = ""

        def add(item: dict[str, Any], match: tuple[int, str, list[str]] | None, identity: str) -> None:
            if match is None:
                return
            rank, reason, fields = match
            item["rank"] = rank
            item["rankReason"] = reason
            item["matchedFields"] = fields
            candidates.append(((rank, -len(fields), _KIND_ORDER[item["kind"]], item["primaryLabel"].casefold(), identity), item))

        if "topic" in requested:
            for profile in profiles:
                topic_id = str(profile.get("profile_id") or profile.get("id") or "").strip()
                label = str(profile.get("title") or profile.get("topic") or topic_id).strip()
                identity = topic_id
                add(
                    {
                        "kind": "topic", "resultId": _result_id("topic", identity), "topicId": topic_id,
                        "primaryLabel": label, "secondaryText": _safe_text(profile.get("description")),
                        "route": f"/topics/{topic_id}/dashboard", "sourceState": "available",
                    },
                    _match(" ".join((topic_id, label, str(profile.get("description") or ""))), normalized, tokens),
                    identity,
                )

        if bundle is not None:
            topic_filter = set(scoped_topics)
            if "paper" in requested or "evidence" in requested:
                paper_rows = []
                evidence_rows = []
                titles_by_topic: dict[str, list[str]] = {}
                if "paper" in requested:
                    for paper_row in bundle.papers:
                        topic_id = _topic_id_for_name(paper_row.runTopic, profiles) or paper_row.runTopic
                        match = _match(" ".join((paper_row.title, paper_row.paperId, paper_row.theme, paper_row.runTopic)), normalized, tokens)
                        if match is None or (topic_filter and topic_id not in topic_filter):
                            continue
                        paper_rows.append((paper_row, topic_id, match))
                        titles_by_topic.setdefault(topic_id, []).append(paper_row.title.casefold())
                if "evidence" in requested:
                    for evidence_row in bundle.evidence:
                        topic_id = _topic_id_for_name(evidence_row.runTopic, profiles) or evidence_row.runTopic
                        match = _match(" ".join((evidence_row.evidenceId, evidence_row.title, evidence_row.snippet, evidence_row.citationText)), normalized, tokens)
                        if match is None or (topic_filter and topic_id not in topic_filter):
                            continue
                        evidence_rows.append((evidence_row, topic_id, match))
                        titles_by_topic.setdefault(topic_id, []).append(evidence_row.title.casefold())
                # Paper Bank q is substring-only and does not search run/topic
                # labels or evidence text. Push down a shared literal title
                # token when possible; keep federation-only matches unfiltered.
                queries = {
                    topic_id: next((token for token in tokens if all(token in title for title in titles)), "")
                    for topic_id, titles in titles_by_topic.items()
                }
                try:
                    if any(topic_id not in profile_by_id for topic_id in titles_by_topic):
                        raise PaperProjectionError("global_search_paper_projection_identity_missing")
                    for paper_row, topic_id, match in paper_rows:
                        canonical = self._canonical_paper(topic_id, paper_row.runId, paper_row.paperId, queries[topic_id])
                        if canonical is None:
                            continue
                        version_id = canonical["paperVersionId"]
                        identity = f"{topic_id}|{canonical['canonicalPaperId']}|{version_id}"
                        add(
                            {
                                "kind": "paper", "resultId": _result_id("paper", identity), "topicId": topic_id,
                                "paperVersionId": version_id, "runId": paper_row.runId, "primaryLabel": paper_row.title,
                                "secondaryText": _safe_text(f"{paper_row.runTopic} · {paper_row.theme} · {paper_row.paperId}"),
                                "route": f"/topics/{topic_id}/papers?paper={version_id}", "sourceState": "available",
                                "updatedAt": paper_row.runUpdatedAt,
                            },
                            match,
                            identity,
                        )
                    for evidence_row, topic_id, match in evidence_rows:
                        canonical = self._canonical_paper(topic_id, evidence_row.runId, evidence_row.paperId, queries[topic_id])
                        if canonical is None:
                            continue
                        version_id = canonical["paperVersionId"]
                        identity = f"{topic_id}|{evidence_row.runId}|{evidence_row.evidenceId}"
                        add(
                            {
                                "kind": "evidence", "resultId": _result_id("evidence", identity), "topicId": topic_id,
                                "paperVersionId": version_id, "evidenceId": evidence_row.evidenceId, "runId": evidence_row.runId,
                                "primaryLabel": evidence_row.evidenceId, "secondaryText": _safe_text(f"{evidence_row.title} · {evidence_row.snippet}"),
                                "route": f"/topics/{topic_id}/papers?paper={version_id}&evidence={evidence_row.evidenceId}",
                                "sourceState": "available", "updatedAt": evidence_row.runUpdatedAt,
                            },
                            match,
                            identity,
                        )
                except PaperProjectionError as exc:
                    paper_error = exc.code
                except paper_bank.PaperBankError as exc:
                    paper_error = exc.detail or exc.code
                except paper_occurrences.OccurrenceIdentityInvalid as exc:
                    paper_error = exc.code
                except (OSError, sqlite3.Error):
                    paper_error = "global_search_paper_projection_unavailable"
                if paper_error:
                    candidates = [row for row in candidates if row[1]["kind"] not in {"paper", "evidence"}]
            if "run" in requested:
                for run_row in bundle.runs:
                    topic_id = _topic_id_for_name(run_row.topic, profiles) or run_row.topic
                    if topic_filter and topic_id not in topic_filter:
                        continue
                    identity = run_row.runId
                    add(
                        {
                            "kind": "run", "resultId": _result_id("run", identity), "topicId": topic_id,
                            "runId": run_row.runId, "primaryLabel": run_row.runId,
                            "secondaryText": _safe_text(f"{run_row.topic} · {run_row.status} · {run_row.updatedAt}"),
                            "route": f"/runs/{run_row.runId}", "sourceState": "available", "updatedAt": run_row.updatedAt,
                        },
                        _match(" ".join((run_row.runId, run_row.topic, run_row.status)), normalized, tokens),
                        identity,
                    )

        candidates.sort(key=lambda row: row[0])
        seen_versions: set[tuple[str, str]] = set()
        unique_candidates = []
        for candidate in candidates:
            item = candidate[1]
            if item["kind"] == "paper":
                version_key = (item["topicId"], item["paperVersionId"])
                if version_key in seen_versions:
                    continue
                seen_versions.add(version_key)
            unique_candidates.append(candidate)
        candidates = unique_candidates
        items = [item for _sort_key, item in candidates[: int(limit)]]
        source_states: dict[str, str] = {}
        unavailable: dict[str, str] = {}
        for kind in requested:
            if bundle is None:
                source_states[kind] = "unavailable"
                unavailable[kind] = f"local_{kind}_projection_unavailable:{bundle_error}"
            elif paper_error and kind in {"paper", "evidence"}:
                source_states[kind] = "unavailable"
                unavailable[kind] = paper_error
            elif kind == "topic":
                source_states[kind] = "available" if profiles else "empty"
            else:
                source_states[kind] = "available" if any(item["kind"] == kind for item in items) else "empty"
        for kind in unsupported:
            source_states[kind] = "unavailable"
            unavailable[kind] = UNAVAILABLE_REASONS.get(kind, "UNAVAILABLE_WITH_REASON_UNSUPPORTED_RESULT_KIND")
        digest = _digest({"normalized": normalized, "kinds": list(requested_raw), "limit": int(limit), "topicIds": list(scoped_topics)})
        return {
            "query": {"raw": raw, "normalized": normalized, "tokens": list(tokens)},
            "queryDigest": digest,
            "requestedKinds": list(requested_raw), "limit": int(limit), "total": len(candidates),
            "items": items, "sourceStates": source_states, "unavailableKinds": unavailable,
        }

    @staticmethod
    def search_help() -> dict[str, Any]:
        return {
            "normalization": {"unicode": "NFKC", "case": "casefold", "controls": "space", "whitespace": "collapse_and_trim"},
            "limits": {"maxCharacters": 200, "maxTokens": 8},
            "syntax": {
                "plain": {"status": "supported", "interpretation": "normalized_literal_text"},
                "quotes": {"status": "literal", "interpretation": "literal"},
                "comma": {"status": "literal", "interpretation": "literal"},
                "boolean": {"status": "literal", "interpretation": "literal"},
                "wildcard": {"status": "literal", "interpretation": "literal"},
                "field": {"status": "literal", "interpretation": "literal"},
            },
            "scopes": {
                "globalSearch": "topics, Paper Bank papers, evidence projections and runs",
                "paperBank": "the selected topic's Paper Bank only; q is substring search",
            },
            "examples": [
                {"input": "ＳＨＡＲＥＤ  Systems", "normalized": "shared systems"},
                {"input": 'title:"agents"', "normalized": 'title:"agents"', "meaning": "literal; no field operator"},
            ],
        }

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.repositories import artifact_refs, evidence_search as repository, runs as run_repository
from app.repositories.files import read_json
from app.schemas.evidence_search import (
    ArtifactVerification,
    ArtifactVerificationStatus,
    CorpusKind,
    EvidenceDetailResponse,
    EvidenceOccurrence,
    EvidenceReviewSummary,
    EvidenceSearchFacets,
    EvidenceSearchFiltersEcho,
    EvidenceSearchItem,
    EvidenceSearchPage,
    EvidenceSearchQueryEcho,
    EvidenceSearchResponse,
    EvidenceSnippet,
    EvidenceStatus,
    ProviderReportSummary,
    ProvenanceEdge,
    ProvenanceEdgeKind,
    ProvenanceNode,
    ProvenanceNodeKind,
    ProvenanceTruthLevel,
    RankComponents,
    ResultKind,
    ReviewLevel,
    ReviewSource,
    SafeLinks,
    WarningCode,
)
from app.services import paper_review, topic_profiles

CURSOR_VERSION = 1
RESULT_ID_VERSION = 1
DEFAULT_CORPUS_KINDS = tuple(kind.value for kind in CorpusKind)
KIND_ORDER = {ResultKind.paper.value: 0, ResultKind.report_section.value: 1, ResultKind.run.value: 2}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|cookie|session)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)


class EvidenceSearchValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EvidenceSearchNotFound(LookupError):
    pass


@dataclass(frozen=True)
class SearchOptions:
    rawQuery: str = ""
    topicIds: tuple[str, ...] = ()
    runIds: tuple[str, ...] = ()
    corpusKinds: tuple[str, ...] = ()
    reviewSources: tuple[str, ...] = ()
    providerIds: tuple[str, ...] = ()
    reviewLevels: tuple[str, ...] = ()
    hasEvidence: bool | None = None
    limit: int = 25
    cursor: str | None = None
    authEnabled: bool = False
    userId: str | None = None


@dataclass(frozen=True)
class TopicInfo:
    topicId: str
    name: str
    description: str | None
    normalizedNames: tuple[str, ...]


@dataclass
class _PaperGroup:
    canonicalPaperId: str
    aliases: set[str] = field(default_factory=set)
    papers: list[repository.PaperCandidate] = field(default_factory=list)
    reviews: list[repository.ReviewCandidate] = field(default_factory=list)
    evidence: list[repository.EvidenceCandidate] = field(default_factory=list)
    occurrences: list[repository.PaperOccurrence] = field(default_factory=list)
    occurrenceCounts: dict[str, int] = field(default_factory=dict)


@dataclass
class _MatchState:
    tier: int = 99
    coverage: int = 0
    fields: set[str] = field(default_factory=set)
    corpora: set[str] = field(default_factory=set)
    snippets: list[tuple[int, str, str]] = field(default_factory=list)

    def add(
        self,
        *,
        tier: int,
        coverage: int,
        field_name: str,
        corpus: str,
        snippet_text: str | None = None,
    ) -> None:
        self.tier = min(self.tier, tier)
        self.coverage = max(self.coverage, coverage)
        self.fields.add(field_name)
        self.corpora.add(corpus)
        if snippet_text:
            self.snippets.append((tier, field_name, snippet_text))


@dataclass(frozen=True)
class _BuiltResult:
    item: EvidenceSearchItem
    sortKey: tuple[int, int, float, int, str]
    group: _PaperGroup | None = None
    run: repository.RunCandidate | None = None
    reportSection: repository.ReportSectionCandidate | None = None


@dataclass
class _GraphAccumulator:
    nodes: dict[str, ProvenanceNode] = field(default_factory=dict)
    edges: dict[str, ProvenanceEdge] = field(default_factory=dict)

    def add_node(self, node: ProvenanceNode) -> None:
        self.nodes.setdefault(node.nodeId, node)

    def add_edge(self, edge: ProvenanceEdge) -> None:
        self.edges.setdefault(edge.edgeId, edge)


def normalize_query(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = _CONTROL_CHARACTERS.sub(" ", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def tokenize_query(normalized_query: str) -> tuple[str, ...]:
    tokens = tuple(token for token in normalized_query.split(" ") if token)
    if len(tokens) > 8:
        raise EvidenceSearchValidationError("too_many_query_tokens", "The query may contain at most 8 tokens.")
    return tokens


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _base64url_encode(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical_json(payload)).decode("ascii").rstrip("=")


def _base64url_decode(value: str, *, code: str) -> dict[str, Any]:
    if not value or len(value) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise EvidenceSearchValidationError(code, "The encoded value is malformed.")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceSearchValidationError(code, "The encoded value is malformed.") from exc
    if not isinstance(decoded, dict):
        raise EvidenceSearchValidationError(code, "The encoded value is malformed.")
    return decoded


def encode_result_id(result_kind: str, stable_identity: str) -> str:
    if result_kind not in KIND_ORDER or not stable_identity or len(stable_identity) > 512:
        raise EvidenceSearchValidationError("invalid_result_id", "The result identity is invalid.")
    return _base64url_encode({"id": stable_identity, "kind": result_kind, "v": RESULT_ID_VERSION})


def decode_result_id(result_id: str) -> tuple[str, str]:
    payload = _base64url_decode(result_id, code="invalid_result_id")
    if set(payload) != {"id", "kind", "v"} or payload.get("v") != RESULT_ID_VERSION:
        raise EvidenceSearchValidationError("invalid_result_id", "The result ID is invalid.")
    kind = payload.get("kind")
    identity = payload.get("id")
    if kind not in KIND_ORDER or not isinstance(identity, str) or not identity or len(identity) > 512:
        raise EvidenceSearchValidationError("invalid_result_id", "The result ID is invalid.")
    return kind, identity


def _query_fingerprint(normalized_query: str, options: SearchOptions) -> str:
    payload = {
        "corpusKinds": list(options.corpusKinds or DEFAULT_CORPUS_KINDS),
        "hasEvidence": options.hasEvidence,
        "providerIds": list(options.providerIds),
        "query": normalized_query,
        "reviewLevels": list(options.reviewLevels),
        "reviewSources": list(options.reviewSources),
        "runIds": list(options.runIds),
        "topicIds": list(options.topicIds),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _encode_cursor(fingerprint: str, sort_key: tuple[int, int, float, int, str]) -> str:
    return _base64url_encode({"f": fingerprint, "s": list(sort_key), "v": CURSOR_VERSION})


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[int, int, float, int, str]:
    payload = _base64url_decode(cursor, code="malformed_cursor")
    if set(payload) != {"f", "s", "v"} or payload.get("v") != CURSOR_VERSION:
        raise EvidenceSearchValidationError("malformed_cursor", "The cursor is malformed.")
    if payload.get("f") != fingerprint:
        raise EvidenceSearchValidationError("cursor_query_mismatch", "The cursor does not match the query and filters.")
    sort_value = payload.get("s")
    if not isinstance(sort_value, list) or len(sort_value) != 5:
        raise EvidenceSearchValidationError("malformed_cursor", "The cursor sort tuple is malformed.")
    tier, coverage, timestamp, kind_order, result_id = sort_value
    if not isinstance(tier, int) or not isinstance(coverage, int) or not isinstance(timestamp, (int, float)):
        raise EvidenceSearchValidationError("malformed_cursor", "The cursor sort tuple is malformed.")
    if not isinstance(kind_order, int) or not isinstance(result_id, str):
        raise EvidenceSearchValidationError("malformed_cursor", "The cursor sort tuple is malformed.")
    return tier, coverage, float(timestamp), kind_order, result_id


def _safe_json_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item).strip() for item in decoded if isinstance(item, (str, int, float)) and str(item).strip()]


def _safe_spans(value: str) -> list[dict[str, str]]:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    allowed = ("evidence_span_id", "span_id", "label", "text", "source_path", "proof_artifact_ref")
    spans: list[dict[str, str]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        span = {key: str(item.get(key) or "").strip() for key in allowed if str(item.get(key) or "").strip()}
        if span:
            spans.append(span)
    return spans


def _timestamp_value(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _match_value(
    normalized_query: str,
    tokens: tuple[str, ...],
    value: str | None,
    *,
    category: str,
) -> tuple[int, int] | None:
    normalized_value = normalize_query(value)
    if not normalized_query or not normalized_value:
        return None
    coverage = sum(1 for token in tokens if token in normalized_value)
    all_tokens = bool(tokens) and coverage == len(tokens)
    if category == "id":
        normalized_paper_id = paper_review.normalize_paper_id(value)
        if normalized_query in {normalized_value, normalized_paper_id}:
            return 1, max(coverage, len(tokens))
        if normalized_query in normalized_value or all_tokens:
            return 7, max(coverage, 1)
        return None
    if category == "title":
        if normalized_query == normalized_value:
            return 2, max(coverage, len(tokens))
        if normalized_value.startswith(normalized_query):
            return 3, max(coverage, len(tokens))
        if all_tokens:
            return 4, coverage
        return None
    if not (normalized_query in normalized_value or all_tokens):
        return None
    tier = {"evidence": 5, "review": 6, "metadata": 7, "context": 8}[category]
    return tier, max(coverage, 1)


def _record_match(
    state: _MatchState,
    normalized_query: str,
    tokens: tuple[str, ...],
    value: str | None,
    *,
    category: str,
    field_name: str,
    corpus: str,
    snippet: bool = False,
) -> None:
    match = _match_value(normalized_query, tokens, value, category=category)
    if match is None:
        return
    tier, coverage = match
    state.add(
        tier=tier,
        coverage=coverage,
        field_name=field_name,
        corpus=corpus,
        snippet_text=str(value) if snippet and value else None,
    )


def _redact_text(value: str) -> str:
    text = _PRIVATE_KEY.sub("<redacted-private-key>", value)
    text = _BEARER.sub("Bearer <redacted>", text)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def _safe_snippet(
    candidates: list[tuple[int, str, str]],
    normalized_query: str,
) -> EvidenceSnippet | None:
    if not candidates:
        return None
    _tier, field_name, raw_text = sorted(candidates, key=lambda item: (item[0], item[1], item[2]))[0]
    text = _WHITESPACE.sub(" ", _CONTROL_CHARACTERS.sub(" ", raw_text)).strip()
    normalized_text = normalize_query(text)
    match_at = normalized_text.find(normalized_query) if normalized_query else 0
    truncated = len(text) > 240
    if truncated:
        if match_at < 0:
            start = 0
        else:
            start = max(0, match_at - 80)
        text = text[start : start + 240]
        if start > 0:
            text = "…" + text[1:]
        if start + 240 < len(raw_text):
            text = text[:-1] + "…"
    text = _redact_text(text)
    if len(text) > 240:
        text = text[:239] + "…"
        truncated = True
    return EvidenceSnippet(text=text, sourceField=field_name, truncated=truncated)


def _load_topic_profiles_read_only() -> tuple[TopicInfo, ...]:
    payload = read_json(topic_profiles.PROFILE_LIBRARY_PATH, default=[])
    if not isinstance(payload, list):
        return ()
    profiles: list[TopicInfo] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        topic_id = str(item.get("profile_id") or item.get("id") or "").strip()
        name = str(item.get("title") or item.get("name") or item.get("topic") or "").strip()
        if not topic_id or not name:
            continue
        normalized_names = tuple(
            dict.fromkeys(
                normalize_query(value)
                for value in (topic_id, name, item.get("topic"))
                if normalize_query(value)
            )
        )
        profiles.append(
            TopicInfo(
                topicId=topic_id,
                name=name,
                description=str(item.get("description") or "").strip() or None,
                normalizedNames=normalized_names,
            )
        )
    return tuple(sorted(profiles, key=lambda profile: profile.topicId))


def _topic_maps(profiles: tuple[TopicInfo, ...]) -> tuple[dict[str, TopicInfo], dict[str, TopicInfo]]:
    by_id = {profile.topicId: profile for profile in profiles}
    by_name: dict[str, TopicInfo] = {}
    for profile in profiles:
        for normalized_name in profile.normalizedNames:
            by_name.setdefault(normalized_name, profile)
    return by_id, by_name


def _topic_for_name(name: str, by_name: Mapping[str, TopicInfo]) -> TopicInfo | None:
    return by_name.get(normalize_query(name))


def _occurrence_model(
    row: repository.PaperOccurrence,
    by_name: Mapping[str, TopicInfo],
    *,
    compact_links: bool = False,
) -> EvidenceOccurrence:
    topic = _topic_for_name(row.runTopic, by_name)
    topic_id = topic.topicId if topic else None
    topic_link = f"/topics/{topic_id}/papers" if topic_id else None
    run_link = f"/topics/{topic_id}/runs/{row.runId}" if topic_id else f"/runs/{row.runId}"
    paper_link = f"{run_link}?focus_paper_id={row.paperId}"
    links = SafeLinks(run=run_link) if compact_links else SafeLinks(topic=topic_link, run=run_link, paper=paper_link)
    return EvidenceOccurrence(
        runId=row.runId,
        rawPaperId=row.paperId,
        title=row.title,
        topicId=topic_id,
        topicName=topic.name if topic else row.runTopic,
        runStatus=row.runStatus,
        paperStatus=row.status,
        theme=row.theme,
        score=row.score,
        published=row.published,
        entryUrl=None if compact_links else row.entryUrl,
        pdfUrl=None if compact_links else row.pdfUrl,
        updatedAt=row.runUpdatedAt,
        links=links,
    )


def _review_level(row: repository.ReviewCandidate) -> ReviewLevel:
    return ReviewLevel.full_text_reviewed if row.fullTextStatus == "reviewed" else ReviewLevel.abstract_only


def _review_source(row: repository.ReviewCandidate) -> ReviewSource:
    try:
        return ReviewSource(row.reviewSourceType)
    except ValueError:
        return ReviewSource.abstract


def _effective_timestamp(group: _PaperGroup) -> str:
    values = [review.reviewedAt for review in group.reviews]
    values.extend(occurrence.runUpdatedAt for occurrence in group.occurrences)
    values.extend(evidence.runUpdatedAt for evidence in group.evidence)
    return max((value for value in values if value), default="")


def _safe_links_for_paper(occurrences: list[EvidenceOccurrence]) -> SafeLinks:
    if not occurrences:
        return SafeLinks()
    return occurrences[0].links


def _paper_match_state(
    group: _PaperGroup,
    normalized_query: str,
    tokens: tuple[str, ...],
    allowed_corpora: set[str],
    by_name: Mapping[str, TopicInfo],
) -> _MatchState:
    state = _MatchState()
    if not normalized_query:
        if CorpusKind.paper.value in allowed_corpora and (group.papers or group.occurrences):
            state.add(tier=9, coverage=0, field_name="browse", corpus=CorpusKind.paper.value)
        if CorpusKind.review.value in allowed_corpora and group.reviews:
            state.add(tier=9, coverage=0, field_name="browse", corpus=CorpusKind.review.value)
        if CorpusKind.evidence.value in allowed_corpora and (group.evidence or any(_safe_spans(row.evidenceSpansJson) for row in group.reviews)):
            state.add(tier=9, coverage=0, field_name="browse", corpus=CorpusKind.evidence.value)
        return state

    if CorpusKind.paper.value in allowed_corpora:
        _record_match(
            state,
            normalized_query,
            tokens,
            group.canonicalPaperId,
            category="id",
            field_name="canonicalPaperId",
            corpus=CorpusKind.paper.value,
        )
        for alias in sorted(group.aliases):
            _record_match(
                state,
                normalized_query,
                tokens,
                alias,
                category="id",
                field_name="paperId",
                corpus=CorpusKind.paper.value,
            )
        for occurrence in group.occurrences:
            _record_match(
                state,
                normalized_query,
                tokens,
                occurrence.title,
                category="title",
                field_name="paperTitle",
                corpus=CorpusKind.paper.value,
                snippet=True,
            )
            for field_name, value in (
                ("theme", occurrence.theme),
                ("published", occurrence.published),
                ("topicName", occurrence.runTopic),
            ):
                _record_match(
                    state,
                    normalized_query,
                    tokens,
                    value,
                    category="metadata" if field_name != "topicName" else "context",
                    field_name=field_name,
                    corpus=CorpusKind.paper.value,
                    snippet=True,
                )
            topic = _topic_for_name(occurrence.runTopic, by_name)
            if topic:
                _record_match(
                    state,
                    normalized_query,
                    tokens,
                    topic.topicId,
                    category="id",
                    field_name="topicId",
                    corpus=CorpusKind.paper.value,
                )

    for review in group.reviews:
        if CorpusKind.review.value in allowed_corpora:
            _record_match(
                state,
                normalized_query,
                tokens,
                review.paperId,
                category="id",
                field_name="reviewPaperId",
                corpus=CorpusKind.review.value,
            )
            _record_match(
                state,
                normalized_query,
                tokens,
                review.topicId,
                category="id",
                field_name="topicId",
                corpus=CorpusKind.review.value,
            )
            _record_match(
                state,
                normalized_query,
                tokens,
                review.title,
                category="title",
                field_name="reviewTitle",
                corpus=CorpusKind.review.value,
                snippet=True,
            )
            review_fields = (
                ("abstractText", review.abstractText),
                ("textExcerpt", review.textExcerpt),
                ("methodSummary", review.methodSummary),
                ("keyFindings", " ".join(_safe_json_list(review.keyFindingsJson))),
                ("limitations", " ".join(_safe_json_list(review.limitationsJson))),
                ("whyItMatters", review.whyItMatters),
                ("topicFitRationale", review.topicFitRationale),
                ("reviewSource", review.reviewSourceType),
                ("providerId", review.sourceProvider),
            )
            for field_name, value in review_fields:
                _record_match(
                    state,
                    normalized_query,
                    tokens,
                    value,
                    category="review",
                    field_name=field_name,
                    corpus=CorpusKind.review.value,
                    snippet=True,
                )
        if CorpusKind.evidence.value in allowed_corpora:
            for span in _safe_spans(review.evidenceSpansJson):
                _record_match(
                    state,
                    normalized_query,
                    tokens,
                    span.get("text"),
                    category="evidence",
                    field_name="evidenceSpan",
                    corpus=CorpusKind.evidence.value,
                    snippet=True,
                )

    if CorpusKind.evidence.value in allowed_corpora:
        for evidence in group.evidence:
            _record_match(
                state,
                normalized_query,
                tokens,
                evidence.paperId,
                category="id",
                field_name="evidencePaperId",
                corpus=CorpusKind.evidence.value,
            )
            for field_name, value in (
                ("evidenceTitle", evidence.title),
                ("citationText", evidence.citationText),
                ("snippet", evidence.snippet),
            ):
                _record_match(
                    state,
                    normalized_query,
                    tokens,
                    value,
                    category="evidence",
                    field_name=field_name,
                    corpus=CorpusKind.evidence.value,
                    snippet=True,
                )
    return state


def _paper_statuses(
    group: _PaperGroup,
    inline_span_count: int,
    run_evidence_count: int,
) -> list[EvidenceStatus]:
    statuses: set[EvidenceStatus] = set()
    if any(review.fullTextStatus == "reviewed" for review in group.reviews):
        statuses.add(EvidenceStatus.full_text_reviewed)
    if any(review.fullTextStatus == "abstract_only" for review in group.reviews):
        statuses.add(EvidenceStatus.abstract_only)
    if any(review.reviewSourceType == ReviewSource.provider_output_class_proof.value for review in group.reviews):
        statuses.add(EvidenceStatus.provider_proof_backed)
    if any(review.reviewSourceType != ReviewSource.provider_output_class_proof.value for review in group.reviews):
        statuses.add(EvidenceStatus.deterministic_local)
    if group.reviews and inline_span_count + run_evidence_count == 0:
        statuses.add(EvidenceStatus.missing_evidence)
    return sorted(statuses, key=lambda status: status.value)


def _preview_edges_for_paper(
    canonical_paper_id: str,
    group: _PaperGroup,
    occurrences: list[EvidenceOccurrence],
) -> list[ProvenanceEdge]:
    paper_node = f"paper:{canonical_paper_id}"
    edges: list[ProvenanceEdge] = []
    if occurrences:
        run_node = f"run:{occurrences[0].runId}"
        edges.append(
            ProvenanceEdge(
                edgeId=f"{run_node}:contains:{paper_node}",
                edgeKind=ProvenanceEdgeKind.run_contains_paper,
                fromNodeId=run_node,
                toNodeId=paper_node,
                truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
            )
        )
    if group.reviews:
        review = group.reviews[0]
        review_node = f"review:{review.topicId}:{review.runId}:{review.paperId}"
        edges.append(
            ProvenanceEdge(
                edgeId=f"{paper_node}:review:{review_node}",
                edgeKind=ProvenanceEdgeKind.paper_has_review,
                fromNodeId=paper_node,
                toNodeId=review_node,
                truthLevel=ProvenanceTruthLevel.exact_persisted,
            )
        )
    return edges[:3]


def _build_paper_result(
    group: _PaperGroup,
    state: _MatchState,
    normalized_query: str,
    by_name: Mapping[str, TopicInfo],
    *,
    occurrence_limit: int,
) -> _BuiltResult:
    reviews = sorted(group.reviews, key=lambda row: (row.reviewedAt, row.topicId, row.runId), reverse=True)
    occurrence_rows = sorted(
        group.occurrences,
        key=lambda row: (row.runUpdatedAt, row.runId, row.paperId),
        reverse=True,
    )
    compact_occurrence_links = occurrence_limit <= 5
    occurrence_models = [
        _occurrence_model(row, by_name, compact_links=compact_occurrence_links)
        for row in occurrence_rows
    ]
    preview_occurrences = occurrence_models[:occurrence_limit]
    if reviews:
        title = reviews[0].title
        title_authority = "latest_review"
    elif occurrence_rows:
        title = occurrence_rows[0].title
        title_authority = "latest_occurrence"
    elif group.evidence:
        title = group.evidence[0].title
        title_authority = "indexed_evidence"
    else:
        title = group.canonicalPaperId
        title_authority = "canonical_id"
    inline_span_count = sum(len(_safe_spans(review.evidenceSpansJson)) for review in reviews)
    run_evidence_count = len(group.evidence)
    latest_review = reviews[0] if reviews else None
    review_summary = (
        EvidenceReviewSummary(
            topicId=latest_review.topicId,
            source=_review_source(latest_review),
            providerId=latest_review.sourceProvider,
            level=_review_level(latest_review),
            reviewedAt=latest_review.reviewedAt,
            reviewModel=latest_review.reviewModel,
            inlineEvidenceSpanCount=len(_safe_spans(latest_review.evidenceSpansJson)),
        )
        if latest_review
        else None
    )
    topic_ids = sorted(
        {
            *[review.topicId for review in reviews if review.topicId],
            *[occurrence.topicId for occurrence in occurrence_models if occurrence.topicId],
        }
    )
    effective_timestamp = _effective_timestamp(group)
    result_id = encode_result_id(ResultKind.paper.value, group.canonicalPaperId)
    rank_tier = state.tier if state.tier < 99 else 9
    rank = RankComponents(
        tier=rank_tier,
        tokenCoverage=state.coverage,
        effectiveTimestamp=effective_timestamp,
        kindOrder=KIND_ORDER[ResultKind.paper.value],
    )
    matched_corpora = [CorpusKind(value) for value in DEFAULT_CORPUS_KINDS if value in state.corpora]
    item = EvidenceSearchItem(
        resultId=result_id,
        resultKind=ResultKind.paper,
        canonicalPaperId=group.canonicalPaperId,
        paperIdAliases=sorted(group.aliases),
        title=title,
        titleAuthority=title_authority,
        safeSnippet=_safe_snippet(state.snippets, normalized_query),
        matchedFields=sorted(state.fields),
        matchedCorpusKinds=matched_corpora,
        matchReason=f"rank_tier_{rank_tier}",
        rankTier=rank_tier,
        rankComponents=rank,
        topicIds=topic_ids,
        reviewSummary=review_summary,
        inlineEvidenceSpanCount=inline_span_count,
        runEvidenceCount=run_evidence_count,
        occurrenceCount=sum(group.occurrenceCounts.values()) if group.occurrenceCounts else len(occurrence_models),
        occurrences=preview_occurrences,
        occurrencesTruncated=len(occurrence_models) > occurrence_limit,
        provenancePreview=_preview_edges_for_paper(group.canonicalPaperId, group, occurrence_models),
        statuses=_paper_statuses(group, inline_span_count, run_evidence_count),
        warnings=[],
        links=(
            _occurrence_model(occurrence_rows[0], by_name, compact_links=False).links
            if occurrence_rows
            else _safe_links_for_paper(occurrence_models)
        ),
    )
    sort_key = (
        rank_tier,
        -state.coverage,
        -_timestamp_value(effective_timestamp),
        KIND_ORDER[ResultKind.paper.value],
        result_id,
    )
    return _BuiltResult(item=item, sortKey=sort_key, group=group)


def _build_run_result(
    row: repository.RunCandidate,
    normalized_query: str,
    tokens: tuple[str, ...],
    by_name: Mapping[str, TopicInfo],
) -> _BuiltResult | None:
    state = _MatchState()
    if not normalized_query:
        state.add(tier=9, coverage=0, field_name="browse", corpus=CorpusKind.run.value)
    else:
        _record_match(
            state,
            normalized_query,
            tokens,
            row.runId,
            category="id",
            field_name="runId",
            corpus=CorpusKind.run.value,
        )
        for field_name, value in (("topicName", row.topic), ("runStatus", row.status)):
            _record_match(
                state,
                normalized_query,
                tokens,
                value,
                category="context",
                field_name=field_name,
                corpus=CorpusKind.run.value,
                snippet=True,
            )
        topic = _topic_for_name(row.topic, by_name)
        if topic:
            _record_match(
                state,
                normalized_query,
                tokens,
                topic.topicId,
                category="id",
                field_name="topicId",
                corpus=CorpusKind.run.value,
            )
    if not state.corpora:
        return None
    topic = _topic_for_name(row.topic, by_name)
    topic_id = topic.topicId if topic else None
    result_id = encode_result_id(ResultKind.run.value, row.runId)
    rank_tier = state.tier if state.tier < 99 else 9
    run_node = f"run:{row.runId}"
    preview: list[ProvenanceEdge] = []
    if row.reportPath:
        preview.append(
            ProvenanceEdge(
                edgeId=f"{run_node}:report",
                edgeKind=ProvenanceEdgeKind.run_has_report,
                fromNodeId=run_node,
                toNodeId=f"report:{row.runId}",
                truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
            )
        )
    item = EvidenceSearchItem(
        resultId=result_id,
        resultKind=ResultKind.run,
        title=row.topic or row.runId,
        titleAuthority="indexed_run",
        safeSnippet=_safe_snippet(state.snippets, normalized_query),
        matchedFields=sorted(state.fields),
        matchedCorpusKinds=[CorpusKind.run],
        matchReason=f"rank_tier_{rank_tier}",
        rankTier=rank_tier,
        rankComponents=RankComponents(
            tier=rank_tier,
            tokenCoverage=state.coverage,
            effectiveTimestamp=row.updatedAt,
            kindOrder=KIND_ORDER[ResultKind.run.value],
        ),
        topicIds=[topic_id] if topic_id else [],
        provenancePreview=preview,
        links=SafeLinks(
            topic=f"/topics/{topic_id}/papers" if topic_id else None,
            run=f"/topics/{topic_id}/runs/{row.runId}" if topic_id else f"/runs/{row.runId}",
            report=f"/reports/{row.runId}" if row.reportPath else None,
        ),
    )
    return _BuiltResult(
        item=item,
        sortKey=(
            rank_tier,
            -state.coverage,
            -_timestamp_value(row.updatedAt),
            KIND_ORDER[ResultKind.run.value],
            result_id,
        ),
        run=row,
    )


def _build_report_section_result(
    row: repository.ReportSectionCandidate,
    normalized_query: str,
    tokens: tuple[str, ...],
    by_name: Mapping[str, TopicInfo],
) -> _BuiltResult | None:
    state = _MatchState()
    if not normalized_query:
        state.add(tier=9, coverage=0, field_name="browse", corpus=CorpusKind.report_section.value)
    else:
        _record_match(
            state,
            normalized_query,
            tokens,
            row.sectionId,
            category="id",
            field_name="sectionId",
            corpus=CorpusKind.report_section.value,
        )
        _record_match(
            state,
            normalized_query,
            tokens,
            row.title,
            category="title",
            field_name="reportSectionTitle",
            corpus=CorpusKind.report_section.value,
            snippet=True,
        )
        for field_name, value in (("runId", row.runId), ("topicName", row.runTopic)):
            _record_match(
                state,
                normalized_query,
                tokens,
                value,
                category="context",
                field_name=field_name,
                corpus=CorpusKind.report_section.value,
                snippet=True,
            )
    if not state.corpora:
        return None
    identity = f"{row.runId}\0{row.sectionId}"
    result_id = encode_result_id(ResultKind.report_section.value, identity)
    rank_tier = state.tier if state.tier < 99 else 9
    topic = _topic_for_name(row.runTopic, by_name)
    topic_id = topic.topicId if topic else None
    run_node = f"run:{row.runId}"
    report_node = f"report:{row.runId}"
    section_node = f"report-section:{row.runId}:{row.sectionId}"
    preview = [
        ProvenanceEdge(
            edgeId=f"{run_node}:report",
            edgeKind=ProvenanceEdgeKind.run_has_report,
            fromNodeId=run_node,
            toNodeId=report_node,
            truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
        ),
        ProvenanceEdge(
            edgeId=f"{report_node}:section:{row.sectionId}",
            edgeKind=ProvenanceEdgeKind.report_has_section,
            fromNodeId=report_node,
            toNodeId=section_node,
            truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
        ),
    ]
    item = EvidenceSearchItem(
        resultId=result_id,
        resultKind=ResultKind.report_section,
        title=row.title,
        titleAuthority="indexed_report_section",
        safeSnippet=_safe_snippet(state.snippets, normalized_query),
        matchedFields=sorted(state.fields),
        matchedCorpusKinds=[CorpusKind.report_section],
        matchReason=f"rank_tier_{rank_tier}",
        rankTier=rank_tier,
        rankComponents=RankComponents(
            tier=rank_tier,
            tokenCoverage=state.coverage,
            effectiveTimestamp=row.updatedAt,
            kindOrder=KIND_ORDER[ResultKind.report_section.value],
        ),
        topicIds=[topic_id] if topic_id else [],
        provenancePreview=preview,
        warnings=[WarningCode.report_provenance_not_indexed],
        links=SafeLinks(
            topic=f"/topics/{topic_id}/papers" if topic_id else None,
            run=f"/topics/{topic_id}/runs/{row.runId}" if topic_id else f"/runs/{row.runId}",
            report=f"/reports/{row.runId}",
        ),
    )
    return _BuiltResult(
        item=item,
        sortKey=(
            rank_tier,
            -state.coverage,
            -_timestamp_value(row.updatedAt),
            KIND_ORDER[ResultKind.report_section.value],
            result_id,
        ),
        reportSection=row,
    )


def _build_facets(results: list[_BuiltResult]) -> EvidenceSearchFacets:
    corpus_counts: dict[str, int] = {}
    review_levels: dict[str, int] = {}
    review_sources: dict[str, int] = {}
    providers: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for built in results:
        item = built.item
        for corpus in item.matchedCorpusKinds:
            corpus_counts[corpus.value] = corpus_counts.get(corpus.value, 0) + 1
        if item.reviewSummary:
            level = item.reviewSummary.level.value
            source = item.reviewSummary.source.value
            review_levels[level] = review_levels.get(level, 0) + 1
            review_sources[source] = review_sources.get(source, 0) + 1
            if item.reviewSummary.providerId:
                provider = item.reviewSummary.providerId
                providers[provider] = providers.get(provider, 0) + 1
        for status in item.statuses:
            statuses[status.value] = statuses.get(status.value, 0) + 1
    return EvidenceSearchFacets(
        corpusKinds=dict(sorted(corpus_counts.items())),
        reviewLevels=dict(sorted(review_levels.items())),
        reviewSources=dict(sorted(review_sources.items())),
        providers=dict(sorted(providers.items())),
        statuses=dict(sorted(statuses.items())),
    )


class EvidenceSearchService:
    def __init__(self, repositoryFactory=repository.EvidenceSearchRepository) -> None:
        self._repository_factory = repositoryFactory

    def _visible_run_ids(self, *, auth_enabled: bool, user_id: str | None) -> tuple[str, ...] | None:
        if not auth_enabled:
            return None
        if not user_id or not run_repository.STORAGE_ROOT.is_dir():
            return ()
        visible: list[str] = []
        for run_json in sorted(run_repository.STORAGE_ROOT.glob("*/run.json")):
            payload = read_json(run_json, default=None)
            if not isinstance(payload, dict):
                continue
            run_id = str(payload.get("run_id") or run_json.parent.name).strip()
            owner_user_id = str(payload.get("owner_user_id") or "").strip()
            if run_id and owner_user_id == user_id:
                visible.append(run_id)
        return tuple(sorted(set(visible)))

    @staticmethod
    def _validate_options(options: SearchOptions) -> tuple[str, tuple[str, ...], set[str]]:
        normalized = normalize_query(options.rawQuery)
        tokens = tokenize_query(normalized)
        if len(options.rawQuery) > 200:
            raise EvidenceSearchValidationError("query_too_long", "The query may contain at most 200 characters.")
        if not 1 <= options.limit <= 100:
            raise EvidenceSearchValidationError("invalid_limit", "The page limit must be between 1 and 100.")
        allowed_corpora = set(options.corpusKinds or DEFAULT_CORPUS_KINDS)
        if not allowed_corpora or not allowed_corpora.issubset(set(DEFAULT_CORPUS_KINDS)):
            raise EvidenceSearchValidationError("invalid_corpus_kinds", "One or more corpus kinds are invalid.")
        return normalized, tokens, allowed_corpora

    def _execute(
        self,
        options: SearchOptions,
        *,
        occurrence_limit: int,
    ) -> tuple[list[_BuiltResult], str, tuple[str, ...], tuple[TopicInfo, ...], repository.SearchBundle]:
        normalized, tokens, allowed_corpora = self._validate_options(options)
        profiles = _load_topic_profiles_read_only()
        by_id, by_name = _topic_maps(profiles)
        topic_names = tuple(by_id[topic_id].name for topic_id in options.topicIds if topic_id in by_id)
        search_terms = [normalized, *tokens]
        if normalized in by_name:
            search_terms.extend(by_name[normalized].normalizedNames)
        paper_patterns = ()
        if normalized and normalized.isascii():
            paper_patterns = tuple(
                dict.fromkeys(f"%{repository.escape_like(term)}%" for term in search_terms if term)
            )
        criteria = repository.RepositorySearchCriteria(
            paperPatterns=paper_patterns,
            visibleRunIds=self._visible_run_ids(auth_enabled=options.authEnabled, user_id=options.userId),
            runIds=options.runIds,
            topicIds=options.topicIds,
            topicNames=topic_names,
            reviewSources=options.reviewSources,
            providerIds=options.providerIds,
            reviewLevels=options.reviewLevels,
            occurrenceLimit=occurrence_limit,
        )
        read_model = self._repository_factory()
        bundle = read_model.search(criteria)
        groups: dict[str, _PaperGroup] = {}

        def group_for(paper_id: str) -> _PaperGroup | None:
            canonical = paper_review.normalize_paper_id(paper_id)
            if not canonical:
                return None
            group = groups.setdefault(canonical, _PaperGroup(canonicalPaperId=canonical))
            group.aliases.add(paper_id)
            return group

        for row in bundle.papers:
            group = group_for(row.paperId)
            if group:
                group.papers.append(row)
        for row in bundle.reviews:
            group = group_for(row.paperId)
            if group:
                group.reviews.append(row)
        for row in bundle.evidence:
            group = group_for(row.paperId)
            if group:
                group.evidence.append(row)
        for row in bundle.occurrences:
            group = group_for(row.paperId)
            if group:
                group.occurrences.append(row)
                group.occurrenceCounts[row.paperId] = max(
                    group.occurrenceCounts.get(row.paperId, 0),
                    row.occurrenceCount,
                )

        built_results: list[_BuiltResult] = []
        review_filter_active = bool(options.reviewSources or options.providerIds or options.reviewLevels)
        for canonical_id in sorted(groups):
            group = groups[canonical_id]
            if review_filter_active and not group.reviews:
                continue
            inline_count = sum(len(_safe_spans(review.evidenceSpansJson)) for review in group.reviews)
            evidence_count = inline_count + len(group.evidence)
            if options.hasEvidence is True and evidence_count == 0:
                continue
            if options.hasEvidence is False and evidence_count > 0:
                continue
            state = _paper_match_state(group, normalized, tokens, allowed_corpora, by_name)
            if not state.corpora:
                continue
            built_results.append(
                _build_paper_result(
                    group,
                    state,
                    normalized,
                    by_name,
                    occurrence_limit=occurrence_limit,
                )
            )

        paper_only_filters = review_filter_active or options.hasEvidence is not None
        if CorpusKind.run.value in allowed_corpora and not paper_only_filters:
            for row in bundle.runs:
                built = _build_run_result(row, normalized, tokens, by_name)
                if built:
                    built_results.append(built)
        if CorpusKind.report_section.value in allowed_corpora and not paper_only_filters:
            for row in bundle.reportSections:
                built = _build_report_section_result(row, normalized, tokens, by_name)
                if built:
                    built_results.append(built)

        built_results.sort(key=lambda result: result.sortKey)
        return built_results, normalized, tokens, profiles, bundle

    def search(
        self,
        *,
        rawQuery: str = "",
        topicIds: tuple[str, ...] = (),
        runIds: tuple[str, ...] = (),
        corpusKinds: tuple[str, ...] = (),
        reviewSources: tuple[str, ...] = (),
        providerIds: tuple[str, ...] = (),
        reviewLevels: tuple[str, ...] = (),
        hasEvidence: bool | None = None,
        limit: int = 25,
        cursor: str | None = None,
        authEnabled: bool = False,
        userId: str | None = None,
    ) -> EvidenceSearchResponse:
        options = SearchOptions(
            rawQuery=rawQuery,
            topicIds=topicIds,
            runIds=runIds,
            corpusKinds=corpusKinds,
            reviewSources=reviewSources,
            providerIds=providerIds,
            reviewLevels=reviewLevels,
            hasEvidence=hasEvidence,
            limit=limit,
            cursor=cursor,
            authEnabled=authEnabled,
            userId=userId,
        )
        built_results, normalized, tokens, _profiles, _bundle = self._execute(options, occurrence_limit=3)
        fingerprint = _query_fingerprint(normalized, options)
        facets = _build_facets(built_results)
        total = len(built_results)
        page_candidates = built_results
        if cursor:
            last_key = _decode_cursor(cursor, fingerprint)
            page_candidates = [result for result in built_results if result.sortKey > last_key]
        page_results = page_candidates[:limit]
        has_more = len(page_candidates) > limit
        next_cursor = _encode_cursor(fingerprint, page_results[-1].sortKey) if has_more and page_results else None
        echo_filters = EvidenceSearchFiltersEcho(
            topicIds=list(topicIds),
            runIds=list(runIds),
            corpusKinds=[CorpusKind(value) for value in (corpusKinds or DEFAULT_CORPUS_KINDS)],
            reviewSources=[ReviewSource(value) for value in reviewSources],
            providerIds=list(providerIds),
            reviewLevels=[ReviewLevel(value) for value in reviewLevels],
            hasEvidence=hasEvidence,
        )
        return EvidenceSearchResponse(
            query=EvidenceSearchQueryEcho(
                raw=rawQuery,
                normalized=normalized,
                tokens=list(tokens),
                filters=echo_filters,
            ),
            page=EvidenceSearchPage(
                limit=limit,
                returnedCount=len(page_results),
                total=total,
                totalRelation="exact",
                nextCursor=next_cursor,
            ),
            facets=facets,
            items=[result.item for result in page_results],
        )

    def _read_selected_run_record(self, run_id: str) -> dict[str, Any] | None:
        if not run_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", run_id):
            return None
        payload = read_json(run_repository.STORAGE_ROOT / run_id / "run.json", default=None)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _provider_report_summary(
        run_record: Mapping[str, Any] | None,
    ) -> tuple[ProviderReportSummary | None, list[WarningCode]]:
        if not run_record or "provider_report" not in run_record:
            return None, []
        payload = run_record.get("provider_report")
        if not isinstance(payload, dict):
            return None, [WarningCode.malformed_provider_report]

        def text_value(key: str) -> str | None:
            value = payload.get(key)
            return str(value).strip() if isinstance(value, str) and value.strip() else None

        def text_list(key: str) -> list[str] | None:
            value = payload.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                return None
            return list(dict.fromkeys(item.strip() for item in value))[:100]

        draft_id = text_value("provider_report_draft_id")
        family_id = text_value("provider_family_id")
        surface_id = text_value("provider_surface_id")
        review_ids = text_list("source_provider_review_ids")
        paper_ids = text_list("source_paper_ids")
        span_refs = text_list("evidence_span_refs_used")
        proof_refs_used = text_list("proof_artifact_refs_used")
        proof_ref = text_value("proof_artifact_ref")
        if not all((draft_id, family_id, surface_id)) or None in (review_ids, paper_ids, span_refs, proof_refs_used):
            return None, [WarningCode.malformed_provider_report]
        proof_refs = list(dict.fromkeys([proof_ref, *(proof_refs_used or [])])) if proof_ref else list(proof_refs_used or [])
        return (
            ProviderReportSummary(
                draftId=draft_id,
                providerFamilyId=family_id,
                providerSurfaceId=surface_id,
                sourceReviewIds=review_ids or [],
                sourcePaperIds=paper_ids or [],
                evidenceSpanRefs=span_refs or [],
                proofRefs=proof_refs,
            ),
            [],
        )

    @staticmethod
    def _artifact_verification(
        ref: str,
        *,
        run_id: str | None,
        expected_sha256: str | None,
    ) -> ArtifactVerification:
        validation = artifact_refs.validate_artifact_reference(
            ref,
            run_id=run_id,
            expected_sha256=expected_sha256,
        )
        if validation.status == "hash_mismatch":
            status = ArtifactVerificationStatus.stale
        elif validation.status == "missing_artifact":
            status = ArtifactVerificationStatus.missing_artifact
        elif validation.status == "valid_portable" and expected_sha256 and validation.sha256_valid is True:
            status = ArtifactVerificationStatus.current
        elif validation.status == "valid_portable":
            status = ArtifactVerificationStatus.unverified_reference
        else:
            status = ArtifactVerificationStatus.orphaned_reference
        portable_ref = validation.resolution.portable_ref
        if portable_ref and not artifact_refs.is_portable_ref(portable_ref):
            portable_ref = None
        prefix = str(portable_ref or ref).split(":", 1)[0] if ":" in str(portable_ref or ref) else "relative"
        return ArtifactVerification(
            portableRef=portable_ref,
            refKind=prefix,
            status=status,
            exists=bool(validation.resolution.exists),
            expectedHashPresent=bool(expected_sha256),
            hashMatches=validation.sha256_valid,
        )

    @staticmethod
    def _artifact_candidates(
        built: _BuiltResult,
        provider_report: ProviderReportSummary | None,
    ) -> list[tuple[str, str | None]]:
        values: list[tuple[str, str | None]] = []
        if built.group:
            for review in sorted(built.group.reviews, key=lambda row: row.reviewedAt, reverse=True):
                if review.fullTextArtifactPath:
                    values.append((review.fullTextArtifactPath, review.sourceTextSha256))
                for span in _safe_spans(review.evidenceSpansJson):
                    proof_ref = span.get("proof_artifact_ref")
                    source_path = span.get("source_path")
                    if proof_ref:
                        values.append((proof_ref, None))
                    if source_path:
                        values.append((source_path, review.sourceTextSha256))
        if provider_report:
            values.extend((ref, None) for ref in provider_report.proofRefs)
        deduped: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for ref, expected in values:
            key = str(ref).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append((key, expected))
            if len(deduped) == 5:
                break
        return deduped

    @staticmethod
    def _add_occurrences_to_graph(graph: _GraphAccumulator, built: _BuiltResult, paper_node: str) -> None:
        for occurrence in built.item.occurrences:
            run_node = f"run:{occurrence.runId}"
            graph.add_node(
                ProvenanceNode(
                    nodeId=run_node,
                    nodeKind=ProvenanceNodeKind.run,
                    stableId=occurrence.runId,
                    label=occurrence.runId,
                    runId=occurrence.runId,
                )
            )
            graph.add_edge(
                ProvenanceEdge(
                    edgeId=f"{run_node}:contains:{paper_node}",
                    edgeKind=ProvenanceEdgeKind.run_contains_paper,
                    fromNodeId=run_node,
                    toNodeId=paper_node,
                    truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
                )
            )
            if not occurrence.topicId:
                continue
            topic_node = f"topic:{occurrence.topicId}"
            graph.add_node(
                ProvenanceNode(
                    nodeId=topic_node,
                    nodeKind=ProvenanceNodeKind.topic,
                    stableId=occurrence.topicId,
                    label=occurrence.topicName,
                    topicId=occurrence.topicId,
                )
            )
            graph.add_edge(
                ProvenanceEdge(
                    edgeId=f"{run_node}:topic:{topic_node}",
                    edgeKind=ProvenanceEdgeKind.run_topic_matches_profile,
                    fromNodeId=run_node,
                    toNodeId=topic_node,
                    truthLevel=ProvenanceTruthLevel.derived_deterministic,
                )
            )

    @staticmethod
    def _add_review_spans_to_graph(
        graph: _GraphAccumulator,
        *,
        review: repository.ReviewCandidate,
        review_node: str,
        canonical_paper_id: str,
    ) -> None:
        for index, span in enumerate(_safe_spans(review.evidenceSpansJson)):
            span_id = span.get("evidence_span_id") or span.get("span_id") or span.get("label") or str(index)
            span_node = f"evidence-span:{review_node}:{span_id}"
            graph.add_node(
                ProvenanceNode(
                    nodeId=span_node,
                    nodeKind=ProvenanceNodeKind.evidence_span,
                    stableId=span_id,
                    label=span_id,
                    paperId=canonical_paper_id,
                )
            )
            graph.add_edge(
                ProvenanceEdge(
                    edgeId=f"{review_node}:span:{span_node}",
                    edgeKind=ProvenanceEdgeKind.review_cites_evidence_span,
                    fromNodeId=review_node,
                    toNodeId=span_node,
                    truthLevel=ProvenanceTruthLevel.exact_persisted,
                )
            )
            proof_ref = span.get("proof_artifact_ref")
            if not proof_ref or not artifact_refs.is_portable_ref(proof_ref):
                continue
            proof_node = f"provider-proof:{proof_ref}"
            graph.add_node(
                ProvenanceNode(
                    nodeId=proof_node,
                    nodeKind=ProvenanceNodeKind.provider_proof,
                    stableId=proof_ref,
                    label=proof_ref,
                    portableRef=proof_ref,
                )
            )
            graph.add_edge(
                ProvenanceEdge(
                    edgeId=f"{span_node}:proof:{proof_node}",
                    edgeKind=ProvenanceEdgeKind.evidence_span_references_provider_proof,
                    fromNodeId=span_node,
                    toNodeId=proof_node,
                    truthLevel=ProvenanceTruthLevel.exact_portable_ref,
                )
            )

    @staticmethod
    def _add_review_to_graph(
        graph: _GraphAccumulator,
        *,
        review: repository.ReviewCandidate,
        paper_node: str,
        canonical_paper_id: str,
        by_name: Mapping[str, TopicInfo],
    ) -> None:
        review_node = f"review:{review.topicId}:{review.runId}:{review.paperId}"
        topic_node = f"topic:{review.topicId}"
        topic = next((value for value in by_name.values() if value.topicId == review.topicId), None)
        graph.add_node(
            ProvenanceNode(
                nodeId=topic_node,
                nodeKind=ProvenanceNodeKind.topic,
                stableId=review.topicId,
                label=topic.name if topic else review.topicId,
                topicId=review.topicId,
            )
        )
        graph.add_node(
            ProvenanceNode(
                nodeId=review_node,
                nodeKind=ProvenanceNodeKind.review,
                stableId=f"{review.topicId}:{review.runId}:{review.paperId}",
                label=review.title,
                topicId=review.topicId,
                paperId=canonical_paper_id,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{topic_node}:paper:{paper_node}",
                edgeKind=ProvenanceEdgeKind.topic_contains_paper,
                fromNodeId=topic_node,
                toNodeId=paper_node,
                truthLevel=ProvenanceTruthLevel.exact_persisted,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{paper_node}:review:{review_node}",
                edgeKind=ProvenanceEdgeKind.paper_has_review,
                fromNodeId=paper_node,
                toNodeId=review_node,
                truthLevel=ProvenanceTruthLevel.exact_persisted,
            )
        )
        EvidenceSearchService._add_review_provider_to_graph(graph, review=review, review_node=review_node)
        EvidenceSearchService._add_review_artifact_to_graph(graph, review=review, review_node=review_node)
        EvidenceSearchService._add_review_spans_to_graph(
            graph,
            review=review,
            review_node=review_node,
            canonical_paper_id=canonical_paper_id,
        )

    @staticmethod
    def _add_review_provider_to_graph(
        graph: _GraphAccumulator,
        *,
        review: repository.ReviewCandidate,
        review_node: str,
    ) -> None:
        if not review.sourceProvider:
            return
        provider_node = f"provider:{review.sourceProvider}"
        graph.add_node(
            ProvenanceNode(
                nodeId=provider_node,
                nodeKind=ProvenanceNodeKind.provider,
                stableId=review.sourceProvider,
                label=review.sourceProvider,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{provider_node}:review:{review_node}",
                edgeKind=ProvenanceEdgeKind.provider_generated_review,
                fromNodeId=provider_node,
                toNodeId=review_node,
                truthLevel=ProvenanceTruthLevel.exact_persisted,
            )
        )

    @staticmethod
    def _add_review_artifact_to_graph(
        graph: _GraphAccumulator,
        *,
        review: repository.ReviewCandidate,
        review_node: str,
    ) -> None:
        if not review.fullTextArtifactPath or not artifact_refs.is_portable_ref(review.fullTextArtifactPath):
            return
        artifact_node = f"artifact:{review.fullTextArtifactPath}"
        graph.add_node(
            ProvenanceNode(
                nodeId=artifact_node,
                nodeKind=ProvenanceNodeKind.artifact,
                stableId=review.fullTextArtifactPath,
                label=review.fullTextArtifactPath,
                portableRef=review.fullTextArtifactPath,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{review_node}:artifact:{artifact_node}",
                edgeKind=ProvenanceEdgeKind.review_references_artifact,
                fromNodeId=review_node,
                toNodeId=artifact_node,
                truthLevel=ProvenanceTruthLevel.exact_portable_ref,
            )
        )

    @staticmethod
    def _add_run_evidence_to_graph(
        graph: _GraphAccumulator,
        *,
        evidence: repository.EvidenceCandidate,
        paper_node: str,
        canonical_paper_id: str,
    ) -> None:
        evidence_node = f"evidence:{evidence.runId}:{evidence.evidenceId}"
        run_node = f"run:{evidence.runId}"
        graph.add_node(
            ProvenanceNode(
                nodeId=evidence_node,
                nodeKind=ProvenanceNodeKind.evidence,
                stableId=evidence.evidenceId,
                label=evidence.title,
                runId=evidence.runId,
                paperId=canonical_paper_id,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{run_node}:evidence:{evidence_node}",
                edgeKind=ProvenanceEdgeKind.run_has_evidence,
                fromNodeId=run_node,
                toNodeId=evidence_node,
                truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{evidence_node}:paper:{paper_node}",
                edgeKind=ProvenanceEdgeKind.evidence_cites_paper,
                fromNodeId=evidence_node,
                toNodeId=paper_node,
                truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
            )
        )

    @staticmethod
    def _add_paper_to_graph(
        graph: _GraphAccumulator,
        built: _BuiltResult,
        by_name: Mapping[str, TopicInfo],
    ) -> None:
        if not built.group or not built.item.canonicalPaperId:
            return
        canonical_paper_id = built.item.canonicalPaperId
        paper_node = f"paper:{canonical_paper_id}"
        graph.add_node(
            ProvenanceNode(
                nodeId=paper_node,
                nodeKind=ProvenanceNodeKind.paper,
                stableId=canonical_paper_id,
                label=built.item.title,
                paperId=canonical_paper_id,
            )
        )
        EvidenceSearchService._add_occurrences_to_graph(graph, built, paper_node)
        for review in sorted(built.group.reviews, key=lambda row: (row.reviewedAt, row.runId), reverse=True):
            EvidenceSearchService._add_review_to_graph(
                graph,
                review=review,
                paper_node=paper_node,
                canonical_paper_id=canonical_paper_id,
                by_name=by_name,
            )
        for evidence in built.group.evidence:
            EvidenceSearchService._add_run_evidence_to_graph(
                graph,
                evidence=evidence,
                paper_node=paper_node,
                canonical_paper_id=canonical_paper_id,
            )

    @staticmethod
    def _add_report_section_to_graph(graph: _GraphAccumulator, built: _BuiltResult) -> str | None:
        if built.run:
            return built.run.runId
        if not built.reportSection:
            return None
        run_id = built.reportSection.runId
        run_node = f"run:{run_id}"
        report_node = f"report:{run_id}"
        section_node = f"report-section:{run_id}:{built.reportSection.sectionId}"
        graph.add_node(ProvenanceNode(nodeId=run_node, nodeKind=ProvenanceNodeKind.run, stableId=run_id, label=run_id, runId=run_id))
        graph.add_node(ProvenanceNode(nodeId=report_node, nodeKind=ProvenanceNodeKind.report, stableId=run_id, label=built.item.title, runId=run_id))
        graph.add_node(
            ProvenanceNode(
                nodeId=section_node,
                nodeKind=ProvenanceNodeKind.report_section,
                stableId=built.reportSection.sectionId,
                label=built.reportSection.title,
                runId=run_id,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{run_node}:report",
                edgeKind=ProvenanceEdgeKind.run_has_report,
                fromNodeId=run_node,
                toNodeId=report_node,
                truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
            )
        )
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{report_node}:section:{section_node}",
                edgeKind=ProvenanceEdgeKind.report_has_section,
                fromNodeId=report_node,
                toNodeId=section_node,
                truthLevel=ProvenanceTruthLevel.exact_indexed_projection,
            )
        )
        return run_id

    @staticmethod
    def _add_provider_report_reference(
        graph: _GraphAccumulator,
        *,
        report_node: str,
        target_node: ProvenanceNode,
        edge_kind: ProvenanceEdgeKind,
        truth_level: ProvenanceTruthLevel,
    ) -> None:
        graph.add_node(target_node)
        graph.add_edge(
            ProvenanceEdge(
                edgeId=f"{report_node}:{edge_kind.value}:{target_node.nodeId}",
                edgeKind=edge_kind,
                fromNodeId=report_node,
                toNodeId=target_node.nodeId,
                truthLevel=truth_level,
            )
        )

    @staticmethod
    def _add_provider_report_to_graph(
        graph: _GraphAccumulator,
        *,
        built: _BuiltResult,
        provider_report: ProviderReportSummary,
        run_id: str | None,
    ) -> None:
        report_node = f"provider-report:{provider_report.draftId}"
        graph.add_node(
            ProvenanceNode(
                nodeId=report_node,
                nodeKind=ProvenanceNodeKind.report,
                stableId=provider_report.draftId,
                label=provider_report.draftId,
                runId=run_id,
            )
        )
        for review_id in provider_report.sourceReviewIds:
            matching_review = next(
                (review for review in (built.group.reviews if built.group else []) if review.runId == review_id),
                None,
            )
            target = (
                f"review:{matching_review.topicId}:{matching_review.runId}:{matching_review.paperId}"
                if matching_review
                else f"review-ref:{review_id}"
            )
            EvidenceSearchService._add_provider_report_reference(
                graph,
                report_node=report_node,
                target_node=ProvenanceNode(
                    nodeId=target,
                    nodeKind=ProvenanceNodeKind.review,
                    stableId=review_id,
                    label=review_id,
                ),
                edge_kind=ProvenanceEdgeKind.provider_report_references_review,
                truth_level=ProvenanceTruthLevel.exact_persisted,
            )
        for paper_id in provider_report.sourcePaperIds:
            canonical = paper_review.normalize_paper_id(paper_id)
            EvidenceSearchService._add_provider_report_reference(
                graph,
                report_node=report_node,
                target_node=ProvenanceNode(
                    nodeId=f"paper:{canonical}",
                    nodeKind=ProvenanceNodeKind.paper,
                    stableId=canonical,
                    label=paper_id,
                    paperId=canonical,
                ),
                edge_kind=ProvenanceEdgeKind.provider_report_references_paper,
                truth_level=ProvenanceTruthLevel.exact_persisted,
            )
        for span_ref in provider_report.evidenceSpanRefs:
            EvidenceSearchService._add_provider_report_reference(
                graph,
                report_node=report_node,
                target_node=ProvenanceNode(
                    nodeId=f"evidence-span-ref:{span_ref}",
                    nodeKind=ProvenanceNodeKind.evidence_span,
                    stableId=span_ref,
                    label=span_ref,
                ),
                edge_kind=ProvenanceEdgeKind.provider_report_references_evidence_span,
                truth_level=ProvenanceTruthLevel.exact_persisted,
            )
        for proof_ref in provider_report.proofRefs:
            if not artifact_refs.is_portable_ref(proof_ref):
                continue
            EvidenceSearchService._add_provider_report_reference(
                graph,
                report_node=report_node,
                target_node=ProvenanceNode(
                    nodeId=f"provider-proof:{proof_ref}",
                    nodeKind=ProvenanceNodeKind.provider_proof,
                    stableId=proof_ref,
                    label=proof_ref,
                    portableRef=proof_ref,
                ),
                edge_kind=ProvenanceEdgeKind.provider_report_references_proof,
                truth_level=ProvenanceTruthLevel.exact_portable_ref,
            )

    @staticmethod
    def _graph_for_result(
        built: _BuiltResult,
        provider_report: ProviderReportSummary | None,
        by_name: Mapping[str, TopicInfo],
    ) -> tuple[list[ProvenanceNode], list[ProvenanceEdge]]:
        graph = _GraphAccumulator()
        EvidenceSearchService._add_paper_to_graph(graph, built, by_name)
        run_id = EvidenceSearchService._add_report_section_to_graph(graph, built)
        if provider_report:
            EvidenceSearchService._add_provider_report_to_graph(
                graph,
                built=built,
                provider_report=provider_report,
                run_id=run_id,
            )
        return list(graph.nodes.values()), list(graph.edges.values())

    def detail(
        self,
        *,
        resultId: str,
        occurrenceRunId: str | None = None,
        authEnabled: bool = False,
        userId: str | None = None,
    ) -> EvidenceDetailResponse:
        kind, identity = decode_result_id(resultId)
        if kind == ResultKind.paper.value:
            query = identity
            corpus_kinds = (CorpusKind.paper.value, CorpusKind.review.value, CorpusKind.evidence.value)
        elif kind == ResultKind.run.value:
            query = identity
            corpus_kinds = (CorpusKind.run.value,)
        else:
            run_id, separator, section_id = identity.partition("\0")
            if not separator or not run_id or not section_id:
                raise EvidenceSearchValidationError("invalid_result_id", "The report-section result ID is invalid.")
            query = section_id
            corpus_kinds = (CorpusKind.report_section.value,)
        options = SearchOptions(
            rawQuery=query,
            corpusKinds=corpus_kinds,
            limit=100,
            authEnabled=authEnabled,
            userId=userId,
        )
        built_results, _normalized, _tokens, profiles, _bundle = self._execute(options, occurrence_limit=100)
        built = next((result for result in built_results if result.item.resultId == resultId), None)
        if built is None:
            raise EvidenceSearchNotFound("The evidence result was not found.")

        selected_run_id: str | None = None
        if kind == ResultKind.paper.value:
            occurrence_ids = {row.runId for row in built.group.occurrences} if built.group else set()
            if occurrenceRunId:
                if occurrenceRunId not in occurrence_ids:
                    raise EvidenceSearchNotFound("The selected occurrence was not found.")
                selected_run_id = occurrenceRunId
            elif built.item.occurrences:
                selected_run_id = built.item.occurrences[0].runId
        elif kind == ResultKind.run.value:
            selected_run_id = identity
        elif built.reportSection:
            selected_run_id = built.reportSection.runId

        run_record = self._read_selected_run_record(selected_run_id) if selected_run_id else None
        provider_report, provider_warnings = self._provider_report_summary(run_record)
        verifications = [
            self._artifact_verification(ref, run_id=selected_run_id, expected_sha256=expected)
            for ref, expected in self._artifact_candidates(built, provider_report)
        ]
        warnings = list(dict.fromkeys([*built.item.warnings, *provider_warnings]))
        if any(item.status == ArtifactVerificationStatus.orphaned_reference for item in verifications):
            warnings.append(WarningCode.orphaned_reference)
        warnings = list(dict.fromkeys(warnings))
        detail_statuses = set(built.item.statuses)
        verification_status_map = {
            ArtifactVerificationStatus.current: EvidenceStatus.current,
            ArtifactVerificationStatus.stale: EvidenceStatus.stale,
            ArtifactVerificationStatus.missing_artifact: EvidenceStatus.missing_artifact,
            ArtifactVerificationStatus.orphaned_reference: EvidenceStatus.orphaned_reference,
            ArtifactVerificationStatus.unverified_reference: EvidenceStatus.unverified_reference,
        }
        detail_statuses.update(verification_status_map[item.status] for item in verifications)
        if provider_warnings:
            detail_statuses.add(EvidenceStatus.degraded)
        summary = built.item.model_copy(update={"statuses": sorted(detail_statuses, key=lambda status: status.value)})
        _by_id, by_name = _topic_maps(profiles)
        nodes, edges = self._graph_for_result(built, provider_report, by_name)
        return EvidenceDetailResponse(
            resultId=resultId,
            resultKind=built.item.resultKind,
            summary=summary,
            occurrenceCount=built.item.occurrenceCount,
            occurrences=built.item.occurrences,
            occurrencesTruncated=built.item.occurrencesTruncated,
            selectedOccurrenceRunId=selected_run_id,
            provenanceNodes=nodes,
            provenanceEdges=edges,
            artifactVerifications=verifications,
            providerReport=provider_report,
            warnings=warnings,
            links=built.item.links,
        )

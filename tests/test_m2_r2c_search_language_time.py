from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import socket
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from app.repositories import evidence_search as repository
from app.services import paper_bank, paper_occurrences
from app.services.global_search import GlobalSearchService, SearchQueryError
from app.services.research_policy import ResearchPolicyError, normalize_research_policy


def _bundle() -> repository.SearchBundle:
    return repository.SearchBundle(
        papers=(
            repository.PaperCandidate(
                runId="run-a", paperId="paper-1", title="Shared Systems Paper", theme="Spatial", score=90,
                status="reviewed", published="2026-01-01", entryUrl="https://example.test/p1", pdfUrl=None,
                dedupClusterId=None, isPrimary=True, runTopic="Topic Alpha", runStatus="completed",
                runCreatedAt="2026-01-01T00:00:00Z", runUpdatedAt="2026-01-02T00:00:00Z",
            ),
            repository.PaperCandidate(
                runId="run-b", paperId="paper-1", title="Shared Systems Paper", theme="Systems", score=88,
                status="triaged", published="2026-02-01", entryUrl="https://example.test/p2", pdfUrl=None,
                dedupClusterId=None, isPrimary=True, runTopic="Topic Beta", runStatus="completed",
                runCreatedAt="2026-02-01T00:00:00Z", runUpdatedAt="2026-02-02T00:00:00Z",
            ),
        ),
        reviews=(),
        evidence=(repository.EvidenceCandidate(
            runId="run-a", evidenceId="ev-1", sourceId="paper-1", paperId="paper-1",
            title="Shared Systems Paper", sourceType="full_text", qualityHint="high", confidence=0.9,
            artifactPath=None, citationText="Paper 1", snippet="Evidence statement", runTopic="Topic Alpha",
            runUpdatedAt="2026-01-02T00:00:00Z",
        ),),
        runs=(repository.RunCandidate(
            runId="run-a", topic="Topic Alpha", status="completed", createdAt="2026-01-01T00:00:00Z",
            updatedAt="2026-01-02T00:00:00Z", rootRunId="run-a", parentRunId=None,
            lineageMode="root", reportPath=None,
        ),),
        reportSections=(),
        occurrences=(),
    )


def test_global_search_projects_all_four_kinds_with_stable_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GlobalSearchService()
    _install_search_catalog(monkeypatch, service, _bundle())

    result = service.search("shared")
    assert {item["kind"] for item in result["items"]} >= {"paper", "evidence"}
    paper = next(item for item in result["items"] if item["kind"] == "paper")
    assert paper["paperVersionId"]
    assert paper["route"].startswith("/topics/") and "paper=" in paper["route"]
    assert result["query"]["normalized"] == "shared"
    assert len(result["queryDigest"]) == 64

    all_results = service.search("")
    assert {item["kind"] for item in all_results["items"]} >= {"topic", "paper", "evidence", "run"}
    assert all_results["items"] == service.search("")["items"]


def test_global_search_keeps_same_labels_distinct_and_normalizes_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GlobalSearchService()
    _install_search_catalog(monkeypatch, service, _bundle())
    result = service.search("ＳＨＡＲＥＤ")
    papers = [item for item in result["items"] if item["kind"] == "paper"]
    assert len({item["resultId"] for item in papers}) == 2
    assert result["query"]["normalized"] == "shared"
    assert all(item["rankReason"] and item["matchedFields"] for item in result["items"])


def test_global_search_query_bounds_and_unavailable_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GlobalSearchService()
    monkeypatch.setattr(service, "_search_bundle", lambda: _bundle())
    monkeypatch.setattr(service, "_topic_profiles", lambda: [])
    with pytest.raises(SearchQueryError):
        service.search("x" * 201)
    result = service.search("run-a", kinds=("run", "citation"))
    assert result["requestedKinds"] == ["run", "citation"]
    assert result["unavailableKinds"]["citation"]


def test_search_help_is_parser_derived() -> None:
    help_contract = GlobalSearchService.search_help()
    assert help_contract["normalization"]["unicode"] == "NFKC"
    assert help_contract["syntax"]["boolean"]["interpretation"] == "literal"
    assert help_contract["syntax"]["wildcard"]["interpretation"] == "literal"


def test_research_policy_is_distinct_and_validated() -> None:
    policy = normalize_research_policy({"acquisition_language": "ko-KR", "review_report_language": "en", "acquisition_start_date": "2026-01-01", "acquisition_end_date": "2026-02-01"})
    assert policy == {"acquisition_language": "ko-KR", "review_report_language": "en", "acquisition_start_date": "2026-01-01", "acquisition_end_date": "2026-02-01"}
    with pytest.raises(ResearchPolicyError):
        normalize_research_policy({"acquisition_language": "not a language"})
    with pytest.raises(ResearchPolicyError):
        normalize_research_policy({"acquisition_start_date": "2026-03-01", "acquisition_end_date": "2026-02-01"})


def test_research_policy_resolves_into_run_payload() -> None:
    from app.services import topic_profiles

    profile = {
        "profile_id": "topic-a", "title": "A", "topic": "A", "queries": ["a"], "categories": [],
        "max_results": 10, "source_provider": "arxiv", "arxiv_sort_by": "submittedDate",
        "arxiv_sort_order": "descending", "source_settings": {}, "top_papers": 5, "weekly_top": 2,
        "default_collection_cadence": "daily", "default_report_cadence": "weekly",
        "acquisition_language": "ko", "review_report_language": "en-US",
        "acquisition_start_date": "2026-01-01", "acquisition_end_date": "2026-01-31",
    }
    payload = topic_profiles.to_run_request_payload(profile)
    assert payload["acquisition_language"] == "ko"
    assert payload["review_report_language"] == "en-US"
    assert payload["acquisition_start_date"] == "2026-01-01"
    assert payload["acquisition_end_date"] == "2026-01-31"


def test_research_policy_is_snapshotted_for_run_and_plan_resolution() -> None:
    from app.services.profile import build_run_profile

    profile = build_run_profile(
        {
            "topic": "Topic A",
            "queries": ["a"],
            "acquisition_language": "ko-KR",
            "review_report_language": "en-US",
            "acquisition_start_date": "2026-01-01",
            "acquisition_end_date": "2026-01-31",
        }
    )
    assert profile["research_policy"] == {
        "acquisition_language": "ko-KR",
        "review_report_language": "en-US",
        "acquisition_start_date": "2026-01-01",
        "acquisition_end_date": "2026-01-31",
    }
    assert profile["acquisition_language"] == "ko-KR"


def _install_search_catalog(monkeypatch, service, bundle):
    """Keep real Paper Bank projection/pagination; replace only stored input reads."""
    profiles = [
        {"profile_id": "topic-a", "title": "Topic Alpha", "topic": "Topic Alpha", "description": "Spatial"},
        {"profile_id": "topic-b", "title": "Topic Beta", "topic": "Topic Beta", "description": "Systems"},
    ]
    by_name = {row["topic"]: row["profile_id"] for row in profiles}
    discoveries = {row["profile_id"]: [] for row in profiles}
    for row in bundle.papers:
        discoveries[by_name[row.runTopic]].append((
            {"run_id": row.runId, "topic": row.runTopic, "created_at": row.runCreatedAt,
             "request": {"source_provider": "arxiv"}},
            {"paper_id": row.paperId, "title": row.title, "theme": row.theme,
             "source_provider": "arxiv", "published": row.published, "authors": ["Local Fixture"],
             "abstract": "Persisted local fixture only", "entry_url": row.entryUrl},
        ))
    monkeypatch.setattr(service, "_search_bundle", lambda: bundle)
    monkeypatch.setattr(service, "_topic_profiles", lambda: profiles)
    monkeypatch.setattr(paper_bank, "_load_topic_profile", lambda topic_id, actor=None: next(
        row for row in profiles if row["profile_id"] == topic_id))
    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", lambda profile, actor=None: discoveries[profile["profile_id"]])
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda topic_id: [])
    return discoveries


@pytest.fixture
def recovery_catalog(monkeypatch, record_property):
    """101 matching versions require two actual server pages, even with q=shared."""
    template = _bundle().papers[0]
    papers = tuple(replace(
        template, paperId=f"2601.{number:05d}v1",
        title="Shared" if number == 101 else "Shared Systems Paper",
        published="1900-01-01" if number == 101 else "2026-01-01",
    ) for number in range(1, 102))
    bundle = replace(_bundle(), papers=papers, evidence=(replace(_bundle().evidence[0], paperId=papers[0].paperId),))
    service = GlobalSearchService()
    discoveries = _install_search_catalog(monkeypatch, service, bundle)
    original_list = paper_bank.list_topic_papers
    pages = [original_list("topic-a", q="shared", page=page, page_size=100) for page in (1, 2)]
    assert [page["returnedCount"] for page in pages] == [100, 1]
    calls, outputs, forbidden = [], [], []

    def listing(topic_id, **kwargs):
        calls.append({"topic_id": topic_id, **{key: value for key, value in kwargs.items() if key != "actor"}})
        return original_list(topic_id, **kwargs)

    original_search = service.search

    def search(*args, **kwargs):
        result = original_search(*args, **kwargs)
        outputs.append(result)
        return result

    def prohibit(*args, **kwargs):
        forbidden.append("external_or_run_activity")
        raise AssertionError("Global Search must only read local projections")

    original_connect = socket.socket.connect

    def local_connect(sock, address):
        # Windows asyncio implements its internal wakeup pipe with loopback sockets.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return prohibit(sock, address)

    from app.services import runs

    monkeypatch.setattr(paper_bank, "list_topic_papers", listing)
    monkeypatch.setattr(service, "search", search)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(subprocess, "Popen", prohibit)
    monkeypatch.setattr(runs, "create_run_from_request", prohibit)
    yield {"service": service, "discoveries": discoveries, "calls": calls, "pages": pages,
           "listing": listing, "original_list": original_list, "bundle": bundle}
    record_property("page_calls", json.dumps(calls, sort_keys=True))
    record_property("search_results", json.dumps(outputs, sort_keys=True))
    record_property("external_activity", json.dumps(forbidden))
    assert forbidden == []


def test_global_search_recovery_supported_page_size_and_late_page_result(recovery_catalog):
    result = recovery_catalog["service"].search("shared", kinds=("paper",), limit=100)
    calls = recovery_catalog["calls"]
    assert all(1 <= call["page_size"] <= 100 for call in calls), calls
    assert [(call["page"], call["page_size"]) for call in calls] == [(1, 100), (2, 100)]
    assert all(call["q"] == "shared" for call in calls)
    assert result["total"] == 101 and len(result["items"]) == 100
    late = recovery_catalog["pages"][1]["items"][0]
    assert late["title"] == "Shared"
    assert result["items"][0]["paperVersionId"] == late["paperVersionId"]


def test_global_search_recovery_canonical_identity_and_detail_200(recovery_catalog, app_factory):
    from fastapi.testclient import TestClient
    from app.api.global_search import get_global_search_service

    application = app_factory()
    application.dependency_overrides[get_global_search_service] = lambda: recovery_catalog["service"]
    with TestClient(application) as client:
        response = client.get("/api/search", params={"q": "shared", "kind": "paper", "limit": 100})
        assert response.status_code == 200, response.text
        paper = response.json()["items"][0]
        destination = urlsplit(paper["route"])
        version = parse_qs(destination.query)["paper"][0]
        detail = client.get(f"/api/topics/{paper['topicId']}/paper-bank/{version}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["paperVersionId"] == paper["paperVersionId"] == version
        assert not version.startswith("legacy-")
        canonical = detail.json()["canonicalPaperId"]
        digest = hashlib.sha256(f"topic-a|{canonical}|{version}".encode()).hexdigest()[:24]
        assert paper["resultId"] == f"paper-{digest}"
        assert destination.path == "/topics/topic-a/papers"


def test_global_search_recovery_same_title_distinct_versions_and_determinism(recovery_catalog):
    service = recovery_catalog["service"]
    first = service.search("shared", kinds=("paper",), limit=100)
    second = service.search("shared", kinds=("paper",), limit=100)
    assert first == second
    same_title = [row for row in first["items"] if row["primaryLabel"] == "Shared Systems Paper"]
    assert len(same_title) == 99
    for key in ("paperVersionId", "resultId", "route"):
        assert len({row[key] for row in same_title}) == 99
    assert [(row["page"], row["page_size"]) for row in recovery_catalog["calls"]] == [(1, 100), (2, 100)] * 2


def test_global_search_recovery_valid_empty_has_no_navigable_fallback(recovery_catalog):
    recovery_catalog["discoveries"]["topic-a"].clear()
    result = recovery_catalog["service"].search("", kinds=("topic", "paper", "run"))
    assert not [row for row in result["items"] if row["kind"] == "paper"]
    assert result["sourceStates"]["paper"] == "empty"
    assert "paper" not in result["unavailableKinds"]
    assert {row["kind"] for row in result["items"]} == {"topic", "run"}


@pytest.mark.parametrize("failure,code", [
    (paper_bank.PaperBankUnavailable("paper_occurrence_identity_duplicate"), "paper_occurrence_identity_duplicate"),
    (paper_occurrences.OccurrenceIdentityDuplicate("occ-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), "occurrence_identity_duplicate"),
    (OSError("fixture unreadable"), "global_search_paper_projection_unavailable"),
])
def test_global_search_recovery_projection_failure_is_typed_and_atomic(recovery_catalog, monkeypatch, failure, code):
    def fail(topic_id, **kwargs):
        raise failure

    monkeypatch.setattr(paper_bank, "list_topic_papers", fail)
    result = recovery_catalog["service"].search("")
    assert {row["kind"] for row in result["items"]} == {"topic", "run"}
    assert result["sourceStates"]["paper"] == result["sourceStates"]["evidence"] == "unavailable"
    assert result["unavailableKinds"]["paper"] == result["unavailableKinds"]["evidence"] == code


def test_global_search_recovery_late_page_failure_discards_first_page(recovery_catalog, monkeypatch):
    def fail_late(topic_id, **kwargs):
        if kwargs.get("page", 1) == 2:
            raise paper_bank.PaperBankUnavailable("paper_occurrence_identity_invalid")
        return recovery_catalog["listing"](topic_id, **kwargs)

    monkeypatch.setattr(paper_bank, "list_topic_papers", fail_late)
    result = recovery_catalog["service"].search("shared", kinds=("paper",))
    assert result["items"] == [] and result["total"] == 0
    assert result["sourceStates"]["paper"] == "unavailable"
    assert result["unavailableKinds"]["paper"] == "paper_occurrence_identity_invalid"


@pytest.mark.parametrize("field,value,page", [
    ("page", 0, 1), ("page", 1, 2), ("page", True, 1), ("pageSize", 1000, 1),
    ("totalPages", "2", 1), ("totalPages", -1, 1), ("totalPages", 0, 1),
    ("totalPages", 3, 2), ("total", 102, 2), ("returnedCount", 99, 1),
])
def test_global_search_recovery_invalid_or_changing_pagination_fails_closed(recovery_catalog, monkeypatch, field, value, page):
    def malformed(topic_id, **kwargs):
        listing = recovery_catalog["listing"](topic_id, **kwargs)
        if kwargs.get("page", 1) == page:
            listing[field] = value
        return listing

    monkeypatch.setattr(paper_bank, "list_topic_papers", malformed)
    result = recovery_catalog["service"].search("shared", kinds=("paper",))
    assert result["items"] == []
    assert result["sourceStates"]["paper"] == "unavailable"
    assert result["unavailableKinds"]["paper"] == "global_search_paper_projection_pagination_invalid"


@pytest.mark.parametrize("conflicting", [False, True])
def test_global_search_recovery_duplicate_version_payload_contract(recovery_catalog, monkeypatch, conflicting):
    first_item = recovery_catalog["pages"][0]["items"][0]

    def duplicate(topic_id, **kwargs):
        listing = recovery_catalog["listing"](topic_id, **kwargs)
        if kwargs.get("page", 1) == 2:
            listing["items"] = [deepcopy(first_item)]
            if conflicting:
                listing["items"][0]["metadataDigest"] = "f" * 64
        return listing

    # Remove the replaced final occurrence from the federation input, retaining
    # 101 server rows so a duplicate crosses the actual page boundary.
    monkeypatch.setattr(recovery_catalog["service"], "_search_bundle", lambda: replace(
        recovery_catalog["bundle"], papers=recovery_catalog["bundle"].papers[:100]))
    monkeypatch.setattr(paper_bank, "list_topic_papers", duplicate)
    result = recovery_catalog["service"].search("shared", kinds=("paper",), limit=100)
    if conflicting:
        assert result["items"] == []
        assert result["unavailableKinds"]["paper"] == "global_search_paper_projection_identity_conflict"
    else:
        assert result["total"] == len(result["items"]) == 100
        assert len({row["paperVersionId"] for row in result["items"]}) == 100
        assert result["unavailableKinds"] == {}


def test_global_search_recovery_request_cache_does_not_hide_later_failure(recovery_catalog):
    service = recovery_catalog["service"]
    assert service.search("shared", kinds=("paper",))["sourceStates"]["paper"] == "available"
    recovery_catalog["discoveries"]["topic-a"].clear()
    assert service.search("shared", kinds=("paper",))["sourceStates"]["paper"] == "empty"


def test_global_search_recovery_four_kind_routes_and_topic_query_semantics(recovery_catalog):
    result = recovery_catalog["service"].search("", limit=100)
    # One broad result can be limited by paper ranking; exact kind scopes prove
    # all four kinds without removing the existing final response limit.
    observed = {row["kind"] for row in result["items"]}
    for kind in ("topic", "paper", "evidence", "run"):
        scoped = recovery_catalog["service"].search("", kinds=(kind,))
        assert scoped["items"] and scoped["sourceStates"][kind] == "available"
        observed.update(row["kind"] for row in scoped["items"])
        assert all(row["resultId"].startswith(kind + "-") and row["route"] for row in scoped["items"])
        assert all(not str(row.get("paperVersionId", "")).startswith("legacy-") for row in scoped["items"])
    assert observed == {"topic", "paper", "evidence", "run"}
    topical = recovery_catalog["service"].search("Topic Alpha", kinds=("paper",), limit=100)
    assert topical["total"] == 101
    assert all(row.get("q", "") == "" for row in recovery_catalog["calls"][-2:])

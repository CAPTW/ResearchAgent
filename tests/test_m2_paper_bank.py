from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.repositories import paper_collections
from app.services import paper_bank, paper_occurrences


TOPIC_A = "topic-a"
TOPIC_B = "topic-b"
OCCURRENCE_A = "occ-11111111111111111111111111111111"
OCCURRENCE_B = "occ-22222222222222222222222222222222"
OCCURRENCE_C = "occ-33333333333333333333333333333333"
OCCURRENCE_D = "occ-44444444444444444444444444444444"
OCCURRENCE_E = "occ-55555555555555555555555555555555"
OCCURRENCE_F = "occ-66666666666666666666666666666666"
OCCURRENCE_G = "occ-77777777777777777777777777777777"


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_fallback_identity(
    run_id: str,
    occurrence_id: str,
    *,
    version: str = "unversioned",
) -> tuple[str, str]:
    occurrence_anchor = _canonical_sha256(
        {
            "domain": "xrml-paper-occurrence-v1",
            "occurrenceId": occurrence_id,
        }
    )
    work_digest = _canonical_sha256(
        {
            "domain": "xrml-paper-work-fallback-v3",
            "occurrenceId": occurrence_anchor,
            "runId": run_id,
        }
    )
    version_digest = _canonical_sha256(
        {
            "domain": "xrml-paper-version-fallback-v3",
            "occurrenceId": occurrence_anchor,
            "runId": run_id,
            "version": version,
        }
    )
    return (
        f"observation:{run_id}:{work_digest}",
        f"observation-version:{run_id}:{version_digest}",
    )


def _fallback_item(
    occurrence_id: str,
    *,
    authors: list[str] | None = None,
    source_url: str | None = None,
    title: str = "Fallback Collision Paper",
    published: str | None = "2026-08-18T00:00:00Z",
) -> dict:
    item: dict = {
        "occurrence_id": occurrence_id,
        "title": title,
        "authors": list(authors or []),
        "source_provider": "fixture-fallback",
        "abstract": f"Stored observation {occurrence_id}.",
    }
    if source_url is not None:
        item["entry_url"] = source_url
    if published is not None:
        item["published"] = published
    return item


def _run(run_id: str, topic: str, created_at: str) -> dict:
    return {
        "run_id": run_id,
        "topic": topic,
        "created_at": created_at,
        "request": {"source_provider": "arxiv"},
    }


def _discoveries() -> dict[str, list[tuple[dict, dict]]]:
    run_a1 = _run("run-a1", "Topic A", "2026-08-01T00:00:00Z")
    run_a2 = _run("run-a2", "Topic A", "2026-08-02T00:00:00Z")
    run_b1 = _run("run-b1", "Topic B", "2026-08-03T00:00:00Z")
    repeated_v1 = {
        "paper_id": "2601.12345v1",
        "entry_id": "http://arxiv.org/abs/2601.12345v1",
        "entry_url": "https://arxiv.org/abs/2601.12345v1",
        "title": "Canonical Systems Paper",
        "authors": ["Ada Researcher"],
        "abstract": "A stable abstract about canonical research systems.",
        "source_provider": "arxiv",
        "published": "2026-01-01T00:00:00Z",
        "score": 80,
    }
    return {
        TOPIC_A: [
            (run_a1, repeated_v1),
            (run_a2, dict(repeated_v1)),
            (
                run_a2,
                {
                    **repeated_v1,
                    "paper_id": "2601.12345v2",
                    "entry_id": "http://arxiv.org/abs/2601.12345v2",
                    "entry_url": "https://arxiv.org/abs/2601.12345v2",
                    "updated": "2026-02-01T00:00:00Z",
                    "score": 85,
                },
            ),
            (
                run_a1,
                {
                    "paper_id": "10.5555/ABC.1",
                    "doi": "https://doi.org/10.5555/ABC.1",
                    "title": "DOI Grounded Paper",
                    "authors": ["D. Author"],
                    "source_provider": "crossref",
                    "entry_url": "https://doi.org/10.5555/abc.1",
                    "published": "2025-12-02T00:00:00Z",
                    "open_access": True,
                    "license": "CC-BY-4.0",
                    "score": 70,
                },
            ),
            (
                run_a1,
                {
                    "paper_id": "provider-record-a",
                    "title": "Shared Display Title",
                    "source_provider": "openalex",
                    "entry_url": "https://openalex.org/W111",
                    "score": 40,
                },
            ),
            (
                run_a2,
                {
                    "paper_id": "provider-record-b",
                    "title": "Shared Display Title",
                    "source_provider": "openalex",
                    "score": 41,
                },
            ),
        ],
        TOPIC_B: [
            (
                run_b1,
                {
                    "paper_id": "topic-b-only",
                    "title": "Topic B Only",
                    "source_provider": "crossref",
                    "score": 99,
                },
            )
        ],
    }


@pytest.fixture
def paper_bank_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[dict, dict]]]:
    discoveries = _discoveries()
    monkeypatch.setattr(
        paper_collections,
        "COLLECTION_LIBRARY_PATH",
        tmp_path / "paper_collections" / "collections.json",
    )
    monkeypatch.setattr(
        paper_bank,
        "_load_topic_profile",
        lambda topic_id, actor=None: {
            "profile_id": topic_id,
            "topic": "Topic A" if topic_id == TOPIC_A else "Topic B",
            "title": topic_id,
        },
    )
    monkeypatch.setattr(
        paper_bank,
        "_load_topic_discoveries",
        lambda profile, actor=None: discoveries[str(profile["profile_id"])],
    )
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda topic_id: [])
    return discoveries


def test_fallback_identity_uses_persisted_occurrence_not_shared_title_and_date() -> None:
    run = _run("same-run", "Topic A", "2026-08-28T00:00:00Z")
    paper_a = _fallback_item(
        OCCURRENCE_A,
        authors=["Alice A"],
        source_url="https://source-a.example/paper",
    )
    paper_b = _fallback_item(
        OCCURRENCE_B,
        authors=["Bob B"],
        source_url="https://source-b.example/paper",
    )

    identity_a = paper_bank._identity(run, paper_a)
    identity_b = paper_bank._identity(run, paper_b)
    expected_a = _expected_fallback_identity("same-run", OCCURRENCE_A)
    expected_b = _expected_fallback_identity("same-run", OCCURRENCE_B)

    assert (identity_a["workKey"], identity_a["versionKey"]) == expected_a
    assert (identity_b["workKey"], identity_b["versionKey"]) == expected_b
    assert identity_a["workKey"] != identity_b["workKey"]
    assert identity_a["versionKey"] != identity_b["versionKey"]


def test_identical_metadata_with_distinct_occurrences_never_merges() -> None:
    run = _run("identical-run", "Topic A", "2026-08-28T00:00:00Z")
    common = {
        "authors": ["Same Author"],
        "source_url": "https://same.example/paper",
        "title": "Identical Metadata Paper",
    }
    identity_a = paper_bank._identity(run, _fallback_item(OCCURRENCE_C, **common))
    identity_b = paper_bank._identity(run, _fallback_item(OCCURRENCE_D, **common))

    assert identity_a["workKey"] != identity_b["workKey"]
    assert identity_a["versionKey"] != identity_b["versionKey"]


def test_fallback_identity_is_run_scoped_and_same_occurrence_is_stable() -> None:
    item = _fallback_item(OCCURRENCE_E, authors=[], source_url=None, published=None)
    reordered = {key: item[key] for key in reversed(list(item))}
    metadata_changed = {
        **item,
        "title": "Updated display title",
        "authors": ["Updated Author"],
        "published": "2026-08-29T00:00:00Z",
        "entry_url": "https://updated.example/paper",
    }
    run_a = _run("run-a", "Topic A", "2026-08-28T00:00:00Z")
    run_b = _run("run-b", "Topic A", "2026-08-28T00:00:00Z")

    first = paper_bank._identity(run_a, item)
    repeated = paper_bank._identity(run_a, reordered)
    display_update = paper_bank._identity(run_a, metadata_changed)
    other_run = paper_bank._identity(run_b, item)

    assert first == repeated
    assert first == display_update
    assert first["workKey"] != other_run["workKey"]
    assert first["versionKey"] != other_run["versionKey"]


def test_fallback_version_domain_is_stable_for_exact_occurrence_authority() -> None:
    run = _run("version-run", "Topic A", "2026-08-28T00:00:00Z")
    item = _fallback_item(OCCURRENCE_F)
    version_one = {**item, "version": "Observed-1"}
    version_two = {**item, "version": "Observed-2"}

    identity = paper_bank._identity(run, item)
    identity_v1 = paper_bank._identity(run, version_one)
    identity_v2 = paper_bank._identity(run, version_two)

    assert identity_v1["workKey"] == identity_v2["workKey"] == identity["workKey"]
    assert identity_v1["versionKey"] != identity_v2["versionKey"]
    assert identity_v1["workKey"] != identity_v1["versionKey"]


def test_fallback_identity_without_loaded_occurrence_authority_fails_closed() -> None:
    run = _run("weak-run", "Topic A", "2026-08-28T00:00:00Z")
    item = {
        "source_provider": "unknown",
        "title": "No Source Record Identifier",
        "abstract": "A complete stored record snapshot remains deterministic.",
    }
    with pytest.raises(ValueError, match="occurrence_identity_invalid"):
        paper_bank._identity(run, item)


def test_doi_arxiv_and_provider_native_identity_bytes_do_not_change() -> None:
    assert paper_bank._identity(
        _run("run-a", "Topic A", "2026-08-28T00:00:00Z"),
        {
            "doi": "https://doi.org/10.5555/ABC.1",
            "paper_id": "10.5555/ABC.1",
            "version": "published",
            "source_provider": "crossref",
        },
    ) == {
        "provider": "crossref",
        "nativeId": "10.5555/ABC.1",
        "doi": "10.5555/abc.1",
        "workKey": "doi:10.5555/abc.1",
        "versionKey": "doi:10.5555/abc.1:version:published",
    }
    assert paper_bank._identity(
        _run("run-a", "Topic A", "2026-08-28T00:00:00Z"),
        {"paper_id": "2601.12345v1", "source_provider": "arxiv"},
    ) == {
        "provider": "arxiv",
        "nativeId": "2601.12345v1",
        "doi": None,
        "workKey": "arxiv:2601.12345",
        "versionKey": "arxiv:2601.12345:v1",
    }
    provider_a = paper_bank._identity(
        _run("run-a", "Topic A", "2026-08-28T00:00:00Z"),
        {"paper_id": "native-1", "source_provider": "provider-a", "version": "2"},
    )
    provider_b = paper_bank._identity(
        _run("run-b", "Topic A", "2026-08-28T00:00:00Z"),
        {"paper_id": "native-1", "source_provider": "provider-b", "version": "2"},
    )
    assert provider_a == {
        "provider": "provider-a",
        "nativeId": "native-1",
        "doi": None,
        "workKey": "provider:provider-a:native-1",
        "versionKey": "provider:provider-a:native-1:version:2",
    }
    assert provider_a["workKey"] != provider_b["workKey"]


def test_fallback_collision_pair_isolated_across_list_detail_collection_review_and_evidence(
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run("collision-run", "Topic A", "2026-08-28T00:00:00Z")
    paper_a = _fallback_item(
        OCCURRENCE_F,
        authors=["Collision Author A"],
        source_url="https://collision-a.example/paper",
    )
    paper_b = _fallback_item(
        OCCURRENCE_G,
        authors=["Collision Author B"],
        source_url="https://collision-b.example/paper",
    )
    paper_bank_fixture[TOPIC_A].extend([(run, paper_a), (run, paper_b)])
    evidence_text = "Evidence belongs only to fallback occurrence A."
    reviews = [
        {
            "topic_id": TOPIC_A,
            "run_id": "collision-run",
            "paper_id": OCCURRENCE_F,
            "full_text_status": "reviewed",
            "reviewed_at": "2026-08-28T01:00:00Z",
            "full_text_artifact_path": "run:raw/fallback-collision-a.txt",
            "source_text_sha256": "a" * 64,
            "evidence_spans": [
                {
                    "evidence_span_id": "fallback-evidence-a",
                    "locator": {"page": 7, "section": "Occurrence A"},
                    "content_sha256": "b" * 64,
                    "text": evidence_text,
                }
            ],
        }
    ]
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda topic_id: reviews)

    listing = paper_bank.list_topic_papers(TOPIC_A, q="Fallback Collision Paper", page_size=50)
    assert listing["total"] == 2
    assert len(listing["items"]) == 2
    by_occurrence = {item["compatibility"]["paperId"]: item for item in listing["items"]}
    assert set(by_occurrence) == {OCCURRENCE_F, OCCURRENCE_G}
    paper_row_a = by_occurrence[OCCURRENCE_F]
    paper_row_b = by_occurrence[OCCURRENCE_G]
    assert paper_row_a["canonicalPaperId"] != paper_row_b["canonicalPaperId"]
    assert paper_row_a["paperVersionId"] != paper_row_b["paperVersionId"]
    assert len(paper_row_a["compatibility"]["occurrences"]) == 1
    assert paper_row_a["review"] == {
        "state": "full_text_reviewed",
        "updatedAt": "2026-08-28T01:00:00Z",
        "evidenceCount": 1,
    }
    assert paper_row_b["review"]["state"] == "not_reviewed"

    page_one = paper_bank.list_topic_papers(
        TOPIC_A,
        q="Fallback Collision Paper",
        sort="title_asc",
        page=1,
        page_size=1,
    )
    page_two = paper_bank.list_topic_papers(
        TOPIC_A,
        q="Fallback Collision Paper",
        sort="title_asc",
        page=2,
        page_size=1,
    )
    assert {
        page_one["items"][0]["paperVersionId"],
        page_two["items"][0]["paperVersionId"],
    } == {paper_row_a["paperVersionId"], paper_row_b["paperVersionId"]}
    assert page_one["queryDigest"] != page_two["queryDigest"]
    assert page_one == paper_bank.list_topic_papers(
        TOPIC_A,
        q="Fallback Collision Paper",
        sort="title_asc",
        page=1,
        page_size=1,
    )

    detail_a = paper_bank.get_topic_paper_detail(TOPIC_A, paper_row_a["paperVersionId"])
    detail_b = paper_bank.get_topic_paper_detail(TOPIC_A, paper_row_b["paperVersionId"])
    assert detail_a["authors"] == ["Collision Author A"]
    assert detail_b["authors"] == ["Collision Author B"]
    assert detail_a["sourceUrl"] == "https://collision-a.example/paper"
    assert detail_b["sourceUrl"] == "https://collision-b.example/paper"
    assert detail_a["fullText"] == {
        "state": "reviewed",
        "artifactRef": "run:raw/fallback-collision-a.txt",
        "contentDigest": "a" * 64,
    }
    assert detail_a["evidence"] == [
        {
            "id": "fallback-evidence-a",
            "locator": {"page": 7, "section": "Occurrence A"},
            "contentDigest": "b" * 64,
            "text": evidence_text,
            "destination": f"/topics/{TOPIC_A}/papers?paper={paper_row_a['paperVersionId']}&evidence=fallback-evidence-a",
            "destinationState": "available",
        }
    ]
    assert detail_b["fullText"] == {
        "state": "unavailable",
        "artifactRef": None,
        "contentDigest": None,
    }
    assert detail_b["evidence"] == []

    saved = paper_collections.create_collection(TOPIC_A, "Fallback A only")
    paper_collections.add_member(
        TOPIC_A,
        saved["id"],
        canonical_paper_id=paper_row_a["canonicalPaperId"],
        paper_version_id=paper_row_a["paperVersionId"],
    )
    paper_collections.add_member(
        TOPIC_A,
        saved["id"],
        canonical_paper_id=paper_row_a["canonicalPaperId"],
        paper_version_id=paper_row_a["paperVersionId"],
    )
    saved_listing = paper_bank.list_topic_papers(TOPIC_A, collection_id=saved["id"], page_size=50)
    assert [item["paperVersionId"] for item in saved_listing["items"]] == [paper_row_a["paperVersionId"]]
    assert paper_collections.get_collection(TOPIC_A, saved["id"])["memberCount"] == 1
    paper_collections.remove_member(TOPIC_A, saved["id"], paper_row_a["paperVersionId"])
    assert paper_collections.get_collection(TOPIC_A, saved["id"])["memberCount"] == 0
    assert paper_bank.get_topic_paper_detail(TOPIC_A, paper_row_b["paperVersionId"])["evidence"] == []

    legacy_digest = _canonical_sha256(
        {
            "runId": "collision-run",
            "title": "Fallback Collision Paper",
            "published": "2026-08-18T00:00:00Z",
        }
    )
    legacy_version_id = paper_bank._stable_id(
        "version",
        f"observation:collision-run:{legacy_digest}",
    )
    with pytest.raises(paper_bank.PaperVersionNotFound):
        paper_bank.get_topic_paper_detail(TOPIC_A, legacy_version_id)


@pytest.mark.parametrize(
    "malformed_identity",
    [
        "occ-" + "a" * 31,
        "occ-legacy-" + "b" * 31,
        17,
    ],
    ids=["generated-like", "legacy-like", "arbitrary-scalar"],
)
def test_occurrence_identity_invalid_at_load_boundary_returns_typed_503(
    client,
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
    malformed_identity: object,
) -> None:
    def invalid_discoveries(profile, actor=None):
        paper_occurrences.normalize_occurrence_id(malformed_identity)
        raise AssertionError("invalid occurrence identity unexpectedly normalized")

    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", invalid_discoveries)

    response = client.get(f"/api/topics/{TOPIC_A}/paper-bank")

    assert response.status_code == 503
    assert response.json() == {"detail": "occurrence_identity_invalid"}


def test_occurrence_identity_api_boundary_preserves_duplicate_code(
    client,
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def duplicate_discoveries(profile, actor=None):
        raise paper_occurrences.OccurrenceIdentityDuplicate("duplicate")

    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", duplicate_discoveries)

    response = client.get(f"/api/topics/{TOPIC_A}/paper-bank")

    assert response.status_code == 503
    assert response.json() == {"detail": "occurrence_identity_duplicate"}


def test_occurrence_identity_api_boundary_leaves_valid_and_unrelated_errors_unchanged(
    client,
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_response = client.get(f"/api/topics/{TOPIC_A}/paper-bank")
    assert valid_response.status_code == 200

    def unexpected_discoveries(profile, actor=None):
        raise RuntimeError("unrelated_paper_bank_failure")

    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", unexpected_discoveries)

    with pytest.raises(RuntimeError, match="unrelated_paper_bank_failure"):
        client.get(f"/api/topics/{TOPIC_A}/paper-bank")


def test_duplicate_occurrence_in_one_run_returns_truthful_api_unavailable(
    client,
    paper_bank_fixture,
) -> None:
    run = _run("duplicate-api-run", "Topic A", "2026-08-28T00:00:00Z")
    paper_bank_fixture[TOPIC_A].extend(
        [
            (run, _fallback_item(OCCURRENCE_A, title="Duplicate A")),
            (run, _fallback_item(OCCURRENCE_A, title="Duplicate B")),
        ]
    )

    response = client.get(f"/api/topics/{TOPIC_A}/paper-bank")

    assert response.status_code == 503
    assert response.json()["detail"] == "occurrence_identity_duplicate"


def test_same_occurrence_id_in_distinct_runs_is_run_scoped_without_deep_link_collision(
    paper_bank_fixture,
) -> None:
    run_a = _run("cross-run-a", "Topic A", "2026-08-28T00:00:00Z")
    run_b = _run("cross-run-b", "Topic A", "2026-08-29T00:00:00Z")
    paper_bank_fixture[TOPIC_A].extend(
        [
            (run_a, _fallback_item(OCCURRENCE_B, title="Cross-run A")),
            (run_b, _fallback_item(OCCURRENCE_B, title="Cross-run B")),
        ]
    )

    listing = paper_bank.list_topic_papers(TOPIC_A, q="Cross-run", page_size=50)

    assert listing["total"] == 2
    assert len({item["canonicalPaperId"] for item in listing["items"]}) == 2
    assert len({item["paperVersionId"] for item in listing["items"]}) == 2
    assert {
        paper_bank.get_topic_paper_detail(TOPIC_A, item["paperVersionId"])["title"]
        for item in listing["items"]
    } == {"Cross-run A", "Cross-run B"}


def test_topic_isolation_and_identity_versions(paper_bank_fixture) -> None:
    topic_a = paper_bank.list_topic_papers(TOPIC_A, page_size=50)
    topic_b = paper_bank.list_topic_papers(TOPIC_B, page_size=50)

    assert {item["title"] for item in topic_a["items"]}.isdisjoint({"Topic B Only"})
    assert [item["title"] for item in topic_b["items"]] == ["Topic B Only"]

    canonical_versions = [item for item in topic_a["items"] if item["title"] == "Canonical Systems Paper"]
    assert len(canonical_versions) == 2
    assert len({item["canonicalPaperId"] for item in canonical_versions}) == 1
    assert len({item["paperVersionId"] for item in canonical_versions}) == 2
    assert sorted(len(item["compatibility"]["occurrences"]) for item in canonical_versions) == [1, 2]

    same_title = [item for item in topic_a["items"] if item["title"] == "Shared Display Title"]
    assert len(same_title) == 2
    assert len({item["canonicalPaperId"] for item in same_title}) == 2


def test_query_sort_pagination_and_digest_are_stable(paper_bank_fixture) -> None:
    first = paper_bank.list_topic_papers(
        TOPIC_A,
        q="paper",
        provider="arxiv",
        sort="published_desc",
        page=1,
        page_size=1,
    )
    second = paper_bank.list_topic_papers(
        TOPIC_A,
        q="paper",
        provider="arxiv",
        sort="published_desc",
        page=2,
        page_size=1,
    )
    repeated = paper_bank.list_topic_papers(
        TOPIC_A,
        q="paper",
        provider="arxiv",
        sort="published_desc",
        page=1,
        page_size=1,
    )

    assert first["total"] == 2
    assert first["queryDigest"] == repeated["queryDigest"]
    assert first["canonicalQuery"] == repeated["canonicalQuery"]
    assert first["items"] == repeated["items"]
    assert first["items"][0]["paperVersionId"] != second["items"][0]["paperVersionId"]
    assert first["page"] == 1
    assert second["page"] == 2


def test_collection_lifecycle_is_idempotent_topic_scoped_and_restart_safe(paper_bank_fixture) -> None:
    item = paper_bank.list_topic_papers(TOPIC_A, page_size=50)["items"][0]
    created = paper_collections.create_collection(TOPIC_A, "Core reading")
    renamed = paper_collections.rename_collection(TOPIC_A, created["id"], "Priority reading")
    first_add = paper_collections.add_member(
        TOPIC_A,
        created["id"],
        canonical_paper_id=item["canonicalPaperId"],
        paper_version_id=item["paperVersionId"],
    )
    repeated_add = paper_collections.add_member(
        TOPIC_A,
        created["id"],
        canonical_paper_id=item["canonicalPaperId"],
        paper_version_id=item["paperVersionId"],
    )

    assert renamed["name"] == "Priority reading"
    assert first_add["memberCount"] == 1
    assert repeated_add["memberCount"] == 1
    assert paper_collections.list_collections(TOPIC_A)["items"][1]["memberCount"] == 1
    assert all(row["memberCount"] == 0 for row in paper_collections.list_collections(TOPIC_B)["items"])

    filtered = paper_bank.list_topic_papers(TOPIC_A, collection_id=created["id"], page_size=50)
    assert [row["paperVersionId"] for row in filtered["items"]] == [item["paperVersionId"]]

    paper_collections.remove_member(TOPIC_A, created["id"], item["paperVersionId"])
    assert paper_collections.get_collection(TOPIC_A, created["id"])["memberCount"] == 0
    paper_collections.delete_collection(TOPIC_A, created["id"])
    with pytest.raises(paper_collections.CollectionNotFound):
        paper_collections.get_collection(TOPIC_A, created["id"])


def test_failed_collection_mutation_leaves_previous_bytes_unchanged(paper_bank_fixture) -> None:
    created = paper_collections.create_collection(TOPIC_A, "Stable")
    before = paper_collections.COLLECTION_LIBRARY_PATH.read_bytes()

    with pytest.raises(paper_collections.CollectionNotFound):
        paper_collections.add_member(
            TOPIC_A,
            "missing",
            canonical_paper_id="paper-missing",
            paper_version_id="version-missing",
        )

    assert paper_collections.COLLECTION_LIBRARY_PATH.read_bytes() == before
    assert paper_collections.get_collection(TOPIC_A, created["id"])["name"] == "Stable"


def test_review_full_text_and_evidence_projection_preserves_locator_and_digest(
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviews = [
        {
            "topic_id": TOPIC_A,
            "run_id": "run-a2",
            "paper_id": "2601.12345v2",
            "full_text_status": "reviewed",
            "reviewed_at": "2026-08-03T00:00:00Z",
            "full_text_artifact_path": "run:raw/2601.12345v2.txt",
            "source_text_sha256": "a" * 64,
            "evidence_spans": [
                {
                    "evidence_span_id": "evidence-v2-1",
                    "locator": {"page": 4, "section": "Results"},
                    "content_sha256": "b" * 64,
                    "text": "Exact evidence text.",
                }
            ],
        }
    ]
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda topic_id: reviews)
    listing = paper_bank.list_topic_papers(TOPIC_A, review_state="full_text_reviewed", page_size=50)
    assert len(listing["items"]) == 1
    assert listing["reviewSummary"]["fullTextReviewed"] == 1

    detail = paper_bank.get_topic_paper_detail(TOPIC_A, listing["items"][0]["paperVersionId"])
    assert detail["review"]["state"] == "full_text_reviewed"
    assert detail["fullText"]["artifactRef"] == "run:raw/2601.12345v2.txt"
    assert detail["fullText"]["contentDigest"] == "a" * 64
    assert detail["evidence"][0]["id"] == "evidence-v2-1"
    assert detail["evidence"][0]["locator"] == {"page": 4, "section": "Results"}
    assert detail["evidence"][0]["contentDigest"] == "b" * 64
    assert detail["evidenceResource"] == {"state": "available", "reason": None}
    assert detail["evidence"][0]["destination"] == (
        f"/topics/{TOPIC_A}/papers?paper={listing['items'][0]['paperVersionId']}&evidence=evidence-v2-1"
    )
    assert detail["evidence"][0]["destinationState"] == "available"


def test_review_store_unavailable_is_not_projected_as_zero(paper_bank_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_topic_id: str):
        raise OSError("fixture store unavailable")

    monkeypatch.setattr(paper_bank, "_load_topic_reviews", unavailable)
    result = paper_bank.list_topic_papers(TOPIC_A, page_size=50)

    assert result["reviewResource"]["state"] == "unavailable"
    assert result["reviewSummary"] is None
    assert {item["review"]["state"] for item in result["items"]} == {"unavailable"}

    detail = paper_bank.get_topic_paper_detail(TOPIC_A, result["items"][0]["paperVersionId"])
    assert detail["evidence"] == []
    assert detail["evidenceResource"] == {
        "state": "unavailable",
        "reason": "paper_review_store_unavailable",
    }


def test_detail_distinguishes_empty_evidence_and_only_claims_proven_rights(paper_bank_fixture) -> None:
    listing = paper_bank.list_topic_papers(TOPIC_A, page_size=50)
    doi_item = next(item for item in listing["items"] if item["title"] == "DOI Grounded Paper")
    unknown_item = next(item for item in listing["items"] if item["title"] == "Shared Display Title")

    doi_detail = paper_bank.get_topic_paper_detail(TOPIC_A, doi_item["paperVersionId"])
    unknown_detail = paper_bank.get_topic_paper_detail(TOPIC_A, unknown_item["paperVersionId"])

    assert doi_detail["rights"] == {"state": "open_access", "label": "CC-BY-4.0"}
    assert unknown_detail["rights"] == {"state": "unknown", "label": None}
    assert unknown_detail["evidence"] == []
    assert unknown_detail["evidenceResource"] == {"state": "empty", "reason": None}


def test_empty_paper_bank_is_distinct_from_backend_failure(paper_bank_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", lambda profile, actor=None: [])
    empty = paper_bank.list_topic_papers(TOPIC_A)
    assert empty["items"] == []
    assert empty["resource"]["state"] == "empty"

    def failed(_profile, actor=None):
        raise OSError("fixture read failed")

    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", failed)
    with pytest.raises(paper_bank.PaperBankUnavailable):
        paper_bank.list_topic_papers(TOPIC_A)


def test_unsafe_source_url_is_not_exposed(paper_bank_fixture) -> None:
    result = paper_bank.list_topic_papers(TOPIC_A, q="provider-record-b", page_size=50)
    assert result["items"][0]["sourceUrl"] is None
    assert result["items"][0]["sourceUrlState"] == "unavailable"


def test_collection_document_is_strict_json_without_cross_topic_leak(paper_bank_fixture) -> None:
    paper_collections.create_collection(TOPIC_A, "A only")
    paper_collections.create_collection(TOPIC_B, "B only")
    document = json.loads(paper_collections.COLLECTION_LIBRARY_PATH.read_text(encoding="utf-8"))

    assert document["schemaVersion"] == 1
    assert set(document["topics"]) == {TOPIC_A, TOPIC_B}
    assert {item["name"] for item in paper_collections.list_collections(TOPIC_A)["items"]} == {
        "Saved papers",
        "A only",
    }


def test_typed_api_supports_bank_detail_collections_and_unsupported_filter_truth(
    client,
    paper_bank_fixture,
) -> None:
    listing_response = client.get(
        f"/api/topics/{TOPIC_A}/paper-bank",
        params={"provider": "arxiv", "sort": "published_desc", "pageSize": 1},
    )
    assert listing_response.status_code == 200, listing_response.text
    listing = listing_response.json()
    assert listing["returnedCount"] == 1
    assert listing["total"] == 2
    assert listing["canonicalQuery"]["provider"] == "arxiv"
    assert len(listing["queryDigest"]) == 64

    version_id = listing["items"][0]["paperVersionId"]
    detail_response = client.get(f"/api/topics/{TOPIC_A}/paper-bank/{version_id}")
    assert detail_response.status_code == 200, detail_response.text
    assert detail_response.json()["paperVersionId"] == version_id

    create_response = client.post(
        f"/api/topics/{TOPIC_A}/paper-collections",
        json={"name": "API collection"},
    )
    assert create_response.status_code == 201, create_response.text
    collection_id = create_response.json()["id"]
    add_response = client.put(
        f"/api/topics/{TOPIC_A}/paper-collections/{collection_id}/members/{version_id}"
    )
    repeated_add_response = client.put(
        f"/api/topics/{TOPIC_A}/paper-collections/{collection_id}/members/{version_id}"
    )
    assert add_response.status_code == 200, add_response.text
    assert repeated_add_response.json()["memberCount"] == 1

    filtered_response = client.get(
        f"/api/topics/{TOPIC_A}/paper-bank",
        params={"collectionId": collection_id},
    )
    assert filtered_response.status_code == 200, filtered_response.text
    assert [item["paperVersionId"] for item in filtered_response.json()["items"]] == [version_id]

    rename_response = client.patch(
        f"/api/topics/{TOPIC_A}/paper-collections/{collection_id}",
        json={"name": "API renamed"},
    )
    assert rename_response.json()["name"] == "API renamed"
    delete_response = client.delete(f"/api/topics/{TOPIC_A}/paper-collections/{collection_id}")
    assert delete_response.json() == {"id": collection_id, "deleted": True}

    unsupported = client.get(f"/api/topics/{TOPIC_A}/paper-bank", params={"citationMin": 10})
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"] == {
        "code": "paper_bank_filter_unsupported",
        "filters": ["citationMin"],
    }


def test_new_paper_bank_api_does_not_remove_the_existing_topic_papers_endpoint(
    client,
    paper_bank_fixture,
) -> None:
    response = client.get("/api/topics/large-language-models/papers")
    assert response.status_code == 200, response.text
    assert {"topic", "items", "total", "q", "providerFilter"}.issubset(response.json())

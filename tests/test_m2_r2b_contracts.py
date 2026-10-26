from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.repositories import paper_collections
from app.services import paper_bank
from tests.test_m2_paper_bank import TOPIC_A, TOPIC_B, _discoveries


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


def test_r2b_facets_are_server_backed_and_paper_type_stays_unsupported(
    client,
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        paper_bank,
        "_load_topic_reviews",
        lambda topic_id: [
            {"topic_id": TOPIC_A, "run_id": "run-a1", "paper_id": "2601.12345v1", "full_text_status": "abstract_only", "reviewed_at": "2026-08-01"},
            {"topic_id": TOPIC_A, "run_id": "run-a2", "paper_id": "2601.12345v2", "full_text_status": "full_text_available", "reviewed_at": "2026-08-02"},
        ],
    )
    listing = paper_bank.list_topic_papers(TOPIC_A, page_size=2)
    facets = listing["facets"]
    assert {option["value"] for option in facets["year"]["options"]} >= {"2025", "2026", "unknown"}
    assert {option["value"] for option in facets["reviewLevel"]["options"]} >= {"not_reviewed", "abstract_reviewed"}
    assert {option["value"] for option in facets["access"]["options"]} >= {"open_access", "unknown"}
    assert facets["access"] == facets["rights"]

    by_year = paper_bank.list_topic_papers(TOPIC_A, year="2025", page_size=50)
    assert by_year["total"] == 1
    assert by_year["items"][0]["publicationYear"] == 2025
    assert by_year["canonicalQuery"]["year"] == "2025"
    assert by_year["queryDigest"] != listing["queryDigest"]

    unknown_year = paper_bank.list_topic_papers(TOPIC_A, year="unknown", page_size=50)
    assert unknown_year["total"] >= 1
    assert all(item["publicationYearState"] == "unknown" for item in unknown_year["items"])
    with pytest.raises(paper_bank.PaperBankQueryInvalid, match="year_invalid"):
        paper_bank.list_topic_papers(TOPIC_A, year="20xx")

    unsupported = client.get(f"/api/topics/{TOPIC_A}/paper-bank", params={"paperType": "survey"})
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"] == {
        "code": "paper_bank_filter_unsupported",
        "filters": ["paperType"],
    }


def test_r2b_rights_filter_does_not_promote_declared_or_unknown(paper_bank_fixture) -> None:
    open_rows = paper_bank.list_topic_papers(TOPIC_A, open_access="open_access", page_size=50)
    assert open_rows["total"] == 1
    assert open_rows["items"][0]["accessState"] == "open_access"
    unknown_rows = paper_bank.list_topic_papers(TOPIC_A, open_access="unknown", page_size=50)
    assert unknown_rows["total"] >= 1
    assert all(item["accessState"] == "unknown" for item in unknown_rows["items"])
    with pytest.raises(paper_bank.PaperBankQueryInvalid, match="open_access_invalid"):
        paper_bank.list_topic_papers(TOPIC_A, open_access="maybe")
    assert paper_bank._rights({"rights": "CC-BY-4.0"})["state"] == "declared"


def test_r2b_csv_export_contains_all_rows_and_verified_artifact(
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(paper_bank, "PAPER_BANK_EXPORT_ROOT", tmp_path / "exports")
    listing = paper_bank.list_topic_papers(TOPIC_A, page=1, page_size=1, sort="title_asc")
    metadata = paper_bank.export_topic_papers_csv(TOPIC_A, sort="title_asc", page=1, page_size=1)
    assert metadata["format"] == "csv"
    assert metadata["rowCount"] == listing["total"]
    assert metadata["paperVersionIds"] == [item["paperVersionId"] for item in paper_bank.list_topic_papers(TOPIC_A, sort="title_asc", page_size=100)["items"]]
    artifact, persisted = paper_bank.resolve_paper_bank_export_download(TOPIC_A, metadata["exportId"])
    assert artifact.is_relative_to(tmp_path / "exports")
    assert artifact.read_bytes().startswith(b"paper_version_id,canonical_paper_id,title")
    assert persisted["contentSha256"] == metadata["contentSha256"]
    rows = list(csv.reader(io.StringIO(artifact.read_text(encoding="utf-8"))))
    assert len(rows) == metadata["rowCount"] + 1
    assert all(not value.startswith(("=", "+", "-", "@")) for row in rows for value in row)


def test_r2b_bulk_save_is_atomic_deduplicated_and_topic_scoped(
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = paper_collections.create_collection(TOPIC_A, "Bulk target")
    rows = paper_bank.list_topic_papers(TOPIC_A, page_size=50)["items"]
    selected = [rows[0]["paperVersionId"], rows[1]["paperVersionId"]]
    members, unique_count = paper_bank.resolve_paper_versions(TOPIC_A, [selected[0], selected[0], selected[1]])
    result = paper_collections.add_members_atomic(TOPIC_A, collection["id"], members)
    assert unique_count == 2
    assert result["requestedCount"] == 2
    assert result["uniqueCount"] == 2
    assert result["addedCount"] == 2
    repeated = paper_collections.add_members_atomic(TOPIC_A, collection["id"], members)
    assert repeated["addedCount"] == 0
    assert repeated["alreadyPresentCount"] == 2
    before = paper_collections.COLLECTION_LIBRARY_PATH.read_bytes()
    with pytest.raises(paper_bank.PaperBankQueryInvalid, match="not_found"):
        paper_bank.resolve_paper_versions(TOPIC_A, ["foreign-version-id"])
    assert paper_collections.COLLECTION_LIBRARY_PATH.read_bytes() == before
    with pytest.raises(paper_bank.PaperBankQueryInvalid, match="not_found"):
        paper_bank.resolve_paper_versions(TOPIC_B, selected)


def test_r2b_bulk_api_reports_exact_destination_counts(client, paper_bank_fixture) -> None:
    collection = client.post(f"/api/topics/{TOPIC_A}/paper-collections", json={"name": "API bulk"})
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["id"]
    rows = client.get(f"/api/topics/{TOPIC_A}/paper-bank", params={"pageSize": 50}).json()["items"]
    response = client.post(
        f"/api/topics/{TOPIC_A}/paper-collections/{collection_id}/members:bulk",
        json={"selectedPaperVersionIds": [rows[0]["paperVersionId"], rows[0]["paperVersionId"], rows[1]["paperVersionId"]]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["destinationCollectionId"] == collection_id
    assert payload["requestedCount"] == 3
    assert payload["uniqueCount"] == 2
    assert payload["addedCount"] == 2
    assert payload["finalMemberCount"] == 2


def test_r2b_evidence_destination_binds_exact_paper_version_and_evidence(
    paper_bank_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = paper_bank.list_topic_papers(TOPIC_A, page_size=50)["items"]
    target = next(row for row in rows if row["sourceIdentity"]["nativeId"] == "2601.12345v2")
    reviews = [{
        "topic_id": TOPIC_A,
        "run_id": "run-a2",
        "paper_id": "2601.12345v2",
        "full_text_status": "reviewed",
        "reviewed_at": "2026-08-03T00:00:00Z",
        "source_text_sha256": "a" * 64,
        "evidence_spans": [{
            "evidence_span_id": "evidence-exact",
            "locator": {"page": 4},
            "content_sha256": "b" * 64,
            "text": "Exact evidence.",
        }],
    }]
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda topic_id: reviews)
    detail = paper_bank.get_topic_paper_detail(TOPIC_A, target["paperVersionId"])
    evidence = detail["evidence"][0]
    assert evidence["destinationState"] == "available"
    assert evidence["destination"] == (
        f"/topics/{TOPIC_A}/papers?paper={target['paperVersionId']}&evidence=evidence-exact"
    )

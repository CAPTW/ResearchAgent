from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.domain import constants
from app.repositories import paper_collections, runs
from app.repositories.files import read_jsonl, write_json, write_jsonl
from app.repositories.jsonl import read_jsonl_strict_with_line_numbers
from app.services import paper_bank, paper_occurrences, ranking, workbench


OPAQUE_OCCURRENCE_ID = re.compile(r"^occ-[0-9a-f]{32}$")


def _rank_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    input_path = tmp_path / "ranking" / "inbox.jsonl"
    keywords_path = tmp_path / "ranking" / "keywords.json"
    themes_path = tmp_path / "ranking" / "themes.json"
    output_path = tmp_path / "ranking" / "triage.jsonl"
    output_md = tmp_path / "ranking" / "triage.md"
    write_json(keywords_path, {"primary": [], "secondary": [], "negative": []})
    write_json(themes_path, {})
    return input_path, keywords_path, themes_path, output_path, output_md


def _rank(
    input_path: Path,
    keywords_path: Path,
    themes_path: Path,
    output_path: Path,
    output_md: Path,
) -> list[dict]:
    ranking.rank_from_paths(
        input_path=input_path,
        keywords_path=keywords_path,
        themes_path=themes_path,
        output_jsonl=output_path,
        output_md=output_md,
    )
    return read_jsonl(output_path)


def _fallback_record(**overrides: object) -> dict:
    record = {
        "source_provider": "unknown",
        "title": "Byte-identical accepted observation",
        "authors": ["Same Author"],
        "abstract": "Two accepted occurrences may have identical descriptive metadata.",
        "score": 42,
        "status": "accepted",
    }
    record.update(overrides)
    return record


def _seed_run(run_id: str, topic: str, records: list[dict]) -> Path:
    paths = runs.ensure_run_layout(run_id)
    triage_path = paths.triage_dir / "triage.jsonl"
    write_jsonl(triage_path, records)
    runs.save_run_record(
        {
            "run_id": run_id,
            "topic": topic,
            "status": "completed",
            "source": "test",
            "write_legacy": False,
            "created_at": "2026-08-29T00:00:00Z",
            "updated_at": "2026-08-29T00:00:00Z",
            "request": {"source_provider": "unknown"},
            "artifacts": {"triage_jsonl": str(triage_path)},
        }
    )
    return triage_path


def _configure_bank(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    topic_id: str,
    topic: str,
) -> None:
    monkeypatch.setattr(
        paper_collections,
        "COLLECTION_LIBRARY_PATH",
        tmp_path / "paper_collections" / "collections.json",
    )
    monkeypatch.setattr(
        paper_bank,
        "_load_topic_profile",
        lambda selected_topic_id, actor=None: {
            "profile_id": selected_topic_id,
            "topic": topic,
            "title": topic,
        },
    )
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda selected_topic_id: [])


def _identity_pair(item: dict) -> tuple[str, str, str]:
    return (
        str(item["canonicalPaperId"]),
        str(item["paperVersionId"]),
        str(item["compatibility"]["paperId"]),
    )


def test_new_triage_persistence_assigns_distinct_occurrence_ids_once(tmp_path: Path) -> None:
    input_path, keywords_path, themes_path, output_path, output_md = _rank_paths(tmp_path)
    write_jsonl(input_path, [_fallback_record(), _fallback_record()])

    first = _rank(input_path, keywords_path, themes_path, output_path, output_md)
    occurrence_ids = [item.get("occurrence_id") for item in first]

    assert all(isinstance(value, str) and OPAQUE_OCCURRENCE_ID.fullmatch(value) for value in occurrence_ids)
    assert len(set(occurrence_ids)) == 2

    second_output = tmp_path / "ranking" / "triage-second.jsonl"
    second_md = tmp_path / "ranking" / "triage-second.md"
    second = _rank(output_path, keywords_path, themes_path, second_output, second_md)
    assert [item["occurrence_id"] for item in second] == occurrence_ids


def test_only_absent_or_null_occurrence_authority_is_missing(tmp_path: Path) -> None:
    input_path, keywords_path, themes_path, output_path, output_md = _rank_paths(tmp_path)
    write_jsonl(
        input_path,
        [
            _fallback_record(),
            _fallback_record(occurrence_id=None),
        ],
    )

    ranked = _rank(input_path, keywords_path, themes_path, output_path, output_md)
    occurrence_ids = [item.get("occurrence_id") for item in ranked]

    assert all(isinstance(value, str) and OPAQUE_OCCURRENCE_ID.fullmatch(value) for value in occurrence_ids)
    assert len(set(occurrence_ids)) == 2


@pytest.mark.parametrize("malformed", ["", "  \t  "])
def test_blank_supplied_occurrence_authority_fails_closed(tmp_path: Path, malformed: str) -> None:
    input_path, keywords_path, themes_path, output_path, output_md = _rank_paths(tmp_path)
    write_jsonl(input_path, [_fallback_record(occurrence_id=malformed)])

    with pytest.raises(ValueError, match="occurrence_identity_invalid"):
        _rank(input_path, keywords_path, themes_path, output_path, output_md)

    assert not output_path.exists()


@pytest.mark.parametrize("malformed", [["not", "scalar"], {"not": "scalar"}, 17, True])
def test_malformed_occurrence_authority_fails_closed(tmp_path: Path, malformed: object) -> None:
    input_path, keywords_path, themes_path, output_path, output_md = _rank_paths(tmp_path)
    write_jsonl(input_path, [_fallback_record(occurrence_id=malformed)])

    with pytest.raises(ValueError, match="occurrence_identity_invalid"):
        _rank(input_path, keywords_path, themes_path, output_path, output_md)

    assert not output_path.exists()


def test_persisted_legacy_identical_records_remain_distinct_and_hide_absolute_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic_id = "legacy-identical-topic"
    topic = "Legacy identical topic"
    absolute_ref = str(tmp_path / "owner-private" / "raw" / "metadata.json")
    record = _fallback_record(raw_metadata_ref=absolute_ref)
    _seed_run("legacy-identical-run", topic, [record, dict(record)])
    _configure_bank(monkeypatch, tmp_path, topic_id=topic_id, topic=topic)

    listing = paper_bank.list_topic_papers(topic_id, page_size=50)
    serialized = json.dumps(listing, ensure_ascii=False)

    assert listing["total"] == 2
    assert len(listing["items"]) == 2
    assert len({item["canonicalPaperId"] for item in listing["items"]}) == 2
    assert len({item["paperVersionId"] for item in listing["items"]}) == 2
    assert len({item["compatibility"]["paperId"] for item in listing["items"]}) == 2
    assert absolute_ref not in serialized
    assert str(tmp_path) not in serialized
    assert "file://" not in serialized.casefold()


def test_explicit_occurrence_authority_precedes_raw_locator_and_is_path_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic_id = "explicit-occurrence-topic"
    topic = "Explicit occurrence topic"
    raw_ref = str(tmp_path / "private-root" / "same-record.json")
    _seed_run(
        "explicit-occurrence-run",
        topic,
        [
            _fallback_record(occurrence_id="occ-" + "a" * 32, raw_metadata_ref=raw_ref),
            _fallback_record(occurrence_id="occ-" + "b" * 32, raw_metadata_ref=raw_ref),
        ],
    )
    _configure_bank(monkeypatch, tmp_path, topic_id=topic_id, topic=topic)

    listing = paper_bank.list_topic_papers(topic_id, page_size=50)
    compatibility_ids = {item["compatibility"]["paperId"] for item in listing["items"]}
    serialized = json.dumps(listing, ensure_ascii=False)

    assert listing["total"] == 2
    assert compatibility_ids == {"occ-" + "a" * 32, "occ-" + "b" * 32}
    assert raw_ref not in serialized
    assert str(tmp_path) not in serialized


def test_legacy_identity_is_stable_across_metadata_mutation_and_data_root_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic_id = "legacy-relocation-topic"
    topic = "Legacy relocation topic"
    run_id = "legacy-relocation-run"
    triage_path = _seed_run(run_id, topic, [_fallback_record()])
    _configure_bank(monkeypatch, tmp_path, topic_id=topic_id, topic=topic)

    initial = paper_bank.list_topic_papers(topic_id, page_size=50)["items"][0]
    initial_identity = _identity_pair(initial)

    write_jsonl(
        triage_path,
        [
            _fallback_record(
                title="Corrected title",
                authors=["Normalized Author"],
                score=99,
                status="reviewed",
                entry_url="https://example.test/corrected",
            )
        ],
    )
    mutated = paper_bank.list_topic_papers(topic_id, page_size=50)["items"][0]
    assert _identity_pair(mutated) == initial_identity

    relocated_data_root = tmp_path / "relocated-data"
    shutil.copytree(constants.STORAGE_ROOT.parent, relocated_data_root)
    monkeypatch.setattr(constants, "STORAGE_ROOT", relocated_data_root / "runs")
    monkeypatch.setattr(runs, "STORAGE_ROOT", relocated_data_root / "runs")

    relocated = paper_bank.list_topic_papers(topic_id, page_size=50)["items"][0]
    assert _identity_pair(relocated) == initial_identity


def test_strict_jsonl_reader_retains_physical_lines_and_legacy_load_is_non_destructive(
    tmp_path: Path,
) -> None:
    run_id = "physical-line-authority-run"
    paths = runs.ensure_run_layout(run_id)
    triage_path = paths.triage_dir / "triage.jsonl"
    first = json.dumps(_fallback_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    second = json.dumps(_fallback_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    triage_path.write_text(f"\n{first}\n\n{second}\n", encoding="utf-8", newline="\n")
    runs.save_run_record(
        {
            "run_id": run_id,
            "topic": "Physical line authority",
            "status": "completed",
            "created_at": "2026-08-29T00:00:00Z",
            "request": {"source_provider": "unknown"},
            "artifacts": {"triage_jsonl": str(triage_path)},
        }
    )
    before = triage_path.read_bytes()

    line_records = read_jsonl_strict_with_line_numbers(triage_path)
    loaded = workbench.load_run_triage_items(run_id)

    assert [line for line, _record in line_records] == [2, 4]
    assert [record["occurrence_id"] for record in loaded] == [
        paper_occurrences.legacy_coordinate_id(
            run_id=run_id,
            logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
            physical_line_number=2,
        ),
        paper_occurrences.legacy_coordinate_id(
            run_id=run_id,
            logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
            physical_line_number=4,
        ),
    ]
    assert loaded[0]["occurrence_id"] != loaded[1]["occurrence_id"]
    assert triage_path.read_bytes() == before


def test_fallback_identity_is_field_order_and_cross_run_stable() -> None:
    occurrence_id = "occ-" + "f" * 32
    item = _fallback_record(occurrence_id=occurrence_id)
    reordered = {key: item[key] for key in reversed(list(item))}
    run_a = {"run_id": "authority-run-a", "request": {"source_provider": "unknown"}}
    run_b = {"run_id": "authority-run-b", "request": {"source_provider": "unknown"}}

    first = paper_bank._identity(run_a, item)
    assert paper_bank._identity(run_a, reordered) == first
    assert paper_bank._identity(run_b, item)["workKey"] != first["workKey"]
    assert first["workKey"] != first["versionKey"]


@pytest.mark.parametrize(
    "path_shaped",
    [
        r"C:\owner\metadata.json",
        r"C:metadata.json",
        r"\\server\share\metadata.json",
        r"\\?\C:\owner\metadata.json",
        r"\\.\PhysicalDrive0",
        "/owner/metadata.json",
        "file:///C:/owner/metadata.json",
        "../metadata.json",
        "folder/metadata.json",
        "folder\\metadata.json",
        "metadata.json:$DATA",
        "CON",
    ],
)
def test_path_shaped_occurrence_ids_fail_closed(path_shaped: str) -> None:
    with pytest.raises(ValueError, match="occurrence_identity_invalid"):
        paper_occurrences.normalize_occurrence_id(path_shaped)


@pytest.mark.parametrize(
    "raw_locator",
    [
        r"C:\Users\owner\private\metadata.json",
        r"\\server\share\metadata.json",
        r"\\?\D:\task\metadata.json",
        r"\\.\PhysicalDrive0",
        "/private/metadata.json",
        "file:///D:/task/metadata.json",
        "../metadata.json",
        "D:/Mixed\\Slash/metadata.json",
    ],
)
def test_raw_locator_representation_never_changes_or_exposes_fallback_identity(raw_locator: str) -> None:
    run = {"run_id": "raw-locator-run", "request": {"source_provider": "unknown"}}
    base = _fallback_record(occurrence_id="occ-" + "c" * 32)
    identity = paper_bank._identity(run, base)
    with_locator = {**base, "raw_metadata_ref": raw_locator}

    assert paper_bank._identity(run, with_locator) == identity
    compatibility_id = paper_bank._compatibility_paper_id(with_locator, identity)
    assert compatibility_id == "occ-" + "c" * 32
    assert raw_locator not in compatibility_id


def test_ambiguous_legacy_review_alias_does_not_bind_to_either_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic_id = "ambiguous-alias-topic"
    topic = "Ambiguous alias topic"
    shared_alias = str(tmp_path / "private" / "same.json")
    _seed_run(
        "ambiguous-alias-run",
        topic,
        [
            _fallback_record(occurrence_id="occ-" + "d" * 32, raw_metadata_ref=shared_alias),
            _fallback_record(occurrence_id="occ-" + "e" * 32, raw_metadata_ref=shared_alias),
        ],
    )
    _configure_bank(monkeypatch, tmp_path, topic_id=topic_id, topic=topic)
    monkeypatch.setattr(
        paper_bank,
        "_load_topic_reviews",
        lambda selected_topic_id: [
            {
                "run_id": "ambiguous-alias-run",
                "paper_id": shared_alias,
                "full_text_status": "reviewed",
                "reviewed_at": "2026-08-29T00:00:00Z",
                "evidence_spans": [{"evidence_span_id": "must-not-bind"}],
            }
        ],
    )

    listing = paper_bank.list_topic_papers(topic_id, page_size=50)

    assert listing["total"] == 2
    assert {item["review"]["state"] for item in listing["items"]} == {"not_reviewed"}
    assert {item["review"]["evidenceCount"] for item in listing["items"]} == {0}
    assert shared_alias not in json.dumps(listing, ensure_ascii=False)


def test_no_discriminator_list_detail_collection_review_fulltext_and_evidence_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic_id = "downstream-isolation-topic"
    topic = "Downstream isolation topic"
    run_id = "downstream-isolation-run"
    record = _fallback_record()
    _seed_run(run_id, topic, [record, dict(record)])
    _configure_bank(monkeypatch, tmp_path, topic_id=topic_id, topic=topic)
    loaded = workbench.load_run_triage_items(run_id)
    occurrence_a = loaded[0]["occurrence_id"]
    occurrence_b = loaded[1]["occurrence_id"]
    assert workbench.get_raw_paper(run_id, occurrence_a)["occurrence_id"] == occurrence_a
    assert workbench.get_raw_paper(run_id, occurrence_b)["occurrence_id"] == occurrence_b

    evidence_text = "Evidence belongs only to no-discriminator occurrence A."
    reviews = [
        {
            "run_id": run_id,
            "paper_id": occurrence_a,
            "full_text_status": "reviewed",
            "reviewed_at": "2026-08-29T00:00:00Z",
            "full_text_artifact_path": "run:raw/no-discriminator-a.txt",
            "source_text_sha256": "a" * 64,
            "evidence_spans": [
                {
                    "evidence_span_id": "no-discriminator-evidence-a",
                    "locator": {"page": 11, "section": "Occurrence authority"},
                    "content_sha256": "b" * 64,
                    "text": evidence_text,
                }
            ],
        }
    ]
    monkeypatch.setattr(paper_bank, "_load_topic_reviews", lambda selected_topic_id: reviews)

    listing = paper_bank.list_topic_papers(topic_id, page_size=50)
    assert listing["total"] == 2
    by_occurrence = {item["compatibility"]["paperId"]: item for item in listing["items"]}
    assert set(by_occurrence) == {occurrence_a, occurrence_b}
    row_a = by_occurrence[occurrence_a]
    row_b = by_occurrence[occurrence_b]
    assert row_a["review"] == {
        "state": "full_text_reviewed",
        "updatedAt": "2026-08-29T00:00:00Z",
        "evidenceCount": 1,
    }
    assert row_b["review"] == {"state": "not_reviewed", "updatedAt": None, "evidenceCount": 0}

    detail_a = paper_bank.get_topic_paper_detail(topic_id, row_a["paperVersionId"])
    detail_b = paper_bank.get_topic_paper_detail(topic_id, row_b["paperVersionId"])
    assert detail_a["fullText"] == {
        "state": "reviewed",
        "artifactRef": "run:raw/no-discriminator-a.txt",
        "contentDigest": "a" * 64,
    }
    assert len(detail_a["evidence"]) == 1
    evidence_a = detail_a["evidence"][0]
    assert evidence_a["id"] == "no-discriminator-evidence-a"
    assert evidence_a["locator"] == {"page": 11, "section": "Occurrence authority"}
    assert evidence_a["contentDigest"] == "b" * 64
    assert evidence_a["text"] == evidence_text
    assert evidence_a["destinationState"] == "available"
    destination_a = evidence_a["destination"]
    assert isinstance(destination_a, str)
    assert destination_a != ""
    parsed_destination_a = urlparse(destination_a)
    assert parsed_destination_a.path == f"/topics/{topic_id}/papers"
    destination_params_a = parse_qs(parsed_destination_a.query, keep_blank_values=True)
    assert destination_params_a["paper"] == [row_a["paperVersionId"]]
    assert destination_params_a["evidence"] == ["no-discriminator-evidence-a"]
    assert "run:raw" not in destination_a
    assert "Byte-identical accepted observation" not in destination_a
    assert row_b["paperVersionId"] not in destination_a
    assert detail_b["fullText"] == {"state": "unavailable", "artifactRef": None, "contentDigest": None}
    assert detail_b["evidence"] == []
    assert detail_b["evidence"] != detail_a["evidence"]
    assert all(item.get("id") != evidence_a["id"] for item in detail_b["evidence"])
    assert all(item.get("locator") != evidence_a["locator"] for item in detail_b["evidence"])
    assert all(item.get("contentDigest") != evidence_a["contentDigest"] for item in detail_b["evidence"])
    assert all(item.get("destination") != destination_a for item in detail_b["evidence"])

    collection = paper_collections.create_collection(topic_id, "Occurrence A only")
    paper_collections.add_member(
        topic_id,
        collection["id"],
        canonical_paper_id=row_a["canonicalPaperId"],
        paper_version_id=row_a["paperVersionId"],
    )
    paper_collections.add_member(
        topic_id,
        collection["id"],
        canonical_paper_id=row_a["canonicalPaperId"],
        paper_version_id=row_a["paperVersionId"],
    )
    filtered = paper_bank.list_topic_papers(topic_id, collection_id=collection["id"], page_size=50)
    assert [item["paperVersionId"] for item in filtered["items"]] == [row_a["paperVersionId"]]
    assert paper_collections.get_collection(topic_id, collection["id"])["memberCount"] == 1

    reviewed_only = paper_bank.list_topic_papers(
        topic_id,
        q="Byte-identical accepted observation",
        review_state="full_text_reviewed",
        page_size=50,
    )
    assert [item["paperVersionId"] for item in reviewed_only["items"]] == [row_a["paperVersionId"]]
    page_one = paper_bank.list_topic_papers(
        topic_id,
        q="Byte-identical accepted observation",
        sort="title_asc",
        page=1,
        page_size=1,
    )
    page_two = paper_bank.list_topic_papers(
        topic_id,
        q="Byte-identical accepted observation",
        sort="title_asc",
        page=2,
        page_size=1,
    )
    assert {
        page_one["items"][0]["paperVersionId"],
        page_two["items"][0]["paperVersionId"],
    } == {row_a["paperVersionId"], row_b["paperVersionId"]}
    assert page_one["queryDigest"] != page_two["queryDigest"]

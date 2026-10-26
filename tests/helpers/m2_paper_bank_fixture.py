from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from app.db.bootstrap import bootstrap_runtime_stores
from app.domain.constants import STORAGE_ROOT
from app.repositories.files import ensure_dir, write_json, write_jsonl
from app.repositories import paper_collections, paper_reviews
from app.services import paper_bank, paper_occurrences


FIXTURE_SCHEMA_VERSION = "m2-paper-bank-real-backend-fixture-v1"
FIXED_TIMESTAMP = "2026-08-28T04:00:00+00:00"
TOPIC_A_ID = "m2-spatial-paper-bank"
TOPIC_A_NAME = "M2 Spatial Paper Bank"
TOPIC_B_ID = "m2-robotics-paper-bank"
TOPIC_B_NAME = "M2 Robotics Paper Bank"
RUN_A_1 = "m2-paper-bank-topic-a-run-1"
RUN_A_2 = "m2-paper-bank-topic-a-run-2"
RUN_B_1 = "m2-paper-bank-topic-b-run-1"
FALLBACK_COLLISION_TITLE = "Fallback Collision Paper"
FALLBACK_OCCURRENCE_A = "occ-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FALLBACK_OCCURRENCE_B = "occ-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NO_DISCRIMINATOR_TITLE = "No Discriminator Identical Paper"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_fixture_root(root: Path) -> Path:
    resolved = root.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    if not _is_relative_to(resolved, temp_root):
        raise ValueError(f"fixture root must be beneath task temp: {resolved}")
    if _is_relative_to(resolved, repository_root) or _is_relative_to(repository_root, resolved):
        raise ValueError(f"fixture root overlaps repository: {resolved}")
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError(f"fixture root must be empty: {resolved}")
    configured = Path(os.environ.get("XRML_DATA_DIR", "")).resolve()
    if configured != resolved or STORAGE_ROOT.parent != resolved:
        raise ValueError("XRML_DATA_DIR must equal the fixture root before Python imports app modules")
    return resolved


def _profile(profile_id: str, title: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "resource_kind": "user_owned",
        "owner_user_id": "local-internal-user",
        "title": title,
        "description": "Synthetic local-only M2 browser acceptance topic.",
        "topic": title,
        "queries": [title],
        "categories": ["cs.HC"],
        "max_results": 25,
        "source_provider": "arxiv",
        "arxiv_sort_by": "submittedDate",
        "arxiv_sort_order": "descending",
        "top_papers": 5,
        "weekly_top": 10,
        "is_favorite": False,
        "status": "active",
        "archived_at": None,
        "default_collection_cadence": "manual",
        "default_report_cadence": "manual",
        "created_at": FIXED_TIMESTAMP,
        "updated_at": FIXED_TIMESTAMP,
    }


def _run(run_id: str, topic_id: str, topic: str, items: list[dict[str, Any]], sequence: int) -> None:
    run_root = STORAGE_ROOT / run_id
    for directory in ("inputs", "raw", "triage", "notes", "reviews", "reports"):
        ensure_dir(run_root / directory)
    timestamp = f"2026-08-28T04:{sequence:02d}:00+00:00"
    write_json(
        run_root / "run.json",
        {
            "run_id": run_id,
            "topic": topic,
            "topic_profile_id": topic_id,
            "status": "completed",
            "source": "m2_synthetic_browser_fixture",
            "write_legacy": False,
            "created_at": timestamp,
            "updated_at": timestamp,
            "started_at": timestamp,
            "finished_at": timestamp,
            "request": {"source_provider": "mixed-fixture"},
            "owner_user_id": "local-internal-user",
            "owner_assigned_at": timestamp,
            "owner_assigned_by": "m2_synthetic_browser_fixture",
            "ownership_source": "test_fixture",
            "steps": [],
            "counts": {"triage_items": len(items)},
            "warnings": ["Synthetic local-only browser acceptance fixture."],
            "artifacts": {"triage_jsonl": "run:triage/triage.jsonl"},
            "lineage": {
                "parent_run_id": None,
                "root_run_id": run_id,
                "lineage_mode": "standalone",
                "source_paper_ids": [],
                "created_from_profile_snapshot": True,
                "compare_baseline_run_id": None,
                "branch_reason": None,
                "created_at": timestamp,
                "notes": "Synthetic M2 acceptance lineage.",
            },
        },
    )
    write_jsonl(run_root / "triage" / "triage.jsonl", items)


def _paper(
    paper_id: str,
    title: str,
    *,
    provider: str,
    published: str,
    authors: list[str],
    abstract: str,
    entry_url: str | None = None,
    doi: str | None = None,
    version: str | None = None,
    open_access: bool | None = None,
    rights: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "paper_id": paper_id,
        "title": title,
        "source_provider": provider,
        "published": published,
        "updated": published,
        "authors": authors,
        "abstract": abstract,
        "theme": "M2 deterministic paper bank",
        "status": "reviewed",
    }
    for key, value in {
        "entry_url": entry_url,
        "doi": doi,
        "version": version,
        "open_access": open_access,
        "rights": rights,
    }.items():
        if value is not None:
            payload[key] = value
    return payload


def _fallback_paper(
    occurrence_id: str,
    *,
    authors: list[str],
    entry_url: str,
    raw_metadata_ref: str,
) -> dict[str, Any]:
    return {
        "occurrence_id": occurrence_id,
        "raw_metadata_ref": raw_metadata_ref,
        "title": FALLBACK_COLLISION_TITLE,
        "source_provider": "fixture-fallback",
        "published": "2026-08-18T00:00:00Z",
        "updated": "2026-08-18T00:00:00Z",
        "authors": authors,
        "abstract": f"Synthetic fallback observation {occurrence_id}.",
        "entry_url": entry_url,
        "theme": "M2 deterministic paper bank",
        "status": "reviewed",
    }


def _review(
    *,
    run_id: str,
    paper_id: str,
    title: str,
    full_text_status: str,
    evidence_spans: list[dict[str, Any]],
    full_text_artifact_path: str | None = None,
) -> None:
    paper_reviews.upsert_review(
        {
            "topic_id": TOPIC_A_ID,
            "run_id": run_id,
            "paper_id": paper_id,
            "title": title,
            "source_provider": "arxiv",
            "authors": ["M2 Fixture Author"],
            "published_at": "2026-08-20",
            "abstract_text": f"Reviewed abstract for {title}.",
            "text_excerpt": f"Reviewed excerpt for {title}.",
            "full_text_status": full_text_status,
            "relevance_score": 0.95,
            "novelty_score": 0.81,
            "evidence_strength": 0.88,
            "method_summary": "Deterministic local-only fixture method.",
            "key_findings": ["The M2 identity remains stable across collection mutations."],
            "limitations": ["Synthetic fixture; not scholarly evidence."],
            "why_it_matters": "Exercises review and evidence projection without external retrieval.",
            "topic_fit_rationale": "M2 paper bank acceptance.",
            "review_model": "deterministic-fixture",
            "reviewer": "m2-fixture",
            "reviewed_at": FIXED_TIMESTAMP,
            "source_text_sha256": hashlib.sha256(title.encode()).hexdigest(),
            "full_text_artifact_path": full_text_artifact_path,
            "text_char_count": 512 if full_text_artifact_path else 0,
            "evidence_spans": evidence_spans,
            "review_source_type": "full_text" if full_text_status == "reviewed" else "abstract",
            "full_text_reviewed_at": FIXED_TIMESTAMP if full_text_status == "reviewed" else None,
        }
    )


def seed_fixture(root: Path) -> dict[str, Any]:
    root = _assert_fixture_root(root)
    ensure_dir(root)
    write_json(
        root / "topic_profiles" / "profiles.json",
        [_profile(TOPIC_A_ID, TOPIC_A_NAME), _profile(TOPIC_B_ID, TOPIC_B_NAME)],
    )

    absolute_locator = str(root / "private-machine-root" / "metadata.json")
    no_discriminator_record = {
        "title": NO_DISCRIMINATOR_TITLE,
        "source_provider": "fixture-fallback",
        "published": "2026-08-15T00:00:00Z",
        "updated": "2026-08-15T00:00:00Z",
        "authors": ["Identical Legacy Author"],
        "abstract": "Two byte-identical accepted observations without explicit identity fields.",
        "theme": "M2 deterministic paper bank",
        "status": "reviewed",
    }

    repeated_v1 = _paper(
        "2608.00001v1",
        "Spatial Evidence Bank Foundations",
        provider="arxiv",
        published="2026-08-20",
        authors=["Ada Vector", "Min Grid"],
        abstract="A deterministic abstract about spatial evidence banks and paper identity.",
        entry_url="https://arxiv.org/abs/2608.00001v1",
    )
    _run(
        RUN_A_1,
        TOPIC_A_ID,
        TOPIC_A_NAME,
        [
            repeated_v1,
            _paper(
                "doi-provider-a",
                "Rights-Aware Spatial Literature",
                provider="crossref",
                published="2026-08-19",
                authors=["Rhea Rights"],
                abstract="A DOI-backed paper with explicit open-access rights.",
                entry_url="https://doi.org/10.5555/m2.paper.1",
                doi="10.5555/M2.PAPER.1",
                version="published",
                open_access=True,
                rights="CC-BY-4.0",
            ),
            _paper(
                "restricted-provider-a",
                "Restricted Spatial Methods",
                provider="crossref",
                published="2024-04-03",
                authors=["Rita Restricted"],
                abstract="A source-backed non-open record for rights facet coverage.",
                open_access=False,
                rights="Institutional access",
            ),
            _paper(
                "declared-provider-a",
                "Declared Rights, Malformed Date",
                provider="crossref",
                published="not-a-date",
                authors=["Dara Declared"],
                abstract="A declared license is not proof of open access.",
                rights="CC-BY-NC-4.0",
            ),
            _paper(
                "csv-special-a",
                '=Formula, "Quoted"\n유니코드 Paper',
                provider="openalex",
                published="2023-05-06",
                authors=["Author, One", "저자 Two"],
                abstract="+formula\n-newline @mention",
                rights="Custom license",
            ),
            {
                "paper_id": "csv-missing-a",
                "title": "Missing Optional Metadata",
                "source_provider": "openalex",
                "authors": [],
                "theme": "M2 deterministic paper bank",
                "status": "reviewed",
            },
            _paper(
                "s2-native-001",
                "Provider Native Paper Without URL",
                provider="semantic-scholar",
                published="2026-08-18",
                authors=["Nora Native"],
                abstract="Provider-native identity with no DOI and no verified source URL.",
            ),
            _paper(
                "title-similar-a",
                "A Similar Paper Title",
                provider="crossref",
                published="2026-08-17",
                authors=["First Identity"],
                abstract="Title similarity must not merge provider-native identities.",
            ),
            _fallback_paper(
                FALLBACK_OCCURRENCE_A,
                authors=["Fallback Author A"],
                entry_url="https://fallback-a.example/paper",
                raw_metadata_ref=absolute_locator,
            ),
            _fallback_paper(
                FALLBACK_OCCURRENCE_B,
                authors=["Fallback Author B"],
                entry_url="https://fallback-b.example/paper",
                raw_metadata_ref=absolute_locator,
            ),
            no_discriminator_record,
            dict(no_discriminator_record),
            *[
                _paper(
                    f"facet-extra-{index:02d}",
                    f"Facet Coverage Record {index:02d}",
                    provider="arxiv" if index % 2 else "crossref",
                    published=f"2022-01-{index:02d}",
                    authors=[f"Facet Author {index:02d}"],
                    abstract="Synthetic pagination and facet coverage record.",
                    open_access=True if index % 4 == 0 else False if index % 4 == 1 else None,
                    rights="CC-BY-4.0" if index % 4 == 2 else None,
                )
                for index in range(1, 17)
            ],
        ],
        1,
    )
    _run(
        RUN_A_2,
        TOPIC_A_ID,
        TOPIC_A_NAME,
        [
            {**repeated_v1, "abstract": "The same v1 work observed again in a second run."},
            _paper(
                "2608.00001v2",
                "Spatial Evidence Bank Foundations",
                provider="arxiv",
                published="2026-08-22",
                authors=["Ada Vector", "Min Grid"],
                abstract="Version two adds a deterministic evidence locator.",
                entry_url="https://arxiv.org/abs/2608.00001v2",
            ),
            _paper(
                "title-similar-b",
                "A Similar Paper Title",
                provider="crossref",
                published="2026-08-16",
                authors=["Second Identity"],
                abstract="Same title, distinct provider-native identity and no merge.",
            ),
        ],
        2,
    )
    _run(
        RUN_B_1,
        TOPIC_B_ID,
        TOPIC_B_NAME,
        [
            _paper(
                "robotics-only-001",
                "Topic B Robotics-Only Corpus",
                provider="semantic-scholar",
                published="2026-08-21",
                authors=["Robo Topic"],
                abstract="This paper must never appear in topic A.",
                entry_url="https://example.org/m2/robotics-only-001",
            )
        ],
        3,
    )

    raw_path = STORAGE_ROOT / RUN_A_2 / "raw" / "2608.00001v2.txt"
    raw_path.write_text("Synthetic full text for M2 paper version two.\n", encoding="utf-8")
    fallback_raw_path = STORAGE_ROOT / RUN_A_1 / "raw" / "fallback-collision-a.txt"
    fallback_raw_path.write_text("Synthetic full text for fallback occurrence A.\n", encoding="utf-8")
    no_discriminator_raw_path = STORAGE_ROOT / RUN_A_1 / "raw" / "no-discriminator-a.txt"
    no_discriminator_raw_path.write_text("Synthetic full text for no-discriminator occurrence A.\n", encoding="utf-8")
    bootstrap_runtime_stores()
    _review(
        run_id=RUN_A_1,
        paper_id="2608.00001v1",
        title="Spatial Evidence Bank Foundations",
        full_text_status="abstract_only",
        evidence_spans=[],
    )
    _review(
        run_id=RUN_A_1,
        paper_id="s2-native-001",
        title="Provider Native Paper Without URL",
        full_text_status="full_text_available",
        evidence_spans=[],
    )
    no_discriminator_a_id = paper_occurrences.legacy_coordinate_id(
        run_id=RUN_A_1,
        logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
        physical_line_number=11,
    )
    no_discriminator_b_id = paper_occurrences.legacy_coordinate_id(
        run_id=RUN_A_1,
        logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
        physical_line_number=12,
    )
    no_discriminator_evidence = "Evidence remains bound only to persisted physical occurrence A."
    _review(
        run_id=RUN_A_1,
        paper_id=no_discriminator_a_id,
        title=NO_DISCRIMINATOR_TITLE,
        full_text_status="reviewed",
        full_text_artifact_path="run:raw/no-discriminator-a.txt",
        evidence_spans=[
            {
                "evidence_span_id": "m2-no-discriminator-evidence-a",
                "locator": {"page": 11, "section": "Occurrence authority"},
                "content_sha256": hashlib.sha256(no_discriminator_evidence.encode()).hexdigest(),
                "text": no_discriminator_evidence,
            }
        ],
    )
    evidence_text = "The deterministic paper bank preserves paper-version identity after restart."
    _review(
        run_id=RUN_A_2,
        paper_id="2608.00001v2",
        title="Spatial Evidence Bank Foundations",
        full_text_status="reviewed",
        full_text_artifact_path="run:raw/2608.00001v2.txt",
        evidence_spans=[
            {
                "evidence_span_id": "m2-evidence-v2-001",
                "locator": {"page": 2, "paragraph": 3},
                "content_sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
                "text": evidence_text,
            }
        ],
    )
    fallback_evidence_text = "Fallback evidence is bound only to stored occurrence A."
    _review(
        run_id=RUN_A_1,
        paper_id=FALLBACK_OCCURRENCE_A,
        title=FALLBACK_COLLISION_TITLE,
        full_text_status="reviewed",
        full_text_artifact_path="run:raw/fallback-collision-a.txt",
        evidence_spans=[
            {
                "evidence_span_id": "m2-fallback-evidence-a",
                "locator": {"page": 7, "section": "Occurrence A"},
                "content_sha256": hashlib.sha256(fallback_evidence_text.encode()).hexdigest(),
                "text": fallback_evidence_text,
            }
        ],
    )

    listing = paper_bank.list_topic_papers(TOPIC_A_ID, page_size=100)
    v2 = next(item for item in listing["items"] if item["sourceIdentity"]["nativeId"] == "2608.00001v2")
    fallback_rows = [item for item in listing["items"] if item["title"] == FALLBACK_COLLISION_TITLE]
    fallback_by_occurrence = {item["compatibility"]["paperId"]: item for item in fallback_rows}
    no_discriminator_rows = [item for item in listing["items"] if item["title"] == NO_DISCRIMINATOR_TITLE]
    no_discriminator_by_occurrence = {
        item["compatibility"]["paperId"]: item for item in no_discriminator_rows
    }
    no_discriminator_public_ids = {
        occurrence_id: paper_occurrences.public_compatibility_id({"occurrence_id": occurrence_id})
        for occurrence_id in (no_discriminator_a_id, no_discriminator_b_id)
    }
    populated = paper_collections.create_collection(TOPIC_A_ID, "Seeded Reading Queue")
    paper_collections.add_member(
        TOPIC_A_ID,
        populated["id"],
        canonical_paper_id=v2["canonicalPaperId"],
        paper_version_id=v2["paperVersionId"],
    )
    empty = paper_collections.create_collection(TOPIC_A_ID, "Seeded Empty Queue")
    paper_collections.create_collection(TOPIC_B_ID, "Topic B Queue")

    result = {
        "schemaVersion": FIXTURE_SCHEMA_VERSION,
        "synthetic": True,
        "root": str(root),
        "topicA": TOPIC_A_ID,
        "topicB": TOPIC_B_ID,
        "topicAPaperVersions": listing["total"],
        "repeatedV1Occurrences": next(
            len(item["compatibility"]["occurrences"])
            for item in listing["items"]
            if item["sourceIdentity"]["nativeId"] == "2608.00001v1"
        ),
        "distinctV2PaperVersionId": v2["paperVersionId"],
        "fallbackCollision": {
            "count": len(fallback_rows),
            "occurrenceA": FALLBACK_OCCURRENCE_A,
            "occurrenceB": FALLBACK_OCCURRENCE_B,
            "paperVersionA": fallback_by_occurrence[FALLBACK_OCCURRENCE_A]["paperVersionId"],
            "paperVersionB": fallback_by_occurrence[FALLBACK_OCCURRENCE_B]["paperVersionId"],
        },
        "noDiscriminator": {
            "count": len(no_discriminator_rows),
            "occurrenceA": no_discriminator_a_id,
            "occurrenceB": no_discriminator_b_id,
            "paperVersionA": no_discriminator_by_occurrence[no_discriminator_public_ids[no_discriminator_a_id]]["paperVersionId"],
            "paperVersionB": no_discriminator_by_occurrence[no_discriminator_public_ids[no_discriminator_b_id]]["paperVersionId"],
        },
        "populatedCollectionId": populated["id"],
        "emptyCollectionId": empty["id"],
    }
    write_json(root / "m2-fixture-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(seed_fixture(args.root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

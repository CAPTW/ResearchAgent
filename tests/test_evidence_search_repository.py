from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.db import bootstrap
from app.repositories import evidence_search, metadata_index


def test_evidence_search_repository_module_exists() -> None:
    assert importlib.util.find_spec("app.repositories.evidence_search") is not None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_metadata_index(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(bootstrap.SCHEMA_SQL)
    runs = [
        ("visible-run", "Topic Alpha", "topic-alpha", "completed", "2026-07-01", "2026-07-03"),
        ("second-visible-run", "Topic Alpha", "topic-alpha", "completed", "2026-07-02", "2026-07-04"),
        ("hidden-run", "Topic Hidden", "topic-hidden", "completed", "2026-07-01", "2026-07-05"),
    ]
    connection.executemany(
        """
        INSERT INTO runs (
          run_id, topic, topic_slug, status, created_at, updated_at, root_run_id,
          lineage_mode, triage_items, reviewed_items, evidence_count, claim_count, report_section_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'standalone', 1, 1, 1, 1, 1)
        """,
        [(run_id, topic, slug, status, created, updated, run_id) for run_id, topic, slug, status, created, updated in runs],
    )
    connection.executemany(
        """
        INSERT INTO papers (
          run_id, paper_id, title, theme, score, status, published, entry_url, pdf_url,
          dedup_cluster_id, is_primary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "visible-run",
                "2601.00001v1",
                "Alpha XR Evidence",
                "spatial computing",
                91,
                "reviewed",
                "2026-01-01",
                "https://example.test/alpha-v1",
                "https://example.test/alpha-v1.pdf",
                "alpha",
                1,
            ),
            (
                "second-visible-run",
                "2601.00001v2",
                "Alpha XR Evidence Revised",
                "spatial computing",
                93,
                "reviewed",
                "2026-02-01",
                "https://example.test/alpha-v2",
                "https://example.test/alpha-v2.pdf",
                "alpha",
                1,
            ),
            (
                "visible-run",
                "2601.00002v1",
                "Literal 100%_Safe Study",
                "security",
                70,
                "unreviewed",
                "2026-03-01",
                None,
                None,
                None,
                1,
            ),
            (
                "hidden-run",
                "9999.00001v1",
                "Hidden Ownership Study",
                "private",
                99,
                "reviewed",
                "2026-01-01",
                None,
                None,
                None,
                1,
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO paper_reviews (
          topic_id, run_id, paper_id, title, source_provider, authors_json, published_at,
          abstract_text, text_excerpt, full_text_status, relevance_score, novelty_score,
          evidence_strength, method_summary, key_findings_json, limitations_json,
          why_it_matters, topic_fit_rationale, review_model, reviewer, reviewed_at,
          source_text_sha256, full_text_artifact_path, text_char_count, evidence_spans_json,
          review_source_type, full_text_reviewed_at
        ) VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, 0.9, 0.8, 0.9, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "topic-alpha",
                "visible-run",
                "2601.00001v1",
                "Authoritative Alpha Review",
                "arxiv",
                "2026-01-01",
                "Alpha abstract",
                "password=review-secret spatial anchor evidence",
                "reviewed",
                "Structured review method",
                json.dumps(["Spatial anchors reduce drift"]),
                json.dumps(["Small evaluation"]),
                "Useful for XR evidence",
                "Direct topic fit",
                "deterministic-local-full-text-v1",
                "local-reviewer",
                "2026-07-06T00:00:00+00:00",
                "a" * 64,
                "run:raw/alpha.txt",
                512,
                json.dumps(
                    [
                        {
                            "evidence_span_id": "alpha-span-1",
                            "text": "spatial anchor evidence",
                            "source_path": "run:raw/alpha.txt",
                        }
                    ]
                ),
                "full_text",
                "2026-07-06T00:00:00+00:00",
            ),
            (
                "topic-alpha",
                "provider-review-synthetic",
                "2601.00003v1",
                "Unowned Provider Review",
                "codex",
                "2026-01-02",
                "Provider abstract",
                "provider evidence",
                "reviewed",
                "Provider method",
                "[]",
                "[]",
                "Provider relevance",
                "Provider topic fit",
                "codex_sdk_python/output_class_live",
                "provider:codex",
                "2026-07-07T00:00:00+00:00",
                "b" * 64,
                "project:data/papers/text/provider.txt",
                256,
                json.dumps(
                    [
                        {
                            "evidence_span_id": "provider-span-1",
                            "text": "provider evidence",
                            "proof_artifact_ref": "project:output/proof.json",
                        }
                    ]
                ),
                "provider_output_class_proof",
                "2026-07-07T00:00:00+00:00",
            ),
            (
                "topic-hidden",
                "hidden-run",
                "9999.00001v1",
                "Hidden Review",
                "arxiv",
                "2026-01-01",
                "Hidden abstract",
                "hidden review evidence",
                "abstract_only",
                "Hidden method",
                "[]",
                "[]",
                "Hidden relevance",
                "Hidden topic fit",
                "deterministic-baseline-v1",
                "local-reviewer",
                "2026-07-05T00:00:00+00:00",
                "c" * 64,
                None,
                100,
                "[]",
                "abstract",
                None,
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO evidence (
          run_id, evidence_id, source_id, paper_id, title, source_type, quality_hint,
          confidence, artifact_path, citation_text, snippet
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "visible-run",
                "evidence-alpha",
                "2601.00001v1",
                "2601.00001v1",
                "Alpha latency evidence",
                "review_note",
                "strong",
                0.9,
                "run:reviews/alpha.json",
                "Alpha citation",
                "Latency stayed below the target.",
            ),
            (
                "hidden-run",
                "evidence-hidden",
                "9999.00001v1",
                "9999.00001v1",
                "Hidden evidence",
                "review_note",
                "strong",
                0.9,
                None,
                "Hidden citation",
                "Hidden snippet",
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO report_sections (run_id, section_id, title, claim_count, citation_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("visible-run", "findings", "Evidence Findings", 2, 2, "2026-07-03"),
            ("hidden-run", "private", "Hidden Findings", 1, 1, "2026-07-05"),
        ],
    )
    connection.commit()
    connection.close()


@pytest.fixture
def metadata_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    storage_root = tmp_path / "storage" / "runs"
    storage_root.mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    path = storage_root.parent / "metadata_index.sqlite3"
    _seed_metadata_index(path)
    return path


def _criteria(**overrides):
    values = {
        "paperPatterns": (),
        "visibleRunIds": None,
        "runIds": (),
        "topicIds": (),
        "topicNames": (),
        "reviewSources": (),
        "providerIds": (),
        "reviewLevels": (),
    }
    values.update(overrides)
    return evidence_search.RepositorySearchCriteria(**values)


def test_repository_reads_cross_corpus_candidates_and_batches_occurrences(metadata_db: Path) -> None:
    repository = evidence_search.EvidenceSearchRepository()

    bundle = repository.search(
        _criteria(
            paperPatterns=(f"%{evidence_search.escape_like('2601.00001')}%",),
            visibleRunIds=("visible-run", "second-visible-run"),
        )
    )

    assert [row.paperId for row in bundle.papers] == ["2601.00001v2", "2601.00001v1"]
    assert [row.paperId for row in bundle.reviews] == ["2601.00001v1"]
    assert [row.evidenceId for row in bundle.evidence] == ["evidence-alpha"]
    assert [row.runId for row in bundle.runs] == ["second-visible-run", "visible-run"]
    assert [row.sectionId for row in bundle.reportSections] == ["findings"]
    assert [(row.runId, row.paperId) for row in bundle.occurrences] == [
        ("second-visible-run", "2601.00001v2"),
        ("visible-run", "2601.00001v1"),
    ]
    assert repository.lastStatementCount <= 6


def test_repository_applies_filters_inside_read_only_queries(metadata_db: Path) -> None:
    repository = evidence_search.EvidenceSearchRepository()

    bundle = repository.search(
        _criteria(
            visibleRunIds=("visible-run", "second-visible-run"),
            runIds=("visible-run",),
            topicIds=("topic-alpha",),
            topicNames=("Topic Alpha",),
            reviewSources=("full_text",),
            providerIds=("arxiv",),
            reviewLevels=("full_text_reviewed",),
        )
    )

    assert {row.runId for row in bundle.papers} == {"visible-run"}
    assert {(row.topicId, row.sourceProvider, row.reviewSourceType) for row in bundle.reviews} == {
        ("topic-alpha", "arxiv", "full_text")
    }
    assert {row.runId for row in bundle.evidence} == {"visible-run"}
    assert {row.runId for row in bundle.reportSections} == {"visible-run"}
    assert all(row.runId != "hidden-run" for row in bundle.occurrences)


def test_repository_fails_closed_for_empty_visibility_and_unowned_provider_review(metadata_db: Path) -> None:
    repository = evidence_search.EvidenceSearchRepository()

    empty = repository.search(_criteria(visibleRunIds=()))
    visible = repository.search(_criteria(visibleRunIds=("visible-run",)))

    assert empty.papers == ()
    assert empty.reviews == ()
    assert empty.evidence == ()
    assert empty.runs == ()
    assert empty.reportSections == ()
    assert {row.paperId for row in visible.reviews} == {"2601.00001v1"}
    assert "2601.00003v1" not in {row.paperId for row in visible.reviews}


def test_repository_escapes_wildcards_and_never_interpolates_filter_values(metadata_db: Path) -> None:
    repository = evidence_search.EvidenceSearchRepository()
    injection_text = "%' OR 1=1 --"

    literal = repository.search(
        _criteria(
            paperPatterns=(f"%{evidence_search.escape_like('100%_safe')}%",),
            visibleRunIds=("visible-run",),
        )
    )
    injected = repository.search(
        _criteria(
            paperPatterns=(f"%{evidence_search.escape_like(injection_text)}%",),
            visibleRunIds=("visible-run",),
        )
    )

    assert [row.paperId for row in literal.papers] == ["2601.00002v1"]
    assert injected.papers == ()


def test_repository_is_deterministic_read_only_and_does_not_bootstrap(
    metadata_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_hash = _sha256(metadata_db)
    before_objects = sqlite3.connect(metadata_db).execute(
        "SELECT type, name FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    monkeypatch.setattr(metadata_index, "ensure_index_ready", lambda: pytest.fail("must not bootstrap"))
    repository = evidence_search.EvidenceSearchRepository()
    criteria = _criteria(visibleRunIds=("visible-run", "second-visible-run"))

    first = repository.search(criteria)
    first_count = repository.lastStatementCount
    second = repository.search(criteria)

    after_objects = sqlite3.connect(metadata_db).execute(
        "SELECT type, name FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert first == second
    assert first_count <= 6
    assert repository.lastStatementCount <= 6
    assert _sha256(metadata_db) == before_hash
    assert after_objects == before_objects
    assert not list(metadata_db.parent.glob("metadata_index.sqlite3-*"))


def test_repository_progress_budget_cancels_without_partial_results(metadata_db: Path) -> None:
    repository = evidence_search.EvidenceSearchRepository(timeoutSeconds=0.0, progressSteps=1)

    with pytest.raises(evidence_search.EvidenceSearchTooExpensive):
        repository.search(_criteria())

    assert repository.lastStatementCount <= 1


def test_repository_bounds_occurrence_rows_but_preserves_exact_counts(metadata_db: Path) -> None:
    connection = sqlite3.connect(metadata_db)
    for index in range(10):
        run_id = f"occurrence-run-{index}"
        connection.execute(
            """
            INSERT INTO runs (
              run_id, topic, topic_slug, status, created_at, updated_at, root_run_id,
              lineage_mode, triage_items, reviewed_items, evidence_count, claim_count, report_section_count
            ) VALUES (?, 'Topic Alpha', 'topic-alpha', 'completed', ?, ?, ?, 'standalone', 1, 0, 0, 0, 0)
            """,
            (run_id, f"2026-06-{index + 1:02d}", f"2026-06-{index + 1:02d}", run_id),
        )
        connection.execute(
            """
            INSERT INTO papers (run_id, paper_id, title, theme, score, status, published, is_primary)
            VALUES (?, '2601.00001v1', 'Alpha XR Evidence', 'spatial', 1, 'reviewed', '2026-01-01', 1)
            """,
            (run_id,),
        )
    connection.commit()
    connection.close()

    bundle = evidence_search.EvidenceSearchRepository().search(
        _criteria(
            paperPatterns=("%2601.00001v1%",),
            occurrenceLimit=3,
        )
    )

    alpha_occurrences = [row for row in bundle.occurrences if row.paperId == "2601.00001v1"]
    assert len(alpha_occurrences) == 3
    assert {row.occurrenceCount for row in alpha_occurrences} == {11}


def test_repository_handles_empty_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage_root = tmp_path / "empty" / "runs"
    storage_root.mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    path = storage_root.parent / "metadata_index.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(bootstrap.SCHEMA_SQL)
    connection.commit()
    connection.close()

    bundle = evidence_search.EvidenceSearchRepository().search(_criteria())

    assert bundle.papers == ()
    assert bundle.reviews == ()
    assert bundle.evidence == ()
    assert bundle.runs == ()
    assert bundle.reportSections == ()
    assert bundle.occurrences == ()


def test_existing_run_indexes_remain_available_to_query_plans(metadata_db: Path) -> None:
    connection = sqlite3.connect(f"file:{metadata_db.as_posix()}?mode=ro&immutable=1", uri=True)
    plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT run_id FROM runs WHERE run_id = ?",
        ("visible-run",),
    ).fetchall()
    connection.close()

    assert any("sqlite_autoindex_runs_1" in str(row) for row in plan)

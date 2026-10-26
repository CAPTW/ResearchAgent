from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import evidence_search as evidence_search_api
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.db import bootstrap
from app.domain import constants
from app.repositories import artifact_refs, evidence_search as evidence_search_repository
from app.repositories import runs as run_repository
from app.services import evidence_search as evidence_search_service, topic_profiles


def test_evidence_search_service_schema_and_router_modules_exist() -> None:
    assert importlib.util.find_spec("app.schemas.evidence_search") is not None
    assert importlib.util.find_spec("app.services.evidence_search") is not None
    assert importlib.util.find_spec("app.api.evidence_search") is not None


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_run(connection: sqlite3.Connection, run_id: str, topic: str, updated_at: str) -> None:
    connection.execute(
        """
        INSERT INTO runs (
          run_id, topic, topic_slug, status, created_at, updated_at, root_run_id,
          lineage_mode, triage_items, reviewed_items, evidence_count, claim_count,
          report_section_count, report_path
        ) VALUES (?, ?, ?, 'completed', ?, ?, ?, 'standalone', 1, 1, 1, 1, 1, 'run:reports/report.md')
        """,
        (run_id, topic, topic.lower().replace(" ", "-"), updated_at, updated_at, run_id),
    )


def _insert_review(
    connection: sqlite3.Connection,
    *,
    topic_id: str,
    run_id: str,
    paper_id: str,
    title: str,
    provider: str,
    reviewed_at: str,
    source_type: str,
    full_text_status: str,
    text_excerpt: str,
    artifact_ref: str | None,
    source_hash: str | None,
    spans: list[dict[str, str]],
) -> None:
    connection.execute(
        """
        INSERT INTO paper_reviews (
          topic_id, run_id, paper_id, title, source_provider, authors_json, published_at,
          abstract_text, text_excerpt, full_text_status, relevance_score, novelty_score,
          evidence_strength, method_summary, key_findings_json, limitations_json,
          why_it_matters, topic_fit_rationale, review_model, reviewer, reviewed_at,
          source_text_sha256, full_text_artifact_path, text_char_count, evidence_spans_json,
          review_source_type, full_text_reviewed_at
        ) VALUES (?, ?, ?, ?, ?, '[]', '2026-01-01', ?, ?, ?, 0.9, 0.8, 0.9, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 512, ?, ?, ?)
        """,
        (
            topic_id,
            run_id,
            paper_id,
            title,
            provider,
            f"Abstract for {title}",
            text_excerpt,
            full_text_status,
            f"Method for {title}",
            json.dumps([f"Finding for {title}"]),
            json.dumps(["Bounded fixture"]),
            f"Why {title} matters",
            f"Topic fit for {title}",
            "codex_sdk_python/output_class_live" if source_type == "provider_output_class_proof" else "deterministic-local-full-text-v1",
            f"provider:{provider}" if source_type == "provider_output_class_proof" else "local-reviewer",
            reviewed_at,
            source_hash,
            artifact_ref,
            json.dumps(spans),
            source_type,
            reviewed_at if full_text_status == "reviewed" else None,
        ),
    )


def _seed_api_metadata(path: Path, runs_root: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(bootstrap.SCHEMA_SQL)
    visible_run_ids = [f"visible-run-{index}" for index in range(6)]
    for index, run_id in enumerate(visible_run_ids):
        _insert_run(connection, run_id, "Topic Alpha", f"2026-07-{index + 1:02d}T00:00:00+00:00")
        raw_paper_id = "2601.00001v1" if index < 3 else "2601.00001v2"
        connection.execute(
            """
            INSERT INTO papers (
              run_id, paper_id, title, theme, score, status, published, entry_url, pdf_url,
              dedup_cluster_id, is_primary
            ) VALUES (?, ?, ?, 'spatial computing', ?, 'reviewed', '2026-01-01', ?, ?, 'alpha', 1)
            """,
            (
                run_id,
                raw_paper_id,
                f"Alpha XR Evidence occurrence {index}",
                90 + index,
                f"https://example.test/{raw_paper_id}/{index}",
                f"https://example.test/{raw_paper_id}/{index}.pdf",
            ),
        )
    _insert_run(connection, "standalone-run", "Topic Beta", "2026-07-07T00:00:00+00:00")
    _insert_run(connection, "hidden-run", "Topic Hidden", "2026-07-08T00:00:00+00:00")
    connection.execute(
        """
        INSERT INTO papers (
          run_id, paper_id, title, theme, score, status, published, dedup_cluster_id, is_primary
        ) VALUES ('hidden-run', '9999.00001v1', 'Hidden Ownership Study', 'private', 99, 'reviewed', '2026-01-01', 'hidden', 1)
        """
    )

    alpha_text = b"alpha artifact content"
    alpha_hash = hashlib.sha256(alpha_text).hexdigest()
    _insert_review(
        connection,
        topic_id="topic-alpha",
        run_id="visible-run-0",
        paper_id="2601.00001v1",
        title="Deterministic Alpha Review",
        provider="arxiv",
        reviewed_at="2026-07-06T00:00:00+00:00",
        source_type="full_text",
        full_text_status="reviewed",
        text_excerpt="<script>alert(1)</script> password=review-secret spatial anchor evidence",
        artifact_ref="run:raw/alpha.txt",
        source_hash=alpha_hash,
        spans=[
            {
                "evidence_span_id": "alpha-span",
                "text": "spatial anchor evidence",
                "source_path": "run:raw/alpha.txt",
            }
        ],
    )
    _insert_review(
        connection,
        topic_id="topic-alpha",
        run_id="visible-run-1",
        paper_id="2601.00001v1",
        title="Stale Alpha Review",
        provider="arxiv",
        reviewed_at="2026-07-06T12:00:00+00:00",
        source_type="full_text",
        full_text_status="reviewed",
        text_excerpt="stale artifact review",
        artifact_ref="run:raw/stale.txt",
        source_hash="0" * 64,
        spans=[],
    )
    _insert_review(
        connection,
        topic_id="topic-alpha",
        run_id="provider-review-1",
        paper_id="2601.00001v1",
        title="Provider Alpha Review",
        provider="codex",
        reviewed_at="2026-07-07T00:00:00+00:00",
        source_type="provider_output_class_proof",
        full_text_status="reviewed",
        text_excerpt="provider-backed spatial anchor evidence",
        artifact_ref="run:raw/alpha.txt",
        source_hash=alpha_hash,
        spans=[
            {
                "evidence_span_id": "provider-span",
                "text": "provider-backed spatial anchor evidence",
                "proof_artifact_ref": "run:proofs/missing-proof.json",
            },
            {
                "evidence_span_id": "malformed-span",
                "text": "malformed proof reference",
                "proof_artifact_ref": "run:../secret.txt",
            },
        ],
    )
    _insert_review(
        connection,
        topic_id="topic-alpha",
        run_id="provider-review-unowned",
        paper_id="2601.99999v1",
        title="Unowned Provider Review",
        provider="codex",
        reviewed_at="2026-07-08T00:00:00+00:00",
        source_type="provider_output_class_proof",
        full_text_status="reviewed",
        text_excerpt="unowned provider evidence",
        artifact_ref="run:raw/unowned.txt",
        source_hash="f" * 64,
        spans=[],
    )
    connection.executemany(
        """
        INSERT INTO evidence (
          run_id, evidence_id, source_id, paper_id, title, source_type, quality_hint,
          confidence, artifact_path, citation_text, snippet
        ) VALUES (?, ?, ?, ?, ?, 'review_note', 'strong', 0.9, ?, ?, ?)
        """,
        [
            (
                "visible-run-0",
                "evidence-alpha",
                "2601.00001v1",
                "2601.00001v1",
                "Alpha latency evidence",
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
            ("visible-run-0", "findings", "Evidence Findings", 2, 2, "2026-07-07T00:00:00+00:00"),
            ("hidden-run", "hidden", "Hidden Findings", 1, 1, "2026-07-08T00:00:00+00:00"),
        ],
    )
    connection.commit()
    connection.close()

    provider_report = {
        "provider_report_draft_id": "provider-report-draft-1",
        "provider_family_id": "codex",
        "provider_surface_id": "codex_sdk_python",
        "proof_artifact_ref": "run:proofs/provider-report.json",
        "proof_artifact_refs_used": ["run:proofs/provider-report.json"],
        "source_provider_review_ids": ["provider-review-1"],
        "source_paper_ids": ["2601.00001v1"],
        "evidence_span_refs_used": ["provider-span"],
        "report_source": "provider_output_class_proof",
        "output_class": "weekly_report",
        "internal_output_class": "report_draft",
        "production_ready": False,
        "external_beta_ready": False,
    }
    for run_id in [*visible_run_ids, "standalone-run", "hidden-run"]:
        run_dir = runs_root / run_id
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        (run_dir / "proofs").mkdir(parents=True, exist_ok=True)
        owner = "user-hidden" if run_id == "hidden-run" else "user-visible"
        payload: dict[str, Any] = {
            "run_id": run_id,
            "topic": "Topic Hidden" if run_id == "hidden-run" else ("Topic Beta" if run_id == "standalone-run" else "Topic Alpha"),
            "topic_profile_id": "topic-hidden" if run_id == "hidden-run" else ("topic-beta" if run_id == "standalone-run" else "topic-alpha"),
            "owner_user_id": owner,
        }
        if run_id == "visible-run-0":
            payload["provider_report"] = provider_report
            (run_dir / "raw" / "alpha.txt").write_bytes(alpha_text)
            (run_dir / "raw" / "stale.txt").write_text("stale content", encoding="utf-8")
            (run_dir / "proofs" / "provider-report.json").write_text("{}", encoding="utf-8")
        (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def evidence_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_factory):
    data_root = tmp_path / "evidence-api-data"
    runs_root = data_root / "runs"
    profiles_dir = data_root / "topic_profiles"
    profiles_dir.mkdir(parents=True)
    profiles = [
        {"profile_id": "topic-alpha", "title": "Topic Alpha", "topic": "Topic Alpha", "description": "Alpha topic"},
        {"profile_id": "topic-beta", "title": "Topic Beta", "topic": "Topic Beta", "description": "Beta topic"},
    ]
    (profiles_dir / "profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    app = app_factory(XRML_DATA_DIR=str(data_root))
    monkeypatch.setattr(constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profiles_dir / "profiles.json")
    db_path = data_root / "metadata_index.sqlite3"
    _seed_api_metadata(db_path, runs_root)
    return {"app": app, "data_root": data_root, "runs_root": runs_root, "db_path": db_path}


def _search(client: TestClient, q: str = "", **params: object):
    return client.get("/api/evidence/search", params={"q": q, **params})


def test_search_groups_canonical_papers_preserves_aliases_and_exact_facets(evidence_app) -> None:
    with TestClient(evidence_app["app"]) as client:
        response = _search(client, "2601.00001")

    assert response.status_code == 200, response.text
    body = response.json()
    paper = next(item for item in body["items"] if item["resultKind"] == "paper")
    assert paper["canonicalPaperId"] == "2601.00001"
    assert paper["paperIdAliases"] == ["2601.00001v1", "2601.00001v2"]
    assert paper["title"] == "Provider Alpha Review"
    assert paper["titleAuthority"] == "latest_review"
    assert paper["occurrenceCount"] == 6
    assert len(paper["occurrences"]) == 3
    assert paper["occurrencesTruncated"] is True
    assert set(paper["matchedCorpusKinds"]) == {"paper", "review", "evidence"}
    assert body["page"]["totalRelation"] == "exact"
    assert body["facets"]["corpusKinds"]["paper"] >= 1
    assert sum(body["facets"]["corpusKinds"].values()) > body["page"]["total"]


@pytest.mark.parametrize(
    ("query", "expected_kind", "expected_corpus"),
    [
        ("spatial anchor", "paper", "review"),
        ("latency target", "paper", "evidence"),
        ("standalone-run", "run", "run"),
        ("Evidence Findings", "report_section", "report_section"),
    ],
)
def test_search_covers_review_evidence_run_and_report_section(
    evidence_app,
    query: str,
    expected_kind: str,
    expected_corpus: str,
) -> None:
    with TestClient(evidence_app["app"]) as client:
        response = _search(client, query)

    assert response.status_code == 200, response.text
    item = next(item for item in response.json()["items"] if item["resultKind"] == expected_kind)
    assert expected_corpus in item["matchedCorpusKinds"]
    if expected_kind == "report_section":
        assert "report_provenance_not_indexed" in item["warnings"]


def test_search_filters_provider_review_level_topic_run_and_evidence(evidence_app) -> None:
    with TestClient(evidence_app["app"]) as client:
        response = _search(
            client,
            "Alpha",
            topic_ids="topic-alpha",
            run_ids="visible-run-0",
            review_sources="provider_output_class_proof",
            provider_ids="codex",
            review_levels="full_text_reviewed",
            has_evidence="true",
        )

    assert response.status_code == 200, response.text
    assert response.json()["page"]["total"] == 1
    item = response.json()["items"][0]
    assert item["resultKind"] == "paper"
    assert item["reviewSummary"]["source"] == "provider_output_class_proof"
    assert item["reviewSummary"]["providerId"] == "codex"
    assert item["inlineEvidenceSpanCount"] == 2
    assert item["runEvidenceCount"] == 1


def test_search_cursor_is_stable_and_bound_to_query_filters(evidence_app) -> None:
    with TestClient(evidence_app["app"]) as client:
        first = _search(client, "", limit=1)
        cursor = first.json()["page"]["nextCursor"]
        second = _search(client, "", limit=1, cursor=cursor)
        mismatch = _search(client, "Alpha", limit=1, cursor=cursor)
        malformed = _search(client, "", cursor="not-a-cursor")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["items"][0]["resultId"] != second.json()["items"][0]["resultId"]
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "cursor_query_mismatch"
    assert malformed.status_code == 422
    assert malformed.json()["detail"]["code"] == "malformed_cursor"


def test_search_zero_browse_validation_snippet_and_payload_guards(evidence_app) -> None:
    with TestClient(evidence_app["app"]) as client:
        zero = _search(client, "no-such-evidence-term")
        browse = _search(client, "", limit=100)
        review = _search(client, "password review-secret")
        too_many_tokens = _search(client, "one two three four five six seven eight nine")
        invalid_filter = _search(client, "Alpha", corpus_kinds="paper,unknown")
        oversized = _search(client, "x" * 201)

    assert zero.status_code == 200
    assert zero.json()["page"] == {
        "limit": 25,
        "returnedCount": 0,
        "total": 0,
        "totalRelation": "exact",
        "nextCursor": None,
    }
    assert zero.json()["facets"] == {
        "corpusKinds": {},
        "reviewLevels": {},
        "reviewSources": {},
        "providers": {},
        "statuses": {},
    }
    assert browse.status_code == 200
    assert len(browse.content) < 250_000
    assert too_many_tokens.status_code == 422
    assert invalid_filter.status_code == 422
    assert oversized.status_code == 422
    snippet = review.json()["items"][0]["safeSnippet"]
    assert len(snippet["text"]) <= 240
    assert "review-secret" not in snippet["text"]
    assert "<mark>" not in snippet["text"]
    assert snippet["sourceField"] == "textExcerpt"


def test_standard_max_page_payload_stays_below_250_kb(evidence_app) -> None:
    connection = sqlite3.connect(evidence_app["db_path"])
    for paper_index in range(100):
        paper_id = f"2701.{paper_index:05d}v1"
        for occurrence_index in range(5):
            run_id = f"payload-run-{paper_index:03d}-{occurrence_index}"
            _insert_run(connection, run_id, "Topic Alpha", f"2026-05-{occurrence_index + 1:02d}T00:00:00+00:00")
            connection.execute(
                """
                INSERT INTO papers (
                  run_id, paper_id, title, theme, score, status, published,
                  entry_url, pdf_url, dedup_cluster_id, is_primary
                ) VALUES (?, ?, ?, 'payload', 50, 'reviewed', '2026-01-01', ?, ?, ?, 1)
                """,
                (
                    run_id,
                    paper_id,
                    f"BulkPayload paper {paper_index}",
                    f"https://example.test/{paper_id}/{occurrence_index}",
                    f"https://example.test/{paper_id}/{occurrence_index}.pdf",
                    f"payload-{paper_index}",
                ),
            )
    connection.commit()
    connection.close()

    with TestClient(evidence_app["app"]) as client:
        response = _search(client, "BulkPayload", limit=100)

    assert response.status_code == 200
    assert response.json()["page"]["returnedCount"] == 100
    assert len(response.content) < 250_000


def test_detail_returns_truthful_graph_bounded_refs_and_no_fabricated_report_edges(
    evidence_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_reads = 0
    ref_validations = 0
    original_read = evidence_search_service.EvidenceSearchService._read_selected_run_record
    original_validate = artifact_refs.validate_artifact_reference

    def counted_read(self, run_id: str):
        nonlocal run_reads
        run_reads += 1
        return original_read(self, run_id)

    def counted_validate(*args, **kwargs):
        nonlocal ref_validations
        ref_validations += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(evidence_search_service.EvidenceSearchService, "_read_selected_run_record", counted_read)
    monkeypatch.setattr(artifact_refs, "validate_artifact_reference", counted_validate)

    with TestClient(evidence_app["app"]) as client:
        search = _search(client, "2601.00001")
        result_id = next(item["resultId"] for item in search.json()["items"] if item["resultKind"] == "paper")
        detail = client.get(
            f"/api/evidence/items/{result_id}",
            params={"occurrence_run_id": "visible-run-0"},
        )
        paper_ref_validations = ref_validations
        ref_validations = 0
        report_search = _search(client, "Evidence Findings")
        report_id = report_search.json()["items"][0]["resultId"]
        report_detail = client.get(f"/api/evidence/items/{report_id}")
        report_ref_validations = ref_validations

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["occurrenceCount"] == 6
    assert len(body["occurrences"]) == 6
    assert {edge["truthLevel"] for edge in body["provenanceEdges"]} >= {
        "exact_persisted",
        "exact_indexed_projection",
        "exact_portable_ref",
        "derived_deterministic",
    }
    assert body["providerReport"]["draftId"] == "provider-report-draft-1"
    assert "provider-review-1" in body["providerReport"]["sourceReviewIds"]
    assert {item["status"] for item in body["artifactVerifications"]} >= {
        "current",
        "stale",
        "missing_artifact",
        "orphaned_reference",
    }
    serialized = json.dumps(body)
    assert str(evidence_app["data_root"]) not in serialized
    assert "raw provider output" not in serialized.lower()
    assert run_reads <= 2  # one selected run for each of the two detail requests
    assert paper_ref_validations <= 5
    assert report_ref_validations <= 5
    report_edges = report_detail.json()["provenanceEdges"]
    assert not any(
        edge["edgeKind"] in {"report_section_references_paper", "report_section_references_evidence"}
        for edge in report_edges
    )
    assert "report_provenance_not_indexed" in report_detail.json()["warnings"]


def test_detail_caps_occurrences_at_100(evidence_app) -> None:
    connection = sqlite3.connect(evidence_app["db_path"])
    for index in range(100, 205):
        run_id = f"bulk-run-{index}"
        _insert_run(connection, run_id, "Topic Alpha", f"2026-06-{(index % 28) + 1:02d}T00:00:00+00:00")
        connection.execute(
            """
            INSERT INTO papers (run_id, paper_id, title, theme, score, status, published, is_primary)
            VALUES (?, '2601.00001v1', 'Bulk Alpha', 'bulk', 1, 'unreviewed', '2026-01-01', 1)
            """,
            (run_id,),
        )
    connection.commit()
    connection.close()

    with TestClient(evidence_app["app"]) as client:
        search = _search(client, "2601.00001")
        result_id = search.json()["items"][0]["resultId"]
        detail = client.get(f"/api/evidence/items/{result_id}")

    assert detail.status_code == 200
    assert detail.json()["occurrenceCount"] == 111
    assert len(detail.json()["occurrences"]) == 100
    assert detail.json()["occurrencesTruncated"] is True


def test_malformed_provider_report_remains_visible_with_degraded_warning(evidence_app) -> None:
    run_json = evidence_app["runs_root"] / "visible-run-0" / "run.json"
    payload = json.loads(run_json.read_text(encoding="utf-8"))
    payload["provider_report"] = {"provider_report_draft_id": []}
    run_json.write_text(json.dumps(payload), encoding="utf-8")

    with TestClient(evidence_app["app"]) as client:
        search = _search(client, "Evidence Findings")
        result_id = search.json()["items"][0]["resultId"]
        detail = client.get(f"/api/evidence/items/{result_id}")

    assert detail.status_code == 200
    assert "malformed_provider_report" in detail.json()["warnings"]
    assert "degraded" in detail.json()["summary"]["statuses"]


def test_auth_enabled_search_filters_hidden_runs_and_unowned_provider_rows(evidence_app, app_factory) -> None:
    app = app_factory(
        XRML_DATA_DIR=str(evidence_app["data_root"]),
        AUTH_MODE="oidc",
        SESSION_SECRET="synthetic-test-session-secret-0123456789abcdef",
        APP_BASE_URL="http://127.0.0.1:8000",
    )
    visible_user = ProductUser(
        user_id="user-visible",
        display_name="Visible User",
        email="visible@example.test",
        auth_source="oidc",
    )
    app.dependency_overrides[require_product_user] = lambda: visible_user
    with TestClient(app) as client:
        hidden = _search(client, "Hidden Ownership")
        unowned = _search(client, "Unowned Provider")
        visible = _search(client, "2601.00001")
        hidden_result_id = evidence_search_service.encode_result_id("paper", "9999.00001")
        hidden_detail = client.get(f"/api/evidence/items/{hidden_result_id}")

    assert hidden.status_code == 200 and hidden.json()["page"]["total"] == 0
    assert unowned.status_code == 200 and unowned.json()["page"]["total"] == 0
    assert visible.status_code == 200 and visible.json()["page"]["total"] == 1
    assert hidden_detail.status_code == 404


def test_routes_are_get_only_typed_and_existing_routes_remain(evidence_app) -> None:
    app = evidence_app["app"]
    openapi = app.openapi()
    route_methods = {
        route.path: set(route.methods or set())
        for route in app.routes
        if route.path.startswith("/api/evidence")
    }

    assert route_methods["/api/evidence/search"] == {"GET"}
    assert route_methods["/api/evidence/items/{result_id}"] == {"GET"}
    assert "EvidenceSearchResponse" in openapi["components"]["schemas"]
    assert "EvidenceDetailResponse" in openapi["components"]["schemas"]
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/runs").status_code == 200
        assert client.post("/api/evidence/search").status_code == 405
        assert client.put("/api/evidence/search").status_code == 405
        assert client.patch("/api/evidence/search").status_code == 405
        assert client.delete("/api/evidence/search").status_code == 405


def test_search_budget_error_maps_to_retryable_503(evidence_app) -> None:
    class TooExpensiveService:
        def search(self, **kwargs):
            raise evidence_search_repository.EvidenceSearchTooExpensive("budget")

    evidence_app["app"].dependency_overrides[evidence_search_api.get_evidence_search_service] = TooExpensiveService
    with TestClient(evidence_app["app"]) as client:
        response = _search(client, "Alpha")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "search_too_expensive",
        "message": "The evidence search exceeded its local read budget.",
        "retryable": True,
    }


def test_endpoint_execution_is_deterministic_and_leaves_synthetic_files_unchanged(evidence_app) -> None:
    db_before = _file_hash(evidence_app["db_path"])
    run_json = evidence_app["runs_root"] / "visible-run-0" / "run.json"
    run_before = _file_hash(run_json)
    with TestClient(evidence_app["app"]) as client:
        first = _search(client, "spatial anchor")
        second = _search(client, "spatial anchor")

    assert first.status_code == 200
    assert first.json() == second.json()
    assert _file_hash(evidence_app["db_path"]) == db_before
    assert _file_hash(run_json) == run_before
    assert not list(evidence_app["data_root"].glob("metadata_index.sqlite3-*"))

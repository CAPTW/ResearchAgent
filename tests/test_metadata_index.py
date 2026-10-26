from __future__ import annotations


from app.db import bootstrap
from app.repositories import metadata_index
from app.repositories.files import write_jsonl
from app.schemas.runs import RunCreateRequest
from app.services import exports, research_plan
from app.services.runs import create_run_from_request


def test_metadata_index_backfill_and_query(isolated_run_repo, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])

    result = metadata_index.ensure_index_ready()
    overview = metadata_index.get_overview()
    run_search = metadata_index.search_runs(q="validation")
    paper_search = metadata_index.search_papers(q="XR")

    assert result["indexed_runs"] >= 2
    assert overview["totals"]["runs"] >= 2
    assert overview["totals"]["papers"] > 0
    assert overview["totals"]["evidence"] > 0
    assert run_search["items"]
    assert paper_search["items"]
    assert bootstrap.metadata_index_path().exists()


def test_incremental_index_refreshes_new_runs_without_full_rebuild(isolated_run_repo, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    metadata_index.ensure_index_ready()

    created = create_run_from_request(
        RunCreateRequest(
            topic="Incremental Index Topic",
            queries=[],
            categories=[],
            max_results=1,
            top_papers=1,
            weekly_top=1,
            skip_scout=True,
            skip_summary=True,
            skip_synthesis=True,
        ),
        source="test",
        write_legacy=False,
    )
    search = metadata_index.search_runs(q="Incremental Index Topic")
    state = metadata_index.get_index_state()

    assert any(item["run_id"] == created["run_id"] for item in search["items"])
    assert state["last_incremental_reason"] == "run_create"
    assert state["last_indexed_run_id"] == created["run_id"]


def test_incremental_index_tracks_export_writes(isolated_run_repo, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(research_plan, "get_active_provider", lambda: None)
    metadata_index.ensure_index_ready()

    research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    exports.export_report_markdown(isolated_run_repo["run_id"])
    state = metadata_index.get_index_state()

    assert state["last_incremental_reason"] == "export_write"


def test_metadata_index_refresh_is_idempotent_for_duplicate_paper_ids(isolated_run_repo, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    run_id = isolated_run_repo["run_id"]
    triage_path = isolated_run_repo["primary_run_dir"] / "triage" / "triage.jsonl"
    duplicate_items = [
        {
            "paper_id": "duplicate-paper-id",
            "title": "중복 논문 첫 번째 버전",
            "theme": "LLM 평가",
            "score": 91,
            "published": "2026-05-01",
            "entry_url": "https://example.test/papers/duplicate",
        },
        {
            "paper_id": "duplicate-paper-id",
            "title": "중복 논문 최신 버전",
            "theme": "LLM 평가",
            "score": 96,
            "published": "2026-05-02",
            "entry_url": "https://example.test/papers/duplicate-v2",
        },
    ]
    write_jsonl(triage_path, duplicate_items)

    result = metadata_index.refresh_run(run_id, reason="duplicate_paper_regression")
    search = metadata_index.search_papers(q="duplicate-paper-id")

    assert result["papers_indexed"] == 1
    assert len(search["items"]) == 1
    assert search["items"][0]["title"] == "중복 논문 최신 버전"

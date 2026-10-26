from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import mcp as mcp_api
from app.repositories.runs import build_run_paths
from app.services import reporting, topic_dashboard


def _hash_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _metadata_sidecars(metadata_index_path: Path) -> list[Path]:
    return [
        metadata_index_path.with_name(metadata_index_path.name + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    ]


def _assert_metadata_unchanged(metadata_index_path: Path, before_hash: str | None) -> None:
    assert _hash_file(metadata_index_path) == before_hash
    assert not any(path.exists() for path in _metadata_sidecars(metadata_index_path))


def test_report_preview_get_is_read_only_when_preview_missing(
    client: TestClient,
    isolated_run_repo: dict,
) -> None:
    run_id = isolated_run_repo["run_id"]
    paths = build_run_paths(run_id)
    preview_path = paths.reports_dir / reporting.PREVIEW_FILENAME
    preview_path.unlink(missing_ok=True)
    before_tree = _tree_snapshot(paths.run_dir)
    before_db_hash = _hash_file(isolated_run_repo["metadata_index_path"])

    response = client.get(f"/runs/{run_id}/preview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["content"].strip()
    assert payload["path"] == "run:reports/preview.md"
    assert not preview_path.exists()
    assert _tree_snapshot(paths.run_dir) == before_tree
    _assert_metadata_unchanged(isolated_run_repo["metadata_index_path"], before_db_hash)


def test_report_outline_preview_and_citations_do_not_backfill_missing_artifacts(
    client: TestClient,
    isolated_run_repo: dict,
) -> None:
    run_id = isolated_run_repo["run_id"]
    paths = build_run_paths(run_id)
    outline_path = paths.reports_dir / reporting.OUTLINE_FILENAME
    preview_path = paths.reports_dir / reporting.PREVIEW_FILENAME
    outline_path.unlink(missing_ok=True)
    preview_path.unlink(missing_ok=True)
    before_tree = _tree_snapshot(paths.run_dir)
    before_db_hash = _hash_file(isolated_run_repo["metadata_index_path"])

    outline_response = client.get(f"/runs/{run_id}/outline")
    preview_response = client.get(f"/runs/{run_id}/preview")
    citation_response = client.get(f"/runs/{run_id}/citations")

    assert outline_response.status_code == 200
    assert preview_response.status_code == 200
    assert citation_response.status_code == 200
    assert outline_response.json()["path"] == "run:reports/outline.json"
    assert preview_response.json()["path"] == "run:reports/preview.md"
    assert citation_response.json()["source_outline_path"] == "run:reports/outline.json"
    assert citation_response.json()["source_preview_path"] == "run:reports/preview.md"
    assert not outline_path.exists()
    assert not preview_path.exists()
    assert _tree_snapshot(paths.run_dir) == before_tree
    _assert_metadata_unchanged(isolated_run_repo["metadata_index_path"], before_db_hash)


def test_write_preview_artifact_is_explicit_and_isolated(
    isolated_run_repo: dict,
) -> None:
    run_id = isolated_run_repo["run_id"]
    paths = build_run_paths(run_id)
    preview_path = paths.reports_dir / reporting.PREVIEW_FILENAME
    preview_path.unlink(missing_ok=True)
    outline = reporting.load_outline(run_id)
    before_tree = _tree_snapshot(paths.run_dir)

    payload = reporting.write_preview_artifact(run_id, outline=outline)

    assert payload["path"] == str(preview_path)
    assert preview_path.is_file()
    after_tree = _tree_snapshot(paths.run_dir)
    changed = {
        name
        for name in set(before_tree) | set(after_tree)
        if before_tree.get(name) != after_tree.get(name)
    }
    assert changed <= {"run.json", "reports/preview.md"}


def test_report_preview_service_default_does_not_write(
    isolated_run_repo: dict,
) -> None:
    run_id = isolated_run_repo["run_id"]
    paths = build_run_paths(run_id)
    outline_path = paths.reports_dir / reporting.OUTLINE_FILENAME
    preview_path = paths.reports_dir / reporting.PREVIEW_FILENAME
    outline_path.unlink(missing_ok=True)
    preview_path.unlink(missing_ok=True)
    before_tree = _tree_snapshot(paths.run_dir)
    before_db_hash = _hash_file(isolated_run_repo["metadata_index_path"])

    payload = reporting.build_preview(run_id)
    citations = reporting.build_citation_trace(run_id)

    assert payload["content"].strip()
    assert citations["run_id"] == run_id
    assert not outline_path.exists()
    assert not preview_path.exists()
    assert _tree_snapshot(paths.run_dir) == before_tree
    _assert_metadata_unchanged(isolated_run_repo["metadata_index_path"], before_db_hash)


def test_mcp_report_preview_uses_read_only_preview_render(
    isolated_run_repo: dict,
) -> None:
    run_id = isolated_run_repo["run_id"]
    paths = build_run_paths(run_id)
    outline_path = paths.reports_dir / reporting.OUTLINE_FILENAME
    preview_path = paths.reports_dir / reporting.PREVIEW_FILENAME
    outline_path.unlink(missing_ok=True)
    preview_path.unlink(missing_ok=True)
    before_tree = _tree_snapshot(paths.run_dir)
    before_db_hash = _hash_file(isolated_run_repo["metadata_index_path"])

    payload = mcp_api._get_report_preview({"run_id": run_id})

    assert payload["run_id"] == run_id
    assert payload["preview_content"].strip()
    assert not outline_path.exists()
    assert not preview_path.exists()
    assert _tree_snapshot(paths.run_dir) == before_tree
    _assert_metadata_unchanged(isolated_run_repo["metadata_index_path"], before_db_hash)


def test_dashboard_research_operations_use_read_only_review_and_draft_reads(monkeypatch) -> None:
    calls: list[tuple[str, bool | None]] = []

    def fake_list_reviews(**kwargs):
        calls.append(("reviews", kwargs.get("read_only")))
        return []

    def fake_list_drafts(**kwargs):
        calls.append(("drafts", kwargs.get("read_only")))
        return []

    monkeypatch.setattr(topic_dashboard.paper_reviews, "list_reviews", fake_list_reviews)
    monkeypatch.setattr(topic_dashboard.ai_review_drafts, "list_drafts", fake_list_drafts)
    monkeypatch.setattr(
        topic_dashboard.recommendations,
        "build_recommendations_for_topic",
        lambda *_args, **_kwargs: {"summary": {}, "coverage_warning": None},
    )

    topic_dashboard._build_research_operations(
        "large-language-models",
        {"fullTextReviewedUniqueCount": 0, "topicRelevantUniqueCount": 0},
        {"profile_id": "large-language-models", "title": "Large Language Models", "queries": []},
        review_scope_rows=None,
    )

    assert calls
    assert all(read_only is True for _, read_only in calls)

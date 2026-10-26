from __future__ import annotations

import hashlib
from pathlib import Path

from app.db import bootstrap
from app.repositories import ai_review_drafts, metadata_index, paper_reviews
from app.services import topic_analytics, topic_dashboard


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


def _block_write_metadata_connections(monkeypatch) -> None:
    original_connect = bootstrap.connect_metadata_index

    def read_only_connect_metadata_index(*, read_only: bool = False):
        if not read_only:
            raise AssertionError("dashboard GET must not open metadata index in write mode")
        return original_connect(read_only=True)

    monkeypatch.setattr(bootstrap, "connect_metadata_index", read_only_connect_metadata_index)
    for module in (ai_review_drafts, metadata_index, paper_reviews, topic_dashboard):
        if hasattr(module, "connect_metadata_index"):
            monkeypatch.setattr(module, "connect_metadata_index", read_only_connect_metadata_index)


def test_api_topic_dashboard_get_reads_persisted_analytics_without_refresh_or_writes(
    client,
    isolated_run_repo: dict,
    monkeypatch,
) -> None:
    data_root = isolated_run_repo["data_root"]
    metadata_index_path = isolated_run_repo["metadata_index_path"]
    analytics_path = data_root / "topic_analytics" / "large-language-models.json"
    assert analytics_path.is_file()
    before_tree = _tree_snapshot(data_root)
    before_db_hash = _hash_file(metadata_index_path)
    before_topic_hash = _hash_file(analytics_path)

    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("dashboard GET must not refresh or persist topic analytics")

    def fail_index_backfill(*_args, **_kwargs):
        raise AssertionError("dashboard GET must not backfill the metadata index")

    monkeypatch.setattr(topic_analytics, "refresh_topic_analytics", fail_refresh)
    monkeypatch.setattr(topic_dashboard.metadata_index, "ensure_index_ready", fail_index_backfill)
    _block_write_metadata_connections(monkeypatch)

    response = client.get("/api/topics/large-language-models/dashboard")
    payload = response.json()

    assert response.status_code == 200, response.text
    assert payload["reviewCoverage"]["totalPapers"] == 524
    assert payload["reviewCoverage"]["reviewedCount"] == 165
    assert payload["reviewCoverage"]["reviewedUniqueCount"] == 165
    assert payload["reviewCoverage"]["fullTextReviewedCount"] == 164
    assert payload["reviewCoverage"]["fullTextReviewedUniqueCount"] == 164
    assert payload["reviewCoverage"]["coveragePercentage"] == 31.5
    assert _hash_file(analytics_path) == before_topic_hash
    assert _tree_snapshot(data_root) == before_tree
    _assert_metadata_unchanged(metadata_index_path, before_db_hash)


def test_api_topic_dashboard_missing_analytics_uses_read_only_runtime_snapshot(
    client,
    isolated_run_repo: dict,
    monkeypatch,
) -> None:
    data_root = isolated_run_repo["data_root"]
    metadata_index_path = isolated_run_repo["metadata_index_path"]
    analytics_path = data_root / "topic_analytics" / "large-language-models.json"
    analytics_path.unlink(missing_ok=True)
    before_tree = _tree_snapshot(data_root)
    before_db_hash = _hash_file(metadata_index_path)

    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("dashboard GET must not refresh or persist topic analytics")

    monkeypatch.setattr(topic_analytics, "refresh_topic_analytics", fail_refresh)
    _block_write_metadata_connections(monkeypatch)

    response = client.get("/api/topics/large-language-models/dashboard")
    payload = response.json()

    assert response.status_code == 200, response.text
    assert payload["reviewCoverage"]["totalPapers"] >= 0
    assert any("read-only runtime snapshot" in warning for warning in payload["warnings"])
    assert not analytics_path.exists()
    assert _tree_snapshot(data_root) == before_tree
    _assert_metadata_unchanged(metadata_index_path, before_db_hash)

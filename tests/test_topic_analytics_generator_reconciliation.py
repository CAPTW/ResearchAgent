from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.db import bootstrap
from app.repositories import ai_review_drafts, metadata_index, paper_reviews
from app.services import topic_analytics, topic_dashboard, topic_profiles, topic_schedules


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _hash_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _metadata_sidecars(metadata_index_path: Path) -> list[Path]:
    return [
        metadata_index_path.with_name(metadata_index_path.name + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    ]


def _patch_metadata_connections_read_only(monkeypatch) -> None:
    original_connect = bootstrap.connect_metadata_index

    def read_only_connect_metadata_index(*, read_only: bool = False):
        if not read_only:
            raise AssertionError("L3D read-only generator/dashboard fallback must not open metadata in write mode")
        return original_connect(read_only=True)

    monkeypatch.setattr(bootstrap, "connect_metadata_index", read_only_connect_metadata_index)
    for module in (ai_review_drafts, metadata_index, paper_reviews, topic_dashboard):
        if hasattr(module, "connect_metadata_index"):
            monkeypatch.setattr(module, "connect_metadata_index", read_only_connect_metadata_index)


def _patch_read_only_runtime_paths(tmp_path: Path, monkeypatch) -> Path:
    data_root = tmp_path / "data"
    profile_dir = data_root / "topic_profiles"
    schedule_dir = data_root / "topic_schedules"
    analytics_dir = data_root / "topic_analytics"
    # Generated rather than copied from `PROJECT_ROOT/storage`: that directory is
    # excluded from the published source archive, so copying it made this test
    # pass only inside a developer checkout.
    from tests.conftest import _write_portable_topic_profiles_fixture

    _write_portable_topic_profiles_fixture(data_root)
    schedule_dir.mkdir(parents=True, exist_ok=True)
    analytics_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    _seed_read_only_reconciliation_inputs(monkeypatch)
    return analytics_dir


def _seed_read_only_reconciliation_inputs(monkeypatch) -> None:
    run = {
        "run_id": "synthetic-read-only-reconciliation",
        "topic": "Large Language Models",
        "status": "completed",
        "created_at": "2026-04-04T03:00:20+00:00",
        "updated_at": "2026-04-04T03:00:20+00:00",
        "finished_at": "2026-04-04T03:00:20+00:00",
        "steps": [{"name": "summary", "status": "completed"}],
    }
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index in range(524):
        paper_id = f"synthetic-llm-reconciliation-{index:03d}"
        item = {
            "paper_id": paper_id,
            "title": f"Synthetic Large Language Model Reconciliation Paper {index:03d}",
            "abstract": "Synthetic large language model fixture for read-only reconciliation.",
            "published": "2026-04-01",
            "score": 80 if index < 164 else 45,
            "theme": "Large Language Models",
            "triage": {"hits_primary": ["large language models"], "hits_secondary": []},
        }
        items.append((run, item))
        if index >= 165:
            continue
        full_text_status = "abstract_only" if index == 0 else "reviewed"
        paper_reviews.upsert_review(
            {
                "topic_id": "large-language-models",
                "run_id": run["run_id"],
                "paper_id": paper_id,
                "title": item["title"],
                "source_provider": "arxiv",
                "abstract_text": item["abstract"],
                "text_excerpt": "Synthetic read-only full text evidence.",
                "full_text_status": full_text_status,
                "relevance_score": 80,
                "novelty_score": 50,
                "evidence_strength": 70,
                "method_summary": "Synthetic read-only reconciliation fixture.",
                "key_findings": ["Read-only analytics can compute coverage from temp metadata."],
                "limitations": ["Synthetic fixture only."],
                "why_it_matters": "Keeps clean worktrees independent of ignored runtime data.",
                "topic_fit_rationale": "Large language model reconciliation fixture.",
                "review_model": "deterministic-baseline-v1",
                "reviewer": "pytest-synthetic-fixture",
                "reviewed_at": "2026-04-04T03:00:20+00:00",
                "source_text_sha256": hashlib.sha256(paper_id.encode("utf-8")).hexdigest(),
                "review_source_type": "full_text" if full_text_status == "reviewed" else "abstract",
                "full_text_reviewed_at": "2026-04-04T03:00:20+00:00" if full_text_status == "reviewed" else None,
                "evidence_spans": (
                    [
                        {
                            "span_id": f"synthetic-span-{index:03d}",
                            "text": "Synthetic read-only full text evidence.",
                            "source_path": f"run:raw/{paper_id}.txt",
                        }
                    ]
                    if full_text_status == "reviewed"
                    else []
                ),
            }
        )

    def topic_run_records(topic: str, *, profile_id: str | None = None) -> list[dict[str, Any]]:
        return [run] if topic == "Large Language Models" else []

    def topic_items(topic: str, *, max_runs: int = 10, profile_id: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return list(items) if topic == "Large Language Models" else []

    monkeypatch.setattr(topic_analytics, "_topic_run_records", topic_run_records)
    monkeypatch.setattr(topic_analytics, "_topic_items", topic_items)


def test_read_only_generator_matches_h008d115_truth_without_persisting(tmp_path, monkeypatch) -> None:
    _patch_read_only_runtime_paths(tmp_path, monkeypatch)
    _patch_metadata_connections_read_only(monkeypatch)
    metadata_index_path = PROJECT_ROOT / "storage" / "metadata_index.sqlite3"
    analytics_path = PROJECT_ROOT / "storage" / "topic_analytics" / "large-language-models.json"
    before_db_hash = _hash_file(metadata_index_path)
    before_topic_hash = _hash_file(analytics_path)

    def fail_save_snapshot(*_args, **_kwargs):
        raise AssertionError("read-only topic analytics generation must not persist snapshots")

    monkeypatch.setattr(topic_analytics, "_save_snapshot", fail_save_snapshot)

    snapshot = topic_analytics.build_topic_analytics_snapshot("large-language-models", read_only=True)
    coverage = snapshot["reviewCoverage"]

    assert coverage["totalPapers"] == 524
    assert coverage["reviewedCount"] == 165
    assert coverage["reviewedUniqueCount"] == 165
    assert coverage["fullTextReviewedCount"] == 164
    assert coverage["fullTextReviewedUniqueCount"] == 164
    assert coverage["coveragePercentage"] == 31.5
    assert _hash_file(metadata_index_path) == before_db_hash
    assert _hash_file(analytics_path) == before_topic_hash
    assert not any(path.exists() for path in _metadata_sidecars(metadata_index_path))


def test_dashboard_missing_artifact_fallback_matches_current_truth_without_persisting(tmp_path, monkeypatch) -> None:
    analytics_dir = _patch_read_only_runtime_paths(tmp_path, monkeypatch)
    _patch_metadata_connections_read_only(monkeypatch)
    metadata_index_path = PROJECT_ROOT / "storage" / "metadata_index.sqlite3"
    analytics_path = PROJECT_ROOT / "storage" / "topic_analytics" / "large-language-models.json"
    before_db_hash = _hash_file(metadata_index_path)
    before_topic_hash = _hash_file(analytics_path)

    def fail_persist(*_args, **_kwargs):
        raise AssertionError("dashboard missing-artifact fallback must not refresh or persist topic analytics")

    monkeypatch.setattr(topic_analytics, "refresh_topic_analytics", fail_persist)
    monkeypatch.setattr(topic_analytics, "_save_snapshot", fail_persist)

    payload = topic_dashboard.build_api_topic_dashboard("large-language-models")
    coverage = payload["reviewCoverage"]

    assert coverage["totalPapers"] == 524
    assert coverage["reviewedCount"] == 165
    assert coverage["reviewedUniqueCount"] == 165
    assert coverage["fullTextReviewedCount"] == 164
    assert coverage["fullTextReviewedUniqueCount"] == 164
    assert coverage["coveragePercentage"] == 31.5
    assert any("read-only runtime snapshot" in warning for warning in payload["warnings"])
    assert list(analytics_dir.iterdir()) == []
    assert _hash_file(metadata_index_path) == before_db_hash
    assert _hash_file(analytics_path) == before_topic_hash
    assert not any(path.exists() for path in _metadata_sidecars(metadata_index_path))

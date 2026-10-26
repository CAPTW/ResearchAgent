from __future__ import annotations

from pathlib import Path
from typing import Any

from app.repositories import runs as run_repository
from app.repositories.files import write_json
from app.services import report_hub
from app.services import runs as run_service
from app.services import topic_analytics

REPORT_MARKDOWN = """# 주간 연구 보고서

## Executive Summary

이번 주 검토 범위와 provenance를 요약한다.

## Evidence Table

| paper_id | full_text_status | source_text_sha256 |
|---|---|---|
| p-1 | abstract_only | 0000 |

## 한계와 리스크

- 초록만 검토(abstract_only)한 항목이 있어 본문 근거는 제한적이다.
"""


def _run_record(run_id: str, *, report_path: str | None) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    if report_path is not None:
        artifacts["report_markdown"] = report_path
    return {
        "run_id": run_id,
        "topic": "Large Language Models",
        "status": "running",
        "source": "api_topic_action:report",
        "created_at": "2026-05-07T01:00:00Z",
        "updated_at": "2026-05-07T02:00:00Z",
        "started_at": "2026-05-07T01:00:00Z",
        "finished_at": "2026-05-07T02:00:00Z",
        "request": {"kind": "research_report", "skip_synthesis": False},
        "schedule_context": {},
        "artifacts": artifacts,
        "counts": {},
        "steps": [],
        "warnings": [],
        "error": None,
        "write_legacy": False,
    }


def _patch_finalize_side_effects(monkeypatch, runs_root: Path) -> None:
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(run_service.reporting, "ensure_compare_ready_metadata", lambda _run_id: None)
    monkeypatch.setattr(run_service.report_versions, "snapshot_completed_report_if_available", lambda _run_id: None)
    monkeypatch.setattr(run_service.metadata_index, "refresh_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(topic_analytics, "refresh_topic_analytics_for_run", lambda _run: [])
    monkeypatch.setattr(run_service.topic_notifications, "emit_notifications_for_run", lambda _run: None)
    # Keep report-quality evaluation deterministic and storage-free: no paper reviews required.
    monkeypatch.setattr(run_service.reporting.paper_reviews, "list_reviews", lambda **_kwargs: [])


def test_finalize_completed_report_run_persists_report_quality(monkeypatch, tmp_path: Path) -> None:
    runs_root = tmp_path / "storage" / "runs"
    run_id = "20260507-report-with-quality"
    reports_dir = runs_root / run_id / "reports"
    reports_dir.mkdir(parents=True)
    report_path = reports_dir / "weekly-report.md"
    report_path.write_text(REPORT_MARKDOWN, encoding="utf-8")

    run = _run_record(run_id, report_path=str(report_path))
    write_json(runs_root / run_id / "run.json", run)
    _patch_finalize_side_effects(monkeypatch, runs_root)

    finalized = run_service._finalize_run(run_id, status="completed")

    # The completed report run keeps its status and now carries a deterministic quality summary.
    assert finalized["status"] == "completed"
    quality = finalized["report_quality"]
    assert quality["evaluated_surface"] == "run_finalize"
    assert quality["evaluation_status"] == "evaluated"
    assert isinstance(quality["report_quality_classification"], str)

    # The summary is persisted to run.json (durable, not ephemeral per-preview).
    persisted = run_repository.load_run_record(run_id)
    assert persisted["report_quality"]["evaluated_surface"] == "run_finalize"
    assert persisted["report_quality"]["report_quality_classification"] == quality["report_quality_classification"]


def test_report_hub_surfaces_persisted_report_quality(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(report_hub.topic_profiles, "list_topic_profiles", lambda: {"items": []})
    report_path = tmp_path / "weekly-report.md"
    report_path.write_text(REPORT_MARKDOWN, encoding="utf-8")

    run = _run_record("hub-report-quality", report_path=str(report_path))
    run["status"] = "completed"
    run["report_quality"] = {
        "report_quality_classification": "needs_revision",
        "overall_status": "warn",
        "overall_score": 60,
        "max_score": 100,
        "threshold": 72,
        "overclaim_detected": False,
        "evaluation_status": "evaluated",
        "evaluated_surface": "run_finalize",
        "checked_at": "2026-05-07T02:00:00Z",
    }

    payload = report_hub.build_api_reports([run])
    item = next(entry for entry in payload["items"] if entry["runId"] == "hub-report-quality")

    assert item["reportQuality"] is not None
    assert item["reportQuality"]["classification"] == "needs_revision"
    assert item["reportQuality"]["evaluatedSurface"] == "run_finalize"
    assert item["reportQuality"]["overallScore"] == 60


def test_report_hub_report_quality_absent_is_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(report_hub.topic_profiles, "list_topic_profiles", lambda: {"items": []})
    report_path = tmp_path / "weekly-report.md"
    report_path.write_text(REPORT_MARKDOWN, encoding="utf-8")

    run = _run_record("hub-no-quality", report_path=str(report_path))
    run["status"] = "completed"

    payload = report_hub.build_api_reports([run])
    item = next(entry for entry in payload["items"] if entry["runId"] == "hub-no-quality")

    assert item["reportQuality"] is None

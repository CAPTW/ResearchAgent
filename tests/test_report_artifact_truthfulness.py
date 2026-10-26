from __future__ import annotations

from pathlib import Path
from typing import Any

from app.repositories import runs as run_repository
from app.repositories.files import write_json
from app.services import report_hub
from app.services import runs as run_service
from app.services import topic_analytics


def _run_record(
    run_id: str,
    *,
    status: str = "completed",
    source: str = "api_topic_action:analyze",
    request: dict[str, Any] | None = None,
    schedule_context: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "topic": "Large Language Models",
        "status": status,
        "source": source,
        "created_at": "2026-05-07T01:00:00Z",
        "updated_at": "2026-05-07T02:00:00Z",
        "started_at": "2026-05-07T01:00:00Z",
        "finished_at": "2026-05-07T02:00:00Z",
        "request": request if request is not None else {"skip_synthesis": False},
        "schedule_context": schedule_context or {},
        "artifacts": artifacts or {},
        "counts": {},
        "steps": [],
        "warnings": [],
        "error": None,
    }


def _stub_profiles(monkeypatch) -> None:
    monkeypatch.setattr(report_hub.topic_profiles, "list_topic_profiles", lambda: {"items": []})


def _reports_by_run(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["runId"]): item for item in payload["items"]}


def test_collection_and_analysis_runs_are_not_report_intent_without_artifacts(monkeypatch) -> None:
    _stub_profiles(monkeypatch)

    payload = report_hub.build_api_reports(
        [
            _run_record("collection-run", source="api_topic_action:collect"),
            _run_record("analysis-run", source="api_topic_action:analyze"),
        ]
    )

    assert payload["items"] == []
    assert payload["warnings"] == []


def test_explicit_report_run_without_artifact_is_not_completed_or_downloadable(monkeypatch) -> None:
    _stub_profiles(monkeypatch)

    payload = report_hub.build_api_reports(
        [
            _run_record(
                "report-no-artifact",
                source="api_topic_action:report",
                request={"kind": "research_report", "skip_synthesis": False},
            )
        ]
    )
    report = _reports_by_run(payload)["report-no-artifact"]

    assert report["status"] == "artifact_unavailable"
    assert report["downloadUrl"] is None
    assert report["error"] == "보고서 산출물을 찾을 수 없습니다."
    assert payload["warnings"] == ["일부 보고서 실행은 완료되었지만 다운로드 가능한 산출물이 없습니다. 상세 보기에서 실행 로그를 확인하세요."]


def test_preview_only_report_run_is_artifact_unavailable(monkeypatch, tmp_path: Path) -> None:
    _stub_profiles(monkeypatch)
    preview_path = tmp_path / "preview.md"
    preview_path.write_text("# 미리보기만 존재\n", encoding="utf-8")

    payload = report_hub.build_api_reports(
        [
            _run_record(
                "preview-only-report",
                source="topic_schedule:report",
                artifacts={"preview_markdown": str(preview_path)},
            )
        ]
    )
    report = _reports_by_run(payload)["preview-only-report"]

    assert report["status"] == "artifact_unavailable"
    assert report["downloadUrl"] is None


def test_completed_report_requires_existing_markdown_artifact(monkeypatch, tmp_path: Path) -> None:
    _stub_profiles(monkeypatch)
    report_path = tmp_path / "weekly-report.md"
    report_path.write_text("# 실제 보고서\n", encoding="utf-8")

    payload = report_hub.build_api_reports(
        [
            _run_record(
                "completed-report",
                source="topic_schedule:report",
                artifacts={"report_markdown": str(report_path)},
            )
        ]
    )
    report = _reports_by_run(payload)["completed-report"]

    assert report["status"] == "completed"
    assert report["downloadUrl"] == "/runs/completed-report/report/download"


def test_finalize_completed_report_run_without_artifact_becomes_failed(monkeypatch, tmp_path: Path) -> None:
    runs_root = tmp_path / "storage" / "runs"
    run_id = "20260507-report-without-artifact"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    run = _run_record(run_id, source="api_topic_action:report")
    run["write_legacy"] = False
    write_json(run_dir / "run.json", run)

    monkeypatch.setattr(run_repository, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(run_service.reporting, "ensure_compare_ready_metadata", lambda _run_id: None)
    monkeypatch.setattr(run_service.report_versions, "snapshot_completed_report_if_available", lambda _run_id: None)
    monkeypatch.setattr(run_service.metadata_index, "refresh_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(topic_analytics, "refresh_topic_analytics_for_run", lambda _run: [])
    monkeypatch.setattr(run_service.topic_notifications, "emit_notifications_for_run", lambda _run: None)

    finalized = run_service._finalize_run(run_id, status="completed")

    assert finalized["status"] == "failed"
    assert finalized["error"] == "보고서 산출물을 찾을 수 없어 완료 처리하지 않았습니다."


def test_finalize_completed_analysis_run_without_artifact_stays_completed(monkeypatch, tmp_path: Path) -> None:
    runs_root = tmp_path / "storage" / "runs"
    run_id = "20260507-analysis-without-report"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    run = _run_record(run_id, source="api_topic_action:analyze")
    run["write_legacy"] = False
    write_json(run_dir / "run.json", run)

    monkeypatch.setattr(run_repository, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(run_service.reporting, "ensure_compare_ready_metadata", lambda _run_id: None)
    monkeypatch.setattr(run_service.report_versions, "snapshot_completed_report_if_available", lambda _run_id: None)
    monkeypatch.setattr(run_service.metadata_index, "refresh_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(topic_analytics, "refresh_topic_analytics_for_run", lambda _run: [])
    monkeypatch.setattr(run_service.topic_notifications, "emit_notifications_for_run", lambda _run: None)

    finalized = run_service._finalize_run(run_id, status="completed")

    assert finalized["status"] == "completed"
    assert finalized["error"] is None

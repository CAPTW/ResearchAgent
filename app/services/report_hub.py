from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domain.topic_membership import run_matches_topic
from app.repositories.files import iso_now_utc
from app.repositories.runs import build_run_paths
from app.services import report_versions, topic_analytics, topic_profiles

REPORT_TASK_TYPES = {"weekly_report", "monthly_report"}
REPORT_PENDING_STATUSES = {"queued", "running"}
REPORT_ARTIFACT_UNAVAILABLE_STATUS = "artifact_unavailable"
REPORT_ARTIFACT_UNAVAILABLE_ERROR = "보고서 산출물을 찾을 수 없습니다."
REPORT_REQUEST_INTENT_VALUES = {"report", "research_report", "weekly_report", "monthly_report"}


def _topic_profile_by_topic(profiles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        topic = str(profile.get("topic") or "").strip().lower()
        title = str(profile.get("title") or "").strip().lower()
        if topic:
            mapping[topic] = profile
        if title and title not in mapping:
            mapping[title] = profile
    return mapping


def _existing_path(raw_path: Any) -> str | None:
    raw_value = str(raw_path or "").strip()
    if not raw_value:
        return None
    candidate = Path(raw_value)
    return str(candidate) if candidate.exists() else None


def _resolve_report_path(run: dict[str, Any]) -> str | None:
    artifacts = run.get("artifacts") or {}
    for field in ("research_plan_markdown", "report_markdown"):
        resolved = _existing_path(artifacts.get(field))
        if resolved:
            return resolved

    reports_dir = build_run_paths(str(run.get("run_id") or "")).reports_dir
    if reports_dir.exists():
        candidates = sorted(
            [
                path
                for path in reports_dir.glob("*.md")
                if path.name != "preview.md" and not path.name.startswith("exported_")
            ]
        )
        if candidates:
            return str(candidates[-1])
    return None


def _resolve_preview_path(run: dict[str, Any]) -> str | None:
    artifacts = run.get("artifacts") or {}
    resolved = _existing_path(artifacts.get("preview_markdown"))
    if resolved:
        return resolved

    preview_path = build_run_paths(str(run.get("run_id") or "")).reports_dir / "preview.md"
    return str(preview_path) if preview_path.exists() else None


def _schedule_task_type(run: dict[str, Any]) -> str:
    schedule_context = run.get("schedule_context") if isinstance(run.get("schedule_context"), dict) else {}
    return str(schedule_context.get("task_type") or "").strip().lower()


def _request_has_report_intent(request: dict[str, Any]) -> bool:
    for field in ("kind", "task_type"):
        value = str(request.get(field) or "").strip().lower()
        if value in REPORT_REQUEST_INTENT_VALUES or value.endswith("_report"):
            return True
    return False


def _has_report_intent(run: dict[str, Any]) -> bool:
    if _resolve_report_path(run) or _resolve_preview_path(run):
        return True

    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    if request and _request_has_report_intent(request):
        return True

    source = str(run.get("source") or "").strip().lower()
    if source.endswith(":report"):
        return True

    return _schedule_task_type(run) in REPORT_TASK_TYPES


def _normalized_report_status(
    run: dict[str, Any],
    *,
    report_path: str | None,
    preview_path: str | None,
    report_intent: bool,
) -> str:
    raw_status = str(run.get("status") or "").strip().lower()
    if raw_status in REPORT_PENDING_STATUSES:
        return raw_status
    if raw_status == "failed":
        return "failed"
    if raw_status == "completed":
        return "completed" if report_path else REPORT_ARTIFACT_UNAVAILABLE_STATUS
    if report_path:
        return "completed"
    if preview_path or report_intent:
        return REPORT_ARTIFACT_UNAVAILABLE_STATUS
    return "failed"


def _report_title(run: dict[str, Any], report_path: str | None) -> str:
    topic = str(run.get("topic") or "연구 주제").strip() or "연구 주제"
    if report_path:
        stem = Path(report_path).stem.replace("_", " ").replace("-", " ").strip()
        if stem:
            return stem
    task_type = _schedule_task_type(run)
    if task_type == "weekly_report":
        return f"{topic} 주간 보고서"
    if task_type == "monthly_report":
        return f"{topic} 월간 보고서"
    return f"{topic} 보고서"


def _source_providers(run: dict[str, Any]) -> list[str]:
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    source_settings = request.get("source_settings") if isinstance(request.get("source_settings"), dict) else {}
    enabled = source_settings.get("enabledProviders")
    if isinstance(enabled, list):
        providers = [str(provider).strip() for provider in enabled if str(provider).strip()]
        if providers:
            return providers
    provider = str(source_settings.get("defaultProvider") or request.get("source_provider") or run.get("source_provider") or "").strip()
    return [provider] if provider else []


def _report_quality_hub_view(run: dict[str, Any]) -> dict[str, Any] | None:
    summary = run.get("report_quality")
    if not isinstance(summary, dict) or not summary:
        return None
    return {
        "classification": summary.get("report_quality_classification"),
        "overallStatus": summary.get("overall_status"),
        "overallScore": summary.get("overall_score"),
        "maxScore": summary.get("max_score"),
        "threshold": summary.get("threshold"),
        "overclaimDetected": summary.get("overclaim_detected"),
        "evaluationStatus": summary.get("evaluation_status"),
        "evaluatedSurface": summary.get("evaluated_surface"),
        "checkedAt": summary.get("checked_at"),
    }


def _completed_version_summary(run_id: str) -> dict[str, Any]:
    try:
        payload = report_versions.list_report_versions(run_id)
    except FileNotFoundError:
        return {
            "completedVersionCount": 0,
            "latestCompletedVersionId": None,
            "latestCompletedVersionAt": None,
            "latestCompletedVersionHash": None,
            "exportArtifacts": [],
            "exportArtifactSummary": _export_artifact_summary([]),
        }

    versions = [item for item in payload.get("items", []) if item.get("status") == "completed"]
    latest = versions[-1] if versions else {}
    export_artifacts = _latest_version_export_artifacts(latest)
    return {
        "completedVersionCount": len(versions),
        "latestCompletedVersionId": latest.get("version_id") or None,
        "latestCompletedVersionAt": latest.get("created_at") or None,
        "latestCompletedVersionHash": latest.get("content_hash") or None,
        "exportArtifacts": export_artifacts,
        "exportArtifactSummary": _export_artifact_summary(export_artifacts),
    }


def _latest_version_export_artifacts(version: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = version.get("artifacts") if isinstance(version.get("artifacts"), list) else []
    return [artifact for artifact in artifacts if isinstance(artifact, dict)]


def _export_artifact_summary(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    formats: list[str] = []
    downloadable = 0
    unavailable = 0
    for artifact in artifacts:
        artifact_format = str(artifact.get("format") or "").strip()
        if artifact_format and artifact_format not in formats:
            formats.append(artifact_format)
        status = str(artifact.get("status") or "").strip().lower()
        if artifact.get("download_available") is True:
            downloadable += 1
        if status in {"failed", "unavailable"} or artifact.get("download_available") is False:
            unavailable += 1
    return {
        "total": len(artifacts),
        "downloadable": downloadable,
        "unavailable": unavailable,
        "formats": formats,
    }


def _contract_report_item(
    run: dict[str, Any],
    profile_by_topic: dict[str, dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    report_path = _resolve_report_path(run)
    preview_path = _resolve_preview_path(run)
    report_intent = _has_report_intent(run)
    if not report_path and not preview_path and not report_intent:
        return None

    run_topic = str(run.get("topic") or "").strip()
    recorded = run.get("topic_profile_id")
    profile: dict[str, Any] | None
    if recorded is not None and not isinstance(recorded, str):
        profile = None
    elif isinstance(recorded, str) and recorded.strip():
        # A recorded binding never falls back to another visible profile's name.
        profile = profile_by_id.get(recorded.strip())
    else:
        profile = profile_by_topic.get(run_topic.lower())
    topic_id_value = str(profile.get("profile_id") or "").strip() or None if profile else None
    run_id = str(run.get("run_id") or "").strip()
    updated_at = str(run.get("updated_at") or run.get("created_at") or "")
    status = _normalized_report_status(run, report_path=report_path, preview_path=preview_path, report_intent=report_intent)
    item = {
        "id": run_id,
        "runId": run_id,
        "topicId": topic_id_value,
        "topicName": run_topic or "토픽 정보 없음",
        "title": _report_title(run, report_path),
        "createdAt": str(run.get("created_at") or updated_at),
        "updatedAt": updated_at,
        "status": status,
        "downloadUrl": f"/runs/{run_id}/report/download" if report_path else None,
        "error": str(run.get("error") or "").strip() or (REPORT_ARTIFACT_UNAVAILABLE_ERROR if status == REPORT_ARTIFACT_UNAVAILABLE_STATUS else None),
        "sourceProviders": _source_providers(run),
        "reportQuality": _report_quality_hub_view(run),
    }
    item.update(_completed_version_summary(run_id))
    return item


def build_api_reports(
    runs: list[dict[str, Any]],
    *,
    topic_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    selected_profile: dict[str, Any] | None = None
    if topic_id:
        selected_profile = topic_profiles.get_topic_profile(topic_id)

    # Both projections use the same actor-filtered profile list; recorded IDs
    # do not grant visibility to a profile absent from that list.
    visible_profiles = topic_profiles.list_topic_profiles()["items"]
    profile_by_topic = _topic_profile_by_topic(visible_profiles)
    profile_by_id = {
        str(profile.get("profile_id") or "").strip(): profile
        for profile in visible_profiles
        if str(profile.get("profile_id") or "").strip()
    }
    selected_topic = str(selected_profile.get("topic") or "").strip() if selected_profile else ""
    warnings: list[str] = []
    pending_count = 0
    failed_count = 0
    unavailable_count = 0
    items: list[dict[str, Any]] = []
    considered_runs: list[dict[str, Any]] = []

    sorted_runs = sorted(
        runs,
        key=lambda run: str(run.get("updated_at") or run.get("created_at") or ""),
        reverse=True,
    )

    for run in sorted_runs:
        if selected_profile is not None and not run_matches_topic(
            run, selected_topic, str(selected_profile.get("profile_id") or "").strip(),
        ):
            continue
        considered_runs.append(run)

        item = _contract_report_item(run, profile_by_topic, profile_by_id)
        if item is None:
            continue

        if item["status"] in REPORT_PENDING_STATUSES:
            pending_count += 1
        elif item["status"] == "failed":
            failed_count += 1
        elif item["status"] == REPORT_ARTIFACT_UNAVAILABLE_STATUS:
            unavailable_count += 1

        items.append(item)
        if len(items) >= limit:
            break

    if pending_count:
        warnings.append("일부 보고서는 생성 대기 또는 생성 중 상태이며 아직 다운로드 가능한 산출물이 없습니다.")
    if failed_count:
        warnings.append("일부 보고서 실행이 실패했습니다. 상세 보기 또는 실행 상세에서 오류를 확인하세요.")
    if unavailable_count:
        warnings.append("일부 보고서 실행은 완료되었지만 다운로드 가능한 산출물이 없습니다. 상세 보기에서 실행 로그를 확인하세요.")

    return {
        "items": items,
        "total": len(items),
        "provenance": topic_analytics.build_runtime_provenance(
            "reports_hub",
            computed_at=iso_now_utc(),
            source_runs=considered_runs[:limit],
            warnings=warnings,
        ),
        "warnings": warnings,
    }

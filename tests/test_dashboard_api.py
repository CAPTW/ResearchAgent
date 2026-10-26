from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dashboard as dashboard_api
from app.auth.config import get_auth_settings
from app.auth.resource_access import ResourceActor
from app.auth.security import ProductUser, encode_product_session_cookie
from app.db import bootstrap
from app.repositories import runs as run_repository
from app.services import paper_occurrences, profile as run_profile_service, topic_analytics, topic_dashboard, topic_profiles, topic_schedules


def test_api_topic_dashboard_maps_duplicate_occurrence_to_typed_unavailable(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def duplicate_dashboard(*_args, **_kwargs):
        raise paper_occurrences.OccurrenceIdentityDuplicate("duplicate")

    monkeypatch.setattr(topic_dashboard, "build_api_topic_dashboard", duplicate_dashboard)

    response = client.get("/api/topics/large-language-models/dashboard")

    assert response.status_code == 503
    assert response.json() == {"detail": "occurrence_identity_duplicate"}


def test_api_topic_dashboard_does_not_misclassify_unrelated_internal_failure(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dashboard(*_args, **_kwargs):
        raise RuntimeError("unrelated_dashboard_failure")

    monkeypatch.setattr(topic_dashboard, "build_api_topic_dashboard", unexpected_dashboard)

    with pytest.raises(RuntimeError, match="unrelated_dashboard_failure"):
        client.get("/api/topics/large-language-models/dashboard")


def _configure_profile_and_schedule_storage(client, isolated_run_repo, monkeypatch) -> Path:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    client.get("/topic-profiles")
    schedule_dir.mkdir(parents=True, exist_ok=True)
    return schedule_dir


def test_dashboard_api_supports_profiles_and_overview(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", isolated_run_repo["storage_root"].parent / "topic_schedules")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", isolated_run_repo["storage_root"].parent / "topic_schedules" / "schedules.json")
    monkeypatch.setattr(
        dashboard_api,
        "start_run",
        lambda request, source="dashboard_profile", owner_user_id=None, topic_profile_id=None, schedule_context=None: {
            "run_id": "queued-profile-run",
            "topic": request.topic,
            "status": "queued",
        },
    )

    profiles_response = client.get("/topic-profiles")
    overview_response = client.get("/dashboard/overview")
    start_response = client.post("/dashboard/start-runs", json={"profile_ids": ["default-xrml"]})

    assert profiles_response.status_code == 200, profiles_response.text
    assert overview_response.status_code == 200, overview_response.text
    assert start_response.status_code == 200, start_response.text

    profiles_body = profiles_response.json()
    overview_body = overview_response.json()
    start_body = start_response.json()

    assert profiles_body["items"]
    assert overview_body["totals"]["runs"] >= 2
    assert start_body["items"][0]["run_id"] == "queued-profile-run"


def test_topic_dashboard_recovers_dead_schedule_store_lock(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    client.get("/topic-profiles")
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "thread": "aborted-playwright-backend",
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/api/topics/large-language-models/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["topic"]["id"] == "large-language-models"
    assert body["scheduledTasks"] == []
    assert any("GET 요청은 lock cleanup을 수행하지 않습니다" in warning for warning in body["warnings"])
    assert lock_path.exists() is True


def test_topic_dashboard_dead_pid_lock_regression_returns_valid_dashboard_contract(
    client, isolated_run_repo, monkeypatch
) -> None:
    schedule_dir = _configure_profile_and_schedule_storage(client, isolated_run_repo, monkeypatch)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "hostname": topic_schedules._current_lock_hostname(),
                "thread": "aborted-backend-regression",
                "owner": "topic_schedule_store",
                "purpose": "schedule_storage_lock",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/api/topics/large-language-models/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["topic"]["id"] == "large-language-models"
    assert isinstance(body["metrics"], dict)
    assert isinstance(body["scheduledTasks"], list)
    assert isinstance(body["warnings"], list)
    assert not any("Traceback" in warning for warning in body["warnings"])
    assert body["scheduledTasks"] == []
    assert any("GET 요청은 lock cleanup을 수행하지 않습니다" in warning for warning in body["warnings"])
    assert lock_path.exists() is True
    assert topic_schedules.schedule_store_lock_preflight(recover_stale=False)["store_status"] != "available"


def test_topic_dashboard_degrades_on_live_schedule_store_lock(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)
    client.get("/topic-profiles")
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": topic_schedules._current_lock_hostname(),
                "thread": "live-dashboard-test",
                "acquired_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=topic_schedules.STORE_LOCK_STALE_SECONDS + 5)
                ).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/api/topics/large-language-models/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["topic"]["id"] == "large-language-models"
    assert body["scheduledTasks"] == []
    assert any("예약 작업 저장소" in warning for warning in body["warnings"])
    assert lock_path.exists() is True


def test_topic_dashboard_live_pid_lock_regression_degrades_without_deleting_live_lock(
    client, isolated_run_repo, monkeypatch
) -> None:
    schedule_dir = _configure_profile_and_schedule_storage(client, isolated_run_repo, monkeypatch)
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": topic_schedules._current_lock_hostname(),
                "thread": "live-dashboard-regression",
                "owner": "topic_schedule_store",
                "purpose": "schedule_storage_lock",
                "createdAt": (
                    datetime.now(timezone.utc) - timedelta(seconds=topic_schedules.STORE_LOCK_STALE_SECONDS + 5)
                ).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/api/topics/large-language-models/dashboard")
    body = response.json()
    lock_status = topic_schedules.schedule_store_lock_preflight(recover_stale=False)

    assert response.status_code == 200, response.text
    assert body["topic"]["id"] == "large-language-models"
    assert body["scheduledTasks"] == []
    assert any("예약 작업 저장소" in warning for warning in body["warnings"])
    assert lock_path.exists() is True
    assert lock_status["store_status"] == "locked_live_owner"
    assert lock_status["after"]["owner_running"] is True


def test_topic_dashboard_malformed_schedule_lock_regression_is_safe(
    client, isolated_run_repo, monkeypatch
) -> None:
    schedule_dir = _configure_profile_and_schedule_storage(client, isolated_run_repo, monkeypatch)
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text("{not-json", encoding="utf-8")

    response = client.get("/api/topics/large-language-models/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["topic"]["id"] == "large-language-models"
    assert isinstance(body["scheduledTasks"], list)
    assert isinstance(body["warnings"], list)
    assert not any("Traceback" in warning for warning in body["warnings"])


def test_topic_schedules_api_recovers_stale_lock(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    client.get("/topic-profiles")
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(json.dumps({"pid": 999999999, "acquired_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")

    response = client.get("/topic-schedules")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["items"]
    assert body["degraded"] is False
    assert lock_path.exists() is False


def test_topic_schedules_api_degrades_on_live_lock(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)
    client.get("/topic-profiles")
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": topic_schedules._current_lock_hostname(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/topic-schedules")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["items"] == []
    assert body["degraded"] is True
    assert body["read_only"] is True
    assert any("예약 작업 저장소" in warning for warning in body["warnings"])
    assert body["scheduler"]["store_lock"]["owner_running"] is True
    assert lock_path.exists() is True


def test_topic_schedule_write_returns_locked_for_live_lock(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)
    client.get("/topic-profiles")
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "hostname": topic_schedules._current_lock_hostname()}, ensure_ascii=False),
        encoding="utf-8",
    )

    response = client.post(
        "/topic-schedules",
        json={
            "profile_id": "large-language-models",
            "title": "잠금 테스트",
            "task_type": "collect_analyze",
            "cadence": "daily_0900",
            "enabled": False,
        },
    )

    assert response.status_code == 423, response.text
    assert "예약 작업 저장소" in response.json()["detail"]
    assert lock_path.exists() is True


def test_system_settings_reports_live_schedule_store_lock_status(client, isolated_run_repo, monkeypatch) -> None:
    schedule_dir = _configure_profile_and_schedule_storage(client, isolated_run_repo, monkeypatch)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": topic_schedules._current_lock_hostname(),
                "owner": "topic_schedule_store",
                "purpose": "schedule_storage_lock",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/settings/status")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["ops"]["scheduler"]["store_lock"]["locked"] is True
    assert body["ops"]["scheduler"]["store_lock"]["owner_running"] is True
    assert body["ops"]["scheduler"]["store_lock"]["pid"] == os.getpid()
    assert lock_path.exists() is True


def test_dashboard_overview_uses_ready_index_without_forcing_full_backfill(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", isolated_run_repo["storage_root"].parent / "topic_schedules")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", isolated_run_repo["storage_root"].parent / "topic_schedules" / "schedules.json")

    dashboard_api.metadata_index.ensure_index_ready()
    monkeypatch.setattr(
        dashboard_api.metadata_index,
        "backfill_all_runs",
        lambda: (_ for _ in ()).throw(AssertionError("full backfill should not run on ready dashboard reads")),
    )

    response = client.get("/dashboard/overview")

    assert response.status_code == 200, response.text
    assert response.json()["totals"]["runs"] >= 2


def test_topic_profile_mutation_touches_index_state(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", isolated_run_repo["storage_root"].parent / "topic_schedules")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", isolated_run_repo["storage_root"].parent / "topic_schedules" / "schedules.json")

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "Audit Profile",
            "description": "temp",
            "topic": "Audit Topic",
            "queries": ["xr"],
            "categories": ["cs.CV"],
            "max_results": 10,
            "top_papers": 2,
            "weekly_top": 5,
        },
    )
    settings_response = client.get("/settings/status")

    assert create_response.status_code == 200, create_response.text
    assert settings_response.status_code == 200, settings_response.text
    assert settings_response.json()["metadata_index_state"]["last_topic_profiles_updated_at"]
    assert settings_response.json()["mcp"]["cli_ready"] is True
    assert settings_response.json()["mcp"]["readiness_label"] == "local_cli_only"
    assert settings_response.json()["ops"]["run_counts"]["completed"] >= 2
    assert settings_response.json()["ops"]["last_successful_run_id"]
    assert "scheduler" in settings_response.json()["ops"]
    assert "notification_delivery" in settings_response.json()["ops"]
    assert settings_response.json()["deployment"]["scheduler_architecture"] == "single_node_file_backed"
    assert settings_response.json()["deployment"]["distributed_scheduler_supported"] is False


def test_crossref_topic_profile_persists_and_quick_collect_uses_saved_provider(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)

    started_runs: list[dict] = []

    def fake_start_run(request, source="dashboard_profile", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_profile = run_profile_service.build_run_profile(request.model_dump())
        started_runs.append(
            {
                "source": source,
                "topic_profile_id": topic_profile_id,
                "source_provider": request.source_provider,
                "source_settings": request.source_settings,
                "profile_sources": run_profile["sources"],
            }
        )
        return {"run_id": "queued-crossref-run", "topic": request.topic, "status": "queued"}

    monkeypatch.setattr(dashboard_api, "start_run", fake_start_run)

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "Crossref Runtime Topic",
            "topic": "Crossref Runtime Topic",
            "queries": ["fallback query"],
            "categories": ["cs.AI"],
            "max_results": 20,
            "source_provider": "crossref",
            "source_settings": {
                "defaultProvider": "crossref",
                "enabledProviders": ["crossref"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": False,
                        "priority": 2,
                        "queries": ["disabled arxiv"],
                        "categories": ["cs.AI"],
                        "maxResults": 10,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    },
                    "crossref": {
                        "provider": "crossref",
                        "enabled": True,
                        "priority": 1,
                        "queries": ["paper intelligence systems"],
                        "maxResults": 6,
                        "sortBy": "published",
                        "sortOrder": "descending",
                    },
                },
            },
            "top_papers": 3,
            "weekly_top": 10,
        },
    )

    assert create_response.status_code == 200, create_response.text
    created = next(item for item in create_response.json()["items"] if item["profile_id"] == "crossref-runtime-topic")
    assert created["source_provider"] == "crossref"
    assert created["source_settings"]["defaultProvider"] == "crossref"
    assert created["source_settings"]["enabledProviders"] == ["crossref"]
    assert created["source_settings"]["providers"]["arxiv"]["enabled"] is False
    assert created["source_settings"]["providers"]["crossref"]["queries"] == ["paper intelligence systems"]
    assert created["source_settings"]["providers"]["crossref"]["sortBy"] == "published"

    collect_response = client.post("/api/topics/crossref-runtime-topic/actions/collect")

    assert collect_response.status_code == 200, collect_response.text
    assert started_runs
    started = started_runs[-1]
    assert started["source"] == "api_topic_action:collect"
    assert started["topic_profile_id"] == "crossref-runtime-topic"
    assert started["source_provider"] == "crossref"
    assert started["source_settings"]["enabledProviders"] == ["crossref"]
    assert set(started["profile_sources"]) == {"crossref"}
    assert started["profile_sources"]["crossref"]["queries"] == ["paper intelligence systems"]
    assert started["profile_sources"]["crossref"]["sort_by"] == "published"
    assert "arxiv" not in started["profile_sources"]


def test_topic_papers_exposes_provider_counts_and_provider_filter(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)

    topic_profiles.create_topic_profile(
        {
            "title": "Provider Provenance Topic",
            "topic": "Provider Provenance Topic",
            "queries": ["source provenance"],
            "categories": ["cs.AI"],
            "max_results": 10,
            "source_settings": {
                "defaultProvider": "arxiv",
                "enabledProviders": ["arxiv", "crossref"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": True,
                        "priority": 1,
                        "queries": ["source provenance"],
                        "categories": ["cs.AI"],
                        "maxResults": 10,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    },
                    "crossref": {
                        "provider": "crossref",
                        "enabled": True,
                        "priority": 2,
                        "queries": ["source provenance"],
                        "maxResults": 10,
                        "sortBy": "relevance",
                        "sortOrder": "descending",
                    },
                },
            },
        }
    )

    run = {
        "run_id": "provider-run",
        "created_at": "2026-04-30T00:00:00Z",
        "request": {"source_settings": {"defaultProvider": ""}, "source_provider": ""},
    }
    items = [
        (run, {"paper_id": "arxiv-1", "title": "Arxiv Paper", "score": 30, "source_provider": "arxiv"}),
        (run, {"paper_id": "crossref-1", "title": "Crossref Paper", "score": 40, "source_provider": "crossref"}),
        (run, {"paper_id": "unknown-1", "title": "Unknown Paper", "score": 10}),
    ]
    monkeypatch.setattr(topic_dashboard, "_topic_items", lambda topic, max_runs=20, **kwargs: items)
    monkeypatch.setattr(topic_dashboard, "_topic_run_records", lambda topic, **kwargs: [run])

    response = client.get("/api/topics/provider-provenance-topic/papers", params={"provider": "crossref"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["providerFilter"] == "crossref"
    assert [item["id"] for item in payload["items"]] == ["crossref-1"]
    assert {item["provider"]: item["paperCount"] for item in payload["providerCounts"]} == {
        "arxiv": 1,
        "crossref": 1,
        "unknown": 1,
    }
    assert next(item for item in payload["providerCounts"] if item["provider"] == "unknown")["label"] == "출처 미확인"


def test_topic_dashboard_and_schedule_adapters(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    schedule_path = schedule_dir / "schedules.json"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_path)
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    started_runs: list[dict[str, str | None]] = []

    def fake_start_run(request, source="dashboard_profile", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        started_runs.append(
            {
                "topic": request.topic,
                "source": source,
                "owner_user_id": owner_user_id,
                "topic_profile_id": topic_profile_id,
                "source_provider": request.source_provider,
                "arxiv_sort_by": request.arxiv_sort_by,
                "arxiv_sort_order": request.arxiv_sort_order,
                "source_settings_default": request.source_settings.get("defaultProvider") if request.source_settings else None,
            }
        )
        return {
            "run_id": f"queued-{request.topic.lower().replace(' ', '-')}",
            "topic": request.topic,
            "status": "queued",
        }

    monkeypatch.setattr(dashboard_api, "start_run", fake_start_run)
    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)

    profiles_response = client.get("/topic-profiles")
    profile_id = profiles_response.json()["items"][0]["profile_id"]
    topic_response = client.get(f"/dashboard/topic-dashboard?profile_id={profile_id}")
    schedules_response = client.get("/topic-schedules")
    schedule = schedules_response.json()["items"][0]
    scheduler_status = schedules_response.json()["scheduler"]
    assert scheduler_status["runtime_mode"] == "single_node_file_backed"
    assert scheduler_status["storage_backend"] == "file_json"
    assert scheduler_status["distributed_ready"] is False
    assert scheduler_status["backend"]["mode"] == "single_node_file_backed"
    assert scheduler_status["available_runtime_modes"]["db_backed"]["status"] == "available"
    create_schedule_response = client.post(
        "/topic-schedules",
        json={
            "profile_id": profile_id,
            "title": "테스트 월간 보고서",
            "task_type": "monthly_report",
            "cadence": "monthly_day_1_1000",
            "enabled": False,
        },
    )
    patched_schedule_response = client.patch(f"/topic-schedules/{schedule['schedule_id']}", json={"enabled": True})
    created_schedule_id = create_schedule_response.json()["schedule_id"]
    edit_schedule_response = client.patch(
        f"/topic-schedules/{created_schedule_id}",
        json={"title": "테스트 월간 보고서 수정", "enabled": True},
    )
    run_now_response = client.post(f"/topic-schedules/{schedule['schedule_id']}/run-now")
    delete_schedule_response = client.delete(f"/topic-schedules/{created_schedule_id}")
    action_response = client.post("/dashboard/topic-action", json={"profile_id": profile_id, "action": "report"})
    api_topics_response = client.get("/api/topics")
    api_topic_id = api_topics_response.json()["items"][0]["id"]
    api_dashboard_response = client.get(f"/api/topics/{api_topic_id}/dashboard")
    api_papers_response = client.get(f"/api/topics/{api_topic_id}/papers")
    api_reports_response = client.get("/api/reports")
    api_topic_reports_response = client.get(f"/api/reports?topicId={api_topic_id}")
    api_task = api_dashboard_response.json()["scheduledTasks"][0]
    api_task_patch_response = client.patch(f"/api/tasks/{api_task['id']}", json={"enabled": True})
    api_dashboard_after_patch_response = client.get(f"/api/topics/{api_topic_id}/dashboard")
    api_task_run_response = client.post(f"/api/tasks/{api_task['id']}/run")
    api_dashboard_after_run_response = client.get(f"/api/topics/{api_topic_id}/dashboard")
    api_action_response = client.post(f"/api/topics/{api_topic_id}/actions/collect")

    assert topic_response.status_code == 200, topic_response.text
    assert schedules_response.status_code == 200, schedules_response.text
    assert create_schedule_response.status_code == 200, create_schedule_response.text
    assert patched_schedule_response.status_code == 200, patched_schedule_response.text
    assert edit_schedule_response.status_code == 200, edit_schedule_response.text
    assert run_now_response.status_code == 200, run_now_response.text
    assert delete_schedule_response.status_code == 200, delete_schedule_response.text
    assert action_response.status_code == 200, action_response.text
    assert api_topics_response.status_code == 200, api_topics_response.text
    assert api_dashboard_response.status_code == 200, api_dashboard_response.text
    assert api_papers_response.status_code == 200, api_papers_response.text
    assert api_reports_response.status_code == 200, api_reports_response.text
    assert api_topic_reports_response.status_code == 200, api_topic_reports_response.text
    assert api_task_patch_response.status_code == 200, api_task_patch_response.text
    assert api_dashboard_after_patch_response.status_code == 200, api_dashboard_after_patch_response.text
    assert api_task_run_response.status_code == 200, api_task_run_response.text
    assert api_dashboard_after_run_response.status_code == 200, api_dashboard_after_run_response.text
    assert api_action_response.status_code == 200, api_action_response.text

    topic_body = topic_response.json()
    assert topic_body["topic"]["profile_id"] == profile_id
    assert topic_body["topic"]["status"] == "active"
    assert topic_body["topic"]["source_provider"] == "arxiv"
    assert topic_body["topic"]["arxiv_sort_by"] == "submittedDate"
    assert topic_body["topic"]["arxiv_sort_order"] == "descending"
    assert topic_body["topic"]["source_settings"]["defaultProvider"] == "arxiv"
    assert topic_body["topic"]["source_settings"]["providers"]["arxiv"]["enabled"] is True
    assert topic_body["topic"]["source_statuses"][0]["provider"] == "arxiv"
    assert "latest_papers" in topic_body
    assert "institution_rankings" in topic_body
    assert topic_body["distributions"]["themes"] is not None

    assert patched_schedule_response.json()["items"][0]["enabled"] is True
    assert len([item for item in schedules_response.json()["items"] if item["profile_id"] == profile_id]) == 4
    assert schedule["task_type"] == "collect_analyze"
    assert schedule["title"].endswith("최신 논문 수집 및 분석")
    assert schedule["cadence_label"] == "매일 09:00"
    assert [step["step_id"] for step in schedule["pipeline"]] == [
        "paper_collection",
        "storage",
        "analysis",
        "report_generation",
    ]
    assert create_schedule_response.json()["title"] == "테스트 월간 보고서"
    edited_schedule = next(item for item in edit_schedule_response.json()["items"] if item["schedule_id"] == created_schedule_id)
    assert edited_schedule["enabled"] is True
    assert edited_schedule["title"] == "테스트 월간 보고서 수정"
    assert run_now_response.json()["run"]["status"] == "queued"
    assert action_response.json()["action"] == "report"

    api_body = api_dashboard_response.json()
    assert api_body["topic"]["id"] == api_topic_id
    assert api_body["topic"]["status"] == "active"
    assert set(api_body["metrics"]) == {
        "collectedPapers",
        "corePapers",
        "averageImpact",
        "totalCitations",
        "trendIndex",
    }
    assert set(api_body["trends"]) == {"labels", "totalPapers", "corePapers", "citations"}
    assert "latestCorePapers" in api_body
    assert "fieldDistribution" in api_body
    assert "institutionDistribution" in api_body
    assert "analytics" in api_body
    assert api_body["sourceSummary"]["defaultProvider"] == "arxiv"
    assert api_body["sourceSummary"]["statuses"][0]["available"] is True
    assert api_body["analytics"]["profileId"] == api_topic_id
    assert api_body["analytics"]["artifactVersion"] == "topic-analytics-v2"
    assert api_body["analytics"]["computedAt"]
    assert api_body["analytics"]["freshness"]["sourceRunCount"] == len(api_body["analytics"]["sourceRuns"])
    assert any(item["surfaceId"] == "metric_cards" for item in api_body["analytics"]["derivations"])
    assert api_body["analytics"]["metricDefinitions"]
    assert "trendDetail" in api_body["analytics"]
    assert set(api_body["analytics"]["coverage"][0]) == {
        "coverageId",
        "label",
        "covered",
        "total",
        "percentage",
        "emptyReason",
    }
    assert set(api_papers_response.json()) == {"topic", "items", "total", "q", "providerFilter", "providerCounts", "provenance", "warnings"}
    assert api_papers_response.json()["provenance"]["surfaceId"] == "topic_papers"
    assert api_papers_response.json()["provenance"]["mode"] == "runtime_adapter"
    assert set(api_reports_response.json()) == {"items", "total", "provenance", "warnings"}
    assert api_reports_response.json()["provenance"]["surfaceId"] == "reports_hub"
    assert api_reports_response.json()["provenance"]["mode"] == "runtime_adapter"
    if api_reports_response.json()["items"]:
        assert set(api_reports_response.json()["items"][0]) == {
            "id",
            "runId",
            "topicId",
            "topicName",
            "title",
            "createdAt",
            "updatedAt",
            "status",
            "downloadUrl",
                "error",
                "sourceProviders",
                "reportQuality",
                "completedVersionCount",
                "latestCompletedVersionId",
                "latestCompletedVersionAt",
                "latestCompletedVersionHash",
                "exportArtifacts",
                "exportArtifactSummary",
            }
    assert api_topic_reports_response.json()["total"] <= api_reports_response.json()["total"]
    assert [task["taskType"] for task in api_body["scheduledTasks"]] == [
        "collect_analyze",
        "weekly_report",
        "monthly_report",
        "citation_alert",
    ]
    assert api_task_patch_response.json()["enabled"] is True
    assert api_task_patch_response.json()["schedulerState"] in {"active", "running", "failed"}
    assert api_task_patch_response.json()["nextRunAt"] is not None
    persisted_schedules = json.loads(schedule_path.read_text(encoding="utf-8"))
    persisted_task = next(item for item in persisted_schedules if item["schedule_id"] == api_task["id"])
    assert persisted_task["enabled"] is True
    assert persisted_task["next_run_at"] is not None
    assert next(task for task in api_dashboard_after_patch_response.json()["scheduledTasks"] if task["id"] == api_task["id"])[
        "enabled"
    ] is True
    assert api_task_run_response.json()["run"]["status"] == "queued"
    assert next(task for task in api_dashboard_after_run_response.json()["scheduledTasks"] if task["id"] == api_task["id"])[
        "lastRunAt"
    ]
    assert created_schedule_id not in {item["schedule_id"] for item in json.loads(schedule_path.read_text(encoding="utf-8")) if not item.get("deleted")}
    assert any(run["source"] == "topic_schedule_run_now:collect_analyze" for run in started_runs)
    assert all(run["source_provider"] == "arxiv" for run in started_runs)
    assert all(run["arxiv_sort_by"] == "submittedDate" for run in started_runs)
    assert all(run["arxiv_sort_order"] == "descending" for run in started_runs)
    assert all(run["source_settings_default"] == "arxiv" for run in started_runs)
    assert api_action_response.json()["action"] == "collect"
    assert any(run["source"] == "api_topic_action:collect" for run in started_runs)


@pytest.mark.parametrize("is_admin", [False, True], ids=["owner", "admin-owner"])
def test_api_reports_preserves_authenticated_actor_for_topic_lookup(
    app_factory, isolated_run_repo, is_admin: bool,
) -> None:
    app = app_factory(
        AUTH_MODE="oidc",
        SESSION_SECRET="synthetic-report-actor-session-secret-0123456789abcdef",
        APP_BASE_URL="http://testserver",
    )
    saved = {}
    for owner in ("report-owner-a", "report-owner-b"):
        profile = topic_profiles.create_topic_profile(
            {"title": owner, "topic": owner, "queries": [owner]},
            actor=ResourceActor.oidc_user(owner),
        )
        run = run_repository.create_run_record(
            topic=owner,
            request_payload={"topic": owner, "queries": [owner]},
            source="api_topic_action:report",
            write_legacy=False,
            owner_user_id=owner,
        )
        report_path = run_repository.build_run_paths(run["run_id"]).reports_dir / "report.md"
        report_path.write_text(f"# Synthetic report for {owner}\n", encoding="utf-8")
        run.update(status="completed", topic_profile_id=profile["profile_id"])
        run["artifacts"]["report_markdown"] = str(report_path)
        saved[owner] = (profile, run_repository.save_run_record(run))

    own_profile, own_run = saved["report-owner-a"]
    other_profile, other_run = saved["report-owner-b"]
    with TestClient(app) as client:
        assert client.get("/api/reports").status_code == 401
        settings = get_auth_settings()
        user = ProductUser(
            user_id="report-owner-a", display_name="Synthetic report owner",
            email="report-owner@example.invalid", auth_source="oidc", is_admin=is_admin,
        )
        client.cookies.set(settings.session_cookie_name, encode_product_session_cookie(user, settings))
        identity = client.get("/auth/me").json()
        assert identity["auth_mode"] == "oidc"
        assert identity["is_admin"] is is_admin
        assert identity["user"]["user_id"] == user.user_id

        for params in ({}, {"topicId": own_profile["profile_id"]}):
            response = client.get("/api/reports", params=params)
            assert response.status_code == 200, response.text
            assert [(item["runId"], item["topicId"]) for item in response.json()["items"]] == [
                (own_run["run_id"], own_profile["profile_id"]),
            ]
            assert response.json()["provenance"]["sourceRunIds"] == [own_run["run_id"]]
            assert other_run["run_id"] not in response.text

        other_response = client.get("/api/reports", params={"topicId": other_profile["profile_id"]})
        # Admin privileges do not grant access to another actor's private topic.
        assert other_response.status_code == 404
        assert other_response.json() == {"detail": "Resource not found."}
        assert client.get("/api/reports", params={"topicId": "missing-report-topic"}).status_code == 404


def test_api_reports_surface_truthful_lifecycle_states(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)

    topic_profiles.create_topic_profile(
        {
            "title": "Report Lifecycle Topic",
            "description": "report lifecycle test",
            "topic": "Report Lifecycle Topic",
            "queries": ["report lifecycle"],
            "categories": ["cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        }
    )
    profile_id = next(
        profile["profile_id"]
        for profile in topic_profiles.list_topic_profiles()["items"]
        if profile["topic"] == "Report Lifecycle Topic"
    )

    def create_report_run(
        *,
        source: str,
        status: str,
        skip_synthesis: bool = False,
        error: str | None = None,
        with_report: bool = False,
        with_preview: bool = False,
    ) -> dict:
        run = run_repository.create_run_record(
            topic="Report Lifecycle Topic",
            request_payload={
                "topic": "Report Lifecycle Topic",
                "queries": ["report lifecycle"],
                "categories": ["cs.AI"],
                "max_results": 10,
                "top_papers": 3,
                "weekly_top": 10,
                "skip_synthesis": skip_synthesis,
            },
            source=source,
            write_legacy=False,
        )
        paths = run_repository.build_run_paths(run["run_id"])
        if with_report:
            report_path = paths.reports_dir / "2026-04.md"
            report_path.write_text("# report\n\ncompleted", encoding="utf-8")
            run.setdefault("artifacts", {})["report_markdown"] = str(report_path)
        if with_preview:
            preview_path = paths.reports_dir / "preview.md"
            preview_path.write_text("# preview\n\nrunning", encoding="utf-8")
            run.setdefault("artifacts", {})["preview_markdown"] = str(preview_path)
        run["status"] = status
        run["topic_profile_id"] = profile_id
        run["error"] = error
        return run_repository.save_run_record(run)

    queued_run = create_report_run(source="api_topic_action:report", status="queued")
    running_run = create_report_run(source="topic_schedule_auto:weekly_report", status="running", with_preview=True)
    completed_run = create_report_run(source="api_topic_action:report", status="completed", with_report=True)
    failed_run = create_report_run(source="api_topic_action:report", status="failed", error="report synthesis failed")
    collect_only_run = create_report_run(source="api_topic_action:collect", status="completed", skip_synthesis=True)

    reports_response = client.get(f"/api/reports?topicId={profile_id}")
    dashboard_response = client.get(f"/api/topics/{profile_id}/dashboard")

    assert reports_response.status_code == 200, reports_response.text
    assert dashboard_response.status_code == 200, dashboard_response.text

    reports_body = reports_response.json()
    report_statuses = {item["runId"]: item["status"] for item in reports_body["items"]}

    assert report_statuses[queued_run["run_id"]] == "queued"
    assert report_statuses[running_run["run_id"]] == "running"
    assert report_statuses[completed_run["run_id"]] == "completed"
    assert report_statuses[failed_run["run_id"]] == "failed"
    assert collect_only_run["run_id"] not in report_statuses
    assert any("생성 대기" in warning or "생성 중" in warning for warning in reports_body["warnings"])

    download_response = client.get(f"/runs/{completed_run['run_id']}/report/download")
    assert download_response.status_code == 200, download_response.text
    assert download_response.headers["content-type"].startswith("text/markdown")
    assert "# report" in download_response.text

    dashboard_recent_reports = {item["runId"]: item for item in dashboard_response.json()["recentReports"]}
    dashboard_reports = {run_id: item["status"] for run_id, item in dashboard_recent_reports.items()}
    assert dashboard_reports[queued_run["run_id"]] == "queued"
    assert dashboard_reports[completed_run["run_id"]] == "completed"
    assert dashboard_recent_reports[completed_run["run_id"]]["error"] is None
    assert dashboard_response.json()["analytics"]["trendDetail"]["bucketMode"] in {
        "published_month",
        "mixed_source_month",
        "run_created_month_fallback",
    }

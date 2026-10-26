"""Unavailable time zones must not silently dispatch a saved schedule in UTC."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import pytest

from app.auth.resource_access import ResourceAccessDenied, ResourceActor
from app.services import topic_dashboard, topic_profiles, topic_schedules
from tests.test_owner_scoped_resources import owner_scoped_storage as owner_scoped_storage
from tests.test_topic_scheduler import _configure_topic_storage


MODES = ["single_node_file_backed", "db_backed", "queue_backed"]
UNKNOWN_ZONE = "Unavailable/Preserved_Zone"
NOW = datetime(2026, 10, 5, 4, tzinfo=timezone.utc)


def _seed_unavailable(backend):
    schedules = backend.load_task_definitions()
    target = schedules[0]
    target.update(
        timezone=UNKNOWN_ZONE, enabled=True,
        next_run_at="2026-10-01T00:00:00+00:00", next_run_hint=None,
        last_run_id="preserved-history", last_status="failed",
        last_error="preserved-research-error", last_failure_message="preserved-research-error",
    )
    backend.save_task_definitions(schedules)
    return deepcopy(target)


@pytest.mark.parametrize("zone,offset", [("UTC", 0), ("Etc/UTC", 0), ("Asia/Seoul", 9)])
def test_known_timezone_fallbacks_keep_their_existing_clock(monkeypatch, zone, offset):
    def unavailable(_name):
        raise ZoneInfoNotFoundError("No installed time-zone data")

    monkeypatch.setattr(topic_schedules, "ZoneInfo", unavailable)
    assert topic_schedules._timezone_from_name(zone).utcoffset(NOW) == timedelta(hours=offset)


@pytest.mark.parametrize("zone", [UNKNOWN_ZONE, "../invalid"])
def test_unknown_timezone_cannot_compute_a_utc_next_run(zone):
    with pytest.raises(RuntimeError, match="시간대"):
        topic_schedules.compute_next_run_at("daily_0900", now=NOW, timezone_name=zone)


@pytest.mark.parametrize("mode", MODES)
def test_unavailable_saved_schedule_is_visible_but_not_due_and_history_is_preserved(tmp_path, monkeypatch, mode):
    _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    topic_profiles.list_topic_profiles()
    backend = topic_schedules.get_scheduler_backend()
    original = _seed_unavailable(backend)
    schedule_id = original["schedule_id"]

    response = topic_schedules.list_topic_schedules()
    target = next(item for item in response["items"] if item["schedule_id"] == schedule_id)
    assert target["scheduler_state"] == "blocked"
    assert "시간대" in target["timezone_error"]
    assert UNKNOWN_ZONE in target["timezone_error"]
    assert target["next_run_hint"] is None
    for key in ("schedule_id", "timezone", "next_run_at", "last_run_id", "last_status", "last_error", "last_failure_message"):
        assert target[key] == original[key]
    assert all(item.get("timezone_error") is None for item in response["items"] if item["schedule_id"] != schedule_id)
    assert topic_schedules.get_topic_schedule(schedule_id) == target
    contract = topic_dashboard.contract_task(target)
    assert contract["timezoneError"] == target["timezone_error"]
    assert contract["nextRunAt"] is None
    assert contract["lastError"] == original["last_error"]
    assert topic_schedules._schedule_runtime_summary(now=NOW, schedules=[original])["due"] == 0
    _, due_ids = backend.query_due_task_ids(backend.load_task_definitions(), now=NOW)
    assert schedule_id not in due_ids
    if mode == "queue_backed":
        assert backend.queue_backend.list_items() == []

    # Derived availability never rewrites the saved time zone or execution history.
    reopened = topic_schedules.get_scheduler_backend().load_task_definitions()
    saved = next(item for item in reopened if item["schedule_id"] == schedule_id)
    assert saved == original


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("operation", ["enable", "cadence", "manual", "automatic"])
def test_unavailable_timezone_refuses_mutation_before_lease_or_run(tmp_path, monkeypatch, mode, operation):
    _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    topic_profiles.list_topic_profiles()
    backend = topic_schedules.get_scheduler_backend()
    original = _seed_unavailable(backend)
    before = deepcopy(backend.load_task_definitions())
    calls = []

    def unexpected_run(*args, **kwargs):
        calls.append((args, kwargs))
        return {"run_id": "must-not-start", "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", unexpected_run)
    with pytest.raises(RuntimeError, match="시간대"):
        if operation in {"manual", "automatic"}:
            topic_schedules.trigger_topic_schedule_run(original["schedule_id"], trigger=operation, due_at=NOW)
        else:
            payload = {"enabled": True} if operation == "enable" else {"cadence": "daily_1000"}
            topic_schedules.update_topic_schedule(original["schedule_id"], payload)
    assert calls == []
    assert backend.load_task_definitions() == before


@pytest.mark.parametrize("mode", MODES)
def test_schedule_api_reports_zone_problem_and_allows_pause_and_delete(client, monkeypatch, mode):
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    assert client.get("/topic-schedules").status_code == 200
    backend = topic_schedules.get_scheduler_backend()
    original = _seed_unavailable(backend)
    schedule_id = original["schedule_id"]
    response = client.get("/topic-schedules")
    assert response.status_code == 200, response.text
    target = next(item for item in response.json()["items"] if item["schedule_id"] == schedule_id)
    assert target["scheduler_state"] == "blocked"
    assert "시간대" in target["timezone_error"]
    denied = client.patch(f"/topic-schedules/{schedule_id}", json={"enabled": True})
    assert denied.status_code == 400, denied.text
    assert "시간대" in denied.json()["detail"]
    paused = client.patch(f"/topic-schedules/{schedule_id}", json={"enabled": False})
    assert paused.status_code == 200, paused.text
    target = next(item for item in paused.json()["items"] if item["schedule_id"] == schedule_id)
    assert target["enabled"] is False
    assert target["timezone"] == UNKNOWN_ZONE
    assert target["last_error"] == original["last_error"]
    assert client.delete(f"/topic-schedules/{schedule_id}").status_code == 200


@pytest.mark.parametrize("mode", MODES)
def test_hidden_unavailable_schedule_does_not_break_other_owner_reads(owner_scoped_storage, monkeypatch, mode):
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    alice = ResourceActor.oidc_user("alice")
    bob = ResourceActor.oidc_user("bob")
    for actor in (alice, bob):
        topic_profiles.create_topic_profile({"title": actor.subject, "topic": actor.subject}, actor=actor)
    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    target = next(item for item in schedules if item["owner_user_id"] == "alice")
    target.update(timezone=UNKNOWN_ZONE, next_run_hint=None)
    backend.save_task_definitions(schedules)
    before = deepcopy(backend.load_task_definitions())
    response = topic_schedules.list_topic_schedules(actor=bob)
    assert response["items"]
    assert any(item["owner_user_id"] == "bob" for item in response["items"])
    assert all(item["owner_user_id"] != "alice" for item in response["items"])
    assert all(item.get("timezone_error") is None for item in response["items"])
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.get_topic_schedule(target["schedule_id"], actor=bob)
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.trigger_topic_schedule_run(target["schedule_id"], trigger="manual", actor=bob)
    owned = topic_schedules.get_topic_schedule(target["schedule_id"], actor=alice)
    assert "시간대" in owned["timezone_error"]
    assert backend.load_task_definitions() == before


def test_dashboard_api_projects_timezone_error_without_writing_saved_file(client, monkeypatch):
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "single_node_file_backed")
    assert client.get("/topic-schedules").status_code == 200
    original = _seed_unavailable(topic_schedules.get_scheduler_backend())
    path = topic_schedules.SCHEDULE_LIBRARY_PATH
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    response = client.get(f"/api/topics/{original['profile_id']}/dashboard")
    assert response.status_code == 200, response.text
    target = next(item for item in response.json()["scheduledTasks"] if item["id"] == original["schedule_id"])
    assert target["schedulerState"] == "blocked"
    assert UNKNOWN_ZONE in target["timezoneError"]
    assert target["nextRunAt"] is None
    assert target["lastError"] == original["last_error"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("mode", MODES)
def test_zone_data_recovery_rechecks_availability_without_losing_saved_clock(tmp_path, monkeypatch, mode):
    _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    monkeypatch.setattr(topic_schedules, "_utc_now", lambda: NOW)
    topic_profiles.list_topic_profiles()
    backend = topic_schedules.get_scheduler_backend()
    original = _seed_unavailable(backend)
    schedules = backend.load_task_definitions()
    target = next(item for item in schedules if item["schedule_id"] == original["schedule_id"])
    target["next_run_at"] = "2027-01-01T00:00:00+00:00"
    backend.save_task_definitions(schedules)
    assert topic_schedules.get_topic_schedule(original["schedule_id"])["timezone_error"]
    previous_resolver = topic_schedules.ZoneInfo

    def recovered_zone(name):
        return timezone(timedelta(hours=2)) if name == UNKNOWN_ZONE else previous_resolver(name)

    monkeypatch.setattr(topic_schedules, "ZoneInfo", recovered_zone)
    recovered = topic_schedules.get_topic_schedule(original["schedule_id"])
    assert recovered.get("timezone_error") is None
    assert recovered["scheduler_state"] != "blocked"
    assert recovered["next_run_at"] == "2027-01-01T00:00:00+00:00"
    assert recovered["next_run_hint"] == "다음 실행 01/01 02:00"
    assert recovered["timezone"] == UNKNOWN_ZONE
    assert recovered["last_error"] == original["last_error"]
    assert topic_schedules.compute_next_run_at("daily_0900", now=NOW, timezone_name=UNKNOWN_ZONE) == "2026-10-05T07:00:00+00:00"
    assert all("timezone_error" not in item for item in backend.load_task_definitions())
    if mode == "queue_backed":
        assert backend.queue_backend.list_items() == []


def test_rejected_write_preserves_unavailable_zone_saved_hint_and_file_bytes(tmp_path, monkeypatch):
    _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "single_node_file_backed")
    topic_profiles.list_topic_profiles()
    backend = topic_schedules.get_scheduler_backend()
    original = _seed_unavailable(backend)
    schedules = backend.load_task_definitions()
    target = next(item for item in schedules if item["schedule_id"] == original["schedule_id"])
    target["next_run_hint"] = "previous environment's display hint"
    backend.save_task_definitions(schedules)
    path = topic_schedules.SCHEDULE_LIBRARY_PATH
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(RuntimeError, match="시간대"):
        topic_schedules.update_topic_schedule(original["schedule_id"], {"enabled": True})
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before

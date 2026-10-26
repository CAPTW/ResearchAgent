"""A reused UI route must distinguish a fresh store from a failed schedule read."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.services import topic_schedules


@pytest.fixture
def client(app_factory, tmp_path):
    # The mixed-profile case has no run library or previously materialized DB.
    with TestClient(app_factory(XRML_DATA_DIR=str(tmp_path / "fresh_app"), AUTH_MODE="disabled")) as value:
        yield value


@pytest.fixture
def fresh_schedule_store(tmp_path, monkeypatch):
    root = tmp_path / "fresh_schedules"
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", root)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", root / "schedules.json")
    return root


def dashboard(client, topic="large-language-models"):
    response = client.get(f"/api/topics/{topic}/dashboard")
    assert response.status_code == 200, response.text
    return response.json()


def test_fresh_store_direct_topic_route_has_disabled_defaults_without_seeding(client, fresh_schedule_store):
    body = dashboard(client)
    assert len(body["scheduledTasks"]) == 4
    assert all(not task["enabled"] for task in body["scheduledTasks"])
    assert body["scheduleStatus"]["state"] == "defaults"
    assert not (fresh_schedule_store / "schedules.json").exists()


def test_contended_dashboard_read_is_unavailable_and_retry_recovers(client, fresh_schedule_store):
    # An actual OS lock held by the first schedule-list materialization, not a
    # mocked empty response. Dashboard GET must neither seed nor clean this lock.
    with topic_schedules._schedule_store_lock():
        marker = fresh_schedule_store / "schedules.lock"
        before = marker.read_bytes()
        body = dashboard(client)
        assert body["scheduledTasks"] == []
        assert any("잠겨" in warning for warning in body["warnings"])
        assert marker.read_bytes() == before
        assert body["scheduleStatus"]["state"] == "unavailable"
        assert body["scheduleStatus"]["warnings"]
    recovered = dashboard(client)
    assert recovered["scheduleStatus"]["state"] == "defaults"
    assert len(recovered["scheduledTasks"]) == 4
    assert all(not task["enabled"] for task in recovered["scheduledTasks"])
    assert not (fresh_schedule_store / "schedules.json").exists()


def test_unreadable_schedule_artifact_is_not_replaced_by_defaults(client, fresh_schedule_store):
    fresh_schedule_store.mkdir()
    path = fresh_schedule_store / "schedules.json"
    path.write_bytes(b"{broken-json")
    body = dashboard(client)
    assert body["scheduledTasks"] == []
    assert body["scheduleStatus"]["state"] == "unavailable"
    assert path.read_bytes() == b"{broken-json"


def test_persisted_schedule_values_survive_repeated_direct_reads(client, fresh_schedule_store):
    response = client.get("/topic-schedules")
    assert response.status_code == 200
    path = fresh_schedule_store / "schedules.json"
    before = path.read_bytes()
    first = dashboard(client)
    second = dashboard(client)
    assert first["scheduleStatus"]["state"] == "ready"
    assert first["scheduledTasks"] == second["scheduledTasks"]
    assert len(first["scheduledTasks"]) == 4
    assert path.read_bytes() == before


def test_first_aggregate_list_overlapping_dashboard_does_not_look_empty(client, fresh_schedule_store, monkeypatch):
    saved, release = Event(), Event()
    original_save = topic_schedules._save_schedules

    def hold_materialization(items):
        original_save(items)
        saved.set()
        assert release.wait(60), "test did not release the materializing request"

    monkeypatch.setattr(topic_schedules, "_save_schedules", hold_materialization)
    with ThreadPoolExecutor(max_workers=1) as pool:
        materialization = pool.submit(client.get, "/topic-schedules")
        try:
            assert saved.wait(60), "aggregate list did not reach first persistence"
            path = fresh_schedule_store / "schedules.json"
            before = path.read_bytes()
            blocked = dashboard(client)
            assert blocked["scheduleStatus"]["state"] == "unavailable"
            assert blocked["scheduledTasks"] == []
            assert path.read_bytes() == before
        finally:
            release.set()
        assert materialization.result(timeout=60).status_code == 200
    recovered = dashboard(client)
    assert recovered["scheduleStatus"]["state"] == "ready"
    assert len(recovered["scheduledTasks"]) == 4
    assert all(not item["enabled"] for item in recovered["scheduledTasks"])
    assert path.read_bytes() == before

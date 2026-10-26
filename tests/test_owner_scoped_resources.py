from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dashboard import router as dashboard_router
from app.auth.config import clear_auth_settings_cache
from app.auth.resource_access import (
    ResourceAccessDenied,
    ResourceAction,
    ResourceActor,
    ResourceAdminRequired,
    ResourceKind,
    ResourceOwnerMissing,
    authorize_resource_access,
    local_resource_actor,
    require_global_admin,
    scoped_system_actor,
)
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.db import bootstrap as db_bootstrap
from app.domain import constants
from app.repositories import metadata_index
from app.repositories import runs as run_repository
from app.services import (
    topic_dashboard,
    topic_notifications,
    topic_profiles,
    topic_schedules,
)


@pytest.fixture
def owner_scoped_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("SESSION_SECRET", "owner-scope-test-session-secret-0123456789")
    monkeypatch.delenv("TOPIC_SCHEDULER_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("SCHEDULER_MODE", raising=False)
    clear_auth_settings_cache()

    state_root = tmp_path / "state"
    runs_root = state_root / "runs"
    profile_dir = state_root / "topic_profiles"
    schedule_dir = state_root / "topic_schedules"
    notification_dir = state_root / "topic_notifications"
    runs_root.mkdir(parents=True)

    monkeypatch.setattr(constants, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", runs_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(
        topic_profiles,
        "PROFILE_LIBRARY_PATH",
        profile_dir / "profiles.json",
    )
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(
        topic_schedules,
        "SCHEDULE_LIBRARY_PATH",
        schedule_dir / "schedules.json",
    )
    monkeypatch.setattr(
        topic_schedules,
        "SCHEDULER_STATUS_PATH",
        schedule_dir / "worker_status.json",
    )
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_LIBRARY_DIR",
        notification_dir,
    )
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_SETTINGS_PATH",
        notification_dir / "settings.json",
    )
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_EVENTS_PATH",
        notification_dir / "notifications.json",
    )
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_DELIVERIES_PATH",
        notification_dir / "deliveries.json",
    )
    monkeypatch.setattr(metadata_index, "touch_index_state", lambda *_args, **_kwargs: {})

    yield {
        "root": state_root,
        "runs": runs_root,
        "profiles": profile_dir / "profiles.json",
        "schedules": schedule_dir / "schedules.json",
        "notifications": notification_dir,
        "scheduler_db": state_root / "scheduler.sqlite3",
    }

    clear_auth_settings_cache()


def _actor(subject: str, *, admin: bool = False) -> ResourceActor:
    return ResourceActor.oidc_user(subject, is_admin=admin)


def _profile_payload(title: str) -> dict[str, object]:
    return {
        "title": title,
        "topic": title,
        "queries": [title.lower()],
        "categories": ["cs.AI"],
        "max_results": 5,
    }


def _schedule_payload(profile_id: str, title: str) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "title": title,
        "task_type": "collect_papers",
        "cadence": "daily_0900",
        "enabled": False,
    }


def test_central_policy_uses_exact_subject_and_scopes_system_actor(
    owner_scoped_storage: dict[str, Path],
) -> None:
    user_a = _actor("subject-a")
    user_b = _actor("subject-b")
    admin = _actor("admin-subject", admin=True)

    assert (
        authorize_resource_access(
            actor=user_a,
            resource_kind=ResourceKind.USER_OWNED,
            action=ResourceAction.UPDATE,
            owner_user_id="subject-a",
        )
        == user_a
    )
    with pytest.raises(ResourceAccessDenied):
        authorize_resource_access(
            actor=user_b,
            resource_kind=ResourceKind.USER_OWNED,
            action=ResourceAction.READ,
            owner_user_id="subject-a",
        )
    with pytest.raises(ResourceAccessDenied):
        authorize_resource_access(
            actor=admin,
            resource_kind=ResourceKind.USER_OWNED,
            action=ResourceAction.DELETE,
            owner_user_id="subject-a",
        )
    with pytest.raises(ResourceOwnerMissing):
        authorize_resource_access(
            actor=user_a,
            resource_kind=ResourceKind.USER_OWNED,
            action=ResourceAction.READ,
            owner_user_id=None,
        )

    system_a = scoped_system_actor("subject-a")
    authorize_resource_access(
        actor=system_a,
        resource_kind=ResourceKind.INHERITED_OWNER,
        action=ResourceAction.TRIGGER,
        owner_user_id="subject-a",
    )
    with pytest.raises(ResourceAccessDenied):
        authorize_resource_access(
            actor=system_a,
            resource_kind=ResourceKind.INHERITED_OWNER,
            action=ResourceAction.TRIGGER,
            owner_user_id="subject-b",
        )
    assert require_global_admin(admin) == admin
    with pytest.raises(ResourceAdminRequired):
        require_global_admin(user_a)
    with pytest.raises(ResourceAdminRequired):
        require_global_admin(system_a)


def test_profile_owner_assignment_filtering_spoof_rejection_and_lock_check(
    owner_scoped_storage: dict[str, Path],
) -> None:
    user_a = _actor("subject-a")
    user_b = _actor("subject-b")
    profile_a = topic_profiles.create_topic_profile(
        _profile_payload("Owner A Topic"),
        actor=user_a,
    )
    profile_b = topic_profiles.create_topic_profile(
        _profile_payload("Owner B Topic"),
        actor=user_b,
    )

    stored = json.loads(owner_scoped_storage["profiles"].read_text(encoding="utf-8"))
    stored_by_id = {item["profile_id"]: item for item in stored}
    assert stored_by_id[profile_a["profile_id"]]["owner_user_id"] == "subject-a"
    assert stored_by_id[profile_b["profile_id"]]["owner_user_id"] == "subject-b"

    a_ids = {
        item["profile_id"]
        for item in topic_profiles.list_topic_profiles(actor=user_a)["items"]
    }
    b_ids = {
        item["profile_id"]
        for item in topic_profiles.list_topic_profiles(actor=user_b)["items"]
    }
    assert profile_a["profile_id"] in a_ids
    assert profile_b["profile_id"] not in a_ids
    assert profile_b["profile_id"] in b_ids
    assert profile_a["profile_id"] not in b_ids
    assert topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS <= a_ids
    assert topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS <= b_ids

    before = owner_scoped_storage["profiles"].read_bytes()
    with pytest.raises(ResourceAccessDenied, match="Resource not found"):
        topic_profiles.get_topic_profile(profile_a["profile_id"], actor=user_b)
    with pytest.raises(ResourceAccessDenied, match="Resource not found"):
        topic_profiles.update_topic_profile(
            profile_a["profile_id"],
            {"title": "foreign write"},
            actor=user_b,
        )
    with pytest.raises(ResourceAccessDenied, match="Resource not found"):
        topic_profiles.delete_topic_profile(profile_a["profile_id"], actor=user_b)
    assert owner_scoped_storage["profiles"].read_bytes() == before

    with pytest.raises(ValueError, match="authorization fields"):
        topic_profiles.update_topic_profile(
            profile_a["profile_id"],
            {"owner_user_id": "subject-b"},
            actor=user_a,
        )
    with pytest.raises(ResourceAccessDenied):
        topic_profiles.update_topic_profile(
            "large-language-models",
            {"title": "mutable template"},
            actor=user_a,
        )

    barrier = Barrier(2)

    def own_update() -> str:
        barrier.wait()
        return str(
            topic_profiles.update_topic_profile(
                profile_a["profile_id"],
                {"description": "owner update"},
                actor=user_a,
            )["description"]
        )

    def foreign_update() -> str:
        barrier.wait()
        with pytest.raises(ResourceAccessDenied):
            topic_profiles.update_topic_profile(
                profile_a["profile_id"],
                {"description": "foreign update"},
                actor=user_b,
            )
        return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        own_future = executor.submit(own_update)
        foreign_future = executor.submit(foreign_update)
        results = {own_future.result(), foreign_future.result()}
    assert results == {"owner update", "blocked"}
    assert (
        topic_profiles.get_topic_profile(
            profile_a["profile_id"],
            actor=user_a,
        )["description"]
        == "owner update"
    )
    topic_profiles.delete_topic_profile(profile_a["profile_id"], actor=user_a)
    deleted_state = owner_scoped_storage["profiles"].read_bytes()
    with pytest.raises(FileNotFoundError):
        topic_profiles.update_topic_profile(
            profile_a["profile_id"],
            {"description": "stale resurrection"},
            actor=user_a,
        )
    assert owner_scoped_storage["profiles"].read_bytes() == deleted_state


def test_file_and_db_schedules_enforce_owner_and_propagate_stored_owner(
    owner_scoped_storage: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = _actor("subject-a")
    user_b = _actor("subject-b")
    profile_a = topic_profiles.create_topic_profile(
        _profile_payload("Schedule A Topic"),
        actor=user_a,
    )
    schedule_a = topic_schedules.create_topic_schedule(
        _schedule_payload(profile_a["profile_id"], "A schedule"),
        actor=user_a,
    )
    assert schedule_a["owner_user_id"] == "subject-a"

    with pytest.raises(ResourceAccessDenied):
        topic_schedules.create_topic_schedule(
            _schedule_payload(profile_a["profile_id"], "B foreign schedule"),
            actor=user_b,
        )
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.get_topic_schedule(schedule_a["schedule_id"], actor=user_b)
    before = owner_scoped_storage["schedules"].read_bytes()
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.update_topic_schedule(
            schedule_a["schedule_id"],
            {"enabled": True},
            actor=user_b,
        )
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.delete_topic_schedule(
            schedule_a["schedule_id"],
            actor=user_b,
        )

    starts: list[dict[str, object]] = []

    def fake_start_run(
        request,
        source="api",
        owner_user_id=None,
        topic_profile_id=None,
        schedule_context=None,
    ):
        starts.append(
            {
                "owner_user_id": owner_user_id,
                "topic_profile_id": topic_profile_id,
                "source": source,
                "schedule_context": schedule_context,
            }
        )
        return {"run_id": "owned-run", "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.trigger_topic_schedule_run(
            schedule_a["schedule_id"],
            trigger="manual",
            actor=user_b,
        )
    assert starts == []
    assert owner_scoped_storage["schedules"].read_bytes() == before

    topic_schedules.trigger_topic_schedule_run(
        schedule_a["schedule_id"],
        trigger="manual",
        actor=user_a,
    )
    assert len(starts) == 1
    assert starts[0]["owner_user_id"] == "subject-a"
    assert starts[0]["topic_profile_id"] == profile_a["profile_id"]

    with pytest.raises(ValueError, match="authorization fields"):
        topic_schedules.create_topic_schedule(
            {
                **_schedule_payload(profile_a["profile_id"], "spoof"),
                "owner_user_id": "subject-b",
            },
            actor=user_a,
        )

    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    db_schedule = topic_schedules.create_topic_schedule(
        _schedule_payload(profile_a["profile_id"], "DB A schedule"),
        actor=user_a,
    )
    assert owner_scoped_storage["scheduler_db"].is_file()
    assert db_schedule["owner_user_id"] == "subject-a"
    db_before = owner_scoped_storage["scheduler_db"].read_bytes()
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.update_topic_schedule(
            db_schedule["schedule_id"],
            {"title": "DB foreign update"},
            actor=user_b,
        )
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.delete_topic_schedule(
            db_schedule["schedule_id"],
            actor=user_b,
        )
    starts_before = len(starts)
    with pytest.raises(ResourceAccessDenied):
        topic_schedules.trigger_topic_schedule_run(
            db_schedule["schedule_id"],
            trigger="manual",
            actor=user_b,
        )
    assert len(starts) == starts_before
    assert owner_scoped_storage["scheduler_db"].read_bytes() == db_before
    assert db_schedule["schedule_id"] not in {
        item["schedule_id"]
        for item in topic_schedules.list_topic_schedules(actor=user_b)["items"]
    }


def test_successful_scheduler_cycle_propagates_stored_owner(
    owner_scoped_storage: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = _actor("subject-a")
    profile = topic_profiles.create_topic_profile(
        _profile_payload("Automatic Owner Topic"),
        actor=user_a,
    )
    schedule = topic_schedules.create_topic_schedule(
        _schedule_payload(profile["profile_id"], "automatic owner"),
        actor=user_a,
    )
    records = json.loads(owner_scoped_storage["schedules"].read_text(encoding="utf-8"))
    target = next(
        item
        for item in records
        if item["schedule_id"] == schedule["schedule_id"]
    )
    target["enabled"] = True
    target["next_run_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    owner_scoped_storage["schedules"].write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    starts: list[dict[str, object]] = []

    def fake_start_run(
        request,
        source="api",
        owner_user_id=None,
        topic_profile_id=None,
        schedule_context=None,
    ):
        starts.append(
            {
                "owner_user_id": owner_user_id,
                "topic_profile_id": topic_profile_id,
                "source": source,
                "schedule_context": schedule_context,
            }
        )
        return {
            "run_id": "automatic-owned-run",
            "topic": request.topic,
            "status": "queued",
        }

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)
    result = topic_schedules.run_scheduler_cycle(
        now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        worker_id="owner-scope-worker",
    )

    assert result["executed_schedule_ids"] == [schedule["schedule_id"]]
    assert len(starts) == 1
    assert starts[0]["owner_user_id"] == "subject-a"
    assert starts[0]["topic_profile_id"] == profile["profile_id"]
    assert starts[0]["schedule_context"]["schedule_id"] == schedule["schedule_id"]


def test_oidc_legacy_unowned_schedule_blocks_once_without_dispatch(
    owner_scoped_storage: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = _actor("subject-a")
    profile_a = topic_profiles.create_topic_profile(
        _profile_payload("Legacy Schedule Topic"),
        actor=user_a,
    )
    schedule = topic_schedules.create_topic_schedule(
        _schedule_payload(profile_a["profile_id"], "legacy"),
        actor=user_a,
    )
    records = json.loads(owner_scoped_storage["schedules"].read_text(encoding="utf-8"))
    target = next(
        item
        for item in records
        if item["schedule_id"] == schedule["schedule_id"]
    )
    target["owner_user_id"] = None
    target["enabled"] = True
    target["next_run_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    owner_scoped_storage["schedules"].write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    starts: list[str] = []
    monkeypatch.setattr(
        "app.services.runs.start_run",
        lambda *_args, **_kwargs: starts.append("called"),
    )
    first = topic_schedules.run_scheduler_cycle(
        now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        worker_id="owner-scope-worker",
    )
    second = topic_schedules.run_scheduler_cycle(
        now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        worker_id="owner-scope-worker",
    )
    assert starts == []
    assert first["executed_schedule_ids"] == []
    assert second["executed_schedule_ids"] == []
    blocked = next(
        item
        for item in json.loads(
            owner_scoped_storage["schedules"].read_text(encoding="utf-8")
        )
        if item["schedule_id"] == schedule["schedule_id"]
    )
    assert blocked["enabled"] is False
    assert blocked["scheduler_state"] == "blocked"
    assert blocked["last_error"] == "schedule_owner_missing"


def test_notification_settings_events_history_and_retention_are_owner_scoped(
    owner_scoped_storage: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = _actor("subject-a")
    user_b = _actor("subject-b")
    profile_a = topic_profiles.create_topic_profile(
        _profile_payload("Notification A Topic"),
        actor=user_a,
    )
    profile_b = topic_profiles.create_topic_profile(
        _profile_payload("Notification B Topic"),
        actor=user_b,
    )
    topic_notifications.update_topic_notification_settings(
        profile_a["profile_id"],
        {
            "channels": [
                {
                    "channel_id": "email",
                    "enabled": False,
                    "destination": "a@example.test",
                }
            ]
        },
        actor=user_a,
    )
    topic_notifications.update_topic_notification_settings(
        profile_b["profile_id"],
        {
            "channels": [
                {
                    "channel_id": "email",
                    "enabled": False,
                    "destination": "b@example.test",
                }
            ]
        },
        actor=user_b,
    )
    with pytest.raises(ResourceAccessDenied):
        topic_notifications.get_topic_notification_settings(
            profile_b["profile_id"],
            actor=user_a,
        )

    # A shared system template receives separate per-user settings rows.
    topic_notifications.update_topic_notification_settings(
        "large-language-models",
        {
            "channels": [
                {
                    "channel_id": "email",
                    "enabled": False,
                    "destination": "template-a@example.test",
                }
            ]
        },
        actor=user_a,
    )
    topic_notifications.update_topic_notification_settings(
        "large-language-models",
        {
            "channels": [
                {
                    "channel_id": "email",
                    "enabled": False,
                    "destination": "template-b@example.test",
                }
            ]
        },
        actor=user_b,
    )
    settings_rows = json.loads(
        (owner_scoped_storage["notifications"] / "settings.json").read_text(
            encoding="utf-8"
        )
    )
    template_owners = {
        item["owner_user_id"]
        for item in settings_rows
        if item["profile_id"] == "large-language-models"
    }
    assert template_owners == {"subject-a", "subject-b"}

    topic_notifications.create_topic_notification(
        profile_a["profile_id"],
        rule_id="run_failed",
        event_type="run.failed",
        severity="error",
        title="A only",
        message="A event",
        actor=user_a,
    )
    topic_notifications.create_topic_notification(
        profile_b["profile_id"],
        rule_id="run_failed",
        event_type="run.failed",
        severity="error",
        title="B only",
        message="B event",
        actor=user_b,
    )
    a_history = topic_notifications.list_topic_notifications(
        profile_a["profile_id"],
        actor=user_a,
    )
    b_history = topic_notifications.list_topic_notifications(
        profile_b["profile_id"],
        actor=user_b,
    )
    assert [item["title"] for item in a_history["items"]] == ["A only"]
    assert [item["title"] for item in b_history["items"]] == ["B only"]
    assert a_history["total"] == 1
    assert b_history["total"] == 1

    network_calls = {"validate": 0, "webhook": 0, "email": 0}
    monkeypatch.setattr(
        topic_notifications.webhook_egress,
        "validate_webhook_configuration",
        lambda *_args, **_kwargs: network_calls.__setitem__(
            "validate",
            network_calls["validate"] + 1,
        ),
    )
    monkeypatch.setattr(
        topic_notifications.webhook_egress,
        "deliver_webhook",
        lambda *_args, **_kwargs: network_calls.__setitem__(
            "webhook",
            network_calls["webhook"] + 1,
        ),
    )
    monkeypatch.setattr(
        topic_notifications,
        "_send_email",
        lambda *_args, **_kwargs: network_calls.__setitem__(
            "email",
            network_calls["email"] + 1,
        ),
    )
    with pytest.raises(ResourceAccessDenied):
        topic_notifications.test_topic_notification_delivery(
            profile_b["profile_id"],
            channel_id="webhook",
            actor=user_a,
        )
    assert network_calls == {"validate": 0, "webhook": 0, "email": 0}

    monkeypatch.setattr(topic_notifications, "MAX_DELIVERY_RECORDS", 2)
    for index in range(2):
        topic_notifications._append_delivery_record(
            {
                "delivery_id": f"b-{index}",
                "profile_id": profile_b["profile_id"],
                "owner_user_id": "subject-b",
                "created_at": f"2026-07-26T00:00:0{index}Z",
            },
            actor=user_b,
        )
    for index in range(3):
        topic_notifications._append_delivery_record(
            {
                "delivery_id": f"a-{index}",
                "profile_id": profile_a["profile_id"],
                "owner_user_id": "subject-a",
                "created_at": f"2026-07-26T00:01:0{index}Z",
            },
            actor=user_a,
        )
    delivery_rows = json.loads(
        (owner_scoped_storage["notifications"] / "deliveries.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["delivery_id"] for item in delivery_rows} == {
        "a-1",
        "a-2",
        "b-0",
        "b-1",
    }


def test_dashboard_aggregates_hide_foreign_profiles_schedules_and_deliveries(
    owner_scoped_storage: dict[str, Path],
) -> None:
    user_a = _actor("subject-a")
    user_b = _actor("subject-b")
    profile_a = topic_profiles.create_topic_profile(
        _profile_payload("Dashboard A Topic"),
        actor=user_a,
    )
    profile_b = topic_profiles.create_topic_profile(
        _profile_payload("Dashboard B Topic"),
        actor=user_b,
    )
    schedule_a = topic_schedules.create_topic_schedule(
        _schedule_payload(profile_a["profile_id"], "A dashboard schedule"),
        actor=user_a,
    )
    topic_schedules.create_topic_schedule(
        _schedule_payload(profile_b["profile_id"], "B dashboard schedule"),
        actor=user_b,
    )
    topic_notifications._append_delivery_record(
        {
            "delivery_id": "delivery-a",
            "profile_id": profile_a["profile_id"],
            "owner_user_id": "subject-a",
            "channel_id": "email",
            "status": "sent",
            "created_at": "2026-07-26T00:00:00Z",
        },
        actor=user_a,
    )
    topic_notifications._append_delivery_record(
        {
            "delivery_id": "delivery-b",
            "profile_id": profile_b["profile_id"],
            "owner_user_id": "subject-b",
            "channel_id": "email",
            "status": "failed",
            "message": "foreign failure",
            "created_at": "2026-07-26T00:00:01Z",
        },
        actor=user_b,
    )

    topics_a = topic_dashboard.build_api_topics(actor=user_a)
    topic_names = {item["name"] for item in topics_a["items"]}
    assert "Dashboard A Topic" in topic_names
    assert "Dashboard B Topic" not in topic_names
    assert all("owner" not in key.lower() for item in topics_a["items"] for key in item)

    schedules_a = topic_schedules.list_topic_schedules(actor=user_a)["items"]
    schedule_ids_a = {item["schedule_id"] for item in schedules_a}
    assert schedule_a["schedule_id"] in schedule_ids_a
    assert all("B dashboard schedule" != item["title"] for item in schedules_a)
    status_a = topic_schedules.get_scheduler_runtime_status(actor=user_a)
    assert status_a["schedule_summary"]["total"] == len(schedules_a)

    health_a = topic_notifications.notification_delivery_health_summary(
        actor=user_a
    )
    assert health_a["total_recent_deliveries"] == 1
    assert health_a["status_counts"] == {"sent": 1}
    assert "foreign failure" not in json.dumps(health_a)


def test_oidc_read_surfaces_do_not_create_state_or_scheduler_db(
    owner_scoped_storage: dict[str, Path],
) -> None:
    user_a = _actor("subject-a")
    assert not owner_scoped_storage["profiles"].exists()
    assert not owner_scoped_storage["schedules"].exists()
    assert not owner_scoped_storage["scheduler_db"].exists()

    topic_profiles.list_topic_profiles(actor=user_a)
    topic_schedules.list_topic_schedules(actor=user_a)
    topic_schedules.get_scheduler_runtime_status(actor=user_a)
    topic_notifications.get_topic_notification_settings(
        "large-language-models",
        actor=user_a,
    )
    topic_dashboard.build_api_topics(actor=user_a)

    assert not owner_scoped_storage["profiles"].exists()
    assert not owner_scoped_storage["schedules"].exists()
    assert not owner_scoped_storage["scheduler_db"].exists()
    assert not owner_scoped_storage["notifications"].exists()


def test_dashboard_metadata_reads_do_not_bootstrap_empty_database(
    owner_scoped_storage: dict[str, Path],
) -> None:
    app = FastAPI()
    app.include_router(dashboard_router)
    current_user = ProductUser(
        user_id="subject-a",
        display_name="Owner A",
        email="owner-a@example.test",
        auth_source="oidc",
        is_admin=False,
    )
    app.dependency_overrides[require_product_user] = lambda: current_user
    before = {
        path.relative_to(owner_scoped_storage["root"]).as_posix()
        for path in owner_scoped_storage["root"].rglob("*")
    }

    with TestClient(app) as client:
        overview = client.get("/dashboard/overview")
        runs = client.get("/dashboard/search-runs")
        papers = client.get("/dashboard/search-papers")

    assert overview.status_code == 200, overview.text
    assert overview.json()["totals"] == {
        "runs": 0,
        "topics": 0,
        "papers": 0,
        "evidence": 0,
        "report_sections": 0,
    }
    assert runs.status_code == 200
    assert runs.json()["items"] == []
    assert papers.status_code == 200
    assert papers.json()["items"] == []
    after = {
        path.relative_to(owner_scoped_storage["root"]).as_posix()
        for path in owner_scoped_storage["root"].rglob("*")
    }
    assert after == before
    assert not (owner_scoped_storage["root"] / "metadata_index.sqlite3").exists()


def test_disabled_local_actor_keeps_unowned_legacy_access() -> None:
    local = local_resource_actor()
    assert (
        authorize_resource_access(
            actor=local,
            resource_kind=ResourceKind.USER_OWNED,
            action=ResourceAction.UPDATE,
            owner_user_id=None,
        )
        == local
    )


def test_oidc_api_masks_foreign_ids_rejects_spoof_fields_and_hides_owner(
    owner_scoped_storage: dict[str, Path],
) -> None:
    user_a_actor = _actor("subject-a")
    user_b_actor = _actor("subject-b")
    profile_a = topic_profiles.create_topic_profile(
        _profile_payload("API A Topic"),
        actor=user_a_actor,
    )
    profile_b = topic_profiles.create_topic_profile(
        _profile_payload("API B Topic"),
        actor=user_b_actor,
    )
    app = FastAPI()
    app.include_router(dashboard_router)
    current_user = ProductUser(
        user_id="subject-b",
        display_name="Same Display Name",
        email="shared@example.test",
        auth_source="oidc",
        is_admin=False,
    )
    app.dependency_overrides[require_product_user] = lambda: current_user

    with TestClient(app) as client:
        listing = client.get("/topic-profiles")
        assert listing.status_code == 200
        listing_text = listing.text
        assert profile_b["profile_id"] in listing_text
        assert profile_a["profile_id"] not in listing_text
        assert "owner_user_id" not in listing_text
        assert "subject-a" not in listing_text
        assert "subject-b" not in listing_text

        foreign = client.patch(
            f"/topic-profiles/{profile_a['profile_id']}",
            json={"title": "foreign"},
        )
        assert foreign.status_code == 404
        assert foreign.json()["detail"] == "Resource not found."
        assert profile_a["profile_id"] not in foreign.text
        assert "subject-a" not in foreign.text

        profile_spoof = client.post(
            "/topic-profiles",
            json={
                **_profile_payload("Spoofed API Topic"),
                "owner_user_id": "subject-a",
            },
        )
        schedule_spoof = client.post(
            "/topic-schedules",
            json={
                **_schedule_payload(profile_b["profile_id"], "spoof schedule"),
                "is_admin": True,
            },
        )
        notification_spoof = client.put(
            f"/topic-notifications/settings/{profile_b['profile_id']}",
            json={"in_app_enabled": True, "role": "admin"},
        )
        assert profile_spoof.status_code == 422
        assert schedule_spoof.status_code == 422
        assert notification_spoof.status_code == 422

    before = owner_scoped_storage["profiles"].read_bytes()
    app.dependency_overrides.clear()
    with TestClient(app) as anonymous_client:
        anonymous = anonymous_client.get(
            f"/topic-notifications?profile_id={profile_b['profile_id']}"
        )
        assert anonymous.status_code == 401
    assert owner_scoped_storage["profiles"].read_bytes() == before

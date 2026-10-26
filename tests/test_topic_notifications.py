from __future__ import annotations

import json
import smtplib
from pathlib import Path

from app.db import bootstrap
from app.domain import constants
from app.repositories import runs as run_repository
from app.repositories.files import write_jsonl
from app.schemas.runs import RunCreateRequest
from app.services import runs as runs_service
from app.services import topic_notifications, topic_profiles


def _patch_notification_paths(isolated_run_repo, monkeypatch) -> Path:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    notification_dir = isolated_run_repo["storage_root"].parent / "topic_notifications"
    notification_settings_path = notification_dir / "settings.json"
    notification_events_path = notification_dir / "notifications.json"
    notification_deliveries_path = notification_dir / "deliveries.json"

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(constants, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_notifications, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_LIBRARY_DIR", notification_dir)
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_SETTINGS_PATH", notification_settings_path)
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_EVENTS_PATH", notification_events_path)
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_DELIVERIES_PATH", notification_deliveries_path)
    monkeypatch.setattr(runs_service, "LEGACY_NOTES_ROOT", isolated_run_repo["storage_root"].parent / "notes")
    monkeypatch.setattr(runs_service.reporting, "ensure_compare_ready_metadata", lambda run_id: None)
    return notification_dir


def test_topic_notification_settings_api_persists_per_topic(client, isolated_run_repo, monkeypatch) -> None:
    notification_dir = _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    monkeypatch.setattr(
        topic_notifications.webhook_egress,
        "_post_json_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("saving notification settings must not invoke webhook transport")
        ),
    )

    profiles_response = client.get("/topic-profiles")
    assert profiles_response.status_code == 200, profiles_response.text
    profile_id = profiles_response.json()["items"][0]["profile_id"]

    settings_response = client.get(f"/topic-notifications/settings?profile_id={profile_id}")
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "in_app_enabled": True,
            "channels": [
                {"channel_id": "email", "enabled": True, "destination": "research@example.com"},
                {
                    "channel_id": "webhook",
                    "enabled": True,
                    "destination": "https://hooks.example.com/webhook?token=secret",
                },
            ],
            "rules": [
                {"rule_id": "report_completed", "enabled": False},
                {"rule_id": "run_failed", "enabled": True},
            ],
        },
    )
    notifications_response = client.get(f"/topic-notifications?profile_id={profile_id}")

    assert settings_response.status_code == 200, settings_response.text
    assert update_response.status_code == 200, update_response.text
    assert notifications_response.status_code == 200, notifications_response.text

    settings_body = settings_response.json()
    update_body = update_response.json()
    notifications_body = notifications_response.json()

    assert settings_body["profile_id"] == profile_id
    assert {rule["rule_id"] for rule in settings_body["rules"]} == {
        "report_completed",
        "run_failed",
        "high_impact_paper",
        "digest_available",
        "automation_task_failure",
    }
    assert next(rule for rule in update_body["rules"] if rule["rule_id"] == "report_completed")["enabled"] is False
    email_channel = next(channel for channel in settings_body["channels"] if channel["channel_id"] == "email")
    assert email_channel["available"] is False
    assert email_channel["state"] == "unavailable_missing_env"
    assert email_channel["supports_test"] is False
    webhook_channel = next(channel for channel in update_body["channels"] if channel["channel_id"] == "webhook")
    assert webhook_channel["enabled"] is True
    assert webhook_channel["destination"] == "https://hooks.example.com/webhook"
    assert webhook_channel["destination_label"] == "https://hooks.example.com/webhook"
    assert notifications_body["items"] == []

    persisted_settings = json.loads((notification_dir / "settings.json").read_text(encoding="utf-8"))
    persisted_entry = next(item for item in persisted_settings if item["profile_id"] == profile_id)
    assert persisted_entry["in_app_enabled"] is True
    assert persisted_entry["rules"]["report_completed"] is False
    assert persisted_entry["channels"]["webhook"]["enabled"] is True
    assert persisted_entry["channels"]["webhook"]["destination"] == "https://hooks.example.com/webhook?token=secret"


def test_missing_smtp_env_surfaces_explicit_unavailable_state(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.delenv("NOTIFICATION_EMAIL_SMTP_HOST", raising=False)
    monkeypatch.delenv("NOTIFICATION_EMAIL_FROM", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)

    profile_id = "large-language-models"
    settings_response = client.get(f"/topic-notifications/settings?profile_id={profile_id}")

    assert settings_response.status_code == 200, settings_response.text
    email_channel = next(channel for channel in settings_response.json()["channels"] if channel["channel_id"] == "email")
    assert email_channel["available"] is False
    assert email_channel["enabled"] is False
    assert email_channel["state"] == "unavailable_missing_env"
    assert email_channel["readiness_state"] == "unavailable_missing_env"
    assert email_channel["status_label"] == "SMTP 설정 없음"
    assert "SMTP host/from" in email_channel["unavailable_reason"]
    assert topic_notifications.notification_delivery_health_summary()["smtp_state"] == "unavailable_missing_env"


def test_partial_smtp_credentials_are_misconfigured(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_USERNAME", "agent")
    monkeypatch.delenv("NOTIFICATION_EMAIL_SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    profile_id = "large-language-models"
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={"channels": [{"channel_id": "email", "enabled": True, "destination": "research@example.test"}]},
    )
    assert update_response.status_code == 200, update_response.text
    email_channel = next(channel for channel in update_response.json()["channels"] if channel["channel_id"] == "email")
    assert email_channel["available"] is False
    assert email_channel["enabled"] is False
    assert email_channel["state"] == "misconfigured"
    assert email_channel["readiness_state"] == "invalid_config"
    assert email_channel["status_label"] == "SMTP 설정 오류"
    assert "username/password" in email_channel["unavailable_reason"]

    test_response = client.post(
        f"/topic-notifications/settings/{profile_id}/test-delivery",
        json={"channel_id": "email"},
    )
    assert test_response.status_code == 200, test_response.text
    delivery = test_response.json()["deliveries"][0]
    assert delivery["status"] == "failed"
    assert delivery["state"] == "misconfigured"
    assert delivery["attempts"] == 0
    assert delivery["attempted"] is False
    assert "username/password" in delivery["message"]


def test_finalize_run_creates_real_notification_records(isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)

    request_payload = RunCreateRequest(topic="Large Language Models").model_dump()

    completed_run = run_repository.create_run_record(
        topic="Large Language Models",
        request_payload=request_payload,
        source="api_topic_action:report",
        write_legacy=False,
    )
    completed_run["topic_profile_id"] = "large-language-models"
    completed_paths = run_repository.build_run_paths(completed_run["run_id"])
    report_path = completed_paths.reports_dir / "weekly-brief.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# Weekly brief\n", encoding="utf-8")
    triage_path = completed_paths.triage_dir / "triage.jsonl"
    write_jsonl(
        triage_path,
        [
            {
                "paper_id": "paper-high-impact",
                "title": "High Impact LLM Paper",
                "score": 74,
                "citations": 48,
            }
        ],
    )
    completed_run.setdefault("artifacts", {})["report_markdown"] = str(report_path)
    completed_run.setdefault("artifacts", {})["triage_jsonl"] = str(triage_path)
    run_repository.save_run_record(completed_run)

    runs_service._finalize_run(completed_run["run_id"], status="completed")

    failed_run = run_repository.create_run_record(
        topic="Large Language Models",
        request_payload=request_payload,
        source="topic_schedule_auto:weekly_report",
        write_legacy=False,
    )
    failed_run["topic_profile_id"] = "large-language-models"
    failed_run["schedule_context"] = {
        "schedule_id": "schedule-large-language-models-weekly_report",
        "profile_id": "large-language-models",
        "task_type": "weekly_report",
        "trigger": "automatic",
    }
    run_repository.save_run_record(failed_run)

    runs_service._finalize_run(failed_run["run_id"], status="failed", error="테스트 자동화 실패")

    notification_items = topic_notifications.list_topic_notifications("large-language-models", limit=20)["items"]
    rule_ids = {item["rule_id"] for item in notification_items}

    assert {"report_completed", "digest_available", "high_impact_paper", "run_failed", "automation_task_failure"} <= rule_ids
    assert any(item["download_url"] == f"/runs/{completed_run['run_id']}/report/download" for item in notification_items)
    assert any(item["run_id"] == failed_run["run_id"] and item["rule_id"] == "automation_task_failure" for item in notification_items)


def test_webhook_channel_configuration_generates_delivery_records(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    sent_payloads: list[dict] = []
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")

    def fake_deliver_webhook(destination: str, payload: dict):
        sent_payloads.append({"destination": destination, "payload": payload})
        return topic_notifications.webhook_egress.WebhookDeliveryResult(
            status="sent",
            attempted=True,
            status_code=202,
            error_code=None,
            message="Webhook delivered.",
        )

    monkeypatch.setattr(topic_notifications.webhook_egress, "deliver_webhook", fake_deliver_webhook)

    profile_id = "large-language-models"
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "in_app_enabled": True,
            "channels": [
                {
                    "channel_id": "webhook",
                    "enabled": True,
                    "destination": "https://hooks.example.com/research-hook?token=secret",
                }
            ],
            "rules": [{"rule_id": "report_completed", "enabled": True}],
        },
    )
    assert update_response.status_code == 200, update_response.text

    notification = topic_notifications.create_topic_notification(
        profile_id,
        rule_id="report_completed",
        event_type="report.completed",
        severity="success",
        title="보고서 생성 완료",
        message="테스트 보고서가 준비되었습니다.",
        run_id="run-webhook-test",
        download_url="/runs/run-webhook-test/report/download",
        dedupe_key="test-webhook-delivery",
    )
    deliveries_response = client.get(f"/topic-notifications/deliveries?profile_id={profile_id}")

    assert notification is not None
    assert deliveries_response.status_code == 200, deliveries_response.text
    deliveries = deliveries_response.json()["items"]
    assert sent_payloads
    assert sent_payloads[0]["destination"] == "https://hooks.example.com/research-hook?token=secret"
    assert sent_payloads[0]["payload"]["eventType"] == "report.completed"
    assert deliveries[0]["channel_id"] == "webhook"
    assert deliveries[0]["status"] == "sent"
    assert deliveries[0]["status_code"] == 202
    assert deliveries[0]["destination_label"] == "https://hooks.example.com/research-hook"

    test_response = client.post(
        f"/topic-notifications/settings/{profile_id}/test-delivery",
        json={"channel_id": "webhook"},
    )
    assert test_response.status_code == 200, test_response.text
    assert test_response.json()["deliveries"][0]["status"] == "sent"
    assert len(sent_payloads) == 2


def test_default_disabled_webhook_test_delivery_never_calls_transport(
    client,
    isolated_run_repo,
    monkeypatch,
) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_ENABLED", raising=False)
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(
        topic_notifications.webhook_egress,
        "_post_json_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled webhook delivery must not invoke transport")
        ),
    )

    response = client.post(
        "/topic-notifications/settings/large-language-models/test-delivery",
        json={"channel_id": "webhook"},
    )

    assert response.status_code == 200, response.text
    delivery = response.json()["deliveries"][0]
    assert delivery["status"] == "disabled"
    assert delivery["state"] == "disabled"
    assert delivery["error_code"] == "webhook_disabled"
    assert delivery["attempts"] == 0
    assert delivery["attempted"] is False


def test_invalid_webhook_configuration_is_redacted_4xx_and_extra_request_controls_are_rejected(
    client,
    isolated_run_repo,
    monkeypatch,
) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    secret = "must-not-escape"

    invalid_response = client.put(
        "/topic-notifications/settings/large-language-models",
        json={
            "channels": [
                {
                    "channel_id": "webhook",
                    "enabled": True,
                    "destination": f"http://hooks.example.com/notify?token={secret}",
                }
            ]
        },
    )
    extra_controls_response = client.put(
        "/topic-notifications/settings/large-language-models",
        json={
            "channels": [
                {
                    "channel_id": "webhook",
                    "enabled": True,
                    "destination": "https://hooks.example.com/notify",
                    "method": "DELETE",
                    "headers": {"Authorization": "untrusted"},
                }
            ]
        },
    )

    assert invalid_response.status_code == 400, invalid_response.text
    assert invalid_response.json()["detail"].startswith("webhook_https_required:")
    assert secret not in invalid_response.text
    assert extra_controls_response.status_code == 422, extra_controls_response.text


def test_manual_and_scheduler_delivery_paths_use_the_same_webhook_gateway(
    client,
    isolated_run_repo,
    monkeypatch,
) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    gateway_calls: list[dict] = []

    def fake_gateway(destination: str, payload: dict):
        gateway_calls.append({"destination": destination, "payload": payload})
        return topic_notifications.webhook_egress.WebhookDeliveryResult(
            status="sent",
            attempted=True,
            status_code=204,
            error_code=None,
            message="Webhook delivered.",
        )

    monkeypatch.setattr(topic_notifications.webhook_egress, "deliver_webhook", fake_gateway)
    update_response = client.put(
        "/topic-notifications/settings/large-language-models",
        json={
            "channels": [
                {
                    "channel_id": "webhook",
                    "enabled": True,
                    "destination": "https://hooks.example.com/notify?token=secret",
                }
            ]
        },
    )
    assert update_response.status_code == 200, update_response.text

    topic_notifications.emit_schedule_failure_notification(
        {
            "schedule_id": "schedule-security-test",
            "profile_id": "large-language-models",
            "title": "Security schedule",
        },
        error="synthetic failure",
    )
    topic_notifications.emit_notifications_for_run(
        {
            "run_id": "run-security-test",
            "status": "failed",
            "topic_profile_id": "large-language-models",
            "topic": "Large Language Models",
            "error": "synthetic run failure",
        }
    )
    manual_response = client.post(
        "/topic-notifications/settings/large-language-models/test-delivery",
        json={"channel_id": "webhook"},
    )

    assert manual_response.status_code == 200, manual_response.text
    assert len(gateway_calls) == 3
    assert {call["payload"]["eventType"] for call in gateway_calls} == {
        "automation.failed",
        "run.failed",
        "notification.test_delivery",
    }
    assert all(call["destination"] == "https://hooks.example.com/notify?token=secret" for call in gateway_calls)


def test_email_channel_configuration_generates_delivery_records(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    sent_messages: list[dict[str, str]] = []

    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PORT", "2525")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "false")

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: int):
            self.host = host
            self.port = port
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self) -> None:
            raise AssertionError("TLS should be disabled in this test")

        def login(self, username: str, password: str) -> None:
            raise AssertionError("login should not be called without credentials")

        def send_message(self, message) -> None:
            sent_messages.append(
                {
                    "host": self.host,
                    "port": str(self.port),
                    "to": message["To"],
                    "from": message["From"],
                    "subject": message["Subject"],
                    "body": message.get_content(),
                }
            )

    monkeypatch.setattr(topic_notifications.smtplib, "SMTP", FakeSmtp)

    profile_id = "large-language-models"
    settings_response = client.get(f"/topic-notifications/settings?profile_id={profile_id}")
    assert settings_response.status_code == 200, settings_response.text
    email_channel = next(channel for channel in settings_response.json()["channels"] if channel["channel_id"] == "email")
    assert email_channel["available"] is True
    assert email_channel["state"] == "available"
    assert email_channel["readiness_state"] == "available"
    assert email_channel["supports_test"] is True

    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "in_app_enabled": True,
            "channels": [{"channel_id": "email", "enabled": True, "destination": "research@example.test"}],
            "rules": [{"rule_id": "report_completed", "enabled": True}],
        },
    )
    assert update_response.status_code == 200, update_response.text
    updated_email_channel = next(channel for channel in update_response.json()["channels"] if channel["channel_id"] == "email")
    assert updated_email_channel["enabled"] is True
    assert updated_email_channel["destination_label"] == "re***@example.test"

    notification = topic_notifications.create_topic_notification(
        profile_id,
        rule_id="report_completed",
        event_type="report.completed",
        severity="success",
        title="보고서 생성 완료",
        message="테스트 이메일 보고서가 준비되었습니다.",
        run_id="run-email-test",
        download_url="/runs/run-email-test/report/download",
        dedupe_key="test-email-delivery",
    )
    deliveries_response = client.get(f"/topic-notifications/deliveries?profile_id={profile_id}")

    assert notification is not None
    assert sent_messages
    assert sent_messages[0]["host"] == "smtp.example.test"
    assert sent_messages[0]["port"] == "2525"
    assert sent_messages[0]["to"] == "research@example.test"
    assert "테스트 이메일 보고서가 준비되었습니다." in sent_messages[0]["body"]
    deliveries = deliveries_response.json()["items"]
    assert deliveries[0]["channel_id"] == "email"
    assert deliveries[0]["status"] == "sent"
    assert deliveries[0]["state"] == "available"
    assert deliveries[0]["attempts"] == 1
    assert deliveries[0]["attempted"] is True
    assert deliveries[0]["destination_label"] == "re***@example.test"

    test_response = client.post(
        f"/topic-notifications/settings/{profile_id}/test-delivery",
        json={"channel_id": "email"},
    )
    assert test_response.status_code == 200, test_response.text
    assert test_response.json()["deliveries"][0]["status"] == "sent"
    assert test_response.json()["deliveries"][0]["state"] == "available"


def test_email_channel_surfaces_delivery_failed_state_after_attempt(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)

    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "false")

    class FailingSmtp:
        def __init__(self, host: str, port: int, timeout: int):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send_message(self, message) -> None:
            raise RuntimeError("mailbox unavailable")

    monkeypatch.setattr(topic_notifications.smtplib, "SMTP", FailingSmtp)

    profile_id = "large-language-models"
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "channels": [{"channel_id": "email", "enabled": True, "destination": "research@example.test"}],
            "rules": [{"rule_id": "report_completed", "enabled": True}],
        },
    )
    assert update_response.status_code == 200, update_response.text

    notification = topic_notifications.create_topic_notification(
        profile_id,
        rule_id="report_completed",
        event_type="report.completed",
        severity="success",
        title="보고서 생성 완료",
        message="테스트 이메일 보고서가 준비되었습니다.",
        run_id="run-email-failure-test",
        download_url="/runs/run-email-failure-test/report/download",
        dedupe_key="test-email-failed-delivery-state",
    )
    assert notification is not None

    deliveries_response = client.get(f"/topic-notifications/deliveries?profile_id={profile_id}")
    assert deliveries_response.status_code == 200, deliveries_response.text
    delivery = deliveries_response.json()["items"][0]
    assert delivery["channel_id"] == "email"
    assert delivery["status"] == "failed"
    assert delivery["state"] == "delivery_failed"
    assert "mailbox unavailable" in delivery["message"]

    settings_response = client.get(f"/topic-notifications/settings?profile_id={profile_id}")
    email_channel = next(channel for channel in settings_response.json()["channels"] if channel["channel_id"] == "email")
    assert email_channel["available"] is True
    assert email_channel["enabled"] is True
    assert email_channel["state"] == "delivery_failed"
    assert email_channel["status_label"] == "최근 전송 실패"


def test_invalid_smtp_configuration_records_failed_email_delivery(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PORT", "not-a-number")

    profile_id = "large-language-models"
    settings_response = client.get(f"/topic-notifications/settings?profile_id={profile_id}")
    assert settings_response.status_code == 200, settings_response.text
    email_channel = next(channel for channel in settings_response.json()["channels"] if channel["channel_id"] == "email")
    assert email_channel["available"] is False
    assert email_channel["state"] == "misconfigured"
    assert "SMTP port" in email_channel["unavailable_reason"]

    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={"channels": [{"channel_id": "email", "enabled": True, "destination": "research@example.test"}]},
    )
    assert update_response.status_code == 200, update_response.text

    test_response = client.post(
        f"/topic-notifications/settings/{profile_id}/test-delivery",
        json={"channel_id": "email"},
    )
    assert test_response.status_code == 200, test_response.text
    delivery = test_response.json()["deliveries"][0]
    assert delivery["status"] == "failed"
    assert delivery["state"] == "misconfigured"
    assert delivery["attempts"] == 0
    assert delivery["attempted"] is False
    assert "SMTP port" in delivery["message"]


def test_smtp_readiness_reports_invalid_config_details(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "not-an-email")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "true")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_SSL", "true")

    settings_response = client.get("/settings/status")

    assert settings_response.status_code == 200, settings_response.text
    delivery_status = settings_response.json()["ops"]["notification_delivery"]
    assert delivery_status["smtp_state"] == "misconfigured"
    assert delivery_status["smtp_readiness_state"] == "invalid_config"
    assert delivery_status["smtp_configured"] is False
    assert "NOTIFICATION_EMAIL_FROM" in delivery_status["smtp_invalid_config"]
    assert "NOTIFICATION_EMAIL_SMTP_TLS/NOTIFICATION_EMAIL_SMTP_SSL" in delivery_status["smtp_invalid_config"]
    assert delivery_status["smtp_tls_mode"] == "invalid"


def test_email_connection_failure_records_connection_failed_state(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PORT", "2525")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "false")

    class ConnectionFailingSmtp:
        def __init__(self, host: str, port: int, timeout: int):
            raise OSError("connection refused")

    monkeypatch.setattr(topic_notifications.smtplib, "SMTP", ConnectionFailingSmtp)

    profile_id = "large-language-models"
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "channels": [{"channel_id": "email", "enabled": True, "destination": "research@example.test"}],
            "rules": [{"rule_id": "report_completed", "enabled": True}],
        },
    )
    assert update_response.status_code == 200, update_response.text

    notification = topic_notifications.create_topic_notification(
        profile_id,
        rule_id="report_completed",
        event_type="report.completed",
        severity="success",
        title="보고서 생성 완료",
        message="연결 실패 분류 테스트입니다.",
        run_id="run-email-connection-failure-test",
        download_url="/runs/run-email-connection-failure-test/report/download",
        dedupe_key="test-email-connection-failed-state",
    )

    assert notification is not None
    deliveries_response = client.get(f"/topic-notifications/deliveries?profile_id={profile_id}")
    delivery = deliveries_response.json()["items"][0]
    assert delivery["status"] == "failed"
    assert delivery["state"] == "connection_failed"
    assert delivery["attempts"] == 1
    assert delivery["attempted"] is True
    assert "connection refused" in delivery["message"]

    health = topic_notifications.notification_delivery_health_summary()
    assert health["smtp_state"] == "available"
    assert health["smtp_readiness_state"] == "connection_failed"


def test_email_send_smtp_exception_records_delivery_failed_state(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "false")

    class DeliveryFailingSmtp:
        def __init__(self, host: str, port: int, timeout: int):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send_message(self, message) -> None:
            raise smtplib.SMTPException("mailbox rejected")

    monkeypatch.setattr(topic_notifications.smtplib, "SMTP", DeliveryFailingSmtp)

    profile_id = "large-language-models"
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "channels": [{"channel_id": "email", "enabled": True, "destination": "research@example.test"}],
            "rules": [{"rule_id": "report_completed", "enabled": True}],
        },
    )
    assert update_response.status_code == 200, update_response.text

    topic_notifications.create_topic_notification(
        profile_id,
        rule_id="report_completed",
        event_type="report.completed",
        severity="success",
        title="보고서 생성 완료",
        message="전송 실패 분류 테스트입니다.",
        run_id="run-email-delivery-failure-test",
        download_url="/runs/run-email-delivery-failure-test/report/download",
        dedupe_key="test-email-delivery-failed-state",
    )

    delivery = client.get(f"/topic-notifications/deliveries?profile_id={profile_id}").json()["items"][0]
    assert delivery["status"] == "failed"
    assert delivery["state"] == "delivery_failed"
    assert "mailbox rejected" in delivery["message"]


def test_weekly_digest_run_generates_email_delivery(client, isolated_run_repo, monkeypatch) -> None:
    _patch_notification_paths(isolated_run_repo, monkeypatch)
    sent_subjects: list[str] = []

    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "agent@example.test")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "false")

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: int):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send_message(self, message) -> None:
            sent_subjects.append(message["Subject"])

    monkeypatch.setattr(topic_notifications.smtplib, "SMTP", FakeSmtp)

    profile_id = "large-language-models"
    update_response = client.put(
        f"/topic-notifications/settings/{profile_id}",
        json={
            "channels": [{"channel_id": "email", "enabled": True, "destination": "digest@example.test"}],
            "rules": [{"rule_id": "digest_available", "enabled": True}],
        },
    )
    assert update_response.status_code == 200, update_response.text

    request_payload = RunCreateRequest(topic="Large Language Models").model_dump()
    digest_run = run_repository.create_run_record(
        topic="Large Language Models",
        request_payload=request_payload,
        source="topic_schedule_auto:weekly_report",
        write_legacy=False,
    )
    digest_run["topic_profile_id"] = profile_id
    digest_run["schedule_context"] = {
        "schedule_id": "schedule-large-language-models-weekly_report",
        "profile_id": profile_id,
        "task_type": "weekly_report",
        "trigger": "automatic",
    }
    digest_paths = run_repository.build_run_paths(digest_run["run_id"])
    report_path = digest_paths.reports_dir / "weekly-digest.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# Weekly digest\n", encoding="utf-8")
    digest_run.setdefault("artifacts", {})["report_markdown"] = str(report_path)
    run_repository.save_run_record(digest_run)

    runs_service._finalize_run(digest_run["run_id"], status="completed")

    notifications = topic_notifications.list_topic_notifications(profile_id, limit=20)["items"]
    deliveries = topic_notifications.list_topic_notification_deliveries(profile_id, limit=20)["items"]

    assert any(item["rule_id"] == "digest_available" and item["event_type"] == "digest.available" for item in notifications)
    assert any("주간 digest" in subject for subject in sent_subjects)
    assert any(
        item["channel_id"] == "email"
        and item["event_type"] == "digest.available"
        and item["status"] == "sent"
        and item["state"] == "available"
        for item in deliveries
    )

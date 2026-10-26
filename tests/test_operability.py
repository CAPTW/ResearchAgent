from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.config import get_auth_settings
from app.jobs import topic_schedule_worker
from app.ops import runtime
from app.ops.logging import JsonLogFormatter
from app.services import topic_notifications, topic_schedules


def test_readiness_health_exposes_operability_without_secrets(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] in {"ready", "degraded"}
    assert payload["deployment"]["single_service_deployable"] is True
    assert payload["deployment"]["distributed_scheduler_supported"] is False
    assert payload["deployment"]["scheduler_architecture"] == "single_node_file_backed"
    assert payload["deployment"]["scheduler_runtime_mode"] == "single_node_file_backed"
    assert payload["deployment"]["scheduler_storage_backend"] == "file_json"
    assert payload["deployment"]["config_health"]["contract_version"] == "deployment-env-v1"
    assert payload["deployment"]["config_health"]["storage"]["artifact_storage_root"] == payload["deployment"]["storage"]["runs_root"]
    # Readiness is observation-only: it must not create a temporary probe file to
    # prove writability, so write capability is reported as "not probed" (None)
    # rather than asserted. Explicit startup/operator preflight still probes.
    assert payload["deployment"]["config_health"]["storage"]["atomic_replace_supported"] is None
    assert payload["deployment"]["config_health"]["storage"]["windows_path_safe"] is True
    assert payload["deployment"]["config_health"]["storage"]["layout"]["runs"] == payload["deployment"]["storage"]["runs_root"]
    report_versions_path = payload["deployment"]["config_health"]["storage"]["layout"]["report_versions"].replace("\\", "/")
    assert report_versions_path.endswith("reports/versions")
    assert payload["deployment"]["storage"]["checks"]["data_root"]["atomic_replace_supported"] is None
    assert payload["deployment"]["storage"]["checks"]["runs_root"]["writable"] is None
    assert payload["deployment"]["storage"]["checks"]["runs_root"]["write_probed"] is False
    assert payload["deployment"]["config_health"]["frontend"]["same_origin_api"] is True
    assert payload["deployment"]["config_health"]["sqlite"]["scheduler_db_path"].endswith("scheduler.sqlite3")
    assert payload["deployment"]["config_health"]["sqlite"]["atomic_replace_supported"] is None
    assert payload["deployment"]["sqlite"]["paths"]["scheduler_db"]["path"].endswith("scheduler.sqlite3")
    assert "release_validation" in payload["deployment"]
    assert payload["deployment"]["config_health"]["release_validation"]["status"] in {"unknown", "passed", "failed", "unreadable"}
    assert "expected_warnings" in payload["deployment"]["config_health"]["release_validation"]
    assert "not DB/queue-backed distributed scheduling" in payload["deployment"]["scheduler_architecture_note"]
    assert payload["scheduler"]["runtime_mode"] == "single_node_file_backed"
    assert payload["scheduler"]["distributed_ready"] is False
    assert payload["scheduler"]["multi_node_dispatch_safe"] is False
    assert payload["deployment"]["storage"]["writable"] is None
    assert payload["notification_delivery"]["smtp_available"] is False
    assert "OPENAI_API_KEY" not in response.text
    assert "change-me-for-shared-deployments" not in response.text


def test_settings_status_includes_deployment_readiness(client: TestClient) -> None:
    response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] in {"ready", "degraded"}
    assert payload["deployment"]["env"]["AUTH_MODE"] == "disabled"
    assert payload["deployment"]["env"]["NOTIFICATION_EMAIL_SMTP_HOST"] in {"missing", "blank", "set"}
    assert payload["deployment"]["config_health"]["auth"]["session_secret"] in {"missing", "blank", "set"}
    assert payload["deployment"]["config_health"]["notification_delivery"]["smtp_state"] in {
        "available",
        "unavailable_missing_env",
        "misconfigured",
        "failed_delivery",
    }
    layout = payload["deployment"]["config_health"]["storage"]["layout"]
    assert layout["topic_profiles"].replace("\\", "/").endswith("topic_profiles")
    assert layout["topic_notifications"].replace("\\", "/").endswith("topic_notifications")
    assert layout["topic_analytics"].replace("\\", "/").endswith("topic_analytics")
    assert layout["auth_sessions"].replace("\\", "/").endswith("auth/product_sessions.json")
    assert layout["run_events"].replace("\\", "/").endswith("runs/<run_id>/events.jsonl")
    assert layout["paper_review_actions"].replace("\\", "/").endswith("runs/<run_id>/paper_review_actions.json")
    assert layout["paper_reviews"].replace("\\", "/").endswith("runs/<run_id>/reviews")
    assert layout["validation_output_artifacts"].replace("\\", "/").endswith("output/release-validation")
    assert layout["package_output_artifacts"].replace("\\", "/").endswith("output/packages")
    release_validation = payload["deployment"]["release_validation"]
    assert release_validation["status"] in {"unknown", "passed", "failed", "unreadable"}
    assert release_validation["step_count"] >= 0
    assert release_validation["failed_step_count"] >= 0
    assert "chunk-size" in " ".join(payload["deployment"]["config_health"]["release_validation"]["expected_warnings"])
    assert payload["deployment"]["config_health"]["contract"]["required_for_shared_oidc"]
    assert payload["deployment"]["distributed_scheduler_supported"] is False
    assert payload["deployment"]["scheduler_runtime_mode"] == "single_node_file_backed"
    assert payload["ops"]["scheduler"]["runtime_mode"] == "single_node_file_backed"
    assert payload["ops"]["scheduler"]["dispatch_safety"]["dispatch_leases"] is True
    assert "scheduler" in payload["ops"]
    assert "notification_delivery" in payload["ops"]


def test_settings_status_env_contract_names_exact_activation_variables(client: TestClient) -> None:
    response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    contract = response.json()["deployment"]["config_health"]["contract"]

    assert "SESSION_SECRET" in contract["required_for_shared_oidc"]
    assert "OIDC_CLIENT_SECRET" in contract["required_for_shared_oidc"]
    assert "NOTIFICATION_EMAIL_SMTP_HOST" in contract["required_for_smtp"]
    assert "NOTIFICATION_EMAIL_FROM" in contract["required_for_smtp"]
    assert "SCHEDULER_MODE=db_backed requires a writable shared SQLite scheduler DB path" in contract["unsafe_combinations"]
    assert "SCHEDULER_MODE=queue_backed requires a writable shared SQLite scheduler DB path" in contract["unsafe_combinations"]


def test_settings_status_reports_latest_release_validation_summary(app_factory, monkeypatch, tmp_path) -> None:
    summary_dir = tmp_path / "output" / "release-validation" / "ci-pass"
    summary_dir.mkdir(parents=True)
    (summary_dir / "validation-summary.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "createdAt": "2026-05-02T07:12:35Z",
                "logDir": str(summary_dir),
                "withPlaywright": True,
                "screenshotDir": str(tmp_path / "output" / "playwright" / "truth-pass"),
                "expectedWarnings": ["Vite may emit the existing chunk-size warning during production build."],
                "steps": [
                    {"name": "backend-full-tests", "status": "passed", "exitCode": 0},
                    {"name": "playwright-truth-pass", "status": "passed", "exitCode": 0},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime.constants, "PROJECT_ROOT", tmp_path)

    with TestClient(app_factory()) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    release_validation = response.json()["deployment"]["release_validation"]
    assert release_validation["available"] is True
    assert release_validation["status"] == "passed"
    assert release_validation["with_playwright"] is True
    assert release_validation["step_count"] == 2
    assert release_validation["failed_step_count"] == 0
    assert release_validation["latest_summary_path"].endswith("validation-summary.json")


def test_settings_status_storage_probe_is_order_independent_with_stale_probe_artifacts(
    app_factory,
    monkeypatch,
    tmp_path,
) -> None:
    data_root = tmp_path / "storage"
    runs_root = data_root / "runs"
    runs_root.mkdir(parents=True)
    (data_root / ".xrml-storage-probe").mkdir()
    (runs_root / ".xrml-storage-probe").mkdir()
    monkeypatch.setattr(runtime.constants, "STORAGE_ROOT", runs_root)

    with TestClient(app_factory(SCHEDULER_MODE="queue_backed", SCHEDULER_DISTRIBUTED="true")) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] == "degraded"
    assert payload["deployment"]["storage"]["writable"] is True
    assert payload["deployment"]["errors"] == []


def test_strict_env_validation_blocks_shared_disabled_auth(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="production",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="disabled",
    )
    with pytest.raises(RuntimeError, match="Shared deployment profiles must use AUTH_MODE=oidc"):
        with TestClient(app):
            pass


def test_explicit_shared_deployment_blocks_disabled_auth_even_in_custom_env(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="prod-us",
        XRML_SHARED_DEPLOYMENT="true",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="disabled",
    )
    with pytest.raises(RuntimeError, match="Shared deployment profiles must use AUTH_MODE=oidc"):
        with TestClient(app):
            pass


def test_strict_env_validation_blocks_shared_deployment_without_durable_data_dir(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="production",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="oidc",
        SESSION_SECRET="production-session-secret-with-enough-entropy",
        APP_BASE_URL="https://research.example.com",
        OIDC_ISSUER="https://issuer.example.com",
        OIDC_CLIENT_ID="client-id",
        OIDC_CLIENT_SECRET="client-secret",
        XRML_DATA_DIR="",
    )

    with pytest.raises(RuntimeError, match="XRML_DATA_DIR"):
        with TestClient(app):
            pass


def test_readiness_reports_auth_safety_and_oidc_configuration_state(app_factory) -> None:
    with TestClient(app_factory(XRML_SHARED_DEPLOYMENT="true", AUTH_MODE="disabled", XRML_DATA_DIR="")) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] == "not_ready"
    assert payload["deployment"]["shared_deployment"] is True
    assert payload["deployment"]["auth"]["mode"] == "disabled"
    assert payload["deployment"]["auth"]["deployment_safe"] is False
    assert payload["deployment"]["auth"]["oidc_configured"] is False
    assert payload["deployment"]["auth"]["callback_url"] == "http://127.0.0.1:8000/auth/callback"
    assert payload["deployment"]["config_health"]["storage"]["shared_requires_xrml_data_dir"] is True
    assert any("XRML_DATA_DIR" in item for item in payload["deployment"]["errors"])
    assert any("AUTH_MODE=disabled" in item for item in payload["deployment"]["auth"]["warnings"])


def test_settings_status_reports_oidc_missing_env_without_uncaught_500(app_factory) -> None:
    with TestClient(app_factory(AUTH_MODE="oidc", SESSION_SECRET="production-session-secret-with-enough-entropy")) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    auth = response.json()["deployment"]["auth"]
    assert auth["mode"] == "oidc"
    assert auth["oidc_readiness_status"] == "missing_env"
    assert auth["oidc_configured"] is False
    assert "OIDC_ISSUER" in auth["oidc_missing_env"]
    assert "OIDC_CLIENT_ID" in auth["oidc_missing_env"]
    assert "OIDC_CLIENT_SECRET" in auth["oidc_missing_env"]
    assert "client-secret" not in response.text


def test_settings_status_reports_oidc_invalid_config_without_secret_leak(app_factory) -> None:
    secret = "super-secret-client-value"
    with TestClient(
        app_factory(
            AUTH_MODE="oidc",
            SESSION_SECRET="production-session-secret-with-enough-entropy",
            APP_BASE_URL="not-a-url",
            OIDC_ISSUER="not-a-url",
            OIDC_CLIENT_ID="client-id",
            OIDC_CLIENT_SECRET=secret,
        )
    ) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    auth = response.json()["deployment"]["auth"]
    assert auth["oidc_readiness_status"] == "invalid_config"
    assert "APP_BASE_URL" in auth["oidc_invalid_config"]
    assert "OIDC_ISSUER" in auth["oidc_invalid_config"]
    assert secret not in response.text


def test_settings_status_reports_disabled_auth_readiness_by_profile(app_factory) -> None:
    with TestClient(app_factory()) as client:
        local_response = client.get("/settings/status")
    with TestClient(app_factory(XRML_SHARED_DEPLOYMENT="true", AUTH_MODE="disabled")) as client:
        shared_response = client.get("/settings/status")

    assert local_response.status_code == 200, local_response.text
    assert shared_response.status_code == 200, shared_response.text
    assert local_response.json()["deployment"]["auth"]["oidc_readiness_status"] == "disabled_dev_mode"
    assert shared_response.json()["deployment"]["auth"]["oidc_readiness_status"] == "unsafe_production_disabled"


def test_settings_status_reports_oidc_configured_with_scope_and_audience(app_factory) -> None:
    with TestClient(
        app_factory(
            AUTH_MODE="oidc",
            SESSION_SECRET="production-session-secret-with-enough-entropy",
            APP_BASE_URL="https://research.example.com",
            OIDC_ISSUER="https://issuer.example.com",
            OIDC_CLIENT_ID="client-id",
            OIDC_CLIENT_SECRET="client-secret",
            OIDC_AUDIENCE="https://issuer.example.com/xrml-api",
            OIDC_SCOPES="openid profile email offline_access",
        )
    ) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    auth = response.json()["deployment"]["auth"]
    assert auth["oidc_readiness_status"] == "configured_not_verified"
    assert auth["oidc_configured"] is True
    assert auth["oidc_audience_configured"] is True
    assert auth["oidc_scopes"] == ["openid", "profile", "email", "offline_access"]


def test_strict_env_validation_blocks_unsafe_oidc_production_config(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="production",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="oidc",
        SESSION_SECRET="change-me-for-shared-deployments",
        APP_BASE_URL="http://research.example.com",
        OIDC_ISSUER="https://issuer.example.com",
        OIDC_CLIENT_ID="client-id",
        OIDC_CLIENT_SECRET="client-secret",
    )

    with pytest.raises(RuntimeError, match="HTTPS APP_BASE_URL"):
        with TestClient(app):
            pass


def test_oidc_config_validation_rejects_invalid_issuer_and_weak_secret(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="production",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="oidc",
        SESSION_SECRET="production-session-secret-with-enough-entropy",
        APP_BASE_URL="https://research.example.com",
        OIDC_ISSUER="not-a-url",
        OIDC_CLIENT_ID="client-id",
        OIDC_CLIENT_SECRET="client-secret",
    )

    with pytest.raises(RuntimeError, match="OIDC_ISSUER must be an absolute http\\(s\\) URL"):
        with TestClient(app):
            pass


def test_settings_status_surfaces_auth_safety_indicators(app_factory) -> None:
    with TestClient(
        app_factory(
            AUTH_MODE="oidc",
            SESSION_SECRET="production-session-secret-with-enough-entropy",
            APP_BASE_URL="https://research.example.com",
            OIDC_ISSUER="https://issuer.example.com",
            OIDC_CLIENT_ID="client-id",
            OIDC_CLIENT_SECRET="client-secret",
            OIDC_AUDIENCE="https://issuer.example.com/xrml-api",
        )
    ) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["auth"]["mode"] == "oidc"
    assert payload["deployment"]["auth"]["oidc_configured"] is True
    assert payload["deployment"]["auth"]["deployment_safe"] is True
    assert payload["deployment"]["auth"]["callback_url"] == "https://research.example.com/auth/callback"
    assert payload["deployment"]["auth"]["mcp_auth_required"] is True
    assert payload["deployment"]["auth"]["mcp_chatgpt_ready"] is True


def test_strict_env_validation_blocks_invalid_scheduler_mode(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="production",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="oidc",
        SESSION_SECRET="production-session-secret-with-enough-entropy",
        APP_BASE_URL="https://research.example.com",
        OIDC_ISSUER="https://issuer.example.com",
        OIDC_CLIENT_ID="client-id",
        OIDC_CLIENT_SECRET="client-secret",
        TOPIC_SCHEDULER_MODE="cluster",
    )

    with pytest.raises(RuntimeError, match="Invalid TOPIC_SCHEDULER_MODE"):
        with TestClient(app):
            pass


def test_readiness_reports_invalid_scheduler_mode_without_fake_distributed_state(app_factory) -> None:
    with TestClient(app_factory(TOPIC_SCHEDULER_MODE="cluster")) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] == "degraded"
    assert payload["deployment"]["distributed_scheduler_supported"] is False
    assert payload["deployment"]["env"]["TOPIC_SCHEDULER_MODE_RAW"] == "cluster"
    assert payload["deployment"]["env"]["TOPIC_SCHEDULER_MODE_VALID"] is False
    assert payload["scheduler"]["configured_mode_raw"] == "cluster"
    assert payload["scheduler"]["configured_mode_valid"] is False
    assert payload["scheduler"]["runtime_mode_status"] == "degraded"
    assert any("Invalid TOPIC_SCHEDULER_MODE" in warning for warning in payload["deployment"]["warnings"])


def test_strict_env_validation_blocks_invalid_scheduler_runtime_mode(app_factory) -> None:
    app = app_factory(
        XRML_ENVIRONMENT="production",
        XRML_STRICT_ENV_VALIDATION="true",
        AUTH_MODE="oidc",
        SESSION_SECRET="production-session-secret-with-enough-entropy",
        APP_BASE_URL="https://research.example.com",
        OIDC_ISSUER="https://issuer.example.com",
        OIDC_CLIENT_ID="client-id",
        OIDC_CLIENT_SECRET="client-secret",
        SCHEDULER_MODE="cluster",
    )

    with pytest.raises(RuntimeError, match="Invalid SCHEDULER_MODE"):
        with TestClient(app):
            pass


def test_readiness_reports_invalid_scheduler_runtime_mode_without_fake_distributed_state(app_factory) -> None:
    with TestClient(app_factory(SCHEDULER_MODE="cluster")) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] == "degraded"
    assert payload["deployment"]["distributed_scheduler_supported"] is False
    assert payload["deployment"]["env"]["SCHEDULER_MODE_RAW"] == "cluster"
    assert payload["deployment"]["env"]["SCHEDULER_MODE_VALID"] is False
    assert payload["scheduler"]["runtime_mode"] == "legacy_file_read_only"
    assert payload["scheduler"]["runtime_mode_status"] == "degraded"
    assert payload["scheduler"]["backend"]["configured_runtime_mode_valid"] is False
    assert any("Invalid SCHEDULER_MODE" in warning for warning in payload["deployment"]["warnings"])


def test_readiness_reports_queue_scheduler_without_fake_distributed_state(app_factory) -> None:
    with TestClient(app_factory(SCHEDULER_MODE="queue_backed")) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] == "degraded"
    assert payload["deployment"]["distributed_scheduler_supported"] is False
    assert payload["deployment"]["env"]["SCHEDULER_MODE_RAW"] == "queue_backed"
    assert payload["deployment"]["env"]["SCHEDULER_MODE_VALID"] is True
    assert payload["ops"]["scheduler"]["runtime_mode"] == "queue_backed"
    assert payload["ops"]["scheduler"]["storage_backend"] == "sqlite_queue"
    assert payload["ops"]["scheduler"]["backend"]["available"] is True
    assert payload["ops"]["scheduler"]["backend"]["capabilities"]["durable_queue"] is True
    assert payload["ops"]["scheduler"]["dispatch_safety"]["durable_queue"] is True


def test_readiness_reports_requested_distributed_scheduler_as_unsupported(app_factory) -> None:
    with TestClient(app_factory(SCHEDULER_MODE="queue_backed", SCHEDULER_DISTRIBUTED="true")) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deployment"]["readiness"] == "degraded"
    assert payload["deployment"]["distributed_scheduler_supported"] is False
    assert payload["deployment"]["scheduler_distributed_requested"] is True
    assert payload["deployment"]["scheduler_distributed_support_status"] == "unsupported"
    assert payload["ops"]["scheduler"]["distributed_requested"] is True
    assert payload["ops"]["scheduler"]["distributed_support_status"] == "unsupported"
    assert payload["ops"]["scheduler"]["multi_node_dispatch_safe"] is False
    assert any("distributed multi-node scheduler is not implemented" in warning for warning in payload["deployment"]["warnings"])
    assert payload["ops"]["scheduler"]["dispatch_safety"]["multi_process_dispatch_safe"] is True
    assert payload["ops"]["scheduler"]["multi_node_dispatch_safe"] is False


def test_readiness_reports_invalid_smtp_from_as_unavailable(app_factory) -> None:
    with TestClient(
        app_factory(
            NOTIFICATION_EMAIL_SMTP_HOST="smtp.example.com",
            NOTIFICATION_EMAIL_FROM="not-an-email",
        )
    ) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["notification_delivery"]["smtp_available"] is False
    assert "SMTP from 이메일 주소 형식" in payload["notification_delivery"]["smtp_unavailable_reason"]
    assert any("SMTP from 이메일 주소 형식" in warning for warning in payload["deployment"]["warnings"])


def test_json_log_formatter_outputs_structured_request_fields() -> None:
    record = logging.LogRecord(
        name="app.request",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="http_request",
        args=(),
        exc_info=None,
    )
    record.request_id = "request-1"
    record.method = "GET"
    record.path = "/health/ready"
    record.status_code = 200
    record.duration_ms = 12.5
    record.event = "http_request"

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["message"] == "http_request"
    assert payload["request_id"] == "request-1"
    assert payload["method"] == "GET"
    assert payload["path"] == "/health/ready"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.5
def test_scheduler_backend_descriptor_is_stable_across_subsequent_mode_resolution(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    env_backend = topic_schedules.get_scheduler_backend()
    assert env_backend.descriptor()["configured_runtime_mode_source"] == "SCHEDULER_MODE"

    argument_backend = topic_schedules.get_scheduler_backend("queue_backed")
    assert argument_backend.descriptor()["configured_runtime_mode_source"] == "argument"

    assert env_backend.descriptor()["configured_runtime_mode"] == "queue_backed"
    assert env_backend.descriptor()["configured_runtime_mode_raw"] == "queue_backed"
    assert env_backend.descriptor()["configured_runtime_mode_source"] == "SCHEDULER_MODE"
    assert env_backend.descriptor()["configured_runtime_mode_valid"] is True


def test_repeated_settings_status_follows_current_env_after_scheduler_backend_checks(app_factory):
    topic_schedules.get_scheduler_backend("queue_backed")
    topic_schedules.get_scheduler_backend("cluster")
    topic_schedules.get_scheduler_backend("single_node_file_backed")

    from fastapi.testclient import TestClient

    with TestClient(app_factory(SCHEDULER_MODE="queue_backed", SCHEDULER_DISTRIBUTED="true")) as client:
        first = client.get("/settings/status")
        second = client.get("/settings/status")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    for response in (first, second):
        payload = response.json()
        assert payload["deployment"]["readiness"] == "degraded"
        assert payload["deployment"]["env"]["SCHEDULER_MODE_RAW"] == "queue_backed"
        assert payload["deployment"]["env"]["SCHEDULER_MODE_SOURCE"] == "SCHEDULER_MODE"
        assert payload["ops"]["scheduler"]["configured_runtime_mode_source"] == "SCHEDULER_MODE"


def test_runtime_state_reset_clears_env_sensitive_caches_and_backend_leakage(
    monkeypatch,
    runtime_state_reset,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("SESSION_SECRET", "production-session-secret-with-enough-entropy")
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    assert get_auth_settings().auth_mode == "oidc"

    monkeypatch.setenv("AUTH_MODE", "disabled")
    assert get_auth_settings().auth_mode == "oidc"

    setattr(topic_schedules.FILE_JSON_SCHEDULER_BACKEND, "configured_runtime_mode_source", "leaked")

    runtime_state_reset()

    assert get_auth_settings().auth_mode == "disabled"
    assert not hasattr(topic_schedules.FILE_JSON_SCHEDULER_BACKEND, "configured_runtime_mode_source")


def test_readiness_source_of_truth_survives_order_sensitive_runtime_mutations(
    app_factory,
    monkeypatch,
    tmp_path,
    capsys,
    runtime_state_reset,
) -> None:
    data_root = tmp_path / "app_factory_status_storage"
    schedule_dir = data_root / "topic_schedules"
    schedule_dir.mkdir(parents=True)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)
    runtime_state_reset()

    # Source of truth: scheduler readiness is derived from scheduler config, not auth/OIDC env.
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("SESSION_SECRET", "production-session-secret-with-enough-entropy")
    monkeypatch.setenv("APP_BASE_URL", "https://research.example.com")
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    after_oidc = topic_schedules.get_scheduler_runtime_status(runtime_mode="single_node_file_backed")
    assert after_oidc["runtime_mode"] == "single_node_file_backed"
    assert after_oidc["configured_runtime_mode_source"] == "argument"
    assert after_oidc["configured_runtime_mode_valid"] is True

    # Source of truth: SMTP readiness is dynamic notification config, not scheduler mode.
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PORT", "587")
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "true")
    smtp_diagnostics = topic_notifications.smtp_config_diagnostics()
    assert smtp_diagnostics["readiness_state"] == "available"
    after_smtp = topic_schedules.get_scheduler_runtime_status(runtime_mode="single_node_file_backed")
    assert after_smtp["runtime_mode"] == after_oidc["runtime_mode"]
    assert after_smtp["configured_runtime_mode_source"] == "argument"

    queue_status_exit = topic_schedule_worker.main(["--status", "--runtime-mode", "queue_backed"])
    queue_status_output = json.loads(capsys.readouterr().out)
    assert queue_status_exit == 0
    assert queue_status_output["runtime_mode"] == "queue_backed"
    assert queue_status_output["configured_runtime_mode_source"] == "argument"
    assert queue_status_output["configured_runtime_mode_valid"] is True

    invalid_check_exit = topic_schedule_worker.main(["--check", "--runtime-mode", "cluster"])
    invalid_check_output = json.loads(capsys.readouterr().out)
    assert invalid_check_exit == 2
    assert invalid_check_output["configured_runtime_mode_raw"] == "cluster"
    assert invalid_check_output["configured_runtime_mode_valid"] is False

    stale_lock = schedule_dir / "schedules.lock"
    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    stale_lock.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "hostname": topic_schedules._current_lock_hostname(),
                "purpose": "readiness-order-regression-stale-lock",
                "acquired_at": stale_time.isoformat(),
                "createdAt": stale_time.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    os.utime(stale_lock, (stale_time.timestamp(), stale_time.timestamp()))
    lock_check_exit = topic_schedule_worker.main(["--lock-check", "--recover-stale-lock"])
    lock_check_output = json.loads(capsys.readouterr().out)
    assert lock_check_exit == 0
    assert lock_check_output["cleanup_result"] == "cleared"
    assert stale_lock.exists() is False

    live_lock = schedule_dir / "schedules.lock"
    live_lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": topic_schedules._current_lock_hostname(),
                "purpose": "readiness-order-regression-live-lock",
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    try:
        env = {
            "SCHEDULER_MODE": "queue_backed",
            "SCHEDULER_DISTRIBUTED": "true",
            "NOTIFICATION_EMAIL_SMTP_HOST": "smtp.example.com",
            "NOTIFICATION_EMAIL_SMTP_PORT": "587",
            "NOTIFICATION_EMAIL_FROM": "alerts@example.com",
            "NOTIFICATION_EMAIL_SMTP_TLS": "true",
            "XRML_DATA_DIR": str(data_root),
        }
        with TestClient(app_factory(**env)) as client:
            first_response = client.get("/settings/status")
            second_response = client.get("/settings/status")

        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text

        first = first_response.json()
        second = second_response.json()
        first_projection = {
            "readiness": first["deployment"]["readiness"],
            "scheduler_mode_raw": first["deployment"]["env"]["SCHEDULER_MODE_RAW"],
            "scheduler_mode_source": first["deployment"]["env"]["SCHEDULER_MODE_SOURCE"],
            "runtime_mode": first["ops"]["scheduler"]["runtime_mode"],
            "runtime_source": first["ops"]["scheduler"]["configured_runtime_mode_source"],
            "runtime_valid": first["ops"]["scheduler"]["configured_runtime_mode_valid"],
            "distributed_support": first["ops"]["scheduler"]["distributed_support_status"],
            "store_locked": first["ops"]["scheduler"]["store_lock"]["locked"],
            "owner_running": first["ops"]["scheduler"]["store_lock"]["owner_running"],
            "smtp_state": first["deployment"]["config_health"]["notification_delivery"]["smtp_readiness_state"],
        }
        second_projection = {
            "readiness": second["deployment"]["readiness"],
            "scheduler_mode_raw": second["deployment"]["env"]["SCHEDULER_MODE_RAW"],
            "scheduler_mode_source": second["deployment"]["env"]["SCHEDULER_MODE_SOURCE"],
            "runtime_mode": second["ops"]["scheduler"]["runtime_mode"],
            "runtime_source": second["ops"]["scheduler"]["configured_runtime_mode_source"],
            "runtime_valid": second["ops"]["scheduler"]["configured_runtime_mode_valid"],
            "distributed_support": second["ops"]["scheduler"]["distributed_support_status"],
            "store_locked": second["ops"]["scheduler"]["store_lock"]["locked"],
            "owner_running": second["ops"]["scheduler"]["store_lock"]["owner_running"],
            "smtp_state": second["deployment"]["config_health"]["notification_delivery"]["smtp_readiness_state"],
        }
        assert second_projection == first_projection
        assert first_projection == {
            "readiness": "degraded",
            "scheduler_mode_raw": "queue_backed",
            "scheduler_mode_source": "SCHEDULER_MODE",
            "runtime_mode": "queue_backed",
            "runtime_source": "SCHEDULER_MODE",
            "runtime_valid": True,
            "distributed_support": "unsupported",
            "store_locked": True,
            "owner_running": True,
            "smtp_state": "available",
        }
    finally:
        live_lock.unlink(missing_ok=True)

    with TestClient(app_factory(SCHEDULER_MODE="queue_backed", SCHEDULER_DISTRIBUTED="true")) as client:
        recovered_response = client.get("/settings/status")

    assert recovered_response.status_code == 200, recovered_response.text
    recovered = recovered_response.json()
    assert recovered["deployment"]["env"]["SCHEDULER_MODE_RAW"] == "queue_backed"
    assert recovered["ops"]["scheduler"]["runtime_mode"] == "queue_backed"
    assert recovered["ops"]["scheduler"]["configured_runtime_mode_source"] == "SCHEDULER_MODE"
    assert recovered["ops"]["scheduler"]["store_lock"]["locked"] is False

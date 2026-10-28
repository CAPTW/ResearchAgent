from __future__ import annotations

import io
import json
import sys

from app.auth.config import clear_auth_settings_cache
from app.jobs import production_activation_check
from app.ops.production_activation import build_production_activation_result


def _clear_activation_env(monkeypatch) -> None:
    for key in (
        "AUTH_MODE",
        "SESSION_SECRET",
        "APP_BASE_URL",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_AUDIENCE",
        "OIDC_SCOPES",
        "XRML_ENVIRONMENT",
        "XRML_SHARED_DEPLOYMENT",
        "XRML_DATA_DIR",
        "XRML_STRICT_ENV_VALIDATION",
        "NOTIFICATION_EMAIL_SMTP_HOST",
        "NOTIFICATION_EMAIL_SMTP_PORT",
        "NOTIFICATION_EMAIL_SMTP_USERNAME",
        "NOTIFICATION_EMAIL_SMTP_PASSWORD",
        "NOTIFICATION_EMAIL_FROM",
        "NOTIFICATION_EMAIL_SMTP_TLS",
        "NOTIFICATION_EMAIL_SMTP_SSL",
    ):
        monkeypatch.delenv(key, raising=False)
    clear_auth_settings_cache()


def test_activation_check_keeps_local_disabled_auth_valid_but_warned(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)

    result = build_production_activation_result()

    assert result.ok is True
    assert result.checks["auth"]["mode"] == "disabled"
    assert result.checks["auth"]["deployment_safety_status"] == "local_internal_only"
    assert result.checks["smtp"]["state"] == "unavailable_missing_env"
    assert result.errors == []


def test_activation_check_require_oidc_fails_without_oidc(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)

    result = build_production_activation_result(require_oidc=True)

    assert result.ok is False
    assert any("AUTH_MODE=oidc" in item for item in result.errors)
    assert any("configured OIDC" in item for item in result.errors)


def test_activation_check_reports_valid_oidc_and_redacts_secret_values(monkeypatch, tmp_path) -> None:
    _clear_activation_env(monkeypatch)
    secret = "super-secret-client-value"
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("SESSION_SECRET", "production-session-secret-with-enough-entropy")
    monkeypatch.setenv("APP_BASE_URL", "https://research.example.com")
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", secret)
    monkeypatch.setenv("OIDC_AUDIENCE", "https://issuer.example.com/xrml-api")
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path / "data"))
    clear_auth_settings_cache()

    result = build_production_activation_result(require_oidc=True)
    rendered = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.ok is True
    assert result.checks["auth"]["oidc_configured"] is True
    assert result.checks["secret_redaction"]["secret_values_redacted"] is True
    assert secret not in rendered


def test_activation_check_require_smtp_fails_when_missing(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)

    result = build_production_activation_result(require_smtp=True)

    assert result.ok is False
    assert result.checks["smtp"]["state"] == "unavailable_missing_env"
    assert any("SMTP activation requires available SMTP env" in item for item in result.errors)


def test_activation_check_reports_available_smtp_without_leaking_password(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)
    password = "smtp-secret-password"
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PORT", "587")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PASSWORD", password)
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "research@example.com")

    result = build_production_activation_result(require_smtp=True)
    rendered = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.ok is True
    assert result.checks["smtp"]["state"] == "available"
    assert result.checks["smtp"]["available"] is True
    assert password not in rendered


def test_activation_check_reports_invalid_smtp_config_without_secret_values(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)
    password = "smtp-secret-password"
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PORT", "not-a-number")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PASSWORD", password)
    monkeypatch.setenv("NOTIFICATION_EMAIL_FROM", "research@example.com")

    result = build_production_activation_result(require_smtp=True)
    rendered = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.ok is False
    assert result.checks["smtp"]["state"] == "misconfigured"
    assert result.checks["smtp"]["readiness_state"] == "invalid_config"
    assert result.checks["smtp"]["configured"] is False
    assert "NOTIFICATION_EMAIL_SMTP_PORT" in result.checks["smtp"]["invalid_config"]
    assert password not in rendered


def test_activation_check_reports_core_env_contract_states_without_secret_values(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)
    secret = "production-session-secret-with-enough-entropy"
    smtp_password = "smtp-secret-password"
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_PASSWORD", smtp_password)
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_TLS", "true")
    monkeypatch.setenv("NOTIFICATION_EMAIL_SMTP_SSL", "false")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    monkeypatch.setenv("XRML_STRICT_ENV_VALIDATION", "true")

    result = build_production_activation_result()
    rendered = json.dumps(result.to_dict(), ensure_ascii=False)
    env_states = result.checks["env"]

    assert env_states["SESSION_SECRET"] == "set"
    assert env_states["NOTIFICATION_EMAIL_SMTP_PASSWORD"] == "set"
    assert env_states["NOTIFICATION_EMAIL_SMTP_TLS"] == "set"
    assert env_states["NOTIFICATION_EMAIL_SMTP_SSL"] == "set"
    assert env_states["NOTIFICATION_WEBHOOK_ENABLED"] == "set"
    assert env_states["SCHEDULER_MODE"] == "set"
    assert env_states["XRML_STRICT_ENV_VALIDATION"] == "set"
    assert secret not in rendered
    assert smtp_password not in rendered


def test_activation_cli_returns_exit_code_two_for_missing_required_oidc(monkeypatch, capsys) -> None:
    _clear_activation_env(monkeypatch)

    exit_code = production_activation_check.main(["--require-oidc"])
    output = capsys.readouterr().out

    assert exit_code == 2
    assert "Production activation requires AUTH_MODE=oidc" in output
    assert "OIDC_CLIENT_SECRET" in output
    assert "client-secret" not in output


def test_activation_cli_preserves_unicode_json_on_legacy_redirected_stdout(monkeypatch) -> None:
    _clear_activation_env(monkeypatch)
    result = build_production_activation_result()
    expected = result.to_dict()
    # The real missing-SMTP diagnostics contain Korean text.
    assert any(ord(character) > 127 for character in json.dumps(expected, ensure_ascii=False))
    monkeypatch.setattr(production_activation_check, "build_production_activation_result", lambda **_kwargs: result)
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    with monkeypatch.context() as capture:
        capture.setattr(sys, "stdout", output)
        exit_code = production_activation_check.main([])
        output.flush()
    assert exit_code == 0
    assert json.loads(buffer.getvalue()) == expected

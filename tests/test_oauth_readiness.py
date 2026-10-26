from __future__ import annotations

import json

from app.auth.config import AuthSettings
from app.cli import oauth_smoke as oauth_smoke_cli
from app.services import oauth_readiness


def test_oauth_readonly_proof_default_disabled_is_safe() -> None:
    result = oauth_readiness.build_oauth_readonly_proof_result(settings=AuthSettings())

    assert result["proofLevel"] == "disabled_default"
    assert result["status"] == "safe"
    assert result["authMode"] == "disabled"
    assert result["tokenStorageEnabled"] is False
    assert result["dashboardTriggerAllowed"] is False
    assert result["liveSmokeRequested"] is False


def test_oauth_readonly_metadata_validation_distinguishes_local_from_live_proof() -> None:
    result = oauth_readiness.build_oauth_readonly_proof_result(
        settings=AuthSettings(app_base_url="http://127.0.0.1:8000"),
        metadata=True,
    )

    assert result["proofLevel"] == "local_metadata_valid"
    assert result["metadataValid"] is True
    assert result["publicHttpsReady"] is False
    assert result["readOnlyRoutesChecked"]
    assert result["writeRoutesExposed"] is False
    assert result["liveSmokeAllowed"] is False


def test_oauth_readonly_metadata_reports_missing_oidc_config() -> None:
    result = oauth_readiness.build_oauth_readonly_proof_result(
        settings=AuthSettings(auth_mode="oidc", app_base_url="https://agent.example.com"),
        metadata=True,
    )

    assert result["proofLevel"] == "config_missing"
    assert result["status"] == "blocked"
    assert result["metadataValid"] is True
    assert "idp_configuration_required" in result["reasonCodes"]


def test_oauth_readonly_proof_rejects_malformed_oidc_metadata() -> None:
    result = oauth_readiness.build_oauth_readonly_proof_result(
        settings=AuthSettings(
            auth_mode="oidc",
            app_base_url="https://agent.example.com",
            oidc_issuer="not-a-url",
            oidc_client_id="client-id",
            oidc_client_secret="client-secret",
            oidc_audience="research-api",
        ),
        metadata=True,
    )

    assert result["proofLevel"] == "failed"
    assert result["metadataValid"] is False
    assert "invalid_oidc_issuer" in result["reasonCodes"]
    assert "client-secret" not in json.dumps(result)


def test_oauth_mock_exchange_passes_without_live_or_secret_output() -> None:
    result = oauth_readiness.build_oauth_readonly_proof_result(mock_exchange=True)
    serialized = json.dumps(result)

    assert result["proofLevel"] == "mock_exchange_passed"
    assert result["mockExchangeRequested"] is True
    assert result["mockExchangePassed"] is True
    assert result["liveSmokeRequested"] is False
    assert result["tokenStorageEnabled"] is False
    assert "ya29.mock-access-token" not in serialized
    assert "refresh-token-secret" not in serialized
    assert "id-token-secret" not in serialized


def test_oauth_live_smoke_requires_flag_env_public_https_and_idp() -> None:
    flag_only = oauth_readiness.build_oauth_readonly_proof_result(
        allow_live_oauth_smoke=True,
        env={},
    )
    env_only = oauth_readiness.build_oauth_readonly_proof_result(
        env={"ALLOW_LIVE_OAUTH_SMOKE": "1"},
    )
    local_http = oauth_readiness.build_oauth_readonly_proof_result(
        settings=AuthSettings(app_base_url="http://127.0.0.1:8000"),
        allow_live_oauth_smoke=True,
        env={"ALLOW_LIVE_OAUTH_SMOKE": "1"},
    )
    missing_idp = oauth_readiness.build_oauth_readonly_proof_result(
        settings=AuthSettings(app_base_url="https://agent.example.com"),
        allow_live_oauth_smoke=True,
        env={"ALLOW_LIVE_OAUTH_SMOKE": "1"},
    )

    assert flag_only["proofLevel"] == "live_smoke_blocked"
    assert flag_only["liveSmokeAllowed"] is False
    assert env_only["liveSmokeRequested"] is False
    assert env_only["proofLevel"] == "disabled_default"
    assert local_http["proofLevel"] == "public_https_missing"
    assert missing_idp["proofLevel"] == "idp_missing"


def test_oauth_live_readonly_connector_probe_can_pass_only_after_gates() -> None:
    settings = AuthSettings(
        auth_mode="oidc",
        app_base_url="https://agent.example.com",
        oidc_issuer="https://issuer.example.com",
        oidc_client_id="client-id",
        oidc_client_secret="client-secret",
        oidc_audience="research-api",
    )
    blocked = oauth_readiness.build_oauth_readonly_proof_result(
        settings=settings,
        allow_live_oauth_smoke=True,
        env={"ALLOW_LIVE_OAUTH_SMOKE": "1"},
    )
    passed = oauth_readiness.build_oauth_readonly_proof_result(
        settings=settings,
        allow_live_oauth_smoke=True,
        env={"ALLOW_LIVE_OAUTH_SMOKE": "1"},
        connector_probe_passed=True,
    )

    assert blocked["proofLevel"] == "live_smoke_blocked"
    assert "read_only_connector_probe_not_configured" in blocked["reasonCodes"]
    assert passed["proofLevel"] == "read_only_connector_smoke_passed"
    assert passed["liveSmokeAllowed"] is True
    assert passed["readOnlyRoutesChecked"]
    assert passed["writeRoutesExposed"] is False


def test_oauth_readonly_proof_api_does_not_trigger_live_oauth(client, monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_OAUTH_SMOKE", "1")

    response = client.get("/auth/oauth/read-only-proof")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["liveSmokeRequested"] is False
    assert body["liveSmokeAllowed"] is False
    assert body["dashboardTriggerAllowed"] is False
    assert body["tokenStorageEnabled"] is False


def test_oauth_smoke_cli_outputs_json_and_redacts_mock_values(capsys) -> None:
    exit_code = oauth_smoke_cli.main(["--mock-exchange"])
    payload = json.loads(capsys.readouterr().out)
    serialized = json.dumps(payload)

    assert exit_code == 0
    assert payload["proofLevel"] == "mock_exchange_passed"
    assert "ya29.mock-access-token" not in serialized
    assert "refresh-token-secret" not in serialized
    assert "id-token-secret" not in serialized

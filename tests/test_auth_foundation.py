from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.config import AuthSettings
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews
from app.schemas.auth import (
    OAuthAccountLinkMetadataResponse,
    OAuthLiveExchangeReadinessResponse,
    OAuthLiveExchangePilotResultResponse,
    OAuthLiveExchangePilotStatusResponse,
    OAuthOperatorAcceptanceChecklistResponse,
    OAuthPreflightPackageResponse,
    OAuthReadinessRemediationSummaryResponse,
)
from app.schemas.operator_audit import OperatorAuditEventResponse
from app.services import (
    action_capabilities,
    auth_foundation,
    oauth_account_links,
    oauth_dry_run,
    oauth_live_evidence,
    oauth_live_readiness,
    oauth_token_exchange,
    operator_audit,
    topic_profiles,
    topic_schedules,
)
from app.services import ai_provider


def _isolate_topic_profiles(client, isolated_run_repo, monkeypatch):
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


def _review(index: int) -> dict:
    text = (
        f"Auth foundation provider gate evidence {index} explains retrieval augmented generation, "
        f"large language model evaluation, limitations, and grounded reporting. "
    ) * 20
    return {
        "topic_id": "large-language-models",
        "run_id": "auth-foundation-provider-run",
        "paper_id": f"auth-provider-paper-{index}",
        "title": f"Auth Provider Gate Paper {index}",
        "source_provider": "arxiv",
        "abstract_text": "This paper studies LLM evidence review with retrieval augmented generation.",
        "text_excerpt": text,
        "full_text_status": "reviewed",
        "relevance_score": 95 - index,
        "novelty_score": 60 + index,
        "evidence_strength": 82,
        "method_summary": "Deterministic baseline summary.",
        "key_findings": ["Grounded evidence helps synthesis."],
        "limitations": ["Provider output must be validated."],
        "why_it_matters": "It supports safe AI-assisted review drafting.",
        "topic_fit_rationale": "Matches LLM and RAG topic terms.",
        "review_model": "deterministic-local-full-text-v1",
        "reviewer": "deterministic-local-full-text",
        "reviewed_at": "2026-05-08T00:00:00+00:00",
        "source_text_sha256": f"auth-provider-sha-{index}",
        "full_text_artifact_path": f"storage/runs/auth-foundation-provider-run/raw/auth-provider-paper-{index}.txt",
        "text_char_count": len(text),
        "evidence_spans": [
            {
                "label": f"auth-provider-span-{index}",
                "text": f"Provider span {index} supports grounded review drafting.",
                "source_path": f"storage/runs/auth-foundation-provider-run/raw/auth-provider-paper-{index}.txt",
                "page_number": 1,
                "char_start": 0,
                "char_end": 64,
            }
        ],
        "review_source_type": "full_text",
        "full_text_reviewed_at": "2026-05-08T00:00:00+00:00",
    }


def test_oauth_metadata_validation_accepts_env_refs_and_rejects_secret_literals() -> None:
    valid_metadata = auth_foundation.validate_oauth_provider_metadata(
        {
            "providerId": "chatgpt-oauth",
            "displayName": "ChatGPT OAuth",
            "issuer": "https://issuer.example.com",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "tokenUrl": "https://issuer.example.com/oauth/token",
            "scopes": ["openid", "profile", "email"],
            "clientIdEnvVar": "CHATGPT_CLIENT_ID",
            "clientSecretEnvVar": "CHATGPT_CLIENT_SECRET",
        }
    )

    assert valid_metadata["validationStatus"] == "valid"
    assert valid_metadata["secretPolicy"]["rejectedFields"] == []

    result = auth_foundation.validate_oauth_provider_metadata(
        {
            "providerId": "chatgpt-oauth",
            "displayName": "ChatGPT OAuth",
            "issuer": "https://issuer.example.com",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "tokenUrl": "https://issuer.example.com/oauth/token",
            "scopes": ["openid", "profile", "email"],
            "clientIdEnvVar": "CHATGPT_CLIENT_ID",
            "clientSecretEnvVar": "CHATGPT_CLIENT_SECRET",
            "clientSecret": "literal-secret-must-not-persist",
            "accessToken": "ya29.token-must-not-persist",
        }
    )
    serialized = json.dumps(result)

    assert result["validationStatus"] == "invalid"
    assert result["oauthProviderMetadata"]["clientSecretEnvVar"] == "CHATGPT_CLIENT_SECRET"
    assert result["oauthProviderMetadata"]["tokenStorageEnabled"] is False
    assert result["oauthProviderMetadata"]["liveTokenExchangeEnabled"] is False
    assert "clientSecret" in result["secretPolicy"]["rejectedFields"]
    assert "accessToken" in result["secretPolicy"]["rejectedFields"]
    assert "literal-secret-must-not-persist" not in serialized
    assert "ya29.token-must-not-persist" not in serialized

    env_var_secret = auth_foundation.validate_oauth_provider_metadata(
        {
            "providerId": "chatgpt-oauth",
            "displayName": "ChatGPT OAuth",
            "clientSecretEnvVar": "sk-env-field-must-not-persist",
        }
    )

    assert env_var_secret["validationStatus"] == "invalid"
    assert "clientSecretEnvVar" in env_var_secret["secretPolicy"]["rejectedFields"]
    assert env_var_secret["oauthProviderMetadata"]["clientSecretEnvVar"] is None
    assert "sk-env-field-must-not-persist" not in json.dumps(env_var_secret)


def test_auth_foundation_defaults_to_safe_local_operator_without_token_storage() -> None:
    status = auth_foundation.build_auth_foundation_status(settings=AuthSettings())

    assert status["authMode"] == "local_operator"
    assert status["legacyAuthMode"] == "disabled"
    assert status["oauthReady"] is False
    assert status["tokenStorageEnabled"] is False
    assert status["operator"]["role"] == "local_operator"
    assert "view_dashboard" in status["operator"]["capabilities"]
    assert "edit_topic_config" in status["operator"]["capabilities"]
    assert "accept_provider_risk" not in status["operator"]["capabilities"]
    assert "token_storage_disabled" in status["reasonCodes"]

    incomplete_oidc = auth_foundation.build_auth_foundation_status(settings=AuthSettings(auth_mode="oidc"))
    ready_oidc = auth_foundation.build_auth_foundation_status(
        settings=AuthSettings(
            auth_mode="oidc",
            oidc_issuer="https://issuer.example.com",
            oidc_client_id="client-id",
        )
    )

    assert incomplete_oidc["authMode"] == "disabled"
    assert incomplete_oidc["oauthReady"] is False
    assert ready_oidc["authMode"] == "oauth_ready"
    assert ready_oidc["oauthReady"] is True
    assert ready_oidc["tokenStorageEnabled"] is False


def test_capability_model_denies_unauthorized_write_and_allows_admin() -> None:
    settings = AuthSettings(auth_mode="oidc")
    viewer = ProductUser(
        user_id="viewer",
        display_name="Viewer",
        email="viewer@example.com",
        auth_source="oidc",
        is_admin=False,
    )
    admin = ProductUser(
        user_id="admin",
        display_name="Admin",
        email="admin@example.com",
        auth_source="oidc",
        is_admin=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_foundation.require_capability(viewer, "edit_topic_config", settings=settings)

    assert exc_info.value.status_code == 403
    assert auth_foundation.require_capability(admin, "edit_topic_config", settings=settings) == admin


def test_topic_configuration_save_checks_edit_capability_when_auth_enabled(app_factory, isolated_run_repo, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit" / "events.jsonl")
    app = app_factory(
        AUTH_MODE="oidc",
        SESSION_SECRET="synthetic-test-session-secret-0123456789abcdef",
        APP_BASE_URL="http://127.0.0.1:8000",
    )
    viewer = ProductUser(
        user_id="viewer",
        display_name="Viewer",
        email="viewer@example.com",
        auth_source="oidc",
        is_admin=False,
    )
    app.dependency_overrides[require_product_user] = lambda: viewer
    with TestClient(app) as client:
        _isolate_topic_profiles(client, isolated_run_repo, monkeypatch)

        response = client.put(
            "/api/topics/large-language-models/configuration",
            json={
                "displayName": "Should Not Save",
                "queryTerms": ["llm safety"],
                "defaultAllowDomains": ["arxiv.org"],
                "defaultDownloadLimit": 1,
                "defaultMaxPdfBytes": 5_000_000,
            },
        )

    assert response.status_code == 403
    assert "missing_capability" in response.json()["detail"]["reason_codes"]


def test_action_capability_registry_fails_closed_for_write_risk_actions() -> None:
    registry = action_capabilities.list_action_requirements()

    assert registry["save_topic_config"]["capabilityRequired"] == "edit_topic_config"
    assert registry["save_topic_config"]["riskLevel"] == "config_write"
    assert registry["save_topic_config"]["writeCapable"] is True
    assert registry["validate_topic_config"]["riskLevel"] == "read_only"
    assert registry["validate_topic_config"]["writeCapable"] is False
    assert "accept_provider_risk" in registry["provider_acceptance_smoke"]["additionalCapabilities"]

    missing = action_capabilities.evaluate_action_capability(
        "unregistered_write_action",
        operator_capabilities=["view_dashboard"],
        fallback_risk_level="config_write",
    )

    assert missing["allowed"] is False
    assert "missing_action_requirement" in missing["reasonCodes"]
    assert missing["riskLevel"] == "config_write"


def test_auth_status_is_exposed_in_settings_and_topic_dashboard(client) -> None:
    settings_response = client.get("/settings/status")
    dashboard_response = client.get("/api/topics/large-language-models/dashboard")

    assert settings_response.status_code == 200, settings_response.text
    assert dashboard_response.status_code == 200, dashboard_response.text
    settings_status = settings_response.json()["authFoundation"]
    dashboard_status = dashboard_response.json()["researchOperations"]["auth"]
    assert settings_status["tokenStorageEnabled"] is False
    assert settings_status["oauthReady"] is False
    assert dashboard_status["providerLiveExecution"] == "cli_only_acceptance_gated"
    assert "preview_run_plan" in dashboard_status["operator"]["capabilities"]


def test_oauth_metadata_validation_api_writes_nothing_and_returns_secret_policy(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit" / "events.jsonl")

    response = client.post(
        "/auth/oauth/metadata/validate",
        json={
            "providerId": "codex-oauth",
            "displayName": "Codex OAuth",
            "issuer": "https://issuer.example.com",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "tokenUrl": "https://issuer.example.com/oauth/token",
            "clientIdEnvVar": "CODEX_CLIENT_ID",
            "clientSecretEnvVar": "CODEX_CLIENT_SECRET",
            "refreshToken": "refresh-token-must-not-persist",
        },
    )
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["validationStatus"] == "invalid"
    assert body["oauthProviderMetadata"]["clientSecretEnvVar"] == "CODEX_CLIENT_SECRET"
    assert body["secretPolicy"]["valid"] is False
    assert body["oauthProviderMetadata"]["tokenStorageEnabled"] is False
    assert "refreshToken" in body["secretPolicy"]["rejectedFields"]
    assert "refresh-token-must-not-persist" not in json.dumps(body)


def test_capability_model_does_not_bypass_provider_acceptance_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    admin = ProductUser(
        user_id="admin",
        display_name="Admin",
        email="admin@example.com",
        auth_source="oidc",
        is_admin=True,
    )
    assert auth_foundation.has_capability(admin, "accept_provider_risk", settings=AuthSettings(auth_mode="oidc"))

    result = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="gemini-cli-example",
        dry_run=False,
        limit=1,
        output_root=tmp_path / "provider-output",
        run_dir=tmp_path / "provider-run",
        allow_live_provider=True,
        accept_provider_risk=False,
        import_provider_drafts=True,
        command_exists=lambda command: True,
    )

    assert result["operator_acceptance_required"] is True
    assert result["operator_acceptance_present"] is False
    assert result["external_provider_executed"] is False
    assert "provider_research_output_blocked" in result["reason_codes"]
    assert "not_allowed_for_research_output" in result["reason_codes"]
    assert "research_output_blocked" in result["reason_codes"]
    assert "live_proof_missing" in result["reason_codes"]
    assert ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10) == []


def test_operator_audit_event_serialization_redacts_secret_values(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    event = operator_audit.record_event(
        action_type="secret_policy_rejection",
        operator_id="viewer",
        auth_mode="oauth_ready",
        decision="rejected",
        reason_codes=["secret_like_value"],
        target_type="oauth_provider",
        target_id="codex-oauth",
        risk_level="secret_policy",
        metadata={
            "accessToken": "ya29.token-must-not-persist",
            "clientSecretEnvVar": "CODEX_CLIENT_SECRET",
            "nested": {"apiKey": "sk-key-must-not-persist"},
        },
        timestamp="2026-05-09T00:00:00+00:00",
    )
    serialized = json.dumps(event, ensure_ascii=False)
    parsed = OperatorAuditEventResponse.model_validate(event)

    assert parsed.actionType == "secret_policy_rejection"
    assert event["secretPresent"] is False
    assert event["metadata"]["accessToken"] == "<redacted>"
    assert event["metadata"]["nested"]["apiKey"] == "<redacted>"
    assert "ya29.token-must-not-persist" not in serialized
    assert "sk-key-must-not-persist" not in serialized
    assert operator_audit.summarize_events()["secretPolicyRejectionCount"] == 1


def test_oauth_metadata_validation_api_audits_secret_policy_without_secret(client, tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    response = client.post(
        "/auth/oauth/metadata/validate",
        json={
            "providerId": "codex-oauth",
            "displayName": "Codex OAuth",
            "clientIdEnvVar": "CODEX_CLIENT_ID",
            "clientSecretEnvVar": "CODEX_CLIENT_SECRET",
            "accessToken": "ya29.audit-token-must-not-persist",
        },
    )
    body = response.json()
    summary = operator_audit.summarize_events()
    events_json = audit_path.read_text(encoding="utf-8")

    assert response.status_code == 200, response.text
    assert body["validationStatus"] == "invalid"
    assert summary["secretPolicyRejectionCount"] == 1
    assert summary["totalEvents"] == 1
    assert summary["latestEventSummaries"][0]["actionType"] == "secret_policy_rejection"
    assert "ya29.audit-token-must-not-persist" not in events_json


def test_topic_configuration_save_allowed_and_denied_are_audited(app_factory, isolated_run_repo, monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    app = app_factory(
        AUTH_MODE="oidc",
        SESSION_SECRET="synthetic-test-session-secret-0123456789abcdef",
        APP_BASE_URL="http://127.0.0.1:8000",
    )
    viewer = ProductUser(
        user_id="viewer",
        display_name="Viewer",
        email="viewer@example.com",
        auth_source="oidc",
        is_admin=False,
    )
    admin = ProductUser(
        user_id="admin",
        display_name="Admin",
        email="admin@example.com",
        auth_source="oidc",
        is_admin=True,
    )
    payload = {
        "displayName": "Audited Topic Config",
        "queryTerms": ["llm safety"],
        "defaultAllowDomains": ["arxiv.org"],
        "defaultDownloadLimit": 1,
        "defaultMaxPdfBytes": 5_000_000,
    }

    app.dependency_overrides[require_product_user] = lambda: viewer
    with TestClient(app) as client:
        _isolate_topic_profiles(client, isolated_run_repo, monkeypatch)
        denied_response = client.put("/api/topics/large-language-models/configuration", json=payload)

        app.dependency_overrides[require_product_user] = lambda: admin
        allowed_response = client.put("/api/topics/large-language-models/configuration", json=payload)

    summary = operator_audit.summarize_events()
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert denied_response.status_code == 403
    assert allowed_response.status_code == 200, allowed_response.text
    assert allowed_response.json()["validationStatus"] == "saved"
    assert summary["deniedCount"] == 1
    assert summary["allowedCount"] == 1
    assert summary["configWriteCount"] == 1
    assert [event["decision"] for event in events] == ["denied", "allowed"]
    assert all(event["actionType"] == "save_topic_config" for event in events)


def test_provider_acceptance_smoke_audits_risk_without_bypassing_gate(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(2))

    denied = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="gemini-cli-example",
        dry_run=False,
        limit=1,
        output_root=tmp_path / "provider-output",
        run_dir=tmp_path / "provider-run",
        allow_live_provider=True,
        accept_provider_risk=False,
        import_provider_drafts=True,
        command_exists=lambda command: True,
    )
    accepted_readiness_only = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="gemini-cli-example",
        dry_run=False,
        limit=1,
        output_root=tmp_path / "provider-output-accepted",
        run_dir=tmp_path / "provider-run-accepted",
        allow_live_provider=True,
        accept_provider_risk=True,
        provider_operator_note="accepted local readiness smoke",
        import_provider_drafts=True,
        command_exists=lambda command: False,
    )
    summary = operator_audit.summarize_events()
    events_json = audit_path.read_text(encoding="utf-8")

    assert denied["operator_acceptance_present"] is False
    assert denied["external_provider_executed"] is False
    assert "provider_research_output_blocked" in denied["reason_codes"]
    assert "not_allowed_for_research_output" in denied["reason_codes"]
    assert "research_output_blocked" in denied["reason_codes"]
    assert "live_proof_missing" in denied["reason_codes"]
    assert accepted_readiness_only["operator_acceptance_present"] is True
    assert accepted_readiness_only["external_provider_executed"] is False
    assert accepted_readiness_only["command_missing_count"] == 1
    assert summary["providerRiskAcceptanceCount"] == 1
    assert summary["deniedCount"] >= 1
    assert "accepted local readiness smoke" not in events_json


def test_provider_acceptance_smoke_dry_run_writes_no_audit_log(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")

    result = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="fixture-command",
        dry_run=True,
        limit=1,
        output_root=tmp_path / "provider-output",
        run_dir=tmp_path / "provider-run",
    )

    assert result["mode"] == "provider_acceptance_smoke"
    assert result["invoked_count"] == 0
    assert not audit_path.exists()
    assert operator_audit.summarize_events()["totalEvents"] == 0


def test_manual_provider_run_prepare_is_audited_without_command_output(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(3))

    result = ai_provider.prepare_manual_provider_run(
        "large-language-models",
        output_dir=tmp_path / "manual-provider-run",
        limit=1,
        provider_run_id="manual-audit-run",
        dry_run=False,
    )
    summary = operator_audit.summarize_events()
    events_json = audit_path.read_text(encoding="utf-8")

    assert result["written"] is True
    assert summary["totalEvents"] == 1
    assert summary["latestEventSummaries"][0]["actionType"] == "prepare_provider_manual_run"
    assert summary["latestEventSummaries"][0]["riskLevel"] == "provider_prepare"
    assert "manual-audit-run" in events_json
    assert "packet_payload" not in events_json


def test_audit_replay_validates_events_and_flags_secret_or_inconsistent_entries(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    operator_audit.record_event(
        action_type="save_topic_config",
        decision="allowed",
        operator_id="admin",
        auth_mode="oauth_ready",
        capability_required="edit_topic_config",
        capability_present=True,
        target_type="topic",
        target_id="large-language-models",
        risk_level="config_write",
        timestamp="2026-05-09T00:00:00+00:00",
    )
    audit_path.write_text(
        audit_path.read_text(encoding="utf-8")
        + "\n"
        + json.dumps({"auditEventId": "missing-fields"}, ensure_ascii=False)
        + "\n"
        + json.dumps(
            {
                "auditEventId": "secret-event",
                "timestamp": "2026-05-09T00:00:01+00:00",
                "operatorId": "admin",
                "authMode": "oauth_ready",
                "actionType": "secret_policy_rejection",
                "decision": "rejected",
                "reasonCodes": ["secret_like_value"],
                "riskLevel": "secret_policy",
                "secretPresent": False,
                "metadata": {"accessToken": "ya29.raw-token-must-be-flagged"},
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "auditEventId": "inconsistent-event",
                "timestamp": "2026-05-09T00:00:02+00:00",
                "operatorId": "admin",
                "authMode": "oauth_ready",
                "actionType": "save_topic_config",
                "capabilityRequired": "view_dashboard",
                "capabilityPresent": True,
                "decision": "allowed",
                "reasonCodes": [],
                "targetType": "topic",
                "targetId": "large-language-models",
                "riskLevel": "config_write",
                "secretPresent": False,
                "metadata": {},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    replay = operator_audit.validate_audit_log()

    assert replay["validEventCount"] == 1
    assert replay["invalidEventCount"] == 3
    assert replay["secretLikeViolationCount"] == 1
    assert replay["missingRequiredFieldCount"] == 1
    assert replay["inconsistentCapabilityCount"] == 1
    assert replay["auditLogStatus"] == "errors"
    assert replay["latestCheckedEventId"] == "inconsistent-event"
    assert replay["reasonCounts"]["secret_like_audit_metadata"] == 1


def test_audit_replay_missing_log_returns_empty_valid_health(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "missing" / "operator-audit.jsonl")

    replay = operator_audit.validate_audit_log()

    assert replay["auditLogStatus"] == "empty"
    assert replay["validEventCount"] == 0
    assert replay["invalidEventCount"] == 0


def test_auth_status_exposes_missing_audit_log_as_empty_summary(client, tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "missing" / "operator-audit.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    settings_response = client.get("/settings/status")
    dashboard_response = client.get("/api/topics/large-language-models/dashboard")

    assert settings_response.status_code == 200, settings_response.text
    assert dashboard_response.status_code == 200, dashboard_response.text
    assert settings_response.json()["authFoundation"]["operatorAuditSummary"]["totalEvents"] == 0
    assert settings_response.json()["authFoundation"]["operatorAuditHealth"]["auditLogStatus"] == "empty"
    assert dashboard_response.json()["researchOperations"]["auth"]["operatorAuditSummary"]["deniedCount"] == 0
    assert dashboard_response.json()["researchOperations"]["auth"]["operatorAuditHealth"]["invalidEventCount"] == 0


def test_auth_status_exposes_audit_health_without_raw_metadata(client, tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    operator_audit.record_event(
        action_type="save_topic_config",
        decision="allowed",
        operator_id="admin",
        auth_mode="oauth_ready",
        capability_required="edit_topic_config",
        capability_present=True,
        target_type="topic",
        target_id="large-language-models",
        risk_level="config_write",
        metadata={"changedFields": ["displayName"]},
    )

    settings_response = client.get("/settings/status")
    dashboard_response = client.get("/api/topics/large-language-models/dashboard")
    settings_health = settings_response.json()["authFoundation"]["operatorAuditHealth"]
    dashboard_health = dashboard_response.json()["researchOperations"]["auth"]["operatorAuditHealth"]

    assert settings_response.status_code == 200, settings_response.text
    assert dashboard_response.status_code == 200, dashboard_response.text
    assert settings_health["auditLogStatus"] == "valid"
    assert settings_health["validEventCount"] == 1
    assert dashboard_health["secretLikeViolationCount"] == 0
    assert "metadata" not in json.dumps(dashboard_health)


def test_oauth_authorization_prepare_builds_pkce_url_without_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_CLIENT_ID", "codex-client-id")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-state" / "states.jsonl")
    monkeypatch.setattr(oauth_dry_run, "_token_urlsafe", lambda _length: "deterministic-oauth-value")

    result = oauth_dry_run.build_authorization_url(
        {
            "providerId": "codex-oauth",
            "displayName": "Codex OAuth",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "tokenUrl": "https://issuer.example.com/oauth/token",
            "scopes": ["openid", "profile"],
            "clientIdEnvVar": "CODEX_CLIENT_ID",
            "clientSecretEnvVar": "CODEX_CLIENT_SECRET",
            "enabled": True,
        },
        redirect_uri="http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        create_state=True,
        now="2026-05-10T00:00:00+00:00",
    )
    parsed = urlparse(result["authorizationUrl"])
    query = parse_qs(parsed.query)
    serialized = json.dumps(result)

    assert result["validationStatus"] == "prepared"
    assert result["stateStored"] is True
    assert query["client_id"] == ["codex-client-id"]
    assert query["state"] == [result["state"]]
    assert query["nonce"] == [result["nonce"]]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [result["pkce"]["codeChallenge"]]
    assert "client_secret" not in parsed.query
    assert "CODEX_CLIENT_SECRET" not in serialized
    assert oauth_dry_run.OAUTH_STATE_PATH.exists()


def test_oauth_authorization_preview_with_missing_client_env_warns_and_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-state" / "states.jsonl")

    result = oauth_dry_run.build_authorization_url(
        {
            "providerId": "codex-oauth",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "clientIdEnvVar": "MISSING_OAUTH_CLIENT_ID",
        },
        redirect_uri="http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        create_state=False,
        now="2026-05-10T00:00:00+00:00",
    )

    assert result["validationStatus"] == "preview"
    assert result["stateStored"] is False
    assert result["clientIdStatus"] == "missing_env"
    assert "missing_client_id_env_var" in result["reasonCodes"]
    assert not oauth_dry_run.OAUTH_STATE_PATH.exists()


def test_oauth_callback_dry_run_accepts_valid_state_without_token_exchange_or_code_storage(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setenv("CODEX_CLIENT_ID", "codex-client-id")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-state" / "states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    prepared = oauth_dry_run.build_authorization_url(
        {
            "providerId": "codex-oauth",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "clientIdEnvVar": "CODEX_CLIENT_ID",
        },
        redirect_uri="http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        create_state=True,
        now="2026-05-10T00:00:00+00:00",
    )

    result = oauth_dry_run.validate_callback_dry_run(
        provider_id="codex-oauth",
        state=prepared["state"],
        code="authorization-code-must-not-persist",
        redirect_uri="http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        now="2026-05-10T00:01:00+00:00",
        audit=True,
    )
    replay = oauth_dry_run.validate_callback_dry_run(
        provider_id="codex-oauth",
        state=prepared["state"],
        code="second-code-must-not-persist",
        redirect_uri="http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        now="2026-05-10T00:02:00+00:00",
        audit=True,
    )
    audit_json = audit_path.read_text(encoding="utf-8")

    assert result["acceptedForTokenExchange"] is True
    assert result["tokenExchangePerformed"] is False
    assert result["stateStatus"] == "valid"
    assert replay["acceptedForTokenExchange"] is False
    assert replay["stateStatus"] == "replayed"
    assert "authorization-code-must-not-persist" not in audit_json
    assert "second-code-must-not-persist" not in audit_json
    assert operator_audit.validate_audit_log()["auditLogStatus"] == "valid"


def test_oauth_callback_dry_run_rejects_missing_expired_and_error_states(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_CLIENT_ID", "codex-client-id")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-state" / "states.jsonl")
    expired = oauth_dry_run.build_authorization_url(
        {
            "providerId": "codex-oauth",
            "authorizationUrl": "https://issuer.example.com/oauth/authorize",
            "clientIdEnvVar": "CODEX_CLIENT_ID",
        },
        redirect_uri="http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        create_state=True,
        ttl_seconds=60,
        now="2026-05-10T00:00:00+00:00",
    )

    missing = oauth_dry_run.validate_callback_dry_run(
        provider_id="codex-oauth",
        state="missing-state",
        code="code",
        now="2026-05-10T00:00:30+00:00",
    )
    expired_result = oauth_dry_run.validate_callback_dry_run(
        provider_id="codex-oauth",
        state=expired["state"],
        code="code",
        now="2026-05-10T00:02:00+00:00",
    )
    error_result = oauth_dry_run.validate_callback_dry_run(
        provider_id="codex-oauth",
        state=expired["state"],
        error="access_denied",
        now="2026-05-10T00:00:30+00:00",
    )

    assert missing["stateStatus"] == "missing"
    assert "missing_state" in missing["reasonCodes"]
    assert expired_result["stateStatus"] == "expired"
    assert "expired_state" in expired_result["reasonCodes"]
    assert error_result["acceptedForTokenExchange"] is False
    assert "provider_error" in error_result["reasonCodes"]


def test_oauth_dry_run_api_and_capability_registry_are_preview_only(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_CLIENT_ID", "codex-client-id")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-state" / "states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit" / "events.jsonl")

    registry = action_capabilities.list_action_requirements()
    assert registry["oauth_authorization_preview"]["riskLevel"] == "read_only"
    assert registry["oauth_authorization_prepare"]["riskLevel"] == "read_only"
    assert registry["oauth_callback_dry_run"]["riskLevel"] == "read_only"
    assert registry["oauth_state_validate"]["riskLevel"] == "read_only"

    preview = client.post(
        "/auth/oauth/authorization/preview",
        json={
            "oauthProviderMetadata": {
                "providerId": "codex-oauth",
                "authorizationUrl": "https://issuer.example.com/oauth/authorize",
                "clientIdEnvVar": "CODEX_CLIENT_ID",
            },
            "redirectUri": "http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        },
    )
    prepare = client.post(
        "/auth/oauth/authorization/prepare",
        json={
            "oauthProviderMetadata": {
                "providerId": "codex-oauth",
                "authorizationUrl": "https://issuer.example.com/oauth/authorize",
                "clientIdEnvVar": "CODEX_CLIENT_ID",
            },
            "redirectUri": "http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        },
    )
    prepared = prepare.json()
    callback = client.post(
        "/auth/oauth/callback/dry-run",
        json={
            "providerId": "codex-oauth",
            "state": prepared["state"],
            "code": "api-code-must-not-persist",
            "redirectUri": "http://127.0.0.1:8000/auth/oauth/callback/dry-run",
        },
    )
    status = client.get("/auth/oauth/dry-run/status").json()
    settings = client.get("/settings/status").json()["authFoundation"]

    assert preview.status_code == 200, preview.text
    assert preview.json()["stateStored"] is False
    assert prepare.status_code == 200, prepare.text
    assert prepared["stateStored"] is True
    assert callback.status_code == 200, callback.text
    assert callback.json()["acceptedForTokenExchange"] is True
    assert callback.json()["tokenExchangePerformed"] is False
    assert status["tokenExchangeEnabled"] is False
    assert settings["oauthDryRun"]["tokenExchangeEnabled"] is False
    assert "api-code-must-not-persist" not in operator_audit.AUDIT_LOG_PATH.read_text(encoding="utf-8")


def test_oauth_mock_token_exchange_redacts_tokens_and_returns_metadata_only(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    result = oauth_token_exchange.validate_mock_token_exchange(
        {
            "providerId": "codex-oauth",
            "correlationId": "oauth-dry-run-correlation",
            "authorizationCodePresent": True,
            "stateStatus": "valid",
            "redirectUri": "http://127.0.0.1:8000/auth/oauth/callback/dry-run",
            "codeVerifierPresent": True,
            "exchangeMode": "mock_only",
            "mockTokenResponse": {
                "access_token": "ya29.access-token-must-not-persist",
                "refresh_token": "refresh-token-must-not-persist",
                "id_token": "eyJheader.payload.signature",
                "expires_in": 3600,
                "scope": "openid profile email",
                "subject": "operator-subject-123",
            },
        },
        audit=True,
        operator_id="operator",
        auth_mode="oauth_ready",
        now="2026-05-10T01:00:00+00:00",
    )
    serialized = json.dumps(result, ensure_ascii=False)
    audit_json = audit_path.read_text(encoding="utf-8")

    assert result["exchangePerformed"] is True
    assert result["exchangeMode"] == "mock_only"
    assert result["accepted"] is True
    assert result["tokenResponsePresent"] is True
    assert result["accessTokenPresent"] is True
    assert result["refreshTokenPresent"] is True
    assert result["idTokenPresent"] is True
    assert result["rawTokenStored"] is False
    assert result["accountLinkPreview"]["subjectHash"]
    assert result["accountLinkPreview"]["tokenStorageEnabled"] is False
    assert result["redactionSummary"]["redactedTokenFieldCount"] == 3
    assert "operator-subject-123" not in serialized
    assert "ya29.access-token-must-not-persist" not in serialized
    assert "refresh-token-must-not-persist" not in serialized
    assert "eyJheader.payload.signature" not in serialized
    assert "ya29.access-token-must-not-persist" not in audit_json
    assert "refresh-token-must-not-persist" not in audit_json
    assert "eyJheader.payload.signature" not in audit_json
    assert operator_audit.validate_audit_log()["auditLogStatus"] == "valid"


def test_oauth_mock_token_exchange_rejects_invalid_fixture_and_live_exchange_is_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit" / "events.jsonl")

    invalid = oauth_token_exchange.validate_mock_token_exchange(
        {
            "providerId": "codex-oauth",
            "correlationId": "oauth-dry-run-correlation",
            "authorizationCodePresent": True,
            "stateStatus": "missing",
            "codeVerifierPresent": False,
            "exchangeMode": "mock_only",
            "mockTokenResponse": {},
        },
        audit=True,
    )
    live_disabled = oauth_token_exchange.live_token_exchange_disabled(
        {
            "providerId": "codex-oauth",
            "correlationId": "oauth-dry-run-correlation",
            "authorizationCodePresent": True,
            "stateStatus": "valid",
            "codeVerifierPresent": True,
            "exchangeMode": "live_not_supported",
        }
    )

    assert invalid["accepted"] is False
    assert invalid["exchangePerformed"] is False
    assert "invalid_state_status" in invalid["reasonCodes"]
    assert "missing_code_verifier" in invalid["reasonCodes"]
    assert "missing_token_response" in invalid["reasonCodes"]
    assert live_disabled["accepted"] is False
    assert live_disabled["exchangePerformed"] is False
    assert live_disabled["exchangeMode"] == "live_not_supported"
    assert "live_token_exchange_disabled" in live_disabled["reasonCodes"]


def test_oauth_token_exchange_api_is_mock_only_and_never_echoes_tokens(client, tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    registry = action_capabilities.list_action_requirements()
    assert registry["oauth_token_exchange_status"]["riskLevel"] == "read_only"
    assert registry["oauth_mock_exchange_validate"]["riskLevel"] == "read_only"
    assert registry["oauth_account_link_preview"]["riskLevel"] == "read_only"

    status = client.get("/auth/oauth/token-exchange/status")
    mock_response = client.post(
        "/auth/oauth/token-exchange/mock/validate",
        json={
            "providerId": "codex-oauth",
            "correlationId": "oauth-dry-run-correlation",
            "authorizationCodePresent": True,
            "stateStatus": "valid",
            "redirectUri": "http://127.0.0.1:8000/auth/oauth/callback/dry-run",
            "codeVerifierPresent": True,
            "exchangeMode": "mock_only",
            "mockTokenResponse": {
                "access_token": "ya29.api-access-token-must-not-persist",
                "refresh_token": "api-refresh-token-must-not-persist",
                "id_token": "eyJapi.header.payload",
                "expires_in": 600,
                "scope": "openid email",
                "sub": "api-user-subject",
            },
        },
    )
    live_response = client.post(
        "/auth/oauth/token-exchange/live",
        json={
            "providerId": "codex-oauth",
            "correlationId": "oauth-dry-run-correlation",
            "authorizationCodePresent": True,
            "stateStatus": "valid",
            "codeVerifierPresent": True,
            "exchangeMode": "live_not_supported",
        },
    )
    settings = client.get("/settings/status").json()["authFoundation"]
    combined = json.dumps([mock_response.json(), live_response.json(), settings], ensure_ascii=False)
    audit_json = audit_path.read_text(encoding="utf-8")

    assert status.status_code == 200
    assert status.json()["exchangeMode"] == "mock_only"
    assert status.json()["liveExchangeSupported"] is False
    assert mock_response.status_code == 200, mock_response.text
    assert mock_response.json()["accepted"] is True
    assert mock_response.json()["tokenExchangePerformed"] is False
    assert live_response.status_code == 200, live_response.text
    assert live_response.json()["exchangeMode"] == "live_not_supported"
    assert "live_token_exchange_disabled" in live_response.json()["reasonCodes"]
    assert settings["oauthTokenExchange"]["liveExchangeSupported"] is False
    assert settings["oauthTokenExchange"]["tokenStorageEnabled"] is False
    assert "oauth_live_exchange_disabled" in audit_json
    assert "ya29.api-access-token-must-not-persist" not in combined
    assert "api-refresh-token-must-not-persist" not in combined
    assert "eyJapi.header.payload" not in combined
    assert "api-user-subject" not in combined
    assert "ya29.api-access-token-must-not-persist" not in audit_json
    assert "api-refresh-token-must-not-persist" not in audit_json
    assert "eyJapi.header.payload" not in audit_json


def test_account_link_schema_rejects_token_like_fields() -> None:
    with pytest.raises(ValueError):
        OAuthAccountLinkMetadataResponse.model_validate(
            {
                "accountLinkId": "oauth-link-unsafe",
                "providerId": "codex-oauth",
                "operatorId": "operator",
                "subjectHash": "subject-hash",
                "accessToken": "ya29.schema-token-must-not-persist",
            }
        )


def test_account_link_schema_rejects_enabled_token_storage_or_live_exchange_flags() -> None:
    base_payload = {
        "accountLinkId": "oauth-link-unsafe-flag",
        "providerId": "codex-oauth",
        "operatorId": "operator",
        "subjectHash": "subject-hash",
    }

    with pytest.raises(ValueError):
        OAuthAccountLinkMetadataResponse.model_validate({**base_payload, "tokenStorageEnabled": True})

    with pytest.raises(ValueError):
        OAuthAccountLinkMetadataResponse.model_validate({**base_payload, "liveTokenExchangePerformed": True})


def test_save_mock_account_link_persists_metadata_only_and_is_deterministic(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)

    payload = {
        "providerId": "codex-oauth",
        "operatorId": "operator",
        "subjectHash": "7b9a0f87c4d4e0b4c6c1c8e2c8c8f7b3",
        "displaySubjectHint": "operator@example.com",
        "scopes": ["openid", "email"],
        "linkedAt": "2026-05-10T02:00:00+00:00",
        "lastValidatedAt": "2026-05-10T02:00:00+00:00",
        "source": "mock_exchange_validation",
    }

    first = oauth_account_links.save_mock_account_link(payload, now="2026-05-10T02:00:00+00:00")
    second = oauth_account_links.save_mock_account_link(payload, now="2026-05-10T02:05:00+00:00")
    saved_json = store_path.read_text(encoding="utf-8")
    links = oauth_account_links.list_account_links()["items"]

    assert first["accepted"] is True
    assert first["linkStored"] is True
    assert second["accountLink"]["accountLinkId"] == first["accountLink"]["accountLinkId"]
    assert len(links) == 1
    assert links[0]["subjectHash"] == payload["subjectHash"]
    assert links[0]["displaySubjectHint"] != "operator@example.com"
    assert links[0]["tokenStorageEnabled"] is False
    assert links[0]["liveTokenExchangePerformed"] is False
    assert "operator@example.com" not in saved_json
    assert "access_token" not in saved_json
    assert "refresh_token" not in saved_json
    assert "id_token" not in saved_json


def test_invalid_account_link_input_is_rejected_and_audited_without_secret(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    result = oauth_account_links.save_mock_account_link(
        {
            "providerId": "codex-oauth",
            "operatorId": "operator",
            "subjectHash": "safe-subject-hash",
            "displaySubjectHint": "operator@example.com",
            "scopes": ["openid"],
            "accessToken": "ya29.account-link-token-must-not-persist",
        },
        audit=True,
        operator_id="operator",
        auth_mode="oauth_ready",
    )
    audit_json = audit_path.read_text(encoding="utf-8")

    assert result["accepted"] is False
    assert result["linkStored"] is False
    assert "token_like_field_rejected" in result["reasonCodes"]
    assert not store_path.exists()
    assert "secret_policy_rejection" in audit_json
    assert "accessToken" in audit_json
    assert "operator@example.com" not in audit_json
    assert "ya29.account-link-token-must-not-persist" not in audit_json


def test_account_link_save_rejects_enabled_token_storage_or_live_exchange_flags(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)

    result = oauth_account_links.save_mock_account_link(
        {
            "providerId": "codex-oauth",
            "operatorId": "operator",
            "subjectHash": "safe-subject-hash",
            "scopes": ["openid"],
            "tokenStorageEnabled": True,
            "liveTokenExchangePerformed": True,
        },
        audit=True,
        operator_id="operator",
        auth_mode="oauth_ready",
    )
    audit_json = audit_path.read_text(encoding="utf-8")

    assert result["accepted"] is False
    assert result["linkStored"] is False
    assert "unsafe_account_link_flag_rejected" in result["reasonCodes"]
    assert not store_path.exists()
    assert "secret_policy_rejection" in audit_json
    assert "tokenStorageEnabled" in audit_json
    assert "liveTokenExchangePerformed" in audit_json


def test_account_link_health_and_session_binding_handle_missing_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "missing" / "links.jsonl")

    health = oauth_account_links.build_account_link_health_summary()
    identity = auth_foundation.operator_identity_for_user(None, settings=AuthSettings())
    binding = oauth_account_links.build_session_binding_preview(identity)

    assert health["status"] == "none"
    assert health["linkedAccountCount"] == 0
    assert health["mockLinkCount"] == 0
    assert health["liveLinkCount"] == 0
    assert health["tokenStorageEnabled"] is False
    assert binding["bindingStatus"] == "unbound"
    assert binding["operatorId"] == "anonymous"
    assert binding["accountLinkGrantsProviderExecution"] is False
    assert binding["providerExecution"] == "cli_only_acceptance_gated"


def test_mock_session_binding_shows_capabilities_without_provider_gate_bypass(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "oauth-account-links" / "links.jsonl")
    identity = auth_foundation.operator_identity_for_user(None, settings=AuthSettings())
    oauth_account_links.save_mock_account_link(
        {
            "providerId": "codex-oauth",
            "operatorId": identity["operatorId"],
            "subjectHash": "bound-subject-hash",
            "scopes": ["openid", "email"],
            "source": "mock_exchange_validation",
        },
        now="2026-05-10T02:00:00+00:00",
    )

    binding = oauth_account_links.build_session_binding_preview(identity)

    assert binding["bindingStatus"] == "local_mock_bound"
    assert binding["accountLinkCount"] == 1
    assert binding["providerIds"] == ["codex-oauth"]
    assert "view_dashboard" in binding["capabilities"]
    assert "accept_provider_risk" not in binding["capabilities"]
    assert binding["accountLinkGrantsProviderExecution"] is False
    assert binding["providerExecution"] == "cli_only_acceptance_gated"
    assert binding["tokenStorageEnabled"] is False
    assert binding["liveTokenExchangePerformed"] is False


def test_session_capability_policy_keeps_account_links_display_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "oauth-account-links" / "links.jsonl")
    identity = auth_foundation.operator_identity_for_user(None, settings=AuthSettings())
    oauth_account_links.save_mock_account_link(
        {
            "providerId": "codex-oauth",
            "operatorId": identity["operatorId"],
            "subjectHash": "policy-subject-hash",
            "scopes": ["openid", "email"],
            "source": "mock_exchange_validation",
        },
        now="2026-05-11T01:00:00+00:00",
    )

    policy = oauth_account_links.build_session_capability_policy(identity)

    assert policy["accountLinkInfluence"] == "display_only"
    assert policy["accountLinkCount"] == 1
    assert "view_dashboard" in policy["effectiveCapabilities"]
    assert policy["capabilitySources"]["view_dashboard"] == ["operator_role:local_operator"]
    assert "run_real_provider_cli" not in policy["effectiveCapabilities"]
    assert "accept_provider_risk" not in policy["effectiveCapabilities"]
    assert "run_real_provider_cli" in policy["deniedByPolicy"]
    assert "accept_provider_risk" in policy["deniedByPolicy"]
    assert "oauth_live_token_exchange" in policy["deniedByPolicy"]
    assert policy["accountLinkGrantsProviderExecution"] is False
    assert policy["providerExecution"] == "cli_only_acceptance_gated"


def test_account_link_health_detects_raw_store_violations_and_duplicates(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "providerId": "codex-oauth",
        "operatorId": "operator",
        "subjectHash": "duplicate-subject-hash",
        "scopes": ["openid"],
        "linkedAt": "2026-05-11T01:00:00+00:00",
        "lastValidatedAt": "2026-05-11T01:00:00+00:00",
        "tokenStorageEnabled": False,
        "liveTokenExchangePerformed": False,
        "source": "mock_exchange_validation",
        "status": "linked_mock",
        "reasonCodes": [],
        "warnings": [],
    }
    records = [
        {"accountLinkId": "oauth-link-active-a", **base},
        {"accountLinkId": "oauth-link-active-b", **base},
        {
            "accountLinkId": "oauth-link-revoked",
            **base,
            "subjectHash": "revoked-subject-hash",
            "status": "revoked_placeholder",
        },
        {
            "accountLinkId": "oauth-link-missing-subject",
            **base,
            "subjectHash": None,
        },
        {
            "accountLinkId": "oauth-link-token-leak",
            **base,
            "subjectHash": "leaky-subject-hash",
            "tokenStorageEnabled": True,
            "liveTokenExchangePerformed": True,
            "accessToken": "ya29.health-token-must-not-persist",
        },
    ]
    store_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    health = oauth_account_links.build_account_link_health_summary()
    serialized = json.dumps(health, ensure_ascii=False)

    assert health["healthStatus"] == "errors"
    assert health["activeLinkCount"] == 1
    assert health["mockLinkCount"] == 1
    assert health["invalidLinkCount"] == 2
    assert health["revokedPlaceholderCount"] == 1
    assert health["tokenLeakViolationCount"] == 1
    assert health["liveExchangeViolationCount"] == 1
    assert health["duplicateLinkCount"] == 1
    assert "missing_subject_hash" in health["reasonCodes"]
    assert "duplicate_account_link" in health["reasonCodes"]
    assert "token_like_account_link_field" in health["reasonCodes"]
    assert "ya29.health-token-must-not-persist" not in serialized


def test_session_binding_ignores_invalid_and_revoked_links() -> None:
    identity = {
        "operatorId": "operator",
        "role": "local_operator",
        "capabilities": ["view_dashboard", "edit_topic_config", "preview_run_plan"],
    }
    links = [
        {
            "accountLinkId": "oauth-link-active",
            "providerId": "codex-oauth",
            "operatorId": "operator",
            "subjectHash": "active-subject-hash",
            "scopes": ["openid"],
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "source": "mock_exchange_validation",
            "status": "linked_mock",
        },
        {
            "accountLinkId": "oauth-link-revoked",
            "providerId": "revoked-oauth",
            "operatorId": "operator",
            "subjectHash": "revoked-subject-hash",
            "scopes": ["openid"],
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "source": "mock_exchange_validation",
            "status": "revoked_placeholder",
        },
        {
            "accountLinkId": "oauth-link-invalid",
            "providerId": "invalid-oauth",
            "operatorId": "operator",
            "subjectHash": None,
            "scopes": ["openid"],
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "source": "mock_exchange_validation",
            "status": "linked_mock",
        },
    ]

    binding = oauth_account_links.build_session_binding_preview(identity, account_links=links)
    policy = oauth_account_links.build_session_capability_policy(identity, account_links=links)

    assert binding["bindingStatus"] == "local_mock_bound"
    assert binding["providerIds"] == ["codex-oauth"]
    assert binding["accountLinkCount"] == 1
    assert policy["accountLinkCount"] == 1
    assert policy["accountLinkInfluence"] == "display_only"


def test_revoke_account_link_placeholder_is_metadata_only_and_audited(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    identity = auth_foundation.operator_identity_for_user(None, settings=AuthSettings())
    saved = oauth_account_links.save_mock_account_link(
        {
            "providerId": "codex-oauth",
            "operatorId": identity["operatorId"],
            "subjectHash": "revocation-subject-hash",
            "scopes": ["openid"],
        },
        now="2026-05-11T01:00:00+00:00",
    )

    revoked = oauth_account_links.revoke_account_link_placeholder(
        saved["accountLink"]["accountLinkId"],
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode="local_operator",
        operator_identity=identity,
        now="2026-05-11T01:05:00+00:00",
    )
    missing = oauth_account_links.revoke_account_link_placeholder(
        "oauth-link-missing",
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode="local_operator",
        operator_identity=identity,
    )
    audit_json = audit_path.read_text(encoding="utf-8")

    assert revoked["accepted"] is True
    assert revoked["accountLink"]["status"] == "revoked_placeholder"
    assert revoked["health"]["activeLinkCount"] == 0
    assert revoked["health"]["revokedPlaceholderCount"] == 1
    assert revoked["sessionBinding"]["bindingStatus"] == "unbound"
    assert revoked["externalAuthCallPerformed"] is False
    assert revoked["tokenRevocationPerformed"] is False
    assert missing["accepted"] is False
    assert missing["validationStatus"] == "invalid"
    assert "account_link_not_found" in missing["reasonCodes"]
    assert "oauth_account_link_revoke_placeholder" in audit_json
    assert "externalAuthCallPerformed" in audit_json
    assert "tokenRevocationPerformed" in audit_json


def test_account_link_api_preview_save_status_and_binding_are_metadata_only(client, tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    payload = {
        "providerId": "codex-oauth",
        "operatorId": "anonymous",
        "subjectHash": "api-subject-hash",
        "displaySubjectHint": "api-user@example.com",
        "scopes": ["openid", "email"],
        "source": "mock_exchange_validation",
    }

    preview = client.post("/auth/oauth/account-links/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["linkStored"] is False
    assert not store_path.exists()

    save = client.post("/auth/oauth/account-links/mock", json={**payload, "accessToken": "ya29.api-link-token"})
    assert save.status_code == 400
    assert "ya29.api-link-token" not in save.text
    assert not store_path.exists()

    save = client.post("/auth/oauth/account-links/mock", json=payload)
    status = client.get("/auth/oauth/account-links/status")
    links = client.get("/auth/oauth/account-links")
    binding = client.get("/auth/oauth/session-binding/preview")
    settings = client.get("/settings/status").json()["authFoundation"]
    combined = json.dumps([save.json(), status.json(), links.json(), binding.json(), settings], ensure_ascii=False)
    audit_json = audit_path.read_text(encoding="utf-8")

    assert save.status_code == 200, save.text
    assert save.json()["accepted"] is True
    assert save.json()["accountLink"]["subjectHash"] == "api-subject-hash"
    assert status.json()["linkedAccountCount"] == 1
    assert status.json()["mockLinkCount"] == 1
    assert status.json()["liveLinkCount"] == 0
    assert links.json()["items"][0]["tokenStorageEnabled"] is False
    assert binding.json()["bindingStatus"] == "local_mock_bound"
    assert binding.json()["accountLinkGrantsProviderExecution"] is False
    assert settings["accountLinkStatus"]["linkedAccountCount"] == 1
    assert settings["sessionBinding"]["bindingStatus"] == "local_mock_bound"
    assert "api-user@example.com" not in combined
    assert "ya29.api-link-token" not in combined
    assert "ya29.api-link-token" not in audit_json


def test_account_link_policy_health_timeline_and_revoke_apis_are_safe(client, tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "oauth-account-links" / "links.jsonl"
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    registry = action_capabilities.list_action_requirements()

    assert registry["oauth_session_capability_policy"]["riskLevel"] == "read_only"
    assert registry["oauth_account_link_health"]["riskLevel"] == "read_only"
    assert registry["oauth_flow_timeline_status"]["riskLevel"] == "read_only"
    assert registry["oauth_account_link_revoke_placeholder"]["riskLevel"] == "config_write"

    payload = {
        "providerId": "codex-oauth",
        "operatorId": "anonymous",
        "subjectHash": "api-policy-subject-hash",
        "scopes": ["openid", "email"],
        "source": "mock_exchange_validation",
    }
    saved = client.post("/auth/oauth/account-links/mock", json=payload)
    health = client.get("/auth/oauth/account-links/health")
    policy = client.get("/auth/oauth/session-capability-policy")
    timeline = client.get("/auth/oauth/flow-timeline")
    revoke = client.post(f"/auth/oauth/account-links/{saved.json()['accountLink']['accountLinkId']}/revoke-placeholder")
    settings = client.get("/settings/status").json()["authFoundation"]
    combined = json.dumps([health.json(), policy.json(), timeline.json(), revoke.json(), settings], ensure_ascii=False)

    assert saved.status_code == 200, saved.text
    assert health.status_code == 200, health.text
    assert health.json()["healthStatus"] == "valid"
    assert health.json()["activeLinkCount"] == 1
    assert policy.status_code == 200, policy.text
    assert policy.json()["accountLinkInfluence"] == "display_only"
    assert "run_real_provider_cli" in policy.json()["deniedByPolicy"]
    assert "accept_provider_risk" in policy.json()["deniedByPolicy"]
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["realTokenExchangeEnabled"] is False
    assert timeline.json()["tokenStorageEnabled"] is False
    assert [step["stepId"] for step in timeline.json()["steps"]] == [
        "metadata_configured",
        "authorization_url_dry_run",
        "callback_dry_run",
        "mock_token_validation",
        "metadata_only_account_link",
        "real_token_exchange_disabled",
    ]
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["accountLink"]["status"] == "revoked_placeholder"
    assert revoke.json()["tokenRevocationPerformed"] is False
    assert settings["sessionCapabilityPolicy"]["accountLinkInfluence"] in {"display_only", "none"}
    assert settings["oauthFlowTimeline"]["realTokenExchangeEnabled"] is False
    assert "accessToken" not in combined
    assert "refreshToken" not in combined
    assert "idToken" not in combined


def test_account_link_actions_are_registered_and_audit_replay_flags_link_token_leak(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    registry = action_capabilities.list_action_requirements()

    assert registry["oauth_account_link_preview"]["riskLevel"] == "read_only"
    assert registry["oauth_account_link_save_mock"]["riskLevel"] == "config_write"
    assert registry["oauth_account_link_list"]["riskLevel"] == "read_only"
    assert registry["oauth_session_binding_preview"]["riskLevel"] == "read_only"
    assert registry["oauth_session_capability_policy"]["riskLevel"] == "read_only"
    assert registry["oauth_account_link_health"]["riskLevel"] == "read_only"
    assert registry["oauth_flow_timeline_status"]["riskLevel"] == "read_only"
    assert registry["oauth_account_link_revoke_placeholder"]["riskLevel"] == "config_write"

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "auditEventId": "account-link-token-leak-event",
                "timestamp": "2026-05-10T03:00:00+00:00",
                "operatorId": "operator",
                "authMode": "oauth_ready",
                "actionType": "oauth_account_link_save_mock",
                "capabilityRequired": "edit_topic_config",
                "capabilityPresent": True,
                "decision": "allowed",
                "reasonCodes": [],
                "targetType": "oauth_account_link",
                "targetId": "codex-oauth",
                "riskLevel": "config_write",
                "secretPresent": False,
                "metadata": {"accessToken": "ya29.raw-account-link-token"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    replay = operator_audit.validate_audit_log()

    assert replay["auditLogStatus"] == "errors"
    assert replay["secretLikeViolationCount"] == 1
    assert replay["reasonCounts"]["secret_like_audit_metadata"] == 1
    assert "ya29.raw-token-leak" not in json.dumps(replay, ensure_ascii=False)


def _oauth_live_readiness_metadata() -> dict:
    return {
        "providerId": "codex-oauth",
        "displayName": "Codex OAuth",
        "authorizationUrl": "https://auth.example.test/oauth/authorize",
        "tokenUrl": "https://auth.example.test/oauth/token",
        "clientIdEnvVar": "CODEX_OAUTH_CLIENT_ID",
        "clientSecretEnvVar": "CODEX_OAUTH_CLIENT_SECRET",
        "scopes": ["openid", "email"],
        "enabled": True,
    }


def _oauth_live_readiness_operator() -> dict:
    return {
        "operatorId": "local-internal-user",
        "displayName": "Local Internal Mode",
        "role": "local_operator",
        "capabilities": ["view_dashboard", "edit_topic_config", "preview_run_plan", "view_provider_readiness"],
    }


def test_oauth_live_exchange_readiness_schema_serializes_without_secret_values() -> None:
    readiness = OAuthLiveExchangeReadinessResponse.model_validate(
        {
            "readinessId": "oauth-readiness-codex-oauth",
            "providerId": "codex-oauth",
            "readinessStatus": "blocked",
            "liveExchangeEnabled": False,
            "tokenStorageEnabled": False,
            "checks": [
                {
                    "checkId": "client_id_env_var_present",
                    "label": "Client ID env var present",
                    "status": "failed",
                    "reasonCodes": ["missing_client_id_env_var"],
                    "warnings": ["CODEX_OAUTH_CLIENT_ID is not set; the value was not inspected."],
                }
            ],
            "failedChecks": ["client_id_env_var_present"],
            "warningChecks": [],
            "reasonCodes": ["missing_client_id_env_var", "live_token_exchange_disabled"],
            "safeNextSteps": ["Set required OAuth env vars outside persisted config before future enablement."],
            "forbiddenActions": ["Do not perform live token exchange in M11."],
        }
    )

    payload = readiness.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)

    assert payload["liveExchangeEnabled"] is False
    assert payload["tokenStorageEnabled"] is False
    assert payload["checks"][0]["checkId"] == "client_id_env_var_present"
    assert "client-secret-value" not in text
    assert "access_token" not in text
    assert "authorization_code" not in text


def test_oauth_live_exchange_readiness_passes_metadata_checks_with_env_presence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")

    result = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )
    checks = {check["checkId"]: check["status"] for check in result["checks"]}
    response_text = json.dumps(result, ensure_ascii=False)

    assert result["readinessStatus"] == "ready_for_future_live_exchange"
    assert result["liveExchangeEnabled"] is False
    assert result["tokenStorageEnabled"] is False
    assert checks["provider_metadata_complete"] == "passed"
    assert checks["authorization_url_valid"] == "passed"
    assert checks["token_url_present"] == "passed"
    assert checks["redirect_uri_valid"] == "passed"
    assert checks["client_id_env_var_present"] == "passed"
    assert checks["client_secret_env_var_present_or_not_required"] == "passed"
    assert checks["dashboard_execution_disabled"] == "passed"
    assert result["failedChecks"] == []
    assert "client-id-value" not in response_text
    assert "client-secret-value" not in response_text


def test_oauth_live_exchange_readiness_missing_env_is_blocked_without_exposing_value(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")

    result = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )
    checks = {check["checkId"]: check["status"] for check in result["checks"]}
    response_text = json.dumps(result, ensure_ascii=False)

    assert result["readinessStatus"] == "blocked"
    assert checks["client_id_env_var_present"] == "failed"
    assert "client_id_env_var_present" in result["failedChecks"]
    assert "missing_client_id_env_var" in result["reasonCodes"]
    assert "client-secret-value" not in response_text


def test_oauth_live_exchange_readiness_invalid_redirect_is_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")

    result = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="javascript:alert(1)",
        operator_identity=_oauth_live_readiness_operator(),
    )
    checks = {check["checkId"]: check["status"] for check in result["checks"]}

    assert result["readinessStatus"] == "blocked"
    assert checks["redirect_uri_valid"] == "failed"
    assert "redirect_uri_valid" in result["failedChecks"]
    assert "invalid_redirect_uri" in result["reasonCodes"]


def test_oauth_live_exchange_readiness_blocks_account_link_token_violation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    store_path = tmp_path / "account-links.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", store_path)
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    store_path.write_text(
        json.dumps(
            {
                "accountLinkId": "unsafe-link",
                "providerId": "codex-oauth",
                "operatorId": "local-internal-user",
                "subjectHash": "unsafe-subject-hash",
                "scopes": ["openid"],
                "linkedAt": "2026-05-12T00:00:00+00:00",
                "lastValidatedAt": "2026-05-12T00:00:00+00:00",
                "tokenStorageEnabled": False,
                "liveTokenExchangePerformed": True,
                "accessToken": "ya29.unsafe-link-token",
                "source": "mock_exchange_validation",
                "status": "linked_mock",
                "reasonCodes": [],
                "warnings": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )
    checks = {check["checkId"]: check["status"] for check in result["checks"]}
    response_text = json.dumps(result, ensure_ascii=False)

    assert result["readinessStatus"] == "blocked"
    assert checks["account_link_health_valid"] == "failed"
    assert "account_link_health_valid" in result["failedChecks"]
    assert "account_link_health_error" in result["reasonCodes"]
    assert result["liveExchangeEnabled"] is False
    assert result["tokenStorageEnabled"] is False
    assert "ya29.unsafe-link-token" not in response_text


def test_oauth_live_exchange_readiness_blocks_unsafe_session_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    unsafe_policy = oauth_account_links.build_session_capability_policy(_oauth_live_readiness_operator())
    unsafe_policy["accountLinkGrantsProviderExecution"] = True
    unsafe_policy["effectiveCapabilities"] = [*unsafe_policy["effectiveCapabilities"], "run_real_provider_cli", "accept_provider_risk"]

    result = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
        session_policy=unsafe_policy,
    )
    checks = {check["checkId"]: check["status"] for check in result["checks"]}

    assert result["readinessStatus"] == "blocked"
    assert checks["session_policy_valid"] == "failed"
    assert "session_policy_valid" in result["failedChecks"]
    assert "unsafe_session_capability_policy" in result["reasonCodes"]


def test_oauth_live_exchange_readiness_api_and_audit_are_read_only(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    audit_path = tmp_path / "operator-audit.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    registry = action_capabilities.list_action_requirements()

    assert registry["oauth_live_exchange_readiness"]["riskLevel"] == "read_only"
    assert registry["oauth_live_exchange_readiness"]["writeCapable"] is False

    response = client.post(
        "/auth/oauth/live-exchange/readiness/preview",
        json={
            "oauthProviderMetadata": _oauth_live_readiness_metadata(),
            "redirectUri": "https://dashboard.example.test/auth/callback",
        },
    )
    status = client.get("/auth/oauth/live-exchange/readiness")
    settings = client.get("/settings/status").json()["authFoundation"]
    response_text = json.dumps([response.json(), status.json(), settings], ensure_ascii=False)
    audit_text = audit_path.read_text(encoding="utf-8")

    assert response.status_code == 200, response.text
    assert response.json()["liveExchangeEnabled"] is False
    assert response.json()["tokenStorageEnabled"] is False
    assert response.json()["externalAuthCallPerformed"] is False
    assert status.status_code == 200, status.text
    assert settings["oauthLiveExchangeReadiness"]["liveExchangeEnabled"] is False
    assert settings["oauthLiveExchangeReadiness"]["tokenStorageEnabled"] is False
    assert "oauth_live_exchange_readiness" in audit_text
    assert "client-id-value" not in response_text
    assert "client-secret-value" not in response_text
    assert "client-id-value" not in audit_text
    assert "client-secret-value" not in audit_text


def test_oauth_readiness_remediation_schema_serializes_without_secret_values() -> None:
    summary = OAuthReadinessRemediationSummaryResponse.model_validate(
        {
            "providerId": "codex-oauth",
            "readinessStatus": "blocked",
            "remediationStatus": "blocking",
            "blockingRemediationCount": 1,
            "warningRemediationCount": 0,
            "infoRemediationCount": 1,
            "items": [
                {
                    "remediationId": "remediation-codex-oauth-client-id",
                    "providerId": "codex-oauth",
                    "readinessCheck": "client_id_env_var_present",
                    "reasonCode": "missing_client_id_env_var",
                    "severity": "blocking",
                    "category": "env_var",
                    "operatorAction": "Set CODEX_OAUTH_CLIENT_ID outside persisted config.",
                    "safeCommandOrConfigHint": "$env:CODEX_OAUTH_CLIENT_ID='<set outside git>'",
                    "envVarNames": ["CODEX_OAUTH_CLIENT_ID"],
                    "docsAnchor": "env-vars",
                    "forbiddenActions": ["Do not commit OAuth secret values."],
                    "canBeResolvedWithoutCodeChange": True,
                    "requiresRestart": True,
                    "secretHandlingNote": "Env var values must stay outside committed files and API responses.",
                }
            ],
            "safeNextSteps": ["Set required OAuth env vars outside persisted config before future enablement."],
            "forbiddenActions": ["Do not perform live token exchange in M11.1."],
        }
    )
    payload = summary.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)

    assert payload["blockingRemediationCount"] == 1
    assert payload["items"][0]["envVarNames"] == ["CODEX_OAUTH_CLIENT_ID"]
    assert "client-secret-value" not in text
    assert "access_token" not in text
    assert "authorization_code" not in text


def test_oauth_readiness_remediation_missing_env_shows_name_not_value(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )

    remediation = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=_oauth_live_readiness_metadata())
    item = next(item for item in remediation["items"] if item["reasonCode"] == "missing_client_id_env_var")
    response_text = json.dumps(remediation, ensure_ascii=False)

    assert remediation["remediationStatus"] == "blocking"
    assert item["severity"] == "blocking"
    assert item["category"] == "env_var"
    assert item["envVarNames"] == ["CODEX_OAUTH_CLIENT_ID"]
    assert item["canBeResolvedWithoutCodeChange"] is True
    assert item["requiresRestart"] is True
    assert "client-secret-value" not in response_text


def test_oauth_readiness_remediation_invalid_redirect_is_blocking(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="javascript:alert(1)",
        operator_identity=_oauth_live_readiness_operator(),
    )

    remediation = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=_oauth_live_readiness_metadata())
    item = next(item for item in remediation["items"] if item["reasonCode"] == "invalid_redirect_uri")

    assert item["severity"] == "blocking"
    assert item["category"] == "redirect_uri"
    assert item["docsAnchor"] == "redirect-uri"
    assert item["canBeResolvedWithoutCodeChange"] is True
    assert "HTTPS" in item["operatorAction"]


def test_oauth_readiness_remediation_labels_live_disabled_as_future_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )

    remediation = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=_oauth_live_readiness_metadata())
    item = next(item for item in remediation["items"] if item["reasonCode"] == "live_token_exchange_disabled")

    assert item["severity"] == "info"
    assert item["category"] == "future_live_exchange"
    assert item["canBeResolvedWithoutCodeChange"] is False
    assert item["requiresRestart"] is False
    assert "intentional" in item["operatorAction"].lower()
    assert any("Do not perform live token exchange" in action for action in item["forbiddenActions"])


def test_oauth_readiness_remediation_maps_audit_and_account_link_warnings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
        audit_health={"auditLogStatus": "warnings"},
        account_link_health={"healthStatus": "warnings"},
    )

    remediation = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=_oauth_live_readiness_metadata())
    by_reason = {item["reasonCode"]: item for item in remediation["items"]}

    assert remediation["remediationStatus"] == "warnings"
    assert by_reason["audit_health_warnings"]["severity"] == "warning"
    assert by_reason["audit_health_warnings"]["category"] == "audit_health"
    assert by_reason["account_link_health_warnings"]["severity"] == "warning"
    assert by_reason["account_link_health_warnings"]["category"] == "account_link_health"


def test_oauth_readiness_remediation_order_is_deterministic(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="javascript:alert(1)",
        operator_identity=_oauth_live_readiness_operator(),
        audit_health={"auditLogStatus": "warnings"},
    )

    first = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=_oauth_live_readiness_metadata())
    second = oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=_oauth_live_readiness_metadata())

    assert [item["reasonCode"] for item in first["items"]] == [item["reasonCode"] for item in second["items"]]
    assert [item["severity"] for item in first["items"]][:4] == ["blocking", "blocking", "blocking", "warning"]


def test_oauth_readiness_remediation_api_is_read_only_and_secret_free(client, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    audit_path = tmp_path / "operator-audit.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    registry = action_capabilities.list_action_requirements()

    assert registry["oauth_readiness_remediation"]["riskLevel"] == "read_only"
    assert registry["oauth_readiness_remediation"]["writeCapable"] is False

    response = client.post(
        "/auth/oauth/live-exchange/readiness/remediation/preview",
        json={
            "oauthProviderMetadata": _oauth_live_readiness_metadata(),
            "redirectUri": "https://dashboard.example.test/auth/callback",
        },
    )
    status = client.get("/auth/oauth/live-exchange/readiness/remediation")
    settings = client.get("/settings/status").json()["authFoundation"]
    response_text = json.dumps([response.json(), status.json(), settings], ensure_ascii=False)

    assert response.status_code == 200, response.text
    assert response.json()["blockingRemediationCount"] >= 1
    assert response.json()["items"][0]["envVarNames"] == ["CODEX_OAUTH_CLIENT_ID"]
    assert status.status_code == 200, status.text
    assert settings["oauthReadinessRemediation"]["blockingRemediationCount"] >= 1
    assert not audit_path.exists()
    assert "CODEX_OAUTH_CLIENT_ID" in response_text
    assert "client-secret-value" not in response_text
    assert response.json()["liveExchangeEnabled"] is False
    assert response.json()["tokenStorageEnabled"] is False


def test_oauth_preflight_package_schema_serializes_without_secret_values() -> None:
    package = OAuthPreflightPackageResponse.model_validate(
        {
            "preflightId": "oauth-preflight-codex-oauth",
            "providerId": "codex-oauth",
            "generatedAt": "2026-05-12T00:00:00+00:00",
            "readinessStatus": "blocked",
            "remediationSummary": {
                "providerId": "codex-oauth",
                "readinessStatus": "blocked",
                "remediationStatus": "blocking",
                "blockingRemediationCount": 1,
                "warningRemediationCount": 0,
                "infoRemediationCount": 1,
                "liveExchangeEnabled": False,
                "tokenStorageEnabled": False,
                "items": [],
                "safeNextSteps": ["Set CODEX_OAUTH_CLIENT_ID outside persisted config."],
                "forbiddenActions": ["Do not perform live token exchange in M11.2."],
            },
            "auditHealthStatus": "valid",
            "accountLinkHealthStatus": "valid",
            "tokenStorageEnabled": False,
            "liveExchangeEnabled": False,
            "envVarNames": ["CODEX_OAUTH_CLIENT_ID", "CODEX_OAUTH_CLIENT_SECRET"],
            "envVarValuesExposed": False,
            "forbiddenActions": ["Do not perform live token exchange in M11.2."],
            "safeNextSteps": ["Review the operator acceptance checklist before future enablement."],
            "requiredOperatorConfirmations": ["confirmed_no_secret_commit"],
            "acceptanceReady": False,
            "reasonCodes": ["token_storage_disabled", "live_token_exchange_disabled"],
            "warnings": ["Preflight package is read-only."],
        }
    )
    payload = package.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)

    assert payload["envVarNames"] == ["CODEX_OAUTH_CLIENT_ID", "CODEX_OAUTH_CLIENT_SECRET"]
    assert payload["envVarValuesExposed"] is False
    assert payload["tokenStorageEnabled"] is False
    assert payload["liveExchangeEnabled"] is False
    assert "client-secret-value" not in text
    assert "access_token" not in text
    assert "authorization_code" not in text


def test_oauth_preflight_package_contains_env_names_only_and_health_summaries(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", tmp_path / "operator-audit.jsonl")

    package = oauth_live_readiness.build_preflight_package(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )
    response_text = json.dumps(package, ensure_ascii=False)

    assert package["providerId"] == "codex-oauth"
    assert package["readinessStatus"] == "blocked"
    assert package["remediationSummary"]["blockingRemediationCount"] >= 1
    assert package["auditHealthStatus"] in {"empty", "valid"}
    assert package["accountLinkHealthStatus"] == "empty"
    assert package["tokenStorageEnabled"] is False
    assert package["liveExchangeEnabled"] is False
    assert package["envVarNames"] == ["CODEX_OAUTH_CLIENT_ID", "CODEX_OAUTH_CLIENT_SECRET"]
    assert package["envVarValuesExposed"] is False
    assert "confirmed_no_secret_commit" in package["requiredOperatorConfirmations"]
    assert "Do not perform live token exchange in M11.2." in package["forbiddenActions"]
    assert "client-secret-value" not in response_text


def test_oauth_operator_acceptance_checklist_missing_confirmations_is_invalid() -> None:
    checklist = oauth_live_readiness.validate_operator_acceptance_checklist(
        {
            "providerId": "codex-oauth",
            "confirmedNoSecretCommit": True,
            "confirmedEnvVarsConfigured": False,
            "confirmedRedirectUriRegistered": False,
            "confirmedAuditHealthReviewed": True,
            "confirmedTokenStoragePolicy": True,
            "confirmedDashboardExecutionDisabled": True,
            "confirmedProviderAcceptanceGateIndependent": True,
            "acceptedByOperatorId": "local-internal-user",
        }
    )

    assert checklist["acceptanceValid"] is False
    assert "confirmed_env_vars_configured" in checklist["missingConfirmations"]
    assert "confirmed_redirect_uri_registered" in checklist["missingConfirmations"]
    assert "missing_operator_confirmations" in checklist["reasonCodes"]
    assert checklist["liveExchangeStillDisabled"] is True
    assert checklist["liveExchangeEnabled"] is False
    assert checklist["tokenStorageEnabled"] is False


def test_oauth_operator_acceptance_checklist_full_confirmation_still_disables_live_exchange() -> None:
    checklist = OAuthOperatorAcceptanceChecklistResponse.model_validate(
        oauth_live_readiness.validate_operator_acceptance_checklist(
            {
                "providerId": "codex-oauth",
                "confirmedNoSecretCommit": True,
                "confirmedEnvVarsConfigured": True,
                "confirmedRedirectUriRegistered": True,
                "confirmedAuditHealthReviewed": True,
                "confirmedTokenStoragePolicy": True,
                "confirmedDashboardExecutionDisabled": True,
                "confirmedProviderAcceptanceGateIndependent": True,
                "acceptedByOperatorId": "local-internal-user",
                "acceptedAt": "2026-05-12T00:00:00+00:00",
                "clientSecretValue": "client-secret-value",
            }
        )
    ).model_dump(mode="json")
    response_text = json.dumps(checklist, ensure_ascii=False)

    assert checklist["acceptanceValid"] is True
    assert checklist["missingConfirmations"] == []
    assert checklist["liveExchangeStillDisabled"] is True
    assert checklist["liveExchangeEnabled"] is False
    assert checklist["tokenStorageEnabled"] is False
    assert "live_exchange_still_disabled" in checklist["reasonCodes"]
    assert "client-secret-value" not in response_text


def test_oauth_preflight_api_is_read_only_and_secret_free(client, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", tmp_path / "account-links.jsonl")
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    audit_path = tmp_path / "operator-audit.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    registry = action_capabilities.list_action_requirements()

    assert registry["oauth_preflight_package"]["riskLevel"] == "read_only"
    assert registry["oauth_preflight_package"]["writeCapable"] is False
    assert registry["oauth_operator_acceptance_checklist"]["riskLevel"] == "read_only"
    assert registry["oauth_operator_acceptance_checklist"]["writeCapable"] is False

    package_response = client.post(
        "/auth/oauth/preflight/package/preview",
        json={
            "oauthProviderMetadata": _oauth_live_readiness_metadata(),
            "redirectUri": "https://dashboard.example.test/auth/callback",
        },
    )
    checklist_response = client.post(
        "/auth/oauth/preflight/checklist/preview",
        json={
            "providerId": "codex-oauth",
            "confirmedNoSecretCommit": True,
            "confirmedEnvVarsConfigured": True,
            "confirmedRedirectUriRegistered": True,
            "confirmedAuditHealthReviewed": True,
            "confirmedTokenStoragePolicy": True,
            "confirmedDashboardExecutionDisabled": True,
            "confirmedProviderAcceptanceGateIndependent": True,
            "acceptedByOperatorId": "local-internal-user",
            "clientSecretValue": "client-secret-value",
        },
    )
    status = client.get("/auth/oauth/preflight/package")
    settings = client.get("/settings/status").json()["authFoundation"]
    response_text = json.dumps([package_response.json(), checklist_response.json(), status.json(), settings], ensure_ascii=False)

    assert package_response.status_code == 200, package_response.text
    assert package_response.json()["envVarNames"] == ["CODEX_OAUTH_CLIENT_ID", "CODEX_OAUTH_CLIENT_SECRET"]
    assert package_response.json()["envVarValuesExposed"] is False
    assert package_response.json()["liveExchangeEnabled"] is False
    assert package_response.json()["tokenStorageEnabled"] is False
    assert checklist_response.status_code == 200, checklist_response.text
    assert checklist_response.json()["acceptanceValid"] is True
    assert checklist_response.json()["liveExchangeStillDisabled"] is True
    assert checklist_response.json()["liveExchangeEnabled"] is False
    assert status.status_code == 200, status.text
    assert settings["oauthPreflightPackage"]["liveExchangeEnabled"] is False
    assert settings["oauthPreflightPackage"]["tokenStorageEnabled"] is False
    assert not audit_path.exists()
    assert "CODEX_OAUTH_CLIENT_ID" in response_text
    assert "client-secret-value" not in response_text


def _accepted_oauth_pilot_checklist() -> dict:
    return {
        "providerId": "codex-oauth",
        "confirmedNoSecretCommit": True,
        "confirmedEnvVarsConfigured": True,
        "confirmedRedirectUriRegistered": True,
        "confirmedAuditHealthReviewed": True,
        "confirmedTokenStoragePolicy": True,
        "confirmedDashboardExecutionDisabled": True,
        "confirmedProviderAcceptanceGateIndependent": True,
        "acceptedByOperatorId": "local-internal-user",
        "acceptedAt": "2026-05-12T00:00:00+00:00",
    }


def _oauth_live_pilot_request(**overrides) -> dict:
    payload = {
        "providerId": "codex-oauth",
        "correlationId": "pilot-correlation-1",
        "oauthProviderMetadata": _oauth_live_readiness_metadata(),
        "redirectUri": "https://dashboard.example.test/auth/callback",
        "authorizationCodePresent": True,
        "authorizationCode": "authorization-code-value",
        "stateStatus": "valid",
        "codeVerifierPresent": True,
        "codeVerifier": "pkce-code-verifier-value",
        "operatorId": "local-internal-user",
        "operatorAcceptancePresent": True,
        "liveExchangeRequested": True,
        "tokenStorageRequested": False,
        "dryRunOnly": False,
        "checklist": _accepted_oauth_pilot_checklist(),
    }
    payload.update(overrides)
    return payload


def _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch) -> tuple:
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-id-value")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_SECRET", "client-secret-value")
    account_link_path = tmp_path / "account-links.jsonl"
    audit_path = tmp_path / "operator-audit.jsonl"
    monkeypatch.setattr(oauth_account_links, "ACCOUNT_LINK_STORE_PATH", account_link_path)
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", tmp_path / "oauth-states.jsonl")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    return account_link_path, audit_path


def test_oauth_live_pilot_status_schema_is_disabled_and_secret_free() -> None:
    status = OAuthLiveExchangePilotStatusResponse.model_validate(
        {
            "pilotStatus": "disabled_by_default",
            "liveExchangeEnabledByDefault": False,
            "tokenStorageEnabled": False,
            "dashboardExecutionEnabled": False,
            "tokenStorageStillDisabled": True,
            "dashboardExecutionStillDisabled": True,
            "explicitOperatorAcceptanceRequired": True,
            "tokenValuesReturned": False,
            "tokenValuesAudited": False,
            "livePilotLeakCheckStatus": "not_run",
            "lastPilotStatus": None,
            "reasonCodes": ["live_exchange_pilot_disabled_by_default", "token_storage_disabled"],
            "warnings": ["Pilot is available only through explicit API/CLI acceptance."],
        }
    ).model_dump(mode="json")
    text = json.dumps(status, ensure_ascii=False)

    assert status["liveExchangeEnabledByDefault"] is False
    assert status["tokenStorageEnabled"] is False
    assert status["dashboardExecutionEnabled"] is False
    assert status["tokenStorageStillDisabled"] is True
    assert status["dashboardExecutionStillDisabled"] is True
    assert status["livePilotLeakCheckStatus"] == "not_run"
    assert status["tokenValuesReturned"] is False
    assert status["tokenValuesAudited"] is False
    assert "access_token" not in text
    assert "authorization-code-value" not in text


def test_oauth_live_pilot_blocks_missing_acceptance_before_http_call(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(operatorAcceptancePresent=False),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["gateStatus"] == "blocked"
    assert result["tokenEndpointCalled"] is False
    assert result["liveExchangePerformed"] is False
    assert "missing_operator_acceptance" in result["reasonCodes"]
    assert calls == []


def test_oauth_live_pilot_blocks_invalid_checklist_before_http_call(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []
    checklist = _accepted_oauth_pilot_checklist()
    checklist["confirmedEnvVarsConfigured"] = False

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(checklist=checklist),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["gateStatus"] == "blocked"
    assert result["tokenEndpointCalled"] is False
    assert "operator_checklist_invalid" in result["reasonCodes"]
    assert calls == []


def test_oauth_live_pilot_blocks_non_https_endpoint_before_http_call(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []
    metadata = _oauth_live_readiness_metadata()
    metadata["tokenUrl"] = "http://auth.example.test/oauth/token"

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(oauthProviderMetadata=metadata),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["gateStatus"] == "blocked"
    assert result["tokenEndpointCalled"] is False
    assert "non_https_token_endpoint" in result["reasonCodes"]
    assert calls == []


def test_oauth_live_pilot_dry_run_calls_no_http_client(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(dryRunOnly=True),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["gateStatus"] == "dry_run_only"
    assert result["tokenEndpointCalled"] is False
    assert result["liveExchangePerformed"] is False
    assert "dry_run_only" in result["reasonCodes"]
    assert calls == []


def test_oauth_live_pilot_success_redacts_tokens_and_saves_metadata_only(tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []

    def fake_http_client(request: dict) -> dict:
        calls.append(request)
        assert request["tokenUrl"] == "https://auth.example.test/oauth/token"
        assert request["form"]["code"] == "authorization-code-value"
        return {
            "statusCode": 200,
            "json": {
                "access_token": "live-access-token-secret",
                "refresh_token": "live-refresh-token-secret",
                "id_token": "live-id-token-secret",
                "expires_in": 3600,
                "scope": "openid email",
                "idTokenClaims": {"sub": "live-subject-123"},
            },
        }

    result = OAuthLiveExchangePilotResultResponse.model_validate(
        oauth_token_exchange.perform_live_exchange_pilot(
            _oauth_live_pilot_request(),
            http_client=fake_http_client,
            audit=True,
            operator_identity=_oauth_live_readiness_operator(),
        )
    ).model_dump(mode="json")
    response_text = json.dumps(result, ensure_ascii=False)
    audit_text = audit_path.read_text(encoding="utf-8")
    account_link_text = account_link_path.read_text(encoding="utf-8")

    assert result["gateStatus"] == "passed"
    assert result["liveExchangePerformed"] is True
    assert result["tokenEndpointCalled"] is True
    assert result["tokenResponsePresent"] is True
    assert result["accessTokenPresent"] is True
    assert result["refreshTokenPresent"] is True
    assert result["idTokenPresent"] is True
    assert result["rawTokenStored"] is False
    assert result["tokenValuesReturned"] is False
    assert result["tokenValuesAudited"] is False
    assert result["accountLinkCreated"] is True
    assert result["accountLinkMetadataOnly"] is True
    assert result["accountLink"]["subjectHash"]
    assert result["accountLink"]["status"] == "linked_live_pilot"
    assert calls
    for text in [response_text, audit_text, account_link_text]:
        assert "live-access-token-secret" not in text
        assert "live-refresh-token-secret" not in text
        assert "live-id-token-secret" not in text
        assert "authorization-code-value" not in text
        assert "live-subject-123" not in text
    assert "subjectHash" in account_link_text
    assert "tokenStorageEnabled" in account_link_text


def test_oauth_live_pilot_subject_missing_returns_no_metadata_link(tmp_path, monkeypatch) -> None:
    account_link_path, _audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 200,
            "json": {
                "access_token": "live-access-token-secret",
                "expires_in": 3600,
                "scope": "openid email",
            },
        }

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(),
        http_client=fake_http_client,
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["liveExchangePerformed"] is True
    assert result["tokenEndpointCalled"] is True
    assert result["accountLinkCreated"] is False
    assert "missing_subject_hash_for_account_link" in result["reasonCodes"]
    assert not account_link_path.exists()


def test_oauth_live_pilot_api_requires_explicit_acceptance_and_never_returns_tokens(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 200,
            "json": {
                "access_token": "api-live-access-token-secret",
                "refresh_token": "api-live-refresh-token-secret",
                "id_token": "api-live-id-token-secret",
                "scope": "openid email",
                "idTokenClaims": {"sub": "api-live-subject"},
            },
        }

    monkeypatch.setattr(oauth_token_exchange, "LIVE_TOKEN_HTTP_CLIENT", fake_http_client)
    registry = action_capabilities.list_action_requirements()
    blocked_response = client.post(
        "/auth/oauth/live-exchange/pilot",
        json=_oauth_live_pilot_request(operatorAcceptancePresent=False),
    )
    allowed_response = client.post("/auth/oauth/live-exchange/pilot", json=_oauth_live_pilot_request())
    status_response = client.get("/auth/oauth/live-exchange/pilot/status")
    settings = client.get("/settings/status").json()["authFoundation"]
    response_text = json.dumps([blocked_response.json(), allowed_response.json(), status_response.json(), settings], ensure_ascii=False)
    audit_text = audit_path.read_text(encoding="utf-8")
    account_link_text = account_link_path.read_text(encoding="utf-8")

    assert registry["oauth_live_exchange_pilot"]["writeCapable"] is True
    assert registry["oauth_live_exchange_pilot_status"]["writeCapable"] is False
    assert blocked_response.status_code == 200, blocked_response.text
    assert blocked_response.json()["gateStatus"] == "blocked"
    assert blocked_response.json()["tokenEndpointCalled"] is False
    assert allowed_response.status_code == 200, allowed_response.text
    assert allowed_response.json()["gateStatus"] == "passed"
    assert allowed_response.json()["tokenValuesReturned"] is False
    assert allowed_response.json()["tokenValuesAudited"] is False
    assert status_response.json()["dashboardExecutionEnabled"] is False
    assert settings["oauthLiveExchangePilot"]["tokenStorageEnabled"] is False
    assert settings["oauthLiveExchangePilot"]["dashboardExecutionEnabled"] is False
    for text in [response_text, audit_text, account_link_text]:
        assert "api-live-access-token-secret" not in text
        assert "api-live-refresh-token-secret" not in text
        assert "api-live-id-token-secret" not in text
        assert "authorization-code-value" not in text
        assert "api-live-subject" not in text


def test_audit_replay_flags_raw_token_leak_in_fixture_event(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "operator-audit" / "events.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "auditEventId": "token-leak-event",
                "timestamp": "2026-05-10T01:00:00+00:00",
                "operatorId": "operator",
                "authMode": "oauth_ready",
                "actionType": "oauth_mock_exchange_validate",
                "capabilityRequired": "view_dashboard",
                "capabilityPresent": True,
                "decision": "allowed",
                "reasonCodes": [],
                "targetType": "oauth_provider",
                "targetId": "codex-oauth",
                "riskLevel": "read_only",
                "secretPresent": False,
                "metadata": {"accessToken": "ya29.raw-token-leak"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    replay = operator_audit.validate_audit_log()

    assert replay["auditLogStatus"] == "errors"
    assert replay["secretLikeViolationCount"] == 1
    assert replay["reasonCounts"]["secret_like_audit_metadata"] == 1


def test_oauth_live_pilot_leak_scanner_detects_secret_patterns_without_echoing_values() -> None:
    payload = {
        "nested": [
            {"access_token": "ya29.fake-access-token-sentinel"},
            {"refreshToken": "refresh-token-secret-sentinel"},
            {"id_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"},
            {"authorizationCode": "authorization-code-secret-sentinel"},
            {"clientSecret": "client-secret-sentinel"},
            {"authorization": "Bearer bearer-token-secret-sentinel"},
            {"rawTokenResponse": {"body": "raw-response-token-secret-sentinel"}},
        ]
    }

    scan = oauth_token_exchange.scan_oauth_live_pilot_leaks(payload)
    text = json.dumps(scan, ensure_ascii=False)

    assert scan["leakDetected"] is True
    assert scan["leakCount"] >= 7
    assert "access_token_like_value" in scan["reasonCodes"]
    assert "refresh_token_like_value" in scan["reasonCodes"]
    assert "id_token_like_value" in scan["reasonCodes"]
    assert "authorization_code_field" in scan["reasonCodes"]
    assert "client_secret_field" in scan["reasonCodes"]
    assert "bearer_token_like_value" in scan["reasonCodes"]
    assert "raw_token_response_field" in scan["reasonCodes"]
    assert "ya29.fake-access-token-sentinel" not in text
    assert "refresh-token-secret-sentinel" not in text
    assert "authorization-code-secret-sentinel" not in text
    assert "client-secret-sentinel" not in text
    assert "bearer-token-secret-sentinel" not in text
    assert "raw-response-token-secret-sentinel" not in text


def test_oauth_live_token_client_rejects_unexpected_content_type_without_raw_body(monkeypatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return b'{"access_token":"content-type-secret-token"}'

    monkeypatch.setattr(oauth_token_exchange, "urlopen", lambda _request, timeout: FakeResponse())

    result = oauth_token_exchange._default_live_token_http_client(
        {
            "tokenUrl": "https://auth.example.test/oauth/token",
            "form": {"grant_type": "authorization_code"},
            "timeoutSeconds": 1,
            "maxResponseBytes": 128,
        }
    )
    text = json.dumps(result, ensure_ascii=False)

    assert result["error"] == "unexpected_token_response_content_type"
    assert result["json"] == {}
    assert "content-type-secret-token" not in text


def test_oauth_live_token_client_rejects_timeout_without_raw_body(monkeypatch) -> None:
    def raise_timeout(_request, timeout):
        raise TimeoutError("fake timeout carrying timeout-secret-token")

    monkeypatch.setattr(oauth_token_exchange, "urlopen", raise_timeout)

    result = oauth_token_exchange._default_live_token_http_client(
        {
            "tokenUrl": "https://auth.example.test/oauth/token",
            "form": {"grant_type": "authorization_code"},
            "timeoutSeconds": 1,
            "maxResponseBytes": 128,
        }
    )
    text = json.dumps(result, ensure_ascii=False)

    assert result["error"] == "token_endpoint_timeout"
    assert result["json"] == {}
    assert "timeout-secret-token" not in text


def test_oauth_live_token_client_rejects_malformed_and_oversized_responses(monkeypatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

    request = {
        "tokenUrl": "https://auth.example.test/oauth/token",
        "form": {"grant_type": "authorization_code"},
        "timeoutSeconds": 1,
        "maxResponseBytes": 128,
    }

    monkeypatch.setattr(oauth_token_exchange, "urlopen", lambda _request, timeout: FakeResponse(b"{not-json-secret-token"))
    malformed = oauth_token_exchange._default_live_token_http_client(request)

    monkeypatch.setattr(oauth_token_exchange, "urlopen", lambda _request, timeout: FakeResponse(b'{"access_token":"oversized-secret-token"}'))
    oversized = oauth_token_exchange._default_live_token_http_client({**request, "maxResponseBytes": 12})
    text = json.dumps([malformed, oversized], ensure_ascii=False)

    assert malformed["error"] == "token_response_not_json"
    assert oversized["error"] == "token_response_too_large"
    assert "not-json-secret-token" not in text
    assert "oversized-secret-token" not in text


def test_oauth_live_pilot_non_2xx_returns_redacted_error_summary(tmp_path, monkeypatch) -> None:
    _account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 400,
            "json": {
                "error": "invalid_grant",
                "access_token": "non-2xx-access-token-secret",
                "authorizationCode": "non-2xx-code-secret",
            },
        }

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(),
        http_client=fake_http_client,
        audit=True,
        operator_identity=_oauth_live_readiness_operator(),
    )
    text = json.dumps([result, audit_path.read_text(encoding="utf-8")], ensure_ascii=False)

    assert result["gateStatus"] == "failed"
    assert result["liveExchangePerformed"] is False
    assert result["tokenEndpointCalled"] is True
    assert "token_endpoint_error" in result["reasonCodes"]
    assert "non-2xx-access-token-secret" not in text
    assert "non-2xx-code-secret" not in text


def test_oauth_live_pilot_rejects_missing_access_token_without_account_link(tmp_path, monkeypatch) -> None:
    account_link_path, _audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 200,
            "json": {
                "expires_in": 3600,
                "scope": "openid email",
                "idTokenClaims": {"sub": "subject-without-access-token"},
            },
        }

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(),
        http_client=fake_http_client,
        operator_identity=_oauth_live_readiness_operator(),
    )
    text = json.dumps(result, ensure_ascii=False)

    assert result["gateStatus"] == "failed"
    assert result["liveExchangePerformed"] is False
    assert result["tokenEndpointCalled"] is True
    assert result["tokenResponsePresent"] is True
    assert result["accountLinkCreated"] is False
    assert "missing_access_token" in result["reasonCodes"]
    assert "subject-without-access-token" not in text
    assert not account_link_path.exists()


def test_oauth_live_pilot_redacts_extra_secret_fields_from_result_and_audit(tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 200,
            "json": {
                "access_token": "extra-field-access-token-secret",
                "refresh_token": "extra-field-refresh-token-secret",
                "expires_in": 3600,
                "scope": "openid email",
                "idTokenClaims": {"sub": "extra-field-subject"},
                "device_secret": "extra-device-secret-token",
                "api_key": "extra-api-key-secret",
            },
        }

    result = oauth_token_exchange.perform_live_exchange_pilot(
        _oauth_live_pilot_request(),
        http_client=fake_http_client,
        audit=True,
        operator_identity=_oauth_live_readiness_operator(),
    )
    response_text = json.dumps(result, ensure_ascii=False)
    audit_text = audit_path.read_text(encoding="utf-8")
    account_link_text = account_link_path.read_text(encoding="utf-8")

    assert result["gateStatus"] == "passed"
    assert "secret_like_token_response_field_redacted" in result["reasonCodes"]
    assert "nested_token_response_leak_detected" in result["redactionSummary"]["reasonCodes"]
    for text in [response_text, audit_text, account_link_text]:
        assert "extra-field-access-token-secret" not in text
        assert "extra-field-refresh-token-secret" not in text
        assert "extra-device-secret-token" not in text
        assert "extra-api-key-secret" not in text
        assert "extra-field-subject" not in text


def test_oauth_live_pilot_evidence_package_is_secret_free_and_env_names_only(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    package = oauth_live_evidence.build_live_pilot_evidence_package(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )
    text = json.dumps(package, ensure_ascii=False)

    assert package["providerId"] == "codex-oauth"
    assert package["tokenStorageEnabled"] is False
    assert package["dashboardExecutionEnabled"] is False
    assert package["livePilotEnabledDefault"] is False
    assert package["externalCallPerformed"] is False
    assert package["tokenValuesPresent"] is False
    assert package["secretValuesPresent"] is False
    assert package["leakCheckStatus"] in {"clean", "warnings"}
    assert "CODEX_OAUTH_CLIENT_ID" in package["envVarNames"]
    assert "CODEX_OAUTH_CLIENT_SECRET" in package["envVarNames"]
    assert "client-id-value" not in text
    assert "client-secret-value" not in text
    assert "authorization-code-value" not in text
    assert "access_token" not in text
    assert "refresh_token" not in text
    assert "id_token" not in text


def test_oauth_live_pilot_replay_verification_passes_clean_package(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    checklist = oauth_live_readiness.validate_operator_acceptance_checklist(
        _accepted_oauth_pilot_checklist(),
        operator_identity=_oauth_live_readiness_operator(),
    )
    package = oauth_live_evidence.build_live_pilot_evidence_package(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
        audit_health={"auditLogStatus": "valid"},
        account_link_health={"healthStatus": "valid"},
        checklist=checklist,
    )

    verification = oauth_live_evidence.verify_live_pilot_evidence_package(package)

    assert verification["verificationStatus"] == "valid"
    assert verification["leakCount"] == 0
    assert verification["secretLikeViolationCount"] == 0
    assert verification["missingRequiredEvidenceCount"] == 0
    assert verification["failedChecks"] == []


def test_oauth_live_pilot_replay_verification_flags_fixture_leak_without_echoing_value(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    package = oauth_live_evidence.build_live_pilot_evidence_package(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
    )
    package["evidenceItems"].append(
        {
            "itemId": "bad-fixture",
            "category": "leak_check",
            "label": "Bad fixture",
            "status": "failed",
            "summary": "ya29.fixture-leak-secret",
            "reasonCodes": [],
            "warnings": [],
        }
    )

    verification = oauth_live_evidence.verify_live_pilot_evidence_package(package)
    text = json.dumps(verification, ensure_ascii=False)

    assert verification["verificationStatus"] == "failed"
    assert verification["leakCount"] >= 1
    assert verification["secretLikeViolationCount"] >= 1
    assert "access_token_like_value" in verification["reasonCodes"]
    assert "ya29.fixture-leak-secret" not in text


def test_oauth_live_pilot_replay_verification_flags_missing_required_evidence() -> None:
    verification = oauth_live_evidence.verify_live_pilot_evidence_package(
        {
            "providerId": "codex-oauth",
            "tokenStorageEnabled": False,
            "livePilotEnabledDefault": False,
            "dashboardExecutionEnabled": False,
            "externalCallPerformed": False,
            "tokenValuesPresent": False,
            "secretValuesPresent": False,
        }
    )

    assert verification["verificationStatus"] == "failed"
    assert verification["missingRequiredEvidenceCount"] >= 4
    assert "missing_required_evidence" in verification["reasonCodes"]


def test_oauth_live_pilot_evidence_api_is_read_only_and_secret_free(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    state_path = tmp_path / "oauth-states.jsonl"
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", state_path)

    package_response = client.post(
        "/auth/oauth/live-exchange/evidence/package/preview",
        json={
            "oauthProviderMetadata": _oauth_live_readiness_metadata(),
            "redirectUri": "https://dashboard.example.test/auth/callback",
        },
    )
    verify_response = client.post("/auth/oauth/live-exchange/evidence/replay/preview", json=package_response.json())
    status_response = client.get("/auth/oauth/live-exchange/evidence/package")
    registry = action_capabilities.list_action_requirements()
    text = json.dumps([package_response.json(), verify_response.json(), status_response.json()], ensure_ascii=False)

    assert package_response.status_code == 200, package_response.text
    assert verify_response.status_code == 200, verify_response.text
    assert status_response.status_code == 200, status_response.text
    assert package_response.json()["externalCallPerformed"] is False
    assert package_response.json()["tokenStorageEnabled"] is False
    assert verify_response.json()["leakCount"] == 0
    assert registry["oauth_live_pilot_evidence_package"]["writeCapable"] is False
    assert registry["oauth_live_pilot_replay_verification"]["writeCapable"] is False
    assert "CODEX_OAUTH_CLIENT_ID" in text
    assert "client-id-value" not in text
    assert "client-secret-value" not in text
    assert "authorization-code-value" not in text
    assert not account_link_path.exists()
    assert not audit_path.exists()
    assert not state_path.exists()


def _oauth_real_smoke_evidence(tmp_path, monkeypatch, *, accepted: bool = True) -> dict:
    checklist = oauth_live_readiness.validate_operator_acceptance_checklist(
        _accepted_oauth_pilot_checklist() if accepted else {"providerId": "codex-oauth"},
        operator_identity=_oauth_live_readiness_operator(),
    )
    return oauth_live_evidence.build_live_pilot_evidence_package(
        provider_metadata=_oauth_live_readiness_metadata(),
        redirect_uri="https://dashboard.example.test/auth/callback",
        operator_identity=_oauth_live_readiness_operator(),
        audit_health={"auditLogStatus": "valid"},
        account_link_health={"healthStatus": "valid"},
        checklist=checklist,
    )


def _oauth_real_smoke_request(tmp_path, monkeypatch, **overrides) -> dict:
    payload = {
        "providerId": "codex-oauth",
        "operatorId": "local-internal-user",
        "oauthProviderMetadata": _oauth_live_readiness_metadata(),
        "redirectUri": "https://dashboard.example.test/auth/callback",
        "authorizationCodePresent": True,
        "authorizationCode": "real-smoke-authorization-code",
        "stateStatus": "valid",
        "codeVerifierPresent": True,
        "codeVerifier": "real-smoke-code-verifier",
        "testProviderConfigured": True,
        "explicitOperatorAcceptance": True,
        "operatorAcceptancePresent": True,
        "liveExchangeRequested": True,
        "tokenStorageRequested": False,
        "dashboardTriggered": False,
        "dryRunOnly": False,
        "checklist": _accepted_oauth_pilot_checklist(),
        "evidencePackage": _oauth_real_smoke_evidence(tmp_path, monkeypatch),
    }
    payload.update(overrides)
    return payload


def test_oauth_real_provider_smoke_missing_provider_config_skips_without_http(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []

    result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, testProviderConfigured=False),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["smokeStatus"] == "skipped"
    assert result["externalAuthCallPerformed"] is False
    assert result["metadataLinkCreated"] is False
    assert "real_provider_smoke_not_configured" in result["reasonCodes"]
    assert calls == []


def test_oauth_real_provider_smoke_missing_acceptance_blocks_before_http(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []

    result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, explicitOperatorAcceptance=False, operatorAcceptancePresent=False),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert result["smokeStatus"] == "blocked"
    assert result["externalAuthCallPerformed"] is False
    assert "missing_explicit_operator_acceptance" in result["reasonCodes"]
    assert calls == []


def test_oauth_real_provider_smoke_dashboard_context_and_token_storage_block_before_http(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []

    dashboard_result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, dashboardTriggered=True),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )
    storage_result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, tokenStorageRequested=True),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )

    assert dashboard_result["smokeStatus"] == "blocked"
    assert storage_result["smokeStatus"] == "blocked"
    assert "dashboard_triggered_forbidden" in dashboard_result["reasonCodes"]
    assert "token_storage_requested" in storage_result["reasonCodes"]
    assert calls == []


def test_oauth_real_provider_smoke_invalid_evidence_replay_blocks_before_http(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    calls: list = []
    evidence = _oauth_real_smoke_evidence(tmp_path, monkeypatch)
    evidence["evidenceItems"].append(
        {
            "itemId": "bad-leak",
            "category": "leak_check",
            "label": "Bad leak",
            "status": "failed",
            "summary": "ya29.real-smoke-leak",
            "reasonCodes": [],
            "warnings": [],
        }
    )

    result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, evidencePackage=evidence),
        http_client=lambda request: calls.append(request),
        operator_identity=_oauth_live_readiness_operator(),
    )
    text = json.dumps(result, ensure_ascii=False)

    assert result["smokeStatus"] == "blocked"
    assert result["externalAuthCallPerformed"] is False
    assert "evidence_replay_not_valid" in result["reasonCodes"]
    assert "ya29.real-smoke-leak" not in text
    assert calls == []


def test_oauth_real_provider_smoke_success_redacts_tokens_and_creates_metadata_link(tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 200,
            "json": {
                "access_token": "real-smoke-access-token-secret",
                "refresh_token": "real-smoke-refresh-token-secret",
                "id_token": "real-smoke-id-token-secret",
                "scope": "openid email",
                "idTokenClaims": {"sub": "real-smoke-subject"},
            },
        }

    request_payload = _oauth_real_smoke_request(tmp_path, monkeypatch)
    result = oauth_token_exchange.perform_real_provider_smoke(
        request_payload,
        http_client=fake_http_client,
        audit=True,
        operator_identity=_oauth_live_readiness_operator(),
    )
    replay_after_smoke = oauth_live_evidence.verify_live_pilot_evidence_package(request_payload["evidencePackage"])
    response_text = json.dumps(result, ensure_ascii=False)
    audit_text = audit_path.read_text(encoding="utf-8")
    account_link_text = account_link_path.read_text(encoding="utf-8")

    assert result["smokeStatus"] == "completed_redacted"
    assert result["externalAuthCallPerformed"] is True
    assert result["metadataLinkCreated"] is True
    assert result["rawTokenStored"] is False
    assert result["tokenValuesReturned"] is False
    assert result["tokenValuesAudited"] is False
    assert result["evidenceReplayStatus"] in {"valid", "warnings"}
    assert replay_after_smoke["verificationStatus"] == "valid"
    for text in [response_text, audit_text, account_link_text]:
        assert "real-smoke-access-token-secret" not in text
        assert "real-smoke-refresh-token-secret" not in text
        assert "real-smoke-id-token-secret" not in text
        assert "real-smoke-authorization-code" not in text
        assert "real-smoke-subject" not in text
    assert "subjectHash" in account_link_text


def test_oauth_real_provider_smoke_missing_subject_creates_no_link(tmp_path, monkeypatch) -> None:
    account_link_path, _audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {"statusCode": 200, "json": {"access_token": "real-smoke-access-token-secret", "scope": "openid email"}}

    result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch),
        http_client=fake_http_client,
        operator_identity=_oauth_live_readiness_operator(),
    )
    text = json.dumps(result, ensure_ascii=False)

    assert result["smokeStatus"] == "completed_redacted"
    assert result["metadataLinkCreated"] is False
    assert "missing_subject_hash_for_account_link" in result["reasonCodes"]
    assert "real-smoke-access-token-secret" not in text
    assert not account_link_path.exists()


def test_oauth_real_provider_smoke_api_status_and_block_are_secret_free(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    registry = action_capabilities.list_action_requirements()

    status_response = client.get("/auth/oauth/live-exchange/smoke/real/status")
    blocked_response = client.post(
        "/auth/oauth/live-exchange/smoke/real",
        json=_oauth_real_smoke_request(tmp_path, monkeypatch, explicitOperatorAcceptance=False, operatorAcceptancePresent=False),
    )
    text = json.dumps([status_response.json(), blocked_response.json()], ensure_ascii=False)

    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["smokeStatus"] == "not_configured"
    assert status_response.json()["liveSmokeEnabledByDefault"] is False
    assert status_response.json()["dashboardExecutionEnabled"] is False
    assert blocked_response.status_code == 200, blocked_response.text
    assert blocked_response.json()["smokeStatus"] == "blocked"
    assert blocked_response.json()["externalAuthCallPerformed"] is False
    assert "missing_explicit_operator_acceptance" in blocked_response.json()["reasonCodes"]
    assert registry["oauth_real_provider_smoke_status"]["writeCapable"] is False
    assert registry["oauth_real_provider_smoke"]["writeCapable"] is True
    assert "real-smoke-authorization-code" not in text
    assert "client-secret-value" not in text
    assert not account_link_path.exists()
    assert audit_path.exists()
    assert "real-smoke-authorization-code" not in audit_path.read_text(encoding="utf-8")


def test_oauth_smoke_evidence_freeze_preview_captures_skipped_result_without_writes(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    state_path = tmp_path / "oauth-states.jsonl"
    smoke_result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, testProviderConfigured=False),
        http_client=lambda _request: pytest.fail("missing provider config must not call HTTP"),
        operator_identity=_oauth_live_readiness_operator(),
    )

    response = client.post(
        "/auth/oauth/live-exchange/smoke/evidence-freeze/preview",
        json={
            "smokeResult": smoke_result,
            "evidencePackage": _oauth_real_smoke_evidence(tmp_path, monkeypatch),
            "mockedHttpUsed": False,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previewOnly"] is True
    assert body["exportWritten"] is False
    assert body["smokeResult"]["smokeStatus"] == "skipped_not_configured"
    assert body["smokeResult"]["externalAuthCallPerformed"] is False
    assert body["smokeResult"]["metadataOnlyLink"] is True
    assert body["smokeResult"]["tokenValuesPresent"] is False
    assert body["smokeResult"]["secretValuesPresent"] is False
    assert body["smokeResult"]["rawResponseStored"] is False
    assert body["replaySummary"]["verificationStatus"] == "valid"
    assert not account_link_path.exists()
    assert not audit_path.exists()
    assert not state_path.exists()


def test_oauth_smoke_evidence_freeze_preview_captures_mocked_success_secret_free(client, tmp_path, monkeypatch) -> None:
    account_link_path, _audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    def fake_http_client(_request: dict) -> dict:
        return {
            "statusCode": 200,
            "json": {
                "access_token": "freeze-access-token-secret",
                "refresh_token": "freeze-refresh-token-secret",
                "id_token": "freeze-id-token-secret",
                "scope": "openid email",
                "idTokenClaims": {"sub": "freeze-subject"},
            },
        }

    smoke_result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch),
        http_client=fake_http_client,
        operator_identity=_oauth_live_readiness_operator(),
    )
    response = client.post(
        "/auth/oauth/live-exchange/smoke/evidence-freeze/preview",
        json={
            "smokeResult": smoke_result,
            "evidencePackage": _oauth_real_smoke_evidence(tmp_path, monkeypatch),
            "mockedHttpUsed": True,
        },
    )
    text = json.dumps(response.json(), ensure_ascii=False)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["freezeStatus"] == "valid"
    assert body["smokeResult"]["smokeStatus"] == "completed_mocked"
    assert body["smokeResult"]["mockedHttpUsed"] is True
    assert body["smokeResult"]["accountLinkCreated"] is True
    assert body["smokeResult"]["metadataOnlyLink"] is True
    assert body["replaySummary"]["verificationStatus"] == "valid"
    assert "subjectHash" in account_link_path.read_text(encoding="utf-8")
    for sentinel in [
        "freeze-access-token-secret",
        "freeze-refresh-token-secret",
        "freeze-id-token-secret",
        "real-smoke-authorization-code",
        "freeze-subject",
    ]:
        assert sentinel not in text


def test_oauth_smoke_evidence_freeze_flags_token_like_fixture_without_echo(client, tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    smoke_result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch, testProviderConfigured=False),
        operator_identity=_oauth_live_readiness_operator(),
    )
    smoke_result["nested"] = {"access_token": "ya29.freeze-leak-sentinel"}

    response = client.post(
        "/auth/oauth/live-exchange/smoke/evidence-freeze/preview",
        json={
            "smokeResult": smoke_result,
            "evidencePackage": _oauth_real_smoke_evidence(tmp_path, monkeypatch),
            "mockedHttpUsed": False,
        },
    )
    text = json.dumps(response.json(), ensure_ascii=False)

    assert response.status_code == 200, response.text
    assert response.json()["freezeStatus"] == "failed"
    assert response.json()["replaySummary"]["leakCount"] >= 1
    assert "smoke_result_leak_detected" in response.json()["reasonCodes"]
    assert "ya29.freeze-leak-sentinel" not in text


def test_oauth_smoke_evidence_freeze_captures_failed_smoke_redacted(client, tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    failed_result = oauth_token_exchange.perform_real_provider_smoke(
        _oauth_real_smoke_request(tmp_path, monkeypatch),
        http_client=lambda _request: {"statusCode": 500, "json": {}, "error": "mock_token_endpoint_error"},
        operator_identity=_oauth_live_readiness_operator(),
    )

    response = client.post(
        "/auth/oauth/live-exchange/smoke/evidence-freeze/preview",
        json={
            "smokeResult": failed_result,
            "evidencePackage": _oauth_real_smoke_evidence(tmp_path, monkeypatch),
            "mockedHttpUsed": True,
        },
    )
    text = json.dumps(response.json(), ensure_ascii=False)

    assert response.status_code == 200, response.text
    assert response.json()["smokeResult"]["smokeStatus"] == "failed_redacted"
    assert response.json()["smokeResult"]["rawResponseStored"] is False
    assert response.json()["smokeResult"]["tokenValuesPresent"] is False
    assert response.json()["smokeResult"]["secretValuesPresent"] is False
    assert "real-smoke-authorization-code" not in text


def test_oauth_smoke_result_summary_api_is_read_only(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    state_path = tmp_path / "oauth-states.jsonl"
    registry = action_capabilities.list_action_requirements()

    response = client.get("/auth/oauth/live-exchange/smoke/result/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latestSmokeStatus"] == "no_result"
    assert body["tokenStorageEnabled"] is False
    assert body["dashboardExecutionEnabled"] is False
    assert registry["oauth_smoke_result_summary"]["writeCapable"] is False
    assert registry["oauth_smoke_evidence_freeze_preview"]["writeCapable"] is False
    assert registry["oauth_smoke_replay_verification"]["writeCapable"] is False
    assert not account_link_path.exists()
    assert not audit_path.exists()
    assert not state_path.exists()


def test_provider_orchestration_safety_matrix_serializes_key_rows(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    identity = _oauth_live_readiness_operator()
    oauth_account_links.save_mock_account_link(
        {
            "providerId": "codex-oauth",
            "operatorId": identity["operatorId"],
            "subjectHash": "subject-hash-only",
            "scopes": ["openid"],
        },
        operator_identity=identity,
    )

    from app.services import provider_orchestration

    matrix = provider_orchestration.build_provider_orchestration_safety_matrix(
        operator_identity=identity,
        provider_readiness_summary={
            "items": [
                {
                    "providerId": "stub",
                    "providerType": "stub",
                    "readinessStatus": "ready_stub",
                    "liveAllowed": False,
                    "operatorAcceptanceRequired": False,
                    "reasonCodes": [],
                    "warnings": [],
                },
                {
                    "providerId": "command",
                    "providerType": "command",
                    "readinessStatus": "not_configured",
                    "liveAllowed": False,
                    "operatorAcceptanceRequired": True,
                    "reasonCodes": ["missing_command_template"],
                    "warnings": ["Command provider has no argument-list command configured."],
                },
            ]
        },
    )
    rows = {row["actionId"]: row for row in matrix["actions"]}

    assert matrix["dashboardExecutionAllowed"] is False
    assert matrix["tokenStorageAllowed"] is False
    assert matrix["accountLinkInfluence"] == "display_only"
    assert rows["stub_provider_review"]["availability"] == "available"
    assert rows["stub_provider_review"]["requiresM4Validation"] is True
    assert rows["manual_provider_output_import"]["requiresM4Validation"] is True
    assert rows["command_provider_live_cli"]["availability"] == "cli_only"
    assert rows["command_provider_live_cli"]["requiredOperatorAcceptance"] is True
    assert "run_real_provider_cli" in rows["command_provider_live_cli"]["requiredCapabilities"]
    assert rows["dashboard_provider_execution_forbidden"]["availability"] == "blocked"
    assert rows["dashboard_provider_execution_forbidden"]["dashboardAllowed"] is False
    assert rows["oauth_live_exchange_pilot"]["defaultEnabled"] is False
    assert rows["oauth_live_exchange_pilot"]["dashboardAllowed"] is False
    assert rows["optional_real_oauth_smoke"]["requiresEvidenceReplay"] is True
    assert rows["optional_real_oauth_smoke"]["tokenStorageAllowed"] is False
    assert "run_real_provider_cli" not in matrix["effectiveCapabilities"]
    assert "accept_provider_risk" not in matrix["effectiveCapabilities"]


def test_provider_orchestration_safety_matrix_api_is_read_only(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    registry = action_capabilities.list_action_requirements()

    response = client.get("/auth/provider-orchestration/safety-matrix")

    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["actionId"]: row for row in body["actions"]}
    assert body["dashboardExecutionAllowed"] is False
    assert body["tokenStorageAllowed"] is False
    assert rows["dashboard_provider_execution_forbidden"]["availability"] == "blocked"
    assert rows["dashboard_provider_execution_forbidden"]["dashboardAllowed"] is False
    assert rows["real_ai_provider_review_future_disabled"]["availability"] == "future_disabled"
    assert registry["provider_orchestration_safety_matrix"]["writeCapable"] is False
    assert not account_link_path.exists()
    assert not audit_path.exists()


def _provider_orchestration_readiness_fixture() -> dict:
    return {
        "items": [
            {
                "providerId": "stub",
                "providerType": "stub",
                "readinessStatus": "ready_stub",
                "liveAllowed": False,
                "operatorAcceptanceRequired": False,
                "reasonCodes": [],
                "warnings": [],
            },
            {
                "providerId": "fixture-command",
                "providerType": "command",
                "profileRole": "fixture",
                "readinessStatus": "dry_run_only",
                "liveAllowed": False,
                "operatorAcceptanceRequired": False,
                "reasonCodes": ["fixture_command_local_only"],
                "warnings": [],
            },
            {
                "providerId": "command",
                "providerType": "command",
                "profileRole": "generic_command",
                "readinessStatus": "not_configured",
                "liveAllowed": False,
                "operatorAcceptanceRequired": True,
                "reasonCodes": ["missing_command_template"],
                "warnings": ["Command provider has no argument-list command configured."],
            },
        ]
    }


def test_provider_orchestration_dry_run_plan_serializes_stub_workflow(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    from app.services import provider_orchestration

    plan = provider_orchestration.build_provider_orchestration_dry_run_plan(
        workflow_type="review_with_stub_provider",
        topic_id="large-language-models",
        operator_identity=_oauth_live_readiness_operator(),
        provider_readiness_summary=_provider_orchestration_readiness_fixture(),
        now="2026-05-13T00:00:00+00:00",
    )
    replay = provider_orchestration.verify_provider_orchestration_plan(plan)
    steps = {step["actionId"]: step for step in plan["steps"]}

    assert plan["workflowType"] == "review_with_stub_provider"
    assert plan["topicId"] == "large-language-models"
    assert plan["externalCallPlanned"] is False
    assert plan["dashboardExecutionPlanned"] is False
    assert plan["tokenStoragePlanned"] is False
    assert plan["previewOnlyStepCount"] >= 1
    assert steps["stub_provider_review"]["plannedMode"] in {"dry_run", "fixture_only"}
    assert steps["stub_provider_review"]["requiresM4Validation"] is True
    assert steps["stub_provider_review"]["externalCallPlanned"] is False
    assert steps["stub_provider_review"]["dashboardExecutionAllowed"] is False
    assert steps["m4_grounded_validation"]["plannedMode"] == "read_only"
    assert replay["replayStatus"] == "valid"
    assert replay["forbiddenActionCount"] == 0
    assert replay["missingGateCount"] == 0


def test_provider_orchestration_replay_detects_forbidden_and_missing_gates(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    from app.services import provider_orchestration

    unsafe_plan = {
        "planId": "unsafe-provider-plan",
        "workflowType": "command_provider_live_cli_preview",
        "generatedAt": "2026-05-13T00:00:00+00:00",
        "dashboardExecutionPlanned": True,
        "externalCallPlanned": True,
        "tokenStoragePlanned": True,
        "accountLinkGrantsProviderExecution": True,
        "steps": [
            {
                "stepId": "unsafe-dashboard-provider",
                "actionId": "dashboard_provider_execution_forbidden",
                "actionLabel": "Dashboard provider execution",
                "providerType": "dashboard",
                "plannedMode": "dry_run",
                "requiredCapabilities": [],
                "requiredAcceptance": True,
                "requiresM4Validation": True,
                "requiresOAuthPreflight": False,
                "requiresEvidenceReplay": False,
                "dashboardExecutionAllowed": True,
                "externalCallPlanned": False,
                "tokenStorageAllowed": False,
                "expectedArtifacts": [],
                "reasonCodes": [],
                "safeNextSteps": [],
            },
            {
                "stepId": "unsafe-live-command",
                "actionId": "command_provider_live_cli",
                "actionLabel": "Command provider live CLI",
                "providerType": "command",
                "plannedMode": "cli_only",
                "requiredCapabilities": ["run_real_provider_cli"],
                "requiredAcceptance": False,
                "requiresM4Validation": False,
                "requiresOAuthPreflight": False,
                "requiresEvidenceReplay": False,
                "dashboardExecutionAllowed": False,
                "externalCallPlanned": True,
                "tokenStorageAllowed": True,
                "expectedArtifacts": ["provider-generated-draft"],
                "reasonCodes": ["missing_provider_config"],
                "safeNextSteps": [],
            },
            {
                "stepId": "unsafe-oauth-smoke",
                "actionId": "optional_real_oauth_smoke",
                "actionLabel": "Optional real OAuth smoke",
                "providerType": "oauth_real_smoke",
                "plannedMode": "cli_only",
                "requiredCapabilities": ["edit_topic_config"],
                "requiredAcceptance": True,
                "requiresM4Validation": False,
                "requiresOAuthPreflight": True,
                "requiresEvidenceReplay": True,
                "dashboardExecutionAllowed": False,
                "externalCallPlanned": False,
                "tokenStorageAllowed": False,
                "expectedArtifacts": [],
                "reasonCodes": [],
                "safeNextSteps": [],
            },
        ],
    }

    replay = provider_orchestration.verify_provider_orchestration_plan(unsafe_plan)

    assert replay["replayStatus"] == "failed"
    assert replay["forbiddenActionCount"] >= 3
    assert replay["missingGateCount"] >= 3
    assert "dashboard_execution_forbidden" in replay["reasonCodes"]
    assert "external_call_planned_in_preview" in replay["reasonCodes"]
    assert "token_storage_planned" in replay["reasonCodes"]
    assert "provider_output_requires_m4_validation" in replay["reasonCodes"]
    assert "provider_acceptance_required" in replay["reasonCodes"]
    assert "account_link_must_not_grant_provider_execution" in replay["reasonCodes"]
    assert "missing_provider_config" in replay["reasonCodes"]
    assert "missing_evidence_replay" in replay["reasonCodes"]


def test_provider_orchestration_plan_api_is_read_only(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    registry = action_capabilities.list_action_requirements()

    preview = client.post(
        "/auth/provider-orchestration/plan/preview",
        json={
            "workflowType": "review_with_stub_provider",
            "topicId": "large-language-models",
            "providerConfigId": "stub",
        },
    )
    replay = client.post("/auth/provider-orchestration/plan/replay/preview", json=preview.json() if preview.status_code == 200 else {})
    summary = client.get("/auth/provider-orchestration/plan/summary")

    assert preview.status_code == 200, preview.text
    assert preview.json()["dashboardExecutionPlanned"] is False
    assert preview.json()["externalCallPlanned"] is False
    assert preview.json()["tokenStoragePlanned"] is False
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayStatus"] in {"valid", "warnings"}
    assert summary.status_code == 200, summary.text
    assert summary.json()["dashboardExecutionPlanned"] is False
    assert summary.json()["externalCallPlanned"] is False
    assert registry["provider_orchestration_dry_run_plan"]["writeCapable"] is False
    assert registry["provider_orchestration_replay_verification"]["writeCapable"] is False
    assert not account_link_path.exists()
    assert not audit_path.exists()


def test_provider_orchestration_fixture_execution_runs_stub_through_m4_validation(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 3):
        paper_reviews.upsert_review(_review(index))

    from app.services import provider_orchestration

    result = provider_orchestration.execute_provider_orchestration_fixture_plan(
        workflow_type="review_with_stub_provider",
        topic_id="large-language-models",
        output_root=tmp_path / "provider-output",
        limit=1,
        dry_run=False,
        now="2026-05-14T00:00:00+00:00",
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert result["fixtureOnly"] is True
    assert result["externalCallPerformed"] is False
    assert result["dashboardTriggered"] is False
    assert result["tokenStorageUsed"] is False
    assert result["m4ValidationApplied"] is True
    assert result["acceptedDraftCount"] == 1
    assert result["rejectedCount"] == 0
    assert result["stepsExecuted"] >= 1
    assert result["resultReplayStatus"] == "valid"
    assert len(stored) == 1
    assert stored[0]["status"] == "accepted"
    assert stored[0]["validation"]["provider"]["origin"] == "stub"


def test_provider_orchestration_fixture_execution_rejects_invalid_stub_output(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    from app.services import provider_orchestration

    result = provider_orchestration.execute_provider_orchestration_fixture_plan(
        workflow_type="review_with_stub_provider",
        topic_id="large-language-models",
        output_root=tmp_path / "provider-output",
        limit=1,
        dry_run=False,
        stub_scenarios=["wrong_sha256"],
        now="2026-05-14T00:00:00+00:00",
    )

    assert result["acceptedDraftCount"] == 0
    assert result["rejectedCount"] == 1
    assert result["m4ValidationApplied"] is True
    assert result["resultReplayStatus"] == "valid"
    assert "provider_output_rejected" in result["reasonCodes"]
    assert ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10) == []


def test_provider_orchestration_fixture_execution_skips_real_provider_steps(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    from app.services import provider_orchestration

    result = provider_orchestration.execute_provider_orchestration_fixture_plan(
        workflow_type="real_provider_review_future_disabled",
        topic_id="large-language-models",
        output_root=tmp_path / "provider-output",
        limit=1,
        dry_run=False,
        now="2026-05-14T00:00:00+00:00",
    )

    assert result["stepsExecuted"] == 0
    assert result["stepsSkipped"] >= 1
    assert result["externalCallPerformed"] is False
    assert result["dashboardTriggered"] is False
    assert result["tokenStorageUsed"] is False
    assert result["resultReplayStatus"] in {"valid", "warnings"}
    assert "real_provider_step_skipped" in result["reasonCodes"]
    assert not (tmp_path / "provider-output").exists()


def test_provider_orchestration_fixture_execution_blocks_non_fixture_command_config(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    calls: list = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("non-fixture command provider must not execute from fixture orchestration")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)

    from app.services import provider_orchestration

    result = provider_orchestration.execute_provider_orchestration_fixture_plan(
        workflow_type="review_with_fixture_command",
        topic_id="large-language-models",
        provider_config_id="gemini-cli-example",
        output_root=tmp_path / "provider-output",
        limit=1,
        dry_run=False,
        now="2026-05-14T00:00:00+00:00",
    )

    assert calls == []
    assert result["stepsExecuted"] == 0
    assert result["externalCallPerformed"] is False
    assert result["resultReplayStatus"] in {"valid", "warnings"}
    assert "non_fixture_provider_config_blocked" in result["reasonCodes"]


def test_provider_orchestration_result_replay_flags_unsafe_execution_fields(tmp_path, monkeypatch) -> None:
    _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)

    from app.services import provider_orchestration

    replay = provider_orchestration.verify_provider_orchestration_execution_result(
        {
            "executionId": "unsafe-fixture-execution",
            "planId": "unsafe-provider-plan",
            "fixtureOnly": False,
            "externalCallPerformed": True,
            "dashboardTriggered": True,
            "tokenStorageUsed": True,
            "m4ValidationApplied": False,
            "executedSteps": [
                {
                    "stepId": "unsafe-live-command",
                    "actionId": "command_provider_live_cli",
                    "executionStatus": "executed_fixture",
                    "fixtureOnly": False,
                    "externalCallPerformed": True,
                    "dashboardTriggered": True,
                    "tokenStorageUsed": True,
                    "m4ValidationApplied": False,
                    "reasonCodes": ["real_provider_profile_executed"],
                }
            ],
        }
    )

    assert replay["replayStatus"] == "failed"
    assert replay["unsafeExecutionCount"] >= 4
    assert replay["missingValidationCount"] >= 1
    assert "fixture_only_required" in replay["reasonCodes"]
    assert "external_call_performed_forbidden" in replay["reasonCodes"]
    assert "dashboard_execution_forbidden" in replay["reasonCodes"]
    assert "token_storage_used_forbidden" in replay["reasonCodes"]
    assert "provider_output_requires_m4_validation" in replay["reasonCodes"]


def test_provider_orchestration_fixture_execution_api_is_preview_only(client, tmp_path, monkeypatch) -> None:
    account_link_path, audit_path = _prepare_oauth_live_pilot_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    registry = action_capabilities.list_action_requirements()

    preview = client.post(
        "/auth/provider-orchestration/fixture-execution/preview",
        json={
            "workflowType": "review_with_stub_provider",
            "topicId": "large-language-models",
            "fixtureOnly": True,
        },
    )
    replay = client.post("/auth/provider-orchestration/fixture-execution/replay/preview", json=preview.json() if preview.status_code == 200 else {})
    summary = client.get("/auth/provider-orchestration/fixture-execution/summary")

    assert preview.status_code == 200, preview.text
    assert preview.json()["fixtureOnly"] is True
    assert preview.json()["dryRun"] is True
    assert preview.json()["externalCallPerformed"] is False
    assert preview.json()["dashboardTriggered"] is False
    assert preview.json()["tokenStorageUsed"] is False
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayStatus"] == "valid"
    assert summary.status_code == 200, summary.text
    assert summary.json()["fixtureOnly"] is True
    assert summary.json()["externalCallPerformed"] is False
    assert summary.json()["dashboardTriggered"] is False
    assert summary.json()["tokenStorageUsed"] is False
    assert registry["provider_orchestration_fixture_execution_preview"]["writeCapable"] is False
    assert registry["provider_orchestration_fixture_result_replay"]["writeCapable"] is False
    assert not account_link_path.exists()
    assert not audit_path.exists()

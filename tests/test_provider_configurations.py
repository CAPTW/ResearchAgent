from __future__ import annotations

import pytest

from app.services import ai_provider
from app.services import provider_configurations


def _external_cli_policy(**overrides) -> dict:
    payload = {
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "External CLI auth is operator-owned; repo stores no credentials.",
    }
    payload.update(overrides)
    return payload


def test_provider_profile_examples_are_secret_free_and_dry_run_by_default() -> None:
    profiles = provider_configurations.example_provider_profiles()
    profiles_by_id = {profile["providerId"]: profile for profile in profiles}

    assert {
        "stub",
        "fixture-command",
        "gemini-cli-example",
            "antigravity-cli-example",
            "claude-code-example",
            "codex-cli-example",
            "antigravity_sdk",
            "antigravity_cli",
            "codex_cli",
            "claude_code",
            "claude_api",
        }.issubset(profiles_by_id)
    for profile in profiles:
        validation = provider_configurations.validate_provider_configuration(profile)
        assert validation["validationStatus"] == "valid", (profile["providerId"], validation["errors"])
        assert "api" not in "\n".join(profile.get("commandArgvTemplate") or []).lower()
        assert "token" not in "\n".join(profile.get("commandArgvTemplate") or []).lower()
    for provider_id in ("gemini-cli-example", "antigravity-cli-example", "claude-code-example", "codex-cli-example"):
        config = profiles_by_id[provider_id]
        assert config["profileRole"] == "real_example"
        assert config["defaultMode"] == "command_dry_run"
        assert config["allowLiveCommand"] is False
        assert config["requiresOperatorAcceptance"] is True
        assert "no_token_storage" in config["credentialPolicy"]
    assert "provider-smoke-ok" in " ".join(profiles_by_id["antigravity-cli-example"]["tinyPromptArgs"])
    assert profiles_by_id["antigravity_sdk"]["executionSurface"] == "sdk"
    assert profiles_by_id["antigravity_sdk"]["authMode"] == "google_adc"
    assert profiles_by_id["antigravity_cli"]["executionSurface"] == "cli"
    assert profiles_by_id["codex_cli"]["authMode"] == "chatgpt_signin"
    assert profiles_by_id["claude_code"]["status"] == "policy_sensitive"
    assert profiles_by_id["claude_api"]["executionSurface"] == "api"


def test_provider_configuration_validation_rejects_secret_fields_and_shell_strings() -> None:
    result = provider_configurations.validate_provider_configuration(
        {
            "providerId": "local-command",
            "displayName": "Local Command",
            "providerType": "command",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": "gemini --json",
            "timeoutSeconds": 30,
            "maxOutputChars": 20000,
            "maxPacketChars": 12000,
            "apiKey": "should-not-be-stored",
        }
    )

    assert result["validationStatus"] == "invalid"
    assert result["providerConfiguration"]["allowLiveCommand"] is False
    assert any("command_argv_template" in error for error in result["errors"])
    assert any("secret" in error.lower() or "credential" in error.lower() for error in result["errors"])


def test_provider_configuration_validation_accepts_argument_list_and_bounds_limits() -> None:
    result = provider_configurations.validate_provider_configuration(
        {
            "providerId": "local-command",
            "displayName": "Local Command",
            "providerType": "command",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["gemini", "--json"],
            **_external_cli_policy(),
            "timeoutSeconds": 999,
            "maxOutputChars": 999_999,
            "maxPacketChars": 999_999,
            "allowedTopicIds": ["large-language-models"],
            "allowedReviewLevels": ["full_text_grounded"],
        }
    )

    config = result["providerConfiguration"]
    assert result["validationStatus"] == "valid"
    assert config["commandArgvTemplate"] == ["gemini", "--json"]
    assert config["allowLiveCommand"] is False
    assert config["requiresOperatorConfirmation"] is True
    assert config["timeoutSeconds"] <= provider_configurations.MAX_TIMEOUT_SECONDS
    assert config["maxOutputChars"] <= provider_configurations.MAX_OUTPUT_CHARS
    assert config["maxPacketChars"] <= provider_configurations.MAX_PACKET_CHARS
    assert any("bounded" in warning for warning in result["warnings"])


def test_provider_configuration_schema_requires_credential_boundary_metadata() -> None:
    result = provider_configurations.validate_provider_configuration(
        {
            "providerId": "local-command",
            "displayName": "Local Command",
            "providerType": "command",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["provider-cli"],
        }
    )

    assert result["validationStatus"] == "invalid"
    assert any("credential_policy is required" in error for error in result["errors"])
    assert any("no_token_storage" in error for error in result["errors"])


def test_provider_configuration_schema_rejects_credential_cache_paths_and_live_status_claims() -> None:
    forbidden = provider_configurations.validate_provider_configuration(
        {
            "providerId": "codex-local",
            "displayName": "Codex Local",
            "providerType": "command",
            "profileRole": "real_example",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["codex", "--config", "codex/auth/cache.json"],
            **_external_cli_policy(
                authMode="chatgpt_signin",
                forbiddenCredentialPaths=["codex/auth", ".mcp.json"],
            ),
        }
    )
    live_claim = provider_configurations.validate_provider_configuration(
        {
            "providerId": "live-claim",
            "displayName": "Live Claim",
            "providerType": "command",
            "profileRole": "real_example",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["provider-cli"],
            **_external_cli_policy(status="live_verified_tiny", proofArtifactRequired=True),
        }
    )
    no_proof_gate = provider_configurations.validate_provider_configuration(
        {
            "providerId": "no-proof-gate",
            "displayName": "No Proof Gate",
            "providerType": "command",
            "profileRole": "real_example",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["provider-cli"],
            **_external_cli_policy(proofArtifactRequired=False),
        }
    )

    assert forbidden["validationStatus"] == "invalid"
    assert any("forbidden credential path" in error for error in forbidden["errors"])
    assert live_claim["validationStatus"] == "invalid"
    assert any("status cannot be live_verified_tiny" in error for error in live_claim["errors"])
    assert no_proof_gate["validationStatus"] == "invalid"
    assert any("proof_artifact_required" in error for error in no_proof_gate["errors"])


def test_provider_configuration_operator_profile_paths_require_secret_free_policy() -> None:
    missing_policy = provider_configurations.validate_provider_configuration(
        {
            "providerId": "profile-path",
            "displayName": "Profile Path",
            "providerType": "command",
            "profileRole": "real_example",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["provider-cli", "--profile-json", "operator-profile.json"],
            **_external_cli_policy(),
        }
    )
    accepted_policy = provider_configurations.validate_provider_configuration(
        {
            "providerId": "profile-path",
            "displayName": "Profile Path",
            "providerType": "command",
            "profileRole": "real_example",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": ["provider-cli", "--profile-json", "operator-profile.json"],
            **_external_cli_policy(
                credentialPolicy=["external_only", "operator_profile", "no_token_storage", "no_cache_inspection"],
                operatorProfileSecretFree=True,
            ),
        }
    )

    assert missing_policy["validationStatus"] == "invalid"
    assert any("operator_profile" in error for error in missing_policy["errors"])
    assert accepted_policy["validationStatus"] == "valid"
    assert accepted_policy["providerConfiguration"]["operatorProfileSecretFree"] is True


def test_command_template_contract_accepts_allowed_placeholders_and_renders_preview() -> None:
    result = provider_configurations.validate_command_template(
        [
            "provider-cli",
            "--packet",
            "{packet_path}",
            "--output",
            "{output_path}",
            "--run-id",
            "{provider_run_id}",
            "--topic",
            "{topic_id}",
        ],
        packet_path="output/provider-manual-run/packets/paper.json",
        output_path="output/provider-manual-run/provider-output.jsonl",
        provider_run_id="manual-run-1",
        topic_id="large-language-models",
    )

    assert result["valid"] is True
    assert result["reasonCodes"] == []
    assert result["renderedPreview"] == [
        "provider-cli",
        "--packet",
        "output/provider-manual-run/packets/paper.json",
        "--output",
        "output/provider-manual-run/provider-output.jsonl",
        "--run-id",
        "manual-run-1",
        "--topic",
        "large-language-models",
    ]
    assert result["redactedPreview"] == result["renderedPreview"]


def test_command_template_contract_rejects_unsupported_placeholders_shell_and_secrets() -> None:
    unsupported = provider_configurations.validate_command_template(["provider-cli", "--input", "{raw_full_text}"])
    shell_like = provider_configurations.validate_command_template(["provider-cli", "&&", "curl"])
    secret_like = provider_configurations.validate_command_template(["provider-cli", "--api-key=abc123", "{packet_path}"])

    assert unsupported["valid"] is False
    assert "unsupported_placeholder:raw_full_text" in unsupported["reasonCodes"]
    assert shell_like["valid"] is False
    assert "shell_operator_not_allowed" in shell_like["reasonCodes"]
    assert secret_like["valid"] is False
    assert "secret_like_command_arg" in secret_like["reasonCodes"]
    assert "<redacted>" in secret_like["redactedPreview"]


def test_provider_configuration_validation_requires_command_template_for_command_provider() -> None:
    result = provider_configurations.validate_provider_configuration(
        {
            "providerId": "local-command",
            "displayName": "Local Command",
            "providerType": "command",
            "profileRole": "real_example",
            "defaultMode": "command_dry_run",
            **_external_cli_policy(),
        }
    )

    assert result["validationStatus"] == "invalid"
    assert any("command_argv_template" in error for error in result["errors"])


def test_provider_readiness_is_read_only_and_reports_stub_and_missing_command() -> None:
    checked_commands: list[list[str]] = []

    def fake_command_exists(command: list[str]) -> bool:
        checked_commands.append(command)
        return False

    result = provider_configurations.build_provider_readiness_summary(command_exists=fake_command_exists)
    items = {item["providerId"]: item for item in result["items"]}

    assert items["stub"]["readinessStatus"] == "ready_stub"
    assert items["stub"]["canonicalStatus"] == "mock_only"
    assert items["stub"]["researchOutputAllowed"] is False
    assert items["stub"]["safeModesAvailable"] == ["stub"]
    assert items["stub"]["liveAllowed"] is False
    assert items["fixture-command"]["readinessStatus"] == "ready_fixture"
    assert items["fixture-command"]["canonicalStatus"] == "mock_only"
    assert items["command"]["readinessStatus"] == "not_configured"
    assert items["command"]["canonicalStatus"] == "docs_only"
    assert items["gemini-cli-example"]["providerAlias"] == "gemini_cli"
    assert items["gemini-cli-example"]["canonicalStatus"] == "environment_blocked_binary_missing"
    assert items["gemini-cli-example"]["routingLabel"] == "setup_required"
    assert "missing_command_template" in items["command"]["reasonCodes"]
    assert {command[0] for command in checked_commands}.issubset({"gemini", "antigravity", "claude", "codex"})
    checked_commands.clear()

    command_result = provider_configurations.build_provider_readiness_summary(
        configs=[
            {
                "providerId": "command",
                "displayName": "Local Command Provider",
                "providerType": "command",
                "enabled": True,
                "defaultMode": "command_dry_run",
                "commandArgvTemplate": ["missing-provider"],
                **_external_cli_policy(),
                "allowLiveCommand": False,
            }
        ],
        command_exists=fake_command_exists,
    )
    command_item = command_result["items"][0]

    assert checked_commands == [["missing-provider"]]
    assert command_item["readinessStatus"] == "command_missing"
    assert command_item["canonicalStatus"] == "configured_but_not_available"
    assert command_item["routingLabel"] == "setup_required"
    assert command_item["safeModesAvailable"] == ["command_dry_run"]
    assert command_item["liveAllowed"] is False


def test_provider_readiness_reports_disabled_and_dry_run_only_command() -> None:
    disabled = provider_configurations.build_provider_readiness_summary(
        configs=[
            {
                "providerId": "stub",
                "displayName": "Disabled Stub",
                "providerType": "stub",
                "enabled": False,
            }
        ]
    )["items"][0]
    dry_run_only = provider_configurations.build_provider_readiness_summary(
        configs=[
            {
                "providerId": "command",
                "displayName": "Local Command Provider",
                "providerType": "command",
                "enabled": True,
                "defaultMode": "command_dry_run",
                "commandArgvTemplate": ["safe-provider"],
                **_external_cli_policy(),
                "allowLiveCommand": False,
            }
        ],
        command_exists=lambda command: True,
    )["items"][0]

    assert disabled["readinessStatus"] == "disabled"
    assert disabled["safeModesAvailable"] == []
    assert dry_run_only["readinessStatus"] == "dry_run_only"
    assert dry_run_only["canonicalStatus"] == "configured_not_verified"
    assert dry_run_only["researchOutputAllowed"] is False
    assert dry_run_only["reportGenerationAllowed"] is False
    assert dry_run_only["orchestrationAllowed"] is False
    assert dry_run_only["safeModesAvailable"] == ["command_dry_run"]
    assert dry_run_only["liveAllowed"] is False
    assert dry_run_only["commandPreviewRedacted"] == ["safe-provider"]


def test_provider_readiness_marks_secret_like_command_arguments_unsafe_and_redacted() -> None:
    result = provider_configurations.build_provider_readiness_summary(
        configs=[
            {
                "providerId": "command",
                "displayName": "Unsafe Command",
                "providerType": "command",
                "defaultMode": "command_dry_run",
                "commandArgvTemplate": ["provider-cli", "--api-key=abc123"],
                **_external_cli_policy(),
            }
        ],
        command_exists=lambda command: True,
    )["items"][0]

    assert result["readinessStatus"] == "unsafe_config"
    assert result["canonicalStatus"] == "unsafe_to_probe"
    assert result["routingLabel"] == "not_allowed_for_research_output"
    assert result["commandPreviewRedacted"] == ["provider-cli", "<redacted>"]
    assert result["liveAllowed"] is False
    assert any("secret-like" in reason for reason in result["reasonCodes"])


def test_provider_readiness_distinguishes_real_examples_and_acceptance_required() -> None:
    examples = provider_configurations.example_provider_profiles()
    items = {
        item["providerId"]: item
        for item in provider_configurations.build_provider_readiness_summary(
            configs=examples,
            command_exists=lambda command: True,
        )["items"]
    }

    assert items["gemini-cli-example"]["readinessStatus"] == "example_profile_configured"
    assert items["gemini-cli-example"]["providerAlias"] == "gemini_cli"
    assert items["gemini-cli-example"]["canonicalStatus"] == "non_live_available"
    assert items["gemini-cli-example"]["routingLabel"] == "provider_smoke_only"
    assert items["gemini-cli-example"]["researchOutputAllowed"] is False
    assert items["gemini-cli-example"]["reportGenerationAllowed"] is False
    assert items["gemini-cli-example"]["orchestrationAllowed"] is False
    assert items["gemini-cli-example"]["statusMessage"] == "Non-live available; live proof missing"
    assert items["gemini-cli-example"]["operatorAcceptanceRequired"] is True
    assert items["gemini-cli-example"]["liveAllowed"] is False
    assert items["antigravity-cli-example"]["readinessStatus"] == "example_profile_configured"
    assert items["antigravity-cli-example"]["providerAlias"] == "antigravity_cli"
    assert items["antigravity-cli-example"]["canonicalStatus"] == "non_live_available"
    assert items["codex-cli-example"]["providerAlias"] == "codex_cli"
    assert items["codex-cli-example"]["canonicalStatus"] == "non_live_available"
    assert items["claude-code-example"]["providerAlias"] == "claude_code"
    assert items["claude-code-example"]["canonicalStatus"] == "policy_sensitive"
    assert items["claude-code-example"]["routingLabel"] == "not_allowed_for_research_output"
    assert items["claude_code"]["canonicalStatus"] == "policy_sensitive"
    assert items["claude_api"]["canonicalStatus"] == "configured_not_verified"
    assert items["antigravity-cli-example"]["operatorAcceptanceRequired"] is True
    assert items["antigravity-cli-example"]["liveAllowed"] is False

    accepted_profile = {
        **items["gemini-cli-example"]["configuration"],
        "allowLiveCommand": True,
        "enabled": True,
    }
    accepted_item = provider_configurations.build_provider_readiness_summary(
        configs=[accepted_profile],
        command_exists=lambda command: True,
    )["items"][0]

    assert accepted_item["readinessStatus"] == "operator_acceptance_required"
    assert accepted_item["canonicalStatus"] == "non_live_available"
    assert accepted_item["researchOutputAllowed"] is False
    assert accepted_item["liveAllowed"] is True
    assert accepted_item["operatorAcceptanceRequired"] is True
    assert "operator_acceptance_required" in accepted_item["reasonCodes"]


def test_provider_routing_decision_rejects_current_real_providers() -> None:
    summary = provider_configurations.build_provider_readiness_summary(
        configs=provider_configurations.example_provider_profiles(),
        command_exists=lambda command: command[0] in {"gemini", "codex"},
    )

    gemini = provider_configurations.build_provider_routing_decision(
        "gemini_cli",
        purpose="research_synthesis",
        provider_readiness_summary=summary,
    )
    codex = provider_configurations.build_provider_routing_decision(
        "codex_cli",
        purpose="report_generation",
        provider_readiness_summary=summary,
    )
    antigravity = provider_configurations.build_provider_routing_decision(
        "antigravity_cli",
        purpose="orchestration",
        provider_readiness_summary=summary,
    )
    claude = provider_configurations.build_provider_routing_decision(
        "claude_code",
        purpose="research_synthesis",
        provider_readiness_summary=summary,
    )

    assert gemini["allowed"] is False
    assert gemini["canonicalStatus"] == "non_live_available"
    assert "live_proof_missing" in gemini["reasonCodes"]
    assert codex["allowed"] is False
    assert codex["canonicalStatus"] == "non_live_available"
    assert antigravity["allowed"] is False
    assert antigravity["canonicalStatus"] == "environment_blocked_binary_missing"
    assert antigravity["routingLabel"] == "setup_required"
    assert claude["allowed"] is False
    assert claude["canonicalStatus"] == "policy_sensitive"


@pytest.mark.parametrize("provider", ["gemini_cli", "codex_cli", "antigravity_cli", "claude_code"])
@pytest.mark.parametrize("purpose", ["research_synthesis", "report_generation", "full_text_review", "orchestration"])
def test_provider_routing_decision_rejects_all_current_real_providers_for_product_output(provider: str, purpose: str) -> None:
    summary = provider_configurations.build_provider_readiness_summary(
        configs=provider_configurations.example_provider_profiles(),
        command_exists=lambda command: command[0] in {"gemini", "codex"},
    )

    decision = provider_configurations.build_provider_routing_decision(
        provider,
        purpose=purpose,
        provider_readiness_summary=summary,
    )

    assert decision["allowed"] is False
    assert "providerId" in decision
    assert "canonicalStatus" in decision
    assert "routingLabel" in decision
    if provider in {"gemini_cli", "codex_cli"}:
        assert decision["canonicalStatus"] == "non_live_available"
        assert decision["routingLabel"] == "provider_smoke_only"
        assert "live_proof_missing" in decision["reasonCodes"]
    elif provider == "antigravity_cli":
        assert decision["canonicalStatus"] == "environment_blocked_binary_missing"
        assert decision["routingLabel"] == "setup_required"
        assert "provider_binary_missing" in decision["reasonCodes"]
    else:
        assert decision["canonicalStatus"] == "policy_sensitive"
        assert decision["routingLabel"] == "not_allowed_for_research_output"
        assert "policy_sensitive" in decision["reasonCodes"]
    if purpose == "report_generation":
        assert "report_generation_blocked" in decision["reasonCodes"]
    elif purpose == "orchestration":
        assert "orchestration_blocked" in decision["reasonCodes"]
    else:
        assert "research_output_blocked" in decision["reasonCodes"]


def test_real_example_command_provider_is_blocked_for_research_output(isolated_run_repo) -> None:
    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="gemini-cli-example",
        allow_live_provider=True,
        accept_provider_risk=True,
        limit=1,
        output_root=isolated_run_repo["storage_root"].parent / "provider-output",
    )

    assert result["invoked_count"] == 0
    assert result["failed_count"] == 1
    assert result["stored_draft_count"] == 0
    assert "provider_research_output_blocked" in result["reason_counts"]
    assert result["results"][0]["routing_decision"]["allowed"] is False
    assert result["results"][0]["routing_decision"]["routingLabel"] in {"provider_smoke_only", "setup_required"}


def test_provider_readiness_api_and_operations_planner_are_preview_only(client, isolated_run_repo, monkeypatch) -> None:
    response = client.get("/api/provider-configurations/readiness")
    body = response.json()

    assert response.status_code == 200, response.text
    assert {item["providerId"] for item in body["items"]} >= {"stub", "command"}
    assert all(item["liveAllowed"] is False for item in body["items"])

    dashboard_response = client.get("/api/topics/large-language-models/dashboard")
    dashboard = dashboard_response.json()
    planner = dashboard["researchOperations"]["operationsPlanner"]
    provider_readiness = {item["providerId"]: item for item in planner["providerReadiness"]}
    command_plan = next(plan for plan in planner["runPlans"] if plan["planId"] == "provider_command_dry_run")
    acceptance_smoke_plan = next(plan for plan in planner["runPlans"] if plan["planId"] == "provider_acceptance_smoke_dry_run")

    assert dashboard_response.status_code == 200, dashboard_response.text
    assert provider_readiness["stub"]["readinessStatus"] == "ready_stub"
    assert provider_readiness["stub"]["canonicalStatus"] == "mock_only"
    assert provider_readiness["command"]["readinessStatus"] in {"not_configured", "command_missing", "dry_run_only"}
    assert all(item["researchOutputAllowed"] is False for item in provider_readiness.values())
    assert command_plan["previewOnly"] is True
    assert command_plan["serverExecutesCommand"] is False
    assert command_plan["callsProvider"] is False
    assert "--dry-run" in command_plan["copyableCliCommand"]
    assert "--allow-live-ai-provider" not in command_plan["copyableCliCommand"]
    assert "--accept-provider-risk" not in command_plan["copyableCliCommand"]
    assert any("accept-provider-risk" in warning for warning in command_plan["warnings"])
    assert acceptance_smoke_plan["previewOnly"] is True
    assert "--provider-acceptance-smoke" in acceptance_smoke_plan["copyableCliCommand"]
    assert "--dry-run" in acceptance_smoke_plan["copyableCliCommand"]
    assert "--allow-live-ai-provider" not in acceptance_smoke_plan["copyableCliCommand"]
    assert "--accept-provider-risk" not in acceptance_smoke_plan["copyableCliCommand"]


def test_provider_configuration_validation_api_rejects_unsafe_command_config(client) -> None:
    response = client.post(
        "/api/provider-configurations/validate",
        json={
            "providerId": "command",
            "displayName": "Local Command",
            "providerType": "command",
            "defaultMode": "command_dry_run",
            "commandArgvTemplate": "gemini --json",
            "token": "not-allowed",
        },
    )
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["validationStatus"] == "invalid"
    assert any("command_argv_template" in error for error in body["errors"])
    assert any("secret" in error.lower() or "credential" in error.lower() for error in body["errors"])
    assert body["providerConfiguration"]["allowLiveCommand"] is False


def test_codex_cli_profile_binding_uses_valid_codex_exec_shape() -> None:
    profiles = {p["providerId"]: p for p in provider_configurations.example_provider_profiles()}

    for provider_id in ("codex_cli", "codex-cli-example"):
        profile = profiles[provider_id]
        template = profile["commandArgvTemplate"]
        tiny = profile["tinyPromptArgs"]

        # 1. corrected to `codex exec`, not the invalid --input/--output shape
        assert template[:2] == ["codex", "exec"], (provider_id, template)
        assert "--input" not in template
        assert "--output" not in template
        assert tiny[:2] == ["codex", "exec"]

        # 2-5. bounding/sandbox flags present in both the full template and the tiny-proof shape
        for flag in ("--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--color", "never"):
            assert flag in template, (provider_id, "commandArgvTemplate", flag)
            assert flag in tiny, (provider_id, "tinyPromptArgs", flag)

        # 6. full template writes the last message via -o / --output-last-message (no invalid --output)
        assert ("-o" in template) or ("--output-last-message" in template)

        # 7. sentinel prompt is positional; packet is delivered via stdin `-`; no invalid --input
        assert tiny[-1] == "Return exactly: provider-smoke-ok"
        assert "--input" not in tiny
        assert template[-1] == "-"

        # 8-10. remains configured_not_verified, output-blocked, and proof-gated before any promotion
        assert profile["status"] == "configured_not_verified"
        assert profile["researchOutputAllowed"] is False
        assert profile["proofArtifactRequired"] is True
        assert profile["statusAfterProofCandidate"] == "live_verified_tiny"
        assert profile["allowLiveCommand"] is False
        assert profile["requiresOperatorAcceptance"] is True

        # secret-free: no auth-cache / mcp / token material bound into the commands
        joined = " ".join(template + tiny).lower()
        for banned in ("auth.json", ".codex", ".mcp.json", "codex/auth", "token", "api_key", "bearer"):
            assert banned not in joined, (provider_id, banned)

        # profile still validates as safe and dry-run only
        validation = provider_configurations.validate_provider_configuration(profile)
        assert validation["validationStatus"] == "valid", validation["errors"]
        assert validation["providerConfiguration"]["allowLiveCommand"] is False

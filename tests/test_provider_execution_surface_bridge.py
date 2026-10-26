from __future__ import annotations

import json

from app.services import provider_execution_surface_bridge as bridge


def _surfaces_by_profile(env: dict[str, str] | None = None) -> dict[str, dict]:
    summary = bridge.describe_provider_execution_surfaces(env=env or {})
    return {item["profileId"]: item for item in summary["surfaces"]}


def test_openai_unconfigured_remains_blocked_without_reading_secret_values() -> None:
    surface = bridge.resolve_provider_execution_surface(
        "openai",
        env={"OPENAI_MODEL": "private-model-name"},
    )

    assert surface is not None
    payload = surface.to_dict()
    serialized = json.dumps(payload)
    assert payload["providerId"] == "openai"
    assert payload["profileId"] == "openai_api"
    assert payload["adapterKind"] == "executable_adapter"
    assert payload["executionSurfaceType"] == "api"
    assert payload["configured"] is False
    assert payload["envPresence"]["OPENAI_API_KEY"] is False
    assert "required_env_missing" in payload["blockedReasonCodes"]
    assert "private-model-name" not in serialized


def test_openai_configured_uses_env_presence_boolean_only() -> None:
    surface = bridge.resolve_provider_execution_surface(
        "openai",
        env={
            "OPENAI_API_KEY": "sk-test-secret-value",
            "OPENAI_MODEL": "private-model",
            "OPENAI_API_BASE": "https://example.invalid/v1?token=secret",
        },
    )

    assert surface is not None
    payload = surface.to_dict()
    serialized = json.dumps(payload)
    assert payload["configured"] is True
    assert payload["envPresence"]["OPENAI_API_KEY"] is True
    assert "sk-test-secret-value" not in serialized
    assert "private-model" not in serialized
    assert "token=secret" not in serialized


def test_provider_inventory_classifies_existing_profiles() -> None:
    profiles = _surfaces_by_profile()

    assert profiles["openai_api"]["adapterKind"] == "executable_adapter"
    assert profiles["openai_api"]["executionSurfaceType"] == "api"
    assert profiles["codex_cli"]["adapterKind"] == "command_contract"
    assert profiles["codex-cli-example"]["adapterKind"] == "command_contract"
    assert profiles["claude_code"]["adapterKind"] == "command_contract"
    assert profiles["claude_api"]["adapterKind"] == "metadata_only"
    assert profiles["gemini-cli-example"]["adapterKind"] == "command_contract"
    assert profiles["antigravity_cli"]["adapterKind"] == "command_contract"
    assert profiles["antigravity_sdk"]["adapterKind"] == "metadata_only"
    assert profiles["stub"]["adapterKind"] == "unsupported"
    assert profiles["fixture-command"]["adapterKind"] == "unsupported"


def test_command_contract_and_metadata_only_profiles_cannot_execute() -> None:
    codex = bridge.plan_provider_output_class_execution("codex_cli", "paper_review", env={}).to_dict()
    claude_api = bridge.plan_provider_output_class_execution("claude_api", "paper_review", env={}).to_dict()

    assert codex["adapterKind"] == "command_contract"
    assert codex["liveExecutionAllowed"] is False
    assert codex["wouldExecuteProvider"] is False
    assert "command_contract_cannot_execute" in codex["reasonCodes"]
    assert claude_api["adapterKind"] == "metadata_only"
    assert claude_api["liveExecutionAllowed"] is False
    assert claude_api["wouldExecuteProvider"] is False
    assert "metadata_only_cannot_execute" in claude_api["reasonCodes"]


def test_status_output_lists_names_but_never_secret_or_auth_paths() -> None:
    payload = bridge.describe_provider_execution_surfaces(
        env={
            "OPENAI_API_KEY": "sk-test-secret-value",
            "GOOGLE_APPLICATION_CREDENTIALS": "C:/Users/operator/adc.json",
            "ANTHROPIC_API_KEY": "secret-value",
        }
    )
    serialized = json.dumps(payload)
    lower = serialized.lower()

    assert "OPENAI_API_KEY" in serialized  # names are allowed
    for forbidden in (
        "sk-test-secret-value",
        "secret-value",
        "adc.json",
        ".mcp.json",
        "auth.json",
        "browser/session",
        "browser/cookies",
        "keychain",
        "credential-store",
    ):
        assert forbidden not in lower


def test_provider_auth_and_mcp_public_connector_stay_out_of_scope() -> None:
    summary = bridge.describe_provider_execution_surfaces(env={})

    assert summary["appOauthOidcInScope"] is False
    assert summary["publicMcpConnectorInScope"] is False
    assert "mcp" in summary["executionSurfaceTypes"]
    for surface in summary["surfaces"]:
        assert surface["appOauthOidcInScope"] is False
        assert surface["publicMcpConnectorInScope"] is False
        assert surface["noSecretInspection"] is True
        assert surface["noTokenCacheInspection"] is True


def test_ranking_selects_candidate_but_routing_refuses_output_without_proof() -> None:
    ranked = bridge.rank_provider_execution_candidates(
        output_class="paper_review",
        weights={"codex_cli": 100, "openai": 1},
        env={},
    )
    candidate = bridge.select_provider_for_future_output_class_proof(output_class="paper_review", env={})
    plan = bridge.plan_provider_output_class_execution("openai", "paper_review", env={}).to_dict()

    assert ranked[0]["providerId"] == "openai"
    assert any(row["profileId"] == "codex_cli" and row["candidateWeight"] == 100 for row in ranked)
    assert candidate is not None
    assert candidate["providerId"] == "openai"
    assert candidate["selected"] is True
    assert candidate["readyNow"] is False
    assert plan["outputAllowed"] is False
    assert plan["liveExecutionAllowed"] is False
    assert plan["wouldExecuteProvider"] is False
    assert plan["blockedReason"] == "output_class_proof_missing"


def test_default_envless_bridge_result_is_deterministic_and_fail_closed() -> None:
    first = bridge.describe_provider_execution_surfaces(env={})
    second = bridge.describe_provider_execution_surfaces(env={})

    assert first == second
    assert first["nonLive"] is True
    assert first["liveExecutionAllowed"] is False
    assert first["wouldExecuteProvider"] is False
    assert first["summary"]["defaultFailClosed"] is True
    assert all(surface["blocked"] for surface in first["surfaces"])


def test_exact_output_class_proof_remains_required_and_non_live() -> None:
    env = {"OPENAI_API_KEY": "sk-test-secret-value"}
    report_proof = {"openai": {"report_draft": "report_draft_verified_tiny"}}
    research_plan = bridge.plan_provider_output_class_execution(
        "openai",
        "paper_review",
        env=env,
        accepted_output_proofs=report_proof,
    ).to_dict()
    report_plan = bridge.plan_provider_output_class_execution(
        "openai",
        "weekly_report",
        env=env,
        accepted_output_proofs=report_proof,
    ).to_dict()

    assert research_plan["outputAllowed"] is False
    assert research_plan["requiredProofLevel"] == "research_output_verified_tiny"
    assert report_plan["outputAllowed"] is True
    assert report_plan["requiredProofLevel"] == "report_draft_verified_tiny"
    assert report_plan["liveExecutionAllowed"] is False
    assert report_plan["wouldExecuteProvider"] is False
    assert "provider_execution_refused_non_live" in report_plan["reasonCodes"]


def test_bare_live_verified_tiny_remains_insufficient() -> None:
    plan = bridge.plan_provider_output_class_execution(
        "openai",
        "paper_review",
        env={"OPENAI_API_KEY": "sk-test-secret-value"},
        accepted_output_proofs={"openai": {"research_output": "live_verified_tiny"}},
    ).to_dict()

    assert plan["outputAllowed"] is False
    assert plan["blockedReason"] == "insufficient_proof_level_for_output_class"
    assert plan["liveExecutionAllowed"] is False
    assert plan["wouldExecuteProvider"] is False


def test_next_paper_review_candidate_is_openai_but_still_blocked() -> None:
    missing_env = bridge.select_provider_for_future_output_class_proof(output_class="paper_review", env={})
    configured_env = bridge.select_provider_for_future_output_class_proof(
        output_class="paper_review",
        env={"OPENAI_API_KEY": "sk-test-secret-value"},
    )

    assert missing_env is not None
    assert missing_env["providerId"] == "openai"
    assert missing_env["configured"] is False
    assert missing_env["requiredEnvNames"] == ["OPENAI_API_KEY"]
    assert missing_env["wouldExecuteProvider"] is False
    assert configured_env is not None
    assert configured_env["configured"] is True
    assert configured_env["readyNow"] is False
    assert configured_env["wouldExecuteProvider"] is False

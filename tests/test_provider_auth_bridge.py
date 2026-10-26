from __future__ import annotations

from pathlib import Path

from app.db import bootstrap as db_bootstrap
from app.repositories import paper_reviews
from app.services import ai_provider, provider_auth_bridge, provider_configurations


def _profile(profile_id: str) -> dict:
    profile = provider_configurations.provider_profile_by_id(profile_id)
    assert profile is not None
    return profile


def _external_cli_profile(**overrides) -> dict:
    payload = {
        "providerId": "external-cli",
        "displayName": "External CLI",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [["provider-cli", "--version"]],
        "forbiddenCredentialPaths": [".mcp.json", "auth.json", "browser/session", "storage/", "storage/backups/"],
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "External CLI auth is operator-owned.",
        "commandArgvTemplate": ["provider-cli", "--packet", "{packet_path}", "--output", "{output_path}"],
        "allowLiveCommand": False,
        "requiresOperatorAcceptance": True,
    }
    payload.update(overrides)
    return payload


def test_provider_auth_bridge_evaluates_provider_examples_as_non_live_and_fail_closed() -> None:
    statuses = {
        profile_id: provider_auth_bridge.evaluate_provider_auth_bridge(_profile(profile_id)).to_dict()
        for profile_id in ("antigravity_sdk", "antigravity_cli", "codex_cli", "claude_code", "claude_api")
    }

    assert statuses["antigravity_sdk"]["readinessLabel"] == "proof_required"
    assert statuses["antigravity_sdk"]["authMode"] == "google_adc"
    assert statuses["antigravity_cli"]["readinessLabel"] == "proof_required"
    assert statuses["antigravity_cli"]["commandPreview"]["argvRedacted"] == [
        "antigravity",
        "--packet",
        "{packet_path}",
        "--output",
        "{output_path}",
    ]
    assert statuses["codex_cli"]["authMode"] == "chatgpt_signin"
    assert statuses["codex_cli"]["commandPreview"]["argvRedacted"] == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-o",
        "{output_path}",
        "-",
    ]
    assert "--input" not in statuses["codex_cli"]["commandPreview"]["argvRedacted"]
    assert "--output" not in statuses["codex_cli"]["commandPreview"]["argvRedacted"]
    assert statuses["claude_code"]["readinessLabel"] == "policy_sensitive_blocked"
    assert statuses["claude_code"]["blockedReason"] == "policy_sensitive_provider"
    assert statuses["claude_api"]["authMode"] == "api_key"

    for payload in statuses.values():
        assert payload["outputAllowed"] is False
        assert payload["researchOutputAllowed"] is False
        assert payload["commandPreview"]["liveExecutionAllowed"] is False
        assert payload["commandPreview"]["credentialValuesPrinted"] is False
        assert payload["proofArtifactPresent"] is False


def test_provider_auth_bridge_rejects_credential_cache_paths_and_mcp_references() -> None:
    profile = _external_cli_profile(
        commandArgvTemplate=["codex", "--config", "codex/auth/cache.json"],
        allowedCommands=[["codex", "--config", ".mcp.json"]],
        forbiddenCredentialPaths=["codex/auth", ".mcp.json"],
    )

    status = provider_auth_bridge.evaluate_provider_auth_bridge(profile).to_dict()
    issue_codes = {issue["code"] for issue in status["issues"]}

    assert "forbidden_credential_path_reference" in issue_codes
    assert status["readinessLabel"] == "unsafe_config"
    assert status["blockedReason"] == "forbidden_credential_path_reference"
    assert provider_auth_bridge.is_forbidden_credential_path(".mcp.json") is True
    assert provider_auth_bridge.is_forbidden_credential_path("C:/Users/me/.codex/auth.json") is True
    assert provider_auth_bridge.is_forbidden_credential_path("output/provider-manual-run/profile.json") is True
    assert provider_auth_bridge.is_forbidden_credential_path("output/provider-live-proof/proof.json") is False


def test_provider_auth_bridge_redacts_secret_values_and_profile_paths() -> None:
    redacted = provider_auth_bridge.redact_provider_command(
        [
            "provider-cli",
            "--profile-json",
            "C:/Users/operator/live-profile.json",
            "--api-key=sk-secret-value",
            "--header",
            "Authorization: Bearer abc.def.ghi",
        ]
    )

    assert redacted == [
        "provider-cli",
        "--profile-json",
        "<redacted-path>",
        "<redacted>",
        "--header",
        "<redacted>",
    ]
    assert "sk-secret-value" not in " ".join(redacted)
    assert "abc.def.ghi" not in " ".join(redacted)


def test_provider_auth_bridge_blocks_missing_policy_live_status_and_research_output() -> None:
    missing_policy = provider_auth_bridge.evaluate_provider_auth_bridge(
        _external_cli_profile(credentialPolicy=[]),
    ).to_dict()
    live_tiny = provider_auth_bridge.evaluate_provider_auth_bridge(
        _external_cli_profile(status="live_verified_tiny", proofArtifactRequired=True),
    ).to_dict()
    live_full = provider_auth_bridge.evaluate_provider_auth_bridge(
        _external_cli_profile(status="live_verified", proofArtifactRequired=True),
    ).to_dict()
    research_output = provider_auth_bridge.evaluate_provider_auth_bridge(
        _external_cli_profile(researchOutputAllowed=True),
    ).to_dict()

    assert "missing_credential_policy" in {issue["code"] for issue in missing_policy["issues"]}
    assert "live_verified_tiny_proof_required" in {issue["code"] for issue in live_tiny["issues"]}
    assert "stronger_live_proof_required" in {issue["code"] for issue in live_full["issues"]}
    assert "research_output_requires_stronger_proof" in {issue["code"] for issue in research_output["issues"]}
    assert live_tiny["outputAllowed"] is False
    assert live_full["outputAllowed"] is False
    assert research_output["researchOutputAllowed"] is False


def test_provider_auth_bridge_proof_artifact_presence_is_exact_surface_tiny_only(tmp_path: Path) -> None:
    proof = tmp_path / "provider-live-tiny.json"
    proof.write_text("{}", encoding="utf-8")
    profile = _external_cli_profile(
        status="live_verified_tiny",
        proofArtifactPath="provider-live-tiny.json",
    )

    status = provider_auth_bridge.evaluate_provider_auth_bridge(profile, proof_root=tmp_path).to_dict()

    assert status["proofArtifactPresent"] is True
    assert status["currentStatus"] == "live_verified_tiny"
    assert status["readinessLabel"] == "live_tiny_proof_present"
    assert status["outputAllowed"] is False
    assert status["nextRequiredGate"] == "stronger_live_verified_proof_gate"
    assert "live_verified_tiny_proof_required" not in {issue["code"] for issue in status["issues"]}


def test_provider_readiness_summary_includes_auth_bridge_without_enabling_output() -> None:
    summary = provider_configurations.build_provider_readiness_summary(
        configs=[_profile("antigravity_cli"), _profile("codex_cli"), _profile("claude_code")],
        command_exists=lambda command: True,
    )
    items = {item["providerId"]: item for item in summary["items"]}

    assert items["antigravity_cli"]["authBridge"]["readinessLabel"] == "proof_required"
    assert items["codex_cli"]["authBridge"]["authMode"] == "chatgpt_signin"
    assert items["claude_code"]["authBridge"]["readinessLabel"] == "policy_sensitive_blocked"
    assert all(item["authBridge"]["outputAllowed"] is False for item in items.values())
    assert all(item["researchOutputAllowed"] is False for item in items.values())
    assert all(item["reportGenerationAllowed"] is False for item in items.values())
    assert all(item["orchestrationAllowed"] is False for item in items.values())


def test_ai_provider_dry_run_redacts_command_preview_for_profile_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(
        {
            "topic_id": "large-language-models",
            "run_id": "provider-auth-bridge-run",
            "paper_id": "provider-auth-bridge-paper",
            "title": "Provider Auth Bridge Paper",
            "abstract_text": "A local fixture paper for provider command preview planning.",
            "text_excerpt": "Grounded provider preview evidence for redaction testing.",
            "full_text_status": "reviewed",
            "relevance_score": 90,
            "evidence_strength": 80,
            "source_text_sha256": "provider-auth-bridge-sha",
            "text_char_count": 56,
            "evidence_spans": [
                {
                    "span_id": "provider-auth-bridge-span",
                    "text": "Grounded provider preview evidence for redaction testing.",
                }
            ],
            "review_source_type": "full_text",
        }
    )
    command = [
        "provider-cli",
        "--profile-json",
        "C:/Users/operator/live-profile.json",
        "--api-key=sk-secret-value",
    ]

    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="dry_run",
        command=command,
        limit=1,
    )

    preview = result["results"][0]["planned_invocation"]["command_preview"]
    assert preview == ["provider-cli", "--profile-json", "<redacted-path>", "<redacted>"]
    assert "sk-secret-value" not in str(result)

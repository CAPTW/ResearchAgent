"""Provider output-class proof acceptance contract tests.

These prove the fail-closed boundaries required before any provider-backed
paper_review / weekly_report output could be treated as accepted:
- output stays blocked without an exact output-class proof,
- tiny smoke / auth / env presence never unlock output,
- a deterministic fixture proof is representable but never unlocking,
- proof records carry no secrets/tokens/cache/session content,
- provider auth is not App OAuth/OIDC, local MCP is not a public MCP connector,
- routing refuses unverified providers for both output classes,
- the runtime provider gate rejects a bare tiny sentinel.

No provider is executed and no network/OAuth/MCP call is made anywhere here.
"""
from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from app.services import provider_output_class_proof as ocp
from app.services import provider_orchestration as po
from app.services import providers as provider_registry
from app.services.providers import describe_provider_status, get_active_provider


def _profile(provider_id: str, status: str, **overrides) -> dict:
    payload = {
        "providerId": provider_id,
        "executionSurface": "cli",
        "status": status,
        "researchOutputAllowed": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("output_class", ["paper_review", "weekly_report"])
@pytest.mark.parametrize("proof_mode", ["deterministic_fixture", "dry_run", "tiny_live"])
def test_non_live_proof_modes_never_unlock_output(output_class: str, proof_mode: str) -> None:
    proof = ocp.build_output_class_proof(
        provider_id="codex_cli",
        output_class=output_class,
        proof_mode=proof_mode,
        profile_status="configured_not_verified",
        configured=True,
    )
    assert proof.allowed is False
    assert proof.accepted is False
    assert proof.blocked_reason is not None
    assert proof.output_class == output_class


def test_tiny_proof_status_is_reported_but_still_blocked() -> None:
    proof = ocp.build_output_class_proof(
        provider_id="antigravity_cli",
        output_class="paper_review",
        proof_mode="tiny_live",
        profile_status="live_verified_tiny",
        configured=True,
    )
    # A tiny execution proof is a status signal only; it unlocks nothing.
    assert proof.status == "tiny_execution_verified"
    assert proof.allowed is False
    assert proof.current_proof_level == "live_verified_tiny"


@pytest.mark.parametrize(
    "output_class, expected_status",
    [
        ("paper_review", "paper_review_output_class_verified"),
        ("weekly_report", "weekly_report_output_class_verified"),
    ],
)
def test_output_class_live_is_the_only_mode_that_unlocks(output_class: str, expected_status: str) -> None:
    proof = ocp.build_output_class_proof(
        provider_id="openai_api",
        output_class=output_class,
        proof_mode="output_class_live",
        profile_status="live_verified",
        paper_ids=["paper-1"],
        full_text_evidence_source="run:raw/paper-1.txt",
        evidence_span_count=2,
        artifact_ref="run:reports/output_class_proof.json",
        configured=True,
    )
    assert proof.allowed is True
    assert proof.accepted is True
    assert proof.status == expected_status
    assert proof.current_proof_level == ocp.PRODUCT_OUTPUT_CLASS_REQUIRED_PROOF[output_class]


def test_output_class_live_still_blocked_when_provider_status_is_policy_sensitive() -> None:
    proof = ocp.build_output_class_proof(
        provider_id="claude_code",
        output_class="paper_review",
        proof_mode="output_class_live",
        profile_status="policy_sensitive",
        configured=True,
    )
    assert proof.allowed is False
    assert proof.blocked_reason == "provider_status_policy_sensitive"


def test_unknown_output_class_is_blocked_without_consulting_provider() -> None:
    proof = ocp.build_output_class_proof(
        provider_id="openai_api",
        output_class="journal_acceptance",
        proof_mode="output_class_live",
        profile_status="live_verified",
    )
    assert proof.allowed is False
    assert proof.blocked_reason == "unknown_output_class"


def test_wrong_output_class_proof_does_not_cross_unlock() -> None:
    # A report_draft proof level must not unlock paper_review (research_output) output.
    gate = po.evaluate_provider_output_gate(
        provider_id="openai_api",
        output_class="research_output",
        current_proof_level="report_draft_verified_tiny",
        proof_present=True,
        accepted=True,
        profile_status="live_verified",
    )
    assert gate.allowed is False
    assert gate.blocked_reason == "insufficient_proof_level_for_output_class"


def test_deterministic_harness_is_non_live_and_blocked() -> None:
    result = ocp.run_deterministic_output_class_proof_harness(output_class="paper_review")
    assert result["nonLive"] is True
    assert result["liveCommandDisabled"] is True
    assert result["networkDisabled"] is True
    assert result["outputAllowed"] is False
    assert result["accepted"] is False
    assert result["secretScanClean"] is True
    assert result["secretScanOffences"] == []
    assert result["proof"]["proofMode"] in ocp.NON_LIVE_PROOF_MODES


def test_harness_refuses_to_represent_a_live_mode() -> None:
    # Even if a caller asks the non-live harness for a live mode, it must stay non-live.
    result = ocp.run_deterministic_output_class_proof_harness(proof_mode="output_class_live")
    assert result["proofMode"] in ocp.NON_LIVE_PROOF_MODES
    assert result["outputAllowed"] is False


def test_proof_record_contains_no_secret_or_cache_content() -> None:
    proof = ocp.build_output_class_proof(
        provider_id="openai_api",
        output_class="paper_review",
        proof_mode="output_class_live",
        profile_status="live_verified",
        artifact_ref="run:reports/output_class_proof.json",
        configured=True,
    )
    payload = json.dumps(proof.to_dict()).lower()
    for marker in ("auth.json", ".mcp.json", "api_key", "access_token", "keychain", "/cache", "session_secret"):
        assert marker not in payload
    assert ocp.scan_for_forbidden_content(proof.to_dict()) == []


def test_secret_like_field_fails_the_proof_closed() -> None:
    proof = ocp.build_output_class_proof(
        provider_id="openai_api",
        output_class="paper_review",
        proof_mode="output_class_live",
        profile_status="live_verified",
        # A caller trying to smuggle a credential-cache path into the artifact ref.
        artifact_ref="/home/op/.cache/openai/auth.json",
        configured=True,
    )
    assert proof.allowed is False
    assert proof.accepted is False
    assert proof.blocked_reason == "secret_like_proof_field"
    assert "secret_like_proof_field" in proof.reason_codes


def test_provider_auth_is_not_app_oauth_oidc_and_local_mcp_is_not_public_connector() -> None:
    for surface in ("cli", "mcp"):
        proof = ocp.build_output_class_proof(
            provider_id="local_mcp_connector",
            output_class="paper_review",
            proof_mode="output_class_live",
            execution_surface=surface,
            profile_status="live_verified",
            configured=True,
        )
        assert proof.app_oauth_oidc_in_scope is False
        assert proof.public_mcp_connector_in_scope is False
        assert "app_oauth_oidc_not_in_scope" in proof.not_in_scope
        assert "public_mcp_not_in_scope" in proof.not_in_scope
        # The mcp execution surface never implies a public ChatGPT connector proof.
        assert proof.execution_surface == surface


@pytest.mark.parametrize("output_class", ["paper_review", "weekly_report"])
def test_routing_refuses_unverified_provider_for_each_output_class(output_class: str) -> None:
    decision = ocp.route_output_class_request(
        output_class=output_class,
        profile=_profile("codex_cli", "configured_not_verified"),
        routing_mode="single_primary",
        accepted_proof=None,
    )
    assert decision["allowed"] is False
    assert decision["deterministicFallback"] is True
    assert decision["productOutputClass"] == output_class
    assert decision["blockedReason"] is not None
    assert "app_oauth_oidc_not_in_scope" in decision["notInScope"]


def test_routing_allows_only_with_matching_accepted_output_class_live_proof() -> None:
    accepted = ocp.build_output_class_proof(
        provider_id="openai_api",
        output_class="paper_review",
        proof_mode="output_class_live",
        profile_status="live_verified",
        artifact_ref="run:reports/output_class_proof.json",
        configured=True,
    )
    assert accepted.accepted is True
    decision = ocp.route_output_class_request(
        output_class="paper_review",
        profile=_profile("openai_api", "live_verified"),
        routing_mode="single_primary",
        accepted_proof=accepted,
    )
    assert decision["allowed"] is True
    # The same accepted proof must not cross-unlock the other output class.
    cross = ocp.route_output_class_request(
        output_class="weekly_report",
        profile=_profile("openai_api", "live_verified"),
        routing_mode="single_primary",
        accepted_proof=accepted,
    )
    assert cross["allowed"] is False


def test_status_taxonomy_covers_required_distinctions() -> None:
    for status in (
        "provider_auth_present_or_configured",
        "tiny_execution_verified",
        "paper_review_output_class_verified",
        "weekly_report_output_class_verified",
        "output_blocked_pending_proof",
        "live_command_disabled",
        "app_oauth_oidc_not_in_scope",
        "public_mcp_not_in_scope",
    ):
        assert status in ocp.OUTPUT_CLASS_PROOF_STATUSES


# ---- runtime provider gate: bare tiny sentinel must not unlock the active provider ----


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XRML_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-live-key")
    monkeypatch.setenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", "1")


def test_runtime_gate_rejects_bare_tiny_sentinel_even_with_present_artifact(tmp_path, monkeypatch) -> None:
    artifact = tmp_path / "provider-proof.json"
    artifact.write_text("{}", encoding="utf-8")
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "live_verified_tiny")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    status = describe_provider_status()
    assert status["researchOutputAllowed"] is False
    assert status["provider_mode"] == "configured_no_project_live_proof"
    assert status["canonicalStatus"] == "configured_not_verified"
    assert get_active_provider() is None


def test_runtime_gate_rejects_environment_label_and_empty_json_artifact(tmp_path, monkeypatch) -> None:
    artifact = tmp_path / "provider-proof.json"
    artifact.write_text("{}", encoding="utf-8")
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "research_output_verified_tiny")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    status = describe_provider_status()
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False
    assert status["live_call_enabled"] is False
    assert get_active_provider() is None


@pytest.mark.parametrize(
    "proof_status",
    [
        "configured_not_verified",
        "provider_smoke_only",
        "live_verified_tiny",
        "research_output_verified_tiny",
        "report_draft_verified_tiny",
        "live_verified",
    ],
)
def test_runtime_factory_blocks_every_current_status_label_without_request_binding(
    proof_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", proof_status)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", raising=False)

    status = describe_provider_status()

    assert get_active_provider() is None
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False
    assert status["live_call_enabled"] is False


@pytest.mark.parametrize(
    "artifact_case",
    [
        "missing",
        "empty_file",
        "empty_json_object",
        "arbitrary_text",
        "json_list",
        "malformed_json",
        "directory",
        "unrelated_regular_file",
    ],
)
def test_runtime_factory_blocks_every_artifact_path_shape(
    artifact_case: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / f"{artifact_case}.proof"
    if artifact_case == "empty_file":
        artifact.write_text("", encoding="utf-8")
    elif artifact_case == "empty_json_object":
        artifact.write_text("{}", encoding="utf-8")
    elif artifact_case == "arbitrary_text":
        artifact.write_text("this is not a proof", encoding="utf-8")
    elif artifact_case == "json_list":
        artifact.write_text("[]", encoding="utf-8")
    elif artifact_case == "malformed_json":
        artifact.write_text("{not-json", encoding="utf-8")
    elif artifact_case == "directory":
        artifact.mkdir()
    elif artifact_case == "unrelated_regular_file":
        artifact.write_text("unrelated local file", encoding="utf-8")

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "research_output_verified_tiny")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    assert get_active_provider() is None
    assert describe_provider_status()["researchOutputAllowed"] is False


@pytest.mark.parametrize(
    "provider_id, execution_surface",
    [
        ("another_provider", "api"),
        ("openai", "another_surface"),
        ("openai", "api"),
    ],
    ids=["provider_mismatch", "surface_mismatch", "nominal_shape_still_untrusted"],
)
def test_runtime_factory_does_not_accept_synthetic_provider_or_surface_claims(
    provider_id: str,
    execution_surface: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "synthetic-provider-proof.json"
    artifact.write_text(
        json.dumps(
            {
                "providerId": provider_id,
                "executionSurface": execution_surface,
                "outputClass": "research_output",
                "proofMode": "output_class_live",
                "accepted": True,
                "currentProofLevel": "research_output_verified_tiny",
            }
        ),
        encoding="utf-8",
    )
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "research_output_verified_tiny")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    assert get_active_provider() is None
    assert describe_provider_status()["researchOutputAllowed"] is False


@pytest.mark.parametrize(
    "proof_status, claimed_output_class",
    [
        ("research_output_verified_tiny", "report_draft"),
        ("report_draft_verified_tiny", "research_output"),
        ("live_verified", "research_output"),
        ("live_verified", "report_draft"),
    ],
)
def test_runtime_factory_blocks_cross_output_class_and_broad_status_claims(
    proof_status: str,
    claimed_output_class: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "synthetic-output-class-proof.json"
    artifact.write_text(
        json.dumps(
            {
                "providerId": "openai",
                "executionSurface": "api",
                "outputClass": claimed_output_class,
                "accepted": True,
                "currentProofLevel": proof_status,
            }
        ),
        encoding="utf-8",
    )
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", proof_status)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    status = describe_provider_status()

    assert get_active_provider() is None
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False


def test_runtime_factory_constructs_no_provider_or_network_capability(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "provider-proof.json"
    artifact.write_text("{}", encoding="utf-8")
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "live_verified")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("external provider, HTTP client, or subprocess was invoked")

    monkeypatch.setattr(provider_registry, "OpenAIProvider", unexpected_call)
    monkeypatch.setattr(httpx, "Client", unexpected_call)
    monkeypatch.setattr(subprocess, "run", unexpected_call)

    status = provider_registry.describe_provider_status()

    assert provider_registry.get_active_provider() is None
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False


def test_runtime_factory_preserves_deterministic_no_provider_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "provider-proof.json"
    artifact.write_text("{}", encoding="utf-8")
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "live_verified")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    assert get_active_provider() is None
    assert describe_provider_status()["provider_mode"] == "configured_no_project_live_proof"


def test_runtime_provider_diagnostics_do_not_expose_synthetic_secrets_or_proof_contents(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_key = "synthetic-key-value-that-must-not-appear"
    synthetic_model = "operator-private-model-that-must-not-appear"
    synthetic_endpoint_token = "endpoint-token-that-must-not-appear"
    synthetic_proof_content = "proof-content-that-must-not-appear"
    artifact = tmp_path / "provider-proof.json"
    artifact.write_text(synthetic_proof_content, encoding="utf-8")
    monkeypatch.setenv("XRML_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", synthetic_key)
    monkeypatch.setenv("OPENAI_MODEL", synthetic_model)
    monkeypatch.setenv("OPENAI_API_BASE", f"https://example.invalid/v1?token={synthetic_endpoint_token}")
    monkeypatch.setenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", "1")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "live_verified")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", str(artifact))

    status = describe_provider_status()
    serialized = json.dumps(status)

    assert get_active_provider() is None
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False
    for forbidden_value in (
        synthetic_key,
        synthetic_model,
        synthetic_endpoint_token,
        synthetic_proof_content,
    ):
        assert forbidden_value not in serialized

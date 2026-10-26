"""Fail-closed provider orchestration contract tests.

These tests prove that the provider multi-backend orchestration contract keeps every real
provider output-blocked by default, that stage routing defaults to disabled, that the
Antigravity tiny-sentinel proof unlocks no output class, and that only an exact accepted
output-class proof unlocks output. No provider is executed anywhere in these tests.
"""
from __future__ import annotations

import json


from app.services import provider_configurations
from app.services import provider_orchestration as po


def _profile(provider_id: str, status: str, **overrides) -> dict:
    payload = {
        "providerId": provider_id,
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": status,
        "researchOutputAllowed": False,
    }
    payload.update(overrides)
    return payload


def test_default_stage_policy_disabled_for_all_six_stages() -> None:
    policy = po.default_provider_routing_policy()
    assert policy.mode == "disabled"
    stages = {sp.stage for sp in policy.stage_policies}
    assert stages == set(po.PROVIDER_STAGES)
    assert len(po.PROVIDER_STAGES) == 6
    for sp in policy.stage_policies:
        assert sp.mode == "disabled"
        assert sp.fallback_to_deterministic is True
        assert sp.preview_only is True
        assert sp.candidates == ()


def test_real_providers_cannot_produce_research_output_by_default() -> None:
    contract = po.build_provider_orchestration_contract()
    assert contract.all_output_blocked is True
    assert contract.deterministic_local_fallback is True
    for decision in contract.decisions:
        assert decision.allowed is False
        assert decision.deterministic_fallback is True
    for gate in contract.output_gates:
        assert gate.allowed is False
    payload = contract.to_dict()
    assert payload["researchOutputAllowed"] is False
    assert payload["reportGenerationAllowed"] is False
    assert payload["orchestrationAllowed"] is False
    assert payload["allOutputBlocked"] is True


def test_antigravity_cli_tiny_proof_does_not_unlock_output() -> None:
    # Even with an accepted live_verified_tiny (sentinel) proof present, output stays blocked.
    for output_class in po.PROVIDER_OUTPUT_CLASSES:
        gate = po.evaluate_provider_output_gate(
            provider_id="antigravity_cli",
            output_class=output_class,
            current_proof_level="live_verified_tiny",
            proof_present=True,
            accepted=True,
            profile_status="provider_smoke_only",
        )
        assert gate.allowed is False
        assert gate.blocked_reason == "insufficient_proof_level_for_output_class"


def test_codex_cli_configured_not_verified_cannot_produce_output() -> None:
    for output_class in po.PROVIDER_OUTPUT_CLASSES:
        gate = po.evaluate_provider_output_gate(
            provider_id="codex_cli",
            output_class=output_class,
            current_proof_level="configured_not_verified",
            proof_present=False,
            accepted=False,
            profile_status="configured_not_verified",
        )
        assert gate.allowed is False
        assert gate.blocked_reason == "output_class_proof_missing"


def test_claude_code_policy_sensitive_is_blocked() -> None:
    # Policy-sensitive blocks even if a proof were somehow present.
    gate = po.evaluate_provider_output_gate(
        provider_id="claude_code",
        output_class="research_output",
        current_proof_level="research_output_verified_tiny",
        proof_present=True,
        accepted=True,
        profile_status="policy_sensitive",
    )
    assert gate.allowed is False
    assert gate.blocked_reason == "provider_status_policy_sensitive"


def test_claude_api_configured_not_verified_is_blocked() -> None:
    gate = po.evaluate_provider_output_gate(
        provider_id="claude_api",
        output_class="research_output",
        current_proof_level="configured_not_verified",
        proof_present=False,
        accepted=False,
        profile_status="configured_not_verified",
    )
    assert gate.allowed is False
    assert gate.blocked_reason == "output_class_proof_missing"


def test_stub_and_fixture_deterministic_path_remains_local() -> None:
    # Deterministic-local providers are excluded from external output gating in the contract.
    profiles = provider_configurations.example_provider_profiles()
    contract = po.build_provider_orchestration_contract(profiles)
    gated_ids = {gate.provider_id for gate in contract.output_gates}
    assert "stub" not in gated_ids
    assert "fixture-command" not in gated_ids
    assert contract.deterministic_local_fallback is True


def test_weighted_routing_rejects_invalid_weights() -> None:
    negative = po.ProviderRoutingPolicy(
        mode="weighted_round_robin",
        stage_policies=(
            po.ProviderStagePolicy(stage="scout_assist", mode="weighted_round_robin", candidates=(("codex_cli", -1),)),
        ),
    )
    issues = po.validate_provider_routing_policy(negative)
    assert any(issue["code"] == "negative_weight" for issue in issues)

    all_zero = po.ProviderRoutingPolicy(
        mode="weighted_round_robin",
        stage_policies=(
            po.ProviderStagePolicy(stage="scout_assist", mode="weighted_round_robin", candidates=(("codex_cli", 0), ("claude_api", 0))),
        ),
    )
    zero_issues = po.validate_provider_routing_policy(all_zero)
    assert any(issue["code"] == "all_zero_weights" for issue in zero_issues)

    unknown_stage = po.ProviderRoutingPolicy(
        mode="stage_specific",
        stage_policies=(po.ProviderStagePolicy(stage="not_a_stage", mode="single_primary"),),
    )
    assert any(issue["code"] == "unknown_stage" for issue in po.validate_provider_routing_policy(unknown_stage))

    unknown_mode = {"mode": "turbo", "stagePolicies": []}
    assert any(issue["code"] == "unknown_routing_mode" for issue in po.validate_provider_routing_policy(unknown_mode))

    unconfigured = po.ProviderRoutingPolicy(
        mode="single_primary",
        stage_policies=(po.ProviderStagePolicy(stage="scout_assist", mode="single_primary", candidates=(("ghost_cli", 1),)),),
    )
    assert any(
        issue["code"] == "provider_not_configured"
        for issue in po.validate_provider_routing_policy(unconfigured, configured_provider_ids={"codex_cli"})
    )

    # A valid weighted policy produces no issues.
    valid = po.ProviderRoutingPolicy(
        mode="weighted_round_robin",
        stage_policies=(
            po.ProviderStagePolicy(stage="scout_assist", mode="weighted_round_robin", candidates=(("codex_cli", 2), ("claude_api", 1))),
        ),
    )
    assert po.validate_provider_routing_policy(valid) == []


def test_weighted_routing_dry_run_is_preview_only_not_execution() -> None:
    # Even with a non-disabled routing mode, a decision without an output-class proof is blocked
    # and never represents execution.
    profile = _profile("codex_cli", "configured_not_verified")
    decision = po.build_provider_stage_routing_decision(
        stage="scout_assist", profile=profile, routing_mode="weighted_round_robin"
    )
    assert decision.allowed is False
    assert decision.blocked_reason == "output_class_proof_missing"
    assert decision.deterministic_fallback is True
    assert any("Dry-run routing preview only" in warning for warning in decision.warnings)


def test_exact_output_class_proof_required_before_output() -> None:
    # research_output is unlocked ONLY by research_output_verified_tiny (or live_verified).
    research_ok = po.evaluate_provider_output_gate(
        provider_id="codex_cli",
        output_class="research_output",
        current_proof_level="research_output_verified_tiny",
        proof_present=True,
        accepted=True,
        profile_status="live_verified_tiny",
    )
    assert research_ok.allowed is True
    assert research_ok.blocked_reason is None

    # report_draft is NOT unlocked by a research_output proof.
    wrong_class = po.evaluate_provider_output_gate(
        provider_id="codex_cli",
        output_class="report_draft",
        current_proof_level="research_output_verified_tiny",
        proof_present=True,
        accepted=True,
        profile_status="live_verified_tiny",
    )
    assert wrong_class.allowed is False
    assert wrong_class.blocked_reason == "insufficient_proof_level_for_output_class"

    # report_draft is unlocked by report_draft_verified_tiny.
    report_ok = po.evaluate_provider_output_gate(
        provider_id="codex_cli",
        output_class="report_draft",
        current_proof_level="report_draft_verified_tiny",
        proof_present=True,
        accepted=True,
    )
    assert report_ok.allowed is True

    # live_verified unlocks both classes.
    for output_class in po.PROVIDER_OUTPUT_CLASSES:
        strong = po.evaluate_provider_output_gate(
            provider_id="codex_cli",
            output_class=output_class,
            current_proof_level="live_verified",
            proof_present=True,
            accepted=True,
        )
        assert strong.allowed is True


def test_no_credential_or_cache_paths_in_contract_output() -> None:
    contract = po.build_provider_orchestration_contract()
    serialized = json.dumps(contract.to_dict())
    for fragment in (".mcp.json", "auth.json", "/credentials", "/cache", "browser/session", "browser/cookies", "keychain"):
        assert fragment not in serialized


def test_command_preview_stays_redacted() -> None:
    profile = _profile("antigravity_cli", "provider_smoke_only")
    decision = po.build_provider_stage_routing_decision(
        stage="full_text_review_assist",
        profile=profile,
        routing_mode="single_primary",
        redacted_command_preview=["antigravity", "--packet", "<redacted-packet-path>", "--output", "<redacted-output-path>"],
        proof_artifact_refs=["output/provider-live-proof/PROVIDER_LIVE_TINY_PROOF_EXECUTION/provider-live-tiny.json"],
    )
    preview = decision.redacted_command_preview or ()
    joined = " ".join(preview)
    assert "<redacted-packet-path>" in joined
    # No secret/token/credential literal leaks into the preview.
    for fragment in ("token", "secret", "api_key", "auth.json", ".mcp.json"):
        assert fragment not in joined.lower()
    # Proof refs are metadata paths only (no content).
    assert decision.proof_artifact_refs and all(ref.endswith(".json") for ref in decision.proof_artifact_refs)


def test_contract_status_uses_non_overclaim_wording() -> None:
    contract = po.build_provider_orchestration_contract()
    payload = contract.to_dict()
    text = json.dumps(payload).lower()
    # Fail-closed flags present and false.
    assert payload["researchOutputAllowed"] is False
    assert payload["reportGenerationAllowed"] is False
    assert payload["orchestrationAllowed"] is False
    # No overclaiming readiness wording.
    for banned in ("production-ready", "external beta ready", "\"ready\"", "oauth connected"):
        assert banned not in text


def test_deterministic_local_fallback_available_for_every_decision() -> None:
    contract = po.build_provider_orchestration_contract()
    assert contract.decisions  # non-empty
    for decision in contract.decisions:
        assert decision.deterministic_fallback is True
    # Unknown stage is blocked, not allowed.
    unknown = po.build_provider_stage_routing_decision(stage="not_a_stage", profile=_profile("codex_cli", "configured_not_verified"), routing_mode="single_primary")
    assert unknown.allowed is False
    assert unknown.blocked_reason == "unknown_stage"


def test_proof_level_for_profile_is_fail_closed() -> None:
    assert po.proof_level_for_profile({"status": "policy_sensitive"}) == "none"
    assert po.proof_level_for_profile({"status": "unsupported"}) == "none"
    assert po.proof_level_for_profile({"status": "provider_smoke_only"}) == "provider_smoke_only"
    assert po.proof_level_for_profile({"status": "live_verified_tiny"}) == "live_verified_tiny"
    assert po.proof_level_for_profile({"status": "totally_unknown"}) == "none"
    assert po.proof_level_for_profile(None) == "none"


def test_provider_surface_state_classifies_execution_types_without_unlocking_output() -> None:
    profiles = {profile["providerId"]: profile for profile in provider_configurations.example_provider_profiles()}

    assert set(po.PROVIDER_EXECUTION_TYPES) == {"cli", "sdk", "api", "mcp", "fixture"}
    assert set(po.PROVIDER_STATE_TAXONOMY) == {
        "unavailable",
        "configured_not_verified",
        "live_verified_tiny",
        "output_class_verified",
        "research_output_allowed",
    }
    assert po.build_provider_surface_state(profiles["stub"])["executionType"] == "fixture"
    assert po.build_provider_surface_state(profiles["fixture-command"])["executionType"] == "fixture"
    assert po.build_provider_surface_state(profiles["codex_cli"])["executionType"] == "cli"
    assert po.build_provider_surface_state(profiles["claude_api"])["executionType"] == "api"
    assert po.build_provider_surface_state(profiles["antigravity_sdk"])["executionType"] == "sdk"

    mcp_state = po.build_provider_surface_state(
        _profile("local_mcp_connector", "live_verified_tiny", executionSurface="mcp", providerType="mcp")
    )
    assert mcp_state["executionType"] == "mcp"
    assert mcp_state["state"] == "live_verified_tiny"
    assert mcp_state["outputAllowed"] is False
    assert mcp_state["researchOutputAllowed"] is False
    assert mcp_state["wouldExecuteProvider"] is False

    codex_state = po.build_provider_surface_state(profiles["codex_cli"])
    assert codex_state["state"] == "configured_not_verified"
    assert codex_state["outputAllowed"] is False
    assert codex_state["wouldExecuteProvider"] is False

    claude_state = po.build_provider_surface_state(profiles["claude_code"])
    assert claude_state["state"] == "unavailable"
    assert claude_state["blockedReason"] == "provider_status_policy_sensitive"

    tiny_state = po.build_provider_surface_state(_profile("antigravity_cli", "live_verified_tiny"))
    assert tiny_state["state"] == "live_verified_tiny"
    assert tiny_state["outputAllowed"] is False


def test_provider_surface_state_requires_exact_output_class_proof() -> None:
    profile = _profile("codex_cli", "configured_not_verified")

    research_state = po.build_provider_surface_state(
        profile,
        output_class="research_output",
        accepted_output_proofs={"codex_cli": {"research_output": "research_output_verified_tiny"}},
    )
    assert research_state["state"] == "research_output_allowed"
    assert research_state["researchOutputAllowed"] is True
    assert research_state["outputAllowed"] is True

    wrong_report_proof = po.build_provider_surface_state(
        profile,
        output_class="report_draft",
        accepted_output_proofs={"codex_cli": {"report_draft": "research_output_verified_tiny"}},
    )
    assert wrong_report_proof["state"] == "configured_not_verified"
    assert wrong_report_proof["blockedReason"] == "insufficient_proof_level_for_output_class"
    assert wrong_report_proof["outputAllowed"] is False

    report_state = po.build_provider_surface_state(
        profile,
        output_class="report_draft",
        accepted_output_proofs={"codex_cli": {"report_draft": "report_draft_verified_tiny"}},
    )
    assert report_state["state"] == "output_class_verified"
    assert report_state["researchOutputAllowed"] is False
    assert report_state["reportGenerationAllowed"] is False
    assert report_state["outputAllowed"] is True

"""Fail-closed provider weighted / stage routing dry-run preview tests.

These tests prove that weighted / single_primary / priority_fallback / stage_specific routing
previews are deterministic, never execute a provider, and never allow output unless an accepted
exact output-class proof exists for the selected provider's surface. No provider is executed.
"""
from __future__ import annotations

import json


from app.services import provider_orchestration as po


def _policy(stage="scout_assist", mode="weighted_round_robin", candidates=None, output_class=None) -> dict:
    return {"stage": stage, "routingMode": mode, "candidates": candidates or [], "outputClass": output_class}


def _cand(provider_id, weight):
    return {"providerId": provider_id, "weight": weight}


def _profile(provider_id, status="configured_not_verified"):
    return {"providerId": provider_id, "status": status}


# --- validation ---------------------------------------------------------------

def test_weighted_routing_rejects_negative_weights() -> None:
    issues = po.validate_provider_weighted_policy(_policy(candidates=[_cand("codex_cli", -1)]))
    assert any(issue["code"] == "negative_weight" for issue in issues)


def test_weighted_routing_rejects_all_zero_weights() -> None:
    issues = po.validate_provider_weighted_policy(_policy(candidates=[_cand("codex_cli", 0), _cand("claude_api", 0)]))
    assert any(issue["code"] == "all_zero_weights" for issue in issues)


def test_weighted_routing_rejects_non_numeric_weights() -> None:
    issues = po.validate_provider_weighted_policy(_policy(candidates=[_cand("codex_cli", "two")]))
    assert any(issue["code"] == "non_numeric_weight" for issue in issues)


def test_weighted_routing_rejects_unknown_provider_ids() -> None:
    issues = po.validate_provider_weighted_policy(
        _policy(candidates=[_cand("ghost_cli", 1)]), configured_provider_ids={"codex_cli", "claude_api"}
    )
    assert any(issue["code"] == "provider_not_configured" for issue in issues)


def test_weighted_routing_rejects_duplicate_provider_ids() -> None:
    issues = po.validate_provider_weighted_policy(_policy(candidates=[_cand("codex_cli", 1), _cand("codex_cli", 2)]))
    assert any(issue["code"] == "duplicate_provider_id" for issue in issues)


def test_weighted_routing_blocks_policy_sensitive_provider() -> None:
    issues = po.validate_provider_weighted_policy(
        _policy(candidates=[_cand("claude_code", 1)]), provider_statuses={"claude_code": "policy_sensitive"}
    )
    assert any(issue["code"] == "policy_sensitive_blocked" for issue in issues)


def test_weighted_routing_blocks_unsupported_provider() -> None:
    issues = po.validate_provider_weighted_policy(
        _policy(candidates=[_cand("weird_cli", 1)]), provider_statuses={"weird_cli": "unsupported"}
    )
    assert any(issue["code"] == "unsupported_blocked" for issue in issues)


def test_weighted_routing_rejects_unknown_stage_mode_and_output_class() -> None:
    issues = po.validate_provider_weighted_policy(_policy(stage="nope", mode="turbo", output_class="weird"))
    codes = {issue["code"] for issue in issues}
    assert {"unknown_stage", "unknown_routing_mode", "unknown_output_class"}.issubset(codes)


def test_valid_weighted_policy_has_no_issues() -> None:
    issues = po.validate_provider_weighted_policy(
        _policy(candidates=[_cand("codex_cli", 2), _cand("claude_api", 1)]),
        configured_provider_ids={"codex_cli", "claude_api"},
        provider_statuses={"codex_cli": "configured_not_verified", "claude_api": "configured_not_verified"},
    )
    assert issues == []


# --- preview: fail-closed output ---------------------------------------------

def test_configured_not_verified_provider_blocked_for_research_output() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="scout_assist", routing_mode="single_primary",
        candidates=[("codex_cli", 1)], provider_profiles=[_profile("codex_cli", "configured_not_verified")],
    )
    assert preview["allowed"] is False
    assert preview["blockedReason"] == "output_class_proof_missing"
    assert preview["wouldExecuteProvider"] is False


def test_provider_smoke_only_provider_blocked_for_research_output() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="scout_assist", routing_mode="single_primary",
        candidates=[("antigravity_cli", 1)], provider_profiles=[_profile("antigravity_cli", "provider_smoke_only")],
    )
    assert preview["allowed"] is False


def test_antigravity_live_verified_tiny_sentinel_does_not_unlock_research_output() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="scout_assist", routing_mode="single_primary",
        candidates=[("antigravity_cli", 1)], provider_profiles=[_profile("antigravity_cli", "provider_smoke_only")],
        accepted_output_proofs={"antigravity_cli": {"research_output": "live_verified_tiny"}},
    )
    assert preview["allowed"] is False
    assert preview["blockedReason"] == "insufficient_proof_level_for_output_class"


def test_policy_sensitive_provider_blocked_in_preview() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="scout_assist", routing_mode="single_primary",
        candidates=[("claude_code", 1)], provider_profiles=[_profile("claude_code", "policy_sensitive")],
        accepted_output_proofs={"claude_code": {"research_output": "research_output_verified_tiny"}},
    )
    assert preview["allowed"] is False
    assert preview["blockedReason"] == "provider_status_policy_sensitive"


# --- preview: selection modes -------------------------------------------------

def test_single_primary_selects_provider_but_output_blocked() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="triage_assist", routing_mode="single_primary",
        candidates=[("claude_api", 1)], provider_profiles=[_profile("claude_api")],
    )
    assert preview["selectedProviderId"] == "claude_api"
    assert preview["allowed"] is False


def test_priority_fallback_returns_first_candidate_but_output_blocked() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="triage_assist", routing_mode="priority_fallback",
        candidates=[("codex_cli", 1), ("claude_api", 1)], provider_profiles=[_profile("codex_cli")],
    )
    assert preview["selectedProviderId"] == "codex_cli"
    assert preview["allowed"] is False


def test_weighted_preview_is_deterministic_with_same_fingerprint() -> None:
    candidates = [("codex_cli", 3), ("claude_api", 1), ("antigravity_cli", 2)]
    first = po.select_weighted_provider_preview(candidates, stage="scout_assist", request_fingerprint="req-1")
    second = po.select_weighted_provider_preview(candidates, stage="scout_assist", request_fingerprint="req-1")
    assert first == second
    assert first in {"codex_cli", "claude_api", "antigravity_cli"}
    p1 = po.build_provider_routing_preview_for_stage(stage="scout_assist", routing_mode="weighted_round_robin", candidates=candidates, request_fingerprint="req-1")
    p2 = po.build_provider_routing_preview_for_stage(stage="scout_assist", routing_mode="weighted_round_robin", candidates=candidates, request_fingerprint="req-1")
    assert p1["selectedProviderId"] == p2["selectedProviderId"]


def test_stage_specific_missing_stage_defaults_disabled() -> None:
    preview = po.build_provider_weighted_routing_preview(po.default_provider_routing_policy())
    assert preview["allOutputBlocked"] is True
    assert preview["dryRunOnly"] is True
    assert preview["researchOutputAllowed"] is False
    for stage_preview in preview["stagePreviews"]:
        assert stage_preview["routingMode"] == "disabled"
        assert stage_preview["allowed"] is False
        assert stage_preview["blockedReason"] == "stage_routing_disabled"


def test_deterministic_local_fallback_remains_enabled() -> None:
    preview = po.build_provider_routing_preview_for_stage(stage="scout_assist", routing_mode="weighted_round_robin", candidates=[("codex_cli", 1)])
    assert preview["deterministicFallback"] is True


def test_dry_run_decisions_never_expose_runnable_execution() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="scout_assist", routing_mode="single_primary", candidates=[("codex_cli", 1)],
    )
    assert preview["dryRunOnly"] is True
    assert preview["wouldExecuteProvider"] is False
    for row in preview["providerDecisionMatrix"]:
        assert row["wouldExecuteProvider"] is False
    assert preview["proofArtifactRefs"] == []


def test_no_credential_or_mcp_paths_in_preview_output() -> None:
    serialized = json.dumps(po.build_provider_stage_preview_matrix())
    for fragment in (".mcp.json", "auth.json", "/credentials", "/cache", "browser/session", "browser/cookies", "keychain"):
        assert fragment not in serialized


# --- preview: exact output-class proof unlock ---------------------------------

def test_report_draft_stage_requires_report_draft_proof() -> None:
    # A research_output proof does NOT unlock the report_draft stage.
    wrong = po.build_provider_routing_preview_for_stage(
        stage="report_draft_assist", routing_mode="single_primary", candidates=[("codex_cli", 1)],
        provider_profiles=[_profile("codex_cli")],
        accepted_output_proofs={"codex_cli": {"research_output": "research_output_verified_tiny"}},
    )
    assert wrong["outputClass"] == "report_draft"
    assert wrong["allowed"] is False
    # The report_draft proof unlocks it.
    ok = po.build_provider_routing_preview_for_stage(
        stage="report_draft_assist", routing_mode="single_primary", candidates=[("codex_cli", 1)],
        provider_profiles=[_profile("codex_cli")],
        accepted_output_proofs={"codex_cli": {"report_draft": "report_draft_verified_tiny"}},
    )
    assert ok["allowed"] is True


def test_research_stage_requires_research_output_proof() -> None:
    ok = po.build_provider_routing_preview_for_stage(
        stage="full_text_review_assist", routing_mode="single_primary", candidates=[("codex_cli", 1)],
        provider_profiles=[_profile("codex_cli")],
        accepted_output_proofs={"codex_cli": {"research_output": "research_output_verified_tiny"}},
    )
    assert ok["outputClass"] == "research_output"
    assert ok["allowed"] is True


def test_live_verified_future_proof_unlocks_synthetic_provider() -> None:
    preview = po.build_provider_routing_preview_for_stage(
        stage="scout_assist", routing_mode="single_primary", candidates=[("synthetic_provider", 1)],
        provider_profiles=[_profile("synthetic_provider", "configured_not_verified")],
        accepted_output_proofs={"synthetic_provider": {"research_output": "live_verified"}},
    )
    assert preview["allowed"] is True


def test_preview_uses_no_overclaim_wording() -> None:
    payload = po.build_provider_weighted_routing_preview(po.default_provider_routing_policy())
    assert payload["researchOutputAllowed"] is False
    assert payload["reportGenerationAllowed"] is False
    assert payload["orchestrationAllowed"] is False
    text = json.dumps(payload).lower()
    for banned in ("production-ready", "external beta ready", "oauth connected"):
        assert banned not in text


def test_normalize_provider_weights_drops_invalid() -> None:
    normalized = po.normalize_provider_weights([_cand("codex_cli", 2), _cand("bad", -1), _cand("boolw", True), _cand("", 5)])
    ids = {pid for pid, _ in normalized}
    assert ids == {"codex_cli"}
    assert normalized == [("codex_cli", 2)]


def test_default_preview_matrix_blocks_every_real_provider() -> None:
    matrix = po.build_provider_stage_preview_matrix()
    assert matrix["allOutputBlocked"] is True
    assert matrix["wouldExecuteProvider"] is False
    stages = {row["stage"] for row in matrix["stages"]}
    assert stages == set(po.PROVIDER_STAGES)
    for stage_row in matrix["stages"]:
        assert stage_row["providers"]  # real providers present
        for provider_row in stage_row["providers"]:
            assert provider_row["outputAllowed"] is False

"""Canonical provider multi-backend routing contract tests (provider-free)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
import json

import pytest


NOW = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture(autouse=True)
def _provider_network_runtime_tripwires(monkeypatch):
    import socket
    import sqlite3
    import subprocess
    from pathlib import Path

    import httpx
    from app.services import provider_orchestration as po
    from app.services import provider_tiny_proof_operator_launcher
    from app.services import public_mcp_connector_proof_plan
    from app.services import providers as provider_registry
    from app.services.providers import codex_sdk_provider

    def blocked(*args, **kwargs):
        raise AssertionError("provider, network, OAuth/MCP, or persistence boundary invoked")

    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(httpx.Client, "request", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "request", blocked)
    monkeypatch.setattr(sqlite3, "connect", blocked)
    for method in ("write_text", "write_bytes", "touch", "rename", "replace"):
        monkeypatch.setattr(Path, method, blocked)
    monkeypatch.setattr(provider_registry, "OpenAIProvider", blocked)
    monkeypatch.setattr(codex_sdk_provider, "CodexSDKProvider", blocked)
    monkeypatch.setattr(codex_sdk_provider, "run_codex_sdk_tiny_sentinel_proof", blocked)
    monkeypatch.setattr(codex_sdk_provider, "run_codex_sdk_paper_review_output_class_proof", blocked)
    monkeypatch.setattr(codex_sdk_provider, "run_codex_sdk_weekly_report_output_class_proof", blocked)
    monkeypatch.setattr(provider_tiny_proof_operator_launcher, "run_codex_cli_tiny_proof_launcher", blocked)
    monkeypatch.setattr(po.ai_provider, "run_ai_review_provider", blocked)
    monkeypatch.setattr(po.oauth_token_exchange, "perform_live_exchange_pilot", blocked)
    monkeypatch.setattr(po.oauth_token_exchange, "perform_real_provider_smoke", blocked)
    monkeypatch.setattr(public_mcp_connector_proof_plan, "evaluate_public_mcp_connector_preconditions", blocked)
    monkeypatch.setattr(po.oauth_account_links, "_write_records", blocked)


def _modules():
    from app.services import provider_orchestration as po
    from app.services import provider_output_class_proof as proof

    return po, proof


def _profile(po, provider_id="provider-a", *, capability=None, policy_status="allowed", enabled=True):
    capability = capability or po.ProviderCapability.ASSIST_SCOUT
    return po.ProviderBackendProfile(
        provider_id=provider_id,
        family_id="family-a",
        display_name=provider_id,
        enabled=enabled,
        surface_ids=(f"{provider_id}-surface",),
        capabilities=frozenset({capability}),
        policy_status=policy_status,
        aliases=(),
    )


def _surface(po, provider_id="provider-a"):
    return po.ProviderExecutionSurface(
        surface_id=f"{provider_id}-surface",
        provider_id=provider_id,
        surface_type="cli",
        adapter_id="adapter-a",
        adapter_version="1.0",
        auth_mode="operator_external",
        command_shape_sha256=HASH_B,
        status="configured",
    )


def _proof(proof, po, provider_id="provider-a", *, level=None, stage=None, output_class=None):
    level = level or proof.ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED
    stage = stage or po.ProviderStage.SCOUT_ASSIST.value
    output_class = output_class or po.ProviderOutputClass.SCOUT_SUGGESTIONS.value
    identity = proof.ProviderProofIdentity(
        schema_version="1",
        policy_version="policy-1",
        provider_id=provider_id,
        surface_id=f"{provider_id}-surface",
        stage=stage,
        output_class=output_class,
        artifact_sha256=HASH_A,
        model="model-a",
        command_shape_sha256=HASH_B,
        adapter_id="adapter-a",
        adapter_version="1.0",
    )
    return proof.ProviderProofRecord(
        proof_id=proof.compute_provider_proof_id(identity),
        identity=identity,
        level=level,
        accepted_result_sha256=HASH_A,
        accepted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        revoked_at=None,
        limitations=(),
        accepted=True,
    )


def _candidate(po, provider_id="provider-a", *, proof_id="", capability=None, weight=1, priority=1):
    return po.ProviderRoutingCandidate(
        provider_id=provider_id,
        surface_id=f"{provider_id}-surface",
        enabled=True,
        capability=capability or po.ProviderCapability.ASSIST_SCOUT,
        proof_id=proof_id,
        weight=weight,
        priority=priority,
    )


def _key(stage="scout_assist"):
    return {
        "policyVersion": "policy-1",
        "runId": "run-1",
        "stage": stage,
        "topicId": "topic-1",
        "artifactId": "artifact-1",
        "operatorStableKey": "operator-1",
    }


def _preview(*, mode="single_primary", profiles=None, surfaces=None, proofs=None, candidates=None, stage="scout_assist", output_class="scout_suggestions", policy_mode=None, policy_decision_ref=""):
    po, _ = _modules()
    stage_policy = po.ProviderStagePolicy(
        stage=stage,
        mode=mode,
        candidates=tuple(candidates or ()),
        policy_decision_ref=policy_decision_ref,
    )
    policy = po.ProviderRoutingPolicy(
        mode=policy_mode or mode,
        stage_policies=(stage_policy,),
        schema_version="1",
        policy_version="policy-1",
        alias_mapping=po.DEFAULT_ROUTING_ALIAS_MAP,
    )
    return po.build_provider_multi_backend_routing_preview(
        policy=policy,
        stage=stage,
        output_class=output_class,
        routing_key=_key(stage),
        profiles=tuple(profiles or ()),
        surfaces=tuple(surfaces or ()),
        proofs=tuple(proofs or ()),
        evaluated_at=NOW,
    )


# accepted: 7
def test_accepted_disabled_mode_selects_deterministic_local() -> None:
    result = _preview(mode="disabled")
    assert result["routingDecision"]["selectedIdentity"] == "deterministic_local"
    assert result["executionStatus"] == "deferred_not_implemented"


def test_accepted_single_primary_selects_one_exact_proven_candidate() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    result = _preview(
        profiles=[_profile(po)], surfaces=[_surface(po)], proofs=[record],
        candidates=[_candidate(po, proof_id=record.proof_id)],
    )
    assert result["routingDecision"]["selectedProviderId"] == "provider-a"
    assert result["routingDecision"]["selectedSurfaceId"] == "provider-a-surface"


def test_accepted_priority_fallback_preserves_deterministic_order() -> None:
    po, proof = _modules()
    records = [_proof(proof, po, pid) for pid in ("provider-b", "provider-a")]
    result = _preview(
        mode="priority_fallback",
        profiles=[_profile(po, pid) for pid in ("provider-b", "provider-a")],
        surfaces=[_surface(po, pid) for pid in ("provider-b", "provider-a")],
        proofs=records,
        candidates=[
            _candidate(po, "provider-b", proof_id=records[0].proof_id, priority=2),
            _candidate(po, "provider-a", proof_id=records[1].proof_id, priority=1),
        ],
    )
    assert result["routingDecision"]["selectedProviderId"] == "provider-a"


def test_accepted_stage_specific_policies_are_independent() -> None:
    po, proof = _modules()
    scout = _proof(proof, po)
    report = _proof(
        proof, po, stage=po.ProviderStage.REPORT_DRAFT_ASSIST.value,
        output_class=po.ProviderOutputClass.REPORT_DRAFT.value,
    )
    policies = (
        po.ProviderStagePolicy(stage="scout_assist", mode="single_primary", candidates=(_candidate(po, proof_id=scout.proof_id),)),
        po.ProviderStagePolicy(
            stage="report_draft_assist", mode="disabled",
            candidates=(_candidate(po, proof_id=report.proof_id, capability=po.ProviderCapability.ASSIST_REPORT_DRAFT),),
        ),
    )
    policy = po.ProviderRoutingPolicy(
        mode="stage_specific", stage_policies=policies, schema_version="1",
        policy_version="policy-1", alias_mapping=po.DEFAULT_ROUTING_ALIAS_MAP,
    )
    result = po.build_provider_multi_backend_routing_preview(
        policy=policy, stage="scout_assist", output_class="scout_suggestions",
        routing_key=_key(), profiles=(_profile(po),), surfaces=(_surface(po),),
        proofs=(scout, report), evaluated_at=NOW,
    )
    assert result["routingDecision"]["selectedProviderId"] == "provider-a"
    assert result["routingDecision"]["effectiveMode"] == "single_primary"


def test_accepted_configuration_preview_includes_blocked_candidate_and_reasons() -> None:
    po, _ = _modules()
    result = _preview(
        profiles=[_profile(po)], surfaces=[_surface(po)],
        candidates=[_candidate(po, proof_id="missing-proof")],
    )
    assert len(result["configurationPreview"]) == 1
    assert "proof_missing" in result["configurationPreview"][0]["reasons"]


def test_accepted_eligible_preview_filters_ineligible_candidates() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    result = _preview(
        profiles=[_profile(po), _profile(po, "provider-b")],
        surfaces=[_surface(po), _surface(po, "provider-b")], proofs=[record],
        candidates=[
            _candidate(po, proof_id=record.proof_id),
            _candidate(po, "provider-b", proof_id="missing"),
        ],
    )
    assert [row["providerId"] for row in result["eligiblePreview"]] == ["provider-a"]


def test_accepted_legacy_aliases_map_deterministically_with_warnings() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    result = _preview(
        mode="weighted_round_robin", profiles=[_profile(po)], surfaces=[_surface(po)],
        proofs=[record], candidates=[_candidate(po, proof_id=record.proof_id)],
    )
    decision = result["routingDecision"]
    assert decision["requestedMode"] == "weighted_round_robin"
    assert decision["effectiveMode"] == "deterministic_weighted"
    assert "legacy_weighted_round_robin_alias" in decision["warnings"]


# rejected: 8
def test_rejected_configured_provider_without_proof() -> None:
    po, _ = _modules()
    result = _preview(
        profiles=[_profile(po)], surfaces=[_surface(po)],
        candidates=[_candidate(po, proof_id="")],
    )
    assert result["routingDecision"]["selectedIdentity"] == "deterministic_local"
    assert "proof_missing" in result["configurationPreview"][0]["reasons"]


def test_rejected_api_key_presence_cannot_fabricate_proof(monkeypatch) -> None:
    po, _ = _modules()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")
    result = _preview(
        profiles=[_profile(po)], surfaces=[_surface(po)],
        candidates=[_candidate(po, proof_id="")],
    )
    assert result["eligiblePreview"] == []


def test_rejected_smoke_or_tiny_proof_for_output() -> None:
    po, proof = _modules()
    for level in (proof.ProviderProofLevel.PROVIDER_SMOKE_ONLY, proof.ProviderProofLevel.LIVE_VERIFIED_TINY):
        record = _proof(proof, po, level=level)
        result = _preview(
            profiles=[_profile(po)], surfaces=[_surface(po)], proofs=[record],
            candidates=[_candidate(po, proof_id=record.proof_id)],
        )
        assert result["eligiblePreview"] == []
        assert "proof_unaccepted" in result["configurationPreview"][0]["reasons"]


def test_rejected_unknown_provider_and_surface() -> None:
    po, _ = _modules()
    unknown_provider = _preview(candidates=[_candidate(po, "ghost", proof_id="proof")])
    assert "provider_unknown" in unknown_provider["configurationPreview"][0]["reasons"]
    unknown_surface = _preview(
        profiles=[_profile(po)], candidates=[_candidate(po, proof_id="proof")],
    )
    assert "surface_unknown" in unknown_surface["configurationPreview"][0]["reasons"]


def test_rejected_unknown_stage_and_output_class() -> None:
    stage = _preview(stage="not-a-stage", output_class="scout_suggestions")
    output = _preview(stage="scout_assist", output_class="research_output")
    assert "stage_unknown" in stage["routingDecision"]["reasons"]
    assert "output_class_unknown" in output["routingDecision"]["reasons"]


def test_rejected_unsupported_capability() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    profile = _profile(po, capability=po.ProviderCapability.ASSIST_TRIAGE)
    result = _preview(
        profiles=[profile], surfaces=[_surface(po)], proofs=[record],
        candidates=[_candidate(po, proof_id=record.proof_id)],
    )
    assert "stage_capability_missing" in result["configurationPreview"][0]["reasons"]


def test_rejected_policy_sensitive_without_explicit_decision() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    result = _preview(
        profiles=[_profile(po, policy_status="policy_sensitive")], surfaces=[_surface(po)],
        proofs=[record], candidates=[_candidate(po, proof_id=record.proof_id)],
    )
    assert "policy_sensitive" in result["configurationPreview"][0]["reasons"]


def test_rejected_unsupported_provider_never_becomes_fallback() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    result = _preview(
        mode="priority_fallback", profiles=[_profile(po, policy_status="unsupported")],
        surfaces=[_surface(po)], proofs=[record],
        candidates=[_candidate(po, proof_id=record.proof_id, priority=1)],
    )
    assert result["routingDecision"]["selectedIdentity"] == "deterministic_local"
    assert "provider_unsupported" in result["configurationPreview"][0]["reasons"]


# support/gate: 5
def test_support_exact_stage_capability_and_output_registries() -> None:
    po, _ = _modules()
    assert len(po.ProviderStage) == len(po.ProviderCapability) == len(po.ProviderOutputClass) == 6
    assert tuple(member.value for member in po.ProviderStage) == po.PROVIDER_STAGES


def test_support_output_aliases_are_exact_and_broad_target_rejected() -> None:
    po, proof = _modules()
    assert po.resolve_provider_output_class("paper_review") is po.ProviderOutputClass.FULL_TEXT_REVIEW
    assert po.resolve_provider_output_class("weekly_report") is po.ProviderOutputClass.REPORT_DRAFT
    assert po.resolve_provider_output_class("evidence-extraction") is po.ProviderOutputClass.EVIDENCE_SPANS
    assert po.resolve_provider_output_class("recommendation") is po.ProviderOutputClass.RECOMMENDATION_SUGGESTIONS
    assert po.resolve_provider_output_class("research_output") is None
    proof_source = inspect.getsource(proof)
    assert "provider_orchestration" not in proof_source
    for forbidden in ("import os", "pathlib", "subprocess", "socket", "datetime.now", "getenv"):
        assert forbidden not in proof_source


def test_support_routing_decision_grants_no_execution_or_acceptance() -> None:
    po, proof = _modules()
    record = _proof(proof, po)
    decision = _preview(
        profiles=[_profile(po)], surfaces=[_surface(po)], proofs=[record],
        candidates=[_candidate(po, proof_id=record.proof_id)],
    )["routingDecision"]
    assert decision["executionAllowed"] is False
    assert decision["outputAcceptanceRequired"] is True
    assert decision["persistenceEligible"] is False


def test_support_preview_is_redacted_and_secret_like_key_is_rejected() -> None:
    po, _ = _modules()
    with pytest.raises(ValueError, match="routing_key_value_rejected"):
        po.compute_provider_routing_key_sha256({**_key(), "operatorStableKey": "Bearer hidden"})
    preview = _preview(candidates=[_candidate(po, "password=hidden", proof_id="proof-file.json")])
    serialized = json.dumps(preview)
    assert "password=hidden" not in serialized
    assert "proof-file.json" not in serialized


def test_support_compatibility_facades_and_legacy_factory_are_non_authoritative(monkeypatch) -> None:
    po, _ = _modules()
    from app.services import providers

    monkeypatch.setenv("XRML_PROVIDER", "openai")
    monkeypatch.setattr(providers, "OpenAIProvider", lambda *args, **kwargs: pytest.fail("provider constructed"))
    gate = po.evaluate_provider_output_gate(
        provider_id="legacy", output_class="research_output", current_proof_level="live_verified",
        proof_present=True, accepted=True,
    )
    assert gate.allowed is True
    assert providers.get_active_provider() is None

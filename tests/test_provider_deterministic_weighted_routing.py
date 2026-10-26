"""Exact deterministic weighted-routing algorithm tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect

import pytest


NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
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


def _key():
    return {
        "policyVersion": "policy-1", "runId": "run-1", "stage": "scout_assist",
        "topicId": "topic-α", "artifactId": "artifact-1", "operatorStableKey": "operator-1",
    }


def _objects(provider_ids=("provider-a", "provider-b"), weights=(1, 2), priorities=(1, 2)):
    po, proof = _modules()
    profiles = []
    surfaces = []
    records = []
    candidates = []
    for provider_id, weight, priority in zip(provider_ids, weights, priorities, strict=True):
        profiles.append(po.ProviderBackendProfile(provider_id, "family", provider_id, True, (f"{provider_id}-surface",), frozenset({po.ProviderCapability.ASSIST_SCOUT}), "allowed", ()))
        surfaces.append(po.ProviderExecutionSurface(f"{provider_id}-surface", provider_id, "cli", "adapter", "1", "external", HASH_B, "configured"))
        identity = proof.ProviderProofIdentity("1", "policy-1", provider_id, f"{provider_id}-surface", "scout_assist", "scout_suggestions", HASH_A, "model", HASH_B, "adapter", "1")
        record = proof.ProviderProofRecord(proof.compute_provider_proof_id(identity), identity, proof.ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, HASH_A, NOW - timedelta(hours=1), NOW + timedelta(days=1), None, (), True)
        records.append(record)
        candidates.append(po.ProviderRoutingCandidate(provider_id, f"{provider_id}-surface", True, po.ProviderCapability.ASSIST_SCOUT, record.proof_id, weight, priority))
    return po, tuple(profiles), tuple(surfaces), tuple(records), tuple(candidates)


def _preview(candidates=None, *, mode="deterministic_weighted", key=None):
    po, profiles, surfaces, records, defaults = _objects()
    policy = po.ProviderRoutingPolicy(mode=mode, stage_policies=(po.ProviderStagePolicy("scout_assist", mode, tuple(candidates or defaults)),), schema_version="1", policy_version="policy-1", alias_mapping=po.DEFAULT_ROUTING_ALIAS_MAP)
    return po.build_provider_multi_backend_routing_preview(policy=policy, stage="scout_assist", output_class="scout_suggestions", routing_key=key or _key(), profiles=profiles, surfaces=surfaces, proofs=records, evaluated_at=NOW)


# accepted: 4
def test_accepted_canonical_json_serialization_is_exact() -> None:
    po, *_ = _objects()
    assert po.canonicalize_provider_routing_key(_key()) == '{"artifactId":"artifact-1","operatorStableKey":"operator-1","policyVersion":"policy-1","runId":"run-1","stage":"scout_assist","topicId":"topic-α"}'


def test_accepted_golden_routing_digest_and_bucket_vector() -> None:
    po, *_ = _objects()
    digest = po.compute_provider_routing_key_sha256(_key())
    assert digest == "62f42bd64910d3a0d1c0272d780996c697bbb786fa377675d358c959a09cb05d"
    assert po.routing_bucket(bytes.fromhex(digest), 3_000_000) == 1_173_024


def test_accepted_selection_is_deterministic_and_input_order_independent() -> None:
    po, _, _, _, candidates = _objects()
    first = _preview(candidates)["routingDecision"]
    second = _preview(tuple(reversed(candidates)))["routingDecision"]
    assert first["selectedIdentity"] == second["selectedIdentity"]
    assert first["decisionId"] == second["decisionId"]


def test_accepted_interval_boundaries_and_exact_weight_scaling() -> None:
    po, *_ = _objects()
    weighted = (("provider-a", "a", 1_000_000), ("provider-b", "b", 2_000_000))
    assert po.select_scaled_weighted_candidate(weighted, 0)[:2] == ("provider-a", "a")
    assert po.select_scaled_weighted_candidate(weighted, 999_999)[:2] == ("provider-a", "a")
    assert po.select_scaled_weighted_candidate(weighted, 1_000_000)[:2] == ("provider-b", "b")
    assert po.select_scaled_weighted_candidate(weighted, 2_999_999)[:2] == ("provider-b", "b")
    assert po.scale_provider_weight(Decimal("0.000001")) == 1


# rejected: 8
def test_rejected_invalid_weight_families() -> None:
    po, *_ = _objects()
    invalid = (True, -1, float("nan"), float("inf"), 1_000_001, Decimal("0.0000001"), "not-a-number")
    for value in invalid:
        with pytest.raises(ValueError, match="weight_invalid"):
            po.scale_provider_weight(value)


def test_rejected_all_zero_eligible_weights_fall_back_local() -> None:
    po, _, _, _, candidates = _objects(weights=(0, 0))
    decision = _preview(candidates)["routingDecision"]
    assert decision["selectedIdentity"] == "deterministic_local"
    assert "all_eligible_weights_zero" in decision["reasons"]


def test_rejected_duplicate_provider_surface_identity() -> None:
    po, _, _, _, candidates = _objects()
    duplicate = (candidates[0], candidates[0])
    result = _preview(duplicate)
    assert "duplicate_candidate" in result["configurationPreview"][0]["reasons"]
    assert result["eligiblePreview"] == []


def test_rejected_invalid_priorities() -> None:
    po, *_ = _objects()
    for value in (True, -1, 1.5):
        with pytest.raises(ValueError, match="priority_invalid"):
            po.validate_provider_priority(value)
    po, profiles, surfaces, records, candidates = _objects(priorities=(1, 1))
    policy = po.ProviderRoutingPolicy(mode="priority_fallback", stage_policies=(po.ProviderStagePolicy("scout_assist", "priority_fallback", candidates),), schema_version="1", policy_version="policy-1", alias_mapping=po.DEFAULT_ROUTING_ALIAS_MAP)
    result = po.build_provider_multi_backend_routing_preview(policy=policy, stage="scout_assist", output_class="scout_suggestions", routing_key=_key(), profiles=profiles, surfaces=surfaces, proofs=records, evaluated_at=NOW)
    assert result["eligiblePreview"] == []
    assert result["routingDecision"]["reasons"] == ["priority_invalid"]


def test_rejected_incomplete_or_secret_like_routing_key() -> None:
    po, *_ = _objects()
    with pytest.raises(ValueError, match="routing_key_fields_missing"):
        po.canonicalize_provider_routing_key({"policyVersion": "policy-1"})
    with pytest.raises(ValueError, match="routing_key_value_rejected"):
        po.canonicalize_provider_routing_key({**_key(), "artifactId": "password=hidden"})


def test_rejected_no_positive_proven_candidate_uses_local_fallback() -> None:
    po, profiles, surfaces, _, candidates = _objects()
    policy = po.ProviderRoutingPolicy(mode="deterministic_weighted", stage_policies=(po.ProviderStagePolicy("scout_assist", "deterministic_weighted", candidates),), schema_version="1", policy_version="policy-1", alias_mapping=po.DEFAULT_ROUTING_ALIAS_MAP)
    result = po.build_provider_multi_backend_routing_preview(policy=policy, stage="scout_assist", output_class="scout_suggestions", routing_key=_key(), profiles=profiles, surfaces=surfaces, proofs=(), evaluated_at=NOW)
    assert result["routingDecision"]["selectedIdentity"] == "deterministic_local"
    assert "no_eligible_candidate" in result["routingDecision"]["reasons"]


def test_rejected_selector_has_no_hash_random_or_implicit_time_dependency() -> None:
    po, *_ = _objects()
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            po.canonicalize_provider_routing_key,
            po.compute_provider_routing_key_sha256,
            po.routing_bucket,
            po.select_scaled_weighted_candidate,
            po._candidate_eligibility,
            po._decision_id,
            po.build_provider_multi_backend_routing_preview,
        )
    )
    for forbidden in ("hash(", "random.", "secrets.", "time.", "datetime.now", "os.environ", "getenv"):
        assert forbidden not in source
    assert _preview()["routingDecision"] == _preview()["routingDecision"]


def test_rejected_weighted_failure_never_redraws_or_tries_unproved_fallback() -> None:
    decision = _preview()["routingDecision"]
    assert decision["executionAllowed"] is False
    assert decision["reasons"] == []
    assert decision["selectedIdentity"] != "deterministic_local"


# support/gate: 2
def test_support_priority_secondary_order_is_provider_then_surface() -> None:
    po, profiles, surfaces, records, candidates = _objects(priorities=(1, 2))
    policy = po.ProviderRoutingPolicy(mode="priority_fallback", stage_policies=(po.ProviderStagePolicy("scout_assist", "priority_fallback", tuple(reversed(candidates))),), schema_version="1", policy_version="policy-1", alias_mapping=po.DEFAULT_ROUTING_ALIAS_MAP)
    result = po.build_provider_multi_backend_routing_preview(policy=policy, stage="scout_assist", output_class="scout_suggestions", routing_key=_key(), profiles=profiles, surfaces=surfaces, proofs=records, evaluated_at=NOW)
    assert result["routingDecision"]["selectedProviderId"] == "provider-a"


def test_support_legacy_weighted_alias_is_explicit() -> None:
    decision = _preview(mode="weighted_round_robin")["routingDecision"]
    assert decision["requestedMode"] == "weighted_round_robin"
    assert decision["effectiveMode"] == "deterministic_weighted"
    assert decision["warnings"] == ["legacy_weighted_round_robin_alias"]

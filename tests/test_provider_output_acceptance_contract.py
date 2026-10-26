"""Pure provider-output acceptance contract tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect

import pytest


NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


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
    from app.services import provider_output_acceptance as acceptance
    from app.services import provider_output_class_proof as proof

    return po, proof, acceptance


def _bundle():
    po, proof, acceptance = _modules()
    identity = proof.ProviderProofIdentity("1", "policy-1", "provider-a", "surface-a", "full_text_review_assist", "full_text_review", HASH_A, "model-a", HASH_B, "adapter-a", "1")
    record = proof.ProviderProofRecord(proof.compute_provider_proof_id(identity), identity, proof.ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, HASH_C, NOW - timedelta(hours=1), NOW + timedelta(days=1), None, (), True)
    source = po.ProviderSourceIdentity("paper-1", HASH_A, "paper")
    decision = po.ProviderRoutingDecision(
        provider_id="provider-a", stage="full_text_review_assist", requested_capability="assist_full_text_review",
        output_class="full_text_review", routing_mode="single_primary", allowed=False, blocked_reason=None,
        required_proof_level="exact_output_class_verified", current_proof_level="exact_output_class_verified",
        deterministic_fallback=True, fallback_provider=None, warnings=(), redacted_command_preview=None,
        proof_artifact_refs=(), decision_id="decision-1", routing_key_sha256=HASH_C, policy_version="policy-1",
        requested_mode="single_primary", effective_mode="single_primary", eligibility=(), total_scaled_weight=1_000_000,
        bucket=None, selected_provider_id="provider-a", selected_surface_id="surface-a", selected_proof_id=record.proof_id,
        reasons=(), execution_allowed=False, output_acceptance_required=True, persistence_eligible=False,
    )
    request = po.ProviderExecutionRequest(
        request_id="request-1", routing_decision_id="decision-1", provider_id="provider-a", surface_id="surface-a",
        stage="full_text_review_assist", output_class="full_text_review", proof_id=record.proof_id,
        adapter_id="adapter-a", adapter_version="1", model="model-a", command_shape_sha256=HASH_B,
        artifact_sha256=HASH_A, source_identities=(source,), request_schema_id="request-v1",
        result_schema_id="result-v1", timeout_seconds=30,
    )
    result = po.ProviderExecutionResultEnvelope(
        execution_id="execution-1", request_id="request-1", routing_decision_id="decision-1", proof_id=record.proof_id,
        provider_id="provider-a", surface_id="surface-a", stage="full_text_review_assist", output_class="full_text_review",
        status="succeeded", adapter_id="adapter-a", adapter_version="1", model="model-a", command_shape_sha256=HASH_B,
        payload_sha256=HASH_C, schema_id="result-v1", source_identities=(source,), evidence_ids=("evidence-1",),
        citation_ids=("citation-1",), timed_out=False, truncated=False, redaction_applied=True, side_effect_detected=False,
    )
    callbacks = {
        "payload_hash_validator": lambda digest: digest == HASH_C,
        "result_schema_validator": lambda envelope: envelope.schema_id == "result-v1",
        "source_catalog_validator": lambda sources: sources == (source,),
        "evidence_citation_validator": lambda evidence, citations: (bool(evidence), bool(citations)),
        "domain_validator": lambda envelope: (True, True),
        "redaction_side_effect_validator": lambda envelope: (envelope.redaction_applied, not envelope.side_effect_detected),
    }
    return po, proof, acceptance, decision, request, result, record, callbacks


def _evaluate(*, decision=None, request=None, result=None, proof_record=None, callbacks=None):
    _, _, acceptance, d, q, r, p, cb = _bundle()
    return acceptance.evaluate_provider_output_acceptance(
        routing_decision=decision or d, execution_request=request or q, result_envelope=result or r,
        proof_record=p if proof_record is None else proof_record, evaluated_at=NOW, **(callbacks or cb),
    )


# accepted: 1
def test_accepted_complete_synthetic_contract_is_persistence_eligible() -> None:
    decision = _evaluate()
    assert decision.accepted is True
    assert decision.persistence_eligible is True
    assert decision.reasons == ()
    assert len(decision.completed_validation_steps) == 10


# rejected: 16
def test_rejected_wrong_provider() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    decision = _evaluate(result=replace(result, provider_id="provider-b"))
    assert decision.reasons == ("provider_mismatch",)


def test_rejected_wrong_surface() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, surface_id="surface-b")).reasons == ("surface_mismatch",)


def test_rejected_wrong_stage_or_output_class() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, stage="triage_assist")).reasons == ("stage_mismatch",)
    assert _evaluate(result=replace(result, output_class="report_draft")).reasons == ("output_class_mismatch",)


def test_rejected_stale_expired_or_revoked_proof() -> None:
    _, _, _, _, _, _, record, _ = _bundle()
    stale_identity = replace(record.identity, policy_version="policy-old")
    stale = replace(record, proof_id=_modules()[1].compute_provider_proof_id(stale_identity), identity=stale_identity)
    assert "proof_stale" in _evaluate(proof_record=stale).reasons
    assert _evaluate(proof_record=replace(record, expires_at=NOW)).reasons == ("proof_expired",)
    assert _evaluate(proof_record=replace(record, revoked_at=NOW - timedelta(minutes=1))).reasons == ("proof_revoked",)


def test_rejected_wrong_model() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, model="model-b")).reasons == ("model_mismatch",)


def test_rejected_wrong_command_shape() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, command_shape_sha256=HASH_A)).reasons == ("command_shape_mismatch",)


def test_rejected_missing_or_mismatched_source_identity_and_hash() -> None:
    po, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, source_identities=())).reasons == ("source_identity_missing",)
    other = po.ProviderSourceIdentity("paper-2", HASH_A, "paper")
    assert _evaluate(result=replace(result, source_identities=(other,))).reasons == ("source_identity_mismatch",)
    wrong_hash = po.ProviderSourceIdentity("paper-1", HASH_B, "paper")
    assert _evaluate(result=replace(result, source_identities=(wrong_hash,))).reasons == ("source_hash_mismatch",)


def test_rejected_payload_hash_or_schema_failure() -> None:
    _, _, _, _, _, _, _, callbacks = _bundle()
    bad_hash = {**callbacks, "payload_hash_validator": lambda digest: False}
    assert _evaluate(callbacks=bad_hash).reasons == ("payload_hash_mismatch",)
    bad_schema = {**callbacks, "result_schema_validator": lambda envelope: False}
    assert _evaluate(callbacks=bad_schema).reasons == ("result_schema_invalid",)


def test_rejected_missing_evidence_or_citation_identity() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, evidence_ids=())).reasons == ("evidence_unresolved",)
    assert _evaluate(result=replace(result, citation_ids=())).reasons == ("citation_unresolved",)


def test_rejected_unsupported_claims_or_domain_failure() -> None:
    _, _, _, _, _, _, _, callbacks = _bundle()
    unsupported = {**callbacks, "domain_validator": lambda envelope: (False, True)}
    assert _evaluate(callbacks=unsupported).reasons == ("unsupported_claims",)
    invalid = {**callbacks, "domain_validator": lambda envelope: (True, False)}
    assert _evaluate(callbacks=invalid).reasons == ("domain_validation_failed",)


def test_rejected_failure_timeout_or_truncation() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, status="failed")).reasons == ("result_unsuccessful",)
    assert _evaluate(result=replace(result, timed_out=True)).reasons == ("result_timeout",)
    assert _evaluate(result=replace(result, truncated=True)).reasons == ("result_truncated",)


def test_rejected_redaction_or_side_effect_policy() -> None:
    _, _, _, _, _, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, redaction_applied=False)).reasons == ("redaction_policy_failed",)
    assert _evaluate(result=replace(result, side_effect_detected=True)).reasons == ("side_effect_detected",)


def test_rejected_file_environment_or_cli_cannot_bypass_proof(monkeypatch) -> None:
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "exact_output_class_verified")
    monkeypatch.setenv("PROOF_FILE", "proof.json")
    decision = _evaluate(proof_record=False)
    assert decision.reasons == ("proof_missing",)


def test_rejected_direct_persistence_attempt_is_a_side_effect(monkeypatch) -> None:
    _, _, acceptance, _, _, result, _, _ = _bundle()
    source = inspect.getsource(acceptance)
    for forbidden in ("app.repositories", "app.services.runs", "Path(", ".write_text(", "open("):
        assert forbidden not in source
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: pytest.fail("filesystem write attempted"))
    assert _evaluate(result=replace(result, side_effect_detected=True)).persistence_eligible is False


def test_rejected_request_or_routing_identity_mismatch() -> None:
    _, _, _, _, request, result, _, _ = _bundle()
    assert _evaluate(result=replace(result, request_id="request-2")).reasons == ("request_identity_mismatch",)
    assert _evaluate(request=replace(request, routing_decision_id="decision-2")).reasons == ("routing_decision_mismatch",)


def test_rejected_unaccepted_or_scope_mismatched_proof() -> None:
    _, _, _, _, _, _, record, _ = _bundle()
    assert _evaluate(proof_record=replace(record, accepted=False)).reasons == ("proof_unaccepted",)
    identity = replace(record.identity, output_class="report_draft")
    scoped = replace(record, proof_id=_modules()[1].compute_provider_proof_id(identity), identity=identity)
    assert "proof_scope_mismatch" in _evaluate(proof_record=scoped).reasons


# support/gate: 1
def test_support_result_envelope_cannot_self_authorize_and_acceptance_is_sole_authority() -> None:
    po, _, acceptance, _, _, result, _, _ = _bundle()
    assert not hasattr(result, "accepted")
    assert not hasattr(result, "output_allowed")
    with pytest.raises(TypeError):
        po.ProviderExecutionResultEnvelope(**{**result.__dict__, "accepted": True})
    assert callable(acceptance.evaluate_provider_output_acceptance)

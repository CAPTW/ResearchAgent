"""Antigravity fake child through the existing durable dispatcher; no live account."""
from dataclasses import replace
from contextlib import closing
from datetime import datetime, timezone
import importlib
from threading import Event

import pytest

from app.db import bootstrap
from app.domain import prepared_review, review_authority, review_execution as domain, review_preparation, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import review_authorities as authorities, review_executions as repository, review_preparations
from app.services.provider_output_class_proof import compute_provider_proof_id
from app.services.review_dispatch import PaperDispatchRuntime
from tests import test_review_authority as proofs, test_review_dispatch as shared

seeded = shared.seeded
runtimes = shared.runtimes


@pytest.fixture
def setup(tmp_path, monkeypatch, runtimes):
    provider_class = importlib.import_module("app.services.providers.antigravity_prepared_review").AntigravityPreparedReviewProvider
    runtime_at = importlib.import_module("tests.test_antigravity_prepared_review").runtime_at
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(repository, "iso_now_utc", lambda: clock[0].isoformat())
    monkeypatch.setattr(authorities, "iso_now_utc", lambda: clock[0].isoformat())
    monkeypatch.setattr(proofs, "NOW", clock[0])
    text = " e\u0301 한글🙂 fixture body "
    source = dict(run_id="source-run", paper_id="paper", source_chars=len(text), source_kind="text",
        artifact_sha256=sha(text), artifact_bytes=len(text.encode()), artifact_ref="run:raw/paper.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.Prepare(request_id="range", run_id="source-run", paper_id="paper",
        expected_source_digest=source["source_digest"], start=0, end=len(text))
    saved = review_preparations.save("topic", "version", "owner", body, [("source-run", "paper")], lambda: (source, text))
    scope = domain.Scope("topic", "version", saved["preparation_id"], "owner", (("source-run", "paper"),))
    frozen = prepared_review.prepare(build_packet(saved), provider_name="antigravity_cli", model="offline-requested-model")
    providers = []

    def make(mode="success", request="execution"):
        root = tmp_path / f"cli-{len(providers)}"
        provider = provider_class(frozen, runtime=runtime_at(root, frozen, mode), timeout_seconds=30)
        providers.append(provider)
        spec = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(saved)),
            provider_name="antigravity_cli", model=frozen["model"])
        ready = repository.prepare_transport(scope, spec, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
        calls = []

        def issuer(contract, command):
            calls.append(command)
            grant = proofs.grant(contract, authorization_id=contract["execution_id"])
            identity = replace(grant.proof.identity, surface_id="antigravity_cli")
            proof = replace(grant.proof, identity=identity, proof_id=compute_provider_proof_id(identity))
            return review_authority.Record(grant=grant.model_copy(update={"proof": proof}),
                issuer_id="fabricated-test-issuer", decision_ref="local-synthetic-only", registered_at=clock[0],
                expected_sequence=command.expected_sequence).model_dump(mode="json")

        runtime = PaperDispatchRuntime(resolver=issuer, provider=provider)
        runtimes.append(runtime)
        return ready, provider, runtime, calls, root, spec, issuer

    return scope, make, clock


def test_concurrent_dispatch_receipt_replay_and_fresh_process_read(setup, tmp_path):
    shared.test_concurrent_dispatch_exact_receipt_lost_response_and_fresh_process_read(setup, tmp_path)


def test_offline_api_actor_and_topic_cannot_authorize_antigravity(setup):
    shared.test_synthetic_api_boundary_cannot_read_approve_or_claim_transport(setup)


@pytest.mark.parametrize("phase", ["after_authority", "after_revocation", "after_receipt"])
def test_atomic_authority_revocation_and_receipt_retry(setup, monkeypatch, phase):
    shared.test_atomic_authority_revocation_and_receipt_retry(setup, monkeypatch, phase)


@pytest.mark.parametrize("stage", ["before_claim", "before_start", "after_start"])
def test_expiry_restart_and_recovery_do_not_relaunch(setup, stage):
    shared.test_expiry_recovery_and_restart_do_not_relaunch(setup, stage)


@pytest.mark.parametrize("mode,state,unknown,reason", [
    ("failed", "FAILED", False, "provider_failed"), ("citation", "FAILED", False, "output_invalid"),
    ("missing", "INTERRUPTED", True, "adapter_failed")])
def test_terminal_failure_unknown_outcome_and_no_automatic_retry(setup, mode, state, unknown, reason):
    shared.test_known_failure_unknown_outcome_and_no_automatic_retry(setup, mode, state, unknown, reason)


@pytest.mark.parametrize("surface", ["codex_cli", "claude_code", "api", "offline"])
def test_rehashed_wrong_surface_proof_refuses_before_launch(setup, surface):
    scope, make, clock = setup
    ready, _, _, _, root, _, issuer = make()
    command = domain.Control(request_id="approve", expected_sequence=0, action="approve")
    record = review_authority.read_record(issuer(ready["contract"], command), ready["contract"])
    identity = replace(record.grant.proof.identity, surface_id=surface)
    proof = replace(record.grant.proof, identity=identity, proof_id=compute_provider_proof_id(identity))
    with pytest.raises(ValueError, match="authority_proof_rejected"):
        review_authority.validate_grant(record.grant.model_copy(update={"proof": proof}), ready["contract"], clock[0])
    assert shared.read(scope, ready) == ready
    assert not (root / "launches.log").exists()


@pytest.mark.parametrize("action", ["cancel", "revoke", "stop"])
def test_completed_child_late_delivery_is_fenced_without_db_lock(setup, monkeypatch, action):
    scope, make, _ = setup
    ready, provider, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    produced, release = Event(), Event()
    original = type(provider).generate_record

    def delayed(self, request):
        record = original(self, request)
        produced.set()
        assert release.wait(20)
        return record

    monkeypatch.setattr(type(provider), "generate_record", delayed)
    shared.approve(scope, ready, runtime)
    runtime.dispatch(scope, identity, shared.dispatch_body())
    try:
        assert produced.wait(10)
        with closing(bootstrap.connect_metadata_index()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE competing_writer(value TEXT)")
            connection.execute("INSERT INTO competing_writer VALUES ('writer available')")
        control = domain.Control(request_id=action, expected_sequence=3, action="revoke" if action == "revoke" else "cancel")
        if action == "stop":
            runtime.stop()
        elif action == "revoke":
            authorities.revoke(scope, identity, control, reason="synthetic revocation")
        else:
            repository.control(scope, identity, control, enabled=False, schema=transport.SCHEMA)
        saved = shared.read(scope, ready)
        assert saved["current"]["outcome_unknown"] is True
    finally:
        release.set()
        runtime.wait_idle(30)
    assert shared.read(scope, ready) == saved
    assert saved["current"]["receipt_id"] is None
    assert (root / "launches.log").read_text().splitlines() == ["launch"]

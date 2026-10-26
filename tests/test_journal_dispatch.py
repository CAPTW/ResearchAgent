"""All approvals/proofs and adapters in this suite are synthetic and offline."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import multiprocessing
import os
from pathlib import Path
from threading import Barrier, Event

import pytest

from app.domain import journal_dispatch as domain
from app.domain.temporal_memory import digest
from app.repositories import journal_dispatch as admission, journal_executions as repo
from app.services.journal_dispatch import JournalDispatcher
from app.services.journal_execution import execute
from app.services.provider_output_class_proof import (
    ProviderProofIdentity, ProviderProofLevel, ProviderProofRecord, compute_provider_proof_id,
)
from tests import test_journal_execution as prior

source = prior.source
completed = prior.completed
execution = prior.execution
NOW = prior.NOW
BINDING = domain.AdapterBinding(surface_id="synthetic-surface", adapter_id="synthetic-adapter",
                                adapter_version="1", command_shape_sha256="a" * 64)


def grant(execution, **changes):
    contract = execution["contract"]
    spec = contract["specification"]
    identity = ProviderProofIdentity("1", "synthetic-policy", spec["provider_name"], BINDING.surface_id,
                                     "report_draft_assist", "report_draft", digest(contract["provider_request"]),
                                     spec["model"], BINDING.command_shape_sha256, BINDING.adapter_id, "1")
    proof = ProviderProofRecord(compute_provider_proof_id(identity), identity,
                                ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, "b" * 64,
                                NOW, NOW + timedelta(minutes=5), None, ("synthetic_test_only",), True)
    values = dict(authorization_id="synthetic-approval", actor_id="owner", contract_digest=digest(contract),
                  policy_version="synthetic-policy", granted_at=NOW, expires_at=NOW + timedelta(minutes=2),
                  max_attempts=3, max_reserved_output_tokens=12000, proof=proof)
    return domain.Grant(**(values | changes))


def body(**changes):
    return domain.Dispatch(**(dict(request_id="dispatch", expected_sequence=0) | changes))


def admit(execution, request=None, approval=None, **kwargs):
    return admission.admit(*prior.scope(execution), request or body(), "owner",
                           approval or grant(execution), BINDING, now=NOW, **kwargs)


def controller(execution, fake, approval=None, **kwargs):
    return JournalDispatcher(provider=fake, model="offline-model", binding=BINDING,
                             authority=lambda *_: approval or grant(execution), clock=lambda: NOW,
                             shutdown_grace_seconds=0.01, **kwargs)


def test_default_authority_denies_without_adapter_or_storage_change(execution):
    fake = prior.Fake(lambda _: pytest.fail("No authority"))
    value = JournalDispatcher(provider=fake, model="offline-model", binding=BINDING, clock=lambda: NOW)
    with pytest.raises(ValueError, match="authority_unavailable"):
        value.dispatch(*prior.scope(execution), body(), "owner")
    assert fake.calls == 0 and repo.detail(*prior.scope(execution)) == execution
    assert value.stop()


@pytest.mark.parametrize("change", ["actor", "contract", "model", "schema", "stage", "surface", "expired", "revoked", "unaccepted"])
def test_exact_scope_denial_does_not_consume_an_attempt(execution, change):
    approved = grant(execution)
    if change in {"actor", "contract"}:
        approved = approved.model_copy(update={"actor_id" if change == "actor" else "contract_digest": "wrong"})
    elif change == "expired":
        approved = approved.model_copy(update={"expires_at": NOW})
    elif change in {"revoked", "unaccepted"}:
        approved = approved.model_copy(update={"proof": replace(approved.proof, **(
            {"revoked_at": NOW} if change == "revoked" else {"accepted": False}))})
    else:
        fields = {"model": "model", "schema": "artifact_sha256", "stage": "stage", "surface": "surface_id"}
        identity = replace(approved.proof.identity, **{fields[change]: "c" * 64 if change == "schema" else "other"})
        approved = approved.model_copy(update={"proof": replace(approved.proof, identity=identity,
                                                                proof_id=compute_provider_proof_id(identity))})
    with pytest.raises(ValueError, match="journal_dispatch_"):
        admit(execution, approval=approved)
    assert repo.detail(*prior.scope(execution)) == execution


def test_duplicate_dispatch_is_durable_without_reissuing_claim_or_refunding(execution):
    saved, claimed = admit(execution)
    assert claimed is not None and saved["current"]["state"] == "LEASED"
    receipt = saved["current"]["dispatch"]
    assert receipt["reserved_output_tokens"] == 4000 and receipt["actor_id"] == "owner"
    assert claimed.token not in str(saved)
    assert repo.abandon(claimed, reason="shutdown", now=NOW)
    current = repo.detail(*prior.scope(execution))
    replay, duplicate = admit(execution)
    assert replay == current and duplicate is None
    assert "dispatch" not in current["current"]
    with pytest.raises(ValueError, match="conflict"):
        admit(execution, body(acknowledge_unknown=True))
    with pytest.raises(ValueError, match="conflict"):
        admission.admit(*prior.scope(execution), body(), "other", grant(execution), BINDING, now=NOW)


def test_budget_counts_every_reserved_attempt_and_unknown_requires_ack(execution):
    approved = grant(execution, max_reserved_output_tokens=8000)
    _, first = admit(execution, approval=approved)
    assert repo.start(first, now=NOW)
    assert repo.abandon(first, reason="shutdown", now=NOW)
    with pytest.raises(ValueError, match="authority_required"):
        repo.claim(*prior.scope(execution), expected_attempt=1, acknowledge_unknown=True, now=NOW)
    request = body(request_id="retry", expected_sequence=3)
    with pytest.raises(ValueError, match="unknown_outcome"):
        admit(execution, request, approved)
    _, second = admit(execution, request.model_copy(update={"acknowledge_unknown": True}), approved)
    assert repo.abandon(second, reason="shutdown", now=NOW)
    with pytest.raises(ValueError, match="budget_exhausted"):
        admit(execution, body(request_id="third", expected_sequence=5), approved)
    assert repo.detail(*prior.scope(execution))["current"]["attempt"] == 2


def test_expiry_between_reservation_and_dispatch_is_known_unstarted(execution):
    _, claimed = admit(execution, approval=grant(execution, expires_at=NOW + timedelta(seconds=30)))
    fake = prior.Fake(lambda _: pytest.fail("Expired approval"))
    execute(claimed, provider=fake, model="offline-model", clock=lambda: NOW + timedelta(seconds=31))
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "FAILED" and not current["outcome_unknown"]
    assert current["reason"] == "dispatch_authority_expired"
    assert fake.calls == 0


def test_six_simultaneous_requests_dispatch_only_once_and_replay_after_close(execution):
    entered, release = Event(), Event()
    def generate(_):
        entered.set()
        assert release.wait(10)
        return prior.response(execution)
    fake = prior.Fake(generate)
    controllers = [controller(execution, fake) for _ in range(2)]
    gate = Barrier(6)
    def send(i):
        gate.wait()
        return controllers[i % 2].dispatch(*prior.scope(execution), body(), "owner")
    try:
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(send, range(6)))
        assert entered.wait(5) and fake.calls == 1
        assert all(not row["closed"] for value in controllers for row in value.snapshot()["workers"])
    finally:
        release.set()
        for value in controllers:
            value.stop()
    before = repo.detail(*prior.scope(execution))
    fresh = controller(execution, fake)
    assert fresh.dispatch(*prior.scope(execution), body(), "owner") == before
    assert fresh.snapshot()["workers"] == [] and fake.calls == 1
    assert fresh.stop()


def test_atomic_rollback_does_not_leave_budget_or_lease(execution, monkeypatch):
    def fail(_):
        raise RuntimeError("injected")
    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "checkpoint", fail)
        with pytest.raises(RuntimeError, match="injected"):
            admit(execution)
    assert repo.detail(*prior.scope(execution)) == execution
    assert admit(execution)[1].attempt == 1


def test_legacy_attempts_count_against_capacity_and_cannot_be_hidden(execution):
    old = prior.claim(execution)
    assert repo.abandon(old, reason="shutdown", now=NOW)
    with pytest.raises(ValueError, match="budget_exhausted"):
        admit(execution, body(expected_sequence=2), grant(execution, max_reserved_output_tokens=4000))
    assert repo.detail(*prior.scope(execution))["current"]["attempt"] == 1


def test_success_keeps_original_request_and_can_be_preserved_for_human_review(execution):
    from app.repositories import journal_candidates
    seen = []
    def generate(request):
        from dataclasses import asdict
        seen.append(asdict(request))
        return prior.response(execution)
    value = controller(execution, prior.Fake(generate))
    value.dispatch(*prior.scope(execution), body(), "owner")
    try:
        for worker in value._workers:
            assert worker.wait(5)
        saved = repo.detail(*prior.scope(execution))
        assert saved["current"]["state"] == "SUCCEEDED"
        assert seen == [execution["contract"]["provider_request"]]
        assert saved["contract"] == execution["contract"]
        assert sum("dispatch" in event for event in saved["events"]) == 1
        assert saved["current"]["result"]["validation"]["independent_live_proof"] == "NOT_VERIFIED"
        candidate, created = journal_candidates.save_execution(*prior.scope(saved), digest(saved["current"]["result"]), "reviewer")
        assert created and candidate["origin"] == "RECORDED_ADAPTER_RESULT"
        assert candidate["execution"]["contract"] == saved["contract"]
        assert candidate["validation"]["independent_live_proof"] == "NOT_VERIFIED"
        assert journal_candidates.detail(*prior.scope(saved)[:2], candidate["candidate_id"]) == candidate
    finally:
        assert value.stop()


def test_stale_or_foreign_request_never_reserves_capacity(execution):
    with pytest.raises(KeyError):
        admission.admit("foreign", *prior.scope(execution)[1:], body(), "owner", grant(execution), BINDING, now=NOW)
    with pytest.raises(ValueError, match="conflict"):
        admit(execution, body(expected_sequence=1))
    assert repo.detail(*prior.scope(execution)) == execution


def test_stop_during_authority_resolution_leaves_no_running_worker(execution):
    entered, release = Event(), Event()
    def resolve(*_):
        entered.set()
        assert release.wait(10)
        return grant(execution)
    fake = prior.Fake(lambda _: pytest.fail("Closed dispatcher"))
    value = JournalDispatcher(provider=fake, model="offline-model", binding=BINDING, authority=resolve, clock=lambda: NOW)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(value.dispatch, *prior.scope(execution), body(), "owner")
        try:
            assert entered.wait(5)
            assert value.stop()
        finally:
            release.set()
        with pytest.raises(ValueError, match="closed"):
            pending.result(timeout=10)
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "READY" and not current["outcome_unknown"]
    assert fake.calls == 0 and value.snapshot() == dict(closed=True, workers=[])


@pytest.mark.parametrize("field", ["expires_at", "revoked_at"])
def test_proof_is_rechecked_at_dispatch_not_only_when_capacity_is_reserved(execution, field):
    approved = grant(execution)
    approved = approved.model_copy(update={"proof": replace(approved.proof, **{field: NOW + timedelta(seconds=20)})})
    _, claimed = admit(execution, approval=approved)
    fake = prior.Fake(lambda _: pytest.fail("No longer eligible proof"))
    execute(claimed, provider=fake, model="offline-model", clock=lambda: NOW + timedelta(seconds=21))
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["state"] == "FAILED" and current["reason"] == "dispatch_proof_invalidated"
    assert not current["outcome_unknown"] and fake.calls == 0


def test_dispatcher_stop_retains_draining_worker_and_fences_completion(execution):
    entered, release = Event(), Event()
    def generate(_):
        entered.set()
        assert release.wait(10)
        return prior.response(execution)
    value = controller(execution, prior.Fake(generate))
    try:
        value.dispatch(*prior.scope(execution), body(), "owner")
        assert entered.wait(5)
        assert not value.stop()
        assert value.snapshot()["workers"][0]["running"]
        before = repo.detail(*prior.scope(execution))
        assert before["current"]["state"] == "INTERRUPTED"
        with pytest.raises(ValueError, match="closed"):
            value.dispatch(*prior.scope(execution), body(request_id="retry", expected_sequence=3, acknowledge_unknown=True), "owner")
    finally:
        release.set()
        for worker in value._workers:
            assert worker.wait(5)
        assert value.stop()
    assert repo.detail(*prior.scope(execution)) == before


def _process_admit(scope, raw_grant, data_root, barrier, output):
    from app.db import bootstrap
    bootstrap.STORAGE_ROOT = Path(data_root) / "runs"
    os.environ["XRML_DATA_DIR"] = data_root
    barrier.wait(timeout=20)
    saved, claimed = admission.admit(*scope, body(), "owner", domain.Grant.model_validate_json(raw_grant), BINDING, now=NOW)
    output.put((claimed is not None, saved["current"]["attempt"]))


def test_separate_processes_reserve_once_and_preserve_receipt_on_reopen(execution):
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(3), ctx.Queue()
    workers = [ctx.Process(target=_process_admit, args=(prior.scope(execution), grant(execution).model_dump_json(),
                                                       os.environ["XRML_DATA_DIR"], barrier, output)) for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        results = [output.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert results.count((True, 1)) == 1 and results.count((False, 1)) == 2
        saved = repo.detail(*prior.scope(execution))
        assert len(saved["events"]) == 1 and saved["current"]["dispatch"]["grant"] == grant(execution).model_dump(mode="json")
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        output.close()
        output.join_thread()

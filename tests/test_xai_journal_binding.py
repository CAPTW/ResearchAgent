"""xAI Journal persistence and recovery; synthetic authority/MockTransport only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import importlib
import json
from threading import Event

import httpx
import pytest

from app.repositories import journal_executions as repo, journal_dispatch as admission, journal_authority as records
from app.services.journal_execution import execute
from app.services.provider_output_class_proof import compute_provider_proof_id
from tests import test_openai_journal_binding as shared, test_generated_journal as generated
from tests import test_journal_execution_controls as controls

source = shared.source
completed = shared.completed


@pytest.fixture
def execution(completed):
    return shared.prior.prepare(completed, provider_name="xai", model="offline-model")[0]


def adapter(execution, calls=None, **changes):
    def handle(request):
        if calls is not None:
            calls.append(request)
        output = shared.prior.imports.output(execution["contract"]["packet"])
        return httpx.Response(200, json=shared.envelope(json.dumps(output, ensure_ascii=False)))
    module = importlib.import_module("app.services.providers.xai_journal")
    return module.XaiJournalProvider(execution["contract"], **(dict(timeout_seconds=45,
        api_key="synthetic-key", transport=httpx.MockTransport(handle)) | changes))


@pytest.fixture
def wired(monkeypatch):
    module = importlib.import_module("app.services.providers.xai_journal")
    monkeypatch.setattr(shared, "adapter", adapter)
    monkeypatch.setattr(shared, "journal_dispatcher", module.journal_dispatcher)
    return module


@pytest.mark.parametrize("check", [
    shared.test_default_denial_and_mismatched_binding_consume_no_attempt,
    shared.test_stored_authority_six_duplicate_dispatches_send_once_and_keep_receipt,
    shared.test_redirect_is_not_followed_or_retried,
    shared.test_bound_adapter_requires_a_saved_dispatch_receipt,
    shared.test_bound_attempt_replay_uses_its_original_receipt_without_calling_again,
])
def test_shared_journal_contracts_use_actual_xai_adapter(execution, wired, check):
    check(execution)


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed", "cancelled"])
@pytest.mark.parametrize("identity", ["missing", "control", "padded"])
def test_unidentified_outcome_requires_acknowledgement_after_restart(execution, wired, status, identity):
    shared.test_unidentified_terminal_reply_preserves_unknown_across_dispatcher_restart(execution, status, identity)


@pytest.mark.parametrize("mode", ["missing", "changed", "extra"])
def test_receipt_reconstruction_rejects_rehashed_manifest_tampering(execution, wired, mode):
    shared.test_missing_or_tampered_manifest_rejects_even_if_event_digest_is_recomputed(execution, mode)


def test_atomic_admission_and_stored_revocation(execution, wired, monkeypatch):
    shared.test_manifest_and_reservation_roll_back_together_and_revocation_still_fences(execution, monkeypatch)


def test_changed_timeout_requires_new_authority_and_cannot_start_old_claim(execution, wired):
    calls = []
    provider = adapter(execution, calls)
    other = adapter(execution, calls, timeout_seconds=46)
    assert other.binding != provider.binding
    grant = shared.approval(execution, provider)
    controller = wired.journal_dispatcher(other, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        with pytest.raises(ValueError, match="proof_rejected"):
            controller.dispatch(*shared.prior.scope(execution), shared.dispatch.body(), "owner")
    finally:
        assert controller.stop()
    assert repo.detail(*shared.prior.scope(execution)) == execution and calls == []
    _, claimed = admission.admit(*shared.prior.scope(execution), shared.dispatch.body(), "owner", grant,
        provider.binding, now=shared.prior.NOW, adapter_manifest=provider.manifest())
    with pytest.raises(ValueError, match="adapter_mismatch"):
        execute(claimed, provider=other, model="offline-model", clock=lambda: shared.prior.NOW)
    assert repo.detail(*shared.prior.scope(execution))["current"]["state"] == "LEASED" and calls == []
    assert repo.abandon(claimed, reason="shutdown", now=shared.prior.NOW)


@pytest.mark.parametrize("change", ["actor", "topic", "provider", "paper_stage", "grok_surface", "expired"])
def test_foreign_authority_cannot_admit_or_send(execution, wired, change):
    calls = []
    provider = adapter(execution, calls)
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    if change == "actor":
        grant = grant.model_copy(update={"actor_id": "other"})
    elif change == "topic":
        scope = ("foreign-topic", *scope[1:])
    elif change == "expired":
        grant = grant.model_copy(update={"expires_at": shared.prior.NOW})
    else:
        fields = {"provider": {"provider_id": "openai"}, "paper_stage": {"stage": "full_text_review_assist"},
                  "grok_surface": {"surface_id": "grok_cli"}}
        identity = replace(grant.proof.identity, **fields[change])
        grant = grant.model_copy(update={"proof": replace(grant.proof, identity=identity,
            proof_id=compute_provider_proof_id(identity))})
    controller = wired.journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        with pytest.raises((KeyError, ValueError)):
            controller.dispatch(*scope, shared.dispatch.body(), "owner")
    finally:
        assert controller.stop()
    assert calls == [] and repo.detail(*shared.prior.scope(execution)) == execution


@pytest.mark.parametrize("action", ["cancel", "revoke", "shutdown", "expire"])
def test_late_http_result_is_fenced_without_claiming_remote_cancellation(execution, wired, action):
    entered, release = Event(), Event()
    calls = []
    now = [shared.prior.NOW]
    def handle(request):
        calls.append(request)
        entered.set()
        assert release.wait(10)
        return httpx.Response(200, json=shared.envelope(json.dumps(shared.prior.imports.output(execution["contract"]["packet"]))))
    provider = adapter(execution, transport=httpx.MockTransport(handle))
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    records.register(*scope, grant, provider.binding, issuer="synthetic", decision_ref="mock-only", expected_sequence=0, now=now[0])
    controller = wired.journal_dispatcher(provider, authority=shared.StoredJournalAuthority(grant.authorization_id, clock=lambda: now[0]),
        clock=lambda: now[0], shutdown_grace_seconds=.01)
    try:
        controller.dispatch(*scope, shared.dispatch.body(), "owner")
        assert entered.wait(5)
        if action == "cancel":
            controls.command(execution, sequence=2)
        elif action == "revoke":
            records.revoke(*scope, grant.authorization_id, request_id="withdraw", issuer="synthetic", reason="test", now=now[0])
        elif action == "shutdown":
            assert not controller.stop()
        else:
            now[0] += timedelta(minutes=10)
            controls.command(execution, action="recover", sequence=2, now=now[0])
        saved = repo.detail(*scope)
        assert saved["current"]["outcome_unknown"] and saved["current"]["result"] is None
    finally:
        release.set()
        for worker in controller._workers:
            assert worker.wait(5)
        assert controller.stop()
    assert len(calls) == 1 and repo.detail(*scope) == saved


def succeeded(execution, wired):
    calls = []
    provider = adapter(execution, calls)
    _, claim = admission.admit(*shared.prior.scope(execution), shared.dispatch.body(), "owner", shared.approval(execution, provider),
        provider.binding, now=shared.prior.NOW, adapter_manifest=provider.manifest())
    saved = execute(claim, provider=provider, model=provider.model, clock=lambda: shared.prior.NOW)
    assert saved["current"]["state"] == "SUCCEEDED" and len(calls) == 1
    assert saved["current"]["result"]["provider_reported"]["provider_name"] == "xai"
    assert shared.journal_candidates.listing(*shared.prior.scope(execution)[:2])["total"] == 0
    return saved


def test_generated_xai_result_requires_human_acceptance_and_keeps_frozen_evidence(execution, wired, source, monkeypatch):
    generated.test_generated_and_imported_same_text_have_distinct_identity_and_judgments(succeeded(execution, wired), source, monkeypatch)


def test_xai_candidate_rollback_and_review_export_reopen_in_fresh_process(execution, wired, monkeypatch, tmp_path):
    generated.test_rollback_process_death_and_restarted_review_readback(succeeded(execution, wired), monkeypatch, tmp_path)


def test_paper_contract_cannot_substitute_for_journal(execution, wired):
    changed = deepcopy(execution["contract"])
    changed["provider_request"]["schema_name"] = "PREPARED_PAPER_REVIEW_V3"
    with pytest.raises(ValueError, match="request_invalid"):
        wired.XaiJournalProvider(changed, api_key="synthetic", timeout_seconds=45)


@pytest.mark.parametrize("change", ["size", "schema", "instructions"])
def test_changed_noncanonical_request_rejected_before_any_transport(execution, wired, change):
    changed = deepcopy(execution)
    field, value = {"size": ("prompt", "a" * 4_000_000), "schema": ("schema", {}),
                    "instructions": ("instructions", execution["contract"]["provider_request"]["instructions"] + " e\u0301")}[change]
    changed["contract"]["provider_request"][field] = value
    with pytest.raises(ValueError, match="request_invalid"):
        adapter(changed)

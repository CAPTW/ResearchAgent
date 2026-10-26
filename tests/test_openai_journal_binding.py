"""Exact journal wire binding, using synthetic authority and MockTransport only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from threading import Barrier
import time

import httpx
import pytest

from app.domain.journal_dispatch import AdapterBinding
from app.db.bootstrap import connect_metadata_index
from app.domain.temporal_memory import digest, canonical_json
from app.repositories import journal_candidates, journal_executions as repo, journal_authority as records, journal_dispatch as admission
from app.services.journal_execution import execute
from app.services.journal_dispatch import JournalDispatcher, StoredJournalAuthority
from app.services.providers.base import ProviderRequest
from app.services.providers.openai_journal import OpenAIJournalProvider, journal_dispatcher
from app.services.providers.openai_journal_wire import MAX_REQUEST_BYTES
from app.services.provider_output_class_proof import compute_provider_proof_id
from tests import test_journal_execution as prior, test_journal_dispatch as dispatch
from tests.test_openai_response_contract import envelope

source = prior.source
completed = prior.completed


@pytest.fixture
def execution(completed):
    return prior.prepare(completed, provider_name="openai", model="offline-model")[0]


def adapter(execution, calls=None, **changes):
    def handle(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=envelope(json.dumps(prior.imports.output(execution["contract"]["packet"]))))
    return OpenAIJournalProvider(execution["contract"], **(dict(endpoint="https://offline.invalid/v1/responses", timeout_seconds=45,
        api_key="synthetic-key", transport=httpx.MockTransport(handle)) | changes))


def approval(execution, provider):
    old = dispatch.grant(execution)
    identity = replace(old.proof.identity, surface_id=provider.binding.surface_id, adapter_id=provider.binding.adapter_id,
                       adapter_version=provider.binding.adapter_version, command_shape_sha256=provider.binding.command_shape_sha256)
    return old.model_copy(update={"proof": replace(old.proof, identity=identity, proof_id=compute_provider_proof_id(identity))})


def wait_terminal(execution):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        saved = repo.detail(*prior.scope(execution))
        if saved["current"]["state"] not in {"READY", "LEASED", "RUNNING"}:
            return saved
        time.sleep(.01)
    pytest.fail("Owned offline journal worker did not finish")


def test_exact_wire_identity_is_derived_and_immutable(execution, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "unrelated-model")
    monkeypatch.setenv("OPENAI_API_BASE", "https://unrelated.invalid")
    calls = []
    provider = adapter(execution, calls)
    original = deepcopy(execution["contract"])
    manifest = provider.manifest()
    execution["contract"]["provider_request"]["prompt"] = "changed by caller"
    request = ProviderRequest(**original["provider_request"])
    result = provider.generate_json(request)
    assert len(calls) == 1 and str(calls[0].url) == "https://offline.invalid/v1/responses"
    wire = json.loads(calls[0].content)
    assert wire["model"] == "offline-model" and wire["input"][1]["content"] == request.prompt
    assert wire["text"]["format"]["schema"] == request.schema
    assert wire["max_output_tokens"] == request.max_output_tokens
    assert wire["store"] is False and wire["background"] is False and wire["stream"] is False
    assert wire["truncation"] == "disabled"
    assert manifest["body_sha256"] == hashlib.sha256(calls[0].content).hexdigest()
    assert manifest["body_bytes"] == len(calls[0].content)
    assert provider.binding.command_shape_sha256 == digest(manifest)
    assert manifest["contract_digest"] == digest(original)
    assert "synthetic-key" not in str(manifest) + repr(provider)
    assert result.model == "reported-offline-model"  # Still reported, not independent proof.
    with pytest.raises(FrozenInstanceError):
        provider.model = "another"  # type: ignore[misc]


@pytest.mark.parametrize("change", ["prompt", "schema", "instructions", "metadata", "max_output_tokens"])
def test_mutated_request_is_rejected_before_transport(execution, change):
    calls = []
    provider = adapter(execution, calls)
    fields = deepcopy(execution["contract"]["provider_request"])
    fields[change] = {} if change in {"schema", "metadata"} else 256 if change == "max_output_tokens" else "changed"
    with pytest.raises(ValueError, match="journal_execution_adapter_mismatch"):
        provider.generate_json(ProviderRequest(**fields))
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"endpoint": "http://offline.invalid/v1/responses"}, {"endpoint": "https://user:secret@offline.invalid/v1/responses"},
    {"endpoint": "https://offline.invalid/v1/responses?key=secret"}, {"endpoint": "https://offline.invalid/v1/responses#fragment"},
    {"endpoint": "https://offline.invalid/wrong"}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": float("inf")}, {"timeout_seconds": True}, {"timeout_seconds": 121},
])
def test_invalid_explicit_transport_settings_fail_locally(execution, changes):
    with pytest.raises(ValueError, match="journal_openai_configuration_invalid") as error:
        adapter(execution, **changes)
    assert "secret" not in str(error.value)


def test_default_denial_and_mismatched_binding_consume_no_attempt(execution):
    calls = []
    provider = adapter(execution, calls)
    controller = journal_dispatcher(provider, clock=lambda: prior.NOW)
    with pytest.raises(ValueError, match="authority_unavailable"):
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
    assert controller.stop()
    wrong = AdapterBinding(**(provider.binding.model_dump() | dict(command_shape_sha256="0" * 64)))
    controller = JournalDispatcher(provider=provider, model="offline-model", binding=wrong,
                                   authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW)
    with pytest.raises(ValueError, match="adapter_mismatch"):
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
    assert calls == [] and repo.detail(*prior.scope(execution)) == execution
    assert controller.stop()


def test_changed_endpoint_or_timeout_requires_different_approval(execution):
    original = adapter(execution)
    grant = approval(execution, original)
    for changes in [dict(endpoint="https://different.invalid/v1/responses"), dict(timeout_seconds=46)]:
        calls = []
        changed = adapter(execution, calls, **changes)
        assert changed.binding != original.binding
        controller = journal_dispatcher(changed, authority=lambda *_: grant, clock=lambda: prior.NOW)
        with pytest.raises(ValueError, match="proof_rejected"):
            controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        assert controller.stop() and calls == []
    assert repo.detail(*prior.scope(execution)) == execution


def test_stored_authority_six_duplicate_dispatches_send_once_and_keep_receipt(execution):
    calls = []
    provider = adapter(execution, calls)
    records.register(*prior.scope(execution), approval(execution, provider), provider.binding,
                     issuer="synthetic-issuer", decision_ref="offline-only", expected_sequence=0, now=prior.NOW)
    controllers = [journal_dispatcher(provider, authority=StoredJournalAuthority("synthetic-approval", clock=lambda: prior.NOW),
                                      clock=lambda: prior.NOW) for _ in range(2)]
    barrier = Barrier(6)
    def send(i):
        barrier.wait()
        return controllers[i % 2].dispatch(*prior.scope(execution), dispatch.body(), "owner")
    try:
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(send, range(6)))
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "SUCCEEDED" and len(calls) == 1
        assert saved["events"][0]["dispatch"]["binding"] == provider.binding.model_dump()
        assert saved["events"][0]["adapter_manifest"] == provider.manifest()
        assert "adapter_manifest" not in saved["current"]
        assert saved["current"]["result"]["validation"]["independent_live_proof"] == "NOT_VERIFIED"
        assert controllers[0].dispatch(*prior.scope(execution), dispatch.body(), "owner") == saved
    finally:
        assert all([controller.stop() for controller in controllers])


def test_redirect_is_not_followed_or_retried(execution):
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://unapproved.invalid/v1/responses"})
    provider = adapter(execution, transport=httpx.MockTransport(redirect))
    controller = journal_dispatcher(provider, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW)
    try:
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        failed = wait_terminal(execution)
        assert len(calls) == 1 and failed["current"]["state"] == "INTERRUPTED"
        assert failed["current"]["result"] is None
    finally:
        assert controller.stop()


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed", "cancelled"])
@pytest.mark.parametrize("identity", ["missing", "control", "padded"])
def test_unidentified_terminal_reply_preserves_unknown_across_dispatcher_restart(execution, status, identity):
    calls = []
    payload = prior.imports.output(execution["contract"]["packet"])
    reply = envelope(json.dumps(payload), status=status)
    if identity == "missing":
        del reply["id"]
    elif identity == "control":
        reply["id"] = "resp\nunconfirmed"
    else:
        reply["model"] = "reported-model "

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=reply)

    provider = adapter(execution, transport=httpx.MockTransport(handle))
    grant = approval(execution, provider)
    controller = journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: prior.NOW)
    try:
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        failed = wait_terminal(execution)
        assert failed["current"]["state"] == "INTERRUPTED"
        assert failed["current"]["outcome_unknown"] and failed["current"]["reason"] == "provider_error"
        assert failed["current"]["result"] is None and len(calls) == 1
    finally:
        assert controller.stop()

    # A new dispatcher reads the durable outcome; same request is only a replay.
    controller = journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: prior.NOW)
    try:
        assert controller.dispatch(*prior.scope(execution), dispatch.body(), "owner") == failed
        retry = dispatch.body(request_id="explicit-retry", expected_sequence=failed["current"]["sequence"])
        with pytest.raises(ValueError, match="unknown_outcome"):
            controller.dispatch(*prior.scope(execution), retry, "owner")
        assert repo.detail(*prior.scope(execution)) == failed and len(calls) == 1
        assert journal_candidates.listing(*prior.scope(execution)[:2])["items"] == []

        reply = envelope(json.dumps(payload))
        controller.dispatch(*prior.scope(execution), retry.model_copy(update={"acknowledge_unknown": True}), "owner")
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "SUCCEEDED" and saved["current"]["attempt"] == 2
        assert len(calls) == 2 and saved["events"][:len(failed["events"])] == failed["events"]
        assert saved["contract"] == execution["contract"]
        reservations = [event["dispatch"]["reserved_output_tokens"] for event in saved["events"] if event["state"] == "LEASED"]
        assert reservations == [execution["contract"]["provider_request"]["max_output_tokens"]] * 2
        assert saved["current"]["result"]["validation"]["independent_live_proof"] == "NOT_VERIFIED"
        assert journal_candidates.listing(*prior.scope(execution)[:2])["items"] == []
    finally:
        assert controller.stop()


def test_changed_contract_or_binding_cannot_start_an_admitted_attempt(execution):
    calls = []
    provider = adapter(execution, calls)
    _, claimed = admission.admit(*prior.scope(execution), dispatch.body(), "owner", approval(execution, provider), provider.binding,
                                 now=prior.NOW, adapter_manifest=provider.manifest())
    other = adapter(execution, calls, endpoint="https://changed.invalid/v1/responses")
    with pytest.raises(ValueError, match="adapter_mismatch"):
        execute(claimed, provider=other, model="offline-model", clock=lambda: prior.NOW)
    assert repo.detail(*prior.scope(execution))["current"]["state"] == "LEASED" and calls == []
    assert repo.abandon(claimed, reason="shutdown", now=prior.NOW)


def test_bound_adapter_requires_a_saved_dispatch_receipt(execution):
    calls = []
    provider = adapter(execution, calls)
    claimed = repo.claim(*prior.scope(execution), expected_attempt=0, now=prior.NOW)
    before = repo.detail(*prior.scope(execution))
    with pytest.raises(ValueError, match="adapter_mismatch"):
        execute(claimed, provider=provider, model="offline-model", clock=lambda: prior.NOW)
    assert calls == [] and repo.detail(*prior.scope(execution)) == before
    assert repo.abandon(claimed, reason="shutdown", now=prior.NOW)


def test_bound_attempt_replay_uses_its_original_receipt_without_calling_again(execution):
    calls = []
    provider = adapter(execution, calls)
    _, claimed = admission.admit(*prior.scope(execution), dispatch.body(), "owner", approval(execution, provider), provider.binding,
                                 now=prior.NOW, adapter_manifest=provider.manifest())
    saved = execute(claimed, provider=provider, model="offline-model", clock=lambda: prior.NOW)
    assert saved["current"]["state"] == "SUCCEEDED" and len(calls) == 1
    assert execute(claimed, provider=provider, model="offline-model", clock=lambda: prior.NOW) == saved
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["missing", "changed", "extra"])
def test_missing_or_tampered_manifest_rejects_even_if_event_digest_is_recomputed(execution, mode):
    provider = adapter(execution)
    _, claimed = admission.admit(*prior.scope(execution), dispatch.body(), "owner", approval(execution, provider), provider.binding,
                                 now=prior.NOW, adapter_manifest=provider.manifest())
    with closing(connect_metadata_index()) as c, c:
        row = c.execute("SELECT payload_json FROM journal_execution_events WHERE execution_id=?", (claimed.execution_id,)).fetchone()
        event = json.loads(row[0])
        if mode == "missing":
            del event["adapter_manifest"]
        else:
            event["adapter_manifest"]["body_sha256" if mode == "changed" else "unexpected"] = "0" * 64
        c.execute("DROP TRIGGER journal_execution_events_no_update")
        c.execute("UPDATE journal_execution_events SET payload_json=?,payload_digest=? WHERE execution_id=?",
                  (canonical_json(event), digest(event), claimed.execution_id))
    with pytest.raises(ValueError, match="integrity_invalid"):
        repo.detail(*prior.scope(execution))


def test_manifest_and_reservation_roll_back_together_and_revocation_still_fences(execution, monkeypatch):
    provider = adapter(execution)
    grant = approval(execution, provider)
    def fail(_):
        raise RuntimeError("injected")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "checkpoint", fail)
        with pytest.raises(RuntimeError, match="injected"):
            admission.admit(*prior.scope(execution), dispatch.body(), "owner", grant, provider.binding,
                            now=prior.NOW, adapter_manifest=provider.manifest())
    assert repo.detail(*prior.scope(execution)) == execution
    records.register(*prior.scope(execution), grant, provider.binding,
                     issuer="synthetic-issuer", decision_ref="offline-only", expected_sequence=0, now=prior.NOW)
    ref = records.resolve(*prior.scope(execution), grant.authorization_id, "owner", provider.binding, now=prior.NOW)
    _, claim = admission.admit(*prior.scope(execution), dispatch.body(), "owner", ref, provider.binding,
                               now=prior.NOW, adapter_manifest=provider.manifest())
    records.revoke(*prior.scope(execution), grant.authorization_id, request_id="withdraw", issuer="synthetic-issuer", reason="Offline cancellation", now=prior.NOW)
    saved = repo.detail(*prior.scope(execution))
    assert saved["current"]["state"] == "FAILED" and not saved["current"]["outcome_unknown"]
    assert saved["events"][0]["adapter_manifest"] == provider.manifest()
    assert "adapter_manifest" not in saved["current"]
    assert repo.start(claim, now=prior.NOW) is None


def test_size_schema_and_unicode_changes_are_not_silently_rewritten(execution):
    changed = deepcopy(execution)
    changed["contract"]["provider_request"]["prompt"] = "a" * MAX_REQUEST_BYTES
    with pytest.raises(ValueError, match="request_invalid"):
        adapter(changed)
    changed = deepcopy(execution)
    changed["contract"]["provider_request"]["schema"] = {}
    with pytest.raises(ValueError, match="request_invalid"):
        adapter(changed)
    changed = deepcopy(execution)
    changed["contract"]["provider_request"]["instructions"] += " e\u0301"
    calls = []
    provider = adapter(changed, calls)
    request = ProviderRequest(**deepcopy(changed["contract"]["provider_request"]))
    request.instructions = request.instructions.replace("e\u0301", "\u00e9")
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(request)
    assert calls == []

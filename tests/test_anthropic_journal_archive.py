"""Messages Journal dispatch, human review and self-contained V2 archival tests."""
from contextlib import closing
import json
from threading import Event

import httpx
import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.repositories import journal_authority as records, journal_dispatch as admission, journal_executions as repo
from app.services.journal_execution import execute
from app.services.providers.anthropic_journal import AnthropicJournalProvider, journal_dispatcher
from tests import test_generated_journal as generated, test_journal_execution_controls as controls
from tests import test_openai_journal_binding as shared
from tests.test_anthropic_messages import envelope

source = shared.source
completed = shared.completed


@pytest.fixture
def execution(completed):
    return shared.prior.prepare(completed, provider_name="anthropic", model="offline-model")[0]


def adapter(execution, calls=None, **changes):
    def handle(request):
        if calls is not None:
            calls.append(request)
        output = shared.prior.imports.output(execution["contract"]["packet"])
        return httpx.Response(200, json=envelope(json.dumps(output, ensure_ascii=False, indent=2) + " \n"))
    return AnthropicJournalProvider(execution["contract"], **(dict(api_key="synthetic-messages-key", timeout_seconds=45,
        transport=httpx.MockTransport(handle)) | changes))


@pytest.fixture
def wired(monkeypatch):
    # Reuse ledger-only assertions, never the Responses envelope helpers.
    monkeypatch.setattr(shared, "adapter", adapter)
    monkeypatch.setattr(shared, "journal_dispatcher", journal_dispatcher)


@pytest.mark.parametrize("check", [
    shared.test_default_denial_and_mismatched_binding_consume_no_attempt,
    shared.test_stored_authority_six_duplicate_dispatches_send_once_and_keep_receipt,
    shared.test_redirect_is_not_followed_or_retried,
    shared.test_bound_adapter_requires_a_saved_dispatch_receipt,
    shared.test_bound_attempt_replay_uses_its_original_receipt_without_calling_again,
])
def test_native_messages_uses_real_dispatch_ledger_with_default_deny_and_one_call(execution, wired, check):
    check(execution)


@pytest.mark.parametrize("mode", ["missing", "changed", "extra"])
def test_receipt_reconstruction_rejects_rehashed_messages_manifest(execution, wired, mode):
    shared.test_missing_or_tampered_manifest_rejects_even_if_event_digest_is_recomputed(execution, mode)


def test_admission_rollback_and_stored_revocation_keep_messages_manifest_atomic(execution, wired, monkeypatch):
    shared.test_manifest_and_reservation_roll_back_together_and_revocation_still_fences(execution, monkeypatch)


@pytest.mark.parametrize("mode,state,unknown,reason", [
    ("missing_id", "INTERRUPTED", True, "provider_error"),
    ("timeout", "INTERRUPTED", True, "provider_error"),
    ("refusal", "FAILED", False, "provider_refused"),
    ("max_tokens", "FAILED", False, "provider_incomplete"),
    ("invalid", "FAILED", False, "output_invalid"),
])
def test_native_terminal_and_ambiguous_responses_survive_dispatcher_restart_without_retry(execution, mode, state, unknown, reason):
    calls = []

    def respond(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("private Messages diagnostic", request=request)
        value = envelope(json.dumps(shared.prior.imports.output(execution["contract"]["packet"])))
        if mode == "missing_id":
            del value["id"]
        elif mode in {"refusal", "max_tokens"}:
            value["stop_reason"] = mode
            value["content"] = [dict(type="text", text="private Messages diagnostic")]
        else:
            value["content"][0]["text"] = "{}"
        return httpx.Response(200, json=value)

    provider = adapter(execution, transport=httpx.MockTransport(respond))
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    controller = journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        controller.dispatch(*scope, shared.dispatch.body(), "owner")
        saved = shared.wait_terminal(execution)
        assert (saved["current"]["state"], saved["current"]["outcome_unknown"], saved["current"]["reason"]) == (state, unknown, reason)
        assert saved["current"]["result"] is None and len(calls) == 1
        assert "private Messages diagnostic" not in preserved_json(saved)
    finally:
        assert controller.stop()
    reopened = journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        assert reopened.dispatch(*scope, shared.dispatch.body(), "owner") == saved
        if unknown:
            retry = shared.dispatch.body(request_id="explicit-retry", expected_sequence=saved["current"]["sequence"])
            with pytest.raises(ValueError, match="unknown_outcome"):
                reopened.dispatch(*scope, retry, "owner")
        assert repo.detail(*scope) == saved and len(calls) == 1
        assert generated.candidates.listing(*scope[:2])["total"] == 0
    finally:
        assert reopened.stop()


@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_late_messages_response_cannot_cross_cancel_or_revocation_fence(execution, action):
    entered, release = Event(), Event()
    calls = []

    def respond(request):
        calls.append(request)
        entered.set()
        assert release.wait(20)
        return httpx.Response(200, json=envelope(json.dumps(shared.prior.imports.output(execution["contract"]["packet"]))))

    provider = adapter(execution, transport=httpx.MockTransport(respond))
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    records.register(*scope, grant, provider.binding, issuer="synthetic", decision_ref="mock-only", expected_sequence=0, now=shared.prior.NOW)
    controller = journal_dispatcher(provider, authority=shared.StoredJournalAuthority(grant.authorization_id, clock=lambda: shared.prior.NOW),
        clock=lambda: shared.prior.NOW)
    try:
        controller.dispatch(*scope, shared.dispatch.body(), "owner")
        assert entered.wait(10)
        if action == "cancel":
            controls.command(execution, sequence=2)
        else:
            records.revoke(*scope, grant.authorization_id, request_id="withdraw", issuer="synthetic", reason="test", now=shared.prior.NOW)
        saved = repo.detail(*scope)
        assert saved["current"]["outcome_unknown"] and saved["current"]["result"] is None
    finally:
        release.set()
        for worker in controller._workers:
            assert worker.wait(10)
        assert controller.stop()
    assert repo.detail(*scope) == saved and len(calls) == 1
    assert generated.candidates.listing(*scope[:2])["total"] == 0


def succeeded(execution):
    calls = []
    provider = adapter(execution, calls)
    _, claim = admission.admit(*shared.prior.scope(execution), shared.dispatch.body(), "owner", shared.approval(execution, provider),
        provider.binding, now=shared.prior.NOW, adapter_manifest=provider.manifest())
    saved = execute(claim, provider=provider, model=provider.model, clock=lambda: shared.prior.NOW)
    assert saved["current"]["state"] == "SUCCEEDED" and len(calls) == 1
    result = saved["current"]["result"]
    assert result["provider_reported"]["provider_name"] == "anthropic"
    assert result["provider_reported"]["usage"] == envelope()["usage"]
    assert "total_tokens" not in result["provider_reported"]["usage"]
    assert result["raw_json"].endswith(" \n")
    assert generated.candidates.listing(*shared.prior.scope(execution)[:2])["total"] == 0
    return saved


def test_messages_candidate_requires_explicit_preservation_and_human_acceptance(execution, source, monkeypatch):
    generated.test_generated_and_imported_same_text_have_distinct_identity_and_judgments(succeeded(execution), source, monkeypatch)


def test_messages_candidate_rollback_review_export_and_fresh_process_reopen(execution, monkeypatch, tmp_path):
    generated.test_rollback_process_death_and_restarted_review_readback(succeeded(execution), monkeypatch, tmp_path)


def test_archived_v2_keeps_content_text_json_usage_and_manifest_without_execution_store(execution, monkeypatch):
    saved = succeeded(execution)
    candidate, _ = generated.preserve(saved)
    receipt = candidate["execution"]
    assert receipt["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V2"
    assert receipt["adapter_manifest"] == saved["events"][0]["adapter_manifest"]
    assert receipt["adapter_manifest"]["raw_output_encoding"] == "CONTENT_TEXT_JSON_UTF8_V1"
    assert receipt["adapter_manifest"]["raw_envelope_retained"] is False
    assert receipt["provider_reported"]["usage"] == envelope()["usage"]
    assert "total_tokens" not in receipt["provider_reported"]["usage"]
    assert "content text JSON" in candidate["markdown"] and "전체 HTTP 응답은 보존되지 않습니다" in candidate["markdown"]
    monkeypatch.setattr(repo, "_read", lambda *args: pytest.fail("Archived candidate must not consult the execution ledger"))
    assert generated.candidates.detail(*shared.prior.scope(saved)[:2], candidate["candidate_id"]) == candidate
    accepted, _ = generated.review_tests.save(candidate, generated.review_tests.body(candidate))
    assert "전체 HTTP 응답은 보존되지 않습니다" in accepted["report"]["content"]
    args = (*shared.prior.scope(saved)[:2], candidate["candidate_id"], accepted["review_id"])
    bundle = json.loads(generated.reviews.export(*args, "json")["content"])
    assert bundle["candidate"] == candidate and bundle["review"] == accepted
    assert generated.reviews.export(*args, "markdown")["content"] == accepted["report"]["content"]


@pytest.mark.parametrize("change", ["encoding", "model", "extra"])
def test_archived_manifest_tampering_rejects_even_after_all_outer_hashes_are_rebuilt(execution, change):
    saved = succeeded(execution)
    candidate, _ = generated.preserve(saved)
    receipt = candidate["execution"]
    old_digest = receipt["adapter_manifest_digest"]
    field, value = {"encoding": ("raw_output_encoding", "RAW_HTTP_ENVELOPE"), "model": ("model", "forged-model"),
                    "extra": ("unverified_claim", "LIVE_VERIFIED")}[change]
    receipt["adapter_manifest"][field] = value
    receipt["adapter_manifest_digest"] = digest(receipt["adapter_manifest"])
    candidate["markdown"] = candidate["markdown"].replace(old_digest, receipt["adapter_manifest_digest"])
    candidate["content_sha256"] = sha(candidate["markdown"])
    with closing(connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER journal_candidates_no_update")
        connection.execute("UPDATE journal_candidates SET payload_json=?,payload_digest=? WHERE candidate_id=?",
            (preserved_json(candidate), digest(candidate), candidate["candidate_id"]))
    with pytest.raises(ValueError, match="integrity_invalid"):
        generated.candidates.detail(*shared.prior.scope(saved)[:2], candidate["candidate_id"])
    with pytest.raises(ValueError, match="integrity_invalid"):
        generated.review_tests.save(candidate, generated.review_tests.body(candidate))

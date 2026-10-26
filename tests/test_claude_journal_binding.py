"""Journal admission/recovery with synthetic authority and a fake Python CLI."""
from dataclasses import replace
from contextlib import closing
from datetime import timedelta
import importlib
import json
from threading import Event
from types import SimpleNamespace

import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain.temporal_memory import digest
from app.repositories import journal_executions as repo, journal_dispatch as admission, journal_authority as records
from app.services.journal_execution import execute
from app.services.providers.claude_prepared_review_process import ClaudeRuntime
from app.services.provider_output_class_proof import compute_provider_proof_id
from tests import test_openai_journal_binding as shared, test_generated_journal as generated
from tests import test_journal_execution_controls as controls
from tests import test_claude_journal as fixtures

source = shared.source
completed = shared.completed


@pytest.fixture
def execution(completed):
    return shared.prior.prepare(completed, provider_name="claude_code", model="offline-model")[0]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    module = importlib.import_module("app.services.providers.claude_journal")
    roots, observers = [], {}
    original = ClaudeRuntime.run
    def run(runtime, *args, **kwargs):
        calls = observers.get(runtime.workspace)
        if calls is not None:
            calls.append(args[0])
        return original(runtime, *args, **kwargs)
    monkeypatch.setattr(ClaudeRuntime, "run", run)
    def adapter(execution, calls=None, *, mode="success", raw=None, timeout_seconds=45):
        root = tmp_path / f"cli-{len(roots)}"
        roots.append(root)
        if mode == "success" and raw is None:
            output = fixtures.envelope(execution["contract"])
            output["structured_output"] = shared.prior.imports.output(execution["contract"]["packet"])
            raw = json.dumps(output, ensure_ascii=False)
        runtime = fixtures.runtime_at(root, execution["contract"], mode=mode, raw=raw)
        observers[runtime.workspace] = calls
        return module.ClaudeJournalProvider(execution["contract"], runtime=runtime, timeout_seconds=timeout_seconds)
    monkeypatch.setattr(shared, "adapter", adapter)
    monkeypatch.setattr(shared, "journal_dispatcher", module.journal_dispatcher)
    return SimpleNamespace(adapter=adapter, journal_dispatcher=module.journal_dispatcher, roots=roots)


@pytest.mark.parametrize("check", [
    shared.test_default_denial_and_mismatched_binding_consume_no_attempt,
    shared.test_stored_authority_six_duplicate_dispatches_send_once_and_keep_receipt,
    shared.test_bound_adapter_requires_a_saved_dispatch_receipt,
    shared.test_bound_attempt_replay_uses_its_original_receipt_without_calling_again,
])
def test_shared_contracts_with_actual_claude_journal_adapter(execution, wired, check):
    check(execution)


@pytest.mark.parametrize("mode", ["missing", "changed", "extra"])
def test_rehashed_manifest_tampering_rejected(execution, wired, mode):
    shared.test_missing_or_tampered_manifest_rejects_even_if_event_digest_is_recomputed(execution, mode)


def test_atomic_admission_and_stored_revocation(execution, wired, monkeypatch):
    shared.test_manifest_and_reservation_roll_back_together_and_revocation_still_fences(execution, monkeypatch)


@pytest.mark.parametrize("when", ["before_admission", "after_admission"])
def test_changed_launcher_cannot_run_under_original_authority(execution, wired, when):
    calls = []
    provider = wired.adapter(execution, calls)
    scope = shared.prior.scope(execution)
    grant = shared.approval(execution, provider)
    if when == "after_admission":
        _, claim = admission.admit(*scope, shared.dispatch.body(), "owner", grant, provider.binding,
            now=shared.prior.NOW, adapter_manifest=provider.manifest())
    script = wired.roots[0] / "fake_claude.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed launcher\n", encoding="utf-8")
    if when == "before_admission":
        controller = wired.journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
        try:
            with pytest.raises(ValueError, match="runtime_changed"):
                controller.dispatch(*scope, shared.dispatch.body(), "owner")
        finally:
            assert controller.stop()
        assert repo.detail(*scope) == execution and calls == []
    else:
        with pytest.raises(ValueError, match="runtime_changed"):
            execute(claim, provider=provider, model=provider.model, clock=lambda: shared.prior.NOW)
        saved = repo.detail(*scope)
        assert saved["current"]["state"] == "INTERRUPTED" and saved["current"]["outcome_unknown"]
        assert saved["current"]["result"] is None
    assert not (wired.roots[0] / "launches.log").exists()


def test_changed_plan_requires_new_authority_and_cannot_start_old_claim(execution, wired):
    calls = []
    provider = wired.adapter(execution, calls)
    other = type(provider)(execution["contract"], runtime=provider._runtime, timeout_seconds=46)
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    controller = wired.journal_dispatcher(other, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        with pytest.raises(ValueError, match="proof_rejected"):
            controller.dispatch(*scope, shared.dispatch.body(), "owner")
    finally:
        assert controller.stop()
    assert repo.detail(*scope) == execution and calls == []
    _, claim = admission.admit(*scope, shared.dispatch.body(), "owner", grant, provider.binding,
        now=shared.prior.NOW, adapter_manifest=provider.manifest())
    with pytest.raises(ValueError, match="adapter_mismatch"):
        execute(claim, provider=other, model=other.model, clock=lambda: shared.prior.NOW)
    assert repo.detail(*scope)["current"]["state"] == "LEASED" and calls == []
    assert repo.abandon(claim, reason="shutdown", now=shared.prior.NOW)


@pytest.mark.parametrize("change", ["actor", "topic", "provider", "paper_stage", "codex_surface", "expired"])
def test_foreign_authority_cannot_admit_or_launch(execution, wired, change):
    calls = []
    provider = wired.adapter(execution, calls)
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
                  "codex_surface": {"surface_id": "codex_cli"}}
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
    assert not (wired.roots[0] / "launches.log").exists()


@pytest.mark.parametrize("action", ["cancel", "revoke", "shutdown", "expire"])
def test_late_cli_result_is_fenced_without_claiming_remote_cancellation(execution, wired, monkeypatch, action):
    entered, release = Event(), Event()
    calls, now = [], [shared.prior.NOW]
    provider = wired.adapter(execution, calls)
    original = ClaudeRuntime.run
    def run(runtime, *args, **kwargs):
        result = original(runtime, *args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(ClaudeRuntime, "run", run)
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    records.register(*scope, grant, provider.binding, issuer="synthetic", decision_ref="fake-CLI-only", expected_sequence=0, now=now[0])
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


@pytest.mark.parametrize("mode,state,reason", [("missing", "INTERRUPTED", "provider_error"),
    ("nonzero", "INTERRUPTED", "provider_error"), ("citation", "FAILED", "output_invalid"),
    ("failed", "FAILED", "provider_failed")])
def test_failed_or_unknown_cli_response_persists_and_never_relaunches_on_replay(execution, wired, mode, state, reason):
    calls = []
    provider = wired.adapter(execution, calls, mode=mode)
    grant = shared.approval(execution, provider)
    scope = shared.prior.scope(execution)
    controller = wired.journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        controller.dispatch(*scope, shared.dispatch.body(), "owner")
        failed = shared.wait_terminal(execution)
        assert failed["current"]["state"] == state and failed["current"]["reason"] == reason
        assert failed["current"]["result"] is None and len(calls) == 1
    finally:
        assert controller.stop()
    controller = wired.journal_dispatcher(provider, authority=lambda *_: grant, clock=lambda: shared.prior.NOW)
    try:
        assert controller.dispatch(*scope, shared.dispatch.body(), "owner") == failed
        if state == "INTERRUPTED":
            retry = shared.dispatch.body(request_id="explicit-retry", expected_sequence=failed["current"]["sequence"])
            with pytest.raises(ValueError, match="unknown_outcome"):
                controller.dispatch(*scope, retry, "owner")
            assert len(calls) == 1
            (wired.roots[0] / "envelope.json").write_text(json.dumps(fixtures.envelope(execution["contract"])), encoding="utf-8")
            # nonzero is baked into this launcher's identity; an acknowledged
            # retry may still fail and must not silently replace the launcher.
            controller.dispatch(*scope, retry.model_copy(update={"acknowledge_unknown": True}), "owner")
            saved = shared.wait_terminal(execution)
            assert saved["current"]["attempt"] == 2 and len(calls) == 2
            assert saved["current"]["state"] == ("SUCCEEDED" if mode == "missing" else "INTERRUPTED")
            assert saved["events"][:len(failed["events"])] == failed["events"]
        assert shared.journal_candidates.listing(*scope[:2])["total"] == 0
    finally:
        assert controller.stop()


def succeeded(execution, wired):
    calls = []
    provider = wired.adapter(execution, calls)
    _, claim = admission.admit(*shared.prior.scope(execution), shared.dispatch.body(), "owner", shared.approval(execution, provider),
        provider.binding, now=shared.prior.NOW, adapter_manifest=provider.manifest())
    saved = execute(claim, provider=provider, model=provider.model, clock=lambda: shared.prior.NOW)
    assert saved["current"]["state"] == "SUCCEEDED" and len(calls) == 1
    assert saved["current"]["result"]["provider_reported"]["model"] == "not_reported"
    assert saved["current"]["result"]["provider_reported"]["provider_name"] == "claude_code"
    assert shared.journal_candidates.listing(*shared.prior.scope(execution)[:2])["total"] == 0
    return saved


def test_claude_candidate_requires_human_acceptance(execution, wired, source, monkeypatch):
    generated.test_generated_and_imported_same_text_have_distinct_identity_and_judgments(succeeded(execution, wired), source, monkeypatch)


def test_claude_candidate_rollback_export_and_fresh_process_readback(execution, wired, monkeypatch, tmp_path):
    generated.test_rollback_process_death_and_restarted_review_readback(succeeded(execution, wired), monkeypatch, tmp_path)


def test_candidate_and_exports_keep_extracted_json_provenance_without_execution_store(execution, wired, monkeypatch):
    saved = succeeded(execution, wired)
    candidate, _ = generated.preserve(saved)
    receipt = candidate["execution"]
    assert receipt["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V2"
    assert receipt["adapter_manifest"] == saved["events"][0]["adapter_manifest"]
    assert receipt["adapter_manifest"]["raw_journal_encoding"] == "EXTRACTED_CANONICAL_JSON_V1"
    assert receipt["adapter_manifest"]["raw_envelope_retained"] is False
    assert "추출 JSON SHA-256" in candidate["markdown"] and "CLI 표준 출력 원문" in candidate["markdown"]
    monkeypatch.setattr(repo, "_read", lambda *args: pytest.fail("preserved candidate/export must be self-contained"))
    accepted, _ = generated.review_tests.save(candidate, generated.review_tests.body(candidate))
    assert "CLI 표준 출력 원문" in accepted["report"]["content"]
    args = (*shared.prior.scope(saved)[:2], candidate["candidate_id"], accepted["review_id"])
    bundle = json.loads(generated.reviews.export(*args, "json")["content"])
    assert bundle["candidate"] == candidate and bundle["review"] == accepted
    assert generated.reviews.export(*args, "markdown")["content"] == accepted["report"]["content"]


@pytest.mark.parametrize("change", ["encoding", "model", "extra"])
def test_candidate_manifest_tampering_cannot_be_hidden_by_rehashing(execution, wired, change):
    saved = succeeded(execution, wired)
    candidate, _ = generated.preserve(saved)
    manifest = candidate["execution"]["adapter_manifest"]
    key, value = {"encoding": ("raw_envelope_retained", True), "model": ("model", "other-model"),
                  "extra": ("unverified_claim", "LIVE_VERIFIED")}[change]
    manifest[key] = value
    with closing(connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER journal_candidates_no_update")
        c.execute("UPDATE journal_candidates SET payload_json=?,payload_digest=? WHERE candidate_id=?",
                  (json.dumps(candidate), digest(candidate), candidate["candidate_id"]))
    with pytest.raises(ValueError, match="integrity_invalid"):
        generated.candidates.detail(*shared.prior.scope(saved)[:2], candidate["candidate_id"])

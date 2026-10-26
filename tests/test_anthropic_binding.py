"""Anthropic paper ledger and connection binding; synthetic MockTransport only."""
from contextlib import closing
import json
from threading import Event

import httpx
import pytest

from app.auth.resource_access import resource_actor_for_user
from app.db import bootstrap
from app.domain import prepared_review, review_execution as domain, review_transport_execution as transport
from app.domain.journal_exchange import preserved_json
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_authorities as authorities, review_executions as repository, review_preparations
from app.services import model_connection_pipeline as pipeline
from app.services import review_execution as execution_service
from app.services.providers.anthropic_prepared_review import AnthropicPreparedReviewProvider
from app.services.providers.base import ProviderRequest, ProviderUnavailableError
from app.services.review_dispatch import PaperDispatchRuntime
from tests import test_model_connection_pipeline as selections
from tests import test_prepared_review as drafts, test_review_http_dispatch as shared
from tests.test_anthropic_messages import envelope

runtimes = shared.runtimes
setup = shared.setup


def seed_anthropic(preparation, *, request="messages-fixture", state="READY"):
    """Browser fixture: native Messages mocks and synthetic authority, no network."""
    scope = execution_service.scope(preparation["topic_id"], preparation["paper_version_id"],
        preparation["preparation_id"], resource_actor_for_user(None))
    frozen = prepared_review.prepare(build_packet(preparation), provider_name="anthropic", model="synthetic-requested-model")
    value = envelope(drafts.response(frozen).raw_text)
    terminal = {"refusal": "refusal", "incomplete": "max_tokens"}.get(state)
    if terminal:
        value["stop_reason"] = terminal
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        return httpx.Response(200, json=value)

    provider = AnthropicPreparedReviewProvider(frozen, api_key="synthetic-messages-key", timeout_seconds=30,
        transport=httpx.MockTransport(respond))
    body = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(preparation)),
        provider_name="anthropic", model=frozen["model"], acknowledge_unknown=True)
    saved = repository.prepare_transport(scope, body, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
    identity = saved["contract"]["execution_id"]
    if state == "READY":
        return scope, saved
    command = domain.Control(request_id="approve", expected_sequence=0, action="approve")
    saved = authorities.register(scope, identity, command, shared.issuer(saved["contract"], command))
    if state == "APPROVED":
        return scope, saved
    if terminal:
        runtime = PaperDispatchRuntime(resolver=shared.issuer, provider=provider)
        try:
            runtime.dispatch(scope, identity, shared.previous.dispatch_body())
            runtime.wait_idle(30)
            saved = repository.detail(scope, identity, schema=transport.SCHEMA)
            assert len(calls) == 1 and saved["current"]["reason"] == ("provider_refused" if state == "refusal" else "provider_incomplete")
            return scope, saved
        finally:
            runtime.stop()
    if state not in {"INTERRUPTED", "RESULT_RECEIVED", "RESULT_SAVED"}:
        raise ValueError("Unknown synthetic Messages fixture state")
    _, claim = repository.claim(scope, identity, shared.previous.dispatch_body(), enabled=True, schema=transport.SCHEMA)
    repository.worker_start(claim)
    if state == "INTERRUPTED":
        repository.interrupt(claim, "adapter_failed")
        return scope, repository.detail(scope, identity, schema=transport.SCHEMA)
    saved = repository.receive(claim, provider.generate_record(ProviderRequest(**frozen["provider_request"])))
    assert len(calls) == 1
    if state == "RESULT_SAVED":
        repository.publish(claim)
        saved = repository.detail(scope, identity, schema=transport.SCHEMA)
    return scope, saved


@pytest.fixture
def messages_setup(setup, runtimes):
    scope, _, clock = setup
    preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    frozen = prepared_review.prepare(build_packet(preparation), provider_name="anthropic", model="synthetic-requested-model")
    created = []

    def make(handler=None, request="messages", **options):
        calls = []

        def respond(outgoing):
            calls.append(outgoing)
            return handler(outgoing) if handler else httpx.Response(200, json=envelope(drafts.response(frozen).raw_text))

        provider = AnthropicPreparedReviewProvider(frozen, **(dict(api_key="synthetic-messages-key", timeout_seconds=30,
            transport=httpx.MockTransport(respond)) | options))
        body = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(preparation)),
            provider_name="anthropic", model=frozen["model"])
        ready = repository.prepare_transport(scope, body, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
        runtime = PaperDispatchRuntime(resolver=shared.issuer, provider=provider)
        runtimes.append(runtime)
        created.append((ready, provider, runtime, calls, body))
        return created[-1]

    return (scope, frozen, make, clock), created


def test_six_duplicate_dispatches_persist_one_native_messages_receipt_and_reopen_in_process(messages_setup):
    contract, created = messages_setup
    shared.test_concurrent_http_dispatch_one_call_receipt_replay_and_process_restart(contract)
    scope, frozen, _, _ = contract
    ready, provider, _, calls, _ = created[0]
    saved = shared.previous.read(scope, ready)
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        receipt = ai_review_drafts.get_input_receipt_in(connection, scope.topic, saved["current"]["receipt_id"])
    proof = receipt["validation"]["prepared_execution"]
    assert proof["response"]["usage"] == envelope()["usage"]
    assert "total_tokens" not in proof["response"]["usage"]
    assert proof["response"]["raw_text"] == drafts.response(frozen).raw_text
    assert proof["response"]["provider_name"] == "anthropic"
    assert receipt["validation"]["execution"]["manifest"] == provider.manifest()
    assert calls[0].url.path == "/v1/messages" and calls[0].headers["x-api-key"] == "synthetic-messages-key"
    assert "authorization" not in calls[0].headers
    wire = json.loads(calls[0].content)
    assert wire["messages"] == [dict(role="user", content=frozen["provider_request"]["prompt"])]
    assert wire["model"] == frozen["model"] and "input" not in wire
    assert "synthetic-messages-key" not in preserved_json(receipt) + preserved_json(saved) + repr(provider)


@pytest.mark.parametrize("surface", ["codex_cli", "offline_synthetic"])
def test_paper_authority_denies_foreign_surface_without_attempt_or_call(messages_setup, surface):
    shared.test_other_surface_proof_cannot_approve_http(messages_setup[0], surface)


def test_missing_key_and_changed_messages_binding_are_refused_before_claim(messages_setup):
    (scope, frozen, make, _), _ = messages_setup
    ready, _, runtime, calls, _ = make(api_key="")
    approved = shared.previous.approve(scope, ready, runtime)
    with pytest.raises(ProviderUnavailableError):
        runtime.dispatch(scope, ready["contract"]["execution_id"], shared.previous.dispatch_body())
    changed_calls = []
    changed = AnthropicPreparedReviewProvider(frozen, api_key="synthetic", timeout_seconds=31,
        transport=httpx.MockTransport(lambda request: changed_calls.append(request)))
    disabled = PaperDispatchRuntime(resolver=shared.issuer, provider=changed)
    try:
        with pytest.raises(ValueError, match="transport_mismatch"):
            disabled.dispatch(scope, ready["contract"]["execution_id"], shared.previous.dispatch_body())
    finally:
        disabled.stop()
    assert shared.previous.read(scope, ready) == approved and calls == changed_calls == []


@pytest.mark.parametrize("mode,state,unknown,reason", [
    ("missing_id", "INTERRUPTED", True, "adapter_failed"),
    ("timeout", "INTERRUPTED", True, "adapter_failed"),
    ("refusal", "FAILED", False, "provider_refused"),
    ("max_tokens", "FAILED", False, "provider_incomplete"),
    ("invalid", "FAILED", False, "output_invalid"),
    ("blank", "FAILED", False, "output_invalid"),
])
def test_paper_messages_failure_classes_persist_without_receipt_or_implicit_retry(messages_setup, mode, state, unknown, reason):
    (scope, frozen, make, _), _ = messages_setup

    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private synthetic diagnostic", request=request)
        value = envelope(drafts.response(frozen).raw_text)
        if mode == "missing_id":
            del value["id"]
        elif mode in {"refusal", "max_tokens"}:
            value["stop_reason"] = mode
            value["content"] = [dict(type="text", text="private synthetic diagnostic")]
        elif mode == "invalid":
            value["content"][0]["text"] = "{}"
        else:
            draft = json.loads(value["content"][0]["text"])
            draft["method_summary"] = " \t\n\u3000"
            value["content"][0]["text"] = json.dumps(draft, ensure_ascii=False)
        return httpx.Response(200, json=value)

    ready, _, runtime, calls, _ = make(respond)
    shared.previous.approve(scope, ready, runtime)
    identity = ready["contract"]["execution_id"]
    runtime.dispatch(scope, identity, shared.previous.dispatch_body())
    runtime.wait_idle(30)
    saved = shared.previous.read(scope, ready)
    assert (saved["current"]["state"], saved["current"]["outcome_unknown"], saved["current"]["reason"]) == (state, unknown, reason)
    assert saved["current"]["receipt_id"] is None and "private synthetic diagnostic" not in encoded(saved)
    assert runtime.dispatch(scope, identity, shared.previous.dispatch_body()) == saved and len(calls) == 1
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='ai_review_input_history'").fetchone() is None
    if unknown:
        with pytest.raises(ValueError, match="unknown_outcome"):
            make(request="retry")
    else:
        retry, _, retry_runtime, retry_calls, _ = make(respond, request="retry")
        assert retry["current"]["state"] == "READY" and retry_calls == []
        with pytest.raises(ValueError, match="not_approved"):
            retry_runtime.dispatch(scope, retry["contract"]["execution_id"], domain.Dispatch(request_id="unapproved", expected_sequence=0))
    assert len(calls) == 1 and shared.previous.read(scope, ready) == saved


@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_paper_messages_late_result_is_fenced_after_control(messages_setup, action):
    (scope, frozen, make, _), _ = messages_setup
    entered, release = Event(), Event()

    def blocked(_):
        entered.set()
        assert release.wait(20)
        return httpx.Response(200, json=envelope(drafts.response(frozen).raw_text))

    ready, _, runtime, calls, _ = make(blocked)
    identity = ready["contract"]["execution_id"]
    shared.previous.approve(scope, ready, runtime)
    runtime.dispatch(scope, identity, shared.previous.dispatch_body())
    try:
        assert entered.wait(10)
        with closing(bootstrap.connect_metadata_index()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE competing_messages_writer(value TEXT)")
        command = domain.Control(request_id=action, expected_sequence=3, action=action)
        if action == "revoke":
            authorities.revoke(scope, identity, command, reason="synthetic revocation")
        else:
            repository.control(scope, identity, command, enabled=False, schema=transport.SCHEMA)
        saved = shared.previous.read(scope, ready)
        assert saved["current"]["outcome_unknown"] and saved["current"]["receipt_id"] is None
    finally:
        release.set()
        runtime.wait_idle(30)
    assert shared.previous.read(scope, ready) == saved and len(calls) == 1


@pytest.fixture
def connection_state(tmp_path, monkeypatch):
    monkeypatch.setattr(selections.model_connections, "STATE_PATH", tmp_path / "connections.json")
    monkeypatch.delenv("XRML_PROVIDER", raising=False)
    for connector in selections.model_connections.CONNECTORS:
        if connector.credential_env:
            monkeypatch.delenv(connector.credential_env, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-openai-key")
    monkeypatch.setenv("XAI_API_KEY", "wrong-xai-key")
    monkeypatch.setenv("CLAUDE_API_KEY", "wrong-cli-key")
    selections.select("anthropic_api", "frozen-anthropic-model")


@pytest.mark.parametrize("kind", ["paper", "journal"])
def test_pipeline_uses_only_fixed_anthropic_key_and_keeps_frozen_choice(connection_state, monkeypatch, kind):
    if kind == "paper":
        frozen = pipeline.prepare_paper(drafts.packet())
        raw = drafts.response(frozen).raw_text
        bind = pipeline.bind_paper
    else:
        from tests.test_anthropic_journal import contract
        from tests.test_journal_review_packet import proposal
        frozen = contract(model="frozen-anthropic-model")
        raw = json.dumps(proposal(frozen["packet"]), ensure_ascii=False)
        bind = pipeline.bind_journal
    with pytest.raises(ValueError, match="model_connection_key_required"):
        bind(frozen, connector_id="anthropic_api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-private-key")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(raw))

    provider = bind(frozen, connector_id="anthropic_api", transport=httpx.MockTransport(handle))
    assert calls == [] and provider.describe_status()["live_call_enabled"] is False
    selections.select("codex_subscription", "changed-default")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "changed-after-binding")
    request = ProviderRequest(**frozen["provider_request"])
    result = provider.generate_record(request) if kind == "paper" else provider.generate_json(request)
    assert len(calls) == 1 and str(calls[0].url) == "https://api.anthropic.com/v1/messages"
    assert calls[0].headers["x-api-key"] == "synthetic-anthropic-private-key" and "authorization" not in calls[0].headers
    assert json.loads(calls[0].content)["model"] == "frozen-anthropic-model"
    assert provider.manifest()["automatic_retry"] is False
    public = repr(provider) + preserved_json(provider.manifest()) + (preserved_json(result) if kind == "paper" else result.raw_text)
    assert "synthetic-anthropic-private-key" not in public and "changed-after-binding" not in public
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        bind(frozen, connector_id="openai_api")

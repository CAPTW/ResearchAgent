"""Synthetic Ollama chat bytes only: no daemon, model, network or receipt store."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json

import httpx
import pytest

from app.domain import prepared_review, review_preparation, review_range_input, review_token_budget
from app.domain.journal_exchange import preserved_json, sha
from app.services.providers import ollama_prepared_review as adapter
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from tests import test_prepared_review as prior

CONTEXT = 131072


def contract(*, model="synthetic-local-model:tiny", document=False):
    packet = prior.packet()
    if document:
        text = "x" * 30000 + " e\u0301 한글🙂 tail "
        source = packet["source_preparation"]["source"]
        source = {**source, "source_digest": review_preparation.source_digest(source)}
        unit = dict(schema_version=review_token_budget.ANALYSIS_UNIT_SCHEMA, plan_id="rplan_" + "1" * 64,
            plan_sha256="2" * 64, unit_index=0, unit_count=1, label="Synthetic section", document_chars=len(text),
            outline=[dict(label="Synthetic section", start=30000, end=len(text))], context_before="", context_after="",
            budget=dict(provider_name="ollama", model=model, context_tokens=CONTEXT, input_tokens=100000,
                max_output_tokens=2200, safety_tokens=4096, estimator=review_token_budget.ESTIMATOR))
        body = review_preparation.DocumentPrepare(request_id="synthetic-document", run_id=source["run_id"],
            paper_id=source["paper_id"], expected_source_digest=source["source_digest"], start=30000, end=len(text), analysis_unit=unit)
        context = dict(schema_version="REVIEW_CONTEXT_V1", topic_title="Synthetic topic", topic_name="synthetic",
            paper_title="Synthetic paper", authors=[], published_at=None, metadata_digest="3" * 64, topic_updated_at="2026-01-01")
        saved = review_preparation.build("topic", "version", "owner", body, source, text, "2026-01-01T00:00:00Z", context=context)
        packet = review_range_input.build_packet(saved)
    return prepared_review.prepare(packet, provider_name="ollama", model=model)


def envelope(frozen):
    return dict(model="synthetic-reported-model:alias", created_at="2026-01-01T00:00:00Z", done=True, done_reason="stop",
        message=dict(role="assistant", content=prior.response(frozen).raw_text),
        prompt_eval_count=4, eval_count=7, total_duration=9000000, load_duration=1000000,
        prompt_eval_duration=2000000, eval_duration=6000000)


def wire(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def provider_for(frozen=None, handler=None, **options):
    frozen = contract() if frozen is None else frozen
    calls = []

    def respond(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, content=wire(envelope(frozen)))

    provider = adapter.OllamaPreparedReviewProvider(frozen, **(dict(context_tokens=CONTEXT, timeout_seconds=30,
        transport=httpx.MockTransport(respond)) | options))
    return provider, frozen, calls


@pytest.mark.parametrize("document", [False, True])
def test_exact_wire_and_separate_unicode_envelope_rebuild_without_locality_claim(document, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic-proxy.invalid")
    monkeypatch.setenv("OLLAMA_HOST", "https://synthetic-remote.invalid")
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-unused-credential")
    provider, frozen, calls = provider_for(contract(document=document))
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1
    request = calls[0]
    body = json.loads(request.content)
    original = frozen["provider_request"]
    assert str(request.url) == adapter.ENDPOINT and request.method == "POST"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert body == dict(model=frozen["model"], messages=[dict(role="system", content=original["instructions"]),
        dict(role="user", content=original["prompt"])], format=original["schema"], stream=False, think=False,
        keep_alive=0, truncate=False, shift=False, options=dict(num_ctx=CONTEXT, num_predict=2200, temperature=0.2))
    assert "tools" not in body and "metadata" not in body
    marker = record["validation"]["execution"]
    response = record["validation"]["prepared_execution"]["response"]
    assert marker["http_response_text"] == wire(envelope(frozen)).decode("utf-8")
    assert response["raw_text"] == envelope(frozen)["message"]["content"]
    assert marker["http_response_text"] != response["raw_text"]
    assert marker["http_response_sha256"] == sha(marker["http_response_text"])
    assert marker["response_sha256"] == response["raw_text_sha256"] == sha(response["raw_text"])
    assert response["model"] == "synthetic-reported-model:alias" and frozen["model"] == "synthetic-local-model:tiny"
    assert response["response_id"] is None and response["usage"] == {"input_tokens": 4, "output_tokens": 7}
    assert marker["reported_metrics"] == {key: envelope(frozen)[key] for key in (
        "prompt_eval_count", "eval_count", "total_duration", "load_duration", "prompt_eval_duration", "eval_duration")}
    assert marker["input_origin"] == "FIXTURE_SYNTHETIC" and marker["locality"] == "LOCALITY_NOT_VERIFIED"
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    assert record["validation"]["prepared_execution"]["scientific_support"] == "NOT_ASSESSED"
    assert record["validation"]["prepared_execution"]["reported_model_identity"] == "NOT_VERIFIED"
    assert record["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    assert " e\u0301 한글🙂 output " in response["raw_text"]
    adapter.validate_transport(record, frozen["packet"])
    assert provider.describe_status() == dict(provider="ollama", model=frozen["model"], configured=True,
        live_call_enabled=False, activation="UNREGISTERED_ADAPTER_CONTRACT", execution_surface="MOCK_TRANSPORT",
        locality="LOCALITY_NOT_VERIFIED", authority="NOT_ESTABLISHED")
    manifest = provider.manifest()
    assert manifest["timeout_semantics"] == "PER_OPERATION_INACTIVITY"
    assert manifest["document_budget"] == (frozen["packet"]["source_preparation"]["analysis_unit"]["budget"] if document else None)
    assert manifest["request_effects"] == "NOT_LIVE_VERIFIED"
    assert manifest["estimated_input_tokens"] == review_token_budget.estimate_input_tokens(original)
    assert manifest["total_tokens"] == "NOT_REPORTED_NOT_DERIVED"


def test_missing_metrics_are_not_invented():
    value = envelope(contract())
    for key in ("prompt_eval_count", "eval_count", "total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        value.pop(key)
    captured = adapter.parse_response(wire(value))
    assert captured.reported_metrics == {} and captured.result.usage == {}


@pytest.mark.parametrize("options", [dict(context_tokens=True), dict(context_tokens=0), dict(context_tokens=1.0),
    dict(context_tokens=adapter.MAX_CONTEXT_TOKENS + 1), dict(timeout_seconds=True), dict(timeout_seconds=0),
    dict(timeout_seconds=-1), dict(timeout_seconds=121), dict(timeout_seconds=float("nan")), dict(timeout_seconds=float("inf"))])
def test_configuration_bounds_reject_before_transport(options):
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        provider_for(**options)


def test_context_must_cover_estimate_and_output_and_match_frozen_document_budget():
    frozen = contract()
    required = review_token_budget.estimate_input_tokens(frozen["provider_request"]) + 2200
    assert adapter.build_plan(frozen, context_tokens=required, timeout_seconds=1)[2]["context_tokens"] == required
    with pytest.raises(ValueError, match="paper_ollama_configuration_invalid"):
        adapter.build_plan(frozen, context_tokens=required - 1, timeout_seconds=1)
    document = contract(document=True)
    with pytest.raises(ValueError, match="paper_ollama_configuration_invalid"):
        adapter.build_plan(document, context_tokens=CONTEXT + 1, timeout_seconds=1)
    changed = deepcopy(document)
    changed["provider_request"]["max_output_tokens"] = 2201
    changed["request_sha256"] = sha(preserved_json(changed["provider_request"]))
    changed["contract_sha256"] = sha(preserved_json({key: value for key, value in changed.items() if key != "contract_sha256"}))
    with pytest.raises(ValueError, match="paper_ollama_configuration_invalid"):
        adapter.build_plan(changed, context_tokens=CONTEXT, timeout_seconds=1)


@pytest.mark.parametrize("model", ["model:cloud", "model:large-cloud", "cloud/model:tiny", "model:CLOUD", "https://remote.invalid/model"])
def test_cloud_or_remote_model_intent_rejected_without_network(model):
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        provider_for(contract(model=model))


@pytest.mark.parametrize("version", [prepared_review.LEGACY_SCHEMA, prepared_review.SCHEMA_V2])
def test_new_profile_does_not_reinterpret_old_prepared_contracts(version):
    frozen = prepared_review.prepare(prior.packet(), provider_name="ollama", model="synthetic:tiny", schema_version=version)
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        provider_for(frozen)


def test_no_default_real_or_subclass_transport_and_no_client_created(monkeypatch):
    class OtherTransport(httpx.BaseTransport):
        def handle_request(self, request):
            pytest.fail("non-mock transport must not run")

    class MockSubclass(httpx.MockTransport):
        pass

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("no HTTP client during invalid construction"))
    for transport in (None, OtherTransport(), MockSubclass(lambda request: pytest.fail("not run"))):
        with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
            adapter.OllamaPreparedReviewProvider(contract(), context_tokens=CONTEXT, timeout_seconds=30, transport=transport)
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        adapter.OllamaPreparedReviewProvider(contract(), context_tokens=CONTEXT, timeout_seconds=30)


def test_frozen_request_and_detached_manifest_cannot_change_wire_or_claim_identity():
    provider, frozen, calls = provider_for()
    request = ProviderRequest(**frozen["provider_request"])
    frozen["provider_request"]["prompt"] = "mutated caller object"
    detached = provider.manifest()
    detached["endpoint"] = "https://synthetic-remote.invalid"
    with pytest.raises(FrozenInstanceError):
        provider.model = "changed"
    with pytest.raises(ValueError, match="^paper_ollama_adapter_mismatch$"):
        provider.generate_record(replace(request, prompt="mutated request"))
    assert calls == []
    record = provider.generate_record(request)
    assert len(calls) == 1 and record["validation"]["execution"]["manifest"]["endpoint"] == adapter.ENDPOINT


@pytest.mark.parametrize("status", [302, 401, 500])
def test_http_errors_and_redirect_never_follow_or_retry_or_echo(status, monkeypatch):
    clients = []
    original_client = httpx.Client

    def client(*args, **kwargs):
        clients.append(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    provider, frozen, calls = provider_for(handler=lambda request: httpx.Response(status,
        content=b"synthetic-private-error", headers={"location": "https://synthetic-remote.invalid/?secret=synthetic"}))
    with pytest.raises(ProviderError, match="^paper_ollama_transport_outcome_unknown$"):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1 and len(clients) == 1
    assert clients[0]["trust_env"] is False and clients[0]["follow_redirects"] is False
    assert "auth" not in clients[0]


@pytest.mark.parametrize("kind", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_exception_is_unknown_single_attempt_and_redacted(kind):
    def fail(request):
        raise kind("synthetic-private-error", request=request)
    provider, frozen, calls = provider_for(handler=fail)
    with pytest.raises(ProviderError, match="^paper_ollama_transport_outcome_unknown$"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1


@pytest.mark.parametrize("change", [dict(done=False), dict(done=1), dict(done_reason="unknown"), dict(done_reason=None),
    dict(model="model:cloud"), dict(model=""), dict(remote_host="synthetic-remote.invalid"), dict(remote_model="model"),
    dict(error="synthetic-private-error"), dict(prompt_eval_count=True), dict(eval_count=-1), dict(total_duration=1.5),
    dict(eval_duration=2**63), dict(prompt_eval_count=10**9 + 1), dict(message={"role": "tool", "content": "{}"}),
    dict(message={"role": "assistant", "content": "{}", "tool_calls": [{"function": {"name": "synthetic"}}]})])
def test_unconfirmed_envelope_does_not_yield_prepared_output(change):
    frozen = contract()
    value = envelope(frozen) | change
    provider, _, calls = provider_for(frozen, handler=lambda request: httpx.Response(200, content=wire(value)))
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1


def test_identified_length_completion_is_known_incomplete_even_with_partial_content():
    frozen = contract()
    value = envelope(frozen)
    value.update(done_reason="length", message=dict(role="assistant", content='{"partial":'))
    provider, _, calls = provider_for(frozen, handler=lambda request: httpx.Response(200, content=wire(value)))
    with pytest.raises(ProviderReportedFailure) as failure:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert failure.value.status == "incomplete" and len(calls) == 1


@pytest.mark.parametrize("raw", [b"\xff", b"{}\n{}", b"[]", b'{"partial":'])
def test_malformed_envelope_is_unknown_and_diagnostics_are_fixed(raw):
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        adapter.parse_response(raw)


@pytest.mark.parametrize("insert", ['"done": true,', '"model": "synthetic:alias",', '"unused": NaN,',
    '"unused": 1e999,', '"unused": "\\ud800",'])
def test_otherwise_complete_envelope_rejects_duplicates_and_nonfinite_or_surrogate_values(insert):
    raw = wire(envelope(contract())).decode("utf-8")
    raw = "{" + insert + raw[1:]
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        adapter.parse_response(raw.encode("utf-8"))


def test_duplicate_nested_message_key_is_rejected_in_otherwise_complete_envelope():
    raw = wire(envelope(contract())).replace(b'"role": "assistant"', b'"role": "assistant", "role": "assistant"')
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        adapter.parse_response(raw)


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', "[]", "synthetic-private-error"])
def test_invalid_completed_json_is_output_invalid(content):
    value = envelope(contract())
    value["message"]["content"] = content
    with pytest.raises(ProviderOutputError, match="^paper_ollama_output_invalid$"):
        adapter.parse_response(wire(value))


def test_valid_json_with_wrong_prepared_schema_does_not_make_a_record():
    frozen = contract()
    value = envelope(frozen)
    value["message"]["content"] = '{"unrelated": true}'
    provider, _, calls = provider_for(frozen, handler=lambda request: httpx.Response(200, content=wire(value)))
    with pytest.raises(ProviderOutputError, match="^paper_ollama_output_invalid$"):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1


def test_response_and_request_bounds_are_local_and_exact(monkeypatch):
    frozen = contract()
    raw = wire(envelope(frozen))
    monkeypatch.setattr(adapter, "MAX_RESPONSE_BYTES", len(raw))
    assert adapter.parse_response(raw).result.raw_text == envelope(frozen)["message"]["content"]
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        adapter.parse_response(raw + b" ")
    provider, _, calls = provider_for(frozen, handler=lambda request: httpx.Response(200, content=raw + b" "))
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1
    request_bytes = adapter.build_plan(frozen, context_tokens=CONTEXT, timeout_seconds=1)[1]
    monkeypatch.setattr(adapter, "MAX_REQUEST_BYTES", len(request_bytes))
    assert adapter.build_plan(frozen, context_tokens=CONTEXT, timeout_seconds=1)[1] == request_bytes
    monkeypatch.setattr(adapter, "MAX_REQUEST_BYTES", len(request_bytes) - 1)
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        adapter.build_plan(frozen, context_tokens=CONTEXT, timeout_seconds=1)


def test_inner_output_bytes_remain_bounded():
    value = envelope(contract())
    value["message"]["content"] = '{"text":"' + "x" * prepared_review.MAX_OUTPUT_BYTES + '"}'
    with pytest.raises(ProviderOutputError, match="^paper_ollama_output_invalid$"):
        adapter.parse_response(wire(value))


@pytest.mark.parametrize("part", ["envelope_content", "content_only", "envelope_metric", "marker_locality", "manifest_endpoint", "origin"])
def test_rehashed_one_side_or_provenance_tampering_fails_reconstruction(part):
    provider, frozen, _ = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    marker = record["validation"]["execution"]
    response = record["validation"]["prepared_execution"]["response"]
    if part in {"envelope_content", "envelope_metric"}:
        value = json.loads(marker["http_response_text"])
        if part == "envelope_content":
            draft = json.loads(value["message"]["content"])
            draft["contribution_summary"] = "Different synthetic claim"
            value["message"]["content"] = preserved_json(draft)
        else:
            value["total_duration"] += 1
        marker["http_response_text"] = wire(value).decode("utf-8")
        marker["http_response_sha256"] = sha(marker["http_response_text"])
    elif part == "content_only":
        # Even a fully rebuilt valid prepared result cannot contradict its envelope.
        result = adapter.parse_response(marker["http_response_text"].encode("utf-8")).result
        payload = deepcopy(result.payload)
        payload["contribution_summary"] = "Different synthetic claim"
        record = prepared_review.result(frozen, replace(result, payload=payload, raw_text=preserved_json(payload)))
        record["validation"]["execution"] = marker
        marker["response_sha256"] = sha(preserved_json(payload))
    elif part == "marker_locality":
        marker["locality"] = "LOCAL_VERIFIED"
    elif part == "manifest_endpoint":
        marker["manifest"]["endpoint"] = "https://synthetic-remote.invalid"
        marker["manifest_sha256"] = sha(preserved_json(marker["manifest"]))
    else:
        record["input_origin"] = "PROVIDER_RESPONSE"
    with pytest.raises(ValueError, match="^paper_ollama_transport_invalid$"):
        adapter.validate_transport(record, frozen["packet"])
    assert response["response_id"] is None


def test_source_range_corruption_is_rejected_before_plan_or_transport():
    frozen = contract()
    frozen["packet"]["source_preparation"]["selection"]["end"] += 1
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        provider_for(frozen)


@pytest.mark.parametrize("stage", ["handler", "iterator", "untrusted_provider_error"])
def test_arbitrary_io_exception_never_exposes_private_diagnostic_or_known_outcome(stage):
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            raise RuntimeError("SYNTHETIC_PRIVATE_DIAGNOSTIC")

    def fail(request):
        if stage == "iterator":
            return httpx.Response(200, stream=BrokenStream())
        if stage == "untrusted_provider_error":
            raise ProviderOutputError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
        raise RuntimeError("SYNTHETIC_PRIVATE_DIAGNOSTIC")

    provider, frozen, calls = provider_for(handler=fail)
    with pytest.raises(ProviderError, match="^paper_ollama_transport_outcome_unknown$") as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(failure.value) is ProviderError
    assert failure.value.__suppress_context__ and failure.value.__cause__ is None
    assert len(calls) == 1


def test_base_exception_cancellation_is_not_reclassified():
    class Cancelled(BaseException):
        pass

    def cancel(request):
        raise Cancelled()

    provider, frozen, calls = provider_for(handler=cancel)
    with pytest.raises(Cancelled):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1


@pytest.mark.parametrize("model", ["sk-not-a-real-model", "XAI-synthetic-placeholder", "Ya29.synthetic-placeholder"])
def test_credential_prefix_is_not_accepted_as_requested_or_reported_model(model):
    with pytest.raises(ValueError, match="^paper_ollama_configuration_invalid$"):
        provider_for(contract(model=model))
    value = envelope(contract())
    value["model"] = model
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        adapter.parse_response(wire(value))


def test_cached_input_count_is_preserved_separately_without_deriving_totals():
    frozen = contract()
    value = envelope(frozen)
    value["prompt_eval_cached_count"] = 2
    raw = wire(value)
    captured = adapter.parse_response(raw)
    assert captured.reported_metrics["prompt_eval_cached_count"] == 2
    assert captured.result.usage == {"input_tokens": 4, "output_tokens": 7}
    record = adapter.transport_record(frozen, raw, adapter.build_plan(frozen, context_tokens=CONTEXT, timeout_seconds=30)[2])
    marker = record["validation"]["execution"]
    assert marker["reported_metrics"]["prompt_eval_cached_count"] == 2
    assert marker["http_response_text"] == raw.decode("utf-8")
    assert marker["manifest"]["cached_input_semantics"] == "REPORTED_SEPARATELY_NOT_ADDED_TO_INPUT"
    adapter.validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("cached", [True, -1, 1.5, 10**9 + 1])
def test_cached_input_count_remains_a_bounded_reported_integer(cached):
    value = envelope(contract())
    value["prompt_eval_cached_count"] = cached
    with pytest.raises(ProviderError, match="^paper_ollama_response_outcome_unknown$"):
        adapter.parse_response(wire(value))

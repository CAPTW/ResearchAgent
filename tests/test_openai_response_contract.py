"""Responses protocol tests use a local httpx transport, never credentials/network."""
from copy import deepcopy
import json

import httpx
import pytest

from app.services.providers.base import ProviderError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest
from app.services.providers.openai_provider import OpenAIProvider
from app.services import journal_execution, research_plan
from app.repositories import journal_candidates, journal_executions
from tests import test_journal_execution as prior

source = prior.source
completed = prior.completed


def envelope(text='{"ok":true}', **changes):
    return dict(id="resp_offline", model="reported-offline-model", status="completed", error=None, incomplete_details=None,
                output=[dict(type="message", role="assistant", status="completed", content=[dict(type="output_text", text=text)])],
                usage={"input_tokens": 5, "output_tokens": 4, "total_tokens": 9}) | changes


def adapter(value=None, *, raw=None, status=200, calls=None):
    def handle(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(status, content=raw) if raw is not None else httpx.Response(status, json=value)
    return OpenAIProvider(api_key="offline-test-key", model="requested-offline-model", api_base="https://offline.invalid/v1",
                          transport=httpx.MockTransport(handle))


def generate(provider):
    return provider.generate_json(ProviderRequest(schema_name="offline", schema={"type": "object"}, instructions="Only JSON", prompt="{}"))


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled", "queued", "in_progress", "unknown", None])
def test_terminal_status_is_required_even_when_text_is_valid_json(status):
    with pytest.raises(ProviderError):
        generate(adapter(envelope(status=status)))


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed", "cancelled"])
@pytest.mark.parametrize("change", [
    "missing_id", "missing_model", "status_only", {"id": None}, {"model": 2},
    {"id": ""}, {"model": " \t"}, {"id": "r" * 201}, {"model": "m" * 201},
    {"id": "resp\nunconfirmed"}, {"id": " resp"}, {"model": "reported\x7fmodel"}, {"model": "model "},
])
def test_terminal_response_without_valid_identity_has_unknown_outcome(status, change):
    value = envelope(status=status)
    if change == "status_only":
        value = {"status": status}
    elif isinstance(change, str):
        del value[change.removeprefix("missing_")]
    else:
        value.update(change)
    calls = []
    with pytest.raises(ProviderError) as error:
        generate(adapter(value, calls=calls))
    # The specialized exceptions are known failures to the execution service.
    assert type(error.value) is ProviderError
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled"])
def test_identified_terminal_failure_remains_known_without_parseable_output(status):
    value = envelope(status=status, output=[], usage=None)
    value["error" if status == "failed" else "incomplete_details"] = (
        {"code": "server_error", "message": "sensitive diagnostic"} if status == "failed"
        else {"reason": "max_output_tokens"} if status == "incomplete" else None)
    with pytest.raises(ProviderReportedFailure) as error:
        generate(adapter(value))
    assert error.value.status == status
    assert "sensitive diagnostic" not in str(error.value)


@pytest.mark.parametrize("identity,model", [("r" * 200, "m" * 200), ("resp-한글", "reported model e\u0301")])
def test_valid_reported_identity_is_preserved_without_normalization(identity, model):
    result = generate(adapter(envelope(id=identity, model=model)))
    assert result.response_id == identity and result.model == model


@pytest.mark.parametrize("content", [dict(type="refusal", refusal="private diagnostic"),
                                     dict(type="output_text", text="invalid private output")])
def test_invalid_identity_takes_precedence_over_known_output_failure(content):
    value = envelope(model="unconfirmed\nmodel")
    value["output"][0]["content"] = [content]
    calls = []
    with pytest.raises(ProviderError) as error:
        generate(adapter(value, calls=calls))
    assert type(error.value) is ProviderError
    assert "private" not in str(error.value) and "unconfirmed" not in str(error.value)
    assert len(calls) == 1


def test_refusal_in_a_later_message_cannot_be_hidden_by_earlier_valid_json():
    value = envelope()
    value["output"].append(dict(type="message", role="assistant", status="completed",
                                content=[dict(type="refusal", refusal="sensitive input echoed here")]))
    with pytest.raises(ProviderRefusalError) as error:
        generate(adapter(value))
    assert "sensitive input" not in str(error.value)


@pytest.mark.parametrize("text", ['{"ok":true,"ok":false}', '{"value":NaN}', '{"value":1e999}', '[]', '"text"', '{"x":"\\ud800"}'])
def test_malformed_structured_object_is_not_wrapped_or_silently_rewritten(text):
    with pytest.raises(ProviderError):
        generate(adapter(envelope(text)))


@pytest.mark.parametrize("change", [
    {"model": None}, {"model": 2}, {"id": None}, {"usage": []}, {"output": {}},
    {"error": {"message": "sensitive diagnostic"}}, {"incomplete_details": {"reason": "max_output_tokens"}},
])
def test_invalid_envelope_does_not_fabricate_reported_identity_or_success(change):
    with pytest.raises(ProviderError) as error:
        generate(adapter(envelope(**change)))
    assert "sensitive diagnostic" not in str(error.value)


@pytest.mark.parametrize("change", [{"status": "incomplete"}, {"role": "user"}, {"content": [{"type": "output_text", "text": 4}]}, {"content": []}])
def test_output_message_shape_must_be_complete_assistant_text(change):
    value = envelope()
    value["output"][0].update(change)
    with pytest.raises(ProviderError):
        generate(adapter(value))


def test_all_text_parts_are_checked_and_exact_whitespace_is_preserved():
    text = ' \n {"ok": true} \n'
    value = envelope()
    value["output"].insert(0, dict(type="reasoning", summary=[]))
    value["output"][1]["content"] = [dict(type="output_text", text=text[:8]), dict(type="output_text", text=text[8:])]
    result = generate(adapter(value))
    assert result.raw_text == text and result.payload == {"ok": True}
    assert result.model == "reported-offline-model" and result.response_id == "resp_offline"
    assert result.usage == value["usage"]
    value["output"].append(deepcopy(value["output"][1]))
    with pytest.raises(ProviderError):
        generate(adapter(value))


@pytest.mark.parametrize("raw,status", [(b'{"status":"completed","status":"failed"}', 200), (b'not json sensitive diagnostic', 200), (b'secret echo', 429)])
def test_protocol_errors_are_bounded_without_echoing_response_body(raw, status):
    with pytest.raises(ProviderError) as error:
        generate(adapter(raw=raw, status=status))
    assert "sensitive diagnostic" not in str(error.value) and "secret echo" not in str(error.value)


def test_oversized_body_stops_reading_and_closes_without_another_request():
    class Stream(httpx.SyncByteStream):
        chunks = 0
        closed = False

        def __iter__(self):
            for _ in range(128):
                self.chunks += 1
                yield b" " * 65536

        def close(self):
            self.closed = True
    stream = Stream()
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)
    provider = OpenAIProvider(api_key="offline-test-key", transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError, match="size limit"):
        generate(provider)
    assert len(calls) == 1 and stream.closed and stream.chunks == 62


def test_missing_usage_is_unknown_not_invented_as_zero():
    assert generate(adapter(envelope(usage=None))).usage == {}


def test_completed_but_invalid_output_records_known_failure_and_no_candidate(completed):
    value = prior.prepare(completed, provider_name="openai", model="requested-offline-model")[0]
    failed = journal_execution.execute(prior.claim(value), provider=adapter(envelope('{"title":"first","title":"second"}')),
                                      model="requested-offline-model", clock=lambda: prior.NOW)
    assert failed["current"]["state"] == "FAILED" and not failed["current"]["outcome_unknown"]
    assert failed["current"]["reason"] == "output_invalid" and failed["current"]["result"] is None
    assert journal_candidates.listing(*prior.scope(value)[:2])["items"] == []


@pytest.mark.parametrize("status,reason,unknown", [
    ("incomplete", "provider_incomplete", False), ("failed", "provider_failed", False),
    ("cancelled", "provider_cancelled", False), ("in_progress", "provider_error", True),
])
def test_journal_cannot_promote_noncompleted_json_and_explicit_retry_keeps_attempt_accounting(completed, status, reason, unknown):
    value = prior.prepare(completed, provider_name="openai", model="requested-offline-model")[0]
    payload = prior.imports.output(value["contract"]["packet"])
    calls = []
    provider = adapter(envelope(json.dumps(payload), status=status), calls=calls)
    claimed = prior.claim(value)
    failed = journal_execution.execute(claimed, provider=provider, model="requested-offline-model", clock=lambda: prior.NOW)
    assert len(calls) == 1
    assert failed["current"]["state"] == ("INTERRUPTED" if unknown else "FAILED")
    assert failed["current"]["reason"] == reason and failed["current"]["outcome_unknown"] is unknown
    assert failed["current"]["result"] is None
    with pytest.raises(ValueError, match="not_succeeded"):
        journal_candidates.save_execution(*prior.scope(value), "0" * 64, "reviewer")
    assert journal_execution.execute(claimed, provider=provider, model="requested-offline-model", clock=lambda: prior.NOW) == failed
    assert len(calls) == 1  # no automatic retry or repeat of an old claim
    if unknown:
        with pytest.raises(ValueError, match="unknown_outcome"):
            journal_executions.claim(*prior.scope(value), expected_attempt=1, now=prior.NOW)
    retry = journal_executions.claim(*prior.scope(value), expected_attempt=1, acknowledge_unknown=unknown, now=prior.NOW)
    assert retry.attempt == 2
    success = journal_execution.execute(retry, provider=adapter(envelope(json.dumps(payload)), calls=calls),
                                        model="requested-offline-model", clock=lambda: prior.NOW)
    assert success["current"]["state"] == "SUCCEEDED" and len(calls) == 2
    assert success["events"][:3] == failed["events"]
    assert success["contract"] == value["contract"]
    assert success["current"]["result"]["validation"]["independent_live_proof"] == "NOT_VERIFIED"


def test_research_plan_falls_back_on_incomplete_response_without_echoing_raw_error(isolated_run_repo, monkeypatch):
    provider = adapter(envelope(status="incomplete", incomplete_details={"reason": "max_output_tokens", "secret": "not-for-logs"}))
    monkeypatch.setattr(research_plan, "get_active_provider", lambda: provider)
    result = research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    assert result.generator.mode == "heuristic"
    assert "not-for-logs" not in result.model_dump_json()

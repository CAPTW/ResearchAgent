"""Messages protocol tests are local byte fixtures and MockTransport only."""
import importlib
import json

import httpx
import pytest

from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure


def module():
    return importlib.import_module("app.services.providers.anthropic_messages")


def envelope(text="{}", **changes):
    return dict(id="msg_offline", type="message", role="assistant", model="reported-offline-model",
                content=[dict(type="text", text=text)], stop_reason="end_turn", stop_sequence=None,
                usage=dict(input_tokens=5, output_tokens=4, cache_creation_input_tokens=2, cache_read_input_tokens=3)) | changes


def test_messages_parser_preserves_exact_assistant_json_and_cache_usage():
    parser = module()
    raw = '{"text": "e\u0301 한글🙂"} \n'
    result = parser.parse_response(json.dumps(envelope(raw), ensure_ascii=False).encode("utf-8"))
    assert result.provider_name == "anthropic" and result.raw_text == raw
    assert result.payload == dict(text="e\u0301 한글🙂")
    assert result.usage == dict(input_tokens=5, output_tokens=4, cache_creation_input_tokens=2,
                               cache_read_input_tokens=3)


@pytest.mark.parametrize("thinking", [
    [dict(type="thinking", thinking="", signature="opaque-signature")],
    [dict(type="thinking", thinking="synthetic display text, not scientific evidence", signature="opaque-signature")],
    [dict(type="redacted_thinking", data="opaque-redacted-data")],
    [dict(type="thinking", thinking="", signature="opaque-signature"),
     dict(type="redacted_thinking", data="opaque-redacted-data")],
])
def test_opus55_recognized_thinking_is_excluded_from_exact_final_json(thinking):
    raw = '{"text": " e\u0301 한글🙂 "} \n'
    body = envelope(model="claude-opus-5-5", content=thinking + [dict(type="text", text=raw)])
    result = module().parse_response(preserved_json(body).encode("utf-8"), max_output_bytes=len(raw.encode("utf-8")))
    assert result.raw_text == raw and result.payload == {"text": " e\u0301 한글🙂 "}
    assert result.model == "claude-opus-5-5" and result.usage == body["usage"]
    assert "opaque" not in result.raw_text and "scientific evidence" not in result.raw_text


@pytest.mark.parametrize("block", [
    dict(type="thinking", signature="opaque"), dict(type="thinking", thinking=""),
    dict(type="thinking", thinking=None, signature="opaque"), dict(type="thinking", thinking="", signature=1),
    dict(type="thinking", thinking="", signature="opaque", text="{}"),
    dict(type="redacted_thinking"), dict(type="redacted_thinking", data=None),
    dict(type="redacted_thinking", data="opaque", text="{}"),
    dict(type="tool_use", name="undeclared", input={}), dict(type="server_tool_use", name="web_search", input={}),
    dict(type="future_thinking", thinking="", signature="opaque"), dict(type="error", error="synthetic"),
])
def test_opus55_malformed_or_unknown_blocks_do_not_hide_behind_valid_text(block):
    body = envelope(model="claude-opus-5-5", content=[block, dict(type="text", text="{}")])
    with pytest.raises(ProviderOutputError, match="anthropic_output_invalid"):
        module().parse_response(preserved_json(body).encode("utf-8"))


def test_opus55_thinking_does_not_supply_missing_json_or_expand_output_budget():
    thinking = dict(type="thinking", thinking='{"pretend_result":true}', signature="opaque")
    for content in ([thinking], [thinking, dict(type="text", text="")], [thinking, dict(type="text", text="not JSON")]):
        with pytest.raises(ProviderOutputError):
            module().parse_response(preserved_json(envelope(content=content)).encode("utf-8"))
    with pytest.raises(ProviderOutputError):
        module().parse_response(preserved_json(envelope(content=[thinking, dict(type="text", text="{} ")])).encode("utf-8"), max_output_bytes=2)
    thinking["thinking"] = "x" * module().MAX_RESPONSE_BYTES
    with pytest.raises(ProviderError, match="anthropic_response_outcome_unknown"):
        module().parse_response(preserved_json(envelope(content=[thinking, dict(type="text", text="{}")])).encode("utf-8"))


@pytest.mark.parametrize("stop,category", [("refusal", "refusal"), ("max_tokens", "incomplete"),
    ("model_context_window_exceeded", "incomplete"), ("tool_use", "unknown"), ("pause_turn", "unknown"), ("unrecognized", "unknown")])
def test_opus55_thinking_never_converts_incomplete_or_unknown_stop_to_success(stop, category):
    body = envelope(model="claude-opus-5-5", stop_reason=stop,
        content=[dict(type="thinking", thinking="", signature="opaque"), dict(type="text", text="{}")])
    with pytest.raises(ProviderError) as error:
        module().parse_response(preserved_json(body).encode("utf-8"))
    if category == "refusal":
        assert isinstance(error.value, ProviderRefusalError)
    elif category == "incomplete":
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == category
    else:
        assert type(error.value) is ProviderError and "unknown" in str(error.value)


@pytest.mark.parametrize("stage", ["journal", "paper"])
def test_native_wire_omits_unsupported_temperature_without_rewriting_frozen_input(stage):
    from app.domain.journal_exchange import sha
    if stage == "journal":
        from tests.test_anthropic_journal import contract, modules
        frozen = contract()
        request, body, manifest = modules()[1].build_wire(frozen, 30)
    else:
        from tests.test_anthropic_prepared_review import contract, module as paper_module
        frozen = contract()
        request, body, manifest = paper_module().build_plan(frozen, timeout_seconds=30)
    assert "temperature" not in json.loads(body)
    assert manifest["temperature_policy"] == "OMITTED_PROVIDER_DEFAULT"
    assert manifest["temperature_enforced"] is False
    assert frozen["provider_request"]["temperature"] == 0.2
    assert request == preserved_json(frozen["provider_request"])
    assert manifest["request_sha256"] == sha(request)


def test_compiler_retains_original_schema_and_describes_only_unsupported_bounds():
    parser = module()
    schema = dict(type="object", properties={
        "minLength": dict(type="string", minLength=1, maxLength=200, pattern=r"\S", description="Summary"),
        "ratio": dict(type="number", minimum=0, maximum=1),
        "list": dict(type="array", items=dict(type="string"), minItems=1, maxItems=30),
        "many": dict(type="array", items=dict(type="integer"), minItems=2)},
        required=["minLength", "ratio", "list", "many"], additionalProperties=False)
    before = preserved_json(schema)
    result = parser.compile_schema(schema)
    assert preserved_json(schema) == before
    assert result["properties"]["minLength"] == dict(type="string", pattern=r"\S",
        description='Summary\nLocal validation constraints: {"maxLength":200,"minLength":1}')
    assert result["properties"]["ratio"] == dict(type="number", description='Local validation constraints: {"maximum":1,"minimum":0}')
    assert result["properties"]["list"]["minItems"] == 1
    assert "maxItems" not in result["properties"]["list"] and "minItems" not in result["properties"]["many"]
    assert "minLength" in result["required"] and result["additionalProperties"] is False


def test_compiler_supports_actual_journal_and_paper_schemas_without_mutation():
    from app.domain import journal_exchange, prepared_review
    parser = module()
    for schema in (journal_exchange.Proposal.model_json_schema(), prepared_review.PreparedReviewDraftV3.model_json_schema()):
        before = preserved_json(schema)
        result = parser.compile_schema(schema)
        assert preserved_json(schema) == before and result != schema
        assert result["additionalProperties"] is False
    paper = parser.compile_schema(prepared_review.PreparedReviewDraftV3.model_json_schema())
    assert paper["properties"]["contribution_summary"]["pattern"] == r"\S"
    assert paper["properties"]["cited_evidence_span_ids"]["minItems"] == 1
    assert "maximum" not in paper["properties"]["confidence"]


def test_v2_describes_regex_without_changing_original_schema_or_property_names():
    from app.domain import prepared_review
    parser = module()
    schema = prepared_review.PreparedReviewDraftV3.model_json_schema()
    original = preserved_json(schema)
    compiled = parser.compile_schema(schema, compiler="ANTHROPIC_JSON_SCHEMA_V2")
    assert preserved_json(schema) == original
    assert compiled["required"] == schema["required"]
    assert compiled["properties"].keys() == schema["properties"].keys()
    assert "pattern" not in compiled["properties"]["contribution_summary"]
    assert '\\S' in compiled["properties"]["contribution_summary"]["description"]
    assert "pattern" not in compiled["properties"]["limitations"]["items"]
    named = dict(type="object", properties={"pattern": dict(type="string", pattern="^x$")}, required=["pattern"])
    assert "pattern" in parser.compile_schema(named, compiler="ANTHROPIC_JSON_SCHEMA_V2")["properties"]
    assert parser.compile_schema(schema)["properties"]["contribution_summary"]["pattern"] == r"\S"


@pytest.mark.parametrize("status,error,expected", [
    (400, {"type": "invalid_request_error", "message": "Schema is too complex."}, "SCHEMA_COMPLEXITY"),
    (400, {"type": "invalid_request_error", "message": "Schema is too complex for compilation."}, "SCHEMA_COMPLEXITY"),
    (400, {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}, "INSUFFICIENT_CREDITS"),
    (402, {"type": "billing_error", "message": "private details"}, "BILLING_ERROR"),
    (401, {"type": "authentication_error", "message": "private details"}, "AUTHENTICATION_ERROR"),
    (400, {"type": "invalid_request_error", "message": "some different problem"}, None),
    (500, {"type": "invalid_request_error", "message": "Schema is too complex."}, None),
    (400, {"type": "unknown", "message": "Schema is too complex."}, None),
])
def test_rejection_diagnostic_uses_fixed_codes_not_arbitrary_provider_text(status, error, expected):
    raw = preserved_json(dict(type="error", error=error)).encode()
    assert module().rejection_diagnostic(status, raw) == expected
    assert module().rejection_diagnostic(status, b"not JSON") is None


@pytest.mark.parametrize("schema", [
    {"type": "object", "additionalProperties": True}, {"type": "string", "unknown": True},
    {"$ref": "https://private.invalid/schema"}, {"$ref": "#/$defs/missing"},
    {"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}},
    {"allOf": [{"$ref": "#/$defs/a"}], "$defs": {"a": {"type": "string"}}},
    {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    {"type": "array", "minItems": True}, {"type": "string", "enum": [{"unsafe": True}]},
])
def test_unsupported_schema_shapes_fail_locally(schema):
    with pytest.raises(ValueError, match="anthropic_schema_invalid"):
        module().compile_schema(schema)


@pytest.mark.parametrize("counts", [{}, {"input_tokens": 5}, {"output_tokens": 4, "cache_read_input_tokens": 0},
                                    {"input_tokens": 5, "output_tokens": 4, "total_tokens": 99}])
def test_absent_usage_counts_are_not_zero_or_derived_totals(counts):
    result = module().parse_response(preserved_json(envelope(usage=counts)).encode("utf-8"))
    assert result.usage == {key: value for key, value in counts.items() if key != "total_tokens"}


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "2", None, 10**9 + 1])
def test_invalid_known_usage_is_not_silently_dropped(key, value):
    body = envelope()
    body["usage"][key] = value
    with pytest.raises(ProviderOutputError):
        module().parse_response(preserved_json(body).encode("utf-8"))


@pytest.mark.parametrize("stop,kind", [("max_tokens", "incomplete"), ("model_context_window_exceeded", "incomplete"), ("refusal", "refusal")])
def test_identified_terminal_failure_is_not_a_success_or_unknown(stop, kind):
    body = envelope("private incomplete response", stop_reason=stop)
    with pytest.raises(ProviderError) as error:
        module().parse_response(preserved_json(body).encode("utf-8"))
    assert "private" not in str(error.value)
    if kind == "refusal":
        assert isinstance(error.value, ProviderRefusalError)
    else:
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == kind


@pytest.mark.parametrize("changes", [dict(id=None), dict(id=" msg "), dict(model=""), dict(role="user"),
    dict(type="error"), dict(stop_reason=None), dict(stop_reason=[]), dict(stop_reason="tool_use"),
    dict(stop_reason="pause_turn"), dict(stop_reason="stop_sequence"), dict(stop_sequence="unexpected"),
    dict(error={"message": "private"}), dict(stop_details={"type": "refusal"}), dict(id=None, stop_reason="refusal")])
def test_missing_identity_or_uncertain_completion_is_unknown(changes):
    with pytest.raises(ProviderError, match="unknown") as error:
        module().parse_response(preserved_json(envelope(**changes)).encode("utf-8"))
    assert type(error.value) is ProviderError and "private" not in str(error.value)


@pytest.mark.parametrize("raw", [b'not json', b'[]', b'{"id":1,"id":2}', b'{"value":NaN}',
    b'{"value":1e309}', b'{"bad":"\\ud800"}', b'{"bad":"\xff"}', b'{}' + b' ' * 4_000_000],
    ids=["not-json", "array", "duplicate", "nonfinite", "overflow", "surrogate", "invalid-utf8", "oversized"])
def test_invalid_or_oversized_utf8_envelopes_are_unknown(raw):
    with pytest.raises(ProviderError, match="unknown") as error:
        module().parse_response(raw)
    assert type(error.value) is ProviderError


@pytest.mark.parametrize("content", [None, [], ["text"], [dict(type="tool_use", id="tool", input={})],
    [dict(type="thinking", thinking="private", signature="signature")], [dict(type="text", text=None)],
    [dict(type="text", text="{}", citations=[{"cited_text": "unsupported"}])],
    [dict(type="text", text="{}", extra="unsupported")]])
def test_completed_invalid_content_blocks_are_output_errors(content):
    with pytest.raises(ProviderOutputError):
        module().parse_response(preserved_json(envelope(content=content)).encode("utf-8"))


@pytest.mark.parametrize("text", ["not JSON", "[]", '{"dup":1,"dup":2}', '{"n":NaN}', '{"n":1e309}', '{"s":"\\ud800"}'])
def test_completed_text_requires_strict_json(text):
    with pytest.raises(ProviderOutputError):
        module().parse_response(preserved_json(envelope(text)).encode("utf-8"))


def test_multiple_text_blocks_preserve_order_and_ignore_no_token_count():
    body = envelope(content=[dict(type="text", text='{"text":'), dict(type="text", text='"e\u0301"}\n', citations=None)])
    result = module().parse_response(preserved_json(body).encode("utf-8"))
    assert result.raw_text == '{"text":"e\u0301"}\n'


@pytest.mark.parametrize("extra", [0, 1])
def test_exact_unicode_output_limit_is_measured_in_utf8_bytes(extra):
    raw = '{"text":"한글🙂"}'
    raw += " " * (512_000 + extra - len(raw.encode("utf-8")))
    encoded = preserved_json(envelope(raw)).encode("utf-8")
    if extra:
        with pytest.raises(ProviderOutputError):
            module().parse_response(encoded)
    else:
        assert module().parse_response(encoded).raw_text == raw


def test_stream_decode_bound_closes_owned_response_without_finishing_body():
    class Stream(httpx.SyncByteStream):
        reads = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                self.reads += 1
                yield b"x" * 65_536

        def close(self):
            self.closed = True

    stream = Stream()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    with pytest.raises(ProviderError, match="unknown"):
        module().send(b"{}", api_key="synthetic-key", timeout_seconds=30,
            transport=httpx.MockTransport(handler), max_output_bytes=512_000)
    assert len(calls) == 1 and stream.closed and stream.reads < 100

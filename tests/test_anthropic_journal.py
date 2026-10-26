"""Native Messages Journal: exact local MockTransport fixtures, no account calls."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import json

import httpx
import pytest

from app.domain import journal_execution
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderUnavailableError
from tests.test_anthropic_messages import envelope
from tests.test_journal_review_packet import frozen_job, proposal, NOW


def modules():
    return (importlib.import_module("app.services.providers.anthropic_journal"),
            importlib.import_module("app.services.providers.anthropic_journal_wire"))


def contract(*, selected=True, **changes):
    job = frozen_job(selected=selected)
    spec = journal_execution.Prepare(**(dict(request_id="anthropic-journal", expected_input_digest=job["input_digest"],
        provider_name="anthropic", model="synthetic-messages-model") | changes))
    return journal_execution.prepare(job, spec, "owner", NOW)


def provider_at(frozen=None, *, value=None, raw=None, status=200, handler=None, **settings):
    adapter, _ = modules()
    frozen = frozen or contract()
    calls = []

    def handle(request):
        calls.append(request)
        if handler is not None:
            return handler(request)
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=value if value is not None else envelope(preserved_json(proposal(frozen["packet"]))))

    provider = adapter.AnthropicJournalProvider(frozen, **(dict(api_key="synthetic-key", timeout_seconds=30,
        transport=httpx.MockTransport(handle)) | settings))
    return provider, calls, frozen


def test_opus55_journal_persists_final_text_only_with_unchanged_request_and_binding():
    frozen = contract(model="claude-opus-5-5")
    expected_wire = modules()[1].build_wire(frozen, 30)
    raw = json.dumps(proposal(frozen["packet"]), ensure_ascii=False, indent=2) + " \n"
    value = envelope(model="claude-opus-5-5", content=[
        dict(type="thinking", thinking="synthetic display text", signature="opaque-signature"),
        dict(type="redacted_thinking", data="opaque-data"), dict(type="text", text=raw)])
    provider, calls, _ = provider_at(frozen, value=value)
    binding = provider.binding
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    saved = journal_execution.result(frozen, response)
    assert saved["raw_json"] == raw and saved["proposal"] == json.loads(raw)
    assert "opaque-signature" not in preserved_json(saved) and "synthetic display text" not in preserved_json(saved)
    assert provider.binding == binding and provider.manifest() == expected_wire[2]
    assert len(calls) == 1 and calls[0].content == expected_wire[1]
    body = json.loads(calls[0].content)
    assert body["model"] == "claude-opus-5-5" and "thinking" not in body
    assert body["max_tokens"] == frozen["provider_request"]["max_output_tokens"]
    assert saved["validation"]["independent_live_proof"] == "NOT_VERIFIED"


@pytest.mark.parametrize("selected", [False, True])
def test_native_messages_preserves_frozen_request_and_exact_unicode_output(selected, monkeypatch):
    _, wire = modules()
    messages = importlib.import_module("app.services.providers.anthropic_messages")
    frozen = contract(selected=selected)
    original = deepcopy(frozen)
    output = proposal(frozen["packet"])
    output["title"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    raw = json.dumps(output, ensure_ascii=False, indent=3) + " \n"
    provider, calls, _ = provider_at(frozen, value=envelope(raw))
    manifest = provider.manifest()
    provider.manifest()["tools"].append({"type": "web_search"})
    frozen["provider_request"]["prompt"] = "caller changed"
    original_client = httpx.Client
    clients = []

    def client(**kwargs):
        clients.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(messages.httpx, "Client", client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-key-not-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ignored.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    response = provider.generate_json(ProviderRequest(**original["provider_request"]))
    assert len(calls) == 1 and calls[0].method == "POST" and str(calls[0].url) == "https://api.anthropic.com/v1/messages"
    assert calls[0].headers["x-api-key"] == "synthetic-key" and "authorization" not in calls[0].headers
    assert calls[0].headers["anthropic-version"] == "2023-06-01"
    assert clients[0]["follow_redirects"] is False and clients[0]["trust_env"] is False
    body = json.loads(calls[0].content)
    request = original["provider_request"]
    assert body["system"] == request["instructions"] and body["messages"] == [dict(role="user", content=request["prompt"])]
    assert body["model"] == original["specification"]["model"] and body["max_tokens"] == request["max_output_tokens"]
    assert request["temperature"] == 0.2 and "temperature" not in body and body["stream"] is False and body["tools"] == []
    assert set(body) == {"model", "system", "messages", "max_tokens", "stream", "tools", "output_config"}
    assert body["output_config"]["format"] == dict(type="json_schema", schema=messages.compile_schema(request["schema"]))
    assert manifest["schema_version"] == wire.MANIFEST_SCHEMA == "ANTHROPIC_JOURNAL_WIRE_V1"
    assert manifest["contract_digest"] == digest(original) and manifest["request_sha256"] == sha(preserved_json(request))
    assert manifest["body_sha256"] == sha(calls[0].content.decode("utf-8")) and manifest["body_bytes"] == len(calls[0].content)
    assert manifest["local_schema_sha256"] == sha(preserved_json(request["schema"]))
    assert manifest["wire_schema_sha256"] == sha(preserved_json(body["output_config"]["format"]["schema"]))
    assert manifest["wire_schema_sha256"] != manifest["local_schema_sha256"]
    assert manifest["schema_compiler"] == "ANTHROPIC_JSON_SCHEMA_V1"
    assert manifest["api_version"] == "2023-06-01" and manifest["schema_constraints"] == "LOCAL_ORIGINAL_VALIDATION"
    assert manifest["temperature_policy"] == "OMITTED_PROVIDER_DEFAULT"
    assert not {"store", "truncation", "truncation_enforced"} & set(manifest)
    assert manifest["usage_input_semantics"] == "NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE"
    assert manifest["total_tokens"] == "NOT_REPORTED_NOT_DERIVED"
    for key in ("automatic_retry", "redirects", "environment_proxy", "output_token_limit_enforced", "temperature_enforced", "cost_limit_enforced", "raw_envelope_retained"):
        assert manifest[key] is False
    assert provider.manifest() == manifest
    assert provider.binding.surface_id == "api" and provider.binding.adapter_id == "xrml-anthropic-frozen-journal"
    assert provider.binding.command_shape_sha256 == digest(manifest)
    assert response.provider_name == "anthropic" and response.raw_text == raw
    assert response.model == "reported-offline-model" and response.response_id == "msg_offline"
    assert response.usage == envelope()["usage"] and "total_tokens" not in response.usage
    saved = journal_execution.result(original, response)
    assert saved["raw_json"] == raw and saved["proposal"] == output
    assert saved["validation"]["independent_live_proof"] == "NOT_VERIFIED"
    assert "synthetic-key" not in repr(provider) + str(provider.describe_status()) + str(manifest)
    assert provider.describe_status()["live_call_enabled"] is False
    if selected:
        assert "e\u0301 한글🙂" in body["messages"][0]["content"]
    with pytest.raises(FrozenInstanceError):
        provider.model = "changed"


@pytest.mark.parametrize("change", ["prompt", "schema", "instructions", "metadata", "max_output_tokens", "temperature", "schema_name"])
def test_mutated_request_is_denied_before_transport(change):
    provider, calls, frozen = provider_at()
    fields = deepcopy(frozen["provider_request"])
    fields[change] = {} if change in {"schema", "metadata"} else 256 if change == "max_output_tokens" else 1 if change == "temperature" else "changed"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(ProviderRequest(**fields))
    assert calls == []


@pytest.mark.parametrize("change", ["provider", "model", "schema", "packet-schema", "prompt", "execution", "packet", "tokens", "output-class", "instructions"])
def test_invalid_frozen_contract_cannot_compile_wire(change):
    _, wire = modules()
    frozen = contract()
    if change == "provider":
        frozen["specification"]["provider_name"] = "openai"
    elif change == "model":
        frozen["specification"]["model"] = " model "
    elif change == "tokens":
        frozen["provider_request"]["max_output_tokens"] += 1
    elif change == "execution":
        frozen["execution_id"] = "jexec_" + "0" * 64
    elif change == "packet":
        frozen["packet"]["packet_id"] = "jpacket_" + "0" * 64
    elif change == "packet-schema":
        frozen["packet"]["schema_version"] = "unsupported"
    elif change == "output-class":
        frozen["provider_request"]["schema_name"] = "PAPER_REVIEW"
    else:
        frozen["provider_request"][change] = {} if change == "schema" else "changed"
    with pytest.raises(ValueError, match="request_invalid"):
        wire.build_wire(frozen, 30)


@pytest.mark.parametrize("change", ["actor", "model", "binding", "unicode"])
def test_binding_never_retargets_another_actor_model_or_exact_input(change):
    provider, calls, frozen = provider_at()
    model, binding = provider.model, provider.binding
    if change == "actor":
        frozen["actor_id"] = "another"
    elif change == "model":
        model = "other"
    elif change == "binding":
        binding = binding.model_copy(update={"adapter_id": "other"})
    else:
        frozen["packet"]["topic_label"] = "e\u0301"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.validate_journal_binding(frozen, model, binding)
    assert calls == []


@pytest.mark.parametrize("timeout", [0, -1, 121, True, "30", float("nan"), float("inf")])
def test_invalid_timeout_is_local_error(timeout):
    with pytest.raises(ValueError, match="configuration_invalid"):
        provider_at(timeout_seconds=timeout)


@pytest.mark.parametrize("key", [None, "private\nkey", "private\rkey", "private\tkey", "private\x00key", "한글-key"])
def test_invalid_keys_cannot_enter_headers_or_diagnostics(key):
    with pytest.raises(ValueError, match="configuration_invalid") as error:
        provider_at(api_key=key)
    assert "private" not in str(error.value)


def test_missing_explicit_key_never_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-env-key")
    provider, calls, frozen = provider_at(api_key=" ")
    with pytest.raises(ProviderUnavailableError):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert calls == [] and not provider.is_configured()


@pytest.mark.parametrize("change", ["packet", "citation", "title-length", "blank-title", "blank-entry", "extra", "section-count"])
def test_original_local_constraints_remain_enforced_after_wire_compilation(change):
    frozen = contract()
    data = proposal(frozen["packet"])
    if change == "packet":
        data["packet_id"] = "jpacket_" + "0" * 64
    elif change == "citation":
        data["sections"][0]["entries"][0]["citation_ids"] = ["foreign"]
    elif change == "title-length":
        data["title"] = "x" * 201
    elif change == "blank-title":
        data["title"] = " \t"
    elif change == "blank-entry":
        data["sections"][0]["entries"][0]["text"] = "\n "
    elif change == "section-count":
        data["sections"] *= 5
    else:
        data["extra"] = True
    provider, calls, _ = provider_at(frozen, value=envelope(preserved_json(data)))
    with pytest.raises(ProviderOutputError):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["redirect", "unauthorized", "rate-limit", "server", "timeout"])
def test_http_failures_do_not_follow_redirects_retry_or_echo_diagnostics(kind):
    def handle(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        return httpx.Response({"redirect": 307, "unauthorized": 401, "rate-limit": 429, "server": 500}[kind],
                              headers={"location": "https://elsewhere.invalid/private"}, content=b"private error")
    provider, calls, frozen = provider_at(handler=handle)
    with pytest.raises(ProviderError, match="unknown") as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(error.value) is ProviderError and len(calls) == 1 and "private" not in str(error.value)


def test_default_dispatcher_denies_before_admission(monkeypatch):
    from app.domain.journal_dispatch import Dispatch
    from app.services import journal_dispatch
    adapter, _ = modules()
    provider, calls, frozen = provider_at()
    monkeypatch.setattr(journal_dispatch.admission, "replay", lambda *_: None)
    monkeypatch.setattr(journal_dispatch.repository, "detail", lambda *_: dict(contract=frozen))
    monkeypatch.setattr(journal_dispatch.admission, "admit", lambda *args, **kwargs: pytest.fail("No authority"))
    dispatcher = adapter.journal_dispatcher(provider)
    try:
        with pytest.raises(ValueError, match="authority_unavailable"):
            dispatcher.dispatch(frozen["topic_id"], frozen["job_id"], frozen["execution_id"],
                                Dispatch(request_id="send", expected_sequence=0), "owner")
        assert calls == [] and dispatcher.snapshot()["workers"] == []
    finally:
        assert dispatcher.stop()

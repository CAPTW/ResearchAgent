"""xAI Journal transport: local MockTransport only, never live credentials."""
from copy import deepcopy
from dataclasses import asdict, FrozenInstanceError
import importlib
import json

import httpx
import pytest

from app.domain import journal_execution
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.services.providers.base import (ProviderError, ProviderOutputError, ProviderRefusalError,
                                        ProviderReportedFailure, ProviderRequest, ProviderUnavailableError)
from tests.test_journal_review_packet import frozen_job, proposal, NOW
from tests.test_openai_response_contract import envelope


def modules():
    return (importlib.import_module("app.services.providers.xai_journal"),
            importlib.import_module("app.services.providers.xai_journal_wire"))


def contract(*, selected=True, **changes):
    job = frozen_job(selected=selected)
    spec = journal_execution.Prepare(**(dict(request_id="xai-journal", expected_input_digest=job["input_digest"],
        provider_name="xai", model="synthetic-api-model") | changes))
    return journal_execution.prepare(job, spec, "owner", NOW)


def test_xai_journal_sends_frozen_request_and_validates_bound_proposal():
    adapter, wire = modules()
    frozen = contract()
    raw = preserved_json(proposal(frozen["packet"]))
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(raw))

    provider = adapter.XaiJournalProvider(frozen, api_key="synthetic-key", timeout_seconds=30,
                                         transport=httpx.MockTransport(handle))
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert response.provider_name == "xai"
    assert response.raw_text == raw
    assert journal_execution.result(frozen, response)["proposal"] == proposal(frozen["packet"])
    assert len(calls) == 1 and str(calls[0].url) == wire.ENDPOINT
    assert wire.ENDPOINT == "https://api.x.ai/v1/responses"
    assert provider.manifest()["schema_version"] == "XAI_JOURNAL_WIRE_V1"
    assert asdict(ProviderRequest(**frozen["provider_request"])) == frozen["provider_request"]


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

    provider = adapter.XaiJournalProvider(frozen, **(dict(api_key="synthetic-key", timeout_seconds=30,
        transport=httpx.MockTransport(handle)) | settings))
    return provider, calls, frozen


@pytest.mark.parametrize("selected", [False, True])
def test_immutable_exact_wire_preserves_both_packet_versions_and_unicode(selected, monkeypatch):
    adapter, wire = modules()
    frozen = contract(selected=selected)
    original = deepcopy(frozen)
    provider, calls, _ = provider_at(frozen)
    original_manifest = provider.manifest()
    detached = provider.manifest()
    detached["tools"].append({"type": "web_search"})
    frozen["provider_request"]["prompt"] = "caller mutated"
    seen_client = []
    original_client = httpx.Client

    def client(**kwargs):
        seen_client.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(adapter.httpx, "Client", client)
    monkeypatch.setenv("XAI_API_KEY", "unrelated-env-key")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    response = provider.generate_json(ProviderRequest(**original["provider_request"]))
    assert seen_client[0]["follow_redirects"] is False and seen_client[0]["trust_env"] is False
    body = json.loads(calls[0].content)
    assert body["model"] == original["specification"]["model"]
    assert body["input"] == [dict(role="system", content=original["provider_request"]["instructions"]),
                             dict(role="user", content=original["provider_request"]["prompt"])]
    assert body["store"] is False and body["stream"] is False and body["tools"] == []
    assert not {"background", "truncation", "metadata", "previous_response_id"} & set(body)
    assert body["max_output_tokens"] == original["specification"]["max_output_tokens"]
    assert body["temperature"] == original["provider_request"]["temperature"]
    assert body["text"]["format"]["name"] == "JOURNAL_PROPOSAL_V1" and body["text"]["format"]["strict"] is True
    assert frozen["packet"]["output_schema"] == original["packet"]["output_schema"]
    assert calls[0].headers["authorization"] == "Bearer synthetic-key"
    assert original_manifest["body_sha256"] == journal_execution.exchange.sha(calls[0].content.decode("utf-8"))
    assert original_manifest["body_bytes"] == len(calls[0].content)
    assert original_manifest["request_sha256"] == journal_execution.exchange.sha(preserved_json(original["provider_request"]))
    assert original_manifest["contract_digest"] == digest(original)
    assert original_manifest["schema_compiler"] == "XAI_NONBLANK_V1"
    assert original_manifest["output_token_limit_enforced"] is False
    assert original_manifest["output_token_scope"] == "VISIBLE_OUTPUT_ONLY"
    assert original_manifest["cost_limit_enforced"] is False
    assert original_manifest["temperature_enforced"] is False
    assert original_manifest["truncation_enforced"] is False
    assert original_manifest["model_identity"] == "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
    assert original_manifest["live_execution"] == "NOT_VERIFIED"
    assert provider.binding.adapter_id == wire.ADAPTER_ID
    assert provider.binding.surface_id == "api" and provider.binding.command_shape_sha256 == digest(original_manifest)
    assert provider.manifest() == original_manifest
    assert response.model == "reported-offline-model" and response.usage == dict(input_tokens=5, output_tokens=4, total_tokens=9)
    assert "synthetic-key" not in repr(provider) + str(provider.describe_status()) + str(original_manifest)
    assert provider.describe_status()["live_call_enabled"] is False
    if selected:
        assert "e\u0301 한글🙂" in body["input"][1]["content"]
    with pytest.raises(FrozenInstanceError):
        provider.model = "changed"


@pytest.mark.parametrize("change", ["prompt", "schema", "instructions", "metadata", "max_output_tokens", "temperature", "schema_name"])
def test_changed_request_is_refused_before_mock_transport(change):
    provider, calls, frozen = provider_at()
    fields = deepcopy(frozen["provider_request"])
    fields[change] = {} if change in {"schema", "metadata"} else 256 if change == "max_output_tokens" else 1 if change == "temperature" else "changed"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(ProviderRequest(**fields))
    assert calls == []


@pytest.mark.parametrize("change", ["provider", "model", "schema", "packet-schema", "prompt", "execution", "packet", "tokens", "binding"])
def test_adapter_binding_cannot_be_reused_for_another_contract(change):
    provider, calls, frozen = provider_at()
    modified = deepcopy(frozen)
    binding = provider.binding
    model = provider.model
    if change == "binding":
        binding = binding.model_copy(update={"command_shape_sha256": "0" * 64})
    elif change == "model":
        model = "different"
    elif change == "provider":
        modified["specification"]["provider_name"] = "openai"
    elif change == "tokens":
        modified["specification"]["max_output_tokens"] = 256
    elif change == "execution":
        modified["execution_id"] = "jexec_" + "0" * 64
    elif change == "packet":
        modified["packet"]["packet_id"] = "jpacket_" + "0" * 64
    elif change == "packet-schema":
        modified["packet"]["schema_version"] = "unsupported"
    else:
        modified["provider_request"][change] = {} if change == "schema" else "changed"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.validate_journal_binding(modified, model, binding)
    assert calls == []


@pytest.mark.parametrize("change", ["provider", "model", "schema", "packet-schema", "prompt", "execution", "packet", "tokens", "output-class"])
def test_invalid_frozen_requests_cannot_build_a_wire(change):
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


@pytest.mark.parametrize("timeout", [0, -1, 121, True, "30", float("nan"), float("inf")])
def test_invalid_timeouts_fail_locally(timeout):
    _, wire = modules()
    with pytest.raises(ValueError, match="configuration_invalid"):
        wire.build_wire(contract(), timeout)


@pytest.mark.parametrize("key", [None, "private\nkey", "private\rkey", "private\tkey", "private\x00key", "한글-key"])
def test_invalid_keys_are_not_injected_into_headers_or_errors(key):
    with pytest.raises(ValueError, match="configuration_invalid") as error:
        provider_at(api_key=key)
    assert "private" not in str(error.value)


def test_missing_explicit_key_never_uses_environment_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "unrelated-env-key")
    provider, calls, frozen = provider_at(api_key=" ")
    assert not provider.is_configured()
    with pytest.raises(ProviderUnavailableError):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert calls == []


@pytest.mark.parametrize("case", ["packet", "citation", "schema", "blank-title", "blank-entry", "extra", "duplicate", "nonfinite", "oversized"])
def test_completed_invalid_proposal_is_rejected_before_return(case):
    frozen = contract()
    payload = proposal(frozen["packet"])
    if case == "packet":
        payload["packet_id"] = "jpacket_" + "0" * 64
    elif case == "citation":
        payload["sections"][0]["entries"][0]["citation_ids"] = ["foreign"]
    elif case == "schema":
        payload["schema_version"] = "PAPER_REVIEW"
    elif case == "blank-title":
        payload["title"] = " \t"
    elif case == "blank-entry":
        payload["sections"][0]["entries"][0]["text"] = "\n "
    elif case == "extra":
        payload["extra"] = True
    raw = preserved_json(payload)
    if case == "duplicate":
        raw = raw.replace('"title":', '"title":"hidden","title":')
    elif case == "nonfinite":
        raw = '{"bad":NaN}'
    elif case == "oversized":
        raw += " " * 512_001
    provider, calls, _ = provider_at(frozen, value=envelope(raw))
    with pytest.raises(ProviderOutputError):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled"])
def test_identified_terminal_outcomes_are_known_without_retry(status):
    provider, calls, frozen = provider_at(value=envelope(status=status))
    with pytest.raises(ProviderReportedFailure) as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert error.value.status == status and len(calls) == 1


def test_refusal_is_known_and_does_not_echo_provider_text():
    value = envelope()
    value["output"][0]["content"] = [dict(type="refusal", refusal="private response")]
    provider, calls, frozen = provider_at(value=value)
    with pytest.raises(ProviderRefusalError) as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert "private" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize("case", ["no-status", "running", "no-id", "padded-id", "no-model", "failed-no-id", "conflicting-completed", "duplicate-key"])
def test_unconfirmed_http_response_remains_unknown(case):
    value = envelope()
    raw = None
    if case == "no-status":
        value.pop("status")
    elif case == "running":
        value["status"] = "in_progress"
    elif case == "no-id":
        value.pop("id")
    elif case == "padded-id":
        value["id"] = " resp "
    elif case == "no-model":
        value.pop("model")
    elif case == "failed-no-id":
        value.update(status="failed", id=None)
    elif case == "conflicting-completed":
        value["error"] = dict(message="private")
    else:
        raw = b'{"id":"first","id":"second"}'
    provider, calls, frozen = provider_at(value=value, raw=raw)
    with pytest.raises(ProviderError, match="unknown") as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(error.value) is ProviderError and len(calls) == 1 and "private" not in str(error.value)


@pytest.mark.parametrize("kind", ["redirect", "unauthorized", "rate-limit", "timeout"])
def test_http_failures_do_not_redirect_retry_or_echo_diagnostics(kind):
    def handle(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private transport error", request=request)
        return httpx.Response({"redirect": 307, "unauthorized": 401, "rate-limit": 429}[kind],
                              headers={"location": "https://elsewhere.invalid/private"}, content=b"private server error")
    provider, calls, frozen = provider_at(handler=handle)
    with pytest.raises(ProviderError, match="unknown") as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(error.value) is ProviderError and len(calls) == 1
    assert "private" not in str(error.value) and "synthetic-key" not in str(error.value)


def test_response_decoding_is_bounded_and_closes_stream():
    class Stream(httpx.SyncByteStream):
        closed = False
        reads = 0

        def __iter__(self):
            for _ in range(100):
                self.reads += 1
                yield b"x" * 65_536

        def close(self):
            self.closed = True

    stream = Stream()
    provider, calls, frozen = provider_at(handler=lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(ProviderError, match="unknown"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1 and stream.closed and stream.reads < 100


def test_default_dispatch_authority_denies_before_admission(monkeypatch):
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

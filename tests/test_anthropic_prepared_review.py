"""Paper Messages fixtures use MockTransport and pure history validation only."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json

import httpx
import pytest

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest, ProviderUnavailableError
from tests.test_anthropic_messages import envelope
from tests import test_prepared_review as prior


def module():
    return importlib.import_module("app.services.providers.anthropic_prepared_review")


def contract(packet=None, **changes):
    return prepared_review.prepare(packet or prior.packet(), **(dict(provider_name="anthropic", model="synthetic-messages-model") | changes))


def provider_for(frozen=None, handler=None, **options):
    frozen = contract() if frozen is None else frozen
    calls = []

    def respond(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=envelope(prior.response(frozen).raw_text))

    provider = module().AnthropicPreparedReviewProvider(frozen, **(dict(timeout_seconds=30,
        api_key="synthetic-key", transport=httpx.MockTransport(respond)) | options))
    return provider, frozen, calls


def test_opus55_paper_retains_final_text_and_unsigned_archive_without_thinking():
    frozen = contract(model="claude-opus-5-5")
    expected_plan = module().build_plan(frozen, timeout_seconds=30)
    raw = prior.response(frozen).raw_text
    value = envelope(model="claude-opus-5-5", content=[dict(type="thinking", thinking="", signature="opaque-signature"),
        dict(type="redacted_thinking", data="opaque-data"), dict(type="text", text=raw)])
    provider, _, calls = provider_for(frozen, lambda _: httpx.Response(200, json=value))
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    proof = record["validation"]["prepared_execution"]
    assert proof["response"]["raw_text"] == raw and proof["response"]["payload"] == json.loads(raw)
    assert "opaque" not in preserved_json(record)
    assert provider.manifest() == expected_plan[2]
    assert len(calls) == 1 and calls[0].content == expected_plan[1]
    body = json.loads(calls[0].content)
    assert body["model"] == "claude-opus-5-5" and "thinking" not in body
    assert body["max_tokens"] == frozen["provider_request"]["max_output_tokens"]
    assert proof["scientific_support"] == "NOT_ASSESSED" and proof["full_document_review"] == "NOT_ESTABLISHED"
    module().validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("version", [prepared_review.LEGACY_SCHEMA, prepared_review.SCHEMA_V2, prepared_review.SCHEMA])
def test_exact_paper_wire_and_pure_archive_reconstruction(version):
    frozen = contract(schema_version=version)
    original = deepcopy(frozen)
    provider, _, calls = provider_for(frozen)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert frozen == original and len(calls) == 1
    body = json.loads(calls[0].content)
    assert str(calls[0].url) == "https://api.anthropic.com/v1/messages" and calls[0].headers["x-api-key"] == "synthetic-key"
    assert calls[0].headers["anthropic-version"] == "2023-06-01" and "authorization" not in calls[0].headers
    assert body["model"] == frozen["model"] and body["max_tokens"] == frozen["provider_request"]["max_output_tokens"]
    assert body["system"] == frozen["provider_request"]["instructions"]
    assert body["messages"] == [dict(role="user", content=frozen["provider_request"]["prompt"])]
    assert " e\u0301 한글🙂 tail " in calls[0].content.decode("utf-8")
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "minimum" not in body["output_config"]["format"]["schema"]["properties"]["confidence"]
    assert frozen["provider_request"]["schema"]["properties"]["confidence"]["minimum"] == 0
    assert body["tools"] == [] and body["stream"] is False
    assert "temperature" not in body and frozen["provider_request"]["temperature"] == 0.2
    assert not {"store", "truncation", "metadata", "background"} & set(body)
    manifest = provider.manifest()
    assert manifest["schema_version"] == "ANTHROPIC_PREPARED_REVIEW_WIRE_V1"
    assert manifest["contract_sha256"] == sha(preserved_json(frozen))
    assert manifest["body_sha256"] == sha(calls[0].content.decode("utf-8")) and manifest["body_bytes"] == len(calls[0].content)
    assert manifest["local_schema_sha256"] != manifest["wire_schema_sha256"]
    assert manifest["max_review_output_bytes"] == 262144 and manifest["raw_envelope_retained"] is False
    assert manifest["raw_output_encoding"] == "CONTENT_TEXT_JSON_UTF8_V1"
    assert manifest["temperature_policy"] == "OMITTED_PROVIDER_DEFAULT"
    proof = record["validation"]["prepared_execution"]
    assert proof["response"]["raw_text"] == prior.response(frozen).raw_text
    assert proof["response"]["model"] == "reported-offline-model" and "total_tokens" not in proof["response"]["usage"]
    assert proof["scientific_support"] == "NOT_ASSESSED" and proof["full_document_review"] == "NOT_ESTABLISHED"
    marker = record["validation"]["execution"]
    assert marker["manifest"] == manifest and marker["schema_version"] == "PREPARED_REVIEW_ANTHROPIC_HTTP_TRANSPORT_V1"
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    module().validate_transport(record, frozen["packet"])
    assert len(calls) == 1
    assert "synthetic-key" not in repr(provider) + str(provider.describe_status()) + str(marker)
    assert provider.describe_status()["live_call_enabled"] is False


def test_detached_manifest_and_missing_key_never_mutate_or_fallback(monkeypatch):
    provider, frozen, calls = provider_for()
    request = ProviderRequest(**frozen["provider_request"])
    provider.manifest()["endpoint"] = "https://private.invalid"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_record(replace(request, prompt=request.prompt + "changed"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-env-key")
    missing, _, no_calls = provider_for(api_key="")
    with pytest.raises(ProviderUnavailableError):
        missing.generate_record(request)
    assert calls == no_calls == []


@pytest.mark.parametrize("options", [dict(timeout_seconds=0), dict(timeout_seconds=121), dict(timeout_seconds=True),
    dict(timeout_seconds=float("inf")), dict(api_key=None), dict(api_key="private\r\nheader")])
def test_invalid_paper_configuration_is_local_error(options):
    with pytest.raises(ValueError, match="configuration_invalid"):
        provider_for(**options)


@pytest.mark.parametrize("change", ["provider", "request", "schema", "instructions", "model"])
def test_paper_plan_rebuilds_original_contract_before_send(change):
    frozen = contract()
    if change in {"provider", "model"}:
        frozen["provider_name" if change == "provider" else "model"] = "other"
    else:
        frozen["provider_request"]["prompt" if change == "request" else change] = {} if change == "schema" else "changed"
    with pytest.raises(ValueError):
        module().build_plan(frozen, timeout_seconds=30)


@pytest.mark.parametrize("mode,category", [("citation", "output"), ("confidence", "output"), ("blank", "output"),
    ("journal", "output"), ("refusal", "refusal"), ("max_tokens", "incomplete"), ("unidentified", "unknown"),
    ("timeout", "unknown"), ("redirect", "unknown")])
def test_paper_output_local_constraints_and_uncertain_failure_never_retry(mode, category):
    frozen = contract()
    draft = prior.response(frozen).payload
    if mode == "citation":
        draft["cited_evidence_span_ids"] = ["invented"]
    elif mode == "confidence":
        draft["confidence"] = 1.1
    elif mode == "blank":
        draft["contribution_summary"] = " \t"
    elif mode == "journal":
        draft = dict(schema_version="JOURNAL_PROPOSAL_V1")
    value = envelope(preserved_json(draft))
    if mode in {"refusal", "max_tokens"}:
        value["stop_reason"] = mode
    elif mode == "unidentified":
        value.update(id=None, stop_reason="refusal")

    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        return httpx.Response(307 if mode == "redirect" else 200, json=value, headers={"location": "https://never-follow.invalid"})

    provider, _, calls = provider_for(frozen, handler)
    with pytest.raises(ProviderError) as error:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(error.value, ProviderOutputError)
    elif category == "refusal":
        assert isinstance(error.value, ProviderRefusalError)
    elif category == "incomplete":
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == "incomplete"
    else:
        assert type(error.value) is ProviderError and "unknown" in str(error.value)
    assert len(calls) == 1 and "private" not in str(error.value)


@pytest.mark.parametrize("change", ["body", "endpoint", "api_version", "schema", "limit", "manifest_hash", "response_hash", "authority", "extra"])
def test_rehashed_archive_contradictions_are_rejected(change):
    provider, frozen, _ = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    marker = record["validation"]["execution"]
    if change in {"body", "endpoint", "api_version", "schema", "limit"}:
        key = {"body": "body_sha256", "schema": "wire_schema_sha256", "limit": "max_review_output_bytes"}.get(change, change)
        marker["manifest"][key] = 1 if change == "limit" else "changed"
        marker["manifest_sha256"] = sha(preserved_json(marker["manifest"]))
    elif change == "manifest_hash":
        marker["manifest_sha256"] = "0" * 64
    elif change == "response_hash":
        marker["response_sha256"] = "0" * 64
    elif change == "authority":
        marker["authority"] = "APPROVED"
    else:
        marker["extra"] = True
    with pytest.raises(ValueError, match="transport_invalid"):
        module().validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("extra", [0, 1])
def test_paper_byte_cap_remains_256k(extra):
    frozen = contract()
    raw = prior.response(frozen).raw_text
    raw += " " * (262144 + extra - len(raw.encode("utf-8")))
    provider, _, calls = provider_for(frozen, lambda _: httpx.Response(200, json=envelope(raw)))
    if extra:
        with pytest.raises(ProviderOutputError):
            provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    else:
        record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
        assert record["validation"]["prepared_execution"]["response"]["raw_text"] == raw
    assert len(calls) == 1

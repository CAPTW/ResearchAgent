"""Paper HTTP boundary: synthetic Responses envelopes, never a live endpoint."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import importlib
import json
import sqlite3

import httpx
import pytest

from app.domain import journal_review_input, prepared_review
from app.domain.journal_exchange import preserved_json
from app.repositories import ai_review_drafts as receipts
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from tests import test_prepared_review as prior

ENDPOINT = "https://paper-provider.invalid/v1/responses"


def contract(packet=None):
    return prepared_review.prepare(packet or prior.packet(), provider_name="openai", model="synthetic-requested-model")


def envelope(frozen):
    return dict(id="synthetic-response", model="synthetic-reported-model", status="completed",
        output=[dict(type="message", role="assistant", status="completed",
                     content=[dict(type="output_text", text=prior.response(frozen).raw_text)])],
        usage=dict(input_tokens=4, output_tokens=7, total_tokens=11))


def provider_for(frozen=None, handler=None, **options):
    frozen = contract() if frozen is None else frozen
    calls = []
    def respond(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=envelope(frozen))
    module = importlib.import_module("app.services.providers.openai_prepared_review")
    provider = module.OpenAIPreparedReviewProvider(frozen, **(dict(endpoint=ENDPOINT, timeout_seconds=30,
        api_key="synthetic-secret", transport=httpx.MockTransport(respond)) | options))
    return provider, frozen, calls


def test_exact_wire_receipt_and_journal_reference_preserve_unicode(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ignored.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "ignored-environment-secret")
    provider, frozen, calls = provider_for()
    assert calls == []
    assert provider.describe_status()["live_call_enabled"] is False
    assert "synthetic-secret" not in repr(provider)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1
    request = calls[0]
    manifest = provider.manifest()
    body = json.loads(request.content)
    assert str(request.url) == ENDPOINT and request.method == "POST"
    assert request.headers["authorization"] == "Bearer synthetic-secret"
    assert manifest["body_sha256"] == prepared_review.sha(request.content.decode("utf-8"))
    assert manifest["body_bytes"] == len(request.content)
    assert body["input"][1]["content"] == frozen["provider_request"]["prompt"]
    assert " e\u0301 한글🙂 tail " in request.content.decode("utf-8")
    assert body["text"]["format"]["name"] == "PREPARED_PAPER_REVIEW_DRAFT_V3"
    assert body["text"]["format"]["schema"] == frozen["provider_request"]["schema"]
    assert body["max_output_tokens"] == 2200
    assert body["model"] == "synthetic-requested-model"
    for key in ("store", "background", "stream"):
        assert body[key] is False
    assert body["truncation"] == "disabled"
    for key in ("automatic_retry", "redirects", "environment_proxy", "output_token_limit_enforced", "temperature_enforced"):
        assert manifest[key] is False
    proof = record["validation"]["prepared_execution"]
    assert proof["response"]["raw_text"] == prior.response(frozen).raw_text
    assert proof["response"]["model"] == "synthetic-reported-model"
    assert proof["scientific_support"] == "NOT_ASSESSED"
    assert proof["full_document_review"] == "NOT_ESTABLISHED"
    marker = record["validation"]["execution"]
    assert marker["manifest"] == manifest
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    database = tmp_path / "receipts.sqlite3"
    with closing(sqlite3.connect(database)) as c, c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN IMMEDIATE")
        saved = receipts.append_input_receipt_in(c, record, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC")
        assert receipts.append_input_receipt_in(c, record, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC") == saved
        selected = journal_review_input.freeze(c, "topic", dict(receipt_id=saved["receipt_id"],
            component_ids=["/evidence_spans/0/text"]), [("source-run", "paper")])
        paper = dict(paper_version_id="version", canonical_paper_id="paper", title="Fixture", source_identity={},
            metadata_digest="a" * 64, review_state="partial", rights={"state": "unknown"}, review_input=selected)
        attachment = journal_review_input.attachment(paper)
        assert attachment["input_origin"] == "FIXTURE_SYNTHETIC"
        assert attachment["review_result"] == record["draft"]
        assert attachment["validation"]["execution"] == marker
    with closing(sqlite3.connect(database)) as c:
        c.row_factory = sqlite3.Row
        assert receipts.get_input_receipt_in(c, "topic", saved["receipt_id"]) == saved


def test_detached_plan_changed_request_and_missing_key_never_send():
    provider, frozen, calls = provider_for()
    request = ProviderRequest(**frozen["provider_request"])
    manifest = provider.manifest()
    manifest["endpoint"] = "https://changed.invalid/responses"
    frozen["provider_request"]["prompt"] += "changed"
    assert provider.manifest()["endpoint"] == ENDPOINT
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_record(replace(request, prompt=request.prompt + "changed"))
    missing, _, missing_calls = provider_for(api_key="")
    for action in (missing.validate_local_runtime, lambda: missing.generate_record(request)):
        with pytest.raises(ProviderError, match="credentials"):
            action()
    assert calls == missing_calls == []


@pytest.mark.parametrize("options", [dict(endpoint="http://paper.invalid/responses"), dict(endpoint=ENDPOINT + "?key=x"),
    dict(endpoint="https://user:secret@paper.invalid/responses"), dict(endpoint=ENDPOINT + "#x"),
    dict(endpoint="https://paper.invalid/other"), dict(timeout_seconds=True), dict(timeout_seconds=float("inf")),
    dict(timeout_seconds=0), dict(timeout_seconds=121), dict(api_key="bad\r\nheader")])
def test_invalid_configuration_is_rejected(options):
    with pytest.raises(ValueError, match="configuration_invalid"):
        provider_for(**options)


@pytest.mark.parametrize("mode,category", [("citation", "output"), ("journal", "output"), ("failed", "failed"),
    ("unidentified", "unknown"), ("timeout", "unknown"), ("redirect", "unknown"), ("http", "unknown")])
def test_invalid_and_uncertain_response_never_retries(mode, category):
    frozen = contract()
    body = envelope(frozen)
    if mode == "citation":
        draft = prior.response(frozen).payload
        draft["cited_evidence_span_ids"] = ["invented"]
        body["output"][0]["content"][0]["text"] = preserved_json(draft)
    elif mode == "journal":
        body["output"][0]["content"][0]["text"] = '{"schema_version":"JOURNAL_PROPOSAL_V1"}'
    elif mode in {"failed", "unidentified"}:
        body["status"] = "failed"
        if mode == "unidentified":
            del body["id"]
    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        return httpx.Response(302 if mode == "redirect" else 500 if mode == "http" else 200,
            json=body, headers={"Location": "https://never-follow.invalid/responses"})
    provider, _, calls = provider_for(frozen, respond)
    with pytest.raises((ProviderError, httpx.HTTPError)) as failure:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(failure.value, ProviderOutputError)
    elif category == "failed":
        assert isinstance(failure.value, ProviderReportedFailure)
    else:
        assert not isinstance(failure.value, (ProviderOutputError, ProviderReportedFailure))
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["body", "endpoint", "manifest_hash", "response_hash", "authority", "extra", "id_only", "schema_only"])
def test_tampered_archive_rejected_before_receipt_insert(tmp_path, change):
    provider, frozen, _ = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    bad = deepcopy(record)
    marker = bad["validation"]["execution"]
    if change in {"body", "endpoint"}:
        marker["manifest"]["body_sha256" if change == "body" else "endpoint"] = "0" * 64
        marker["manifest_sha256"] = prepared_review.sha(preserved_json(marker["manifest"]))
    elif change == "manifest_hash":
        marker["manifest_sha256"] = "0" * 64
    elif change == "response_hash":
        marker["response_sha256"] = "0" * 64
    elif change == "authority":
        marker["authority"] = "APPROVED"
    elif change == "id_only":
        del marker["schema_version"]
    elif change == "schema_only":
        del marker["adapter_id"]
    else:
        marker["extra"] = True
    with closing(sqlite3.connect(tmp_path / "rejected.sqlite3")) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="transport_invalid"):
            receipts.append_input_receipt_in(c, bad, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC")
        assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []

"""Frozen prepared-review requests/results, using only synthetic local values."""
from copy import deepcopy
from dataclasses import asdict
import importlib
import json

import pytest

from app.domain import review_preparation, review_range_input
from app.domain.journal_exchange import preserved_json
from app.services import ai_review
from app.services.providers.base import ProviderRequest, ProviderResult


def module():
    return importlib.import_module("app.domain.prepared_review")


def packet():
    selected = " e\u0301 한글🙂 tail "
    text = "x" * 30000 + selected
    source = dict(run_id="source-run", paper_id="paper", source_chars=len(text), source_kind="text",
                  artifact_sha256=review_preparation.sha(text), artifact_bytes=len(text.encode("utf-8")),
                  artifact_ref="run:raw/paper.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.Prepare(request_id="range", run_id="source-run", paper_id="paper",
        expected_source_digest=source["source_digest"], start=30000, end=len(text))
    saved = review_preparation.build("topic", "version", "owner", body, source, text, "2026-09-28T00:00:00Z")
    return review_range_input.build_packet(saved)


def contract():
    return module().prepare(packet(), provider_name="synthetic", model="synthetic-review-v1", max_output_tokens=2200)


def response(frozen, *, unsupported=False):
    draft = ai_review.generate_fixture_ai_review_draft(frozen["packet"], unsupported_claim=unsupported)
    draft["contribution_summary"] = " e\u0301 한글🙂 output "
    raw = json.dumps(draft, ensure_ascii=False, indent=2)
    return ProviderResult(provider_name="synthetic", model="synthetic-review-v1", response_id="fixture-result",
                          payload=draft, raw_text=raw, usage={"input_tokens": 4, "output_tokens": 7, "total_tokens": 11})


def changed_response(frozen, change):
    item = response(frozen)
    change(item.payload)
    item.raw_text = json.dumps(item.payload, ensure_ascii=False)
    return item


@pytest.mark.parametrize("version", ["PREPARED_REVIEW_REQUEST_V1", "PREPARED_REVIEW_REQUEST_V2", "PREPARED_REVIEW_REQUEST_V3"])
@pytest.mark.parametrize("unsupported", [False, True])
def test_diagnostics_use_historical_validation_without_rejecting_valid_or_needs_review(version, unsupported):
    domain = module()
    frozen = domain.prepare(packet(), provider_name="synthetic", model="synthetic-review-v1", schema_version=version)
    output = response(frozen, unsupported=unsupported)
    original = deepcopy((frozen, output))
    assert domain.output_rejection_reasons(frozen, output) == []
    assert (frozen, output) == original
    assert domain.result(frozen, output)["status"] == ("needs_review" if unsupported else "accepted")


@pytest.mark.parametrize("raw", ["not-json-untrusted", '{"paper_id":"a","paper_id":"b"}', '{"confidence":NaN}'])
def test_diagnostics_never_echo_invalid_response_text(raw):
    frozen = contract()
    output = response(frozen)
    output.raw_text = raw
    assert module().output_rejection_reasons(frozen, output) == ["OUTPUT_CONTRACT_INVALID"]


def test_diagnostics_contain_deep_json_recursion():
    import sys
    frozen = contract()
    output = response(frozen)
    output.raw_text = '{"nested":' + '[' * 10000 + '0' + ']' * 10000 + '}'
    previous_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        assert module().output_rejection_reasons(frozen, output) == ["OUTPUT_CONTRACT_INVALID"]
    finally:
        sys.setrecursionlimit(previous_limit)


def test_request_freezes_exact_unicode_packet_and_structured_contract():
    original = packet()
    saved = deepcopy(original)
    frozen = module().prepare(original, provider_name="synthetic", model="synthetic-review-v1")
    assert original == saved
    assert module().validate_contract(frozen) == frozen
    assert frozen["schema_version"] == "PREPARED_REVIEW_REQUEST_V3"
    assert frozen["packet"] == saved
    request = ProviderRequest(**frozen["provider_request"])
    assert asdict(request) == frozen["provider_request"]
    assert request.schema_name == "PREPARED_PAPER_REVIEW_DRAFT_V3"
    assert request.schema["additionalProperties"] is False
    assert set(request.schema["required"]) == set(request.schema["properties"])
    assert " e\u0301 한글🙂 tail " in request.prompt
    assert request.prompt == preserved_json(saved)
    assert request.max_output_tokens == 2200
    assert frozen["request_sha256"] == review_preparation.sha(preserved_json(asdict(request)))
    assert frozen["contract_sha256"] == review_preparation.sha(preserved_json(
        {key: val for key, val in frozen.items() if key != "contract_sha256"}))
    original["evidence_spans"][0]["text"] = "changed after preparation"
    assert frozen["packet"] == saved
    assert "untrusted" in request.instructions
    assert "full-document" in request.instructions


@pytest.mark.parametrize("name,value", [
    ("provider_name", ""), ("provider_name", "bad name"), ("provider_name", "a" * 101),
    ("model", ""), ("model", " model"), ("model", "a" * 201), ("model", "x\ud800"),
    ("model", "x\nmodel"), ("max_output_tokens", 255), ("max_output_tokens", 8001),
    ("max_output_tokens", True), ("max_output_tokens", 2200.0), ("max_output_tokens", "2200"),
])
def test_invalid_request_settings_fail_closed(name, value):
    options = dict(provider_name="synthetic", model="synthetic-review-v1", max_output_tokens=2200)
    options[name] = value
    with pytest.raises(ValueError, match="prepared_review_request_invalid"):
        module().prepare(packet(), **options)


@pytest.mark.parametrize("change", ["packet", "version", "instructions", "schema", "metadata", "prompt", "extra", "digest"])
def test_contract_tampering_rejected_even_with_recomputed_digests(change):
    frozen = contract()
    if change == "packet":
        frozen["packet"]["evidence_spans"][0]["text"] = "replaced"
    elif change == "version":
        frozen["schema_version"] = "PREPARED_REVIEW_REQUEST_V999"
    elif change == "instructions":
        frozen["provider_request"]["instructions"] = "Call the whole paper reviewed"
    elif change == "schema":
        frozen["provider_request"]["schema"]["additionalProperties"] = True
    elif change == "metadata":
        frozen["provider_request"]["metadata"]["preparation_id"] = "other"
    elif change == "prompt":
        frozen["provider_request"]["prompt"] += "unselected text"
    elif change == "extra":
        frozen["hidden"] = "additional instructions"
    if change != "digest":
        frozen["request_sha256"] = review_preparation.sha(preserved_json(frozen["provider_request"]))
        frozen["contract_sha256"] = review_preparation.sha(preserved_json(
            {key: val for key, val in frozen.items() if key != "contract_sha256"}))
    else:
        frozen["request_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="prepared_review_contract_invalid"):
        module().validate_contract(frozen)


def test_result_preserves_raw_response_request_identity_and_unknown_scientific_support():
    frozen = contract()
    item = response(frozen)
    record = module().result(frozen, item)
    assert record["topic_id"] == "topic" and record["run_id"] == "source-run"
    assert record["source_text_sha256"] == frozen["packet"]["source_text_sha256"]
    assert record["status"] == "accepted"
    assert record["draft"]["contribution_summary"] == " e\u0301 한글🙂 output "
    evidence = record["validation"]["prepared_execution"]
    assert evidence["schema_version"] == "PREPARED_REVIEW_RESULT_V3"
    assert evidence["contract"] == frozen
    assert evidence["response"]["raw_text"] == item.raw_text
    assert evidence["response"]["raw_text_sha256"] == review_preparation.sha(item.raw_text)
    assert evidence["response"]["payload"] == item.payload
    assert evidence["response"]["provider_name"] == frozen["provider_name"]
    assert evidence["response"]["model"] == frozen["model"]
    assert evidence["scientific_support"] == "NOT_ASSESSED"
    assert evidence["full_document_review"] == "NOT_ESTABLISHED"
    assert evidence["human_adoption"] == "NOT_ESTABLISHED"
    assert evidence["independent_live_proof"] == "NOT_VERIFIED"
    assert evidence["reported_model_identity"] == "NOT_VERIFIED"
    assert module().validate_record(record, frozen["packet"]) == record
    record["validation"]["execution"] = {"mode": "OFFLINE_SYNTHETIC", "adapter_version": "2"}
    record.update(receipt_id="server-owned", recorded_at="server-owned", input_origin="FIXTURE_SYNTHETIC")
    assert module().validate_record(record, frozen["packet"]) == record
    item.payload["contribution_summary"] = "mutated response"
    assert record["draft"]["contribution_summary"] == " e\u0301 한글🙂 output "


def test_unsupported_claims_stay_needs_review_and_are_preserved():
    frozen = contract()
    record = module().result(frozen, response(frozen, unsupported=True))
    assert record["status"] == "needs_review"
    assert record["validation"]["accepted"] is False
    assert record["draft"]["unsupported_claims"] == ["unsupported fixture claim"]
    assert module().validate_record(record, frozen["packet"]) == record


def test_abstract_only_response_cannot_bypass_citation_identity_binding():
    frozen = contract()
    item = changed_response(frozen, lambda value: value.update(
        review_level="abstract_only", cited_evidence_span_ids=["invented"]))
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        module().result(frozen, item)


@pytest.mark.parametrize("raw", [
    '{"confidence":0,"confidence":0}', '{"nested":{"x":1,"x":2}}',
    '{"confidence":NaN}', '{"confidence":Infinity}', '{"confidence":1e999}',
    '{"text":"\\ud800"}', '{"text":"\ud800"}', '[]', '{}{}', '',
])
def test_ambiguous_or_invalid_raw_json_rejected(raw):
    frozen = contract()
    item = response(frozen)
    item.raw_text = raw
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        module().result(frozen, item)


@pytest.mark.parametrize("change", [
    lambda value: value.update(paper_id="other"),
    lambda value: value.update(source_text_sha256="0" * 64),
    lambda value: value.update(cited_evidence_span_ids=["invented"]),
    lambda value: value.update(cited_evidence_span_ids=[]),
    lambda value: value.update(confidence=True),
    lambda value: value.update(confidence="0.4"),
    lambda value: value.update(confidence=2.0),
    lambda value: value.update(limitations=[42]),
    lambda value: value.update(unknown_field="unsupported"),
    lambda value: value.pop("follow_up_questions"),
])
def test_schema_and_packet_binding_failures_never_make_receipts(change):
    frozen = contract()
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        module().result(frozen, changed_response(frozen, change))


@pytest.mark.parametrize("field,value", [
    ("provider_name", "other"), ("model", ""), ("model", "synthetic-review-v1 "),
    ("response_id", ""), ("response_id", "a" * 201), ("response_id", "x\nsecret"),
    ("response_id", "\ud800"), ("raw_text", None), ("raw_text", " " * (256 * 1024 + 1)),
], ids=["provider-mismatch", "missing-model", "padded-model", "missing-id", "long-id", "control-id",
        "surrogate-id", "missing-raw", "large-raw"])
def test_reported_identity_and_response_bounds_are_checked(field, value):
    frozen = contract()
    item = response(frozen)
    setattr(item, field, value)
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        module().result(frozen, item)


def test_payload_must_exactly_match_raw_json_including_unicode_and_numeric_types():
    frozen = contract()
    item = response(frozen)
    item.payload["contribution_summary"] = " é 한글🙂 output "
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        module().result(frozen, item)
    item = response(frozen)
    item.payload["confidence"] = True
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        module().result(frozen, item)


def test_only_bounded_reported_usage_counts_are_retained():
    frozen = contract()
    item = response(frozen)
    item.response_id = None
    item.usage = {"input_tokens": 7, "output_tokens": True, "total_tokens": -1,
                  "headers": {"secret": "never archived"}, "cached_tokens": 99999999999}
    record = module().result(frozen, item)
    assert record["validation"]["prepared_execution"]["response"]["usage"] == {"input_tokens": 7}
    assert module().validate_record(record, frozen["packet"]) == record


@pytest.mark.parametrize("reported", ["not_reported", "resolved-model-20260928"])
def test_reported_model_is_preserved_separately_without_proving_requested_identity(reported):
    frozen = contract()
    item = response(frozen)
    item.model = reported
    record = module().result(frozen, item)
    proof = record["validation"]["prepared_execution"]
    assert proof["response"]["model"] == reported
    assert proof["contract"]["model"] == "synthetic-review-v1"
    assert proof["reported_model_identity"] == "NOT_VERIFIED"
    assert module().validate_record(record, frozen["packet"]) == record


@pytest.mark.parametrize("change", ["draft", "status", "validation", "raw", "raw_hash", "packet", "scientific", "usage", "extra"])
def test_archived_record_is_rebuilt_and_checked(change):
    frozen = contract()
    record = module().result(frozen, response(frozen))
    proof = record["validation"]["prepared_execution"]
    if change == "draft":
        record["draft"]["contribution_summary"] = "new"
    elif change == "status":
        record["status"] = "needs_review"
    elif change == "validation":
        record["validation"]["reason_codes"] = ["invented"]
    elif change == "raw":
        proof["response"]["raw_text"] += " "
    elif change == "raw_hash":
        proof["response"]["raw_text_sha256"] = "0" * 64
    elif change == "packet":
        frozen["packet"]["source_preparation"]["preparation_id"] = "other"
    elif change == "scientific":
        proof["scientific_support"] = "VERIFIED"
    elif change == "usage":
        proof["response"]["usage"]["extra"] = "secret"
    else:
        record["validation"]["unknown"] = "not part of validation"
    with pytest.raises(ValueError, match="prepared_review_record_invalid"):
        module().validate_record(record, frozen["packet"])


def test_legacy_packet_cannot_silently_enter_prepared_contract():
    old = packet()
    old["packet_version"] = "ai-review-packet-v2"
    with pytest.raises(ValueError, match="prepared_review_request_invalid"):
        module().prepare(old, provider_name="synthetic", model="synthetic-review-v1")

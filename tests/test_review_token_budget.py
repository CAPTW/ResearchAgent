"""Document units preserve legacy archives and bind a local conservative budget."""
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from app.domain import prepared_review, review_preparation, review_range_input
from app.domain.journal_exchange import preserved_json
from app.repositories import review_preparations
from tests.test_prepared_review import contract as legacy_contract, packet as legacy_packet, response


def budget(**changes):
    return dict(provider_name="synthetic", model="synthetic-review-v1", context_tokens=2_000_000,
        input_tokens=1_900_000, max_output_tokens=2200, safety_tokens=8192,
        estimator="UTF8_BYTES_CONSERVATIVE_V1", **changes)


def context():
    return dict(schema_version="REVIEW_CONTEXT_V1", topic_title="연구 e\u0301", topic_name="Model research",
        paper_title="Preserved paper 🙂", authors=["연구자"], published_at="2026-10-18",
        metadata_digest="d" * 64, topic_updated_at="2026-10-18T00:00:00Z")


def unit(text, **changes):
    value = dict(schema_version="PAPER_ANALYSIS_UNIT_V1", plan_id="rplan_" + "a" * 64,
        plan_sha256="b" * 64, unit_index=0, unit_count=1, label="전체 추출 본문 e\u0301 🙂",
        document_chars=len(text), outline=[dict(label="분석 범위", start=0, end=len(text))],
        context_before="", context_after="", budget=budget())
    value.update(changes)
    return value


def preparation(text=None, *, start=0, end=None, analysis_unit=None):
    text = "한글🙂 e\u0301 source body " * 900 if text is None else text
    end = len(text) if end is None else end
    source = dict(run_id="source-run", paper_id="paper", source_chars=len(text), source_kind="text",
        artifact_sha256=review_preparation.sha(text), artifact_bytes=len(text.encode("utf-8")),
        artifact_ref="run:raw/document.txt", extraction_truncation="NOT_OBSERVED")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.DocumentPrepare(request_id="document", run_id="source-run", paper_id="paper",
        expected_source_digest=source["source_digest"], start=start, end=end,
        analysis_unit=unit(text) if analysis_unit is None else analysis_unit)
    saved = review_preparation.build("topic", "version", "owner", body, source, text,
        "2026-10-18T00:00:00Z", context=context())
    return saved, body


def frozen(saved):
    return prepared_review.prepare(review_range_input.build_packet(saved), provider_name="synthetic",
        model="synthetic-review-v1", max_output_tokens=2200)


def token_domain():
    return importlib.import_module("app.domain.review_token_budget")


def test_document_target_over_12000_uses_new_versions_and_preserves_unicode():
    saved, body = preparation()
    assert saved["selection"]["chars"] > 12000
    assert saved["selection"]["text"] == "한글🙂 e\u0301 source body " * 900
    assert saved["schema_version"] == "PAPER_REVIEW_PREPARATION_V3"
    assert saved["analysis_unit"] == body.analysis_unit
    value = frozen(saved)
    assert value["schema_version"] == "PREPARED_REVIEW_REQUEST_V4"
    assert value["packet"]["packet_version"] == "ai-review-packet-v5"
    assert value["packet"]["source_preparation"]["schema_version"] == "REVIEW_PREPARATION_BINDING_V3"
    assert value["provider_request"]["schema_name"] == "PREPARED_PAPER_REVIEW_DRAFT_V3"
    assert prepared_review.validate_contract(value) == value


def test_conservative_estimate_counts_exact_request_bytes_and_schema_twice():
    request = dict(instructions=" e\u0301 instruction 🙂 ", prompt="한글\nbody",
        schema={"type": "object", "title": "정확한 스키마"})
    before = deepcopy(request)
    expected = (len(request["instructions"].encode("utf-8")) + len(request["prompt"].encode("utf-8"))
        + 2 * len(preserved_json(request["schema"]).encode("utf-8")) + 1024)
    assert token_domain().estimate_input_tokens(request) == expected
    assert request == before


@pytest.mark.parametrize("field,value", [
    ("context_tokens", True), ("input_tokens", 0), ("input_tokens", 1.5), ("safety_tokens", -1),
    ("max_output_tokens", 255), ("max_output_tokens", 8001), ("model", " model"),
    ("provider_name", "anthropic\n"), ("estimator", "EXACT_TOKENIZER"), ("extra", 1),
])
def test_budget_rejects_invalid_shape_and_counts(field, value):
    item = budget()
    item[field] = value
    with pytest.raises(ValueError, match="review_token_budget_invalid"):
        token_domain().validate_budget(item)


def test_budget_rejects_context_overcommit_and_returns_independent_copy():
    item = budget()
    result = token_domain().validate_budget(item)
    assert result == item and result is not item
    item["context_tokens"] = item["input_tokens"] + item["max_output_tokens"] + item["safety_tokens"] - 1
    with pytest.raises(ValueError, match="review_token_budget_invalid"):
        token_domain().validate_budget(item)


def test_full_serialized_request_not_body_alone_must_fit_input_budget():
    saved, _ = preparation("a" * 13000)
    saved["analysis_unit"]["budget"]["input_tokens"] = 13000
    with pytest.raises(ValueError, match="prepared_review_request_invalid"):
        frozen(saved)


@pytest.mark.parametrize("field,value", [("provider_name", "anthropic"), ("model", "other-model"), ("max_output_tokens", 2201)])
def test_document_request_must_match_budget_target_and_output(field, value):
    saved, _ = preparation()
    options = dict(provider_name="synthetic", model="synthetic-review-v1", max_output_tokens=2200)
    options[field] = value
    with pytest.raises(ValueError, match="prepared_review_request_invalid"):
        prepared_review.prepare(review_range_input.build_packet(saved), **options)


def test_context_and_outline_are_bound_but_not_evidence_or_reviewed_coverage():
    before, target, after = "앞선 문맥 ", "선택된 본문 e\u0301 🙂 " * 1000, " 다음 문맥"
    text = before + target + after
    saved, _ = preparation(text, start=len(before), end=len(before) + len(target),
        analysis_unit=unit(text, context_before=before, context_after=after, unit_count=2))
    packet = review_range_input.build_packet(saved)
    scope = review_range_input.validate_packet(packet)
    assert [span["text"] for span in packet["evidence_spans"]] == [target]
    assert [item["json_pointer"] for item in scope["components"]] == ["/evidence_spans/0/text"]
    assert scope["covered_stored_chars"] == len(target)
    assert scope["full_document_review"] == "NOT_ESTABLISHED"
    assert scope["document_coverage"] == "UNKNOWN"
    value = frozen(saved)
    assert "context" in value["provider_request"]["instructions"]
    assert "not the full paper" not in value["provider_request"]["instructions"]
    record = prepared_review.result(value, response(value))
    proof = record["validation"]["prepared_execution"]
    assert proof["schema_version"] == "PREPARED_REVIEW_RESULT_V4"
    assert proof["full_document_review"] == "NOT_ESTABLISHED"
    assert proof["scientific_support"] == "NOT_ASSESSED"
    assert prepared_review.validate_record(record, packet) == record


@pytest.mark.parametrize("field,value", [
    ("schema_version", "PAPER_ANALYSIS_UNIT_V0"), ("plan_id", "rplan_" + "A" * 64),
    ("plan_sha256", "invalid"), ("unit_index", True), ("unit_index", 1), ("unit_count", 0),
    ("unit_count", 257), ("document_chars", 0), ("label", " \t"),
    ("context_before", "🙂" * 1025), ("context_after", "\ud800"),
    ("outline", [dict(label="outside", start=0, end=999999)]),
    ("outline", [dict(label="", start=0, end=1)]),
    ("outline", [dict(label="valid", start=0, end=1)] * 257), ("extra", "unbound"),
])
def test_analysis_unit_is_bounded_and_strict(field, value):
    text = "x" * 13000
    item = unit(text)
    item[field] = value
    with pytest.raises(ValueError):
        preparation(text, analysis_unit=item)


def test_analysis_unit_document_size_cannot_disagree_with_source():
    text = "x" * 13000
    with pytest.raises(ValueError, match="review_preparation_integrity_invalid"):
        preparation(text, analysis_unit=unit(text, document_chars=len(text) + 1))


@pytest.mark.parametrize("version", ["PREPARED_REVIEW_REQUEST_V1", "PREPARED_REVIEW_REQUEST_V2", "PREPARED_REVIEW_REQUEST_V3"])
def test_document_packet_cannot_downgrade_to_contract_without_budget_checks(version):
    saved, _ = preparation()
    with pytest.raises(ValueError, match="prepared_review_request_invalid"):
        prepared_review.prepare(review_range_input.build_packet(saved), provider_name="synthetic",
            model="synthetic-review-v1", schema_version=version)


def test_legacy_packet_cannot_be_relabelled_as_document_request():
    with pytest.raises(ValueError, match="prepared_review_request_invalid"):
        prepared_review.prepare(legacy_packet(), provider_name="synthetic", model="synthetic-review-v1",
            schema_version="PREPARED_REVIEW_REQUEST_V4")


def test_document_contract_can_exceed_legacy_serialization_limit_within_budget():
    saved, _ = preparation("source " * 85000)
    value = frozen(saved)
    assert 1024 * 1024 < len(preserved_json(value).encode("utf-8")) <= 8 * 1024 * 1024
    assert prepared_review.validate_contract(value) == value
    assert prepared_review.validate_record(prepared_review.result(value, response(value)), value["packet"])


def test_repository_reader_uses_stored_schema_without_changing_record_identity():
    saved, body = preparation()
    row = dict(preparation_id=saved["preparation_id"], topic_id="topic", paper_version_id="version", actor_id="owner",
        request_id=body.request_id, run_id="source-run", paper_id="paper", request_json=preserved_json(body.model_dump()),
        payload_json=preserved_json(saved), payload_sha256=review_preparation.sha(preserved_json(saved)))
    assert review_preparations._read(row) == saved
    assert saved["preparation_id"] == review_preparation.identity("topic", "version", "owner", body.request_id)
    row["request_json"] = preserved_json({key: val for key, val in body.model_dump().items() if key != "analysis_unit"})
    with pytest.raises(ValueError, match="review_preparation_integrity_invalid"):
        review_preparations._read(row)


def test_new_preparation_requires_context_and_rejects_rehashed_analysis_mismatch():
    saved, body = preparation()
    original = deepcopy(saved)
    changed = deepcopy(saved)
    changed.pop("context")
    with pytest.raises(ValueError, match="review_preparation_integrity_invalid"):
        review_preparation.validate(changed, body)
    changed = deepcopy(saved)
    changed["analysis_unit"]["label"] += " changed"
    with pytest.raises(ValueError, match="review_preparation_integrity_invalid"):
        review_preparation.validate(changed, body)
    assert saved == original


@pytest.mark.parametrize("change", ["context", "outline", "budget", "plan", "scope"])
def test_rehashed_packet_and_contract_cannot_hide_analysis_metadata_changes(change):
    saved, _ = preparation()
    value = frozen(saved)
    metadata = value["packet"]["source_preparation"]["analysis_unit"]
    if change == "context":
        metadata["context_after"] = "Other text"
    elif change == "outline":
        metadata["outline"][0]["label"] = "Other heading"
    elif change == "budget":
        metadata["budget"]["max_output_tokens"] = 2201
    elif change == "plan":
        metadata["plan_sha256"] = "c" * 64
    else:
        value["packet"]["input_scope"]["components"].append(dict(
            json_pointer="/source_preparation/analysis_unit/context_before", text_sha256="a" * 64,
            chars=1, source_start=0, source_end=1, mapping="exact"))
    value["packet"]["input_scope"]["packet_payload_sha256"] = review_preparation.sha(preserved_json(
        {key: val for key, val in value["packet"].items() if key != "input_scope"}))
    value["contract_sha256"] = review_preparation.sha(preserved_json(
        {key: val for key, val in value.items() if key != "contract_sha256"}))
    with pytest.raises(ValueError, match="prepared_review_contract_invalid"):
        prepared_review.validate_contract(value)


def test_document_result_keeps_citation_and_body_classification_requirements():
    saved, _ = preparation()
    value = frozen(saved)
    item = response(value)
    item.payload.update(review_level="abstract_only", cited_evidence_span_ids=[])
    item.raw_text = preserved_json(item.payload)
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        prepared_review.result(value, item)
    assert "MISSING_CITATIONS" in prepared_review.output_rejection_reasons(value, item)


def test_historical_serializers_and_saved_results_are_unchanged():
    old_contract = legacy_contract()
    assert old_contract["schema_version"] == "PREPARED_REVIEW_REQUEST_V3"
    assert review_preparation.sha(preserved_json(old_contract)) == "7ed926ecd77a157e0fbaa4f0a4103431d76b12bfc1269e4dd975a098c0c6e629"
    assert review_preparation.sha(preserved_json(prepared_review.result(old_contract, response(old_contract)))) == "bc13e77e797be6ef96754a83f11366611c890945f0961386ffc1da21fcd93eee"
    for version in (1, 2):
        raw = (Path(__file__).parent / "fixtures" / f"prepared_review_v{version}.json").read_bytes()
        record = json.loads(raw)
        encoded_before = preserved_json(record)
        archived = record["validation"]["prepared_execution"]["contract"]
        assert prepared_review.validate_contract(archived) == archived
        assert prepared_review.validate_record(record, archived["packet"]) == record
        assert preserved_json(record) == encoded_before
        assert (Path(__file__).parent / "fixtures" / f"prepared_review_v{version}.json").read_bytes() == raw
    old = legacy_packet()["source_preparation"]
    with pytest.raises(ValueError):
        review_preparation.Prepare(request_id="old", run_id=old["source"]["run_id"], paper_id=old["source"]["paper_id"],
            expected_source_digest=old["source"]["source_digest"], start=0, end=12001)

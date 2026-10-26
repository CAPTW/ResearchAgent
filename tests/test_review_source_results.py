"""Saved response bindings are distinct from origin labels or prepared ranges."""
from contextlib import closing
from dataclasses import replace
import json

from app.db import bootstrap
from app.domain import prepared_review
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts
from app.services.providers import anthropic_prepared_review as native
from tests import test_prepared_review as drafts, test_review_source_coverage as coverage_tests

store = coverage_tests.store


def append_result(preparation, *, origin="PROVIDER_RESPONSE", legacy=False, native_marker=True):
    packet = build_packet(preparation)
    contract = prepared_review.prepare(packet, provider_name="anthropic", model="synthetic-requested-model",
        schema_version=prepared_review.SCHEMA_V2 if legacy else prepared_review.SCHEMA)
    response = replace(drafts.response(contract), provider_name="anthropic", model="synthetic-reported-model")
    if legacy:
        response.payload.update(review_level="abstract_only", cited_evidence_span_ids=[])
        response = replace(response, raw_text=json.dumps(response.payload, ensure_ascii=False))
    if native_marker:
        manifest = native.build_plan(contract, timeout_seconds=30, profile=native.MANIFEST_SCHEMA_V2,
            execution_surface="MOCK_TRANSPORT" if origin == "FIXTURE_SYNTHETIC" else "NATIVE_HTTP")[-1]
        record = native.transport_record(contract, response, manifest)
    else:
        record = prepared_review.result(contract, response)
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return ai_review_drafts.append_input_receipt_in(connection, record, input_packet=packet, input_origin=origin)


def test_results_require_validated_returned_binding_and_keep_legacy_class(store):
    first = coverage_tests.save(store, "body", 10, 17)
    second = coverage_tests.save(store, "legacy", 15, 23)
    body = append_result(first)
    old = append_result(second, legacy=True)
    generic = coverage_tests.append(first, "PROVIDER_RESPONSE")
    append_result(first, origin="FIXTURE_SYNTHETIC")
    append_result(second, origin="MANUAL_IMPORT_UNVERIFIED", native_marker=False)
    value = coverage_tests.coverage(store, first)
    groups = value["results"]
    assert groups["gap_semantics"] == "NO_VALIDATED_SAVED_RESULT"
    assert groups["attempt_history_included"] is False
    assert groups["validated_returned"]["record_count"] == 2
    assert groups["validated_returned"]["ranges"] == [dict(start=10, end=23)]
    assert groups["body_grounded"]["ranges"] == [dict(start=10, end=17)]
    assert groups["body_grounded"]["records"][0]["receipt_id"] == body["receipt_id"]
    assert groups["body_grounded"]["records"][0]["subtype"] == "NATIVE_ANTHROPIC_V2"
    assert groups["other_returned"]["records"][0]["receipt_id"] == old["receipt_id"]
    assert groups["other_returned"]["records"][0]["class"] == "OTHER_RETURNED"
    assert groups["unverified_provider"]["records"][0]["receipt_id"] == generic["receipt_id"]
    assert groups["body_grounded"]["gaps"] == [dict(start=0, end=10), dict(start=17, end=len(store["text"]))]
    assert value["prepared"]["record_count"] == 2
    assert {group["input_origin"]: group["record_count"] for group in value["inputs"]} == {
        "PROVIDER_RESPONSE": 3, "FIXTURE_SYNTHETIC": 1, "MANUAL_IMPORT_UNVERIFIED": 1}
    assert value["full_document_review"] == "NOT_ESTABLISHED"


def test_preparation_and_generic_origin_do_not_close_saved_result_gaps(store):
    preparation = coverage_tests.save(store, "prepared-only", 0, len(store["text"]))
    coverage_tests.append(preparation, "PROVIDER_RESPONSE")
    value = coverage_tests.coverage(store, preparation)
    assert value["prepared"]["gaps"] == []
    assert value["results"]["validated_returned"]["ranges"] == []
    assert value["results"]["body_grounded"]["gaps"] == [dict(start=0, end=len(store["text"]))]

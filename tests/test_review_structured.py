"""The real app review path consumes and preserves the structured contract."""
import json
from contextlib import closing
from copy import deepcopy

import pytest

from app.db import bootstrap
from app.domain.journal_exchange import preserved_json
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_offline as repository
from app.repositories.files import iso_now_utc
from app.services import ai_review
from app.services import review_offline as service
from app.services.ai_provider import ProviderRequest
from app.services.providers.base import ProviderRequest as StructuredRequest
from tests import test_review_offline as offline_tests

linked = offline_tests.linked
seeded = offline_tests.seeded
prepared = offline_tests.prepared


def test_app_result_preserves_the_exact_structured_request_and_response(prepared):
    context, preparation, _ = prepared
    saved = offline_tests.execute(prepared, service.OfflineReviewRuntime())
    receipt = ai_review_drafts.get_input_receipt(context["topic"], saved["receipt_id"])
    assert receipt is not None
    proof = receipt["validation"]["prepared_execution"]
    assert proof["schema_version"] == "PREPARED_REVIEW_RESULT_V3"
    request = proof["contract"]["provider_request"]
    assert json.loads(request["prompt"]) == receipt["packet"]
    assert receipt["packet"]["evidence_spans"][0]["text"] == preparation["selection"]["text"]
    assert json.loads(proof["response"]["raw_text"]) == receipt["draft"]
    assert proof["human_adoption"] == "NOT_ESTABLISHED"
    assert proof["full_document_review"] == "NOT_ESTABLISHED"
    assert saved["adapter_version"] == "2"
    assert offline_tests.execute(prepared) == saved


def test_app_passes_the_exact_request_to_the_fixed_adapter(prepared, monkeypatch):
    captured = []
    original = service.SyntheticStructuredReviewProvider.generate_json
    def observe(self, request):
        from dataclasses import asdict
        captured.append(deepcopy(asdict(request)))
        return original(self, request)
    monkeypatch.setattr(service.SyntheticStructuredReviewProvider, "generate_json", observe)
    saved = offline_tests.execute(prepared, service.OfflineReviewRuntime())
    receipt = ai_review_drafts.get_input_receipt(prepared[0]["topic"], saved["receipt_id"])
    assert captured == [receipt["validation"]["prepared_execution"]["contract"]["provider_request"]]
    assert offline_tests.execute(prepared) == saved
    assert len(captured) == 1


@pytest.mark.parametrize("field,value", [("schema_name", "JOURNAL_PROPOSAL_V1"), ("instructions", "changed"),
    ("schema", {}), ("max_output_tokens", 8000), ("metadata", {"approved": True}), ("temperature", 1)])
def test_fixed_adapter_rejects_changed_request_before_generation(prepared, monkeypatch, field, value):
    from app.domain import prepared_review
    contract = prepared_review.prepare(build_packet(prepared[1]), provider_name=service.ADAPTER, model=service.MODEL)
    request = deepcopy(contract["provider_request"])
    request[field] = value
    monkeypatch.setattr(service.SyntheticReviewProvider, "generate", lambda *a: pytest.fail("no generation"))
    with pytest.raises(ValueError, match="review_offline_target_mismatch"):
        service.SyntheticStructuredReviewProvider().generate_json(StructuredRequest(**request))


@pytest.mark.parametrize("change", ["provider", "model", "raw", "extra", "span", "blank", "mutated_request"])
def test_invalid_structured_response_never_partially_saves(prepared, monkeypatch, change):
    before = offline_tests.database()
    original = service.SyntheticStructuredReviewProvider.generate_json
    def invalid(self, request):
        response = original(self, request)
        if change == "provider":
            response.provider_name = "different"
        elif change == "model":
            response.model = "live-model"
        elif change == "raw":
            response.raw_text = "{}"
        elif change == "extra":
            response.payload["invented_field"] = "not in schema"
            response.raw_text = preserved_json(response.payload)
        elif change == "span":
            response.payload["cited_evidence_span_ids"] = ["not-in-selected-input"]
            response.raw_text = preserved_json(response.payload)
        elif change == "blank":
            response.payload["method_summary"] = " \t\n\u3000"
            response.raw_text = preserved_json(response.payload)
        else:
            request.schema.clear()
            request.metadata.clear()
            request.prompt = "mutated by adapter"
        return response
    monkeypatch.setattr(service.SyntheticStructuredReviewProvider, "generate_json", invalid)
    with pytest.raises(ValueError):
        offline_tests.execute(prepared, service.OfflineReviewRuntime())
    assert offline_tests.database() == before


def seed_v1(prepared):
    """Write the unchanged historical V1 payload into disposable fixture storage."""
    context, preparation, body = prepared
    packet = build_packet(preparation)
    response = service.SyntheticReviewProvider().generate(ProviderRequest(
        provider_name=service.ADAPTER, packet=packet, mode="stub", run_id=context["run"]))
    validation = ai_review.validate_ai_review_draft(packet, response.parsed_draft)
    validation["execution"] = dict(mode=service.MODE, adapter_id=service.ADAPTER, adapter_version="1",
                                   scientific_assessment="NOT_ASSESSED")
    record = dict(topic_id=context["topic"], run_id=context["run"], paper_id=context["paper"],
                  source_text_sha256=packet["source_text_sha256"], review_level=packet["review_level"],
                  status=validation["status"], draft=validation["normalized_draft"], validation=validation)
    actor = context["actor"].subject
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        receipt = ai_review_drafts.append_input_receipt_in(c, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        result = dict(execution_id=repository._identity(context["topic"], context["version"], actor, body.request_id),
                      preparation_id=preparation["preparation_id"], receipt_id=receipt["receipt_id"], actor_id=actor,
                      created_at=iso_now_utc(), state="RESULT_SAVED", mode=service.MODE, adapter_id=service.ADAPTER, adapter_version="1")
        envelope = dict(schema_version="REVIEW_OFFLINE_RESULT_V1", topic_id=context["topic"], paper_version_id=context["version"],
                        request=body.model_dump(), result=result)
        payload = encoded(envelope)
        repository._initialize(c)
        c.execute("""INSERT INTO paper_review_offline_results
            (execution_id,topic_id,paper_version_id,preparation_id,actor_id,request_id,payload_json,payload_sha256)
            VALUES (?,?,?,?,?,?,?,?)""", (result["execution_id"], context["topic"], context["version"], preparation["preparation_id"],
                                         actor, body.request_id, payload, sha(payload)))
    return result, receipt


def test_v1_replay_is_unchanged_and_new_v2_coexists(prepared):
    result, receipt = seed_v1(prepared)
    before = offline_tests.database()
    assert offline_tests.execute(prepared) == result
    assert offline_tests.execute(prepared, service.OfflineReviewRuntime()) == result
    assert offline_tests.database() == before
    assert "prepared_execution" not in receipt["validation"]
    new = offline_tests.execute(prepared, service.OfflineReviewRuntime(), body=prepared[2].model_copy(update={"request_id": "v2"}))
    assert new["adapter_version"] == "2" and new["receipt_id"] != result["receipt_id"]
    assert ai_review_drafts.get_input_receipt(prepared[0]["topic"], result["receipt_id"]) == receipt
    assert offline_tests.execute(prepared) == result


def test_v2_marker_requires_structured_proof_at_receipt_boundary(prepared):
    packet = build_packet(prepared[1])
    record = service.OfflineReviewRuntime().generate_record(packet)
    del record["validation"]["prepared_execution"]
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="prepared_review_record_invalid"):
            ai_review_drafts.append_input_receipt_in(c, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")


def test_journal_retains_full_receipt_locally_and_projects_only_identity_summary(prepared):
    from app.domain import journal_exchange
    from app.repositories import local_synthesis
    from app.services import paper_bank
    context, _, _ = prepared
    saved = offline_tests.execute(prepared, service.OfflineReviewRuntime())
    receipt = ai_review_drafts.get_input_receipt(context["topic"], saved["receipt_id"])
    context["path"].unlink()
    item = next(item for item in paper_bank.list_topic_papers(context["topic"], page_size=100)["items"]
                if item["paperVersionId"] == context["version"])
    job, created = local_synthesis.create(context["topic"], [dict(
        paper_version_id=context["version"], metadata_digest=item["metadataDigest"],
        review_input=dict(receipt_id=saved["receipt_id"], component_ids=["/evidence_spans/0/text"]))], "structured", context["actor"])
    assert created
    for step in range(job["total_steps"]):
        job, advanced = local_synthesis.advance(context["topic"], job["job_id"], step)
        assert advanced
    assert job["inputs"]["papers"][0]["review_input"]["receipt"] == receipt
    packet = journal_exchange.packet(job)
    attachment = packet["review_inputs"][0]
    proof = receipt["validation"]["prepared_execution"]
    summary = attachment["validation"]["prepared_execution"]
    assert attachment["schema_version"] == "JOURNAL_REVIEW_INPUT_V2"
    assert summary["schema_version"] == "PREPARED_REVIEW_RESULT_SUMMARY_V1"
    assert summary["contract_sha256"] == proof["contract"]["contract_sha256"]
    assert summary["request_sha256"] == proof["contract"]["request_sha256"]
    assert summary["raw_text_sha256"] == proof["response"]["raw_text_sha256"]
    assert summary["independent_live_proof"] == "NOT_VERIFIED"
    prompt = journal_exchange.provider_prompt(packet)
    assert "provider_request" not in prompt and "raw_text\"" not in prompt
    references = [reference for reference in packet["references"] if reference["kind"] == "review_input_excerpt"]
    assert [reference["text"] for reference in references] == [prepared[1]["selection"]["text"]]

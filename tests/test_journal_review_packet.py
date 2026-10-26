"""Selected historical review input stays exact throughout Journal exchange."""
import json

import pytest

from app.domain import journal_exchange as exchange, journal_execution as execution, local_journal
from app.domain import generated_journal, journal_review
from app.domain.temporal_memory import digest
from app.repositories import ai_review_drafts, journal_candidates
from app.services.providers.base import ProviderResult
from tests import test_journal_execution as execution_tests, test_journal_reviews as review_tests
from tests.test_ai_review_input_history import input_and_record
from tests.test_synthesis_review_inputs import finish, receipt_selection


TEXT = "e\u0301 한글🙂 preserved input"
NOW = "2026-09-28T00:00:00+00:00"
source = execution_tests.source


def frozen_job(*, selected=True, origin="FIXTURE_SYNTHETIC"):
    packet, record = input_and_record(paper="paper-v1", text=TEXT, result="Prior interpretation, not source text")
    record.update(created_at=NOW, imported_at=NOW)
    content = ai_review_drafts._receipt_content(ai_review_drafts._normalize_record(record), packet, origin)
    receipt = dict(receipt_id=ai_review_drafts._receipt_id(content), recorded_at=NOW, **content)
    paper = dict(paper_version_id="pv1", canonical_paper_id="paper-v1", title="Paper", metadata_digest="a" * 64,
                 source_identity={"doi": "synthetic"}, review_state="partial", rights=dict(state="unknown", label=None),
                 review_source_run_id="latest-run", source_text_digest="b" * 64, abstract="Stored abstract", evidence=[])
    if selected:
        paper["review_input"] = dict(schema_version="FROZEN_REVIEW_INPUT_V1", receipt=receipt,
                                     selected_component_ids=["/selected_text_excerpts/0/text"])
    inputs = dict(schema_version="LOCAL_EVIDENCE_SYNTHESIS_V2" if selected else "LOCAL_EVIDENCE_SYNTHESIS_V1",
                  topic_id="topic", topic_label="Synthetic topic", source_snapshot_id="snapshot", papers=[paper])
    return dict(job_id="syn_" + digest(inputs), topic_id="topic", input_digest=digest(inputs), inputs=inputs, candidate={})


def prepare(job):
    body = execution.Prepare(request_id="prepare", expected_input_digest=job["input_digest"], provider_name="test", model="synthetic")
    return execution.prepare(job, body, "owner", NOW)


def proposal(frozen):
    ref = next((ref for ref in frozen["references"] if ref["kind"] == "review_input_excerpt"), frozen["references"][0])
    return dict(schema_version="JOURNAL_PROPOSAL_V1", packet_id=frozen["packet_id"], title="Selected historical evidence",
                sections=[dict(kind="synthesis", entries=[dict(kind="interpretation", text="Limited interpretation",
                                citation_ids=[ref["citation_id"]])])])


def row(candidate):
    return dict(candidate_id=candidate["candidate_id"], payload_json=exchange.preserved_json(candidate), payload_digest=digest(candidate))


def test_v1_packet_and_reference_identity_remain_unchanged():
    job = frozen_job(selected=False)
    frozen = exchange.packet(job)
    assert frozen["schema_version"] == "FROZEN_JOURNAL_PACKET_V1"
    assert set(frozen) == {"packet_id", "schema_version", "job_id", "topic_id", "input_digest", "source_snapshot_id", "topic_label",
                           "references", "selected_paper_version_ids", "output_schema", "instructions", "execution"}
    assert len(frozen["instructions"]) == 7
    assert frozen["references"][0]["citation_id"] == "aref_" + digest(["pv1", "Stored abstract"])
    contract = prepare(job)
    assert set(json.loads(contract["provider_request"]["prompt"])) == {"packet_id", "topic_label", "selected_paper_version_ids", "references"}
    execution.validate_contract(contract, job)


def test_v2_adds_exact_selected_ranges_and_prior_interpretation_to_actual_prompt():
    job = frozen_job()
    frozen = exchange.packet(job)
    assert frozen["schema_version"] == "FROZEN_JOURNAL_PACKET_V2"
    assert frozen["references"][0] == exchange.packet(frozen_job(selected=False))["references"][0]
    ref = next(ref for ref in frozen["references"] if ref["kind"] == "review_input_excerpt")
    assert ref["citation_id"].startswith("riref_") and ref["text"] == TEXT
    assert frozen["review_inputs"][0]["receipt_id"] == job["inputs"]["papers"][0]["review_input"]["receipt"]["receipt_id"]
    assert frozen["review_inputs"][0]["input_scope"]["full_document_review"] == "NOT_ESTABLISHED"
    contract = prepare(job)
    prompt = json.loads(contract["provider_request"]["prompt"])
    assert prompt["references"] == frozen["references"] and prompt["review_inputs"] == frozen["review_inputs"]
    assert TEXT in contract["provider_request"]["prompt"]
    assert "Prior interpretation, not source text" in contract["provider_request"]["prompt"]
    execution.validate_contract(contract, job)


@pytest.mark.parametrize("change", ["omit", "text", "instructions", "downgrade", "attachment"])
def test_v2_contract_rejects_prompt_or_packet_detachment_even_with_new_digest(change):
    job = frozen_job()
    contract = prepare(job)
    if change in {"omit", "text"}:
        prompt = json.loads(contract["provider_request"]["prompt"])
        if change == "omit":
            prompt.pop("review_inputs", None)
        else:
            prompt["references"][0]["text"] = "Different supplied text"
        contract["provider_request"]["prompt"] = exchange.preserved_json(prompt)
    elif change == "instructions":
        contract["provider_request"]["instructions"] = "Do not include coverage limitations"
    elif change == "downgrade":
        contract["packet"]["schema_version"] = "FROZEN_JOURNAL_PACKET_V1"
    else:
        contract["packet"]["review_inputs"] = []
    contract["packet"]["packet_id"] = "jpacket_" + digest({k: v for k, v in contract["packet"].items() if k != "packet_id"})
    with pytest.raises(ValueError, match="journal_execution_integrity_invalid"):
        execution.validate_contract(contract, job)


@pytest.mark.parametrize("change", ["receipt", "missing", "downgrade", "coerced_count", "extra", "coverage"])
def test_v2_candidate_read_rejects_detached_attachment_without_renderer(change):
    job = frozen_job()
    frozen = exchange.packet(job)
    value = exchange.assemble(job, exchange.preserved_json(proposal(frozen)), "owner", NOW)
    if change == "receipt":
        value["packet"].setdefault("review_inputs", [dict(receipt_id="unused")])[0]["receipt_id"] = "aireceipt_" + "0" * 64
    elif change == "missing":
        value["packet"].pop("review_inputs", None)
    elif change == "downgrade":
        value["packet"]["schema_version"] = "FROZEN_JOURNAL_PACKET_V1"
    elif change == "coerced_count":
        component = value["packet"]["review_inputs"][0]["input_scope"]["components"][0]
        component["chars"] = float(component["chars"])
    elif change == "extra":
        value["packet"]["unbound_input"] = "Input not included in the selected frozen scope"
    else:
        value["coverage"]["review_input_excerpt_citations"] = 0
    value["packet"]["packet_id"] = "jpacket_" + digest({k: v for k, v in value["packet"].items() if k != "packet_id"})
    value["proposal"]["packet_id"] = value["packet"]["packet_id"]
    value["raw_json"] = exchange.preserved_json(value["proposal"])
    value["raw_sha256"] = exchange.sha(value["raw_json"])
    value["candidate_id"] = "jcand_" + digest([job["job_id"], value["raw_sha256"]])
    with pytest.raises(ValueError, match="journal_candidate_integrity_invalid"):
        journal_candidates._read(row(value), job)


def test_exact_candidate_reopens_without_live_renderer_and_retains_origin_in_report(monkeypatch):
    job = frozen_job(origin="MANUAL_IMPORT_UNVERIFIED")
    value = exchange.assemble(job, exchange.preserved_json(proposal(exchange.packet(job))), "owner", NOW)
    assert value["coverage"]["review_input_excerpt_citations"] == 1
    assert "MANUAL_IMPORT_UNVERIFIED" in value["markdown"] and "NOT_ESTABLISHED" in value["markdown"]
    monkeypatch.setattr(exchange, "packet", lambda *_: pytest.fail("Saved packet must not be regenerated"))
    assert journal_candidates._read(row(value), job) == value
    review = dict(review_id="review", actor_id="owner", created_at=NOW, candidate_digest=digest(value), note="",
                  entries=[dict(entry_id=entry["entry_id"], assessment="limited", note="Partial supplied source") for entry in journal_review.entries(value)])
    report = journal_review.report(value, review)
    assert TEXT in report["content"] and "MANUAL_IMPORT_UNVERIFIED" in report["content"]


def test_generated_candidate_and_local_manuscript_preserve_selected_input_unicode():
    job = frozen_job()
    contract = prepare(job)
    raw = exchange.preserved_json(proposal(contract["packet"]))
    result = execution.result(contract, ProviderResult(provider_name="test", model="synthetic", response_id="fixture", payload=json.loads(raw), raw_text=raw))
    event = dict(state="SUCCEEDED", result=result, attempt=1, at=NOW)
    value = generated_journal.assemble(contract, event, "owner", NOW)
    assert value["coverage"]["review_input_excerpt_citations"] == 1
    assert "FIXTURE_SYNTHETIC" in value["markdown"]
    generated_journal.validate(value, job)
    refs = local_journal.references(job)
    body = local_journal.Save(request_id="save", expected_input_digest=job["input_digest"], title="Human draft", entries=[
        local_journal.Entry(kind="observation", text="Human observation", citation_ids=[refs[-1]["citation_id"]])])
    manuscript = local_journal.assemble(job, body, "owner", NOW, "version")
    assert manuscript["citations"][-1]["text"] == TEXT and TEXT in manuscript["markdown"]


def test_review_receipt_payload_tampering_is_not_a_new_valid_frozen_packet():
    job = frozen_job()
    job["inputs"]["papers"][0]["review_input"]["receipt"]["packet"]["selected_text_excerpts"][0]["text"] = "Changed"
    with pytest.raises(ValueError):
        exchange.packet(job)


def completed_selected(source):
    from app.auth.resource_access import resource_actor_for_user
    from app.repositories import local_synthesis
    selected, receipt = receipt_selection(source, text=TEXT)
    job = finish(local_synthesis.create("topic", selected, "journal-source", resource_actor_for_user(None))[0])
    return job, receipt


def test_repository_manuscript_import_review_and_export_reopen_exactly(source, monkeypatch):
    from app.repositories import local_journal as local_repository, journal_reviews
    job, receipt = completed_selected(source)
    args = ("topic", job["job_id"])
    frozen = journal_candidates.input_packet(*args)
    ref = next(ref for ref in frozen["references"] if ref["kind"] == "review_input_excerpt")
    body = local_journal.Save(request_id="manuscript", expected_input_digest=job["input_digest"], title="Exact local draft", entries=[
        local_journal.Entry(kind="observation", text="Human bounded note", citation_ids=[ref["citation_id"]])])
    manuscript, _ = local_repository.save(*args, body, "owner")
    assert local_repository.detail(*args, manuscript["version_id"]) == manuscript
    bundle = json.loads(local_repository.export(*args, manuscript["version_id"], "json")["content"])
    assert bundle["frozen_input"]["papers"][0]["review_input"]["receipt"] == receipt
    assert bundle["version"]["citations"][0]["text"] == TEXT
    raw = exchange.preserved_json(proposal(frozen))
    candidate, _ = journal_candidates.save(*args, raw, "owner")
    accepted, _ = review_tests.save(candidate, review_tests.body(candidate))
    candidate_args = (*args, candidate["candidate_id"])
    source.clear()
    monkeypatch.setattr(exchange, "packet", lambda *_: pytest.fail("Do not regenerate saved packet"))
    assert journal_candidates.detail(*candidate_args) == candidate
    assert journal_candidates.save(*args, raw, "owner") == (candidate, False)
    bundle = json.loads(journal_reviews.export(*candidate_args, accepted["review_id"], "json")["content"])
    assert bundle["candidate"]["packet"] == frozen and bundle["review"] == accepted
    assert TEXT in journal_reviews.export(*candidate_args, accepted["review_id"], "markdown")["content"]


def test_repository_synthetic_execution_preserves_actual_prompt_and_replays_without_call(source):
    from app.repositories import journal_executions, journal_reviews
    job, receipt = completed_selected(source)
    saved, _ = execution_tests.prepare(job)
    requested = []
    def generate(request):
        prompt = json.loads(request.prompt)
        requested.append(prompt)
        assert prompt["review_inputs"][0]["receipt_id"] == receipt["receipt_id"]
        assert next(ref for ref in prompt["references"] if ref["kind"] == "review_input_excerpt")["text"] == TEXT
        payload = proposal(saved["contract"]["packet"])
        return execution_tests.response(saved, payload=payload, raw_text=exchange.preserved_json(payload))
    provider = execution_tests.Fake(generate)
    claim = execution_tests.claim(saved)
    succeeded = execution_tests.execute(claim, provider)
    assert succeeded["current"]["state"] == "SUCCEEDED" and len(requested) == 1
    assert execution_tests.execute(claim, provider) == succeeded and provider.calls == 1
    args = execution_tests.scope(saved)
    candidate, created = journal_candidates.save_execution(*args, digest(succeeded["current"]["result"]), "owner")
    assert created and candidate["coverage"]["review_input_excerpt_citations"] == 1
    accepted, _ = review_tests.save(candidate, review_tests.body(candidate))
    bundle = json.loads(journal_reviews.export(*args[:2], candidate["candidate_id"], accepted["review_id"], "json")["content"])
    assert bundle["candidate"] == candidate and bundle["review"] == accepted
    assert journal_executions.detail(*args) == succeeded

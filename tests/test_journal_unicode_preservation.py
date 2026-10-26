"""Original journal bytes must survive the complete local preservation flow."""
import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.domain import journal_execution, journal_exchange
from app.domain.temporal_memory import digest
from app.repositories import journal_candidates as candidates, journal_executions as executions, journal_reviews as reviews
from tests import test_journal_candidates as imports, test_journal_reviews as review_tests, test_journal_execution as execution_tests

source = imports.source
completed = imports.completed
candidate = review_tests.candidate
execution = execution_tests.execution

TEXT = "  e\u0301 · \u1100\u1161 · \u212b\n확인이 필요한 해석  "


@pytest.mark.parametrize("escaped", [False, True])
def test_import_retains_raw_file_proposal_hashes_and_distinct_normalized_file(completed, escaped):
    packet = candidates.input_packet("topic", completed["job_id"])
    proposal = imports.output(packet, TEXT)
    proposal["sections"][0]["entries"][0]["text"] = TEXT
    raw = "\ufeff" + json.dumps(proposal, ensure_ascii=escaped, indent=2) + "\r\n"
    first, created = candidates.save("topic", completed["job_id"], raw, "importe\u0301r")
    assert created and first["raw_json"] == raw and first["proposal"] == proposal
    assert candidates.detail("topic", completed["job_id"], first["candidate_id"]) == first
    assert candidates.save("topic", completed["job_id"], raw, "another-reader") == (first, False)
    assert first["raw_sha256"] == journal_exchange.sha(raw)
    assert first["content_sha256"] == journal_exchange.sha(first["markdown"])
    normalized = json.loads(unicodedata.normalize("NFC", json.dumps(proposal, ensure_ascii=False)))
    other_raw = "\ufeff" + json.dumps(normalized, ensure_ascii=escaped, indent=2) + "\r\n"
    second, _ = candidates.save("topic", completed["job_id"], other_raw, "importer")
    assert second["candidate_id"] != first["candidate_id"]
    assert candidates.listing("topic", completed["job_id"])["total"] == 2


@pytest.mark.parametrize("state", ["reviewing", "accepted"])
def test_review_notes_actor_report_and_export_read_back_exactly(candidate, state):
    args = ("topic", candidate["job_id"], candidate["candidate_id"])
    body = review_tests.body(candidate, state=state)
    body.note = TEXT
    body.entries[0].note = TEXT
    value, created = reviews.save(*args, body, "reviewe\u0301r")
    assert created and value["note"] == TEXT and value["entries"][0]["note"] == TEXT
    assert reviews.detail(*args, value["review_id"]) == value
    assert reviews.save(*args, body, "reviewe\u0301r") == (value, False)
    if state == "accepted":
        assert value["report"]["content_sha256"] == journal_exchange.sha(value["report"]["content"])
        bundle = json.loads(reviews.export(*args, value["review_id"], "json")["content"])
        assert bundle["review"] == value and bundle["candidate"] == candidate
        assert reviews.export(*args, value["review_id"], "markdown")["content"] == value["report"]["content"]


def test_execution_preparation_retains_exact_specification_actor_and_retry(completed):
    body = journal_execution.Prepare(request_id="unicode-prepare", expected_input_digest=completed["input_digest"], provider_name="test", model="offline-e\u0301")
    value, _ = executions.prepare("topic", completed["job_id"], body, "owne\u0301r", now=execution_tests.NOW)
    assert executions.detail(*execution_tests.scope(value)) == value
    assert executions.prepare("topic", completed["job_id"], body, "owne\u0301r", now=execution_tests.NOW) == (value, False)


def test_recorded_response_promotion_review_and_export_preserve_original_bytes(execution):
    payload = imports.output(execution["contract"]["packet"], TEXT)
    payload["sections"][0]["entries"][0]["text"] = TEXT
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\r\n"
    fake = execution_tests.Fake(lambda _: execution_tests.response(execution, payload=payload, raw_text=raw, response_id="response-e\u0301"))
    saved = execution_tests.execute(execution_tests.claim(execution), fake)
    assert saved["current"]["state"] == "SUCCEEDED" and fake.calls == 1
    args = execution_tests.scope(execution)
    assert executions.detail(*args) == saved
    result = saved["current"]["result"]
    assert result["raw_json"] == raw and result["proposal"] == payload
    preserved, created = candidates.save_execution(*args, digest(result), "preserve\u0301r")
    assert created and candidates.detail(*args[:2], preserved["candidate_id"]) == preserved
    assert candidates.save_execution(*args, digest(result), "preserve\u0301r") == (preserved, False)
    review, _ = review_tests.save(preserved, review_tests.body(preserved))
    bundle = json.loads(reviews.export(*args[:2], preserved["candidate_id"], review["review_id"], "json")["content"])
    assert bundle["candidate"] == preserved and bundle["review"] == review
    assert bundle["candidate"]["raw_json"] == raw
    assert executions.detail(*args) == saved


def test_concurrent_unicode_import_and_acceptance_keep_one_exact_record(completed):
    packet = candidates.input_packet("topic", completed["job_id"])
    raw = json.dumps(imports.output(packet, TEXT), ensure_ascii=False)
    barrier = Barrier(6)
    def save_import(_):
        barrier.wait()
        return candidates.save("topic", completed["job_id"], raw, "importe\u0301r")
    with ThreadPoolExecutor(6) as pool:
        imported = list(pool.map(save_import, range(6)))
    assert sum(created for _, created in imported) == 1
    candidate = imported[0][0]
    assert all(value == candidate for value, _ in imported)
    args = ("topic", candidate["job_id"], candidate["candidate_id"])
    body = review_tests.body(candidate)
    body.note = TEXT
    body.entries[0].note = TEXT
    def save_review(_):
        barrier.wait()
        return reviews.save(*args, body, "reviewe\u0301r")
    with ThreadPoolExecutor(6) as pool:
        accepted = list(pool.map(save_review, range(6)))
    assert sum(created for _, created in accepted) == 1
    review = accepted[0][0]
    assert all(value == review for value, _ in accepted)
    assert reviews.history(*args)["total"] == 1
    assert json.loads(reviews.export(*args, review["review_id"], "json")["content"])["candidate"]["raw_json"] == raw

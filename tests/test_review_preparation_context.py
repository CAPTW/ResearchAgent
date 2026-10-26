"""Frozen research context travels with the selected body, never a live call."""
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
import unicodedata

import pytest

from app.db import bootstrap
from app.domain import prepared_review, review_context, review_preparation as domain, review_range_input
from app.domain.review_input import validate_packet
from app.repositories import ai_review_drafts, review_preparations, review_source_coverage
from app.services import paper_bank, review_preparation, topic_profiles
from tests import test_review_preparation as preparation_tests
from tests.test_review_range_input import preparation, receipt_record

linked = preparation_tests.linked
seeded = preparation_tests.seeded
selected = preparation_tests.selected


def context():
    return dict(schema_version="REVIEW_CONTEXT_V1", topic_title="주제 e\u0301", topic_name="XR 공간 이해 e\u0301",
                paper_title="Later methods e\u0301 한글🙂", authors=["연구자 e\u0301"], published_at="2026-09-28",
                metadata_digest="a" * 64, topic_updated_at="2026-10-05T00:00:00Z")


def contextual_preparation():
    return dict(preparation(), schema_version=domain.CONTEXT_SCHEMA, context=context())


def test_prepared_request_contains_the_actual_topic_and_paper_context(linked):
    profile = topic_profiles.get_topic_profile(linked["topic"], actor=linked["actor"])
    item = next(item for item in paper_bank.list_topic_papers(linked["topic"], page_size=100)["items"]
                if item["paperVersionId"] == linked["version"])
    saved = review_preparation.prepare(linked["topic"], linked["version"], selected(linked), linked["actor"])
    packet = review_range_input.build_packet(saved)
    assert packet["topic_name"] == profile["topic"]
    assert packet["title"] == item["title"]
    assert packet["authors"] == item["authors"]
    request = prepared_review.prepare(packet, provider_name="synthetic-context-test", model="not-a-live-model")
    assert json.loads(request["provider_request"]["prompt"]) == packet
    assert packet["abstract"] is None
    assert packet["evidence_spans"][0]["text"] == saved["selection"]["text"]


def test_legacy_preparation_packet_and_request_are_byte_identical():
    old = preparation()
    packet = review_range_input.build_packet(old)
    contract = prepared_review.prepare(packet, provider_name="synthetic-context-test", model="not-a-live-model",
                                       schema_version=prepared_review.LEGACY_SCHEMA)
    # Captured from unmodified 3b9ced5 with V1. New request defaults can advance;
    # historical reconstruction must keep that explicit version and these hashes.
    assert {name: domain.sha(domain.encoded(value)) for name, value in
            [("preparation", old), ("packet", packet), ("contract", contract)]} == {
        "preparation": "622b5b332e746bf0f54b33aa39101f43c1b336e4af725f2bb01c59a7d5d364e0",
        "packet": "fa074cddebc6aa29052a8b929415581d9122a7d1e42f098ac733eb0ae419038d",
        "contract": "b681e1a0c71fd3ddb1225215fe29fed7b61815f4fcd5dbaf93df754cfb05cdf4"}


def test_new_context_is_exact_metadata_outside_body_coverage():
    saved = contextual_preparation()
    packet = review_range_input.build_packet(saved)
    assert packet["packet_version"] == "ai-review-packet-v4"
    assert packet["source_preparation"]["schema_version"] == "REVIEW_PREPARATION_BINDING_V2"
    assert packet["source_preparation"]["context"] == context()
    assert packet["source_preparation"]["preparation_sha256"] == domain.sha(domain.encoded(saved))
    assert packet["title"] == context()["paper_title"]
    assert packet["topic_name"] == context()["topic_name"]
    assert packet["title"] != unicodedata.normalize("NFC", packet["title"])
    scope = validate_packet(packet)
    assert scope["covered_stored_chars"] == saved["selection"]["chars"]
    assert [item["json_pointer"] for item in scope["components"]] == ["/evidence_spans/0/text"]
    assert scope["full_document_review"] == "NOT_ESTABLISHED"
    assert packet["abstract"] is None and packet["existing_review"] == {}


@pytest.mark.parametrize("change", ["title", "binding_version", "packet_version", "missing_context", "context_extra"])
def test_inconsistent_context_or_version_is_rejected_even_with_new_payload_hash(change):
    packet = review_range_input.build_packet(contextual_preparation())
    if change == "title":
        packet["title"] = "different current title"
    elif change == "binding_version":
        packet["source_preparation"]["schema_version"] = "REVIEW_PREPARATION_BINDING_V1"
    elif change == "packet_version":
        packet["packet_version"] = "ai-review-packet-v3"
    elif change == "missing_context":
        del packet["source_preparation"]["context"]
    else:
        packet["source_preparation"]["context"]["abstract"] = "unselected input"
    packet["input_scope"]["packet_payload_sha256"] = domain.sha(domain.encoded(
        {key: value for key, value in packet.items() if key != "input_scope"}))
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        validate_packet(packet)


@pytest.mark.parametrize("patch", [{"topic_name": " "}, {"paper_title": "x" * 8001}, {"authors": [2]},
    {"authors": ["x" * 1001]}, {"authors": ["x"] * 501}, {"topic_name": "\ud800"},
    {"metadata_digest": "bad"}, {"published_at": "2026-02-30"}, {"authors": ["x" * 1000] * 100}])
def test_invalid_or_oversized_context_is_never_silently_truncated(patch):
    with pytest.raises(ValueError, match="review_preparation_context_invalid"):
        review_context.validate({**context(), **patch})


@pytest.mark.parametrize("published", [None, "bad-date", "2026-02-30"])
def test_missing_publication_does_not_use_paper_update_date(published):
    value = review_context.capture(dict(title="Topic", topic="Research", updated_at="2026-10-05"),
        dict(title="Paper", authors=[], metadataDigest="a" * 64, publishedAt="2026-10-04", _raw={"published": published}))
    assert value["published_at"] is None


def test_replay_keeps_first_context_and_new_request_captures_new_label(linked):
    body = selected(linked)
    first = review_preparation.prepare(linked["topic"], linked["version"], body, linked["actor"])
    topic_profiles.update_topic_profile(linked["topic"], {"title": "새 표시명 e\u0301"}, actor=linked["actor"])
    assert review_preparation.prepare(linked["topic"], linked["version"], body, linked["actor"]) == first
    second = review_preparation.prepare(linked["topic"], linked["version"], body.model_copy(update={"request_id": "next"}), linked["actor"])
    assert second["context"]["topic_title"] == "새 표시명 e\u0301"
    assert first["context"]["topic_title"] != second["context"]["topic_title"]
    assert first["source"] == second["source"] and first["selection"] == second["selection"]
    linked["path"].unlink()
    assert review_preparation.detail(linked["topic"], linked["version"], first["preparation_id"], linked["actor"]) == first
    assert review_preparation.prepare(linked["topic"], linked["version"], body, linked["actor"]) == first


def test_legacy_replay_does_not_try_to_add_current_context(linked, monkeypatch):
    body = selected(linked)
    occurrences = review_preparation._occurrences(linked["topic"], linked["version"], linked["actor"])
    old = review_preparations.save(linked["topic"], linked["version"], linked["actor"].subject, body, occurrences,
                                  lambda: review_preparation._observe(linked["topic"], linked["run"], linked["paper"]))
    def forbidden(*args):
        pytest.fail("a replay must not reconstruct context")
    monkeypatch.setattr(review_context, "capture", forbidden)
    assert review_preparation.prepare(linked["topic"], linked["version"], body, linked["actor"]) == old
    assert old["schema_version"] == domain.SCHEMA and "context" not in old


def test_invalid_context_rolls_back_and_same_request_can_be_retried(linked, monkeypatch):
    body = selected(linked)
    def invalid(*args):
        return {**context(), "paper_title": "x" * 8001}
    with monkeypatch.context() as patch:
        patch.setattr(review_context, "capture", invalid)
        with pytest.raises(ValueError, match="integrity_invalid"):
            review_preparation.prepare(linked["topic"], linked["version"], body, linked["actor"])
    assert review_preparation.listing(linked["topic"], linked["version"], linked["actor"])["items"] == []
    assert review_preparation.prepare(linked["topic"], linked["version"], body, linked["actor"])["schema_version"] == domain.CONTEXT_SCHEMA


def test_contextual_range_is_blocked_from_both_legacy_cache_entry_points(monkeypatch):
    from app.services import ai_provider
    packet = review_range_input.build_packet(contextual_preparation())
    def forbidden(*args, **kwargs):
        pytest.fail("legacy draft cache must not open")
    monkeypatch.setattr(ai_review_drafts, "connect_metadata_index", forbidden)
    with pytest.raises(ValueError, match="review_range_receipt_only"):
        ai_review_drafts.upsert_draft(receipt_record(packet), input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
    with pytest.raises(ValueError, match="review_range_receipt_only"):
        ai_provider._store_accepted_draft(packet, None, provider_mode="stub", provider_origin="fixture")


def test_mixed_preparations_keep_body_coverage_and_frozen_journal_context(linked):
    from app.domain import journal_review_input
    from tests.test_review_source_coverage import append
    body = selected(linked)
    occurrences = review_preparation._occurrences(linked["topic"], linked["version"], linked["actor"])
    old = review_preparations.save(linked["topic"], linked["version"], linked["actor"].subject, body, occurrences,
                                  lambda: review_preparation._observe(linked["topic"], linked["run"], linked["paper"]))
    new = review_preparation.prepare(linked["topic"], linked["version"], body.model_copy(update={"request_id": "new"}), linked["actor"])
    old_receipt, new_receipt = append(old), append(new)
    result = review_source_coverage.overview(linked["topic"], linked["version"], new["preparation_id"], occurrences)
    assert result["unmapped_receipt_count"] == 0
    assert result["prepared"]["record_count"] == 2
    group = next(row for row in result["inputs"] if row["input_origin"] == "FIXTURE_SYNTHETIC")
    assert group["record_count"] == 2 and group["covered_chars"] == new["selection"]["chars"]
    assert {row["receipt_id"] for row in group["records"]} == {old_receipt["receipt_id"], new_receipt["receipt_id"]}
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        frozen = journal_review_input.freeze(c, linked["topic"], dict(receipt_id=new_receipt["receipt_id"],
            component_ids=["/evidence_spans/0/text"]), occurrences)
    attached = journal_review_input.attachment(dict(paper_version_id=linked["version"], review_input=frozen))
    assert attached["source_preparation"]["context"] == new["context"]
    altered = deepcopy(frozen)
    altered["receipt"]["packet"]["source_preparation"]["context"]["topic_name"] = "different topic"
    with pytest.raises(ValueError, match="synthesis_review_input_invalid"):
        journal_review_input.validate_frozen(altered)


@pytest.mark.parametrize("kind", ["http", "cli"])
def test_context_reaches_mock_or_fake_transport_and_reopened_journal_input(tmp_path, kind):
    from app.domain import journal_review_input
    from app.services.providers.base import ProviderRequest
    from tests import test_openai_prepared_review as http, test_codex_prepared_review as cli
    packet = review_range_input.build_packet(contextual_preparation())
    frozen = prepared_review.prepare(packet, provider_name="openai" if kind == "http" else "codex_cli", model="fixture")
    if kind == "http":
        provider, _, calls = http.provider_for(frozen)
    else:
        provider = cli.adapter_module().CodexPreparedReviewProvider(frozen,
            runtime=cli.runtime_at(tmp_path / "fake", frozen), reasoning_effort="low", timeout_seconds=30)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if kind == "http":
        assert len(calls) == 1
        sent = json.loads(calls[0].content)["input"][1]["content"]
    else:
        assert (tmp_path / "fake" / "launches.log").read_text().splitlines() == ["launch"]
        sent = json.loads((tmp_path / "fake" / "stdin.json").read_text(encoding="utf-8"))["prompt"]
    assert json.loads(sent) == packet
    path = tmp_path / "receipt.sqlite3"
    with closing(sqlite3.connect(path)) as c, c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN IMMEDIATE")
        saved = ai_review_drafts.append_input_receipt_in(c, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
    with closing(sqlite3.connect(path)) as c:
        c.row_factory = sqlite3.Row
        assert ai_review_drafts.get_input_receipt_in(c, "topic", saved["receipt_id"]) == saved
        selected = journal_review_input.freeze(c, "topic", dict(receipt_id=saved["receipt_id"],
            component_ids=["/evidence_spans/0/text"]), [("source-run", "paper-1")])
    result = journal_review_input.attachment(dict(paper_version_id="paper-version", review_input=selected))
    assert result["source_preparation"]["context"] == context()
    assert result["validation"]["prepared_execution"]["contract_sha256"] == frozen["contract_sha256"]
    assert result["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    assert result["input_origin"] == "FIXTURE_SYNTHETIC"

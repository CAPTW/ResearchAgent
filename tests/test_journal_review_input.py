"""A Journal selection freezes exact receipt components, not current files."""
from contextlib import closing
from copy import deepcopy
import hashlib
import importlib
import json
import unicodedata

import pytest

from app.db import bootstrap
from app.domain.review_input import enrich_packet
from app.repositories import ai_review_drafts as repository
from tests.test_ai_review_input_history import input_and_record


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.connect_metadata_index().close()


def module():
    return importlib.import_module("app.domain.journal_review_input")


def exact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def saved(*, topic="topic", run="source-run", paper="paper-v1", empty=False, unknown=False,
          extraction=False, status="accepted"):
    packet, record = input_and_record(topic=topic, run=run, paper=paper)
    packet.pop("input_scope")
    text = packet["selected_text_excerpts"][0]["text"]
    packet["selected_text_excerpts"].append(dict(source="abstract", text="Unselected private context", char_count=26))
    if empty:
        packet["selected_text_excerpts"][0]["text"] = ""
        packet["selected_text_excerpts"][0]["char_count"] = 0
    packet["evidence_spans"] = [dict(span_id="original-span", text=text[:4], char_start=0, char_end=4)]
    if unknown:
        packet["evidence_spans"][0].pop("char_start")
        packet["evidence_spans"][0].pop("char_end")
    review = dict(run_id=run, paper_id=paper, source_text_sha256=record["source_text_sha256"],
                  text_excerpt=text, evidence_spans=packet["evidence_spans"])
    if extraction:
        review["extraction_provenance"] = dict(
            schema_version="REVIEW_EXTRACTION_V1",
            artifact=dict(kind="text", sha256=record["source_text_sha256"], bytes=len(text.encode("utf-8"))),
            normalization="XRML_WHITESPACE_V1", decoding="UTF8_ERRORS_IGNORE",
            selection=dict(sha256=record["source_text_sha256"], chars=len(text), max_chars=24000,
                           available_chars=len(text), truncation="NOT_OBSERVED"), pdf_extraction=None)
    packet = enrich_packet(review, packet)
    record["status"] = status
    draft = repository.upsert_draft(record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
    return repository.get_input_receipt(topic, draft["validation"]["input_receipt_id"])


def selection(receipt, *pointers):
    return dict(receipt_id=receipt["receipt_id"], component_ids=list(pointers or ("/selected_text_excerpts/0/text",)))


def freeze(receipt, selected=None, *, topic="topic", occurrences=None):
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection, connection:
        connection.execute("BEGIN")
        return module().freeze(connection, topic, selected or selection(receipt),
                               occurrences if occurrences is not None else [(receipt["run_id"], receipt["paper_id"])])


def paper(frozen):
    return dict(paper_version_id="version-1", canonical_paper_id="canonical-1", title="Preserved paper",
                source_identity={"native_id": "paper-v1"}, metadata_digest="a" * 64,
                review_state="full_text_reviewed", rights={"state": "unknown"},
                review_source_run_id="newer-run", source_text_digest="b" * 64, review_input=frozen)


def test_freeze_exact_receipt_and_source_components_preserve_unicode():
    receipt = saved()
    frozen = freeze(receipt, selection(receipt, "/selected_text_excerpts/0/text", "/evidence_spans/0/text"))
    assert frozen == {"schema_version": "FROZEN_REVIEW_INPUT_V1", "receipt": receipt,
                      "selected_component_ids": ["/evidence_spans/0/text", "/selected_text_excerpts/0/text"]}
    assert module().validate_frozen(frozen) == frozen
    refs = module().references(paper(frozen))
    ref = next(item for item in refs if item["input_component"]["json_pointer"].startswith("/selected"))
    text = receipt["packet"]["selected_text_excerpts"][0]["text"]
    assert ref["text"] == text and ref["text"] != unicodedata.normalize("NFC", text)
    assert ref["citation_id"] == "riref_" + hashlib.sha256(exact(["version-1", receipt["receipt_id"], "/selected_text_excerpts/0/text"]).encode()).hexdigest()
    assert ref["recorded_content_digest"] == hashlib.sha256(text.encode()).hexdigest()
    assert ref["review_source_run_id"] == receipt["run_id"]
    assert ref["source_text_digest"] == receipt["source_text_sha256"]
    assert ref["input_origin"] == "FIXTURE_SYNTHETIC"
    assert refs[0]["source_evidence_id"] == "original-span"


def test_same_connection_getter_never_commits_or_closes_caller_transaction():
    receipt = saved()
    with closing(bootstrap.connect_metadata_index()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE uncommitted_marker (value TEXT)")
        assert repository.get_input_receipt_in(connection, "topic", receipt["receipt_id"]) == receipt
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='uncommitted_marker'").fetchone() is None


def test_absent_history_read_does_not_create_optional_table():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection, connection:
        connection.execute("BEGIN")
        before = tuple(connection.iterdump())
        assert repository.get_input_receipt_in(connection, "topic", "aireceipt_" + "0" * 64) is None
        assert connection.in_transaction
        assert tuple(connection.iterdump()) == before


def test_same_connection_occurrence_filter_hides_corrupt_foreign_receipt_before_decoding():
    receipt = saved()
    with closing(bootstrap.connect_metadata_index()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET packet_json='corrupt fixture'")
        assert repository.get_input_receipt_in(connection, "topic", receipt["receipt_id"],
                                               occurrences=[("hidden-run", "paper-v1")]) is None
        assert repository.get_input_receipt_in(connection, "topic", receipt["receipt_id"], occurrences=[]) is None
        with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
            repository.get_input_receipt_in(connection, "topic", receipt["receipt_id"],
                                            occurrences=[("source-run", "paper-v1")])
        with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
            repository.get_input_receipt_in(connection, "topic", receipt["receipt_id"])
        assert connection.in_transaction
        connection.rollback()


@pytest.mark.parametrize("damage", ["topic", "run", "paper", "missing"])
def test_capture_masks_foreign_topic_occurrence_or_absent_receipt(damage):
    receipt = saved()
    selected = selection(receipt)
    topic = "other" if damage == "topic" else "topic"
    occurrences = [("other" if damage == "run" else "source-run", "other" if damage == "paper" else "paper-v1")]
    if damage == "missing":
        selected["receipt_id"] = "aireceipt_" + "0" * 64
    with pytest.raises(KeyError, match="synthesis_review_input_not_found"):
        freeze(receipt, selected, topic=topic, occurrences=occurrences)


@pytest.mark.parametrize("damage", ["duplicate", "unknown", "empty", "extra", "bad_type", "arbitrary_pointer"])
def test_selection_rejects_invalid_or_unselected_json_fields(damage):
    receipt = saved()
    selected = selection(receipt)
    if damage == "duplicate":
        selected["component_ids"] *= 2
    elif damage == "unknown":
        selected["component_ids"] = ["/evidence_spans/55/text"]
    elif damage == "empty":
        selected["component_ids"] = []
    elif damage == "extra":
        selected["text"] = "caller replacement"
    elif damage == "bad_type":
        selected["component_ids"] = [0]
    else:
        selected["component_ids"] = ["/draft/summary"]
    with pytest.raises(ValueError, match="synthesis_review_input_invalid"):
        freeze(receipt, selected)


def test_empty_component_is_not_a_citable_reference():
    receipt = saved(empty=True)
    with pytest.raises(ValueError, match="synthesis_review_input_invalid"):
        freeze(receipt)


@pytest.mark.parametrize("damage", ["text", "range", "digest", "origin", "legacy", "selection", "receipt_extra"])
def test_frozen_receipt_and_range_tampering_fails_closed(damage):
    frozen = freeze(saved())
    receipt = frozen["receipt"]
    if damage == "text":
        receipt["packet"]["selected_text_excerpts"][0]["text"] += "changed"
    elif damage == "range":
        receipt["packet"]["input_scope"]["components"][0]["source_end"] += 1
    elif damage == "digest":
        receipt["receipt_id"] = "aireceipt_" + "f" * 64
    elif damage == "origin":
        receipt["input_origin"] = "PROVIDER_RESPONSE"
    elif damage == "legacy":
        receipt["packet"]["packet_version"] = "ai-review-packet-v1"
        receipt["packet"].pop("input_scope")
    elif damage == "selection":
        frozen["selected_component_ids"] = ["/draft/summary"]
    else:
        receipt["trusted"] = True
    with pytest.raises(ValueError, match="synthesis_review_input_invalid"):
        module().validate_frozen(frozen)


def test_unknown_mapping_is_preserved_without_invented_source_range():
    receipt = saved(unknown=True)
    frozen = freeze(receipt, selection(receipt, "/evidence_spans/0/text"))
    reference = module().references(paper(frozen))[0]
    assert reference["input_component"]["mapping"] == "unknown"
    assert reference["locator"]["char_start"] is None and reference["locator"]["char_end"] is None


def test_attachment_carries_result_and_scope_but_not_unselected_packet_text():
    frozen = freeze(saved())
    attachment = module().attachment(paper(frozen))
    assert attachment["schema_version"] == "JOURNAL_REVIEW_INPUT_V1"
    assert attachment["receipt_id"] == frozen["receipt"]["receipt_id"]
    assert attachment["review_result"] == frozen["receipt"]["draft"]
    assert attachment["input_scope"] == frozen["receipt"]["packet"]["input_scope"]
    assert attachment["selected_component_ids"] == frozen["selected_component_ids"]
    assert "Unselected private context" not in exact(attachment)
    assert "packet" not in attachment
    attachment["review_result"]["summary"] = "changed copy"
    assert frozen["receipt"]["draft"]["summary"] != "changed copy"


def test_attachment_and_references_preserve_extraction_and_separate_result_status():
    frozen = freeze(saved(extraction=True, status="needs_review"))
    attachment = module().attachment(paper(frozen))
    refs = module().references(paper(frozen))
    extraction = frozen["receipt"]["packet"]["source_extraction"]
    assert attachment["source_extraction"] == extraction == refs[0]["source_extraction"]
    assert attachment["status"] == "needs_review"
    assert attachment["validation"]["status"] == "accepted"
    assert attachment["input_scope"]["full_document_review"] == "NOT_ESTABLISHED"


def test_legacy_paper_has_no_synthetic_review_attachment():
    assert module().references({}) == []
    assert module().attachment({}) is None


def test_same_selected_receipt_remains_stable_after_latest_draft_changes():
    receipt = saved()
    old = freeze(receipt)
    packet, record = input_and_record(result="Replacement result")
    repository.upsert_draft(record, input_packet=packet, input_origin="MANUAL_IMPORT_UNVERIFIED")
    assert freeze(receipt) == old
    copy = module().validate_frozen(old)
    copy["receipt"]["draft"]["summary"] = "changed returned copy"
    assert old["receipt"]["draft"]["summary"] == receipt["draft"]["summary"]


def test_component_order_is_canonical_and_does_not_mutate_caller_selection():
    receipt = saved()
    selected = selection(receipt, "/selected_text_excerpts/0/text", "/evidence_spans/0/text")
    original = deepcopy(selected)
    first = freeze(receipt, selected)
    assert selected == original
    selected["component_ids"].reverse()
    assert first == freeze(receipt, selected)
    assert original["component_ids"] == ["/selected_text_excerpts/0/text", "/evidence_spans/0/text"]

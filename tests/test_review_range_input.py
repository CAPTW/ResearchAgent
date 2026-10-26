"""Explicit prepared-selection packets and receipt-only persistence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import importlib
from threading import Barrier

import pytest

from app.db import bootstrap
from app.domain import review_preparation
from app.domain.journal_exchange import preserved_json
from app.domain.review_input import validate_packet
from app.repositories import ai_review_drafts as repository
from app.services import ai_review


def preparation(text=" e\u0301 한글🙂 tail "):
    whole = "A" * 30000 + text
    source = dict(run_id="source-run", paper_id="paper-1", source_chars=len(whole), source_kind="text",
                  artifact_sha256=review_preparation.sha(whole), artifact_bytes=len(whole.encode("utf-8")),
                  artifact_ref="run:raw/paper.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.Prepare(request_id="select-1", run_id=source["run_id"], paper_id=source["paper_id"],
                                     expected_source_digest=source["source_digest"], start=30000, end=len(whole))
    return review_preparation.build("topic", "paper-version", "owner", body, source, whole, "2026-09-28T00:00:00Z")


def module():
    return importlib.import_module("app.domain.review_range_input")


def receipt_record(packet):
    return dict(topic_id=packet["topic_id"], run_id=packet["input_scope"]["source"]["run_id"],
                paper_id=packet["paper_id"], source_text_sha256=packet["source_text_sha256"],
                review_level="full_text_grounded", status="needs_review", draft={"summary": "Synthetic result"},
                validation={"status": "accepted", "reason_codes": [], "synthetic": True})


def test_packet_keeps_exact_selected_text_and_distinct_artifact_coordinates():
    saved = preparation()
    original = deepcopy(saved)
    packet = module().build_packet(saved)
    assert saved == original
    assert packet == module().build_packet(deepcopy(saved))
    assert packet["packet_version"] == "ai-review-packet-v3"
    assert packet["selected_text_excerpts"] == []
    assert len(packet["evidence_spans"]) == 1
    assert packet["evidence_spans"][0]["text"] == " e\u0301 한글🙂 tail "
    assert packet["title"] == saved["source"]["paper_id"]
    assert packet["abstract"] is None and packet["existing_review"] == {}
    assert packet["source_text_sha256"] == saved["selection"]["sha256"]
    binding = packet["source_preparation"]
    assert binding["preparation_sha256"] == review_preparation.sha(preserved_json(saved))
    assert binding["source"] == saved["source"]
    assert binding["selection"] == {key: value for key, value in saved["selection"].items() if key != "text"}
    scope = validate_packet(packet)
    assert scope["schema_version"] == "AI_REVIEW_INPUT_SCOPE_V2"
    assert scope["source"]["coordinate_space"] == "PREPARED_SELECTION_CODEPOINTS"
    assert scope["source"]["preserved_text_chars"] == len(saved["selection"]["text"])
    assert scope["components"][0]["source_start"] == 0
    assert scope["components"][0]["source_end"] == saved["selection"]["chars"]
    assert scope["document_coverage"] == "UNKNOWN"
    assert scope["full_document_review"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("change", ["text", "local_offset", "source_hash", "artifact_hash", "original_range", "preparation_id",
                                    "normalization", "extra_text", "version", "scope_version", "full_document", "extra_field"])
def test_tampered_packet_or_binding_is_rejected_even_with_updated_payload_hash(change):
    packet = module().build_packet(preparation())
    if change == "text":
        packet["evidence_spans"][0]["text"] = "rewritten"
    elif change == "local_offset":
        packet["evidence_spans"][0]["char_start"] = 30000
    elif change == "source_hash":
        packet["source_text_sha256"] = "a" * 64
    elif change == "artifact_hash":
        packet["source_preparation"]["source"]["artifact_sha256"] = "b" * 64
    elif change == "original_range":
        packet["source_preparation"]["selection"]["end"] += 1
    elif change == "preparation_id":
        packet["source_preparation"]["preparation_id"] = "other"
    elif change == "normalization":
        packet["source_preparation"]["normalization"] = "NFC"
    elif change == "extra_text":
        packet["selected_text_excerpts"] = [{"source": "unexpected", "text": "unselected", "char_count": 10}]
    elif change == "version":
        packet["packet_version"] = "ai-review-packet-v2"
    elif change == "scope_version":
        packet["input_scope"]["schema_version"] = "AI_REVIEW_INPUT_SCOPE_V1"
    elif change == "full_document":
        packet["input_scope"]["full_document_review"] = "COMPLETE"
    else:
        packet["hidden_prompt"] = "unexpected body"
    packet["input_scope"]["packet_payload_sha256"] = review_preparation.sha(
        preserved_json({key: value for key, value in packet.items() if key != "input_scope"}))
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        validate_packet(packet)


@pytest.mark.parametrize("change", ["state", "selection", "source_digest", "source_size", "extra", "surrogate"])
def test_builder_rejects_invalid_preparation_snapshot(change):
    saved = preparation()
    if change == "state":
        saved["execution"]["started"] = True
    elif change == "selection":
        saved["selection"]["chars"] += 1
    elif change == "source_digest":
        saved["source"]["source_digest"] = "0" * 64
    elif change == "source_size":
        saved["source"]["source_chars"] = True
    elif change == "extra":
        saved["extra"] = "unexpected"
    else:
        saved["selection"]["text"] = "\ud800"
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        module().build_packet(saved)


def test_run_binding_never_reads_mutable_review_and_v1_v2_semantics_stay(monkeypatch):
    from tests.test_review_input import inputs
    from app.domain.review_input import enrich_packet
    source, old = inputs()
    v2 = enrich_packet(source, old)
    v2_original = deepcopy(v2)
    v1 = dict(packet_version="ai-review-packet-v1")
    assert validate_packet(v1)["schema_version"] == "LEGACY_REVIEW_INPUT_UNKNOWN"
    assert validate_packet(v2)["schema_version"] == "AI_REVIEW_INPUT_SCOPE_V1"
    def forbidden(*args, **kwargs):
        pytest.fail("prepared packet must never look up the mutable review")
    monkeypatch.setattr(ai_review.paper_reviews, "list_reviews", forbidden)
    packet = module().build_packet(preparation())
    assert ai_review._run_id_for_packet(packet, "topic") == "source-run"
    assert v2 == v2_original


def test_v3_cannot_enter_legacy_latest_draft_upsert(monkeypatch):
    packet = module().build_packet(preparation())
    def forbidden(*args, **kwargs):
        pytest.fail("V3 legacy write must be rejected before opening the database")
    monkeypatch.setattr(repository, "connect_metadata_index", forbidden)
    with pytest.raises(ValueError, match="review_range_receipt_only"):
        repository.upsert_draft(receipt_record(packet), input_packet=packet, input_origin="FIXTURE_SYNTHETIC")


def test_v3_receipt_remains_readable_by_existing_frozen_journal_decoder():
    from app.domain.journal_review_input import validate_frozen
    packet = module().build_packet(preparation())
    content = repository._receipt_content(repository._normalize_record(receipt_record(packet)), packet, "FIXTURE_SYNTHETIC")
    receipt = dict(receipt_id=repository._receipt_id(content), recorded_at="2026-09-28T00:00:00Z", **content)
    frozen = dict(schema_version="FROZEN_REVIEW_INPUT_V1", receipt=receipt,
                  selected_component_ids=["/evidence_spans/0/text"])
    assert validate_frozen(frozen) == frozen


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.connect_metadata_index().close()


def test_receipt_only_append_requires_transaction_and_never_changes_latest_cache(store):
    from tests.test_ai_review_input_history import input_and_record
    old_packet, old_record = input_and_record()
    old = repository.upsert_draft(old_record, input_packet=old_packet, input_origin="FIXTURE_SYNTHETIC")
    packet = module().build_packet(preparation())
    record = receipt_record(packet)
    with closing(bootstrap.connect_metadata_index()) as connection:
        with pytest.raises(ValueError, match="review_input_transaction_required"):
            repository.append_input_receipt_in(connection, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        connection.execute("BEGIN IMMEDIATE")
        first = repository.append_input_receipt_in(connection, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        second = repository.append_input_receipt_in(connection, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        assert connection.in_transaction and first == second
        connection.commit()
    assert first["packet"] == packet and first["input_origin"] == "FIXTURE_SYNTHETIC"
    assert "selected_for_report" not in preserved_json(first["validation"])
    assert repository.get_input_receipt("topic", first["receipt_id"]) == first
    assert repository.list_drafts(topic_id="topic", read_only=True) == [old]


def test_receipt_append_rollback_removes_first_history_table_and_record(store):
    packet = module().build_packet(preparation())
    with closing(bootstrap.connect_metadata_index()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        repository.append_input_receipt_in(connection, receipt_record(packet), input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        connection.rollback()
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='ai_review_input_history'").fetchone() is None
    assert repository.list_drafts(topic_id="topic", read_only=True) == []


def test_duplicate_receipt_appends_share_identity_without_latest_mutation(store):
    packet = module().build_packet(preparation())
    barrier = Barrier(4)
    def append(_):
        barrier.wait()
        with closing(bootstrap.connect_metadata_index()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return repository.append_input_receipt_in(connection, receipt_record(packet), input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(append, range(4)))
    assert all(item == results[0] for item in results)
    assert len(repository.list_input_history(topic_id="topic")) == 1
    assert repository.list_drafts(topic_id="topic", read_only=True) == []

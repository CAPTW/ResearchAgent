"""Immutable input/result receipts alongside the legacy latest-draft cache."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import sqlite3
from threading import Barrier

import pytest

from app.db import bootstrap
from app.domain.review_input import enrich_packet
from app.repositories import ai_review_drafts as repository


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.connect_metadata_index().close()


def input_and_record(*, topic="topic", run="source-run", paper="paper-v1", text="e\u0301 한글🙂 evidence", result="Initial result"):
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    packet = enrich_packet(
        dict(run_id=run, paper_id=paper, text_excerpt=text, source_text_sha256=source_hash),
        dict(packet_version="ai-review-packet-v2", topic_id=topic, paper_id=paper, source_text_sha256=source_hash,
             selected_text_excerpts=[dict(source="text_excerpt", text=text, char_count=len(text))], evidence_spans=[]),
    )
    record = dict(topic_id=topic, run_id=run, paper_id=paper, source_text_sha256=source_hash,
                  review_level="abstract_only", status="accepted", draft=dict(summary=result),
                  validation=dict(status="accepted", reason_codes=[], validated_at="2026-01-01T00:00:00Z"))
    return packet, record


def save(packet, record, origin="FIXTURE_SYNTHETIC"):
    return repository.upsert_draft(record, input_packet=packet, input_origin=origin)


def receipt_id(stored):
    return stored["validation"]["input_receipt_id"]


def database_state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        return tuple(connection.iterdump())


def test_distinct_input_and_results_survive_same_legacy_key_and_reopen():
    packet, record = input_and_record()
    first = save(packet, record)
    changed_result = deepcopy(record)
    changed_result["draft"]["summary"] = "Changed interpretation"
    second = save(packet, changed_result)
    changed_packet = deepcopy(packet)
    # A new bounded transmission of the same recorded source is distinct history.
    changed_packet.pop("input_scope")
    changed_packet["selected_text_excerpts"][0]["text"] = "e\u0301"
    changed_packet["selected_text_excerpts"][0]["char_count"] = 2
    changed_packet = enrich_packet(dict(run_id=record["run_id"], paper_id=record["paper_id"], source_text_sha256=record["source_text_sha256"],
                                        text_excerpt=packet["selected_text_excerpts"][0]["text"]), changed_packet)
    third = save(changed_packet, changed_result)
    ids = [receipt_id(item) for item in (first, second, third)]
    assert len(set(ids)) == 3
    history = repository.list_input_history(topic_id="topic", paper_ids=["paper-v1"])
    assert {item["receipt_id"] for item in history} == set(ids)
    old = repository.get_input_receipt("topic", ids[0])
    assert old["packet"] == packet and old["draft"] == record["draft"]
    assert old["input_origin"] == "FIXTURE_SYNTHETIC"
    assert old["validation"] == {"status": "accepted", "reason_codes": []}
    latest = repository.get_draft("topic", "source-run", "paper-v1", record["source_text_sha256"], read_only=True)
    assert receipt_id(latest) == ids[-1]
    assert len(repository.list_drafts(topic_id="topic", read_only=True)) == 1


def test_replay_ignores_save_and_validation_timestamps_without_changing_first_receipt():
    packet, record = input_and_record()
    first = save(packet, record)
    original = repository.get_input_receipt("topic", receipt_id(first))
    record.update(created_at="later", imported_at="later")
    record["validation"].update(validated_at="later", input_receipt_id="untrusted-stale-value")
    replay = save(packet, record)
    assert receipt_id(replay) == receipt_id(first)
    assert repository.get_input_receipt("topic", receipt_id(first)) == original
    assert len(repository.list_input_history(topic_id="topic", paper_ids=None)) == 1


def test_concurrent_duplicate_receipts_share_identity_and_commit_atomically():
    packet, record = input_and_record()
    gate = Barrier(4)
    def concurrent(_):
        gate.wait()
        return save(packet, record)
    with ThreadPoolExecutor(max_workers=4) as pool:
        saved = list(pool.map(concurrent, range(4)))
    assert len({receipt_id(item) for item in saved}) == 1
    assert all(item["draft"] == record["draft"] for item in saved)
    assert len(repository.list_input_history(topic_id="topic", paper_ids=None)) == 1


def test_concurrent_distinct_results_return_each_callers_own_draft():
    packet, record = input_and_record()
    gate = Barrier(4)
    def concurrent(index):
        changed = deepcopy(record)
        changed["draft"]["summary"] = f"Result {index}"
        gate.wait()
        return index, save(packet, changed)
    with ThreadPoolExecutor(max_workers=4) as pool:
        saved = list(pool.map(concurrent, range(4)))
    assert len({receipt_id(item) for _, item in saved}) == 4
    for index, item in saved:
        assert item["draft"]["summary"] == f"Result {index}"
        assert repository.get_input_receipt("topic", receipt_id(item))["draft"] == item["draft"]


@pytest.mark.parametrize("failure_table", ("ai_review_drafts", "ai_review_input_history"))
def test_rollback_preserves_previous_latest_and_no_partial_receipt(failure_table):
    packet, record = input_and_record()
    save(packet, record)
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute(f"""CREATE TRIGGER fail_receipt_test BEFORE INSERT ON {failure_table}
            BEGIN SELECT RAISE(ABORT,'injected_receipt_failure'); END""")
    before = database_state()
    record["draft"]["summary"] = "Must not survive"
    with pytest.raises(sqlite3.IntegrityError, match="injected_receipt_failure"):
        save(packet, record)
    assert database_state() == before


def test_first_archive_failure_rolls_back_extension_schema_as_well_as_result():
    packet, record = input_and_record()
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("""CREATE TRIGGER fail_first_draft BEFORE INSERT ON ai_review_drafts
            BEGIN SELECT RAISE(ABORT,'injected_first_failure'); END""")
    before = database_state()
    with pytest.raises(sqlite3.IntegrityError, match="injected_first_failure"):
        save(packet, record)
    assert database_state() == before
    assert repository.list_input_history(topic_id="topic", paper_ids=None) == []


def test_read_filters_topic_and_authorized_occurrences_before_pagination():
    for topic, run, paper in (("other", "source-run", "paper-v1"), ("topic", "private-run", "paper-v1"),
                              ("topic", "public-run", "paper-v1"), ("topic", "public-run", "paper-v2")):
        packet, record = input_and_record(topic=topic, run=run, paper=paper)
        save(packet, record)
    visible = repository.list_input_history(topic_id="topic", paper_ids=["paper-v1", "paper-v2"],
                                            occurrences=[("public-run", "paper-v1"), ("public-run", "paper-v2")])
    assert len(visible) == 2 and all(row["run_id"] == "public-run" for row in visible)
    assert repository.list_input_history(topic_id="topic", paper_ids=None, occurrences=[], limit=1) == []
    assert repository.list_input_history(topic_id="topic", paper_ids=[]) == []
    for offset, expected in enumerate(visible):
        page = repository.list_input_history(topic_id="topic", paper_ids=None, occurrences=[("public-run", "paper-v1"), ("public-run", "paper-v2")],
                                             limit=1, offset=offset)
        assert page == [expected]
    assert repository.get_input_receipt("other", visible[0]["receipt_id"]) is None
    assert repository.get_input_receipt("topic", "missing") is None


def test_legacy_database_reads_do_not_create_history_or_fabricate_receipts():
    _, record = input_and_record()
    legacy = repository.upsert_draft(record)
    before = database_state()
    assert "input_receipt_id" not in legacy["validation"]
    assert repository.list_input_history(topic_id="topic", paper_ids=None) == []
    assert repository.get_input_receipt("topic", "missing") is None
    assert database_state() == before


def test_immutable_triggers_and_unicode_bytes_survive_readback():
    packet, record = input_and_record(result="e\u0301 원문🙂")
    stored = save(packet, record)
    receipt = repository.get_input_receipt("topic", receipt_id(stored))
    assert receipt["packet"] == packet and receipt["draft"] == record["draft"]
    assert receipt["draft"]["summary"].encode("utf-8") == b"e\xcc\x81 " + "원문🙂".encode("utf-8")
    for sql in ("UPDATE ai_review_input_history SET input_origin='PROVIDER_RESPONSE'", "DELETE FROM ai_review_input_history"):
        with closing(bootstrap.connect_metadata_index()) as connection, connection:
            with pytest.raises(sqlite3.IntegrityError, match="ai_review_input_immutable"):
                connection.execute(sql)
    assert repository.get_input_receipt("topic", receipt_id(stored)) == receipt


@pytest.mark.parametrize("field", ("topic_id", "run_id", "paper_id", "source_text_sha256"))
def test_binding_mismatch_rejects_before_any_store_change(field):
    packet, record = input_and_record()
    record[field] = "different"
    before = database_state()
    with pytest.raises(ValueError, match="review_input_binding_invalid"):
        save(packet, record)
    assert database_state() == before


@pytest.mark.parametrize("invalid", ("origin", "packet", "legacy"))
def test_invalid_origin_or_packet_rejects_without_writes(invalid):
    packet, record = input_and_record()
    origin = "FIXTURE_SYNTHETIC"
    if invalid == "origin":
        origin = "LIVE_PROOF_FROM_BROWSER"
    elif invalid == "packet":
        packet["selected_text_excerpts"][0]["text"] = "Altered"
    else:
        packet.pop("input_scope")
        packet["packet_version"] = "ai-review-packet-v1"
    before = database_state()
    with pytest.raises(ValueError, match="review_input_"):
        save(packet, record, origin)
    assert database_state() == before


def test_different_origin_does_not_inherit_receipt_identity():
    packet, record = input_and_record()
    ids = [receipt_id(save(packet, record, origin)) for origin in
           ("FIXTURE_SYNTHETIC", "PROVIDER_RESPONSE", "MANUAL_IMPORT_UNVERIFIED")]
    assert len(set(ids)) == 3


def test_abstract_only_missing_source_hash_retains_legacy_empty_database_key():
    packet, record = input_and_record()
    packet.pop("input_scope")
    packet["source_text_sha256"] = None
    packet = enrich_packet(dict(run_id=record["run_id"], paper_id=record["paper_id"],
                                text_excerpt=packet["selected_text_excerpts"][0]["text"]), packet)
    record["source_text_sha256"] = None
    stored = save(packet, record)
    assert stored["source_text_sha256"] == ""
    archived = repository.get_input_receipt("topic", receipt_id(stored))
    assert archived["packet"]["source_text_sha256"] is None
    assert archived["packet"]["input_scope"]["source"]["recorded_source_sha256"] is None


def test_corrupt_archive_never_silently_discards_unexpected_validation_fields():
    packet, record = input_and_record()
    stored = save(packet, record)
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET validation_json=?", ('{"status":"accepted","reason_codes":[],"timestamp":"tampered"}',))
    with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
        repository.get_input_receipt("topic", receipt_id(stored))


@pytest.mark.parametrize("column", ("packet_json", "draft_json", "validation_json", "input_origin"))
def test_corrupt_archive_json_or_origin_fails_closed(column):
    packet, record = input_and_record()
    save(packet, record)
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute(f"UPDATE ai_review_input_history SET {column}=?", ("tampered",))
    with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
        repository.list_input_history(topic_id="topic", paper_ids=None)

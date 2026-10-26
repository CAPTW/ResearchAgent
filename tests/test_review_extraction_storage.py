"""Atomic current extraction provenance; legacy rows never inherit a receipt."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import sqlite3
from threading import Barrier

import pytest

from app.db import bootstrap
from app.domain import constants
from app.repositories import paper_reviews


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", root)
    monkeypatch.setattr(constants, "STORAGE_ROOT", root)
    bootstrap.connect_metadata_index().close()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review(index=0, *, topic="topic", run="run", paper="paper"):
    text = (f"e\u0301 한글🙂 preserved evidence {index}. " * 30).strip()
    return dict(topic_id=topic, run_id=run, paper_id=paper, title=f"Review {index}", text_excerpt=text,
                full_text_status="reviewed", text_char_count=len(text), source_text_sha256=sha(text),
                full_text_artifact_path="run:raw/paper.txt", review_source_type="full_text",
                evidence_spans=[dict(label="span", text=text[:30])],
                extraction_provenance=dict(schema_version="REVIEW_EXTRACTION_V1",
                    artifact=dict(kind="text", sha256=sha("raw " + text), bytes=len(("raw " + text).encode("utf-8"))),
                    normalization="XRML_WHITESPACE_V1", decoding="UTF8_ERRORS_IGNORE",
                    selection=dict(sha256=sha(text), chars=len(text), max_chars=24000,
                                   available_chars=len(text), truncation="NOT_OBSERVED"), pdf_extraction=None))


def database_state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        return tuple(connection.iterdump())


def test_provenance_roundtrip_preserves_unicode_and_query_projection():
    value = review()
    saved = paper_reviews.upsert_review(value)
    assert saved["extraction_provenance"] == value["extraction_provenance"]
    assert saved["text_excerpt"].encode("utf-8") == value["text_excerpt"].encode("utf-8")
    assert paper_reviews.get_review("topic", "run", "paper", read_only=True) == saved
    assert paper_reviews.list_reviews(topic_id="topic", read_only=True) == [saved]


def test_legacy_read_does_not_create_extension_or_fabricate_provenance():
    value = review()
    value.pop("extraction_provenance")
    saved = paper_reviews.upsert_review(value)
    before = database_state()
    assert "extraction_provenance" not in saved
    assert paper_reviews.get_review("topic", "run", "paper", read_only=True) == saved
    assert paper_reviews.list_reviews(topic_id="topic", read_only=True) == [saved]
    assert database_state() == before
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='paper_review_extraction'").fetchone() is None


def test_legacy_replacement_clears_provenance_instead_of_inheriting_previous_source():
    old = paper_reviews.upsert_review(review())
    value = review(1)
    value.pop("extraction_provenance")
    changed = paper_reviews.upsert_review(value)
    assert changed["text_excerpt"] != old["text_excerpt"]
    assert "extraction_provenance" not in changed
    assert "extraction_provenance" not in paper_reviews.get_review("topic", "run", "paper", read_only=True)
    assert paper_reviews.list_reviews(topic_id="topic", read_only=True) == [changed]
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_review_extraction").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ("hash", "chars", "text", "selection", "status", "malformed"))
def test_inconsistent_provenance_rejects_before_any_write(damage):
    value = review()
    if damage == "hash":
        value["source_text_sha256"] = "0" * 64
    elif damage == "chars":
        value["text_char_count"] += 1
    elif damage == "text":
        value["text_excerpt"] += "changed"
    elif damage == "selection":
        value["extraction_provenance"]["selection"]["sha256"] = "1" * 64
    elif damage == "status":
        value["full_text_status"] = "abstract_only"
    else:
        value["extraction_provenance"] = {"invalid": True}
    before = database_state()
    with pytest.raises(ValueError, match="review_extraction_provenance_invalid"):
        paper_reviews.upsert_review(value)
    assert database_state() == before


def test_failed_extension_write_rolls_back_latest_review_and_previous_provenance():
    paper_reviews.upsert_review(review())
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("""CREATE TRIGGER reject_extraction BEFORE INSERT ON paper_review_extraction
            BEGIN SELECT RAISE(ABORT,'injected_extraction_failure'); END""")
    before = database_state()
    with pytest.raises(sqlite3.IntegrityError, match="injected_extraction_failure"):
        paper_reviews.upsert_review(review(1))
    assert database_state() == before


def test_failed_provenance_clear_rolls_back_legacy_replacement():
    paper_reviews.upsert_review(review())
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("""CREATE TRIGGER reject_clear BEFORE DELETE ON paper_review_extraction
            BEGIN SELECT RAISE(ABORT,'injected_extraction_failure'); END""")
    before = database_state()
    changed = review(1)
    changed.pop("extraction_provenance")
    with pytest.raises(sqlite3.IntegrityError, match="injected_extraction_failure"):
        paper_reviews.upsert_review(changed)
    assert database_state() == before


def test_concurrent_updates_return_own_projection_and_matching_provenance():
    gate = Barrier(4)
    def update(index):
        value = review(index)
        gate.wait()
        return value, paper_reviews.upsert_review(value)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(update, range(4)))
    for original, returned in results:
        assert returned["text_excerpt"] == original["text_excerpt"]
        assert returned["extraction_provenance"] == original["extraction_provenance"]
    latest = paper_reviews.get_review("topic", "run", "paper", read_only=True)
    assert latest in [returned for _, returned in results]
    assert paper_reviews.list_reviews(topic_id="topic", read_only=True) == [latest]


@pytest.mark.parametrize("target", ("json", "checksum", "source_hash", "stored_text", "stored_length", "status"))
def test_corrupt_projection_fails_closed_on_detail_and_list(target):
    paper_reviews.upsert_review(review())
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        if target == "json":
            connection.execute("UPDATE paper_review_extraction SET provenance_json='bad json'")
        elif target == "checksum":
            connection.execute("UPDATE paper_review_extraction SET provenance_sha256='bad hash'")
        elif target == "source_hash":
            connection.execute("UPDATE paper_reviews SET source_text_sha256='changed'")
        elif target == "stored_text":
            connection.execute("UPDATE paper_reviews SET text_excerpt='changed'")
        elif target == "stored_length":
            connection.execute("UPDATE paper_reviews SET text_char_count=1")
        else:
            connection.execute("UPDATE paper_reviews SET full_text_status='abstract_only'")
    with pytest.raises(ValueError, match="review_extraction_"):
        paper_reviews.get_review("topic", "run", "paper", read_only=True)
    with pytest.raises(ValueError, match="review_extraction_"):
        paper_reviews.list_reviews(topic_id="topic", read_only=True)


def test_same_paper_in_other_run_or_topic_keeps_its_own_provenance():
    original = review()
    other_run = review(1, run="other-run")
    other_topic = review(2, topic="other-topic")
    for value in (original, other_run, other_topic):
        paper_reviews.upsert_review(value)
    changed = deepcopy(original)
    changed.pop("extraction_provenance")
    paper_reviews.upsert_review(changed)
    assert paper_reviews.get_review("topic", "other-run", "paper", read_only=True)["extraction_provenance"] == other_run["extraction_provenance"]
    assert paper_reviews.get_review("other-topic", "run", "paper", read_only=True)["extraction_provenance"] == other_topic["extraction_provenance"]

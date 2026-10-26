"""Hidden paper occurrences are filtered before immutable receipt decoding."""
from contextlib import closing

import pytest

from app.db import bootstrap
from app.repositories import ai_review_drafts as repository
from app.services import paper_bank
from tests.test_ai_review_input_history import input_and_record, receipt_id, save
from tests import test_review_input_integration as integration_tests
from tests.test_review_input_integration import _seed_receipt
from tests.test_review_preparation_api import client

seeded = integration_tests.seeded


def _corrupt(identity):
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER IF EXISTS ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET packet_json='hidden corrupt receipt' WHERE receipt_id=?", (identity,))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.connect_metadata_index().close()


def test_receipt_getter_keeps_default_read_and_filters_pairs_before_decoding(store, monkeypatch):
    packet, record = input_and_record()
    identity = receipt_id(save(packet, record))
    original = repository.get_input_receipt("topic", identity)
    assert repository.get_input_receipt("topic", identity, occurrences=[("source-run", "paper-v1")]) == original
    assert repository.get_input_receipt("topic", identity, occurrences=None) == original
    assert repository.get_input_receipt("topic", identity, read_only=False) == original
    _corrupt(identity)
    observed = []
    decode = repository._row_to_receipt

    def observe(row):
        observed.append(row["receipt_id"])
        return decode(row)

    monkeypatch.setattr(repository, "_row_to_receipt", observe)
    for pairs in ([], [("source-run", "another-paper")], [("another-run", "paper-v1")]):
        assert repository.get_input_receipt("topic", identity, occurrences=pairs) is None
    assert repository.get_input_receipt("other-topic", identity) is None
    assert observed == []
    with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
        repository.get_input_receipt("topic", identity, occurrences=[("source-run", "paper-v1")])
    with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
        repository.get_input_receipt("topic", identity)
    assert observed == [identity, identity]


def test_actual_history_route_masks_corrupt_hidden_occurrence_but_reports_visible_corruption(seeded, monkeypatch):
    topic = seeded.TOPIC_A_ID
    item = paper_bank.list_topic_papers(topic, page_size=100)["items"][0]
    occurrence = item["compatibility"]["occurrences"][0]
    visible_id, packet = _seed_receipt(topic, occurrence["runId"], occurrence["paperId"])
    hidden_id, _ = _seed_receipt(topic, "unrelated-run", occurrence["paperId"])
    root = f"/api/topics/{topic}/paper-bank/{item['paperVersionId']}/review-inputs"
    _corrupt(hidden_id)
    observed = []
    decode = repository._row_to_receipt

    def observe(row):
        observed.append(row["receipt_id"])
        return decode(row)

    monkeypatch.setattr(repository, "_row_to_receipt", observe)
    with client() as http:
        hidden = http.get(root + "/" + hidden_id)
        missing = http.get(root + "/missing")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() == {"detail": "review_input_not_found"}
        assert observed == []
        assert http.get(root + "/" + visible_id).json()["packet"] == packet
        assert observed == [visible_id]
        _corrupt(visible_id)
        unavailable = http.get(root + "/" + visible_id)
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "review_input_store_unavailable"}
        assert observed == [visible_id, visible_id]

from contextlib import closing
import json

import pytest

from app.db.bootstrap import connect_metadata_index
from app.repositories import journal_candidates, journal_reviews as repo
from tests import test_journal_reviews as reviews

source = reviews.source
completed = reviews.completed
candidate = reviews.candidate


def test_exact_judgment_and_note_changes_keep_frozen_sources(candidate, source, monkeypatch):
    first, _ = reviews.save(candidate, reviews.body(candidate))
    body = reviews.body(candidate, "revise", first["review_id"], "held")
    body.note = "Check the original context again."
    body.entries[0].assessment = "limited"
    body.entries[0].note = "Only abstract evidence."
    body.entries.reverse()  # Entry identity, not array position, determines matching.
    second, _ = repo.save("topic", candidate["job_id"], candidate["candidate_id"], body, "another-reviewer")
    args = ("topic", candidate["job_id"], candidate["candidate_id"], second["review_id"], first["review_id"])
    compared = repo.compare(*args)
    assert compared["schema_version"] == "JOURNAL_REVIEW_COMPARISON_V1"
    assert compared["counts"] == dict(total=2, changed=1, unchanged=1, assessment_changed=1, note_changed=1)
    assert compared["overall_changes"] == dict(state=True, note=True)
    assert compared["from"]["review_id"] == first["review_id"]
    assert compared["from"]["report_version_id"] == first["report"]["version_id"]
    assert compared["to"]["report_version_id"] is None
    changed = compared["entries"][0]
    assert changed["before"]["assessment"] == "supported"
    assert changed["after"]["assessment"] == "limited"
    assert changed["changed"] and not compared["entries"][1]["changed"]
    assert compared["citations"] == candidate["citations"]
    assert all("citations" not in entry for entry in compared["entries"]), "Do not repeat large quotations per sentence"
    assert set(changed["citation_ids"]) <= {c["citation_id"] for c in compared["citations"]}
    assert compared["scientific_assessment"] == "NOT_ASSESSED"
    source[0]["_raw"]["abstract"] = "CURRENT SOURCE CHANGED"
    monkeypatch.setattr(reviews.domain, "report", lambda *_: pytest.fail("Never regenerate a historical manuscript"))
    assert repo.compare(*args) == compared
    reverse = repo.compare(*args[:3], first["review_id"], second["review_id"])
    assert reverse["entries"][0]["before"] == changed["after"]
    assert repo.detail(*args[:3], first["review_id"]) == first


def test_identical_judgments_are_zero_changes_but_overall_notes_are_separate(candidate):
    first, _ = reviews.save(candidate, reviews.body(candidate))
    value = reviews.body(candidate, "again", first["review_id"])
    value.note += " "  # Exact saved text, no silent normalization.
    second, _ = reviews.save(candidate, value)
    compared = repo.compare("topic", candidate["job_id"], candidate["candidate_id"], second["review_id"], first["review_id"])
    assert compared["counts"] == dict(total=2, changed=0, unchanged=2, assessment_changed=0, note_changed=0)
    assert compared["overall_changes"] == dict(state=False, note=True)
    assert compared["from"]["report_version_id"] != compared["to"]["report_version_id"]


def test_same_missing_and_foreign_records_never_appear_as_zero_changes(candidate):
    first, _ = reviews.save(candidate, reviews.body(candidate))
    args = ("topic", candidate["job_id"], candidate["candidate_id"])
    with pytest.raises(ValueError, match="comparison_same"):
        repo.compare(*args, first["review_id"], first["review_id"])
    with pytest.raises(KeyError):
        repo.compare(*args, first["review_id"], "missing")
    other, _ = journal_candidates.save("topic", candidate["job_id"], json.dumps(dict(candidate["proposal"], title="Different candidate")), "importer")
    foreign, _ = reviews.save(other, reviews.body(other))
    with pytest.raises(KeyError):
        repo.compare(*args, first["review_id"], foreign["review_id"])
    with pytest.raises(KeyError):
        repo.compare("another-topic", *args[1:], first["review_id"], foreign["review_id"])


@pytest.mark.parametrize("damaged", ["candidate", "from", "to"])
def test_damaged_record_blocks_comparison(candidate, damaged):
    first, _ = reviews.save(candidate, reviews.body(candidate))
    second, _ = reviews.save(candidate, reviews.body(candidate, "again", first["review_id"]))
    with closing(connect_metadata_index()) as c, c:
        if damaged == "candidate":
            c.execute("DROP TRIGGER journal_candidates_no_update")
            c.execute("UPDATE journal_candidates SET payload_digest='damaged'")
        else:
            c.execute("DROP TRIGGER journal_review_no_update")
            c.execute("UPDATE journal_candidate_reviews SET payload_digest='damaged' WHERE review_id=?", (first["review_id"] if damaged == "from" else second["review_id"],))
    with pytest.raises(ValueError, match="integrity_invalid"):
        repo.compare("topic", candidate["job_id"], candidate["candidate_id"], second["review_id"], first["review_id"])

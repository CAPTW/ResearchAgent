"""Topic discovery must keep current review separate from preserved acceptance."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from app.db.bootstrap import connect_metadata_index
from app.repositories import journal_candidates, journal_reviews as repo
from tests import test_journal_reviews as reviews

source = reviews.source
completed = reviews.completed
candidate = reviews.candidate


def test_empty_overview_does_not_initialize_tables(completed):
    assert repo.overview("topic") == dict(items=[], total=0, page=1, limit=10)
    with closing(connect_metadata_index(read_only=True)) as c:
        assert not journal_candidates._exists(c) and not repo._exists(c)


def test_pagination_and_current_state_are_independent_of_last_acceptance(candidate, source):
    initial = repo.overview("topic")["items"][0]
    assert initial["latest_review"] is None and initial["last_acceptance"] is None
    first, _ = reviews.save(candidate, reviews.body(candidate))
    held, _ = reviews.save(candidate, reviews.body(candidate, "hold", first["review_id"], "held"))
    another, _ = journal_candidates.save("topic", candidate["job_id"], json.dumps(dict(candidate["proposal"], title="Unreviewed second candidate")), "importer")
    assert repo.overview("topic", 1, 1)["items"][0]["candidate_id"] == another["candidate_id"]
    row = repo.overview("topic", 2, 1)["items"][0]
    assert row["latest_review"]["state"] == "held"
    assert row["latest_review"]["review_id"] == held["review_id"]
    assert row["last_acceptance"]["review_id"] == first["review_id"]
    assert row["last_acceptance"]["version_id"] == first["report"]["version_id"]
    assert "content" not in row["last_acceptance"]
    assert "entries" not in row["latest_review"]
    assert repo.overview("topic", 3, 1)["items"] == []
    assert repo.overview("other-topic")["total"] == 0
    second, _ = reviews.save(candidate, reviews.body(candidate, "again", held["review_id"]))
    source[0]["_raw"]["abstract"] = "MUTATED CURRENT SOURCE"
    row = repo.overview("topic", 2, 1)["items"][0]
    assert row["latest_review"]["review_id"] == second["review_id"]
    assert row["last_acceptance"]["version_id"] == second["report"]["version_id"]
    assert row["title"] == candidate["proposal"]["title"]
    assert repo.detail("topic", candidate["job_id"], candidate["candidate_id"], first["review_id"]) == first


@pytest.mark.parametrize("part", ["candidate", "latest", "acceptance"])
def test_corruption_is_an_error_not_missing_or_successful_history(candidate, part):
    first, _ = reviews.save(candidate, reviews.body(candidate))
    held, _ = reviews.save(candidate, reviews.body(candidate, "hold", first["review_id"], "held"))
    with closing(connect_metadata_index()) as c, c:
        if part == "candidate":
            c.execute("DROP TRIGGER journal_candidates_no_update")
            c.execute("UPDATE journal_candidates SET payload_digest='damaged'")
        else:
            c.execute("DROP TRIGGER journal_review_no_update")
            c.execute("UPDATE journal_candidate_reviews SET payload_digest='damaged' WHERE review_id=?", (held["review_id"] if part == "latest" else first["review_id"],))
    with pytest.raises(ValueError, match="integrity_invalid"):
        repo.overview("topic")


def test_overview_reads_one_snapshot_during_a_competing_review(candidate, monkeypatch):
    first, _ = reviews.save(candidate, reviews.body(candidate))
    original = journal_candidates._get
    changed = False
    prepared = Event()
    future = None
    monkeypatch.setattr(repo, "checkpoint", lambda phase: prepared.set() if phase == "after_review" else None)

    def concurrent_review(*args):
        nonlocal changed, future
        value = original(*args)
        if not changed:
            changed = True
            future = pool.submit(reviews.save, candidate, reviews.body(candidate, "hold", first["review_id"], "held"))
            assert prepared.wait(5), "Competing writer must reach its commit while the read is open"
        return value

    monkeypatch.setattr(journal_candidates, "_get", concurrent_review)
    with ThreadPoolExecutor(max_workers=1) as pool:
        row = repo.overview("topic")["items"][0]
        assert future is not None
        future.result(timeout=5)
    assert row["latest_review"]["review_id"] == row["last_acceptance"]["review_id"] == first["review_id"]
    assert repo.overview("topic")["items"][0]["latest_review"]["state"] == "held"


@pytest.mark.parametrize("page,limit", [(0, 10), (1, 0), (1, 101)])
def test_invalid_pagination(page, limit):
    with pytest.raises(ValueError, match="pagination_invalid"):
        repo.overview("topic", page, limit)

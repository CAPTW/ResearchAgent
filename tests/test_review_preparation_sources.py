"""Per-occurrence availability never relaxes capture or workspace authority."""
from pathlib import Path
import json
import re
import sqlite3

import pytest

from app.auth.resource_access import ResourceActor
from app.repositories import paper_reviews
from app.repositories.files import safe_filename
from app.repositories.runs import build_run_paths
from app.services import paper_bank, paper_review, review_preparation
from tests.test_ai_review_provider import _review
from tests import test_review_preparation_api as api

seeded = api.seeded


def seed_sources(topic):
    item = next(item for item in paper_bank.list_topic_papers(topic, page_size=100)["items"]
                if len(item["compatibility"]["occurrences"]) == 2)
    entries = []
    for index, occurrence in enumerate(sorted(item["compatibility"]["occurrences"], key=lambda row: row["runId"])):
        run, paper = occurrence["runId"], occurrence["paperId"]
        path = build_run_paths(run).raw_dir / (safe_filename(paper) + ".txt")
        text = f"Source {index}: e\u0301 한글🙂 preserved methods and results."
        path.write_bytes(text.encode("utf-8"))
        review = _review(index + 1, topic_id=topic, run_id=run, paper_id=paper, title=item["title"])
        review.update(paper_review.read_local_full_text_evidence(run_id=run, paper_id=paper))
        paper_reviews.upsert_review(review)
        entries.append(dict(run_id=run, paper_id=paper, path=str(path), text=text))
    return dict(topic=topic, version=item["paperVersionId"], entries=entries)


@pytest.fixture
def multiple_sources(seeded):
    return seed_sources(seeded.TOPIC_A_ID)


def root(fixture):
    return f'/api/topics/{fixture["topic"]}/paper-bank/{fixture["version"]}/review-preparations'


def request(source, identity):
    return dict(request_id=identity, run_id=source["run_id"], paper_id=source["paper_id"],
                expected_source_digest=source["source_digest"], start=0, end=12)


def assert_diagnostic(entries, run, reason):
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {"diagnostic_id", "run_id", "reason"}
    assert entry["run_id"] == run and entry["reason"] == reason
    assert re.fullmatch(r"[0-9a-f]{64}", entry["diagnostic_id"])


@pytest.mark.parametrize("damage,reason,status", [
    ("changed", "review_preparation_source_changed", 409),
    ("missing", "review_preparation_not_found", 404),
    ("oversized", "review_preparation_source_too_large", 422),
])
def test_unavailable_occurrence_does_not_block_other_source_or_rewrite_history(multiple_sources, damage, reason, status):
    fixture = multiple_sources
    bad, good = fixture["entries"]
    with api.client() as http:
        before = http.get(root(fixture) + "/sources").json()["sources"]
        bodies = [request(source, f"original-{index}") for index, source in enumerate(before)]
        old = http.post(root(fixture), json=bodies[0]).json()
        path = Path(bad["path"])
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"a" * (4 * 1024 * 1024 + 1) if damage == "oversized" else b"Changed bytes")
        response = http.get(root(fixture) + "/sources")
        assert response.status_code == 200, response.text
        listing = response.json()
        assert listing["sources"] == before[1:]
        assert_diagnostic(listing["unavailable_sources"], bad["run_id"], reason)
        assert http.get(root(fixture) + "/sources").json() == listing
        assert "path" not in response.text and "Changed bytes" not in response.text
        # Read/replay remains exact; a new capture still fails closed.
        assert http.get(root(fixture) + "/" + old["preparation_id"]).json() == old
        assert http.post(root(fixture), json=bodies[0]).json() == old
        assert http.post(root(fixture), json=bodies[0] | {"request_id": "new-bad"}).status_code == status
        good_record = http.post(root(fixture), json=bodies[1]).json()
        assert good_record["selection"]["text"] == good["text"][:12]
        assert good_record["execution"]["started"] is False
        assert len(http.get(root(fixture)).json()["items"]) == 2
    with api.client() as http:
        assert http.get(root(fixture) + "/" + old["preparation_id"]).json() == old
        assert http.get(root(fixture) + "/" + good_record["preparation_id"]).json() == good_record


@pytest.mark.parametrize("error", [ValueError("review_extraction_storage_corrupt"), ValueError("unexpected"),
                                    KeyError("unexpected"), sqlite3.OperationalError("private database error")])
def test_storage_or_unknown_failure_is_not_reported_as_partial_success(multiple_sources, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(paper_reviews, "get_review", fail)
    with api.client() as http:
        response = http.get(root(multiple_sources) + "/sources")
        assert response.status_code >= 400
        assert "sources" not in response.json() and "private database error" not in response.text


def test_hidden_occurrences_are_never_observed_or_reported(multiple_sources, monkeypatch):
    def forbidden(*args):
        pytest.fail("hidden source must not be observed")
    monkeypatch.setattr(review_preparation, "_observe", forbidden)
    with pytest.raises((KeyError, FileNotFoundError)):
        review_preparation.sources(multiple_sources["topic"], multiple_sources["version"], ResourceActor.oidc_user("outsider"))


@pytest.mark.parametrize("phase", ["open", "final_guard"])
def test_file_removed_during_observation_is_reported_without_losing_other_source(multiple_sources, monkeypatch, phase):
    bad, good = multiple_sources["entries"]
    target = Path(bad["path"])
    original_open, original_guard = Path.open, review_preparation._guard
    calls = 0

    def guard(path):
        nonlocal calls
        if path == target:
            calls += 1
            if phase == "final_guard" and calls == 3:
                target.unlink()
        return original_guard(path)

    def opening(path, *args, **kwargs):
        if path == target and phase == "open" and target.exists():
            target.unlink()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(review_preparation, "_guard", guard)
    monkeypatch.setattr(Path, "open", opening)
    with api.client() as http:
        response = http.get(root(multiple_sources) + "/sources")
        assert response.status_code == 200, response.text
        assert_diagnostic(response.json()["unavailable_sources"], bad["run_id"], "review_preparation_not_found")
        assert response.json()["sources"][0]["run_id"] == good["run_id"]
        assert not target.exists()


def test_diagnostics_hide_linked_private_aliases_and_skip_unlinked_lookups(seeded, monkeypatch):
    # Source inventory also reads immutable PDF links; use the same isolated DB
    # as the other source tests instead of depending on a missing ambient store.
    aliases = [r"D:\private\paper.json", "file:/private/other.json"]
    actor = ResourceActor.oidc_user("fixture")
    monkeypatch.setattr(review_preparation, "_occurrences", lambda *args: [
        ("run", alias) for alias in [*aliases, "unlinked", "no-body"]])
    monkeypatch.setattr(paper_reviews, "get_review", lambda topic, run, paper, **kwargs:
                        None if paper == "unlinked" else {} if paper == "no-body" else
                        {"full_text_artifact_path": "run:raw/source.pdf"})
    listing = review_preparation.sources("topic", "version", actor)
    assert listing["sources"] == [] and listing["reason"] == "review_preparation_no_supported_source"
    diagnostics = listing["unavailable_sources"]
    assert len(diagnostics) == 2
    assert len({entry["diagnostic_id"] for entry in diagnostics}) == 2
    for entry in diagnostics:
        assert_diagnostic([entry], "run", "review_preparation_source_unsupported")
    assert "private" not in json.dumps(listing) and "paper_id" not in json.dumps(listing)
    assert review_preparation.sources("topic", "version", actor) == listing
    from app.domain.review_preparation import Preview
    for alias in [*aliases, "unlinked", "no-body"]:
        body = Preview(run_id="run", paper_id=alias, expected_source_digest="0" * 64, start=0, length=1)
        error = ValueError if alias in aliases else KeyError
        reason = "review_preparation_source_unsupported" if alias in aliases else "review_preparation_not_found"
        with pytest.raises(error, match=reason):
            review_preparation.preview("topic", "version", body, actor)

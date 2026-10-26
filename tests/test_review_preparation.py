"""Immutable local range preparation, with no model execution or review replacement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib
import json
from threading import Barrier

import pytest

from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.db import bootstrap
from app.repositories import paper_reviews
from app.services import paper_bank
from tests.test_ai_review_provider import _review
from tests import test_temporal_memory

seeded = test_temporal_memory.seeded


def modules():
    return (importlib.import_module("app.domain.review_preparation"),
            importlib.import_module("app.repositories.review_preparations"),
            importlib.import_module("app.services.review_preparation"))


@pytest.fixture
def linked(seeded):
    topic = seeded.TOPIC_A_ID
    item = paper_bank.list_topic_papers(topic, page_size=100)["items"][0]
    occurrence = item["compatibility"]["occurrences"][0]
    run, paper = occurrence["runId"], occurrence["paperId"]
    path = bootstrap.STORAGE_ROOT / run / "raw" / "range-source.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "A" * 30000 + " e\u0301 한글🙂 selected tail."
    path.write_bytes(text.encode("utf-8"))
    review = _review(1, topic_id=topic, run_id=run, paper_id=paper,
                     full_text_artifact_path="run:raw/range-source.txt")
    paper_reviews.upsert_review(review)
    return dict(topic=topic, version=item["paperVersionId"], run=run, paper=paper,
                path=path, text=text, actor=resource_actor_for_user(None), other_topic=seeded.TOPIC_B_ID)


def selected(linked, request="range-1", **changes):
    domain, _, service = modules()
    sources = service.sources(linked["topic"], linked["version"], linked["actor"])
    source = next(s for s in sources["sources"] if s["run_id"] == linked["run"] and s["paper_id"] == linked["paper"])
    return domain.Prepare(**dict(dict(request_id=request, run_id=linked["run"], paper_id=linked["paper"],
                                     expected_source_digest=source["source_digest"], start=30001, end=len(linked["text"])), **changes))


def assert_unavailable_source(linked, reason):
    domain, _, service = modules()
    listing = service.sources(linked["topic"], linked["version"], linked["actor"])
    assert not any(s["run_id"] == linked["run"] and s["paper_id"] == linked["paper"] for s in listing["sources"])
    from tests.test_review_preparation_sources import assert_diagnostic
    assert_diagnostic(listing["unavailable_sources"], linked["run"], reason)
    fields = dict(run_id=linked["run"], paper_id=linked["paper"], expected_source_digest="0" * 64, start=0)
    with pytest.raises(ValueError, match=reason):
        service.prepare(linked["topic"], linked["version"], domain.Prepare(**fields, request_id="invalid-source", end=1), linked["actor"])
    with pytest.raises(ValueError, match=reason):
        service.preview(linked["topic"], linked["version"], domain.Preview(**fields, length=1), linked["actor"])
    assert service.listing(linked["topic"], linked["version"], linked["actor"])["items"] == []


def test_tail_range_preserves_unicode_without_changing_old_review_or_receipt(linked):
    from tests.test_review_input_integration import _seed_receipt
    from app.repositories import ai_review_drafts

    _, _, service = modules()
    old = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
    receipt_id, _ = _seed_receipt(linked["topic"], linked["run"], linked["paper"])
    receipt = ai_review_drafts.get_input_receipt(linked["topic"], receipt_id)
    original = linked["path"].read_bytes()
    body = selected(linked)
    result = service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    assert result["selection"]["text"] == "e\u0301 한글🙂 selected tail."
    assert result["selection"]["chars"] == len(result["selection"]["text"])
    assert result["state"] == "PREPARED_NOT_EXECUTED"
    assert result["execution"] == {"started": False, "full_document_review": "NOT_ESTABLISHED"}
    assert result["source"]["source_chars"] == len(linked["text"])
    assert result["source"]["extraction_truncation"] == "UNKNOWN"
    assert service.detail(linked["topic"], linked["version"], result["preparation_id"], linked["actor"]) == result
    assert paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True) == old
    assert ai_review_drafts.get_input_receipt(linked["topic"], receipt_id) == receipt
    assert linked["path"].read_bytes() == original


def test_source_change_rejected_but_committed_request_replays_after_file_removed(linked):
    _, _, service = modules()
    body = selected(linked)
    first = service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    linked["path"].write_text("changed source", encoding="utf-8")
    assert service.prepare(linked["topic"], linked["version"], body, linked["actor"]) == first
    with pytest.raises(ValueError, match="review_preparation_source_changed"):
        service.prepare(linked["topic"], linked["version"], body.model_copy(update={"request_id": "new"}), linked["actor"])
    with pytest.raises(ValueError, match="review_preparation_request_conflict"):
        service.prepare(linked["topic"], linked["version"], body.model_copy(update={"start": 30002}), linked["actor"])
    linked["path"].unlink()
    assert service.prepare(linked["topic"], linked["version"], body, linked["actor"]) == first
    assert service.detail(linked["topic"], linked["version"], first["preparation_id"], linked["actor"]) == first


@pytest.mark.parametrize("change", [{"start": True}, {"end": "3"}, {"start": -1}, {"start": 5, "end": 5},
                                    {"start": 0, "end": 12001}, {"unexpected": True}])
def test_strict_request_rejects_bad_range(linked, change):
    with pytest.raises(ValueError):
        selected(linked, **change)


def test_out_of_bounds_range_not_persisted(linked):
    _, _, service = modules()
    body = selected(linked, start=len(linked["text"]), end=len(linked["text"]) + 1)
    with pytest.raises(ValueError, match="review_preparation_range_invalid"):
        service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    assert service.listing(linked["topic"], linked["version"], linked["actor"])["items"] == []


def test_pages_source_can_select_later_page_beyond_recorded_prefix(linked):
    _, _, service = modules()
    path = linked["path"].with_name("range-source.pages.json")
    path.write_text(json.dumps({"pages": [{"page": 1, "text": "A" * 30000},
                                         {"page": 9, "text": "e\u0301 한글🙂 selected tail."}]}), encoding="utf-8")
    review = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
    paper_reviews.upsert_review({**review, "full_text_artifact_path": "run:raw/range-source.pages.json"})
    body = selected(linked)
    result = service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    assert result["source"]["source_kind"] == "pages"
    assert result["selection"]["text"] == linked["text"][30001:]


@pytest.mark.parametrize("oversized", ["bytes", "chars"])
def test_oversized_source_is_rejected_not_silently_truncated(linked, oversized):
    _, _, service = modules()
    linked["path"].write_bytes(b"A" * (4 * 1024 * 1024 + 1 if oversized == "bytes" else 1_000_001))
    assert_unavailable_source(linked, "review_preparation_source_too_large")


def test_scoped_history_does_not_decode_other_actor_topic_or_occurrence_corruption(linked, monkeypatch):
    _, repo, service = modules()
    result = service.prepare(linked["topic"], linked["version"], selected(linked), linked["actor"])
    def corrupt(_):
        pytest.fail("hidden row must not be decoded")
    monkeypatch.setattr(repo, "_read", corrupt)
    with pytest.raises((KeyError, FileNotFoundError)):
        service.detail(linked["topic"], linked["version"], result["preparation_id"], ResourceActor.oidc_user("outsider"))
    with pytest.raises((KeyError, FileNotFoundError)):
        service.detail(linked["other_topic"], linked["version"], result["preparation_id"], linked["actor"])
    monkeypatch.setattr(service, "_occurrences", lambda *args: [])
    assert service.listing(linked["topic"], linked["version"], linked["actor"])["items"] == []
    with pytest.raises(KeyError):
        service.detail(linked["topic"], linked["version"], result["preparation_id"], linked["actor"])


def test_duplicate_concurrent_requests_have_one_immutable_row_and_first_timestamp(linked):
    _, _, service = modules()
    body = selected(linked)
    barrier = Barrier(4)
    def prepare(_):
        barrier.wait()
        return service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(prepare, range(4)))
    assert all(record == results[0] for record in results)
    assert len(service.listing(linked["topic"], linked["version"], linked["actor"])["items"]) == 1


def test_rollback_keeps_retry_available_and_pagination_is_read_only(linked, monkeypatch):
    _, repo, service = modules()
    body = selected(linked)
    def fail(phase):
        if phase == "after_insert":
            raise RuntimeError("fixture rollback")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError, match="fixture rollback"):
        service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    assert service.listing(linked["topic"], linked["version"], linked["actor"])["items"] == []
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    first = service.prepare(linked["topic"], linked["version"], body, linked["actor"])
    second = service.prepare(linked["topic"], linked["version"], selected(linked, "range-2", start=30002), linked["actor"])
    before = bootstrap.metadata_index_path().read_bytes()
    page1 = service.listing(linked["topic"], linked["version"], linked["actor"], page_size=1)
    page2 = service.listing(linked["topic"], linked["version"], linked["actor"], page=2, page_size=1)
    assert page1["hasMore"] is True and page2["hasMore"] is False
    assert {page1["items"][0]["preparation_id"], page2["items"][0]["preparation_id"]} == {first["preparation_id"], second["preparation_id"]}
    assert "text" not in page1["items"][0]["selection"]
    assert bootstrap.metadata_index_path().read_bytes() == before
    with closing(bootstrap.connect_metadata_index()) as conn, conn:
        with pytest.raises(Exception, match="review_preparation_immutable"):
            conn.execute("DELETE FROM paper_review_preparations")


def test_visible_corruption_is_unavailable_while_other_scope_skips_decode(linked):
    _, _, service = modules()
    result = service.prepare(linked["topic"], linked["version"], selected(linked), linked["actor"])
    with closing(bootstrap.connect_metadata_index()) as conn, conn:
        conn.execute("DROP TRIGGER review_preparations_no_update")
        row = conn.execute("SELECT payload_json FROM paper_review_preparations").fetchone()
        payload = json.loads(row[0])
        payload["selection"]["text"] = payload["selection"]["text"].replace("e\u0301", "é")
        conn.execute("UPDATE paper_review_preparations SET payload_json=?", (json.dumps(payload),))
    with pytest.raises(ValueError, match="review_preparation_integrity_invalid"):
        service.detail(linked["topic"], linked["version"], result["preparation_id"], linked["actor"])
    with pytest.raises(KeyError):
        service.detail(linked["other_topic"], linked["version"], result["preparation_id"], linked["actor"])


@pytest.mark.parametrize("kind", ["outside", "reparse", "hardlink"])
def test_source_cannot_follow_link_or_escape_its_run(linked, tmp_path, monkeypatch, kind):
    _, _, service = modules()
    if kind == "outside":
        outside = tmp_path / "owner.txt"
        outside.write_text("private owner source")
        review = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
        paper_reviews.upsert_review({**review, "full_text_artifact_path": str(outside)})
    elif kind == "hardlink":
        (linked["path"].parent / "copy.txt").hardlink_to(linked["path"])
    else:
        real = service.is_reparse_point
        # Simulated filesystem guard, not a real Windows junction exercise.
        monkeypatch.setattr(service, "is_reparse_point", lambda path: path == linked["path"] or real(path))
    assert_unavailable_source(linked, "review_preparation_source_invalid")


def test_read_only_template_actor_cannot_prepare_even_when_topic_has_sources(linked, monkeypatch):
    _, _, service = modules()
    body = selected(linked)
    profiles = service.review_input_history.topic_profiles
    # Exercise the public-template policy without corrupting the run/profile ID binding.
    monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {linked["topic"]})
    with pytest.raises(KeyError):
        service.prepare(linked["topic"], linked["version"], body, ResourceActor.oidc_user("outsider"))
    with closing(bootstrap.connect_metadata_index(read_only=True)) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='paper_review_preparations'").fetchone() is None


def test_preparation_reads_only_selected_artifact_not_adjacent_pdf_or_markers(linked, monkeypatch):
    from app.services import pdf_acquisition
    _, _, service = modules()
    def forbidden(*args, **kwargs):
        pytest.fail("preparation must not read sibling PDF/marker paths")
    monkeypatch.setattr(pdf_acquisition, "read_extraction_provenance", forbidden)
    linked["path"].with_suffix(".pdf").write_bytes(b"Unrelated synthetic PDF bytes")
    linked["path"].with_suffix(".extraction.json").write_text("untrusted sibling")
    result = service.prepare(linked["topic"], linked["version"], selected(linked), linked["actor"])
    assert result["selection"]["text"] == linked["text"][30001:]


def test_invalid_utf8_cannot_be_silently_removed_from_new_preparation(linked):
    _, _, service = modules()
    linked["path"].write_bytes(b"before\xffafter")
    assert_unavailable_source(linked, "review_preparation_source_invalid")


@pytest.mark.parametrize("value", [42, {"injected": "not text"}, ["not", "text"]])
def test_malformed_pages_content_is_not_fabricated_as_source_text(linked, value):
    _, _, service = modules()
    path = linked["path"].with_name("range-source.pages.json")
    path.write_text(json.dumps({"pages": [{"page": 1, "text": value}]}), encoding="utf-8")
    review = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
    paper_reviews.upsert_review({**review, "full_text_artifact_path": "run:raw/range-source.pages.json"})
    assert_unavailable_source(linked, "review_preparation_source_invalid")


@pytest.mark.parametrize("raw", [
    '{"pages":[{"text":"original","text":"replacement"}]}',
    '{"pages":[{"content":"original","content":"replacement"}]}',
    '{"pages":[{"text":"first","content":"different"}]}',
    '{"pages":[{"text":"valid","page":NaN}]}',
    '{"pages":[{"text":"valid","page":Infinity}]}',
])
def test_ambiguous_or_nonstandard_pages_json_is_rejected(linked, raw):
    _, _, service = modules()
    path = linked["path"].with_name("range-source.pages.json")
    path.write_text(raw, encoding="utf-8")
    review = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
    paper_reviews.upsert_review({**review, "full_text_artifact_path": "run:raw/range-source.pages.json"})
    assert_unavailable_source(linked, "review_preparation_source_invalid")


def test_pagination_rejects_offset_outside_sqlite_integer_range(linked):
    _, _, service = modules()
    service.prepare(linked["topic"], linked["version"], selected(linked), linked["actor"])
    with pytest.raises(ValueError, match="review_preparation_request_invalid"):
        service.listing(linked["topic"], linked["version"], linked["actor"], page=9223372036854775808)


def test_unsupported_artifact_does_not_enter_file_observation(monkeypatch):
    _, _, service = modules()
    monkeypatch.setattr(paper_reviews, "get_review", lambda *args, **kwargs: {"full_text_artifact_path": "run:raw/paper.pdf"})
    def forbidden(*args):
        pytest.fail("unsupported PDF must not be observed as a text source")
    monkeypatch.setattr(service, "_source_path", forbidden)
    with pytest.raises(ValueError, match="review_preparation_source_unsupported"):
        service._observe("topic", "run", "paper")

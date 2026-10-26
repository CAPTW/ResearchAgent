"""Historical source coordinates, not scientific or whole-document review coverage."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import importlib
from threading import Event

from fastapi.testclient import TestClient
import pytest

from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.db import bootstrap
from app.domain import review_preparation as domain
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_preparations
from app.services import review_preparation as service
from tests import test_review_preparation as preparation_tests
from tests.test_review_range_input import receipt_record

seeded = preparation_tests.seeded
linked = preparation_tests.linked


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.connect_metadata_index().close()
    return dict(topic="topic", version="version", run="run", paper="paper",
                text="0123456789 e\u0301 한글🙂 ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def save(context, request, start=10, end=17, *, text=None):
    text = context["text"] if text is None else text
    source = dict(run_id=context["run"], paper_id=context["paper"], source_chars=len(text), source_kind="text",
                  artifact_sha256=domain.sha(text), artifact_bytes=len(text.encode("utf-8")),
                  artifact_ref="run:raw/source.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = domain.source_digest(source)
    body = domain.Prepare(request_id=request, run_id=context["run"], paper_id=context["paper"],
                          expected_source_digest=source["source_digest"], start=start, end=end)
    return review_preparations.save(context["topic"], context["version"], "owner", body,
                                   [(context["run"], context["paper"])], lambda: (source, text))


def append(preparation, origin="FIXTURE_SYNTHETIC"):
    packet = build_packet(preparation)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        return ai_review_drafts.append_input_receipt_in(c, receipt_record(packet), input_packet=packet,
                                                        input_origin=origin)


def coverage(context, anchor, *, occurrences=None):
    repository = importlib.import_module("app.repositories.review_source_coverage")
    return repository.overview(context["topic"], context["version"], anchor["preparation_id"],
                               occurrences if occurrences is not None else [(context["run"], context["paper"])])


def test_union_uses_original_unicode_offsets_deduplicates_overlap_and_separates_origins(store):
    first = save(store, "first")
    second = save(store, "second", 15, 23)
    adjacent = save(store, "adjacent", 23, 26)
    nested = save(store, "nested", 12, 15)
    disjoint = save(store, "disjoint", 35, 37)
    assert save(store, "first") == first
    one = append(first)
    assert append(first) == one
    two = append(second)
    provider = append(first, "PROVIDER_RESPONSE")  # Stored fixture metadata, no Provider call.
    manual = append(disjoint, "MANUAL_IMPORT_UNVERIFIED")
    result = coverage(store, first)
    assert result["schema_version"] == "PAPER_REVIEW_SOURCE_COVERAGE_V1"
    assert result["anchor_preparation_id"] == first["preparation_id"]
    assert result["source"] == first["source"]
    assert result["normalization"] == "XRML_WHITESPACE_V1" and result["decoding"] == "UTF8_STRICT"
    prepared = result["prepared"]
    assert prepared["record_count"] == 5
    assert prepared["covered_chars"] == 18
    assert prepared["ranges"] == [dict(start=10, end=26), dict(start=35, end=37)]
    assert prepared["gaps"] == [dict(start=0, end=10), dict(start=26, end=35),
                                 dict(start=37, end=len(store["text"]))]
    assert {row["preparation_id"] for row in prepared["records"]} == {
        value["preparation_id"] for value in (first, second, adjacent, nested, disjoint)}
    inputs = {row["input_origin"]: row for row in result["inputs"]}
    assert set(inputs) == ai_review_drafts.INPUT_ORIGINS
    assert inputs["FIXTURE_SYNTHETIC"]["record_count"] == 2
    assert inputs["FIXTURE_SYNTHETIC"]["covered_chars"] == 13
    assert inputs["FIXTURE_SYNTHETIC"]["ranges"] == [dict(start=10, end=23)]
    assert {row["receipt_id"] for row in inputs["FIXTURE_SYNTHETIC"]["records"]} == {one["receipt_id"], two["receipt_id"]}
    assert inputs["PROVIDER_RESPONSE"]["records"] == [dict(receipt_id=provider["receipt_id"],
        preparation_id=first["preparation_id"], start=10, end=17)]
    assert inputs["MANUAL_IMPORT_UNVERIFIED"]["records"][0]["receipt_id"] == manual["receipt_id"]
    assert result["unmapped_receipt_count"] == result["other_source_receipt_count"] == 0
    assert result["full_document_review"] == "NOT_ESTABLISHED"
    assert result["scientific_support"] == "NOT_ASSESSED"


def test_distinct_observation_and_unmapped_legacy_are_not_zero_reviewed_coverage(store):
    from tests.test_review_input_integration import _seed_receipt
    first = save(store, "first")
    changed = save(store, "changed", text=store["text"] + " changed")
    append(changed)
    _seed_receipt(store["topic"], store["run"], store["paper"])
    result = coverage(store, first)
    assert result["prepared"]["record_count"] == 1
    assert result["other_source_receipt_count"] == result["unmapped_receipt_count"] == 1
    assert all(row["record_count"] == row["covered_chars"] == 0 and row["ranges"] == row["records"] == []
               for row in result["inputs"])


@pytest.mark.parametrize("change", ["missing", "digest", "other_source_digest", "other_version"])
def test_self_consistent_receipt_requires_real_canonical_preparation_even_for_other_source(store, change):
    first = save(store, "first")
    target = save(store, "changed", text=store["text"] + " changed") if change == "other_source_digest" else first
    if change == "other_version":
        target = save({**store, "version": "another-version"}, "different-version")
    forged = deepcopy(target)
    if change == "missing":
        body = domain.Prepare(request_id="never-saved", run_id=store["run"], paper_id=store["paper"],
            expected_source_digest=target["source"]["source_digest"], start=10, end=17)
        forged = domain.build(store["topic"], store["version"], "owner", body, target["source"],
                              store["text"], "2026-10-02T00:00:00Z")
    elif change != "other_version":
        forged["created_at"] = "2026-10-02T00:00:00Z" if target["created_at"] != "2026-10-02T00:00:00Z" else "2026-10-03T00:00:00Z"
    append(forged)
    with pytest.raises(ValueError, match="review_source_coverage_integrity_invalid"):
        coverage(store, first)


@pytest.mark.parametrize("kind", ["preparation", "receipt"])
def test_visible_corruption_fails_closed(store, kind):
    first = save(store, "first")
    append(first)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        if kind == "preparation":
            c.execute("DROP TRIGGER review_preparations_no_update")
            c.execute("UPDATE paper_review_preparations SET payload_json='{}'")
        else:
            c.execute("DROP TRIGGER ai_review_input_no_update")
            c.execute("UPDATE ai_review_input_history SET packet_json='{}'")
    with pytest.raises(ValueError, match="integrity_invalid|receipt_corrupt"):
        coverage(store, first)


def test_hidden_topic_version_and_occurrence_rows_are_scoped_before_decoding(store):
    first = save(store, "first")
    hidden = []
    for label, changes in (("topic", {"topic": "private"}), ("version", {"version": "private"}),
                           ("occurrence", {"run": "unrelated"})):
        hidden.append(save({**store, **changes}, label))
    # Receipts have no paper_version column. Do not seed a receipt of another
    # version sharing this authorized occurrence: its canonical link is checked.
    for item in (hidden[0], hidden[2]):
        append(item)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER review_preparations_no_update")
        c.execute("UPDATE paper_review_preparations SET payload_json='{}' WHERE preparation_id<>?", (first["preparation_id"],))
        c.execute("DROP TRIGGER ai_review_input_no_update")
        c.execute("UPDATE ai_review_input_history SET packet_json='{}'")
    result = coverage(store, first)
    assert result["prepared"]["record_count"] == 1
    assert result["unmapped_receipt_count"] == result["other_source_receipt_count"] == 0
    for context, occurrences in (({**store, "topic": "other"}, None), ({**store, "version": "other"}, None), (store, [])):
        with pytest.raises(KeyError):
            coverage(context, first, occurrences=occurrences)


@pytest.mark.parametrize("kind", ["preparations", "receipts"])
def test_limit_rejects_incomplete_projection_instead_of_truncating(store, monkeypatch, kind):
    repository = importlib.import_module("app.repositories.review_source_coverage")
    first = save(store, "first")
    if kind == "preparations":
        save(store, "second", 15, 23)
    else:
        append(first)
        append(first, "MANUAL_IMPORT_UNVERIFIED")
    monkeypatch.setattr(repository, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="review_source_coverage_limit_exceeded"):
        coverage(store, first)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_anchor_preparations_and_receipts_share_one_read_snapshot(store, monkeypatch, journal):
    first = save(store, "first")
    with closing(bootstrap.connect_metadata_index()) as c:
        assert c.execute(f"PRAGMA journal_mode={journal}").fetchone()[0].upper() == journal
    original = review_preparations.detail_in
    inserted = Event()
    future = None
    def checkpoint(phase):
        if phase == "after_insert":
            inserted.set()
    monkeypatch.setattr(review_preparations, "checkpoint", checkpoint)
    def write():
        second = save(store, "during-read", 17, 23)
        append(second)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def observed(*args, **kwargs):
            nonlocal future
            result = original(*args, **kwargs)
            if future is None:
                future = pool.submit(write)
                assert inserted.wait(10)
                if journal == "WAL":
                    future.result(timeout=10)
            return result
        monkeypatch.setattr(review_preparations, "detail_in", observed)
        before = coverage(store, first)
        assert future is not None
        future.result(timeout=10)
    assert before["prepared"]["record_count"] == 1
    assert all(row["record_count"] == 0 for row in before["inputs"])
    after = coverage(store, first)
    assert after["prepared"]["record_count"] == 2
    assert next(row for row in after["inputs"] if row["input_origin"] == "FIXTURE_SYNTHETIC")["record_count"] == 1


def test_http_read_only_restart_missing_source_and_oidc_scope(linked, monkeypatch):
    from app.api import paper_bank as api
    from app.main import create_app
    first = service.prepare(linked["topic"], linked["version"], preparation_tests.selected(linked), linked["actor"])
    append(first)
    linked["path"].unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("coverage must not observe current artifact files")
    monkeypatch.setattr(service, "_observe", forbidden)
    root = f'/api/topics/{linked["topic"]}/paper-bank/{linked["version"]}/review-preparations/{first["preparation_id"]}/coverage'
    def client():
        app = create_app()
        app.dependency_overrides[require_product_user] = lambda: None
        return TestClient(app)
    saved = None
    for _ in range(2):
        with client() as http:
            before = bootstrap.metadata_index_path().read_bytes()
            response = http.get(root)
            assert response.status_code == 200, response.text
            assert bootstrap.metadata_index_path().read_bytes() == before
            assert saved is None or response.json() == saved
            saved = response.json()
            assert http.get(root.replace(first["preparation_id"], "missing")).status_code == 404
            assert http.get(root.replace(linked["topic"], linked["other_topic"])).status_code == 404
    with client() as http:
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user"))
        assert http.get(root).json() == saved
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.get(root).status_code == 404
        profiles = service.review_input_history.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {linked["topic"]})
        assert http.get(root).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("owner", is_admin=True))
        # Global/template administration does not bypass private run ownership.
        assert http.get(root).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
        assert http.get(root).json() == saved
        repository = importlib.import_module("app.repositories.review_source_coverage")
        monkeypatch.setattr(repository, "MAX_RECORDS", 0)
        response = http.get(root)
        assert response.status_code == 503
        assert response.json()["detail"] == "review_source_coverage_limit_exceeded"

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from app.domain.temporal_memory import Member, canonical_json, digest, snapshot, diff, EVENT_TYPES
from app.repositories import temporal_memory as repo


def member(**patch):
    return replace(Member(canonical_paper_id="paper-a", paper_version_id="version-a"), **patch)


def test_canonical_order_nfc_strict_and_unknown():
    assert canonical_json({"b": "e\u0301", "a": 1}) == canonical_json({"a": 1, "b": "é"})
    assert len({digest(None), digest(False), digest(0)}) == 3
    with pytest.raises(ValueError):
        digest(float("nan"))


def test_snapshot_order_sets_timestamp_labels_and_immutability():
    a = member(occurrence_ids=("b", "a", "a"))
    b = member(canonical_paper_id="paper-b", paper_version_id="version-b")
    x = snapshot("topic", [a, b], "2026-01-01T00:00:00Z")
    y = snapshot("topic", [b, replace(a, occurrence_ids=("a", "b"))], "2026-02-01T00:00:00Z")
    assert x.snapshot_id == y.snapshot_id
    assert x.corpus_digest == y.corpus_digest
    assert len(x.snapshot_id) == 70
    assert snapshot("topic", [replace(a, review_state="reviewed"), b], "now").corpus_digest != x.corpus_digest
    with pytest.raises(FrozenInstanceError):
        x.paper_count = 9


def test_all_eight_events_version_precedence_order_direction_provenance():
    a = member()
    removed = member(canonical_paper_id="removed", paper_version_id="removed-v")
    added = member(canonical_paper_id="added", paper_version_id="added-v")
    changed = replace(a, paper_version_id="version-b", collection_ids=("collection",),
                      review_state="reviewed", full_text_state="reviewed", evidence_ids=("ev",),
                      evidence_digests=(("ev", "sha"),), rights_access_state="open_access")
    before = snapshot("topic", [removed, a], "first")
    after = snapshot("topic", [changed, added], "second")
    events = diff(before, after)
    assert [e.event_type for e in events] == list(EVENT_TYPES)
    assert all(e.canonical_paper_id != "paper-a" for e in events[:2])
    assert diff(before, before) == ()
    assert diff(before, after) == events
    assert diff(after, before)[0].canonical_paper_id == "removed"
    assert all(e.source_snapshot_id == after.snapshot_id and e.algorithm_version and e.acceptance_state for e in events)
    assert all(e.confidence == 1.0 for e in events)


def test_pair_topic_and_schema_conflicts():
    x = snapshot("a", [], "now")
    with pytest.raises(ValueError, match="cross_topic"):
        diff(x, snapshot("b", [], "now"))
    with pytest.raises(ValueError, match="schema"):
        diff(x, replace(x, schema_version=2))


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.db import bootstrap
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    return repo


def test_store_dedup_first_adjacent_immutable_and_read_only(store):
    first, created = store.capture("topic", lambda conn: [member()])
    assert created
    assert store.detail("topic", first.snapshot_id)["events"] == []
    duplicate, created = store.capture("topic", lambda conn: [member()])
    assert not created and duplicate == first
    second, created = store.capture("topic", lambda conn: [member(review_state="reviewed")])
    assert created and second.parent_snapshot_id == first.snapshot_id
    assert store.detail("topic", first.snapshot_id)["snapshot"] == first
    assert len(store.detail("topic", second.snapshot_id)["events"]) == 1
    assert store.list_snapshots("topic", 1, 1)["items"] == [second]
    assert store.list_snapshots("topic", 2, 1)["items"] == [first]
    with pytest.raises(KeyError):
        store.detail("other", first.snapshot_id)
    with pytest.raises(KeyError):
        store.detail("topic", "missing")


def test_concurrent_identical_capture_at_most_one(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.capture("topic", lambda conn: [member()]), range(8)))
    assert sum(created for _, created in results) == 1
    assert len({s.snapshot_id for s, _ in results}) == 1


@pytest.mark.parametrize("change", ["content", "locator", "missing_paper", "unavailable_catalog"])
def test_comparison_keeps_history_but_never_links_changed_current_evidence(store, monkeypatch, change):
    from app.services import paper_bank, temporal_memory as service

    monkeypatch.setattr(service.topic_profiles, "get_topic_profile", lambda *args, **kwargs: {})
    span = {"evidence_span_id": "ev", "content_sha256": "a" * 64, "locator": {"page": 1}}
    captured = member(
        evidence_state="available", evidence_ids=("ev",),
        evidence_digests=(("ev", digest({"content_digest": span["content_sha256"], "locator": span["locator"]})),),
    )
    before, _ = store.capture("topic", lambda _: [member()])
    after, _ = store.capture("topic", lambda _: [captured])
    persisted = store.detail("topic", after.snapshot_id)
    review = {"run_id": "run", "evidence_spans": [span]}
    catalog = [{"paperVersionId": "version-a", "_selectedReview": review}]
    monkeypatch.setattr(paper_bank, "_load_catalog", lambda *args, **kwargs: (catalog, False, {}, None))
    original = service.comparison("topic", before.snapshot_id, after.snapshot_id, None)
    assert original["events"][0]["evidence_destinations"][0]["id"] == "ev"

    if change == "content":
        review["evidence_spans"] = [{**span, "content_sha256": "b" * 64}]
    elif change == "locator":
        review["evidence_spans"] = [{**span, "locator": {"page": 2}}]
    elif change == "missing_paper":
        catalog.clear()
    else:
        def unavailable(*args, **kwargs):
            raise OSError("test-only unavailable catalog")
        monkeypatch.setattr(paper_bank, "_load_catalog", unavailable)

    current = service.comparison("topic", before.snapshot_id, after.snapshot_id, None)
    event = current["events"][0]
    assert event["evidence_destinations"] == []
    assert event["unavailable_evidence_ids"] == ["ev"]
    if change in ("missing_paper", "unavailable_catalog"):
        assert event["paper_destination"] is None
        assert event["destination_reason"] == (
            "NO_CURRENT_DESTINATION" if change == "missing_paper" else "CURRENT_DESTINATIONS_UNAVAILABLE"
        )
    else:
        assert event["paper_destination"] == "/topics/topic/papers?paper=version-a"
    for key in ("event_id", "before_state", "after_state", "source_entity_ids", "source_evidence_ids"):
        assert event[key] == original["events"][0][key]
    assert current["counts"] == original["counts"]
    assert store.detail("topic", after.snapshot_id) == persisted


def test_atomic_migration_and_capture_rollback_retry(store, monkeypatch):
    def fail(phase):
        if phase == "after_members":
            raise RuntimeError("injected failure")
    monkeypatch.setattr(store, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        store.capture("topic", lambda conn: [member()])
    assert store.list_snapshots("topic", 1, 10)["items"] == []
    monkeypatch.setattr(store, "checkpoint", lambda _: None)
    assert store.capture("topic", lambda conn: [member()])[1]


@pytest.mark.parametrize("field,value", [
    ("occurrence_ids", ("occ-new",)), ("collection_ids", ("collection",)),
    ("review_state", "abstract_reviewed"), ("full_text_state", "reviewed"),
    ("full_text_content_digest", "content"), ("evidence_ids", ("evidence",)),
    ("evidence_digests", (("evidence", "content"),)), ("evidence_state", "available"),
    ("rights_access_state", "restricted"), ("rights_declaration_digest", "license"),
])
def test_each_authoritative_field_changes_member_and_source_revision(field, value):
    before = snapshot("topic", [member()], "now")
    after = snapshot("topic", [member(**{field: value})], "now")
    assert before.corpus_digest != after.corpus_digest
    assert before.source_revision != after.source_revision


def test_schema_extension_preserves_old_rows_startup_and_enforces_insert_references(store):
    from app.db.bootstrap import connect_metadata_index
    import sqlite3
    with connect_metadata_index() as connection:
        connection.execute("INSERT INTO index_state VALUES ('m3a-preserve', 'original')")
    current, _ = store.capture("topic", lambda conn: [member()])
    with connect_metadata_index() as connection:
        assert connection.execute("SELECT value FROM index_state WHERE key='m3a-preserve'").fetchone()[0] == "original"
        store.initialize(connection)
        for operation in ("UPDATE topic_snapshots SET paper_count=2", "DELETE FROM topic_snapshots", "DELETE FROM topic_snapshot_members"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(operation)
        with pytest.raises(sqlite3.IntegrityError, match="parent_invalid"):
            connection.execute("INSERT INTO topic_change_events VALUES ('bad','other','missing',?,'PAPER_ADDED',0,'{}')", (current.snapshot_id,))


@pytest.mark.parametrize("phase", ["after_snapshot", "after_members", "after_events"])
def test_rollback_keeps_previous_snapshot_and_events_exact(store, monkeypatch, phase):
    first, _ = store.capture("topic", lambda conn: [member()])
    def fail(current):
        if current == phase:
            raise RuntimeError("rollback")
    monkeypatch.setattr(store, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        store.capture("topic", lambda conn: [member(review_state="reviewed")])
    assert store.list_snapshots("topic")["items"] == [first]
    assert store.detail("topic", first.snapshot_id)["events"] == []


def test_nfc_identity_duplicate_and_multiversion_membership():
    with pytest.raises(ValueError, match="duplicate"):
        snapshot("topic", [member(canonical_paper_id="é"), member(canonical_paper_id="e\u0301")], "now")
    result = snapshot("topic", [member(), member(paper_version_id="v2")], "now")
    assert result.paper_count == 2


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    from tests.conftest import _patch_storage_roots
    fixture_path = Path(__file__).parent / "helpers" / "m2_paper_bank_fixture.py"
    spec = importlib.util.spec_from_file_location("m3a_seed", fixture_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "m3a-seed"
    _patch_storage_roots(monkeypatch, root, set_env=True)
    monkeypatch.setattr(module, "STORAGE_ROOT", root / "runs")
    from app.repositories import paper_collections
    monkeypatch.setattr(paper_collections, "COLLECTION_LIBRARY_PATH", root / "paper_collections" / "collections.json")
    module.seed_fixture(root)
    return module


def test_real_source_capture_exact_identity_evidence_no_bodies_and_local_mutation(seeded):
    from app.auth.resource_access import resource_actor_for_user
    from app.repositories import paper_collections
    from app.services import temporal_memory as service, paper_bank
    actor = resource_actor_for_user(None)
    first = service.capture(seeded.TOPIC_A_ID, actor)
    assert first["created"]
    detail = service.detail(seeded.TOPIC_A_ID, first["snapshot"]["snapshot_id"], actor)
    bank = paper_bank.list_topic_papers(seeded.TOPIC_A_ID, page_size=100)
    assert {m["paper_version_id"] for m in detail["snapshot"]["members"]} == {m["paperVersionId"] for m in bank["items"]}
    encoded = canonical_json(detail)
    assert "Fallback evidence is bound" not in encoded
    assert "text_excerpt" not in encoded and "full_text_artifact_path" not in encoded
    assert "m2-fallback-evidence-a" in encoded
    selected = bank["items"][0]
    paper_collections.add_member(seeded.TOPIC_A_ID, "saved-papers", canonical_paper_id=selected["canonicalPaperId"], paper_version_id=selected["paperVersionId"])
    second = service.capture(seeded.TOPIC_A_ID, actor)
    result = service.comparison(seeded.TOPIC_A_ID, first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"], actor)
    assert result["counts"]["COLLECTION_MEMBERSHIP_CHANGED"] == 1
    assert len(result["events"]) == 1
    assert result["events"][0]["calculated_at"] == result["calculated_at"]
    assert result["calculated_at"] >= second["snapshot"]["created_at"]


def test_source_publication_during_capture_is_conflict_and_rolls_back(seeded, monkeypatch):
    from app.repositories import temporal_sources
    from app.auth.resource_access import resource_actor_for_user
    from app.services import temporal_memory as service
    original = temporal_sources._manifests
    calls = 0
    def changed():
        nonlocal calls
        calls += 1
        result = original()
        return {} if calls == 2 else result
    monkeypatch.setattr(temporal_sources, "_manifests", changed)
    with pytest.raises(ValueError, match="capture_conflict"):
        service.capture(seeded.TOPIC_A_ID, resource_actor_for_user(None))
    assert repo.list_snapshots(seeded.TOPIC_A_ID)["total"] == 0


def test_real_api_status_pagination_missing_cross_topic_direction_no_execution(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.repositories import runs
    import httpx
    import urllib.request
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail("M3-A must not create a run")
    monkeypatch.setattr(runs, "create_run_record", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as client:
        root = f"/api/topics/{seeded.TOPIC_A_ID}/memory"
        first = client.post(root + "/snapshots")
        assert first.status_code == 201, first.text
        identity = first.json()["snapshot"]["snapshot_id"]
        assert client.post(root + "/snapshots").status_code == 200
        assert client.get(root + "/snapshots?pageSize=101").status_code == 422
        assert client.get(root + "/snapshots?page=0").status_code == 422
        assert client.get(root + "/snapshots/missing").status_code == 404
        same = client.get(root + "/diff", params={"from": identity, "to": identity})
        assert same.status_code == 200 and same.json()["events"] == []
        other = client.post(f"/api/topics/{seeded.TOPIC_B_ID}/memory/snapshots").json()["snapshot"]["snapshot_id"]
        assert client.get(root + "/diff", params={"from": identity, "to": other}).status_code == 422
        assert client.get("/api/topics/missing-topic/memory/snapshots").status_code == 404
        assert client.delete(root + "/snapshots/" + identity).status_code == 405


def test_arbitrary_nonadjacent_pair_reverse_and_persisted_equivalence(store):
    a, _ = store.capture("topic", lambda conn: [member()])
    b, _ = store.capture("topic", lambda conn: [member(review_state="reviewed")])
    c, _ = store.capture("topic", lambda conn: [member(review_state="reviewed", collection_ids=("saved",))])
    assert [e.event_type for e in diff(*store.pair("topic", a.snapshot_id, c.snapshot_id))] == ["COLLECTION_MEMBERSHIP_CHANGED", "REVIEW_STATE_CHANGED"]
    assert diff(*store.pair("topic", c.snapshot_id, a.snapshot_id))[0].before_state != diff(a, c)[0].before_state
    from dataclasses import asdict
    assert canonical_json(store.detail("topic", c.snapshot_id)["events"]) == canonical_json([asdict(e) for e in diff(b, c)])


@pytest.mark.parametrize("page,size", [(0, 1), (1, 0), (1, 101), (-1, 25)])
def test_repository_pagination_validation(store, page, size):
    with pytest.raises(ValueError, match="pagination"):
        store.list_snapshots("topic", page, size)


def test_uninitialized_read_does_not_migrate_or_write(store):
    from app.db.bootstrap import connect_metadata_index, metadata_index_path
    with connect_metadata_index():
        pass
    before = metadata_index_path().read_bytes()
    assert store.list_snapshots("topic")["items"] == []
    assert metadata_index_path().read_bytes() == before


@pytest.mark.parametrize("code,status", [("snapshot_schema_conflict",409), ("snapshot_capture_conflict",409), ("cross_topic_pair",422), ("private path C:/private",422)])
def test_typed_failures_redacted(code, status):
    from app.api.temporal_memory import invoke
    from fastapi import HTTPException
    def operation(actor):
        raise ValueError(code)
    with pytest.raises(HTTPException) as caught:
        invoke(None, operation)
    assert caught.value.status_code == status
    assert "C:/" not in str(caught.value.detail)

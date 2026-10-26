from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.db import bootstrap
from app.domain.temporal_memory import Member
from app.repositories import temporal_analysis as repo, temporal_memory as memory


@pytest.fixture
def record(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    policy, _ = repo.create_policy("topic", {})
    for members in [[Member("paper", "version")], [Member("paper", "version"), Member("paper-b", "version-b")]]:
        snapshot, _ = memory.capture("topic", lambda _: members)
        repo.evaluate("topic", snapshot.snapshot_id, policy.policy_id)
    return next(i for i in repo.insights("topic")["items"]
                if i["insight_kind"] == "CORPUS_SIZE_CHANGE" and i["state"] == "ACCEPTED")["insight_record_id"]


def save(record, request="request-1", state="held", previous=None, note="Need to read the evidence"):
    return repo.decide("topic", record, request, state, note, previous, "owner")


def test_append_only_history_replay_and_stale_conflict(record):
    original = repo.insights("topic")
    first, created = save(record)
    assert created
    assert save(record) == (first, False)
    second, _ = save(record, "request-2", "accepted", first["decision_id"])
    assert save(record) == (first, False)  # Lost response retry after later decisions.
    assert repo.decisions("topic", record)["items"] == [second, first]
    assert repo.insights("topic") == original
    # Additive decisions must leave existing capture/evaluation writers usable.
    later, _ = memory.capture("topic", lambda _: [Member("paper-c", "version-c")])
    repo.evaluate("topic", later.snapshot_id, repo.policies("topic")["items"][0]["policy_id"])
    assert repo.decisions("topic", record)["items"] == [second, first]
    with pytest.raises(ValueError, match="decision_conflict"):
        save(record, "stale")
    with pytest.raises(ValueError, match="decision_request_conflict"):
        save(record, note="different payload")
    with bootstrap.connect_metadata_index() as c:
        for sql in ["UPDATE temporal_insight_decisions SET note='changed'", "DELETE FROM temporal_insight_decisions"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)


def test_concurrent_duplicate_and_competing_decisions(record):
    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(lambda _: save(record), range(8)))
    assert sum(created for _, created in repeated) == 1
    previous = repeated[0][0]["decision_id"]

    def competing(n):
        try:
            return save(record, f"next-{n}", "rejected", previous)[1]
        except ValueError as exc:
            assert str(exc) == "decision_conflict"
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(competing, range(4))) == 1
    assert repo.decisions("topic", record)["total"] == 2


def test_rollback_and_topic_binding(record, monkeypatch):
    def fail(phase):
        if phase == "after_decision":
            raise RuntimeError("injected")

    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError, match="injected"):
        save(record)
    assert repo.decisions("topic", record)["total"] == 0
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    assert save(record)[1]
    for topic, identity in [("other", record), ("topic", "missing")]:
        with pytest.raises(KeyError, match="insight_not_found"):
            repo.decisions(topic, identity)
        with pytest.raises(KeyError, match="insight_not_found"):
            repo.decide(topic, identity, "key", "held", "", None, "owner")


def test_api_actor_validation_conflict_and_readonly_history(record, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.temporal_analysis import router
    from app.auth.security import require_product_user
    from app.services import topic_profiles

    monkeypatch.setattr(topic_profiles, "get_topic_profile", lambda *a, **kw: {})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as client:
        url = f"/api/topics/topic/analysis/insights/{record}/decisions"
        body = dict(request_id="browser-1", state="held", note="local note", expected_previous_id=None)
        with bootstrap.connect_metadata_index(read_only=True) as c:
            before = list(c.execute("SELECT sql FROM sqlite_master ORDER BY name"))
        assert client.get(url).json()["items"] == []
        with bootstrap.connect_metadata_index(read_only=True) as c:
            assert list(c.execute("SELECT sql FROM sqlite_master ORDER BY name")) == before
        response = client.post(url, json=body)
        assert response.status_code == 201
        assert response.json()["decision"]["actor_id"] == "local-internal-user"
        assert client.post(url, json=body).status_code == 200
        assert client.post(url, json={**body, "request_id": "stale"}).status_code == 409
        for invalid in [dict(state="wrong"), dict(note="a" * 2001), dict(actor_id="spoof"), dict(expected_previous_id=3)]:
            assert client.post(url, json={**body, **invalid}).status_code == 422
        insufficient = next(i for i in repo.insights("topic")["items"] if i["state"] == "INSUFFICIENT_DATA")
        other_url = f'/api/topics/topic/analysis/insights/{insufficient["insight_record_id"]}/decisions'
        assert client.post(other_url, json={**body, "state": "accepted"}).status_code == 409

        def deny(*args, **kwargs):
            raise FileNotFoundError("not owned")
        monkeypatch.setattr(topic_profiles, "get_topic_profile", deny)
        assert client.get(url).status_code == 404
        assert client.post(url, json=body).status_code == 404


def test_decision_pagination_and_database_reference_guards(record):
    first, _ = save(record)
    second, _ = save(record, "second", "held", first["decision_id"])
    page = repo.decisions("topic", record, limit=1, page=2)
    assert page["items"] == [first] and page["latest_id"] == second["decision_id"]
    assert page["total"] == 2
    with bootstrap.connect_metadata_index() as c:
        sql = """INSERT INTO temporal_insight_decisions
                 (decision_id,topic_id,insight_record_id,request_id,state,note,previous_id,actor_id,created_at,schema_version)
                 VALUES ('bad',?,?, 'bad','held','',?,'owner','clock','M3C_INSIGHT_DECISION_V1')"""
        for topic, identity, previous in [("other", record, None), ("topic", "missing", None), ("topic", record, None)]:
            with pytest.raises(sqlite3.IntegrityError, match="decision_binding_invalid"):
                c.execute(sql, (topic, identity, previous))
    assert repo.decisions("topic", record)["total"] == 2

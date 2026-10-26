from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.recommendation_versions import router
from app.auth.security import require_product_user
from app.db import bootstrap
from app.domain import recommendation_versions as domain
from app.domain.temporal_memory import Member
from app.repositories import recommendation_versions as repo, temporal_analysis, temporal_memory
from app.schemas.recommendations import ResearchGapCandidate, RecommendationEvidenceRef
from app.services import topic_dashboard, topic_profiles


@pytest.fixture
def output(tmp_path, monkeypatch):
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.connect_metadata_index().close()
    ref = RecommendationEvidenceRef(paper_id="paper", source_text_sha256="a" * 64, evidence_span_id="span",
                                   review_level="abstract_only", quoted_or_summarized_basis="Original observed limitation")
    gap = ResearchGapCandidate(gap_id="gap-evaluation", topic_id="topic", title="Evaluation gap", gap_statement="Local limitation",
                               supporting_paper_ids=["paper"], supporting_evidence_refs=[ref], limitation_refs=[ref],
                               source_basis="deterministic_review", confidence="low", confidence_score=0.25,
                               coverage_warning="Abstract only", novelty_rationale="Needs more evidence", risk_notes=["Not exhaustive"])
    payload = dict(topic_id="topic", topic_title="Local topic", recommendation_version="research-recommendation-baseline-v1",
                   gap_candidates=[gap.model_dump()], coverage_warning="1/10 reviewed")
    monkeypatch.setattr(topic_dashboard.recommendations, "build_recommendations_for_topic", lambda *a, **kw: deepcopy(payload))
    monkeypatch.setattr(topic_profiles, "get_topic_profile", lambda *a, **kw: {})
    return payload


def value():
    return topic_dashboard.build_topic_recommendation_views("topic")[1][0]


def decide(version, request="first", previous=None, state="held", note="Read more evidence"):
    return repo.decide("topic", version["version_id"], request, state, note, previous, "owner")


def test_version_binding_survives_mutable_candidate_and_generator_changes(output):
    original, _ = repo.save(value(), "owner")
    decision, _ = decide(original)
    output["gap_candidates"][0]["supporting_evidence_refs"][0]["quoted_or_summarized_basis"] = "Changed evidence under same ID"
    changed, created = repo.save(value(), "owner")
    assert created and changed["candidate_id"] == original["candidate_id"]
    assert changed["version_id"] != original["version_id"]
    assert repo.get("topic", original["version_id"]) == original
    assert repo.decisions("topic", original["version_id"])["items"] == [decision]
    assert repo.decisions("topic", changed["version_id"])["items"] == []
    output["recommendation_version"] = "future-generator"
    future, _ = repo.save(value(), "owner")
    assert future["version_id"] != changed["version_id"]
    assert repo.versions("topic", limit=1, page=3)["items"] == [original]
    assert repo.versions("other")["total"] == 0
    # Existing shared-store writers must work after the extension is installed.
    policy, _ = temporal_analysis.create_policy("topic", {})
    snapshot, _ = temporal_memory.capture("topic", lambda _: [Member("paper", "v1")])
    temporal_analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id)
    assert repo.get("topic", original["version_id"]) == original


@pytest.mark.parametrize("field", ["source_text_sha256", "evidence_span_id", "review_level", "quoted_or_summarized_basis"])
def test_each_evidence_identity_component_changes_version(output, field):
    before = domain.identity(value())
    ref = output["gap_candidates"][0]["supporting_evidence_refs"][0]
    ref[field] = "full_text_reviewed" if field == "review_level" else "different"
    assert domain.identity(value()) != before


def test_concurrent_saves_replays_and_competing_decisions(output):
    content = value()
    gate = Barrier(4)
    def capture(_):
        gate.wait()
        return repo.save(content, "owner")
    with ThreadPoolExecutor(max_workers=4) as pool:
        saved = list(pool.map(capture, range(4)))
    assert sum(created for _, created in saved) == 1
    version = saved[0][0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(lambda _: decide(version), range(8)))
    assert sum(created for _, created in repeated) == 1
    previous = repeated[0][0]["decision_id"]
    def competing(n):
        gate.wait()
        try:
            return decide(version, str(n), previous, "accepted")[1]
        except ValueError as exc:
            assert str(exc) == "decision_conflict"
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(competing, range(4))) == 1
    assert decide(version) == (repeated[0][0], False)
    assert repo.decisions("topic", version["version_id"])["total"] == 2
    with pytest.raises(ValueError, match="decision_request_conflict"):
        decide(version, note="altered retry")


def test_rollback_readonly_and_reference_guards(output, monkeypatch):
    with bootstrap.connect_metadata_index(read_only=True) as c:
        before = list(c.execute("SELECT sql FROM sqlite_master ORDER BY name"))
    assert repo.versions("topic")["items"] == []
    with bootstrap.connect_metadata_index(read_only=True) as c:
        assert list(c.execute("SELECT sql FROM sqlite_master ORDER BY name")) == before
    def fail(_):
        raise RuntimeError("injected rollback")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        repo.save(value(), "owner")
    assert repo.versions("topic")["total"] == 0
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    version, _ = repo.save(value(), "owner")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        decide(version)
    assert repo.decisions("topic", version["version_id"])["total"] == 0
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    first, _ = decide(version)
    second, _ = decide(version, "next", first["decision_id"], "rejected")
    page = repo.decisions("topic", version["version_id"], 1, 2)
    assert page["items"] == [first] and page["latest_id"] == second["decision_id"]
    for topic, identity in [("other", version["version_id"]), ("topic", "missing")]:
        with pytest.raises(KeyError):
            repo.decide(topic, identity, "attempt", "accepted", "", None, "owner")
    with bootstrap.connect_metadata_index() as c:
        for table in ["recommendation_versions", "recommendation_decisions"]:
            for sql in [f"DELETE FROM {table}", f"UPDATE {table} SET actor_id='changed'"]:
                with pytest.raises(sqlite3.IntegrityError, match="recommendation_immutable"):
                    c.execute(sql)
        for topic, identity, predecessor in [("other", version["version_id"], None), ("topic", "missing", None), ("topic", version["version_id"], None)]:
            with pytest.raises(sqlite3.IntegrityError, match="decision_binding_invalid"):
                c.execute("""INSERT INTO recommendation_decisions
                    (decision_id,topic_id,version_id,request_id,state,note,previous_id,actor_id,created_at,schema_version)
                    VALUES ('bad',?,?,'bad','held','',?,'owner','clock','M3C_RECOMMENDATION_DECISION_V1')""", (topic, identity, predecessor))


def test_api_stale_capture_replay_authorization_and_no_client_evidence(output, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_product_user] = lambda: None
    root = "/api/topics/topic/recommendation-versions"
    original = value()
    body = dict(candidate_type="gap", candidate_id="gap-evaluation", expected_version_id=domain.identity(original))
    with TestClient(app) as client:
        output["coverage_warning"] = "Now 2/10 reviewed"
        assert client.post(root, json=body).status_code == 409
        assert client.get(root).json()["total"] == 0
        body["expected_version_id"] = domain.identity(value())
        response = client.post(root, json=body)
        assert response.status_code == 201
        version = response.json()["version"]
        assert version["actor_id"] == "local-internal-user"
        output["gap_candidates"] = []
        replay = client.post(root, json=body)
        assert replay.status_code == 200 and replay.json() == dict(version=version, created=False)
        assert client.get(root + "/" + version["version_id"]).json()["version"] == version
        assert client.post(root, json={**body, "candidate_type": "new_topic"}).status_code == 409
        for extra in [dict(actor_id="spoof"), dict(candidate={"title": "fake"}), dict(expected_version_id="x")]:
            assert client.post(root, json={**body, **extra}).status_code == 422
        url = root + "/" + version["version_id"] + "/decisions"
        decision = dict(request_id="decision-1", state="accepted", note="Follow up locally")
        assert client.post(url, json=decision).status_code == 201
        assert client.post(url, json=decision).status_code == 200
        assert client.post(url, json={**decision, "request_id": "stale"}).status_code == 409
        assert client.post(url, json={**decision, "actor_id": "spoof"}).status_code == 422
        assert client.post(url, json={**decision, "note": "n" * 2001}).status_code == 422
        assert client.get(root.replace("/topic/", "/other/") + "/" + version["version_id"]).status_code == 404
        def deny(*a, **kw):
            raise FileNotFoundError("private profile")
        monkeypatch.setattr(topic_profiles, "get_topic_profile", deny)
        for endpoint in [root, root + "/" + version["version_id"], url]:
            assert client.get(endpoint).status_code == 404
        assert client.post(root, json=body).status_code == 404
        assert client.post(url, json=decision).status_code == 404


def test_current_read_contention_is_retryable_not_empty_success(output, monkeypatch):
    from app.api.dashboard import router as dashboard_router
    from app.services.recommendations import RecommendationReadOnlySafetyError
    app = FastAPI()
    app.include_router(router)
    app.include_router(dashboard_router)
    app.dependency_overrides[require_product_user] = lambda: None
    body = dict(candidate_type="gap", candidate_id="gap-evaluation", expected_version_id=domain.identity(value()))
    def blocked(*a, **kw):
        raise RecommendationReadOnlySafetyError("synthetic active writer")
    monkeypatch.setattr(topic_dashboard.recommendations, "build_recommendations_for_topic", blocked)
    with TestClient(app) as client:
        assert client.get("/api/topics/topic/recommendations").status_code == 503
        assert client.post("/api/topics/topic/recommendation-versions", json=body).status_code == 503
        assert client.get("/api/topics/topic/recommendation-versions").json()["total"] == 0

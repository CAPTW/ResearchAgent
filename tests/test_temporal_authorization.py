"""Real topic ACL coverage for M3 writes, with disposable local records only."""

from contextlib import closing
from copy import deepcopy
import json
import subprocess
import urllib.request

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import recommendation_versions, temporal_analysis, temporal_memory
from app.auth.config import clear_auth_settings_cache
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.db import bootstrap
from app.domain import recommendation_versions as recommendation_domain
from app.domain.temporal_memory import Member
from app.repositories import recommendation_versions as recommendations
from app.repositories import temporal_analysis as analysis
from app.repositories import temporal_memory as memory, temporal_sources
from app.schemas.recommendations import ResearchGapCandidate, RecommendationEvidenceRef
from app.services import topic_dashboard, topic_profiles


MUTATIONS = (
    "snapshot", "policy", "activation", "evaluation", "insight_decision",
    "recommendation", "recommendation_decision",
)


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("No external/provider/subprocess execution in authorization tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibited)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", prohibited)
    monkeypatch.setattr(urllib.request, "urlopen", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    clear_auth_settings_cache()
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    profile_path = tmp_path / "topic_profiles" / "profiles.json"
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_path.parent)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    profile_path.parent.mkdir()
    profile_path.write_text(json.dumps([
        {"profile_id": "default-xrml", "title": "Public template", "owner_user_id": None},
        {"profile_id": "private-topic", "title": "Private topic", "owner_user_id": "owner"},
    ]), encoding="utf-8")
    # Only corpus projection and recommendation generation are synthetic seams.
    # Authentication actor construction and the real stored topic ACL are intact.
    monkeypatch.setattr(temporal_sources, "project", lambda *args: [Member("latest", "v-latest")])
    generated = {}
    seeded = {}
    for topic in ("default-xrml", "private-topic"):
        policy, _ = analysis.create_policy(topic, {})
        inactive_policy, _ = analysis.create_policy(topic, {"minimum_paper_count": 2})
        first, _ = memory.capture(topic, lambda _: [Member("paper", "v1", review_state="not_reviewed")])
        second, _ = memory.capture(topic, lambda _: [Member("paper", "v1", review_state="abstract_reviewed")])
        analysis.evaluate(topic, second.snapshot_id, policy.policy_id)
        insight = next(row for row in analysis.insights(topic)["items"] if row["state"] == "ACCEPTED")
        unevaluated, _ = memory.capture(topic, lambda _: [Member("unevaluated", "v-next")])
        ref = RecommendationEvidenceRef(
            paper_id="paper", source_text_sha256="a" * 64, evidence_span_id="span",
            review_level="abstract_only", quoted_or_summarized_basis="Synthetic limitation",
        )
        gap = ResearchGapCandidate(
            gap_id="gap", topic_id=topic, title="Synthetic gap", gap_statement="Synthetic limitation",
            supporting_paper_ids=["paper"], supporting_evidence_refs=[ref], source_basis="deterministic_review",
            confidence="low", confidence_score=0.2, novelty_rationale="Not assessed",
        )
        value = recommendation_domain.content(
            topic, {"recommendation_version": "test-v1", "topic_title": topic},
            gap.model_dump(), {"type": "gap", "id": "gap"},
        )
        saved, _ = recommendations.save(value, "owner")
        new_value = deepcopy(value)
        new_value["candidate"]["title"] = "New synthetic gap version"
        generated[topic] = new_value
        seeded[topic] = dict(
            policy=policy.policy_id, activation=inactive_policy.policy_id,
            snapshot=first.snapshot_id, evaluation=unevaluated.snapshot_id,
            insight=insight["insight_record_id"], recommendation=saved["version_id"],
            new_recommendation=recommendation_domain.identity(new_value),
        )
    monkeypatch.setattr(topic_dashboard, "build_topic_recommendation_views", lambda topic: ({}, [deepcopy(generated[topic])]))
    app = FastAPI()
    for router in (temporal_analysis.router, temporal_memory.router, recommendation_versions.router):
        app.include_router(router)
    user = {"value": ProductUser("reader", "Reader", None, "oidc")}
    app.dependency_overrides[require_product_user] = lambda: user["value"]
    with TestClient(app) as client:
        yield client, user, seeded, profile_path
    clear_auth_settings_cache()


def stored_state(profile_path):
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        database = tuple(connection.iterdump())
    return database, profile_path.read_bytes()


def mutation_request(client, topic, records, operation):
    root = f"/api/topics/{topic}"
    decision = {"request_id": "decision", "state": "held", "note": "Needs human review"}
    if operation == "snapshot":
        return client.post(root + "/memory/snapshots")
    if operation == "policy":
        return client.post(root + "/analysis/policies", json={"minimum_paper_count": 3})
    if operation == "activation":
        return client.put(root + "/analysis/policies/active", json={"policy_id": records["activation"]})
    if operation == "evaluation":
        return client.post(root + "/analysis/evaluations", json={"snapshot_id": records["evaluation"], "policy_id": records["policy"]})
    if operation == "insight_decision":
        return client.post(root + f"/analysis/insights/{records['insight']}/decisions", json=decision)
    if operation == "recommendation":
        return client.post(root + "/recommendation-versions", json={
            "candidate_type": "gap", "candidate_id": "gap", "expected_version_id": records["new_recommendation"],
        })
    return client.post(root + f"/recommendation-versions/{records['recommendation']}/decisions", json=decision)


@pytest.mark.parametrize("operation", MUTATIONS)
@pytest.mark.parametrize("topic", ("default-xrml", "private-topic"))
def test_read_only_or_nonowner_actor_cannot_mutate_topic(local_api, topic, operation):
    client, _, seeded, profile_path = local_api
    before = stored_state(profile_path)
    response = mutation_request(client, topic, seeded[topic], operation)
    after = stored_state(profile_path)
    assert (response.status_code, after == before) == (404, True), response.text


@pytest.mark.parametrize("actor,topic,mode", (
    (ProductUser("admin", "Admin", None, "oidc", is_admin=True), "default-xrml", "oidc"),
    (ProductUser("owner", "Owner", None, "oidc"), "private-topic", "oidc"),
    (ProductUser("local", "Local", None, "disabled"), "default-xrml", "disabled"),
))
def test_intended_writers_can_mutate_and_replay(local_api, monkeypatch, actor, topic, mode):
    client, user, seeded, _ = local_api
    monkeypatch.setenv("AUTH_MODE", mode)
    clear_auth_settings_cache()
    user["value"] = actor
    for operation in MUTATIONS:
        response = mutation_request(client, topic, seeded[topic], operation)
        assert response.status_code in (200, 201), (operation, response.text)
        replay = mutation_request(client, topic, seeded[topic], operation)
        assert replay.status_code == 200, (operation, replay.text)


@pytest.mark.parametrize("topic,expected", (("default-xrml", 200), ("private-topic", 404)))
def test_public_read_and_private_topic_isolation(local_api, topic, expected):
    client, _, seeded, profile_path = local_api
    records = seeded[topic]
    root = f"/api/topics/{topic}"
    paths = [
        "/analysis/policies", "/analysis/series?metric_id=corpus.paper_count",
        f"/analysis/insights/{records['insight']}/decisions", "/memory/snapshots",
        f"/memory/snapshots/{records['snapshot']}",
        f"/memory/diff?from={records['snapshot']}&to={records['snapshot']}",
        "/recommendation-versions", f"/recommendation-versions/{records['recommendation']}",
        f"/recommendation-versions/{records['recommendation']}/decisions",
    ]
    before = stored_state(profile_path)
    for path in paths:
        response = client.get(root + path)
        assert response.status_code == expected, (path, response.text)
    assert stored_state(profile_path) == before


def test_admin_is_not_owner_of_private_topic(local_api):
    client, user, seeded, profile_path = local_api
    user["value"] = ProductUser("admin", "Admin", None, "oidc", is_admin=True)
    before = stored_state(profile_path)
    for operation in MUTATIONS:
        assert mutation_request(client, "private-topic", seeded["private-topic"], operation).status_code == 404
    assert stored_state(profile_path) == before


def test_lost_response_replay_still_requires_current_write_authority(local_api):
    client, user, seeded, profile_path = local_api
    user["value"] = ProductUser("former-admin", "Admin", None, "oidc", is_admin=True)
    for operation in MUTATIONS:
        assert mutation_request(client, "default-xrml", seeded["default-xrml"], operation).status_code in (200, 201)
    user["value"] = ProductUser("former-admin", "Reader", None, "oidc")
    before = stored_state(profile_path)
    for operation in MUTATIONS:
        assert mutation_request(client, "default-xrml", seeded["default-xrml"], operation).status_code == 404
    assert stored_state(profile_path) == before

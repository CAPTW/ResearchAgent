"""Actual app routes for local range preparation, without external execution."""
from contextlib import closing
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.bootstrap import connect_metadata_index
from app.main import create_app
from app.repositories import paper_reviews
from app.repositories.files import safe_filename
from app.repositories.runs import build_run_paths
from app.services import paper_bank, paper_review
from tests.test_ai_review_provider import _review
from tests import test_temporal_memory

seeded = test_temporal_memory.seeded


@pytest.fixture
def range_source(seeded):
    topic = seeded.TOPIC_A_ID
    item = paper_bank.list_topic_papers(topic, page_size=100)["items"][0]
    occurrence = item["compatibility"]["occurrences"][0]
    run, paper = occurrence["runId"], occurrence["paperId"]
    path = build_run_paths(run).raw_dir / (safe_filename(paper) + ".txt")
    text = "a" * 26000 + "e\u0301 한글🙂 later methods and results " * 300
    path.write_bytes(text.encode())
    review = _review(1, topic_id=topic, run_id=run, paper_id=paper, title=item["title"])
    review.update(paper_review.read_local_full_text_evidence(run_id=run, paper_id=paper, max_text_chars=24000))
    paper_reviews.upsert_review(review)
    return topic, item["paperVersionId"], path, text, run, paper


def client():
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    return TestClient(app)


def root(source):
    return f"/api/topics/{source[0]}/paper-bank/{source[1]}/review-preparations"


def observed(http, source, request="one"):
    response = http.get(root(source) + "/sources")
    assert response.status_code == 200, response.text
    current = next(s for s in response.json()["sources"] if s["run_id"] == source[4] and s["paper_id"] == source[5])
    assert current["source_chars"] > 24000
    return dict(request_id=request, run_id=current["run_id"], paper_id=current["paper_id"],
                expected_source_digest=current["source_digest"], start=26000, end=26010)


def test_prepare_tail_range_replay_source_loss_and_app_restart(range_source, monkeypatch):
    from app.services import ai_provider
    monkeypatch.setattr(ai_provider, "run_ai_review_provider", lambda *a, **kw: pytest.fail("preparation must not invoke provider"))
    with client() as http:
        body = observed(http, range_source)
        response = http.post(root(range_source), json=body)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["selection"]["text"] == range_source[3][26000:26010]
        assert saved["selection"]["chars"] == 10
        assert saved["state"] == "PREPARED_NOT_EXECUTED" and saved["execution"]["started"] is False
        assert saved["execution"]["full_document_review"] == "NOT_ESTABLISHED"
        listing = http.get(root(range_source)).json()
        assert listing["items"][0]["preparation_id"] == saved["preparation_id"]
        assert "text" not in listing["items"][0]["selection"]
        range_source[2].unlink()
        assert http.post(root(range_source), json=body).json() == saved
        changed = {**body, "end": body["end"] + 1}
        assert http.post(root(range_source), json=changed).status_code == 409
    with client() as http:
        assert http.get(root(range_source) + "/" + saved["preparation_id"]).json() == saved
        assert http.get(root(range_source)).json()["items"][0]["preparation_id"] == saved["preparation_id"]


def test_source_change_and_invalid_input_never_add_a_record(range_source):
    with client() as http:
        body = observed(http, range_source)
        for change in ({"start": True}, {"end": 1.5}, {"end": 26000}, {"start": -1},
                       {"end": 50000}, {"actor_id": "spoof"}, {"approval": True}):
            response = http.post(root(range_source), json={**body, **change})
            assert response.status_code == 422, response.text
        response = http.post(root(range_source), content=json.dumps({**body, "request_id": "bad\ud800"}),
                             headers={"Content-Type": "application/json"})
        assert response.status_code == 422, response.text
        for invalid in ([], "unexpected", 1):
            assert http.post(root(range_source), json=invalid).status_code == 422
        assert http.get(root(range_source) + "?page=9223372036854775808").status_code == 422
        assert http.get(root(range_source)).json()["items"] == []
        range_source[2].write_bytes(b"changed source")
        response = http.post(root(range_source), json=body)
        assert response.status_code == 409, response.text
        assert http.get(root(range_source)).json()["items"] == []


def test_topic_actor_and_public_read_are_not_preparation_access(range_source, seeded, monkeypatch):
    from app.api import paper_bank as api
    with client() as http:
        body = observed(http, range_source)
        saved = http.post(root(range_source), json=body).json()
        assert http.get(root(range_source).replace(range_source[0], seeded.TOPIC_B_ID)).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        for url in (root(range_source), root(range_source) + "/sources", root(range_source) + "/" + saved["preparation_id"]):
            assert http.get(url).status_code == 404
        assert http.post(root(range_source), json={**body, "request_id": "denied"}).status_code == 404
        from app.services import review_input_history
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: resource_actor_for_user(None))
        profiles = review_input_history.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {range_source[0]})
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.post(root(range_source), json={**body, "request_id": "public-denied"}).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
        assert http.get(root(range_source) + "/" + saved["preparation_id"]).status_code == 200
    with closing(connect_metadata_index(read_only=True)) as conn:
        assert conn.execute("SELECT count(*) FROM paper_review_preparations").fetchone()[0] == 1


def test_storage_error_is_visible_and_new_routes_require_auth(range_source, monkeypatch):
    from app.services import review_preparation
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("private failure detail")
    with client() as http:
        monkeypatch.setattr(review_preparation, "listing", broken)
        response = http.get(root(range_source))
        assert response.status_code == 503
        assert response.json()["detail"] == "review_preparation_store_unavailable"
        assert "private failure detail" not in response.text
    app = create_app()
    def unauthenticated():
        from fastapi import HTTPException
        raise HTTPException(401, detail="authentication_required")
    app.dependency_overrides[require_product_user] = unauthenticated
    with TestClient(app) as http:
        assert http.get(root(range_source)).status_code == 401

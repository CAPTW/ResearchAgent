"""Authenticated local request preparation never dispatches or grants authority."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api import local_synthesis as api
from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.main import create_app
from app.repositories import journal_executions as repo
from app.services import paper_bank, providers
from app.services.journal_dispatch import JournalDispatcher
from tests import test_journal_execution as prior
from tests.test_local_synthesis import select

seeded = prior.imports.seeded


@pytest.fixture
def workspace(seeded, monkeypatch):
    monkeypatch.setattr(providers, "get_active_provider", lambda: pytest.fail("Preparation cannot select a provider"))
    monkeypatch.setattr(JournalDispatcher, "dispatch", lambda *a, **k: pytest.fail("Preparation cannot dispatch"))
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    with TestClient(app) as client:
        root = f"/api/topics/{topic}/local-synthesis"
        job = client.post(root, json=dict(request_id="freeze", selection=select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:2]))).json()["job"]
        base = root + "/" + job["job_id"]
        body = dict(request_id="prepare", expected_input_digest=job["input_digest"], provider_name="openai", model="offline-model", max_attempts=1, max_output_tokens=1024)
        url = base + "/journal-executions"
        assert client.post(url, json=body).status_code == 409  # Only completed frozen input.
        for step in range(job["total_steps"]):
            job = client.post(base + "/step", json=dict(expected_step=step)).json()["job"]
        yield app, client, url, body, topic, job


def test_preparation_is_actor_bound_local_and_replays_current_history(workspace):
    app, client, url, body, topic, job = workspace
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    saved = response.json()["execution"]
    assert saved["contract"]["actor_id"] == "local-internal-user"
    assert saved["contract"]["specification"] == body
    assert saved["contract"]["activation"] == "UNREGISTERED_ADAPTER_CONTRACT"
    assert saved["events"] == [] and saved["current"]["attempt"] == 0 and saved["current"]["state"] == "READY"
    assert client.post(url, json=body).json() == dict(execution=saved, created=False)
    detail = url + "/" + saved["contract"]["execution_id"]
    assert client.get(detail).json() == saved
    cancelled = client.post(detail + "/control", json=dict(request_id="cancel", action="cancel", expected_sequence=0)).json()["execution"]
    assert client.post(url, json=body).json() == dict(execution=cancelled, created=False)
    assert cancelled["current"]["attempt"] == 0 and cancelled["current"]["state"] == "CANCELLED"
    assert client.get(url).json()["total"] == 1
    # A fresh application reads the exact contract and cancellation; no resume.
    other = create_app()
    other.dependency_overrides[require_product_user] = lambda: None
    with TestClient(other) as reopened:
        assert reopened.get(detail).json() == cancelled


def test_strict_input_binding_authentication_and_topic_isolation(workspace, monkeypatch):
    app, client, url, body, topic, job = workspace
    for extra in [dict(actor_id="spoof"), dict(created_at="2000"), dict(proof={}), dict(max_attempts=True), dict(max_attempts=4), dict(max_output_tokens=255), dict(model=" "), dict(provider_name="bad/path")]:
        assert client.post(url, json=body | extra).status_code == 422
    stale = client.post(url, json=body | dict(expected_input_digest="0" * 64))
    assert stale.status_code == 409 and stale.json()["detail"] == "journal_execution_input_changed"
    assert client.get(url).json()["total"] == 0
    assert client.post(url, json=body).status_code == 201
    assert client.post(url, json=body | dict(model="different-model")).status_code == 409
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
    assert client.post(url, json=body).status_code == 404
    assert client.get(url).status_code == 404
    def unauthenticated():
        raise HTTPException(401)
    app.dependency_overrides[require_product_user] = unauthenticated
    assert client.post(url, json=body).status_code == 401
    assert repo.listing(topic, job["job_id"])["total"] == 1


def test_duplicate_http_preparation_and_failed_commit_or_response_are_recoverable(workspace, monkeypatch):
    _, client, url, body, topic, job = workspace
    barrier = Barrier(6)
    def send(_):
        barrier.wait()
        return client.post(url, json=body)
    with ThreadPoolExecutor(6) as pool:
        values = list(pool.map(send, range(6)))
    assert sorted(value.status_code for value in values) == [200] * 5 + [201]
    assert all(value.json()["execution"] == values[0].json()["execution"] for value in values)
    def fail(_):
        raise ValueError("injected_before_commit")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "checkpoint", fail)
        assert client.post(url, json=body | dict(request_id="rollback")).status_code == 503
    assert client.get(url).json()["total"] == 1
    assert client.post(url, json=body | dict(request_id="rollback")).status_code == 201
    original = repo.prepare
    def lose_response(*a, **kw):
        original(*a, **kw)
        raise ValueError("injected_after_commit")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "prepare", lose_response)
        assert client.post(url, json=body | dict(request_id="lost")).status_code == 503
    replay = client.post(url, json=body | dict(request_id="lost"))
    assert replay.status_code == 200 and not replay.json()["created"]
    assert client.get(url).json()["total"] == 3
    assert all(item["attempt"] == 0 and item["state"] == "READY" for item in repo.listing(topic, job["job_id"])["items"])

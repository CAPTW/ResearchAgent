"""Saved connections prepare immutable requests without granting execution authority."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import subprocess
from threading import Barrier
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from app.api import local_synthesis as api
from app.auth.config import clear_auth_settings_cache
from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.db import bootstrap
from app.main import create_app
from app.repositories import journal_executions as repo, local_synthesis
from app.services import model_connections as connections
from app.services import model_connection_pipeline, topic_profiles
from tests import test_journal_preparation as prior
from tests import test_local_synthesis

seeded = prior.seeded
workspace = prior.workspace
source = test_local_synthesis.source
select = test_local_synthesis.select


@pytest.fixture(autouse=True)
def _quiescent_preparation_store(monkeypatch):
    # Apply before workspace startup so independent scheduler writes cannot race
    # whole-store checks for rejected or read-only preparation requests.
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")


@pytest.fixture
def selected(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "STATE_PATH", tmp_path / "connections.json")
    monkeypatch.delenv("XRML_PROVIDER", raising=False)
    def prohibited(*args, **kwargs):
        pytest.fail("Connection preparation cannot invoke adapters, credentials, subprocesses or network")
    monkeypatch.setattr(model_connection_pipeline, "bind_journal", prohibited)
    monkeypatch.setattr(model_connection_pipeline, "_key", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibited)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", prohibited)
    monkeypatch.setattr(urllib.request, "urlopen", prohibited)
    return workspace


def summary_url(url):
    return url.removesuffix("/journal-executions") + "/journal-connection"


def save_connection(connector="codex_subscription", model="selected-model"):
    state = connections.read()
    connections.save(connections.Update(expected_revision=state["revision"], default_connector_id=connector,
        models=state["models"] | {connector: model}))


def request_body(client, url, body):
    observed = client.get(summary_url(url))
    assert observed.status_code == 200, observed.text
    value = observed.json()
    return body | dict(connector_id=value["connector_id"], provider_name=value["provider_name"], model=value["model"],
        expected_selection_digest=value["selection_digest"])


def stored_state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        return tuple(connection.iterdump())


def test_codex_default_summary_is_topic_scoped_and_never_execution_authority(selected):
    _, client, url, _, _, _ = selected
    response = client.get(url.removesuffix("/journal-executions") + "/journal-connection")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["connector_id"] == "codex_subscription"
    assert value["provider_name"] == "codex_cli"
    assert value["model"] == ""
    assert value["auth_mode"] == "subscription_cli"
    assert value["environment_override"] is False
    assert value["ready"] is False
    assert value["reason"] == "model_connection_model_required"
    assert value["execution_allowed"] is False
    assert len(value["selection_digest"]) == 64
    assert set(value) == {"connector_id", "provider_name", "model", "display_name", "auth_mode", "environment_override",
                          "ready", "reason", "execution_allowed", "selection_digest"}
    assert not connections.STATE_PATH.exists()


@pytest.mark.parametrize("connector", tuple(identity for identity, connector in connections.BY_ID.items() if connector.adapter_available))
def test_saved_connection_prepares_exact_request_without_selecting_adapter_or_reading_credentials(selected, connector, monkeypatch):
    _, client, url, body, _, _ = selected
    save_connection(connector, "selected-model")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-expose-openai")
    monkeypatch.setenv("XAI_API_KEY", "xai-never-expose-xai")
    observed = client.get(summary_url(url))
    assert observed.json()["ready"] and observed.json()["reason"] is None
    assert "credential" not in observed.text and "sk-never" not in observed.text and "xai-never" not in observed.text
    assert "login" not in observed.text and "OPENAI_API_KEY" not in observed.text
    body = request_body(client, url, body)
    response = client.post(url + "/selected-connection", json=body)
    assert response.status_code == 201, response.text
    saved = response.json()["execution"]
    assert saved["contract"]["specification"] == {key: value for key, value in body.items()
        if key not in {"connector_id", "expected_selection_digest"}}
    assert saved["contract"]["actor_id"] == "local-internal-user"
    assert saved["contract"]["activation"] == "UNREGISTERED_ADAPTER_CONTRACT"
    assert saved["current"]["state"] == "READY" and saved["current"]["attempt"] == 0 and saved["events"] == []


def test_local_planning_connection_cannot_prepare_journal_execution_or_mutate_store(selected):
    _, client, url, body, _, _ = selected
    save_connection("ollama_local", "synthetic-local-model")
    before = stored_state()
    response = client.get(summary_url(url))
    assert response.status_code == 200
    value = response.json()
    assert (value["connector_id"], value["provider_name"], value["auth_mode"]) == ("ollama_local", "ollama", "local_runtime")
    assert value["ready"] is False and value["execution_allowed"] is False
    assert value["reason"] == "model_connection_adapter_unavailable"
    rejected = client.post(url + "/selected-connection", json=request_body(client, url, body))
    assert rejected.status_code == 409 and rejected.json()["detail"] == "model_connection_adapter_unavailable"
    assert client.get(url).json()["total"] == 0
    assert stored_state() == before


@pytest.mark.parametrize("override,connector,provider,ready", (
    ("codex", "codex_subscription", "codex_cli", True),
    ("openai", "openai_api", "openai", True),
    ("disabled", None, None, False),
    ("unknown-provider", None, None, False),
))
def test_effective_environment_choice_is_explicit_without_fallback(selected, monkeypatch, override, connector, provider, ready):
    _, client, url, _, _, _ = selected
    save_connection("codex_subscription")
    save_connection("openai_api")
    monkeypatch.setenv("XRML_PROVIDER", override)
    value = client.get(summary_url(url)).json()
    assert (value["connector_id"], value["provider_name"], value["ready"]) == (connector, provider, ready)
    assert value["environment_override"] is True and value["execution_allowed"] is False
    assert value["reason"] == (None if ready else "model_connection_unavailable")
    if not ready:
        assert value["model"] == "" and value["auth_mode"] is None


def test_unavailable_adapter_is_explained_and_cannot_prepare(selected, monkeypatch):
    _, client, url, body, _, _ = selected
    save_connection()
    monkeypatch.setitem(connections.BY_ID, "codex_subscription", replace(connections.BY_ID["codex_subscription"], adapter_available=False))
    value = client.get(summary_url(url)).json()
    assert value["reason"] == "model_connection_adapter_unavailable" and not value["ready"]
    result = client.post(url + "/selected-connection", json=request_body(client, url, body))
    assert result.status_code == 409 and result.json()["detail"] == "model_connection_adapter_unavailable"
    assert client.get(url).json()["total"] == 0


@pytest.mark.parametrize("change", (
    {"connector_id": "openai_api"}, {"connector_id": "unknown"}, {"provider_name": "openai"},
    {"model": "forged-model"}, {"expected_selection_digest": "0" * 64},
))
def test_forged_or_stale_selection_leaves_store_unchanged(selected, change):
    _, client, url, body, _, _ = selected
    save_connection()
    body = request_body(client, url, body)
    before = stored_state()
    result = client.post(url + "/selected-connection", json=body | change)
    assert result.status_code == 409 and result.json()["detail"] == "model_connection_selection_changed"
    assert stored_state() == before


def test_changed_selection_requires_refresh_and_frozen_request_replays_without_settings_read(selected, monkeypatch):
    app, client, url, body, topic, job = selected
    save_connection()
    original = request_body(client, url, body)
    save_connection("openai_api", "other-model")
    assert client.post(url + "/selected-connection", json=original).status_code == 409
    assert client.get(url).json()["total"] == 0
    refreshed = request_body(client, url, body)
    saved = client.post(url + "/selected-connection", json=refreshed).json()["execution"]
    detail = url + "/" + saved["contract"]["execution_id"]
    cancelled = client.post(detail + "/control", json=dict(request_id="cancel", action="cancel", expected_sequence=0)).json()["execution"]
    assert cancelled["current"]["state"] == "CANCELLED"
    connections.STATE_PATH.write_text("{corrupt settings", encoding="utf-8")
    def no_read():
        pytest.fail("A frozen request replay must precede settings reads")
    with monkeypatch.context() as patch:
        patch.setattr(connections, "read", no_read)
        assert client.post(url + "/selected-connection", json=refreshed).json() == dict(execution=cancelled, created=False)
        # The digest was only a new-record precondition, never persisted authority.
        assert client.post(url + "/selected-connection", json=refreshed | dict(expected_selection_digest="f" * 64)).json()["execution"] == cancelled
        for change in (dict(model="changed"), dict(max_attempts=2), dict(max_output_tokens=512),
                       dict(expected_input_digest="a" * 64), dict(connector_id="codex_subscription", provider_name="codex_cli")):
            response = client.post(url + "/selected-connection", json=refreshed | change)
            assert response.status_code == 409 and response.json()["detail"] == "journal_execution_request_conflict"
    reopened = create_app()
    reopened.dependency_overrides[require_product_user] = lambda: None
    with TestClient(reopened) as other:
        assert other.get(detail).json() == cancelled
        assert other.post(url + "/selected-connection", json=refreshed).json() == dict(execution=cancelled, created=False)
    assert connections.STATE_PATH.read_text(encoding="utf-8") == "{corrupt settings"
    assert repo.listing(topic, job["job_id"])["total"] == 1


def test_one_observed_snapshot_is_frozen_even_if_settings_change_before_sqlite_commit(selected, monkeypatch):
    _, client, url, body, _, _ = selected
    save_connection()
    body = request_body(client, url, body)
    read = connections.read
    calls = []
    def changed_after_read():
        snapshot = read()
        calls.append(snapshot)
        connections.save(connections.Update(expected_revision=snapshot["revision"], default_connector_id="openai_api",
            models={"openai_api": "newer-model"}))
        return snapshot
    with monkeypatch.context() as patch:
        patch.setattr(connections, "read", changed_after_read)
        response = client.post(url + "/selected-connection", json=body)
    assert response.status_code == 201, response.text
    assert len(calls) == 1
    assert response.json()["execution"]["contract"]["specification"]["model"] == "selected-model"
    assert response.json()["execution"]["contract"]["specification"]["provider_name"] == "codex_cli"
    assert read()["default_connector_id"] == "openai_api"


def test_six_concurrent_submissions_and_commit_or_response_failure_are_recoverable(selected, monkeypatch):
    _, client, url, body, topic, job = selected
    save_connection()
    body = request_body(client, url, body)
    endpoint = url + "/selected-connection"
    barrier = Barrier(6)
    def send(_):
        barrier.wait()
        return client.post(endpoint, json=body)
    with ThreadPoolExecutor(6) as pool:
        values = list(pool.map(send, range(6)))
    assert sorted(value.status_code for value in values) == [200] * 5 + [201]
    assert all(value.json()["execution"] == values[0].json()["execution"] for value in values)
    before = stored_state()
    def fail(_):
        raise ValueError("injected_before_commit")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "checkpoint", fail)
        assert client.post(endpoint, json=body | dict(request_id="rollback")).status_code == 503
    assert stored_state() == before
    assert client.post(endpoint, json=body | dict(request_id="rollback")).status_code == 201
    original = repo.prepare
    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("injected_after_commit")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "prepare", lose_response)
        assert client.post(endpoint, json=body | dict(request_id="lost")).status_code == 503
    replay = client.post(endpoint, json=body | dict(request_id="lost"))
    assert replay.status_code == 200 and replay.json()["created"] is False
    values = repo.listing(topic, job["job_id"])
    assert values["total"] == 3
    assert all(item["state"] == "READY" and item["attempt"] == 0 for item in values["items"])


@pytest.mark.parametrize("extra", (dict(actor_id="spoof"), dict(approval=True), dict(proof={}), dict(credential="secret"),
    dict(max_attempts=True), dict(max_output_tokens=255), dict(connector_id="bad/path"), dict(expected_selection_digest="short")))
def test_browser_cannot_grant_authority_or_weaken_strict_prepare(selected, extra):
    _, client, url, body, _, _ = selected
    save_connection()
    before = stored_state()
    assert client.post(url + "/selected-connection", json=request_body(client, url, body) | extra).status_code == 422
    assert stored_state() == before


def test_missing_job_cross_topic_and_corrupt_settings_fail_without_reset(selected, seeded):
    _, client, url, body, topic, job = selected
    save_connection()
    body = request_body(client, url, body)
    for bad in (url.replace(job["job_id"], "missing-job"), url.replace(topic, seeded.TOPIC_B_ID)):
        assert client.get(summary_url(bad)).status_code == 404
        assert client.post(bad + "/selected-connection", json=body).status_code == 404
    connections.STATE_PATH.write_text("{malformed", encoding="utf-8")
    before = stored_state()
    assert client.get(summary_url(url)).status_code == 503
    assert client.post(url + "/selected-connection", json=body).status_code == 503
    assert stored_state() == before and connections.STATE_PATH.read_text(encoding="utf-8") == "{malformed"


@pytest.fixture
def oidc_api(source, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.delenv("XRML_PROVIDER", raising=False)
    clear_auth_settings_cache()
    profile_path = tmp_path / "profiles.json"
    profile_path.write_text(json.dumps([
        {"profile_id": "default-xrml", "title": "Public template", "owner_user_id": None},
        {"profile_id": "private-topic", "title": "Private topic", "owner_user_id": "owner"},
    ]), encoding="utf-8")
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(connections, "STATE_PATH", tmp_path / "connections.json")
    save_connection()
    jobs = {}
    for topic in ("default-xrml", "private-topic"):
        job, _ = local_synthesis.create(topic, select(source), "fixture", ResourceActor.oidc_user("owner"))
        for step in range(job["total_steps"]):
            job, _ = local_synthesis.advance(topic, job["job_id"], step)
        jobs[topic] = job
    app = FastAPI()
    app.include_router(api.router)
    user = {"value": ProductUser("reader", "Reader", None, "oidc")}
    app.dependency_overrides[require_product_user] = lambda: user["value"]
    with TestClient(app) as client:
        yield app, client, user, jobs
    clear_auth_settings_cache()


@pytest.mark.parametrize("topic,subject,admin,allowed", (
    ("default-xrml", "reader", False, False), ("default-xrml", "admin", True, True),
    ("private-topic", "owner", False, True), ("private-topic", "outsider", False, False),
    ("private-topic", "admin", True, False),
))
def test_real_oidc_actor_and_topic_acl_protect_summary_and_preparation(oidc_api, topic, subject, admin, allowed):
    _, client, user, jobs = oidc_api
    user["value"] = ProductUser(subject, subject, None, "oidc", is_admin=admin)
    job = jobs[topic]
    url = f"/api/topics/{topic}/local-synthesis/{job['job_id']}/journal-executions"
    original = dict(request_id="prepare", expected_input_digest=job["input_digest"], provider_name="codex_cli",
        model="selected-model", max_attempts=1, max_output_tokens=1024, connector_id="codex_subscription",
        expected_selection_digest="0" * 64)
    before = stored_state()
    settings_bytes = connections.STATE_PATH.read_bytes()
    observed = client.get(summary_url(url))
    assert observed.status_code == (200 if allowed else 404), observed.text
    body = request_body(client, url, original) if allowed else original
    response = client.post(url + "/selected-connection", json=body)
    assert response.status_code == (201 if allowed else 404), response.text
    if allowed:
        assert response.json()["execution"]["contract"]["actor_id"] == subject
        assert client.post(url + "/selected-connection", json=body).status_code == 200
    else:
        assert stored_state() == before
    assert connections.STATE_PATH.read_bytes() == settings_bytes


def test_replay_rechecks_current_permission_and_original_actor(oidc_api):
    _, client, user, jobs = oidc_api
    job = jobs["default-xrml"]
    url = f"/api/topics/default-xrml/local-synthesis/{job['job_id']}/journal-executions"
    user["value"] = ProductUser("admin", "Admin", None, "oidc", is_admin=True)
    body = request_body(client, url, dict(request_id="prepare", expected_input_digest=job["input_digest"],
        max_attempts=1, max_output_tokens=1024))
    assert client.post(url + "/selected-connection", json=body).status_code == 201
    before = stored_state()
    user["value"] = ProductUser("admin", "Former admin", None, "oidc")
    assert client.get(summary_url(url)).status_code == 404
    assert client.post(url + "/selected-connection", json=body).status_code == 404
    user["value"] = ProductUser("other-admin", "Other admin", None, "oidc", is_admin=True)
    response = client.post(url + "/selected-connection", json=body)
    assert response.status_code == 409 and response.json()["detail"] == "journal_execution_request_conflict"
    assert stored_state() == before


def test_missing_authentication_cannot_read_or_prepare(selected):
    app, client, url, body, _, _ = selected
    save_connection()
    body = request_body(client, url, body)
    before = stored_state()
    def unauthenticated():
        raise HTTPException(401)
    app.dependency_overrides[require_product_user] = unauthenticated
    assert client.get(summary_url(url)).status_code == 401
    assert client.post(url + "/selected-connection", json=body).status_code == 401
    assert stored_state() == before

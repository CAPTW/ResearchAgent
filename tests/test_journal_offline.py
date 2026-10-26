"""Isolated app integration: synthetic output is never live research evidence."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
import time

import pytest
import httpx
from fastapi.testclient import TestClient

from app.auth.security import require_product_user
from app.db import bootstrap
from app.domain.temporal_memory import digest
from app.main import create_app
from app.repositories import journal_executions
from app.services import journal_offline
from app.services import paper_bank
from tests import test_journal_execution as prior
from tests.test_local_synthesis import select
from tests.test_journal_reviews import body as review_body

seeded = prior.imports.seeded
PROVIDER = "xrml_offline_synthetic"
MODEL = "synthetic-journal-v1"


@pytest.fixture
def workspace(seeded, monkeypatch, request):
    monkeypatch.setenv("XRML_ENVIRONMENT", "development")
    monkeypatch.setenv("XRML_JOURNAL_OFFLINE_ENABLED", "1" if getattr(request, "param", True) else "0")
    monkeypatch.setenv("XRML_JOURNAL_OFFLINE_DATA_ROOT", str(bootstrap.STORAGE_ROOT.parent.resolve()))
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as client:
        topic = seeded.TOPIC_A_ID
        root = f"/api/topics/{topic}/local-synthesis"
        response = client.post(root, json=dict(request_id="freeze", selection=select(
            paper_bank.list_topic_papers(topic, page_size=100)["items"][:2])))
        assert response.status_code == 201, response.text
        job = response.json()["job"]
        base = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            response = client.post(base + "/step", json=dict(expected_step=step))
            assert response.status_code == 200, response.text
            job = response.json()["job"]
        body = dict(request_id="prepare", expected_input_digest=job["input_digest"],
                    provider_name=PROVIDER, model=MODEL, max_attempts=2, max_output_tokens=1024)
        response = client.post(base + "/journal-executions", json=body)
        assert response.status_code == 201, response.text
        execution = response.json()["execution"]
        url = base + "/journal-executions/" + execution["contract"]["execution_id"]
        yield app, client, base, url, execution, body


def wait_terminal(client, url):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = client.get(url).json()
        if value["current"]["state"] not in {"READY", "LEASED", "RUNNING"}:
            return value
        time.sleep(0.02)
    pytest.fail("Synthetic worker did not complete within 10 seconds")


def database_state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        return "\n".join(connection.iterdump())


def approve(client, url, request="approve", sequence=0):
    response = client.post(url + "/offline-approvals", json=dict(request_id=request, expected_sequence=sequence))
    assert response.status_code in (200, 201), response.text
    return response.json()["authority"]


def dispatch_body(authority, request="dispatch", sequence=0, unknown=False):
    return dict(request_id=request, expected_sequence=sequence,
                authorization_id=authority["record"]["grant"]["authorization_id"], acknowledge_unknown=unknown)


def test_approval_is_separate_and_app_worker_result_is_unreviewed(workspace):
    _, client, base, url, saved, _ = workspace
    capability = client.get(base + "/journal-offline")
    assert capability.status_code == 200, capability.text
    assert capability.json()["enabled"] is True
    assert capability.json()["provider_name"] == PROVIDER
    approval_body = dict(request_id="approve", expected_sequence=0)
    approved = client.post(url + "/offline-approvals", json=approval_body)
    assert approved.status_code == 201, approved.text
    authority = approved.json()["authority"]
    assert authority["status"] == "AVAILABLE"
    assert client.get(url).json() == saved  # Approval did not reserve/start an attempt.
    assert client.post(url + "/offline-approvals", json=approval_body).json() == dict(
        authority=authority, created=False)
    assert client.get(url + "/offline").json()["authorities"] == [authority]
    dispatch = dict(request_id="dispatch", expected_sequence=0,
                    authorization_id=authority["record"]["grant"]["authorization_id"], acknowledge_unknown=False)
    response = client.post(url + "/offline-dispatch", json=dispatch)
    assert response.status_code == 200, response.text
    finished = wait_terminal(client, url)
    assert finished["current"]["state"] == "SUCCEEDED"
    assert [row["state"] for row in finished["events"]] == ["LEASED", "RUNNING", "SUCCEEDED"]
    assert finished["current"]["attempt"] == 1
    assert client.post(url + "/offline-dispatch", json=dispatch).json()["execution"] == finished
    assert client.get(base + "/journal-candidates").json()["total"] == 0
    result = finished["current"]["result"]
    assert result["provider_reported"]["provider_name"] == PROVIDER
    assert result["provider_reported"]["usage"] == {}  # No invented usage/cost.
    response = client.post(url + "/candidate", json=dict(expected_result_digest=digest(result)))
    assert response.status_code == 201, response.text
    candidate = response.json()["candidate"]
    candidate_url = base + "/journal-candidates/" + candidate["candidate_id"]
    assert client.get(candidate_url + "/reviews").json()["latest"] is None
    review = review_body(candidate)
    review.entries[0].assessment = "not_assessed"
    assert client.post(candidate_url + "/reviews", json=review.model_dump()).status_code == 422
    review.entries[0].assessment = "limited"
    review.entries[0].note = "Synthetic connection test only; no scientific assessment."
    response = client.post(candidate_url + "/reviews", json=review.model_dump())
    assert response.status_code == 201, response.text
    accepted = response.json()["review"]
    export_url = candidate_url + "/reviews/" + accepted["review_id"] + "/export?format=json"
    exported = client.get(export_url).json()
    bundle = json.loads(exported["content"])
    assert bundle["candidate"] == candidate and bundle["review"] == accepted
    assert "오프라인 합성" in exported["content"]
    # Rebuild the app/runtime from the same disposable durable records. No work
    # is scheduled on startup; a replay finds the one existing result.
    restarted = create_app()
    restarted.dependency_overrides[require_product_user] = lambda: None
    with TestClient(restarted) as reopened:
        assert reopened.get(url).json() == finished
        assert reopened.get(export_url).json() == exported
        assert reopened.get(url + "/offline").json()["authorities"] == [authority]
        assert reopened.post(url + "/offline-dispatch", json=dispatch).json()["execution"] == finished
        assert not restarted.state.journal_offline_runtime._dispatchers[dispatch["authorization_id"]].snapshot()["workers"]


@pytest.mark.parametrize("workspace", [False], indirect=True)
def test_default_disabled_does_not_create_authority_or_execution_attempt(workspace):
    _, client, base, url, saved, _ = workspace
    capability = client.get(base + "/journal-offline")
    assert capability.status_code == 200, capability.text
    assert capability.json()["enabled"] is False
    before = database_state()
    assert client.post(url + "/offline-approvals", json=dict(request_id="denied", expected_sequence=0)).status_code == 409
    assert client.post(url + "/offline-dispatch", json=dict(request_id="denied", expected_sequence=0,
        authorization_id="made-up", acknowledge_unknown=False)).status_code == 409
    assert client.get(url).json() == saved
    assert database_state() == before


@pytest.mark.parametrize("name,value", [
    ("XRML_ENVIRONMENT", "production"), ("XRML_SHARED_DEPLOYMENT", "1"),
    ("TOPIC_SCHEDULER_ENABLED", "1"), ("XRML_RUN_EXECUTOR_MODE", "embedded"),
    ("APP_BASE_URL", "https://research.example.org"), ("XRML_JOURNAL_OFFLINE_DATA_ROOT", "relative"),
    ("XRML_DATA_DIR", "different-relative-root"),
])
def test_invalid_enable_configuration_fails_closed(workspace, monkeypatch, name, value):
    before = database_state()
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="journal_offline_configuration_invalid"):
        create_app()
    assert database_state() == before


def test_browser_cannot_supply_issuer_proof_target_or_other_request_authority(workspace, monkeypatch):
    from app.auth.resource_access import ResourceActor
    from app.api import local_synthesis as api
    app, client, base, url, saved, prepared = workspace
    before = database_state()
    for spoof in ({"approved": True}, {"proof": {}}, {"actor_id": "admin"}, {"issuer": "trusted"},
                  {"expires_at": "2099-01-01"}, {"expected_sequence": True}, {"request_id": " "}):
        response = client.post(url + "/offline-approvals", json=dict(request_id="spoof", expected_sequence=0) | spoof)
        assert response.status_code == 422, response.text
    assert database_state() == before
    authority = approve(client, url)
    before = database_state()
    body = dispatch_body(authority)
    for spoof in ({"provider_name": "openai"}, {"model": "live-model"}, {"proof": {}}, {"approved": True}):
        assert client.post(url + "/offline-dispatch", json=body | spoof).status_code == 422
    # Even a caller passing topic authorization must be the original actor.
    with monkeypatch.context() as patch:
        patch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor("different-local-actor", "disabled", "local", True))
        assert client.post(url + "/offline-approvals", json=dict(request_id="other", expected_sequence=0)).status_code == 409
        assert client.post(url + "/offline-dispatch", json=body).status_code == 409
    with monkeypatch.context() as patch:
        patch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(url + "/offline").status_code == 404
        assert client.post(url + "/offline-dispatch", json=body).status_code == 404
    assert database_state() == before
    # A different valid contract cannot borrow this authority, including a live target.
    for provider, model in ((PROVIDER, MODEL), ("openai", "owner-not-approved-model")):
        response = client.post(base + "/journal-executions", json=prepared | dict(request_id=provider + "-other", provider_name=provider, model=model))
        other = base + "/journal-executions/" + response.json()["execution"]["contract"]["execution_id"]
        before = database_state()
        denied = client.post(other + "/offline-dispatch", json=body)
        assert denied.status_code in (404, 409), denied.text
        if provider == "openai":
            assert client.post(other + "/offline-approvals", json=dict(request_id="live", expected_sequence=0)).status_code == 409
        assert database_state() == before
    assert client.get(url).json() == saved


@pytest.mark.parametrize("kind", ["expired", "revoked", "cancelled"])
def test_noncurrent_authority_or_cancelled_execution_cannot_start_and_replay_never_renews(workspace, kind):
    app, client, _, url, _, _ = workspace
    runtime = app.state.journal_offline_runtime
    authority = approve(client, url)
    if kind == "expired":
        runtime._clock = lambda: datetime.fromisoformat(authority["record"]["grant"]["expires_at"]) + timedelta(seconds=1)
    elif kind == "revoked":
        response = client.post(url + "/authority/revoke", json=dict(request_id="withdraw", reason="Offline approval withdrawn",
            authorization_id=authority["record"]["grant"]["authorization_id"], expected_record_digest=authority["record_digest"]))
        assert response.status_code == 200, response.text
    else:
        assert client.post(url + "/control", json=dict(request_id="cancel", action="cancel", expected_sequence=0)).status_code == 200
    before = database_state()
    response = client.post(url + "/offline-dispatch", json=dispatch_body(authority))
    assert response.status_code == 409, response.text
    replayed = approve(client, url)
    assert replayed["record"] == authority["record"]
    assert replayed["status"] == {"expired": "EXPIRED", "revoked": "REVOKED", "cancelled": "AVAILABLE"}[kind]
    assert client.get(url).json()["current"]["attempt"] == 0
    assert database_state() == before


def test_concurrent_app_requests_and_lost_response_replay_reserve_once(workspace):
    app, client, _, url, _, _ = workspace
    # Two independent runtime locks model two app processes sharing the store.
    scope = tuple(url.split("/")[index] for index in (3, 5, 7))
    barrier = Barrier(4)
    runtimes = [journal_offline.OfflineJournalRuntime() for _ in range(2)]
    try:
        def approve_concurrently(index):
            barrier.wait()
            return runtimes[index % 2].approve(*scope, journal_offline.Approve(request_id="same", expected_sequence=0), "local-internal-user")
        with ThreadPoolExecutor(4) as pool:
            responses = list(pool.map(approve_concurrently, range(4)))
        assert sum(row["created"] for row in responses) == 1
        assert all(row["authority"] == responses[0]["authority"] for row in responses)
        authority = responses[0]["authority"]
        barrier = Barrier(4)
        calls = []
        original = app.state.journal_offline_runtime._provider.generate_json
        app.state.journal_offline_runtime._provider.generate_json = lambda request: (calls.append(request), original(request))[1]
        body = dispatch_body(authority)
        def dispatch_concurrently(_):
            barrier.wait()
            return client.post(url + "/offline-dispatch", json=body)
        with ThreadPoolExecutor(4) as pool:
            responses = list(pool.map(dispatch_concurrently, range(4)))
        assert all(row.status_code == 200 for row in responses), [row.text for row in responses]
        finished = wait_terminal(client, url)
        assert finished["current"]["state"] == "SUCCEEDED" and finished["current"]["attempt"] == 1
        assert len(calls) == 1
        before = database_state()
        assert client.post(url + "/offline-dispatch", json=body | dict(expected_sequence=1)).status_code == 409
        assert database_state() == before
    finally:
        for runtime in runtimes:
            runtime.stop()


def test_competing_approval_ids_cannot_share_one_dispatch_request_identity(workspace, monkeypatch):
    app, client, _, url, _, _ = workspace
    grants = [approve(client, url, name) for name in ("first", "second")]
    barrier = Barrier(2)
    original = journal_offline._check_record
    def simultaneous_read(record, contract, actor):
        original(record, contract, actor)
        barrier.wait(timeout=5)  # Both observe the same undispatched state.
    monkeypatch.setattr(journal_offline, "_check_record", simultaneous_read)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda grant: client.post(url + "/offline-dispatch", json=dispatch_body(grant)), grants))
    assert sorted(row.status_code for row in responses) == [200, 409], [row.text for row in responses]
    finished = wait_terminal(client, url)
    assert finished["current"]["attempt"] == 1
    assert sum(event["state"] == "LEASED" for event in finished["events"]) == 1


def test_approval_rollback_and_response_loss_preserve_one_record(workspace, monkeypatch):
    from app.repositories import journal_authority
    _, client, _, url, _, _ = workspace
    before = database_state()
    def fail(_):
        raise OSError("synthetic storage failure")
    with monkeypatch.context() as patch:
        patch.setattr(journal_authority, "checkpoint", fail)
        response = client.post(url + "/offline-approvals", json=dict(request_id="retry", expected_sequence=0))
        assert response.status_code == 503
    assert database_state() == before
    with monkeypatch.context() as patch:
        patch.setattr(journal_offline, "_describe", lambda *_: fail(None))
        response = client.post(url + "/offline-approvals", json=dict(request_id="retry", expected_sequence=0))
        assert response.status_code == 503  # Commit succeeded; response could not be assembled.
    listed = client.get(url + "/offline").json()
    assert listed["authority_total"] == 1
    response = client.post(url + "/offline-approvals", json=dict(request_id="retry", expected_sequence=0))
    assert response.status_code == 200 and response.json()["created"] is False
    assert response.json()["authority"] == listed["authorities"][0]


@pytest.mark.parametrize("field", ["actor", "reason"])
def test_approval_and_withdrawal_preserve_combining_unicode(workspace, monkeypatch, field):
    from app.api import local_synthesis as api
    from app.auth.resource_access import ResourceActor
    _, client, base, url, _, prepared = workspace
    exact = "e\u0301 · \u1100\u1161 · \u212b"
    actor = "local-internal-user"
    if field == "actor":
        actor = "owner-" + exact
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor(actor, "disabled", "local", True))
        response = client.post(base + "/journal-executions", json=prepared | dict(request_id="unicode"))
        assert response.status_code == 201, response.text
        url = base + "/journal-executions/" + response.json()["execution"]["contract"]["execution_id"]
    authority = approve(client, url)
    assert client.get(url + "/offline").json()["authorities"][0]["record"]["grant"]["actor_id"] == actor
    if field == "actor":
        response = client.post(url + "/offline-dispatch", json=dispatch_body(authority))
        assert response.status_code == 200, response.text
        assert wait_terminal(client, url)["current"]["state"] == "SUCCEEDED"
    body = dict(request_id="withdraw-" + exact, reason="Synthetic test: " + exact,
                authorization_id=authority["record"]["grant"]["authorization_id"], expected_record_digest=authority["record_digest"])
    response = client.post(url + "/authority/revoke", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()["revocation"]
    assert saved["actor_id"] == actor and saved["reason"] == body["reason"] and saved["request_id"] == body["request_id"]
    assert client.post(url + "/authority/revoke", json=body).json() == response.json() | {"applied": False}
    assert client.get(url + "/offline").json()["authorities"][0]["revocation"] == saved


def test_worker_start_failure_is_durable_and_same_request_does_not_start_again(workspace, monkeypatch):
    from app.services.journal_worker import JournalAttemptWorker
    _, client, _, url, _, _ = workspace
    authority = approve(client, url)
    calls = []
    def fail(_):
        calls.append(True)
        raise RuntimeError("journal_worker_start_failed")
    monkeypatch.setattr(JournalAttemptWorker, "start", fail)
    body = dispatch_body(authority)
    with TestClient(client.app, raise_server_exceptions=False) as observed:
        response = observed.post(url + "/offline-dispatch", json=body)
        assert response.status_code == 503, response.text
        failed = observed.get(url).json()
        assert failed["current"]["state"] == "FAILED"
        assert failed["current"]["reason"] == "worker_failed_before_dispatch"
        assert not failed["current"]["outcome_unknown"]
        assert observed.post(url + "/offline-dispatch", json=body).json()["execution"] == failed
        assert len(calls) == 1


def test_unknown_worker_failure_requires_explicit_new_attempt_and_budget_is_not_reset(workspace):
    app, client, _, url, _, _ = workspace
    calls = []
    def broken(request):
        calls.append(request)
        raise TimeoutError("synthetic failure after attempt start")
    runtime = app.state.journal_offline_runtime
    runtime._provider.generate_json = broken
    authority = approve(client, url)
    body = dispatch_body(authority)
    assert client.post(url + "/offline-dispatch", json=body).status_code == 200
    failed = wait_terminal(client, url)
    assert failed["current"]["state"] == "INTERRUPTED" and failed["current"]["outcome_unknown"]
    before = database_state()
    assert client.post(url + "/offline-dispatch", json=body).json()["execution"] == failed
    retry = dispatch_body(authority, "retry", failed["current"]["sequence"])
    response = client.post(url + "/offline-dispatch", json=retry)
    assert response.status_code == 409 and response.json()["detail"] == "journal_execution_unknown_outcome"
    assert len(calls) == 1 and database_state() == before
    assert client.post(url + "/offline-dispatch", json=retry | dict(acknowledge_unknown=True)).status_code == 200
    failed = wait_terminal(client, url)
    assert failed["current"]["attempt"] == 2 and len(calls) == 2
    fresh = approve(client, url, "fresh", failed["current"]["sequence"])
    before = database_state()
    response = client.post(url + "/offline-dispatch", json=dispatch_body(fresh, "exhausted", failed["current"]["sequence"], True))
    assert response.status_code == 409 and response.json()["detail"] == "journal_dispatch_budget_exhausted"
    assert database_state() == before and len(calls) == 2


@pytest.mark.parametrize("action", ["cancel", "revoke", "recover", "shutdown"])
def test_inflight_control_fences_late_synthetic_result_without_retry(workspace, monkeypatch, action):
    app, client, _, url, _, _ = workspace
    runtime = app.state.journal_offline_runtime
    started, release = Event(), Event()
    original = runtime._provider.generate_json
    calls = []
    def delayed(request):
        calls.append(request)
        started.set()
        assert release.wait(10), "Test must release the owned synthetic worker"
        return original(request)
    runtime._provider.generate_json = delayed
    authority = approve(client, url)
    try:
        assert client.post(url + "/offline-dispatch", json=dispatch_body(authority)).status_code == 200
        assert started.wait(5)
        running = client.get(url).json()
        if action == "revoke":
            response = client.post(url + "/authority/revoke", json=dict(request_id="withdraw", reason="Stop test result acceptance: e\u0301 · \u1100\u1161",
                authorization_id=authority["record"]["grant"]["authorization_id"], expected_record_digest=authority["record_digest"]))
            assert response.status_code == 200, response.text
        elif action == "shutdown":
            assert runtime.stop() is False  # Tracked, still draining until test releases it.
        else:
            if action == "recover":
                now = datetime.fromisoformat(running["current"]["deadline"]) + timedelta(seconds=1)
                original_time = journal_executions._time
                monkeypatch.setattr(journal_executions, "_time", lambda value=None: original_time(value or now))
                runtime._clock = lambda: now
            response = client.post(url + "/control", json=dict(request_id="control", action=action, expected_sequence=running["current"]["sequence"]))
            assert response.status_code == 200, response.text
        fenced = client.get(url).json()
        assert fenced["current"]["state"] == ("CANCELLED" if action == "cancel" else "INTERRUPTED")
        release.set()
        assert runtime.stop()
        assert client.get(url).json() == fenced and len(calls) == 1
        assert fenced["current"].get("result") is None
    finally:
        release.set()
        runtime.stop()


def test_real_desktop_sidecar_process_runs_offline_and_restarts_exact_history(workspace, tmp_path):
    _, _, _, url, prepared, _ = workspace
    dist = tmp_path / "source-sidecar-web"
    dist.mkdir()
    (dist / "index.html").write_text("<html><title>Sidecar fixture</title></html>", encoding="utf-8")
    data = bootstrap.STORAGE_ROOT.parent.resolve()
    env = dict(os.environ, XRML_DATA_DIR=str(data), XRML_JOURNAL_OFFLINE_DATA_ROOT=str(data),
               XRML_JOURNAL_OFFLINE_ENABLED="1", XRML_ENVIRONMENT="development", XRML_SHARED_DEPLOYMENT="0",
               XRML_RUN_EXECUTOR_MODE="disabled", TOPIC_SCHEDULER_ENABLED="0")
    finished = None
    authority = None
    for launch in range(2):
        with (tmp_path / f"offline-sidecar-{launch}.stderr.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "app.desktop.sidecar", "--web-dist", str(dist),
                "--data-dir", str(data), "--startup-timeout-seconds", "20"],
                cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8")
            try:
                assert process.stdout is not None and process.stdin is not None
                ready = json.loads(process.stdout.readline())
                assert ready["event"] == "ready", ready
                with httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}", timeout=5, trust_env=False) as client:
                    if launch == 0:
                        assert client.get(url).json() == prepared
                        authority = approve(client, url, "sidecar-approve")
                        response = client.post(url + "/offline-dispatch", json=dispatch_body(authority, "sidecar-dispatch"))
                        assert response.status_code == 200, response.text
                        finished = wait_terminal(client, url)
                        assert finished["current"]["state"] == "SUCCEEDED"
                    else:
                        assert client.get(url).json() == finished
                        assert client.get(url + "/offline").json()["authorities"] == [authority]
                        assert client.post(url + "/offline-dispatch", json=dispatch_body(authority, "sidecar-dispatch")).json()["execution"] == finished
                process.stdin.write("shutdown\n")
                process.stdin.flush()
                assert process.wait(timeout=15) == 0
                events = [json.loads(line) for line in process.stdout.read().splitlines() if line.strip()]
                assert any(row.get("event") == "shutdown" and row.get("reason") == "host_requested" for row in events)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

"""App dispatch uses stored server authority; fixtures never invoke Codex."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
import time

from fastapi.testclient import TestClient
import pytest

from app.auth.security import require_product_user
from app.domain.temporal_memory import digest
from app.main import create_app
from app.repositories import journal_authority as authority
from app.services import journal_codex_dispatch as service, paper_bank
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
from tests import test_journal_preparation as prior, test_codex_journal as cli
from tests.test_journal_codex_preview import stored_state
from tests.test_local_synthesis import select

seeded = prior.seeded
workspace = prior.workspace


def test_default_app_cannot_dispatch_or_create_authority(workspace):
    _, client, url, body, _, _ = workspace
    saved = client.post(url, json=body | dict(provider_name="codex_cli")).json()["execution"]
    endpoint = url + "/" + saved["contract"]["execution_id"]
    from tests.test_journal_codex_preview import stored_state
    before = stored_state()
    response = client.get(endpoint + "/codex-dispatch")
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert response.json()["reason"] == "journal_codex_dispatch_disabled"
    response = client.post(endpoint + "/codex-dispatch", json=dict(request_id="attempt", expected_sequence=0,
        authorization_id="invented", expected_authority_digest="a" * 64, expected_binding_digest="b" * 64))
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "journal_codex_dispatch_disabled"
    assert client.get(endpoint).json() == saved
    assert stored_state() == before


@pytest.fixture
def app_case(seeded, tmp_path, monkeypatch, request):
    mode = getattr(request, "param", "success")
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("XRML_JOURNAL_OFFLINE_ENABLED", "0")
    now = datetime.now(timezone.utc)
    local_runtime = cli.runtime_at(tmp_path / "fake", mode)
    runtime = service.CodexJournalRuntime(runtime=local_runtime,
        trusted_issuers=frozenset({("fixture-only-issuer", "synthetic-policy")}),
        reasoning_effort="low", timeout_seconds=30, clock=lambda: now)
    app = create_app(journal_codex_runtime=runtime)
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as client:
        topic = seeded.TOPIC_A_ID
        root = f"/api/topics/{topic}/local-synthesis"
        response = client.post(root, json=dict(request_id="freeze", selection=select(paper_bank.list_topic_papers(topic)["items"][:1])))
        assert response.status_code == 201, response.text
        job = response.json()["job"]
        base = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            assert client.post(base + "/step", json=dict(expected_step=step)).status_code == 200
        response = client.post(base + "/journal-executions", json=dict(request_id="prepare", expected_input_digest=job["input_digest"],
            provider_name="codex_cli", model="offline-requested-model", max_attempts=2, max_output_tokens=1024))
        assert response.status_code == 201, response.text
        saved = response.json()["execution"]
        scope = cli.prior.scope(saved)
        url = base + "/journal-executions/" + scope[2]
        provider = CodexJournalV2Provider(saved["contract"], runtime=local_runtime, reasoning_effort="low", timeout_seconds=30)
        grant = cli.approval(saved, provider)
        grant = grant.model_copy(update=dict(actor_id=saved["contract"]["actor_id"], granted_at=now, expires_at=now + timedelta(minutes=2),
            proof=replace(grant.proof, accepted_at=now, expires_at=now + timedelta(minutes=5))))
        record, _ = authority.register(*scope, grant, provider.binding, issuer="fixture-only-issuer",
            decision_ref="fake-python-executable-only", expected_sequence=0, now=now)
        body = dict(request_id="dispatch", expected_sequence=0, acknowledge_unknown=False,
            authorization_id=grant.authorization_id, expected_authority_digest=digest(record),
            expected_binding_digest=provider.binding.command_shape_sha256)
        yield dict(app=app, client=client, url=url, endpoint=url + "/codex-dispatch", saved=saved, runtime=runtime,
                   record=record, body=body, root=tmp_path / "fake", provider=provider, now=now, scope=scope)


def test_exact_app_dispatch_and_disabled_restart_replay_preserve_result(app_case):
    c = app_case
    before = stored_state()
    response = c["client"].get(c["endpoint"])
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["enabled"] and status["profile"] == "CODEX_JOURNAL_CLI_V2"
    assert status["manifest"] == c["provider"].manifest()
    assert status["binding"] == c["provider"].binding.model_dump()
    assert status["authorities"][0]["status"] == "AVAILABLE"
    assert stored_state() == before and not (c["root"] / "launches.log").exists()
    assert str(c["root"]) not in response.text
    response = c["client"].post(c["endpoint"], json=c["body"])
    assert response.status_code == 200, response.text
    finished = cli.wait_terminal(c["saved"])
    assert finished["current"]["state"] == "SUCCEEDED"
    assert finished["events"][0]["adapter_manifest"] == status["manifest"]
    assert finished["events"][0]["dispatch"]["authority"]["record_digest"] == digest(c["record"])
    assert c["client"].post(c["endpoint"], json=c["body"]).json()["execution"] == finished
    restarted = create_app()
    restarted.dependency_overrides[require_product_user] = lambda: None
    with TestClient(restarted) as client:
        assert client.get(c["endpoint"]).json()["enabled"] is False
        assert client.post(c["endpoint"], json=c["body"]).json()["execution"] == finished
        assert client.post(c["endpoint"], json=c["body"] | dict(request_id="new", expected_sequence=3)).status_code == 409
    assert (c["root"] / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("field,value", [("expected_binding_digest", "a" * 64), ("expected_authority_digest", "b" * 64),
    ("authorization_id", "forged"), ("expected_sequence", 1)])
def test_mismatched_request_cannot_reserve_and_cannot_replay(app_case, field, value):
    c = app_case
    before = stored_state()
    response = c["client"].post(c["endpoint"], json=c["body"] | {field: value})
    assert response.status_code in {404, 409}, response.text
    assert stored_state() == before and not (c["root"] / "launches.log").exists()
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 200
    cli.wait_terminal(c["saved"])
    assert c["client"].post(c["endpoint"], json=c["body"] | {field: value}).status_code == 409
    assert (c["root"] / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("extra", [dict(approved=True), dict(proof={}), dict(actor_id="owner"), dict(issuer="fixture-only-issuer"),
    dict(expected_sequence=True), dict(acknowledge_unknown="true"), dict(expected_binding_digest="invalid")])
def test_browser_fields_cannot_issue_or_change_authority(app_case, extra):
    c = app_case
    before = stored_state()
    assert c["client"].post(c["endpoint"], json=c["body"] | extra).status_code == 422
    assert stored_state() == before and not (c["root"] / "launches.log").exists()


@pytest.mark.parametrize("change", ["expired", "revoked", "issuer", "policy", "actor", "runtime"])
def test_authority_and_runtime_change_deny_before_attempt(app_case, change):
    c = app_case
    if change == "expired":
        c["runtime"]._clock = lambda: c["now"] + timedelta(minutes=3)
    elif change == "revoked":
        authority.revoke(*c["scope"], c["body"]["authorization_id"], request_id="revoke", issuer="owner", reason="fixture", now=c["now"])
    elif change in {"issuer", "policy"}:
        c["runtime"]._trusted = frozenset({("wrong-issuer" if change == "issuer" else "fixture-only-issuer",
                                         "wrong-policy" if change == "policy" else "synthetic-policy")})
    elif change == "actor":
        from app.api import local_synthesis
        from app.auth.resource_access import ResourceActor
        # OIDC administration does not grant another owner's workspace access.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(local_synthesis, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("other-admin", is_admin=True))
            before = stored_state()
            assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 404
            assert stored_state() == before
        with pytest.raises(ValueError, match="actor_mismatch"):
            service.dispatch(c["runtime"], *c["scope"], service.Request(**c["body"]), "other-admin")
        assert stored_state() == before
        return
    else:
        (c["root"] / "fake_cli.py").write_text("raise SystemExit('must not execute')", encoding="utf-8")
    before = stored_state()
    response = c["client"].post(c["endpoint"], json=c["body"])
    assert response.status_code == 409, response.text
    assert stored_state() == before and not (c["root"] / "launches.log").exists()
    status = c["client"].get(c["endpoint"])
    if change != "runtime":
        assert status.json()["authorities"][0]["status"] == {"expired": "EXPIRED", "revoked": "REVOKED"}.get(change, "MISMATCH")


def test_concurrent_api_duplicate_has_one_claim_and_one_process(app_case):
    c = app_case
    barrier = Barrier(6)
    def send(_):
        barrier.wait()
        response = c["client"].post(c["endpoint"], json=c["body"])
        assert response.status_code == 200, response.text
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(send, range(6)))
    finished = cli.wait_terminal(c["saved"])
    assert finished["current"]["attempt"] == 1 and finished["current"]["state"] == "SUCCEEDED"
    assert (c["root"] / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("app_case", ["held"], indirect=True)
@pytest.mark.parametrize("action", ["cancel", "revoke", "shutdown"])
def test_app_controls_fence_late_result_without_automatic_retry(app_case, action):
    c = app_case
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 200
    deadline = time.monotonic() + 5
    while not (c["root"] / "entered").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (c["root"] / "entered").exists()
    if action == "cancel":
        response = c["client"].post(c["url"] + "/control", json=dict(request_id="cancel", action="cancel", expected_sequence=2))
        assert response.status_code == 200, response.text
    elif action == "revoke":
        authority.revoke(*c["scope"], c["body"]["authorization_id"], request_id="revoke", issuer="owner", reason="fixture", now=c["now"])
    else:
        c["runtime"].stop()
    (c["root"] / "release").touch()
    finished = cli.wait_terminal(c["saved"])
    assert finished["current"]["state"] in {"CANCELLED", "INTERRUPTED"}
    assert finished["current"]["outcome_unknown"] and finished["current"]["result"] is None
    assert c["client"].post(c["endpoint"], json=c["body"]).json()["execution"] == finished
    assert (c["root"] / "launches.log").read_text().splitlines() == ["launch"]


def test_isolated_offline_issuer_cannot_be_trusted_for_codex(app_case):
    from app.services.journal_offline import ISSUER, POLICY
    for pair in ((ISSUER, "synthetic-policy"), ("fixture-only-issuer", POLICY)):
        with pytest.raises(ValueError, match="issuer_invalid"):
            service.CodexJournalRuntime(runtime=app_case["provider"]._runtime, trusted_issuers=frozenset({pair}),
                                       reasoning_effort="low", timeout_seconds=30)


def test_topic_and_nonadmin_boundaries_preserve_storage(app_case, monkeypatch):
    c = app_case
    from app.api import local_synthesis
    from app.auth.resource_access import ResourceActor
    before = stored_state()
    assert c["client"].post(c["endpoint"].replace(c["scope"][0], "m2-other-topic"), json=c["body"]).status_code == 404
    monkeypatch.setattr(local_synthesis, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
    assert c["client"].get(c["endpoint"]).status_code == 404
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 404
    assert stored_state() == before


def test_failed_reservation_rolls_back_and_same_request_recovers(app_case, monkeypatch):
    from app.repositories import journal_executions
    c = app_case
    before = stored_state()
    with monkeypatch.context() as patch:
        def fail(_):
            raise OSError("fixture reservation fault")
        patch.setattr(journal_executions, "checkpoint", fail)
        response = c["client"].post(c["endpoint"], json=c["body"])
        assert response.status_code == 503, response.text
    assert stored_state() == before and not (c["root"] / "launches.log").exists()
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 200
    assert cli.wait_terminal(c["saved"])["current"]["state"] == "SUCCEEDED"


def test_worker_start_failure_is_known_and_exact_replay_does_not_launch(app_case, monkeypatch):
    from app.services import journal_worker
    c = app_case
    start = journal_worker.threading.Thread.start
    def fail_worker(thread):
        if thread.name == "journal-attempt-worker":
            raise RuntimeError("fixture start fault")
        return start(thread)
    with monkeypatch.context() as patch:
        patch.setattr(journal_worker.threading.Thread, "start", fail_worker)
        response = c["client"].post(c["endpoint"], json=c["body"])
        assert response.status_code == 503, response.text
    finished = cli.wait_terminal(c["saved"])
    assert finished["current"]["state"] == "FAILED" and not finished["current"]["outcome_unknown"]
    assert c["client"].post(c["endpoint"], json=c["body"]).json()["execution"] == finished
    assert not (c["root"] / "launches.log").exists()


@pytest.mark.parametrize("app_case", ["missing_completion"], indirect=True)
def test_unknown_result_does_not_automatically_retry(app_case):
    c = app_case
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 200
    finished = cli.wait_terminal(c["saved"])
    assert finished["current"]["state"] == "INTERRUPTED" and finished["current"]["outcome_unknown"]
    assert c["client"].post(c["endpoint"], json=c["body"]).json()["execution"] == finished
    rejected = c["client"].post(c["endpoint"], json=c["body"] | dict(request_id="new", expected_sequence=3))
    assert rejected.status_code == 409 and rejected.json()["detail"] == "journal_execution_unknown_outcome"
    assert (c["root"] / "launches.log").read_text().splitlines() == ["launch"]


def test_replay_survives_runtime_drift_revocation_and_expiry(app_case):
    c = app_case
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 200
    finished = cli.wait_terminal(c["saved"])
    (c["root"] / "fake_cli.py").write_text("raise SystemExit('must not execute')", encoding="utf-8")
    authority.revoke(*c["scope"], c["body"]["authorization_id"], request_id="revoke", issuer="owner", reason="fixture", now=c["now"])
    c["runtime"]._clock = lambda: c["now"] + timedelta(minutes=3)
    assert c["client"].post(c["endpoint"], json=c["body"]).json()["execution"] == finished
    assert (c["root"] / "launches.log").read_text().splitlines() == ["launch"]


def test_app_lifespan_closes_injected_runtime(app_case):
    c = app_case
    other = create_app(journal_codex_runtime=c["runtime"])
    with TestClient(other):
        assert not c["runtime"]._closed
    assert c["runtime"]._closed
    assert c["client"].get(c["endpoint"]).json()["reason"] == "journal_dispatch_closed"
    assert c["client"].post(c["endpoint"], json=c["body"]).status_code == 409
    assert not (c["root"] / "launches.log").exists()


@pytest.mark.parametrize("limited_by", ["initial_tokens", "attempts", "cumulative_tokens"])
def test_insufficient_reserved_budget_is_not_presented_as_available(app_case, limited_by):
    c = app_case
    grant = cli.approval(c["saved"], c["provider"])
    grant = grant.model_copy(update=dict(authorization_id="limited", actor_id=c["saved"]["contract"]["actor_id"],
        granted_at=c["now"], expires_at=c["now"] + timedelta(minutes=2),
        max_attempts=1 if limited_by == "attempts" else 3,
        max_reserved_output_tokens={"initial_tokens": 256, "attempts": 12000, "cumulative_tokens": 1024}[limited_by],
        proof=replace(grant.proof, accepted_at=c["now"], expires_at=c["now"] + timedelta(minutes=5))))
    authority.register(*c["scope"], grant, c["provider"].binding, issuer="fixture-only-issuer",
        decision_ref="synthetic limited budget", expected_sequence=0, now=c["now"])
    if limited_by != "initial_tokens":
        from app.repositories import journal_dispatch as admission, journal_executions
        from app.domain.journal_dispatch import Dispatch
        ref = authority.resolve(*c["scope"], "limited", c["saved"]["contract"]["actor_id"], c["provider"].binding, now=c["now"])
        _, claim = admission.admit(*c["scope"], Dispatch(request_id="first-reservation", expected_sequence=0),
            c["saved"]["contract"]["actor_id"], ref, c["provider"].binding, now=c["now"], adapter_manifest=c["provider"].manifest())
        assert claim is not None
        assert journal_executions.abandon(claim, reason="worker_failed", now=c["now"])
    status = c["client"].get(c["endpoint"]).json()
    assert next(item for item in status["authorities"] if item["authorization_id"] == "limited")["status"] == "BUDGET_EXHAUSTED"

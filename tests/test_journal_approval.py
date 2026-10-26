"""Synthetic Journal source records; no external authentication or Provider call."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
import unicodedata

from fastapi.testclient import TestClient
import pytest

from app.api import local_synthesis as api
from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.db.bootstrap import connect_metadata_index
from app.domain import journal_authority as domain
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.main import create_app
from app.repositories import journal_authority as authority, journal_executions
from app.services import journal_codex_dispatch, paper_bank
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
from tests import test_journal_authority as prior, test_codex_journal as cli
from tests.test_local_synthesis import select

source = prior.source
completed = prior.completed
execution = prior.execution
seeded = prior.seeded


def source_record(execution):
    return domain.Record(topic_id=prior.scope(execution)[0], job_id=prior.scope(execution)[1],
        execution_id=prior.scope(execution)[2], grant=prior.prior.grant(execution), binding=prior.BINDING,
        registered_by="synthetic-owner", decision_ref="original-source", expected_sequence=0,
        registered_at=prior.NOW).model_dump(mode="json")


def exact_record(value):
    return domain.Record.model_validate_json(preserved_json(value)).model_dump(mode="json")


def test_source_record_registration_preserves_timestamp_and_exact_replay(execution, monkeypatch):
    monkeypatch.setattr(journal_executions, "_time", lambda _: prior.NOW + timedelta(seconds=1))
    record = source_record(execution)
    result, created = authority.register_record(*prior.scope(execution), record)
    assert created and result == record
    assert authority.detail(*prior.scope(execution), record["grant"]["authorization_id"])["record"] == record
    monkeypatch.setattr(journal_executions, "_time", lambda _: prior.NOW + timedelta(days=1))
    assert authority.register_record(*prior.scope(execution), record) == (record, False)


def db_state():
    with closing(connect_metadata_index(read_only=True)) as db:
        return "\n".join(db.iterdump())


@pytest.mark.parametrize("change", ["grant_expired", "proof_expired", "future", "actor", "sequence"])
def test_repository_checks_current_validity_and_rolls_back_denial(execution, monkeypatch, change):
    record = source_record(execution)
    current = prior.NOW + timedelta(seconds=1)
    if change == "grant_expired":
        current += timedelta(minutes=3)
    elif change == "proof_expired":
        record["grant"]["proof"]["expires_at"] = (prior.NOW + timedelta(microseconds=1)).isoformat()
    elif change == "future":
        record["registered_at"] = (prior.NOW + timedelta(seconds=30)).isoformat()
    elif change == "actor":
        record["grant"]["actor_id"] = "other-actor"
    else:
        record["expected_sequence"] = 1
    record = exact_record(record)
    # Prove rejection is current-time/actor/sequence, not a malformed historical
    # datetime representation (Pydantic's canonical JSON uses Z, not +00:00).
    domain.read_record(record, execution["contract"])
    monkeypatch.setattr(journal_executions, "_time", lambda _: current)
    before = db_state()
    with pytest.raises(ValueError):
        authority.register_record(*prior.scope(execution), record)
    assert db_state() == before


def test_record_conflict_is_exact_including_timestamp_and_other_execution(execution, completed, monkeypatch):
    from tests import test_journal_execution as executions_test
    monkeypatch.setattr(journal_executions, "_time", lambda _: prior.NOW + timedelta(seconds=1))
    record = source_record(execution)
    authority.register_record(*prior.scope(execution), record)
    before = db_state()
    for field, value in (("registered_at", (prior.NOW + timedelta(microseconds=1)).isoformat()),
                         ("decision_ref", "different")):
        changed = exact_record(record | {field: value})
        domain.read_record(changed, execution["contract"])
        with pytest.raises(ValueError, match="conflict"):
            authority.register_record(*prior.scope(execution), changed)
        assert db_state() == before
    other = executions_test.prepare(completed, request_id="other")[0]
    with pytest.raises(ValueError, match="conflict"):
        authority.register_record(*prior.scope(other), source_record(other))


def test_expiry_while_waiting_for_real_writer_lock_cannot_register(execution, monkeypatch):
    record = source_record(execution)
    original_connect = authority.connect_metadata_index
    connected, begin_entered, start, clock_read = Event(), Event(), Event(), Event()
    current = [prior.NOW + timedelta(seconds=1)]
    def clock(_):
        clock_read.set()
        return current[0]
    class Connection:
        def __init__(self):
            self.connection = original_connect()
            connected.set()
        def __enter__(self):
            self.connection.__enter__()
            return self
        def __exit__(self, *args):
            return self.connection.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                assert start.wait(10)
                begin_entered.set()
            return self.connection.execute(sql, *args)
    monkeypatch.setattr(authority, "connect_metadata_index", Connection)
    monkeypatch.setattr(journal_executions, "_time", clock)
    before = db_state()
    with closing(original_connect()) as blocker, ThreadPoolExecutor(1) as pool:
        result = pool.submit(authority.register_record, *prior.scope(execution), record)
        try:
            assert connected.wait(10)
            blocker.execute("BEGIN IMMEDIATE")
            start.set()
            assert begin_entered.wait(10)
            assert not clock_read.is_set()
            current[0] = prior.NOW + timedelta(minutes=3)
        finally:
            start.set()
            blocker.rollback()
        with pytest.raises(ValueError, match="authority_expired"):
            result.result(timeout=10)
    assert clock_read.is_set() and db_state() == before


def test_atomic_source_registration_rollback_then_retry(execution, monkeypatch):
    monkeypatch.setattr(journal_executions, "_time", lambda _: prior.NOW)
    def fault(phase):
        if phase == "after_registration":
            raise RuntimeError("synthetic failure")
    monkeypatch.setattr(authority, "checkpoint", fault)
    before = db_state()
    record = source_record(execution)
    with pytest.raises(RuntimeError):
        authority.register_record(*prior.scope(execution), record)
    assert db_state() == before
    monkeypatch.setattr(authority, "checkpoint", lambda _: None)
    assert authority.register_record(*prior.scope(execution), record) == (record, True)


@pytest.fixture
def app_case(seeded, tmp_path, monkeypatch):
    from app.services.journal_approval import JournalApprovalRuntime
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("XRML_JOURNAL_OFFLINE_ENABLED", "0")
    local_runtime = cli.runtime_at(tmp_path / "fake", "success")
    runtime = journal_codex_dispatch.CodexJournalRuntime(runtime=local_runtime,
        trusted_issuers=frozenset({("fixture-only-issuer", "synthetic-policy")}), reasoning_effort="low", timeout_seconds=30)
    holder, calls = {}, []
    def lookup(contract):
        calls.append(deepcopy(contract))
        contract["actor_id"] = "source-mutated-copy"
        return holder.get("offer")
    approvals = JournalApprovalRuntime(source=lookup, approvers=frozenset({"local-internal-user"}),
        trusted_issuers=frozenset({("fixture-only-issuer", "synthetic-policy")}))
    app = create_app(journal_codex_runtime=runtime, journal_approval_runtime=approvals)
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as http:
        topic = seeded.TOPIC_A_ID
        root = f"/api/topics/{topic}/local-synthesis"
        response = http.post(root, json=dict(request_id="freeze", selection=select(paper_bank.list_topic_papers(topic)["items"][:1])))
        assert response.status_code == 201, response.text
        job = response.json()["job"]
        base = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            assert http.post(base + "/step", json=dict(expected_step=step)).status_code == 200
        response = http.post(base + "/journal-executions", json=dict(request_id="prepare", expected_input_digest=job["input_digest"],
            provider_name="codex_cli", model="offline-requested-model", max_attempts=2, max_output_tokens=1024))
        assert response.status_code == 201, response.text
        saved = response.json()["execution"]
        scope = cli.prior.scope(saved)
        provider = CodexJournalV2Provider(saved["contract"], runtime=local_runtime, reasoning_effort="low", timeout_seconds=30)
        now = datetime.now(timezone.utc)
        grant = cli.approval(saved, provider)
        grant = grant.model_copy(update=dict(actor_id=saved["contract"]["actor_id"], granted_at=now, expires_at=now + timedelta(minutes=2),
            proof=replace(grant.proof, accepted_at=now, expires_at=now + timedelta(minutes=3))))
        record = domain.Record(topic_id=topic, job_id=scope[1], execution_id=scope[2], grant=grant, binding=provider.binding,
            registered_by="fixture-only-issuer", decision_ref="synthetic-e\u0301-decision", expected_sequence=0,
            registered_at=now).model_dump(mode="json")
        holder["offer"] = dict(record=record, manifest=provider.manifest())
        url = base + "/journal-executions/" + scope[2]
        body = dict(authorization_id=grant.authorization_id, expected_sequence=0,
            expected_contract_digest=digest(saved["contract"]), expected_record_digest=digest(record),
            expected_record_sha256=sha(preserved_json(record)))
        actor = ResourceActor.oidc_user("local-internal-user")
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: actor)
        yield dict(app=app, http=http, url=url, endpoint=url + "/codex-approval", saved=saved, runtime=runtime,
            approvals=approvals, holder=holder, calls=calls, body=body, provider=provider, now=now, scope=scope,
            fake=tmp_path / "fake", actor=actor)


def test_preview_register_and_disabled_restart_readback_do_not_dispatch(app_case):
    c = app_case
    before = db_state()
    response = c["http"].get(c["endpoint"])
    assert response.status_code == 200, response.text
    shown = response.json()
    assert shown["approval_allowed"] and shown["execution_allowed"] is False
    assert shown["offer"]["record_digest"] == c["body"]["expected_record_digest"]
    assert shown["offer"]["record_sha256"] == c["body"]["expected_record_sha256"]
    assert shown["manifest"] == c["provider"].manifest()
    assert db_state() == before
    response = c["http"].post(c["endpoint"], json=c["body"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["authority"]["record"] == c["holder"]["offer"]["record"]
    assert result["execution"] == c["saved"] and result["execution"]["current"]["attempt"] == 0
    assert not (c["fake"] / "launches.log").exists()
    restart = create_app()
    restart.dependency_overrides[require_product_user] = lambda: None
    with TestClient(restart) as http:
        assert http.post(c["endpoint"], json=c["body"]).json() == result
        assert http.get(c["endpoint"]).json()["reason"] == "journal_approval_disabled"
        assert http.post(c["endpoint"], json=c["body"] | dict(authorization_id="new")).status_code == 409
    assert len(c["calls"]) == 2


def test_auth_disabled_other_actor_public_read_and_unlisted_admin_never_reach_source(app_case, monkeypatch, seeded):
    from app.services import topic_profiles
    from app.services.journal_approval import JournalApprovalRuntime
    c = app_case
    before = db_state()
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor("local-internal-user", "disabled", "local", True))
    assert c["http"].get(c["endpoint"]).status_code == 403
    assert c["http"].post(c["endpoint"], json=c["body"]).status_code == 403
    monkeypatch.setattr(topic_profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {c["scope"][0]})
    for actor in (ResourceActor.oidc_user("reader"), ResourceActor.oidc_user("outsider", is_admin=True)):
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _, actor=actor: actor)
        assert c["http"].get(c["endpoint"]).status_code in {403, 404}
        assert c["http"].post(c["endpoint"], json=c["body"]).status_code in {403, 404}
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
    c["app"].state.journal_approval_runtime = JournalApprovalRuntime(source=lambda _: pytest.fail("source called"),
        approvers=frozenset({"other-user"}), trusted_issuers=frozenset({("issuer", "policy")}))
    assert c["http"].post(c["endpoint"], json=c["body"]).status_code == 403
    assert c["http"].get(c["endpoint"].replace(c["scope"][0], seeded.TOPIC_B_ID)).status_code == 404
    assert c["calls"] == [] and db_state() == before


def test_strict_browser_fields_and_stale_request_cannot_write_or_replay(app_case):
    c = app_case
    before = db_state()
    for field in ("request_id", "approved", "proof", "grant", "issuer", "actor_id", "runtime", "manifest", "model"):
        assert c["http"].post(c["endpoint"], json=c["body"] | {field: "forged"}).status_code == 422
    assert c["calls"] == [] and db_state() == before
    changes = [dict(expected_contract_digest="a" * 64), dict(expected_record_digest="b" * 64),
               dict(expected_record_sha256="c" * 64),
               dict(expected_sequence=1), dict(authorization_id="other")]
    for change in changes:
        assert c["http"].post(c["endpoint"], json=c["body"] | change).status_code == 409
        assert db_state() == before
    assert c["http"].post(c["endpoint"], json=c["body"]).status_code == 200
    after = db_state()
    for change in changes:
        assert c["http"].post(c["endpoint"], json=c["body"] | change).status_code == 409
        assert db_state() == after


@pytest.mark.parametrize("change", ["issuer", "actor", "sequence", "future", "proof", "profile", "runtime", "decision", "budget"])
def test_changed_source_record_or_plan_never_registers(app_case, change):
    c = app_case
    assert c["http"].get(c["endpoint"]).json()["approval_allowed"]
    offer = c["holder"]["offer"]
    record = offer["record"]
    if change == "issuer":
        record["registered_by"] = "other"
    elif change == "actor":
        record["grant"]["actor_id"] = "other"
    elif change == "sequence":
        record["expected_sequence"] = 1
    elif change == "future":
        record["registered_at"] = (c["now"] + timedelta(seconds=30)).isoformat()
    elif change == "proof":
        record["grant"]["proof"]["accepted"] = False
    elif change == "profile":
        offer["manifest"]["schema_version"] = "CODEX_JOURNAL_CLI_V1"
    elif change == "runtime":
        offer["manifest"]["runtime_identity"]["launcher_sha256"] = "c" * 64
    elif change == "budget":
        record["grant"]["max_reserved_output_tokens"] = 256
    else:
        record["decision_ref"] = "different"
    if change == "future":
        offer["record"] = exact_record(record)
        domain.read_record(offer["record"], c["saved"]["contract"])
    if change == "budget":
        assert c["http"].get(c["endpoint"]).json()["reason"] == "journal_dispatch_budget_exhausted"
    before = db_state()
    assert c["http"].post(c["endpoint"], json=c["body"]).status_code in {409, 503}
    assert db_state() == before and not (c["fake"] / "launches.log").exists()


def test_unicode_source_drift_after_preview_cannot_register(app_case):
    c = app_case
    shown = c["http"].get(c["endpoint"]).json()
    assert shown["approval_allowed"]
    record = c["holder"]["offer"]["record"]
    original = record["decision_ref"]
    record["decision_ref"] = unicodedata.normalize("NFC", original)
    assert record["decision_ref"] != original
    assert digest(record) == shown["offer"]["record_digest"]
    domain.read_record(record, c["saved"]["contract"])
    before = db_state()
    response = c["http"].post(c["endpoint"], json=c["body"])
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "journal_approval_changed"
    assert db_state() == before and not (c["fake"] / "launches.log").exists()


def test_source_error_and_bootstrap_failure_are_explicit(app_case):
    from app.services.journal_approval import JournalApprovalRuntime
    c = app_case
    def broken(_):
        raise RuntimeError("fixture-sensitive-secret")
    c["app"].state.journal_approval_runtime = JournalApprovalRuntime(source=broken,
        approvers=frozenset({c["actor"].subject}), trusted_issuers=frozenset({("issuer", "policy")}))
    before = db_state()
    assert c["http"].get(c["endpoint"]).json()["reason"] == "journal_approval_source_unavailable"
    response = c["http"].post(c["endpoint"], json=c["body"])
    assert response.status_code == 503 and "sensitive" not in response.text
    c["app"].state.runtime_bootstrap_ok = False
    assert c["http"].post(c["endpoint"], json=c["body"]).json()["detail"] == "journal_approval_disabled"
    assert db_state() == before


def test_six_duplicate_posts_register_one_exact_record(app_case):
    c = app_case
    barrier = Barrier(6)
    def submit(_):
        barrier.wait(timeout=10)
        response = c["http"].post(c["endpoint"], json=c["body"])
        assert response.status_code == 200, response.text
        return response.json()
    with ThreadPoolExecutor(6) as pool:
        rows = list(pool.map(submit, range(6)))
    assert all(row == rows[0] for row in rows)
    assert authority.listing(*c["scope"])["total"] == 1
    assert rows[0]["execution"] == c["saved"]
    assert not (c["fake"] / "launches.log").exists()


def test_expired_revoked_registration_replay_never_renews_source(app_case, monkeypatch):
    c = app_case
    assert c["http"].post(c["endpoint"], json=c["body"]).status_code == 200
    future = c["now"] + timedelta(days=1)
    authority.revoke(*c["scope"], c["body"]["authorization_id"], request_id="withdraw", issuer=c["actor"].subject,
        reason="synthetic withdrawal", now=future)
    monkeypatch.setattr(journal_executions, "_time", lambda _: future)
    c["app"].state.journal_approval_runtime = None
    before, calls = db_state(), len(c["calls"])
    response = c["http"].post(c["endpoint"], json=c["body"])
    assert response.status_code == 200 and response.json()["authority"]["revocation"] is not None
    assert db_state() == before and len(c["calls"]) == calls


def test_real_session_owner_can_register_then_separately_dispatch_and_revoke_session(app_case, monkeypatch):
    from app.auth import security, sessions
    from app.auth.config import clear_auth_settings_cache, get_auth_settings
    from app.auth.resource_access import resource_actor_for_user
    from tests import test_auth_session_hardening as auth
    c = app_case
    monkeypatch.setattr(api, "resource_actor_for_user", resource_actor_for_user)
    with monkeypatch.context() as env:
        for key, value in auth._oidc_env().items():
            env.setenv(key, value)
        env.setenv("ADMIN_USER_IDS", "")
        env.setenv("ADMIN_USER_EMAILS", "")
        clear_auth_settings_cache()
        try:
            app = create_app(journal_approval_runtime=c["approvals"], journal_codex_runtime=c["runtime"])
            with TestClient(app) as http:
                before = db_state()
                assert http.get(c["endpoint"]).status_code == 401
                settings = get_auth_settings()
                cookie = security.encode_product_session_cookie(auth._user(c["actor"].subject), settings)
                http.cookies.set(settings.session_cookie_name, auth._tamper_signed_cookie_signature(cookie)[0])
                assert http.post(c["endpoint"], json=c["body"]).status_code == 401
                assert c["calls"] == [] and db_state() == before
                http.cookies.set(settings.session_cookie_name, cookie)
                assert http.get("/auth/me").json()["is_admin"] is False
                assert http.get(c["endpoint"]).json()["approval_allowed"]
                response = http.post(c["endpoint"], json=c["body"])
                assert response.status_code == 200, response.text
                assert response.json()["execution"] == c["saved"]
                assert not (c["fake"] / "launches.log").exists()
                body = dict(request_id="manual-dispatch", expected_sequence=0, acknowledge_unknown=False,
                    authorization_id=c["body"]["authorization_id"], expected_authority_digest=c["body"]["expected_record_digest"],
                    expected_binding_digest=c["provider"].binding.command_shape_sha256)
                assert http.post(c["url"] + "/codex-dispatch", json=body).status_code == 200
                final = cli.wait_terminal(c["saved"])
                assert final["current"]["state"] == "SUCCEEDED"
                assert http.post(c["endpoint"], json=c["body"]).json()["execution"] == final
                assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]
                session = security._load_signed_cookie(settings, salt="product-session", value=cookie,
                    max_age=settings.product_session_ttl_seconds)
                assert session is not None and sessions.revoke_product_session(session["sid"])
                after, calls = db_state(), len(c["calls"])
                assert http.get(c["endpoint"]).status_code == 401
                assert http.post(c["endpoint"], json=c["body"]).status_code == 401
                assert db_state() == after and len(c["calls"]) == calls
        finally:
            clear_auth_settings_cache()

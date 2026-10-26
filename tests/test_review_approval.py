"""Authenticated registration boundary; all users and proof sources are fabricated."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from app.api import review_execution as api
from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.db.bootstrap import connect_metadata_index
from app.domain import review_authority, review_execution as domain
from app.domain.review_preparation import encoded, sha
from app.main import create_app
from app.repositories import review_authorities
from app.services import review_approval as service
from tests import test_review_codex_execution as prior

seeded = prior.seeded
range_source = prior.range_source
app_case = prior.app_case


def test_default_approval_boundary_denies_without_writing(app_case):
    c = app_case
    ready = prior.prepare(c)
    url = c["url"] + "/" + ready["contract"]["execution_id"]
    before = c["http"].get(url).json()
    response = c["http"].get(url + "/codex-approval")
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "paper_approval_authenticated_user_required"
    assert c["http"].get(url).json() == before


def db_state():
    with closing(connect_metadata_index(read_only=True)) as db:
        return "\n".join(db.iterdump())


@pytest.fixture
def approval_case(app_case, monkeypatch):
    c = app_case
    ready = prior.prepare(c)
    actor = ResourceActor.oidc_user(c["scope"].actor, is_admin=True)
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: actor)
    now = datetime.now(timezone.utc)
    old = prior.proofs.grant(ready["contract"])
    grant = old.model_copy(update=dict(granted_at=now, expires_at=now + timedelta(minutes=2),
        proof=replace(old.proof, accepted_at=now, expires_at=now + timedelta(minutes=3))))
    value = review_authority.Record(grant=grant, issuer_id="fixture-paper-issuer", decision_ref="synthetic-source",
        registered_at=now, expected_sequence=0).model_dump(mode="json")
    holder, calls = {"record": value}, []

    def source(contract):
        calls.append(deepcopy(contract))
        # A source must not be able to mutate the caller's persisted contract.
        contract["actor_id"] = "mutated-source-copy"
        return holder["record"]

    runtime = service.PaperApprovalRuntime(source=source, approvers=frozenset({actor.subject}),
        trusted_issuers=frozenset({("fixture-paper-issuer", "synthetic-policy-e\u0301")}), input_origin="FIXTURE_SYNTHETIC")
    c["app"].state.paper_approval_runtime = runtime
    url = c["url"] + "/" + ready["contract"]["execution_id"]
    body = dict(request_id="human-approve", expected_sequence=0,
        expected_contract_sha256=sha(encoded(ready["contract"])), expected_authority_sha256=sha(encoded(value)))
    return c | dict(ready=ready, actor=actor, holder=holder, calls=calls, approval_runtime=runtime,
        approval_url=url + "/codex-approval", detail_url=url, approval_body=body, now=now)


def test_preview_register_and_restart_replay_preserve_source_without_dispatch(approval_case):
    c = approval_case
    before = db_state()
    response = c["http"].get(c["approval_url"])
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["approval_allowed"] and preview["execution_allowed"] is False
    assert preview["offer"]["record_sha256"] == c["approval_body"]["expected_authority_sha256"]
    assert preview["offer"]["input_origin"] == "FIXTURE_SYNTHETIC"
    assert "proof" not in encoded(preview) and db_state() == before
    response = c["http"].post(c["approval_url"], json=c["approval_body"])
    assert response.status_code == 200, response.text
    saved = response.json()
    assert [event["state"] for event in saved["events"]] == ["READY", "APPROVED"]
    assert saved["current"]["claim_sha256"] is None
    assert saved["contract"] == c["ready"]["contract"]
    with closing(connect_metadata_index(read_only=True)) as db:
        stored = db.execute("SELECT payload_json FROM paper_review_authorities").fetchone()[0]
    assert stored == encoded(c["holder"]["record"])
    assert len(c["calls"]) == 2
    app = create_app()  # No approval source or execution runtime after restart.
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as http:
        assert http.post(c["approval_url"], json=c["approval_body"]).json() == saved
        assert http.get(c["approval_url"]).json()["reason"] == "paper_approval_disabled"
        assert http.post(c["approval_url"], json=c["approval_body"] | dict(request_id="new")).status_code == 409
    assert len(c["calls"]) == 2 and not (c["fake"] / "launches.log").exists()


@pytest.mark.parametrize("actor", [
    ResourceActor("local-internal-user", "disabled", "local", True),
    ResourceActor(None, "oidc", "system", True, "local-internal-user"),
])
def test_unauthenticated_or_system_actor_never_calls_source(approval_case, monkeypatch, actor):
    c = approval_case
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: actor)
    before = db_state()
    for response in (c["http"].get(c["approval_url"]), c["http"].post(c["approval_url"], json=c["approval_body"])):
        assert response.status_code in {403, 404}, response.text
    assert c["calls"] == [] and db_state() == before


def test_admin_not_allowlisted_and_other_actor_topic_or_public_reader_cannot_approve(approval_case, monkeypatch, seeded):
    from app.services import topic_profiles
    c = approval_case
    c["app"].state.paper_approval_runtime = service.PaperApprovalRuntime(source=lambda _: pytest.fail("source called"),
        approvers=frozenset({"another-approver"}), trusted_issuers=frozenset({("issuer", "policy")}))
    before = db_state()
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 403
    assert c["http"].get(c["approval_url"]).status_code == 403
    c["app"].state.paper_approval_runtime = c["approval_runtime"]
    assert c["http"].post(c["approval_url"].replace(c["scope"].topic, seeded.TOPIC_B_ID), json=c["approval_body"]).status_code == 404
    monkeypatch.setattr(topic_profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {c["scope"].topic})
    for subject in ("reader", "another-admin"):
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _, subject=subject:
            ResourceActor.oidc_user(subject, is_admin=subject == "another-admin"))
        assert c["http"].get(c["approval_url"]).status_code == 404
        assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 404
    assert c["calls"] == [] and db_state() == before


def test_browser_fields_are_rejected_before_source_or_writes(approval_case):
    c = approval_case
    before = db_state()
    for field in ("approved", "proof", "issuer_id", "grant", "model", "runtime", "actor_id", "input_origin"):
        assert c["http"].post(c["approval_url"], json=c["approval_body"] | {field: True}).status_code == 422
    assert c["calls"] == [] and db_state() == before


@pytest.mark.parametrize("field,value", [("expected_contract_sha256", "a" * 64),
    ("expected_authority_sha256", "b" * 64), ("expected_sequence", 1)])
def test_stale_request_cannot_approve_or_replay(approval_case, field, value):
    c = approval_case
    changed = c["approval_body"] | {field: value}
    before = db_state()
    assert c["http"].post(c["approval_url"], json=changed).status_code == 409
    assert db_state() == before
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 200
    after = db_state()
    assert c["http"].post(c["approval_url"], json=changed).status_code == 409
    assert db_state() == after


@pytest.mark.parametrize("change", ["issuer", "policy", "actor", "sequence", "future", "proof", "expiry", "revoked", "digest"])
def test_source_changes_after_preview_fail_without_partial_approval(approval_case, change):
    c = approval_case
    assert c["http"].get(c["approval_url"]).json()["approval_allowed"]
    value = c["holder"]["record"]
    if change == "issuer":
        value["issuer_id"] = "unknown-issuer"
    elif change in {"policy", "actor"}:
        value["grant"]["policy_version" if change == "policy" else "actor_id"] = "changed"
    elif change == "sequence":
        value["expected_sequence"] = 1
    elif change == "future":
        value["registered_at"] = (c["now"] + timedelta(seconds=30)).isoformat()
    elif change == "proof":
        value["grant"]["proof"]["accepted"] = False
    elif change in {"expiry", "revoked"}:
        # Initially valid, now expired/revoked by the source. The source must not
        # silently replace the previewed decision even with a valid new digest.
        value["grant"]["expires_at" if change == "expiry" else "granted_at"] = (c["now"] - timedelta(seconds=1)).isoformat()
        if change == "revoked":
            value["grant"]["proof"]["revoked_at"] = c["now"].isoformat()
    else:
        value["decision_ref"] = "different-source-decision"
    before = db_state()
    response = c["http"].post(c["approval_url"], json=c["approval_body"])
    assert response.status_code in {409, 503}, response.text
    assert db_state() == before and not (c["fake"] / "launches.log").exists()


def test_expiry_between_preview_and_register_uses_actual_time(approval_case, monkeypatch):
    c = approval_case
    assert c["http"].get(c["approval_url"]).json()["approval_allowed"]
    future = c["now"] + timedelta(minutes=3)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future
    monkeypatch.setattr(service, "datetime", Later)
    before = db_state()
    response = c["http"].post(c["approval_url"], json=c["approval_body"])
    assert response.status_code == 409 and response.json()["detail"] == "paper_review_authority_expired"
    assert db_state() == before


def test_source_failure_bootstrap_disable_and_origin_mismatch_do_not_write(approval_case):
    c = approval_case
    def broken(_):
        raise RuntimeError("sensitive-fixture-secret-path")
    runtime = service.PaperApprovalRuntime(source=broken, approvers=frozenset({c["actor"].subject}),
        trusted_issuers=frozenset({("fixture-paper-issuer", "synthetic-policy-e\u0301")}), input_origin="FIXTURE_SYNTHETIC")
    c["app"].state.paper_approval_runtime = runtime
    before = db_state()
    assert c["http"].get(c["approval_url"]).json()["reason"] == "paper_approval_source_unavailable"
    response = c["http"].post(c["approval_url"], json=c["approval_body"])
    assert response.status_code == 503 and "sensitive" not in response.text
    c["app"].state.paper_approval_runtime = service.PaperApprovalRuntime(source=broken,
        approvers=frozenset({c["actor"].subject}), trusted_issuers=frozenset({("issuer", "policy")}))
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 409
    c["app"].state.runtime_bootstrap_ok = False
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).json()["detail"] == "paper_approval_disabled"
    assert db_state() == before


def test_registration_rollback_and_retry_preserve_single_record(approval_case, monkeypatch):
    from app.repositories import review_executions
    c = approval_case
    before = db_state()
    def fault(phase):
        if phase == "after_authority":
            raise RuntimeError("synthetic transaction failure")
    monkeypatch.setattr(review_executions, "checkpoint", fault)
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 503
    assert db_state() == before
    monkeypatch.setattr(review_executions, "checkpoint", lambda _: None)
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 200


def test_six_simultaneous_approvals_register_once(approval_case):
    c = approval_case
    barrier = Barrier(6)
    def submit(_):
        barrier.wait(timeout=10)
        response = c["http"].post(c["approval_url"], json=c["approval_body"])
        assert response.status_code == 200, response.text
        return response.json()
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(submit, range(6)))
    assert all(row == rows[0] for row in rows)
    assert [event["state"] for event in rows[0]["events"]] == ["READY", "APPROVED"]
    assert rows[0]["current"]["claim_sha256"] is None
    with closing(connect_metadata_index(read_only=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM paper_review_authorities").fetchone()[0] == 1
    assert not (c["fake"] / "launches.log").exists()


def test_revocation_never_renews_approval_on_exact_replay(approval_case):
    c = approval_case
    assert c["http"].post(c["approval_url"], json=c["approval_body"]).status_code == 200
    review_authorities.revoke(c["scope"], c["ready"]["contract"]["execution_id"],
        domain.Control(request_id="withdraw", expected_sequence=1, action="revoke"), reason="synthetic withdrawal")
    calls = len(c["calls"])
    before = db_state()
    c["app"].state.paper_approval_runtime = None
    response = c["http"].post(c["approval_url"], json=c["approval_body"])
    assert response.status_code == 200 and response.json()["authority"]["validity"] == "REVOKED"
    assert db_state() == before and len(c["calls"]) == calls


def test_configuration_rejects_offline_issuer_and_empty_allowlists():
    from app.services.review_transport_offline import ISSUER, POLICY
    good = dict(source=lambda _: None, approvers=frozenset({"owner"}), trusted_issuers=frozenset({("issuer", "policy")}))
    for changes in (dict(approvers=frozenset()), dict(trusted_issuers=frozenset()),
                    dict(trusted_issuers=frozenset({(ISSUER, "policy")})),
                    dict(trusted_issuers=frozenset({("issuer", POLICY)}))):
        with pytest.raises(ValueError, match="configuration_invalid"):
            service.PaperApprovalRuntime(**(good | changes))


def test_real_session_dependency_authenticates_nonadmin_owner_and_rejects_invalid_sessions(approval_case, monkeypatch):
    from app.auth import security, sessions
    from app.auth.config import clear_auth_settings_cache, get_auth_settings
    from app.auth.resource_access import resource_actor_for_user
    from tests import test_auth_session_hardening as auth
    c = approval_case
    # Exercise actual cookie/session validation and actor conversion. Only the
    # opaque session and issuer evidence are fabricated; no external OIDC login.
    monkeypatch.setattr(api, "resource_actor_for_user", resource_actor_for_user)
    with monkeypatch.context() as env:
        for key, value in auth._oidc_env().items():
            env.setenv(key, value)
        env.setenv("ADMIN_USER_IDS", "")
        env.setenv("ADMIN_USER_EMAILS", "")
        clear_auth_settings_cache()
        try:
            app = create_app(paper_approval_runtime=c["approval_runtime"], paper_codex_runtime=c["runtime"])
            with TestClient(app) as http:
                before = db_state()
                assert http.get(c["approval_url"]).status_code == 401
                assert http.post(c["approval_url"], json=c["approval_body"]).status_code == 401
                settings = get_auth_settings()
                cookie = security.encode_product_session_cookie(auth._user(c["actor"].subject), settings)
                tampered = auth._tamper_signed_cookie_signature(cookie)[0]
                http.cookies.set(settings.session_cookie_name, tampered)
                assert http.post(c["approval_url"], json=c["approval_body"]).status_code == 401
                assert c["calls"] == [] and db_state() == before
                http.cookies.set(settings.session_cookie_name, cookie)
                me = http.get("/auth/me").json()
                assert me["authenticated"] and me["is_admin"] is False
                assert http.get(c["approval_url"]).json()["approval_allowed"]
                response = http.post(c["approval_url"], json=c["approval_body"])
                assert response.status_code == 200, response.text
                assert response.json()["current"]["state"] == "APPROVED"
                assert not (c["fake"] / "launches.log").exists()
                request = dict(request_id="manual-dispatch", expected_sequence=1,
                    expected_contract_sha256=c["approval_body"]["expected_contract_sha256"],
                    expected_authority_sha256=c["approval_body"]["expected_authority_sha256"])
                assert http.post(c["detail_url"] + "/codex-dispatch", json=request).status_code == 200
                c["runtime"].wait_idle(10)
                assert http.get(c["detail_url"]).json()["current"]["state"] == "RESULT_SAVED"
                assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]
                session = security._load_signed_cookie(settings, salt="product-session", value=cookie,
                    max_age=settings.product_session_ttl_seconds)
                assert session is not None
                assert sessions.revoke_product_session(session["sid"])
                after, calls = db_state(), len(c["calls"])
                assert http.get(c["approval_url"]).status_code == 401
                assert http.post(c["approval_url"], json=c["approval_body"]).status_code == 401
                assert len(c["calls"]) == calls and db_state() == after
        finally:
            clear_auth_settings_cache()

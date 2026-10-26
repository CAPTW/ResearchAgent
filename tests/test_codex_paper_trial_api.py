"""First-output trial API: synthetic sessions, decisions and fake Python only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

from fastapi.testclient import TestClient
import pytest

from app.api import review_execution as api
from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.domain.review_preparation import encoded, sha
from app.db.bootstrap import connect_metadata_index
from app.main import create_app
from app.services.codex_cli_report_draft import CliExecResult
from app.services.providers.base import ProviderError
from tests import test_review_codex_execution as prior
from tests.test_review_approval import db_state

seeded = prior.seeded
range_source = prior.range_source
app_case = prior.app_case


def test_default_trial_is_unavailable_and_does_not_issue_or_write(app_case, monkeypatch):
    c = app_case
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user(c["scope"].actor, is_admin=True))
    before = db_state()
    response = c["http"].get(c["root"] + "/codex-qualification")
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False and response.json()["execution_allowed"] is False
    body = dict(request_id="no-source", expected_binding_sha256="a" * 64, expected_decision_sha256="b" * 64)
    assert c["http"].post(c["root"] + "/codex-qualification-attempts", json=body).status_code == 409
    assert db_state() == before and not (c["fake"] / "launches.log").exists()


@pytest.fixture
def trial_case(app_case, monkeypatch):
    from app.domain import codex_paper_trial as domain
    from app.services import codex_paper_trial as service
    c = app_case
    actor = ResourceActor.oidc_user(c["scope"].actor, is_admin=True)
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: actor)
    now = datetime.now(timezone.utc)
    holder, calls = {}, []
    def source(contract):
        calls.append(deepcopy(contract))
        return deepcopy(holder.get("decision"))
    runtime = service.PaperTrialRuntime(paper=c["runtime"], source=source,
        approvers=frozenset({actor.subject}), trusted_issuers=frozenset({("trial-fixture", "fixture-policy")}))
    contract = runtime.contract(c["scope"])
    holder["decision"] = domain.Decision(issuer_id="trial-fixture", policy_version="fixture-policy",
        decision_id="one-shot-e\u0301", decision_ref="synthetic-local-test", account_ref="not-a-real-account",
        actor_id=actor.subject, binding_sha256=sha(encoded(contract)), granted_at=now,
        expires_at=now + timedelta(minutes=5), reserved_output_tokens=2200).model_dump(mode="json")
    c["app"].state.paper_trial_runtime = runtime
    url = c["root"] + "/codex-qualification-attempts"
    body = dict(request_id="first-trial", expected_binding_sha256=sha(encoded(contract)),
        expected_decision_sha256=sha(encoded(holder["decision"])))
    return c | dict(trial_runtime=runtime, actor=actor, trial_url=url, trial_body=body, contract=contract,
                    holder=holder, calls=calls, now=now)


def test_trial_preserves_input_capture_before_validation_and_reopens_without_source(trial_case, monkeypatch):
    from app.repositories import codex_paper_trials as repo
    from app.services import codex_paper_trial as service
    c = trial_case
    before = db_state()
    shown = c["http"].get(c["root"] + "/codex-qualification").json()
    assert shown["enabled"] and not shown["execution_allowed"]
    assert shown["decision_sha256"] == c["trial_body"]["expected_decision_sha256"]
    assert shown["account_verified"] is False and db_state() == before
    original = service.validate_capture
    def ordinary_rows():
        with closing(connect_metadata_index(read_only=True)) as db:
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() else None
                for table in ("paper_review_executions", "paper_review_authorities", "ai_review_input_history")}
    ordinary_before = ordinary_rows()
    seen = []
    def validation(contract, capture):
        stored = repo.detail(c["scope"], repo.listing(c["scope"])["items"][0]["trial_id"])
        assert stored["current"]["state"] == "CAPTURED"
        assert stored["current"]["capture"] == capture
        seen.append(True)
        return original(contract, capture)
    monkeypatch.setattr(service, "validate_capture", validation)
    response = c["http"].post(c["trial_url"], json=c["trial_body"])
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["current"]["state"] == "VALIDATED_VALID" and seen == [True]
    assert saved["contract"] == c["contract"] and saved["decision"] == c["holder"]["decision"]
    assert saved["current"]["capture"]["last_message"] == (c["fake"] / "output.json").read_text(encoding="utf-8")
    assert c["http"].get(c["url"]).json()["items"] == []  # No ordinary execution/authority/receipt.
    assert ordinary_rows() == ordinary_before
    c["source"][2].unlink()
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as http:
        assert http.post(c["trial_url"], json=c["trial_body"]).json() == saved
        result = http.get(c["trial_url"] + "/" + saved["trial_id"])
        assert result.json() == saved and result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
    assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("mode", ["invalid", "unknown", "validation-crash"])
def test_failed_outputs_are_quarantined_without_another_call(trial_case, monkeypatch, mode):
    from app.services import codex_paper_trial as service
    c = trial_case
    if mode == "invalid":
        (c["fake"] / "output.json").write_text('{"not":"the schema"}', encoding="utf-8")
    elif mode == "unknown":
        def failed(*args, **kwargs):
            raise ProviderError("sensitive diagnostics must not reach history")
        monkeypatch.setattr(type(c["local_runtime"]), "run", failed)
    else:
        def crashed(*args):
            raise RuntimeError("private validation crash")
        monkeypatch.setattr(service, "validate_capture", crashed)
    response = c["http"].post(c["trial_url"], json=c["trial_body"])
    assert response.status_code == 200, response.text
    saved = response.json()
    expected = {"invalid": "VALIDATED_INVALID", "unknown": "UNKNOWN", "validation-crash": "CAPTURED"}[mode]
    assert saved["current"]["state"] == expected
    assert saved["current"]["capture_availability"] == ("UNAVAILABLE" if mode == "unknown" else "AVAILABLE")
    assert c["http"].post(c["trial_url"], json=c["trial_body"]).json() == saved
    assert "sensitive diagnostics" not in encoded(saved) and "private validation crash" not in encoded(saved)
    if mode != "unknown":
        assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]


def test_concurrent_duplicate_submissions_only_launch_once(trial_case):
    c = trial_case
    barrier = Barrier(6)
    def submit(_):
        barrier.wait(timeout=10)
        return c["http"].post(c["trial_url"], json=c["trial_body"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(6)))
    assert all(result.status_code == 200 for result in results), [r.text for r in results]
    assert len({result.json()["trial_id"] for result in results}) == 1
    assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]
    assert c["http"].post(c["trial_url"], json=c["trial_body"] | dict(request_id="rotated")).status_code == 409


def test_scope_identity_browser_flags_and_source_drift_cannot_claim(trial_case, monkeypatch, seeded):
    c = trial_case
    before = db_state()
    for field in ("approved", "proof", "decision", "model", "account_ref", "actor_id", "runtime"):
        assert c["http"].post(c["trial_url"], json=c["trial_body"] | {field: True}).status_code == 422
    assert c["calls"] == [] and db_state() == before
    c["holder"]["decision"]["account_ref"] = "changed-label"
    assert c["http"].post(c["trial_url"], json=c["trial_body"]).status_code == 409
    assert c["http"].post(c["trial_url"].replace(c["scope"].topic, seeded.TOPIC_B_ID), json=c["trial_body"]).status_code == 404
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("other", is_admin=True))
    assert c["http"].post(c["trial_url"], json=c["trial_body"]).status_code == 404
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor("local-internal-user", "disabled", "local", True))
    assert c["http"].get(c["root"] + "/codex-qualification").status_code == 403
    assert db_state() == before and not (c["fake"] / "launches.log").exists()


def test_cancel_during_capture_preserves_late_raw_without_reviving_result(trial_case, monkeypatch):
    c = trial_case
    entered, release = Event(), Event()
    capture = CliExecResult(0, "synthetic stdout", "", last_message="late raw e\u0301")
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return capture
    monkeypatch.setattr(type(c["local_runtime"]), "run", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: c["http"].post(c["trial_url"], json=c["trial_body"]))
        try:
            assert entered.wait(10)
            saved = c["http"].get(c["trial_url"]).json()["items"][0]
            endpoint = c["trial_url"] + "/" + saved["trial_id"] + "/control"
            c["app"].state.paper_trial_runtime = None
            control = dict(request_id="cancel-held", expected_sequence=saved["current"]["sequence"], action="cancel")
            cancelled = c["http"].post(endpoint, json=control)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["current"]["state"] == "CANCELLED"
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == 200, response.text
    current = response.json()["current"]
    assert current["state"] == "CANCELLED" and current["late_capture"]
    assert current["capture"] == asdict(capture) and current["validation"] == "NOT_ASSESSED"


def test_cancel_between_claim_and_start_prevents_any_process(trial_case, monkeypatch):
    from app.repositories import codex_paper_trials as repo
    c = trial_case
    entered, release = Event(), Event()
    original = repo.start
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, "start", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: c["http"].post(c["trial_url"], json=c["trial_body"]))
        try:
            assert entered.wait(10)
            saved = c["http"].get(c["trial_url"]).json()["items"][0]
            body = dict(request_id="cancel-before-start", expected_sequence=saved["current"]["sequence"], action="cancel")
            cancelled = c["http"].post(c["trial_url"] + "/" + saved["trial_id"] + "/control", json=body)
            assert cancelled.status_code == 200, cancelled.text
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == 200 and response.json()["current"]["state"] == "CANCELLED"
    assert not (c["fake"] / "launches.log").exists()


def test_expiry_and_source_changes_fail_before_process(trial_case):
    c = trial_case
    before = db_state()
    c["trial_runtime"]._clock = lambda: c["now"] + timedelta(minutes=6)
    assert c["http"].post(c["trial_url"], json=c["trial_body"]).status_code == 409
    assert db_state() == before
    c["trial_runtime"]._clock = lambda: datetime.now(timezone.utc)
    original = c["trial_runtime"]._source
    seen = []
    def changed(contract):
        seen.append(True)
        return original(contract) if len(seen) == 1 else None
    c["trial_runtime"]._source = changed
    assert c["http"].post(c["trial_url"], json=c["trial_body"]).status_code == 409
    saved = c["http"].post(c["trial_url"], json=c["trial_body"]).json()
    assert saved["current"]["state"] == "UNKNOWN" and saved["current"]["capture"] is None
    assert not (c["fake"] / "launches.log").exists()


def test_changed_runtime_fails_before_source_or_claim(trial_case):
    from pathlib import Path
    c = trial_case
    before = db_state()
    # Only the disposable Python fixture entrypoint, never an installed CLI.
    launcher = Path(c["local_runtime"].launcher[1])
    launcher.write_text(launcher.read_text(encoding="utf-8") + "\n# changed fixture\n", encoding="utf-8")
    response = c["http"].post(c["trial_url"], json=c["trial_body"])
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "paper_codex_runtime_changed"
    assert c["calls"] == [] and db_state() == before
    assert not (c["fake"] / "launches.log").exists()


def test_failed_capture_commit_does_not_reexecute(trial_case, monkeypatch):
    from app.repositories import codex_paper_trials as repo
    c = trial_case
    def failed(*args, **kwargs):
        raise RuntimeError("simulated storage interruption")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "capture", failed)
        response = c["http"].post(c["trial_url"], json=c["trial_body"])
    assert response.status_code == 503
    saved = c["http"].post(c["trial_url"], json=c["trial_body"]).json()
    assert saved["current"]["state"] == "STARTED" and saved["current"]["capture"] is None
    assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]


def test_real_session_authentication_and_scoped_raw_readback(trial_case, monkeypatch):
    from app.auth import security
    from app.auth.config import get_auth_settings, clear_auth_settings_cache
    from app.auth.resource_access import resource_actor_for_user
    from tests import test_auth_session_hardening as auth
    c = trial_case
    monkeypatch.setattr(api, "resource_actor_for_user", resource_actor_for_user)
    for key, value in auth._oidc_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("ADMIN_USER_EMAILS", "")
    clear_auth_settings_cache()
    try:
        app = create_app(paper_trial_runtime=c["trial_runtime"])
        with TestClient(app) as http:
            assert http.post(c["trial_url"], json=c["trial_body"]).status_code == 401
            settings = get_auth_settings()
            cookie = security.encode_product_session_cookie(auth._user(c["actor"].subject), settings)
            http.cookies.set(settings.session_cookie_name, auth._tamper_signed_cookie_signature(cookie)[0])
            assert http.get(c["root"] + "/codex-qualification").status_code == 401
            assert c["calls"] == []
            http.cookies.set(settings.session_cookie_name, cookie)
            assert http.get("/auth/me").json()["is_admin"] is False
            response = http.post(c["trial_url"], json=c["trial_body"])
            assert response.status_code == 200, response.text
            saved = response.json()
            assert saved["current"]["state"] == "VALIDATED_VALID"
            other = security.encode_product_session_cookie(auth._user("another-user"), settings)
            http.cookies.set(settings.session_cookie_name, other)
            target = c["trial_url"] + "/" + saved["trial_id"]
            assert http.get(target).status_code == 404
            assert http.post(target + "/control", json=dict(request_id="deny", expected_sequence=0, action="cancel")).status_code == 404
    finally:
        clear_auth_settings_cache()

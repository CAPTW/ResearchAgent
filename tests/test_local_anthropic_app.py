"""Local-owner composition: synthetic metadata/transport only, never real keys."""
from copy import deepcopy
import importlib
from contextlib import closing
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

import pytest

from tests import test_review_anthropic_native as native
from app.services import model_credentials

credential_status = model_credentials.status

range_source = native.range_source
seeded = native.seeded
native_case = native.native_case


@pytest.fixture
def local_case(native_case, monkeypatch):
    module = importlib.import_module("app.services.local_anthropic_app")
    c = native_case
    settings = dict(models={"anthropic_api": native.MODEL}, default_connector_id="codex_subscription", revision=1)
    monkeypatch.setattr(module.model_connections, "read", lambda: deepcopy(settings))
    runtime = module.LocalAnthropicAppRuntime()
    c["app"].state.paper_anthropic_native_runtime = runtime
    c.update(local=runtime, settings=settings)
    try:
        yield c
    finally:
        runtime.stop()


def decision_command(saved):
    return dict(request_id="owner-one", expected_sequence=saved["current"]["sequence"],
        expected_contract_sha256=saved["contract_sha256"], expected_binding_sha256=saved["binding_sha256"])


def test_owner_decision_is_explicit_and_does_not_resolve_key_or_dispatch(local_case):
    c = local_case
    saved, url, _, _ = native.ready(c)
    shown = c["http"].get(url + "/anthropic-native-approval")
    assert shown.status_code == 200, shown.text
    assert shown.json()["local_owner_decision_allowed"] is True
    assert shown.json()["offer"] is None
    assert c["resolutions"] == c["calls"] == []
    command = decision_command(saved)
    result = c["http"].post(url + "/anthropic-native-owner-decision", json=command, headers=native.HEADERS)
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt["command"] == command and receipt["execution_allowed"] is False
    assert c["http"].get(url).json()["current"]["state"] == "READY"
    offer = c["http"].get(url + "/anthropic-native-approval").json()["offer"]
    assert offer["authorization_kind"] == "OWNER_FIRST_OUTPUT"
    assert offer["decision_sha256"] == receipt["decision_sha256"]
    assert c["resolutions"] == c["calls"] == []


def decide(c, saved, url):
    command = decision_command(saved)
    response = c["http"].post(url + "/anthropic-native-owner-decision", json=command, headers=native.HEADERS)
    assert response.status_code == 200, response.text
    return response.json(), command


def test_explicit_register_dispatch_one_attempt_and_restart_exact_replay(local_case):
    c = local_case
    saved, url, prepare_command, _ = native.ready(c)
    receipt, decision = decide(c, saved, url)
    offer = c["http"].get(url + "/anthropic-native-approval").json()["offer"]
    approval = dict(request_id="owner-register", expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
        expected_authority_sha256=offer["record_sha256"])
    registered = c["http"].post(url + "/anthropic-native-approval", json=approval, headers=native.HEADERS)
    assert registered.status_code == 200, registered.text
    assert c["calls"] == c["resolutions"] == []
    dispatch = approval | dict(request_id="owner-dispatch", expected_sequence=1)
    response = c["http"].post(url + "/anthropic-native-dispatch", json=dispatch, headers=native.HEADERS)
    assert response.status_code == 200, response.text
    c["local"].wait_idle(15)
    saved_result = c["http"].get(url).json()
    assert saved_result["current"]["state"] == "RESULT_SAVED", saved_result["current"]
    assert len(c["resolutions"]) == len(c["calls"]) == 1
    c["local"].stop()
    module = importlib.import_module("app.services.local_anthropic_app")
    replacement = module.LocalAnthropicAppRuntime()
    c["app"].state.paper_anthropic_native_runtime = replacement
    c["state"].update(stored=False, revision=8)
    c["settings"]["models"]["anthropic_api"] = "changed-model"
    try:
        for endpoint, command in ((c["root"] + "/anthropic-native-requests", prepare_command),
                (url + "/anthropic-native-owner-decision", decision), (url + "/anthropic-native-approval", approval),
                (url + "/anthropic-native-dispatch", dispatch)):
            replay = c["http"].post(endpoint, json=command, headers=native.HEADERS)
            assert replay.status_code == 200, replay.text
            assert replay.json() == receipt if endpoint.endswith("owner-decision") else replay.json()["events"] == saved_result["events"]
        assert not c["http"].get(url + "/anthropic-native-status").json()["enabled"]
        assert len(c["resolutions"]) == len(c["calls"]) == 1
    finally:
        replacement.stop()


@pytest.mark.parametrize("field", ["expected_sequence", "expected_contract_sha256", "expected_binding_sha256"])
def test_decision_rejects_stale_or_changed_binding_without_storage(local_case, field):
    from app.repositories import local_anthropic_decisions
    c = local_case
    saved, url, _, _ = native.ready(c)
    command = decision_command(saved) | {field: 3 if field == "expected_sequence" else "f" * 64}
    result = c["http"].post(url + "/anthropic-native-owner-decision", json=command, headers=native.HEADERS)
    assert result.status_code == 409, result.text
    assert local_anthropic_decisions.get(saved["contract"]) is None
    assert c["resolutions"] == c["calls"] == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://foreign.invalid", "X-XRML-Paper-Anthropic-Action": "1"},
    {"Origin": native.ORIGIN, "X-XRML-Paper-Anthropic-Action": "1", "Forwarded": "for=127.0.0.1"}])
def test_owner_decision_requires_same_origin_and_explicit_intent(local_case, headers):
    c = local_case
    saved, url, _, _ = native.ready(c)
    response = c["http"].post(url + "/anthropic-native-owner-decision", json=decision_command(saved), headers=headers)
    assert response.status_code == 403, response.text
    assert c["resolutions"] == c["calls"] == []


def test_no_browser_proof_and_changed_key_cannot_create_decision(local_case):
    c = local_case
    saved, url, _, _ = native.ready(c)
    body = decision_command(saved)
    for extra in ("approved", "issuer", "proof", "api_key", "model", "credential_revision"):
        response = c["http"].post(url + "/anthropic-native-owner-decision", json=body | {extra: "DO-NOT-ECHO"}, headers=native.HEADERS)
        assert response.status_code == 422 and "DO-NOT-ECHO" not in response.text
    c["state"]["revision"] = 8
    response = c["http"].post(url + "/anthropic-native-owner-decision", json=body, headers=native.HEADERS)
    assert response.status_code == 409, response.text
    assert c["resolutions"] == c["calls"] == []


def test_decision_rollback_and_immutable_exact_replay(local_case, monkeypatch):
    from app.db.bootstrap import connect_metadata_index
    from app.repositories import review_executions, local_anthropic_decisions
    c = local_case
    saved, url, _, _ = native.ready(c)
    def fail(phase):
        if phase == "after_local_owner_decision":
            raise RuntimeError("synthetic rollback")
    monkeypatch.setattr(review_executions, "checkpoint", fail)
    failed = c["http"].post(url + "/anthropic-native-owner-decision", json=decision_command(saved), headers=native.HEADERS)
    assert failed.status_code == 503 and failed.json()["detail"] == "review_execution_unavailable"
    assert local_anthropic_decisions.get(saved["contract"]) is None
    monkeypatch.setattr(review_executions, "checkpoint", lambda _: None)
    receipt, command = decide(c, saved, url)
    assert c["http"].post(url + "/anthropic-native-owner-decision", json=command, headers=native.HEADERS).json() == receipt
    changed = c["http"].post(url + "/anthropic-native-owner-decision", json=command | {"request_id": "new-decision"}, headers=native.HEADERS)
    assert changed.status_code == 409
    with closing(connect_metadata_index()) as db:
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM paper_local_anthropic_decisions")


def test_settings_change_does_not_retarget_ready_request(local_case):
    c = local_case
    saved, url, _, _ = native.ready(c)
    c["settings"]["models"]["anthropic_api"] = "new-model"
    decide(c, saved, url)
    assert c["http"].get(url).json()["contract"] == saved["contract"]
    assert c["http"].get(c["root"] + "/anthropic-native-preview").json()["prepared"]["model"] == "new-model"
    assert c["resolutions"] == c["calls"] == []


def test_new_local_factory_is_explicit_and_default_app_stays_disabled(monkeypatch):
    from app.auth.config import clear_auth_settings_cache
    from app.local import create_local_app
    from app.main import create_app
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("APP_BASE_URL", native.ORIGIN)
    monkeypatch.setenv("XRML_ENVIRONMENT", "local")
    monkeypatch.setenv("XRML_SHARED_DEPLOYMENT", "0")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    clear_auth_settings_cache()
    assert create_app().state.paper_anthropic_native_runtime is None
    with TestClient(create_local_app()) as http:
        assert http.app.state.paper_anthropic_native_runtime is not None
    for name, value in (("XRML_ENVIRONMENT", "production"), ("XRML_SHARED_DEPLOYMENT", "1"), ("AUTH_MODE", "oidc")):
        with monkeypatch.context() as change:
            change.setenv(name, value)
            clear_auth_settings_cache()
            assert create_local_app().state.paper_anthropic_native_runtime is None
    clear_auth_settings_cache()


def test_duplicate_decisions_are_atomic_and_do_not_renew_authority(local_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db.bootstrap import connect_metadata_index
    c = local_case
    saved, url, _, _ = native.ready(c)
    body = decision_command(saved)
    barrier = Barrier(2)
    def submit():
        barrier.wait(10)
        response = c["http"].post(url + "/anthropic-native-owner-decision", json=body, headers=native.HEADERS)
        assert response.status_code == 200, response.text
        return response.json()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: submit(), range(2)))
    assert results[0] == results[1]
    with closing(connect_metadata_index(read_only=True)) as db:
        assert db.execute("SELECT count(*) FROM paper_local_anthropic_decisions").fetchone()[0] == 1
    c["app"].state.paper_anthropic_native_runtime = None
    assert c["http"].post(url + "/anthropic-native-owner-decision", json=body, headers=native.HEADERS).json() == results[0]
    assert c["resolutions"] == c["calls"] == []


def test_local_decision_rejects_other_topic_and_oidc_actor(local_case):
    from types import SimpleNamespace
    from app.auth.security import require_admin_user
    c = local_case
    saved, url, _, _ = native.ready(c)
    other = url.replace(c["source"][0], "different-topic")
    result = c["http"].post(other + "/anthropic-native-owner-decision", json=decision_command(saved), headers=native.HEADERS)
    assert result.status_code == 404, result.text
    c["app"].dependency_overrides[require_admin_user] = lambda: SimpleNamespace(user_id="someone", auth_source="oidc", is_admin=True)
    try:
        result = c["http"].post(url + "/anthropic-native-owner-decision", json=decision_command(saved), headers=native.HEADERS)
        assert result.status_code == 403, result.text
    finally:
        c["app"].dependency_overrides.clear()
    assert c["resolutions"] == c["calls"] == []


def test_document_v4_budget_survives_settings_change_through_manual_result_readback(local_case):
    from tests import test_review_document_plan as plans
    c = local_case
    with plans.client() as http:
        request = plans.request(http, c["source"]) | dict(model=native.MODEL)
        response = http.post(plans.root(c["source"]) + "/document-plans", json=request)
        assert response.status_code == 200, response.text
        plan = response.json()
        plan_url = plans.root(c["source"]) + "/document-plans/" + plan["plan_id"]
        response = http.post(plan_url + "/units/0/prepare")
        assert response.status_code == 200, response.text
        preparation = response.json()
    c["root"] = plans.root(c["source"]) + "/" + preparation["preparation_id"] + "/transport-executions"
    c["settings"]["models"]["anthropic_api"] = "different-saved-setting"
    saved, url, _, shown = native.ready(c)
    assert shown["prepared"]["schema_version"] == "PREPARED_REVIEW_REQUEST_V4"
    assert shown["prepared"]["model"] == native.MODEL
    assert shown["prepared"]["provider_request"]["max_output_tokens"] == request["max_output_tokens"]
    assert shown["prepared"]["packet"]["source_preparation"]["selection"]["chars"] > 12000
    decide(c, saved, url)
    offer = c["http"].get(url + "/anthropic-native-approval").json()["offer"]
    approved = dict(request_id="document-register", expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
        expected_authority_sha256=offer["record_sha256"])
    response = c["http"].post(url + "/anthropic-native-approval", json=approved, headers=native.HEADERS)
    assert response.status_code == 200, response.text
    assert c["calls"] == c["resolutions"] == []
    response = c["http"].post(url + "/anthropic-native-dispatch", json=approved | dict(request_id="document-dispatch", expected_sequence=1), headers=native.HEADERS)
    assert response.status_code == 200, response.text
    c["local"].wait_idle(15)
    final = c["http"].get(url).json()
    assert final["current"]["state"] == "RESULT_SAVED", final["current"]
    record = next(event["record"] for event in final["events"] if event["state"] == "RESULT_RECEIVED")
    assert record["validation"]["prepared_execution"]["schema_version"] == "PREPARED_REVIEW_RESULT_V4"
    assert record["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    assert len(c["calls"]) == len(c["resolutions"]) == 1
    with plans.client() as http:
        assert http.get(plan_url).json()["units"][0]["receipts"][0]["receipt_id"] == final["current"]["receipt_id"]


def test_app_key_save_after_start_enables_preview_without_restart_or_decryption(local_case, monkeypatch):
    from app.api.model_connections import router
    c = local_case
    c["app"].include_router(router)
    monkeypatch.setattr(model_credentials, "status", credential_status)
    monkeypatch.setattr(model_credentials, "available", lambda: True)
    def fake_encrypt(raw, identity, *, decrypt=False):
        assert identity == "anthropic_api" and not decrypt
        return b"synthetic-encrypted-fixture"
    monkeypatch.setattr(model_credentials, "_crypt", fake_encrypt)
    preview = c["http"].get(c["root"] + "/anthropic-native-preview").json()
    assert preview["enabled"] is False and preview["reason"] == "paper_anthropic_native_credential_unavailable"
    response = c["http"].put("/settings/model-connections/anthropic_api/credential",
        json=dict(expected_revision=0, api_key="synthetic-never-a-real-key"),
        headers={"Origin": native.ORIGIN, "X-XRML-Credential-Action": "1"})
    assert response.status_code == 200, response.text
    assert "synthetic-never-a-real-key" not in response.text
    preview = c["http"].get(c["root"] + "/anthropic-native-preview").json()
    assert preview["enabled"] is True and preview["manifest"]["credential_binding"]["revision"] == 1
    assert c["calls"] == c["resolutions"] == []


@pytest.mark.parametrize("fence", ["expired", "revoked"])
def test_local_manager_fences_new_dispatch_but_preserves_decision_readback(local_case, monkeypatch, fence):
    from app.repositories import review_authorities, review_transport_history
    from app.services.local_anthropic_app import LocalAnthropicAppRuntime
    c = local_case
    saved, url, _, _ = native.ready(c)
    receipt, decision = decide(c, saved, url)
    offer = c["http"].get(url + "/anthropic-native-approval").json()["offer"]
    approval = dict(request_id="fenced-register", expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
        expected_authority_sha256=offer["record_sha256"])
    response = c["http"].post(url + "/anthropic-native-approval", json=approval, headers=native.HEADERS)
    assert response.status_code == 200, response.text
    if fence == "expired":
        future = (datetime.fromisoformat(offer["expires_at"]) + timedelta(seconds=1)).isoformat()
        monkeypatch.setattr(review_transport_history, "iso_now_utc", lambda: future)
        monkeypatch.setattr(review_authorities, "iso_now_utc", lambda: future)
    else:
        response = c["http"].post(url + "/anthropic-native-revocation",
            json=approval | dict(request_id="fenced-revoke", expected_sequence=1), headers=native.HEADERS)
        assert response.status_code == 200, response.text
    c["local"].stop()
    replacement = LocalAnthropicAppRuntime()
    c["app"].state.paper_anthropic_native_runtime = replacement
    try:
        observed = c["http"].get(url + "/anthropic-native-status")
        assert observed.status_code == 200, observed.text
        assert observed.json()["enabled"] is False
        assert observed.json()["reason"] == "paper_review_authority_" + fence
        assert observed.json()["authority"]["validity"] == fence.upper()
        command = approval | dict(request_id="fenced-dispatch", expected_sequence=observed.json()["observed_sequence"])
        rejected = c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=native.HEADERS)
        assert rejected.status_code == 409 and rejected.json()["detail"] == "paper_review_authority_" + fence
        before = c["http"].get(url).json()
        assert not any(event["state"] in {"LEASED", "RUNNING", "RESULT_RECEIVED"} for event in before["events"])
        c["state"].update(stored=False, revision=8)
        replay = c["http"].post(url + "/anthropic-native-owner-decision", json=decision, headers=native.HEADERS)
        assert replay.status_code == 200 and replay.json() == receipt
        approval_replay = c["http"].post(url + "/anthropic-native-approval", json=approval, headers=native.HEADERS)
        assert approval_replay.status_code == 200, approval_replay.text
        assert approval_replay.json()["events"] == before["events"]
        assert c["http"].get(url).json()["events"] == before["events"]
        assert c["calls"] == c["resolutions"] == []
    finally:
        replacement.stop()

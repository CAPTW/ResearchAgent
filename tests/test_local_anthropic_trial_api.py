"""Actual trial routes and auth dependencies; synthetic inputs and MockTransport only."""
from concurrent.futures import ThreadPoolExecutor
import importlib
from threading import Barrier

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth import security, sessions
from app.auth.config import clear_auth_settings_cache, get_auth_settings
from app.auth.security_types import ProductUser
from app.domain.review_preparation import sha
from tests import test_auth_session_hardening as auth
from tests import test_local_anthropic_trial as prior
from tests.test_anthropic_messages import envelope
from tests.test_prepared_review import response

case = prior.case
ORIGIN = "http://127.0.0.1:8002"
ROOT = "/settings/model-connections/anthropic_api/first-trial"
HEADERS = {"Origin": ORIGIN, "X-XRML-Trial-Action": "1"}


def test_get_exposes_rejection_diagnostic_from_saved_capture_without_new_authority(http_case):
    import httpx
    import json
    c = http_case
    payload = response(c["contract"]).payload | dict(paper_id="x", source_text_sha256="x", cited_evidence_span_ids=[""])
    runtime = c["build"](transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope(json.dumps(payload)))))
    runtime.execute(prior.binding(runtime))
    original = runtime.path.read_bytes()
    c["app"].state.local_anthropic_trial = c["build"](resolve_key=lambda _: pytest.fail("GET must not resolve a credential"))
    result = c["client"].get(ROOT)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    value = result.json()["trial"]
    assert value["output_diagnostic"]["reason_codes"] == ["PAPER_ID_MISMATCH", "SOURCE_HASH_MISMATCH", "MISSING_CITATIONS"]
    assert value["state"] == "STRUCTURE_INVALID" and value["draft"] is None and not value["can_execute"]
    assert runtime.path.read_bytes() == original


def app_for(runtime=None, *, bootstrap=True):
    api = importlib.import_module("app.api.local_anthropic_trial")
    app = FastAPI()
    app.include_router(api.router)
    app.state.local_anthropic_trial = runtime
    app.state.runtime_bootstrap_ok = bootstrap
    return app


@pytest.fixture
def http_case(case, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    clear_auth_settings_cache()
    module, build, calls, raw, grant, contract = case
    runtime = build()
    app = app_for(runtime)
    try:
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
            yield dict(client=client, app=app, runtime=runtime, build=build, calls=calls, raw=raw,
                grant=grant, contract=contract, module=module,
                body={"expected_binding_sha256": prior.binding(runtime)})
    finally:
        clear_auth_settings_cache()


@pytest.mark.parametrize("runtime", [None, object()])
def test_unconfigured_trial_is_disabled_without_key_resolution_or_write(http_case, runtime):
    c = http_case
    c["app"].state.local_anthropic_trial = runtime
    result = c["client"].get(ROOT)
    assert result.status_code == 200
    assert result.json() == {"enabled": False, "reason": "anthropic_trial_disabled", "trial": None}
    assert result.headers["cache-control"] == "no-store"
    for operation in ("execute", "cancel"):
        result = c["client"].post(ROOT + "/" + operation, json=c["body"], headers=HEADERS)
        assert result.status_code == 409 and result.json()["detail"] == "anthropic_trial_disabled"
    assert c["client"].get(ROOT + "/capture").status_code == 409
    assert not c["calls"] and not c["runtime"].path.exists()


@pytest.mark.parametrize("suffix", ["", "/capture", "/execute", "/cancel"])
def test_bootstrap_failure_blocks_reads_and_mutations(http_case, suffix):
    c = http_case
    c["app"].state.runtime_bootstrap_ok = False
    result = c["client"].post(ROOT + suffix, headers=HEADERS, json=c["body"]) if suffix in {"/execute", "/cancel"} else c["client"].get(ROOT + suffix)
    assert result.status_code == 503
    assert result.json()["detail"] == "anthropic_trial_storage_unavailable"
    assert result.headers["cache-control"] == "no-store"
    assert not c["calls"] and not c["runtime"].path.exists()


@pytest.mark.parametrize("headers", [
    {}, {"Origin": ORIGIN}, {"X-XRML-Trial-Action": "1"},
    {"Origin": "null", "X-XRML-Trial-Action": "1"},
    {"Origin": "https://other.invalid", "X-XRML-Trial-Action": "1"},
    {"Origin": ORIGIN, "X-XRML-Credential-Action": "1"},
    {"Origin": ORIGIN, "X-XRML-Trial-Action": "true"},
])
def test_mutations_require_matching_origin_and_distinct_explicit_trial_intent(http_case, headers):
    c = http_case
    for operation in ("execute", "cancel"):
        assert c["client"].post(ROOT + "/" + operation, json=c["body"], headers=headers).status_code == 403
    assert not c["calls"] and not c["runtime"].path.exists()


@pytest.mark.parametrize("headers", [
    {"Origin": "https://other.invalid"}, {"Host": "other.invalid"},
    {"Forwarded": "for=127.0.0.1"}, {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-Host": "127.0.0.1"},
])
def test_wrong_origin_host_or_forwarding_cannot_read_or_execute(http_case, headers):
    c = http_case
    assert c["client"].get(ROOT, headers=headers).status_code == 403
    assert c["client"].get(ROOT + "/capture", headers=headers).status_code == 403
    assert c["client"].post(ROOT + "/execute", json=c["body"], headers={**HEADERS, **headers}).status_code == 403
    assert not c["calls"] and not c["runtime"].path.exists()


def test_remote_peer_and_proxy_trust_are_not_local_authority(http_case, monkeypatch):
    c = http_case
    with TestClient(c["app"], base_url=ORIGIN, client=("203.0.113.9", 2345)) as remote:
        assert remote.get(ROOT).status_code == 403
        assert remote.get(ROOT + "/capture").status_code == 403
        assert remote.post(ROOT + "/execute", json=c["body"], headers=HEADERS).status_code == 403
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    clear_auth_settings_cache()
    assert c["client"].get(ROOT).status_code == 403
    assert c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS).status_code == 403
    assert not c["calls"] and not c["runtime"].path.exists()


def test_real_signed_session_gate_does_not_promote_oidc_admin_to_the_local_trial_actor(http_case, monkeypatch, tmp_path):
    c = http_case
    monkeypatch.setattr(sessions, "_session_store_path", lambda: tmp_path / "synthetic-sessions.json")
    for key, value in auth._oidc_env().items():
        monkeypatch.setenv(key, value)
    clear_auth_settings_cache()
    settings = get_auth_settings()
    assert c["client"].get(ROOT).status_code == 401
    assert c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS).status_code == 401
    for identity, admin in (("reader", False), ("admin", True), ("local-internal-user", True)):
        user = ProductUser(identity, identity, None, "oidc", admin)
        c["client"].cookies.set(settings.session_cookie_name, security.encode_product_session_cookie(user, settings))
        assert c["client"].get(ROOT).status_code == 403
        assert c["client"].get(ROOT + "/capture").status_code == 403
        assert c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS).status_code == 403
    assert not c["calls"] and not c["runtime"].path.exists()


def test_disabled_mode_still_requires_the_exact_server_local_actor(http_case, monkeypatch):
    c = http_case
    monkeypatch.setattr(security, "get_local_product_user", lambda: ProductUser("other-local", "other", None, "disabled", True))
    result = c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS)
    assert result.status_code == 403 and result.json()["detail"] == "anthropic_trial_local_actor_required"
    assert not c["calls"] and not c["runtime"].path.exists()


@pytest.mark.parametrize("body", [
    {}, None, [], {"expected_binding_sha256": True}, {"expected_binding_sha256": "A" * 64},
    {"expected_binding_sha256": "f" * 64, "approved": True},
    {"expected_binding_sha256": "f" * 64, "proof": "synthetic-private-input"},
    {"expected_binding_sha256": "f" * 64, "actor_id": "local-internal-user"},
])
def test_strict_body_rejects_browser_authority_and_input_echoes(http_case, body):
    c = http_case
    result = c["client"].post(ROOT + "/execute", json=body, headers=HEADERS)
    assert result.status_code == 422
    assert result.json() == {"detail": "anthropic_trial_command_invalid"}
    assert "synthetic-private-input" not in result.text
    assert not c["calls"] and not c["runtime"].path.exists()


def test_command_size_operation_and_frozen_binding_fail_before_claim(http_case):
    c = http_case
    assert c["client"].post(ROOT + "/execute", content=b"x" * 1025, headers=HEADERS).status_code == 413
    assert c["client"].post(ROOT + "/approve", json=c["body"], headers=HEADERS).status_code == 404
    for operation in ("execute", "cancel"):
        result = c["client"].post(ROOT + "/" + operation, json={"expected_binding_sha256": "0" * 64}, headers=HEADERS)
        assert result.status_code == 409 and result.json()["detail"] == "anthropic_trial_binding_changed"
    assert not c["calls"] and not c["runtime"].path.exists()


def test_preview_execute_capture_and_replay_preserve_the_exact_server_binding(http_case):
    c = http_case
    preview = c["client"].get(ROOT)
    assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
    trial = preview.json()["trial"]
    assert trial["state"] == "PREPARED" and trial["attempts"] == 0 and trial["can_execute"]
    assert trial["source"] == c["grant"]["source"] and trial["credential_revision"] == 1
    assert not c["runtime"].path.exists() and not c["calls"]
    assert c["client"].get(ROOT + "/capture").status_code == 409
    result = c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    saved = result.json()
    assert saved["trial"]["state"] == "STRUCTURE_VALID" and saved["trial"]["attempts"] == 1
    assert saved["trial"]["scientific_support"] == "NOT_ASSESSED"
    assert saved["trial"]["full_document_review"] == "NOT_ESTABLISHED"
    capture = c["client"].get(ROOT + "/capture")
    assert capture.status_code == 200 and capture.headers["cache-control"] == "no-store"
    assert capture.json() == {"trial_id": c["grant"]["trial_id"],
        "binding_sha256": c["body"]["expected_binding_sha256"], "raw_response": c["raw"].decode(), "sha256": sha(c["raw"].decode())}
    assert "synthetic-trial-secret" not in result.text + capture.text
    before = c["runtime"].path.read_bytes()
    c["app"].state.local_anthropic_trial = c["build"](resolve_key=lambda _: pytest.fail("Durable replay must not resolve a key"))
    replay = c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS)
    assert replay.status_code == 200 and replay.json() == saved
    assert c["runtime"].path.read_bytes() == before and len(c["calls"]) == 1


def test_concurrent_duplicate_http_commands_consume_exactly_one_attempt(http_case):
    c = http_case
    barrier = Barrier(4)
    def execute(_):
        barrier.wait(timeout=10)
        return c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(execute, range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len(c["calls"]) == 1
    assert c["client"].get(ROOT).json()["trial"]["state"] == "STRUCTURE_VALID"
    assert all(response.json()["trial"]["attempts"] == 1 for response in responses)


def test_cancel_is_terminal_without_launch_and_other_trial_binding_cannot_select_a_capture(http_case, tmp_path):
    c = http_case
    other = c["build"](path=tmp_path / "other-trial.json", grant=dict(c["grant"], trial_id="other-fixture"))
    wrong = {"expected_binding_sha256": prior.binding(other)}
    assert c["client"].post(ROOT + "/cancel", json=wrong, headers=HEADERS).status_code == 409
    cancelled = c["client"].post(ROOT + "/cancel", json=c["body"], headers=HEADERS)
    assert cancelled.status_code == 200
    assert cancelled.json()["trial"]["state"] == "CANCELLED" and cancelled.json()["trial"]["attempts"] == 0
    assert c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS).json() == cancelled.json()
    c["app"].state.local_anthropic_trial = other
    assert c["client"].get(ROOT + "/capture", params={"trial_id": c["grant"]["trial_id"]}).status_code == 409
    assert not c["calls"] and not other.path.exists()


@pytest.mark.parametrize("failure,status,code", [
    (ValueError("model_credentials_changed"), 409, "model_credentials_changed"),
    (ValueError("sensitive synthetic detail"), 503, "anthropic_trial_state_unavailable"),
    (RuntimeError("sensitive synthetic detail"), 503, "anthropic_trial_state_unavailable"),
])
def test_resolution_failures_have_safe_api_errors_without_consuming_a_claim(http_case, failure, status, code):
    c = http_case
    def failed(_):
        raise failure
    c["app"].state.local_anthropic_trial = c["build"](resolve_key=failed)
    result = c["client"].post(ROOT + "/execute", json=c["body"], headers=HEADERS)
    assert result.status_code == status and result.json() == {"detail": code}
    assert result.headers["cache-control"] == "no-store" and "sensitive" not in result.text
    assert not c["calls"] and not c["runtime"].path.exists()

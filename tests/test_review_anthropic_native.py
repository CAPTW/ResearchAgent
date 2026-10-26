"""Native composition tests replace only credential/send boundaries; no real key/network."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
from threading import Barrier, Event

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth.config import clear_auth_settings_cache
from app.domain import review_authority
from app.domain.review_preparation import encoded, sha
from app.services import model_credentials
from app.services.providers import anthropic_prepared_review
from app.services.provider_output_class_proof import compute_provider_proof_id
from tests import test_review_offline_api as previous, test_review_authority as proofs, test_prepared_review as drafts

range_source = previous.range_source
seeded = previous.seeded
ORIGIN = "http://127.0.0.1:8013"
HEADERS = {"Origin": ORIGIN, "X-XRML-Paper-Anthropic-Action": "1"}
MODEL = "synthetic-native-model"
TRUSTED = frozenset({("synthetic-native-issuer", "synthetic-policy-e\u0301")})


def service():
    return importlib.import_module("app.services.review_anthropic_native")


def test_default_app_has_disabled_native_preview_route(range_source, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    clear_auth_settings_cache()
    with previous.client() as existing:
        _, root = previous.prepared(existing, range_source)
    try:
        with TestClient(create_app(), base_url=ORIGIN, client=("127.0.0.1", 54321)) as client:
            response = client.get(root + "/transport-executions/anthropic-native-preview")
            assert response.status_code == 200, response.text
            assert response.json()["enabled"] is False
            assert response.json()["execution_allowed"] is False
    finally:
        clear_auth_settings_cache()


def app_for(runtime=None):
    app = FastAPI()
    app.include_router(importlib.import_module("app.api.review_anthropic_native").router)
    # Common readback/control routes are independent from native authorization.
    app.include_router(importlib.import_module("app.api.review_execution").router)
    app.state.runtime_bootstrap_ok = True
    app.state.paper_anthropic_native_runtime = runtime
    return app


def authority_record(value):
    now = datetime.now(timezone.utc)
    approved = proofs.grant(value)
    identity = replace(approved.proof.identity, surface_id="api")
    proof = replace(approved.proof, identity=identity, proof_id=compute_provider_proof_id(identity),
        accepted_at=now, expires_at=now + timedelta(minutes=3))
    approved = approved.model_copy(update=dict(proof=proof, granted_at=now, expires_at=now + timedelta(minutes=2)))
    return review_authority.Record(grant=approved, issuer_id="synthetic-native-issuer", decision_ref="fixture-only",
        registered_at=now, expected_sequence=0).model_dump(mode="json")


@pytest.fixture
def native_case(range_source, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    clear_auth_settings_cache()
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
    vault = (tmp_path / "private" / "synthetic.json").resolve()
    monkeypatch.setattr(model_credentials, "path", lambda: vault)
    state = dict(revision=7, stored=True, storage_available=True)
    calls, resolutions, offered = [], [], {}
    def source(value):
        return deepcopy(offered.get(value["execution_id"]))
    def metadata(_, *, credential_path=None):
        assert credential_path == vault
        return dict(state)
    monkeypatch.setattr(model_credentials, "status", metadata)
    def resolve(connector, revision, *, credential_path=None):
        assert connector == "anthropic_api" and revision == 7
        assert credential_path == vault
        resolutions.append(1)
        return "synthetic-not-a-real-key"
    monkeypatch.setattr(model_credentials, "resolve_stored", resolve)
    monkeypatch.setattr(model_credentials, "resolve", lambda *_: pytest.fail("no environment fallback"))
    def send(body, **kwargs):
        import json
        if kwargs.get("before_send") is not None:
            kwargs["before_send"]()
        assert kwargs["api_key"] == "synthetic-not-a-real-key" and kwargs["transport"] is None
        calls.append(body)
        packet = json.loads(json.loads(body)["messages"][0]["content"])
        from app.domain import prepared_review
        frozen = prepared_review.prepare(packet, provider_name="anthropic", model=MODEL)
        response = drafts.response(frozen)
        response.provider_name = "anthropic"
        return response
    monkeypatch.setattr(anthropic_prepared_review, "send", send)
    runtime = service().AnthropicNativeRuntime(model=MODEL, credential_revision=7, timeout_seconds=60,
        trusted_issuers=TRUSTED, source=source)
    app = app_for(runtime)
    try:
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 54321)) as client:
            yield dict(http=client, app=app, runtime=runtime, root=root + "/transport-executions", preparation=preparation,
                state=state, calls=calls, resolutions=resolutions, offered=offered, source=range_source, vault=vault)
    finally:
        runtime.stop()
        clear_auth_settings_cache()


def ready(c):
    result = c["http"].get(c["root"] + "/anthropic-native-preview")
    assert result.status_code == 200, result.text
    preview = result.json()
    command = dict(request_id="native-prepare", expected_preparation_digest=preview["preparation_digest"],
        expected_binding_sha256=preview["binding_sha256"], acknowledge_unknown=False)
    result = c["http"].post(c["root"] + "/anthropic-native-requests", json=command, headers=HEADERS)
    assert result.status_code == 200, result.text
    saved = result.json()
    return saved, c["root"] + "/" + saved["contract"]["execution_id"], command, preview


def approve(c, saved, url):
    record = authority_record(saved["contract"])
    c["offered"][saved["contract"]["execution_id"]] = record
    result = c["http"].get(url + "/anthropic-native-approval")
    assert result.status_code == 200, result.text
    assert result.json()["offer"]["record_sha256"] == sha(encoded(record))
    command = dict(request_id="native-approve", expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
        expected_authority_sha256=sha(encoded(record)))
    result = c["http"].post(url + "/anthropic-native-approval", json=command, headers=HEADERS)
    assert result.status_code == 200, result.text
    return result.json(), command


def test_native_default_deny_does_not_resolve_key_or_enable_from_browser(native_case, monkeypatch):
    c = native_case
    c["app"].state.paper_anthropic_native_runtime = None
    monkeypatch.setattr(model_credentials, "status", lambda *args, **kwargs: pytest.fail("disabled preview cannot read key metadata"))
    result = c["http"].get(c["root"] + "/anthropic-native-preview")
    assert result.status_code == 200, result.text
    preview = result.json()
    assert not preview["enabled"] and not preview["execution_allowed"] and preview["manifest"] is None
    body = dict(request_id="disabled", expected_preparation_digest=preview["preparation_digest"], expected_binding_sha256="a" * 64)
    assert c["http"].post(c["root"] + "/anthropic-native-requests", json=body, headers=HEADERS).status_code == 409
    for key in ("api_key", "model", "proof", "issuer", "approved", "credential_revision"):
        result = c["http"].post(c["root"] + "/anthropic-native-requests", json=body | {key: "never-used"}, headers=HEADERS)
        assert result.status_code == 422 and "never-used" not in result.text
    assert c["resolutions"] == c["calls"] == []


def test_native_preview_approval_dispatch_and_exact_source_free_readback(native_case):
    c = native_case
    saved, url, prepare_command, preview = ready(c)
    assert preview["manifest"]["credential_binding"] == dict(connector_id="anthropic_api", revision=7, storage="LOCAL_DPAPI")
    assert preview["manifest"]["execution_surface"] == "NATIVE_HTTP" and preview["execution_allowed"] is False
    assert saved["current"]["state"] == "READY"
    assert c["http"].get(url + "/anthropic-native-approval").json()["reason"] == "paper_approval_source_unavailable"
    approved, approval_command = approve(c, saved, url)
    assert approved["current"]["state"] == "APPROVED" and c["resolutions"] == c["calls"] == []
    assert c["http"].get(url + "/anthropic-native-status").json()["enabled"] is True
    assert c["resolutions"] == []
    dispatch = approval_command | dict(request_id="native-dispatch", expected_sequence=1)
    result = c["http"].post(url + "/anthropic-native-dispatch", json=dispatch, headers=HEADERS)
    assert result.status_code == 200, result.text
    c["runtime"].wait_idle(15)
    final = c["http"].get(url).json()
    assert final["current"]["state"] == "RESULT_SAVED", final["current"]
    received = next(event["record"] for event in final["events"] if event["state"] == "RESULT_RECEIVED")
    assert received["validation"]["execution"]["manifest"] == preview["manifest"]
    assert len(c["calls"]) == len(c["resolutions"]) == 1
    c["offered"].clear()
    c["state"].update(stored=False, revision=8)
    c["source"][2].unlink()
    c["app"].state.paper_anthropic_native_runtime = None
    for endpoint, body in ((c["root"] + "/anthropic-native-requests", prepare_command),
            (url + "/anthropic-native-approval", approval_command), (url + "/anthropic-native-dispatch", dispatch)):
        replay = c["http"].post(endpoint, json=body, headers=HEADERS)
        assert replay.status_code == 200, replay.text
        assert replay.json()["events"] == final["events"]
    assert not c["http"].get(url + "/anthropic-native-status").json()["enabled"]
    assert len(c["calls"]) == len(c["resolutions"]) == 1


@pytest.mark.parametrize("change", ["revision", "deleted", "unavailable"])
def test_native_changed_credential_stops_before_claim_and_plaintext(native_case, change):
    c = native_case
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    c["state"].update({"revision": 8} if change == "revision" else {"stored": False} if change == "deleted" else {"storage_available": False})
    assert not c["http"].get(url + "/anthropic-native-status").json()["enabled"]
    command = approved | dict(request_id="dispatch", expected_sequence=1)
    result = c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS)
    assert result.status_code == 409, result.text
    assert c["http"].get(url).json()["current"]["state"] == "APPROVED"
    assert c["calls"] == c["resolutions"] == []


@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_native_decryption_race_fences_before_http_and_preserves_replay(native_case, monkeypatch, action):
    c = native_case
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    started, release = Event(), Event()
    def resolve(*args, **kwargs):
        c["resolutions"].append(1)
        started.set()
        assert release.wait(15)
        return "synthetic-not-a-real-key"
    monkeypatch.setattr(model_credentials, "resolve_stored", resolve)
    command = approved | dict(request_id="dispatch", expected_sequence=1)
    try:
        assert c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS).status_code == 200
        assert started.wait(10)
        current = c["http"].get(url).json()
        if action == "cancel":
            body = dict(request_id=action, expected_sequence=current["current"]["sequence"], action="cancel")
            response = c["http"].post(url + "/controls", json=body)
        else:
            c["app"].state.paper_anthropic_native_runtime = None
            c["offered"].clear()
            body = approved | dict(request_id=action, expected_sequence=current["current"]["sequence"])
            response = c["http"].post(url + "/anthropic-native-revocation", json=body, headers=HEADERS)
        assert response.status_code == 200, response.text
    finally:
        release.set()
        c["runtime"].wait_idle(15)
    final = c["http"].get(url).json()
    assert final["current"]["state"] == {"cancel": "CANCELLED", "revoke": "REVOKED"}[action]
    assert final["current"]["receipt_id"] is None and not c["calls"]
    assert c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS).status_code == 200


@pytest.mark.parametrize("failure", ["transport", "resolve", "echo"])
def test_native_failed_or_secret_output_is_not_published_or_retried(native_case, monkeypatch, failure):
    c = native_case
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    original = anthropic_prepared_review.send
    def send(*args, **kwargs):
        if failure == "transport":
            c["calls"].append(1)
            raise RuntimeError("synthetic-not-a-real-key")
        response = original(*args, **kwargs)
        response.payload["contribution_summary"] = "synthetic-not-a-real-key"
        response.raw_text = encoded(response.payload)
        return response
    if failure == "resolve":
        def fail(*args, **kwargs):
            c["resolutions"].append(1)
            raise ValueError("synthetic-not-a-real-key")
        monkeypatch.setattr(model_credentials, "resolve_stored", fail)
    else:
        monkeypatch.setattr(anthropic_prepared_review, "send", send)
    command = approved | dict(request_id="dispatch", expected_sequence=1)
    assert c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS).status_code == 200
    c["runtime"].wait_idle(15)
    final = c["http"].get(url).json()
    assert final["current"]["receipt_id"] is None and "synthetic-not-a-real-key" not in encoded(final)
    assert final["current"]["state"] in {"INTERRUPTED", "FAILED"}
    replay = c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS)
    assert replay.status_code == 200 and replay.json()["events"] == final["events"]
    assert len(c["resolutions"]) == 1 and len(c["calls"]) <= 1


@pytest.mark.parametrize("headers", [{}, {"Origin": ORIGIN}, {"Origin": "https://other.invalid", "X-XRML-Paper-Anthropic-Action": "1"}])
def test_native_post_requires_local_origin_and_specific_intent(native_case, headers):
    c = native_case
    saved, url, _, _ = ready(c)
    response = c["http"].post(url + "/anthropic-native-dispatch", json=dict(request_id="x", expected_sequence=0,
        expected_contract_sha256=saved["contract_sha256"], expected_authority_sha256="a" * 64), headers=headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert c["calls"] == c["resolutions"] == []


def test_native_untrusted_expired_or_changed_offer_never_registers(native_case):
    c = native_case
    saved, url, _, _ = ready(c)
    original = authority_record(saved["contract"])
    for change in ("issuer", "expired", "hash"):
        record = deepcopy(original)
        if change == "issuer":
            record["issuer_id"] = "untrusted"
        elif change == "expired":
            record["grant"]["expires_at"] = record["grant"]["granted_at"]
        c["offered"][saved["contract"]["execution_id"]] = record
        body = dict(request_id=change, expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
            expected_authority_sha256="0" * 64 if change == "hash" else sha(encoded(record)))
        result = c["http"].post(url + "/anthropic-native-approval", json=body, headers=HEADERS)
        assert result.status_code in {409, 503}, result.text
        assert c["http"].get(url).json()["current"]["state"] == "READY"
    assert c["resolutions"] == c["calls"] == []


def test_native_runtime_pins_metadata_and_decryption_to_same_vault(native_case, monkeypatch, tmp_path):
    c = native_case
    monkeypatch.setattr(model_credentials, "path", lambda: tmp_path / "different-vault.json")
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    result = c["http"].post(url + "/anthropic-native-dispatch", json=approved | dict(request_id="dispatch", expected_sequence=1), headers=HEADERS)
    assert result.status_code == 200, result.text
    c["runtime"].wait_idle(15)
    assert c["http"].get(url).json()["current"]["state"] == "RESULT_SAVED"
    assert len(c["resolutions"]) == len(c["calls"]) == 1


@pytest.mark.parametrize("trusted", [frozenset({("xrml-anthropic-mock-issuer-v1", "safe")}),
    frozenset({("safe", "PAPER_ANTHROPIC_MOCK_ONLY_V1")})])
def test_native_runtime_cannot_trust_mock_issuer_or_policy(trusted):
    with pytest.raises(ValueError, match="configuration_invalid"):
        service().AnthropicNativeRuntime(model=MODEL, credential_revision=7, trusted_issuers=trusted)


@pytest.mark.parametrize("action", ["cancel", "revoke", "revision", "expiry"])
def test_client_construction_delay_rechecks_fences_before_http(native_case, monkeypatch, action):
    import httpx
    from app.repositories import review_transport_history
    from app.services.providers import anthropic_messages
    from tests.test_anthropic_messages import envelope
    c = native_case
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    started, release = Event(), Event()
    client_type = httpx.Client
    def respond(request):
        c["calls"].append(1)
        return httpx.Response(200, json=envelope(drafts.response(saved["contract"]["prepared"]).raw_text))
    def slow_client(**kwargs):
        assert kwargs["transport"] is None and kwargs["trust_env"] is False
        result = client_type(**(kwargs | {"transport": httpx.MockTransport(respond)}))
        started.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr(anthropic_prepared_review, "send", anthropic_messages.send)
    monkeypatch.setattr(anthropic_messages.httpx, "Client", slow_client)
    command = approved | dict(request_id="dispatch", expected_sequence=1)
    try:
        result = c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS)
        assert result.status_code == 200, result.text
        assert started.wait(10)
        current = c["http"].get(url).json()
        if action == "cancel":
            result = c["http"].post(url + "/controls", json=dict(request_id="cancel", expected_sequence=current["current"]["sequence"], action="cancel"))
            assert result.status_code == 200, result.text
        elif action == "revoke":
            result = c["http"].post(url + "/anthropic-native-revocation",
                json=approved | dict(request_id="revoke", expected_sequence=current["current"]["sequence"]), headers=HEADERS)
            assert result.status_code == 200, result.text
        elif action == "revision":
            c["state"]["revision"] = 8
        else:
            future = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
            monkeypatch.setattr(review_transport_history, "iso_now_utc", lambda: future)
    finally:
        release.set()
        c["runtime"].wait_idle(15)
    assert c["calls"] == []
    final = c["http"].get(url).json()
    assert final["current"]["receipt_id"] is None
    assert c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS).status_code == 200


def test_native_api_rejects_remote_host_proxy_bootstrap_and_oversize(native_case, monkeypatch):
    c = native_case
    preview = c["root"] + "/anthropic-native-preview"
    missing = c["http"].get(preview.replace(c["preparation"]["topic_id"], "missing-topic"))
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    for headers in ({"Host": "remote.invalid"}, {"Origin": "https://other.invalid"},
            {"Forwarded": "for=127.0.0.1"}, {"X-Forwarded-Host": "localhost"}, {"X-Forwarded-For": "127.0.0.1"}):
        assert c["http"].get(preview, headers=headers).status_code == 403
    with TestClient(c["app"], base_url=ORIGIN, client=("203.0.113.1", 12345)) as remote:
        assert remote.get(preview).status_code == 403
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    clear_auth_settings_cache()
    assert c["http"].get(preview).status_code == 403
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    clear_auth_settings_cache()
    c["app"].state.runtime_bootstrap_ok = False
    assert c["http"].get(preview).status_code == 503
    c["app"].state.runtime_bootstrap_ok = True
    endpoint = c["root"] + "/anthropic-native-requests"
    response = c["http"].post(endpoint, content="x" * 4097, headers=HEADERS)
    assert response.status_code == 413 and "xxxx" not in response.text
    for body in ("{", '{"api_key":"never-reflect-this"}'):
        response = c["http"].post(endpoint, content=body, headers=HEADERS)
        assert response.status_code == 422 and "never-reflect-this" not in response.text
    assert c["resolutions"] == c["calls"] == []


def test_native_api_oidc_admin_and_nonadmin_are_not_local_authority(native_case, monkeypatch, tmp_path):
    from app.auth import security, sessions
    from app.auth.config import get_auth_settings
    from app.auth.security_types import ProductUser
    from tests.test_auth_session_hardening import _oidc_env
    c = native_case
    monkeypatch.setattr(sessions, "_session_store_path", lambda: tmp_path / "synthetic-sessions.json")
    for key, value in _oidc_env().items():
        monkeypatch.setenv(key, value)
    clear_auth_settings_cache()
    settings = get_auth_settings()
    endpoint = c["root"] + "/anthropic-native-preview"
    assert c["http"].get(endpoint).status_code == 401
    for identity, admin in (("reader", False), ("admin", True), ("local-internal-user", True)):
        user = ProductUser(identity, identity, None, "oidc", admin)
        c["http"].cookies.set(settings.session_cookie_name, security.encode_product_session_cookie(user, settings))
        assert c["http"].get(endpoint).status_code == 403
    assert c["resolutions"] == c["calls"] == []


def test_native_concurrent_dispatch_claims_one_decryption_and_http_attempt(native_case):
    c = native_case
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    command = approved | dict(request_id="same-dispatch", expected_sequence=1)
    barrier = Barrier(4)
    def submit(_):
        barrier.wait()
        return c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert [result.status_code for result in results] == [200] * 4
    c["runtime"].wait_idle(15)
    final = c["http"].get(url).json()
    assert final["current"]["state"] == "RESULT_SAVED"
    assert [event["state"] for event in final["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
    assert len(c["resolutions"]) == len(c["calls"]) == 1


def test_native_receipt_rollback_recovers_without_runtime_source_key_or_resend(native_case, monkeypatch):
    from app.repositories import review_executions
    from tests.test_review_anthropic_execution import receipt_count
    c = native_case
    saved, url, _, _ = ready(c)
    _, approved = approve(c, saved, url)
    def fail(phase):
        if phase == "after_receipt":
            raise RuntimeError("synthetic-publication-failure")
    monkeypatch.setattr(review_executions, "checkpoint", fail)
    command = approved | dict(request_id="dispatch", expected_sequence=1)
    assert c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS).status_code == 200
    c["runtime"].wait_idle(15)
    received = c["http"].get(url).json()
    assert received["current"]["state"] == "RESULT_RECEIVED" and receipt_count() == 0
    assert len(c["resolutions"]) == len(c["calls"]) == 1
    c["app"].state.paper_anthropic_native_runtime = None
    c["offered"].clear()
    c["state"].update(stored=False, revision=8)
    c["source"][2].unlink()
    monkeypatch.setattr(review_executions, "checkpoint", lambda _: None)
    body = dict(request_id="save-existing", expected_sequence=received["current"]["sequence"], action="save_result")
    result = c["http"].post(url + "/controls", json=body)
    assert result.status_code == 200, result.text
    final = result.json()
    assert final["current"]["state"] == "RESULT_SAVED" and receipt_count() == 1
    assert c["http"].post(url + "/controls", json=body).json() == final
    assert c["http"].post(url + "/anthropic-native-dispatch", json=command, headers=HEADERS).status_code == 200
    assert len(c["resolutions"]) == len(c["calls"]) == 1

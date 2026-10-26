"""Disposable encrypted credentials; never real API keys or Provider traffic."""
from concurrent.futures import ThreadPoolExecutor
import base64
import importlib
import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app.auth import security
from app.auth.config import AuthSettings
from app.services import model_connections


@pytest.fixture
def vault(tmp_path, monkeypatch):
    module = importlib.import_module("app.services.model_credentials")
    monkeypatch.setattr(model_connections, "STATE_PATH", tmp_path / "connections.json")
    for name in ("OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY", "XRML_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    return module


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI roundtrip")
def test_native_encrypted_save_reopen_replace_delete_and_environment_fallback(vault, monkeypatch):
    key = "sk-ant-synthetic-do-not-use-credentials-test"
    assert vault.status("anthropic_api")["source"] == "none"
    assert not vault.path().exists()
    saved = vault.save("anthropic_api", 0, key)
    assert saved["revision"] == 1 and saved["source"] == "stored"
    assert not saved["execution_allowed"]
    assert key not in vault.path().read_text(encoding="utf-8")
    assert vault.resolve("anthropic_api") == key
    assert vault.save("anthropic_api", 0, key) == saved
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-environment-key")
    assert vault.resolve("anthropic_api") == key
    deleted = vault.delete("anthropic_api", 1)
    assert deleted["source"] == "environment" and not deleted["stored"]
    assert vault.resolve("anthropic_api") == "synthetic-environment-key"
    assert vault.delete("anthropic_api", 1) == deleted
    with pytest.raises(ValueError, match="credentials_changed"):
        vault.save("anthropic_api", 1, key)
    assert vault.status("anthropic_api") == deleted


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI pinned stored credential")
def test_resolve_stored_decrypts_exact_snapshot_without_environment_fallback(vault, monkeypatch):
    key = "synthetic-stored-only-key"
    vault.save("anthropic_api", 0, key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-other-account-key")
    read = vault._read
    observed = []
    def one_snapshot(location=None):
        observed.append(True)
        assert len(observed) == 1
        return read(location)
    monkeypatch.setattr(vault, "_read", one_snapshot)
    before = vault.path().read_bytes()
    assert vault.resolve_stored("anthropic_api", 1) == key
    assert observed == [True] and vault.path().read_bytes() == before
    assert key not in before.decode("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI credential revision fencing")
@pytest.mark.parametrize("change", ["changed", "deleted", "missing", "environment-only"])
def test_resolve_stored_rejects_stale_or_missing_key_without_decrypting_another_value(vault, monkeypatch, change):
    if change in {"changed", "deleted"}:
        vault.save("anthropic_api", 0, "synthetic-original-key")
        if change == "changed":
            vault.save("anthropic_api", 1, "synthetic-replacement-key")
        else:
            vault.delete("anthropic_api", 1)
    if change != "missing":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-must-not-fallback")
    def forbidden_decode(*args):
        pytest.fail("A rejected stored revision must not decrypt a key")
    monkeypatch.setattr(vault, "_decode", forbidden_decode)
    with pytest.raises(ValueError, match="^model_credentials_changed$"):
        vault.resolve_stored("anthropic_api", 1)
    if change in {"missing", "environment-only"}:
        assert not vault.path().exists()


@pytest.mark.parametrize("revision", [True, False, 0, -1, 1.0, "1", None])
def test_resolve_stored_rejects_invalid_revision_before_read(vault, monkeypatch, revision):
    def forbidden_read():
        pytest.fail("An invalid revision must not read the vault")
    monkeypatch.setattr(vault, "_read", forbidden_read)
    with pytest.raises(ValueError, match="^model_credentials_invalid$"):
        vault.resolve_stored("anthropic_api", revision)


def test_resolve_stored_rejects_unknown_connector_before_read(vault, monkeypatch):
    def forbidden_read():
        pytest.fail("An unknown connector must not read the vault")
    monkeypatch.setattr(vault, "_read", forbidden_read)
    with pytest.raises(ValueError, match="^model_credentials_connector_invalid$"):
        vault.resolve_stored("codex_subscription", 1)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI pinned credential integrity")
def test_resolve_stored_corrupt_cipher_never_uses_environment(vault, monkeypatch):
    vault.save("anthropic_api", 0, "synthetic-original-key")
    raw = json.loads(vault.path().read_text(encoding="utf-8"))
    raw["entries"]["anthropic_api"]["ciphertext"] = "c3ludGhldGljLW5vdC1hLURQQVBJLWJsb2I="
    vault.path().write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-must-not-fallback")
    with pytest.raises(vault.CredentialUnavailable, match="^model_credentials_unavailable$"):
        vault.resolve_stored("anthropic_api", 1)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI explicit stored-key location")
def test_resolve_stored_explicit_location_reads_only_its_snapshot_and_preserves_default(vault, monkeypatch, tmp_path):
    vault.save("anthropic_api", 0, "synthetic-default-key")
    default_location = vault.path()
    before = default_location.read_bytes()
    alternate = tmp_path / "separate-owner-workspace" / "private" / "credentials.json"
    alternate.parent.mkdir(parents=True)
    key = "synthetic-alternate-key"
    cipher = base64.b64encode(vault._crypt(key.encode("ascii"), "anthropic_api")).decode("ascii")
    alternate.write_text(json.dumps({"schema_version": vault.SCHEMA, "entries": {
        "anthropic_api": {"revision": 1, "ciphertext": cipher}}}), encoding="utf-8")
    alternate_before = alternate.read_bytes()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-environment-key")
    read, observed = vault._read, []
    def one_snapshot(location=None):
        observed.append(location)
        assert observed == [alternate]
        return read(location)
    monkeypatch.setattr(vault, "_read", one_snapshot)
    assert vault.resolve_stored("anthropic_api", 1, credential_path=alternate) == key
    assert vault.path() == default_location and default_location.read_bytes() == before
    assert alternate.read_bytes() == alternate_before and key not in alternate_before.decode("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI explicit-location isolation")
@pytest.mark.parametrize("entry", [None, {"revision": 1, "ciphertext": None}, {"revision": 2, "ciphertext": "c3ludGhldGlj"}])
def test_resolve_stored_explicit_missing_deleted_or_changed_never_uses_default_or_environment(vault, monkeypatch, tmp_path, entry):
    vault.save("anthropic_api", 0, "synthetic-default-key")
    before = vault.path().read_bytes()
    alternate = tmp_path / "alternate.json"
    if entry is not None:
        alternate.write_text(json.dumps({"schema_version": vault.SCHEMA, "entries": {"anthropic_api": entry}}), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-must-not-fallback")
    def forbidden_decode(*args):
        pytest.fail("A missing explicit revision must not decrypt a default or environment key")
    monkeypatch.setattr(vault, "_decode", forbidden_decode)
    with pytest.raises(ValueError, match="^model_credentials_changed$"):
        vault.resolve_stored("anthropic_api", 1, credential_path=alternate)
    assert vault.path().read_bytes() == before
    assert alternate.exists() is (entry is not None)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI roundtrip")
def test_concurrent_distinct_keys_conflict_without_lost_update(vault):
    def write(key):
        try:
            return vault.save("anthropic_api", 0, key)
        except ValueError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(write, [f"synthetic-key-{i}" for i in range(4)]))
    assert sum(isinstance(item, dict) for item in values) == 1
    assert values.count("conflict") == 3
    assert vault.status("anthropic_api")["revision"] == 1


def test_unavailable_os_never_writes_plaintext(vault, monkeypatch):
    monkeypatch.setattr(vault, "available", lambda: False)
    assert not vault.status("anthropic_api")["storage_available"]
    with pytest.raises(vault.CredentialUnavailable):
        vault.save("anthropic_api", 0, "synthetic-test-key")
    assert not vault.path().exists()


@pytest.fixture
def client(vault, monkeypatch):
    api = importlib.import_module("app.api.model_connections")
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(security, "get_auth_settings", lambda: AuthSettings(auth_mode="disabled"))
    with TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 12345)) as value:
        yield value


HEADERS = {"Origin": "http://127.0.0.1:8002", "X-XRML-Credential-Action": "1"}
URL = "/settings/model-connections/anthropic_api/credential"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI API integration")
def test_settings_key_write_is_redacted_separate_from_model_choice_and_execution(client, vault):
    key = "sk-ant-synthetic-settings-key"
    result = client.put(URL, headers=HEADERS, json={"expected_revision": 0, "api_key": key})
    assert result.status_code == 200
    assert result.json()["stored"] and key not in result.text
    assert client.get(URL).json() == result.json()
    prefs = client.get("/settings/model-connections").json()
    assert prefs["default_connector_id"] == "codex_subscription"
    assert not prefs["execution_allowed"] and key not in json.dumps(prefs)
    assert not model_connections.STATE_PATH.exists()
    assert client.request("DELETE", URL, headers=HEADERS, json={"expected_revision": 1}).status_code == 200
    assert client.get(URL).json()["source"] == "none"


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.invalid", "X-XRML-Credential-Action": "1"},
                                    {"Origin": "null", "X-XRML-Credential-Action": "1"},
                                    {"Origin": "http://127.0.0.1:8002"}])
def test_cross_origin_and_missing_intent_cannot_write(client, vault, headers):
    result = client.put(URL, headers=headers, json={"expected_revision": 0, "api_key": "synthetic-secret"})
    assert result.status_code == 403
    assert not vault.path().exists()


def test_invalid_bodies_do_not_echo_secret(client, vault):
    key = "synthetic-private-invalid-input"
    for body in ({"expected_revision": 0, "api_key": key, "extra": key},
                 {"expected_revision": key, "api_key": key}, {"expected_revision": 0, "api_key": [key]}):
        result = client.put(URL, headers=HEADERS, json=body)
        assert result.status_code == 422 and key not in result.text
    assert not vault.path().exists()


def test_remote_host_forwarded_peer_and_unknown_connector_rejected(client, vault):
    for headers in ({**HEADERS, "Host": "evil.invalid"}, {**HEADERS, "X-Forwarded-For": "127.0.0.1"}):
        result = client.put(URL, headers=headers, json={"expected_revision": 0, "api_key": "synthetic-key"})
        assert result.status_code == 403
    assert client.get(URL.replace("anthropic_api", "codex_subscription")).status_code == 422
    result = client.put(URL, headers=HEADERS, content=b"x" * 8193)
    assert result.status_code == 413
    assert not vault.path().exists()


def test_remote_peer_and_proxy_trust_are_not_local_authority(client, vault, monkeypatch):
    api = importlib.import_module("app.api.model_connections")
    with TestClient(client.app, base_url="http://127.0.0.1:8002", client=("203.0.113.4", 12345)) as remote:
        assert remote.get(URL).status_code == 403
        assert remote.put(URL, headers=HEADERS, json={"expected_revision": 0, "api_key": "synthetic-key"}).status_code == 403
    monkeypatch.setattr(api, "get_auth_settings", lambda: AuthSettings(auth_mode="disabled", trust_proxy_headers=True))
    assert client.get(URL).status_code == 403
    assert client.put(URL, headers=HEADERS, json={"expected_revision": 0, "api_key": "synthetic-key"}).status_code == 403
    assert not vault.path().exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI rollback")
def test_failed_replace_preserves_ciphertext_and_deleted_key_cannot_be_replayed(vault, monkeypatch):
    from app.repositories import shared_state
    vault.save("anthropic_api", 0, "synthetic-first-key")
    before = vault.path().read_bytes()
    def fail(*args, **kwargs):
        raise shared_state.SharedStateWriteFailed("model_credentials")
    with monkeypatch.context() as patch:
        patch.setattr(shared_state, "_atomic_write", fail)
        with pytest.raises(vault.CredentialUnavailable):
            vault.save("anthropic_api", 1, "synthetic-second-key")
    assert vault.path().read_bytes() == before
    assert vault.resolve("anthropic_api") == "synthetic-first-key"
    vault.delete("anthropic_api", 1)
    with pytest.raises(ValueError, match="credentials_changed"):
        vault.save("anthropic_api", 0, "synthetic-first-key")


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DPAPI tamper detection")
def test_corrupt_or_wrong_connector_cipher_never_falls_back_to_environment(vault, monkeypatch):
    vault.save("anthropic_api", 0, "synthetic-first-key")
    state = json.loads(vault.path().read_text(encoding="utf-8"))
    state["entries"]["openai_api"] = state["entries"]["anthropic_api"]
    vault.path().write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-fallback")
    with pytest.raises(vault.CredentialUnavailable):
        vault.resolve("openai_api")
    vault.path().write_text("{broken", encoding="utf-8")
    with pytest.raises(vault.CredentialUnavailable):
        vault.save("anthropic_api", 1, "synthetic-new-key")
    assert vault.path().read_text(encoding="utf-8") == "{broken"
    assert next(item for item in model_connections.status()["connectors"] if item["connector_id"] == "anthropic_api")["credential_present"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="native encrypted key to MockTransport")
@pytest.mark.parametrize("domain", ["paper", "journal"])
def test_saved_key_feeds_existing_pipeline_without_environment_or_automatic_send(vault, domain):
    from app.services import model_connection_pipeline as pipeline
    from app.services.providers import describe_provider_status, get_active_provider
    from app.services.providers.base import ProviderRequest
    from tests import test_anthropic_journal as journal, test_anthropic_prepared_review as paper
    from tests.test_anthropic_messages import envelope
    key = "synthetic-encrypted-pipeline-key"
    vault.save("anthropic_api", 0, key)
    model_connections.save(model_connections.Update(expected_revision=0, default_connector_id="anthropic_api",
                                                   models={"anthropic_api": "claude-opus-5-5"}))
    assert describe_provider_status()["configured"]
    assert not describe_provider_status()["live_call_enabled"] and get_active_provider() is None
    frozen = (paper.contract if domain == "paper" else journal.contract)(model="claude-opus-5-5")
    output = paper.prior.response(frozen).raw_text if domain == "paper" else json.dumps(journal.proposal(frozen["packet"]))
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == key
        return httpx.Response(200, json=envelope(output, model="claude-opus-5-5"))
    provider = (pipeline.bind_paper if domain == "paper" else pipeline.bind_journal)(
        frozen, connector_id="anthropic_api", transport=httpx.MockTransport(handler))
    assert calls == [] and key not in repr(provider)
    result = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1 and result.model == "claude-opus-5-5"
    assert key not in result.raw_text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows encrypted authenticated settings")
def test_signed_admin_session_required_for_key_changes(vault, monkeypatch, tmp_path):
    from app.auth import sessions
    from app.auth.config import get_auth_settings, clear_auth_settings_cache
    from app.auth.security_types import ProductUser
    from tests import test_auth_session_hardening as auth
    monkeypatch.setattr(sessions, "_session_store_path", lambda: tmp_path / "sessions.json")
    for key, value in auth._oidc_env().items():
        monkeypatch.setenv(key, value)
    clear_auth_settings_cache()
    try:
        settings = get_auth_settings()
        api = importlib.import_module("app.api.model_connections")
        app = FastAPI()
        app.include_router(api.router)
        with TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 12345)) as http:
            body = {"expected_revision": 0, "api_key": "synthetic-session-key"}
            assert http.get(URL).status_code == 401
            assert http.put(URL, headers=HEADERS, json=body).status_code == 401
            user = ProductUser("reader", "reader", None, "oidc", False)
            http.cookies.set(settings.session_cookie_name, security.encode_product_session_cookie(user, settings))
            assert http.get(URL).status_code == 403
            assert http.put(URL, headers=HEADERS, json=body).status_code == 403
            assert not vault.path().exists()
            admin = ProductUser("admin", "admin", None, "oidc", True)
            http.cookies.set(settings.session_cookie_name, security.encode_product_session_cookie(admin, settings))
            result = http.put(URL, headers=HEADERS, json=body)
            assert result.status_code == 200 and result.json()["stored"]
            assert body["api_key"] not in result.text
    finally:
        clear_auth_settings_cache()

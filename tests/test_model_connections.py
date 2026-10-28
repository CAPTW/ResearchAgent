"""Connection selection is durable configuration, never execution authority."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import importlib
import json
from threading import Event, Timer

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import httpx

from app.auth.security import require_admin_user
from app.auth.security_types import ProductUser
from app.services.providers import describe_provider_status, get_active_provider


@pytest.fixture
def connections(tmp_path, monkeypatch):
    module = importlib.import_module("app.services.model_connections")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "connections.json")
    for name in ("XRML_PROVIDER", "OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return module


def test_codex_default_is_read_only_and_not_execution_authority(connections):
    value = connections.status()
    assert value["default_connector_id"] == value["effective_connector_id"] == "codex_subscription"
    assert not connections.STATE_PATH.exists()
    assert value["execution_allowed"] is False
    assert {row["connector_id"] for row in value["connectors"]} == {
        "codex_subscription", "claude_subscription", "antigravity_subscription", "grok_subscription", "openai_api", "xai_api", "anthropic_api", "ollama_local"}
    assert describe_provider_status()["provider"] == "codex_cli"
    assert get_active_provider() is None


def test_save_reload_stale_write_and_idempotent_lost_response(connections):
    body = connections.Update(expected_revision=0, default_connector_id="openai_api", models={"openai_api": "chosen-model"})
    saved = connections.save(body)
    assert saved["revision"] == 1
    assert connections.save(body) == saved  # lost response must be safe to repeat
    state = json.loads(connections.STATE_PATH.read_text(encoding="utf-8"))
    assert state["models"] == {"openai_api": "chosen-model"}
    with pytest.raises(ValueError, match="model_connections_changed"):
        connections.save(connections.Update(expected_revision=0, default_connector_id="codex_subscription", models={}))
    assert connections.status() == saved
    assert describe_provider_status()["provider"] == "openai"


def test_competing_updates_do_not_overwrite(connections):
    def save(name):
        try:
            return connections.save(connections.Update(expected_revision=0, default_connector_id=name, models={}))
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(save, ["openai_api", "xai_api"]))
    assert sum(isinstance(value, dict) for value in values) == 1
    assert "model_connections_changed" in values
    assert connections.status()["revision"] == 1


def test_secrets_and_unknown_authority_fields_rejected_without_echo(connections, monkeypatch):
    api = importlib.import_module("app.api.model_connections")
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin_user] = lambda: ProductUser("local", "local", None, "local", True)
    secret = "sk-never-print-this-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with TestClient(app) as client:
        status = client.get("/settings/model-connections")
        assert status.status_code == 200
        assert secret not in status.text
        assert next(row for row in status.json()["connectors"] if row["connector_id"] == "openai_api")["credential_present"]
        for field in ("api_key", "approval", "endpoint", "cli_path"):
            result = client.put("/settings/model-connections", json={"expected_revision": 0,
                "default_connector_id": "openai_api", "models": {}, field: secret})
            assert result.status_code == 422
            assert secret not in result.text
        for model in (secret, " model", "model\n", "", "a" * 201):
            if model == "":
                continue  # clearing an unset model is allowed
            result = client.put("/settings/model-connections", json={"expected_revision": 0,
                "default_connector_id": "openai_api", "models": {"openai_api": model}})
            assert result.status_code == 422
            assert secret not in result.text
    assert not connections.STATE_PATH.exists()


def test_corrupt_configuration_never_reset(connections):
    raw = '{"revision": 99, "models": "invalid"}'
    connections.STATE_PATH.write_text(raw, encoding="utf-8")
    from app.repositories.shared_state import SharedStateSchemaInvalid
    with pytest.raises(SharedStateSchemaInvalid):
        connections.save(connections.Update(expected_revision=0, default_connector_id="codex_subscription", models={}))
    assert connections.STATE_PATH.read_text(encoding="utf-8") == raw


def test_bad_preferences_cannot_break_disabled_startup_or_hide_failure(connections, monkeypatch):
    connections.STATE_PATH.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("XRML_PROVIDER", "disabled")
    assert describe_provider_status()["provider_mode"] == "disabled"
    monkeypatch.delenv("XRML_PROVIDER")
    assert describe_provider_status()["provider_mode"] == "connection_settings_unavailable"
    assert get_active_provider() is None
    assert connections.STATE_PATH.read_text(encoding="utf-8") == "{broken"


def test_connection_preferences_require_real_admin_and_do_not_mutate_on_denial(connections, monkeypatch):
    from app.auth import security
    from app.auth.config import AuthSettings
    api = importlib.import_module("app.api.model_connections")
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(security, "get_auth_settings", lambda: AuthSettings(auth_mode="disabled"))
    for user, expected in ((None, 401), (ProductUser("reader", "reader", None, "oidc", False), 403)):
        monkeypatch.setattr(security, "get_optional_product_user", lambda request: user)
        with TestClient(app) as client:
            assert client.get("/settings/model-connections").status_code == expected
            assert client.put("/settings/model-connections", json={"expected_revision": 0,
                "default_connector_id": "xai_api", "models": {}}).status_code == expected
    assert not connections.STATE_PATH.exists()


def test_slow_preference_write_does_not_block_other_async_requests(connections, monkeypatch):
    api = importlib.import_module("app.api.model_connections")
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin_user] = lambda: ProductUser("local", "local", None, "disabled", True)
    order = []
    release = Event()
    def save(body):
        release.wait(2)
        order.append("saved")
        return connections.status()
    monkeypatch.setattr(connections, "save", save)
    @app.get("/probe")
    async def probe():
        order.append("probe")
        release.set()
        return {"ok": True}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pending = asyncio.create_task(client.put("/settings/model-connections", json={"expected_revision": 0,
                "default_connector_id": "codex_subscription", "models": {}}))
            await asyncio.sleep(0.05)
            assert (await client.get("/probe")).status_code == 200
            assert (await pending).status_code == 200
    timer = Timer(1, release.set)
    timer.start()
    try:
        asyncio.run(run())
    finally:
        release.set()
        timer.cancel()
        timer.join()
    assert order == ["probe", "saved"]


def test_environment_override_is_explicit_and_never_falls_back(connections, monkeypatch):
    for override, expected in (("disabled", "disabled"), ("openai", "openai_api"), ("codex", "codex_subscription"), ("unknown", "unknown")):
        monkeypatch.setenv("XRML_PROVIDER", override)
        value = connections.status()
        assert value["environment_override"] == override
        assert value["effective_connector_id"] == expected
        assert get_active_provider() is None


def test_anthropic_api_is_distinct_from_subscription_and_never_activates(connections, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rows = {row["connector_id"]: row for row in connections.status()["connectors"]}
    assert "anthropic_api" in rows
    assert rows["anthropic_api"]["auth_mode"] == "api_key"
    assert rows["anthropic_api"]["provider_name"] == "anthropic"
    assert rows["anthropic_api"]["credential_env"] == "ANTHROPIC_API_KEY"
    assert rows["claude_subscription"]["provider_name"] == "claude_code"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-secret")
    saved = connections.save(connections.Update(expected_revision=0,
        default_connector_id="anthropic_api", models={"anthropic_api": "explicit-model"}))
    assert saved["execution_allowed"] is False
    assert "synthetic-anthropic-secret" not in json.dumps(saved) + connections.STATE_PATH.read_text(encoding="utf-8")
    assert describe_provider_status()["provider"] == "anthropic"
    assert describe_provider_status()["configured"] is True
    assert describe_provider_status()["live_call_enabled"] is False
    assert get_active_provider() is None

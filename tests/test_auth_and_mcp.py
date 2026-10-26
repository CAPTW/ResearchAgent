from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.auth import security as auth_security
from app.auth.config import get_auth_settings
from app.auth.security import (
    ProductUser,
    RESEARCH_READ_SCOPE,
    RESEARCH_WRITE_SCOPE,
    encode_product_session_cookie,
)
from app.schemas.auth import AuthSessionResponse


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_json_path(repo: dict[str, Any], run_id: str) -> Path:
    return repo["storage_root"] / run_id / "run.json"


def _set_run_owner(repo: dict[str, Any], run_id: str, owner_user_id: str | None) -> None:
    path = _run_json_path(repo, run_id)
    payload = _read_json(path)
    payload["owner_user_id"] = owner_user_id
    _write_json(path, payload)


def _attach_session_cookie(client: TestClient, *, user_id: str, display_name: str = "Test User", email: str = "user@example.com") -> None:
    settings = get_auth_settings()
    user = ProductUser(user_id=user_id, display_name=display_name, email=email, auth_source="oidc")
    client.cookies.set(settings.session_cookie_name, encode_product_session_cookie(user, settings))


def _oidc_env() -> dict[str, str]:
    return {
        "AUTH_MODE": "oidc",
        "SESSION_SECRET": "synthetic-test-session-secret-0123456789abcdef",
        "APP_BASE_URL": "http://testserver",
        "OIDC_ISSUER": "https://issuer.example.com",
        "OIDC_CLIENT_ID": "client-id",
        "OIDC_CLIENT_SECRET": "client-secret",
        "OIDC_AUDIENCE": "https://issuer.example.com/xrml-api",
    }


def test_auth_disabled_mode_preserves_local_internal_access(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    me_response = client.get("/auth/me")
    runs_response = client.get("/runs")
    mcp_initialize_response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    assert me_response.status_code == 200, me_response.text
    assert runs_response.status_code == 200, runs_response.text
    assert mcp_initialize_response.status_code == 200, mcp_initialize_response.text

    payload = AuthSessionResponse.model_validate(me_response.json())

    assert payload.auth_mode == "disabled"
    assert payload.authenticated is True
    assert payload.user is not None
    assert payload.user.user_id == "local-internal-user"
    assert payload.product_session_backend == "local_internal"
    assert payload.warnings
    assert any(item["run_id"] == isolated_run_repo["run_id"] for item in runs_response.json())
    assert mcp_initialize_response.json()["result"]["protocolVersion"] == get_auth_settings().mcp_protocol_version


def test_oidc_login_redirects_to_provider_authorization_endpoint(
    app_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_security,
        "_oidc_metadata",
        lambda settings: {"authorization_endpoint": "https://issuer.example.com/oauth2/authorize"},
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        response = client.get("/auth/login", params={"return_to": "/app/runs/demo"}, follow_redirects=False)

    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith("https://issuer.example.com/oauth2/authorize?")
    assert "state=" in response.headers["location"]
    assert "nonce=" in response.headers["location"]
    assert get_auth_settings().auth_state_cookie_name in response.cookies


def test_unauthenticated_oidc_mode_denies_protected_endpoints(
    app_factory,
    isolated_run_repo: dict[str, Any],
) -> None:
    with TestClient(app_factory(**_oidc_env())) as client:
        runs_response = client.get("/runs")
        run_response = client.get(f"/runs/{isolated_run_repo['run_id']}")
        create_response = client.post(
            "/runs",
            json={
                "topic": "Auth Test",
                "queries": [],
                "categories": [],
                "max_results": None,
                "top_papers": 3,
                "weekly_top": 15,
            },
        )

    assert runs_response.status_code == 401, runs_response.text
    assert run_response.status_code == 401, run_response.text
    assert create_response.status_code == 401, create_response.text


def test_authenticated_oidc_session_can_access_owned_runs_and_filters_list(
    app_factory,
    isolated_run_repo: dict[str, Any],
    monkeypatch,
) -> None:
    _set_run_owner(isolated_run_repo, isolated_run_repo["run_id"], "user-1")
    _set_run_owner(isolated_run_repo, isolated_run_repo["compare_run_id"], "user-2")
    monkeypatch.setattr(
        "app.api.runs.start_run",
        lambda request, owner_user_id=None: {
            "run_id": "owned-auth-run",
            "topic": request.topic,
            "status": "queued",
            "created_at": "2026-04-09T00:00:00+00:00",
            "updated_at": "2026-04-09T00:00:00+00:00",
            "owner_user_id": owner_user_id,
        },
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        _attach_session_cookie(client, user_id="user-1")

        me_response = client.get("/auth/me")
        runs_response = client.get("/runs")
        run_response = client.get(f"/runs/{isolated_run_repo['run_id']}")
        forbidden_response = client.get(f"/runs/{isolated_run_repo['compare_run_id']}")
        create_response = client.post(
            "/runs",
            json={
                "topic": "Owned Auth Run",
                "queries": [],
                "categories": [],
                "max_results": None,
                "top_papers": 3,
                "weekly_top": 15,
            },
        )

    assert me_response.status_code == 200, me_response.text
    assert runs_response.status_code == 200, runs_response.text
    assert run_response.status_code == 200, run_response.text
    assert forbidden_response.status_code == 403, forbidden_response.text
    assert create_response.status_code == 200, create_response.text

    run_ids = [item["run_id"] for item in runs_response.json()]
    assert isolated_run_repo["run_id"] in run_ids
    assert isolated_run_repo["compare_run_id"] not in run_ids
    assert create_response.json()["owner_user_id"] == "user-1"


def test_oidc_product_session_uses_server_side_session_table(
    app_factory,
    isolated_run_repo: dict[str, Any],
) -> None:
    _set_run_owner(isolated_run_repo, isolated_run_repo["run_id"], "user-1")

    with TestClient(app_factory(**_oidc_env())) as client:
        _attach_session_cookie(client, user_id="user-1")
        settings = get_auth_settings()
        cookie_value = client.cookies.get(settings.session_cookie_name)

        signed_payload = auth_security._load_signed_cookie(
            settings,
            salt="product-session",
            value=cookie_value,
            max_age=settings.product_session_ttl_seconds,
        )
        me_response = client.get("/auth/me")
        runs_response = client.get("/runs")

    assert signed_payload is not None
    assert "sid" in signed_payload
    assert "sub" not in signed_payload
    assert me_response.status_code == 200, me_response.text
    assert me_response.json()["product_session_backend"] == "server_side_file"
    assert runs_response.status_code == 200, runs_response.text

    session_store_path = isolated_run_repo["storage_root"].parent / "auth" / "product_sessions.json"
    session_store = _read_json(session_store_path)
    session_record = session_store["sessions"][signed_payload["sid"]]
    assert session_record["user_id"] == "user-1"
    assert session_record["email"] == "user@example.com"
    assert session_record["revoked_at"] is None


def test_logout_revokes_server_side_product_session(
    app_factory,
    isolated_run_repo: dict[str, Any],
) -> None:
    _set_run_owner(isolated_run_repo, isolated_run_repo["run_id"], "user-1")

    with TestClient(app_factory(**_oidc_env())) as client:
        _attach_session_cookie(client, user_id="user-1")
        settings = get_auth_settings()
        stolen_cookie_value = client.cookies.get(settings.session_cookie_name)

        authorized_response = client.get("/runs")
        logout_response = client.post("/auth/logout")
        client.cookies.set(settings.session_cookie_name, stolen_cookie_value)
        revoked_response = client.get("/runs")

    assert authorized_response.status_code == 200, authorized_response.text
    assert logout_response.status_code == 200, logout_response.text
    assert revoked_response.status_code == 401, revoked_response.text


def test_mcp_unauthorized_calls_return_auth_challenge(
    app_factory,
    isolated_run_repo: dict[str, Any],
) -> None:
    _set_run_owner(isolated_run_repo, isolated_run_repo["run_id"], "user-1")

    with TestClient(app_factory(**_oidc_env())) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_papers",
                    "arguments": {"run_id": isolated_run_repo["run_id"]},
                },
            },
        )

    assert response.status_code == 401, response.text
    assert "WWW-Authenticate" in response.headers
    assert "research.read" in response.headers["WWW-Authenticate"]
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["result"]["structuredContent"]["resource_metadata"].endswith("/.well-known/oauth-protected-resource/mcp")
    assert payload["result"]["_meta"]["mcp/www_authenticate"]


def test_mcp_scoped_tools_enforce_read_and_write_boundaries(
    app_factory,
    isolated_run_repo: dict[str, Any],
    monkeypatch,
) -> None:
    _set_run_owner(isolated_run_repo, isolated_run_repo["run_id"], "user-1")
    monkeypatch.setattr(
        auth_security,
        "_decode_mcp_access_token",
        lambda token, settings: {"sub": "user-1", "email": "user@example.com", "scope": "research.read research.write"},
    )
    monkeypatch.setattr(
        "app.api.mcp.start_run",
        lambda request, owner_user_id=None: {
            "run_id": "mcp-created-run",
            "topic": request.topic,
            "status": "queued",
            "created_at": "2026-04-09T00:00:00+00:00",
            "updated_at": "2026-04-09T00:00:00+00:00",
            "owner_user_id": owner_user_id,
        },
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        search_response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer signed-read-write-token"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_papers",
                    "arguments": {"run_id": isolated_run_repo["run_id"], "page_size": 5},
                },
            },
        )
        start_response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer signed-read-write-token"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "start_research_run",
                    "arguments": {
                        "topic": "MCP Started Run",
                        "queries": [],
                        "categories": [],
                        "max_results": None,
                        "top_papers": 3,
                        "weekly_top": 15,
                    },
                },
            },
        )

    assert search_response.status_code == 200, search_response.text
    assert start_response.status_code == 200, start_response.text
    assert search_response.json()["result"]["structuredContent"]["run_id"] == isolated_run_repo["run_id"]
    assert start_response.json()["result"]["structuredContent"]["run_id"] == "mcp-created-run"


def test_mcp_write_tool_rejects_missing_write_scope(
    app_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_security,
        "_decode_mcp_access_token",
        lambda token, settings: {"sub": "user-1", "email": "user@example.com", "scope": "research.read"},
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer signed-read-only-token"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "start_research_run",
                    "arguments": {
                        "topic": "Blocked MCP Write",
                        "queries": [],
                        "categories": [],
                        "max_results": None,
                        "top_papers": 3,
                        "weekly_top": 15,
                    },
                },
            },
        )

    assert response.status_code == 403, response.text
    assert "research.write" in response.headers["WWW-Authenticate"]
    payload = response.json()
    assert payload["result"]["structuredContent"]["error"] == "insufficient_scope"
    assert payload["result"]["_meta"]["mcp/www_authenticate"]


def test_auth_and_mcp_surfaces_do_not_leak_provider_or_model_secrets(
    app_factory,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-openai-key")

    with TestClient(app_factory(**_oidc_env())) as client:
        auth_me_response = client.get("/auth/me")
        metadata_response = client.get("/.well-known/oauth-protected-resource/mcp")
        tools_response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})

    combined = "\n".join(
        [
            auth_me_response.text,
            metadata_response.text,
            tools_response.text,
        ]
    )

    assert auth_me_response.status_code == 200, auth_me_response.text
    assert metadata_response.status_code == 200, metadata_response.text
    assert tools_response.status_code == 200, tools_response.text
    assert "client-secret" not in combined
    assert "super-secret-openai-key" not in combined
    assert RESEARCH_READ_SCOPE in tools_response.text
    assert RESEARCH_WRITE_SCOPE in tools_response.text

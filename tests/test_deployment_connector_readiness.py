from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import security as auth_security


def _oidc_env(**extra: str) -> dict[str, str]:
    env = {
        "AUTH_MODE": "oidc",
        "SESSION_SECRET": "synthetic-test-session-secret-0123456789abcdef",
        "APP_BASE_URL": "https://research.example.com",
        "WEB_APP_BASE_PATH": "/app",
        "OIDC_ISSUER": "https://issuer.example.com",
        "OIDC_CLIENT_ID": "client-id",
        "OIDC_CLIENT_SECRET": "client-secret",
    }
    env.update(extra)
    return env


def test_https_deployment_uses_public_base_url_for_auth_and_mcp_metadata(app_factory, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_security,
        "_oidc_metadata",
        lambda settings: {"authorization_endpoint": "https://issuer.example.com/oauth2/authorize"},
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        login_response = client.get("/auth/login", follow_redirects=False)
        me_response = client.get("/auth/me")
        metadata_response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert login_response.status_code == 302
    assert login_response.headers["location"].startswith("https://issuer.example.com/oauth2/authorize?")
    assert "Secure" in login_response.headers["set-cookie"]
    assert "SameSite=lax" in login_response.headers["set-cookie"]

    payload = me_response.json()
    assert payload["login_url"] == "https://research.example.com/auth/login"
    assert payload["mcp_url"] == "https://research.example.com/mcp"
    assert payload["mcp_resource_metadata_url"] == "https://research.example.com/.well-known/oauth-protected-resource/mcp"

    metadata = metadata_response.json()
    assert metadata["resource"] == "https://research.example.com/mcp"
    assert metadata["resource_name"] == "XRML Research Agent MCP"
    assert metadata["resource_documentation"] == "https://research.example.com/app/"
    assert metadata["authorization_servers"] == ["https://issuer.example.com"]


def test_settings_status_reports_mcp_oauth_readiness_for_shared_https_deployments(app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(OIDC_AUDIENCE="https://issuer.example.com/xrml-api"))) as client:
        response = client.get("/settings/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["auth_mode"] == "oidc"
    assert payload["mcp"]["readiness_label"] == "oauth_ready"
    assert payload["mcp"]["chatgpt_ready"] is True
    assert payload["mcp"]["authorization_servers"] == ["https://issuer.example.com"]
    assert payload["mcp"]["protected_write_tools"] == ["start_research_run"]
    assert payload["mcp"]["warnings"]


def test_production_static_serving_uses_app_base_path_and_preserves_api_routes(
    app_factory,
    isolated_run_repo,
    monkeypatch,
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "web" / "dist"
    assets_root = dist_root / "assets"
    assets_root.mkdir(parents=True, exist_ok=True)
    (dist_root / "index.html").write_text("<!doctype html><html><body>XRML App</body></html>", encoding="utf-8")
    (assets_root / "app.js").write_text("console.log('xrml');", encoding="utf-8")

    monkeypatch.setattr(main_module, "WEB_DIST_ROOT", dist_root)

    with TestClient(app_factory(WEB_APP_BASE_PATH="/app")) as client:
        app_index = client.get("/app")
        app_route = client.get("/app/runs/demo")
        app_asset = client.get("/app/assets/app.js")
        api_route = client.get(f"/runs/{isolated_run_repo['run_id']}")

    assert app_index.status_code == 200
    assert "XRML App" in app_index.text
    assert app_route.status_code == 200
    assert "XRML App" in app_route.text
    assert app_asset.status_code == 200
    assert "console.log('xrml')" in app_asset.text
    assert api_route.status_code == 200
    assert api_route.json()["run_id"] == isolated_run_repo["run_id"]


def test_mcp_challenge_metadata_stays_connector_ready_in_oidc_mode(app_factory, isolated_run_repo) -> None:
    with TestClient(app_factory(**_oidc_env())) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "search_papers",
                    "arguments": {"run_id": isolated_run_repo["run_id"], "page_size": 1},
                },
            },
        )
        root_metadata = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
    assert 'resource_metadata="https://research.example.com/.well-known/oauth-protected-resource/mcp"' in response.headers["WWW-Authenticate"]
    payload = response.json()
    assert payload["result"]["structuredContent"]["resource_metadata"] == "https://research.example.com/.well-known/oauth-protected-resource/mcp"
    assert payload["result"]["_meta"]["mcp/www_authenticate"]
    assert root_metadata.status_code == 200
    assert root_metadata.json()["scopes_supported"] == ["research.read", "research.write"]

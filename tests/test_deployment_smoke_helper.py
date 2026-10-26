from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

from app.services.deployment_smoke import (
    EXPECTED_MCP_TOOL_NAMES,
    deployment_smoke_failed,
    render_deployment_smoke_markdown,
    run_deployment_smoke,
)
from tools.mcp_connector_smoke import main as smoke_main


@dataclass
class _StubState:
    web_app_ok: bool = True
    web_app_failures_remaining: int = 0
    metadata_ok: bool = True
    tools: list[str] = field(default_factory=lambda: list(EXPECTED_MCP_TOOL_NAMES))
    unauthorized_challenge_ok: bool = True
    tool_calls: list[str] = field(default_factory=list)
    authenticated_run_id: str | None = None


class _SmokeHandler(BaseHTTPRequestHandler):
    server_version = "XRMLSmoke/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    @property
    def state(self) -> _StubState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, status_code: int, payload: dict, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/app":
            if self.state.web_app_failures_remaining > 0:
                self.state.web_app_failures_remaining -= 1
                self._send_json(503, {"detail": "warming up"})
            elif self.state.web_app_ok:
                self._send_html(200, "<!doctype html><html><body>XRML App</body></html>")
            else:
                self._send_json(404, {"detail": "missing"})
            return

        if self.path in {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}:
            if self.state.metadata_ok:
                self._send_json(
                    200,
                    {
                        "resource": "https://staging.example.com/mcp",
                        "resource_name": "XRML Research Agent MCP",
                        "authorization_servers": ["https://issuer.example.com"],
                        "scopes_supported": ["research.read", "research.write"],
                    },
                )
            else:
                self._send_json(200, {"resource": "https://staging.example.com/mcp"})
            return

        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._send_json(404, {"detail": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")

        if method == "initialize":
            self._send_json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"protocolVersion": "2025-03-26"}})
            return

        if method == "tools/list":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "tools": [{"name": name} for name in self.state.tools],
                    },
                },
            )
            return

        if method == "tools/call":
            params = payload.get("params") or {}
            tool_name = params.get("name")
            if isinstance(tool_name, str):
                self.state.tool_calls.append(tool_name)

            authorization = self.headers.get("Authorization")
            if authorization and self.state.authenticated_run_id:
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {"structuredContent": {"run_id": self.state.authenticated_run_id}},
                    },
                )
                return

            headers = (
                {"WWW-Authenticate": 'Bearer realm="xrml-mcp", resource_metadata="https://staging.example.com/.well-known/oauth-protected-resource/mcp"'}
                if self.state.unauthorized_challenge_ok
                else None
            )
            body = (
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "structuredContent": {
                            "error": "invalid_token",
                            "resource_metadata": "https://staging.example.com/.well-known/oauth-protected-resource/mcp",
                        },
                        "_meta": {
                            "mcp/www_authenticate": [
                                'Bearer realm="xrml-mcp", resource_metadata="https://staging.example.com/.well-known/oauth-protected-resource/mcp", error="invalid_token", error_description="Bearer token required for this MCP tool."'
                            ]
                        },
                        "isError": True,
                    },
                }
                if self.state.unauthorized_challenge_ok
                else {"error": "invalid_token"}
            )
            self._send_json(401, body, headers=headers)
            return

        self._send_json(400, {"detail": "unsupported"})


@contextmanager
def _smoke_server(state: _StubState) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SmokeHandler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_deployment_smoke_helper_success_path_is_read_only_by_default() -> None:
    state = _StubState()
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url)

    assert result.ok is True
    assert result.web_app_ok is True
    assert result.mcp_initialize_ok is True
    assert result.tools_list_ok is True
    assert result.metadata_ok is True
    assert result.unauthorized_challenge_ok is True
    assert result.authenticated_read_ok == "skipped"
    assert state.tool_calls == ["search_papers"]


def test_deployment_smoke_helper_fails_clearly_when_app_shell_is_missing() -> None:
    state = _StubState(web_app_ok=False)
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url)

    assert result.ok is False
    assert any("/app" in failure for failure in result.failures)


def test_deployment_smoke_helper_fails_on_tool_catalog_mismatch() -> None:
    state = _StubState(tools=["search_papers", "get_run_status"])
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url)

    assert result.ok is False
    assert any("tools/list mismatch" in failure for failure in result.failures)


def test_deployment_smoke_helper_fails_on_missing_metadata_fields() -> None:
    state = _StubState(metadata_ok=False)
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url)

    assert result.ok is False
    assert any("Protected-resource metadata" in failure for failure in result.failures)


def test_deployment_smoke_helper_fails_on_missing_unauthorized_challenge() -> None:
    state = _StubState(unauthorized_challenge_ok=False)
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url)

    assert result.ok is False
    assert any("Unauthorized protected MCP tool call" in failure for failure in result.failures)


def test_deployment_smoke_helper_supports_optional_authenticated_read() -> None:
    state = _StubState(authenticated_run_id="demo-run")
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url, bearer_token="signed-token", run_id="demo-run")

    assert result.ok is True
    assert result.authenticated_read_ok == "passed"
    assert state.tool_calls == ["search_papers", "get_run_status"]


def test_deployment_smoke_helper_can_render_markdown_summary() -> None:
    state = _StubState()
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url)

    summary = render_deployment_smoke_markdown(result)

    assert "# Deployment smoke report" in summary
    assert "All required checks passed." in summary
    assert "`search_papers`" in summary


def test_cli_can_write_markdown_summary_file(tmp_path) -> None:
    state = _StubState()
    summary_path = tmp_path / "summary.md"

    with _smoke_server(state) as base_url:
        exit_code = smoke_main(["--base-url", base_url, "--markdown-summary", str(summary_path), "--json"])

    assert exit_code == 0
    assert summary_path.exists()
    assert "Deployment smoke report" in summary_path.read_text(encoding="utf-8")


def test_cli_can_fail_on_warning_without_failures() -> None:
    state = _StubState()
    with _smoke_server(state) as base_url:
        exit_code = smoke_main(["--base-url", base_url, "--bearer-token", "signed-token", "--fail-on-warning", "true"])

    assert exit_code == 1


def test_deployment_smoke_failed_respects_warning_mode() -> None:
    state = _StubState()
    with _smoke_server(state) as base_url:
        result = run_deployment_smoke(base_url=base_url, bearer_token="signed-token")

    assert result.failures == []
    assert result.warnings
    assert deployment_smoke_failed(result, fail_on_warning=False) is False
    assert deployment_smoke_failed(result, fail_on_warning=True) is True


def test_cli_retry_can_wait_for_transient_app_readiness() -> None:
    state = _StubState(web_app_failures_remaining=1)
    with _smoke_server(state) as base_url:
        exit_code = smoke_main(
            [
                "--base-url",
                base_url,
                "--retry-attempts",
                "2",
                "--retry-delay-seconds",
                "0",
            ]
        )

    assert exit_code == 0

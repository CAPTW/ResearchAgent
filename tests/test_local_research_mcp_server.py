from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "tools" / "local_research_mcp_server.py"


def _send_message(process: subprocess.Popen[bytes], payload: dict[str, object]) -> None:
    assert process.stdin is not None
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    process.stdin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    process.stdin.write(encoded)
    process.stdin.flush()


def _read_message(process: subprocess.Popen[bytes]) -> dict[str, object]:
    assert process.stdout is not None
    length = None
    while True:
        line = process.stdout.readline()
        if line == b"":
            stderr = process.stderr.read().decode("utf-8") if process.stderr is not None else ""
            raise AssertionError(f"MCP server closed unexpectedly. stderr={stderr}")
        if line in {b"\r\n", b"\n"}:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    assert length is not None
    body = process.stdout.read(length)
    return json.loads(body.decode("utf-8"))


def _start_server() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_mcp_server_exposes_run_local_research_tool() -> None:
    process = _start_server()
    try:
        _send_message(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        initialize = _read_message(process)
        assert initialize["result"]["serverInfo"]["name"] == "xrml-local-research"

        _send_message(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = _read_message(process)
        tools = {tool["name"] for tool in listing["result"]["tools"]}

        assert "run_local_research" in tools
        assert "get_local_workflow_help" in tools
        assert "check_local_workflow_env" in tools
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_run_local_research_tool_returns_runner_contract(tmp_path: Path) -> None:
    process = _start_server()
    try:
        _send_message(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        _read_message(process)
        _send_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "run_local_research",
                    "arguments": {
                        "topic": "MCP smoke topic",
                        "profile_name": "MCP smoke profile",
                        "storage_root": str(tmp_path / "storage"),
                        "dry_run": True,
                    },
                },
            },
        )
        response = _read_message(process)
        payload = response["result"]["structuredContent"]

        assert payload["status"] == "dry_run"
        assert payload["executed_via"] == "mcp_stdio"
        assert payload["run_id"] is None
        for field in ("status", "run_id", "provider_mode", "artifact_paths", "local_ui_urls", "failures", "executed_via"):
            assert field in payload
    finally:
        process.terminate()
        process.wait(timeout=10)

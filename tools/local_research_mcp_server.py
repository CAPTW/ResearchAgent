from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.local_research_workflow import (
    RESULT_STATUS_DRY_RUN,
    RESULT_STATUS_FAILED,
    RESULT_STATUS_NOT_EXECUTED,
    normalize_result_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "tools" / "local_research_workflow.py"
SERVER_NAME = "xrml-local-research"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-03-26"


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _json_rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_required_string(arguments: dict[str, Any], *names: str) -> str:
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"Missing required string argument: {names[0]}")


def _coerce_optional_string(arguments: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _runner_command(
    *,
    topic: str,
    profile_name: str,
    storage_root: str | None,
    dry_run: bool,
    json_out: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(RUNNER_PATH),
        "--topic",
        topic,
        "--profile-name",
        profile_name,
        "--json-out",
        str(json_out),
        "--fail-if-not-executed",
    ]
    if storage_root:
        command.extend(["--storage-root", storage_root])
    if dry_run:
        command.append("--dry-run")
    return command


def _load_runner_result(result_path: Path, stdout_text: str, stderr_text: str) -> dict[str, Any]:
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    if stdout_text.strip():
        try:
            return json.loads(stdout_text)
        except json.JSONDecodeError:
            pass
    return normalize_result_payload(
        {
            "status": RESULT_STATUS_NOT_EXECUTED,
            "failures": ["Runner did not emit a machine-readable result file."],
            "stdout": stdout_text.strip() or None,
            "stderr": stderr_text.strip() or None,
        },
        executed_via="mcp_stdio",
    )


def invoke_runner(
    *,
    topic: str,
    profile_name: str,
    storage_root: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="xrml-local-run-", suffix=".json", delete=False) as handle:
        result_path = Path(handle.name)

    command = _runner_command(
        topic=topic,
        profile_name=profile_name,
        storage_root=storage_root,
        dry_run=dry_run,
        json_out=result_path,
    )
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
    )
    payload = _load_runner_result(result_path, completed.stdout, completed.stderr)
    result_path.unlink(missing_ok=True)
    normalized = normalize_result_payload(payload, executed_via="mcp_stdio")
    normalized["runner_exit_code"] = completed.returncode
    normalized["runner_command"] = command
    if completed.stderr.strip():
        normalized["runner_stderr"] = completed.stderr.strip()
    if completed.returncode != 0 and not normalized["failures"]:
        normalized["failures"] = [f"Runner exited with code {completed.returncode}."]
    return normalized


def run_local_research(arguments: dict[str, Any]) -> dict[str, Any]:
    topic = _coerce_required_string(arguments, "topic")
    profile_name = _coerce_required_string(arguments, "profile_name", "profile-name")
    storage_root = _coerce_optional_string(arguments, "storage_root", "storage-root")
    dry_run = _coerce_bool(arguments.get("dry_run", False))
    return invoke_runner(topic=topic, profile_name=profile_name, storage_root=storage_root, dry_run=dry_run)


def get_local_workflow_help(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "local_only": True,
        "mcp_first": True,
        "notes": [
            "Do not touch Render/public OAuth/staging in this mode.",
            "If the MCP tool is available, use it before any shell fallback.",
            "fallback_only is acceptable without OPENAI_API_KEY.",
            "Never report success without a real run_id.",
        ],
        "preferred_tool": "run_local_research",
        "manual_fallback_commands": {
            "powershell": 'pwsh scripts/run_local_research.ps1 --topic "<topic>" --profile-name "<profile>"',
            "bash": 'bash scripts/run_local_research.sh --topic "<topic>" --profile-name "<profile>"',
            "runner": 'uv run python tools/local_research_workflow.py --topic "<topic>" --profile-name "<profile>" --strict',
        },
        "mcp_server_command": 'uv run python tools/local_research_mcp_server.py',
        "result_contract_fields": [
            "status",
            "run_id",
            "provider_mode",
            "artifact_paths",
            "local_ui_urls",
            "failures",
            "executed_via",
        ],
    }


def check_local_workflow_env(arguments: dict[str, Any]) -> dict[str, Any]:
    storage_root = _coerce_optional_string(arguments, "storage_root", "storage-root")
    payload = invoke_runner(
        topic="Local workflow environment check",
        profile_name="Local workflow environment check",
        storage_root=storage_root,
        dry_run=True,
    )
    return {
        "status": payload.get("status", RESULT_STATUS_DRY_RUN),
        "provider_mode": payload.get("provider_mode"),
        "endpoint_checks": payload.get("endpoint_checks", {}),
        "failures": payload.get("failures", []),
        "executed_via": "mcp_stdio",
        "script_availability": {
            "runner": RUNNER_PATH.is_file(),
            "powershell_wrapper": (REPO_ROOT / "scripts" / "run_local_research.ps1").is_file(),
            "bash_wrapper": (REPO_ROOT / "scripts" / "run_local_research.sh").is_file(),
        },
        "manual_fallback_commands": get_local_workflow_help({})["manual_fallback_commands"],
    }


TOOLS: dict[str, dict[str, Any]] = {
    "run_local_research": {
        "description": (
            "local-only deterministic research workflow runner. No Render/public OAuth/staging. "
            "fallback_only is acceptable without OPENAI_API_KEY. "
            "Returns status, run_id, provider_mode, artifact_paths, local_ui_urls, failures, executed_via."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "profile_name": {"type": "string"},
                "storage_root": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["topic", "profile_name"],
            "additionalProperties": False,
        },
        "handler": run_local_research,
    },
    "get_local_workflow_help": {
        "description": (
            "Return runner, wrapper, and manual fallback commands for the local-only research workflow. "
            "No Render/public OAuth/staging."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_local_workflow_help,
    },
    "check_local_workflow_env": {
        "description": (
            "Summarize local backend/frontend reachability, provider mode, wrapper availability, and dry-run environment checks. "
            "local-only; fallback_only is acceptable without OPENAI_API_KEY."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"storage_root": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": check_local_workflow_env,
    },
}


def _tool_definition(name: str, tool: dict[str, Any]) -> dict[str, Any]:
    annotations = {"readOnlyHint": name != "run_local_research"}
    return {
        "name": name,
        "description": tool["description"],
        "inputSchema": tool["inputSchema"],
        "annotations": annotations,
        "_meta": {"annotations": annotations},
    }


class StdioMcpTransport:
    def __init__(self) -> None:
        self._input = sys.stdin.buffer
        self._output = sys.stdout.buffer
        self._output_mode = "content-length"

    def read_message(self) -> dict[str, Any] | None:
        while True:
            first_line = self._input.readline()
            if first_line == b"":
                return None
            if not first_line.strip():
                continue
            if first_line.lower().startswith(b"content-length:"):
                self._output_mode = "content-length"
                try:
                    length = int(first_line.split(b":", 1)[1].strip())
                except ValueError as exc:
                    raise RuntimeError("Invalid Content-Length header.") from exc
                while True:
                    header_line = self._input.readline()
                    if header_line in {b"\r\n", b"\n", b""}:
                        break
                body = self._input.read(length)
                return json.loads(body.decode("utf-8"))

            self._output_mode = "line"
            return json.loads(first_line.decode("utf-8"))

    def send_message(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self._output_mode == "line":
            self._output.write(encoded + b"\n")
        else:
            header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
            self._output.write(header)
            self._output.write(encoded)
        self._output.flush()


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return _json_rpc_error(request_id, -32600, "JSON-RPC method is required.")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _json_rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )
    if method == "ping":
        return _json_rpc_result(request_id, {})
    if method == "tools/list":
        return _json_rpc_result(request_id, {"tools": [_tool_definition(name, tool) for name, tool in TOOLS.items()]})
    if method != "tools/call":
        return _json_rpc_error(request_id, -32601, f"Unsupported MCP method: {method}")

    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _json_rpc_error(request_id, -32602, "tools/call params must be an object.")
    name = str(params.get("name") or "").strip()
    if name not in TOOLS:
        return _json_rpc_error(request_id, -32601, f"Unknown tool: {name or 'unknown'}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _json_rpc_error(request_id, -32602, "tools/call arguments must be an object.")

    try:
        payload = TOOLS[name]["handler"](arguments)
        is_error = bool(payload.get("status") in {RESULT_STATUS_NOT_EXECUTED, RESULT_STATUS_FAILED})
        return _json_rpc_result(request_id, _tool_result(payload, is_error=is_error))
    except Exception as exc:
        payload = normalize_result_payload(
            {"status": RESULT_STATUS_FAILED, "failures": [str(exc)]},
            executed_via="mcp_stdio",
            error=str(exc),
        )
        return _json_rpc_result(request_id, _tool_result(payload, is_error=True))


def main() -> int:
    transport = StdioMcpTransport()
    while True:
        try:
            message = transport.read_message()
        except json.JSONDecodeError as exc:
            transport.send_message(_json_rpc_error(None, -32700, f"Invalid JSON body: {exc.msg}"))
            continue
        except RuntimeError as exc:
            transport.send_message(_json_rpc_error(None, -32600, str(exc)))
            continue
        if message is None:
            return 0
        response = handle_request(message)
        if response is not None:
            transport.send_message(response)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"


def _npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _run(command: list[str], *, cwd: Path, extra_env: dict[str, str]) -> int:
    env = os.environ.copy()
    for key, value in extra_env.items():
        env.setdefault(key, value)
    return subprocess.call(command, cwd=str(cwd), env=env)


def _json_request(method: str, url: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body_text = error.read().decode("utf-8")
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError:
            parsed = {"raw": body_text}
        return error.code, parsed
    except URLError as error:
        raise RuntimeError(f"Could not reach {url}: {error.reason}") from error


def run_backend(args: argparse.Namespace) -> int:
    app_base_url = f"http://{args.host}:{args.port}"
    return _run(
        [
            "uv",
            "run",
            "uvicorn",
            "app.local:create_local_app",
            "--factory",
            "--host",
            args.host,
            "--port",
            str(args.port),
            *([] if args.no_reload else ["--reload"]),
        ],
        cwd=REPO_ROOT,
        extra_env={
            "AUTH_MODE": args.auth_mode,
            "APP_BASE_URL": app_base_url,
            "WEB_APP_BASE_PATH": "/app",
            "TRUST_PROXY_HEADERS": "false",
        },
    )


def run_frontend(args: argparse.Namespace) -> int:
    return _run(
        [
            _npm_command(),
            "run",
            "dev",
            "--",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        cwd=WEB_ROOT,
        extra_env={
            "VITE_API_BASE_URL": args.api_base_url,
        },
    )


def run_prod(args: argparse.Namespace) -> int:
    build_env = os.environ.copy()
    build_env.setdefault("WEB_APP_BASE_PATH", "/app")
    if not args.skip_build:
        build_result = subprocess.call([_npm_command(), "run", "build"], cwd=str(WEB_ROOT), env=build_env)
        if build_result != 0:
            return build_result
    return _run(
        [
            "uv",
            "run",
            "uvicorn",
            "app.local:create_local_app",
            "--factory",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        cwd=REPO_ROOT,
        extra_env={
            "AUTH_MODE": args.auth_mode,
            "APP_BASE_URL": f"http://{args.host}:{args.port}",
            "WEB_APP_BASE_PATH": "/app",
            "TRUST_PROXY_HEADERS": "false",
        },
    )


def run_local_mcp_smoke(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    initialize_status, _ = _json_request(
        "POST",
        f"{base_url}/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    tools_status, tools_payload = _json_request(
        "POST",
        f"{base_url}/mcp",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    metadata_status, metadata_payload = _json_request("GET", f"{base_url}/.well-known/oauth-protected-resource/mcp")

    tool_names = sorted(
        tool.get("name", "")
        for tool in ((tools_payload.get("result") or {}).get("tools") or [])
        if isinstance(tool, dict)
    )
    ok = initialize_status == 200 and tools_status == 200 and metadata_status == 200 and len(tool_names) == 5
    result = {
        "base_url": base_url,
        "initialize_status": initialize_status,
        "tools_status": tools_status,
        "metadata_status": metadata_status,
        "tool_names": tool_names,
        "resource": metadata_payload.get("resource"),
        "resource_name": metadata_payload.get("resource_name"),
        "ok": ok,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"base_url: {base_url}")
        print(f"initialize_status: {initialize_status}")
        print(f"tools_status: {tools_status}")
        print(f"metadata_status: {metadata_status}")
        print(f"tool_names: {', '.join(tool_names)}")
        print(f"resource: {metadata_payload.get('resource')}")
        print(f"resource_name: {metadata_payload.get('resource_name')}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first demo helpers for the XRML research workbench.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backend = subparsers.add_parser("backend", help="Start the FastAPI backend in local demo mode.")
    backend.add_argument("--host", default="127.0.0.1")
    backend.add_argument("--port", type=int, default=8000)
    backend.add_argument("--auth-mode", choices=["disabled", "oidc"], default="disabled")
    backend.add_argument("--no-reload", action="store_true")
    backend.set_defaults(handler=run_backend)

    frontend = subparsers.add_parser("frontend", help="Start the Vite frontend against the local backend.")
    frontend.add_argument("--host", default="127.0.0.1")
    frontend.add_argument("--port", type=int, default=5173)
    frontend.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    frontend.set_defaults(handler=run_frontend)

    prod = subparsers.add_parser("prod", help="Build the frontend for /app and serve the backend in prod-like local mode.")
    prod.add_argument("--host", default="127.0.0.1")
    prod.add_argument("--port", type=int, default=8000)
    prod.add_argument("--auth-mode", choices=["disabled", "oidc"], default="disabled")
    prod.add_argument("--skip-build", action="store_true")
    prod.set_defaults(handler=run_prod)

    mcp_smoke = subparsers.add_parser("mcp-local-smoke", help="Verify localhost MCP initialize/tools/list/metadata without public deployment.")
    mcp_smoke.add_argument("--base-url", default="http://127.0.0.1:8000")
    mcp_smoke.add_argument("--json", action="store_true")
    mcp_smoke.set_defaults(handler=run_local_mcp_smoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

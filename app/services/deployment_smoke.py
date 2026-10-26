from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

EXPECTED_MCP_TOOL_NAMES = [
    "fetch_paper_details",
    "get_report_preview",
    "get_run_status",
    "search_papers",
    "start_research_run",
]


@dataclass
class DeploymentSmokeResult:
    base_url: str
    web_base_path: str
    web_app_ok: bool
    mcp_initialize_ok: bool
    tools_list_ok: bool
    metadata_ok: bool
    unauthorized_challenge_ok: bool
    authenticated_read_ok: str
    warnings: list[str]
    failures: list[str]
    checked_tool_names: list[str]
    checked_metadata_summary: dict[str, Any]
    generated_at: str

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


def deployment_smoke_failed(result: DeploymentSmokeResult, *, fail_on_warning: bool = False) -> bool:
    if result.failures:
        return True
    if fail_on_warning and result.warnings:
        return True
    return False


def render_deployment_smoke_markdown(result: DeploymentSmokeResult) -> str:
    lines = [
        "# Deployment smoke report",
        "",
        f"- base_url: `{result.base_url}`",
        f"- web_base_path: `{result.web_base_path}`",
        f"- web_app_ok: `{result.web_app_ok}`",
        f"- mcp_initialize_ok: `{result.mcp_initialize_ok}`",
        f"- tools_list_ok: `{result.tools_list_ok}`",
        f"- metadata_ok: `{result.metadata_ok}`",
        f"- unauthorized_challenge_ok: `{result.unauthorized_challenge_ok}`",
        f"- authenticated_read_ok: `{result.authenticated_read_ok}`",
        f"- generated_at: `{result.generated_at}`",
        "",
    ]
    if result.checked_tool_names:
        lines.append("## Checked tool names")
        lines.append("")
        for name in result.checked_tool_names:
            lines.append(f"- `{name}`")
        lines.append("")
    if result.checked_metadata_summary:
        lines.append("## Metadata summary")
        lines.append("")
        for key, value in result.checked_metadata_summary.items():
            lines.append(f"- `{key}`: `{value}`")
        lines.append("")
    if result.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    if result.failures:
        lines.append("## Failures")
        lines.append("")
        for failure in result.failures:
            lines.append(f"- {failure}")
        lines.append("")
    else:
        lines.append("## Result")
        lines.append("")
        lines.append("- All required checks passed.")
        lines.append("")
    return "\n".join(lines)


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _normalize_web_base_path(value: str) -> str:
    cleaned = (value or "/app").strip() or "/app"
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned.rstrip("/") or "/"


def _json_rpc_result(client: httpx.Client, method: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/mcp", json=payload, headers=headers)


def _is_html_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        return True
    body = response.text.lstrip().lower()
    return body.startswith("<!doctype html") or body.startswith("<html")


def _extract_auth_challenge_details(payload: dict[str, Any]) -> tuple[str | None, list[str]]:
    if not isinstance(payload, dict):
        return None, []
    if isinstance(payload.get("resource_metadata"), str):
        return payload["resource_metadata"], []

    result = payload.get("result")
    if not isinstance(result, dict):
        return None, []
    structured = result.get("structuredContent")
    resource_metadata = structured.get("resource_metadata") if isinstance(structured, dict) else None
    meta = result.get("_meta") or {}
    challenges = meta.get("mcp/www_authenticate") if isinstance(meta, dict) else []
    if isinstance(challenges, str):
        challenges = [challenges]
    if not isinstance(challenges, list):
        challenges = []
    normalized_challenges = [str(item) for item in challenges if str(item).strip()]
    return (str(resource_metadata) if isinstance(resource_metadata, str) else None), normalized_challenges


def run_deployment_smoke(
    *,
    base_url: str,
    web_base_path: str = "/app",
    bearer_token: str | None = None,
    run_id: str | None = None,
    timeout_seconds: float = 10.0,
) -> DeploymentSmokeResult:
    normalized_base_url = _normalize_base_url(base_url)
    normalized_web_base_path = _normalize_web_base_path(web_base_path)

    warnings: list[str] = []
    failures: list[str] = []
    checked_tool_names: list[str] = []
    metadata_summary: dict[str, Any] = {}

    with httpx.Client(base_url=normalized_base_url, follow_redirects=True, timeout=timeout_seconds) as client:
        web_response = client.get(normalized_web_base_path)
        web_app_ok = web_response.status_code == 200 and _is_html_response(web_response)
        if not web_app_ok:
            failures.append(f"{normalized_web_base_path} did not return a usable SPA shell.")

        initialize_response = _json_rpc_result(client, "initialize")
        mcp_initialize_ok = initialize_response.status_code == 200 and "result" in initialize_response.json()
        if not mcp_initialize_ok:
            failures.append("/mcp initialize failed.")

        tools_list_response = _json_rpc_result(client, "tools/list")
        tools_list_ok = False
        if tools_list_response.status_code == 200:
            tools_payload = tools_list_response.json()
            tools = tools_payload.get("result", {}).get("tools", [])
            checked_tool_names = sorted(tool.get("name") for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str))
            tools_list_ok = checked_tool_names == EXPECTED_MCP_TOOL_NAMES
        if not tools_list_ok:
            failures.append(f"tools/list mismatch: expected {EXPECTED_MCP_TOOL_NAMES}, got {checked_tool_names}")

        metadata_root_response = client.get("/.well-known/oauth-protected-resource")
        metadata_mcp_response = client.get("/.well-known/oauth-protected-resource/mcp")
        metadata_ok = False
        if metadata_root_response.status_code == 200 and metadata_mcp_response.status_code == 200:
            metadata = metadata_mcp_response.json()
            metadata_summary = {
                "resource": metadata.get("resource"),
                "resource_name": metadata.get("resource_name"),
                "authorization_servers": list(metadata.get("authorization_servers") or []),
                "scopes_supported": list(metadata.get("scopes_supported") or []),
            }
            required_keys = {"resource", "authorization_servers", "scopes_supported", "resource_name"}
            metadata_ok = required_keys.issubset(metadata.keys())
        if not metadata_ok:
            failures.append("Protected-resource metadata is missing expected connector-facing fields.")

        unauthorized_response = _json_rpc_result(
            client,
            "tools/call",
            params={"name": "search_papers", "arguments": {"run_id": "staging-smoke-run", "page_size": 1}},
        )
        unauthorized_status_ok = unauthorized_response.status_code in {401, 403}
        unauthorized_header = unauthorized_response.headers.get("WWW-Authenticate", "")
        unauthorized_payload = {}
        try:
            unauthorized_payload = unauthorized_response.json()
        except Exception:
            unauthorized_payload = {}
        unauthorized_resource_metadata, unauthorized_meta_challenges = _extract_auth_challenge_details(unauthorized_payload)
        unauthorized_challenge_ok = (
            unauthorized_status_ok
            and bool(unauthorized_header)
            and "resource_metadata" in unauthorized_header
            and bool(unauthorized_resource_metadata)
            and (not unauthorized_meta_challenges or any("resource_metadata" in item for item in unauthorized_meta_challenges))
        )
        if not unauthorized_challenge_ok:
            failures.append("Unauthorized protected MCP tool call did not return the expected auth challenge.")

        authenticated_read_ok = "skipped"
        if bearer_token and run_id:
            authenticated_response = _json_rpc_result(
                client,
                "tools/call",
                params={"name": "get_run_status", "arguments": {"run_id": run_id}},
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
            if authenticated_response.status_code == 200:
                payload = authenticated_response.json()
                structured = payload.get("result", {}).get("structuredContent", {})
                if isinstance(structured, dict) and structured.get("run_id") == run_id:
                    authenticated_read_ok = "passed"
                else:
                    authenticated_read_ok = "failed"
                    failures.append("Authenticated read-only MCP smoke returned an unexpected payload.")
            else:
                authenticated_read_ok = "failed"
                failures.append(f"Authenticated read-only MCP smoke failed with status {authenticated_response.status_code}.")
        elif bearer_token or run_id:
            authenticated_read_ok = "skipped"
            warnings.append("Authenticated read smoke was skipped because both --bearer-token and --run-id are required.")

    return DeploymentSmokeResult(
        base_url=normalized_base_url,
        web_base_path=normalized_web_base_path,
        web_app_ok=web_app_ok,
        mcp_initialize_ok=mcp_initialize_ok,
        tools_list_ok=tools_list_ok,
        metadata_ok=metadata_ok,
        unauthorized_challenge_ok=unauthorized_challenge_ok,
        authenticated_read_ok=authenticated_read_ok,
        warnings=warnings,
        failures=failures,
        checked_tool_names=checked_tool_names,
        checked_metadata_summary=metadata_summary,
        generated_at=datetime.now(UTC).isoformat(),
    )

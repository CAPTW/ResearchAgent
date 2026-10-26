"""Direct HTTP MCP helper for Stitch handoff extraction."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "https://stitch.googleapis.com/mcp"
API_KEY_ENV = "STITCH_API_KEY"
API_KEY_HEADER = "X-Goog-Api-Key"
DEFAULT_TIMEOUT_SECONDS = 30.0

WRITE_PREFIXES = ("create_", "update_", "delete_", "apply_", "generate_", "edit_", "remove_")
READ_PREFIXES = ("get_", "list_", "search_", "check_")
CANDIDATE_KEYWORDS = (
    "screen",
    "page",
    "design",
    "component",
    "layout",
    "node",
    "frame",
    "artifact",
    "image",
    "screenshot",
    "preview",
    "thumbnail",
    "dna",
    "token",
    "theme",
    "export",
)


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, raw in value.items():
            key_l = str(key).lower()
            sensitive = (
                key_l in {"api_key", "api-token", "api-token", "api key", "secret", "password", "auth", "authorization"}
                or key_l.endswith("_api_key")
                or key_l.startswith("x-goog-api")
                or key_l.endswith("_secret")
                or key_l.endswith("secret")
                or key_l == "access_token"
                or key_l == "refresh_token"
            )
            if sensitive:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(raw)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(entry) for entry in value]
    return value


def _write_json(path: Path, payload: Any, *, redact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _redact_payload(payload) if redact else payload
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


class DirectHandoffError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class DirectToolCall:
    tool_name: str
    arguments: dict[str, Any]
    request_payload: dict[str, Any]
    success: bool
    raw_output: dict[str, Any] | None = None
    reason: str | None = None


@dataclass
class DirectHandoffResult:
    label: str
    source: str
    endpoint: str
    selected_project_id: str | None
    selected_project_name: str | None
    available_tools: list[dict[str, Any]]
    project_metadata: dict[str, Any] | None
    extracted_design_context: dict[str, Any] | None
    limitations: list[str]
    generated_at: str
    tools_list_count: int
    projects_count: int
    api_initialized: bool
    failure_reason: str | None = None
    artifacts_written: list[str] | None = None
    project_selected: bool = False
    tool_calls: list[DirectToolCall] = field(default_factory=list)


def _post_jsonrpc(endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", API_KEY_HEADER: api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise DirectHandoffError("auth_failed" if exc.code in {401, 403} else "endpoint_failed", f"HTTP {exc.code}: {exc.reason}")
    except error.URLError as exc:
        raise DirectHandoffError("endpoint_failed", f"URLError: {exc}")
    except Exception as exc:
        raise DirectHandoffError("endpoint_failed", f"Request failed: {exc}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DirectHandoffError("endpoint_failed", f"Invalid JSON: {exc}")


def _call(
    endpoint: str,
    api_key: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int,
    request_fn: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = request_fn(endpoint, api_key, payload) if request_fn is not None else _post_jsonrpc(endpoint, api_key, payload)
    if not isinstance(response, dict):
        raise DirectHandoffError("endpoint_failed", "Response is not JSON object.")
    if "error" in response:
        error_payload = response["error"]
        if isinstance(error_payload, dict):
            code = error_payload.get("code")
            message = str(error_payload.get("message", error_payload))
        else:
            code = None
            message = str(error_payload)
        raise DirectHandoffError("auth_failed" if code in {401, 403} else "endpoint_failed", message)
    if "result" not in response:
        raise DirectHandoffError("endpoint_failed", "Missing JSON-RPC result.")
    return response


def _extract_content(payload: dict[str, Any]) -> Any:
    result = payload["result"]
    if not isinstance(result, dict):
        return result
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


def _extract_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("tools", "projects", "screens", "items", "designSystems"):
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _normalize_tools(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in _extract_list(raw):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        description = str(entry.get("description", "")).strip()
        schema = entry.get("inputSchema") if isinstance(entry.get("inputSchema"), dict) else entry.get("input_schema", {})
        out.append({"name": name, "description": description, "inputSchema": schema})
    return out


def _normalize_projects(raw: Any) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for entry in _extract_list(raw):
        if not isinstance(entry, dict):
            continue
        project_id = str(entry.get("name") or entry.get("id") or "").strip()
        if not project_id:
            continue
        title = str(entry.get("title") or entry.get("displayName") or project_id).strip()
        normalized = {"name": project_id, "title": title, "raw": entry}
        projects.append(normalized)
    return projects


def _read_only(tool_name: str, description: str) -> bool:
    name = tool_name.lower()
    if any(name.startswith(prefix) for prefix in WRITE_PREFIXES):
        return False
    if any(name.startswith(prefix) for prefix in READ_PREFIXES):
        return True
    ldesc = description.lower()
    return any(token in ldesc for token in ("returns", "retrieves", "lists", "fetch", "details"))


def _supports_project_id_input(schema: dict[str, Any] | None) -> bool:
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    return isinstance(props, dict) and any(
        key in props for key in ("name", "projectId", "project_id", "project", "projectname")
    )


def _keyword_matches(text: str) -> list[str]:
    lower = text.lower()
    return [keyword for keyword in CANDIDATE_KEYWORDS if keyword in lower]


def _is_candidate_tool(tool: dict[str, Any]) -> bool:
    blob = " ".join(
        [str(tool.get("name", "")), str(tool.get("description", "")), json.dumps(tool.get("inputSchema", {}), ensure_ascii=False)]
    )
    return bool(_keyword_matches(blob))


def _analyze_tools(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    rows: list[dict[str, Any]] = []
    safe_read: list[str] = []
    candidate: list[str] = []
    unsafe: list[str] = []
    unknown: list[str] = []

    for tool in tools:
        name = str(tool.get("name", ""))
        desc = str(tool.get("description", ""))
        read_only = _read_only(name, desc)
        schema = tool.get("inputSchema", {})
        supports_project_id = _supports_project_id_input(schema)
        matches = _keyword_matches(f"{name} {desc} {json.dumps(schema)}")
        candidate_tool = bool(matches)
        if read_only:
            safe_read.append(name)
            if candidate_tool:
                candidate.append(name)
            else:
                unknown.append(name)
            category = "candidate_handoff_tools" if candidate_tool else "safe_read_tools"
        else:
            unsafe.append(name)
            category = "unsafe_write_tools"
        rows.append(
            {
                "name": name,
                "description": desc,
                "input_schema": schema,
                "read_only": read_only,
                "supports_project_id_or_name": supports_project_id,
                "matches": matches,
                "candidate_handoff_tool": candidate_tool,
                "category": category,
                "output_expectation": (desc.split(".")[0][:140] + "...") if len(desc) > 140 else (desc.split(".")[0] or "Tool output."),
            }
        )
    return rows, {
        "safe_read_tools": safe_read,
        "candidate_handoff_tools": candidate,
        "unsafe_write_tools": unsafe,
        "unknown_tools": unknown,
    }


def _write_tool_audit(tools: list[dict[str, Any]], out_dir: Path, source_file: Path) -> tuple[Path, Path]:
    rows, classified = _analyze_tools(tools)
    json_payload = {
        "generated_at": _utcnow(),
        "source_file": str(source_file),
        "tool_count": len(rows),
        "tools": [
            {
                "name": row["name"],
                "description": row["description"],
                "input_schema": row["input_schema"],
                "output_expectation": row["output_expectation"],
                "read_only": row["read_only"],
                "supports_project_id_or_name": row["supports_project_id_or_name"],
                "matches": row["matches"],
                "candidate_handoff_tool": row["candidate_handoff_tool"],
                "category": row["category"],
            }
            for row in rows
        ],
        **classified,
    }
    json_path = out_dir / "tool-schema-audit.json"
    md_path = out_dir / "tool-schema-audit.md"
    _write_json(json_path, json_payload, redact=False)
    lines = [
        "# Stitch Tool Schema Audit",
        "",
        f"Generated at: {_utcnow()}",
        f"Source file: {source_file}",
        f"Tool count: {len(rows)}",
        "",
        "## Tool schema table",
        "",
        "| tool | read_only | supports project/id/name | output expectation | keyword matches | candidate_handoff |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {name} | {ro} | {pid} | {out} | {matches} | {candidate} |".format(
                name=row["name"],
                ro="true" if row["read_only"] else "false",
                pid="true" if row["supports_project_id_or_name"] else "false",
                out=row["output_expectation"].replace("|", "\\|"),
                matches=", ".join(row["matches"]),
                candidate="true" if row["candidate_handoff_tool"] else "false",
            )
        )
    lines.extend(
        [
            "",
            "## Classification",
            f"- safe_read_tools: {', '.join(row['name'] for row in rows if row['category'] in {'safe_read_tools', 'candidate_handoff_tools'}) or 'none'}",
            f"- candidate_handoff_tools: {', '.join(classified['candidate_handoff_tools']) or 'none'}",
            f"- unsafe_write_tools: {', '.join(classified['unsafe_write_tools']) or 'none'}",
            f"- unknown_tools: {', '.join(classified['unknown_tools']) or 'none'}",
            "",
            "## Notes",
            "- This audit was derived from tool schemas in `stitch-tools.json`.",
        ]
    )
    _write_text(md_path, "\n".join(lines))
    return json_path, md_path


def _safe_filename(raw: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("_") or "resource"
    return normalized[:120]


def _select_project(projects: list[dict[str, Any]], project_id: str | None, project_index: int | None) -> tuple[str | None, str | None]:
    if project_id:
        for project in projects:
            if project.get("name") == project_id:
                return project["name"], project.get("title")
        return project_id, project_id
    if project_index is not None:
        if 0 <= project_index < len(projects):
            item = projects[project_index]
            return item.get("name"), item.get("title")
    return None, None


def _project_without_prefix(project_id: str) -> str:
    return project_id[9:] if project_id.startswith("projects/") else project_id


def _default_tool_args(tool: dict[str, Any], project_id: str) -> dict[str, Any]:
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    args: dict[str, Any] = {}
    tool_name = str(tool.get("name", ""))
    if "name" in props and tool_name != "get_screen":
        args["name"] = project_id
    if "projectId" in props:
        args["projectId"] = _project_without_prefix(project_id)
    if "project_id" in props:
        args["project_id"] = project_id
    if "project" in props:
        args["project"] = project_id
    return args


def _validate_and_build_tool_args(
    tool: dict[str, Any], project_id: str, args_json: str | None
) -> tuple[dict[str, Any], list[str]]:
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", []) if isinstance(schema, dict) else [])

    args = _default_tool_args(tool, project_id)
    if args_json is None:
        missing = required - set(args.keys())
        return args, [f"required args missing: {', '.join(sorted(missing))}"] if missing else []

    try:
        user_args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --args-json payload: {exc}")
    if not isinstance(user_args, dict):
        raise ValueError("--args-json payload must be an object")

    extras = set(user_args.keys()) - set(props.keys())
    if extras:
        raise ValueError(f"unexpected args: {', '.join(sorted(extras))}")

    args.update(user_args)
    missing = required - set(args.keys())
    return args, [f"required args missing: {', '.join(sorted(missing))}"] if missing else []


def _extract_image_refs(value: Any, parent_key: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            next_key = f"{parent_key}.{key}" if parent_key else str(key)
            if isinstance(nested, (dict, list)):
                found.extend(_extract_image_refs(nested, next_key))
            elif isinstance(nested, str):
                low = nested.lower()
                if low.startswith("http://") or low.startswith("https://") or (low.startswith("data:") and "base64" in low):
                    found.append((next_key, nested))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            next_key = f"{parent_key}[{index}]"
            if isinstance(nested, (dict, list)):
                found.extend(_extract_image_refs(nested, next_key))
            elif isinstance(nested, str):
                low = nested.lower()
                if low.startswith("http://") or low.startswith("https://") or (low.startswith("data:") and "base64" in low):
                    found.append((next_key, nested))
    return found


def _write_image(url: str, path: Path) -> bool:
    try:
        if url.startswith("data:") and "base64" in url.lower():
            _, encoded = url.split(",", 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded))
            return True
        req = Request(url, method="GET")
        with urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.read())
        return True
    except Exception:
        return False


def _collect_nested_match(payload: Any, keys: set[str]) -> list[Any]:
    results: list[Any] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            lower_key = str(key).lower()
            if any(k in lower_key for k in keys):
                results.append(value)
            if isinstance(value, (dict, list)):
                results.extend(_collect_nested_match(value, keys))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)):
                results.extend(_collect_nested_match(item, keys))
    return results


def _build_extracted_design_context(
    project_metadata: dict[str, Any] | None,
    candidate_payloads: list[dict[str, Any]],
    image_refs: list[tuple[str, str]],
) -> dict[str, Any]:
    context: dict[str, Any] = {"source": "Stitch MCP direct HTTP"}
    if project_metadata:
        theme = project_metadata.get("designTheme", {})
        if isinstance(theme, dict):
            if theme.get("namedColors"):
                context["colors"] = theme["namedColors"]
            if theme.get("typography"):
                context["typography"] = theme["typography"]
            if theme.get("spacing"):
                context["spacing"] = theme["spacing"]
            elif theme.get("spacingScale"):
                context["spacing"] = theme["spacingScale"]
            if theme.get("designMd"):
                context["design_markdown"] = theme["designMd"]
            context["tokens"] = {
                "named_colors": theme.get("namedColors", {}),
                "typography": theme.get("typography", {}),
                "spacing": theme.get("spacing", {}),
            }
    if project_metadata:
        raw_tokens = project_metadata.get("metadata")
        if isinstance(raw_tokens, dict):
            dm = raw_tokens.get("designMd")
            if isinstance(dm, str) and dm.strip():
                context["design_markdown"] = dm
    for payload in candidate_payloads:
        design_systems = payload.get("designSystems")
        if isinstance(design_systems, list) and design_systems:
            design_system = design_systems[0]
            if isinstance(design_system, dict):
                system = design_system.get("designSystem")
                if isinstance(system, dict):
                    theme = system.get("theme")
                    if isinstance(theme, dict):
                        context.setdefault("colors", theme.get("namedColors", context.get("colors")))
                        context.setdefault("spacing", theme.get("spacing", theme.get("spacingScale")))
                        context.setdefault("typography", theme.get("typography"))
                        if theme.get("namedColors"):
                            context.setdefault("tokens", {})
                            context["tokens"].setdefault("named_colors", theme.get("namedColors"))
                    design_md = system.get("designMd")
                    if not context.get("design_markdown") and isinstance(design_md, str):
                        context["design_markdown"] = design_md
    layout = _collect_nested_match(candidate_payloads, {"layout", "layoutjson", "layout_config", "grid"})
    hierarchy = _collect_nested_match(candidate_payloads, {"component", "components", "nodes", "hierarchy", "tree"})
    if layout:
        context["layout_structure"] = layout[0]
    if hierarchy:
        context["component_hierarchy"] = hierarchy[0]
    if image_refs:
        context["screen_images"] = [url for _, url in image_refs]
    return context


def _build_completeness(
    project_metadata: dict[str, Any] | None,
    extracted_context: dict[str, Any] | None,
    notes_exists: bool,
) -> dict[str, Any]:
    has_project_metadata = bool(project_metadata)
    has_tokens = False
    has_typography = False
    has_colors = False
    has_spacing = False
    if project_metadata and isinstance(project_metadata, dict):
        design = project_metadata.get("designTheme", {})
        if isinstance(design, dict):
            has_typography = bool(design.get("typography")) or bool(design.get("font")) or bool(design.get("headlineFont"))
            has_colors = bool(design.get("namedColors"))
            has_spacing = bool(design.get("spacing")) or bool(design.get("spacingScale"))
            has_tokens = has_colors
    if extracted_context:
        has_tokens = has_tokens or bool(extracted_context.get("tokens"))
        has_typography = has_typography or bool(extracted_context.get("typography"))
        has_colors = has_colors or bool(extracted_context.get("colors"))
        has_spacing = has_spacing or bool(extracted_context.get("spacing"))

    has_thumbnail = bool(project_metadata and project_metadata.get("thumbnailScreenshot"))
    has_screen_image = bool(extracted_context and extracted_context.get("screen_images"))
    has_layout_structure = bool(extracted_context and extracted_context.get("layout_structure"))
    has_component_hierarchy = bool(extracted_context and extracted_context.get("component_hierarchy"))
    enough = has_project_metadata and has_tokens and (has_layout_structure or has_component_hierarchy or has_screen_image)
    missing: list[str] = []
    if not has_project_metadata:
        missing.append("project_metadata")
    if not has_tokens:
        missing.append("tokens")
    if not (has_layout_structure or has_component_hierarchy or has_screen_image):
        missing.append("layout_or_component_or_image")
    return {
        "has_project_metadata": has_project_metadata,
        "has_tokens": has_tokens,
        "has_typography": has_typography,
        "has_colors": has_colors,
        "has_spacing": has_spacing,
        "has_thumbnail": has_thumbnail,
        "has_screen_image": has_screen_image,
        "has_layout_structure": has_layout_structure,
        "has_component_hierarchy": has_component_hierarchy,
        "has_implementation_notes": notes_exists,
        "enough_for_implementation": enough,
        "missing_fields": missing,
        "recommended_next_action": "Proceed to implementation handoff."
        if enough
        else "Expand handoff by running --auto-discover-handoff and --save-images with a screen-capable tool response.",
    }


def _write_completeness(out_dir: Path, project_id: str | None, report: dict[str, Any]) -> None:
    payload = {"selected_project_id": project_id, **report}
    _write_json(out_dir / "handoff-completeness.json", payload, redact=False)
    lines = [
        "# Stitch Handoff Completeness Report",
        f"Generated at: {_utcnow()}",
        f"Project: `{project_id}`",
        "",
        f"enough_for_implementation: {report['enough_for_implementation']}",
        f"missing_fields: {', '.join(report['missing_fields']) or 'none'}",
        f"recommended_next_action: {report['recommended_next_action']}",
        "",
        "## Field checks",
    ]
    for key in [
        "has_project_metadata",
        "has_tokens",
        "has_typography",
        "has_colors",
        "has_spacing",
        "has_thumbnail",
        "has_screen_image",
        "has_layout_structure",
        "has_component_hierarchy",
        "has_implementation_notes",
    ]:
        lines.append(f"- {key}: {report[key]}")
    _write_text(out_dir / "handoff-completeness.md", "\n".join(lines))


def _serialize_tool_calls(tool_calls: list[DirectToolCall]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for call in tool_calls:
        entry: dict[str, Any] = {
            "tool_name": call.tool_name,
            "arguments": call.arguments,
            "request_payload": call.request_payload,
            "success": call.success,
        }
        if call.reason is not None:
            entry["reason"] = call.reason
        serialized.append(entry)
    return serialized


def _build_handoff_payload(
    *,
    selected_project_id: str | None,
    selected_project_name: str | None,
    tools: list[dict[str, Any]],
    project_metadata: dict[str, Any] | None,
    extracted_design_context: dict[str, Any] | None,
    limitations: list[str],
    generated_at: str,
    endpoint: str,
    called_tools: list[DirectToolCall] | None = None,
    completeness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    design_theme = {}
    if isinstance(project_metadata, dict):
        design_theme = project_metadata.get("designTheme", {})
    if not isinstance(design_theme, dict):
        design_theme = {}

    context = extracted_design_context or {}
    colors = context.get("colors", design_theme.get("namedColors", {}))
    typography = context.get("typography", design_theme.get("typography"))
    spacing = context.get("spacing", design_theme.get("spacing", design_theme.get("spacingScale")))
    if typography is None:
        typography = {}
    if colors is None:
        colors = {}
    if spacing is None:
        spacing = {}
    tokens = context.get("tokens")
    if not tokens:
        tokens = {
            "named_colors": design_theme.get("namedColors", {}),
            "typography": typography if isinstance(typography, dict) else {},
            "spacing": spacing if isinstance(spacing, dict) else {},
        }

    thumbnail_refs: list[str] = []
    if isinstance(project_metadata, dict):
        thumbnail = project_metadata.get("thumbnailScreenshot")
        if isinstance(thumbnail, dict):
            thumb_url = thumbnail.get("downloadUrl")
            if isinstance(thumb_url, str):
                thumbnail_refs.append(thumb_url)
        elif isinstance(thumbnail, str):
            thumbnail_refs.append(thumbnail)
    thumbnail_refs.extend(context.get("screen_images", []))
    # De-duplicate while preserving order
    thumbnail_or_screen_refs = []
    for item in thumbnail_refs:
        if item and item not in thumbnail_or_screen_refs:
            thumbnail_or_screen_refs.append(item)

    return {
        "source": "Stitch MCP direct HTTP",
        "endpoint": endpoint,
        "selected_project_id": selected_project_id,
        "selected_project_name": selected_project_name,
        "generated_at": generated_at,
        "available_tools": tools,
        "called_tools": _serialize_tool_calls(called_tools or []),
        "project_metadata": project_metadata,
        "design_theme": design_theme,
        "design_markdown": context.get("design_markdown"),
        "tokens": tokens,
        "typography": typography,
        "colors": colors,
        "spacing": spacing,
        "thumbnail_or_screen_refs": thumbnail_or_screen_refs,
        "layout_structure": context.get("layout_structure"),
        "component_hierarchy": context.get("component_hierarchy"),
        "implementation_notes": [
            "Source: Stitch MCP direct HTTP",
            f"Project: {selected_project_id}",
            f"Generated: {generated_at}",
            "Implementation guidance is derived from tool schema + extracted read-only responses.",
        ],
        "limitations": limitations,
        "extracted_design_context": context,
        "completeness": completeness,
    }


def _write_optional_handoff_artifacts(
    out_dir: Path,
    project_id: str | None,
    project_name: str | None,
    extracted_design_context: dict[str, Any] | None,
) -> None:
    payload = {
        "selected_project_id": project_id,
        "selected_project_name": project_name,
        "generated_at": _utcnow(),
        "tokens": {
            "colors": {},
            "typography": {},
            "spacing": {},
            "raw": {},
        },
        "colors": {},
        "typography": {},
        "spacing": {},
    }
    if extracted_design_context:
        payload["tokens"]["raw"] = extracted_design_context.get("tokens", {})
        payload["tokens"]["colors"] = extracted_design_context.get("colors", {})
        payload["tokens"]["typography"] = extracted_design_context.get("typography", {})
        payload["tokens"]["spacing"] = extracted_design_context.get("spacing", {})
        payload["colors"] = extracted_design_context.get("colors", {})
        payload["typography"] = extracted_design_context.get("typography", {})
        payload["spacing"] = extracted_design_context.get("spacing", {})
    _write_json(out_dir / "stitch-tokens.json", payload, redact=False)

    layout_payload = {
        "project_id": project_id,
        "project_name": project_name,
        "layout_structure": extracted_design_context.get("layout_structure") if extracted_design_context else None,
        "status": "found" if extracted_design_context and extracted_design_context.get("layout_structure") else "missing",
        "notes": "Recovered from tool call payload fields." if extracted_design_context and extracted_design_context.get("layout_structure") else "Not exposed by current Stitch MCP responses.",
    }
    _write_json(out_dir / "layout-structure.json", layout_payload, redact=False)

    component_payload = {
        "project_id": project_id,
        "project_name": project_name,
        "component_hierarchy": extracted_design_context.get("component_hierarchy") if extracted_design_context else None,
        "status": "found" if extracted_design_context and extracted_design_context.get("component_hierarchy") else "missing",
        "notes": "Recovered from tool call payload fields." if extracted_design_context and extracted_design_context.get("component_hierarchy") else "Not exposed by current Stitch MCP responses.",
    }
    _write_json(out_dir / "component-hierarchy.json", component_payload, redact=False)


def _write_dashboard_brief(
    out_dir: Path,
    project_id: str,
    project_name: str,
    extracted_context: dict[str, Any] | None,
    completeness: dict[str, Any] | None,
    manual_needed: bool,
) -> None:
    selected_variant = "Research Cockpit"
    colors = extracted_context.get("colors", {}) if isinstance(extracted_context, dict) else {}
    typography = extracted_context.get("typography", {}) if isinstance(extracted_context, dict) else {}
    spacing = extracted_context.get("spacing", {}) if isinstance(extracted_context, dict) else {}
    lines = [
        "# Dashboard Redesign Brief (Stitch Direct Handoff)",
        "",
        "## 1) Original Stitch project",
        f"- Project ID: `{project_id}`",
        f"- Project name: `{project_name}`",
        "- Source: Stitch MCP direct HTTP handoff",
        f"- Selected variant: `{selected_variant}`",
        "",
        "## 2) Current app problem summary",
        "- Existing dashboard is card-heavy and does not prioritize workflow actions for wide desktop.",
        "- Primary actions, run controls, and status are not clearly separated in a cockpit-like structure.",
        "- Add/Edit profile usage should not occupy persistent main surface area.",
        "",
        "## 3) Stitch-confirmed design direction",
        "- Color/typography tokens are available in Stitch design theme metadata.",
        f"- Color keys available: {list(colors.keys())[:10] if isinstance(colors, dict) else 'n/a'}",
        f"- Typography payload available: {'yes' if isinstance(typography, dict) else 'no'}",
        f"- Spacing payload available: {'yes' if isinstance(spacing, dict) else 'no'}",
        "- Design tone indicates layered dark surfaces, restrained contrast, and readable label/value hierarchy.",
        "",
        "## 4) Wide desktop target",
        "- 1440px: first-class 3-column information density.",
        "- 1600px: expand command + table + inspector density while keeping action visibility.",
        "- 1920px: ensure action chips, status row, and top run table are above fold.",
        "",
        "## 5) Recommended IA",
        "- Sidebar: Dashboard / Topics / Runs / Evidence / Reports / Compare / Automations / Settings",
        "- Command bar: new research, start selected profiles, compare selected runs, open latest report, add topic profile",
        "- Main workbench: run table, topic portfolio, schedule status queue",
        "- Right inspector: selected-run/profile details and compact status chips",
        "- Profile editing: drawer/modal rather than inline dominant panel",
        "",
        "## 6) Implementation preservation requirements",
        "- topic profile CRUD, multi-profile selection, bulk start, compare selected runs, rerun entry",
        "- settings/status, saved sessions/views, schedule result awareness",
        "",
        "## 7) Truthfulness constraints",
        "- Show `provider` state explicitly and keep `fallback_only` when present.",
        "- Keep Stitch status as `Seam prepared` unless integrated path is proven.",
        "",
        "## 8) Completeness",
    ]
    if completeness:
        lines.append(f"- enough_for_implementation: {completeness.get('enough_for_implementation', False)}")
        lines.append(f"- missing_fields: {', '.join(completeness.get('missing_fields', [])) or 'none'}")
    if manual_needed:
        lines.extend(["- manual_export_required: true"])
    _write_text(out_dir / "dashboard-redesign-brief.md", "\n".join(lines))


def _write_manual_export_request(
    out_dir: Path,
    *,
    project_id: str,
    project_name: str,
    selected: list[str] | None = None,
) -> None:
    missing = selected or []
    required_items = [
        "wide desktop screen PNG at 1920px",
        "1440px screen PNG",
        "narrow responsive PNG",
        "Design DNA / implementation handoff text",
        "component hierarchy or screen structure",
        "tokens: colors, typography, spacing, radius, shadows",
        "selected variant name used for implementation",
    ]
    missing_block = "\n".join(f"- {item}" for item in required_items)
    requested = "\n".join(f"- {item}" for item in missing) if missing else "None listed."
    _write_text(
        out_dir / "manual-export-request.md",
        "\n".join(
            [
                "# Manual Export Request for Stitch Handoff",
                "",
                f"- Project ID: `{project_id}`",
                f"- Project name: `{project_name}`",
                "",
                "## Missing fields from MCP extraction",
                f"{missing_block}" if missing_block else "- no missing fields",
                "",
                "## Requested export items",
                "- Please export and provide these before proceeding to implementation:",
                requested,
                "",
                "## Notes for implementation review",
                "- sidebar",
                "- command bar",
                "- run table",
                "- topic portfolio",
                "- right inspector",
                "- profile drawer/modal",
            ]
        ),
    )


def _write_tool_details(out_dir: Path, out_payload: dict[str, Any], *, redact: bool) -> None:
    _write_json(out_dir / "tool-schema-audit-details.json", out_payload, redact=redact)


def run_direct_handoff(
    *,
    project_id: str | None,
    project_index: int | None,
    out_dir: Path | str,
    json_out: Path | str,
    dry_run: bool = False,
    redact: bool = False,
    request_fn: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    list_tools_detail: bool = False,
    call_tool: str | None = None,
    args_json: str | None = None,
    auto_discover_handoff: bool = False,
    save_images: bool = False,
    completeness_report: bool = False,
) -> DirectHandoffResult:
    out_dir_path = Path(out_dir)
    json_out_path = Path(json_out)
    artifacts: list[str] = []
    tool_calls: list[DirectToolCall] = []
    limitations: list[str] = []
    selected_project_name: str | None = None

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        return DirectHandoffResult(
            label="Blocked: Codex runtime env missing STITCH_API_KEY",
            source="Stitch MCP direct HTTP",
            endpoint=endpoint,
            selected_project_id=None,
            selected_project_name=None,
            available_tools=[],
            project_metadata=None,
            extracted_design_context=None,
            limitations=["env_missing"],
            generated_at=_utcnow(),
            tools_list_count=0,
            projects_count=0,
            api_initialized=False,
            failure_reason="env_missing",
            artifacts_written=[],
            project_selected=False,
        )

    request_id = 1
    generated_at = _utcnow()

    # initialize
    try:
        _call(
            endpoint,
            api_key,
            "initialize",
            {"protocolVersion": "2024-11-05"},
            request_id=request_id,
            request_fn=request_fn,
        )
        api_initialized = True
        request_id += 1
    except DirectHandoffError as exc:
        return DirectHandoffResult(
            label="Blocked: Stitch auth failed" if exc.reason == "auth_failed" else "Blocked: Stitch endpoint failed",
            source="Stitch MCP direct HTTP",
            endpoint=endpoint,
            selected_project_id=None,
            selected_project_name=None,
            available_tools=[],
            project_metadata=None,
            extracted_design_context=None,
            limitations=[exc.reason],
            generated_at=generated_at,
            tools_list_count=0,
            projects_count=0,
            api_initialized=False,
            failure_reason=exc.reason,
            artifacts_written=[],
            project_selected=False,
        )

    # tools/list
    tools_resp = _call(endpoint, api_key, "tools/list", None, request_id=request_id, request_fn=request_fn)
    request_id += 1
    tools_payload = _extract_content(tools_resp)
    tools = _normalize_tools(tools_payload)
    if not dry_run:
        _write_json(out_dir_path / "stitch-tools.json", {"tools": tools}, redact=redact)
        artifacts.append(str(out_dir_path / "stitch-tools.json"))
    if list_tools_detail:
        _write_tool_audit(tools, out_dir_path, out_dir_path / "stitch-tools.json")
        if not dry_run:
            artifacts.extend([str(out_dir_path / "tool-schema-audit.json"), str(out_dir_path / "tool-schema-audit.md")])

    # list_projects
    projects: list[dict[str, Any]] = []
    if any(tool.get("name") == "list_projects" for tool in tools):
        try:
            list_projects_req = {
                "name": "list_projects",
                "arguments": {},
            }
            list_projects_resp = _call(
                endpoint,
                api_key,
                "tools/call",
                list_projects_req,
                request_id=request_id,
                request_fn=request_fn,
            )
            request_id += 1
            project_payload = _extract_content(list_projects_resp)
            projects = _normalize_projects(project_payload)
            if not dry_run:
                _write_json(out_dir_path / "stitch-projects.json", {"projects": projects}, redact=redact)
                artifacts.append(str(out_dir_path / "stitch-projects.json"))
        except DirectHandoffError as exc:
            limitations.append(f"list_projects failed: {exc.reason}")

    selected_project_id, selected_project_name = _select_project(projects, project_id, project_index)
    if selected_project_id is None:
        return DirectHandoffResult(
            label="Blocked: no Stitch project selected",
            source="Stitch MCP direct HTTP",
            endpoint=endpoint,
            selected_project_id=None,
            selected_project_name=None,
            available_tools=tools,
            project_metadata=None,
            extracted_design_context=None,
            limitations=limitations,
            generated_at=_utcnow(),
            tools_list_count=len(tools),
            projects_count=len(projects),
            api_initialized=api_initialized,
            failure_reason="no_project_selected",
            artifacts_written=artifacts,
            project_selected=False,
        )

    # explicit call
    if call_tool:
        chosen = next((tool for tool in tools if tool.get("name") == call_tool), None)
        if chosen is None:
            return DirectHandoffResult(
                label=f"Blocked: tool {call_tool} not found",
                source="Stitch MCP direct HTTP",
                endpoint=endpoint,
                selected_project_id=selected_project_id,
                selected_project_name=selected_project_name,
                available_tools=tools,
                project_metadata=None,
                extracted_design_context=None,
                limitations=limitations + [f"tool not found: {call_tool}"],
                generated_at=_utcnow(),
                tools_list_count=len(tools),
                projects_count=len(projects),
                api_initialized=api_initialized,
                failure_reason="tool_not_found",
                artifacts_written=artifacts,
                project_selected=True,
            )
        if not _read_only(call_tool, str(chosen.get("description", ""))):
            return DirectHandoffResult(
                label=f"Blocked: {call_tool} is not read-only",
                source="Stitch MCP direct HTTP",
                endpoint=endpoint,
                selected_project_id=selected_project_id,
                selected_project_name=selected_project_name,
                available_tools=tools,
                project_metadata=None,
                extracted_design_context=None,
                limitations=limitations + [f"tool blocked for write: {call_tool}"],
                generated_at=_utcnow(),
                tools_list_count=len(tools),
                projects_count=len(projects),
                api_initialized=api_initialized,
                failure_reason="tool_not_read_only",
                artifacts_written=artifacts,
                project_selected=True,
            )
        try:
            explicit_args, arg_errors = _validate_and_build_tool_args(chosen, selected_project_id, args_json)
        except ValueError as exc:
            return DirectHandoffResult(
                label="Blocked: invalid --args-json payload",
                source="Stitch MCP direct HTTP",
                endpoint=endpoint,
                selected_project_id=selected_project_id,
                selected_project_name=selected_project_name,
                available_tools=tools,
                project_metadata=None,
                extracted_design_context=None,
                limitations=limitations + [str(exc)],
                generated_at=_utcnow(),
                tools_list_count=len(tools),
                projects_count=len(projects),
                api_initialized=api_initialized,
                failure_reason="invalid_args_json",
                artifacts_written=artifacts,
                project_selected=True,
            )
        if arg_errors:
            return DirectHandoffResult(
                label="Blocked: invalid --args-json payload",
                source="Stitch MCP direct HTTP",
                endpoint=endpoint,
                selected_project_id=selected_project_id,
                selected_project_name=selected_project_name,
                available_tools=tools,
                project_metadata=None,
                extracted_design_context=None,
                limitations=limitations + arg_errors,
                generated_at=_utcnow(),
                tools_list_count=len(tools),
                projects_count=len(projects),
                api_initialized=api_initialized,
                failure_reason="invalid_args_json",
                artifacts_written=artifacts,
                project_selected=True,
            )
        req = {"name": call_tool, "arguments": explicit_args}
        req_payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": req,
        }
        request_id += 1
        tool_resp = _call(endpoint, api_key, "tools/call", req, request_id=request_id - 1, request_fn=request_fn)
        if not dry_run:
            target = out_dir_path / "raw" / f"{_safe_filename(call_tool)}.json"
            _write_json(target, tool_resp, redact=redact)
            artifacts.append(str(target))
        tool_calls.append(DirectToolCall(call_tool, explicit_args, req_payload, True, raw_output=tool_resp))
        limitations.append(f"explicit_call:{call_tool}:ok")

    # get_project
    project_metadata: dict[str, Any] | None = None
    if selected_project_id:
        req = {"name": "get_project", "arguments": {"name": selected_project_id}}
        get_project_resp = _call(
            endpoint,
            api_key,
            "tools/call",
            req,
            request_id=request_id,
            request_fn=request_fn,
        )
        request_id += 1
        project_content = _extract_content(get_project_resp)
        if isinstance(project_content, dict):
            if project_content.get("name") == selected_project_id:
                project_metadata = project_content
            elif project_content.get("project") and isinstance(project_content["project"], dict):
                project_metadata = project_content["project"]
        if selected_project_name is None and isinstance(project_metadata, dict):
            selected_project_name = str(project_metadata.get("title") or project_metadata.get("name") or selected_project_id)

        preliminary_context = _build_extracted_design_context(project_metadata, [], [])
        if not dry_run:
            handoff_payload = _build_handoff_payload(
                selected_project_id=selected_project_id,
                selected_project_name=selected_project_name,
                tools=tools,
                project_metadata=project_metadata,
                extracted_design_context=preliminary_context,
                limitations=limitations,
                generated_at=generated_at,
                endpoint=endpoint,
                called_tools=tool_calls,
                completeness=None,
            )
            _write_json(json_out_path, handoff_payload, redact=redact)
            artifacts.append(str(json_out_path))
            _write_text(
                out_dir_path / "stitch-implementation-notes.md",
                "\n".join(
                    [
                        "# Stitch MCP Direct Handoff",
                        f"Generated at: {_utcnow()}",
                        "Source: Stitch MCP direct HTTP",
                        f"Endpoint: {endpoint}",
                        "",
                        "## Selection",
                        f"- Project ID: `{selected_project_id}`",
                        f"- Project name: `{selected_project_name}`",
                        "",
                        "## Discovery summary",
                        "- initialize: success",
                        f"- tools/list: {len(tools)}",
                        f"- list_projects: {len(projects)}",
                        "- get_project: success",
                        "",
                        "## Extracted design assets",
                        f"- design_theme keys: `{', '.join(sorted((project_metadata or {}).get('designTheme', {}).keys()) if isinstance((project_metadata or {}).get('designTheme', {}), dict) else [])}`",
                    ]
                )
            )
            artifacts.append(str(out_dir_path / "stitch-implementation-notes.md"))

    extracted_design_context: dict[str, Any] | None = None
    candidate_payloads: list[dict[str, Any]] = []
    image_refs: list[tuple[str, str]] = []

    # auto discovery
    if auto_discover_handoff and project_metadata is not None:
        _, classified = _analyze_tools(tools)
        for tool_name in classified.get("candidate_handoff_tools", []):
            if tool_name in {"generate_screen_from_text", "edit_screens", "generate_variants", "create_project"}:
                continue
            tool_def = next((tool for tool in tools if tool["name"] == tool_name), None)
            if tool_def is None or not _read_only(tool_name, str(tool_def.get("description", ""))):
                continue

            args = _default_tool_args(tool_def, selected_project_id)
            if tool_name == "get_screen":
                # get_screen requires a screen resource identifier; infer from list_screens by project.
                list_screens_payload = {"name": "list_screens", "arguments": {"projectId": _project_without_prefix(selected_project_id)}}
                try:
                    list_screens_resp = _call(
                        endpoint,
                        api_key,
                        "tools/call",
                        list_screens_payload,
                        request_id=request_id,
                        request_fn=request_fn,
                    )
                    request_id += 1
                    ls_payload = _extract_content(list_screens_resp)
                    screens = _extract_list(ls_payload)
                    if screens:
                        first = screens[0]
                        screen_name = str(first.get("name") or first.get("id") or "")
                        if screen_name:
                            args["name"] = screen_name
                            if "projectId" not in args:
                                args["projectId"] = _project_without_prefix(selected_project_id)
                            if "screenId" not in args:
                                if "/screens/" in screen_name:
                                    parsed_screen_id = screen_name.split("/screens/", 1)[1].split("/", 1)[0]
                                else:
                                    parsed_screen_id = _project_without_prefix(screen_name)
                                args["screenId"] = parsed_screen_id
                    else:
                        limitations.append("get_screen candidate skipped: no screens returned")
                        continue
                except DirectHandoffError as exc:
                    limitations.append(f"get_screen candidate skipped: {exc.reason}")
                    continue

            if tool_name == "get_screen" and "name" not in args:
                continue

            req = {"name": tool_name, "arguments": args}
            raw_req = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": req}
            request_id += 1
            try:
                tool_resp = _call(endpoint, api_key, "tools/call", req, request_id=request_id - 1, request_fn=request_fn)
                response_content = _extract_content(tool_resp)
                if isinstance(response_content, dict):
                    candidate_payloads.append(response_content)
                    image_refs.extend(_extract_image_refs(response_content))
                if not dry_run:
                    _write_json(out_dir_path / "raw" / f"{_safe_filename(tool_name)}.json", tool_resp, redact=redact)
                    artifacts.append(str(out_dir_path / "raw" / f"{_safe_filename(tool_name)}.json"))
                tool_calls.append(DirectToolCall(tool_name, args, raw_req, True, raw_output=tool_resp))
            except DirectHandoffError as exc:
                limitations.append(f"{tool_name} failed: {exc.reason}")
                tool_calls.append(DirectToolCall(tool_name, args, raw_req, False, raw_output=None, reason=exc.reason))

    if project_metadata:
        image_refs.extend(_extract_image_refs(project_metadata))
    extracted_design_context = _build_extracted_design_context(project_metadata, candidate_payloads, image_refs)
    report: dict[str, Any] | None = None
    if not dry_run:
        _write_optional_handoff_artifacts(
            out_dir=out_dir_path,
            project_id=selected_project_id,
            project_name=selected_project_name,
            extracted_design_context=extracted_design_context,
        )

        if save_images and image_refs:
            saved: set[str] = set()
            saved_screenshot = False
            for key, url in image_refs:
                if url in saved:
                    continue
                target: Path
                lower_key = key.lower()
                if "thumbnail" in lower_key or "thumb" in lower_key:
                    target = out_dir_path / "stitch-thumbnail.png"
                elif "screenshot" in lower_key or "preview" in lower_key:
                    target = out_dir_path / "stitch-screen-wide.png"
                    saved_screenshot = True
                else:
                    target = out_dir_path / "raw" / "images" / f"{_safe_filename(key)}.png"
                if _write_image(url, target):
                    saved.add(url)
                    if target.name == "stitch-screen-wide.png":
                        saved_screenshot = True
                    if not dry_run:
                        artifacts.append(str(target))
                else:
                    limitations.append(f"image_save_failed:{key}")
            thumbnail_path = out_dir_path / "stitch-thumbnail.png"
            screen_wide_path = out_dir_path / "stitch-screen-wide.png"
            if not dry_run and not saved_screenshot and thumbnail_path.exists():
                try:
                    screen_wide_path.write_bytes(thumbnail_path.read_bytes())
                    artifacts.append(str(screen_wide_path))
                except Exception:
                    limitations.append("image_copy_failed:stitch-screen-wide.png")

    if completeness_report:
        report = _build_completeness(project_metadata, extracted_design_context, (out_dir_path / "stitch-implementation-notes.md").exists())
        if not dry_run:
            _write_completeness(out_dir_path, selected_project_id, report)
            artifacts.extend([str(out_dir_path / "handoff-completeness.json"), str(out_dir_path / "handoff-completeness.md")])

    # finalize handoff with extracted context
    if project_metadata is not None and not dry_run:
        handoff_payload = _build_handoff_payload(
            selected_project_id=selected_project_id,
            selected_project_name=selected_project_name,
            tools=tools,
            project_metadata=project_metadata,
            extracted_design_context=extracted_design_context,
            limitations=limitations,
            generated_at=generated_at,
            endpoint=endpoint,
            called_tools=tool_calls,
            completeness=report if completeness_report else None,
        )
        _write_json(json_out_path, handoff_payload, redact=redact)
        if str(json_out_path) not in artifacts:
            artifacts.append(str(json_out_path))
        _write_text(
            out_dir_path / "stitch-implementation-notes.md",
            "\n".join(
                [
                    "# Stitch MCP Direct Handoff",
                    f"Generated at: {_utcnow()}",
                    "Source: Stitch MCP direct HTTP",
                    f"Endpoint: {endpoint}",
                    "",
                    f"- Project ID: `{selected_project_id}`",
                    f"- Project name: `{selected_project_name}`",
                    "",
                    "- initialize: success",
                    f"- list_projects: {len(projects)}",
                    f"- get_project: {'success' if project_metadata else 'failed'}",
                    "",
                    "- layout/component references: " + ("extracted" if extracted_design_context.get("layout_structure") or extracted_design_context.get("component_hierarchy") else "not found"),
                ]
            ),
        )
        if str(out_dir_path / "stitch-implementation-notes.md") not in artifacts:
            artifacts.append(str(out_dir_path / "stitch-implementation-notes.md"))

    if (
        not dry_run
        and extracted_design_context is not None
        and not extracted_design_context.get("layout_structure")
        and not extracted_design_context.get("component_hierarchy")
    ):
        _write_manual_export_request(
            out_dir_path,
            project_id=selected_project_id,
            project_name=selected_project_name or selected_project_id or "",
            selected=[
                "wide desktop screen PNG at 1920px",
                "1440px screen PNG",
                "narrow responsive PNG",
                "Design DNA / implementation handoff text",
                "component hierarchy or screen structure",
                "tokens: colors, typography, spacing, radius, shadows",
            ],
        )
        artifacts.append(str(out_dir_path / "manual-export-request.md"))

    manual_export_needed = (
        extracted_design_context is not None
        and not extracted_design_context.get("layout_structure")
        and not extracted_design_context.get("component_hierarchy")
    )

    if project_metadata is None:
        return DirectHandoffResult(
            label="Blocked: Stitch get_project failed",
            source="Stitch MCP direct HTTP",
            endpoint=endpoint,
            selected_project_id=selected_project_id,
            selected_project_name=selected_project_name,
            available_tools=tools,
            project_metadata=None,
            extracted_design_context=None,
            limitations=limitations,
            generated_at=_utcnow(),
            tools_list_count=len(tools),
            projects_count=len(projects),
            api_initialized=api_initialized,
            failure_reason="get_project_failed",
            artifacts_written=artifacts,
            project_selected=True,
            tool_calls=tool_calls,
        )

    if manual_export_needed:
        if report is None:
            label = "Stitch project fetched but handoff requires manual export"
        else:
            label = "Stitch project fetched but handoff requires manual export" if not report["enough_for_implementation"] else "Stitch project fetched but handoff requires manual export"
    elif report is None:
        label = "Stitch direct MCP handoff available"
    else:
        label = "Stitch direct MCP handoff available" if report["enough_for_implementation"] else "Stitch project fetched but design handoff incomplete"

    if not dry_run and project_metadata:
        _write_dashboard_brief(
            out_dir=out_dir_path,
            project_id=selected_project_id or "",
            project_name=selected_project_name or selected_project_id or "",
            extracted_context=extracted_design_context,
            completeness=report,
            manual_needed=manual_export_needed,
        )
        if str(out_dir_path / "dashboard-redesign-brief.md") not in artifacts:
            artifacts.append(str(out_dir_path / "dashboard-redesign-brief.md"))

    return DirectHandoffResult(
        label=label,
        source="Stitch MCP direct HTTP",
        endpoint=endpoint,
        selected_project_id=selected_project_id,
        selected_project_name=selected_project_name,
        available_tools=tools,
        project_metadata=project_metadata,
        extracted_design_context=extracted_design_context,
        limitations=limitations,
        generated_at=generated_at,
        tools_list_count=len(tools),
        projects_count=len(projects),
        api_initialized=api_initialized,
        failure_reason=None,
        artifacts_written=artifacts,
        project_selected=True,
        tool_calls=tool_calls,
    )


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--project-id")
    parser.add_argument("--project-index", type=int, default=None)
    parser.add_argument("--out-dir", default="design/stitch")
    parser.add_argument("--json-out", default="design/stitch/stitch-handoff.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--redact", action="store_true")
    parser.add_argument("--list-tools-detail", action="store_true")
    parser.add_argument("--call-tool")
    parser.add_argument("--args-json")
    parser.add_argument("--auto-discover-handoff", action="store_true")
    parser.add_argument("--save-images", action="store_true")
    parser.add_argument("--completeness-report", action="store_true")
    args = parser.parse_args()

    result = run_direct_handoff(
        project_id=args.project_id,
        project_index=args.project_index,
        out_dir=Path(args.out_dir),
        json_out=Path(args.json_out),
        dry_run=args.dry_run,
        redact=args.redact,
        endpoint=args.endpoint,
        list_tools_detail=args.list_tools_detail,
        call_tool=args.call_tool,
        args_json=args.args_json,
        auto_discover_handoff=args.auto_discover_handoff,
        save_images=args.save_images,
        completeness_report=args.completeness_report,
    )
    print(json.dumps({"label": result.label, "failure_reason": result.failure_reason, "artifacts_written": result.artifacts_written}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

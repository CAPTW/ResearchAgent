from __future__ import annotations

import json
from pathlib import Path

from tools.stitch_mcp_direct_handoff import _build_completeness, run_direct_handoff


def _request_fn_factory(payloads: dict[str, dict], call_log: list[tuple[str, str]] | None = None):
    def request_fn(endpoint: str, api_key: str, payload: dict) -> dict:
        assert endpoint == "https://stitch.googleapis.com/mcp"
        assert api_key == "test-stitch-key"
        method = payload["method"]
        if call_log is not None:
            tool_name = payload.get("params", {}).get("name", "")
            call_log.append((method, tool_name))
        if method == "tools/call":
            name = payload["params"]["name"]
            key = f"tools/call:{name}"
            if key in payloads:
                return payloads[key]
            if "tools/call" in payloads:
                return payloads["tools/call"]
            raise AssertionError(f"Unexpected tool call: {name}")
        if method not in payloads:
            raise AssertionError(f"Unexpected MCP method: {method}")
        return payloads[method]

    return request_fn


def _assert_no_sensitive(text: str, secret: str) -> None:
    assert secret not in text
    assert "test-stitch-key" not in text


def _make_payload() -> dict:
    return {
        "initialize": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"protocolVersion": "2024-11-05"},
        },
        "tools/list": {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "structuredContent": {
                    "tools": [
                        {
                            "name": "create_project",
                            "description": "Creates a project",
                            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
                        },
                        {
                            "name": "get_project",
                            "description": "Get a project",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            },
                        },
                        {
                            "name": "list_projects",
                            "description": "List projects",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "list_screens",
                            "description": "List screens",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"projectId": {"type": "string"}},
                                "required": ["projectId"],
                            },
                        },
                        {
                            "name": "get_screen",
                            "description": "Get screen",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "projectId": {"type": "string"},
                                    "screenId": {"type": "string"},
                                },
                                "required": ["name", "projectId", "screenId"],
                            },
                        },
                        {
                            "name": "list_design_systems",
                            "description": "List design systems",
                            "inputSchema": {"type": "object", "properties": {"projectId": {"type": "string"}}},
                        },
                    ]
                }
            },
        },
        "tools/call:list_projects": {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "structuredContent": {
                    "projects": [
                        {"name": "projects/alpha", "title": "Alpha"},
                        {"name": "projects/beta", "title": "Beta"},
                    ]
                }
            },
        },
        "tools/call:get_project": {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {
                "structuredContent": {
                    "name": "projects/alpha",
                    "designTheme": {
                        "namedColors": {"surface": "#0b1326"},
                        "typography": {"body": {"fontFamily": "Inter"}},
                        "spacing": 1,
                        "spacingScale": 1,
                    },
                    "thumbnailScreenshot": {
                        "downloadUrl": "https://example.test/thumb.png",
                    },
                }
            },
        },
        "tools/call:list_screens": {
            "jsonrpc": "2.0",
            "id": 5,
            "result": {
                "structuredContent": {
                    "screens": [{"name": "projects/alpha/screens/abc123", "title": "Wide dashboard"}]
                }
            },
        },
        "tools/call:get_screen": {
            "jsonrpc": "2.0",
            "id": 6,
            "result": {
                "structuredContent": {
                    "name": "projects/alpha/screens/abc123",
                    "layout": {"rows": [{"id": 1}]},
                    "thumbnailScreenshot": {"downloadUrl": "https://example.test/abc123-thumb.png"},
                }
            },
        },
        "tools/call:list_design_systems": {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"designSystems": []})},
                ]
            },
        },
    }


def _set_api_key(monkeypatch) -> str:
    secret = "test-stitch-key"
    monkeypatch.setenv("STITCH_API_KEY", secret)
    return secret


def test_tool_schema_audit_is_generated(monkeypatch, tmp_path: Path) -> None:
    _set_api_key(monkeypatch)
    out_dir = tmp_path / "audit"
    request_fn = _request_fn_factory(_make_payload())

    result = run_direct_handoff(
        project_id="projects/alpha",
        project_index=None,
        out_dir=out_dir,
        json_out=out_dir / "stitch-handoff.json",
        dry_run=True,
        redact=False,
        request_fn=request_fn,
        list_tools_detail=True,
        auto_discover_handoff=False,
    )

    assert result.label.startswith("Stitch")
    assert (out_dir / "tool-schema-audit.json").exists()
    assert (out_dir / "tool-schema-audit.md").exists()
    audit = json.loads((out_dir / "tool-schema-audit.json").read_text(encoding="utf-8"))
    assert audit["tool_count"] == 6
    assert "create_project" in audit["unsafe_write_tools"]
    assert set(audit["candidate_handoff_tools"]) == {"list_screens", "get_screen", "list_design_systems"}


def test_auto_discover_handoff_does_not_call_unsafe_tools(monkeypatch, tmp_path: Path) -> None:
    _set_api_key(monkeypatch)
    out_dir = tmp_path / "discover"
    call_log: list[tuple[str, str]] = []
    request_fn = _request_fn_factory(_make_payload(), call_log=call_log)

    result = run_direct_handoff(
        project_id="projects/alpha",
        project_index=None,
        out_dir=out_dir,
        json_out=out_dir / "stitch-handoff.json",
        dry_run=False,
        redact=True,
        request_fn=request_fn,
        auto_discover_handoff=True,
    )

    called_tools = [name for method, name in call_log if method == "tools/call"]
    assert "create_project" not in called_tools
    assert "get_project" in called_tools
    assert "list_screens" in called_tools
    assert "get_screen" in called_tools
    assert "list_design_systems" in called_tools
    assert result.label.startswith("Stitch")

    handoff = json.loads((out_dir / "stitch-handoff.json").read_text(encoding="utf-8"))
    for tool_call in handoff.get("called_tools", []):
        assert tool_call["tool_name"] in {"list_screens", "get_screen", "list_design_systems"}


def test_call_tool_rejects_invalid_args(monkeypatch, tmp_path: Path) -> None:
    _set_api_key(monkeypatch)
    out_dir = tmp_path / "invalid"
    request_fn = _request_fn_factory(_make_payload())
    result = run_direct_handoff(
        project_id="projects/alpha",
        project_index=None,
        out_dir=out_dir,
        json_out=out_dir / "stitch-handoff.json",
        dry_run=True,
        redact=True,
        request_fn=request_fn,
        call_tool="get_project",
        args_json='{"foo":"bar"}',
    )

    assert result.label == "Blocked: invalid --args-json payload"
    assert result.failure_reason == "invalid_args_json"


def test_call_tool_argument_validation_and_redaction(monkeypatch, tmp_path: Path) -> None:
    _set_api_key(monkeypatch)
    secret = "test-stitch-key"
    payload = _make_payload()
    payload["tools/call:get_project"] = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {
            "structuredContent": {
                "name": "projects/alpha",
                "design_theme": {"api_key": secret},
            }
        },
    }
    request_fn = _request_fn_factory(payload)

    result = run_direct_handoff(
        project_id="projects/alpha",
        project_index=None,
        out_dir=tmp_path / "call",
        json_out=tmp_path / "call" / "stitch-handoff.json",
        dry_run=False,
        redact=True,
        request_fn=request_fn,
        call_tool="get_project",
        args_json='{"name":"projects/alpha"}',
    )

    assert result.label in {"Stitch direct MCP handoff available", "Stitch project fetched but handoff requires manual export", "Stitch project fetched but design handoff incomplete"}
    handoff = json.loads((tmp_path / "call" / "stitch-handoff.json").read_text(encoding="utf-8"))
    assert any(entry["tool_name"] == "get_project" and entry["success"] for entry in handoff["called_tools"])
    _assert_no_sensitive(json.dumps(handoff), secret)


def test_image_data_uri_extraction(monkeypatch, tmp_path: Path) -> None:
    _set_api_key(monkeypatch)
    out_dir = tmp_path / "image"
    payload = _make_payload()
    payload["tools/call:get_project"]["result"]["structuredContent"]["thumbnailScreenshot"] = {
        "downloadUrl": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+c7ZsAAAAASUVORK5CYII="
    }
    request_fn = _request_fn_factory(payload)

    result = run_direct_handoff(
        project_id="projects/alpha",
        project_index=None,
        out_dir=out_dir,
        json_out=out_dir / "stitch-handoff.json",
        dry_run=False,
        redact=True,
        request_fn=request_fn,
        auto_discover_handoff=False,
        save_images=True,
    )

    saved = list((out_dir / "raw" / "images").glob("*.png")) + [
        out_dir / "stitch-thumbnail.png",
        out_dir / "stitch-screen-wide.png",
    ]
    assert any(path.exists() for path in saved)
    assert result.label in {"Stitch direct MCP handoff available", "Stitch project fetched but handoff requires manual export"}


def test_completeness_report_true_when_layout_or_screen_exists() -> None:
    no_data = _build_completeness(None, None, False)
    assert no_data["enough_for_implementation"] is False
    assert "project_metadata" in no_data["missing_fields"]

    project_meta = {"designTheme": {"namedColors": {"surface": "#000"}, "typography": {"body": {"fontFamily": "Inter"}}, "spacing": 1}}
    with_layout = _build_completeness(project_meta, {"screen_images": ["https://x"], "layout_structure": {"rows": []}}, False)
    without_layout_but_image = _build_completeness(project_meta, {"screen_images": ["https://x"]}, False)
    assert with_layout["enough_for_implementation"] is True
    assert without_layout_but_image["enough_for_implementation"] is True


def test_secret_not_written_to_outputs(monkeypatch, tmp_path: Path) -> None:
    secret = _set_api_key(monkeypatch)
    payload = _make_payload()
    payload["tools/call:get_project"]["result"]["structuredContent"]["design_theme"] = {"secret": secret}
    request_fn = _request_fn_factory(payload)

    run_direct_handoff(
        project_id="projects/alpha",
        project_index=None,
        out_dir=tmp_path / "secret",
        json_out=tmp_path / "secret" / "stitch-handoff.json",
        dry_run=False,
        redact=True,
        request_fn=request_fn,
    )

    for rel_path in ["stitch-handoff.json", "stitch-tools.json", "stitch-implementation-notes.md"]:
        target = tmp_path / "secret" / rel_path
        if target.exists():
            _assert_no_sensitive(target.read_text(encoding="utf-8"), secret)

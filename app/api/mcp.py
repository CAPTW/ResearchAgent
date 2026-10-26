from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.auth.config import get_auth_settings
from app.auth.security import (
    McpAuthChallenge,
    RESEARCH_READ_SCOPE,
    RESEARCH_WRITE_SCOPE,
    build_mcp_protected_resource_metadata,
    build_mcp_security_schemes,
    ensure_mcp_run_access,
    require_mcp_principal,
)
from app.schemas.mcp import (
    McpFetchPaperDetailsRequest,
    McpGetReportPreviewRequest,
    McpGetRunStatusRequest,
    McpOutlineSectionSummaryResponse,
    McpPaperDetailsResponse,
    McpPaperSummaryResponse,
    McpReviewSummaryResponse,
    McpRunStatusResponse,
    McpSearchPapersRequest,
    McpSearchPapersResponse,
    McpStartResearchRunRequest,
    McpStartResearchRunResponse,
    McpReportPreviewResponse,
)
from app.services import reporting, reviews, workbench
from app.services.runs import get_run, start_run

router = APIRouter(tags=["mcp"])


def _mcp_headers() -> dict[str, str]:
    return {"MCP-Protocol-Version": get_auth_settings().mcp_protocol_version}


def _json_rpc_result(
    request_id: Any,
    result: dict[str, Any],
    *,
    status_code: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = _mcp_headers()
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": request_id, "result": result},
        headers=headers,
    )


def _json_rpc_error(request_id: Any, code: int, message: str, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        headers=_mcp_headers(),
    )


def _tool_result(payload: dict[str, Any], *, is_error: bool = False, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }
    if meta:
        result["_meta"] = meta
    return result


def _json_rpc_tool_error(
    request_id: Any,
    payload: dict[str, Any],
    *,
    status_code: int = 200,
    extra_headers: dict[str, str] | None = None,
    meta: dict[str, Any] | None = None,
) -> JSONResponse:
    return _json_rpc_result(
        request_id,
        _tool_result(payload, is_error=True, meta=meta),
        status_code=status_code,
        extra_headers=extra_headers,
    )


def _search_papers(arguments: dict[str, Any]) -> dict[str, Any]:
    request = McpSearchPapersRequest.model_validate(arguments)
    paper_list = workbench.list_run_papers(
        request.run_id,
        page=request.page,
        page_size=request.page_size,
        q=request.q,
        status=request.status,
        theme=request.theme,
        score_band=request.score_band,
        dedup=request.dedup,
    )
    payload = McpSearchPapersResponse(
        run_id=request.run_id,
        total=paper_list.get("total", 0),
        page=paper_list.get("page", request.page),
        page_size=paper_list.get("page_size", request.page_size),
        query=request.q,
        items=[
            McpPaperSummaryResponse(
                paper_id=item.get("paper_id", ""),
                title=item.get("title", ""),
                score=int(item.get("score", 0) or 0),
                score_band=item.get("score_band", "low"),
                theme=item.get("theme", "Uncategorized"),
                published=item.get("published"),
                entry_url=item.get("entry_url"),
                pdf_url=item.get("pdf_url"),
                status=item.get("status", "unreviewed"),
                abstract_excerpt=item.get("abstract_excerpt", ""),
            )
            for item in paper_list.get("items", [])
        ],
    )
    return payload.model_dump()


def _fetch_paper_details(arguments: dict[str, Any]) -> dict[str, Any]:
    request = McpFetchPaperDetailsRequest.model_validate(arguments)
    detail = workbench.get_run_paper_detail(request.run_id, request.paper_id)
    review_payload = reviews.load_review_payload(request.run_id, workbench.get_raw_paper(request.run_id, request.paper_id))
    payload = McpPaperDetailsResponse(
        run_id=request.run_id,
        paper=McpPaperSummaryResponse(
            paper_id=detail["paper"]["paper_id"],
            title=detail["paper"]["title"],
            score=int(detail["paper"].get("score", 0) or 0),
            score_band=detail["paper"].get("score_band", "low"),
            theme=detail["paper"].get("theme", "Uncategorized"),
            published=detail["paper"].get("published"),
            entry_url=detail["paper"].get("entry_url"),
            pdf_url=detail["paper"].get("pdf_url"),
            status=detail["paper"].get("status", "unreviewed"),
            abstract_excerpt=detail["paper"].get("abstract_excerpt", ""),
        ),
        abstract=detail.get("abstract", ""),
        note_available=bool(detail.get("note_content")),
        ranking_explanation=list(detail.get("ranking_explanation", [])),
        related_papers=[
            {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "score": int(item.get("score", 0) or 0),
                "theme": item.get("theme", "Uncategorized"),
                "relation": item.get("relation", "related"),
            }
            for item in detail.get("related_papers", [])
        ],
        review=(
            McpReviewSummaryResponse(
                status=review_payload.get("status", "missing"),
                completeness=float(review_payload.get("completeness", 0.0) or 0.0),
                one_liner=review_payload.get("one_liner"),
                why_it_matters=list(review_payload.get("why_it_matters", [])),
                key_contributions=list(review_payload.get("key_contributions", [])),
                xr_takeaways=list(review_payload.get("xr_takeaways", [])),
                limitations=list(review_payload.get("limitations", [])),
                lab_actions=list(review_payload.get("lab_actions", [])),
                confidence=review_payload.get("confidence"),
                evidence_count=len(review_payload.get("evidence", [])),
            )
            if request.include_review
            else None
        ),
    )
    return payload.model_dump()


def _start_research_run(arguments: dict[str, Any], *, owner_user_id: str | None) -> dict[str, Any]:
    request = McpStartResearchRunRequest.model_validate(arguments)
    run = start_run(request, owner_user_id=owner_user_id)
    payload = McpStartResearchRunResponse(
        run_id=run["run_id"],
        topic=run["topic"],
        status=run["status"],
        created_at=run["created_at"],
    )
    return payload.model_dump()


def _get_run_status(arguments: dict[str, Any]) -> dict[str, Any]:
    request = McpGetRunStatusRequest.model_validate(arguments)
    run = get_run(request.run_id)
    payload = McpRunStatusResponse(
        run_id=run["run_id"],
        topic=run["topic"],
        status=run["status"],
        created_at=run["created_at"],
        updated_at=run["updated_at"],
        started_at=run.get("started_at"),
        finished_at=run.get("finished_at"),
        counts={key: int(value) for key, value in (run.get("counts") or {}).items()},
        error=run.get("error"),
        lineage=run.get("lineage") or {},
        rerun_context=run.get("rerun_context") or {},
    )
    return payload.model_dump()


def _get_report_preview(arguments: dict[str, Any]) -> dict[str, Any]:
    request = McpGetReportPreviewRequest.model_validate(arguments)
    preview = reporting.build_preview(request.run_id)
    outline = reporting.load_outline(request.run_id)
    payload = McpReportPreviewResponse(
        run_id=request.run_id,
        preview_content=preview.get("content", ""),
        generated_at=preview.get("generated_at", ""),
        sections=[
            McpOutlineSectionSummaryResponse(
                section_id=section.get("section_id", ""),
                title=section.get("title", ""),
                kind=section.get("kind", "markdown"),
                paper_ids=[str(paper_id) for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
                compare_notes_count=len(section.get("compare_notes", [])),
            )
            for section in outline.get("sections", [])
        ],
    )
    return payload.model_dump()


MCP_TOOLS: dict[str, dict[str, Any]] = {
    "search_papers": {
        "description": "Search papers within an existing research run without exposing internal artifact paths.",
        "input_model": McpSearchPapersRequest,
        "output_model": McpSearchPapersResponse,
        "required_scope": RESEARCH_READ_SCOPE,
        "read_only_hint": True,
        "handler": _search_papers,
    },
    "fetch_paper_details": {
        "description": "Fetch paper detail and normalized review summary for a paper in an existing run.",
        "input_model": McpFetchPaperDetailsRequest,
        "output_model": McpPaperDetailsResponse,
        "required_scope": RESEARCH_READ_SCOPE,
        "read_only_hint": True,
        "handler": _fetch_paper_details,
    },
    "start_research_run": {
        "description": "Start a new research run using the same backend pipeline as the web app.",
        "input_model": McpStartResearchRunRequest,
        "output_model": McpStartResearchRunResponse,
        "required_scope": RESEARCH_WRITE_SCOPE,
        "read_only_hint": False,
        "handler": _start_research_run,
    },
    "get_run_status": {
        "description": "Get run status and high-level counts for an existing research run.",
        "input_model": McpGetRunStatusRequest,
        "output_model": McpRunStatusResponse,
        "required_scope": RESEARCH_READ_SCOPE,
        "read_only_hint": True,
        "handler": _get_run_status,
    },
    "get_report_preview": {
        "description": "Return a report preview and outline section summary for an existing research run.",
        "input_model": McpGetReportPreviewRequest,
        "output_model": McpReportPreviewResponse,
        "required_scope": RESEARCH_READ_SCOPE,
        "read_only_hint": True,
        "handler": _get_report_preview,
    },
}


def _tool_definition(name: str, config: dict[str, Any]) -> dict[str, Any]:
    security_schemes = build_mcp_security_schemes(config["required_scope"])
    annotations = {"readOnlyHint": bool(config.get("read_only_hint", False))}
    return {
        "name": name,
        "description": config["description"],
        "inputSchema": config["input_model"].model_json_schema(),
        "outputSchema": config["output_model"].model_json_schema(),
        "annotations": annotations,
        "securitySchemes": security_schemes,
        "_meta": {"securitySchemes": security_schemes, "annotations": annotations},
    }


def get_mcp_tool_access_summary() -> dict[str, list[str]]:
    public_tools: list[str] = []
    protected_read_tools = sorted(
        name
        for name, config in MCP_TOOLS.items()
        if config["required_scope"] == RESEARCH_READ_SCOPE
    )
    protected_write_tools = sorted(
        name
        for name, config in MCP_TOOLS.items()
        if config["required_scope"] == RESEARCH_WRITE_SCOPE
    )
    return {
        "public_tools": public_tools,
        "protected_read_tools": protected_read_tools,
        "protected_write_tools": protected_write_tools,
    }


def _tool_name_from_request(body: dict[str, Any]) -> str | None:
    if body.get("method") != "tools/call":
        return None
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return str(name).strip() if name else None


@router.get("/mcp")
def get_mcp_overview():
    settings = get_auth_settings()
    return {
        "name": "xrml-research-agent-mcp",
        "protocol_version": settings.mcp_protocol_version,
        "transport": "streamable-http",
        "endpoint": "/mcp",
        "auth_mode": settings.auth_mode,
        "tools": sorted(MCP_TOOLS.keys()),
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def get_protected_resource_metadata():
    return build_mcp_protected_resource_metadata()


@router.post("/mcp")
async def handle_mcp(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _json_rpc_error(None, -32700, "Invalid JSON body.", status_code=400)

    if not isinstance(body, dict):
        return _json_rpc_error(None, -32600, "MCP expects a single JSON-RPC request object.", status_code=400)

    request_id = body.get("id")
    method = body.get("method")

    if not isinstance(method, str):
        return _json_rpc_error(request_id, -32600, "JSON-RPC method is required.", status_code=400)

    if method == "notifications/initialized":
        return Response(status_code=202, headers=_mcp_headers())

    if method == "initialize":
        return _json_rpc_result(
            request_id,
            {
                "protocolVersion": get_auth_settings().mcp_protocol_version,
                "serverInfo": {"name": "xrml-research-agent-mcp", "version": "0.1.0"},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )

    if method == "ping":
        return _json_rpc_result(request_id, {})

    if method == "tools/list":
        return _json_rpc_result(request_id, {"tools": [_tool_definition(name, config) for name, config in MCP_TOOLS.items()]})

    if method != "tools/call":
        return _json_rpc_error(request_id, -32601, f"Unsupported MCP method: {method}")

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _json_rpc_error(request_id, -32602, "tools/call params must be an object.", status_code=400)

    tool_name = _tool_name_from_request(body)
    if not tool_name or tool_name not in MCP_TOOLS:
        return _json_rpc_error(request_id, -32601, f"Unknown tool: {tool_name or 'unknown'}")

    tool_config = MCP_TOOLS[tool_name]
    principal = require_mcp_principal(request, required_scope=tool_config["required_scope"])
    if isinstance(principal, McpAuthChallenge):
        return _json_rpc_tool_error(
            request_id,
            principal.payload(),
            status_code=principal.status_code,
            extra_headers={"WWW-Authenticate": principal.www_authenticate},
            meta=principal.meta(),
        )

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _json_rpc_error(request_id, -32602, "tools/call arguments must be an object.", status_code=400)

    run_id = arguments.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        try:
            ensure_mcp_run_access(principal, run_id)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "forbidden", "message": exc.detail},
                headers=_mcp_headers(),
            )

    try:
        if tool_name == "start_research_run":
            payload = tool_config["handler"](arguments, owner_user_id=principal.user_id)
        else:
            payload = tool_config["handler"](arguments)
    except FileNotFoundError as exc:
        return _json_rpc_result(request_id, _tool_result({"error": str(exc)}, is_error=True))
    except Exception as exc:
        return _json_rpc_result(request_id, _tool_result({"error": str(exc)}, is_error=True))

    return _json_rpc_result(request_id, _tool_result(payload))

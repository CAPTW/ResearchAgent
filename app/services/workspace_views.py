from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc, read_json, slugify, write_json
from app.repositories.runs import build_run_paths, load_run_record

VIEWS_FILENAME = "workspace_views.json"
VALID_COMPARE_VIEWS = {"papers", "themes", "report", "exports", "lineage", "rerun", "preset"}
VALID_REPORT_SIDES = {"left", "right", "both"}
VALID_ACTION_TYPES = {
    "insert_section_before",
    "insert_section_after",
    "replace_section_draft",
    "create_candidate_section",
    "attach_compare_note",
    "apply_theme_outline_suggestion",
    "apply_paper_seed_suggestion",
}


def _views_path(run_id: str) -> Path:
    return build_run_paths(run_id).inputs_dir / VIEWS_FILENAME


def _read_views(run_id: str) -> list[dict[str, Any]]:
    load_run_record(run_id)
    payload = read_json(_views_path(run_id), default=[]) or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _write_views(run_id: str, items: list[dict[str, Any]]) -> None:
    write_json(_views_path(run_id), items)


def _make_view_id(view_title: str, existing_ids: set[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(view_title)}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _normalize_action_seed(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    action_type = payload.get("action_type")
    if action_type not in VALID_ACTION_TYPES:
        return None
    preferred_report_side = payload.get("preferred_report_side")
    return {
        "action_type": action_type,
        "source_section_id": payload.get("source_section_id"),
        "source_section_title": payload.get("source_section_title"),
        "target_section_id": payload.get("target_section_id"),
        "preferred_report_side": preferred_report_side if preferred_report_side in VALID_REPORT_SIDES else "both",
        "source_bucket_id": payload.get("source_bucket_id"),
        "source_theme": payload.get("source_theme"),
        "source_paper_ids": [
            str(paper_id).strip()
            for paper_id in payload.get("source_paper_ids", [])
            if str(paper_id).strip()
        ],
        "payload": {
            "title": (payload.get("payload") or {}).get("title"),
            "body_markdown": (payload.get("payload") or {}).get("body_markdown"),
            "paper_ids": [
                str(paper_id).strip()
                for paper_id in ((payload.get("payload") or {}).get("paper_ids") or [])
                if str(paper_id).strip()
            ],
            "compare_note": (payload.get("payload") or {}).get("compare_note"),
            "metadata": (payload.get("payload") or {}).get("metadata") or {},
        },
    }


def _normalize_view(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    compare_view = payload.get("active_compare_view")
    preferred_report_side = payload.get("preferred_report_side")
    return {
        "view_id": str(payload.get("view_id") or ""),
        "run_id": str(payload.get("run_id") or run_id),
        "compare_partner_run_id": payload.get("compare_partner_run_id"),
        "title": str(payload.get("title") or "Untitled workspace view").strip() or "Untitled workspace view",
        "description": payload.get("description"),
        "active_compare_view": compare_view if compare_view in VALID_COMPARE_VIEWS else None,
        "compare_mode": payload.get("compare_mode"),
        "selection_bucket_id": payload.get("selection_bucket_id"),
        "preset_id": payload.get("preset_id"),
        "focus_theme": payload.get("focus_theme"),
        "focus_section_id": payload.get("focus_section_id"),
        "focus_section_title": payload.get("focus_section_title"),
        "preferred_report_side": preferred_report_side if preferred_report_side in VALID_REPORT_SIDES else "both",
        "selection_source": payload.get("selection_source"),
        "selected_paper_ids": [
            str(paper_id).strip()
            for paper_id in payload.get("selected_paper_ids", [])
            if str(paper_id).strip()
        ],
        "pending_action_seed": _normalize_action_seed(payload.get("pending_action_seed")),
        "report_tab_state": payload.get("report_tab_state") if isinstance(payload.get("report_tab_state"), dict) else {},
        "created_at": str(payload.get("created_at") or iso_now_utc()),
        "updated_at": str(payload.get("updated_at") or payload.get("created_at") or iso_now_utc()),
    }


def list_workspace_views(run_id: str) -> dict[str, Any]:
    items = [_normalize_view(run_id, item) for item in _read_views(run_id)]
    items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return {"run_id": run_id, "items": items}


def get_workspace_view(run_id: str, view_id: str) -> dict[str, Any]:
    views = list_workspace_views(run_id).get("items", [])
    match = next((item for item in views if item.get("view_id") == view_id), None)
    if match is None:
        raise FileNotFoundError(f"Workspace view not found: {view_id}")
    return match


def create_workspace_view(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    items = _read_views(run_id)
    existing_ids = {str(item.get("view_id") or "") for item in items}
    normalized = _normalize_view(
        run_id,
        {
            **payload,
            "view_id": _make_view_id(str(payload.get("title") or "workspace-view"), existing_ids),
            "run_id": run_id,
            "created_at": iso_now_utc(),
            "updated_at": iso_now_utc(),
        },
    )
    items.append(normalized)
    _write_views(run_id, items)
    return normalized


def delete_workspace_view(run_id: str, view_id: str) -> dict[str, Any]:
    items = _read_views(run_id)
    kept = [item for item in items if str(item.get("view_id") or "") != view_id]
    if len(kept) == len(items):
        raise FileNotFoundError(f"Workspace view not found: {view_id}")
    _write_views(run_id, kept)
    return {"run_id": run_id, "view_id": view_id, "status": "deleted"}

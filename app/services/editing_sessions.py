from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc, read_json, slugify, write_json
from app.repositories.runs import build_run_paths, load_run_record
from app.services import reporting

SESSIONS_FILENAME = "editing_sessions.json"
VALID_COMPARE_VIEWS = {"papers", "themes", "report", "exports", "lineage", "rerun", "preset"}
VALID_REPORT_SIDES = {"left", "right", "both"}


def _sessions_path(run_id: str) -> Path:
    return build_run_paths(run_id).inputs_dir / SESSIONS_FILENAME


def _read_sessions(run_id: str) -> list[dict[str, Any]]:
    load_run_record(run_id)
    payload = read_json(_sessions_path(run_id), default=[]) or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _write_sessions(run_id: str, items: list[dict[str, Any]]) -> None:
    write_json(_sessions_path(run_id), items)


def _make_session_id(title: str, existing_ids: set[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(title)}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _normalize_compare_view(value: str | None) -> str | None:
    return value if value in VALID_COMPARE_VIEWS else None


def _normalize_preferred_side(value: str | None) -> str:
    return value if value in VALID_REPORT_SIDES else "both"


def _normalize_session(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current_outline_hash = reporting.compute_outline_draft_hash(run_id=run_id)
    outline_draft_hash = str(payload.get("outline_draft_hash") or current_outline_hash or "")
    staged_action_ids = [str(action_id).strip() for action_id in payload.get("staged_action_ids", []) if str(action_id).strip()]
    pending_merge_order = [str(action_id).strip() for action_id in payload.get("pending_merge_order", []) if str(action_id).strip()]
    warnings: list[str] = []
    stale = bool(outline_draft_hash and current_outline_hash and outline_draft_hash != current_outline_hash)
    if stale:
        warnings.append("Outline draft hash changed since this editing session was saved.")
    missing_ids = [action_id for action_id in pending_merge_order if action_id not in staged_action_ids]
    if missing_ids:
        warnings.append("Pending merge order referenced staged actions that are not part of this session.")
    return {
        "session_id": str(payload.get("session_id") or ""),
        "run_id": str(payload.get("run_id") or run_id),
        "title": str(payload.get("title") or "Untitled editing session").strip() or "Untitled editing session",
        "description": payload.get("description"),
        "workspace_view_id": payload.get("workspace_view_id"),
        "compare_partner_run_id": payload.get("compare_partner_run_id"),
        "active_compare_view": _normalize_compare_view(payload.get("active_compare_view")),
        "compare_mode": payload.get("compare_mode"),
        "selection_bucket_id": payload.get("selection_bucket_id"),
        "preset_id": payload.get("preset_id"),
        "focus_theme": payload.get("focus_theme"),
        "focus_section_id": payload.get("focus_section_id"),
        "focus_section_title": payload.get("focus_section_title"),
        "preferred_report_side": _normalize_preferred_side(payload.get("preferred_report_side")),
        "selection_source": payload.get("selection_source"),
        "selected_paper_ids": [
            str(paper_id).strip() for paper_id in payload.get("selected_paper_ids", []) if str(paper_id).strip()
        ],
        "staged_action_ids": staged_action_ids,
        "pending_merge_order": pending_merge_order or staged_action_ids,
        "report_tab_state": payload.get("report_tab_state") if isinstance(payload.get("report_tab_state"), dict) else {},
        "outline_draft_hash": outline_draft_hash or None,
        "current_outline_draft_hash": current_outline_hash or None,
        "stale": stale,
        "warnings": warnings,
        "created_at": str(payload.get("created_at") or iso_now_utc()),
        "updated_at": str(payload.get("updated_at") or payload.get("created_at") or iso_now_utc()),
    }


def list_editing_sessions(run_id: str) -> dict[str, Any]:
    items = [_normalize_session(run_id, item) for item in _read_sessions(run_id)]
    items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return {"run_id": run_id, "items": items}


def get_editing_session(run_id: str, session_id: str) -> dict[str, Any]:
    match = next((item for item in list_editing_sessions(run_id).get("items", []) if item.get("session_id") == session_id), None)
    if match is None:
        raise FileNotFoundError(f"Editing session not found: {session_id}")
    return match


def create_editing_session(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    items = _read_sessions(run_id)
    existing_ids = {str(item.get("session_id") or "") for item in items}
    normalized = _normalize_session(
        run_id,
        {
            **payload,
            "session_id": _make_session_id(str(payload.get("title") or "editing-session"), existing_ids),
            "run_id": run_id,
            "outline_draft_hash": payload.get("outline_draft_hash") or reporting.compute_outline_draft_hash(run_id=run_id),
            "created_at": iso_now_utc(),
            "updated_at": iso_now_utc(),
        },
    )
    items.append(normalized)
    _write_sessions(run_id, items)
    return normalized


def update_editing_session(run_id: str, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    items = _read_sessions(run_id)
    updated: dict[str, Any] | None = None
    for index, item in enumerate(items):
        if str(item.get("session_id") or "") != session_id:
            continue
        payload = {
            **item,
            **updates,
            "session_id": session_id,
            "run_id": run_id,
            "updated_at": iso_now_utc(),
        }
        updated = _normalize_session(run_id, payload)
        items[index] = updated
        break
    if updated is None:
        raise FileNotFoundError(f"Editing session not found: {session_id}")
    _write_sessions(run_id, items)
    return updated


def delete_editing_session(run_id: str, session_id: str) -> dict[str, Any]:
    items = _read_sessions(run_id)
    kept = [item for item in items if str(item.get("session_id") or "") != session_id]
    if len(kept) == len(items):
        raise FileNotFoundError(f"Editing session not found: {session_id}")
    _write_sessions(run_id, kept)
    return {"run_id": run_id, "session_id": session_id, "status": "deleted"}

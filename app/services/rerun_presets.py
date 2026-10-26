from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc, read_json, slugify, write_json
from app.repositories.runs import build_run_paths, load_run_record

PRESET_FILENAME = "rerun_presets.json"
VALID_MODES = {
    "full_rerun_from_profile",
    "branch_from_selected_papers",
    "rerun_with_profile_overrides",
}
VALID_PRESET_TYPES = {"compare_bucket", "manual_selection", "profile_override", "full_rerun"}
VALID_SELECTION_SOURCES = {"manual", "compare_bucket", "preset", "suggested"}


def _preset_path(run_id: str) -> Path:
    return build_run_paths(run_id).inputs_dir / PRESET_FILENAME


def _read_presets(run_id: str) -> list[dict[str, Any]]:
    load_run_record(run_id)
    payload = read_json(_preset_path(run_id), default=[]) or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _write_presets(run_id: str, items: list[dict[str, Any]]) -> None:
    write_json(_preset_path(run_id), items)


def _make_preset_id(run_id: str, preset_name: str, existing_ids: set[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(preset_name)}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _normalize_compare_filters(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    return {
        "paper_query": payload.get("paper_query"),
        "theme": payload.get("theme"),
        "status": payload.get("status"),
        "source_side": payload.get("source_side") if payload.get("source_side") in {"left", "right", "both"} else "both",
        "bucket_id": payload.get("bucket_id"),
        "bucket_label": payload.get("bucket_label"),
    }


def _normalize_lineage_hint(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    selection_source = payload.get("selection_source")
    return {
        "compare_baseline_run_id": payload.get("compare_baseline_run_id"),
        "selection_source": selection_source if selection_source in VALID_SELECTION_SOURCES else None,
        "selection_bucket_id": payload.get("selection_bucket_id"),
        "selection_bucket_label": payload.get("selection_bucket_label"),
    }


def _default_preset_type(mode: str) -> str:
    if mode == "full_rerun_from_profile":
        return "full_rerun"
    if mode == "rerun_with_profile_overrides":
        return "profile_override"
    return "manual_selection"


def _normalize_preset(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode")
    if mode not in VALID_MODES:
        mode = "branch_from_selected_papers"
    preset_type = payload.get("preset_type")
    if preset_type not in VALID_PRESET_TYPES:
        preset_type = _default_preset_type(mode)
    return {
        "preset_id": str(payload.get("preset_id") or ""),
        "source_run_id": str(payload.get("source_run_id") or run_id),
        "compare_partner_run_id": payload.get("compare_partner_run_id"),
        "preset_name": str(payload.get("preset_name") or "Unnamed preset").strip() or "Unnamed preset",
        "preset_type": preset_type,
        "mode": mode,
        "selected_paper_ids": [
            str(paper_id).strip()
            for paper_id in payload.get("selected_paper_ids", [])
            if str(paper_id).strip()
        ],
        "profile_overrides": payload.get("profile_overrides") if isinstance(payload.get("profile_overrides"), dict) else None,
        "compare_filters": _normalize_compare_filters(payload.get("compare_filters")),
        "note": payload.get("note"),
        "created_at": str(payload.get("created_at") or iso_now_utc()),
        "created_from_compare": bool(payload.get("created_from_compare", False)),
        "lineage_hint": _normalize_lineage_hint(payload.get("lineage_hint")),
    }


def list_rerun_presets(run_id: str) -> dict[str, Any]:
    items = [_normalize_preset(run_id, item) for item in _read_presets(run_id)]
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"run_id": run_id, "items": items}


def create_rerun_preset(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    items = _read_presets(run_id)
    existing_ids = {str(item.get("preset_id") or "") for item in items}
    normalized = _normalize_preset(
        run_id,
        {
            **payload,
            "preset_id": _make_preset_id(run_id, str(payload.get("preset_name") or "preset"), existing_ids),
            "source_run_id": run_id,
            "created_at": iso_now_utc(),
        },
    )
    items.append(normalized)
    _write_presets(run_id, items)
    return normalized


def delete_rerun_preset(run_id: str, preset_id: str) -> dict[str, Any]:
    items = _read_presets(run_id)
    kept = [item for item in items if str(item.get("preset_id")) != preset_id]
    if len(kept) == len(items):
        raise FileNotFoundError(f"Rerun preset not found: {preset_id}")
    _write_presets(run_id, kept)
    return {"run_id": run_id, "preset_id": preset_id, "status": "deleted"}

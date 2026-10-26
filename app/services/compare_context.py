from __future__ import annotations

import re
from typing import Any

from app.repositories.runs import load_run_record
from app.services import compare, editing_sessions, report_diff, reporting, rerun_presets, workspace_views

VALID_SELECTION_SOURCES = {"manual", "compare_bucket", "preset", "suggested"}
VALID_COMPARE_VIEWS = {"papers", "themes", "report", "exports", "lineage", "rerun", "preset"}
VALID_REPORT_SIDES = {"left", "right", "both"}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "section"


def _empty_filter_context() -> dict[str, Any]:
    return {
        "paper_query": None,
        "theme": None,
        "status": None,
        "source_side": "both",
        "bucket_id": None,
        "bucket_label": None,
    }


def _merge_filter_context(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if not override:
        return merged
    for key in ["paper_query", "theme", "status", "bucket_id", "bucket_label"]:
        value = override.get(key)
        if value:
            merged[key] = value
    if override.get("source_side") in {"left", "right", "both"}:
        merged["source_side"] = override["source_side"]
    return merged


def _normalize_compare_view(value: str | None) -> str | None:
    if value in VALID_COMPARE_VIEWS:
        return value
    return None


def _normalize_selection_source(value: str | None) -> str | None:
    if value in VALID_SELECTION_SOURCES:
        return value
    return None


def _normalize_preferred_side(value: str | None) -> str:
    if value in VALID_REPORT_SIDES:
        return value
    return "both"


def _parse_selected_paper_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [paper_id.strip() for paper_id in value.split(",") if paper_id.strip()]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _load_preset(run_id: str, preset_id: str | None) -> dict[str, Any] | None:
    if not preset_id:
        return None
    presets = rerun_presets.list_rerun_presets(run_id).get("items", [])
    return next((item for item in presets if item.get("preset_id") == preset_id), None)


def _resolve_compare_payloads(run_id: str, compare_partner_run_id: str) -> list[tuple[str, dict[str, Any]]]:
    payloads: list[tuple[str, dict[str, Any]]] = []
    attempted: set[tuple[str, str]] = set()
    for left_run_id, right_run_id in [(run_id, compare_partner_run_id), (compare_partner_run_id, run_id)]:
        key = (left_run_id, right_run_id)
        if key in attempted:
            continue
        attempted.add(key)
        payloads.append((f"{left_run_id}->{right_run_id}", compare.compare_runs(left_run_id, right_run_id)))
    return payloads


def _resolve_bucket(
    run_id: str,
    compare_partner_run_id: str | None,
    bucket_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    if not bucket_id:
        return None, None, warnings
    if not compare_partner_run_id:
        warnings.append("Compare partner run is missing, so the compare bucket could not be resolved.")
        return None, None, warnings

    fallback_match: tuple[dict[str, Any], dict[str, Any]] | None = None
    for _, payload in _resolve_compare_payloads(run_id, compare_partner_run_id):
        match = next((item for item in payload.get("rerun_buckets", []) if item.get("bucket_id") == bucket_id and item.get("source_run_id") == run_id), None)
        if match is not None:
            return match, payload, warnings
        if fallback_match is None:
            any_match = next((item for item in payload.get("rerun_buckets", []) if item.get("bucket_id") == bucket_id), None)
            if any_match is not None:
                fallback_match = (any_match, payload)

    if fallback_match is not None:
        warnings.append(f'Compare bucket "{bucket_id}" exists, but it is not sourced from the current run.')
        return fallback_match[0], fallback_match[1], warnings

    warnings.append(f'Compare bucket "{bucket_id}" was not found for the selected run pair.')
    return None, None, warnings


def _match_outline_section_id(run_id: str, section_id: str | None, section_title: str | None) -> str | None:
    outline = reporting.load_outline(run_id)
    normalized_section_id = (section_id or "").strip()
    normalized_section_title = (section_title or "").strip().lower()
    normalized_section_slug = _slug(normalized_section_title) if normalized_section_title else ""
    for section in outline.get("sections", []):
        candidate_id = str(section.get("section_id", "")).strip()
        candidate_title = str(section.get("title", "")).strip()
        if normalized_section_id and candidate_id == normalized_section_id:
            return candidate_id
        if normalized_section_title and candidate_title.lower() == normalized_section_title:
            return candidate_id
        if normalized_section_slug and _slug(candidate_title) == normalized_section_slug:
            return candidate_id
    return None


def _resolve_focus_section(
    *,
    run_id: str,
    compare_partner_run_id: str | None,
    focus_section_id: str | None,
    focus_section_title: str | None,
    preferred_report_side: str,
    seed_section: bool,
) -> tuple[dict[str, Any] | None, str | None, str | None, list[str], dict[str, Any] | None]:
    warnings: list[str] = []
    if not compare_partner_run_id or not (focus_section_id or focus_section_title or seed_section):
        return None, None, None, warnings, None

    left_snapshot, _ = report_diff.load_report_snapshot(run_id, run_side="left")
    right_snapshot, _ = report_diff.load_report_snapshot(compare_partner_run_id, run_side="right")
    report_detail = report_diff.build_report_diff(run_id, compare_partner_run_id)
    change_map = {
        item.get("section_key"): item.get("change_summary", [])
        for item in report_detail.get("section_diffs", [])
    }
    side_candidates = ["left", "right"] if preferred_report_side == "both" else [preferred_report_side]
    snapshots = {"left": left_snapshot, "right": right_snapshot}
    matched_outline_section_id = _match_outline_section_id(run_id, focus_section_id, focus_section_title)

    for side in side_candidates:
        snapshot = snapshots[side]
        section = report_diff.match_report_section(snapshot, section_id=focus_section_id, section_title=focus_section_title)
        if section is None:
            continue
        serialized = report_diff.serialize_report_section(
            section,
            source_side=side,
            matched_outline_section_id=matched_outline_section_id,
            change_summary=change_map.get(section.section_key, []),
        )
        focus_id = matched_outline_section_id or section.section_key
        focus_title = section.title
        return (serialized if seed_section else None), focus_id, focus_title, warnings, report_detail

    warnings.append("The requested report section could not be resolved from the compared artifacts.")
    return None, matched_outline_section_id, focus_section_title, warnings, report_detail


def _build_banner(
    *,
    compare_partner_run_id: str | None,
    relation: str | None,
    selection_bucket: dict[str, Any] | None,
    preset: dict[str, Any] | None,
    focus_theme: str | None,
    focus_section_title: str | None,
    seed_section: bool,
) -> dict[str, Any] | None:
    if not any([compare_partner_run_id, selection_bucket, preset, focus_theme, focus_section_title]):
        return None

    if focus_section_title:
        title = "Compare-driven report workspace"
    else:
        title = "Compare-driven review focus"

    messages: list[str] = []
    if compare_partner_run_id:
        messages.append(f"Compared with run {compare_partner_run_id}.")
    if selection_bucket:
        messages.append(f'Bucket: {selection_bucket.get("label", selection_bucket.get("bucket_id", "selected bucket"))}.')
    if preset:
        messages.append(f'Preset: {preset.get("preset_name", preset.get("preset_id", "saved preset"))}.')
    if focus_theme:
        messages.append(f"Theme focus: {focus_theme}.")
    if focus_section_title:
        messages.append(f"Section focus: {focus_section_title}.")
    if seed_section:
        messages.append("Seed content is staged only and requires an explicit apply action.")

    return {
        "title": title,
        "message": " ".join(messages).strip(),
        "relation": relation,
        "bucket_label": selection_bucket.get("label") if selection_bucket else None,
        "preset_name": preset.get("preset_name") if preset else None,
        "focus_theme": focus_theme,
        "focus_section_title": focus_section_title,
    }


def resolve_run_compare_context(
    run_id: str,
    *,
    editing_session_id: str | None = None,
    workspace_view_id: str | None = None,
    compare_partner_run_id: str | None = None,
    compare_mode: str | None = None,
    compare_view: str | None = None,
    selection_bucket_id: str | None = None,
    preset_id: str | None = None,
    focus_paper_id: str | None = None,
    focus_theme: str | None = None,
    focus_section_id: str | None = None,
    focus_section_title: str | None = None,
    preferred_report_side: str | None = None,
    selection_source: str | None = None,
    selected_paper_ids: str | None = None,
    seed_section: bool = False,
) -> dict[str, Any]:
    load_run_record(run_id)

    editing_session: dict[str, Any] | None = None
    warnings: list[str] = []
    if editing_session_id:
        try:
            editing_session = editing_sessions.get_editing_session(run_id, editing_session_id)
        except FileNotFoundError:
            editing_session = None
            warnings.append(f'Editing session "{editing_session_id}" was not found for this run.')

    if editing_session is not None:
        workspace_view_id = workspace_view_id or editing_session.get("workspace_view_id")
        compare_partner_run_id = compare_partner_run_id or editing_session.get("compare_partner_run_id")
        compare_mode = compare_mode or editing_session.get("compare_mode")
        compare_view = compare_view or editing_session.get("active_compare_view")
        selection_bucket_id = selection_bucket_id or editing_session.get("selection_bucket_id")
        preset_id = preset_id or editing_session.get("preset_id")
        focus_theme = focus_theme or editing_session.get("focus_theme")
        focus_section_id = focus_section_id or editing_session.get("focus_section_id")
        focus_section_title = focus_section_title or editing_session.get("focus_section_title")
        preferred_report_side = preferred_report_side or editing_session.get("preferred_report_side")
        selection_source = selection_source or editing_session.get("selection_source")
        if not selected_paper_ids and editing_session.get("selected_paper_ids"):
            selected_paper_ids = ",".join(editing_session.get("selected_paper_ids", []))

    workspace_view: dict[str, Any] | None = None
    if workspace_view_id:
        try:
            workspace_view = workspace_views.get_workspace_view(run_id, workspace_view_id)
        except FileNotFoundError:
            workspace_view = None
            warnings.append(f'Workspace view "{workspace_view_id}" was not found for this run.')

    normalized_view = _normalize_compare_view(compare_view)
    normalized_source = _normalize_selection_source(selection_source)
    normalized_side = _normalize_preferred_side(preferred_report_side)
    effective_filters = _empty_filter_context()
    selected_ids = _parse_selected_paper_ids(selected_paper_ids)
    active_pane = "report" if normalized_view == "report" or focus_section_id or focus_section_title or seed_section else "paper"

    if workspace_view is not None:
        action_seed = workspace_view.get("pending_action_seed") or {}
        compare_partner_run_id = compare_partner_run_id or workspace_view.get("compare_partner_run_id")
        compare_mode = compare_mode or workspace_view.get("compare_mode")
        normalized_view = normalized_view or _normalize_compare_view(workspace_view.get("active_compare_view"))
        selection_bucket_id = selection_bucket_id or workspace_view.get("selection_bucket_id")
        preset_id = preset_id or workspace_view.get("preset_id")
        focus_theme = focus_theme or workspace_view.get("focus_theme")
        focus_section_id = focus_section_id or workspace_view.get("focus_section_id") or action_seed.get("source_section_id")
        focus_section_title = focus_section_title or workspace_view.get("focus_section_title") or action_seed.get("source_section_title")
        normalized_side = _normalize_preferred_side(
            preferred_report_side or workspace_view.get("preferred_report_side") or action_seed.get("preferred_report_side")
        )
        normalized_source = normalized_source or _normalize_selection_source(workspace_view.get("selection_source"))
        selected_ids = workspace_view.get("selected_paper_ids", []) or selected_ids
        if action_seed and not seed_section:
            seed_section = bool(action_seed.get("source_section_id") or action_seed.get("source_section_title"))
        if normalized_view == "report" or focus_section_id or focus_section_title or workspace_view.get("pending_action_seed"):
            active_pane = "report"
    elif workspace_view_id:
        pass

    preset = _load_preset(run_id, preset_id)
    if preset:
        compare_partner_run_id = compare_partner_run_id or preset.get("compare_partner_run_id")
        selected_ids = preset.get("selected_paper_ids", []) or selected_ids
        effective_filters = _merge_filter_context(effective_filters, preset.get("compare_filters"))
        normalized_source = normalized_source or "preset"
    elif preset_id:
        warnings.append(f'Rerun preset "{preset_id}" was not found for this run.')

    compare_payload: dict[str, Any] | None = None
    selection_bucket: dict[str, Any] | None = None
    if selection_bucket_id:
        selection_bucket, compare_payload, bucket_warnings = _resolve_bucket(run_id, compare_partner_run_id, selection_bucket_id)
        warnings.extend(bucket_warnings)
        if selection_bucket is not None:
            compare_partner_run_id = compare_partner_run_id or selection_bucket.get("compare_partner_run_id")
            selected_ids = selection_bucket.get("selected_paper_ids", []) or selected_ids
            effective_filters = _merge_filter_context(effective_filters, selection_bucket.get("compare_filters"))
            normalized_source = normalized_source or "compare_bucket"

    if focus_theme:
        effective_filters["theme"] = focus_theme

    selected_ids = _dedupe_preserve_order(selected_ids)
    focus_paper_id = focus_paper_id or (selected_ids[0] if selected_ids else None)

    relation = None
    same_root = False
    if compare_partner_run_id:
        if compare_payload is None:
            compare_payload = compare.compare_runs(run_id, compare_partner_run_id)
        relation = compare_payload.get("summary", {}).get("relation")
        same_root = bool(compare_payload.get("summary", {}).get("same_root", False))

    pending_section_seed, resolved_focus_section_id, resolved_focus_section_title, section_warnings, _ = _resolve_focus_section(
        run_id=run_id,
        compare_partner_run_id=compare_partner_run_id,
        focus_section_id=focus_section_id,
        focus_section_title=focus_section_title,
        preferred_report_side=normalized_side,
        seed_section=seed_section,
    )
    warnings.extend(section_warnings)
    focus_section_id = resolved_focus_section_id or focus_section_id
    focus_section_title = resolved_focus_section_title or focus_section_title
    if focus_section_id or focus_section_title or pending_section_seed is not None:
        active_pane = "report"
        normalized_view = normalized_view or "report"
    elif editing_session and editing_session.get("report_tab_state", {}).get("active_pane") == "report":
        active_pane = "report"

    normalized_source = normalized_source or ("manual" if selected_ids else None)
    banner = _build_banner(
        compare_partner_run_id=compare_partner_run_id,
        relation=relation,
        selection_bucket=selection_bucket,
        preset=preset,
        focus_theme=focus_theme or effective_filters.get("theme"),
        focus_section_title=focus_section_title,
        seed_section=seed_section,
    )

    return {
        "run_id": run_id,
        "editing_session_id": editing_session.get("session_id") if editing_session else None,
        "editing_session_title": editing_session.get("title") if editing_session else None,
        "editing_session_stale": bool(editing_session.get("stale", False)) if editing_session else False,
        "workspace_view_id": workspace_view.get("view_id") if workspace_view else None,
        "workspace_view_title": workspace_view.get("title") if workspace_view else None,
        "compare_partner_run_id": compare_partner_run_id,
        "compare_mode": compare_mode,
        "compare_view": normalized_view,
        "selection_bucket_id": selection_bucket_id,
        "preset_id": preset_id,
        "selection_source": normalized_source,
        "preferred_report_side": normalized_side,
        "active_pane": active_pane,
        "effective_filters": effective_filters,
        "selected_paper_ids": selected_ids,
        "focus_paper_id": focus_paper_id,
        "focus_theme": focus_theme or effective_filters.get("theme"),
        "focus_section_id": focus_section_id,
        "focus_section_title": focus_section_title,
        "banner": banner,
        "summary": {
            "source_run_id": run_id,
            "compare_partner_run_id": compare_partner_run_id,
            "relation": relation,
            "same_root": same_root,
        },
        "pending_section_seed": pending_section_seed,
        "warnings": warnings,
    }

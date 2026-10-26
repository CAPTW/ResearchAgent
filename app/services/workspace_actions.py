from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc, read_json, slugify, write_json
from app.services import report_transaction
from app.repositories.runs import build_run_paths, load_run_record
from app.services import compare, compare_context, editing_sessions, outline_history, report_diff, reporting

STAGED_ACTIONS_FILENAME = "staged_workspace_actions.json"
MERGE_INTENT_BY_ACTION_TYPE = {
    "insert_section_before": "insert_before_target",
    "insert_section_after": "insert_after_target",
    "append_to_target_section": "append_to_target_section",
    "replace_section_draft": "replace_target_section",
    "create_candidate_section": "create_candidate_section",
    "attach_compare_note": "attach_compare_note_only",
    "apply_theme_outline_suggestion": "create_candidate_section",
    "apply_paper_seed_suggestion": "create_candidate_section",
}


def _make_action_id(action_type: str, title: str | None = None) -> str:
    seed = f"{iso_now_utc()}::{action_type}::{title or ''}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _staged_actions_path(run_id: str) -> Path:
    return build_run_paths(run_id).inputs_dir / STAGED_ACTIONS_FILENAME


def _read_staged_actions(run_id: str) -> list[dict[str, Any]]:
    load_run_record(run_id)
    payload = read_json(_staged_actions_path(run_id), default=[]) or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _write_staged_actions(run_id: str, items: list[dict[str, Any]]) -> None:
    write_json(_staged_actions_path(run_id), items)


def _outline_section_excerpt(section: dict[str, Any] | None) -> str | None:
    if not section:
        return None
    if section.get("body_markdown"):
        return _section_excerpt(str(section.get("body_markdown")))
    paper_ids = [str(paper_id).strip() for paper_id in section.get("paper_ids", []) if str(paper_id).strip()]
    if paper_ids:
        return f"Papers: {', '.join(paper_ids[:4])}" + (" ..." if len(paper_ids) > 4 else "")
    return None


def _build_preview_blocks(preview: dict[str, Any], outline_sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if preview.get("summary_lines"):
        blocks.append(
            {
                "label": "Summary",
                "kind": "summary",
                "content": "\n".join(str(line) for line in preview.get("summary_lines", []) if str(line).strip()),
            }
        )
    target_index = preview.get("effective_target", {}).get("target_index")
    target_section = outline_sections[target_index] if isinstance(target_index, int) and 0 <= target_index < len(outline_sections) else None
    target_excerpt = _outline_section_excerpt(target_section)
    if target_excerpt:
        blocks.append(
            {
                "label": f"Target · {target_section.get('title', 'section')}",
                "kind": "target",
                "content": target_excerpt,
            }
        )
    proposed_section = preview.get("proposed_section")
    if isinstance(proposed_section, dict):
        blocks.append(
            {
                "label": f"Proposed · {proposed_section.get('title') or 'candidate section'}",
                "kind": "proposed",
                "content": _outline_section_excerpt(proposed_section) or "_empty body_",
            }
        )
    proposed_note = preview.get("proposed_compare_note")
    if proposed_note:
        blocks.append({"label": "Compare note", "kind": "note", "content": str(proposed_note)})
    return blocks


def _build_citation_delta(preview: dict[str, Any], outline_sections: list[dict[str, Any]]) -> dict[str, Any]:
    target_index = preview.get("effective_target", {}).get("target_index")
    target_section = outline_sections[target_index] if isinstance(target_index, int) and 0 <= target_index < len(outline_sections) else {}
    proposed_section = preview.get("proposed_section") or {}
    left_count = len([paper_id for paper_id in target_section.get("paper_ids", []) if str(paper_id).strip()])
    right_count = len([paper_id for paper_id in proposed_section.get("paper_ids", []) if str(paper_id).strip()])
    notes: list[str] = []
    if preview.get("proposed_compare_note"):
        notes.append("Compare note only action; citation delta may be informational.")
    if preview.get("effective_target", {}).get("destructive"):
        notes.append("Target section already contains draft content.")
    return {
        "left_count": left_count,
        "right_count": right_count,
        "delta": right_count - left_count,
        "notes": notes,
    }


def _normalize_preview_block(block: dict[str, Any], *, index: int) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {
            "block_id": f"block-{index}",
            "block_type": "context",
            "label": f"Block {index}",
            "kind": "summary",
            "content": "",
            "before_text": None,
            "after_text": None,
            "before_excerpt": None,
            "after_excerpt": None,
            "anchor_kind": "section",
            "anchor_value": None,
            "related_citation_keys": [],
            "warnings": [],
        }
    before_text = block.get("before_text")
    after_text = block.get("after_text")
    content = block.get("content")
    if not content:
        content = after_text or before_text or ""
    return {
        "block_id": str(block.get("block_id") or f"block-{index}"),
        "block_type": str(block.get("block_type") or "context"),
        "label": str(block.get("label") or f"Block {index}"),
        "kind": str(block.get("kind") or "summary"),
        "content": str(content or ""),
        "before_text": before_text,
        "after_text": after_text,
        "before_excerpt": block.get("before_excerpt"),
        "after_excerpt": block.get("after_excerpt"),
        "anchor_kind": str(block.get("anchor_kind") or "section"),
        "anchor_value": block.get("anchor_value"),
        "related_citation_keys": [str(item) for item in block.get("related_citation_keys", []) if str(item).strip()],
        "warnings": [str(item) for item in block.get("warnings", []) if str(item).strip()],
    }


def _normalize_patch_anchor(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "anchor_kind": str(payload.get("anchor_kind") or "fallback"),
        "anchor_value": payload.get("anchor_value"),
        "matched": bool(payload.get("matched", False)),
        "confidence": float(payload.get("confidence", 0.0) or 0.0),
        "fallback_reason": payload.get("fallback_reason"),
    }


def _normalize_structured_patch_block(block: dict[str, Any], *, index: int) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or f"patch-{index}"),
        "operation": str(block.get("operation") or "context"),
        "anchor_before": _normalize_patch_anchor(block.get("anchor_before") if isinstance(block.get("anchor_before"), dict) else {}),
        "anchor_after": _normalize_patch_anchor(block.get("anchor_after") if isinstance(block.get("anchor_after"), dict) else {}),
        "before_text": block.get("before_text"),
        "after_text": block.get("after_text"),
        "before_excerpt": block.get("before_excerpt"),
        "after_excerpt": block.get("after_excerpt"),
        "related_citation_keys": [str(item) for item in block.get("related_citation_keys", []) if str(item).strip()],
        "related_paper_ids": [str(item) for item in block.get("related_paper_ids", []) if str(item).strip()],
        "warnings": [str(item) for item in block.get("warnings", []) if str(item).strip()],
        "confidence": float(block.get("confidence", 0.0) or 0.0),
    }


def _normalize_structured_patch(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    target_resolution = payload.get("target_resolution") if isinstance(payload.get("target_resolution"), dict) else {}
    normalized = {
        "candidate_id": payload.get("candidate_id"),
        "merge_intent": str(payload.get("merge_intent") or "create_candidate_section"),
        "source_run_id": payload.get("source_run_id"),
        "compare_partner_run_id": payload.get("compare_partner_run_id"),
        "source_section_id": payload.get("source_section_id"),
        "source_section_title": payload.get("source_section_title"),
        "target_section_id": payload.get("target_section_id"),
        "target_section_title": payload.get("target_section_title"),
        "patch_scope": str(payload.get("patch_scope") or "section"),
        "destructive": bool(payload.get("destructive", False)),
        "requires_confirmation": bool(payload.get("requires_confirmation", False)),
        "completeness": str(payload.get("completeness") or "coarse"),
        "provenance": payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {},
        "warnings": [str(item) for item in payload.get("warnings", []) if str(item).strip()],
        "conflict_flags": [str(item) for item in payload.get("conflict_flags", []) if str(item).strip()],
        "conflict_details": [dict(item) for item in payload.get("conflict_details", []) if isinstance(item, dict)],
        "candidate_conflict_summary": dict(payload.get("candidate_conflict_summary") or {}),
        "target_resolution": {
            "original_target_section_id": target_resolution.get("original_target_section_id"),
            "resolved_target_section_id": target_resolution.get("resolved_target_section_id"),
            "resolution_mode": str(target_resolution.get("resolution_mode") or "fallback"),
            "resolution_confidence": float(target_resolution.get("resolution_confidence", 0.0) or 0.0),
            "retarget_warnings": [str(item) for item in target_resolution.get("retarget_warnings", []) if str(item).strip()],
        },
        "blocks": [
            _normalize_structured_patch_block(block, index=index)
            for index, block in enumerate(payload.get("blocks", []), start=1)
            if isinstance(block, dict)
        ],
        "created_at": payload.get("created_at"),
    }
    return outline_history.enrich_structured_patch_conflicts(normalized)


def _normalize_action_request(payload: dict[str, Any]) -> dict[str, Any]:
    request_payload = payload.get("payload") or {}
    return {
        "editing_session_id": payload.get("editing_session_id"),
        "workspace_view_id": payload.get("workspace_view_id"),
        "action_type": payload.get("action_type"),
        "source_run_id": payload.get("source_run_id"),
        "compare_partner_run_id": payload.get("compare_partner_run_id"),
        "source_section_id": payload.get("source_section_id"),
        "source_section_title": payload.get("source_section_title"),
        "target_section_id": payload.get("target_section_id"),
        "target_position": payload.get("target_position"),
        "preferred_report_side": payload.get("preferred_report_side") or "both",
        "source_bucket_id": payload.get("source_bucket_id"),
        "source_theme": payload.get("source_theme"),
        "source_paper_ids": [str(paper_id).strip() for paper_id in payload.get("source_paper_ids", []) if str(paper_id).strip()],
        "preset_id": payload.get("preset_id"),
        "selection_source": payload.get("selection_source"),
        "payload": {
            "title": request_payload.get("title"),
            "body_markdown": request_payload.get("body_markdown"),
            "paper_ids": [str(paper_id).strip() for paper_id in request_payload.get("paper_ids", []) if str(paper_id).strip()],
            "compare_note": request_payload.get("compare_note"),
            "metadata": request_payload.get("metadata") or {},
        },
        "warnings": [str(item) for item in payload.get("warnings", []) if str(item).strip()],
    }


def _normalize_candidate(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = _normalize_action_request(payload.get("request") if isinstance(payload.get("request"), dict) else payload)
    structured_patch = _normalize_structured_patch(payload.get("structured_patch"))
    return {
        "candidate_id": str(payload.get("candidate_id") or payload.get("action_id") or ""),
        "editing_session_id": payload.get("editing_session_id"),
        "source_run_id": str(payload.get("source_run_id") or run_id),
        "compare_partner_run_id": payload.get("compare_partner_run_id"),
        "workspace_view_id": payload.get("workspace_view_id"),
        "source_section_id": payload.get("source_section_id"),
        "source_section_title": payload.get("source_section_title"),
        "source_bucket_id": payload.get("source_bucket_id"),
        "source_theme": payload.get("source_theme"),
        "source_paper_ids": [str(paper_id).strip() for paper_id in payload.get("source_paper_ids", []) if str(paper_id).strip()],
        "target_section_id": payload.get("target_section_id"),
        "target_section_title": payload.get("target_section_title"),
        "target_index": payload.get("target_index"),
        "merge_intent": payload.get("merge_intent") or MERGE_INTENT_BY_ACTION_TYPE.get(request.get("action_type"), "create_candidate_section"),
        "preview_blocks": [
            _normalize_preview_block(block, index=index)
            for index, block in enumerate(payload.get("preview_blocks", []), start=1)
            if isinstance(block, dict)
        ],
        "changed_block_count": int(payload.get("changed_block_count", 0) or 0),
        "citation_delta": payload.get("citation_delta") if isinstance(payload.get("citation_delta"), dict) else {"left_count": 0, "right_count": 0, "delta": 0, "notes": []},
        "warnings": [str(item) for item in payload.get("warnings", []) if str(item).strip()],
        "conflict_flags": [str(item) for item in payload.get("conflict_flags", []) if str(item).strip()],
        "missing_artifacts": [str(item) for item in payload.get("missing_artifacts", []) if str(item).strip()],
        "destructive": bool(payload.get("destructive", False)),
        "requires_confirmation": bool(payload.get("requires_confirmation", False)),
        "provenance": payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {},
        "summary_lines": [str(item) for item in payload.get("summary_lines", []) if str(item).strip()],
        "completeness": str(payload.get("completeness") or "coarse"),
        "structured_patch": structured_patch,
        "conflict_summary": dict(payload.get("conflict_summary") or (structured_patch or {}).get("candidate_conflict_summary") or {}),
        "request": request,
        "created_at": str(payload.get("created_at") or iso_now_utc()),
        "order": int(payload.get("order", 0) or 0),
    }


def _next_candidate_order(items: list[dict[str, Any]], editing_session_id: str | None) -> int:
    relevant = [
        int(item.get("order", 0) or 0)
        for item in items
        if (item.get("editing_session_id") or None) == (editing_session_id or None)
    ]
    return (max(relevant) + 1) if relevant else 1


def _sync_editing_session_membership(
    run_id: str,
    editing_session_id: str | None,
    *,
    add_ids: list[str] | None = None,
    remove_ids: list[str] | None = None,
    ordered_ids: list[str] | None = None,
    update_outline_hash: bool = False,
) -> None:
    if not editing_session_id:
        return
    session = editing_sessions.get_editing_session(run_id, editing_session_id)
    staged_ids = list(session.get("staged_action_ids", []))
    pending_order = list(session.get("pending_merge_order", [])) or list(staged_ids)
    if add_ids:
        for action_id in add_ids:
            if action_id not in staged_ids:
                staged_ids.append(action_id)
            if action_id not in pending_order:
                pending_order.append(action_id)
    if remove_ids:
        removal = set(remove_ids)
        staged_ids = [action_id for action_id in staged_ids if action_id not in removal]
        pending_order = [action_id for action_id in pending_order if action_id not in removal]
    if ordered_ids is not None:
        ordered = [action_id for action_id in ordered_ids if action_id in staged_ids]
        remaining = [action_id for action_id in staged_ids if action_id not in ordered]
        pending_order = ordered + remaining
        staged_ids = pending_order
    updates: dict[str, Any] = {
        "staged_action_ids": staged_ids,
        "pending_merge_order": pending_order or staged_ids,
    }
    if update_outline_hash:
        updates["outline_draft_hash"] = reporting.compute_outline_draft_hash(run_id=run_id)
    editing_sessions.update_editing_session(run_id, editing_session_id, updates)


def attach_staged_actions_to_editing_session(run_id: str, editing_session_id: str, candidate_ids: list[str]) -> list[dict[str, Any]]:
    if not candidate_ids:
        return list_staged_workspace_actions(run_id, editing_session_id=editing_session_id).get("items", [])
    items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
    candidate_set = set(candidate_ids)
    for item in items:
        if item.get("candidate_id") in candidate_set:
            item["editing_session_id"] = editing_session_id
            item["request"]["editing_session_id"] = editing_session_id
    _write_staged_actions(run_id, items)
    _sync_editing_session_membership(run_id, editing_session_id, add_ids=[item["candidate_id"] for item in items if item.get("candidate_id") in candidate_set])
    return list_staged_workspace_actions(run_id, editing_session_id=editing_session_id).get("items", [])


def _delete_staged_action_ids(run_id: str, candidate_ids: list[str]) -> None:
    if not candidate_ids:
        return
    candidate_set = set(candidate_ids)
    items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
    kept = [item for item in items if item.get("candidate_id") not in candidate_set]
    _write_staged_actions(run_id, kept)


def delete_staged_actions_for_session(run_id: str, editing_session_id: str) -> None:
    session_items = list_staged_workspace_actions(run_id, editing_session_id=editing_session_id).get("items", [])
    _delete_staged_action_ids(run_id, [item.get("candidate_id") for item in session_items if item.get("candidate_id")])


def _get_outline(run_id: str) -> dict[str, Any]:
    return reporting.load_outline(run_id)


def _find_section_index(sections: list[dict[str, Any]], section_id: str | None) -> int:
    if not section_id:
        return -1
    for index, section in enumerate(sections):
        if section.get("section_id") == section_id:
            return index
    return -1


def _section_excerpt(body_markdown: str | None) -> str | None:
    text = str(body_markdown or "").strip()
    if not text:
        return None
    compact = " ".join(text.split())
    return compact[:240]


def _normalize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "title": payload.get("title"),
        "body_markdown": payload.get("body_markdown"),
        "paper_ids": [str(paper_id).strip() for paper_id in payload.get("paper_ids", []) if str(paper_id).strip()],
        "compare_note": payload.get("compare_note"),
        "metadata": payload.get("metadata") or {},
    }


def _build_provenance(action: dict[str, Any], compare_partner_run_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "source_run_id": action.get("source_run_id"),
        "compare_partner_run_id": compare_partner_run_id,
        "source_section_id": action.get("source_section_id"),
        "source_section_title": action.get("source_section_title"),
        "source_bucket_id": action.get("source_bucket_id"),
        "source_theme": action.get("source_theme"),
        "source_paper_ids": action.get("source_paper_ids", []),
        "preferred_report_side": action.get("preferred_report_side"),
        "created_at": iso_now_utc(),
        "payload_title": payload.get("title"),
    }


def _candidate_title(action: dict[str, Any], payload: dict[str, Any], section_seed: dict[str, Any] | None) -> str:
    if payload.get("title"):
        return str(payload["title"]).strip()
    if section_seed and section_seed.get("section_title"):
        return str(section_seed["section_title"]).strip()
    if action.get("source_theme"):
        return f"{action['source_theme']} Focus"
    if action.get("action_type") == "apply_paper_seed_suggestion":
        return "Paper Seed Section"
    return "Candidate Section"


def _candidate_body(action: dict[str, Any], payload: dict[str, Any], section_seed: dict[str, Any] | None) -> str:
    if payload.get("body_markdown"):
        return str(payload["body_markdown"])
    if section_seed and section_seed.get("body_markdown"):
        return str(section_seed["body_markdown"])
    if section_seed and section_seed.get("excerpt"):
        return str(section_seed["excerpt"])
    if action.get("action_type") == "apply_theme_outline_suggestion":
        theme = action.get("source_theme") or "Uncategorized"
        related = action.get("source_paper_ids", [])
        lines = [
            f"- Theme focus: {theme}",
            f"- Related papers: {len(related)}",
            "- Compare-driven suggestion: capture why this theme changed and what to review next.",
        ]
        return "\n".join(lines)
    if action.get("action_type") == "apply_paper_seed_suggestion":
        lines = [
            "- Compare-selected papers seeded into a dedicated section.",
            "- Review the linked papers and convert this section into a stronger narrative after inspection.",
        ]
        return "\n".join(lines)
    return ""


def _candidate_paper_ids(action: dict[str, Any], payload: dict[str, Any], section_seed: dict[str, Any] | None) -> list[str]:
    if payload.get("paper_ids"):
        return payload["paper_ids"]
    if section_seed and section_seed.get("paper_ids"):
        return list(section_seed["paper_ids"])
    return [paper_id for paper_id in action.get("source_paper_ids", []) if paper_id]


def _build_candidate_section(action: dict[str, Any], section_seed: dict[str, Any] | None) -> dict[str, Any]:
    payload = _normalize_payload(action.get("payload"))
    title = _candidate_title(action, payload, section_seed)
    body_markdown = _candidate_body(action, payload, section_seed)
    paper_ids = _candidate_paper_ids(action, payload, section_seed)
    kind = "papers" if paper_ids else "markdown"
    return {
        "section_id": f"{slugify(title)}-candidate" if title else "candidate-section",
        "title": title,
        "kind": kind,
        "body_markdown": body_markdown,
        "paper_ids": paper_ids,
        "compare_notes": [payload["compare_note"]] if payload.get("compare_note") else [],
        "provenance": [],
    }


def _resolve_seed_from_action(run_id: str, action: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    warnings: list[str] = []
    missing_artifacts: list[str] = []
    source_run_id = action.get("source_run_id") or run_id
    compare_partner_run_id = action.get("compare_partner_run_id")
    source_section_id = action.get("source_section_id")
    source_section_title = action.get("source_section_title")
    if source_run_id == run_id and compare_partner_run_id and (source_section_id or source_section_title):
        try:
            context = compare_context.resolve_run_compare_context(
                run_id,
                compare_partner_run_id=compare_partner_run_id,
                compare_mode="report",
                compare_view="report",
                focus_section_id=source_section_id,
                focus_section_title=source_section_title,
                preferred_report_side=action.get("preferred_report_side"),
                selection_source=action.get("selection_source"),
                seed_section=True,
            )
        except FileNotFoundError:
            missing_artifacts.append(f"Source artifacts for compare run {compare_partner_run_id} are unavailable.")
            return None, warnings, missing_artifacts
        warnings.extend(context.get("warnings", []))
        if context.get("pending_section_seed") is None:
            missing_artifacts.append("Requested compare source section could not be resolved.")
        return context.get("pending_section_seed"), warnings, missing_artifacts
    return None, warnings, missing_artifacts


def preview_workspace_action(
    run_id: str,
    payload: dict[str, Any],
    *,
    outline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview one workspace action.

    ``outline`` lets a caller preview against an in-memory outline instead of the
    persisted one. Batch apply uses it to fold candidates without writing between
    them, so every candidate is still evaluated against the state the previous
    candidate would produce.
    """
    action = dict(payload)
    action.setdefault("source_run_id", run_id)
    action["action_id"] = str(
        action.get("action_id")
        or action.get("candidate_id")
        or _make_action_id(action.get("action_type", "workspace-action"), action.get("source_section_title"))
    )
    outline = outline if outline is not None else _get_outline(run_id)
    sections = [dict(section) for section in outline.get("sections", [])]
    payload_data = _normalize_payload(action.get("payload"))
    warnings: list[str] = list(action.get("warnings", []))
    conflicts: list[str] = []
    missing_artifacts: list[str] = []
    compare_partner_run_id = action.get("compare_partner_run_id")
    section_seed, seed_warnings, seed_missing_artifacts = _resolve_seed_from_action(run_id, action)
    warnings.extend(seed_warnings)
    missing_artifacts.extend(seed_missing_artifacts)
    candidate_section = _build_candidate_section(action, section_seed)
    target_section_id = action.get("target_section_id") or (section_seed or {}).get("matched_outline_section_id")
    target_index = _find_section_index(sections, target_section_id)
    destructive = False
    summary_lines: list[str] = []
    proposed_compare_note: str | None = None

    action_type = action.get("action_type")
    if action_type == "insert_section_before":
        if target_index < 0:
            conflicts.append("Target section was not found. The preview will append the section to the end instead.")
            target_index = len(sections)
        summary_lines.append("Insert a new candidate section before the target section.")
    elif action_type == "insert_section_after":
        if target_index < 0:
            conflicts.append("Target section was not found. The preview will append the section to the end instead.")
            target_index = len(sections) - 1 if sections else -1
        summary_lines.append("Insert a new candidate section after the target section.")
    elif action_type == "append_to_target_section":
        if target_index < 0:
            conflicts.append("Target section is required for append_to_target_section.")
        else:
            target = sections[target_index]
            destructive = bool(target.get("body_markdown") or target.get("paper_ids"))
            summary_lines.append("Append compare-derived content to the target draft section.")
    elif action_type == "replace_section_draft":
        if target_index < 0:
            conflicts.append("Target section is required for replace_section_draft.")
        else:
            target = sections[target_index]
            destructive = bool(target.get("body_markdown") or target.get("paper_ids"))
            summary_lines.append("Replace the target draft section with compare-derived content.")
    elif action_type == "create_candidate_section":
        target_index = len(sections)
        summary_lines.append("Create a new candidate section at the end of the outline.")
    elif action_type == "attach_compare_note":
        if target_index < 0:
            conflicts.append("Target section is required for attach_compare_note.")
        note_body = payload_data.get("compare_note") or (
            f"Compare note from {compare_partner_run_id or 'compare context'} · "
            f"{action.get('source_section_title') or action.get('source_theme') or 'selection'}"
        )
        proposed_compare_note = note_body
        summary_lines.append("Attach a compare note to the target section without changing the section body.")
    elif action_type == "apply_theme_outline_suggestion":
        target_index = len(sections)
        summary_lines.append("Create a new theme-focused outline candidate section.")
    elif action_type == "apply_paper_seed_suggestion":
        target_index = len(sections)
        summary_lines.append("Create a new paper-seed section from compare-selected papers.")

    effective_target_id = sections[target_index]["section_id"] if 0 <= target_index < len(sections) else None
    effective_target_title = sections[target_index]["title"] if 0 <= target_index < len(sections) else None
    if action_type == "replace_section_draft" and target_index >= 0:
        proposed_section = {
            **sections[target_index],
            "title": candidate_section["title"] or sections[target_index].get("title"),
            "kind": candidate_section["kind"],
            "body_markdown": candidate_section["body_markdown"],
            "paper_ids": candidate_section["paper_ids"],
            "compare_notes": sections[target_index].get("compare_notes", []),
            "provenance": sections[target_index].get("provenance", []),
        }
    elif action_type == "append_to_target_section" and target_index >= 0:
        target = sections[target_index]
        append_body = candidate_section["body_markdown"]
        combined_body = "\n\n".join([part for part in [target.get("body_markdown", ""), append_body] if str(part).strip()])
        merged_paper_ids = list(dict.fromkeys([*target.get("paper_ids", []), *candidate_section.get("paper_ids", [])]))
        proposed_section = {
            **target,
            "title": target.get("title"),
            "kind": "papers" if merged_paper_ids else target.get("kind"),
            "body_markdown": combined_body,
            "paper_ids": merged_paper_ids,
            "compare_notes": target.get("compare_notes", []),
            "provenance": target.get("provenance", []),
        }
    elif action_type == "attach_compare_note":
        proposed_section = None
    else:
        proposed_section = candidate_section

    merge_intent = MERGE_INTENT_BY_ACTION_TYPE.get(action_type, "create_candidate_section")
    target_section = sections[target_index] if isinstance(target_index, int) and 0 <= target_index < len(sections) else None
    patch_preview = outline_history.build_body_patch_preview(
        merge_intent=merge_intent,
        target_section=target_section,
        proposed_section=proposed_section,
        proposed_compare_note=proposed_compare_note,
        warnings=warnings,
    )
    preview_blocks = [
        _normalize_preview_block(block, index=index)
        for index, block in enumerate(patch_preview.get("preview_blocks", []), start=1)
    ]
    changed_block_count = int(patch_preview.get("changed_block_count", 0) or 0)
    completeness = str(patch_preview.get("completeness") or "coarse")
    requires_confirmation = bool(destructive)
    provenance = _build_provenance(action, compare_partner_run_id, payload_data)
    structured_patch = outline_history.build_structured_patch(
        candidate_id=action["action_id"],
        merge_intent=merge_intent,
        preview_blocks=preview_blocks,
        completeness=completeness,
        target_section=target_section,
        proposed_section=proposed_section,
        proposed_compare_note=proposed_compare_note,
        warnings=warnings,
        conflict_flags=conflicts,
        source_run_id=action.get("source_run_id") or run_id,
        compare_partner_run_id=compare_partner_run_id,
        source_section_id=action.get("source_section_id"),
        source_section_title=action.get("source_section_title"),
        target_section_id=effective_target_id or target_section_id,
        target_section_title=effective_target_title or action.get("target_section_id"),
        destructive=destructive,
        requires_confirmation=requires_confirmation,
        provenance=provenance,
        created_at=iso_now_utc(),
    )
    preview_payload = {
        "run_id": run_id,
        "action_id": action["action_id"],
        "action_type": action_type,
        "preview_ready": len(conflicts) == 0,
        "effective_target": {
            "target_section_id": effective_target_id or target_section_id,
            "target_section_title": effective_target_title or action.get("target_section_id"),
            "target_index": target_index if target_index >= 0 else None,
            "destructive": destructive,
        },
        "proposed_section": proposed_section,
        "proposed_compare_note": proposed_compare_note,
        "provenance": provenance,
        "warnings": warnings,
        "conflicts": conflicts,
        "missing_artifacts": missing_artifacts,
        "summary_lines": summary_lines,
        "preview_blocks": preview_blocks,
        "changed_block_count": changed_block_count,
        "citation_delta": patch_preview.get("citation_delta", {"left_count": 0, "right_count": 0, "delta": 0, "notes": []}),
        "destructive": destructive,
        "requires_confirmation": requires_confirmation,
        "completeness": completeness,
        "structured_patch": structured_patch,
    }
    preview_item = {
        "candidate_id": action["action_id"],
        "merge_intent": merge_intent,
        "target_section_id": effective_target_id or target_section_id,
        "target_index": target_index if target_index >= 0 else None,
        "destructive": destructive,
        "requires_confirmation": requires_confirmation,
        "source_paper_ids": action.get("source_paper_ids", []),
        "provenance": provenance,
    }
    assessment = _candidate_runtime_assessment(
        run_id,
        preview_item,
        preview_payload,
        editing_session_id=action.get("editing_session_id"),
    )
    preview_payload["structured_patch"] = assessment["structured_patch"]
    preview_payload["conflict_details"] = assessment["conflict_details"]
    preview_payload["candidate_conflict_summary"] = assessment["conflict_summary"]
    return preview_payload


def _plan_workspace_action(
    run_id: str,
    payload: dict[str, Any],
    *,
    outline: dict[str, Any],
) -> dict[str, Any]:
    """Pure planner for one action against ``outline``.

    Validates the action and computes the resulting sections plus the metadata
    needed to finalize it. Performs **no** persistence: no outline save, no
    history append, no queue mutation, no directory creation. Raising here
    guarantees zero writes, which is what makes batch apply all-or-nothing.
    """
    preview = preview_workspace_action(run_id, payload, outline=outline)
    if preview["conflicts"]:
        raise RuntimeError("; ".join(preview["conflicts"]))
    if preview["effective_target"]["destructive"] and not payload.get("confirm_destructive"):
        raise RuntimeError("This action is destructive. Set confirm_destructive=true after reviewing the preview.")

    before_outline_hash = reporting.compute_outline_draft_hash(outline=outline)
    sections = [dict(section) for section in outline.get("sections", [])]
    target_index = preview["effective_target"].get("target_index")
    action_type = preview["action_type"]
    merge_intent = MERGE_INTENT_BY_ACTION_TYPE.get(action_type, "create_candidate_section")
    provenance = preview["provenance"]
    proposed_section = preview.get("proposed_section")
    applied_warnings = list(preview.get("warnings", []))
    candidate_id = str(payload.get("candidate_id") or payload.get("action_id") or preview["action_id"])
    applied_action = {
        "action_id": preview["action_id"],
        "candidate_id": candidate_id,
        "editing_session_id": payload.get("editing_session_id"),
        "action_type": action_type,
        "merge_intent": merge_intent,
        "source_run_id": provenance.get("source_run_id"),
        "compare_partner_run_id": provenance.get("compare_partner_run_id"),
        "source_section_id": provenance.get("source_section_id"),
        "source_section_title": provenance.get("source_section_title"),
        "source_bucket_id": provenance.get("source_bucket_id"),
        "source_theme": provenance.get("source_theme"),
        "source_paper_ids": provenance.get("source_paper_ids", []),
        "created_at": provenance.get("created_at"),
    }

    if action_type == "insert_section_before":
        insert_at = target_index if isinstance(target_index, int) and target_index >= 0 else len(sections)
        section = dict(proposed_section or {})
        section.setdefault("provenance", []).append(provenance)
        sections.insert(insert_at, section)
        message = "Compare section을 target section 앞에 draft section으로 추가했습니다."
    elif action_type == "insert_section_after":
        insert_at = target_index + 1 if isinstance(target_index, int) and target_index >= 0 else len(sections)
        section = dict(proposed_section or {})
        section.setdefault("provenance", []).append(provenance)
        sections.insert(insert_at, section)
        message = "Compare section을 target section 뒤에 draft section으로 추가했습니다."
    elif action_type == "append_to_target_section":
        if not isinstance(target_index, int) or target_index < 0:
            raise RuntimeError("Target section was not found for append_to_target_section.")
        appended = dict(sections[target_index])
        appended.update(
            {
                "title": proposed_section.get("title"),
                "kind": proposed_section.get("kind"),
                "body_markdown": proposed_section.get("body_markdown"),
                "paper_ids": proposed_section.get("paper_ids", []),
            }
        )
        appended.setdefault("provenance", []).append(provenance)
        sections[target_index] = appended
        message = "Target section에 compare-derived fragment를 append했습니다."
    elif action_type == "replace_section_draft":
        if not isinstance(target_index, int) or target_index < 0:
            raise RuntimeError("Target section was not found for replace_section_draft.")
        replaced = dict(sections[target_index])
        replaced.update(
            {
                "title": proposed_section.get("title"),
                "kind": proposed_section.get("kind"),
                "body_markdown": proposed_section.get("body_markdown"),
                "paper_ids": proposed_section.get("paper_ids", []),
            }
        )
        replaced.setdefault("provenance", []).append(provenance)
        sections[target_index] = replaced
        message = "Target draft section을 compare-derived content로 교체했습니다."
    elif action_type == "create_candidate_section":
        section = dict(proposed_section or {})
        section.setdefault("provenance", []).append(provenance)
        sections.append(section)
        message = "새 candidate section을 outline 끝에 추가했습니다."
    elif action_type == "attach_compare_note":
        if not isinstance(target_index, int) or target_index < 0:
            raise RuntimeError("Target section was not found for attach_compare_note.")
        target = dict(sections[target_index])
        notes = list(target.get("compare_notes", []))
        notes.append(preview.get("proposed_compare_note") or "Compare note")
        target["compare_notes"] = notes
        target.setdefault("provenance", []).append(provenance)
        sections[target_index] = target
        message = "Compare note를 section metadata에 추가했습니다."
    elif action_type in {"apply_theme_outline_suggestion", "apply_paper_seed_suggestion"}:
        section = dict(proposed_section or {})
        section.setdefault("provenance", []).append(provenance)
        sections.append(section)
        message = "Suggestion을 candidate section으로 추가했습니다."
    else:
        raise RuntimeError(f"Unsupported workspace action: {action_type}")

    existing_actions = list(outline.get("applied_actions", []))
    existing_actions.append(applied_action)
    # Projected next outline state, in memory only.
    projected_outline = {**outline, "sections": sections, "applied_actions": existing_actions}
    after_outline_hash = reporting.compute_outline_draft_hash(outline=projected_outline)
    before_sections_by_id = {section.get("section_id"): section for section in outline.get("sections", [])}
    after_sections_by_id = {section.get("section_id"): section for section in projected_outline.get("sections", [])}
    affected_section_ids: list[str] = []
    if action_type in {"insert_section_before", "insert_section_after", "create_candidate_section", "apply_theme_outline_suggestion", "apply_paper_seed_suggestion"}:
        before_ids = set(before_sections_by_id)
        after_ids = [section_id for section_id in after_sections_by_id if section_id not in before_ids]
        affected_section_ids.extend(after_ids)
    if preview["effective_target"].get("target_section_id"):
        affected_section_ids.append(str(preview["effective_target"]["target_section_id"]))
    affected_section_ids = list(dict.fromkeys([section_id for section_id in affected_section_ids if section_id]))
    history_affected_sections = [
        outline_history.build_history_affected_section(
            merge_intent=merge_intent,
            before_section=before_sections_by_id.get(section_id),
            after_section=after_sections_by_id.get(section_id),
            compare_note=preview.get("proposed_compare_note"),
            warnings=applied_warnings,
        )
        for section_id in affected_section_ids
    ]
    history_entry = {
        "action_id": preview["action_id"],
        "candidate_id": candidate_id,
        "editing_session_id": payload.get("editing_session_id"),
        "merge_intent": merge_intent,
        "source_run_id": provenance.get("source_run_id"),
        "compare_partner_run_id": provenance.get("compare_partner_run_id"),
        "source_section_id": provenance.get("source_section_id"),
        "target_section_id": preview["effective_target"].get("target_section_id"),
        "timestamp": provenance.get("created_at") or iso_now_utc(),
        "outcome": "applied",
        "warnings": applied_warnings,
        "before_outline_hash": before_outline_hash,
        "after_outline_hash": after_outline_hash,
        "affected_sections": history_affected_sections,
        "provenance": provenance,
        "preview_summary": preview.get("summary_lines", []),
    }
    return {
        "action_id": preview["action_id"],
        "action_type": action_type,
        "candidate_id": candidate_id,
        "message": message,
        "warnings": applied_warnings,
        "sections": sections,
        "applied_actions": existing_actions,
        "applied_action": applied_action,
        "projected_outline": projected_outline,
        "history_entry": history_entry,
        "editing_session_id": payload.get("editing_session_id"),
        "before_outline_hash": before_outline_hash,
        "after_outline_hash": after_outline_hash,
    }


def _commit_outline_transaction(
    run_id: str,
    *,
    base_outline_hash: str,
    sections: list[dict[str, Any]],
    applied_actions: list[dict[str, Any]],
    history_entries: list[dict[str, Any]],
    kind: str = report_transaction.KIND_SINGLE_APPLY,
    candidate_ids: list[str] | None = None,
    editing_session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Commit a planned report mutation as one logical transaction.

    Holds the per-run cross-process mutation lock, re-reads the authoritative
    base and compare-and-swaps it against the digest the plan was built on, then
    performs the single atomic outline replace that is the commit point. History
    entries are appended only after the commit succeeds; a history append failure
    never rolls back the committed report.
    """
    with report_transaction.report_mutation_lock(run_id):
        # Finish or refuse any transaction left behind by an earlier crash
        # before planning anything new against this run.
        recovery = _recover_incomplete_transaction(run_id)

        current_hash = reporting.compute_outline_draft_hash(outline=_get_outline(run_id))
        report_transaction.ensure_base_unchanged(
            expected_digest=base_outline_hash,
            actual_digest=current_hash,
        )

        transaction_id = report_transaction.new_transaction_id()
        now = iso_now_utc()
        stamped_entries = [
            {**entry, "transaction_id": transaction_id, "transaction_kind": kind}
            for entry in history_entries
        ]
        # Predict the digest the commit will actually produce by normalizing
        # exactly the way save_outline does; hashing the raw projection would
        # not match the persisted outline and would make every recovery look
        # like a digest mismatch.
        target_digest = reporting.compute_outline_draft_hash(
            outline=reporting._normalize_outline(
                run_id, {"sections": sections, "applied_actions": applied_actions}
            )
        )
        journal = report_transaction.build_journal(
            transaction_id=transaction_id,
            kind=kind,
            run_id=run_id,
            base_digest=current_hash,
            target_digest=target_digest,
            candidate_ids=list(candidate_ids or []),
            editing_session_ids=list(editing_session_ids or []),
            now=now,
        )
        # Durable intent BEFORE the commit: a prepare failure means zero commit.
        report_transaction.write_journal(run_id, journal)

        # Single authoritative commit point for the whole batch.
        saved_outline = reporting.save_outline(run_id, sections, applied_actions=applied_actions)

        try:
            journal = report_transaction.mark_journal_state(
                run_id, journal, report_transaction.STATE_COMMITTED, now=iso_now_utc()
            )
        except report_transaction.ReportTransactionPrepareFailed:
            # The commit stands; recovery classifies by digest, not journal state.
            pass

        post_commit_warnings = _finalize_committed_transaction(
            run_id,
            journal=journal,
            history_entries=stamped_entries,
            candidate_ids=list(candidate_ids or []),
            editing_session_ids=list(editing_session_ids or []),
        )
        return {
            "outline": saved_outline,
            "post_commit_warnings": post_commit_warnings,
            "recovery": recovery,
            "transaction_id": transaction_id,
        }


def _history_entry_identity(entry: dict[str, Any]) -> tuple[str, str]:
    return (str(entry.get("transaction_id") or ""), str(entry.get("candidate_id") or entry.get("action_id") or ""))


def _finalize_committed_transaction(
    run_id: str,
    *,
    journal: dict[str, Any],
    history_entries: list[dict[str, Any]],
    candidate_ids: list[str],
    editing_session_ids: list[str],
) -> list[str]:
    """Idempotently finalize secondary state after the outline commit.

    Safe to run repeatedly: history entries are appended once per
    (transaction, candidate), queue removal is a single atomic write over the
    frozen id set, and session sync is repeat-safe. A failure here never rolls
    back the committed report; it is surfaced as a cleanup-pending warning so a
    later mutation can complete the finalize.
    """
    warnings: list[str] = []

    # 1) History: append only the entries that are not present yet, in one write.
    try:
        existing = outline_history._read_history(run_id)
        existing_ids = {_history_entry_identity(item) for item in existing}
        missing = [entry for entry in history_entries if _history_entry_identity(entry) not in existing_ids]
        if missing:
            outline_history._write_history(run_id, existing + missing)
    except OSError:
        warnings.append(report_transaction.CODE_CLEANUP_PENDING)
        return warnings

    # 2) Editing sessions: repeat-safe membership sync.
    try:
        for session_id in dict.fromkeys(editing_session_ids):
            if session_id:
                _sync_editing_session_membership(
                    run_id, session_id, remove_ids=list(candidate_ids), update_outline_hash=True
                )
    except (OSError, RuntimeError, FileNotFoundError):
        warnings.append(report_transaction.CODE_CLEANUP_PENDING)
        return warnings

    # 3) Queue: one atomic removal of exactly the frozen candidate ids.
    try:
        if candidate_ids:
            _delete_staged_action_ids(run_id, list(candidate_ids))
    except OSError:
        warnings.append(report_transaction.CODE_CLEANUP_PENDING)
        return warnings

    # 4) Journal cleanup. Failure is non-fatal and retried by the next mutation.
    if not report_transaction.clear_journal(run_id):
        warnings.append(report_transaction.CODE_CLEANUP_PENDING)
    return warnings


def _recover_incomplete_transaction(run_id: str) -> str | None:
    """Classify and resolve a leftover transaction. Must hold the run lock.

    Returns a stable outcome code, or None when there was nothing to recover.
    Raises :class:`ReportTransactionRecoveryRequired` for ambiguous or
    contradictory state instead of guessing.
    """
    journal = report_transaction.read_journal(run_id)
    if journal is None:
        return None

    current_digest = reporting.compute_outline_draft_hash(outline=_get_outline(run_id))
    verdict = report_transaction.classify(journal=journal, current_digest=current_digest)

    if verdict == "aborted_precommit":
        # The commit never landed: touch no candidate, history or session.
        report_transaction.clear_journal(run_id)
        return report_transaction.CODE_ABORTED_PRECOMMIT

    if verdict == "finalize_required":
        # The commit landed; finish the secondary finalize idempotently. The
        # planned history entries are reconstructed from the queue only if they
        # are still missing, so nothing is duplicated and no candidate is
        # applied to the report a second time.
        _finalize_committed_transaction(
            run_id,
            journal=journal,
            history_entries=[],
            candidate_ids=[str(item) for item in journal.get("candidate_ids", [])],
            editing_session_ids=[str(item) for item in journal.get("editing_session_ids", [])],
        )
        return report_transaction.CODE_RECOVERED

    report_transaction.mark_recovery_required(
        run_id,
        journal,
        now=iso_now_utc(),
        reason_code=report_transaction.CODE_DIGEST_MISMATCH,
    )
    raise report_transaction.ReportTransactionRecoveryRequired(report_transaction.CODE_DIGEST_MISMATCH)


def apply_workspace_action(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    base_outline = _get_outline(run_id)
    plan = _plan_workspace_action(run_id, payload, outline=base_outline)
    commit = _commit_outline_transaction(
        run_id,
        base_outline_hash=plan["before_outline_hash"],
        sections=plan["sections"],
        applied_actions=plan["applied_actions"],
        history_entries=[plan["history_entry"]],
        kind=report_transaction.KIND_SINGLE_APPLY,
        candidate_ids=[],
        editing_session_ids=[payload.get("editing_session_id")] if payload.get("editing_session_id") else [],
    )
    saved_outline = commit["outline"]
    preview_payload = reporting.build_preview(run_id, outline=saved_outline)
    return {
        "run_id": run_id,
        "action_id": plan["action_id"],
        "action_type": plan["action_type"],
        "applied": True,
        "message": plan["message"],
        "warnings": list(plan["warnings"]) + commit["post_commit_warnings"],
        "outline": saved_outline,
        "preview": preview_payload,
    }


def list_staged_workspace_actions(run_id: str, *, editing_session_id: str | None = None) -> dict[str, Any]:
    warnings: list[str] = []
    items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
    if editing_session_id:
        session = editing_sessions.get_editing_session(run_id, editing_session_id)
        session_ids = list(session.get("staged_action_ids", []))
        session_set = set(session_ids)
        filtered = [
            item
            for item in items
            if item.get("editing_session_id") == editing_session_id or item.get("candidate_id") in session_set
        ]
        order_map = {action_id: index for index, action_id in enumerate(session.get("pending_merge_order", []) or session_ids)}
        filtered.sort(key=lambda item: (order_map.get(item.get("candidate_id"), 10_000), item.get("order", 0), item.get("created_at", "")))
        if session.get("warnings"):
            warnings.extend(session.get("warnings", []))
        return {
            "run_id": run_id,
            "editing_session_id": editing_session_id,
            "items": filtered,
            "warnings": warnings,
        }

    filtered = [item for item in items if not item.get("editing_session_id")]
    filtered.sort(key=lambda item: (item.get("order", 0), item.get("created_at", "")))
    return {"run_id": run_id, "editing_session_id": None, "items": filtered, "warnings": warnings}


def get_staged_workspace_action_patch(run_id: str, action_id: str, *, editing_session_id: str | None = None) -> dict[str, Any]:
    items = list_staged_workspace_actions(run_id, editing_session_id=editing_session_id).get("items", [])
    item = next((candidate for candidate in items if candidate.get("candidate_id") == action_id), None)
    if item is None:
        raise FileNotFoundError(f"Staged workspace action not found: {action_id}")
    preview = preview_workspace_action(
        run_id,
        {
            **item.get("request", {}),
            "action_id": action_id,
            "candidate_id": action_id,
            "editing_session_id": editing_session_id or item.get("editing_session_id"),
        },
    )
    staleness = inspect_editing_session_staleness(run_id, editing_session_id) if editing_session_id else None
    duplicate_target_candidate_ids: list[str] = []
    duplicate_target_section_id = preview.get("effective_target", {}).get("target_section_id") or item.get("target_section_id")
    if duplicate_target_section_id:
        duplicate_target_candidate_ids = [
            str(candidate.get("candidate_id"))
            for candidate in items
            if str(candidate.get("candidate_id"))
            and _candidate_requires_target(candidate)
            and candidate.get("target_section_id") == duplicate_target_section_id
        ]
    assessment = _candidate_runtime_assessment(
        run_id,
        item,
        preview,
        editing_session_id=editing_session_id or item.get("editing_session_id"),
        staleness=staleness,
        duplicate_target_candidate_ids=duplicate_target_candidate_ids,
        duplicate_target_section_id=duplicate_target_section_id,
    )
    return assessment.get("structured_patch") or preview.get("structured_patch") or item.get("structured_patch") or {}


def stage_workspace_action(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    preview = preview_workspace_action(run_id, payload)
    request_payload = _normalize_action_request(payload)
    candidate = _normalize_candidate(
        run_id,
        {
            "candidate_id": preview["action_id"],
            "editing_session_id": payload.get("editing_session_id"),
            "source_run_id": request_payload.get("source_run_id") or run_id,
            "compare_partner_run_id": request_payload.get("compare_partner_run_id"),
            "workspace_view_id": request_payload.get("workspace_view_id"),
            "source_section_id": request_payload.get("source_section_id"),
            "source_section_title": request_payload.get("source_section_title"),
            "source_bucket_id": request_payload.get("source_bucket_id"),
            "source_theme": request_payload.get("source_theme"),
            "source_paper_ids": request_payload.get("source_paper_ids", []),
            "target_section_id": preview.get("effective_target", {}).get("target_section_id") or request_payload.get("target_section_id"),
            "target_section_title": preview.get("effective_target", {}).get("target_section_title"),
            "target_index": preview.get("effective_target", {}).get("target_index"),
            "merge_intent": MERGE_INTENT_BY_ACTION_TYPE.get(request_payload.get("action_type"), "create_candidate_section"),
            "preview_blocks": preview.get("preview_blocks", []),
            "changed_block_count": preview.get("changed_block_count", 0),
            "citation_delta": preview.get("citation_delta", {"left_count": 0, "right_count": 0, "delta": 0, "notes": []}),
            "warnings": preview.get("warnings", []),
            "conflict_flags": preview.get("conflicts", []),
            "missing_artifacts": preview.get("missing_artifacts", []),
            "destructive": preview.get("destructive", preview.get("effective_target", {}).get("destructive", False)),
            "requires_confirmation": preview.get("requires_confirmation", preview.get("effective_target", {}).get("destructive", False)),
            "provenance": preview.get("provenance", {}),
            "summary_lines": preview.get("summary_lines", []),
            "completeness": preview.get("completeness", "coarse"),
            "structured_patch": preview.get("structured_patch"),
            "conflict_summary": preview.get("candidate_conflict_summary", {}),
            "request": request_payload,
            "created_at": iso_now_utc(),
        },
    )
    items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
    items = [item for item in items if item.get("candidate_id") != candidate["candidate_id"]]
    candidate["order"] = _next_candidate_order(items, candidate.get("editing_session_id"))
    items.append(candidate)
    _write_staged_actions(run_id, items)
    if candidate.get("editing_session_id"):
        _sync_editing_session_membership(run_id, candidate.get("editing_session_id"), add_ids=[candidate["candidate_id"]])
    return candidate


def reorder_staged_workspace_actions(run_id: str, *, editing_session_id: str | None = None, ordered_action_ids: list[str]) -> dict[str, Any]:
    items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
    target_items = list_staged_workspace_actions(run_id, editing_session_id=editing_session_id).get("items", [])
    current_ids = [item.get("candidate_id") for item in target_items]
    if not current_ids:
        return {"run_id": run_id, "editing_session_id": editing_session_id, "items": [], "warnings": []}
    requested_ids = [action_id for action_id in ordered_action_ids if action_id in current_ids]
    final_ids = requested_ids + [action_id for action_id in current_ids if action_id not in requested_ids]
    order_map = {action_id: index + 1 for index, action_id in enumerate(final_ids)}
    for item in items:
        candidate_id = item.get("candidate_id")
        if candidate_id in order_map and (item.get("editing_session_id") or None) == (editing_session_id or None):
            item["order"] = order_map[candidate_id]
    _write_staged_actions(run_id, items)
    if editing_session_id:
        _sync_editing_session_membership(run_id, editing_session_id, ordered_ids=final_ids)
    return list_staged_workspace_actions(run_id, editing_session_id=editing_session_id)


def delete_staged_workspace_action(run_id: str, action_id: str, *, editing_session_id: str | None = None) -> dict[str, Any]:
    items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
    match = next((item for item in items if item.get("candidate_id") == action_id), None)
    if match is None:
        raise FileNotFoundError(f"Staged workspace action not found: {action_id}")
    kept = [item for item in items if item.get("candidate_id") != action_id]
    _write_staged_actions(run_id, kept)
    if editing_session_id:
        _sync_editing_session_membership(run_id, editing_session_id, remove_ids=[action_id])
    elif match.get("editing_session_id"):
        _sync_editing_session_membership(run_id, match.get("editing_session_id"), remove_ids=[action_id])
    return {"run_id": run_id, "editing_session_id": editing_session_id or match.get("editing_session_id"), "items": list_staged_workspace_actions(run_id, editing_session_id=editing_session_id or match.get("editing_session_id")).get("items", []), "warnings": []}


def _selected_staged_items(
    run_id: str,
    *,
    editing_session_id: str | None = None,
    action_ids: list[str] | None = None,
    apply_all: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    staged = list_staged_workspace_actions(run_id, editing_session_id=editing_session_id)
    items = staged.get("items", [])
    if not items:
        return staged, []
    selected_ids = [action_id for action_id in (action_ids or []) if action_id]
    if apply_all or not selected_ids:
        return staged, items
    selected_set = set(selected_ids)
    selected_items = [item for item in items if item.get("candidate_id") in selected_set]
    if not selected_items:
        raise RuntimeError("Requested staged merge candidates were not found.")
    return staged, selected_items


def _candidate_preview_request(item: dict[str, Any], *, editing_session_id: str | None = None) -> dict[str, Any]:
    return {
        **item.get("request", {}),
        "action_id": str(item.get("candidate_id")),
        "candidate_id": str(item.get("candidate_id")),
        "editing_session_id": editing_session_id or item.get("editing_session_id"),
    }


def _candidate_requires_target(item: dict[str, Any]) -> bool:
    return item.get("merge_intent") in {"replace_target_section", "append_to_target_section", "attach_compare_note_only"}


def _candidate_from_preview(run_id: str, item: dict[str, Any], preview: dict[str, Any], *, editing_session_id: str | None = None) -> dict[str, Any]:
    return _normalize_candidate(
        run_id,
        {
            **item,
            "candidate_id": str(item.get("candidate_id")),
            "editing_session_id": editing_session_id or item.get("editing_session_id"),
            "target_section_id": preview.get("effective_target", {}).get("target_section_id") or item.get("target_section_id"),
            "target_section_title": preview.get("effective_target", {}).get("target_section_title") or item.get("target_section_title"),
            "target_index": preview.get("effective_target", {}).get("target_index"),
            "preview_blocks": preview.get("preview_blocks", []),
            "changed_block_count": preview.get("changed_block_count", item.get("changed_block_count", 0)),
            "citation_delta": preview.get("citation_delta", item.get("citation_delta", {})),
            "warnings": preview.get("warnings", []),
            "conflict_flags": preview.get("conflicts", []),
            "missing_artifacts": preview.get("missing_artifacts", []),
            "destructive": preview.get("destructive", item.get("destructive", False)),
            "requires_confirmation": preview.get("requires_confirmation", item.get("requires_confirmation", False)),
            "summary_lines": preview.get("summary_lines", []),
            "completeness": preview.get("completeness", item.get("completeness", "coarse")),
            "structured_patch": preview.get("structured_patch", item.get("structured_patch")),
            "conflict_summary": preview.get("candidate_conflict_summary", item.get("conflict_summary", {})),
        },
    )


def _patch_related_section_ids(structured_patch: dict[str, Any] | None, item: dict[str, Any], preview: dict[str, Any]) -> list[str]:
    values = [
        str((structured_patch or {}).get("target_section_id") or "").strip(),
        str((structured_patch or {}).get("source_section_id") or "").strip(),
        str(((structured_patch or {}).get("target_resolution") or {}).get("original_target_section_id") or "").strip(),
        str(((structured_patch or {}).get("target_resolution") or {}).get("resolved_target_section_id") or "").strip(),
        str(item.get("target_section_id") or "").strip(),
        str(preview.get("effective_target", {}).get("target_section_id") or "").strip(),
    ]
    return list(dict.fromkeys([value for value in values if value]))


def _patch_related_paper_ids(structured_patch: dict[str, Any] | None, item: dict[str, Any]) -> list[str]:
    values = [
        str(paper_id).strip()
        for paper_id in (item.get("source_paper_ids") or [])
        if str(paper_id).strip()
    ]
    if isinstance(structured_patch, dict):
        for block in structured_patch.get("blocks", []):
            if not isinstance(block, dict):
                continue
            values.extend(str(paper_id).strip() for paper_id in block.get("related_paper_ids", []) if str(paper_id).strip())
    return list(dict.fromkeys(values))


def _patch_anchor_summary(structured_patch: dict[str, Any] | None) -> str | None:
    if not isinstance(structured_patch, dict):
        return None
    details = [detail for detail in structured_patch.get("conflict_details", []) if isinstance(detail, dict)]
    weak_anchor = next((detail for detail in details if detail.get("conflict_kind") == "weak_anchor_match" and detail.get("anchor_summary")), None)
    if weak_anchor:
        return weak_anchor.get("anchor_summary")
    blocks = [block for block in structured_patch.get("blocks", []) if isinstance(block, dict)]
    if not blocks:
        return None
    average_confidence = sum(float(block.get("confidence", 0.0) or 0.0) for block in blocks) / max(len(blocks), 1)
    fallback_count = sum(
        1
        for block in blocks
        if str((block.get("anchor_before") or {}).get("anchor_kind") or "") == "fallback"
        or str((block.get("anchor_after") or {}).get("anchor_kind") or "") == "fallback"
        or float(block.get("confidence", 0.0) or 0.0) < 0.65
    )
    if fallback_count:
        return f"{fallback_count} of {len(blocks)} patch blocks rely on coarse or low-confidence anchors (avg {average_confidence:.2f})."
    return f"{len(blocks)} patch blocks resolve with average confidence {average_confidence:.2f}."


def _patch_target_resolution_summary(structured_patch: dict[str, Any] | None) -> str | None:
    if not isinstance(structured_patch, dict):
        return None
    target_resolution = structured_patch.get("target_resolution") if isinstance(structured_patch.get("target_resolution"), dict) else {}
    mode = str(target_resolution.get("resolution_mode") or "fallback")
    confidence = float(target_resolution.get("resolution_confidence", 0.0) or 0.0)
    original_target = target_resolution.get("original_target_section_id") or "none"
    resolved_target = target_resolution.get("resolved_target_section_id") or "none"
    return f"Resolution {mode} ({confidence:.2f}) from {original_target} to {resolved_target}."


def _runtime_conflict_detail(
    *,
    candidate_id: str,
    conflict_kind: str,
    severity: str,
    title: str,
    message: str,
    structured_patch: dict[str, Any] | None,
    item: dict[str, Any],
    preview: dict[str, Any],
    related_candidate_ids: list[str] | None = None,
    suggested_recovery_mode: str = "none",
    suggested_manual_actions: list[str] | None = None,
    can_recover_by_rebase: bool = False,
    can_proceed_with_confirmation: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return outline_history.build_conflict_detail(
        conflict_id=f"{candidate_id}::{conflict_kind}::{severity}",
        candidate_id=candidate_id,
        conflict_kind=conflict_kind,
        severity=severity,
        title=title,
        message=message,
        related_section_ids=_patch_related_section_ids(structured_patch, item, preview),
        related_candidate_ids=related_candidate_ids or [],
        related_paper_ids=_patch_related_paper_ids(structured_patch, item),
        anchor_summary=_patch_anchor_summary(structured_patch),
        target_resolution_summary=_patch_target_resolution_summary(structured_patch),
        suggested_recovery_mode=suggested_recovery_mode,
        suggested_manual_actions=suggested_manual_actions or [],
        can_recover_by_rebase=can_recover_by_rebase,
        can_proceed_with_confirmation=can_proceed_with_confirmation,
        warnings=warnings or [],
        provenance=(structured_patch or {}).get("provenance") if isinstance(structured_patch, dict) else item.get("provenance") or preview.get("provenance") or {},
    )


def _candidate_runtime_assessment(
    run_id: str,
    item: dict[str, Any],
    preview: dict[str, Any],
    *,
    editing_session_id: str | None = None,
    staleness: dict[str, Any] | None = None,
    duplicate_target_candidate_ids: list[str] | None = None,
    duplicate_target_section_id: str | None = None,
    confirm_destructive: bool = False,
) -> dict[str, Any]:
    candidate_id = str(item.get("candidate_id") or preview.get("action_id") or preview.get("candidate_id") or "")
    structured_patch = preview.get("structured_patch") if isinstance(preview.get("structured_patch"), dict) else _normalize_structured_patch(item.get("structured_patch"))
    duplicate_target_candidate_ids = [str(value) for value in (duplicate_target_candidate_ids or []) if str(value).strip()]
    candidate_warnings = [str(value) for value in preview.get("warnings", []) if str(value).strip()]
    candidate_warnings.extend(str(value) for value in preview.get("missing_artifacts", []) if str(value).strip())
    candidate_blockers: list[str] = []
    requires_confirmation = bool(item.get("requires_confirmation") or preview.get("requires_confirmation") or preview.get("effective_target", {}).get("destructive"))
    if requires_confirmation and not confirm_destructive:
        candidate_warnings.append("Explicit destructive confirmation is still required.")
    if _candidate_requires_target(item) and preview.get("conflicts"):
        candidate_blockers.extend(str(value) for value in preview.get("conflicts", []) if str(value).strip())
    else:
        candidate_warnings.extend(str(value) for value in preview.get("conflicts", []) if str(value).strip())

    stale_missing_target = set(staleness.get("missing_target_refs", [])) if staleness else set()
    stale_missing_source = set(staleness.get("missing_source_refs", [])) if staleness else set()
    stale_reorder = set(staleness.get("reorder_risk", [])) if staleness else set()
    can_recover_by_rebase = bool(candidate_id in stale_missing_target or candidate_id in stale_missing_source or candidate_id in stale_reorder)

    if candidate_id in stale_missing_target:
        candidate_blockers.append("The staged target section no longer resolves in the current outline.")
    if candidate_id in stale_missing_source:
        candidate_blockers.append("The staged compare/source section no longer resolves in the current artifacts.")
    if candidate_id in stale_reorder:
        candidate_warnings.append("The current outline order differs from the order used when this candidate was staged.")
    if duplicate_target_candidate_ids and len(duplicate_target_candidate_ids) > 1:
        candidate_blockers.append(
            f"Multiple queued candidates still mutate target section {duplicate_target_section_id or preview.get('effective_target', {}).get('target_section_id') or item.get('target_section_id') or 'unknown'}: {', '.join(duplicate_target_candidate_ids)}."
        )

    has_duplicate_conflict = bool(duplicate_target_candidate_ids and len(duplicate_target_candidate_ids) > 1)
    if has_duplicate_conflict or (candidate_blockers and not can_recover_by_rebase):
        status = "blocked"
    elif can_recover_by_rebase:
        status = "recoverable_by_rebase"
    elif requires_confirmation and not confirm_destructive:
        status = "destructive_confirmation_required"
    elif candidate_warnings:
        status = "warn"
    else:
        status = "safe"

    manual_actions: list[str] = []
    if can_recover_by_rebase:
        manual_actions.append("Preview rebase before apply-all.")
    if requires_confirmation:
        manual_actions.append("Confirm the destructive replace explicitly before apply.")
    if duplicate_target_candidate_ids and len(duplicate_target_candidate_ids) > 1:
        manual_actions.append("Apply or discard one of the duplicate target mutations.")
    if preview.get("missing_artifacts"):
        manual_actions.append("Re-resolve the compare source or discard the candidate.")
    if candidate_blockers:
        manual_actions.append("Resolve blockers before apply-all.")

    additional_conflicts: list[dict[str, Any]] = []
    for message in [str(value) for value in preview.get("missing_artifacts", []) if str(value).strip()]:
        lowered = message.lower()
        if "compare run" in lowered or "artifact" in lowered:
            additional_conflicts.append(
                _runtime_conflict_detail(
                    candidate_id=candidate_id,
                    conflict_kind="source_artifact_missing",
                    severity="blocker",
                    title="Source artifacts missing",
                    message=message,
                    structured_patch=structured_patch,
                    item=item,
                    preview=preview,
                    suggested_recovery_mode="discard_staged_candidates" if editing_session_id else "none",
                    suggested_manual_actions=["Restore the missing source artifacts or discard this candidate."],
                    warnings=[message],
                )
            )
        else:
            additional_conflicts.append(
                _runtime_conflict_detail(
                    candidate_id=candidate_id,
                    conflict_kind="missing_source_section",
                    severity="blocker" if candidate_id in stale_missing_source else "warn",
                    title="Missing source section",
                    message=message,
                    structured_patch=structured_patch,
                    item=item,
                    preview=preview,
                    suggested_recovery_mode="rebase_staged_candidates" if editing_session_id else "none",
                    suggested_manual_actions=["Re-resolve the compare source section before applying this candidate."],
                    can_recover_by_rebase=bool(editing_session_id),
                    warnings=[message],
                )
            )

    if staleness and staleness.get("outline_hash_changed"):
        additional_conflicts.append(
            _runtime_conflict_detail(
                candidate_id=candidate_id,
                conflict_kind="stale_outline_hash",
                severity="warn",
                title="Stale outline hash",
                message="The editing session outline hash no longer matches the current outline draft.",
                structured_patch=structured_patch,
                item=item,
                preview=preview,
                suggested_recovery_mode="refresh_session_metadata",
                suggested_manual_actions=["Refresh the saved session metadata or preview recovery before apply-all."],
            )
        )
    if candidate_id in stale_missing_target:
        additional_conflicts.append(
            _runtime_conflict_detail(
                candidate_id=candidate_id,
                conflict_kind="missing_target_section",
                severity="blocker",
                title="Missing target section",
                message="The staged target section no longer resolves in the current outline.",
                structured_patch=structured_patch,
                item=item,
                preview=preview,
                suggested_recovery_mode="rebase_staged_candidates",
                suggested_manual_actions=["Preview rebase to retarget this candidate against the current outline."],
                can_recover_by_rebase=True,
            )
        )
    if candidate_id in stale_missing_source:
        additional_conflicts.append(
            _runtime_conflict_detail(
                candidate_id=candidate_id,
                conflict_kind="missing_source_section",
                severity="blocker",
                title="Missing source section",
                message="The staged compare/source section no longer resolves in the current artifacts.",
                structured_patch=structured_patch,
                item=item,
                preview=preview,
                suggested_recovery_mode="rebase_staged_candidates",
                suggested_manual_actions=["Preview rebase to recover or drop this candidate."],
                can_recover_by_rebase=True,
            )
        )
    if candidate_id in stale_reorder:
        additional_conflicts.append(
            _runtime_conflict_detail(
                candidate_id=candidate_id,
                conflict_kind="target_order_instability",
                severity="recoverable",
                title="Target order instability",
                message="The current outline order differs from the order used when this candidate was staged.",
                structured_patch=structured_patch,
                item=item,
                preview=preview,
                suggested_recovery_mode="rebase_staged_candidates",
                suggested_manual_actions=["Preview rebase to recompute positional target mapping."],
                can_recover_by_rebase=True,
            )
        )
    if duplicate_target_candidate_ids and len(duplicate_target_candidate_ids) > 1:
        additional_conflicts.append(
            _runtime_conflict_detail(
                candidate_id=candidate_id,
                conflict_kind="duplicate_target_mutation",
                severity="blocker",
                title="Duplicate target mutation",
                message=f"Multiple queued candidates mutate the same target section {duplicate_target_section_id or preview.get('effective_target', {}).get('target_section_id') or item.get('target_section_id') or 'unknown'}.",
                structured_patch=structured_patch,
                item=item,
                preview=preview,
                related_candidate_ids=duplicate_target_candidate_ids,
                suggested_manual_actions=["Apply, rebase, or discard one of the competing target mutations."],
            )
        )
    if can_recover_by_rebase:
        additional_conflicts.append(
            _runtime_conflict_detail(
                candidate_id=candidate_id,
                conflict_kind="recoverable_by_rebase",
                severity="recoverable",
                title="Recoverable by rebase",
                message="A recovery rebase can recompute this candidate against the current outline and compare artifacts.",
                structured_patch=structured_patch,
                item=item,
                preview=preview,
                suggested_recovery_mode="rebase_staged_candidates",
                suggested_manual_actions=["Open recovery preview to inspect the rebased before/after diff."],
                can_recover_by_rebase=True,
            )
        )

    enriched_patch = outline_history.enrich_structured_patch_conflicts(
        structured_patch,
        additional_conflicts=additional_conflicts,
        status=status,
        suggested_recovery_mode="rebase_staged_candidates" if can_recover_by_rebase else "refresh_session_metadata" if staleness and staleness.get("outline_hash_changed") else "none",
        suggested_manual_actions=manual_actions,
        can_recover_by_rebase=can_recover_by_rebase,
        can_proceed_with_confirmation=requires_confirmation,
    )
    return {
        "candidate_id": candidate_id,
        "warnings": list(dict.fromkeys([value for value in candidate_warnings if value])),
        "blockers": list(dict.fromkeys([value for value in candidate_blockers if value])),
        "status": status,
        "structured_patch": enriched_patch,
        "conflict_details": list((enriched_patch or {}).get("conflict_details", [])),
        "conflict_summary": dict((enriched_patch or {}).get("candidate_conflict_summary", {})),
        "can_recover_by_rebase": can_recover_by_rebase,
        "requires_confirmation": requires_confirmation,
    }


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys([value for value in values if value]))


def _copy_staleness(staleness: dict[str, Any] | None) -> dict[str, Any] | None:
    if not staleness:
        return None
    return {
        "outline_hash_changed": bool(staleness.get("outline_hash_changed", False)),
        "missing_target_refs": list(staleness.get("missing_target_refs", [])),
        "missing_source_refs": list(staleness.get("missing_source_refs", [])),
        "reorder_risk": list(staleness.get("reorder_risk", [])),
        "deleted_staged_dependencies": list(staleness.get("deleted_staged_dependencies", [])),
        "stale_reasons": list(staleness.get("stale_reasons", [])),
        "recommended_actions": list(staleness.get("recommended_actions", [])),
        "warnings": list(staleness.get("warnings", [])),
    }


def _recommended_recovery_payload(staleness: dict[str, Any] | None, candidate_order: list[str]) -> tuple[str, dict[str, Any]]:
    recommended_recovery_mode = "none"
    if staleness:
        recommended_recovery_mode = (
            "rebase_staged_candidates"
            if staleness.get("missing_target_refs") or staleness.get("missing_source_refs") or staleness.get("reorder_risk")
            else "refresh_session_metadata"
            if staleness.get("outline_hash_changed")
            else "discard_staged_candidates"
            if staleness.get("deleted_staged_dependencies")
            else "none"
        )
    candidate_ids = (
        _dedupe(list(staleness.get("missing_target_refs", [])) + list(staleness.get("missing_source_refs", [])) + list(staleness.get("reorder_risk", [])))
        if staleness and recommended_recovery_mode == "rebase_staged_candidates"
        else candidate_order
        if recommended_recovery_mode == "refresh_session_metadata"
        else _dedupe(list(staleness.get("deleted_staged_dependencies", [])))
        if staleness and recommended_recovery_mode == "discard_staged_candidates"
        else []
    )
    summary = {
        "rebase_staged_candidates": "Rebase staged candidates against the current outline draft before apply-all.",
        "refresh_session_metadata": "Refresh the saved outline hash before apply-all.",
        "discard_staged_candidates": "Discard stale queued candidates that can no longer be trusted.",
        "none": "No recovery is required.",
    }[recommended_recovery_mode]
    return recommended_recovery_mode, {
        "mode": recommended_recovery_mode,
        "summary": summary,
        "candidate_ids": candidate_ids,
        "warnings": list(staleness.get("warnings", [])) if staleness else [],
    }


def _build_preflight_payload(
    run_id: str,
    *,
    editing_session_id: str | None,
    selected_items: list[dict[str, Any]],
    session_outline_hash: str | None,
    current_outline_hash: str | None,
    staleness: dict[str, Any] | None,
    confirm_destructive_action_ids: list[str] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    blockers: list[str] = []
    destructive_candidates: list[str] = []
    confirmation_required_ids: list[str] = []
    affected_section_ids: list[str] = []
    items: list[dict[str, Any]] = []
    confirm_set = set(confirm_destructive_action_ids or [])
    grouped_warning_seeds: list[dict[str, Any]] = []
    grouped_blocker_seeds: list[dict[str, Any]] = []
    queue_conflict_groups: list[dict[str, Any]] = []
    duplicate_target_map: dict[str, list[str]] = {}
    preview_entries: list[dict[str, Any]] = []

    stale_missing_target = set(staleness.get("missing_target_refs", [])) if staleness else set()
    stale_missing_source = set(staleness.get("missing_source_refs", [])) if staleness else set()
    stale_reorder = set(staleness.get("reorder_risk", [])) if staleness else set()
    rebaseable_ids = stale_missing_target | stale_missing_source | stale_reorder

    if editing_session_id and staleness:
        warnings.extend(staleness.get("warnings", []))
        if staleness.get("outline_hash_changed"):
            warnings.append("Editing session outline hash differs from the current outline draft.")
            grouped_warning_seeds.append(
                {
                    "group_key": "outline_hash_changed",
                    "label": "Outline hash changed",
                    "severity": "warning",
                    "reason": "The saved editing session hash does not match the current outline draft.",
                    "candidate_ids": [str(item.get("candidate_id")) for item in selected_items if item.get("candidate_id")],
                    "messages": ["Run refresh_session_metadata or preview recovery before apply-all."],
                }
            )
        if stale_missing_target:
            grouped_blocker_seeds.append(
                {
                    "group_key": "missing_target_refs",
                    "label": "Missing target references",
                    "severity": "blocker",
                    "reason": "Some staged candidates no longer resolve to a target section.",
                    "candidate_ids": sorted(stale_missing_target),
                    "messages": ["Preview rebase to retarget these candidates against the current outline."],
                }
            )
        if stale_missing_source:
            grouped_blocker_seeds.append(
                {
                    "group_key": "missing_source_refs",
                    "label": "Missing source references",
                    "severity": "blocker",
                    "reason": "Some staged candidates no longer resolve to their compare/source section.",
                    "candidate_ids": sorted(stale_missing_source),
                    "messages": ["Preview rebase or discard candidates whose source artifacts are gone."],
                }
            )
        if stale_reorder:
            grouped_warning_seeds.append(
                {
                    "group_key": "reorder_risk",
                    "label": "Target reorder risk",
                    "severity": "warning",
                    "reason": "Current section order differs from the order used when the candidates were staged.",
                    "candidate_ids": sorted(stale_reorder),
                    "messages": ["Preview rebase if these candidates depend on positional targeting."],
                }
            )
        if staleness.get("deleted_staged_dependencies"):
            dropped_conflict = outline_history.build_conflict_detail(
                conflict_id=f"{editing_session_id or run_id}::dropped_dependency",
                conflict_kind="dropped_dependency",
                severity="warn",
                title="Dropped queue dependency",
                message="Saved pending merge order still references staged candidates that no longer exist.",
                related_candidate_ids=list(staleness.get("deleted_staged_dependencies", [])),
                suggested_recovery_mode="discard_staged_candidates",
                suggested_manual_actions=["Discard or rebuild the stale queue before apply-all."],
            )
            grouped_warning_seeds.append(
                {
                    "group_key": "dropped_dependency",
                    "label": "Dropped queue dependency",
                    "severity": "warning",
                    "reason": "The saved queue order references candidates that no longer exist.",
                    "candidate_ids": [],
                    "messages": ["Discard or rebuild the stale queue before apply-all."],
                    "conflicts": [dropped_conflict],
                }
            )

    for item in selected_items:
        candidate_id = str(item.get("candidate_id"))
        preview = preview_workspace_action(run_id, _candidate_preview_request(item, editing_session_id=editing_session_id))
        target_section_id = preview.get("effective_target", {}).get("target_section_id") or item.get("target_section_id")
        if target_section_id:
            affected_section_ids.append(str(target_section_id))
            if _candidate_requires_target(item):
                duplicate_target_map.setdefault(str(target_section_id), []).append(candidate_id)
        preview_entries.append(
            {
                "candidate_id": candidate_id,
                "item": item,
                "preview": preview,
                "target_section_id": target_section_id,
            }
        )

    duplicate_candidate_ids: set[str] = set()
    for section_id, candidate_ids in duplicate_target_map.items():
        if len(candidate_ids) <= 1:
            continue
        duplicate_candidate_ids.update(candidate_ids)
        message = f"Multiple staged candidates target section {section_id} for mutation: {', '.join(candidate_ids)}."
        blockers.append(message)
        queue_conflict_groups.append(
            {
                "conflict_type": "duplicate_target_mutation",
                "target_section_id": section_id,
                "candidate_ids": candidate_ids,
                "message": message,
            }
        )
        grouped_blocker_seeds.append(
            {
                "group_key": f"duplicate-target-{section_id}",
                "label": "Duplicate target mutation",
                "severity": "blocker",
                "reason": f"More than one queued candidate mutates target section {section_id}.",
                "candidate_ids": candidate_ids,
                "messages": [message],
            }
        )

    safe_candidate_ids: list[str] = []
    warn_candidate_ids: list[str] = []
    blocked_candidate_ids: list[str] = []
    for entry in preview_entries:
        candidate_id = entry["candidate_id"]
        item = entry["item"]
        preview = entry["preview"]
        duplicate_ids = duplicate_target_map.get(str(entry.get("target_section_id")), [])
        assessment = _candidate_runtime_assessment(
            run_id,
            item,
            preview,
            editing_session_id=editing_session_id,
            staleness=staleness,
            duplicate_target_candidate_ids=duplicate_ids,
            duplicate_target_section_id=str(entry.get("target_section_id") or "") or None,
            confirm_destructive=candidate_id in confirm_set,
        )
        if bool(item.get("destructive") or preview.get("destructive")):
            destructive_candidates.append(candidate_id)
        if assessment["requires_confirmation"] and candidate_id not in confirm_set:
            confirmation_required_ids.append(candidate_id)
        if assessment["status"] == "safe":
            safe_candidate_ids.append(candidate_id)
        elif assessment["status"] == "warn":
            warn_candidate_ids.append(candidate_id)
        elif assessment["status"] == "blocked":
            blocked_candidate_ids.append(candidate_id)

        items.append(
            {
                "candidate_id": candidate_id,
                "merge_intent": item.get("merge_intent", "create_candidate_section"),
                "target_section_id": entry.get("target_section_id"),
                "target_section_title": preview.get("effective_target", {}).get("target_section_title") or item.get("target_section_title"),
                "target_index": preview.get("effective_target", {}).get("target_index"),
                "warnings": assessment["warnings"],
                "blockers": assessment["blockers"],
                "destructive": bool(item.get("destructive") or preview.get("destructive")),
                "requires_confirmation": assessment["requires_confirmation"],
                "changed_block_count": int(preview.get("changed_block_count", item.get("changed_block_count", 0)) or 0),
                "status": assessment["status"],
                "structured_patch": assessment["structured_patch"],
                "conflict_summary": assessment["conflict_summary"],
            }
        )
        warnings.extend(assessment["warnings"])
        blockers.extend(assessment["blockers"])

    if confirmation_required_ids:
        grouped_warning_seeds.append(
            {
                "group_key": "destructive_confirmation",
                "label": "Destructive confirmation required",
                "severity": "warning",
                "reason": "Some queued candidates would overwrite or replace existing draft content.",
                "candidate_ids": _dedupe(confirmation_required_ids),
                "messages": ["Confirm destructive candidates explicitly before apply-all."],
            }
        )
        queue_conflict_groups.append(
            {
                "conflict_type": "destructive_confirmation",
                "target_section_id": None,
                "candidate_ids": _dedupe(confirmation_required_ids),
                "message": "Destructive candidates still require confirmation.",
            }
        )

    if any(item["warnings"] for item in items):
        grouped_warning_seeds.append(
            {
                "group_key": "candidate_warnings",
                "label": "Candidate warnings",
                "severity": "warning",
                "reason": "One or more queued candidates contain non-blocking warnings.",
                "candidate_ids": [item["candidate_id"] for item in items if item["warnings"]],
                "messages": _dedupe([warning for item in items for warning in item["warnings"]]),
            }
        )
    if any(item["blockers"] for item in items):
        grouped_blocker_seeds.append(
            {
                "group_key": "candidate_blockers",
                "label": "Candidate blockers",
                "severity": "blocker",
                "reason": "One or more queued candidates cannot be safely applied in their current form.",
                "candidate_ids": [item["candidate_id"] for item in items if item["blockers"]],
                "messages": _dedupe([blocker for item in items for blocker in item["blockers"]]),
            }
        )

    def _materialize_preflight_group(seed: dict[str, Any]) -> dict[str, Any]:
        candidate_ids = [str(value) for value in seed.get("candidate_ids", []) if str(value).strip()]
        grouped_items = [candidate for candidate in items if candidate.get("candidate_id") in set(candidate_ids)] if candidate_ids else []
        conflicts = list(seed.get("conflicts", []))
        conflicts.extend(
            conflict
            for candidate in grouped_items
            for conflict in ((candidate.get("structured_patch") or {}).get("conflict_details", []))
            if isinstance(conflict, dict)
        )
        candidate_summaries = [candidate.get("conflict_summary", {}) for candidate in grouped_items]
        affected_sections = _dedupe(
            [
                section_id
                for candidate in grouped_items
                for section_id in candidate.get("conflict_summary", {}).get("affected_section_ids", [])
            ]
        )
        related_papers = _dedupe(
            [
                paper_id
                for candidate in grouped_items
                for paper_id in candidate.get("conflict_summary", {}).get("related_paper_ids", [])
            ]
        )
        suggested_modes = [
            candidate.get("conflict_summary", {}).get("suggested_recovery_mode")
            for candidate in grouped_items
            if candidate.get("conflict_summary", {}).get("suggested_recovery_mode")
            and candidate.get("conflict_summary", {}).get("suggested_recovery_mode") != "none"
        ]
        suggested_manual_actions = _dedupe(
            list(seed.get("suggested_manual_actions", []))
            + [
                action
                for candidate in grouped_items
                for action in candidate.get("conflict_summary", {}).get("suggested_manual_actions", [])
            ]
        )
        return {
            **seed,
            "candidate_summaries": candidate_summaries,
            "conflicts": outline_history._dedupe_conflict_details(conflicts),
            "affected_section_ids": affected_sections,
            "related_paper_ids": related_papers,
            "suggested_recovery_mode": suggested_modes[0] if suggested_modes else seed.get("suggested_recovery_mode", "none"),
            "suggested_manual_actions": suggested_manual_actions,
        }

    warnings = _dedupe(warnings)
    blockers = _dedupe(blockers)
    affected_section_ids = _dedupe(affected_section_ids)
    candidate_order = [str(item.get("candidate_id")) for item in selected_items if item.get("candidate_id")]
    if blocked_candidate_ids or blockers:
        status = "blocked"
    elif warnings or confirmation_required_ids or rebaseable_ids:
        status = "ready_with_warnings"
    else:
        status = "ready"

    recommended_recovery_mode, recommended_recovery_plan = _recommended_recovery_payload(staleness, candidate_order)
    return {
        "run_id": run_id,
        "editing_session_id": editing_session_id,
        "current_outline_hash": current_outline_hash,
        "session_outline_hash": session_outline_hash,
        "is_stale": bool(staleness and staleness.get("stale_reasons")),
        "status": status,
        "candidate_order": candidate_order,
        "affected_section_ids": affected_section_ids,
        "warnings": warnings,
        "blockers": blockers,
        "destructive_candidates": _dedupe(destructive_candidates),
        "confirmation_required_ids": _dedupe(confirmation_required_ids),
        "safe_candidate_ids": _dedupe(safe_candidate_ids),
        "warn_candidate_ids": _dedupe(warn_candidate_ids),
        "blocked_candidate_ids": _dedupe(blocked_candidate_ids),
        "rebaseable_candidate_ids": sorted(rebaseable_ids),
        "grouped_warnings": [_materialize_preflight_group(group) for group in grouped_warning_seeds],
        "grouped_blockers": [_materialize_preflight_group(group) for group in grouped_blocker_seeds],
        "queue_conflict_groups": queue_conflict_groups,
        "can_apply_all": bool(selected_items) and not blockers and not confirmation_required_ids,
        "recommended_recovery_mode": recommended_recovery_mode,
        "recommended_recovery_plan": recommended_recovery_plan,
        "items": items,
    }


def _average_patch_confidence(structured_patch: dict[str, Any] | None) -> float:
    if not isinstance(structured_patch, dict):
        return 0.0
    blocks = [block for block in structured_patch.get("blocks", []) if isinstance(block, dict)]
    if not blocks:
        return 0.0
    return sum(float(block.get("confidence", 0.0) or 0.0) for block in blocks) / len(blocks)


def _block_signature(block: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(block.get("operation") or "context"),
        str(block.get("before_excerpt") or block.get("before_text") or "").strip(),
        str(block.get("after_excerpt") or block.get("after_text") or "").strip(),
    )


def _build_recovery_conflict_delta(
    candidate_id: str,
    *,
    original_status: str,
    projected_status: str,
    original_conflicts: list[dict[str, Any]],
    projected_conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    original_ids = {str(conflict.get("conflict_id")) for conflict in original_conflicts if str(conflict.get("conflict_id") or "").strip()}
    projected_ids = {str(conflict.get("conflict_id")) for conflict in projected_conflicts if str(conflict.get("conflict_id") or "").strip()}
    return {
        "candidate_id": candidate_id,
        "original_status": original_status,
        "projected_status": projected_status,
        "original_conflicts": original_conflicts,
        "projected_conflicts": projected_conflicts,
        "resolved_conflict_ids": sorted(original_ids - projected_ids),
        "introduced_conflict_ids": sorted(projected_ids - original_ids),
        "warnings": _dedupe(
            [
                str(warning).strip()
                for conflict in [*original_conflicts, *projected_conflicts]
                for warning in conflict.get("warnings", [])
                if str(warning).strip()
            ]
        ),
    }


def _build_rebase_diff(
    candidate_id: str,
    *,
    recovery_mode: str,
    original_patch: dict[str, Any] | None,
    projected_patch: dict[str, Any] | None,
    original_status: str,
    projected_status: str,
) -> dict[str, Any]:
    original_patch = original_patch if isinstance(original_patch, dict) else None
    projected_patch = projected_patch if isinstance(projected_patch, dict) else None
    original_blocks = [block for block in (original_patch or {}).get("blocks", []) if isinstance(block, dict)]
    projected_blocks = [block for block in (projected_patch or {}).get("blocks", []) if isinstance(block, dict)]
    original_signatures = {_block_signature(block) for block in original_blocks}
    projected_signatures = {_block_signature(block) for block in projected_blocks}
    changed_anchor_count = 0
    for original_block, projected_block in zip(original_blocks, projected_blocks):
        if (
            (original_block.get("anchor_before") or {}).get("anchor_kind") != (projected_block.get("anchor_before") or {}).get("anchor_kind")
            or (original_block.get("anchor_before") or {}).get("anchor_value") != (projected_block.get("anchor_before") or {}).get("anchor_value")
            or (original_block.get("anchor_after") or {}).get("anchor_kind") != (projected_block.get("anchor_after") or {}).get("anchor_kind")
            or (original_block.get("anchor_after") or {}).get("anchor_value") != (projected_block.get("anchor_after") or {}).get("anchor_value")
        ):
            changed_anchor_count += 1
    comparison_precision = "fine_grained"
    if not original_patch or not projected_patch:
        comparison_precision = "coarse"
    elif str((projected_patch or {}).get("completeness") or "coarse") in {"coarse", "metadata_only"} or str((original_patch or {}).get("completeness") or "coarse") in {"coarse", "metadata_only"}:
        comparison_precision = "section_level"
    return {
        "candidate_id": candidate_id,
        "recovery_mode": recovery_mode,
        "comparison_precision": comparison_precision,
        "original_target_resolution": (original_patch or {}).get("target_resolution")
        or {
            "original_target_section_id": None,
            "resolved_target_section_id": None,
            "resolution_mode": "missing",
            "resolution_confidence": 0.0,
            "retarget_warnings": [],
        },
        "projected_target_resolution": (projected_patch or {}).get("target_resolution")
        or {
            "original_target_section_id": None,
            "resolved_target_section_id": None,
            "resolution_mode": "missing",
            "resolution_confidence": 0.0,
            "retarget_warnings": ["Projected patch is unavailable for this recovery mode."],
        },
        "original_patch": original_patch,
        "projected_patch": projected_patch,
        "original_conflicts": list((original_patch or {}).get("conflict_details", [])),
        "projected_conflicts": list((projected_patch or {}).get("conflict_details", [])),
        "original_status": original_status,
        "projected_status": projected_status,
        "confidence_delta": round(_average_patch_confidence(projected_patch) - _average_patch_confidence(original_patch), 2),
        "warnings_before": list((original_patch or {}).get("warnings", [])),
        "warnings_after": list((projected_patch or {}).get("warnings", [])),
        "dropped_blocks": max(len(original_signatures - projected_signatures), 0),
        "added_blocks": max(len(projected_signatures - original_signatures), 0),
        "changed_anchor_count": changed_anchor_count,
        "affected_sections_before": _dedupe(_patch_related_section_ids(original_patch, {}, {"effective_target": {}})),
        "affected_sections_after": _dedupe(_patch_related_section_ids(projected_patch, {}, {"effective_target": {}})),
        "projected_can_apply": projected_status in {"safe", "warn"},
        "projected_requires_confirmation": bool((projected_patch or {}).get("requires_confirmation", False)),
        "warnings": _dedupe(
            list((original_patch or {}).get("warnings", []))
            + list((projected_patch or {}).get("warnings", []))
            + (["Projected patch is unavailable for this recovery mode."] if projected_patch is None else [])
        ),
    }


def _project_recovery(run_id: str, session_id: str, *, mode: str) -> dict[str, Any]:
    session = editing_sessions.get_editing_session(run_id, session_id)
    staged_payload = list_staged_workspace_actions(run_id, editing_session_id=session_id)
    staged_items = staged_payload.get("items", [])
    current_outline_hash = reporting.compute_outline_draft_hash(run_id=run_id)
    staleness = inspect_editing_session_staleness(run_id, session_id)
    candidate_results: list[dict[str, Any]] = []
    preserved_candidate_ids: list[str] = []
    rebased_candidate_ids: list[str] = []
    dropped_candidate_ids: list[str] = []
    projected_items: list[dict[str, Any]] = []
    warnings: list[str] = []
    blockers: list[str] = []

    if mode == "discard_staged_candidates":
        for item in staged_items:
            candidate_id = str(item.get("candidate_id"))
            original_patch = _normalize_structured_patch(item.get("structured_patch"))
            original_summary = dict((original_patch or {}).get("candidate_conflict_summary", {}))
            projected_patch = outline_history.enrich_structured_patch_conflicts(
                {
                    "candidate_id": candidate_id,
                    "merge_intent": item.get("merge_intent", "create_candidate_section"),
                    "source_run_id": item.get("source_run_id"),
                    "compare_partner_run_id": item.get("compare_partner_run_id"),
                    "source_section_id": item.get("source_section_id"),
                    "target_section_id": None,
                    "target_section_title": item.get("target_section_title"),
                    "patch_scope": "metadata",
                    "destructive": False,
                    "requires_confirmation": False,
                    "completeness": "metadata_only",
                    "provenance": item.get("provenance", {}),
                    "warnings": ["Discard recovery removes this candidate from the queue."],
                    "conflict_flags": [],
                    "target_resolution": {
                        "original_target_section_id": item.get("target_section_id"),
                        "resolved_target_section_id": None,
                        "resolution_mode": "missing",
                        "resolution_confidence": 0.0,
                        "retarget_warnings": ["The candidate would be removed from the queue by discard recovery."],
                    },
                    "blocks": [],
                    "created_at": iso_now_utc(),
                },
                additional_conflicts=[
                    outline_history.build_conflict_detail(
                        conflict_id=f"{candidate_id}::dropped_dependency::recoverable",
                        candidate_id=candidate_id,
                        conflict_kind="dropped_dependency",
                        severity="recoverable",
                        title="Candidate would be dropped",
                        message="Discard recovery removes this candidate from the queue.",
                        suggested_recovery_mode="discard_staged_candidates",
                        suggested_manual_actions=["Apply discard recovery to clear the stale queue."],
                    )
                ],
                status="dropped",
                suggested_recovery_mode="discard_staged_candidates",
                suggested_manual_actions=["Apply discard recovery to clear the stale queue."],
            )
            dropped_candidate_ids.append(candidate_id)
            projected_summary = dict((projected_patch or {}).get("candidate_conflict_summary", {}))
            original_conflicts = list((original_patch or {}).get("conflict_details", []))
            projected_conflicts = list((projected_patch or {}).get("conflict_details", []))
            candidate_results.append(
                {
                    "candidate_id": candidate_id,
                    "merge_intent": item.get("merge_intent", "create_candidate_section"),
                    "result": "dropped",
                    "original_target_section_id": item.get("target_section_id"),
                    "resolved_target_section_id": None,
                    "original_status": original_summary.get("status", "warn"),
                    "projected_status": "dropped",
                    "target_resolution": {
                        "original_target_section_id": item.get("target_section_id"),
                        "resolved_target_section_id": None,
                        "resolution_mode": "missing",
                        "resolution_confidence": 0.0,
                        "retarget_warnings": ["The candidate would be removed from the queue by discard recovery."],
                    },
                    "warnings": ["Discard recovery removes this candidate from the queue."],
                    "blockers": [],
                    "original_structured_patch": original_patch,
                    "structured_patch": projected_patch,
                    "original_conflict_summary": original_summary,
                    "projected_conflict_summary": projected_summary,
                    "recovery_conflict_delta": _build_recovery_conflict_delta(
                        candidate_id,
                        original_status=original_summary.get("status", "warn"),
                        projected_status="dropped",
                        original_conflicts=original_conflicts,
                        projected_conflicts=projected_conflicts,
                    ),
                    "rebase_diff": _build_rebase_diff(
                        candidate_id,
                        recovery_mode=mode,
                        original_patch=original_patch,
                        projected_patch=projected_patch,
                        original_status=original_summary.get("status", "warn"),
                        projected_status="dropped",
                    ),
                }
            )
        projected_preflight_summary = {
            "run_id": run_id,
            "editing_session_id": session_id,
            "current_outline_hash": current_outline_hash,
            "session_outline_hash": current_outline_hash,
            "is_stale": False,
            "status": "ready",
            "candidate_order": [],
            "affected_section_ids": [],
            "warnings": ["The queue would be empty after discard recovery."],
            "blockers": [],
            "destructive_candidates": [],
            "confirmation_required_ids": [],
            "safe_candidate_ids": [],
            "warn_candidate_ids": [],
            "blocked_candidate_ids": [],
            "rebaseable_candidate_ids": [],
            "grouped_warnings": [],
            "grouped_blockers": [],
            "queue_conflict_groups": [],
            "can_apply_all": False,
            "recommended_recovery_mode": "none",
            "recommended_recovery_plan": {"mode": "none", "summary": "No recovery is required after discard.", "candidate_ids": [], "warnings": []},
            "items": [],
        }
    else:
        projected_staleness = _copy_staleness(staleness) or {}
        projected_staleness["outline_hash_changed"] = False
        projected_staleness["stale_reasons"] = [
            reason for reason in projected_staleness.get("stale_reasons", []) if "hash" not in reason.lower()
        ]
        if mode == "refresh_session_metadata":
            projected_items = staged_items
            preserved_candidate_ids = [str(item.get("candidate_id")) for item in staged_items if item.get("candidate_id")]
            for item in staged_items:
                original_patch = _normalize_structured_patch(item.get("structured_patch"))
                original_summary = dict((original_patch or {}).get("candidate_conflict_summary", {}))
                candidate_results.append(
                    {
                        "candidate_id": str(item.get("candidate_id")),
                        "merge_intent": item.get("merge_intent", "create_candidate_section"),
                        "result": "preserved",
                        "original_target_section_id": item.get("target_section_id"),
                        "resolved_target_section_id": item.get("target_section_id"),
                        "original_status": original_summary.get("status", "warn"),
                        "projected_status": original_summary.get("status", "warn"),
                        "target_resolution": (item.get("structured_patch") or {}).get("target_resolution")
                        or {
                            "original_target_section_id": item.get("target_section_id"),
                            "resolved_target_section_id": item.get("target_section_id"),
                            "resolution_mode": "exact",
                            "resolution_confidence": 1.0,
                            "retarget_warnings": [],
                        },
                        "warnings": [],
                        "blockers": [],
                        "original_structured_patch": original_patch,
                        "structured_patch": original_patch,
                        "original_conflict_summary": original_summary,
                        "projected_conflict_summary": original_summary,
                        "recovery_conflict_delta": _build_recovery_conflict_delta(
                            str(item.get("candidate_id")),
                            original_status=original_summary.get("status", "warn"),
                            projected_status=original_summary.get("status", "warn"),
                            original_conflicts=list((original_patch or {}).get("conflict_details", [])),
                            projected_conflicts=list((original_patch or {}).get("conflict_details", [])),
                        ),
                        "rebase_diff": _build_rebase_diff(
                            str(item.get("candidate_id")),
                            recovery_mode=mode,
                            original_patch=original_patch,
                            projected_patch=original_patch,
                            original_status=original_summary.get("status", "warn"),
                            projected_status=original_summary.get("status", "warn"),
                        ),
                    }
                )
        elif mode == "rebase_staged_candidates":
            projected_staleness["missing_target_refs"] = []
            projected_staleness["missing_source_refs"] = []
            projected_staleness["reorder_risk"] = []
            projected_staleness["deleted_staged_dependencies"] = []
            projected_staleness["warnings"] = []
            for item in staged_items:
                candidate_id = str(item.get("candidate_id"))
                preview = preview_workspace_action(run_id, _candidate_preview_request(item, editing_session_id=session_id))
                rebased_item = _candidate_from_preview(run_id, item, preview, editing_session_id=session_id)
                original_patch = _normalize_structured_patch(item.get("structured_patch"))
                original_summary = dict((original_patch or {}).get("candidate_conflict_summary", {}))
                requires_target = _candidate_requires_target(item)
                unresolved = requires_target and (
                    preview.get("conflicts")
                    or not preview.get("effective_target", {}).get("target_section_id")
                )
                if unresolved:
                    dropped_candidate_ids.append(candidate_id)
                    warnings.append(f"{candidate_id}: candidate would be dropped because its target cannot be resolved during rebase.")
                    projected_patch = outline_history.enrich_structured_patch_conflicts(
                        preview.get("structured_patch"),
                        additional_conflicts=[
                            outline_history.build_conflict_detail(
                                conflict_id=f"{candidate_id}::recoverable_by_rebase::recoverable",
                                candidate_id=candidate_id,
                                conflict_kind="recoverable_by_rebase",
                                severity="recoverable",
                                title="Rebase preview still drops this candidate",
                                message="Rebase preview could not resolve a stable target for this candidate.",
                                suggested_recovery_mode="discard_staged_candidates",
                                suggested_manual_actions=["Discard the candidate or retarget it manually."],
                                can_recover_by_rebase=True,
                            )
                        ],
                        status="dropped",
                        suggested_recovery_mode="discard_staged_candidates",
                        suggested_manual_actions=["Discard the candidate or retarget it manually."],
                        can_recover_by_rebase=True,
                    )
                    projected_summary = dict((projected_patch or {}).get("candidate_conflict_summary", {}))
                    candidate_results.append(
                        {
                            "candidate_id": candidate_id,
                            "merge_intent": item.get("merge_intent", "create_candidate_section"),
                            "result": "dropped",
                            "original_target_section_id": item.get("target_section_id"),
                            "resolved_target_section_id": preview.get("effective_target", {}).get("target_section_id"),
                            "original_status": original_summary.get("status", "warn"),
                            "projected_status": "dropped",
                            "target_resolution": (preview.get("structured_patch") or {}).get("target_resolution")
                            or {
                                "original_target_section_id": item.get("target_section_id"),
                                "resolved_target_section_id": preview.get("effective_target", {}).get("target_section_id"),
                                "resolution_mode": "missing",
                                "resolution_confidence": 0.0,
                                "retarget_warnings": preview.get("conflicts", []),
                            },
                            "warnings": preview.get("warnings", []) + preview.get("missing_artifacts", []),
                            "blockers": preview.get("conflicts", []),
                            "original_structured_patch": original_patch,
                            "structured_patch": projected_patch,
                            "original_conflict_summary": original_summary,
                            "projected_conflict_summary": projected_summary,
                            "recovery_conflict_delta": _build_recovery_conflict_delta(
                                candidate_id,
                                original_status=original_summary.get("status", "warn"),
                                projected_status="dropped",
                                original_conflicts=list((original_patch or {}).get("conflict_details", [])),
                                projected_conflicts=list((projected_patch or {}).get("conflict_details", [])),
                            ),
                            "rebase_diff": _build_rebase_diff(
                                candidate_id,
                                recovery_mode=mode,
                                original_patch=original_patch,
                                projected_patch=projected_patch,
                                original_status=original_summary.get("status", "warn"),
                                projected_status="dropped",
                            ),
                        }
                    )
                    continue
                is_rebased = (
                    item.get("target_section_id") != rebased_item.get("target_section_id")
                    or item.get("target_index") != rebased_item.get("target_index")
                    or bool(item.get("warnings")) != bool(rebased_item.get("warnings"))
                )
                if is_rebased:
                    rebased_candidate_ids.append(candidate_id)
                else:
                    preserved_candidate_ids.append(candidate_id)
                projected_items.append(rebased_item)
                projected_patch = _normalize_structured_patch(rebased_item.get("structured_patch"))
                projected_summary = dict((projected_patch or {}).get("candidate_conflict_summary", {}))
                projected_status = projected_summary.get("status", "warn" if rebased_item.get("warnings") else "safe")
                candidate_results.append(
                    {
                        "candidate_id": candidate_id,
                        "merge_intent": item.get("merge_intent", "create_candidate_section"),
                        "result": "rebased" if is_rebased else "preserved",
                        "original_target_section_id": item.get("target_section_id"),
                        "resolved_target_section_id": rebased_item.get("target_section_id"),
                        "original_status": original_summary.get("status", "warn"),
                        "projected_status": projected_status,
                        "target_resolution": (rebased_item.get("structured_patch") or {}).get("target_resolution")
                        or {
                            "original_target_section_id": item.get("target_section_id"),
                            "resolved_target_section_id": rebased_item.get("target_section_id"),
                            "resolution_mode": "fallback",
                            "resolution_confidence": 0.5,
                            "retarget_warnings": [],
                        },
                        "warnings": rebased_item.get("warnings", []),
                        "blockers": rebased_item.get("conflict_flags", []),
                        "original_structured_patch": original_patch,
                        "structured_patch": projected_patch,
                        "original_conflict_summary": original_summary,
                        "projected_conflict_summary": projected_summary,
                        "recovery_conflict_delta": _build_recovery_conflict_delta(
                            candidate_id,
                            original_status=original_summary.get("status", "warn"),
                            projected_status=projected_status,
                            original_conflicts=list((original_patch or {}).get("conflict_details", [])),
                            projected_conflicts=list((projected_patch or {}).get("conflict_details", [])),
                        ),
                        "rebase_diff": _build_rebase_diff(
                            candidate_id,
                            recovery_mode=mode,
                            original_patch=original_patch,
                            projected_patch=projected_patch,
                            original_status=original_summary.get("status", "warn"),
                            projected_status=projected_status,
                        ),
                    }
                )
        else:
            raise RuntimeError(f"Unsupported editing session recovery mode: {mode}")

        projected_preflight_summary = _build_preflight_payload(
            run_id,
            editing_session_id=session_id,
            selected_items=projected_items,
            session_outline_hash=current_outline_hash,
            current_outline_hash=current_outline_hash,
            staleness=projected_staleness,
            confirm_destructive_action_ids=[],
        )

    return {
        "run_id": run_id,
        "session_id": session_id,
        "recovery_mode": mode,
        "outline_hash_changed": bool(staleness.get("outline_hash_changed")) if staleness else False,
        "candidate_results": candidate_results,
        "preserved_candidate_ids": _dedupe(preserved_candidate_ids),
        "rebased_candidate_ids": _dedupe(rebased_candidate_ids),
        "dropped_candidate_ids": _dedupe(dropped_candidate_ids),
        "warnings": _dedupe(warnings),
        "blockers": _dedupe(blockers),
        "projected_can_apply_all": bool(projected_preflight_summary.get("can_apply_all")),
        "projected_preflight_summary": projected_preflight_summary,
        "projected_items": projected_items,
        "current_outline_hash": current_outline_hash,
        "session": session,
        "staleness": staleness,
    }


def inspect_editing_session_staleness(run_id: str, session_id: str) -> dict[str, Any]:
    session = editing_sessions.get_editing_session(run_id, session_id)
    outline = _get_outline(run_id)
    current_outline_hash = reporting.compute_outline_draft_hash(outline=outline)
    outline_sections = outline.get("sections", [])
    outline_index_map = {str(section.get("section_id")): index for index, section in enumerate(outline_sections)}
    staged_payload = list_staged_workspace_actions(run_id, editing_session_id=session_id)
    staged_items = staged_payload.get("items", [])
    staged_ids = [str(item.get("candidate_id")) for item in staged_items if item.get("candidate_id")]
    pending_ids = [str(item) for item in session.get("pending_merge_order", []) if str(item).strip()]

    outline_hash_changed = bool(session.get("outline_draft_hash") and current_outline_hash and session.get("outline_draft_hash") != current_outline_hash)
    missing_target_refs: list[str] = []
    missing_source_refs: list[str] = []
    reorder_risk: list[str] = []
    deleted_staged_dependencies = [action_id for action_id in pending_ids if action_id not in staged_ids]
    warnings = list(session.get("warnings", []))

    for item in staged_items:
        candidate_id = str(item.get("candidate_id"))
        target_section_id = str(item.get("target_section_id") or item.get("request", {}).get("target_section_id") or "").strip()
        current_target_index = outline_index_map.get(target_section_id) if target_section_id else None
        stored_target_index = item.get("target_index")
        if stored_target_index is not None and current_target_index is not None and int(stored_target_index) != int(current_target_index):
            reorder_risk.append(candidate_id)
        try:
            preview = preview_workspace_action(
                run_id,
                {
                    **item.get("request", {}),
                    "action_id": candidate_id,
                    "candidate_id": candidate_id,
                    "editing_session_id": session_id,
                },
            )
        except RuntimeError as exc:
            preview = {
                "conflicts": [str(exc)],
                "missing_artifacts": [],
                "effective_target": {"target_section_id": target_section_id, "target_index": current_target_index},
            }

        conflicts = [str(value) for value in preview.get("conflicts", []) if str(value).strip()]
        missing = [str(value) for value in preview.get("missing_artifacts", []) if str(value).strip()]
        if missing:
            missing_source_refs.append(candidate_id)
            warnings.extend(f"{candidate_id}: {message}" for message in missing)
        if conflicts and item.get("merge_intent") in {"replace_target_section", "append_to_target_section", "attach_compare_note_only"}:
            missing_target_refs.append(candidate_id)
            warnings.extend(f"{candidate_id}: {message}" for message in conflicts)
        preview_target_index = preview.get("effective_target", {}).get("target_index")
        if stored_target_index is not None and preview_target_index is not None and int(stored_target_index) != int(preview_target_index):
            reorder_risk.append(candidate_id)

    stale_reasons: list[str] = []
    if outline_hash_changed:
        stale_reasons.append("Outline draft hash changed since this editing session was saved.")
    if missing_target_refs:
        stale_reasons.append("One or more staged candidates no longer resolve to their target section.")
    if missing_source_refs:
        stale_reasons.append("One or more staged candidates no longer resolve to their compare/source section.")
    if reorder_risk:
        stale_reasons.append("Section ordering changed relative to one or more staged candidate targets.")
    if deleted_staged_dependencies:
        stale_reasons.append("Saved pending merge order references staged candidates that no longer exist.")

    recommended_actions: list[str] = []
    if outline_hash_changed and "refresh_session_metadata" not in recommended_actions:
        recommended_actions.append("refresh_session_metadata")
    if (missing_target_refs or missing_source_refs or reorder_risk) and "rebase_staged_candidates" not in recommended_actions:
        recommended_actions.append("rebase_staged_candidates")
    if (staged_items or deleted_staged_dependencies) and "discard_staged_candidates" not in recommended_actions:
        recommended_actions.append("discard_staged_candidates")

    return {
        "run_id": run_id,
        "session_id": session_id,
        "outline_hash_changed": outline_hash_changed,
        "missing_target_refs": list(dict.fromkeys(missing_target_refs)),
        "missing_source_refs": list(dict.fromkeys(missing_source_refs)),
        "reorder_risk": list(dict.fromkeys(reorder_risk)),
        "deleted_staged_dependencies": deleted_staged_dependencies,
        "stale_reasons": stale_reasons,
        "recommended_actions": recommended_actions,
        "can_rebase": bool(staged_items),
        "can_refresh": True,
        "can_discard_queue": bool(staged_items or deleted_staged_dependencies),
        "warnings": list(dict.fromkeys([warning for warning in warnings if warning])),
    }


def preflight_apply_staged_workspace_actions(
    run_id: str,
    *,
    editing_session_id: str | None = None,
    action_ids: list[str] | None = None,
    apply_all: bool = False,
    confirm_destructive_action_ids: list[str] | None = None,
) -> dict[str, Any]:
    staged, selected_items = _selected_staged_items(run_id, editing_session_id=editing_session_id, action_ids=action_ids, apply_all=apply_all)
    session = editing_sessions.get_editing_session(run_id, editing_session_id) if editing_session_id else None
    staleness = inspect_editing_session_staleness(run_id, editing_session_id) if editing_session_id else None
    current_outline_hash = reporting.compute_outline_draft_hash(run_id=run_id)
    return _build_preflight_payload(
        run_id,
        editing_session_id=editing_session_id,
        selected_items=selected_items,
        session_outline_hash=session.get("outline_draft_hash") if session else None,
        current_outline_hash=current_outline_hash,
        staleness=staleness,
        confirm_destructive_action_ids=confirm_destructive_action_ids,
    )


def preview_editing_session_recovery(run_id: str, session_id: str, *, mode: str) -> dict[str, Any]:
    preview = _project_recovery(run_id, session_id, mode=mode)
    return {
        "run_id": run_id,
        "session_id": session_id,
        "recovery_mode": mode,
        "outline_hash_changed": bool(preview.get("outline_hash_changed", False)),
        "candidate_results": preview.get("candidate_results", []),
        "preserved_candidate_ids": preview.get("preserved_candidate_ids", []),
        "rebased_candidate_ids": preview.get("rebased_candidate_ids", []),
        "dropped_candidate_ids": preview.get("dropped_candidate_ids", []),
        "warnings": preview.get("warnings", []),
        "blockers": preview.get("blockers", []),
        "projected_can_apply_all": bool(preview.get("projected_can_apply_all", False)),
        "projected_preflight_summary": preview.get("projected_preflight_summary"),
    }


def recover_editing_session(run_id: str, session_id: str, *, mode: str) -> dict[str, Any]:
    projection = _project_recovery(run_id, session_id, mode=mode)
    preview = {
        "run_id": run_id,
        "session_id": session_id,
        "recovery_mode": mode,
        "outline_hash_changed": bool(projection.get("outline_hash_changed", False)),
        "candidate_results": projection.get("candidate_results", []),
        "preserved_candidate_ids": projection.get("preserved_candidate_ids", []),
        "rebased_candidate_ids": projection.get("rebased_candidate_ids", []),
        "dropped_candidate_ids": projection.get("dropped_candidate_ids", []),
        "warnings": projection.get("warnings", []),
        "blockers": projection.get("blockers", []),
        "projected_can_apply_all": bool(projection.get("projected_can_apply_all", False)),
        "projected_preflight_summary": projection.get("projected_preflight_summary"),
    }
    session = editing_sessions.get_editing_session(run_id, session_id)
    warnings: list[str] = list(preview.get("warnings", []))
    recovered_candidate_ids: list[str] = []
    discarded_candidate_ids: list[str] = []
    current_outline_hash = projection.get("current_outline_hash") or reporting.compute_outline_draft_hash(run_id=run_id)

    if mode == "refresh_session_metadata":
        session = editing_sessions.update_editing_session(run_id, session_id, {"outline_draft_hash": current_outline_hash})
        recovered_candidate_ids = list(preview.get("preserved_candidate_ids", []))
    elif mode == "discard_staged_candidates":
        discarded_candidate_ids = [str(item.get("candidate_id")) for item in list_staged_workspace_actions(run_id, editing_session_id=session_id).get("items", []) if item.get("candidate_id")]
        _delete_staged_action_ids(run_id, discarded_candidate_ids)
        session = editing_sessions.update_editing_session(
            run_id,
            session_id,
            {
                "outline_draft_hash": current_outline_hash,
                "staged_action_ids": [],
                "pending_merge_order": [],
            },
        )
    elif mode == "rebase_staged_candidates":
        all_items = [_normalize_candidate(run_id, item) for item in _read_staged_actions(run_id)]
        projected_map = {
            str(item.get("candidate_id")): item
            for item in projection.get("projected_items", [])
            if isinstance(item, dict) and item.get("candidate_id")
        }
        kept_ids = set(projected_map)
        discarded_candidate_ids = list(preview.get("dropped_candidate_ids", []))
        rebased: list[dict[str, Any]] = []
        for item in all_items:
            candidate_id = str(item.get("candidate_id"))
            if item.get("editing_session_id") != session_id and candidate_id not in session.get("staged_action_ids", []):
                rebased.append(item)
                continue
            if candidate_id in projected_map:
                rebased.append(projected_map[candidate_id])
                recovered_candidate_ids.append(candidate_id)
            elif candidate_id not in kept_ids:
                continue
        _write_staged_actions(run_id, rebased)
        session = editing_sessions.update_editing_session(
            run_id,
            session_id,
            {
                "outline_draft_hash": current_outline_hash,
                "staged_action_ids": recovered_candidate_ids,
                "pending_merge_order": [candidate_id for candidate_id in session.get("pending_merge_order", []) if candidate_id in recovered_candidate_ids],
            },
        )
    else:
        raise RuntimeError(f"Unsupported editing session recovery mode: {mode}")

    staleness = inspect_editing_session_staleness(run_id, session_id)
    staged_payload = list_staged_workspace_actions(run_id, editing_session_id=session_id)
    warnings.extend(staleness.get("warnings", []))
    return {
        "run_id": run_id,
        "session_id": session_id,
        "mode": mode,
        "recovered_candidate_ids": recovered_candidate_ids,
        "discarded_candidate_ids": discarded_candidate_ids,
        "warnings": list(dict.fromkeys([warning for warning in warnings if warning])),
        "session": editing_sessions.get_editing_session(run_id, session_id),
        "staged": staged_payload,
        "staleness": staleness,
    }


def apply_staged_workspace_actions(
    run_id: str,
    *,
    editing_session_id: str | None = None,
    action_ids: list[str] | None = None,
    apply_all: bool = False,
    confirm_destructive_action_ids: list[str] | None = None,
) -> dict[str, Any]:
    staged, items = _selected_staged_items(run_id, editing_session_id=editing_session_id, action_ids=action_ids, apply_all=apply_all)
    if not items:
        outline = _get_outline(run_id)
        preview = reporting.build_preview(run_id, outline=outline)
        return {
            "run_id": run_id,
            "editing_session_id": editing_session_id,
            "applied_count": 0,
            "applied_action_ids": [],
            "warnings": ["No staged merge candidates are available for this run."],
            "blockers": [],
            "status": "noop",
            "outline": outline,
            "preview": preview,
        }

    selected_items = items
    confirm_set = set(confirm_destructive_action_ids or [])
    applied_ids: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    outline = _get_outline(run_id)
    preview_payload = reporting.build_preview(run_id, outline=outline)
    preflight = preflight_apply_staged_workspace_actions(
        run_id,
        editing_session_id=editing_session_id,
        action_ids=action_ids,
        apply_all=apply_all,
        confirm_destructive_action_ids=confirm_destructive_action_ids,
    )
    warnings.extend(preflight.get("warnings", []))
    blockers.extend(preflight.get("blockers", []))
    if blockers:
        return {
            "run_id": run_id,
            "editing_session_id": editing_session_id,
            "applied_count": 0,
            "applied_action_ids": [],
            "warnings": list(dict.fromkeys(warnings)),
            "blockers": list(dict.fromkeys(blockers)),
            "status": "blocked",
            "outline": outline,
            "preview": preview_payload,
        }

    # --- Plan phase: fold every candidate in memory. Zero persistent writes. ---
    # The candidate batch is frozen here (selected_items), each candidate is
    # planned against the state the previous candidate would produce, and any
    # failure aborts the whole batch before the single commit point is reached.
    base_outline_hash = reporting.compute_outline_draft_hash(outline=outline)
    working_outline = outline
    plans: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()

    for item in selected_items:
        candidate_id = item.get("candidate_id")
        if candidate_id in seen_candidate_ids:
            raise RuntimeError(f"{candidate_id}: duplicate staged merge candidate in batch.")
        seen_candidate_ids.add(candidate_id)
        if item.get("requires_confirmation") and candidate_id not in confirm_set:
            # All-or-nothing: an unconfirmed destructive candidate fails the
            # whole batch instead of applying the preceding candidates.
            raise RuntimeError(f"{candidate_id}: destructive staged merge requires confirmation.")
        try:
            plan = _plan_workspace_action(
                run_id,
                {
                    **item.get("request", {}),
                    "action_id": candidate_id,
                    "candidate_id": candidate_id,
                    "editing_session_id": editing_session_id or item.get("editing_session_id"),
                    "confirm_destructive": candidate_id in confirm_set,
                },
                outline=working_outline,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"{candidate_id}: {exc}") from exc
        working_outline = plan["projected_outline"]
        plans.append(plan)
        warnings.extend(plan.get("warnings", []))

    if not plans:
        return {
            "run_id": run_id,
            "editing_session_id": editing_session_id,
            "applied_count": 0,
            "applied_action_ids": [],
            "warnings": list(dict.fromkeys(warnings)),
            "blockers": [],
            "status": "noop",
            "outline": outline,
            "preview": preview_payload,
        }

    # --- Commit phase: one atomic outline replace for the whole batch, with the
    # secondary finalize performed inside the same lock and journal window. ---
    applied_ids = [plan["candidate_id"] for plan in plans]
    if editing_session_id:
        session_ids = [editing_session_id]
    else:
        session_ids = [
            item.get("editing_session_id")
            for item in selected_items
            if item.get("candidate_id") in applied_ids and item.get("editing_session_id")
        ]
    commit = _commit_outline_transaction(
        run_id,
        base_outline_hash=base_outline_hash,
        sections=plans[-1]["sections"],
        applied_actions=plans[-1]["applied_actions"],
        history_entries=[plan["history_entry"] for plan in plans],
        kind=report_transaction.KIND_APPLY_ALL,
        candidate_ids=applied_ids,
        editing_session_ids=list(dict.fromkeys(session_ids)),
    )
    outline = commit["outline"]
    preview_payload = reporting.build_preview(run_id, outline=outline)
    warnings.extend(commit["post_commit_warnings"])

    return {
        "run_id": run_id,
        "editing_session_id": editing_session_id,
        "applied_count": len(applied_ids),
        "applied_action_ids": applied_ids,
        "warnings": list(dict.fromkeys(warnings)),
        "blockers": [],
        # Every planned candidate is committed together, so a successful apply is
        # never reported as a partial application.
        "status": "applied",
        "outline": outline,
        "preview": preview_payload,
    }


def list_workspace_suggestions(
    run_id: str,
    *,
    editing_session_id: str | None = None,
    compare_partner_run_id: str | None = None,
    workspace_view_id: str | None = None,
    compare_mode: str | None = None,
    compare_view: str | None = None,
    selection_bucket_id: str | None = None,
    preset_id: str | None = None,
    focus_theme: str | None = None,
    focus_section_id: str | None = None,
    focus_section_title: str | None = None,
    preferred_report_side: str | None = None,
    selection_source: str | None = None,
    selected_paper_ids: str | None = None,
) -> dict[str, Any]:
    context = compare_context.resolve_run_compare_context(
        run_id,
        editing_session_id=editing_session_id,
        workspace_view_id=workspace_view_id,
        compare_partner_run_id=compare_partner_run_id,
        compare_mode=compare_mode,
        compare_view=compare_view,
        selection_bucket_id=selection_bucket_id,
        preset_id=preset_id,
        focus_theme=focus_theme,
        focus_section_id=focus_section_id,
        focus_section_title=focus_section_title,
        preferred_report_side=preferred_report_side,
        selection_source=selection_source,
        selected_paper_ids=selected_paper_ids,
        seed_section=False,
    )

    suggestions: list[dict[str, Any]] = []
    warnings = list(context.get("warnings", []))
    compare_partner = context.get("compare_partner_run_id")
    compare_payload = compare.compare_runs(run_id, compare_partner) if compare_partner else None
    report_payload = report_diff.build_report_diff(run_id, compare_partner) if compare_partner else None

    resolved_focus_section_id = context.get("focus_section_id")
    resolved_focus_section_title = context.get("focus_section_title")
    resolved_focus_theme = context.get("focus_theme")

    if report_payload and not (resolved_focus_section_id or resolved_focus_section_title):
        first_changed_section = next(
            (
                item
                for item in report_payload.get("section_diffs", [])
                if item.get("status") in {"added", "removed", "changed"}
            ),
            None,
        )
        if first_changed_section:
            resolved_focus_section_id = first_changed_section.get("section_key")
            resolved_focus_section_title = first_changed_section.get("display_title")
            warnings.append("No focused report section was selected. Suggestions are seeded from the first changed section in the report diff.")

    if compare_payload and not resolved_focus_theme:
        top_theme_delta = next(
            (item for item in compare_payload.get("themes", []) if item.get("delta")),
            None,
        )
        if top_theme_delta:
            resolved_focus_theme = top_theme_delta.get("theme")
            warnings.append("No explicit theme focus was selected. Suggestions are seeded from the strongest theme delta.")

    if compare_partner and (resolved_focus_section_id or resolved_focus_section_title):
        for action_type, label in [
            ("insert_section_after", "Insert compare section after target"),
            ("replace_section_draft", "Replace target draft with compare section"),
            ("attach_compare_note", "Attach compare note to focused section"),
        ]:
            suggestions.append(
                {
                    "suggestion_id": f"{action_type}-{context.get('focus_section_id') or context.get('focus_section_title')}",
                    "suggestion_type": "report_delta",
                    "title": label,
                    "description": "Focused report section diff를 실제 workspace action으로 전환합니다.",
                    "action_seed": {
                    "action_type": action_type,
                    "source_run_id": run_id,
                    "compare_partner_run_id": compare_partner,
                    "source_section_id": resolved_focus_section_id,
                    "source_section_title": resolved_focus_section_title,
                    "target_section_id": resolved_focus_section_id,
                    "preferred_report_side": context.get("preferred_report_side", "both"),
                    "selection_source": context.get("selection_source"),
                    "payload": {},
                },
                "related_paper_ids": list(context.get("selected_paper_ids", [])),
                "focus_section_id": resolved_focus_section_id,
                "focus_section_title": resolved_focus_section_title,
                "warnings": [],
            }
        )

    if resolved_focus_theme:
        suggestions.append(
            {
                "suggestion_id": f"theme-{resolved_focus_theme}",
                "suggestion_type": "theme_delta",
                "title": f"Create theme section: {resolved_focus_theme}",
                "description": "Theme delta를 요약하는 candidate section을 만듭니다.",
                "action_seed": {
                    "action_type": "apply_theme_outline_suggestion",
                    "source_run_id": run_id,
                    "compare_partner_run_id": compare_partner,
                    "source_theme": resolved_focus_theme,
                    "source_paper_ids": list(context.get("selected_paper_ids", [])),
                    "selection_source": context.get("selection_source"),
                    "payload": {},
                },
                "related_paper_ids": list(context.get("selected_paper_ids", [])),
                "focus_theme": resolved_focus_theme,
                "warnings": [],
            }
        )

    selected_ids = list(context.get("selected_paper_ids", []))
    if not selected_ids:
        rerun_context = load_run_record(run_id).get("rerun_context", {}) or {}
        selected_ids = [paper_id for paper_id in rerun_context.get("selected_paper_ids", []) if paper_id]
        if selected_ids:
            warnings.append("Compare selection was restored from rerun_context because no explicit compare selection was active.")

    if selected_ids:
        suggestions.append(
            {
                "suggestion_id": "paper-seed-suggestion",
                "suggestion_type": "paper_seed" if context.get("compare_partner_run_id") else "rerun_context",
                "title": "Create papers section from selected papers",
                "description": "현재 compare/rerun context의 selected papers로 candidate papers section을 만듭니다.",
                "action_seed": {
                    "action_type": "apply_paper_seed_suggestion",
                    "source_run_id": run_id,
                    "compare_partner_run_id": compare_partner,
                    "source_bucket_id": context.get("selection_bucket_id"),
                    "source_theme": context.get("focus_theme"),
                    "source_paper_ids": selected_ids,
                    "selection_source": context.get("selection_source"),
                    "payload": {},
                },
                "related_paper_ids": selected_ids,
                "focus_theme": context.get("focus_theme"),
                "warnings": [],
            }
        )

    return {
        "run_id": run_id,
        "compare_partner_run_id": compare_partner,
        "items": suggestions,
        "warnings": warnings,
    }

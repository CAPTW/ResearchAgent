from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from app.repositories.files import ensure_dir, read_json, write_json
from app.repositories.runs import build_run_paths, load_run_record
from app.services import editing_sessions, reporting

ACTION_HISTORY_FILENAME = "outline_action_history.json"


def _history_path(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / ACTION_HISTORY_FILENAME


def _read_history(run_id: str) -> list[dict[str, Any]]:
    load_run_record(run_id)
    payload = read_json(_history_path(run_id), default=[]) or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _write_history(run_id: str, items: list[dict[str, Any]]) -> None:
    ensure_dir(_history_path(run_id).parent)
    write_json(_history_path(run_id), items)


def _body_text(section: dict[str, Any] | None) -> str:
    if not isinstance(section, dict):
        return ""
    body = str(section.get("body_markdown", "") or "").strip()
    if body:
        return body
    paper_ids = [str(paper_id).strip() for paper_id in section.get("paper_ids", []) if str(paper_id).strip()]
    if paper_ids:
        return "\n".join(f"- {paper_id}" for paper_id in paper_ids)
    return ""


def _excerpt(text: str | None, *, limit: int = 240) -> str | None:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return None
    return compact[:limit]


def _paragraphs(text: str) -> list[str]:
    stripped = str(text or "").strip()
    if not stripped:
        return []
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", stripped) if chunk.strip()]
    return chunks or [stripped]


def _citation_keys(section: dict[str, Any] | None) -> list[str]:
    if not isinstance(section, dict):
        return []
    keys: list[str] = [str(paper_id).strip() for paper_id in section.get("paper_ids", []) if str(paper_id).strip()]
    body = _body_text(section)
    url_matches = re.findall(r"https?://\S+", body)
    keys.extend(match.rstrip(").,]") for match in url_matches if match)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in keys:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _make_block(
    *,
    block_id: str,
    block_type: str,
    label: str,
    kind: str,
    before_text: str | None,
    after_text: str | None,
    anchor_kind: str,
    anchor_value: str | None,
    related_citation_keys: list[str],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    content = after_text or before_text or ""
    return {
        "block_id": block_id,
        "block_type": block_type,
        "label": label,
        "kind": kind,
        "content": content,
        "before_text": before_text,
        "after_text": after_text,
        "before_excerpt": _excerpt(before_text),
        "after_excerpt": _excerpt(after_text),
        "anchor_kind": anchor_kind,
        "anchor_value": anchor_value,
        "related_citation_keys": related_citation_keys,
        "warnings": list(warnings or []),
    }


def _related_paper_ids(before_section: dict[str, Any] | None, after_section: dict[str, Any] | None) -> list[str]:
    values: list[str] = []
    for section in [before_section, after_section]:
        if not isinstance(section, dict):
            continue
        values.extend(str(paper_id).strip() for paper_id in section.get("paper_ids", []) if str(paper_id).strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _severity_rank(value: str) -> int:
    return {
        "info": 0,
        "warn": 1,
        "recoverable": 2,
        "confirmation_required": 3,
        "blocker": 4,
    }.get(str(value or "info"), 0)


def _anchor_summary(blocks: list[dict[str, Any]]) -> str | None:
    if not blocks:
        return "No structured patch blocks are available."
    low_confidence = [
        block
        for block in blocks
        if float(block.get("confidence", 0.0) or 0.0) < 0.65
        or str((block.get("anchor_before") or {}).get("anchor_kind") or "") == "fallback"
        or str((block.get("anchor_after") or {}).get("anchor_kind") or "") == "fallback"
    ]
    average_confidence = sum(float(block.get("confidence", 0.0) or 0.0) for block in blocks) / max(len(blocks), 1)
    if low_confidence:
        return f"{len(low_confidence)} of {len(blocks)} patch blocks rely on fallback or low-confidence anchors (avg {average_confidence:.2f})."
    return f"{len(blocks)} patch blocks resolved with average anchor confidence {average_confidence:.2f}."


def _target_resolution_summary(target_resolution: dict[str, Any] | None) -> str:
    payload = target_resolution or {}
    mode = str(payload.get("resolution_mode") or "fallback")
    confidence = float(payload.get("resolution_confidence", 0.0) or 0.0)
    original_target = payload.get("original_target_section_id") or "none"
    resolved_target = payload.get("resolved_target_section_id") or "none"
    return f"Resolution {mode} ({confidence:.2f}) from {original_target} to {resolved_target}."


def build_conflict_detail(
    *,
    conflict_id: str,
    conflict_kind: str,
    severity: str,
    title: str,
    message: str,
    candidate_id: str | None = None,
    related_section_ids: list[str] | None = None,
    related_candidate_ids: list[str] | None = None,
    related_paper_ids: list[str] | None = None,
    anchor_summary: str | None = None,
    target_resolution_summary: str | None = None,
    suggested_recovery_mode: str = "none",
    suggested_manual_actions: list[str] | None = None,
    can_recover_by_rebase: bool = False,
    can_proceed_with_confirmation: bool = False,
    warnings: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "conflict_id": conflict_id,
        "candidate_id": candidate_id,
        "conflict_kind": conflict_kind,
        "severity": severity,
        "title": title,
        "message": message,
        "related_section_ids": _dedupe_strings(list(related_section_ids or [])),
        "related_candidate_ids": _dedupe_strings(list(related_candidate_ids or [])),
        "related_paper_ids": _dedupe_strings(list(related_paper_ids or [])),
        "anchor_summary": anchor_summary,
        "target_resolution_summary": target_resolution_summary,
        "suggested_recovery_mode": suggested_recovery_mode,
        "suggested_manual_actions": _dedupe_strings(list(suggested_manual_actions or [])),
        "can_recover_by_rebase": bool(can_recover_by_rebase),
        "can_proceed_with_confirmation": bool(can_proceed_with_confirmation),
        "warnings": _dedupe_strings(list(warnings or [])),
        "provenance": dict(provenance or {}),
    }


def _dedupe_conflict_details(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    ordered: list[dict[str, Any]] = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        key = (
            str(conflict.get("conflict_kind") or ""),
            str(conflict.get("severity") or ""),
            str(conflict.get("message") or ""),
            tuple(_dedupe_strings(conflict.get("related_section_ids", []))),
            tuple(_dedupe_strings(conflict.get("related_candidate_ids", []))),
        )
        if key in seen:
            continue
        seen.add(key)
        ordered.append(
            {
                **conflict,
                "related_section_ids": _dedupe_strings(conflict.get("related_section_ids", [])),
                "related_candidate_ids": _dedupe_strings(conflict.get("related_candidate_ids", [])),
                "related_paper_ids": _dedupe_strings(conflict.get("related_paper_ids", [])),
                "suggested_manual_actions": _dedupe_strings(conflict.get("suggested_manual_actions", [])),
                "warnings": _dedupe_strings(conflict.get("warnings", [])),
                "provenance": dict(conflict.get("provenance") or {}),
            }
        )
    return ordered


def _build_base_patch_conflicts(structured_patch: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_id = structured_patch.get("candidate_id")
    merge_intent = str(structured_patch.get("merge_intent") or "create_candidate_section")
    blocks = [block for block in structured_patch.get("blocks", []) if isinstance(block, dict)]
    target_resolution = structured_patch.get("target_resolution") if isinstance(structured_patch.get("target_resolution"), dict) else {}
    target_summary = _target_resolution_summary(target_resolution)
    anchor_summary = _anchor_summary(blocks)
    provenance = dict(structured_patch.get("provenance") or {})
    related_paper_ids = _dedupe_strings(
        [
            str(paper_id).strip()
            for block in blocks
            for paper_id in block.get("related_paper_ids", [])
            if str(paper_id).strip()
        ]
    )
    related_section_ids = _dedupe_strings(
        [
            str(target_resolution.get("resolved_target_section_id") or "").strip(),
            str(target_resolution.get("original_target_section_id") or "").strip(),
            str(structured_patch.get("source_section_id") or "").strip(),
            str(structured_patch.get("target_section_id") or "").strip(),
        ]
    )
    mode = str(target_resolution.get("resolution_mode") or "fallback")
    completeness = str(structured_patch.get("completeness") or "coarse")
    conflicts: list[dict[str, Any]] = []
    requires_target = merge_intent in {"replace_target_section", "append_to_target_section", "attach_compare_note_only"}

    if mode == "missing":
        conflicts.append(
            build_conflict_detail(
                conflict_id=f"{candidate_id or 'patch'}::missing_target_section",
                candidate_id=candidate_id,
                conflict_kind="missing_target_section",
                severity="blocker" if requires_target else "warn",
                title="Missing target section",
                message="The candidate could not resolve a stable target section from the current outline.",
                related_section_ids=related_section_ids,
                related_paper_ids=related_paper_ids,
                anchor_summary=anchor_summary,
                target_resolution_summary=target_summary,
                suggested_recovery_mode="rebase_staged_candidates" if requires_target else "none",
                suggested_manual_actions=[
                    "Inspect the current outline target and retarget the candidate explicitly.",
                    "Preview the candidate again before apply.",
                ],
                can_recover_by_rebase=requires_target,
                warnings=list(target_resolution.get("retarget_warnings", [])),
                provenance=provenance,
            )
        )
    elif mode in {"title_match", "fallback"}:
        conflicts.append(
            build_conflict_detail(
                conflict_id=f"{candidate_id or 'patch'}::ambiguous_target_match",
                candidate_id=candidate_id,
                conflict_kind="ambiguous_target_match",
                severity="warn",
                title="Ambiguous target match",
                message="The candidate target was resolved using a fallback match instead of an exact section id.",
                related_section_ids=related_section_ids,
                related_paper_ids=related_paper_ids,
                anchor_summary=anchor_summary,
                target_resolution_summary=target_summary,
                suggested_manual_actions=["Confirm that the resolved target section is still the intended destination."],
                warnings=list(target_resolution.get("retarget_warnings", [])),
                provenance=provenance,
            )
        )

    weak_anchor_blocks = [
        block
        for block in blocks
        if float(block.get("confidence", 0.0) or 0.0) < 0.65
        or str((block.get("anchor_before") or {}).get("anchor_kind") or "") == "fallback"
        or str((block.get("anchor_after") or {}).get("anchor_kind") or "") == "fallback"
    ]
    if weak_anchor_blocks or completeness in {"partial", "coarse", "metadata_only"}:
        warnings = _dedupe_strings(
            [
                str((block.get("anchor_before") or {}).get("fallback_reason") or "").strip()
                for block in weak_anchor_blocks
            ]
            + [
                str((block.get("anchor_after") or {}).get("fallback_reason") or "").strip()
                for block in weak_anchor_blocks
            ]
        )
        conflicts.append(
            build_conflict_detail(
                conflict_id=f"{candidate_id or 'patch'}::weak_anchor_match",
                candidate_id=candidate_id,
                conflict_kind="weak_anchor_match",
                severity="warn",
                title="Weak anchor match",
                message="At least one patch block depends on coarse or low-confidence anchor matching.",
                related_section_ids=related_section_ids,
                related_paper_ids=related_paper_ids,
                anchor_summary=anchor_summary,
                target_resolution_summary=target_summary,
                suggested_manual_actions=[
                    "Inspect the structured patch before applying.",
                    "Prefer a manual retarget or rebase when anchor confidence is weak.",
                ],
                warnings=warnings,
                provenance=provenance,
            )
        )

    if bool(structured_patch.get("destructive")) or bool(structured_patch.get("requires_confirmation")):
        conflicts.append(
            build_conflict_detail(
                conflict_id=f"{candidate_id or 'patch'}::destructive_replace_requires_confirmation",
                candidate_id=candidate_id,
                conflict_kind="destructive_replace_requires_confirmation",
                severity="confirmation_required",
                title="Destructive confirmation required",
                message="Applying this candidate would overwrite or replace existing draft content.",
                related_section_ids=related_section_ids,
                related_paper_ids=related_paper_ids,
                anchor_summary=anchor_summary,
                target_resolution_summary=target_summary,
                suggested_manual_actions=["Confirm the destructive replace explicitly before apply."],
                can_proceed_with_confirmation=True,
                provenance=provenance,
            )
        )

    has_reference_risk = (
        merge_intent in {"replace_target_section", "append_to_target_section"}
        and any(block.get("related_citation_keys") for block in blocks)
        and (weak_anchor_blocks or completeness != "full")
    )
    if has_reference_risk:
        conflicts.append(
            build_conflict_detail(
                conflict_id=f"{candidate_id or 'patch'}::citation_overlap_or_reference_risk",
                candidate_id=candidate_id,
                conflict_kind="citation_overlap_or_reference_risk",
                severity="warn",
                title="Citation overlap or reference risk",
                message="Reference-bearing blocks are being moved or replaced under coarse matching, so citations should be reviewed manually.",
                related_section_ids=related_section_ids,
                related_paper_ids=related_paper_ids,
                anchor_summary=anchor_summary,
                target_resolution_summary=target_summary,
                suggested_manual_actions=["Review reference and citation continuity in the affected section."],
                provenance=provenance,
            )
        )

    return conflicts


def build_candidate_conflict_summary(
    structured_patch: dict[str, Any],
    *,
    conflicts: list[dict[str, Any]] | None = None,
    status: str | None = None,
    suggested_recovery_mode: str | None = None,
    suggested_manual_actions: list[str] | None = None,
    can_recover_by_rebase: bool | None = None,
    can_proceed_with_confirmation: bool | None = None,
) -> dict[str, Any]:
    resolved_conflicts = _dedupe_conflict_details(list(conflicts or structured_patch.get("conflict_details", [])))
    severity_counts: dict[str, int] = {}
    highest_severity = "info"
    affected_section_ids = _dedupe_strings(
        [
            str(structured_patch.get("target_section_id") or "").strip(),
            str(structured_patch.get("source_section_id") or "").strip(),
            *[
                str(section_id).strip()
                for conflict in resolved_conflicts
                for section_id in conflict.get("related_section_ids", [])
                if str(section_id).strip()
            ],
        ]
    )
    related_paper_ids = _dedupe_strings(
        [
            str(paper_id).strip()
            for conflict in resolved_conflicts
            for paper_id in conflict.get("related_paper_ids", [])
            if str(paper_id).strip()
        ]
        + [
            str(paper_id).strip()
            for block in structured_patch.get("blocks", [])
            if isinstance(block, dict)
            for paper_id in block.get("related_paper_ids", [])
            if str(paper_id).strip()
        ]
    )
    for conflict in resolved_conflicts:
        severity = str(conflict.get("severity") or "info")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        if _severity_rank(severity) >= _severity_rank(highest_severity):
            highest_severity = severity

    computed_status = status
    if computed_status is None:
        if any(str(conflict.get("severity") or "") == "blocker" for conflict in resolved_conflicts):
            computed_status = "blocked"
        elif any(str(conflict.get("severity") or "") == "confirmation_required" for conflict in resolved_conflicts):
            computed_status = "destructive_confirmation_required"
        elif any(str(conflict.get("severity") or "") == "recoverable" for conflict in resolved_conflicts):
            computed_status = "recoverable_by_rebase"
        elif resolved_conflicts:
            computed_status = "warn"
        else:
            computed_status = "safe"

    recovery_mode = suggested_recovery_mode
    if recovery_mode is None or recovery_mode == "none":
        recovery_mode = next(
            (
                str(conflict.get("suggested_recovery_mode") or "none")
                for conflict in resolved_conflicts
                if str(conflict.get("suggested_recovery_mode") or "none") != "none"
            ),
            "none",
        )

    manual_actions = _dedupe_strings(
        list(suggested_manual_actions or [])
        + [
            str(action).strip()
            for conflict in resolved_conflicts
            for action in conflict.get("suggested_manual_actions", [])
            if str(action).strip()
        ]
    )
    can_recover = (
        bool(can_recover_by_rebase)
        or any(bool(conflict.get("can_recover_by_rebase", False)) for conflict in resolved_conflicts)
    )
    can_confirm = (
        bool(can_proceed_with_confirmation)
        or any(bool(conflict.get("can_proceed_with_confirmation", False)) for conflict in resolved_conflicts)
    )
    warnings = _dedupe_strings(
        list(structured_patch.get("warnings", []))
        + [
            str(warning).strip()
            for conflict in resolved_conflicts
            for warning in conflict.get("warnings", [])
            if str(warning).strip()
        ]
    )
    return {
        "candidate_id": structured_patch.get("candidate_id"),
        "status": computed_status,
        "total_conflicts": len(resolved_conflicts),
        "highest_severity": highest_severity,
        "severity_counts": severity_counts,
        "affected_section_ids": affected_section_ids,
        "related_paper_ids": related_paper_ids,
        "can_apply": computed_status in {"safe", "warn"},
        "can_recover_by_rebase": can_recover,
        "can_proceed_with_confirmation": can_confirm,
        "suggested_recovery_mode": recovery_mode,
        "suggested_manual_actions": manual_actions,
        "warnings": warnings,
    }


def enrich_structured_patch_conflicts(
    structured_patch: dict[str, Any] | None,
    *,
    additional_conflicts: list[dict[str, Any]] | None = None,
    status: str | None = None,
    suggested_recovery_mode: str | None = None,
    suggested_manual_actions: list[str] | None = None,
    can_recover_by_rebase: bool | None = None,
    can_proceed_with_confirmation: bool | None = None,
) -> dict[str, Any] | None:
    if not isinstance(structured_patch, dict):
        return None
    enriched = dict(structured_patch)
    base_conflicts = (
        _dedupe_conflict_details(enriched.get("conflict_details", []))
        if enriched.get("conflict_details")
        else _build_base_patch_conflicts(enriched)
    )
    merged_conflicts = _dedupe_conflict_details(base_conflicts + list(additional_conflicts or []))
    enriched["conflict_details"] = merged_conflicts
    enriched["candidate_conflict_summary"] = build_candidate_conflict_summary(
        enriched,
        conflicts=merged_conflicts,
        status=status,
        suggested_recovery_mode=suggested_recovery_mode,
        suggested_manual_actions=suggested_manual_actions,
        can_recover_by_rebase=can_recover_by_rebase,
        can_proceed_with_confirmation=can_proceed_with_confirmation,
    )
    return enriched


def _anchor_confidence(anchor_kind: str) -> float:
    return {
        "paragraph": 0.78,
        "section": 0.94,
        "note": 0.9,
        "summary": 0.55,
    }.get(anchor_kind, 0.42)


def _make_structured_anchor(
    *,
    anchor_kind: str | None,
    anchor_value: str | None,
    matched: bool = True,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    normalized_kind = {
        "paragraph": "paragraph_index",
        "section": "section_boundary",
        "note": "note_slot",
        "summary": "fallback",
    }.get(str(anchor_kind or "fallback"), "fallback")
    confidence = _anchor_confidence(str(anchor_kind or "fallback"))
    if normalized_kind == "fallback" and fallback_reason is None:
        fallback_reason = "Only coarse section-level anchoring is available for this patch block."
    return {
        "anchor_kind": normalized_kind,
        "anchor_value": anchor_value,
        "matched": matched and bool(anchor_value),
        "confidence": confidence if anchor_value else max(confidence - 0.2, 0.1),
        "fallback_reason": fallback_reason,
    }


def build_structured_patch(
    *,
    merge_intent: str,
    preview_blocks: list[dict[str, Any]],
    completeness: str,
    target_section: dict[str, Any] | None,
    proposed_section: dict[str, Any] | None,
    proposed_compare_note: str | None = None,
    warnings: list[str] | None = None,
    conflict_flags: list[str] | None = None,
    candidate_id: str | None = None,
    source_run_id: str | None = None,
    compare_partner_run_id: str | None = None,
    source_section_id: str | None = None,
    source_section_title: str | None = None,
    target_section_id: str | None = None,
    target_section_title: str | None = None,
    destructive: bool = False,
    requires_confirmation: bool = False,
    provenance: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    related_paper_ids = _related_paper_ids(target_section, proposed_section)
    blocks: list[dict[str, Any]] = []
    for preview_block in preview_blocks:
        block_type = str(preview_block.get("block_type") or "context")
        operation = {
            "added": "note" if merge_intent == "attach_compare_note_only" else "add",
            "removed": "remove",
            "replaced": "append" if merge_intent == "append_to_target_section" else "replace",
            "unchanged": "context",
            "context": "context",
        }.get(block_type, "context")
        fallback_reason = None
        if preview_block.get("anchor_kind") == "summary":
            fallback_reason = "Preview relies on summary-level matching rather than stable paragraph anchors."
        blocks.append(
            {
                "block_id": str(preview_block.get("block_id") or f"{candidate_id or 'patch'}-{len(blocks) + 1}"),
                "operation": operation,
                "anchor_before": _make_structured_anchor(
                    anchor_kind=preview_block.get("anchor_kind"),
                    anchor_value=preview_block.get("anchor_value"),
                    matched=block_type != "added",
                    fallback_reason=fallback_reason,
                ),
                "anchor_after": _make_structured_anchor(
                    anchor_kind=preview_block.get("anchor_kind"),
                    anchor_value=preview_block.get("anchor_value"),
                    matched=block_type != "removed",
                    fallback_reason=fallback_reason,
                ),
                "before_text": preview_block.get("before_text"),
                "after_text": preview_block.get("after_text"),
                "before_excerpt": preview_block.get("before_excerpt"),
                "after_excerpt": preview_block.get("after_excerpt"),
                "related_citation_keys": list(preview_block.get("related_citation_keys", [])),
                "related_paper_ids": related_paper_ids,
                "warnings": list(preview_block.get("warnings", [])),
                "confidence": _anchor_confidence(str(preview_block.get("anchor_kind") or "fallback")),
            }
        )

    original_target_id = target_section_id or (target_section or {}).get("section_id")
    resolved_target_id = (target_section or {}).get("section_id") or target_section_id or (proposed_section or {}).get("section_id")
    average_confidence = (
        sum(float(block.get("confidence", 0.0) or 0.0) for block in blocks) / max(len(blocks), 1)
        if blocks
        else 0.0
    )
    resolution_mode = "exact"
    resolution_warnings: list[str] = []
    if original_target_id and resolved_target_id and original_target_id != resolved_target_id:
        resolution_mode = "retargeted"
        resolution_warnings.append("Target section was retargeted while building the patch preview.")
    elif not original_target_id and resolved_target_id:
        resolution_mode = "fallback"
        resolution_warnings.append("Patch preview resolved a target without an explicit stored target id.")
    elif not resolved_target_id:
        resolution_mode = "missing"
        resolution_warnings.append("Patch preview could not resolve a stable target section.")
    patch_scope = "note" if merge_intent == "attach_compare_note_only" else "body"
    if merge_intent == "create_candidate_section":
        patch_scope = "section"
    if proposed_compare_note and merge_intent == "attach_compare_note_only" and not blocks:
        blocks.append(
            {
                "block_id": f"{candidate_id or 'patch'}-note",
                "operation": "note",
                "anchor_before": _make_structured_anchor(anchor_kind="note", anchor_value=resolved_target_id or target_section_title or source_section_title),
                "anchor_after": _make_structured_anchor(anchor_kind="note", anchor_value=resolved_target_id or target_section_title or source_section_title),
                "before_text": None,
                "after_text": proposed_compare_note,
                "before_excerpt": None,
                "after_excerpt": _excerpt(proposed_compare_note),
                "related_citation_keys": _citation_keys(target_section),
                "related_paper_ids": related_paper_ids,
                "warnings": list(warnings or []),
                "confidence": 0.9,
            }
        )
    structured_patch = {
        "candidate_id": candidate_id,
        "merge_intent": merge_intent,
        "source_run_id": source_run_id,
        "compare_partner_run_id": compare_partner_run_id,
        "source_section_id": source_section_id,
        "source_section_title": source_section_title,
        "target_section_id": resolved_target_id,
        "target_section_title": target_section_title or (target_section or {}).get("title") or (proposed_section or {}).get("title"),
        "patch_scope": patch_scope,
        "destructive": destructive,
        "requires_confirmation": requires_confirmation,
        "completeness": completeness,
        "provenance": provenance or {},
        "warnings": list(warnings or []),
        "conflict_flags": list(conflict_flags or []),
        "target_resolution": {
            "original_target_section_id": original_target_id,
            "resolved_target_section_id": resolved_target_id,
            "resolution_mode": resolution_mode,
            "resolution_confidence": round(
                max(
                    0.25,
                    average_confidence if resolution_mode == "exact" else average_confidence - 0.15 if resolution_mode == "retargeted" else average_confidence - 0.25,
                ),
                2,
            ),
            "retarget_warnings": resolution_warnings,
        },
        "blocks": blocks,
        "created_at": created_at,
    }
    return enrich_structured_patch_conflicts(structured_patch)


def build_body_patch_preview(
    *,
    merge_intent: str,
    target_section: dict[str, Any] | None,
    proposed_section: dict[str, Any] | None,
    proposed_compare_note: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    block_warnings = list(warnings or [])
    target_body = _body_text(target_section)
    proposed_body = _body_text(proposed_section)
    section_id = (
        (proposed_section or {}).get("section_id")
        or (target_section or {}).get("section_id")
        or (proposed_section or {}).get("title")
        or (target_section or {}).get("title")
    )
    citation_keys = _citation_keys(target_section) + [key for key in _citation_keys(proposed_section) if key not in _citation_keys(target_section)]
    blocks: list[dict[str, Any]] = []

    if merge_intent == "attach_compare_note_only":
        if target_body:
            blocks.append(
                _make_block(
                    block_id="context-1",
                    block_type="context",
                    label="Target body",
                    kind="target",
                    before_text=target_body,
                    after_text=target_body,
                    anchor_kind="section",
                    anchor_value=str(section_id or ""),
                    related_citation_keys=citation_keys,
                )
            )
        blocks.append(
            _make_block(
                block_id="note-1",
                block_type="added",
                label="Compare note",
                kind="note",
                before_text=None,
                after_text=proposed_compare_note or "",
                anchor_kind="note",
                anchor_value=str(section_id or ""),
                related_citation_keys=citation_keys,
                warnings=block_warnings,
            )
        )
        return {
            "preview_blocks": blocks,
            "changed_block_count": 1,
            "citation_delta": {
                "left_count": len(_citation_keys(target_section)),
                "right_count": len(_citation_keys(target_section)),
                "delta": 0,
                "notes": ["Compare note only action; body content remains unchanged."],
            },
            "completeness": "metadata_only",
        }

    if merge_intent in {"insert_before_target", "insert_after_target", "create_candidate_section"}:
        if target_body:
            blocks.append(
                _make_block(
                    block_id="context-1",
                    block_type="context",
                    label="Target context",
                    kind="target",
                    before_text=target_body,
                    after_text=target_body,
                    anchor_kind="section",
                    anchor_value=str(section_id or ""),
                    related_citation_keys=citation_keys,
                )
            )
        blocks.append(
            _make_block(
                block_id="added-1",
                block_type="added",
                label="Inserted candidate",
                kind="proposed",
                before_text=None,
                after_text=proposed_body,
                anchor_kind="section",
                anchor_value=str(section_id or ""),
                related_citation_keys=citation_keys,
                warnings=block_warnings,
            )
        )
        return {
            "preview_blocks": blocks,
            "changed_block_count": 1,
            "citation_delta": {
                "left_count": len(_citation_keys(target_section)),
                "right_count": len(_citation_keys(proposed_section)),
                "delta": len(_citation_keys(proposed_section)) - len(_citation_keys(target_section)),
                "notes": [],
            },
            "completeness": "coarse" if target_body else "partial",
        }

    if merge_intent == "append_to_target_section":
        combined_after = "\n\n".join([part for part in [target_body, proposed_body] if part.strip()])
        blocks.append(
            _make_block(
                block_id="context-1",
                block_type="context",
                label="Target before append",
                kind="target",
                before_text=target_body,
                after_text=target_body,
                anchor_kind="section",
                anchor_value=str(section_id or ""),
                related_citation_keys=citation_keys,
            )
        )
        blocks.append(
            _make_block(
                block_id="added-1",
                block_type="added",
                label="Appended fragment",
                kind="patch",
                before_text=target_body,
                after_text=combined_after,
                anchor_kind="section",
                anchor_value=str(section_id or ""),
                related_citation_keys=citation_keys,
                warnings=block_warnings,
            )
        )
        return {
            "preview_blocks": blocks,
            "changed_block_count": 1,
            "citation_delta": {
                "left_count": len(_citation_keys(target_section)),
                "right_count": len(_citation_keys({"paper_ids": citation_keys})),
                "delta": max(len(_citation_keys(proposed_section)), 0),
                "notes": ["Append preview uses coarse section-level patching."],
            },
            "completeness": "coarse",
        }

    before_parts = _paragraphs(target_body)
    after_parts = _paragraphs(proposed_body)
    matcher = difflib.SequenceMatcher(a=before_parts, b=after_parts)
    changed_block_count = 0
    for index, (opcode, a0, a1, b0, b1) in enumerate(matcher.get_opcodes(), start=1):
        before_text = "\n\n".join(before_parts[a0:a1]).strip() or None
        after_text = "\n\n".join(after_parts[b0:b1]).strip() or None
        if opcode == "equal":
            block_type = "unchanged"
            label = "Unchanged context"
        elif opcode == "insert":
            block_type = "added"
            label = "Added fragment"
            changed_block_count += 1
        elif opcode == "delete":
            block_type = "removed"
            label = "Removed fragment"
            changed_block_count += 1
        else:
            block_type = "replaced"
            label = "Replaced fragment"
            changed_block_count += 1
        blocks.append(
            _make_block(
                block_id=f"patch-{index}",
                block_type=block_type,
                label=label,
                kind="patch",
                before_text=before_text,
                after_text=after_text,
                anchor_kind="paragraph",
                anchor_value=str(section_id or ""),
                related_citation_keys=citation_keys,
                warnings=block_warnings if block_type != "unchanged" else [],
            )
        )

    if not blocks:
        blocks.append(
            _make_block(
                block_id="patch-empty",
                block_type="context",
                label="No body diff",
                kind="summary",
                before_text=target_body,
                after_text=proposed_body,
                anchor_kind="section",
                anchor_value=str(section_id or ""),
                related_citation_keys=citation_keys,
                warnings=["No paragraph-level diff could be derived from the current sections."],
            )
        )

    return {
        "preview_blocks": blocks,
        "changed_block_count": changed_block_count,
        "citation_delta": {
            "left_count": len(_citation_keys(target_section)),
            "right_count": len(_citation_keys(proposed_section)),
            "delta": len(_citation_keys(proposed_section)) - len(_citation_keys(target_section)),
            "notes": [],
        },
        "completeness": "full" if target_body and proposed_body else "partial",
    }


def _section_snapshot(section: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(section, dict):
        return None
    return {
        "section_id": section.get("section_id"),
        "title": section.get("title"),
        "kind": section.get("kind"),
        "body_markdown": section.get("body_markdown", ""),
        "paper_ids": [str(paper_id).strip() for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
        "compare_notes": [str(note).strip() for note in section.get("compare_notes", []) if str(note).strip()],
    }


def build_history_affected_section(
    *,
    merge_intent: str,
    before_section: dict[str, Any] | None,
    after_section: dict[str, Any] | None,
    compare_note: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    patch = build_body_patch_preview(
        merge_intent=merge_intent,
        target_section=before_section,
        proposed_section=after_section,
        proposed_compare_note=compare_note,
        warnings=warnings,
    )
    structured_patch = build_structured_patch(
        merge_intent=merge_intent,
        preview_blocks=list(patch.get("preview_blocks", [])),
        completeness=str(patch.get("completeness", "coarse")),
        target_section=before_section,
        proposed_section=after_section,
        proposed_compare_note=compare_note,
        warnings=warnings,
        target_section_id=(before_section or {}).get("section_id"),
        target_section_title=(before_section or {}).get("title") or (after_section or {}).get("title"),
    )
    section_id = (
        (after_section or {}).get("section_id")
        or (before_section or {}).get("section_id")
        or "unknown-section"
    )
    title = (after_section or {}).get("title") or (before_section or {}).get("title") or "Untitled Section"
    return {
        "section_id": str(section_id),
        "title": str(title),
        "before_excerpt": _excerpt(_body_text(before_section)),
        "after_excerpt": _excerpt(_body_text(after_section)),
        "before_paper_ids": [str(paper_id).strip() for paper_id in (before_section or {}).get("paper_ids", []) if str(paper_id).strip()],
        "after_paper_ids": [str(paper_id).strip() for paper_id in (after_section or {}).get("paper_ids", []) if str(paper_id).strip()],
        "changed_block_count": int(patch.get("changed_block_count", 0)),
        "completeness": str(patch.get("completeness", "coarse")),
        "preview_blocks": list(patch.get("preview_blocks", [])),
        "structured_patch": structured_patch,
        "related_paper_ids": _related_paper_ids(before_section, after_section),
        "warnings": list(warnings or []),
        "before_section_snapshot": _section_snapshot(before_section),
        "after_section_snapshot": _section_snapshot(after_section),
    }


def _normalize_history_affected_section(section: dict[str, Any]) -> dict[str, Any]:
    structured_patch = section.get("structured_patch")
    if not isinstance(structured_patch, dict):
        structured_patch = build_structured_patch(
            merge_intent="replace_target_section",
            preview_blocks=list(section.get("preview_blocks", [])),
            completeness=str(section.get("completeness", "coarse")),
            target_section=section.get("before_section_snapshot"),
            proposed_section=section.get("after_section_snapshot"),
            warnings=list(section.get("warnings", [])),
            target_section_id=section.get("section_id"),
            target_section_title=section.get("title"),
        )
    else:
        structured_patch = enrich_structured_patch_conflicts(structured_patch)
    return {
        "section_id": section.get("section_id"),
        "title": section.get("title"),
        "before_excerpt": section.get("before_excerpt"),
        "after_excerpt": section.get("after_excerpt"),
        "before_paper_ids": section.get("before_paper_ids", []),
        "after_paper_ids": section.get("after_paper_ids", []),
        "changed_block_count": section.get("changed_block_count", 0),
        "completeness": section.get("completeness", "coarse"),
        "preview_blocks": section.get("preview_blocks", []),
        "structured_patch": structured_patch,
        "related_paper_ids": _dedupe_strings(section.get("related_paper_ids", []) or _related_paper_ids(section.get("before_section_snapshot"), section.get("after_section_snapshot"))),
        "warnings": section.get("warnings", []),
    }


def append_outline_action_history(run_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    items = _read_history(run_id)
    items.append(entry)
    _write_history(run_id, items)
    return entry


def list_outline_action_history(run_id: str) -> dict[str, Any]:
    items = _read_history(run_id)
    items.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    normalized = []
    for item in items:
        normalized.append(
            {
                "action_id": item.get("action_id"),
                "candidate_id": item.get("candidate_id"),
                "editing_session_id": item.get("editing_session_id"),
                "merge_intent": item.get("merge_intent", "create_candidate_section"),
                "source_run_id": item.get("source_run_id"),
                "compare_partner_run_id": item.get("compare_partner_run_id"),
                "source_section_id": item.get("source_section_id"),
                "target_section_id": item.get("target_section_id"),
                "timestamp": item.get("timestamp"),
                "outcome": item.get("outcome", "applied"),
                "warnings": item.get("warnings", []),
                "before_outline_hash": item.get("before_outline_hash"),
                "after_outline_hash": item.get("after_outline_hash"),
                "affected_sections": [
                    _normalize_history_affected_section(section)
                    for section in item.get("affected_sections", [])
                    if isinstance(section, dict)
                ],
                "provenance": item.get("provenance", {}),
                "preview_summary": item.get("preview_summary", []),
            }
        )
    return {"run_id": run_id, "items": normalized, "warnings": []}


def _find_history_entry(run_id: str, action_id: str | None) -> dict[str, Any] | None:
    if not action_id:
        return None
    return next((item for item in _read_history(run_id) if item.get("action_id") == action_id), None)


def _section_after_map(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(section.get("section_id")): section.get("after_section_snapshot")
        for section in entry.get("affected_sections", [])
        if isinstance(section, dict) and section.get("section_id")
    }


def _section_title_map(entry: dict[str, Any]) -> dict[str, str]:
    return {
        str(section.get("section_id")): str(section.get("title") or section.get("section_id"))
        for section in entry.get("affected_sections", [])
        if isinstance(section, dict) and section.get("section_id")
    }


def get_outline_action_history_diff(
    run_id: str,
    *,
    from_action_id: str | None = None,
    to_action_id: str | None = None,
    against_current_draft: bool = False,
) -> dict[str, Any]:
    history = _read_history(run_id)
    if not history:
        return {
            "run_id": run_id,
            "from_action_id": from_action_id,
            "to_action_id": to_action_id,
            "ready": False,
            "mode": "single_action",
            "items": [],
            "warnings": ["No outline action history is available for this run yet."],
        }

    if to_action_id is None and from_action_id is None:
        to_entry = history[-1]
        from_entry = None
    else:
        to_entry = _find_history_entry(run_id, to_action_id or from_action_id)
        from_entry = _find_history_entry(run_id, from_action_id) if from_action_id else None

    warnings: list[str] = []
    if to_entry is None:
        return {
            "run_id": run_id,
            "from_action_id": from_action_id,
            "to_action_id": to_action_id,
            "ready": False,
            "mode": "single_action",
            "items": [],
            "warnings": ["The requested outline action history entry could not be found."],
        }

    if against_current_draft:
        outline = reporting.load_outline(run_id)
        current_sections = {
            str(section.get("section_id")): section
            for section in outline.get("sections", [])
            if isinstance(section, dict) and section.get("section_id")
        }
        items = []
        for section in to_entry.get("affected_sections", []):
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            affected = build_history_affected_section(
                merge_intent="replace_target_section",
                before_section=section.get("after_section_snapshot"),
                after_section=current_sections.get(section_id),
                warnings=section.get("warnings", []),
            )
            affected["title"] = section.get("title") or affected.get("title") or section_id
            items.append(_normalize_history_affected_section(affected))
        if not items:
            warnings.append("The selected action does not contain enough section snapshots to compare against the current draft.")
        return {
            "run_id": run_id,
            "from_action_id": to_entry.get("action_id"),
            "to_action_id": None,
            "ready": bool(items),
            "mode": "action_vs_current_draft",
            "items": items,
            "warnings": warnings,
        }

    if from_entry is None or from_entry.get("action_id") == to_entry.get("action_id"):
        return {
            "run_id": run_id,
            "from_action_id": to_entry.get("action_id"),
            "to_action_id": to_entry.get("action_id"),
            "ready": True,
            "mode": "single_action",
            "items": [
                _normalize_history_affected_section(section)
                for section in to_entry.get("affected_sections", [])
                if isinstance(section, dict)
            ],
            "warnings": warnings,
        }

    from_after = _section_after_map(from_entry)
    to_after = _section_after_map(to_entry)
    title_map = {**_section_title_map(from_entry), **_section_title_map(to_entry)}
    union_ids = list(dict.fromkeys([*from_after.keys(), *to_after.keys()]))
    items: list[dict[str, Any]] = []
    for section_id in union_ids:
        before_section = from_after.get(section_id)
        after_section = to_after.get(section_id)
        affected = build_history_affected_section(
            merge_intent="replace_target_section",
            before_section=before_section,
            after_section=after_section,
            warnings=[],
        )
        affected["title"] = title_map.get(section_id, affected.get("title") or section_id)
        items.append(_normalize_history_affected_section(affected))
    if not items:
        warnings.append("The selected history entries do not contain affected section snapshots to diff.")

    return {
        "run_id": run_id,
        "from_action_id": from_entry.get("action_id"),
        "to_action_id": to_entry.get("action_id"),
        "ready": bool(items),
        "mode": "between_actions",
        "items": items,
        "warnings": warnings,
    }


def get_outline_action_focus_context(run_id: str, action_id: str) -> dict[str, Any]:
    entry = _find_history_entry(run_id, action_id)
    if entry is None:
        return {
            "run_id": run_id,
            "action_id": action_id,
            "ready": False,
            "editing_session_id": None,
            "compare_partner_run_id": None,
            "source_run_id": None,
            "affected_section_ids": [],
            "focus_section_id": None,
            "focus_section_title": None,
            "related_paper_ids": [],
            "focus_paper_id": None,
            "selection_source": "suggested",
            "provenance_summary": [],
            "source_artifacts_available": False,
            "route_restore_hints": {
                "compare_view": None,
                "active_pane": "report",
                "editing_session_id": None,
                "compare_partner_run_id": None,
                "focus_section_id": None,
                "focus_section_title": None,
                "focus_paper_id": None,
                "selected_paper_ids": [],
                "selection_source": "suggested",
                "warnings": ["The requested outline history action could not be found."],
            },
            "warnings": ["The requested outline history action could not be found."],
        }

    outline = reporting.load_outline(run_id)
    current_section_ids = {
        str(section.get("section_id"))
        for section in outline.get("sections", [])
        if isinstance(section, dict) and section.get("section_id")
    }
    affected_sections = [section for section in entry.get("affected_sections", []) if isinstance(section, dict)]
    affected_section_ids = [str(section.get("section_id")) for section in affected_sections if section.get("section_id")]
    focus_section = next(
        (section for section in affected_sections if str(section.get("section_id")) in current_section_ids),
        affected_sections[0] if affected_sections else None,
    )
    related_paper_ids = list(
        dict.fromkeys(
            [
                *[
                    str(paper_id).strip()
                    for section in affected_sections
                    for paper_id in section.get("related_paper_ids", [])
                    if str(paper_id).strip()
                ],
                *[
                    str(paper_id).strip()
                    for section in affected_sections
                    for paper_id in section.get("before_paper_ids", [])
                    if str(paper_id).strip()
                ],
                *[
                    str(paper_id).strip()
                    for section in affected_sections
                    for paper_id in section.get("after_paper_ids", [])
                    if str(paper_id).strip()
                ],
                *[
                    str(paper_id).strip()
                    for paper_id in (entry.get("provenance", {}) or {}).get("source_paper_ids", [])
                    if str(paper_id).strip()
                ],
            ]
        )
    )
    warnings: list[str] = []
    source_artifacts_available = True
    editing_session_id = entry.get("editing_session_id")
    if editing_session_id:
        try:
            editing_sessions.get_editing_session(run_id, str(editing_session_id))
        except FileNotFoundError:
            warnings.append(f"Editing session {editing_session_id} is no longer available.")
            editing_session_id = None
    for related_run_id in [entry.get("source_run_id"), entry.get("compare_partner_run_id")]:
        if not related_run_id:
            continue
        try:
            load_run_record(str(related_run_id))
        except FileNotFoundError:
            source_artifacts_available = False
            warnings.append(f"Referenced run {related_run_id} is no longer available.")
    provenance_summary = [
        f"Intent: {entry.get('merge_intent', 'unknown')}",
        *[str(line) for line in entry.get("preview_summary", []) if str(line).strip()],
    ]
    if entry.get("source_section_id") or entry.get("provenance", {}).get("source_section_title"):
        provenance_summary.append(
            f"Source section: {entry.get('provenance', {}).get('source_section_title') or entry.get('source_section_id')}"
        )
    route_restore_hints = {
        "compare_view": "report" if focus_section else "papers" if related_paper_ids else None,
        "active_pane": "report" if focus_section else "paper",
        "editing_session_id": editing_session_id,
        "compare_partner_run_id": entry.get("compare_partner_run_id"),
        "focus_section_id": focus_section.get("section_id") if focus_section else None,
        "focus_section_title": focus_section.get("title") if focus_section else None,
        "focus_paper_id": related_paper_ids[0] if related_paper_ids else None,
        "selected_paper_ids": related_paper_ids,
        "selection_source": "suggested",
        "warnings": warnings,
    }
    return {
        "run_id": run_id,
        "action_id": action_id,
        "ready": bool(focus_section or related_paper_ids),
        "editing_session_id": editing_session_id,
        "compare_partner_run_id": entry.get("compare_partner_run_id"),
        "source_run_id": entry.get("source_run_id"),
        "affected_section_ids": affected_section_ids,
        "focus_section_id": focus_section.get("section_id") if focus_section else None,
        "focus_section_title": focus_section.get("title") if focus_section else None,
        "related_paper_ids": related_paper_ids,
        "focus_paper_id": related_paper_ids[0] if related_paper_ids else None,
        "selection_source": "suggested",
        "provenance_summary": provenance_summary,
        "source_artifacts_available": source_artifacts_available,
        "route_restore_hints": route_restore_hints,
        "warnings": warnings,
    }

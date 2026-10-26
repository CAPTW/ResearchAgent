from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc, read_json, read_jsonl, read_text, write_json, write_text
from app.repositories.artifact_refs import display_artifact_reference, normalize_payload_references, resolve_payload_references
from app.repositories import ai_review_drafts, metadata_index, paper_reviews
from app.repositories.runs import build_run_paths, load_run_record, save_run_record
from app.services import (
    experiments,
    provider_review_coverage,
    recommendations,
    report_quality,
    research_domain,
    research_plan,
    reviews,
)

OUTLINE_FILENAME = "outline.json"
PREVIEW_FILENAME = "preview.md"
_LEGACY_EXPLICIT_PREVIEW_WRITER_MODULES = {
    "app.services.exports",
    "app.services.workspace_actions",
}


def _report_paths(run_id: str) -> tuple[Path, Path]:
    paths = build_run_paths(run_id)
    return paths.reports_dir / OUTLINE_FILENAME, paths.reports_dir / PREVIEW_FILENAME


def load_outline_if_present(run_id: str) -> dict[str, Any] | None:
    outline_path, _ = _report_paths(run_id)
    if not outline_path.is_file():
        return None
    raw = read_json(outline_path, default={}) or {}
    return _normalize_outline(run_id, resolve_payload_references(raw, run_id=run_id))


def _report_markdown_candidates(run_id: str) -> list[Path]:
    reports_dir = build_run_paths(run_id).reports_dir
    return sorted(
        [
            path
            for path in reports_dir.glob("*.md")
            if path.name != PREVIEW_FILENAME and not path.name.startswith("exported_")
        ]
    )


def _load_triage_items(run_id: str) -> list[dict[str, Any]]:
    run = load_run_record(run_id)
    triage_path = Path(run.get("artifacts", {}).get("triage_jsonl") or build_run_paths(run_id).triage_dir / "triage.jsonl")
    return read_jsonl(triage_path)


def _parse_report_sections(content: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"_intro": []}
    current = "_intro"
    for line in content.splitlines():
        heading_match = re.match(r"^##\s+(.*)$", line.strip())
        if heading_match:
            current = heading_match.group(1).strip()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def collect_outline_summary(run_id: str) -> list[dict[str, Any]]:
    run = load_run_record(run_id)
    outline_path = Path(run.get("artifacts", {}).get("outline_json") or _report_paths(run_id)[0])
    if outline_path.is_file():
        payload = read_json(outline_path, default={}) or {}
        sections = payload.get("sections") or []
        return [
            {
                "section_id": section.get("section_id", ""),
                "title": section.get("title", ""),
                "kind": section.get("kind", "markdown"),
                "paper_ids": [str(paper_id) for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
                "body_hash": hashlib.sha1(str(section.get("body_markdown", "")).encode("utf-8")).hexdigest()[:12],
            }
            for section in sections
        ]

    compare_ready_sections = run.get("compare_ready", {}).get("outline_sections", []) or []
    if compare_ready_sections:
        return [
            {
                "section_id": section.get("section_id", ""),
                "title": section.get("title", ""),
                "kind": section.get("kind", "markdown"),
                "paper_ids": [str(paper_id) for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
                "body_hash": None,
            }
            for section in compare_ready_sections
        ]

    report_candidates = _report_markdown_candidates(run_id)
    if not report_candidates:
        return []

    sections = _parse_report_sections(read_text(report_candidates[-1]))
    summaries: list[dict[str, Any]] = []
    for index, (title, body) in enumerate(sections.items(), start=1):
        section_id = "_intro" if title == "_intro" else re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or f"section-{index}"
        summaries.append(
            {
                "section_id": section_id,
                "title": "Executive Summary" if title == "_intro" else title,
                "kind": "markdown",
                "paper_ids": [],
                "body_hash": hashlib.sha1(body.encode("utf-8")).hexdigest()[:12],
            }
        )
    return summaries


def collect_preview_fingerprint(run_id: str) -> dict[str, str | None]:
    run = load_run_record(run_id)
    compare_ready = run.get("compare_ready", {}) or {}
    preview_hash = compare_ready.get("preview_hash")
    if preview_hash:
        return {"hash": str(preview_hash), "source": "preview"}

    preview_path = Path(run.get("artifacts", {}).get("preview_markdown") or _report_paths(run_id)[1])
    if preview_path.is_file():
        return {"hash": hashlib.sha1(read_text(preview_path).encode("utf-8")).hexdigest()[:12], "source": "preview"}

    report_path = Path(run.get("artifacts", {}).get("report_markdown") or "")
    if report_path.is_file():
        return {"hash": hashlib.sha1(read_text(report_path).encode("utf-8")).hexdigest()[:12], "source": "report_markdown"}

    report_candidates = _report_markdown_candidates(run_id)
    if report_candidates:
        return {"hash": hashlib.sha1(read_text(report_candidates[-1]).encode("utf-8")).hexdigest()[:12], "source": "report_markdown"}

    return {"hash": None, "source": None}


def _preview_paper_lines(run_id: str, paper_ids: list[str], papers: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    for paper_id in paper_ids:
        paper = papers.get(paper_id)
        if not paper:
            continue
        review = reviews.load_review_payload(run_id, paper)
        summary = review.get("one_liner") or str(paper.get("abstract", "")).strip()[:240]
        lines.append(f"- **{paper.get('title', '')}** (`{paper_id}`)")
        lines.append(f"  - score: {paper.get('score', 0)} | theme: {paper.get('theme', 'Uncategorized')}")
        if summary:
            lines.append(f"  - {summary}")
    return "\n".join(lines).strip()


def _render_claim_lines(run_id: str, claims: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for claim in claims:
        claim_text = str(claim.get("claim_text", "")).strip()
        if not claim_text:
            continue
        lines.append(f"- {claim_text}")
        for citation in claim.get("citations", []) or []:
            evidence_id = str(citation.get("evidence_id", "")).strip() or "unknown"
            source_id = str(citation.get("source_id", "")).strip() or "unknown"
            raw_artifact_path = str(citation.get("artifact_path", "")).strip()
            artifact_path = (
                display_artifact_reference(raw_artifact_path, run_id=run_id)
                if raw_artifact_path
                else "n/a"
            )
            citation_text = str(citation.get("citation_text", "")).strip() or source_id
            snippet = str(citation.get("snippet", "")).strip()
            lines.append(f"  - evidence_id={evidence_id} | source_id={source_id} | artifact_path={artifact_path}")
            lines.append(f"  - citation: {citation_text}")
            if snippet:
                lines.append(f"  - snippet: {snippet}")
    return lines


def _default_outline(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    plan = research_plan.load_plan_if_present(run_id)
    if plan is not None and plan.sections:
        return {
            "run_id": run_id,
            "path": str(_report_paths(run_id)[0]),
            "updated_at": iso_now_utc(),
            "sections": [
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "kind": "papers" if section.section_id == "top-papers" else "markdown",
                    "body_markdown": section.summary,
                    "paper_ids": section.paper_ids,
                    "compare_notes": [],
                    "provenance": [
                        {
                            "action_id": None,
                            "action_type": "research_plan_generation",
                            "source_run_id": run_id,
                            "compare_partner_run_id": None,
                            "source_section_id": section.section_id,
                            "source_section_title": section.title,
                            "source_bucket_id": None,
                            "source_theme": None,
                            "source_paper_ids": claim.get("source_ids", []),
                            "preferred_report_side": None,
                            "created_at": plan.generated_at,
                            "payload_title": claim.get("claim_text"),
                        }
                        for claim in [claim.model_dump() for claim in section.claims]
                    ],
                    "claims": [claim.model_dump() for claim in section.claims],
                }
                for section in plan.sections
            ],
            "applied_actions": [],
        }
    report_candidates = sorted(build_run_paths(run_id).reports_dir.glob("*.md"))
    report_content = read_text(report_candidates[-1]) if report_candidates else ""
    report_sections = _parse_report_sections(report_content) if report_content else {}
    triage_items = _load_triage_items(run_id)
    top_papers = triage_items[:5]
    theme_counts = Counter(item.get("theme") or "Uncategorized" for item in triage_items[:20])
    executive_lines = [
        f"- Topic: {run.get('topic', '')}",
        f"- Run status: {run.get('status', '')}",
        f"- Ranked papers: {run.get('counts', {}).get('triage_items', len(triage_items))}",
    ]
    if theme_counts:
        executive_lines.append(
            "- Top themes: " + ", ".join(f"{theme} ({count})" for theme, count in theme_counts.most_common(3))
        )

    sections = [
        {
            "section_id": "executive-summary",
            "title": "Executive Summary",
            "kind": "markdown",
            "body_markdown": report_sections.get("_intro") or "\n".join(executive_lines),
            "paper_ids": [],
            "compare_notes": [],
            "provenance": [],
        },
        {
            "section_id": "top-papers",
            "title": "Top Papers",
            "kind": "papers",
            "body_markdown": report_sections.get("Top papers (short)", ""),
            "paper_ids": [item.get("paper_id", "") for item in top_papers if item.get("paper_id")],
            "compare_notes": [],
            "provenance": [],
        },
        {
            "section_id": "trend-signals",
            "title": "Trend Signals",
            "kind": "markdown",
            "body_markdown": report_sections.get("Trend signals (heuristic)", ""),
            "paper_ids": [],
            "compare_notes": [],
            "provenance": [],
        },
        {
            "section_id": "research-gaps",
            "title": "Research Gaps / Open Questions",
            "kind": "markdown",
            "body_markdown": report_sections.get("Research gaps / open questions (draft)", ""),
            "paper_ids": [],
            "compare_notes": [],
            "provenance": [],
        },
        {
            "section_id": "next-experiments",
            "title": "Proposed Experiment Scaffolds (Not Executed)",
            "kind": "markdown",
            "body_markdown": report_sections.get(
                "Proposed Experiment Scaffolds (Not Executed)",
                report_sections.get("Next experiments (lightweight)", ""),
            ),
            "paper_ids": [],
            "compare_notes": [],
            "provenance": [],
        },
    ]
    return {
        "run_id": run_id,
        "path": str(_report_paths(run_id)[0]),
        "updated_at": iso_now_utc(),
        "sections": sections,
        "applied_actions": [],
    }


def _normalize_outline(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    seen_ids: set[str] = set()
    normalized_sections: list[dict[str, Any]] = []
    for index, section in enumerate(payload.get("sections") or [], start=1):
        title = str(section.get("title", "")).strip() or f"Section {index}"
        section_id = str(section.get("section_id", "")).strip() or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if not section_id:
            section_id = f"section-{index}"
        if section_id in seen_ids:
            section_id = f"{section_id}-{index}"
        seen_ids.add(section_id)
        kind = "papers" if section.get("kind") == "papers" else "markdown"
        normalized_sections.append(
            {
                "section_id": section_id,
                "title": title,
                "kind": kind,
                "body_markdown": str(section.get("body_markdown", "") or ""),
                "paper_ids": [str(paper_id) for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
                "compare_notes": [str(note).strip() for note in section.get("compare_notes", []) if str(note).strip()],
                "claims": [
                    {
                        "claim_id": str(claim.get("claim_id", "") or "").strip(),
                        "claim_text": str(claim.get("claim_text", "") or "").strip(),
                        "importance": str(claim.get("importance", "medium") or "medium"),
                        "source_ids": [str(source_id) for source_id in claim.get("source_ids", []) if str(source_id).strip()],
                        "citations": [
                            {
                                "evidence_id": str(citation.get("evidence_id", "") or "").strip(),
                                "source_id": str(citation.get("source_id", "") or "").strip(),
                                "paper_id": str(citation.get("paper_id", "") or "").strip(),
                                "artifact_path": str(citation.get("artifact_path", "") or "") or None,
                                "citation_text": str(citation.get("citation_text", "") or "").strip(),
                                "snippet": str(citation.get("snippet", "") or "").strip(),
                            }
                            for citation in claim.get("citations", [])
                            if isinstance(citation, dict)
                        ],
                    }
                    for claim in section.get("claims", [])
                    if isinstance(claim, dict)
                ],
                "provenance": [
                    {
                        "action_id": str(entry.get("action_id", "") or "") or None,
                        "action_type": str(entry.get("action_type", "") or "") or None,
                        "source_run_id": str(entry.get("source_run_id", "") or "") or None,
                        "compare_partner_run_id": str(entry.get("compare_partner_run_id", "") or "") or None,
                        "source_section_id": str(entry.get("source_section_id", "") or "") or None,
                        "source_section_title": str(entry.get("source_section_title", "") or "") or None,
                        "source_bucket_id": str(entry.get("source_bucket_id", "") or "") or None,
                        "source_theme": str(entry.get("source_theme", "") or "") or None,
                        "source_paper_ids": [
                            str(paper_id)
                            for paper_id in entry.get("source_paper_ids", [])
                            if str(paper_id).strip()
                        ],
                        "preferred_report_side": str(entry.get("preferred_report_side", "") or "") or None,
                        "created_at": str(entry.get("created_at", "") or "") or None,
                        "payload_title": str(entry.get("payload_title", "") or "") or None,
                    }
                    for entry in section.get("provenance", [])
                    if isinstance(entry, dict)
                ],
            }
        )
    return {
        "run_id": run_id,
        "path": str(_report_paths(run_id)[0]),
        "updated_at": iso_now_utc(),
        "sections": normalized_sections,
        "applied_actions": [
            {
                "action_id": str(entry.get("action_id", "") or "") or None,
                "action_type": str(entry.get("action_type", "") or "") or None,
                "source_run_id": str(entry.get("source_run_id", "") or "") or None,
                "compare_partner_run_id": str(entry.get("compare_partner_run_id", "") or "") or None,
                "source_section_id": str(entry.get("source_section_id", "") or "") or None,
                "source_section_title": str(entry.get("source_section_title", "") or "") or None,
                "source_bucket_id": str(entry.get("source_bucket_id", "") or "") or None,
                "source_theme": str(entry.get("source_theme", "") or "") or None,
                "source_paper_ids": [
                    str(paper_id)
                    for paper_id in entry.get("source_paper_ids", [])
                    if str(paper_id).strip()
                ],
                "created_at": str(entry.get("created_at", "") or "") or None,
            }
            for entry in payload.get("applied_actions", [])
            if isinstance(entry, dict)
        ],
    }


def _merge_plan_into_outline(run_id: str, outline: dict[str, Any]) -> dict[str, Any]:
    plan = research_plan.load_plan_if_present(run_id)
    if plan is None:
        return outline
    plan_sections = {section.section_id: section for section in plan.sections}
    merged_sections: list[dict[str, Any]] = []
    changed = False
    for section in outline.get("sections", []):
        plan_section = plan_sections.get(str(section.get("section_id", "")).strip())
        if plan_section is None:
            merged_sections.append(section)
            continue
        existing_claims = section.get("claims", []) or []
        if existing_claims:
            merged_sections.append(section)
            continue
        enriched = dict(section)
        enriched["claims"] = [claim.model_dump() for claim in plan_section.claims]
        if not str(enriched.get("body_markdown", "")).strip():
            enriched["body_markdown"] = plan_section.summary
        if not enriched.get("paper_ids"):
            enriched["paper_ids"] = list(plan_section.paper_ids)
        if not enriched.get("provenance"):
            enriched["provenance"] = [
                {
                    "action_id": None,
                    "action_type": "research_plan_generation",
                    "source_run_id": run_id,
                    "compare_partner_run_id": None,
                    "source_section_id": plan_section.section_id,
                    "source_section_title": plan_section.title,
                    "source_bucket_id": None,
                    "source_theme": None,
                    "source_paper_ids": claim.source_ids,
                    "preferred_report_side": None,
                    "created_at": plan.generated_at,
                    "payload_title": claim.claim_text,
                }
                for claim in plan_section.claims
            ]
        merged_sections.append(enriched)
        changed = True
    if not changed:
        return outline
    enriched_outline = dict(outline)
    enriched_outline["sections"] = merged_sections
    return enriched_outline


def _sync_compare_metadata(run_id: str, outline: dict[str, Any], preview_path: str) -> None:
    run = load_run_record(run_id)
    triage_items = _load_triage_items(run_id)
    theme_counts = Counter(item.get("theme") or "Uncategorized" for item in triage_items[:25])
    preview_hash = hashlib.sha1(read_text(Path(preview_path)).encode("utf-8")).hexdigest()[:12]
    run["compare_ready"] = {
        "top_papers": [
            {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "score": item.get("score", 0),
                "theme": item.get("theme", "Uncategorized"),
            }
            for item in triage_items[:10]
        ],
        "theme_counts": dict(theme_counts.most_common(6)),
        "outline_path": outline.get("path"),
        "preview_path": preview_path,
        "preview_hash": preview_hash,
        "outline_sections": [
            {
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "kind": section.get("kind"),
                "paper_ids": section.get("paper_ids", []),
            }
            for section in outline.get("sections", [])
        ],
    }
    run.setdefault("artifacts", {})["outline_json"] = outline.get("path")
    run.setdefault("artifacts", {})["preview_markdown"] = preview_path
    save_run_record(run)
    metadata_index.refresh_run(run_id, reason="report_write")


def compute_outline_draft_hash(*, outline: dict[str, Any] | None = None, run_id: str | None = None) -> str:
    target_outline = outline or (load_outline(run_id) if run_id else None)
    if target_outline is None:
        return ""
    payload = {
        "sections": [
            {
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "kind": section.get("kind"),
                "body_markdown": section.get("body_markdown", ""),
                "paper_ids": section.get("paper_ids", []),
                "compare_notes": section.get("compare_notes", []),
            }
            for section in target_outline.get("sections", [])
        ],
        "applied_actions": target_outline.get("applied_actions", []),
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def ensure_compare_ready_metadata(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    try:
        triage_items = _load_triage_items(run_id)
    except FileNotFoundError:
        triage_items = []

    outline_sections = collect_outline_summary(run_id)
    preview = collect_preview_fingerprint(run_id)
    theme_counts = Counter(item.get("theme") or "Uncategorized" for item in triage_items[:25])

    run["compare_ready"] = {
        "top_papers": [
            {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "score": item.get("score", 0),
                "theme": item.get("theme", "Uncategorized"),
            }
            for item in triage_items[:10]
            if item.get("paper_id")
        ],
        "theme_counts": dict(theme_counts.most_common(6)),
        "outline_path": run.get("artifacts", {}).get("outline_json"),
        "preview_path": run.get("artifacts", {}).get("preview_markdown"),
        "preview_hash": preview.get("hash"),
        "outline_sections": [
            {
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "kind": section.get("kind"),
                "paper_ids": section.get("paper_ids", []),
            }
            for section in outline_sections
        ],
    }
    save_run_record(run)
    return run["compare_ready"]


def load_outline(run_id: str, *, materialize_missing: bool = False) -> dict[str, Any]:
    existing = load_outline_if_present(run_id)
    if existing is not None:
        return _merge_plan_into_outline(run_id, existing)
    outline_path, _ = _report_paths(run_id)
    outline = _default_outline(run_id)
    if materialize_missing:
        write_json(outline_path, outline)
    return outline


def save_outline(run_id: str, sections: list[dict[str, Any]], *, applied_actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    outline_path, _ = _report_paths(run_id)
    existing = load_outline_if_present(run_id)
    outline = _normalize_outline(
        run_id,
        {
            "sections": sections,
            "applied_actions": applied_actions if applied_actions is not None else (existing or {}).get("applied_actions", []),
        },
    )
    write_json(outline_path, normalize_payload_references(outline, run_id=run_id))
    write_preview_artifact(run_id, outline=outline)
    return outline


def _called_from_legacy_explicit_preview_writer() -> bool:
    for frame in inspect.stack()[2:12]:
        module_name = str(frame.frame.f_globals.get("__name__") or "")
        if module_name in _LEGACY_EXPLICIT_PREVIEW_WRITER_MODULES:
            return True
    return False


def _render_preview_content(run_id: str, outline: dict[str, Any], *, papers: dict[str, Any] | None = None) -> str:
    if papers is None:
        papers = {item["paper_id"]: item for item in _load_triage_items(run_id) if item.get("paper_id")}
    domain_context = research_domain.research_domain_context_for_run(
        load_run_record(run_id)
    )

    lines: list[str] = [
        f"# Research Report Preview — {research_domain.markdown_inline(domain_context.display_name)}",
        "",
    ]
    for section in outline.get("sections", []):
        lines.append(f"## {section.get('title', 'Untitled Section')}")
        body = str(section.get("body_markdown", "") or "").strip()
        if body:
            lines.append(body)
            lines.append("")
        rendered_claims = _render_claim_lines(run_id, section.get("claims", []) or [])
        if rendered_claims:
            lines.extend(rendered_claims)
            lines.append("")
        if section.get("kind") == "papers" and section.get("paper_ids"):
            rendered_papers = _preview_paper_lines(run_id, section.get("paper_ids", []), papers)
            lines.append(rendered_papers or "_선택된 논문이 없습니다._")
            lines.append("")
        elif not body:
            lines.append("_(empty section)_")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def _run_quality_summary(run_id: str, markdown: str, *, evaluated_at: str, surface: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    topic_id = str(run.get("topic_profile_id") or run.get("topic") or run_id)
    try:
        reviews_payload = paper_reviews.list_reviews(topic_id=topic_id, run_id=run_id, limit=100, read_only=True)
    except FileNotFoundError:
        reviews_payload = []
    try:
        result = report_quality.evaluate_weekly_journal_quality(
            markdown,
            reviews=_dedupe_reviews(reviews_payload),
            expected_topic=topic_id,
            evaluated_at=evaluated_at,
        )
        result["evaluation_status"] = "evaluated"
        result["evaluated_surface"] = surface
        return result
    except Exception as exc:  # pragma: no cover - defensive; caller must not break
        return {
            "evaluation_status": "unavailable",
            "evaluation_method": report_quality.EVALUATION_METHOD,
            "rubric_version": report_quality.RUBRIC_VERSION,
            "report_kind": "weekly_journal",
            "evaluated_surface": surface,
            "topic": topic_id,
            "checked_at": evaluated_at,
            "error": str(exc),
        }


def _preview_quality_summary(run_id: str, markdown: str, *, evaluated_at: str) -> dict[str, Any]:
    return _run_quality_summary(run_id, markdown, evaluated_at=evaluated_at, surface="report_preview")


def evaluate_report_quality_for_run(
    run_id: str, *, report_markdown: str, evaluated_at: str | None = None
) -> dict[str, Any]:
    """Evaluate deterministic report quality for a finalized run and tag it for persistence.

    Reuses the same rubric as the preview surface so finalize/persist/rollup stay consistent.
    """
    return _run_quality_summary(
        run_id,
        str(report_markdown or ""),
        evaluated_at=evaluated_at or iso_now_utc(),
        surface="run_finalize",
    )


def build_preview(run_id: str, *, outline: dict[str, Any] | None = None, persist: bool | None = None) -> dict[str, Any]:
    should_persist = _called_from_legacy_explicit_preview_writer() if persist is None else persist
    outline = outline or load_outline(run_id, materialize_missing=should_persist)
    _, preview_path = _report_paths(run_id)
    content = _render_preview_content(run_id, outline)
    generated_at = iso_now_utc()
    if should_persist:
        write_text(preview_path, content)
        _sync_compare_metadata(run_id, outline, str(preview_path))
    return {
        "run_id": run_id,
        "path": str(preview_path),
        "content": content,
        "generated_at": generated_at,
        "quality": _preview_quality_summary(run_id, content, evaluated_at=generated_at),
    }


def write_preview_artifact(run_id: str, *, outline: dict[str, Any] | None = None) -> dict[str, Any]:
    return build_preview(run_id, outline=outline, persist=True)


def build_citation_trace(run_id: str, *, persist_preview: bool = False) -> dict[str, Any]:
    outline = load_outline(run_id, materialize_missing=persist_preview)
    preview = build_preview(run_id, outline=outline, persist=persist_preview)
    papers = {item.get("paper_id", ""): item for item in _load_triage_items(run_id) if item.get("paper_id")}
    return _citation_trace(run_id, outline, papers, preview["path"])


def capture_version_citations(run_id: str, content: str) -> dict[str, Any]:
    """Freeze only a trace whose rendered outline equals the saved body.

    This does not infer citations for journal/provider output from a different
    outline. Both rendering and trace extraction use the same in-memory inputs.
    """
    if load_outline_if_present(run_id) is None:
        return {"status": "OUTLINE_UNAVAILABLE", "items": [], "warnings": []}
    outline = load_outline(run_id)
    papers = {item["paper_id"]: item for item in _load_triage_items(run_id) if item.get("paper_id")}
    rendered = _render_preview_content(run_id, outline, papers=papers)
    if rendered.replace("\r\n", "\n") != content.replace("\r\n", "\n"):
        return {"status": "UNBOUND_CONTENT", "items": [], "warnings": []}
    trace = _citation_trace(run_id, outline, papers, str(_report_paths(run_id)[1]))
    return {**trace, "status": "CAPTURED", "binding_method": "EXACT_RENDERED_OUTLINE_V1"}


def _citation_trace(run_id: str, outline: dict[str, Any], papers: dict[str, Any], preview_path: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    warnings: list[str] = []

    for section in outline.get("sections", []):
        section_id = str(section.get("section_id", "") or "")
        section_title = str(section.get("title", "") or section_id)
        provenance_entries = section.get("provenance", []) or []
        for claim_index, claim in enumerate(section.get("claims", []) or [], start=1):
            claim_id = str(claim.get("claim_id") or f"{section_id}-claim-{claim_index}")
            citations = [citation for citation in claim.get("citations", []) or [] if isinstance(citation, dict)]
            if not citations:
                warnings.append(f"{section_id}: claim {claim_id} has no citation artifacts.")
            for citation_index, citation in enumerate(citations, start=1):
                paper_id = str(citation.get("paper_id") or citation.get("source_id") or "").strip()
                paper = papers.get(paper_id, {})
                items.append(
                    {
                        "citation_id": f"{section_id}:{claim_id}:{citation_index}",
                        "section_id": section_id,
                        "section_title": section_title,
                        "claim_id": claim_id,
                        "claim_text": str(claim.get("claim_text", "") or "") or None,
                        "evidence_id": str(citation.get("evidence_id", "") or "") or None,
                        "source_id": str(citation.get("source_id", "") or "") or None,
                        "paper_id": paper_id or None,
                        "paper_title": str(paper.get("title", "") or "") or None,
                        "artifact_path": str(citation.get("artifact_path", "") or "") or None,
                        "citation_text": str(citation.get("citation_text", "") or "") or None,
                        "snippet": str(citation.get("snippet", "") or "") or None,
                        "provenance": {
                            "outline_path": outline.get("path"),
                            "preview_path": preview_path,
                            "source_run_id": run_id,
                            "section_provenance": provenance_entries,
                        },
                    }
                )

        if section.get("kind") == "papers":
            for paper_id in section.get("paper_ids", []) or []:
                paper = papers.get(str(paper_id), {})
                items.append(
                    {
                        "citation_id": f"{section_id}:paper:{paper_id}",
                        "section_id": section_id,
                        "section_title": section_title,
                        "claim_id": None,
                        "claim_text": None,
                        "evidence_id": None,
                        "source_id": str(paper_id),
                        "paper_id": str(paper_id),
                        "paper_title": str(paper.get("title", "") or "") or None,
                        "artifact_path": str(paper.get("entry_url") or paper.get("entry_id") or "") or None,
                        "citation_text": str(paper.get("title", "") or paper_id),
                        "snippet": str(paper.get("abstract", "") or "")[:360] or None,
                        "provenance": {
                            "outline_path": outline.get("path"),
                            "preview_path": preview_path,
                            "source_run_id": run_id,
                            "section_provenance": provenance_entries,
                        },
                    }
                )

    if not items:
        warnings.append("No citation trace is available because the current outline has no claims or paper sections.")

    return {
        "run_id": run_id,
        "generated_at": iso_now_utc(),
        "source_outline_path": outline.get("path"),
        "source_preview_path": preview_path,
        "items": items,
        "warnings": warnings,
    }


def _review_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [str(value).strip()]
    return []


def _review_status_note(status: str) -> str:
    if status == "abstract_only":
        return "초록만 검토"
    if status == "full_text_available":
        return "로컬 전문 파일은 있으나 이번 보고서에서 본문 근거로 사용하지 않음"
    if status == "reviewed":
        return "전문/리뷰 검토"
    return "원문 확인 불가"


def _review_artifact_display(review: dict[str, Any]) -> str:
    value = str(review.get("full_text_artifact_path") or "").strip()
    if not value:
        return "-"
    return display_artifact_reference(value, run_id=str(review.get("run_id") or "") or None).replace("|", "\\|")


_REPORT_KEYWORD_STOPWORDS = frozenset(
    {
        "about",
        "abstract",
        "analysis",
        "based",
        "describes",
        "evidence",
        "findings",
        "from",
        "into",
        "method",
        "paper",
        "report",
        "research",
        "result",
        "results",
        "study",
        "that",
        "their",
        "this",
        "using",
        "with",
        "기반으로",
        "논문",
        "방법",
        "연구",
        "요약했다",
        "평가",
    }
)


def _top_report_keywords(reviews_payload: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for review in reviews_payload:
        blob = " ".join(
            [
                str(review.get("title") or ""),
                str(review.get("abstract_text") or ""),
                str(review.get("method_summary") or ""),
                str(review.get("why_it_matters") or ""),
                " ".join(_review_list(review.get("key_findings"))),
            ]
        )
        observed: set[str] = set()
        for match in re.finditer(r"[^\W_]{3,}(?:-[^\W_]{2,})?", blob, flags=re.UNICODE):
            token = match.group(0)
            key = token.casefold()
            if key in _REPORT_KEYWORD_STOPWORDS or key.isdigit():
                continue
            observed.add(key)
            display.setdefault(key, token)
        for key in observed:
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ["저장된 리뷰에서 반복 키워드가 충분히 추출되지 않았습니다."]
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [
        f"{research_domain.markdown_inline(display[key])}: 관련 리뷰 {count}편"
        for key, count in ranked
    ]


def _review_identity(review: dict[str, Any]) -> str:
    paper_id = str(review.get("paper_id") or "").strip().lower()
    if paper_id:
        return f"paper:{paper_id}"
    title = re.sub(r"\s+", " ", str(review.get("title") or "").strip().lower())
    return f"title:{title}" if title else f"review:{id(review)}"


def _review_rank_key(review: dict[str, Any]) -> tuple[float, float, str, str]:
    return (
        float(review.get("relevance_score") or 0),
        float(review.get("evidence_strength") or 0),
        str(review.get("published_at") or review.get("published") or ""),
        str(review.get("reviewed_at") or ""),
    )


def _dedupe_reviews(reviews_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for review in reviews_payload:
        key = _review_identity(review)
        existing = selected.get(key)
        if existing is None or _review_rank_key(review) > _review_rank_key(existing):
            selected[key] = review
    return list(selected.values())


def _report_truthful_reviews(reviews_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    truthful: list[dict[str, Any]] = []
    for review in reviews_payload:
        if review.get("full_text_status") != "reviewed":
            truthful.append(review)
            continue
        from app.services import paper_review as paper_review_service

        validation = paper_review_service.validate_reviewed_artifact_content(review)
        if validation.get("valid") is True:
            truthful.append(review)
            continue
        demoted = dict(review)
        demoted["full_text_status"] = "unavailable"
        demoted["review_source_type"] = "unverified_full_text"
        demoted["full_text_validation_reason"] = validation.get("reason") or "unverified_full_text"
        demoted["evidence_spans"] = []
        truthful.append(demoted)
    return truthful


def build_weekly_journal_markdown(
    *,
    topic_profile: dict[str, Any],
    reviews: list[dict[str, Any]],
    generated_at: str,
    topic_relevant_unique_count: int | None = None,
    audit_summary: dict[str, Any] | None = None,
    ai_drafts: dict[str, dict[str, Any]] | None = None,
    recommendation_followup_execution: dict[str, Any] | None = None,
    recommendation_quality_comparison: dict[str, Any] | None = None,
    domain_context: research_domain.ResearchDomainContext | None = None,
    experiment_status: dict[str, Any] | None = None,
) -> str:
    resolved_domain_context = domain_context or research_domain.build_research_domain_context(
        profile=topic_profile or None
    )
    topic_title = research_domain.markdown_inline(
        resolved_domain_context.display_name
    )
    reviews = _report_truthful_reviews(_dedupe_reviews(reviews))
    ranked = sorted(
        reviews,
        key=lambda review: (
            float(review.get("relevance_score") or 0),
            float(review.get("evidence_strength") or 0),
            str(review.get("published_at") or ""),
        ),
        reverse=True,
    )
    top_reviews = ranked[:5]
    abstract_only = sum(1 for review in ranked if review.get("full_text_status") == "abstract_only")
    full_text_available = sum(1 for review in ranked if review.get("full_text_status") == "full_text_available")
    full_text_reviewed = sum(1 for review in ranked if review.get("full_text_status") == "reviewed")
    ai_drafts = ai_drafts or {}
    resolved_topic_id = str(
        topic_profile.get("profile_id")
        or topic_profile.get("topic_id")
        or (ranked[0].get("topic_id") if ranked else "")
        or "legacy-neutral"
    )
    ai_grounded_count = sum(
        1
        for review in ranked
        if review.get("full_text_status") == "reviewed" and _ai_draft_for_review(review, ai_drafts) is not None
    )
    provider_generated_count = sum(
        1
        for review in ranked
        if _ai_provider_metadata(_ai_draft_for_review(review, ai_drafts)).get("generated")
    )
    provider_coverage = provider_review_coverage.build_provider_review_coverage_summary(
        resolved_topic_id,
        full_text_reviewed_denominator=full_text_reviewed,
        reviews=ranked,
        all_drafts=list(ai_drafts.values()),
        accepted_by_review_key={key: value for key, value in ai_drafts.items() if value.get("status") == "accepted"},
    )
    avg_relevance = round(sum(float(review.get("relevance_score") or 0) for review in ranked) / len(ranked), 1) if ranked else 0.0

    lines: list[str] = []
    lines.append(f"# {topic_title} 주간 연구 저널")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- 생성 시각: {generated_at}")
    lines.append(f"- 검토 논문: {len(ranked)}편")
    lines.append(f"- 평균 relevance score: {avg_relevance}")
    lines.append(
        f"- 검토 범위: abstract_only {abstract_only}편, full_text_available {full_text_available}편, full_text_reviewed {full_text_reviewed}편"
    )
    lines.append(
        f"- 보고서 입력 기준 full_text_reviewed {full_text_reviewed}/{len(ranked)}편; 전체 대시보드 커버리지 분모는 topic-relevant unique paper 기준입니다."
    )
    lines.append(f"- AI-grounded review coverage is {ai_grounded_count} / {full_text_reviewed} full-text reviewed papers.")
    lines.append(f"- Provider-generated accepted AI drafts: {provider_generated_count}")
    lines.append(
        "- Provider-orchestrated accepted draft coverage is "
        f"{provider_coverage['providerOrchestratedAcceptedCount']} / {full_text_reviewed} full-text reviewed papers."
    )
    lines.append(f"- Synthetic provider accepted drafts: {provider_coverage['fixtureSyntheticCount']}")
    lines.append(f"- Operator-accepted real provider accepted drafts: {provider_coverage['realProviderAcceptedCount']}")
    if provider_coverage.get("coverageWarning"):
        lines.append(f"- Provider coverage note: {provider_coverage['coverageWarning']}")
    lines.append("- full-text를 검토했다고 가정하지 않습니다. `abstract_only` 항목은 초록만 검토한 것으로 표시합니다.")
    if len(ranked) < 5:
        lines.append("")
        lines.append("## Evidence Sufficiency")
        lines.append("")
        if ranked:
            lines.append(
                f"- 고유 리뷰 논문 {len(ranked)}편만 확인되어 이번 보고서는 제한된 근거 범위의 baseline입니다."
            )
            lines.append("- 중복 run row는 반복 표시하지 않았고, 검토되지 않은 논문은 주목 논문으로 승격하지 않았습니다.")
        else:
            lines.append(
                "- 검토된 논문이 없어 trend, finding, 또는 연구 공백을 substantive result로 단정하지 않았습니다."
            )
    lines.append("")
    lines.append("## 이번 주 핵심 연구 흐름")
    lines.append("")
    if ranked:
        for keyword in _top_report_keywords(ranked)[:5]:
            lines.append(f"- {keyword}")
    else:
        lines.append("- 저장된 paper review가 없어 흐름을 산출하지 못했습니다.")
    lines.append("")
    if len(top_reviews) >= 5:
        lines.append("## 주목할 논문 Top 5")
    else:
        lines.append(f"## 주목할 논문 {len(top_reviews)}/5")
    lines.append("")
    if not top_reviews:
        lines.append("- 리뷰된 논문이 없습니다.")
    for index, review in enumerate(top_reviews, start=1):
        authors = ", ".join(_review_list(review.get("authors"))[:4]) or "저자 정보 없음"
        published = review.get("published_at") or review.get("published") or "발행일 미상"
        findings = "; ".join(_review_list(review.get("key_findings"))[:2]) or "핵심 발견 미기록"
        limitations = "; ".join(_review_list(review.get("limitations"))[:2]) or "한계 미기록"
        lines.append(f"{index}. **{review.get('title') or 'Untitled paper'}**")
        lines.append(f"   - authors: {authors}")
        lines.append(f"   - published date: {published}")
        lines.append(f"   - relevance score: {float(review.get('relevance_score') or 0):.1f}")
        lines.append(f"   - key findings: {findings}")
        lines.append(f"   - limitations: {limitations}")
        lines.append(f"   - why it matters: {review.get('why_it_matters') or '미기록'}")
        lines.append(f"   - evidence source: {review.get('source_provider') or 'unknown'} / {_review_status_note(str(review.get('full_text_status') or 'unavailable'))}")
        lines.append(f"   - full_text_status: {review.get('full_text_status') or 'unavailable'}")
        ai_draft = _ai_draft_for_review(review, ai_drafts)
        if ai_draft is not None:
            draft = ai_draft.get("draft") or {}
            cited_ids = ", ".join(str(value) for value in draft.get("cited_evidence_span_ids") or []) or "-"
            lines.append(f"   - AI review: validated {draft.get('review_level') or ai_draft.get('review_level')} / source_text_sha256: {ai_draft.get('source_text_sha256')}")
            provider_meta = _ai_provider_metadata(ai_draft)
            if provider_meta:
                lines.append(f"   - {_ai_provider_label(provider_meta)}")
            lines.append(f"   - AI contribution summary: {draft.get('contribution_summary') or '미기록'}")
            lines.append(f"   - AI evidence summary: {draft.get('evidence_summary') or '미기록'}")
            lines.append(f"   - AI cited evidence span IDs: {cited_ids}")
        spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
        if review.get("full_text_status") == "reviewed" and spans:
            lines.append("   - evidence snippets:")
            for span in spans[:2]:
                snippet = str(span.get("text") or "").strip()
                if not snippet:
                    continue
                page = span.get("page_number")
                page_label = f"p.{page}" if page else "local text"
                lines.append(f"     - {page_label}: {snippet}")
        elif review.get("full_text_status") == "abstract_only":
            lines.append("   - 본문 근거: 초록만 검토")
        elif review.get("full_text_status") == "full_text_available":
            lines.append("   - 본문 근거: 로컬 전문 파일은 있으나 이번 보고서에서 본문 근거로 사용하지 않음")
    lines.append("")
    lines.append("## 방법론/데이터셋/평가 동향")
    lines.append("")
    for review in top_reviews:
        method = review.get("method_summary") or "방법론 요약 미기록"
        lines.append(f"- {review.get('title')}: {method}")
    if not top_reviews:
        lines.append("- 방법론 신호가 없습니다.")
    lines.append("")
    lines.append("## 한계와 리스크")
    lines.append("")
    if ranked:
        for review in top_reviews:
            for limitation in _review_list(review.get("limitations"))[:1]:
                lines.append(f"- {review.get('title')}: {limitation}")
    else:
        lines.append("- 리뷰 데이터가 없어 리스크를 산출하지 못했습니다.")
    lines.append("")
    lines.append("## 다음 주 모니터링 포인트")
    lines.append("")
    lines.append("- relevance score가 높지만 `abstract_only`인 논문은 전문 확보 여부를 확인합니다.")
    if ranked:
        lines.append(
            f"- {topic_title} 범위에서 이번 evidence에 반복된 방법·데이터·평가 신호를 다음 수집분과 비교합니다."
        )
    else:
        lines.append(
            f"- {topic_title} 범위의 추가 문헌을 수집한 뒤 monitoring priority를 다시 결정합니다."
        )
    lines.append("- 보고서에 포함된 모든 근거의 source/provider와 검토 범위를 유지합니다.")
    if experiment_status is not None:
        lines.extend(["", "## Proposed Experiment Scaffold (Not Executed)", ""])
        lines.append(
            "- This is a planning artifact, not empirical evidence or an experiment result."
        )
        lines.append(
            f"- artifact_kind: `{experiment_status.get('artifact_kind') or 'unknown_experiment_artifact'}`"
        )
        lines.append(
            f"- lifecycle_state: `{experiment_status.get('lifecycle_state') or 'unknown'}`"
        )
        lines.append(
            f"- execution_status: `{experiment_status.get('execution_status') or 'unknown'}`"
        )
        lines.append("- executable: `false`")
        lines.append("- runner_available: `false`")
        unresolved = experiment_status.get("unresolved_items")
        if isinstance(unresolved, list) and unresolved:
            lines.append("- Required inputs remain unresolved:")
            for item in unresolved:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"  - `{item.get('code') or 'unspecified'}`: "
                    f"{research_domain.markdown_inline(item.get('label') or '')}"
                )
        elif experiment_status.get("lifecycle_state") == "unknown_legacy":
            lines.append(
                "- Legacy execution history is unknown and is not treated as an executed result."
            )
        elif experiment_status.get("lifecycle_state") == "not_generated":
            lines.append("- No experiment proposal scaffold was generated for this run.")
    lines.extend(
        _research_recommendations_section(
            topic_profile=topic_profile,
            topic_relevant_unique_count=topic_relevant_unique_count or len(ranked),
        )
    )
    lines.extend(
        _recommendation_followup_execution_section(
            recommendation_followup_execution,
            recommendation_quality_comparison,
        )
    )
    lines.append("")
    lines.append("## 참고 논문 / evidence table")
    lines.append("")
    lines.append("| paper_id | title | evidence source | full_text_status | relevance_score | full_text_artifact_path | source_text_sha256 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for review in ranked:
        title = str(review.get("title") or "").replace("|", "\\|")
        evidence_source = str(review.get("source_provider") or "unknown")
        status = str(review.get("full_text_status") or "unavailable")
        artifact_path = _review_artifact_display(review)
        source_hash = str(review.get("source_text_sha256") or "-") if status == "reviewed" else "-"
        lines.append(
            f"| {review.get('paper_id') or ''} | {title} | {evidence_source} / {_review_status_note(status)} | {status} | {float(review.get('relevance_score') or 0):.1f} | {artifact_path} | {source_hash} |"
        )
    lines.extend(
        _weekly_journal_evidence_appendix(
            ranked,
            topic_relevant_unique_count=topic_relevant_unique_count,
            audit_summary=audit_summary,
            ai_drafts=ai_drafts,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _research_recommendations_section(
    *,
    topic_profile: dict[str, Any],
    topic_relevant_unique_count: int,
) -> list[str]:
    topic_id = str(topic_profile.get("profile_id") or "").strip()
    if not topic_id:
        return []
    try:
        payload = recommendations.build_recommendations_for_topic(
            topic_id,
            limit=3,
            include_abstract_only=True,
            topic_relevant_unique_count=topic_relevant_unique_count,
        )
    except Exception:
        return [
            "",
            "## Research Gaps & New Topic Candidates",
            "",
            "- preliminary recommendation generation was unavailable for this report.",
        ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    gaps = payload.get("gap_candidates") if isinstance(payload.get("gap_candidates"), list) else []
    topics = payload.get("new_topic_candidates") if isinstance(payload.get("new_topic_candidates"), list) else []
    audit = recommendations.audit_recommendation_quality(payload)
    try:
        followup = recommendations.plan_recommendation_followup(topic_id, payload, limit=3, candidate_rows=[])
    except Exception:
        followup = {"plans": [], "summary": {}}
    audit_by_id = {
        str(item.get("recommendation_id")): item
        for item in audit.get("items", [])
        if isinstance(item, dict)
    }
    followup_by_id = {
        str(item.get("recommendation_id")): item
        for item in followup.get("plans", [])
        if isinstance(item, dict)
    }
    lines = [
        "",
        "## Research Gaps & New Topic Candidates",
        "",
        "- preliminary baseline: recommendations are evidence-linked and not exhaustive.",
        f"- full-text reviewed coverage is {summary.get('full_text_reviewed_count', 0)} / {summary.get('topic_relevant_unique_count', topic_relevant_unique_count)} topic-relevant unique papers.",
        f"- AI-grounded accepted draft coverage is {summary.get('ai_grounded_accepted_count', 0)} / {summary.get('full_text_reviewed_count', 0)} full-text reviewed papers.",
    ]
    warning = payload.get("coverage_warning")
    if warning:
        lines.append(f"- coverage warning: {warning}")
    lines.extend(["", "### Candidate Research Gaps", ""])
    if not gaps:
        lines.append("- No evidence-linked research gaps could be generated from current reviews.")
    for gap in gaps:
        refs = gap.get("supporting_evidence_refs") if isinstance(gap.get("supporting_evidence_refs"), list) else []
        paper_ids = ", ".join(str(value) for value in gap.get("supporting_paper_ids") or []) or "-"
        audit_item = audit_by_id.get(str(gap.get("gap_id") or ""))
        followup_item = followup_by_id.get(str(gap.get("gap_id") or ""))
        lines.append(f"- **{gap.get('title') or gap.get('gap_id')}** ({gap.get('confidence')} / {gap.get('confidence_score')})")
        lines.append(f"  - statement: {gap.get('gap_statement')}")
        lines.append(f"  - supporting paper IDs: {paper_ids}")
        lines.append(f"  - source basis: {gap.get('source_basis')}")
        if audit_item:
            lines.append(
                "  - evidence strength: "
                f"{audit_item.get('full_text_support_count', 0)} full-text, "
                f"{audit_item.get('ai_grounded_support_count', 0)} AI-grounded, "
                f"{audit_item.get('abstract_only_support_count', 0)} abstract-only; "
                f"needs_follow_up={audit_item.get('needs_follow_up')}"
            )
            downgrade_reasons = ", ".join(str(value) for value in audit_item.get("downgrade_reason_codes") or []) or "-"
            lines.append(f"  - audit reason codes: {downgrade_reasons}")
        if followup_item:
            candidates = followup_item.get("candidates") if isinstance(followup_item.get("candidates"), list) else []
            if candidates:
                top = candidates[0]
                status = top.get("current_full_text_status") or "unavailable"
                lines.append(
                    "  - follow-up acquisition suggestion: "
                    f"{top.get('paper_id')} ({top.get('title')}) is not yet reviewed as full text "
                    f"(current status: {status}); planned evidence impact={top.get('expected_confidence_impact')}."
                )
            else:
                lines.append(
                    "  - follow-up acquisition suggestion: review more topic-relevant papers; "
                    "no local candidate that is not yet reviewed was selected in this report run."
                )
        for ref in refs[:3]:
            lines.append(
                "  - evidence ref: "
                f"{ref.get('paper_id')} / {ref.get('review_level')} / "
                f"sha256={ref.get('source_text_sha256') or '-'} / span={ref.get('evidence_span_id') or '-'}"
            )
    lines.extend(["", "### New Topic Candidates", ""])
    if not topics:
        lines.append("- No evidence-linked new topic candidates could be generated from current reviews.")
    for topic in topics:
        paper_ids = ", ".join(str(value) for value in topic.get("supporting_paper_ids") or []) or "-"
        audit_item = audit_by_id.get(str(topic.get("topic_candidate_id") or ""))
        followup_item = followup_by_id.get(str(topic.get("topic_candidate_id") or ""))
        lines.append(f"- **{topic.get('title') or topic.get('topic_candidate_id')}** ({topic.get('confidence')} / {topic.get('confidence_score')})")
        lines.append(f"  - research question: {topic.get('proposed_research_question')}")
        lines.append(f"  - supporting paper IDs: {paper_ids}")
        if audit_item:
            lines.append(
                "  - evidence strength: "
                f"{audit_item.get('full_text_support_count', 0)} full-text, "
                f"{audit_item.get('ai_grounded_support_count', 0)} AI-grounded, "
                f"{audit_item.get('abstract_only_support_count', 0)} abstract-only; "
                f"needs_follow_up={audit_item.get('needs_follow_up')}"
            )
        if followup_item:
            candidates = followup_item.get("candidates") if isinstance(followup_item.get("candidates"), list) else []
            if candidates:
                top = candidates[0]
                lines.append(
                    "  - follow-up acquisition suggestion: "
                    f"{top.get('paper_id')} is a not yet reviewed follow-up candidate; "
                    f"impact={top.get('expected_confidence_impact')}."
                )
            else:
                lines.append("  - follow-up acquisition suggestion: no local candidate that is not yet reviewed was selected in this report run.")
        lines.append(f"  - required follow-up evidence: {'; '.join(str(value) for value in topic.get('required_follow_up_evidence') or [])}")
    return lines


def _recommendation_followup_execution_section(
    execution: dict[str, Any] | None,
    comparison: dict[str, Any] | None,
) -> list[str]:
    if not execution:
        return []
    summary = execution.get("summary") if isinstance(execution.get("summary"), dict) else {}
    actions = execution.get("actions") if isinstance(execution.get("actions"), list) else []
    lines = [
        "",
        "## Recommendation Follow-up Execution",
        "",
        "- follow-up execution is explicit, bounded, and not automatic.",
        f"- planned follow-up candidates: {int(summary.get('planned_followup_count') or 0)}",
        f"- executed downloads: {int(summary.get('downloaded_count') or 0)}",
        f"- extracted PDFs/text artifacts: {int(summary.get('extracted_count') or 0)}",
        f"- newly full-text reviewed papers: {int(summary.get('reviewed_from_followup_count') or 0)}",
        f"- skipped existing reviewed: {int(summary.get('skipped_existing_reviewed_count') or 0)}",
        f"- skipped missing PDF URL: {int(summary.get('skipped_no_pdf_url_count') or 0)}",
        f"- skipped/blocked: {int(summary.get('skipped_blocked_count') or 0)}",
        "- skipped candidates are not counted as reviewed evidence.",
    ]
    rejected = summary.get("rejected_count_by_reason") if isinstance(summary.get("rejected_count_by_reason"), dict) else {}
    failed = summary.get("failed_count_by_reason") if isinstance(summary.get("failed_count_by_reason"), dict) else {}
    if rejected:
        lines.append(f"- rejected_count_by_reason: {json.dumps(rejected, ensure_ascii=False, sort_keys=True)}")
    if failed:
        lines.append(f"- failed_count_by_reason: {json.dumps(failed, ensure_ascii=False, sort_keys=True)}")
    lines.extend(["", "### Follow-up action audit", ""])
    if not actions:
        lines.append("- No follow-up execution actions were attached.")
    for action in actions[:8]:
        reason_codes = ", ".join(str(value) for value in action.get("reason_codes") or []) or "-"
        source_hash = str(action.get("source_text_sha256") or "-")
        lines.append(
            f"- {action.get('paper_id')}: {action.get('planned_action')} / reasons={reason_codes} / sha256={source_hash}"
        )
    if comparison:
        before_dist = comparison.get("confidence_distribution_before") if isinstance(comparison.get("confidence_distribution_before"), dict) else {}
        after_dist = comparison.get("confidence_distribution_after") if isinstance(comparison.get("confidence_distribution_after"), dict) else {}
        lines.extend(
            [
                "",
                "### Recommendation quality before/after",
                "",
                f"- full-text reviewed before/after: {int(comparison.get('full_text_reviewed_count_before') or 0)} / {int(comparison.get('full_text_reviewed_count_after') or 0)}",
                f"- weak recommendations before/after: {int(comparison.get('weak_recommendation_count_before') or 0)} / {int(comparison.get('weak_recommendation_count_after') or 0)}",
                f"- needs-follow-up before/after: {int(comparison.get('needs_follow_up_count_before') or 0)} / {int(comparison.get('needs_follow_up_count_after') or 0)}",
                f"- confidence distribution before: {json.dumps(before_dist, ensure_ascii=False, sort_keys=True)}",
                f"- confidence distribution after: {json.dumps(after_dist, ensure_ascii=False, sort_keys=True)}",
            ]
        )
        if not comparison.get("new_reviews_created"):
            lines.append("- no new full-text reviews were created, so recommendation confidence was not increased.")
    return lines


def _weekly_journal_evidence_appendix(
    reviews: list[dict[str, Any]],
    *,
    topic_relevant_unique_count: int | None,
    audit_summary: dict[str, Any] | None,
    ai_drafts: dict[str, dict[str, Any]],
) -> list[str]:
    full_text_reviews = [review for review in reviews if review.get("full_text_status") == "reviewed"]
    abstract_reviews = [review for review in reviews if review.get("full_text_status") == "abstract_only"]
    available_reviews = [review for review in reviews if review.get("full_text_status") == "full_text_available"]
    denominator = int(topic_relevant_unique_count or len(reviews) or 0)
    lines = [
        "",
        "## Evidence Appendix",
        "",
        f"- full-text reviewed coverage is {len(full_text_reviews)} / {denominator} topic-relevant unique papers.",
        "- `reviewed` 항목만 로컬 전문 evidence snippets와 source_text_sha256를 포함합니다.",
        "",
        "### Full-text reviewed papers",
        "",
        "| paper_id | title | source_text_sha256 | text_char_count | evidence_span_count | full_text_artifact_path |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if full_text_reviews:
        for review in full_text_reviews:
            spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
            title = str(review.get("title") or "").replace("|", "\\|")
            artifact = _review_artifact_display(review)
            lines.append(
                f"| {review.get('paper_id') or ''} | {title} | {review.get('source_text_sha256') or '-'} | {int(review.get('text_char_count') or 0)} | {len(spans)} | {artifact} |"
            )
    else:
        lines.append("| - | - | - | 0 | 0 | - |")
    lines.extend(
        [
            "",
            "### Abstract-only reviewed papers",
            "",
            "| paper_id | title | relevance_score | status note |",
            "| --- | --- | --- | --- |",
        ]
    )
    if abstract_reviews:
        for review in abstract_reviews:
            title = str(review.get("title") or "").replace("|", "\\|")
            lines.append(f"| {review.get('paper_id') or ''} | {title} | {float(review.get('relevance_score') or 0):.1f} | 초록만 검토 |")
    else:
        lines.append("| - | - | 0.0 | 없음 |")
    lines.extend(
        [
            "",
            "### Full-text available but not reviewed",
            "",
            "| paper_id | title | artifact_path | status note |",
            "| --- | --- | --- | --- |",
        ]
    )
    if available_reviews:
        for review in available_reviews:
            title = str(review.get("title") or "").replace("|", "\\|")
            artifact = _review_artifact_display(review)
            lines.append(f"| {review.get('paper_id') or ''} | {title} | {artifact} | 로컬 전문 파일은 있으나 본문 근거로 사용하지 않음 |")
    else:
        lines.append("| - | - | - | 없음 |")
    grounded_ai = [
        (review, _ai_draft_for_review(review, ai_drafts))
        for review in reviews
        if _ai_draft_for_review(review, ai_drafts) is not None
    ]
    lines.extend(
        [
            "",
            "### Validated AI review drafts",
            "",
            "| paper_id | review_level | provider_provenance | source_text_sha256 | cited_evidence_span_ids | confidence |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    if grounded_ai:
        for review, ai_draft in grounded_ai:
            draft = (ai_draft or {}).get("draft") or {}
            cited = ", ".join(str(value) for value in draft.get("cited_evidence_span_ids") or []) or "-"
            confidence = draft.get("confidence")
            provenance = provider_review_coverage.origin_bucket(ai_draft)
            lines.append(
                f"| {review.get('paper_id') or ''} | {draft.get('review_level') or ai_draft.get('review_level')} | {provenance} | {ai_draft.get('source_text_sha256') or '-'} | {cited} | {confidence if confidence is not None else '-'} |"
            )
    else:
        lines.append("| - | - | - | - | - | - |")
    lines.extend(["", "### Acquisition/extraction audit summary", ""])
    if audit_summary:
        for key in (
            "candidate_count",
            "planned_count",
            "downloaded_count",
            "rejected_count",
            "failed_count",
            "extracted_count",
            "extraction_failed_count",
            "full_text_reviewed_count",
            "no_evidence_span_count",
            "weak_evidence_span_count",
        ):
            if key in audit_summary:
                lines.append(f"- {key}: {audit_summary.get(key)}")
        reason_counts = audit_summary.get("rejected_by_reason") if isinstance(audit_summary.get("rejected_by_reason"), dict) else {}
        if reason_counts:
            lines.append(f"- rejected_by_reason: {json.dumps(reason_counts, ensure_ascii=False, sort_keys=True)}")
    else:
        lines.append("- refresh audit summary was not attached to this generated report.")
    return lines


def _topic_relevant_review_denominator(
    topic_profile: dict[str, Any],
    topic_id: str,
    fallback: int,
    *,
    read_only: bool = True,
) -> int:
    try:
        from app.services import paper_review as paper_review_service

        coverage = paper_review_service.review_coverage_for_rows(
            topic_profile,
            paper_review_service._topic_rows(topic_profile),  # Local report denominator should match dashboard semantics.
            read_only=read_only,
        )
        return int(coverage.get("topicRelevantUniqueCount") or fallback)
    except Exception:
        return fallback


def _ai_draft_key(review: dict[str, Any]) -> str:
    return f"{review.get('paper_id') or ''}::{review.get('source_text_sha256') or ''}"


def _ai_draft_for_review(review: dict[str, Any], ai_drafts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if review.get("full_text_status") != "reviewed":
        return None
    return ai_drafts.get(_ai_draft_key(review))


def _ai_provider_metadata(ai_draft: dict[str, Any] | None) -> dict[str, Any]:
    if not ai_draft:
        return {}
    validation = ai_draft.get("validation") if isinstance(ai_draft.get("validation"), dict) else {}
    provider = validation.get("provider") if isinstance(validation.get("provider"), dict) else {}
    return provider


def _ai_provider_label(provider_meta: dict[str, Any]) -> str:
    name = provider_meta.get("name") or "manual_import"
    mode = provider_meta.get("mode") or "manual_import"
    origin = provider_meta.get("origin")
    if not origin:
        if name == "stub" or mode == "stub":
            origin = "stub"
        elif mode == "live_command":
            origin = "live_command"
        elif mode == "dry_run":
            origin = "command_dry_run"
        elif name == "manual_import":
            origin = "manual_import"
        else:
            origin = "unknown"
    label = f"AI provider: {name} / mode: {mode} / origin: {origin}"
    run_id = provider_meta.get("provider_run_id") or provider_meta.get("run_id")
    if run_id:
        label += f" / run_id: {run_id}"
    if origin == "stub":
        label += " (stub/synthetic)"
    elif origin == "command_fixture":
        label += " (local fixture/synthetic)"
    elif origin in {"real_provider", "live_command"} and provider_meta.get("operator_accepted"):
        label += " (operator accepted real provider)"
    return label


def _accepted_ai_drafts_for_reviews(
    topic_id: str,
    reviews: list[dict[str, Any]],
    *,
    read_only: bool = True,
) -> dict[str, dict[str, Any]]:
    paper_ids = [str(review.get("paper_id") or "") for review in reviews if str(review.get("paper_id") or "")]
    if not paper_ids:
        return {}
    drafts = ai_review_drafts.list_drafts(
        topic_id=topic_id,
        paper_ids=paper_ids,
        status="accepted",
        limit=1000,
        read_only=read_only,
    )
    selected: dict[str, dict[str, Any]] = {}
    for draft in drafts:
        key = f"{draft.get('paper_id') or ''}::{draft.get('source_text_sha256') or ''}"
        if not key.endswith("::") and key not in selected:
            selected[key] = draft
    return selected


def _weekly_journal_quality_summary(
    *,
    markdown: str,
    reviews: list[dict[str, Any]],
    topic_id: str,
    evaluated_at: str,
) -> dict[str, Any]:
    """Deterministic, provider-free quality summary for a generated weekly journal report.

    Wraps :func:`report_quality.evaluate_weekly_journal_quality`, a fixed heuristic rubric
    (never an LLM/provider judgement). It is intentionally defensive: evaluating quality must
    not be able to break report generation, so any unexpected failure yields a degraded
    ``unavailable`` summary instead of raising. Provider/OAuth provenance is deliberately not
    supplied — the rubric reads the honest provenance wording already embedded in the report
    markdown, keeping the evaluation fully local and reproducible.
    """
    try:
        result = report_quality.evaluate_weekly_journal_quality(
            markdown,
            reviews=reviews,
            expected_topic=topic_id,
            evaluated_at=evaluated_at,
        )
        result["evaluation_status"] = "evaluated"
        return result
    except Exception as exc:  # pragma: no cover - defensive; report generation must not break
        return {
            "evaluation_status": "unavailable",
            "evaluation_method": report_quality.EVALUATION_METHOD,
            "rubric_version": report_quality.RUBRIC_VERSION,
            "report_kind": "weekly_journal",
            "topic": topic_id,
            "checked_at": evaluated_at,
            "error": str(exc),
        }


def generate_weekly_journal_report_from_reviews(
    *,
    topic_profile: dict[str, Any],
    topic_id: str,
    run_id: str | None = None,
    generated_at: str | None = None,
    audit_summary: dict[str, Any] | None = None,
    topic_relevant_unique_count: int | None = None,
    recommendation_followup_execution: dict[str, Any] | None = None,
    recommendation_quality_comparison: dict[str, Any] | None = None,
    read_only: bool = True,
) -> dict[str, Any]:
    run_record: dict[str, Any] | None = None
    if run_id:
        try:
            run_record = load_run_record(run_id)
            domain_context = research_domain.research_domain_context_for_run(run_record)
        except FileNotFoundError:
            domain_context = research_domain.build_research_domain_context(
                profile=topic_profile or None
            )
    else:
        domain_context = research_domain.build_research_domain_context(
            profile=topic_profile or None
        )
    try:
        reviews_payload = paper_reviews.list_reviews(topic_id=topic_id, run_id=run_id, limit=100, read_only=read_only)
    except FileNotFoundError:
        if not read_only:
            raise
        reviews_payload = []
    unique_reviews = _dedupe_reviews(reviews_payload)
    denominator = topic_relevant_unique_count or _topic_relevant_review_denominator(
        topic_profile,
        topic_id,
        len(unique_reviews),
        read_only=read_only,
    )
    accepted_ai_drafts = _accepted_ai_drafts_for_reviews(topic_id, unique_reviews, read_only=read_only)
    generated_at_value = generated_at or iso_now_utc()
    markdown = build_weekly_journal_markdown(
        topic_profile=topic_profile,
        reviews=unique_reviews,
        generated_at=generated_at_value,
        topic_relevant_unique_count=denominator,
        audit_summary=audit_summary,
        ai_drafts=accepted_ai_drafts,
        recommendation_followup_execution=recommendation_followup_execution,
        recommendation_quality_comparison=recommendation_quality_comparison,
        domain_context=domain_context,
        experiment_status=(
            experiments.present_run_experiment_status(run_record)["experiment_status"]
            if run_record is not None
            else None
        ),
    )
    return {
        "status": "ok" if unique_reviews else "empty",
        "topic_id": topic_id,
        "run_id": run_id,
        "review_count": len(unique_reviews),
        "raw_review_count": len(reviews_payload),
        "markdown": markdown,
        "quality": _weekly_journal_quality_summary(
            markdown=markdown,
            reviews=unique_reviews,
            topic_id=topic_id,
            evaluated_at=generated_at_value,
        ),
    }

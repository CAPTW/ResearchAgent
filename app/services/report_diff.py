from __future__ import annotations

import difflib
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.domain.models import ReportArtifactFallback, ReportDocumentSnapshot, ReportSectionSnapshot
from app.repositories.files import read_text
from app.repositories.runs import build_run_paths, load_run_record
from app.services import exports, reporting

ARXIV_ID_PATTERN = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b")
URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")
REFERENCE_PATTERN = re.compile(r"\[\d{1,3}\]")


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text or "section"


def _trim_excerpt(text: str, limit: int = 280) -> str | None:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _hash_text(text: str) -> str | None:
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _extract_identifiers(text: str, paper_ids: list[str] | tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    citations = list(ARXIV_ID_PATTERN.findall(text))
    citations.extend([paper_id for paper_id in paper_ids if paper_id])
    references = [match.group(0) for match in URL_PATTERN.finditer(text)]
    references.extend(match.group(0) for match in REFERENCE_PATTERN.finditer(text))
    return tuple(citations), tuple(references)


def _build_section_snapshot(
    *,
    section_key: str,
    title: str,
    order: int,
    body: str,
    paper_ids: list[str] | tuple[str, ...],
) -> ReportSectionSnapshot:
    citations, references = _extract_identifiers(body, paper_ids)
    return ReportSectionSnapshot(
        section_key=section_key,
        title=title,
        order=order,
        body=body,
        excerpt=_trim_excerpt(body),
        body_hash=_hash_text(body),
        paper_ids=tuple(paper_ids),
        citations=citations,
        references=references,
    )


def _sections_from_markdown(content: str) -> tuple[ReportSectionSnapshot, ...]:
    parsed_sections = reporting._parse_report_sections(content)
    sections: list[ReportSectionSnapshot] = []
    for index, (title, body) in enumerate(parsed_sections.items(), start=1):
        display_title = "Executive Summary" if title == "_intro" else title
        section_key = "executive-summary" if title == "_intro" else _slug(display_title)
        sections.append(
            _build_section_snapshot(
                section_key=section_key,
                title=display_title,
                order=index,
                body=body,
                paper_ids=[],
            )
        )
    return tuple(sections)


def _sections_from_outline(run_id: str, outline: dict[str, Any]) -> tuple[ReportSectionSnapshot, ...]:
    sections: list[ReportSectionSnapshot] = []
    for index, section in enumerate(outline.get("sections", []) or [], start=1):
        sections.append(
            _build_section_snapshot(
                section_key=str(section.get("section_id", "")).strip() or f"section-{index}",
                title=str(section.get("title", "")).strip() or f"Section {index}",
                order=index,
                body=str(section.get("body_markdown", "") or ""),
                paper_ids=[str(paper_id) for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
            )
        )
    return tuple(sections)


def _sections_from_outline_summary(summary: list[dict[str, Any]]) -> tuple[ReportSectionSnapshot, ...]:
    sections: list[ReportSectionSnapshot] = []
    for index, section in enumerate(summary, start=1):
        sections.append(
            _build_section_snapshot(
                section_key=str(section.get("section_id", "")).strip() or f"section-{index}",
                title=str(section.get("title", "")).strip() or f"Section {index}",
                order=index,
                body="",
                paper_ids=[str(paper_id) for paper_id in section.get("paper_ids", []) if str(paper_id).strip()],
            )
        )
    return tuple(sections)


def _record_fallback(
    items: list[ReportArtifactFallback],
    *,
    run_side: str,
    artifact_kind: str,
    available: bool,
    used: bool = False,
    path: str | None = None,
    reason: str | None = None,
) -> None:
    items.append(
        ReportArtifactFallback(
            run_side=run_side,
            artifact_kind=artifact_kind,
            available=available,
            used=used,
            path=path,
            reason=reason,
        )
    )


def _latest_exported_report_markdown(run_id: str) -> Path | None:
    manifest = exports.list_exports(run_id).get("items", [])
    for item in reversed(manifest):
        if item.get("format") != "report_markdown" or item.get("status") != "ready":
            continue
        candidate = Path(str(item.get("output", "")))
        if candidate.is_file():
            return candidate
    return None


def _report_markdown_path(run_id: str, run: dict[str, Any]) -> Path | None:
    report_path = Path(str(run.get("artifacts", {}).get("report_markdown") or ""))
    if report_path.is_file():
        return report_path
    candidates = reporting._report_markdown_candidates(run_id)
    if candidates:
        return candidates[-1]
    return None


def _snapshot_from_artifacts(run_id: str, *, run_side: str) -> tuple[ReportDocumentSnapshot, list[ReportArtifactFallback]]:
    run = load_run_record(run_id)
    fallbacks: list[ReportArtifactFallback] = []

    preview_path = Path(str(run.get("artifacts", {}).get("preview_markdown") or build_run_paths(run_id).reports_dir / reporting.PREVIEW_FILENAME))
    if preview_path.is_file():
        content = read_text(preview_path)
        _record_fallback(
            fallbacks,
            run_side=run_side,
            artifact_kind="preview_artifact",
            available=True,
            used=True,
            path=str(preview_path),
        )
        return (
            ReportDocumentSnapshot(
                run_id=run_id,
                source="preview_artifact",
                completeness="full",
                confidence=1.0,
                path=str(preview_path),
                content_hash=_hash_text(content),
                sections=_sections_from_markdown(content),
            ),
            fallbacks,
        )
    _record_fallback(
        fallbacks,
        run_side=run_side,
        artifact_kind="preview_artifact",
        available=False,
        reason="Preview artifact not found.",
    )

    exported_report_path = _latest_exported_report_markdown(run_id)
    if exported_report_path is not None:
        content = read_text(exported_report_path)
        _record_fallback(
            fallbacks,
            run_side=run_side,
            artifact_kind="exported_report_markdown",
            available=True,
            used=True,
            path=str(exported_report_path),
        )
        return (
            ReportDocumentSnapshot(
                run_id=run_id,
                source="exported_report_markdown",
                completeness="full",
                confidence=0.96,
                path=str(exported_report_path),
                content_hash=_hash_text(content),
                sections=_sections_from_markdown(content),
            ),
            fallbacks,
        )
    _record_fallback(
        fallbacks,
        run_side=run_side,
        artifact_kind="exported_report_markdown",
        available=False,
        reason="No exported report markdown artifact found.",
    )

    report_markdown_path = _report_markdown_path(run_id, run)
    if report_markdown_path is not None:
        content = read_text(report_markdown_path)
        _record_fallback(
            fallbacks,
            run_side=run_side,
            artifact_kind="report_markdown",
            available=True,
            used=True,
            path=str(report_markdown_path),
        )
        return (
            ReportDocumentSnapshot(
                run_id=run_id,
                source="report_markdown",
                completeness="full",
                confidence=0.9,
                path=str(report_markdown_path),
                content_hash=_hash_text(content),
                sections=_sections_from_markdown(content),
            ),
            fallbacks,
        )
    _record_fallback(
        fallbacks,
        run_side=run_side,
        artifact_kind="report_markdown",
        available=False,
        reason="No report markdown artifact found.",
    )

    outline = reporting.load_outline_if_present(run_id)
    if outline is not None:
        _record_fallback(
            fallbacks,
            run_side=run_side,
            artifact_kind="outline_json",
            available=True,
            used=True,
            path=str(outline.get("path") or ""),
        )
        return (
            ReportDocumentSnapshot(
                run_id=run_id,
                source="outline_json",
                completeness="outline_only",
                confidence=0.65,
                path=str(outline.get("path") or ""),
                content_hash=_hash_text("\n".join(str(section.get("body_markdown", "") or "") for section in outline.get("sections", []))),
                sections=_sections_from_outline(run_id, outline),
                missing_explanations=("Detailed report markdown is unavailable; using outline sections only.",),
            ),
            fallbacks,
        )
    _record_fallback(
        fallbacks,
        run_side=run_side,
        artifact_kind="outline_json",
        available=False,
        reason="Outline JSON not found.",
    )

    outline_summary = reporting.collect_outline_summary(run_id)
    if outline_summary:
        _record_fallback(
            fallbacks,
            run_side=run_side,
            artifact_kind="outline_summary",
            available=True,
            used=True,
            reason="Using compare-ready or parsed outline summary fallback.",
        )
        compare_ready = run.get("compare_ready", {}) or {}
        return (
            ReportDocumentSnapshot(
                run_id=run_id,
                source="outline_summary",
                completeness="outline_only",
                confidence=0.48,
                path=str(compare_ready.get("outline_path") or ""),
                content_hash=str(compare_ready.get("preview_hash") or "") or None,
                sections=_sections_from_outline_summary(outline_summary),
                missing_explanations=("Section bodies are unavailable; using outline summary only.",),
            ),
            fallbacks,
        )
    _record_fallback(
        fallbacks,
        run_side=run_side,
        artifact_kind="outline_summary",
        available=False,
        reason="No outline summary could be derived.",
    )

    preview = reporting.collect_preview_fingerprint(run_id)
    if preview.get("hash"):
        _record_fallback(
            fallbacks,
            run_side=run_side,
            artifact_kind="compare_ready_hash",
            available=True,
            used=True,
            path=str(run.get("compare_ready", {}).get("preview_path") or ""),
            reason="Only preview/report fingerprint metadata is available.",
        )
        return (
            ReportDocumentSnapshot(
                run_id=run_id,
                source="compare_ready_hash",
                completeness="metadata_only",
                confidence=0.2,
                path=str(run.get("compare_ready", {}).get("preview_path") or ""),
                content_hash=str(preview.get("hash") or ""),
                sections=(),
                missing_explanations=("Detailed report artifacts are unavailable; using compare-ready hash only.",),
            ),
            fallbacks,
        )
    _record_fallback(
        fallbacks,
        run_side=run_side,
        artifact_kind="compare_ready_hash",
        available=False,
        reason="No compare-ready hash is available.",
    )

    return (
        ReportDocumentSnapshot(
            run_id=run_id,
            source="unavailable",
            completeness="unavailable",
            confidence=0.0,
            sections=(),
            missing_explanations=("No report artifacts are available for this run.",),
        ),
        fallbacks,
    )


def load_report_snapshot(run_id: str, *, run_side: str = "left") -> tuple[ReportDocumentSnapshot, list[dict[str, Any]]]:
    snapshot, fallbacks = _snapshot_from_artifacts(run_id, run_side=run_side)
    return snapshot, [asdict(item) for item in fallbacks]


def match_report_section(
    snapshot: ReportDocumentSnapshot,
    *,
    section_id: str | None = None,
    section_title: str | None = None,
) -> ReportSectionSnapshot | None:
    normalized_section_id = (section_id or "").strip()
    normalized_section_title = (section_title or "").strip().lower()
    section_title_slug = _slug(normalized_section_title) if normalized_section_title else ""

    for section in snapshot.sections:
        if normalized_section_id and section.section_key == normalized_section_id:
            return section
        if normalized_section_title and section.title.strip().lower() == normalized_section_title:
            return section
        if section_title_slug and _slug(section.title) == section_title_slug:
            return section
    return None


def serialize_report_section(
    section: ReportSectionSnapshot,
    *,
    source_side: str,
    matched_outline_section_id: str | None = None,
    change_summary: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "section_id": section.section_key,
        "section_title": section.title,
        "source_side": source_side,
        "available": True,
        "matched_outline_section_id": matched_outline_section_id,
        "body_markdown": section.body,
        "excerpt": section.excerpt,
        "paper_ids": list(section.paper_ids),
        "change_summary": change_summary or [],
    }


def _section_similarity(left_body: str, right_body: str) -> float | None:
    if not left_body and not right_body:
        return 1.0
    if not left_body or not right_body:
        return 0.0
    return round(difflib.SequenceMatcher(a=left_body, b=right_body).ratio(), 3)


def _build_section_diffs(
    left_snapshot: ReportDocumentSnapshot,
    right_snapshot: ReportDocumentSnapshot,
) -> list[dict[str, Any]]:
    left_map = {section.section_key: section for section in left_snapshot.sections}
    right_map = {section.section_key: section for section in right_snapshot.sections}
    ordered_keys: list[str] = []
    for section in list(left_snapshot.sections) + list(right_snapshot.sections):
        if section.section_key not in ordered_keys:
            ordered_keys.append(section.section_key)

    section_diffs: list[dict[str, Any]] = []
    for section_key in ordered_keys:
        left = left_map.get(section_key)
        right = right_map.get(section_key)
        change_summary: list[str] = []
        title_changed = bool(left and right and left.title != right.title)
        order_changed = bool(left and right and left.order != right.order)
        body_changed = bool(left and right and (left.body_hash != right.body_hash or left.body != right.body))

        if left and not right:
            status = "removed"
            change_summary.append("Section removed from right run.")
        elif right and not left:
            status = "added"
            change_summary.append("Section added in right run.")
        elif title_changed or order_changed or body_changed or left.paper_ids != right.paper_ids:
            status = "changed"
            if title_changed:
                change_summary.append("Section title changed.")
            if order_changed:
                change_summary.append("Section order changed.")
            if body_changed:
                change_summary.append("Section body changed.")
            if left.paper_ids != right.paper_ids:
                change_summary.append("Paper references changed.")
        else:
            status = "unchanged"

        left_citation_count = len((left.citations if left else ())) + len((left.references if left else ()))
        right_citation_count = len((right.citations if right else ())) + len((right.references if right else ()))
        section_diffs.append(
            {
                "section_key": section_key,
                "display_title": (right or left).title if (right or left) else section_key,
                "status": status,
                "left_title": left.title if left else None,
                "right_title": right.title if right else None,
                "left_order": left.order if left else None,
                "right_order": right.order if right else None,
                "title_changed": title_changed,
                "order_changed": order_changed,
                "body_changed": body_changed,
                "body_similarity": _section_similarity(left.body if left else "", right.body if right else ""),
                "left_excerpt": left.excerpt if left else None,
                "right_excerpt": right.excerpt if right else None,
                "left_citation_count": left_citation_count,
                "right_citation_count": right_citation_count,
                "citation_delta_count": right_citation_count - left_citation_count,
                "left_paper_ids": list(left.paper_ids) if left else [],
                "right_paper_ids": list(right.paper_ids) if right else [],
                "change_summary": change_summary,
            }
        )
    return section_diffs


def _aggregate_identifiers(snapshot: ReportDocumentSnapshot) -> tuple[Counter[str], dict[str, set[str]], dict[str, str]]:
    counter: Counter[str] = Counter()
    sections: dict[str, set[str]] = defaultdict(set)
    kinds: dict[str, str] = {}
    for section in snapshot.sections:
        for identifier in section.citations:
            counter[identifier] += 1
            sections[identifier].add(section.title)
            kinds[identifier] = "paper_id"
        for identifier in section.references:
            counter[identifier] += 1
            sections[identifier].add(section.title)
            kinds[identifier] = "url" if identifier.startswith("http") else "reference"
    return counter, sections, kinds


def _build_citation_diffs(
    left_snapshot: ReportDocumentSnapshot,
    right_snapshot: ReportDocumentSnapshot,
) -> list[dict[str, Any]]:
    left_counter, left_sections, left_kinds = _aggregate_identifiers(left_snapshot)
    right_counter, right_sections, right_kinds = _aggregate_identifiers(right_snapshot)
    all_identifiers = sorted(set(left_counter) | set(right_counter))

    diffs: list[dict[str, Any]] = []
    for identifier in all_identifiers:
        left_count = left_counter.get(identifier, 0)
        right_count = right_counter.get(identifier, 0)
        delta = right_count - left_count
        if left_count and not right_count:
            status = "removed"
        elif right_count and not left_count:
            status = "added"
        elif delta:
            status = "changed"
        else:
            status = "unchanged"
        diffs.append(
            {
                "identifier": identifier,
                "kind": (right_kinds.get(identifier) or left_kinds.get(identifier) or "reference"),
                "status": status,
                "left_count": left_count,
                "right_count": right_count,
                "delta": delta,
                "left_sections": sorted(left_sections.get(identifier, set())),
                "right_sections": sorted(right_sections.get(identifier, set())),
            }
        )
    diffs.sort(key=lambda item: (-abs(item["delta"]), item["status"], item["identifier"]))
    return diffs


def _export_diffs(left_run_id: str, right_run_id: str) -> list[dict[str, Any]]:
    left_exports = exports.list_exports(left_run_id).get("items", [])
    right_exports = exports.list_exports(right_run_id).get("items", [])
    left_export_map = {str(item.get("format", "")): item for item in left_exports}
    right_export_map = {str(item.get("format", "")): item for item in right_exports}
    export_formats = sorted({*left_export_map, *right_export_map})
    return [
        {
            "format": export_format,
            "left_available": export_format in left_export_map,
            "right_available": export_format in right_export_map,
            "left_status": left_export_map.get(export_format, {}).get("status"),
            "right_status": right_export_map.get(export_format, {}).get("status"),
        }
        for export_format in export_formats
    ]


def build_report_diff(left_run_id: str, right_run_id: str) -> dict[str, Any]:
    left_snapshot, left_fallbacks = _snapshot_from_artifacts(left_run_id, run_side="left")
    right_snapshot, right_fallbacks = _snapshot_from_artifacts(right_run_id, run_side="right")
    section_diffs = _build_section_diffs(left_snapshot, right_snapshot)
    citation_diffs = _build_citation_diffs(left_snapshot, right_snapshot)
    export_diffs = _export_diffs(left_run_id, right_run_id)

    summary = {
        "left_source": left_snapshot.source,
        "right_source": right_snapshot.source,
        "left_hash": left_snapshot.content_hash,
        "right_hash": right_snapshot.content_hash,
        "hash_changed": None
        if not left_snapshot.content_hash or not right_snapshot.content_hash
        else left_snapshot.content_hash != right_snapshot.content_hash,
        "completeness": "full"
        if left_snapshot.completeness == "full" and right_snapshot.completeness == "full"
        else "partial"
        if "full" in {left_snapshot.completeness, right_snapshot.completeness}
        else "outline_only"
        if "outline_only" in {left_snapshot.completeness, right_snapshot.completeness}
        else "metadata_only"
        if "metadata_only" in {left_snapshot.completeness, right_snapshot.completeness}
        else "unavailable",
        "confidence": round(min(left_snapshot.confidence, right_snapshot.confidence), 2),
        "added_sections": sum(1 for item in section_diffs if item["status"] == "added"),
        "removed_sections": sum(1 for item in section_diffs if item["status"] == "removed"),
        "changed_sections": sum(1 for item in section_diffs if item["status"] == "changed"),
        "unchanged_sections": sum(1 for item in section_diffs if item["status"] == "unchanged"),
        "order_changed_sections": sum(1 for item in section_diffs if item["order_changed"]),
        "citation_delta_count": sum(1 for item in citation_diffs if item["status"] != "unchanged"),
        "missing_explanations": list(dict.fromkeys([*left_snapshot.missing_explanations, *right_snapshot.missing_explanations])),
    }

    return {
        "left_run_id": left_run_id,
        "right_run_id": right_run_id,
        "report_summary_diff": summary,
        "section_diffs": section_diffs,
        "citation_diffs": citation_diffs,
        "export_diffs": export_diffs,
        "artifact_fallbacks": [asdict(item) for item in [*left_fallbacks, *right_fallbacks]],
    }

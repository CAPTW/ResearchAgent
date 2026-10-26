from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc
from app.repositories.runs import list_run_records, load_run_record
from app.services import exports, report_diff, reporting, reviews, workbench


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "topic": run.get("topic", run["run_id"]),
        "status": run.get("status", "unknown"),
        "created_at": run.get("created_at", ""),
        "finished_at": run.get("finished_at"),
        "lineage_mode": run.get("lineage", {}).get("lineage_mode", "standalone"),
    }


def _compare_relation(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, bool]:
    left_lineage = left.get("lineage", {})
    right_lineage = right.get("lineage", {})
    same_root = left_lineage.get("root_run_id") == right_lineage.get("root_run_id")
    if left["run_id"] == right_lineage.get("parent_run_id"):
        return "left_is_parent_of_right", same_root
    if right["run_id"] == left_lineage.get("parent_run_id"):
        return "right_is_parent_of_left", same_root
    if same_root:
        return "same_root", True
    return "unrelated", False


def _load_compare_papers(run_id: str) -> list[dict[str, Any]]:
    run = load_run_record(run_id)
    try:
        triage_items = workbench.load_run_triage_items(run_id)
    except FileNotFoundError:
        triage_items = []

    if triage_items:
        return [
            {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "score": int(item.get("score", 0) or 0),
                "theme": item.get("theme") or "Uncategorized",
                "rank": index,
                "source_provider": str(item.get("source_provider") or item.get("source") or "unknown").lower(),
                "keyword_hits": item.get("keyword_hits") or {},
            }
            for index, item in enumerate(triage_items, start=1)
            if item.get("paper_id")
        ]

    return [
        {
            "paper_id": item.get("paper_id", ""),
            "title": item.get("title", ""),
            "score": int(item.get("score", 0) or 0),
            "theme": item.get("theme") or "Uncategorized",
            "rank": index,
            "source_provider": str(item.get("source_provider") or item.get("source") or "unknown").lower(),
            "keyword_hits": item.get("keyword_hits") or {},
        }
        for index, item in enumerate(run.get("compare_ready", {}).get("top_papers", []) or [], start=1)
        if item.get("paper_id")
    ]


def _theme_counts(run: dict[str, Any], papers: list[dict[str, Any]]) -> dict[str, int]:
    if papers:
        counter = Counter(item.get("theme") or "Uncategorized" for item in papers)
        return dict(counter.most_common())
    return {str(key): int(value) for key, value in (run.get("compare_ready", {}).get("theme_counts", {}) or {}).items()}


def _score_band(score: int | None) -> str:
    if score is None:
        return "unscored"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    if score > 0:
        return "low"
    return "unscored"


def _score_distribution(left_papers: list[dict[str, Any]], right_papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    left_counts = Counter(_score_band(paper.get("score")) for paper in left_papers)
    right_counts = Counter(_score_band(paper.get("score")) for paper in right_papers)
    return [
        {
            "band": band,
            "left_count": left_counts.get(band, 0),
            "right_count": right_counts.get(band, 0),
            "delta": right_counts.get(band, 0) - left_counts.get(band, 0),
        }
        for band in ["high", "medium", "low", "unscored"]
    ]


def _keyword_map(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for paper in papers:
        hits = paper.get("keyword_hits") or {}
        if not isinstance(hits, dict):
            continue
        values: list[str] = []
        for key in ("primary", "secondary"):
            raw_values = hits.get(key) or []
            if isinstance(raw_values, list):
                values.extend(str(value).strip().lower() for value in raw_values if str(value).strip())
        for keyword in sorted(set(values)):
            entry = result.setdefault(keyword, {"count": 0, "sample_paper_ids": []})
            entry["count"] += 1
            if len(entry["sample_paper_ids"]) < 3:
                entry["sample_paper_ids"].append(paper["paper_id"])
    return result


def _keyword_movement(left_papers: list[dict[str, Any]], right_papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    left_keywords = _keyword_map(left_papers)
    right_keywords = _keyword_map(right_papers)
    keywords = sorted(set(left_keywords) | set(right_keywords))
    items = [
        {
            "keyword": keyword,
            "left_count": left_keywords.get(keyword, {}).get("count", 0),
            "right_count": right_keywords.get(keyword, {}).get("count", 0),
            "delta": right_keywords.get(keyword, {}).get("count", 0) - left_keywords.get(keyword, {}).get("count", 0),
            "left_sample_paper_ids": left_keywords.get(keyword, {}).get("sample_paper_ids", []),
            "right_sample_paper_ids": right_keywords.get(keyword, {}).get("sample_paper_ids", []),
        }
        for keyword in keywords
    ]
    items.sort(key=lambda item: (-abs(item["delta"]), -max(item["left_count"], item["right_count"]), item["keyword"]))
    return items[:20]


def _provider_contribution(left_papers: list[dict[str, Any]], right_papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def build(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for paper in papers:
            provider = str(paper.get("source_provider") or "unknown").lower()
            entry = result.setdefault(provider, {"count": 0, "sample_paper_ids": []})
            entry["count"] += 1
            if len(entry["sample_paper_ids"]) < 3:
                entry["sample_paper_ids"].append(paper["paper_id"])
        return result

    left_providers = build(left_papers)
    right_providers = build(right_papers)
    providers = sorted(set(left_providers) | set(right_providers))
    items = [
        {
            "provider": provider,
            "left_count": left_providers.get(provider, {}).get("count", 0),
            "right_count": right_providers.get(provider, {}).get("count", 0),
            "delta": right_providers.get(provider, {}).get("count", 0) - left_providers.get(provider, {}).get("count", 0),
            "left_sample_paper_ids": left_providers.get(provider, {}).get("sample_paper_ids", []),
            "right_sample_paper_ids": right_providers.get(provider, {}).get("sample_paper_ids", []),
        }
        for provider in providers
    ]
    items.sort(key=lambda item: (-max(item["left_count"], item["right_count"]), item["provider"]))
    return items


def _report_state(run: dict[str, Any]) -> dict[str, Any]:
    artifacts = run.get("artifacts", {}) or {}
    report_path = artifacts.get("research_plan_markdown") or artifacts.get("report_markdown")
    available = bool(report_path and Path(str(report_path)).is_file())
    return {
        "available": available,
        "status": run.get("status", "unknown") if available else "missing",
        "updated_at": run.get("finished_at") or run.get("updated_at") or run.get("created_at"),
        "artifact_path": str(report_path) if report_path else None,
    }


def _report_status(left_run: dict[str, Any], right_run: dict[str, Any]) -> dict[str, Any]:
    left = _report_state(left_run)
    right = _report_state(right_run)
    return {
        "left_available": left["available"],
        "right_available": right["available"],
        "left_status": left["status"],
        "right_status": right["status"],
        "left_updated_at": left["updated_at"],
        "right_updated_at": right["updated_at"],
        "left_artifact_path": left["artifact_path"],
        "right_artifact_path": right["artifact_path"],
    }


def _reviewed_count(run_id: str, papers: list[dict[str, Any]]) -> int:
    if not papers:
        return 0
    reviewed = 0
    for paper in papers:
        try:
            raw = workbench.get_raw_paper(run_id, paper["paper_id"])
            if reviews.get_review_status(run_id, raw) in {"reviewed", "partial_review"}:
                reviewed += 1
        except FileNotFoundError:
            continue
    return reviewed


def _latest_export_formats(run_id: str) -> list[str]:
    manifest = exports.list_exports(run_id).get("items", [])
    formats = []
    for item in manifest:
        export_format = str(item.get("format", "")).strip()
        if export_format and export_format not in formats:
            formats.append(export_format)
    return formats


def _outline_deltas(left_outline: list[dict[str, Any]], right_outline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    left_map = {section["section_id"]: section for section in left_outline}
    right_map = {section["section_id"]: section for section in right_outline}
    section_ids = sorted(set(left_map) | set(right_map))
    deltas: list[dict[str, Any]] = []
    for section_id in section_ids:
        left = left_map.get(section_id)
        right = right_map.get(section_id)
        if left and not right:
            status = "removed"
        elif right and not left:
            status = "added"
        elif left and right and (
            left.get("title") != right.get("title")
            or left.get("kind") != right.get("kind")
            or left.get("paper_ids") != right.get("paper_ids")
            or left.get("body_hash") != right.get("body_hash")
        ):
            status = "changed"
        else:
            status = "unchanged"
        deltas.append(
            {
                "section_id": section_id,
                "title": (right or left or {}).get("title", section_id),
                "status": status,
                "left_kind": (left or {}).get("kind"),
                "right_kind": (right or {}).get("kind"),
                "left_paper_count": len((left or {}).get("paper_ids", [])),
                "right_paper_count": len((right or {}).get("paper_ids", [])),
            }
        )
    return deltas


def _build_bucket(
    *,
    bucket_id: str,
    label: str,
    description: str,
    source_run_id: str,
    compare_partner_run_id: str,
    selected_paper_ids: list[str],
    source_side: str,
    theme: str | None = None,
    status: str | None = None,
    suggested_note: str | None = None,
) -> dict[str, Any]:
    available = bool(selected_paper_ids) or bucket_id == "manual_selection_seed"
    return {
        "bucket_id": bucket_id,
        "label": label,
        "description": description,
        "source_run_id": source_run_id,
        "compare_partner_run_id": compare_partner_run_id,
        "mode": "branch_from_selected_papers",
        "selected_paper_ids": selected_paper_ids,
        "selection_count": len(selected_paper_ids),
        "available": available,
        "empty_reason": None if available else "No papers match this compare bucket.",
        "suggested_note": suggested_note,
        "profile_overrides": None,
        "compare_filters": {
            "paper_query": None,
            "theme": theme,
            "status": status,
            "source_side": source_side,
            "bucket_id": bucket_id,
            "bucket_label": label,
        },
    }


def _build_rerun_buckets(
    *,
    left_run_id: str,
    right_run_id: str,
    left_papers: list[dict[str, Any]],
    right_papers: list[dict[str, Any]],
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    common: list[dict[str, Any]],
    theme_deltas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = [
        _build_bucket(
            bucket_id="manual_selection_seed",
            label="Manual selection seed",
            description="Start with no preselected papers and choose papers manually from the compare view.",
            source_run_id=left_run_id,
            compare_partner_run_id=right_run_id,
            selected_paper_ids=[],
            source_side="both",
            suggested_note="Manual compare-driven rerun.",
        )
    ]

    added_ids = [item["paper_id"] for item in added]
    removed_ids = [item["paper_id"] for item in removed]
    changed_overlap_left_ids = [item["paper_id"] for item in common if item["changed"] and item["present_in_left"]]
    changed_overlap_right_ids = [item["paper_id"] for item in common if item["changed"] and item["present_in_right"]]

    buckets.append(
        _build_bucket(
            bucket_id="right_added_papers_only",
            label="Added papers in right run",
            description="Branch the right run using only papers that appear newly on the right side.",
            source_run_id=right_run_id,
            compare_partner_run_id=left_run_id,
            selected_paper_ids=added_ids,
            source_side="right",
            status="added",
            suggested_note="Focus on papers newly added in the right compare run.",
        )
    )
    buckets.append(
        _build_bucket(
            bucket_id="left_removed_or_missing_focus",
            label="Removed or missing from right",
            description="Branch the left run using papers that disappeared on the right side.",
            source_run_id=left_run_id,
            compare_partner_run_id=right_run_id,
            selected_paper_ids=removed_ids,
            source_side="left",
            status="removed",
            suggested_note="Investigate papers present in the left run but missing on the right.",
        )
    )
    buckets.append(
        _build_bucket(
            bucket_id="left_changed_score_overlap",
            label="Changed overlap from left",
            description="Branch the left run with overlapping papers whose rank or score changed between runs.",
            source_run_id=left_run_id,
            compare_partner_run_id=right_run_id,
            selected_paper_ids=changed_overlap_left_ids,
            source_side="left",
            status="common",
            suggested_note="Revisit overlapping papers whose triage score or rank shifted.",
        )
    )
    buckets.append(
        _build_bucket(
            bucket_id="right_changed_score_overlap",
            label="Changed overlap from right",
            description="Branch the right run with overlapping papers whose rank or score changed between runs.",
            source_run_id=right_run_id,
            compare_partner_run_id=left_run_id,
            selected_paper_ids=changed_overlap_right_ids,
            source_side="right",
            status="common",
            suggested_note="Revisit overlapping papers from the right run whose triage score or rank shifted.",
        )
    )

    if theme_deltas:
        positive_theme = next((item for item in theme_deltas if item["delta"] > 0), None)
        negative_theme = next((item for item in theme_deltas if item["delta"] < 0), None)
        if positive_theme:
            right_theme_ids = [paper["paper_id"] for paper in right_papers if paper.get("theme") == positive_theme["theme"]]
            buckets.append(
                _build_bucket(
                    bucket_id="right_theme_delta_focus",
                    label=f"Right theme delta: {positive_theme['theme']}",
                    description="Branch the right run using papers from the theme that gained the most weight on the right side.",
                    source_run_id=right_run_id,
                    compare_partner_run_id=left_run_id,
                    selected_paper_ids=right_theme_ids,
                    source_side="right",
                    theme=positive_theme["theme"],
                    suggested_note=f"Focus on theme growth for {positive_theme['theme']} in the right run.",
                )
            )
        if negative_theme:
            left_theme_ids = [paper["paper_id"] for paper in left_papers if paper.get("theme") == negative_theme["theme"]]
            buckets.append(
                _build_bucket(
                    bucket_id="left_theme_delta_focus",
                    label=f"Left theme delta: {negative_theme['theme']}",
                    description="Branch the left run using papers from the theme that shrank most on the right side.",
                    source_run_id=left_run_id,
                    compare_partner_run_id=right_run_id,
                    selected_paper_ids=left_theme_ids,
                    source_side="left",
                    theme=negative_theme["theme"],
                    suggested_note=f"Investigate theme loss for {negative_theme['theme']} relative to the right run.",
                )
            )

    return buckets


def get_run_lineage(run_id: str) -> dict[str, Any]:
    target = load_run_record(run_id)
    runs = list_run_records(limit=500)
    lineage = target.get("lineage", {})
    root_run_id = lineage.get("root_run_id") or run_id
    parent_run_id = lineage.get("parent_run_id")

    parent = next((run for run in runs if run["run_id"] == parent_run_id), None)
    root = next((run for run in runs if run["run_id"] == root_run_id), target)
    children = [run for run in runs if run.get("lineage", {}).get("parent_run_id") == run_id]
    same_root_runs = [
        run
        for run in runs
        if run["run_id"] != run_id and run.get("lineage", {}).get("root_run_id") == root_run_id
    ]

    return {
        "run_id": run_id,
        "lineage": lineage,
        "parent": _run_summary(parent) if parent else None,
        "root": _run_summary(root),
        "children": [_run_summary(run) for run in children],
        "same_root_runs": [_run_summary(run) for run in same_root_runs[:20]],
    }


def list_compare_candidates(run_id: str) -> dict[str, Any]:
    target = load_run_record(run_id)
    relation_items: list[dict[str, Any]] = []
    for candidate in list_run_records(limit=100):
        if candidate["run_id"] == run_id:
            continue
        relation, same_root = _compare_relation(target, candidate)
        preview = reporting.collect_preview_fingerprint(candidate["run_id"])
        relation_items.append(
            {
                "run_id": candidate["run_id"],
                "topic": candidate.get("topic", candidate["run_id"]),
                "status": candidate.get("status", "unknown"),
                "created_at": candidate.get("created_at", ""),
                "triage_items": int(candidate.get("counts", {}).get("triage_items", 0) or 0),
                "compare_ready_available": bool(candidate.get("compare_ready")),
                "relation": relation,
                "same_root": same_root,
                "preview_hash": preview.get("hash"),
                "parent_run_id": candidate.get("lineage", {}).get("parent_run_id"),
                "root_run_id": candidate.get("lineage", {}).get("root_run_id") or candidate["run_id"],
            }
        )

    relation_items.sort(key=lambda item: (not item["same_root"], item["created_at"]), reverse=False)
    relation_items.sort(key=lambda item: item["created_at"], reverse=True)
    relation_items.sort(key=lambda item: not item["same_root"])
    return {"run_id": run_id, "items": relation_items[:40]}


def compare_runs(left_run_id: str, right_run_id: str) -> dict[str, Any]:
    left_run = load_run_record(left_run_id)
    right_run = load_run_record(right_run_id)
    relation, same_root = _compare_relation(left_run, right_run)

    left_papers = _load_compare_papers(left_run_id)
    right_papers = _load_compare_papers(right_run_id)
    left_map = {paper["paper_id"]: paper for paper in left_papers}
    right_map = {paper["paper_id"]: paper for paper in right_papers}

    left_ids = set(left_map)
    right_ids = set(right_map)
    added_ids = sorted(right_ids - left_ids, key=lambda paper_id: right_map[paper_id]["rank"])
    removed_ids = sorted(left_ids - right_ids, key=lambda paper_id: left_map[paper_id]["rank"])
    common_ids = sorted(left_ids & right_ids, key=lambda paper_id: min(left_map[paper_id]["rank"], right_map[paper_id]["rank"]))

    added = [
        {
            "paper_id": paper_id,
            "title": right_map[paper_id]["title"],
            "theme": right_map[paper_id]["theme"],
            "status": "added",
            "left_score": None,
            "right_score": right_map[paper_id]["score"],
            "score_delta": right_map[paper_id]["score"],
            "left_rank": None,
            "right_rank": right_map[paper_id]["rank"],
            "rank_delta": None,
            "changed": True,
            "present_in_left": False,
            "present_in_right": True,
        }
        for paper_id in added_ids
    ]
    removed = [
        {
            "paper_id": paper_id,
            "title": left_map[paper_id]["title"],
            "theme": left_map[paper_id]["theme"],
            "status": "removed",
            "left_score": left_map[paper_id]["score"],
            "right_score": None,
            "score_delta": -left_map[paper_id]["score"],
            "left_rank": left_map[paper_id]["rank"],
            "right_rank": None,
            "rank_delta": None,
            "changed": True,
            "present_in_left": True,
            "present_in_right": False,
        }
        for paper_id in removed_ids
    ]

    common: list[dict[str, Any]] = []
    changed_common = 0
    for paper_id in common_ids:
        left = left_map[paper_id]
        right = right_map[paper_id]
        rank_delta = left["rank"] - right["rank"]
        score_delta = right["score"] - left["score"]
        changed = bool(rank_delta or score_delta or left["theme"] != right["theme"] or left["title"] != right["title"])
        if changed:
            changed_common += 1
        common.append(
            {
                "paper_id": paper_id,
                "title": right["title"] or left["title"],
                "theme": right["theme"] or left["theme"],
                "status": "common",
                "left_score": left["score"],
                "right_score": right["score"],
                "score_delta": score_delta,
                "left_rank": left["rank"],
                "right_rank": right["rank"],
                "rank_delta": rank_delta,
                "changed": changed,
                "present_in_left": True,
                "present_in_right": True,
            }
        )

    left_theme_counts = _theme_counts(left_run, left_papers)
    right_theme_counts = _theme_counts(right_run, right_papers)
    all_themes = sorted(set(left_theme_counts) | set(right_theme_counts))
    theme_deltas = [
        {
            "theme": theme,
            "left_count": left_theme_counts.get(theme, 0),
            "right_count": right_theme_counts.get(theme, 0),
            "delta": right_theme_counts.get(theme, 0) - left_theme_counts.get(theme, 0),
        }
        for theme in all_themes
    ]
    theme_deltas.sort(key=lambda item: (-abs(item["delta"]), item["theme"]))

    left_outline = reporting.collect_outline_summary(left_run_id)
    right_outline = reporting.collect_outline_summary(right_run_id)
    outline_deltas = _outline_deltas(left_outline, right_outline)

    left_preview = reporting.collect_preview_fingerprint(left_run_id)
    right_preview = reporting.collect_preview_fingerprint(right_run_id)

    report_detail = report_diff.build_report_diff(left_run_id, right_run_id)
    export_diffs = report_detail["export_diffs"]
    rerun_buckets = _build_rerun_buckets(
        left_run_id=left_run_id,
        right_run_id=right_run_id,
        left_papers=left_papers,
        right_papers=right_papers,
        added=added,
        removed=removed,
        common=common,
        theme_deltas=theme_deltas,
    )
    warnings = []
    if not left_papers or not right_papers:
        warnings.append("One or both runs have no paper artifacts, so compare metrics are partial.")

    return {
        "left": {
            "run_id": left_run_id,
            "topic": left_run.get("topic", left_run_id),
            "status": left_run.get("status", "unknown"),
            "created_at": left_run.get("created_at", ""),
            "started_at": left_run.get("started_at"),
            "finished_at": left_run.get("finished_at"),
            "triage_items": len(left_papers) or int(left_run.get("counts", {}).get("triage_items", 0) or 0),
            "reviewed_items": _reviewed_count(left_run_id, left_papers),
            "preview_hash": left_preview.get("hash"),
            "preview_hash_source": left_preview.get("source"),
            "outline_sections": len(left_outline),
            "exports": _latest_export_formats(left_run_id),
            "lineage": left_run.get("lineage", {}),
        },
        "right": {
            "run_id": right_run_id,
            "topic": right_run.get("topic", right_run_id),
            "status": right_run.get("status", "unknown"),
            "created_at": right_run.get("created_at", ""),
            "started_at": right_run.get("started_at"),
            "finished_at": right_run.get("finished_at"),
            "triage_items": len(right_papers) or int(right_run.get("counts", {}).get("triage_items", 0) or 0),
            "reviewed_items": _reviewed_count(right_run_id, right_papers),
            "preview_hash": right_preview.get("hash"),
            "preview_hash_source": right_preview.get("source"),
            "outline_sections": len(right_outline),
            "exports": _latest_export_formats(right_run_id),
            "lineage": right_run.get("lineage", {}),
        },
        "summary": {
            "relation": relation,
            "same_root": same_root,
            "preview_changed": None
            if not left_preview.get("hash") or not right_preview.get("hash")
            else left_preview.get("hash") != right_preview.get("hash"),
            "added_papers": len(added),
            "removed_papers": len(removed),
            "common_papers": len(common),
            "changed_common_papers": changed_common,
            "unchanged_common_papers": len(common) - changed_common,
            "theme_delta_count": sum(1 for item in theme_deltas if item["delta"] != 0),
            "outline_changed": any(item["status"] != "unchanged" for item in outline_deltas),
        },
        "papers": {
            "added": added,
            "removed": removed,
            "common": common,
        },
        "themes": theme_deltas,
        "score_distribution": _score_distribution(left_papers, right_papers),
        "keyword_movement": _keyword_movement(left_papers, right_papers),
        "provider_contribution": _provider_contribution(left_papers, right_papers),
        "report_status": _report_status(left_run, right_run),
        "report": {
            "left_outline": left_outline,
            "right_outline": right_outline,
            "outline_deltas": outline_deltas,
            "left_preview_hash": left_preview.get("hash"),
            "right_preview_hash": right_preview.get("hash"),
            "left_preview_hash_source": left_preview.get("source"),
            "right_preview_hash_source": right_preview.get("source"),
            "report_summary_diff": report_detail["report_summary_diff"],
            "artifact_fallbacks": report_detail["artifact_fallbacks"],
        },
        "exports": export_diffs,
        "lineage": {
            "relation": relation,
            "same_root": same_root,
            "left": get_run_lineage(left_run_id),
            "right": get_run_lineage(right_run_id),
        },
        "rerun_buckets": rerun_buckets,
        "provenance": {
            "source_run_ids": [left_run_id, right_run_id],
            "computed_at": iso_now_utc(),
            "definitions": [
                "paper volume uses run triage artifacts or compare_ready fallback counts.",
                "score distribution uses stored triage scores: high >= 50, medium >= 25, low > 0, otherwise unscored.",
                "keyword movement counts normalized primary and secondary keyword_hits per paper.",
                "provider contribution counts stored source/source_provider metadata per paper.",
                "changed papers are common paper IDs with score, rank, title, or theme differences.",
                "branch_from_selected_papers rerun seeds a child run from selected parent triage items when retrieval is skipped.",
            ],
            "warnings": warnings,
        },
    }

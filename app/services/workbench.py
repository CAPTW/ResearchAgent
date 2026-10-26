from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc, read_json, read_text, safe_filename, write_json
from app.repositories.jsonl import read_jsonl_strict_with_line_numbers
from app.repositories.runs import build_run_paths, load_run_record
from app.services import paper_occurrences, reviews

XR_TITLE_TERMS = ["xr", "ar", "vr", "mixed reality", "augmented reality", "virtual reality", "head-mounted"]
REALTIME_TERMS = ["real-time", "latency", "on-device", "edge", "mobile", "efficient"]
STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
    "using",
}
REVIEW_ACTIONS_FILENAME = "paper_review_actions.json"
VALID_REVIEW_DECISIONS = {"unreviewed", "keep", "dismiss", "save"}


def load_run_triage_items(run_id: str) -> list[dict[str, Any]]:
    run = load_run_record(run_id)
    triage_path = Path(run.get("artifacts", {}).get("triage_jsonl") or build_run_paths(run_id).triage_dir / "triage.jsonl")
    records = read_jsonl_strict_with_line_numbers(triage_path)
    loaded = [
        paper_occurrences.attach_loaded_authority(
            item,
            run_id=run_id,
            logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
            physical_line_number=line_number,
        )
        for line_number, item in records
    ]
    paper_occurrences.require_unique_authorities(loaded)
    return loaded


def get_raw_paper(run_id: str, paper_id: str) -> dict[str, Any]:
    raw_paper = next(
        (
            item
            for item in load_run_triage_items(run_id)
            if item.get("paper_id") == paper_id or item.get("occurrence_id") == paper_id
        ),
        None,
    )
    if raw_paper is None:
        raise FileNotFoundError(f"Paper not found in run {run_id}: {paper_id}")
    return raw_paper


def _review_actions_path(run_id: str) -> Path:
    return build_run_paths(run_id).run_dir / REVIEW_ACTIONS_FILENAME


def _default_review_action() -> dict[str, Any]:
    return {
        "decision": "unreviewed",
        "pinned": False,
        "note": "",
        "updated_at": None,
    }


def _normalize_review_action(payload: dict[str, Any] | None) -> dict[str, Any]:
    action = _default_review_action()
    if not isinstance(payload, dict):
        return action

    decision = str(payload.get("decision") or "unreviewed")
    action["decision"] = decision if decision in VALID_REVIEW_DECISIONS else "unreviewed"
    action["pinned"] = bool(payload.get("pinned", False))
    action["note"] = str(payload.get("note") or "")[:4000]
    updated_at = payload.get("updated_at")
    action["updated_at"] = str(updated_at) if updated_at else None
    return action


def load_paper_review_actions(run_id: str) -> dict[str, dict[str, Any]]:
    load_run_record(run_id)
    payload = read_json(_review_actions_path(run_id), default={})
    if not isinstance(payload, dict):
        return {}
    return {str(paper_id): _normalize_review_action(action) for paper_id, action in payload.items() if isinstance(action, dict)}


def get_paper_review_action(run_id: str, paper_id: str) -> dict[str, Any]:
    return load_paper_review_actions(run_id).get(paper_id, _default_review_action())


def update_paper_review_action(run_id: str, paper_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    get_raw_paper(run_id, paper_id)
    current_actions = load_paper_review_actions(run_id)
    current = _normalize_review_action(current_actions.get(paper_id))

    if "decision" in patch and patch["decision"] is not None:
        decision = str(patch["decision"])
        if decision not in VALID_REVIEW_DECISIONS:
            raise ValueError(f"Unsupported review decision: {decision}")
        current["decision"] = decision
    if "pinned" in patch and patch["pinned"] is not None:
        current["pinned"] = bool(patch["pinned"])
    if "note" in patch and patch["note"] is not None:
        current["note"] = str(patch["note"])[:4000]

    current["updated_at"] = iso_now_utc()
    current_actions[paper_id] = current
    write_json(_review_actions_path(run_id), current_actions)
    return current


def _score_band(score: int) -> str:
    if score >= 50:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _score_breakdown(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title", "")).lower()
    abstract = str(item.get("abstract", "")).lower()
    blob = f"{title} {abstract}"
    triage = item.get("triage", {}) or {}
    primary_hits = sorted(set(triage.get("hits_primary", []) or []))
    secondary_hits = sorted(set(triage.get("hits_secondary", []) or []))
    negative_hits = sorted(set(triage.get("hits_negative", []) or []))

    primary_points = min(40, 4 * len(primary_hits))
    secondary_points = min(20, 2 * len(secondary_hits))
    negative_points = -min(30, 6 * len(negative_hits))
    xr_bonus = 12 if any(term in title for term in XR_TITLE_TERMS) else 0
    realtime_bonus = 6 if any(term in blob for term in REALTIME_TERMS) else 0
    pdf_bonus = 2 if item.get("pdf_url") else 0
    final_score = max(0, min(100, primary_points + secondary_points + negative_points + xr_bonus + realtime_bonus + pdf_bonus))
    return {
        "primary_hits": primary_hits,
        "secondary_hits": secondary_hits,
        "negative_hits": negative_hits,
        "primary_points": primary_points,
        "secondary_points": secondary_points,
        "negative_points": negative_points,
        "xr_bonus": xr_bonus,
        "realtime_bonus": realtime_bonus,
        "pdf_bonus": pdf_bonus,
        "final_score": final_score,
    }


def _abstract_excerpt(item: dict[str, Any]) -> str:
    abstract = " ".join(str(item.get("abstract", "")).split())
    return abstract[:260] + ("..." if len(abstract) > 260 else "")


def _base_arxiv_id(paper_id: str) -> str | None:
    match = re.match(r"(?P<base>\d+\.\d+)(?:v\d+)?$", paper_id)
    return match.group("base") if match else None


def _title_fingerprint(title: str) -> str:
    tokens = [token for token in re.findall(r"[a-z0-9]+", title.lower()) if token not in STOPWORDS]
    if not tokens:
        return safe_filename(title.lower())
    return "-".join(tokens[:8])


def _cluster_key(item: dict[str, Any]) -> str:
    doi = str(item.get("doi", "")).strip().lower()
    if doi:
        return f"doi:{doi}"
    arxiv_base = _base_arxiv_id(str(item.get("paper_id", "")).strip())
    if arxiv_base:
        return f"arxiv:{arxiv_base}"
    return f"title:{_title_fingerprint(str(item.get('title', '')))}"


def _build_cluster_map(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    clusters: dict[str, list[str]] = defaultdict(list)
    for item in items:
        paper_id = str(item.get("paper_id", "")).strip()
        if not paper_id:
            continue
        clusters[_cluster_key(item)].append(paper_id)
    return clusters


def _paper_artifacts(run_id: str, paper_id: str) -> dict[str, str | None]:
    return reviews.resolve_paper_artifacts(run_id, paper_id)


def _paper_status(run_id: str, paper_item: dict[str, Any]) -> str:
    return reviews.get_review_status(run_id, paper_item)


def _source_provider(paper_item: dict[str, Any]) -> str | None:
    for key in ("source_provider", "provider", "source"):
        value = str(paper_item.get(key) or "").strip()
        if value:
            return value

    source_url = str(paper_item.get("entry_url") or paper_item.get("entry_id") or "").lower()
    if "arxiv.org" in source_url:
        return "arxiv"
    if "openalex.org" in source_url:
        return "openalex"
    if "doi.org" in source_url or "crossref.org" in source_url:
        return "crossref"
    return None


def _build_paper_list_item(
    run_id: str,
    paper_item: dict[str, Any],
    clusters: dict[str, list[str]],
    review_actions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    paper_id = str(paper_item.get("paper_id", ""))
    cluster_id = _cluster_key(paper_item)
    cluster_members = clusters.get(cluster_id, [])
    score_breakdown = _score_breakdown(paper_item)
    action = (review_actions or {}).get(paper_id, _default_review_action())
    return {
        "paper_id": paper_id,
        "title": paper_item.get("title", ""),
        "source_provider": _source_provider(paper_item),
        "score": int(paper_item.get("score", 0) or 0),
        "score_band": _score_band(int(paper_item.get("score", 0) or 0)),
        "theme": paper_item.get("theme") or "Uncategorized",
        "published": paper_item.get("published"),
        "entry_url": paper_item.get("entry_url") or paper_item.get("entry_id"),
        "pdf_url": paper_item.get("pdf_url"),
        "status": _paper_status(run_id, paper_item),
        "abstract_excerpt": _abstract_excerpt(paper_item),
        "score_breakdown": score_breakdown,
        "dedup": {
            "cluster_id": cluster_id,
            "cluster_size": len(cluster_members),
            "is_primary": cluster_members[:1] == [paper_id],
            "primary_paper_id": cluster_members[0] if cluster_members else None,
            "members": cluster_members,
        },
        "review_action": _normalize_review_action(action),
        "triage": paper_item.get("triage") or {},
        "artifacts": _paper_artifacts(run_id, paper_id),
    }


def _matches_query(item: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    haystack = " ".join(
        [
            str(item.get("paper_id", "")),
            str(item.get("title", "")),
            str(item.get("theme", "")),
            str(item.get("abstract_excerpt", "")),
            " ".join(item.get("score_breakdown", {}).get("primary_hits", [])),
            " ".join(item.get("score_breakdown", {}).get("secondary_hits", [])),
        ]
    ).lower()
    return q.lower() in haystack


def _related_papers(target_item: dict[str, Any], items: list[dict[str, Any]], clusters: dict[str, list[str]]) -> list[dict[str, Any]]:
    cluster_members = set(clusters.get(_cluster_key(target_item), []))
    if len(cluster_members) > 1:
        related = [
            {
                "paper_id": item["paper_id"],
                "title": item["title"],
                "score": item["score"],
                "theme": item["theme"],
                "relation": "duplicate_cluster",
            }
            for item in items
            if item["paper_id"] in cluster_members and item["paper_id"] != target_item["paper_id"]
        ]
        if related:
            return related[:5]

    return [
        {
            "paper_id": item["paper_id"],
            "title": item["title"],
            "score": item["score"],
            "theme": item["theme"],
            "relation": "same_theme",
        }
        for item in items
        if item["paper_id"] != target_item["paper_id"] and item["theme"] == target_item["theme"]
    ][:5]


def list_run_papers(
    run_id: str,
    *,
    page: int = 1,
    page_size: int = 25,
    q: str = "",
    status: str | None = None,
    theme: str | None = None,
    score_band: str | None = None,
    provider: str | None = None,
    dedup: str = "all",
) -> dict[str, Any]:
    raw_items = load_run_triage_items(run_id)
    clusters = _build_cluster_map(raw_items)
    review_actions = load_paper_review_actions(run_id)
    enriched = [_build_paper_list_item(run_id, item, clusters, review_actions) for item in raw_items]

    themes_counter = Counter(item["theme"] for item in enriched)
    status_counter = Counter(item["status"] for item in enriched)
    band_counter = Counter(item["score_band"] for item in enriched)
    provider_counter = Counter(item["source_provider"] or "unknown" for item in enriched)
    duplicate_clusters = sum(1 for members in clusters.values() if len(members) > 1)

    filtered = [item for item in enriched if _matches_query(item, q)]
    if status:
        filtered = [item for item in filtered if item["status"] == status]
    if theme:
        filtered = [item for item in filtered if item["theme"] == theme]
    if score_band:
        filtered = [item for item in filtered if item["score_band"] == score_band]
    if provider:
        filtered = [item for item in filtered if (item["source_provider"] or "unknown") == provider]
    if dedup == "duplicates_only":
        filtered = [item for item in filtered if item["dedup"]["cluster_size"] > 1]
    elif dedup == "unique_only":
        filtered = [item for item in filtered if item["dedup"]["cluster_size"] == 1]

    total = len(filtered)
    total_pages = math.ceil(total / page_size) if total else 0
    current_page = min(max(page, 1), max(total_pages, 1))
    start = (current_page - 1) * page_size
    end = start + page_size

    return {
        "run_id": run_id,
        "items": filtered[start:end],
        "page": current_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "q": q,
        "status": status,
        "theme": theme,
        "score_band": score_band,
        "provider": provider,
        "dedup": dedup,
        "facets": {
            "themes": [{"value": value, "count": count} for value, count in sorted(themes_counter.items(), key=lambda pair: (-pair[1], pair[0]))],
            "statuses": [{"value": value, "count": count} for value, count in sorted(status_counter.items(), key=lambda pair: (-pair[1], pair[0]))],
            "score_bands": [{"value": value, "count": count} for value, count in sorted(band_counter.items(), key=lambda pair: (-pair[1], pair[0]))],
            "providers": [{"value": value, "count": count} for value, count in sorted(provider_counter.items(), key=lambda pair: (-pair[1], pair[0]))],
            "duplicate_clusters": duplicate_clusters,
        },
    }


def get_run_paper_detail(run_id: str, paper_id: str) -> dict[str, Any]:
    raw_items = load_run_triage_items(run_id)
    raw_paper = get_raw_paper(run_id, paper_id)

    clusters = _build_cluster_map(raw_items)
    review_actions = load_paper_review_actions(run_id)
    paper = _build_paper_list_item(run_id, raw_paper, clusters, review_actions)
    artifacts = paper["artifacts"]
    note_path = artifacts.get("note_markdown")
    note_content = read_text(Path(note_path)) if note_path else None

    ranking_explanation: list[str] = []
    breakdown = paper["score_breakdown"]
    if breakdown["primary_hits"]:
        ranking_explanation.append(f"Primary keyword hits: {', '.join(breakdown['primary_hits'][:8])}")
    if breakdown["secondary_hits"]:
        ranking_explanation.append(f"Secondary keyword hits: {', '.join(breakdown['secondary_hits'][:8])}")
    if breakdown["negative_hits"]:
        ranking_explanation.append(f"Negative keyword hits: {', '.join(breakdown['negative_hits'][:8])}")
    if breakdown["xr_bonus"]:
        ranking_explanation.append("XR title bonus applied")
    if breakdown["realtime_bonus"]:
        ranking_explanation.append("Real-time/system bonus applied")
    if breakdown["pdf_bonus"]:
        ranking_explanation.append("PDF availability bonus applied")

    related = _related_papers(paper, [_build_paper_list_item(run_id, item, clusters, review_actions) for item in raw_items], clusters)

    return {
        "run_id": run_id,
        "paper": paper,
        "abstract": raw_paper.get("abstract", ""),
        "note_content": note_content,
        "note_path": note_path,
        "ranking_explanation": ranking_explanation,
        "related_papers": related,
    }

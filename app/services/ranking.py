from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.constants import LEGACY_QUEUE_ROOT, PROJECT_ROOT
from app.repositories.files import read_jsonl, write_text
from app.services import paper_occurrences, paper_review

XR_LEGACY_TERMS = {
    "xr",
    "ar",
    "vr",
    "mr",
    "augmented reality",
    "virtual reality",
    "mixed reality",
    "head-mounted",
    "hmd",
    "pose",
    "tracking",
    "visual inertial",
    "visual-inertial",
    "vio",
    "slam",
    "localization",
    "mapping",
    "egocentric",
    "first-person",
    "inside-out",
    "spatial computing",
    "nerf",
    "gaussian splatting",
    "novel view synthesis",
    "3d reconstruction",
    "foveated",
    "eye tracking",
    "gaze",
    "hand tracking",
    "gesture",
    "avatar",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_themes(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = _load_json(path)
    return {str(key): [str(item).lower() for item in value] for key, value in data.items()}


def infer_theme(item: dict[str, Any], themes: dict[str, list[str]]) -> str:
    blob = f"{item.get('title', '')} {item.get('abstract', '')}".lower()
    best_theme = ""
    best_score = 0
    for theme, keywords in themes.items():
        score = sum(1 for keyword in keywords if keyword in blob)
        if score > best_score:
            best_theme = theme
            best_score = score
    return best_theme or item.get("theme") or "Uncategorized"


def _canonical_term(value: str) -> str:
    return paper_review.canonical_keyword(value)


def _topic_allows_xr_boost(topic_profile: dict[str, Any] | None) -> bool:
    if not topic_profile:
        return True
    profile_blob = _canonical_term(
        " ".join(
            [
                str(topic_profile.get("title") or ""),
                str(topic_profile.get("topic") or ""),
                " ".join(str(query) for query in topic_profile.get("queries") or []),
            ]
        )
    )
    return any(term in profile_blob for term in ("xr", "augmented reality", "virtual reality", "mixed reality", "spatial computing"))


def _topic_keyword_allowed(value: str, topic_profile: dict[str, Any] | None) -> bool:
    if not topic_profile:
        return True
    canonical = _canonical_term(value)
    if not paper_review.is_quality_keyword(value):
        return False
    if not _topic_allows_xr_boost(topic_profile) and canonical in XR_LEGACY_TERMS:
        return False
    return True


def _contains_term(blob: str, value: str, *, topic_aware: bool) -> bool:
    if not topic_aware:
        return value in blob
    canonical = _canonical_term(value)
    if len(value) <= 3 and re.fullmatch(r"[a-z0-9]+", value):
        return re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", blob) is not None
    if len(canonical) <= 3 and re.fullmatch(r"[a-z0-9]+", canonical):
        return re.search(rf"(?<![a-z0-9]){re.escape(canonical)}(?![a-z0-9])", _canonical_term(blob)) is not None
    return value in blob or canonical in _canonical_term(blob)


def _keyword_hits(values: list[str], blob: str, topic_profile: dict[str, Any] | None) -> list[str]:
    hits: list[str] = []
    for value in values:
        if not _topic_keyword_allowed(value, topic_profile):
            continue
        if _contains_term(blob, value, topic_aware=topic_profile is not None):
            hits.append(value)
    return hits


def score_item(
    item: dict[str, Any],
    keywords: dict[str, Any],
    themes: dict[str, list[str]] | None = None,
    *,
    topic_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = (item.get("title") or "").lower()
    abstract = (item.get("abstract") or "").lower()
    blob = f"{title} {abstract}"

    primary = [value.lower() for value in keywords.get("primary", []) if value.strip()]
    secondary = [value.lower() for value in keywords.get("secondary", []) if value.strip()]
    negative = [value.lower() for value in keywords.get("negative", []) if value.strip()]

    hits_primary = _keyword_hits(primary, blob, topic_profile)
    hits_secondary = _keyword_hits(secondary, blob, topic_profile)
    hits_negative = _keyword_hits(negative, blob, topic_profile)

    score = 0
    score += min(40, 4 * len(set(hits_primary)))
    score += min(20, 2 * len(set(hits_secondary)))
    score -= min(30, 6 * len(set(hits_negative)))

    xr_title_terms = ["xr", "ar", "vr", "mixed reality", "augmented reality", "virtual reality", "head-mounted"]
    if _topic_allows_xr_boost(topic_profile) and any(term in title for term in xr_title_terms):
        score += 12

    realtime_terms = ["real-time", "latency", "on-device", "edge", "mobile", "efficient"]
    if any(term in blob for term in realtime_terms):
        score += 6

    if item.get("pdf_url"):
        score += 2
    topic_relevance_score = 0.0
    if topic_profile is not None:
        topic_relevance_score = paper_review.score_topic_relevance(topic_profile, item)
        score += min(60, int(round(topic_relevance_score * 0.55)))

    scored = dict(item)
    scored["score"] = max(0, min(100, score))
    if topic_profile is not None:
        scored["topic_relevance_score"] = topic_relevance_score
    scored["theme"] = infer_theme(item, themes or {})
    scored["triage"] = {
        "hits_primary": sorted(set(hits_primary)),
        "hits_secondary": sorted(set(hits_secondary)),
        "hits_negative": sorted(set(hits_negative)),
        "theme": scored["theme"],
        "scored_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if topic_profile is not None:
        scored["triage"]["topic_relevance_score"] = topic_relevance_score
    return scored


def _write_triage_markdown(path: Path, items: list[dict[str, Any]], top_n: int = 25) -> None:
    lines: list[str] = []
    lines.append(f"# XRML Triage Results ({datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC)\n")
    lines.append(f"- Total: {len(items)}\n")
    lines.append("\n## Top papers\n")
    for index, item in enumerate(items[:top_n], start=1):
        title = item.get("title", "").strip()
        paper_id = item.get("paper_id", "")
        score = item.get("score", 0)
        url = item.get("entry_url") or item.get("entry_id", "")
        published = item.get("published", "")
        theme = item.get("theme", "Uncategorized")
        hits = item.get("triage", {}).get("hits_primary", [])[:8]
        lines.append(
            f"{index}. **[{score:>3}]** [{paper_id}] {title}\n"
            f"   - published: {published}\n"
            f"   - theme: {theme}\n"
            f"   - url: {url}\n"
            f"   - hits: {', '.join(hits) if hits else '(none)'}\n"
        )
    lines.append("\n## Notes\n")
    lines.append("- 점수는 현재 레거시 휴리스틱을 그대로 노출합니다.\n")
    lines.append("- `theme`는 config/themes.json 기반의 얕은 분류이며, 품질 개선은 후속 단계입니다.\n")
    write_text(path, "\n".join(lines))


def rank_from_paths(
    *,
    input_path: Path,
    keywords_path: Path,
    output_jsonl: Path,
    output_md: Path,
    themes_path: Path | None = None,
    mirror_legacy: bool = False,
    topic_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    keywords = _load_json(keywords_path)
    themes = _load_themes(themes_path or (PROJECT_ROOT / "config" / "themes.json"))
    items = read_jsonl(input_path)
    scored = [score_item(item, keywords, themes, topic_profile=topic_profile) for item in items]
    scored.sort(key=lambda item: (item.get("score", 0), item.get("published", "")), reverse=True)

    scored = paper_occurrences.write_triage_records(output_jsonl, scored)
    _write_triage_markdown(output_md, scored)

    legacy_paths: dict[str, str] = {}
    legacy_mirror_errors: list[str] = []
    if mirror_legacy:
        legacy_jsonl = LEGACY_QUEUE_ROOT / "triage.jsonl"
        legacy_md = LEGACY_QUEUE_ROOT / "triage.md"
        try:
            paper_occurrences.write_triage_records(legacy_jsonl, scored)
            _write_triage_markdown(legacy_md, scored)
            legacy_paths = {"output_jsonl": str(legacy_jsonl), "output_md": str(legacy_md)}
        except OSError as exc:
            legacy_mirror_errors.append(str(exc))

    return {
        "status": "ok",
        "count": len(scored),
        "input": str(input_path),
        "output_jsonl": str(output_jsonl),
        "output_md": str(output_md),
        "items": scored,
        "legacy_paths": legacy_paths,
        "legacy_mirror_errors": legacy_mirror_errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/queues/inbox.jsonl")
    parser.add_argument("--keywords", default="config/keywords.json")
    parser.add_argument("--output_jsonl", default="data/queues/triage.jsonl")
    parser.add_argument("--output_md", default="data/queues/triage.md")
    args = parser.parse_args(argv)

    result = rank_from_paths(
        input_path=Path(args.input),
        keywords_path=Path(args.keywords),
        output_jsonl=Path(args.output_jsonl),
        output_md=Path(args.output_md),
        mirror_legacy=False,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "items"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

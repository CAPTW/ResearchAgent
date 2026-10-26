from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domain import constants
from app.repositories import paper_reviews
from app.repositories import runs as run_repository
from app.services import paper_review, topic_profiles, workbench

ARTIFACT_GLOBS = {
    "text_txt": "*.txt",
    "pages_json": "*.pages.json",
    "pdf": "*.pdf",
    "note_markdown": "*.md",
}


def _artifact_kind(path: Path) -> tuple[str, str] | None:
    name = path.name
    if name.endswith(".pages.json"):
        return "pages_json", name[: -len(".pages.json")]
    if path.suffix == ".txt":
        return "text_txt", path.stem
    if path.suffix == ".pdf":
        return "pdf", path.stem
    if path.suffix == ".md":
        return "note_markdown", path.stem
    return None


def _normalize_whitespace(value: str) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def _read_text_char_count(path: Path) -> int:
    try:
        if path.name.endswith(".pages.json"):
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            pages = payload.get("pages") if isinstance(payload, dict) else payload
            parts: list[str] = []
            if isinstance(pages, list):
                for page in pages:
                    if isinstance(page, dict):
                        parts.append(str(page.get("text") or ""))
                    else:
                        parts.append(str(page or ""))
            return len(_normalize_whitespace(" ".join(parts)))
        if path.suffix in {".txt", ".md"}:
            return len(_normalize_whitespace(path.read_text(encoding="utf-8", errors="ignore")))
    except Exception:
        return 0
    return 0


def _add_artifact(groups: dict[str, dict[str, Any]], path: Path, *, run_id: str | None = None) -> None:
    parsed = _artifact_kind(path)
    if parsed is None:
        return
    kind, paper_id = parsed
    normalized_paper_id = paper_review.normalize_paper_id(paper_id)
    if not normalized_paper_id:
        return
    entry = groups.setdefault(
        normalized_paper_id,
        {
            "paper_id": paper_id,
            "normalized_paper_id": normalized_paper_id,
            "artifact_paths": {
                "text_txt": [],
                "pages_json": [],
                "pdf": [],
                "note_markdown": [],
            },
            "seen_run_ids": [],
        },
    )
    path_text = str(path)
    if path_text not in entry["artifact_paths"][kind]:
        entry["artifact_paths"][kind].append(path_text)
    if run_id and run_id not in entry["seen_run_ids"]:
        entry["seen_run_ids"].append(run_id)


def _scan_artifact_groups() -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for run in run_repository.list_run_records(limit=None):
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            continue
        paths = run_repository.build_run_paths(run_id)
        for pattern in ("*.txt", "*.pages.json", "*.pdf"):
            if paths.raw_dir.exists():
                for path in paths.raw_dir.glob(pattern):
                    _add_artifact(groups, path, run_id=run_id)
        notes_dir = paths.notes_dir / "papers"
        if notes_dir.exists():
            for path in notes_dir.glob("*.md"):
                _add_artifact(groups, path, run_id=run_id)

    legacy_locations = [
        (constants.LEGACY_TEXT_ROOT, "*.txt"),
        (constants.LEGACY_TEXT_ROOT, "*.pages.json"),
        (constants.LEGACY_PDF_ROOT, "*.pdf"),
        (constants.LEGACY_NOTES_ROOT / "papers", "*.md"),
    ]
    for root, pattern in legacy_locations:
        if root.exists():
            for path in root.glob(pattern):
                _add_artifact(groups, path)
    return groups


def _metadata_by_normalized_id(target_ids: set[str]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    if not target_ids:
        return metadata
    for run in run_repository.list_run_records(limit=None):
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            continue
        try:
            items = workbench.load_run_triage_items(run_id)
        except FileNotFoundError:
            continue
        for item in items:
            paper_id = str(item.get("paper_id") or item.get("entry_id") or "").strip()
            normalized_paper_id = paper_review.normalize_paper_id(paper_id)
            if normalized_paper_id not in target_ids:
                continue
            candidate = {
                "paper_id": paper_id,
                "title": str(item.get("title") or "").strip(),
                "abstract": str(item.get("abstract") or item.get("summary") or "").strip(),
                "source_provider": str(item.get("source_provider") or item.get("provider") or item.get("source") or "").strip(),
                "published_at": str(item.get("published") or item.get("published_at") or item.get("updated") or "").strip(),
                "pdf_url": str(item.get("pdf_url") or "").strip(),
                "best_run_id": run_id,
                "item": dict(item),
            }
            existing = metadata.get(normalized_paper_id)
            if existing is None or (
                len(candidate["abstract"]),
                len(candidate["title"]),
                candidate["published_at"],
            ) > (
                len(str(existing.get("abstract") or "")),
                len(str(existing.get("title") or "")),
                str(existing.get("published_at") or ""),
            ):
                metadata[normalized_paper_id] = candidate
    return metadata


def _topic_profile_list(topic_id: str | None) -> list[dict[str, Any]]:
    profiles = list(topic_profiles.list_topic_profiles().get("items") or [])
    if topic_id and all(profile.get("profile_id") != topic_id for profile in profiles):
        try:
            profiles.append(topic_profiles.get_topic_profile(topic_id))
        except FileNotFoundError:
            pass
    return profiles


def _reviews_by_normalized_id(topic_id: str | None) -> dict[str, dict[str, Any]]:
    reviews = paper_reviews.list_reviews(topic_id=topic_id, limit=100_000)
    result: dict[str, dict[str, Any]] = {}
    for review in reviews:
        key = paper_review.normalize_paper_id(review.get("paper_id"))
        if not key:
            continue
        existing = result.get(key)
        if existing is None or (
            str(review.get("full_text_status") or ""),
            str(review.get("reviewed_at") or ""),
        ) > (
            str(existing.get("full_text_status") or ""),
            str(existing.get("reviewed_at") or ""),
        ):
            result[key] = review
    return result


def _artifact_text_char_count(paths_by_type: dict[str, list[str]]) -> int:
    for kind in ("text_txt", "pages_json", "note_markdown"):
        for path_value in paths_by_type.get(kind) or []:
            count = _read_text_char_count(Path(path_value))
            if count:
                return count
    return 0


def inventory_full_text_artifacts(
    *,
    topic_id: str | None = None,
    min_relevance: float | None = None,
    only_with_text: bool = False,
    only_with_pdf: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    groups = _scan_artifact_groups()
    metadata = _metadata_by_normalized_id(set(groups))
    profiles = _topic_profile_list(topic_id)
    reviews_by_id = _reviews_by_normalized_id(topic_id)
    items: list[dict[str, Any]] = []

    for normalized_paper_id, group in groups.items():
        paths_by_type = {
            key: sorted(values)
            for key, values in dict(group.get("artifact_paths") or {}).items()
            if values
        }
        has_text = any(paths_by_type.get(kind) for kind in ("text_txt", "pages_json", "note_markdown"))
        has_pdf = bool(paths_by_type.get("pdf"))
        if only_with_text and not has_text:
            continue
        if only_with_pdf and not has_pdf:
            continue

        meta = metadata.get(normalized_paper_id, {})
        item_for_score = dict(meta.get("item") or {})
        if not item_for_score:
            item_for_score = {
                "paper_id": group.get("paper_id"),
                "title": meta.get("title") or group.get("paper_id"),
                "abstract": meta.get("abstract") or "",
            }
        scores = {
            str(profile.get("profile_id") or ""): paper_review.score_topic_relevance(profile, item_for_score)
            for profile in profiles
            if str(profile.get("profile_id") or "").strip()
        }
        selected_score = float(scores.get(topic_id or "", 0.0)) if topic_id else (max(scores.values()) if scores else 0.0)
        if min_relevance is not None and selected_score < float(min_relevance):
            continue
        best_matching_topic = max(scores, key=scores.get) if scores else None
        review = reviews_by_id.get(normalized_paper_id)
        items.append(
            {
                "paper_id": str(meta.get("paper_id") or group.get("paper_id") or normalized_paper_id),
                "normalized_paper_id": normalized_paper_id,
                "title": str(meta.get("title") or "").strip() or None,
                "source_provider": str(meta.get("source_provider") or "").strip() or None,
                "best_run_id": str(meta.get("best_run_id") or "").strip() or None,
                "seen_run_ids": sorted(group.get("seen_run_ids") or []),
                "published_at": str(meta.get("published_at") or "").strip() or None,
                "pdf_url": str(meta.get("pdf_url") or "").strip() or None,
                "artifact_types": sorted(paths_by_type),
                "artifact_paths": paths_by_type,
                "text_char_count": _artifact_text_char_count(paths_by_type),
                "topic_relevance_scores": scores,
                "topic_relevance_score": selected_score,
                "best_matching_topic": best_matching_topic,
                "paper_review_exists": review is not None,
                "current_full_text_status": str(review.get("full_text_status") or "") if review else None,
            }
        )

    items.sort(
        key=lambda item: (
            float(item.get("topic_relevance_score") or 0),
            int(item.get("text_char_count") or 0),
            str(item.get("normalized_paper_id") or ""),
        ),
        reverse=True,
    )
    filtered_count = len(items)
    if limit is not None:
        items = items[: max(0, int(limit))]
    return {
        "topic_id": topic_id,
        "min_relevance": min_relevance,
        "only_with_text": bool(only_with_text),
        "only_with_pdf": bool(only_with_pdf),
        "limit": limit,
        "total_artifact_candidates": filtered_count,
        "returned_count": len(items),
        "items": items,
    }

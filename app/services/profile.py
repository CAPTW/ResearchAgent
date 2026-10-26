from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from app.domain.constants import PROJECT_ROOT
from app.repositories.files import ensure_dir, read_json, safe_filename, write_json
from app.services import research_domain, research_policy, source_config


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_default_profile() -> dict[str, Any]:
    sources_path = PROJECT_ROOT / "config" / "sources.json"
    keywords_path = PROJECT_ROOT / "config" / "keywords.json"
    themes_path = PROJECT_ROOT / "config" / "themes.json"
    return {
        "topic": "XR/AR × ML (Image/Video)",
        "sources": _load_json(sources_path),
        "keywords": _load_json(keywords_path),
        "themes": _load_json(themes_path),
        "paths": {
            "sources": str(sources_path),
            "keywords": str(keywords_path),
            "themes": str(themes_path),
        },
    }


def load_profile_snapshot(path: Path) -> dict[str, Any]:
    payload = read_json(path, default=None)
    if not isinstance(payload, dict):
        raise FileNotFoundError(f"Profile snapshot not found or invalid: {path}")
    return payload


def build_run_profile(request_payload: dict[str, Any], *, base_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = deepcopy(base_profile) if base_profile is not None else load_default_profile()
    topic = (request_payload.get("topic") or profile["topic"]).strip()
    queries = [query.strip() for query in request_payload.get("queries", []) if query and query.strip()]
    categories = [category.strip() for category in request_payload.get("categories", []) if category and category.strip()]
    source_provider = str(request_payload.get("source_provider") or "arxiv").strip() or "arxiv"
    base_arxiv = dict(profile.get("sources", {}).get("arxiv", {}))
    source_settings = source_config.normalize_source_settings(
        request_payload.get("source_settings"),
        fallback={
            "queries": queries or list(base_arxiv.get("queries", [])),
            "categories": categories or list(base_arxiv.get("categories", [])),
            "max_results": request_payload.get("max_results") or base_arxiv.get("max_results"),
            "source_provider": source_provider,
            "arxiv_sort_by": request_payload.get("arxiv_sort_by") or base_arxiv.get("sort_by"),
            "arxiv_sort_order": request_payload.get("arxiv_sort_order") or base_arxiv.get("sort_order"),
        },
    )
    source_config.validate_source_settings(source_settings)

    profile["topic"] = topic or profile["topic"]
    profile["sources"] = source_config.run_sources_from_settings(source_settings, base_sources=profile.get("sources", {}))
    profile["source_provider"] = source_settings["defaultProvider"]
    profile["source_settings"] = source_settings
    profile["run_options"] = {
        "top_papers": int(request_payload.get("top_papers", 3)),
        "weekly_top": int(request_payload.get("weekly_top", 15)),
        "skip_scout": bool(request_payload.get("skip_scout", False)),
        "skip_summary": bool(request_payload.get("skip_summary", False)),
        "skip_synthesis": bool(request_payload.get("skip_synthesis", False)),
        "experiment": bool(request_payload.get("experiment", False)),
        "rank": int(request_payload.get("rank", 1)),
    }
    inherited_context = None
    if base_profile is not None and isinstance(base_profile.get("domain_context"), dict):
        inherited_context = research_domain.research_domain_context_from_snapshot(
            base_profile["domain_context"]
        )
    inherited_same_topic = bool(
        inherited_context
        and inherited_context.topic.casefold() == profile["topic"].casefold()
    )
    context_profile = {
        "title": inherited_context.display_name if inherited_same_topic else profile["topic"],
        "topic": profile["topic"],
        "description": inherited_context.description if inherited_same_topic else "",
        "queries": (
            request_payload.get("queries", [])
            or (list(inherited_context.search_queries) if inherited_same_topic else [])
        ),
        "focus_terms": list(inherited_context.focus_terms) if inherited_same_topic else [],
        "research_questions": (
            list(inherited_context.research_questions) if inherited_same_topic else []
        ),
    }
    profile["domain_context"] = research_domain.build_research_domain_context(
        profile=context_profile,
        source=research_domain.DomainContextSource.RUN_PROFILE_SNAPSHOT,
    ).to_snapshot()
    # Resolve the persisted per-topic research policy at the same boundary as
    # the run profile. Keeping the normalized fields on the immutable run
    # snapshot makes the next-run/research-plan resolver observe exactly what
    # the topic settings saved, without adding a second store or migration.
    policy = research_policy.normalize_research_policy(request_payload, current=profile)
    profile.update(policy)
    profile["research_policy"] = deepcopy(policy)
    return profile


def snapshot_profile(inputs_dir: Path, profile: dict[str, Any]) -> dict[str, str]:
    ensure_dir(inputs_dir)
    topic_slug = safe_filename(profile.get("topic", "profile"))
    sources_path = inputs_dir / f"{topic_slug}.sources.json"
    keywords_path = inputs_dir / f"{topic_slug}.keywords.json"
    profile_path = inputs_dir / "profile.json"

    write_json(sources_path, profile["sources"])
    write_json(keywords_path, profile["keywords"])
    write_json(profile_path, profile)

    return {
        "profile": str(profile_path),
        "sources": str(sources_path),
        "keywords": str(keywords_path),
    }

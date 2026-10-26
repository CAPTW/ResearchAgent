from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from app.auth.resource_access import (
    ResourceActor,
    resource_actor_context,
    resolve_resource_actor,
    resource_visible_to_actor,
)
from app.db.bootstrap import connect_metadata_index
from app.domain.topic_membership import run_matches_topic
from app.repositories import ai_review_drafts, metadata_index, paper_reviews
from app.repositories.files import iso_now_utc, read_json
from app.repositories.runs import list_run_records
from app.services import (
    auth_foundation,
    provider_review_coverage,
    provider_configurations,
    paper_review as paper_review_service,
    research_operations_rc_audit,
    recommendations,
    report_hub,
    source_config,
    topic_analytics,
    topic_configurations,
    topic_profiles,
    topic_schedules,
    workbench,
)

CORE_PAPER_SCORE_THRESHOLD = 50
CITATION_FIELDS = (
    "citations",
    "citation_count",
    "citationCount",
    "cited_by_count",
    "citedByCount",
    "num_citations",
    "numCitations",
    "influential_citation_count",
    "influentialCitationCount",
)
CITATION_CONTAINER_FIELDS = (
    "metadata",
    "metrics",
    "citation_metrics",
    "openalex",
    "semantic_scholar",
    "semanticscholar",
    "crossref",
)
INSTITUTION_FIELDS = (
    "institutions",
    "institution",
    "affiliations",
    "affiliation",
    "organizations",
    "organization",
    "raw_affiliation_string",
)
RESEARCH_OPERATIONS_OUTPUT_ROOT = Path("output")


def _select_profile(
    profile_id: str | None,
    *,
    actor: ResourceActor | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    profiles = topic_profiles.list_topic_profiles(actor=actor)["items"]
    active_profiles = [profile for profile in profiles if str(profile.get("status") or "active") != "archived"]
    if not profiles:
        return profiles, None
    if profile_id:
        selected = next((profile for profile in profiles if profile.get("profile_id") == profile_id), None)
        if selected is not None:
            return profiles, selected
    visible_profiles = active_profiles or profiles
    llm_profile = next(
        (
            profile
            for profile in visible_profiles
            if "large language" in str(profile.get("topic", "")).lower()
            or "large language" in str(profile.get("title", "")).lower()
        ),
        None,
    )
    return profiles, llm_profile or visible_profiles[0]


def _get_profile_or_raise(
    profile_id: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    try:
        return topic_profiles.get_topic_profile(profile_id, actor=actor)
    except FileNotFoundError:
        raise FileNotFoundError(f"Topic not found: {profile_id}")


def _api_topic(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(profile.get("profile_id", "")),
        "name": str(profile.get("title") or profile.get("topic") or profile.get("profile_id") or ""),
        "description": profile.get("description"),
        "updatedAt": str(profile.get("updated_at") or profile.get("created_at") or ""),
        "isFavorite": bool(profile.get("is_favorite") or profile.get("isFavorite") or False),
        "status": "archived" if str(profile.get("status") or "").strip().lower() == "archived" else "active",
    }


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        normalized = re.sub(r"[^\d.\-]", "", stripped.replace(",", ""))
        if not normalized or normalized in {".", "-", "-."}:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _coerce_count(value: Any) -> int | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return max(0, int(round(number)))


def _metadata_containers(item: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    pending = [item]
    seen: set[int] = set()
    while pending:
        container = pending.pop(0)
        container_id = id(container)
        if container_id in seen:
            continue
        seen.add(container_id)
        containers.append(container)
        for field in CITATION_CONTAINER_FIELDS:
            raw_value = container.get(field)
            if isinstance(raw_value, dict):
                pending.append(raw_value)
    return containers


def _citation_count(item: dict[str, Any]) -> int:
    for container in _metadata_containers(item):
        for field in CITATION_FIELDS:
            value = _coerce_count(container.get(field))
            if value is not None:
                return value
    return 0


def _author_name(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for field in ("name", "full_name", "display_name", "author", "literal"):
            text = str(value.get(field) or "").strip()
            if text:
                return text
    return None


def _item_authors(item: dict[str, Any]) -> list[str]:
    raw_authors = item.get("authors") or item.get("author") or item.get("creators")
    if raw_authors is None and isinstance(item.get("metadata"), dict):
        metadata = item["metadata"]
        raw_authors = metadata.get("authors") or metadata.get("author") or metadata.get("creators")

    if isinstance(raw_authors, list):
        return [name for author in raw_authors if (name := _author_name(author))]
    if isinstance(raw_authors, str):
        separator = ";" if ";" in raw_authors else "|" if "|" in raw_authors else None
        if separator:
            return [name for part in raw_authors.split(separator) if (name := _author_name(part))]
        return [raw_authors.strip()] if raw_authors.strip() else []
    return []


def _source_provider_for_run(run: dict[str, Any]) -> str | None:
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    profile = run.get("profile") if isinstance(run.get("profile"), dict) else {}
    source_settings = request.get("source_settings") if isinstance(request.get("source_settings"), dict) else {}
    provider = str(source_settings.get("defaultProvider") or request.get("source_provider") or profile.get("source_provider") or "").strip()
    return provider or None


def _source_provider_for_item(run: dict[str, Any], item: dict[str, Any]) -> str | None:
    provider = str(item.get("source_provider") or item.get("source") or "").strip()
    return provider or _source_provider_for_run(run)


def _provider_key(run: dict[str, Any], item: dict[str, Any]) -> str:
    return _source_provider_for_item(run, item) or "unknown"


def _provider_label(provider: str) -> str:
    if provider == "unknown":
        return "출처 미확인"
    catalog = source_config.PROVIDER_CATALOG.get(provider)
    return str(catalog.get("label") or provider) if catalog else provider


def _institution_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;|]", value) if part.strip()]
    if isinstance(value, list):
        texts: list[str] = []
        for entry in value:
            texts.extend(_institution_texts(entry))
        return texts
    if isinstance(value, dict):
        for field in INSTITUTION_FIELDS + ("name", "display_name"):
            texts = _institution_texts(value.get(field))
            if texts:
                return texts
    return []


def _institution_values(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for container in _metadata_containers(item):
        for field in INSTITUTION_FIELDS:
            values.extend(_institution_texts(container.get(field)))

    raw_authors = item.get("authors")
    if raw_authors is None and isinstance(item.get("metadata"), dict):
        raw_authors = item["metadata"].get("authors")
    if isinstance(raw_authors, list):
        for author in raw_authors:
            if isinstance(author, dict):
                for field in INSTITUTION_FIELDS:
                    values.extend(_institution_texts(author.get(field)))

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        key = text.lower()
        if text and key not in seen:
            normalized.append(text)
            seen.add(key)
    return normalized


def _query_rows(sql: str, params: list[Any], *, read_only: bool = True) -> list[dict[str, Any]]:
    if read_only and not metadata_index.metadata_index_path(ensure_parent=False).exists():
        return []
    with connect_metadata_index(read_only=read_only) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _topic_sql_scope(
    topic: str,
    *,
    actor: ResourceActor | None,
    column: str,
    profile_id: str | None = None,
) -> tuple[str, list[str]]:
    resolved_actor = resolve_resource_actor(actor)
    if profile_id is None and resolved_actor.auth_mode == "disabled":
        topic_column = "r.topic" if column.startswith("r.") else "topic"
        return f"{topic_column} = ?", [topic]
    run_ids = [
        str(run.get("run_id") or "").strip()
        for run in _topic_run_records(topic, profile_id=profile_id, actor=resolved_actor)
        if str(run.get("run_id") or "").strip()
    ]
    if not run_ids:
        return "1 = 0", []
    placeholders = ", ".join("?" for _ in run_ids)
    return f"{column} IN ({placeholders})", run_ids


def _latest_papers(
    topic: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> list[dict[str, Any]]:
    scope_sql, scope_params = _topic_sql_scope(
        topic,
        profile_id=profile_id,
        actor=actor,
        column="r.run_id",
    )
    return _query_rows(
        f"""
        SELECT p.run_id, p.paper_id, p.title, p.theme, p.score, p.status, p.published
        FROM papers p
        INNER JOIN runs r ON r.run_id = p.run_id
        WHERE {scope_sql}
        ORDER BY COALESCE(p.published, '') DESC, p.score DESC
        LIMIT 8
        """,
        scope_params,
    )


def _distribution(
    topic: str,
    field: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> list[dict[str, Any]]:
    if field not in {"theme", "status"}:
        raise RuntimeError(f"Unsupported distribution field: {field}")
    scope_sql, scope_params = _topic_sql_scope(
        topic,
        profile_id=profile_id,
        actor=actor,
        column="r.run_id",
    )
    return [
        {"label": str(row["label"]), "count": int(row["count"] or 0)}
        for row in _query_rows(
            f"""
            SELECT p.{field} AS label, COUNT(*) AS count
            FROM papers p
            INNER JOIN runs r ON r.run_id = p.run_id
            WHERE {scope_sql}
            GROUP BY p.{field}
            ORDER BY count DESC, label ASC
            LIMIT 8
            """,
            scope_params,
        )
    ]


def _trend(
    topic: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> list[dict[str, Any]]:
    scope_sql, scope_params = _topic_sql_scope(
        topic,
        profile_id=profile_id,
        actor=actor,
        column="r.run_id",
    )
    paper_rows = _query_rows(
        f"""
        SELECT substr(p.published, 1, 7) AS label, COUNT(*) AS count
        FROM papers p
        INNER JOIN runs r ON r.run_id = p.run_id
        WHERE {scope_sql} AND COALESCE(p.published, '') != ''
        GROUP BY substr(p.published, 1, 7)
        ORDER BY label ASC
        LIMIT 12
        """,
        scope_params,
    )
    if paper_rows:
        return [{"label": str(row["label"]), "count": int(row["count"] or 0)} for row in paper_rows]
    run_scope_sql, run_scope_params = _topic_sql_scope(
        topic,
        profile_id=profile_id,
        actor=actor,
        column="run_id",
    )
    run_rows = _query_rows(
        f"""
        SELECT substr(created_at, 1, 10) AS label, COUNT(*) AS count
        FROM runs
        WHERE {run_scope_sql}
        GROUP BY substr(created_at, 1, 10)
        ORDER BY label ASC
        LIMIT 12
        """,
        run_scope_params,
    )
    return [{"label": str(row["label"]), "count": int(row["count"] or 0)} for row in run_rows]


def _recent_runs(
    topic: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> list[dict[str, Any]]:
    scope_sql, scope_params = _topic_sql_scope(
        topic,
        profile_id=profile_id,
        actor=actor,
        column="run_id",
    )
    return _query_rows(
        f"""
        SELECT run_id, topic, status, created_at, updated_at, triage_items, reviewed_items, evidence_count, claim_count
        FROM runs
        WHERE {scope_sql}
        ORDER BY created_at DESC
        LIMIT 8
        """,
        scope_params,
    )


def _recent_reports(
    topic_id: str,
    topic: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    return report_hub.build_api_reports(
        _topic_run_records(topic, profile_id=topic_id, actor=actor),
        topic_id=topic_id,
        limit=6,
    )


def _topic_run_records(
    topic: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> list[dict[str, Any]]:
    resolved_actor = resolve_resource_actor(actor)
    runs = [run for run in list_run_records(limit=None) if run_matches_topic(run, topic, profile_id)]
    if resolved_actor.auth_mode != "disabled":
        owner_user_id = (
            resolved_actor.scoped_owner_user_id
            if resolved_actor.actor_type == "system"
            else resolved_actor.subject
        )
        runs = [
            run
            for run in runs
            if str(run.get("owner_user_id") or "").strip() == str(owner_user_id or "")
        ]
    runs.sort(key=lambda run: str(run.get("created_at", "")), reverse=True)
    return runs


def _topic_items(
    topic: str,
    *,
    profile_id: str | None = None,
    max_runs: int = 8,
    actor: ResourceActor | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in _topic_run_records(topic, profile_id=profile_id, actor=actor)[:max_runs]:
        run_id = str(run.get("run_id", ""))
        if not run_id:
            continue
        try:
            for item in workbench.load_run_triage_items(run_id):
                items.append((run, item))
        except FileNotFoundError:
            continue
    return items


def _last_success_by_provider(
    topic: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> dict[str, str | None]:
    last_success: dict[str, str | None] = {}
    for run in _topic_run_records(topic, profile_id=profile_id, actor=actor):
        request = run.get("request") if isinstance(run.get("request"), dict) else {}
        if run.get("status") != "completed" or request.get("skip_scout"):
            continue
        provider = _source_provider_for_run(run)
        if provider not in source_config.SUPPORTED_PROVIDER_IDS or provider in last_success:
            continue
        last_success[provider] = str(run.get("finished_at") or run.get("updated_at") or run.get("created_at") or "") or None
    return last_success


def _recent_source_counts(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for run, item in items:
        paper_id = str(item.get("paper_id", "")).strip()
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        provider = _source_provider_for_item(run, item)
        if provider:
            counts[provider] += 1
    return dict(counts)


def _provider_counts(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for run, item in items:
        paper_id = str(item.get("paper_id", "")).strip()
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        counts[_provider_key(run, item)] += 1
    return [
        {
            "provider": provider,
            "label": _provider_label(provider),
            "paperCount": count,
            "lastSuccessfulCollectionAt": None,
        }
        for provider, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _paper_label(run: dict[str, Any], item: dict[str, Any]) -> str:
    published = str(item.get("published") or item.get("updated") or "").strip()
    if len(published) >= 7:
        return published[:7]
    created_at = str(run.get("created_at") or "").strip()
    return created_at[:7] if len(created_at) >= 7 else "unknown"


def _series_delta(values: list[float]) -> float:
    if len(values) < 2:
        return 0
    return round(values[-1] - values[-2], 2)


def _delta_percent(values: list[float]) -> float:
    if len(values) < 2 or values[-2] == 0:
        return 0
    return round(((values[-1] - values[-2]) / values[-2]) * 100, 2)


def _build_contract_trends(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, list[float] | int]] = {}
    for run, item in items:
        label = _paper_label(run, item)
        bucket = buckets.setdefault(label, {"total": 0, "core": 0, "scores": [], "citations": 0})
        bucket["total"] = int(bucket["total"]) + 1
        score = float(item.get("score", 0) or 0)
        if score >= CORE_PAPER_SCORE_THRESHOLD:
            bucket["core"] = int(bucket["core"]) + 1
        bucket["citations"] = int(bucket["citations"]) + _citation_count(item)
        scores = bucket["scores"]
        if isinstance(scores, list):
            scores.append(score)

    labels = sorted(label for label in buckets if label != "unknown")[-12:]
    if not labels and "unknown" in buckets:
        labels = ["unknown"]

    total_papers = [int(buckets[label]["total"]) for label in labels]
    core_papers = [int(buckets[label]["core"]) for label in labels]
    avg_impact: list[float] = []
    for label in labels:
        scores = buckets[label]["scores"]
        avg_impact.append(round(sum(scores) / len(scores), 2) if isinstance(scores, list) and scores else 0)

    citations = [int(buckets[label]["citations"]) for label in labels]
    return {
        "labels": labels,
        "totalPapers": total_papers,
        "corePapers": core_papers,
        "averageImpact": avg_impact,
        "citations": citations,
    }


def _metric(value: float, sparkline: list[float]) -> dict[str, Any]:
    return {
        "value": round(value, 2),
        "delta": _series_delta(sparkline),
        "sparkline": [round(point, 2) for point in sparkline],
    }


def _contract_metrics(
    *,
    overview: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    trends: dict[str, Any],
) -> dict[str, Any]:
    scores = [float(item.get("score", 0) or 0) for _, item in items]
    total_papers = int(overview.get("totals", {}).get("papers", 0) or len(items))
    core_count = sum(1 for score in scores if score >= CORE_PAPER_SCORE_THRESHOLD)
    average_impact = round(sum(scores) / len(scores), 2) if scores else 0
    total_series = [float(value) for value in trends.get("totalPapers", [])]
    core_series = [float(value) for value in trends.get("corePapers", [])]
    impact_series = [float(value) for value in trends.get("averageImpact", [])]
    citation_series = [float(value) for value in trends.get("citations", [])]
    total_citations = sum(_citation_count(item) for _, item in items)
    trend_delta = _delta_percent(total_series)
    return {
        "collectedPapers": _metric(float(total_papers), total_series),
        "corePapers": _metric(float(core_count), core_series),
        "averageImpact": _metric(float(average_impact), impact_series),
        "totalCitations": _metric(float(total_citations), citation_series),
        "trendIndex": {
            "label": str(total_series[-1] if total_series else 0),
            "deltaPercent": trend_delta,
            "sparkline": total_series,
        },
    }


def _keywords_and_institutions(
    topic: str,
    *,
    profile_id: str | None = None,
    actor: ResourceActor | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    keyword_counter: Counter[str] = Counter()
    institution_counter: Counter[str] = Counter()
    warnings: list[str] = []

    topic_runs = _topic_run_records(topic, profile_id=profile_id, actor=actor)
    for run in topic_runs[:6]:
        try:
            items = workbench.load_run_triage_items(str(run["run_id"]))
        except FileNotFoundError:
            continue
        for item in items[:100]:
            triage = item.get("triage") or {}
            keyword_hits = item.get("keyword_hits") or {}
            for bucket in (
                triage.get("hits_primary") or [],
                triage.get("hits_secondary") or [],
                keyword_hits.get("primary") or [],
                keyword_hits.get("secondary") or [],
            ):
                for value in bucket:
                    keyword = str(value).strip().lower()
                    if len(keyword) >= 2:
                        keyword_counter[keyword] += 1

            for institution in _institution_values(item):
                institution_counter[institution] += 1

    if not institution_counter:
        warnings.append("기관/소속 메타데이터가 아직 수집되지 않아 기관 순위는 빈 상태로 표시됩니다.")

    return (
        [{"keyword": keyword, "count": count} for keyword, count in keyword_counter.most_common(12)],
        [{"name": name, "count": count} for name, count in institution_counter.most_common(8)],
        warnings,
    )


def _latest_core_papers(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    sorted_items = sorted(
        items,
        key=lambda pair: (
            float(pair[1].get("score", 0) or 0),
            str(pair[1].get("published") or pair[1].get("updated") or pair[0].get("created_at") or ""),
        ),
        reverse=True,
    )
    papers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run, item in sorted_items:
        paper_id = str(item.get("paper_id", "")).strip()
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        papers.append(
            {
                "id": paper_id,
                "title": str(item.get("title", "")).strip() or "Untitled paper",
                "authors": _item_authors(item),
                "publishedAt": item.get("published") or item.get("updated"),
                "citations": _citation_count(item),
                "impactScore": float(item.get("score", 0) or 0),
                "sourceUrl": item.get("entry_url") or item.get("entry_id") or item.get("pdf_url"),
                "sourceProvider": _source_provider_for_item(run, item),
                "saved": True,
            }
        )
        if len(papers) >= 8:
            break
    return papers


def _topic_paper_records(
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    q: str = "",
    provider: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = q.strip().lower()
    provider_filter = (provider or "").strip().lower()
    sorted_items = sorted(
        items,
        key=lambda pair: (
            float(pair[1].get("score", 0) or 0),
            str(pair[1].get("published") or pair[1].get("updated") or pair[0].get("created_at") or ""),
        ),
        reverse=True,
    )
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run, item in sorted_items:
        paper_id = str(item.get("paper_id", "")).strip()
        if not paper_id or paper_id in seen:
            continue
        item_provider = _source_provider_for_item(run, item)
        provider_key = item_provider or "unknown"
        if provider_filter and provider_key.lower() != provider_filter:
            continue
        authors = _item_authors(item)
        record = {
            "id": paper_id,
            "runId": str(run.get("run_id", "")).strip(),
            "title": str(item.get("title", "")).strip() or "Untitled paper",
            "authors": authors,
            "publishedAt": item.get("published") or item.get("updated"),
            "citations": _citation_count(item),
            "impactScore": float(item.get("score", 0) or 0),
            "sourceUrl": item.get("entry_url") or item.get("entry_id") or item.get("pdf_url"),
            "sourceProvider": item_provider,
            "saved": True,
            "theme": str(item.get("theme", "")).strip() or None,
            "status": str(item.get("status", "")).strip() or None,
        }
        if query:
            haystack = " ".join(
                value
                for value in (
                    record["title"],
                    " ".join(record["authors"]),
                    record["theme"] or "",
                    record["status"] or "",
                )
                if value
            ).lower()
            if query not in haystack:
                continue
        seen.add(paper_id)
        records.append(record)
        if len(records) >= limit:
            break
    return records


def _field_distribution(topic: str) -> list[dict[str, Any]]:
    rows = _distribution(topic, "theme")
    total = sum(row["count"] for row in rows) or 0
    return [
        {
            "label": row["label"],
            "count": row["count"],
            "percentage": round((row["count"] / total) * 100, 1) if total else 0,
        }
        for row in rows
    ]


def _contract_task(schedule: dict[str, Any]) -> dict[str, Any]:
    schedule = topic_schedules._schedule_view(schedule)
    return {
        "id": schedule.get("schedule_id", ""),
        "title": schedule.get("title", ""),
        "scheduleLabel": schedule.get("cadence_label", ""),
        "enabled": bool(schedule.get("enabled", False)),
        "taskType": schedule.get("task_type") or "collect_analyze",
        "lastRunAt": schedule.get("last_run_at"),
        "nextRunAt": None if schedule.get("timezone_error") else schedule.get("next_run_at"),
        "timezoneError": schedule.get("timezone_error"),
        "schedulerState": schedule.get("scheduler_state") or "paused",
        "currentRunStatus": schedule.get("current_run_status"),
        "lastStatus": schedule.get("last_status"),
        "lastError": schedule.get("last_error"),
    }


def contract_task(schedule: dict[str, Any]) -> dict[str, Any]:
    return _contract_task(schedule)


def _load_raw_topic_schedules_read_only() -> tuple[list[dict[str, Any]], list[str], str]:
    path = topic_schedules.SCHEDULE_LIBRARY_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], ["Persisted topic schedules artifact is missing; dashboard used in-memory defaults without writing."], "defaults"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [f"예약 작업 저장소를 읽지 못했습니다. 원본은 변경하지 않았습니다: {exc}"], "unavailable"
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        return [], ["예약 작업 저장소 형식이 올바르지 않습니다. 원본은 변경하지 않았습니다."], "unavailable"
    return payload, [], "ready"


def _list_topic_schedules_read_only(
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    warnings: list[str] = []
    try:
        backend_descriptor = topic_schedules.get_scheduler_backend().descriptor()
    except Exception as exc:
        backend_descriptor = {
            "mode": "unavailable",
            "storage_backend": "unavailable",
            "available": False,
            "read_only": True,
            "unavailable_reason": str(exc),
        }
        warnings.append(f"Topic scheduler backend descriptor could not be loaded in read-only mode: {exc}")

    lock_status = topic_schedules._lock_file_status()
    if lock_status.get("locked"):
        warnings.append("예약 작업 저장소가 잠겨 있어 목록을 읽지 못했습니다. 잠시 후 다시 불러오세요.")
        return {
            "items": [],
            "state": "unavailable",
            "backend": backend_descriptor,
            "degraded": True,
            "read_only": True,
            "warnings": warnings,
            "store_lock": lock_status,
        }

    raw_schedules, load_warnings, state = _load_raw_topic_schedules_read_only()
    warnings.extend(load_warnings)
    if state == "unavailable":
        return {
            "items": [], "state": state, "backend": backend_descriptor,
            "degraded": True, "read_only": True, "warnings": warnings,
        }
    profiles = topic_profiles.list_topic_profiles(actor=resolved_actor)["items"]
    raw_by_schedule_id = {
        str(item.get("schedule_id") or "").strip(): item
        for item in raw_schedules
        if str(item.get("schedule_id") or "").strip()
    }
    normalized: list[dict[str, Any]] = []
    known_schedule_ids: set[str] = set()

    for profile in profiles:
        profile_id = str(profile.get("profile_id") or "").strip()
        for template in topic_schedules.DEFAULT_TASK_TEMPLATES:
            schedule_id = f"schedule-{profile_id}-{template['task_type']}"
            raw = raw_by_schedule_id.get(schedule_id, {})
            try:
                normalized_schedule = topic_schedules._normalize_schedule(raw, profile, template)
            except Exception as exc:
                state = "unavailable"
                warnings.append(f"Topic schedule {schedule_id} could not be normalized in read-only mode: {exc}")
                continue
            known_schedule_ids.add(schedule_id)
            normalized.append(normalized_schedule)

        for raw in raw_schedules:
            if str(raw.get("profile_id") or "").strip() != profile_id:
                continue
            if bool(raw.get("deleted", False)):
                continue
            schedule_id = str(raw.get("schedule_id") or "").strip()
            if not schedule_id or schedule_id in known_schedule_ids:
                continue
            try:
                normalized.append(topic_schedules._normalize_schedule(raw, profile, None))
            except Exception as exc:
                state = "unavailable"
                warnings.append(f"Topic schedule {schedule_id} could not be normalized in read-only mode: {exc}")
                continue
            known_schedule_ids.add(schedule_id)

    known_profile_ids = {str(profile.get("profile_id") or "").strip() for profile in profiles}
    for item in raw_schedules:
        schedule_id = str(item.get("schedule_id") or "").strip()
        if not schedule_id:
            continue
        if item.get("deleted") or str(item.get("profile_id") or "").strip() not in known_profile_ids:
            normalized.append(item)

    return {
        "items": [] if state == "unavailable" else topic_schedules._visible_schedules_for_actor(
            normalized,
            actor=resolved_actor,
        ),
        "state": state,
        "backend": backend_descriptor,
        "degraded": bool(warnings),
        "read_only": True,
        "warnings": warnings,
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _contract_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": _optional_text(report.get("id")) or "",
            "runId": _optional_text(report.get("runId")) or _optional_text(report.get("id")) or "",
            "title": _optional_text(report.get("title")) or "",
            "createdAt": _optional_text(report.get("createdAt")) or "",
            "updatedAt": _optional_text(report.get("updatedAt")),
            "status": _optional_text(report.get("status")) or "queued",
            "downloadUrl": _optional_text(report.get("downloadUrl")),
            "error": _optional_text(report.get("error")),
        }
        for report in reports
    ]


def _contract_legacy_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": _optional_text(report.get("runId")) or _optional_text(report.get("id")) or "",
            "title": _optional_text(report.get("title")) or "",
            "path": _optional_text(report.get("downloadUrl")),
            "created_at": _optional_text(report.get("updatedAt")) or _optional_text(report.get("createdAt")) or "",
            "status": _optional_text(report.get("status")) or "queued",
        }
        for report in reports
    ]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator > 0 else 0.0


def _review_status_rank(review: dict[str, Any]) -> int:
    status = str(review.get("full_text_status") or "unavailable")
    return {"unavailable": 0, "abstract_only": 1, "full_text_available": 2, "reviewed": 3}.get(status, 0)


def _review_rank_key(review: dict[str, Any]) -> tuple[int, float, float, str]:
    return (
        _review_status_rank(review),
        float(review.get("evidence_strength") or 0),
        float(review.get("relevance_score") or 0),
        str(review.get("reviewed_at") or ""),
    )


def _unique_reviews_for_topic(topic_id: str, *, read_only: bool = True) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for review in paper_reviews.list_reviews(topic_id=topic_id, limit=100_000, read_only=read_only):
        paper_id = str(review.get("paper_id") or "").strip()
        if not paper_id:
            continue
        existing = selected.get(paper_id)
        if existing is None or _review_rank_key(review) > _review_rank_key(existing):
            selected[paper_id] = review
    return list(selected.values())


def _scoped_unique_reviews_for_topic_items(
    topic_id: str,
    profile: dict[str, Any],
    rows: list[tuple[dict[str, Any], dict[str, Any]]] | None,
    *,
    read_only: bool = True,
) -> list[dict[str, Any]]:
    if not rows:
        return _unique_reviews_for_topic(topic_id, read_only=read_only)
    consolidated = paper_review_service._consolidate_rows(profile, rows)
    candidates = consolidated.get("candidates", [])
    relevant = [
        candidate
        for candidate in candidates
        if float(candidate.get("topic_relevance_score") or 0) >= paper_review_service.LOW_RELEVANCE_THRESHOLD
    ]
    denominator_candidates = relevant or candidates
    scoped_ids = {
        str(candidate.get("normalized_paper_id") or "").strip()
        for candidate in denominator_candidates
        if str(candidate.get("normalized_paper_id") or "").strip()
    }
    if not scoped_ids:
        return []
    return [
        review
        for review in _unique_reviews_for_topic(topic_id, read_only=read_only)
        if paper_review_service.normalize_paper_id(review.get("paper_id")) in scoped_ids
    ]


def _draft_key(value: dict[str, Any]) -> str:
    return f"{value.get('paper_id') or ''}::{value.get('source_text_sha256') or ''}"


def _draft_provider(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    validation = value.get("validation") if isinstance(value.get("validation"), dict) else {}
    provider = validation.get("provider") if isinstance(validation.get("provider"), dict) else {}
    return provider


def _provider_origin_label(provider: dict[str, Any]) -> str | None:
    origin = str(provider.get("origin") or "").strip()
    if origin:
        return origin
    name = str(provider.get("name") or "").strip()
    mode = str(provider.get("mode") or "").strip()
    if name == "stub" or mode == "stub":
        return "stub"
    if mode == "live_command":
        return "live_command"
    if mode == "dry_run":
        return "command_dry_run"
    if name == "manual_import" or not provider:
        return "manual_import" if name == "manual_import" else None
    return "unknown"


def _latest_json_artifact(pattern: str) -> dict[str, Any] | None:
    root = RESEARCH_OPERATIONS_OUTPUT_ROOT
    if not root.exists():
        return None
    try:
        candidates = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("artifact_path", str(path))
            return payload
    return None


def _matches_topic_artifact(payload: dict[str, Any], topic_id: str) -> bool:
    if str(payload.get("topic_id") or "") == topic_id:
        return True
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return str(summary.get("topic_id") or "") == topic_id


def _latest_topic_json_artifact(pattern: str, topic_id: str) -> dict[str, Any] | None:
    root = RESEARCH_OPERATIONS_OUTPUT_ROOT
    if not root.exists():
        return None
    try:
        candidates = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and _matches_topic_artifact(payload, topic_id):
            payload.setdefault("artifact_path", str(path))
            return payload
    return None


def _latest_report_path(topic_id: str) -> str | None:
    reports_dir = RESEARCH_OPERATIONS_OUTPUT_ROOT / "reports"
    if not reports_dir.exists():
        return None
    patterns = [f"{topic_id}-weekly-journal-*.md", "*weekly-journal-m*.md"]
    seen: set[Path] = set()
    candidates: list[Path] = []
    for pattern in patterns:
        for path in reports_dir.glob(pattern):
            if path not in seen:
                seen.add(path)
                candidates.append(path)
    if not candidates:
        return None
    try:
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None
    return str(latest)


def _dedupe_preserve_order(values: list[Any]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            items.append(text)
            seen.add(key)
    return items


def _topic_configuration_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return topic_configurations.build_topic_configuration_summary(profile)


def _run_plan(
    *,
    plan_id: str,
    topic_id: str,
    description: str,
    command: str,
    risk_level: str = "dry_run",
    safety_flags: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "planId": plan_id,
        "topicId": topic_id,
        "description": description,
        "riskLevel": risk_level,
        "copyableCliCommand": command,
        "requiredSafetyFlags": safety_flags or [],
        "writesFiles": False,
        "callsNetwork": False,
        "callsProvider": False,
        "requiresExplicitOperatorAction": True,
        "previewOnly": True,
        "serverExecutesCommand": False,
        "warnings": warnings or ["Preview only: the dashboard does not execute this command."],
    }


def build_topic_operations_planner(profile: dict[str, Any]) -> dict[str, Any]:
    config = _topic_configuration_summary(profile)
    provider_readiness_summary = provider_configurations.build_provider_readiness_summary()
    provider_readiness = provider_readiness_summary.get("items", [])
    provider_by_id = {str(item.get("providerId") or ""): item for item in provider_readiness}
    command_readiness = provider_by_id.get("command", {})
    command_warnings = [
        "Preview only: the dashboard does not execute this command.",
        *[str(warning) for warning in command_readiness.get("warnings", []) if warning],
    ]
    topic_id = config["topicId"] or "large-language-models"
    unique_limit = int(config["defaultUniqueLimit"] or 50)
    download_limit = int(config["defaultDownloadLimit"] or 5)
    max_pdf_bytes = int(config["defaultMaxPdfBytes"] or 25_000_000)
    recommendation_limit = int(config["recommendationLimit"] or 5)
    followup_limit = int(config["followupLimit"] or 10)
    allow_domain = config["defaultAllowDomains"][0] if config["defaultAllowDomains"] else "arxiv.org"
    base = f"python -m app.cli.review_refresh --topic {topic_id}"
    full_text_base = f"{base} --dry-run --use-full-text"
    recommendation_base = f"{full_text_base} --generate-recommendations --recommendation-limit {recommendation_limit}"
    acquisition_safety = [
        "--dry-run",
        "--fetch-missing-pdf",
        f"--download-limit {download_limit}",
        f"--allow-domain {allow_domain}",
        "--extract-after-fetch",
    ]
    plans = [
        _run_plan(
            plan_id="refresh_reviews_dry_run",
            topic_id=topic_id,
            description="Preview deterministic review refresh candidates without writing review records.",
            command=f"{base} --dry-run --unique-limit {unique_limit}",
            safety_flags=["--dry-run"],
        ),
        _run_plan(
            plan_id="full_text_acquisition_dry_run",
            topic_id=topic_id,
            description="Preview bounded full-text PDF acquisition candidates with explicit safety gates.",
            command=(
                f"{full_text_base} --unique-limit {unique_limit} --fetch-missing-pdf "
                f"--download-limit {download_limit} --allow-domain {allow_domain} "
                f"--max-pdf-bytes {max_pdf_bytes} --extract-after-fetch"
            ),
            safety_flags=[*acquisition_safety, f"--max-pdf-bytes {max_pdf_bytes}"],
        ),
        _run_plan(
            plan_id="recommendation_generation",
            topic_id=topic_id,
            description="Preview deterministic evidence-linked recommendations without writing exports.",
            command=recommendation_base,
            safety_flags=["--dry-run"],
        ),
        _run_plan(
            plan_id="recommendation_audit",
            topic_id=topic_id,
            description="Preview recommendation quality audit and coverage warnings.",
            command=f"{recommendation_base} --audit-recommendations",
            safety_flags=["--dry-run"],
        ),
        _run_plan(
            plan_id="recommendation_followup_plan",
            topic_id=topic_id,
            description="Preview follow-up acquisition candidates without downloading or writing.",
            command=f"{recommendation_base} --audit-recommendations --plan-recommendation-followup --recommendation-followup-limit {followup_limit}",
            safety_flags=["--dry-run"],
        ),
        _run_plan(
            plan_id="recommendation_followup_execution_dry_run",
            topic_id=topic_id,
            description="Preview recommendation-driven follow-up acquisition with all PDF gates present.",
            command=(
                f"{recommendation_base} --audit-recommendations --plan-recommendation-followup "
                f"--recommendation-followup-limit {followup_limit} --execute-recommendation-followup "
                f"--fetch-missing-pdf --download-limit {download_limit} --allow-domain {allow_domain} "
                f"--max-pdf-bytes {max_pdf_bytes} --extract-after-fetch --compare-recommendation-quality-before-after"
            ),
            safety_flags=[*acquisition_safety, f"--max-pdf-bytes {max_pdf_bytes}"],
        ),
        _run_plan(
            plan_id="provider_stub_dry_run",
            topic_id=topic_id,
            description="Preview deterministic stub provider invocations; no live provider is called.",
            command=f"{full_text_base} --run-ai-review-provider --ai-provider stub --ai-provider-limit 5",
            safety_flags=["--dry-run", "--ai-provider stub"],
        ),
        _run_plan(
            plan_id="provider_command_dry_run",
            topic_id=topic_id,
            description="Preview local command-provider invocation shape without launching a provider binary.",
            command=(
                f"{full_text_base} --run-ai-review-provider --ai-provider command "
                "--ai-provider-mode dry-run --ai-provider-limit 1"
            ),
            safety_flags=["--dry-run", "--ai-provider-mode dry-run"],
            warnings=list(
                dict.fromkeys(
                    [
                        *command_warnings,
                        "Dashboard guidance remains preview-only; live command execution is CLI-only and real provider profiles require --allow-live-ai-provider plus --accept-provider-risk.",
                    ]
                )
            ),
        ),
        _run_plan(
            plan_id="provider_acceptance_smoke_dry_run",
            topic_id=topic_id,
            description="Preview optional provider acceptance smoke; real providers still require explicit CLI acceptance outside the dashboard.",
            command=(
                f"{full_text_base} --provider-acceptance-smoke --ai-provider-config fixture-command "
                "--ai-provider-limit 2 --ai-provider-run-dir output/ai-provider-runs/provider-acceptance-smoke"
            ),
            safety_flags=["--dry-run", "--provider-acceptance-smoke", "--ai-provider-config fixture-command"],
            warnings=[
                "Preview only: the dashboard never executes provider commands.",
                "Real provider smoke is CLI-only and requires explicit --allow-live-ai-provider plus --accept-provider-risk when operators run it manually.",
                "Provider output must pass M4 grounded validation before storage or report use.",
            ],
        ),
        _run_plan(
            plan_id="provider_manual_run_prepare_dry_run",
            topic_id=topic_id,
            description="Preview a manual command-provider run package with bounded packets and command previews.",
            command=(
                f"{full_text_base} --prepare-provider-manual-run --ai-provider command "
                f"--ai-provider-limit 3 --provider-manual-run-dir output/provider-manual-run-{topic_id}"
            ),
            safety_flags=["--dry-run", "--prepare-provider-manual-run"],
            warnings=list(
                dict.fromkeys(
                    [
                        "Preview only: the dashboard does not execute this command.",
                        "Manual provider packages write only when the operator runs the CLI without --dry-run.",
                    ]
                )
            ),
        ),
        _run_plan(
            plan_id="provider_manual_output_import",
            topic_id=topic_id,
            description="Import manually produced provider output only through grounded AI review draft validation.",
            command=(
                f"{base} --import-provider-drafts output/provider-manual-run-{topic_id}/provider-output.jsonl "
                "--validate-ai-review-drafts"
            ),
            risk_level="write_requires_cli",
            safety_flags=["--validate-ai-review-drafts"],
            warnings=[
                "Manual CLI only: imported outputs must pass grounded validation before storage.",
                "Rejected or needs-review drafts do not improve report claims.",
            ],
        ),
    ]
    return {
        "topicConfiguration": config,
        "runPlans": plans,
        "providerReadiness": provider_readiness,
        "warnings": config["validationWarnings"],
    }


def _research_operations_guidance(topic_id: str) -> dict[str, str]:
    topic_arg = topic_id or "large-language-models"
    base = f"python -m app.cli.review_refresh --topic {topic_arg} --dry-run --use-full-text"
    recommendation = f"{base} --generate-recommendations --recommendation-limit 5"
    audit = f"{recommendation} --audit-recommendations"
    followup_plan = f"{recommendation} --plan-recommendation-followup --recommendation-followup-limit 10"
    followup_execution = (
        f"{audit} --plan-recommendation-followup --recommendation-followup-limit 10 "
        "--execute-recommendation-followup --fetch-missing-pdf --download-limit 5 "
        "--allow-domain arxiv.org --extract-after-fetch"
    )
    return {
        "recommendationDryRunCommand": recommendation,
        "recommendationAuditCommand": audit,
        "followupPlanCommand": followup_plan,
        "followupExecutionDryRunCommand": followup_execution,
    }


def _provider_operations(
    topic_id: str,
    accepted_by_review_key: dict[str, dict[str, Any]],
    *,
    read_only: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    drafts = ai_review_drafts.list_drafts(topic_id=topic_id, limit=100_000, read_only=read_only)
    provider_drafts = [draft for draft in drafts if _draft_provider(draft)]
    latest_provider = _draft_provider(provider_drafts[0]) if provider_drafts else {}
    generated_drafts = [draft for draft in provider_drafts if bool(_draft_provider(draft).get("generated"))]
    accepted_provider_keys = {
        key
        for key, draft in accepted_by_review_key.items()
        if bool(_draft_provider(draft).get("generated"))
    }
    provider_payload = {
        "generatedCount": len(generated_drafts),
        "acceptedCount": len(accepted_provider_keys),
        "needsReviewCount": sum(1 for draft in generated_drafts if draft.get("status") == "needs_review"),
        "rejectedCount": sum(1 for draft in generated_drafts if draft.get("status") == "rejected"),
        "failedCount": 0,
        "latestProviderOrigin": _provider_origin_label(latest_provider),
    }
    latest_summary = None
    if latest_provider:
        latest_summary = {
            "provider": latest_provider.get("name"),
            "provider_mode": latest_provider.get("mode"),
            "provider_origin": _provider_origin_label(latest_provider),
            "provider_run_id": latest_provider.get("provider_run_id") or latest_provider.get("run_id"),
            "generated_count": provider_payload["generatedCount"],
            "accepted_count": provider_payload["acceptedCount"],
            "needs_review_count": provider_payload["needsReviewCount"],
            "rejected_count": provider_payload["rejectedCount"],
        }
    return provider_payload, latest_summary


def _recommendation_payload_has_current_coverage(
    payload: dict[str, Any],
    *,
    full_text_reviewed: int,
    topic_relevant: int,
) -> bool:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if not summary:
        return False
    return _safe_int(summary.get("full_text_reviewed_count")) == full_text_reviewed and _safe_int(
        summary.get("topic_relevant_unique_count")
    ) == topic_relevant


def _build_research_operations(
    topic_id: str,
    review_coverage: dict[str, Any],
    profile: dict[str, Any] | None = None,
    *,
    operator_user: Any | None = None,
    review_scope_rows: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    read_only: bool = True,
) -> dict[str, Any]:
    warnings: list[str] = []
    full_text_reviewed = _safe_int(review_coverage.get("fullTextReviewedUniqueCount") or review_coverage.get("fullTextReviewedCount"))
    topic_relevant = _safe_int(
        review_coverage.get("topicRelevantUniqueCount")
        or review_coverage.get("uniqueCandidateCount")
        or review_coverage.get("totalPapers")
    )
    unique_reviews = _scoped_unique_reviews_for_topic_items(
        topic_id,
        profile or {},
        review_scope_rows,
        read_only=read_only,
    )
    all_ai_drafts = ai_review_drafts.list_drafts(topic_id=topic_id, limit=100_000, read_only=read_only)
    reviewed_keys = {
        _draft_key(review)
        for review in unique_reviews
        if str(review.get("full_text_status") or "") == "reviewed" and str(review.get("source_text_sha256") or "").strip()
    }
    accepted_by_review_key: dict[str, dict[str, Any]] = {}
    for draft in all_ai_drafts:
        if draft.get("status") != "accepted":
            continue
        key = _draft_key(draft)
        if key in reviewed_keys and key not in accepted_by_review_key:
            accepted_by_review_key[key] = draft
    ai_grounded_count = len(accepted_by_review_key)
    provider_generated_accepted = sum(1 for draft in accepted_by_review_key.values() if bool(_draft_provider(draft).get("generated")))
    provider_review_summary = provider_review_coverage.build_provider_review_coverage_summary(
        topic_id,
        full_text_reviewed_denominator=full_text_reviewed,
        reviews=unique_reviews,
        all_drafts=all_ai_drafts,
        accepted_by_review_key=accepted_by_review_key,
    )

    recommendation_payload: dict[str, Any] = (
        _latest_topic_json_artifact("research-recommendations*.json", topic_id)
        or {"summary": {}, "coverage_warning": None}
    )
    audit_payload: dict[str, Any] | None = None
    followup_payload: dict[str, Any] | None = None
    if isinstance(recommendation_payload.get("recommendation_audit"), dict):
        audit_payload = recommendation_payload["recommendation_audit"]
    else:
        audit_payload = _latest_topic_json_artifact("recommendation-audit*.json", topic_id)
    if isinstance(recommendation_payload.get("recommendation_followup"), dict):
        followup_payload = recommendation_payload["recommendation_followup"]
    else:
        followup_payload = _latest_topic_json_artifact("recommendation-followup-m*.json", topic_id)
    if not _recommendation_payload_has_current_coverage(
        recommendation_payload,
        full_text_reviewed=full_text_reviewed,
        topic_relevant=topic_relevant,
    ):
        try:
            recommendation_payload = recommendations.build_recommendations_for_topic(
                topic_id,
                limit=5,
                topic_relevant_unique_count=topic_relevant or None,
            )
        except Exception:
            warnings.append("추천 요약을 계산할 수 없어 연구 운영 상태 일부를 비워 표시합니다.")
    if audit_payload is None and recommendation_payload.get("summary"):
        try:
            audit_payload = recommendations.audit_recommendation_quality(recommendation_payload)
        except Exception:
            warnings.append("추천 품질 감사 요약을 계산할 수 없어 해당 상태를 비워 표시합니다.")
    if followup_payload is None and recommendation_payload.get("summary"):
        try:
            followup_payload = recommendations.plan_recommendation_followup(topic_id, recommendation_payload, limit=10)
        except Exception:
            warnings.append("후속 획득 계획 요약을 계산할 수 없어 해당 상태를 비워 표시합니다.")

    rec_summary = recommendation_payload.get("summary") if isinstance(recommendation_payload.get("summary"), dict) else {}
    audit_summary = audit_payload.get("summary") if isinstance(audit_payload, dict) and isinstance(audit_payload.get("summary"), dict) else None
    followup_summary = (
        followup_payload.get("summary") if isinstance(followup_payload, dict) and isinstance(followup_payload.get("summary"), dict) else None
    )
    latest_execution = _latest_json_artifact("recommendation-followup-execution*.json")
    execution_summary = (
        latest_execution.get("summary") if isinstance(latest_execution, dict) and isinstance(latest_execution.get("summary"), dict) else None
    )
    provider_payload, latest_provider_summary = _provider_operations(
        topic_id,
        accepted_by_review_key,
        read_only=read_only,
    )
    coverage_warning = str(recommendation_payload.get("coverage_warning") or "").strip()
    if coverage_warning:
        warnings.append(coverage_warning)
    try:
        provider_review_summary["provenanceAudit"] = provider_review_coverage.build_provider_provenance_audit(
            topic_id,
            dashboard_summary=provider_review_summary,
            recommendation_payload=recommendation_payload,
            reviews=unique_reviews,
            all_drafts=all_ai_drafts,
            coverage_summary=provider_review_summary,
        )
    except Exception:
        warnings.append("Provider provenance audit could not be computed, so coverage consistency status is unavailable.")

    planned_count = _safe_int((execution_summary or {}).get("planned_followup_count") or (followup_summary or {}).get("followup_candidate_count"))
    auth_status = auth_foundation.build_auth_foundation_status(user=operator_user)
    operations_payload = {
        "coverage": {
            "fullTextReviewedUniqueCount": full_text_reviewed,
            "topicRelevantUniqueCount": topic_relevant,
            "fullTextCoverageRatio": _ratio(full_text_reviewed, topic_relevant),
            "aiGroundedAcceptedCount": ai_grounded_count,
            "aiGroundedCoverageRatio": _ratio(ai_grounded_count, full_text_reviewed),
            "providerGeneratedAcceptedCount": provider_generated_accepted,
            "providerReviewCoverage": provider_review_summary,
        },
        "recommendations": {
            "recommendationGapCandidateCount": _safe_int(rec_summary.get("candidate_gap_count")),
            "recommendationTopicCandidateCount": _safe_int(rec_summary.get("candidate_topic_count")),
            "recommendationWeakCount": _safe_int((audit_summary or {}).get("weak_recommendation_count")),
            "recommendationNeedsFollowupCount": _safe_int((audit_summary or {}).get("needs_follow_up_count")),
            "recommendationCoverageWarningCount": _safe_int(
                (audit_summary or {}).get("coverage_warning_count") or rec_summary.get("coverage_warning_count")
            ),
            "preliminary": bool(coverage_warning or _safe_int((audit_summary or {}).get("coverage_warning_count"))),
        },
        "followup": {
            "followupPlannedCount": planned_count,
            "followupExecutedCount": _safe_int((execution_summary or {}).get("downloaded_count")),
            "followupReviewedCount": _safe_int((execution_summary or {}).get("reviewed_from_followup_count")),
            "skippedExistingReviewedCount": _safe_int((execution_summary or {}).get("skipped_existing_reviewed_count")),
            "skippedNoPdfUrlCount": _safe_int((execution_summary or {}).get("skipped_no_pdf_url_count")),
            "skippedBlockedCount": _safe_int((execution_summary or {}).get("skipped_blocked_count")),
        },
        "provider": provider_payload,
        "auth": auth_status,
        "latestRecommendationAuditSummary": audit_summary,
        "latestRecommendationFollowupPlanSummary": followup_summary,
        "latestFollowupExecutionSummary": execution_summary,
        "latestProviderRunSummary": latest_provider_summary,
        "latestReportPath": _latest_report_path(topic_id),
        "guidance": _research_operations_guidance(topic_id),
        "operationsPlanner": build_topic_operations_planner(profile or {"profile_id": topic_id, "title": topic_id, "queries": []}),
        "warnings": list(dict.fromkeys(warnings)),
    }
    try:
        rc_audit = research_operations_rc_audit.build_research_operations_rc_audit(
            topic_id,
            dashboard_operations=operations_payload,
            recommendation_payload=recommendation_payload,
            auth_status=auth_status,
            reviews=unique_reviews,
            all_drafts=all_ai_drafts,
            coverage_summary=provider_review_summary,
        )
        operations_payload["rcAudit"] = rc_audit
        operations_payload["releaseFreeze"] = research_operations_rc_audit.build_research_operations_release_freeze_summary(
            rc_audit,
            coverage_summary={
                "fullTextReviewedUniqueCount": full_text_reviewed,
                "aiGroundedAcceptedCount": ai_grounded_count,
                "providerOrchestratedAcceptedCount": provider_review_summary.get("providerOrchestratedAcceptedCount", 0),
            },
            provider_provenance_summary=provider_review_summary.get("provenanceAudit", {}),
            recommendation_summary={
                "preliminary": bool(coverage_warning or _safe_int((audit_summary or {}).get("coverage_warning_count"))),
                "weakCount": _safe_int((audit_summary or {}).get("weak_recommendation_count")),
                "needsFollowupCount": _safe_int((audit_summary or {}).get("needs_follow_up_count")),
            },
            oauth_provider_safety_summary={
                "tokenStorageEnabled": bool(auth_status.get("tokenStorageEnabled")),
                "liveOAuthEnabled": bool(auth_status.get("oauthReady")) and bool(auth_status.get("tokenStorageEnabled")),
                "dashboardExecutionEnabled": False,
                "providerExecutionEnabled": str(auth_status.get("providerLiveExecution") or "") == "dashboard_enabled",
            },
        )
    except Exception:
        operations_payload["warnings"] = list(
            dict.fromkeys([*operations_payload.get("warnings", []), "Release-candidate audit could not be computed."])
        )
    return operations_payload


def build_api_topics(*, actor: ResourceActor | None = None) -> dict[str, Any]:
    return {
        "items": [
            _api_topic(profile)
            for profile in topic_profiles.list_topic_profiles(actor=actor)["items"]
            if str(profile.get("status") or "active") != "archived"
        ]
    }


def _list_topic_profiles_read_only() -> list[dict[str, Any]]:
    payload = read_json(topic_profiles.PROFILE_LIBRARY_PATH, default=[])
    profiles = [dict(profile) for profile in payload if isinstance(profile, dict)] if isinstance(payload, list) else []
    profiles.sort(key=lambda profile: str(profile.get("updated_at") or ""), reverse=True)
    profiles.sort(
        key=lambda profile: (
            str(profile.get("status") or "active") == "archived",
            not bool(profile.get("is_favorite") or profile.get("isFavorite")),
        )
    )
    return profiles


def _overview_numeric_series(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    return [number for item in value if (number := _coerce_float(item)) is not None]


def _build_api_topic_overview_item(profile: dict[str, Any]) -> dict[str, Any]:
    topic = _api_topic(profile)
    profile_id = topic["id"]
    degraded = False
    try:
        analytics_snapshot = topic_analytics.load_topic_analytics(profile_id)
        if analytics_snapshot is None:
            analytics_snapshot = topic_analytics.build_topic_analytics_snapshot(
                profile_id,
                read_only=True,
                profile=profile,
            )
        metrics = analytics_snapshot.get("metrics") if isinstance(analytics_snapshot.get("metrics"), dict) else {}
        coverage = (
            analytics_snapshot.get("reviewCoverage")
            if isinstance(analytics_snapshot.get("reviewCoverage"), dict)
            else {}
        )
    except Exception:
        metrics = {}
        coverage = {}
        degraded = True

    collected = metrics.get("collectedPapers") if isinstance(metrics.get("collectedPapers"), dict) else {}
    core = metrics.get("corePapers") if isinstance(metrics.get("corePapers"), dict) else {}
    trend = metrics.get("trendIndex") if isinstance(metrics.get("trendIndex"), dict) else {}
    return {
        "id": profile_id,
        "name": topic["name"],
        "description": topic.get("description"),
        "updatedAt": topic["updatedAt"],
        "isFavorite": topic["isFavorite"],
        "collectedPapers": _safe_int(collected.get("value")),
        "collectedDelta": _coerce_float(collected.get("delta")) or 0,
        "collectedSparkline": _overview_numeric_series(collected.get("sparkline")),
        "corePapers": _safe_int(core.get("value")),
        "coveragePercentage": _coerce_float(coverage.get("coveragePercentage")) or 0,
        "fullTextReviewedCount": _safe_int(coverage.get("fullTextReviewedCount")),
        "totalPapers": _safe_int(coverage.get("totalPapers")),
        "trendLabel": str(trend.get("label") or ""),
        "trendDeltaPercent": _coerce_float(trend.get("deltaPercent")) or 0,
        "degraded": degraded,
    }


def build_api_topics_overview(*, actor: ResourceActor | None = None) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    items: list[dict[str, Any]] = []
    seen_topic_ids: set[str] = set()
    for profile in _list_topic_profiles_read_only():
        if str(profile.get("status") or "active") == "archived":
            continue
        if not resource_visible_to_actor(
            actor=resolved_actor,
            resource_kind=topic_profiles._profile_resource_kind(profile),
            owner_user_id=profile.get("owner_user_id"),
        ):
            continue
        profile_id = str(profile.get("profile_id") or "").strip()
        if not profile_id or profile_id in seen_topic_ids:
            continue
        seen_topic_ids.add(profile_id)
        items.append(_build_api_topic_overview_item(profile))

    return {
        "summary": {
            "topicCount": len(items),
            "collectedPapers": sum(_safe_int(item.get("collectedPapers")) for item in items),
            "fullTextReviewedCount": sum(_safe_int(item.get("fullTextReviewedCount")) for item in items),
        },
        "items": items,
    }


def build_topic_recommendation_views(
    topic_id: str,
    *,
    limit: int = 8,
    actor: ResourceActor | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read-only actionable recommendation candidates for a topic.

    Projects the deterministic ``recommendations`` service output into a compact,
    GUI-friendly list. ``new_topic`` candidates are actionable (they can seed a new
    topic profile); ``gap`` candidates are informational research-gap signals.
    """
    payload = recommendations.build_recommendations_for_topic(
        str(topic_id or "").strip(),
        limit=limit,
        read_only=True,
    )
    items: list[dict[str, Any]] = []

    for candidate in payload.get("new_topic_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        queries = [str(query).strip() for query in (candidate.get("suggested_queries") or []) if str(query).strip()]
        if not queries:
            question = str(candidate.get("proposed_research_question") or "").strip()
            queries = [question] if question else []
        items.append(
            {
                "id": str(candidate.get("topic_candidate_id") or ""),
                "type": "new_topic",
                "title": str(candidate.get("title") or ""),
                "question": str(candidate.get("proposed_research_question") or ""),
                "whyNow": str(candidate.get("why_now") or ""),
                "methodDirection": str(candidate.get("suggested_method_direction") or ""),
                "expectedContribution": str(candidate.get("expected_contribution") or ""),
                "confidence": str(candidate.get("confidence") or ""),
                "supportingPaperCount": int(
                    candidate.get("supporting_paper_count") or len(candidate.get("supporting_paper_ids") or [])
                ),
                "suggestedQueries": queries,
                "actionable": True,
            }
        )

    for gap in payload.get("gap_candidates") or []:
        if not isinstance(gap, dict):
            continue
        items.append(
            {
                "id": str(gap.get("gap_id") or ""),
                "type": "gap",
                "title": str(gap.get("title") or ""),
                "gapStatement": str(gap.get("gap_statement") or ""),
                "noveltyRationale": str(gap.get("novelty_rationale") or ""),
                "confidence": str(gap.get("confidence") or ""),
                "supportingPaperCount": int(
                    gap.get("supporting_paper_count") or len(gap.get("supporting_paper_ids") or [])
                ),
                "suggestedQueries": [],
                "actionable": False,
            }
        )

    from app.domain import recommendation_versions

    source_candidates = {
        ("new_topic", str(c.get("topic_candidate_id") or "")): c
        for c in payload.get("new_topic_candidates") or [] if isinstance(c, dict)
    }
    source_candidates.update({
        ("gap", str(c.get("gap_id") or "")): c
        for c in payload.get("gap_candidates") or [] if isinstance(c, dict)
    })
    versions = []
    for item in items:
        source = source_candidates[(item["type"], item["id"])]
        item.update(
            supportingPaperIds=source.get("supporting_paper_ids") or [],
            evidenceRefs=source.get("supporting_evidence_refs") or [],
            coverageWarning=source.get("coverage_warning") or None,
            riskNotes=source.get("risk_notes") or [],
            requiredFollowUpEvidence=source.get("required_follow_up_evidence") or [],
            problemStatement=source.get("problem_statement") or "",
            differentiationClaim=source.get("differentiation_claim") or "",
            nearestExistingWork=source.get("nearest_existing_work") or [],
            limitationRefs=source.get("limitation_refs") or [],
            sourceBasis=source.get("source_basis") or "",
        )
        version = recommendation_versions.content(str(payload.get("topic_id") or topic_id), payload, source, dict(item))
        item["versionId"] = recommendation_versions.identity(version)
        versions.append(version)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "topicId": str(payload.get("topic_id") or topic_id),
        "items": items,
        "newTopicCount": sum(1 for item in items if item["type"] == "new_topic"),
        "gapCount": sum(1 for item in items if item["type"] == "gap"),
        "coverageWarning": str(payload.get("coverage_warning") or summary.get("coverage_warning") or "") or None,
    }, versions


def build_api_topic_recommendations(topic_id: str, *, limit: int = 8, actor: ResourceActor | None = None) -> dict[str, Any]:
    return build_topic_recommendation_views(topic_id, limit=limit, actor=actor)[0]


def build_api_topic_dashboard(
    topic_id: str,
    *,
    operator_user: Any | None = None,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    selected = _get_profile_or_raise(topic_id, actor=resolved_actor)
    topic = str(selected.get("topic", ""))
    profile_id = str(selected.get("profile_id") or topic_id)
    analytics_snapshot = topic_analytics.load_topic_analytics(profile_id)
    analytics_read_warnings: list[str] = []
    if analytics_snapshot is None:
        analytics_snapshot = topic_analytics.build_topic_analytics_snapshot(profile_id, read_only=True)
        analytics_read_warnings.append(
            "Persisted topic analytics artifact is missing; dashboard used a read-only runtime snapshot without refreshing the artifact."
        )
    topic_items = _topic_items(topic, profile_id=profile_id, max_runs=20, actor=resolved_actor)
    schedule_warnings: list[str] = []
    schedule_response = _list_topic_schedules_read_only(actor=resolved_actor)
    if schedule_response.get("degraded"):
        schedule_warnings.extend(str(item) for item in schedule_response.get("warnings", []) if item)
    schedules = [
        _contract_task(schedule)
        for schedule in schedule_response.get("items", [])
        if schedule.get("profile_id") == selected.get("profile_id")
    ]
    reports_payload = _recent_reports(
        str(selected.get("profile_id") or ""),
        topic,
        actor=resolved_actor,
    )
    warnings = list(
        dict.fromkeys(
            [
                *(analytics_snapshot.get("warnings", []) or []),
                *analytics_read_warnings,
                *(reports_payload.get("warnings", []) or []),
                *schedule_warnings,
            ]
        )
    )
    source_summary = source_config.source_summary(
        selected.get("source_settings", {}),
        recent_paper_counts=_recent_source_counts(topic_items),
        last_success_by_provider=_last_success_by_provider(
            topic,
            profile_id=profile_id,
            actor=resolved_actor,
        ),
    )
    review_coverage = analytics_snapshot.get(
        "reviewCoverage",
        {
            "totalPapers": 0,
            "reviewedCount": 0,
            "abstractOnlyCount": 0,
            "fullTextReviewedCount": 0,
            "coveragePercentage": 0.0,
            "rawIndexedPaperCount": 0,
            "uniqueCandidateCount": 0,
            "topicRelevantUniqueCount": 0,
            "reviewedUniqueCount": 0,
            "abstractOnlyUniqueCount": 0,
            "fullTextAvailableUniqueCount": 0,
            "fullTextReviewedUniqueCount": 0,
        },
    )
    return {
        "topic": _api_topic(selected),
        "sourceSummary": source_summary,
        "metrics": analytics_snapshot.get("metrics", {}),
        "trends": analytics_snapshot.get("trends", {}),
        "hotKeywords": analytics_snapshot.get("hotKeywords", []),
        "scheduledTasks": schedules,
        "scheduleStatus": {
            "state": schedule_response["state"],
            "warnings": schedule_response.get("warnings", []),
        },
        "latestCorePapers": analytics_snapshot.get("latestCorePapers", []),
        "fieldDistribution": analytics_snapshot.get("fieldDistribution", []),
        "institutionDistribution": analytics_snapshot.get("institutionDistribution", []),
        "recentReports": _contract_reports(reports_payload.get("items", [])),
        "reviewCoverage": review_coverage,
        "researchOperations": _build_research_operations(
            profile_id,
            review_coverage,
            selected,
            operator_user=operator_user,
            review_scope_rows=topic_items,
        ),
        "analytics": {
            "profileId": analytics_snapshot.get("profileId") or str(selected.get("profile_id") or ""),
            "artifactVersion": analytics_snapshot.get("artifactVersion") or "topic-analytics-v1",
            "computedAt": analytics_snapshot.get("computedAt") or analytics_snapshot.get("generatedAt") or str(selected.get("updated_at") or ""),
            "generatedAt": analytics_snapshot.get("generatedAt") or str(selected.get("updated_at") or ""),
            "lastAnalysisAt": analytics_snapshot.get("lastAnalysisAt"),
            "freshness": analytics_snapshot.get("freshness"),
            "sourceRuns": analytics_snapshot.get("sourceRuns", []),
            "coverage": analytics_snapshot.get("coverage", []),
            "metricDefinitions": analytics_snapshot.get("metricDefinitions", []),
            "derivations": analytics_snapshot.get("derivations", []),
            "trendDetail": analytics_snapshot.get("trendDetail", {"bucketMode": "published_month", "summary": "", "points": []}),
            "keywordDetails": analytics_snapshot.get("keywordDetails", []),
            "fieldDetails": analytics_snapshot.get("fieldDetails", []),
            "institutionDetails": analytics_snapshot.get("institutionDetails", []),
            "corePaperDetails": analytics_snapshot.get("corePaperDetails", []),
        },
        "warnings": warnings,
    }


def build_api_topic_papers(
    topic_id: str,
    *,
    q: str = "",
    provider: str | None = None,
    limit: int = 50,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    selected = _get_profile_or_raise(topic_id, actor=resolved_actor)
    topic = str(selected.get("topic", ""))
    with resource_actor_context(resolved_actor):
        items = _topic_items(topic, profile_id=str(selected["profile_id"]), max_runs=20)
    provider_filter = (provider or "").strip() or None
    warnings: list[str] = []
    if not items:
        warnings.append("이 토픽에는 아직 저장된 논문이 없습니다.")
    records = _topic_paper_records(items, q=q, provider=provider_filter, limit=limit)
    with resource_actor_context(resolved_actor):
        run_rows = _topic_run_records(topic, profile_id=str(selected["profile_id"]))[:20]
    provenance = topic_analytics.build_runtime_provenance(
        "topic_papers",
        computed_at=iso_now_utc(),
        source_runs=run_rows,
        warnings=warnings,
    )
    return {
        "topic": _api_topic(selected),
        "items": records,
        "total": len(records),
        "q": q,
        "providerFilter": provider_filter,
        "providerCounts": _provider_counts(items),
        "provenance": provenance,
        "warnings": warnings,
    }


def build_topic_dashboard(
    profile_id: str | None = None,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    _, selected = _select_profile(profile_id, actor=resolved_actor)
    if selected is None:
        return {
            "selected_profile_id": None,
            "topic": None,
            "totals": {},
            "recent_runs": [],
            "latest_papers": [],
            "hot_keywords": [],
            "trend": [],
            "distributions": {"themes": [], "statuses": []},
            "institution_rankings": [],
            "recent_reports": [],
            "warnings": ["등록된 연구 토픽이 없습니다."],
        }

    topic = str(selected.get("topic", ""))
    selected_profile_id = str(selected["profile_id"])
    visible_run_ids = [
        str(run.get("run_id") or "").strip()
        for run in _topic_run_records(topic, profile_id=selected_profile_id, actor=resolved_actor)
        if str(run.get("run_id") or "").strip()
    ]
    overview = metadata_index.get_overview(
        run_ids=visible_run_ids,
    )
    run_rows = _recent_runs(topic, profile_id=selected_profile_id, actor=resolved_actor)
    keywords, institutions, warnings = _keywords_and_institutions(
        topic,
        profile_id=selected_profile_id,
        actor=resolved_actor,
    )
    return {
        "selected_profile_id": selected.get("profile_id"),
        "topic": selected,
        "totals": overview.get("totals", {}),
        "recent_runs": run_rows,
        "latest_papers": _latest_papers(topic, profile_id=selected_profile_id, actor=resolved_actor),
        "hot_keywords": keywords,
        "trend": _trend(topic, profile_id=selected_profile_id, actor=resolved_actor),
        "distributions": {
            "themes": _distribution(topic, "theme", profile_id=selected_profile_id, actor=resolved_actor),
            "statuses": _distribution(topic, "status", profile_id=selected_profile_id, actor=resolved_actor),
        },
        "institution_rankings": institutions,
        "recent_reports": _contract_legacy_reports(
            _recent_reports(
                str(selected.get("profile_id") or ""),
                topic,
                actor=resolved_actor,
            ).get("items", [])
        ),
        "warnings": warnings,
    }

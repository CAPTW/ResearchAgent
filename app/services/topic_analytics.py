from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import re
import threading
from typing import Any

from app.domain.constants import STORAGE_ROOT
from app.domain.topic_membership import run_matches_topic
from app.repositories import metadata_index, paper_reviews
from app.repositories.files import ensure_dir, iso_now_utc, read_json, safe_filename, write_json
from app.repositories.runs import list_run_records
from app.services import paper_review, topic_profiles, workbench

ANALYTICS_LIBRARY_DIR = STORAGE_ROOT.parent / "topic_analytics"
ANALYTICS_LOCK = threading.RLock()
# v4 binds source runs by profile ID; only unbound legacy runs use exact topic text.
# Existing artifacts retain their recorded version and values when read.
ANALYTICS_ARTIFACT_VERSION = "topic-analytics-v4"
MAX_SOURCE_RUNS = 12
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
METRIC_DEFINITIONS = [
    {
        "metricId": "collectedPapers",
        "label": "수집된 논문",
        "definition": "최근 분석 snapshot에 포함된 run들에서 paper_id 기준으로 중복 제거한 논문 수입니다.",
    },
    {
        "metricId": "corePapers",
        "label": "핵심 논문",
        "definition": f"로컬 선별 점수 {CORE_PAPER_SCORE_THRESHOLD}점 이상 또는 주제 관련성 점수 60점 이상인 논문 수입니다.",
    },
    {
        "metricId": "averageImpact",
        "label": "평균 선별 점수",
        "definition": "선별 점수가 확인된 논문의 triage 점수 평균입니다. 선별 점수 coverage로 계산에 포함된 논문 수를 확인할 수 있습니다.",
    },
    {
        "metricId": "totalCitations",
        "label": "인용 수 (확인된 합계)",
        "definition": "확인된 citation 메타데이터의 합계입니다. 인용 메타데이터 coverage와 함께 확인하며 누락된 논문의 인용 수는 알 수 없습니다.",
    },
    {
        "metricId": "trendIndex",
        "label": "최근 집계 월",
        "definition": "label은 최근 집계 월입니다. 기존 deltaPercent 필드는 마지막 두 비어 있지 않은 월 구간의 논문 수 비교로, 시점 간 연구 성장률이 아닙니다.",
    },
]
DERIVATION_DEFINITIONS = [
    {
        "surfaceId": "metric_cards",
        "label": "대시보드 지표 카드",
        "mode": "persisted_artifact",
        "definition": "토픽 분석 snapshot에 포함된 중복 제거 논문과 월별 버킷에서 계산합니다.",
    },
    {
        "surfaceId": "trend_chart",
        "label": "트렌드 차트",
        "mode": "persisted_artifact",
        "definition": "유효한 발행월, 갱신월, run 생성월 순으로 집계합니다. 최대 12개 비어 있지 않은 월 구간이며 시점 간 변화량이 아닙니다.",
    },
    {
        "surfaceId": "hot_keywords",
        "label": "핫 키워드",
        "mode": "persisted_artifact",
        "definition": "triage/keyword hit 산출물의 primary hit는 2점, secondary hit는 1점으로 합산합니다.",
    },
    {
        "surfaceId": "latest_core_papers",
        "label": "최신 핵심 논문",
        "mode": "persisted_artifact",
        "definition": f"영향력 점수 {CORE_PAPER_SCORE_THRESHOLD}점 이상인 논문만 상위 정렬해 표시합니다.",
    },
    {
        "surfaceId": "field_distribution",
        "label": "연구 분야 분포",
        "mode": "persisted_artifact",
        "definition": "분석 산출물의 theme 값을 집계하며 누락 시 미분류로 남깁니다.",
    },
    {
        "surfaceId": "institution_distribution",
        "label": "기관 분포",
        "mode": "persisted_artifact",
        "definition": "논문/저자 메타데이터의 기관 문자열을 정규화해 논문 단위로 중복 제거한 뒤 집계합니다.",
    },
    {
        "surfaceId": "topic_papers",
        "label": "토픽 논문 목록",
        "mode": "runtime_adapter",
        "definition": "요청 시 최근 run triage 산출물을 중복 제거해 검색/정렬합니다. 별도 persisted artifact는 아직 없습니다.",
    },
    {
        "surfaceId": "reports_hub",
        "label": "보고서 허브 목록",
        "mode": "runtime_adapter",
        "definition": "요청 시 run 상태와 보고서 markdown 산출물 존재 여부에서 목록과 다운로드 가능 여부를 계산합니다.",
    },
]


def _analytics_path(profile_id: str) -> Path:
    return ANALYTICS_LIBRARY_DIR / f"{safe_filename(profile_id)}.json"


def _api_topic(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(profile.get("profile_id", "")),
        "name": str(profile.get("title") or profile.get("topic") or profile.get("profile_id") or ""),
        "description": profile.get("description"),
        "updatedAt": str(profile.get("updated_at") or profile.get("created_at") or ""),
        "isFavorite": bool(profile.get("is_favorite") or profile.get("isFavorite") or False),
        "status": "archived" if str(profile.get("status") or "").strip().lower() == "archived" else "active",
    }


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sort_stamp(value: Any) -> float:
    parsed = _parse_dt(value)
    if parsed is None:
        return 0
    return parsed.timestamp()


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
            nested = container.get(field)
            if isinstance(nested, dict):
                pending.append(nested)
    return containers


def _citation_metadata_count(item: dict[str, Any]) -> int | None:
    for container in _metadata_containers(item):
        for field in CITATION_FIELDS:
            value = _coerce_count(container.get(field))
            if value is not None:
                return value
    return None


def _citation_count(item: dict[str, Any]) -> int:
    return _citation_metadata_count(item) or 0


def _triage_score(item: dict[str, Any]) -> float | None:
    value = item.get("score")
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if isfinite(score) else None


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
        key = re.sub(r"\s+", " ", text).strip().lower()
        if text and key not in seen:
            normalized.append(text)
            seen.add(key)
    return normalized


def _canonical_institution_key(value: str) -> str:
    normalized = re.sub(r"[^\w\s&-]", " ", value.lower())
    normalized = re.sub(r"\b(univ|univ\.)\b", "university", normalized)
    normalized = re.sub(r"\b(inst|inst\.)\b", "institute", normalized)
    normalized = re.sub(r"\bdept\b", "department", normalized)
    normalized = re.sub(r"\btech\b", "technology", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    aliases = {
        "kaist": "korea advanced institute of science and technology",
        "k a i s t": "korea advanced institute of science and technology",
        "korea advanced institute of science and technology kaist": "korea advanced institute of science and technology",
        "massachusetts institute of technology mit": "massachusetts institute of technology",
        "mit": "massachusetts institute of technology",
        "uc berkeley": "university of california berkeley",
        "university of california berkeley": "university of california berkeley",
    }
    return aliases.get(normalized, normalized)


def _canonical_keyword(value: str) -> str:
    return paper_review.canonical_keyword(value)


def _run_id(run: dict[str, Any]) -> str | None:
    text = str(run.get("run_id") or run.get("runId") or "").strip()
    return text or None


def _source_run_ids(values: set[str]) -> list[str]:
    return sorted(value for value in values if value)


def _sample_ids(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
        if len(result) >= 3:
            break
    return result


def _topic_run_records(topic: str, *, profile_id: str | None = None) -> list[dict[str, Any]]:
    runs = [run for run in list_run_records(limit=None) if run_matches_topic(run, topic, profile_id)]
    runs.sort(
        key=lambda run: (
            _sort_stamp(run.get("finished_at") or run.get("updated_at") or run.get("created_at")),
            _sort_stamp(run.get("created_at")),
        ),
        reverse=True,
    )
    return runs


def _topic_items(topic: str, *, max_runs: int = MAX_SOURCE_RUNS,
                 profile_id: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in _topic_run_records(topic, profile_id=profile_id)[:max_runs]:
        run_id = str(run.get("run_id", "")).strip()
        if not run_id:
            continue
        try:
            for item in workbench.load_run_triage_items(run_id):
                items.append((run, item))
        except FileNotFoundError:
            continue
    return items


def _paper_identity(item: dict[str, Any]) -> str:
    for value in (
        item.get("paper_id"),
        item.get("entry_url"),
        item.get("entry_id"),
        item.get("pdf_url"),
        item.get("title"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return safe_filename(str(item))


def _paper_sort_key(run: dict[str, Any], item: dict[str, Any]) -> tuple[float, float, int, float, float]:
    return (
        float(item.get("topic_relevance_score", 0) or 0),
        float(item.get("score", 0) or 0),
        _citation_count(item),
        _sort_stamp(item.get("published") or item.get("updated")),
        _sort_stamp(run.get("created_at")),
    )


def _dedupe_items(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    deduped: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for run, item in items:
        key = _paper_identity(item)
        existing = deduped.get(key)
        if existing is None or _paper_sort_key(run, item) > _paper_sort_key(*existing):
            deduped[key] = (run, item)
    return sorted(deduped.values(), key=lambda pair: _paper_sort_key(pair[0], pair[1]), reverse=True)


def _rank_topic_items(
    profile: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    read_only: bool = False,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    ranked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    topic_id = str(profile.get("profile_id") or "").strip()
    for run, item in items:
        run_id = str(run.get("run_id") or "").strip()
        paper_id = str(item.get("paper_id") or _paper_identity(item)).strip()
        review = paper_reviews.get_review(topic_id, run_id, paper_id, read_only=read_only) if topic_id and run_id and paper_id else None
        enriched = dict(item)
        enriched["topic_relevance_score"] = paper_review.score_topic_relevance(profile, item, review)
        ranked.append((run, enriched))
    return sorted(ranked, key=lambda pair: _paper_sort_key(pair[0], pair[1]), reverse=True)


def _calendar_month(value: Any) -> str | None:
    text = str(value or "").strip()
    # Month-only provider metadata is valid, but a longer malformed date must
    # never be made valid by truncating it (e.g. 2026-02-30 or not-a-date).
    parsed = _parse_dt(f"{text}-01" if re.fullmatch(r"\d{4}-\d{2}", text) else text)
    return f"{parsed.year:04d}-{parsed.month:02d}" if parsed else None


def _paper_bucket_label(run: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    for field in ("published", "updated"):
        if month := _calendar_month(item.get(field)):
            return month, "published_month"
    if month := _calendar_month(run.get("created_at")):
        return month, "run_created_month_fallback"
    return "unknown", "unknown"


def _series_delta(values: list[float]) -> float:
    if len(values) < 2:
        return 0
    return round(values[-1] - values[-2], 2)


def _delta_percent(values: list[float]) -> float:
    if len(values) < 2 or values[-2] == 0:
        return 0
    return round(((values[-1] - values[-2]) / values[-2]) * 100, 2)


def _metric(value: float, sparkline: list[float]) -> dict[str, Any]:
    return {
        "value": round(value, 2),
        "delta": _series_delta(sparkline),
        "sparkline": [round(point, 2) for point in sparkline],
    }


def _analysis_timestamp(run: dict[str, Any]) -> str | None:
    steps = run.get("steps") if isinstance(run.get("steps"), list) else []
    analyzed = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("name") or "") not in {"summary", "synthesis"}:
            continue
        if str(step.get("status") or "") in {"completed", "completed_with_warnings"}:
            analyzed = True
            break
    if not analyzed:
        return None
    return str(run.get("finished_at") or run.get("updated_at") or run.get("created_at") or "").strip() or None


def _coverage_item(
    coverage_id: str,
    label: str,
    *,
    covered: int,
    total: int,
    empty_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "coverageId": coverage_id,
        "label": label,
        "covered": covered,
        "total": total,
        "percentage": round((covered / total) * 100, 1) if total else 0,
        "emptyReason": empty_reason,
    }


def _trend_points(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> tuple[list[dict[str, Any]], str]:
    buckets: dict[str, dict[str, Any]] = {}
    bucket_modes: set[str] = set()
    for run, item in items:
        label, mode = _paper_bucket_label(run, item)
        bucket_modes.add(mode)
        bucket = buckets.setdefault(
            label,
            {"total": 0, "core": 0, "scores": [], "citations": 0, "sourceRunIds": set(), "bucketModes": Counter()},
        )
        bucket["total"] += 1
        score = _triage_score(item)
        if (score is not None and score >= CORE_PAPER_SCORE_THRESHOLD) or float(item.get("topic_relevance_score", 0) or 0) >= 60:
            bucket["core"] += 1
        bucket["citations"] += _citation_count(item)
        if score is not None:
            bucket["scores"].append(score)
        if run_id := _run_id(run):
            bucket["sourceRunIds"].add(run_id)
        bucket["bucketModes"][mode] += 1

    labels = sorted(label for label in buckets if label != "unknown")[-12:]
    if not labels and "unknown" in buckets:
        labels = ["unknown"]

    points = []
    for label in labels:
        scores = buckets[label]["scores"]
        average_impact = round(sum(scores) / len(scores), 2) if scores else 0
        points.append(
            {
                "label": label,
                "totalPapers": int(buckets[label]["total"]),
                "corePapers": int(buckets[label]["core"]),
                "citations": int(buckets[label]["citations"]),
                "averageImpact": average_impact,
                "sourceRunIds": _source_run_ids(buckets[label]["sourceRunIds"]),
                "bucketMode": buckets[label]["bucketModes"].most_common(1)[0][0],
                "emptyReason": None,
            }
        )

    if bucket_modes == {"published_month"}:
        bucket_mode = "published_month"
    elif bucket_modes <= {"run_created_month_fallback", "unknown"}:
        bucket_mode = "run_created_month_fallback"
    else:
        bucket_mode = "mixed_source_month"
    return points, bucket_mode


def _keyword_details(
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    profile_terms = set(paper_review.topic_terms(profile or {})) if profile else set()
    strict_profile_filter = bool(
        profile_terms
        & {
            "large language model",
            "retrieval augmented generation",
            "foundation model",
            "alignment",
            "reasoning",
            "agent",
            "tool use",
        }
    )

    def add_keyword(
        *,
        keyword: str,
        raw_value: str,
        bucket_name: str,
        weight: float,
        paper_id: str,
        title: str,
        run_id: str | None,
    ) -> None:
        value = _canonical_keyword(keyword)
        if not paper_review.is_quality_keyword(value):
            return
        detail = details.setdefault(
            value,
            {
                "keyword": value,
                "score": 0.0,
                "paperIds": set(),
                "samplePaperIds": [],
                "sourceRunIds": set(),
                "sampleTitles": [],
                "rawVariants": Counter(),
                "primaryCount": 0,
                "secondaryCount": 0,
            },
        )
        already_for_paper = paper_id in detail["paperIds"]
        detail["score"] += float(weight)
        detail["paperIds"].add(paper_id)
        if len(detail["samplePaperIds"]) < 3 and paper_id not in detail["samplePaperIds"]:
            detail["samplePaperIds"].append(paper_id)
        if run_id:
            detail["sourceRunIds"].add(run_id)
        if raw_value and not already_for_paper:
            detail["rawVariants"][str(raw_value).strip()] += 1
        if len(detail["sampleTitles"]) < 3 and title not in detail["sampleTitles"]:
            detail["sampleTitles"].append(title)
        if bucket_name == "primary":
            detail["primaryCount"] += 1
        else:
            detail["secondaryCount"] += 1

    for run, item in items:
        paper_id = str(item.get("paper_id") or _paper_identity(item)).strip()
        title = str(item.get("title") or "제목 없음").strip() or "제목 없음"
        run_id = _run_id(run)
        triage = item.get("triage") if isinstance(item.get("triage"), dict) else {}
        keyword_hits = item.get("keyword_hits") if isinstance(item.get("keyword_hits"), dict) else {}
        primary_hits = [
            str(value).strip()
            for value in [*(triage.get("hits_primary") or []), *(keyword_hits.get("primary") or [])]
            if str(value).strip()
        ]
        secondary_hits = [
            str(value).strip()
            for value in [*(triage.get("hits_secondary") or []), *(keyword_hits.get("secondary") or [])]
            if str(value).strip()
        ]

        for bucket_name, values, weight in (
            ("primary", primary_hits, 2),
            ("secondary", secondary_hits, 1),
        ):
            for raw_value in values:
                canonical_raw = _canonical_keyword(raw_value)
                if strict_profile_filter and canonical_raw not in profile_terms:
                    continue
                add_keyword(
                    keyword=canonical_raw,
                    raw_value=raw_value,
                    bucket_name=bucket_name,
                    weight=float(weight),
                    paper_id=paper_id,
                    title=title,
                    run_id=run_id,
                )
        if profile:
            for phrase in paper_review.phrase_keywords_for_item(profile, item):
                if phrase in details and paper_id in details[phrase]["paperIds"]:
                    continue
                add_keyword(
                    keyword=phrase,
                    raw_value="",
                    bucket_name="primary",
                    weight=3,
                    paper_id=paper_id,
                    title=title,
                    run_id=run_id,
                )

    ranked = sorted(
        details.values(),
        key=lambda detail: (
            float(detail["score"]),
            len(detail["paperIds"]),
            str(detail["keyword"]),
        ),
        reverse=True,
    )
    return [
        {
            "keyword": detail["keyword"],
            "score": round(float(detail["score"]), 1),
            "paperCount": len(detail["paperIds"]),
            "sampleTitles": list(detail["sampleTitles"]),
            "samplePaperIds": list(detail["samplePaperIds"]),
            "sourceRunIds": _source_run_ids(detail["sourceRunIds"]),
            "rawVariants": [name for name, _ in detail["rawVariants"].most_common(4)],
            "explanation": (
                f"primary hit {detail['primaryCount']}회, secondary hit {detail['secondaryCount']}회가 "
                f"{len(detail['paperIds'])}편 논문, source run {len(detail['sourceRunIds'])}건에서 확인되었습니다. "
                "표기는 정규화된 키워드 기준입니다."
            ),
        }
        for detail in ranked[:10]
    ]


def _field_details(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    total = len(items)
    buckets: dict[str, dict[str, Any]] = {}
    for run, item in items:
        label = str(item.get("theme") or "").strip() or "미분류"
        bucket = buckets.setdefault(label, {"count": 0, "sampleTitles": [], "samplePaperIds": [], "sourceRunIds": set()})
        bucket["count"] += 1
        paper_id = str(item.get("paper_id") or _paper_identity(item)).strip()
        title = str(item.get("title") or "제목 없음").strip() or "제목 없음"
        if len(bucket["samplePaperIds"]) < 3 and paper_id not in bucket["samplePaperIds"]:
            bucket["samplePaperIds"].append(paper_id)
        if run_id := _run_id(run):
            bucket["sourceRunIds"].add(run_id)
        if len(bucket["sampleTitles"]) < 3 and title not in bucket["sampleTitles"]:
            bucket["sampleTitles"].append(title)
    ranked = sorted(buckets.items(), key=lambda pair: (-pair[1]["count"], pair[0]))
    return [
        {
            "label": label,
            "count": bucket["count"],
            "percentage": round((bucket["count"] / total) * 100, 1) if total else 0,
            "sampleTitles": list(bucket["sampleTitles"]),
            "samplePaperIds": list(bucket["samplePaperIds"]),
            "sourceRunIds": _source_run_ids(bucket["sourceRunIds"]),
            "emptyReason": None,
        }
        for label, bucket in ranked
    ]


def _institution_details(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> tuple[list[dict[str, Any]], str | None]:
    buckets: dict[str, dict[str, Any]] = {}
    for run, item in items:
        title = str(item.get("title") or "제목 없음").strip() or "제목 없음"
        paper_id = str(item.get("paper_id") or _paper_identity(item)).strip()
        run_id = _run_id(run)
        seen_for_paper: set[str] = set()
        for raw_name in _institution_values(item):
            canonical_key = _canonical_institution_key(raw_name)
            if not canonical_key or canonical_key in seen_for_paper:
                continue
            seen_for_paper.add(canonical_key)
            bucket = buckets.setdefault(
                canonical_key,
                {
                    "count": 0,
                    "rawVariants": Counter(),
                    "sampleTitles": [],
                    "samplePaperIds": [],
                    "sourceRunIds": set(),
                },
            )
            bucket["count"] += 1
            bucket["rawVariants"][raw_name] += 1
            if len(bucket["samplePaperIds"]) < 3 and paper_id not in bucket["samplePaperIds"]:
                bucket["samplePaperIds"].append(paper_id)
            if run_id:
                bucket["sourceRunIds"].add(run_id)
            if len(bucket["sampleTitles"]) < 3 and title not in bucket["sampleTitles"]:
                bucket["sampleTitles"].append(title)

    if not buckets:
        return [], "논문 메타데이터에 기관/소속 정보가 없어 기관 분포를 계산할 수 없습니다."

    ranked = sorted(buckets.items(), key=lambda pair: (-pair[1]["count"], pair[0]))
    details = []
    for canonical_key, bucket in ranked[:8]:
        display_name, _ = bucket["rawVariants"].most_common(1)[0]
        details.append(
            {
                "name": display_name,
                "canonicalName": canonical_key,
                "count": bucket["count"],
                "rawVariants": [name for name, _ in bucket["rawVariants"].most_common(4)],
                "sampleTitles": list(bucket["sampleTitles"]),
                "samplePaperIds": list(bucket["samplePaperIds"]),
                "sourceRunIds": _source_run_ids(bucket["sourceRunIds"]),
                "emptyReason": None,
            }
        )
    return details, None


def _latest_core_papers(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    papers: list[dict[str, Any]] = []
    rationales: list[dict[str, Any]] = []
    core_items = [
        (run, item)
        for run, item in sorted(items, key=lambda pair: _paper_sort_key(pair[0], pair[1]), reverse=True)
        if float(item.get("score", 0) or 0) >= CORE_PAPER_SCORE_THRESHOLD
        or float(item.get("topic_relevance_score", 0) or 0) >= 60
    ]
    for run, item in core_items[:8]:
        paper_id = str(item.get("paper_id", "")).strip() or _paper_identity(item)
        citations = _citation_count(item)
        score = float(item.get("score", 0) or 0)
        relevance = float(item.get("topic_relevance_score", 0) or 0)
        published_at = item.get("published") or item.get("updated")
        title = str(item.get("title", "")).strip() or "Untitled paper"
        citation_text = f"인용 {citations}회" if citations > 0 else "인용 메타데이터 없음"
        published_text = str(published_at).split("T", 1)[0] if published_at else "발행일 미상"
        relevance_text = f", 주제 관련도 {round(relevance, 1)}점" if relevance > 0 else ""
        rationale = f"영향력 점수 {round(score, 1)}점{relevance_text}, {citation_text}, {published_text} 기준으로 상위권에 포함되었습니다."
        papers.append(
            {
                "id": paper_id,
                "title": title,
                "authors": _item_authors(item),
                "publishedAt": published_at,
                "citations": citations,
                "impactScore": score,
                "sourceUrl": item.get("entry_url") or item.get("entry_id") or item.get("pdf_url"),
                "sourceProvider": item.get("source_provider") or item.get("source"),
                "runId": str(run.get("run_id", "")).strip() or None,
                "rationale": rationale,
                "saved": True,
            }
        )
        rationales.append(
            {
                "paperId": paper_id,
                "title": title,
                "runId": str(run.get("run_id", "")).strip() or None,
                "publishedAt": published_at,
                "citations": citations,
                "impactScore": round(score, 1),
                "rationale": rationale,
                "sourceUrl": item.get("entry_url") or item.get("entry_id") or item.get("pdf_url"),
            }
        )
    return papers, rationales


def _review_coverage_payload(
    profile: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    coverage = paper_review.review_coverage_for_rows(profile, items, read_only=read_only)
    candidate_counts = _topic_candidate_review_counts(profile, items, read_only=read_only)
    if candidate_counts is None:
        return coverage

    denominator = int(coverage.get("totalPapers") or 0)
    reviewed = candidate_counts["reviewed"]
    full_text_available = candidate_counts["full_text_available"]
    full_text_reviewed = candidate_counts["full_text_reviewed"]
    abstract_only = candidate_counts["abstract_only"]
    return {
        **coverage,
        "reviewedCount": reviewed,
        "reviewedUniqueCount": reviewed,
        "abstractOnlyCount": abstract_only,
        "abstractOnlyUniqueCount": abstract_only,
        "fullTextAvailableUniqueCount": full_text_available,
        "fullTextReviewedCount": full_text_available + full_text_reviewed,
        "fullTextReviewedUniqueCount": full_text_reviewed,
        "coveragePercentage": round((reviewed / denominator) * 100, 1) if denominator else 0.0,
    }


def _topic_candidate_review_counts(
    profile: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    read_only: bool = False,
) -> dict[str, int] | None:
    topic_id = str(profile.get("profile_id") or "").strip()
    if not topic_id:
        return None
    consolidated = paper_review._consolidate_rows(profile, items)
    candidate_ids = {
        str(candidate.get("normalized_paper_id") or "").strip()
        for candidate in consolidated["candidates"]
        if str(candidate.get("normalized_paper_id") or "").strip()
    }
    if not candidate_ids:
        return None

    existing_by_normalized = paper_review._existing_reviews_by_normalized_paper_id(topic_id, read_only=read_only)
    counts = {
        "reviewed": 0,
        "abstract_only": 0,
        "full_text_available": 0,
        "full_text_reviewed": 0,
    }
    for normalized_id in candidate_ids:
        review = existing_by_normalized.get(normalized_id)
        if review is None:
            continue
        counts["reviewed"] += 1
        status = str(review.get("full_text_status") or "unavailable").strip().lower()
        if status == "abstract_only":
            counts["abstract_only"] += 1
        elif status == "full_text_available":
            counts["full_text_available"] += 1
        elif status == "reviewed":
            counts["full_text_reviewed"] += 1
    return counts


def _source_runs(run_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "runId": str(run.get("run_id", "")),
            "status": str(run.get("status") or ""),
            "createdAt": str(run.get("created_at") or ""),
            "finishedAt": str(run.get("finished_at") or run.get("updated_at") or "") or None,
        }
        for run in run_rows[:MAX_SOURCE_RUNS]
    ]


def _last_analysis_at(run_rows: list[dict[str, Any]]) -> str | None:
    for run in run_rows:
        timestamp = _analysis_timestamp(run)
        if timestamp:
            return timestamp
    return None


def _freshness(
    *,
    computed_at: str,
    last_analysis_at: str | None,
    source_runs: list[dict[str, Any]],
    source_paper_count: int,
) -> dict[str, Any]:
    newest_source_run_at = None
    for run in source_runs:
        newest_source_run_at = run.get("finishedAt") or run.get("createdAt")
        if newest_source_run_at:
            break

    if not source_runs:
        status = "empty"
        label = "분석할 source run이 없습니다."
        stale_reason = "수집 또는 분석 run을 실행하면 analytics artifact가 채워집니다."
    elif source_paper_count == 0:
        status = "sparse"
        label = "source run은 있으나 논문 artifact가 비어 있습니다."
        stale_reason = "수집 산출물 또는 triage artifact가 아직 저장되지 않았습니다."
    elif not last_analysis_at:
        status = "runtime_only"
        label = "분석 완료 시각이 없어 최근 run 산출물 기준입니다."
        stale_reason = "summary/synthesis 완료 step을 확인할 수 없습니다."
    else:
        status = "fresh"
        label = "최근 분석 artifact 기준입니다."
        stale_reason = None

    return {
        "status": status,
        "label": label,
        "computedAt": computed_at,
        "lastAnalysisAt": last_analysis_at,
        "newestSourceRunAt": newest_source_run_at,
        "sourceRunCount": len(source_runs),
        "sourcePaperCount": source_paper_count,
        "staleReason": stale_reason,
    }


def _derivations(computed_at: str, source_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_run_ids = [str(run.get("runId") or "") for run in source_runs if str(run.get("runId") or "").strip()]
    return [
        {
            **definition,
            "computedAt": computed_at,
            "sourceRunIds": source_run_ids,
        }
        for definition in DERIVATION_DEFINITIONS
    ]


def build_runtime_provenance(
    surface_id: str,
    *,
    computed_at: str,
    source_runs: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    definition = next(
        (row for row in DERIVATION_DEFINITIONS if row["surfaceId"] == surface_id),
        {
            "surfaceId": surface_id,
            "label": surface_id,
            "mode": "runtime_adapter",
            "definition": "요청 시점에 계산되는 runtime adapter입니다.",
        },
    )
    source_run_ids = [str(run.get("runId") or run.get("run_id") or "") for run in source_runs if str(run.get("runId") or run.get("run_id") or "").strip()]
    return {
        **definition,
        "computedAt": computed_at,
        "sourceRunIds": source_run_ids,
        "warnings": list(warnings or []),
    }


def _warnings(
    *,
    total_papers: int,
    citation_coverage: int,
    keyword_coverage: int,
    institution_empty_reason: str | None,
    last_analysis_at: str | None,
) -> list[str]:
    warnings: list[str] = []
    if total_papers == 0:
        warnings.append("이 토픽에 아직 저장된 논문 데이터가 없어 일부 지표는 0으로 표시됩니다.")
    if total_papers > 0 and citation_coverage == 0:
        warnings.append("인용 메타데이터가 없어 인용 수를 확인할 수 없습니다.")
    if total_papers > 0 and keyword_coverage == 0:
        warnings.append("키워드 히트 메타데이터가 없어 키워드 인사이트는 비어 있을 수 있습니다.")
    if institution_empty_reason:
        warnings.append("기관/소속 메타데이터가 아직 수집되지 않아 기관 순위는 빈 상태로 표시됩니다.")
    if total_papers > 0 and not last_analysis_at:
        warnings.append("최근 분석 완료 시각을 확인할 수 없어 일부 인사이트는 최신 run 기준으로만 계산되었습니다.")
    return warnings


def _empty_snapshot(profile: dict[str, Any], generated_at: str) -> dict[str, Any]:
    warning = "이 토픽에 아직 저장된 논문 데이터가 없어 일부 지표는 0으로 표시됩니다."
    freshness = _freshness(computed_at=generated_at, last_analysis_at=None, source_runs=[], source_paper_count=0)
    return {
        "profileId": str(profile.get("profile_id") or ""),
        "artifactVersion": ANALYTICS_ARTIFACT_VERSION,
        "computedAt": generated_at,
        "generatedAt": generated_at,
        "lastAnalysisAt": None,
        "freshness": freshness,
        "sourceRuns": [],
        "coverage": [
            _coverage_item("published_metadata", "발행일 메타데이터", covered=0, total=0, empty_reason="수집된 논문이 없습니다."),
            _coverage_item("triage_score", "선별 점수", covered=0, total=0, empty_reason="수집된 논문이 없습니다."),
            _coverage_item("citation_metadata", "인용 메타데이터", covered=0, total=0, empty_reason="수집된 논문이 없습니다."),
            _coverage_item("keyword_hits", "키워드 히트", covered=0, total=0, empty_reason="수집된 논문이 없습니다."),
            _coverage_item("institution_metadata", "기관/소속 메타데이터", covered=0, total=0, empty_reason="수집된 논문이 없습니다."),
        ],
        "metricDefinitions": list(METRIC_DEFINITIONS),
        "derivations": _derivations(generated_at, []),
        "trendDetail": {
            "bucketMode": "published_month",
            "summary": "수집된 논문이 없어 추세를 계산하지 못했습니다.",
            "points": [],
        },
        "keywordDetails": [],
        "fieldDetails": [],
        "institutionDetails": [],
        "corePaperDetails": [],
        "metrics": {
            "collectedPapers": _metric(0, []),
            "corePapers": _metric(0, []),
            "averageImpact": _metric(0, []),
            "totalCitations": _metric(0, []),
            "trendIndex": {"label": "0", "deltaPercent": 0, "sparkline": []},
        },
        "trends": {"labels": [], "totalPapers": [], "corePapers": [], "citations": []},
        "hotKeywords": [],
        "latestCorePapers": [],
        "fieldDistribution": [],
        "institutionDistribution": [],
        "reviewCoverage": {
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
        "warnings": [warning],
    }


def _save_snapshot(profile_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    ensure_dir(ANALYTICS_LIBRARY_DIR)
    write_json(_analytics_path(profile_id), snapshot)
    metadata_index.touch_index_state("last_topic_analytics_updated_at")
    return snapshot


def load_topic_analytics(profile_id: str) -> dict[str, Any] | None:
    path = _analytics_path(str(profile_id or "").strip())
    payload = read_json(path, default=None)
    return payload if isinstance(payload, dict) else None


def build_topic_analytics_snapshot(
    profile_id: str,
    *,
    read_only: bool = True,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with ANALYTICS_LOCK:
        profile = dict(profile) if profile is not None else topic_profiles.get_topic_profile(str(profile_id or "").strip())
        profile_id = str(profile["profile_id"])
        topic = str(profile.get("topic") or "")
        generated_at = iso_now_utc()
        run_rows = _topic_run_records(topic, profile_id=profile_id)
        source_runs = _source_runs(run_rows)
        raw_items = _topic_items(topic, profile_id=profile_id)
        items = _rank_topic_items(profile, _dedupe_items(raw_items), read_only=read_only)
        if not items:
            snapshot = _empty_snapshot(profile, generated_at)
            snapshot["sourceRuns"] = source_runs
            snapshot["lastAnalysisAt"] = _last_analysis_at(run_rows)
            snapshot["freshness"] = _freshness(
                computed_at=generated_at,
                last_analysis_at=snapshot["lastAnalysisAt"],
                source_runs=source_runs,
                source_paper_count=0,
            )
            snapshot["derivations"] = _derivations(generated_at, source_runs)
            snapshot["reviewCoverage"] = _review_coverage_payload(profile, raw_items, read_only=read_only)
            return snapshot

        trend_points, bucket_mode = _trend_points(items)
        keyword_details = _keyword_details(items, profile=profile)
        field_details = _field_details(items)
        institution_details, institution_empty_reason = _institution_details(items)
        latest_core_papers, core_paper_details = _latest_core_papers(items)

        total_series = [float(point["totalPapers"]) for point in trend_points]
        core_series = [float(point["corePapers"]) for point in trend_points]
        impact_series = [float(point["averageImpact"]) for point in trend_points]
        citation_series = [float(point["citations"]) for point in trend_points]
        total_papers = len(items)
        total_citations = sum(_citation_count(item) for _, item in items)
        review_coverage = _review_coverage_payload(profile, raw_items, read_only=read_only)
        published_coverage = sum(
            1 for _, item in items if _calendar_month(item.get("published")) or _calendar_month(item.get("updated"))
        )
        citation_coverage = sum(1 for _, item in items if _citation_metadata_count(item) is not None)
        keyword_coverage = sum(1 for _, item in items if any(detail for detail in (item.get("triage"), item.get("keyword_hits")) if detail))
        institution_coverage = sum(1 for _, item in items if _institution_values(item))
        scores = [score for _, item in items if (score := _triage_score(item)) is not None]
        average_impact = round(sum(scores) / len(scores), 2) if scores else 0
        core_count = sum(
            1
            for _, item in items
            if float(item.get("score", 0) or 0) >= CORE_PAPER_SCORE_THRESHOLD
            or float(item.get("topic_relevance_score", 0) or 0) >= 60
        )
        last_analysis_at = _last_analysis_at(run_rows)

        latest_bucket = trend_points[-1] if trend_points else None
        if latest_bucket:
            trend_summary = (
                f"최근 버킷 {latest_bucket['label']}에 논문 {latest_bucket['totalPapers']}편, "
                f"핵심 논문 {latest_bucket['corePapers']}편, 인용 합계 {latest_bucket['citations']}회가 집계되었습니다."
            )
        else:
            trend_summary = "추세를 계산할 버킷이 없습니다."

        warnings = _warnings(
            total_papers=total_papers,
            citation_coverage=citation_coverage,
            keyword_coverage=keyword_coverage,
            institution_empty_reason=institution_empty_reason,
            last_analysis_at=last_analysis_at,
        )
        date_modes = Counter(_paper_bucket_label(run, item)[1] for run, item in items)
        if fallback_count := date_modes["run_created_month_fallback"]:
            warnings.append(f"유효한 발행일·갱신일이 없는 논문 {fallback_count}편은 실행월로 집계했습니다.")
        if unknown_count := date_modes["unknown"]:
            warnings.append(f"날짜를 확인할 수 없는 논문 {unknown_count}편은 월별 분포에 표시할 수 없습니다.")

        snapshot = {
            "profileId": str(profile.get("profile_id") or ""),
            "artifactVersion": ANALYTICS_ARTIFACT_VERSION,
            "computedAt": generated_at,
            "generatedAt": generated_at,
            "lastAnalysisAt": last_analysis_at,
            "freshness": _freshness(
                computed_at=generated_at,
                last_analysis_at=last_analysis_at,
                source_runs=source_runs,
                source_paper_count=total_papers,
            ),
            "sourceRuns": source_runs,
            "coverage": [
                _coverage_item("published_metadata", "발행일 메타데이터", covered=published_coverage, total=total_papers),
                _coverage_item(
                    "triage_score", "선별 점수", covered=len(scores), total=total_papers,
                    empty_reason="확인된 선별 점수가 없습니다." if not scores else None,
                ),
                _coverage_item(
                    "citation_metadata",
                    "인용 메타데이터",
                    covered=citation_coverage,
                    total=total_papers,
                    empty_reason="인용 관련 메타데이터가 아직 저장되지 않았습니다." if citation_coverage == 0 else None,
                ),
                _coverage_item(
                    "keyword_hits",
                    "키워드 히트",
                    covered=keyword_coverage,
                    total=total_papers,
                    empty_reason="triage/keyword hit 메타데이터가 없어 키워드 랭킹을 계산하지 못했습니다." if keyword_coverage == 0 else None,
                ),
                _coverage_item(
                    "institution_metadata",
                    "기관/소속 메타데이터",
                    covered=institution_coverage,
                    total=total_papers,
                    empty_reason=institution_empty_reason,
                ),
            ],
            "metricDefinitions": list(METRIC_DEFINITIONS),
            "derivations": _derivations(generated_at, source_runs),
            "trendDetail": {
                "bucketMode": bucket_mode,
                "summary": trend_summary,
                "points": trend_points,
            },
            "keywordDetails": keyword_details,
            "fieldDetails": field_details,
            "institutionDetails": institution_details,
            "corePaperDetails": core_paper_details,
            "metrics": {
                "collectedPapers": _metric(float(total_papers), total_series),
                "corePapers": _metric(float(core_count), core_series),
                "averageImpact": _metric(float(average_impact), impact_series),
                "totalCitations": _metric(float(total_citations), citation_series),
                "trendIndex": {
                    "label": str(trend_points[-1]["label"]) if trend_points else "0",
                    "deltaPercent": _delta_percent(total_series),
                    "sparkline": [round(point, 2) for point in total_series],
                },
            },
            "trends": {
                "labels": [point["label"] for point in trend_points],
                "totalPapers": [point["totalPapers"] for point in trend_points],
                "corePapers": [point["corePapers"] for point in trend_points],
                "citations": [point["citations"] for point in trend_points],
            },
            "hotKeywords": [
                {
                    "rank": index,
                    "keyword": row["keyword"],
                    "score": row["score"],
                    "paperCount": row["paperCount"],
                    "sourceRunIds": row["sourceRunIds"],
                    "explanation": row["explanation"],
                }
                for index, row in enumerate(keyword_details[:8], start=1)
            ],
            "latestCorePapers": latest_core_papers,
            "fieldDistribution": [
                {
                    "label": row["label"],
                    "count": row["count"],
                    "percentage": row["percentage"],
                    "sampleTitles": row["sampleTitles"],
                    "samplePaperIds": row["samplePaperIds"],
                    "sourceRunIds": row["sourceRunIds"],
                    "emptyReason": row.get("emptyReason"),
                }
                for row in field_details
            ],
            "institutionDistribution": [
                {
                    "name": row["name"],
                    "canonicalName": row.get("canonicalName"),
                    "count": row["count"],
                    "rawVariants": row["rawVariants"],
                    "sampleTitles": row["sampleTitles"],
                    "samplePaperIds": row["samplePaperIds"],
                    "sourceRunIds": row["sourceRunIds"],
                    "emptyReason": row.get("emptyReason"),
                }
                for row in institution_details[:5]
            ],
            "reviewCoverage": review_coverage,
            "warnings": warnings,
        }
        return snapshot


def refresh_topic_analytics(profile_id: str) -> dict[str, Any]:
    with ANALYTICS_LOCK:
        snapshot = build_topic_analytics_snapshot(profile_id, read_only=False)
        snapshot_profile_id = str(snapshot.get("profileId") or profile_id or "").strip()
        return _save_snapshot(snapshot_profile_id, snapshot)


def refresh_topic_analytics_for_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    explicit_profile_id = str(run.get("topic_profile_id") or "").strip()
    profile_ids: list[str] = []
    if explicit_profile_id:
        profile_ids.append(explicit_profile_id)
    else:
        topic_name = str(run.get("topic") or "").strip().lower()
        for profile in topic_profiles.list_topic_profiles()["items"]:
            if topic_name in {
                str(profile.get("topic") or "").strip().lower(),
                str(profile.get("title") or "").strip().lower(),
            }:
                profile_ids.append(str(profile.get("profile_id") or "").strip())

    refreshed: list[dict[str, Any]] = []
    with ANALYTICS_LOCK:
        for profile_id in dict.fromkeys([profile_id for profile_id in profile_ids if profile_id]):
            try:
                refreshed.append(refresh_topic_analytics(profile_id))
            except FileNotFoundError:
                continue
    return refreshed

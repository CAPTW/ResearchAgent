from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx

from app.schemas import dashboard as dashboard_schemas
from app.services import topic_analytics, topic_dashboard, topic_profiles


def _profile(
    profile_id: str,
    title: str,
    *,
    favorite: bool = False,
    status: str = "active",
    updated_at: str = "2026-05-07T09:00:00+09:00",
) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "title": title,
        "topic": title,
        "description": f"{title} research",
        "is_favorite": favorite,
        "status": status,
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def _analytics(
    *,
    collected: int,
    core: int,
    full_text_reviewed: int,
    total: int,
    coverage: float,
    delta: int,
    sparkline: list[int],
    trend_label: str,
    trend_delta: int,
) -> dict[str, Any]:
    return {
        "metrics": {
            "collectedPapers": {"value": collected, "delta": delta, "sparkline": sparkline},
            "corePapers": {"value": core, "delta": 0, "sparkline": []},
            "trendIndex": {"label": trend_label, "deltaPercent": trend_delta, "sparkline": []},
        },
        "reviewCoverage": {
            "totalPapers": total,
            "fullTextReviewedCount": full_text_reviewed,
            "coveragePercentage": coverage,
        },
    }


def _overview_builder() -> Callable[[], dict[str, Any]]:
    builder = getattr(topic_dashboard, "build_api_topics_overview", None)
    assert callable(builder), "build_api_topics_overview must exist"
    return builder


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("forbidden write, provider, or HTTP helper was called")


def test_build_api_topics_overview_projects_two_topics_with_exact_summary_once(monkeypatch) -> None:
    profiles = [
        _profile("xr", "Extended Reality"),
        _profile("llm", "Large Language Models", favorite=True),
        _profile("archived", "Archived Topic", status="archived"),
        _profile("xr", "Duplicate Extended Reality"),
    ]
    snapshots = {
        "xr": _analytics(
            collected=30,
            core=10,
            full_text_reviewed=5,
            total=30,
            coverage=20,
            delta=4,
            sparkline=[18, 22, 30],
            trend_label="안정",
            trend_delta=3,
        ),
        "llm": _analytics(
            collected=120,
            core=40,
            full_text_reviewed=40,
            total=120,
            coverage=65,
            delta=12,
            sparkline=[84, 96, 108, 120],
            trend_label="상승",
            trend_delta=14,
        ),
    }
    profile_loads = 0
    analytics_loads: list[str] = []

    def load_profiles() -> list[dict[str, Any]]:
        nonlocal profile_loads
        profile_loads += 1
        return profiles

    def load_analytics(profile_id: str) -> dict[str, Any]:
        analytics_loads.append(profile_id)
        return snapshots[profile_id]

    monkeypatch.setattr(topic_dashboard, "_list_topic_profiles_read_only", load_profiles, raising=False)
    monkeypatch.setattr(topic_dashboard.topic_analytics, "load_topic_analytics", load_analytics)
    monkeypatch.setattr(topic_dashboard.topic_analytics, "build_topic_analytics_snapshot", _fail_if_called)
    # Profile writes now go through the shared-state mutation boundary, so the
    # read-only guard targets that boundary instead of the removed write_json.
    monkeypatch.setattr(topic_profiles.shared_state, "mutate_json_state", _fail_if_called)
    monkeypatch.setattr(topic_analytics, "_save_snapshot", _fail_if_called)
    monkeypatch.setattr(topic_dashboard.metadata_index, "touch_index_state", _fail_if_called)
    monkeypatch.setattr(topic_dashboard.recommendations, "build_recommendations_for_topic", _fail_if_called)
    monkeypatch.setattr(topic_dashboard.provider_configurations, "validate_provider_configuration", _fail_if_called)
    monkeypatch.setattr(httpx, "request", _fail_if_called)
    monkeypatch.setattr(httpx.Client, "request", _fail_if_called)

    result = _overview_builder()()

    assert profile_loads == 1
    assert analytics_loads == ["xr", "llm"]
    assert result["summary"] == {
        "topicCount": 2,
        "collectedPapers": 150,
        "fullTextReviewedCount": 45,
    }
    assert [item["id"] for item in result["items"]] == ["xr", "llm"]
    assert result["items"][0] == {
        "id": "xr",
        "name": "Extended Reality",
        "description": "Extended Reality research",
        "updatedAt": "2026-05-07T09:00:00+09:00",
        "isFavorite": False,
        "collectedPapers": 30,
        "collectedDelta": 4,
        "collectedSparkline": [18, 22, 30],
        "corePapers": 10,
        "coveragePercentage": 20,
        "fullTextReviewedCount": 5,
        "totalPapers": 30,
        "trendLabel": "안정",
        "trendDeltaPercent": 3,
        "degraded": False,
    }
    schema = getattr(dashboard_schemas, "ApiTopicsOverviewResponse", None)
    assert schema is not None, "ApiTopicsOverviewResponse must exist"
    schema.model_validate(result)


def test_build_api_topics_overview_returns_deterministic_zero_topic_contract(monkeypatch) -> None:
    profile_loads = 0

    def load_profiles() -> list[dict[str, Any]]:
        nonlocal profile_loads
        profile_loads += 1
        return []

    monkeypatch.setattr(topic_dashboard, "_list_topic_profiles_read_only", load_profiles, raising=False)
    monkeypatch.setattr(topic_dashboard.topic_analytics, "load_topic_analytics", _fail_if_called)

    builder = _overview_builder()
    first = builder()
    second = builder()

    assert first == second == {
        "summary": {"topicCount": 0, "collectedPapers": 0, "fullTextReviewedCount": 0},
        "items": [],
    }
    assert profile_loads == 2


def test_build_api_topics_overview_uses_read_only_snapshot_fallback_with_loaded_profile(monkeypatch) -> None:
    profile = _profile("llm", "Large Language Models", favorite=True)
    snapshot = _analytics(
        collected=12,
        core=4,
        full_text_reviewed=3,
        total=12,
        coverage=25,
        delta=2,
        sparkline=[8, 10, 12],
        trend_label="상승",
        trend_delta=8,
    )
    captured: dict[str, Any] = {}

    def build_snapshot(profile_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"profile_id": profile_id, **kwargs})
        return snapshot

    monkeypatch.setattr(topic_dashboard, "_list_topic_profiles_read_only", lambda: [profile], raising=False)
    monkeypatch.setattr(topic_dashboard.topic_analytics, "load_topic_analytics", lambda _profile_id: None)
    monkeypatch.setattr(topic_dashboard.topic_analytics, "build_topic_analytics_snapshot", build_snapshot)

    result = _overview_builder()()

    assert captured == {"profile_id": "llm", "read_only": True, "profile": profile}
    assert result["items"][0]["collectedPapers"] == 12
    assert result["items"][0]["degraded"] is False


def test_build_api_topics_overview_preserves_degraded_card_on_analytics_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        topic_dashboard,
        "_list_topic_profiles_read_only",
        lambda: [_profile("xr", "Extended Reality")],
        raising=False,
    )
    monkeypatch.setattr(topic_dashboard.topic_analytics, "load_topic_analytics", _fail_if_called)

    result = _overview_builder()()

    assert result["summary"] == {
        "topicCount": 1,
        "collectedPapers": 0,
        "fullTextReviewedCount": 0,
    }
    assert result["items"][0]["degraded"] is True
    assert result["items"][0]["collectedPapers"] == 0
    assert result["items"][0]["collectedSparkline"] == []


def test_overview_profile_loader_is_read_only_and_preserves_profile_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "profiles.json"
    profile_path.write_text(
        json.dumps(
            [
                _profile("newer", "Newer", updated_at="2026-05-08T09:00:00+09:00"),
                _profile("favorite", "Favorite", favorite=True, updated_at="2026-05-01T09:00:00+09:00"),
                _profile("archived", "Archived", status="archived", updated_at="2026-05-09T09:00:00+09:00"),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)
    monkeypatch.setattr(topic_profiles.shared_state, "mutate_json_state", _fail_if_called)
    loader = getattr(topic_dashboard, "_list_topic_profiles_read_only", None)
    assert callable(loader), "_list_topic_profiles_read_only must exist"

    result = loader()

    assert [profile["profile_id"] for profile in result] == ["favorite", "newer", "archived"]


def test_topic_analytics_snapshot_accepts_loaded_profile_without_reloading_or_persisting(monkeypatch) -> None:
    profile = _profile("llm", "Large Language Models")
    monkeypatch.setattr(topic_analytics.topic_profiles, "get_topic_profile", _fail_if_called)
    monkeypatch.setattr(topic_analytics, "_topic_run_records", lambda _topic, **kwargs: [])
    monkeypatch.setattr(topic_analytics, "_topic_items", lambda _topic, **kwargs: [])
    monkeypatch.setattr(
        topic_analytics,
        "_review_coverage_payload",
        lambda _profile, _items, *, read_only: {
            "totalPapers": 0,
            "reviewedCount": 0,
            "abstractOnlyCount": 0,
            "fullTextReviewedCount": 0,
            "coveragePercentage": 0,
            "rawIndexedPaperCount": 0,
            "uniqueCandidateCount": 0,
            "topicRelevantUniqueCount": 0,
            "reviewedUniqueCount": 0,
            "abstractOnlyUniqueCount": 0,
            "fullTextAvailableUniqueCount": 0,
            "fullTextReviewedUniqueCount": 0,
        },
    )
    monkeypatch.setattr(topic_analytics, "_save_snapshot", _fail_if_called)

    snapshot = topic_analytics.build_topic_analytics_snapshot("llm", read_only=True, profile=profile)

    assert snapshot["profileId"] == "llm"
    assert snapshot["metrics"]["collectedPapers"]["value"] == 0


def test_topics_overview_static_route_uses_typed_contract(client, monkeypatch) -> None:
    payload = {
        "summary": {"topicCount": 0, "collectedPapers": 0, "fullTextReviewedCount": 0},
        "items": [],
    }
    monkeypatch.setattr(topic_dashboard, "build_api_topics_overview", lambda: payload, raising=False)

    response = client.get("/api/topics/overview")

    assert response.status_code == 200, response.text
    assert response.json() == payload
    route = next(route for route in client.app.routes if getattr(route, "path", None) == "/api/topics/overview")
    assert route.methods == {"GET"}
    assert route.response_model is getattr(dashboard_schemas, "ApiTopicsOverviewResponse")

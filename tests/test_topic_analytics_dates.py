from __future__ import annotations

import pytest

from app.repositories.files import write_json
from app.services import topic_analytics


@pytest.mark.parametrize(
    ("published", "updated", "created_at", "expected"),
    [
        ("not-a-date", None, "2026-09-26T00:00:00Z", ("2026-09", "run_created_month_fallback")),
        ("2026-02-30", None, "2026-09-26", ("2026-09", "run_created_month_fallback")),
        ("2026-13", None, "2026-09-26", ("2026-09", "run_created_month_fallback")),
        ("0000-01", None, "2026-09-26", ("2026-09", "run_created_month_fallback")),
        ("2026-01-garbage", None, "2026-09-26", ("2026-09", "run_created_month_fallback")),
        ("not-a-date", "2025-04-02", "2026-09-26", ("2025-04", "published_month")),
        ("2024-02-29", None, "2026-09-26", ("2024-02", "published_month")),
        ("2025-04", None, "2026-09-26", ("2025-04", "published_month")),
        ("2025-04-01T00:30:00+09:00", None, "2026-09-26", ("2025-04", "published_month")),
        (None, None, "also-invalid", ("unknown", "unknown")),
    ],
)
def test_month_buckets_require_valid_calendar_dates(published, updated, created_at, expected) -> None:
    assert topic_analytics._paper_bucket_label(
        {"created_at": created_at}, {"published": published, "updated": updated}
    ) == expected


def test_read_only_snapshot_explains_date_fallback_and_known_zero_citations(tmp_path, isolated_run_repo, monkeypatch) -> None:
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", tmp_path / "analytics")
    run = {"run_id": "synthetic-run", "created_at": "2026-09-26T00:00:00Z"}
    items = [
        (run, {"paper_id": "invalid", "published": "not-a-date", "score": 20, "citations": 0}),
        (run, {"paper_id": "valid", "published": "2024-02-29", "score": 60}),
        (run, {"paper_id": "unscored", "published": "2024-02-29"}),
    ]
    monkeypatch.setattr(topic_analytics, "_topic_run_records", lambda _, **kwargs: [])
    monkeypatch.setattr(topic_analytics, "_topic_items", lambda _, **kwargs: items)
    profile = {"profile_id": "date-contract", "topic": "date-contract"}
    snapshot = topic_analytics.build_topic_analytics_snapshot("date-contract", profile=profile)
    assert snapshot["artifactVersion"] == "topic-analytics-v4"
    assert snapshot["trends"]["labels"] == ["2024-02", "2026-09"]
    assert snapshot["trends"]["totalPapers"] == [2, 1]
    assert snapshot["metrics"]["collectedPapers"]["value"] == 3
    coverage = {item["coverageId"]: item for item in snapshot["coverage"]}
    assert coverage["published_metadata"]["covered"] == 2
    assert coverage["citation_metadata"]["covered"] == 1
    assert coverage["triage_score"]["covered"] == 2
    assert coverage["triage_score"]["total"] == 3
    assert snapshot["metrics"]["averageImpact"]["value"] == 40
    assert snapshot["metrics"]["totalCitations"]["value"] == 0
    assert any("실행월" in warning for warning in snapshot["warnings"])
    assert not (tmp_path / "analytics").exists()


def test_loading_legacy_dashboard_artifact_preserves_its_version_and_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", tmp_path)
    legacy = {
        "profileId": "legacy", "artifactVersion": "topic-analytics-v2",
        "trends": {"labels": ["not-a-d"], "totalPapers": [1]},
    }
    path = tmp_path / "legacy.json"
    write_json(path, legacy)
    before = path.read_bytes()
    assert topic_analytics.load_topic_analytics("legacy") == legacy
    assert path.read_bytes() == before

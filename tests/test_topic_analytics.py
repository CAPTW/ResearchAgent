from __future__ import annotations

from app.db import bootstrap
from app.repositories.files import write_jsonl
from app.repositories import runs as run_repository
from app.services import topic_analytics, topic_profiles


def _patch_topic_paths(isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)


def test_refresh_topic_analytics_persists_snapshot(isolated_run_repo, monkeypatch) -> None:
    _patch_topic_paths(isolated_run_repo, monkeypatch)

    profile = topic_profiles.create_topic_profile(
        {
            "title": "M3 Validation Run",
            "description": "analytics test",
            "topic": "M3 Validation Run",
            "queries": ["xr ml"],
            "categories": ["cs.CV"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        }
    )

    # These synthetic runs have an explicit binding to another fixture profile.
    # Bind this test's inputs to its newly created profile; names are not identity.
    for run_id in (isolated_run_repo["run_id"], isolated_run_repo["compare_run_id"]):
        run = run_repository.load_run_record(run_id)
        run["topic_profile_id"] = profile["profile_id"]
        run_repository.save_run_record(run)

    snapshot = topic_analytics.refresh_topic_analytics(profile["profile_id"])
    stored = topic_analytics.load_topic_analytics(profile["profile_id"])

    assert snapshot["profileId"] == profile["profile_id"]
    assert snapshot["artifactVersion"] == "topic-analytics-v4"
    assert snapshot["computedAt"] == snapshot["generatedAt"]
    assert snapshot["freshness"]["sourceRunCount"] == len(snapshot["sourceRuns"])
    assert snapshot["freshness"]["sourcePaperCount"] == snapshot["metrics"]["collectedPapers"]["value"]
    assert any(item["surfaceId"] == "metric_cards" and item["mode"] == "persisted_artifact" for item in snapshot["derivations"])
    assert any(item["surfaceId"] == "topic_papers" and item["mode"] == "runtime_adapter" for item in snapshot["derivations"])
    assert snapshot["metrics"]["collectedPapers"]["value"] > 0
    assert snapshot["trendDetail"]["summary"]
    assert snapshot["trendDetail"]["points"][0]["sourceRunIds"]
    assert snapshot["trendDetail"]["points"][0]["bucketMode"]
    assert snapshot["metricDefinitions"]
    assert snapshot["keywordDetails"]
    assert snapshot["keywordDetails"][0]["sourceRunIds"]
    assert snapshot["keywordDetails"][0]["samplePaperIds"]
    assert snapshot["hotKeywords"][0]["sourceRunIds"]
    assert snapshot["hotKeywords"][0]["paperCount"] == snapshot["keywordDetails"][0]["paperCount"]
    assert snapshot["fieldDetails"]
    assert snapshot["fieldDetails"][0]["sourceRunIds"]
    assert snapshot["fieldDetails"][0]["samplePaperIds"]
    assert snapshot["corePaperDetails"]
    assert snapshot["latestCorePapers"][0]["runId"]
    assert snapshot["latestCorePapers"][0]["rationale"]
    assert stored is not None
    assert stored["generatedAt"] == snapshot["generatedAt"]
    assert (topic_analytics.ANALYTICS_LIBRARY_DIR / f"{profile['profile_id']}.json").exists()


def test_refresh_topic_analytics_falls_back_to_run_created_month_when_published_missing(
    isolated_run_repo,
    monkeypatch,
) -> None:
    _patch_topic_paths(isolated_run_repo, monkeypatch)

    profile = topic_profiles.create_topic_profile(
        {
            "title": "Fallback Topic",
            "description": "fallback analytics",
            "topic": "Fallback Topic",
            "queries": ["fallback topic"],
            "categories": ["cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        }
    )

    run = run_repository.create_run_record(
        topic="Fallback Topic",
        request_payload={
            "topic": "Fallback Topic",
            "queries": ["fallback topic"],
            "categories": ["cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        },
        source="test:fallback-analytics",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    triage_path = paths.triage_dir / "triage.jsonl"
    write_jsonl(
        triage_path,
        [
            {
                "paper_id": "fallback-paper-1",
                "title": "Fallback Analytics Paper",
                "score": 55,
                "theme": "Reasoning",
                "triage": {"hits_primary": ["reasoning"], "hits_secondary": []},
            }
        ],
    )
    run["artifacts"]["triage_jsonl"] = str(triage_path)
    run["status"] = "completed"
    run["steps"] = [{"name": "summary", "status": "completed"}]
    run["finished_at"] = "2026-04-01T10:05:00+00:00"
    run["updated_at"] = "2026-04-01T10:05:00+00:00"
    run_repository.save_run_record(run)

    snapshot = topic_analytics.refresh_topic_analytics(profile["profile_id"])

    assert snapshot["trendDetail"]["bucketMode"] == "run_created_month_fallback"
    assert snapshot["trendDetail"]["points"][0]["bucketMode"] == "run_created_month_fallback"
    assert snapshot["trendDetail"]["points"][0]["sourceRunIds"] == [run["run_id"]]
    assert snapshot["freshness"]["status"] == "fresh"
    assert snapshot["coverage"][0]["covered"] == 0
    assert snapshot["fieldDistribution"][0]["label"] == "Reasoning"
    assert snapshot["fieldDistribution"][0]["count"] == 1
    assert snapshot["coverage"][-1]["emptyReason"] == "논문 메타데이터에 기관/소속 정보가 없어 기관 분포를 계산할 수 없습니다."


def test_refresh_topic_analytics_canonicalizes_keywords_and_institutions(
    isolated_run_repo,
    monkeypatch,
) -> None:
    _patch_topic_paths(isolated_run_repo, monkeypatch)

    profile = topic_profiles.create_topic_profile(
        {
            "title": "Canonical Topic",
            "description": "canonical analytics",
            "topic": "Canonical Topic",
            "queries": ["canonical topic"],
            "categories": ["cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        }
    )

    run = run_repository.create_run_record(
        topic="Canonical Topic",
        request_payload={
            "topic": "Canonical Topic",
            "queries": ["canonical topic"],
            "categories": ["cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        },
        source="test:canonical-analytics",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    triage_path = paths.triage_dir / "triage.jsonl"
    write_jsonl(
        triage_path,
        [
            {
                "paper_id": "canonical-paper-1",
                "title": "Large-Language Model at KAIST",
                "published": "2026-03-15",
                "score": 72,
                "theme": "Reasoning",
                "triage": {"hits_primary": ["Large-Language Model"], "hits_secondary": ["RAG"]},
                "metadata": {"institutions": ["KAIST"]},
            },
            {
                "paper_id": "canonical-paper-2",
                "title": "Large Language Models in Retrieval",
                "published": "2026-03-20",
                "score": 49,
                "theme": "Retrieval",
                "triage": {"hits_primary": ["large language models"], "hits_secondary": ["retrieval-augmented generation"]},
                "metadata": {"institutions": ["Korea Advanced Institute of Science and Technology"]},
            },
        ],
    )
    run["artifacts"]["triage_jsonl"] = str(triage_path)
    run["status"] = "completed"
    run["steps"] = [{"name": "summary", "status": "completed"}]
    run["finished_at"] = "2026-04-02T10:05:00+00:00"
    run["updated_at"] = "2026-04-02T10:05:00+00:00"
    run_repository.save_run_record(run)

    snapshot = topic_analytics.refresh_topic_analytics(profile["profile_id"])

    llm_keyword = next(item for item in snapshot["keywordDetails"] if item["keyword"] == "large language model")
    rag_keyword = next(item for item in snapshot["keywordDetails"] if item["keyword"] == "retrieval augmented generation")
    institution = snapshot["institutionDetails"][0]

    assert llm_keyword["paperCount"] == 2
    assert llm_keyword["sourceRunIds"] == [run["run_id"]]
    assert llm_keyword["samplePaperIds"] == ["canonical-paper-1", "canonical-paper-2"]
    assert set(llm_keyword["rawVariants"]) == {"Large-Language Model", "large language models"}
    assert rag_keyword["paperCount"] == 2
    assert institution["count"] == 2
    assert institution["canonicalName"] == "korea advanced institute of science and technology"
    assert institution["sourceRunIds"] == [run["run_id"]]
    assert institution["samplePaperIds"] == ["canonical-paper-1", "canonical-paper-2"]
    assert set(institution["rawVariants"]) == {"KAIST", "Korea Advanced Institute of Science and Technology"}
    assert snapshot["institutionDistribution"][0]["canonicalName"] == "korea advanced institute of science and technology"
    assert snapshot["fieldDistribution"][0]["sourceRunIds"] == [run["run_id"]]
    assert snapshot["metrics"]["corePapers"]["value"] == 1
    assert [paper["id"] for paper in snapshot["latestCorePapers"]] == ["canonical-paper-1"]
    assert snapshot["latestCorePapers"][0]["runId"] == run["run_id"]

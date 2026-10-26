from __future__ import annotations

from pathlib import Path

from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews, runs as run_repository
from app.repositories.files import write_jsonl
from app.schemas.dashboard import TopicProfileCreateRequest
from app.services import topic_analytics, topic_dashboard, topic_profiles, topic_schedules


def _remove_topic_analytics_artifact(analytics_dir: Path, profile_id: str = "large-language-models") -> None:
    (analytics_dir / f"{profile_id}.json").unlink(missing_ok=True)


def _coverage_profile():
    # The common fixture already contains explicitly bound LLM runs/reviews.
    # Keep this test's measured population separate, despite equal topic text.
    template = topic_profiles.get_topic_profile("large-language-models")
    payload = {key: template[key] for key in TopicProfileCreateRequest.model_fields if key in template}
    return topic_profiles.create_topic_profile({**payload, "title": "Coverage fixture"})


def test_api_topic_dashboard_exposes_review_coverage(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    client.get("/topic-profiles")
    _remove_topic_analytics_artifact(analytics_dir)
    profile = _coverage_profile()

    run = run_repository.create_run_record(
        topic="Large Language Models",
        request_payload={
            "topic": "Large Language Models",
            "queries": ["large language model"],
            "categories": ["cs.CL"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        },
        source="test:review-coverage",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "coverage-paper-1",
                "title": "Large Language Model Review Coverage",
                "abstract": "A large language model review coverage test.",
                "score": 60,
                "published": "2026-05-01",
                "triage": {"hits_primary": ["large language models"], "hits_secondary": []},
            },
            {
                "paper_id": "coverage-paper-2",
                "title": "Unreviewed Large Language Model Paper",
                "abstract": "A second large language model paper without a review.",
                "score": 55,
                "published": "2026-05-02",
                "triage": {"hits_primary": ["large language models"], "hits_secondary": []},
            },
        ],
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run["steps"] = [{"name": "summary", "status": "completed"}]
    run_repository.save_run_record(run)
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "coverage-paper-1",
            "title": "Large Language Model Review Coverage",
            "source_provider": "arxiv",
            "abstract_text": "A large language model review coverage test.",
            "full_text_status": "abstract_only",
            "relevance_score": 80,
            "novelty_score": 50,
            "evidence_strength": 40,
            "method_summary": "초록 기반 리뷰",
            "key_findings": ["리뷰 커버리지 산출"],
            "limitations": ["본문 없음"],
            "why_it_matters": "대시보드 커버리지 표시",
            "topic_fit_rationale": "LLM 주제와 일치",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-07T00:00:00+00:00",
            "source_text_sha256": "coverage-hash",
        }
    )

    response = client.get(f"/api/topics/{profile['profile_id']}/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["reviewCoverage"] == {
        "totalPapers": 2,
        "reviewedCount": 1,
        "abstractOnlyCount": 1,
        "fullTextReviewedCount": 0,
        "coveragePercentage": 50.0,
        "rawIndexedPaperCount": 2,
        "uniqueCandidateCount": 2,
        "topicRelevantUniqueCount": 2,
        "reviewedUniqueCount": 1,
        "abstractOnlyUniqueCount": 1,
        "fullTextAvailableUniqueCount": 0,
        "fullTextReviewedUniqueCount": 0,
    }


def test_dashboard_review_coverage_denominator_ignores_duplicate_rows(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    client.get("/topic-profiles")
    _remove_topic_analytics_artifact(analytics_dir)
    profile = _coverage_profile()

    runs = []
    for index in range(2):
        run = run_repository.create_run_record(
            topic=profile["topic"],
            request_payload={
                "topic": profile["topic"],
                "queries": profile["queries"],
                "categories": profile["categories"],
                "max_results": 10,
                "top_papers": 3,
                "weekly_top": 10,
            },
            source="test:review-coverage-duplicates",
            write_legacy=False,
        )
        paths = run_repository.build_run_paths(run["run_id"])
        write_jsonl(
            paths.triage_dir / "triage.jsonl",
            [
                {
                    "paper_id": "coverage-duplicate-1",
                    "title": "Large Language Model Agents with Retrieval-Augmented Generation",
                    "abstract": "Large language model agents use RAG and tool use.",
                    "score": 60,
                    "published": f"2026-05-0{index + 1}",
                }
            ],
        )
        run["status"] = "completed"
        run["topic_profile_id"] = profile["profile_id"]
        run["steps"] = [{"name": "summary", "status": "completed"}]
        runs.append(run_repository.save_run_record(run))
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": runs[0]["run_id"],
            "paper_id": "coverage-duplicate-1",
            "title": "Large Language Model Agents with Retrieval-Augmented Generation",
            "source_provider": "arxiv",
            "abstract_text": "Large language model agents use RAG and tool use.",
            "full_text_status": "abstract_only",
            "relevance_score": 90,
            "novelty_score": 50,
            "evidence_strength": 40,
            "method_summary": "초록 기반 리뷰",
            "key_findings": ["중복 row를 하나의 고유 논문으로 집계"],
            "limitations": ["본문 없음"],
            "why_it_matters": "대시보드 커버리지 분모 검증",
            "topic_fit_rationale": "LLM 주제와 일치",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-07T00:00:00+00:00",
            "source_text_sha256": "coverage-duplicate-hash",
        }
    )

    response = client.get(f"/api/topics/{profile['profile_id']}/dashboard")
    coverage = response.json()["reviewCoverage"]

    assert response.status_code == 200, response.text
    assert coverage["rawIndexedPaperCount"] == 2
    assert coverage["uniqueCandidateCount"] == 1
    assert coverage["topicRelevantUniqueCount"] == 1
    assert coverage["reviewedUniqueCount"] == 1
    assert coverage["coveragePercentage"] == 100.0


def test_api_topic_dashboard_exposes_read_only_research_operations_state(client, isolated_run_repo, monkeypatch, tmp_path) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(topic_dashboard, "RESEARCH_OPERATIONS_OUTPUT_ROOT", tmp_path / "missing-output", raising=False)
    client.get("/topic-profiles")
    _remove_topic_analytics_artifact(analytics_dir)
    profile = _coverage_profile()

    run = run_repository.create_run_record(
        topic=profile["topic"],
        request_payload={
            "topic": profile["topic"],
            "queries": profile["queries"],
            "categories": profile["categories"],
            "max_results": 20,
            "top_papers": 5,
            "weekly_top": 10,
        },
        source="test:research-operations",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": f"ops-paper-{index}",
                "title": f"Large Language Model RAG Agent Evaluation {index}",
                "abstract": "Large language model RAG agent evaluation and reasoning benchmark.",
                "score": 70 - index,
                "published": f"2026-05-{index + 1:02d}",
                "pdf_url": f"https://arxiv.org/pdf/2605.{index:05d}v1",
                "triage": {"hits_primary": ["large language models", "rag", "agent"], "hits_secondary": ["reasoning"]},
            }
            for index in range(20)
        ],
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run["steps"] = [{"name": "summary", "status": "completed"}]
    run_repository.save_run_record(run)

    for index, status in enumerate(("reviewed", "reviewed", "abstract_only")):
        paper_reviews.upsert_review(
            {
                "topic_id": profile["profile_id"],
                "run_id": run["run_id"],
                "paper_id": f"ops-paper-{index}",
                "title": f"Large Language Model RAG Agent Evaluation {index}",
                "source_provider": "arxiv",
                "abstract_text": "Large language model RAG agent evaluation and reasoning benchmark.",
                "text_excerpt": "RAG agents are evaluated with reasoning benchmarks and limitations.",
                "full_text_status": status,
                "relevance_score": 90 - index,
                "novelty_score": 60,
                "evidence_strength": 80 if status == "reviewed" else 40,
                "method_summary": "RAG/agent evaluation baseline",
                "key_findings": ["RAG evaluation", "agent reasoning"],
                "limitations": ["coverage remains limited"],
                "why_it_matters": "LLM 운영 대시보드의 연구 근거 품질을 설명합니다.",
                "topic_fit_rationale": "LLM, RAG, agent 키워드가 일치합니다.",
                "review_model": "deterministic-local-full-text-v1",
                "reviewer": "deterministic-baseline",
                "reviewed_at": "2026-05-08T00:00:00+00:00",
                "source_text_sha256": f"ops-sha-{index}" if status == "reviewed" else None,
                "full_text_artifact_path": f"data/papers/text/ops-paper-{index}.txt" if status == "reviewed" else None,
                "text_char_count": 12000 if status == "reviewed" else 0,
                "review_source_type": "full_text" if status == "reviewed" else "abstract",
                "full_text_reviewed_at": "2026-05-08T00:00:00+00:00" if status == "reviewed" else None,
                "evidence_spans": [
                    {
                        "label": "full_text_excerpt_1",
                        "text": "RAG agents are evaluated with reasoning benchmarks and limitations.",
                        "source_path": f"data/papers/text/ops-paper-{index}.txt",
                        "char_start": 0,
                        "char_end": 64,
                    }
                ]
                if status == "reviewed"
                else [],
            }
        )

    ai_review_drafts.upsert_draft(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "ops-paper-0",
            "source_text_sha256": "ops-sha-0",
            "review_level": "full_text_grounded",
            "status": "accepted",
            "draft": {
                "paper_id": "ops-paper-0",
                "source_text_sha256": "ops-sha-0",
                "review_level": "full_text_grounded",
                "contribution_summary": "RAG agent evaluation contribution.",
                "method_summary": "Evidence-span grounded method summary.",
                "evidence_summary": "Uses full_text_excerpt_1.",
                "limitations": ["coverage remains limited"],
                "relevance_to_topic": "LLM/RAG agent evaluation",
                "novelty_signal": "medium",
                "practical_implications": "prioritize follow-up review",
                "follow_up_questions": ["Which agents generalize?"],
                "recommended_tags": ["rag", "agent"],
                "cited_evidence_span_ids": ["full_text_excerpt_1"],
                "confidence": 0.82,
                "unsupported_claims": [],
            },
            "validation": {
                "status": "accepted",
                "provider": {"name": "stub", "mode": "stub", "origin": "stub", "generated": True, "provider_run_id": "ops-provider-run"},
            },
        }
    )

    response = client.get(f"/api/topics/{profile['profile_id']}/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    operations = body["researchOperations"]
    coverage = body["reviewCoverage"]
    assert operations["coverage"]["fullTextReviewedUniqueCount"] == coverage["fullTextReviewedUniqueCount"] == 2
    assert operations["coverage"]["topicRelevantUniqueCount"] == coverage["topicRelevantUniqueCount"] == 20
    assert operations["coverage"]["fullTextCoverageRatio"] == 0.1
    assert operations["coverage"]["aiGroundedAcceptedCount"] == 1
    assert operations["coverage"]["aiGroundedCoverageRatio"] == 0.5
    assert operations["coverage"]["providerGeneratedAcceptedCount"] == 1
    assert operations["recommendations"]["recommendationGapCandidateCount"] >= 1
    assert operations["recommendations"]["recommendationTopicCandidateCount"] >= 1
    assert operations["recommendations"]["recommendationCoverageWarningCount"] >= 1
    assert operations["recommendations"]["preliminary"] is True
    assert operations["latestRecommendationAuditSummary"]["recommendation_count"] >= 1
    assert operations["latestFollowupExecutionSummary"] is None
    assert operations["guidance"]["followupExecutionDryRunCommand"].startswith("python -m app.cli.review_refresh")
    assert "--dry-run" in operations["guidance"]["followupExecutionDryRunCommand"]
    assert "--fetch-missing-pdf" in operations["guidance"]["followupExecutionDryRunCommand"]
    assert operations["provider"]["acceptedCount"] == 1
    assert operations["provider"]["needsReviewCount"] == 0
    assert operations["provider"]["rejectedCount"] == 0


def test_api_topic_dashboard_exposes_topic_config_and_preview_only_run_plans(client, isolated_run_repo, monkeypatch, tmp_path) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(topic_dashboard, "RESEARCH_OPERATIONS_OUTPUT_ROOT", tmp_path / "missing-output", raising=False)
    client.get("/topic-profiles")

    response = client.get("/api/topics/large-language-models/dashboard")
    body = response.json()

    assert response.status_code == 200, response.text
    planner = body["researchOperations"]["operationsPlanner"]
    config = planner["topicConfiguration"]
    assert config["topicId"] == "large-language-models"
    assert config["displayName"] == "Large Language Models"
    assert "large language model" in " ".join(config["queryTerms"]).lower()
    assert config["defaultAllowDomains"] == ["arxiv.org"]
    assert config["defaultDownloadLimit"] == 5
    assert config["defaultMaxPdfBytes"] == 25_000_000
    assert config["providerModeDefault"] == "none"
    assert config["liveActionsEnabled"] is False
    assert config["validationWarnings"] == []

    plans = planner["runPlans"]
    plan_by_id = {plan["planId"]: plan for plan in plans}
    assert list(plan_by_id) == [
        "refresh_reviews_dry_run",
        "full_text_acquisition_dry_run",
        "recommendation_generation",
        "recommendation_audit",
        "recommendation_followup_plan",
        "recommendation_followup_execution_dry_run",
        "provider_stub_dry_run",
        "provider_command_dry_run",
        "provider_acceptance_smoke_dry_run",
        "provider_manual_run_prepare_dry_run",
        "provider_manual_output_import",
    ]
    assert all(plan["previewOnly"] is True for plan in plans)
    assert all(plan["requiresExplicitOperatorAction"] is True for plan in plans)
    assert all(plan["serverExecutesCommand"] is False for plan in plans)
    acquisition = plan_by_id["full_text_acquisition_dry_run"]
    assert acquisition["riskLevel"] == "dry_run"
    assert acquisition["writesFiles"] is False
    assert acquisition["callsNetwork"] is False
    assert acquisition["callsProvider"] is False
    assert "--dry-run" in acquisition["copyableCliCommand"]
    assert "--fetch-missing-pdf" in acquisition["copyableCliCommand"]
    assert "--download-limit 5" in acquisition["copyableCliCommand"]
    assert "--allow-domain arxiv.org" in acquisition["copyableCliCommand"]
    assert "--extract-after-fetch" in acquisition["copyableCliCommand"]
    assert "--allow-live-ai-provider" not in plan_by_id["provider_command_dry_run"]["copyableCliCommand"]
    manual_prepare = plan_by_id["provider_manual_run_prepare_dry_run"]
    manual_import = plan_by_id["provider_manual_output_import"]
    assert "--prepare-provider-manual-run" in manual_prepare["copyableCliCommand"]
    assert "--provider-manual-run-dir" in manual_prepare["copyableCliCommand"]
    assert "--dry-run" in manual_prepare["copyableCliCommand"]
    assert "--allow-live-ai-provider" not in manual_prepare["copyableCliCommand"]
    assert "--import-provider-drafts" in manual_import["copyableCliCommand"]
    assert "--validate-ai-review-drafts" in manual_import["copyableCliCommand"]
    assert manual_prepare["serverExecutesCommand"] is False
    assert manual_import["serverExecutesCommand"] is False


def test_topic_operations_planner_reports_invalid_config_warnings_without_crashing() -> None:
    planner = topic_dashboard.build_topic_operations_planner(
        {
            "profile_id": "bad topic!",
            "title": "Bad Topic",
            "topic": "Bad Topic",
            "queries": [],
            "categories": [],
            "max_results": 0,
            "source_settings": {
                "defaultProvider": "arxiv",
                "enabledProviders": ["arxiv"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": True,
                        "priority": 1,
                        "queries": [],
                        "categories": [],
                        "maxResults": 0,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    }
                },
            },
        }
    )

    config = planner["topicConfiguration"]
    assert config["liveActionsEnabled"] is False
    assert config["providerModeDefault"] == "none"
    assert any("topic_id" in warning for warning in config["validationWarnings"])
    assert any("query_terms" in warning for warning in config["validationWarnings"])
    assert any("default_unique_limit" in warning for warning in config["validationWarnings"])
    assert planner["warnings"] == config["validationWarnings"]

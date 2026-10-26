from __future__ import annotations

from app.db import bootstrap
from app.repositories import runs as run_repository
from app.repositories.files import write_jsonl
from app.services import paper_review, topic_analytics, topic_profiles


def _patch_topic_paths(isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)


def _llm_profile() -> dict:
    return {
        "profile_id": "large-language-models",
        "title": "Large Language Models",
        "topic": "Large Language Models",
        "queries": [
            '"large language model" OR LLM OR "foundation model"',
            '"retrieval augmented generation" OR RAG OR "tool use"',
            '"alignment" OR "reasoning" OR "agent"',
        ],
        "categories": ["cs.CL", "cs.AI", "cs.LG"],
    }


def test_fragment_keyword_filtering_keeps_only_approved_short_acronyms() -> None:
    assert paper_review.is_quality_keyword("AI") is True
    assert paper_review.is_quality_keyword("ML") is True
    assert paper_review.is_quality_keyword("RL") is True
    assert paper_review.is_quality_keyword("LLM") is True
    assert paper_review.is_quality_keyword("RAG") is True
    assert paper_review.is_quality_keyword("NLP") is True
    assert paper_review.is_quality_keyword("ar") is False
    assert paper_review.is_quality_keyword("vr") is False
    assert paper_review.is_quality_keyword("q") is False
    assert paper_review.is_quality_keyword("large language model") is True


def test_topic_relevance_scores_profile_terms_over_legacy_xr_terms() -> None:
    profile = _llm_profile()
    llm_item = {
        "title": "Large Language Model Agents with Retrieval-Augmented Generation",
        "abstract": "A foundation model uses RAG and tool use for evidence-backed reasoning.",
    }
    xr_item = {
        "title": "Gaze Prediction in Virtual Reality Without Eye Tracking",
        "abstract": "This paper studies pose tracking, VR, and foveated rendering for head-mounted displays.",
    }

    assert paper_review.score_topic_relevance(profile, llm_item) >= 60
    assert paper_review.score_topic_relevance(profile, xr_item) <= 25


def test_topic_analytics_filters_fragment_keywords_and_ranks_by_topic_relevance(
    isolated_run_repo,
    monkeypatch,
) -> None:
    _patch_topic_paths(isolated_run_repo, monkeypatch)
    profile = topic_profiles.create_topic_profile(
        {
            "title": "Large Language Models",
            "description": "LLM topic relevance regression",
            "topic": "Large Language Models",
            "queries": [
                '"large language model" OR LLM OR "foundation model"',
                '"retrieval augmented generation" OR RAG OR "tool use"',
            ],
            "categories": ["cs.CL", "cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        }
    )
    run = run_repository.create_run_record(
        topic="Large Language Models",
        request_payload={
            "topic": "Large Language Models",
            "queries": profile["queries"],
            "categories": profile["categories"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        },
        source="test:keyword-quality",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "xr-fragment-1",
                "title": "Gaze Prediction in Virtual Reality Without Eye Tracking",
                "abstract": "Virtual reality, pose tracking, AR, VR, VIO, and HMD signals.",
                "score": 92,
                "published": "2026-05-02",
                "triage": {"hits_primary": ["ar", "vr", "vio", "pose"], "hits_secondary": ["imu"]},
            },
            {
                "paper_id": "llm-topic-1",
                "title": "Retrieval-Augmented Large Language Model Agents for Scientific Review",
                "abstract": "Large language model agents use RAG and tool use for evidence-backed Korean reports.",
                "score": 54,
                "published": "2026-05-03",
                "triage": {
                    "hits_primary": ["large language models", "retrieval-augmented generation"],
                    "hits_secondary": ["tool use"],
                },
            },
        ],
    )
    run["status"] = "completed"
    run["steps"] = [{"name": "summary", "status": "completed"}]
    run_repository.save_run_record(run)

    snapshot = topic_analytics.refresh_topic_analytics(profile["profile_id"])
    keywords = [item["keyword"] for item in snapshot["hotKeywords"]]

    assert snapshot["latestCorePapers"][0]["id"] == "llm-topic-1"
    assert "large language model" in keywords
    assert "retrieval augmented generation" in keywords
    assert "ar" not in keywords
    assert "vr" not in keywords
    assert "vio" not in keywords
    assert all(len(keyword) > 2 or keyword.upper() in {"AI", "ML", "RL", "LLM", "RAG", "NLP"} for keyword in keywords)


from __future__ import annotations

from app.services import ranking


LEGACY_XR_KEYWORDS = {
    "primary": ["augmented reality", "virtual reality", "xr", "ar", "vr", "pose", "tracking", "LLM", "RAG", "AI"],
    "secondary": ["edge", "latency", "transformer"],
    "negative": [],
}


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


def test_topic_aware_ranking_prefers_llm_rag_over_legacy_xr_hits() -> None:
    profile = _llm_profile()
    llm_item = {
        "paper_id": "llm-rank-1",
        "title": "Large Language Model Agents with Retrieval-Augmented Generation",
        "abstract": "An LLM agent uses RAG, AI planning, and tool use for evidence-backed reasoning.",
    }
    xr_item = {
        "paper_id": "xr-rank-1",
        "title": "Gaze Prediction in Virtual Reality Without Eye Tracking",
        "abstract": "Augmented reality, pose tracking, AR, VR, edge latency, and head-mounted displays.",
    }

    llm_scored = ranking.score_item(llm_item, LEGACY_XR_KEYWORDS, topic_profile=profile)
    xr_scored = ranking.score_item(xr_item, LEGACY_XR_KEYWORDS, topic_profile=profile)

    assert llm_scored["score"] > xr_scored["score"]
    assert llm_scored["topic_relevance_score"] >= 60
    assert xr_scored["topic_relevance_score"] <= 25


def test_topic_aware_ranking_retains_approved_acronyms_and_filters_fragments() -> None:
    scored = ranking.score_item(
        {
            "paper_id": "llm-rank-2",
            "title": "LLM and RAG Agents for AI Research Review",
            "abstract": "Large language model agents use retrieval augmented generation.",
        },
        LEGACY_XR_KEYWORDS,
        topic_profile=_llm_profile(),
    )
    primary_hits = set(scored["triage"]["hits_primary"])

    assert {"ai", "llm", "rag"}.issubset(primary_hits)
    assert "ar" not in primary_hits
    assert "vr" not in primary_hits

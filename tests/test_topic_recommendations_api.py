from __future__ import annotations

from app.services import topic_dashboard


def test_build_api_topic_recommendations_projects_actionable_new_topics_and_gaps(monkeypatch) -> None:
    payload = {
        "topic_id": "llm",
        "summary": {"coverage_warning": "coverage limited"},
        "new_topic_candidates": [
            {
                "topic_candidate_id": "topic-eval",
                "title": "Evaluation for LLM",
                "proposed_research_question": "How to evaluate?",
                "why_now": "signals repeat",
                "suggested_method_direction": "start with reviews",
                "suggested_queries": ["llm evaluation", "benchmark"],
                "confidence": "medium",
                "supporting_paper_ids": ["p1", "p2"],
            }
        ],
        "gap_candidates": [
            {
                "gap_id": "gap-eval",
                "title": "Evaluation evidence gap",
                "gap_statement": "too small",
                "novelty_rationale": "compare across papers",
                "confidence": "low",
                "supporting_paper_ids": ["p1"],
            }
        ],
    }
    monkeypatch.setattr(
        topic_dashboard.recommendations,
        "build_recommendations_for_topic",
        lambda topic_id, **kwargs: payload,
    )

    result = topic_dashboard.build_api_topic_recommendations("llm")

    assert result["topicId"] == "llm"
    assert result["newTopicCount"] == 1
    assert result["gapCount"] == 1
    assert result["coverageWarning"] == "coverage limited"

    new_topic = next(item for item in result["items"] if item["type"] == "new_topic")
    assert new_topic["id"] == "topic-eval"
    assert new_topic["actionable"] is True
    assert new_topic["suggestedQueries"] == ["llm evaluation", "benchmark"]
    assert new_topic["supportingPaperCount"] == 2

    gap = next(item for item in result["items"] if item["type"] == "gap")
    assert gap["actionable"] is False
    assert gap["suggestedQueries"] == []


def test_build_api_topic_recommendations_falls_back_to_question_for_queries(monkeypatch) -> None:
    payload = {
        "topic_id": "xr",
        "new_topic_candidates": [
            {
                "topic_candidate_id": "topic-1",
                "title": "T",
                "proposed_research_question": "RQ?",
                "suggested_queries": [],
            }
        ],
        "gap_candidates": [],
    }
    monkeypatch.setattr(
        topic_dashboard.recommendations,
        "build_recommendations_for_topic",
        lambda topic_id, **kwargs: payload,
    )

    result = topic_dashboard.build_api_topic_recommendations("xr")

    assert result["items"][0]["suggestedQueries"] == ["RQ?"]
    assert result["coverageWarning"] is None


def test_recommendation_projection_preserves_evidence_and_top_level_warning(monkeypatch):
    from app.schemas.dashboard import ApiTopicRecommendationsResponse

    ref = dict(paper_id="paper", source_text_sha256="a" * 64, evidence_span_id="span",
               review_level="abstract_only", quoted_or_summarized_basis="Abstract limitation")
    payload = dict(topic_id="topic", summary={}, coverage_warning="1 of 10 full texts reviewed",
                   gap_candidates=[dict(gap_id="gap", title="Candidate", confidence="low",
                                        supporting_paper_ids=["paper"], supporting_evidence_refs=[ref],
                                        coverage_warning="Abstract only", risk_notes=["No causal evidence"])])
    monkeypatch.setattr(topic_dashboard.recommendations, "build_recommendations_for_topic", lambda *a, **kw: payload)
    result = ApiTopicRecommendationsResponse(**topic_dashboard.build_api_topic_recommendations("topic"))
    assert result.coverageWarning == payload["coverage_warning"]
    item = result.items[0]
    assert item.confidence == "low" and item.coverageWarning == "Abstract only"
    assert item.supportingPaperIds == ["paper"]
    assert item.evidenceRefs[0].quoted_or_summarized_basis == "Abstract limitation"
    assert item.riskNotes == ["No causal evidence"]

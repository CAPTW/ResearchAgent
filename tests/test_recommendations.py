from __future__ import annotations

import hashlib
import json
import sys

import pytest
from pydantic import ValidationError

from app.cli import review_refresh
from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews, runs as run_repository
from app.repositories.files import write_jsonl
from app.schemas.recommendations import NewTopicCandidate, RecommendationEvidenceRef, ResearchGapCandidate
from app.services import pdf_acquisition, recommendations, topic_analytics, topic_profiles
from tests.research_transport_fakes import install_pdf_urlopen_mock


def _review(index: int, *, full_text_status: str = "reviewed", **overrides) -> dict:
    payload = {
        "topic_id": "large-language-models",
        "run_id": "recommendation-run",
        "paper_id": f"llm-recommendation-{index}",
        "title": f"Grounded RAG Evaluation Paper {index}",
        "source_provider": "arxiv",
        "authors": ["Kim", "Lee"],
        "published_at": f"2026-05-{index:02d}",
        "abstract_text": (
            "This paper studies retrieval augmented generation, agent tool use, "
            "evaluation drift, benchmark reliability, and follow-up research gaps."
        ),
        "text_excerpt": "Full text evidence discusses RAG evaluation drift and benchmark limitations. " * 8,
        "full_text_status": full_text_status,
        "relevance_score": 94 - index,
        "novelty_score": 65,
        "evidence_strength": 82,
        "method_summary": "Compares RAG methods with benchmark and evaluation protocols.",
        "key_findings": ["RAG evaluation remains sensitive to benchmark design."],
        "limitations": ["Evaluation drift and dataset coverage remain unresolved."],
        "why_it_matters": "It supports cautious research-gap recommendations.",
        "topic_fit_rationale": "Matches LLM, RAG, and evaluation monitoring.",
        "review_model": "deterministic-local-full-text-v1",
        "reviewer": "deterministic-local-full-text",
        "reviewed_at": "2026-05-08T00:00:00+00:00",
        "source_text_sha256": f"recommendation-sha-{index}",
        "full_text_artifact_path": f"storage/runs/recommendation-run/raw/llm-recommendation-{index}.txt",
        "text_char_count": 640,
        "evidence_spans": [
            {
                "label": f"recommendation-span-{index}",
                "text": f"Evidence span {index} links RAG evaluation drift to benchmark limitations.",
                "source_path": f"storage/runs/recommendation-run/raw/llm-recommendation-{index}.txt",
                "page_number": 1,
                "char_start": 0,
                "char_end": 80,
            }
        ],
        "review_source_type": "full_text",
        "full_text_reviewed_at": "2026-05-08T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _accepted_draft(paper_id: str, source_hash: str, *, span_id: str = "recommendation-span-1") -> dict:
    return {
        "topic_id": "large-language-models",
        "run_id": "recommendation-run",
        "paper_id": paper_id,
        "source_text_sha256": source_hash,
        "review_level": "full_text_grounded",
        "status": "accepted",
        "draft": {
            "paper_id": paper_id,
            "source_text_sha256": source_hash,
            "contribution_summary": "Accepted AI draft identifies a reusable RAG evaluation gap.",
            "method_summary": "Grounded method summary.",
            "evidence_summary": "Grounded evidence cites the recommendation span.",
            "limitations": ["Accepted AI limitation: benchmark coverage remains narrow."],
            "relevance_to_topic": "Relevant to LLM/RAG evaluation.",
            "novelty_signal": "moderate",
            "practical_implications": "Track benchmark drift before proposing stronger claims.",
            "follow_up_questions": ["How stable are RAG benchmarks across domains?"],
            "recommended_tags": ["RAG", "evaluation"],
            "cited_evidence_span_ids": [span_id],
            "confidence": 0.81,
            "unsupported_claims": [],
            "review_level": "full_text_grounded",
        },
        "validation": {
            "status": "accepted",
            "reason_codes": [],
            "provider": {"name": "stub", "mode": "stub", "origin": "stub", "generated": True},
        },
    }


def _long_extracted_text(label: str = "recommendation follow-up") -> str:
    return (
        f"Full text evidence about {label}, retrieval augmented generation, benchmark drift, and LLM evaluation. "
        "The local extracted text is long enough to pass deterministic evidence quality checks. "
        "It includes method details, limitations, and source traceability for recommendation follow-up planning."
    )


def _patch_topic_storage(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    profile_dir = storage_root.parent / "topic_profiles"
    analytics_dir = storage_root.parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)


class _FakePdfResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = {"Content-Type": "application/pdf", "Content-Length": str(len(body))}

    def __enter__(self) -> "_FakePdfResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _metadata_sidecars(path) -> dict[str, bool]:
    return {
        suffix: path.with_name(f"{path.name}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    }


def _h006_known_papers() -> list[dict]:
    return [
        {
            "paper_id": "h006-paper-1",
            "full_text_status": "reviewed",
            "source_text_sha256": "h006-sha-1",
            "evidence_spans": [{"label": "h006-span-1", "text": "RAG benchmark drift evidence."}],
        },
        {
            "paper_id": "h006-paper-2",
            "full_text_status": "reviewed",
            "source_text_sha256": "h006-sha-2",
            "evidence_spans": [{"label": "h006-span-2", "text": "Evaluation coverage limitation evidence."}],
        },
    ]


def _h006_ref(index: int, *, review_level: str = "deterministic_full_text") -> dict:
    return {
        "paper_id": f"h006-paper-{index}",
        "source_text_sha256": f"h006-sha-{index}" if review_level != "abstract_only" else None,
        "evidence_span_id": f"h006-span-{index}" if review_level != "abstract_only" else None,
        "review_level": review_level,
        "quoted_or_summarized_basis": f"Evidence basis {index} supports a cautious RAG evaluation follow-up.",
    }


def _h006_good_payload() -> dict:
    refs = [_h006_ref(1), _h006_ref(2)]
    return {
        "topic_id": "large-language-models",
        "summary": {
            "full_text_reviewed_count": 2,
            "topic_relevant_unique_count": 2,
            "ai_grounded_accepted_count": 0,
        },
        "gap_candidates": [
            {
                "gap_id": "gap-h006-rag-evaluation",
                "title": "RAG evaluation follow-up evidence gap",
                "gap_statement": "Stored reviews show a preliminary candidate gap around RAG benchmark drift.",
                "supporting_paper_ids": ["h006-paper-1", "h006-paper-2"],
                "supporting_evidence_refs": refs,
                "source_basis": "full_text_evidence",
                "confidence": "medium",
                "confidence_score": 0.67,
                "coverage_warning": "preliminary: AI-grounded accepted draft coverage is 0 / 2 full-text reviewed papers.",
                "novelty_rationale": "Follow-up work should compare whether this hypothesis holds across more reviewed papers.",
                "risk_notes": ["This is a candidate direction, not an exhaustive literature finding."],
            }
        ],
        "new_topic_candidates": [
            {
                "topic_candidate_id": "topic-h006-rag-evaluation",
                "title": "RAG evaluation drift monitoring",
                "problem_statement": "A preliminary candidate topic is to monitor RAG evaluation drift across reviewed papers.",
                "why_now": "The current evidence base contains repeated signals but still requires manual validation.",
                "proposed_research_question": "How should RAG benchmark drift be evaluated across stronger evidence?",
                "suggested_method_direction": "Start with evidence-linked review expansion and compare limitation patterns.",
                "supporting_paper_ids": ["h006-paper-1", "h006-paper-2"],
                "supporting_evidence_refs": refs,
                "expected_contribution": "A cautious future monitoring direction for local research planning.",
                "nearest_existing_work": ["h006-paper-1", "h006-paper-2"],
                "differentiation_claim": "Differentiation is a hypothesis requiring more full-text reviewed evidence.",
                "confidence": "medium",
                "confidence_score": 0.66,
                "coverage_warning": "preliminary: AI-grounded accepted draft coverage is 0 / 2 full-text reviewed papers.",
                "required_follow_up_evidence": ["Increase full-text coverage.", "Validate evidence spans manually."],
            }
        ],
    }


def test_recommendation_schemas_require_evidence_and_block_abstract_only_high_confidence() -> None:
    with pytest.raises(ValidationError):
        ResearchGapCandidate(
            gap_id="gap-empty",
            topic_id="large-language-models",
            title="Unsupported gap",
            gap_statement="No supporting papers.",
            supporting_paper_ids=[],
            supporting_evidence_refs=[],
            limitation_refs=[],
            source_basis="deterministic_review",
            confidence="low",
            confidence_score=0.1,
            coverage_warning="low coverage",
            novelty_rationale="none",
            risk_notes=["unsupported"],
        )

    abstract_ref = RecommendationEvidenceRef(
        paper_id="abstract-paper",
        review_level="abstract_only",
        quoted_or_summarized_basis="Abstract-only signal.",
    )
    with pytest.raises(ValidationError):
        NewTopicCandidate(
            topic_candidate_id="topic-high",
            title="Overconfident abstract-only topic",
            problem_statement="Abstract-only support must not be high confidence.",
            why_now="One abstract mentions it.",
            proposed_research_question="What is the gap?",
            suggested_method_direction="Inspect more papers.",
            supporting_paper_ids=["abstract-paper"],
            supporting_evidence_refs=[abstract_ref],
            expected_contribution="Unsupported high confidence.",
            nearest_existing_work=["abstract-paper"],
            differentiation_claim="Too strong.",
            confidence="high",
            confidence_score=0.9,
            coverage_warning="abstract only",
            required_follow_up_evidence=["full text review"],
        )


def test_recommendation_engine_links_accepted_ai_and_full_text_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    paper_reviews.upsert_review(_review(2, paper_id="llm-recommendation-2", source_text_sha256="recommendation-sha-2"))
    paper_reviews.upsert_review(
        _review(
            3,
            paper_id="llm-abstract-only",
            full_text_status="abstract_only",
            source_text_sha256="",
            evidence_spans=[],
        )
    )
    ai_review_drafts.upsert_draft(_accepted_draft("llm-recommendation-1", "recommendation-sha-1"))
    ai_review_drafts.upsert_draft(
        {
            **_accepted_draft("llm-recommendation-2", "recommendation-sha-2", span_id="recommendation-span-2"),
            "status": "rejected",
            "validation": {"status": "rejected", "reason_codes": ["missing_cited_evidence_span_ids"]},
        }
    )

    result = recommendations.build_recommendations_for_topic(
        "large-language-models",
        limit=3,
        topic_relevant_unique_count=524,
    )

    assert result["summary"]["candidate_gap_count"] >= 1
    assert result["summary"]["candidate_topic_count"] >= 1
    assert result["summary"]["accepted_ai_grounded_evidence_count"] == 0
    assert result["summary"]["accepted_ai_grounded_synthetic_evidence_count"] == 1
    assert result["summary"]["full_text_evidence_count"] >= 2
    assert result["summary"]["coverage_warning_count"] >= 1
    assert result["summary"]["high_confidence_count"] == 0
    first_gap = result["gap_candidates"][0]
    refs = first_gap["supporting_evidence_refs"]
    assert first_gap["supporting_paper_ids"]
    assert any(ref["review_level"] == "accepted_ai_grounded_synthetic" for ref in refs)
    assert any(ref.get("provider_origin") == "stub_synthetic" for ref in refs)
    assert any(ref.get("source_text_sha256") == "recommendation-sha-1" for ref in refs)
    assert any(ref.get("evidence_span_id") == "recommendation-span-1" for ref in refs)
    assert "preliminary" in first_gap["coverage_warning"].lower()
    assert "Rejected" not in json.dumps(result, ensure_ascii=False)


def test_synthetic_ai_evidence_is_labeled_and_does_not_silently_inflate_confidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    ai_review_drafts.upsert_draft(_accepted_draft("llm-recommendation-1", "recommendation-sha-1"))

    result = recommendations.build_recommendations_for_topic(
        "large-language-models",
        limit=1,
        topic_relevant_unique_count=1,
    )

    first_gap = result["gap_candidates"][0]
    synthetic_refs = [
        ref
        for ref in first_gap["supporting_evidence_refs"]
        if ref["review_level"] == "accepted_ai_grounded_synthetic"
    ]
    assert synthetic_refs
    assert synthetic_refs[0]["provider_origin"] == "stub_synthetic"
    assert result["summary"]["accepted_ai_grounded_evidence_count"] == 0
    assert result["summary"]["accepted_ai_grounded_synthetic_evidence_count"] >= 1
    assert first_gap["confidence"] != "high"
    assert first_gap["source_basis"] in {"accepted_ai_grounded_synthetic", "mixed"}


def test_high_confidence_recommendation_rejects_synthetic_only_evidence_refs() -> None:
    synthetic_refs = [
        RecommendationEvidenceRef(
            paper_id=f"synthetic-{index}",
            source_text_sha256=f"synthetic-sha-{index}",
            evidence_span_id=f"synthetic-span-{index}",
            review_level="accepted_ai_grounded_synthetic",
            provider_origin="stub_synthetic",
            quoted_or_summarized_basis="Synthetic fixture evidence for guardrail testing.",
        )
        for index in range(1, 3)
    ]

    with pytest.raises(ValidationError, match="high confidence recommendations require non-synthetic strong evidence"):
        ResearchGapCandidate(
            gap_id="gap-synthetic-only",
            topic_id="large-language-models",
            title="Synthetic-only gap",
            gap_statement="Synthetic-only evidence must not be high confidence.",
            supporting_paper_ids=["synthetic-1", "synthetic-2"],
            supporting_evidence_refs=synthetic_refs,
            limitation_refs=[],
            source_basis="accepted_ai_grounded_synthetic",
            confidence="high",
            confidence_score=0.9,
            novelty_rationale="Synthetic-only rationale.",
            risk_notes=[],
        )


def test_recommendations_are_deterministically_ordered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 4):
        paper_reviews.upsert_review(_review(index))

    first = recommendations.build_recommendations_for_topic("large-language-models", limit=5, topic_relevant_unique_count=524)
    second = recommendations.build_recommendations_for_topic("large-language-models", limit=5, topic_relevant_unique_count=524)

    assert [item["gap_id"] for item in first["gap_candidates"]] == [item["gap_id"] for item in second["gap_candidates"]]
    assert [item["topic_candidate_id"] for item in first["new_topic_candidates"]] == [
        item["topic_candidate_id"] for item in second["new_topic_candidates"]
    ]


def test_recommendation_quality_audit_flags_weak_and_missing_evidence() -> None:
    payload = {
        "topic_id": "large-language-models",
        "summary": {
            "full_text_reviewed_count": 1,
            "topic_relevant_unique_count": 524,
            "ai_grounded_accepted_count": 0,
        },
        "gap_candidates": [
            {
                "gap_id": "gap-rag-weak",
                "confidence": "medium",
                "confidence_score": 0.58,
                "coverage_warning": "preliminary: full-text reviewed coverage is 1 / 524 topic-relevant unique papers.",
                "supporting_paper_ids": ["abstract-paper", "full-text-paper"],
                "supporting_evidence_refs": [
                    {
                        "paper_id": "abstract-paper",
                        "review_level": "abstract_only",
                        "quoted_or_summarized_basis": "Abstract-only RAG signal.",
                    },
                    {
                        "paper_id": "full-text-paper",
                        "review_level": "full_text_reviewed",
                        "quoted_or_summarized_basis": "Full-text claim but missing hash/span metadata.",
                    },
                ],
            }
        ],
        "new_topic_candidates": [],
    }

    audit = recommendations.audit_recommendation_quality(payload)

    assert audit["summary"]["recommendation_count"] == 1
    assert audit["summary"]["weak_recommendation_count"] == 1
    assert audit["summary"]["needs_follow_up_count"] == 1
    assert audit["summary"]["abstract_only_support_total"] == 1
    assert audit["items"][0]["missing_evidence_span_count"] == 1
    assert audit["items"][0]["missing_source_hash_count"] == 1
    assert "abstract_only_support" in audit["items"][0]["unsupported_or_weak_claim_flags"]
    assert "missing_evidence_span" in audit["items"][0]["downgrade_reason_codes"]
    assert audit["items"][0]["needs_follow_up"] is True


def test_h006_scorecard_classifies_traceable_recommendations_as_local_rc_ready() -> None:
    result = recommendations.evaluate_recommendation_quality(_h006_good_payload(), known_papers=_h006_known_papers())

    assert result["recommendation_quality_classification"] == "recommendation_ready_local_rc"
    assert result["h006_total_score"] == result["h006_max_score"] == 20
    assert result["critical_fail_flags"] == []
    assert set(result["category_scores"]) == {
        "Provenance completeness",
        "Evidence support",
        "Speculation honesty",
        "Gap rationale quality",
        "Actionability",
        "Citation and paper traceability",
        "Coverage awareness",
        "Diversity and non-duplication",
        "Provider truthfulness",
        "Safety of downstream use",
    }
    assert all(category["score"] == 2 for category in result["category_scores"].values())
    assert result["summary"]["referenced_paper_count"] == 2


def test_h006_generated_recommendations_are_traceable_and_cautious(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    reviews = [_review(1), _review(2), _review(3)]
    for review in reviews:
        paper_reviews.upsert_review(review)

    payload = recommendations.build_recommendations_for_topic(
        "large-language-models",
        limit=2,
        topic_relevant_unique_count=3,
    )
    result = recommendations.evaluate_recommendation_quality(payload, known_papers=reviews)

    assert result["recommendation_quality_classification"] in {"recommendation_ready_local_rc", "usable_with_warnings"}
    assert result["critical_fail_flags"] == []
    assert result["category_scores"]["Provenance completeness"]["score"] == 2
    assert result["category_scores"]["Provider truthfulness"]["score"] == 2
    assert result["summary"]["strong_evidence_recommendation_count"] == result["summary"]["recommendation_count"]


def test_h006_blocks_recommendation_without_supporting_provenance() -> None:
    payload = _h006_good_payload()
    payload["gap_candidates"][0]["supporting_paper_ids"] = []
    payload["gap_candidates"][0]["supporting_evidence_refs"] = []

    result = recommendations.evaluate_recommendation_quality(payload, known_papers=_h006_known_papers())

    assert result["recommendation_quality_classification"] == "blocked_truthfulness_issue"
    assert "recommendation_missing_supporting_paper" in result["critical_fail_flags"]
    assert "recommendation_missing_evidence_ref" in result["critical_fail_flags"]
    assert result["category_scores"]["Provenance completeness"]["score"] == 0
    assert result["category_scores"]["Evidence support"]["score"] == 0


def test_h006_blocks_unknown_or_untraceable_supporting_papers() -> None:
    payload = _h006_good_payload()
    payload["gap_candidates"][0]["supporting_paper_ids"] = ["unknown-paper"]
    payload["gap_candidates"][0]["supporting_evidence_refs"] = [
        {
            "paper_id": "unknown-paper",
            "review_level": "deterministic_full_text",
            "quoted_or_summarized_basis": "A full-text claim without hash or span.",
        }
    ]

    result = recommendations.evaluate_recommendation_quality(payload, known_papers=_h006_known_papers())

    assert result["recommendation_quality_classification"] == "blocked_truthfulness_issue"
    assert "supporting_paper_unknown" in result["critical_fail_flags"]
    assert "reviewed_support_missing_source_text_sha256" in result["critical_fail_flags"]
    assert "reviewed_support_missing_evidence_span" in result["critical_fail_flags"]


def test_h006_blocks_provider_generated_novelty_and_verified_fact_overclaims() -> None:
    payload = _h006_good_payload()
    payload["gap_candidates"][0]["gap_statement"] = (
        "Gemini generated this confirmed gap and proves a novel breakthrough unexplored by all prior work."
    )

    result = recommendations.evaluate_recommendation_quality(payload, known_papers=_h006_known_papers())

    assert result["recommendation_quality_classification"] == "blocked_truthfulness_issue"
    assert "provider_generated_recommendation_claim" in result["critical_fail_flags"]
    assert "novelty_claim_without_support" in result["critical_fail_flags"]
    assert "recommendation_presented_as_verified_fact" in result["critical_fail_flags"]
    assert result["category_scores"]["Provider truthfulness"]["score"] == 0


def test_h006_blocks_full_text_overclaim_for_abstract_only_support() -> None:
    payload = _h006_good_payload()
    payload["gap_candidates"][0]["gap_statement"] = "Full-text supported evidence proves this candidate gap."
    payload["gap_candidates"][0]["supporting_paper_ids"] = ["h006-paper-1"]
    payload["gap_candidates"][0]["supporting_evidence_refs"] = [_h006_ref(1, review_level="abstract_only")]

    result = recommendations.evaluate_recommendation_quality(payload, known_papers=_h006_known_papers())

    assert result["recommendation_quality_classification"] == "blocked_truthfulness_issue"
    assert "full_text_support_claimed_for_abstract_only" in result["critical_fail_flags"]
    assert result["category_scores"]["Evidence support"]["score"] == 0


def test_h006_blocks_duplicate_recommendations() -> None:
    payload = _h006_good_payload()
    duplicate = dict(payload["gap_candidates"][0])
    duplicate["gap_id"] = "gap-h006-rag-evaluation-copy"
    payload["gap_candidates"].append(duplicate)

    result = recommendations.evaluate_recommendation_quality(payload, known_papers=_h006_known_papers())

    assert result["recommendation_quality_classification"] == "blocked_truthfulness_issue"
    assert "duplicate_recommendation" in result["critical_fail_flags"]
    assert result["summary"]["duplicate_recommendation_count"] == 1
    assert result["category_scores"]["Diversity and non-duplication"]["score"] == 0


def test_recommendation_followup_plan_excludes_reviewed_and_prioritizes_pdf_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1, paper_id="already-reviewed", title="Already Reviewed RAG Paper"))
    paper_reviews.upsert_review(
        _review(
            2,
            paper_id="abstract-rag-candidate",
            title="Abstract RAG Candidate",
            full_text_status="abstract_only",
            source_text_sha256="",
            evidence_spans=[],
        )
    )
    payload = {
        "topic_id": "large-language-models",
        "summary": {"full_text_reviewed_count": 1, "topic_relevant_unique_count": 524},
        "gap_candidates": [
            {
                "gap_id": "gap-rag-evaluation",
                "title": "RAG Evaluation evidence gap",
                "confidence": "medium",
                "confidence_score": 0.6,
                "coverage_warning": "preliminary coverage warning",
                "gap_statement": "RAG benchmark evaluation needs stronger full-text evidence.",
                "supporting_paper_ids": ["already-reviewed"],
                "supporting_evidence_refs": [
                    {
                        "paper_id": "already-reviewed",
                        "review_level": "full_text_reviewed",
                        "source_text_sha256": "recommendation-sha-1",
                        "evidence_span_id": "recommendation-span-1",
                        "quoted_or_summarized_basis": "Reviewed RAG benchmark signal.",
                    }
                ],
            }
        ],
        "new_topic_candidates": [],
    }
    candidate_rows = [
        {
            "paper_id": "already-reviewed",
            "normalized_paper_id": "already-reviewed",
            "title": "Already Reviewed RAG Paper",
            "abstract_text": "RAG benchmark evaluation.",
            "pdf_url": "https://arxiv.org/pdf/2601.00001",
            "source_provider": "arxiv",
            "topic_relevance_score": 99,
        },
        {
            "paper_id": "pdf-rag-candidate",
            "normalized_paper_id": "pdf-rag-candidate",
            "title": "RAG Benchmark Follow-up Paper",
            "abstract_text": "Retrieval augmented generation evaluation and benchmark drift.",
            "pdf_url": "https://arxiv.org/pdf/2601.00002",
            "source_provider": "arxiv",
            "topic_relevance_score": 94,
        },
        {
            "paper_id": "no-pdf-rag-candidate",
            "normalized_paper_id": "no-pdf-rag-candidate",
            "title": "RAG Benchmark Candidate Without PDF",
            "abstract_text": "RAG evaluation coverage and benchmark limitations.",
            "pdf_url": None,
            "source_provider": "arxiv",
            "topic_relevance_score": 96,
        },
        {
            "paper_id": "xr-low-fit",
            "normalized_paper_id": "xr-low-fit",
            "title": "XR Pose Paper",
            "abstract_text": "Virtual reality pose estimation.",
            "pdf_url": "https://arxiv.org/pdf/2601.00003",
            "source_provider": "arxiv",
            "topic_relevance_score": 5,
        },
    ]

    plan = recommendations.plan_recommendation_followup(
        "large-language-models",
        payload,
        limit=3,
        candidate_rows=candidate_rows,
    )

    candidates = plan["plans"][0]["candidates"]
    candidate_ids = [candidate["paper_id"] for candidate in candidates]
    assert "already-reviewed" not in candidate_ids
    assert candidates[0]["paper_id"] == "pdf-rag-candidate"
    assert candidates[0]["has_pdf_url"] is True
    assert "trusted_pdf_url" in candidates[0]["candidate_reason_codes"]
    assert any(candidate["blocked_reason"] == "missing_pdf_url" for candidate in candidates)
    assert plan["summary"]["followup_candidate_count"] == 2
    assert plan["summary"]["blocked_reason_counts"]["missing_pdf_url"] == 1


def test_recommendation_followup_execution_plans_skips_and_download_actions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(
        _review(
            1,
            paper_id="already-reviewed",
            title="Already Reviewed RAG Paper",
            source_text_sha256="reviewed-sha",
        )
    )
    payload = {
        "topic_id": "large-language-models",
        "summary": {"full_text_reviewed_count": 1, "topic_relevant_unique_count": 524},
        "gap_candidates": [
            {
                "gap_id": "gap-rag-followup",
                "title": "RAG follow-up evidence gap",
                "confidence": "medium",
                "confidence_score": 0.61,
                "coverage_warning": "preliminary coverage warning",
                "gap_statement": "RAG benchmark evaluation needs more full-text evidence.",
                "supporting_paper_ids": ["already-reviewed"],
                "supporting_evidence_refs": [
                    {
                        "paper_id": "already-reviewed",
                        "review_level": "full_text_reviewed",
                        "source_text_sha256": "reviewed-sha",
                        "evidence_span_id": "recommendation-span-1",
                        "quoted_or_summarized_basis": "Reviewed RAG benchmark signal.",
                    }
                ],
            }
        ],
        "new_topic_candidates": [],
    }
    candidate_rows = [
        {
            "paper_id": "already-reviewed",
            "normalized_paper_id": "already-reviewed",
            "title": "Already Reviewed RAG Paper",
            "abstract_text": "RAG benchmark evaluation.",
            "pdf_url": "https://arxiv.org/pdf/2601.00001",
            "source_provider": "arxiv",
            "topic_relevance_score": 99,
        },
        {
            "paper_id": "eligible-followup",
            "normalized_paper_id": "eligible-followup",
            "title": "RAG Benchmark Follow-up Paper",
            "abstract_text": "Retrieval augmented generation evaluation and benchmark drift.",
            "pdf_url": "https://arxiv.org/pdf/2601.00002",
            "source_provider": "arxiv",
            "topic_relevance_score": 94,
        },
        {
            "paper_id": "missing-pdf-followup",
            "normalized_paper_id": "missing-pdf-followup",
            "title": "RAG Candidate Missing PDF",
            "abstract_text": "RAG evaluation coverage and benchmark limitations.",
            "source_provider": "arxiv",
            "topic_relevance_score": 93,
        },
        {
            "paper_id": "bad-domain-followup",
            "normalized_paper_id": "bad-domain-followup",
            "title": "RAG Candidate External PDF",
            "abstract_text": "RAG benchmark evaluation.",
            "pdf_url": "https://example.com/bad.pdf",
            "source_provider": "arxiv",
            "topic_relevance_score": 92,
        },
    ]

    execution = recommendations.plan_recommendation_followup_execution(
        "large-language-models",
        payload,
        candidate_rows=candidate_rows,
        allowed_domains=["arxiv.org"],
        fetch_missing_pdf=True,
        extract_after_fetch=True,
        download_limit=1,
        review_limit=1,
    )

    actions = {action["paper_id"]: action for action in execution["actions"]}
    assert execution["summary"]["planned_followup_count"] == 1
    assert execution["summary"]["skipped_existing_reviewed_count"] == 1
    assert execution["summary"]["skipped_no_pdf_url_count"] == 1
    assert execution["summary"]["skipped_blocked_count"] == 1
    assert actions["eligible-followup"]["planned_action"] == "download_extract_review"
    assert actions["eligible-followup"]["allowed_domain_match"] is True
    assert actions["already-reviewed"]["planned_action"] == "skip_existing_reviewed"
    assert actions["missing-pdf-followup"]["planned_action"] == "skip_no_pdf_url"
    assert actions["bad-domain-followup"]["planned_action"] == "skip_blocked"
    assert "domain_not_allowed" in actions["bad-domain-followup"]["reason_codes"]


def test_recommendation_cli_dry_run_writes_nothing_and_export_is_explicit(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    export_path = tmp_path / "recommendations.json"

    assert (
        review_refresh.main(
            [
                "--topic",
                "large-language-models",
                "--dry-run",
                "--generate-recommendations",
                "--recommendation-limit",
                "2",
                "--export-recommendations",
                str(export_path),
            ]
        )
        == 0
    )
    dry_output = json.loads(capsys.readouterr().out)

    assert dry_output["mode"] == "research_recommendation_generation"
    assert dry_output["dry_run"] is True
    assert dry_output["written"] is False
    assert not export_path.exists()

    assert (
        review_refresh.main(
            [
                "--topic",
                "large-language-models",
                "--generate-recommendations",
                "--recommendation-limit",
                "2",
                "--export-recommendations",
                str(export_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["written"] is True
    assert export_path.exists()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["summary"]["candidate_gap_count"] >= 1
    assert exported["gap_candidates"][0]["supporting_paper_ids"]


def test_recommendation_dry_run_audit_without_generate_is_no_write(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    db_path = bootstrap.metadata_index_path()
    before_hash = _sha256_file(db_path)
    before_sidecars = _metadata_sidecars(db_path)

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--recommendation-dry-run",
            "--audit-recommendations",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["mode"] == "research_recommendation_generation"
    assert output["dry_run"] is True
    assert output["db_read_only"] is True
    assert output["writes"] is False
    assert output["written"] is False
    assert output["audit_written"] is False
    assert output["sidecar_guard"]["blocked"] is False
    assert _sha256_file(db_path) == before_hash
    assert _metadata_sidecars(db_path) == before_sidecars == {"-journal": False, "-wal": False, "-shm": False}


def test_recommendation_dry_run_audit_fails_closed_when_metadata_sidecar_exists(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    db_path = bootstrap.metadata_index_path()
    journal_path = db_path.with_name(f"{db_path.name}-journal")
    journal_path.write_bytes(b"simulated sidecar")
    before_hash = _sha256_file(db_path)
    before_journal_hash = _sha256_file(journal_path)

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--recommendation-dry-run",
            "--audit-recommendations",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["status"] == "recommendation_read_only_blocked"
    assert output["writes"] is False
    assert output["sidecar_guard"]["blocked"] is True
    assert output["sidecar_guard"]["sidecars"]["journal"]["exists"] is True
    assert _sha256_file(db_path) == before_hash
    assert _sha256_file(journal_path) == before_journal_hash


def test_recommendation_cli_audit_and_followup_exports_are_explicit(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    recommendations_path = tmp_path / "recommendations.json"
    audit_path = tmp_path / "audit.json"
    followup_path = tmp_path / "followup.json"

    assert (
        review_refresh.main(
            [
                "--topic",
                "large-language-models",
                "--dry-run",
                "--generate-recommendations",
                "--recommendation-limit",
                "2",
                "--audit-recommendations",
                "--plan-recommendation-followup",
                "--export-recommendations",
                str(recommendations_path),
                "--export-recommendation-audit",
                str(audit_path),
                "--export-recommendation-followup",
                str(followup_path),
            ]
        )
        == 0
    )
    dry_output = json.loads(capsys.readouterr().out)

    assert dry_output["dry_run"] is True
    assert dry_output["recommendation_audit"]["summary"]["recommendation_count"] >= 1
    assert dry_output["recommendation_followup"]["summary"]["recommendation_count"] >= 1
    assert not recommendations_path.exists()
    assert not audit_path.exists()
    assert not followup_path.exists()

    assert (
        review_refresh.main(
            [
                "--topic",
                "large-language-models",
                "--generate-recommendations",
                "--recommendation-limit",
                "2",
                "--audit-recommendations",
                "--plan-recommendation-followup",
                "--export-recommendations",
                str(recommendations_path),
                "--export-recommendation-audit",
                str(audit_path),
                "--export-recommendation-followup",
                str(followup_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["written"] is True
    assert recommendations_path.exists()
    assert audit_path.exists()
    assert followup_path.exists()
    assert json.loads(audit_path.read_text(encoding="utf-8"))["summary"]["recommendation_count"] >= 1
    assert json.loads(followup_path.read_text(encoding="utf-8"))["summary"]["recommendation_count"] >= 1


def test_recommendation_followup_execution_cli_dry_run_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    _patch_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = run_repository.create_run_record(
        topic=profile["topic"],
        request_payload={"topic": profile["topic"], "queries": profile["queries"], "categories": profile["categories"]},
        source="test:recommendation-followup",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "m62-followup-candidate",
                "title": "RAG Benchmark Follow-up Acquisition Candidate",
                "abstract": "Retrieval augmented generation benchmark drift and LLM evaluation coverage.",
                "score": 92,
                "source_provider": "arxiv",
                "published": "2026-05-09",
                "pdf_url": "https://arxiv.org/pdf/2605.62001",
            }
        ],
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run_repository.save_run_record(run)
    paper_reviews.upsert_review(_review(1, paper_id="m62-reviewed-seed", title="RAG Benchmark Seed Paper"))
    execution_path = tmp_path / "followup-execution.json"

    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--use-full-text",
            "--generate-recommendations",
            "--recommendation-limit",
            "2",
            "--audit-recommendations",
            "--plan-recommendation-followup",
            "--recommendation-followup-limit",
            "3",
            "--execute-recommendation-followup",
            "--fetch-missing-pdf",
            "--download-limit",
            "1",
            "--allow-domain",
            "arxiv.org",
            "--extract-after-fetch",
            "--compare-recommendation-quality-before-after",
            "--export-recommendation-followup-execution",
            str(execution_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["dry_run"] is True
    assert output["recommendation_followup_execution"]["dry_run"] is True
    assert output["recommendation_followup_execution"]["summary"]["planned_followup_count"] >= 1
    assert output["recommendation_quality_comparison"]["new_reviews_created"] is False
    assert not execution_path.exists()
    assert not (paths.raw_dir / "m62-followup-candidate.pdf").exists()
    assert paper_reviews.get_review(profile["profile_id"], run["run_id"], "m62-followup-candidate") is None


def test_recommendation_followup_execution_requires_explicit_pdf_gates() -> None:
    base_args = [
        "--topic",
        "large-language-models",
        "--generate-recommendations",
        "--plan-recommendation-followup",
        "--execute-recommendation-followup",
        "--fetch-missing-pdf",
        "--download-limit",
        "1",
    ]

    with pytest.raises(SystemExit):
        review_refresh.main(base_args)

    with pytest.raises(SystemExit):
        review_refresh.main([*base_args, "--allow-domain", "arxiv.org"])


def test_recommendation_followup_execution_downloads_extracts_and_compares_quality(tmp_path, monkeypatch, capsys) -> None:
    _patch_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = run_repository.create_run_record(
        topic=profile["topic"],
        request_payload={"topic": profile["topic"], "queries": profile["queries"], "categories": profile["categories"]},
        source="test:recommendation-followup",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "m62-write-candidate",
                "title": "RAG Benchmark Follow-up Execution Candidate",
                "abstract": "Retrieval augmented generation benchmark drift and LLM evaluation coverage.",
                "score": 92,
                "source_provider": "arxiv",
                "published": "2026-05-09",
                "pdf_url": "https://arxiv.org/pdf/2605.62002",
            }
        ],
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run_repository.save_run_record(run)
    paper_reviews.upsert_review(_review(1, paper_id="m62-reviewed-seed", title="RAG Benchmark Seed Paper"))
    execution_path = tmp_path / "followup-execution.json"

    install_pdf_urlopen_mock(
        monkeypatch,
        lambda _request, *, timeout: _FakePdfResponse(b"%PDF-1.7\nmock recommendation follow-up pdf\n%%EOF"),
    )
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [{"page_number": 1, "text": _long_extracted_text()}],
    )
    monkeypatch.setenv("ALLOW_BOUNDED_PDF_FETCH", "1")

    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--use-full-text",
            "--generate-recommendations",
            "--recommendation-limit",
            "2",
            "--audit-recommendations",
            "--plan-recommendation-followup",
            "--recommendation-followup-limit",
            "3",
            "--execute-recommendation-followup",
            "--fetch-missing-pdf",
            "--download-limit",
            "1",
            "--allow-domain",
            "arxiv.org",
            "--extract-after-fetch",
            "--allow-bounded-pdf-fetch",
            "--compare-recommendation-quality-before-after",
            "--export-recommendation-followup-execution",
            str(execution_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], "m62-write-candidate")

    assert exit_code == 0
    execution = output["recommendation_followup_execution"]
    assert execution["summary"]["downloaded_count"] == 1
    assert execution["summary"]["extracted_count"] == 1
    assert execution["summary"]["reviewed_from_followup_count"] == 1
    assert output["recommendation_quality_comparison"]["full_text_reviewed_count_after"] >= output["recommendation_quality_comparison"]["full_text_reviewed_count_before"]
    assert stored is not None
    assert stored["full_text_status"] == "reviewed"
    assert stored["source_text_sha256"]
    assert stored["evidence_spans"]
    assert execution_path.exists()
    assert (paths.raw_dir / "m62-write-candidate.pdf").exists()
    assert (paths.raw_dir / "m62-write-candidate.txt").exists()


def test_cli_json_print_falls_back_for_cp949_incompatible_text(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "stdout.txt"
    with output_path.open("w", encoding="cp949", errors="strict") as stream:
        monkeypatch.setattr(sys, "stdout", stream)
        review_refresh._print_json({"value": "RAG evidence – preliminary"})

    payload = json.loads(output_path.read_text(encoding="cp949"))
    assert payload["value"] == "RAG evidence – preliminary"

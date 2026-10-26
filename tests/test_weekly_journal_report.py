from __future__ import annotations

from app.db import bootstrap
from app.repositories import ai_review_drafts, artifact_refs, paper_reviews
from app.services import paper_review, reporting


def _profile() -> dict:
    return {
        "profile_id": "large-language-models",
        "title": "Large Language Models",
        "topic": "Large Language Models",
        "queries": ["large language model", "retrieval augmented generation", "alignment"],
    }


def _review(index: int, *, full_text_status: str = "abstract_only", **overrides) -> dict:
    payload = {
        "topic_id": "large-language-models",
        "run_id": "weekly-report-run",
        "paper_id": f"llm-weekly-{index}",
        "title": f"Evidence-Backed Large Language Model Report {index}",
        "source_provider": "arxiv",
        "abstract_text": "This abstract describes LLM evidence, RAG, alignment, and reporting.",
        "text_excerpt": "",
        "full_text_status": full_text_status,
        "relevance_score": 90 - index,
        "novelty_score": 60 + index,
        "evidence_strength": 45 + index,
        "method_summary": "초록 기반으로 RAG와 평가 방법을 요약했다.",
        "key_findings": [f"핵심 발견 {index}-1", f"핵심 발견 {index}-2"],
        "limitations": [f"한계 {index}: 본문 검토가 필요하다."],
        "why_it_matters": f"LLM 연구 에이전트 보고서 품질 개선에 중요하다 {index}.",
        "topic_fit_rationale": "LLM/RAG/alignment 주제와 일치한다.",
        "review_model": "deterministic-baseline-v1",
        "reviewer": "deterministic-baseline",
        "reviewed_at": "2026-05-07T00:00:00+00:00",
        "source_text_sha256": f"hash-{index}",
        "authors": ["Kim", "Lee"],
        "published_at": "2026-05-01",
    }
    payload.update(overrides)
    return payload


def _configure_storage(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    return storage_root


def _materialize_review_artifact(storage_root, review: dict, *, text: str | None = None) -> dict:
    run_id = str(review.get("run_id") or "weekly-report-run")
    paper_id = str(review.get("paper_id") or "paper")
    artifact = storage_root / run_id / "raw" / f"{paper_id}.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
    seed = " ".join(str(span.get("text") or "") for span in spans if isinstance(span, dict)).strip()
    body = text or " ".join([seed or str(review.get("title") or paper_id)] * 20)
    artifact.write_text(body, encoding="utf-8")
    review = dict(review)
    review["full_text_artifact_path"] = f"run:raw/{paper_id}.txt"
    review["source_text_sha256"] = paper_review._sha256_text(body)
    review["text_char_count"] = len(body)
    normalized_spans: list[dict] = []
    for span in spans:
        normalized = dict(span)
        normalized["source_path"] = review["full_text_artifact_path"]
        normalized_spans.append(normalized)
    review["evidence_spans"] = normalized_spans or [{"label": "span-1", "text": body[:120], "source_path": review["full_text_artifact_path"]}]
    return review


def test_weekly_journal_report_uses_stored_reviews_and_evidence_table(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 7):
        paper_reviews.upsert_review(_review(index))

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        run_id="weekly-report-run",
        generated_at="2026-05-07T00:00:00+00:00",
    )
    markdown = result["markdown"]

    assert result["status"] == "ok"
    assert result["review_count"] == 6
    assert "Executive Summary" in markdown
    assert "이번 주 핵심 연구 흐름" in markdown
    assert "주목할 논문 Top 5" in markdown
    assert "Evidence Sufficiency" not in markdown
    assert "방법론/데이터셋/평가 동향" in markdown
    assert "한계와 리스크" in markdown
    assert "다음 주 모니터링 포인트" in markdown
    assert "참고 논문 / evidence table" in markdown
    assert "Evidence-Backed Large Language Model Report 1" in markdown
    assert "relevance score" in markdown
    assert "abstract_only" in markdown
    assert "초록만 검토" in markdown
    assert "| paper_id | title | evidence source | full_text_status | relevance_score | full_text_artifact_path | source_text_sha256 |" in markdown
    assert "full-text를 검토했다고 가정하지 않습니다" in markdown
    assert "보고서 입력 기준 full_text_reviewed 0/6편" in markdown


def test_weekly_journal_report_reads_repositories_in_read_only_mode(monkeypatch) -> None:
    calls: dict[str, bool] = {}

    def fake_list_reviews(**kwargs):
        calls["reviews"] = bool(kwargs.get("read_only"))
        return [_review(1)]

    def fake_denominator(_topic_profile, _topic_id, fallback, *, read_only: bool = True):
        calls["denominator"] = read_only
        return fallback

    def fake_ai_drafts(_topic_id, _reviews, *, read_only: bool = True):
        calls["drafts"] = read_only
        return {}

    monkeypatch.setattr(reporting.paper_reviews, "list_reviews", fake_list_reviews)
    monkeypatch.setattr(reporting, "_topic_relevant_review_denominator", fake_denominator)
    monkeypatch.setattr(reporting, "_accepted_ai_drafts_for_reviews", fake_ai_drafts)

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        run_id="weekly-report-run",
        generated_at="2026-05-07T00:00:00+00:00",
    )

    assert result["status"] == "ok"
    assert calls == {"reviews": True, "denominator": True, "drafts": True}


def test_weekly_journal_report_deduplicates_same_paper_across_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(
        _review(
            1,
            run_id="weekly-report-run-a",
            paper_id="llm-duplicate-paper",
            title="Shared Large Language Model Review Paper",
            relevance_score=91,
        )
    )
    paper_reviews.upsert_review(
        _review(
            2,
            run_id="weekly-report-run-b",
            paper_id="llm-duplicate-paper",
            title="Shared Large Language Model Review Paper",
            relevance_score=95,
        )
    )
    paper_reviews.upsert_review(
        _review(
            3,
            run_id="weekly-report-run-c",
            paper_id="llm-unique-paper",
            title="Unique Retrieval-Augmented Generation Review Paper",
            relevance_score=82,
        )
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-07T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )
    markdown = result["markdown"]

    assert result["review_count"] == 2
    assert result["raw_review_count"] == 3
    assert markdown.count("**Shared Large Language Model Review Paper**") == 1
    assert markdown.count("| llm-duplicate-paper | Shared Large Language Model Review Paper | arxiv / 초록만 검토") == 1
    assert markdown.count("| llm-duplicate-paper | Shared Large Language Model Review Paper | 95.0 | 초록만 검토 |") == 1


def test_weekly_journal_report_labels_insufficient_unique_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1, paper_id="llm-shortfall-1", title="Shortfall LLM Paper 1"))
    paper_reviews.upsert_review(_review(2, paper_id="llm-shortfall-2", title="Shortfall LLM Paper 2"))

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-07T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )
    markdown = result["markdown"]

    assert result["review_count"] == 2
    assert "Evidence Sufficiency" in markdown
    assert "고유 리뷰 논문 2편" in markdown
    assert "주목할 논문 2/5" in markdown
    assert "주목할 논문 Top 5" not in markdown
    assert markdown.count("full_text_status: abstract_only") == 2


def test_weekly_journal_report_labels_mixed_full_text_availability_without_fabricating_evidence(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    reviewed = _materialize_review_artifact(
        storage_root,
        _review(
            1,
            paper_id="llm-reviewed",
            title="Reviewed Local Full Text LLM Paper",
            full_text_status="reviewed",
            full_text_artifact_path="storage/runs/run/raw/llm-reviewed.txt",
            review_source_type="full_text",
            text_char_count=128,
            source_text_sha256="reviewed-hash",
            evidence_spans=[
                {
                    "label": "full_text_excerpt_1",
                    "text": "local full text evidence supports retrieval augmented generation.",
                    "source_path": "storage/runs/run/raw/llm-reviewed.txt",
                    "page_number": None,
                    "char_start": 0,
                    "char_end": 64,
                }
            ],
        ),
    )
    paper_reviews.upsert_review(
        reviewed
    )
    paper_reviews.upsert_review(
        _review(
            2,
            paper_id="llm-available",
            title="Available But Not Extracted LLM Paper",
            full_text_status="full_text_available",
            full_text_artifact_path="storage/runs/run/raw/llm-available.pdf",
            source_text_sha256=None,
            evidence_spans=[],
        )
    )
    paper_reviews.upsert_review(_review(3, paper_id="llm-abstract", title="Abstract Only LLM Paper"))

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-07T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )
    markdown = result["markdown"]

    assert "full_text_reviewed 1편" in markdown
    assert "full_text_available 1편" in markdown
    assert "보고서 입력 기준 full_text_reviewed 1/3편" in markdown
    assert "## Evidence Appendix" in markdown
    assert "### Full-text reviewed papers" in markdown
    assert "### Abstract-only reviewed papers" in markdown
    assert "### Full-text available but not reviewed" in markdown
    assert "### Acquisition/extraction audit summary" in markdown
    assert "full-text reviewed coverage is 1 / 3 topic-relevant unique papers" in markdown
    assert f"| llm-reviewed | Reviewed Local Full Text LLM Paper | {reviewed['source_text_sha256']} | {reviewed['text_char_count']} | 1 |" in markdown
    assert "local full text evidence supports retrieval augmented generation" in markdown
    assert "full_text_status: full_text_available" in markdown
    assert "Available But Not Extracted LLM Paper" in markdown
    assert "| llm-available | Available But Not Extracted LLM Paper |" in markdown
    assert "storage:runs/run/raw/llm-available.pdf (missing)" in markdown
    assert "| llm-available | Available But Not Extracted LLM Paper | arxiv / 로컬 전문 파일은 있으나 이번 보고서에서 본문 근거로 사용하지 않음 | full_text_available" in markdown


def test_weekly_journal_report_sanitizes_legacy_absolute_artifact_paths(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    from app.repositories import artifact_refs

    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    artifact = storage_root / "weekly-report-run" / "raw" / "llm-reviewed.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Legacy absolute path should not be printed.", encoding="utf-8")
    legacy_absolute = r"C:\Users\alice\old-checkout\storage\runs\weekly-report-run\raw\llm-reviewed.txt"
    paper_reviews.upsert_review(
        _review(
            1,
            paper_id="llm-reviewed",
            title="Reviewed Legacy Path LLM Paper",
            full_text_status="reviewed",
            full_text_artifact_path=legacy_absolute,
            review_source_type="full_text",
            text_char_count=128,
            source_text_sha256="reviewed-hash",
            evidence_spans=[
                {
                    "label": "legacy-span",
                    "text": "Legacy absolute path should not be printed.",
                    "source_path": legacy_absolute,
                    "page_number": None,
                    "char_start": 0,
                    "char_end": 44,
                }
            ],
        )
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        run_id="weekly-report-run",
        generated_at="2026-05-07T00:00:00+00:00",
    )
    markdown = result["markdown"]

    assert "C:\\Users\\alice" not in markdown
    assert "old-checkout" not in markdown
    assert "run:raw/llm-reviewed.txt" in markdown
    assert "reviewed-hash" in markdown


def test_weekly_journal_report_suppresses_unverified_reviewed_evidence(tmp_path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    paper_reviews.upsert_review(
        _review(
            1,
            paper_id="llm-unverified",
            title="Unverified Full Text LLM Paper",
            full_text_status="reviewed",
            full_text_artifact_path="run:raw/missing.txt",
            review_source_type="full_text",
            text_char_count=512,
            source_text_sha256="missing-artifact-sha",
            evidence_spans=[
                {
                    "label": "invalid-span",
                    "text": "This missing artifact span must not be shown as full-text evidence.",
                    "source_path": "run:raw/missing.txt",
                }
            ],
        )
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-08T00:00:00+00:00",
        topic_relevant_unique_count=1,
    )
    markdown = result["markdown"]

    assert "보고서 입력 기준 full_text_reviewed 0/1편" in markdown
    assert "full-text reviewed coverage is 0 / 1 topic-relevant unique papers" in markdown
    assert "full_text_status: unavailable" in markdown
    assert "This missing artifact span must not be shown" not in markdown
    assert "| - | - | - | 0 | 0 | - |" in markdown


def test_weekly_journal_report_uses_only_accepted_grounded_ai_drafts(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    reviewed = _materialize_review_artifact(
        storage_root,
        _review(
            1,
            paper_id="llm-ai-accepted",
            title="Accepted AI Grounded LLM Paper",
            full_text_status="reviewed",
            source_text_sha256="accepted-sha",
            full_text_artifact_path="storage/runs/run/raw/llm-ai-accepted.txt",
            review_source_type="full_text",
            text_char_count=512,
            evidence_spans=[
                {
                    "label": "accepted-span",
                    "text": "grounded evidence about retrieval augmented generation.",
                    "source_path": "storage/runs/run/raw/llm-ai-accepted.txt",
                    "page_number": 2,
                    "char_start": 0,
                    "char_end": 64,
                }
            ],
        ),
    )
    rejected = _materialize_review_artifact(
        storage_root,
        _review(
            2,
            paper_id="llm-ai-rejected",
            title="Rejected AI Grounded LLM Paper",
            full_text_status="reviewed",
            source_text_sha256="rejected-sha",
            full_text_artifact_path="storage/runs/run/raw/llm-ai-rejected.txt",
            review_source_type="full_text",
            text_char_count=512,
            evidence_spans=[
                {
                    "label": "rejected-span",
                    "text": "grounded evidence that should not be used by rejected draft.",
                    "source_path": "storage/runs/run/raw/llm-ai-rejected.txt",
                }
            ],
        ),
    )
    paper_reviews.upsert_review(reviewed)
    paper_reviews.upsert_review(rejected)
    ai_review_drafts.upsert_draft(
        {
            "topic_id": "large-language-models",
            "run_id": "weekly-report-run",
            "paper_id": "llm-ai-accepted",
            "source_text_sha256": reviewed["source_text_sha256"],
            "review_level": "full_text_grounded",
            "status": "accepted",
            "draft": {
                "paper_id": "llm-ai-accepted",
                "source_text_sha256": reviewed["source_text_sha256"],
                "contribution_summary": "AI draft: validated contribution summary.",
                "method_summary": "AI draft: grounded method summary.",
                "evidence_summary": "AI draft: evidence summary cites accepted-span.",
                "limitations": ["AI draft: limitation grounded in evidence."],
                "relevance_to_topic": "AI draft: relevant to LLM/RAG monitoring.",
                "novelty_signal": "moderate",
                "practical_implications": "Use for evidence-backed weekly synthesis.",
                "follow_up_questions": ["Which benchmark is most robust?"],
                "recommended_tags": ["RAG"],
                "cited_evidence_span_ids": ["accepted-span"],
                "confidence": 0.82,
                "unsupported_claims": [],
                "review_level": "full_text_grounded",
            },
            "validation": {"status": "accepted", "reason_codes": []},
        }
    )
    ai_review_drafts.upsert_draft(
        {
            "topic_id": "large-language-models",
            "run_id": "weekly-report-run",
            "paper_id": "llm-ai-rejected",
            "source_text_sha256": rejected["source_text_sha256"],
            "review_level": "full_text_grounded",
            "status": "rejected",
            "draft": {
                "paper_id": "llm-ai-rejected",
                "source_text_sha256": rejected["source_text_sha256"],
                "contribution_summary": "Rejected draft must not improve the report.",
                "review_level": "full_text_grounded",
            },
            "validation": {"status": "rejected", "reason_codes": ["missing_cited_evidence_span_ids"]},
        }
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-08T00:00:00+00:00",
        topic_relevant_unique_count=2,
    )
    markdown = result["markdown"]

    assert "AI-grounded review coverage is 1 / 2 full-text reviewed papers." in markdown
    assert "AI draft: validated contribution summary." in markdown
    assert "accepted-span" in markdown
    assert reviewed["source_text_sha256"] in markdown
    assert "Rejected draft must not improve the report" not in markdown


def test_weekly_journal_report_distinguishes_provider_generated_ai_drafts(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    accepted_review = _materialize_review_artifact(
        storage_root,
        _review(
            1,
            paper_id="llm-provider-accepted",
            title="Provider Accepted LLM Paper",
            full_text_status="reviewed",
            source_text_sha256="provider-accepted-sha",
            full_text_artifact_path="storage/runs/run/raw/llm-provider-accepted.txt",
            review_source_type="full_text",
            text_char_count=512,
            evidence_spans=[
                {
                    "label": "provider-span",
                    "text": "provider grounded evidence about RAG.",
                    "source_path": "storage/runs/run/raw/llm-provider-accepted.txt",
                }
            ],
        ),
    )
    rejected_review = _materialize_review_artifact(
        storage_root,
        _review(
            2,
            paper_id="llm-provider-rejected",
            title="Provider Rejected LLM Paper",
            full_text_status="reviewed",
            source_text_sha256="provider-rejected-sha",
            full_text_artifact_path="storage/runs/run/raw/llm-provider-rejected.txt",
            review_source_type="full_text",
            text_char_count=512,
            evidence_spans=[
                {
                    "label": "provider-rejected-span",
                    "text": "provider rejected evidence.",
                    "source_path": "storage/runs/run/raw/llm-provider-rejected.txt",
                }
            ],
        ),
    )
    paper_reviews.upsert_review(accepted_review)
    paper_reviews.upsert_review(rejected_review)
    ai_review_drafts.upsert_draft(
        {
            "topic_id": "large-language-models",
            "run_id": "weekly-report-run",
            "paper_id": "llm-provider-accepted",
            "source_text_sha256": accepted_review["source_text_sha256"],
            "review_level": "full_text_grounded",
            "status": "accepted",
            "draft": {
                "paper_id": "llm-provider-accepted",
                "source_text_sha256": accepted_review["source_text_sha256"],
                "contribution_summary": "Provider draft: accepted contribution.",
                "method_summary": "Provider draft: method.",
                "evidence_summary": "Provider draft: evidence cites provider-span.",
                "limitations": ["Provider draft: limitation."],
                "relevance_to_topic": "Provider draft: relevant.",
                "novelty_signal": "moderate",
                "practical_implications": "Provider draft: useful.",
                "follow_up_questions": [],
                "recommended_tags": ["RAG"],
                "cited_evidence_span_ids": ["provider-span"],
                "confidence": 0.8,
                "unsupported_claims": [],
                "review_level": "full_text_grounded",
            },
            "validation": {
                "status": "accepted",
                "reason_codes": [],
                "provider": {
                    "name": "command",
                    "mode": "live_command",
                    "origin": "command_fixture",
                    "run_id": "provider-run-a",
                    "generated": True,
                },
            },
        }
    )
    ai_review_drafts.upsert_draft(
        {
            "topic_id": "large-language-models",
            "run_id": "weekly-report-run",
            "paper_id": "llm-provider-rejected",
            "source_text_sha256": rejected_review["source_text_sha256"],
            "review_level": "full_text_grounded",
            "status": "rejected",
            "draft": {
                "paper_id": "llm-provider-rejected",
                "source_text_sha256": rejected_review["source_text_sha256"],
                "contribution_summary": "Rejected provider draft must not be used.",
                "review_level": "full_text_grounded",
            },
            "validation": {
                "status": "rejected",
                "reason_codes": ["missing_cited_evidence_span_ids"],
                "provider": {"name": "stub", "mode": "stub", "generated": True},
            },
        }
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-08T00:00:00+00:00",
        topic_relevant_unique_count=2,
    )
    markdown = result["markdown"]

    assert "Provider-generated accepted AI drafts: 1" in markdown
    assert "Provider-orchestrated accepted draft coverage is 1 / 2 full-text reviewed papers." in markdown
    assert "Synthetic provider accepted drafts: 1" in markdown
    assert "Operator-accepted real provider accepted drafts: 0" in markdown
    assert "AI provider: command / mode: live_command / origin: command_fixture" in markdown
    assert "(local fixture/synthetic)" in markdown
    assert "provider-run-a" in markdown
    assert f"| llm-provider-accepted | full_text_grounded | command_fixture_synthetic | {accepted_review['source_text_sha256']} | provider-span | 0.8 |" in markdown
    assert "Provider draft: accepted contribution." in markdown
    assert "Rejected provider draft must not be used" not in markdown


def test_weekly_journal_report_labels_real_command_provider_origin(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    reviewed = _materialize_review_artifact(
        storage_root,
        _review(
            1,
            paper_id="llm-real-provider",
            title="Real Provider Accepted LLM Paper",
            full_text_status="reviewed",
            source_text_sha256="real-provider-sha",
            full_text_artifact_path="storage/runs/run/raw/llm-real-provider.txt",
            review_source_type="full_text",
            text_char_count=512,
            evidence_spans=[
                {
                    "label": "real-provider-span",
                    "text": "real provider grounded evidence about RAG.",
                    "source_path": "storage/runs/run/raw/llm-real-provider.txt",
                }
            ],
        ),
    )
    paper_reviews.upsert_review(reviewed)
    ai_review_drafts.upsert_draft(
        {
            "topic_id": "large-language-models",
            "run_id": "weekly-report-run",
            "paper_id": "llm-real-provider",
            "source_text_sha256": reviewed["source_text_sha256"],
            "review_level": "full_text_grounded",
            "status": "accepted",
            "draft": {
                "paper_id": "llm-real-provider",
                "source_text_sha256": reviewed["source_text_sha256"],
                "contribution_summary": "Real provider accepted contribution.",
                "method_summary": "Real provider method.",
                "evidence_summary": "Real provider evidence cites real-provider-span.",
                "limitations": ["Real provider limitation."],
                "relevance_to_topic": "Real provider relevant.",
                "novelty_signal": "moderate",
                "practical_implications": "Real provider useful.",
                "follow_up_questions": [],
                "recommended_tags": ["RAG"],
                "cited_evidence_span_ids": ["real-provider-span"],
                "confidence": 0.8,
                "unsupported_claims": [],
                "review_level": "full_text_grounded",
            },
            "validation": {
                "status": "accepted",
                "reason_codes": [],
                "provider": {
                    "name": "command",
                    "mode": "live_command",
                    "origin": "real_provider",
                    "run_id": "real-provider-run",
                    "generated": True,
                    "operator_accepted": True,
                },
            },
        }
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-08T00:00:00+00:00",
        topic_relevant_unique_count=1,
    )
    markdown = result["markdown"]

    assert "AI provider: command / mode: live_command / origin: real_provider" in markdown
    assert "(operator accepted real provider)" in markdown
    assert "Operator-accepted real provider accepted drafts: 1" in markdown
    assert "stub/synthetic" not in markdown
    assert "local fixture/synthetic" not in markdown


def test_weekly_journal_report_includes_preliminary_recommendations_without_fabricating_claims(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    reviewed = _materialize_review_artifact(
        storage_root,
        _review(
            1,
            paper_id="llm-gap-reviewed",
            title="RAG Evaluation Drift Paper",
            full_text_status="reviewed",
            source_text_sha256="gap-reviewed-sha",
            full_text_artifact_path="storage/runs/run/raw/llm-gap-reviewed.txt",
            review_source_type="full_text",
            text_char_count=512,
            abstract_text="RAG benchmark evaluation drift motivates cautious follow-up research.",
            method_summary="Tracks RAG evaluation and benchmark drift.",
            limitations=["Benchmark coverage remains narrow."],
            evidence_spans=[
                {
                    "label": "gap-span",
                    "text": "Full-text evidence links RAG evaluation drift to benchmark coverage.",
                    "source_path": "storage/runs/run/raw/llm-gap-reviewed.txt",
                }
            ],
        ),
    )
    paper_reviews.upsert_review(reviewed)
    paper_reviews.upsert_review(
        _review(
            2,
            paper_id="llm-gap-abstract",
            title="Abstract Only Gap Paper",
            abstract_text="An abstract-only paper mentions RAG benchmark coverage.",
        )
    )
    ai_review_drafts.upsert_draft(
        {
            "topic_id": "large-language-models",
            "run_id": "weekly-report-run",
            "paper_id": "llm-gap-reviewed",
            "source_text_sha256": reviewed["source_text_sha256"],
            "review_level": "full_text_grounded",
            "status": "accepted",
            "draft": {
                "paper_id": "llm-gap-reviewed",
                "source_text_sha256": reviewed["source_text_sha256"],
                "contribution_summary": "Grounded recommendation signal.",
                "method_summary": "AI method summary.",
                "evidence_summary": "Cites gap-span.",
                "limitations": ["AI limitation: benchmark coverage remains narrow."],
                "relevance_to_topic": "RAG evaluation gap.",
                "novelty_signal": "moderate",
                "practical_implications": "Track drift before stronger claims.",
                "follow_up_questions": ["Which RAG benchmarks drift across domains?"],
                "recommended_tags": ["RAG", "evaluation"],
                "cited_evidence_span_ids": ["gap-span"],
                "confidence": 0.8,
                "unsupported_claims": [],
                "review_level": "full_text_grounded",
            },
            "validation": {
                "status": "accepted",
                "reason_codes": [],
                "provider": {"name": "stub", "mode": "stub", "origin": "stub", "generated": True},
            },
        }
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=_profile(),
        topic_id="large-language-models",
        generated_at="2026-05-08T00:00:00+00:00",
        topic_relevant_unique_count=524,
    )
    markdown = result["markdown"]

    assert "## Research Gaps & New Topic Candidates" in markdown
    assert "preliminary" in markdown.lower()
    assert "full-text reviewed coverage is 1 / 524 topic-relevant unique papers" in markdown
    assert "AI-grounded accepted draft coverage is 1 / 1 full-text reviewed papers" in markdown
    assert "llm-gap-reviewed" in markdown
    assert reviewed["source_text_sha256"] in markdown
    assert "gap-span" in markdown
    assert "Abstract Only Gap Paper" in markdown
    assert "exhaustive" in markdown.lower()
    assert "evidence strength:" in markdown
    assert "follow-up acquisition suggestion:" in markdown
    assert "not yet reviewed" in markdown


def test_weekly_journal_report_includes_followup_execution_summary_without_overclaiming() -> None:
    execution = {
        "summary": {
            "planned_followup_count": 3,
            "skipped_existing_reviewed_count": 1,
            "skipped_no_pdf_url_count": 1,
            "skipped_blocked_count": 1,
            "downloaded_count": 1,
            "extracted_count": 1,
            "reviewed_from_followup_count": 1,
            "rejected_count_by_reason": {"file_too_large": 1},
            "failed_count_by_reason": {},
        },
        "actions": [
            {
                "recommendation_id": "gap-rag-followup",
                "paper_id": "reviewed-paper",
                "title": "Already Reviewed Paper",
                "planned_action": "skip_existing_reviewed",
                "reason_codes": ["already_full_text_reviewed"],
            },
            {
                "recommendation_id": "gap-rag-followup",
                "paper_id": "newly-reviewed-paper",
                "title": "Newly Reviewed Follow-up Paper",
                "planned_action": "download_extract_review",
                "reason_codes": ["downloaded", "extracted", "reviewed"],
                "source_text_sha256": "new-followup-sha",
            },
            {
                "recommendation_id": "gap-rag-followup",
                "paper_id": "missing-pdf-paper",
                "title": "Missing PDF Candidate",
                "planned_action": "skip_no_pdf_url",
                "reason_codes": ["missing_pdf_url"],
            },
        ],
    }
    comparison = {
        "new_reviews_created": True,
        "full_text_reviewed_count_before": 22,
        "full_text_reviewed_count_after": 23,
        "weak_recommendation_count_before": 10,
        "weak_recommendation_count_after": 10,
        "needs_follow_up_count_before": 10,
        "needs_follow_up_count_after": 10,
        "confidence_distribution_before": {"medium": 10},
        "confidence_distribution_after": {"medium": 10},
    }

    markdown = reporting.build_weekly_journal_markdown(
        topic_profile=_profile(),
        reviews=[
            _review(
                1,
                paper_id="newly-reviewed-paper",
                title="Newly Reviewed Follow-up Paper",
                full_text_status="reviewed",
                source_text_sha256="new-followup-sha",
                full_text_artifact_path="storage/runs/run/raw/newly-reviewed-paper.txt",
                evidence_spans=[{"label": "followup-span", "text": "New full-text evidence from local extraction."}],
            )
        ],
        generated_at="2026-05-09T00:00:00+00:00",
        topic_relevant_unique_count=524,
        recommendation_followup_execution=execution,
        recommendation_quality_comparison=comparison,
    )

    assert "## Recommendation Follow-up Execution" in markdown
    assert "planned follow-up candidates: 3" in markdown
    assert "executed downloads: 1" in markdown
    assert "newly full-text reviewed papers: 1" in markdown
    assert "skip_existing_reviewed" in markdown
    assert "skip_no_pdf_url" in markdown
    assert "newly-reviewed-paper" in markdown
    assert "new-followup-sha" in markdown
    assert "skipped candidates are not counted as reviewed evidence" in markdown
    assert "weak recommendations before/after: 10 / 10" in markdown

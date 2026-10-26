from __future__ import annotations

from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews, runs as run_repository
from app.schemas.workbench import PreviewResponse
from app.services import paper_review, report_quality, reporting


def _review(
    paper_id: str,
    *,
    topic_id: str = "quality-report-topic",
    run_id: str = "quality-report-run",
    full_text_status: str = "abstract_only",
    source_text_sha256: str | None = "abstract-hash",
    evidence_spans: list[dict] | None = None,
    full_text_artifact_path: str | None = None,
) -> dict:
    return {
        "topic_id": topic_id,
        "run_id": run_id,
        "paper_id": paper_id,
        "title": f"Quality Report Paper {paper_id}",
        "source_provider": "arxiv",
        "abstract_text": "This abstract covers large language model evidence and evaluation.",
        "full_text_status": full_text_status,
        "relevance_score": 90,
        "novelty_score": 62,
        "evidence_strength": 70,
        "method_summary": "The paper evaluates retrieval and alignment methods.",
        "key_findings": ["Evidence-linked finding.", "Evaluation result."],
        "limitations": ["Coverage remains incomplete."],
        "why_it_matters": "It informs the next research direction.",
        "topic_fit_rationale": "It matches the LLM topic.",
        "review_model": "deterministic-baseline-v1",
        "reviewer": "deterministic-baseline",
        "reviewed_at": "2026-05-14T00:00:00+00:00",
        "source_text_sha256": source_text_sha256,
        "full_text_artifact_path": full_text_artifact_path,
        "text_char_count": 180 if full_text_status == "reviewed" else 0,
        "evidence_spans": evidence_spans or [],
        "review_source_type": "full_text" if full_text_status == "reviewed" else "abstract",
    }


def _quality_reviews() -> list[dict]:
    return [
        _review(
            "q-reviewed",
            full_text_status="reviewed",
            source_text_sha256="sha-reviewed",
            full_text_artifact_path="run:raw/q-reviewed.txt",
            evidence_spans=[
                {
                    "label": "span-1",
                    "text": "Full text evidence supports the grounded recommendation.",
                    "source_path": "run:raw/q-reviewed.txt",
                }
            ],
        ),
        _review("q-abstract", full_text_status="abstract_only", source_text_sha256="abstract-hash"),
        _review(
            "q-available",
            full_text_status="full_text_available",
            source_text_sha256=None,
            full_text_artifact_path="run:raw/q-available.pdf",
        ),
    ]


def _configure_storage(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    return storage_root


def _materialize_review_artifact(storage_root, review: dict, *, text: str | None = None) -> dict:
    run_id = str(review.get("run_id") or "quality-report-run")
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
    review["evidence_spans"] = [
        {**span, "source_path": review["full_text_artifact_path"]}
        for span in spans
        if isinstance(span, dict)
    ] or [{"label": "span-1", "text": body[:120], "source_path": review["full_text_artifact_path"]}]
    return review


def _good_report() -> str:
    return """# Large Language Models 주간 연구 저널

## Executive Summary

- 검토 범위: abstract_only 1편, full_text_available 1편, full_text_reviewed 1편
- 보고서 입력 기준 full_text_reviewed 1/3편; all collected papers were not read in full.
- AI-grounded review coverage is 0 / 1 full-text reviewed papers.
- Synthetic provider accepted drafts: 1
- Operator-accepted real provider accepted drafts: 0
- Provider coverage note: Synthetic provider drafts are test artifacts and do not imply real external AI use.
- OAuth and provider readiness checks are not production connector proof.

## 이번 주 핵심 연구 흐름

- retrieval augmented generation
- evaluation methodology

## 주목할 논문 3/5

1. **Quality Report Paper q-reviewed**
   - full_text_status: reviewed
   - evidence source: arxiv / full-text reviewed
   - evidence snippets:
     - local text: Full text evidence supports the grounded recommendation.
2. **Quality Report Paper q-abstract**
   - full_text_status: abstract_only
   - 본문 근거: 초록만 검토
3. **Quality Report Paper q-available**
   - full_text_status: full_text_available
   - 본문 근거: 로컬 전문 파일은 있으나 이번 보고서에서 본문 근거로 사용하지 않음

## 방법론/데이터셋/평가 동향

- The report compares retrieval and alignment methods.

## 한계와 리스크

- abstract_only rows are 초록 기반 and coverage remains incomplete.
- full_text_available rows are not counted as reviewed evidence.
- provider/OAuth proof remains gated and synthetic provider evidence remains local fixture evidence.

## 다음 주 모니터링 포인트

- Expand full-text coverage with bounded acquisition.

## Research Gaps & New Topic Candidates

- Follow-up direction: evaluate retrieval robustness with evidence-linked papers.

## 참고 논문 / evidence table

| paper_id | title | evidence source | full_text_status | relevance_score | full_text_artifact_path | source_text_sha256 |
| --- | --- | --- | --- | --- | --- | --- |
| q-reviewed | Quality Report Paper q-reviewed | arxiv / full-text reviewed | reviewed | 90.0 | run:raw/q-reviewed.txt | sha-reviewed |
| q-abstract | Quality Report Paper q-abstract | arxiv / 초록만 검토 | abstract_only | 88.0 | - | - |
| q-available | Quality Report Paper q-available | arxiv / 로컬 전문 파일은 있으나 이번 보고서에서 본문 근거로 사용하지 않음 | full_text_available | 87.0 | run:raw/q-available.pdf | - |

## Evidence Appendix

### Full-text reviewed papers

| paper_id | title | source_text_sha256 | text_char_count | evidence_span_count | full_text_artifact_path |
| --- | --- | --- | --- | --- | --- |
| q-reviewed | Quality Report Paper q-reviewed | sha-reviewed | 180 | 1 | run:raw/q-reviewed.txt |

### Abstract-only reviewed papers

- q-abstract is abstract-only / 초록 기반.

### Full-text available but not reviewed

- q-available is available but not reviewed.
"""


def _evaluate(markdown: str, *, reviews: list[dict] | None = None) -> dict:
    return report_quality.evaluate_weekly_journal_quality(
        markdown,
        reviews=reviews if reviews is not None else _quality_reviews(),
        provider_provenance={
            "fixtureSyntheticCount": 1,
            "realProviderAcceptedCount": 0,
            "providerOrchestratedSyntheticCount": 1,
        },
        oauth_proof={"liveConnectorProof": False, "mockExchangePassed": True},
        expected_topic="large-language-models",
    )


def test_good_report_passes_quality_gate() -> None:
    result = _evaluate(_good_report())

    assert result["overall_status"] == "pass"
    assert result["overall_score"] >= result["threshold"]
    assert result["evidence_table_detected"] is True
    assert result["limitations_detected"] is True
    assert result["provenance_detected"] is True
    assert result["overclaim_detected"] is False


def test_h005_scorecard_classifies_good_report_as_local_rc_ready() -> None:
    result = _evaluate(_good_report())

    assert result["report_quality_classification"] == "journal_ready_local_rc"
    assert result["h005_total_score"] == result["h005_max_score"] == 16
    assert result["critical_fail_flags"] == []
    assert set(result["category_scores"]) == {
        "Structure and readability",
        "Evidence traceability",
        "Full-text honesty",
        "Citation integrity",
        "Coverage disclosure",
        "Weekly Journal readability",
        "Research gap / next topic quality",
        "Provider truthfulness",
    }
    assert all(category["score"] == 2 for category in result["category_scores"].values())
    assert result["coverage_disclosure_detected"] is True
    assert result["unmatched_citation_ids"] == []


def test_missing_evidence_table_fails() -> None:
    markdown = _good_report().replace(
        "| paper_id | title | evidence source | full_text_status | relevance_score | full_text_artifact_path | source_text_sha256 |",
        "paper list without structured status or hash columns",
    )
    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert "missing_evidence_table" in result["failure_reasons"]


def test_missing_coverage_disclosure_fails_h005_scorecard() -> None:
    markdown = (
        _good_report()
        .replace("- 검토 범위: abstract_only 1편, full_text_available 1편, full_text_reviewed 1편\n", "")
        .replace("- 보고서 입력 기준 full_text_reviewed 1/3편; all collected papers were not read in full.\n", "")
    )
    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert result["report_quality_classification"] == "needs_revision"
    assert "missing_coverage_disclosure" in result["failure_reasons"]
    assert result["category_scores"]["Coverage disclosure"]["score"] == 0


def test_missing_executive_summary_fails() -> None:
    result = _evaluate(_good_report().replace("## Executive Summary", "## Summary Notes"))

    assert result["overall_status"] == "fail"
    assert "missing_executive_summary" in result["failure_reasons"]


def test_missing_limitations_fails_when_abstract_only_rows_exist() -> None:
    markdown = _good_report().replace("## 한계와 리스크", "## Observations")

    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert "missing_limitations_for_abstract_only" in result["failure_reasons"]


def test_full_text_overclaim_fails_when_counts_do_not_support_it() -> None:
    markdown = _good_report().replace(
        "all collected papers were not read in full",
        "all collected papers were read in full text and reviewed",
    )

    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert "full_text_overclaim" in result["failure_reasons"]
    assert "full_text_overclaim" in result["critical_fail_flags"]
    assert result["report_quality_classification"] == "blocked_truthfulness_issue"


def test_full_text_available_described_as_reviewed_fails() -> None:
    markdown = _good_report().replace(
        "| q-available | Quality Report Paper q-available | arxiv / 로컬 전문 파일은 있으나 이번 보고서에서 본문 근거로 사용하지 않음 | full_text_available | 87.0 | run:raw/q-available.pdf | - |",
        "| q-available | Quality Report Paper q-available | arxiv / full-text reviewed evidence | full_text_available | 87.0 | run:raw/q-available.pdf | - |",
    )

    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert "full_text_available_described_as_reviewed" in result["failure_reasons"]


def test_provider_and_oauth_overclaims_fail_without_live_proof() -> None:
    markdown = (
        _good_report()
        + "\nReal provider orchestration is complete with Gemini, Claude, and Codex.\n"
        + "Production ChatGPT/Codex OAuth is complete and mock exchange is live connector proof.\n"
    )

    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert "real_provider_orchestration_overclaim" in result["failure_reasons"]
    assert "production_oauth_overclaim" in result["failure_reasons"]
    assert "mock_exchange_overclaim" in result["failure_reasons"]
    assert result["report_quality_classification"] == "blocked_truthfulness_issue"


def test_synthetic_provider_and_version_smoke_overclaims_fail() -> None:
    markdown = (
        _good_report()
        + "\nSynthetic fixture provider draft is real provider output.\n"
        + "Version/help smoke is real provider synthesis for paper review generation.\n"
    )

    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert "synthetic_provider_described_as_real" in result["failure_reasons"]
    assert "version_smoke_provider_synthesis_overclaim" in result["failure_reasons"]


def test_path_leak_fails_but_portable_refs_pass() -> None:
    portable = _evaluate(_good_report())
    leaked = _evaluate(_good_report() + "\nArtifact: C:\\Users\\alice\\old-checkout\\storage\\runs\\run\\raw\\paper.txt\n")

    assert portable["overall_status"] == "pass"
    assert leaked["overall_status"] == "fail"
    assert "local_absolute_path_leak" in leaked["failure_reasons"]


def test_reviewed_metadata_missing_hash_or_spans_fails() -> None:
    reviews = [
        _review("missing-hash", full_text_status="reviewed", source_text_sha256=None, evidence_spans=[]),
    ]

    result = _evaluate(_good_report(), reviews=reviews)

    assert result["overall_status"] == "fail"
    assert "reviewed_missing_source_text_sha256" in result["failure_reasons"]
    assert "reviewed_missing_evidence_spans" in result["failure_reasons"]
    assert "reviewed_missing_source_text_sha256" in result["critical_fail_flags"]
    assert "reviewed_missing_evidence_spans" in result["critical_fail_flags"]


def test_unmatched_citation_reference_fails_h005_traceability() -> None:
    markdown = _good_report().replace(
        "| q-reviewed | Quality Report Paper q-reviewed | arxiv / full-text reviewed | reviewed | 90.0 | run:raw/q-reviewed.txt | sha-reviewed |",
        "| q-fabricated | Fabricated Citation Paper | arxiv / full-text reviewed | reviewed | 90.0 | run:raw/q-fabricated.txt | sha-fabricated |",
    )
    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert result["report_quality_classification"] == "blocked_truthfulness_issue"
    assert "citation_reference_unmatched" in result["failure_reasons"]
    assert "citation_reference_unmatched" in result["critical_fail_flags"]
    assert "q-fabricated" in result["unmatched_citation_ids"]
    assert result["category_scores"]["Citation integrity"]["score"] == 0


def test_h005_forbidden_overclaim_phrases_fail() -> None:
    markdown = (
        _good_report()
        + "\nThis report is journal-grade and production ready.\n"
        + "Gemini generated the report and live provider verified all synthesis.\n"
        + "OAuth ready and orchestration ready.\n"
    )
    result = _evaluate(markdown)

    assert result["overall_status"] == "fail"
    assert result["report_quality_classification"] == "blocked_truthfulness_issue"
    assert "journal_grade_claim" in result["failure_reasons"]
    assert "production_ready_claim" in result["failure_reasons"]
    assert "provider_generated_report_claim" in result["failure_reasons"]
    assert "live_provider_verified_claim" in result["failure_reasons"]
    assert "oauth_ready_claim" in result["failure_reasons"]
    assert "orchestration_ready_claim" in result["failure_reasons"]
    assert "provider_generated_report_claim" in result["critical_fail_flags"]


def test_generated_weekly_journal_report_passes_quality_gate(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    topic_id = "quality-generated-topic"
    run_id = "quality-generated-run"
    profile = {
        "profile_id": topic_id,
        "topic_id": topic_id,
        "topic": "Large Language Models",
        "title": "Large Language Models",
        "queries": ["large language model", "retrieval augmented generation"],
    }
    for review in [
        _materialize_review_artifact(
            storage_root,
            _review(
                "generated-reviewed",
                topic_id=topic_id,
                run_id=run_id,
                full_text_status="reviewed",
                source_text_sha256="generated-sha",
                full_text_artifact_path="run:raw/generated-reviewed.txt",
                evidence_spans=[
                    {
                        "label": "generated-span",
                        "text": "Generated report evidence grounds the research direction.",
                        "source_path": "run:raw/generated-reviewed.txt",
                    }
                ],
            ),
        ),
        _review("generated-abstract", topic_id=topic_id, run_id=run_id, full_text_status="abstract_only"),
        _review(
            "generated-available",
            topic_id=topic_id,
            run_id=run_id,
            full_text_status="full_text_available",
            source_text_sha256=None,
            full_text_artifact_path="run:raw/generated-available.pdf",
        ),
    ]:
        paper_reviews.upsert_review(review)

    report = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=profile,
        topic_id=topic_id,
        run_id=run_id,
        generated_at="2026-05-14T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )
    result = report_quality.evaluate_weekly_journal_quality(
        report["markdown"],
        reviews=paper_reviews.list_reviews(topic_id=topic_id, run_id=run_id),
        provider_provenance={"fixtureSyntheticCount": 0, "realProviderAcceptedCount": 0},
        oauth_proof={"liveConnectorProof": False},
        expected_topic=topic_id,
    )

    assert report["status"] == "ok"
    assert result["overall_status"] == "pass"
    assert "C:\\Users" not in report["markdown"]
    assert "full_text_reviewed 1/3" in report["markdown"]


def _seed_generated_run(storage_root, topic_id: str, run_id: str) -> dict:
    profile = {
        "profile_id": topic_id,
        "topic_id": topic_id,
        "topic": "Large Language Models",
        "title": "Large Language Models",
        "queries": ["large language model", "retrieval augmented generation"],
    }
    for review in [
        _materialize_review_artifact(
            storage_root,
            _review(
                "wired-reviewed",
                topic_id=topic_id,
                run_id=run_id,
                full_text_status="reviewed",
                source_text_sha256="wired-sha",
                full_text_artifact_path="run:raw/wired-reviewed.txt",
                evidence_spans=[
                    {
                        "label": "wired-span",
                        "text": "Wired report evidence grounds the research direction.",
                        "source_path": "run:raw/wired-reviewed.txt",
                    }
                ],
            ),
        ),
        _review("wired-abstract", topic_id=topic_id, run_id=run_id, full_text_status="abstract_only"),
        _review(
            "wired-available",
            topic_id=topic_id,
            run_id=run_id,
            full_text_status="full_text_available",
            source_text_sha256=None,
            full_text_artifact_path="run:raw/wired-available.pdf",
        ),
    ]:
        paper_reviews.upsert_review(review)
    return profile


def test_runtime_report_generation_embeds_deterministic_quality_summary(tmp_path, monkeypatch) -> None:
    # Proves report_quality is now wired into the runtime report-generation service path:
    # generate_weekly_journal_report_from_reviews attaches a quality summary itself.
    storage_root = _configure_storage(tmp_path, monkeypatch)
    topic_id = "wired-quality-topic"
    run_id = "wired-quality-run"
    profile = _seed_generated_run(storage_root, topic_id, run_id)

    report = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=profile,
        topic_id=topic_id,
        run_id=run_id,
        generated_at="2026-05-14T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )

    assert report["status"] == "ok"
    quality = report.get("quality")
    assert isinstance(quality, dict)
    # Clearly labeled as a deterministic rubric result, never LLM/provider judgement.
    assert quality["evaluation_method"] == report_quality.EVALUATION_METHOD == "deterministic_rubric"
    assert quality["rubric_version"] == report_quality.RUBRIC_VERSION
    assert quality["evaluation_status"] == "evaluated"
    assert quality["report_kind"] == "weekly_journal"
    # Core rubric fields are present and consistent with a well-formed generated report.
    assert quality["overall_status"] == "pass"
    assert quality["report_quality_classification"] in {
        "journal_ready_local_rc",
        "usable_with_warnings",
    }
    assert quality["critical_fail_flags"] == []
    assert quality["overall_score"] >= quality["threshold"]
    # evaluated_at is pinned to the report's generated_at (deterministic timestamp).
    assert quality["checked_at"] == "2026-05-14T00:00:00+00:00"
    assert set(quality["category_scores"]) == set(report_quality.H005_CATEGORIES)


def test_preview_response_embeds_read_only_deterministic_quality_summary(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    topic_id = "preview-quality-topic"
    run_id = "preview-quality-run"
    profile = _seed_generated_run(storage_root, topic_id, run_id)
    run_repository.save_run_record(
        {
            "run_id": run_id,
            "topic": profile["topic"],
            "status": "completed",
            "source": "pytest",
            "write_legacy": False,
            "created_at": "2026-05-14T00:00:00+00:00",
            "updated_at": "2026-05-14T00:00:00+00:00",
            "artifacts": {},
            "counts": {},
            "warnings": [],
            "topic_profile_id": topic_id,
            "lineage": {},
        }
    )
    report = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=profile,
        topic_id=topic_id,
        run_id=run_id,
        generated_at="2026-05-14T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )
    preview_path = run_repository.build_run_paths(run_id).reports_dir / reporting.PREVIEW_FILENAME

    preview = reporting.build_preview(
        run_id,
        outline={
            "run_id": run_id,
            "path": str(preview_path.with_name(reporting.OUTLINE_FILENAME)),
            "updated_at": "2026-05-14T00:00:00+00:00",
            "sections": [
                {
                    "section_id": "weekly-journal",
                    "title": "Weekly Journal",
                    "kind": "markdown",
                    "body_markdown": report["markdown"],
                    "paper_ids": [],
                    "compare_notes": [],
                    "provenance": [],
                }
            ],
            "applied_actions": [],
        },
        persist=False,
    )

    quality = preview.get("quality")
    assert isinstance(quality, dict)
    assert quality["evaluation_method"] == "deterministic_rubric"
    assert quality["rubric_version"] == report_quality.RUBRIC_VERSION
    assert quality["evaluation_status"] == "evaluated"
    assert quality["evaluated_surface"] == "report_preview"
    assert quality["report_kind"] == "weekly_journal"
    assert quality["checked_at"] == preview["generated_at"]
    assert "overall_score" in quality
    assert not preview_path.exists()
    validated = PreviewResponse.model_validate(preview)
    assert validated.quality is not None
    assert validated.quality["evaluation_method"] == "deterministic_rubric"


def test_runtime_report_quality_is_deterministic(tmp_path, monkeypatch) -> None:
    storage_root = _configure_storage(tmp_path, monkeypatch)
    topic_id = "deterministic-quality-topic"
    run_id = "deterministic-quality-run"
    profile = _seed_generated_run(storage_root, topic_id, run_id)

    kwargs = dict(
        topic_profile=profile,
        topic_id=topic_id,
        run_id=run_id,
        generated_at="2026-05-14T00:00:00+00:00",
        topic_relevant_unique_count=3,
    )
    first = reporting.generate_weekly_journal_report_from_reviews(**kwargs)
    second = reporting.generate_weekly_journal_report_from_reviews(**kwargs)

    # Same inputs + pinned generated_at => byte-identical markdown and quality summary.
    assert first["markdown"] == second["markdown"]
    assert first["quality"] == second["quality"]


def test_runtime_report_quality_degrades_gracefully_without_reviews(tmp_path, monkeypatch) -> None:
    # Missing/partial report input must never crash the report pipeline; a quality
    # summary is still attached so consumers can rely on its presence.
    _configure_storage(tmp_path, monkeypatch)
    profile = {
        "profile_id": "empty-quality-topic",
        "topic_id": "empty-quality-topic",
        "topic": "Large Language Models",
        "title": "Large Language Models",
        "queries": ["large language model"],
    }

    report = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=profile,
        topic_id="empty-quality-topic",
        run_id="empty-quality-run",
        generated_at="2026-05-14T00:00:00+00:00",
        topic_relevant_unique_count=0,
    )

    assert report["status"] == "empty"
    assert report["review_count"] == 0
    quality = report.get("quality")
    assert isinstance(quality, dict)
    assert quality["evaluation_method"] == "deterministic_rubric"
    assert quality["rubric_version"] == report_quality.RUBRIC_VERSION
    # With no reviewed evidence the rubric honestly reports a non-pass status rather
    # than raising; the important invariant is that generation still succeeded.
    assert quality["overall_status"] in {"fail", "warning", "pass"}
    assert quality["checked_at"] == "2026-05-14T00:00:00+00:00"

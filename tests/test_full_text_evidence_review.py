from __future__ import annotations

import json

from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews, runs as run_repository
from app.repositories.files import write_jsonl
from app.services import paper_review, reporting, topic_analytics, topic_profiles
from app.cli import review_refresh


def _patch_local_topic_storage(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    profile_dir = storage_root.parent / "topic_profiles"
    analytics_dir = storage_root.parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)


def _create_run_with_artifacts(
    profile: dict,
    *,
    paper_id: str = "2605.12345v1",
    artifact_kind: str | None = "text",
) -> dict:
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
        source="test:full-text",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": paper_id,
                "title": "Full Text Large Language Model Agents with Retrieval",
                "abstract": "Large language model agents use retrieval augmented generation for grounded research.",
                "score": 52,
                "source_provider": "arxiv",
                "published": "2026-05-04",
            }
        ],
    )
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    if artifact_kind == "text":
        (paths.raw_dir / f"{paper_id}.txt").write_text(
            """
            Full Text Large Language Model Agents with Retrieval.
            Method: the system combines retrieval augmented generation with planner agents.
            Evaluation: evidence-grounded answers improve citation traceability and reduce unsupported claims.
            Limitation: the local benchmark remains small and needs broader multilingual validation.
            """,
            encoding="utf-8",
        )
    elif artifact_kind == "pages":
        (paths.raw_dir / f"{paper_id}.pages.json").write_text(
            json.dumps(
                {
                    "paper_id": paper_id,
                    "pages": [
                        {
                            "page": 1,
                            "text": "Page one introduces retrieval augmented generation for large language model agents.",
                        },
                        {
                            "page": 2,
                            "text": "Page two reports improved evidence traceability and cites limitations.",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif artifact_kind == "pdf":
        (paths.raw_dir / f"{paper_id}.pdf").write_bytes(b"%PDF-1.4 local placeholder")
    elif artifact_kind is None:
        pass
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    return run_repository.save_run_record(run)


def test_read_local_full_text_evidence_reads_from_explicit_source_run_id(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    source_run = _create_run_with_artifacts(profile, paper_id="2605.12345v1", artifact_kind="text")
    target_run = _create_run_with_artifacts(profile, paper_id="2605.12345v1", artifact_kind=None)

    evidence = paper_review.read_local_full_text_evidence(
        run_id=target_run["run_id"],
        paper_id="2605.12345v1",
        max_text_chars=160,
        text_source_run_id=source_run["run_id"],
    )

    assert evidence["full_text_status"] == "reviewed"
    assert evidence["artifact_source_run_id"] == source_run["run_id"]
    assert evidence["full_text_artifact_path"].startswith("storage:")
    assert evidence["full_text_artifact_path"].endswith("2605.12345v1.txt")
    assert evidence["evidence_spans"][0]["source_path"].endswith("2605.12345v1.txt")


def test_generate_deterministic_review_uses_explicit_source_run_artifact_path(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    source_run = _create_run_with_artifacts(profile, paper_id="2605.90123v1", artifact_kind="text")
    target_run = _create_run_with_artifacts(profile, paper_id="2605.90123v1", artifact_kind=None)

    review = paper_review.generate_deterministic_review(
        topic_profile=profile,
        run=target_run,
        item={
            "paper_id": "2605.90123v1",
            "title": "Explicit Source Run Review",
            "abstract": "Local artifact fallback test for explicit source run id.",
            "source_provider": "arxiv",
        },
        use_full_text=True,
        text_source_run_id=source_run["run_id"],
    )

    assert review["full_text_status"] == "reviewed"
    assert review["review_source_type"] == "full_text"
    assert review["full_text_artifact_path"].startswith("storage:")
    assert review["full_text_artifact_path"].endswith("2605.90123v1.txt")
    assert review["source_text_sha256"]


def test_read_local_full_text_evidence_missing_source_run_returns_unavailable(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    target_run = _create_run_with_artifacts(profile, paper_id="2605.12345v1", artifact_kind="text")

    evidence = paper_review.read_local_full_text_evidence(
        run_id=target_run["run_id"],
        paper_id="2605.12345v1",
        max_text_chars=120,
        text_source_run_id="nonexistent-run-id",
    )

    assert evidence["full_text_status"] == "unavailable"
    assert evidence["full_text_source_kind"] is None


def test_full_text_reader_prefers_text_and_hashes_actual_content(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)

    evidence = paper_review.read_local_full_text_evidence(
        run_id=run["run_id"],
        paper_id="2605.12345v1",
        max_text_chars=160,
    )

    assert evidence["full_text_status"] == "reviewed"
    assert evidence["review_source_type"] == "full_text"
    assert evidence["text_char_count"] > 0
    assert len(evidence["text_excerpt"]) <= 160
    assert evidence["source_text_sha256"]
    assert evidence["evidence_spans"][0]["source_path"].endswith("2605.12345v1.txt")
    assert evidence["evidence_spans"][0]["char_start"] == 0


def test_pages_json_review_includes_page_aware_evidence_spans(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile, paper_id="2605.54321v1", artifact_kind="pages")

    items = paper_review._review_candidates(profile, limit=1)
    review = paper_review.generate_deterministic_review(
        topic_profile=profile,
        run=run,
        item=items[0][1],
        use_full_text=True,
        max_text_chars=500,
    )

    assert review["full_text_status"] == "reviewed"
    assert review["review_source_type"] == "full_text"
    assert review["full_text_artifact_path"].endswith("2605.54321v1.pages.json")
    assert {span["page_number"] for span in review["evidence_spans"]} == {1, 2}
    assert "본문 근거" in " ".join(review["key_findings"])


def test_full_text_available_is_not_reviewed_without_use_full_text(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)

    item = paper_review._review_candidates(profile, limit=1)[0][1]
    abstract_review = paper_review.generate_deterministic_review(topic_profile=profile, run=run, item=item)
    full_text_review = paper_review.generate_deterministic_review(
        topic_profile=profile,
        run=run,
        item=item,
        use_full_text=True,
    )

    assert abstract_review["full_text_status"] == "full_text_available"
    assert abstract_review["review_source_type"] == "abstract"
    assert full_text_review["full_text_status"] == "reviewed"
    assert full_text_review["review_source_type"] == "full_text"
    assert abstract_review["source_text_sha256"] != full_text_review["source_text_sha256"]


def test_review_refresh_full_text_counts_and_dry_run(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_run_with_artifacts(profile)

    dry_exit = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--unique-limit",
            "1",
            "--use-full-text",
        ]
    )
    dry = json.loads(capsys.readouterr().out)
    stored_after_dry_run = paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=10)
    write = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=True)
    stored_after_write = paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=10)

    assert dry_exit == 0
    assert dry["dry_run"] is True
    assert dry["full_text_reviewed_count"] == 1
    assert dry["reviewed_from_text_count"] == 1
    assert dry["reviewed_from_pages_count"] == 0
    assert stored_after_dry_run == []
    assert write["full_text_reviewed_count"] == 1
    assert write["reviewed_from_text_count"] == 1
    assert len(stored_after_write) == 1
    assert stored_after_write[0]["full_text_status"] == "reviewed"
    assert stored_after_write[0]["evidence_spans"][0]["text"]


def test_use_full_text_dry_run_reports_existing_abstract_review_upgrade(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.12345v1",
            "title": "Full Text Large Language Model Agents with Retrieval",
            "source_provider": "arxiv",
            "abstract_text": "Existing abstract-only record.",
            "full_text_status": "abstract_only",
            "relevance_score": 90,
            "novelty_score": 60,
            "evidence_strength": 45,
            "method_summary": "초록 기반 기존 리뷰.",
            "key_findings": ["기존 초록 리뷰."],
            "limitations": ["본문은 아직 읽지 않음."],
            "why_it_matters": "upgrade dry-run 검증.",
            "topic_fit_rationale": "LLM/RAG 주제와 일치.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "source_text_sha256": "existing-abstract-hash",
        }
    )

    dry = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        dry_run=True,
    )
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    assert dry["unique_reviewed_count"] == 1
    assert dry["unique_skipped_count"] == 0
    assert dry["full_text_reviewed_count"] == 1
    assert dry["reviewed_from_text_count"] == 1
    assert stored is not None
    assert stored["full_text_status"] == "abstract_only"


def test_existing_abstract_review_upgrades_to_reviewed_with_force_false(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.12345v1",
            "title": "Full Text Large Language Model Agents with Retrieval",
            "source_provider": "arxiv",
            "abstract_text": "Existing abstract-only record.",
            "full_text_status": "abstract_only",
            "relevance_score": 90,
            "novelty_score": 60,
            "evidence_strength": 45,
            "method_summary": "초록 기반 기존 리뷰.",
            "key_findings": ["기존 초록 리뷰."],
            "limitations": ["본문은 아직 읽지 않음."],
            "why_it_matters": "upgrade 검증.",
            "topic_fit_rationale": "LLM/RAG 주제와 일치.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "source_text_sha256": "existing-abstract-hash",
        }
    )

    result = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=True)
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    assert result["unique_reviewed_count"] == 1
    assert result["unique_skipped_count"] == 0
    assert result["full_text_reviewed_count"] == 1
    assert stored is not None
    assert stored["full_text_status"] == "reviewed"
    assert stored["review_source_type"] == "full_text"
    assert stored["evidence_spans"]


def test_existing_reviewed_record_is_skipped_without_better_artifact(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)
    first = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=True)
    stored_before = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    second = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=True)
    stored_after = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    assert first["full_text_reviewed_count"] == 1
    assert second["unique_reviewed_count"] == 0
    assert second["unique_skipped_count"] == 1
    assert stored_before is not None and stored_after is not None
    assert stored_after["full_text_status"] == "reviewed"
    assert stored_after["source_text_sha256"] == stored_before["source_text_sha256"]


def test_force_refresh_preserves_reviewed_status_when_evidence_exists(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)
    paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=True)

    forced = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        force=True,
    )
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    assert forced["unique_reviewed_count"] == 1
    assert forced["full_text_reviewed_count"] == 1
    assert stored is not None
    assert stored["full_text_status"] == "reviewed"
    assert stored["evidence_spans"]


def test_reviewed_record_is_not_downgraded_by_abstract_refresh(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_run_with_artifacts(profile)
    paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=True)
    stored_before = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    result = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=1, use_full_text=False, force=True)
    stored_after = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.12345v1")

    assert result["full_text_reviewed_count"] == 1
    assert stored_before is not None and stored_after is not None
    assert stored_after["full_text_status"] == "reviewed"
    assert stored_after["source_text_sha256"] == stored_before["source_text_sha256"]


def test_weekly_report_includes_full_text_evidence_only_for_reviewed_records(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    full_text = " ".join(["retrieval augmented generation improves traceability for local full text evidence."] * 20)
    artifact = storage_root / "report-run-full" / "raw" / "full-text-paper.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(full_text, encoding="utf-8")
    paper_reviews.upsert_review(
        {
            "topic_id": "large-language-models",
            "run_id": "report-run-full",
            "paper_id": "full-text-paper",
            "title": "Reviewed Full Text LLM Evidence Paper",
            "source_provider": "arxiv",
            "abstract_text": "LLM abstract.",
            "text_excerpt": "Full text evidence shows retrieval augmented generation improves traceability.",
            "full_text_status": "reviewed",
            "relevance_score": 95,
            "novelty_score": 70,
            "evidence_strength": 80,
            "method_summary": "본문 근거 기반 방법 요약.",
            "key_findings": ["본문 근거: retrieval augmented generation improves traceability."],
            "limitations": ["본문 근거 기반 한계도 포함한다."],
            "why_it_matters": "본문 근거가 보고서 신뢰도를 높인다.",
            "topic_fit_rationale": "LLM/RAG 주제와 일치한다.",
            "review_model": "deterministic-local-full-text-v1",
            "reviewer": "deterministic-local-full-text",
            "source_text_sha256": paper_review._sha256_text(full_text),
            "review_source_type": "full_text",
            "text_char_count": len(full_text),
            "full_text_artifact_path": "run:raw/full-text-paper.txt",
            "evidence_spans": [
                {
                    "label": "full_text_excerpt_1",
                    "text": "retrieval augmented generation improves traceability",
                    "source_path": "run:raw/full-text-paper.txt",
                    "page_number": None,
                    "char_start": 0,
                    "char_end": 52,
                }
            ],
        }
    )
    paper_reviews.upsert_review(
        {
            "topic_id": "large-language-models",
            "run_id": "report-run-abstract",
            "paper_id": "abstract-paper",
            "title": "Abstract Only LLM Evidence Paper",
            "source_provider": "arxiv",
            "abstract_text": "Only abstract was available.",
            "full_text_status": "abstract_only",
            "relevance_score": 90,
            "novelty_score": 64,
            "evidence_strength": 45,
            "method_summary": "초록 기반 요약.",
            "key_findings": ["초록 기반 발견."],
            "limitations": ["본문은 검토하지 않았다."],
            "why_it_matters": "초록 기반 주제 적합성.",
            "topic_fit_rationale": "LLM 주제와 일치한다.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "source_text_sha256": "abstract-hash",
        }
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile={
            "profile_id": "large-language-models",
            "title": "Large Language Models",
            "topic": "Large Language Models",
            "queries": ["large language model", "retrieval augmented generation"],
        },
        topic_id="large-language-models",
        generated_at="2026-05-07T00:00:00+00:00",
    )
    markdown = result["markdown"]

    assert "검토 범위:" in markdown
    assert "full_text_reviewed 1편" in markdown
    assert "evidence snippets" in markdown
    assert "retrieval augmented generation improves traceability" in markdown
    assert "full_text_status: reviewed" in markdown
    assert "full_text_status: abstract_only" in markdown
    assert "Abstract Only LLM Evidence Paper" in markdown
    assert "본문 근거: 초록만 검토" in markdown

from __future__ import annotations

import json
import hashlib

import pytest

from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews, runs as run_repository
from app.repositories.files import write_jsonl
from app.services import paper_review, pdf_acquisition, reporting, topic_analytics, topic_profiles
from app.cli import review_refresh
from tests.research_transport_fakes import install_pdf_urlopen_mock


def _file_sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _long_extracted_text(label: str = "retrieval augmented generation") -> str:
    return (
        f"Full text evidence about {label} for large language model agents. "
        "The extracted local text is intentionally long enough to pass deterministic quality checks. "
        "It includes methods, evaluation signals, limitations, and evidence traceability for report generation. "
        "This baseline text is used only in mocked tests and never calls an external AI provider."
    )


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
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)


def _create_refresh_run(profile: dict) -> dict:
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
        source="test:review-refresh",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "llm-refresh-1",
                "title": "Retrieval-Augmented Large Language Model Agents",
                "abstract": "Large language model agents use retrieval augmented generation and tool use for evidence-backed reasoning.",
                "score": 48,
                "source_provider": "arxiv",
                "published": "2026-05-02",
            },
            {
                "paper_id": "xr-refresh-1",
                "title": "Gaze Prediction in Virtual Reality Without Eye Tracking",
                "abstract": "Virtual reality, pose tracking, AR, VR, VIO, and HMD signals for head-mounted displays.",
                "score": 96,
                "source_provider": "arxiv",
                "published": "2026-05-03",
            },
        ],
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run["steps"] = [{"name": "summary", "status": "completed"}]
    return run_repository.save_run_record(run)


def _create_review_refresh_run_with_text_artifact(
    profile: dict,
    *,
    paper_id: str,
    title: str,
    abstract: str,
    include_text_artifact: bool = True,
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
                "title": title,
                "abstract": abstract,
                "score": 91,
                "source_provider": "arxiv",
                "published": "2026-05-10",
            }
        ],
    )
    if include_text_artifact:
        paths.raw_dir.mkdir(parents=True, exist_ok=True)
        (paths.raw_dir / f"{paper_id}.txt").write_text(
            f"{abstract} This is local full-text evidence for {paper_id}.",
            encoding="utf-8",
        )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    return run_repository.save_run_record(run)


def _seed_existing_review(profile: dict, run: dict) -> None:
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "llm-refresh-1",
            "title": "Retrieval-Augmented Large Language Model Agents",
            "source_provider": "arxiv",
            "abstract_text": "Existing abstract review.",
            "full_text_status": "abstract_only",
            "relevance_score": 88,
            "novelty_score": 61,
            "evidence_strength": 45,
            "method_summary": "기존 초록 기반 리뷰입니다.",
            "key_findings": ["기존 리뷰가 유지됩니다."],
            "limitations": ["본문은 처리되지 않았습니다."],
            "why_it_matters": "idempotent skip 검증용입니다.",
            "topic_fit_rationale": "LLM/RAG 주제에 부합합니다.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-07T00:00:00+00:00",
            "source_text_sha256": "existing-review",
        }
    )


def _seed_candidate_control_rows(profile: dict, run: dict) -> None:
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.03205v1",
                "title": "From Knowledge to Action: Outcomes of the 2025 Large Language Model Hackathon",
                "abstract": (
                    "Large language model hackathon outcomes for materials science and chemistry with "
                    "agents, retrieval augmented generation, and benchmark evaluation."
                ),
                "score": 100,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.03205v1",
            },
            {
                "paper_id": "2604.26498v1",
                "title": "Do Larger Models Really Win in Drug Discovery?",
                "abstract": (
                    "A benchmark assessment of model scaling in AI-driven molecular property and "
                    "activity prediction with large language model evaluation signals."
                ),
                "score": 68,
                "source_provider": "arxiv",
                "published": "2026-04-30",
                "pdf_url": "https://arxiv.org/pdf/2604.26498v1",
            },
            {
                "paper_id": "2604.other-v1",
                "title": "Large Language Model Retrieval Planning",
                "abstract": "Large language model retrieval augmented generation planning.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-04-29",
                "pdf_url": "https://arxiv.org/pdf/2604.other-v1",
            },
        ],
    )


def test_refresh_reviews_for_topic_is_idempotent_and_force_refreshes_existing(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    _seed_existing_review(profile, run)

    first = paper_review.refresh_reviews_for_topic(profile["profile_id"], limit=2)
    second = paper_review.refresh_reviews_for_topic(profile["profile_id"], limit=2)
    forced = paper_review.refresh_reviews_for_topic(profile["profile_id"], limit=2, force=True)
    stored = paper_reviews.list_reviews(topic_id=profile["profile_id"], run_id=run["run_id"], limit=10)

    assert first["discovered_count"] == 2
    assert first["reviewed_count"] == 1
    assert first["skipped_count"] == 1
    assert first["abstract_only_count"] == 2
    assert first["unavailable_count"] == 0
    assert first["full_text_reviewed_count"] == 0
    assert first["low_relevance_count"] == 1
    assert second["reviewed_count"] == 0
    assert second["skipped_count"] == 2
    assert forced["reviewed_count"] == 2
    assert forced["skipped_count"] == 0
    assert len(stored) == 2


def test_refresh_reviews_for_topic_reports_raw_and_unique_counts_for_duplicates(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    first_run = _create_refresh_run(profile)
    _create_refresh_run(profile)
    _seed_existing_review(profile, first_run)

    first = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=2)
    second = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=2)
    forced = paper_review.refresh_reviews_for_topic(profile["profile_id"], unique_limit=2, force=True)
    stored = paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=20)

    assert first["raw_discovered_count"] == 4
    assert first["unique_discovered_count"] == 2
    assert first["duplicate_row_count"] == 2
    assert first["unique_reviewed_count"] == 1
    assert first["unique_skipped_count"] == 1
    assert first["raw_skipped_count"] == 2
    assert first["report_eligible_unique_count"] == 2
    assert second["unique_reviewed_count"] == 0
    assert second["unique_skipped_count"] == 2
    assert forced["unique_reviewed_count"] == 2
    assert forced["unique_skipped_count"] == 0
    assert len({review["paper_id"] for review in stored}) == 2


def test_refresh_reviews_for_topic_dry_run_and_filters_are_unique_scoped(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_refresh_run(profile)
    _create_refresh_run(profile)

    dry_run = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        dry_run=True,
        unique_limit=1,
        min_relevance=60,
    )
    stored_after_dry_run = paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=20)
    actual = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        min_relevance=60,
    )
    stored_after_actual = paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=20)

    assert dry_run["dry_run"] is True
    assert dry_run["raw_discovered_count"] == 4
    assert dry_run["unique_discovered_count"] == 1
    assert dry_run["unique_reviewed_count"] == 1
    assert stored_after_dry_run == []
    assert actual["unique_reviewed_count"] == 1
    assert len(stored_after_actual) == 1


def test_review_refresh_cli_emits_json_summary(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_refresh_run(profile)

    exit_code = review_refresh.main(["--topic", profile["profile_id"], "--limit", "1"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["topic_id"] == profile["profile_id"]
    assert output["discovered_count"] == 1
    assert output["reviewed_count"] == 1
    assert output["skipped_count"] == 0


def test_review_refresh_cli_rejects_text_source_run_without_target(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_refresh_run(profile)

    with pytest.raises(SystemExit):
        review_refresh.main(
            [
                "--topic",
                profile["profile_id"],
                "--dry-run",
                "--use-full-text",
                "--text-source-run-id",
                "run-source-only",
            ]
        )


def test_review_refresh_cli_dry_run_scopes_text_source_run_to_target(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    source_run = _create_review_refresh_run_with_text_artifact(
        profile,
        paper_id="2605.scoped-targetv1",
        title="Scoped source run artifact",
        abstract="Source run artifact for scoped review testing.",
        include_text_artifact=True,
    )
    _create_review_refresh_run_with_text_artifact(
        profile,
        paper_id="2605.scoped-targetv1",
        title="Scoped target run paper",
        abstract="Target paper for scoped source run selection.",
        include_text_artifact=False,
    )

    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--unique-limit",
            "1",
            "--use-full-text",
            "--text-source-run-id",
            source_run["run_id"],
            "--target-paper-id",
            "2605.scoped-targetv1",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["dry_run"] is True
    assert output["text_source_run_id"] == source_run["run_id"]
    assert output["prioritization"]["summary"]["target_status"] == "target_found"
    assert output["full_text_reviewed_count"] == 1
    assert output["reviewed_from_text_count"] == 1
    assert output["prioritization"]["summary"]["target_paper_id"] == "2605.scoped-targetv1"


def test_review_refresh_cli_supports_dry_run_unique_limit_and_min_relevance(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_refresh_run(profile)
    _create_refresh_run(profile)

    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--unique-limit",
            "1",
            "--min-relevance",
            "60",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    stored = paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=20)

    assert exit_code == 0
    assert output["dry_run"] is True
    assert output["unique_discovered_count"] == 1
    assert output["raw_discovered_count"] == 4
    assert stored == []


def test_review_refresh_cli_plans_domain_limited_pdf_acquisition(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.55555v1",
                "title": "Retrieval Augmented Large Language Model Agents",
                "abstract": "Large language model agents use retrieval augmented generation.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.55555v1.pdf",
            }
        ],
    )
    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--unique-limit",
            "1",
            "--fetch-missing-pdf",
            "--download-limit",
            "1",
            "--provider",
            "arxiv",
            "--allow-domain",
            "arxiv.org",
            "--extract-after-fetch",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["fetch_missing_pdf"] is True
    assert output["pdf_acquisition"]["mode"] == "plan_only"
    assert output["pdf_acquisition"]["planned_count"] == 1
    assert output["pdf_acquisition"]["downloaded_count"] == 0
    assert output["pdf_planned_count"] == 1
    assert output["pdf_downloaded_count"] == 0
    assert output["pdf_acquisition"]["items"][0]["source_url"] == "https://arxiv.org/<redacted>"
    assert not (paths.raw_dir / "2605.55555v1.pdf").exists()


def test_review_refresh_cli_targets_and_excludes_pdf_acquisition_candidates(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    _seed_candidate_control_rows(profile, run)

    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--unique-limit",
            "3",
            "--fetch-missing-pdf",
            "--download-limit",
            "1",
            "--provider",
            "arxiv",
            "--allow-domain",
            "arxiv.org",
            "--extract-after-fetch",
            "--exclude-paper-id",
            "2605.03205v1",
            "--target-paper-id",
            "2604.26498v1",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    candidates = output["prioritization"]["candidates"]
    target = next(candidate for candidate in candidates if candidate["paper_id"] == "2604.26498v1")
    excluded = next(candidate for candidate in candidates if candidate["paper_id"] == "2605.03205v1")

    assert exit_code == 0
    assert output["dry_run"] is True
    assert output["pdf_planned_count"] == 1
    assert output["pdf_acquisition"]["items"][0]["paper_id"] == "2604.26498v1"
    assert output["pdf_acquisition"]["items"][0]["source_url"] == "https://arxiv.org/<redacted>"
    assert target["is_acquisition_candidate"] is True
    assert target["rank"] == 1
    assert excluded["is_acquisition_candidate"] is False
    assert excluded["skip_reason"] == "excluded_paper_id"
    assert output["prioritization"]["summary"]["excluded_count"] == 1
    assert output["prioritization"]["summary"]["target_status"] == "target_found"
    assert paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=10) == []


def test_review_refresh_cli_requires_download_limit_for_real_fetch() -> None:
    with pytest.raises(SystemExit):
        review_refresh.main(
            [
                "--topic",
                "large-language-models",
                "--use-full-text",
                "--fetch-missing-pdf",
            ]
        )


def test_review_refresh_blocks_live_pdf_fetch_without_operator_gate(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.blockedv1",
                "title": "Blocked Live PDF Fetch for Large Language Model Agents",
                "abstract": "Large language model agents use retrieval augmented generation.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.blockedv1",
            }
        ],
    )

    def fail_urlopen(_request, *, timeout):
        raise AssertionError("live PDF fetch must be blocked before urlopen")

    install_pdf_urlopen_mock(monkeypatch, fail_urlopen)

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
    )

    assert result["pdf_acquisition"]["mode"] == "live_fetch_blocked"
    assert result["pdf_acquisition"]["status"] == "live_fetch_blocked"
    assert result["pdf_acquisition"]["live_fetch_allowed"] is False
    assert result["pdf_acquisition"]["live_fetch_blocked_count"] == 1
    assert "operator_flag_required" in result["pdf_acquisition"]["live_fetch_gate"]["reason_codes"]
    assert result["pdf_downloaded_count"] == 0
    assert not (paths.raw_dir / "2605.blockedv1.pdf").exists()


def test_review_refresh_live_pdf_fetch_requires_flag_and_env(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.gatedv1",
                "title": "Gated Live PDF Fetch for Large Language Model Agents",
                "abstract": "Large language model agents use retrieval augmented generation.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.gatedv1",
            }
        ],
    )
    calls = 0

    def fake_urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        return _FakePdfResponse(b"%PDF-1.7\nmock gated pdf\n%%EOF")

    install_pdf_urlopen_mock(monkeypatch, fake_urlopen)
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [{"page_number": 1, "text": _long_extracted_text("gated fetch")}],
    )

    flag_only = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        allow_bounded_pdf_fetch=True,
        env={},
    )
    env_only = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
    )
    both = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
        allow_bounded_pdf_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
    )

    assert flag_only["pdf_acquisition"]["mode"] == "live_fetch_blocked"
    assert "env_var_required" in flag_only["pdf_acquisition"]["live_fetch_gate"]["reason_codes"]
    assert env_only["pdf_acquisition"]["mode"] == "live_fetch_blocked"
    assert "operator_flag_required" in env_only["pdf_acquisition"]["live_fetch_gate"]["reason_codes"]
    assert both["pdf_acquisition"]["live_fetch_allowed"] is True
    assert both["pdf_downloaded_count"] == 1
    assert both["full_text_reviewed_count"] == 1
    assert calls == 1
    assert (paths.raw_dir / "2605.gatedv1.pdf").exists()


def test_review_refresh_downloads_extracts_and_upgrades_abstract_review(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.77777v1",
                "title": "Retrieval Augmented Large Language Model Agents",
                "abstract": "Large language model agents use retrieval augmented generation.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.77777v1",
            }
        ],
    )
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.77777v1",
            "title": "Retrieval Augmented Large Language Model Agents",
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

    install_pdf_urlopen_mock(
        monkeypatch,
        lambda _request, *, timeout: _FakePdfResponse(b"%PDF-1.7\nmock downloaded pdf\n%%EOF"),
    )
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [
            {
                "page_number": 1,
                "text": _long_extracted_text(),
            }
        ],
    )

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
        allow_bounded_pdf_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
    )
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.77777v1")

    assert result["pdf_downloaded_count"] == 1
    assert result["pdf_extracted_count"] == 1
    assert result["full_text_reviewed_count"] == 1
    assert result["reviewed_from_pdf_extract_count"] == 1
    assert stored is not None
    assert stored["full_text_status"] == "reviewed"
    assert stored["review_source_type"] == "full_text"
    assert stored["source_text_sha256"] != "existing-abstract-hash"
    assert stored["evidence_spans"]
    assert (paths.raw_dir / "2605.77777v1.pdf").exists()
    assert (paths.raw_dir / "2605.77777v1.txt").exists()


def test_review_refresh_downloaded_pdf_without_extractable_text_is_available_not_reviewed(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.88888v1",
                "title": "Retrieval Augmented Large Language Model Evaluation",
                "abstract": "Large language model retrieval augmented generation evaluation.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.88888v1",
            }
        ],
    )

    install_pdf_urlopen_mock(
        monkeypatch,
        lambda _request, *, timeout: _FakePdfResponse(b"%PDF-1.7\nmock downloaded pdf\n%%EOF"),
    )
    monkeypatch.setattr(pdf_acquisition, "_extract_pdf_pages", lambda _path, max_text_chars=24000: [])

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
        allow_bounded_pdf_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
    )
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.88888v1")

    assert result["pdf_downloaded_count"] == 1
    assert result["pdf_extracted_count"] == 0
    assert result["pdf_extraction_failed_count"] == 1
    assert result["full_text_available_count"] == 1
    assert result["full_text_reviewed_count"] == 0
    assert stored is not None
    assert stored["full_text_status"] == "full_text_available"
    assert stored["source_text_sha256"]
    assert stored["evidence_spans"] == []
    assert (paths.raw_dir / "2605.88888v1.pdf").exists()
    assert not (paths.raw_dir / "2605.88888v1.txt").exists()


def test_review_refresh_existing_pdf_available_upgrades_after_later_extraction(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    paper_id = "2605.99999v1"
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": paper_id,
                "title": "Retrieval Augmented Large Language Model Planning",
                "abstract": "Large language model retrieval augmented generation planning.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.99999v1",
            }
        ],
    )
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    (paths.raw_dir / f"{paper_id}.pdf").write_bytes(b"%PDF-1.7\nexisting local pdf")
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": paper_id,
            "title": "Retrieval Augmented Large Language Model Planning",
            "source_provider": "arxiv",
            "abstract_text": "Existing full_text_available record.",
            "full_text_status": "full_text_available",
            "relevance_score": 90,
            "novelty_score": 60,
            "evidence_strength": 55,
            "method_summary": "PDF 가용 상태.",
            "key_findings": ["PDF는 있으나 아직 추출되지 않음."],
            "limitations": ["본문은 아직 읽지 않음."],
            "why_it_matters": "later extraction 검증.",
            "topic_fit_rationale": "LLM/RAG 주제와 일치.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "source_text_sha256": "available-hash",
            "full_text_artifact_path": str(paths.raw_dir / f"{paper_id}.pdf"),
        }
    )

    def fail_urlopen(_request, *, timeout):
        raise AssertionError("existing local PDF extraction must not download again")

    install_pdf_urlopen_mock(monkeypatch, fail_urlopen)
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [
            {"page_number": 1, "text": _long_extracted_text("RAG planning")}
        ],
    )

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
    )
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], paper_id)

    assert result["pdf_acquisition"]["mode"] == "live_fetch_blocked"
    assert result["pdf_acquisition"]["live_fetch_allowed"] is False
    assert result["pdf_downloaded_count"] == 0
    assert result["pdf_extracted_count"] == 1
    assert result["full_text_reviewed_count"] == 1
    assert result["reviewed_from_pdf_extract_count"] == 1
    assert stored is not None
    assert stored["full_text_status"] == "reviewed"
    assert stored["source_text_sha256"] != "available-hash"
    assert (paths.raw_dir / f"{paper_id}.txt").exists()


def test_review_refresh_audit_summary_counts_reasons_and_quality_failures(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.audit-short",
                "title": "Large Language Model Audit Short Extraction",
                "abstract": "Large language model retrieval augmented generation audit.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.audit-short",
            },
            {
                "paper_id": "2605.audit-missing",
                "title": "Large Language Model Audit Missing PDF",
                "abstract": "Large language model retrieval augmented generation audit.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
            },
            {
                "paper_id": "2605.audit-domain",
                "title": "Large Language Model Audit Bad Domain",
                "abstract": "Large language model retrieval augmented generation audit.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://example.com/2605.audit-domain.pdf",
            },
        ],
    )
    install_pdf_urlopen_mock(
        monkeypatch,
        lambda _request, *, timeout: _FakePdfResponse(b"%PDF-1.7\nmock downloaded pdf\n%%EOF"),
    )
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [{"page_number": 1, "text": "Too short."}],
    )

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=3,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=3,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
        allow_bounded_pdf_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
    )
    audit = result["audit_summary"]
    stored_short = paper_reviews.get_review(profile["profile_id"], run["run_id"], "2605.audit-short")

    assert audit["candidate_count"] == 3
    assert audit["missing_pdf_url_count"] == 1
    assert audit["downloaded_count"] == 1
    assert audit["rejected_by_reason"]["missing_pdf_url"] == 1
    assert audit["rejected_by_reason"]["domain_not_allowed"] == 1
    assert audit["extraction_failed_by_reason"]["weak_extracted_text"] == 1
    assert audit["full_text_available_count"] == 1
    assert audit["full_text_reviewed_count"] == 0
    assert audit["reviewed_from_pdf_extract_count"] == 0
    assert audit["weak_evidence_span_count"] == 0
    assert audit["high_failure_rate_warning"] is not None
    assert stored_short is not None
    assert stored_short["full_text_status"] == "full_text_available"
    assert not (paths.raw_dir / "2605.audit-short.txt").exists()


def test_review_refresh_dedupes_downloads_for_duplicate_paper_ids(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    first_run = _create_refresh_run(profile)
    second_run = _create_refresh_run(profile)
    for run in (first_run, second_run):
        paths = run_repository.build_run_paths(run["run_id"])
        write_jsonl(
            paths.triage_dir / "triage.jsonl",
            [
                {
                    "paper_id": "2605.dupv1",
                    "title": "Duplicate Large Language Model PDF Candidate",
                    "abstract": "Large language model retrieval augmented generation duplicate PDF.",
                    "score": 55,
                    "source_provider": "arxiv",
                    "published": "2026-05-08",
                    "pdf_url": "https://arxiv.org/pdf/2605.dupv1",
                }
            ],
        )
    calls = 0

    def fake_urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        return _FakePdfResponse(b"%PDF-1.7\nmock downloaded pdf\n%%EOF")

    install_pdf_urlopen_mock(monkeypatch, fake_urlopen)
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [{"page_number": 1, "text": _long_extracted_text("duplicate RAG")}],
    )

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=10,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
        allow_bounded_pdf_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
    )

    assert calls == 1
    assert result["raw_discovered_count"] == 2
    assert result["unique_discovered_count"] == 1
    assert result["duplicate_row_count"] == 1
    assert result["pdf_downloaded_count"] == 1
    assert result["full_text_reviewed_count"] == 1


def test_review_refresh_reuses_existing_extracted_text_without_redownload_or_reextract(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    paper_id = "2605.reusev1"
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": paper_id,
                "title": "Reusable Extracted Text for Large Language Model Agents",
                "abstract": "Large language model retrieval augmented generation extracted text reuse.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.reusev1",
            }
        ],
    )
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    (paths.raw_dir / f"{paper_id}.pdf").write_bytes(b"%PDF-1.7\nexisting local pdf")
    (paths.raw_dir / f"{paper_id}.txt").write_text(_long_extracted_text("text reuse"), encoding="utf-8")

    def fail_urlopen(_request, *, timeout):
        raise AssertionError("existing extracted text must not trigger download")

    def fail_extract(_path, max_text_chars=24000):
        raise AssertionError("existing extracted text must not be re-extracted")

    install_pdf_urlopen_mock(monkeypatch, fail_urlopen)
    monkeypatch.setattr(pdf_acquisition, "_extract_pdf_pages", fail_extract)

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=1,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=10,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
    )
    stored = paper_reviews.get_review(profile["profile_id"], run["run_id"], paper_id)

    assert result["pdf_downloaded_count"] == 0
    assert result["pdf_extracted_count"] == 0
    assert result["audit_summary"]["skipped_existing_artifact_count"] == 1
    assert result["full_text_reviewed_count"] == 1
    assert result["reviewed_from_text_count"] == 1
    assert stored is not None
    assert stored["full_text_status"] == "reviewed"


def test_prioritize_full_text_candidates_orders_eligible_and_surfaces_skip_reasons(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.reviewedv1",
                "title": "Reviewed Large Language Model Agent Paper",
                "abstract": "Large language model agent retrieval augmented generation.",
                "score": 95,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.reviewedv1",
            },
            {
                "paper_id": "2605.eligiblev1",
                "title": "Eligible RAG Agent Paper for Full Text Review",
                "abstract": "Large language model retrieval augmented generation agent evidence.",
                "score": 90,
                "source_provider": "arxiv",
                "published": "2026-05-07",
                "pdf_url": "https://arxiv.org/pdf/2605.eligiblev1",
            },
            {
                "paper_id": "2605.missingv1",
                "title": "Missing PDF URL LLM Paper",
                "abstract": "Large language model retrieval augmented generation.",
                "score": 88,
                "source_provider": "arxiv",
                "published": "2026-05-06",
            },
            {
                "paper_id": "2605.bad-domainv1",
                "title": "Bad Domain LLM Paper",
                "abstract": "Large language model retrieval augmented generation.",
                "score": 87,
                "source_provider": "arxiv",
                "published": "2026-05-05",
                "pdf_url": "https://example.com/2605.bad-domainv1.pdf",
                "pdf_acquisition_status": "rejected",
                "pdf_rejection_reason": "domain_not_allowed",
            },
        ],
    )
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    reviewed_text = paths.raw_dir / "2605.reviewedv1.txt"
    reviewed_text.write_text(_long_extracted_text("already reviewed"), encoding="utf-8")
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.reviewedv1",
            "title": "Reviewed Large Language Model Agent Paper",
            "source_provider": "arxiv",
            "abstract_text": "Large language model agent retrieval augmented generation.",
            "text_excerpt": _long_extracted_text("already reviewed"),
            "full_text_status": "reviewed",
            "relevance_score": 95,
            "novelty_score": 60,
            "evidence_strength": 80,
            "method_summary": "reviewed",
            "key_findings": ["reviewed"],
            "limitations": ["none"],
            "why_it_matters": "reviewed",
            "topic_fit_rationale": "reviewed",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-08T00:00:00+00:00",
            "source_text_sha256": "reviewed-hash",
            "full_text_artifact_path": str(reviewed_text),
            "text_char_count": reviewed_text.stat().st_size,
            "evidence_spans": [{"text": "reviewed evidence", "source_path": str(reviewed_text)}],
            "review_source_type": "full_text",
        }
    )

    result = paper_review.prioritize_full_text_acquisition_candidates(
        profile["profile_id"],
        unique_limit=4,
        allowed_domains=["arxiv.org"],
    )

    eligible = [candidate for candidate in result["candidates"] if candidate["is_acquisition_candidate"]]
    skipped = {candidate["paper_id"]: candidate for candidate in result["candidates"] if candidate["skip_reason"]}

    assert eligible[0]["paper_id"] == "2605.eligiblev1"
    assert eligible[0]["rank"] == 1
    assert eligible[0]["pdf_url_domain"] == "arxiv.org"
    assert skipped["2605.reviewedv1"]["skip_reason"] == "already_full_text_reviewed"
    assert skipped["2605.missingv1"]["skip_reason"] == "missing_pdf_url"
    assert skipped["2605.bad-domainv1"]["skip_reason"] == "domain_not_allowed"
    assert skipped["2605.bad-domainv1"]["previous_acquisition_status"] == "rejected:domain_not_allowed"
    assert result["summary"]["candidate_count"] == 4
    assert result["summary"]["acquisition_candidate_count"] == 1
    assert result["summary"]["skip_reason_counts"]["already_full_text_reviewed"] == 1
    assert result["summary"]["skip_reason_counts"]["missing_pdf_url"] == 1


def test_prioritize_full_text_candidates_targeting_preserves_default_behavior(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    _seed_candidate_control_rows(profile, run)

    default = paper_review.prioritize_full_text_acquisition_candidates(
        profile["profile_id"],
        unique_limit=3,
        allowed_domains=["arxiv.org"],
        download_limit=1,
    )
    targeted = paper_review.prioritize_full_text_acquisition_candidates(
        profile["profile_id"],
        unique_limit=3,
        allowed_domains=["arxiv.org"],
        download_limit=1,
        exclude_paper_ids=["2605.03205v1"],
        target_paper_id="2604.26498v1",
    )

    default_eligible = [candidate for candidate in default["candidates"] if candidate["is_acquisition_candidate"]]
    targeted_eligible = [candidate for candidate in targeted["candidates"] if candidate["is_acquisition_candidate"]]
    excluded = next(candidate for candidate in targeted["candidates"] if candidate["paper_id"] == "2605.03205v1")
    other = next(candidate for candidate in targeted["candidates"] if candidate["paper_id"] == "2604.other-v1")

    assert default_eligible[0]["paper_id"] == "2605.03205v1"
    assert len(targeted_eligible) == 1
    assert targeted_eligible[0]["paper_id"] == "2604.26498v1"
    assert targeted_eligible[0]["rank"] == 1
    assert targeted_eligible[0]["planned_action"] == "download_pdf"
    assert excluded["skip_reason"] == "excluded_paper_id"
    assert other["skip_reason"] == "target_paper_id_mismatch"
    assert targeted["summary"]["acquisition_candidate_count"] == 1
    assert targeted["summary"]["planned_download_count"] == 1
    assert targeted["summary"]["excluded_count"] == 1
    assert targeted["summary"]["target_status"] == "target_found"


def test_prioritize_full_text_candidates_target_missing_reports_no_candidate_found(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    _seed_candidate_control_rows(profile, run)

    result = paper_review.prioritize_full_text_acquisition_candidates(
        profile["profile_id"],
        unique_limit=3,
        allowed_domains=["arxiv.org"],
        download_limit=1,
        target_paper_id="2604.missingv1",
    )

    assert [candidate for candidate in result["candidates"] if candidate["is_acquisition_candidate"]] == []
    assert result["summary"]["acquisition_candidate_count"] == 0
    assert result["summary"]["planned_download_count"] == 0
    assert result["summary"]["target_status"] == "no_candidate_found"


def test_review_refresh_live_pdf_fetch_never_attempts_excluded_candidate(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    _seed_candidate_control_rows(profile, run)
    requested_urls: list[str] = []

    def fake_urlopen(request, *, timeout):
        requested_urls.append(request.full_url)
        return _FakePdfResponse(b"%PDF-1.7\nmock targeted pdf\n%%EOF")

    install_pdf_urlopen_mock(monkeypatch, fake_urlopen)
    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [{"page_number": 1, "text": _long_extracted_text("targeted drug discovery")}],
    )

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        unique_limit=3,
        use_full_text=True,
        fetch_missing_pdf=True,
        download_limit=1,
        allow_domains=["arxiv.org"],
        extract_after_fetch=True,
        max_pdf_bytes=1024,
        allow_bounded_pdf_fetch=True,
        env={"ALLOW_BOUNDED_PDF_FETCH": "1"},
        exclude_paper_ids=["2605.03205v1"],
        target_paper_id="2604.26498v1",
    )

    assert requested_urls == ["https://arxiv.org/pdf/2604.26498v1"]
    assert result["pdf_downloaded_count"] == 1
    assert result["pdf_acquisition"]["items"][0]["paper_id"] == "2604.26498v1"
    assert all(item.get("paper_id") != "2605.03205v1" for item in result["pdf_acquisition"]["items"])
    assert result["prioritization"]["summary"]["skip_reason_counts"]["excluded_paper_id"] == 1
    assert result["full_text_reviewed_count"] == 1
    assert paper_reviews.get_review(profile["profile_id"], run["run_id"], "2604.other-v1") is None


def test_review_refresh_dry_run_candidate_preview_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.previewv1",
                "title": "Preview Only RAG Agent Paper",
                "abstract": "Large language model retrieval augmented generation preview.",
                "score": 90,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/2605.previewv1",
            }
        ],
    )

    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--unique-limit",
            "1",
            "--use-full-text",
            "--fetch-missing-pdf",
            "--download-limit",
            "1",
            "--allow-domain",
            "arxiv.org",
            "--extract-after-fetch",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["prioritization"]["summary"]["acquisition_candidate_count"] == 1
    assert output["prioritization"]["candidates"][0]["paper_id"] == "2605.previewv1"
    assert output["prioritization"]["candidates"][0]["rank"] == 1
    assert output["pdf_planned_count"] == 1
    assert not (paths.raw_dir / "2605.previewv1.pdf").exists()
    assert paper_reviews.list_reviews(topic_id=profile["profile_id"], limit=10) == []


def test_evidence_store_validation_detects_broken_reviewed_states_and_plans_repairs(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    valid_text = paths.raw_dir / "2605.validv1.txt"
    valid_content = _long_extracted_text("valid evidence")
    valid_text.write_text(valid_content, encoding="utf-8")
    valid_hash = paper_review._sha256_text(paper_review._normalize_text(valid_content))
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.validv1",
            "title": "Valid Full Text Review",
            "source_provider": "arxiv",
            "abstract_text": "valid",
            "text_excerpt": valid_content,
            "full_text_status": "reviewed",
            "relevance_score": 90,
            "novelty_score": 60,
            "evidence_strength": 80,
            "method_summary": "valid",
            "key_findings": ["valid"],
            "limitations": ["valid"],
            "why_it_matters": "valid",
            "topic_fit_rationale": "valid",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-08T00:00:00+00:00",
            "source_text_sha256": valid_hash,
            "full_text_artifact_path": str(valid_text),
            "text_char_count": len(paper_review._normalize_text(valid_content)),
            "evidence_spans": [{"text": "valid evidence span", "source_path": str(valid_text)}],
            "review_source_type": "full_text",
        }
    )
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.brokenv1",
            "title": "Broken Full Text Review",
            "source_provider": "arxiv",
            "abstract_text": "broken",
            "full_text_status": "reviewed",
            "relevance_score": 80,
            "novelty_score": 60,
            "evidence_strength": 80,
            "method_summary": "broken",
            "key_findings": ["broken"],
            "limitations": ["broken"],
            "why_it_matters": "broken",
            "topic_fit_rationale": "broken",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-08T00:00:00+00:00",
            "full_text_artifact_path": str(paths.raw_dir / "missing.txt"),
            "evidence_spans": [],
            "review_source_type": "full_text",
        }
    )
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.stalev1",
            "title": "Stale Available Full Text Review",
            "source_provider": "arxiv",
            "abstract_text": "stale",
            "full_text_status": "full_text_available",
            "relevance_score": 70,
            "novelty_score": 55,
            "evidence_strength": 55,
            "method_summary": "stale",
            "key_findings": ["stale"],
            "limitations": ["stale"],
            "why_it_matters": "stale",
            "topic_fit_rationale": "stale",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-08T00:00:00+00:00",
            "full_text_artifact_path": str(paths.raw_dir / "missing.pdf"),
        }
    )

    validation = paper_review.validate_evidence_store_for_topic(profile["profile_id"], include_repair_plan=True)

    assert validation["valid_count"] == 1
    assert validation["error_count"] >= 2
    assert validation["warning_count"] >= 1
    assert validation["reason_counts"]["missing_artifact_file"] == 1
    assert validation["reason_counts"]["reviewed_missing_source_text_sha256"] == 1
    assert validation["reason_counts"]["reviewed_missing_evidence_spans"] == 1
    assert validation["reason_counts"]["stale_full_text_available"] == 1
    assert "2605.brokenv1" in validation["affected_paper_ids"]
    assert validation["repair_plan"]["mode"] == "plan_only"
    assert any(action["action"] == "re_extract_or_refresh_review" for action in validation["repair_plan"]["actions"])


def test_review_refresh_cli_validate_evidence_store_is_report_only(tmp_path, monkeypatch, capsys) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    missing_path = run_repository.build_run_paths(run["run_id"]).raw_dir / "missing.txt"
    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.cli-brokenv1",
            "title": "CLI Broken Full Text Review",
            "source_provider": "arxiv",
            "abstract_text": "broken",
            "full_text_status": "reviewed",
            "relevance_score": 80,
            "novelty_score": 60,
            "evidence_strength": 80,
            "method_summary": "broken",
            "key_findings": ["broken"],
            "limitations": ["broken"],
            "why_it_matters": "broken",
            "topic_fit_rationale": "broken",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "reviewed_at": "2026-05-08T00:00:00+00:00",
            "full_text_artifact_path": str(missing_path),
            "evidence_spans": [],
            "review_source_type": "full_text",
        }
    )

    before_validation_hash = _file_sha256(bootstrap.metadata_index_path())
    exit_code = review_refresh.main(
        [
            "--topic",
            profile["profile_id"],
            "--dry-run",
            "--use-full-text",
            "--validate-evidence-store",
            "--repair-plan",
        ]
    )
    after_validation_hash = _file_sha256(bootstrap.metadata_index_path())
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert after_validation_hash == before_validation_hash
    assert output["mode"] == "evidence_store_validation"
    assert output["dry_run"] is True
    assert output["evidence_store_validation"]["error_count"] >= 1
    assert output["evidence_store_validation"]["repair_plan"]["mode"] == "plan_only"
    assert not missing_path.exists()


def test_pdf_acquisition_plan_rejects_disallowed_domains(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.66666v1",
                "title": "Large Language Model Agents with External PDF",
                "abstract": "Large language model agents use retrieval augmented generation.",
                "score": 55,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://example.com/2605.66666.pdf",
            }
        ],
    )

    result = paper_review.refresh_reviews_for_topic(
        profile["profile_id"],
        dry_run=True,
        unique_limit=1,
        fetch_missing_pdf=True,
        download_limit=1,
        provider="arxiv",
        allow_domains=["arxiv.org"],
    )

    assert result["pdf_acquisition"]["planned_count"] == 0
    assert result["pdf_acquisition"]["rejected_count"] == 1
    assert result["pdf_acquisition"]["rejected"][0]["reason"] == "domain_not_allowed"
    assert not (paths.raw_dir / "2605.66666v1.pdf").exists()


def test_review_refresh_api_returns_summary_counts(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_refresh_run(profile)

    response = client.post(f"/api/topics/{profile['profile_id']}/reviews/refresh", json={"limit": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["topic_id"] == profile["profile_id"]
    assert body["discovered_count"] == 1
    assert body["reviewed_count"] == 1
    assert body["skipped_count"] == 0


def test_review_refresh_api_blocks_dashboard_pdf_fetch(client, isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    analytics_dir = isolated_run_repo["storage_root"].parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", isolated_run_repo["storage_root"].parent.parent)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_refresh_run(profile)
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "2605.api-blockedv1",
                "title": "Dashboard API Must Not Fetch PDF",
                "abstract": "Large language model retrieval augmented generation dashboard safety.",
                "score": 92,
                "source_provider": "arxiv",
                "published": "2026-05-09",
                "pdf_url": "https://arxiv.org/pdf/2605.api-blockedv1",
            }
        ],
    )

    def fail_urlopen(_request, *, timeout):
        raise AssertionError("dashboard API must not trigger live PDF fetch")

    install_pdf_urlopen_mock(monkeypatch, fail_urlopen)

    response = client.post(
        f"/api/topics/{profile['profile_id']}/reviews/refresh",
        json={
            "unique_limit": 1,
            "use_full_text": True,
            "fetch_missing_pdf": True,
            "download_limit": 1,
            "allow_domains": ["arxiv.org"],
            "extract_after_fetch": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pdf_acquisition"]["mode"] == "live_fetch_blocked"
    assert body["pdf_acquisition"]["live_fetch_allowed"] is False
    assert body["pdf_downloaded_count"] == 0
    assert not (paths.raw_dir / "2605.api-blockedv1.pdf").exists()


def test_weekly_report_can_be_generated_from_refreshed_topic_reviews(tmp_path, monkeypatch) -> None:
    _patch_local_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    _create_refresh_run(profile)
    summary = paper_review.refresh_reviews_for_topic(profile["profile_id"], limit=2, force=True)

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile=profile,
        topic_id=profile["profile_id"],
        generated_at="2026-05-07T00:00:00+00:00",
    )
    markdown = result["markdown"]

    assert summary["reviewed_count"] == 2
    assert result["status"] == "ok"
    assert result["review_count"] == 2
    assert "Executive Summary" in markdown
    assert "이번 주 핵심 연구 흐름" in markdown
    assert "Evidence Sufficiency" in markdown
    assert "주목할 논문 2/5" in markdown
    assert "방법론/데이터셋/평가 동향" in markdown
    assert "한계와 리스크" in markdown
    assert "다음 주 모니터링 포인트" in markdown
    assert "참고 논문 / evidence table" in markdown
    assert "full_text_status" in markdown
    assert "abstract_only" in markdown
    assert "full-text를 검토했다고 가정하지 않습니다" in markdown

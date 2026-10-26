from __future__ import annotations

import json
import hashlib

from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews, runs as run_repository
from app.repositories.files import write_jsonl
from app.services import paper_review


def _file_sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_payload(**overrides):
    payload = {
        "topic_id": "large-language-models",
        "run_id": "run-review-1",
        "paper_id": "paper-review-1",
        "title": "Retrieval-Augmented Language Models for Scientific Review",
        "source_provider": "arxiv",
        "abstract_text": "This paper studies retrieval augmented generation for scientific review workflows.",
        "text_excerpt": "",
        "full_text_status": "abstract_only",
        "relevance_score": 88.0,
        "novelty_score": 62.0,
        "evidence_strength": 41.0,
        "method_summary": "초록 기준으로 RAG 기반 과학 문헌 검토 파이프라인을 요약했다.",
        "key_findings": ["RAG가 근거 추적을 개선한다.", "도메인별 평가가 필요하다."],
        "limitations": ["본문/PDF는 아직 처리되지 않았다."],
        "why_it_matters": "LLM 기반 연구 에이전트의 근거 연결 품질을 높이는 방향이다.",
        "topic_fit_rationale": "LLM, retrieval augmented generation, scientific review 키워드가 주제 프로필과 일치한다.",
        "review_model": "deterministic-baseline-v1",
        "reviewer": "deterministic-baseline",
        "reviewed_at": "2026-05-07T00:00:00+00:00",
        "source_text_sha256": "abc123",
    }
    payload.update(overrides)
    return payload


def test_paper_review_upsert_is_idempotent_and_queryable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")

    first = paper_reviews.upsert_review(_review_payload())
    second = paper_reviews.upsert_review(
        _review_payload(
            title="Updated Retrieval-Augmented Language Models for Scientific Review",
            relevance_score=91.5,
            key_findings=["업데이트된 핵심 발견"],
            source_text_sha256="def456",
        )
    )

    stored = paper_reviews.get_review("large-language-models", "run-review-1", "paper-review-1")
    listed = paper_reviews.list_reviews(topic_id="large-language-models", run_id="run-review-1")
    coverage = paper_reviews.get_review_coverage("large-language-models", total_papers=3)

    assert first["paper_id"] == "paper-review-1"
    assert second["title"].startswith("Updated")
    assert stored is not None
    assert stored["relevance_score"] == 91.5
    assert stored["key_findings"] == ["업데이트된 핵심 발견"]
    assert len(listed) == 1
    assert coverage == {
        "total_papers": 3,
        "reviewed_count": 1,
        "abstract_only_count": 1,
        "full_text_reviewed_count": 0,
        "coverage_percentage": 33.3,
    }


def test_artifact_reference_resolves_portable_and_legacy_paths(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    artifact = storage_root / "portable-run" / "raw" / "portable-paper.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Portable full text evidence.", encoding="utf-8")

    repo_relative = artifact_refs.resolve_artifact_reference(
        "storage/runs/portable-run/raw/portable-paper.txt",
        run_id="portable-run",
    )
    storage_relative = artifact_refs.resolve_artifact_reference(
        "storage:runs/portable-run/raw/portable-paper.txt",
        run_id="portable-run",
    )
    legacy_windows = artifact_refs.resolve_artifact_reference(
        r"C:\old\checkout\storage\runs\portable-run\raw\portable-paper.txt",
        run_id="portable-run",
    )
    legacy_posix = artifact_refs.resolve_artifact_reference(
        "/old/checkout/storage/runs/portable-run/raw/portable-paper.txt",
        run_id="portable-run",
    )

    assert repo_relative.exists is True
    assert repo_relative.portable_ref == "run:raw/portable-paper.txt"
    assert storage_relative.exists is True
    assert storage_relative.portable_ref == "run:raw/portable-paper.txt"
    assert legacy_windows.exists is True
    assert legacy_windows.was_legacy_absolute is True
    assert legacy_windows.resolution_strategy == "legacy_storage_suffix"
    assert legacy_windows.display_ref == "run:raw/portable-paper.txt"
    assert legacy_posix.exists is True
    assert legacy_posix.was_legacy_absolute is True
    assert legacy_posix.display_ref == "run:raw/portable-paper.txt"


def test_artifact_reference_rejects_traversal_and_unsafe_absolute_paths(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)

    traversal = artifact_refs.resolve_artifact_reference("storage:runs/../secrets.txt", run_id="portable-run")
    unsafe_absolute = artifact_refs.resolve_artifact_reference(r"C:\Users\alice\secret.txt", run_id="portable-run")
    missing = artifact_refs.resolve_artifact_reference(
        r"C:\old\checkout\storage\runs\portable-run\raw\missing-paper.txt",
        run_id="portable-run",
    )

    assert traversal.exists is False
    assert traversal.is_safe is False
    assert traversal.reason == "unsafe_artifact_reference"
    assert unsafe_absolute.exists is False
    assert unsafe_absolute.is_safe is False
    assert "alice" not in unsafe_absolute.display_ref
    assert missing.exists is False
    assert missing.is_safe is True
    assert missing.reason == "missing_artifact"
    assert missing.display_ref == "run:raw/missing-paper.txt"


def test_reviewed_upsert_stores_portable_artifact_reference_without_changing_evidence(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    artifact = storage_root / "portable-write-run" / "raw" / "paper-review-1.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Stored full text evidence.", encoding="utf-8")
    spans = [{"label": "span-1", "text": "Stored full text evidence.", "source_path": str(artifact)}]

    stored = paper_reviews.upsert_review(
        _review_payload(
            run_id="portable-write-run",
            full_text_status="reviewed",
            full_text_artifact_path=str(artifact),
            source_text_sha256="reviewed-hash",
            evidence_spans=spans,
            review_source_type="full_text",
            text_char_count=26,
        )
    )

    assert stored["full_text_status"] == "reviewed"
    assert stored["full_text_artifact_path"] == "run:raw/paper-review-1.txt"
    assert stored["source_text_sha256"] == "reviewed-hash"
    assert stored["evidence_spans"] == spans


def test_artifact_refs_cli_dry_run_and_apply_normalizes_only_artifact_path(tmp_path, monkeypatch, capsys) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    artifact = storage_root / "cli-run" / "raw" / "paper-review-1.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact_text = " ".join(["CLI normalization keeps review evidence unchanged and long enough for validation."] * 20)
    artifact.write_text(artifact_text, encoding="utf-8")
    artifact_hash = paper_review._sha256_text(artifact_text)
    spans = [{"label": "span-1", "text": "CLI normalization keeps review evidence unchanged."}]
    paper_reviews.upsert_review(
        _review_payload(
            run_id="cli-run",
            full_text_status="reviewed",
            full_text_artifact_path="run:raw/paper-review-1.txt",
            source_text_sha256=artifact_hash,
            evidence_spans=spans,
            review_source_type="full_text",
            text_char_count=len(artifact_text),
        )
    )
    legacy_absolute = r"C:\old\checkout\storage\runs\cli-run\raw\paper-review-1.txt"
    with bootstrap.connect_metadata_index() as connection:
        connection.execute(
            "UPDATE paper_reviews SET full_text_artifact_path = ? WHERE topic_id = ? AND run_id = ? AND paper_id = ?",
            (legacy_absolute, "large-language-models", "cli-run", "paper-review-1"),
        )

    from app.cli import artifact_refs as artifact_refs_cli

    before_dry_hash = _file_sha256(bootstrap.metadata_index_path())
    dry_exit = artifact_refs_cli.main(["--topic", "large-language-models", "--dry-run"])
    after_dry_hash = _file_sha256(bootstrap.metadata_index_path())
    dry_output = json.loads(capsys.readouterr().out)
    after_dry = paper_reviews.get_review("large-language-models", "cli-run", "paper-review-1")
    apply_exit = artifact_refs_cli.main(["--topic", "large-language-models", "--apply", "--backup-first"])
    apply_output = json.loads(capsys.readouterr().out)
    after_apply = paper_reviews.get_review("large-language-models", "cli-run", "paper-review-1")

    assert dry_exit == 0
    assert after_dry_hash == before_dry_hash
    assert dry_output["dry_run"] is True
    assert dry_output["legacy_absolute_count"] == 1
    assert dry_output["would_update_count"] == 1
    assert dry_output["updated_count"] == 0
    assert dry_output["status_counts"]["convertible_legacy_absolute"] == 1
    assert after_dry is not None
    assert after_dry["full_text_artifact_path"] == legacy_absolute
    assert apply_exit == 0
    assert apply_output["dry_run"] is False
    assert apply_output["updated_count"] == 1
    assert apply_output["backup_path"]
    assert after_apply is not None
    assert after_apply["full_text_status"] == "reviewed"
    assert after_apply["full_text_artifact_path"] == "run:raw/paper-review-1.txt"
    assert after_apply["source_text_sha256"] == artifact_hash
    assert after_apply["evidence_spans"] == spans


def test_deterministic_review_refresh_creates_honest_abstract_only_records(isolated_run_repo, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", isolated_run_repo["storage_root"])

    run = run_repository.create_run_record(
        topic="Large Language Models",
        request_payload={
            "topic": "Large Language Models",
            "queries": ["large language model", "retrieval augmented generation"],
            "categories": ["cs.CL", "cs.AI"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        },
        source="test:paper-review",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "llm-review-1",
                "title": "Trustworthy Large Language Model Agents with Retrieval",
                "abstract": "We study large language model agents that use retrieval augmented generation for evidence-backed answers.",
                "score": 42,
                "source_provider": "arxiv",
                "published": "2026-05-01",
            }
        ],
    )
    profile = {
        "profile_id": "large-language-models",
        "title": "Large Language Models",
        "topic": "Large Language Models",
        "queries": ["large language model", "retrieval augmented generation"],
        "categories": ["cs.CL", "cs.AI"],
    }

    first = paper_review.refresh_reviews_for_run(run["run_id"], topic_profile=profile)
    second = paper_review.refresh_reviews_for_run(run["run_id"], topic_profile=profile)
    stored = paper_reviews.list_reviews(topic_id="large-language-models", run_id=run["run_id"])

    assert first["upserted"] == 1
    assert second["upserted"] == 1
    assert len(stored) == 1
    assert stored[0]["full_text_status"] == "abstract_only"
    assert "본문" not in stored[0]["method_summary"]
    assert stored[0]["source_text_sha256"]
    assert stored[0]["key_findings"]
    assert stored[0]["why_it_matters"]

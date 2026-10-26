from __future__ import annotations

import json

from app.db import bootstrap
from app.repositories import runs as run_repository
from app.repositories.files import write_jsonl
from app.services import full_text_inventory, paper_review, topic_analytics, topic_profiles
from app.cli import full_text_inventory as full_text_inventory_cli


def _patch_inventory_storage(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    profile_dir = storage_root.parent / "topic_profiles"
    analytics_dir = storage_root.parent / "topic_analytics"
    legacy_text_root = tmp_path / "data" / "papers" / "text"
    legacy_pdf_root = tmp_path / "data" / "papers" / "pdf"
    legacy_notes_root = tmp_path / "notes"

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(full_text_inventory.constants, "LEGACY_TEXT_ROOT", legacy_text_root)
    monkeypatch.setattr(full_text_inventory.constants, "LEGACY_PDF_ROOT", legacy_pdf_root)
    monkeypatch.setattr(full_text_inventory.constants, "LEGACY_NOTES_ROOT", legacy_notes_root)


def _create_inventory_run(profile: dict, *, paper_id: str, title: str, abstract: str) -> dict:
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
        source="test:full-text-inventory",
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
                "score": 54,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": f"https://arxiv.org/pdf/{paper_review.normalize_paper_id(paper_id)}.pdf",
            }
        ],
    )
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    return run_repository.save_run_record(run)


def test_inventory_scans_artifacts_and_normalizes_paper_ids(tmp_path, monkeypatch) -> None:
    _patch_inventory_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_inventory_run(
        profile,
        paper_id="2605.12345v1",
        title="Large Language Model Agents with Retrieval Evidence",
        abstract="Large language model agents use retrieval augmented generation for grounded answers.",
    )
    paths = run_repository.build_run_paths(run["run_id"])
    (paths.raw_dir / "2605.12345v1.txt").write_text("Local full text for a RAG agent paper.", encoding="utf-8")
    (paths.raw_dir / "2605.12345v1.pdf").write_bytes(b"%PDF local")
    full_text_inventory.constants.LEGACY_TEXT_ROOT.mkdir(parents=True, exist_ok=True)
    (full_text_inventory.constants.LEGACY_TEXT_ROOT / "2605.54321v2.pages.json").write_text(
        json.dumps({"pages": [{"page": 1, "text": "Legacy page text"}]}),
        encoding="utf-8",
    )

    result = full_text_inventory.inventory_full_text_artifacts(topic_id=profile["profile_id"], limit=10)
    by_id = {item["normalized_paper_id"]: item for item in result["items"]}

    assert "2605.12345" in by_id
    assert "2605.54321" in by_id
    assert by_id["2605.12345"]["paper_id"] == "2605.12345v1"
    assert set(by_id["2605.12345"]["artifact_types"]) == {"pdf", "text_txt"}
    assert by_id["2605.12345"]["text_char_count"] > 0
    assert by_id["2605.12345"]["paper_review_exists"] is False


def test_inventory_joins_metadata_and_ranks_by_topic_relevance(tmp_path, monkeypatch) -> None:
    _patch_inventory_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    llm_run = _create_inventory_run(
        profile,
        paper_id="2605.11111v1",
        title="Retrieval Augmented Generation for Large Language Model Agents",
        abstract="RAG improves reasoning and tool use in large language model agents.",
    )
    xr_run = _create_inventory_run(
        profile,
        paper_id="2605.22222v1",
        title="Gaze Prediction for Virtual Reality Headsets",
        abstract="Virtual reality gaze tracking uses pose, AR, VR, and HMD signals.",
    )
    (run_repository.build_run_paths(llm_run["run_id"]).raw_dir / "2605.11111v1.txt").write_text(
        "LLM RAG agent full text.",
        encoding="utf-8",
    )
    (run_repository.build_run_paths(xr_run["run_id"]).raw_dir / "2605.22222v1.txt").write_text(
        "XR gaze full text.",
        encoding="utf-8",
    )

    result = full_text_inventory.inventory_full_text_artifacts(topic_id=profile["profile_id"], only_with_text=True)

    assert [item["normalized_paper_id"] for item in result["items"]][:2] == ["2605.11111", "2605.22222"]
    assert result["items"][0]["title"] == "Retrieval Augmented Generation for Large Language Model Agents"
    assert result["items"][0]["topic_relevance_scores"][profile["profile_id"]] > result["items"][1]["topic_relevance_scores"][profile["profile_id"]]
    assert result["items"][0]["best_matching_topic"] == profile["profile_id"]


def test_inventory_reports_existing_review_status(tmp_path, monkeypatch) -> None:
    _patch_inventory_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_inventory_run(
        profile,
        paper_id="2605.33333v1",
        title="Large Language Model Review Coverage",
        abstract="Large language model evidence review coverage.",
    )
    (run_repository.build_run_paths(run["run_id"]).raw_dir / "2605.33333v1.txt").write_text(
        "Review coverage full text.",
        encoding="utf-8",
    )
    from app.repositories import paper_reviews

    paper_reviews.upsert_review(
        {
            "topic_id": profile["profile_id"],
            "run_id": run["run_id"],
            "paper_id": "2605.33333v1",
            "title": "Large Language Model Review Coverage",
            "source_provider": "arxiv",
            "abstract_text": "Existing abstract review.",
            "full_text_status": "abstract_only",
            "relevance_score": 90,
            "novelty_score": 60,
            "evidence_strength": 45,
            "method_summary": "초록 기반 리뷰.",
            "key_findings": ["기존 리뷰."],
            "limitations": ["전문 미검토."],
            "why_it_matters": "inventory status 검증.",
            "topic_fit_rationale": "LLM 주제와 일치.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "deterministic-baseline",
            "source_text_sha256": "abstract-hash",
        }
    )

    result = full_text_inventory.inventory_full_text_artifacts(topic_id=profile["profile_id"], limit=1)

    assert result["items"][0]["paper_review_exists"] is True
    assert result["items"][0]["current_full_text_status"] == "abstract_only"


def test_inventory_cli_emits_json_shape(tmp_path, monkeypatch, capsys) -> None:
    _patch_inventory_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = _create_inventory_run(
        profile,
        paper_id="2605.44444v1",
        title="Large Language Model JSON Inventory",
        abstract="Large language model inventory JSON output.",
    )
    (run_repository.build_run_paths(run["run_id"]).raw_dir / "2605.44444v1.txt").write_text(
        "Inventory JSON text.",
        encoding="utf-8",
    )

    exit_code = full_text_inventory_cli.main(["--topic", profile["profile_id"], "--limit", "5", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["topic_id"] == profile["profile_id"]
    assert output["total_artifact_candidates"] == 1
    assert output["items"][0]["artifact_paths"]["text_txt"]
    assert output["items"][0]["topic_relevance_scores"][profile["profile_id"]] > 0

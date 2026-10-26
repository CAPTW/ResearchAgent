from __future__ import annotations

from app.db import bootstrap
from app.repositories import runs as run_repository
from app.repositories.files import write_jsonl
from app.services import paper_review, topic_analytics, topic_profiles


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


def _create_run(profile: dict, run_label: str, items: list[dict]) -> dict:
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
        source=f"test:unique-consolidation:{run_label}",
        write_legacy=False,
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run["steps"] = [{"name": "summary", "status": "completed"}]
    run["created_at"] = f"2026-05-0{run_label}T00:00:00+00:00"
    run["updated_at"] = run["created_at"]
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(paths.triage_dir / "triage.jsonl", items)
    return run_repository.save_run_record(run)


def _duplicate_items() -> list[list[dict]]:
    short_abstract = "Large language model agents use RAG."
    complete_abstract = (
        "Large language model agents use retrieval augmented generation, tool use, "
        "alignment checks, and reasoning traces for evidence-backed research reports."
    )
    return [
        [
            {
                "paper_id": "2605.11111v1",
                "title": "Large Language Model Agents with Retrieval-Augmented Generation",
                "abstract": short_abstract,
                "source_provider": "arxiv",
                "published": "2026-05-01",
                "score": 90,
            },
            {
                "paper_id": "2605.22222v1",
                "title": "Gaze Prediction in Virtual Reality Without Eye Tracking",
                "abstract": "Virtual reality gaze, pose tracking, AR, VR, VIO, and HMD signals.",
                "source_provider": "arxiv",
                "published": "2026-05-01",
                "score": 99,
            },
        ],
        [
            {
                "paper_id": "2605.11111v1",
                "title": "Large Language Model Agents with Retrieval-Augmented Generation",
                "abstract": complete_abstract,
                "source_provider": "arxiv",
                "published": "2026-05-02",
                "score": 75,
            }
        ],
        [
            {
                "paper_id": "arXiv:2605.11111v2",
                "title": "Large Language Model Agents with Retrieval-Augmented Generation",
                "abstract": short_abstract,
                "source_provider": "arxiv",
                "published": "2026-05-03",
                "score": 70,
            }
        ],
    ]


def test_consolidates_duplicate_run_rows_into_unique_paper_candidates(tmp_path, monkeypatch) -> None:
    _patch_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    runs = [_create_run(profile, str(index + 1), items) for index, items in enumerate(_duplicate_items())]

    result = paper_review.consolidate_topic_paper_candidates(profile["profile_id"])
    candidates = {candidate["normalized_paper_id"]: candidate for candidate in result["candidates"]}
    duplicate = candidates["2605.11111"]

    assert result["raw_row_count"] == 4
    assert result["unique_candidate_count"] == 2
    assert result["duplicate_row_count"] == 2
    assert duplicate["paper_id"] == "2605.11111v1"
    assert duplicate["best_run_id"] == runs[1]["run_id"]
    assert duplicate["seen_count"] == 3
    assert duplicate["duplicate_count"] == 2
    assert duplicate["seen_run_ids"] == [runs[0]["run_id"], runs[1]["run_id"], runs[2]["run_id"]]
    assert "alignment checks" in duplicate["abstract_text"]
    assert duplicate["topic_relevance_score"] >= 60


def test_consolidation_filters_low_relevance_and_limits_unique_candidates(tmp_path, monkeypatch) -> None:
    _patch_topic_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    for index, items in enumerate(_duplicate_items()):
        _create_run(profile, str(index + 1), items)

    result = paper_review.consolidate_topic_paper_candidates(
        profile["profile_id"],
        min_relevance=60,
        limit=1,
    )

    assert result["raw_row_count"] == 4
    assert result["unique_candidate_count"] == 1
    assert result["low_relevance_unique_count"] == 1
    assert result["candidates"][0]["normalized_paper_id"] == "2605.11111"

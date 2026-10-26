from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from app.cli import evidence_manifest
from app.db import bootstrap
from app.domain import constants
from app.repositories import artifact_refs, paper_reviews, runs as run_repository
from app.services import (
    evidence_manifest_replay,
    evidence_manifest_seed,
    evidence_manifest_verifier,
    topic_analytics,
    topic_profiles,
)
from tests.research_transport_fakes import install_pdf_urlopen_mock

GENERATED_OCCURRENCE_ID = re.compile(r"^occ-[0-9a-f]{32}$")


def _manifest_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "manifest_kind": "paper_full_text_evidence_replay",
        "topic_id": "large-language-models",
        "paper_id": "2604.26498v1",
        "title": "Do Larger Models Really Win in Drug Discovery? A Benchmark Assessment of Model Scaling in AI-Driven Molecular Property and Activity Prediction",
        "pdf_url": "https://arxiv.org/pdf/2604.26498v1",
        "pdf_domain": "arxiv.org",
        "pdf_sha256": "f3f36c95584d978347d8358501bf328756e37ef187e7b4143c68fdc2ac7d1f05",
        "source_text_sha256": "306a9924fc26625edf05e89028cdb000a5a14c1ba9b796ac551ba7c149679049",
        "artifact_ref": "run:raw/2604.26498v1.txt",
        "pages_artifact_ref": "run:raw/2604.26498v1.pages.json",
        "pdf_artifact_ref": "run:raw/2604.26498v1.pdf",
        "extraction": {
            "page_count": 10,
            "extracted_chars": 24000,
            "extractor": "local_pdf_extraction",
        },
        "review": {
            "expected_status": "reviewed",
            "evidence_span_count": 1,
            "evidence_span_metadata": [{"index": 1, "label": "full_text_excerpt_1"}],
        },
        "acquisition": {
            "command_provenance": "python -m app.cli.review_refresh --topic large-language-models --fetch-missing-pdf",
            "max_pdf_bytes": 25000000,
            "download_limit": 1,
            "allow_domain": "arxiv.org",
            "timeout_seconds": 30,
            "operator_gate_required": True,
        },
        "known_exclusions": [
            {
                "paper_id": "2605.03205v1",
                "reason": "file_too_large",
                "max_pdf_bytes": 25000000,
            }
        ],
        "coverage": {
            "before_full_text_reviewed_unique_count": 27,
            "after_full_text_reviewed_unique_count": 28,
            "topic_relevant_unique_count": 524,
        },
        "portability": {
            "do_not_commit_pdf": True,
            "do_not_commit_full_text": True,
            "do_not_commit_pages_json": True,
            "replay_requires_bounded_network": True,
        },
        "created_from_commit": "test",
        "notes": ["test seed manifest"],
    }
    payload.update(overrides)
    return payload


def _write_manifest(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload or _manifest_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _patch_runtime(data_dir: Path, monkeypatch) -> None:
    storage_root = data_dir / "runs"
    profile_dir = data_dir / "topic_profiles"
    analytics_dir = data_dir / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)


def _seed(path: Path, data_dir: Path, *, dry_run: bool = False) -> dict:
    return evidence_manifest_seed.seed_manifest_metadata(path, data_dir=data_dir, dry_run=dry_run)


def test_seed_missing_data_dir_blocks(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)

    result = evidence_manifest_seed.seed_manifest_metadata(path, data_dir=None)

    assert result["status"] == "seed_blocked_no_data_dir"
    assert result["wrote_files"] is False
    assert result["network_attempted"] is False


def test_seed_blocks_real_data_root_by_default(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)

    result = _seed(path, constants.PROJECT_ROOT / "storage")

    assert result["status"] == "seed_blocked_real_data_dir"
    assert result["wrote_files"] is False
    assert result["review_written"] is False


def test_seed_invalid_manifest_blocks(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, {"schema_version": 1, "manifest_kind": "wrong"})

    result = _seed(path, tmp_path / "runtime")

    assert result["status"] == "manifest_invalid"
    assert result["wrote_files"] is False


def test_seed_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"

    result = _seed(path, data_dir, dry_run=True)

    assert result["status"] == "seed_plan_created"
    assert result["wrote_files"] is False
    assert result["wrote_db"] is False
    assert result["files_written"] == []
    assert not data_dir.exists()


def test_seed_writes_completed_run_metadata_and_triage_only(tmp_path: Path, monkeypatch) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"

    result = _seed(path, data_dir)
    _patch_runtime(data_dir, monkeypatch)
    run_json = Path(result["files_written"][0])
    triage_jsonl = Path(result["files_written"][1])
    run = json.loads(run_json.read_text(encoding="utf-8"))
    triage_lines = [json.loads(line) for line in triage_jsonl.read_text(encoding="utf-8").splitlines()]

    assert result["status"] == "seed_written"
    assert run["status"] == "completed"
    assert run["topic_profile_id"] == "large-language-models"
    assert result["seeded_run_count"] == 1
    assert result["seeded_triage_count"] == 2
    assert result["seeded_candidate_count"] == 1
    assert result["seeded_exclusion_count"] == 1
    assert {item["paper_id"] for item in triage_lines} == {"2604.26498v1", "2605.03205v1"}
    assert all(GENERATED_OCCURRENCE_ID.fullmatch(item["occurrence_id"]) for item in triage_lines)
    assert len({item["occurrence_id"] for item in triage_lines}) == len(triage_lines)
    assert any(item.get("exclusion_reason") == "file_too_large" for item in triage_lines)
    assert paper_reviews.list_reviews(topic_id="large-language-models", limit=10) == []


def test_seed_does_not_write_review_evidence_or_full_text_artifacts(tmp_path: Path, monkeypatch) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"

    result = _seed(path, data_dir)
    _patch_runtime(data_dir, monkeypatch)
    raw_dir = data_dir / "runs" / result["run_id"] / "raw"
    review_rows = paper_reviews.list_reviews(topic_id="large-language-models", limit=10)

    assert review_rows == []
    assert result["review_written"] is False
    assert result["safe_to_claim_reviewed"] is False
    assert list(raw_dir.glob("*.pdf")) == []
    assert list(raw_dir.glob("*.txt")) == []
    assert list(raw_dir.glob("*.pages.json")) == []
    assert "source_text_sha256" not in (data_dir / "runs" / result["run_id"] / "triage" / "triage.jsonl").read_text(encoding="utf-8")


def test_after_seed_replay_dry_run_plans_but_verify_local_still_missing(tmp_path: Path, monkeypatch) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"
    _patch_runtime(data_dir, monkeypatch)

    before = evidence_manifest_verifier.verify_manifest(path)
    seed_result = _seed(path, data_dir)
    replay = evidence_manifest_replay.replay_manifest(path, dry_run=True, env={})
    after = evidence_manifest_verifier.verify_manifest(path)

    assert before["status"] == "missing_review_row"
    assert seed_result["status"] == "seed_written"
    assert replay["status"] == "replay_dry_run_planned"
    assert replay["network_attempted"] is False
    assert replay["write_attempted"] is False
    assert after["status"] == "missing_review_row"
    assert after["safe_to_claim_local_verified"] is False


def test_seed_helper_does_not_call_network_or_provider_paths(tmp_path: Path, monkeypatch) -> None:
    path = _write_manifest(tmp_path)

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("seed helper must not call network")

    install_pdf_urlopen_mock(monkeypatch, fail_urlopen)
    result = _seed(path, tmp_path / "runtime")

    assert result["status"] == "seed_written"
    assert result["network_attempted"] is False
    assert result["replay_executed"] is False


def test_seed_existing_metadata_returns_already_present(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"

    first = _seed(path, data_dir)
    second = _seed(path, data_dir)

    assert first["status"] == "seed_written"
    assert second["status"] == "seed_already_present"
    assert second["wrote_files"] is False


def test_cli_seed_temp_metadata_json_is_stable(tmp_path: Path, capsys) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"

    exit_code = evidence_manifest.main(
        ["--manifest", str(path), "--seed-temp-metadata", "--data-dir", str(data_dir), "--dry-run", "--json"]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "seed_plan_created"
    assert output["paper_id"] == "2604.26498v1"
    assert output["network_attempted"] is False
    assert output["review_written"] is False


def test_cli_seed_requires_data_dir_and_does_not_change_existing_commands(tmp_path: Path, capsys) -> None:
    path = _write_manifest(tmp_path)

    seed_exit = evidence_manifest.main(["--manifest", str(path), "--seed-temp-metadata", "--json"])
    seed_output = json.loads(capsys.readouterr().out)
    plan_exit = evidence_manifest.main(["--manifest", str(path), "--plan-replay"])
    plan_output = capsys.readouterr().out

    assert seed_exit == 1
    assert seed_output["status"] == "seed_blocked_no_data_dir"
    assert plan_exit == 0
    assert "status: plan_only" in plan_output


def test_seed_helper_is_not_exposed_through_dashboard_api() -> None:
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))

    assert "seed-temp-metadata" not in api_text
    assert "seed_manifest_metadata" not in api_text


def test_temp_seed_artifacts_are_removable(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    data_dir = tmp_path / "runtime"

    result = _seed(path, data_dir)
    shutil.rmtree(data_dir)

    assert result["status"] == "seed_written"
    assert not data_dir.exists()

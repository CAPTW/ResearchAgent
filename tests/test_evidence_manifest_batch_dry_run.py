from __future__ import annotations

import json
import os
from pathlib import Path

from app.cli import evidence_manifest
from app.services import evidence_manifest_batch_dry_run


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]


def _admitted(paths: list[str | Path]) -> dict:
    return {
        "status": "admitted",
        "topic_id": "large-language-models",
        "batch_size": len(paths),
        "admitted_count": len(paths),
        "rejected_count": 0,
        "warning_count": 0,
        "safe_to_run_dry_run_batch": True,
        "safe_to_run_live_batch": False,
        "safe_to_claim_arbitrary_manifest_automation": False,
        "manifests": [
            {
                "index": index,
                "manifest_path": str(path),
                "status": "admitted",
                "paper_id": f"paper-{index + 1}",
                "topic_id": "large-language-models",
                "reasons": [],
            }
            for index, path in enumerate(paths)
        ],
        "rejections": [],
        "reasons": [],
        "read_only": True,
        "network_attempted": False,
        "write_attempted": False,
    }


def _verify_status(status: str) -> dict:
    return {
        "status": status,
        "local_status": None,
        "safe_to_claim_local_verified": status == "verified",
        "safe_to_claim_clean_clone_reproducible": False,
        "reasons": [] if status == "verified" else ["review_row_not_found"],
    }


def _seed_status(status: str = "seed_written") -> dict:
    return {
        "status": status,
        "wrote_files": status == "seed_written",
        "wrote_db": False,
        "network_attempted": False,
        "review_written": False,
        "replay_executed": False,
        "safe_to_claim_reviewed": False,
    }


def _replay_status(status: str = "replay_dry_run_planned", *, network: bool = False, write: bool = False) -> dict:
    return {
        "status": status,
        "network_attempted": network,
        "write_attempted": write,
        "safe_to_claim_reviewed": False,
        "safe_to_claim_clean_clone_reproducible": False,
    }


def test_current_three_manifests_reach_batch_dry_run_planned_and_cleanup() -> None:
    result = evidence_manifest_batch_dry_run.run_batch_dry_run(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
    )

    assert result["status"] == "batch_dry_run_planned"
    assert result["batch_size"] == 3
    assert result["attempted_count"] == 3
    assert result["dry_run_planned_count"] == 3
    assert result["failed_count"] == 0
    assert result["skipped_count"] == 0
    assert result["stopped_early"] is False
    assert result["network_attempted_count"] == 0
    assert result["write_attempted_count"] == 0
    assert result["replay_verified_count"] == 0
    assert result["reviewed_claim_count"] == 0
    assert result["safe_to_claim_batch_dry_run_readiness"] is True
    assert result["safe_to_claim_batch_live_replay_success"] is False
    assert result["safe_to_claim_arbitrary_manifest_automation"] is False
    assert result["temp_cleanup_performed"] is True
    assert result["temp_cleanup_exists"] is False
    assert [item["paper_id"] for item in result["per_manifest_results"]] == ["2604.26498v1", "2605.01143v1", "2604.14475v1"]
    assert all(item["dry_run_status"] == "replay_dry_run_planned" for item in result["per_manifest_results"])
    assert all(item["pre_verify_status"] in {"missing_review_row", "replay_required"} for item in result["per_manifest_results"])
    assert all(item["post_verify_status"] in {"missing_review_row", "replay_required"} for item in result["per_manifest_results"])


def test_keep_temp_preserves_parent_and_uses_separate_paper_dirs(tmp_path: Path) -> None:
    temp_parent = tmp_path / "batch"

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(
        KNOWN_MANIFESTS[:2],
        topic_id="large-language-models",
        temp_parent_dir=temp_parent,
        keep_temp=True,
    )

    assert result["status"] == "batch_dry_run_planned"
    assert result["temp_cleanup_performed"] is False
    assert Path(result["temp_parent_dir"]).exists()
    data_dirs = [Path(item["temp_data_dir"]) for item in result["per_manifest_results"]]
    assert len(set(data_dirs)) == 2
    assert all(path.exists() for path in data_dirs)
    assert all((path / "runs").exists() for path in data_dirs)


def test_admission_rejection_stops_before_temp_seed_or_replay(tmp_path: Path) -> None:
    temp_parent = tmp_path / "should-not-exist"

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(
        [KNOWN_MANIFESTS[2], KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        temp_parent_dir=temp_parent,
    )

    assert result["status"] == "batch_admission_rejected"
    assert "duplicate_paper_in_batch" in result["admission_report"]["reasons"]
    assert result["attempted_count"] == 0
    assert result["dry_run_planned_count"] == 0
    assert result["per_manifest_results"] == []
    assert result["temp_cleanup_performed"] is False
    assert not temp_parent.exists()


def test_seed_failure_stops_and_skips_remaining(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status("failed"))

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json", "b.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["attempted_count"] == 1
    assert result["failed_count"] == 1
    assert result["skipped_count"] == 1
    assert result["stopped_early"] is True
    assert result["first_failure_status"] == "seed_failed"


def test_pre_verify_unexpected_verified_stops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status())
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_verifier, "verify_manifest", lambda *_args, **_kwargs: _verify_status("verified"))

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["first_failure_status"] == "pre_verify_unexpectedly_verified"
    assert result["replay_verified_count"] == 0
    assert result["reviewed_claim_count"] == 0


def test_replay_dry_run_failure_network_or_write_stops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status())
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_verifier, "verify_manifest", lambda *_args, **_kwargs: _verify_status("missing_review_row"))
    monkeypatch.setattr(
        evidence_manifest_batch_dry_run.evidence_manifest_replay,
        "replay_manifest",
        lambda *_args, **_kwargs: _replay_status("replay_dry_run_planned", network=True),
    )

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["first_failure_status"] == "replay_dry_run_unexpected_network"
    assert result["network_attempted_count"] == 1
    assert result["write_attempted_count"] == 0


def test_replay_dry_run_write_attempted_stops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status())
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_verifier, "verify_manifest", lambda *_args, **_kwargs: _verify_status("missing_review_row"))
    monkeypatch.setattr(
        evidence_manifest_batch_dry_run.evidence_manifest_replay,
        "replay_manifest",
        lambda *_args, **_kwargs: _replay_status("replay_dry_run_planned", write=True),
    )

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["first_failure_status"] == "replay_dry_run_unexpected_write"
    assert result["network_attempted_count"] == 0
    assert result["write_attempted_count"] == 1


def test_replay_dry_run_unexpected_status_stops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status())
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_verifier, "verify_manifest", lambda *_args, **_kwargs: _verify_status("missing_review_row"))
    monkeypatch.setattr(
        evidence_manifest_batch_dry_run.evidence_manifest_replay,
        "replay_manifest",
        lambda *_args, **_kwargs: _replay_status("failed"),
    )

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["first_failure_status"] == "replay_dry_run_failed"
    assert result["dry_run_planned_count"] == 0


def test_post_verify_unexpected_verified_stops(tmp_path: Path, monkeypatch) -> None:
    calls = {"verify": 0}

    def fake_verify(*_args, **_kwargs):
        calls["verify"] += 1
        return _verify_status("missing_review_row" if calls["verify"] == 1 else "verified")

    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status())
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_verifier, "verify_manifest", fake_verify)
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_replay, "replay_manifest", lambda *_args, **_kwargs: _replay_status())

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["first_failure_status"] == "post_verify_unexpectedly_verified"


def test_temp_cleanup_failure_has_stable_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_admission, "check_batch_admission", lambda paths, **_kwargs: _admitted(list(paths)))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_seed, "seed_manifest_metadata", lambda *_args, **_kwargs: _seed_status())
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_verifier, "verify_manifest", lambda *_args, **_kwargs: _verify_status("missing_review_row"))
    monkeypatch.setattr(evidence_manifest_batch_dry_run.evidence_manifest_replay, "replay_manifest", lambda *_args, **_kwargs: _replay_status())

    def fail_cleanup(_path):
        raise OSError("locked")

    monkeypatch.setattr(evidence_manifest_batch_dry_run.shutil, "rmtree", fail_cleanup)
    result = evidence_manifest_batch_dry_run.run_batch_dry_run(["a.json"], topic_id="large-language-models", temp_parent_dir=tmp_path / "batch")

    assert result["status"] == "batch_dry_run_failed"
    assert result["first_failure_status"] == "temp_cleanup_failed"
    assert result["temp_cleanup_performed"] is False
    assert result["temp_cleanup_exists"] is True


def test_orchestrator_does_not_modify_bounded_fetch_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_BOUNDED_PDF_FETCH", raising=False)

    result = evidence_manifest_batch_dry_run.run_batch_dry_run(
        [KNOWN_MANIFESTS[0]],
        topic_id="large-language-models",
        temp_parent_dir=tmp_path / "batch",
    )

    assert result["status"] == "batch_dry_run_planned"
    assert "ALLOW_BOUNDED_PDF_FETCH" not in os.environ


def test_batch_dry_run_report_contract_includes_stable_fields() -> None:
    result = evidence_manifest_batch_dry_run.run_batch_dry_run(
        [KNOWN_MANIFESTS[0]],
        topic_id="large-language-models",
    )

    assert {
        "status",
        "topic_id",
        "batch_size",
        "attempted_count",
        "dry_run_planned_count",
        "failed_count",
        "skipped_count",
        "stopped_early",
        "first_failure_paper_id",
        "first_failure_status",
        "temp_parent_dir",
        "temp_cleanup_performed",
        "temp_cleanup_exists",
        "network_attempted_count",
        "write_attempted_count",
        "replay_verified_count",
        "reviewed_claim_count",
        "safe_to_claim_batch_dry_run_readiness",
        "safe_to_claim_batch_live_replay_success",
        "safe_to_claim_arbitrary_manifest_automation",
        "admission_report",
        "per_manifest_results",
        "reasons",
    } <= set(result)
    per_manifest = result["per_manifest_results"][0]
    assert {
        "index",
        "manifest_path",
        "paper_id",
        "temp_data_dir",
        "status",
        "seed_status",
        "pre_verify_status",
        "dry_run_status",
        "post_verify_status",
        "network_attempted",
        "write_attempted",
        "safe_to_claim_reviewed",
        "failure_status",
    } <= set(per_manifest)


def test_cli_batch_dry_run_json_is_parseable_and_duplicate_rejects(capsys) -> None:
    happy_exit = evidence_manifest.main(
        [
            "--batch-dry-run",
            "--topic",
            "large-language-models",
            *sum((["--manifest", manifest] for manifest in KNOWN_MANIFESTS), []),
            "--json",
        ]
    )
    happy = json.loads(capsys.readouterr().out)

    reject_exit = evidence_manifest.main(
        [
            "--batch-dry-run",
            "--topic",
            "large-language-models",
            "--manifest",
            KNOWN_MANIFESTS[2],
            "--manifest",
            KNOWN_MANIFESTS[2],
            "--json",
        ]
    )
    rejected = json.loads(capsys.readouterr().out)

    assert happy_exit == 0
    assert happy["status"] == "batch_dry_run_planned"
    assert happy["dry_run_planned_count"] == 3
    assert happy["safe_to_claim_batch_live_replay_success"] is False
    assert reject_exit == 1
    assert rejected["status"] == "batch_admission_rejected"
    assert "duplicate_paper_in_batch" in rejected["admission_report"]["reasons"]


def test_orchestrator_does_not_expose_dashboard_route_or_live_fetch_controls() -> None:
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))
    service_text = Path("app/services/evidence_manifest_batch_dry_run.py").read_text(encoding="utf-8")

    assert "batch-dry-run" not in api_text
    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert "dry_run=False" not in service_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "provider" not in service_text
    assert "oauth" not in service_text

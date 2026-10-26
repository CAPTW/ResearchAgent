from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
from importlib import import_module
from pathlib import Path

from app.cli import evidence_manifest
from app.domain import constants
from app.services import evidence_manifest_registry_proposal, evidence_manifest_registry_write


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]
TARGET_REGISTRY = "data/evidence_manifests/registry.json"


def _service():
    return import_module("app.services.evidence_manifest_registry_apply_preflight")


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _copy_known_manifests(repo: Path) -> None:
    for manifest in KNOWN_MANIFESTS:
        source = constants.PROJECT_ROOT / manifest
        target = repo / manifest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "preflight-repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "tests@example.test")
    _run_git(repo, "config", "user.name", "Tests")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _copy_known_manifests(repo)
    _run_git(repo, "add", "README.md", "data/evidence_manifests")
    _run_git(repo, "commit", "-m", "baseline manifests")
    return repo


def _proposal_path(tmp_path: Path, *, proposal_id: str = "apply-preflight-proposal") -> Path:
    proposal_path = tmp_path / f"{proposal_id}.json"
    result = evidence_manifest_registry_proposal.write_registry_proposal(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
        output_path=proposal_path,
        proposal_id=proposal_id,
    )
    assert result["status"] == "proposal_written"
    assert result["registry_check_status"] == "registry_verified"
    return proposal_path


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", [])}


def _ready_kwargs(repo: Path, proposal_path: Path) -> dict:
    return {
        "proposal_path": proposal_path,
        "registry_path": TARGET_REGISTRY,
        "topic_id": "large-language-models",
        "operator_approved": True,
        "confirm_registry_path": TARGET_REGISTRY,
        "env_gate_value": "1",
        "acknowledge_known_untracked": True,
        "acknowledge_commit_guard_warnings": True,
        "backup_policy_acknowledged": True,
        "rollback_plan_acknowledged": True,
        "post_write_diff_review_acknowledged": True,
        "commit_plan_acknowledged": True,
        "push_deferred_acknowledged": True,
        "project_root": repo,
    }


def _preflight(repo: Path, proposal_path: Path, **overrides) -> dict:
    kwargs = _ready_kwargs(repo, proposal_path)
    kwargs.update(overrides)
    return _service().run_production_apply_preflight(**kwargs)


def test_valid_fresh_proposal_returns_ready_without_writing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)

    result = _preflight(repo, proposal_path)

    assert result["status"] == "production_apply_preflight_ready"
    assert result["preflight_ready"] is True
    assert result["eligible_for_operator_review"] is True
    assert result["would_apply"] is False
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert result["db_storage_write_attempted"] is False
    assert result["network_attempted"] is False
    assert result["live_replay_executed"] is False
    assert result["provider_oauth_external_ai_used"] is False
    assert result["dashboard_trigger_used"] is False
    assert result["registry_absent"] is True
    assert result["proposal_fresh"] is True
    assert result["manifest_hashes_fresh"] is True
    assert result["registry_check_status"] == "registry_verified"
    assert result["writer_dry_run_status"] == "registry_write_dry_run_ready"
    assert result["commit_guard_status"] == "registry_commit_guard_passed"
    assert result["safe_to_claim_real_registry_exists"] is False
    assert result["safe_to_claim_registry_lifecycle_complete"] is False
    assert result["safe_to_run_future_apply"] is False
    assert result["reasons"] == []
    assert not (repo / TARGET_REGISTRY).exists()
    assert not (constants.PROJECT_ROOT / TARGET_REGISTRY).exists()


def test_missing_acknowledgements_block_even_when_writer_dry_run_is_ready(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _preflight(
        repo,
        _proposal_path(tmp_path),
        backup_policy_acknowledged=False,
        rollback_plan_acknowledged=False,
        post_write_diff_review_acknowledged=False,
        commit_plan_acknowledged=False,
        push_deferred_acknowledged=False,
    )

    assert result["status"] == "production_apply_preflight_blocked"
    assert result["writer_dry_run_status"] == "registry_write_dry_run_ready"
    assert result["registry_written"] is False
    codes = _reason_codes(result)
    assert "production_apply_backup_policy_missing" in codes
    assert "production_apply_rollback_plan_missing" in codes
    assert "production_apply_post_write_diff_review_missing" in codes
    assert "production_apply_commit_plan_missing" in codes
    assert "production_apply_push_deferred_missing" in codes


def test_missing_proposal_is_rejected(tmp_path: Path) -> None:
    result = _preflight(_init_repo(tmp_path), tmp_path / "missing.json")

    assert result["status"] == "production_apply_preflight_blocked"
    assert "production_apply_proposal_missing" in _reason_codes(result)


def test_invalid_proposal_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")

    result = _preflight(_init_repo(tmp_path), bad)

    assert result["status"] == "production_apply_preflight_blocked"
    assert "production_apply_proposal_invalid" in _reason_codes(result)


def test_stale_proposal_is_rejected(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = _load_json(proposal_path)
    proposal["proposal_generated_at"] = "2000-01-01T00:00:00Z"
    _write_json(proposal_path, proposal)

    repo = _init_repo(tmp_path)
    result = _preflight(repo, proposal_path)

    assert result["proposal_fresh"] is False
    assert "production_apply_proposal_stale" in _reason_codes(result)


def test_invalid_proposal_timestamp_is_rejected_with_stable_reason(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = _load_json(proposal_path)
    proposal["proposal_generated_at"] = "not-a-timestamp"
    _write_json(proposal_path, proposal)

    repo = _init_repo(tmp_path)
    result = _preflight(repo, proposal_path)

    assert result["proposal_fresh"] is False
    assert result["proposal_age_minutes"] is None
    assert result["max_proposal_age_minutes"] == 60
    assert "production_apply_proposal_timestamp_invalid" in _reason_codes(result)


def test_missing_proposal_timestamp_is_rejected_with_stable_reason(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = _load_json(proposal_path)
    proposal.pop("proposal_generated_at")
    _write_json(proposal_path, proposal)

    result = _preflight(_init_repo(tmp_path), proposal_path)

    assert result["proposal_fresh"] is False
    assert result["proposal_generated_at"] is None
    assert "production_apply_proposal_timestamp_missing" in _reason_codes(result)


def test_max_proposal_age_minutes_is_enforced_and_reported(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)

    result = _preflight(_init_repo(tmp_path), proposal_path, max_proposal_age_minutes=0)

    assert result["max_proposal_age_minutes"] == 0
    assert result["proposal_fresh"] is False
    assert "production_apply_proposal_stale" in _reason_codes(result)


def test_proposal_inside_repo_is_rejected_with_location_report(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = repo / "proposal.json"
    shutil.copyfile(_proposal_path(tmp_path), proposal_path)

    result = _preflight(repo, proposal_path)

    assert result["proposal_in_repo"] is True
    assert result["proposal_under_registry_dir"] is False
    assert "production_apply_proposal_inside_repo" in _reason_codes(result)


def test_proposal_under_registry_dir_is_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = repo / "data/evidence_manifests/proposal.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_proposal_path(tmp_path), proposal_path)

    result = _preflight(repo, proposal_path)

    assert result["proposal_in_repo"] is True
    assert result["proposal_under_registry_dir"] is True
    codes = _reason_codes(result)
    assert "production_apply_proposal_inside_repo" in codes
    assert "production_apply_proposal_in_registry_dir" in codes


def test_proposal_symlink_is_rejected_if_supported(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    link_path = tmp_path / "proposal-link.json"
    try:
        link_path.symlink_to(proposal_path)
    except OSError:
        return

    result = _preflight(repo, link_path)

    assert result["proposal_path_is_symlink"] is True
    assert "production_apply_proposal_symlink_disallowed" in _reason_codes(result)


def test_stale_manifest_hash_is_rejected(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = _load_json(proposal_path)
    proposal["proposed_registry"]["manifest_entries"][0]["manifest_sha256"] = "0" * 64
    _write_json(proposal_path, proposal)

    repo = _init_repo(tmp_path)
    result = _preflight(repo, proposal_path)

    assert result["manifest_hashes_fresh"] is False
    assert "production_apply_manifest_hash_stale" in _reason_codes(result)
    assert result["fresh_manifest_entries"] == 2
    assert result["stale_manifest_entries"] == [
        {
            "paper_id": "2604.26498v1",
            "manifest_path": "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
            "expected_sha256": "0" * 64,
            "actual_sha256": _sha256_file(repo / "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json"),
            "status": "stale",
        }
    ]
    assert result["registry_written"] is False


def test_proposed_registry_checker_failure_is_rejected(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = _load_json(proposal_path)
    proposal["proposed_registry"]["registry_schema_version"] = 999
    _write_json(proposal_path, proposal)

    result = _preflight(_init_repo(tmp_path), proposal_path)

    assert result["registry_check_status"] == "registry_rejected"
    assert "production_apply_dry_run_not_ready" in _reason_codes(result)


def test_missing_operator_env_and_confirmation_block(tmp_path: Path) -> None:
    result = _preflight(
        _init_repo(tmp_path),
        _proposal_path(tmp_path),
        operator_approved=False,
        env_gate_value=None,
        confirm_registry_path="data/evidence_manifests/not-registry.json",
    )

    codes = _reason_codes(result)
    assert "production_apply_operator_flag_required" in codes
    assert "production_apply_env_gate_required" in codes
    assert "production_apply_confirmation_mismatch" in codes
    assert result["writer_dry_run_status"] != "registry_write_dry_run_ready"


def test_known_untracked_duplicate_requires_acknowledgement(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    duplicate = repo / "docs/dev-current-state (1).md"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("duplicate\n", encoding="utf-8")

    result = _preflight(
        repo,
        _proposal_path(tmp_path),
        acknowledge_known_untracked=False,
        acknowledge_commit_guard_warnings=True,
    )

    assert result["commit_guard_status"] == "registry_commit_guard_warning"
    assert result["known_untracked_files"] == ["docs/dev-current-state (1).md"]
    assert "production_apply_known_untracked_unacknowledged" in _reason_codes(result)


def test_commit_guard_warning_requires_acknowledgement(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    duplicate = repo / "storage/metadata_index (1).sqlite3"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("duplicate\n", encoding="utf-8")

    result = _preflight(
        repo,
        _proposal_path(tmp_path),
        acknowledge_known_untracked=True,
        acknowledge_commit_guard_warnings=False,
    )

    assert result["commit_guard_status"] == "registry_commit_guard_warning"
    assert "production_apply_commit_guard_warning_unaccepted" in _reason_codes(result)


def test_existing_registry_blocks_preflight(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    registry = repo / TARGET_REGISTRY
    registry.write_text("{}", encoding="utf-8")

    result = _preflight(repo, _proposal_path(tmp_path))

    codes = _reason_codes(result)
    assert result["registry_absent"] is False
    assert "production_apply_registry_already_exists" in codes
    assert "production_apply_overwrite_not_approved" in codes
    assert registry.exists()


def test_dirty_tracked_worktree_blocks_preflight(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    result = _preflight(repo, _proposal_path(tmp_path))

    assert result["commit_guard_status"] == "registry_commit_guard_blocked"
    assert result["commit_guard_reasons"]
    assert "production_apply_dirty_worktree" in _reason_codes(result)
    assert "production_apply_commit_guard_blocked" in _reason_codes(result)


def test_strict_mode_blocks_known_untracked_even_when_acknowledged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    duplicate = repo / "docs/dev-current-state (1).md"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("duplicate\n", encoding="utf-8")

    result = _preflight(repo, _proposal_path(tmp_path), strict=True)

    assert result["commit_guard_status"] == "registry_commit_guard_blocked"
    assert result["known_untracked_files"] == ["docs/dev-current-state (1).md"]
    assert "production_apply_commit_guard_blocked" in _reason_codes(result)


def test_staged_runtime_artifact_blocks_preflight(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    artifact = repo / "storage/runtime.sqlite3"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("runtime\n", encoding="utf-8")
    _run_git(repo, "add", "storage/runtime.sqlite3")

    result = _preflight(repo, _proposal_path(tmp_path))

    assert result["commit_guard_status"] == "registry_commit_guard_blocked"
    assert "production_apply_runtime_artifact_present" in _reason_codes(result)
    assert "production_apply_commit_guard_blocked" in _reason_codes(result)


def test_preflight_never_runs_production_apply_or_temp_harness(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _preflight(repo, _proposal_path(tmp_path))

    assert result["would_apply"] is False
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert result["temp_harness_apply_attempted"] is False
    assert result["live_replay_executed"] is False
    assert result["provider_oauth_external_ai_used"] is False
    assert result["dashboard_trigger_used"] is False
    assert result["acknowledgement_status"] == {
        "known_untracked": True,
        "commit_guard_warnings": True,
        "backup_policy": True,
        "rollback_plan": True,
        "post_write_diff_review": True,
        "commit_plan": True,
        "push_deferred": True,
    }
    assert not (repo / TARGET_REGISTRY).exists()


def test_registry_write_apply_still_not_implemented(tmp_path: Path) -> None:
    result = evidence_manifest_registry_write.validate_registry_write_dry_run(
        proposal_path=_proposal_path(tmp_path),
        registry_path=TARGET_REGISTRY,
        topic_id="large-language-models",
        dry_run=False,
        operator_approved=True,
        confirm_registry_path=TARGET_REGISTRY,
    )

    assert result["status"] == "registry_write_apply_not_implemented"
    assert result["registry_written"] is False
    assert result["write_attempted"] is False


def test_happy_cli_exits_zero_and_outputs_parseable_json(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-preflight",
            "--proposal",
            str(proposal_path),
            "--registry",
            TARGET_REGISTRY,
            "--topic",
            "large-language-models",
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--acknowledge-known-untracked",
            "--acknowledge-commit-guard-warnings",
            "--acknowledge-backup-policy",
            "--acknowledge-rollback-plan",
            "--acknowledge-post-write-diff-review",
            "--acknowledge-commit-plan",
            "--acknowledge-push-deferred",
            "--repo-root",
            str(repo),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "production_apply_preflight_ready"
    assert payload["registry_written"] is False


def test_cli_missing_acknowledgement_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-preflight",
            "--proposal",
            str(_proposal_path(tmp_path)),
            "--registry",
            TARGET_REGISTRY,
            "--topic",
            "large-language-models",
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--repo-root",
            str(_init_repo(tmp_path)),
            "--json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "production_apply_preflight_blocked"
    assert "production_apply_backup_policy_missing" in _reason_codes(payload)


def test_cli_stale_proposal_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")
    proposal = _load_json(proposal_path)
    proposal["proposal_generated_at"] = "2000-01-01T00:00:00Z"
    _write_json(proposal_path, proposal)

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-preflight",
            "--proposal",
            str(proposal_path),
            "--registry",
            TARGET_REGISTRY,
            "--topic",
            "large-language-models",
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--acknowledge-known-untracked",
            "--acknowledge-commit-guard-warnings",
            "--acknowledge-backup-policy",
            "--acknowledge-rollback-plan",
            "--acknowledge-post-write-diff-review",
            "--acknowledge-commit-plan",
            "--acknowledge-push-deferred",
            "--repo-root",
            str(repo),
            "--json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "production_apply_proposal_stale" in _reason_codes(payload)


def test_cli_stale_manifest_hash_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")
    proposal = _load_json(proposal_path)
    proposal["proposed_registry"]["manifest_entries"][0]["manifest_sha256"] = "0" * 64
    _write_json(proposal_path, proposal)

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-preflight",
            "--proposal",
            str(proposal_path),
            "--registry",
            TARGET_REGISTRY,
            "--topic",
            "large-language-models",
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--acknowledge-known-untracked",
            "--acknowledge-commit-guard-warnings",
            "--acknowledge-backup-policy",
            "--acknowledge-rollback-plan",
            "--acknowledge-post-write-diff-review",
            "--acknowledge-commit-plan",
            "--acknowledge-push-deferred",
            "--repo-root",
            str(repo),
            "--json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "production_apply_manifest_hash_stale" in _reason_codes(payload)
    assert payload["stale_manifest_entries"][0]["paper_id"] == "2604.26498v1"

from __future__ import annotations

import json
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
    return import_module("app.services.evidence_manifest_registry_apply_harness")


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "temp-harness-repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "tests@example.test")
    _run_git(repo, "config", "user.name", "Tests")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _copy_known_manifests(repo)
    _run_git(repo, "add", "README.md", "data/evidence_manifests")
    _run_git(repo, "commit", "-m", "baseline manifests")
    return repo


def _copy_known_manifests(repo: Path) -> None:
    for manifest in KNOWN_MANIFESTS:
        source = constants.PROJECT_ROOT / manifest
        target = repo / manifest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _proposal_path(tmp_path: Path, *, proposal_id: str = "apply-harness-proposal") -> Path:
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


def _apply(repo: Path, proposal_path: Path, **overrides) -> dict:
    kwargs = {
        "proposal_path": proposal_path,
        "temp_project_root": repo,
        "registry_path": TARGET_REGISTRY,
        "topic_id": "large-language-models",
        "operator_approved": True,
        "confirm_registry_path": TARGET_REGISTRY,
        "env_gate_value": "1",
    }
    kwargs.update(overrides)
    return _service().apply_registry_in_temp_harness(**kwargs)


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", [])}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_valid_proposal_applies_registry_only_inside_temp_repo(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)

    result = _apply(repo, proposal_path)

    target = repo / TARGET_REGISTRY
    assert result["status"] == "registry_apply_harness_applied"
    assert target.exists()
    assert result["registry_written"] is True
    assert result["write_attempted"] is True
    assert result["candidate_written"] is True
    assert result["atomic_replace_attempted"] is True
    assert result["atomic_replace_succeeded"] is True
    assert result["backup_created"] is False
    assert result["rollback_attempted"] is False
    assert result["post_write_check_status"] == "registry_verified"
    assert result["safe_to_claim_harness_apply_verified"] is True
    assert result["safe_to_claim_real_project_registry_exists"] is False
    assert result["safe_to_claim_registry_lifecycle_complete"] is False
    assert result["network_attempted"] is False
    assert result["db_storage_write_attempted"] is False
    assert result["live_replay_executed"] is False
    assert result["provider_oauth_external_ai_used"] is False
    assert result["dashboard_trigger_used"] is False
    assert not (constants.PROJECT_ROOT / TARGET_REGISTRY).exists()


def test_missing_harness_env_gate_rejects_before_write(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)

    result = _apply(repo, _proposal_path(tmp_path), env_gate_value=None)

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_harness_env_gate_required" in _reason_codes(result)
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert not (repo / TARGET_REGISTRY).exists()


def test_real_project_root_is_blocked(tmp_path: Path) -> None:
    result = _apply(constants.PROJECT_ROOT, _proposal_path(tmp_path))

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_harness_real_repo_blocked" in _reason_codes(result)
    assert result["registry_written"] is False
    assert not (constants.PROJECT_ROOT / TARGET_REGISTRY).exists()


def test_missing_operator_flag_rejects(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)

    result = _apply(repo, _proposal_path(tmp_path), operator_approved=False)

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_operator_flag_required" in _reason_codes(result)
    assert result["registry_written"] is False


def test_confirm_path_mismatch_rejects(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)

    result = _apply(repo, _proposal_path(tmp_path), confirm_registry_path="data/evidence_manifests/not-registry.json")

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_confirmation_mismatch" in _reason_codes(result)
    assert result["confirm_path_matched"] is False


def test_missing_temp_project_root_rejects(tmp_path: Path) -> None:
    result = _service().apply_registry_in_temp_harness(
        proposal_path=_proposal_path(tmp_path),
        temp_project_root="",
        registry_path=TARGET_REGISTRY,
        operator_approved=True,
        confirm_registry_path=TARGET_REGISTRY,
        env_gate_value="1",
    )

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_harness_temp_root_required" in _reason_codes(result)
    assert result["registry_written"] is False


def test_non_git_temp_root_rejects(tmp_path: Path) -> None:
    non_git_root = tmp_path / "not-git"
    non_git_root.mkdir()

    result = _apply(non_git_root, _proposal_path(tmp_path))

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_harness_git_repo_required" in _reason_codes(result)
    assert result["registry_written"] is False


def test_existing_registry_without_overwrite_rejects(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    existing = repo / TARGET_REGISTRY
    _write_json(existing, {"registry_schema_version": 1, "topic_id": "large-language-models", "manifest_entries": []})

    result = _apply(repo, _proposal_path(tmp_path), allow_existing_registry=False)

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_existing_registry_requires_overwrite" in _reason_codes(result)
    assert result["registry_written"] is False
    assert _load_json(existing)["manifest_entries"] == []


def test_existing_registry_with_backup_is_replaced_in_temp_repo(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    existing = repo / TARGET_REGISTRY
    old_registry = {"registry_schema_version": 1, "topic_id": "large-language-models", "manifest_entries": []}
    _write_json(existing, old_registry)

    result = _apply(repo, _proposal_path(tmp_path), allow_existing_registry=True, backup_existing_registry=True)

    assert result["status"] == "registry_apply_harness_applied"
    assert result["backup_created"] is True
    assert Path(result["backup_path"]).exists()
    assert _load_json(existing)["manifest_entries"]
    assert result["safe_to_claim_real_project_registry_exists"] is False


def test_invalid_proposal_rejects(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    bad = tmp_path / "bad-proposal.json"
    bad.write_text("{not-json", encoding="utf-8")

    result = _apply(repo, bad)

    assert result["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_proposal_invalid" in _reason_codes(result)
    assert result["registry_written"] is False


def test_candidate_registry_check_failure_rejects_before_replace(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    proposal = _load_json(proposal_path)
    proposal["proposed_registry"]["manifest_entries"][0]["manifest_sha256"] = "0" * 64
    _write_json(proposal_path, proposal)

    result = _apply(repo, proposal_path)

    assert result["status"] == "registry_apply_harness_failed"
    assert "registry_apply_candidate_check_failed" in _reason_codes(result)
    assert result["candidate_written"] is True
    assert result["atomic_replace_attempted"] is False
    assert result["registry_written"] is False
    assert not (repo / TARGET_REGISTRY).exists()


def test_atomic_replace_failure_reports_without_target_write(tmp_path: Path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path)
    service = _service()

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(service.os, "replace", fail_replace)

    result = _apply(repo, _proposal_path(tmp_path))

    assert result["status"] == "registry_apply_harness_failed"
    assert "registry_apply_atomic_replace_failed" in _reason_codes(result)
    assert result["atomic_replace_attempted"] is True
    assert result["atomic_replace_succeeded"] is False
    assert result["registry_written"] is False
    assert not (repo / TARGET_REGISTRY).exists()


def test_post_write_check_failure_removes_new_registry(tmp_path: Path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    service = _service()
    real_check = service.evidence_manifest_registry.check_manifest_registry
    calls: list[str] = []

    def fake_check(registry_path, *args, **kwargs):
        calls.append(Path(registry_path).name)
        if len(calls) == 2:
            result = real_check(registry_path, *args, **kwargs)
            result["status"] = "registry_rejected"
            result["reasons"].append({"code": "forced_post_check_failure"})
            return result
        return real_check(registry_path, *args, **kwargs)

    monkeypatch.setattr(service.evidence_manifest_registry, "check_manifest_registry", fake_check)

    result = _apply(repo, proposal_path)

    assert result["status"] == "registry_apply_harness_rolled_back"
    assert "registry_apply_post_check_failed" in _reason_codes(result)
    assert "registry_apply_rollback_required" in _reason_codes(result)
    assert result["rollback_attempted"] is True
    assert result["rollback_succeeded"] is True
    assert result["registry_written"] is False
    assert not (repo / TARGET_REGISTRY).exists()


def test_post_write_check_failure_restores_existing_registry(tmp_path: Path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    existing = repo / TARGET_REGISTRY
    old_registry = {"registry_schema_version": 1, "topic_id": "large-language-models", "manifest_entries": []}
    _write_json(existing, old_registry)
    old_text = existing.read_text(encoding="utf-8")
    service = _service()
    real_check = service.evidence_manifest_registry.check_manifest_registry
    calls: list[str] = []

    def fake_check(registry_path, *args, **kwargs):
        calls.append(Path(registry_path).name)
        if len(calls) == 2:
            result = real_check(registry_path, *args, **kwargs)
            result["status"] = "registry_rejected"
            result["reasons"].append({"code": "forced_post_check_failure"})
            return result
        if len(calls) == 3:
            return {"status": "registry_verified", "network_attempted": False, "write_attempted": False}
        return real_check(registry_path, *args, **kwargs)

    monkeypatch.setattr(service.evidence_manifest_registry, "check_manifest_registry", fake_check)

    result = _apply(repo, proposal_path, allow_existing_registry=True, backup_existing_registry=True)

    assert result["status"] == "registry_apply_harness_rolled_back"
    assert result["rollback_attempted"] is True
    assert result["rollback_succeeded"] is True
    assert existing.read_text(encoding="utf-8") == old_text


def test_rollback_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path)
    proposal_path = _proposal_path(tmp_path)
    existing = repo / TARGET_REGISTRY
    _write_json(existing, {"registry_schema_version": 1, "topic_id": "large-language-models", "manifest_entries": []})
    service = _service()
    real_check = service.evidence_manifest_registry.check_manifest_registry
    real_replace = service.os.replace
    checks: list[str] = []
    replaces: list[tuple[object, object]] = []

    def fake_check(registry_path, *args, **kwargs):
        checks.append(Path(registry_path).name)
        if len(checks) == 2:
            result = real_check(registry_path, *args, **kwargs)
            result["status"] = "registry_rejected"
            result["reasons"].append({"code": "forced_post_check_failure"})
            return result
        return real_check(registry_path, *args, **kwargs)

    def fake_replace(source, target):
        replaces.append((source, target))
        if len(replaces) == 2:
            raise OSError("rollback failed")
        return real_replace(source, target)

    monkeypatch.setattr(service.evidence_manifest_registry, "check_manifest_registry", fake_check)
    monkeypatch.setattr(service.os, "replace", fake_replace)

    result = _apply(repo, proposal_path, allow_existing_registry=True, backup_existing_registry=True)

    assert result["status"] == "registry_apply_harness_failed"
    assert "registry_apply_rollback_failed" in _reason_codes(result)
    assert result["rollback_attempted"] is True
    assert result["rollback_succeeded"] is False


def test_existing_registry_write_mode_still_rejects_production_apply(tmp_path: Path) -> None:
    result = evidence_manifest_registry_write.validate_registry_write_dry_run(
        proposal_path=_proposal_path(tmp_path),
        registry_path=TARGET_REGISTRY,
        dry_run=False,
        operator_approved=True,
        env_gate_value="1",
        confirm_registry_path=TARGET_REGISTRY,
    )

    assert result["status"] == "registry_write_apply_not_implemented"
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert not (constants.PROJECT_ROOT / TARGET_REGISTRY).exists()


def test_harness_service_contains_no_live_fetch_provider_or_dashboard_paths() -> None:
    service_text = Path("app/services/evidence_manifest_registry_apply_harness.py").read_text(encoding="utf-8")
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))

    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert "replay_manifest(" not in service_text
    assert "seed_manifest_metadata(" not in service_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "openai" not in service_text.lower()
    assert "oauth_token_exchange" not in service_text
    assert "perform_real_provider_smoke" not in service_text
    assert "registry-apply-harness" not in api_text


def test_cli_harness_happy_path_outputs_parseable_json(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path)
    monkeypatch.setenv("ALLOW_TEMP_REGISTRY_APPLY_TEST_HARNESS", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-harness",
            "--temp-project-root",
            str(repo),
            "--proposal",
            str(_proposal_path(tmp_path, proposal_id="cli-harness")),
            "--registry",
            TARGET_REGISTRY,
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "registry_apply_harness_applied"
    assert output["registry_written"] is True
    assert output["post_write_check_status"] == "registry_verified"
    assert output["safe_to_claim_real_project_registry_exists"] is False


def test_cli_harness_missing_env_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path)
    monkeypatch.delenv("ALLOW_TEMP_REGISTRY_APPLY_TEST_HARNESS", raising=False)

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-harness",
            "--temp-project-root",
            str(repo),
            "--proposal",
            str(_proposal_path(tmp_path, proposal_id="cli-missing-env")),
            "--registry",
            TARGET_REGISTRY,
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_harness_env_gate_required" in _reason_codes(output)


def test_cli_harness_real_repo_root_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_TEMP_REGISTRY_APPLY_TEST_HARNESS", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-apply-harness",
            "--temp-project-root",
            str(constants.PROJECT_ROOT),
            "--proposal",
            str(_proposal_path(tmp_path, proposal_id="cli-real-root")),
            "--registry",
            TARGET_REGISTRY,
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_apply_harness_blocked"
    assert "registry_apply_harness_real_repo_blocked" in _reason_codes(output)

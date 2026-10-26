from __future__ import annotations

import json
import subprocess
from importlib import import_module
from pathlib import Path

from app.cli import evidence_manifest


TARGET_REGISTRY = "data/evidence_manifests/registry.json"


def _service():
    return import_module("app.services.evidence_manifest_registry_commit_guard")


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "tests@example.test")
    _run_git(repo, "config", "user.name", "Tests")
    _write(repo / "README.md", "baseline\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "baseline")
    return repo


def _write(path: Path, text: str = "content\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _guard(repo: Path, **kwargs) -> dict:
    options = {"repo_root": repo, "mode": "pre_apply"}
    options.update(kwargs)
    return _service().check_registry_commit_guard(**options)


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", [])}


def _blocking_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", []) if reason.get("blocking") is True}


def _status(repo: Path) -> str:
    return _run_git(repo, "status", "--short").stdout


def _commit_file(repo: Path, relative_path: str, text: str = "content\n") -> None:
    _write(repo / relative_path, text)
    _run_git(repo, "add", relative_path)
    _run_git(repo, "commit", "-m", f"add {relative_path}")


def test_clean_temp_repo_passes_pre_apply_without_write(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_passed"
    assert result["mode"] == "pre_apply"
    assert result["staged_files"] == []
    assert result["tracked_modified_files"] == []
    assert result["untracked_files"] == []
    assert result["safe_to_apply_registry_write"] is False
    assert result["safe_to_commit_registry"] is False
    assert result["registry_apply_performed"] is False
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert result["db_storage_write_attempted"] is False
    assert result["network_attempted"] is False
    assert result["reasons"] == []
    assert not (repo / TARGET_REGISTRY).exists()


def test_output_fields_are_stable_and_json_serializable(tmp_path: Path) -> None:
    result = _guard(_init_repo(tmp_path))

    json.loads(json.dumps(result))
    assert {
        "status",
        "mode",
        "repo_root",
        "checked_files_count",
        "staged_files",
        "tracked_modified_files",
        "untracked_files",
        "ignored_files_summary",
        "forbidden_files",
        "warning_files",
        "allow_registry_file",
        "allow_docs_changes",
        "safe_to_apply_registry_write",
        "safe_to_commit_registry",
        "registry_apply_performed",
        "registry_written",
        "write_attempted",
        "db_storage_write_attempted",
        "network_attempted",
        "reasons",
    }.issubset(result)


def test_staged_file_blocks_pre_apply(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "notes.md")
    _run_git(repo, "add", "notes.md")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "notes.md" in result["staged_files"]
    assert "registry_commit_guard_staged_files_present" in _blocking_codes(result)


def test_unexpected_tracked_modification_blocks_pre_apply(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "README.md", "changed\n")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "README.md" in result["tracked_modified_files"]
    assert "registry_commit_guard_unexpected_tracked_modification" in _blocking_codes(result)


def test_allow_docs_changes_allows_unstaged_docs_modification(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "docs/state.md")
    _write(repo / "docs/state.md", "changed\n")

    result = _guard(repo, allow_docs_changes=True)

    assert result["status"] == "registry_commit_guard_passed"
    assert _blocking_codes(result) == set()


def test_unexpected_source_modification_blocks_even_when_docs_allowed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "app/service.py", "VALUE = 1\n")
    _write(repo / "app/service.py", "VALUE = 2\n")

    result = _guard(repo, allow_docs_changes=True)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "app/service.py" in result["tracked_modified_files"]
    assert "registry_commit_guard_unexpected_tracked_modification" in _blocking_codes(result)


def test_registry_file_present_blocks_pre_apply_when_not_allowed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / TARGET_REGISTRY, "{}\n")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_forbidden_registry_file_present" in _blocking_codes(result)
    assert result["registry_written"] is False


def test_storage_artifact_blocks_when_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "storage/runtime.sqlite3")
    _run_git(repo, "add", "storage/runtime.sqlite3")

    result = _guard(repo)

    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)
    assert "storage/runtime.sqlite3" in result["forbidden_files"]


def test_output_artifact_blocks_when_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "output/report.json")
    _run_git(repo, "add", "output/report.json")

    result = _guard(repo)

    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)
    assert "output/report.json" in result["forbidden_files"]


def test_sqlite_database_blocks_when_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "metadata.sqlite3")
    _run_git(repo, "add", "metadata.sqlite3")

    result = _guard(repo)

    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)
    assert "metadata.sqlite3" in result["forbidden_files"]


def test_pdf_blocks_when_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "paper.pdf", "%PDF-1.4\n")
    _run_git(repo, "add", "paper.pdf")

    result = _guard(repo)

    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)
    assert "paper.pdf" in result["forbidden_files"]


def test_extracted_text_runtime_artifact_blocks_when_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "data/papers/raw.txt", "full text\n")
    _run_git(repo, "add", "data/papers/raw.txt")

    result = _guard(repo)

    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)
    assert "data/papers/raw.txt" in result["forbidden_files"]


def test_pages_json_blocks_when_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "paper.pages.json", "[]\n")
    _run_git(repo, "add", "paper.pages.json")

    result = _guard(repo)

    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)
    assert "paper.pages.json" in result["forbidden_files"]


def test_proposal_json_artifact_inside_repo_blocks(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "registry-proposal.json", "{}\n")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_forbidden_proposal_artifact" in _blocking_codes(result)


def test_known_duplicate_dev_current_state_warns_by_default(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "docs/dev-current-state (1).md")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_warning"
    assert "registry_commit_guard_known_duplicate_file_present" in _reason_codes(result)
    assert "registry_commit_guard_known_duplicate_file_present" not in _blocking_codes(result)
    assert "docs/dev-current-state (1).md" in result["warning_files"]


def test_known_duplicate_storage_metadata_warns_by_default(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "storage/metadata_index (1).sqlite3")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_warning"
    assert "registry_commit_guard_known_duplicate_file_present" in _reason_codes(result)
    assert "registry_commit_guard_forbidden_runtime_artifact" not in _blocking_codes(result)
    assert "storage/metadata_index (1).sqlite3" in result["warning_files"]


def test_strict_known_duplicate_blocks(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "docs/dev-current-state (1).md")

    result = _guard(repo, strict=True)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_known_duplicate_file_present" in _blocking_codes(result)


def test_unexpected_untracked_file_blocks(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "scratch.tmp")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_unexpected_untracked_file" in _blocking_codes(result)


def test_post_apply_allows_registry_path_when_configured(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / TARGET_REGISTRY, "{}\n")

    result = _guard(repo, mode="post_apply", allow_registry_file=True)

    assert result["status"] == "registry_commit_guard_passed"
    assert "registry_commit_guard_forbidden_registry_file_present" not in _reason_codes(result)


def test_post_apply_still_blocks_runtime_artifacts(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / TARGET_REGISTRY, "{}\n")
    _write(repo / "output/report.json", "{}\n")

    result = _guard(repo, mode="post_apply", allow_registry_file=True)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)


def test_pre_commit_allows_staged_registry_when_configured(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / TARGET_REGISTRY, "{}\n")
    _run_git(repo, "add", TARGET_REGISTRY)

    result = _guard(repo, mode="pre_commit", allow_registry_file=True)

    assert result["status"] == "registry_commit_guard_passed"
    assert "registry_commit_guard_staged_files_present" not in _reason_codes(result)
    assert "registry_commit_guard_forbidden_registry_file_present" not in _reason_codes(result)


def test_pre_commit_blocks_unexpected_staged_source(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "app/new_source.py", "VALUE = 1\n")
    _run_git(repo, "add", "app/new_source.py")

    result = _guard(repo, mode="pre_commit", allow_registry_file=True)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_unexpected_tracked_modification" in _blocking_codes(result)


def test_unsupported_mode_returns_reason(tmp_path: Path) -> None:
    result = _guard(_init_repo(tmp_path), mode="future_mode")

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_mode_unsupported" in _blocking_codes(result)


def test_guard_does_not_write_stage_or_unstage_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before_status = _status(repo)
    before_files = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*") if ".git" not in path.parts)

    result = _guard(repo)

    after_status = _status(repo)
    after_files = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*") if ".git" not in path.parts)
    assert result["write_attempted"] is False
    assert before_status == after_status
    assert before_files == after_files


def test_guard_service_contains_no_live_fetch_provider_or_dashboard_paths() -> None:
    service_text = Path("app/services/evidence_manifest_registry_commit_guard.py").read_text(encoding="utf-8")
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))

    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert "replay_manifest(" not in service_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "openai" not in service_text.lower()
    assert "oauth" not in service_text.lower()
    assert "provider" not in service_text.lower()
    assert "registry-commit-guard" not in api_text


def test_cli_clean_repo_exits_zero_with_parseable_json(tmp_path: Path, capsys) -> None:
    repo = _init_repo(tmp_path)

    exit_code = evidence_manifest.main(["--registry-commit-guard", "--mode", "pre-apply", "--repo-root", str(repo), "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "registry_commit_guard_passed"
    assert output["registry_written"] is False
    assert output["write_attempted"] is False


def test_cli_blocked_repo_exits_nonzero(tmp_path: Path, capsys) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "README.md", "changed\n")

    exit_code = evidence_manifest.main(["--registry-commit-guard", "--mode", "pre-apply", "--repo-root", str(repo), "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_unexpected_tracked_modification" in _reason_codes(output)


def test_cli_warning_repo_exits_zero_with_json(tmp_path: Path, capsys) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "docs/dev-current-state (1).md")

    exit_code = evidence_manifest.main(["--registry-commit-guard", "--mode", "pre-apply", "--repo-root", str(repo), "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "registry_commit_guard_warning"
    assert "registry_commit_guard_known_duplicate_file_present" in _reason_codes(output)


def test_local_absolute_path_leak_in_staged_doc_blocks_pre_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / "docs/leak.md", "local path C:\\Users\\USER\\secret.txt\n")
    _run_git(repo, "add", "docs/leak.md")

    result = _guard(repo, mode="pre_commit", allow_docs_changes=True)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_local_absolute_path_leak" in _blocking_codes(result)


def test_ignored_runtime_artifact_staged_is_reported(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo / ".gitignore", "storage/\n")
    _run_git(repo, "add", ".gitignore")
    _run_git(repo, "commit", "-m", "ignore storage")
    _write(repo / "storage/metadata_index.sqlite3")
    _run_git(repo, "add", "-f", "storage/metadata_index.sqlite3")

    result = _guard(repo)

    assert result["status"] == "registry_commit_guard_blocked"
    assert "registry_commit_guard_ignored_runtime_artifact_staged" in _blocking_codes(result)
    assert "registry_commit_guard_forbidden_runtime_artifact" in _blocking_codes(result)


def test_guard_does_not_add_dashboard_api_route() -> None:
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))

    assert "registry-commit-guard" not in api_text
    assert "registry_commit_guard" not in api_text

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from app.domain import constants

TARGET_REGISTRY_PATH = "data/evidence_manifests/registry.json"
SUPPORTED_MODES = {"pre_apply", "post_apply", "pre_commit"}
DEFAULT_KNOWN_UNTRACKED_ALLOWLIST = (
    "docs/dev-current-state (1).md",
    "storage/metadata_index (1).sqlite3",
)

_REASON_META: dict[str, tuple[str, bool, bool, str]] = {
    "registry_commit_guard_staged_files_present": ("Staged files are present before registry apply preflight.", True, True, "Unstage files before running the guard again."),
    "registry_commit_guard_unexpected_tracked_modification": ("Unexpected tracked modification is present.", True, True, "Commit, stash, or remove unrelated tracked changes."),
    "registry_commit_guard_forbidden_runtime_artifact": ("Runtime artifact or generated binary/text artifact is present.", True, True, "Remove the artifact from the commit scope."),
    "registry_commit_guard_forbidden_registry_file_present": ("Tracked registry path is present when this mode does not allow it.", True, True, "Do not create registry.json before the approved apply phase."),
    "registry_commit_guard_forbidden_proposal_artifact": ("Proposal JSON artifact is present inside the repository.", True, True, "Keep proposal artifacts in temp or approved non-repo locations."),
    "registry_commit_guard_known_duplicate_file_present": ("Known duplicate local file is present.", False, True, "Leave it unstaged or remove it outside this guarded workflow."),
    "registry_commit_guard_unexpected_untracked_file": ("Unexpected untracked file is present.", True, True, "Remove, ignore, or explicitly account for the file before continuing."),
    "registry_commit_guard_ignored_runtime_artifact_staged": ("Ignored runtime artifact is staged.", True, True, "Unstage the ignored runtime artifact."),
    "registry_commit_guard_local_absolute_path_leak": ("Staged docs contain a local absolute filesystem path.", True, True, "Remove local absolute paths before committing."),
    "registry_commit_guard_git_status_failed": ("Git status inspection failed.", True, True, "Run the guard from a valid git repository and inspect git errors."),
    "registry_commit_guard_mode_unsupported": ("Guard mode is unsupported.", True, True, "Use pre-apply, post-apply, or pre-commit."),
    "unknown_error": ("Unexpected commit guard error.", True, True, "Stop and diagnose before retrying."),
}

_LOCAL_ABSOLUTE_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s`'\"<>]+")


def _normalize_path(value: str | Path) -> str:
    normalized = Path(str(value)).as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


def _normalize_mode(value: str) -> str:
    return str(value or "").strip().replace("-", "_")


def _reason(code: str, *, file_path: str | None = None, blocking: bool | None = None) -> dict[str, Any]:
    message, default_blocking, operator_fixable, safe_next_action = _REASON_META.get(code, _REASON_META["unknown_error"])
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "blocking": default_blocking if blocking is None else bool(blocking),
        "operator_fixable": operator_fixable,
        "safe_next_action": safe_next_action,
    }
    if file_path is not None:
        payload["file_path"] = file_path
    return payload


def _add_reason(report: dict[str, Any], code: str, *, file_path: str | None = None, blocking: bool | None = None) -> None:
    key = (code, file_path)
    seen = report.setdefault("_reason_keys", set())
    if key in seen:
        return
    seen.add(key)
    report["reasons"].append(_reason(code, file_path=file_path, blocking=blocking))


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _split_z(value: str) -> list[str]:
    return [_normalize_path(item) for item in value.split("\0") if item]


def _git_files(repo_root: Path, *args: str) -> tuple[list[str], str | None]:
    result = _run_git(repo_root, *args)
    if result.returncode != 0:
        return [], result.stderr.strip() or result.stdout.strip() or "git command failed"
    return _split_z(result.stdout), None


def _resolve_repo_root(repo_root: str | Path | None) -> tuple[Path, str | None]:
    root = Path(repo_root or constants.PROJECT_ROOT).resolve()
    result = _run_git(root, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return root, result.stderr.strip() or result.stdout.strip() or "git rev-parse failed"
    return Path(result.stdout.strip()).resolve(), None


def _base_report(
    *,
    repo_root: Path,
    mode: str,
    allow_registry_file: bool,
    allow_docs_changes: bool,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "mode": mode,
        "repo_root": str(repo_root),
        "checked_files_count": 0,
        "staged_files": [],
        "tracked_modified_files": [],
        "untracked_files": [],
        "ignored_files_summary": {"count": 0, "sample": []},
        "forbidden_files": [],
        "warning_files": [],
        "allow_registry_file": bool(allow_registry_file),
        "allow_docs_changes": bool(allow_docs_changes),
        "safe_to_apply_registry_write": False,
        "safe_to_commit_registry": False,
        "registry_apply_performed": False,
        "registry_written": False,
        "write_attempted": False,
        "db_storage_write_attempted": False,
        "network_attempted": False,
        "reasons": [],
    }


def _is_docs_path(path: str) -> bool:
    return path == "docs" or path.startswith("docs/")


def _is_known_duplicate(path: str, known_untracked_allowlist: set[str]) -> bool:
    return path in known_untracked_allowlist


def _is_registry_path(path: str, expected_registry_path: str) -> bool:
    return _normalize_path(path) == expected_registry_path


def _is_runtime_artifact(path: str) -> bool:
    lowered = path.lower()
    name = Path(path).name.lower()
    if lowered.startswith(("storage/", "output/", ".pytest_cache/", "web/dist/", "web/node_modules/", "web/test-results/")):
        return True
    if "/__pycache__/" in f"/{lowered}" or lowered.endswith("/__pycache__"):
        return True
    if lowered.startswith(("data/papers/", "notes/papers/")) and lowered.endswith(".txt"):
        return True
    if lowered.endswith((".pdf", ".sqlite", ".sqlite3", ".db")):
        return True
    if lowered.endswith(".pages.json") or name in {"pages.json", "pages_content.json"}:
        return True
    return False


def _is_proposal_artifact(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(".json") and "proposal" in Path(lowered).name


def _is_allowed_expected_path(
    path: str,
    *,
    expected_registry_path: str,
    allow_registry_file: bool,
    allow_docs_changes: bool,
) -> bool:
    if allow_registry_file and _is_registry_path(path, expected_registry_path):
        return True
    if allow_docs_changes and _is_docs_path(path):
        return True
    return False


def _check_ignore_no_index(repo_root: Path, path: str) -> bool:
    result = _run_git(repo_root, "check-ignore", "--no-index", "-q", "--", path)
    return result.returncode == 0


def _staged_text(repo_root: Path, path: str) -> str:
    result = _run_git(repo_root, "show", f":{path}")
    if result.returncode != 0:
        return ""
    return result.stdout


def _classify_path(
    report: dict[str, Any],
    path: str,
    *,
    repo_root: Path,
    source: str,
    mode: str,
    expected_registry_path: str,
    allow_registry_file: bool,
    allow_docs_changes: bool,
    allow_known_untracked: bool,
    known_untracked_allowlist: set[str],
    strict: bool,
) -> None:
    is_untracked = source == "untracked"
    is_staged = source == "staged"
    is_known_duplicate = is_untracked and _is_known_duplicate(path, known_untracked_allowlist)

    if is_known_duplicate:
        blocking = bool(strict or not allow_known_untracked)
        _append_unique(report["warning_files"] if not blocking else report["forbidden_files"], path)
        _add_reason(report, "registry_commit_guard_known_duplicate_file_present", file_path=path, blocking=blocking)
        return

    if _is_registry_path(path, expected_registry_path) and not allow_registry_file:
        _append_unique(report["forbidden_files"], path)
        _add_reason(report, "registry_commit_guard_forbidden_registry_file_present", file_path=path)

    if _is_proposal_artifact(path):
        _append_unique(report["forbidden_files"], path)
        _add_reason(report, "registry_commit_guard_forbidden_proposal_artifact", file_path=path)

    if _is_runtime_artifact(path):
        _append_unique(report["forbidden_files"], path)
        _add_reason(report, "registry_commit_guard_forbidden_runtime_artifact", file_path=path)
        if is_staged and _check_ignore_no_index(repo_root, path):
            _add_reason(report, "registry_commit_guard_ignored_runtime_artifact_staged", file_path=path)

    if is_staged and mode == "pre_apply":
        _add_reason(report, "registry_commit_guard_staged_files_present", file_path=path)

    allowed = _is_allowed_expected_path(
        path,
        expected_registry_path=expected_registry_path,
        allow_registry_file=allow_registry_file,
        allow_docs_changes=allow_docs_changes,
    )
    if not allowed and not _is_runtime_artifact(path) and not _is_proposal_artifact(path) and not _is_registry_path(path, expected_registry_path):
        if source in {"staged", "tracked"}:
            _add_reason(report, "registry_commit_guard_unexpected_tracked_modification", file_path=path)
        elif source == "untracked":
            _add_reason(report, "registry_commit_guard_unexpected_untracked_file", file_path=path)

    if is_staged and _is_docs_path(path):
        staged_text = _staged_text(repo_root, path)
        if _LOCAL_ABSOLUTE_PATH_RE.search(staged_text):
            _append_unique(report["forbidden_files"], path)
            _add_reason(report, "registry_commit_guard_local_absolute_path_leak", file_path=path)


def check_registry_commit_guard(
    *,
    repo_root: str | Path | None = None,
    expected_registry_path: str = TARGET_REGISTRY_PATH,
    mode: str = "pre_apply",
    allow_docs_changes: bool = False,
    allow_registry_file: bool = False,
    allow_known_untracked: bool = True,
    known_untracked_allowlist: list[str] | tuple[str, ...] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    expected_registry = _normalize_path(expected_registry_path)
    root, root_error = _resolve_repo_root(repo_root)
    report = _base_report(
        repo_root=root,
        mode=normalized_mode,
        allow_registry_file=allow_registry_file,
        allow_docs_changes=allow_docs_changes,
    )
    known_untracked = {_normalize_path(path) for path in (known_untracked_allowlist or DEFAULT_KNOWN_UNTRACKED_ALLOWLIST)}

    if root_error:
        _add_reason(report, "registry_commit_guard_git_status_failed")
        report["status"] = "failed"
        report.pop("_reason_keys", None)
        return report

    if normalized_mode not in SUPPORTED_MODES:
        _add_reason(report, "registry_commit_guard_mode_unsupported")

    staged_files, staged_error = _git_files(root, "diff", "--name-only", "--cached", "-z")
    tracked_modified_files, tracked_error = _git_files(root, "diff", "--name-only", "-z")
    untracked_files, untracked_error = _git_files(root, "ls-files", "--others", "--exclude-standard", "-z")
    ignored_files, ignored_error = _git_files(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if any(error is not None for error in (staged_error, tracked_error, untracked_error, ignored_error)):
        _add_reason(report, "registry_commit_guard_git_status_failed")

    report["staged_files"] = staged_files
    report["tracked_modified_files"] = tracked_modified_files
    report["untracked_files"] = untracked_files
    report["ignored_files_summary"] = {"count": len(ignored_files), "sample": ignored_files[:20]}

    registry_candidate = root / expected_registry
    registry_in_git_lists = expected_registry in set(staged_files + tracked_modified_files + untracked_files)
    if registry_candidate.exists() and not allow_registry_file and not registry_in_git_lists:
        _append_unique(report["forbidden_files"], expected_registry)
        _add_reason(report, "registry_commit_guard_forbidden_registry_file_present", file_path=expected_registry)

    for path in staged_files:
        _classify_path(
            report,
            path,
            repo_root=root,
            source="staged",
            mode=normalized_mode,
            expected_registry_path=expected_registry,
            allow_registry_file=allow_registry_file,
            allow_docs_changes=allow_docs_changes,
            allow_known_untracked=allow_known_untracked,
            known_untracked_allowlist=known_untracked,
            strict=strict,
        )
    for path in tracked_modified_files:
        _classify_path(
            report,
            path,
            repo_root=root,
            source="tracked",
            mode=normalized_mode,
            expected_registry_path=expected_registry,
            allow_registry_file=allow_registry_file,
            allow_docs_changes=allow_docs_changes,
            allow_known_untracked=allow_known_untracked,
            known_untracked_allowlist=known_untracked,
            strict=strict,
        )
    for path in untracked_files:
        _classify_path(
            report,
            path,
            repo_root=root,
            source="untracked",
            mode=normalized_mode,
            expected_registry_path=expected_registry,
            allow_registry_file=allow_registry_file,
            allow_docs_changes=allow_docs_changes,
            allow_known_untracked=allow_known_untracked,
            known_untracked_allowlist=known_untracked,
            strict=strict,
        )

    checked_files = sorted(set(staged_files + tracked_modified_files + untracked_files + ignored_files))
    report["checked_files_count"] = len(checked_files)

    blocking_reasons = [reason for reason in report["reasons"] if reason.get("blocking") is True]
    if blocking_reasons:
        report["status"] = "registry_commit_guard_blocked"
    elif report["reasons"]:
        report["status"] = "registry_commit_guard_warning"
    else:
        report["status"] = "registry_commit_guard_passed"
    if any(reason["code"] == "registry_commit_guard_git_status_failed" for reason in report["reasons"]):
        report["status"] = "failed"

    report.pop("_reason_keys", None)
    return report

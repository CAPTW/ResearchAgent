from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8002
ORIGIN = f"http://{HOST}:{PORT}"
VITE_CONFIGS = ("web/vite.config.ts", "web/vite.config.js", "web/vite.config.d.ts")
GENERATED_VITE_CONFIGS = ("web/vite.config.js", "web/vite.config.d.ts")
PACKAGE_FILES = ("pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json")
M50_FILES = (
    "docs/audit/M50_EXECUTIVE_SUMMARY.md",
    "docs/audit/M50_REPO_AUDIT.md",
    "docs/audit/M50_ARCHITECTURE_AND_MODULE_MAP.md",
    "docs/audit/M50_GAP_AND_RISK_REGISTER.md",
    "docs/audit/M50_PRODUCT_GOAL_COVERAGE.md",
)
SENSITIVE_TEXT = re.compile(
    r"(?:BEGIN [A-Z ]+PRIVATE KEY|Bearer\s+[A-Za-z0-9._-]+|(?:api[_-]?key|password|token)\s*[=:]\s*\S+)",
    re.IGNORECASE,
)

sys.path.insert(0, str(REPO_ROOT))
from scripts.evidence_provenance_harness_git_policy import (  # noqa: E402
    BASE_OID,
    CHECKPOINT_GENERATION,
    CHECKPOINT_STATUS_SUBJECT,
    CHECKPOINT_TWO_PATHS,
    CHECKPOINT_TWO_SUBJECT,
    CONTAINMENT_BLOB_OIDS,
    CONTAINMENT_OID,
    CONTAINMENT_PATHS,
    CONTAINMENT_SUBJECT,
    CONTAINMENT_TREE,
    CURRENT_STATUS_OID,
    CURRENT_STATUS_SUBJECT,
    CURRENT_STATUS_TREE,
    FEATURE_PATHS,
    FEATURE_OID,
    FEATURE_SUBJECT,
    GENERATION3_CHECKPOINT_GENERATION,
    GENERATION3_CHECKPOINT_PATH_STATUSES,
    GENERATION3_CHECKPOINT_PATHS,
    GENERATION3_CHECKPOINT_SUBJECT,
    GENERATION3_POLICY_FEATURE_SUBJECT,
    GENERATION3_PREVIOUS_CHECKPOINT_OID,
    GENERATION3_PREVIOUS_CHECKPOINT_TREE,
    GENERATION3_PROOF_BLOB_OIDS,
    GENERATION3_STATUS_BASE_OID,
    GENERATION3_STATUS_BASE_SUBJECT,
    GENERATION3_STATUS_BASE_TREE,
    GENERATION3_STATUS_SUBJECT,
    GENERATION3_TRANSITION_FEATURE_OID,
    GENERATION3_TRANSITION_ID,
    INTEGRATION_PATHS,
    INTEGRATION_OID,
    INTEGRATION_SUBJECT,
    POLICY_BASE_OID,
    POLICY_FEATURE_SUBJECT,
    POLICY_MERGE_SUBJECT,
    POLICY_PATHS,
    POLICY_VERSION,
    PREVIOUS_CHECKPOINT_OID,
    PREVIOUS_POLICY_FEATURE_OID,
    PROVIDER_ROUTING_FEATURE_BLOB_OIDS,
    PROVIDER_ROUTING_FEATURE_OID,
    PROVIDER_ROUTING_FEATURE_PATH_STATUSES,
    PROVIDER_ROUTING_FEATURE_PATHS,
    PROVIDER_ROUTING_FEATURE_SUBJECT,
    PROVIDER_ROUTING_FEATURE_TREE,
    RC_BRANCH,
    SAFE_LINK_PATHS,
    SAFE_LINK_OID,
    SAFE_LINK_SUBJECT,
    SOURCE_BRANCH,
    TRANSITION_ID,
    TRANSITION_SUBJECT,
    TRUSTED_BLOB_OIDS,
    TRUSTED_BLOB_SHA256,
    BlobRecord,
    CheckoutRecord,
    CommitRecord,
    ExecutionMode,
    OwnerArtifactRecord,
    PolicyDecision,
    compare_owner_inventories,
    evaluate_checkout,
    is_protected_untracked_path,
)
from tests.helpers.evidence_provenance_fixture import (  # noqa: E402
    CREATED_BY,
    FIXTURE_ID,
    FIXTURE_KIND,
    FIXTURE_SCHEMA_VERSION,
    PAPER_ID,
    PORTABLE_ARTIFACT_REF,
    REPORT_SECTION_TITLE,
    RUN_ID,
    SeedManifest,
    TOPIC_ID,
    verify_evidence_provenance_overlay,
)


class HarnessFailure(RuntimeError):
    def __init__(self, stop_reason: str, message: str) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


@dataclass(frozen=True)
class ViteConfigIdentity:
    path: str
    head_blob: str
    index_blob: str
    filtered_worktree_blob: str
    raw_sha256: str
    size: int
    head_mode: str
    index_mode: str
    eol: str
    attributes: tuple[str, ...]
    porcelain_status: str

    def guard_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "headBlob": self.head_blob,
            "indexBlob": self.index_blob,
            "filteredWorktreeBlob": self.filtered_worktree_blob,
            "rawSha256": self.raw_sha256,
            "size": self.size,
            "headMode": self.head_mode,
            "indexMode": self.index_mode,
            "eol": self.eol,
            "attributes": list(self.attributes),
            "porcelainStatus": self.porcelain_status,
        }


@dataclass(frozen=True)
class FailureResolution:
    primary_failure: str | None
    secondary_failure_codes: tuple[str, ...]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", f"unexpected HTTP redirect to {newurl}")


def _run(
    command: Sequence[str],
    *,
    cwd: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _git(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return _run(("git", *args), cwd=cwd, env=_safe_base_environment())


def _safe_base_environment() -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PLAYWRIGHT_BROWSER_CHANNEL",
    }
    env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


def _runtime_environment(temp_root: Path) -> dict[str, str]:
    env = _safe_base_environment()
    env.update(
        {
            "AUTH_MODE": "disabled",
            "APP_BASE_URL": ORIGIN,
            "WEB_APP_BASE_PATH": "/app",
            "XRML_DATA_DIR": str(temp_root),
            "XRML_OPERATOR_AUDIT_PATH": str(temp_root / "operator-audit" / "operator-audit.jsonl"),
            "XRML_ENVIRONMENT": "local",
            "XRML_DEPLOYMENT_PROFILE": "single-service",
            "XRML_STRICT_ENV_VALIDATION": "false",
            "TRUST_PROXY_HEADERS": "false",
            "TOPIC_SCHEDULER_MODE": "disabled",
            "TOPIC_SCHEDULER_ENABLED": "0",
            "SCHEDULER_MODE": "single_node_file_backed",
            "XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT": "0",
            "ALLOW_REAL_PROVIDER_SMOKE": "0",
            "ALLOW_LIVE_OAUTH_SMOKE": "0",
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "TMPDIR": str(temp_root),
        }
    )
    return env


def _fixture_environment(temp_root: Path) -> dict[str, str]:
    env = _runtime_environment(temp_root)
    base = _safe_base_environment()
    for name in ("TEMP", "TMP", "TMPDIR"):
        if name in base:
            env[name] = base[name]
        else:
            env.pop(name, None)
    return env


def _tail(text: str, *, lines: int = 30) -> str:
    bounded = "\n".join(text.splitlines()[-lines:])
    return SENSITIVE_TEXT.sub("<redacted>", bounded)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_meta(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "sha256": _sha256(path),
    }


def _file_content_meta(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"exists": False}
    return {"exists": True, "size": path.stat().st_size, "sha256": _sha256(path)}


def _required_git_output(stop_reason: str, *args: str) -> str:
    result = _git(*args)
    if result.returncode != 0:
        raise HarnessFailure(stop_reason, _tail(result.stderr or result.stdout))
    return result.stdout.rstrip("\r\n")


def _capture_vite_config_identity(relative: str) -> ViteConfigIdentity:
    path = REPO_ROOT / relative
    if not path.is_file():
        raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"tracked Vite config is missing: {relative}")

    head_tree = _required_git_output("VITE_GENERATED_CONFIG_DRIFT", "ls-tree", "HEAD", "--", relative).split()
    index_entry = _required_git_output("VITE_GENERATED_CONFIG_DRIFT", "ls-files", "-s", "--", relative).split()
    if len(head_tree) < 3 or len(index_entry) < 4 or index_entry[2] != "0":
        raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"unexpected Git entry shape for {relative}")

    raw_sha256 = _sha256(path)
    if raw_sha256 is None:
        raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"unable to hash {relative}")
    attributes = tuple(
        line.strip()
        for line in _required_git_output("VITE_GENERATED_CONFIG_DRIFT", "check-attr", "-a", "--", relative).splitlines()
        if line.strip()
    )
    return ViteConfigIdentity(
        path=relative,
        head_blob=_required_git_output("VITE_GENERATED_CONFIG_DRIFT", "rev-parse", f"HEAD:{relative}"),
        index_blob=index_entry[1],
        filtered_worktree_blob=_required_git_output(
            "VITE_GENERATED_CONFIG_DRIFT", "hash-object", f"--path={relative}", relative
        ),
        raw_sha256=raw_sha256,
        size=path.stat().st_size,
        head_mode=head_tree[0],
        index_mode=index_entry[0],
        eol=_required_git_output("VITE_GENERATED_CONFIG_DRIFT", "ls-files", "--eol", "--", relative).strip(),
        attributes=attributes,
        porcelain_status=_required_git_output(
            "VITE_GENERATED_CONFIG_DRIFT", "status", "--porcelain=v2", "--", relative
        ).strip(),
    )


def _capture_vite_config_identities() -> dict[str, ViteConfigIdentity]:
    return {relative: _capture_vite_config_identity(relative) for relative in GENERATED_VITE_CONFIGS}


def _compare_vite_config_identity(
    before: ViteConfigIdentity,
    after: ViteConfigIdentity,
) -> tuple[str, ...]:
    comparable_fields = (
        "head_blob",
        "index_blob",
        "filtered_worktree_blob",
        "raw_sha256",
        "size",
        "head_mode",
        "index_mode",
        "eol",
        "attributes",
    )
    return tuple(field for field in comparable_fields if getattr(before, field) != getattr(after, field))


def _compare_vite_identity_sets(
    before: Mapping[str, ViteConfigIdentity],
    after: Mapping[str, ViteConfigIdentity],
) -> dict[str, tuple[str, ...]]:
    return {
        relative: changed
        for relative in GENERATED_VITE_CONFIGS
        if (changed := _compare_vite_config_identity(before[relative], after[relative]))
    }


def _assert_vite_content_identity(identities: Mapping[str, ViteConfigIdentity]) -> None:
    for relative, identity in identities.items():
        if identity.head_blob != identity.index_blob:
            raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"HEAD/index blob mismatch: {relative}")
        if identity.index_blob != identity.filtered_worktree_blob:
            raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"index/worktree blob mismatch: {relative}")
        if identity.head_mode != identity.index_mode:
            raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"HEAD/index mode mismatch: {relative}")


def _refresh_vite_stat_cache_if_content_identical(
    identities: Mapping[str, ViteConfigIdentity],
) -> tuple[dict[str, ViteConfigIdentity], bool, bool, bool]:
    _assert_vite_content_identity(identities)
    status_only_drift = any(identity.porcelain_status for identity in identities.values())
    if not status_only_drift:
        return dict(identities), False, False, False

    baseline = dict(identities)
    _git("update-index", "--refresh", "--", *GENERATED_VITE_CONFIGS)
    current = _capture_vite_config_identities()
    _assert_vite_content_identity(current)
    changed = _compare_vite_identity_sets(baseline, current)
    if changed:
        index_changed = any("index_blob" in fields for fields in changed.values())
        reason = (
            "VITE_GENERATED_CONFIG_INDEX_CHANGED_DURING_REFRESH"
            if index_changed
            else "VITE_GENERATED_CONFIG_DRIFT"
        )
        raise HarnessFailure(reason, f"Vite identity changed during stat refresh: {changed}")

    if any(identity.porcelain_status for identity in current.values()):
        _git("update-index", "--really-refresh", "--", *GENERATED_VITE_CONFIGS)
        current = _capture_vite_config_identities()
        _assert_vite_content_identity(current)
        changed = _compare_vite_identity_sets(baseline, current)
        if changed:
            index_changed = any("index_blob" in fields for fields in changed.values())
            reason = (
                "VITE_GENERATED_CONFIG_INDEX_CHANGED_DURING_REFRESH"
                if index_changed
                else "VITE_GENERATED_CONFIG_DRIFT"
            )
            raise HarnessFailure(reason, f"Vite identity changed during forced stat refresh: {changed}")

    if any(identity.porcelain_status for identity in current.values()):
        raise HarnessFailure(
            "VITE_GENERATED_CONFIG_STATUS_REFRESH_FAILED",
            "content-identical Vite paths remained dirty after path-scoped stat refresh",
        )
    index_changed = any(baseline[path].index_blob != current[path].index_blob for path in GENERATED_VITE_CONFIGS)
    if index_changed:
        raise HarnessFailure(
            "VITE_GENERATED_CONFIG_INDEX_CHANGED_DURING_REFRESH",
            "Vite index blob changed during path-scoped stat refresh",
        )
    return current, True, True, False


def _vite_guard_snapshot() -> dict[str, object]:
    return {
        "generated": {
            relative: identity.guard_payload()
            for relative, identity in _capture_vite_config_identities().items()
        },
        "source": _file_content_meta(REPO_ROOT / "web" / "vite.config.ts"),
    }


def _directory_meta(path: Path) -> dict[str, object]:
    if not path.is_dir():
        return {"exists": False}
    files = [entry for entry in path.rglob("*") if entry.is_file()]
    return {
        "exists": True,
        "mtimeNs": path.stat().st_mtime_ns,
        "fileCount": len(files),
        "totalBytes": sum(entry.stat().st_size for entry in files),
    }


def _port_available() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _toolchain_root() -> Path:
    common = _git("rev-parse", "--git-common-dir")
    if common.returncode != 0:
        raise HarnessFailure("PROJECT_TEST_ENVIRONMENT_UNAVAILABLE", _tail(common.stderr))
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (REPO_ROOT / common_dir).resolve()
    candidate = common_dir.parent
    return candidate if candidate.is_dir() else REPO_ROOT


def _python_version_contract() -> dict[str, str]:
    if sys.version_info < (3, 11):
        raise HarnessFailure("PROJECT_TEST_ENVIRONMENT_UNAVAILABLE", "Python 3.11+ is required")
    try:
        import uvicorn
    except ImportError as exc:
        raise HarnessFailure("PROJECT_TEST_ENVIRONMENT_UNAVAILABLE", "uvicorn is unavailable") from exc
    return {"python": sys.version.split()[0], "uvicorn": uvicorn.__version__}


def _npm_command() -> Path:
    executable = shutil.which("npm.cmd" if os.name == "nt" else "npm", path=_safe_base_environment().get("PATH"))
    if executable is None:
        raise HarnessFailure("PROJECT_TEST_ENVIRONMENT_UNAVAILABLE", "npm is unavailable")
    return Path(executable)


def _playwright_command() -> Path:
    suffix = "playwright.cmd" if os.name == "nt" else "playwright"
    command = REPO_ROOT / "web" / "node_modules" / ".bin" / suffix
    if not command.is_file():
        raise HarnessFailure(
            "PROJECT_TEST_ENVIRONMENT_UNAVAILABLE",
            "web/node_modules with the local Playwright CLI is required; no install is allowed",
        )
    return command


def _git_paths(stop_reason: str, *args: str) -> tuple[str, ...]:
    result = _git(*args)
    if result.returncode != 0:
        raise HarnessFailure(stop_reason, _tail(result.stderr or result.stdout))
    entries = result.stdout.split("\0")
    if entries and entries[-1] == "":
        entries.pop()
    if any(not entry for entry in entries):
        raise HarnessFailure(stop_reason, "Git returned an empty path entry")
    return tuple(entry.replace("\\", "/") for entry in entries)


def _git_name_status(stop_reason: str, *args: str) -> tuple[tuple[str, str], ...]:
    result = _git(*args)
    if result.returncode != 0:
        raise HarnessFailure(stop_reason, _tail(result.stderr or result.stdout))
    entries = result.stdout.split("\0")
    if entries and entries[-1] == "":
        entries.pop()
    if len(entries) % 2 or any(not entry for entry in entries):
        raise HarnessFailure(stop_reason, "Git returned malformed name-status output")
    records = tuple(
        (entries[index + 1].replace("\\", "/"), entries[index])
        for index in range(0, len(entries), 2)
    )
    paths = tuple(path for path, _status in records)
    if len(paths) != len(set(paths)):
        raise HarnessFailure(stop_reason, "Git returned duplicate name-status paths")
    return tuple(sorted(records))


def _worktree_status() -> tuple[bool, bool, tuple[str, ...]]:
    status = _git("status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")
    if status.returncode != 0:
        raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", _tail(status.stderr or status.stdout))
    entries = status.stdout.split("\0")
    if entries and entries[-1] == "":
        entries.pop()
    tracked_clean = True
    untracked: list[str] = []
    for entry in entries:
        if len(entry) < 4 or entry[2] != " ":
            raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", "malformed porcelain status entry")
        if entry[:2] == "??":
            untracked.append(entry[3:].replace("\\", "/"))
        else:
            tracked_clean = False
    staged_paths = _git_paths(
        "POLICY_GIT_COMMAND_FAILED",
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--",
    )
    return tracked_clean, not staged_paths, tuple(sorted(untracked))


def _owner_artifact(relative: str) -> tuple[OwnerArtifactRecord, ...]:
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", f"unsafe relative Git path: {normalized}")
    path = REPO_ROOT.joinpath(*parts)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", f"unable to stat owner artifact: {normalized}") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        kind = "reparse"
    elif stat.S_ISLNK(metadata.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        kind = "other"
    record = OwnerArtifactRecord(
        path=normalized,
        kind=kind,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=_sha256(path) if kind == "file" else None,
    )
    if kind != "directory":
        return (record,)
    descendants: list[OwnerArtifactRecord] = [record]
    try:
        children = sorted(os.scandir(path), key=lambda item: item.name)
    except OSError as exc:
        raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", f"unable to inventory owner directory: {normalized}") from exc
    for child in children:
        descendants.extend(_owner_artifact(f"{normalized}/{child.name}"))
    return tuple(descendants)


def _owner_inventory(untracked: Sequence[str]) -> tuple[OwnerArtifactRecord, ...]:
    records: dict[str, OwnerArtifactRecord] = {}
    for relative in untracked:
        for record in _owner_artifact(relative):
            previous = records.get(record.path)
            if previous is not None and previous != record:
                raise HarnessFailure("POLICY_CONTEXT_AMBIGUOUS", f"conflicting owner metadata: {record.path}")
            records[record.path] = record
    return tuple(records[path] for path in sorted(records))


def _collect_commit(oid: str) -> CommitRecord:
    parents = tuple(_required_git_output("POLICY_GIT_COMMAND_FAILED", "show", "-s", "--format=%P", oid).split())
    subject = _required_git_output("POLICY_GIT_COMMAND_FAILED", "show", "-s", "--format=%s", oid)
    tree_oid = _required_git_output("POLICY_GIT_COMMAND_FAILED", "rev-parse", f"{oid}^{{tree}}")
    changed_path_statuses: tuple[tuple[str, str], ...] = ()
    if parents:
        changed_path_statuses = _git_name_status(
            "POLICY_GIT_COMMAND_FAILED",
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            parents[0],
            oid,
            "--",
        )
    return CommitRecord(
        oid=oid,
        parents=parents,
        subject=subject,
        changed_paths=frozenset(path for path, _status in changed_path_statuses),
        tree_oid=tree_oid,
        changed_path_statuses=changed_path_statuses,
    )


def _git_blob_bytes(oid: str) -> bytes:
    result = subprocess.run(
        ("git", "cat-file", "blob", oid),
        cwd=REPO_ROOT,
        env=_safe_base_environment(),
        capture_output=True,
        text=False,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")
        raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", _tail(detail))
    return result.stdout


def _collect_blob(commit: str, relative: str) -> BlobRecord:
    oid = _required_git_output("POLICY_GIT_COMMAND_FAILED", "rev-parse", f"{commit}:{relative}")
    sha256 = None
    if relative in TRUSTED_BLOB_SHA256:
        sha256 = hashlib.sha256(_git_blob_bytes(oid)).hexdigest().upper()
    return BlobRecord(path=relative, oid=oid, sha256=sha256)


def _collect_blobs(commit: str, paths: Iterable[str]) -> tuple[BlobRecord, ...]:
    return tuple(_collect_blob(commit, relative) for relative in sorted(paths))


def _collect_checkout() -> tuple[CheckoutRecord, PolicyDecision, tuple[OwnerArtifactRecord, ...]]:
    branch = _required_git_output("POLICY_GIT_COMMAND_FAILED", "branch", "--show-current")
    head = _required_git_output("POLICY_GIT_COMMAND_FAILED", "rev-parse", "HEAD")
    tracked_clean, index_empty, untracked = _worktree_status()
    protected = tuple(sorted(path for path in untracked if is_protected_untracked_path(path)))
    owner_inventory = _owner_inventory(tuple(path for path in untracked if path not in protected))

    if branch == RC_BRANCH:
        ancestor = _git("merge-base", "--is-ancestor", INTEGRATION_OID, head)
        if ancestor.returncode == 0:
            chain_oids = _required_git_output(
                "POLICY_GIT_COMMAND_FAILED",
                "rev-list",
                "--first-parent",
                "--reverse",
                f"{INTEGRATION_OID}..{head}",
            ).splitlines()
        elif ancestor.returncode == 1:
            chain_oids = []
        else:
            raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", "integration ancestry query failed")
    else:
        chain_oids = []
    first_parent_commits = tuple(_collect_commit(oid) for oid in chain_oids)
    related: list[CommitRecord] = [
        _collect_commit(BASE_OID),
        _collect_commit(SAFE_LINK_OID),
        _collect_commit(FEATURE_OID),
        _collect_commit(INTEGRATION_OID),
        _collect_commit(PREVIOUS_POLICY_FEATURE_OID),
    ]
    p_positions = tuple(
        index for index, commit in enumerate(first_parent_commits) if commit.oid == CURRENT_STATUS_OID
    )
    post_p = branch == RC_BRANCH and head != CURRENT_STATUS_OID and len(p_positions) == 1
    s2_positions = tuple(
        index
        for index, commit in enumerate(first_parent_commits)
        if commit.oid == GENERATION3_STATUS_BASE_OID
    )
    post_s2 = branch == RC_BRANCH and head != GENERATION3_STATUS_BASE_OID and len(s2_positions) == 1
    checkpoint_oid: str | None = None
    if post_p and p_positions[0] + 1 < len(first_parent_commits):
        candidate = first_parent_commits[p_positions[0] + 1]
        related.append(_collect_commit(CONTAINMENT_OID))
        if len(candidate.parents) == 2:
            checkpoint_oid = candidate.oid
            transition = _collect_commit(candidate.parents[1])
            related.append(transition)
            containment_ancestor = _git("merge-base", "--is-ancestor", CONTAINMENT_OID, transition.oid)
            if containment_ancestor.returncode == 1:
                raise HarnessFailure(
                    "POLICY_REQUIRED_ANCESTOR_MISSING",
                    "containment commit is not an ancestor of the derived transition commit",
                )
            if containment_ancestor.returncode != 0:
                raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", "containment ancestry query failed")
            if transition.parents:
                related.append(_collect_commit(transition.parents[0]))
    elif len(first_parent_commits) >= 2 and first_parent_commits[0].oid == POLICY_BASE_OID:
        candidate = first_parent_commits[1]
        if len(candidate.parents) == 2:
            checkpoint_oid = candidate.oid
            related.append(_collect_commit(candidate.parents[1]))

    generation3_checkpoint_oid: str | None = None
    generation3_policy_feature_oid: str | None = None
    if post_s2 and s2_positions[0] + 1 < len(first_parent_commits):
        candidate = first_parent_commits[s2_positions[0] + 1]
        if len(candidate.parents) == 2:
            generation3_checkpoint_oid = candidate.oid
            generation3_policy_feature_oid = candidate.parents[1]
            policy_feature = _collect_commit(generation3_policy_feature_oid)
            related.append(policy_feature)
            if policy_feature.parents:
                related.append(_collect_commit(policy_feature.parents[0]))

    verified_ancestors: set[str] = set()
    for oid in (
        BASE_OID,
        SAFE_LINK_OID,
        FEATURE_OID,
        INTEGRATION_OID,
        POLICY_BASE_OID,
        PREVIOUS_CHECKPOINT_OID,
        CURRENT_STATUS_OID,
        CONTAINMENT_OID,
        GENERATION3_PREVIOUS_CHECKPOINT_OID,
        GENERATION3_STATUS_BASE_OID,
        PROVIDER_ROUTING_FEATURE_OID,
    ):
        result = _git("merge-base", "--is-ancestor", oid, head)
        if result.returncode == 0:
            verified_ancestors.add(oid)
        elif result.returncode != 1:
            raise HarnessFailure("POLICY_GIT_COMMAND_FAILED", f"ancestor query failed: {oid}")
    if generation3_policy_feature_oid is not None:
        result = _git("merge-base", "--is-ancestor", generation3_policy_feature_oid, head)
        if result.returncode == 0:
            verified_ancestors.add(generation3_policy_feature_oid)
        elif result.returncode != 1:
            raise HarnessFailure(
                "POLICY_GIT_COMMAND_FAILED",
                f"ancestor query failed: {generation3_policy_feature_oid}",
            )

    if generation3_checkpoint_oid is not None:
        current_paths = {path for path, _oid in GENERATION3_PROOF_BLOB_OIDS} | set(POLICY_PATHS)
        blobs = _collect_blobs(head, current_paths)
        checkpoint_blobs = _collect_blobs(generation3_checkpoint_oid, POLICY_PATHS)
    elif checkpoint_oid is None:
        blobs = _collect_blobs(head, TRUSTED_BLOB_OIDS)
        checkpoint_blobs: tuple[BlobRecord, ...] = ()
    else:
        current_paths = (set(TRUSTED_BLOB_OIDS) - {"scripts/test_evidence_provenance_real_backend.py"}) | set(
            POLICY_PATHS
        )
        blobs = _collect_blobs(head, current_paths)
        checkpoint_blobs = _collect_blobs(checkpoint_oid, POLICY_PATHS)
    containment_blobs = _collect_blobs(head, CONTAINMENT_PATHS) if post_p else ()
    provider_routing_blobs = _collect_blobs(head, PROVIDER_ROUTING_FEATURE_PATHS) if post_s2 else ()
    state = CheckoutRecord(
        branch=branch,
        head=head,
        head_commit=_collect_commit(head),
        first_parent_commits=first_parent_commits,
        related_commits=tuple(related),
        blobs=blobs,
        checkpoint_blobs=checkpoint_blobs,
        verified_ancestors=frozenset(verified_ancestors),
        tracked_clean=tracked_clean,
        index_empty=index_empty,
        protected_untracked_paths=protected,
        containment_blobs=containment_blobs,
        provider_routing_blobs=provider_routing_blobs,
    )
    return state, evaluate_checkout(state), owner_inventory


def _verify_owner_inventory(before: tuple[OwnerArtifactRecord, ...]) -> tuple[OwnerArtifactRecord, ...]:
    tracked_clean, index_empty, untracked = _worktree_status()
    if not tracked_clean:
        raise HarnessFailure("POLICY_TRACKED_DIRTY", "tracked worktree changed during harness execution")
    if not index_empty:
        raise HarnessFailure("POLICY_INDEX_NOT_EMPTY", "index changed during harness execution")
    protected = tuple(sorted(path for path in untracked if is_protected_untracked_path(path)))
    if protected:
        raise HarnessFailure("POLICY_UNTRACKED_PROTECTED_OVERLAP", f"protected untracked path: {protected[0]}")
    after = _owner_inventory(untracked)
    equal, changed = compare_owner_inventories(before, after)
    if not equal:
        raise HarnessFailure("POLICY_OWNER_ARTIFACT_DRIFT", f"owner artifact changed: {changed[0]}")
    return after


def _runtime_guard_snapshot(protected_repo: Path) -> dict[str, object]:
    script = REPO_ROOT / "scripts" / "runtime_data_guard.py"
    result = _run(
        (sys.executable, str(script), "--repo-root", str(protected_repo)),
        cwd=REPO_ROOT,
        env=_safe_base_environment(),
        timeout=60,
    )
    if result.returncode != 0:
        raise HarnessFailure("RUNTIME_ARTIFACT_GUARD_DRIFT", _tail(result.stderr))
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise HarnessFailure("RUNTIME_ARTIFACT_GUARD_DRIFT", "runtime guard returned a non-object")
    return payload


def _git_lines(repo: Path, *args: str) -> list[str]:
    result = _git(*args, cwd=repo)
    if result.returncode != 0:
        raise HarnessFailure("RUNTIME_ARTIFACT_GUARD_DRIFT", _tail(result.stderr))
    return result.stdout.splitlines()


def _protected_snapshot(protected_repo: Path) -> dict[str, object]:
    queue = {
        path.relative_to(protected_repo).as_posix(): _file_meta(path)
        for path in sorted((protected_repo / "data" / "queues").rglob("*"))
        if path.is_file()
    }
    ignored_raw = {
        relative: _file_meta(protected_repo / relative)
        for relative in sorted(
            _git_lines(protected_repo, "ls-files", "--others", "-i", "--exclude-standard", "--", "data/papers/raw")
        )
    }
    m50 = {relative: _file_meta(protected_repo / relative) for relative in M50_FILES}
    root_tmp = {
        path.name: _file_meta(path) for path in sorted(protected_repo.glob(".tmp_*")) if path.is_file()
    }
    sidecars = {
        relative: _file_meta(protected_repo / relative)
        for relative in (
            "storage/metadata_index.sqlite3-journal",
            "storage/metadata_index.sqlite3-wal",
            "storage/metadata_index.sqlite3-shm",
            "storage/scheduler.sqlite3-journal",
            "storage/scheduler.sqlite3-wal",
            "storage/scheduler.sqlite3-shm",
        )
    }
    return {
        "runtime": _runtime_guard_snapshot(protected_repo),
        "queue": queue,
        "ignoredRaw": ignored_raw,
        "m50": m50,
        "operatorAudit": _file_meta(protected_repo / "output" / "operator-audit" / "operator-audit.jsonl"),
        "sidecars": sidecars,
        "rootTmp": root_tmp,
        "output": _directory_meta(protected_repo / "output"),
        "storage": _directory_meta(protected_repo / "storage"),
        "protectedTestResults": _directory_meta(protected_repo / "web" / "test-results"),
        "protectedPlaywrightReport": _directory_meta(protected_repo / "web" / "playwright-report"),
        "featureTestResults": _directory_meta(REPO_ROOT / "web" / "test-results"),
        "featurePlaywrightReport": _directory_meta(REPO_ROOT / "web" / "playwright-report"),
        "vite": _vite_guard_snapshot(),
        "packages": {relative: _file_meta(REPO_ROOT / relative) for relative in PACKAGE_FILES},
        "protectedGit": _git_lines(protected_repo, "status", "--short"),
        "featureGit": _git_lines(REPO_ROOT, "status", "--short", "--untracked-files=all"),
        "featureStaged": _git_lines(REPO_ROOT, "diff", "--cached", "--name-only"),
        "portAvailable": _port_available(),
    }


def _frontend_source_fingerprint() -> str:
    result = _git(
        "ls-files",
        "--",
        "web/src",
        "web/index.html",
        "web/vite.config.ts",
        "web/tsconfig.json",
        "web/tsconfig.node.json",
        "web/package.json",
        "web/package-lock.json",
    )
    if result.returncode != 0:
        raise HarnessFailure("FRONTEND_BUILD_FAILED", _tail(result.stderr))
    digest = hashlib.sha256()
    for relative in sorted(result.stdout.splitlines()):
        path = REPO_ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((_sha256(path) or "missing").encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _build_frontend(*, skip_build: bool, summary: dict[str, object]) -> str:
    source_fingerprint = _frontend_source_fingerprint()
    marker = REPO_ROOT / "web" / "dist" / ".xrml-real-backend-source-fingerprint"
    before_vite = _capture_vite_config_identities()
    before_vite, status_observed, stat_refreshed, index_changed = (
        _refresh_vite_stat_cache_if_content_identical(before_vite)
    )
    summary["viteConfigStatusOnlyDriftObserved"] = bool(
        summary["viteConfigStatusOnlyDriftObserved"] or status_observed
    )
    summary["viteConfigStatCacheRefreshed"] = bool(summary["viteConfigStatCacheRefreshed"] or stat_refreshed)
    summary["viteConfigIndexBlobChanged"] = bool(summary["viteConfigIndexBlobChanged"] or index_changed)
    if skip_build:
        if not (REPO_ROOT / "web" / "dist" / "index.html").is_file():
            raise HarnessFailure("FRONTEND_BUILD_FAILED", "--skip-build requires web/dist/index.html")
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != source_fingerprint:
            raise HarnessFailure("FRONTEND_BUILD_FAILED", "--skip-build requires a matching source fingerprint")
        summary["viteConfigFinalClean"] = not any(
            identity.porcelain_status for identity in before_vite.values()
        )
        return source_fingerprint

    before_packages = {relative: _file_meta(REPO_ROOT / relative) for relative in PACKAGE_FILES}
    env = _safe_base_environment()
    env["WEB_APP_BASE_PATH"] = "/app"
    result = _run((str(_npm_command()), "--prefix", "web", "run", "build"), env=env, timeout=180)
    if result.returncode != 0:
        raise HarnessFailure("FRONTEND_BUILD_FAILED", _tail(f"{result.stdout}\n{result.stderr}"))
    after_vite = _capture_vite_config_identities()
    changed = _compare_vite_identity_sets(before_vite, after_vite)
    summary["viteConfigContentChanged"] = bool(changed)
    summary["viteConfigIndexBlobChanged"] = bool(
        summary["viteConfigIndexBlobChanged"]
        or any("index_blob" in fields for fields in changed.values())
    )
    if changed:
        raise HarnessFailure("VITE_GENERATED_CONFIG_DRIFT", f"tracked Vite identity changed during build: {changed}")
    after_vite, status_observed, stat_refreshed, index_changed = (
        _refresh_vite_stat_cache_if_content_identical(after_vite)
    )
    summary["viteConfigStatusOnlyDriftObserved"] = bool(
        summary["viteConfigStatusOnlyDriftObserved"] or status_observed
    )
    summary["viteConfigStatCacheRefreshed"] = bool(summary["viteConfigStatCacheRefreshed"] or stat_refreshed)
    summary["viteConfigIndexBlobChanged"] = bool(summary["viteConfigIndexBlobChanged"] or index_changed)
    summary["viteConfigFinalClean"] = not any(identity.porcelain_status for identity in after_vite.values())
    if before_packages != {relative: _file_meta(REPO_ROOT / relative) for relative in PACKAGE_FILES}:
        raise HarnessFailure("PACKAGE_OR_LOCKFILE_CHANGE_REQUIRED", "package or lockfile changed during build")
    dist = REPO_ROOT / "web" / "dist"
    if not (dist / "index.html").is_file() or not any((dist / "assets").glob("*")):
        raise HarnessFailure("FRONTEND_BUILD_FAILED", "web/dist is incomplete after build")
    marker.write_text(source_fingerprint + "\n", encoding="utf-8")
    return source_fingerprint


def _fixture_manifest(temp_root: Path) -> SeedManifest:
    helper = REPO_ROOT / "tests" / "helpers" / "evidence_provenance_fixture.py"
    result = _run(
        (sys.executable, str(helper), "--root", str(temp_root), "--json"),
        env=_fixture_environment(temp_root),
        timeout=60,
    )
    if result.returncode != 0:
        raise HarnessFailure("FIXTURE_SELF_TEST_FAILED", _tail(result.stderr))
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise HarnessFailure("FIXTURE_SELF_TEST_FAILED", "fixture helper returned a non-object")
    manifest = SeedManifest.from_payload(payload)
    verify_evidence_provenance_overlay(temp_root, manifest)
    return manifest


def _assert_runtime_paths(temp_root: Path, env: Mapping[str, str]) -> dict[str, str]:
    code = """
import json
import os
from pathlib import Path
from app.domain import constants
from app.services import operator_audit, topic_analytics, topic_profiles, topic_schedules
root = constants.STORAGE_ROOT.parent
print(json.dumps({
  'metadataDb': str(root / 'metadata_index.sqlite3'),
  'schedulerDb': str(root / 'scheduler.sqlite3'),
  'runsRoot': str(constants.STORAGE_ROOT),
  'topicProfiles': str(topic_profiles.PROFILE_LIBRARY_PATH),
  'topicAnalytics': str(topic_analytics.ANALYTICS_LIBRARY_DIR),
  'topicSchedules': str(topic_schedules.SCHEDULE_LIBRARY_DIR),
  'operatorAudit': str(operator_audit._audit_log_path()),
}, sort_keys=True))
"""
    result = _run((sys.executable, "-c", code), env=env, timeout=30)
    if result.returncode != 0:
        raise HarnessFailure("REPO_RUNTIME_PATH_RESOLUTION", _tail(result.stderr))
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise HarnessFailure("REPO_RUNTIME_PATH_RESOLUTION", "runtime path probe returned a non-object")
    resolved_root = temp_root.resolve()
    for name, raw_path in payload.items():
        path = Path(str(raw_path)).expanduser().resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise HarnessFailure("REPO_RUNTIME_PATH_RESOLUTION", f"{name} resolved outside temp root") from exc
    return {str(name): str(Path(str(value)).resolve().relative_to(resolved_root)) for name, value in payload.items()}


def _wait_for_health(process: subprocess.Popen[str], stderr_path: Path, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = _tail(stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else "")
            raise HarnessFailure("FASTAPI_START_FAILED", f"backend exited before readiness:\n{tail}")
        try:
            status, payload = _http_json("/health", timeout=1.0)
            if status == 200 and payload == {"status": "ok"}:
                return
        except (OSError, urllib.error.URLError, HarnessFailure, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    tail = _tail(stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else "")
    raise HarnessFailure("FASTAPI_START_FAILED", f"backend readiness timed out:\n{tail}")


def _http_json(path: str, *, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    url = urllib.parse.urljoin(f"{ORIGIN}/", path.lstrip("/"))
    if urllib.parse.urlparse(url).scheme != "http" or urllib.parse.urlparse(url).netloc != f"{HOST}:{PORT}":
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", f"non-loopback URL rejected: {url}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", f"oversized response from {path}")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", f"non-object JSON from {path}")
        return int(response.status), payload


def _assert_no_path_or_secret_leak(payload: object) -> None:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    forbidden = (
        str(REPO_ROOT),
        "C:\\Users\\",
        "D:\\dev\\repos\\",
        "/home/",
        ".env",
        ".mcp.json",
        "BEGIN PRIVATE KEY",
        "Bearer ",
        "raw provider output",
    )
    if any(fragment in serialized for fragment in forbidden):
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "absolute path or secret-like text leaked from evidence API")


def _api_preflight(manifest: SeedManifest) -> dict[str, object]:
    auth_status, auth = _http_json("/auth/me")
    expected_auth = {
        "auth_mode": "disabled",
        "authenticated": True,
        "is_admin": True,
        "product_session_backend": "local_internal",
        "login_url": None,
    }
    if auth_status != 200 or any(auth.get(key) != value for key, value in expected_auth.items()):
        raise HarnessFailure("AUTH_DISABLED_CONTRACT_MISMATCH", "real /auth/me contract mismatch")
    user = auth.get("user")
    if not isinstance(user, dict) or user.get("user_id") != "local-internal-user" or user.get("auth_source") != "disabled":
        raise HarnessFailure("AUTH_DISABLED_CONTRACT_MISMATCH", "real /auth/me user mismatch")
    if not any("internal" in str(item).lower() for item in auth.get("warnings", [])):
        raise HarnessFailure("AUTH_DISABLED_CONTRACT_MISMATCH", "real /auth/me local warning is missing")

    topics_status, topics = _http_json("/api/topics")
    topic_items = topics.get("items")
    if topics_status != 200 or not isinstance(topic_items, list) or not any(
        isinstance(item, dict) and item.get("id") == TOPIC_ID for item in topic_items
    ):
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "real /api/topics omitted the synthetic topic")

    browse_status, browse = _http_json("/api/evidence/search")
    browse_items = browse.get("items")
    if browse_status != 200 or not isinstance(browse_items, list):
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "real evidence browse failed")
    paper = next(
        (item for item in browse_items if isinstance(item, dict) and item.get("canonicalPaperId") == manifest.canonicalPaperId),
        None,
    )
    report = next(
        (item for item in browse_items if isinstance(item, dict) and item.get("title") == REPORT_SECTION_TITLE),
        None,
    )
    if paper is None or report is None or "report_provenance_not_indexed" not in report.get("warnings", []):
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "browse result identities/warning mismatch")

    query = urllib.parse.urlencode(
        {
            "q": "spatial",
            "topic_ids": TOPIC_ID,
            "corpus_kinds": "paper",
            "review_levels": "full_text_reviewed",
            "has_evidence": "true",
        }
    )
    filtered_status, filtered = _http_json(f"/api/evidence/search?{query}")
    filtered_items = filtered.get("items")
    if filtered_status != 200 or not isinstance(filtered_items, list) or len(filtered_items) != 1:
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "filtered evidence search did not return exactly one result")
    filtered_paper = filtered_items[0]
    if (
        not isinstance(filtered_paper, dict)
        or filtered_paper.get("resultId") != manifest.resultId
        or filtered_paper.get("canonicalPaperId") != manifest.canonicalPaperId
        or "full_text_reviewed" not in filtered_paper.get("statuses", [])
        or int(filtered_paper.get("inlineEvidenceSpanCount") or 0) != 1
        or int(filtered_paper.get("runEvidenceCount") or 0) != 1
    ):
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "filtered evidence result contract mismatch")

    encoded_result_id = urllib.parse.quote(manifest.resultId, safe="")
    detail_status, detail = _http_json(f"/api/evidence/items/{encoded_result_id}")
    truth_levels = {
        str(edge.get("truthLevel"))
        for edge in detail.get("provenanceEdges", [])
        if isinstance(edge, dict)
    }
    if detail_status != 200 or detail.get("resultId") != manifest.resultId:
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "real evidence detail identity mismatch")
    if truth_levels != {
        "exact_persisted",
        "exact_indexed_projection",
        "exact_portable_ref",
        "derived_deterministic",
    }:
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", f"unexpected detail truth levels: {sorted(truth_levels)}")
    portable_refs = {
        str(item.get("portableRef"))
        for item in detail.get("artifactVerifications", [])
        if isinstance(item, dict) and item.get("portableRef")
    }
    if PORTABLE_ARTIFACT_REF not in portable_refs or detail.get("providerReport") is not None:
        raise HarnessFailure("REAL_API_PREFLIGHT_FAILED", "portable artifact/provider boundary mismatch")
    _assert_no_path_or_secret_leak({"browse": browse, "filtered": filtered, "detail": detail})
    return {
        "auth": "passed",
        "topics": "passed",
        "browse": "passed",
        "filteredSearch": "passed",
        "detail": "passed",
        "browseResultCount": len(browse_items),
        "filteredResultCount": len(filtered_items),
        "truthLevels": sorted(truth_levels),
    }


def _validate_playwright_source() -> None:
    spec = REPO_ROOT / "web" / "tests" / "real-backend" / "evidence-provenance.real-backend.spec.ts"
    text = spec.read_text(encoding="utf-8")
    forbidden = (".fulfill(", "installEvidenceMocks", "fakeAuth", "fakeTopics", "fakeSearch", "fakeDetail")
    matches = [token for token in forbidden if token in text]
    if matches:
        raise HarnessFailure("SAME_ORIGIN_API_MOCK_OBSERVED", f"forbidden Playwright source tokens: {matches}")


def _run_playwright(temp_root: Path) -> tuple[dict[str, object], str]:
    _validate_playwright_source()
    output_dir = temp_root / "playwright-output"
    output_dir.mkdir(parents=True, exist_ok=False)
    env = _runtime_environment(temp_root)
    env.update(
        {
            "XRML_REAL_BACKEND_BASE_URL": ORIGIN,
            "XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR": str(output_dir),
        }
    )
    command = (
        str(_playwright_command()),
        "test",
        "--config",
        "playwright.real-backend.config.ts",
        "tests/real-backend/evidence-provenance.real-backend.spec.ts",
    )
    result = _run(command, cwd=REPO_ROOT / "web", env=env, timeout=120)
    output = f"{result.stdout}\n{result.stderr}"
    ledger_lines = [line.split("XRML_REAL_BACKEND_LEDGER=", 1)[1] for line in output.splitlines() if "XRML_REAL_BACKEND_LEDGER=" in line]
    if result.returncode != 0:
        raise HarnessFailure("REAL_BACKEND_PLAYWRIGHT_FAILED", _tail(output, lines=50))
    if len(ledger_lines) != 1:
        raise HarnessFailure("REAL_BACKEND_PLAYWRIGHT_FAILED", "Playwright did not emit exactly one request ledger")
    ledger = json.loads(ledger_lines[0])
    if not isinstance(ledger, dict):
        raise HarnessFailure("REAL_BACKEND_PLAYWRIGHT_FAILED", "Playwright ledger is not an object")
    if int(ledger.get("sameOriginIntercepted") or 0) != 0:
        raise HarnessFailure("SAME_ORIGIN_API_MOCK_OBSERVED", "same-origin route interception was observed")
    if int(ledger.get("offOriginAttempts") or 0) != 0:
        raise HarnessFailure("OFF_ORIGIN_REQUEST_OBSERVED", "an off-origin browser request was attempted")
    if int(ledger.get("mutationRequests") or 0) != 0:
        raise HarnessFailure("MUTATION_REQUEST_OBSERVED", "a browser mutation request was observed")
    if int(ledger.get("forbiddenRequests") or 0) != 0:
        raise HarnessFailure(
            "PROVIDER_OAUTH_MCP_OR_NETWORK_REQUEST_OBSERVED",
            "a provider/OAuth/MCP/tunnel/unexpected API request was observed",
        )
    return ledger, "passed"


def _start_backend(temp_root: Path, env: Mapping[str, str]) -> tuple[subprocess.Popen[str], Path, Path]:
    logs = temp_root / "logs"
    logs.mkdir(parents=True, exist_ok=False)
    stdout_path = logs / "backend.stdout.log"
    stderr_path = logs / "backend.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    kwargs: dict[str, object] = {
        "cwd": REPO_ROOT,
        "env": dict(env),
        "stdout": stdout_handle,
        "stderr": stderr_handle,
        "text": True,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
            **kwargs,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return process, stdout_path, stderr_path


def _stop_backend(process: subprocess.Popen[str] | None) -> str:
    if process is None:
        return "not_started"
    if process.poll() is not None:
        return "already_exited"
    mode = "graceful"
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        mode = "forced_owned_tree"
        if os.name == "nt":
            _run(("taskkill", "/PID", str(process.pid), "/T", "/F"), env=_safe_base_environment(), timeout=15)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired as exc:
            raise HarnessFailure("PROCESS_LEAK", f"owned backend PID {process.pid} did not stop") from exc
    if process.poll() is None:
        raise HarnessFailure("PROCESS_LEAK", f"owned backend PID {process.pid} remains alive")
    return mode


def _wait_for_port_release(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_available():
            return
        time.sleep(0.1)
    raise HarnessFailure("PORT_NOT_RELEASED", f"port {PORT} was not released")


def _sentinel_payload() -> dict[str, object]:
    return {
        "schemaVersion": FIXTURE_SCHEMA_VERSION,
        "synthetic": True,
        "fixtureKind": FIXTURE_KIND,
        "fixtureId": FIXTURE_ID,
        "expectedRunId": RUN_ID,
        "expectedPaperId": PAPER_ID,
        "createdBy": CREATED_BY,
    }


def _cleanup_temp_root(temp_root: Path) -> None:
    resolved = temp_root.resolve()
    temp_parent = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_parent)
    except ValueError as exc:
        raise HarnessFailure("TEMP_OVERLAY_CLEANUP_FAILED", "temp root resolved outside OS temp") from exc
    sentinel = resolved / "fixture-sentinel.json"
    if not sentinel.is_file() and resolved.is_dir() and not any(resolved.iterdir()):
        sentinel.write_text(json.dumps(_sentinel_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not sentinel.is_file() or json.loads(sentinel.read_text(encoding="utf-8")) != _sentinel_payload():
        raise HarnessFailure("TEMP_OVERLAY_CLEANUP_FAILED", "fixture sentinel mismatch before cleanup")
    shutil.rmtree(resolved)
    if resolved.exists():
        raise HarnessFailure("TEMP_OVERLAY_CLEANUP_FAILED", "temp root still exists after cleanup")


def _run_fixture_self_test() -> dict[str, object]:
    helper = REPO_ROOT / "tests" / "helpers" / "evidence_provenance_fixture.py"
    result = _run((sys.executable, str(helper), "--self-test"), env=_safe_base_environment(), timeout=120)
    if result.returncode != 0:
        raise HarnessFailure("FIXTURE_SELF_TEST_FAILED", _tail(result.stderr))
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or payload.get("status") != "passed":
        raise HarnessFailure("FIXTURE_SELF_TEST_FAILED", "fixture self-test summary mismatch")
    return payload


def _run_regressions() -> dict[str, str]:
    npm = str(_npm_command())
    env = _safe_base_environment()
    commands = {
        "mockedSmoke": (npm, "--prefix", "web", "run", "test:e2e:smoke"),
        "focusedFrontend": (
            npm,
            "--prefix",
            "web",
            "test",
            "--",
            "--run",
            "src/lib/__tests__/api.evidence.test.ts",
            "src/screens/__tests__/EvidenceProvenanceScreen.test.tsx",
            "src/__tests__/AppAuth.test.tsx",
        ),
        "focusedBackend": (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_evidence_search_repository.py",
            "tests/test_evidence_search_api.py",
            "tests/test_auth_and_mcp.py::test_auth_disabled_mode_preserves_local_internal_access",
        ),
    }
    results: dict[str, str] = {}
    for name, command in commands.items():
        timeout = 180 if name != "focusedBackend" else 300
        result = _run(command, env=env, timeout=timeout)
        if result.returncode != 0:
            reason = {
                "mockedSmoke": "EXISTING_MOCKED_SMOKE_FAILED",
                "focusedFrontend": "FRONTEND_REGRESSION_FAILED",
                "focusedBackend": "BACKEND_FOCUSED_FAILED",
            }[name]
            raise HarnessFailure(reason, _tail(f"{result.stdout}\n{result.stderr}", lines=50))
        results[name] = "passed"
    return results


def _proof_limitations() -> list[str]:
    return [
        "local_loopback_only",
        "synthetic_fixture_only",
        "not_staging_or_production_proof",
        "not_oauth_or_public_mcp_proof",
        "not_provider_or_external_network_proof",
        "not_scholarly_corpus_or_latency_proof",
    ]


SECONDARY_FAILURE_CODES = {
    "RUNTIME_ARTIFACT_GUARD_DRIFT": "SECONDARY_RUNTIME_GUARD_DRIFT",
    "QUEUE_ARTIFACT_DRIFT": "SECONDARY_QUEUE_GUARD_DRIFT",
    "RAW_PAPER_HASH_DRIFT": "SECONDARY_RAW_GUARD_DRIFT",
    "M50_HASH_DRIFT": "SECONDARY_M50_GUARD_DRIFT",
    "OPERATOR_AUDIT_REPO_FILE_DRIFT": "SECONDARY_OPERATOR_AUDIT_DRIFT",
    "PROCESS_LEAK": "SECONDARY_PROCESS_CLEANUP_FAILED",
    "PORT_NOT_RELEASED": "SECONDARY_PORT_RELEASE_FAILED",
    "TEMP_OVERLAY_CLEANUP_FAILED": "SECONDARY_TEMP_CLEANUP_FAILED",
    "GENERATED_ARTIFACT_CREATED": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "DIFF_SCOPE_EXCEEDED": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "STAGED_FILES_PRESENT": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "WORKTREE_TRACKED_DIRTY": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "POLICY_TRACKED_DIRTY": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "POLICY_INDEX_NOT_EMPTY": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "POLICY_UNTRACKED_PROTECTED_OVERLAP": "SECONDARY_GENERATED_ARTIFACT_DRIFT",
    "POLICY_OWNER_ARTIFACT_DRIFT": "SECONDARY_OWNER_ARTIFACT_DRIFT",
}


def _secondary_failure_code(stop_reason: str) -> str:
    return SECONDARY_FAILURE_CODES.get(stop_reason, "SECONDARY_GENERATED_ARTIFACT_DRIFT")


def _resolve_failure_precedence(
    primary_failure: str | None,
    post_failure_reasons: Sequence[str],
) -> FailureResolution:
    if primary_failure is not None:
        return FailureResolution(
            primary_failure=primary_failure,
            secondary_failure_codes=tuple(_secondary_failure_code(reason) for reason in post_failure_reasons),
        )
    if not post_failure_reasons:
        return FailureResolution(primary_failure=None, secondary_failure_codes=())
    return FailureResolution(
        primary_failure=post_failure_reasons[0],
        secondary_failure_codes=tuple(
            _secondary_failure_code(reason) for reason in post_failure_reasons[1:]
        ),
    )


def _run_contract_self_check() -> dict[str, object]:
    def commit(
        oid: str,
        parents: tuple[str, ...],
        subject: str,
        paths: frozenset[str],
        tree: str,
        statuses: tuple[tuple[str, str], ...] = (),
    ) -> CommitRecord:
        return CommitRecord(oid, parents, subject, paths, tree, statuses)

    base = commit(BASE_OID, (), "base", frozenset(), "0" * 40)
    safe = commit(SAFE_LINK_OID, (BASE_OID,), SAFE_LINK_SUBJECT, SAFE_LINK_PATHS, "1" * 40)
    feature = commit(FEATURE_OID, (SAFE_LINK_OID,), FEATURE_SUBJECT, FEATURE_PATHS, "2" * 40)
    integration = commit(
        INTEGRATION_OID,
        (BASE_OID, FEATURE_OID),
        INTEGRATION_SUBJECT,
        INTEGRATION_PATHS,
        "3" * 40,
    )
    status = commit(POLICY_BASE_OID, (INTEGRATION_OID,), "status", frozenset({"STATUS.md"}), "4" * 40)
    policy_feature_oid = "f" * 40
    policy_merge_oid = "e" * 40
    policy_status_oid = "c" * 40
    policy_feature = commit(
        policy_feature_oid,
        (POLICY_BASE_OID,),
        POLICY_FEATURE_SUBJECT,
        POLICY_PATHS,
        "5" * 40,
    )
    policy_merge = commit(
        policy_merge_oid,
        (POLICY_BASE_OID, policy_feature_oid),
        POLICY_MERGE_SUBJECT,
        POLICY_PATHS,
        "5" * 40,
    )
    policy_status = commit(
        policy_status_oid,
        (policy_merge_oid,),
        "status",
        frozenset({"STATUS.md"}),
        "6" * 40,
    )
    historical_blobs = tuple(
        BlobRecord(path, oid, TRUSTED_BLOB_SHA256.get(path)) for path, oid in sorted(TRUSTED_BLOB_OIDS.items())
    )
    checkpoint_blobs = tuple(
        sorted(
            (
                *(
                    BlobRecord(path, oid, TRUSTED_BLOB_SHA256.get(path))
                    for path, oid in TRUSTED_BLOB_OIDS.items()
                    if path != "scripts/test_evidence_provenance_real_backend.py"
                ),
                BlobRecord("scripts/test_evidence_provenance_real_backend.py", "6" * 40),
                BlobRecord("scripts/evidence_provenance_harness_git_policy.py", "7" * 40),
                BlobRecord("tests/test_evidence_provenance_real_backend_harness_policy.py", "8" * 40),
            ),
            key=lambda item: item.path,
        )
    )
    feature_state = CheckoutRecord(
        SOURCE_BRANCH,
        FEATURE_OID,
        feature,
        (),
        (base, safe),
        historical_blobs,
        (),
        frozenset({BASE_OID, SAFE_LINK_OID}),
        True,
        True,
    )
    merge_state = CheckoutRecord(
        RC_BRANCH,
        INTEGRATION_OID,
        integration,
        (),
        (base, safe, feature),
        historical_blobs,
        (),
        frozenset({BASE_OID, SAFE_LINK_OID, FEATURE_OID}),
        True,
        True,
    )
    status_state = CheckoutRecord(
        RC_BRANCH,
        POLICY_BASE_OID,
        status,
        (status,),
        (base, safe, feature, integration),
        historical_blobs,
        (),
        frozenset({BASE_OID, SAFE_LINK_OID, FEATURE_OID, INTEGRATION_OID}),
        True,
        True,
    )
    checkpoint_state = CheckoutRecord(
        RC_BRANCH,
        policy_merge_oid,
        policy_merge,
        (status, policy_merge),
        (base, safe, feature, integration, policy_feature),
        checkpoint_blobs,
        checkpoint_blobs,
        frozenset({BASE_OID, SAFE_LINK_OID, FEATURE_OID, INTEGRATION_OID, POLICY_BASE_OID}),
        True,
        True,
    )
    policy_status_state = replace(
        checkpoint_state,
        head=policy_status_oid,
        head_commit=policy_status,
        first_parent_commits=(status, policy_merge, policy_status),
    )
    accepted = tuple(
        evaluate_checkout(state)
        for state in (feature_state, merge_state, status_state, checkpoint_state, policy_status_state)
    )
    assert all(decision.accepted for decision in accepted)
    assert all(
        decision.execution_mode is ExecutionMode.HISTORICAL_PROVENANCE_ONLY for decision in accepted[:3]
    )
    assert all(decision.execution_mode is ExecutionMode.FULL_HARNESS_ALLOWED for decision in accepted[3:])

    changed_blobs = tuple(
        BlobRecord(item.path, "9" * 40 if item.path.startswith("web/src/components/") else item.oid, item.sha256)
        for item in historical_blobs
    )
    rejected_policy_cases = (
        replace(feature_state, branch="revive/unrelated"),
        replace(feature_state, head="9" * 40, head_commit=replace(feature, oid="9" * 40)),
        replace(merge_state, head_commit=replace(integration, parents=("9" * 40, FEATURE_OID))),
        replace(
            merge_state,
            head_commit=replace(integration, changed_paths=INTEGRATION_PATHS | {"README.md"}),
        ),
        replace(status_state, blobs=changed_blobs),
        replace(
            status_state,
            head_commit=replace(status, changed_paths=frozenset({"STATUS.md", "app/main.py"})),
            first_parent_commits=(replace(status, changed_paths=frozenset({"STATUS.md", "app/main.py"})),),
        ),
        replace(status_state, tracked_clean=False),
        replace(status_state, protected_untracked_paths=("scripts/untracked.py",)),
        replace(status_state, verified_ancestors=status_state.verified_ancestors - {SAFE_LINK_OID}),
    )
    assert all(not evaluate_checkout(state).accepted for state in rejected_policy_cases)
    parser_options = {option for action in _build_parser()._actions for option in action.option_strings}
    assert "--expected-head" not in parser_options
    assert "--allow-current-head" not in parser_options

    def modified(paths: frozenset[str]) -> tuple[tuple[str, str], ...]:
        return tuple((path, "M") for path in sorted(paths))

    def v2_commit(
        oid: str,
        parents: tuple[str, ...],
        subject: str,
        paths: frozenset[str],
        tree: str,
        statuses: tuple[tuple[str, str], ...] | None = None,
    ) -> CommitRecord:
        return commit(oid, parents, subject, paths, tree, modified(paths) if statuses is None else statuses)

    transition_oid = "9" * 40
    checkpoint_two_oid = "8" * 40
    checkpoint_status_oid = "7" * 40
    checkpoint_status_two_oid = "6" * 40
    unrelated_oid = "d" * 40
    transition_tree = "a" * 40
    previous_feature = v2_commit(
        PREVIOUS_POLICY_FEATURE_OID,
        (POLICY_BASE_OID,),
        POLICY_FEATURE_SUBJECT,
        POLICY_PATHS,
        "5" * 40,
    )
    previous_checkpoint = v2_commit(
        PREVIOUS_CHECKPOINT_OID,
        (POLICY_BASE_OID, PREVIOUS_POLICY_FEATURE_OID),
        POLICY_MERGE_SUBJECT,
        POLICY_PATHS,
        "5" * 40,
    )
    current_status = v2_commit(
        CURRENT_STATUS_OID,
        (PREVIOUS_CHECKPOINT_OID,),
        CURRENT_STATUS_SUBJECT,
        frozenset({"STATUS.md"}),
        CURRENT_STATUS_TREE,
    )
    containment = v2_commit(
        CONTAINMENT_OID,
        (CURRENT_STATUS_OID,),
        CONTAINMENT_SUBJECT,
        CONTAINMENT_PATHS,
        CONTAINMENT_TREE,
    )
    transition = v2_commit(
        transition_oid,
        (CONTAINMENT_OID,),
        TRANSITION_SUBJECT,
        POLICY_PATHS,
        transition_tree,
    )
    checkpoint_two = v2_commit(
        checkpoint_two_oid,
        (CURRENT_STATUS_OID, transition_oid),
        CHECKPOINT_TWO_SUBJECT,
        CHECKPOINT_TWO_PATHS,
        transition_tree,
    )
    checkpoint_status = v2_commit(
        checkpoint_status_oid,
        (checkpoint_two_oid,),
        CHECKPOINT_STATUS_SUBJECT,
        frozenset({"STATUS.md"}),
        "b" * 40,
    )
    checkpoint_status_two = v2_commit(
        checkpoint_status_two_oid,
        (checkpoint_status_oid,),
        "Record later provider containment validation",
        frozenset({"STATUS.md"}),
        "c" * 40,
    )
    containment_blobs = tuple(
        BlobRecord(path, oid) for path, oid in sorted(CONTAINMENT_BLOB_OIDS.items())
    )
    v2_ancestors = frozenset(
        {
            BASE_OID,
            SAFE_LINK_OID,
            FEATURE_OID,
            INTEGRATION_OID,
            POLICY_BASE_OID,
            PREVIOUS_CHECKPOINT_OID,
            CURRENT_STATUS_OID,
            CONTAINMENT_OID,
        }
    )
    v2_related = (base, safe, feature, integration, previous_feature, containment, transition)

    def v2_state(later: tuple[CommitRecord, ...] = ()) -> CheckoutRecord:
        first_parent = (status, previous_checkpoint, current_status, checkpoint_two, *later)
        head_commit = first_parent[-1]
        return CheckoutRecord(
            branch=RC_BRANCH,
            head=head_commit.oid,
            head_commit=head_commit,
            first_parent_commits=first_parent,
            related_commits=v2_related,
            blobs=checkpoint_blobs,
            checkpoint_blobs=checkpoint_blobs,
            containment_blobs=containment_blobs,
            verified_ancestors=v2_ancestors,
            tracked_clean=True,
            index_empty=True,
        )

    previous_checkpoint_state = CheckoutRecord(
        branch=RC_BRANCH,
        head=PREVIOUS_CHECKPOINT_OID,
        head_commit=previous_checkpoint,
        first_parent_commits=(status, previous_checkpoint),
        related_commits=(base, safe, feature, integration, previous_feature),
        blobs=checkpoint_blobs,
        checkpoint_blobs=checkpoint_blobs,
        containment_blobs=(),
        verified_ancestors=v2_ancestors - {CURRENT_STATUS_OID, CONTAINMENT_OID},
        tracked_clean=True,
        index_empty=True,
    )
    current_status_state = replace(
        previous_checkpoint_state,
        head=CURRENT_STATUS_OID,
        head_commit=current_status,
        first_parent_commits=(status, previous_checkpoint, current_status),
        verified_ancestors=v2_ancestors - {CONTAINMENT_OID},
    )
    checkpoint_two_state = v2_state()
    checkpoint_status_state = v2_state((checkpoint_status,))
    multiple_status_state = v2_state((checkpoint_status, checkpoint_status_two))

    checkpoint_two_decision = evaluate_checkout(checkpoint_two_state)
    checkpoint_status_decision = evaluate_checkout(checkpoint_status_state)
    multiple_status_decision = evaluate_checkout(multiple_status_state)
    previous_checkpoint_decision = evaluate_checkout(previous_checkpoint_state)
    current_status_decision = evaluate_checkout(current_status_state)

    prefix = checkpoint_two_state.first_parent_commits[:3]
    containment_state = replace(
        checkpoint_two_state,
        head=CONTAINMENT_OID,
        head_commit=containment,
        first_parent_commits=(*prefix, containment),
    )
    transition_state = replace(
        checkpoint_two_state,
        head=transition_oid,
        head_commit=transition,
        first_parent_commits=(*prefix, containment, transition),
    )

    def replace_commit_record(
        state: CheckoutRecord,
        oid: str,
        replacement: CommitRecord,
    ) -> CheckoutRecord:
        return replace(
            state,
            head_commit=replacement if state.head_commit.oid == oid else state.head_commit,
            first_parent_commits=tuple(
                replacement if item.oid == oid else item for item in state.first_parent_commits
            ),
            related_commits=tuple(
                replacement if item.oid == oid else item for item in state.related_commits
            ),
        )

    mutation_states: dict[str, CheckoutRecord] = {}
    mutation_states["t-parent-not-pc"] = replace_commit_record(
        checkpoint_two_state,
        transition_oid,
        replace(transition, parents=(unrelated_oid,)),
    )
    mutation_states["t-subject-mismatch"] = replace_commit_record(
        checkpoint_two_state,
        transition_oid,
        replace(transition, subject="Unexpected transition"),
    )
    transition_extra_paths = POLICY_PATHS | {"README.md"}
    mutation_states["t-fourth-path"] = replace_commit_record(
        checkpoint_two_state,
        transition_oid,
        replace(
            transition,
            changed_paths=transition_extra_paths,
            changed_path_statuses=modified(transition_extra_paths),
        ),
    )
    mutation_states["pc-subject-mismatch"] = replace_commit_record(
        checkpoint_two_state,
        CONTAINMENT_OID,
        replace(containment, subject="Unexpected containment"),
    )
    mutation_states["pc-tree-mismatch"] = replace_commit_record(
        checkpoint_two_state,
        CONTAINMENT_OID,
        replace(containment, tree_oid="c" * 40),
    )
    mutation_states["pc-provider-blob-mismatch"] = replace(
        checkpoint_two_state,
        containment_blobs=tuple(
            BlobRecord(item.path, "c" * 40 if item.path == "app/services/providers/__init__.py" else item.oid)
            for item in containment_blobs
        ),
    )
    mutation_states["pc-test-blob-mismatch"] = replace(
        checkpoint_two_state,
        containment_blobs=tuple(
            BlobRecord(
                item.path,
                "c" * 40 if item.path == "tests/test_provider_output_class_proof.py" else item.oid,
            )
            for item in containment_blobs
        ),
    )
    mutation_states["m2-first-parent-not-ps1"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, parents=(unrelated_oid, transition_oid)),
    )
    mutation_states["m2-second-parent-not-t"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, parents=(CURRENT_STATUS_OID, unrelated_oid)),
    )
    mutation_states["m2-parent-order-reversed"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, parents=(transition_oid, CURRENT_STATUS_OID)),
    )
    mutation_states["m2-subject-mismatch"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, subject="Unexpected checkpoint"),
    )
    mutation_states["m2-tree-not-t"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, tree_oid="c" * 40),
    )
    missing_paths = CHECKPOINT_TWO_PATHS - {"app/services/providers/__init__.py"}
    mutation_states["m2-missing-containment-path"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(
            checkpoint_two,
            changed_paths=missing_paths,
            changed_path_statuses=modified(missing_paths),
        ),
    )
    extra_paths = CHECKPOINT_TWO_PATHS | {"README.md"}
    mutation_states["m2-sixth-path"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(
            checkpoint_two,
            changed_paths=extra_paths,
            changed_path_statuses=modified(extra_paths),
        ),
    )
    merge_time_statuses = tuple(
        (path, "A" if path == "app/services/providers/__init__.py" else "M")
        for path in sorted(CHECKPOINT_TWO_PATHS)
    )
    mutation_states["m2-merge-time-edit"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, changed_path_statuses=merge_time_statuses),
    )
    post_source = v2_commit(
        unrelated_oid,
        (checkpoint_two_oid,),
        "Change source after checkpoint",
        frozenset({"app/main.py"}),
        "c" * 40,
    )
    mutation_states["post-m2-source-commit"] = replace(
        checkpoint_two_state,
        head=unrelated_oid,
        head_commit=post_source,
        first_parent_commits=(*checkpoint_two_state.first_parent_commits, post_source),
    )
    post_merge = v2_commit(
        unrelated_oid,
        (checkpoint_two_oid, transition_oid),
        "Merge after checkpoint",
        frozenset({"STATUS.md"}),
        "c" * 40,
    )
    mutation_states["post-m2-merge"] = replace(
        checkpoint_two_state,
        head=unrelated_oid,
        head_commit=post_merge,
        first_parent_commits=(*checkpoint_two_state.first_parent_commits, post_merge),
    )
    status_plus_source_paths = frozenset({"STATUS.md", "app/main.py"})
    status_plus_source = v2_commit(
        checkpoint_status_oid,
        (checkpoint_two_oid,),
        CHECKPOINT_STATUS_SUBJECT,
        status_plus_source_paths,
        "b" * 40,
    )
    mutation_states["s2-status-plus-source"] = replace_commit_record(
        checkpoint_status_state,
        checkpoint_status_oid,
        status_plus_source,
    )
    mutation_states["stale-transition-record"] = replace(
        checkpoint_two_state,
        first_parent_commits=checkpoint_two_state.first_parent_commits[:-1],
    )
    mutation_states["ambiguous-old-new-context"] = replace(
        checkpoint_two_state,
        related_commits=(
            *checkpoint_two_state.related_commits,
            replace(transition, subject="Conflicting transition record"),
        ),
    )
    mutation_states["malformed-git-facts"] = replace_commit_record(
        checkpoint_two_state,
        checkpoint_two_oid,
        replace(checkpoint_two, tree_oid="not-a-tree"),
    )
    mutation_states["owner-runtime-path-regression"] = replace(
        checkpoint_two_state,
        protected_untracked_paths=("scripts/runtime-guard-drift.py",),
    )
    expected_mutation_reasons = {
        "t-parent-not-pc": "POLICY_PARENT_MISMATCH",
        "t-subject-mismatch": "POLICY_SUBJECT_MISMATCH",
        "t-fourth-path": "POLICY_PATH_CLOSURE_VIOLATION",
        "pc-subject-mismatch": "POLICY_SUBJECT_MISMATCH",
        "pc-tree-mismatch": "POLICY_COMMIT_SHAPE_MISMATCH",
        "pc-provider-blob-mismatch": "POLICY_PROOF_BLOB_MISMATCH",
        "pc-test-blob-mismatch": "POLICY_PROOF_BLOB_MISMATCH",
        "m2-first-parent-not-ps1": "POLICY_PARENT_MISMATCH",
        "m2-second-parent-not-t": "POLICY_REQUIRED_ANCESTOR_MISSING",
        "m2-parent-order-reversed": "POLICY_PARENT_MISMATCH",
        "m2-subject-mismatch": "POLICY_SUBJECT_MISMATCH",
        "m2-tree-not-t": "POLICY_COMMIT_SHAPE_MISMATCH",
        "m2-missing-containment-path": "POLICY_PATH_CLOSURE_VIOLATION",
        "m2-sixth-path": "POLICY_PATH_CLOSURE_VIOLATION",
        "m2-merge-time-edit": "POLICY_COMMIT_SHAPE_MISMATCH",
        "post-m2-source-commit": "POLICY_PATH_CLOSURE_VIOLATION",
        "post-m2-merge": "POLICY_COMMIT_SHAPE_MISMATCH",
        "s2-status-plus-source": "POLICY_PATH_CLOSURE_VIOLATION",
        "stale-transition-record": "POLICY_CONTEXT_UNRECOGNIZED",
        "ambiguous-old-new-context": "POLICY_CONTEXT_AMBIGUOUS",
        "malformed-git-facts": "POLICY_GIT_COMMAND_FAILED",
        "owner-runtime-path-regression": "POLICY_UNTRACKED_PROTECTED_OVERLAP",
    }

    new_git_policy_cases = {
        "exact-m2": (
            checkpoint_two_decision.accepted
            and checkpoint_two_decision.context.value == "rc_policy_checkpoint_head"
            and checkpoint_two_decision.checkpoint_oid == checkpoint_two_oid
            and checkpoint_two_decision.status_commit_count == 0
        ),
        "exact-s2": (
            checkpoint_status_decision.accepted
            and checkpoint_status_decision.context.value == "rc_policy_status_descendant"
            and checkpoint_status_decision.checkpoint_oid == checkpoint_two_oid
            and checkpoint_status_decision.status_commit_count == 1
        ),
        "multiple-status-descendants-after-m2": (
            multiple_status_decision.accepted and multiple_status_decision.status_commit_count == 2
        ),
        "pm-ps1-historical-preservation": (
            previous_checkpoint_decision.accepted
            and previous_checkpoint_decision.checkpoint_oid == PREVIOUS_CHECKPOINT_OID
            and current_status_decision.accepted
            and current_status_decision.checkpoint_oid == PREVIOUS_CHECKPOINT_OID
            and current_status_decision.status_commit_count == 1
        ),
        "pc-full-execution": not evaluate_checkout(containment_state).accepted,
        "t-full-execution": not evaluate_checkout(transition_state).accepted,
    }
    new_git_policy_cases.update(
        {
            case: (
                not (decision := evaluate_checkout(state)).accepted
                and decision.reason == expected_mutation_reasons[case]
                and not decision.proof_critical_blobs_verified
            )
            for case, state in mutation_states.items()
        }
    )
    decision_fields = CheckoutRecord.__dataclass_fields__
    new_git_policy_cases["environment-expected-head"] = (
        not evaluate_checkout(containment_state).accepted
        and not any("expected" in name or "override" in name for name in decision_fields)
    )
    new_git_policy_cases["cli-expected-head"] = "--expected-head" not in parser_options
    assert len(new_git_policy_cases) == 30
    assert set(new_git_policy_cases) == {
        "exact-m2",
        "exact-s2",
        "multiple-status-descendants-after-m2",
        "pm-ps1-historical-preservation",
        "pc-full-execution",
        "t-full-execution",
        *expected_mutation_reasons,
        "environment-expected-head",
        "cli-expected-head",
    }
    assert all(new_git_policy_cases.values())
    for decision in (checkpoint_two_decision, checkpoint_status_decision, multiple_status_decision):
        assert decision.previous_checkpoint_oid == PREVIOUS_CHECKPOINT_OID
        assert decision.checkpoint_generation == CHECKPOINT_GENERATION
        assert decision.transition_id == TRANSITION_ID
        assert decision.proof_critical_blobs_verified

    q3_oid = "5" * 40
    g3_oid = "4" * 40
    s3_oid = "3" * 40
    s3_two_oid = "2" * 40
    q3_tree = "1f" * 20
    transition_two = v2_commit(
        GENERATION3_TRANSITION_FEATURE_OID,
        (CONTAINMENT_OID,),
        TRANSITION_SUBJECT,
        POLICY_PATHS,
        GENERATION3_PREVIOUS_CHECKPOINT_TREE,
    )
    generation3_previous_checkpoint = v2_commit(
        GENERATION3_PREVIOUS_CHECKPOINT_OID,
        (CURRENT_STATUS_OID, GENERATION3_TRANSITION_FEATURE_OID),
        CHECKPOINT_TWO_SUBJECT,
        CHECKPOINT_TWO_PATHS,
        GENERATION3_PREVIOUS_CHECKPOINT_TREE,
    )
    generation3_status_base = v2_commit(
        GENERATION3_STATUS_BASE_OID,
        (GENERATION3_PREVIOUS_CHECKPOINT_OID,),
        GENERATION3_STATUS_BASE_SUBJECT,
        frozenset({"STATUS.md"}),
        GENERATION3_STATUS_BASE_TREE,
    )
    provider_routing_feature = v2_commit(
        PROVIDER_ROUTING_FEATURE_OID,
        (GENERATION3_STATUS_BASE_OID,),
        PROVIDER_ROUTING_FEATURE_SUBJECT,
        PROVIDER_ROUTING_FEATURE_PATHS,
        PROVIDER_ROUTING_FEATURE_TREE,
        PROVIDER_ROUTING_FEATURE_PATH_STATUSES,
    )
    generation3_policy_feature = v2_commit(
        q3_oid,
        (PROVIDER_ROUTING_FEATURE_OID,),
        GENERATION3_POLICY_FEATURE_SUBJECT,
        POLICY_PATHS,
        q3_tree,
    )
    generation3_checkpoint = v2_commit(
        g3_oid,
        (GENERATION3_STATUS_BASE_OID, q3_oid),
        GENERATION3_CHECKPOINT_SUBJECT,
        GENERATION3_CHECKPOINT_PATHS,
        q3_tree,
        GENERATION3_CHECKPOINT_PATH_STATUSES,
    )
    generation3_status = v2_commit(
        s3_oid,
        (g3_oid,),
        GENERATION3_STATUS_SUBJECT,
        frozenset({"STATUS.md"}),
        "2f" * 20,
    )
    generation3_status_two = v2_commit(
        s3_two_oid,
        (s3_oid,),
        "Record later generation-3 validation",
        frozenset({"STATUS.md"}),
        "3f" * 20,
    )
    generation3_policy_blobs = tuple(
        BlobRecord(path, oid)
        for path, oid in sorted(
            {
                "scripts/evidence_provenance_harness_git_policy.py": "a" * 40,
                "scripts/test_evidence_provenance_real_backend.py": "b" * 40,
                "tests/test_evidence_provenance_real_backend_harness_policy.py": "c" * 40,
            }.items()
        )
    )
    generation3_current_blobs = tuple(
        sorted(
            (
                *(
                    BlobRecord(path, oid, TRUSTED_BLOB_SHA256.get(path))
                    for path, oid in GENERATION3_PROOF_BLOB_OIDS
                ),
                *generation3_policy_blobs,
            ),
            key=lambda item: item.path,
        )
    )
    provider_routing_blobs = tuple(
        BlobRecord(path, oid) for path, oid in PROVIDER_ROUTING_FEATURE_BLOB_OIDS
    )
    generation3_ancestors = v2_ancestors | frozenset(
        {
            GENERATION3_PREVIOUS_CHECKPOINT_OID,
            GENERATION3_STATUS_BASE_OID,
            PROVIDER_ROUTING_FEATURE_OID,
            q3_oid,
        }
    )
    generation3_related = (
        base,
        safe,
        feature,
        integration,
        previous_feature,
        containment,
        transition_two,
        provider_routing_feature,
        generation3_policy_feature,
    )

    def generation3_state(later: tuple[CommitRecord, ...] = ()) -> CheckoutRecord:
        first_parent = (
            status,
            previous_checkpoint,
            current_status,
            generation3_previous_checkpoint,
            generation3_status_base,
            generation3_checkpoint,
            *later,
        )
        head_commit = first_parent[-1]
        return CheckoutRecord(
            branch=RC_BRANCH,
            head=head_commit.oid,
            head_commit=head_commit,
            first_parent_commits=first_parent,
            related_commits=generation3_related,
            blobs=generation3_current_blobs,
            checkpoint_blobs=generation3_policy_blobs,
            containment_blobs=containment_blobs,
            provider_routing_blobs=provider_routing_blobs,
            verified_ancestors=generation3_ancestors,
            tracked_clean=True,
            index_empty=True,
        )

    generation3_checkpoint_state = generation3_state()
    generation3_status_state = generation3_state((generation3_status,))
    generation3_multiple_status_state = generation3_state(
        (generation3_status, generation3_status_two)
    )
    generation3_checkpoint_decision = evaluate_checkout(generation3_checkpoint_state)
    generation3_status_decision = evaluate_checkout(generation3_status_state)
    generation3_multiple_status_decision = evaluate_checkout(generation3_multiple_status_state)
    generation3_prefix = generation3_checkpoint_state.first_parent_commits[:-1]
    generation3_feature_state = replace(
        generation3_checkpoint_state,
        head=PROVIDER_ROUTING_FEATURE_OID,
        head_commit=provider_routing_feature,
        first_parent_commits=(*generation3_prefix, provider_routing_feature),
    )
    generation3_policy_feature_state = replace(
        generation3_checkpoint_state,
        head=q3_oid,
        head_commit=generation3_policy_feature,
        first_parent_commits=(
            *generation3_prefix,
            provider_routing_feature,
            generation3_policy_feature,
        ),
    )

    generation3_mutations: dict[str, CheckoutRecord] = {}
    generation3_mutations["f-parent-not-s2"] = replace_commit_record(
        generation3_checkpoint_state,
        PROVIDER_ROUTING_FEATURE_OID,
        replace(provider_routing_feature, parents=(unrelated_oid,)),
    )
    generation3_mutations["f-subject-mismatch"] = replace_commit_record(
        generation3_checkpoint_state,
        PROVIDER_ROUTING_FEATURE_OID,
        replace(provider_routing_feature, subject="Unexpected feature"),
    )
    generation3_mutations["f-tree-mismatch"] = replace_commit_record(
        generation3_checkpoint_state,
        PROVIDER_ROUTING_FEATURE_OID,
        replace(provider_routing_feature, tree_oid="0" * 40),
    )
    feature_status_mismatch = tuple(
        (path, "A" if path == "app/services/provider_orchestration.py" else path_status)
        for path, path_status in provider_routing_feature.changed_path_statuses
    )
    generation3_mutations["f-path-status-mismatch"] = replace_commit_record(
        generation3_checkpoint_state,
        PROVIDER_ROUTING_FEATURE_OID,
        replace(provider_routing_feature, changed_path_statuses=feature_status_mismatch),
    )
    for case, paths in (
        (
            "f-missing-path",
            PROVIDER_ROUTING_FEATURE_PATHS - {"app/services/provider_output_acceptance.py"},
        ),
        ("f-seventh-path", PROVIDER_ROUTING_FEATURE_PATHS | {"README.md"}),
    ):
        statuses = tuple(
            (path, dict(PROVIDER_ROUTING_FEATURE_PATH_STATUSES).get(path, "M"))
            for path in sorted(paths)
        )
        generation3_mutations[case] = replace_commit_record(
            generation3_checkpoint_state,
            PROVIDER_ROUTING_FEATURE_OID,
            replace(provider_routing_feature, changed_paths=paths, changed_path_statuses=statuses),
        )
    generation3_mutations["q-parent-not-f"] = replace_commit_record(
        generation3_checkpoint_state,
        q3_oid,
        replace(generation3_policy_feature, parents=(unrelated_oid,)),
    )
    generation3_mutations["q-subject-mismatch"] = replace_commit_record(
        generation3_checkpoint_state,
        q3_oid,
        replace(generation3_policy_feature, subject="Unexpected policy transition"),
    )
    q3_extra_paths = POLICY_PATHS | {"README.md"}
    generation3_mutations["q-fourth-path"] = replace_commit_record(
        generation3_checkpoint_state,
        q3_oid,
        replace(
            generation3_policy_feature,
            changed_paths=q3_extra_paths,
            changed_path_statuses=modified(q3_extra_paths),
        ),
    )
    generation3_mutations["g3-first-parent-not-s2"] = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, parents=(unrelated_oid, q3_oid)),
    )
    generation3_mutations["g3-second-parent-not-q"] = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, parents=(GENERATION3_STATUS_BASE_OID, unrelated_oid)),
    )
    generation3_mutations["g3-parent-order-reversed"] = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, parents=(q3_oid, GENERATION3_STATUS_BASE_OID)),
    )
    generation3_mutations["g3-subject-mismatch"] = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, subject="Unexpected generation-3 checkpoint"),
    )
    generation3_mutations["g3-tree-not-q"] = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, tree_oid="0" * 40),
    )
    for case, paths in (
        (
            "g3-missing-feature-path",
            GENERATION3_CHECKPOINT_PATHS - {"app/services/provider_output_acceptance.py"},
        ),
        ("g3-tenth-path", GENERATION3_CHECKPOINT_PATHS | {"README.md"}),
    ):
        statuses = tuple(
            (path, dict(PROVIDER_ROUTING_FEATURE_PATH_STATUSES).get(path, "M"))
            for path in sorted(paths)
        )
        generation3_mutations[case] = replace_commit_record(
            generation3_checkpoint_state,
            g3_oid,
            replace(generation3_checkpoint, changed_paths=paths, changed_path_statuses=statuses),
        )
    generation3_merge_edit_statuses = tuple(
        (
            path,
            "A" if path == "scripts/evidence_provenance_harness_git_policy.py" else path_status,
        )
        for path, path_status in generation3_checkpoint.changed_path_statuses
    )
    generation3_mutations["g3-merge-time-edit"] = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, changed_path_statuses=generation3_merge_edit_statuses),
    )
    post_generation3_source = v2_commit(
        unrelated_oid,
        (g3_oid,),
        "Change source after generation-3 checkpoint",
        frozenset({"app/main.py"}),
        "0" * 40,
    )
    generation3_mutations["post-g3-source-commit"] = replace(
        generation3_checkpoint_state,
        head=unrelated_oid,
        head_commit=post_generation3_source,
        first_parent_commits=(
            *generation3_checkpoint_state.first_parent_commits,
            post_generation3_source,
        ),
    )
    post_generation3_merge = v2_commit(
        unrelated_oid,
        (g3_oid, q3_oid),
        "Merge after generation-3 checkpoint",
        frozenset({"STATUS.md"}),
        "0" * 40,
    )
    generation3_mutations["post-g3-merge"] = replace(
        generation3_checkpoint_state,
        head=unrelated_oid,
        head_commit=post_generation3_merge,
        first_parent_commits=(
            *generation3_checkpoint_state.first_parent_commits,
            post_generation3_merge,
        ),
    )
    bad_generation3_status_paths = frozenset({"STATUS.md", "app/main.py"})
    bad_generation3_status = v2_commit(
        s3_oid,
        (g3_oid,),
        GENERATION3_STATUS_SUBJECT,
        bad_generation3_status_paths,
        "2f" * 20,
    )
    generation3_mutations["s3-status-plus-source"] = replace_commit_record(
        generation3_status_state,
        s3_oid,
        bad_generation3_status,
    )

    generation3_cases = {
        "exact-g3": (
            generation3_checkpoint_decision.accepted
            and generation3_checkpoint_decision.context.value == "rc_policy_checkpoint_head"
            and generation3_checkpoint_decision.status_commit_count == 0
        ),
        "exact-s3": (
            generation3_status_decision.accepted
            and generation3_status_decision.context.value == "rc_policy_status_descendant"
            and generation3_status_decision.status_commit_count == 1
        ),
        "multiple-status-descendants-after-g3": (
            generation3_multiple_status_decision.accepted
            and generation3_multiple_status_decision.status_commit_count == 2
        ),
        "generation1-generation2-history-preserved": all(
            decision.accepted
            for decision in (
                previous_checkpoint_decision,
                current_status_decision,
                checkpoint_two_decision,
                checkpoint_status_decision,
            )
        ),
        "f-full-execution": not evaluate_checkout(generation3_feature_state).accepted,
        "q-full-execution": not evaluate_checkout(generation3_policy_feature_state).accepted,
    }
    generation3_cases.update(
        {
            case: (
                not (decision := evaluate_checkout(state)).accepted
                and not decision.proof_critical_blobs_verified
            )
            for case, state in generation3_mutations.items()
        }
    )
    generation3_cases["f-integrated-blob-mismatch"] = all(
        not (decision := evaluate_checkout(replace(
            generation3_checkpoint_state,
            provider_routing_blobs=tuple(
                BlobRecord(item.path, "0" * 40 if item.path == changed_path else item.oid)
                for item in provider_routing_blobs
            ),
        ))).accepted
        and decision.reason == "POLICY_PROOF_BLOB_MISMATCH"
        and not decision.proof_critical_blobs_verified
        for changed_path in PROVIDER_ROUTING_FEATURE_PATHS
    )
    stale_generation3 = replace(
        generation3_checkpoint_state,
        first_parent_commits=generation3_checkpoint_state.first_parent_commits[:-1],
    )
    ambiguous_generation3 = replace(
        generation3_checkpoint_state,
        related_commits=(
            *generation3_checkpoint_state.related_commits,
            replace(generation3_policy_feature, subject="Conflicting generation-3 transition"),
        ),
    )
    malformed_generation3 = replace_commit_record(
        generation3_checkpoint_state,
        g3_oid,
        replace(generation3_checkpoint, tree_oid="not-a-tree"),
    )
    generation3_cases["stale-ambiguous-or-malformed-generation-record"] = all(
        not (decision := evaluate_checkout(state)).accepted
        and not decision.proof_critical_blobs_verified
        for state in (stale_generation3, ambiguous_generation3, malformed_generation3)
    )
    generation3_cases["environment-expected-checkpoint"] = (
        not evaluate_checkout(generation3_policy_feature_state).accepted
        and not any(
            "expected" in name or "override" in name
            for name in CheckoutRecord.__dataclass_fields__
        )
    )
    generation3_cases["cli-expected-checkpoint"] = "--expected-checkpoint" not in parser_options
    assert len(generation3_cases) == 30
    assert all(generation3_cases.values())
    for decision, expected_status_count in (
        (generation3_checkpoint_decision, 0),
        (generation3_status_decision, 1),
        (generation3_multiple_status_decision, 2),
    ):
        assert decision.execution_mode is ExecutionMode.FULL_HARNESS_ALLOWED
        assert decision.checkpoint_oid == g3_oid
        assert decision.status_commit_count == expected_status_count
        assert decision.previous_checkpoint_oid == GENERATION3_PREVIOUS_CHECKPOINT_OID
        assert decision.checkpoint_generation == GENERATION3_CHECKPOINT_GENERATION
        assert decision.transition_id == GENERATION3_TRANSITION_ID
        assert decision.proof_critical_blobs_verified

    def generation3_summary(decision: PolicyDecision) -> dict[str, object]:
        return {
            "status": "passed",
            "policyVersion": POLICY_VERSION,
            "context": decision.context.value if decision.context is not None else None,
            "executionMode": (
                decision.execution_mode.value if decision.execution_mode is not None else None
            ),
            "checkpointOid": decision.checkpoint_oid,
            "statusCommitCount": decision.status_commit_count,
            "previousCheckpointOid": decision.previous_checkpoint_oid,
            "checkpointGeneration": decision.checkpoint_generation,
            "transitionId": decision.transition_id,
            "headPolicyAccepted": decision.accepted,
            "proofCriticalBlobsVerified": decision.proof_critical_blobs_verified,
            "trackedWorktreeClean": True,
            "indexEmpty": True,
            "ownerArtifactCount": 13,
            "ownerArtifactInventoryUnchanged": True,
            "providerRequestCount": 0,
            "externalRequestCount": 0,
            "oauthOidcRequestCount": 0,
            "mcpRequestCount": 0,
            "primaryFailure": None,
            "secondaryFailureCodes": [],
            "proofLimitations": _proof_limitations(),
        }

    assert generation3_summary(generation3_checkpoint_decision) == {
        "status": "passed",
        "policyVersion": "evidence_provenance_multi_context_v3",
        "context": "rc_policy_checkpoint_head",
        "executionMode": "full_harness_allowed",
        "checkpointOid": g3_oid,
        "statusCommitCount": 0,
        "previousCheckpointOid": GENERATION3_PREVIOUS_CHECKPOINT_OID,
        "checkpointGeneration": 3,
        "transitionId": "provider_multi_backend_routing_contract",
        "headPolicyAccepted": True,
        "proofCriticalBlobsVerified": True,
        "trackedWorktreeClean": True,
        "indexEmpty": True,
        "ownerArtifactCount": 13,
        "ownerArtifactInventoryUnchanged": True,
        "providerRequestCount": 0,
        "externalRequestCount": 0,
        "oauthOidcRequestCount": 0,
        "mcpRequestCount": 0,
        "primaryFailure": None,
        "secondaryFailureCodes": [],
        "proofLimitations": _proof_limitations(),
    }
    assert generation3_summary(generation3_status_decision) == {
        "status": "passed",
        "policyVersion": "evidence_provenance_multi_context_v3",
        "context": "rc_policy_status_descendant",
        "executionMode": "full_harness_allowed",
        "checkpointOid": g3_oid,
        "statusCommitCount": 1,
        "previousCheckpointOid": GENERATION3_PREVIOUS_CHECKPOINT_OID,
        "checkpointGeneration": 3,
        "transitionId": "provider_multi_backend_routing_contract",
        "headPolicyAccepted": True,
        "proofCriticalBlobsVerified": True,
        "trackedWorktreeClean": True,
        "indexEmpty": True,
        "ownerArtifactCount": 13,
        "ownerArtifactInventoryUnchanged": True,
        "providerRequestCount": 0,
        "externalRequestCount": 0,
        "oauthOidcRequestCount": 0,
        "mcpRequestCount": 0,
        "primaryFailure": None,
        "secondaryFailureCodes": [],
        "proofLimitations": _proof_limitations(),
    }

    vite_then_runtime = _resolve_failure_precedence(
        "VITE_GENERATED_CONFIG_DRIFT",
        ("RUNTIME_ARTIFACT_GUARD_DRIFT",),
    )
    assert vite_then_runtime == FailureResolution(
        primary_failure="VITE_GENERATED_CONFIG_DRIFT",
        secondary_failure_codes=("SECONDARY_RUNTIME_GUARD_DRIFT",),
    )

    runtime_only = _resolve_failure_precedence(None, ("RUNTIME_ARTIFACT_GUARD_DRIFT",))
    assert runtime_only == FailureResolution(
        primary_failure="RUNTIME_ARTIFACT_GUARD_DRIFT",
        secondary_failure_codes=(),
    )

    fixture_then_cleanup = _resolve_failure_precedence(
        "FIXTURE_SELF_TEST_FAILED",
        ("PROCESS_LEAK", "PORT_NOT_RELEASED"),
    )
    assert fixture_then_cleanup == FailureResolution(
        primary_failure="FIXTURE_SELF_TEST_FAILED",
        secondary_failure_codes=("SECONDARY_PROCESS_CLEANUP_FAILED", "SECONDARY_PORT_RELEASE_FAILED"),
    )
    fixture_root = Path(tempfile.gettempdir()).resolve() / "xrml-contract-fixture-root"
    fixture_env = _fixture_environment(fixture_root)
    assert Path(fixture_env["XRML_DATA_DIR"]).resolve() == fixture_root
    for name in ("TEMP", "TMP", "TMPDIR"):
        if name in fixture_env:
            assert Path(fixture_env[name]).resolve() != fixture_root
    assert _serialize_summary({"message": "\u203a \ud55c\uae00"}).isascii()
    return {
        "status": "passed",
        "cases": 80,
        "gitPolicyCases": 75,
        "historicalContextsAccepted": True,
        "historicalExecutionBlocked": True,
        "operationalContextsAccepted": True,
        "arbitraryAndMalformedGitStatesRejected": True,
        "bypassOptionsAbsent": True,
        "primaryPreserved": True,
        "secondaryFailuresRetained": True,
        "fixtureTempParentPreserved": True,
        "summaryEncodingSafe": True,
        "v2TransitionCasesPassed": True,
        "v2StructuredSummaryContract": True,
        "v3TransitionCasesPassed": True,
        "v3StructuredSummaryContract": True,
        "proofCriticalBlobsExplicit": True,
    }


def _coerce_failure(exc: Exception, default_reason: str) -> HarnessFailure:
    if isinstance(exc, HarnessFailure):
        return exc
    return HarnessFailure(default_reason, f"{type(exc).__name__}: {exc}")


def _serialize_summary(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    summary: dict[str, object] = {
        "status": "failed",
        "stopReason": None,
        "primaryFailure": None,
        "secondaryFailureCodes": [],
        "secondaryDiagnostics": [],
        "cleanupAttempted": False,
        "cleanupSucceeded": False,
        "guardsAttempted": False,
        "guardsSucceeded": False,
        "repoHead": None,
        "gitPolicy": POLICY_VERSION,
        "policyVersion": POLICY_VERSION,
        "context": None,
        "executionMode": None,
        "checkpointOid": None,
        "statusCommitCount": 0,
        "previousCheckpointOid": None,
        "checkpointGeneration": None,
        "transitionId": None,
        "currentHead": None,
        "headPolicyAccepted": False,
        "proofCriticalBlobsVerified": False,
        "trackedWorktreeClean": False,
        "indexEmpty": False,
        "ownerArtifactCount": 0,
        "ownerArtifactInventoryUnchanged": False,
        "tempRootClassification": "not_created",
        "fixtureFingerprint": None,
        "backendPid": None,
        "backendOrigin": ORIGIN,
        "apiPreflight": "not_run",
        "playwright": "not_run",
        "requestLedger": None,
        "providerRequestCount": None,
        "externalRequestCount": None,
        "oauthOidcRequestCount": None,
        "mcpRequestCount": None,
        "guards": "not_run",
        "cleanup": "not_run",
        "viteConfigContentChanged": False,
        "viteConfigStatusOnlyDriftObserved": False,
        "viteConfigStatCacheRefreshed": False,
        "viteConfigIndexBlobChanged": False,
        "viteConfigFinalClean": False,
        "proofLimitations": _proof_limitations(),
    }
    process: subprocess.Popen[str] | None = None
    temp_root: Path | None = None
    protected_before: dict[str, object] | None = None
    owner_before: tuple[OwnerArtifactRecord, ...] | None = None
    execution_failure: HarnessFailure | None = None
    post_failures: list[HarnessFailure] = []
    cleanup_failures: list[HarnessFailure] = []
    guard_failures: list[HarnessFailure] = []
    try:
        checkout, policy_decision, owner_before = _collect_checkout()
        summary.update(
            {
                "repoHead": checkout.head,
                "currentHead": checkout.head,
                "context": policy_decision.context.value if policy_decision.context is not None else None,
                "executionMode": (
                    policy_decision.execution_mode.value if policy_decision.execution_mode is not None else None
                ),
                "checkpointOid": policy_decision.checkpoint_oid,
                "statusCommitCount": policy_decision.status_commit_count,
                "previousCheckpointOid": policy_decision.previous_checkpoint_oid,
                "checkpointGeneration": policy_decision.checkpoint_generation,
                "transitionId": policy_decision.transition_id,
                "headPolicyAccepted": policy_decision.accepted,
                "proofCriticalBlobsVerified": policy_decision.proof_critical_blobs_verified,
                "trackedWorktreeClean": checkout.tracked_clean,
                "indexEmpty": checkout.index_empty,
                "ownerArtifactCount": len(owner_before),
            }
        )
        if not policy_decision.accepted:
            raise HarnessFailure(
                policy_decision.reason or "POLICY_CONTEXT_UNRECOGNIZED",
                f"Git policy rejected context: {policy_decision.reason or 'unknown'}",
            )
        if policy_decision.execution_mode is not ExecutionMode.FULL_HARNESS_ALLOWED:
            raise HarnessFailure(
                "POLICY_HISTORICAL_CONTEXT_EXECUTION_BLOCKED",
                f"recognized historical context cannot execute the full harness: {policy_decision.context.value}",
            )
        summary["toolchain"] = _python_version_contract()
        _playwright_command()
        if not _port_available():
            raise HarnessFailure("SAFE_PORT_ALLOCATION_UNRESOLVED", f"port {PORT} is occupied")
        protected_repo = _toolchain_root()
        protected_before = _protected_snapshot(protected_repo)

        if args.fixture_self_test:
            summary["fixtureSelfTest"] = _run_fixture_self_test()
        else:
            summary["frontendSourceFingerprint"] = _build_frontend(
                skip_build=args.skip_build,
                summary=summary,
            )
            temp_root = Path(tempfile.mkdtemp(prefix="xrml-evidence-real-backend-"))
            summary["tempRootClassification"] = "owned_os_temp"
            manifest = _fixture_manifest(temp_root)
            summary["fixtureFingerprint"] = manifest.logicalCorpusFingerprint
            runtime_env = _runtime_environment(temp_root)
            summary["runtimePaths"] = _assert_runtime_paths(temp_root, runtime_env)

            process, _stdout_path, stderr_path = _start_backend(temp_root, runtime_env)
            summary["backendPid"] = process.pid
            _wait_for_health(process, stderr_path)
            summary["apiPreflight"] = _api_preflight(manifest)
            ledger, playwright_status = _run_playwright(temp_root)
            summary["requestLedger"] = ledger
            summary["playwright"] = playwright_status
            summary["providerRequestCount"] = 0
            summary["externalRequestCount"] = 0
            summary["oauthOidcRequestCount"] = 0
            summary["mcpRequestCount"] = 0
            if args.with_regressions:
                summary["regressions"] = _run_regressions()
    except HarnessFailure as exc:
        execution_failure = exc
    except Exception as exc:  # pragma: no cover - unexpected failures still require cleanup and a safe summary
        execution_failure = HarnessFailure(
            "REAL_BACKEND_PLAYWRIGHT_FAILED",
            f"unexpected harness error: {type(exc).__name__}: {exc}",
        )
    finally:
        summary["cleanupAttempted"] = True
        try:
            summary["processTermination"] = _stop_backend(process)
        except Exception as exc:  # pragma: no cover - cleanup failures are retained structurally
            failure = _coerce_failure(exc, "PROCESS_LEAK")
            post_failures.append(failure)
            cleanup_failures.append(failure)
        try:
            _wait_for_port_release()
        except Exception as exc:  # pragma: no cover - cleanup failures are retained structurally
            failure = _coerce_failure(exc, "PORT_NOT_RELEASED")
            post_failures.append(failure)
            cleanup_failures.append(failure)
        if temp_root is not None:
            if execution_failure is not None and args.keep_temp_on_failure:
                summary["cleanup"] = "retained_on_failure_by_flag"
            else:
                try:
                    _cleanup_temp_root(temp_root)
                except Exception as exc:  # pragma: no cover - cleanup failures are retained structurally
                    failure = _coerce_failure(exc, "TEMP_OVERLAY_CLEANUP_FAILED")
                    post_failures.append(failure)
                    cleanup_failures.append(failure)
        summary["cleanupSucceeded"] = not cleanup_failures and summary["cleanup"] != "retained_on_failure_by_flag"
        if summary["cleanup"] != "retained_on_failure_by_flag":
            summary["cleanup"] = "passed" if summary["cleanupSucceeded"] else "failed"

        summary["guardsAttempted"] = True
        if protected_before is not None:
            try:
                protected_after = _protected_snapshot(_toolchain_root())
                if protected_after != protected_before:
                    raise HarnessFailure("RUNTIME_ARTIFACT_GUARD_DRIFT", "protected pre/post snapshots differ")
            except Exception as exc:  # pragma: no cover - guard failures are retained structurally
                failure = _coerce_failure(exc, "RUNTIME_ARTIFACT_GUARD_DRIFT")
                post_failures.append(failure)
                guard_failures.append(failure)
        if owner_before is not None:
            try:
                owner_after = _verify_owner_inventory(owner_before)
                summary["ownerArtifactCount"] = len(owner_after)
                summary["ownerArtifactInventoryUnchanged"] = True
            except Exception as exc:  # pragma: no cover - guard failures are retained structurally
                failure = _coerce_failure(exc, "POLICY_OWNER_ARTIFACT_DRIFT")
                post_failures.append(failure)
                guard_failures.append(failure)
        summary["guardsSucceeded"] = not guard_failures
        summary["guards"] = "passed" if summary["guardsSucceeded"] else "failed"

    resolution = _resolve_failure_precedence(
        execution_failure.stop_reason if execution_failure is not None else None,
        tuple(failure.stop_reason for failure in post_failures),
    )
    summary["primaryFailure"] = resolution.primary_failure
    summary["secondaryFailureCodes"] = list(resolution.secondary_failure_codes)
    secondary_failures = post_failures if execution_failure is not None else post_failures[1:]
    summary["secondaryDiagnostics"] = [
        {
            "code": _secondary_failure_code(failure.stop_reason),
            "sourceStopReason": failure.stop_reason,
            "message": _tail(str(failure), lines=10),
        }
        for failure in secondary_failures
    ]

    if resolution.primary_failure is None:
        summary["status"] = "passed"
        summary["stopReason"] = None
        return 0, summary
    primary_exception = execution_failure if execution_failure is not None else post_failures[0]
    summary["status"] = "failed"
    summary["stopReason"] = resolution.primary_failure
    summary["error"] = _tail(str(primary_exception), lines=20)
    return 1, summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local browser-to-real-FastAPI evidence provenance proof.")
    parser.add_argument("--contract-self-check", action="store_true")
    parser.add_argument("--fixture-self-test", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep-temp-on-failure", action="store_true")
    parser.add_argument("--with-regressions", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.contract_self_check:
        print(_serialize_summary(_run_contract_self_check()))
        return 0
    exit_code, summary = run(args)
    print(_serialize_summary(summary))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

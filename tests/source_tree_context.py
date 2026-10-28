"""Distinguish a preserved development checkout from a complete public source tree.

An independent public Git root deliberately has no private ancestors. Git's mere
presence cannot select private-history tests, and missing private files alone
cannot authorize the public fixture path in a damaged development checkout.
This verifies governed documents and known exclusions, not the absence of secret
or private non-document content; publication still requires its separate audit.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools import public_content_policy as pcp

PRIVATE_HISTORY_ANCHORS = (
    "3db71003e9aae6c2709c3fc6cfc820716b741c94",
    "a24951827095cd7bbd1db4c8fd38f997049df8c1",
    "d0cd79647d0cc9ff30466e78b8358840999797d7",
    "b7d3047831027f8b027f4e0b1dd1e568d0ef9c61",
    "635380c7280d704bcc4c9ccf15e24b412323e8d9",
    "3c4dd21b4505d94463cd15fb38857c54812719e3",
)


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], input=input_bytes, capture_output=True, check=True
    )
    return result.stdout


def _filesystem_paths(repo: Path) -> list[str]:
    """Inspect extracted source, excluding only dependency/build/cache directories."""
    ignored = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    paths = []
    for root, dirs, files in os.walk(repo):
        relative = Path(root).relative_to(repo)
        dirs[:] = [name for name in dirs if name not in ignored
                   and not (relative == Path(".") and name.startswith(".venv"))
                   and not (relative == Path("web") and name == "dist")]
        paths.extend((relative / name).as_posix() for name in files)
    return sorted(paths)


@dataclass(frozen=True)
class SourceTreeContext:
    repo: Path
    policy: pcp.ContentPolicy
    paths: tuple[str, ...]
    is_public: bool
    has_git: bool

    def validate_paths(self, paths: list[str]) -> None:
        """Keep negative inventory tests meaningful in either source scope."""
        if not self.is_public:
            pcp.validate_against(paths, self.policy)
            return
        unknown = self.policy.unclassified(paths)
        if unknown:
            raise pcp.PolicyError(f"document(s) have no role: {unknown[:5]}")
        unresolved = self.policy.unresolved(paths)
        if unresolved:
            raise pcp.PolicyError(f"document(s) carry no final disposition: {unresolved[:5]}")
        shipping = self.policy.shipping_documents()
        missing = sorted(shipping - set(paths))
        if missing:
            raise pcp.PolicyError(f"shipping document no longer exists: {missing[:5]}")
        excluded = sorted((set(self.policy.documents) - shipping) & set(paths))
        if excluded:
            raise pcp.PolicyError(f"excluded document present in public tree: {excluded[:5]}")

    def assert_excluded(self, paths: tuple[str, ...] | list[str]) -> None:
        assert self.is_public, "exclusion assertions require a verified public source tree"
        for path in paths:
            assert path in self.policy.documents and path not in self.policy.shipping_documents(), path
            assert path not in self.paths, path
            assert not (self.repo / path).exists() and not (self.repo / path).is_symlink(), path


def inspect_source_tree(repo: Path, policy: pcp.ContentPolicy | None = None) -> SourceTreeContext:
    repo = repo.resolve()
    policy = policy or pcp.load(repo / "tools/public_content_policy.json")
    has_git = (repo / ".git").exists() or (repo / ".git").is_symlink()
    private_history = False
    shallow = False
    if has_git:
        top = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if top != repo:
            raise pcp.PolicyError("Git worktree root differs from source root")
        _git(repo, "cat-file", "-e", "HEAD^{commit}")
        shallow_state = _git(repo, "rev-parse", "--is-shallow-repository").strip()
        if shallow_state not in {b"true", b"false"}:
            raise pcp.PolicyError("invalid shallow-repository response")
        shallow = shallow_state == b"true"
        paths = sorted(set(_git(repo, "ls-files", "--cached", "-z")
                           .decode("utf-8").rstrip("\0").split("\0")) - {""})
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
        unknown = policy.unclassified(untracked)
        if unknown:
            raise pcp.PolicyError(f"untracked document(s) have no role: {unknown[:5]}")
        request = "".join(f"{commit}^{{commit}}\n" for commit in PRIVATE_HISTORY_ANCHORS).encode("ascii")
        rows = _git(repo, "cat-file", "--batch-check", input_bytes=request).splitlines()
        if len(rows) != len(PRIVATE_HISTORY_ANCHORS):
            raise pcp.PolicyError("incomplete private-history availability response")
        for commit, row in zip(PRIVATE_HISTORY_ANCHORS, rows):
            if row == f"{commit}^{{commit}} missing".encode("ascii"):
                continue
            parts = row.split()
            if len(parts) != 3 or parts[:2] != [commit.encode("ascii"), b"commit"]:
                raise pcp.PolicyError("unexpected private-history availability response")
            private_history = True
    else:
        paths = _filesystem_paths(repo)
    shipping = policy.shipping_documents()
    excluded = set(policy.documents) - shipping
    physically_present = {path for path in excluded
                          if (repo / path).exists() or (repo / path).is_symlink()}
    is_public = not private_history and not physically_present and not (excluded & set(paths))
    context = SourceTreeContext(repo, policy, tuple(paths), is_public, has_git)
    context.validate_paths(paths)
    missing_files = sorted(path for path in (shipping if is_public else policy.documents)
                           if not (repo / path).is_file())
    if missing_files:
        raise pcp.PolicyError(f"governed document no longer exists on disk: {missing_files[:5]}")
    if not is_public and not has_git:
        raise pcp.PolicyError("private development source requires its Git checkout")
    if not is_public and not private_history and not shallow:
        raise pcp.PolicyError("private development history is absent from a non-shallow source tree")
    return context

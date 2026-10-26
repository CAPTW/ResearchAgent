#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Read packaged source bytes from the Git object database, never the checkout.

A Git-clean checkout does not imply byte-identical files. Git materializes blobs
through `core.autocrlf`, `core.eol` and `.gitattributes`, so two clean checkouts
of one commit can hold different bytes for the same tracked path and still report
no modifications -- Git converts back when it compares. A packager that lists
paths from Git but reads *bytes* from the working tree therefore produces an
archive that depends on how the checkout happened to be materialized.

Everything here addresses one entry point: the payload comes from the object
identified by the commit tree, so the archive is a function of the commit alone.

Fail-closed on anything not representable: submodules, unknown modes, unexpected
object types, short reads and non-UTF-8 paths. A source distribution that quietly
drops or mangles an entry is worse than one that refuses to build.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Git tree modes this packager knows how to represent in a tar archive.
REGULAR_FILE = "100644"
EXECUTABLE_FILE = "100755"
SYMLINK = "120000"
GITLINK = "160000"

SUPPORTED_MODES = frozenset({REGULAR_FILE, EXECUTABLE_FILE, SYMLINK})

# Git LFS pointers are small text stubs standing in for content held elsewhere.
# Shipping one silently would publish a file that is not the file it claims to be.
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
LFS_POINTER_MAX_BYTES = 1024


class GitObjectError(RuntimeError):
    """Raised when the commit tree cannot be read exactly."""


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a commit tree, addressed by object id rather than by path."""

    path: str
    mode: str
    object_type: str
    oid: str
    size: int

    @property
    def is_symlink(self) -> bool:
        return self.mode == SYMLINK

    @property
    def is_executable(self) -> bool:
        return self.mode == EXECUTABLE_FILE

    @property
    def archive_mode(self) -> int:
        return 0o755 if self.is_executable else 0o644


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitObjectError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def resolve_commit(repo: Path, revision: str = "HEAD") -> tuple[str, str, int]:
    """Return (commit oid, root tree oid, committer epoch) for `revision`."""
    commit = _git_bytes(repo, "rev-parse", f"{revision}^{{commit}}").decode().strip()
    tree = _git_bytes(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    epoch = int(_git_bytes(repo, "show", "-s", "--format=%ct", commit).decode().strip())
    return commit, tree, epoch


def read_tree(repo: Path, commit: str) -> list[TreeEntry]:
    """Enumerate the commit tree recursively, with sizes, from Git plumbing.

    `--full-tree` ignores the current directory, and `-z` keeps paths intact --
    a path containing a newline would corrupt line-delimited output, and quietly
    losing an entry is exactly the failure this module exists to prevent.
    """
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", commit)
    entries: list[TreeEntry] = []
    unsupported: list[tuple[str, str]] = []

    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path_bytes = record.partition(b"\t")
        if not path_bytes:
            raise GitObjectError(f"unparseable ls-tree record: {meta[:80]!r}")
        try:
            path = path_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Reported as escaped bytes: never normalized into something else.
            raise GitObjectError(
                f"tree path is not valid UTF-8 and cannot be archived safely: "
                f"{path_bytes.decode('utf-8', errors='backslashreplace')}"
            ) from exc

        fields = meta.decode("utf-8", errors="replace").split()
        if len(fields) != 4:
            raise GitObjectError(f"unexpected ls-tree metadata for {path!r}: {fields!r}")
        mode, object_type, oid, size_text = fields

        if mode == GITLINK:
            unsupported.append((path, "submodule (gitlink); its content is not in this repository"))
            continue
        if mode not in SUPPORTED_MODES:
            unsupported.append((path, f"unsupported tree mode {mode}"))
            continue
        if object_type != "blob":
            unsupported.append((path, f"unexpected object type {object_type!r}"))
            continue

        entries.append(TreeEntry(path=path, mode=mode, object_type=object_type, oid=oid,
                                 size=int(size_text)))

    if unsupported:
        listed = "\n".join(f"  {path}  [{why}]" for path, why in sorted(unsupported))
        raise GitObjectError(
            "commit tree holds entries this packager cannot represent; refusing to build:\n" + listed
        )

    entries.sort(key=lambda entry: entry.path)
    return entries


class BlobReader:
    """Stream blob bytes from one long-lived `git cat-file --batch` process.

    Spawning a process per object would be correct but is minutes slower over a
    tree this size; a single batch keeps the read cheap without giving up the
    guarantee that every byte came from the object database.
    """

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self._process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def read(self, entry: TreeEntry) -> bytes:
        assert self._process.stdin and self._process.stdout
        self._process.stdin.write(entry.oid.encode("ascii") + b"\n")
        self._process.stdin.flush()

        header = self._process.stdout.readline()
        if not header:
            raise GitObjectError(f"git cat-file closed while reading {entry.path} ({entry.oid})")
        fields = header.decode("utf-8", errors="replace").split()
        if len(fields) != 3 or fields[1] != "blob":
            raise GitObjectError(f"unexpected cat-file header for {entry.path}: {header!r}")
        declared = int(fields[2])
        if declared != entry.size:
            raise GitObjectError(
                f"{entry.path}: tree says {entry.size} bytes, object says {declared}"
            )

        payload = self._process.stdout.read(declared)
        if len(payload) != declared:
            raise GitObjectError(
                f"{entry.path}: short read, expected {declared} bytes, got {len(payload)}"
            )
        self._process.stdout.read(1)  # trailing newline emitted by --batch

        if payload[:len(LFS_POINTER_PREFIX)] == LFS_POINTER_PREFIX and declared <= LFS_POINTER_MAX_BYTES:
            raise GitObjectError(
                f"{entry.path} is a Git LFS pointer; its real content is not in this "
                "repository and fetching it is not authorized for a source package"
            )
        return payload

    def close(self) -> None:
        if self._process.poll() is None:
            assert self._process.stdin
            self._process.stdin.close()
            self._process.wait(timeout=30)

    def __enter__(self) -> "BlobReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def link_target_is_safe(target: str) -> bool:
    """Whether a symlink target stays inside the extracted archive."""
    text = target.replace("\\", "/")
    if not text or text.startswith("/") or ":" in text:
        return False
    parts = PurePosixPath(text).parts
    depth = 0
    for part in parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        elif part not in (".", ""):
            depth += 1
    return True

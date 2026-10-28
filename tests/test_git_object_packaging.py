# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""The archive must be a function of the commit, not of how it was checked out.

A Git-clean checkout does not imply byte-identical files: `core.autocrlf`,
`core.eol` and `.gitattributes` all decide how a blob is materialized, and Git
converts back when it compares, so two clean checkouts of one commit can hold
different bytes and both report no modifications. These tests build real
synthetic repositories rather than mocking Git, because the bug lived precisely
in the gap between what Git reports and what the filesystem holds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tools import git_object_source as gos
from tools import package_source_distribution as psd

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "probe")
    _git(repo, "config", "core.autocrlf", "false")


def _commit(repo: Path, message: str = "c") -> str:
    _git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=probe@example.invalid",
                    "-c", "user.name=probe", "commit", "-q", "-m", message], check=True)
    return _git(repo, "rev-parse", "HEAD").strip()


def _minimal_repo(root: Path) -> tuple[Path, str]:
    """A repo holding every REQUIRED_PATH plus an executable and a binary."""
    repo = root / "src"
    _init(repo)
    for rel in psd.REQUIRED_PATHS:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"content of {rel}\nsecond line\n".encode())
    (repo / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    # POSIX Git honors the on-disk execute bit on the later add -A too. Keep
    # the explicit index mode below for filesystems that cannot represent it.
    (repo / "run.sh").chmod(0o755)
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    # The document policy ships inside the tree it governs, so a synthetic tree
    # needs its own -- the real inventory describes a different repository and
    # every one of its entries would read as stale here.
    from tools import public_content_policy as pcp

    tracked = [p.relative_to(repo).as_posix() for p in repo.rglob("*")
               if p.is_file() and ".git" not in p.parts]
    policy_file = repo / pcp.POLICY_TRACKED_PATH
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps(pcp.generate(tracked, public_required=frozenset(tracked)), indent=2) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "run.sh")
    return repo, _commit(repo)


def _clone_with(source: Path, dest: Path, autocrlf: str, commit: str) -> Path:
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(source), str(dest)], check=True)
    _git(dest, "config", "core.autocrlf", autocrlf)
    for path in dest.iterdir():
        if path.name != ".git":
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    _git(dest, "checkout", "-q", "-f", commit, "--", ".")
    return dest


class TestTreeReader:
    def test_reads_modes_types_and_sizes_from_the_tree(self, tmp_path: Path) -> None:
        repo, commit = _minimal_repo(tmp_path)
        entries = {e.path: e for e in gos.read_tree(repo, commit)}
        assert entries["run.sh"].mode == gos.EXECUTABLE_FILE
        assert entries["run.sh"].archive_mode == 0o755
        assert entries["pyproject.toml"].mode == gos.REGULAR_FILE
        assert entries["pyproject.toml"].archive_mode == 0o644
        for entry in entries.values():
            assert entry.object_type == "blob"
            assert entry.size == len(subprocess.run(
                ["git", "-C", str(repo), "cat-file", "blob", entry.oid],
                capture_output=True, check=True).stdout)

    def test_entries_are_sorted_deterministically(self, tmp_path: Path) -> None:
        repo, commit = _minimal_repo(tmp_path)
        paths = [e.path for e in gos.read_tree(repo, commit)]
        assert paths == sorted(paths)

    def test_submodule_fails_closed(self, tmp_path: Path) -> None:
        """A gitlink's content is not in this repository; silently dropping it lies."""
        repo, base = _minimal_repo(tmp_path)
        # A real gitlink without fetching a submodule: point the entry at any commit
        # object this repository already has.
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/sub")
        commit = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=probe@example.invalid",
             "-c", "user.name=probe", "commit", "-q", "-m", "with gitlink"],
            capture_output=True, text=True)
        if commit.returncode != 0:
            pytest.skip(f"could not stage a gitlink on this host: {commit.stderr.strip()[:120]}")
        head = _git(repo, "rev-parse", "HEAD").strip()
        with pytest.raises(gos.GitObjectError, match="submodule"):
            gos.read_tree(repo, head)

    def test_missing_object_fails_closed(self, tmp_path: Path) -> None:
        repo, commit = _minimal_repo(tmp_path)
        entry = next(e for e in gos.read_tree(repo, commit) if e.path == "blob.bin")
        broken = gos.TreeEntry(path=entry.path, mode=entry.mode, object_type="blob",
                               oid="0" * 40, size=entry.size)
        with gos.BlobReader(repo) as blobs:
            with pytest.raises(gos.GitObjectError):
                blobs.read(broken)

    def test_size_disagreement_fails_closed(self, tmp_path: Path) -> None:
        repo, commit = _minimal_repo(tmp_path)
        entry = next(e for e in gos.read_tree(repo, commit) if e.path == "blob.bin")
        lying = gos.TreeEntry(path=entry.path, mode=entry.mode, object_type="blob",
                              oid=entry.oid, size=entry.size + 1)
        with gos.BlobReader(repo) as blobs:
            with pytest.raises(gos.GitObjectError, match="tree says"):
                blobs.read(lying)


class TestLinkTargetSafety:
    @pytest.mark.parametrize("target", ["docs/readme.md", "./a/b", "a/../b"])
    def test_safe_relative_targets_accepted(self, target: str) -> None:
        assert gos.link_target_is_safe(target)

    @pytest.mark.parametrize("target", ["/etc/passwd", "../outside", "a/../../escape",
                                        "C:/Windows", "", "C:\\Users\\x"])
    def test_escaping_or_absolute_targets_rejected(self, target: str) -> None:
        assert not gos.link_target_is_safe(target)


class TestCheckoutByteIndependence:
    """The headline property: materialization cannot change the archive."""

    def test_dirty_policy_cannot_promote_private_documents(self, tmp_path: Path) -> None:
        from tools import public_content_policy as pcp

        source, _commit_id = _minimal_repo(tmp_path)
        note = source / "internal-note.md"
        note.write_text("Private development record\n", encoding="utf-8")
        policy_file = source / pcp.POLICY_TRACKED_PATH
        policy = json.loads(policy_file.read_text(encoding="utf-8"))
        policy["documents"][note.name] = "INTERNAL_HISTORICAL"
        policy["dispositions"][note.name] = "internal_historical_final"
        policy_file.write_text(json.dumps(policy), encoding="utf-8")
        _commit(source)
        baseline = psd.build(source, tmp_path / "before")

        policy["documents"][note.name] = "PUBLIC_REQUIRED"
        policy["dispositions"][note.name] = "public_required_final"
        policy_file.write_text(json.dumps(policy), encoding="utf-8")
        dirty = psd.build(source, tmp_path / "after", allow_dirty=True)
        assert note.name not in dirty.included
        assert baseline.archive_sha256 == dirty.archive_sha256

    def test_two_clean_checkouts_with_different_bytes_build_identical_archives(
        self, tmp_path: Path
    ) -> None:
        source, commit = _minimal_repo(tmp_path)
        a = _clone_with(source, tmp_path / "a", "false", commit)  # LF on disk
        b = _clone_with(source, tmp_path / "b", "true", commit)   # CRLF on disk

        def aggregate(root: Path) -> tuple[int, int]:
            total = crlf = 0
            for rel in _git(root, "ls-files").split("\n"):
                rel = rel.strip()
                if not rel or not (root / rel).is_file():
                    continue
                data = (root / rel).read_bytes()
                total += len(data)
                crlf += 1 if b"\r\n" in data else 0
            return total, crlf

        bytes_a, crlf_a = aggregate(a)
        bytes_b, crlf_b = aggregate(b)
        assert (bytes_a, crlf_a) != (bytes_b, crlf_b), (
            "the checkouts materialized identically, so this test would prove nothing"
        )
        assert _git(a, "rev-parse", "HEAD").strip() == _git(b, "rev-parse", "HEAD").strip()
        assert not _git(a, "status", "--porcelain").strip()
        assert not _git(b, "status", "--porcelain").strip()

        ra = psd.build(a, tmp_path / "out-a")
        rb = psd.build(b, tmp_path / "out-b")

        assert ra.archive_sha256 == rb.archive_sha256
        assert ra.archive_path.read_bytes() == rb.archive_path.read_bytes()
        assert ra.unpacked_bytes == rb.unpacked_bytes
        assert ra.manifest_path.read_text(encoding="utf-8") == rb.manifest_path.read_text(encoding="utf-8")

    def test_archive_bytes_equal_git_blob_bytes(self, tmp_path: Path) -> None:
        """Not a normalized variant -- the exact committed bytes."""
        source, commit = _minimal_repo(tmp_path)
        b = _clone_with(source, tmp_path / "crlf", "true", commit)
        result = psd.build(b, tmp_path / "out")
        entries = {e.path: e for e in gos.read_tree(b, commit)}
        checked = 0
        with tarfile.open(result.archive_path) as tar:
            for member in tar.getmembers():
                rel = member.name.split("/", 1)[1]
                blob = subprocess.run(["git", "-C", str(b), "cat-file", "blob", entries[rel].oid],
                                      capture_output=True, check=True).stdout
                extracted = tar.extractfile(member).read()
                assert extracted == blob, f"{rel} does not match its blob"
                checked += 1
        assert checked == len(result.included)

    def test_executable_mode_survives_a_checkout_that_cannot_represent_it(
        self, tmp_path: Path
    ) -> None:
        source, commit = _minimal_repo(tmp_path)
        b = _clone_with(source, tmp_path / "co", "true", commit)
        result = psd.build(b, tmp_path / "out")
        with tarfile.open(result.archive_path) as tar:
            modes = {m.name.split("/", 1)[1]: m.mode for m in tar.getmembers()}
        assert modes["run.sh"] == 0o755
        assert modes["pyproject.toml"] == 0o644


class TestManifest:
    def test_records_commit_tree_and_object_provenance(self, tmp_path: Path) -> None:
        import json

        source, commit = _minimal_repo(tmp_path)
        result = psd.build(source, tmp_path / "out")
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["commit_oid"] == commit
        assert manifest["tree_oid"] == _git(source, "rev-parse", f"{commit}^{{tree}}").strip()
        assert manifest["source_byte_origin"] == "git_object_database"
        assert manifest["is_official_release"] is False
        entries = {e.path: e for e in gos.read_tree(source, commit)}
        for member in manifest["members"]:
            entry = entries[member["path"]]
            assert member["object_id"] == entry.oid
            assert member["object_bytes"] == entry.size
            assert member["git_mode"] == entry.mode

    def test_manifest_carries_no_wall_clock_or_absolute_path(self, tmp_path: Path) -> None:
        """A timestamp or a build directory would break deterministic equality."""
        source, _ = _minimal_repo(tmp_path)
        result = psd.build(source, tmp_path / "out")
        text = result.manifest_path.read_text(encoding="utf-8")
        assert "created_utc" not in text
        assert str(tmp_path) not in text
        assert "C:\\\\" not in text and "/home/" not in text

    def test_member_order_is_stable(self, tmp_path: Path) -> None:
        import json

        source, _ = _minimal_repo(tmp_path)
        result = psd.build(source, tmp_path / "out")
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        paths = [m["path"] for m in manifest["members"]]
        assert paths == sorted(paths)


class TestGuardsRetained:
    def test_clean_tree_guard_still_refuses_a_dirty_checkout(self, tmp_path: Path) -> None:
        """Git-object reads make the payload safe; they do not make dirt acceptable."""
        source, _ = _minimal_repo(tmp_path)
        (source / "README.md").write_bytes(b"locally edited\n")
        with pytest.raises(psd.PackagingError, match="tracked files are modified"):
            psd.build(source, tmp_path / "out")

    def test_fatal_paths_still_abort(self, tmp_path: Path) -> None:
        source, _ = _minimal_repo(tmp_path)
        (source / ".env").write_bytes(b"SECRET=x\n")
        _commit(source, "add env")
        with pytest.raises(psd.PackagingError, match="must never exist"):
            psd.build(source, tmp_path / "out", check_only=True)

    def test_output_inside_the_repository_still_refused(self, tmp_path: Path) -> None:
        source, _ = _minimal_repo(tmp_path)
        with pytest.raises(psd.PackagingError, match="outside the repository"):
            psd.build(source, source / "dist")

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Publication scanning preserves deleted content and metadata as review findings."""

from __future__ import annotations

import json
import io
from pathlib import Path
import subprocess

import pytest

from tools import publication_audit as audit


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "--no-replace-objects", "-C", str(root), *args],
                                   stderr=subprocess.DEVNULL, timeout=30).decode().strip()


def test_findings_never_return_matched_credentials_or_private_names() -> None:
    token = "ghp_" + "a1b2c3d4" * 5
    owner_mail = "test-person" + "@private.test"
    rows = audit.findings(f"credential: {token}\nauthor: {owner_mail}", path="fixture.txt")
    assert {row["rule"] for row in rows} == {"github-token", "email"}
    encoded = json.dumps(rows)
    assert token not in encoded and owner_mail not in encoded
    assert [row["line"] for row in rows] == [1, 2]


def test_reserved_examples_are_distinguished_from_actual_metadata() -> None:
    assert audit.findings("demo@example.com") == []
    assert audit.findings("person@research.test")[0]["rule"] == "email"


def test_sensitive_filename_does_not_repeat_value_in_report(tmp_path: Path) -> None:
    token = "ghp_" + "a1b2c3d4" * 5
    (tmp_path / (token + ".txt")).write_text(token)
    report = audit.scan_directory(tmp_path)
    assert len(report["findings"]) == 2
    assert token not in json.dumps(report)


def test_missing_candidate_is_not_reported_clean(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        audit.scan_directory(tmp_path / "missing")


def test_unreadable_later_file_preserves_findings_and_binary_filename_is_scanned(tmp_path: Path, monkeypatch) -> None:
    token = "ghp_" + "a1b2c3d4" * 5
    (tmp_path / (token + ".bin")).write_bytes(b"\x00\xff")
    blocked = tmp_path / "z-unreadable.txt"
    blocked.write_text("unreadable")
    original = Path.read_bytes
    def read(path):
        if path == blocked:
            raise PermissionError("synthetic inaccessible file")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    result = audit.scan_directory(tmp_path)
    assert result["complete"] is False
    assert result["findings"][0]["rule"] == "github-token"
    assert {row["reason"] for row in result["unreviewed"]} == {"binary", "read_failed"}
    assert token not in json.dumps(result)


def test_history_includes_removed_secret_author_trailer_and_annotated_tag(tmp_path: Path) -> None:
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "Synthetic contributor")
    git(tmp_path, "config", "user.email", "synthetic@research.test")
    token = "ghp_" + "q7k8m9z0" * 5
    (tmp_path / "removed.txt").write_text(token)
    git(tmp_path, "add", "removed.txt")
    git(tmp_path, "commit", "--quiet", "-m", "Synthetic first commit\n\nCo-authored-by: Example <test@research.test>")
    git(tmp_path, "rm", "--quiet", "removed.txt")
    (tmp_path / "source.py").write_text("value = 1\n")
    git(tmp_path, "add", "source.py")
    git(tmp_path, "commit", "--quiet", "-m", "Remove synthetic credential")
    git(tmp_path, "tag", "-a", "candidate", "-m", "Tag contact: tag@research.test")
    result = audit.scan_git(tmp_path, ["refs/tags/candidate"])
    assert result["counts"]["commit"] == 2
    assert result["counts"]["tag"] == 1
    assert any(row["rule"] == "github-token" and row["path"] == "[blob]" for row in result["findings"])
    assert {row["path"] for row in result["findings"] if row["rule"] == "email"} >= {"[commit]", "[tag]"}
    assert token not in json.dumps(result)
    assert not result["unreviewed"]


def test_non_utf8_and_oversized_content_never_pass_silently(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "picture.bin").write_bytes(b"\x89PNG\x00\xff")
    (tmp_path / "large.txt").write_text("a" * 101)
    monkeypatch.setattr(audit, "MAX_BLOB_BYTES", 100)
    result = audit.scan_directory(tmp_path)
    assert {row["reason"] for row in result["unreviewed"]} == {"binary", "size_limit"}
    assert result["counts"]["files"] == 2


def test_late_history_read_failure_preserves_earlier_findings(tmp_path: Path, monkeypatch) -> None:
    oid = "a" * 40
    monkeypatch.setattr(audit, "_git", lambda *args: (oid + "\n" + "b" * 40 + "\n").encode()
                        if "rev-list" in args else (oid + "\n").encode())
    monkeypatch.setattr(audit, "git_executable", lambda: "unused")
    class Reader:
        stdin = io.BytesIO()
        def kill(self):
            pass
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(audit.subprocess, "Popen", lambda *args, **kwargs: Reader())
    token = ("ghp_" + "m5n6o7p8" * 5).encode()
    calls = 0
    def interrupted(_reader, _oid, _limit):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "blob", token, len(token)
        raise RuntimeError("simulated timeout")
    monkeypatch.setattr(audit, "_read_blob", interrupted)
    result = audit.scan_git(tmp_path, ["HEAD"])
    assert result["complete"] is False
    assert result["counts"]["blob"] == 1
    assert result["findings"][0]["rule"] == "github-token"
    assert token.decode() not in json.dumps(result)


def test_directory_scan_does_not_follow_link_outside_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    other = tmp_path / "outside"
    other.mkdir()
    (other / "private.txt").write_text("person@research.test")
    try:
        (source / "linked").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    result = audit.scan_directory(source)
    assert result["findings"] == []
    assert result["unreviewed"] == [{"path": "linked", "reason": "link"}]

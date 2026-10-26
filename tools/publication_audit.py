#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Read-only publication review of a source directory or exact reachable Git refs.

Reports contain rule ids, object ids and line numbers, never matched values.
This is a bounded secret/privacy scanner, not proof that personal data is absent.
Binary payloads are inventoried as unreviewed instead of silently passing.
Keep the detailed report outside the source/public candidate.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
from typing import Any


RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider-key": re.compile(r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{20,}\b"),
    "url-userinfo": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.I),
    "personal-home": re.compile(r"(?:[A-Z]:[\\/]+Users[\\/]+|/(?:home|Users)/)[^\s/\\\"'<>]+", re.I),
    "machine-workspace": re.compile(r"[A-Z]:[\\/]+(?:dev[\\/]+(?:repos|worktrees)|XRMLTaskRuns)\b", re.I),
    "email": re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"),
}
SECRET_RULES = frozenset({"private-key", "provider-key", "github-token", "aws-access-key", "slack-token", "jwt", "url-userinfo"})
MAX_BLOB_BYTES = 16 * 1024 * 1024


def safe_location(value: str) -> str:
    """A secret in a filename/ref must not be repeated by its finding location."""
    for rule, pattern in RULES.items():
        value = pattern.sub(f"[redacted-{rule}]", value)
    return value


def git_executable() -> str:
    """Use Git's real Windows executable so timeout cleanup owns the reader."""
    located = shutil.which("git")
    if located is None:
        raise RuntimeError("Git is required for history review")
    executable = Path(located)
    native = executable.parent.parent / "mingw64" / "bin" / "git.exe"
    return str(native if os.name == "nt" and executable.parent.name == "cmd" and native.is_file() else executable)


def findings(text: str, *, object_id: str = "", path: str = "") -> list[dict[str, Any]]:
    """Keep even likely synthetic hits for review; never return the value."""
    result = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for rule, pattern in RULES.items():
            for match in pattern.finditer(line):
                value = match.group()
                # Reserved documentation domains and explicit generic account examples.
                if rule == "email" and (value.lower().endswith(("@example.com", "@example.org", "@example.net", "@localhost.local", "@users.noreply.github.com"))):
                    continue
                if rule == "personal-home" and re.search(r"[/\\](?:EXAMPLE|example|test|demo|user|username|runner|<[^>]+>)$", value):
                    continue
                result.append({"rule": rule, "kind": "credential_candidate" if rule in SECRET_RULES else "privacy_review",
                               "object_id": object_id, "path": safe_location(path), "line": line_no,
                               "fingerprint": hashlib.sha256(value.encode()).hexdigest()})
    return result


def _git(repo: Path, *args: str) -> bytes:
    # Actual stored objects, never replace-ref overlays; no environment/config change.
    process = subprocess.run([git_executable(), "--no-replace-objects", "-C", str(repo), *args],
                             capture_output=True, timeout=180, check=False)
    if process.returncode:
        raise RuntimeError("Git audit read failed; no stderr content is reproduced")
    return process.stdout


def _read_blob(reader: subprocess.Popen, oid: str, limit: int) -> tuple[str, bytes | None, int]:
    assert reader.stdin is not None and reader.stdout is not None
    reader.stdin.write(oid.encode("ascii") + b"\n")
    reader.stdin.flush()
    header = reader.stdout.readline().decode("ascii").split()
    if len(header) != 3 or header[0] != oid:
        raise RuntimeError("Unexpected object response; audit incomplete")
    size = int(header[2])
    if size <= limit:
        data = reader.stdout.read(size)
        if len(data) != size:
            raise RuntimeError("Short object response; audit incomplete")
    else:
        data = None
        remaining = size
        while remaining:
            chunk = reader.stdout.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeError("Short large-object response; audit incomplete")
            remaining -= len(chunk)
    if reader.stdout.read(1) != b"\n":
        raise RuntimeError("Invalid object response terminator; audit incomplete")
    return header[1], data, size


def scan_git(repo: Path, refs: list[str], *, metadata_only: bool = False) -> dict[str, Any]:
    """Scan unique reachable blobs, original paths, commit and annotated-tag metadata."""
    resolved = [_git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{object}").decode().strip() for ref in refs]
    if any(len(oid) != 40 for oid in resolved):
        raise RuntimeError("This tree parser currently supports SHA-1 Git repositories only")
    result: dict[str, Any] = {"schema": "xrml_publication_audit_v1", "mode": "reachable_git_objects",
        "requested_refs": [safe_location(ref) for ref in refs], "resolved_objects": resolved, "findings": [], "unreviewed": [],
        "complete": True, "counts": {}, "identity_fingerprints": [], "limits": ["Pattern review is not proof of absence; inspect findings before publishing.",
        "Binary and oversized objects are inventoried, not content-reviewed.",
        "Remote hosting surfaces are outside this local scan."]}
    # Commit metadata remains reviewable if this host cannot enumerate its much
    # larger historical tree/blob graph. Preserve that partial scope explicitly.
    commits = _git(repo, "rev-list", *resolved).decode().splitlines()
    objects: list[str] = []
    if metadata_only:
        result["complete"] = False
        result["limits"].append("Metadata-only scope: historical trees and blob contents were not scanned.")
    else:
        try:
            objects = _git(repo, "rev-list", "--objects", "--no-object-names", *resolved).decode().splitlines()
        except (subprocess.TimeoutExpired, RuntimeError):
            result["complete"] = False
            result["limits"].append("Historical object enumeration failed or exceeded 180 seconds; commit/tag metadata only.")
    # Include annotated tag objects themselves, not only their peeled commits.
    object_ids = sorted(set(objects) | set(commits) | set(resolved))
    counts: Counter = Counter()
    identities: set[tuple[str, str]] = set()
    reader = subprocess.Popen([git_executable(), "--no-replace-objects", "-C", str(repo), "cat-file", "--batch"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = threading.Timer(180, reader.kill)
    deadline.daemon = True
    deadline.start()
    try:
        for oid in object_ids:
            kind, data, size = _read_blob(reader, oid, MAX_BLOB_BYTES)
            counts[kind] += 1
            if data is None:
                result["unreviewed"].append({"object_id": oid, "type": kind, "bytes": size, "reason": "size_limit"})
            elif kind == "tree":
                # A tree is <mode> <name> NUL <20-byte oid>; each filename is scanned
                # at every historical location, including before moves/deletions.
                offset = 0
                while offset < len(data):
                    end = data.index(b"\0", offset)
                    name = data[offset:end].split(b" ", 1)[1].decode("utf-8", errors="replace")
                    result["findings"].extend(findings(name, object_id=oid, path="[tree-entry]"))
                    counts["tree_paths"] += 1
                    offset = end + 21
            else:
                try:
                    text = data.decode("utf-8")
                    if "\0" in text:
                        raise UnicodeError("binary")
                except UnicodeError:
                    result["unreviewed"].append({"object_id": oid, "type": kind, "bytes": size, "reason": "binary"})
                else:
                    result["findings"].extend(findings(text, object_id=oid, path=f"[{kind}]"))
                    counts["text_objects_scanned"] += 1
                    if kind in {"commit", "tag"}:
                        for header in text.split("\n\n", 1)[0].splitlines():
                            label, _, value = header.partition(" ")
                            if label in {"author", "committer", "tagger"}:
                                # Keep author name + email inventory separately from
                                # regex warnings, stripping only timestamp/timezone.
                                identity = value.rsplit(" ", 2)[0]
                                identities.add((label, hashlib.sha256(identity.encode()).hexdigest()))
    except (RuntimeError, OSError, ValueError):
        result["complete"] = False
        result["limits"].append("Object traversal was interrupted; findings and counts cover only objects already read.")
    finally:
        deadline.cancel()
        if reader.stdin:
            try:
                reader.stdin.close()
            except OSError:
                pass
        try:
            reader.wait(timeout=10)
        except subprocess.TimeoutExpired:
            reader.kill()
            reader.wait(timeout=10)
    result["counts"] = dict(counts)
    result["identity_fingerprints"] = [{"field": key, "sha256": value} for key, value in sorted(identities)]
    return result


def scan_directory(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise RuntimeError("Candidate directory does not exist")
    result: dict[str, Any] = {"schema": "xrml_publication_audit_v1", "mode": "directory",
        "complete": True, "findings": [], "unreviewed": [], "counts": {}, "limits": [
        "Only this directory is scanned; Git history and remote hosting surfaces are not.",
        "Pattern review is not proof of absence; binary and oversized files need separate review."]}
    counts: Counter = Counter()
    files: list[Path] = []
    def unreadable(_error: OSError) -> None:
        result["complete"] = False
        result["unreviewed"].append({"path": "[unreadable-directory]", "reason": "enumeration_failed"})

    for parent, directories, names in os.walk(root, followlinks=False, onerror=unreadable):
        for dirname in list(directories):
            directory = Path(parent) / dirname
            if dirname == ".git":
                directories.remove(dirname)
                continue
            result["findings"].extend(findings(directory.relative_to(root).as_posix(), path="[directory-name]"))
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                directories.remove(dirname)
                result["unreviewed"].append({"path": safe_location(directory.relative_to(root).as_posix()), "reason": "link"})
        files.extend(Path(parent) / name for name in names)
    for file in sorted(files):
        rel = safe_location(file.relative_to(root).as_posix())
        counts["files"] += 1
        result["findings"].extend(findings(file.relative_to(root).as_posix(), path="[filename]"))
        try:
            if file.is_symlink() or getattr(file, "is_junction", lambda: False)():
                result["unreviewed"].append({"path": rel, "reason": "link"})
                continue
            info = file.stat()
            if not stat.S_ISREG(info.st_mode):
                result["unreviewed"].append({"path": rel, "reason": "special_file"})
                continue
            if info.st_size > MAX_BLOB_BYTES:
                result["unreviewed"].append({"path": rel, "reason": "size_limit"})
                continue
            data = file.read_bytes()
        except OSError:
            result["complete"] = False
            result["unreviewed"].append({"path": rel, "reason": "read_failed"})
            continue
        try:
            text = data.decode("utf-8")
            if "\0" in text:
                raise UnicodeError("binary")
        except UnicodeError:
            result["unreviewed"].append({"path": rel, "bytes": len(data), "reason": "binary"})
        else:
            result["findings"].extend(findings(text, path=rel))
            counts["text_files_scanned"] += 1
    result["counts"] = dict(counts)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--ref", action="append", default=[])
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--metadata-only", action="store_true", help="explicit partial history scope, no historical blobs")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.repo) == bool(args.directory) or (args.directory and (args.ref or args.metadata_only)):
        parser.error("choose --repo with explicit --ref, or --directory")
    if args.repo and not args.ref:
        parser.error("--repo requires at least one explicit --ref")
    root = (args.repo or args.directory).resolve()
    if args.report.resolve().is_relative_to(root):
        parser.error("keep detailed reports outside the scanned source")
    try:
        result = scan_git(root, args.ref, metadata_only=args.metadata_only) if args.repo else scan_directory(root)
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        result = {"schema": "xrml_publication_audit_v1", "complete": False, "counts": {}, "findings": [],
                  "unreviewed": [], "failure": type(exc).__name__,
                  "limits": ["Audit did not complete; no privacy or secret clearance is established."]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "rules": dict(Counter(x["rule"] for x in result["findings"])),
                      "unreviewed": len(result["unreviewed"]), "scan_complete": result.get("complete", True),
                      "complete_privacy_clearance": False}))
    if not result.get("complete", True):
        return 2
    return 1 if result["findings"] or result["unreviewed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

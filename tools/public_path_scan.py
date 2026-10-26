#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Fail the build when an owner-machine path reaches a public surface.

Scope is deliberately narrow: the surfaces a reader would *execute*, *configure*
or follow as current instruction. A path-shaped string is not automatically a
leak -- documentation examples, synthetic fixtures and the scanner's own patterns
all look like paths and none of them disclose anything.

Historical audit and development-state records are counted for visibility but not
judged here; their disposition is a separate decision about what belongs in a
public snapshot at all.

Matched literals are never printed. A finding is identified by its path, line and
a truncated hash, so the report can be kept without republishing the value it
was raised about.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

# The type checker resolves the canonical module; the runtime keeps the fallback that
# lets this file run as a standalone script, where `tools` is not a package root. Without
# the split, mypy resolves this file as `tools.public_path_scan` and the bare fallback
# import has no module to find.
if TYPE_CHECKING:
    from tools import public_content_policy
else:
    try:
        from tools import public_content_policy
    except ImportError:  # pragma: no cover - direct script use
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import public_content_policy

# Roots that identify a real machine rather than an illustration. `EXAMPLE` and
# friends are deliberately absent -- that is the point of them.
OWNER_ROOTS = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/]USER\b"
    r"|[A-Za-z]:[\\/]dev[\\/]repos\b"
    r"|[\\/]source[\\/]repos[\\/]xrml"
    r"|[A-Za-z]:[\\/]XRMLData\b"
    # Any drive letter, not the two this machine happens to use: broader detection,
    # and it keeps two real roots out of a file that ships publicly.
    r"|/[a-z]/Users/USER\b|/[a-z]/dev/repos\b|/Users/USER/"
    r"|[A-Za-z]:[\\/]Program Files[\\/]PowerShell",
    re.IGNORECASE,
)

# Neutral stand-ins that must never be reported as leaks.
PLACEHOLDERS = re.compile(
    r"<(REPO_ROOT|PROJECT_ROOT|WORKTREE_ROOT|RUNTIME_ROOT|USER_HOME)>"
    r"|%[A-Z_]+%|\$\{[A-Z_]+\}|\$env:[A-Za-z_]+"
    r"|[\\/]Users[\\/]EXAMPLE\b|example-repo|sample-project|Example Worktrees",
    re.IGNORECASE,
)

EXECUTABLE_SUFFIX = {".bat", ".cmd", ".ps1", ".sh"}
CONFIG_SUFFIX = {".toml", ".json", ".yaml", ".yml", ".ini", ".cfg"}
# Derived, non-authoritative. This used to be a hand-maintained six-name set, and
# it was the fail-open hole: a document outside it was never judged as a public
# surface, and a newly added document was outside it by default. The names now
# come from `tools/public_content_policy.json`, which the packager reads too, so
# scanner and archive cannot disagree about what "public" means.
#
# Kept only so existing readers of the symbol keep working. Reassigning it does
# not change scanning -- `surface_of` asks the policy, never this view.
CURRENT_PUBLIC_DOCS = frozenset(public_content_policy.load_cached().public_documents())
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|\.spec\.|\.test\.")
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
               ".webm", ".woff", ".woff2", ".ttf", ".gz"}

# The scanner defines the very patterns it looks for, and its tests must contain
# real owner-root forms to prove detection actually works. Scanning either would
# be a guaranteed self-report. This is a two-file exception, not a rule about
# tools/ or tests/ -- every other fixture is still judged.
SELF = "tools/public_path_scan.py"
SELF_PATHS = frozenset({SELF, "tests/test_public_path_scan.py"})

# Internal records. Their bytes stay in the repository; whether they belong in a
# public snapshot is a separate decision, so they are counted, not judged.
HISTORICAL_RECORD = re.compile(
    r"^docs/audit/|^docs/m\d|^docs/dev-current-state|^design/stitch/"
    r"|final-audit-handoff|no-write-apply-rehearsal",
    re.IGNORECASE,
)


def surface_of(path: str, excluded: frozenset[str] = frozenset()) -> str | None:
    """Which actionable public surface `path` is, or None if it is not one.

    `excluded` names paths the packager keeps out of the archive; a file that
    never ships cannot leak through a public surface, so it is counted as
    deferred rather than raised.
    """
    if path in SELF_PATHS or path in excluded:
        return None
    # The policy is consulted before the historical short-circuit, not after. Three
    # promoted design-intake documents live under `design/stitch/`, which the
    # historical pattern matches -- so judging that first meant a document the
    # packager ships was never scanned as a public surface at all. Anything the
    # policy calls public is a public surface, wherever it happens to sit.
    if path in public_content_policy.load_cached().public_documents():
        return "current_doc"
    if HISTORICAL_RECORD.search(path):
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if TEST_PATH.search(path):
        return "fixture"
    if suffix in EXECUTABLE_SUFFIX:
        return "executable"
    if suffix in CONFIG_SUFFIX:
        return "config"
    return None


def scan_text(path: str, text: str) -> list[dict]:
    surface = surface_of(path)
    findings: list[dict] = []
    for lineno, line in enumerate(text.split("\n"), 1):
        # A URL is not a local path, and stripping it first stops `https://x/Users/...`
        # style false hits.
        probe = re.sub(r"https?://\S+", "", line)
        for match in OWNER_ROOTS.finditer(probe):
            literal = match.group(0)
            findings.append({
                "path": path,
                "line": lineno,
                "surface": surface or "deferred_history",
                "actionable": surface is not None,
                "literal_sha256": hashlib.sha256(literal.encode()).hexdigest()[:16],
            })
    return findings


def scan_commit(repo: Path, revision: str) -> list[dict]:
    raw = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", revision],
                         capture_output=True, check=True).stdout
    findings: list[dict] = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        path = chunk.decode("utf-8")
        if PurePosixPath(path).suffix.lower() in SKIP_SUFFIX:
            continue
        blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{revision}:{path}"],
                              capture_output=True, check=True).stdout
        try:
            findings += scan_text(path, blob.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    findings = scan_commit(args.repo, args.revision)
    actionable = [f for f in findings if f["actionable"]]
    deferred = [f for f in findings if not f["actionable"]]

    print(f"  revision            {args.revision}")
    print(f"  actionable findings {len(actionable)}")
    for surface, count in Counter(f["surface"] for f in actionable).most_common():
        print(f"    {count:5}  {surface}")
    for f in sorted(actionable, key=lambda x: (x["path"], x["line"])):
        print(f"    LEAK {f['surface']:11} {f['path']}:{f['line']}  sha={f['literal_sha256']}")
    print(f"  deferred (historical//docs, not judged here) {len(deferred)}")

    if args.json_out:
        args.json_out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    return 1 if actionable else 0


if __name__ == "__main__":
    raise SystemExit(main())

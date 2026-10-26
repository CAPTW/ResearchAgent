#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Lock the current mypy debt exactly, and fail on any movement in it.

The tree carries a large pre-existing type-error backlog. Gating on "mypy is clean"
is years away; gating on "mypy is exactly as bad as we agreed it was" is available
now, and it is the property that actually stops regressions. So the debt is recorded
diagnostic by diagnostic and compared as a multiset.

Deliberately not line-tolerant. A diagnostic that moves one line is a changed
diagnostic, because the alternative -- fuzzy matching -- is what lets a genuinely new
error hide behind a shifted old one. Fixing a historical error is welcome, but it
still fails the check until the baseline is updated in the same change; a verifier
that quietly accepts improvement is a verifier that quietly accepts anything.

Text output is parsed rather than a machine format: mypy 1.20.2 exposes only
`--junit-xml` and a generic `-O/--output FORMAT` with no documented JSON schema, and
JUnit XML discards the error code. The strict template below parses all 594 current
records with zero unparsed lines, and any line that does not match is fatal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "xrml_mypy_baseline"
SCHEMA_VERSION = "1.0.0"
CLASSIFICATION = "mypy_debt_baselined"

REQUIRED_MYPY_VERSION = "1.20.2"
REQUIRED_EXECUTION_PYTHON = "3.12.12"
REQUIRED_TARGET_PYTHON = "3.11"

BASELINE_RELATIVE = "tools/mypy_baseline.json"
ROOT_MARKERS = ("pyproject.toml", "uv.lock", "app", "tools")

# Ordered, and the order is part of the contract: mypy's own output order follows the
# argument order, and a reordered scope is a different run.
SCOPE: tuple[str, ...] = (
    "app",
    "tools/git_object_source.py",
    "tools/package_source_distribution.py",
    "tools/public_content_policy.py",
    "tools/public_path_scan.py",
)

MYPY_FLAGS: tuple[str, ...] = (
    "--config-file",
    "pyproject.toml",
    "--explicit-package-bases",
    "--no-incremental",
    "--show-error-codes",
    "--show-column-numbers",
    "--no-pretty",
    "--no-color-output",
    "--no-error-summary",
)

COMMAND_ARGUMENTS: tuple[str, ...] = MYPY_FLAGS + SCOPE

# One anchored template, applied to the whole line. `[^:]+` for the path is doing real
# work: it rejects every drive-qualified, UNC and absolute-POSIX path by construction,
# because each of those contains a colon or a leading separator the group cannot cover.
RECORD_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):(?P<column>\d+): (?P<severity>error|note): (?P<message>.*)$"
)
# Split off the trailing code only; the message keeps whatever else it contains.
CODE_RE = re.compile(r"^(?P<message>.*)  \[(?P<code>[a-z][a-z0-9-]*)\]$")

SEVERITIES = ("error", "note")
# Unresolved imports are never debt. They mean the checker could not see a module, so
# every downstream diagnostic is suspect; baselining them would freeze a lie.
IMPORT_CODES = ("import-not-found", "import-untyped", "import")


class BaselineError(RuntimeError):
    """Any condition that must stop the run rather than be absorbed."""


@dataclass(frozen=True, order=False)
class Diagnostic:
    path: str
    line: int
    column: int | None
    severity: str
    code: str | None
    message: str
    occurrence_index: int

    def identity(self) -> tuple[str, int, int | None, str, str | None, str, int]:
        return (self.path, self.line, self.column, self.severity, self.code,
                self.message, self.occurrence_index)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "message_sha256": sha256_text(self.message),
            "occurrence_index": self.occurrence_index,
        }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: object) -> str:
    """The one serialization used everywhere a digest is taken."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sort_key(item: Diagnostic) -> tuple[object, ...]:
    """None sorts deterministically beside integers and strings, never against them."""
    return (
        item.path,
        item.line,
        (item.column is None, item.column if item.column is not None else 0),
        item.severity,
        (item.code is None, item.code if item.code is not None else ""),
        item.message,
        item.occurrence_index,
    )


def resolve_root(start: Path | None = None) -> Path:
    """Derive the repository root from this file, never from the shell's cwd.

    The verifier has to work inside an extracted source with no `.git`, so Git is not
    consulted. It also has to refuse to silently validate a *different* checkout, which
    is exactly what a cwd-derived root would do.
    """
    anchor = (start if start is not None else Path(__file__).resolve().parent).resolve()
    candidates = [parent for parent in (anchor, *anchor.parents)
                  if all((parent / marker).exists() for marker in ROOT_MARKERS)]
    if not candidates:
        raise BaselineError(
            f"no repository root above {anchor.name}: expected {', '.join(ROOT_MARKERS)}"
        )
    root = candidates[0]
    if not (root / "tools" / Path(__file__).name).exists():
        raise BaselineError("resolved root does not contain this verifier")
    return root


def verify_environment() -> None:
    actual_python = ".".join(str(part) for part in sys.version_info[:3])
    if actual_python != REQUIRED_EXECUTION_PYTHON:
        raise BaselineError(
            f"execution Python {actual_python} != required {REQUIRED_EXECUTION_PYTHON}"
        )
    try:
        import mypy
        from mypy.version import __version__ as mypy_version
    except ImportError as exc:  # pragma: no cover - environment defect
        raise BaselineError(f"mypy is not importable in this environment: {exc}") from exc
    if mypy_version != REQUIRED_MYPY_VERSION:
        raise BaselineError(f"mypy {mypy_version} != required {REQUIRED_MYPY_VERSION}")
    module = Path(str(mypy.__file__)).resolve()
    prefix = Path(sys.prefix).resolve()
    if not module.is_relative_to(prefix):
        # A global mypy on PATH must not be able to satisfy this check: the whole
        # baseline is only meaningful against one exact distribution.
        raise BaselineError(f"mypy resolves outside the active environment prefix {prefix}")


def normalized_mypy_config(root: Path) -> dict[str, Any]:
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    tool = document.get("tool", {})
    mypy_table = dict(tool.get("mypy", {}))
    overrides = [dict(entry) for entry in mypy_table.pop("overrides", [])]
    for entry in overrides:
        module = entry.get("module")
        if isinstance(module, list):
            entry["module"] = list(module)
    return {"settings": mypy_table, "overrides": overrides}


def compute_config_digest(root: Path) -> str:
    """Hash only the mypy verification contract, and all of it.

    Scope and command arguments go in as ordered sequences, not sets: reordering them
    produces a different mypy invocation, so it must produce a different digest.
    """
    config = normalized_mypy_config(root)
    payload = {
        "mypy_settings": config["settings"],
        "mypy_overrides": config["overrides"],
        "target_python": REQUIRED_TARGET_PYTHON,
        "scope": list(SCOPE),
        "command_arguments": list(COMMAND_ARGUMENTS),
        "explicit_package_bases": "--explicit-package-bases" in COMMAND_ARGUMENTS,
    }
    return sha256_text(canonical_json(payload))


def in_scope(path: str) -> bool:
    return any(path == entry or path.startswith(entry + "/") for entry in SCOPE)


def parse_output(text: str) -> list[Diagnostic]:
    """Every non-empty line must be a diagnostic. Anything else stops the run."""
    parsed: list[tuple[str, int, int | None, str, str | None, str]] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw_line:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_line):
            raise BaselineError("control or ANSI characters in mypy output")
        match = RECORD_RE.match(raw_line)
        if match is None:
            raise BaselineError(f"unparsed mypy output line: {raw_line[:120]!r}")
        message = match["message"]
        code: str | None = None
        code_match = CODE_RE.match(message)
        if code_match is not None:
            message, code = code_match["message"], code_match["code"]
        severity = match["severity"]
        if severity not in SEVERITIES:  # pragma: no cover - regex already constrains it
            raise BaselineError(f"unknown severity {severity!r}")
        if severity == "error" and code is None:
            raise BaselineError(f"error without a code: {raw_line[:120]!r}")
        if code in IMPORT_CODES:
            raise BaselineError(f"unresolved-import diagnostic may not be baselined: {code}")
        path = match["path"].replace("\\", "/")
        if path.startswith("/") or "//" in path or ".." in path.split("/"):
            raise BaselineError(f"non-repository-relative path: {path!r}")
        if not in_scope(path):
            raise BaselineError(f"path outside the fixed scope: {path!r}")
        line, column = int(match["line"]), int(match["column"])
        if line <= 0 or column <= 0:
            raise BaselineError(f"non-positive position in {raw_line[:120]!r}")
        parsed.append((path, line, column, severity, code, message))

    # occurrence_index counts identical predecessors, which makes it independent of the
    # order mypy happened to emit duplicates in: identical records are indistinguishable,
    # so the resulting set of (identity, index) pairs is the same either way.
    diagnostics: list[Diagnostic] = []
    seen: dict[tuple[str, int, int | None, str, str | None, str], int] = {}
    for record in parsed:
        index = seen.get(record, 0)
        seen[record] = index + 1
        diagnostics.append(Diagnostic(*record, occurrence_index=index))
    diagnostics.sort(key=sort_key)
    return diagnostics


def external_cache_directory(root: Path) -> Path:
    """A unique cache location under the OS temporary root, never inside the tree.

    mypy writes roughly sixteen megabytes of cache -- `cache.db`, `CACHEDIR.TAG`,
    `missing_stubs` -- into `.mypy_cache` beside whatever it is checking, and
    `--no-incremental` does not suppress that. Verifying a repository is a read-only
    act, so the write is redirected rather than tidied up afterwards.
    """
    cache = Path(tempfile.mkdtemp(prefix="xrml-mypy-cache-")).resolve()
    resolved = root.resolve()
    if cache == resolved or cache.is_relative_to(resolved):
        shutil.rmtree(cache, ignore_errors=True)
        raise BaselineError("temporary cache directory resolved inside the repository")
    return cache


def discard_cache(cache: Path) -> None:
    """Strict removal: a cache that outlives the run is the defect, not a nuisance."""
    try:
        shutil.rmtree(cache)
    except OSError as exc:
        # Named by type only. The absolute path is deliberately absent from every
        # message this program emits.
        raise BaselineError(f"mypy cache cleanup failed ({type(exc).__name__})") from exc


def operational_argv(cache: Path) -> list[str]:
    """The argv actually executed, which is not the argv that is under contract.

    `COMMAND_ARGUMENTS` is the diagnostic-semantic contract: it is recorded in the
    baseline and hashed into `config_digest`, so it must stay byte- and order-identical.
    `--cache-dir` selects only where bytes land, provably changing no diagnostic, so it
    is injected here at execution time and never enters that contract.

    Fails closed if the ordered scope is no longer the exact suffix of the contract --
    without that, options and operands cannot be told apart and the flag could land
    after an operand.
    """
    width = len(SCOPE)
    if COMMAND_ARGUMENTS[-width:] != SCOPE:
        raise BaselineError("mypy argument contract no longer ends with the ordered scope")
    options = list(COMMAND_ARGUMENTS[:-width])
    return [*options, "--cache-dir", str(cache), *SCOPE]


def run_mypy(root: Path) -> tuple[int, str]:
    cache = external_cache_directory(root)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "mypy", *operational_argv(cache)],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
        )
    except OSError as exc:
        raise BaselineError(f"mypy could not be executed ({type(exc).__name__})") from exc
    finally:
        # Runs on every path: ordinary debt, clean tree, fatal exit, or a raised
        # subprocess error. A cleanup failure here replaces the in-flight error, which
        # is acceptable because both are BaselineError and both exit nonzero.
        discard_cache(cache)
    if completed.returncode not in (0, 1):
        raise BaselineError(
            f"mypy exited {completed.returncode}: {completed.stderr.strip()[:200]}"
        )
    if completed.stderr.strip():
        raise BaselineError(f"mypy wrote to stderr: {completed.stderr.strip()[:200]}")
    return completed.returncode, completed.stdout


def collect(root: Path) -> list[Diagnostic]:
    exit_code, output = run_mypy(root)
    diagnostics = parse_output(output)
    if exit_code == 1 and not diagnostics:
        # A non-zero exit with nothing parsed is a crash wearing a clean run's clothes.
        raise BaselineError("mypy exited 1 but produced no parsable diagnostic")
    if exit_code == 0 and diagnostics:
        raise BaselineError("mypy exited 0 but produced diagnostics")
    return diagnostics


def build_document(root: Path, diagnostics: list[Diagnostic]) -> dict[str, Any]:
    issues = [item.as_dict() for item in diagnostics]
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "classification": CLASSIFICATION,
        "mypy_version": REQUIRED_MYPY_VERSION,
        "execution_python": REQUIRED_EXECUTION_PYTHON,
        "target_python": REQUIRED_TARGET_PYTHON,
        "scope": list(SCOPE),
        "command_arguments": list(COMMAND_ARGUMENTS),
        "config_digest": compute_config_digest(root),
        "diagnostic_count": len(issues),
        "error_count": sum(1 for item in diagnostics if item.severity == "error"),
        "note_count": sum(1 for item in diagnostics if item.severity == "note"),
        "affected_file_count": len({item.path for item in diagnostics}),
        "issues_sha256": sha256_text(canonical_json(issues)),
        "issues": issues,
    }


def serialize(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def load_baseline(root: Path) -> dict[str, Any]:
    path = root / BASELINE_RELATIVE
    if not path.exists():
        raise BaselineError(f"baseline is missing: {BASELINE_RELATIVE}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineError(f"baseline is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise BaselineError("baseline root is not an object")
    required = ("schema", "version", "classification", "mypy_version", "execution_python",
                "target_python", "scope", "command_arguments", "config_digest",
                "diagnostic_count", "error_count", "note_count", "affected_file_count",
                "issues_sha256", "issues")
    missing = [field for field in required if field not in document]
    if missing:
        raise BaselineError(f"baseline is missing fields: {', '.join(missing)}")
    if document["schema"] != SCHEMA or document["version"] != SCHEMA_VERSION:
        raise BaselineError("baseline schema or version is not recognized")
    issues = document["issues"]
    if not isinstance(issues, list):
        raise BaselineError("baseline issues is not an array")
    # Counts are a summary, never the comparison; they still have to reconcile, because
    # a document whose own counts disagree with its own issues is not trustworthy.
    if document["diagnostic_count"] != len(issues):
        raise BaselineError("baseline diagnostic_count does not reconcile with issues")
    errors = sum(1 for item in issues if item.get("severity") == "error")
    notes = sum(1 for item in issues if item.get("severity") == "note")
    if document["error_count"] != errors or document["note_count"] != notes:
        raise BaselineError("baseline error/note counts do not reconcile with issues")
    if document["affected_file_count"] != len({item.get("path") for item in issues}):
        raise BaselineError("baseline affected_file_count does not reconcile with issues")
    for item in issues:
        if item.get("message_sha256") != sha256_text(str(item.get("message", ""))):
            raise BaselineError("baseline message_sha256 does not reconcile")
    if document["issues_sha256"] != sha256_text(canonical_json(issues)):
        raise BaselineError("baseline issues_sha256 does not reconcile with issues")
    return document


def identity_of(item: dict[str, Any]) -> tuple[object, ...]:
    return (item.get("path"), item.get("line"), item.get("column"), item.get("severity"),
            item.get("code"), item.get("message"), item.get("occurrence_index"))


def compare(root: Path, baseline: dict[str, Any],
            diagnostics: list[Diagnostic]) -> dict[str, list[str]]:
    """Every drift class the verifier recognizes, as deterministic bounded samples."""
    delta: dict[str, list[str]] = {"added": [], "removed": [], "changed_identity": [],
                                   "contract_drift": [], "baseline_invalid": [],
                                   "execution_failure": []}
    for label, expected, actual in (
        ("mypy_version", baseline["mypy_version"], REQUIRED_MYPY_VERSION),
        ("execution_python", baseline["execution_python"], REQUIRED_EXECUTION_PYTHON),
        ("target_python", baseline["target_python"], REQUIRED_TARGET_PYTHON),
        ("scope", baseline["scope"], list(SCOPE)),
        ("command_arguments", baseline["command_arguments"], list(COMMAND_ARGUMENTS)),
        ("config_digest", baseline["config_digest"], compute_config_digest(root)),
        ("classification", baseline["classification"], CLASSIFICATION),
    ):
        if expected != actual:
            delta["contract_drift"].append(f"{label}: baseline != current")

    current = build_document(root, diagnostics)
    if current["issues_sha256"] != baseline["issues_sha256"]:
        # Reported unconditionally, before the set difference is even consulted. A
        # corrupted digest over an otherwise-matching issue array produces two empty
        # difference sets, and reporting only those would let the mismatch pass.
        delta["contract_drift"].append("issues_sha256: baseline != current")
        recorded = {identity_of(item) for item in baseline["issues"]}
        observed = {identity_of(item) for item in current["issues"]}
        by_message_removed = {str(item.get("message")) for item in baseline["issues"]
                              if identity_of(item) not in observed}
        for identity in sorted(observed - recorded, key=repr)[:10]:
            bucket = "changed_identity" if str(identity[5]) in by_message_removed else "added"
            delta[bucket].append(f"{identity[0]}:{identity[1]}:{identity[2]} {identity[3]}")
        for identity in sorted(recorded - observed, key=repr)[:10]:
            if str(identity[5]) not in by_message_removed:
                continue
            delta["removed"].append(f"{identity[0]}:{identity[1]}:{identity[2]} {identity[3]}")
    for label in ("diagnostic_count", "error_count", "note_count", "affected_file_count"):
        if baseline[label] != current[label]:
            delta["contract_drift"].append(
                f"{label}: baseline {baseline[label]} != current {current[label]}"
            )
    return delta


def write_baseline(root: Path, document: dict[str, Any]) -> None:
    """Atomic replacement, and the only write this program ever performs."""
    target = root / BASELINE_RELATIVE
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=target.parent,
        prefix=".mypy_baseline.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(serialize(document))
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def report_success(document: dict[str, Any]) -> None:
    print(f"  classification      {document['classification']}")
    print(f"  errors              {document['error_count']}")
    print(f"  notes               {document['note_count']}")
    print(f"  diagnostics         {document['diagnostic_count']}")
    print(f"  affected files      {document['affected_file_count']}")
    print(f"  issues sha256       {document['issues_sha256'][:32]}")
    print(f"  config digest       {document['config_digest'][:32]}")
    print("  baseline identity matched")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="read-only comparison against the committed baseline (default)")
    mode.add_argument("--write-baseline", action="store_true",
                      help="explicitly regenerate tools/mypy_baseline.json")
    args = parser.parse_args(argv)

    try:
        root = resolve_root()
        verify_environment()
        diagnostics = collect(root)
        document = build_document(root, diagnostics)
        if args.write_baseline:
            write_baseline(root, document)
            print(f"  wrote {BASELINE_RELATIVE}")
            report_success(document)
            return 0
        baseline = load_baseline(root)
        delta = compare(root, baseline, diagnostics)
    except BaselineError as exc:
        category = "execution_failure" if "mypy" in str(exc) else "baseline_invalid"
        print(f"  {category}: {exc}")
        return 1

    if any(delta.values()):
        print("  mypy baseline mismatch")
        for category, entries in delta.items():
            if entries:
                print(f"    {category:18} {len(entries)}")
                for entry in entries[:5]:
                    print(f"      {entry}")
        print("  update the baseline intentionally with --write-baseline")
        return 1
    report_success(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

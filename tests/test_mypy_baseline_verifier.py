# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the fail-closed mypy debt baseline.

The verifier's only value is that it refuses to pass when anything moved, so nearly
every test here is a negative: mutate one field, assert the check fails. A verifier
that is merely *usually* right is worse than none, because it launders drift as
approval.

Running the real five-path mypy scope takes tens of seconds, so it happens a handful
of times at most. Everything else drives the pure functions -- parser, comparator,
digest -- directly, and every mutation is applied to an in-memory or `tmp_path` copy.
No test writes to the real worktree.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools import check_mypy_baseline as verifier

REPO = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO / "tools" / "mypy_baseline.json"
BASELINE_TEXT = BASELINE_PATH.read_text(encoding="utf-8")
BASELINE: dict[str, Any] = json.loads(BASELINE_TEXT)

ERROR_LINE = "app/main.py:12:5: error: Incompatible return value type  [return-value]"
NOTE_LINE = "app/main.py:12:5: note: Left operand is of type \"int\""


def mutated(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(BASELINE)
    document.update(changes)
    return document


def current_diagnostics() -> list[verifier.Diagnostic]:
    """Rebuild the exact diagnostic objects from the committed baseline.

    Equivalent to a fresh mypy run for comparator purposes, and it costs nothing --
    the baseline is proven to reconcile with itself by `load_baseline`.
    """
    return [
        verifier.Diagnostic(
            path=item["path"], line=item["line"], column=item["column"],
            severity=item["severity"], code=item["code"], message=item["message"],
            occurrence_index=item["occurrence_index"],
        )
        for item in BASELINE["issues"]
    ]


def scratch_root(tmp_path: Path) -> Path:
    """A no-Git source copy carrying only what the verifier needs."""
    root = tmp_path / "src"
    (root / "tools").mkdir(parents=True)
    (root / "app").mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(REPO / name, root / name)
    shutil.copy2(REPO / "tools" / "check_mypy_baseline.py", root / "tools")
    shutil.copy2(BASELINE_PATH, root / "tools")
    return root


# --------------------------------------------------------------------------- A. root
class TestRootAndEnvironment:
    def test_root_resolves_without_git(self, tmp_path: Path) -> None:
        root = scratch_root(tmp_path)
        assert not (root / ".git").exists()
        assert verifier.resolve_root(root / "tools") == root

    @pytest.mark.parametrize("missing", verifier.ROOT_MARKERS)
    def test_missing_root_marker_fails(self, tmp_path: Path, missing: str) -> None:
        root = scratch_root(tmp_path)
        target = root / missing
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        with pytest.raises(verifier.BaselineError):
            verifier.resolve_root(root / "tools")

    def test_nearest_root_wins_when_markers_nest(self, tmp_path: Path) -> None:
        """An outer marker set must never capture an inner checkout."""
        outer = scratch_root(tmp_path)
        inner = scratch_root(outer / "app")
        assert verifier.resolve_root(inner / "tools") == inner

    def test_a_root_without_this_verifier_is_rejected(self, tmp_path: Path) -> None:
        root = scratch_root(tmp_path)
        (root / "tools" / "check_mypy_baseline.py").unlink()
        with pytest.raises(verifier.BaselineError):
            verifier.resolve_root(root / "tools")

    def test_the_shell_cwd_is_never_the_authority(self) -> None:
        assert "cwd" not in verifier.resolve_root.__code__.co_names
        assert verifier.resolve_root() == REPO

    def test_environment_provenance_holds_here(self) -> None:
        verifier.verify_environment()

    def test_required_identities_are_exact_not_prefixes(self) -> None:
        assert verifier.REQUIRED_MYPY_VERSION == "1.20.2"
        assert verifier.REQUIRED_EXECUTION_PYTHON == "3.12.12"
        assert verifier.REQUIRED_TARGET_PYTHON == "3.11"

    def test_python_drift_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verifier, "REQUIRED_EXECUTION_PYTHON", "3.13.0")
        with pytest.raises(verifier.BaselineError, match="execution Python"):
            verifier.verify_environment()

    def test_mypy_version_drift_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verifier, "REQUIRED_MYPY_VERSION", "1.19.0")
        with pytest.raises(verifier.BaselineError, match="mypy"):
            verifier.verify_environment()

    def test_a_global_mypy_cannot_satisfy_provenance(self) -> None:
        """The prefix check is what stops an unrelated installation counting."""
        source = (REPO / "tools" / "check_mypy_baseline.py").read_text(encoding="utf-8")
        assert "is_relative_to(prefix)" in source
        assert "Path(sys.prefix).resolve()" in source


# ------------------------------------------------------------------------- B. parser
class TestParser:
    def test_a_valid_error_parses(self) -> None:
        (item,) = verifier.parse_output(ERROR_LINE)
        assert (item.path, item.line, item.column) == ("app/main.py", 12, 5)
        assert (item.severity, item.code) == ("error", "return-value")
        assert item.message == "Incompatible return value type"
        assert item.occurrence_index == 0

    def test_a_valid_note_without_a_code_parses(self) -> None:
        (item,) = verifier.parse_output(NOTE_LINE)
        assert item.severity == "note" and item.code is None

    def test_backslash_paths_normalize_to_posix(self) -> None:
        (item,) = verifier.parse_output(ERROR_LINE.replace("app/main.py", "app\\main.py"))
        assert item.path == "app/main.py"

    def test_a_message_is_never_re_scanned_for_a_second_severity(self) -> None:
        line = "app/a.py:1:1: error: got error: nested  [misc]"
        (item,) = verifier.parse_output(line)
        assert item.message == "got error: nested" and item.code == "misc"

    def test_a_bracketed_tail_inside_a_message_is_not_forged_into_a_code(self) -> None:
        (item,) = verifier.parse_output("app/a.py:1:1: note: variants  [x]")
        assert item.code == "x" and item.message == "variants"
        (plain,) = verifier.parse_output("app/a.py:1:1: note: list[int] is fine")
        assert plain.code is None and plain.message == "list[int] is fine"

    @pytest.mark.parametrize("line", [
        "app/main.py:12:5: error: no code here",
        "app/main.py:x:5: error: bad line  [misc]",
        "app/main.py:12:y: error: bad column  [misc]",
        "app/main.py:0:5: error: zero line  [misc]",
        "app/main.py:12:0: error: zero column  [misc]",
        "C:\\repo\\app\\main.py:12:5: error: absolute windows  [misc]",
        "\\\\server\\share\\app.py:12:5: error: unc  [misc]",
        "/home/user/app/main.py:12:5: error: absolute posix  [misc]",
        "app/../../etc/passwd:1:1: error: traversal  [misc]",
        "scripts/tool.py:1:1: error: out of scope  [misc]",
        "tests/test_x.py:1:1: error: out of scope  [misc]",
        "app/main.py:12:5: warning: unknown severity  [misc]",
        "app/main.py:12:5: error: ansi \x1b[31mred\x1b[0m  [misc]",
        "app/main.py:12:5: error: nul \x00 byte  [misc]",
        'Traceback (most recent call last):',
        "app/main.py:12:5: error: cannot find module  [import-not-found]",
        "app/main.py:12:5: error: missing stubs  [import-untyped]",
        "Found 594 errors in 71 files (checked 5 source files)",
        "app/main.py: error: Duplicate module named 'main'",
    ])
    def test_rejected_output_shapes(self, line: str) -> None:
        with pytest.raises(verifier.BaselineError):
            verifier.parse_output(line)

    def test_blank_lines_are_ignored_but_whitespace_lines_are_not(self) -> None:
        assert verifier.parse_output("\n\n") == []
        with pytest.raises(verifier.BaselineError):
            verifier.parse_output("   ")

    def test_crlf_is_normalized_rather_than_absorbed(self) -> None:
        (item,) = verifier.parse_output(ERROR_LINE + "\r\n")
        assert "\r" not in item.message

    @pytest.mark.parametrize("code", [2, 3, 127])
    def test_unexpected_process_exit_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], code, "", "boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(verifier.BaselineError, match="exited"):
            verifier.run_mypy(REPO)

    def test_exit_one_with_nothing_parsed_is_a_crash_not_a_clean_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verifier, "run_mypy", lambda _root: (1, ""))
        with pytest.raises(verifier.BaselineError, match="no parsable diagnostic"):
            verifier.collect(REPO)

    def test_exit_zero_with_diagnostics_is_contradictory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verifier, "run_mypy", lambda _root: (0, ERROR_LINE))
        with pytest.raises(verifier.BaselineError, match="exited 0"):
            verifier.collect(REPO)

    def test_stderr_output_is_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1, ERROR_LINE, "internal error")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(verifier.BaselineError, match="stderr"):
            verifier.run_mypy(REPO)

    def test_every_parsed_diagnostic_is_retained(self) -> None:
        text = "\n".join([ERROR_LINE, NOTE_LINE, ERROR_LINE])
        assert len(verifier.parse_output(text)) == 3


# --------------------------------------------------------------- C. baseline structure
class TestBaselineStructure:
    def test_schema_version_and_classification(self) -> None:
        assert BASELINE["schema"] == "xrml_mypy_baseline"
        assert BASELINE["version"] == "1.0.0"
        assert BASELINE["classification"] == "mypy_debt_baselined"

    def test_the_expected_counts_are_exact(self) -> None:
        assert BASELINE["error_count"] == 559
        assert BASELINE["note_count"] == 20
        assert BASELINE["diagnostic_count"] == 579
        assert BASELINE["affected_file_count"] == 69

    def test_counts_reconcile_with_the_issue_array(self) -> None:
        issues = BASELINE["issues"]
        assert len(issues) == BASELINE["diagnostic_count"]
        assert sum(1 for i in issues if i["severity"] == "error") == BASELINE["error_count"]
        assert sum(1 for i in issues if i["severity"] == "note") == BASELINE["note_count"]
        assert len({i["path"] for i in issues}) == BASELINE["affected_file_count"]

    def test_message_hashes_reconcile(self) -> None:
        for item in BASELINE["issues"]:
            digest = hashlib.sha256(item["message"].encode("utf-8")).hexdigest()
            assert item["message_sha256"] == digest

    def test_issues_sha256_reconciles(self) -> None:
        payload = verifier.canonical_json(BASELINE["issues"])
        assert BASELINE["issues_sha256"] == hashlib.sha256(payload.encode()).hexdigest()

    def test_issues_are_deterministically_sorted(self) -> None:
        issues = BASELINE["issues"]
        rebuilt = sorted(current_diagnostics(), key=verifier.sort_key)
        assert [i["path"] for i in issues] == [d.path for d in rebuilt]
        assert issues == sorted(issues, key=lambda i: (
            i["path"], i["line"], (i["column"] is None, i["column"] or 0), i["severity"],
            (i["code"] is None, i["code"] or ""), i["message"], i["occurrence_index"],
        ))

    def test_duplicate_occurrence_indexing_is_zero_based_and_dense(self) -> None:
        duplicates = verifier.parse_output("\n".join([ERROR_LINE] * 3))
        assert [d.occurrence_index for d in duplicates] == [0, 1, 2]
        assert min(i["occurrence_index"] for i in BASELINE["issues"]) == 0

    def test_duplicate_indexing_is_independent_of_emission_order(self) -> None:
        other = "app/main.py:99:1: error: other  [misc]"
        forward = verifier.parse_output("\n".join([ERROR_LINE, other, ERROR_LINE]))
        reverse = verifier.parse_output("\n".join([other, ERROR_LINE, ERROR_LINE]))
        assert [d.identity() for d in forward] == [d.identity() for d in reverse]

    @pytest.mark.parametrize("pattern", [
        r"[A-Za-z]:[\\/]", r"AppData", r"\d{4}-\d{2}-\d{2}T", r"\bhostname\b",
        r"generated_at", r"timestamp", r"/home/", r"/Users/",
    ])
    def test_no_machine_specific_metadata(self, pattern: str) -> None:
        assert not re.search(pattern, BASELINE_TEXT)

    def test_every_issue_path_is_repository_relative_and_in_scope(self) -> None:
        for item in BASELINE["issues"]:
            assert not item["path"].startswith("/") and ":" not in item["path"]
            assert verifier.in_scope(item["path"])

    def test_the_committed_bytes_are_exactly_what_the_serializer_emits(self) -> None:
        """Byte-determinism, pinned against the tracked file rather than re-run."""
        assert verifier.serialize(BASELINE) == BASELINE_TEXT

    def test_file_encoding_contract(self) -> None:
        raw = BASELINE_PATH.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")

    def test_the_required_top_level_fields_are_all_present(self) -> None:
        assert set(BASELINE) == {
            "schema", "version", "classification", "mypy_version", "execution_python",
            "target_python", "scope", "command_arguments", "config_digest",
            "diagnostic_count", "error_count", "note_count", "affected_file_count",
            "issues_sha256", "issues",
        }

    def test_every_issue_carries_the_required_fields(self) -> None:
        for item in BASELINE["issues"]:
            assert set(item) == {"path", "line", "column", "severity", "code", "message",
                                 "message_sha256", "occurrence_index"}


# ------------------------------------------------------------------ D. contract digest
class TestContractDigest:
    def test_the_current_digest_matches_the_baseline(self) -> None:
        assert verifier.compute_config_digest(REPO) == BASELINE["config_digest"]

    def digest_after(self, tmp_path: Path, transform: str) -> str:
        root = scratch_root(tmp_path)
        target = root / "pyproject.toml"
        target.write_text(transform, encoding="utf-8")
        return verifier.compute_config_digest(root)

    def test_unrelated_project_metadata_does_not_change_it(self, tmp_path: Path) -> None:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        changed = (text.replace('name = "xrml-research-agent"', 'name = "renamed-thing"')
                       .replace('version = "0.1.0"', 'version = "9.9.9"'))
        assert changed != text
        assert self.digest_after(tmp_path, changed) == BASELINE["config_digest"]

    @pytest.mark.parametrize("old,new", [
        ("ignore_missing_imports = false", "ignore_missing_imports = true"),
        ('python_version = "3.11"', 'python_version = "3.12"'),
        ('    "reportlab.platypus",\n', ""),
        ('    "reportlab",\n', '    "reportlab",\n    "numpy",\n'),
        ('    "reportlab",\n', '    "reportlab",\n    "app.*",\n'),
        ('    "reportlab",\n', '    "reportlab",\n    "tools.helpers",\n'),
    ])
    def test_mypy_contract_changes_move_the_digest(
        self, tmp_path: Path, old: str, new: str
    ) -> None:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert old in text
        assert self.digest_after(tmp_path, text.replace(old, new, 1)) != BASELINE["config_digest"]

    @pytest.mark.parametrize("attribute,value", [
        ("SCOPE", ("tools/public_path_scan.py", "app", "tools/git_object_source.py",
                   "tools/package_source_distribution.py", "tools/public_content_policy.py")),
        ("SCOPE", ("app",)),
        ("REQUIRED_TARGET_PYTHON", "3.12"),
        ("COMMAND_ARGUMENTS", ("--config-file", "pyproject.toml", "--no-incremental")),
        ("COMMAND_ARGUMENTS", ("--config-file", "pyproject.toml", "--strict")),
    ])
    def test_scope_target_and_argument_drift_move_the_digest(
        self, monkeypatch: pytest.MonkeyPatch, attribute: str, value: object
    ) -> None:
        monkeypatch.setattr(verifier, attribute, value)
        assert verifier.compute_config_digest(REPO) != BASELINE["config_digest"]

    def test_reordering_the_scope_alone_moves_the_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order is contract: mypy's output order follows it."""
        monkeypatch.setattr(verifier, "SCOPE", tuple(reversed(verifier.SCOPE)))
        assert verifier.compute_config_digest(REPO) != BASELINE["config_digest"]

    def test_dropping_explicit_package_bases_moves_the_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stripped = tuple(a for a in verifier.COMMAND_ARGUMENTS
                         if a != "--explicit-package-bases")
        monkeypatch.setattr(verifier, "COMMAND_ARGUMENTS", stripped)
        assert verifier.compute_config_digest(REPO) != BASELINE["config_digest"]

    def test_ruff_settings_are_not_part_of_the_mypy_digest(self, tmp_path: Path) -> None:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        changed = text.replace('line-length = 120', 'line-length = 100')
        assert changed != text
        assert self.digest_after(tmp_path, changed) == BASELINE["config_digest"]


# ------------------------------------------------------------------- E. the comparator
class TestComparator:
    def test_the_exact_current_result_passes(self) -> None:
        delta = verifier.compare(REPO, BASELINE, current_diagnostics())
        assert not any(delta.values()), delta

    def test_an_added_diagnostic_fails(self) -> None:
        extra = verifier.Diagnostic("app/main.py", 1, 1, "error", "misc", "brand new", 0)
        delta = verifier.compare(REPO, BASELINE, [*current_diagnostics(), extra])
        assert delta["added"] and delta["contract_drift"]

    def test_a_removed_diagnostic_fails(self) -> None:
        delta = verifier.compare(REPO, BASELINE, current_diagnostics()[1:])
        assert delta["removed"] or delta["contract_drift"]

    @pytest.mark.parametrize("field,value", [
        ("line", 9999), ("column", 4242), ("severity", "note"),
        ("code", "totally-different"), ("message", "rewritten message"),
    ])
    def test_a_changed_identity_fails(self, field: str, value: object) -> None:
        diagnostics = current_diagnostics()
        first = diagnostics[0]
        diagnostics[0] = verifier.Diagnostic(**{
            **{k: getattr(first, k) for k in
               ("path", "line", "column", "severity", "code", "message", "occurrence_index")},
            field: value,
        })
        delta = verifier.compare(REPO, BASELINE, diagnostics)
        assert any(delta[k] for k in ("added", "removed", "changed_identity"))

    def test_a_one_line_move_is_not_tolerated(self) -> None:
        diagnostics = current_diagnostics()
        first = diagnostics[0]
        diagnostics[0] = verifier.Diagnostic(
            first.path, first.line + 1, first.column, first.severity, first.code,
            first.message, first.occurrence_index)
        delta = verifier.compare(REPO, BASELINE, diagnostics)
        assert any(delta[k] for k in ("added", "removed", "changed_identity"))

    def test_a_changed_duplicate_count_fails(self) -> None:
        diagnostics = current_diagnostics()
        first = diagnostics[0]
        diagnostics.append(verifier.Diagnostic(
            first.path, first.line, first.column, first.severity, first.code,
            first.message, first.occurrence_index + 1))
        delta = verifier.compare(REPO, BASELINE, diagnostics)
        assert any(delta.values())

    @pytest.mark.parametrize("field,value", [
        ("mypy_version", "1.19.0"),
        ("execution_python", "3.11.9"),
        ("target_python", "3.12"),
        ("classification", "mypy_clean"),
        ("config_digest", "0" * 64),
        ("scope", ["app"]),
        ("command_arguments", ["--config-file", "pyproject.toml"]),
    ])
    def test_contract_drift_in_the_baseline_fails(self, field: str, value: object) -> None:
        delta = verifier.compare(REPO, mutated(**{field: value}), current_diagnostics())
        assert delta["contract_drift"]

    @pytest.mark.parametrize("field", [
        "diagnostic_count", "error_count", "note_count", "affected_file_count",
    ])
    def test_a_count_mismatch_fails(self, field: str) -> None:
        delta = verifier.compare(REPO, mutated(**{field: 1}), current_diagnostics())
        assert delta["contract_drift"]

    def test_an_issues_digest_mismatch_alone_fails(self) -> None:
        delta = verifier.compare(REPO, mutated(issues_sha256="f" * 64), current_diagnostics())
        assert any(delta.values())


class TestBaselineLoading:
    def write(self, tmp_path: Path, document: object) -> Path:
        root = tmp_path / "r"
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "mypy_baseline.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return root

    def test_the_real_baseline_loads(self) -> None:
        assert verifier.load_baseline(REPO)["issues_sha256"] == BASELINE["issues_sha256"]

    def test_a_missing_baseline_fails(self, tmp_path: Path) -> None:
        (tmp_path / "tools").mkdir()
        with pytest.raises(verifier.BaselineError, match="missing"):
            verifier.load_baseline(tmp_path)

    @pytest.mark.parametrize("document,match", [
        ([], "not an object"),
        ({"schema": "xrml_mypy_baseline"}, "missing fields"),
    ])
    def test_a_malformed_baseline_fails(
        self, tmp_path: Path, document: object, match: str
    ) -> None:
        with pytest.raises(verifier.BaselineError, match=match):
            verifier.load_baseline(self.write(tmp_path, document))

    def test_invalid_json_fails(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "mypy_baseline.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(verifier.BaselineError, match="valid JSON"):
            verifier.load_baseline(root)

    @pytest.mark.parametrize("field", ["schema", "version"])
    def test_an_unrecognized_schema_fails(self, tmp_path: Path, field: str) -> None:
        with pytest.raises(verifier.BaselineError, match="schema or version"):
            verifier.load_baseline(self.write(tmp_path, mutated(**{field: "wrong"})))

    @pytest.mark.parametrize("field,message", [
        ("diagnostic_count", "diagnostic_count"),
        ("error_count", "error/note"),
        ("note_count", "error/note"),
        ("affected_file_count", "affected_file_count"),
    ])
    def test_self_inconsistent_counts_fail(
        self, tmp_path: Path, field: str, message: str
    ) -> None:
        with pytest.raises(verifier.BaselineError, match=message):
            verifier.load_baseline(self.write(tmp_path, mutated(**{field: 7})))

    def test_a_stale_issue_breaks_the_issues_digest(self, tmp_path: Path) -> None:
        document = copy.deepcopy(BASELINE)
        document["issues"] = document["issues"][:-1]
        document["diagnostic_count"] -= 1
        document["error_count"] = sum(
            1 for i in document["issues"] if i["severity"] == "error")
        document["note_count"] = sum(1 for i in document["issues"] if i["severity"] == "note")
        document["affected_file_count"] = len({i["path"] for i in document["issues"]})
        with pytest.raises(verifier.BaselineError, match="issues_sha256"):
            verifier.load_baseline(self.write(tmp_path, document))

    def test_a_tampered_message_breaks_its_own_hash(self, tmp_path: Path) -> None:
        document = copy.deepcopy(BASELINE)
        document["issues"][0]["message"] = "quietly rewritten"
        with pytest.raises(verifier.BaselineError, match="message_sha256"):
            verifier.load_baseline(self.write(tmp_path, document))


# ------------------------------------------------------------------------- F. the modes
class TestModes:
    SOURCE = (REPO / "tools" / "check_mypy_baseline.py").read_text(encoding="utf-8")

    def test_default_mode_is_check(self) -> None:
        parser_args = verifier.main.__doc__
        assert parser_args is None or "check" in parser_args.lower()
        assert "--check" in self.SOURCE and "args.write_baseline" in self.SOURCE

    def test_check_and_write_are_mutually_exclusive(self) -> None:
        assert "add_mutually_exclusive_group" in self.SOURCE

    def test_write_mode_is_the_only_writer(self) -> None:
        assert self.SOURCE.count("write_baseline(root, document)") == 1
        body = self.SOURCE.split("def main(")[1]
        assert body.index("args.write_baseline") < body.index("write_baseline(root")

    def test_check_mode_leaves_the_baseline_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verifier, "collect", lambda _root: current_diagnostics())

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("check mode attempted a write")

        monkeypatch.setattr(verifier, "write_baseline", refuse)
        before = BASELINE_PATH.read_bytes()
        assert verifier.main(["--check"]) == 0
        assert BASELINE_PATH.read_bytes() == before

    def test_write_mode_targets_only_the_baseline_path(self) -> None:
        assert verifier.BASELINE_RELATIVE == "tools/mypy_baseline.json"
        write_body = self.SOURCE.split("def write_baseline(")[1].split("\ndef ")[0]
        assert "os.replace" in write_body
        assert "BASELINE_RELATIVE" in write_body
        assert write_body.count("os.replace") == 1

    def test_write_mode_uses_atomic_replacement_in_the_target_directory(self) -> None:
        write_body = self.SOURCE.split("def write_baseline(")[1].split("\ndef ")[0]
        assert "dir=target.parent" in write_body
        assert "unlink(missing_ok=True)" in write_body

    def test_write_mode_writes_only_after_collection_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fatal or import diagnostic must abort before any file is touched."""
        root = scratch_root(tmp_path)
        target = root / "tools" / "mypy_baseline.json"
        before = target.read_bytes()
        monkeypatch.setattr(verifier, "resolve_root", lambda *_a, **_k: root)
        monkeypatch.setattr(verifier, "verify_environment", lambda: None)
        monkeypatch.setattr(
            verifier, "run_mypy",
            lambda _root: (1, "app/a.py:1:1: error: nope  [import-not-found]"))
        assert verifier.main(["--write-baseline"]) == 1
        assert target.read_bytes() == before
        assert not list(target.parent.glob(".mypy_baseline.*"))

    def test_there_is_no_automatic_fallback_from_check_to_write(self) -> None:
        after_load = self.SOURCE.split("baseline = load_baseline(root)")[1]
        assert "write_baseline(" not in after_load

    def test_a_mismatch_returns_nonzero_without_rewriting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = scratch_root(tmp_path)
        target = root / "tools" / "mypy_baseline.json"
        before = target.read_bytes()
        monkeypatch.setattr(verifier, "resolve_root", lambda *_a, **_k: root)
        monkeypatch.setattr(verifier, "verify_environment", lambda: None)
        monkeypatch.setattr(verifier, "collect", lambda _root: current_diagnostics()[2:])
        assert verifier.main(["--check"]) == 1
        assert target.read_bytes() == before

    def test_the_delta_categories_are_exactly_the_agreed_six(self) -> None:
        delta = verifier.compare(REPO, BASELINE, current_diagnostics())
        assert set(delta) == {"added", "removed", "changed_identity", "contract_drift",
                              "baseline_invalid", "execution_failure"}

    def test_success_output_names_the_classification_and_counts(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verifier, "collect", lambda _root: current_diagnostics())
        assert verifier.main([]) == 0
        out = capsys.readouterr().out
        lines = [line.strip() for line in out.splitlines()]
        assert lines == [
            "classification      mypy_debt_baselined",
            f"errors              {BASELINE['error_count']}",
            f"notes               {BASELINE['note_count']}",
            f"diagnostics         {BASELINE['diagnostic_count']}",
            f"affected files      {BASELINE['affected_file_count']}",
            f"issues sha256       {BASELINE['issues_sha256'][:32]}",
            f"config digest       {BASELINE['config_digest'][:32]}",
            "baseline identity matched",
        ]

    def test_failure_output_is_bounded_and_leaks_no_local_paths(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = [verifier.Diagnostic("app/a.py", n, 1, "error", "misc", f"new {n}", 0)
                 for n in range(1, 40)]
        monkeypatch.setattr(verifier, "collect",
                            lambda _root: [*current_diagnostics(), *extra])
        assert verifier.main(["--check"]) == 1
        out = capsys.readouterr().out
        assert len(out.splitlines()) < 40
        assert not re.search(r"[A-Za-z]:[\\/]|/tmp/|AppData", out)


# --------------------------------------------------------- G. current repository state
class TestCurrentRepositoryContract:
    def test_the_verifier_check_passes_on_the_real_source(self) -> None:
        assert verifier.main(["--check"]) == 0

    def test_the_verifier_is_mypy_clean_and_outside_the_debt_scope(self) -> None:
        assert not verifier.in_scope("tools/check_mypy_baseline.py")
        assert all(i["path"] != "tools/check_mypy_baseline.py" for i in BASELINE["issues"])
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", "pyproject.toml",
             "--explicit-package-bases", "--no-incremental", "--show-error-codes",
             "--show-column-numbers", "--no-pretty", "--no-color-output",
             "--no-error-summary", "tools/check_mypy_baseline.py"],
            cwd=REPO, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout[-500:]
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize("relative,digest", [
        # pyproject.toml and uv.lock were pinned here while this lane's scope forbade manifest
        # changes. That scope has passed: the build-backend authority lane is explicitly
        # authorized to pin [build-system].requires and add [tool.uv].build-constraint-
        # dependencies, and one `uv lock` then records those constraints in uv.lock's manifest
        # table. A byte pin would forbid an authorized change; swapping in a fresh digest would
        # rebuild the same cross-lane overconstraint. Both are asserted by property below and
        # in tests/test_project_build_backend_authority.py instead.
        # The later public-snapshot work authorizes packager changes. Its committed
        # blob/mode, dirty-policy and exclusion boundaries are tested by behaviour
        # in test_source_distribution_packaging.py and test_git_object_packaging.py;
        # this earlier baseline-only lane must not freeze that product file forever.
        ("tools/public_path_scan.py", "0ff9a0cd21521acb"),
        ("tests/test_mypy_dual_import_contract.py", "cdec62849f5ae5fc"),
        ("tests/test_dev_tool_lock_and_ruff_contract.py", "85e3d71beae3b643"),
        # CONTRIBUTING.md was pinned here while this lane's scope forbade documentation
        # changes. That scope has passed: the contributor contract is now an authorized,
        # evolving document, and its content is asserted by property in
        # tests/test_static_toolchain_ci_contract.py. A byte pin here would forbid every
        # future authorized documentation change, which is not an invariant worth keeping.
    ])
    def test_no_pre_existing_file_was_modified(self, relative: str, digest: str) -> None:
        actual = hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
        assert actual.startswith(digest), f"{relative} changed in a baseline-only lane"

    def test_the_ruff_and_mypy_contracts_are_untouched(self) -> None:
        import tomllib

        document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        assert len(document["tool"]["ruff"]["lint"]["select"]) == 58
        assert document["tool"]["ruff"]["lint"]["ignore"] == ["E402"]
        assert document["tool"]["mypy"]["ignore_missing_imports"] is False
        assert len(document["tool"]["mypy"]["overrides"]) == 1

    def test_the_lane_added_exactly_three_paths(self) -> None:
        for relative in ("tools/check_mypy_baseline.py", "tools/mypy_baseline.json",
                         "tests/test_mypy_baseline_verifier.py"):
            assert (REPO / relative).exists()
        assert not (REPO / "tools" / "__init__.py").exists()


class TestManifestPinReplacedByProperty:
    """What the retired pyproject.toml and uv.lock whole-file pins guaranteed for mypy.

    The pins asserted "the manifest did not move", which was a proxy for "nothing the mypy
    baseline depends on moved". The build-backend lane moves two parts of the manifest that
    the baseline provably does not depend on, so the proxy is replaced by the dependency
    itself: the mypy configuration, the verifier contract, and the baseline identity.
    """

    LOCK_PACKAGE_DIGEST = "8f1915f2064f6d51a4ad10b6bb413403cc7fdf0916de51204350f95cb8768826"

    def manifest(self) -> dict[str, Any]:
        import tomllib

        return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    def test_the_mypy_table_is_structurally_unchanged(self) -> None:
        mypy = self.manifest()["tool"]["mypy"]
        assert mypy["python_version"] == "3.11"
        assert mypy["ignore_missing_imports"] is False
        assert mypy["warn_unused_ignores"] is False
        assert mypy["warn_redundant_casts"] is False
        assert mypy["check_untyped_defs"] is False
        assert "mypy_path" not in mypy

    def test_the_reportlab_override_is_exact(self) -> None:
        overrides = self.manifest()["tool"]["mypy"]["overrides"]
        assert len(overrides) == 1
        assert overrides[0]["ignore_missing_imports"] is True
        assert overrides[0]["module"] == [
            "reportlab",
            "reportlab.lib.pagesizes",
            "reportlab.lib.styles",
            "reportlab.lib.units",
            "reportlab.pdfbase",
            "reportlab.pdfbase.ttfonts",
            "reportlab.platypus",
        ]

    def test_the_ordered_scope_and_arguments_are_unchanged(self) -> None:
        assert verifier.SCOPE == ("app", "tools/git_object_source.py",
                                  "tools/package_source_distribution.py",
                                  "tools/public_content_policy.py", "tools/public_path_scan.py")
        assert list(verifier.COMMAND_ARGUMENTS) == BASELINE["command_arguments"]
        assert list(verifier.SCOPE) == BASELINE["scope"]

    def test_the_python_authorities_are_unchanged(self) -> None:
        assert verifier.REQUIRED_TARGET_PYTHON == "3.11" == BASELINE["target_python"]
        assert verifier.REQUIRED_EXECUTION_PYTHON == "3.12.12" == BASELINE["execution_python"]
        assert verifier.REQUIRED_MYPY_VERSION == "1.20.2" == BASELINE["mypy_version"]

    def test_import_diagnostics_remain_forbidden(self) -> None:
        assert verifier.IMPORT_CODES == ("import-not-found", "import-untyped", "import")
        assert all(i.get("code") not in verifier.IMPORT_CODES for i in BASELINE["issues"])

    def test_the_baseline_file_matches_the_reviewed_platform_correction(self) -> None:
        assert hashlib.sha256(BASELINE_PATH.read_bytes()).hexdigest() == (
            "5a2293845942d783196e3c80f72db7dfa602fa462236d35703351e407ab0aac2")

    def test_the_baseline_identity_fields_are_unchanged(self) -> None:
        assert BASELINE["classification"] == "mypy_debt_baselined"
        assert BASELINE["issues_sha256"] == (
            "4385d8277fd1e8880e7f83e65d2e70947191f209e228867b4d26821c21b1f73e")
        assert BASELINE["config_digest"] == (
            "cf7054b5eeb41acd50293b845d17eb68aab73820f4a4f7beb7da24bbfc9f01f5")
        assert verifier.compute_config_digest(REPO) == BASELINE["config_digest"]

    @pytest.mark.parametrize("old,new", [
        ('requires = ["hatchling==1.32.0"]', 'requires = ["hatchling==9.9.9"]'),
        ('"editables==0.6",', '"editables==0.7",'),
        ('"packaging==26.3",', '"packaging==26.0",'),
    ])
    def test_build_authority_does_not_move_the_config_digest(
        self, tmp_path: Path, old: str, new: str
    ) -> None:
        """The manifest pin is retired precisely because mypy identity cannot depend on it."""
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert old in text
        root = scratch_root(tmp_path)
        (root / "pyproject.toml").write_text(text.replace(old, new, 1), encoding="utf-8")
        assert verifier.compute_config_digest(root) == BASELINE["config_digest"]

    def test_no_build_package_appears_in_the_baseline_identity(self) -> None:
        for name in ("hatchling", "editables", "trove-classifiers"):
            assert name not in json.dumps(BASELINE["issues"])
            assert name not in BASELINE["command_arguments"]

    def test_the_lock_package_records_are_structurally_unchanged(self) -> None:
        import tomllib

        lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
        packages = sorted(lock["package"],
                          key=lambda p: (p.get("name", ""), p.get("version", "")))
        assert len(packages) == 46
        canonical = json.dumps(packages, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, default=str)
        assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == self.LOCK_PACKAGE_DIGEST

    @pytest.mark.parametrize("name,version", [
        ("pytest", "8.4.2"), ("ruff", "0.16.2"), ("mypy", "1.20.2"),
    ])
    def test_the_locked_static_tool_versions_are_unchanged(self, name: str, version: str) -> None:
        import tomllib

        lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
        record = next(p for p in lock["package"] if p["name"] == name)
        assert record["version"] == version

    def test_the_desktop_packager_is_exactly_locked(self) -> None:
        import tomllib

        lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
        record = next(p for p in lock["package"] if p["name"] == "pyinstaller")
        assert record["version"] == "6.22.2"

    def test_no_whole_file_manifest_pin_was_reintroduced(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        block = source.split('@pytest.mark.parametrize("relative,digest"')[1]
        pins = re.findall(r'\(\s*"([^"]+)",\s*"[0-9a-f]{16}"\s*\)', block[:block.index("])")])
        assert "pyproject.toml" not in pins
        assert "uv.lock" not in pins

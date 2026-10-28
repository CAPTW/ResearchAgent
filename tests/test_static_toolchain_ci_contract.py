# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls binding the contributor documentation to the enforced CI gates.

Documentation that drifts from CI is worse than no documentation: it tells a contributor
their change is fine and lets the pipeline disagree later. So the exact command strings
are compared token for token, in both directions, rather than described in prose.

The asymmetry that matters most is `--write-baseline`. It belongs in the documentation,
because a human sometimes has to accept a change in the debt. It must never appear in CI,
because a gate that can rewrite its own expectations has stopped being a gate.

No Git, no network, no YAML dependency -- this runs inside an extracted source package.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
WORKFLOW_PATH = REPO / ".github" / "workflows" / "ci.yml"
CI = WORKFLOW_PATH.read_text(encoding="utf-8")

LOCK_CHECK = "uv lock --check"
FROZEN_SYNC = "uv sync --frozen --extra dev"
RUFF = "uv run --frozen --extra dev ruff check --no-cache app tests tools scripts"
VERIFY = "uv run --frozen --extra dev python tools/check_mypy_baseline.py --check"
WRITE = "uv run --frozen --extra dev python tools/check_mypy_baseline.py --write-baseline"
SELF_MYPY_FLAGS = (
    "--config-file pyproject.toml --explicit-package-bases --no-incremental "
    "--show-error-codes --show-column-numbers --no-pretty --no-color-output "
    "--no-error-summary"
)
SCOPE = ("app", "tests", "tools", "scripts")

# Surviving pins from the historical docs/CI-only lane.
IMMUTABLE = {
    # pyproject.toml and uv.lock were pinned here while every lane was forbidden to touch the
    # manifest. The build-backend authority lane is explicitly authorized to change both, so a
    # whole-file digest would now forbid an authorized change rather than protect anything.
    # Replacing the digest with a new one would recreate the same cross-lane overconstraint, so
    # both are held by property instead -- see TestBuildAuthorityPreservesTheStaticContract
    # below and tests/test_project_build_backend_authority.py.
    # The platform correction intentionally changes the reviewed diagnostic inventory.
    # The verifier's negative tests and exact baseline pins are maintained together in
    # test_mypy_baseline_verifier.py; a second historical byte pin would contradict them.
    # tests/test_mypy_baseline_verifier.py is deliberately absent: this lane retargets one
    # obsolete parameter in it, and pinning a file inside your own authorized closure is
    # exactly the mistake being corrected there. Its content is asserted by property in
    # TestPredecessorInvariantRetarget instead.
    "tools/public_path_scan.py": "0ff9a0cd21521acb",
    # Public-snapshot packaging now evolves under committed-object and exclusion
    # behaviour tests, as described in TestPredecessorInvariantRetarget below.
    "tests/test_mypy_dual_import_contract.py": "cdec62849f5ae5fc",
    "tests/test_dev_tool_lock_and_ruff_contract.py": "85e3d71beae3b643",
}


def normalize(text: str) -> str:
    """Collapse shell line continuations and whitespace so docs and YAML compare."""
    return " ".join(text.replace("\\\n", " ").split())


def cache_agnostic(text: str) -> str:
    """Erase the dynamic cache path -- the requirement is that it is external, not equal."""
    # A quoted argument may contain spaces -- `"$(mktemp -d)"` and
    # `"${{ runner.temp }}/..."` both do -- so quoted forms are matched whole.
    text = re.sub(r'--cache-dir\s+(?:"[^"]*"|\S+)', "--cache-dir <EXTERNAL>", text)
    return normalize(text)


DOCS_FLAT = normalize(DOCS)
CI_FLAT = normalize(CI)


def backend_job() -> str:
    start = CI.index("  backend-contracts:")
    end = CI.index("\n  frontend-contracts:", start)
    return CI[start:end]


BACKEND = backend_job()
BACKEND_FLAT = normalize(BACKEND)


class TestContributorContract:
    @pytest.mark.parametrize("command", [LOCK_CHECK, FROZEN_SYNC, RUFF, VERIFY, WRITE])
    def test_the_exact_command_is_documented(self, command: str) -> None:
        assert command in DOCS_FLAT

    def test_self_mypy_flags_and_external_cache_are_documented(self) -> None:
        assert SELF_MYPY_FLAGS in DOCS_FLAT
        assert "--cache-dir" in DOCS_FLAT
        assert "MYPY_CACHE_DIR" in DOCS
        assert ".mypy_cache" not in cache_agnostic(DOCS).split("--cache-dir")[1][:80]

    def test_the_designated_interpreter_is_exact(self) -> None:
        assert "3.12.12" in DOCS

    @pytest.mark.parametrize("tool,version", [("Ruff", "0.16.2"), ("mypy", "1.20.2")])
    def test_the_locked_tool_versions_are_stated(self, tool: str, version: str) -> None:
        assert version in DOCS, f"{tool} {version} is what uv.lock pins"

    def test_the_current_classification_is_stated(self) -> None:
        assert "mypy_debt_baselined" in DOCS
        for figure in ("559 errors", "20 notes", "579 diagnostics", "69 files"):
            assert figure in DOCS

    def test_mypy_clean_is_disclaimed(self) -> None:
        assert re.search(r"not\s+\*?\*?mypy-clean", DOCS, re.IGNORECASE)

    def test_ruff_all_cleanliness_is_disclaimed(self) -> None:
        assert "not `ruff --select ALL`" in DOCS or "not Ruff `ALL` clean" in DOCS
        assert "--select ALL" not in RUFF

    def test_no_global_path_fallback_is_endorsed(self) -> None:
        assert "not from your global PATH" in DOCS
        assert "--with ruff" not in DOCS, "the `--with` form bypasses the frozen environment"

    def test_the_baseline_update_procedure_is_ordered_and_gated(self) -> None:
        procedure = DOCS[DOCS.index("### Updating the baseline"):]
        for step in ("import-not-found", "Re-run the check", "together"):
            assert step in procedure
        assert "CI never runs `--write-baseline`" in procedure


class TestCiWorkflow:
    def test_the_active_backend_job_exists(self) -> None:
        assert "backend-contracts:" in CI
        assert "runs-on: ubuntu-24.04" in BACKEND

    def test_the_python_authority_is_the_exact_patch(self) -> None:
        assert 'python-version: "3.12.12"' in BACKEND
        assert 'python-version: "3.11"' not in CI
        assert 'python-version: "3.12"\n' not in CI

    @pytest.mark.parametrize("command", [LOCK_CHECK, FROZEN_SYNC, RUFF, VERIFY])
    def test_the_exact_command_is_enforced(self, command: str) -> None:
        assert command in BACKEND_FLAT

    def test_the_frozen_sync_carries_the_dev_extra(self) -> None:
        assert "uv sync --extra dev" not in BACKEND
        assert FROZEN_SYNC in BACKEND

    def test_self_mypy_is_enforced_with_a_runner_temp_cache(self) -> None:
        assert SELF_MYPY_FLAGS in BACKEND_FLAT
        assert "${{ runner.temp }}" in BACKEND
        assert re.search(r"--cache-dir\s+\"\$\{\{ runner\.temp \}\}/", BACKEND)

    def test_ci_never_writes_the_baseline(self) -> None:
        assert "--write-baseline" not in CI

    @pytest.mark.parametrize("forbidden", [
        "continue-on-error", "|| true", "|| exit 0", "--select ALL", "NUL",
    ])
    def test_no_failure_masking_or_forbidden_target(self, forbidden: str) -> None:
        assert forbidden not in BACKEND

    def test_no_checkout_relative_mypy_cache(self) -> None:
        assert ".mypy_cache" not in CI
        assert ".ruff_cache" not in CI

    def test_unrelated_workflow_shape_is_retained(self) -> None:
        for fragment in ("name: CI", "pull_request:", "workflow_dispatch:",
                         "permissions:\n  contents: read", "concurrency:",
                         "frontend-contracts:", "npm run build",
                         "Run backend regression suite", "storage/runs"):
            assert fragment in CI

    def test_the_static_steps_do_not_override_the_job_shell(self) -> None:
        for step in ("Check lockfile consistency", "Check mypy debt baseline",
                     "Check mypy baseline verifier is self-typed"):
            block = BACKEND[BACKEND.index(step):][:400]
            assert "shell:" not in block

    def test_windows_contracts_have_an_independent_exact_runtime(self) -> None:
        windows = CI[CI.index("\n  windows-contracts:"):]
        assert "runs-on: windows-2022" in windows
        assert "needs:" not in windows
        assert "uses: astral-sh/setup-uv@v5" in windows
        assert 'python-version: "3.12.12"' in windows
        assert "${{ runner." not in windows.split("\n    steps:", 1)[0]
        assert ('"UV_PYTHON_INSTALL_DIR=${{ runner.temp }}/xrml-python" '
                '| Out-File -FilePath $env:GITHUB_ENV') in windows
        assert "uv python install 3.12.12" in windows
        assert FROZEN_SYNC in windows
        assert "sys.platform == 'win32'" in windows
        assert "sys.version_info[:3] == (3, 12, 12)" in windows
        assert "actions/setup-python" not in windows

    def test_windows_operator_and_native_scope_is_executed_without_masking(self) -> None:
        windows = CI[CI.index("\n  windows-contracts:"):]
        for module in ("activation_scripts", "browser_native_host", "model_credentials",
                       "codex_journal", "grok_prepared_review", "topic_schedule_locking",
                       "topic_scheduler", "report_transaction"):
            assert f"tests/test_{module}.py" in windows
        for forbidden in ("continue-on-error", "|| true", "|| exit 0", "--ignore", " -k "):
            assert forbidden not in windows
        assert "uv run --frozen --extra dev pytest -x --tb=short" in normalize(windows)
        # Fail-fast changes failure reporting, not the selected Linux test scope.
        assert "run: uv run --frozen --extra dev pytest -x --tb=short\n" in BACKEND


class TestLocalCiParity:
    @pytest.mark.parametrize("command", [LOCK_CHECK, FROZEN_SYNC, RUFF, VERIFY])
    def test_documented_and_enforced_commands_are_identical(self, command: str) -> None:
        assert command in DOCS_FLAT and command in BACKEND_FLAT

    def test_self_mypy_matches_once_the_cache_path_is_normalized(self) -> None:
        pattern = re.compile(
            r"python -m mypy " + re.escape(SELF_MYPY_FLAGS)
            + r" --cache-dir <EXTERNAL> tools/check_mypy_baseline\.py")
        assert pattern.search(cache_agnostic(DOCS)), "documentation form"
        assert pattern.search(cache_agnostic(BACKEND)), "CI form"

    def test_ruff_scope_order_is_preserved_everywhere(self) -> None:
        for text in (DOCS_FLAT, BACKEND_FLAT):
            tail = text[text.index(RUFF) + len(RUFF) - len(" ".join(SCOPE)):]
            assert tail.startswith(" ".join(SCOPE))

    @pytest.mark.parametrize("flag", [
        "--explicit-package-bases", "--no-incremental", "--no-error-summary",
        "--show-error-codes", "--show-column-numbers", "--no-pretty", "--no-color-output",
    ])
    def test_semantic_flags_survive_in_both(self, flag: str) -> None:
        assert flag in DOCS_FLAT and flag in BACKEND_FLAT

    def test_write_baseline_is_documentation_only(self) -> None:
        assert WRITE in DOCS_FLAT
        assert "--write-baseline" not in CI


class TestPredecessorInvariantRetarget:
    """The predecessor pin was correct for its lane and wrong for this one.

    `test_no_pre_existing_file_was_modified` froze the SHA of every file the
    baseline/verifier lane was forbidden to touch, `CONTRIBUTING.md` included. That lane
    has passed. Updating the digest would have kept asserting a cross-lane invariant that
    is no longer true; removing exactly that one entry restores the assertion to the files
    that really are out of bounds, and the contributor document is now held to a property
    contract here instead.
    """

    PREDECESSOR = (REPO / "tests" / "test_mypy_baseline_verifier.py").read_text(encoding="utf-8")

    def test_the_obsolete_contributing_pin_is_gone(self) -> None:
        assert "9fc366c79fd7244c" not in self.PREDECESSOR
        assert '"CONTRIBUTING.md"' not in self.PREDECESSOR

    def test_the_containing_assertion_still_exists(self) -> None:
        assert "def test_no_pre_existing_file_was_modified" in self.PREDECESSOR

    # Retire only the two manifest pins from the build-backend lane and the packager
    # pin superseded by authorized public-snapshot work. Keep the exact remaining set,
    # rather than accepting removal of an arbitrary number of pins.
    BASE_PINS = frozenset({
        "pyproject.toml",
        "uv.lock",
        "tools/package_source_distribution.py",
        "tools/public_path_scan.py",
        "tests/test_mypy_dual_import_contract.py",
        "tests/test_dev_tool_lock_and_ruff_contract.py",
    })
    RETIRED_PINS = frozenset({"pyproject.toml", "uv.lock", "tools/package_source_distribution.py"})

    @pytest.mark.parametrize("relative,digest", [
        ("tools/public_path_scan.py", "0ff9a0cd21521acb"),
        ("tests/test_mypy_dual_import_contract.py", "cdec62849f5ae5fc"),
        ("tests/test_dev_tool_lock_and_ruff_contract.py", "85e3d71beae3b643"),
    ])
    def test_every_other_pinned_invariant_survives(self, relative: str, digest: str) -> None:
        assert f'("{relative}", "{digest}")' in self.PREDECESSOR

    # Deliberately permissive about *form* and strict about *meaning*. A pin that evaded this
    # by using single quotes, a trailing comma, uppercase hex or a full 64-character digest
    # would be reintroduced silently, so all of those still count as a pin.
    PIN_RE = re.compile(r"""\(\s*["']([^"']+)["']\s*,\s*["'][0-9a-fA-F]{16,64}["']\s*,?\s*\)""")

    def observed_pins(self) -> frozenset[str]:
        # Every parametrize block with this signature, not just the first: a second block
        # later in the file would otherwise be an unobserved place to hide a pin.
        pins: set[str] = set()
        for block in self.PREDECESSOR.split('@pytest.mark.parametrize("relative,digest"')[1:]:
            pins.update(self.PIN_RE.findall(block[:block.index("])")]))
        return frozenset(pins)

    def test_no_retired_pin_survives_anywhere_in_the_file(self) -> None:
        """Whole-file sweep, independent of how the parametrize blocks are split."""
        for relative in self.PIN_RE.findall(self.PREDECESSOR):
            assert relative not in self.RETIRED_PINS

    # Renamed in place from test_exactly_six_pins_remain, with strictly stronger semantics:
    # the old assertion checked only that six entries existed and would have accepted any six.
    def test_the_surviving_pins_are_the_base_set_minus_the_named_retired(self) -> None:
        assert self.observed_pins() == self.BASE_PINS - self.RETIRED_PINS

    def test_no_extra_pin_disappeared(self) -> None:
        assert not (self.BASE_PINS - self.RETIRED_PINS) - self.observed_pins()

    def test_no_new_whole_file_pin_was_added(self) -> None:
        assert not self.observed_pins() - self.BASE_PINS

    @pytest.mark.parametrize("relative", sorted(RETIRED_PINS))
    def test_the_obsolete_manifest_pins_are_gone(self, relative: str) -> None:
        # Must require an actual digest: `("pyproject.toml", "uv.lock")` also appears as an
        # ordinary tuple in the predecessor's scratch-root helper, which is not a pin.
        assert not re.search(rf'\(\s*"{re.escape(relative)}",\s*"[0-9a-f]{{16}}"\s*\)',
                             self.PREDECESSOR)

    @pytest.mark.parametrize("marker", [
        "def test_the_mypy_table_is_structurally_unchanged",
        "def test_the_reportlab_override_is_exact",
        "def test_the_lock_package_records_are_structurally_unchanged",
        "def test_no_whole_file_manifest_pin_was_reintroduced",
    ])
    def test_structural_replacements_are_present_in_the_predecessor(self, marker: str) -> None:
        assert marker in self.PREDECESSOR

    def test_nothing_was_skipped_or_deselected_instead(self) -> None:
        assert "skip" not in self.PREDECESSOR and "xfail" not in self.PREDECESSOR

    def test_reintroducing_the_pin_would_break_the_document(self) -> None:
        """The retarget is only safe because the pin genuinely cannot hold any more."""
        current = hashlib.sha256((REPO / "CONTRIBUTING.md").read_bytes()).hexdigest()
        assert not current.startswith("9fc366c79fd7244c")


class TestImmutableSourceContract:
    @pytest.mark.parametrize("relative,digest", sorted(IMMUTABLE.items()))
    def test_the_lane_changed_no_product_file(self, relative: str, digest: str) -> None:
        actual = hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
        assert actual.startswith(digest), f"{relative} changed in a docs/CI-only lane"


class TestBuildAuthorityPreservesTheStaticContract:
    """What the retired pyproject.toml and uv.lock whole-file pins used to guarantee.

    A digest said "none of this moved" in one indivisible assertion. The build-backend lane
    has to move two specific things, so the guarantee is restated as the individual properties
    the digest was standing in for. Each one fails on its own and names what broke.
    """

    LOCK_PACKAGE_DIGEST = "8f1915f2064f6d51a4ad10b6bb413403cc7fdf0916de51204350f95cb8768826"

    def manifest(self) -> dict:
        import tomllib

        return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    def test_project_identity_is_unchanged(self) -> None:
        project = self.manifest()["project"]
        assert project["name"] == "xrml-research-agent"
        assert project["version"] == "0.1.0"
        assert project["requires-python"] == ">=3.11"

    def test_runtime_dependencies_are_unchanged(self) -> None:
        assert self.manifest()["project"]["dependencies"] == [
            "fastapi>=0.116,<1.0",
            "httpx>=0.28,<1.0",
            "itsdangerous>=2.2,<3.0",
            "PyJWT[crypto]>=2.10,<3.0",
            "reportlab>=4.4,<5.0",
            "uvicorn>=0.35,<1.0",
        ]

    def test_optional_dependency_groups_are_unchanged(self) -> None:
        assert self.manifest()["project"]["optional-dependencies"] == {
            "fulltext": ["pymupdf>=1.26,<2.0"],
            "codex-sdk": ["openai-codex"],
            "dev": [
                "httpx>=0.28,<1.0",
                "pytest>=8.4,<9.0",
                "ruff>=0.14,<1.0",
                "mypy>=1.19,<2.0",
            ],
            "desktop": ["pyinstaller==6.22.2"],
        }

    def test_the_ruff_contract_is_unchanged(self) -> None:
        ruff = self.manifest()["tool"]["ruff"]
        assert ruff["line-length"] == 120
        assert ruff["target-version"] == "py311"
        assert len(ruff["lint"]["select"]) == 58
        assert ruff["lint"]["ignore"] == ["E402"]

    def test_the_mypy_table_is_unchanged(self) -> None:
        mypy = self.manifest()["tool"]["mypy"]
        assert mypy["python_version"] == "3.11"
        assert mypy["ignore_missing_imports"] is False
        assert mypy["warn_unused_ignores"] is False
        assert mypy["warn_redundant_casts"] is False
        assert mypy["check_untyped_defs"] is False
        assert "mypy_path" not in mypy

    def test_the_reportlab_override_is_unchanged(self) -> None:
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

    def test_the_build_system_authority_is_exact(self) -> None:
        build = self.manifest()["build-system"]
        assert build["requires"] == ["hatchling==1.32.0"]
        assert build["build-backend"] == "hatchling.build"

    def test_the_build_constraints_are_exact(self) -> None:
        assert self.manifest()["tool"]["uv"]["build-constraint-dependencies"] == [
            "editables==0.6",
            "hatchling==1.32.0",
            "packaging==26.3",
            "pathspec==1.1.1",
            "pluggy==1.6.0",
            "tomlkit==0.15.1",
            "trove-classifiers==2026.6.1.19",
        ]

    def test_no_build_package_leaked_into_runtime_or_dev(self) -> None:
        project = self.manifest()["project"]
        declared = project["dependencies"] + project["optional-dependencies"]["dev"]
        for name in ("hatchling", "editables", "tomlkit", "trove-classifiers", "pathspec"):
            assert not any(r.lower().startswith(name) for r in declared)

    def test_the_lock_package_records_are_structurally_unchanged(self) -> None:
        import json
        import tomllib

        lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
        packages = sorted(lock["package"], key=lambda p: (p.get("name", ""), p.get("version", "")))
        assert len(packages) == 46
        canonical = json.dumps(packages, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, default=str)
        assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == self.LOCK_PACKAGE_DIGEST

    @pytest.mark.parametrize("command", [LOCK_CHECK, FROZEN_SYNC, RUFF, VERIFY, WRITE])
    def test_the_documented_static_tool_commands_survive(self, command: str) -> None:
        assert command in normalize(DOCS)

    @pytest.mark.parametrize("command", [RUFF, VERIFY])
    def test_the_ci_static_tool_commands_survive(self, command: str) -> None:
        assert command in normalize(CI)

    def test_the_new_build_section_did_not_weaken_ci(self) -> None:
        assert "--write-baseline" not in CI
        assert "--no-build-isolation" not in CI
        assert "--no-install-project" not in CI

    def test_the_baseline_identities_are_unchanged(self) -> None:
        import json

        baseline = json.loads((REPO / "tools" / "mypy_baseline.json").read_text(encoding="utf-8"))
        assert baseline["classification"] == "mypy_debt_baselined"
        assert (baseline["error_count"], baseline["note_count"]) == (559, 20)
        assert (baseline["diagnostic_count"], baseline["affected_file_count"]) == (579, 69)
        assert baseline["issues_sha256"] == (
            "4385d8277fd1e8880e7f83e65d2e70947191f209e228867b4d26821c21b1f73e")
        assert baseline["config_digest"] == (
            "cf7054b5eeb41acd50293b845d17eb68aab73820f4a4f7beb7da24bbfc9f01f5")


class TestNegativeMutations:
    """Every check above is only worth what its failure mode is worth."""

    def check_ci(self, text: str) -> bool:
        """True when the mutated CI text still satisfies the enforced contract."""
        backend = text[text.index("  backend-contracts:"):text.index("\n  frontend-contracts:")]
        flat = normalize(backend)
        return (RUFF in flat and VERIFY in flat and LOCK_CHECK in flat
                and FROZEN_SYNC in flat and SELF_MYPY_FLAGS in flat
                and "--write-baseline" not in text
                and "continue-on-error" not in text and "|| true" not in text
                and ".mypy_cache" not in text and "NUL" not in text
                and "--select ALL" not in text
                and 'python-version: "3.12.12"' in backend
                and "runs-on: ubuntu-24.04" in backend
                and "${{ runner.temp }}" in text)

    @pytest.mark.parametrize("old,new", [
        (RUFF, "uv run --frozen --extra dev ruff check --no-cache app tests tools"),
        (VERIFY, "uv run --frozen --extra dev python tools/check_mypy_baseline.py"),
        (VERIFY, WRITE),
        ('"${{ runner.temp }}/mypy-verifier-cache"', ".mypy_cache"),
        ('"${{ runner.temp }}/mypy-verifier-cache"', "NUL"),
        ('python-version: "3.12.12"', 'python-version: "3.12"'),
        ('python-version: "3.12.12"', 'python-version: "3.11"'),
        ("runs-on: ubuntu-24.04", "runs-on: ubuntu-latest"),
        (LOCK_CHECK, "uv lock"),
        (FROZEN_SYNC, "uv sync --extra dev"),
        ("--explicit-package-bases ", ""),
    ])
    def test_a_weakened_ci_contract_is_rejected(self, old: str, new: str) -> None:
        mutated = CI.replace(old, new, 1)
        assert mutated != CI, f"mutation anchor missing: {old!r}"
        assert not self.check_ci(mutated)

    def test_continue_on_error_is_rejected(self) -> None:
        mutated = CI.replace("        run: uv lock --check",
                             "        continue-on-error: true\n        run: uv lock --check", 1)
        assert not self.check_ci(mutated)

    @pytest.mark.parametrize("claim", [
        "This repository is mypy-clean.",
        "Ruff `ALL` is clean.",
    ])
    def test_an_overclaiming_document_is_rejected(self, claim: str) -> None:
        mutated = DOCS + "\n" + claim + "\n"
        forbidden = re.search(r"\bis mypy-clean\b|\bALL` is clean\b", mutated)
        assert forbidden is not None
        assert not re.search(r"\bis mypy-clean\b|\bALL` is clean\b", DOCS)

    def test_reintroducing_the_obsolete_pin_is_detected(self) -> None:
        predecessor = (REPO / "tests" / "test_mypy_baseline_verifier.py").read_text(encoding="utf-8")
        mutated = predecessor.replace(
            '        ("tests/test_dev_tool_lock_and_ruff_contract.py", "85e3d71beae3b643"),',
            '        ("tests/test_dev_tool_lock_and_ruff_contract.py", "85e3d71beae3b643"),\n'
            '        ("CONTRIBUTING.md", "9fc366c79fd7244c"),', 1)
        assert mutated != predecessor
        assert "9fc366c79fd7244c" in mutated
        assert "9fc366c79fd7244c" not in predecessor

    def test_divergent_command_order_is_detected(self) -> None:
        swapped = RUFF.replace("app tests tools scripts", "tests app tools scripts")
        assert swapped not in DOCS_FLAT
        assert swapped not in BACKEND_FLAT
        assert normalize(CI.replace(RUFF, swapped, 1)) != CI_FLAT

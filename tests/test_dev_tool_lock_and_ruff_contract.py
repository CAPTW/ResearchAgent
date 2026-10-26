# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the locked development toolchain and the explicit Ruff contract.

Two defects sat behind this. The `dev` extra declared Ruff and mypy but `uv.lock`
contained neither, so `uv sync --frozen --extra dev` succeeded while installing
nothing -- and every check afterwards silently used whatever was on the global
PATH. And Ruff's *implicit* defaults are version-dependent: the globally installed
0.14.14 reported 6 findings over this scope while the locked 0.16.2 reports 894.

So the rule set is now enumerated rather than inherited. It is written by code,
not by prefix, because 0.14's default `E4` group is E401 alone -- selecting "E4"
also pulls in E402, which the committed ignore deliberately drops.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPORTLAB_MODULES = [
    "reportlab", "reportlab.lib.pagesizes", "reportlab.lib.styles", "reportlab.lib.units",
    "reportlab.pdfbase", "reportlab.pdfbase.ttfonts", "reportlab.platypus",
]
PYPROJECT = tomllib.load((REPO / "pyproject.toml").open("rb"))
LOCK = (REPO / "uv.lock").read_text(encoding="utf-8")

RUFF_SCOPE = ("app", "tests", "tools", "scripts")
LOCKED = {"ruff": "0.16.2", "mypy": "1.20.2",
          "mypy-extensions": "1.1.0", "pathspec": "1.1.1", "librt": "0.15.0"}


def locked_version(package: str) -> str | None:
    match = re.search(r'name = "%s"\nversion = "([^"]+)"' % re.escape(package), LOCK)
    return match.group(1) if match else None


class TestDeclarationAndLock:
    def test_the_dev_selector_is_an_optional_extra(self) -> None:
        """`--extra dev`, not `--group dev`: the flag is derived, never assumed."""
        assert "dev" in PYPROJECT["project"]["optional-dependencies"]
        assert not PYPROJECT.get("dependency-groups")

    @pytest.mark.parametrize("tool,spec", [("ruff", "ruff>=0.14,<1.0"), ("mypy", "mypy>=1.19,<2.0")])
    def test_the_tool_stays_declared(self, tool: str, spec: str) -> None:
        assert spec in PYPROJECT["project"]["optional-dependencies"]["dev"]

    @pytest.mark.parametrize("package,version", sorted(LOCKED.items()))
    def test_the_locked_entry_exists(self, package: str, version: str) -> None:
        assert locked_version(package) == version

    def test_locked_versions_satisfy_the_declared_ranges(self) -> None:
        assert locked_version("ruff").startswith("0.1")
        assert int(locked_version("mypy").split(".")[0]) == 1

    def test_only_tool_reachable_packages_were_added(self) -> None:
        """`mypy-extensions`, `pathspec` and `librt` are mypy's own dependencies."""
        mypy_block = re.search(r'name = "mypy"\n.*?(?=\n\[\[package\]\])', LOCK, re.DOTALL).group(0)
        for package in ("mypy-extensions", "pathspec", "librt"):
            assert package in mypy_block, f"{package} is not reachable from mypy"


class TestExplicitRuffContract:
    def test_the_rule_set_is_enumerated_not_inherited(self) -> None:
        select = PYPROJECT["tool"]["ruff"]["lint"]["select"]
        assert len(select) == 58, "the contract must stay the exact measured 0.14.14 default set"
        assert all(re.fullmatch(r"[A-Z]+\d+", code) for code in select), (
            "codes only -- a prefix silently widens with the next Ruff release"
        )

    def test_e402_is_not_reintroduced_by_a_prefix(self) -> None:
        """Selecting `E4` would re-add E402, which the committed ignore drops."""
        select = PYPROJECT["tool"]["ruff"]["lint"]["select"]
        assert "E401" in select
        assert "E402" not in select
        assert PYPROJECT["tool"]["ruff"]["lint"]["ignore"] == ["E402"]

    def test_no_broad_suppression_was_added(self) -> None:
        lint = PYPROJECT["tool"]["ruff"]["lint"]
        assert set(lint) <= {"select", "ignore"}, f"unexpected lint keys: {sorted(lint)}"
        assert "per-file-ignores" not in lint and "extend-ignore" not in lint

    def test_the_target_version_is_unchanged(self) -> None:
        assert PYPROJECT["tool"]["ruff"]["target-version"] == "py311"

    def test_no_ruff_debt_baseline_exists(self) -> None:
        assert not list((REPO / "tools").glob("*ruff*baseline*"))


class TestDeferredMypyStateIsUntouched:
    """This checkpoint installs mypy; it does not run or configure it."""

    def test_mypy_configuration_matches_authorized_post_repair_contract(self) -> None:
        """Retargeted one-for-one, and stricter than what it replaces.

        This used to diff the whole `[tool.mypy]` table against the base commit to pin the
        then-deferred state. The owner has since authorized removing the global
        missing-import suppression, so it now names the exact authorized end state -- and
        additionally pins the table boundary, which the old diff could not see: a
        misplaced array-of-tables moves global keys into the override without changing a
        single mypy count. It also no longer needs Git history, so it runs in an
        extracted source.
        """
        mypy = PYPROJECT["tool"]["mypy"]
        assert mypy["python_version"] == "3.11"
        assert mypy["ignore_missing_imports"] is False
        for key in ("warn_unused_ignores", "warn_redundant_casts", "check_untyped_defs"):
            assert mypy[key] is False, f"{key} must remain a global mypy setting"
            assert key not in mypy["overrides"][0], f"{key} was absorbed by the override"
        assert len(mypy["overrides"]) == 1
        assert set(mypy["overrides"][0]) == {"module", "ignore_missing_imports"}
        assert mypy["overrides"][0]["module"] == REPORTLAB_MODULES
        assert "mypy_path" not in mypy
        assert PYPROJECT["tool"]["ruff"]["target-version"] == "py311"
        assert len(PYPROJECT["tool"]["ruff"]["lint"]["select"]) == 58

    def test_global_import_suppression_is_removed_and_reportlab_override_is_exact(self) -> None:
        """The inverse of the assertion it replaces, one for one.

        It recorded that the global suppression was still present, to be narrowed later.
        Later is now: it is gone, and the only thing standing in for it is an enumerated
        seven-module third-party exception.
        """
        mypy = PYPROJECT["tool"]["mypy"]
        assert mypy.get("ignore_missing_imports") in (False, None)
        assert mypy["overrides"][0]["ignore_missing_imports"] is True
        modules = mypy["overrides"][0]["module"]
        assert modules == REPORTLAB_MODULES
        assert not any(m.endswith("*") for m in modules)
        assert not any(m.split(".")[0] in {"app", "tools"} for m in modules)
        assert not (REPO / "tools" / "__init__.py").exists()

    def test_no_mypy_baseline_was_created(self) -> None:
        assert not list((REPO / "tools").glob("*static_analysis_baseline*"))


class TestContributorContractMatchesImplementation:
    def test_the_documented_commands_are_the_implemented_ones(self) -> None:
        text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
        assert "uv sync --frozen --extra dev" in text
        assert "uv lock --check" in text
        assert "ruff check --no-cache " + " ".join(RUFF_SCOPE) in text

    def test_the_documentation_does_not_overclaim(self) -> None:
        """The gate is a bounded core contract, and mypy is not yet accepted."""
        text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8").lower()
        assert "not" in text and "mypy" in text
        assert "ruff all" not in text

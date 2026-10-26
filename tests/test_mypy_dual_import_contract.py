# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the dual-import contract the type checker and the runtime disagree about.

Two tools import their siblings two ways on purpose: as `tools.X` when imported as a
package, and as a bare `X` after a `sys.path` insertion when run as a standalone script
inside an extracted source with no package context. Under `--explicit-package-bases` mypy
resolves those files as `tools.*`, so the bare fallback had no module to find -- four
`import-not-found` errors that a global `ignore_missing_imports` had been hiding along
with everything else.

The `TYPE_CHECKING` split changes what the checker sees without changing what the runtime
does. Both halves are pinned here, because either one is easy to break silently: delete
the static branch and the import errors return; touch the runtime branch and standalone
execution breaks with nothing failing in a package context.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.load((REPO / "pyproject.toml").open("rb"))
MYPY = PYPROJECT["tool"]["mypy"]
SPLIT_FILES = ("tools/public_path_scan.py", "tools/package_source_distribution.py")
REPORTLAB_MODULES = [
    "reportlab", "reportlab.lib.pagesizes", "reportlab.lib.styles", "reportlab.lib.units",
    "reportlab.pdfbase", "reportlab.pdfbase.ttfonts", "reportlab.platypus",
]
GLOBAL_SCALARS = ("warn_unused_ignores", "warn_redundant_casts", "check_untyped_defs")


class TestConfigurationContract:
    def test_the_global_suppression_is_gone(self) -> None:
        assert MYPY.get("ignore_missing_imports") in (False, None)

    def test_reportlab_is_the_only_exception_and_is_enumerated(self) -> None:
        assert len(MYPY["overrides"]) == 1
        assert MYPY["overrides"][0]["module"] == REPORTLAB_MODULES
        assert set(MYPY["overrides"][0]) == {"module", "ignore_missing_imports"}

    @pytest.mark.parametrize("forbidden", ["reportlab.*", "*", "app.*", "tools.*"])
    def test_no_wildcard_or_first_party_pattern(self, forbidden: str) -> None:
        for override in MYPY["overrides"]:
            assert forbidden not in override["module"]

    @pytest.mark.parametrize("key", GLOBAL_SCALARS)
    def test_global_scalars_did_not_fall_into_the_override(self, key: str) -> None:
        """A TOML array-of-tables claims everything after it.

        Placing the override before these keys moved them into it and changed the
        effective configuration while every mypy count stayed identical. Only the parse
        shape reveals that, so it is asserted rather than inferred.
        """
        assert key in MYPY
        assert key not in MYPY["overrides"][0]

    def test_targets_and_layout_are_untouched(self) -> None:
        assert MYPY["python_version"] == "3.11"
        assert "mypy_path" not in MYPY
        assert PYPROJECT["tool"]["ruff"]["target-version"] == "py311"
        assert len(PYPROJECT["tool"]["ruff"]["lint"]["select"]) == 58
        assert not (REPO / "tools" / "__init__.py").exists()

    def test_pyproject_ends_with_exactly_one_newline(self) -> None:
        raw = (REPO / "pyproject.toml").read_bytes()
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


class TestSourceArchitecture:
    @pytest.mark.parametrize("relative", SPLIT_FILES)
    def test_the_static_branch_imports_canonically(self, relative: str) -> None:
        text = (REPO / relative).read_text(encoding="utf-8")
        assert "from typing import TYPE_CHECKING" in text
        assert "if TYPE_CHECKING:" in text
        assert "    from tools import " in text

    @pytest.mark.parametrize("relative", SPLIT_FILES)
    def test_the_runtime_fallback_survives(self, relative: str) -> None:
        """The point of the split is that this half is untouched."""
        text = (REPO / relative).read_text(encoding="utf-8")
        assert "except ImportError:" in text
        assert "sys.path.insert(0, str(Path(__file__).resolve().parent))" in text

    @pytest.mark.parametrize("relative", SPLIT_FILES)
    def test_no_suppression_or_import_hook_was_added(self, relative: str) -> None:
        text = (REPO / relative).read_text(encoding="utf-8")
        assert "type: ignore[import-not-found]" not in text
        assert "importlib" not in text


class TestRuntimeSmoke:
    def test_both_modules_import_under_the_tools_namespace(self) -> None:
        from tools import package_source_distribution, public_path_scan

        for module in (public_path_scan, package_source_distribution):
            assert Path(module.__file__).resolve().is_relative_to(REPO)

    @pytest.mark.parametrize("relative", SPLIT_FILES)
    def test_direct_script_execution_works_from_an_unrelated_directory(
        self, relative: str, tmp_path: Path
    ) -> None:
        """By absolute path, with an unrelated cwd -- the standalone contract."""
        probe = (
            "import runpy, sys\n"
            f"sys.argv = [r'{Path(relative).name}', '--help']\n"
            "try:\n"
            f"    runpy.run_path(r'{REPO / relative}', run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    raise SystemExit(0 if exc.code in (0, None) else 1)\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-400:]

    def test_the_canonical_import_wins_when_the_package_is_importable(self) -> None:
        """The fallback is a fallback.

        A fail-closed proof needs an environment with no installed project at all, which
        an editable-install suite cannot provide; that belongs to the extracted-source
        lane.
        """
        from tools import public_path_scan

        assert Path(public_path_scan.public_content_policy.__file__).resolve().is_relative_to(REPO)


class TestExistingControlsHold:
    @pytest.mark.parametrize("path,tier", [
        (".env", "fatal"), (".envrc", "fatal"), (".mcp.json", "fatal"), ("tool.exe", "prohibited"),
    ])
    def test_secret_and_binary_controls_remain_armed(self, path: str, tier: str) -> None:
        from tools import package_source_distribution as psd

        included, _excluded, prohibited, fatal = psd.classify([path])
        assert path not in included
        assert path in [p for p, _ in (fatal if tier == "fatal" else prohibited)]

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the public-path scanner and the portable entrypoints.

Two of these exist because of defects that actually happened. The scanner's first
draft over-escaped backslashes and classified the three batch scripts -- the whole
reason it was written -- as false positives. The batch helper's first draft put a
percent expansion inside a comment, which cmd.exe parses as a parameter reference
and aborts the script. Both are pinned below so neither can return quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools import package_source_distribution as psd
from tools import public_path_scan as pps

REPO = Path(__file__).resolve().parents[1]

# The detector has to be proven against the exact shapes it exists to catch, but
# this file ships in the public source distribution and must not carry the owner's
# actual layout. Each shape is assembled from segments, so the runtime values are
# precisely the ones the scanner must reject while the file itself contains none
# of them whole. Splitting at these seams is deliberate: joining them differently
# would put a real root back into the shipped bytes.
_USERS, _ACCOUNT, _DEV, _REPOS = "Users", "USER", "dev", "repos"

OWNER_SHAPES: tuple[str, ...] = (
    "C:\\" + _USERS + "\\" + _ACCOUNT + "\\source\\repos\\thing",
    "C:/" + _USERS + "/" + _ACCOUNT + "/.codex/auth.json",
    "D:\\" + _DEV + "\\" + _REPOS + "\\Rsearch Agent",
    "C:\\XRML" + "Data\\locks",
    "C:\\Program " + "Files\\PowerShell\\7\\pwsh.exe",
    "/c/" + _USERS + "/" + _ACCOUNT + "/x",
    "/d/" + _DEV + "/" + _REPOS + "/x",
)
CODEX_AUTH_ROOT = "C:/" + _USERS + "/" + _ACCOUNT + "/.codex"
LOCK_ROOT_LITERAL = '"C:\\XRML' + 'Data\\locks"'


class TestOwnerRootDetection:
    @pytest.mark.parametrize("literal", OWNER_SHAPES)
    def test_real_owner_roots_are_detected(self, literal: str) -> None:
        """Backslashes must not be over-escaped -- that bug hid every batch file."""
        assert pps.OWNER_ROOTS.search(literal), f"missed an owner root: {literal!r}"

    def test_this_fixture_does_not_ship_the_owner_layout(self) -> None:
        """The file proving detection must not itself be a disclosure.

        It is exempt from the scan (it has to be), so nothing else would catch a
        real root reappearing here in a future edit.
        """
        text = Path(__file__).read_text(encoding="utf-8")
        # The scanner's own judgment, not the raw pattern: a URL that happens to
        # contain `/Users/...` is not a machine path, and one below deliberately
        # proves exactly that.
        assert pps.scan_text("tests/test_public_path_scan.py", text) == [], (
            "an owner root was written into this fixture"
        )

    @pytest.mark.parametrize("literal", [
        r"C:\Users\EXAMPLE\example-repo",
        r"D:\Example Worktrees\sample-project",
        "<REPO_ROOT>/docs/x.md", "%LOCALAPPDATA%\\XRMLData", "${REPO_ROOT}/x",
        "$env:LOCALAPPDATA", "docs/OPERATIONS.md",
    ])
    def test_placeholders_and_synthetic_roots_are_not_leaks(self, literal: str) -> None:
        assert not pps.OWNER_ROOTS.search(literal) or pps.PLACEHOLDERS.search(literal)

    def test_web_urls_are_not_local_paths(self) -> None:
        findings = pps.scan_text("README.md", "see https://example.com/Users/USER/x for details")
        assert findings == []

    def test_the_scanner_does_not_report_its_own_patterns(self) -> None:
        text = (REPO / "tools" / "public_path_scan.py").read_text(encoding="utf-8")
        # Its own pattern strings match, of course; what matters is that none is
        # raised as an actionable leak against a public surface.
        assert [f for f in pps.scan_text(pps.SELF, text) if f["actionable"]] == []


class TestSurfaceClassification:
    @pytest.mark.parametrize("path,expected", [
        ("ops/bat/run_weekday_am.bat", "executable"),
        ("ops/bat/_repo_root.cmd", "executable"),
        ("scripts/run_all.bat", "executable"),
        ("scripts/run_gemini_schedule.ps1", "executable"),
        ("README.md", "current_doc"),
        ("docs/LICENSE_SCOPE.md", "current_doc"),
        ("ops/schedules/weekday-am.json", "config"),
        ("tests/test_x.py", "fixture"),
    ])
    def test_actionable_surfaces_are_recognized(self, path: str, expected: str) -> None:
        assert pps.surface_of(path) == expected

    @pytest.mark.parametrize("path", [
        "docs/m50-final-audit-handoff.md", "docs/dev-current-state.md",
        "docs/audit/M50_REPO_AUDIT.md",
        "docs/audit/L10E_LOCAL_DATA_OVERLAY_MANIFEST.lock.json",
        "design/stitch/mcp-visibility-report.json",
    ])
    def test_historical_records_are_deferred_not_actionable(self, path: str) -> None:
        """Counted for visibility; their disposition is a separate decision."""
        assert pps.surface_of(path) is None

    def test_deferred_findings_do_not_pass_as_resolved(self) -> None:
        findings = pps.scan_text("docs/dev-current-state.md", "ran in " + OWNER_SHAPES[0])
        assert findings and all(f["actionable"] is False for f in findings)
        assert findings[0]["surface"] == "deferred_history"


class TestActionableSurfacesAreClean:
    """The headline assertion: nothing executable, current-doc or config leaks."""

    def test_no_owner_root_on_any_actionable_surface(self) -> None:
        """Scans the working tree, so it guards the change before it is committed.

        The committed revision is scanned separately by the post-commit package
        check; a pre-commit control that read HEAD could only ever describe the
        state this change is trying to leave behind.
        """
        actionable = []
        for candidate in REPO.rglob("*"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(REPO).as_posix()
            if any(part in {".git", "node_modules", ".venv", "__pycache__"} for part in candidate.parts):
                continue
            if pps.surface_of(rel) is None:
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            actionable += [f for f in pps.scan_text(rel, text) if f["actionable"]]
        assert actionable == [], (
            "owner-machine paths reached a public surface: "
            + ", ".join(f"{f['path']}:{f['line']}" for f in actionable)
        )

    def test_readme_links_resolve(self) -> None:
        text = (REPO / "README.md").read_text(encoding="utf-8")
        missing = [t for t in re.findall(r"\]\((docs/[^)#]+)\)", text) if not (REPO / t).exists()]
        assert not missing, f"README points at missing files: {missing}"

    def test_the_leak_detector_fixture_stays_synthetic(self) -> None:
        """It must still trigger the product's detector without a real username."""
        text = (REPO / "tests" / "test_provider_report_integration.py").read_text(encoding="utf-8")
        assert "C:/Users/EXAMPLE/.codex/auth.json" in text
        assert CODEX_AUTH_ROOT not in text


class TestPortableEntrypoints:
    def test_batch_files_carry_no_machine_specific_root(self) -> None:
        for script in sorted((REPO / "ops" / "bat").glob("*")) + [REPO / "scripts" / "run_all.bat"]:
            body = script.read_text(encoding="utf-8", errors="replace")
            assert not pps.OWNER_ROOTS.search(body), f"{script.name} hardcodes a machine path"

    def test_batch_comments_avoid_percent_expansion(self) -> None:
        """cmd.exe parses a tilde expansion inside a comment and aborts the script."""
        for script in sorted((REPO / "ops" / "bat").glob("*")) + [REPO / "scripts" / "run_all.bat"]:
            for lineno, line in enumerate(script.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("::") or stripped.lower().startswith("rem "):
                    assert "%~" not in stripped, f"{script.name}:{lineno} percent expansion in a comment"

    def test_callers_check_the_root_helper_result(self) -> None:
        for name in ("run_weekday_am.bat", "run_weekday_pm.bat", "run_sunday_deep.bat"):
            body = (REPO / "ops" / "bat" / name).read_text(encoding="utf-8", errors="replace")
            assert "_repo_root.cmd" in body
            assert "|| exit /b 1" in body, f"{name} does not stop when root resolution fails"
            assert 'pushd "%REPO_ROOT%" || exit /b 1' in body, f"{name} ignores pushd failure"
            assert "EXIT_CODE" in body, f"{name} does not preserve the payload exit code"

    def test_run_all_uses_the_shared_helper_and_absolute_target(self) -> None:
        body = (REPO / "scripts" / "run_all.bat").read_text(encoding="utf-8", errors="replace")
        assert "_repo_root.cmd" in body
        assert "%REPO_ROOT%\\tools\\pipeline.py" in body, "still resolves the payload relative to CWD"
        commands = [ln.strip().lower() for ln in body.splitlines()
                    if not ln.strip().lower().startswith(("rem ", "::"))]
        assert "pause" not in commands, "a bare pause swallows the exit code in unattended use"

    def test_powershell_lock_root_is_not_hardcoded(self) -> None:
        body = (REPO / "scripts" / "run_gemini_schedule.ps1").read_text(encoding="utf-8")
        assert "Get-DefaultLockRoot" in body
        assert "XRML_SCHEDULE_RESULT_ROOT" in body
        assert LOCK_ROOT_LITERAL not in body


class TestNarrowInternalArtifactExclusion:
    def test_the_unreferenced_design_artifact_is_excluded(self) -> None:
        included, _excluded, _prohibited, _fatal = psd.classify(
            ["design/stitch/mcp-visibility-report.json", "app/main.py"]
        )
        internal = psd.internal_artifacts(["design/stitch/mcp-visibility-report.json", "app/main.py"])
        assert "design/stitch/mcp-visibility-report.json" not in included
        assert [p for p, _ in internal] == ["design/stitch/mcp-visibility-report.json"]
        assert "app/main.py" in included

    def test_the_exclusion_is_exact_not_a_directory_glob(self) -> None:
        """A broad `design/**` rule would drop genuine public design documentation."""
        included, _excluded, _prohibited, _fatal = psd.classify(
            ["design/stitch/other-design-doc.md", "design/architecture.md"]
        )
        internal = psd.internal_artifacts(["design/stitch/other-design-doc.md", "design/architecture.md"])
        assert not internal
        assert len(included) == 2

    def test_internal_artifacts_are_reported_separately_from_prohibited(self) -> None:
        paths = ["design/stitch/mcp-visibility-report.json", "storage/runs/x.json", ".env"]
        _inc, _exc, prohibited, fatal = psd.classify(paths)
        internal = psd.internal_artifacts(paths)
        assert [p for p, _ in internal] == ["design/stitch/mcp-visibility-report.json"]
        assert [p for p, _ in prohibited] == ["storage/runs/x.json"]
        assert [p for p, _ in fatal] == [".env"]

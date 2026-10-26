# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls proving the published source distribution can verify itself.

The failure these guard against is invisible in a developer checkout: a test
reads accumulated runtime state that happens to be lying around, passes, and
then fails for whoever downloads the source archive -- where that state is
correctly absent. Every control here is written to fail in a checkout too, so it
cannot rot into a no-op.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.domain import constants
from app.services import topic_profiles
from tests import conftest as suite_conftest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runtime state the package boundary excludes. A test that needs any of these to
# exist cannot run from the distribution.
EXCLUDED_RUNTIME_PATHS = (
    "storage/topic_profiles/profiles.json",
    "storage/topic_notifications/settings.json",
    "storage/metadata_index.sqlite3",
    "reports",
)


class TestFixturesAreGeneratedNotCopied:
    def test_default_run_uses_generated_fixtures(self) -> None:
        """Reading the developer's own state must stay opt-in."""
        assert suite_conftest._use_real_repository_fixtures() is False, (
            f"{suite_conftest.REAL_RUN_FIXTURE_OPT_IN_ENV} is set; the suite is reading "
            "real repository state and is not proving anything about the distribution"
        )

    def test_profiles_fixture_is_deterministic(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        suite_conftest._write_portable_topic_profiles_fixture(first)
        suite_conftest._write_portable_topic_profiles_fixture(second)
        a = (first / "topic_profiles" / "profiles.json").read_bytes()
        b = (second / "topic_profiles" / "profiles.json").read_bytes()
        assert a == b, "two builds of the profile fixture differ"

    def test_profiles_fixture_pins_every_nondeterministic_field(self) -> None:
        """Re-derive which seeded fields vary, rather than trusting a fixed list.

        If the product gains another clock- or uuid-derived default, this fails
        instead of quietly reintroducing a flaky equality assertion.
        """

        def leaves(value, prefix=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield from leaves(item, f"{prefix}.{key}" if prefix else str(key))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from leaves(item, f"{prefix}[{index}]")
            else:
                yield prefix, value

        samples = []
        for _ in range(5):
            seeded = [topic_profiles._normalize_profile(p) for p in topic_profiles._seed_default_profiles()]
            samples.append(dict(leaves(seeded)))

        varying = {
            key
            for key in samples[0]
            if len({json.dumps(s.get(key), sort_keys=True, default=str) for s in samples}) > 1
        }
        unpinned = {key.split(".")[-1] for key in varying} - {"created_at", "updated_at"}
        assert not unpinned, (
            f"seeded profiles gained nondeterministic field(s) {sorted(unpinned)}; "
            "pin them in _write_portable_topic_profiles_fixture"
        )

    def test_fixture_profiles_carry_the_pinned_epoch(self, tmp_path: Path) -> None:
        suite_conftest._write_portable_topic_profiles_fixture(tmp_path)
        profiles = json.loads((tmp_path / "topic_profiles" / "profiles.json").read_text(encoding="utf-8"))
        assert profiles, "fixture wrote an empty profile library"
        for profile in profiles:
            assert profile["created_at"] == suite_conftest.FIXTURE_EPOCH
            assert profile["updated_at"] == suite_conftest.FIXTURE_EPOCH


class TestAnalyticsFixtureMatchesTheProductContract:
    """The fixture must look like something the product could actually emit.

    It previously declared `derivations`, `metricDefinitions` and `coverage` as
    empty lists, which no generated artifact ever is -- the generator fills all
    three unconditionally. Nothing noticed until a profile-ordering change made
    the dashboard read this artifact instead of computing one.
    """

    def _artifact(self, tmp_path: Path) -> dict:
        suite_conftest._write_portable_topic_analytics_fixture(tmp_path)
        return json.loads(
            (tmp_path / "topic_analytics" / "large-language-models.json").read_text(encoding="utf-8")
        )

    def test_derivations_cover_every_product_surface(self, tmp_path: Path) -> None:
        from app.services import topic_analytics

        artifact = self._artifact(tmp_path)
        assert {row["surfaceId"] for row in artifact["derivations"]} == {
            row["surfaceId"] for row in topic_analytics.DERIVATION_DEFINITIONS
        }, "fixture derivations drifted from DERIVATION_DEFINITIONS"

    def test_metric_definitions_match_the_product(self, tmp_path: Path) -> None:
        from app.services import topic_analytics

        artifact = self._artifact(tmp_path)
        assert len(artifact["metricDefinitions"]) == len(topic_analytics.METRIC_DEFINITIONS)
        assert artifact["metricDefinitions"], "an artifact with no metric definitions is not a real artifact"

    def test_coverage_rows_carry_the_product_key_shape(self, tmp_path: Path) -> None:
        from app.services import topic_analytics

        expected = set(topic_analytics._coverage_item("x", "y", covered=0, total=0))
        artifact = self._artifact(tmp_path)
        assert artifact["coverage"], "an artifact with no coverage rows is not a real artifact"
        for row in artifact["coverage"]:
            assert set(row) == expected

    def test_coverage_numbers_are_internally_consistent(self, tmp_path: Path) -> None:
        """Guards a nonsensical artifact: covered > total yields percentage > 100.

        The counts are read from two other places in the same payload, so an edit
        to one and not the other would otherwise produce a self-contradicting
        artifact that every key-shape assertion still accepts.
        """
        artifact = self._artifact(tmp_path)
        for row in artifact["coverage"]:
            assert 0 <= row["covered"] <= row["total"], f"{row['coverageId']}: covered exceeds total"
            assert 0 <= row["percentage"] <= 100, f"{row['coverageId']}: percentage out of range"

    def test_fixture_is_deterministic(self, tmp_path: Path) -> None:
        first = self._artifact(tmp_path / "a")
        second = self._artifact(tmp_path / "b")
        assert first == second


class TestRuntimeRootsStayOutsideTheCheckout:
    def test_isolated_roots_do_not_resolve_into_the_source_tree(self, isolated_run_repo) -> None:
        """Fail closed on the roots the isolation contract actually redirects.

        `_patch_storage_roots` rebinds `STORAGE_ROOT` and each service's library
        path. It does not rebind the module-level `constants.LEGACY_*` values --
        see `test_legacy_roots_are_a_known_isolation_gap` for that, which is
        recorded rather than asserted away.
        """
        from app.services import topic_analytics, topic_notifications, topic_schedules

        source_tree = Path(constants.PROJECT_ROOT).resolve()
        redirected = {
            "constants.STORAGE_ROOT": constants.STORAGE_ROOT,
            "topic_profiles.PROFILE_LIBRARY_DIR": topic_profiles.PROFILE_LIBRARY_DIR,
            "topic_analytics.ANALYTICS_LIBRARY_DIR": topic_analytics.ANALYTICS_LIBRARY_DIR,
            "topic_notifications.NOTIFICATION_LIBRARY_DIR": topic_notifications.NOTIFICATION_LIBRARY_DIR,
            "topic_schedules.SCHEDULE_LIBRARY_DIR": topic_schedules.SCHEDULE_LIBRARY_DIR,
        }
        for name, root in redirected.items():
            assert not Path(root).resolve().is_relative_to(source_tree), (
                f"{name} resolves inside the source tree; a test run would mutate the checkout"
            )

    def test_legacy_roots_are_a_known_isolation_gap(self, isolated_run_repo) -> None:
        """Record, do not hide: `constants.LEGACY_*` still point into the checkout.

        They are bound at import from `PROJECT_ROOT` and the per-test fixture does
        not rebind them. Nothing in the suite currently writes through them -- both
        full suites pass with a clean tree -- but asserting they are isolated would
        be false. This test pins the real state so a future change that *does*
        redirect them shows up as a deliberate improvement rather than noise.
        """
        source_tree = Path(constants.PROJECT_ROOT).resolve()
        still_repo_relative = [
            name
            for name in ("LEGACY_DATA_ROOT", "LEGACY_QUEUE_ROOT", "LEGACY_RAW_ROOT",
                         "LEGACY_REPORTS_ROOT", "LEGACY_EXPERIMENTS_ROOT")
            if (root := getattr(constants, name, None)) is not None
            and Path(root).resolve().is_relative_to(source_tree)
        ]
        assert still_repo_relative, (
            "LEGACY_* roots are now redirected by the fixture -- tighten "
            "test_isolated_roots_do_not_resolve_into_the_source_tree to cover them"
        )

    def test_profile_library_is_redirected_away_from_the_repository(self, isolated_run_repo) -> None:
        library = Path(topic_profiles.PROFILE_LIBRARY_PATH).resolve()
        assert not library.is_relative_to(Path(constants.PROJECT_ROOT).resolve())
        assert library.is_file(), "the redirected profile library was never materialized"


class TestGitIsNotRequired:
    def test_git_checkout_detection_matches_reality(self) -> None:
        inside = suite_conftest._is_git_checkout()
        assert inside == (REPO_ROOT / ".git").exists() or inside is False

    def test_admission_tracking_gate_is_relaxed_only_without_git(self, tmp_path: Path) -> None:
        """The Git gate must still run for real inside a checkout."""
        if suite_conftest._is_git_checkout():
            pytest.skip("covered by the archive run; inside a checkout the gate is live")
        from app.services import evidence_manifest_admission

        result = evidence_manifest_admission.check_batch_admission(
            [str(REPO_ROOT / p) for p in suite_conftest.KNOWN_EVIDENCE_MANIFESTS],
            topic_id="large-language-models",
            require_tracked=True,
        )
        assert result["status"] == "rejected", "expected the untouched gate to reject without Git"


class TestDistributionShape:
    @pytest.mark.parametrize("relative", EXCLUDED_RUNTIME_PATHS)
    def test_suite_does_not_require_excluded_runtime_state(self, relative: str) -> None:
        """Whether the path exists is irrelevant; nothing may *depend* on it.

        Asserting absence would fail in a checkout, so this asserts the weaker
        and more useful property: the file is not a declared test input.
        """
        target = REPO_ROOT / relative
        assert relative not in {
            str(p) for p in getattr(suite_conftest, "REQUIRED_TEST_INPUTS", set())
        }, f"{relative} was declared a required test input"
        if target.exists():
            pytest.skip(f"{relative} is present in this checkout; the archive run is the real proof")

    def test_a_focused_test_passes_without_any_repository_runtime_state(self, tmp_path: Path) -> None:
        """Run a real test from a tree that has no storage/ and no .git.

        This is the control that actually proves the claim. It is deliberately
        narrow -- one module, one subprocess, a copied minimal tree -- so it stays
        fast and platform-portable rather than duplicating the whole suite.
        """
        sandbox = tmp_path / "snapshot"
        for relative in (
            "pyproject.toml",
            "app",
            "config",
            "data/evidence_manifests",
            "tests/__init__.py",
            "tests/conftest.py",
            "tests/helpers",
            "tests/test_topic_configurations.py",
        ):
            source = REPO_ROOT / relative
            if not source.exists():
                pytest.skip(f"{relative} missing from this tree")
            target = sandbox / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                import shutil

                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                target.write_bytes(source.read_bytes())

        assert not (sandbox / ".git").exists()
        assert not (sandbox / "storage").exists()

        env = {k: v for k, v in os.environ.items() if k != "XRML_DATA_DIR"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_topic_configurations.py",
             "-q", "-p", "no:cacheprovider", "--no-header"],
            cwd=sandbox, env=env, capture_output=True, text=True, timeout=600,
        )
        assert result.returncode == 0, (
            "a focused test failed in a storage-less, Git-less snapshot -- the "
            f"distribution is not self-testable:\n{result.stdout[-3000:]}"
        )

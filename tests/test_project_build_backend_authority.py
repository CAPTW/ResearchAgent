# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""The isolated project-build authority, and why it is separate from `uv.lock`.

`uv.lock` governs the runtime and dev environment. It does not govern the *build* environment:
uv resolves that fresh for every build, from `[build-system].requires`, and nothing it selects
is recorded as an ordinary lock package. An unpinned `requires = ["hatchling"]` therefore
floats silently -- two builds a month apart can use different backends and the lockfile will
not have moved a byte.

So the build environment gets its own committed authority: an exact `[build-system].requires`
plus the exact measured closure in `[tool.uv].build-constraint-dependencies`. The clearest
evidence that these really are two authorities is `packaging`: the build environment pins
26.3 while `uv.lock` pins 26.0. That split is intentional and this module fails closed if
anyone collapses it.

The ordinary lock records are held by a digest over the normalized package array only, so the
manifest's build-constraint table can change without weakening the guarantee that no runtime
package moved.

No Git, no network, no third-party import -- this runs inside an extracted source.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST_TEXT = (REPO / "pyproject.toml").read_text(encoding="utf-8")
LOCK_TEXT = (REPO / "uv.lock").read_text(encoding="utf-8")
DOCS = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
STATIC_SRC = (REPO / "tests" / "test_static_toolchain_ci_contract.py").read_text(encoding="utf-8")
VERIFIER_SRC = (REPO / "tests" / "test_mypy_baseline_verifier.py").read_text(encoding="utf-8")

BUILD_BACKEND = "hatchling.build"
BUILD_REQUIRES = ["hatchling==1.32.0"]
BUILD_CONSTRAINTS = [
    "editables==0.6",
    "hatchling==1.32.0",
    "packaging==26.3",
    "pathspec==1.1.1",
    "pluggy==1.6.0",
    "tomlkit==0.15.1",
    "trove-classifiers==2026.6.1.19",
]
BUILD_PACKAGE_NAMES = frozenset(c.split("==")[0] for c in BUILD_CONSTRAINTS)

# SHA-256 over the M1 lock's normalized `package` array only, excluding the top-level
# manifest metadata. The only authorized package-record expansion is the exact pinned
# desktop packager closure asserted below; runtime and dev dependency authority is unchanged.
LOCK_PACKAGE_DIGEST = "8f1915f2064f6d51a4ad10b6bb413403cc7fdf0916de51204350f95cb8768826"
LOCK_PACKAGE_COUNT = 46
BUILD_PACKAGING = "packaging==26.3"
LOCKED_PACKAGING = "26.0"

FULL_SYNC = "uv sync --frozen --all-extras --python 3.12.12"
RUNTIME_DEPENDENCIES = [
    "fastapi>=0.116,<1.0",
    "httpx>=0.28,<1.0",
    "itsdangerous>=2.2,<3.0",
    "PyJWT[crypto]>=2.10,<3.0",
    "reportlab>=4.4,<5.0",
    "uvicorn>=0.35,<1.0",
]
DEV_EXTRA = [
    "httpx>=0.28,<1.0",
    "pytest>=8.4,<9.0",
    "ruff>=0.14,<1.0",
    "mypy>=1.19,<2.0",
]
DESKTOP_EXTRA = ["pyinstaller==6.22.2"]
# Strict about meaning, permissive about form: single quotes, a trailing comma, uppercase hex
# or a full 64-character digest are all still whole-file pins and must not slip through.
PIN_RE = re.compile(r"""\(\s*["']([^"']+)["']\s*,\s*["'][0-9a-fA-F]{16,64}["']\s*,?\s*\)""")
DICT_PIN_RE = re.compile(r"""["']([^"']+)["']\s*:\s*["'][0-9a-fA-F]{16,64}["']""")


def normalize_name(requirement: str) -> str:
    return re.split(r"[=<>!~\[]", requirement, maxsplit=1)[0].strip().lower().replace("_", "-")


def lock_package_digest(packages: list[dict[str, Any]]) -> str:
    ordered = sorted(packages, key=lambda p: (p.get("name", ""), p.get("version", "")))
    canonical = json.dumps(ordered, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def whole_file_pins(source: str) -> set[str]:
    """Every path still held by a whole-file digest in a predecessor test.

    Swept over the entire file rather than a parsed parametrize block: block splitting is
    what a reintroduced pin would hide behind, and a false positive here is a loud test
    failure while a false negative is a silent loss of the contract.
    """
    return set(PIN_RE.findall(source)) | set(DICT_PIN_RE.findall(source))


def bundle() -> dict[str, Any]:
    return {
        "manifest": tomllib.loads(MANIFEST_TEXT),
        "lock": tomllib.loads(LOCK_TEXT),
        "docs": DOCS,
        "static_src": STATIC_SRC,
        "verifier_src": VERIFIER_SRC,
    }


def verify(data: dict[str, Any]) -> None:
    """The whole contract in one fail-closed pass. Every mutation below must break it."""
    manifest, lock = data["manifest"], data["lock"]
    project = manifest["project"]
    build = manifest["build-system"]
    constraints = manifest["tool"]["uv"]["build-constraint-dependencies"]

    # -- build authority ------------------------------------------------------------------
    assert build["build-backend"] == BUILD_BACKEND
    assert build["requires"] == BUILD_REQUIRES
    assert constraints == BUILD_CONSTRAINTS
    assert all("==" in c for c in constraints)
    assert all(not re.search(r"[<>~!*]|@|/", c) for c in constraints)
    names = [normalize_name(c) for c in constraints]
    assert len(names) == len(set(names))
    assert names == sorted(names)
    assert BUILD_PACKAGING in constraints

    hatchling_requirement = next(r for r in build["requires"] if normalize_name(r) == "hatchling")
    hatchling_constraint = next(c for c in constraints if normalize_name(c) == "hatchling")
    assert hatchling_requirement == hatchling_constraint

    # -- no leakage into ordinary dependency authority ------------------------------------
    assert project["dependencies"] == RUNTIME_DEPENDENCIES
    assert project["optional-dependencies"]["dev"] == DEV_EXTRA
    assert project["optional-dependencies"]["fulltext"] == ["pymupdf>=1.26,<2.0"]
    assert project["optional-dependencies"]["codex-sdk"] == ["openai-codex"]
    assert project["optional-dependencies"]["desktop"] == DESKTOP_EXTRA
    assert set(project["optional-dependencies"]) == {"fulltext", "codex-sdk", "dev", "desktop"}
    declared = project["dependencies"] + [
        r for group in project["optional-dependencies"].values() for r in group
    ]
    assert not BUILD_PACKAGE_NAMES & {normalize_name(r) for r in declared}
    assert project["requires-python"] == ">=3.11"
    assert project["name"] == "xrml-research-agent"
    assert project["version"] == "0.1.0"

    # -- Ruff and mypy contracts untouched ------------------------------------------------
    ruff = manifest["tool"]["ruff"]
    assert ruff["target-version"] == "py311"
    assert ruff["line-length"] == 120
    assert len(ruff["lint"]["select"]) == 58
    assert ruff["lint"]["ignore"] == ["E402"]
    mypy = manifest["tool"]["mypy"]
    assert mypy["python_version"] == "3.11"
    assert mypy["ignore_missing_imports"] is False
    assert "mypy_path" not in mypy
    assert len(mypy["overrides"]) == 1
    assert mypy["overrides"][0]["ignore_missing_imports"] is True
    assert mypy["overrides"][0]["module"][0] == "reportlab"

    # -- ordinary lock package records unmoved --------------------------------------------
    packages = lock["package"]
    assert len(packages) == LOCK_PACKAGE_COUNT
    assert lock_package_digest(packages) == LOCK_PACKAGE_DIGEST
    by_name = {p["name"]: p for p in packages}
    assert "hatchling" not in by_name
    assert by_name["packaging"]["version"] == LOCKED_PACKAGING

    # -- contributor contract --------------------------------------------------------------
    docs = data["docs"]
    assert FULL_SYNC in docs
    assert "`--no-install-project` is not an accepted project environment" in docs
    assert "--no-build-isolation" in docs
    assert "`packaging==26.3`" in docs and "`packaging==26.0`" in docs
    assert "uv pip" in docs

    # -- predecessor pins retired, not re-pinned -------------------------------------------
    for source in (data["static_src"], data["verifier_src"]):
        pins = whole_file_pins(source)
        assert "pyproject.toml" not in pins
        assert "uv.lock" not in pins


@pytest.fixture()
def data() -> dict[str, Any]:
    return bundle()


class TestBuildAuthorityIsCommitted:
    def test_the_contract_holds_on_the_committed_tree(self, data: dict[str, Any]) -> None:
        verify(data)

    def test_the_backend_is_hatchling(self, data: dict[str, Any]) -> None:
        assert data["manifest"]["build-system"]["build-backend"] == BUILD_BACKEND

    def test_the_build_requirement_is_exactly_pinned(self, data: dict[str, Any]) -> None:
        assert data["manifest"]["build-system"]["requires"] == ["hatchling==1.32.0"]

    def test_the_constraint_list_is_the_measured_closure(self, data: dict[str, Any]) -> None:
        assert data["manifest"]["tool"]["uv"]["build-constraint-dependencies"] == BUILD_CONSTRAINTS

    def test_every_constraint_is_an_exact_equality_pin(self, data: dict[str, Any]) -> None:
        for constraint in data["manifest"]["tool"]["uv"]["build-constraint-dependencies"]:
            assert "==" in constraint
            assert not re.search(r"[<>~!*]|@|/", constraint)

    def test_normalized_constraint_names_are_unique_and_ordered(self, data: dict[str, Any]) -> None:
        names = [normalize_name(c)
                 for c in data["manifest"]["tool"]["uv"]["build-constraint-dependencies"]]
        assert len(names) == len(set(names)) == 7
        assert names == sorted(names)

    def test_the_two_hatchling_statements_agree(self, data: dict[str, Any]) -> None:
        manifest = data["manifest"]
        requirement = next(r for r in manifest["build-system"]["requires"]
                           if normalize_name(r) == "hatchling")
        constraint = next(c for c in manifest["tool"]["uv"]["build-constraint-dependencies"]
                          if normalize_name(c) == "hatchling")
        assert requirement == constraint == "hatchling==1.32.0"

    def test_the_editable_hook_dependency_is_represented(self, data: dict[str, Any]) -> None:
        """`editables` is only requested on the hook's second resolution pass -- easy to miss."""
        assert "editables==0.6" in data["manifest"]["tool"]["uv"]["build-constraint-dependencies"]


class TestTheTwoAuthoritiesStaySeparate:
    def test_build_packaging_is_pinned_only_in_build_authority(self, data: dict[str, Any]) -> None:
        assert BUILD_PACKAGING in data["manifest"]["tool"]["uv"]["build-constraint-dependencies"]
        assert BUILD_PACKAGING not in data["manifest"]["project"]["dependencies"]

    def test_locked_packaging_is_untouched(self, data: dict[str, Any]) -> None:
        record = next(p for p in data["lock"]["package"] if p["name"] == "packaging")
        assert record["version"] == LOCKED_PACKAGING

    def test_hatchling_has_no_ordinary_lock_record(self, data: dict[str, Any]) -> None:
        assert not [p for p in data["lock"]["package"] if p["name"] == "hatchling"]

    def test_no_build_package_entered_runtime_dependencies(self, data: dict[str, Any]) -> None:
        declared = {normalize_name(r) for r in data["manifest"]["project"]["dependencies"]}
        assert not BUILD_PACKAGE_NAMES & declared

    def test_no_build_package_entered_the_dev_extra(self, data: dict[str, Any]) -> None:
        dev = {normalize_name(r)
               for r in data["manifest"]["project"]["optional-dependencies"]["dev"]}
        assert not BUILD_PACKAGE_NAMES & dev

    def test_runtime_and_optional_dependencies_are_unchanged(self, data: dict[str, Any]) -> None:
        project = data["manifest"]["project"]
        assert project["dependencies"] == RUNTIME_DEPENDENCIES
        assert project["optional-dependencies"]["dev"] == DEV_EXTRA
        assert project["optional-dependencies"]["fulltext"] == ["pymupdf>=1.26,<2.0"]
        assert project["optional-dependencies"]["codex-sdk"] == ["openai-codex"]
        assert project["optional-dependencies"]["desktop"] == DESKTOP_EXTRA
        assert set(project["optional-dependencies"]) == {"fulltext", "codex-sdk", "dev", "desktop"}

    def test_requires_python_is_unchanged(self, data: dict[str, Any]) -> None:
        assert data["manifest"]["project"]["requires-python"] == ">=3.11"


class TestOrdinaryLockRecordsAreUnmoved:
    def test_the_package_record_count_is_unchanged(self, data: dict[str, Any]) -> None:
        assert len(data["lock"]["package"]) == LOCK_PACKAGE_COUNT

    def test_the_normalized_package_digest_is_unchanged(self, data: dict[str, Any]) -> None:
        assert lock_package_digest(data["lock"]["package"]) == LOCK_PACKAGE_DIGEST

    @pytest.mark.parametrize("name,version", [
        ("pytest", "8.4.2"), ("ruff", "0.16.2"), ("mypy", "1.20.2"),
    ])
    def test_the_locked_static_tools_are_unchanged(
        self, data: dict[str, Any], name: str, version: str
    ) -> None:
        record = next(p for p in data["lock"]["package"] if p["name"] == name)
        assert record["version"] == version

    def test_the_build_constraints_are_recorded_in_the_lock_manifest(
        self, data: dict[str, Any]
    ) -> None:
        recorded = data["lock"]["manifest"]["build-constraints"]
        assert [f"{c['name']}{c['specifier']}" for c in recorded] == BUILD_CONSTRAINTS


class TestStaticContractsAreUnchanged:
    def test_the_ruff_configuration_is_unchanged(self, data: dict[str, Any]) -> None:
        ruff = data["manifest"]["tool"]["ruff"]
        assert ruff["target-version"] == "py311"
        assert len(ruff["lint"]["select"]) == 58
        assert ruff["lint"]["ignore"] == ["E402"]

    def test_the_mypy_configuration_is_unchanged(self, data: dict[str, Any]) -> None:
        mypy = data["manifest"]["tool"]["mypy"]
        assert mypy["python_version"] == "3.11"
        assert mypy["ignore_missing_imports"] is False
        assert "mypy_path" not in mypy

    def test_the_reportlab_override_is_unchanged(self, data: dict[str, Any]) -> None:
        overrides = data["manifest"]["tool"]["mypy"]["overrides"]
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

    def test_the_mypy_baseline_is_byte_identical(self) -> None:
        digest = hashlib.sha256((REPO / "tools" / "mypy_baseline.json").read_bytes()).hexdigest()
        assert digest == "a00f277bbb09c735ff6f800b6f3b44b6a121be107dd56b45734f2df3c93590dd"

    def test_the_verifier_tool_is_byte_identical(self) -> None:
        digest = hashlib.sha256((REPO / "tools" / "check_mypy_baseline.py").read_bytes()).hexdigest()
        assert digest == "1886da52ca00d6a8e92be86f8e606c1187fc87a01f6e9bfb0c7ea5e6035b27a5"


class TestContributorContract:
    def test_the_full_sync_command_is_exact(self) -> None:
        assert FULL_SYNC in DOCS

    def test_no_install_project_is_not_endorsed(self) -> None:
        assert "`--no-install-project` is not an accepted project environment" in DOCS

    def test_no_build_isolation_is_prohibited(self) -> None:
        prohibited = DOCS.split("### Prohibited for this contract")[1]
        assert "--no-build-isolation" in prohibited.split("###")[0]

    def test_the_two_packaging_authorities_are_explained(self) -> None:
        assert "`packaging==26.3`" in DOCS
        assert "`packaging==26.0`" in DOCS
        assert "Do not\ncollapse them into one." in DOCS

    def test_hatchling_absence_from_the_lock_is_explained_truthfully(self) -> None:
        assert "Hatchling can legitimately be absent from `uv.lock`" in DOCS

    def test_pip_and_uv_pip_are_prohibited(self) -> None:
        prohibited = DOCS.split("### Prohibited for this contract")[1].split("###")[0]
        assert "`pip` and `uv pip`" in prohibited

    def test_the_claim_boundary_is_stated(self) -> None:
        assert "not release, staging or production" in DOCS


PREDECESSORS = {
    "test_static_toolchain_ci_contract.py": STATIC_SRC,
    "test_mypy_baseline_verifier.py": VERIFIER_SRC,
}


class TestObsoletePinsAreRetired:
    @pytest.mark.parametrize("label", sorted(PREDECESSORS))
    def test_no_whole_file_manifest_pin_remains(self, label: str) -> None:
        pins = whole_file_pins(PREDECESSORS[label])
        assert "pyproject.toml" not in pins, label
        assert "uv.lock" not in pins, label

    @pytest.mark.parametrize("label", sorted(PREDECESSORS))
    def test_no_test_was_neutralized_instead_of_retargeted(self, label: str) -> None:
        source = PREDECESSORS[label]
        assert "pytest.mark.skip" not in source
        assert "pytest.mark.xfail" not in source


MUTATIONS: list[tuple[str, Any]] = [
    ("unpin_hatchling",
     lambda d: d["manifest"]["build-system"].__setitem__("requires", ["hatchling"])),
    ("widen_hatchling",
     lambda d: d["manifest"]["build-system"].__setitem__("requires", ["hatchling>=1.32.0"])),
    ("change_hatchling_constraint_only",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].__setitem__(
         1, "hatchling==1.31.0")),
    ("remove_editables",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].remove(
         "editables==0.6")),
    ("remove_tomlkit",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].remove(
         "tomlkit==0.15.1")),
    ("collapse_build_packaging_to_lock_version",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].__setitem__(
         2, "packaging==26.0")),
    ("duplicate_normalized_name",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].append(
         "Trove_Classifiers==2026.6.1.19")),
    ("reorder_constraints",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].reverse()),
    ("add_unrelated_build_constraint",
     lambda d: d["manifest"]["tool"]["uv"]["build-constraint-dependencies"].append(
         "setuptools==80.0.0")),
    ("hatchling_into_runtime_dependencies",
     lambda d: d["manifest"]["project"]["dependencies"].append("hatchling==1.32.0")),
    ("hatchling_into_dev_extra",
     lambda d: d["manifest"]["project"]["optional-dependencies"]["dev"].append(
         "hatchling==1.32.0")),
    ("document_no_build_isolation_as_accepted",
     lambda d: d.__setitem__("docs", d["docs"].replace("--no-build-isolation", "isolation"))),
    ("endorse_no_install_project",
     lambda d: d.__setitem__("docs", d["docs"].replace(
         "`--no-install-project` is not an accepted project environment", "use it freely"))),
    ("reintroduce_pyproject_pin_in_static_contract",
     lambda d: d.__setitem__("static_src", d["static_src"].replace(
         '        ("tools/public_path_scan.py", "0ff9a0cd21521acb"),',
         '        ("pyproject.toml", "5b9c2854c63a4ce3"),\n'
         '        ("tools/public_path_scan.py", "0ff9a0cd21521acb"),'))),
    ("reintroduce_lock_pin_in_verifier_contract",
     lambda d: d.__setitem__("verifier_src", d["verifier_src"].replace(
         '        ("tools/public_path_scan.py", "0ff9a0cd21521acb"),',
         '        ("uv.lock", "1bed4e1199bc7013"),\n'
         '        ("tools/public_path_scan.py", "0ff9a0cd21521acb"),'))),
    ("alter_one_ordinary_lock_record",
     lambda d: next(p for p in d["lock"]["package"]
                    if p["name"] == "pytest").__setitem__("version", "8.4.1")),
    ("add_one_ordinary_lock_record",
     lambda d: d["lock"]["package"].append({"name": "hatchling", "version": "1.32.0"})),
]


class TestNegativeMutations:
    @pytest.mark.parametrize("name,mutate", MUTATIONS, ids=[m[0] for m in MUTATIONS])
    def test_the_mutation_fails_the_contract(self, name: str, mutate: Any) -> None:
        mutated = copy.deepcopy(bundle())
        mutate(mutated)
        with pytest.raises(AssertionError):
            verify(mutated)

    def test_the_mutation_matrix_is_complete(self) -> None:
        assert len(MUTATIONS) == 17
        assert len({m[0] for m in MUTATIONS}) == 17

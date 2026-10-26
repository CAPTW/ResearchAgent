# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""The license must say the same thing everywhere it is stated.

Metadata drift is the realistic failure here: someone changes `pyproject.toml`
and the LICENSE file, package.json, the packaging manifest and the SPDX headers
quietly disagree. Each assertion below pins one place the expression appears.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPDX = "Apache-2.0"
COPYRIGHT = "Copyright 2026 CAPTW"

# The canonical Apache-2.0 text, verified byte-for-byte against official SPDX
# license-list data. If this changes, the license text was edited -- which is not
# something to do accidentally.
LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"


class TestLicenseFile:
    def test_license_exists_and_is_unmodified_apache_2_0(self) -> None:
        body = (REPO / "LICENSE").read_bytes()
        assert hashlib.sha256(body).hexdigest() == LICENSE_SHA256, (
            "LICENSE no longer matches the canonical Apache-2.0 text"
        )

    def test_license_carries_every_operative_section(self) -> None:
        text = (REPO / "LICENSE").read_text(encoding="utf-8")
        for section in (
            "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
            "1. Definitions.",
            "2. Grant of Copyright License.",
            "3. Grant of Patent License.",
            "4. Redistribution.",
            "5. Submission of Contributions.",
            "7. Disclaimer of Warranty.",
            "8. Limitation of Liability.",
            "END OF TERMS AND CONDITIONS",
        ):
            assert section in text, f"LICENSE is missing {section!r}"

    def test_no_notice_file_is_present(self) -> None:
        """Apache-2.0 only requires propagating a NOTICE that exists.

        The owner chose not to have one. An empty or invented NOTICE would assert
        attribution obligations this project does not carry.
        """
        assert not (REPO / "NOTICE").exists()
        assert not (REPO / "NOTICE.txt").exists()


class TestMetadataConsistency:
    def test_pyproject_declares_the_spdx_expression(self) -> None:
        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        assert project["license"] == SPDX
        assert "LICENSE" in project["license-files"]

    def test_pyproject_has_no_license_classifier(self) -> None:
        """PEP 639 forbids pairing an SPDX expression with a License:: classifier."""
        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        assert not [c for c in project["classifiers"] if c.startswith("License ::")]

    def test_accidental_publication_guards_remain(self) -> None:
        """Nothing in this lane authorized publishing; the guards must survive it."""
        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        assert "Private :: Do Not Upload" in project["classifiers"]
        web = json.loads((REPO / "web" / "package.json").read_text(encoding="utf-8"))
        assert web["private"] is True

    def test_web_package_declares_the_same_expression(self) -> None:
        web = json.loads((REPO / "web" / "package.json").read_text(encoding="utf-8"))
        assert web["license"] == SPDX

    def test_packaging_tool_records_the_same_expression(self) -> None:
        from tools import package_source_distribution as psd

        assert psd.PROJECT_LICENSE == SPDX
        assert psd.PROJECT_COPYRIGHT == COPYRIGHT
        assert "LICENSE" in psd.REQUIRED_PATHS

    def test_readme_states_the_license(self) -> None:
        text = (REPO / "README.md").read_text(encoding="utf-8")
        assert SPDX in text and "LICENSE" in text


class TestHeaderPolicy:
    """Bounded policy: headers on files this project chose to head, no retrofit."""

    def test_headers_that_exist_are_correct(self) -> None:
        wrong: list[str] = []
        for path in REPO.rglob("*.py"):
            if any(p in path.parts for p in (".venv", "node_modules", "__pycache__", ".git")):
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
            match = re.search(r"SPDX-License-Identifier:\s*(\S+)", head)
            if match and match.group(1) != SPDX:
                wrong.append(f"{path.relative_to(REPO)} -> {match.group(1)}")
        assert not wrong, f"SPDX headers disagree with the project license: {wrong}"

    @pytest.mark.parametrize(
        "rel",
        [
            "tools/package_source_distribution.py",
            "tools/generate_dependency_licenses.py",
            "tests/test_render_deploy_config.py",
            "tests/test_license_metadata.py",
        ],
    )
    def test_files_touched_under_the_policy_carry_a_header(self, rel: str) -> None:
        head = (REPO / rel).read_text(encoding="utf-8")[:400]
        assert f"SPDX-License-Identifier: {SPDX}" in head
        assert COPYRIGHT in head

    def test_headers_are_not_added_to_formats_that_cannot_carry_them(self) -> None:
        """A '#' line in JSON is a syntax error, not a license header."""
        for rel in ("web/package.json", "docs/dependency_licenses.json", "web/package-lock.json"):
            body = (REPO / rel).read_text(encoding="utf-8")
            assert not body.lstrip().startswith("#")
            json.loads(body)  # still parses


class TestLicenseScopeDocumented:
    def test_scope_document_names_what_is_excluded(self) -> None:
        text = (REPO / "docs" / "LICENSE_SCOPE.md").read_text(encoding="utf-8")
        for claim in ("PyMuPDF", "AFFERO", "not installed by default",
                      "Runtime data", "Generated output", "not legal advice"):
            assert claim.lower() in text.lower(), f"license scope does not address {claim!r}"

    def test_third_party_notices_state_no_bundling(self) -> None:
        text = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        assert "does not bundle" in text.lower()
        assert "not legal advice" in text.lower()

    def test_inventory_is_machine_readable_and_flags_nothing_unresolved(self) -> None:
        data = json.loads((REPO / "docs" / "dependency_licenses.json").read_text(encoding="utf-8"))
        assert data["bundles_dependency_code"] is False
        unresolved = [d["name"] for d in data["dependencies"] if d["unresolved"]]
        assert not unresolved, f"dependencies without evidenced licenses: {unresolved}"

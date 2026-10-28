# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the fail-closed public document boundary.

The defect these replace was quiet rather than dramatic: the scanner judged six
hardcoded document names and the packager judged none, so a document nobody had
thought about was public by default. Nothing failed -- it simply shipped.

So the assertions here are mostly about *absence* working correctly: that an
unlisted document stops a build, that a stale entry stops a build, and that the
old six-name symbol can no longer decide anything on its own.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath

import pytest

from tools import package_source_distribution as psd
from tests.source_tree_context import inspect_source_tree
from tools import public_content_policy as pcp
from tools import public_path_scan as pps
from tests.history_contract_baseline import retained_document_bytes_match

REPO = Path(__file__).resolve().parents[1]
THREE_NAMED = (
    "docs/LOCAL_AGENT_RUNTIME.md",
    "docs/SCHEDULED_LOCAL_RESEARCH.md",
    "docs/STORAGE_PORTABILITY.md",
)
THREE_NAMED_BASE_COMMIT = "a24951827095cd7bbd1db4c8fd38f997049df8c1"

BASELINE_PUBLIC_DOCUMENTS_AT_635380C7 = frozenset({
    ".agents/skills/local-research-run/SKILL.md",
    ".claude/rules/local-only.md",
    ".claude/rules/reporting.md",
    ".claude/skills/local-research-run/SKILL.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "GEMINI.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "design/stitch/manual/component-hierarchy.md",
    "design/stitch/manual/design-dna.md",
    "design/stitch/manual/implementation-notes.md",
    "docs/AGENT_ADAPTERS.md",
    "docs/DEPLOYMENT_RUNBOOK.md",
    "docs/ENVIRONMENT_REFERENCE.md",
    "docs/LICENSE_SCOPE.md",
    "docs/LOCAL_RESEARCH_WORKFLOW.md",
    "docs/OPEN_SOURCE_READINESS.md",
    "docs/OPERATIONS.md",
    "docs/PACKAGING_AND_INSTALLATION.md",
    "docs/PRODUCTION_ACTIVATION.md",
    "docs/PRODUCTION_ENVIRONMENT.md",
    "docs/adr/0001-source-identity-and-current-authority.md",
    "docs/dependency_licenses.json",
    "templates/paper_summary_template.md",
})

APPROVED_M0_PUBLIC_REQUIRED_ADDITIONS = frozenset({
    "docs/adr/0002-current-v2-native-research-workspace-reuse-first-migration.md",
    "docs/architecture/current-v2-research-workspace-rebaseline.md",
    "docs/architecture/reuse-and-legacy-disposition-current-v2.md",
    "docs/architecture/target-desktop-architecture-research-memory-and-large-corpus.md",
    "docs/architecture/ui-to-domain-mapping-current-v2.md",
    "docs/roadmap/m0-to-m5-native-research-workspace.md",
})

M0_PUBLIC_REQUIRED_BASELINE = (
    BASELINE_PUBLIC_DOCUMENTS_AT_635380C7 | APPROVED_M0_PUBLIC_REQUIRED_ADDITIONS
)
M1_PUBLIC_REQUIRED_PATH = "docs/architecture/m1-native-research-workspace-foundation.md"
M1_PUBLIC_REQUIRED_REASON = (
    "m1_native_research_workspace_foundation_implemented_and_locally_qualified"
)
M1_PUBLIC_REQUIRED_ADDITION = frozenset({M1_PUBLIC_REQUIRED_PATH})
CURRENT_PRODUCT_GUIDES = frozenset({"docs/BROWSER_COLLECTION.md", "docs/MODEL_CONNECTIONS.md"})
FINAL_PUBLIC_REQUIRED_SET = M0_PUBLIC_REQUIRED_BASELINE | M1_PUBLIC_REQUIRED_ADDITION | CURRENT_PRODUCT_GUIDES


def _assert_exact_final_public_documents(paths: set[str] | frozenset[str]) -> None:
    assert frozenset(paths) == FINAL_PUBLIC_REQUIRED_SET, (
        "public documents differ from the individually reviewed current publication set"
    )


def _documents_block() -> str:
    """The raw `documents` object, so duplicate keys survive to be counted."""
    raw = (REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8")
    return re.search(r'"documents": \{(.*?)\n  \}', raw, re.DOTALL).group(1)


@pytest.fixture(scope="module")
def policy() -> pcp.ContentPolicy:
    return pcp.load()


@pytest.fixture(scope="module", autouse=True)
def source_context(policy):
    return inspect_source_tree(REPO, policy)


class TestPolicySchemaAndInventory:
    def test_the_policy_parses_and_declares_a_supported_version(self, policy) -> None:
        assert policy.version in pcp.SUPPORTED_VERSIONS
        assert policy.documents

    def test_every_role_used_is_one_of_the_eight(self, policy) -> None:
        assert set(policy.documents.values()) <= set(pcp.ROLES)

    def test_every_tracked_document_has_exactly_one_role(self, policy, source_context) -> None:
        paths = list(source_context.paths)
        assert policy.unclassified(paths) == []
        # A dict cannot hold one key twice, so "exactly one" is structural; what
        # needs asserting is that the inventory covers the tree and nothing else.
        source_context.validate_paths(paths)

    def test_no_path_appears_under_two_roles(self, policy) -> None:
        """`json.loads` would silently keep the last of two duplicate keys."""
        listed = _documents_block()
        paths = re.findall(r'"([^"]+)":', listed)
        assert len(paths) == len(set(paths)), "a path is listed twice in the raw JSON"
        assert len(paths) == len(policy.documents)

    @pytest.mark.parametrize("path", [
        "docs/x.md", "docs/audit/x.json", "docs/a/b/c.bin", "docs/nested/note.txt",
        "Docs/x.json", "DOCS/AUDIT/x.json",          # one directory on Windows
        "README.md", "README.MD", "LICENSE", "notes.txt",
        "design/new-design-note.md", "web/README.md",
    ])
    def test_documentation_shaped_paths_are_governed(self, path: str, policy) -> None:
        """Anything this misses would ship without ever being classified."""
        assert policy.is_document(path), f"{path} would escape the document policy"

    @pytest.mark.parametrize("path", [
        "app/main.py", "web/src/App.tsx", "pyproject.toml", "uv.lock", "docs2/x.json",
    ])
    def test_ordinary_source_is_not_swept_up(self, path: str, policy) -> None:
        assert not policy.is_document(path)

    def test_ordering_is_deterministic(self) -> None:
        raw = (REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8")
        listed = re.search(r'"documents": \{(.*?)\n  \}', raw, re.DOTALL).group(1)
        paths = re.findall(r'"([^"]+)":', listed)
        assert paths == sorted(paths), "document inventory is not sorted"

    def test_the_policy_holds_no_absolute_or_machine_path(self, policy) -> None:
        for path in policy.documents:
            assert not path.startswith("/") and ":" not in path[:2]
            assert not pps.OWNER_ROOTS.search(path)

    def test_it_loads_the_same_from_any_working_directory(self, tmp_path, policy) -> None:
        """Checkout-independence: the loader resolves beside its own module."""
        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            assert pcp.load().documents == policy.documents
        finally:
            os.chdir(cwd)


class TestFailsClosed:
    """The headline behaviour: an unclassified document stops the build."""

    @pytest.mark.parametrize("newcomer", [
        "docs/new-public-guide.md",
        "docs/audit/new-internal-record.md",
        "NEW_ROOT_NOTE.md",
        "design/new-design-note.md",
        "docs/nested/deep/new.txt",
    ])
    def test_an_unlisted_document_is_rejected(self, newcomer: str, policy, source_context) -> None:
        paths = list(source_context.paths) + [newcomer]
        assert policy.unclassified(paths) == [newcomer]
        with pytest.raises(pcp.PolicyError, match="no role"):
            source_context.validate_paths(paths)

    def test_a_stale_entry_is_rejected(self, policy, source_context) -> None:
        survivors = [p for p in list(source_context.paths) if p != "README.md"]
        with pytest.raises(pcp.PolicyError, match="no longer exists"):
            source_context.validate_paths(survivors)

    def test_a_new_non_document_keeps_the_existing_classifier(self, policy, source_context) -> None:
        """Only documents are policed here; source files are not swept up."""
        paths = list(source_context.paths) + ["app/services/brand_new_module.py"]
        assert policy.unclassified(paths) == []
        source_context.validate_paths(paths)

    def test_the_build_refuses_an_unlisted_document(self, monkeypatch, tmp_path) -> None:
        """A policy rejection must reach the caller as a build failure, not a warning."""
        from tests.test_source_distribution_packaging import _MINIMAL_TRACKED, _install_fake_source

        # Installed first: the fake source layer supplies its own policy, and this
        # must override it rather than the other way round.
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, repo=None)

        def refuse(tracked, policy=None):
            raise pcp.PolicyError("1 tracked document(s) have no role: docs/unlisted.md")

        monkeypatch.setattr(psd.public_content_policy, "validate_against", refuse)
        with pytest.raises(psd.PackagingError, match="no role"):
            psd.build(tmp_path, None, check_only=True)

    @pytest.mark.parametrize("mutation,expected", [
        ({"schema": "wrong"}, "schema"),
        ({"version": "9.9.9"}, "version"),
        ({"documents": {"README.md": "NOT_A_ROLE"}}, "unknown roles"),
        ({"documents": {}}, "no document inventory"),
        ({"documents": {"README.md": "PUBLIC_REQUIRED", "readme.MD": "INTERNAL_HISTORICAL"}},
         "case-folded"),
        ({"documents": {"/etc/passwd.md": "PUBLIC_REQUIRED"}}, "absolute"),
    ])
    def test_a_malformed_policy_is_rejected(self, tmp_path, mutation, expected) -> None:
        raw = json.loads((REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8"))
        raw.update(mutation)
        candidate = tmp_path / "policy.json"
        candidate.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(pcp.PolicyError, match=expected):
            pcp.load(candidate)


class TestOneAuthority:
    def test_the_old_hardcoded_set_is_now_derived(self, policy) -> None:
        assert pps.CURRENT_PUBLIC_DOCS == policy.public_documents()

    def test_mutating_the_old_view_cannot_change_behaviour(self, monkeypatch, policy) -> None:
        """It is a view now, not a decision. Reassigning it must do nothing."""
        monkeypatch.setattr(pps, "CURRENT_PUBLIC_DOCS", frozenset({"docs/M3_WORKBENCH.md"}))
        assert pps.surface_of("docs/M3_WORKBENCH.md") is None
        assert pps.surface_of("README.md") == "current_doc"

    def test_scanner_and_packager_agree_on_the_public_set(self, policy, source_context) -> None:
        scanner_public = {p for p in list(source_context.paths) if pps.surface_of(p) == "current_doc"}
        packager_public = {p for p in policy.public_documents() if p in set(list(source_context.paths))}
        # The scanner defers historical records before consulting the policy, so it
        # can only ever be a subset; what must never happen is the reverse.
        assert scanner_public <= packager_public
        assert packager_public - scanner_public == set(), "a public doc the scanner never judges"

    def test_there_is_exactly_one_policy_file(self) -> None:
        found = sorted(p.name for p in (REPO / "tools").glob("*content_policy*"))
        assert found == ["public_content_policy.json", "public_content_policy.py"]


class TestConservativeRoles:
    @pytest.mark.parametrize("path", ["README.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md",
                                      "THIRD_PARTY_NOTICES.md", "docs/LICENSE_SCOPE.md",
                                      "docs/OPEN_SOURCE_READINESS.md"])
    def test_required_documents_are_public(self, path: str, policy) -> None:
        assert policy.role_of(path) == "PUBLIC_REQUIRED"

    def test_every_required_package_path_still_ships(self, policy) -> None:
        for path in psd.REQUIRED_PATHS:
            if policy.is_document(path):
                assert policy.role_of(path) in pcp.SHIPPING_ROLES, path

    def test_audit_records_are_internal_historical(self, policy) -> None:
        audit = [p for p in policy.documents if p.startswith("docs/audit/")]
        assert audit, "expected tracked audit records"
        assert {policy.role_of(p) for p in audit} == {"INTERNAL_HISTORICAL"}

    def test_milestone_records_are_internal_historical(self, policy) -> None:
        milestones = [p for p in policy.documents if re.match(r"^docs/M\d", p)]
        assert len(milestones) >= 20
        assert {policy.role_of(p) for p in milestones} == {"INTERNAL_HISTORICAL"}

    def test_public_curated_is_empty_in_this_skeleton(self, policy) -> None:
        """Promotion is the next lane's job and needs individual review first."""
        assert [p for p, r in policy.documents.items() if r == "PUBLIC_CURATED"] == []

    def test_the_public_set_is_exactly_the_approved_m0_set(self, policy) -> None:
        """M1 adds one named document to M0; it does not raise a generic cap."""
        _assert_exact_final_public_documents(policy.public_documents())
        assert len(M0_PUBLIC_REQUIRED_BASELINE) == 34
        assert M1_PUBLIC_REQUIRED_ADDITION == frozenset({M1_PUBLIC_REQUIRED_PATH})
        assert CURRENT_PRODUCT_GUIDES <= policy.public_documents()
        assert policy.role_of(M1_PUBLIC_REQUIRED_PATH) == "PUBLIC_REQUIRED"
        assert policy.dispositions[M1_PUBLIC_REQUIRED_PATH] == "public_required_final"
        assert policy.reason_codes[M1_PUBLIC_REQUIRED_PATH] == M1_PUBLIC_REQUIRED_REASON

    def test_a_seventh_m0_public_addition_is_rejected(self, policy) -> None:
        unapproved = set(policy.public_documents()) | {"docs/unapproved-second-m1-note.md"}
        with pytest.raises(AssertionError, match="individually reviewed current publication set"):
            _assert_exact_final_public_documents(unapproved)


class TestThreeNamedDocuments:
    @pytest.mark.parametrize("path", THREE_NAMED)
    def test_they_are_internal_historical(self, path: str, policy) -> None:
        assert policy.role_of(path) == "INTERNAL_HISTORICAL"

    @pytest.mark.parametrize("path", THREE_NAMED)
    def test_their_retained_contracts_or_public_exclusion(self, path: str, source_context) -> None:
        """Excluded from the snapshot, not removed from the repository."""
        if source_context.is_public:
            source_context.assert_excluded([path])
            return
        assert (REPO / path).exists()
        assert retained_document_bytes_match(REPO, THREE_NAMED_BASE_COMMIT, path), (
            f"{path} differs from its explicitly retained contract"
        )

    @pytest.mark.parametrize("path", THREE_NAMED)
    def test_the_scanner_does_not_treat_them_as_public(self, path: str) -> None:
        assert pps.surface_of(path) != "current_doc"

    def test_their_findings_are_deferred_not_resolved(self, source_context) -> None:
        """Recorded as outstanding. Neutralization belongs to the disposition lane."""
        if source_context.is_public:
            source_context.assert_excluded(THREE_NAMED)
            return
        total = 0
        for path in THREE_NAMED:
            total += len(pps.scan_text(path, (REPO / path).read_text(encoding="utf-8")))
        assert total > 0, "expected the historical owner-root findings to still be present"


class TestPublicRequiredLinks:
    def test_required_documents_only_link_to_shipping_targets(self, policy) -> None:
        link = re.compile(r"\]\(([^)\s]+)")
        offenders: list[str] = []
        for doc in sorted(policy.public_documents()):
            if not (REPO / doc).exists():
                continue
            for raw in link.findall((REPO / doc).read_text(encoding="utf-8", errors="replace")):
                if raw.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                target = raw.split("#")[0]
                if not target:
                    continue
                base = PurePosixPath(doc).parent
                resolved = os.path.normpath(str(base / target) if str(base) != "." else target)
                resolved = resolved.replace("\\", "/")
                if not (REPO / resolved).exists():
                    offenders.append(f"{doc} -> {resolved} (missing)")
                elif policy.is_document(resolved) and resolved not in policy.public_documents():
                    offenders.append(f"{doc} -> {resolved} (internal)")
        assert offenders == [], "public documents must not depend on excluded ones: " + "; ".join(offenders)

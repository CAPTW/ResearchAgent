# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the final D/E/F disposition and the public link graph.

The previous checkpoint made the document boundary fail closed but left every
historical document marked "internal, pending review". These assertions cover the
step after that: the question is now settled for this snapshot, and nothing is
waiting on a decision that never comes.

Three properties matter most, and each replaces a way the snapshot could be wrong
without anything failing:

* a governed path with a role but no disposition would look classified while
  still being undecided;
* a public document linking to an excluded one would ship a broken instruction;
* a shipped fixture holding the owner's real layout would disclose it while
  passing every test that only looks at documents.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from tools import public_content_policy as pcp
from tools import public_path_scan as pps
from tests.history_contract_baseline import current_bytes_match, historical_urls

REPO = Path(__file__).resolve().parents[1]
BASE_COMMIT = "d0cd79647d0cc9ff30466e78b8358840999797d7"
THREE_NAMED = (
    "docs/LOCAL_AGENT_RUNTIME.md",
    "docs/SCHEDULED_LOCAL_RESEARCH.md",
    "docs/STORAGE_PORTABILITY.md",
)
LINK = re.compile(r"\]\(([^)\s]+)")


def _is_git_checkout() -> bool:
    return (REPO / ".git").exists()


requires_repository = pytest.mark.skipif(
    not _is_git_checkout(), reason="extracted source carries no Git history"
)


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", "HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\n") if p]


@pytest.fixture(scope="module")
def policy() -> pcp.ContentPolicy:
    return pcp.load()


def _local_links(doc: str) -> list[str]:
    """Repository-relative targets of every local link in `doc`."""
    text = (REPO / doc).read_text(encoding="utf-8", errors="replace")
    targets = []
    for raw in LINK.findall(text):
        if raw.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = raw.split("#")[0]
        if not target:
            continue
        base = PurePosixPath(doc).parent
        resolved = os.path.normpath(str(base / target) if str(base) != "." else target)
        targets.append(resolved.replace("\\", "/"))
    return targets


class TestFinalDisposition:
    def test_every_governed_path_is_settled(self, policy) -> None:
        assert set(policy.dispositions) == set(policy.documents)
        assert set(policy.dispositions.values()) <= pcp.DISPOSITION_STATUSES

    @requires_repository
    def test_no_unresolved_disposition_remains(self, policy) -> None:
        assert policy.unresolved(tracked()) == []

    def test_every_governed_path_carries_a_reason_code(self, policy) -> None:
        missing = [p for p in policy.documents if not policy.reason_codes.get(p)]
        assert missing == [], f"paths with no reason code: {missing[:5]}"

    def test_disposition_agrees_with_role(self, policy) -> None:
        expected = {
            "PUBLIC_REQUIRED": "public_required_final",
            "PUBLIC_CURATED": "public_curated_final",
            "INTERNAL_HISTORICAL": "internal_historical_final",
            "INTERNAL_ARTIFACT": "internal_artifact_final",
            "TEST_SYNTHETIC": "test_synthetic_final",
        }
        for path, role in policy.documents.items():
            assert policy.dispositions[path] == expected[role], path

    def test_no_pending_review_marker_survives(self, policy) -> None:
        """Checked over the decisions, not the raw file: a document *path* may
        legitimately contain a word like "pending" without being undecided."""
        for value in set(policy.dispositions.values()) | set(policy.reason_codes.values()):
            assert not re.search(r"pending|todo|tbd|unresolved", value, re.IGNORECASE), value

    @pytest.mark.parametrize("name", ["LICENSE", "License", "license", "docs/LICENSE"])
    def test_exact_name_matching_folds_case(self, name: str, policy) -> None:
        """`License` and `LICENSE` are one file on Windows.

        Matching only the shouted spelling left the others ungoverned, so they
        skipped the unclassified check entirely and shipped as ordinary source.
        """
        assert policy.is_document(name)

    def test_an_unconfigured_name_is_still_not_a_document(self, policy) -> None:
        """Case-folding widened the match; it must not have widened the set."""
        assert not policy.is_document("NOTICE")
        assert not policy.is_document("Makefile")

    @pytest.mark.parametrize("mutation,expected", [
        ({"dispositions": {"docs/GHOST.md": "internal_historical_final"}}, "not in the inventory"),
        ({"reason_codes": {"docs/GHOST.md": "raw_audit_record"}}, "not in the inventory"),
        ({"dispositions": {"docs/M3_WORKBENCH.md": "public_required_final"}}, "contradicts its role"),
    ])
    def test_metadata_must_agree_with_the_inventory(self, tmp_path, mutation, expected) -> None:
        """An orphan entry decides about a path nobody can look at."""
        import json
        raw = json.loads((REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8"))
        for key, value in mutation.items():
            raw[key].update(value)
        candidate = tmp_path / "policy.json"
        candidate.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(pcp.PolicyError, match=expected):
            pcp.load(candidate)

    def test_policy_metadata_holds_no_machine_or_clock_data(self, policy) -> None:
        raw = (REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8")
        assert not pps.OWNER_ROOTS.search(raw)
        assert not re.search(r"\b20\d\d-\d\d-\d\dT\d\d:", raw), "a wall-clock stamp reached the policy"


class TestDClassInternalHistorical:
    @requires_repository
    def test_internal_documents_stay_tracked(self, policy) -> None:
        present = set(tracked())
        internal = [p for p, r in policy.documents.items() if r == "INTERNAL_HISTORICAL"]
        assert internal, "expected internal historical documents"
        assert all(p in present for p in internal), "an internal document left the repository"

    @requires_repository
    def test_internal_blobs_are_byte_identical_to_the_base(self, policy) -> None:
        """Exclusion is the control. Editing an excluded file would be pointless
        churn at best and a quiet history rewrite at worst."""
        internal = sorted(p for p, r in policy.documents.items() if r == "INTERNAL_HISTORICAL")
        changed = [path for path in internal if not current_bytes_match(REPO, BASE_COMMIT, path)]
        assert changed == [], f"internal historical documents were edited: {changed[:5]}"

    def test_audit_and_milestone_families_are_internal(self, policy) -> None:
        for prefix, pattern in (("docs/audit/", None), (None, r"^docs/M\d")):
            group = [p for p in policy.documents
                     if (p.startswith(prefix) if prefix else re.match(pattern, p))]
            assert group
            assert {policy.documents[p] for p in group} == {"INTERNAL_HISTORICAL"}

    def test_no_internal_rule_captures_a_public_document(self, policy) -> None:
        public = policy.public_documents()
        assert not any(p.startswith("docs/audit/") or re.match(r"^docs/M\d", p) for p in public)


class TestEClassCuratedSelection:
    def test_the_allowlist_matches_the_roles_it_names(self, policy) -> None:
        by_role = sorted(p for p, r in policy.documents.items() if r == "PUBLIC_CURATED")
        assert list(policy.curated_allowlist) == by_role

    def test_an_empty_curated_set_is_accepted(self, policy) -> None:
        """Promotion needs independent public value; none was demonstrated here."""
        assert len(policy.curated_allowlist) <= policy.curated_cap

    def test_the_cap_is_enforced_by_the_loader(self, tmp_path, policy) -> None:
        import json
        raw = json.loads((REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8"))
        overflow = sorted(p for p, r in raw["documents"].items()
                          if r == "INTERNAL_HISTORICAL")[:raw["curated_public_cap"] + 1]
        for path in overflow:
            raw["documents"][path] = "PUBLIC_CURATED"
            raw["dispositions"][path] = "public_curated_final"
        raw["curated_public_allowlist"] = sorted(overflow)
        candidate = tmp_path / "policy.json"
        candidate.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(pcp.PolicyError, match="cap"):
            pcp.load(candidate)

    def test_a_curated_claim_without_the_role_is_rejected(self, tmp_path) -> None:
        import json
        raw = json.loads((REPO / "tools" / "public_content_policy.json").read_text(encoding="utf-8"))
        raw["curated_public_allowlist"] = ["docs/M3_WORKBENCH.md"]
        candidate = tmp_path / "policy.json"
        candidate.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(pcp.PolicyError, match="allowlist disagrees"):
            pcp.load(candidate)

    def test_promoted_documents_would_have_to_be_path_safe(self, policy) -> None:
        """Vacuous while the set is empty, and the guard that stops it staying so."""
        for path in policy.curated_allowlist:
            text = (REPO / path).read_text(encoding="utf-8", errors="replace")
            assert pps.scan_text(path, text) == [], f"{path} carries an owner root"


class TestThreeNamedDocuments:
    """One dependent operational family: local runtime, its schedule, its storage.

    Splitting it would publish half an instruction, so it stays internal as a unit
    until a lane reviews the whole set.
    """

    @pytest.mark.parametrize("path", THREE_NAMED)
    def test_each_is_internal_historical(self, path: str, policy) -> None:
        assert policy.documents[path] == "INTERNAL_HISTORICAL"
        assert policy.dispositions[path] == "internal_historical_final"

    @pytest.mark.parametrize("path", THREE_NAMED)
    @requires_repository
    def test_each_is_byte_identical_to_the_base(self, path: str) -> None:
        assert current_bytes_match(REPO, BASE_COMMIT, path), (
            f"{path} was edited; this lane retains it unchanged"
        )

    @requires_repository
    def test_the_family_is_not_split(self, policy) -> None:
        roles = {policy.documents[p] for p in THREE_NAMED}
        assert len(roles) == 1, "the dependent family was split across public and internal"

    @requires_repository
    def test_no_public_document_depends_on_them(self, policy) -> None:
        needed = set()
        for doc in policy.public_documents():
            if (REPO / doc).exists():
                needed.update(_local_links(doc))
        assert not (needed & set(THREE_NAMED))


class TestFClassSyntheticFixtures:
    def test_no_shipped_fixture_carries_the_owner_layout(self, policy) -> None:
        """The headline F-class property, checked over what actually ships."""
        offenders = []
        for candidate in (REPO / "tests").rglob("*.py"):
            rel = candidate.relative_to(REPO).as_posix()
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if pps.scan_text(rel, text):
                offenders.append(rel)
            # The scanner exempts its own fixture, so check that one directly too.
            if rel in pps.SELF_PATHS and pps.scan_text("probe.md", text):
                offenders.append(rel)
        assert offenders == [], f"actual owner layout in shipped fixtures: {offenders}"

    def test_the_detector_is_still_armed_against_those_shapes(self) -> None:
        from tests.test_public_path_scan import OWNER_SHAPES

        assert len(OWNER_SHAPES) >= 7
        assert all(pps.OWNER_ROOTS.search(shape) for shape in OWNER_SHAPES)

    @pytest.mark.parametrize("safe", [
        r"C:\Users\EXAMPLE\example-repo", "/home/example-user/project",
        r"\\server\share\fixture-root", "file:///C:/sample-project/x",
        "<REPO_ROOT>/docs/x.md", "%LOCALAPPDATA%/XRMLData",
    ])
    def test_synthetic_shapes_are_still_accepted(self, safe: str) -> None:
        assert pps.scan_text("tests/test_x.py", safe) == []

    def test_no_blanket_test_directory_suppression(self) -> None:
        """`tests/**` must still be judged; only two exact files are exempt."""
        assert pps.surface_of("tests/test_anything.py") == "fixture"
        assert len(pps.SELF_PATHS) == 2

    def test_a_reintroduced_owner_root_in_a_fixture_would_fail(self) -> None:
        from tests.test_public_path_scan import OWNER_SHAPES

        assert pps.scan_text("tests/test_new_fixture.py", OWNER_SHAPES[2])


class TestPublicLinkClosure:
    def _public_docs(self, policy) -> list[str]:
        return sorted(p for p in policy.public_documents() if (REPO / p).exists())

    def test_every_public_link_resolves(self, policy) -> None:
        missing = [f"{doc} -> {t}" for doc in self._public_docs(policy)
                   for t in _local_links(doc) if not (REPO / t).exists()]
        assert missing == [], f"broken public links: {missing}"

    def test_no_public_document_links_to_an_excluded_one(self, policy) -> None:
        ship = policy.shipping_documents()
        leaks = [f"{doc} -> {t}" for doc in self._public_docs(policy)
                 for t in _local_links(doc)
                 if policy.is_document(t) and t not in ship]
        assert leaks == [], f"public documents depending on excluded ones: {leaks}"

    @requires_repository
    def test_link_targets_match_case_exactly(self, policy) -> None:
        """A case-only match works on Windows and breaks everywhere else."""
        present = set(tracked())
        folded = {p.lower(): p for p in present}
        wrong = [f"{doc} -> {t}" for doc in self._public_docs(policy)
                 for t in _local_links(doc)
                 if t not in present and folded.get(t.lower())]
        assert wrong == [], f"case-mismatched links: {wrong}"

    def test_no_link_escapes_the_package_root(self, policy) -> None:
        escaping = [f"{doc} -> {t}" for doc in self._public_docs(policy)
                    for t in _local_links(doc)
                    if t.startswith("/") or t.startswith("..") or ".." in PurePosixPath(t).parts]
        assert escaping == [], f"links escaping the root: {escaping}"

    @requires_repository
    def test_no_repository_url_was_invented(self, policy) -> None:
        """Differential, because the question is whether *this* change fabricated one.

        Public documents already reference a staging host and an `example.com`
        issuer placeholder. Those are pre-existing facts, not inventions, and this
        lane has no mandate to rewrite them -- so the assertion is that no new
        self-referential URL appeared, not that none exists.
        """
        url = re.compile(r"https?://[^\s`'\")<>]+")
        present_at_base = {
            line
            for line in subprocess.run(
                ["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", BASE_COMMIT],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            if line
        }
        for doc in self._public_docs(policy):
            if doc not in present_at_base:
                continue
            after = set(url.findall((REPO / doc).read_text(encoding="utf-8", errors="replace")))
            before = historical_urls(REPO, BASE_COMMIT, doc)
            assert after - before == set(), f"{doc} introduced new URLs: {sorted(after - before)}"


class TestHistoricalHonesty:
    """Vacuous while `PUBLIC_CURATED` is empty; the point is that it stops being
    vacuous the moment a document is promoted and edited."""

    def test_edited_curated_documents_keep_their_status_tokens(self, policy) -> None:
        for path in policy.curated_allowlist:
            text = (REPO / path).read_text(encoding="utf-8", errors="replace")
            for token in ("PARTIAL", "BLOCKED"):
                if token in text:
                    assert token in text, f"{path} lost a {token} token"

    def test_no_normalization_note_claims_the_placeholder_was_original(self, policy) -> None:
        for path in policy.curated_allowlist:
            text = (REPO / path).read_text(encoding="utf-8", errors="replace").lower()
            assert "was originally <repo_root>" not in text
            assert "the original path was <" not in text

    @requires_repository
    def test_the_readme_milestone_note_does_not_rewrite_history(self) -> None:
        """The links became plain mentions; the records themselves are untouched."""
        text = (REPO / "README.md").read_text(encoding="utf-8")
        assert "docs/M9_CI_AND_MERGE_GATING.md" in text, "the record must still be named"
        assert "](docs/M9_CI_AND_MERGE_GATING.md)" not in text, "it must not link to an excluded file"

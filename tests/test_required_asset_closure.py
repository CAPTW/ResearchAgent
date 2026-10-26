# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Controls for the required-asset closure the public package must carry.

The document policy defaulted everything not provably public to internal, which
was right for historical records and wrong for thirteen files the shipped product
and its adapter contracts actually load. The archive still built, still validated,
still passed every focused gate -- and the extracted package could not run its own
report pipeline, because `templates/paper_summary_template.md` was not in it.

Nothing caught that until a full no-Git suite ran, so these assertions exist to
make the closure explicit and to fail loudly if any member of it is demoted again.
The list is exact: a directory rule here would re-open the boundary that the
fail-closed policy exists to hold shut.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools import public_content_policy as pcp
from tools import public_path_scan as pps
from tests import history_contract_baseline as hcb
from tests.history_contract_baseline import (
    current_bytes_match,
    historical_documents,
    historical_line_contract,
)

REPO = Path(__file__).resolve().parents[1]
BASE_COMMIT = "b7d3047831027f8b027f4e0b1dd1e568d0ef9c61"
M0_POLICY_BASE_COMMIT = "635380c7280d704bcc4c9ccf15e24b412323e8d9"
M1_POLICY_PARENT_COMMIT = "3c4dd21b4505d94463cd15fb38857c54812719e3"
HISTORICAL_DISPOSITION_BASE_COMMIT = "d0cd79647d0cc9ff30466e78b8358840999797d7"

# Exactly the measured dependency closure. Each entry was proven required by
# removing it and watching a committed test fail.
REQUIRED_ASSETS: dict[str, str] = {
    "templates/paper_summary_template.md": "runtime_template_required",
    "AGENTS.md": "public_operational_agent_instruction",
    "GEMINI.md": "public_agent_adapter_contract",
    "CLAUDE.md": "public_agent_adapter_contract",
    ".claude/rules/local-only.md": "public_agent_adapter_contract",
    ".claude/rules/reporting.md": "public_agent_adapter_contract",
    ".claude/skills/local-research-run/SKILL.md": "public_skill_contract",
    "docs/AGENT_ADAPTERS.md": "public_agent_adapter_contract",
    ".agents/skills/local-research-run/SKILL.md": "public_skill_contract",
    "docs/LOCAL_RESEARCH_WORKFLOW.md": "public_operational_agent_instruction",
    "design/stitch/manual/component-hierarchy.md": "public_design_intake_contract",
    "design/stitch/manual/design-dna.md": "public_design_intake_contract",
    "design/stitch/manual/implementation-notes.md": "public_design_intake_contract",
}

STILL_INTERNAL = (
    "docs/LOCAL_AGENT_RUNTIME.md",
    "docs/SCHEDULED_LOCAL_RESEARCH.md",
    "docs/STORAGE_PORTABILITY.md",
    "docs/M3_WORKBENCH.md",
)

CURRENT_AUTHORITY_DOCUMENTS = {
    "docs/adr/0001-source-identity-and-current-authority.md",
}

APPROVED_M0_PUBLIC_REQUIRED_ADDITIONS = {
    "docs/adr/0002-current-v2-native-research-workspace-reuse-first-migration.md",
    "docs/architecture/current-v2-research-workspace-rebaseline.md",
    "docs/architecture/reuse-and-legacy-disposition-current-v2.md",
    "docs/architecture/target-desktop-architecture-research-memory-and-large-corpus.md",
    "docs/architecture/ui-to-domain-mapping-current-v2.md",
    "docs/roadmap/m0-to-m5-native-research-workspace.md",
}

DESIGN_QA_PATH = "design-qa.md"
M0_APPROVED_POLICY_DELTA = APPROVED_M0_PUBLIC_REQUIRED_ADDITIONS | {DESIGN_QA_PATH}
M1_PUBLIC_REQUIRED_PATH = "docs/architecture/m1-native-research-workspace-foundation.md"
M1_APPROVED_POLICY_DELTA = {M1_PUBLIC_REQUIRED_PATH}
CURRENT_PREPARATION_POLICY_DELTA = {
    "docs/BROWSER_COLLECTION.md", "docs/MODEL_CONNECTIONS.md",
    "docs/roadmap/current-local-status.md", "docs/roadmap/model-connections.md",
}
FINAL_APPROVED_POLICY_DELTA = M0_APPROVED_POLICY_DELTA | M1_APPROVED_POLICY_DELTA | CURRENT_PREPARATION_POLICY_DELTA
DESIGN_QA_SOURCE_PIN = {
    "baseline_request_commits": [HISTORICAL_DISPOSITION_BASE_COMMIT],
    "fixture_schema_version": 1,
    "raw_byte_size": 3346,
    "raw_sha256": "779245b76f6189b82884298a621e0235abab1b587422b4a412a3dc439e89c7df",
    "source_blob": "56a58bb4131e793cb0e4ae0a17bd21742e758ab8",
    "source_commit": M0_POLICY_BASE_COMMIT,
    "source_mode": "100644",
    "source_path": DESIGN_QA_PATH,
    "source_tree": "1903fb555147d33f657f1c6e9ad09c4c3066f1f5",
    "use": "raw_byte_identity",
}


def _git_json(commit: str, path: str) -> dict[str, object]:
    raw = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{commit}:{path}"],
        capture_output=True,
        check=True,
    ).stdout
    loaded = json.loads(raw)
    assert isinstance(loaded, dict)
    return loaded


def _mapping_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, set[str]]:
    before_keys = set(before)
    after_keys = set(after)
    return {
        "added": after_keys - before_keys,
        "removed": before_keys - after_keys,
        "modified": {key for key in before_keys & after_keys if before[key] != after[key]},
    }


def _assert_exact_policy_delta(
    before: dict[str, object],
    after: dict[str, object],
    expected_added: set[str],
) -> None:
    for section in ("documents", "dispositions", "reason_codes"):
        old = before[section]
        current = after[section]
        assert isinstance(old, dict) and isinstance(current, dict)
        delta = _mapping_delta(old, current)
        assert delta == {
            "added": expected_added,
            "removed": set(),
            "modified": set(),
        }, f"unexpected {section} delta: {delta}"


def _install_history_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    entries = document["entries"]
    assert isinstance(entries, dict)
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    document["entries_sha256"] = digest
    fixture = tmp_path / "history-contracts.json"
    fixture.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(hcb, "FIXTURE", fixture)
    monkeypatch.setattr(hcb, "EXPECTED_ENTRIES_SHA256", digest)
    hcb._entries.cache_clear()
    hcb._commit_mode.cache_clear()


def _is_git_checkout() -> bool:
    return (REPO / ".git").exists()


requires_repository = pytest.mark.skipif(
    not _is_git_checkout(), reason="extracted source carries no Git history"
)


@pytest.fixture(scope="module")
def policy() -> pcp.ContentPolicy:
    return pcp.load()


class TestExactPromotionClosure:
    @pytest.mark.parametrize("path", sorted(REQUIRED_ASSETS))
    def test_each_required_asset_is_public(self, path: str, policy) -> None:
        assert policy.documents[path] == "PUBLIC_REQUIRED"
        assert policy.dispositions[path] == "public_required_final"
        assert policy.reason_codes[path] == REQUIRED_ASSETS[path]

    @requires_repository
    def test_exactly_thirteen_paths_were_promoted(self, policy) -> None:
        """The cap is the point. A fourteenth would be an unreviewed decision."""
        old = historical_documents(REPO, BASE_COMMIT, "tools/public_content_policy.json")
        promoted = sorted(
            p
            for p, r in policy.documents.items()
            if r == "PUBLIC_REQUIRED"
            and old.get(p) != "PUBLIC_REQUIRED"
            and p not in CURRENT_AUTHORITY_DOCUMENTS
            and p not in APPROVED_M0_PUBLIC_REQUIRED_ADDITIONS
            and p not in M1_APPROVED_POLICY_DELTA
            and p not in CURRENT_PREPARATION_POLICY_DELTA
        )
        assert promoted == sorted(REQUIRED_ASSETS), f"unexpected promotion set: {promoted}"
        assert len(promoted) == 13
        assert M1_APPROVED_POLICY_DELTA.isdisjoint(REQUIRED_ASSETS)

    @requires_repository
    def test_no_unrelated_role_changed(self, policy) -> None:
        before = historical_documents(REPO, BASE_COMMIT, "tools/public_content_policy.json")
        changed = {p for p in policy.documents
                   if before.get(p) != policy.documents[p]} | (set(before) ^ set(policy.documents))
        changed -= CURRENT_AUTHORITY_DOCUMENTS
        expected = set(REQUIRED_ASSETS) | FINAL_APPROVED_POLICY_DELTA
        assert changed == expected, (
            "policy change mismatch: "
            f"missing={sorted(expected - changed)} extra={sorted(changed - expected)}"
        )

    @requires_repository
    def test_the_m0_policy_delta_is_exactly_six_public_and_one_internal(self) -> None:
        before = _git_json(M0_POLICY_BASE_COMMIT, "tools/public_content_policy.json")
        m0_after = _git_json(M1_POLICY_PARENT_COMMIT, "tools/public_content_policy.json")
        after = json.loads((REPO / "tools/public_content_policy.json").read_text(encoding="utf-8"))
        _assert_exact_policy_delta(before, m0_after, M0_APPROVED_POLICY_DELTA)
        _assert_exact_policy_delta(before, after, FINAL_APPROVED_POLICY_DELTA)
        _assert_exact_policy_delta(m0_after, after, M1_APPROVED_POLICY_DELTA | CURRENT_PREPARATION_POLICY_DELTA)

    @requires_repository
    def test_an_eighth_policy_delta_is_rejected(self) -> None:
        before = _git_json(M0_POLICY_BASE_COMMIT, "tools/public_content_policy.json")
        m0_after = _git_json(M1_POLICY_PARENT_COMMIT, "tools/public_content_policy.json")
        after = json.loads((REPO / "tools/public_content_policy.json").read_text(encoding="utf-8"))
        for section, value in (
            ("documents", "PUBLIC_REQUIRED"),
            ("dispositions", "public_required_final"),
            ("reason_codes", "unapproved_test_path"),
        ):
            after[section]["docs/unapproved-ninth.md"] = value
        with pytest.raises(AssertionError, match="unexpected documents delta"):
            _assert_exact_policy_delta(before, after, FINAL_APPROVED_POLICY_DELTA)
        with pytest.raises(AssertionError, match="unexpected documents delta"):
            _assert_exact_policy_delta(m0_after, after, M1_APPROVED_POLICY_DELTA | CURRENT_PREPARATION_POLICY_DELTA)

    def test_promotion_is_exact_not_a_directory_rule(self, policy) -> None:
        """Siblings of a promoted file must stay closed."""
        siblings = [p for p in policy.documents
                    if (p.startswith("design/stitch/") or p.startswith(".claude/")
                        or p.startswith(".agents/") or p.startswith("templates/"))
                    and p not in REQUIRED_ASSETS]
        assert siblings, "expected unpromoted siblings to exist"
        assert {policy.documents[p] for p in siblings} == {"INTERNAL_HISTORICAL"}

    def test_public_curated_is_still_empty(self, policy) -> None:
        assert list(policy.curated_allowlist) == []

    @pytest.mark.parametrize("path", STILL_INTERNAL)
    def test_unrelated_internal_documents_stay_internal(self, path: str, policy) -> None:
        assert policy.documents[path] == "INTERNAL_HISTORICAL"

    def test_audit_records_stay_internal(self, policy) -> None:
        audit = [p for p in policy.documents if p.startswith("docs/audit/")]
        assert audit and {policy.documents[p] for p in audit} == {"INTERNAL_HISTORICAL"}


class TestRequiredAssetsShip:
    @pytest.mark.parametrize("path", sorted(REQUIRED_ASSETS))
    def test_the_asset_exists_in_this_source_tree(self, path: str) -> None:
        """Passes in a checkout and in an extracted archive -- that is the point."""
        assert (REPO / path).is_file(), f"{path} is missing from the distributed source"

    # Publishing `docs/AGENT_ADAPTERS.md` exposed one defect in it: a link written
    # as an absolute path containing the repository directory name, pointing at a
    # document the policy keeps internal. Both halves are disqualifying for a public
    # document, so the link became a plain-text mention -- the narrowest correction
    # that closes it, and the only content change in the whole closure.
    NARROWLY_CORRECTED = {"docs/AGENT_ADAPTERS.md"}

    @pytest.mark.parametrize("path", sorted(set(REQUIRED_ASSETS) - NARROWLY_CORRECTED))
    @requires_repository
    def test_the_asset_is_byte_identical_to_the_base(self, path: str) -> None:
        """Policy-only repair: twelve of the thirteen were not touched at all."""
        assert current_bytes_match(REPO, BASE_COMMIT, path)

    @requires_repository
    def test_the_one_corrected_asset_changed_only_that_link(self) -> None:
        path = "docs/AGENT_ADAPTERS.md"
        before, absolute_link_lines = historical_line_contract(REPO, BASE_COMMIT, path)
        after = (REPO / path).read_text(encoding="utf-8").splitlines()
        assert len(before) == len(after), "the correction must not add or remove lines"
        differing = [
            i
            for i, (expected, current) in enumerate(zip(before, after))
            if expected != hashlib.sha256(current.encode("utf-8")).hexdigest()
        ]
        assert len(differing) == 1, f"expected exactly one changed line, got {len(differing)}"
        new = after[differing[0]]
        assert differing[0] in absolute_link_lines, "the original line held an absolute link"
        assert "](" not in new, "the replacement must not be a link"
        assert "UI_UX_STITCH_HANDOFF_RELEASE.md" in new, "the record must still be named"

    @pytest.mark.parametrize("path", sorted(REQUIRED_ASSETS))
    def test_the_asset_carries_no_owner_path(self, path: str) -> None:
        text = (REPO / path).read_text(encoding="utf-8", errors="replace")
        assert pps.scan_text(path, text) == []

    def test_the_runtime_template_is_where_the_product_looks(self) -> None:
        """`app/services/runs.py` resolves this by literal relative path.

        Its absence did not fail a policy check -- it failed the pipeline, with
        `pipeline_execution_failed`, only once a no-Git package was actually run.
        """
        template = REPO / "templates" / "paper_summary_template.md"
        assert template.is_file()
        assert template.read_text(encoding="utf-8", errors="replace").strip()

    def test_the_adapter_mirror_pair_ships_together(self) -> None:
        """A canonical skill without its mirror fails synchronization, not import."""
        for path in (".agents/skills/local-research-run/SKILL.md",
                     ".claude/skills/local-research-run/SKILL.md"):
            assert (REPO / path).is_file(), path


@requires_repository
class TestDesignQaHistoricalPin:
    def test_the_fixture_records_the_exact_635380c7_git_identity(self) -> None:
        document = json.loads(hcb.FIXTURE.read_text(encoding="utf-8"))
        key = f"{M0_POLICY_BASE_COMMIT}:{DESIGN_QA_PATH}"
        assert document["entries"][key] == DESIGN_QA_SOURCE_PIN

        row = subprocess.run(
            ["git", "-C", str(REPO), "ls-tree", M0_POLICY_BASE_COMMIT, DESIGN_QA_PATH],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert row == f"100644 blob {DESIGN_QA_SOURCE_PIN['source_blob']}\t{DESIGN_QA_PATH}"

    def test_current_design_qa_matches_the_explicit_635380c7_pin(self) -> None:
        assert current_bytes_match(REPO, HISTORICAL_DISPOSITION_BASE_COMMIT, DESIGN_QA_PATH)

    def test_modified_design_qa_bytes_are_rejected(self) -> None:
        raw = (REPO / DESIGN_QA_PATH).read_bytes()
        with pytest.raises(AssertionError, match="historical bytes disagree"):
            hcb._verify_payload(DESIGN_QA_SOURCE_PIN, raw + b"\n")

    def test_missing_design_qa_history_is_rejected(self) -> None:
        entries = {
            "0000000000000000000000000000000000000000:other.md": {
                "source_path": "other.md",
                "source_commit": "0000000000000000000000000000000000000000",
            }
        }
        with pytest.raises(AssertionError, match="baseline key is not tracked"):
            hcb._resolve_entry(
                entries,
                HISTORICAL_DISPOSITION_BASE_COMMIT,
                DESIGN_QA_PATH,
            )

    def test_malformed_history_fixture_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        document = json.loads(hcb.FIXTURE.read_text(encoding="utf-8"))
        key = f"{M0_POLICY_BASE_COMMIT}:{DESIGN_QA_PATH}"
        document["entries"][key]["source_mode"] = "100600"
        _install_history_fixture(monkeypatch, tmp_path, document)
        try:
            with pytest.raises(AssertionError, match="invalid source mode"):
                hcb._entries()
        finally:
            hcb._entries.cache_clear()
            hcb._commit_mode.cache_clear()

    def test_missing_commit_in_a_non_shallow_repository_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing_commit = "f" * 40
        empty_sha = hashlib.sha256(b"").hexdigest()
        document: dict[str, object] = {
            "entries": {
                f"{missing_commit}:missing.md": {
                    "fixture_schema_version": 1,
                    "raw_byte_size": 0,
                    "raw_sha256": empty_sha,
                    "source_blob": "0" * 40,
                    "source_commit": missing_commit,
                    "source_path": "missing.md",
                    "source_tree": "0" * 40,
                    "use": "raw_byte_identity",
                }
            },
            "schema_version": 1,
        }
        _install_history_fixture(monkeypatch, tmp_path, document)
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=XRML Validation",
                "-c",
                "user.email=xrml-validation@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
        )
        try:
            with pytest.raises(AssertionError, match="absent from a non-shallow repository"):
                hcb._commit_mode(str(repo), missing_commit)
        finally:
            hcb._entries.cache_clear()
            hcb._commit_mode.cache_clear()

    def test_missing_source_blob_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        document = json.loads(hcb.FIXTURE.read_text(encoding="utf-8"))
        key = f"{M0_POLICY_BASE_COMMIT}:{DESIGN_QA_PATH}"
        document["entries"] = {key: document["entries"][key]}
        document["entries"][key]["source_blob"] = "0" * 40
        _install_history_fixture(monkeypatch, tmp_path, document)
        try:
            with pytest.raises(AssertionError, match="live historical path/blob mismatch"):
                hcb._commit_mode(str(REPO), M0_POLICY_BASE_COMMIT)
        finally:
            hcb._entries.cache_clear()
            hcb._commit_mode.cache_clear()


class TestPolicyContractsStillHold:
    @requires_repository
    def test_the_boundary_is_still_fail_closed(self, policy) -> None:
        tracked = [p for p in subprocess.run(
            ["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True).stdout.split("\n") if p]
        assert policy.unclassified(tracked) == []
        assert policy.unresolved(tracked) == []
        assert policy.stale(tracked) == []
        with pytest.raises(pcp.PolicyError, match="no role"):
            pcp.validate_against(tracked + ["docs/brand-new.md"], policy)

    @pytest.mark.parametrize("path,tier", [
        (".env", "fatal"), (".envrc", "fatal"), (".mcp.json", "fatal"),
        ("tool.exe", "prohibited"),
    ])
    def test_secret_and_binary_controls_remain_armed(self, path: str, tier: str) -> None:
        from tools import package_source_distribution as psd

        included, _excluded, prohibited, fatal = psd.classify([path])
        assert path not in included
        rows = fatal if tier == "fatal" else prohibited
        assert path in [p for p, _ in rows]

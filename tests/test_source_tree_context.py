"""Public snapshot admission must not mask a broken private checkout."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tests import history_contract_baseline as hcb
from tests import source_tree_context as stc
from tools import public_content_policy as pcp


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo: Path) -> str:
    git(repo, "add", ".")
    git(repo, "-c", "user.name=XRML Validation", "-c", "user.email=xrml-validation@example.invalid",
        "commit", "-m", "synthetic source tree fixture")
    return git(repo, "rev-parse", "HEAD")


def make_tree(root: Path, *, public: bool, with_git: bool) -> pcp.ContentPolicy:
    root.mkdir()
    # Raw-byte contracts must not depend on the host's checkout EOL defaults.
    (root / ".gitattributes").write_bytes(b"*.md -text\n")
    paths = ["README.md", "docs/guide.md", "docs/private.md"]
    policy = pcp.load(data=json.dumps(pcp.generate(
        paths, public_required=frozenset({"README.md", "docs/guide.md"})
    )).encode())
    for path in paths if not public else sorted(policy.shipping_documents()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic fixture\n")
    if with_git:
        git(root, "init", "--initial-branch=main")
        commit(root)
    return policy


def pin(repo: Path, revision: str, path: str) -> dict:
    raw = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:{path}"],
                         check=True, capture_output=True).stdout
    mode, _, blob = git(repo, "ls-tree", revision, path).split("\t")[0].split()
    return {
        "fixture_schema_version": 1, "source_commit": revision, "source_path": path,
        "source_tree": git(repo, "show", "-s", "--format=%T", revision), "source_mode": mode,
        "source_blob": blob, "raw_byte_size": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "use": "raw_byte_identity",
    }


@pytest.fixture
def install_pin(monkeypatch, tmp_path):
    def install(entry):
        entries = {f"{entry['source_commit']}:{entry['source_path']}": entry}
        digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        fixture = tmp_path / "contracts.json"
        fixture.write_text(json.dumps({"schema_version": 1, "entries": entries,
                                       "entries_sha256": digest}), encoding="utf-8")
        monkeypatch.setattr(hcb, "FIXTURE", fixture)
        monkeypatch.setattr(hcb, "EXPECTED_ENTRIES_SHA256", digest)
        hcb._entries.cache_clear()
        hcb._commit_mode.cache_clear()
    yield install
    hcb._entries.cache_clear()
    hcb._commit_mode.cache_clear()


@pytest.mark.parametrize("with_git", [False, True])
def test_complete_public_tree_is_admitted_and_inventory_remains_strict(tmp_path, with_git):
    repo = tmp_path / "public"
    policy = make_tree(repo, public=True, with_git=with_git)
    context = stc.inspect_source_tree(repo, policy)
    assert context.is_public and context.has_git == with_git
    context.assert_excluded(["docs/private.md"])
    context.validate_paths(list(context.paths) + ["app/new.py"])
    with pytest.raises(pcp.PolicyError, match="no longer exists"):
        context.validate_paths([p for p in context.paths if p != "README.md"])
    with pytest.raises(pcp.PolicyError, match="no role"):
        context.validate_paths(list(context.paths) + ["docs/unclassified.md"])
    with pytest.raises(pcp.PolicyError, match="excluded document present"):
        context.validate_paths(list(context.paths) + ["docs/private.md"])


@pytest.mark.parametrize("with_git", [False, True])
@pytest.mark.parametrize("mutation", ["missing_public", "untracked_private", "unclassified"])
def test_incomplete_or_mixed_public_tree_fails(tmp_path, with_git, mutation):
    repo = tmp_path / "public"
    policy = make_tree(repo, public=True, with_git=with_git)
    if mutation == "missing_public":
        (repo / "README.md").unlink()
    else:
        name = "private.md" if mutation == "untracked_private" else "unclassified.md"
        (repo / "docs" / name).write_text("synthetic fixture", encoding="utf-8")
    with pytest.raises(pcp.PolicyError):
        stc.inspect_source_tree(repo, policy)


def test_ignored_internal_file_cannot_enter_public_scope(tmp_path):
    repo = tmp_path / "public"
    policy = make_tree(repo, public=True, with_git=True)
    (repo / ".gitignore").write_text("docs/private.md\n", encoding="utf-8")
    (repo / "docs/private.md").write_text("synthetic fixture", encoding="utf-8")
    with pytest.raises(pcp.PolicyError):
        stc.inspect_source_tree(repo, policy)


def test_untracked_public_document_cannot_replace_a_shipping_git_entry(tmp_path):
    repo = tmp_path / "public"
    policy = make_tree(repo, public=True, with_git=True)
    git(repo, "rm", "--cached", "README.md")
    assert (repo / "README.md").is_file()
    with pytest.raises(pcp.PolicyError, match="no longer exists"):
        stc.inspect_source_tree(repo, policy)


def test_private_anchor_prevents_reclassifying_deleted_private_files(tmp_path, monkeypatch):
    repo = tmp_path / "development"
    policy = make_tree(repo, public=False, with_git=True)
    original = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(stc, "PRIVATE_HISTORY_ANCHORS", (original,))
    assert not stc.inspect_source_tree(repo, policy).is_public
    (repo / "docs/private.md").unlink()
    commit(repo)
    with pytest.raises(pcp.PolicyError, match="no longer exists"):
        stc.inspect_source_tree(repo, policy)


def test_partial_private_tree_cannot_use_public_fixture_path(tmp_path):
    repo = tmp_path / "development"
    policy = make_tree(repo, public=False, with_git=True)
    (repo / "docs/guide.md").unlink()
    with pytest.raises(pcp.PolicyError, match="no longer exists"):
        stc.inspect_source_tree(repo, policy)


def test_invalid_git_marker_does_not_fall_back_to_public(tmp_path):
    repo = tmp_path / "public"
    policy = make_tree(repo, public=True, with_git=False)
    (repo / ".git").write_text("invalid fixture", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        stc.inspect_source_tree(repo, policy)


def test_shallow_development_keeps_private_files_and_frozen_contract(tmp_path, monkeypatch, install_pin):
    origin = tmp_path / "origin"
    policy = make_tree(origin, public=False, with_git=True)
    original = git(origin, "rev-parse", "HEAD")
    entry = pin(origin, original, "README.md")
    (origin / "app.py").write_text("# synthetic fixture\n", encoding="utf-8")
    commit(origin)
    clone = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth=1", origin.as_uri(), str(clone))
    monkeypatch.setattr(stc, "PRIVATE_HISTORY_ANCHORS", (original,))
    install_pin(entry)
    context = stc.inspect_source_tree(clone, policy)
    assert not context.is_public
    assert hcb._commit_mode(str(clone), original) == "fixture"
    assert hcb.source_current_bytes_match(context, original, "README.md")


def test_public_fixture_access_does_not_weaken_generic_missing_history_guard(tmp_path, install_pin):
    origin = tmp_path / "origin"
    make_tree(origin, public=False, with_git=True)
    revision = git(origin, "rev-parse", "HEAD")
    install_pin(pin(origin, revision, "README.md"))
    public = tmp_path / "public"
    policy = make_tree(public, public=True, with_git=True)
    context = stc.inspect_source_tree(public, policy)
    assert context.is_public
    assert hcb.source_current_bytes_match(context, revision, "README.md")
    with pytest.raises(AssertionError, match="absent from a non-shallow repository"):
        hcb._commit_mode(str(public), revision)
    (public / "README.md").write_bytes(b"changed public fixture\n")
    assert not hcb.source_current_bytes_match(context, revision, "README.md")


def test_missing_live_blob_fails_in_an_independent_synthetic_repository(tmp_path, install_pin):
    repo = tmp_path / "development"
    make_tree(repo, public=False, with_git=True)
    revision = git(repo, "rev-parse", "HEAD")
    entry = pin(repo, revision, "README.md")
    entry["source_blob"] = "0" * 40
    install_pin(entry)
    with pytest.raises(AssertionError, match="live historical path/blob mismatch"):
        hcb._commit_mode(str(repo), revision)


@pytest.mark.parametrize("mutation", ["extra_field", "missing_disposition"])
def test_policy_projection_cannot_admit_extra_fields_or_drifted_inventory(mutation):
    entry = {"use": "parsed_policy_contract", "semantic": {
        "documents": {"README.md": "PUBLIC_REQUIRED"},
        "dispositions": {"README.md": "public_required_final"},
        "reason_codes": {"README.md": "synthetic_required"},
    }}
    if mutation == "extra_field":
        entry["semantic"]["unreviewed_field"] = {"example": "synthetic"}
    else:
        entry["semantic"]["dispositions"] = {}
    with pytest.raises(AssertionError, match="policy projection"):
        hcb._validate_semantic_shape("synthetic", entry)

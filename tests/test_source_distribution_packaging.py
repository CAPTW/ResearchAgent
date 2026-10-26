"""Contract tests for the source-distribution boundary.

The point of these is that the boundary fails *closed*. A packaging filter that
silently drops a secret looks identical to one that never saw it, so each tier is
pinned by behaviour rather than by inspecting the pattern lists.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from tools import package_source_distribution as psd


def _fake_git(head: str, tree: str, tracked: str, status: str = "", epoch: str = "1700000000"):
    """Stand in for the real `git` so a test does not need a repository."""
    table = {
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "HEAD^{tree}"): tree,
        ("ls-files",): tracked,
        ("status", "--porcelain", "--untracked-files=no"): status,
        ("show", "-s", "--format=%ct", head): epoch,
    }
    return lambda repo, *args: table[args]


def _install_fake_source(monkeypatch, head: str, tree: str, tracked: str,
                         status: str = "", epoch: int = 1700000000, repo: Path | None = None):
    """Stub the Git-object layer as well as the plumbing helper.

    Payload bytes now come from the object database rather than the checkout, so a
    test that only stubbed `psd._git` would no longer describe a whole source. The
    fake tree mirrors `tracked`, and blob bytes are taken from the temp files the
    test already wrote -- which is a stand-in for the object store, not a
    reintroduction of working-tree reads in the packager itself.
    """
    from tools import git_object_source as gos

    paths = [p.strip() for p in tracked.split("\n") if p.strip()]
    monkeypatch.setattr(psd, "_git", _fake_git(head, tree, tracked, status, str(epoch)))
    monkeypatch.setattr(gos, "resolve_commit", lambda r, revision="HEAD": (head, tree, epoch))

    def _read_tree(r, commit):
        entries = []
        for rel in paths:
            candidate = Path(repo or r) / rel
            size = candidate.stat().st_size if candidate.is_file() else 0
            entries.append(gos.TreeEntry(path=rel, mode=gos.REGULAR_FILE,
                                         object_type="blob", oid=f"{abs(hash(rel)):040x}"[:40],
                                         size=size))
        entries.sort(key=lambda e: e.path)
        return entries

    monkeypatch.setattr(gos, "read_tree", _read_tree)

    class _Reader:
        def __init__(self, r):
            self._root = Path(repo or r)

        def read(self, entry):
            return (self._root / entry.path).read_bytes()

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    # A synthetic tree is not the tree the committed policy describes, so the
    # real inventory would read as entirely stale here. Give these repos a policy
    # of their own; the fail-closed behaviour is proven against the real one in
    # `tests/test_public_content_policy.py`.
    from tools import public_content_policy as pcp

    synthetic = pcp.ContentPolicy(
        version=pcp.SUPPORTED_VERSIONS[0],
        documents={p: "PUBLIC_REQUIRED" for p in paths
                   if PurePosixPath(p).suffix.lower() in {".md", ".txt"} or PurePosixPath(p).name == "LICENSE"},
        document_rules={},
        extensions=frozenset({".md", ".txt"}),
        exact_names=frozenset({"LICENSE"}),
    )
    monkeypatch.setattr(psd.public_content_policy, "validate_against",
                        lambda tracked, policy=None: synthetic)

    monkeypatch.setattr(gos, "BlobReader", _Reader)
    monkeypatch.setattr(psd.git_object_source, "resolve_commit", gos.resolve_commit)
    monkeypatch.setattr(psd.git_object_source, "read_tree", gos.read_tree)
    monkeypatch.setattr(psd.git_object_source, "BlobReader", gos.BlobReader)


"""Derived from the real requirement list so the two cannot drift apart."""
_MINIMAL_TRACKED = "".join(f"{p}\n" for p in psd.REQUIRED_PATHS)


def _minimal_repo(tmp_path: Path) -> Path:
    """A tree holding exactly the paths `REQUIRED_PATHS` demands."""
    repo = tmp_path / "repo"
    for rel in _MINIMAL_TRACKED.split():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return repo


class TestClassification:
    def test_local_agent_settings_and_private_captures_do_not_ship(self) -> None:
        private = [".gemini/settings.json", ".gemini/settings.local.json",
                   "design/stitch/manual-export-inventory.json",
                   "Source/AI Research Agent.png", "design/input/current-dashboard-wide.png"]
        source = ["AGENTS.md", "tests/test_auth_foundation.py", "desktop/src-tauri/icons/icon.ico",
                  "extensions/whale-collector/manifest.json"]
        included, excluded, prohibited, fatal = psd.classify(private + source)
        assert included == source
        assert {path for path, _reason in excluded} == set(private)
        assert not prohibited and not fatal

    def test_ordinary_source_is_included(self) -> None:
        included, _excluded, prohibited, fatal = psd.classify(
            ["app/main.py", "web/src/App.tsx", "README.md", "pyproject.toml"]
        )
        assert included == ["app/main.py", "web/src/App.tsx", "README.md", "pyproject.toml"]
        assert not prohibited and not fatal

    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            ".env.local",
            ".env.production",
            ".mcp.json",
            "storage/metadata_index.sqlite3",
            "storage/scheduler.sqlite3-wal",
            "certs/server.pem",
            "certs/server.key",
            "secrets/id_rsa",
        ],
    )
    def test_secret_and_runtime_database_paths_are_fatal(self, path: str) -> None:
        """These abort the build. Excluding them quietly would hide a real problem."""
        _included, _excluded, _prohibited, fatal = psd.classify([path])
        assert [p for p, _ in fatal] == [path]

    @pytest.mark.parametrize(
        "path",
        [
            "storage/topic_profiles/profiles.json",
            "output/generated/thing.json",
            "reports/XRML_Report.pdf",
            "data/papers/pdf/paper.pdf",
            "bundle.xrmlbundle",
            "addon.zip",
            "run.log",
            "web/node_modules/pkg/index.js",
            "app/__pycache__/x.pyc",
        ],
    )
    def test_runtime_and_generated_paths_are_prohibited_not_included(self, path: str) -> None:
        """Excluded from the archive and surfaced, never silently shipped."""
        included, _excluded, prohibited, _fatal = psd.classify([path])
        assert path not in included
        assert [p for p, _ in prohibited] == [path]

    def test_local_working_state_is_ordinary_exclusion(self) -> None:
        included, excluded, prohibited, fatal = psd.classify(
            ["data/queues/inbox.jsonl", "notes/weekly/2026-W01.md"]
        )
        assert not included and not prohibited and not fatal
        assert len(excluded) == 2

    @pytest.mark.parametrize(
        "path",
        [
            # Real configuration is fatal wherever it sits, not only at the root.
            "app/.env",
            "config/nested/.env",
            "sub/.mcp.json",
            # ...and under any suffix, including ones nobody enumerated.
            ".env.staging",
            ".env.development",
            ".env.local.backup",
            # ...and in any case, because git on Windows and macOS folds case.
            ".ENV",
            "app/.MCP.JSON",
            "Secret.PEM",
            "DATA.SQLITE3",
            "deep/CERT.KEY",
        ],
    )
    def test_secrets_are_fatal_at_any_depth_suffix_and_case(self, path: str) -> None:
        _included, _excluded, _prohibited, fatal = psd.classify([path])
        assert [p for p, _ in fatal] == [path]

    @pytest.mark.parametrize(
        "path",
        [
            # The `**/*.ext` patterns must fire at every depth, not just the root.
            "deep/nested/archive.zip",
            "a/b/c/d/e/run.log",
            "a/b/c/trace.trace.zip",
            "a/b/clip.webm",
            "a/b/c/paper.pdf",
            "a/b/pkg.xrmlbundle",
            "x/y/z/db.sqlite3",
        ],
    )
    def test_extension_patterns_fire_at_any_depth(self, path: str) -> None:
        included, _excluded, _prohibited, _fatal = psd.classify([path])
        assert path not in included

    @pytest.mark.parametrize("path", ["x.Zip", "REPORT.PDF", "Trace.WEBM"])
    def test_prohibited_matching_folds_case_too(self, path: str) -> None:
        included, _excluded, prohibited, _fatal = psd.classify([path])
        assert path not in included
        assert [p for p, _ in prohibited] == [path]

    @pytest.mark.parametrize("path", [".env.example", ".env.sample", ".mcp.json.template", ".env.dist"])
    def test_placeholder_configuration_survives_the_secret_rule(self, path: str) -> None:
        """The stem rule must not swallow the files that document the config shape."""
        included, _excluded, prohibited, fatal = psd.classify([path])
        assert included == [path]
        assert not prohibited and not fatal

    def test_example_configuration_is_shippable(self) -> None:
        """The examples are the public config contract and must survive the filter."""
        included, _excluded, prohibited, fatal = psd.classify([".env.example", ".mcp.json.example"])
        assert included == [".env.example", ".mcp.json.example"]
        assert not prohibited and not fatal


class TestBuild:
    def test_build_refuses_when_a_fatal_path_is_tracked(self, monkeypatch, tmp_path: Path) -> None:
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", "app/main.py\n.env\n", repo=None)
        with pytest.raises(psd.PackagingError, match="must never exist"):
            psd.build(tmp_path, None, check_only=True)

    def test_build_refuses_when_a_required_source_path_is_missing(self, monkeypatch, tmp_path: Path) -> None:
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", "app/main.py\n", repo=None)
        with pytest.raises(psd.PackagingError, match="required source paths missing"):
            psd.build(tmp_path, None, check_only=True)

    def test_output_directory_inside_the_repository_is_refused(self, monkeypatch, tmp_path: Path) -> None:
        """Writing the archive into the tree would feed the next build its own output."""
        repo = _minimal_repo(tmp_path)
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, repo=repo)
        with pytest.raises(psd.PackagingError, match="outside the repository"):
            psd.build(repo, repo / "dist")

    def test_archive_round_trip_is_safe_and_manifest_is_adjacent(self, monkeypatch, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        out = tmp_path / "out"
        files = {rel: f"content of {rel}\n" for rel in psd.REQUIRED_PATHS}
        for rel, body in files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        _install_fake_source(monkeypatch, "0123456789abcdef", "fedcba9876543210", "\n".join(files) + "\n", repo=repo)

        result = psd.build(repo, out)

        assert result.archive_path is not None and result.archive_path.is_file()
        assert result.manifest_path is not None and result.manifest_path.is_file()
        # the manifest records the archive hash, so it must not live inside it
        with tarfile.open(result.archive_path) as tar:
            names = tar.getnames()
        assert result.manifest_path.name not in names
        for name in names:
            assert not name.startswith("/")
            assert ".." not in Path(name).parts
        assert len(names) == len(files)
        assert len(set(names)) == len(names), "duplicate archive entries"

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["base_head"] == "0123456789abcdef"
        assert manifest["base_tree"] == "fedcba9876543210"
        assert manifest["file_count"] == len(files)
        assert manifest["manifest_in_archive"] is False
        assert manifest["is_official_release"] is False
        assert manifest["archive_sha256"] == result.archive_sha256

    def test_a_modified_tracked_file_blocks_the_build(self, monkeypatch, tmp_path: Path) -> None:
        """`base_head` must describe the archived bytes, so a dirty tree is refused."""
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, status=" M pyproject.toml\n", repo=None)
        with pytest.raises(psd.PackagingError, match="tracked files are modified"):
            psd.build(tmp_path, None, check_only=True)

    def test_allow_dirty_overrides_the_clean_tree_guard(self, monkeypatch, tmp_path: Path) -> None:
        repo = _minimal_repo(tmp_path)
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, status=" M pyproject.toml\n", repo=repo)
        result = psd.build(repo, tmp_path / "out", allow_dirty=True)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["built_from_clean_tree"] is False

    def test_a_clean_build_is_recorded_as_clean(self, monkeypatch, tmp_path: Path) -> None:
        repo = _minimal_repo(tmp_path)
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, repo=repo)
        result = psd.build(repo, tmp_path / "out")
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["built_from_clean_tree"] is True

    def test_a_symlink_escaping_the_archive_blocks_the_build(self, monkeypatch, tmp_path: Path) -> None:
        """The link target would be written into a public archive verbatim.

        Symlinks are now judged by the tree entry (mode 120000) and the target held
        in its blob, not by asking the filesystem — a checkout may not even be able
        to represent a symlink, and the archive must be correct either way.
        """
        from tools import git_object_source as gos

        repo = _minimal_repo(tmp_path)
        (repo / "app" / "link.py").write_bytes(b"../../../../etc/passwd")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + "app/link.py\n", repo=repo)
        real_read_tree = gos.read_tree

        def _tree_with_symlink(r, commit):
            return [
                gos.TreeEntry(path=e.path, mode=(gos.SYMLINK if e.path == "app/link.py" else e.mode),
                              object_type="blob", oid=e.oid, size=e.size)
                for e in real_read_tree(r, commit)
            ]

        monkeypatch.setattr(gos, "read_tree", _tree_with_symlink)
        monkeypatch.setattr(psd.git_object_source, "read_tree", _tree_with_symlink)
        with pytest.raises(psd.PackagingError, match="points outside the archive"):
            psd.build(repo, tmp_path / "out")

    def test_a_safe_symlink_is_archived_without_dereferencing(self, monkeypatch, tmp_path: Path) -> None:
        from tools import git_object_source as gos

        repo = _minimal_repo(tmp_path)
        (repo / "app" / "link.py").write_bytes(b"main.py")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + "app/link.py\n", repo=repo)
        real_read_tree = gos.read_tree

        def _tree_with_symlink(r, commit):
            return [
                gos.TreeEntry(path=e.path, mode=(gos.SYMLINK if e.path == "app/link.py" else e.mode),
                              object_type="blob", oid=e.oid, size=e.size)
                for e in real_read_tree(r, commit)
            ]

        monkeypatch.setattr(gos, "read_tree", _tree_with_symlink)
        monkeypatch.setattr(psd.git_object_source, "read_tree", _tree_with_symlink)
        result = psd.build(repo, tmp_path / "out")
        with tarfile.open(result.archive_path) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("app/link.py"))
        assert member.issym() and member.linkname == "main.py"

    def test_two_builds_of_the_same_commit_are_byte_identical(self, monkeypatch, tmp_path: Path) -> None:
        """Reproducibility is the whole point of a reviewable candidate.

        If two builds of one commit differ, an independent rebuild cannot be used
        to check what was published, and the archive hash means nothing.
        """
        repo = _minimal_repo(tmp_path)
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, repo=repo)

        first = psd.build(repo, tmp_path / "out-a")
        second = psd.build(repo, tmp_path / "out-b")

        assert first.archive_sha256 == second.archive_sha256
        assert first.archive_bytes == second.archive_bytes
        assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
        assert first.archive_path.name == second.archive_path.name, "filename must not carry a clock"

        a = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        b = json.loads(second.manifest_path.read_text(encoding="utf-8"))
        assert a["package_content_digest"] == b["package_content_digest"]
        assert a["source_date_epoch"] == b["source_date_epoch"] == 1700000000
        # `created_utc` is external run metadata and is allowed to differ.
        assert {k: v for k, v in a.items() if k != "created_utc"} == \
               {k: v for k, v in b.items() if k != "created_utc"}

    def test_archive_carries_no_host_identity_or_clock(self, monkeypatch, tmp_path: Path) -> None:
        repo = _minimal_repo(tmp_path)
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", _MINIMAL_TRACKED, repo=repo)
        result = psd.build(repo, tmp_path / "out")
        with tarfile.open(result.archive_path) as tar:
            for member in tar.getmembers():
                assert member.uid == member.gid == 0
                assert member.uname == member.gname == ""
                assert member.mtime == 1700000000, "member times must come from the commit"
                assert member.mode == 0o644
        # gzip stores an mtime in its header; a wall-clock value breaks reproducibility
        raw = result.archive_path.read_bytes()
        assert int.from_bytes(raw[4:8], "little") == 1700000000

    def test_license_is_required_in_the_candidate(self, monkeypatch, tmp_path: Path) -> None:
        """A source distribution without its terms is not redistributable."""
        without_license = "".join(f"{p}\n" for p in psd.REQUIRED_PATHS if p != "LICENSE")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe", without_license, repo=None)
        with pytest.raises(psd.PackagingError, match="LICENSE"):
            psd.build(tmp_path, None, check_only=True)


def _tier(path: str) -> str:
    """Which tier `path` lands in -- the shape every rule below is stated in."""
    included, excluded, prohibited, fatal = psd.classify([path])
    if path in included:
        return "included"
    for name, rows in (("fatal", fatal), ("prohibited", prohibited), ("excluded", excluded)):
        if path in [p for p, _ in rows]:
            return name
    raise AssertionError(f"{path!r} landed in no tier")


class TestEnvrcIsActiveConfiguration:
    """`.envrc` is sourced by direnv on directory entry, so shipping one is worse
    than shipping an inert key/value file. Every active form fails closed."""

    @pytest.mark.parametrize("path", [
        ".envrc", "nested/.envrc", "a/b/c/.envrc", ".ENVRC", "nested/.ENVRC",
        ".envrc.local", ".envrc.production", ".envrc.backup", ".envrc.example.bak",
        ".envrc-template", ".envrc.dist", ".envrc.bak", ".ENVRC.LOCAL",
    ])
    def test_active_forms_are_fatal_at_any_depth_and_case(self, path: str) -> None:
        assert _tier(path) == "fatal"

    def test_an_added_suffix_does_not_buy_an_escape(self) -> None:
        """`.dist` is a placeholder suffix for `.env`; it is not one for `.envrc`."""
        assert _tier(".env.dist") == "included"
        assert _tier(".envrc.dist") == "fatal"

    def test_the_build_aborts_and_never_includes_it(self, monkeypatch, tmp_path: Path) -> None:
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + ".envrc\n", repo=None)
        with pytest.raises(psd.PackagingError, match="must never exist"):
            psd.build(tmp_path, None, check_only=True)

    def test_fatal_evidence_names_the_path_but_not_the_contents(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A report that quoted the file would republish whatever leaked."""
        repo = _minimal_repo(tmp_path)
        secret = "SUPER_SECRET_VALUE_98217"
        (repo / ".envrc").write_text(f'export TOKEN="{secret}"\n', encoding="utf-8")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + ".envrc\n", repo=repo)
        with pytest.raises(psd.PackagingError) as raised:
            psd.build(repo, None, check_only=True)
        assert ".envrc" in str(raised.value)
        assert secret not in str(raised.value)


class TestEnvrcExampleAllowlist:
    @pytest.mark.parametrize("path", [
        ".envrc.example", ".envrc.sample", ".envrc.template",
        "nested/.envrc.example", ".ENVRC.EXAMPLE",
    ])
    def test_the_exact_example_forms_may_ship(self, path: str) -> None:
        assert _tier(path) == "included"

    def test_the_allowlist_is_exact_not_a_broad_carve_out(self) -> None:
        assert psd.ENVRC_EXAMPLE_NAMES == {".envrc.example", ".envrc.sample", ".envrc.template"}
        for near_miss in (".envrc.examples", ".envrc.example2", ".envrc.template.old"):
            assert _tier(near_miss) == "fatal", near_miss

    @pytest.mark.parametrize("body,rule", [
        (b'export API_TOKEN="ghp_ABCDEFGHIJKLMNOPQRSTUV"\n', "github-token"),
        (b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n", "private-key-block"),
        (b'export CLIENT_SECRET="hunter2realvalue"\n', "credential-like-value"),
        (b"export AWS_ID=AKIAIOSFODNN7EXAMPLE\n", "aws-access-key"),
        (b"export XRML_DATA_DIR=C:\\Users\\USER\\dev\\repos\n", "owner-machine-path"),
    ])
    def test_a_real_looking_value_is_rejected(self, body: bytes, rule: str) -> None:
        assert rule in psd.example_config_findings(body)

    @pytest.mark.parametrize("body", [
        b"export SESSION_SECRET=\nexport AUTH_MODE=disabled\n",
        b"export API_KEY=<YOUR_KEY_HERE>\n",
        b"export TOKEN=${MY_TOKEN}\n",
        b"export PASSWORD=changeme\n",
        b'{\n  "mcpServers": {"x": {"command": "uv"}}\n}\n',
    ])
    def test_a_genuine_placeholder_is_accepted(self, body: bytes) -> None:
        assert psd.example_config_findings(body) == []

    @pytest.mark.parametrize("body", [
        b"API_KEY=changeme_actual_secret_12345\n",
        b"SESSION_SECRET=none_of_your_business_realvalue\n",
        b"API_TOKEN=examplerealtokenvalue123456\n",
        b"PASSWORD=todoreplacewithrealpasswordXYZ\n",
    ])
    def test_a_placeholder_prefix_does_not_excuse_the_rest(self, body: bytes) -> None:
        """The placeholder must be the whole value, not its first word.

        Prefix matching let a half-edited example ship: the author replaced the
        tail of `changeme` with a live key and the file still read as a template.
        """
        assert "credential-like-value" in psd.example_config_findings(body)

    def test_an_indented_json_example_is_parsed_too(self) -> None:
        assert psd.example_config_findings(b'  "api_key": "sk-live-ABCDEFGHIJKLMNOP"\n')

    def test_an_empty_value_does_not_borrow_the_next_line(self) -> None:
        """`\\s*` after the separator matches a newline; that flagged the real file."""
        assert psd.example_config_findings(b"SESSION_SECRET=\nAPP_BASE_URL=http://127.0.0.1:8000\n") == []

    def test_the_findings_are_rule_ids_never_the_matched_value(self) -> None:
        leaked = "ghp_ABCDEFGHIJKLMNOPQRSTUV"
        findings = psd.example_config_findings(f'TOKEN="{leaked}"\n'.encode())
        assert findings and all(leaked not in f for f in findings)

    def test_an_unsafe_example_aborts_the_build(self, monkeypatch, tmp_path: Path) -> None:
        repo = _minimal_repo(tmp_path)
        (repo / ".envrc.example").write_text('export TOKEN="ghp_ABCDEFGHIJKLMNOPQRSTUV"\n',
                                             encoding="utf-8")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + ".envrc.example\n", repo=repo)
        with pytest.raises(psd.PackagingError, match="no real values"):
            psd.build(repo, None, check_only=True)

    def test_a_safe_example_survives_the_same_build(self, monkeypatch, tmp_path: Path) -> None:
        repo = _minimal_repo(tmp_path)
        (repo / ".envrc.example").write_text("export SESSION_SECRET=\n", encoding="utf-8")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + ".envrc.example\n", repo=repo)
        assert ".envrc.example" in psd.build(repo, None, check_only=True).included

    def test_the_example_vocabulary_is_not_weaker_than_the_products(self) -> None:
        """The packager must not become the soft spot the product already covers."""
        from app.services import evidence_manifest_admission as admission

        assert set(admission._SECRET_KEYS) <= set(psd.SECRET_KEY_WORDS)
        for pattern in admission._SECRET_VALUE_PATTERNS:
            probe = {
                r"\bsk-(?:live|test|proj)?-[A-Za-z0-9_-]{8,}\b": b"K=sk-live-ABCDEFGHIJKLMNOP\n",
                r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}\b": b"K=ghp_ABCDEFGHIJKLMNOP\n",
                r"\bAKIA[0-9A-Z]{16}\b": b"K=AKIAIOSFODNN7EXAMPLE\n",
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----": b"-----BEGIN RSA PRIVATE KEY-----\n",
                r"\bBearer\s+[A-Za-z0-9._-]{12,}\b": b"K=Bearer abcdefghijklmnop\n",
            }[pattern.pattern]
            assert psd.example_config_findings(probe), f"packager misses {pattern.pattern}"


class TestExistingConfigurationControlsStayArmed:
    @pytest.mark.parametrize("path", [
        ".env", ".env.local", ".env.staging", ".env.production", ".ENV",
        "app/.env", "nested/deep/.env", ".mcp.json", "nested/.mcp.json", ".MCP.JSON",
    ])
    def test_real_configuration_remains_fatal(self, path: str) -> None:
        assert _tier(path) == "fatal"

    @pytest.mark.parametrize("path", [".env.example", ".mcp.json.example", ".env.sample"])
    def test_exact_placeholders_remain_permitted(self, path: str) -> None:
        assert _tier(path) == "included"


class TestTrackedExecutablesAreProhibited:
    @pytest.mark.parametrize("path", [
        "tool.exe", "nested/tool.exe", "a/b/c/tool.exe", "nested/tool.EXE",
        "name.with.dots.exe", "sample.txt.exe", "Setup.Exe",
    ])
    def test_exe_is_prohibited_at_any_depth_and_case(self, path: str) -> None:
        assert _tier(path) == "prohibited"

    @pytest.mark.parametrize("path", [
        "tool.exe.txt", "docs/exe.md", "app/runner.py", "docs/exe-guide.md", "executable.md",
    ])
    def test_a_name_that_merely_mentions_exe_is_not_a_binary(self, path: str) -> None:
        assert _tier(path) == "included"

    def test_source_text_naming_an_executable_is_untouched(self, tmp_path: Path) -> None:
        """The rule reads the path, so file *contents* cannot trigger it."""
        assert _tier("app/runner.py") == "included"
        assert psd.example_config_findings(b'SUBPROCESS = "tool.exe"\n') == []

    def test_it_is_reported_exactly_once_and_not_as_ordinary_exclusion(self) -> None:
        paths = ["tool.exe", "nested/helper.EXE", "app/main.py"]
        included, excluded, prohibited, fatal = psd.classify(paths)
        assert [p for p, _ in prohibited] == ["tool.exe", "nested/helper.EXE"]
        assert excluded == [] and fatal == []
        assert included == ["app/main.py"]
        assert len(prohibited) == len({p for p, _ in prohibited})

    def test_it_is_absent_from_the_archive(self, monkeypatch, tmp_path: Path) -> None:
        repo = _minimal_repo(tmp_path)
        (repo / "tool.exe").write_bytes(b"MZ\x90\x00INERT-SYNTHETIC")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + "tool.exe\n", repo=repo)
        result = psd.build(repo, tmp_path / "out")
        with tarfile.open(result.archive_path) as tar:
            assert not [n for n in tar.getnames() if n.lower().endswith(".exe")]
        assert [p for p, _ in result.prohibited] == ["tool.exe"]

    def test_its_payload_is_never_read(self, monkeypatch, tmp_path: Path) -> None:
        """Path classification precedes the blob reader, so the bytes stay unopened.

        This is the property that makes the rule safe: the packager never has to
        touch -- let alone run -- a binary it cannot vouch for.
        """
        repo = _minimal_repo(tmp_path)
        (repo / "tool.exe").write_bytes(b"MZ\x90\x00INERT-SYNTHETIC")
        _install_fake_source(monkeypatch, "deadbeef", "cafebabe",
                             _MINIMAL_TRACKED + "tool.exe\n", repo=repo)
        from tools import git_object_source as gos

        opened: list[str] = []
        original = gos.BlobReader

        class _Watched(original):  # type: ignore[misc,valid-type]
            def read(self, entry):
                opened.append(entry.path)
                if entry.path.lower().endswith(".exe"):
                    raise AssertionError(f"packager opened a prohibited binary: {entry.path}")
                return super().read(entry)

        monkeypatch.setattr(gos, "BlobReader", _Watched)
        monkeypatch.setattr(psd.git_object_source, "BlobReader", _Watched)
        psd.build(repo, tmp_path / "out")
        assert "tool.exe" not in opened

    def test_a_symlink_named_exe_is_stopped_by_its_path_first(self) -> None:
        """Classification is path-only, so it lands before any target handling."""
        assert _tier("nested/link.exe") == "prohibited"

    def test_the_rule_claims_only_exe(self) -> None:
        """A wider native-binary group needs its own inventory and owner decision."""
        for other in ("tool.dll", "installer.msi", "legacy.com", "screensaver.scr"):
            assert _tier(other) == "included", other

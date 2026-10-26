"""Synthetic coverage for the central run-id / artifact-path containment layer.

Every fixture is an OS temp / pytest ``tmp_path`` tree. No canonical
``storage/**`` content is read or used. The autouse conftest isolation already
redirects the storage roots into ``tmp_path`` for every test here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.domain import path_safety as ps
from app.repositories import artifact_refs, runs as run_repository

# ---------------------------------------------------------------------------
# run_id validator - accepted
# ---------------------------------------------------------------------------

ACCEPTED_RUN_IDS = [
    "20260403-100702-m3-validation-run",  # current generator output shape
    "20260724-060846-ar-slam",
    "run-20260724T060846Z",  # uppercase T/Z legacy stamp
    "h-008d115",
    "550e8400-e29b-41d4-a716-446655440000",  # uuid4 form
    "run-1",
    "portable_write.run.v2",  # hyphen + underscore + interior dots
    "deterministic-quality-run",
    ".hidden-but-safe",  # leading dot is a valid hidden segment, not "." / ".."
]


@pytest.mark.parametrize("run_id", ACCEPTED_RUN_IDS)
def test_validate_run_id_accepts_safe_identity(run_id: str) -> None:
    assert ps.validate_run_id(run_id) == run_id
    assert ps.is_safe_run_id(run_id) is True


REJECTED_RUN_IDS = [
    "",
    "   ",
    " run",
    "run ",
    ".",
    "..",
    "../run",
    "run/child",
    "run\\child",
    "/absolute",
    "C:\\absolute",
    "C:/absolute",
    "C:relative",
    "\\\\server\\share",
    "\\\\?\\C:\\path",
    "\\\\.\\device",
    "NUL",
    "CON",
    "CON.txt",
    "COM1",
    "LPT9.log",
    "file:stream",
    "run.",  # trailing dot
    "run id",  # interior space
    "run\x00id",  # embedded NUL
    "run\rid",
    "run\nid",
    "run\x07id",  # other control char
    "%2e%2e",  # encoded dot traversal
    "%2f",  # encoded slash
    "%5c",  # encoded backslash
    "%252e%252e",  # double-encoded traversal
    "run%",  # malformed / bare percent
    "a" * (ps.MAX_RUN_ID_LENGTH + 1),  # over max length
]


@pytest.mark.parametrize("run_id", REJECTED_RUN_IDS)
def test_validate_run_id_rejects_unsafe(run_id: str) -> None:
    assert ps.is_safe_run_id(run_id) is False
    with pytest.raises(ps.InvalidRunId):
        ps.validate_run_id(run_id)


def test_validate_run_id_rejects_non_string() -> None:
    with pytest.raises(ps.InvalidRunId):
        ps.validate_run_id(None)
    with pytest.raises(ps.InvalidRunId):
        ps.validate_run_id(123)


def test_generated_run_id_passes_validator() -> None:
    generated = run_repository.make_run_id("AR/XR SLAM with 6DoF!!")
    assert ps.validate_run_id(generated) == generated


# ---------------------------------------------------------------------------
# artifact relative-path validator
# ---------------------------------------------------------------------------

ACCEPTED_ARTIFACT_PATHS = [
    "reports/weekly_journal.md",
    "papers/2604.20598v1/review.json",
    "evidence/spans.jsonl",
    "reports/research_plan.json",
    "run.json",
]


@pytest.mark.parametrize("value", ACCEPTED_ARTIFACT_PATHS)
def test_validate_artifact_path_accepts(value: str) -> None:
    assert ps.validate_artifact_relative_path(value) == value
    assert ps.is_safe_artifact_relative_path(value) is True


REJECTED_ARTIFACT_PATHS = [
    "/absolute/file",
    "//host/path",
    "C:\\file",
    "C:file",
    "\\\\server\\share\\f",
    "\\\\.\\device",
    "../file",
    "a/../file",
    "a/./file",
    "a//file",
    "a\\file",
    "",
    " a/b",
    "a/ b",  # trailing/leading space segment
    "a/b ",
    "a/.",
    "a/..",
    "%2e%2e/passwd",
    "a/%2ffile",
    "a/%5cfile",
    "a/%252e%252e",
    "a:stream/file",
    "a/b:stream",
    "reports\x00/x",
    "reports\n/x",
    "reports/CON",
    "reports/CON.txt",
    "reports/name.",  # trailing dot segment
    "a/b?query=1",
    "a/b#frag",
    "x/" + "y" * (ps.MAX_ARTIFACT_SEGMENT_LENGTH + 1),
    "a/" * 600 + "b",  # over total length
]


@pytest.mark.parametrize("value", REJECTED_ARTIFACT_PATHS)
def test_validate_artifact_path_rejects(value: str) -> None:
    assert ps.is_safe_artifact_relative_path(value) is False
    with pytest.raises(ps.InvalidArtifactPath):
        ps.validate_artifact_relative_path(value)


# ---------------------------------------------------------------------------
# containment resolver
# ---------------------------------------------------------------------------


def test_resolve_within_root_contains_safe_read(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    target = root / "reports"
    target.mkdir()
    (target / "preview.md").write_text("ok", encoding="utf-8")
    resolved = ps.resolve_within_root(root, "reports/preview.md", must_exist=True)
    assert resolved == (root / "reports" / "preview.md")


def test_resolve_within_root_allows_nonexistent_write_target(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    resolved = ps.resolve_within_root(root, "reports/new.md")
    assert resolved == (root / "reports" / "new.md")
    # Pure resolution never creates anything.
    assert not resolved.exists()
    assert not (root / "reports").exists()


def test_resolve_within_root_blocks_sibling_prefix_collision(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (tmp_path / "runs-evil").mkdir()
    # A relative path can never reach the sibling, but prove the check is not a
    # string prefix by confirming ".../runs-evil" is not considered inside root.
    evil = tmp_path / "runs-evil" / "secret.md"
    with pytest.raises(ps.PathSafetyError):
        # A traversal toward the sibling is rejected; containment is identity
        # based (relative_to), so "runs-evil" is never treated as inside "runs".
        ps.resolve_within_root(root, "../runs-evil/secret.md")
    assert not evil.exists()


def test_resolve_within_root_blocks_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    with pytest.raises(ps.InvalidArtifactPath):
        ps.resolve_within_root(root, "../outside.md")


def test_resolve_within_root_blocks_absolute(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    with pytest.raises(ps.InvalidArtifactPath):
        ps.resolve_within_root(root, "C:/Windows/system.ini")


def test_is_reparse_point_false_for_regular(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert ps.is_reparse_point(d) is False
    assert ps.is_reparse_point(tmp_path / "file.txt") is False
    assert ps.is_reparse_point(tmp_path / "missing") is False


def test_reparse_component_rejected_via_monkeypatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic (privilege-independent) reparse rejection."""
    root = tmp_path / "runs"
    (root / "linkdir").mkdir(parents=True)
    marked = (root / "linkdir").resolve()

    def fake_is_reparse(path: str | os.PathLike[str]) -> bool:
        return Path(path).resolve() == marked

    monkeypatch.setattr(ps, "is_reparse_point", fake_is_reparse)
    with pytest.raises(ps.PathContainmentError):
        ps.resolve_within_root(root, "linkdir/report.md")


def test_reparse_final_target_rejected_via_monkeypatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    target = (root / "report.md")
    target.write_text("x", encoding="utf-8")
    resolved_target = target.resolve()

    def fake_is_reparse(path: str | os.PathLike[str]) -> bool:
        return Path(path).resolve() == resolved_target

    monkeypatch.setattr(ps, "is_reparse_point", fake_is_reparse)
    with pytest.raises(ps.PathContainmentError):
        ps.resolve_within_root(root, "report.md")


def test_real_symlink_escape_blocked(tmp_path: Path) -> None:
    """Real symlink escape is blocked; skipped only if the OS denies symlinks."""
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    link = root / "reports"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"symlink unsupported in this environment: {exc!r}")
    with pytest.raises(ps.PathContainmentError):
        ps.resolve_within_root(root, "reports/secret.md")


# ---------------------------------------------------------------------------
# repository chokepoint (build_run_paths / ensure_run_layout)
# ---------------------------------------------------------------------------


def test_build_run_paths_rejects_malformed_before_any_fs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    storage = tmp_path / "storage_runs"
    storage.mkdir()
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage)
    for bad in ["../evil", "C:\\Windows", "\\\\srv\\share", "a/b", "CON", "run:stream"]:
        with pytest.raises(ps.InvalidRunId):
            run_repository.build_run_paths(bad)
    # No directory or file was created for any rejected identifier.
    assert list(storage.iterdir()) == []


def test_ensure_run_layout_creates_for_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    storage = tmp_path / "storage_runs"
    storage.mkdir()
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage)
    paths = run_repository.ensure_run_layout("safe-run-1")
    assert paths.run_dir == storage / "safe-run-1"
    assert paths.run_dir.is_dir()


def test_ensure_run_layout_rejects_reparse_run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    storage = tmp_path / "storage_runs"
    storage.mkdir()
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage)

    def fake_is_reparse(path: str | os.PathLike[str]) -> bool:
        return Path(path).name == "junction-run"

    monkeypatch.setattr(run_repository, "is_reparse_point", fake_is_reparse)
    with pytest.raises(ps.PathContainmentError):
        run_repository.ensure_run_layout("junction-run")


def test_load_run_record_unknown_valid_id_raises_filenotfound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    storage = tmp_path / "storage_runs"
    storage.mkdir()
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage)
    # Syntactically valid but unknown -> FileNotFoundError (preserved 404 path),
    # not InvalidRunId.
    with pytest.raises(FileNotFoundError):
        run_repository.load_run_record("valid-but-unknown-run")


# ---------------------------------------------------------------------------
# artifact_refs fail-closed on invalid run_id
# ---------------------------------------------------------------------------


def test_run_root_fails_closed_on_invalid_run_id() -> None:
    assert artifact_refs._run_root("../evil") is None
    assert artifact_refs._run_root("a/b") is None
    assert artifact_refs._run_root("safe-run") is not None


def test_resolve_ref_with_invalid_run_id_is_neutralized() -> None:
    resolution = artifact_refs.resolve_artifact_reference("run:reports/preview.md", run_id="../evil")
    # The malicious run_id must never appear as a resolved path component: the
    # fail-closed _run_root drops it instead of joining STORAGE_ROOT / "../evil".
    assert "evil" not in (resolution.resolved_path or "")


# ---------------------------------------------------------------------------
# API boundary (redacted 4xx, no reflection, unknown-valid still 404)
# ---------------------------------------------------------------------------


def test_api_malformed_run_id_is_redacted_400(client) -> None:
    resp = client.get("/runs/run:zzevilzz")
    assert resp.status_code == 400
    body = resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_run_id"
    assert detail["field"] == "run_id"
    # Raw offending token is never reflected; no absolute local path leaks.
    assert "zzevilzz" not in body
    assert "storage" not in body.lower()
    assert ":\\" not in body and ":/" not in body


def test_api_reserved_name_run_id_400(client) -> None:
    resp = client.get("/runs/CON.txt")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_run_id"


def test_api_unknown_valid_run_id_reaches_normal_not_found(client) -> None:
    # A syntactically valid but unknown run id must flow to the pre-existing
    # not-found path (compare returns 404), never to the new 400 validator.
    resp = client.get("/compare?left_run_id=unknown-valid-a&right_run_id=unknown-valid-b")
    assert resp.status_code == 404
    detail = resp.json().get("detail")
    assert not (isinstance(detail, dict) and detail.get("code") == "invalid_run_id")


def test_api_encoded_traversal_not_5xx(client) -> None:
    # However the ASGI stack decodes it, the result must never be a 5xx and must
    # never reflect an absolute path.
    resp = client.get("/runs/%2e%2e")
    assert resp.status_code < 500
    assert ":\\" not in resp.text and ":/" not in resp.text


def test_api_download_malformed_run_id_400(client) -> None:
    resp = client.get("/runs/run:evil/exports/some-export/download")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_run_id"

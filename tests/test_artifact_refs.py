from __future__ import annotations

import json
import hashlib

import pytest

from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews
from app.services import paper_review


def _long_text(label: str) -> str:
    return " ".join([f"{label} portable full text evidence remains available after relocation."] * 20)


def _file_sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_payload(**overrides):
    payload = {
        "topic_id": "portable-evidence-topic",
        "run_id": "portable-run",
        "paper_id": "portable-paper",
        "title": "Portable Evidence Paper",
        "source_provider": "fixture",
        "abstract_text": "Portable evidence path fixture.",
        "text_excerpt": "",
        "full_text_status": "reviewed",
        "relevance_score": 88.0,
        "novelty_score": 62.0,
        "evidence_strength": 71.0,
        "method_summary": "Fixture method summary.",
        "key_findings": ["Portable evidence works."],
        "limitations": ["Fixture only."],
        "why_it_matters": "It proves local evidence references can move roots.",
        "topic_fit_rationale": "Portable evidence topic.",
        "review_model": "deterministic-baseline-v1",
        "reviewer": "deterministic-baseline",
        "reviewed_at": "2026-06-06T00:00:00+00:00",
        "source_text_sha256": "placeholder",
        "full_text_artifact_path": "run:raw/portable-paper.txt",
        "text_char_count": 0,
        "evidence_spans": [{"label": "span-1", "text": "portable full text evidence"}],
        "review_source_type": "full_text",
    }
    payload.update(overrides)
    return payload


# A portable reference is an opaque product identifier, never a native path.
# Every form below must stay outside the accepted grammar on Windows and POSIX
# alike: several of them are only dangerous on one platform, so the contract is
# enforced identically on both rather than per-host.
NON_PORTABLE_REFS = [
    ("traversal", "run:../secrets.txt"),
    ("backslash_traversal", "run:..\\..\\secrets.txt"),
    ("absolute_posix", "/etc/passwd"),
    ("absolute_windows", "C:/Windows/System32/config/SAM"),
    ("absolute_windows_backslash", "C:\\Windows\\System32\\config\\SAM"),
    ("windows_drive_relative", "C:secrets.txt"),
    ("unc", "//attacker/share/secrets.txt"),
    ("unc_backslash", "\\\\attacker\\share\\secrets.txt"),
    ("alternate_data_stream", "run:reports/preview.md:$DATA"),
    ("alternate_data_stream_named", "run:reports/preview.md:hidden"),
    ("unknown_namespace", "evil:secrets.txt"),
    ("malformed_colon", "run:C:/secrets.txt"),
    ("nul_byte", "run:reports/pre\x00view.md"),
]

ACCEPTED_REFS = [
    "run:reports/preview.md",
    "run:raw/paper.txt",
    "storage:runs/portable-run/reports/preview.md",
    "project:docs/notes.md",
]


def _portable_roots(tmp_path):
    repo_root = tmp_path / "checkout"
    storage_root = repo_root / "storage" / "runs"
    storage_root.mkdir(parents=True)
    return repo_root, storage_root


def test_accepted_portable_grammar_parses_and_round_trips_unchanged(tmp_path) -> None:
    repo_root, storage_root = _portable_roots(tmp_path)
    artifact = storage_root / "portable-run" / "reports" / "preview.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(_long_text("round trip"), encoding="utf-8")

    for ref in ACCEPTED_REFS:
        assert artifact_refs.is_portable_ref(ref) is True, ref

    serialized = artifact_refs.normalize_path_to_ref(
        artifact,
        repo_root=repo_root,
        storage_roots=[storage_root],
        run_id="portable-run",
    )
    resolved = artifact_refs.resolve_ref_to_path(
        serialized,
        repo_root=repo_root,
        storage_roots=[storage_root],
        run_id="portable-run",
    )
    reserialized = artifact_refs.normalize_path_to_ref(
        resolved,
        repo_root=repo_root,
        storage_roots=[storage_root],
        run_id="portable-run",
    )

    assert serialized == "run:reports/preview.md"
    assert reserialized == serialized, "parse/serialize round trip must be stable"
    assert "\\" not in serialized, "portable references never carry a native separator"
    assert resolved == artifact.resolve()


@pytest.mark.parametrize(("label", "ref"), NON_PORTABLE_REFS, ids=[label for label, _ in NON_PORTABLE_REFS])
def test_non_portable_references_are_rejected_by_parser_and_resolver(tmp_path, label: str, ref: str) -> None:
    repo_root, storage_root = _portable_roots(tmp_path)

    assert artifact_refs.is_portable_ref(ref) is False, f"{label} must not parse as portable"
    assert (
        artifact_refs.resolve_ref_to_path(
            ref,
            repo_root=repo_root,
            storage_roots=[storage_root],
            run_id="portable-run",
        )
        is None
    ), f"{label} must not resolve to a native path"
    assert (
        artifact_refs.validate_ref_within_allowed_roots(
            ref,
            repo_root=repo_root,
            storage_roots=[storage_root],
            run_id="portable-run",
        )
        is False
    ), f"{label} must fail containment validation"


def test_portable_reference_is_never_a_native_filesystem_path(tmp_path) -> None:
    from pathlib import Path

    repo_root, storage_root = _portable_roots(tmp_path)
    artifact = storage_root / "portable-run" / "reports" / "preview.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(_long_text("opaque"), encoding="utf-8")
    ref = "run:reports/preview.md"

    resolved = artifact_refs.resolve_ref_to_path(
        ref,
        repo_root=repo_root,
        storage_roots=[storage_root],
        run_id="portable-run",
    )

    # The locator is only usable through the resolver. Joining it onto the
    # authorized root the way a caller might if it mistook the locator for a
    # relative path must not reach the artifact, and the native path the
    # resolver returns must never be mistaken for a portable reference itself.
    assert resolved is not None
    assert resolved == artifact.resolve()
    assert resolved.read_text(encoding="utf-8") == artifact.read_text(encoding="utf-8")
    assert Path(resolved) != Path(ref)
    assert not (storage_root / "portable-run" / ref).exists()
    assert artifact_refs.is_portable_ref(str(resolved)) is False


def test_resolved_portable_reference_stays_inside_the_authorized_root(tmp_path) -> None:
    repo_root, storage_root = _portable_roots(tmp_path)
    outside = tmp_path / "outside" / "secrets.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("must never be reachable", encoding="utf-8")
    run_root = storage_root / "portable-run"
    (run_root / "reports").mkdir(parents=True)
    (run_root / "reports" / "preview.md").write_text(_long_text("contained"), encoding="utf-8")

    def resolve(ref: str):
        return artifact_refs.resolve_ref_to_path(
            ref,
            repo_root=repo_root,
            storage_roots=[storage_root],
            run_id="portable-run",
        )

    resolved = resolve("run:reports/preview.md")
    assert resolved is not None
    assert resolved.is_relative_to(run_root.resolve())
    assert not resolved.is_relative_to(outside.parent.resolve())

    # Escape attempts must not resolve at all, and anything the parser does
    # accept must land inside the authorized root rather than beside it.
    for escape in ("run:../outside/secrets.txt", "run:../../outside/secrets.txt", "run:..\\outside\\secrets.txt"):
        assert resolve(escape) is None, f"escape attempt must not resolve: {escape!r}"

    for tolerated in ("run:/reports/preview.md", "run://reports/preview.md", "run:./reports/preview.md"):
        candidate = resolve(tolerated)
        if candidate is not None:
            assert candidate.is_relative_to(run_root.resolve()), f"{tolerated!r} escaped the run root"


def test_backslash_spelled_locator_normalizes_to_one_portable_form(tmp_path) -> None:
    repo_root, storage_root = _portable_roots(tmp_path)
    artifact = storage_root / "portable-run" / "reports" / "preview.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(_long_text("separators"), encoding="utf-8")

    # Spelled with native separators the way a Windows caller would write it,
    # and with posix separators. Both must serialize to one canonical form on
    # every platform, so this control is not vacuous on POSIX.
    posix_spelling = artifact_refs.normalize_path_to_ref(
        artifact.as_posix(),
        repo_root=repo_root,
        storage_roots=[storage_root],
        run_id="portable-run",
    )
    native_spelling = artifact_refs.normalize_path_to_ref(
        str(artifact),
        repo_root=repo_root,
        storage_roots=[storage_root],
        run_id="portable-run",
    )
    backslash_locator = artifact_refs.resolve_artifact_reference(
        "run:reports\\preview.md",
        run_id="portable-run",
    )

    assert posix_spelling == native_spelling == "run:reports/preview.md"
    assert backslash_locator.is_safe
    assert backslash_locator.portable_ref == "run:reports/preview.md"


def test_every_accepted_namespace_round_trips_through_the_resolver(tmp_path) -> None:
    repo_root, storage_root = _portable_roots(tmp_path)
    run_artifact = storage_root / "portable-run" / "reports" / "preview.md"
    storage_artifact = storage_root / "other-run" / "reports" / "preview.md"
    project_artifact = repo_root / "docs" / "notes.md"
    for path in (run_artifact, storage_artifact, project_artifact):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_long_text(path.parent.name), encoding="utf-8")

    cases = {
        "run:reports/preview.md": run_artifact,
        "storage:runs/other-run/reports/preview.md": storage_artifact,
        "project:docs/notes.md": project_artifact,
    }
    for ref, expected in cases.items():
        resolved = artifact_refs.resolve_ref_to_path(
            ref,
            repo_root=repo_root,
            storage_roots=[storage_root],
            run_id="portable-run",
        )
        assert resolved == expected.resolve(), f"{ref!r} must resolve to its artifact"
        assert resolved.read_text(encoding="utf-8") == expected.read_text(encoding="utf-8")


def test_portable_ref_resolves_after_storage_root_relocation(tmp_path) -> None:
    old_repo = tmp_path / "old-checkout"
    new_repo = tmp_path / "new-checkout"
    old_storage = old_repo / "storage" / "runs"
    new_storage = new_repo / "storage" / "runs"
    old_artifact = old_storage / "relocated-run" / "raw" / "paper.txt"
    new_artifact = new_storage / "relocated-run" / "raw" / "paper.txt"
    old_artifact.parent.mkdir(parents=True)
    new_artifact.parent.mkdir(parents=True)
    text = _long_text("relocated")
    old_artifact.write_text(text, encoding="utf-8")
    new_artifact.write_text(text, encoding="utf-8")

    ref = artifact_refs.normalize_path_to_ref(
        old_artifact,
        repo_root=old_repo,
        storage_roots=[old_storage],
        run_id="relocated-run",
    )
    resolved = artifact_refs.resolve_ref_to_path(
        ref,
        repo_root=new_repo,
        storage_roots=[new_storage],
        run_id="relocated-run",
    )

    assert ref == "run:raw/paper.txt"
    assert resolved == new_artifact.resolve()
    assert resolved.read_text(encoding="utf-8") == text


def test_artifact_reference_validation_statuses_and_sha256(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    artifact = storage_root / "portable-run" / "raw" / "paper.txt"
    artifact.parent.mkdir(parents=True)
    text = _long_text("validation")
    artifact.write_text(text, encoding="utf-8")
    nested_artifact = storage_root / "runs" / "nested-run" / "raw" / "paper.txt"
    nested_artifact.parent.mkdir(parents=True)
    nested_artifact.write_text(text, encoding="utf-8")
    raw_hash = artifact_refs.compute_sha256(artifact)

    valid = artifact_refs.validate_artifact_reference("run:raw/paper.txt", run_id="portable-run")
    legacy = artifact_refs.validate_artifact_reference(
        r"C:\old\checkout\storage\runs\portable-run\raw\paper.txt",
        run_id="portable-run",
    )
    missing = artifact_refs.validate_artifact_reference("run:raw/missing.txt", run_id="portable-run")
    traversal = artifact_refs.validate_artifact_reference("run:../secrets.txt", run_id="portable-run")
    mismatch = artifact_refs.validate_artifact_reference(
        "run:raw/paper.txt",
        run_id="portable-run",
        expected_sha256="0" * 64,
    )
    nested_storage = artifact_refs.validate_artifact_reference(
        "storage:runs/runs/nested-run/raw/paper.txt",
        run_id="portable-run",
    )
    nested_absolute = artifact_refs.validate_artifact_reference(str(nested_artifact), run_id="portable-run")
    nested_custom = artifact_refs.resolve_ref_to_path(
        "storage:runs/runs/nested-run/raw/paper.txt",
        repo_root=tmp_path,
        storage_roots=[storage_root],
        run_id="portable-run",
    )

    assert valid.status == "valid_portable"
    assert artifact_refs.validate_sha256(artifact, raw_hash) is True
    assert legacy.status == "convertible_legacy_absolute"
    assert legacy.resolution.portable_ref == "run:raw/paper.txt"
    assert missing.status == "missing_artifact"
    assert traversal.status == "outside_allowed_roots"
    assert mismatch.status == "hash_mismatch"
    assert nested_storage.status == "nested_storage_namespace"
    assert nested_storage.resolution.is_safe is False
    assert nested_absolute.status == "nested_storage_namespace"
    assert nested_custom is None
    assert artifact_refs.is_portable_ref("run:raw/paper.txt") is True
    assert artifact_refs.is_portable_ref("storage:runs/runs/nested-run/raw/paper.txt") is False
    assert artifact_refs.is_legacy_absolute_path(r"C:\old\checkout\storage\runs\portable-run\raw\paper.txt") is True


def test_artifact_refs_cli_reports_hash_mismatch_and_requires_backup_for_apply(tmp_path, monkeypatch, capsys) -> None:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)

    good_text = _long_text("good")
    bad_text = _long_text("bad")
    good_artifact = storage_root / "portable-run" / "raw" / "good.txt"
    bad_artifact = storage_root / "portable-run" / "raw" / "bad.txt"
    good_artifact.parent.mkdir(parents=True)
    good_artifact.write_text(good_text, encoding="utf-8")
    bad_artifact.write_text(bad_text, encoding="utf-8")
    good_hash = paper_review._sha256_text(good_text)

    paper_reviews.upsert_review(
        _review_payload(
            paper_id="good",
            full_text_artifact_path="run:raw/good.txt",
            source_text_sha256=good_hash,
            text_char_count=len(good_text),
            evidence_spans=[{"label": "good-span", "text": "good portable full text evidence"}],
        )
    )
    paper_reviews.upsert_review(
        _review_payload(
            paper_id="bad",
            full_text_artifact_path="run:raw/bad.txt",
            source_text_sha256="wrong-source-text-sha",
            text_char_count=len(bad_text),
            evidence_spans=[{"label": "bad-span", "text": "bad portable full text evidence"}],
        )
    )
    paper_reviews.upsert_review(
        _review_payload(
            paper_id="nested",
            full_text_artifact_path="storage:runs/runs/nested-run/raw/nested.txt",
            source_text_sha256="nested-source-text-sha",
            text_char_count=len(good_text),
            evidence_spans=[{"label": "nested-span", "text": "nested portable full text evidence"}],
        )
    )
    legacy_absolute = r"C:\old\checkout\storage\runs\portable-run\raw\good.txt"
    with bootstrap.connect_metadata_index() as connection:
        connection.execute(
            "UPDATE paper_reviews SET full_text_artifact_path = ? WHERE topic_id = ? AND run_id = ? AND paper_id = ?",
            (legacy_absolute, "portable-evidence-topic", "portable-run", "good"),
        )

    from app.cli import artifact_refs as artifact_refs_cli

    before_dry_hash = _file_sha256(bootstrap.metadata_index_path())
    dry_exit = artifact_refs_cli.main(["--topic", "portable-evidence-topic", "--dry-run"])
    after_dry_hash = _file_sha256(bootstrap.metadata_index_path())
    dry_output = json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit):
        artifact_refs_cli.main(["--topic", "portable-evidence-topic", "--apply"])
    _ = capsys.readouterr()
    apply_exit = artifact_refs_cli.main(["--topic", "portable-evidence-topic", "--apply", "--backup-first"])
    apply_output = json.loads(capsys.readouterr().out)
    good_after = paper_reviews.get_review("portable-evidence-topic", "portable-run", "good")
    bad_after = paper_reviews.get_review("portable-evidence-topic", "portable-run", "bad")

    assert dry_exit == 0
    assert after_dry_hash == before_dry_hash
    assert dry_output["hash_mismatch_count"] == 1
    assert dry_output["nested_namespace_count"] == 1
    assert dry_output["unsafe_count"] == 1
    assert dry_output["would_update_count"] == 1
    assert dry_output["status_counts"]["convertible_legacy_absolute"] == 1
    assert dry_output["status_counts"]["hash_mismatch"] == 1
    assert dry_output["status_counts"]["nested_storage_namespace"] == 1
    assert apply_exit == 0
    assert apply_output["updated_count"] == 1
    assert apply_output["hash_mismatch_count"] == 1
    assert apply_output["nested_namespace_count"] == 1
    assert apply_output["backup_path"]
    assert good_after is not None
    assert good_after["full_text_artifact_path"] == "run:raw/good.txt"
    assert bad_after is not None
    assert bad_after["full_text_artifact_path"] == "run:raw/bad.txt"

"""Stopped, private canonical data copies; never mutate the source or merge roots."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest


@pytest.fixture
def backup():
    return importlib.import_module("app.services.local_data_backup")


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    with closing(sqlite3.connect(root / "metadata_index.sqlite3")) as conn, conn:
        conn.execute("CREATE TABLE history (id TEXT PRIMARY KEY, body BLOB)")
        conn.execute("INSERT INTO history VALUES (?, ?)", ("preserved-id", "e\u0301 한글🙂".encode()))
    (root / "runs" / "empty").mkdir(parents=True)
    (root / "runs" / "논문 원문.bin").write_bytes(b"\x00\xff\r\n" + "e\u0301🙂".encode())
    (root / "auth").mkdir()
    (root / "auth" / "product_sessions.json").write_bytes(b'{"fixture_only":true}\n')
    return root


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_exact_copy_verify_restore_and_no_source_writes(backup, library, tmp_path):
    original = inventory(library)
    bundle = tmp_path / "bundle"
    manifest = backup.create_backup(library, bundle, source_stopped=True)
    assert backup.verify_backup(bundle) == manifest
    assert inventory(library) == original
    assert inventory(bundle / "data") == original
    destination = tmp_path / "restored"
    assert backup.restore_backup(bundle, destination) == manifest
    assert inventory(destination) == original
    assert (destination / "runs" / "empty").is_dir()
    assert set(p.name for p in bundle.iterdir()) == {"manifest.json", "data"}
    assert manifest["schema_version"] == "XRML_LOCAL_DATA_BACKUP_V1"
    assert manifest["consistency"] == "STOPPED_SOURCE_VERIFIED_COPY"


def test_source_stopped_is_required_and_missing_db_is_not_a_backup(backup, library, tmp_path):
    with pytest.raises(backup.BackupError, match="source_stop_required"):
        backup.create_backup(library, tmp_path / "missing-confirmation")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(backup.BackupError, match="metadata_database_required"):
        backup.create_backup(empty, tmp_path / "bad", source_stopped=True)
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_existing_destination_is_untouched_even_when_empty(backup, library, tmp_path, operation):
    bundle = tmp_path / "bundle"
    backup.create_backup(library, bundle, source_stopped=True)
    dest = tmp_path / "existing"
    dest.mkdir()
    for populated in (False, True):
        if populated:
            (dest / "owner.txt").write_text("owner")
        before = inventory(dest)
        with pytest.raises(backup.BackupError, match="destination_exists"):
            if operation == "create":
                backup.create_backup(library, dest, source_stopped=True)
            else:
                backup.restore_backup(bundle, dest)
        assert inventory(dest) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_any_sqlite_sidecar_requires_clean_shutdown(backup, library, tmp_path, suffix):
    sidecar = library / ("metadata_index.sqlite3" + suffix)
    sidecar.write_bytes(b"")
    before = inventory(library)
    with pytest.raises(backup.BackupError, match="sqlite_sidecars_present"):
        backup.create_backup(library, tmp_path / "bad", source_stopped=True)
    assert inventory(library) == before
    assert not (tmp_path / "bad").exists()


def test_live_uncheckpointed_wal_is_not_a_stale_success(backup, library, tmp_path):
    with closing(sqlite3.connect(library / "metadata_index.sqlite3")) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO history VALUES ('wal-only', X'1234')")
        conn.commit()
        with pytest.raises(backup.BackupError):
            backup.create_backup(library, tmp_path / "bad", source_stopped=True)
        assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 2
    assert not (tmp_path / "bad").exists()
    backup.create_backup(library, tmp_path / "after-shutdown", source_stopped=True)


def test_schedule_lock_blocks_without_recovery_or_deletion(backup, library, tmp_path):
    lock = library / "topic_schedules" / "schedules.lock"
    lock.parent.mkdir()
    lock.write_text('{"pid":42}')
    with pytest.raises(backup.BackupError, match="schedule_lock_present"):
        backup.create_backup(library, tmp_path / "bad", source_stopped=True)
    assert lock.read_text() == '{"pid":42}'


@pytest.mark.parametrize("damage", ["changed", "missing", "extra", "directory", "manifest", "duplicate", "traversal"])
def test_corrupt_or_incomplete_bundle_never_publishes_restore(backup, library, tmp_path, damage):
    bundle = tmp_path / "bundle"
    backup.create_backup(library, bundle, source_stopped=True)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    file = bundle / "data" / "runs" / "논문 원문.bin"
    if damage == "changed":
        file.write_bytes(b"changed")
    elif damage == "missing":
        file.unlink()
    elif damage == "extra":
        (bundle / "data" / "extra").write_text("not recorded")
    elif damage == "directory":
        (bundle / "data" / "runs" / "empty").rmdir()
    elif damage == "duplicate":
        manifest_path.write_text('{"schema_version":"one","schema_version":"two"}', encoding="utf-8")
    elif damage == "manifest":
        manifest["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        manifest["files"][0]["path"] = "../escape"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bundle, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    assert not (tmp_path / "escape").exists()
    assert not [p for p in tmp_path.glob(".xrml-copy-*") if p.is_dir()]


def test_invalid_database_does_not_publish(backup, library, tmp_path):
    (library / "metadata_index.sqlite3").write_bytes(b"SQLite format 3\x00" + b"broken" * 30)
    with pytest.raises(backup.BackupError):
        backup.create_backup(library, tmp_path / "bad", source_stopped=True)
    assert not (tmp_path / "bad").exists()


def test_empty_database_cannot_be_verified_as_a_canonical_backup(backup, library, tmp_path):
    (library / "metadata_index.sqlite3").write_bytes(b"")
    with pytest.raises(backup.BackupError, match="sqlite_invalid"):
        backup.create_backup(library, tmp_path / "bad", source_stopped=True)
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_orphan_database_sidecars_block_copy(backup, library, tmp_path, suffix):
    (library / ("missing.sqlite3" + suffix)).write_bytes(b"orphan journal")
    with pytest.raises(backup.BackupError, match="sqlite_sidecars_present"):
        backup.create_backup(library, tmp_path / "bad", source_stopped=True)
    assert not (tmp_path / "bad").exists()


def test_restore_has_no_fallible_stage_cleanup_after_publication(backup, library, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    backup.create_backup(library, bundle, source_stopped=True)
    remove = backup.shutil.rmtree
    def fail_only_stage_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".xrml-copy-"):
            raise OSError("fixture cleanup failed after publication")
        return remove(path, *args, **kwargs)
    monkeypatch.setattr(backup.shutil, "rmtree", fail_only_stage_cleanup)
    restored = tmp_path / "restored"
    assert backup.restore_backup(bundle, restored)["schema_version"] == "XRML_LOCAL_DATA_BACKUP_V1"
    assert inventory(restored) == inventory(library)


@pytest.mark.parametrize("path", ["../escape", "C:/escape", "/escape", "runs/file:ads", "runs/CON", "runs\\file"])
def test_rehashed_manifest_cannot_escape_bundle(backup, library, tmp_path, path):
    bundle = tmp_path / "bundle"
    manifest = backup.create_backup(library, bundle, source_stopped=True)
    manifest["files"][-1]["path"] = path
    manifest["manifest_sha256"] = backup._manifest_digest(manifest)
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="invalid_relative_path"):
        backup.restore_backup(bundle, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_nested_destinations_and_relative_source_are_rejected(backup, library, tmp_path):
    with pytest.raises(backup.BackupError):
        backup.create_backup(library, library / "nested", source_stopped=True)
    with pytest.raises(backup.BackupError):
        backup.create_backup(Path("."), tmp_path / "bad", source_stopped=True)


def test_source_change_during_copy_and_io_failure_leave_no_destination(backup, library, tmp_path, monkeypatch):
    copy = backup._copy_file
    def mutate(source, destination):
        copy(source, destination)
        if source.name == "논문 원문.bin":
            source.write_bytes(b"changed after copy")
    monkeypatch.setattr(backup, "_copy_file", mutate)
    with pytest.raises(backup.BackupError, match="source_changed"):
        backup.create_backup(library, tmp_path / "changed", source_stopped=True)
    assert not (tmp_path / "changed").exists()
    def fail(*args):
        raise OSError("fixture disk full")
    monkeypatch.setattr(backup, "_copy_file", fail)
    with pytest.raises(backup.BackupError, match="io_failed"):
        backup.create_backup(library, tmp_path / "failed", source_stopped=True)
    assert not (tmp_path / "failed").exists()
    assert not [p for p in tmp_path.glob(".xrml-copy-*") if p.is_dir()]


def test_hardlinked_source_is_rejected(backup, library, tmp_path):
    linked = library / "linked"
    linked.hardlink_to(library / "metadata_index.sqlite3")
    with pytest.raises(backup.BackupError, match="linked_path"):
        backup.create_backup(library, tmp_path / "bad", source_stopped=True)


def test_cli_refusal_has_stable_error_and_no_traceback(backup, library, tmp_path):
    result = subprocess.run([sys.executable, "-m", "app.cli.local_data_backup", "create", "--source", str(library),
                             "--destination", str(tmp_path / "bad")], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "source_stop_required" in result.stderr
    assert "Traceback" not in result.stderr and not (tmp_path / "bad").exists()


def test_concurrent_create_publishes_once_and_preserves_winning_bundle(backup, library, tmp_path):
    gate = Barrier(2)
    bundle = tmp_path / "bundle"
    def create():
        gate.wait()
        try:
            return backup.create_backup(library, bundle, source_stopped=True)
        except backup.BackupError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "backup_destination_exists" in results
    assert backup.verify_backup(bundle) in results
    assert inventory(bundle / "data") == inventory(library)


@pytest.mark.parametrize("at_root", [False, True])
def test_real_link_or_windows_junction_cannot_import_outside_data(backup, library, tmp_path, at_root):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "owner.txt").write_text("must not follow")
    link = tmp_path / "source-link" if at_root else library / "linked-directory"
    target = library if at_root else outside
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="linked_path"):
        backup.create_backup(link if at_root else library, tmp_path / "bad", source_stopped=True)
    assert (outside / "owner.txt").read_text() == "must not follow"
    assert not (tmp_path / "bad").exists()


def test_cli_create_verify_restore_roundtrip(backup, library, tmp_path):
    bundle, restored = tmp_path / "bundle", tmp_path / "restored"
    commands = [
        ["create", "--source", str(library), "--destination", str(bundle), "--source-stopped"],
        ["verify", "--bundle", str(bundle)],
        ["restore", "--bundle", str(bundle), "--destination", str(restored)],
    ]
    for command in commands:
        result = subprocess.run([sys.executable, "-m", "app.cli.local_data_backup", *command],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "verified" and payload["runtime_started"] is False
    assert inventory(restored) == inventory(library)

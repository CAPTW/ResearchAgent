"""Schedule ownership must survive metadata readers without a stranded live PID."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import pytest

from app.services import topic_schedules as schedules


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "topic_schedules"
    monkeypatch.setattr(schedules, "SCHEDULE_LIBRARY_DIR", root)
    monkeypatch.setattr(schedules, "SCHEDULE_LIBRARY_PATH", root / "schedules.json")
    return root


@pytest.mark.parametrize("body_fails", [False, True])
def test_metadata_reader_does_not_strand_completed_owner(library: Path, body_fails: bool) -> None:
    """A real Windows read handle denies unlink; it must not retain ownership."""
    reader = None
    try:
        try:
            with schedules._schedule_store_lock():
                reader = (library / "schedules.lock").open(encoding="utf-8")
                assert json.load(reader)["pid"] == os.getpid()
                if body_fails:
                    raise ValueError("body failed")
        except ValueError as error:
            assert body_fails and str(error) == "body failed"
        assert schedules._lock_file_status()["locked"] is False
        # The observer remains open, and this process remains alive.
        with schedules._schedule_store_lock():
            assert schedules._lock_file_status()["locked"] is True
        assert schedules._lock_file_status()["locked"] is False
    finally:
        if reader is not None:
            reader.close()


def test_metadata_write_failure_releases_created_lock(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_dump = json.dump

    def fail_owner_metadata(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("owner") == "topic_schedule_store":
            raise OSError("metadata write failed")
        return original_dump(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", fail_owner_metadata)
        with pytest.raises(OSError, match="metadata write failed"):
            with schedules._schedule_store_lock():
                pytest.fail("body must not run without owner metadata")
    assert schedules._lock_file_status()["locked"] is False
    with schedules._schedule_store_lock():
        pass


def test_another_process_can_acquire_after_release_with_reader_open(library: Path) -> None:
    reader = None
    try:
        with schedules._schedule_store_lock():
            reader = (library / "schedules.lock").open(encoding="utf-8")
            assert json.load(reader)["pid"] == os.getpid()
        code = """
import sys
from pathlib import Path
from app.services import topic_schedules as s
s.SCHEDULE_LIBRARY_DIR=Path(sys.argv[1])
s.SCHEDULE_LIBRARY_PATH=s.SCHEDULE_LIBRARY_DIR/'schedules.json'
with s._schedule_store_lock():
    print('acquired', flush=True)
assert s._lock_file_status()['locked'] is False
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(library)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "acquired"
    finally:
        if reader is not None:
            reader.close()


def test_active_guard_cannot_be_recovered_from_stale_metadata(library: Path) -> None:
    with schedules._schedule_store_lock():
        marker = library / "schedules.lock"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload.update(pid=999999999, acquired_at="2000-01-01T00:00:00+00:00")
        marker.write_text(json.dumps(payload), encoding="utf-8")
        before = marker.read_bytes()
        assert schedules._lock_file_status()["locked"] is True
        assert schedules._recover_stale_schedule_lock(marker) is False
        assert marker.read_bytes() == before
        # Nested use must not release the enclosing owner.
        with schedules._schedule_store_lock():
            assert schedules._lock_file_status()["locked"] is True
        assert schedules._lock_file_status()["locked"] is True
    assert schedules._lock_file_status()["locked"] is False


def test_process_exit_releases_ownership_without_deleting_metadata(library: Path) -> None:
    code = """
import os, sys
from pathlib import Path
from app.services import topic_schedules as s
s.SCHEDULE_LIBRARY_DIR=Path(sys.argv[1])
s.SCHEDULE_LIBRARY_PATH=s.SCHEDULE_LIBRARY_DIR/'schedules.json'
with s._schedule_store_lock():
    os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", code, str(library)], timeout=30, check=False)
    assert result.returncode == 17
    assert (library / "schedules.lock").exists()
    assert schedules._lock_file_status()["locked"] is False
    with schedules._schedule_store_lock():
        assert schedules._lock_file_status()["locked"] is True


def test_processes_serialize_schedule_mutations(library: Path) -> None:
    library.mkdir()
    (library / "counter").write_text("0", encoding="utf-8")
    code = """
import os, sys, time
from pathlib import Path
from app.services import topic_schedules as s
s.SCHEDULE_LIBRARY_DIR=root=Path(sys.argv[1])
s.SCHEDULE_LIBRARY_PATH=root/'schedules.json'
for _ in range(12):
    with s._schedule_store_lock():
        fd=os.open(root/'inside', os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        try:
            value=int((root/'counter').read_text(encoding='utf-8'))
            time.sleep(.005)
            (root/'counter').write_text(str(value+1),encoding='utf-8')
        finally:
            os.close(fd)
            (root/'inside').unlink()
"""
    def run():
        return subprocess.run(
            [sys.executable, "-c", code, str(library)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [future.result() for future in futures]
    for result in results:
        assert result.returncode == 0, result.stderr
    assert (library / "counter").read_text(encoding="utf-8") == "24"
    assert not (library / "inside").exists()


def test_schedule_api_reads_after_metadata_cleanup_was_blocked(client, library: Path) -> None:
    before = client.get("/topic-schedules")
    assert before.status_code == 200
    assert before.json()["items"]
    reader = None
    try:
        with schedules._schedule_store_lock():
            reader = (library / "schedules.lock").open(encoding="utf-8")
            json.load(reader)
        response = client.get("/topic-schedules")
        assert response.status_code == 200
        assert response.json()["degraded"] is False
        assert response.json()["scheduler"]["store_lock"]["locked"] is False
        assert response.json()["items"] == before.json()["items"]
    finally:
        if reader is not None:
            reader.close()

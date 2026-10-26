from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "runtime-data-guard-v1"
SQLITE_SIDECARS = {
    "journal": "-journal",
    "wal": "-wal",
    "shm": "-shm",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_snapshot(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "kind": "file",
        "size": None,
        "sha256": None,
    }
    if not path.exists():
        return snapshot
    if path.is_symlink():
        snapshot["kind"] = "symlink"
        snapshot["target"] = os.readlink(path)
        return snapshot
    if not path.is_file():
        snapshot["kind"] = "other"
        return snapshot
    snapshot["size"] = path.stat().st_size
    snapshot["sha256"] = _sha256_file(path)
    return snapshot


def _sqlite_sidecars(path: Path) -> dict[str, dict[str, Any]]:
    return {
        name: _file_snapshot(Path(f"{path}{suffix}"))
        for name, suffix in SQLITE_SIDECARS.items()
    }


def _hash_directory(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "kind": "directory",
        "size": None,
        "file_count": 0,
        "symlink_count": 0,
        "sha256": None,
    }
    if not path.exists():
        return snapshot
    if path.is_symlink():
        snapshot["kind"] = "symlink"
        snapshot["target"] = os.readlink(path)
        return snapshot
    if not path.is_dir():
        snapshot["kind"] = "other"
        return snapshot

    digest = hashlib.sha256()
    total_size = 0
    file_count = 0
    symlink_count = 0
    for current_root, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current_root)
        for dirname in list(dirnames):
            entry = current_path / dirname
            if not entry.is_symlink():
                continue
            symlink_count += 1
            dirnames.remove(dirname)
            target = os.readlink(entry)
            relative = entry.relative_to(path).as_posix()
            digest.update(f"symlink_dir\0{relative}\0{target}\0".encode("utf-8"))
        for filename in sorted(filenames):
            entry = current_path / filename
            relative = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                symlink_count += 1
                target = os.readlink(entry)
                digest.update(f"symlink_file\0{relative}\0{target}\0".encode("utf-8"))
                continue
            if not entry.is_file():
                digest.update(f"other\0{relative}\0".encode("utf-8"))
                continue
            size = entry.stat().st_size
            file_hash = _sha256_file(entry)
            total_size += size
            file_count += 1
            digest.update(f"file\0{relative}\0{size}\0{file_hash}\0".encode("utf-8"))

    snapshot["size"] = total_size
    snapshot["file_count"] = file_count
    snapshot["symlink_count"] = symlink_count
    snapshot["sha256"] = digest.hexdigest()
    return snapshot


def _runs_snapshot(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "kind": "directory",
        "immediate_dir_count": 0,
        "recursive_file_count": 0,
    }
    if not path.exists():
        return snapshot
    if path.is_symlink():
        snapshot["kind"] = "symlink"
        snapshot["target"] = os.readlink(path)
        return snapshot
    if not path.is_dir():
        snapshot["kind"] = "other"
        return snapshot

    with os.scandir(path) as entries:
        snapshot["immediate_dir_count"] = sum(
            1 for entry in entries if entry.is_dir(follow_symlinks=False)
        )
    recursive_file_count = 0
    for _current_root, _dirnames, filenames in os.walk(path, followlinks=False):
        recursive_file_count += len(filenames)
    snapshot["recursive_file_count"] = recursive_file_count
    return snapshot


def build_snapshot(repo_root: Path) -> dict[str, Any]:
    root = repo_root.expanduser().resolve()
    storage_root = root / "storage"
    metadata_db = storage_root / "metadata_index.sqlite3"
    scheduler_db = storage_root / "scheduler.sqlite3"
    metadata_snapshot = _file_snapshot(metadata_db)
    metadata_snapshot["sidecars"] = _sqlite_sidecars(metadata_db)
    scheduler_snapshot = _file_snapshot(scheduler_db)
    scheduler_snapshot["sidecars"] = _sqlite_sidecars(scheduler_db)
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_root": str(root),
        "read_only": True,
        "artifacts": {
            "metadata_db": metadata_snapshot,
            "scheduler_db": scheduler_snapshot,
            "topic_analytics": _hash_directory(storage_root / "topic_analytics"),
            "topic_schedules": _hash_directory(storage_root / "topic_schedules"),
            "storage_runs": _runs_snapshot(storage_root / "runs"),
        },
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_artifacts = before.get("artifacts", {})
    after_artifacts = after.get("artifacts", {})
    all_names = sorted(set(before_artifacts) | set(after_artifacts))
    changed = [
        name
        for name in all_names
        if before_artifacts.get(name) != after_artifacts.get(name)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "equal": not changed,
        "changed_artifacts": changed,
    }


def _emit_json(payload: dict[str, Any], output_path: Path | None) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output_path is None:
        sys.stdout.write(serialized)
        return
    resolved_output = output_path.expanduser().resolve()
    if not resolved_output.parent.exists():
        raise FileNotFoundError(f"output parent does not exist: {resolved_output.parent}")
    resolved_output.write_text(serialized, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only snapshot and comparison guard for XRML runtime data artifacts."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to inspect. Defaults to the parent of scripts/.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path. Without this, the guard writes JSON to stdout only.",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BEFORE_JSON", "AFTER_JSON"),
        type=Path,
        help="Compare two guard snapshots and exit non-zero if protected artifacts changed.",
    )
    args = parser.parse_args(argv)

    if args.compare:
        before = json.loads(args.compare[0].read_text(encoding="utf-8"))
        after = json.loads(args.compare[1].read_text(encoding="utf-8"))
        result = compare_snapshots(before, after)
        _emit_json(result, args.output)
        return 0 if result["equal"] else 1

    snapshot = build_snapshot(args.repo_root)
    _emit_json(snapshot, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

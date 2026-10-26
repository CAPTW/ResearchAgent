"""Private, stopped-source data copies with fresh-destination restore.

No app bootstrap, source SQLite connection, migration, credential discovery or
runtime activation occurs here. This is not a live multi-store transaction.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Iterator

from app.db.sqlite_snapshot import (
    SqliteSnapshotError, capture_stable_sqlite_snapshot, open_immutable_snapshot,
)
from app.domain.path_safety import is_reparse_point
from app.repositories.file_locks import FileLockError, file_lock

SCHEMA = "XRML_LOCAL_DATA_BACKUP_V1"
_CHUNK = 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_ENTRIES = 100_000


class BackupError(RuntimeError):
    """Stable diagnostic without research text, credentials or OS error paths."""


@dataclass(frozen=True)
class _File:
    size: int
    sha256: str
    identity: tuple[int, int, int, int, int]
    sqlite: bool


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except BackupError:
        raise
    except SqliteSnapshotError as exc:
        raise BackupError(f"backup_{exc.code}") from exc
    except sqlite3.Error as exc:
        raise BackupError("backup_sqlite_invalid") from exc
    except FileLockError as exc:
        raise BackupError("backup_destination_busy") from exc
    except (OSError, ValueError, UnicodeError) as exc:
        raise BackupError("backup_io_failed") from exc


def _path(value: Path) -> Path:
    # Check before resolve: resolving a junction first would hide it.
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("backup_absolute_path_required")
    for part in (*reversed(path.parents), path):
        if is_reparse_point(part):
            raise BackupError("backup_linked_path")
    return path.resolve()


def _relative(value: str) -> str:
    # Preserve Unicode and spaces; never normalize original filenames. Refuse
    # names that change meaning on Windows, including ADS and device names.
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupError("backup_invalid_relative_path")
    for part in value.split("/"):
        if (part in {"", ".", ".."} or part[-1:] in {" ", "."}
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or PureWindowsPath(part).is_reserved()):
            raise BackupError("backup_invalid_relative_path")
    value.encode("utf-8", errors="strict")
    return value


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if is_reparse_point(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("backup_linked_path")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino


def _file(path: Path) -> _File:
    before = _identity(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        head = stream.read(16)
        digest.update(head)
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    if _identity(path) != before:
        raise BackupError("backup_source_changed")
    sqlite = head == b"SQLite format 3\x00"
    if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"} and not sqlite:
        raise BackupError("backup_sqlite_invalid")
    return _File(before[0], digest.hexdigest(), before, sqlite)


def _scan(root: Path) -> tuple[list[str], dict[str, _File]]:
    root = _path(root)
    if not root.is_dir():
        raise BackupError("backup_directory_required")
    dirs: list[str] = []
    files: dict[str, _File] = {}
    names: set[str] = set()
    pending = [root]
    while pending:
        parent = pending.pop()
        for path in sorted(parent.iterdir()):
            relative = _relative(path.relative_to(root).as_posix())
            if relative.casefold() in names:
                raise BackupError("backup_path_collision")
            names.add(relative.casefold())
            if len(names) > _MAX_ENTRIES:
                raise BackupError("backup_too_many_entries")
            if is_reparse_point(path):
                raise BackupError("backup_linked_path")
            if path.is_dir():
                dirs.append(relative)
                pending.append(path)
            else:
                files[relative] = _file(path)
    if "topic_schedules/schedules.lock" in files:
        raise BackupError("backup_schedule_lock_present")
    for name in files:
        # Orphans are also refused: copying one could replay stale state when a
        # corresponding DB is later opened. Never remove or checkpoint them.
        if name.lower().endswith(("-wal", "-shm", "-journal")):
            raise BackupError("backup_sqlite_sidecars_present")
    return sorted(dirs), dict(sorted(files.items()))


def _copy_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, _CHUNK)
        writer.flush()
        os.fsync(writer.fileno())


def _database(path: Path, destination: Path | None = None) -> list[dict[str, Any]]:
    with capture_stable_sqlite_snapshot(path) as snapshot:
        if snapshot.source_observation.sidecars_present:
            raise BackupError("backup_sqlite_sidecars_present")
        store = "scheduler" if path.name == "scheduler.sqlite3" else "metadata"
        with closing(open_immutable_snapshot(snapshot, store=store)) as connection:
            if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                raise BackupError("backup_sqlite_invalid")
            schema = [dict(row) for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            )]
        if destination is not None:
            _copy_file(snapshot.snapshot_path, destination)
        return schema


def _copy_tree(source: Path, destination: Path, dirs: list[str], files: dict[str, _File]) -> list[dict[str, Any]]:
    destination.mkdir(exist_ok=True)
    if any(destination.iterdir()):
        raise BackupError("backup_destination_exists")
    for name in dirs:
        (destination / name).mkdir(parents=True, exist_ok=True)
    records = []
    for name, entry in files.items():
        target = destination / name
        record: dict[str, Any] = {"path": name, "size": entry.size, "sha256": entry.sha256,
                                  "kind": "sqlite" if entry.sqlite else "file"}
        if entry.sqlite:
            record["schema"] = _database(source / name, target)
        else:
            _copy_file(source / name, target)
        records.append(record)
    return records


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _manifest_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes({k: v for k, v in value.items() if k != "manifest_sha256"})).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BackupError("backup_manifest_invalid")
        value[key] = item
    return value


def _manifest(bundle: Path) -> dict[str, Any]:
    if set(p.name for p in bundle.iterdir()) != {"manifest.json", "data"}:
        raise BackupError("backup_bundle_inventory_invalid")
    path = bundle / "manifest.json"
    if _identity(path)[0] > _MAX_MANIFEST_BYTES:
        raise BackupError("backup_manifest_too_large")
    try:
        with path.open("rb") as reader:
            encoded = reader.read(_MAX_MANIFEST_BYTES + 1)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise BackupError("backup_manifest_too_large")
        value = json.loads(encoded, object_pairs_hook=_no_duplicate_keys)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "consistency", "created_at", "private_data",
                                                         "directories", "files", "manifest_sha256"}
                or value["schema_version"] != SCHEMA or value["consistency"] != "STOPPED_SOURCE_VERIFIED_COPY"
                or value["private_data"] is not True or not isinstance(value["created_at"], str)
                or not isinstance(value["directories"], list) or not isinstance(value["files"], list)
                or len(value["directories"]) + len(value["files"]) > _MAX_ENTRIES
                or value["manifest_sha256"] != _manifest_digest(value)):
            raise BackupError("backup_manifest_invalid")
        names = [_relative(n) for n in value["directories"]]
        for record in value["files"]:
            if (not isinstance(record, dict) or record.get("kind") not in {"file", "sqlite"}
                    or set(record) != ({"path", "size", "sha256", "kind", "schema"} if record["kind"] == "sqlite"
                                       else {"path", "size", "sha256", "kind"})
                    or type(record["size"]) is not int or record["size"] < 0
                    or not isinstance(record["sha256"], str) or len(record["sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in record["sha256"])):
                raise BackupError("backup_manifest_invalid")
            names.append(_relative(record["path"]))
        if len({n.casefold() for n in names}) != len(names):
            raise BackupError("backup_manifest_invalid")
        if not any(r["path"] == "metadata_index.sqlite3" and r["kind"] == "sqlite" for r in value["files"]):
            raise BackupError("backup_metadata_database_required")
        return value
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise BackupError("backup_manifest_invalid") from exc


def _verify_data(root: Path, manifest: dict[str, Any]) -> tuple[list[str], dict[str, _File]]:
    dirs, files = _scan(root)
    if dirs != manifest["directories"] or list(files) != [r["path"] for r in manifest["files"]]:
        raise BackupError("backup_inventory_mismatch")
    for record in manifest["files"]:
        entry = files[record["path"]]
        if (entry.size != record["size"] or entry.sha256 != record["sha256"]
                or entry.sqlite != (record["kind"] == "sqlite")):
            raise BackupError("backup_content_mismatch")
        if entry.sqlite and _database(root / record["path"]) != record["schema"]:
            raise BackupError("backup_schema_mismatch")
    return dirs, files


def _destination(source: Path, destination: Path) -> Path:
    dest = _path(destination)
    if dest.exists():
        raise BackupError("backup_destination_exists")
    if source == dest or dest.is_relative_to(source) or source.is_relative_to(dest):
        raise BackupError("backup_overlapping_paths")
    if not dest.parent.is_dir():
        raise BackupError("backup_destination_parent_required")
    return dest


@contextmanager
def _stage(destination: Path) -> Iterator[Path]:
    # Serialize cooperating publications. This does not freeze app writers.
    key = hashlib.sha256(str(destination).casefold().encode()).hexdigest()[:24]
    with file_lock(destination.parent / f".xrml-copy-{key}.lock"):
        if destination.exists():
            raise BackupError("backup_destination_exists")
        stage = Path(tempfile.mkdtemp(prefix=".xrml-copy-", dir=destination.parent))
        try:
            yield stage
        finally:
            # Only remove our generated, checked sibling staging directory.
            if stage.exists():
                if stage.parent != destination.parent or is_reparse_point(stage):
                    raise BackupError("backup_cleanup_failed")
                shutil.rmtree(stage)


def _publish(stage: Path, destination: Path) -> None:
    if destination.exists():
        raise BackupError("backup_destination_exists")
    # Windows rename refuses an existing destination. The lock also serializes
    # other copies using this tool; local trusted filesystem access is assumed.
    stage.rename(destination)


def create_backup(source: Path, destination: Path, *, source_stopped: bool = False) -> dict[str, Any]:
    """Copy an explicit, quiescent XRML_DATA_DIR; never repair or checkpoint it."""
    with _errors():
        if source_stopped is not True:
            raise BackupError("backup_source_stop_required")
        root = _path(source)
        dest = _destination(root, destination)
        before = _scan(root)
        if "metadata_index.sqlite3" not in before[1]:
            raise BackupError("backup_metadata_database_required")
        with _stage(dest) as stage:
            records = _copy_tree(root, stage / "data", *before)
            if _scan(root) != before:
                raise BackupError("backup_source_changed")
            manifest = dict(schema_version=SCHEMA, consistency="STOPPED_SOURCE_VERIFIED_COPY",
                            created_at=datetime.now(timezone.utc).isoformat(), private_data=True,
                            directories=before[0], files=records)
            manifest["manifest_sha256"] = _manifest_digest(manifest)
            encoded = _json_bytes(manifest)
            if len(encoded) > _MAX_MANIFEST_BYTES:
                raise BackupError("backup_manifest_too_large")
            with (stage / "manifest.json").open("xb") as writer:
                writer.write(encoded)
                writer.flush()
                os.fsync(writer.fileno())
            verify_backup(stage)
            if _scan(root) != before:
                raise BackupError("backup_source_changed")
            _publish(stage, dest)
            return manifest


def verify_backup(bundle: Path) -> dict[str, Any]:
    """Check inventory, exact bytes and SQLite structure without app startup."""
    with _errors():
        root = _path(bundle)
        manifest = _manifest(root)
        before = _verify_data(root / "data", manifest)
        if _scan(root / "data") != before or _manifest(root) != manifest:
            raise BackupError("backup_source_changed")
        return manifest


def restore_backup(bundle: Path, destination: Path) -> dict[str, Any]:
    """Verify then publish a fresh data root. Never overwrite, merge or launch."""
    with _errors():
        root = _path(bundle)
        dest = _destination(root, destination)
        manifest = verify_backup(root)
        before = _scan(root / "data")
        with _stage(dest) as stage:
            _copy_tree(root / "data", stage, *before)
            _verify_data(stage, manifest)
            if _scan(root / "data") != before or _manifest(root) != manifest:
                raise BackupError("backup_source_changed")
            _publish(stage, dest)
            return manifest

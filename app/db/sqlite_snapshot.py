"""Physical snapshot boundary for explicit SQLite plans and integrity audits.

The source database is inspected and copied with ordinary filesystem reads
only.  SQLite is opened only on the verified, private OS-temporary copy.  This
keeps planning/auditing from creating WAL or shared-memory files beside a live
source database.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Iterator

from app.db.sqlite_integrity import (
    DEFAULT_READ_TIMEOUT_SECONDS,
    ManagedSqliteConnection,
    SqliteIntegrityError,
    SqliteStoreUnavailable,
    configure_sqlite_read_only,
)

SNAPSHOT_STRATEGY: Final = "physical_copy_immutable"

SOURCE_OBSERVATION_NONE: Final = "none"
SOURCE_OBSERVATION_STABLE_ZERO_WAL: Final = "stable_zero_wal_sidecars"
SOURCE_OBSERVATION_ACTIVE_WAL: Final = "active_or_uncheckpointed_wal"
SOURCE_OBSERVATION_ROLLBACK_JOURNAL: Final = "rollback_journal_present"
SOURCE_OBSERVATION_SIDECAR_UNSTABLE: Final = "sidecar_state_unstable"
SOURCE_OBSERVATION_UNSUPPORTED: Final = "unsupported_sidecar_state"
SOURCE_OBSERVATION_SOURCE_UNSTABLE: Final = "source_unstable"

CODE_SOURCE_UNSTABLE: Final = "sqlite_snapshot_source_unstable"
CODE_COPY_FAILED: Final = "sqlite_snapshot_copy_failed"
CODE_VERIFICATION_FAILED: Final = "sqlite_snapshot_verification_failed"
CODE_CLEANUP_FAILED: Final = "sqlite_snapshot_cleanup_failed"
CODE_SOURCE_SIDECARS_PRESENT: Final = "sqlite_source_sidecars_present"
CODE_ACTIVE_WAL: Final = "sqlite_active_or_uncheckpointed_wal"
CODE_ROLLBACK_JOURNAL: Final = "sqlite_rollback_journal_present"
CODE_SIDECAR_UNSTABLE: Final = "sqlite_sidecar_state_unstable"
CODE_UNSUPPORTED_SIDECARS: Final = "sqlite_unsupported_sidecar_state"
CODE_AUDIT_FAILED: Final = "sqlite_audit_snapshot_failed"

DEFAULT_STABILITY_DELAY_SECONDS: Final = 0.05
_COPY_CHUNK_BYTES: Final = 1024 * 1024
_SIDECARS: Final = (
    ("journal", "-journal"),
    ("wal", "-wal"),
    ("shm", "-shm"),
)


class SqliteSnapshotError(RuntimeError):
    """Stable, path-free failure raised by the physical snapshot boundary."""

    code = CODE_AUDIT_FAILED
    observation_status = SOURCE_OBSERVATION_UNSUPPORTED

    def __init__(
        self,
        *,
        code: str | None = None,
        observation_status: str | None = None,
        source_size: int | None = None,
        source_sha256: str | None = None,
        sidecars_present: tuple[str, ...] = (),
    ) -> None:
        self.code = code or self.code
        self.observation_status = observation_status or self.observation_status
        self.source_size = source_size
        self.source_sha256 = source_sha256
        self.sidecars_present = sidecars_present
        super().__init__(self.code)


class SqliteSnapshotUnstable(SqliteSnapshotError):
    code = CODE_SOURCE_UNSTABLE
    observation_status = SOURCE_OBSERVATION_SOURCE_UNSTABLE


class SqliteSnapshotBlocked(SqliteSnapshotError):
    pass


class SqliteSnapshotCopyFailed(SqliteSnapshotError):
    code = CODE_COPY_FAILED


class SqliteSnapshotVerificationFailed(SqliteSnapshotError):
    code = CODE_VERIFICATION_FAILED


class SqliteSnapshotCleanupFailed(SqliteSnapshotError):
    code = CODE_CLEANUP_FAILED


@dataclass(frozen=True)
class SqliteFileFingerprint:
    size: int
    sha256: str
    mtime_ns: int
    device: int
    inode: int
    ctime_ns: int


@dataclass(frozen=True)
class SqliteSidecarObservation:
    label: str
    present: bool
    fingerprint: SqliteFileFingerprint | None = None


@dataclass(frozen=True)
class SqliteSourceFingerprint:
    main: SqliteFileFingerprint
    sidecars: tuple[SqliteSidecarObservation, ...]

    @property
    def sidecars_present(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.sidecars if item.present)


@dataclass(frozen=True)
class SqliteSourceObservation:
    status: str
    sidecars_present: tuple[str, ...]
    issue_codes: tuple[str, ...]
    audit_allowed: bool
    apply_allowed: bool


@dataclass(frozen=True)
class SqliteAuditSnapshot:
    """Internal handle for one verified temporary physical snapshot."""

    source_path: Path
    snapshot_path: Path
    temp_directory: Path
    source_fingerprint: SqliteSourceFingerprint
    source_observation: SqliteSourceObservation
    snapshot_fingerprint: SqliteFileFingerprint
    strategy: str = SNAPSHOT_STRATEGY


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_ctime_ns),
    )


def _capture_file(path: Path) -> SqliteFileFingerprint:
    if not path.exists():
        raise FileNotFoundError("SQLite file disappeared")
    if path.is_symlink() or not path.is_file():
        raise SqliteSnapshotBlocked(
            code=CODE_UNSUPPORTED_SIDECARS,
            observation_status=SOURCE_OBSERVATION_UNSUPPORTED,
        )
    before = _stat_identity(path)
    digest = _sha256_file(path)
    after = _stat_identity(path)
    if before != after:
        raise SqliteSnapshotUnstable()
    size, mtime_ns, device, inode, ctime_ns = after
    return SqliteFileFingerprint(
        size=size,
        sha256=digest,
        mtime_ns=mtime_ns,
        device=device,
        inode=inode,
        ctime_ns=ctime_ns,
    )


def capture_sqlite_source_fingerprint(path: Path) -> SqliteSourceFingerprint:
    """Capture source bytes/identity and sidecars without opening SQLite."""

    try:
        resolved = path.expanduser().resolve(strict=True)
        main = _capture_file(resolved)
        sidecars: list[SqliteSidecarObservation] = []
        for label, suffix in _SIDECARS:
            sidecar = Path(f"{resolved}{suffix}")
            if not sidecar.exists():
                sidecars.append(SqliteSidecarObservation(label=label, present=False))
                continue
            try:
                sidecar_fingerprint = _capture_file(sidecar)
            except SqliteSnapshotUnstable as exc:
                raise SqliteSnapshotBlocked(
                    code=CODE_SIDECAR_UNSTABLE,
                    observation_status=SOURCE_OBSERVATION_SIDECAR_UNSTABLE,
                    source_size=main.size,
                    source_sha256=main.sha256,
                    sidecars_present=(label,),
                ) from exc
            except FileNotFoundError as exc:
                raise SqliteSnapshotBlocked(
                    code=CODE_SIDECAR_UNSTABLE,
                    observation_status=SOURCE_OBSERVATION_SIDECAR_UNSTABLE,
                    source_size=main.size,
                    source_sha256=main.sha256,
                    sidecars_present=(label,),
                ) from exc
            sidecars.append(
                SqliteSidecarObservation(
                    label=label,
                    present=True,
                    fingerprint=sidecar_fingerprint,
                )
            )
        return SqliteSourceFingerprint(main=main, sidecars=tuple(sidecars))
    except SqliteSnapshotError:
        raise
    except OSError as exc:
        raise SqliteSnapshotUnstable() from exc


def classify_sqlite_source(
    fingerprint: SqliteSourceFingerprint,
) -> SqliteSourceObservation:
    """Classify whether a stable source can be copied for diagnostics."""

    by_label = {item.label: item for item in fingerprint.sidecars}
    present = fingerprint.sidecars_present
    journal = by_label["journal"]
    wal = by_label["wal"]
    shm = by_label["shm"]
    if journal.present:
        return SqliteSourceObservation(
            status=SOURCE_OBSERVATION_ROLLBACK_JOURNAL,
            sidecars_present=present,
            issue_codes=(CODE_ROLLBACK_JOURNAL,),
            audit_allowed=False,
            apply_allowed=False,
        )
    if wal.present:
        if wal.fingerprint is None:
            raise SqliteSnapshotVerificationFailed()
        if wal.fingerprint.size > 0:
            return SqliteSourceObservation(
                status=SOURCE_OBSERVATION_ACTIVE_WAL,
                sidecars_present=present,
                issue_codes=(CODE_ACTIVE_WAL,),
                audit_allowed=False,
                apply_allowed=False,
            )
        if shm.present and shm.fingerprint is not None:
            return SqliteSourceObservation(
                status=SOURCE_OBSERVATION_STABLE_ZERO_WAL,
                sidecars_present=present,
                issue_codes=(CODE_SOURCE_SIDECARS_PRESENT,),
                audit_allowed=True,
                apply_allowed=False,
            )
    if not present:
        return SqliteSourceObservation(
            status=SOURCE_OBSERVATION_NONE,
            sidecars_present=(),
            issue_codes=(),
            audit_allowed=True,
            apply_allowed=True,
        )
    return SqliteSourceObservation(
        status=SOURCE_OBSERVATION_UNSUPPORTED,
        sidecars_present=present,
        issue_codes=(CODE_UNSUPPORTED_SIDECARS,),
        audit_allowed=False,
        apply_allowed=False,
    )


def _require_same_source(
    first: SqliteSourceFingerprint,
    second: SqliteSourceFingerprint,
) -> None:
    if first.main != second.main:
        raise SqliteSnapshotUnstable(
            source_size=second.main.size,
            source_sha256=second.main.sha256,
            sidecars_present=second.sidecars_present,
        )
    if first.sidecars != second.sidecars:
        raise SqliteSnapshotBlocked(
            code=CODE_SIDECAR_UNSTABLE,
            observation_status=SOURCE_OBSERVATION_SIDECAR_UNSTABLE,
            source_size=second.main.size,
            source_sha256=second.main.sha256,
            sidecars_present=tuple(
                sorted(
                    set(first.sidecars_present)
                    | set(second.sidecars_present)
                )
            ),
        )


def _copy_main_database(source: Path, destination: Path) -> int:
    copied = 0
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            while True:
                chunk = reader.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                writer.write(chunk)
                copied += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        return copied
    except OSError as exc:
        raise SqliteSnapshotCopyFailed() from exc


def _cleanup_snapshot_directory(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError as exc:
        raise SqliteSnapshotCleanupFailed() from exc


def _new_snapshot_directory(
    temp_root: Path | None,
    *,
    source_directory: Path,
) -> Path:
    try:
        system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
        root_path = (
            system_temp
            if temp_root is None
            else temp_root.expanduser().resolve(strict=True)
        )
        if (
            not root_path.is_dir()
            or (
                root_path != system_temp
                and not root_path.is_relative_to(system_temp)
            )
            or root_path == source_directory
            or root_path.is_relative_to(source_directory)
        ):
            raise SqliteSnapshotCopyFailed()
        return Path(
            tempfile.mkdtemp(
                prefix="xrml-sqlite-audit-",
                dir=str(root_path),
            )
        ).resolve()
    except SqliteSnapshotError:
        raise
    except OSError as exc:
        raise SqliteSnapshotCopyFailed() from exc


@contextmanager
def capture_stable_sqlite_snapshot(
    path: Path,
    *,
    stability_delay_seconds: float = DEFAULT_STABILITY_DELAY_SECONDS,
    temp_root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[SqliteAuditSnapshot]:
    """Yield a verified OS-temp copy while never opening SQLite on ``path``."""

    if stability_delay_seconds < 0:
        raise ValueError("stability delay must be non-negative")
    resolved_source = path.expanduser().resolve(strict=True)
    first = capture_sqlite_source_fingerprint(resolved_source)
    sleep(float(stability_delay_seconds))
    second = capture_sqlite_source_fingerprint(resolved_source)
    _require_same_source(first, second)
    observation = classify_sqlite_source(second)
    if not observation.audit_allowed:
        raise SqliteSnapshotBlocked(
            code=observation.issue_codes[0],
            observation_status=observation.status,
            source_size=second.main.size,
            source_sha256=second.main.sha256,
            sidecars_present=observation.sidecars_present,
        )

    temp_directory: Path | None = None
    try:
        temp_directory = _new_snapshot_directory(
            temp_root,
            source_directory=resolved_source.parent,
        )
        snapshot_path = temp_directory / "audit.sqlite3"
        try:
            copied = _copy_main_database(resolved_source, snapshot_path)
        except SqliteSnapshotCopyFailed as exc:
            raise SqliteSnapshotCopyFailed(
                source_size=second.main.size,
                source_sha256=second.main.sha256,
                sidecars_present=second.sidecars_present,
            ) from exc
        third = capture_sqlite_source_fingerprint(resolved_source)
        _require_same_source(second, third)
        if copied != second.main.size:
            raise SqliteSnapshotVerificationFailed(
                source_size=second.main.size,
                source_sha256=second.main.sha256,
                sidecars_present=second.sidecars_present,
            )
        destination = _capture_file(snapshot_path)
        if (
            destination.size != second.main.size
            or destination.sha256 != second.main.sha256
        ):
            raise SqliteSnapshotVerificationFailed(
                source_size=second.main.size,
                source_sha256=second.main.sha256,
                sidecars_present=second.sidecars_present,
            )
        if os.path.samefile(resolved_source, snapshot_path):
            raise SqliteSnapshotVerificationFailed(
                source_size=second.main.size,
                source_sha256=second.main.sha256,
                sidecars_present=second.sidecars_present,
            )
        yield SqliteAuditSnapshot(
            source_path=resolved_source,
            snapshot_path=snapshot_path,
            temp_directory=temp_directory,
            source_fingerprint=third,
            source_observation=observation,
            snapshot_fingerprint=destination,
        )
    finally:
        if temp_directory is not None:
            _cleanup_snapshot_directory(temp_directory)


def open_immutable_snapshot(
    snapshot: SqliteAuditSnapshot,
    *,
    store: str,
    timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> ManagedSqliteConnection:
    """Open only a verified temporary snapshot as immutable/query-only."""

    resolved_snapshot = snapshot.snapshot_path.resolve(strict=True)
    resolved_temp = snapshot.temp_directory.resolve(strict=True)
    system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        resolved_snapshot.parent != resolved_temp
        or (
            resolved_temp != system_temp
            and not resolved_temp.is_relative_to(system_temp)
        )
        or resolved_temp == snapshot.source_path.parent
        or resolved_temp.is_relative_to(snapshot.source_path.parent)
        or _capture_file(resolved_snapshot) != snapshot.snapshot_fingerprint
    ):
        raise SqliteSnapshotVerificationFailed()
    if os.path.samefile(snapshot.source_path, resolved_snapshot):
        raise SqliteSnapshotVerificationFailed()

    connection: ManagedSqliteConnection | None = None
    try:
        uri = f"{resolved_snapshot.as_uri()}?mode=ro&immutable=1&cache=private"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=float(timeout_seconds),
            factory=ManagedSqliteConnection,
        )
        connection.row_factory = sqlite3.Row
        configure_sqlite_read_only(
            connection,
            store=store,
            busy_timeout_ms=max(1, int(float(timeout_seconds) * 1000)),
        )
        return connection
    except SqliteIntegrityError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise SqliteStoreUnavailable(store) from exc

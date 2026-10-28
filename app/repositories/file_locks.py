"""Crash-safe advisory file locks.

Ownership is an **OS handle lock** (``msvcrt.locking`` on Windows,
``fcntl.flock`` on POSIX), not the existence of a marker file. The OS releases
the lock when the owning process dies, so a holder that crashed cannot block
the resource forever. The lock file is therefore left on disk on purpose: its
presence means nothing, only the OS lock does. A lock held by a live owner is
never force-removed and staleness is never inferred from mtime.

Scope boundary: a local, single-host, *advisory* guard. A process that ignores
the protocol is not blocked. No claim is made for network filesystems,
multi-node coordination, or power-loss durability.

This module owns the primitive only. Higher layers decide what the lock
protects (see :mod:`app.repositories.shared_state`).
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.domain.path_safety import is_reparse_point

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 0.05


class FileLockError(RuntimeError):
    """Base class. Carries a stable code, never a path or a traceback."""

    code = "file_lock_failed"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


class FileLockTimeout(FileLockError):
    code = "file_lock_timeout"


class FileLockUnavailable(FileLockError):
    code = "file_lock_unavailable"


def try_acquire_os_lock(handle: int) -> bool:
    """Try to take an exclusive OS lock on an open descriptor.

    The lock belongs to the file handle, so the OS releases it when the owning
    process dies. Returns False when another live owner holds it.
    """
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            return True
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def release_os_lock(handle: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def file_lock(
    path: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path``, or raise :class:`FileLockTimeout`.

    ``path`` must be an application-owned location, never a user-supplied path.
    Callers create the parent directory; this only opens the lock file itself.
    """
    if is_reparse_point(path):
        raise FileLockUnavailable("lock_path_reparse_point")

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    interval = max(0.001, float(poll_seconds))
    handle: int | None = None
    acquired = False
    try:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise FileLockUnavailable("lock_open_failed") from exc
        while True:
            if try_acquire_os_lock(handle):
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise FileLockTimeout("lock_acquire_timeout")
            time.sleep(interval)
        yield
    finally:
        if handle is not None:
            if acquired:
                release_os_lock(handle)
            try:
                os.close(handle)
            except OSError:
                pass

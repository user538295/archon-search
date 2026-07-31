"""Advisory install lock (Task C0-2.1)."""
from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from archon_search.paths import get_data_dir

from .errors import InstallLockError


def _install_lock_path() -> Path:
    return get_data_dir() / ".install.lock"


def _pid_is_alive(pid: int) -> bool:
    """Return True if *pid* refers to a running process, False if dead."""
    if sys.platform == "win32":
        try:
            import psutil  # type: ignore[import-untyped]
            return psutil.pid_exists(pid)
        except ImportError:
            # psutil unavailable on Windows — treat as stale (conservative proceed)
            return False
    else:
        try:
            os.kill(pid, 0)
            return True  # no exception → process alive
        except ProcessLookupError:
            return False  # ESRCH → dead
        except PermissionError:
            return True  # EPERM → alive, different user


@contextlib.contextmanager
def _acquire_install_lock() -> Iterator[None]:
    """Context manager that holds an advisory file-based install lock."""
    lock_path = _install_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _try_create() -> int:
        """Atomically create the lock file; return the fd or raise FileExistsError."""
        return os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # noqa: durable-write — PID lock file; O_EXCL is the atomic guard, not a data write

    def _claim_lock() -> None:
        """Write our PID:timestamp into the fd obtained from _try_create."""
        fd = _try_create()
        try:
            os.write(fd, f"{os.getpid()}:{int(time.time())}".encode())
        except OSError:
            os.close(fd)
            lock_path.unlink(missing_ok=True)
            raise
        os.close(fd)

    retry_done = False
    while True:
        try:
            _claim_lock()
            break  # lock acquired
        except FileExistsError:
            # Lock file already exists — inspect it
            try:
                contents = lock_path.read_text()
                parts = contents.split(":")
                pid = int(parts[0])
            except (ValueError, IndexError, OSError):
                # Corrupted / unreadable → treat as stale
                lock_path.unlink(missing_ok=True)
                if retry_done:
                    raise InstallLockError("Could not acquire install lock (corrupted lock)") from None
                retry_done = True
                continue

            if _pid_is_alive(pid):
                raise InstallLockError(
                    f"Install is already running (PID {pid}). "
                    "Wait for it to finish or remove ~/.archon-search/.install.lock if stale."
                )

            # PID is dead → stale lock
            lock_path.unlink(missing_ok=True)
            if retry_done:
                raise InstallLockError("Could not acquire install lock after removing stale lock") from None
            retry_done = True
            # loop again to retry O_EXCL

    try:
        yield
    finally:
        try:
            contents = lock_path.read_text()
            if contents.split(":")[0] == str(os.getpid()):
                lock_path.unlink(missing_ok=True)
        except OSError:
            lock_path.unlink(missing_ok=True)

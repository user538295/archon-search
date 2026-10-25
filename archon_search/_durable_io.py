"""Durable (fsync-backed) atomic file writes, plus the directory/tree fsync
primitives that make a rename-based publish durable.

Callers must serialize writes to the same path. The helper is not internally
synchronized.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from archon_search.paths import get_data_dir

#: Mode of every directory the app creates for its own state (data dir and below): owner only.
PRIVATE_DIR_MODE = 0o700
#: Mode of every file the app creates for its own state (jobs, logs, telemetry, archives, indexes): owner only.
PRIVATE_FILE_MODE = 0o600


class DurabilityUnconfirmedError(OSError):
    """``os.replace`` published the new file, but fsyncing its directory failed.

    The data is written and readable; only crash durability of the rename is
    unconfirmed. Distinct from a pre-replace ``OSError`` (nothing written), so a
    caller can tell "written" from "not written". Keeps the cause's ``errno``.
    """


def _fsync_path(path: Path) -> None:
    """fsync whatever *path* names — one expression of "how to sync an fd"."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quietly(name: str) -> None:
    """Remove the temp file *name* by the name actually opened, never via a ``Path`` method.

    Suppresses every ``OSError``: cleanup runs while another exception propagates and
    must never replace it.
    """
    with contextlib.suppress(OSError):
        os.unlink(name)


def _fsync_published_dir(path: Path) -> None:
    """fsync *path*'s directory after ``os.replace``; a failure raises :class:`DurabilityUnconfirmedError`."""
    try:
        fsync_dir(path.resolve().parent)
    except OSError as exc:
        raise DurabilityUnconfirmedError(exc.errno, exc.strerror, exc.filename) from exc


def fsync_dir(path: Path) -> None:
    """fsync a directory entry, so a rename published into it survives a crash.

    A no-op on Windows: a directory cannot be opened there (``os.open`` raises ``PermissionError``),
    and NTFS makes ``os.replace`` durable itself, so there is no directory handle to sync.
    """
    if sys.platform == "win32":
        return
    _fsync_path(path)


def restrict_fd_to_owner(fd: int, mode: int = PRIVATE_FILE_MODE) -> None:
    """``fchmod`` *fd* to *mode* where the platform has it; Windows has no per-descriptor mode bits."""
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)


def _data_dir_or_none() -> Path | None:
    try:
        return get_data_dir().resolve()
    except ValueError:
        return None


def _tighten(directory: Path) -> None:
    """chmod *directory* to the private mode when it is ours and looser than that."""
    # Windows has no uid and ignores POSIX mode bits: there is nothing to tighten.
    if not hasattr(os, "geteuid"):
        return
    info = directory.stat()
    if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) != PRIVATE_DIR_MODE:
        directory.chmod(PRIVATE_DIR_MODE)


def ensure_private_dir(path: Path) -> None:
    """Create *path* and every missing ancestor, each ``0o700`` whatever the umask.

    A directory that already exists is left as it is unless it lies inside the data directory
    (``get_data_dir()``, or is it): there the app owns the whole tree, so a loose directory left by an
    earlier version is tightened. Existing directories elsewhere (an operator's ``/srv``, ``/var/log``,
    a shared mount) are never re-permissioned.
    """
    path = Path(path)
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            continue  # created concurrently
        directory.chmod(PRIVATE_DIR_MODE)  # mkdir's mode is masked by the umask on some platforms; be exact
    root = _data_dir_or_none()
    if root is None:
        return
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        return
    for directory in (resolved, *resolved.parents):
        _tighten(directory)
        if directory == root:
            break


def atomic_write_json(path: Path, data: Any, mode: int = PRIVATE_FILE_MODE) -> None:
    """Atomically write `data` as JSON to `path` with durability and a private mode.

    Sequence: open path.tmp (mode *mode*) -> write -> flush -> os.fsync(file_fd) -> os.replace(tmp, path)
    -> os.fsync(parent_dir_fd).

    The file is private to the owner (``0o600``) whatever the umask: *mode* applies at creation and is
    re-applied to a leftover temp file, so the published file — which replaces a world-readable predecessor —
    never carries looser permissions.

    Raises OSError on any underlying I/O failure. The temp file is opened before the
    cleanup scope, so a failed open never unlinks a file this call did not open. On any
    later failure before os.replace completes (serialization, fsync, replace, or any
    other exception) the temp file is unlinked before re-raising (POSIX fsyncgate: do
    NOT retry fsync). A failure after os.replace succeeds raises
    :class:`DurabilityUnconfirmedError`: the data is written but rename durability is
    unconfirmed.

    Concurrency precondition: callers must serialize writes to the same path. The
    helper is not internally synchronized.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    fh = os.fdopen(fd, "w")
    try:
        with fh:
            restrict_fd_to_owner(fh.fileno(), mode)
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(str(tmp))
        raise
    _fsync_published_dir(path)


def atomic_write_bytes(path: Path, data: bytes, mode: int = PRIVATE_FILE_MODE, *, replace: bool = True) -> None:
    """Atomically write `data` to `path` with durability and a creation-time mode.

    Sequence: os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, mode) -> os.write -> os.fsync(fd)
    -> os.close -> os.replace -> os.fsync(parent_dir_fd).

    With ``replace=False`` the temp file is published with ``os.link`` instead of ``os.replace``, so an existing
    *path* is never overwritten: the call raises ``FileExistsError`` and the temp file is removed. That is the
    race-free "create unless someone else already did" two processes need to agree on one file.

    Mode is set at file creation (no chmod-after window). On EEXIST of the temp file the helper raises
    FileExistsError without retry and without unlinking the pre-existing tmp -- the
    O_EXCL collision is signal, not noise. Raises OSError on any other I/O failure;
    on any exception after the temp file is created (OSError or not, e.g. a TypeError
    from a non-path *path*) the temp file is unlinked before re-raising, so the payload
    never lingers on disk. A failure after the publish succeeds raises
    :class:`DurabilityUnconfirmedError`: the data is written but rename durability is
    unconfirmed.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp_name = str(tmp)
    fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    closed = False
    try:
        os.write(fd, data)
        os.fsync(fd)
        closed = True
        os.close(fd)
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
            _unlink_quietly(tmp_name)
    except BaseException:
        if not closed:
            os.close(fd)
        _unlink_quietly(tmp_name)
        raise
    _fsync_published_dir(path)


def fsync_tree(root: Path) -> None:
    """fsync every regular file under *root*, then every directory deepest-first.

    Complements the single-file ``atomic_write_*`` helpers above for the case
    where a whole extracted directory tree is published with one
    same-filesystem rename: the rename is atomic, but only durable if its
    contents were already on disk.

    Nested directories must be fsynced too, not just *root*: fsyncing a file
    makes its *data* durable, but its *directory entry* only becomes durable
    when the containing directory is fsynced. A downloaded model tree nests its
    weights under several subdirectories, so syncing only *root* can survive a
    crash as a valid-looking top-level manifest beside empty subdirectories —
    exactly the "on disk but unloadable" state the caller relies on this to
    prevent (2026-08-19-030 C2-I-4).
    """
    directories: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            _fsync_path(path)
    # Deepest-first: a child's entry must be durable before its parent is synced.
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        fsync_dir(directory)
    fsync_dir(root)

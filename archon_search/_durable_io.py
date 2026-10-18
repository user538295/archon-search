"""Durable (fsync-backed) atomic file writes, plus the directory/tree fsync
primitives that make a rename-based publish durable.

Callers must serialize writes to the same path. The helper is not internally
synchronized.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any


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
    """fsync a directory entry, so a rename published into it survives a crash."""
    _fsync_path(path)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write `data` as JSON to `path` with durability.

    Sequence: open path.tmp -> write -> flush -> os.fsync(file_fd) -> os.replace(tmp, path)
    -> os.fsync(parent_dir_fd).

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
    fh = open(tmp, "w")
    try:
        with fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(str(tmp))
        raise
    _fsync_published_dir(path)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write `data` to `path` with durability and a creation-time mode.

    Sequence: os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, mode) -> os.write -> os.fsync(fd)
    -> os.close -> os.replace -> os.fsync(parent_dir_fd).

    Mode is set at file creation (no chmod-after window). On EEXIST the helper raises
    FileExistsError without retry and without unlinking the pre-existing tmp -- the
    O_EXCL collision is signal, not noise. Raises OSError on any other I/O failure;
    on any exception after the temp file is created (OSError or not, e.g. a TypeError
    from a non-path *path*) the temp file is unlinked before re-raising, so the payload
    never lingers on disk. A failure after os.replace succeeds raises
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
        os.replace(tmp, path)
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

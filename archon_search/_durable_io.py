"""Durable (fsync-backed) atomic file writes, plus the directory/tree fsync
primitives that make a rename-based publish durable.

Callers must serialize writes to the same path. The helper is not internally
synchronized.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _fsync_path(path: Path) -> None:
    """fsync whatever *path* names — one expression of "how to sync an fd"."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """fsync a directory entry, so a rename published into it survives a crash."""
    _fsync_path(path)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write `data` as JSON to `path` with durability.

    Sequence: write to path.tmp -> flush -> os.fsync(file_fd) -> os.replace(tmp, path)
    -> os.fsync(parent_dir_fd).

    Raises OSError on any underlying I/O failure. On fsync/replace failure the temp
    file is unlinked before re-raising (POSIX fsyncgate: do NOT retry fsync). An
    OSError raised after os.replace succeeds means the data is written but rename
    durability is unconfirmed.

    Concurrency precondition: callers must serialize writes to the same path. The
    helper is not internally synchronized.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.resolve().parent)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write `data` to `path` with durability and a creation-time mode.

    Sequence: os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, mode) -> os.write -> os.fsync(fd)
    -> os.close -> os.replace -> os.fsync(parent_dir_fd).

    Mode is set at file creation (no chmod-after window). On EEXIST the helper raises
    FileExistsError without retry and without unlinking the pre-existing tmp -- the
    O_EXCL collision is signal, not noise. Raises OSError on any other I/O failure;
    the temp file we created is unlinked before re-raising. An OSError raised after
    os.replace succeeds means the data is written but rename durability is unconfirmed.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    closed = False
    try:
        os.write(fd, data)
        os.fsync(fd)
        closed = True
        os.close(fd)
        os.replace(tmp, path)
    except OSError:
        if not closed:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.resolve().parent)


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

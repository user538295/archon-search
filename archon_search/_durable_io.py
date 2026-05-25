"""Durable (fsync-backed) atomic file writes.

Callers must serialize writes to the same path. The helper is not internally
synchronized.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


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
    dir_fd = os.open(path.resolve().parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
    dir_fd = os.open(path.resolve().parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

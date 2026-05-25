"""Shared helpers for crash-injection integration tests.

The crash-injection tests in this package require a disk-backed filesystem:
``os._exit()`` mid-write only proves the durable-write call sequence if the
backing store actually persists (or rolls back) across the simulated crash.
On a ``tmpfs`` mount the data lives in RAM, so the tests are meaningless and
must skip. ``_tmp_is_tmpfs`` answers "is this path on a tmpfs mount?" by
parsing the Linux mount table.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _read_mountinfo() -> str:
    """Return the raw contents of ``/proc/self/mountinfo``.

    Factored out as a seam so tests can monkeypatch the mount table without
    touching the real ``/proc``.
    """
    with open("/proc/self/mountinfo", encoding="utf-8") as fh:
        return fh.read()


def _parse_mountinfo(text: str) -> list[tuple[Path, str]]:
    """Parse mountinfo text into ``(mount_point, fstype)`` pairs.

    mountinfo format (man 5 proc): fields are space-separated; the mount point
    is field index 4 (0-based). After a literal ``-`` separator field, the next
    field is the filesystem type. Optional fields may appear between index 6 and
    the ``-`` separator, so we locate ``-`` rather than assuming a fixed offset.
    """
    mounts: list[tuple[Path, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or "-" not in fields:
            continue
        mount_point = fields[4]
        sep = fields.index("-")
        if sep + 1 >= len(fields):
            continue
        fstype = fields[sep + 1]
        mounts.append((Path(mount_point), fstype))
    return mounts


def _is_prefix(mount: Path, target: Path) -> bool:
    """True if ``mount`` is ``target`` or an ancestor directory of ``target``."""
    if mount == target:
        return True
    try:
        target.relative_to(mount)
    except ValueError:
        return False
    return True


def _tmp_is_tmpfs(path: Path) -> bool:
    """Return True iff ``path`` resides on a ``tmpfs`` mount.

    On Linux, parse the mount table and pick the mount point that is the
    longest path-prefix of ``path.resolve()`` (the mount actually backing the
    path), then check its fstype. On non-Linux platforms, return False.
    """
    if not sys.platform.startswith("linux"):
        return False

    resolved = path.resolve()
    mounts = _parse_mountinfo(_read_mountinfo())

    best: tuple[Path, str] | None = None
    for mount_point, fstype in mounts:
        if not _is_prefix(mount_point, resolved):
            continue
        if best is None or len(mount_point.parts) > len(best[0].parts):
            best = (mount_point, fstype)

    return best is not None and best[1] == "tmpfs"

"""Unit tests for the ``_tmp_is_tmpfs`` mount-table parser.

These feed a fixture mountinfo string via the monkeypatchable
``tests.integration._helpers._read_mountinfo`` seam rather than touching the
real ``/proc``, so the longest-prefix and fstype logic is exercised
deterministically on any host.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tests.integration import _helpers
from tests.integration._helpers import _tmp_is_tmpfs

# A root ext4 mount plus a tmpfs mount over /tmp. Any path under /tmp must
# resolve to the longer (tmpfs) match, not the root ext4 mount.
_MOUNTINFO_TMPFS_OVER_TMP = (
    "24 30 8:1 / / rw,relatime shared:1 - ext4 /dev/vda rw\n"
    "36 35 0:30 / /tmp rw,nosuid,nodev shared:15 - tmpfs tmpfs rw,size=8192000k\n"
)

# A root ext4 mount plus an ext4 mount over /tmp; nothing is tmpfs.
_MOUNTINFO_EXT4_OVER_TMP = (
    "24 30 8:1 / / rw,relatime shared:1 - ext4 /dev/vda rw\n"
    "36 35 8:2 / /tmp rw,relatime shared:15 - ext4 /dev/vdb rw\n"
)


@pytest.mark.integration
def test_tmp_is_tmpfs_detects_tmpfs_mount(monkeypatch):
    monkeypatch.setattr(_helpers.sys, "platform", "linux")
    with mock.patch(
        "tests.integration._helpers._read_mountinfo",
        return_value=_MOUNTINFO_TMPFS_OVER_TMP,
    ):
        # Path under /tmp must select the longer /tmp (tmpfs) match over /.
        assert _tmp_is_tmpfs(Path("/tmp/archon-search-it/some-test")) is True


@pytest.mark.integration
def test_tmp_is_tmpfs_detects_ext4_mount(monkeypatch):
    monkeypatch.setattr(_helpers.sys, "platform", "linux")
    with mock.patch(
        "tests.integration._helpers._read_mountinfo",
        return_value=_MOUNTINFO_EXT4_OVER_TMP,
    ):
        assert _tmp_is_tmpfs(Path("/tmp/archon-search-it/some-test")) is False


@pytest.mark.integration
def test_tmp_is_tmpfs_non_linux_returns_false(monkeypatch):
    monkeypatch.setattr(_helpers.sys, "platform", "darwin")
    # Short-circuit: mountinfo must never be read on non-Linux.
    with mock.patch(
        "tests.integration._helpers._read_mountinfo",
        side_effect=AssertionError("mountinfo must not be read on non-Linux"),
    ):
        assert _tmp_is_tmpfs(Path("/tmp/whatever")) is False

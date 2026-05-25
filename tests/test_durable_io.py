from __future__ import annotations

import errno
import json
import os
import stat
import sys
from unittest import mock

import pytest

from archon_search._durable_io import atomic_write_bytes, atomic_write_json


class TestAtomicWriteJson:
    def test_atomic_write_json_writes_data(self, tmp_path):
        path = tmp_path / "data.json"
        payload = {"a": 1, "b": ["x", "y"]}
        atomic_write_json(path, payload)
        assert json.loads(path.read_text()) == payload

    def test_atomic_write_json_fsync_call_sequence(self, tmp_path):
        path = tmp_path / "data.json"
        calls: list[tuple[str, str]] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def record_fsync(fd):
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            calls.append(("fsync", kind))
            return real_fsync(fd)

        def record_replace(src, dst):
            calls.append(("replace", ""))
            return real_replace(src, dst)

        with mock.patch("os.fsync", side_effect=record_fsync), mock.patch(
            "os.replace", side_effect=record_replace
        ):
            atomic_write_json(path, {"k": "v"})

        assert calls == [("fsync", "file"), ("replace", ""), ("fsync", "dir")]

    def test_atomic_write_json_unlinks_tmp_on_fsync_failure(self, tmp_path):
        path = tmp_path / "data.json"
        tmp = path.with_suffix(path.suffix + ".tmp")

        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "io")), mock.patch(
            "os.replace"
        ) as replace_mock:
            with pytest.raises(OSError) as excinfo:
                atomic_write_json(path, {"k": "v"})

        assert excinfo.value.errno == errno.EIO
        assert not tmp.exists()
        replace_mock.assert_not_called()

    def test_atomic_write_json_unlinks_tmp_on_replace_failure(self, tmp_path):
        path = tmp_path / "data.json"
        tmp = path.with_suffix(path.suffix + ".tmp")

        with mock.patch("os.replace", side_effect=OSError(errno.EXDEV, "xdev")):
            with pytest.raises(OSError) as excinfo:
                atomic_write_json(path, {"k": "v"})

        assert excinfo.value.errno == errno.EXDEV
        assert not tmp.exists()

    def test_atomic_write_json_does_not_retry_fsync(self, tmp_path):
        path = tmp_path / "data.json"

        fsync_mock = mock.Mock(side_effect=OSError(errno.EIO, "io"))
        with mock.patch("os.fsync", fsync_mock):
            with pytest.raises(OSError):
                atomic_write_json(path, {"k": "v"})

        assert fsync_mock.call_count == 1

    def test_atomic_write_json_overwrites_existing(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text(json.dumps({"old": True}))
        atomic_write_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"new": True}

    def test_atomic_write_json_dir_fsync_failure_propagates(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text(json.dumps({"old": True}))

        opened_fds: list[int] = []
        real_open = os.open

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened_fds.append(fd)
            return fd

        real_close = os.close
        closed_fds: list[int] = []

        def record_close(fd):
            closed_fds.append(fd)
            real_close(fd)

        # First fsync (file) succeeds; second fsync (parent dir) raises.
        fsync_mock = mock.Mock(side_effect=[None, OSError(errno.EIO, "dir")])
        with mock.patch("os.fsync", fsync_mock), mock.patch(
            "os.open", side_effect=record_open
        ), mock.patch("os.close", side_effect=record_close):
            with pytest.raises(OSError) as excinfo:
                atomic_write_json(path, {"new": True})

        # (a) the OSError propagates out of the function.
        assert excinfo.value.errno == errno.EIO
        # (b) replace happened before the dir fsync: the new data is committed.
        assert json.loads(path.read_text()) == {"new": True}
        # (c) the dir fd was still closed despite the fsync failure (no leak).
        #     The JSON path only os.open()s the dir fd, so it is closed exactly once.
        dir_fd = opened_fds[-1]
        assert closed_fds.count(dir_fd) == 1


class TestAtomicWriteBytes:
    def test_atomic_write_bytes_writes_data(self, tmp_path):
        path = tmp_path / "blob.bin"
        payload = b"hello\x00world"
        atomic_write_bytes(path, payload)
        assert path.read_bytes() == payload

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_atomic_write_bytes_mode_is_0600(self, tmp_path):
        path = tmp_path / "blob.bin"
        atomic_write_bytes(path, b"data")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_atomic_write_bytes_mode_is_set_at_creation(self, tmp_path):
        path = tmp_path / "blob.bin"
        with mock.patch("os.chmod") as chmod_mock:
            atomic_write_bytes(path, b"data")
        chmod_mock.assert_not_called()

    def test_atomic_write_bytes_raises_on_existing_tmp(self, tmp_path):
        path = tmp_path / "blob.bin"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(b"preexisting")

        with pytest.raises(FileExistsError):
            atomic_write_bytes(path, b"data")

        assert tmp.exists()
        assert tmp.read_bytes() == b"preexisting"

    def test_atomic_write_bytes_fsync_call_sequence(self, tmp_path):
        path = tmp_path / "blob.bin"
        calls: list[tuple[str, str]] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def record_fsync(fd):
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            calls.append(("fsync", kind))
            return real_fsync(fd)

        def record_replace(src, dst):
            calls.append(("replace", ""))
            return real_replace(src, dst)

        with mock.patch("os.fsync", side_effect=record_fsync), mock.patch(
            "os.replace", side_effect=record_replace
        ):
            atomic_write_bytes(path, b"data")

        assert calls == [("fsync", "file"), ("replace", ""), ("fsync", "dir")]

    def test_atomic_write_bytes_unlinks_tmp_on_fsync_failure(self, tmp_path):
        path = tmp_path / "blob.bin"
        tmp = path.with_suffix(path.suffix + ".tmp")

        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "io")), mock.patch(
            "os.replace"
        ) as replace_mock:
            with pytest.raises(OSError) as excinfo:
                atomic_write_bytes(path, b"data")

        assert excinfo.value.errno == errno.EIO
        assert not tmp.exists()
        replace_mock.assert_not_called()

    def test_atomic_write_bytes_unlinks_tmp_on_replace_failure(self, tmp_path):
        path = tmp_path / "blob.bin"
        tmp = path.with_suffix(path.suffix + ".tmp")

        with mock.patch("os.replace", side_effect=OSError(errno.EXDEV, "xdev")):
            with pytest.raises(OSError) as excinfo:
                atomic_write_bytes(path, b"data")

        assert excinfo.value.errno == errno.EXDEV
        assert not tmp.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_atomic_write_bytes_custom_mode(self, tmp_path):
        path = tmp_path / "blob.bin"
        atomic_write_bytes(path, b"data", mode=0o644)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    def test_atomic_write_bytes_dir_fsync_failure_propagates(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(b"old")

        opened_fds: list[int] = []
        real_open = os.open

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened_fds.append(fd)
            return fd

        real_close = os.close
        closed_fds: list[int] = []

        def record_close(fd):
            closed_fds.append(fd)
            real_close(fd)

        # First fsync (file) succeeds; second fsync (parent dir) raises.
        fsync_mock = mock.Mock(side_effect=[None, OSError(errno.EIO, "dir")])
        with mock.patch("os.fsync", fsync_mock), mock.patch(
            "os.open", side_effect=record_open
        ), mock.patch("os.close", side_effect=record_close):
            with pytest.raises(OSError) as excinfo:
                atomic_write_bytes(path, b"new")

        # (a) the OSError propagates out of the function.
        assert excinfo.value.errno == errno.EIO
        # (b) replace happened before the dir fsync: the new data is committed.
        assert path.read_bytes() == b"new"
        # (c) the dir fd was still closed despite the fsync failure (no leak).
        #     The bytes path also closes the file fd, so just require the dir fd
        #     to be among the closed fds.
        dir_fd = opened_fds[-1]
        assert dir_fd in closed_fds

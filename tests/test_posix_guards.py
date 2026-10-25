"""H5: startup and file-write paths must not need POSIX-only calls.

The docs list Windows as supported, but ``os.geteuid``, ``os.fchmod`` and ``os.O_NOFOLLOW``
do not exist there and a directory cannot be opened (so fsynced) with ``os.open``. These tests
remove or fake exactly those facilities on the host platform: nothing here runs on Windows.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from archon_search import _durable_io
from archon_search.jobs.export_archive import _open_private
from archon_search.logging_setup import _PrivateTimedRotatingFileHandler
from archon_search.parser import _read_regular_text


@pytest.fixture
def no_fchmod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "fchmod", raising=False)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(root))
    return root


def test_atomic_write_json_works_without_fchmod(no_fchmod: None, tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    _durable_io.atomic_write_json(target, {"a": 1})

    assert target.read_text() == '{"a": 1}'


def test_ensure_private_dir_works_without_geteuid(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    monkeypatch.delattr(os, "geteuid", raising=False)
    nested = data_dir / "search" / "logs"

    _durable_io.ensure_private_dir(nested)

    assert nested.is_dir()


def test_fsync_dir_is_skipped_on_windows_where_a_directory_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_open = os.open

    def _open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path).is_dir():
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", _open)
    monkeypatch.setattr("sys.platform", "win32")

    _durable_io.fsync_dir(tmp_path)
    target = tmp_path / "state.json"
    _durable_io.atomic_write_bytes(target, b"x")

    assert target.read_bytes() == b"x"


def test_fsync_dir_still_syncs_the_directory_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(_durable_io, "_fsync_path", synced.append)

    _durable_io.fsync_dir(tmp_path)

    assert synced == [tmp_path]


def test_private_log_handler_opens_without_fchmod(no_fchmod: None, tmp_path: Path) -> None:
    handler = _PrivateTimedRotatingFileHandler(str(tmp_path / "app.log"))
    try:
        handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None))
    finally:
        handler.close()

    assert "hello" in (tmp_path / "app.log").read_text()


def test_export_archive_private_open_works_without_fchmod(no_fchmod: None, tmp_path: Path) -> None:
    with _open_private(tmp_path / "out.jsonl", os.O_TRUNC, "wb") as handle:
        handle.write(b"x")

    assert (tmp_path / "out.jsonl").read_bytes() == b"x"


def test_parser_reads_text_without_o_nofollow_and_o_nonblock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    source = tmp_path / "note.txt"
    source.write_text("héllo")

    assert _read_regular_text(source) == "héllo"

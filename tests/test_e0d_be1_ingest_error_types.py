"""BE-1: IngestError, _file_exceeds_limit helper, IngestResult.code field.

Tests for the Entities layer additions introduced in E0d.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_search._types import IngestError, IngestResult, _file_exceeds_limit


def test_ingest_error_carries_code_and_message() -> None:
    """IngestError has code='file_too_large' and a non-empty message."""
    err = IngestError(file_size_mb=150, limit_mb=100)
    assert err.code == "file_too_large"
    assert len(err.message) > 0


def test_ingest_error_message_format() -> None:
    """Message names both sizes and config key in the expected format."""
    err = IngestError(file_size_mb=150, limit_mb=100)
    expected = (
        "File size 150 MB exceeds the configured limit of 100 MB "
        "(`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."
    )
    assert err.message == expected


def test_ingest_error_is_exception() -> None:
    """IngestError is a proper Exception subclass with correct message propagation."""
    err = IngestError(file_size_mb=150, limit_mb=100)
    assert isinstance(err, Exception)
    assert str(err) == err.message

    with pytest.raises(IngestError):
        raise IngestError(file_size_mb=200, limit_mb=50)


def test_ingest_result_code_defaults_none() -> None:
    """IngestResult.code is None when not set."""
    result = IngestResult(doc_id="abc", chunks_created=3, status="ok")
    assert result.code is None


def test_ingest_result_code_set() -> None:
    """IngestResult.code='file_too_large' survives dataclass creation."""
    result = IngestResult(doc_id="abc", chunks_created=0, status="error", code="file_too_large")
    assert result.code == "file_too_large"


def test_file_exceeds_limit_helper_boundary(tmp_path: Path) -> None:
    """File exactly at max_file_mb returns False; one byte over returns True."""
    # Create a file of exactly 1 MB (1 * 1024 * 1024 bytes)
    limit_mb = 1
    exactly_limit = tmp_path / "exact.bin"
    one_mb = limit_mb * 1024 * 1024
    exactly_limit.write_bytes(b"\x00" * one_mb)

    # Exactly at limit: should NOT exceed (strictly greater-than)
    assert _file_exceeds_limit(exactly_limit, limit_mb) is False

    # One byte over: should exceed
    one_over = tmp_path / "over.bin"
    one_over.write_bytes(b"\x00" * (one_mb + 1))
    assert _file_exceeds_limit(one_over, limit_mb) is True


def test_file_exceeds_limit_zero_disables_check(tmp_path: Path) -> None:
    """max_file_mb=0 means no limit — always returns False regardless of size."""
    tiny = tmp_path / "tiny.bin"
    tiny.write_bytes(b"\x00")  # 1 byte — size is irrelevant when limit is 0
    assert _file_exceeds_limit(tiny, 0) is False


def test_file_exceeds_limit_follows_symlinks(tmp_path: Path) -> None:
    """_file_exceeds_limit follows symlinks (os.path.getsize follows symlinks)."""
    target = tmp_path / "real.bin"
    two_mb = 2 * 1024 * 1024
    target.write_bytes(b"\x00" * two_mb)
    link = tmp_path / "link.bin"
    os.symlink(target, link)

    # limit = 1 MB; symlink target is 2 MB → exceeds
    assert _file_exceeds_limit(link, 1) is True


def test_file_exceeds_limit_negative_max_file_mb_disables_check(tmp_path: Path) -> None:
    """Negative max_file_mb is treated as no limit — always returns False."""
    tiny = tmp_path / "tiny.bin"
    tiny.write_bytes(b"\x00")  # 1 byte — size is irrelevant when limit <= 0
    assert _file_exceeds_limit(tiny, -1) is False

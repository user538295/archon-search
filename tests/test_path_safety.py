"""Unit tests for archon_search._path_safety."""
import os
from pathlib import Path

import pytest

from archon_search._path_safety import PathUnsafeError, validate_ingest_path


def test_accepts_absolute_path():
    result = validate_ingest_path("/tmp/foo.md")
    assert result == Path("/tmp/foo.md").resolve()


def test_accepts_path_with_spaces_and_unicode():
    result = validate_ingest_path("/home/user/My Documents/notés.md")
    assert result == Path("/home/user/My Documents/notés.md").resolve()


def test_accepts_tilde_expansion():
    result = validate_ingest_path("~/foo")
    assert result.is_absolute()
    assert result != Path("~/foo")
    assert "~" not in str(result)


def test_accepts_dotdot_substring_in_dirname():
    # "..backup" is a dir name containing ".." as substring — not a ".." part
    result = validate_ingest_path("/data/..backup/x.md")
    assert result.is_absolute()


def test_accepts_nonexistent_absolute_path():
    result = validate_ingest_path("/no/such/file.md")
    assert result.is_absolute()


def test_accepts_trailing_slash():
    result = validate_ingest_path("/tmp/foo/")
    assert result.is_absolute()


def test_accepts_long_absolute_path():
    long_path = "/tmp/" + "a" * 200 + ".md"
    result = validate_ingest_path(long_path)
    assert result.is_absolute()


def test_accepts_symlink_without_dotdot(tmp_path):
    real_file = tmp_path / "real.md"
    real_file.write_text("content")
    symlink = tmp_path / "link.md"
    try:
        os.symlink(real_file, symlink)
    except OSError:
        pytest.skip("os.symlink not supported on this platform")
    result = validate_ingest_path(str(symlink))
    assert result.is_absolute()


def test_rejects_dotdot_standalone():
    # ".." is relative, so not_absolute fires before contains_dotdot per check order
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("..")
    assert exc_info.value.reason == "not_absolute"


def test_rejects_dotdot_mid_path():
    # "/foo/../bar" is absolute, passes absoluteness check, then fails on ".." part
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/foo/../bar")
    assert exc_info.value.reason == "contains_dotdot"


def test_rejects_relative_dotdot_path():
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("../foo")
    assert exc_info.value.reason == "not_absolute"


def test_rejects_empty_string():
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("")
    assert exc_info.value.reason == "empty"


def test_rejects_whitespace_only():
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("   ")
    assert exc_info.value.reason == "whitespace_only"


def test_rejects_nul_byte():
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/tmp/foo\x00.md")
    assert exc_info.value.reason == "nul_byte"


def test_rejects_relative_path():
    for raw in ["./foo", "foo/bar", "."]:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_ingest_path(raw)
        assert exc_info.value.reason == "not_absolute", f"expected not_absolute for {raw!r}"


def test_path_unsafe_error_is_value_error():
    assert isinstance(PathUnsafeError("x"), ValueError)


def test_path_unsafe_error_carries_reason():
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/foo/../bar")
    assert exc_info.value.reason == "contains_dotdot"

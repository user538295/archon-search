"""Unit tests for archon_search._path_safety (A5a).

Tests cover validate_ingest_path and PathUnsafeError.
These tests are written FIRST (TDD red state) and should all pass once the
implementation module is created.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Happy-path (accept) tests
# ---------------------------------------------------------------------------


def test_accepts_absolute_path():
    """Absolute path /tmp/foo.md returns the resolved Path."""
    from archon_search._path_safety import validate_ingest_path
    from pathlib import Path

    result = validate_ingest_path("/tmp/foo.md")
    assert result.is_absolute()
    # Should be a Path object
    assert isinstance(result, Path)


def test_accepts_path_with_spaces_and_unicode():
    """/home/user/My Documents/notés.md returns the resolved Path."""
    from archon_search._path_safety import validate_ingest_path
    from pathlib import Path

    result = validate_ingest_path("/home/user/My Documents/notés.md")
    assert isinstance(result, Path)
    assert result.is_absolute()


def test_accepts_tilde_expansion():
    """~/foo returns an absolute path (not ~/foo)."""
    from archon_search._path_safety import validate_ingest_path
    from pathlib import Path

    result = validate_ingest_path("~/foo")
    assert result.is_absolute()
    assert result != Path("~/foo")


def test_accepts_dotdot_substring_in_dirname():
    """/data/..backup/x.md is accepted — ..backup is a real dir name, not a '..' part."""
    from archon_search._path_safety import validate_ingest_path
    from pathlib import Path

    result = validate_ingest_path("/data/..backup/x.md")
    assert isinstance(result, Path)
    assert result.is_absolute()


def test_accepts_nonexistent_absolute_path():
    """/no/such/file.md passes the validator; existence is downstream's concern."""
    from archon_search._path_safety import validate_ingest_path
    from pathlib import Path

    result = validate_ingest_path("/no/such/file.md")
    assert isinstance(result, Path)
    assert result.is_absolute()


def test_accepts_trailing_slash():
    """/tmp/foo/ is accepted."""
    from archon_search._path_safety import validate_ingest_path
    from pathlib import Path

    result = validate_ingest_path("/tmp/foo/")
    assert isinstance(result, Path)
    assert result.is_absolute()


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------


def test_rejects_dotdot_standalone():
    """'..' alone raises PathUnsafeError with reason='contains_dotdot'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("..")
    assert exc_info.value.reason == "contains_dotdot"


def test_rejects_dotdot_mid_path():
    """/foo/../bar is rejected with reason='contains_dotdot'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/foo/../bar")
    assert exc_info.value.reason == "contains_dotdot"


def test_rejects_relative_dotdot_path():
    """../foo is rejected with reason='not_absolute' (relative fires before dotdot-parts)."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("../foo")
    # Per plan C1-I-DA1-1: relative-path rejection fires before dotdot-parts rejection
    assert exc_info.value.reason == "not_absolute"


def test_rejects_empty_string():
    """Empty string raises PathUnsafeError with reason='empty'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("")
    assert exc_info.value.reason == "empty"


def test_rejects_whitespace_only():
    """Whitespace-only string raises PathUnsafeError with reason='whitespace_only'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("   ")
    assert exc_info.value.reason == "whitespace_only"


def test_rejects_nul_byte():
    """/tmp/foo\\x00.md raises PathUnsafeError with reason='nul_byte'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/tmp/foo\x00.md")
    assert exc_info.value.reason == "nul_byte"


def test_rejects_relative_path_dot_slash():
    """./foo raises PathUnsafeError with reason='not_absolute'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("./foo")
    assert exc_info.value.reason == "not_absolute"


def test_rejects_relative_path_plain():
    """foo/bar raises PathUnsafeError with reason='not_absolute'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("foo/bar")
    assert exc_info.value.reason == "not_absolute"


def test_rejects_relative_path_dot():
    """'.' raises PathUnsafeError with reason='not_absolute'."""
    from archon_search._path_safety import validate_ingest_path, PathUnsafeError

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path(".")
    assert exc_info.value.reason == "not_absolute"


# ---------------------------------------------------------------------------
# PathUnsafeError contract tests
# ---------------------------------------------------------------------------


def test_path_unsafe_error_is_value_error():
    """PathUnsafeError is a subclass of ValueError."""
    from archon_search._path_safety import PathUnsafeError

    assert isinstance(PathUnsafeError("x"), ValueError)


def test_path_unsafe_error_carries_reason():
    """PathUnsafeError.reason survives a raise/except round-trip."""
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    try:
        validate_ingest_path("/foo/../bar")
    except PathUnsafeError as e:
        assert e.reason == "contains_dotdot"
    else:
        pytest.fail("Expected PathUnsafeError was not raised")

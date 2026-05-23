"""Unit tests for archon_search._path_safety."""
from pathlib import Path

import pytest


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_accepts_absolute_path():
    from archon_search._path_safety import validate_ingest_path

    result = validate_ingest_path("/tmp/foo.md")
    assert result == Path("/tmp/foo.md").resolve()


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_accepts_path_with_spaces_and_unicode():
    from archon_search._path_safety import validate_ingest_path

    result = validate_ingest_path("/home/user/My Documents/notés.md")
    assert result == Path("/home/user/My Documents/notés.md").resolve()


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_accepts_tilde_expansion():
    from archon_search._path_safety import validate_ingest_path

    result = validate_ingest_path("~/foo")
    assert result.is_absolute()
    assert result != Path("~/foo")


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_accepts_dotdot_substring_in_dirname():
    from archon_search._path_safety import validate_ingest_path

    # "..backup" is a dir name containing ".." as substring — not a ".." part
    result = validate_ingest_path("/data/..backup/x.md")
    assert result.is_absolute()


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_accepts_nonexistent_absolute_path():
    from archon_search._path_safety import validate_ingest_path

    result = validate_ingest_path("/no/such/file.md")
    assert result.is_absolute()


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_accepts_trailing_slash():
    from archon_search._path_safety import validate_ingest_path

    result = validate_ingest_path("/tmp/foo/")
    assert result.is_absolute()


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_dotdot_standalone():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    # ".." is relative, so not_absolute fires before contains_dotdot per check order
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("..")
    assert exc_info.value.reason == "not_absolute"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_dotdot_mid_path():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    # "/foo/../bar" is absolute, passes absoluteness check, then fails on ".." part
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/foo/../bar")
    assert exc_info.value.reason == "contains_dotdot"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_relative_dotdot_path():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("../foo")
    assert exc_info.value.reason == "not_absolute"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_empty_string():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("")
    assert exc_info.value.reason == "empty"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_whitespace_only():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("   ")
    assert exc_info.value.reason == "whitespace_only"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_nul_byte():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    with pytest.raises(PathUnsafeError) as exc_info:
        validate_ingest_path("/tmp/foo\x00.md")
    assert exc_info.value.reason == "nul_byte"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_rejects_relative_path():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    for raw in ["./foo", "foo/bar", "."]:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_ingest_path(raw)
        assert exc_info.value.reason == "not_absolute", f"expected not_absolute for {raw!r}"


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_path_unsafe_error_is_value_error():
    from archon_search._path_safety import PathUnsafeError

    assert isinstance(PathUnsafeError("x"), ValueError)


@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)
def test_path_unsafe_error_carries_reason():
    from archon_search._path_safety import PathUnsafeError, validate_ingest_path

    try:
        validate_ingest_path("/foo/../bar")
    except PathUnsafeError as e:
        assert e.reason == "contains_dotdot"
    else:
        pytest.fail("PathUnsafeError not raised")

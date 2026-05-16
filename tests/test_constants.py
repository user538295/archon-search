"""Tests for _validate_namespace() in archon_search.constants."""

import pytest

from archon_search.constants import _validate_namespace


def test_validate_namespace_valid_names() -> None:
    for name in ("default", "tenantA", "tenant-1", "a", "Z"):
        _validate_namespace(name)  # must not raise


def test_validate_namespace_invalid_starts_with_underscore() -> None:
    with pytest.raises(ValueError):
        _validate_namespace("_bad")


def test_validate_namespace_invalid_empty() -> None:
    with pytest.raises(ValueError):
        _validate_namespace("")


def test_validate_namespace_too_long() -> None:
    # 65-char name → ValueError
    with pytest.raises(ValueError):
        _validate_namespace("a" * 65)
    # 64-char name → passes
    _validate_namespace("a" * 64)


@pytest.mark.parametrize("name", ["has space", "has.dot", "-bad", "valid\n"])
def test_validate_namespace_invalid_special_chars(name: str) -> None:
    with pytest.raises(ValueError):
        _validate_namespace(name)


def test_validate_namespace_exactly_64_chars() -> None:
    name = "A" + "b" * 63  # 64 chars, starts with letter
    _validate_namespace(name)  # must not raise

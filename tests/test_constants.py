"""Tests for _validate_namespace() in archon_search.constants."""

from typing import Final, get_type_hints

import pytest

from archon_search.constants import (
    PING_TIMEOUT_SECONDS,
    PING_TTL_SECONDS,
    _INGEST_CHUNK_BATCH_SIZE,
    _validate_namespace,
)


def test_ingest_chunk_batch_size_constant() -> None:
    """_INGEST_CHUNK_BATCH_SIZE exists, equals 512, and is Final[int]."""
    assert _INGEST_CHUNK_BATCH_SIZE == 512
    assert isinstance(_INGEST_CHUNK_BATCH_SIZE, int)
    # Verify it is declared as Final[int] in the module-level annotations
    import archon_search.constants as _mod
    hints = get_type_hints(_mod)
    assert hints.get("_INGEST_CHUNK_BATCH_SIZE") == Final[int]


def test_ping_timeout_seconds_is_float() -> None:
    assert isinstance(PING_TIMEOUT_SECONDS, float)
    assert PING_TIMEOUT_SECONDS > 0


def test_ping_ttl_seconds_is_float() -> None:
    assert isinstance(PING_TTL_SECONDS, float)
    assert PING_TTL_SECONDS > 0


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

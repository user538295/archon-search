"""Unit tests for archon_search.server._validators."""

import pytest

from archon_search.server._validators import validate_scope_filter


@pytest.mark.parametrize(
    "value,expected_fragment",
    [
        ("*", "bare '*'"),
        ("*user", "only at the end"),
        ("us*er", "only at the end"),
        ("user:*:*", "multiple '*'"),
        ("", "must not be empty"),
    ],
)
def test_validate_scope_filter_rejects(value, expected_fragment):
    msg = validate_scope_filter(value)
    assert msg is not None
    assert expected_fragment in msg


def test_validate_scope_filter_accepts_none():
    assert validate_scope_filter(None) is None


def test_validate_scope_filter_accepts_exact():
    assert validate_scope_filter("user:alice") is None


def test_validate_scope_filter_accepts_trailing_wildcard():
    assert validate_scope_filter("user:*") is None

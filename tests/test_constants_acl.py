"""Tests for reserved namespace name enforcement in archon_search.constants."""

import pytest

from archon_search.constants import _validate_namespace


def test_validate_namespace_rejects_deny_all() -> None:
    with pytest.raises(ValueError, match="Namespace name 'deny-all' is reserved and cannot be used."):
        _validate_namespace("deny-all")


def test_validate_namespace_allows_valid_names() -> None:
    # Regression: ensure normal names still pass after the deny-all guard
    for name in ("default", "tenantA", "tenant-1", "myns", "a", "Z", "deny", "deny-all2"):
        _validate_namespace(name)  # must not raise

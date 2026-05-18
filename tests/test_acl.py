"""Tests for archon_search.acl — ACL parsing utilities."""

import logging

import pytest

from archon_search.acl import is_acl_namespace_valid, parse_acl_value


# ---------------------------------------------------------------------------
# is_acl_namespace_valid
# ---------------------------------------------------------------------------


def test_is_acl_namespace_valid_blocks_deny_all() -> None:
    assert is_acl_namespace_valid("deny-all") is False


def test_is_acl_namespace_valid_blocks_invalid_chars() -> None:
    assert is_acl_namespace_valid("!!!") is False


def test_is_acl_namespace_valid_accepts_valid_name() -> None:
    assert is_acl_namespace_valid("tenantA") is True


def test_is_acl_namespace_valid_accepts_name_with_hyphen_and_underscore() -> None:
    assert is_acl_namespace_valid("my-tenant_1") is True


def test_is_acl_namespace_valid_rejects_empty() -> None:
    assert is_acl_namespace_valid("") is False


# ---------------------------------------------------------------------------
# parse_acl_value — basic types
# ---------------------------------------------------------------------------


def test_parse_acl_value_none() -> None:
    assert parse_acl_value(None, "doc.md") is None


def test_parse_acl_value_int_defaults_open_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value(42, "doc.md")
    assert result is None
    assert "invalid type" in caplog.text.lower() or "int" in caplog.text


def test_parse_acl_value_bool_defaults_open_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value(True, "doc.md")
    assert result is None
    assert any("bool" in r.message or "invalid type" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# parse_acl_value — string inputs
# ---------------------------------------------------------------------------


def test_parse_acl_value_string_comma_separated() -> None:
    assert parse_acl_value("tenantA,tenantB", "doc.md") == ["tenantA", "tenantB"]


def test_parse_acl_value_newline_separated() -> None:
    assert parse_acl_value("tenantA\ntenantB", "doc.md") == ["tenantA", "tenantB"]


def test_parse_acl_value_strips_whitespace() -> None:
    assert parse_acl_value(" tenantA , tenantB ", "doc.md") == ["tenantA", "tenantB"]


def test_parse_acl_value_invalid_names_dropped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value("!!!bad!!!", "doc.md")
    assert result is None


# ---------------------------------------------------------------------------
# parse_acl_value — list inputs
# ---------------------------------------------------------------------------


def test_parse_acl_value_list() -> None:
    assert parse_acl_value(["tenantA", "tenantB"], "doc.md") == ["tenantA", "tenantB"]


def test_parse_acl_value_all_invalid_defaults_open() -> None:
    result = parse_acl_value(["!!!bad1!!!", "!!!bad2!!!"], "doc.md")
    assert result is None


def test_parse_acl_value_empty_list_returns_deny_all() -> None:
    result = parse_acl_value([], "doc.md")
    assert result == []


def test_parse_acl_value_mixed_valid_and_invalid(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value(["tenantA", "!!!bad!!!", "tenantB"], "doc.md")
    assert result == ["tenantA", "tenantB"]
    assert caplog.records  # a warning was emitted


def test_parse_acl_value_list_with_nonstring_elements(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value([42, "tenantA", None], "doc.md")
    assert result == ["tenantA"]
    assert caplog.records  # a warning was emitted


# ---------------------------------------------------------------------------
# parse_acl_value — deny-all special cases
# ---------------------------------------------------------------------------


def test_parse_acl_value_deny_all_name_rejected() -> None:
    """deny-all as sole string entry → empty list (deny-all interpretation)."""
    result = parse_acl_value("deny-all", "doc.md")
    assert result == []


def test_parse_acl_value_deny_all_sole_entry_returns_deny_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value("deny-all", "doc.md")
    assert result == []
    assert any("deny-all" in r.message for r in caplog.records)


def test_parse_acl_value_deny_all_mixed_with_valid_drops_deny_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """deny-all mixed with valid names → valid names only, deny-all dropped with warning."""
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value("deny-all,tenantA", "doc.md")
    assert result == ["tenantA"]
    assert caplog.records


def test_parse_acl_value_deny_all_mixed_with_invalid_fails_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """deny-all + only invalid names → fail-open (None), not deny-all."""
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = parse_acl_value("deny-all,!!!bad!!!", "doc.md")
    assert result is None
    assert caplog.records

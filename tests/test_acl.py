"""Tests for archon_search.acl — ACL parsing utilities."""

import logging

import pytest

from archon_search.acl import (
    apply_acl_filter,
    is_acl_allowed,
    is_acl_namespace_valid,
    parse_acl_value,
    read_acl_sidecar,
    resolve_acl,
)


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


# ---------------------------------------------------------------------------
# read_acl_sidecar
# ---------------------------------------------------------------------------


def test_read_acl_sidecar_namespace_list(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\nns2\n")
    assert read_acl_sidecar(doc) == ["ns1", "ns2"]


def test_read_acl_sidecar_deny_all_sentinel(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("deny-all\n")
    assert read_acl_sidecar(doc) == []


def test_read_acl_sidecar_deny_all_case_insensitive(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("DENY-ALL\n")
    assert read_acl_sidecar(doc) == []


def test_read_acl_sidecar_empty_returns_none(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("   \n\n  \n")
    assert read_acl_sidecar(doc) is None


def test_read_acl_sidecar_absent_returns_none(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    assert read_acl_sidecar(doc) is None


def test_read_acl_sidecar_size_limit(
    tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"ns1\n" * 20000)  # > 64 KB
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = read_acl_sidecar(doc)
    assert result is None
    assert caplog.records


def test_read_acl_sidecar_bom_stripped(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"\xef\xbb\xbfns1\n")  # UTF-8 BOM + ns1
    assert read_acl_sidecar(doc) == ["ns1"]


def test_read_acl_sidecar_invalid_lines_dropped(
    tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\n!!!bad!!!\n")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = read_acl_sidecar(doc)
    assert result == ["ns1"]
    assert caplog.records


def test_read_acl_sidecar_symlink_returns_none(
    tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    real_file = tmp_path / "real.acl"
    real_file.write_text("ns1\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.symlink_to(real_file)
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = read_acl_sidecar(doc)
    assert result is None
    assert caplog.records


def test_read_acl_sidecar_deny_all_with_trailing_lines(
    tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("deny-all\nns1\nns2\n")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = read_acl_sidecar(doc)
    assert result == []
    assert caplog.records


def test_read_acl_sidecar_invalid_utf8_returns_none(
    tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"\xff\xfe invalid bytes")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = read_acl_sidecar(doc)
    assert result is None
    assert caplog.records


# ---------------------------------------------------------------------------
# resolve_acl
# ---------------------------------------------------------------------------


def test_resolve_acl_front_matter_takes_precedence(
    tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns2\n")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = resolve_acl(doc, "ns1")
    assert result == ["ns1"]
    assert caplog.records  # warning about both existing


def test_resolve_acl_sidecar_used_when_no_front_matter(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns2\n")
    result = resolve_acl(doc, None)
    assert result == ["ns2"]


def test_resolve_acl_explicit_null_front_matter_falls_through_to_sidecar(
    tmp_path: pytest.TempPathFactory,
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns3\n")
    result = resolve_acl(doc, None)
    assert result == ["ns3"]


# ---------------------------------------------------------------------------
# is_acl_allowed
# ---------------------------------------------------------------------------


def test_is_acl_allowed_null_open() -> None:
    assert is_acl_allowed(None, "ns1") is True


def test_is_acl_allowed_deny_all() -> None:
    assert is_acl_allowed([], "ns1") is False


def test_is_acl_allowed_match() -> None:
    assert is_acl_allowed(["ns1", "ns2"], "ns1") is True


def test_is_acl_allowed_no_match() -> None:
    assert is_acl_allowed(["ns2"], "ns1") is False


def test_is_acl_allowed_case_sensitive() -> None:
    assert is_acl_allowed(["TenantA"], "tenanta") is False


def test_is_acl_allowed_empty_namespace_denies_protected() -> None:
    assert is_acl_allowed(["ns1"], "") is False


def test_is_acl_allowed_none_namespace() -> None:
    assert is_acl_allowed(["ns1"], "") is False


# ---------------------------------------------------------------------------
# apply_acl_filter
# ---------------------------------------------------------------------------


def test_apply_acl_filter_removes_denied() -> None:
    items = [
        {"acl": ["ns1"], "v": "a"},
        {"acl": ["ns2"], "v": "b"},
        {"acl": ["ns1", "ns2"], "v": "c"},
    ]
    result, dropped = apply_acl_filter(items, lambda x: x["acl"], "ns1")
    assert [i["v"] for i in result] == ["a", "c"]
    assert dropped is True


def test_apply_acl_filter_all_open() -> None:
    items = [{"acl": None, "v": "a"}, {"acl": None, "v": "b"}]
    result, dropped = apply_acl_filter(items, lambda x: x["acl"], "ns1")
    assert len(result) == 2
    assert dropped is False


def test_apply_acl_filter_deny_all() -> None:
    items = [{"acl": [], "v": "a"}, {"acl": [], "v": "b"}]
    result, dropped = apply_acl_filter(items, lambda x: x["acl"], "ns1")
    assert result == []
    assert dropped is True


def test_apply_acl_filter_empty_list() -> None:
    result, dropped = apply_acl_filter([], lambda x: None, "ns1")
    assert result == []
    assert dropped is False


def test_apply_acl_filter_all_denied() -> None:
    items = [{"acl": ["ns2"], "v": "a"}, {"acl": ["ns3"], "v": "b"}]
    result, dropped = apply_acl_filter(items, lambda x: x["acl"], "ns1")
    assert result == []
    assert dropped is True

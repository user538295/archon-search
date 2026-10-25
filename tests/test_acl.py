"""Tests for archon_search.acl — ACL parsing utilities."""

import logging
from pathlib import Path

import pytest

from archon_search.acl import (
    ChunkVisibility,
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
    acl, _warnings = parse_acl_value(None, "doc.md")
    assert acl is None


def test_parse_acl_value_int_fails_closed_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, warnings = parse_acl_value(42, "doc.md")
    assert result == []
    assert "invalid type" in caplog.text.lower() or "int" in caplog.text
    assert "chunk denied" in warnings[0]


def test_parse_acl_value_bool_fails_closed_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, warnings = parse_acl_value(True, "doc.md")
    assert result == []
    assert any("bool" in r.message or "invalid type" in r.message.lower() for r in caplog.records)
    assert "chunk denied" in warnings[0]


# ---------------------------------------------------------------------------
# parse_acl_value — string inputs
# ---------------------------------------------------------------------------


def test_parse_acl_value_string_comma_separated() -> None:
    acl, _ = parse_acl_value("tenantA,tenantB", "doc.md")
    assert acl == ["tenantA", "tenantB"]


def test_parse_acl_value_newline_separated() -> None:
    acl, _ = parse_acl_value("tenantA\ntenantB", "doc.md")
    assert acl == ["tenantA", "tenantB"]


def test_parse_acl_value_strips_whitespace() -> None:
    acl, _ = parse_acl_value(" tenantA , tenantB ", "doc.md")
    assert acl == ["tenantA", "tenantB"]


def test_parse_acl_value_all_invalid_string_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, warnings = parse_acl_value("!!!bad!!!", "doc.md")
    assert result == []
    assert "chunk denied" in warnings[0]


@pytest.mark.parametrize("raw", [{"team": "a"}, 1.5, object()])
def test_parse_acl_value_other_invalid_types_fail_closed(raw: object) -> None:
    result, warnings = parse_acl_value(raw, "doc.md")
    assert result == []
    assert any("chunk denied" in w for w in warnings)


@pytest.mark.parametrize("raw", ["", "   ", ",", "\n"])
def test_parse_acl_value_empty_string_stays_open(raw: str) -> None:
    """A blank scalar declares no restriction (mirrors an empty sidecar)."""
    result, warnings = parse_acl_value(raw, "doc.md")
    assert result is None
    assert warnings == []


# ---------------------------------------------------------------------------
# parse_acl_value — list inputs
# ---------------------------------------------------------------------------


def test_parse_acl_value_list() -> None:
    acl, _ = parse_acl_value(["tenantA", "tenantB"], "doc.md")
    assert acl == ["tenantA", "tenantB"]


def test_parse_acl_value_all_invalid_list_fails_closed() -> None:
    result, warnings = parse_acl_value(["!!!bad1!!!", "!!!bad2!!!"], "doc.md")
    assert result == []
    assert any("chunk denied" in w for w in warnings)


@pytest.mark.parametrize("raw", [[42], [None, 42], [{"a": 1}, True]])
def test_parse_acl_value_only_non_string_elements_fails_closed(raw: list[object]) -> None:
    result, warnings = parse_acl_value(raw, "doc.md")
    assert result == []
    assert any("chunk denied" in w for w in warnings)


def test_parse_acl_value_empty_list_returns_deny_all() -> None:
    result, _ = parse_acl_value([], "doc.md")
    assert result == []


def test_parse_acl_value_mixed_valid_and_invalid(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, _warnings = parse_acl_value(["tenantA", "!!!bad!!!", "tenantB"], "doc.md")
    assert result == ["tenantA", "tenantB"]
    assert caplog.records  # a warning was emitted


def test_parse_acl_value_list_with_nonstring_elements(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, _warnings = parse_acl_value([42, "tenantA", None], "doc.md")
    assert result == ["tenantA"]
    assert caplog.records  # a warning was emitted


# ---------------------------------------------------------------------------
# parse_acl_value — deny-all special cases
# ---------------------------------------------------------------------------


def test_parse_acl_value_deny_all_name_rejected() -> None:
    """deny-all as sole string entry → empty list (deny-all interpretation)."""
    result, _ = parse_acl_value("deny-all", "doc.md")
    assert result == []


def test_parse_acl_value_deny_all_sole_entry_returns_deny_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, _ = parse_acl_value("deny-all", "doc.md")
    assert result == []
    assert any("deny-all" in r.message for r in caplog.records)


def test_parse_acl_value_deny_all_mixed_with_valid_drops_deny_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """deny-all mixed with valid names → valid names only, deny-all dropped with warning."""
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, _ = parse_acl_value("deny-all,tenantA", "doc.md")
    assert result == ["tenantA"]
    assert caplog.records


def test_parse_acl_value_deny_all_mixed_with_invalid_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """deny-all + only invalid names → deny-all ([]), never open."""
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result, warnings = parse_acl_value("deny-all,!!!bad!!!", "doc.md")
    assert result == []
    assert caplog.records
    assert any("chunk denied" in w for w in warnings)


# ---------------------------------------------------------------------------
# read_acl_sidecar
# ---------------------------------------------------------------------------


def test_read_acl_sidecar_namespace_list(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\nns2\n")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == ["ns1", "ns2"]
    assert warnings == []


def test_read_acl_sidecar_deny_all_sentinel(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("deny-all\n")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert warnings == []


def test_read_acl_sidecar_deny_all_case_insensitive(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("DENY-ALL\n")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert warnings == []


def test_read_acl_sidecar_empty_returns_none(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("   \n\n  \n")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries is None
    assert warnings == []


def test_read_acl_sidecar_absent_returns_none(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries is None
    assert warnings == []


def test_read_acl_sidecar_size_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"ns1\n" * 20000)  # > 64 KB
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert len(warnings) == 1
    assert caplog.records


def test_read_acl_sidecar_bom_stripped(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"\xef\xbb\xbfns1\n")  # UTF-8 BOM + ns1
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == ["ns1"]
    assert warnings == []


def test_read_acl_sidecar_invalid_lines_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\n!!!bad!!!\n")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        acl_entries, _source, _sidecar_path, _warnings = read_acl_sidecar(doc)
    assert acl_entries == ["ns1"]
    assert caplog.records


def test_read_acl_sidecar_symlink_fails_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    real_file = tmp_path / "real.acl"
    real_file.write_text("ns1\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.symlink_to(real_file)
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        acl_entries, _source, _sidecar_path, _warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert caplog.records


def test_read_acl_sidecar_deny_all_with_trailing_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("deny-all\nns1\nns2\n")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        acl_entries, _source, _sidecar_path, _warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert caplog.records


def test_read_acl_sidecar_invalid_utf8_fails_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"\xff\xfe invalid bytes")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        acl_entries, _source, _sidecar_path, _warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert caplog.records


def test_read_acl_sidecar_utf16_notepad_file_fails_closed(tmp_path: Path) -> None:
    """A Windows-Notepad UTF-16 sidecar must not silently open the document."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes("team-a\n".encode("utf-16"))
    acl_entries, source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert source == "sidecar"
    assert "chunk denied" in warnings[0]


def test_read_acl_sidecar_unreadable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError while reading the sidecar denies the document instead of crashing or opening it."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("team-a\n")
    original_read_bytes = Path.read_bytes

    def _read_bytes(self: Path) -> bytes:
        if self == sidecar:
            raise PermissionError(13, "Permission denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    acl_entries, source, sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert source == "sidecar"
    assert sidecar_path == sidecar
    assert "unreadable" in warnings[0].lower()
    assert "chunk denied" in warnings[0]


def test_read_acl_sidecar_dangling_symlink_fails_closed(tmp_path: Path) -> None:
    """A dangling symlink does not 'exist' yet still declares a sidecar: deny, never open."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    (tmp_path / "doc.md.acl").symlink_to(tmp_path / "missing.acl")
    acl_entries, source, _path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert source == "sidecar"
    assert "symlink" in warnings[0].lower()


def test_read_acl_sidecar_directory_fails_closed(tmp_path: Path) -> None:
    """A directory named <doc>.acl is a declared but unreadable sidecar: deny."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    (tmp_path / "doc.md.acl").mkdir()
    acl_entries, source, _path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert source == "sidecar"
    assert "unreadable" in warnings[0].lower()


def test_read_acl_sidecar_partially_valid_keeps_valid_names(tmp_path: Path) -> None:
    """Narrowing, not widening: one valid line keeps its restriction (invalid lines dropped)."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    (tmp_path / "doc.md.acl").write_text("team-a\n!!!bad!!!\n")
    acl_entries, _source, _path, warnings = read_acl_sidecar(doc)
    assert acl_entries == ["team-a"]
    assert len(warnings) == 1


def test_read_acl_sidecar_symlink_target_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The symlink check precedes any read: the sidecar is denied without being opened."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    real_file = tmp_path / "real.acl"
    real_file.write_text("team-a\n")
    (tmp_path / "doc.md.acl").symlink_to(real_file)

    def _never_read(self: Path) -> bytes:
        raise AssertionError(f"symlink sidecar {self} must not be read")

    monkeypatch.setattr(Path, "read_bytes", _never_read)
    acl_entries, _source, _path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert "symlink" in warnings[0].lower()
    assert "chunk denied" in warnings[0]


# ---------------------------------------------------------------------------
# resolve_acl
# ---------------------------------------------------------------------------


def test_resolve_acl_front_matter_takes_precedence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns2\n")
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = resolve_acl(doc, "ns1")
    assert result.acl == ["ns1"]
    assert caplog.records  # warning about both existing


def test_resolve_acl_sidecar_used_when_no_front_matter(tmp_path: pytest.TempPathFactory) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns2\n")
    result = resolve_acl(doc, None)
    assert result.acl == ["ns2"]
    assert result.warnings == []


def test_resolve_acl_explicit_null_front_matter_falls_through_to_sidecar(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns3\n")
    result = resolve_acl(doc, None)
    assert result.acl == ["ns3"]
    assert result.warnings == []


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


# ---------------------------------------------------------------------------
# BE-6: read_acl_sidecar returns (acl, warnings) tuple
# ---------------------------------------------------------------------------


def test_read_acl_sidecar_oversized_returns_warning(
    tmp_path: Path,
) -> None:
    """Sidecar > 64 KB must return ([], source, sidecar_path, [warning_str]) naming the file and limit."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"ns1\n" * 20000)  # > 64 KB
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert len(warnings) == 1
    assert "64" in warnings[0]  # mentions the limit
    assert str(sidecar) in warnings[0] or sidecar.name in warnings[0]  # names the file


def test_read_acl_sidecar_normal_returns_no_warning(
    tmp_path: Path,
) -> None:
    """Sidecar ≤ 64 KB must return (acl_list, source, sidecar_path, []) with empty warnings."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\nns2\n")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == ["ns1", "ns2"]
    assert warnings == []


def test_read_acl_sidecar_absent_returns_no_warning(
    tmp_path: Path,
) -> None:
    """No sidecar must return (None, None, None, []) with empty warnings."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    acl_entries, _source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries is None
    assert warnings == []


def test_resolve_acl_sidecar_path_returns_result(
    tmp_path: Path,
) -> None:
    """resolve_acl() via sidecar path returns AclResolutionResult with empty warnings."""
    from archon_search.acl import AclResolutionResult

    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\n")
    result = resolve_acl(doc, None)
    assert isinstance(result, AclResolutionResult)
    assert result.acl == ["ns1"]
    assert result.warnings == []


def test_resolve_acl_frontmatter_path_returns_result(
    tmp_path: Path,
) -> None:
    """resolve_acl() via front-matter path returns AclResolutionResult."""
    from archon_search.acl import AclResolutionResult

    doc = tmp_path / "doc.md"
    doc.write_text("")
    result = resolve_acl(doc, "ns1")
    assert isinstance(result, AclResolutionResult)
    assert result.acl == ["ns1"]
    assert result.warnings == []


# ---------------------------------------------------------------------------
# BE-6: IngestResult.warnings default empty list
# ---------------------------------------------------------------------------


def test_resolve_acl_oversized_sidecar_propagates_warning(
    tmp_path: Path,
) -> None:
    """resolve_acl() must propagate the oversized-sidecar warning and deny the document."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"ns1\n" * 20000)  # > 64 KB
    result = resolve_acl(doc, None)
    assert result.acl == []
    assert len(result.warnings) == 1
    assert "64" in result.warnings[0]


# ---------------------------------------------------------------------------
# BE-6: parse_acl_value warning branches
# ---------------------------------------------------------------------------


def test_parse_acl_bool_returns_warning() -> None:
    """parse_acl_value with bool returns ([], [non-empty warning]) (S4a/S4e)."""
    acl, warnings = parse_acl_value(True, "doc.md")
    assert acl == []
    assert len(warnings) >= 1
    assert any(warnings), "warnings list must contain non-empty strings"


def test_parse_acl_other_type_returns_warning() -> None:
    """parse_acl_value with an int returns ([], [non-empty warning]) (S4a)."""
    acl, warnings = parse_acl_value(42, "doc.md")
    assert acl == []
    assert len(warnings) >= 1
    assert any(warnings), "warnings list must contain non-empty strings"


def test_parse_acl_non_string_elements_returns_warning() -> None:
    """list with non-string elements returns a warning."""
    acl, warnings = parse_acl_value([42, None, "ns1"], "doc.md")
    # Valid string elements are kept; non-string ones trigger a warning
    assert acl == ["ns1"]
    assert len(warnings) >= 1
    assert any(warnings)


def test_parse_acl_invalid_namespace_names_returns_warning() -> None:
    """list with invalid namespace names returns a warning."""
    acl, warnings = parse_acl_value(["!!!bad!!!", "ns1"], "doc.md")
    # valid ns1 is kept; invalid one triggers a warning
    assert acl == ["ns1"]
    assert len(warnings) >= 1
    assert any(warnings)


def test_parse_acl_deny_all_mixed_invalid_returns_warning() -> None:
    """deny-all plus only invalid entries → ([], [non-empty warning]) (S4d)."""
    acl, warnings = parse_acl_value("deny-all,!!!bad!!!", "doc.md")
    assert acl == []
    assert len(warnings) >= 1
    assert any(warnings)


# ---------------------------------------------------------------------------
# BE-6: read_acl_sidecar warning branches (symlink, UTF-8, invalid namespace)
# ---------------------------------------------------------------------------


def test_read_sidecar_symlink_returns_warning(
    tmp_path: Path,
) -> None:
    """symlink sidecar yields ([], source, sidecar_path, [non-empty warning]) (S4b)."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    real_file = tmp_path / "real.acl"
    real_file.write_text("ns1\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.symlink_to(real_file)
    acl_entries, source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert source == "sidecar"
    assert len(warnings) >= 1
    assert any(warnings), "warnings must be non-empty for symlink sidecar"
    assert "symlink" in warnings[0].lower()


def test_read_sidecar_utf8_failure_returns_warning(
    tmp_path: Path,
) -> None:
    """non-UTF-8 sidecar bytes yield ([], source, sidecar_path, [non-empty warning]) (S4c)."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"\xff\xfe invalid bytes")
    acl_entries, source, _sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl_entries == []
    assert source == "sidecar"
    assert len(warnings) >= 1
    assert any(warnings), "warnings must be non-empty for UTF-8 decode failure"
    assert "utf" in warnings[0].lower()


def test_read_sidecar_invalid_namespace_returns_warning(
    tmp_path: Path,
) -> None:
    """sidecar with invalid namespace lines yields non-empty warnings."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("!!!bad!!!\n")
    acl_entries, source, _sidecar_path, warnings = read_acl_sidecar(doc)
    # All entries invalid → fail-closed ([])
    assert acl_entries == []
    assert source == "sidecar"
    assert len(warnings) >= 1
    assert any(warnings), "warnings must be non-empty for invalid namespace in sidecar"
    assert "invalid" in warnings[0].lower() or "namespace" in warnings[0].lower()


# ---------------------------------------------------------------------------
# BE-6: IngestResult.warnings default empty list
# ---------------------------------------------------------------------------


def test_ingest_result_warnings_default_empty() -> None:
    """IngestResult.warnings must default to empty list."""
    from archon_search._types import IngestResult

    result = IngestResult(doc_id="abc123", chunks_created=1, status="ok")
    assert result.warnings == []


# ---------------------------------------------------------------------------
# ChunkVisibility — the per-collection hidden chunk / document sets
# ---------------------------------------------------------------------------


def _rows() -> list[tuple[str, str, list[str] | None]]:
    return [
        ("open", "open-0", None),
        ("team-a-only", "team-a-only-0", ["team-a"]),
        ("team-b-only", "team-b-only-0", ["team-b"]),
        ("denied", "denied-0", []),
        ("mixed", "mixed-0", ["team-a"]),
        ("mixed", "mixed-1", None),
    ]


def test_chunk_visibility_hides_chunks_the_namespace_may_not_read() -> None:
    visibility = ChunkVisibility.from_chunk_acls(_rows(), "team-b")
    assert visibility.hidden_chunk_ids == {"team-a-only-0", "denied-0", "mixed-0"}
    assert visibility.chunk_visible("open-0")
    assert visibility.chunk_visible("team-b-only-0")
    assert not visibility.chunk_visible("mixed-0")
    assert visibility.chunk_visible("unknown-chunk"), "a chunk without a stored ACL (deleted, stale) hides nothing"


def test_chunk_visibility_hides_only_documents_with_no_visible_chunk() -> None:
    visibility = ChunkVisibility.from_chunk_acls(_rows(), "team-b")
    assert visibility.hidden_doc_ids == {"team-a-only", "denied"}
    assert visibility.doc_visible("mixed"), "one visible chunk keeps the document visible"
    assert visibility.doc_visible("unknown-doc")


def test_chunk_visibility_matches_search_rule_for_every_namespace() -> None:
    for namespace in ("team-a", "team-b", "team-c", ""):
        visibility = ChunkVisibility.from_chunk_acls(_rows(), namespace)
        for _doc, chunk_id, acl in _rows():
            assert visibility.chunk_visible(chunk_id) == is_acl_allowed(acl, namespace)


def test_chunk_visibility_default_hides_nothing() -> None:
    visibility = ChunkVisibility()
    assert visibility.chunk_visible("any") and visibility.doc_visible("any")

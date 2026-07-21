"""BE-2: AclResolutionResult dataclass and refactored acl.py functions.

Tests for:
- AclResolutionResult dataclass fields
- resolve_acl() returns AclResolutionResult with source/sidecar_path/warnings
- read_acl_sidecar() returns 4-tuple (acl, source, sidecar_path, warnings)
- parse_acl_value() returns (acl, warnings) 2-tuple
- shadowing warning (S4f) and sidecar-too-large warning (S4)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.acl import (
    AclResolutionResult,
    parse_acl_value,
    read_acl_sidecar,
    resolve_acl,
)


# ---------------------------------------------------------------------------
# AclResolutionResult dataclass
# ---------------------------------------------------------------------------


def test_aclresult_dataclass_fields() -> None:
    """AclResolutionResult has acl, source, sidecar_path, warnings fields."""
    result = AclResolutionResult(
        acl=["ns1"],
        source="frontmatter",
        sidecar_path=None,
        warnings=[],
    )
    assert result.acl == ["ns1"]
    assert result.source == "frontmatter"
    assert result.sidecar_path is None
    assert result.warnings == []


def test_aclresult_warnings_default_empty() -> None:
    """AclResolutionResult.warnings defaults to empty list (field(default_factory=list))."""
    result = AclResolutionResult(acl=None, source=None, sidecar_path=None)
    assert result.warnings == []


# ---------------------------------------------------------------------------
# resolve_acl returns AclResolutionResult
# ---------------------------------------------------------------------------


def test_resolve_acl_frontmatter_returns_source(tmp_path: Path) -> None:
    """Front-matter ACL yields source='frontmatter', sidecar_path=None."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    result = resolve_acl(doc, "ns1")
    assert isinstance(result, AclResolutionResult)
    assert result.acl == ["ns1"]
    assert result.source == "frontmatter"
    assert result.sidecar_path is None
    assert result.warnings == []


def test_resolve_acl_sidecar_returns_source_and_path(tmp_path: Path) -> None:
    """Sidecar ACL yields source='sidecar', sidecar_path set to the absolute sidecar path."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\n")
    result = resolve_acl(doc, None)
    assert isinstance(result, AclResolutionResult)
    assert result.acl == ["ns1"]
    assert result.source == "sidecar"
    assert result.sidecar_path == sidecar
    assert result.warnings == []


def test_resolve_acl_no_rule_returns_none_source(tmp_path: Path) -> None:
    """No front-matter and no sidecar yields source=None, sidecar_path=None, warnings=[]."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    result = resolve_acl(doc, None)
    assert isinstance(result, AclResolutionResult)
    assert result.acl is None
    assert result.source is None
    assert result.sidecar_path is None
    assert result.warnings == []


def test_resolve_acl_shadowing_warning(tmp_path: Path) -> None:
    """Both front-matter and sidecar present → source='frontmatter', sidecar_path=None, warnings non-empty (S4f)."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns2\n")
    result = resolve_acl(doc, "ns1")
    assert isinstance(result, AclResolutionResult)
    assert result.source == "frontmatter"
    assert result.sidecar_path is None
    assert len(result.warnings) > 0


def test_sidecar_too_large_warning_surfaced(tmp_path: Path) -> None:
    """Sidecar exceeding 64 KB yields non-empty warnings propagated into AclResolutionResult (S4)."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"ns1\n" * 20000)  # > 64 KB
    result = resolve_acl(doc, None)
    assert isinstance(result, AclResolutionResult)
    assert result.acl is None
    assert result.source == "sidecar"
    assert len(result.warnings) > 0
    assert "64" in result.warnings[0]


# ---------------------------------------------------------------------------
# parse_acl_value returns (acl, warnings) tuple
# ---------------------------------------------------------------------------


def test_parse_acl_value_returns_tuple() -> None:
    """parse_acl_value() returns (list | None, list[str]) in all branches."""
    # Valid single name
    acl, warnings = parse_acl_value("ns1", "doc.md")
    assert acl == ["ns1"]
    assert isinstance(warnings, list)

    # None input
    acl, warnings = parse_acl_value(None, "doc.md")
    assert acl is None
    assert isinstance(warnings, list)

    # Invalid type (bool) — fail-open with warning
    acl, warnings = parse_acl_value(True, "doc.md")
    assert acl is None
    assert isinstance(warnings, list)
    assert len(warnings) > 0

    # Invalid names — fail-open with warning
    acl, warnings = parse_acl_value("!!!bad!!!", "doc.md")
    assert acl is None
    assert isinstance(warnings, list)

    # Empty list → deny-all
    acl, warnings = parse_acl_value([], "doc.md")
    assert acl == []
    assert isinstance(warnings, list)


def test_parse_acl_value_bool_returns_warning_in_tuple() -> None:
    """parse_acl_value(True, ...) returns (None, [warning_str])."""
    acl, warnings = parse_acl_value(True, "doc.md")
    assert acl is None
    assert len(warnings) == 1
    assert "bool" in warnings[0].lower() or "invalid type" in warnings[0].lower()


def test_parse_acl_value_int_returns_warning_in_tuple() -> None:
    """parse_acl_value(42, ...) returns (None, [warning_str])."""
    acl, warnings = parse_acl_value(42, "doc.md")
    assert acl is None
    assert len(warnings) == 1


def test_parse_acl_value_invalid_names_returns_warning_in_tuple() -> None:
    """parse_acl_value with all-invalid names returns (None, [warning_str])."""
    acl, warnings = parse_acl_value("!!!bad!!!", "doc.md")
    assert acl is None
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# read_acl_sidecar returns 4-tuple
# ---------------------------------------------------------------------------


def test_read_acl_sidecar_returns_four_tuple_with_sidecar(tmp_path: Path) -> None:
    """read_acl_sidecar with a valid sidecar returns (acl, 'sidecar', sidecar_path, warnings)."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\n")
    acl, source, sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl == ["ns1"]
    assert source == "sidecar"
    assert sidecar_path == sidecar
    assert warnings == []


def test_read_acl_sidecar_returns_four_tuple_absent(tmp_path: Path) -> None:
    """read_acl_sidecar with no sidecar returns (None, None, None, [])."""
    doc = tmp_path / "doc.md"
    doc.write_text("")
    acl, source, sidecar_path, warnings = read_acl_sidecar(doc)
    assert acl is None
    assert source is None
    assert sidecar_path is None
    assert warnings == []

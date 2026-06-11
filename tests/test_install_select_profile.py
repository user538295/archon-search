"""Tests for _select_profile() in archon_search.install (Task C0-3.3)."""
from __future__ import annotations

import sys
from unittest.mock import patch

import click
import pytest

from archon_search.install import _select_profile


# ---------------------------------------------------------------------------
# Non-interactive defaults
# ---------------------------------------------------------------------------

def test_non_interactive_no_flag_defaults_minimal_english():
    result = _select_profile(profile_flag=None, multilingual_flag=False, non_interactive=True)
    assert result == ("minimal", False)


def test_non_interactive_with_multilingual_flag_returns_minimal_multilingual():
    result = _select_profile(profile_flag=None, multilingual_flag=True, non_interactive=True)
    assert result == ("minimal", True)


# ---------------------------------------------------------------------------
# Explicit profile flag
# ---------------------------------------------------------------------------

def test_explicit_profile_flag_returned_as_is():
    result = _select_profile(profile_flag="max", multilingual_flag=False, non_interactive=False)
    assert result == ("max", False)


def test_explicit_profile_flag_with_multilingual_true():
    result = _select_profile(profile_flag="balanced", multilingual_flag=True, non_interactive=False)
    assert result == ("balanced", True)


def test_explicit_invalid_profile_raises():
    with pytest.raises(click.BadParameter):
        _select_profile(profile_flag="ultra", multilingual_flag=False, non_interactive=False)


# ---------------------------------------------------------------------------
# Interactive path
# ---------------------------------------------------------------------------

def test_interactive_choice_1_returns_minimal():
    with (
        patch("archon_search.install._render_profile_table", return_value=""),
        patch("builtins.input", return_value="1"),
    ):
        result = _select_profile(profile_flag=None, multilingual_flag=False, non_interactive=False)
    assert result == ("minimal", False)


def test_interactive_empty_defaults_to_minimal():
    with (
        patch("archon_search.install._render_profile_table", return_value=""),
        patch("builtins.input", return_value=""),
    ):
        result = _select_profile(profile_flag=None, multilingual_flag=False, non_interactive=False)
    assert result == ("minimal", False)


def test_interactive_invalid_then_valid_retries():
    with (
        patch("archon_search.install._render_profile_table", return_value=""),
        patch("builtins.input", side_effect=["x", "2"]),
    ):
        result = _select_profile(profile_flag=None, multilingual_flag=False, non_interactive=False)
    assert result == ("balanced", False)


def test_interactive_three_invalid_inputs_exits(capsys):
    with (
        patch("archon_search.install._render_profile_table", return_value=""),
        patch("builtins.input", side_effect=["x", "y", "z"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        _select_profile(profile_flag=None, multilingual_flag=False, non_interactive=False)
    assert exc_info.value.code == 1
    assert "Too many invalid attempts" in capsys.readouterr().out


def test_interactive_eof_on_input_exits(capsys):
    with (
        patch("archon_search.install._render_profile_table", return_value=""),
        patch("builtins.input", side_effect=EOFError),
        pytest.raises(SystemExit) as exc_info,
    ):
        _select_profile(profile_flag=None, multilingual_flag=False, non_interactive=False)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "No input received (EOF). Aborting." in captured.out


def test_interactive_choice_returns_multilingual_flag_as_given():
    with (
        patch("archon_search.install._render_profile_table", return_value=""),
        patch("builtins.input", return_value="2"),
    ):
        result = _select_profile(profile_flag=None, multilingual_flag=True, non_interactive=False)
    assert result == ("balanced", True)


# ---------------------------------------------------------------------------
# Task 5.1 — "Recommended" annotation on balanced profile row
# ---------------------------------------------------------------------------

def test_profile_table_balanced_recommended_annotation():
    from archon_search.install import _render_profile_table
    table = _render_profile_table(multilingual=False, width=80)
    # "Recommended" must appear somewhere in the balanced row area
    assert "Recommended" in table
    # More specifically, the balanced row (containing "Balanced") should come
    # before the "Recommended" annotation in the same row (both on the same line)
    lines = table.splitlines()
    balanced_lines = [l for l in lines if "Balanced" in l]
    assert len(balanced_lines) >= 1
    assert any("Recommended" in l for l in balanced_lines)


def test_profile_table_minimal_no_recommended():
    from archon_search.install import _render_profile_table
    table = _render_profile_table(multilingual=False, width=80)
    lines = table.splitlines()
    minimal_lines = [l for l in lines if "Minimal" in l and "Recommended" not in l]
    # Minimal lines exist and do NOT contain "Recommended"
    assert any("Minimal" in l for l in lines)
    assert not any("Minimal" in l and "Recommended" in l for l in lines)


def test_profile_table_narrow_has_footnote():
    from archon_search.install import _render_profile_table
    table = _render_profile_table(multilingual=False, width=60)
    # In narrow mode, the balanced row must have a "*" marker on the row itself
    lines = table.splitlines()
    balanced_lines = [l for l in lines if "Balanced" in l]
    assert len(balanced_lines) >= 1
    # The balanced row line must contain the "*" marker directly
    assert any("*" in l for l in balanced_lines), (
        "Expected '*' marker on balanced row in narrow mode"
    )
    # And there must be a footnote explaining the marker
    assert any("Recommended" in l for l in lines), (
        "Expected footnote with 'Recommended' in narrow mode output"
    )

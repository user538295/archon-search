"""Tests for _render_profile_table and _render_summary in archon_search/install.py."""
from __future__ import annotations

import pytest

from archon_search.install import _render_profile_table, _render_summary
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES, InstallProfile, get_profile


# ---------------------------------------------------------------------------
# _render_profile_table tests
# ---------------------------------------------------------------------------


def test_render_table_wide_contains_all_profiles():
    """Wide mode (width=80), English: output contains all three profile names."""
    output = _render_profile_table(multilingual=False, width=80)
    assert "Minimal" in output
    assert "Balanced" in output
    assert "Max" in output


def test_render_table_narrow_uses_list_format():
    """Narrow mode (width=60): no table separator, but list markers present."""
    output = _render_profile_table(multilingual=False, width=60)
    assert "─────" not in output  # "─────"
    assert "1)" in output
    assert "2)" in output
    assert "3)" in output


def test_render_table_multilingual_shows_multilingual_note():
    """multilingual=True: footer says '(Showing multilingual models)'."""
    output = _render_profile_table(multilingual=True, width=80)
    assert "(Showing multilingual models)" in output


def test_render_table_english_shows_add_multilingual_hint():
    """multilingual=False: footer prompts user to use --multilingual."""
    output = _render_profile_table(multilingual=False, width=80)
    assert "Add --multilingual" in output


def test_render_table_multilingual_minimal_shows_no_reranker():
    """Wide multilingual mode: minimal profile shows 'no reranker'."""
    output = _render_profile_table(multilingual=True, width=80)
    assert "no reranker" in output


# ---------------------------------------------------------------------------
# _render_summary tests
# ---------------------------------------------------------------------------


def test_render_summary_balanced_english():
    """Balanced English profile summary contains key model name, chunk size, and profile label."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary("balanced", profile, multilingual=False, providers=[])
    assert "BAAI/bge-base-en-v1.5" in output
    assert "512" in output
    assert "Balanced" in output


def test_render_summary_shows_providers():
    """Providers list is rendered in the summary."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary("minimal", profile, multilingual=False, providers=["CoreMLExecutionProvider"])
    assert "CoreML" in output


def test_render_summary_multilingual_label():
    """multilingual=True: summary contains 'Multilingual'."""
    profile = get_profile("balanced", multilingual=True)
    output = _render_summary("balanced", profile, multilingual=True, providers=[])
    assert "Multilingual" in output


def test_render_summary_no_reranker():
    """Profile with reranker=None: summary contains '(none)'."""
    profile = get_profile("minimal", multilingual=True)
    assert profile.reranker is None
    output = _render_summary("minimal", profile, multilingual=True, providers=[])
    assert "(none)" in output


def test_render_summary_no_providers():
    """Empty providers list: summary contains 'CPU default'."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary("minimal", profile, multilingual=False, providers=[])
    assert "CPU default" in output


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------

def test_render_table_wide_formats_large_download_as_gb():
    """Max English profile has download_mb >= 1000: output shows GB not MB."""
    max_profile = ENGLISH_PROFILES["max"]
    assert max_profile.download_mb >= 1000, "Precondition: max profile must be >=1000 MB"
    output = _render_profile_table(multilingual=False, width=80)
    assert "GB" in output


def test_render_table_narrow_formats_large_download_as_gb():
    """Max English profile in narrow mode also shows GB."""
    max_profile = ENGLISH_PROFILES["max"]
    assert max_profile.download_mb >= 1000
    output = _render_profile_table(multilingual=False, width=60)
    assert "GB" in output


def test_render_table_boundary_at_width_80_is_wide():
    """width=80 exactly should produce the full table (header separator present)."""
    output = _render_profile_table(multilingual=False, width=80)
    assert "─────" in output


def test_render_table_boundary_at_width_79_is_narrow():
    """width=79 should produce compact list (no header separator)."""
    output = _render_profile_table(multilingual=False, width=79)
    assert "─────" not in output
    assert "1)" in output


def test_render_table_narrow_multilingual_minimal_no_reranker_no_plus():
    """Narrow multilingual minimal: '+ no reranker' should NOT appear (no misleading '+')."""
    output = _render_profile_table(multilingual=True, width=60)
    assert "+ no reranker" not in output
    assert "no reranker" in output

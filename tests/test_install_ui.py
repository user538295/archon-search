"""Tests for _render_profile_table and _render_summary in archon_search/install.py."""
from __future__ import annotations

import pytest

from archon_search.install import _render_profile_table, _render_summary, WizardFeatures
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


# ---------------------------------------------------------------------------
# _render_summary optional features tests (Task C8-2.4)
# ---------------------------------------------------------------------------


def test_render_summary_no_features():
    """features=None produces no 'Optional features' section (backward-compatible)."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary("minimal", profile, multilingual=False, providers=[], features=None)
    assert "Optional features" not in output


def test_render_summary_all_defaults():
    """WizardFeatures() (all defaults) also produces no optional section."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary("minimal", profile, multilingual=False, providers=[], features=WizardFeatures())
    assert "Optional features" not in output


def test_render_summary_with_code_and_telemetry():
    """WizardFeatures with install_code_extra and enable_telemetry contains both labels."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(install_code_extra=True, enable_telemetry=True),
    )
    assert "Optional features" in output
    assert "Code enrichment" in output
    assert "Telemetry" in output


def test_render_summary_routing_hybrid():
    """WizardFeatures with routing_strategy='hybrid' mentions 'Routing: hybrid'."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(routing_strategy="hybrid"),
    )
    assert "Optional features" in output
    assert "Routing: hybrid" in output


def test_render_summary_disable_reranker():
    """WizardFeatures with disable_reranker=True mentions reranker disabled."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(disable_reranker=True),
    )
    assert "Optional features" in output
    assert "Reranker disabled" in output


def test_render_summary_log_format_json():
    """WizardFeatures with log_format='json' mentions 'Log format: json'."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(log_format="json"),
    )
    assert "Optional features" in output
    assert "Log format: json" in output


def test_render_summary_eager_load_and_watch():
    """WizardFeatures with eager_load_embedders and enable_watch mentions both."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(eager_load_embedders=True, enable_watch=True),
    )
    assert "Optional features" in output
    assert "Eager load" in output
    assert "Watch" in output


def test_render_summary_base_content_preserved_with_features():
    """Base profile content (embedder, chunk size) is still present when features are non-None."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(enable_telemetry=True),
    )
    # Base content still present
    assert "Balanced" in output
    assert profile.embedder in output
    assert str(profile.chunk_size) in output
    # Optional section also present
    assert "Optional features" in output
    assert "Telemetry" in output

"""Tests for _render_profile_table and _render_summary in archon_search/install.py."""
from __future__ import annotations

import pytest

from archon_search.install import _render_profile_table, _render_summary, WizardFeatures, _print_next_steps
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


def test_render_summary_multilingual_extra():
    """WizardFeatures with install_multilingual_extra=True shows language-detection bullet."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(install_multilingual_extra=True),
    )
    assert "Optional features" in output
    assert "Language detection (fasttext)" in output


def test_render_summary_multilingual_header_and_extra_bullet():
    """multilingual=True + install_multilingual_extra=True: header and bullet both render."""
    profile = get_profile("minimal", multilingual=True)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=True,
        providers=[],
        features=WizardFeatures(install_multilingual_extra=True),
    )
    assert "· Multilingual" in output  # header separator+label (install.py:708)
    assert "Language detection (fasttext)" in output  # feature bullet
    assert "Optional features" in output


def test_render_summary_multilingual_extra_absent_by_default():
    """install_multilingual_extra=False: no language-detection bullet even when section renders."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal",
        profile,
        multilingual=False,
        providers=[],
        features=WizardFeatures(enable_telemetry=True),
    )
    assert "Optional features" in output  # section renders due to telemetry
    assert "Language detection" not in output  # multilingual bullet absent


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


# ---------------------------------------------------------------------------
# Task 5.2: Expanded _render_summary with new fields
# ---------------------------------------------------------------------------


def test_summary_contains_db_path():
    """_render_summary with db_path shows the db path in output."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        db_path="/home/user/.archon-search/lancedb",
    )
    assert "lancedb" in output
    assert "Database:" in output


def test_summary_contains_server_url():
    """_render_summary with host/port shows http://host:port in output."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        host="127.0.0.1",
        port=8765,
    )
    assert "http://127.0.0.1:8765" in output
    assert "Server:" in output


def test_summary_contains_download_size():
    """_render_summary with download_mb shows the size in output."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        download_mb=300,
    )
    assert "300 MB" in output
    assert "Download:" in output


def test_summary_api_key_masked(tmp_path):
    """_render_summary with a valid key file shows a masked key."""
    key_file = tmp_path / ".search.env"
    key_file.write_text("ARCHON_SEARCH_API_KEY=abcdefghijklmnopqrst\n")
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        api_key_file=str(key_file),
    )
    assert "abcdefgh" in output
    assert "qrst" in output
    assert "API key:" in output


def test_summary_api_key_file_missing():
    """_render_summary with a non-existent key file shows 'not yet generated'."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary(
        "balanced",
        profile,
        multilingual=False,
        providers=[],
        api_key_file="/nonexistent/path/.search.env",
    )
    assert "not yet generated" in output
    assert "API key:" in output


# ---------------------------------------------------------------------------
# Task 5.3: _print_next_steps
# ---------------------------------------------------------------------------


def test_next_steps_all_commands_present(capsys):
    """_print_next_steps prints all four follow-up commands."""
    _print_next_steps("127.0.0.1", 8765, "/tmp/test.env")
    out = capsys.readouterr().out
    assert "ingest" in out
    assert "status" in out
    assert "sync" in out
    assert "stop" in out


def test_next_steps_shows_correct_host_port(capsys):
    """_print_next_steps uses the supplied host and port."""
    _print_next_steps("0.0.0.0", 9000, "/tmp/test.env")
    out = capsys.readouterr().out
    assert "http://0.0.0.0:9000" in out


def test_next_steps_shows_key_file_path(capsys):
    """_print_next_steps shows the api_key_file path."""
    _print_next_steps("127.0.0.1", 8765, "/tmp/mykey.env")
    out = capsys.readouterr().out
    assert "/tmp/mykey.env" in out


def test_next_steps_key_file_from_key_manager(capsys):
    """_print_next_steps with empty api_key_file falls back to key_manager.get_key_file()."""
    from archon_search import key_manager
    _print_next_steps("127.0.0.1", 8765, "")
    out = capsys.readouterr().out
    assert str(key_manager.get_key_file()) in out


def test_next_steps_shows_next_steps_header(capsys):
    """_print_next_steps output contains 'Next steps:' header."""
    _print_next_steps("127.0.0.1", 8765, "/tmp/test.env")
    out = capsys.readouterr().out
    assert "Next steps:" in out


# ---------------------------------------------------------------------------
# Additional edge case and coverage tests (DA review cycle 1)
# ---------------------------------------------------------------------------


def test_summary_db_path_absent_when_empty():
    """_render_summary with default db_path='' must NOT emit a Database: line."""
    profile = get_profile("balanced", multilingual=False)
    output = _render_summary("balanced", profile, multilingual=False, providers=[])
    assert "Database:" not in output


def test_summary_server_custom_host_port():
    """_render_summary emits the correct Server URL for a non-default host:port."""
    profile = get_profile("minimal", multilingual=False)
    output = _render_summary(
        "minimal", profile, multilingual=False, providers=[],
        host="0.0.0.0", port=9999,
    )
    assert "http://0.0.0.0:9999" in output


def test_mask_api_key_no_matching_line(tmp_path):
    """Key file exists but has no ARCHON_SEARCH_API_KEY= line → '(not yet generated)'."""
    from archon_search.install import _mask_api_key
    key_file = tmp_path / ".search.env"
    key_file.write_text("# no key here\nSOME_OTHER_VAR=value\n")
    assert _mask_api_key(str(key_file)) == "(not yet generated)"


def test_mask_api_key_with_equals_in_value(tmp_path):
    """Key value containing '=' is parsed correctly (split on first '=' only)."""
    from archon_search.install import _mask_api_key
    key_file = tmp_path / ".search.env"
    # Simulate a base64-like key with = padding
    key = "abcdefghijklmnopqrst==extra"
    key_file.write_text(f"ARCHON_SEARCH_API_KEY={key}\n")
    result = _mask_api_key(str(key_file))
    assert result == f"{key[:8]}…{key[-4:]}"


def test_next_steps_not_printed_in_dry_run(tmp_path, capsys):
    """_print_next_steps must NOT be called during a dry-run wizard run."""
    from unittest.mock import patch, MagicMock
    from archon_search.install import SearchInstaller
    from archon_search.platform.types import GpuType

    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    installer = SearchInstaller(config_file=str(config_path), dry_run=True)

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        rc = installer.run(non_interactive=True, profile="balanced", skip_preload=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps:" not in out, "Next steps must NOT appear in dry-run output"

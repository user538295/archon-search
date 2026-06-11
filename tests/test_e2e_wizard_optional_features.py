"""End-to-end tests for wizard CLI optional features (Task C8-3.3).

These tests invoke the real `wizard` Click command via CliRunner, then parse
the written TOML with tomlkit to assert on config values.  All tests run
--non-interactive (except interactive-mode use-cases) with --config pointing to
a tmp_path so no real services or model downloads are triggered.

Run:
    uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v
"""
from __future__ import annotations

import contextlib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import tomlkit
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.install import InstallError, SearchInstaller
from archon_search.platform.types import GpuType

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("install")]

# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _noop_install_lock() -> Generator[None, None, None]:
    """No-op replacement for _acquire_install_lock to avoid lock collisions in parallel tests."""
    yield


@contextmanager
def _patched_wizard(**extra_module_patches: Any) -> Generator[None, None, None]:
    """Patch all infrastructure that touches real filesystem or services.

    Fresh MagicMock instances are created on each invocation to prevent call-count
    state from leaking between tests.
    """
    # Build fresh mocks per invocation (no shared state between tests)
    base_patches: dict[str, Any] = {
        "_prewarm_models": MagicMock(),
        "_check_disk_space": MagicMock(),
        "_legacy_service_path": MagicMock(return_value=Path("/nonexistent")),
        "_remove_legacy_service": MagicMock(),
        "_acquire_install_lock": _noop_install_lock,
    }

    # Merge extra patches (using short names for consistency)
    for key, val in extra_module_patches.items():
        short_key = key.replace("archon_search.install.", "")
        base_patches[short_key] = val

    module_level = base_patches
    # Build fresh mocks for installer methods to avoid state leak between tests
    installer_patches: dict[str, Any] = {
        "detect_gpu": MagicMock(return_value=GpuType.NONE),
        "validate_providers": MagicMock(return_value=False),
        "configure_providers": MagicMock(),
        "write_service_file": MagicMock(),
        "load_service": MagicMock(return_value=0),
        "_wait_for_service": MagicMock(return_value=True),
        "_is_service_running": MagicMock(return_value=False),
    }

    with patch.multiple("archon_search.install", **module_level):
        with patch.multiple(SearchInstaller, **installer_patches):
            yield


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Use case 1: All feature flags non-interactive
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_all_feature_flags(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive with all feature flags writes expected config values."""
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "balanced",
            "--config", str(config_path),
            "--skip-preload",
            "--code",
            "--watch",
            "--telemetry",
            "--log-format", "json",
            "--routing-strategy", "hybrid",
            "--no-reranker",
            "--eager-load",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert config_path.exists(), "Config file not written"
    doc = tomlkit.parse(config_path.read_text())

    assert doc["collections"]["watch"] is True, "watch should be true"
    assert doc["telemetry"]["enabled"] is True, "telemetry.enabled should be true"
    assert doc["logging"]["format"] == "json", "logging.format should be json"
    assert doc["routing"]["routing_strategy"] == "hybrid", "routing_strategy should be hybrid"
    assert doc["database"]["reranker_model"] == "", "reranker_model should be empty"
    assert doc["database"]["eager_load_embedders"] is True, "eager_load_embedders should be true"


# ---------------------------------------------------------------------------
# Use case 2: GPU decline flag
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_disable_gpu(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --disable-gpu writes database.providers = []."""
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--disable-gpu",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())
    providers = list(doc["database"]["providers"])
    assert providers == [], f"Expected empty providers list, got {providers}"


# ---------------------------------------------------------------------------
# Use case 3: Defaults produce minimal (clean) config
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_defaults_produce_clean_config(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive with no feature flags does not write optional keys."""
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())

    # Optional wizard-added keys must not be written beyond profile defaults:
    # - telemetry section and routing_strategy are not in the default profile
    assert "enabled" not in doc.get("telemetry", {}), "telemetry.enabled should not be present"
    assert "routing_strategy" not in doc.get("routing", {}), "routing_strategy should not be present"
    # watch is in the profile template but should default to false
    assert doc.get("collections", {}).get("watch") is False, "watch should default to false"
    # logging.format is in the profile template at 'text'
    assert doc.get("logging", {}).get("format") == "text", "logging.format should default to text"


# ---------------------------------------------------------------------------
# Use case 4: Interactive — user enables telemetry and watch
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_interactive_watch_and_telemetry(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive mode: user answers y to watch and telemetry, n to others."""
    config_path = tmp_path / "archon-search.toml"

    # Input queue (order matches prompt order in _prompt_multilingual + _prompt_optional_features
    # + confirmation prompt):
    #  1. multilingual: "n"
    #  2. code enrichment: "n"
    #  3. disable reranker: "n"   (profile=balanced has reranker)
    #  4. watch: "y"
    #  5. telemetry: "y"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default=centroid)
    #  8. log format: "" (default=text)
    #  9. "Proceed?": "y"
    stdin_responses = "\n".join(["n", "n", "n", "y", "y", "n", "", "", "y"]) + "\n"

    with _patched_wizard():
        result = runner.invoke(
            main,
            [
                "wizard",
                "--profile", "balanced",
                "--config", str(config_path),
                "--skip-preload",
            ],
            input=stdin_responses,
        )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["collections"]["watch"] is True
    assert doc["telemetry"]["enabled"] is True


# ---------------------------------------------------------------------------
# Use case 5: Interactive — user enables multilingual
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_interactive_multilingual_yes(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive mode: user answers y to multilingual question.

    Verifies that _select_profile is called with multilingual=True.
    """
    config_path = tmp_path / "archon-search.toml"

    # We spy on _select_profile to verify it receives multilingual=True.
    # _select_profile is called after _prompt_multilingual; its return value
    # determines profile_name and multilingual for the rest of run().
    select_profile_spy = MagicMock(return_value=("minimal", True))

    # Input queue:
    #  1. multilingual: "y"
    #  All optional features: defaults (n / "" for choices)
    #  confirmation: "y"
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "y"]) + "\n"

    with _patched_wizard(
        **{"archon_search.install._select_profile": select_profile_spy,
           "archon_search.install._prompt_fasttext_license": MagicMock(),
           "archon_search.install._download_fasttext_model": MagicMock()}
    ):
        result = runner.invoke(
            main,
            [
                "wizard",
                "--profile", "minimal",
                "--config", str(config_path),
                "--skip-preload",
            ],
            input=stdin_responses,
        )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    # _select_profile should have been called with multilingual=True
    select_profile_spy.assert_called_once()
    args = select_profile_spy.call_args.args
    # Signature: _select_profile(profile_flag, multilingual_flag, non_interactive)
    assert args[1] is True, f"multilingual_flag should be True, got {args[1]}"


# ---------------------------------------------------------------------------
# Use case 6: Interactive — invalid routing then valid
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_interactive_invalid_routing_retries(runner: CliRunner, tmp_path: Path) -> None:
    """First invalid routing input then 'hybrid' → config has routing_strategy='hybrid'."""
    config_path = tmp_path / "archon-search.toml"

    # Input queue (minimal profile HAS a reranker, so reranker question IS shown):
    #  1. multilingual: "n"
    #  2. code: "n"
    #  3. disable reranker: "n"  (minimal has reranker → question shown)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing (bad): "badval"  ← triggers retry
    #  8. routing (retry, valid): "hybrid"
    #  9. log format: ""
    # 10. "Proceed?": "y"
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "badval", "hybrid", "", "y"]) + "\n"

    with _patched_wizard():
        result = runner.invoke(
            main,
            [
                "wizard",
                "--profile", "minimal",
                "--config", str(config_path),
                "--skip-preload",
            ],
            input=stdin_responses,
        )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["routing"]["routing_strategy"] == "hybrid"


# ---------------------------------------------------------------------------
# Use case 7: Code extra install triggered
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_code_extra_install_triggered(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --code → _install_code_extra called exactly once."""
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock()

    with _patched_wizard(**{"archon_search.install._install_code_extra": install_code_mock}):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--code",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    install_code_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Use case 8: Code extra install failure is non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_code_extra_install_failure_non_fatal(runner: CliRunner, tmp_path: Path) -> None:
    """_install_code_extra raising InstallError → wizard exits 0, config is intact."""
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock(side_effect=InstallError("pip failed"))

    with _patched_wizard(**{"archon_search.install._install_code_extra": install_code_mock}):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--code",
        ])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
    assert config_path.exists(), "Config file should still exist after non-fatal failure"


# ---------------------------------------------------------------------------
# Use case 9: Re-run wizard adds features to existing config
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_rerun_adds_features_to_existing_config(runner: CliRunner, tmp_path: Path) -> None:
    """First run writes minimal config; second run with --watch adds watch, preserves other keys."""
    config_path = tmp_path / "archon-search.toml"

    # First run: no features
    with _patched_wizard():
        result1 = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
        ])
    assert result1.exit_code == 0, f"First run failed: {result1.output}"
    assert config_path.exists()
    first_content = config_path.read_text()
    first_doc = tomlkit.parse(first_content)
    # Grab a key present in both runs to verify preservation
    original_embedder = first_doc["database"]["embedding_model"]

    # Second run: add --watch
    with _patched_wizard():
        result2 = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--watch",
        ])
    assert result2.exit_code == 0, f"Second run failed: {result2.output}"
    doc2 = tomlkit.parse(config_path.read_text())
    assert doc2["collections"]["watch"] is True, "watch should be true after second run"
    assert doc2["database"]["embedding_model"] == original_embedder, "embedding_model should be preserved"


# ---------------------------------------------------------------------------
# Task 5.5 — e2e: overwrite warning on re-run with hand-edited config
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_rerun_with_hand_edited_config(runner: CliRunner, tmp_path: Path) -> None:
    """Full e2e: install, hand-edit config (non-destructive), re-run wizard → wizard completes.

    Uses a non-destructive hand-edit (telemetry.enabled) that does NOT trigger the
    reinstall guard (which only fires on model/chunk_size mismatches). Both runs use
    --no-multilingual and --profile minimal to avoid model mismatch on re-run.
    """
    config_path = tmp_path / "archon-search.toml"

    # Step 1: initial install (non-interactive, minimal profile, English)
    with _patched_wizard():
        result1 = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--no-multilingual",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
        ])
    assert result1.exit_code == 0, f"First run failed: {result1.output}"
    assert config_path.exists()

    # Step 2: hand-edit the config (add telemetry.enabled — non-destructive, no reinstall guard)
    doc = tomlkit.parse(config_path.read_text())
    if "telemetry" not in doc:
        doc.add("telemetry", tomlkit.table())
    doc["telemetry"]["enabled"] = True
    config_path.write_text(tomlkit.dumps(doc))

    # Verify the hand-edit is actually detected by _detect_config_hand_edits
    from archon_search.install import _detect_config_hand_edits
    assert _detect_config_hand_edits(config_path, "minimal", False) is True, (
        "Hand-edit detection should return True after adding telemetry.enabled=True"
    )

    # Step 3: re-run wizard non-interactively (non-interactive auto-accepts overwrite)
    with _patched_wizard():
        result2 = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--no-multilingual",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
        ])

    assert result2.exit_code == 0, f"Second run failed: {result2.output}"
    # After the wizard re-run, the profile-specific database keys should be at profile defaults.
    # Note: _write_profile_config overlays profile keys on top of existing config; it does NOT
    # clear hand-edited optional sections (that's by design). The overwrite warning is about
    # overwriting wizard-profile keys, not clearing all custom sections.
    final_doc = tomlkit.parse(config_path.read_text())
    from archon_search.profiles import get_profile
    expected_embedder = get_profile("minimal", False).embedder
    assert final_doc["database"]["embedding_model"] == expected_embedder, (
        f"embedding_model should be minimal profile default {expected_embedder}, "
        f"got {final_doc['database']['embedding_model']}"
    )
    # The overwrite warning message should appear in the second run's output
    assert "[warn] Existing config has custom values" in result2.output, (
        f"Expected overwrite warning in output. Got: {result2.output}"
    )

"""End-to-end tests for wizard CLI optional features (Task C8-3.3, C15-2.1).

These tests invoke the real `wizard` Click command via CliRunner, then parse
the written TOML with tomlkit to assert on config values.  All tests run
--non-interactive (except interactive-mode use-cases) with --config pointing to
a tmp_path so no real services or model downloads are triggered.

Run:
    uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v
"""
from __future__ import annotations

import contextlib
import os
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


@contextmanager
def _no_anthropic_key() -> Generator[None, None, None]:
    """Clear ANTHROPIC_API_KEY from the env for the duration of the test.

    Used by tests that need a predictable environment without a real Anthropic
    key.  The interactive HyDE/RAG Fusion prompt always fires in interactive
    mode (no API key gate); tests must include a response for it in their
    input sequences.
    """
    env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict("os.environ", env_without_key, clear=True):
        yield

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
        # C3-I-1: mock the real subprocess-shelling install functions by
        # default so every test using this fixture is hermetic, not just the
        # ones that individually patched these in cycles 1-2. Per-test
        # overrides passed via extra_module_patches below still take
        # precedence (merged after base_patches).
        "_install_code_extra": MagicMock(),
        "_install_graph_extra": MagicMock(),
        "_install_multilingual_extra": MagicMock(),
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
    #  9. HyDE/RAG Fusion: "n"
    # 10. "Proceed?": "y"
    stdin_responses = "\n".join(["n", "n", "n", "y", "y", "n", "", "", "n", "y"]) + "\n"

    with _no_anthropic_key():
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
    #  9. HyDE/RAG Fusion: "n"
    #  confirmation: "y"
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "n", "y"]) + "\n"

    with _no_anthropic_key():
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
    # 10. HyDE/RAG Fusion: "n"
    # 11. "Proceed?": "y"
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "badval", "hybrid", "", "n", "y"]) + "\n"

    with _no_anthropic_key():
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
    # --code auto-triggers install_graph_extra too (BE-11 bundling) — mock it as
    # well so this test never shells out to a real `pip install archon-search[graph]`.
    install_graph_mock = MagicMock()

    with _patched_wizard(**{
        "archon_search.install._install_code_extra": install_code_mock,
        "archon_search.install._install_graph_extra": install_graph_mock,
    }):
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


@pytest.mark.integration
def test_wizard_autoInstallsCodeAndGraphBundles(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --code auto-installs BOTH [code] and [graph] bundles.

    BE-11: opting into code indexing must not leave a guided user in the
    degraded-startup path (S9) — both extras install together automatically,
    with no separate y/n for graph.
    """
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock()
    install_graph_mock = MagicMock()

    with _patched_wizard(
        **{
            "archon_search.install._install_code_extra": install_code_mock,
            "archon_search.install._install_graph_extra": install_graph_mock,
        }
    ):
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
    install_graph_mock.assert_called_once()

    doc = tomlkit.parse(config_path.read_text())
    assert doc["graph"]["enabled"] is True, (
        "graph.enabled must be true in the written config — otherwise the "
        "auto-installed [graph] extras are inert (C1-I-1)"
    )


@pytest.mark.integration
def test_wizard_declinesCode_doesNotWriteGraphEnabled(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive WITHOUT --code must not write graph.enabled=true.

    Negative case for the C1-I-1 fix: declining code indexing must not
    auto-enable graph, since ``install_graph_extra`` mirrors
    ``install_code_extra`` and defaults to False. ``_default_toml()`` (the
    base template) has no ``[graph]`` section at all, so the written config
    should have no ``[graph]`` section either.
    """
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

    doc = tomlkit.parse(config_path.read_text())
    assert "graph" not in doc, (
        "graph.enabled must not be written when --code was declined — "
        "install_graph_extra mirrors install_code_extra and defaults to False"
    )


# ---------------------------------------------------------------------------
# Use case 8: Code extra install failure is non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_code_extra_install_failure_non_fatal(runner: CliRunner, tmp_path: Path) -> None:
    """_install_code_extra raising InstallError → wizard exits 0, config is intact.

    Both sibling install functions are mocked: ``--code`` triggers BOTH
    ``install_code_extra`` and ``install_graph_extra`` (mirrored bundle, BE-11),
    so the non-failing sibling must also be mocked or it would run a real
    subprocess install (C2-M-2).
    """
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock(side_effect=InstallError("pip failed"))
    install_graph_mock = MagicMock()

    with _patched_wizard(
        **{
            "archon_search.install._install_code_extra": install_code_mock,
            "archon_search.install._install_graph_extra": install_graph_mock,
        }
    ):
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
    install_graph_mock.assert_called_once()


@pytest.mark.integration
def test_e2e_graph_extra_install_failure_non_fatal(runner: CliRunner, tmp_path: Path) -> None:
    """_install_graph_extra raising InstallError → wizard exits 0, config is intact (C1-T-3).

    Both sibling install functions are mocked — see docstring on
    ``test_e2e_code_extra_install_failure_non_fatal`` above (C2-M-2). Also
    asserts the config-rollback behavior added for Fix A: a failed
    ``[graph]`` install must revert ``graph.enabled`` to ``False`` so the next
    server start doesn't hard-fail on the missing spaCy dependency.
    """
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock()
    install_graph_mock = MagicMock(side_effect=InstallError("pip failed"))

    with _patched_wizard(
        **{
            "archon_search.install._install_code_extra": install_code_mock,
            "archon_search.install._install_graph_extra": install_graph_mock,
        }
    ):
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
    install_code_mock.assert_called_once()

    doc = tomlkit.parse(config_path.read_text())
    assert doc["graph"]["enabled"] is False, (
        "graph.enabled must be reverted to false when [graph] extras failed to "
        "install — otherwise the next server start hard-fails on missing spaCy"
    )


@pytest.mark.integration
def test_wizard_diskSpaceFailure_revertsGraphEnabled(runner: CliRunner, tmp_path: Path) -> None:
    """Disk-space check failure after config write must revert graph.enabled (C3-A-1).

    ``run()`` writes ``graph.enabled=true`` early (config-write branches, Step
    6/7/8) whenever ``--code`` is passed, but the disk-space check (Step 11)
    happens later and can raise ``InstallError``. Before this fix, that early
    return skipped the rollback entirely, leaving ``graph.enabled=true`` on
    disk with ``[graph]`` extras never installed — a hard ``ConfigError`` at
    the next server start.
    """
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock()
    install_graph_mock = MagicMock()
    disk_space_mock = MagicMock(side_effect=InstallError("not enough disk space"))

    with _patched_wizard(
        **{
            "archon_search.install._install_code_extra": install_code_mock,
            "archon_search.install._install_graph_extra": install_graph_mock,
            "archon_search.install._check_disk_space": disk_space_mock,
        }
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--code",
        ])

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}: {result.output}"
    # The install calls are unreachable from this early-return path — they
    # must never fire.
    install_code_mock.assert_not_called()
    install_graph_mock.assert_not_called()

    assert config_path.exists(), "Config should have been written before the disk-space check"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["graph"]["enabled"] is False, (
        "graph.enabled must be reverted to false when the disk-space check "
        "fails after the config write — otherwise the next server start "
        "hard-fails on missing spaCy"
    )


@pytest.mark.integration
def test_wizard_declineProceedPrompt_revertsGraphEnabled(runner: CliRunner, tmp_path: Path) -> None:
    """Declining the final 'Proceed?' prompt must revert graph.enabled (C3-A-1).

    Interactive-mode equivalent of the disk-space-failure test above: the
    config is written with ``graph.enabled=true`` early (config-write
    branches), but if the user declines the confirmation prompt (Step 13,
    which only exists in interactive mode), the function returned early
    without rolling back the flag — an ordinary user action (declining a
    prompt), not an edge case.
    """
    config_path = tmp_path / "archon-search.toml"
    install_code_mock = MagicMock()
    install_graph_mock = MagicMock()

    # Input queue (minimal profile HAS a reranker, so reranker question IS shown):
    #  1. multilingual: "n"
    #  2. code enrichment: "y"   (triggers install_graph_extra bundling too)
    #  3. disable reranker: "n"
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default)
    #  8. log format: "" (default)
    #  9. HyDE/RAG Fusion: "n"
    # 10. "Proceed?": "n"  ← decline
    stdin_responses = "\n".join(["n", "y", "n", "n", "n", "n", "", "", "n", "n"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{
                "archon_search.install._install_code_extra": install_code_mock,
                "archon_search.install._install_graph_extra": install_graph_mock,
            }
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

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}:\nOUT: {result.output}"
    # The install calls are unreachable from this early-return path — they
    # must never fire.
    install_code_mock.assert_not_called()
    install_graph_mock.assert_not_called()

    assert config_path.exists(), "Config should have been written before the Proceed? prompt"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["graph"]["enabled"] is False, (
        "graph.enabled must be reverted to false when the user declines the "
        "Proceed? prompt after the config write — otherwise the next server "
        "start hard-fails on missing spaCy"
    )


# ---------------------------------------------------------------------------
# Use case 8b: Multilingual extra install (2026-07-15-040)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_multilingual_extra_install_triggered(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --multilingual → _install_multilingual_extra called once."""
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock()

    with _patched_wizard(
        **{"archon_search.install._install_multilingual_extra": install_multilingual_mock}
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--multilingual",
            "--config", str(config_path),
            "--skip-preload",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    install_multilingual_mock.assert_called_once()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["multilingual"] is True


@pytest.mark.integration
def test_e2e_english_profile_does_not_install_multilingual(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --no-multilingual → _install_multilingual_extra NOT called."""
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock()

    with _patched_wizard(
        **{"archon_search.install._install_multilingual_extra": install_multilingual_mock}
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--no-multilingual",
            "--config", str(config_path),
            "--skip-preload",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    install_multilingual_mock.assert_not_called()


@pytest.mark.integration
def test_e2e_multilingual_install_failure_reverts_flag(runner: CliRunner, tmp_path: Path) -> None:
    """_install_multilingual_extra raising InstallError → exit 0, config reverted to multilingual=false.

    Otherwise the next server start hard-fails in _check_multilingual_deps when
    fasttext-wheel is absent. Reverting lets the server start English-only.
    """
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock(side_effect=InstallError("pip failed"))

    with _patched_wizard(
        **{"archon_search.install._install_multilingual_extra": install_multilingual_mock}
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--multilingual",
            "--config", str(config_path),
            "--skip-preload",
        ])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
    install_multilingual_mock.assert_called_once()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["multilingual"] is False, (
        "multilingual must be reverted to false when the [multilingual] extra fails "
        "to install — otherwise the next server start hard-fails on missing fasttext-wheel"
    )


@pytest.mark.integration
def test_e2e_multilingual_disk_space_failure_reverts_flag(runner: CliRunner, tmp_path: Path) -> None:
    """Disk-space check failure after config write must revert multilingual (mirrors C3-A-1).

    The config is written with multilingual=true early (Step 6/7/8), but the
    disk-space check (Step 11) happens later and can raise before the install
    step. That early return must still roll back the flag.
    """
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock()
    disk_space_mock = MagicMock(side_effect=InstallError("not enough disk space"))

    with _patched_wizard(
        **{
            "archon_search.install._install_multilingual_extra": install_multilingual_mock,
            "archon_search.install._check_disk_space": disk_space_mock,
        }
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--multilingual",
            "--config", str(config_path),
            "--skip-preload",
        ])

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}: {result.output}"
    install_multilingual_mock.assert_not_called()
    assert config_path.exists(), "Config should have been written before the disk-space check"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["multilingual"] is False, (
        "multilingual must be reverted to false when the disk-space check fails "
        "after the config write"
    )


@pytest.mark.integration
def test_e2e_multilingual_declineProceedPrompt_reverts_flag(runner: CliRunner, tmp_path: Path) -> None:
    """Declining the final 'Proceed?' prompt must revert multilingual (mirrors C3-A-1).

    Interactive-mode abort path for multilingual: the user answers "y" to the
    multilingual prompt (so the config is written with multilingual=true
    early), but then declines the final Proceed? confirmation. That early
    return must still roll back the flag so the next server start does not
    hard-fail on missing fasttext-wheel. Mirrors
    test_wizard_declineProceedPrompt_revertsGraphEnabled.
    """
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock()

    # Input queue (multilingual minimal profile has reranker=None, so the
    # reranker question is SKIPPED):
    #  1. multilingual: "y"
    #  2. code enrichment: "n"
    #  3. watch: "n"
    #  4. telemetry: "n"
    #  5. eager load: "n"
    #  6. routing strategy: "" (default)
    #  7. log format: "" (default)
    #  8. HyDE/RAG Fusion: "n"
    #  9. "Proceed?": "n"  ← decline
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "n", "n"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{"archon_search.install._install_multilingual_extra": install_multilingual_mock}
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

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}:\nOUT: {result.output}"
    install_multilingual_mock.assert_not_called()
    assert config_path.exists(), "Config should have been written before the Proceed? prompt"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["multilingual"] is False, (
        "multilingual must be reverted to false when the user declines the "
        "Proceed? prompt after the config write — otherwise the next server "
        "start hard-fails on missing fasttext-wheel"
    )


@pytest.mark.integration
def test_e2e_interactive_multilingual_install_triggered(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive mode: user answers "y" to multilingual and Proceed → install fires.

    The existing test_e2e_interactive_multilingual_yes only spies on
    _select_profile and proves nothing about the install actually firing.
    This proves the interactive stdin-y -> install-fires wiring end-to-end,
    and that multilingual=true is written to the config.
    """
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock()

    # Same interactive setup as the decline test above, but Proceed="y".
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "n", "y"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{"archon_search.install._install_multilingual_extra": install_multilingual_mock}
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
    install_multilingual_mock.assert_called_once()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["multilingual"] is True


@pytest.mark.integration
def test_e2e_multilingual_install_before_prewarm(runner: CliRunner, tmp_path: Path) -> None:
    """_install_multilingual_extra must run before _prewarm_models (Step 14 ordering).

    Deliberately omits --skip-preload (unlike other tests in this module) —
    with it, _prewarm_models is never called at all (install.py:2244), so the
    ordering this test asserts could not be observed.  _prewarm_models is
    mocked, so no real model download occurs.
    """
    config_path = tmp_path / "archon-search.toml"
    call_order: list[str] = []
    install_mock = MagicMock(side_effect=lambda *a, **k: call_order.append("install"))
    prewarm_mock = MagicMock(side_effect=lambda *a, **k: call_order.append("prewarm"))

    with _patched_wizard(
        **{
            "archon_search.install._install_multilingual_extra": install_mock,
            "archon_search.install._prewarm_models": prewarm_mock,
            "archon_search.install._download_fasttext_model": MagicMock(),
        }
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--multilingual",
            "--accept-fasttext-license",
            "--config", str(config_path),
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert call_order == ["install", "prewarm"], (
        f"Expected install before prewarm, got call order: {call_order}"
    )


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


# ---------------------------------------------------------------------------
# Task 2.1 — C15 Tier 1 Click options
# ---------------------------------------------------------------------------


def _wizard_args(config_path: Path, *extra: str) -> list[str]:
    """Build a non-interactive wizard invocation with skip-preload."""
    return [
        "wizard",
        "--non-interactive",
        "--profile", "minimal",
        "--config", str(config_path),
        "--skip-preload",
        *extra,
    ]


@pytest.mark.integration
def test_wizard_host_writes_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--host 0.0.0.0 writes [server].host = '0.0.0.0' to TOML."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--host", "0.0.0.0"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["server"]["host"] == "0.0.0.0"


@pytest.mark.integration
def test_wizard_host_non_loopback_prints_security_note(runner: CliRunner, tmp_path: Path) -> None:
    """--host 0.0.0.0 prints security note in stdout."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--host", "0.0.0.0"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "firewall" in result.output or "exposes" in result.output, (
        f"Security note not in output: {result.output}"
    )


@pytest.mark.integration
def test_wizard_host_lan_ip_prints_security_note(runner: CliRunner, tmp_path: Path) -> None:
    """--host 192.168.1.100 prints security note in stdout."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--host", "192.168.1.100"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "firewall" in result.output or "exposes" in result.output, (
        f"Security note not in output for LAN IP: {result.output}"
    )


@pytest.mark.integration
def test_wizard_host_loopback_no_security_note(runner: CliRunner, tmp_path: Path) -> None:
    """--host 127.0.0.1 does NOT print security note."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--host", "127.0.0.1"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "exposes the service" not in result.output


@pytest.mark.integration
def test_wizard_port_writes_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--port 9000 writes [server].port = 9000 to TOML."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--port", "9000"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["server"]["port"] == 9000


@pytest.mark.integration
def test_wizard_port_invalid_rejects(runner: CliRunner, tmp_path: Path) -> None:
    """--port 0 is rejected (below valid range)."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--port", "0"))
    assert result.exit_code != 0, f"Expected non-zero exit for --port 0, got: {result.output}"


@pytest.mark.integration
def test_wizard_port_65536_rejects(runner: CliRunner, tmp_path: Path) -> None:
    """--port 65536 is rejected (above valid range)."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--port", "65536"))
    assert result.exit_code != 0, f"Expected non-zero exit for --port 65536, got: {result.output}"


@pytest.mark.integration
def test_wizard_db_path_writes_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--db-path ~/custom writes [database].db_path = '~/custom' (tilde preserved)."""
    config_path = tmp_path / "archon-search.toml"
    db_path_dir = tmp_path / "custom_db"
    db_path_dir.mkdir()
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--db-path", str(db_path_dir)))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert "db_path" in doc["database"]
    assert doc["database"]["db_path"] == str(db_path_dir)


@pytest.mark.integration
def test_wizard_log_level_writes_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--log-level DEBUG writes [logging].level = 'DEBUG'."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--log-level", "DEBUG"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["logging"]["level"] == "DEBUG"


@pytest.mark.integration
def test_wizard_log_level_invalid_rejects(runner: CliRunner, tmp_path: Path) -> None:
    """--log-level VERBOSE is rejected."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--log-level", "VERBOSE"))
    assert result.exit_code != 0, f"Expected non-zero exit for invalid log level"


@pytest.mark.integration
def test_wizard_log_to_stderr_writes_empty_log_file(runner: CliRunner, tmp_path: Path) -> None:
    """--log-to-stderr writes [logging].log_file = ''."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--log-to-stderr"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["logging"]["log_file"] == ""


@pytest.mark.integration
def test_wizard_top_k_writes_both_keys(runner: CliRunner, tmp_path: Path) -> None:
    """--top-k 20 writes top_k_return=20 and top_k_retrieve=60."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--top-k", "20"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["top_k_return"] == 20
    assert doc["database"]["top_k_retrieve"] == 60


@pytest.mark.integration
def test_wizard_top_k_1_sets_retrieve_to_15(runner: CliRunner, tmp_path: Path) -> None:
    """--top-k 1 sets top_k_retrieve=15 (max guard)."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--top-k", "1"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["top_k_return"] == 1
    assert doc["database"]["top_k_retrieve"] == 15


@pytest.mark.integration
def test_wizard_top_k_0_rejects(runner: CliRunner, tmp_path: Path) -> None:
    """--top-k 0 is rejected."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--top-k", "0"))
    assert result.exit_code != 0, f"Expected non-zero exit for --top-k 0"


@pytest.mark.integration
def test_wizard_top_k_101_rejects(runner: CliRunner, tmp_path: Path) -> None:
    """--top-k 101 is rejected with performance message."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--top-k", "101"))
    assert result.exit_code != 0, f"Expected non-zero exit for --top-k 101"
    assert "performance" in result.output.lower() or "performance" in (result.stderr or "").lower(), (
        f"Expected performance message, got: {result.output}"
    )


@pytest.mark.integration
def test_wizard_telemetry_retention_without_telemetry_warns(runner: CliRunner, tmp_path: Path) -> None:
    """--telemetry-retention-days 7 without --telemetry prints warning and does NOT write retention_days."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--telemetry-retention-days", "7"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    # Warning should be in output (stdout or stderr)
    combined = result.output + (result.stderr or "")
    assert "telemetry" in combined.lower() and (
        "no effect" in combined.lower() or "warning" in combined.lower() or "not enabled" in combined.lower()
    ), f"Warning not found in output: {combined}"
    # retention_days should NOT be written
    doc = tomlkit.parse(config_path.read_text())
    assert "retention_days" not in doc.get("telemetry", {}), "retention_days should not be in TOML"


@pytest.mark.integration
def test_wizard_telemetry_retention_with_telemetry_writes_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--telemetry --telemetry-retention-days 7 writes [telemetry].retention_days = 7."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(
            main, _wizard_args(config_path, "--telemetry", "--telemetry-retention-days", "7")
        )
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["telemetry"]["retention_days"] == 7


@pytest.mark.integration
def test_wizard_host_empty_string_rejects(runner: CliRunner, tmp_path: Path) -> None:
    """--host '' is rejected."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--host", ""))
    assert result.exit_code != 0, f"Expected non-zero exit for empty --host"


@pytest.mark.integration
def test_wizard_not_passed_flags_do_not_write_toml(runner: CliRunner, tmp_path: Path) -> None:
    """Running without --host does not override [server].host beyond the profile template default."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    # The profile template always writes [server].host = "127.0.0.1".
    # Without --host, it must remain at the default; no user value is injected.
    assert doc.get("server", {}).get("host") == "127.0.0.1", (
        f"Expected default host '127.0.0.1', got: {doc.get('server', {}).get('host')}"
    )


@pytest.mark.integration
def test_wizard_explicit_default_value_writes_to_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--port 8765 (same as default) IS written to TOML (idempotency behavior)."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path, "--port", "8765"))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["server"]["port"] == 8765


@pytest.mark.integration
def test_wizard_log_format_json_prompts_log_to_stderr(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive mode: log-format json triggers 'Log to stderr only?' follow-up prompt; y → log_file=''."""
    config_path = tmp_path / "archon-search.toml"
    # Input order: multilingual, code, reranker, watch, telemetry, eager, routing, log-format=json, stderr=y, HyDE/RAG Fusion=n, proceed
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "", "json", "y", "n", "y"]) + "\n"
    with _no_anthropic_key():
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
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["logging"]["log_file"] == "", f"log_file should be '' when stderr prompt answered y"


@pytest.mark.integration
def test_wizard_log_format_text_does_not_prompt_log_to_stderr(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive mode: log-format text does NOT show stderr follow-up prompt."""
    config_path = tmp_path / "archon-search.toml"
    # Input order: multilingual, code, reranker, watch, telemetry, eager, routing, log-format=text, HyDE/RAG Fusion=n, proceed
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "", "text", "n", "y"]) + "\n"
    with _no_anthropic_key():
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
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "stderr" not in result.output.lower() or "Log to stderr" not in result.output, (
        f"Stderr prompt should not appear for text log format. Output: {result.output}"
    )


@pytest.mark.integration
def test_wizard_non_interactive_json_does_not_prompt_log_to_stderr(runner: CliRunner, tmp_path: Path) -> None:
    """--log-format json --non-interactive: no stderr prompt, log_file NOT set to '' (flag not passed)."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(
            main, _wizard_args(config_path, "--log-format", "json")
        )
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    # The profile template includes a default log_file path; what must NOT happen is
    # log_file being set to "" (which only occurs when --log-to-stderr is enabled).
    log_file_val = doc.get("logging", {}).get("log_file")
    assert log_file_val != "", (
        "log_file should not be '' in non-interactive mode without --log-to-stderr; "
        f"got: {log_file_val!r}"
    )


@pytest.mark.integration
def test_wizard_log_to_stderr_flag_bypasses_conditional_prompt(runner: CliRunner, tmp_path: Path) -> None:
    """--log-format json --log-to-stderr --non-interactive: log_file='' written."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(
            main, _wizard_args(config_path, "--log-format", "json", "--log-to-stderr")
        )
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["logging"]["log_file"] == ""


@pytest.mark.integration
def test_wizard_success_output_contains_top_k_hint(runner: CliRunner, tmp_path: Path) -> None:
    """Success output next-steps block contains '--top-k' hint."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        result = runner.invoke(main, _wizard_args(config_path))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "--top-k" in result.output, f"--top-k hint not in output: {result.output}"


# ---------------------------------------------------------------------------
# Task 4.2 — C15 HyDE/RAG Fusion interactive prompt in _prompt_optional_features
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_wizard_enable_hyde_works_without_anthropic_key(runner: CliRunner, tmp_path: Path) -> None:
    """--enable-hyde without ANTHROPIC_API_KEY succeeds (BE-8 removed the API key gate)."""
    config_path = tmp_path / "archon-search.toml"
    env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with _patched_wizard():
        with patch.dict("os.environ", env_without_key, clear=True):
            result = runner.invoke(main, _wizard_args(config_path, "--enable-hyde"))
    assert result.exit_code == 0, f"Expected exit 0 (no API key gate), got: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc.get("hyde", {}).get("enabled") is True, (
        f"Expected [hyde] enabled = true in TOML: {dict(doc.get('hyde', {}))}"
    )


@pytest.mark.integration
def test_wizard_enable_rag_fusion_works_without_anthropic_key(runner: CliRunner, tmp_path: Path) -> None:
    """--enable-rag-fusion without ANTHROPIC_API_KEY succeeds (BE-8 removed the API key gate)."""
    config_path = tmp_path / "archon-search.toml"
    env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with _patched_wizard():
        with patch.dict("os.environ", env_without_key, clear=True):
            result = runner.invoke(main, _wizard_args(config_path, "--enable-rag-fusion"))
    assert result.exit_code == 0, f"Expected exit 0 (no API key gate), got: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc.get("rag_fusion", {}).get("enabled") is True, (
        f"Expected [rag_fusion] enabled = true in TOML: {dict(doc.get('rag_fusion', {}))}"
    )


@pytest.mark.integration
def test_wizard_non_interactive_skips_hyde_prompt_even_with_key(runner: CliRunner, tmp_path: Path) -> None:
    """--non-interactive with ANTHROPIC_API_KEY set: neither [hyde] nor [rag_fusion] in TOML."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"}):
            result = runner.invoke(main, _wizard_args(config_path))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert "enabled" not in doc.get("hyde", {}), "hyde.enabled should not be written in non-interactive mode"
    assert "enabled" not in doc.get("rag_fusion", {}), "rag_fusion.enabled should not be written"


@pytest.mark.integration
def test_wizard_enable_hyde_and_rag_fusion_writes_toml(runner: CliRunner, tmp_path: Path) -> None:
    """--enable-hyde --enable-rag-fusion with ANTHROPIC_API_KEY writes both to TOML."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"}):
            result = runner.invoke(
                main,
                _wizard_args(config_path, "--enable-hyde", "--enable-rag-fusion"),
            )
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True, "hyde.enabled should be True"
    assert doc["rag_fusion"]["enabled"] is True, "rag_fusion.enabled should be True"


# ---------------------------------------------------------------------------
# Task 5.2 — --server-key integration tests
# ---------------------------------------------------------------------------

_VALID_SERVER_KEY = "a" * 32  # 32-char lowercase hex (valid)


@pytest.mark.integration
def test_wizard_server_key_writes_key_file(runner: CliRunner, tmp_path: Path) -> None:
    """--server-key writes ARCHON_SEARCH_API_KEY=<key> to the key file with mode 0o600."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"

    with _patched_wizard():
        with _no_anthropic_key():
            with patch.dict(os.environ, {"ARCHON_SEARCH_DATA_DIR": str(tmp_path)}):
                with patch("archon_search.install.os.chmod") as mock_chmod:
                    result = runner.invoke(
                        main,
                        _wizard_args(config_path, "--server-key", _VALID_SERVER_KEY),
                    )

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert key_file.exists(), "key file should be created by --server-key"
    content = key_file.read_text()
    assert f"ARCHON_SEARCH_API_KEY={_VALID_SERVER_KEY}" in content, (
        f"key file content missing expected key: {content!r}"
    )
    mock_chmod.assert_any_call(key_file, 0o600)


@pytest.mark.integration
def test_wizard_server_key_prints_history_warning(runner: CliRunner, tmp_path: Path) -> None:
    """--server-key prints shell history warning to output."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"

    with _patched_wizard():
        with _no_anthropic_key():
            with patch.dict(os.environ, {"ARCHON_SEARCH_DATA_DIR": str(tmp_path)}):
                with patch("archon_search.install.os.chmod"):
                    result = runner.invoke(
                        main,
                        _wizard_args(config_path, "--server-key", _VALID_SERVER_KEY),
                    )

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "shell history" in result.output, (
        f"Expected 'shell history' warning in output: {result.output}"
    )


@pytest.mark.integration
def test_wizard_server_key_prints_restart_note(runner: CliRunner, tmp_path: Path) -> None:
    """--server-key prints restart note to output."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"

    with _patched_wizard():
        with _no_anthropic_key():
            with patch.dict(os.environ, {"ARCHON_SEARCH_DATA_DIR": str(tmp_path)}):
                with patch("archon_search.install.os.chmod"):
                    result = runner.invoke(
                        main,
                        _wizard_args(config_path, "--server-key", _VALID_SERVER_KEY),
                    )

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "archon-search restart" in result.output, (
        f"Expected restart note in output: {result.output}"
    )


@pytest.mark.integration
def test_wizard_server_key_with_env_var_set_prints_priority_warning(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--server-key with ARCHON_SEARCH_API_KEY set prints env-var priority warning."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"

    with _patched_wizard():
        with patch.dict("os.environ", {"ARCHON_SEARCH_API_KEY": "b" * 64}, clear=False):
            with patch.dict(
                "os.environ",
                {
                    **{k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"},
                    "ARCHON_SEARCH_DATA_DIR": str(tmp_path),
                },
                clear=True,
            ):
                with patch("archon_search.install.os.chmod"):
                    result = runner.invoke(
                        main,
                        _wizard_args(config_path, "--server-key", _VALID_SERVER_KEY),
                    )

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "ARCHON_SEARCH_API_KEY" in result.output, (
        f"Expected ARCHON_SEARCH_API_KEY mention in output: {result.output}"
    )
    assert "priority" in result.output.lower() or "takes priority" in result.output, (
        f"Expected priority warning in output: {result.output}"
    )


@pytest.mark.integration
def test_wizard_server_key_with_env_var_set_still_writes_file(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--server-key with ARCHON_SEARCH_API_KEY set still writes the key to the key file."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"

    with _patched_wizard():
        with patch.dict("os.environ", {"ARCHON_SEARCH_API_KEY": "b" * 64}, clear=False):
            with patch.dict(
                "os.environ",
                {
                    **{k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"},
                    "ARCHON_SEARCH_DATA_DIR": str(tmp_path),
                },
                clear=True,
            ):
                with patch("archon_search.install.os.chmod"):
                    result = runner.invoke(
                        main,
                        _wizard_args(config_path, "--server-key", _VALID_SERVER_KEY),
                    )

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert key_file.exists(), "key file should be written even when ARCHON_SEARCH_API_KEY env is set"
    content = key_file.read_text()
    assert f"ARCHON_SEARCH_API_KEY={_VALID_SERVER_KEY}" in content, (
        f"key file should contain the --server-key value: {content!r}"
    )

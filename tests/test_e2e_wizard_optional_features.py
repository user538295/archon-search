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
from click.testing import CliRunner, Result

from archon_search.cli.main import main
from archon_search.install import InstallError, RealInstaller
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
        "_install_query_expansion_extras": MagicMock(return_value=[]),
        # 2026-07-16: the fasttext model download now runs for any multilingual
        # profile regardless of --skip-preload, so mock the license gate + download
        # by default to keep multilingual e2e tests hermetic (no real network / no
        # non-interactive license SystemExit). Per-test overrides still win.
        "_prompt_fasttext_license": MagicMock(),
        "_download_fasttext_model": MagicMock(),
    }

    # Merge extra patches (using short names for consistency)
    for key, val in extra_module_patches.items():
        short_key = key.replace("archon_search.install.installer.", "")
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

    with patch.multiple("archon_search.install.installer", **module_level):
        with patch.multiple(RealInstaller, **installer_patches):
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

    # S561: accepted defaults are NOT written — 20_wizard.md §"Only the choices you actually made are written"
    assert "enabled" not in doc.get("telemetry", {}), "telemetry.enabled should be omitted (default)"
    assert "routing_strategy" not in doc.get("routing", {}), "routing_strategy should be omitted (default centroid)"
    assert "watch" not in doc.get("collections", {}), "watch should be omitted (default false)"
    assert "format" not in doc.get("logging", {}), "logging.format should be omitted (default text)"


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
    #  3. keep reranker enabled: "n" (disables — profile=balanced has reranker)
    #  4. watch: "y"
    #  5. telemetry: "y"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default=centroid)
    #  8. log format: "" (default=text)
    #  9. HyDE/RAG Fusion: "n"
    # 10. graph enrichment: "n"
    # 11. "Proceed?": "y"
    stdin_responses = "\n".join(["n", "n", "n", "y", "y", "n", "", "", "n", "n", "y"]) + "\n"

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


@pytest.mark.integration
def test_e2e_interactive_force_delete_db_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    """S03: the documented prompt sequence plus the delete confirmation exits 0.

    Guards the prompt count: an extra or missing prompt shifts every later answer
    by one, so the `yes` meant for the destructive confirmation is swallowed and
    the wizard aborts with exit 1.
    """
    config_path = tmp_path / "archon-search.toml"

    # 1 multilingual n / 2 code n / 3 reranker n / 4 watch n / 5 telemetry n /
    # 6 eager n / 7 routing "" / 8 log format "" / 9 HyDE n / 10 graph enrichment n /
    # 11 delete-db confirm "yes" / 12 "Proceed?" y
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "", "", "n", "n", "yes", "y"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard():
            result = runner.invoke(
                main,
                [
                    "wizard",
                    "--profile", "minimal",
                    "--config", str(config_path),
                    "--force",
                    "--delete-db",
                    "--skip-preload",
                ],
                input=stdin_responses,
            )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"


# Every prompt and provider line of the interactive flow, in the order
# documented in Documentation/UserManual/20_wizard.md (Step 1 → Step 6).
# --profile is omitted so the Step 2 profile prompt fires; GPU is NONE so
# Step 3 shows no prompt.
_DOCUMENTED_PROMPT_ORDER = [
    "Will your corpus include non-English documents? [y/N]: ",
    "Choice [1-3, default 1]: ",
    "Index code files (installs tree-sitter + graph enrichment, enables graph)? [y/N]: ",
    "Keep reranker enabled? [Y/n]: ",
    "Auto-watch directories and re-index on file changes? [y/N]: ",
    "Enable local query telemetry? [y/N]: ",
    "Pre-load embedding models at startup (eliminates first-query latency)? [y/N]: ",
    "Routing strategy (centroid/hybrid) [centroid]: ",
    "Log format (text/json) [text]: ",
    "    anthropic  - Anthropic API (needs ANTHROPIC_API_KEY)",
    "    openai     - OpenAI API (needs OPENAI_API_KEY)",
    "    ollama     - runs locally, no API key",
    "    claude_cli - uses Claude Code's login, no API key",
    "    llama_cpp  - runs against a local llama-server, no API key",
    "Enable AI query expansion (HyDE + RAG Fusion)? [y/N]: ",
    "Enable LLM-backed graph enrichment? [y/N]: ",
    "Proceed? [Y/n]: ",
]


# Answers driving the full interactive flow above: 1 multilingual n /
# 2 profile "" (→ minimal) / 3 code n / 4 reranker n / 5 watch n /
# 6 telemetry n / 7 eager n / 8 routing "" / 9 log "" / 10 HyDE n /
# 11 graph enrichment n / 12 "Proceed?" y
_DOCUMENTED_FLOW_ANSWERS = (
    "\n".join(["n", "", "n", "n", "n", "n", "n", "", "", "n", "n", "y"]) + "\n"
)


def _run_documented_flow(runner: CliRunner, tmp_path: Path) -> Result:
    """Drive the wizard through the whole documented prompt sequence.

    Answers every prompt in `_DOCUMENTED_PROMPT_ORDER` with its default, so the
    captured output is the reference transcript that `20_wizard.md` documents.
    Asserts a clean exit so callers can assume the flow completed.
    """
    config_path = tmp_path / "archon-search.toml"

    with _no_anthropic_key():
        with _patched_wizard():
            result = runner.invoke(
                main,
                ["wizard", "--config", str(config_path), "--skip-preload"],
                input=_DOCUMENTED_FLOW_ANSWERS,
            )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    return result


@pytest.mark.integration
def test_wizard_prompts_appear_in_documented_order(runner: CliRunner, tmp_path: Path) -> None:
    """S03: every documented prompt is printed, once, in the documented order."""
    result = _run_documented_flow(runner, tmp_path)

    position = -1
    for expected in _DOCUMENTED_PROMPT_ORDER:
        found = result.output.find(expected, position + 1)
        assert found != -1, f"Prompt not found after position {position}: {expected!r}\nOUT: {result.output}"
        position = found


# The phrase the S03 acceptance scenario greps for to locate the Step 5e prompt
# in wizard output.  It is a contiguous substring of the prompt transcribed in
# `Documentation/UserManual/20_wizard.md` section "5e. Eager load" — note that
# `10_installation.md`'s `--eager-load` row describes the CLI flag, not this
# prompt, and deliberately says "and the reranker" instead.
_STEP5E_EAGER_LOAD_PHRASE = "Pre-load embedding models at startup"


@pytest.mark.integration
def test_step5e_eager_load_prompt_wording(runner: CliRunner, tmp_path: Path) -> None:
    """S03-step5e: exactly one Step 5e prompt, and it carries the eager-load phrase.

    `_DOCUMENTED_PROMPT_ORDER` pins the full prompt string but only checks that
    it appears *somewhere after* the previous prompt.  This test additionally
    pins that the eager-load prompt is emitted exactly **once**, so a duplicated
    or relocated Step 5e is a distinct, self-describing failure rather than a
    silent pass.

    The phrase itself has flipped three times (32b783ca added "and reranker" when
    eager loading grew to warm the cross-encoder; b5994d73 pinned that wording;
    0d01dd3e restored this one).  The wording here is the settled contract — the
    description block printed directly above the prompt still names the reranker.
    """
    result = _run_documented_flow(runner, tmp_path)

    # Locate the Step 5e prompt itself, so a failure below is a wording defect
    # and not a missing/relocated prompt.
    step5e_lines = [
        line
        for line in result.output.splitlines()
        if "at startup (eliminates first-query latency)?" in line
    ]
    assert len(step5e_lines) == 1, (
        f"Expected exactly one Step 5e eager-load prompt, got {step5e_lines!r}\n"
        f"OUT: {result.output}"
    )

    assert _STEP5E_EAGER_LOAD_PHRASE in step5e_lines[0], (
        f"Step 5e prompt must contain {_STEP5E_EAGER_LOAD_PHRASE!r}; "
        f"emitted: {step5e_lines[0]!r}"
    )


@pytest.mark.integration
def test_proceed_prompt_accepts_yes_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    """Proceed? prompt accepts 'yes' (full word) as confirmation — exits 0.

    Guards the EOFError + 'yes' acceptance fix in installer.py:769-773.
    Same input sequence as test_e2e_interactive_force_delete_db_exits_zero
    but 'yes' replaces 'y' at the final Proceed? prompt.
    """
    config_path = tmp_path / "archon-search.toml"

    # 1 multilingual n / 2 code n / 3 reranker n / 4 watch n / 5 telemetry n /
    # 6 eager n / 7 routing "" / 8 log format "" / 9 HyDE n / 10 graph enrichment n /
    # 11 delete-db confirm "yes" / 12 "Proceed?" yes
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "", "", "n", "n", "yes", "yes"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard():
            result = runner.invoke(
                main,
                [
                    "wizard",
                    "--profile", "minimal",
                    "--config", str(config_path),
                    "--force",
                    "--delete-db",
                    "--skip-preload",
                ],
                input=stdin_responses,
            )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"


@pytest.mark.integration
def test_proceed_prompt_eof_aborts_installation(runner: CliRunner, tmp_path: Path) -> None:
    """Proceed? prompt receiving EOF aborts the installation — exits non-zero.

    Guards the EOFError guard in installer.py:769-773: stdin exhausts at Proceed?,
    the EOFError handler sets answer='n', which aborts with exit 1.
    """
    config_path = tmp_path / "archon-search.toml"

    # Same as test_proceed_prompt_accepts_yes_exits_zero but the final 'yes' for
    # Proceed? is omitted so stdin exhausts there, triggering the EOFError path.
    # 1 multilingual n / 2 code n / 3 reranker n / 4 watch n / 5 telemetry n /
    # 6 eager n / 7 routing "" / 8 log format "" / 9 HyDE n / 10 graph enrichment n /
    # 11 delete-db confirm "yes"  (stdin exhausted → EOFError at Proceed?)
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "", "", "n", "n", "yes"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard():
            result = runner.invoke(
                main,
                [
                    "wizard",
                    "--profile", "minimal",
                    "--config", str(config_path),
                    "--force",
                    "--delete-db",
                    "--skip-preload",
                ],
                input=stdin_responses,
            )

    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}:\nOUT: {result.output}"
    assert "Installation aborted." in result.output, (
        f"Expected 'Installation aborted.' in output:\n{result.output}"
    )


@pytest.mark.integration
def test_e2e_interactive_no_profile_flag_metal_gpu_force_delete_exits_zero(
    runner: CliRunner, tmp_path: Path
) -> None:
    """S03 regression: no --profile + Metal GPU path must have all 14 inputs aligned.

    Without --profile the profile-choice prompt fires.  With Metal GPU detected
    the GPU-confirm prompt fires.  Graph enrichment (section 5i) fires right
    after HyDE.  A test that omits the 'n' for graph enrichment sends its
    'yes' to the wrong gate and the wizard aborts with exit 1.
    """
    config_path = tmp_path / "archon-search.toml"

    # 1 multilingual n / 2 profile "" (→ minimal) / 3 GPU "" (accept Metal) /
    # 4 code n / 5 reranker n / 6 watch n / 7 telemetry n / 8 eager n /
    # 9 routing "" / 10 log "" / 11 HyDE n / 12 graph enrichment n /
    # 13 delete-db confirm "yes" / 14 "Proceed?" y
    stdin_responses = "\n".join(
        ["n", "", "", "n", "n", "n", "n", "n", "", "", "n", "n", "yes", "y"]
    ) + "\n"

    with _no_anthropic_key():
        with _patched_wizard():
            with patch.multiple(
                RealInstaller,
                detect_gpu=MagicMock(return_value=GpuType.METAL),
                validate_embedder_only=MagicMock(return_value=False),
            ):
                result = runner.invoke(
                    main,
                    [
                        "wizard",
                        "--config", str(config_path),
                        "--force",
                        "--delete-db",
                        "--skip-preload",
                    ],
                    input=stdin_responses,
                )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["embedding_model"] == "BAAI/bge-small-en-v1.5", (
        "Profile '' (Enter) should resolve to minimal English"
    )
    # Pin input #5 (reranker "n") to its actual prompt via the config effect
    assert doc["database"]["reranker_model"] == ""  # "n" at reranker → disabled → empty
    # General alignment oracle: no input was silently swallowed by _ask_choice retry
    assert "Invalid value" not in result.output
    assert "Invalid choice" not in result.output


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
    # 10. graph enrichment: "n"
    #  confirmation: "y"
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "n", "n", "y"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{"archon_search.install.installer._select_profile": select_profile_spy,
               "archon_search.install.installer._prompt_fasttext_license": MagicMock(),
               "archon_search.install.installer._download_fasttext_model": MagicMock()}
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
    #  3. keep reranker enabled: "n" (disables — minimal has reranker → question shown)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing (bad): "badval"  ← triggers retry
    #  8. routing (retry, valid): "hybrid"
    #  9. log format: ""
    # 10. HyDE/RAG Fusion: "n"
    # 11. graph enrichment: "n"
    # 12. "Proceed?": "y"
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "badval", "hybrid", "", "n", "n", "y"]) + "\n"

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
        "archon_search.install.installer._install_code_extra": install_code_mock,
        "archon_search.install.installer._install_graph_extra": install_graph_mock,
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
            "archon_search.install.installer._install_code_extra": install_code_mock,
            "archon_search.install.installer._install_graph_extra": install_graph_mock,
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
            "archon_search.install.installer._install_code_extra": install_code_mock,
            "archon_search.install.installer._install_graph_extra": install_graph_mock,
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
            "archon_search.install.installer._install_code_extra": install_code_mock,
            "archon_search.install.installer._install_graph_extra": install_graph_mock,
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
            "archon_search.install.installer._install_code_extra": install_code_mock,
            "archon_search.install.installer._install_graph_extra": install_graph_mock,
            "archon_search.install.installer._check_disk_space": disk_space_mock,
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
    #  3. keep reranker enabled: "n" (disables)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default)
    #  8. log format: "" (default)
    #  9. HyDE/RAG Fusion: "n"
    # 10. graph enrichment: "n"
    # 11. "Proceed?": "n"  ← decline
    stdin_responses = "\n".join(["n", "y", "n", "n", "n", "n", "", "", "n", "n", "n"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{
                "archon_search.install.installer._install_code_extra": install_code_mock,
                "archon_search.install.installer._install_graph_extra": install_graph_mock,
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
# Use case 8a: HyDE / RAG Fusion provider install + rollback
# (Documentation/Backlog/2026-07-15-060-hyde-ragfusion-wizard-brief.md)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_hyde_install_triggered(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --enable-hyde installs the provider package once
    and writes [hyde].enabled = true."""
    config_path = tmp_path / "archon-search.toml"
    install_mock = MagicMock(return_value=[])

    with _patched_wizard(
        **{"archon_search.install.installer._install_query_expansion_extras": install_mock}
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--enable-hyde",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    install_mock.assert_called_once()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True


@pytest.mark.integration
def test_e2e_hyde_and_rag_fusion_both_enabled(runner: CliRunner, tmp_path: Path) -> None:
    """Both flags → both sections enabled in the written config."""
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--enable-hyde",
            "--enable-rag-fusion",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True
    assert doc["rag_fusion"]["enabled"] is True


@pytest.mark.integration
def test_e2e_hyde_install_failure_reverts(runner: CliRunner, tmp_path: Path) -> None:
    """_install_query_expansion_extras returning a failed package → exit 0, [hyde].enabled
    reverted to false so the next server start does not hard-fail on missing anthropic."""
    config_path = tmp_path / "archon-search.toml"
    install_mock = MagicMock(return_value=["hyde"])

    with _patched_wizard(
        **{"archon_search.install.installer._install_query_expansion_extras": install_mock}
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--enable-hyde",
        ])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
    install_mock.assert_called_once()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is False, (
        "hyde.enabled must be reverted to false when the provider package fails to "
        "install — otherwise the next server start hard-fails on missing anthropic"
    )
    assert "disabled because the install did not complete" in result.output


@pytest.mark.integration
def test_e2e_hyde_disk_space_failure_reverts(runner: CliRunner, tmp_path: Path) -> None:
    """Disk-space check failure after config write must revert hyde.enabled (mirrors C3-A-1)."""
    config_path = tmp_path / "archon-search.toml"
    install_mock = MagicMock(return_value=[])
    disk_space_mock = MagicMock(side_effect=InstallError("not enough disk space"))

    with _patched_wizard(
        **{
            "archon_search.install.installer._install_query_expansion_extras": install_mock,
            "archon_search.install.installer._check_disk_space": disk_space_mock,
        }
    ):
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--enable-hyde",
        ])

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}: {result.output}"
    install_mock.assert_not_called()
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is False
    assert "disabled because the install did not complete" in result.output


@pytest.mark.integration
def test_e2e_hyde_declineProceedPrompt_reverts(runner: CliRunner, tmp_path: Path) -> None:
    """Declining the final 'Proceed?' prompt must revert hyde.enabled.

    Passing --enable-hyde short-circuits the interactive HyDE/provider prompts,
    so the queue below covers the remaining optional-feature prompts for a
    minimal English profile (which HAS a reranker, so that question is shown).
    """
    config_path = tmp_path / "archon-search.toml"
    install_mock = MagicMock(return_value=[])

    # Input queue (minimal English profile HAS a reranker; --enable-hyde skips
    # the HyDE/RAG Fusion prompt):
    #  1. multilingual: "n"
    #  2. code enrichment: "n"
    #  3. keep reranker enabled: "n" (disables)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default)
    #  8. log format: "" (default)
    #  9. "Proceed?": "n"  ← decline
    stdin_responses = "\n".join(["n", "n", "n", "n", "n", "n", "", "", "n"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{"archon_search.install.installer._install_query_expansion_extras": install_mock}
        ):
            result = runner.invoke(
                main,
                [
                    "wizard",
                    "--profile", "minimal",
                    "--config", str(config_path),
                    "--skip-preload",
                    "--enable-hyde",
                ],
                input=stdin_responses,
            )

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}:\nOUT: {result.output}"
    install_mock.assert_not_called()
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is False
    assert "disabled because the install did not complete" in result.output


@pytest.mark.integration
def test_wizard_llama_cpp_model_picker_cache_populated(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive wizard, llama_cpp provider chosen for HyDE + RAG Fusion, non-empty
    local model cache → the numbered picker is presented and its choice written to the
    TOML (S4)."""
    config_path = tmp_path / "archon-search.toml"

    # Input queue (minimal English profile HAS a reranker):
    #  1. multilingual: "n"
    #  2. code enrichment: "n"
    #  3. keep reranker enabled: "n" (disables)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default)
    #  8. log format: "" (default)
    #  9. "Enable AI query expansion?": "y"
    # 10. "Which provider for HyDE?": "llama_cpp"
    # 11. llama-server base URL for HyDE: "" (default)
    # 12. numbered model picker for HyDE: "1"
    # 13. "Which provider for RAG Fusion?": "llama_cpp"
    # 14. llama-server base URL for RAG Fusion: "" (default)
    # 15. numbered model picker for RAG Fusion: "2"
    # 16. "Enable LLM-backed graph enrichment?": "n"
    # 17. "Proceed?": "y"
    stdin_responses = (
        "\n".join(
            ["n", "n", "n", "n", "n", "n", "", "", "y", "llama_cpp", "", "1", "llama_cpp", "", "2", "n", "y"]
        )
        + "\n"
    )

    with _no_anthropic_key():
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1", "m2"]) as mock_fetch:
            with _patched_wizard():
                result = runner.invoke(
                    main,
                    ["wizard", "--profile", "minimal", "--config", str(config_path), "--skip-preload"],
                    input=stdin_responses,
                )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    assert mock_fetch.call_count == 2
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True
    assert doc["hyde"]["provider"] == "llama_cpp"
    assert doc["hyde"]["model"] == "m1"
    assert doc["rag_fusion"]["enabled"] is True
    assert doc["rag_fusion"]["provider"] == "llama_cpp"
    assert doc["rag_fusion"]["model"] == "m2"


@pytest.mark.integration
def test_wizard_llama_cpp_model_picker_cache_empty(runner: CliRunner, tmp_path: Path) -> None:
    """Interactive wizard, llama_cpp provider chosen, empty local model cache → free-text
    model entry is shown instead of the numbered picker, and never raises (S12)."""
    config_path = tmp_path / "archon-search.toml"

    # Same queue as the reachable case, except steps 12/15 are free-text model names
    # (no numbered picker is shown when the local cache is empty); trailing "n" declines
    # the FE-2 graph-enrichment step before "Proceed?".
    stdin_responses = (
        "\n".join(
            ["n", "n", "n", "n", "n", "n", "", "", "y", "llama_cpp", "", "hmodel", "llama_cpp", "", "rmodel", "n", "y"]
        )
        + "\n"
    )

    with _no_anthropic_key():
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=[]):
            with _patched_wizard():
                result = runner.invoke(
                    main,
                    ["wizard", "--profile", "minimal", "--config", str(config_path), "--skip-preload"],
                    input=stdin_responses,
                )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True
    assert doc["hyde"]["provider"] == "llama_cpp"
    assert doc["hyde"]["model"] == "hmodel"
    assert doc["rag_fusion"]["enabled"] is True
    assert doc["rag_fusion"]["provider"] == "llama_cpp"
    assert doc["rag_fusion"]["model"] == "rmodel"


@pytest.mark.integration
def test_wizard_graph_provider_step_writes_all_three_fields(runner: CliRunner, tmp_path: Path) -> None:
    """FE-2 S18: choosing llama_cpp for graph enrichment writes provider,
    extraction_model, and llama_cpp_base_url to [graph]."""
    config_path = tmp_path / "archon-search.toml"

    # Input queue (minimal English profile HAS a reranker):
    #  1. multilingual: "n"
    #  2. code enrichment: "n"
    #  3. keep reranker enabled: "n" (disables)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default)
    #  8. log format: "" (default)
    #  9. "Enable AI query expansion?": "n"
    # 10. "Enable LLM-backed graph enrichment?": "y"
    # 11. "Which provider for graph enrichment?": "llama_cpp"
    # 12. llama-server base URL: "http://box:8080" (custom, so the key is written)
    # 13. numbered model picker: "1"
    # 14. "Proceed?": "y"
    stdin_responses = (
        "\n".join(
            ["n", "n", "n", "n", "n", "n", "", "", "n", "y", "llama_cpp", "http://box:8080", "1", "y"]
        )
        + "\n"
    )

    with _no_anthropic_key():
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1", "m2"]) as mock_fetch:
            with _patched_wizard():
                result = runner.invoke(
                    main,
                    ["wizard", "--profile", "minimal", "--config", str(config_path), "--skip-preload"],
                    input=stdin_responses,
                )

    assert result.exit_code == 0, f"Exit {result.exit_code}:\nOUT: {result.output}"
    mock_fetch.assert_called_once_with()  # the local cache probe takes no base URL
    doc = tomlkit.parse(config_path.read_text())
    assert doc["graph"]["provider"] == "llama_cpp"
    assert doc["graph"]["extraction_model"] == "m1"
    assert doc["graph"]["llama_cpp_base_url"] == "http://box:8080"


@pytest.mark.integration
def test_wizard_abort_reverts_graph_enrichment_only(runner: CliRunner, tmp_path: Path) -> None:
    """FE-2 S22: declining the final 'Proceed?' prompt after configuring graph
    enrichment strips provider/extraction_model/llama_cpp_base_url from [graph],
    but leaves graph.enabled untouched — distinct from _revert_graph_enabled_flag,
    which is never triggered here since code enrichment (install_graph_extra) is
    declined in this test's input queue."""
    config_path = tmp_path / "archon-search.toml"

    # Input queue (minimal English profile HAS a reranker):
    #  1. multilingual: "n"
    #  2. code enrichment: "n"   (install_graph_extra stays False — isolates the assertion)
    #  3. keep reranker enabled: "n" (disables)
    #  4. watch: "n"
    #  5. telemetry: "n"
    #  6. eager load: "n"
    #  7. routing strategy: "" (default)
    #  8. log format: "" (default)
    #  9. "Enable AI query expansion?": "n"
    # 10. "Enable LLM-backed graph enrichment?": "y"
    # 11. "Which provider for graph enrichment?": "llama_cpp"
    # 12. llama-server base URL: "http://box:8080"
    # 13. numbered model picker: "1"
    # 14. "Proceed?": "n"  ← decline
    stdin_responses = (
        "\n".join(
            ["n", "n", "n", "n", "n", "n", "", "", "n", "y", "llama_cpp", "http://box:8080", "1", "n"]
        )
        + "\n"
    )

    with _no_anthropic_key():
        with patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=["m1"]):
            with _patched_wizard():
                result = runner.invoke(
                    main,
                    ["wizard", "--profile", "minimal", "--config", str(config_path), "--skip-preload"],
                    input=stdin_responses,
                )

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}:\nOUT: {result.output}"
    assert config_path.exists(), "Config should have been written before the Proceed? prompt"
    doc = tomlkit.parse(config_path.read_text())
    assert "provider" not in doc["graph"]
    assert "extraction_model" not in doc["graph"]
    assert "llama_cpp_base_url" not in doc["graph"]
    assert "enabled" not in doc["graph"], (
        "graph.enabled must remain untouched by the enrichment-only revert — "
        "install_graph_extra was never selected in this test"
    )


@pytest.mark.integration
def test_e2e_summary_shows_hyde_bullet(runner: CliRunner, tmp_path: Path) -> None:
    """The install summary must visibly confirm HyDE was enabled (mandatory confirmation)."""
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--enable-hyde",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "HyDE: enabled (provider: anthropic)" in result.output


@pytest.mark.integration
def test_e2e_post_write_assertion_fires_on_silent_drop(runner: CliRunner, tmp_path: Path) -> None:
    """If the config write silently drops the [hyde] section, the post-write
    assertion must abort the install with a clear error (Q1)."""
    config_path = tmp_path / "archon-search.toml"

    with patch("archon_search.install.config_writer._apply_wizard_features_to_toml", MagicMock()), _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--enable-hyde",
        ])

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}: {result.output}"
    assert "hyde" in result.output.lower() and "persist" in result.output.lower()


# ---------------------------------------------------------------------------
# Use case 8b: Multilingual extra install (2026-07-15-040)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_multilingual_extra_install_triggered(runner: CliRunner, tmp_path: Path) -> None:
    """wizard --non-interactive --multilingual → _install_multilingual_extra called once."""
    config_path = tmp_path / "archon-search.toml"
    install_multilingual_mock = MagicMock()

    with _patched_wizard(
        **{"archon_search.install.installer._install_multilingual_extra": install_multilingual_mock}
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
        **{"archon_search.install.installer._install_multilingual_extra": install_multilingual_mock}
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
        **{"archon_search.install.installer._install_multilingual_extra": install_multilingual_mock}
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
            "archon_search.install.installer._install_multilingual_extra": install_multilingual_mock,
            "archon_search.install.installer._check_disk_space": disk_space_mock,
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
    #  9. graph enrichment: "n"
    # 10. "Proceed?": "n"  ← decline
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "n", "n", "n"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{"archon_search.install.installer._install_multilingual_extra": install_multilingual_mock}
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
    stdin_responses = "\n".join(["y", "n", "n", "n", "n", "", "", "n", "n", "y"]) + "\n"

    with _no_anthropic_key():
        with _patched_wizard(
            **{"archon_search.install.installer._install_multilingual_extra": install_multilingual_mock}
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
            "archon_search.install.installer._install_multilingual_extra": install_mock,
            "archon_search.install.installer._prewarm_models": prewarm_mock,
            "archon_search.install.installer._download_fasttext_model": MagicMock(),
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
    """--non-interactive with ANTHROPIC_API_KEY set: [hyde] and [rag_fusion] written with enabled=false."""
    config_path = tmp_path / "archon-search.toml"
    with _patched_wizard():
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"}):
            result = runner.invoke(main, _wizard_args(config_path))
    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is False
    assert doc["rag_fusion"]["enabled"] is False


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


# ---------------------------------------------------------------------------
# S561 regression — explicitly-passed flag must be written even when matching default
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_s561_routing_strategy_written_when_explicitly_passed(
    runner: CliRunner, tmp_path: Path
) -> None:
    """S561 (flag half): when --routing-strategy centroid is passed explicitly,
    [routing].routing_strategy must appear in the TOML even though "centroid" is
    the wizard default.

    See 20_wizard.md:703: "Passing an explicit flag value (even if it matches the
    default, e.g. --port 8765) always writes the key."
    """
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--routing-strategy", "centroid",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())

    routing_strategy = doc.get("routing", {}).get("routing_strategy")
    assert routing_strategy == "centroid", (
        f"routing_strategy should be written when explicitly passed; got {routing_strategy!r}"
    )


@pytest.mark.integration
def test_s561_log_format_written_when_explicitly_passed(
    runner: CliRunner, tmp_path: Path
) -> None:
    """S561 (flag half, log_format twin): when --log-format text is passed explicitly,
    [logging].format must appear in the TOML even though "text" is the wizard default.

    See 20_wizard.md: "Passing an explicit flag value (even if it matches the
    default, e.g. --port 8765) always writes the key."
    """
    config_path = tmp_path / "archon-search.toml"

    with _patched_wizard():
        result = runner.invoke(main, [
            "wizard",
            "--non-interactive",
            "--profile", "minimal",
            "--config", str(config_path),
            "--skip-preload",
            "--log-format", "text",
        ])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert config_path.exists()
    doc = tomlkit.parse(config_path.read_text())

    log_format = doc.get("logging", {}).get("format")
    assert log_format == "text", (
        f"log_format should be written when explicitly passed; got {log_format!r}"
    )

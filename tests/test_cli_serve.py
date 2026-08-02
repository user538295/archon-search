"""TDD tests for archon-search serve CLI subcommand (Task 3.1, C9 plan).

The `serve` command is a foreground-blocking entry point used by the Docker
container. It:
  - Calls `load_config(path, serve=True)` so the host default is `0.0.0.0`.
  - Calls `run_server(config)` and never touches platform service management.
  - Emits a startup log warning when ARCHON_SEARCH_DATA_DIR is set but
    ARCHON_SEARCH_CONFIG is not — surfacing the container collection-management
    limitation at runtime.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.config import ConfigError, SearchConfig, get_default_config_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config_with_host(host: str) -> SearchConfig:
    cfg = SearchConfig()
    cfg.host = host
    return cfg


# ---------------------------------------------------------------------------
# serve subcommand behaviour
# ---------------------------------------------------------------------------

def test_serve_calls_run_server(runner: CliRunner) -> None:
    """`serve` must call `run_server` exactly once with the loaded config."""
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg) as mock_load,
        patch("archon_search.server.app.run_server") as mock_run,
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    mock_load.assert_called_once()
    # run_server receives the resolved config path so collection add/remove
    # persists to the file this config was loaded from (S07/S252).
    mock_run.assert_called_once_with(cfg, get_default_config_path())


def test_serve_uses_serve_load_config(runner: CliRunner) -> None:
    """`load_config` must be invoked with `serve=True` so the host default flips to 0.0.0.0."""
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg) as mock_load,
        patch("archon_search.server.app.run_server"),
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    # serve=True must be passed (positional config path may be None).
    _, kwargs = mock_load.call_args
    assert kwargs.get("serve") is True, f"Expected serve=True, got {mock_load.call_args}"


def test_serve_host_defaults_to_0000(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no env/TOML overrides, the loaded config's host is 0.0.0.0 in serve mode."""
    captured: dict[str, SearchConfig] = {}

    def _capture(cfg: SearchConfig, config_path: Path | None = None) -> None:
        captured["cfg"] = cfg

    # Real load_config — but force the config path to a non-existent file inside tmp_path
    # so no TOML is read (load_config falls through `FileNotFoundError`).
    nonexistent = tmp_path / "nonexistent.toml"
    with patch("archon_search.server.app.run_server", side_effect=_capture):
        result = runner.invoke(main, ["serve", "--config", str(nonexistent)])
    assert result.exit_code == 0, result.output
    assert captured["cfg"].host == "0.0.0.0"


def test_serve_respects_host_env_var(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit ARCHON_SEARCH_HOST env var overrides the serve default."""
    monkeypatch.setenv("ARCHON_SEARCH_HOST", "192.168.1.1")
    captured: dict[str, SearchConfig] = {}

    def _capture(cfg: SearchConfig, config_path: Path | None = None) -> None:
        captured["cfg"] = cfg

    nonexistent = tmp_path / "nonexistent.toml"
    with patch("archon_search.server.app.run_server", side_effect=_capture):
        result = runner.invoke(main, ["serve", "--config", str(nonexistent)])
    assert result.exit_code == 0, result.output
    assert captured["cfg"].host == "192.168.1.1"


def test_serve_forwards_config_path_to_load_config(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """The `--config <path>` value is forwarded to `load_config` as the positional arg.

    Guards against the failure mode where a refactor silently hardcodes
    `load_config(None, serve=True)` and ignores the user-supplied `--config`.
    """
    config_file = tmp_path / "my-search.toml"
    config_file.write_text("[server]\nport = 9000\n")
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg) as mock_load,
        patch("archon_search.server.app.run_server"),
    ):
        result = runner.invoke(main, ["serve", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    mock_load.assert_called_once_with(config_file, serve=True)


def test_serve_forwards_config_path_to_run_server(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """`--config <path>` must reach `run_server` so collection add/remove persists
    to THAT file, not the default TOML (S07/S252 regression guard).
    """
    config_file = tmp_path / "my-search.toml"
    config_file.write_text("[server]\nport = 9000\n")
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server") as mock_run,
    ):
        result = runner.invoke(main, ["serve", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with(cfg, config_file)


def test_serve_config_error_exits_nonzero(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """A ConfigError from `load_config` causes serve to exit non-zero with a stderr message.

    Mirrors `test_start_config_error_exits_nonzero` in `tests/cli/test_start_stop.py`.
    """
    bad_config = tmp_path / "bad.toml"
    bad_config.write_text("[broken")  # malformed TOML
    with patch("archon_search.server.app.run_server") as mock_run:
        result = runner.invoke(main, ["serve", "--config", str(bad_config)])
    assert result.exit_code != 0
    assert "Error" in result.output or "error" in result.output.lower()
    mock_run.assert_not_called()


def test_serve_config_error_message_propagated(runner: CliRunner) -> None:
    """The ConfigError message text is surfaced verbatim to the operator."""
    with (
        patch(
            "archon_search.cli.serve.load_config",
            side_effect=ConfigError("port out of range"),
        ),
        patch("archon_search.server.app.run_server") as mock_run,
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code != 0
    assert "port out of range" in result.output
    mock_run.assert_not_called()


def test_serve_does_not_call_service_management(runner: CliRunner) -> None:
    """`serve` must NEVER call `_get_service` — it is a pure foreground call."""
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server"),
        patch("archon_search.cli._helpers._get_service") as mock_get_service,
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    mock_get_service.assert_not_called()


def test_serve_registered_in_cli() -> None:
    """`serve` must be registered as a top-level subcommand of `main`."""
    from archon_search.cli.main import main as main_group  # noqa: F401 — import in test for explicitness

    assert "serve" in main_group.commands, (
        f"`serve` missing from CLI commands: {sorted(main_group.commands)}"
    )


def test_start_still_registered_in_cli() -> None:
    """Sanity check: registering `serve` did not remove or shadow the existing `start`."""
    from archon_search.cli.main import main as main_group

    assert "start" in main_group.commands, (
        f"`start` missing from CLI commands: {sorted(main_group.commands)}"
    )


def test_serve_warns_when_data_dir_set_without_config(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ARCHON_SEARCH_DATA_DIR is set but ARCHON_SEARCH_CONFIG is not,
    `serve` emits a startup warning explaining that `collection add/remove`
    will fail inside the container.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server"),
        caplog.at_level(logging.WARNING, logger="archon_search.cli.serve"),
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    # Look for the warning that mentions ARCHON_SEARCH_CONFIG.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING log record"
    assert any("ARCHON_SEARCH_CONFIG" in r.message for r in warning_records), (
        f"Expected ARCHON_SEARCH_CONFIG in warning messages, got: {[r.message for r in warning_records]}"
    )


def test_serve_no_warning_when_both_data_dir_and_config_set(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both ARCHON_SEARCH_DATA_DIR and ARCHON_SEARCH_CONFIG are set,
    the container-limitation warning is suppressed.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", "/data/archon-search.toml")
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server"),
        caplog.at_level(logging.WARNING, logger="archon_search.cli.serve"),
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    # The DATA_DIR/CONFIG warning specifically should NOT appear.
    assert not any(
        "ARCHON_SEARCH_CONFIG" in r.message and "collection" in r.message.lower()
        for r in caplog.records
    ), f"Did not expect container-limitation warning, got: {[r.message for r in caplog.records]}"


@pytest.mark.archon_unset_data_dir
def test_serve_no_warning_when_data_dir_unset(
    runner: CliRunner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ARCHON_SEARCH_DATA_DIR is unset, no container-limitation warning fires."""
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server"),
        caplog.at_level(logging.WARNING, logger="archon_search.cli.serve"),
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    assert not any(
        "ARCHON_SEARCH_CONFIG" in r.message and "collection" in r.message.lower()
        for r in caplog.records
    ), f"Did not expect container-limitation warning, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# BE-2: run_server import moved into serve() body (lazy import)
# ---------------------------------------------------------------------------

def test_serve_invokes_run_server_with_config(runner: CliRunner) -> None:
    """After BE-2, run_server is imported lazily inside serve().

    - Patching at `archon_search.server.app.run_server` must intercept the call.
    - `archon_search.cli.serve` must NOT expose `run_server` as a module attribute.
    """
    import archon_search.cli.serve as serve_mod

    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server") as mock_run,
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with(cfg, get_default_config_path())
    # run_server must NOT be a module-level attribute after the lazy-import move.
    assert not hasattr(serve_mod, "run_server"), (
        "cli.serve must not expose run_server at module level after BE-2"
    )


@pytest.mark.archon_unset_data_dir
def test_serve_output_and_exit_code_unchanged(runner: CliRunner) -> None:
    """Invoking serve with a mocked run_server in a clean env (no DATA_DIR) exits 0 with no stdout/stderr output (S10 baseline)."""
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server"),
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0
    assert result.output == ""

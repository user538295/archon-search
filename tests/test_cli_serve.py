"""TDD tests for archon-search serve CLI subcommand (Task 3.1, C9 plan).

The `serve` command is a foreground-blocking entry point used by the Docker
container. It:
  - Calls `load_config(path, serve=True)` so the host default is `0.0.0.0` inside the container (loopback natively).
  - Calls `run_server(config, config_path)` and never touches platform service management.
  - Resolves the default config under ARCHON_SEARCH_DATA_DIR, so a container's
    config lives inside its /data volume without an explicit ARCHON_SEARCH_CONFIG.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.config import (
    ConfigError,
    SearchConfig,
    warn_if_legacy_config_ignored,
)
from archon_search.constants import CONTAINER_BIND_HOST, CONTAINER_ENV_VAR


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

def test_serve_calls_run_server(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`serve` must call `run_server` exactly once with the loaded config."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg) as mock_load,
        patch("archon_search.server.app.run_server") as mock_run,
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    mock_load.assert_called_once_with(None, serve=True)
    # No --config: run_server gets None and resolves the same default load_config
    # read, so collection add/remove persists to that file (S07/S252).
    mock_run.assert_called_once_with(cfg, None)


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


@pytest.mark.parametrize(
    ("container_env", "expected_host"),
    [(None, "127.0.0.1"), ("1", CONTAINER_BIND_HOST)],
    ids=["native-loopback", "container-binds-all-interfaces"],
)
def test_serve_host_default_depends_on_the_container_flag(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    container_env: str | None,
    expected_host: str,
) -> None:
    """With no env/TOML overrides, `serve` binds loopback natively and every interface only inside the container."""
    if container_env is not None:
        monkeypatch.setenv(CONTAINER_ENV_VAR, container_env)
    captured: dict[str, SearchConfig] = {}

    def _capture(cfg: SearchConfig, config_path: Path | None = None) -> None:
        captured["cfg"] = cfg

    # Real load_config — but force the config path to a non-existent file inside tmp_path
    # so no TOML is read (load_config falls through `FileNotFoundError`).
    nonexistent = tmp_path / "nonexistent.toml"
    with patch("archon_search.server.app.run_server", side_effect=_capture):
        result = runner.invoke(main, ["serve", "--config", str(nonexistent)])
    assert result.exit_code == 0, result.output
    assert captured["cfg"].host == expected_host


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


def test_serve_malformed_toml_does_not_remove_plist(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """`serve` on malformed TOML must NEVER invoke macOS plist removal (S138 regression guard).

    The launchd plist is owned exclusively by install/uninstall. This spies on
    the macOS plist-removal call sites at their real bindings per repo
    convention: `LaunchdSearchService.unregister` (the canonical plist unlink,
    patched at the class so any instance is intercepted), `_remove_legacy_service`
    (the legacy-plist unlink), and `_legacy_service_path` (a path resolver, spied
    as a tripwire) — both legacy helpers patched at their `install.installer`
    call-site binding. Preventive: serve's ConfigError path does not reach
    service management today, so this guards a future refactor that would wire
    plist removal into the serve failure path.
    """
    bad_config = tmp_path / "bad.toml"
    bad_config.write_text("[broken")  # malformed TOML

    with (
        patch("archon_search.install.installer._legacy_service_path") as mock_legacy_path,
        patch("archon_search.install.installer._remove_legacy_service") as mock_remove_legacy,
        patch("archon_search.platform.macos.LaunchdSearchService.unregister") as mock_unregister,
    ):
        result = runner.invoke(main, ["serve", "--config", str(bad_config)])

    assert result.exit_code == 1
    assert "Error" in result.output
    mock_legacy_path.assert_not_called()
    mock_remove_legacy.assert_not_called()
    mock_unregister.assert_not_called()


def test_serve_malformed_toml_preserves_real_plist_on_disk(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end S138 repro: a real plist file on disk must survive a serve
    config-parse failure.

    Stronger than `test_serve_malformed_toml_does_not_remove_plist` (which spies
    on the removal call sites): this drives the FULL real path — real
    `load_config`, real `serve` body, no mocks — with an isolated HOME that holds
    an actual `~/Library/LaunchAgents/com.archon.search.plist`, triggered exactly
    as the S138 report describes (`ARCHON_SEARCH_CONFIG` pointing at malformed
    TOML). It asserts the plist file itself still exists afterward, since
    `LaunchdSearchService` resolves the plist path from `Path.home()` at call
    time — so it also catches a future removal path that bypasses the three
    call-site symbols the mock-based guard spies on.
    """
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / "com.archon.search.plist"
    plist.write_text("<plist>installed</plist>")

    malformed = tmp_path / "malformed.toml"
    malformed.write_text("this is = = not valid [[[\n")  # malformed TOML

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(malformed))
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))

    result = runner.invoke(main, ["serve"])

    assert result.exit_code == 1, result.output
    assert "Error" in result.output
    assert plist.exists(), "S138: serve removed the launchd plist on malformed TOML"


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
    from archon_search.cli.main import main as main_group

    assert "serve" in main_group.commands, (
        f"`serve` missing from CLI commands: {sorted(main_group.commands)}"
    )


def test_start_still_registered_in_cli() -> None:
    """Sanity check: registering `serve` did not remove or shadow the existing `start`."""
    from archon_search.cli.main import main as main_group

    assert "start" in main_group.commands, (
        f"`start` missing from CLI commands: {sorted(main_group.commands)}"
    )


def _run_serve_capturing_config(runner: CliRunner, args: list[str]) -> tuple[SearchConfig, object]:
    """Invoke ``serve`` with the REAL load_config and run_server stubbed; return what run_server got."""
    captured: dict[str, object] = {}

    def _capture(cfg: SearchConfig, config_path: object) -> None:
        captured["cfg"], captured["config_path"] = cfg, config_path

    with patch("archon_search.server.app.run_server", side_effect=_capture):
        result = runner.invoke(main, ["serve", *args])
    assert result.exit_code == 0, result.output
    return captured["cfg"], captured["config_path"]  # type: ignore[return-value]


def test_serve_data_dir_without_config_uses_data_dir_config(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARCHON_SEARCH_DATA_DIR set, ARCHON_SEARCH_CONFIG unset → the config lives
    under the data dir (inside a container's /data volume), so serve loads it from there.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "archon-search.toml").write_text("[server]\nport = 9123\n")
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)
    cfg, config_path = _run_serve_capturing_config(runner, [])
    assert cfg.port == 9123
    assert config_path is None  # run_server resolves the same default itself


def test_serve_config_env_wins_over_data_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both ARCHON_SEARCH_DATA_DIR and ARCHON_SEARCH_CONFIG set → serve loads the CONFIG file."""
    config_file = tmp_path / "etc" / "archon-search.toml"
    config_file.parent.mkdir()
    config_file.write_text("[server]\nport = 9001\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "archon-search.toml").write_text("[server]\nport = 9002\n")
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(config_file))
    cfg, config_path = _run_serve_capturing_config(runner, [])
    assert cfg.port == 9001
    assert config_path is None  # run_server re-resolves ARCHON_SEARCH_CONFIG itself


def test_serve_defers_legacy_config_warning_to_run_server(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """serve must not log the legacy-config WARNING itself — that would precede configure_logging and
    bypass log_file / JSON format. It forwards ``None`` so run_server warns once logging is set up
    (pinned by tests/test_run_server_config_path_persistence.py)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)
    legacy = tmp_path / ".archon-search" / "archon-search.toml"
    legacy.parent.mkdir()
    legacy.write_text("[server]\n")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        _, config_path = _run_serve_capturing_config(runner, [])
        assert config_path is None
        assert not any(str(legacy) in r.getMessage() for r in caplog.records)
        # Positive control: this setup does trigger the warning run_server will emit.
        warn_if_legacy_config_ignored(config_path)
    assert any(str(legacy) in r.getMessage() for r in caplog.records)


def test_serve_invalid_data_dir_exits_nonzero(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ARCHON_SEARCH_DATA_DIR with no --config fails with a clean error, not a traceback."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "relative/data")
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)
    with patch("archon_search.server.app.run_server") as mock_run:
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "absolute path" in result.output
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# BE-2: run_server import moved into serve() body (lazy import)
# ---------------------------------------------------------------------------

def test_serve_invokes_run_server_with_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After BE-2, run_server is imported lazily inside serve().

    - Patching at `archon_search.server.app.run_server` must intercept the call.
    - `archon_search.cli.serve` must NOT expose `run_server` as a module attribute.
    """
    import archon_search.cli.serve as serve_mod

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    cfg = _config_with_host("0.0.0.0")
    with (
        patch("archon_search.cli.serve.load_config", return_value=cfg),
        patch("archon_search.server.app.run_server") as mock_run,
    ):
        result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with(cfg, None)
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

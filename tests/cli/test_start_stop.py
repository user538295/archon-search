"""TDD tests for archon-search start and stop CLI subcommands."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.platform.service import ServiceStatus


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_service() -> MagicMock:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=False, pid=None, uptime_seconds=None)
    return svc


# ---------------------------------------------------------------------------
# start subcommand
# ---------------------------------------------------------------------------

def test_start_calls_service_start(runner: CliRunner, mock_service: MagicMock) -> None:
    with (
        patch("archon_search.cli.start._get_service", return_value=mock_service),
        patch("archon_search.cli.start.load_config"),
    ):
        result = runner.invoke(main, ["start"])
    assert result.exit_code == 0, result.output
    mock_service.start.assert_called_once()


def test_start_prints_started_message(runner: CliRunner, mock_service: MagicMock) -> None:
    with (
        patch("archon_search.cli.start._get_service", return_value=mock_service),
        patch("archon_search.cli.start.load_config"),
    ):
        result = runner.invoke(main, ["start"])
    assert "started" in result.output.lower()


def test_start_with_custom_config_path(runner: CliRunner, mock_service: MagicMock, tmp_path: Path) -> None:
    config_file = tmp_path / "my-search.toml"
    config_file.write_text("[server]\nport = 9000\n")
    with (
        patch("archon_search.cli.start._get_service", return_value=mock_service),
        patch("archon_search.cli.start.load_config") as mock_load_config,
    ):
        result = runner.invoke(main, ["start", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    mock_service.start.assert_called_once()
    mock_load_config.assert_called_once_with(config_file)


def test_start_service_error_exits_nonzero(runner: CliRunner, mock_service: MagicMock) -> None:
    mock_service.start.side_effect = RuntimeError("launchctl load failed")
    with (
        patch("archon_search.cli.start._get_service", return_value=mock_service),
        patch("archon_search.cli.start.load_config"),
    ):
        result = runner.invoke(main, ["start"])
    assert result.exit_code != 0
    assert "launchctl load failed" in result.output


def test_start_unsupported_platform_exits_nonzero(runner: CliRunner) -> None:
    with (
        patch("archon_search.cli.start.load_config"),
        patch("archon_search.cli.start._get_service", side_effect=NotImplementedError("Unsupported platform: freebsd13")),
    ):
        result = runner.invoke(main, ["start"])
    assert result.exit_code != 0
    assert "Unsupported platform" in result.output


def test_stop_unsupported_platform_exits_nonzero(runner: CliRunner) -> None:
    with patch("archon_search.cli.stop._get_service", side_effect=NotImplementedError("Unsupported platform: freebsd13")):
        result = runner.invoke(main, ["stop"])
    assert result.exit_code != 0
    assert "Unsupported platform" in result.output


def test_start_config_error_exits_nonzero(runner: CliRunner, mock_service: MagicMock, tmp_path: Path) -> None:
    bad_config = tmp_path / "bad.toml"
    bad_config.write_text("[broken")
    with patch("archon_search.cli.start._get_service", return_value=mock_service):
        result = runner.invoke(main, ["start", "--config", str(bad_config)])
    assert result.exit_code != 0
    assert "Error" in result.output or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# stop subcommand
# ---------------------------------------------------------------------------

def test_stop_calls_service_stop(runner: CliRunner, mock_service: MagicMock) -> None:
    with patch("archon_search.cli.stop._get_service", return_value=mock_service):
        result = runner.invoke(main, ["stop"])
    assert result.exit_code == 0, result.output
    mock_service.stop.assert_called_once()


def test_stop_prints_stopped_message(runner: CliRunner, mock_service: MagicMock) -> None:
    with patch("archon_search.cli.stop._get_service", return_value=mock_service):
        result = runner.invoke(main, ["stop"])
    assert "stopped" in result.output.lower()


def test_stop_service_error_exits_nonzero(runner: CliRunner, mock_service: MagicMock) -> None:
    mock_service.stop.side_effect = RuntimeError("launchctl unload failed")
    with patch("archon_search.cli.stop._get_service", return_value=mock_service):
        result = runner.invoke(main, ["stop"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# _get_service platform dispatch (tests the helper directly)
# ---------------------------------------------------------------------------

def test_get_service_darwin_returns_launchd() -> None:
    from archon_search.cli._helpers import _get_service
    from archon_search.platform.macos import LaunchdSearchService

    with patch.object(sys, "platform", "darwin"):
        svc = _get_service()
    assert isinstance(svc, LaunchdSearchService)


def test_get_service_linux_returns_systemd() -> None:
    from archon_search.cli._helpers import _get_service
    from archon_search.platform.linux import SystemdSearchService

    with patch.object(sys, "platform", "linux"):
        svc = _get_service()
    assert isinstance(svc, SystemdSearchService)


def test_get_service_win32_returns_windows() -> None:
    from archon_search.cli._helpers import _get_service
    from archon_search.platform.windows import WindowsSearchService

    with patch.object(sys, "platform", "win32"):
        svc = _get_service()
    assert isinstance(svc, WindowsSearchService)


def test_get_service_unknown_platform_raises() -> None:
    from archon_search.cli._helpers import _get_service

    with patch.object(sys, "platform", "freebsd13"):
        with pytest.raises(NotImplementedError, match="Unsupported platform"):
            _get_service()


def test_get_service_returns_correct_type_for_current_platform() -> None:
    """_get_service() returns the platform-appropriate service type."""
    from archon_search.cli._helpers import _get_service
    from archon_search.platform.service import SearchServiceLifecycle

    svc = _get_service()
    assert isinstance(svc, SearchServiceLifecycle)

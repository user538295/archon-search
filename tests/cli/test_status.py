"""TDD tests for archon-search status CLI subcommand."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.platform.service import ServiceStatus


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_status_running_output(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=123, uptime_seconds=42.0)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output


def test_status_stopped_output(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=False, pid=None, uptime_seconds=None)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_status_running_includes_pid_and_uptime(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=456, uptime_seconds=99.0)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        result = runner.invoke(main, ["status"])
    assert "456" in result.output
    assert "99" in result.output


def test_status_running_no_pid_no_uptime(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output


def test_status_service_error_exits_nonzero(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.side_effect = RuntimeError("connection refused")
    with patch("archon_search.cli.status._get_service", return_value=svc):
        result = runner.invoke(main, ["status"])
    assert result.exit_code != 0
    assert "connection refused" in result.output

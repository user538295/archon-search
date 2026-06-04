"""Tests for the install and uninstall Click commands."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# install command — no config: should print guidance and exit non-zero
# ---------------------------------------------------------------------------


def test_install_cmd_no_config_exits_with_message(runner: CliRunner, tmp_path: Path) -> None:
    run_mock = MagicMock(return_value=1)
    with patch("archon_search.cli.install_cmd.SearchInstaller") as installer_cls:
        installer_cls.return_value.run_register_and_start = run_mock
        result = runner.invoke(main, ["install"])

    assert result.exit_code != 0


def test_install_cmd_with_config_calls_register_and_start(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text("[database]\n")

    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.SearchInstaller") as installer_cls:
        installer_cls.return_value.run_register_and_start = run_mock
        result = runner.invoke(main, ["install", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    run_mock.assert_called_once()


def test_install_cmd_dry_run_passes_through(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text("[database]\n")

    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.SearchInstaller") as installer_cls:
        installer_cls.return_value.run_register_and_start = run_mock
        result = runner.invoke(main, ["install", "--config", str(config_path), "--dry-run"])

    installer_cls.assert_called_once_with(config_file=str(config_path), dry_run=True)
    assert result.exit_code == 0, result.output


def test_install_cmd_wizard_options_are_rejected(runner: CliRunner) -> None:
    result = runner.invoke(main, ["install", "--profile", "minimal"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# uninstall command — stop and unregister are called
# ---------------------------------------------------------------------------


def test_uninstall_cmd_unchanged(runner: CliRunner, tmp_path: Path) -> None:
    mock_service = MagicMock()
    db_dir = tmp_path / "db"
    db_dir.mkdir()

    with (
        patch("archon_search.cli.install_cmd._get_service", return_value=mock_service),
        patch("archon_search.cli.install_cmd._get_db_path", return_value=db_dir),
    ):
        result = runner.invoke(main, ["uninstall", "--delete-db"])

    assert result.exit_code == 0, result.output
    mock_service.stop.assert_called_once()
    mock_service.unregister.assert_called_once()
    assert not db_dir.exists()

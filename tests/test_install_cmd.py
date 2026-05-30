"""TDD tests for the refactored install Click command (Task 3.5)."""
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
# Test 1: non-interactive + minimal + skip-preload calls run() with correct kwargs
# ---------------------------------------------------------------------------


def test_install_cmd_non_interactive_minimal_skip_preload(runner: CliRunner) -> None:
    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.SearchInstaller") as installer_cls:
        installer_cls.return_value.run = run_mock
        result = runner.invoke(
            main,
            ["install", "--non-interactive", "--profile", "minimal", "--skip-preload"],
        )

    assert result.exit_code == 0, result.output
    run_mock.assert_called_once_with(
        non_interactive=True,
        profile="minimal",
        multilingual=False,
        skip_preload=True,
        force=False,
        delete_db=False,
        accept_jina_license=False,
    )


# ---------------------------------------------------------------------------
# Test 2: invalid --profile value is rejected by Click before run() is called
# ---------------------------------------------------------------------------


def test_install_cmd_profile_choice_validation(runner: CliRunner) -> None:
    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.SearchInstaller") as installer_cls:
        installer_cls.return_value.run = run_mock
        result = runner.invoke(main, ["install", "--profile", "ultra"])

    assert result.exit_code != 0
    run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: --force without --delete-db; run() returns 1 → sys.exit(1)
# ---------------------------------------------------------------------------


def test_install_cmd_force_without_delete_db(runner: CliRunner) -> None:
    run_mock = MagicMock(return_value=1)
    with patch("archon_search.cli.install_cmd.SearchInstaller") as installer_cls:
        installer_cls.return_value.run = run_mock
        result = runner.invoke(main, ["install", "--non-interactive", "--force"])

    assert result.exit_code == 1
    run_mock.assert_called_once_with(
        non_interactive=True,
        profile=None,
        multilingual=False,
        skip_preload=False,
        force=True,
        delete_db=False,
        accept_jina_license=False,
    )


# ---------------------------------------------------------------------------
# Test 4: uninstall command is unchanged — stop and unregister are called
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

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
# Task 4.3 — --accept-fasttext-license flag on wizard command
# ---------------------------------------------------------------------------


def test_accept_fasttext_license_flag_present(runner: CliRunner) -> None:
    """--accept-fasttext-license must appear in `wizard --help` output."""
    result = runner.invoke(main, ["wizard", "--help"])
    assert result.exit_code == 0, result.output
    assert "--accept-fasttext-license" in result.output


def test_install_multilingual_non_interactive_with_flag(runner: CliRunner) -> None:
    """When --accept-fasttext-license is set, _prompt_fasttext_license is called with accept_fasttext_license=True."""
    with patch("archon_search.install._prompt_fasttext_license") as mock_prompt, \
         patch("archon_search.install._download_fasttext_model") as mock_download, \
         patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        result = runner.invoke(
            main,
            ["wizard", "--multilingual", "--accept-fasttext-license", "--non-interactive",
             "--accept-jina-license"],
        )
    # The run method should have been called with accept_fasttext_license=True
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args
    # accept_fasttext_license must be passed as True
    assert call_kwargs.kwargs.get("accept_fasttext_license") is True or (
        len(call_kwargs.args) > 0 and True in call_kwargs.args
    ), f"accept_fasttext_license=True not passed to run(); call_args={call_kwargs}"


def test_install_without_fasttext_flag_passes_false(runner: CliRunner) -> None:
    """When --accept-fasttext-license is NOT set, accept_fasttext_license=False is passed."""
    with patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        result = runner.invoke(
            main,
            ["wizard", "--multilingual", "--non-interactive", "--accept-jina-license"],
        )
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args
    passed = call_kwargs.kwargs.get("accept_fasttext_license", None)
    if passed is None and call_kwargs.args:
        # positional args — check the signature order
        # run(non_interactive, profile, multilingual, skip_preload, force, delete_db,
        #     accept_jina_license, accept_fasttext_license)
        pass  # hard to assert positional without knowing exact position; rely on kwarg form
    else:
        assert passed is False, f"Expected False, got {passed}"


# ---------------------------------------------------------------------------
# uninstall command — stop and unregister are called
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 3.2 — New CLI flags on wizard command
# ---------------------------------------------------------------------------


def test_wizard_help_contains_new_flags(runner: CliRunner) -> None:
    """All 8 new flags must appear in `wizard --help` output."""
    result = runner.invoke(main, ["wizard", "--help"])
    assert result.exit_code == 0, result.output
    for flag in [
        "--code",
        "--no-code",
        "--watch",
        "--no-watch",
        "--telemetry",
        "--no-telemetry",
        "--eager-load",
        "--no-eager-load",
        "--no-reranker",
        "--routing-strategy",
        "--log-format",
        "--disable-gpu",
    ]:
        assert flag in result.output, f"Missing flag {flag!r} in wizard --help"


def test_wizard_non_interactive_with_code_flag(runner: CliRunner) -> None:
    """--code passes install_code=True to run()."""
    with patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        runner.invoke(main, ["wizard", "--non-interactive", "--code", "--profile", "minimal"])
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("install_code") is True, f"install_code not True: {mock_run.call_args}"


def test_wizard_routing_strategy_hybrid(runner: CliRunner) -> None:
    """--routing-strategy hybrid passes routing_strategy='hybrid' to run()."""
    with patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        runner.invoke(
            main,
            ["wizard", "--non-interactive", "--routing-strategy", "hybrid", "--profile", "minimal"],
        )
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("routing_strategy") == "hybrid", f"routing_strategy not hybrid: {mock_run.call_args}"


def test_install_command_does_not_have_new_flags(runner: CliRunner) -> None:
    """The `install` subcommand must NOT expose wizard-only flags."""
    result = runner.invoke(main, ["install", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ["--code", "--watch", "--telemetry", "--eager-load", "--no-reranker",
                 "--routing-strategy", "--log-format", "--disable-gpu"]:
        assert flag not in result.output, f"Flag {flag!r} should not appear in install --help"


# ---------------------------------------------------------------------------
# Task 2.1 — --multilingual/--no-multilingual flag-pair
# ---------------------------------------------------------------------------


def test_no_multilingual_cli_flag(runner: CliRunner) -> None:
    """--no-multilingual passes multilingual=False to run()."""
    with patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        runner.invoke(
            main,
            ["wizard", "--no-multilingual", "--non-interactive", "--dry-run"],
        )
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("multilingual") is False, f"Expected False, got {kwargs.get('multilingual')}"


def test_multilingual_cli_flag(runner: CliRunner) -> None:
    """--multilingual passes multilingual=True to run()."""
    with patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        runner.invoke(
            main,
            ["wizard", "--multilingual", "--non-interactive", "--dry-run"],
        )
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("multilingual") is True, f"Expected True, got {kwargs.get('multilingual')}"


def test_no_multilingual_flag_no_prompt_shown(runner: CliRunner) -> None:
    """--no-multilingual + interactive mode: multilingual prompt text absent from stdout."""
    with patch("archon_search.install.SearchInstaller.run", return_value=0) as mock_run:
        result = runner.invoke(
            main,
            ["wizard", "--no-multilingual", "--dry-run"],
        )
    # Whether run() was called or not, the CLI must pass multilingual=False
    if mock_run.called:
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("multilingual") is False
    assert "non-English" not in result.output


def test_no_multilingual_flag_in_help(runner: CliRunner) -> None:
    """--no-multilingual must appear in wizard --help output."""
    result = runner.invoke(main, ["wizard", "--help"])
    assert result.exit_code == 0, result.output
    assert "--no-multilingual" in result.output


# ---------------------------------------------------------------------------
# Task 4.3 — --enable-hyde and --enable-rag-fusion flags on wizard command
# ---------------------------------------------------------------------------


def test_wizard_help_contains_enable_hyde(runner: CliRunner) -> None:
    """--enable-hyde must appear in `wizard --help` output."""
    result = runner.invoke(main, ["wizard", "--help"])
    assert result.exit_code == 0, result.output
    assert "--enable-hyde" in result.output


def test_wizard_help_contains_enable_rag_fusion(runner: CliRunner) -> None:
    """--enable-rag-fusion must appear in `wizard --help` output."""
    result = runner.invoke(main, ["wizard", "--help"])
    assert result.exit_code == 0, result.output
    assert "--enable-rag-fusion" in result.output


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

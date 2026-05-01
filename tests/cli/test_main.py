from click.testing import CliRunner

from archon_search.cli.main import main


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0


def test_unknown_command_exits_nonzero() -> None:
    result = CliRunner().invoke(main, ["bogus"])
    assert result.exit_code != 0

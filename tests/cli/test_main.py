from click.testing import CliRunner

from archon_search.cli.main import main


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0


def test_unknown_command_exits_nonzero() -> None:
    result = CliRunner().invoke(main, ["bogus"])
    assert result.exit_code != 0


def test_help_lists_all_subcommands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    for cmd in ("install", "uninstall", "start", "stop", "status", "ingest", "sync", "collection", "config"):
        assert cmd in result.output, f"{cmd!r} missing from --help output"

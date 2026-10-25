"""A weak ``ARCHON_SEARCH_API_KEY`` must reach the operator as one readable line, never a traceback."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main

_WEAK_KEY = "abc"
_EXPECTED_FRAGMENT = "ARCHON_SEARCH_API_KEY is not a valid API key"


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["key", "list"],
        ["jobs", "list"],
        ["maintenance", "status"],
        ["export", "docs"],
        ["ingest", "--collection", "docs", "--path", "/tmp/x"],
        ["sync"],
        ["collection", "list"],
    ],
    ids=lambda argv: "-".join(argv[:2]),
)
def test_client_commands_report_weak_key_in_one_line(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", _WEAK_KEY)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)

    result = CliRunner().invoke(main, argv)

    assert result.exit_code == 1, result.output
    assert _EXPECTED_FRAGMENT in result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    last_line = result.output.strip().splitlines()[-1]
    assert last_line.startswith("Error") and _EXPECTED_FRAGMENT in last_line, result.output

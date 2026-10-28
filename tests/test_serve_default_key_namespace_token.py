"""S806: a ``[namespaces]`` token equal to the default API key is refused with an error that names the namespace.

``serve`` must exit 1 with exactly one ``Error:`` line naming the namespace and the rule, never the token, never a
traceback (OperatorGuide/90_incident_runbook.md, SecurityGuide/02_authentication_and_keys.md).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main

DEFAULT_KEY = "ab" * 32
NAMESPACE = "team-token-ns"


def test_serve_refuses_a_namespace_token_equal_to_the_default_key_naming_the_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "archon-search.toml"
    config.write_text(f'[namespaces]\n"{DEFAULT_KEY}" = "{NAMESPACE}"\n')
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", DEFAULT_KEY)
    monkeypatch.delenv("ARCHON_SEARCH_KEY_FILE", raising=False)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(config))

    with patch("uvicorn.run"):
        result = CliRunner().invoke(main, ["serve"])

    error_lines = [line for line in result.stderr.splitlines() if line.startswith("Error:")]
    assert result.exit_code == 1, f"{result.exception!r}\n{result.output}"
    assert isinstance(result.exception, SystemExit), result.exception
    assert len(error_lines) == 1, result.stderr
    assert NAMESPACE in error_lines[0], error_lines[0]
    assert "equals the default API key" in error_lines[0], error_lines[0]
    assert DEFAULT_KEY not in result.output

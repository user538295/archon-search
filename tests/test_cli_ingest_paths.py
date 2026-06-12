"""Unit tests for the lazy history sessions path in ``archon-search ingest`` (C9 Task 2.6).

Replaces the hard-coded ``Path.home() / ".archon-search" / "history" / "sessions"``
default in ``archon_search/cli/ingest.py`` with ``get_data_dir() / "history" / "sessions"``
so ``ARCHON_SEARCH_DATA_DIR`` redirects the default ingest path at call time.

The path is computed inside the ``ingest`` Click command before ``load_config`` is
invoked, so the test mocks ``load_config`` to raise an exception immediately. The
default path is echoed via ``click.echo`` BEFORE that, letting the test assert on
``result.output`` (or ``result.stderr_bytes``) without spinning up the real
pipeline.

The autouse fixture in ``tests/conftest.py`` clears ``ARCHON_SEARCH_DATA_DIR``
between tests, so each test starts with a clean environment.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from archon_search.cli.ingest import ingest


def _invoke_ingest_capturing_default_path() -> str:
    """Invoke ``ingest`` with no ``--path``; force ``load_config`` to abort.

    The command echoes the default ingest path before calling ``load_config``.
    Mocking ``load_config`` to raise stops the command before any pipeline
    work, but the echoed default has already been written to stdout.

    Returns:
        Captured stdout containing the "No --path given, using default: ..."
        line.
    """
    runner = CliRunner()
    with patch(
        "archon_search.cli.ingest.load_config",
        side_effect=RuntimeError("stop here"),
    ):
        result = runner.invoke(ingest, [])
    # The command exits 1 because of the RuntimeError → SystemExit(1) path,
    # but the default-path echo happened before that.
    assert result.exit_code == 1, result.output
    return result.output


def test_default_history_path() -> None:
    """No env vars set → default ingest path is ``~/.archon-search/history/sessions``."""
    output = _invoke_ingest_capturing_default_path()
    expected = Path.home() / ".archon-search" / "history" / "sessions"
    assert str(expected) in output, (
        f"Expected default path {expected!s} in output, got: {output!r}"
    )


def test_history_path_uses_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARCHON_SEARCH_DATA_DIR="/data"`` → default ingest path is
    ``/data/history/sessions``."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
    output = _invoke_ingest_capturing_default_path()
    expected = Path("/data") / "history" / "sessions"
    assert str(expected) in output, (
        f"Expected default path {expected!s} in output, got: {output!r}"
    )


def test_history_path_reflects_env_change_between_invocations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two sequential CLI invocations resolve different defaults when
    ``ARCHON_SEARCH_DATA_DIR`` changes between them — pins the lazy contract for
    the ingest CLI (parity with Tasks 2.3/2.4/2.5 laziness tests)."""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(first_dir))
    first_output = _invoke_ingest_capturing_default_path()
    assert str(first_dir / "history" / "sessions") in first_output

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(second_dir))
    second_output = _invoke_ingest_capturing_default_path()
    assert str(second_dir / "history" / "sessions") in second_output


def test_history_path_propagates_invalid_env_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid ``ARCHON_SEARCH_DATA_DIR`` (relative path) propagates as
    ``ValueError`` from ``get_data_dir()`` — parity with Task 2.4 / 2.5
    error-propagation contracts.

    After Task 2.6, the path is composed *before* ``load_config`` is called,
    so the ``ValueError`` bubbles out of the command function and Click
    captures it on ``result.exception`` (no ``try/except`` around the
    ``ingest_path = get_data_dir() / ...`` line). We pin this exact path —
    asserting ``result.exception`` is a ``ValueError`` — rather than a loose
    "either surfaces" check, so a future refactor that swallows the error
    (or pushes it past ``load_config``) breaks the test.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "relative/not/absolute")
    runner = CliRunner()
    result = runner.invoke(ingest, [])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ValueError), (
        f"Expected ValueError to bubble from get_data_dir(), got "
        f"exception={result.exception!r}, output={result.output!r}"
    )
    assert "must be an absolute path" in str(result.exception), (
        f"Expected ValueError to mention 'must be an absolute path', "
        f"got: {result.exception!r}"
    )
    # The default-path echo line MUST NOT be printed — get_data_dir() raised
    # before reaching click.echo(). This pins the order of operations:
    # get_data_dir() is called first, then the echo, then load_config.
    assert "No --path given" not in result.output, (
        f"Expected no default-path echo (get_data_dir() should raise first), "
        f"got output: {result.output!r}"
    )

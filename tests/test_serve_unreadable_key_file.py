"""S805: ``serve`` refuses to start on a key file that exists but cannot be read, with one ``Error:`` line.

A key-file *directory* and a key file holding non-UTF-8 bytes are both "exists but unreadable": ``serve`` must exit 1,
never reach uvicorn, leave the path untouched (no regenerated key), and report the failure as a single ``Error:`` line
naming the key file — never a traceback (OperatorGuide/90_incident_runbook.md, UserManual/30_configuration.md).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main

NON_UTF8_BYTES = b"\xff\xfe\x00"


def _make_directory(key_file: Path) -> None:
    key_file.mkdir()
    (key_file / "keep.txt").write_text("keep")


def _make_non_utf8_file(key_file: Path) -> None:
    key_file.write_bytes(NON_UTF8_BYTES)


@pytest.mark.parametrize(
    "make_unreadable",
    [_make_directory, _make_non_utf8_file],
    ids=["directory", "non-utf8-bytes"],
)
def test_serve_refuses_an_unreadable_key_file_with_one_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_unreadable: Callable[[Path], None]
) -> None:
    key_file = tmp_path / ".search.env"
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_KEY_FILE", raising=False)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(tmp_path / "archon-search.toml"))
    make_unreadable(key_file)
    before = sorted(p.name for p in tmp_path.iterdir())  # ``logs/`` appears later: configure_logging creates it
    mode_before = key_file.stat().st_mode

    with patch("uvicorn.run") as uvicorn_run:
        result = CliRunner().invoke(main, ["serve"])

    error_lines = [line for line in result.stderr.splitlines() if line.startswith("Error:")]
    assert result.exit_code == 1, f"{result.exception!r}\n{result.output}"
    assert isinstance(result.exception, SystemExit), f"escaped as {result.exception!r}"
    assert "Traceback" not in result.stderr
    assert len(error_lines) == 1, result.stderr
    assert str(key_file) in error_lines[0]
    uvicorn_run.assert_not_called()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "logs") == before, "no backup, no new key file"
    if key_file.is_dir():
        assert key_file.stat().st_mode == mode_before, "a directory at the key path is reported, never chmod'ed"
        assert [p.name for p in key_file.iterdir()] == ["keep.txt"]
    else:
        assert key_file.read_bytes() == NON_UTF8_BYTES


def test_a_key_file_with_a_utf8_bom_keeps_its_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A BOM-prefixed file (Windows editors) still holds its key: it is read, never replaced by a generated one."""
    from archon_search.key_manager import load_or_generate_key

    key_file = tmp_path / ".search.env"
    key = "ab" * 32
    original = b"\xef\xbb\xbf" + f"ARCHON_SEARCH_API_KEY={key}\n".encode()
    key_file.write_bytes(original)
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))

    assert load_or_generate_key() == (key, f"file: {key_file}")
    assert key_file.read_bytes() == original

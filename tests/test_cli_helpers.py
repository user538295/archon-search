"""Tests for the shared CLI helpers in ``archon_search/cli/_helpers.py``."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.cli._helpers import _resolve_api_key
from archon_search.config import ConfigError


def test_resolve_api_key_arg_priority() -> None:
    """Explicit --api-key arg wins even when ARCHON_SEARCH_API_KEY env var is set."""
    with patch.dict("os.environ", {"ARCHON_SEARCH_API_KEY": "env-key"}), patch("archon_search.cli._helpers.load_key") as mock_load:
        result = _resolve_api_key("explicit-key")

    assert result == "explicit-key"
    mock_load.assert_not_called()


def test_resolve_api_key_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """None arg + ARCHON_SEARCH_API_KEY set → returns the key ``load_key`` resolves from the environment."""
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "env-key")

    with patch("archon_search.cli._helpers.load_key", return_value="env-key") as mock_load:
        result = _resolve_api_key(None)

    assert result == "env-key"
    mock_load.assert_called_once()


def test_resolve_api_key_file_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """None arg + no env var → falls back to the key file result of ``load_key()``."""
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)

    with patch("archon_search.cli._helpers.load_key", return_value="file-key") as mock_load:
        result = _resolve_api_key(None)

    assert result == "file-key"
    mock_load.assert_called_once()


def test_resolve_api_key_no_key_no_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """None arg + no env var + no key file → exits 1 without touching the filesystem.

    Regression for: PermissionError: [Errno 13] Permission denied: '/data'
    In a container, load_or_generate_key() tried to makedirs('/data') when
    no key exists — the CLI must never generate keys, only load them.
    """
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    # Point key file at a path inside tmp_path that does NOT exist — no key on disk.
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / "missing.env"))

    with pytest.raises(SystemExit) as exc:
        _resolve_api_key(None)

    assert exc.value.code == 1
    assert "No API key found. Pass --api-key, set ARCHON_SEARCH_API_KEY" in capsys.readouterr().err
    # Must not have written anything to disk.
    assert not any(tmp_path.iterdir())


def test_resolve_api_key_lets_config_error_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable ARCHON_SEARCH_API_KEY raises ConfigError; the helper must not swallow it into the no-key exit."""
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "not-a-valid-key")

    with pytest.raises(ConfigError):
        _resolve_api_key(None)

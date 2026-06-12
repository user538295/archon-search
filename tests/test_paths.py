"""Unit tests for archon_search.paths.get_data_dir() — single source of truth
for the base data directory.

Verifies env var override semantics, tilde expansion, whitespace handling,
trailing-slash normalisation, and the HOME-unset failure mode that matters
for misconfigured containers.

The autouse fixture in `tests/conftest.py` clears `ARCHON_SEARCH_DATA_DIR`
between tests, so individual tests can assume a clean environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.paths import get_data_dir


def test_default_returns_home_archon() -> None:
    """No env var set → fall back to ``Path.home() / ".archon-search"``."""
    assert get_data_dir() == Path.home() / ".archon-search"


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARCHON_SEARCH_DATA_DIR="/data"`` → ``Path("/data")``."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
    assert get_data_dir() == Path("/data")


def test_env_var_tilde_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``~`` in the env var must be expanded via ``Path.expanduser()``."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "~/mydata")
    expected = Path("~/mydata").expanduser()
    assert get_data_dir() == expected


def test_empty_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string for the env var is a misconfiguration — surface it loudly."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "")
    with pytest.raises(ValueError, match="must not be empty"):
        get_data_dir()


def test_whitespace_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only env var is also a misconfiguration."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "   ")
    with pytest.raises(ValueError, match="must not be empty"):
        get_data_dir()


def test_root_path_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/`` is a legitimate (if unusual) value — pathlib handles it."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/")
    assert get_data_dir() == Path("/")


def test_trailing_slash_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing slash on the env var is normalised away by ``pathlib``."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data/")
    assert get_data_dir() == Path("/data")


def test_home_unset_raises_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``Path.home()`` raises (HOME unset) AND no env var is set,
    surface a ``ValueError`` mentioning ``HOME is not set`` so the operator
    knows to set ``ARCHON_SEARCH_DATA_DIR``."""
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)

    def _raise_runtime_error() -> Path:
        raise RuntimeError("HOME is not set")

    monkeypatch.setattr(Path, "home", staticmethod(_raise_runtime_error))
    with pytest.raises(ValueError, match="HOME is not set"):
        get_data_dir()

"""Unit tests for archon_search.paths.get_data_dir() — single source of truth
for the base data directory.

Verifies env var override semantics, tilde expansion, whitespace handling,
trailing-slash normalisation, the "no side effects" guarantee, and the
HOME-unset failure mode that matters for misconfigured containers.

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
    """``~`` in the env var must be expanded — assert the tilde is gone and
    the result is rooted at ``Path.home()``, not that two ``expanduser()``
    calls happen to agree (which would pass even if expansion silently
    no-op'd)."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "~/mydata")
    result = get_data_dir()
    assert "~" not in str(result)
    assert result == Path.home() / "mydata"


def test_env_var_whitespace_padding_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leading/trailing whitespace from copy-paste must be stripped before
    Path construction, not silently baked into a path with literal spaces."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "  /data  ")
    assert get_data_dir() == Path("/data")


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


def test_relative_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative paths are rejected — CWD inside the container is not
    contractually stable, and silently CWD-dependent behavior is worse
    than a loud failure."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "data")
    with pytest.raises(ValueError, match="absolute path"):
        get_data_dir()


def test_root_path_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/`` is a legitimate (if unusual) value — pathlib handles it."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/")
    assert get_data_dir() == Path("/")


def test_trailing_slash_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing slash on the env var is normalised away by ``pathlib``."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data/")
    assert get_data_dir() == Path("/data")


def test_tilde_env_var_with_home_unset_raises_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ARCHON_SEARCH_DATA_DIR="~/x"`` + HOME unset → ``Path.expanduser``
    raises ``RuntimeError`` inside `get_data_dir`. The function must
    translate that into the documented ``ValueError`` so callers (and the
    Task 2.2 ``load_config()`` wrapper) only have to catch one exception
    type."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "~/mydata")

    def _raise(_self: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _raise)
    with pytest.raises(ValueError, match="HOME is not set"):
        get_data_dir()


@pytest.mark.archon_unset_data_dir
def test_home_unset_raises_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``Path.home()`` raises (HOME unset) AND no env var is set,
    surface a ``ValueError`` mentioning ``HOME is not set`` so the operator
    knows to set ``ARCHON_SEARCH_DATA_DIR``.

    `Path.home` is a classmethod in CPython, so the replacement is wrapped
    in ``classmethod(...)`` — patching with ``staticmethod`` happens to
    work via descriptor coincidence but is semantically wrong."""

    def _raise_runtime_error(cls: type[Path]) -> Path:
        raise RuntimeError("HOME is not set")

    monkeypatch.setattr(Path, "home", classmethod(_raise_runtime_error))
    with pytest.raises(ValueError, match="HOME is not set"):
        get_data_dir()


def test_does_not_create_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The plan's "no side effects" guarantee — `get_data_dir()` must not
    create the directory it returns, even when it's deeply nested. A future
    contributor adding `path.mkdir(parents=True, exist_ok=True)` would break
    the container bootstrap where ``/data`` is a mounted volume that may be
    read-only until the entrypoint prepares it."""
    target = tmp_path / "nonexistent" / "deeply" / "nested"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(target))
    result = get_data_dir()
    assert result == target
    assert not target.exists()

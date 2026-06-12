"""Tests for `ARCHON_SEARCH_HOST` / `ARCHON_SEARCH_PORT` env var overrides and `serve` kwarg.

Plan: Documentation/Backlog/C9-container-support-plan.md Task 1.1.

All env vars (`ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`, etc.)
are cleared by the `_clear_archon_env_vars` autouse fixture in `tests/conftest.py`,
so each test starts with a clean environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, SearchConfig, load_config


# ---------------------------------------------------------------------------
# ARCHON_SEARCH_HOST override
# ---------------------------------------------------------------------------


def test_host_env_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_HOST", "0.0.0.0")
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.host == "0.0.0.0"


def test_host_env_empty_string_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_HOST", "")
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.host == "127.0.0.1"


# ---------------------------------------------------------------------------
# ARCHON_SEARCH_PORT override
# ---------------------------------------------------------------------------


def test_port_env_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_PORT", "9000")
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.port == 9000


def test_port_env_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[server]\nport = 8000\n", encoding="utf-8")
    monkeypatch.setenv("ARCHON_SEARCH_PORT", "9000")
    config = load_config(path=toml_file)
    assert config.port == 9000


def test_port_env_invalid_non_int(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_PORT", "abc")
    with pytest.raises(ConfigError, match="integer"):
        load_config(path=tmp_path / "nonexistent.toml")


def test_port_env_invalid_out_of_range(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_PORT", "0")
    with pytest.raises(ConfigError, match="1 and 65535"):
        load_config(path=tmp_path / "nonexistent.toml")

    monkeypatch.setenv("ARCHON_SEARCH_PORT", "65536")
    with pytest.raises(ConfigError, match="1 and 65535"):
        load_config(path=tmp_path / "nonexistent.toml")

    monkeypatch.setenv("ARCHON_SEARCH_PORT", "-1")
    with pytest.raises(ConfigError, match="1 and 65535"):
        load_config(path=tmp_path / "nonexistent.toml")


def test_port_env_empty_string_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_PORT", "")
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.port == 8765


# ---------------------------------------------------------------------------
# `serve=True` kwarg
# ---------------------------------------------------------------------------


def test_serve_kwarg_sets_default_host(tmp_path: Path) -> None:
    """`serve=True` with no env/TOML → host defaults to `0.0.0.0`."""
    config = load_config(tmp_path / "nonexistent.toml", serve=True)
    assert config.host == "0.0.0.0"


def test_serve_kwarg_overridable_by_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`serve=True` with `ARCHON_SEARCH_HOST` set → env var wins."""
    monkeypatch.setenv("ARCHON_SEARCH_HOST", "192.168.1.1")
    config = load_config(tmp_path / "nonexistent.toml", serve=True)
    assert config.host == "192.168.1.1"


def test_serve_kwarg_overridable_by_toml(tmp_path: Path) -> None:
    """`serve=True` with TOML host → TOML wins. Positional argument required (`path`)."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[server]\nhost = \"10.0.0.5\"\n", encoding="utf-8")
    config = load_config(toml_file, serve=True)
    assert config.host == "10.0.0.5"


# ---------------------------------------------------------------------------
# `load_config()` baseline: defaults are still produced with zero env / no TOML
# (sanity check that the autouse env-clearing fixture works).
# ---------------------------------------------------------------------------


def test_defaults_when_env_cleared(tmp_path: Path) -> None:
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert isinstance(config, SearchConfig)
    assert config.host == "127.0.0.1"
    assert config.port == 8765

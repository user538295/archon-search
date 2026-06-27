"""Tests for top_k_max in SearchConfig — E0c BE-2.

TDD: tests written first; implementation in config.py follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, SearchConfig, load_config


@pytest.fixture
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that could influence config loading."""
    monkeypatch.delenv("ARCHON_SEARCH_HOST", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_PORT", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)


def test_top_k_max_default_is_100() -> None:
    """SearchConfig().top_k_max must default to 100."""
    assert SearchConfig().top_k_max == 100


def test_top_k_max_loaded_from_toml(_no_env: None, tmp_path: Path) -> None:
    """TOML [search].top_k_max = 200 is loaded into config.top_k_max."""
    toml_content = "[search]\ntop_k_max = 200\n"
    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=toml_path)
    assert config.top_k_max == 200


def test_top_k_max_boundary_one_loads(_no_env: None, tmp_path: Path) -> None:
    """TOML [search].top_k_max = 1 is the minimum valid value and loads successfully."""
    toml_content = "[search]\ntop_k_max = 1\n"
    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=toml_path)
    assert config.top_k_max == 1


def test_top_k_max_zero_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """TOML [search].top_k_max = 0 raises ConfigError (must be > 0)."""
    toml_content = "[search]\ntop_k_max = 0\n"
    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="top_k_max"):
        load_config(path=toml_path)


def test_top_k_max_negative_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """TOML [search].top_k_max = -1 raises ConfigError (must be > 0)."""
    toml_content = "[search]\ntop_k_max = -1\n"
    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="top_k_max"):
        load_config(path=toml_path)

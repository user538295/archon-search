"""Tests for the [search] request ceilings in SearchConfig: top_k_max (E0c BE-2) and context_window_max.

TDD: tests written first; implementation in config.py follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, SearchConfig, load_config
from archon_search.constants import DEFAULT_CONTEXT_WINDOW


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


# ---------------------------------------------------------------------------
# [search].context_window_max — ceiling on MCP search_with_context's context_window
# ---------------------------------------------------------------------------


def _load_search_toml(tmp_path: Path, body: str) -> SearchConfig:
    toml_path = tmp_path / "archon-search.toml"
    toml_path.write_text(f"[search]\n{body}\n", encoding="utf-8")
    return load_config(path=toml_path)


def test_context_window_max_default_is_10() -> None:
    """SearchConfig().context_window_max must default to 10."""
    assert SearchConfig().context_window_max == 10


def test_context_window_max_loaded_from_toml(_no_env: None, tmp_path: Path) -> None:
    """TOML [search].context_window_max = 25 is loaded into config.context_window_max."""
    assert _load_search_toml(tmp_path, "context_window_max = 25").context_window_max == 25


def test_context_window_max_boundary_one_loads(_no_env: None, tmp_path: Path) -> None:
    """1 is the minimum valid value: it still admits the tool's default context_window of 1."""
    assert _load_search_toml(tmp_path, "context_window_max = 1").context_window_max == 1


@pytest.mark.parametrize("raw", ["0", "-1"], ids=["zero", "negative"])
def test_context_window_max_below_one_raises_config_error(_no_env: None, tmp_path: Path, raw: str) -> None:
    """A ceiling below the tool's default context_window of 1 would reject every default call."""
    with pytest.raises(ConfigError, match=rf"^context_window_max must be >= {DEFAULT_CONTEXT_WINDOW}, got {raw}$"):
        _load_search_toml(tmp_path, f"context_window_max = {raw}")


@pytest.mark.parametrize(
    ("raw", "type_name"),
    [("true", "bool"), ("2.5", "Float"), ('"ten"', "String")],
    ids=["bool", "fractional", "non_numeric"],
)
def test_context_window_max_non_integer_raises_config_error(
    _no_env: None, tmp_path: Path, raw: str, type_name: str
) -> None:
    """Booleans, fractional and non-numeric values fail the integer coercion, like top_k_max."""
    with pytest.raises(ConfigError, match=rf"^Expected integer for 'context_window_max', got {type_name}$"):
        _load_search_toml(tmp_path, f"context_window_max = {raw}")

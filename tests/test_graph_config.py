"""Unit tests for GraphConfig — BE-1 of E1a (GraphRAG entity extraction).

These tests cover:
- Default field values for `GraphConfig`
- TOML `[graph]` section parsing into `GraphConfig`
- Snapshot of the `graph` field in `test_config_defaults.py` (via `SearchConfig`)
- Validation: `backend_threshold_edges = true` raises `ConfigError` (bool rejected)
- Validation: `extraction_model = ""` raises `ConfigError` (empty string rejected)
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest

from archon_search.config import ConfigError, GraphConfig, SearchConfig, load_config


# ---------------------------------------------------------------------------
# Default value assertions
# ---------------------------------------------------------------------------


def test_graph_config_defaults() -> None:
    """GraphConfig defaults: enabled=False, extraction_model=None, backend_threshold_edges=10_000."""
    cfg = GraphConfig()
    assert cfg.enabled is False
    assert cfg.extraction_model is None
    assert cfg.backend_threshold_edges == 10_000


def test_search_config_has_graph_field() -> None:
    """SearchConfig must carry a `graph` field that defaults to GraphConfig()."""
    config = SearchConfig()
    assert isinstance(config.graph, GraphConfig)
    assert config.graph.enabled is False
    assert config.graph.extraction_model is None
    assert config.graph.backend_threshold_edges == 10_000


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, content: str) -> Path:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return toml_file


def test_graph_config_toml_loading(tmp_path: Path) -> None:
    """TOML [graph] section parsed correctly into GraphConfig fields."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        enabled = true
        extraction_model = "claude-haiku-4-5-20251001"
        backend_threshold_edges = 5000
        """,
    )
    config = load_config(path=toml)
    assert config.graph.enabled is True
    assert config.graph.extraction_model == "claude-haiku-4-5-20251001"
    assert config.graph.backend_threshold_edges == 5000


def test_graph_config_toml_enabled_only(tmp_path: Path) -> None:
    """Only setting enabled=true leaves other fields at defaults."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        enabled = true
        """,
    )
    config = load_config(path=toml)
    assert config.graph.enabled is True
    assert config.graph.extraction_model is None
    assert config.graph.backend_threshold_edges == 10_000


def test_graph_config_snapshot(tmp_path: Path) -> None:
    """SearchConfig dataclass snapshot includes graph field with expected defaults."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    as_dict = dataclasses.asdict(config)
    assert "graph" in as_dict
    assert as_dict["graph"] == {
        "enabled": False,
        "extraction_model": None,
        "backend_threshold_edges": 10_000,
    }


# ---------------------------------------------------------------------------
# Validation: invalid values raise ConfigError
# ---------------------------------------------------------------------------


def test_backend_threshold_edges_rejects_bool(tmp_path: Path) -> None:
    """TOML backend_threshold_edges = true raises ConfigError (bool rejected as int)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        backend_threshold_edges = true
        """,
    )
    with pytest.raises(ConfigError, match="backend_threshold_edges"):
        load_config(path=toml)


def test_graph_config_extraction_model_rejects_empty_string(tmp_path: Path) -> None:
    """extraction_model="" raises ConfigError at config parse time."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        extraction_model = ""
        """,
    )
    with pytest.raises(ConfigError, match="extraction_model"):
        load_config(path=toml)


def test_backend_threshold_edges_accepts_minimum(tmp_path: Path) -> None:
    """backend_threshold_edges = 1 (minimum valid value) must be accepted."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        backend_threshold_edges = 1
        """,
    )
    config = load_config(path=toml)
    assert config.graph.backend_threshold_edges == 1


def test_backend_threshold_edges_rejects_float(tmp_path: Path) -> None:
    """backend_threshold_edges = 10000.5 raises ConfigError (float, not integer)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        backend_threshold_edges = 10000.5
        """,
    )
    with pytest.raises(ConfigError, match="backend_threshold_edges"):
        load_config(path=toml)


def test_backend_threshold_edges_rejects_negative(tmp_path: Path) -> None:
    """backend_threshold_edges < 1 raises ConfigError."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        backend_threshold_edges = 0
        """,
    )
    with pytest.raises(ConfigError, match="backend_threshold_edges"):
        load_config(path=toml)


def test_graph_enabled_rejects_non_bool(tmp_path: Path) -> None:
    """graph.enabled with a non-boolean value raises ConfigError."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        enabled = "yes"
        """,
    )
    with pytest.raises(ConfigError, match="enabled"):
        load_config(path=toml)

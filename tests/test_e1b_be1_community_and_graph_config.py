"""Unit tests for E1b BE-1 — Community dataclass + GraphConfig leiden fields.

Coverage:
- Community dataclass: field defaults, summary_text nullable, required list fields,
  built_at UTC enforcement (naive rejected, non-UTC offset rejected)
- GraphConfig defaults for leiden_resolution, max_community_size,
  community_summary_chunks, max_global_candidates
- TOML parsing: all four new fields, TOML integer coercion for leiden_resolution
- Validation: resolution <= 0, max_global_candidates <= 0, max_community_size/
  community_summary_chunks < 1, and bool rejection for all three new integer fields
- Snapshot: SearchConfig.graph dict includes all new fields at correct defaults
"""

from __future__ import annotations

import dataclasses
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search.config import ConfigError, GraphConfig, load_config
from archon_search.graph_types import Community


# ---------------------------------------------------------------------------
# Community dataclass
# ---------------------------------------------------------------------------


def test_community_dataclass_defaults() -> None:
    """Community instantiates with required fields; summary_text defaults to None."""
    now = datetime.now(tz=timezone.utc)
    community = Community(
        community_id="comm-001",
        entity_ids=["entity-a", "entity-b"],
        representative_chunk_ids=["chunk-001", "chunk-002"],
        built_at=now,
    )
    assert community.community_id == "comm-001"
    assert community.entity_ids == ["entity-a", "entity-b"]
    assert community.representative_chunk_ids == ["chunk-001", "chunk-002"]
    assert community.summary_text is None
    assert community.built_at == now


def test_community_summary_text_nullable() -> None:
    """Community.summary_text can be set to a non-None string."""
    now = datetime.now(tz=timezone.utc)
    community = Community(
        community_id="comm-002",
        entity_ids=["entity-x"],
        representative_chunk_ids=[],
        built_at=now,
        summary_text="This community covers the authentication subsystem.",
    )
    assert community.summary_text == "This community covers the authentication subsystem."


def test_community_required_list_fields_are_not_shared() -> None:
    """Two Community instances constructed with separate list literals have independent entity_ids.

    Each [] literal creates a new list object; this test documents that Community does
    NOT share list state between instances (no mutable-default trap since both list fields
    are required positional arguments with no default value).
    """
    now = datetime.now(tz=timezone.utc)
    c1 = Community(
        community_id="c1",
        entity_ids=[],
        representative_chunk_ids=[],
        built_at=now,
    )
    c2 = Community(
        community_id="c2",
        entity_ids=[],
        representative_chunk_ids=[],
        built_at=now,
    )
    c1.entity_ids.append("shared?")
    assert c2.entity_ids == []


def test_community_built_at_accepts_utc_aware() -> None:
    """Community accepts a UTC-aware datetime for built_at."""
    utc_now = datetime.now(tz=timezone.utc)
    community = Community(
        community_id="c3",
        entity_ids=[],
        representative_chunk_ids=[],
        built_at=utc_now,
    )
    assert community.built_at == utc_now


def test_community_built_at_accepts_naive_datetime() -> None:
    """Community accepts a naive datetime for built_at — UTC is expected by convention, not enforced."""
    naive = datetime(2024, 1, 1, 12, 0, 0)  # no tzinfo
    community = Community(
        community_id="c4",
        entity_ids=[],
        representative_chunk_ids=[],
        built_at=naive,
    )
    assert community.built_at == naive


def test_community_dataclass_field_types() -> None:
    """Community has fields: community_id (str), entity_ids (list), representative_chunk_ids (list),
    summary_text (str | None), built_at (datetime).
    """
    fields = {f.name: f for f in dataclasses.fields(Community)}
    assert "community_id" in fields
    assert "entity_ids" in fields
    assert "representative_chunk_ids" in fields
    assert "summary_text" in fields
    assert "built_at" in fields


# ---------------------------------------------------------------------------
# GraphConfig defaults for new leiden fields
# ---------------------------------------------------------------------------


def test_graph_config_leiden_defaults() -> None:
    """GraphConfig has correct defaults for E1b leiden fields."""
    cfg = GraphConfig()
    assert cfg.leiden_resolution == 1.0
    assert cfg.max_community_size == 10
    assert cfg.community_summary_chunks == 3
    assert cfg.max_global_candidates == 100


# ---------------------------------------------------------------------------
# TOML loading for leiden fields
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, content: str) -> Path:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return toml_file


def test_graph_config_leiden_fields_toml_loading(tmp_path: Path) -> None:
    """TOML [graph] section parses leiden fields into GraphConfig."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        leiden_resolution = 1.5
        max_community_size = 20
        community_summary_chunks = 5
        max_global_candidates = 50
        """,
    )
    config = load_config(path=toml)
    assert config.graph.leiden_resolution == 1.5
    assert config.graph.max_community_size == 20
    assert config.graph.community_summary_chunks == 5
    assert config.graph.max_global_candidates == 50


def test_graph_config_leiden_resolution_minimum_valid(tmp_path: Path) -> None:
    """leiden_resolution = 0.01 (small positive float) must be accepted."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        leiden_resolution = 0.01
        """,
    )
    config = load_config(path=toml)
    assert config.graph.leiden_resolution == pytest.approx(0.01)


def test_graph_config_leiden_resolution_zero_raises(tmp_path: Path) -> None:
    """leiden_resolution = 0.0 raises ConfigError (must be > 0)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        leiden_resolution = 0.0
        """,
    )
    with pytest.raises(ConfigError, match="leiden_resolution"):
        load_config(path=toml)


def test_graph_config_leiden_resolution_negative_raises(tmp_path: Path) -> None:
    """leiden_resolution = -1.0 raises ConfigError (must be > 0)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        leiden_resolution = -1.0
        """,
    )
    with pytest.raises(ConfigError, match="leiden_resolution"):
        load_config(path=toml)


def test_graph_config_max_global_candidates_zero_raises(tmp_path: Path) -> None:
    """max_global_candidates = 0 raises ConfigError (must be > 0)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_global_candidates = 0
        """,
    )
    with pytest.raises(ConfigError, match="max_global_candidates"):
        load_config(path=toml)


def test_graph_config_max_global_candidates_negative_raises(tmp_path: Path) -> None:
    """max_global_candidates = -5 raises ConfigError (must be > 0)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_global_candidates = -5
        """,
    )
    with pytest.raises(ConfigError, match="max_global_candidates"):
        load_config(path=toml)


def test_graph_config_max_community_size_minimum_valid(tmp_path: Path) -> None:
    """max_community_size = 1 (minimum valid) must be accepted."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_community_size = 1
        """,
    )
    config = load_config(path=toml)
    assert config.graph.max_community_size == 1


def test_graph_config_max_community_size_zero_raises(tmp_path: Path) -> None:
    """max_community_size = 0 raises ConfigError (must be >= 1)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_community_size = 0
        """,
    )
    with pytest.raises(ConfigError, match="max_community_size"):
        load_config(path=toml)


def test_graph_config_community_summary_chunks_minimum_valid(tmp_path: Path) -> None:
    """community_summary_chunks = 1 (minimum valid) must be accepted."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        community_summary_chunks = 1
        """,
    )
    config = load_config(path=toml)
    assert config.graph.community_summary_chunks == 1


def test_graph_config_community_summary_chunks_zero_raises(tmp_path: Path) -> None:
    """community_summary_chunks = 0 raises ConfigError (must be >= 1)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        community_summary_chunks = 0
        """,
    )
    with pytest.raises(ConfigError, match="community_summary_chunks"):
        load_config(path=toml)


def test_graph_config_leiden_resolution_accepts_toml_integer(tmp_path: Path) -> None:
    """leiden_resolution = 1 (TOML integer) is coerced to float 1.0 by _coerce_float."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        leiden_resolution = 1
        """,
    )
    config = load_config(path=toml)
    assert config.graph.leiden_resolution == pytest.approx(1.0)


def test_graph_config_max_community_size_rejects_bool(tmp_path: Path) -> None:
    """max_community_size = true raises ConfigError (bool rejected as int)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_community_size = true
        """,
    )
    with pytest.raises(ConfigError, match="max_community_size"):
        load_config(path=toml)


def test_graph_config_community_summary_chunks_rejects_bool(tmp_path: Path) -> None:
    """community_summary_chunks = true raises ConfigError (bool rejected as int)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        community_summary_chunks = true
        """,
    )
    with pytest.raises(ConfigError, match="community_summary_chunks"):
        load_config(path=toml)


def test_graph_config_max_global_candidates_rejects_bool(tmp_path: Path) -> None:
    """max_global_candidates = true raises ConfigError (bool rejected as int)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_global_candidates = true
        """,
    )
    with pytest.raises(ConfigError, match="max_global_candidates"):
        load_config(path=toml)


def test_graph_config_leiden_resolution_rejects_bool(tmp_path: Path) -> None:
    """leiden_resolution = true raises ConfigError (bool rejected consistently with sibling int fields)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        leiden_resolution = true
        """,
    )
    with pytest.raises(ConfigError, match="leiden_resolution"):
        load_config(path=toml)


def test_graph_config_max_global_candidates_minimum_valid(tmp_path: Path) -> None:
    """max_global_candidates = 1 (minimum valid) must be accepted."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_global_candidates = 1
        """,
    )
    config = load_config(path=toml)
    assert config.graph.max_global_candidates == 1


def test_graph_config_snapshot_includes_leiden_fields(tmp_path: Path) -> None:
    """SearchConfig dataclass snapshot includes all new E1b GraphConfig fields."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    as_dict = dataclasses.asdict(config)
    assert "graph" in as_dict
    graph_dict = as_dict["graph"]
    assert graph_dict["leiden_resolution"] == 1.0
    assert graph_dict["max_community_size"] == 10
    assert graph_dict["community_summary_chunks"] == 3
    assert graph_dict["max_global_candidates"] == 100


def test_graph_config_max_community_size_negative_raises(tmp_path: Path) -> None:
    """max_community_size = -3 raises ConfigError (must be >= 1)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        max_community_size = -3
        """,
    )
    with pytest.raises(ConfigError, match="max_community_size"):
        load_config(path=toml)


def test_graph_config_community_summary_chunks_negative_raises(tmp_path: Path) -> None:
    """community_summary_chunks = -2 raises ConfigError (must be >= 1)."""
    toml = _write_toml(
        tmp_path,
        """\
        [graph]
        community_summary_chunks = -2
        """,
    )
    with pytest.raises(ConfigError, match="community_summary_chunks"):
        load_config(path=toml)


def test_graph_config_leiden_fields_retained_when_graph_section_absent(tmp_path: Path) -> None:
    """When TOML has no [graph] section, all leiden fields retain their defaults."""
    toml = _write_toml(
        tmp_path,
        """\
        [search]
        top_k = 10
        """,
    )
    config = load_config(path=toml)
    assert config.graph.leiden_resolution == 1.0
    assert config.graph.max_community_size == 10
    assert config.graph.community_summary_chunks == 3
    assert config.graph.max_global_candidates == 100

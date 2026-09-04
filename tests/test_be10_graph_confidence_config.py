"""Tests for BE-10: ner_confidence, relation_confidence, providers in GraphConfig (C5, S43, S47)."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, GraphConfig, load_config


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_graphConfig_nerConfidence_default() -> None:
    assert GraphConfig().ner_confidence == 0.5


def test_graphConfig_relationConfidence_default() -> None:
    assert GraphConfig().relation_confidence == 0.75


def test_graphConfig_providers_default_is_none() -> None:
    assert GraphConfig().providers is None


# ---------------------------------------------------------------------------
# S47 — below-zero / zero / above-one / boolean / non-numeric, unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["ner_confidence", "relation_confidence"])
@pytest.mark.parametrize("bad_value", [-0.1, 0.0, 1.5], ids=["below_zero", "zero", "above_one"])
def test_below_zero_zero_and_above_one_each_raise_naming_the_key(
    tmp_path: Path, key: str, bad_value: float
) -> None:
    """Separate test cases, not one `or` — below-zero, zero, and above-one are
    each individually confirmed to raise, naming the offending key, so a failure
    in one boundary does not mask the others."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(f"[graph]\n{key} = {bad_value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"{key}.*must be in"):
        load_config(path=toml_file, serve=False)


@pytest.mark.parametrize("key", ["ner_confidence", "relation_confidence"])
def test_boolean_true_rejected_before_coercion(tmp_path: Path, key: str) -> None:
    """`true` is not silently coerced to 1.0 — it must raise, not pass range validation."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(f"[graph]\n{key} = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"{key}.*bool"):
        load_config(path=toml_file, serve=False)


@pytest.mark.parametrize("key", ["ner_confidence", "relation_confidence"])
def test_non_numeric_value_raises_config_error(tmp_path: Path, key: str) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(f'[graph]\n{key} = "not-a-number"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"{key}.*got String"):
        load_config(path=toml_file, serve=False)


@pytest.mark.parametrize("key", ["ner_confidence", "relation_confidence"])
def test_upper_bound_inclusive_one_is_valid(tmp_path: Path, key: str) -> None:
    """(0.0, 1.0] is half-open — 1.0 itself is a valid value, not rejected."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(f"[graph]\n{key} = 1.0\n", encoding="utf-8")
    cfg = load_config(path=toml_file, serve=False)
    assert getattr(cfg.graph, key) == 1.0


@pytest.mark.parametrize("key", ["ner_confidence", "relation_confidence"])
def test_just_above_lower_bound_is_valid(tmp_path: Path, key: str) -> None:
    """(0.0, 1.0] is exclusive at the lower bound — a value just above 0.0 is accepted."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(f"[graph]\n{key} = 0.001\n", encoding="utf-8")
    cfg = load_config(path=toml_file, serve=False)
    assert getattr(cfg.graph, key) == 0.001


# ---------------------------------------------------------------------------
# S43 — adjacency_threshold is not a recognised key
# ---------------------------------------------------------------------------


def test_adjacency_threshold_is_not_a_recognised_key(tmp_path: Path) -> None:
    """A value under this name in [graph] has no effect — it's not parsed into GraphConfig
    at all, and it must not be treated as an unknown-key error either."""
    assert not hasattr(GraphConfig(), "adjacency_threshold")

    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nadjacency_threshold = 0.6\n", encoding="utf-8")
    cfg = load_config(path=toml_file, serve=False)
    assert not hasattr(cfg.graph, "adjacency_threshold")


# ---------------------------------------------------------------------------
# providers — no inheritance from [database].providers
# ---------------------------------------------------------------------------


def test_unset_graph_providers_reads_as_cpu(tmp_path: Path) -> None:
    """Unlike resolve_reranker_providers, [graph].providers is never inherited from
    [database].providers — an unset [graph].providers reads as CPU (None) even when
    [database].providers is set."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[database]\nproviders = ["CoreMLExecutionProvider"]\n', encoding="utf-8"
    )
    cfg = load_config(path=toml_file, serve=False)
    assert cfg.graph.providers is None
    assert cfg.providers == ["CoreMLExecutionProvider"]


def test_graph_providers_parsed_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nproviders = ["CPUExecutionProvider"]\n', encoding="utf-8")
    cfg = load_config(path=toml_file, serve=False)
    assert cfg.graph.providers == ["CPUExecutionProvider"]


def test_confidence_fields_loaded_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[graph]\nner_confidence = 0.6\nrelation_confidence = 0.8\n", encoding="utf-8"
    )
    cfg = load_config(path=toml_file, serve=False)
    assert cfg.graph.ner_confidence == 0.6
    assert cfg.graph.relation_confidence == 0.8


# ---------------------------------------------------------------------------
# Malformed [graph].providers — must raise ConfigError, not TypeError (C1-I-1/23/41)
# ---------------------------------------------------------------------------


def test_graph_providers_bare_string_raises_config_error(tmp_path: Path) -> None:
    """A bare string must not be silently iterated into one-character elements."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nproviders = "CPUExecutionProvider"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="providers"):
        load_config(path=toml_file, serve=False)


def test_graph_providers_non_list_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nproviders = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="providers"):
        load_config(path=toml_file, serve=False)


def test_graph_providers_non_string_element_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nproviders = [1]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="providers"):
        load_config(path=toml_file, serve=False)


def test_graph_providers_empty_string_element_raises_config_error(tmp_path: Path) -> None:
    """An empty-string element is falsy and must be rejected like any other malformed entry."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nproviders = [""]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="providers"):
        load_config(path=toml_file, serve=False)


def test_graph_providers_empty_list_normalizes_to_none(tmp_path: Path) -> None:
    """Mirrors resolve_reranker_providers's [] -> None normalization."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nproviders = []\n", encoding="utf-8")
    cfg = load_config(path=toml_file, serve=False)
    assert cfg.graph.providers is None

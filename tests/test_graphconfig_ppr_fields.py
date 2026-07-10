"""Tests for BE-1: ppr_damping, ppr_top_entities, naive_max_expansion_terms in GraphConfig."""
import pytest
from pathlib import Path

from archon_search.config import ConfigError, GraphConfig, load_config


# ---------------------------------------------------------------------------
# Unit tests — no marks needed
# ---------------------------------------------------------------------------


def test_graphConfig_pprDamping_default() -> None:
    assert GraphConfig().ppr_damping == 0.85


def test_graphConfig_pprTopEntities_default() -> None:
    assert GraphConfig().ppr_top_entities == 20


def test_graphConfig_naiveMaxExpansionTerms_default() -> None:
    assert GraphConfig().naive_max_expansion_terms == 20


def test_graphConfig_pprDamping_outOfRange_raisesConfigError(
    tmp_path: Path,
) -> None:
    """damping <= 0 or >= 1 must raise ConfigError at config-load time."""
    toml_file = tmp_path / "archon-search.toml"

    for bad_value in (0.0, -0.1, 1.0, 1.5):
        toml_file.write_text(f"[graph]\nppr_damping = {bad_value}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="ppr_damping"):
            load_config(path=toml_file, serve=False)


def test_graphConfig_pprTopEntities_zero_raisesConfigError(
    tmp_path: Path,
) -> None:
    """ppr_top_entities <= 0 must raise ConfigError at config-load time."""
    toml_file = tmp_path / "archon-search.toml"

    for bad_value in (0, -1, -10):
        toml_file.write_text(
            f"[graph]\nppr_top_entities = {bad_value}\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="ppr_top_entities"):
            load_config(path=toml_file, serve=False)


def test_graphConfig_naiveMaxExpansionTerms_zero_raisesConfigError(
    tmp_path: Path,
) -> None:
    """naive_max_expansion_terms <= 0 must raise ConfigError at config-load time."""
    toml_file = tmp_path / "archon-search.toml"

    for bad_value in (0, -1):
        toml_file.write_text(
            f"[graph]\nnaive_max_expansion_terms = {bad_value}\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="naive_max_expansion_terms"):
            load_config(path=toml_file, serve=False)


def test_graphConfig_pprDamping_bool_raisesConfigError(
    tmp_path: Path,
) -> None:
    """ppr_damping = true (bool) must raise ConfigError at config-load time."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nppr_damping = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ppr_damping"):
        load_config(path=toml_file, serve=False)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_config_pprFields_loadedFromToml(
    tmp_path: Path,
) -> None:
    """TOML [graph] ppr_damping=0.9 is loaded into GraphConfig.ppr_damping."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[graph]\nppr_damping = 0.9\nppr_top_entities = 30\nnaive_max_expansion_terms = 50\n",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_file, serve=False)
    assert cfg.graph.ppr_damping == 0.9
    assert cfg.graph.ppr_top_entities == 30
    assert cfg.graph.naive_max_expansion_terms == 50


@pytest.mark.integration
def test_config_pprDamping_outOfRange_rejectsAtStartup(
    tmp_path: Path,
) -> None:
    """TOML with [graph] ppr_damping=1.5 raises ConfigError at load time."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nppr_damping = 1.5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ppr_damping"):
        load_config(path=toml_file, serve=False)

"""BE-1 (G9 OpenAI shim): Unit tests for OpenAIShimConfig and SearchConfig.openai_shim.

TDD: these tests are written before the implementation and must fail first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import OpenAIShimConfig, SearchConfig, load_config


# ---------------------------------------------------------------------------
# OpenAIShimConfig dataclass defaults
# ---------------------------------------------------------------------------


def test_openai_shim_config_defaults() -> None:
    """OpenAIShimConfig() has enabled=False, inject_citations=True, top_k=5."""
    cfg = OpenAIShimConfig()
    assert cfg.enabled is False
    assert cfg.inject_citations is True
    assert cfg.top_k == 5


# ---------------------------------------------------------------------------
# SearchConfig default
# ---------------------------------------------------------------------------


def test_openai_shim_disabled_by_default(tmp_path: Path) -> None:
    """SearchConfig() default has openai_shim.enabled = False."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.openai_shim.enabled is False


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def test_openai_shim_toml_parse(tmp_path: Path) -> None:
    """[openai_shim] TOML block is parsed correctly into SearchConfig.openai_shim."""
    toml = tmp_path / "archon-search.toml"
    toml.write_text(
        "[openai_shim]\nenabled = true\ninject_citations = false\ntop_k = 10\n",
        encoding="utf-8",
    )
    config = load_config(path=toml)
    assert config.openai_shim.enabled is True
    assert config.openai_shim.inject_citations is False
    assert config.openai_shim.top_k == 10


def test_openai_shim_toml_partial_parse(tmp_path: Path) -> None:
    """Partial [openai_shim] block keeps unset fields at their defaults."""
    toml = tmp_path / "archon-search.toml"
    toml.write_text("[openai_shim]\nenabled = true\n", encoding="utf-8")
    config = load_config(path=toml)
    assert config.openai_shim.enabled is True
    assert config.openai_shim.inject_citations is True
    assert config.openai_shim.top_k == 5


# ---------------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------------


def test_openai_shim_enabled_must_be_bool(tmp_path: Path) -> None:
    """[openai_shim].enabled must be a boolean; other types raise ConfigError."""
    from archon_search.config import ConfigError

    toml = tmp_path / "archon-search.toml"
    toml.write_text("[openai_shim]\nenabled = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="boolean"):
        load_config(path=toml)


def test_openai_shim_inject_citations_must_be_bool(tmp_path: Path) -> None:
    """[openai_shim].inject_citations must be a boolean; other types raise ConfigError."""
    from archon_search.config import ConfigError

    toml = tmp_path / "archon-search.toml"
    toml.write_text("[openai_shim]\ninject_citations = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="boolean"):
        load_config(path=toml)


def test_openai_shim_top_k_must_be_positive_int(tmp_path: Path) -> None:
    """[openai_shim].top_k must be a positive integer; 0 raises ConfigError."""
    from archon_search.config import ConfigError

    toml = tmp_path / "archon-search.toml"
    toml.write_text("[openai_shim]\ntop_k = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"must be >= 1"):
        load_config(path=toml)


def test_openai_shim_top_k_bool_rejected(tmp_path: Path) -> None:
    """[openai_shim].top_k must not accept booleans (bool is int subclass)."""
    from archon_search.config import ConfigError

    toml = tmp_path / "archon-search.toml"
    toml.write_text("[openai_shim]\ntop_k = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"Expected integer"):
        load_config(path=toml)


def test_openai_shim_top_k_float_rejected(tmp_path: Path) -> None:
    """[openai_shim].top_k must not accept floats; 3.14 raises ConfigError."""
    from archon_search.config import ConfigError

    toml = tmp_path / "archon-search.toml"
    toml.write_text("[openai_shim]\ntop_k = 3.14\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"Expected integer"):
        load_config(path=toml)


def test_openai_shim_enabled_string_rejected(tmp_path: Path) -> None:
    """[openai_shim].enabled must not accept strings; 'yes' raises ConfigError."""
    from archon_search.config import ConfigError

    toml = tmp_path / "archon-search.toml"
    toml.write_text('[openai_shim]\nenabled = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"Expected boolean"):
        load_config(path=toml)


# ---------------------------------------------------------------------------
# Snapshot inclusion
# ---------------------------------------------------------------------------


def test_config_snapshot_includes_openai_shim(tmp_path: Path) -> None:
    """SearchConfig snapshot includes openai_shim with correct nested defaults."""
    import dataclasses

    config = load_config(path=tmp_path / "nonexistent.toml")
    assert dataclasses.asdict(config)["openai_shim"] == {
        "enabled": False,
        "inject_citations": True,
        "top_k": 5,
    }

"""Tests for IngestConfig dataclass and SearchConfig.ingest integration.

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task BE-2.

TDD: tests written before IngestConfig was added to config.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, IngestConfig, SearchConfig, load_config


@pytest.fixture
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that would influence data dir or config path."""
    monkeypatch.delenv("ARCHON_SEARCH_HOST", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_PORT", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_ingest_config_default_max_file_mb_zero(_no_env: None, tmp_path: Path) -> None:
    """Default IngestConfig has max_file_mb=0 (no limit)."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.ingest.max_file_mb == 0


def test_ingest_config_dataclass_default() -> None:
    """IngestConfig() directly has max_file_mb=0."""
    ingest = IngestConfig()
    assert ingest.max_file_mb == 0


def test_search_config_has_ingest_field() -> None:
    """SearchConfig has an ingest field of type IngestConfig."""
    config = SearchConfig()
    assert isinstance(config.ingest, IngestConfig)
    assert config.ingest.max_file_mb == 0


# ---------------------------------------------------------------------------
# Round-trip from TOML
# ---------------------------------------------------------------------------


def test_ingest_config_parsed_from_toml(_no_env: None, tmp_path: Path) -> None:
    """[ingest] max_file_mb = 50 loads correctly."""
    toml_content = "[ingest]\nmax_file_mb = 50\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_file_mb == 50


def test_ingest_config_zero_is_valid(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=0 is valid (zero disables the guard)."""
    toml_content = "[ingest]\nmax_file_mb = 0\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_file_mb == 0


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_ingest_config_negative_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=-1 raises ConfigError at load time."""
    toml_content = "[ingest]\nmax_file_mb = -1\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


def test_ingest_config_float_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=3.5 (TOML float) raises ConfigError."""
    toml_content = "[ingest]\nmax_file_mb = 3.5\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


def test_ingest_config_string_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb="50" (string) raises ConfigError."""
    toml_content = '[ingest]\nmax_file_mb = "50"\n'
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


def test_ingest_config_bool_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=true (TOML boolean) raises ConfigError (bool is int subclass in Python)."""
    toml_content = "[ingest]\nmax_file_mb = true\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


# ---------------------------------------------------------------------------
# No [ingest] section (S8: defaults)
# ---------------------------------------------------------------------------


def test_no_ingest_section_defaults_to_zero(_no_env: None, tmp_path: Path) -> None:
    """When [ingest] section is absent from TOML, max_file_mb defaults to 0."""
    toml_content = "[server]\nport = 8765\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_file_mb == 0

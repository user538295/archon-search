"""Tests for MaintenanceConfig dataclass and SearchConfig.maintenance integration.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task BE-1.

TDD: write tests first, then implement MaintenanceConfig in config.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from archon_search.config import MaintenanceConfig, SearchConfig, load_config


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


def test_maintenance_config_defaults(_no_env: None, tmp_path: Path) -> None:
    """All seven MaintenanceConfig fields load with correct defaults."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    m = config.maintenance
    assert m.interval_hours == 0
    assert m.fts_optimize is True
    assert m.orphan_cleanup is True
    assert m.failed_ingest_retry is True
    assert m.retry_max_attempts == 3
    assert m.retry_max_age_hours == 72
    assert m.exclude == []


# ---------------------------------------------------------------------------
# Round-trip from TOML
# ---------------------------------------------------------------------------


def test_maintenance_config_from_toml(_no_env: None, tmp_path: Path) -> None:
    """All MaintenanceConfig fields round-trip from a TOML string via load_config()."""
    toml_content = (
        "[maintenance]\n"
        "interval_hours = 12\n"
        "fts_optimize = false\n"
        "orphan_cleanup = false\n"
        "failed_ingest_retry = false\n"
        "retry_max_attempts = 5\n"
        "retry_max_age_hours = 48\n"
        'exclude = ["ns1/col-a", "col-b"]\n'
    )
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    m = config.maintenance
    assert m.interval_hours == 12
    assert m.fts_optimize is False
    assert m.orphan_cleanup is False
    assert m.failed_ingest_retry is False
    assert m.retry_max_attempts == 5
    assert m.retry_max_age_hours == 48
    assert m.exclude == ["ns1/col-a", "col-b"]


# ---------------------------------------------------------------------------
# Edge: retry_max_age_hours = 0 → WARNING
# ---------------------------------------------------------------------------


def test_retry_max_age_hours_zero_emits_warning(
    _no_env: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """retry_max_age_hours=0 triggers a WARNING during _post_process_maintenance."""
    toml_content = "[maintenance]\nretry_max_age_hours = 0\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        config = load_config(path=cfg_path)

    assert config.maintenance.retry_max_age_hours == 0
    assert any(
        "retry_max_age_hours" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), f"Expected WARNING mentioning retry_max_age_hours; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Validation errors (ConfigError)
# ---------------------------------------------------------------------------


def test_negative_interval_hours_raises(_no_env: None, tmp_path: Path) -> None:
    """Negative interval_hours raises ConfigError."""
    from archon_search.config import ConfigError

    toml_content = "[maintenance]\ninterval_hours = -1\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="interval_hours"):
        load_config(path=cfg_path)


def test_retry_max_attempts_below_one_raises(_no_env: None, tmp_path: Path) -> None:
    """retry_max_attempts < 1 raises ConfigError."""
    from archon_search.config import ConfigError

    toml_content = "[maintenance]\nretry_max_attempts = 0\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="retry_max_attempts"):
        load_config(path=cfg_path)


def test_negative_retry_max_age_hours_raises(_no_env: None, tmp_path: Path) -> None:
    """Negative retry_max_age_hours raises ConfigError."""
    from archon_search.config import ConfigError

    toml_content = "[maintenance]\nretry_max_age_hours = -1\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="retry_max_age_hours"):
        load_config(path=cfg_path)

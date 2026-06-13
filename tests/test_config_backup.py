"""Tests for BackupConfig dataclass and SearchConfig.backup integration.

Plan: Documentation/Backlog/D2-scheduled-backup-plan.md Task 1.1.

TDD: write tests first, then implement BackupConfig in config.py.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from archon_search.config import BackupConfig, SearchConfig, load_config
from archon_search.paths import get_data_dir


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


def test_backup_defaults(_no_env: None, tmp_path: Path) -> None:
    """Default BackupConfig has interval_hours=0, keep=7, exclude=[], output_dir resolved to data_dir/backups."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.backup.interval_hours == 0
    assert config.backup.keep == 7
    assert config.backup.exclude == []
    # output_dir should be resolved to an absolute path under data_dir
    expected = str(get_data_dir() / "backups")
    assert config.backup.output_dir == expected


def test_output_dir_resolved_when_empty(_no_env: None, tmp_path: Path) -> None:
    """An empty output_dir in the TOML resolves to get_data_dir() / 'backups'."""
    toml_content = "[backup]\noutput_dir = \"\"\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    expected = str(get_data_dir() / "backups")
    assert config.backup.output_dir == expected


# ---------------------------------------------------------------------------
# Validation errors (ConfigError)
# ---------------------------------------------------------------------------


def test_negative_interval_hours_raises(_no_env: None, tmp_path: Path) -> None:
    """Negative interval_hours raises ConfigError."""
    from archon_search.config import ConfigError

    toml_content = "[backup]\ninterval_hours = -1\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="interval_hours"):
        load_config(path=cfg_path)


def test_negative_keep_raises(_no_env: None, tmp_path: Path) -> None:
    """Negative keep raises ConfigError."""
    from archon_search.config import ConfigError

    toml_content = "[backup]\nkeep = -1\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="keep"):
        load_config(path=cfg_path)


# ---------------------------------------------------------------------------
# Validation warnings
# ---------------------------------------------------------------------------


def test_warning_on_keep_zero_with_interval(_no_env: None, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """interval_hours=24 and keep=0 emits a WARNING about unbounded disk growth."""
    toml_content = "[backup]\ninterval_hours = 24\nkeep = 0\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        load_config(path=cfg_path)

    assert any(
        "keep" in record.message.lower() or "rotation" in record.message.lower()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), f"Expected WARNING about rotation being disabled; got: {[r.message for r in caplog.records]}"


def test_error_on_shallow_output_dir(_no_env: None, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """output_dir with fewer than 3 path components emits ERROR and falls back to default."""
    toml_content = '[backup]\noutput_dir = "/tmp"\n'
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="archon_search.config"):
        config = load_config(path=cfg_path)

    # Should have emitted an ERROR
    assert any(
        record.levelno == logging.ERROR for record in caplog.records
    ), f"Expected ERROR log; got: {[r.message for r in caplog.records]}"

    # Should fall back to default
    expected = str(get_data_dir() / "backups")
    assert config.backup.output_dir == expected


def test_shallow_output_dir_three_components_ok(_no_env: None, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """output_dir=/mnt/nfs/backups has 3 components and is accepted without error."""
    toml_content = '[backup]\noutput_dir = "/mnt/nfs/backups"\n'
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="archon_search.config"):
        config = load_config(path=cfg_path)

    assert not any(
        record.levelno == logging.ERROR for record in caplog.records
    ), f"Did not expect ERROR log; got: {[r.message for r in caplog.records]}"
    assert config.backup.output_dir == "/mnt/nfs/backups"


# ---------------------------------------------------------------------------
# Exclude patterns
# ---------------------------------------------------------------------------


def test_exclude_patterns_load(_no_env: None, tmp_path: Path) -> None:
    """Bare and qualified exclude patterns parse correctly."""
    toml_content = '[backup]\nexclude = ["docs", "tenants/private"]\n'
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.backup.exclude == ["docs", "tenants/private"]


# ---------------------------------------------------------------------------
# Snapshot test — SearchConfig must include backup key
# ---------------------------------------------------------------------------


def test_config_snapshot_includes_backup(_no_env: None, tmp_path: Path) -> None:
    """SearchConfig.backup key is present in the dataclass fields and in asdict output."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    fields = {f.name for f in dataclasses.fields(SearchConfig)}
    assert "backup" in fields

    d = dataclasses.asdict(config)
    assert "backup" in d
    backup_d = d["backup"]
    assert "interval_hours" in backup_d
    assert "keep" in backup_d
    assert "exclude" in backup_d
    assert "output_dir" in backup_d

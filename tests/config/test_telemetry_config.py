"""Tests for TelemetryConfig integration in SearchConfig (FEAT-039b Task 1.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from archon_search.config import ConfigError, SearchConfig, load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "archon-search.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_telemetry_config_defaults() -> None:
    cfg = SearchConfig()
    assert cfg.telemetry.enabled is False
    assert cfg.telemetry.retention_days == 30
    assert cfg.telemetry.export_enabled is False
    assert cfg.telemetry.log_dir == "~/.archon/search-logs"


def test_telemetry_config_parses_toml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[telemetry]
enabled = true
retention_days = 7
""",
    )
    cfg = load_config(path)
    assert cfg.telemetry.enabled is True
    assert cfg.telemetry.retention_days == 7


def test_telemetry_config_missing_section_uses_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path, "[server]\nhost = \"127.0.0.1\"\n")
    cfg = load_config(path)
    assert cfg.telemetry.enabled is False
    assert cfg.telemetry.retention_days == 30
    assert cfg.telemetry.export_enabled is False
    assert cfg.telemetry.log_dir == "~/.archon/search-logs"


def test_telemetry_config_rejects_retention_days_zero(tmp_path: Path) -> None:
    path = _write(tmp_path, "[telemetry]\nretention_days = 0\n")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(path)


def test_telemetry_config_rejects_non_bool_enabled(tmp_path: Path) -> None:
    path = _write(tmp_path, '[telemetry]\nenabled = "yes"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_telemetry_config_parses_log_dir_override(tmp_path: Path) -> None:
    path = _write(tmp_path, '[telemetry]\nlog_dir = "/custom/path"\n')
    cfg = load_config(path)
    assert cfg.telemetry.log_dir == "/custom/path"


def test_telemetry_config_rejects_empty_log_dir(tmp_path: Path) -> None:
    path = _write(tmp_path, '[telemetry]\nlog_dir = ""\n')
    with pytest.raises(ConfigError, match="log_dir"):
        load_config(path)


def test_pyproject_has_explicit_pydantic_dependency() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    doc = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    deps = [str(d) for d in doc["project"]["dependencies"]]
    assert any(d.startswith("pydantic") for d in deps), deps

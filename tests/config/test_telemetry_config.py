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


def test_telemetry_config_rejects_export_enabled_true(tmp_path: Path) -> None:
    path = _write(tmp_path, "[telemetry]\nexport_enabled = true\n")
    with pytest.raises(ConfigError, match="reserved for FEAT-039c"):
        load_config(path)


def test_telemetry_config_emits_warning_on_export_rejection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, "[telemetry]\nexport_enabled = true\n")
    with caplog.at_level("WARNING", logger="archon.search"):
        with pytest.raises(ConfigError):
            load_config(path)
    records = [
        r for r in caplog.records if r.name == "archon.search" and r.levelname == "WARNING"
    ]
    assert any("telemetry: export attempt rejected" in r.getMessage() for r in records), [
        r.getMessage() for r in records
    ]


def test_telemetry_config_warning_emitted_before_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from archon_search import config as config_module

    events: list[str] = []
    original_warning = config_module._logger.warning

    def record_warning(*args: object, **kwargs: object) -> None:
        events.append("warning")
        original_warning(*args, **kwargs)

    monkeypatch.setattr(config_module._logger, "warning", record_warning)

    path = _write(tmp_path, "[telemetry]\nexport_enabled = true\n")
    try:
        load_config(path)
    except ConfigError:
        events.append("raise")
    assert events == ["warning", "raise"], events


def test_telemetry_config_export_enabled_false_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, "[telemetry]\nexport_enabled = false\n")
    with caplog.at_level("WARNING", logger="archon.search"):
        cfg = load_config(path)
    assert cfg.telemetry.export_enabled is False
    assert not [
        r for r in caplog.records if r.name == "archon.search" and r.levelname == "WARNING"
    ]


def test_pyproject_has_explicit_pydantic_dependency() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    doc = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    deps = [str(d) for d in doc["project"]["dependencies"]]
    assert any(d.startswith("pydantic") for d in deps), deps

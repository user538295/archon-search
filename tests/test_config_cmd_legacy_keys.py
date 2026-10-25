"""``config set|show`` on legacy (removed ``providers`` keys), invalid and malformed files."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from archon_search.cli.config_cmd import config as config_group

LEGACY = '[database]\nchunk_size = 512\nproviders = []\nreranker_providers = []\n\n[graph]\nenabled = false\nproviders = ["CPUExecutionProvider"]\n'
REMOVED_MSG = "[database].providers was replaced by [database].device (auto|cpu|mps|cuda); see BREAKING.md"


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(config_group, list(args))


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    path = tmp_path / "archon-search.toml"
    path.write_text(LEGACY, encoding="utf-8")
    return path


def test_set_removed_key_is_rejected_and_file_unchanged(legacy: Path) -> None:
    result = _invoke("set", "database.providers", "[]", "--config", str(legacy))

    assert result.exit_code == 1
    assert result.stderr.strip() == f"Error: {REMOVED_MSG}"
    assert legacy.read_text(encoding="utf-8") == LEGACY


def test_set_removed_graph_key_names_graph_device(legacy: Path) -> None:
    result = _invoke("set", "graph.providers", "cpu", "--config", str(legacy))

    assert result.exit_code == 1
    assert "[graph].providers was replaced by [graph].device" in result.stderr


def test_set_invalid_device_is_rejected_and_file_unchanged(legacy: Path) -> None:
    result = _invoke("set", "database.device", "tpu", "--config", str(legacy))

    assert result.exit_code == 1
    assert result.stderr.startswith("Error: [database].device must be one of")
    assert len(result.stderr.strip().splitlines()) == 1
    assert legacy.read_text(encoding="utf-8") == LEGACY


def test_set_valid_device_works_on_legacy_file(legacy: Path) -> None:
    result = _invoke("set", "database.device", "mps", "--config", str(legacy))

    assert result.exit_code == 0, result.stderr
    assert 'device = "mps"' in legacy.read_text(encoding="utf-8")


def test_set_invalid_value_type_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "archon-search.toml"

    result = _invoke("set", "server.port", "70000", "--config", str(path))

    assert result.exit_code == 1
    assert "port must be between 1 and 65535" in result.stderr
    assert not path.exists()


@pytest.mark.parametrize("args", [("set", "server.port", "9000"), ("show",), ("get", "server.port")])
def test_malformed_toml_reports_one_clean_line(tmp_path: Path, args: tuple[str, ...]) -> None:
    path = tmp_path / "archon-search.toml"
    path.write_text("[graph]\nenabled = false\n[graph]\n", encoding="utf-8")

    result = _invoke(*args, "--config", str(path))

    assert result.exit_code == 1
    assert "Traceback" not in result.output + result.stderr
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"Error: Failed to parse {path}")
    assert "line 3" in lines[0]


def test_show_legacy_file_warns_on_stderr_and_exits_zero(legacy: Path) -> None:
    result = _invoke("show", "--config", str(legacy))

    assert result.exit_code == 0
    warning = result.stderr.strip()
    assert len(warning.splitlines()) == 1
    assert warning.startswith("Warning:")
    for name in ("[database].providers", "[database].reranker_providers", "[graph].providers"):
        assert name in warning
    assert "device" in warning


def test_show_clean_file_prints_no_warning(tmp_path: Path) -> None:
    path = tmp_path / "archon-search.toml"
    path.write_text('[database]\ndevice = "cpu"\n', encoding="utf-8")

    result = _invoke("show", "--config", str(path))

    assert result.exit_code == 0
    assert result.stderr == ""

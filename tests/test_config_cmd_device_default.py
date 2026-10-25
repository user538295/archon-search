"""``config get/show`` report the effective ``[database].device`` default for a file that omits it."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from archon_search.cli.config_cmd import config as config_group
from archon_search.config import SearchConfig


def test_get_database_device_returns_default_when_file_omits_it(tmp_path: Path) -> None:
    """A file written before the key existed must still answer ``config get database.device``."""
    path = tmp_path / "archon-search.toml"
    path.write_text('[database]\nchunk_size = 512\n', encoding="utf-8")

    result = CliRunner().invoke(config_group, ["get", "database.device", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == SearchConfig().device


def test_show_lists_database_device_default(tmp_path: Path) -> None:
    path = tmp_path / "archon-search.toml"
    path.write_text("", encoding="utf-8")

    result = CliRunner().invoke(config_group, ["show", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert f'device = "{SearchConfig().device}"' in result.output

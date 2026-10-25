"""The wizard proceeds on a legacy config (removed ``providers`` keys); serve/start/install stay strict."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.config import ConfigError, load_config
from archon_search.install import create_installer
from tests.test_e2e_wizard_optional_features import _patched_wizard

LEGACY = (
    '[database]\nembedding_model = "BAAI/bge-small-en-v1.5"\nproviders = ["CoreMLExecutionProvider"]\n'
    'reranker_providers = []\n\n[graph]\nenabled = false\nproviders = ["CPUExecutionProvider"]\n'
)
DROPPED_LINE = "Dropping removed config keys [database].providers, [database].reranker_providers, [graph].providers"


@pytest.fixture
def legacy_path(tmp_path: Path) -> Path:
    path = tmp_path / "archon-search.toml"
    path.write_text(LEGACY, encoding="utf-8")
    return path


def test_create_installer_is_strict_by_default(legacy_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[database\]\.providers was replaced by \[database\]\.device"):
        create_installer(str(legacy_path), dry_run=True)


def test_create_installer_can_tolerate_legacy_config(legacy_path: Path) -> None:
    installer = create_installer(str(legacy_path), dry_run=True, legacy_config=True)

    assert installer.cfg.device == "auto"


def test_wizard_rewrites_legacy_config_without_removed_keys(legacy_path: Path) -> None:
    with _patched_wizard():
        result = CliRunner().invoke(
            main, ["wizard", "--non-interactive", "--profile", "minimal", "--config", str(legacy_path),
                   "--skip-preload", "--disable-gpu"],
        )

    assert result.exit_code == 0, result.output
    assert DROPPED_LINE in result.output
    doc = tomlkit.parse(legacy_path.read_text(encoding="utf-8"))
    assert "providers" not in doc["database"]
    assert "reranker_providers" not in doc["database"]
    assert "providers" not in doc["graph"]
    assert doc["database"]["device"] == "cpu"
    assert load_config(legacy_path).device == "cpu"  # strict loader accepts the rewritten file
    assert "providers" in legacy_path.with_suffix(".toml.bak").read_text(encoding="utf-8")


def test_wizard_dry_run_names_keys_and_leaves_file_untouched(legacy_path: Path) -> None:
    with _patched_wizard():
        result = CliRunner().invoke(
            main, ["wizard", "--dry-run", "--non-interactive", "--profile", "minimal",
                   "--config", str(legacy_path), "--skip-preload", "--disable-gpu"],
        )

    assert result.exit_code == 0, result.output
    assert "Would drop removed config keys" in result.output
    assert legacy_path.read_text(encoding="utf-8") == LEGACY


def test_serve_style_load_stays_strict(legacy_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(legacy_path)

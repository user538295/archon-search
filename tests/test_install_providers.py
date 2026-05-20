"""Tests for SearchInstaller.configure_providers() — Task 2.3 Fix 3."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from archon_search.install import SearchInstaller
from archon_search.platform.types import GpuType


def _make_minimal_toml(tmp_path: Path) -> Path:
    """Create a minimal valid TOML config file and return its path."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[server]\nhost = \"127.0.0.1\"\n", encoding="utf-8")
    return toml_file


class TestConfigureProviders:
    def test_configure_providers_cuda_writes_cuda_provider(self, tmp_path: Path) -> None:
        toml_file = _make_minimal_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))
        installer.configure_providers(GpuType.CUDA)

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["providers"] == ["CUDAExecutionProvider"]

    def test_configure_providers_metal_writes_coreml_provider(self, tmp_path: Path) -> None:
        toml_file = _make_minimal_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))
        installer.configure_providers(GpuType.METAL)

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["providers"] == ["CoreMLExecutionProvider"]

    def test_configure_providers_none_does_not_write(self, tmp_path: Path) -> None:
        toml_file = _make_minimal_toml(tmp_path)
        original_content = toml_file.read_text()
        installer = SearchInstaller(config_file=str(toml_file))
        installer.configure_providers(GpuType.NONE)

        doc = tomlkit.parse(toml_file.read_text())
        assert "providers" not in doc.get("database", {})

    def test_configure_providers_dry_run_does_not_write(self, tmp_path: Path) -> None:
        toml_file = _make_minimal_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file), dry_run=True)
        installer.configure_providers(GpuType.CUDA)

        doc = tomlkit.parse(toml_file.read_text())
        assert "providers" not in doc.get("database", {})

    def test_configure_providers_fallback_path_warns_when_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """configure_providers with config_file=None uses ~/.archon-search/archon-search.toml as fallback.

        When that file does not exist the method logs a warning and returns without writing.
        """
        import logging
        from unittest.mock import patch

        installer = SearchInstaller(config_file=None)
        expected_path = Path.home() / ".archon-search" / "archon-search.toml"

        # Patch Path.exists() only for the fallback path so it returns False (file absent).
        original_exists = Path.exists

        def patched_exists(self: Path) -> bool:
            if self == expected_path:
                return False
            return original_exists(self)

        with (
            caplog.at_level(logging.WARNING),
            patch.object(Path, "exists", patched_exists),
        ):
            installer.configure_providers(GpuType.CUDA)

        # A warning must have been emitted referencing the .archon-search path.
        assert any(".archon-search" in r.message for r in caplog.records)

    def test_configure_providers_skips_if_already_set(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "archon-search.toml"
        toml_file.write_text(
            '[server]\nhost = "127.0.0.1"\n\n[database]\nproviders = ["CUDAExecutionProvider", "CPUExecutionProvider"]\n',
            encoding="utf-8",
        )
        installer = SearchInstaller(config_file=str(toml_file))
        installer.configure_providers(GpuType.CUDA)

        doc = tomlkit.parse(toml_file.read_text())
        # Should not overwrite the existing extended chain
        assert doc["database"]["providers"] == ["CUDAExecutionProvider", "CPUExecutionProvider"]

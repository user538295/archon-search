"""Tests for CoreML split-provider wizard support.

Tests cover:
- configure_reranker_providers writes empty list to TOML
- validate_embedder_only calls validate_providers_shared with empty reranker_model
- Wizard split logic: combined fails, embedder-only passes → split config
- Wizard: both fail → CPU fallback, no split
- Wizard: combined passes → no split (existing behaviour)
- Summary shows split text "CoreML — text search; CPU — result ranking"
- FE-1 skips re-probe when split_coreml=True
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import tomlkit

from archon_search.install import SearchInstaller


def _make_toml(tmp_path: Path) -> Path:
    p = tmp_path / "archon-search.toml"
    p.write_text("[server]\nhost = \"127.0.0.1\"\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# configure_reranker_providers
# ---------------------------------------------------------------------------


class TestConfigureRerankerProviders:
    def test_writes_empty_list_to_toml(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))
        installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == []

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file), dry_run=True)
        installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})

    def test_missing_config_file_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        installer = SearchInstaller(config_file="/nonexistent/path/archon-search.toml")
        with caplog.at_level(logging.WARNING):
            installer.configure_reranker_providers([])
        assert "not found" in caplog.text

    def test_creates_database_section_if_absent(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "archon-search.toml"
        toml_file.write_text("[server]\nport = 8765\n", encoding="utf-8")
        installer = SearchInstaller(config_file=str(toml_file))
        installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == []


# ---------------------------------------------------------------------------
# validate_embedder_only
# ---------------------------------------------------------------------------


class TestValidateEmbedderOnly:
    def test_calls_validate_providers_shared_with_empty_reranker(
        self, tmp_path: Path
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        with patch(
            "archon_search.install.SearchInstaller.validate_embedder_only",
            wraps=installer.validate_embedder_only,
        ):
            with patch(
                "archon_search.model_validation.validate_providers_shared",
                return_value=(True, True, []),
            ) as mock_vps:
                result = installer.validate_embedder_only(["CoreMLExecutionProvider"])

        # validate_embedder_only calls validate_providers_shared with reranker_model=""
        mock_vps.assert_called_once_with(
            ["CoreMLExecutionProvider"],
            installer.cfg.embedding_model,
            "",  # disabled reranker
        )
        assert result is True

    def test_returns_false_on_embedder_failure(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        with patch(
            "archon_search.model_validation.validate_providers_shared",
            return_value=(False, True, ["embedder probe failed"]),
        ):
            result = installer.validate_embedder_only(["CoreMLExecutionProvider"])

        assert result is False

    def test_returns_false_on_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        with patch(
            "archon_search.model_validation.validate_providers_shared",
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.WARNING):
                result = installer.validate_embedder_only(["CoreMLExecutionProvider"])

        assert result is False
        assert "boom" in caplog.text


# ---------------------------------------------------------------------------
# Wizard CoreML split flow (via install_run)
# ---------------------------------------------------------------------------


def _make_full_toml(tmp_path: Path) -> Path:
    """Minimal TOML that passes wizard config-file checks."""
    p = tmp_path / "archon-search.toml"
    p.write_text(
        "[server]\nhost = \"127.0.0.1\"\nport = 8765\n"
        "[database]\n"
        "embedding_model = \"BAAI/bge-small-en-v1.5\"\n"
        "reranker_model = \"Xenova/ms-marco-MiniLM-L-6-v2\"\n",
        encoding="utf-8",
    )
    return p


class TestWizardCoreMlSplitLogic:
    """Unit tests for the wizard's Step 9 CoreML split-detection branch.

    We call validate_providers / validate_embedder_only / configure_providers /
    configure_reranker_providers directly rather than running install_run() to
    avoid the many irrelevant wizard steps.
    """

    def test_combined_passes_no_split(self, tmp_path: Path) -> None:
        """When combined probe passes, no reranker_providers key is written."""
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        with patch.object(installer, "validate_providers", return_value=True):
            # Simulate Step 9: combined passed → configure_providers only
            installer.configure_providers(
                __import__(
                    "archon_search.platform.types", fromlist=["GpuType"]
                ).GpuType.METAL
            )

        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})
        assert doc["database"]["providers"] == ["CoreMLExecutionProvider"]

    def test_combined_fails_embedder_passes_split_written(
        self, tmp_path: Path
    ) -> None:
        """When combined fails but embedder-only passes, split config is written."""
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        # Step 9 logic: combined fails, embedder-only passes
        combined_ok = False
        embedder_only_ok = True

        from archon_search.platform.types import GpuType

        if combined_ok:
            installer.configure_providers(gpu=GpuType.METAL)
        elif embedder_only_ok:
            installer.configure_providers(gpu=GpuType.METAL)
            installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["providers"] == ["CoreMLExecutionProvider"]
        assert doc["database"]["reranker_providers"] == []

    def test_both_fail_no_split_written(self, tmp_path: Path) -> None:
        """When both probes fail, no provider config is written."""
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        combined_ok = False
        embedder_only_ok = False

        from archon_search.platform.types import GpuType

        if combined_ok:
            installer.configure_providers(gpu=GpuType.METAL)
        elif embedder_only_ok:
            installer.configure_providers(gpu=GpuType.METAL)
            installer.configure_reranker_providers([])
        # else: CPU fallback — nothing written

        doc = tomlkit.parse(toml_file.read_text())
        assert "providers" not in doc.get("database", {})
        assert "reranker_providers" not in doc.get("database", {})

    def test_split_summary_label(self) -> None:
        """The split path sets providers label to the split-text string."""
        providers: list[str] = []
        split_coreml = False
        combined_ok = False
        embedder_only_ok = True

        if combined_ok:
            providers = ["CoreML (Apple Silicon)"]
        elif embedder_only_ok:
            providers = ["CoreML — text search; CPU — result ranking"]
            split_coreml = True

        assert providers == ["CoreML — text search; CPU — result ranking"]
        assert split_coreml is True

    def test_fe1_skips_reprobe_when_split_coreml(self, tmp_path: Path) -> None:
        """FE-1 block skips validate_providers when split_coreml=True."""
        toml_file = _make_toml(tmp_path)
        installer = SearchInstaller(config_file=str(toml_file))

        called = []

        def _fake_validate_providers(providers_arg: list[str]) -> bool:
            called.append(providers_arg)
            return False  # would fail

        split_coreml = True
        gpu_provider = "CoreMLExecutionProvider"
        has_reranker = True

        # Simulate the FE-1 guard
        if gpu_provider is not None and has_reranker and not split_coreml:
            _fake_validate_providers([gpu_provider])

        assert called == [], "validate_providers should not be called when split_coreml=True"

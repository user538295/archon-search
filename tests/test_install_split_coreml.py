"""Tests for CoreML split-provider wizard support.

Tests cover:
- configure_reranker_providers writes empty list to TOML
- validate_embedder_only calls validate_providers_shared with empty reranker_model
- Wizard split logic: combined fails, embedder-only passes → split config
- Wizard: both fail → CPU fallback, providers = [] written, no split
- Wizard: CoreML absent from onnxruntime → CPU written, never the unvalidated accelerator
- Wizard: combined passes → no split (existing behaviour)
- Summary shows split text "CoreML — text search; CPU — result ranking"
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomlkit

from archon_search.install import create_installer

_AVAILABLE_PROVIDERS = "archon_search.onnx_providers._available_providers"
# A host whose onnxruntime has CoreML, so the probe — not availability — decides.
_COREML_HOST = ["CoreMLExecutionProvider", "CPUExecutionProvider"]


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
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == []

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file), dry_run=True)
        installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})

    def test_missing_config_file_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        installer = create_installer(config_file="/nonexistent/path/archon-search.toml")
        with caplog.at_level(logging.WARNING):
            installer.configure_reranker_providers([])
        assert "not found" in caplog.text

    def test_creates_database_section_if_absent(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "archon-search.toml"
        toml_file.write_text("[server]\nport = 8765\n", encoding="utf-8")
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers([])

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == []

    def test_writes_nonempty_list_to_toml(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers(["CPUExecutionProvider"])

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == ["CPUExecutionProvider"]

    def test_idempotent_does_not_rewrite_when_already_set(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers([])
        mtime1 = toml_file.stat().st_mtime_ns
        installer.configure_reranker_providers([])  # second call
        mtime2 = toml_file.stat().st_mtime_ns
        assert mtime1 == mtime2  # file not rewritten


# ---------------------------------------------------------------------------
# clear_reranker_providers
# ---------------------------------------------------------------------------


class TestClearRerankerProviders:
    def test_removes_key_from_toml(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers([])  # write stale split
        installer.clear_reranker_providers()

        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})

    def test_no_op_when_key_absent(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        installer.clear_reranker_providers()  # no reranker_providers present → no error

        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})

    def test_dry_run_does_not_clear(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers([])  # write split
        installer_dry = create_installer(config_file=str(toml_file), dry_run=True)
        installer_dry.clear_reranker_providers()

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == []  # unchanged

    def test_preserves_manual_value(self, tmp_path: Path) -> None:
        """A user-set non-empty reranker_providers must not be deleted by the self-heal."""
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        installer.configure_reranker_providers(["CPUExecutionProvider"])
        installer.clear_reranker_providers()

        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["reranker_providers"] == ["CPUExecutionProvider"]


# ---------------------------------------------------------------------------
# validate_embedder_only
# ---------------------------------------------------------------------------


class TestValidateEmbedderOnly:
    def test_calls_validate_providers_shared_with_empty_reranker(
        self, tmp_path: Path
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(
            "archon_search.install.BaseInstaller.validate_embedder_only",
            wraps=installer.validate_embedder_only,
        ), patch(_AVAILABLE_PROVIDERS, return_value=_COREML_HOST):
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
        installer = create_installer(config_file=str(toml_file))

        with patch(
            "archon_search.model_validation.validate_providers_shared",
            return_value=(False, True, ["embedder probe failed"]),
        ), patch(_AVAILABLE_PROVIDERS, return_value=_COREML_HOST):
            result = installer.validate_embedder_only(["CoreMLExecutionProvider"])

        assert result is False

    def test_returns_false_on_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(
            "archon_search.model_validation.validate_providers_shared",
            side_effect=RuntimeError("boom"),
        ), patch(_AVAILABLE_PROVIDERS, return_value=_COREML_HOST):
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
    """Unit tests for the wizard's CoreML split-detection branch via _probe_and_configure_coreml."""

    def test_combined_passes_no_split(self, tmp_path: Path) -> None:
        """When combined probe passes, no reranker_providers key is written."""
        from archon_search.platform.types import GpuType

        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch.object(installer, "validate_providers", return_value=True):
            labels = installer._probe_and_configure_coreml(GpuType.METAL, toml_file)

        assert labels == ["CoreML (Apple Silicon)"]
        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})
        assert doc["database"]["providers"] == ["CoreMLExecutionProvider"]

    def test_combined_fails_embedder_passes_split_written(
        self, tmp_path: Path
    ) -> None:
        """When combined fails but embedder-only passes, split config is written."""
        from archon_search.platform.types import GpuType

        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch.object(installer, "validate_providers", return_value=False):
            with patch.object(installer, "validate_embedder_only", return_value=True):
                labels = installer._probe_and_configure_coreml(GpuType.METAL, toml_file)

        assert "CPU — result ranking" in labels[0]
        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["providers"] == ["CoreMLExecutionProvider"]
        assert doc["database"]["reranker_providers"] == []

    def test_both_fail_writes_cpu_providers_no_split(self, tmp_path: Path) -> None:
        """When both probes fail, CPU is written (providers = []) and no split config."""
        from archon_search.platform.types import GpuType

        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch.object(installer, "validate_providers", return_value=False):
            with patch.object(installer, "validate_embedder_only", return_value=False):
                labels = installer._probe_and_configure_coreml(GpuType.METAL, toml_file)

        assert labels == []
        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["providers"] == []
        assert "reranker_providers" not in doc.get("database", {})

    def test_coreml_unavailable_in_onnxruntime_writes_cpu(self, tmp_path: Path) -> None:
        """S635: models would load on CPU under a CoreML config on this host, but an
        unavailable accelerator is not a validated one — CPU is written, no CoreML label."""
        from archon_search.platform.types import GpuType

        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        te, ce = MagicMock(), MagicMock()

        with patch("archon_search.model_validation.TextEmbedding", te), \
             patch("archon_search.model_validation._load_cross_encoder", ce), \
             patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]):
            labels = installer._probe_and_configure_coreml(GpuType.METAL, toml_file)

        assert labels == []
        doc = tomlkit.parse(toml_file.read_text())
        assert doc["database"]["providers"] == []
        assert "reranker_providers" not in doc.get("database", {})
        te.assert_not_called()
        ce.assert_not_called()

    def test_combined_passes_clears_stale_split_config(self, tmp_path: Path) -> None:
        """Re-run after upgrade: combined probe now passes → stale reranker_providers=[] is removed.
        validate_embedder_only must NOT be called (proves the if-branch, not elif, did the work)."""
        from archon_search.platform.types import GpuType

        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))
        # Simulate an existing split config written by a prior wizard run
        installer.configure_reranker_providers([])

        with patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "validate_embedder_only") as mock_embedder_only:
            installer._probe_and_configure_coreml(GpuType.METAL, toml_file)

        mock_embedder_only.assert_not_called()
        doc = tomlkit.parse(toml_file.read_text())
        assert "reranker_providers" not in doc.get("database", {})

    def test_split_summary_label_in_rendered_output(self, tmp_path: Path) -> None:
        """The split provider label renders in the install summary."""
        from archon_search.install import _render_summary
        from archon_search.profiles import InstallProfile

        prof = InstallProfile(
            name="minimal",
            embedder="BAAI/bge-small-en-v1.5",
            reranker="Xenova/ms-marco-MiniLM-L-6-v2",
            chunk_size=512,
            download_mb=147,
            quality_stars="★★☆☆☆",
            cpu_ms=40,
            metal_ms=15,
            memory_gb=0.5,
        )
        # the provider labels _probe_and_configure_coreml returns for the split config
        providers = ["CoreML — text search; CPU — result ranking"]
        summary = _render_summary("minimal", prof, False, providers, config_path="archon-search.toml")
        assert "CoreML — text search; CPU — result ranking" in summary

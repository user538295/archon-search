"""Unit tests for the HyDE / RAG Fusion wizard install fix.

Brief: Documentation/Backlog/2026-07-15-060-hyde-ragfusion-wizard-brief.md

Covers four new pieces of behavior:
  1. _check_provider_deps (app.py) — anthropic guard, ENABLED-GATED.
  2. _install_query_expansion_extras (install.py) — provider→package install, deduped.
  3. _revert_query_expansion_flags (install.py) — rollback on failure.
  4. _assert_features_persisted (install.py) — post-write persistence assertion.
  5. _render_summary (install.py) — HyDE / RAG Fusion summary bullets.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomlkit

from archon_search.install import (
    InstallError,
    WizardFeatures,
    _assert_features_persisted,
    _install_query_expansion_extras,
    _render_summary,
    _revert_query_expansion_flags,
)
from archon_search.profiles import get_profile

pytestmark = pytest.mark.xdist_group("install")


# ---------------------------------------------------------------------------
# _check_provider_deps — anthropic guard is ENABLED-GATED
#
# The default provider is "anthropic" (config.py), so an UNCONDITIONAL guard
# would force the package on every install and break optional-extras. The guard
# must fire only when the feature is enabled.
# ---------------------------------------------------------------------------


class TestCheckProviderDepsAnthropic:
    def _config(self, **hyde_kwargs):
        from archon_search.config import HyDEConfig, RAGFusionConfig, SearchConfig  # noqa: PLC0415

        cfg = SearchConfig()
        cfg.hyde = HyDEConfig(**hyde_kwargs)
        cfg.rag_fusion = RAGFusionConfig()
        return cfg

    def test_hyde_enabled_anthropic_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hyde.enabled + provider=anthropic + package absent → ConfigError."""
        from archon_search.config import ConfigError  # noqa: PLC0415
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415

        monkeypatch.setitem(sys.modules, "anthropic", None)
        cfg = self._config(enabled=True, provider="anthropic")
        with pytest.raises(ConfigError, match="anthropic"):
            _check_provider_deps(cfg)

    def test_hyde_disabled_anthropic_missing_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """provider=anthropic (default) but hyde DISABLED + package absent → NO error.

        This is the optional-extras invariant: a default install must not require
        the anthropic package just because the default provider is anthropic.
        """
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415

        monkeypatch.setitem(sys.modules, "anthropic", None)
        cfg = self._config(enabled=False, provider="anthropic")
        _check_provider_deps(cfg)  # must not raise

    def test_hyde_enabled_anthropic_present_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hyde.enabled + provider=anthropic + package present → NO error."""
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415

        monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
        cfg = self._config(enabled=True, provider="anthropic")
        _check_provider_deps(cfg)  # must not raise

    def test_rag_fusion_enabled_anthropic_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """rag_fusion.enabled + provider=anthropic + package absent → ConfigError names rag_fusion."""
        from archon_search.config import ConfigError, RAGFusionConfig, SearchConfig  # noqa: PLC0415
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415

        monkeypatch.setitem(sys.modules, "anthropic", None)
        cfg = SearchConfig()
        cfg.rag_fusion = RAGFusionConfig(enabled=True, provider="anthropic")
        with pytest.raises(ConfigError, match="rag_fusion"):
            _check_provider_deps(cfg)

    def test_claude_cli_enabled_no_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """provider=claude_cli must NOT be guarded — it degrades gracefully via shutil.which."""
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415

        # No `anthropic`/`claude` presence assumptions — claude_cli has no pip package.
        monkeypatch.setitem(sys.modules, "anthropic", None)
        cfg = self._config(enabled=True, provider="claude_cli", model="")
        _check_provider_deps(cfg)  # must not raise

    def test_both_enabled_anthropic_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from archon_search.config import ConfigError, HyDEConfig, RAGFusionConfig, SearchConfig  # noqa: PLC0415
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415
        monkeypatch.setitem(sys.modules, "anthropic", None)
        cfg = SearchConfig()
        cfg.hyde = HyDEConfig(enabled=True, provider="anthropic")
        cfg.rag_fusion = RAGFusionConfig(enabled=True, provider="anthropic")
        with pytest.raises(ConfigError, match="hyde"):
            _check_provider_deps(cfg)

    def test_rag_fusion_disabled_anthropic_missing_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from archon_search.config import RAGFusionConfig, SearchConfig  # noqa: PLC0415
        from archon_search.server.app import _check_provider_deps  # noqa: PLC0415
        monkeypatch.setitem(sys.modules, "anthropic", None)
        cfg = SearchConfig()
        cfg.rag_fusion = RAGFusionConfig(enabled=False, provider="anthropic")
        _check_provider_deps(cfg)  # must not raise


# ---------------------------------------------------------------------------
# _install_query_expansion_extras — provider→package, deduplicated
# ---------------------------------------------------------------------------


class TestInstallQueryExpansionExtras:
    def test_both_anthropic_dedups_to_single_install(self) -> None:
        """HyDE + RAG Fusion both on anthropic → exactly one _install_extra call."""
        features = WizardFeatures(
            enable_hyde=True, enable_rag_fusion=True,
            hyde_provider="anthropic", rag_fusion_provider="anthropic",
        )
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(features, dry_run=False)
        mock_extra.assert_called_once()
        assert mock_extra.call_args[0][0] == "archon-search[hyde]"

    def test_hyde_only_anthropic(self) -> None:
        features = WizardFeatures(enable_hyde=True, hyde_provider="anthropic")
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(features, dry_run=False)
        mock_extra.assert_called_once()
        assert mock_extra.call_args[0][0] == "archon-search[hyde]"

    def test_mixed_providers_install_each_package(self) -> None:
        """HyDE=anthropic + RAG Fusion=ollama → two distinct package installs."""
        features = WizardFeatures(
            enable_hyde=True, enable_rag_fusion=True,
            hyde_provider="anthropic", rag_fusion_provider="ollama",
            rag_fusion_model="qwen2.5:3b",
        )
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(features, dry_run=False)
        installed = {c[0][0] for c in mock_extra.call_args_list}
        assert installed == {"archon-search[hyde]", "archon-search[ollama]"}

    def test_openai_provider_package(self) -> None:
        features = WizardFeatures(enable_hyde=True, hyde_provider="openai", hyde_model="gpt-4o-mini")
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(features, dry_run=False)
        mock_extra.assert_called_once()
        assert mock_extra.call_args[0][0] == "archon-search[openai-provider]"

    def test_claude_cli_installs_nothing(self) -> None:
        """claude_cli has no pip package → no install call."""
        features = WizardFeatures(enable_hyde=True, hyde_provider="claude_cli")
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(features, dry_run=False)
        mock_extra.assert_not_called()

    def test_neither_enabled_installs_nothing(self) -> None:
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(WizardFeatures(), dry_run=False)
        mock_extra.assert_not_called()

    def test_dry_run_forwarded(self) -> None:
        features = WizardFeatures(enable_hyde=True, hyde_provider="anthropic")
        with patch("archon_search.install._install_extra") as mock_extra:
            _install_query_expansion_extras(features, dry_run=True)
        assert mock_extra.call_args[0][2] is True

    def test_failed_install_is_returned_not_raised(self) -> None:
        features = WizardFeatures(enable_hyde=True, hyde_provider="anthropic")
        with patch("archon_search.install._install_extra", side_effect=InstallError("pip failed")):
            failed = _install_query_expansion_extras(features, dry_run=False)
        assert failed == ["hyde"]

    def test_mixed_provider_partial_failure_returns_only_failed_section(self) -> None:
        """HyDE=anthropic (installs) + RAG Fusion=ollama (fails) → only rag_fusion reported."""
        features = WizardFeatures(
            enable_hyde=True, hyde_provider="anthropic",
            enable_rag_fusion=True, rag_fusion_provider="ollama", rag_fusion_model="qwen2.5:3b",
        )

        def _fail_ollama(package, *_a, **_k):
            if package == "archon-search[ollama]":
                raise InstallError("boom")

        with patch("archon_search.install._install_extra", side_effect=_fail_ollama):
            failed = _install_query_expansion_extras(features, dry_run=False)
        assert failed == ["rag_fusion"]

    def test_shared_package_failure_reports_both_sections(self) -> None:
        """Both on anthropic (one shared package); its failure marks BOTH sections failed."""
        features = WizardFeatures(
            enable_hyde=True, enable_rag_fusion=True,
            hyde_provider="anthropic", rag_fusion_provider="anthropic",
        )
        with patch("archon_search.install._install_extra", side_effect=InstallError("boom")):
            failed = _install_query_expansion_extras(features, dry_run=False)
        assert set(failed) == {"hyde", "rag_fusion"}


# ---------------------------------------------------------------------------
# _revert_query_expansion_flags — rollback on failure/abort
# ---------------------------------------------------------------------------


class TestRevertQueryExpansionFlags:
    def _write(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "archon-search.toml"
        cfg.write_text(body)
        return cfg

    def test_reverts_both_enabled_flags(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        cfg = self._write(tmp_path, "[hyde]\nenabled = true\n[rag_fusion]\nenabled = true\n")
        _revert_query_expansion_flags(cfg, dry_run=False)
        doc = tomlkit.parse(cfg.read_text())
        assert doc["hyde"]["enabled"] is False
        assert doc["rag_fusion"]["enabled"] is False
        assert "warning" in capsys.readouterr().err.lower()

    def test_reverts_only_present_section(self, tmp_path: Path) -> None:
        """Only [hyde] present → reverted, no crash on absent [rag_fusion]."""
        cfg = self._write(tmp_path, "[hyde]\nenabled = true\n")
        _revert_query_expansion_flags(cfg, dry_run=False)
        doc = tomlkit.parse(cfg.read_text())
        assert doc["hyde"]["enabled"] is False
        assert "rag_fusion" not in doc

    def test_dry_run_is_noop(self, tmp_path: Path) -> None:
        cfg = self._write(tmp_path, "[hyde]\nenabled = true\n")
        _revert_query_expansion_flags(cfg, dry_run=True)
        assert tomlkit.parse(cfg.read_text())["hyde"]["enabled"] is True

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        _revert_query_expansion_flags(tmp_path / "nope.toml", dry_run=False)  # must not raise

    def test_strips_provider_keys(self, tmp_path: Path) -> None:
        cfg = self._write(tmp_path, '[hyde]\nenabled = true\nprovider = "ollama"\nmodel = "x"\nollama_base_url = "http://h"\n')
        _revert_query_expansion_flags(cfg, dry_run=False)
        doc = tomlkit.parse(cfg.read_text())
        assert doc["hyde"]["enabled"] is False
        assert "provider" not in doc["hyde"]
        assert "model" not in doc["hyde"]
        assert "ollama_base_url" not in doc["hyde"]

    def test_sections_kwarg_limits_scope(self, tmp_path: Path) -> None:
        cfg = self._write(tmp_path, "[hyde]\nenabled = true\n[rag_fusion]\nenabled = true\n")
        _revert_query_expansion_flags(cfg, dry_run=False, sections=("rag_fusion",))
        doc = tomlkit.parse(cfg.read_text())
        assert doc["hyde"]["enabled"] is True
        assert doc["rag_fusion"]["enabled"] is False


# ---------------------------------------------------------------------------
# _assert_features_persisted — post-write persistence assertion (Q1)
# ---------------------------------------------------------------------------


class TestAssertFeaturesPersisted:
    def _cfg(self, hyde_enabled: bool = False, rag_enabled: bool = False):
        from archon_search.config import HyDEConfig, RAGFusionConfig, SearchConfig  # noqa: PLC0415

        cfg = SearchConfig()
        cfg.hyde = HyDEConfig(enabled=hyde_enabled)
        cfg.rag_fusion = RAGFusionConfig(enabled=rag_enabled)
        return cfg

    def test_ok_when_hyde_persisted(self) -> None:
        _assert_features_persisted(self._cfg(hyde_enabled=True), WizardFeatures(enable_hyde=True))

    def test_raises_when_hyde_not_persisted(self) -> None:
        with pytest.raises(InstallError, match="hyde"):
            _assert_features_persisted(self._cfg(hyde_enabled=False), WizardFeatures(enable_hyde=True))

    def test_raises_when_rag_fusion_not_persisted(self) -> None:
        with pytest.raises(InstallError, match="rag_fusion"):
            _assert_features_persisted(
                self._cfg(rag_enabled=False), WizardFeatures(enable_rag_fusion=True)
            )

    def test_noop_when_feature_not_requested(self) -> None:
        # Wizard did not enable hyde; disabled on disk is correct → no raise.
        _assert_features_persisted(self._cfg(hyde_enabled=False), WizardFeatures())


# ---------------------------------------------------------------------------
# _render_summary — HyDE / RAG Fusion bullets (mandatory confirmation)
# ---------------------------------------------------------------------------


class TestRenderSummaryQueryExpansion:
    def test_hyde_bullet_shows_provider(self) -> None:
        profile = get_profile("minimal", multilingual=False)
        out = _render_summary(
            "minimal", profile, multilingual=False, providers=[],
            features=WizardFeatures(enable_hyde=True, hyde_provider="anthropic"),
        )
        assert "Optional features" in out
        assert "HyDE: enabled (provider: anthropic)" in out
        assert "RAG Fusion" not in out

    def test_rag_fusion_bullet_shows_provider(self) -> None:
        profile = get_profile("minimal", multilingual=False)
        out = _render_summary(
            "minimal", profile, multilingual=False, providers=[],
            features=WizardFeatures(enable_rag_fusion=True, rag_fusion_provider="ollama"),
        )
        assert "RAG Fusion: enabled (provider: ollama)" in out

    def test_no_bullets_when_disabled(self) -> None:
        profile = get_profile("minimal", multilingual=False)
        out = _render_summary(
            "minimal", profile, multilingual=False, providers=[], features=WizardFeatures(),
        )
        assert "HyDE" not in out
        assert "RAG Fusion" not in out

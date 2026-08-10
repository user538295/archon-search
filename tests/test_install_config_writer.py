"""Tests for _write_profile_config, _profile_toml, and configure_providers durable-write fix."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from archon_search.config import load_config
from archon_search.install import WizardFeatures
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES


# ---------------------------------------------------------------------------
# _write_profile_config tests
# ---------------------------------------------------------------------------


class TestWriteProfileConfig:
    def test_write_profile_config_fresh_file(self, tmp_path: Path) -> None:
        """Creates config file; load_config returns correct embedding_model, chunk_size, profile, multilingual."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["minimal"]
        _write_profile_config(config_path, profile, "minimal", False)

        assert config_path.exists()
        cfg = load_config(config_path)
        assert cfg.embedding_model == profile.embedder
        assert cfg.chunk_size == profile.chunk_size
        assert cfg.profile == "minimal"
        assert cfg.multilingual is False

    def test_write_profile_config_updates_existing(self, tmp_path: Path) -> None:
        """Existing TOML with other sections preserved; only [database] updated."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[server]\nhost = \"localhost\"\nport = 9999\n\n[database]\ndb_path = \"/custom/path\"\n")

        profile = ENGLISH_PROFILES["balanced"]
        _write_profile_config(config_path, profile, "balanced", False)

        doc = tomlkit.parse(config_path.read_text())
        assert doc["server"]["port"] == 9999
        assert doc["database"]["embedding_model"] == profile.embedder
        assert doc["database"]["profile"] == "balanced"

    def test_write_profile_config_preserves_server_and_logging_sections(self, tmp_path: Path) -> None:
        """Non-[database] sections are unchanged after calling _write_profile_config."""
        from archon_search.install import _write_profile_config

        initial_toml = (
            "[server]\nhost = \"0.0.0.0\"\nport = 8888\n\n"
            "[database]\ndb_path = \"/some/path\"\n\n"
            "[logging]\nlevel = \"DEBUG\"\n\n"
            "[telemetry]\nenabled = false\n\n"
            "[collections]\nwatch = true\n"
        )
        config_path = tmp_path / "archon-search.toml"
        config_path.write_text(initial_toml)

        profile = ENGLISH_PROFILES["max"]
        _write_profile_config(config_path, profile, "max", False)

        doc = tomlkit.parse(config_path.read_text())
        assert doc["server"]["host"] == "0.0.0.0"
        assert doc["server"]["port"] == 8888
        assert doc["logging"]["level"] == "DEBUG"
        assert doc["telemetry"]["enabled"] is False
        assert doc["collections"]["watch"] is True

    def test_write_profile_config_no_reranker_writes_empty_string(self, tmp_path: Path) -> None:
        """multilingual minimal profile (reranker=None) writes reranker_model = ''."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = MULTILINGUAL_PROFILES["minimal"]
        assert profile.reranker is None

        _write_profile_config(config_path, profile, "minimal", True)

        doc = tomlkit.parse(config_path.read_text())
        assert doc["database"]["reranker_model"] == ""

    def test_write_profile_config_is_atomic(self, tmp_path: Path) -> None:
        """atomic_write_bytes is called with the config path and correct content."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["minimal"]

        with patch("archon_search.install.config_writer.atomic_write_bytes") as mock_atomic:
            _write_profile_config(config_path, profile, "minimal", False)

        mock_atomic.assert_called_once()
        call_args = mock_atomic.call_args
        assert call_args[0][0] == config_path
        written_bytes = call_args[0][1]
        doc = tomlkit.parse(written_bytes.decode())
        assert doc["database"]["embedding_model"] == profile.embedder
        assert doc["database"]["profile"] == "minimal"

    def test_write_profile_config_cleans_stale_tmp_before_write(self, tmp_path: Path) -> None:
        """Stale .tmp file is removed before calling atomic_write_bytes."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        stale_tmp = config_path.with_suffix(config_path.suffix + ".tmp")
        stale_tmp.write_text("stale content")
        assert stale_tmp.exists()

        profile = ENGLISH_PROFILES["balanced"]
        _write_profile_config(config_path, profile, "balanced", False)

        assert config_path.exists()
        assert not stale_tmp.exists()


# ---------------------------------------------------------------------------
# _profile_toml tests
# ---------------------------------------------------------------------------


class TestProfileToml:
    def test_profile_toml_fresh_minimal(self, tmp_path: Path) -> None:
        """_profile_toml('minimal', False) generates correct TOML for English minimal profile."""
        from archon_search.install import _profile_toml

        toml_str = _profile_toml("minimal", False)
        doc = tomlkit.parse(toml_str)
        assert {"server", "database", "routing", "collections", "logging"} <= set(doc.keys())

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text(toml_str)

        cfg = load_config(config_path)
        assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"
        assert cfg.profile == "minimal"
        assert cfg.multilingual is False

    def test_profile_toml_fresh_max_multilingual(self, tmp_path: Path) -> None:
        """_profile_toml('max', True) generates correct TOML for multilingual max profile."""
        from archon_search.install import _profile_toml

        toml_str = _profile_toml("max", True)
        doc = tomlkit.parse(toml_str)
        assert {"server", "database", "routing", "collections", "logging"} <= set(doc.keys())

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text(toml_str)

        cfg = load_config(config_path)
        assert cfg.embedding_model == MULTILINGUAL_PROFILES["max"].embedder
        assert cfg.profile == "max"
        assert cfg.multilingual is True


# ---------------------------------------------------------------------------
# configure_providers durable-write tests
# ---------------------------------------------------------------------------


class TestConfigureProvidersDurableWrite:
    def test_configure_providers_uses_durable_write(self, tmp_path: Path) -> None:
        """configure_providers uses atomic_write_bytes, not write_text."""
        from archon_search.install import RealInstaller

        config_file = tmp_path / "archon-search.toml"
        config_file.write_text("[database]\n")

        installer = RealInstaller.__new__(RealInstaller)
        installer.dry_run = False
        installer.config_file = str(config_file)

        from unittest.mock import MagicMock
        installer.cfg = MagicMock()
        installer.cfg.embedding_model = "BAAI/bge-small-en-v1.5"

        with patch("archon_search.install.installer.atomic_write_bytes") as mock_atomic:
            installer.configure_providers(gpu="cuda")

        mock_atomic.assert_called_once()
        call_path = mock_atomic.call_args[0][0]
        assert call_path == config_file

    def test_configure_providers_atomic_write_receives_encoded_toml(self, tmp_path: Path) -> None:
        """atomic_write_bytes receives bytes containing the updated provider config."""
        from archon_search.install import RealInstaller
        from unittest.mock import MagicMock

        config_file = tmp_path / "archon-search.toml"
        config_file.write_text("[database]\n")

        installer = RealInstaller.__new__(RealInstaller)
        installer.dry_run = False
        installer.config_file = str(config_file)
        installer.cfg = MagicMock()
        installer.cfg.embedding_model = "BAAI/bge-small-en-v1.5"

        with patch("archon_search.install.installer.atomic_write_bytes") as mock_atomic:
            installer.configure_providers(gpu="cuda")

        written = mock_atomic.call_args[0][1]
        assert isinstance(written, bytes)
        doc = tomlkit.parse(written.decode())
        assert "CUDAExecutionProvider" in doc["database"]["providers"]


# ---------------------------------------------------------------------------
# _apply_wizard_features_to_toml tests (Task C8-2.1)
# ---------------------------------------------------------------------------


class TestApplyWizardFeaturesToToml:
    def _empty_doc(self) -> tomlkit.TOMLDocument:
        return tomlkit.document()

    def test_apply_defaults_creates_mandatory_sections_but_omits_default_keys(self) -> None:
        """WizardFeatures() creates mandatory sections but omits default-valued keys (S561)."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures())
        # hyde/rag_fusion always write their enabled key (not via _set_or_remove)
        assert doc["hyde"]["enabled"] is False
        assert doc["rag_fusion"]["enabled"] is False
        # default-valued keys are NOT written (20_wizard.md §"Only the choices you actually made are written")
        assert "eager_load_embedders" not in doc.get("database", {})
        assert "watch" not in doc.get("collections", {})
        assert "enabled" not in doc.get("telemetry", {})
        assert "routing_strategy" not in doc.get("routing", {})
        assert "format" not in doc.get("logging", {})
        # sections ARE created so config_show can find them
        assert "database" in doc
        assert "collections" in doc
        assert "telemetry" in doc
        assert "routing" in doc
        assert "logging" in doc

    def test_apply_disable_reranker(self) -> None:
        """disable_reranker=True writes doc['database']['reranker_model'] = ''."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(disable_reranker=True))
        assert doc["database"]["reranker_model"] == ""

    def test_apply_enable_watch(self) -> None:
        """enable_watch=True writes doc['collections']['watch'] = True."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_watch=True))
        assert doc["collections"]["watch"] is True

    def test_apply_enable_telemetry(self) -> None:
        """enable_telemetry=True writes doc['telemetry']['enabled'] = True."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_telemetry=True))
        assert doc["telemetry"]["enabled"] is True

    def test_apply_eager_load(self) -> None:
        """eager_load_embedders=True writes doc['database']['eager_load_embedders'] = True."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(eager_load_embedders=True))
        assert doc["database"]["eager_load_embedders"] is True

    def test_apply_routing_hybrid(self) -> None:
        """routing_strategy='hybrid' writes doc['routing']['routing_strategy'] = 'hybrid'."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(routing_strategy="hybrid"))
        assert doc["routing"]["routing_strategy"] == "hybrid"

    def test_apply_log_format_json(self) -> None:
        """log_format='json' writes doc['logging']['format'] = 'json'."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(log_format="json"))
        assert doc["logging"]["format"] == "json"

    def test_apply_creates_missing_sections(self) -> None:
        """Sections absent before call are created correctly."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        features = WizardFeatures(
            disable_reranker=True,
            enable_watch=True,
            enable_telemetry=True,
            eager_load_embedders=True,
            routing_strategy="hybrid",
            log_format="json",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert "database" in doc
        assert "collections" in doc
        assert "telemetry" in doc
        assert "routing" in doc
        assert "logging" in doc

    def test_apply_preserves_existing_sections(self) -> None:
        """Other TOML content is untouched after applying features."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.parse("[server]\nhost = \"localhost\"\nport = 9999\n\n[database]\ndb_path = \"/data\"\n")
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_watch=True))
        # Pre-existing content preserved
        assert doc["server"]["host"] == "localhost"
        assert doc["server"]["port"] == 9999
        assert doc["database"]["db_path"] == "/data"
        # New content added
        assert doc["collections"]["watch"] is True

    def test_apply_install_code_extra_not_written_to_toml(self) -> None:
        """WizardFeatures(install_code_extra=True) does not write any 'install_code_extra' key."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(install_code_extra=True))
        doc_str = tomlkit.dumps(doc)
        assert "install_code_extra" not in doc_str

    def test_apply_install_multilingual_extra_not_written_to_toml(self) -> None:
        """WizardFeatures(install_multilingual_extra=True) writes no 'install_multilingual_extra' key.

        The multilingual state the server reads is [database].multilingual, written
        separately from the profile. The install flag itself is not a config key.
        """
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(install_multilingual_extra=True))
        assert "install_multilingual_extra" not in tomlkit.dumps(doc)

    # --- Task C15-1.2 tests ---

    def test_apply_host_writes_server_section(self) -> None:
        """host='0.0.0.0' writes doc['server']['host'] = '0.0.0.0'."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(host="0.0.0.0"))
        assert doc["server"]["host"] == "0.0.0.0"

    def test_apply_port_writes_server_section(self) -> None:
        """port=9000 writes doc['server']['port'] = 9000."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(port=9000))
        assert doc["server"]["port"] == 9000

    def test_apply_db_path_writes_database_section(self) -> None:
        """db_path='~/custom' writes doc['database']['db_path'] = '~/custom'."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(db_path="~/custom"))
        assert doc["database"]["db_path"] == "~/custom"

    def test_apply_log_level_writes_logging_section(self) -> None:
        """log_level='DEBUG' writes doc['logging']['level'] = 'DEBUG'."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(log_level="DEBUG"))
        assert doc["logging"]["level"] == "DEBUG"

    def test_apply_top_k_writes_both_keys(self) -> None:
        """top_k=10 writes top_k_return=10 and top_k_retrieve=30."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(top_k=10))
        assert doc["database"]["top_k_return"] == 10
        assert doc["database"]["top_k_retrieve"] == 30

    def test_apply_top_k_1_sets_retrieve_to_15(self) -> None:
        """top_k=1 writes top_k_retrieve=15 (max guard: max(15, 3*1)=15)."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(top_k=1))
        assert doc["database"]["top_k_return"] == 1
        assert doc["database"]["top_k_retrieve"] == 15

    def test_apply_top_k_33_sets_retrieve_to_99(self) -> None:
        """top_k=33 writes top_k_retrieve=99 (max(15, 3*33)=99)."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(top_k=33))
        assert doc["database"]["top_k_return"] == 33
        assert doc["database"]["top_k_retrieve"] == 99

    def test_apply_top_k_none_does_not_write(self) -> None:
        """top_k=None does not write any top_k keys."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(top_k=None))
        if "database" in doc:
            assert "top_k_return" not in doc["database"]
            assert "top_k_retrieve" not in doc["database"]

    def test_apply_telemetry_retention_with_telemetry_enabled(self) -> None:
        """enable_telemetry=True + telemetry_retention_days=7 writes [telemetry].retention_days=7."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(
            doc, WizardFeatures(enable_telemetry=True, telemetry_retention_days=7)
        )
        assert doc["telemetry"]["retention_days"] == 7

    def test_apply_telemetry_retention_without_telemetry_skipped(self) -> None:
        """telemetry_retention_days=7 without enable_telemetry does NOT write retention_days."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(
            doc, WizardFeatures(enable_telemetry=False, telemetry_retention_days=7)
        )
        if "telemetry" in doc:
            assert "retention_days" not in doc["telemetry"]

    def test_apply_enable_hyde(self) -> None:
        """enable_hyde=True writes doc['hyde']['enabled'] = True."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_hyde=True))
        assert doc["hyde"]["enabled"] is True

    def test_apply_enable_rag_fusion(self) -> None:
        """enable_rag_fusion=True writes doc['rag_fusion']['enabled'] = True."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_rag_fusion=True))
        assert doc["rag_fusion"]["enabled"] is True

    def test_apply_all_new_fields_together(self) -> None:
        """All new non-default fields set; assert all expected keys present in doc."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        features = WizardFeatures(
            host="0.0.0.0",
            port=9000,
            db_path="~/custom",
            log_level="WARNING",
            top_k=20,
            enable_telemetry=True,
            telemetry_retention_days=14,
            enable_hyde=True,
            enable_rag_fusion=True,
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["server"]["host"] == "0.0.0.0"
        assert doc["server"]["port"] == 9000
        assert doc["database"]["db_path"] == "~/custom"
        assert doc["database"]["top_k_return"] == 20
        assert doc["database"]["top_k_retrieve"] == 60
        assert doc["logging"]["level"] == "WARNING"
        assert "log_file" not in doc["logging"]
        assert doc["telemetry"]["retention_days"] == 14
        assert doc["hyde"]["enabled"] is True
        assert doc["rag_fusion"]["enabled"] is True

    def test_apply_hyde_false_writes_enabled_false(self) -> None:
        """enable_hyde=False writes doc['hyde']['enabled'] = False (creates section if absent)."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_hyde=False))
        assert doc["hyde"]["enabled"] is False

    def test_apply_rag_fusion_false_writes_enabled_false(self) -> None:
        """enable_rag_fusion=False writes doc['rag_fusion']['enabled'] = False."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_rag_fusion=False))
        assert doc["rag_fusion"]["enabled"] is False

    def test_rerun_disable_hyde_overwrites_existing_true(self) -> None:
        """Re-run with enable_hyde=False overwrites a previously written enabled = true."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.parse("[hyde]\nenabled = true\n")
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_hyde=False))
        assert doc["hyde"]["enabled"] is False

    def test_rerun_disable_rag_fusion_overwrites_existing_true(self) -> None:
        """Re-run with enable_rag_fusion=False overwrites a previously written enabled = true."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.parse("[rag_fusion]\nenabled = true\n")
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_rag_fusion=False))
        assert doc["rag_fusion"]["enabled"] is False

    def test_disable_hyde_preserves_other_keys(self) -> None:
        """Disabling HyDE writes enabled=false but leaves provider/model intact."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.parse('[hyde]\nenabled = true\nprovider = "ollama"\nmodel = "llama3"\n')
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_hyde=False))
        assert doc["hyde"]["enabled"] is False
        assert doc["hyde"]["provider"] == "ollama"
        assert doc["hyde"]["model"] == "llama3"


class TestApplyWizardFeaturesFE2LlamaCppAndGraph:
    """FE-2: llama_cpp_base_url threading for [hyde]/[rag_fusion], and [graph] enrichment."""

    def _empty_doc(self) -> tomlkit.TOMLDocument:
        return tomlkit.document()

    def test_apply_wizard_features_writes_llama_cpp_base_url_to_hyde_rag_fusion(self) -> None:
        """A custom llama_cpp_base_url is written under both [hyde] and [rag_fusion]."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        features = WizardFeatures(
            enable_hyde=True,
            hyde_provider="llama_cpp",
            hyde_model="m1",
            hyde_llama_cpp_base_url="http://hyde-box:8080",
            enable_rag_fusion=True,
            rag_fusion_provider="llama_cpp",
            rag_fusion_model="m2",
            rag_fusion_llama_cpp_base_url="http://rag-box:8080",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["hyde"]["provider"] == "llama_cpp"
        assert doc["hyde"]["llama_cpp_base_url"] == "http://hyde-box:8080"
        assert doc["rag_fusion"]["provider"] == "llama_cpp"
        assert doc["rag_fusion"]["llama_cpp_base_url"] == "http://rag-box:8080"

    def test_hyde_llama_cpp_default_base_url_omits_key(self) -> None:
        """Empty hyde_llama_cpp_base_url (built-in default) writes no key."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        features = WizardFeatures(
            enable_hyde=True, hyde_provider="llama_cpp", hyde_model="m1", hyde_llama_cpp_base_url=""
        )
        _apply_wizard_features_to_toml(doc, features)
        assert "llama_cpp_base_url" not in doc["hyde"]

    def test_graph_provider_writes_all_three_fields(self) -> None:
        """graph_provider set writes provider/extraction_model/llama_cpp_base_url to [graph]."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        features = WizardFeatures(
            graph_provider="llama_cpp",
            graph_extraction_model="qwen2.5-coder",
            graph_llama_cpp_base_url="http://graph-box:8080",
        )
        _apply_wizard_features_to_toml(doc, features)
        assert doc["graph"]["provider"] == "llama_cpp"
        assert doc["graph"]["extraction_model"] == "qwen2.5-coder"
        assert doc["graph"]["llama_cpp_base_url"] == "http://graph-box:8080"

    def test_graph_provider_anthropic_omits_llama_cpp_base_url(self) -> None:
        """A non-llama_cpp graph provider writes provider/extraction_model but no base URL key."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        features = WizardFeatures(graph_provider="anthropic", graph_extraction_model="claude-haiku-4-5")
        _apply_wizard_features_to_toml(doc, features)
        assert doc["graph"]["provider"] == "anthropic"
        assert doc["graph"]["extraction_model"] == "claude-haiku-4-5"
        assert "llama_cpp_base_url" not in doc["graph"]

    def test_graph_provider_empty_writes_nothing(self) -> None:
        """graph_provider='' (declined enrichment) writes no [graph] section at all."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._empty_doc()
        _apply_wizard_features_to_toml(doc, WizardFeatures())
        assert "graph" not in doc


# ---------------------------------------------------------------------------
# Brief 150: re-run wizard with settings disabled writes the off/default value
# ---------------------------------------------------------------------------


class TestApplyWizardFeaturesElseBranches:
    """S561: keys equal to their default are REMOVED (not written) — 20_wizard.md §"Only the choices you actually made are written"."""

    def _enabled_doc(self) -> tomlkit.TOMLDocument:
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(
            doc,
            WizardFeatures(
                eager_load_embedders=True,
                enable_watch=True,
                enable_telemetry=True,
                routing_strategy="hybrid",
                log_format="json",
            ),
        )
        return doc

    def test_rerun_with_defaults_removes_all_five_settings(self) -> None:
        """Re-running wizard with defaults REMOVES each previously-enabled key (S561)."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = self._enabled_doc()
        assert doc["database"]["eager_load_embedders"] is True
        assert doc["collections"]["watch"] is True
        assert doc["telemetry"]["enabled"] is True
        assert doc["routing"]["routing_strategy"] == "hybrid"
        assert doc["logging"]["format"] == "json"

        _apply_wizard_features_to_toml(doc, WizardFeatures())

        assert "eager_load_embedders" not in doc.get("database", {})
        assert "watch" not in doc.get("collections", {})
        assert "enabled" not in doc.get("telemetry", {})
        assert "routing_strategy" not in doc.get("routing", {})
        assert "format" not in doc.get("logging", {})

    def test_eager_load_default_not_written(self) -> None:
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures(eager_load_embedders=False))
        assert "eager_load_embedders" not in doc.get("database", {})

    def test_watch_default_not_written(self) -> None:
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_watch=False))
        assert "watch" not in doc.get("collections", {})

    def test_telemetry_default_not_written(self) -> None:
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures(enable_telemetry=False))
        assert "enabled" not in doc.get("telemetry", {})

    def test_default_routing_strategy_not_written(self) -> None:
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures())
        assert "routing_strategy" not in doc.get("routing", {})

    def test_default_log_format_not_written(self) -> None:
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures())
        assert "format" not in doc.get("logging", {})

    def test_explicit_default_routing_strategy_is_written(self) -> None:
        """S561: an explicitly-chosen 'centroid' IS written, unlike the None sentinel."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures(routing_strategy="centroid"))
        assert doc["routing"]["routing_strategy"] == "centroid"

    def test_explicit_default_log_format_is_written(self) -> None:
        """S561: an explicitly-chosen 'text' IS written, unlike the None sentinel."""
        from archon_search.install import _apply_wizard_features_to_toml

        doc = tomlkit.document()
        _apply_wizard_features_to_toml(doc, WizardFeatures(log_format="text"))
        assert doc["logging"]["format"] == "text"


# ---------------------------------------------------------------------------
# Task C8-2.2: extend _write_profile_config() and _profile_toml() with features
# ---------------------------------------------------------------------------


class TestWriteProfileConfigWithFeatures:
    def test_write_profile_with_features_telemetry(self, tmp_path: Path) -> None:
        """_write_profile_config with enable_telemetry=True writes [telemetry].enabled = true."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["minimal"]
        features = WizardFeatures(enable_telemetry=True)
        _write_profile_config(config_path, profile, "minimal", False, features=features)

        cfg = load_config(config_path)
        assert cfg.telemetry.enabled is True
        # Profile fields preserved
        assert cfg.embedding_model == profile.embedder
        assert cfg.profile == "minimal"

    def test_write_profile_no_features(self, tmp_path: Path) -> None:
        """Backward-compatible: existing test_write_profile_config_fresh_file still passes."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["minimal"]
        _write_profile_config(config_path, profile, "minimal", False)

        assert config_path.exists()
        cfg = load_config(config_path)
        assert cfg.embedding_model == profile.embedder
        assert cfg.chunk_size == profile.chunk_size
        assert cfg.profile == "minimal"
        assert cfg.multilingual is False

    def test_write_profile_with_features_watch(self, tmp_path: Path) -> None:
        """_write_profile_config with enable_watch=True writes [collections].watch = true."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["balanced"]
        features = WizardFeatures(enable_watch=True)
        _write_profile_config(config_path, profile, "balanced", False, features=features)

        cfg = load_config(config_path)
        assert cfg.watch is True

    def test_write_profile_with_multiple_features(self, tmp_path: Path) -> None:
        """Multiple features are all written correctly."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["minimal"]
        features = WizardFeatures(
            disable_reranker=True,
            enable_watch=True,
            enable_telemetry=True,
            routing_strategy="hybrid",
            log_format="json",
        )
        _write_profile_config(config_path, profile, "minimal", False, features=features)

        cfg = load_config(config_path)
        assert cfg.reranker_model == ""
        assert cfg.watch is True
        assert cfg.telemetry.enabled is True
        assert cfg.routing_strategy == "hybrid"
        assert cfg.log_format == "json"

    def test_write_profile_features_none_leaves_no_optional_keys(self, tmp_path: Path) -> None:
        """features=None (default) does not write optional feature keys to TOML."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["minimal"]
        _write_profile_config(config_path, profile, "minimal", False, features=None)

        doc = tomlkit.parse(config_path.read_text())
        # Optional sections should not be present (no telemetry, collections, routing, logging from features)
        assert "telemetry" not in doc
        assert "routing" not in doc


class TestProfileTomlWithFeatures:
    def test_profile_toml_with_features_watch(self, tmp_path: Path) -> None:
        """_profile_toml('minimal', False, WizardFeatures(enable_watch=True)) contains [collections].watch = true."""
        from archon_search.install import _profile_toml

        features = WizardFeatures(enable_watch=True)
        toml_str = _profile_toml("minimal", False, features=features)

        doc = tomlkit.parse(toml_str)
        assert doc["collections"]["watch"] is True

    def test_profile_toml_no_features_backward_compatible(self, tmp_path: Path) -> None:
        """_profile_toml without features argument is backward-compatible."""
        from archon_search.install import _profile_toml

        toml_str = _profile_toml("minimal", False)
        doc = tomlkit.parse(toml_str)
        assert {"server", "database", "routing", "collections", "logging"} <= set(doc.keys())

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text(toml_str)
        cfg = load_config(config_path)
        assert cfg.profile == "minimal"

    def test_profile_toml_with_features_telemetry(self, tmp_path: Path) -> None:
        """_profile_toml with enable_telemetry=True produces TOML that loads with telemetry.enabled=True."""
        from archon_search.install import _profile_toml

        features = WizardFeatures(enable_telemetry=True)
        toml_str = _profile_toml("minimal", False, features=features)

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text(toml_str)
        cfg = load_config(config_path)
        assert cfg.telemetry.enabled is True

    def test_profile_toml_with_all_features(self, tmp_path: Path) -> None:
        """_profile_toml with all features enabled returns correct config after load."""
        from archon_search.install import _profile_toml

        features = WizardFeatures(
            disable_reranker=True,
            enable_watch=True,
            enable_telemetry=True,
            eager_load_embedders=True,
            routing_strategy="hybrid",
            log_format="json",
        )
        toml_str = _profile_toml("balanced", False, features=features)

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text(toml_str)
        cfg = load_config(config_path)
        assert cfg.reranker_model == ""
        assert cfg.watch is True
        assert cfg.telemetry.enabled is True
        assert cfg.eager_load_embedders is True
        assert cfg.routing_strategy == "hybrid"
        assert cfg.log_format == "json"


class TestLoadConfigAfterWriteWithFeatures:
    def test_load_config_after_write_with_features(self, tmp_path: Path) -> None:
        """Round-trip: write config with features via _write_profile_config, load_config returns correct values."""
        from archon_search.install import _write_profile_config

        config_path = tmp_path / "archon-search.toml"
        profile = ENGLISH_PROFILES["balanced"]
        features = WizardFeatures(
            enable_watch=True,
            enable_telemetry=True,
            eager_load_embedders=True,
            routing_strategy="hybrid",
            log_format="json",
        )
        _write_profile_config(config_path, profile, "balanced", False, features=features)

        cfg = load_config(config_path)
        assert cfg.watch is True
        assert cfg.telemetry.enabled is True
        assert cfg.eager_load_embedders is True
        assert cfg.routing_strategy == "hybrid"
        assert cfg.log_format == "json"
        # Profile fields still correct
        assert cfg.embedding_model == profile.embedder
        assert cfg.profile == "balanced"


class TestRevertMultilingualFlag:
    """Unit tests for _revert_multilingual_flag() — mirrors _revert_graph_enabled_flag."""

    def test_reverts_multilingual_true_to_false(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A config with [database].multilingual=true is rewritten to false."""
        from archon_search.install import _revert_multilingual_flag

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[database]\nmultilingual = true\n")
        _revert_multilingual_flag(config_path, dry_run=False)
        doc = tomlkit.parse(config_path.read_text())
        assert doc["database"]["multilingual"] is False

        captured = capsys.readouterr()
        assert "multilingual" in captured.err.lower()
        assert "revert" in captured.err.lower()
        assert "english-only" in captured.err.lower()

    def test_dry_run_is_noop(self, tmp_path: Path) -> None:
        """dry_run=True must not touch the file."""
        from archon_search.install import _revert_multilingual_flag

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[database]\nmultilingual = true\n")
        _revert_multilingual_flag(config_path, dry_run=True)
        doc = tomlkit.parse(config_path.read_text())
        assert doc["database"]["multilingual"] is True

    def test_missing_config_is_noop(self, tmp_path: Path) -> None:
        """Absent config file must not raise."""
        from archon_search.install import _revert_multilingual_flag

        config_path = tmp_path / "does-not-exist.toml"
        _revert_multilingual_flag(config_path, dry_run=False)  # must not raise
        assert not config_path.exists()

    def test_revert_flips_crashing_config_to_startable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reverting multilingual=true turns a crash-at-startup config into a startable one."""
        from archon_search.install import _revert_multilingual_flag
        from archon_search.server.app import _check_multilingual_deps

        config_path = tmp_path / "archon-search.toml"
        config_path.write_text("[database]\nmultilingual = true\n")

        # Simulate fasttext-wheel absent: `import fasttext` raises ImportError.
        monkeypatch.setitem(sys.modules, "fasttext", None)

        with pytest.raises(RuntimeError):
            _check_multilingual_deps(load_config(config_path))

        _revert_multilingual_flag(config_path, dry_run=False)

        # No exception — multilingual=false is a no-op for the dependency check.
        _check_multilingual_deps(load_config(config_path))


# ---------------------------------------------------------------------------
# WizardFeatures ↔ SearchConfig default parity guard
# ---------------------------------------------------------------------------


def test_wizard_features_defaults_match_search_config() -> None:
    """Guard that WizardFeatures defaults stay in sync with SearchConfig defaults.

    _set_or_remove deletes keys when value == default, trusting that the
    SearchConfig default will apply. If these diverge, a deletion silently
    activates a different default. See S561 and C1-I-5.
    """
    from archon_search.config import SearchConfig
    from archon_search.install import WizardFeatures

    wf = WizardFeatures()
    cfg = SearchConfig()
    assert wf.eager_load_embedders == cfg.eager_load_embedders
    assert wf.enable_watch == cfg.watch
    assert wf.enable_telemetry == cfg.telemetry.enabled
    # routing_strategy and log_format carry a None sentinel meaning "accepted default,
    # omit the key" — they are deliberately NOT the config default string, so the
    # equality guard above does not apply. Pin both sides instead.
    assert wf.routing_strategy is None, "None sentinel = accepted default (omitted from config)"
    assert wf.log_format is None, "None sentinel = accepted default (omitted from config)"
    assert cfg.routing_strategy == "centroid"
    assert cfg.log_format == "text"

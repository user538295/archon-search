"""Tests for _write_profile_config, _profile_toml, and configure_providers durable-write fix."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from archon_search.config import load_config
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

        with patch("archon_search.install.atomic_write_bytes") as mock_atomic:
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
        from archon_search.install import SearchInstaller

        config_file = tmp_path / "archon-search.toml"
        config_file.write_text("[database]\n")

        installer = SearchInstaller.__new__(SearchInstaller)
        installer.dry_run = False
        installer.config_file = str(config_file)

        from unittest.mock import MagicMock
        installer.cfg = MagicMock()
        installer.cfg.embedding_model = "BAAI/bge-small-en-v1.5"

        with patch("archon_search.install.atomic_write_bytes") as mock_atomic:
            installer.configure_providers(gpu="cuda")

        mock_atomic.assert_called_once()
        call_path = mock_atomic.call_args[0][0]
        assert call_path == config_file

    def test_configure_providers_atomic_write_receives_encoded_toml(self, tmp_path: Path) -> None:
        """atomic_write_bytes receives bytes containing the updated provider config."""
        from archon_search.install import SearchInstaller
        from unittest.mock import MagicMock

        config_file = tmp_path / "archon-search.toml"
        config_file.write_text("[database]\n")

        installer = SearchInstaller.__new__(SearchInstaller)
        installer.dry_run = False
        installer.config_file = str(config_file)
        installer.cfg = MagicMock()
        installer.cfg.embedding_model = "BAAI/bge-small-en-v1.5"

        with patch("archon_search.install.atomic_write_bytes") as mock_atomic:
            installer.configure_providers(gpu="cuda")

        written = mock_atomic.call_args[0][1]
        assert isinstance(written, bytes)
        doc = tomlkit.parse(written.decode())
        assert "CUDAExecutionProvider" in doc["database"]["providers"]

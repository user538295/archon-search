"""Tests for archon_search.config (Task 1.3 — Standalone config loader)."""

from pathlib import Path

import pytest

from archon_search.config import ConfigError, SearchConfig, get_default_config_path, load_config, save_config


def test_load_config_defaults_when_no_file(tmp_path: Path) -> None:
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert isinstance(config, SearchConfig)
    assert config.host == "127.0.0.1"
    assert config.port == 8765


def test_load_config_from_file(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[server]\nhost = \"0.0.0.0\"\nport = 9000\n\n[database]\ndb_path = \"/custom/db\"\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.db_path == "/custom/db"


def test_load_config_custom_path(tmp_path: Path) -> None:
    toml_file = tmp_path / "custom.toml"
    toml_file.write_text("[server]\nport = 1234\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.port == 1234


def test_host_default_is_loopback() -> None:
    config = SearchConfig()
    assert config.host == "127.0.0.1"


def test_db_path_tilde_preserved() -> None:
    config = SearchConfig()
    assert "~" in config.db_path


def test_db_path_tilde_preserved_from_file(tmp_path: Path) -> None:
    toml_file = tmp_path / "t.toml"
    toml_file.write_text('[database]\ndb_path = "~/my/db"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.db_path == "~/my/db"


def test_load_config_routing_section(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[routing]\nrouting_shortlist_size = 5\nrouting_confidence_threshold = 0.5\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.routing_shortlist_size == 5
    assert config.routing_confidence_threshold == 0.5


def test_load_config_collections_section(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[collections]\npinned_collections = ["/path/a", "/path/b"]\nwatch = true\n',
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.pinned_collections == ["/path/a", "/path/b"]
    assert config.watch is True


def test_load_config_logging_section(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[logging]\nlevel = "DEBUG"\nlog_file = "/tmp/search.log"\n',
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.level == "DEBUG"
    assert config.log_file == "/tmp/search.log"


def test_get_default_config_path() -> None:
    path = get_default_config_path()
    assert path == Path.home() / ".archon" / "archon-search.toml"


def test_config_error_is_exception() -> None:
    err = ConfigError("bad value")
    assert isinstance(err, Exception)
    assert str(err) == "bad value"


def test_load_config_defaults_for_all_sections() -> None:
    config = SearchConfig()
    assert config.embedding_model == "BAAI/bge-small-en-v1.5"
    assert config.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert config.chunk_size == 512
    assert config.auto_reindex_on_chunk_size_change is True
    assert config.routing_shortlist_size == 8
    assert config.routing_confidence_threshold == 0.30
    assert config.max_parallel_collections == 3
    assert config.pinned_collections == []
    assert config.watch is False
    assert config.level == "INFO"


def test_load_config_corrupt_toml_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[invalid\ngarbage ===\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_load_config_invalid_port_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[server]\nport = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_load_config_providers_from_database_section(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[database]\nproviders = ["CUDAExecutionProvider"]\n',
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.providers == ["CUDAExecutionProvider"]


def test_load_config_providers_default_is_empty_list() -> None:
    config = SearchConfig()
    assert config.providers == []


# ---------------------------------------------------------------------------
# save_config tests
# ---------------------------------------------------------------------------


def test_save_config_round_trip(tmp_path: Path) -> None:
    """Load config, mutate collections, save, reload — collections match."""
    toml_file = tmp_path / "archon-search.toml"
    config = SearchConfig()
    config.collections = ["/path/a", "/path/b"]
    config.pinned_collections = ["/pinned/x"]

    save_config(config, toml_file)

    reloaded = load_config(path=toml_file)
    assert reloaded.collections == ["/path/a", "/path/b"]
    assert reloaded.pinned_collections == ["/pinned/x"]


def test_save_config_preserves_other_sections(tmp_path: Path) -> None:
    """save_config only touches [collections] keys; other TOML sections are preserved."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[server]\nhost = \"0.0.0.0\"\nport = 9000\n\n[database]\ndb_path = \"/custom/db\"\n",
        encoding="utf-8",
    )

    config = SearchConfig()
    config.collections = ["/new/path"]
    config.pinned_collections = []

    save_config(config, toml_file)

    reloaded = load_config(path=toml_file)
    # Other sections must be intact
    assert reloaded.host == "0.0.0.0"
    assert reloaded.port == 9000
    assert reloaded.db_path == "/custom/db"
    # Collections updated
    assert reloaded.collections == ["/new/path"]
    assert reloaded.pinned_collections == []

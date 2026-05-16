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


# ---------------------------------------------------------------------------
# C11.13–C11.22: explicit coverage tests
# ---------------------------------------------------------------------------


def test_c11_13_search_config_no_args_all_defaults_valid() -> None:
    """C11.13: SearchConfig() with no args → all defaults are valid values."""
    config = SearchConfig()
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.db_path == "~/.archon/search"
    assert config.embedding_model == "BAAI/bge-small-en-v1.5"
    assert config.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert config.chunk_size == 512
    assert config.auto_reindex_on_chunk_size_change is True
    assert config.providers == []
    assert config.routing_shortlist_size == 8
    assert config.routing_confidence_threshold == 0.30
    assert config.max_parallel_collections == 3
    assert config.pinned_collections == []
    assert config.collections == []
    assert config.watch is False
    assert config.level == "INFO"
    assert config.log_file == "~/.archon/logs/archon-search.log"
    assert config.namespaces == {}


def test_c11_14_nonexistent_path_returns_defaults(tmp_path: Path) -> None:
    """C11.14: non-existent path → returns SearchConfig() defaults."""
    config = load_config(path=tmp_path / "does_not_exist.toml")
    defaults = SearchConfig()
    assert config == defaults


def test_c11_15_invalid_toml_raises_config_error_with_cause(tmp_path: Path) -> None:
    """C11.15: invalid TOML → raises ConfigError with __cause__ set."""
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("[[[[not valid toml\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(path=bad_file)
    assert exc_info.value.__cause__ is not None


def test_c11_16_all_four_sections_populated(tmp_path: Path) -> None:
    """C11.16: TOML with all 4 sections ([server], [database], [routing], [collections]) → all fields populated."""
    toml_file = tmp_path / "full.toml"
    toml_file.write_text(
        "[server]\n"
        'host = "0.0.0.0"\n'
        "port = 9000\n\n"
        "[database]\n"
        'db_path = "/data/db"\n'
        'embedding_model = "custom/model"\n'
        'reranker_model = "custom/reranker"\n'
        "chunk_size = 256\n"
        "auto_reindex_on_chunk_size_change = false\n"
        'providers = ["CPUExecutionProvider"]\n\n'
        "[routing]\n"
        "routing_shortlist_size = 5\n"
        "routing_confidence_threshold = 0.75\n"
        "max_parallel_collections = 2\n\n"
        "[collections]\n"
        'pinned_collections = ["/pinned/a"]\n'
        'collections = ["/col/b"]\n'
        "watch = true\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.db_path == "/data/db"
    assert config.embedding_model == "custom/model"
    assert config.reranker_model == "custom/reranker"
    assert config.chunk_size == 256
    assert config.auto_reindex_on_chunk_size_change is False
    assert config.providers == ["CPUExecutionProvider"]
    assert config.routing_shortlist_size == 5
    assert config.routing_confidence_threshold == 0.75
    assert config.max_parallel_collections == 2
    assert config.pinned_collections == ["/pinned/a"]
    assert config.collections == ["/col/b"]
    assert config.watch is True


def test_c11_17_load_modify_save_reload_values_preserved(tmp_path: Path) -> None:
    """C11.17: load → modify → save → reload → values preserved."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[server]\nport = 9000\n\n[collections]\ncollections = []\npinned_collections = []\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    config.collections = ["/path/x", "/path/y"]
    config.pinned_collections = ["/pinned/z"]
    save_config(config, toml_file)

    reloaded = load_config(path=toml_file)
    assert reloaded.collections == ["/path/x", "/path/y"]
    assert reloaded.pinned_collections == ["/pinned/z"]
    # Other values from original file are preserved
    assert reloaded.port == 9000


def test_c11_18_save_to_nonexistent_path_creates_file(tmp_path: Path) -> None:
    """C11.18: save to nonexistent path → file created."""
    new_file = tmp_path / "subdir" / "new_config.toml"
    new_file.parent.mkdir(parents=True)
    config = SearchConfig()
    config.collections = ["/some/path"]
    save_config(config, new_file)
    assert new_file.exists()
    reloaded = load_config(path=new_file)
    assert reloaded.collections == ["/some/path"]


def test_c11_19_port_zero_raises_config_error(tmp_path: Path) -> None:
    """C11.19: port=0 → ConfigError."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[server]\nport = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_c11_19_port_65536_raises_config_error(tmp_path: Path) -> None:
    """C11.19: port=65536 → ConfigError."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[server]\nport = 65536\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_c11_20_chunk_size_zero_raises_config_error(tmp_path: Path) -> None:
    """C11.20: chunk_size=0 → ConfigError."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[database]\nchunk_size = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_c11_21_routing_shortlist_size_zero_raises_config_error(tmp_path: Path) -> None:
    """C11.21: routing_shortlist_size=0 → ConfigError."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[routing]\nrouting_shortlist_size = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_c11_22_routing_confidence_threshold_boundary_valid(tmp_path: Path) -> None:
    """C11.22: routing_confidence_threshold=0.0 and 1.0 are valid."""
    for value in (0.0, 1.0):
        toml_file = tmp_path / f"conf_{value}.toml"
        toml_file.write_text(f"[routing]\nrouting_confidence_threshold = {value}\n", encoding="utf-8")
        config = load_config(path=toml_file)
        assert config.routing_confidence_threshold == value


def test_c11_22_routing_confidence_threshold_below_zero_raises(tmp_path: Path) -> None:
    """C11.22: routing_confidence_threshold=-0.1 → ConfigError."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[routing]\nrouting_confidence_threshold = -0.1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_c11_22_routing_confidence_threshold_above_one_raises(tmp_path: Path) -> None:
    """C11.22: routing_confidence_threshold=1.1 → ConfigError."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[routing]\nrouting_confidence_threshold = 1.1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


# ---------------------------------------------------------------------------
# [namespaces] section tests
# ---------------------------------------------------------------------------


def test_config_namespaces_populated(tmp_path: Path) -> None:
    """TOML with [namespaces] section → config.namespaces populated."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[namespaces]\nkeyA = "tenantA"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.namespaces == {"keyA": "tenantA"}


def test_config_namespaces_absent(tmp_path: Path) -> None:
    """TOML with no [namespaces] section → config.namespaces == {}."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[server]\nport = 8765\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.namespaces == {}


def test_config_namespaces_empty_section(tmp_path: Path) -> None:
    """[namespaces] present but empty → config.namespaces == {}."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[namespaces]\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.namespaces == {}


def test_save_config_does_not_destroy_existing_namespaces(tmp_path: Path) -> None:
    """save_config() round-trip preserves [namespaces] entries unchanged."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[namespaces]\nkeyA = "tenantA"\nkeyB = "tenantB"\n\n[collections]\ncollections = []\npinned_collections = []\n',
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    config.collections = ["/new/path"]
    save_config(config, toml_file)

    reloaded = load_config(path=toml_file)
    assert reloaded.namespaces == {"keyA": "tenantA", "keyB": "tenantB"}


def test_config_namespaces_non_string_value_raises(tmp_path: Path) -> None:
    """[namespaces] with integer value → ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[namespaces]\nkeyA = 42\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_search_config_default_namespaces_empty() -> None:
    """SearchConfig() with no args has namespaces == {}."""
    config = SearchConfig()
    assert config.namespaces == {}

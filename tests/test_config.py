"""Tests for archon_search.config ( — Standalone config loader)."""

import os
from pathlib import Path

import pytest

from archon_search.config import ConfigError, ObservabilityConfig, SearchConfig, get_default_config_path, load_config, save_config


def test_load_config_defaults_when_no_file(tmp_path: Path) -> None:
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert isinstance(config, SearchConfig)
    assert config.host == "127.0.0.1"
    assert config.port == 8765


def test_load_config_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
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


def test_db_path_tilde_preserved_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
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


def test_load_config_logging_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
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
    assert path == Path.home() / ".archon-search" / "archon-search.toml"


def test_config_error_is_exception() -> None:
    err = ConfigError("bad value")
    assert isinstance(err, Exception)
    assert str(err) == "bad value"


def test_load_config_defaults_for_all_sections() -> None:
    config = SearchConfig()
    assert config.embedding_model == "BAAI/bge-small-en-v1.5"
    assert config.reranker_model == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert config.chunk_size == 512
    assert config.auto_reindex_on_chunk_size_change is True
    assert config.routing_shortlist_size == 8
    assert config.routing_confidence_threshold == 0.30
    assert config.pinned_collections == []
    assert config.watch is False
    assert config.level == "INFO"


def test_max_fanout_default() -> None:
    assert SearchConfig().max_fanout == 8


def test_fanout_leg_trim_default() -> None:
    assert SearchConfig().fanout_leg_trim == 40


def test_fanout_timeout_seconds_default() -> None:
    assert SearchConfig().fanout_timeout_seconds == 30.0


def test_max_fanout_loaded_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nmax_fanout = 4\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.max_fanout == 4


def test_fanout_leg_trim_loaded_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nfanout_leg_trim = 12\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.fanout_leg_trim == 12


def test_fanout_timeout_seconds_loaded_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nfanout_timeout_seconds = 5.5\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.fanout_timeout_seconds == 5.5


def test_max_fanout_zero_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nmax_fanout = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_fanout must be"):
        load_config(path=toml_file)


def test_max_fanout_negative_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nmax_fanout = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_fanout must be"):
        load_config(path=toml_file)


def test_fanout_leg_trim_zero_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nfanout_leg_trim = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="fanout_leg_trim must be"):
        load_config(path=toml_file)


def test_fanout_leg_trim_negative_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nfanout_leg_trim = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="fanout_leg_trim must be"):
        load_config(path=toml_file)


def test_fanout_timeout_zero_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nfanout_timeout_seconds = 0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="fanout_timeout_seconds must be"):
        load_config(path=toml_file)


def test_fanout_timeout_negative_raises_config_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[search]\nfanout_timeout_seconds = -1.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="fanout_timeout_seconds must be"):
        load_config(path=toml_file)


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


def test_load_config_top_k_from_database_section(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[database]\ntop_k_retrieve = 20\ntop_k_return = 8\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.top_k_retrieve == 20
    assert config.top_k_return == 8


def test_load_config_top_k_defaults() -> None:
    config = SearchConfig()
    assert config.top_k_retrieve == 15
    assert config.top_k_return == 5


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


def test_save_config_preserves_other_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """save_config only touches [collections] keys; other TOML sections are preserved."""
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
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
    assert config.db_path == "~/.archon-search/search"
    assert config.embedding_model == "BAAI/bge-small-en-v1.5"
    assert config.reranker_model == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert config.chunk_size == 512
    assert config.auto_reindex_on_chunk_size_change is True
    assert config.providers == []
    assert config.routing_shortlist_size == 8
    assert config.routing_confidence_threshold == 0.30
    assert config.pinned_collections == []
    assert config.collections == []
    assert config.watch is False
    assert config.level == "INFO"
    assert config.log_file == "~/.archon-search/logs/archon-search.log"
    assert config.namespaces == {}


def test_c11_14_nonexistent_path_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C11.14: non-existent path → returns SearchConfig() defaults (with post-processed backup.output_dir)."""
    from archon_search.config import BackupConfig
    from archon_search.paths import get_data_dir

    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    config = load_config(path=tmp_path / "does_not_exist.toml")
    # BackupConfig.output_dir is resolved from "" to the data dir path by load_config post-processing.
    # Build expected defaults with the resolved output_dir to match.
    defaults = SearchConfig()
    defaults.backup = BackupConfig(output_dir=str(get_data_dir() / "backups"))
    assert config == defaults


def test_c11_15_invalid_toml_raises_config_error_with_cause(tmp_path: Path) -> None:
    """C11.15: invalid TOML → raises ConfigError with __cause__ set."""
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("[[[[not valid toml\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(path=bad_file)
    assert exc_info.value.__cause__ is not None


def test_c11_16_all_four_sections_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C11.16: TOML with all 4 sections ([server], [database], [routing], [collections]) → all fields populated."""
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
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
        "routing_confidence_threshold = 0.75\n\n"
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


# ---------------------------------------------------------------------------
# ARCHON_SEARCH_CONFIG env var override tests 
# ---------------------------------------------------------------------------


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHON_SEARCH_CONFIG=/tmp/custom.toml → returns Path('/tmp/custom.toml')."""
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", "/tmp/custom.toml")
    assert get_default_config_path() == Path("/tmp/custom.toml")


def test_env_var_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHON_SEARCH_CONFIG=~/.custom/archon-search.toml → absolute path under home."""
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", "~/.custom/archon-search.toml")
    result = get_default_config_path()
    assert result.is_absolute()
    assert str(result).startswith(str(Path.home()))


def test_env_var_empty_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHON_SEARCH_CONFIG="" → returns the ~/.archon-search/archon-search.toml default."""
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", "")
    assert get_default_config_path() == Path.home() / ".archon-search" / "archon-search.toml"


def test_env_var_relative_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHON_SEARCH_CONFIG=relative/path.toml → resolved against cwd."""
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", "relative/path.toml")
    result = get_default_config_path()
    assert result == (Path.cwd() / "relative/path.toml").resolve()


# ---------------------------------------------------------------------------
# [observability] config section — Task 1.2 (B1)
# ---------------------------------------------------------------------------


def test_observability_defaults(tmp_path: Path) -> None:
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.observability.stage_timings_enabled is True
    assert config.observability.request_id_header == "X-Request-ID"


def test_observability_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[observability]\nstage_timings_enabled = false\nrequest_id_header = \"X-Trace-ID\"\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.observability.stage_timings_enabled is False
    assert config.observability.request_id_header == "X-Trace-ID"


def test_observability_invalid_bool_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[observability]\nstage_timings_enabled = \"yes\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_observability_empty_header_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[observability]\nrequest_id_header = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_observability_config_dataclass_defaults() -> None:
    obs = ObservabilityConfig()
    assert obs.stage_timings_enabled is True
    assert obs.request_id_header == "X-Request-ID"


def test_observability_section_absent_uses_defaults(tmp_path: Path) -> None:
    """A config file with no [observability] section still gives defaults."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[server]\nhost = \"0.0.0.0\"\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.observability.stage_timings_enabled is True
    assert config.observability.request_id_header == "X-Request-ID"


# ---------------------------------------------------------------------------
# routing_strategy and routing_description_weight (Task 5.1)
# ---------------------------------------------------------------------------


def test_routing_strategy_default(tmp_path: Path) -> None:
    """Config loaded from empty TOML has routing_strategy == 'centroid'."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.routing_strategy == "centroid"


def test_routing_strategy_hybrid_parsed(tmp_path: Path) -> None:
    """TOML with routing_strategy = 'hybrid' → config.routing_strategy == 'hybrid'."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[routing]\nrouting_strategy = "hybrid"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.routing_strategy == "hybrid"


def test_routing_strategy_invalid_raises(tmp_path: Path) -> None:
    """TOML with routing_strategy = 'cosine' → ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[routing]\nrouting_strategy = "cosine"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="routing_strategy must be"):
        load_config(path=toml_file)


def test_routing_description_weight_default() -> None:
    """Default routing_description_weight equals DEFAULT_ROUTING_DESCRIPTION_WEIGHT."""
    from archon_search.constants import DEFAULT_ROUTING_DESCRIPTION_WEIGHT

    config = SearchConfig()
    assert config.routing_description_weight == DEFAULT_ROUTING_DESCRIPTION_WEIGHT


def test_routing_description_weight_boundary_zero(tmp_path: Path) -> None:
    """routing_description_weight = 0.0 parses without error."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[routing]\nrouting_description_weight = 0.0\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.routing_description_weight == 0.0


def test_routing_description_weight_boundary_one(tmp_path: Path) -> None:
    """routing_description_weight = 1.0 parses without error."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[routing]\nrouting_description_weight = 1.0\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.routing_description_weight == 1.0


def test_routing_description_weight_out_of_range_raises(tmp_path: Path) -> None:
    """routing_description_weight = 1.1 and -0.1 → ConfigError."""
    for value in (1.1, -0.1):
        toml_file = tmp_path / f"bad_{value}.toml"
        toml_file.write_text(
            f"[routing]\nrouting_description_weight = {value}\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="routing_description_weight must be"):
            load_config(path=toml_file)


# ---------------------------------------------------------------------------
# B5 Task 3.1 — centroid_recompute_threshold and centroid_incremental_enabled
# ---------------------------------------------------------------------------


def test_centroid_recompute_threshold_default() -> None:
    assert SearchConfig().centroid_recompute_threshold == 10_000


def test_deprecated_flag_emits_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Loading centroid_incremental_enabled from TOML must emit a deprecation WARNING and ignore it."""
    import logging
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\ncentroid_incremental_enabled = false\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        load_config(path=toml_file)
    assert any("centroid_incremental_enabled" in r.message for r in caplog.records)


def test_deprecated_flag_emits_warning_for_true_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """centroid_incremental_enabled = true also emits a deprecation WARNING — the handler is value-agnostic."""
    import logging
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\ncentroid_incremental_enabled = true\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        load_config(path=toml_file)
    assert any("centroid_incremental_enabled" in r.message for r in caplog.records)


def test_deprecated_flag_is_ignored(tmp_path: Path) -> None:
    """centroid_incremental_enabled in TOML is silently ignored — SearchConfig has no such field."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\ncentroid_incremental_enabled = false\n", encoding="utf-8")
    cfg = load_config(path=toml_file)
    assert not hasattr(cfg, "centroid_incremental_enabled")


def test_removed_max_parallel_collections_still_loads(tmp_path: Path) -> None:
    """An existing TOML that still sets the removed [routing].max_parallel_collections key
    must load without raising (the BREAKING.md forward-compat contract), and the field must
    not reappear on SearchConfig. Previously-invalid values (<= 0) must no longer raise."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text(
        "[routing]\nrouting_shortlist_size = 5\nmax_parallel_collections = 0\n",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_file)
    assert not hasattr(cfg, "max_parallel_collections")
    # The recognised sibling key still loads; the removed key does not disturb it.
    assert cfg.routing_shortlist_size == 5


def test_removed_max_parallel_collections_emits_deprecation_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Loading the removed max_parallel_collections key emits a deprecation WARNING (mirrors the
    centroid_incremental_enabled precedent), so operators who tuned it get a signal."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[routing]\nmax_parallel_collections = 3\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        load_config(path=toml_file)
    assert any("max_parallel_collections" in r.message for r in caplog.records)


def test_centroid_recompute_threshold_loaded_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\ncentroid_recompute_threshold = 500\n", encoding="utf-8")
    cfg = load_config(path=toml_file)
    assert cfg.centroid_recompute_threshold == 500


def test_centroid_recompute_threshold_validation(tmp_path: Path) -> None:
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("[database]\ncentroid_recompute_threshold = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="centroid_recompute_threshold must be >= 1"):
        load_config(path=toml_file)


# ---------------------------------------------------------------------------
# B7 Task 1.1 — [logging] section: log_format and backup_count
# ---------------------------------------------------------------------------


def test_logging_level_default() -> None:
    assert SearchConfig().level == "INFO"


def test_logging_log_file_default() -> None:
    assert SearchConfig().log_file == "~/.archon-search/logs/archon-search.log"


def test_logging_level_valid_values(tmp_path: Path) -> None:
    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        toml_file = tmp_path / f"cfg_{level}.toml"
        toml_file.write_text(f'[logging]\nlevel = "{level}"\n', encoding="utf-8")
        config = load_config(path=toml_file)
        assert config.level == level


def test_logging_level_case_insensitive(tmp_path: Path) -> None:
    for raw, expected in [("info", "INFO"), ("Info", "INFO"), ("WARNING", "WARNING")]:
        toml_file = tmp_path / f"cfg_{raw}.toml"
        toml_file.write_text(f'[logging]\nlevel = "{raw}"\n', encoding="utf-8")
        config = load_config(path=toml_file)
        assert config.level == expected


def test_logging_level_invalid_raises(tmp_path: Path) -> None:
    for bad in ("VERBOSE", "ALL", ""):
        toml_file = tmp_path / f"bad_{bad}.toml"
        toml_file.write_text(f'[logging]\nlevel = "{bad}"\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path=toml_file)


def test_logging_level_warn_normalized_to_warning(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nlevel = "WARN"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.level == "WARNING"


def test_logging_format_text_default() -> None:
    assert SearchConfig().log_format == "text"


def test_logging_format_json_parsed(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nformat = "json"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.log_format == "json"


def test_logging_format_invalid_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nformat = "xml"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_logging_backup_count_default() -> None:
    assert SearchConfig().backup_count == 7


def test_logging_backup_count_zero_allowed(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[logging]\nbackup_count = 0\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.backup_count == 0


def test_logging_backup_count_negative_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[logging]\nbackup_count = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_logging_backup_count_parsed(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[logging]\nbackup_count = 14\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.backup_count == 14


def test_logging_log_file_empty_string_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nlog_file = ""\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.log_file == ""


def test_logging_log_file_empty_string_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """[logging].log_file = '' must emit a WARNING so operators know file logging is disabled."""
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_CONTAINER", raising=False)
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nlog_file = ""\n', encoding="utf-8")
    import logging

    from archon_search.constants import LOG_FILE_DISABLED_WARNING
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        load_config(path=toml_file)
    matches = [r for r in caplog.records if "log_file" in r.message and r.levelno == logging.WARNING]
    assert matches, (
        f"Expected a WARNING about empty log_file, got: {[r.message for r in caplog.records]}"
    )
    # Pins the config.py call site to the shared constant — a hardcoded literal
    # here would drift silently without this equality check (S107 C2-B).
    assert matches[0].message == LOG_FILE_DISABLED_WARNING


def test_logging_log_file_empty_string_no_warn_in_container_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """log_file='' must NOT warn when ARCHON_SEARCH_CONTAINER=1 (intentional container mode)."""
    import logging

    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    monkeypatch.setenv("ARCHON_SEARCH_CONTAINER", "1")
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nlog_file = ""\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        load_config(path=toml_file)
    assert not any("log_file" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_logging_log_file_empty_string_preserved_and_warns_when_data_dir_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S107: TOML log_file="" is the disable-file-logging opt-out and must be PRESERVED
    (not clobbered) when ARCHON_SEARCH_DATA_DIR is set. The empty-string warning MUST
    fire (since ARCHON_SEARCH_CONTAINER is not "1")."""
    import logging

    from archon_search.constants import LOG_FILE_DISABLED_WARNING

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ARCHON_SEARCH_CONTAINER", raising=False)
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nlog_file = ""\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        config = load_config(path=toml_file)
    matches = [r for r in caplog.records if "log_file" in r.message and r.levelno == logging.WARNING]
    assert matches
    # Pins the config.py call site to the shared constant — a hardcoded literal
    # here would drift silently without this equality check (S107 C2-B).
    assert matches[0].message == LOG_FILE_DISABLED_WARNING
    assert config.log_file == ""


def test_logging_log_file_empty_string_warning_reaches_stderr(tmp_path: Path) -> None:
    """S107: the empty-log_file warning must reach STDERR (the stream a serving
    operator / smoke harness captures) — not just the logging record — and no log
    file may be created while file logging is disabled.

    caplog cannot prove this: it attaches its own handler. The real guarantee is
    that load_config emits the warning before logging is configured, so Python's
    last-resort handler flushes it to stderr. This runs load_config in a clean
    subprocess with unconfigured logging to observe the actual stream.
    """
    import subprocess
    import sys

    data_dir = tmp_path / "datadir"
    data_dir.mkdir()
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nlog_file = ""\n', encoding="utf-8")

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ARCHON_SEARCH_CONTAINER"}
    }
    env["ARCHON_SEARCH_DATA_DIR"] = str(data_dir)

    snippet = (
        "import pathlib\n"
        "from archon_search.config import load_config\n"
        f"cfg = load_config(pathlib.Path({str(toml_file)!r}), serve=True)\n"
        "assert cfg.log_file == '', repr(cfg.log_file)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "log_file" in result.stderr and "file logging is disabled" in result.stderr, (
        f"expected empty-log_file warning on stderr, got: {result.stderr!r}"
    )
    assert not (data_dir / "logs").exists(), "no log directory may be created when file logging is disabled"


def test_logging_toml_key_format_maps_to_log_format_field(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nformat = "text"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.log_format == "text"


def test_default_toml_logging_section_includes_all_keys() -> None:
    from archon_search.cli.config_cmd import _default_toml

    import tomlkit as _tomlkit

    doc = _tomlkit.parse(_default_toml())
    log_section = doc.get("logging", {})
    for key in ("level", "log_file", "format", "backup_count"):
        assert key in log_section, f"Missing key in [logging]: {key!r}"


def test_logging_format_case_sensitive_uppercase_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nformat = "JSON"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_logging_backup_count_string_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[logging]\nbackup_count = "seven"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


# ---------------------------------------------------------------------------
# C0 Task 1.2 — profile and multilingual fields
# ---------------------------------------------------------------------------


def test_load_config_profile_and_multilingual_defaults(tmp_path: Path) -> None:
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.profile == ""
    assert config.multilingual is False


def test_load_config_reads_profile_balanced(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[database]\nprofile = "balanced"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.profile == "balanced"


def test_load_config_reads_multilingual_true(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nmultilingual = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.multilingual is True


def test_load_config_multilingual_wrong_type_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[database]\nmultilingual = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_save_config_round_trip_preserves_profile_and_multilingual(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text(
        '[database]\nprofile = "balanced"\nmultilingual = true\n',
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    save_config(config, toml_file)
    reloaded = load_config(path=toml_file)
    assert reloaded.profile == "balanced"
    assert reloaded.multilingual is True


# ---------------------------------------------------------------------------
# C1 Task 1.5 — embedder_cache_size and eager_load_embedders config keys
# ---------------------------------------------------------------------------


def test_embedder_cache_size_defaults_to_3() -> None:
    config = SearchConfig()
    assert config.embedder_cache_size == 3


def test_eager_load_embedders_defaults_to_false() -> None:
    config = SearchConfig()
    assert config.eager_load_embedders is False


def test_embedder_cache_size_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nembedder_cache_size = 5\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.embedder_cache_size == 5


def test_eager_load_embedders_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\neager_load_embedders = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.eager_load_embedders is True


def test_embedder_cache_size_zero_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nembedder_cache_size = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="embedder_cache_size must be"):
        load_config(path=toml_file)


def test_embedder_cache_size_negative_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nembedder_cache_size = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="embedder_cache_size must be"):
        load_config(path=toml_file)


# ---------------------------------------------------------------------------
# C2 Task 2.2 — language_detection_confidence_threshold config key
# ---------------------------------------------------------------------------


def test_default_confidence_threshold() -> None:
    config = load_config(path=Path("/nonexistent/path.toml"))
    assert config.language_detection_confidence_threshold == 0.7


def test_custom_confidence_threshold(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nlanguage_detection_confidence_threshold = 0.5\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.language_detection_confidence_threshold == 0.5


def test_confidence_threshold_out_of_range(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nlanguage_detection_confidence_threshold = 1.5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="language_detection_confidence_threshold"):
        load_config(path=toml_file)


def test_confidence_threshold_zero(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nlanguage_detection_confidence_threshold = 0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="language_detection_confidence_threshold"):
        load_config(path=toml_file)


def test_confidence_threshold_negative(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nlanguage_detection_confidence_threshold = -0.1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="language_detection_confidence_threshold"):
        load_config(path=toml_file)


def test_confidence_threshold_upper_bound(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[database]\nlanguage_detection_confidence_threshold = 1.0\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.language_detection_confidence_threshold == 1.0


def test_confidence_threshold_non_numeric_raises(tmp_path: Path) -> None:
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[database]\nlanguage_detection_confidence_threshold = "high"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="language_detection_confidence_threshold"):
        load_config(path=toml_file)


# ---------------------------------------------------------------------------
# C4 Task 1.1 — HyDEConfig dataclass + [hyde] TOML loader
# ---------------------------------------------------------------------------


def test_hyde_config_defaults(tmp_path: Path) -> None:
    """load_config with no TOML file returns HyDEConfig with enabled=False and all other defaults."""
    from archon_search.config import HyDEConfig
    from archon_search.constants import DEFAULT_FAST_MODEL

    config = load_config(path=tmp_path / "nonexistent.toml")
    assert isinstance(config.hyde, HyDEConfig)
    assert config.hyde.enabled is False
    assert config.hyde.model == DEFAULT_FAST_MODEL
    assert config.hyde.timeout_seconds == 10.0
    assert config.hyde.max_requests_per_minute == 60


def test_hyde_toml_all_keys(tmp_path: Path) -> None:
    """TOML with all [hyde] keys parses correctly."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text(
        "[hyde]\nenabled = true\nmodel = \"gpt-test\"\ntimeout_seconds = 10.0\nmax_requests_per_minute = 30\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.hyde.enabled is True
    assert config.hyde.model == "gpt-test"
    assert config.hyde.timeout_seconds == 10.0
    assert config.hyde.max_requests_per_minute == 30


def test_hyde_toml_partial_keys(tmp_path: Path) -> None:
    """TOML with only [hyde] timeout_seconds applies that value; other fields remain default."""
    from archon_search.constants import DEFAULT_FAST_MODEL

    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[hyde]\ntimeout_seconds = 3.0\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.hyde.timeout_seconds == 3.0
    assert config.hyde.enabled is False
    assert config.hyde.model == DEFAULT_FAST_MODEL
    assert config.hyde.max_requests_per_minute == 60


def test_hyde_config_invalid_timeout(tmp_path: Path) -> None:
    """timeout_seconds = 0 raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[hyde]\ntimeout_seconds = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="timeout_seconds"):
        load_config(path=toml_file)


def test_hyde_config_invalid_rpm(tmp_path: Path) -> None:
    """max_requests_per_minute = 0 raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[hyde]\nmax_requests_per_minute = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_requests_per_minute"):
        load_config(path=toml_file)


def test_hyde_config_empty_model(tmp_path: Path) -> None:
    """model = "" raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[hyde]\nmodel = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="model"):
        load_config(path=toml_file)


# ---------------------------------------------------------------------------
# C5 Task 1.1 — RAGFusionConfig dataclass + [rag_fusion] TOML loader
# ---------------------------------------------------------------------------


def test_rag_fusion_config_defaults(tmp_path: Path) -> None:
    """load_config with no TOML returns RAGFusionConfig with enabled=False, num_queries=2."""
    from archon_search.config import RAGFusionConfig
    from archon_search.constants import DEFAULT_FAST_MODEL

    config = load_config(path=tmp_path / "nonexistent.toml")
    assert isinstance(config.rag_fusion, RAGFusionConfig)
    assert config.rag_fusion.enabled is False
    assert config.rag_fusion.model == DEFAULT_FAST_MODEL
    assert config.rag_fusion.timeout_seconds == 10.0
    assert config.rag_fusion.max_requests_per_minute == 60
    assert config.rag_fusion.num_queries == 2


def test_rag_fusion_toml_all_keys(tmp_path: Path) -> None:
    """TOML with all [rag_fusion] keys parses correctly."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text(
        "[rag_fusion]\nenabled = true\nmodel = \"claude-test\"\ntimeout_seconds = 10.0\nmax_requests_per_minute = 30\nnum_queries = 3\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.rag_fusion.enabled is True
    assert config.rag_fusion.model == "claude-test"
    assert config.rag_fusion.timeout_seconds == 10.0
    assert config.rag_fusion.max_requests_per_minute == 30
    assert config.rag_fusion.num_queries == 3


def test_rag_fusion_toml_partial_keys(tmp_path: Path) -> None:
    """TOML with only num_queries=3 applies that; other fields remain default."""
    from archon_search.constants import DEFAULT_FAST_MODEL

    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[rag_fusion]\nnum_queries = 3\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.rag_fusion.num_queries == 3
    assert config.rag_fusion.enabled is False
    assert config.rag_fusion.model == DEFAULT_FAST_MODEL
    assert config.rag_fusion.timeout_seconds == 10.0
    assert config.rag_fusion.max_requests_per_minute == 60


def test_rag_fusion_config_invalid_timeout(tmp_path: Path) -> None:
    """timeout_seconds = 0 raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[rag_fusion]\ntimeout_seconds = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="timeout_seconds"):
        load_config(path=toml_file)


def test_rag_fusion_config_invalid_rpm(tmp_path: Path) -> None:
    """max_requests_per_minute = 0 raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[rag_fusion]\nmax_requests_per_minute = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_requests_per_minute"):
        load_config(path=toml_file)


def test_rag_fusion_config_empty_model(tmp_path: Path) -> None:
    """model = "" raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text('[rag_fusion]\nmodel = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="model"):
        load_config(path=toml_file)


def test_rag_fusion_config_num_queries_zero(tmp_path: Path) -> None:
    """num_queries = 0 raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[rag_fusion]\nnum_queries = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="num_queries"):
        load_config(path=toml_file)


def test_rag_fusion_config_num_queries_six(tmp_path: Path) -> None:
    """num_queries = 6 raises ConfigError."""
    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[rag_fusion]\nnum_queries = 6\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="num_queries"):
        load_config(path=toml_file)


def test_rag_fusion_config_num_queries_one_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """num_queries = 1 does NOT raise; caplog contains WARNING about LLM overhead."""
    import logging

    toml_file = tmp_path / "cfg.toml"
    toml_file.write_text("[rag_fusion]\nnum_queries = 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        config = load_config(path=toml_file)
    assert config.rag_fusion.num_queries == 1
    assert any("num_queries" in r.message and "overhead" in r.message for r in caplog.records)


def test_validation_timeout_seconds_default() -> None:
    assert SearchConfig().validation_timeout_seconds == 60


def test_validation_timeout_seconds_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[database]\nvalidation_timeout_seconds = 30\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.validation_timeout_seconds == 30


def test_validation_timeout_seconds_zero_rejected(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[database]\nvalidation_timeout_seconds = 0\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        config = load_config(path=toml_file)
    assert config.validation_timeout_seconds == 60
    assert any("validation_timeout_seconds" in r.message for r in caplog.records)


def test_validation_timeout_seconds_negative_rejected(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[database]\nvalidation_timeout_seconds = -5\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        config = load_config(path=toml_file)
    assert config.validation_timeout_seconds == 60


# --- BE-1: McpConfig ---


def test_mcp_config_defaults() -> None:
    from archon_search.config import McpConfig

    cfg = McpConfig()
    assert cfg.enabled is True


def test_mcp_config_toml_section(tmp_path: Path) -> None:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[mcp]\nenabled = false\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.mcp.enabled is False


def test_mcp_config_missing_section_uses_defaults(tmp_path: Path) -> None:
    """Missing [mcp] section yields all defaults."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[server]\nport = 9000\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.mcp.enabled is True


def test_mcp_config_enabled_true_explicit(tmp_path: Path) -> None:
    """enabled = true explicitly in TOML is parsed correctly (runs through _coerce_bool)."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[mcp]\nenabled = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.mcp.enabled is True


def test_mcp_config_enabled_false(tmp_path: Path) -> None:
    """enabled = false in TOML sets config.mcp.enabled = False."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[mcp]\nenabled = false\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.mcp.enabled is False


# ---------------------------------------------------------------------------
# [telemetry] hash_doc_ids (D8 BE-1)
# ---------------------------------------------------------------------------


def test_hash_doc_ids_defaults_to_false(tmp_path: Path) -> None:
    """TelemetryConfig() has hash_doc_ids=False by default."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.telemetry.hash_doc_ids is False


def test_hash_doc_ids_parsed_from_toml_true(tmp_path: Path) -> None:
    """[telemetry] hash_doc_ids = true sets the field."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[telemetry]\nhash_doc_ids = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.telemetry.hash_doc_ids is True


def test_hash_doc_ids_parsed_from_toml_false(tmp_path: Path) -> None:
    """Explicit [telemetry] hash_doc_ids = false parses correctly."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[telemetry]\nhash_doc_ids = false\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.telemetry.hash_doc_ids is False


def test_hash_doc_ids_absent_from_telemetry_section_stays_false(tmp_path: Path) -> None:
    """[telemetry] section present with other keys but no hash_doc_ids → default False preserved."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[telemetry]\nenabled = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.telemetry.enabled is True
    assert config.telemetry.hash_doc_ids is False


@pytest.mark.integration
def test_telemetry_config_hash_doc_ids_in_load_config(tmp_path: Path) -> None:
    """Full load_config() round-trip: hash_doc_ids toggled via TOML and coexists with other telemetry fields."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[telemetry]\nenabled = true\nretention_days = 14\nhash_doc_ids = true\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.telemetry.enabled is True
    assert config.telemetry.retention_days == 14
    assert config.telemetry.hash_doc_ids is True


# ---------------------------------------------------------------------------
# BE-6: Config fields for graph garbage collection
# ---------------------------------------------------------------------------


def test_maintenance_config_graph_gc_default_true() -> None:
    """MaintenanceConfig() has graph_gc=True by default."""
    from archon_search.config import MaintenanceConfig

    cfg = MaintenanceConfig()
    assert cfg.graph_gc is True


def test_graph_config_gc_rebuild_defaults() -> None:
    """GraphConfig() has gc_rebuild_communities=True and gc_rebuild_cpu_priority='low' by default."""
    from archon_search.config import GraphConfig

    cfg = GraphConfig()
    assert cfg.gc_rebuild_communities is True
    assert cfg.gc_rebuild_cpu_priority == "low"


def test_toml_graph_gc_false_overrides_default(tmp_path: Path) -> None:
    """TOML [maintenance] graph_gc = false → MaintenanceConfig.graph_gc == False."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[maintenance]\ngraph_gc = false\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.maintenance.graph_gc is False


def test_toml_gc_cpu_priority_round_trip(tmp_path: Path) -> None:
    """TOML [graph] gc_rebuild_cpu_priority = 'normal' → GraphConfig.gc_rebuild_cpu_priority == 'normal'."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\ngc_rebuild_cpu_priority = "normal"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.gc_rebuild_cpu_priority == "normal"


def test_toml_gc_rebuild_communities_false(tmp_path: Path) -> None:
    """TOML [graph] gc_rebuild_communities = false → GraphConfig.gc_rebuild_communities == False."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\ngc_rebuild_communities = false\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.gc_rebuild_communities is False


def test_toml_gc_cpu_priority_invalid_raises(tmp_path: Path) -> None:
    """TOML [graph] gc_rebuild_cpu_priority with invalid value → ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\ngc_rebuild_cpu_priority = "urgent"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="gc_rebuild_cpu_priority"):
        load_config(path=toml_file)


def test_toml_gc_cpu_priority_all_valid_values(tmp_path: Path) -> None:
    """TOML [graph] gc_rebuild_cpu_priority accepts 'low', 'normal', 'high'."""
    for value in ("low", "normal", "high"):
        toml_file = tmp_path / f"cfg_{value}.toml"
        toml_file.write_text(f'[graph]\ngc_rebuild_cpu_priority = "{value}"\n', encoding="utf-8")
        config = load_config(path=toml_file)
        assert config.graph.gc_rebuild_cpu_priority == value


def test_toml_gc_cpu_priority_empty_string_raises(tmp_path: Path) -> None:
    """TOML [graph] gc_rebuild_cpu_priority = '' raises ConfigError."""
    from archon_search.config import ConfigError

    toml_file = tmp_path / "cfg_empty.toml"
    toml_file.write_text('[graph]\ngc_rebuild_cpu_priority = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=toml_file)


def test_warn_gc_cpu_priority_wired_in_app_lifespan() -> None:
    """Verify that app.py imports and calls warn_gc_cpu_priority in its lifespan.

    Verified by source inspection: archon_search/server/app.py line 19 imports
    warn_gc_cpu_priority from archon_search.config, and line 194 calls it with
    the loaded config. This test asserts those facts remain true so any future
    refactor that removes the wiring fails loudly.
    """
    import ast
    from pathlib import Path as _Path

    app_src = (_Path(__file__).parent.parent / "archon_search" / "server" / "app.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(app_src)

    # Assert import of warn_gc_cpu_priority
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "archon_search.config":
            if any(alias.name == "warn_gc_cpu_priority" for alias in node.names):
                imported = True
                break
    assert imported, "app.py must import warn_gc_cpu_priority from archon_search.config"

    # Assert call to warn_gc_cpu_priority
    called = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "warn_gc_cpu_priority"
        ):
            called = True
            break
    assert called, "app.py lifespan must call warn_gc_cpu_priority(config)"


def test_startup_warns_when_cpu_priority_non_normal_on_non_linux(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """warn_gc_cpu_priority logs WARNING on non-Linux when priority != 'normal';
    no WARNING when priority == 'normal'; no WARNING on Linux regardless of value."""
    import logging
    import sys

    from archon_search.config import GraphConfig, SearchConfig, warn_gc_cpu_priority

    # Case 1: non-Linux, low priority, graph fully active → WARNING
    cfg = SearchConfig()
    cfg.graph = GraphConfig(enabled=True, gc_rebuild_cpu_priority="low")
    monkeypatch.setattr(sys, "platform", "darwin")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg)
    assert any(
        "gc_rebuild_cpu_priority" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )

    caplog.clear()

    # Case 2: non-Linux (still darwin from Case 1 monkeypatch), normal priority, graph
    # fully active → no WARNING. Suppression is because priority == 'normal', NOT because
    # graph is disabled — graph.enabled=True here to prevent a tautological pass.
    cfg2 = SearchConfig()
    cfg2.graph = GraphConfig(enabled=True, gc_rebuild_cpu_priority="normal")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg2)
    assert not any("gc_rebuild_cpu_priority" in r.message for r in caplog.records)

    caplog.clear()

    # Case 3: non-Linux, high priority, graph fully active → WARNING
    cfg3 = SearchConfig()
    cfg3.graph = GraphConfig(enabled=True, gc_rebuild_cpu_priority="high")
    # platform is still "darwin" from Case 1 monkeypatch
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg3)
    assert any(
        "gc_rebuild_cpu_priority" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )

    caplog.clear()

    # Case 4: Linux, low priority → no WARNING
    cfg4 = SearchConfig()
    cfg4.graph = GraphConfig(enabled=True, gc_rebuild_cpu_priority="low")
    monkeypatch.setattr(sys, "platform", "linux")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg4)
    assert not any("gc_rebuild_cpu_priority" in r.message for r in caplog.records)


def test_startup_warns_suppressed_when_feature_inactive(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """warn_gc_cpu_priority is suppressed when the GC feature is not fully active,
    even when priority != 'normal' on non-Linux."""
    import logging
    import sys

    from archon_search.config import GraphConfig, MaintenanceConfig, SearchConfig, warn_gc_cpu_priority

    monkeypatch.setattr(sys, "platform", "darwin")

    # Case 1: graph.enabled=False → no WARNING
    cfg1 = SearchConfig()
    cfg1.graph = GraphConfig(enabled=False, gc_rebuild_cpu_priority="low")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg1)
    assert not any("gc_rebuild_cpu_priority" in r.message for r in caplog.records)

    caplog.clear()

    # Case 2: maintenance.graph_gc=False → no WARNING
    cfg2 = SearchConfig()
    cfg2.graph = GraphConfig(enabled=True, gc_rebuild_cpu_priority="low")
    cfg2.maintenance = MaintenanceConfig(graph_gc=False)
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg2)
    assert not any("gc_rebuild_cpu_priority" in r.message for r in caplog.records)

    caplog.clear()

    # Case 3: gc_rebuild_communities=False → no WARNING
    cfg3 = SearchConfig()
    cfg3.graph = GraphConfig(enabled=True, gc_rebuild_communities=False, gc_rebuild_cpu_priority="low")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        warn_gc_cpu_priority(cfg3)
    assert not any("gc_rebuild_cpu_priority" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# BE-3: GraphConfig synonym fields
# ---------------------------------------------------------------------------


def test_graph_config_synonym_threshold_default() -> None:
    """GraphConfig().synonym_threshold defaults to 0.85."""
    from archon_search.config import GraphConfig

    cfg = GraphConfig()
    assert cfg.synonym_threshold == 0.85


def test_graph_config_enrichment_auto_default_true() -> None:
    """GraphConfig().enrichment_auto defaults to True."""
    from archon_search.config import GraphConfig

    cfg = GraphConfig()
    assert cfg.enrichment_auto is True


def test_graph_config_synonym_fields_toml_loading(tmp_path: Path) -> None:
    """TOML overrides for synonym_threshold, alias_file, and enrichment_auto are read correctly."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        '[graph]\nsynonym_threshold = 0.75\nalias_file = "/etc/archon/aliases.toml"\nenrichment_auto = false\n',
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.graph.synonym_threshold == 0.75
    assert config.graph.alias_file == "/etc/archon/aliases.toml"
    assert config.graph.enrichment_auto is False


def test_graph_config_synonym_threshold_zero_raises(tmp_path: Path) -> None:
    """synonym_threshold = 0.0 raises ConfigError (exclusive lower bound)."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nsynonym_threshold = 0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="synonym_threshold"):
        load_config(path=toml_file)


def test_graph_config_synonym_threshold_above_one_raises(tmp_path: Path) -> None:
    """synonym_threshold = 1.5 raises ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nsynonym_threshold = 1.5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="synonym_threshold"):
        load_config(path=toml_file)


def test_graph_config_synonym_threshold_bool_raises(tmp_path: Path) -> None:
    """synonym_threshold = true in TOML raises ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nsynonym_threshold = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="synonym_threshold"):
        load_config(path=toml_file)


def test_graph_config_synonym_threshold_exactly_one_valid(tmp_path: Path) -> None:
    """synonym_threshold = 1.0 is valid (inclusive upper bound)."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nsynonym_threshold = 1.0\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.synonym_threshold == 1.0


def test_graph_config_alias_file_non_string_raises(tmp_path: Path) -> None:
    """alias_file = 123 (integer) in TOML raises ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nalias_file = 123\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="alias_file"):
        load_config(path=toml_file)


def test_graph_config_enrichment_auto_non_bool_raises(tmp_path: Path) -> None:
    """enrichment_auto = 1 (integer) in TOML raises ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nenrichment_auto = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="enrichment_auto"):
        load_config(path=toml_file)


def test_graph_config_enrichment_auto_true_via_toml(tmp_path: Path) -> None:
    """enrichment_auto = true in TOML is loaded correctly."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nenrichment_auto = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.enrichment_auto is True


def test_graph_config_alias_file_empty_string_raises(tmp_path: Path) -> None:
    """alias_file = "" (empty string) raises ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nalias_file = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="alias_file"):
        load_config(path=toml_file)


def test_graph_config_alias_file_whitespace_only_raises(tmp_path: Path) -> None:
    """alias_file = "   " (whitespace only) raises ConfigError."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nalias_file = "   "\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="alias_file"):
        load_config(path=toml_file)


def test_graph_config_alias_file_leading_trailing_whitespace_stripped(tmp_path: Path) -> None:
    """alias_file with surrounding whitespace is stored stripped."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nalias_file = "  /etc/archon/aliases.toml  "\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.alias_file == "/etc/archon/aliases.toml"


def test_graph_config_synonym_threshold_numeric_string_coerces(tmp_path: Path) -> None:
    """synonym_threshold = "0.9" (quoted numeric string) is coerced to float 0.9 (pre-existing _coerce_float behavior)."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nsynonym_threshold = "0.9"\n', encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.synonym_threshold == pytest.approx(0.9)


def test_graph_config_alias_file_default_none() -> None:
    """GraphConfig().alias_file defaults to None."""
    from archon_search.config import GraphConfig

    cfg = GraphConfig()
    assert cfg.alias_file is None


# ---------------------------------------------------------------------------
# BE-4: GraphConfig enrichment provider fields
# ---------------------------------------------------------------------------


def test_graph_config_provider_defaults_to_none() -> None:
    """GraphConfig().provider defaults to None (enrichment disabled, air-gap safe)."""
    from archon_search.config import GraphConfig

    cfg = GraphConfig()
    assert cfg.provider is None


def test_all_six_graph_fields_loaded_from_toml(tmp_path: Path) -> None:
    """A [graph] TOML section with all six new fields set to non-default values is fully applied.

    Proves no silent branch omission in the TOML loader.
    """
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(
        "[graph]\n"
        'provider = "llama_cpp"\n'
        'extraction_model = "qwen2.5-coder"\n'
        'llama_cpp_base_url = "http://example:9999"\n'
        'ollama_base_url = "http://example:4444"\n'
        "extraction_timeout_seconds = 45.0\n"
        "extraction_rate_limit_rpm = 30\n"
        "extraction_token_budget = 2048\n",
        encoding="utf-8",
    )
    config = load_config(path=toml_file)
    assert config.graph.provider == "llama_cpp"
    assert config.graph.llama_cpp_base_url == "http://example:9999"
    assert config.graph.ollama_base_url == "http://example:4444"
    assert config.graph.extraction_timeout_seconds == 45.0
    assert config.graph.extraction_rate_limit_rpm == 30
    assert config.graph.extraction_token_budget == 2048


def test_unknown_graph_provider_raises_config_error(tmp_path: Path) -> None:
    """[graph] provider = 'garbage' raises ConfigError naming the value and valid choices."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nprovider = "garbage"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="garbage"):
        load_config(path=toml_file)


def test_none_graph_provider_boots_cleanly(tmp_path: Path) -> None:
    """No [graph] provider set → default None → no ConfigError, boots cleanly."""
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text("[graph]\nenabled = true\n", encoding="utf-8")
    config = load_config(path=toml_file)
    assert config.graph.provider is None


def test_provider_without_model_emits_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """[graph] provider set but extraction_model absent → WARNING, not ConfigError."""
    import logging

    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[graph]\nprovider = "llama_cpp"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="archon_search.config"):
        config = load_config(path=toml_file)
    assert config.graph.provider == "llama_cpp"
    assert config.graph.extraction_model is None
    assert any("extraction_model" in r.message for r in caplog.records)


def test_real_graphconfig_constructible_with_all_fields() -> None:
    """AnthropicEnrichmentClient is constructible from a real (non-MagicMock) GraphConfig instance.

    Regression test for Q6: GraphConfig previously lacked extraction_timeout_seconds /
    extraction_rate_limit_rpm / extraction_token_budget, so only MagicMock configs
    (which auto-provide any attribute) could construct an enrichment client.
    """
    from archon_search.config import GraphConfig
    from archon_search.enrichment.anthropic import AnthropicEnrichmentClient

    cfg = GraphConfig(
        provider="anthropic",
        extraction_model="claude-haiku-4-5",
        extraction_timeout_seconds=30.0,
        extraction_rate_limit_rpm=60,
        extraction_token_budget=1024,
    )
    client = AnthropicEnrichmentClient(model="claude-haiku-4-5", config=cfg)
    assert client is not None

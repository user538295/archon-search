"""Tests for archon_search.config ( — Standalone config loader)."""

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
    assert config.max_parallel_collections == 3
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
    assert config.max_parallel_collections == 3
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
    assert config.hyde.timeout_seconds == 5.0
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
    assert config.rag_fusion.timeout_seconds == 5.0
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
    assert config.rag_fusion.timeout_seconds == 5.0
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

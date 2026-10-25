"""Tests for IngestConfig dataclass and SearchConfig.ingest integration.

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task BE-2.

TDD: tests written before IngestConfig was added to config.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, IngestConfig, SearchConfig, load_config
from archon_search.constants import (
    DEFAULT_DOCLING_MAX_TASKS_PER_CHILD,
    DEFAULT_MAX_CONCURRENT_INGEST_TASKS,
    DEFAULT_MAX_CROSS_COLLECTION_COLLECTIONS,
    DEFAULT_MAX_DISCOVERED_FILES,
    DEFAULT_MAX_FILE_MB,
    DEFAULT_MAX_REQUEST_BODY_MB,
    DEFAULT_MAX_ZIP_ENTRIES,
    DEFAULT_MAX_ZIP_UNCOMPRESSED_MB,
    DEFAULT_PARSE_TIMEOUT_SECONDS,
    DEFAULT_PARSE_TIMEOUT_SECONDS_PER_MB,
)


@pytest.fixture
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that would influence data dir or config path."""
    monkeypatch.delenv("ARCHON_SEARCH_HOST", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_PORT", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_ingest_config_default_max_file_mb_is_capped(_no_env: None, tmp_path: Path) -> None:
    """Default IngestConfig caps a file at DEFAULT_MAX_FILE_MB (0 = no limit is an explicit opt-out)."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.ingest.max_file_mb == DEFAULT_MAX_FILE_MB
    assert DEFAULT_MAX_FILE_MB > 0


def test_ingest_config_dataclass_default() -> None:
    """IngestConfig() directly has the capped default."""
    ingest = IngestConfig()
    assert ingest.max_file_mb == DEFAULT_MAX_FILE_MB


def test_search_config_has_ingest_field() -> None:
    """SearchConfig has an ingest field of type IngestConfig."""
    config = SearchConfig()
    assert isinstance(config.ingest, IngestConfig)
    assert config.ingest.max_file_mb == DEFAULT_MAX_FILE_MB


# ---------------------------------------------------------------------------
# Round-trip from TOML
# ---------------------------------------------------------------------------


def test_ingest_config_parsed_from_toml(_no_env: None, tmp_path: Path) -> None:
    """[ingest] max_file_mb = 50 loads correctly."""
    toml_content = "[ingest]\nmax_file_mb = 50\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_file_mb == 50


def test_ingest_config_zero_is_valid(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=0 is valid (zero disables the guard)."""
    toml_content = "[ingest]\nmax_file_mb = 0\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_file_mb == 0


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_ingest_config_negative_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=-1 raises ConfigError at load time."""
    toml_content = "[ingest]\nmax_file_mb = -1\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


def test_ingest_config_float_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=3.5 (TOML float) raises ConfigError."""
    toml_content = "[ingest]\nmax_file_mb = 3.5\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


def test_ingest_config_string_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb="50" (string) raises ConfigError."""
    toml_content = '[ingest]\nmax_file_mb = "50"\n'
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


def test_ingest_config_bool_raises_config_error(_no_env: None, tmp_path: Path) -> None:
    """max_file_mb=true (TOML boolean) raises ConfigError (bool is int subclass in Python)."""
    toml_content = "[ingest]\nmax_file_mb = true\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_file_mb"):
        load_config(path=cfg_path)


# ---------------------------------------------------------------------------
# No [ingest] section (S8: defaults)
# ---------------------------------------------------------------------------


def test_no_ingest_section_defaults_to_the_documented_cap(_no_env: None, tmp_path: Path) -> None:
    """When [ingest] section is absent from TOML, max_file_mb defaults to DEFAULT_MAX_FILE_MB."""
    toml_content = "[server]\nport = 8765\n"
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(toml_content, encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_file_mb == DEFAULT_MAX_FILE_MB


# ---------------------------------------------------------------------------
# max_tasks_per_child — docling parse-worker recycling
# (Documentation/Backlog/2026-08-19-010-image-ocr-unbounded-memory-brief.md)
# ---------------------------------------------------------------------------


def test_ingest_config_default_max_tasks_per_child(_no_env: None, tmp_path: Path) -> None:
    """Default max_tasks_per_child is the shared constant, not a literal in config.py."""
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.ingest.max_tasks_per_child == DEFAULT_DOCLING_MAX_TASKS_PER_CHILD
    assert IngestConfig().max_tasks_per_child == DEFAULT_DOCLING_MAX_TASKS_PER_CHILD


def test_default_docling_max_tasks_per_child_is_25() -> None:
    """Pin the constant's value: it is quoted as fact in five shipped docs (UserManual,
    OperatorGuide, Architecture 110/210, archon-search.toml.example), and
    test_parser_ocr_memory.py's `< _CORPUS_SIZE` precondition depends on it staying below 40.
    A silent change to either bound would desync the docs or break that precondition (C1-I-10).
    """
    assert DEFAULT_DOCLING_MAX_TASKS_PER_CHILD == 25


@pytest.mark.parametrize("value", [10, 1], ids=["typical", "minimum-boundary"])
def test_ingest_config_max_tasks_per_child_parsed_from_toml(
    _no_env: None, tmp_path: Path, value: int
) -> None:
    """[ingest] max_tasks_per_child loads correctly, including at the documented minimum (1)."""
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(f"[ingest]\nmax_tasks_per_child = {value}\n", encoding="utf-8")
    config = load_config(path=cfg_path)
    assert config.ingest.max_tasks_per_child == value


@pytest.mark.parametrize(
    ("value", "expected_fragment"),
    [
        ("0", "must be >= 1"),
        ("-1", "must be >= 1"),
        ("3.5", "Expected integer"),
        ('"10"', "Expected integer"),
        ("true", "Expected integer"),
    ],
    ids=["zero", "negative", "float", "string", "bool"],
)
def test_ingest_config_max_tasks_per_child_invalid_raises(
    _no_env: None, tmp_path: Path, value: str, expected_fragment: str
) -> None:
    """Anything but a positive integer raises ConfigError with the matching one of the two
    distinct messages (config.py: "Expected integer for..." for type errors, "...must be >= 1"
    for range errors — Documentation/Architecture/140_error_handling_strategy.md §"Configuration
    errors").

    Both messages contain the substring "max_tasks_per_child", so matching only that substring
    would let a type-error case pass on a range-error message and vice versa; assert the
    distinguishing fragment too.

    0 is rejected rather than meaning "never recycle": an unbounded worker is exactly the
    unbounded OCR memory growth this setting exists to cap, and ProcessPoolExecutor itself
    requires max_tasks_per_child >= 1.
    """
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(f"[ingest]\nmax_tasks_per_child = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_tasks_per_child") as exc_info:
        load_config(path=cfg_path)
    assert expected_fragment in str(exc_info.value), (
        f"expected the ConfigError message to contain {expected_fragment!r}, "
        f"got {str(exc_info.value)!r}"
    )


# ---------------------------------------------------------------------------
# Hardening keys: allowed_roots, parse timeout, zip bounds, discovery bound, task cap
# ---------------------------------------------------------------------------


def test_hardening_keys_default_to_the_documented_constants(_no_env: None, tmp_path: Path) -> None:
    ingest = load_config(path=tmp_path / "nonexistent.toml").ingest
    assert ingest.allowed_roots == []
    assert ingest.parse_timeout_seconds == DEFAULT_PARSE_TIMEOUT_SECONDS
    assert ingest.parse_timeout_seconds_per_mb == DEFAULT_PARSE_TIMEOUT_SECONDS_PER_MB
    assert ingest.max_zip_entries == DEFAULT_MAX_ZIP_ENTRIES
    assert ingest.max_zip_uncompressed_mb == DEFAULT_MAX_ZIP_UNCOMPRESSED_MB
    assert ingest.max_discovered_files == DEFAULT_MAX_DISCOVERED_FILES
    assert ingest.max_concurrent_tasks == DEFAULT_MAX_CONCURRENT_INGEST_TASKS


def test_hardening_keys_parse_from_toml(_no_env: None, tmp_path: Path) -> None:
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(
        "[ingest]\n"
        'allowed_roots = ["/srv/docs", "/data/corpus"]\n'
        "parse_timeout_seconds = 12.5\n"
        "parse_timeout_seconds_per_mb = 3.5\n"
        "max_zip_entries = 50\n"
        "max_zip_uncompressed_mb = 7\n"
        "max_discovered_files = 99\n"
        "max_concurrent_tasks = 2\n",
        encoding="utf-8",
    )
    ingest = load_config(path=cfg_path).ingest
    assert ingest.allowed_roots == ["/srv/docs", "/data/corpus"]
    assert ingest.parse_timeout_seconds == 12.5
    assert ingest.parse_timeout_seconds_per_mb == 3.5
    assert ingest.max_zip_entries == 50
    assert ingest.max_zip_uncompressed_mb == 7
    assert ingest.max_discovered_files == 99
    assert ingest.max_concurrent_tasks == 2


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ('allowed_roots = "/srv/docs"', "list of non-empty strings"),
        ("allowed_roots = [1]", "list of non-empty strings"),
        ('allowed_roots = [""]', "list of non-empty strings"),
        ('allowed_roots = ["relative/dir"]', "absolute"),
        ("parse_timeout_seconds = 0", "must be > 0"),
        ("parse_timeout_seconds = -1.0", "must be > 0"),
        ('parse_timeout_seconds = "fast"', "Expected float"),
        ("parse_timeout_seconds_per_mb = -0.5", "must be >= 0"),
        ('parse_timeout_seconds_per_mb = "slow"', "Expected float"),
        ("max_zip_entries = 0", "must be >= 1"),
        ("max_zip_uncompressed_mb = 0", "must be >= 1"),
        ("max_discovered_files = 0", "must be >= 1"),
        ("max_concurrent_tasks = 0", "must be >= 1"),
        ("max_concurrent_tasks = true", "Expected integer"),
    ],
)
def test_hardening_keys_reject_invalid_values(_no_env: None, tmp_path: Path, line: str, fragment: str) -> None:
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(f"[ingest]\n{line}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=fragment):
        load_config(path=cfg_path)



def test_a_zero_per_mb_allowance_is_accepted_and_turns_the_scaling_off(_no_env: None, tmp_path: Path) -> None:
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text("[ingest]\nparse_timeout_seconds_per_mb = 0\n", encoding="utf-8")

    assert load_config(path=cfg_path).ingest.parse_timeout_seconds_per_mb == 0.0

# ---------------------------------------------------------------------------
# [server].cors_origins / max_request_body_mb and [graph].max_cross_collection_collections
# ---------------------------------------------------------------------------


def test_server_hardening_keys_default(_no_env: None, tmp_path: Path) -> None:
    config = load_config(path=tmp_path / "nonexistent.toml")
    assert config.cors_origins == []
    assert config.max_request_body_mb == DEFAULT_MAX_REQUEST_BODY_MB
    assert config.graph.max_cross_collection_collections == DEFAULT_MAX_CROSS_COLLECTION_COLLECTIONS


def test_server_hardening_keys_parse(_no_env: None, tmp_path: Path) -> None:
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(
        '[server]\ncors_origins = ["https://app.example.com", "http://localhost:3000"]\nmax_request_body_mb = 4\n'
        "[graph]\nmax_cross_collection_collections = 3\n",
        encoding="utf-8",
    )
    config = load_config(path=cfg_path)
    assert config.cors_origins == ["https://app.example.com", "http://localhost:3000"]
    assert config.max_request_body_mb == 4
    assert config.graph.max_cross_collection_collections == 3


@pytest.mark.parametrize(
    ("section", "line", "fragment"),
    [
        ("server", 'cors_origins = ["*"]', "explicit origins"),
        ("server", 'cors_origins = ["app.example.com"]', "explicit origins"),
        ("server", 'cors_origins = ["https://app.example.com/path"]', "explicit origins"),
        ("server", 'cors_origins = "https://app.example.com"', "list of non-empty strings"),
        ("server", "max_request_body_mb = 0", "must be >= 1"),
        ("graph", "max_cross_collection_collections = 1", "must be >= 2"),
    ],
)
def test_server_hardening_keys_reject_invalid_values(
    _no_env: None, tmp_path: Path, section: str, line: str, fragment: str
) -> None:
    cfg_path = tmp_path / "archon-search.toml"
    cfg_path.write_text(f"[{section}]\n{line}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=fragment):
        load_config(path=cfg_path)

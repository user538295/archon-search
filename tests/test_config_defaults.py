"""Regression baseline: `SearchConfig` defaults when loaded with no env vars and no TOML.

Plan: Documentation/Backlog/C9-container-support-plan.md Task 1.2.

These tests pin every `SearchConfig` field to its expected default value. The
`test_all_defaults_snapshot` test fails whenever a new field is added to
`SearchConfig` without updating the expected dict — guarding against accidental
default-value drift.

All env vars are cleared by the `_clear_archon_env_vars` autouse fixture in
`tests/conftest.py`, so each test starts with a clean environment.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from archon_search.config import SearchConfig, load_config
from archon_search.constants import DEFAULT_FAST_MODEL, DEFAULT_ROUTING_DESCRIPTION_WEIGHT
from archon_search.paths import get_data_dir


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: explicitly delete the three env vars called out in the task spec.

    The autouse `_clear_archon_env_vars` fixture in `tests/conftest.py` already
    clears these, but the task description explicitly mandates `monkeypatch.delenv(...,
    raising=False)` for `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, and
    `ARCHON_SEARCH_DATA_DIR` to guarantee isolation. Keeping it here documents intent.
    """
    monkeypatch.delenv("ARCHON_SEARCH_HOST", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_PORT", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)


def _default_config(tmp_path: Path) -> SearchConfig:
    """Load a fresh `SearchConfig` with no TOML file and no env overrides."""
    return load_config(path=tmp_path / "nonexistent.toml")


# ---------------------------------------------------------------------------
# Per-field default assertions
# ---------------------------------------------------------------------------


def test_default_host(_isolated_env: None, tmp_path: Path) -> None:
    config = _default_config(tmp_path)
    assert config.host == "127.0.0.1"


def test_default_port(_isolated_env: None, tmp_path: Path) -> None:
    config = _default_config(tmp_path)
    assert config.port == 8765


def test_default_db_path(_isolated_env: None, tmp_path: Path) -> None:
    config = _default_config(tmp_path)
    assert config.db_path == "~/.archon-search/search"


def test_default_log_file(_isolated_env: None, tmp_path: Path) -> None:
    config = _default_config(tmp_path)
    assert config.log_file == "~/.archon-search/logs/archon-search.log"


def test_default_telemetry_disabled(_isolated_env: None, tmp_path: Path) -> None:
    config = _default_config(tmp_path)
    assert config.telemetry.enabled is False


def test_default_telemetry_log_dir(_isolated_env: None, tmp_path: Path) -> None:
    config = _default_config(tmp_path)
    assert config.telemetry.log_dir == "~/.archon-search/search-logs"


# ---------------------------------------------------------------------------
# Full-field snapshot
# ---------------------------------------------------------------------------


def test_all_defaults_snapshot(_isolated_env: None, tmp_path: Path) -> None:
    """Snapshot of every `SearchConfig` field default.

    Fails when a new field is added to `SearchConfig` without updating
    `expected` (the keyset assertion below catches this), AND fails when any
    existing default value drifts (the asdict comparison catches this).
    """
    expected: dict[str, object] = {
        # [server]
        "host": "127.0.0.1",
        "port": 8765,
        # [database]
        "db_path": "~/.archon-search/search",
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2",
        "chunk_size": 512,
        "auto_reindex_on_chunk_size_change": True,
        "providers": [],
        "top_k_retrieve": 15,
        "top_k_return": 5,
        # [search] — multi-collection fan-out execution bounds (B3)
        "max_fanout": 8,
        "fanout_leg_trim": 40,
        "fanout_timeout_seconds": 30.0,
        # [routing]
        "routing_shortlist_size": 8,
        "routing_confidence_threshold": 0.30,
        "max_parallel_collections": 3,
        "routing_strategy": "centroid",
        "routing_description_weight": DEFAULT_ROUTING_DESCRIPTION_WEIGHT,
        # [database] — B5 incremental centroid
        "centroid_recompute_threshold": 10_000,
        "centroid_incremental_enabled": True,
        # [database] — C0 tiered install profiles
        "profile": "",
        "multilingual": False,
        # [database] — C2 multilingual language detection
        "language_detection_confidence_threshold": 0.7,
        # [database] — C1 per-collection embedding model
        "embedder_cache_size": 3,
        "eager_load_embedders": False,
        # [collections]
        "pinned_collections": [],
        "collections": [],
        "watch": False,
        # [logging]
        "level": "INFO",
        "log_file": "~/.archon-search/logs/archon-search.log",
        "log_format": "text",
        "backup_count": 7,
        # [telemetry]
        "telemetry": {
            "enabled": False,
            "retention_days": 30,
            "export_enabled": False,
            "log_dir": "~/.archon-search/search-logs",
        },
        # [observability]
        "observability": {
            "stage_timings_enabled": True,
            "request_id_header": "X-Request-ID",
        },
        # [namespaces]
        "namespaces": {},
        # [hyde]
        "hyde": {
            "enabled": False,
            "model": DEFAULT_FAST_MODEL,
            "timeout_seconds": 5.0,
            "max_requests_per_minute": 60,
        },
        # [rag_fusion]
        "rag_fusion": {
            "enabled": False,
            "model": DEFAULT_FAST_MODEL,
            "timeout_seconds": 5.0,
            "max_requests_per_minute": 60,
            "num_queries": 2,
        },
        # [jobs]
        "jobs": {
            "max_concurrent_bulk": 1,
            "checkpoint_interval": 100,
        },
        # [backup]
        "backup": {
            "interval_hours": 0,
            "keep": 7,
            "exclude": [],
            "output_dir": str(get_data_dir() / "backups"),
        },
    }

    # Keyset guard — fails when a new top-level field is added to SearchConfig
    # without updating `expected`. This is the trip-wire the task description
    # explicitly calls for.
    assert set(expected.keys()) == {f.name for f in dataclasses.fields(SearchConfig)}

    # Value-level snapshot — fails when any default shifts.
    config = _default_config(tmp_path)
    assert dataclasses.asdict(config) == expected

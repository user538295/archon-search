"""M12: a startup WARNING when the configured models cannot fit the container's memory limit.

fp32 weights of three cached embedders plus a reranker can exceed an 8 GB limit, and the
OOM kill that follows names no cause. ``memory_budget`` compares a deliberately simple
estimate (spec ``weight_mb`` x headroom + a fixed runtime cost) with the cgroup limit, else
system RAM, and only ever warns — it never refuses to start.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from archon_search import memory_budget
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.memory_budget import (
    estimate_model_memory_mb,
    read_memory_limit,
    warn_if_models_exceed_memory,
)
from archon_search.model_validation import ModelValidationResult
from archon_search.server import app as app_module
from archon_search.server.app import create_app
from archon_search.store import SearchStore

_LOGGER = "archon_search.memory_budget"
_BYTES_PER_MB = 1024 * 1024
_EIGHT_GB = 8 * 1024 * _BYTES_PER_MB
_SMALL_EMBEDDER = "BAAI/bge-small-en-v1.5"
_LARGE_EMBEDDERS = [
    "BAAI/bge-large-en-v1.5",
    "intfloat/multilingual-e5-large",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
]
_LARGE_RERANKER = "BAAI/bge-reranker-v2-m3"
_SMALL_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"


@pytest.fixture
def cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake cgroup v2 ``memory.max``; the v1 file and system RAM are made undetectable."""
    limit_file = tmp_path / "memory.max"
    monkeypatch.setattr(memory_budget, "_CGROUP_LIMIT_FILES", (limit_file, tmp_path / "absent-v1"))
    monkeypatch.setattr(memory_budget, "_system_ram_bytes", lambda: None)
    return limit_file


def _config(reranker: str = _SMALL_RERANKER, cache_size: int = 3, embedding_model: str = _SMALL_EMBEDDER) -> SearchConfig:
    config = SearchConfig()
    config.embedding_model = embedding_model
    config.reranker_model = reranker
    config.embedder_cache_size = cache_size
    return config


def test_estimate_scales_the_spec_weights_by_the_headroom_factor_and_adds_the_runtime_cost() -> None:
    weights = 133.5 + 90.9  # bge-small + MiniLM-L-6 reranker (model_specs weight_mb)

    estimate = estimate_model_memory_mb(
        [_SMALL_EMBEDDER], _SMALL_RERANKER, pinned_model=_SMALL_EMBEDDER, cache_size=3
    )

    assert estimate == pytest.approx(
        weights * memory_budget._WEIGHTS_HEADROOM_FACTOR + memory_budget._RUNTIME_BASELINE_MB
    )


def test_estimate_counts_the_pinned_default_plus_only_the_largest_models_the_cache_holds() -> None:
    pinned = _LARGE_EMBEDDERS[0]  # bge-large, 1340.6 MB: resident whatever the cache size

    def estimate(cache_size: int) -> float:
        return estimate_model_memory_mb(_LARGE_EMBEDDERS, "", pinned_model=pinned, cache_size=cache_size)

    assert estimate(2) > estimate(1) > estimate(0)
    assert estimate(0) == pytest.approx(
        1340.6 * memory_budget._WEIGHTS_HEADROOM_FACTOR + memory_budget._RUNTIME_BASELINE_MB
    )
    assert estimate(1) == pytest.approx(  # plus the largest other: e5-large, 2239.6 MB
        (1340.6 + 2239.6) * memory_budget._WEIGHTS_HEADROOM_FACTOR + memory_budget._RUNTIME_BASELINE_MB
    )
    assert estimate(3) == estimate(2)  # only two models besides the pinned one exist


def test_estimate_ignores_models_outside_the_registry_and_an_empty_reranker() -> None:
    assert estimate_model_memory_mb(
        ["nonexistent/model"], "", pinned_model="nonexistent/model", cache_size=3
    ) == pytest.approx(memory_budget._RUNTIME_BASELINE_MB)


def test_read_memory_limit_reads_a_cgroup_v2_file(cgroup: Path) -> None:
    cgroup.write_text(f"{_EIGHT_GB}\n")

    assert read_memory_limit() == (8192.0, "cgroup memory limit")


def test_read_memory_limit_falls_back_to_the_cgroup_v1_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = tmp_path / "memory.limit_in_bytes"
    v1.write_text(str(_EIGHT_GB))
    monkeypatch.setattr(memory_budget, "_CGROUP_LIMIT_FILES", (tmp_path / "absent-v2", v1))
    monkeypatch.setattr(memory_budget, "_system_ram_bytes", lambda: None)

    assert read_memory_limit() == (8192.0, "cgroup memory limit")


@pytest.mark.parametrize("content", ["max\n", str(2**63 - 4096), "not a number", ""])
def test_an_unlimited_or_unreadable_cgroup_falls_back_to_system_ram(
    cgroup: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    cgroup.write_text(content)
    monkeypatch.setattr(memory_budget, "_system_ram_bytes", lambda: 16 * 1024 * _BYTES_PER_MB)

    assert read_memory_limit() == (16384.0, "system RAM")


def test_the_tighter_of_the_cgroup_limit_and_system_ram_wins(
    cgroup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cgroup.write_text(str(_EIGHT_GB))
    monkeypatch.setattr(memory_budget, "_system_ram_bytes", lambda: 4 * 1024 * _BYTES_PER_MB)

    assert read_memory_limit() == (4096.0, "system RAM")


def test_no_detectable_limit_reads_as_none(cgroup: Path) -> None:
    assert read_memory_limit() is None


def test_three_large_embedders_and_a_reranker_warn_against_an_8gb_limit(
    cgroup: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cgroup.write_text(str(_EIGHT_GB))
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    warn_if_models_exceed_memory(_config(_LARGE_RERANKER, embedding_model=_LARGE_EMBEDDERS[0]), _LARGE_EMBEDDERS)

    [record] = caplog.records
    message = record.getMessage()
    estimate = estimate_model_memory_mb(
        _LARGE_EMBEDDERS, _LARGE_RERANKER, pinned_model=_LARGE_EMBEDDERS[0], cache_size=3
    )
    assert record.levelno == logging.WARNING
    assert f"{estimate:.0f} MB" in message
    assert "8192 MB" in message
    assert "cgroup memory limit" in message
    assert "embedder_cache_size" in message
    assert "smaller profile" in message


def test_the_default_small_profile_fits_an_8gb_limit_silently(
    cgroup: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cgroup.write_text(str(_EIGHT_GB))
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    warn_if_models_exceed_memory(_config(), [_SMALL_EMBEDDER])

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_no_detectable_limit_never_warns_or_raises(cgroup: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    warn_if_models_exceed_memory(_config(_LARGE_RERANKER), _LARGE_EMBEDDERS)

    assert caplog.records == []


def test_the_lifespan_warns_when_the_configured_models_exceed_the_limit(
    cgroup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cgroup.write_text(str(512 * _BYTES_PER_MB))
    monkeypatch.setattr(
        app_module,
        "validate_models_async",
        AsyncMock(return_value=ModelValidationResult(embedder_ok=True, reranker_ok=True)),
    )
    for name, mock in (
        ("connect", AsyncMock()),
        ("_run_startup_migrations", AsyncMock()),
        ("disconnect", AsyncMock()),
        ("ping", AsyncMock(return_value=True)),
        ("get_all_collections_meta", AsyncMock(return_value=[])),
    ):
        monkeypatch.setattr(SearchStore, name, mock)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    with TestClient(create_app(config, JobStore(path=tmp_path / "jobs.json"))):
        pass

    assert any("512 MB" in record.getMessage() for record in caplog.records)

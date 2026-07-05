"""TDD tests for BE-8 (graph GC cycle): schema additions and status builder updates.

Covers:
- GraphCollectionStats.communities_invalidated field (default False)
- GraphStatusDetail.stale_mention_count field (default 0)
- MaintenanceStatusDetail.last_graph_gc_at field (default None)
- _build_graph_status reads communities_invalidated from maintenance state file
- _build_maintenance_status populates last_graph_gc_at from state file
- Both builders share the same _load_state helper (no duplicate reader)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.store import JobStore
from archon_search.server.schemas import (
    GraphCollectionStats,
    GraphStatusDetail,
    MaintenanceStatusDetail,
)


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_graph_collection_stats_has_communities_invalidated() -> None:
    """GraphCollectionStats gains communities_invalidated: bool = False."""
    stats = GraphCollectionStats(collection="docs", node_count=10, edge_count=5)
    assert hasattr(stats, "communities_invalidated"), (
        "GraphCollectionStats must have a 'communities_invalidated' field"
    )
    assert stats.communities_invalidated is False, (
        "communities_invalidated must default to False"
    )


def test_graph_collection_stats_communities_invalidated_set_true() -> None:
    """GraphCollectionStats.communities_invalidated can be set to True."""
    stats = GraphCollectionStats(
        collection="docs", node_count=10, edge_count=5, communities_invalidated=True
    )
    assert stats.communities_invalidated is True


def test_graph_collection_stats_communities_invalidated_in_serialisation() -> None:
    """communities_invalidated appears in model_dump() output."""
    stats = GraphCollectionStats(collection="x", node_count=0, edge_count=0)
    d = stats.model_dump()
    assert "communities_invalidated" in d
    assert d["communities_invalidated"] is False


def test_graph_status_detail_has_stale_mention_count() -> None:
    """GraphStatusDetail gains stale_mention_count: int = 0."""
    detail = GraphStatusDetail(enabled=True, backend_threshold_edges=10_000)
    assert hasattr(detail, "stale_mention_count"), (
        "GraphStatusDetail must have a 'stale_mention_count' field"
    )
    assert detail.stale_mention_count == 0, (
        "stale_mention_count must default to 0"
    )


def test_graph_status_detail_stale_mention_count_set() -> None:
    """GraphStatusDetail.stale_mention_count can be set to a positive integer."""
    detail = GraphStatusDetail(
        enabled=True, backend_threshold_edges=5_000, stale_mention_count=42
    )
    assert detail.stale_mention_count == 42


def test_graph_status_detail_stale_mention_count_in_serialisation() -> None:
    """stale_mention_count appears in model_dump() output."""
    detail = GraphStatusDetail(enabled=True, backend_threshold_edges=1_000, stale_mention_count=7)
    d = detail.model_dump()
    assert "stale_mention_count" in d
    assert d["stale_mention_count"] == 7


def test_maintenance_status_detail_has_last_graph_gc_at() -> None:
    """MaintenanceStatusDetail gains last_graph_gc_at: str | None = None."""
    detail = MaintenanceStatusDetail(enabled=False, interval_hours=0)
    assert hasattr(detail, "last_graph_gc_at"), (
        "MaintenanceStatusDetail must have a 'last_graph_gc_at' field"
    )
    assert detail.last_graph_gc_at is None, (
        "last_graph_gc_at must default to None"
    )


def test_maintenance_status_detail_last_graph_gc_at_set() -> None:
    """MaintenanceStatusDetail.last_graph_gc_at can be set to a non-null ISO string."""
    ts = "2026-07-05T12:00:00+00:00"
    detail = MaintenanceStatusDetail(
        enabled=True, interval_hours=24, last_graph_gc_at=ts
    )
    assert detail.last_graph_gc_at == ts


def test_maintenance_status_detail_last_graph_gc_at_in_serialisation() -> None:
    """last_graph_gc_at appears in model_dump() output."""
    ts = "2026-07-05T08:00:00+00:00"
    detail = MaintenanceStatusDetail(enabled=True, interval_hours=1, last_graph_gc_at=ts)
    d = detail.model_dump()
    assert "last_graph_gc_at" in d
    assert d["last_graph_gc_at"] == ts


# ---------------------------------------------------------------------------
# Unit tests — _build_graph_status reads communities_invalidated from state file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_graph_status_reads_communities_invalidated_from_state() -> None:
    """_build_graph_status reads communities_invalidated=True for a collection from state file."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=3)
    mock_graph_store.edge_count = AsyncMock(return_value=1)

    mock_maintenance_loop = MagicMock()
    mock_maintenance_loop._load_state.return_value = {
        "last_run_at": "2026-07-05T10:00:00+00:00",
        "next_run_at": None,
        "collection_health": {
            f"{DEFAULT_NAMESPACE}/docs": {
                "communities_invalidated": True,
                "fts_optimized_at": None,
                "orphans_removed_last_run": 0,
            }
        },
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": "2026-07-05T10:00:00+00:00",
        "stale_mention_count": 5,
    }

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store
    mock_request.app.state.maintenance_loop = mock_maintenance_loop
    mock_request.state.namespace = DEFAULT_NAMESPACE

    result = await _build_graph_status(mock_request, config, ["docs"])

    assert result is not None
    assert len(result.collections) == 1
    col_stats = result.collections[0]
    assert col_stats.collection == "docs"
    assert col_stats.communities_invalidated is True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated", [True, False])
async def test_build_graph_status_reads_communities_invalidated_both_true_and_false(
    invalidated: bool,
) -> None:
    """_build_graph_status propagates communities_invalidated exactly — both True and False."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=2)
    mock_graph_store.edge_count = AsyncMock(return_value=0)

    mock_maintenance_loop = MagicMock()
    mock_maintenance_loop._load_state.return_value = {
        "last_run_at": None,
        "next_run_at": None,
        "collection_health": {
            f"{DEFAULT_NAMESPACE}/testcol": {
                "communities_invalidated": invalidated,
            }
        },
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": None,
        "stale_mention_count": 0,
    }

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store
    mock_request.app.state.maintenance_loop = mock_maintenance_loop
    mock_request.state.namespace = DEFAULT_NAMESPACE

    result = await _build_graph_status(mock_request, config, ["testcol"])

    assert result is not None
    assert len(result.collections) == 1
    assert result.collections[0].communities_invalidated is invalidated


@pytest.mark.asyncio
async def test_build_graph_status_communities_invalidated_defaults_false_when_not_in_state() -> None:
    """If collection key is absent from state file, communities_invalidated defaults to False."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=1)
    mock_graph_store.edge_count = AsyncMock(return_value=0)

    mock_maintenance_loop = MagicMock()
    # State file has no entry for this collection
    mock_maintenance_loop._load_state.return_value = {
        "last_run_at": None,
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": None,
        "stale_mention_count": 0,
    }

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store
    mock_request.app.state.maintenance_loop = mock_maintenance_loop
    mock_request.state.namespace = DEFAULT_NAMESPACE

    result = await _build_graph_status(mock_request, config, ["newcol"])

    assert result is not None
    assert result.collections[0].communities_invalidated is False


@pytest.mark.asyncio
async def test_build_graph_status_stale_mention_count_from_state() -> None:
    """_build_graph_status reads stale_mention_count from maintenance state file."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=1)
    mock_graph_store.edge_count = AsyncMock(return_value=0)

    mock_maintenance_loop = MagicMock()
    mock_maintenance_loop._load_state.return_value = {
        "last_run_at": None,
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": None,
        "stale_mention_count": 17,
    }

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store
    mock_request.app.state.maintenance_loop = mock_maintenance_loop
    mock_request.state.namespace = DEFAULT_NAMESPACE

    result = await _build_graph_status(mock_request, config, ["col"])

    assert result is not None
    assert result.stale_mention_count == 17


@pytest.mark.asyncio
async def test_build_graph_status_no_maintenance_loop_defaults_stale_zero() -> None:
    """_build_graph_status falls back to 0 stale_mention_count when maintenance_loop absent."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=0)
    mock_graph_store.edge_count = AsyncMock(return_value=0)

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store
    # No maintenance_loop on app.state
    mock_request.app.state = MagicMock(spec=["graph_store"])
    mock_request.app.state.graph_store = mock_graph_store
    mock_request.state.namespace = DEFAULT_NAMESPACE

    result = await _build_graph_status(mock_request, config, [])

    assert result is not None
    assert result.stale_mention_count == 0


# ---------------------------------------------------------------------------
# Unit tests — _build_maintenance_status reads last_graph_gc_at from state file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_maintenance_status_last_graph_gc_at_from_state(tmp_path: Path) -> None:
    """_build_maintenance_status reads last_graph_gc_at from the maintenance state file."""
    from archon_search.server.routes_status import _build_maintenance_status

    gc_ts = "2026-07-05T09:00:00+00:00"

    config = SearchConfig()
    config.maintenance.interval_hours = 0
    config.maintenance.prune_expired_chunks = False

    mock_store = MagicMock()
    mock_store.count_expired_chunks = AsyncMock(return_value=0)

    loop = MaintenanceLoop(
        job_store=MagicMock(),
        search_store=mock_store,
        config=config.maintenance,
        data_dir=tmp_path,
    )
    loop._save_state({
        "last_run_at": gc_ts,
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": gc_ts,
        "stale_mention_count": 0,
    })

    mock_request = MagicMock()
    mock_request.app.state.maintenance_loop = loop

    result = await _build_maintenance_status(
        mock_request, config, DEFAULT_NAMESPACE, mock_store, []
    )

    assert result is not None
    assert result.last_graph_gc_at == gc_ts


@pytest.mark.asyncio
async def test_build_maintenance_status_last_graph_gc_at_null_before_gc(tmp_path: Path) -> None:
    """last_graph_gc_at is null when no GC pass has run yet."""
    from archon_search.server.routes_status import _build_maintenance_status

    config = SearchConfig()
    config.maintenance.interval_hours = 0

    mock_store = MagicMock()
    mock_store.count_expired_chunks = AsyncMock(return_value=0)

    loop = MaintenanceLoop(
        job_store=MagicMock(),
        search_store=mock_store,
        config=config.maintenance,
        data_dir=tmp_path,
    )
    # No state written → empty state, no GC run yet

    mock_request = MagicMock()
    mock_request.app.state.maintenance_loop = loop

    result = await _build_maintenance_status(
        mock_request, config, DEFAULT_NAMESPACE, mock_store, []
    )

    assert result is not None
    assert result.last_graph_gc_at is None


# ---------------------------------------------------------------------------
# Integration test — full app, GET /status after GC via trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_status_graph_fields_after_gc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full app: after writing GC state, GET /status shows stale_mention_count >= 0
    and maintenance.last_graph_gc_at is non-null.
    """
    import sys
    import types

    from fastapi.testclient import TestClient

    from archon_search.collection_meta import CollectionMeta
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    # Use the fixed test API key from conftest to avoid env-var contamination.
    api_key = "0" * 64
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    # Stub spaCy so _check_graph_deps + GraphExtractor import succeed without install
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.util = types.ModuleType("spacy.util")  # type: ignore[attr-defined]
    fake_spacy.util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_spacy.cli = types.ModuleType("spacy.cli")  # type: ignore[attr-defined]
    fake_spacy.load = lambda model: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_spacy.util)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_spacy.cli)  # type: ignore[attr-defined]

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)
    cfg.mcp.enabled = False

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)

    # Mock search store
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name="docs", namespace=DEFAULT_NAMESPACE)]
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    mock_store.count_untagged_language_chunks = AsyncMock(return_value=0)
    mock_store._run_startup_migrations = AsyncMock()
    mock_store.count_expired_chunks = AsyncMock(return_value=0)
    app.state.search_store = mock_store

    # Mock graph store with community stats support
    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=5)
    mock_graph_store.edge_count = AsyncMock(return_value=2)
    mock_graph_store.get_community_stats = AsyncMock(return_value=(3, None))
    mock_graph_store.connect = AsyncMock()
    mock_graph_store.disconnect = AsyncMock()
    app.state.graph_store = mock_graph_store

    gc_ts = "2026-07-05T10:00:00+00:00"

    with TestClient(app) as client:
        # Write a maintenance state simulating a completed GC pass
        loop: MaintenanceLoop = client.app.state.maintenance_loop
        loop._save_state({
            "last_run_at": gc_ts,
            "next_run_at": None,
            "collection_health": {
                f"{DEFAULT_NAMESPACE}/docs": {
                    "communities_invalidated": False,
                    "fts_optimized_at": None,
                    "orphans_removed_last_run": 0,
                    "last_retry_at": None,
                    "last_error": None,
                    "meta_chunk_count": 10,
                    "expired_chunks_removed_last_run": 0,
                }
            },
            "retry_counts": {},
            "last_expired_pruned_at": None,
            "last_graph_gc_at": gc_ts,
            "stale_mention_count": 3,
        })

        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        # graph sub-object present and stale_mention_count >= 0
        assert data.get("graph") is not None, "graph sub-object must be non-null"
        assert data["graph"]["stale_mention_count"] >= 0, (
            "stale_mention_count must be >= 0"
        )
        assert data["graph"]["stale_mention_count"] == 3, (
            f"Expected stale_mention_count=3, got {data['graph']['stale_mention_count']}"
        )

        # maintenance.last_graph_gc_at is non-null after GC
        assert data.get("maintenance") is not None, "maintenance sub-object must be non-null"
        assert data["maintenance"]["last_graph_gc_at"] is not None, (
            "maintenance.last_graph_gc_at must be non-null after a GC pass"
        )
        assert data["maintenance"]["last_graph_gc_at"] == gc_ts

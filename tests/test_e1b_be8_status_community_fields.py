"""Unit and integration tests for BE-8: community_count and last_built_at in StatusCollectionEntry.

Tests:
  - StatusCollectionEntry has community_count=0 and last_built_at=None as defaults
  - GET /status includes community_count >= 1 and last_built_at non-null when communities built
  - GET /status shows stale last_built_at after new ingest (communities not rebuilt)
  - GET /status shows community_count=0 and last_built_at=None when no communities
  - Community stats not fetched when graph is disabled

Scenarios covered: C2, S4, S14
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import GraphConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Unit tests — StatusCollectionEntry schema
# ---------------------------------------------------------------------------


def test_status_collection_entry_community_fields_default() -> None:
    """StatusCollectionEntry has community_count=0 and last_built_at=None as defaults (BE-8, C2).

    This is the primary structural guard — new fields must be present with sensible
    defaults so existing serialised responses (missing the fields) deserialise correctly.
    """
    from archon_search.server.schemas import StatusCollectionEntry

    entry = StatusCollectionEntry(name="col", path="/tmp", status="not_yet_indexed", watching=False)
    assert entry.community_count == 0, "community_count must default to 0"
    assert entry.last_built_at is None, "last_built_at must default to None"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    tmp_db: Path,
    *,
    graph_enabled: bool = True,
    community_count: int = 0,
    last_built_at_dt: datetime | None = None,
    collection_name: str = "testcol",
) -> TestClient:
    """Build a TestClient where graph is toggled on app.state after creation.

    Creates the app with graph.enabled=False to bypass the spaCy import guard
    (_check_graph_deps), then overrides app.state.config.graph.enabled and
    app.state.graph_store so routes_status can read community stats.
    """
    from archon_search.collection_meta import CollectionMeta

    # Always create app with graph disabled to avoid spaCy import at create_app time.
    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.graph = GraphConfig(enabled=False)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    # Wire a mock search_store with a single named collection.
    mock_meta = MagicMock(spec=CollectionMeta)
    mock_meta.namespace = "default"
    mock_meta.name = collection_name
    mock_meta.needs_reindex = False
    mock_meta.schema_version = 0

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[mock_meta])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    mock_store.count_untagged_language_chunks = AsyncMock(return_value=0)
    app.state.search_store = mock_store

    # Flip graph.enabled on the live config so the status builder sees enabled=True.
    app.state.config.graph.enabled = graph_enabled

    if graph_enabled:
        # Wire a mock graph_store that returns the configured stats.
        mock_graph_store = MagicMock()
        mock_graph_store.get_community_stats = AsyncMock(
            return_value=(community_count, last_built_at_dt)
        )
        mock_graph_store.node_count = AsyncMock(return_value=0)
        mock_graph_store.edge_count = AsyncMock(return_value=0)
        app.state.graph_store = mock_graph_store

    api_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {api_key}"})


def _find_collection(data: dict, name: str) -> dict | None:
    return next((c for c in data["collections"] if c["name"] == name), None)


# ---------------------------------------------------------------------------
# Integration tests — GET /status
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_status_includes_community_count(tmp_path: Path) -> None:
    """GET /status includes community_count >= 1 and last_built_at non-null after communities built (S4).

    The status builder must call graph_store.get_community_stats(collection) for each
    collection and surface the result in the per-collection StatusCollectionEntry.
    """
    built_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    client = _make_client(
        tmp_path,
        graph_enabled=True,
        community_count=5,
        last_built_at_dt=built_at,
        collection_name="mycol",
    )
    response = client.get("/status")
    assert response.status_code == 200, f"GET /status failed: {response.text}"
    data = response.json()

    col = _find_collection(data, "mycol")
    assert col is not None, f"'mycol' missing from collections: {data['collections']}"
    assert col["community_count"] == 5, f"Expected community_count=5, got {col['community_count']}"
    assert col["last_built_at"] is not None, "last_built_at must be non-null after communities built"


@pytest.mark.integration
def test_status_last_built_at_shows_before_reingest(tmp_path: Path) -> None:
    """GET /status shows stale last_built_at after new ingest if communities not rebuilt (S14).

    The last_built_at timestamp reflects the last time build-communities was run, NOT
    the last ingest.  After a new document is ingested without rebuilding communities,
    the timestamp must remain unchanged — signalling staleness to the operator.
    """
    built_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    client = _make_client(
        tmp_path,
        graph_enabled=True,
        community_count=3,
        last_built_at_dt=built_at,
        collection_name="stalecol",
    )
    response = client.get("/status")
    assert response.status_code == 200

    col = _find_collection(response.json(), "stalecol")
    assert col is not None
    # The timestamp comes from communities, not from ingest — so it's the build timestamp.
    assert col["last_built_at"] is not None
    # Must contain "2026-01-01" — the build date, not a later ingest date.
    assert "2026-01-01" in col["last_built_at"], (
        f"Expected build date 2026-01-01 in last_built_at, got: {col['last_built_at']}"
    )


@pytest.mark.integration
def test_get_status_community_count_zero_when_no_communities(tmp_path: Path) -> None:
    """GET /status shows community_count=0 and last_built_at=None when no communities built."""
    client = _make_client(
        tmp_path,
        graph_enabled=True,
        community_count=0,
        last_built_at_dt=None,
        collection_name="emptycol",
    )
    response = client.get("/status")
    assert response.status_code == 200

    col = _find_collection(response.json(), "emptycol")
    assert col is not None
    assert col["community_count"] == 0
    assert col["last_built_at"] is None


@pytest.mark.integration
def test_get_status_community_fields_absent_when_graph_disabled(tmp_path: Path) -> None:
    """When graph.enabled=False, community stats are not fetched from graph_store.

    The collection entry still carries community_count=0 and last_built_at=None (defaults).
    graph_store.get_community_stats must NOT be called.
    """
    from archon_search.collection_meta import CollectionMeta

    config = SearchConfig()
    config.db_path = str(tmp_path)
    config.graph = GraphConfig(enabled=False)
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)

    col_name = "graphless"
    mock_meta = MagicMock(spec=CollectionMeta)
    mock_meta.namespace = "default"
    mock_meta.name = col_name
    mock_meta.needs_reindex = False
    mock_meta.schema_version = 0

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[mock_meta])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    mock_store.count_untagged_language_chunks = AsyncMock(return_value=0)
    app.state.search_store = mock_store

    # Spy graph_store to verify get_community_stats is never called.
    mock_graph_store = MagicMock()
    mock_graph_store.get_community_stats = AsyncMock()
    app.state.graph_store = mock_graph_store

    api_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {api_key}"})

    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()

    col = _find_collection(data, col_name)
    assert col is not None
    # Defaults must hold when graph is disabled.
    assert col["community_count"] == 0
    assert col["last_built_at"] is None
    # Critical: get_community_stats must not have been called.
    mock_graph_store.get_community_stats.assert_not_called()


@pytest.mark.integration
def test_get_status_community_stats_called_per_collection(tmp_path: Path) -> None:
    """GET /status calls get_community_stats once per collection with the correct name (C1-B-2).

    Uses two collections with distinct community counts to prove the per-collection
    mapping is correct and that the correct argument is passed to get_community_stats.
    """
    from archon_search.collection_meta import CollectionMeta

    config = SearchConfig()
    config.db_path = str(tmp_path)
    config.graph = GraphConfig(enabled=False)
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)

    col_a, col_b = "alpha", "beta"

    def _make_meta(name: str) -> MagicMock:
        m = MagicMock(spec=CollectionMeta)
        m.namespace = "default"
        m.name = name
        m.needs_reindex = False
        m.schema_version = 0
        return m

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[_make_meta(col_a), _make_meta(col_b)])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    mock_store.count_untagged_language_chunks = AsyncMock(return_value=0)
    app.state.search_store = mock_store

    app.state.config.graph.enabled = True

    built_at_a = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    built_at_b = datetime(2026, 2, 15, 9, 0, 0, tzinfo=timezone.utc)

    # Return distinct stats keyed on the collection name argument.
    stats_by_col = {col_a: (7, built_at_a), col_b: (2, built_at_b)}

    async def _side_effect(name: str, ns: str = "default") -> tuple:
        return stats_by_col[name]

    mock_graph_store = MagicMock()
    mock_graph_store.get_community_stats = _side_effect
    mock_graph_store.node_count = AsyncMock(return_value=0)
    mock_graph_store.edge_count = AsyncMock(return_value=0)
    app.state.graph_store = mock_graph_store

    api_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {api_key}"})

    response = client.get("/status")
    assert response.status_code == 200, f"GET /status failed: {response.text}"
    data = response.json()

    entry_a = _find_collection(data, col_a)
    entry_b = _find_collection(data, col_b)
    assert entry_a is not None, f"'{col_a}' missing from collections"
    assert entry_b is not None, f"'{col_b}' missing from collections"

    assert entry_a["community_count"] == 7, f"Expected 7 for {col_a}, got {entry_a['community_count']}"
    assert "2026-01-01" in entry_a["last_built_at"], (
        f"Expected 2026-01-01 in {col_a}'s last_built_at, got {entry_a['last_built_at']}"
    )
    assert entry_b["community_count"] == 2, f"Expected 2 for {col_b}, got {entry_b['community_count']}"
    assert "2026-02-15" in entry_b["last_built_at"], (
        f"Expected 2026-02-15 in {col_b}'s last_built_at, got {entry_b['last_built_at']}"
    )


@pytest.mark.integration
def test_get_status_degrades_gracefully_when_community_stats_raises(tmp_path: Path) -> None:
    """GET /status returns 200 even when graph_store.get_community_stats raises (C1-I-1).

    If LanceDB returns an unexpected error the endpoint must degrade gracefully —
    returning community_count=0 and last_built_at=None for the affected collection
    instead of propagating the exception as a 500.
    """
    from archon_search.collection_meta import CollectionMeta

    config = SearchConfig()
    config.db_path = str(tmp_path)
    config.graph = GraphConfig(enabled=False)
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)

    col_name = "errcol"
    mock_meta = MagicMock(spec=CollectionMeta)
    mock_meta.namespace = "default"
    mock_meta.name = col_name
    mock_meta.needs_reindex = False
    mock_meta.schema_version = 0

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[mock_meta])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    mock_store.count_untagged_language_chunks = AsyncMock(return_value=0)
    app.state.search_store = mock_store

    # Flip graph.enabled so the status builder enters the community-stats branch.
    app.state.config.graph.enabled = True

    mock_graph_store = MagicMock()
    mock_graph_store.get_community_stats = AsyncMock(side_effect=RuntimeError("LanceDB error"))
    mock_graph_store.node_count = AsyncMock(return_value=0)
    mock_graph_store.edge_count = AsyncMock(return_value=0)
    app.state.graph_store = mock_graph_store

    api_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {api_key}"})

    response = client.get("/status")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

    data = response.json()
    col = _find_collection(data, col_name)
    assert col is not None, f"'{col_name}' missing from collections: {data['collections']}"
    assert col["community_count"] == 0, f"Expected community_count=0 on error, got {col['community_count']}"
    assert col["last_built_at"] is None, f"Expected last_built_at=None on error, got {col['last_built_at']}"

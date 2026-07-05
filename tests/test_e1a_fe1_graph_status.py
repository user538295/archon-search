"""Tests for FE-1: GraphStatusDetail + GraphCollectionStats in schemas.py,
StatusResponse.graph field, and _build_graph_status() builder in routes_status.py.

Scenarios covered: C2, S3, S15
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.server.schemas import GraphCollectionStats, GraphStatusDetail, StatusResponse


# ---------------------------------------------------------------------------
# Unit tests — schemas
# ---------------------------------------------------------------------------


def test_status_response_graph_field_present() -> None:
    """StatusResponse has a ``graph: GraphStatusDetail | None`` field (FE-1)."""
    from pydantic.fields import FieldInfo

    fields = StatusResponse.model_fields
    assert "graph" in fields, "StatusResponse must have a 'graph' field"
    field: FieldInfo = fields["graph"]
    # Default must be None (additive, optional sub-object)
    assert field.default is None, "StatusResponse.graph must default to None"


def test_graph_collection_stats_fields() -> None:
    """GraphCollectionStats has collection, node_count, edge_count."""
    stats = GraphCollectionStats(collection="docs", node_count=10, edge_count=5)
    assert stats.collection == "docs"
    assert stats.node_count == 10
    assert stats.edge_count == 5


def test_graph_status_detail_fields() -> None:
    """GraphStatusDetail carries enabled, backend_threshold_edges, and collections."""
    detail = GraphStatusDetail(
        enabled=True,
        backend_threshold_edges=10_000,
        collections=[GraphCollectionStats(collection="docs", node_count=3, edge_count=1)],
    )
    assert detail.enabled is True
    assert detail.backend_threshold_edges == 10_000
    assert len(detail.collections) == 1
    assert detail.collections[0].collection == "docs"


def test_graph_status_detail_collections_defaults_to_empty() -> None:
    """GraphStatusDetail.collections defaults to empty list."""
    detail = GraphStatusDetail(enabled=False, backend_threshold_edges=10_000)
    assert detail.collections == []


# ---------------------------------------------------------------------------
# Unit tests — _build_graph_status builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_graph_status_returns_none_when_disabled() -> None:
    """config.graph.enabled=False → _build_graph_status returns None."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=False)

    mock_request = MagicMock()
    mock_request.app.state.graph_store = None

    result = await _build_graph_status(mock_request, config, [])
    assert result is None


@pytest.mark.asyncio
async def test_build_graph_status_returns_none_when_no_graph_store() -> None:
    """graph.enabled=True but graph_store absent on app.state → None (guard against missing state)."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)

    mock_request = MagicMock()
    mock_request.app.state = MagicMock(spec=[])  # no graph_store attribute

    result = await _build_graph_status(mock_request, config, ["docs"])
    assert result is None


@pytest.mark.asyncio
async def test_build_graph_status_includes_collection_stats() -> None:
    """Stub GraphStore returns known counts; assert detail object fields."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True, backend_threshold_edges=5_000)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=7)
    mock_graph_store.edge_count = AsyncMock(return_value=3)

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store

    result = await _build_graph_status(mock_request, config, ["docs"])

    assert result is not None
    assert result.enabled is True
    assert result.backend_threshold_edges == 5_000
    assert len(result.collections) == 1
    assert result.collections[0].collection == "docs"
    assert result.collections[0].node_count == 7
    assert result.collections[0].edge_count == 3


@pytest.mark.asyncio
async def test_build_graph_status_empty_ns_names() -> None:
    """graph.enabled=True but no collections in namespace → empty collections list."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=0)
    mock_graph_store.edge_count = AsyncMock(return_value=0)

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store

    result = await _build_graph_status(mock_request, config, [])

    assert result is not None
    assert result.enabled is True
    assert result.collections == []


@pytest.mark.asyncio
async def test_build_graph_status_multiple_collections() -> None:
    """Multiple collections each get their own stats entry."""
    from archon_search.server.routes_status import _build_graph_status

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)

    node_counts = {"col_a": 10, "col_b": 20}
    edge_counts = {"col_a": 4, "col_b": 8}

    async def _node_count(col: str, ns: str = "default") -> int:
        return node_counts[col]

    async def _edge_count(col: str, ns: str = "default") -> int:
        return edge_counts[col]

    mock_graph_store = MagicMock()
    mock_graph_store.node_count = _node_count
    mock_graph_store.edge_count = _edge_count

    mock_request = MagicMock()
    mock_request.app.state.graph_store = mock_graph_store

    result = await _build_graph_status(mock_request, config, ["col_a", "col_b"])

    assert result is not None
    by_name = {s.collection: s for s in result.collections}
    assert by_name["col_a"].node_count == 10
    assert by_name["col_a"].edge_count == 4
    assert by_name["col_b"].node_count == 20
    assert by_name["col_b"].edge_count == 8


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_status_graph_subobject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status returns graph sub-object with enabled:true when graph is enabled."""
    import secrets
    import sys
    import types

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

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
    cfg.graph.enabled = True
    cfg.mcp.enabled = False

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)

    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

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
    app.state.search_store = mock_store

    # Stub the graph_store so node/edge count calls don't require a real LanceDB DB
    mock_graph_store = MagicMock()
    mock_graph_store.node_count = AsyncMock(return_value=5)
    mock_graph_store.edge_count = AsyncMock(return_value=2)
    mock_graph_store.connect = AsyncMock()
    mock_graph_store.disconnect = AsyncMock()
    app.state.graph_store = mock_graph_store

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "graph" in data, f"'graph' key missing from status response: {list(data.keys())}"
        graph = data["graph"]
        assert graph is not None, "graph sub-object is None with graph.enabled=True"
        assert graph["enabled"] is True
        assert "backend_threshold_edges" in graph
        assert graph["backend_threshold_edges"] == 10_000  # default
        assert "collections" in graph, "'collections' key missing from graph sub-object"
        # 'docs' collection was set up via mock_store.get_all_collections_meta
        assert len(graph["collections"]) == 1, f"Expected 1 collection, got {len(graph['collections'])}"
        col_stats = graph["collections"][0]
        assert col_stats["collection"] == "docs"
        assert col_stats["node_count"] == 5  # from mock_graph_store.node_count AsyncMock
        assert col_stats["edge_count"] == 2  # from mock_graph_store.edge_count AsyncMock


@pytest.mark.integration
def test_get_status_graph_null_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status returns graph:null when graph.enabled=False (S15: config flag takes precedence over store presence)."""
    import secrets

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.graph.enabled = False
    cfg.mcp.enabled = False

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)

    # S15: graph_store present on app.state but config.graph.enabled=False → still returns null
    mock_graph_store_ignored = MagicMock()
    mock_graph_store_ignored.node_count = AsyncMock(return_value=999)
    mock_graph_store_ignored.edge_count = AsyncMock(return_value=999)
    mock_graph_store_ignored.connect = AsyncMock()
    mock_graph_store_ignored.disconnect = AsyncMock()
    app.state.graph_store = mock_graph_store_ignored

    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    mock_store.count_untagged_language_chunks = AsyncMock(return_value=0)
    mock_store._run_startup_migrations = AsyncMock()
    app.state.search_store = mock_store

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "graph" in data, f"'graph' key missing from status response"
        assert data["graph"] is None, f"graph should be null when disabled, got: {data['graph']}"

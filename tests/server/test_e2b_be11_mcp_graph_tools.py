"""BE-11 — MCP graph inspection tools unit tests.

Tests:
- test_mcp_get_graph_disabled_returns_mcp_error
    graph.enabled=False → McpErrorResponse with code="graph_disabled"
- test_mcp_get_graph_cross_collection_disabled_returns_mcp_error
    graph.enabled=False; call MCP get_graph_cross_collection;
    assert McpErrorResponse with code="graph_disabled"
- test_mcp_get_graph_summary_shape
    mock inspector; result contains top_nodes (≤20), top_edges (≤20),
    entity_type_distribution

Scenario: BE-11 graph inspection MCP tools
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Stub fastmcp only when the real package is unavailable.  When the real
# fastmcp IS importable (CI and local dev), prefer it so the module-level
# ``from fastmcp import FastMCP`` in mcp.py gets the real class.
if "fastmcp" not in sys.modules:
    try:
        import fastmcp as _real_fastmcp  # type: ignore[import]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _stub_fastmcp = types.ModuleType("fastmcp")
        _stub_fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _stub_fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _stub_fastmcp


# ---------------------------------------------------------------------------
# FastMCP stub
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func
        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func
        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


def _make_app(
    pipeline: MagicMock,
    graph_store: MagicMock | None = None,
    config: MagicMock | None = None,
) -> _FakeApp:
    """Create an MCP app with mocked dependencies."""
    if config is None:
        config = MagicMock()
        config.graph = MagicMock()
        config.graph.enabled = False
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(
            pipeline,
            "default",
            writer=None,
            config=config,
            graph_store=graph_store,
        )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_mcp_get_graph_disabled_returns_mcp_error() -> None:
    """When graph.enabled=False, get_graph returns McpErrorResponse with code="graph_disabled"."""
    pipeline = MagicMock()
    config = MagicMock()
    config.graph = MagicMock()
    config.graph.enabled = False

    app = _make_app(pipeline, graph_store=None, config=config)
    get_graph_tool = app.tools["get_graph"]

    # Call the tool directly with a collection name
    result = AsyncMock(return_value={})
    with patch("archon_search.server.mcp._get_request_namespace", return_value="default"):
        # Manually invoke the tool as an async function
        import asyncio
        result = asyncio.run(get_graph_tool("test-collection"))

    assert isinstance(result, dict)
    assert result.get("code") == "graph_disabled"


def test_mcp_get_graph_cross_collection_disabled_returns_mcp_error() -> None:
    """When graph.enabled=False, get_graph_cross_collection returns McpErrorResponse with code="graph_disabled"."""
    pipeline = MagicMock()
    config = MagicMock()
    config.graph = MagicMock()
    config.graph.enabled = False

    app = _make_app(pipeline, graph_store=None, config=config)
    get_graph_cc_tool = app.tools["get_graph_cross_collection"]

    # Call the tool directly with two collection names
    import asyncio
    with patch("archon_search.server.mcp._get_request_namespace", return_value="default"):
        result = asyncio.run(get_graph_cc_tool(["col1", "col2"]))

    assert isinstance(result, dict)
    assert result.get("code") == "graph_disabled"


def test_mcp_get_graph_summary_shape() -> None:
    """get_graph tool returns dict with required fields: top_nodes (list), top_edges (list), entity_type_distribution (dict)."""
    import asyncio

    # Create a simple mock GraphInspection result
    class MockGraphNode:
        def __init__(self, entity_id: str, entity_name: str, chunk_count: int, salience: float):
            self.entity_id = entity_id
            self.entity_name = entity_name
            self.chunk_count = chunk_count
            self.salience = salience

    class MockGraphEdge:
        def __init__(self, edge_id: str, source_entity_id: str, target_entity_id: str, weight: float):
            self.edge_id = edge_id
            self.source_entity_id = source_entity_id
            self.target_entity_id = target_entity_id
            self.weight = weight

    class MockGraphView:
        def __init__(self):
            self.node_count = 2
            self.edge_count = 1
            self.nodes = [
                MockGraphNode("PERSON:alice", "alice", 5, 0.95),
                MockGraphNode("PLACE:seattle", "seattle", 3, 0.75),
            ]
            self.edges = [
                MockGraphEdge("e1", "PERSON:alice", "PLACE:seattle", 0.8),
            ]

    # Mock pipeline and graph_store
    pipeline = MagicMock()
    meta = MagicMock()
    meta.chunk_count = 100
    pipeline.get_collection_meta = AsyncMock(return_value=meta)

    graph_store = AsyncMock()

    config = MagicMock()
    config.graph = MagicMock()
    config.graph.enabled = True
    config.graph.max_inspection_nodes = 5000
    config.graph.max_inspection_edges = 25000

    app = _make_app(pipeline, graph_store=graph_store, config=config)
    get_graph_tool = app.tools["get_graph"]

    # Mock the inspector function
    async def mock_inspect(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockGraphView()

    with patch(
        "archon_search.graph_inspector.inspect_collection",
        new_callable=AsyncMock,
        side_effect=mock_inspect,
    ):
        with patch("archon_search.server.mcp._get_request_namespace", return_value="default"):
            result = asyncio.run(get_graph_tool("test-collection"))

    # Verify result is a dict with required fields
    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result}"
    assert "node_count" in result, f"Missing node_count in {result.keys()}"
    assert "edge_count" in result, f"Missing edge_count in {result.keys()}"
    assert "entity_type_distribution" in result, f"Missing entity_type_distribution in {result.keys()}"
    assert "top_nodes" in result, f"Missing top_nodes in {result.keys()}"
    assert "top_edges" in result, f"Missing top_edges in {result.keys()}"

    # Verify top_nodes and top_edges are lists
    assert isinstance(result["top_nodes"], list), "top_nodes should be a list"
    assert isinstance(result["top_edges"], list), "top_edges should be a list"
    assert isinstance(result["entity_type_distribution"], dict), "entity_type_distribution should be a dict"

    # Verify constraints
    assert len(result["top_nodes"]) <= 20, "top_nodes length should be ≤ 20"
    assert len(result["top_edges"]) <= 20, "top_edges length should be ≤ 20"

    # Verify top_node structure
    for node in result["top_nodes"]:
        assert isinstance(node, dict), "Each top_node should be a dict"
        assert "entity_id" in node, "top_node missing entity_id"
        assert "entity_name" in node, "top_node missing entity_name"
        assert "chunk_count" in node, "top_node missing chunk_count"
        assert "salience" in node, "top_node missing salience"

    # Verify top_edge structure
    for edge in result["top_edges"]:
        assert isinstance(edge, dict), "Each top_edge should be a dict"
        assert "edge_id" in edge, "top_edge missing edge_id"
        assert "source_entity_id" in edge, "top_edge missing source_entity_id"
        assert "target_entity_id" in edge, "top_edge missing target_entity_id"
        assert "weight" in edge, "top_edge missing weight"

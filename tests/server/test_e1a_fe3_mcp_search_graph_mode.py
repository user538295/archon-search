"""Tests for FE-3: graph_mode on MCP search tool and search_with_context guard.

Covers:
- McpSearchResponse has graph_expansion_applied field (False by default)
- MCP search tool accepts graph_mode parameter
- graph_mode forwarded to pipeline.search and pipeline.search_many
- graph_mode disabled → code='graph_disabled' (not exception)
- graph_mode unknown value → code='invalid_graph_mode'
- search_with_context with graph_mode → code='graph_mode_not_supported'
- expansion_used includes graph_expansion_applied in all three response paths
- integration roundtrip: graph_expansion_applied present in response dict
"""
from __future__ import annotations

import inspect
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Resolve fastmcp the same way sibling tests do.
if "fastmcp" not in sys.modules:
    try:
        import fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp

from archon_search.pipeline import SearchPipelineResult


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


def _make_pipeline(
    *,
    graph_expansion_applied: bool = False,
) -> Any:
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[],
            acl_filtered=False,
            graph_expansion_applied=graph_expansion_applied,
        )
    )
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(
            results=[],
            acl_filtered=False,
            graph_expansion_applied=graph_expansion_applied,
        )
    )
    return pipeline


def _make_mcp_app(
    pipeline: Any,
    *,
    graph_enabled: bool = True,
) -> _FakeApp:
    from archon_search.config import GraphConfig, SearchConfig

    config = SearchConfig()
    config.graph = GraphConfig(enabled=graph_enabled)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        app = mcp_module.create_app(pipeline, "default", config=config)
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Unit tests — McpSearchResponse schema
# ---------------------------------------------------------------------------


def test_mcp_search_response_includes_graph_expansion_applied_field() -> None:
    """McpSearchResponse has graph_expansion_applied field; False by default."""
    from archon_search.server.mcp_schemas import McpSearchResponse

    resp = McpSearchResponse(results=[], acl_filtered=False, excluded_collections=[])
    assert resp.graph_expansion_applied is False

    resp_true = McpSearchResponse(
        results=[], acl_filtered=False, excluded_collections=[], graph_expansion_applied=True
    )
    assert resp_true.graph_expansion_applied is True


# ---------------------------------------------------------------------------
# Unit tests — search tool signature
# ---------------------------------------------------------------------------


def test_mcp_search_graph_mode_param_accepted() -> None:
    """MCP search tool signature includes graph_mode parameter."""
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        pipeline = _make_pipeline()
        app = mcp_module.create_app(pipeline, "default")
    search_fn = app.tools["search"]  # type: ignore[union-attr]
    sig = inspect.signature(search_fn)
    assert "graph_mode" in sig.parameters, (
        "search tool must accept graph_mode parameter"
    )
    param = sig.parameters["graph_mode"]
    assert param.default is None, "graph_mode default must be None"


def test_mcp_search_with_context_graph_mode_param_accepted() -> None:
    """MCP search_with_context tool signature includes graph_mode parameter."""
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        pipeline = _make_pipeline()
        app = mcp_module.create_app(pipeline, "default")
    swc_fn = app.tools["search_with_context"]  # type: ignore[union-attr]
    sig = inspect.signature(swc_fn)
    assert "graph_mode" in sig.parameters, (
        "search_with_context tool must accept graph_mode parameter"
    )


# ---------------------------------------------------------------------------
# Unit tests — graph_mode forwarding (single-collection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_forwarded() -> None:
    """graph_mode='naive' forwarded to pipeline.search."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collection="col", graph_mode="naive")

    assert "error" not in result, f"Unexpected error: {result}"
    call_kwargs = pipeline.search.call_args.kwargs
    assert call_kwargs.get("graph_mode") == "naive"


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_none_forwarded() -> None:
    """graph_mode=None (default) forwarded to pipeline.search as None."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    await search_fn(query="hello", collection="col")

    call_kwargs = pipeline.search.call_args.kwargs
    assert call_kwargs.get("graph_mode") is None


# ---------------------------------------------------------------------------
# Unit tests — graph_mode forwarding (multi-collection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_forwarded_multi_collection() -> None:
    """graph_mode='naive' forwarded to pipeline.search_many."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collections=["a", "b"], graph_mode="naive")

    assert "error" not in result, f"Unexpected error: {result}"
    call_kwargs = pipeline.search_many.call_args.kwargs
    assert call_kwargs.get("graph_mode") == "naive"


# ---------------------------------------------------------------------------
# Unit tests — graph_mode guard: disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_disabled_returns_error_code() -> None:
    """graph.enabled=False + graph_mode='naive' → code='graph_disabled'."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=False)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collection="col", graph_mode="naive")

    assert result.get("code") == "graph_disabled", f"Expected code='graph_disabled', got: {result}"
    assert "graph" in result.get("error", "").lower() or "enabled" in result.get("error", "").lower()
    pipeline.search.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_disabled_multi_collection_returns_error_code() -> None:
    """Multi-collection: graph.enabled=False + graph_mode='naive' → code='graph_disabled'."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=False)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collections=["a", "b"], graph_mode="naive")

    assert result.get("code") == "graph_disabled", f"Expected code='graph_disabled', got: {result}"
    pipeline.search_many.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — graph_mode guard: unknown value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_unknown_value_returns_error() -> None:
    """graph_mode='unknown' → code='invalid_graph_mode'."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collection="col", graph_mode="unknown")

    assert result.get("code") == "invalid_graph_mode", f"Expected code='invalid_graph_mode', got: {result}"
    pipeline.search.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_local_value_returns_error() -> None:
    """graph_mode='local' → code='invalid_graph_mode' (deferred to E1b)."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collection="col", graph_mode="local")

    assert result.get("code") == "invalid_graph_mode", f"Expected code='invalid_graph_mode', got: {result}"


# ---------------------------------------------------------------------------
# Unit tests — search_with_context graph_mode guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_with_context_graph_mode_returns_error() -> None:
    """search_with_context with graph_mode='naive' → code='graph_mode_not_supported'."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    swc_fn = app.tools["search_with_context"]  # type: ignore[union-attr]

    result = await swc_fn(query="hello", collection="col", graph_mode="naive")

    assert result.get("code") == "graph_mode_not_supported", (
        f"Expected code='graph_mode_not_supported', got: {result}"
    )
    assert "deferred" in result.get("error", "").lower() or "E1c" in result.get("error", ""), (
        f"Expected error to mention deferral, got: {result.get('error', '')!r}"
    )
    # Guard must fire BEFORE calling the pipeline's search_with_context method.
    pipeline.search_with_context.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_with_context_graph_mode_local_returns_error() -> None:
    """search_with_context with graph_mode='local' → code='graph_mode_not_supported'."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    swc_fn = app.tools["search_with_context"]  # type: ignore[union-attr]

    result = await swc_fn(query="hello", collection="col", graph_mode="local")

    assert result.get("code") == "graph_mode_not_supported"
    # Guard must fire BEFORE calling the pipeline's search_with_context method.
    pipeline.search_with_context.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — expansion_used includes graph_expansion_applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_expansion_used_includes_graph_expansion_single() -> None:
    """Single-collection: graph_mode=naive, expander returns expansionApplied=True; expansion_used==True."""
    pipeline = _make_pipeline(graph_expansion_applied=True)
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collection="col", graph_mode="naive")

    assert result.get("expansion_used") is True
    assert result.get("graph_expansion_applied") is True


@pytest.mark.asyncio
async def test_mcp_search_expansion_used_includes_graph_expansion_multi() -> None:
    """Multi-collection: graph_mode=naive, expander returns expansionApplied=True; expansion_used==True."""
    pipeline = _make_pipeline(graph_expansion_applied=True)
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collections=["a", "b"], graph_mode="naive")

    assert result.get("expansion_used") is True
    assert result.get("graph_expansion_applied") is True


@pytest.mark.asyncio
async def test_mcp_search_expansion_used_false_when_graph_not_expanded() -> None:
    """graph_mode=naive requested but no expansion occurred; expansion_used remains False."""
    pipeline = _make_pipeline(graph_expansion_applied=False)
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="hello", collection="col", graph_mode="naive")

    assert result.get("expansion_used") is False
    assert result.get("graph_expansion_applied") is False


# ---------------------------------------------------------------------------
# Integration test — graph_expansion_applied roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_roundtrip() -> None:
    """Real app with graph enabled; MCP search with graph_mode='naive'; graph_expansion_applied in response."""
    pipeline = _make_pipeline(graph_expansion_applied=True)
    app = _make_mcp_app(pipeline, graph_enabled=True)
    search_fn = app.tools["search"]  # type: ignore[union-attr]

    result = await search_fn(query="AuthService", collection="col", graph_mode="naive")

    assert "graph_expansion_applied" in result, f"Missing graph_expansion_applied in response: {result}"
    assert result["graph_expansion_applied"] is True
    assert result.get("expansion_used") is True


# ---------------------------------------------------------------------------
# Unit tests — graph_mode=None behaviour with graph disabled / search_with_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_graph_mode_none_when_disabled_succeeds() -> None:
    """graph_mode=None + graph.enabled=False → search proceeds normally, no error."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=False)
    search_fn = app.tools["search"]

    result = await search_fn(query="hello", collection="col", graph_mode=None)

    assert result.get("code") not in ("graph_disabled", "invalid_graph_mode"), (
        f"graph_mode=None should not trigger error when graph disabled, got: {result}"
    )
    pipeline.search.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_search_with_context_graph_mode_none_succeeds() -> None:
    """search_with_context with graph_mode=None → proceeds normally, no guard triggered."""
    pipeline = _make_pipeline()
    from archon_search.pipeline import SearchWithContextResult
    pipeline.search_with_context = AsyncMock(
        return_value=SearchWithContextResult(results=[], pipeline_result=pipeline.search.return_value)
    )
    app = _make_mcp_app(pipeline, graph_enabled=True)
    swc_fn = app.tools["search_with_context"]

    result = await swc_fn(query="hello", collection="col", graph_mode=None)

    assert result.get("code") != "graph_mode_not_supported", (
        f"graph_mode=None should not trigger the deferred guard, got: {result}"
    )

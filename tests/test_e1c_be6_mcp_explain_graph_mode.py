"""BE-6: Tests for graph_mode on MCP explain tool.

Covers:
- MCP explain tool accepts graph_mode parameter (None default)
- graph_mode="naive"/"local"/"global" forwarded to pipeline.explain on single-collection path
- graph_mode + collections together → error result (multi-collection not supported)
- graph_mode="invalid" → error result via _VALID_GRAPH_MODES validation
- graph disabled + graph_mode → code="graph_disabled" (not a pipeline call)
- GraphCommunitiesNotBuiltError from pipeline → code="graph_communities_not_built"
- graph_mode=None → result dict contains graph_mode_applied=null; all result items
  have graph_provenance=null (S12 partial — full provenance requires E1a)
- hyde=True + graph_mode → hyde_applied=False (graph_mode wins, HyDE suppressed early)
- graph_mode_applied surfaced in result dict (S12)

Scenarios: S12 (partial).
"""
from __future__ import annotations

import inspect
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Stub fastmcp so mcp.py can be imported without the real package interfering.
if "fastmcp" not in sys.modules:
    try:
        import fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp


# ---------------------------------------------------------------------------
# FastMCP stub (captures registered tools in a dict)
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


def _make_pipeline(*, graph_mode_applied: str | None = None) -> Any:
    """Build a mock pipeline with pipeline.explain returning a minimal result."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.pipeline import ExplainPipelineResult

    candidate = ScoredSearchCandidate(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello",
        source_path="/tmp/test.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.5,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=0.5,
            reranker_score=0.7,
        ),
        collection="test-col",
    )
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[candidate],
            near_misses=[],
            acl_filtered=False,
            graph_mode_applied=graph_mode_applied,
        )
    )
    return pipeline


def _make_mcp_app(pipeline: Any, *, graph_enabled: bool = True) -> _FakeApp:
    """Create an MCP app with optional graph_enabled flag."""
    from unittest.mock import patch
    from archon_search.config import GraphConfig, SearchConfig

    config = SearchConfig()
    config.graph = GraphConfig(enabled=graph_enabled)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        app = mcp_module.create_app(pipeline, "default", config=config)
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Unit tests — tool signature
# ---------------------------------------------------------------------------


def test_mcp_explain_tool_graph_mode_parameter_accepted() -> None:
    """MCP explain tool signature includes graph_mode parameter with None default."""
    from unittest.mock import patch

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        pipeline = _make_pipeline()
        app = mcp_module.create_app(pipeline, "default")
    explain_fn = app.tools["explain"]  # type: ignore[union-attr]
    sig = inspect.signature(explain_fn)
    assert "graph_mode" in sig.parameters, (
        "explain tool must accept graph_mode parameter"
    )
    param = sig.parameters["graph_mode"]
    assert param.default is None, "graph_mode default must be None"


# ---------------------------------------------------------------------------
# Unit tests — graph_mode forwarding (single-collection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_tool_graph_mode_naive_forwarded_single_collection() -> None:
    """graph_mode='naive' is forwarded to pipeline.explain on the single-collection path.

    Verifies both the pipeline call kwarg AND the result dict contains graph_mode_applied.
    """
    pipeline = _make_pipeline(graph_mode_applied="naive")
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode="naive")

    assert "error" not in result, f"Unexpected error: {result}"
    # Verify pipeline was called with correct graph_mode kwarg
    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args.kwargs
    assert call_kwargs.get("graph_mode") == "naive", (
        f"Expected graph_mode='naive' in pipeline.explain kwargs; got: {call_kwargs}"
    )
    # Verify result dict surfaces graph_mode_applied (S12)
    assert result.get("graph_mode_applied") == "naive", (
        f"Expected graph_mode_applied='naive' in result dict; got: {result.get('graph_mode_applied')}"
    )


@pytest.mark.asyncio
async def test_mcp_explain_tool_graph_mode_local_forwarded_single_collection() -> None:
    """graph_mode='local' is forwarded to pipeline.explain on the single-collection path."""
    pipeline = _make_pipeline(graph_mode_applied="local")
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode="local")

    assert "error" not in result, f"Unexpected error: {result}"
    pipeline.explain.assert_called_once()
    assert pipeline.explain.call_args.kwargs.get("graph_mode") == "local"
    assert result.get("graph_mode_applied") == "local"


@pytest.mark.asyncio
async def test_mcp_explain_tool_graph_mode_global_forwarded_single_collection() -> None:
    """graph_mode='global' is forwarded to pipeline.explain on the single-collection path."""
    pipeline = _make_pipeline(graph_mode_applied="global")
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode="global")

    assert "error" not in result, f"Unexpected error: {result}"
    pipeline.explain.assert_called_once()
    assert pipeline.explain.call_args.kwargs.get("graph_mode") == "global"
    assert result.get("graph_mode_applied") == "global"


@pytest.mark.asyncio
async def test_mcp_explain_tool_graph_mode_none_forwarded_as_none() -> None:
    """graph_mode=None (default) is forwarded to pipeline.explain as None."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    await explain_fn(query="hello", collection="test-col")

    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args.kwargs
    # graph_mode should either be absent or None (both mean no graph retrieval)
    assert call_kwargs.get("graph_mode") is None, (
        f"Expected graph_mode=None in pipeline.explain kwargs; got: {call_kwargs}"
    )


# ---------------------------------------------------------------------------
# Unit tests — graph_mode with collections rejection guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_tool_graph_mode_with_collections_rejected() -> None:
    """graph_mode='naive' + collections=['a','b'] → error with specific code."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collections=["a", "b"], graph_mode="naive")

    assert result.get("code") == "graph_mode_multi_collection_unsupported", (
        f"Expected code='graph_mode_multi_collection_unsupported'; got: {result}"
    )
    # pipeline.explain should NOT be called — rejection happens before pipeline call
    pipeline.explain.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — invalid graph_mode value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_tool_invalid_graph_mode_rejected() -> None:
    """graph_mode='invalid' → error result via _VALID_GRAPH_MODES validation, no pipeline call."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode="invalid")

    assert result.get("code") == "invalid_graph_mode", (
        f"Expected code='invalid_graph_mode'; got: {result}"
    )
    # pipeline.explain must not be called
    pipeline.explain.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — graph disabled guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_graph_not_enabled_returns_error() -> None:
    """MCP explain tool with graph_mode='naive' + graph disabled → code='graph_disabled'."""
    pipeline = _make_pipeline()
    app = _make_mcp_app(pipeline, graph_enabled=False)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode="naive")

    assert result.get("code") == "graph_disabled", (
        f"Expected code='graph_disabled'; got: {result}"
    )
    # pipeline.explain must not be called when graph is disabled
    pipeline.explain.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — GraphCommunitiesNotBuiltError handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_communities_not_built_returns_error() -> None:
    """MCP explain tool with graph_mode='local' + communities not built → code='graph_communities_not_built'."""
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    pipeline = _make_pipeline()
    pipeline.explain = AsyncMock(side_effect=GraphCommunitiesNotBuiltError("test-col"))
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode="local")

    assert result.get("code") == "graph_communities_not_built", (
        f"Expected code='graph_communities_not_built'; got: {result}"
    )


# ---------------------------------------------------------------------------
# Unit tests — HyDE suppression when graph_mode is set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_graph_mode_suppresses_hyde() -> None:
    """hyde=True + graph_mode='naive' → hyde_applied=False in result; pipeline.explain query_vector=None.

    graph_mode wins over HyDE — the HyDE LLM call is skipped entirely (early suppression),
    and query_vector is nulled before the pipeline call so routing-computed vectors do not
    leak into graph retrieval. (S15 equivalent for MCP)
    """
    pipeline = _make_pipeline(graph_mode_applied="naive")
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(
        query="hello", collection="test-col", graph_mode="naive", hyde=True
    )

    assert "error" not in result, f"Unexpected error: {result}"
    # HyDE must not have been applied — graph_mode takes precedence
    assert result.get("hyde_applied") is False, (
        f"Expected hyde_applied=False when graph_mode is set; got: {result.get('hyde_applied')}"
    )
    # pipeline.explain must have received query_vector=None (HyDE vector suppressed)
    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args.kwargs
    assert call_kwargs.get("query_vector") is None, (
        f"Expected query_vector=None forwarded to pipeline when graph_mode is set; got: {call_kwargs.get('query_vector')}"
    )


# ---------------------------------------------------------------------------
# Integration tests — graph_mode=None result dict structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_graph_mode_none_result_dict() -> None:
    """MCP explain call with graph_mode=None (default) → result dict has graph_mode_applied=null.

    Result items have graph_provenance key present and null (S12 partial).
    """
    pipeline = _make_pipeline(graph_mode_applied=None)
    app = _make_mcp_app(pipeline, graph_enabled=True)
    explain_fn = app.tools["explain"]

    result = await explain_fn(query="hello", collection="test-col", graph_mode=None)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result.get("graph_mode_applied") is None, (
        f"Expected graph_mode_applied=null; got: {result.get('graph_mode_applied')}"
    )
    for item in result.get("results", []):
        # Key must be present (not omitted) with explicit null value
        # (ExplainResponse uses exclude_none=False in model_dump)
        assert "graph_provenance" in item, (
            f"Expected graph_provenance key present in result item; got keys: {list(item.keys())}"
        )
        assert item["graph_provenance"] is None, (
            f"Expected graph_provenance=null on result item; got: {item['graph_provenance']}"
        )

"""Unit tests for BE-9: MCP search tool graph_mode parameter extension.

Tests:
- graph_mode='global' is accepted; pipeline.search called with graph_mode='global'
- graph_mode='local' is accepted; pipeline.search called with graph_mode='local'
- graph_mode='bad' returns error dict with code='invalid_graph_mode'
- GraphCommunitiesNotBuiltError from pipeline → error dict with code='graph_communities_not_built'
- search_with_context graph_mode='local' returns code='graph_mode_not_supported'
- search_with_context graph_mode='global' returns code='graph_mode_not_supported'
- Updated search_with_context error message lists all three modes

Scenarios: C1, S5
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# fastmcp stub — mirrors the approach in test_mcp.py (xdist_group isolation)
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.xdist_group("mcp_stub_be9")


class _StubFastMCP:
    def __init__(self, *args, **kwargs):
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


_MCP_MODULE = "archon_search.server.mcp"
_FASTMCP_MODULE = "fastmcp"

try:
    import fastmcp as _real_fastmcp_pkg  # type: ignore[import]
    _real_fastmcp_class = _real_fastmcp_pkg.FastMCP
    _real_fastmcp_context = getattr(_real_fastmcp_pkg, "Context", None)
except (ImportError, AttributeError):
    _real_fastmcp_class = None
    _real_fastmcp_context = None

if _FASTMCP_MODULE not in sys.modules:
    _fastmcp_mod = types.ModuleType(_FASTMCP_MODULE)
    _fastmcp_mod.FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    _fastmcp_mod.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules[_FASTMCP_MODULE] = _fastmcp_mod
else:
    sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]


@pytest.fixture(autouse=True, scope="module")
def _stub_fastmcp_for_module():
    """Reinstall _StubFastMCP for this module's tests, then restore."""
    sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)
    yield
    if _real_fastmcp_class is not None:
        sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
        if _real_fastmcp_context is not None:
            sys.modules[_FASTMCP_MODULE].Context = _real_fastmcp_context  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_mock_with_result(graph_expansion_applied: bool = False):
    """Return a minimal pipeline mock that returns a SearchPipelineResult."""
    from archon_search.pipeline import SearchPipelineResult

    result_obj = SearchPipelineResult(
        results=[],
        acl_filtered=False,
        excluded_collections=[],
        graph_expansion_applied=graph_expansion_applied,
    )
    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=result_obj)
    pipeline.search_many = AsyncMock(return_value=result_obj)
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock(
        namespace="default",
        name="col1",
        active_embedding_model=None,
    ))
    return pipeline


def _make_config_with_graph_enabled():
    """Return a SearchConfig with graph.enabled=True."""
    from archon_search.config import GraphConfig, SearchConfig

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)
    return config


def _get_tool_fn(tool_name: str, pipeline, config=None):
    """Build a stub-backed MCP app and return the named tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1", config=config)
    return app._tools[tool_name]


# ---------------------------------------------------------------------------
# test_mcp_search_global_mode_calls_pipeline
# ---------------------------------------------------------------------------


def test_mcp_search_global_mode_calls_pipeline() -> None:
    """MCP search(graph_mode='global') is accepted and pipeline.search is called
    with graph_mode='global'.
    """
    pipeline = _make_pipeline_mock_with_result(graph_expansion_applied=True)
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="global"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") != "invalid_graph_mode", (
        f"graph_mode='global' must not produce invalid_graph_mode error; got: {result!r}"
    )
    # pipeline.search must have been called with graph_mode="global"
    pipeline.search.assert_called_once()
    assert pipeline.search.call_args.kwargs["graph_mode"] == "global", (
        f"pipeline.search must be called with graph_mode='global'; call args: {pipeline.search.call_args!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_local_mode_calls_pipeline
# ---------------------------------------------------------------------------


def test_mcp_search_local_mode_calls_pipeline() -> None:
    """MCP search(graph_mode='local') is accepted and pipeline.search is called
    with graph_mode='local'.
    """
    pipeline = _make_pipeline_mock_with_result(graph_expansion_applied=True)
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="local"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") != "invalid_graph_mode", (
        f"graph_mode='local' must not produce invalid_graph_mode error; got: {result!r}"
    )
    pipeline.search.assert_called_once()
    assert pipeline.search.call_args.kwargs["graph_mode"] == "local", (
        f"pipeline.search must be called with graph_mode='local'; call args: {pipeline.search.call_args!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_invalid_graph_mode_returns_error
# ---------------------------------------------------------------------------


def test_mcp_search_invalid_graph_mode_returns_error() -> None:
    """MCP search(graph_mode='bad') returns an error dict with code='invalid_graph_mode'."""
    pipeline = _make_pipeline_mock_with_result()
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="bad"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert "error" in result, f"Expected 'error' key in result: {result!r}"
    assert result.get("code") == "invalid_graph_mode", (
        f"Expected code='invalid_graph_mode' for invalid graph_mode; got: {result!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_communities_not_built_returns_error
# ---------------------------------------------------------------------------


def test_mcp_search_communities_not_built_returns_error() -> None:
    """MCP search(graph_mode='global') when pipeline raises GraphCommunitiesNotBuiltError
    returns error dict with code='graph_communities_not_built'.
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    pipeline = MagicMock()
    pipeline.search = AsyncMock(side_effect=GraphCommunitiesNotBuiltError("col1"))
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock(
        namespace="default",
        name="col1",
        active_embedding_model=None,
    ))

    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="global"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert "error" in result, f"Expected 'error' key in result: {result!r}"
    assert result.get("code") == "graph_communities_not_built", (
        f"Expected code='graph_communities_not_built'; got: {result!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_with_context_local_global_deferred
# ---------------------------------------------------------------------------


def test_mcp_search_with_context_local_mode_returns_not_supported() -> None:
    """MCP search_with_context(graph_mode='local') returns code='graph_mode_not_supported'."""
    pipeline = _make_pipeline_mock_with_result()
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search_with_context", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="local"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert "error" in result, f"Expected 'error' key in result: {result!r}"
    assert result.get("code") == "graph_mode_not_supported", (
        f"Expected code='graph_mode_not_supported' for local; got: {result!r}"
    )


def test_mcp_search_with_context_global_mode_returns_not_supported() -> None:
    """MCP search_with_context(graph_mode='global') returns code='graph_mode_not_supported'."""
    pipeline = _make_pipeline_mock_with_result()
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search_with_context", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="global"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert "error" in result, f"Expected 'error' key in result: {result!r}"
    assert result.get("code") == "graph_mode_not_supported", (
        f"Expected code='graph_mode_not_supported' for global; got: {result!r}"
    )


def test_mcp_search_with_context_error_message_lists_all_modes() -> None:
    """The search_with_context error message lists naive, local, and global modes."""
    pipeline = _make_pipeline_mock_with_result()
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search_with_context", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test", collection="col1", graph_mode="local"))

    assert isinstance(result, dict), f"Expected dict: {result!r}"
    error_msg = result.get("error", "")
    # Error message must mention all three modes
    assert "naive" in error_msg, f"Error message must mention 'naive'; got: {error_msg!r}"
    assert "local" in error_msg, f"Error message must mention 'local'; got: {error_msg!r}"
    assert "global" in error_msg, f"Error message must mention 'global'; got: {error_msg!r}"


# ---------------------------------------------------------------------------
# test_mcp_search_naive_mode_still_accepted
# ---------------------------------------------------------------------------


def test_mcp_search_naive_mode_still_accepted() -> None:
    """MCP search(graph_mode='naive') is still a valid mode (regression guard)."""
    pipeline = _make_pipeline_mock_with_result()
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="naive"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") != "invalid_graph_mode", (
        f"graph_mode='naive' must not produce invalid_graph_mode error; got: {result!r}"
    )


def test_mcp_search_global_mode_graph_disabled_returns_error() -> None:
    """MCP search(graph_mode='global') when graph.enabled=False returns code='graph_disabled'."""
    from archon_search.config import GraphConfig, SearchConfig

    pipeline = _make_pipeline_mock_with_result()
    config = SearchConfig()
    config.graph = GraphConfig(enabled=False)
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="global"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") == "graph_disabled", (
        f"Expected code='graph_disabled' when graph disabled; got: {result!r}"
    )


def test_mcp_search_local_mode_graph_disabled_returns_error() -> None:
    """MCP search(graph_mode='local') when graph.enabled=False returns code='graph_disabled'."""
    from archon_search.config import GraphConfig, SearchConfig

    pipeline = _make_pipeline_mock_with_result()
    config = SearchConfig()
    config.graph = GraphConfig(enabled=False)
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test query", collection="col1", graph_mode="local"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") == "graph_disabled", (
        f"Expected code='graph_disabled' when graph disabled; got: {result!r}"
    )


def test_mcp_search_many_global_mode_calls_pipeline() -> None:
    """MCP search(collections=[...], graph_mode='global') calls pipeline.search_many
    with graph_mode='global' (multi-collection path).
    """
    pipeline = _make_pipeline_mock_with_result(graph_expansion_applied=True)
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(
        query="test query",
        collections=["col1", "col2"],
        graph_mode="global",
    ))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") != "invalid_graph_mode", (
        f"graph_mode='global' must not produce invalid_graph_mode error; got: {result!r}"
    )
    pipeline.search_many.assert_called_once()
    assert pipeline.search_many.call_args.kwargs["graph_mode"] == "global", (
        f"pipeline.search_many must be called with graph_mode='global'; "
        f"call args: {pipeline.search_many.call_args!r}"
    )


def test_mcp_search_many_local_mode_calls_pipeline() -> None:
    """MCP search(collections=[...], graph_mode='local') calls pipeline.search_many
    with graph_mode='local' (multi-collection path).
    """
    pipeline = _make_pipeline_mock_with_result(graph_expansion_applied=True)
    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(
        query="test query",
        collections=["col1", "col2"],
        graph_mode="local",
    ))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert result.get("code") != "invalid_graph_mode", (
        f"graph_mode='local' must not produce invalid_graph_mode error; got: {result!r}"
    )
    pipeline.search_many.assert_called_once()
    assert pipeline.search_many.call_args.kwargs["graph_mode"] == "local", (
        f"pipeline.search_many must be called with graph_mode='local'; "
        f"call args: {pipeline.search_many.call_args!r}"
    )


def test_mcp_search_many_communities_not_built_returns_error() -> None:
    """MCP search(collections=[...], graph_mode='global') when pipeline raises
    GraphCommunitiesNotBuiltError returns error dict with code='graph_communities_not_built'.

    Tests the multi-collection (search_many) code path in mcp.py.
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    pipeline = MagicMock()
    pipeline.search_many = AsyncMock(side_effect=GraphCommunitiesNotBuiltError("col1"))
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock(
        namespace="default",
        name="col1",
        active_embedding_model=None,
    ))

    config = _make_config_with_graph_enabled()
    tool_fn = _get_tool_fn("search", pipeline, config=config)

    result = asyncio.run(tool_fn(
        query="test query",
        collections=["col1", "col2"],
        graph_mode="global",
    ))

    assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
    assert "error" in result, f"Expected 'error' key in result: {result!r}"
    assert result.get("code") == "graph_communities_not_built", (
        f"Expected code='graph_communities_not_built' from search_many path; got: {result!r}"
    )

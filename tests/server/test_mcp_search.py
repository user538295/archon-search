"""Tests for MCP search tool filter support (A2)."""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import SearchResult

# FastMCP stub (same pattern as test_mcp_search_with_context.py)
if "fastmcp" not in sys.modules:
    try:
        import mcp.server.fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp


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


def _make_result(n: int = 1, metadata: dict | None = None) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + f"-{n:06d}",
        text=f"result {n}",
        score=0.9,
        source_path=f"/tmp/doc{n}.md",
        metadata=metadata or {},
    )


def _build_mcp_app(pipeline: MagicMock) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", _FakeFastMCP):
        from archon_search.server.mcp import create_app
        app = create_app(pipeline, "default-col", writer=None)
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# test_mcp_search_forwards_filters_to_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_forwards_filters_to_pipeline() -> None:
    """MCP search tool forwards file_type filter to pipeline.search()."""
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[_make_result(1)], acl_filtered=False)
    )
    app = _build_mcp_app(pipeline)

    await app.tools["search"](query="hello", collection="col", file_type="md")

    pipeline.search.assert_called_once()
    call_kwargs = pipeline.search.call_args
    filters_arg = call_kwargs.kwargs.get("filters")
    assert filters_arg is not None
    assert filters_arg.file_type == "md"


@pytest.mark.asyncio
async def test_mcp_search_invalid_filter_surfaces_validator_error() -> None:
    """MCP search with invalid file_type (empty string after strip) returns error dict."""
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )
    app = _build_mcp_app(pipeline)

    # file_type="." → stripped to "" → ValidationError
    result = await app.tools["search"](query="hello", collection="col", file_type=".")

    assert isinstance(result, dict)
    assert result.get("code") == "validation_error"
    pipeline.search.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_suppresses_metadata_when_include_metadata_false() -> None:
    """MCP search with include_metadata=False returns empty metadata dicts."""
    from archon_search.pipeline import SearchPipelineResult

    results = [_make_result(1, metadata={"key": "secret"})]
    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=results, acl_filtered=False)
    )
    app = _build_mcp_app(pipeline)

    result = await app.tools["search"](
        query="hello", collection="col", include_metadata=False
    )

    assert isinstance(result, dict)
    for r in result["results"]:
        assert r["metadata"] == {}


@pytest.mark.asyncio
async def test_mcp_search_includes_metadata_when_include_metadata_true() -> None:
    """MCP search with include_metadata=True returns full metadata dicts."""
    from archon_search.pipeline import SearchPipelineResult

    results = [_make_result(1, metadata={"key": "visible"})]
    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=results, acl_filtered=False)
    )
    app = _build_mcp_app(pipeline)

    result = await app.tools["search"](
        query="hello", collection="col", include_metadata=True
    )

    assert isinstance(result, dict)
    for r in result["results"]:
        assert r["metadata"] == {"key": "visible"}


@pytest.mark.asyncio
async def test_mcp_search_no_filter_kwargs_passes_none_to_pipeline() -> None:
    """When no filter kwargs supplied, pipeline.search() gets filters=None."""
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )
    app = _build_mcp_app(pipeline)

    await app.tools["search"](query="hello")

    pipeline.search.assert_called_once()
    call_kwargs = pipeline.search.call_args
    filters_arg = call_kwargs.kwargs.get("filters")
    assert filters_arg is None

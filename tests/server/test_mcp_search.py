"""Tests for MCP ``search`` tool metadata suppression (Task 1.3).

Verifies:
- metadata stripped from results when include_metadata=False (default)
- metadata present when include_metadata=True
- language field appears in MCP search output schema
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Resolve fastmcp the same way test_mcp_search_with_context.py does.
if "fastmcp" not in sys.modules:
    try:
        import mcp.server.fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp

from archon_search._types import SearchResult
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


def _make_result(metadata: dict[str, str] | None = None, language: str | None = None) -> SearchResult:
    return SearchResult(
        doc_id="d" * 64,
        chunk_id="d" * 64 + "-000001",
        text="some matched text",
        score=0.85,
        source_path="/tmp/doc.md",
        file_type="md",
        language=language,
        metadata=metadata or {},
        ingested_by="cli",
    )


async def _call_mcp_search(pipeline_result: SearchPipelineResult, include_metadata: bool | None = None) -> dict[str, Any]:
    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=pipeline_result)

    with __import__("unittest.mock", fromlist=["patch"]).patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        if include_metadata is None:
            return await fn(query="hello", collection=None)
        return await fn(query="hello", collection=None, include_metadata=include_metadata)


@pytest.mark.asyncio
async def test_mcp_search_strips_metadata_when_include_metadata_false() -> None:
    """MCP search tool must strip metadata from results when include_metadata is False (default)."""
    result = _make_result(metadata={"k": "v"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    payload = await _call_mcp_search(pipeline_result)

    assert "results" in payload
    assert len(payload["results"]) == 1
    # empty dict not key-absent, consistent with REST surface
    assert payload["results"][0]["metadata"] == {}


@pytest.mark.asyncio
async def test_mcp_search_includes_metadata_when_include_metadata_true() -> None:
    """MCP search tool must include metadata when include_metadata=True."""
    result = _make_result(metadata={"k": "v"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    payload = await _call_mcp_search(pipeline_result, include_metadata=True)

    assert "results" in payload
    assert len(payload["results"]) == 1
    assert payload["results"][0]["metadata"] == {"k": "v"}


@pytest.mark.asyncio
async def test_mcp_search_tool_schema_advertises_language_field() -> None:
    """MCP search results must include language field."""
    result = _make_result(language="en")
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    # include_metadata=True so we get the full result dict
    payload = await _call_mcp_search(pipeline_result, include_metadata=True)

    assert "results" in payload
    assert len(payload["results"]) == 1
    assert "language" in payload["results"][0]
    assert payload["results"][0]["language"] == "en"

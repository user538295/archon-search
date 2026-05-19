"""Tests for structured MCP error responses — FEAT-045 Task 4.1."""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub fastmcp so mcp.py can be imported without the real package.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search._types import SearchResult
from archon_search.pipeline import SearchPipelineResult


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


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_app(pipeline: MagicMock) -> _FakeApp:
    from unittest.mock import patch
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Tests — Task 4.1: structured MCP error responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_error_returns_structured_error() -> None:
    """When pipeline.search() raises, tool returns dict with error and code keys."""
    pipeline = MagicMock()
    pipeline.search = AsyncMock(side_effect=RuntimeError("boom"))
    app = _make_app(pipeline)
    result = await app.tools["search"](query="q", collection=None)
    assert isinstance(result, dict)
    assert set(result.keys()) == {"error", "code"}
    assert result["code"] == "internal_error"
    assert "boom" in result["error"]


@pytest.mark.asyncio
async def test_search_with_context_error_returns_structured_error() -> None:
    """When pipeline.search_with_context() raises, tool returns structured error dict."""
    pipeline = MagicMock()
    pipeline.search_with_context = AsyncMock(side_effect=RuntimeError("swc boom"))
    app = _make_app(pipeline)
    result = await app.tools["search_with_context"](query="q", collection=None)
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_ingest_file_error_has_internal_error_code() -> None:
    """When pipeline.ingest_file() raises, tool returns code='internal_error'."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=OSError("no file"))
    app = _make_app(pipeline)
    result = await app.tools["ingest_file"](path="/no/such/file.txt", collection=None)
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"
    assert "error" in result


@pytest.mark.asyncio
async def test_ingest_directory_error_returns_structured_error() -> None:
    """When pipeline.ingest_directory() raises, tool returns structured error dict."""
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=RuntimeError("dir error"))
    app = _make_app(pipeline)
    result = await app.tools["ingest_directory"](path="/no/dir", collection=None)
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_list_collections_error_returns_structured_error() -> None:
    """When pipeline.get_all_collections_meta() raises, tool returns structured error dict."""
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("store error"))
    app = _make_app(pipeline)
    result = await app.tools["list_collections"]()
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_get_collections_meta_error_returns_structured_error() -> None:
    """When pipeline.get_all_collections_meta() raises, tool returns structured error dict."""
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("meta error"))
    app = _make_app(pipeline)
    result = await app.tools["get_collections_meta"]()
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_get_collection_meta_not_found_has_not_found_code() -> None:
    """When collection is missing, tool returns code='not_found' with collection name in error."""
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    app = _make_app(pipeline)
    result = await app.tools["get_collection_meta"](name="nonexistent")
    assert isinstance(result, dict)
    assert set(result.keys()) == {"error", "code"}
    assert result["code"] == "not_found"
    assert "nonexistent" in result["error"]


@pytest.mark.asyncio
async def test_get_collection_meta_exception_has_internal_error_code() -> None:
    """When pipeline.get_collection_meta() raises, tool returns code='internal_error'."""
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError("db error"))
    app = _make_app(pipeline)
    result = await app.tools["get_collection_meta"](name="col")
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_list_documents_error_returns_structured_error() -> None:
    """When pipeline.list_documents() raises, tool returns structured error dict."""
    pipeline = MagicMock()
    pipeline.list_documents = AsyncMock(side_effect=RuntimeError("list error"))
    app = _make_app(pipeline)
    result = await app.tools["list_documents"](collection=None)
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_delete_document_error_returns_structured_error() -> None:
    """When pipeline.delete_document() raises, tool returns structured error dict."""
    pipeline = MagicMock()
    pipeline.delete_document = AsyncMock(side_effect=RuntimeError("delete error"))
    app = _make_app(pipeline)
    result = await app.tools["delete_document"](doc_id="doc1", collection=None)
    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"

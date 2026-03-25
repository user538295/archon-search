"""tests/rag/test_server.py — unit and integration tests for archon.rag.server."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon.rag._types import CollectionInfo, DocumentInfo, IngestResult, SearchResult
from archon.rag.pipeline import RagPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline(
    search_result: list[SearchResult] | None = None,
    swc_result: list[dict[str, Any]] | None = None,
    ingest_file_result: IngestResult | None = None,
    ingest_dir_result: list[IngestResult] | None = None,
    collections: list[CollectionInfo] | None = None,
    documents: list[DocumentInfo] | None = None,
    delete_count: int = 1,
) -> MagicMock:
    pipeline = MagicMock(spec=RagPipeline)
    pipeline.search = AsyncMock(return_value=search_result or [])
    pipeline.search_with_context = AsyncMock(return_value=swc_result or [])
    pipeline.ingest_file = AsyncMock(
        return_value=ingest_file_result or IngestResult("abc" * 21 + "ab", 3, "ok")
    )
    pipeline.ingest_directory = AsyncMock(
        return_value=ingest_dir_result or []
    )
    pipeline.list_collections = AsyncMock(return_value=collections or [])
    pipeline.list_documents = AsyncMock(return_value=documents or [])
    pipeline.delete_document = AsyncMock(return_value=delete_count)
    return pipeline


def _make_app(pipeline: MagicMock, default_collection: str = "docs") -> Any:
    from archon.rag.server import create_app
    return create_app(pipeline, default_collection)


def _list_data(result: Any) -> list[dict[str, Any]]:
    """Extract list result from a CallToolResult (list-returning tools)."""
    return result.structured_content["result"]


def _dict_data(result: Any) -> dict[str, Any]:
    """Extract dict result from a CallToolResult (dict-returning tools)."""
    return result.data


# ---------------------------------------------------------------------------
# Unit tests — create_app / tool registration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_app_returns_fastmcp_instance() -> None:
    """create_app returns a FastMCP with 7 registered tools."""
    from fastmcp import Client, FastMCP

    pipeline = _make_pipeline()
    app = _make_app(pipeline)

    assert isinstance(app, FastMCP)
    async with Client(app) as c:
        tools = await c.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {
        "search",
        "search_with_context",
        "ingest_file",
        "ingest_directory",
        "list_collections",
        "list_documents",
        "delete_document",
    }


@pytest.mark.asyncio
async def test_search_tool_delegates_to_pipeline() -> None:
    """search tool calls pipeline.search with correct args."""
    from fastmcp import Client

    sr = SearchResult("doc1", "doc1-000000", "hello world", 0.9, "/tmp/file.md")
    pipeline = _make_pipeline(search_result=[sr])
    app = _make_app(pipeline, default_collection="docs")

    async with Client(app) as c:
        result = await c.call_tool("search", {"query": "hello"})

    pipeline.search.assert_awaited_once_with("hello", "docs")
    data = _list_data(result)
    assert isinstance(data, list)
    assert data[0]["doc_id"] == "doc1"


@pytest.mark.asyncio
async def test_search_tool_uses_default_collection_when_none() -> None:
    """search with collection=None uses default_collection."""
    from fastmcp import Client

    pipeline = _make_pipeline()
    app = _make_app(pipeline, default_collection="my-collection")

    async with Client(app) as c:
        await c.call_tool("search", {"query": "q"})

    pipeline.search.assert_awaited_once_with("q", "my-collection")


@pytest.mark.asyncio
async def test_search_tool_uses_explicit_collection() -> None:
    """search with explicit collection overrides default."""
    from fastmcp import Client

    pipeline = _make_pipeline()
    app = _make_app(pipeline, default_collection="default")

    async with Client(app) as c:
        await c.call_tool("search", {"query": "q", "collection": "custom"})

    pipeline.search.assert_awaited_once_with("q", "custom")


@pytest.mark.asyncio
async def test_search_with_context_tool() -> None:
    """search_with_context tool delegates to pipeline with correct args."""
    from fastmcp import Client

    sr = SearchResult("doc1", "doc1-000001", "text", 0.8, "/tmp/file.md")
    swc = [{"result": sr, "context_before": [], "context_after": []}]
    pipeline = _make_pipeline(swc_result=swc)
    app = _make_app(pipeline, default_collection="docs")

    async with Client(app) as c:
        result = await c.call_tool(
            "search_with_context", {"query": "q", "context_window": 2}
        )

    pipeline.search_with_context.assert_awaited_once_with("q", "docs", 2)
    data = _list_data(result)
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_ingest_file_tool_returns_result_dict() -> None:
    """ingest_file tool serialises IngestResult to dict."""
    from fastmcp import Client

    ir = IngestResult("a" * 64, 5, "ok")
    pipeline = _make_pipeline(ingest_file_result=ir)
    app = _make_app(pipeline, default_collection="docs")

    async with Client(app) as c:
        result = await c.call_tool("ingest_file", {"path": "/tmp/doc.md"})

    pipeline.ingest_file.assert_awaited_once()
    call_args = pipeline.ingest_file.call_args
    assert call_args[0][0] == Path("/tmp/doc.md")
    assert call_args[0][1] == "docs"
    data = _dict_data(result)
    assert data["status"] == "ok"
    assert data["chunks_created"] == 5


@pytest.mark.asyncio
async def test_ingest_directory_tool() -> None:
    """ingest_directory tool returns list of dicts."""
    from fastmcp import Client

    irs = [IngestResult("a" * 64, 3, "ok"), IngestResult("b" * 64, 2, "ok")]
    pipeline = _make_pipeline(ingest_dir_result=irs)
    app = _make_app(pipeline, default_collection="docs")

    async with Client(app) as c:
        result = await c.call_tool("ingest_directory", {"path": "/tmp/docs"})

    pipeline.ingest_directory.assert_awaited_once()
    data = _list_data(result)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_ingest_directory_tool_wires_progress_cb() -> None:
    """ingest_directory tool passes a non-None progress_cb to pipeline."""
    from fastmcp import Client

    pipeline = _make_pipeline()
    app = _make_app(pipeline)

    async with Client(app) as c:
        await c.call_tool("ingest_directory", {"path": "/tmp"})

    call_kwargs = pipeline.ingest_directory.call_args[1]
    assert call_kwargs.get("progress_cb") is not None


@pytest.mark.asyncio
async def test_list_collections_tool() -> None:
    """list_collections tool serialises CollectionInfo list."""
    from fastmcp import Client

    cols = [CollectionInfo("col1", 2, 10), CollectionInfo("col2", 1, 5)]
    pipeline = _make_pipeline(collections=cols)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("list_collections", {})

    data = _list_data(result)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["name"] == "col1"
    assert data[0]["doc_count"] == 2


@pytest.mark.asyncio
async def test_list_documents_tool() -> None:
    """list_documents tool serialises DocumentInfo list."""
    from fastmcp import Client

    docs = [DocumentInfo("a" * 64, "/tmp/file.md", 3, "2026-01-01T00:00:00")]
    pipeline = _make_pipeline(documents=docs)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool(
            "list_documents", {"collection": "docs", "limit": 50}
        )

    pipeline.list_documents.assert_awaited_once_with("docs", 50)
    data = _list_data(result)
    assert data[0]["doc_id"] == "a" * 64


@pytest.mark.asyncio
async def test_delete_document_tool() -> None:
    """delete_document tool returns deleted count."""
    from fastmcp import Client

    pipeline = _make_pipeline(delete_count=3)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool(
            "delete_document", {"doc_id": "a" * 64, "collection": "docs"}
        )

    pipeline.delete_document.assert_awaited_once_with("a" * 64, "docs")
    data = _dict_data(result)
    assert data["deleted"] == 3


@pytest.mark.asyncio
async def test_tool_exception_returns_error_dict() -> None:
    """Pipeline exception → error entry in result, no transport-level exception."""
    from fastmcp import Client

    pipeline = _make_pipeline()
    pipeline.search.side_effect = RuntimeError("store not connected")
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("search", {"query": "q"})

    # search returns list[dict] — error is wrapped in a list
    data = _list_data(result)
    assert isinstance(data, list)
    assert len(data) == 1
    assert "error" in data[0]
    assert "store not connected" in data[0]["error"]


# ---------------------------------------------------------------------------
# Integration tests — full MCP protocol round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_search_tool_with_real_pipeline(
    connected_store: Any, col_name: str, tmp_path: Path
) -> None:
    """search tool works end-to-end with real store through MCP protocol."""
    from fastmcp import Client

    from archon.rag.chunker import DocumentChunker
    from archon.rag.embedder import Embedder
    from archon.rag.parser import DocumentParser
    from archon.rag.reranker import Reranker

    doc = tmp_path / "hello.md"
    doc.write_text("The quick brown fox jumps over the lazy dog.")

    class _FakeEmbed:
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    class _FakeRerank:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    pipeline = RagPipeline(
        store=connected_store,
        embedder=Embedder(_FakeEmbed()),
        reranker=Reranker(_FakeRerank()),
        chunker=DocumentChunker(),
        parser=DocumentParser(),
        history_collection=col_name,
        top_k_retrieve=5,
        top_k_return=3,
    )
    await pipeline.ingest_file(doc, col_name)

    app = _make_app(pipeline, default_collection=col_name)
    async with Client(app) as c:
        result = await c.call_tool("search", {"query": "fox"})

    data = _list_data(result)
    assert isinstance(data, list)
    assert len(data) > 0


@pytest.mark.asyncio
async def test_server_error_serialization_through_mcp_transport(
    connected_store: Any, col_name: str
) -> None:
    """search before store has data returns result (may be empty or error) — not transport crash."""
    from fastmcp import Client

    from archon.rag.chunker import DocumentChunker
    from archon.rag.embedder import Embedder
    from archon.rag.parser import DocumentParser
    from archon.rag.reranker import Reranker

    class _FailEmbed:
        def encode(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("boom")

    pipeline = RagPipeline(
        store=connected_store,
        embedder=Embedder(_FailEmbed()),
        reranker=Reranker(MagicMock()),
        chunker=DocumentChunker(),
        parser=DocumentParser(),
        history_collection=col_name,
        top_k_retrieve=5,
        top_k_return=3,
    )
    app = _make_app(pipeline, default_collection=col_name)
    async with Client(app) as c:
        result = await c.call_tool("search", {"query": "anything"})

    data = _list_data(result)
    assert isinstance(data, list)
    assert len(data) == 1
    assert "error" in data[0]


# ---------------------------------------------------------------------------
# Unit test — main() wires components
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_main_wires_all_components() -> None:
    """main() calls store.connect() and app.run_http_async with correct host/port."""
    from archon.config.loader import RagConfig
    from archon.rag.server import main

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.rag = RagConfig(host="127.0.0.1", port=9999)

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon.rag.server.create_pipeline", return_value=mock_pipeline),
        patch("archon.rag.server.create_app", return_value=mock_app),
    ):
        await main()

    mock_store.connect.assert_awaited_once()
    mock_app.run_http_async.assert_awaited_once()
    call_kwargs = mock_app.run_http_async.call_args[1]
    assert call_kwargs["host"] == "127.0.0.1"
    assert call_kwargs["port"] == 9999
    mock_store.disconnect.assert_awaited_once()  # finally block must always disconnect

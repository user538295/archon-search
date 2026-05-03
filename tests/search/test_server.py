"""tests/rag/test_server.py — unit and integration tests for archon.rag.server."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import CollectionInfo, DocumentInfo, IngestResult, SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.pipeline import SearchPipeline


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
    all_collections_meta: list[CollectionMeta] | None = None,
    single_collection_meta: CollectionMeta | None = None,
) -> MagicMock:
    pipeline = MagicMock(spec=SearchPipeline)
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
    pipeline.get_all_collections_meta = AsyncMock(return_value=all_collections_meta or [])
    pipeline.get_collection_meta = AsyncMock(return_value=single_collection_meta)
    return pipeline


def _make_app(pipeline: MagicMock, default_collection: str = "docs") -> Any:
    from archon_search.server.mcp import create_app
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
    """create_app returns a FastMCP with 9 registered tools."""
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
        "get_collections_meta",
        "get_collection_meta",
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
    """list_collections tool returns CollectionMeta list (centroid omitted)."""
    from fastmcp import Client

    metas = [
        CollectionMeta(name="col1", doc_count=2, chunk_count=10,
                       centroid=[0.1, 0.2], description="A collection"),
        CollectionMeta(name="col2", doc_count=1, chunk_count=5),
    ]
    pipeline = _make_pipeline(all_collections_meta=metas)
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

    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.reranker import Reranker

    doc = tmp_path / "hello.md"
    doc.write_text("The quick brown fox jumps over the lazy dog.")

    class _FakeEmbed:
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    class _FakeRerank:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=Embedder(_FakeEmbed()),
        reranker=Reranker(_FakeRerank()),
        chunker=DocumentChunker(),
        parser=DocumentParser(),

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

    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.reranker import Reranker

    class _FailEmbed:
        def encode(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("boom")

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=Embedder(_FailEmbed()),
        reranker=Reranker(MagicMock()),
        chunker=DocumentChunker(),
        parser=DocumentParser(),

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
    from archon_search.server.mcp import main
    from archon_search.sync import SyncResult

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = 5
    mock_cfg.search.db_path = "~/.archon/search"
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = False
    mock_cfg.search.watch = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore"),
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        await main()

    mock_store.connect.assert_awaited_once()
    mock_app.run_http_async.assert_awaited_once()
    call_kwargs = mock_app.run_http_async.call_args[1]
    assert call_kwargs["host"] == "127.0.0.1"
    assert call_kwargs["port"] == 9999
    mock_store.disconnect.assert_awaited_once()  # finally block must always disconnect


# ---------------------------------------------------------------------------
# Task 3.1 — startup sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_runs_sync_on_startup() -> None:
    """main() calls SearchCollectionSync.sync() before app.run_http_async."""
    import asyncio
    from archon_search.server.mcp import main
    from archon_search.sync import SyncResult

    call_order: list[str] = []

    mock_store = MagicMock()
    mock_store.connect = AsyncMock(side_effect=lambda: call_order.append("connect"))
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    mock_sync_result = SyncResult(added=["col1"], removed=[], unchanged=[], errors=[], skipped=[])
    mock_sync = AsyncMock(side_effect=lambda cols: (call_order.append("sync"), mock_sync_result)[1])

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock(side_effect=lambda **kw: call_order.append("http"))

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = 5
    mock_cfg.search.db_path = "~/.archon/search"
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = False
    mock_cfg.search.watch = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore"),
    ):
        MockSync.return_value.sync = mock_sync
        await main()

    assert call_order.index("sync") < call_order.index("http"), (
        "sync must be called before HTTP server starts"
    )
    mock_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_logs_warning_on_sync_errors(caplog: pytest.LogCaptureFixture) -> None:
    """main() logs WARNING when sync_result.errors is non-empty."""
    import logging
    from archon_search.server.mcp import main
    from archon_search.sync import SyncResult

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    error_result = SyncResult(
        added=[], removed=[], unchanged=[], errors=["path does not exist: /bad/path"], skipped=[]
    )
    mock_sync = AsyncMock(return_value=error_result)

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = 5
    mock_cfg.search.db_path = "~/.archon/search"
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = False
    mock_cfg.search.watch = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore"),
        caplog.at_level(logging.WARNING),
    ):
        MockSync.return_value.sync = mock_sync
        await main()

    assert any("error" in r.message.lower() or "sync" in r.message.lower()
               for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_server_starts_even_if_sync_times_out() -> None:
    """main() starts the HTTP server even when startup sync times out."""
    import asyncio
    from archon_search.server.mcp import main

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    async def _slow_sync(cols: list[str]) -> None:
        await asyncio.sleep(10)  # much longer than timeout

    mock_app = MagicMock()
    http_started = asyncio.Event()
    mock_app.run_http_async = AsyncMock(side_effect=lambda **kw: http_started.set())

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = 1
    mock_cfg.search.db_path = "~/.archon/search"
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = False
    mock_cfg.search.watch = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
    ):
        MockSync.return_value.sync = _slow_sync
        await main()

    mock_app.run_http_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 1.4 — get_collections_meta / get_collection_meta / list_collections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collections_endpoint_returns_list() -> None:
    """list_collections returns CollectionMeta list from get_all_collections_meta."""
    from fastmcp import Client

    metas = [
        CollectionMeta(name="col1", doc_count=3, chunk_count=12),
        CollectionMeta(name="col2", doc_count=1, chunk_count=4),
    ]
    pipeline = _make_pipeline(all_collections_meta=metas)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("list_collections", {})

    pipeline.get_all_collections_meta.assert_awaited_once()
    data = _list_data(result)
    assert len(data) == 2
    assert data[0]["name"] == "col1"
    assert data[1]["name"] == "col2"


@pytest.mark.asyncio
async def test_collections_endpoint_omits_centroid() -> None:
    """list_collections tool strips centroid from each CollectionMeta."""
    from fastmcp import Client

    metas = [
        CollectionMeta(name="col1", doc_count=2, chunk_count=8, centroid=[0.1, 0.2, 0.3]),
    ]
    pipeline = _make_pipeline(all_collections_meta=metas)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("list_collections", {})

    data = _list_data(result)
    assert len(data) == 1
    assert "centroid" not in data[0]


@pytest.mark.asyncio
async def test_collections_meta_bulk_endpoint_returns_centroids() -> None:
    """get_collections_meta returns full CollectionMeta including centroid."""
    from fastmcp import Client

    metas = [
        CollectionMeta(name="col1", doc_count=2, chunk_count=8, centroid=[0.1, 0.2]),
        CollectionMeta(name="col2", doc_count=1, chunk_count=4, centroid=None),
    ]
    pipeline = _make_pipeline(all_collections_meta=metas)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("get_collections_meta", {})

    pipeline.get_all_collections_meta.assert_awaited_once()
    data = _list_data(result)
    assert len(data) == 2
    assert data[0]["centroid"] == [0.1, 0.2]
    assert data[1]["centroid"] is None


@pytest.mark.asyncio
async def test_collection_meta_endpoint_includes_centroid() -> None:
    """get_collection_meta returns full CollectionMeta including centroid for a named collection."""
    from fastmcp import Client

    meta = CollectionMeta(name="col1", doc_count=5, chunk_count=20, centroid=[0.5, 0.6])
    pipeline = _make_pipeline(single_collection_meta=meta)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("get_collection_meta", {"name": "col1"})

    pipeline.get_collection_meta.assert_awaited_once_with("col1")
    data = _dict_data(result)
    assert data["name"] == "col1"
    assert data["centroid"] == [0.5, 0.6]
    assert data["doc_count"] == 5


@pytest.mark.asyncio
async def test_collection_meta_unknown_name_raises_error() -> None:
    """get_collection_meta returns an error dict when name is unknown."""
    from fastmcp import Client

    pipeline = _make_pipeline(single_collection_meta=None)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("get_collection_meta", {"name": "unknown"})

    data = _dict_data(result)
    assert "error" in data
    assert "unknown" in data["error"]


@pytest.mark.asyncio
async def test_collection_meta_serializes_datetime_fields() -> None:
    """datetime fields in CollectionMeta are serialized to ISO strings by MCP transport."""
    from datetime import UTC, datetime
    from fastmcp import Client

    ts = datetime(2026, 3, 27, 10, 0, 0, tzinfo=UTC)
    meta = CollectionMeta(name="col1", doc_count=1, chunk_count=4, last_indexed=ts)
    pipeline = _make_pipeline(single_collection_meta=meta)
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("get_collection_meta", {"name": "col1"})

    data = _dict_data(result)
    assert "last_indexed" in data
    # Must be a string (ISO format), not a datetime object
    assert isinstance(data["last_indexed"], str)
    assert "2026" in data["last_indexed"]


@pytest.mark.asyncio
async def test_get_collections_meta_exception_returns_error() -> None:
    """Pipeline exception in get_collections_meta → error entry in result."""
    from fastmcp import Client

    pipeline = _make_pipeline()
    pipeline.get_all_collections_meta.side_effect = RuntimeError("meta store broken")
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("get_collections_meta", {})

    data = _list_data(result)
    assert "error" in data[0]
    assert "meta store broken" in data[0]["error"]


@pytest.mark.asyncio
async def test_get_collection_meta_exception_returns_error() -> None:
    """Pipeline exception in get_collection_meta → error dict in result."""
    from fastmcp import Client

    pipeline = _make_pipeline()
    pipeline.get_collection_meta.side_effect = RuntimeError("store disconnected")
    app = _make_app(pipeline)

    async with Client(app) as c:
        result = await c.call_tool("get_collection_meta", {"name": "col1"})

    data = _dict_data(result)
    assert "error" in data
    assert "store disconnected" in data["error"]


# ---------------------------------------------------------------------------
# Task 1.1 — /health endpoint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 1.5 — state store wiring in main()
# ---------------------------------------------------------------------------


class TestServerStateStore:
    """Verify main() creates IndexingStateStore and passes it to SearchCollectionSync."""

    @pytest.mark.asyncio
    async def test_main_creates_state_store(self) -> None:
        """main() instantiates IndexingStateStore with cfg.rag.db_path."""
        from archon_search.server.mcp import main
        from archon_search.sync import SyncResult

        mock_store = MagicMock()
        mock_store.connect = AsyncMock()
        mock_store.disconnect = AsyncMock()

        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store

        mock_app = MagicMock()
        mock_app.run_http_async = AsyncMock()

        db_path = "/tmp/test-rag-db"
        mock_cfg = MagicMock()
        mock_cfg.search.host = "127.0.0.1"
        mock_cfg.search.port = 9999
        mock_cfg.search.sync_timeout_seconds = 5
        mock_cfg.search.db_path = db_path
        mock_cfg.search.pinned_collections = []
        mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
        mock_cfg.search.chunk_size = 512
        mock_cfg.search.auto_reindex_on_chunk_size_change = False
        mock_cfg.search.watch = False
        mock_cfg.search.all_indexed_collections = []
        mock_cfg.history.directory = "/tmp/history"

        mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore") as MockStateStore,
        ):
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

            MockStateStore.assert_called_once_with(Path(db_path))

    @pytest.mark.asyncio
    async def test_main_passes_state_store_to_sync(self) -> None:
        """main() passes the created state_store to SearchCollectionSync."""
        from archon_search.server.mcp import main
        from archon_search.sync import SyncResult

        mock_store = MagicMock()
        mock_store.connect = AsyncMock()
        mock_store.disconnect = AsyncMock()

        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store

        mock_app = MagicMock()
        mock_app.run_http_async = AsyncMock()

        db_path = "/tmp/test-rag-db"
        mock_cfg = MagicMock()
        mock_cfg.search.host = "127.0.0.1"
        mock_cfg.search.port = 9999
        mock_cfg.search.sync_timeout_seconds = 5
        mock_cfg.search.db_path = db_path
        mock_cfg.search.pinned_collections = []
        mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
        mock_cfg.search.chunk_size = 512
        mock_cfg.search.auto_reindex_on_chunk_size_change = False
        mock_cfg.search.watch = False
        mock_cfg.search.all_indexed_collections = []
        mock_cfg.history.directory = "/tmp/history"

        mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])
        sentinel_state_store = MagicMock()

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

            # Verify state_store is passed; pinned_collections will have SearchConfig defaults
            call_kwargs = MockSync.call_args[1]
            assert call_kwargs["state_store"] is sentinel_state_store
            assert "pinned_collections" in call_kwargs

    @pytest.mark.asyncio
    async def test_main_passes_pinned_collections_to_sync(self) -> None:
        """main() passes cfg.rag.pinned_collections to SearchCollectionSync."""
        from archon_search.server.mcp import main
        from archon_search.sync import SyncResult

        mock_store = MagicMock()
        mock_store.connect = AsyncMock()
        mock_store.disconnect = AsyncMock()

        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store

        mock_app = MagicMock()
        mock_app.run_http_async = AsyncMock()

        db_path = "/tmp/test-rag-db"
        pinned = ["~/docs/notes", "~/docs/wiki"]
        mock_cfg = MagicMock()
        mock_cfg.search.host = "127.0.0.1"
        mock_cfg.search.port = 9999
        mock_cfg.search.sync_timeout_seconds = 5
        mock_cfg.search.db_path = db_path
        mock_cfg.search.pinned_collections = pinned
        mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
        mock_cfg.search.chunk_size = 512
        mock_cfg.search.auto_reindex_on_chunk_size_change = False
        mock_cfg.search.watch = False
        mock_cfg.search.all_indexed_collections = []
        mock_cfg.history.directory = "/tmp/history"

        mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])
        sentinel_state_store = MagicMock()

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

            MockSync.assert_called_once_with(
                mock_pipeline,
                state_store=sentinel_state_store,
                pinned_collections=pinned,
                embedding_model=mock_cfg.search.embedding_model,
                chunk_size=mock_cfg.search.chunk_size,
                auto_reindex_on_chunk_size_change=mock_cfg.search.auto_reindex_on_chunk_size_change,
            )


@pytest.mark.asyncio
async def test_server_sync_passes_config_params() -> None:
    """main() passes embedding_model, chunk_size, auto_reindex_on_chunk_size_change to SearchCollectionSync."""
    from archon_search.server.mcp import main
    from archon_search.sync import SyncResult

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = 5
    mock_cfg.search.db_path = "/tmp/test-rag-db"
    mock_cfg.search.embedding_model = "custom-embed-model"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = True
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.watch = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])
    sentinel_state_store = MagicMock()

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        await main()

        MockSync.assert_called_once_with(
            mock_pipeline,
            state_store=sentinel_state_store,
            pinned_collections=mock_cfg.search.pinned_collections,
            embedding_model="custom-embed-model",
            chunk_size=512,
            auto_reindex_on_chunk_size_change=True,
        )


# ---------------------------------------------------------------------------
# F5 — _needs_install_trigger unit tests (all branches)
# ---------------------------------------------------------------------------


class TestNeedsInstallTrigger:
    """Direct unit tests for _needs_install_trigger covering all branches."""

    def _state(self, **collections):  # type: ignore[return]
        from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
        parsed = {
            name: CollectionProgress(status=IndexingStatus(status))
            for name, status in collections.items()
        }
        return IndexingState(collections=parsed)

    def test_no_state_with_collections_returns_true(self) -> None:
        from archon_search.server.mcp import _needs_install_trigger
        assert _needs_install_trigger(None, {"a": "/a"}) is True

    def test_no_state_empty_desired_returns_false(self) -> None:
        """Empty desired → nothing to index → no trigger, even with no state file."""
        from archon_search.server.mcp import _needs_install_trigger
        assert _needs_install_trigger(None, {}) is False

    def test_empty_desired_returns_false_regardless_of_state(self) -> None:
        from archon_search.progress import IndexingState
        from archon_search.server.mcp import _needs_install_trigger
        assert _needs_install_trigger(IndexingState({}), {}) is False

    def test_empty_desired_with_done_collections_returns_false(self) -> None:
        from archon_search.server.mcp import _needs_install_trigger
        state = self._state(a="done")
        assert _needs_install_trigger(state, {}) is False

    def test_all_done_returns_false(self) -> None:
        from archon_search.server.mcp import _needs_install_trigger
        state = self._state(a="done")
        assert _needs_install_trigger(state, {"a": "/a"}) is False

    def test_in_progress_returns_true(self) -> None:
        """IN_PROGRESS on startup means crash → needs re-trigger."""
        from archon_search.server.mcp import _needs_install_trigger
        state = self._state(a="in_progress")
        assert _needs_install_trigger(state, {"a": "/a"}) is True

    def test_failed_returns_true(self) -> None:
        from archon_search.server.mcp import _needs_install_trigger
        state = self._state(a="failed")
        assert _needs_install_trigger(state, {"a": "/a"}) is True

    def test_pending_returns_true(self) -> None:
        from archon_search.server.mcp import _needs_install_trigger
        state = self._state(a="pending")
        assert _needs_install_trigger(state, {"a": "/a"}) is True

    def test_collection_absent_from_state_returns_true(self) -> None:
        from archon_search.progress import IndexingState
        from archon_search.server.mcp import _needs_install_trigger
        assert _needs_install_trigger(IndexingState({}), {"a": "/a"}) is True

    def test_mixed_done_and_failed_returns_true(self) -> None:
        from archon_search.server.mcp import _needs_install_trigger
        state = self._state(a="done", b="failed")
        assert _needs_install_trigger(state, {"a": "/a", "b": "/b"}) is True


# ---------------------------------------------------------------------------
# Task 5.3 — server.py sets install trigger before sync
# ---------------------------------------------------------------------------


def _make_server_mocks(sync_timeout: int = 5, db_path: str = "/tmp/test-rag-db"):
    """Return common mocks needed for main() trigger tests."""
    from archon_search.sync import SyncResult

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = sync_timeout
    mock_cfg.search.db_path = db_path
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = False
    mock_cfg.search.watch = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])
    return mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result


class TestServerInstallTrigger:
    @pytest.mark.asyncio
    async def test_server_startup_sync_sets_install_trigger(self) -> None:
        """main() calls state_store.set_trigger('install') before sync.sync() (normal path)."""
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None  # no previous state → trigger must be set
        call_order: list[str] = []
        sentinel_state_store.set_trigger.side_effect = lambda t: call_order.append(f"set_trigger:{t}")

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"some_col": "/some/path"}
            MockSync.return_value.sync = AsyncMock(
                side_effect=lambda cols: (call_order.append("sync"), mock_sync_result)[1]
            )
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")
        assert call_order.index("set_trigger:install") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_server_background_path_sets_install_trigger(self) -> None:
        """main() calls set_trigger('install') even when sync_timeout_seconds=0 (background path)."""
        import asyncio
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, _ = _make_server_mocks(sync_timeout=0)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None  # no previous state → trigger must be set
        call_order: list[str] = []
        sentinel_state_store.set_trigger.side_effect = lambda t: call_order.append(f"set_trigger:{t}")

        def _fake_create_task(coro, **kw):
            call_order.append("create_task")
            task = asyncio.ensure_future(coro)
            return task

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
            patch("asyncio.create_task", side_effect=_fake_create_task),
        ):
            MockSync.return_value.build_desired.return_value = {"some_col": "/some/path"}
            MockSync.return_value.sync = AsyncMock()
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")
        assert call_order.index("set_trigger:install") < call_order.index("create_task")

    @pytest.mark.asyncio
    async def test_server_timeout_fallback_path_sets_install_trigger(self) -> None:
        """main() calls set_trigger('install') before any branch — also covers timeout-fallback path."""
        import asyncio as _asyncio
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, _ = _make_server_mocks(sync_timeout=1)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None  # no previous state → trigger must be set

        async def _slow_sync(cols):
            await _asyncio.sleep(10)

        def _fake_create_task(coro, **kw):
            task = _asyncio.ensure_future(coro)
            return task

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
            patch("asyncio.create_task", side_effect=_fake_create_task),
        ):
            MockSync.return_value.build_desired.return_value = {"some_col": "/some/path"}
            MockSync.return_value.sync = _slow_sync
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")

    @pytest.mark.asyncio
    async def test_server_startup_set_trigger_failure_does_not_crash(self) -> None:
        """If set_trigger('install') raises, main() logs a warning and continues — server still starts."""
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None  # no previous state → trigger must fire
        sentinel_state_store.set_trigger.side_effect = OSError("disk full")

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"some_col": "/some/path"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()  # must not raise

        mock_app.run_http_async.assert_awaited_once()
        MockSync.return_value.sync.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 8.5 — WatcherManager integration tests
# ---------------------------------------------------------------------------


def _make_server_mocks_with_collections(
    watch: bool = False,
    collections: list[str] | None = None,
    sync_timeout: int = 5,
) -> tuple:
    """Return common mocks for watcher integration tests."""
    from archon_search.sync import SyncResult

    mock_store = MagicMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    mock_pipeline = MagicMock()
    mock_pipeline.store = mock_store

    mock_app = MagicMock()
    mock_app.run_http_async = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.search.host = "127.0.0.1"
    mock_cfg.search.port = 9999
    mock_cfg.search.sync_timeout_seconds = sync_timeout
    mock_cfg.search.collections = collections or ["~/docs"]
    mock_cfg.search.watch = watch
    mock_cfg.search.db_path = "/tmp/test-rag-db"
    mock_cfg.search.pinned_collections = []
    mock_cfg.search.embedding_model = "BAAI/bge-small-en-v1.5"
    mock_cfg.search.chunk_size = 512
    mock_cfg.search.auto_reindex_on_chunk_size_change = False
    mock_cfg.search.all_indexed_collections = []
    mock_cfg.history.directory = "/tmp/history"

    mock_sync_result = SyncResult(added=[], removed=[], unchanged=[], errors=[], skipped=[])
    return mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result


@pytest.mark.asyncio
async def test_server_starts_watcher_manager_when_watch_true() -> None:
    """main() creates WatcherManager, calls add() for each collection, stop_all() in finally."""
    from archon_search.server.mcp import main

    mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = (
        _make_server_mocks_with_collections(watch=True, collections=["~/docs", "~/notes"])
    )
    sentinel_state_store = MagicMock()
    mock_watcher_manager = MagicMock()
    mock_watcher_manager.stop_all = AsyncMock()

    desired = {"docs": "/home/user/docs", "notes": "/home/user/notes"}

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        patch("archon_search.watcher.WatcherManager", return_value=mock_watcher_manager) as MockWatcherManager,
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        MockSync.return_value.build_desired = MagicMock(return_value=desired)
        await main()

    MockWatcherManager.assert_called_once()
    assert mock_watcher_manager.add.call_count == 2
    mock_watcher_manager.add.assert_any_call("docs", Path("/home/user/docs"))
    mock_watcher_manager.add.assert_any_call("notes", Path("/home/user/notes"))
    mock_watcher_manager.stop_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_skips_watcher_manager_when_watch_false() -> None:
    """main() does NOT create WatcherManager when cfg.rag.watch=False."""
    from archon_search.server.mcp import main

    mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = (
        _make_server_mocks_with_collections(watch=False, collections=["~/docs"])
    )
    sentinel_state_store = MagicMock()

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        patch("archon_search.watcher.WatcherManager") as MockWatcherManager,
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        MockSync.return_value.build_desired = MagicMock(return_value={"docs": "/home/user/docs"})
        await main()

    MockWatcherManager.assert_not_called()


@pytest.mark.asyncio
async def test_server_stops_watcher_on_shutdown() -> None:
    """main() calls watcher_manager.stop_all() in finally even when run_http_async raises."""
    from archon_search.server.mcp import main

    mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = (
        _make_server_mocks_with_collections(watch=True, collections=["~/docs"])
    )
    sentinel_state_store = MagicMock()
    mock_watcher_manager = MagicMock()
    call_order: list[str] = []
    mock_watcher_manager.stop_all = AsyncMock(side_effect=lambda: call_order.append("stop_all"))
    mock_store.disconnect = AsyncMock(side_effect=lambda: call_order.append("disconnect"))
    mock_app.run_http_async = AsyncMock(side_effect=RuntimeError("server error"))

    desired = {"docs": "/home/user/docs"}

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        patch("archon_search.watcher.WatcherManager", return_value=mock_watcher_manager),
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        MockSync.return_value.build_desired = MagicMock(return_value=desired)
        with pytest.raises(RuntimeError, match="server error"):
            await main()

    mock_watcher_manager.stop_all.assert_awaited_once()
    assert call_order == ["stop_all", "disconnect"]


@pytest.mark.asyncio
async def test_server_on_change_handles_sync_exception(caplog: pytest.LogCaptureFixture) -> None:
    """_on_change callback logs error and watcher continues when sync_collection raises."""
    import logging
    from archon_search.server.mcp import main

    mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = (
        _make_server_mocks_with_collections(watch=True, collections=["~/docs"])
    )
    sentinel_state_store = MagicMock()

    captured_on_change: list = []
    desired = {"docs": "/home/user/docs"}

    class _CapturingWatcherManager:
        def __init__(self, on_change, loop, **kwargs):
            captured_on_change.append(on_change)
            self.stop_all = AsyncMock()
            self.add = MagicMock()

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        patch("archon_search.watcher.WatcherManager", _CapturingWatcherManager),
        caplog.at_level(logging.ERROR),
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        MockSync.return_value.sync_collection = AsyncMock(side_effect=RuntimeError("sync boom"))
        MockSync.return_value.build_desired = MagicMock(return_value=desired)
        await main()

        # Call _on_change from inside the context so MockSync.return_value is still valid
        assert len(captured_on_change) == 1
        on_change = captured_on_change[0]
        # The callback should not propagate the exception — sync_collection raises RuntimeError
        await on_change("docs")
        MockSync.return_value.sync_collection.assert_awaited_once()
        assert any("sync boom" in r.message for r in caplog.records)



@pytest.mark.asyncio
async def test_server_stop_all_exception_still_disconnects() -> None:
    """disconnect() is called even when stop_all() raises."""
    from archon_search.server.mcp import main

    mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = (
        _make_server_mocks_with_collections(watch=True, collections=["~/docs"])
    )
    sentinel_state_store = MagicMock()
    mock_watcher_manager = MagicMock()
    mock_watcher_manager.stop_all = AsyncMock(side_effect=RuntimeError("stop failed"))

    desired = {"docs": "/home/user/docs"}

    with (
        patch("archon.config.loader.load_config", return_value=mock_cfg),
        patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
        patch("archon_search.server.mcp.create_app", return_value=mock_app),
        patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
        patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        patch("archon_search.watcher.WatcherManager", return_value=mock_watcher_manager),
    ):
        MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
        MockSync.return_value.build_desired = MagicMock(return_value=desired)
        await main()

    mock_store.disconnect.assert_awaited_once()

# ---------------------------------------------------------------------------
# Bug 1 — set_trigger skipped when indexed collections already exist
# ---------------------------------------------------------------------------


class TestServerInstallTriggerSkippedWhenCollectionsExist:
    @pytest.mark.asyncio
    async def test_set_trigger_skipped_when_state_has_indexed_collections(self) -> None:
        """main() skips set_trigger('install') when existing state has indexed collections."""
        from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        sentinel_state_store = MagicMock()
        # Simulate existing state with an indexed collection
        existing_state = IndexingState(
            collections={"docs": CollectionProgress(status=IndexingStatus.DONE)}
        )
        sentinel_state_store.read.return_value = existing_state

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            # build_desired returns {"docs": "/tmp/docs"} → "docs" IS in state as DONE → no trigger
            MockSync.return_value.build_desired.return_value = {"docs": "/tmp/docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_trigger_called_when_state_is_none(self) -> None:
        """main() calls set_trigger('install') when state file is absent (None)."""
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"docs": "/tmp/docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")

    @pytest.mark.asyncio
    async def test_set_trigger_called_when_state_has_empty_collections(self) -> None:
        """main() calls set_trigger('install') when existing state has no collections."""
        from archon_search.progress import IndexingState
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = IndexingState(collections={})

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            # build_desired returns a configured collection → not in state → trigger must fire
            MockSync.return_value.build_desired.return_value = {"docs": "/tmp/docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")

    @pytest.mark.asyncio
    async def test_set_trigger_called_when_pinned_collection_missing_from_state(self) -> None:
        """main() calls set_trigger('install') when a configured collection is absent from state."""
        from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        # Add a pinned collection to the config
        mock_cfg.search.pinned_collections = ["/tmp/my-docs"]
        sentinel_state_store = MagicMock()
        # State exists but the pinned collection is NOT indexed yet
        existing_state = IndexingState(
            collections={"other": CollectionProgress(status=IndexingStatus.DONE)}
        )
        sentinel_state_store.read.return_value = existing_state

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"my_docs": "/tmp/my-docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")

    @pytest.mark.asyncio
    async def test_set_trigger_called_when_collection_has_failed_status(self) -> None:
        """main() calls set_trigger('install') when a configured collection has FAILED status."""
        from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        mock_cfg.search.pinned_collections = ["/tmp/my-docs"]
        sentinel_state_store = MagicMock()
        # State exists, collection is present but FAILED
        existing_state = IndexingState(
            collections={"my_docs": CollectionProgress(status=IndexingStatus.FAILED)}
        )
        sentinel_state_store.read.return_value = existing_state

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"my_docs": "/tmp/my-docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")

    @pytest.mark.asyncio
    async def test_set_trigger_skipped_when_all_collections_done(self) -> None:
        """main() skips set_trigger when all configured collections are DONE in state."""
        from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        mock_cfg.search.pinned_collections = ["/tmp/my-docs"]
        sentinel_state_store = MagicMock()
        existing_state = IndexingState(
            collections={"my_docs": CollectionProgress(status=IndexingStatus.DONE)}
        )
        sentinel_state_store.read.return_value = existing_state

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"my_docs": "/tmp/my-docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_trigger_called_when_all_collections_in_progress(self) -> None:
        """main() calls set_trigger when all configured collections are IN_PROGRESS.

        On restart, IN_PROGRESS always means the previous process crashed mid-index.
        We must re-trigger so the notification monitor fires when indexing completes.
        """
        from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, mock_sync_result = _make_server_mocks(sync_timeout=5)
        mock_cfg.search.pinned_collections = ["/tmp/my-docs"]
        sentinel_state_store = MagicMock()
        existing_state = IndexingState(
            collections={"my_docs": CollectionProgress(status=IndexingStatus.IN_PROGRESS)}
        )
        sentinel_state_store.read.return_value = existing_state

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
        ):
            MockSync.return_value.build_desired.return_value = {"my_docs": "/tmp/my-docs"}
            MockSync.return_value.sync = AsyncMock(return_value=mock_sync_result)
            await main()

        sentinel_state_store.set_trigger.assert_called_once_with("install")


# ---------------------------------------------------------------------------
# Bug 2 — Background sync task done callback logs exceptions
# ---------------------------------------------------------------------------


class TestServerBackgroundSyncErrorLogging:
    @pytest.mark.asyncio
    async def test_background_sync_task_exception_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When background sync task raises, the exception is logged at ERROR level."""
        import asyncio
        import logging
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, _ = _make_server_mocks(sync_timeout=0)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None

        done_callbacks: list = []

        def _capture_task(coro, **kw):
            task = asyncio.ensure_future(coro)
            task.add_done_callback = MagicMock(side_effect=lambda cb: done_callbacks.append(cb))
            return task

        async def _failing_sync(cols):
            raise RuntimeError("sync exploded")

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
            patch("asyncio.create_task", side_effect=_capture_task),
            caplog.at_level(logging.ERROR),
        ):
            MockSync.return_value.sync = _failing_sync
            await main()

        # A done callback must have been registered on the task
        assert len(done_callbacks) >= 1

    @pytest.mark.asyncio
    async def test_background_sync_done_callback_logs_on_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The done callback logs at ERROR when the task raises an exception."""
        import asyncio
        import logging
        from archon_search.server.mcp import main

        mock_store, mock_pipeline, mock_app, mock_cfg, _ = _make_server_mocks(sync_timeout=0)
        sentinel_state_store = MagicMock()
        sentinel_state_store.read.return_value = None

        real_tasks: list = []

        def _capture_task(coro, **kw):
            task = asyncio.ensure_future(coro)
            real_tasks.append(task)
            return task

        async def _failing_sync(cols):
            raise RuntimeError("sync exploded")

        with (
            patch("archon.config.loader.load_config", return_value=mock_cfg),
            patch("archon_search.server.mcp.create_pipeline", return_value=mock_pipeline),
            patch("archon_search.server.mcp.create_app", return_value=mock_app),
            patch("archon_search.server.mcp.SearchCollectionSync") as MockSync,
            patch("archon_search.server.mcp.IndexingStateStore", return_value=sentinel_state_store),
            patch("asyncio.create_task", side_effect=_capture_task),
            caplog.at_level(logging.ERROR),
        ):
            MockSync.return_value.sync = _failing_sync
            await main()

        # Wait for the background task to finish so the callback fires
        for task in real_tasks:
            try:
                await task
            except Exception:
                pass

        assert any("sync" in r.message.lower() for r in caplog.records if r.levelno >= logging.ERROR)


@pytest.mark.asyncio
async def test_health_endpoint_returns_200() -> None:
    """GET /health returns 200 with JSON body {"status": "ok"}."""
    import httpx

    pipeline = _make_pipeline()
    app = _make_app(pipeline)

    asgi_app = app.http_app()
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

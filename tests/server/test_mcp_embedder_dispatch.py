"""Tests for MCP per-collection embedder dispatch (Task 7.4).

Verifies that search, search_with_context, explain, ingest_file, and
ingest_directory tools all resolve the active_embedding_model from
CollectionMeta and call embedder_cache.get_or_load with it, passing
the resolved embedder into the pipeline method.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# ---------------------------------------------------------------------------
# FastMCP stub
# ---------------------------------------------------------------------------
if "fastmcp" not in sys.modules:
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


def _make_mcp_app(
    pipeline: Any,
    *,
    config: Any = None,
    embedder_cache: Any = None,
) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(
            pipeline, "default", config=config, embedder_cache=embedder_cache
        )


def _make_embedder_cache(model_name: str = "model-X") -> tuple[MagicMock, MagicMock]:
    """Return (cache, resolved_embedder). cache.get_or_load returns resolved_embedder."""
    resolved_embedder = MagicMock()
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=resolved_embedder)
    return cache, resolved_embedder


def _make_collection_meta(model: str = "model-X") -> MagicMock:
    from archon_search.collection_meta import CollectionMeta
    return CollectionMeta(name="col", namespace="default", active_embedding_model=model)


# ---------------------------------------------------------------------------
# search tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_calls_embedder_cache_get_or_load() -> None:
    """search tool must call embedder_cache.get_or_load with active_embedding_model."""
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["search"](query="q", collection="col")

    cache.get_or_load.assert_awaited_once_with("model-X")


@pytest.mark.asyncio
async def test_mcp_search_passes_resolved_embedder_to_pipeline() -> None:
    """search tool must pass the resolved embedder to pipeline.search."""
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["search"](query="q", collection="col")

    _, kwargs = pipeline.search.call_args
    assert kwargs["embedder"] is resolved


@pytest.mark.asyncio
async def test_mcp_search_empty_active_model_falls_back_to_global() -> None:
    """search with empty active_embedding_model must use global config model."""
    from archon_search.config import SearchConfig
    from archon_search.pipeline import SearchPipelineResult

    config = SearchConfig()
    config.embedding_model = "global-model"

    pipeline = MagicMock()
    meta = _make_collection_meta("")  # empty → fall back to global
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    cache, _ = _make_embedder_cache("global-model")
    app = _make_mcp_app(pipeline, config=config, embedder_cache=cache)

    await app.tools["search"](query="q", collection="col")

    cache.get_or_load.assert_awaited_once_with("global-model")


# ---------------------------------------------------------------------------
# search_with_context tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_with_context_calls_embedder_cache_get_or_load() -> None:
    """search_with_context tool must call embedder_cache.get_or_load with active model."""
    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False)))

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["search_with_context"](query="q", collection="col")

    cache.get_or_load.assert_awaited_once_with("model-X")


@pytest.mark.asyncio
async def test_mcp_search_with_context_passes_resolved_embedder_to_pipeline() -> None:
    """search_with_context tool must pass resolved embedder to pipeline."""
    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False)))

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["search_with_context"](query="q", collection="col")

    _, kwargs = pipeline.search_with_context.call_args
    assert kwargs["embedder"] is resolved


# ---------------------------------------------------------------------------
# explain tool — single collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_single_collection_calls_embedder_cache_get_or_load() -> None:
    """explain with a pinned collection must call embedder_cache.get_or_load."""
    from archon_search.pipeline import ExplainPipelineResult

    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    )

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["explain"](query="q", collection="col")

    cache.get_or_load.assert_awaited_once_with("model-X")


@pytest.mark.asyncio
async def test_mcp_explain_single_collection_passes_resolved_embedder_to_pipeline() -> None:
    """explain with a pinned collection must pass resolved embedder to pipeline."""
    from archon_search.pipeline import ExplainPipelineResult

    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    )

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["explain"](query="q", collection="col")

    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("embedder") is resolved


# ---------------------------------------------------------------------------
# explain tool — routing (auto-routing, no collection param)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_explain_routing_uses_chosen_collection_model() -> None:
    """explain without a collection must use the chosen collection's active model."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig
    from archon_search.pipeline import ExplainPipelineResult

    config = SearchConfig()
    config.embedding_model = "global-model"
    config.routing_confidence_threshold = 0.0

    chosen_meta = CollectionMeta(
        name="col",
        namespace="default",
        active_embedding_model="model-X",
        centroid=[1.0, 0.0],
    )
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[chosen_meta])
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    )

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, config=config, embedder_cache=cache)

    await app.tools["explain"](query="q")  # no collection → routing

    cache.get_or_load.assert_awaited_once_with("model-X")
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("embedder") is resolved


# ---------------------------------------------------------------------------
# ingest_file tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_ingest_file_calls_embedder_cache_get_or_load(tmp_path: "Path") -> None:
    """ingest_file must resolve and pass the collection's embedder via embedder_cache."""
    from dataclasses import dataclass
    from pathlib import Path

    @dataclass
    class _IngestResult:
        chunks_indexed: int = 1
        doc_id: str = "x" * 64
        doc_ids: list = None
        def __post_init__(self): self.doc_ids = [self.doc_id]

    test_file = tmp_path / "doc.md"
    test_file.write_text("hello")

    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.ingest_file = AsyncMock(return_value=_IngestResult())

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["ingest_file"](path=str(test_file), collection="col")

    cache.get_or_load.assert_awaited_once_with("model-X")
    _, kwargs = pipeline.ingest_file.call_args
    assert kwargs["embedder"] is resolved


# ---------------------------------------------------------------------------
# ingest_directory tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_ingest_directory_calls_embedder_cache_get_or_load(tmp_path: "Path") -> None:
    """ingest_directory must resolve and pass the collection's embedder via embedder_cache."""
    pipeline = MagicMock()
    meta = _make_collection_meta("model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.ingest_directory = AsyncMock(return_value=[])

    cache, resolved = _make_embedder_cache("model-X")
    app = _make_mcp_app(pipeline, embedder_cache=cache)

    await app.tools["ingest_directory"](path=str(tmp_path), collection="col")

    cache.get_or_load.assert_awaited_once_with("model-X")
    _, kwargs = pipeline.ingest_directory.call_args
    assert kwargs["embedder"] is resolved

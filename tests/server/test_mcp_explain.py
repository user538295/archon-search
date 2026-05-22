"""Tests for the explain MCP tool (Task 4.1)."""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure fastmcp is stubbed before importing mcp module
if "fastmcp" not in sys.modules:
    try:
        import mcp.server.fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.collection_meta import CollectionMeta
from archon_search.pipeline import ExplainPipelineResult


# ---------------------------------------------------------------------------
# FastMCP stub that captures registered tools
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
# Helpers
# ---------------------------------------------------------------------------


def _make_breakdown(rrf: float = 0.1, reranker: float | None = None) -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=0,
        vector_score=0.5,
        vector_score_kind="distance",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=rrf,
        reranker_score=reranker,
    )


def _make_candidate(doc_id: str = "a" * 64) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="sample text",
        source_path="/path/doc.md",
        score_breakdown=_make_breakdown(rrf=0.1, reranker=0.8),
        collection="my-col",
    )


def _make_pipeline(
    meta_return: CollectionMeta | None = None,
    all_meta_return: list[CollectionMeta] | None = None,
    explain_return: ExplainPipelineResult | None = None,
    meta_raises: Exception | None = None,
) -> MagicMock:
    pipeline = MagicMock()

    default_meta = CollectionMeta(name="my-col", namespace="default")

    if meta_raises is not None:
        pipeline.get_collection_meta = AsyncMock(side_effect=meta_raises)
    else:
        pipeline.get_collection_meta = AsyncMock(
            return_value=meta_return if meta_return is not None else default_meta
        )

    pipeline.get_all_collections_meta = AsyncMock(
        return_value=all_meta_return if all_meta_return is not None else [default_meta]
    )

    default_explain = ExplainPipelineResult(
        top_results=[_make_candidate()],
        near_misses=[],
        acl_filtered=False,
    )
    pipeline.explain = AsyncMock(return_value=explain_return or default_explain)

    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
    embedder.model_name = "test-model"
    pipeline._embedder = embedder

    return pipeline


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mcp_app_registers_explain_tool() -> None:
    """The MCP app must have exactly 10 registered tools, including explain."""
    pipeline = _make_pipeline()
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default")  # type: ignore[call-arg]
        assert "explain" in app.tools  # type: ignore[attr-defined]
        assert len(app.tools) == 10  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mcp_explain_rejects_empty_query() -> None:
    pipeline = _make_pipeline()
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default")  # type: ignore[call-arg]
        explain_fn = app.tools["explain"]  # type: ignore[attr-defined]
        result = await explain_fn(query="", collection="my-col")

    assert result.get("code") == "validation_error"


@pytest.mark.asyncio
async def test_mcp_explain_missing_collection_returns_not_found() -> None:
    pipeline = _make_pipeline()
    # Override: return None for get_collection_meta
    pipeline.get_collection_meta = AsyncMock(return_value=None)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default")  # type: ignore[call-arg]
        explain_fn = app.tools["explain"]  # type: ignore[attr-defined]
        result = await explain_fn(query="hello", collection="no-such-col")

    assert result.get("code") == "not_found"


@pytest.mark.asyncio
async def test_mcp_explain_collectionless_no_collections_returns_not_found() -> None:
    pipeline = _make_pipeline(all_meta_return=[])

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default")  # type: ignore[call-arg]
        explain_fn = app.tools["explain"]  # type: ignore[attr-defined]
        result = await explain_fn(query="hello")

    assert result.get("code") == "not_found"

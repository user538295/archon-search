"""Unit tests for SearchPipeline filter forwarding (A2).

Uses a fully mocked store — no real LanceDB, no model downloads.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import SearchResult
from archon_search.embedder import Embedder
from archon_search.filters import SearchFilters
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker


class _MockEmbedderBackend:
    model_name = "mock"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class _MockRerankerBackend:
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


class _MockChunker:
    """Minimal chunker stub — never called in search path."""

    def chunk(self, text: str, doc_id: str, source_path: str, **kwargs):  # type: ignore[override]
        return []


class _MockParser:
    async def parse(self, path):  # type: ignore[override]
        return ""


def _make_result(n: int = 1) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + f"-{n:06d}",
        text=f"text {n}",
        score=0.9,
        source_path=f"/tmp/doc{n}.md",
    )


def _make_pipeline_with_mock_store(results: list[SearchResult] | None = None) -> tuple[SearchPipeline, MagicMock]:
    store = MagicMock()
    store.hybrid_search = AsyncMock(return_value=results or [])
    store.get_collection_meta = AsyncMock(return_value=MagicMock())
    store.fetch_adjacent_chunks = AsyncMock(return_value=[])

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=_MockChunker(),  # type: ignore[arg-type]
        parser=_MockParser(),  # type: ignore[arg-type]
        top_k_retrieve=10,
        top_k_return=5,
    )
    return pipeline, store


# ---------------------------------------------------------------------------
# Filters forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_search_forwards_filters_to_store() -> None:
    """pipeline.search() must forward filters= to store.hybrid_search()."""
    pipeline, store = _make_pipeline_with_mock_store()
    f = SearchFilters(file_type="md")

    await pipeline.search("hello", "my-col", filters=f)

    store.hybrid_search.assert_called_once()
    call_kwargs = store.hybrid_search.call_args
    assert call_kwargs.kwargs.get("filters") is f


@pytest.mark.asyncio
async def test_pipeline_no_filter_passes_none_to_store() -> None:
    """pipeline.search() with no filters passes filters=None to store.hybrid_search()."""
    pipeline, store = _make_pipeline_with_mock_store()

    await pipeline.search("hello", "my-col")

    store.hybrid_search.assert_called_once()
    call_kwargs = store.hybrid_search.call_args
    assert call_kwargs.kwargs.get("filters") is None


@pytest.mark.asyncio
async def test_pipeline_warns_on_filter_plus_acl_under_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When filters reduce pool below top_k_return, pipeline emits a WARNING."""
    # Return only 2 results (below top_k_return=5) after ACL filter
    results = [_make_result(i) for i in range(2)]
    pipeline, _ = _make_pipeline_with_mock_store(results=results)
    f = SearchFilters(file_type="md")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("hello", "my-col", filters=f)

    assert any("reduced candidate pool" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_no_filter_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No under-delivery warning when filters=None (even if pool < top_k)."""
    results = [_make_result(i) for i in range(1)]
    pipeline, _ = _make_pipeline_with_mock_store(results=results)

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("hello", "my-col")  # no filters

    assert not any("reduced candidate pool" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_pool_above_top_k(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No under-delivery warning when pool >= top_k_return."""
    results = [_make_result(i) for i in range(10)]  # 10 > top_k_return=5
    pipeline, _ = _make_pipeline_with_mock_store(results=results)
    f = SearchFilters(file_type="md")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("hello", "my-col", filters=f)

    assert not any("reduced candidate pool" in r.message for r in caplog.records)

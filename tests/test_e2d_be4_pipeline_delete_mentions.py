"""Unit tests for BE-4: graph_store.delete_mentions_by_doc wired into pipeline.delete_document."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from unittest.mock import patch

from archon_search.config import GraphConfig


# ---------------------------------------------------------------------------
# Helper — build a minimal SearchPipeline with optional graph components
# ---------------------------------------------------------------------------

def _make_pipeline(store, *, graph_store=None, graph_config=None):
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_store=graph_store,
        graph_config=graph_config,
    )


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_delete_document_calls_delete_mentions_by_doc(
    connected_store, col_name
):
    """Mock _graph_store; assert delete_mentions_by_doc called with correct args after delete."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mock_graph_store = MagicMock()
    mock_graph_store.delete_mentions_by_doc = AsyncMock()

    # Patch the store's get_collection_meta and delete_document methods
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=MagicMock())  # non-None = collection exists
    mock_store.delete_document = AsyncMock(return_value=3)

    pipeline = _make_pipeline(
        mock_store,
        graph_store=mock_graph_store,
        graph_config=graph_config,
    )

    result = await pipeline.delete_document(
        doc_id="doc-abc",
        collection=col_name,
        namespace="default",
    )

    assert result == 3

    # delete_mentions_by_doc must be called with the correct collection, doc_id, and namespace
    mock_graph_store.delete_mentions_by_doc.assert_called_once_with(
        col_name, "doc-abc", ns="default"
    )


@pytest.mark.asyncio
async def test_pipeline_delete_document_skips_graph_when_graph_store_none(
    connected_store, col_name
):
    """When _graph_store is None, delete_document succeeds without AttributeError."""
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=MagicMock())
    mock_store.delete_document = AsyncMock(return_value=2)

    pipeline = _make_pipeline(
        mock_store,
        graph_store=None,
        graph_config=None,
    )

    # Must not raise
    result = await pipeline.delete_document(
        doc_id="doc-xyz",
        collection=col_name,
        namespace="default",
    )

    assert result == 2


@pytest.mark.asyncio
async def test_pipeline_delete_document_graph_failure_does_not_fail_delete(
    connected_store, col_name, caplog
):
    """When delete_mentions_by_doc raises, delete_document still succeeds and WARNING is logged."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mock_graph_store = MagicMock()
    mock_graph_store.delete_mentions_by_doc = AsyncMock(
        side_effect=RuntimeError("Graph store connection lost")
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=MagicMock())
    mock_store.delete_document = AsyncMock(return_value=5)

    pipeline = _make_pipeline(
        mock_store,
        graph_store=mock_graph_store,
        graph_config=graph_config,
    )

    doc_id = "doc-fail"
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        result = await pipeline.delete_document(
            doc_id=doc_id,
            collection=col_name,
            namespace="default",
        )

    # Delete must succeed despite graph hook failure
    assert result == 5

    # A WARNING must be logged
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING to be logged"
    assert any(
        "graph mention cleanup failed" in r.message and doc_id in r.message
        for r in warning_records
    ), f"Expected cleanup WARNING with doc_id, got: {[r.message for r in warning_records]}"


@pytest.mark.asyncio
async def test_pipeline_delete_document_graph_hook_skipped_when_store_raises(
    col_name,
):
    """If store.delete_document raises, the graph hook must NOT fire and the exception propagates."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mock_graph_store = MagicMock()
    mock_graph_store.delete_mentions_by_doc = AsyncMock()

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=MagicMock())  # non-None = collection exists
    mock_store.delete_document = AsyncMock(side_effect=RuntimeError("store failure"))

    pipeline = _make_pipeline(
        mock_store,
        graph_store=mock_graph_store,
        graph_config=graph_config,
    )

    with pytest.raises(RuntimeError, match="store failure"):
        await pipeline.delete_document(
            doc_id="doc-store-raises",
            collection=col_name,
            namespace="default",
        )

    # Graph hook must NOT have been called
    mock_graph_store.delete_mentions_by_doc.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_delete_document_graph_hook_skipped_when_collection_not_found(
    mock_graph_store, connected_store
):
    """If the collection is not found, ValueError is raised and graph hook must NOT fire."""
    mock_graph_store.delete_mentions_by_doc = AsyncMock()
    pipeline = _make_pipeline(connected_store, graph_store=mock_graph_store)
    # Patch get_collection_meta to return None (collection not found); async method requires AsyncMock
    with patch.object(pipeline.store, "get_collection_meta", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ValueError, match="not found"):
            await pipeline.delete_document(
                "a" * 64,  # valid 64-char doc_id format
                "nonexistent_collection",
                namespace="default",
            )
    # Graph hook must NOT have been called — ValueError raised before reaching the hook
    mock_graph_store.delete_mentions_by_doc.assert_not_called()

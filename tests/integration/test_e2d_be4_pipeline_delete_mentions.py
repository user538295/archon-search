"""Integration tests for BE-4: delete_mentions_by_doc wired into pipeline.delete_document.

Uses real LanceDB in tmp_path. Tests verify:
- After ingest + delete, mentions for that doc_id are removed from the mentions table.
- Re-ingesting a deleted document produces no duplicate mentions (idempotent).

Run with:
    uv run pytest tests/integration/test_e2d_be4_pipeline_delete_mentions.py -v --no-cov
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphExtractionResult,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

pytestmark = pytest.mark.integration

_EMBEDDING_DIM = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_embedder():
    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * _EMBEDDING_DIM for _ in texts]

    return Embedder(_MockEmbedderBackend())


def _make_pipeline(store, *, graph_extractor=None, graph_store=None, graph_config=None):
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * _EMBEDDING_DIM for _ in texts]

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
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )


def _make_stub_extractor_for_entity_names(entity_names: list[str], collection: str):
    """Return a mock GraphExtractor that produces extractions based on the doc_id passed at extract time.

    The extractor captures the doc_id from the actual extract() call so that the
    resulting mentions use the real pipeline-generated doc_id (sha256 of path).
    """
    async def _extract(chunk_inputs, doc_id, collection_name):
        node_a_name = entity_names[0]
        node_a = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, node_a_name.lower()),
            entity_name=node_a_name,
            entity_type=EntityType.concept,
            source_doc_id=doc_id,
            collection_name=collection_name,
        )

        nodes = [node_a]
        mentions = [
            GraphMention(entity_id=node_a.id, chunk_id="chunk-0", doc_id=doc_id)
        ]

        if len(entity_names) > 1:
            node_b_name = entity_names[1]
            node_b = GraphNode(
                id=make_stable_entity_id(EntityType.concept.value, node_b_name.lower()),
                entity_name=node_b_name,
                entity_type=EntityType.concept,
                source_doc_id=doc_id,
                collection_name=collection_name,
            )
            nodes.append(node_b)
            mentions.append(GraphMention(entity_id=node_b.id, chunk_id="chunk-0", doc_id=doc_id))

        return GraphExtractionResult(
            nodes=nodes,
            edges=[],
            mentions=mentions,
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )

    extractor = MagicMock()
    extractor.extract = _extract
    return extractor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_delete_document_removes_mentions_real_graph(tmp_path: Path):
    """Ingest a doc, delete it, assert 0 mention rows for that doc_id in the real graph store."""
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_delete_removes_mentions"

    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)
    extractor = _make_stub_extractor_for_entity_names(["AuthService", "TokenValidator"], collection)
    pipeline = _make_pipeline(store, graph_extractor=extractor, graph_store=graph_store, graph_config=graph_config)

    # Create collection and ingest a file
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# AuthService\n\nTokenValidator is a component.\n")

    ingest_result = await pipeline.ingest_file(doc_file, collection, embedder=_make_embedder())
    assert ingest_result.status == "ok"
    doc_id = ingest_result.doc_id  # real sha256 hex doc_id from pipeline

    # Verify mentions were written
    mentions_before = await graph_store.get_all_mentions(collection, ns="default")
    doc_mentions_before = [m for m in mentions_before if m.doc_id == doc_id]
    assert len(doc_mentions_before) > 0, "Extractor should have written at least one mention"

    # Delete the document
    deleted_count = await pipeline.delete_document(
        doc_id=doc_id,
        collection=collection,
        namespace="default",
    )
    assert deleted_count >= 0

    # Verify mentions for this doc_id are gone
    mentions_after = await graph_store.get_all_mentions(collection, ns="default")
    doc_mentions_after = [m for m in mentions_after if m.doc_id == doc_id]
    assert len(doc_mentions_after) == 0, (
        f"Expected 0 mentions for doc_id={doc_id!r} after delete, "
        f"got {len(doc_mentions_after)}"
    )

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_pipeline_delete_document_idempotent_on_re_ingest(tmp_path: Path):
    """Delete a doc then re-ingest it; mention count must equal the fresh-ingest count (no duplicates)."""
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_delete_reingest_idempotent"

    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)
    extractor = _make_stub_extractor_for_entity_names(["AuthService"], collection)
    pipeline = _make_pipeline(store, graph_extractor=extractor, graph_store=graph_store, graph_config=graph_config)

    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# AuthService\n\nA simple service component.\n")

    # First ingest — doc_id is sha256 of the resolved file path
    result1 = await pipeline.ingest_file(doc_file, collection, embedder=_make_embedder())
    assert result1.status == "ok"
    doc_id = result1.doc_id

    mentions_after_first_ingest = await graph_store.get_all_mentions(collection, ns="default")
    first_ingest_count = len([m for m in mentions_after_first_ingest if m.doc_id == doc_id])
    assert first_ingest_count > 0, "Extractor should have written at least one mention"

    # Delete the document
    await pipeline.delete_document(doc_id=doc_id, collection=collection, namespace="default")

    # Verify mentions removed
    mentions_after_delete = await graph_store.get_all_mentions(collection, ns="default")
    assert len([m for m in mentions_after_delete if m.doc_id == doc_id]) == 0

    # Re-ingest the same file (extractor produces same mentions)
    result2 = await pipeline.ingest_file(doc_file, collection, embedder=_make_embedder())
    assert result2.status == "ok"

    mentions_after_reingest = await graph_store.get_all_mentions(collection, ns="default")
    reingest_count = len([m for m in mentions_after_reingest if m.doc_id == doc_id])

    assert reingest_count == first_ingest_count, (
        f"Re-ingest should produce same mention count as first ingest: "
        f"first={first_ingest_count}, after_reingest={reingest_count}. "
        "No duplicates should exist."
    )

    await store.disconnect()
    await graph_store.disconnect()

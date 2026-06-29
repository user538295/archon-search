"""Integration tests for BE-5: GraphExtractor + GraphStore wired into pipeline.ingest_file."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_store import GraphStore
from archon_search.graph_types import GraphEdge, GraphExtractionResult, GraphNode

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_pipeline_with_graph(store, graph_extractor, graph_store, graph_config):
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
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )


def _make_embedder():
    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    return Embedder(_MockEmbedderBackend())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_file_graph_entities_written(tmp_path: Path, monkeypatch):
    """Real GraphStore: ingest writes nodes and edges from stub extractor."""
    from archon_search.store import SearchStore
    from archon_search.graph_types import EntityType, RelationshipType, make_stable_entity_id, make_stable_edge_id

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_ingest_graph"

    # Fixed nodes and edge for the stub extractor
    node_a = GraphNode(
        id=make_stable_entity_id(EntityType.concept.value, "authservice"),
        entity_name="AuthService",
        entity_type=EntityType.concept,
        source_doc_id="doc1",
        collection_name=collection,
        entity_subtype=None,
    )
    node_b = GraphNode(
        id=make_stable_entity_id(EntityType.concept.value, "tokenvalidator"),
        entity_name="TokenValidator",
        entity_type=EntityType.concept,
        source_doc_id="doc1",
        collection_name=collection,
        entity_subtype=None,
    )
    edge = GraphEdge(
        id=make_stable_edge_id(node_a.id, node_b.id, RelationshipType.related_to.value),
        source_node_id=node_a.id,
        target_node_id=node_b.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc1",
    )

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[node_a, node_b],
            edges=[edge],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )
    )

    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)
    pipeline = _make_pipeline_with_graph(store, mock_extractor, graph_store, graph_config)

    # Write a real file
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# AuthService\n\nTokenValidator is related.\n")

    result = await pipeline.ingest_file(doc_file, collection, embedder=_make_embedder())
    assert result.status == "ok"

    assert await graph_store.node_count(collection) == 2
    assert await graph_store.edge_count(collection) == 1

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_ingest_after_graph_disable_skips_extraction_preserves_tables(tmp_path: Path):
    """Second ingest without graph skips extractor but tables from first ingest are preserved."""
    from archon_search.store import SearchStore
    from archon_search.graph_types import EntityType, make_stable_entity_id

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_disable_graph"

    node_a = GraphNode(
        id=make_stable_entity_id(EntityType.concept.value, "authservice"),
        entity_name="AuthService",
        entity_type=EntityType.concept,
        source_doc_id="doc1",
        collection_name=collection,
        entity_subtype=None,
    )

    call_count = 0

    mock_extractor = MagicMock()

    async def _extract_side_effect(chunks, doc_id, collection_name):
        nonlocal call_count
        call_count += 1
        return GraphExtractionResult(
            nodes=[node_a],
            edges=[],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )

    mock_extractor.extract = _extract_side_effect

    graph_config_on = GraphConfig(enabled=True, backend_threshold_edges=10_000)
    pipeline_on = _make_pipeline_with_graph(store, mock_extractor, graph_store, graph_config_on)

    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Test\n\nAuthService here.\n")

    result = await pipeline_on.ingest_file(doc_file, collection, embedder=_make_embedder())
    assert result.status == "ok"
    assert call_count == 1
    assert await graph_store.node_count(collection) == 1

    # Second ingest: pipeline without graph components (graph disabled)
    pipeline_off = _make_pipeline_with_graph(store, None, None, GraphConfig(enabled=False))

    result2 = await pipeline_off.ingest_file(doc_file, collection, embedder=_make_embedder())
    assert result2.status == "ok"
    # Extractor was NOT called on the second ingest
    assert call_count == 1

    # Tables from first ingest still have the node
    assert await graph_store.node_count(collection) == 1

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_ingest_two_docs_merges_graph(tmp_path: Path):
    """Two ingests with a stub extractor: graph merges nodes by stable ID."""
    from archon_search.store import SearchStore
    from archon_search.graph_types import EntityType, RelationshipType, make_stable_entity_id, make_stable_edge_id

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_merge_graph"

    def _make_node(name, doc_id):
        return GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, name.lower()),
            entity_name=name,
            entity_type=EntityType.concept,
            source_doc_id=doc_id,
            collection_name=collection,
            entity_subtype=None,
        )

    def _make_edge(src_node, tgt_node, doc_id):
        return GraphEdge(
            id=make_stable_edge_id(src_node.id, tgt_node.id, RelationshipType.related_to.value),
            source_node_id=src_node.id,
            target_node_id=tgt_node.id,
            relationship_type=RelationshipType.related_to,
            source_doc_id=doc_id,
        )

    # doc1: AuthService + TokenValidator
    doc1_auth = _make_node("AuthService", "doc1")
    doc1_token = _make_node("TokenValidator", "doc1")
    edge1 = _make_edge(doc1_auth, doc1_token, "doc1")

    # doc2: AuthService (same stable ID) + UserStore
    doc2_auth = _make_node("AuthService", "doc2")
    doc2_user = _make_node("UserStore", "doc2")
    edge2 = _make_edge(doc2_auth, doc2_user, "doc2")

    results_per_doc = [
        GraphExtractionResult(
            nodes=[doc1_auth, doc1_token],
            edges=[edge1],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        ),
        GraphExtractionResult(
            nodes=[doc2_auth, doc2_user],
            edges=[edge2],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        ),
    ]

    call_idx = 0

    mock_extractor = MagicMock()

    async def _extract(chunks, doc_id, collection_name):
        nonlocal call_idx
        result = results_per_doc[call_idx]
        call_idx += 1
        return result

    mock_extractor.extract = _extract

    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    # Ingest doc1
    doc1_file = tmp_path / "doc1.md"
    doc1_file.write_text("# Doc1\n\nAuthService and TokenValidator.\n")

    pipeline1 = _make_pipeline_with_graph(store, mock_extractor, graph_store, graph_config)
    result1 = await pipeline1.ingest_file(doc1_file, collection, embedder=_make_embedder())
    assert result1.status == "ok"

    # Ingest doc2
    doc2_file = tmp_path / "doc2.md"
    doc2_file.write_text("# Doc2\n\nAuthService and UserStore.\n")

    pipeline2 = _make_pipeline_with_graph(store, mock_extractor, graph_store, graph_config)
    result2 = await pipeline2.ingest_file(doc2_file, collection, embedder=_make_embedder())
    assert result2.status == "ok"

    # AuthService is merged (same stable ID) → 3 unique nodes total
    node_count = await graph_store.node_count(collection)
    assert node_count == 3, f"Expected 3 unique nodes (AuthService, TokenValidator, UserStore), got {node_count}"

    # 2 unique edges
    edge_count = await graph_store.edge_count(collection)
    assert edge_count == 2, f"Expected 2 edges, got {edge_count}"

    # Verify AuthService (merged node) has both TokenValidator and UserStore as neighbours
    from archon_search.graph_types import EntityType, make_stable_entity_id
    auth_id = make_stable_entity_id(EntityType.concept.value, "authservice")
    neighbours = await graph_store.get_neighbours(collection, [auth_id])
    neighbour_names = {n.entity_name for n in neighbours}
    assert "TokenValidator" in neighbour_names, f"Expected TokenValidator in AuthService neighbours, got: {neighbour_names}"
    assert "UserStore" in neighbour_names, f"Expected UserStore in AuthService neighbours, got: {neighbour_names}"

    await store.disconnect()
    await graph_store.disconnect()

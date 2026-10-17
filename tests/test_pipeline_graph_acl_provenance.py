"""Graph-sourced results must carry the stored ACL provenance like hybrid ones.

Every graph path — global / local / PPR search, their ``search_many`` fan-out, and the
``explain`` variants — turns raw LanceDB rows into candidates through the shared
``store.row_to_scored_candidate``. Each path must surface ``acl_source``,
``acl_sidecar_path`` and ``acl_warning`` from the stored row, or ``acl_gate.source`` is
null and ``acl_gate.warnings`` empty for graph results even when the row records sidecar
provenance and fail-open warnings. Builder-level coverage lives in
``tests/test_store_row_to_candidate.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from archon_search.collection_meta import CollectionMeta
from archon_search.config import GraphConfig
from archon_search.embedder import Embedder
from archon_search.graph_types import Community, EntityType, GraphNode
from archon_search.pipeline import SearchPipeline
from archon_search.ppr_walker import PPRWalkResult

_COLLECTION = "graph-acl-prov"
_SIDECAR_SOURCE = "sidecar"
_SIDECAR_PATH = "docs/secret.md.acl"
_WARNINGS = ["acl sidecar entry 'deny-all' is not a valid namespace; ignored"]
_SCOPES = ["user:alice"]
_ENTITY_ID = "entity-1"
_EXPECTED_PROVENANCE = (_SIDECAR_SOURCE, _SIDECAR_PATH, _WARNINGS)


def _provenance_row(chunk_id: str = "chunk-1", **overrides: Any) -> dict[str, Any]:
    """A raw LanceDB chunk row carrying G15 ACL provenance and scope tags."""
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "source_path": "/docs/secret.md",
        "text": "graph sourced text",
        "doc_id": "doc-1",
        "namespace": "default",
        "chunk_index": 0,
        "total_chunks": 1,
        "language": "en",
        "acl": ["default"],
        "scopes": list(_SCOPES),
        "acl_source": _SIDECAR_SOURCE,
        "acl_sidecar_path": _SIDECAR_PATH,
        "acl_warning": list(_WARNINGS),
        "metadata": "{}",
        "ingested_by": "cli",
        "file_type": "md",
        "indexed_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }
    row.update(overrides)
    return row


def _make_pipeline(*, graph_store, ppr_walker=None) -> tuple[SearchPipeline, Embedder]:
    """A pipeline over a mocked store whose graph rows carry provenance; returns (pipeline, embedder)."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.reranker import Reranker

    class _EmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    class _RerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    store = MagicMock()
    store.get_chunks_by_ids = AsyncMock(return_value=[_provenance_row()])
    store.hybrid_search_with_trace = AsyncMock(return_value=[])
    embedder = Embedder(_EmbedderBackend())
    reranker = Reranker(_RerankerBackend())
    reranker.rerank_candidates = AsyncMock(side_effect=lambda q, cands, top_k: cands[:top_k])

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_store=graph_store,
        graph_config=GraphConfig(enabled=True, max_global_candidates=100),
        ppr_walker=ppr_walker,
    )
    return pipeline, embedder


def _community() -> Community:
    return Community(
        community_id="comm-1",
        entity_ids=[_ENTITY_ID],
        representative_chunk_ids=["chunk-1"],
        built_at=datetime(2024, 1, 1, tzinfo=UTC),
        summary_text=None,
    )


def _global_graph_store() -> MagicMock:
    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=[_community()])
    return graph_store


def _local_graph_store() -> MagicMock:
    """A graph store whose query entity belongs to one community with a provenance row."""
    graph_store = MagicMock()
    graph_store.find_nodes_by_name = AsyncMock(return_value=[
        GraphNode(
            id=_ENTITY_ID, entity_name="PaymentService", entity_type=EntityType.system,
            source_doc_id="doc-1", collection_name=_COLLECTION,
        )
    ])
    graph_store.communities_table_exists = AsyncMock(return_value=True)
    graph_store.get_communities_for_entities = AsyncMock(return_value=[_community()])
    return graph_store


def _ppr_walker(chunk_ids: list[str] | None = None) -> MagicMock:
    walker = MagicMock()
    walker.walk = AsyncMock(
        return_value=PPRWalkResult(entity_ids=[_ENTITY_ID], chunk_ids=chunk_ids or ["chunk-1"], entities_matched=1)
    )
    return walker


def _fanout_meta(embedder: Embedder) -> list[MagicMock]:
    meta = MagicMock(spec=CollectionMeta)
    meta.name = _COLLECTION
    meta.active_embedding_model = embedder.model_name
    return [meta]


def _provenance_of(result) -> tuple[Any, Any, Any]:
    assert result.results, "graph path returned no results; cannot check provenance"
    r = result.results[0]
    return r.acl_source, r.acl_sidecar_path, r.acl_warning


def _explain_provenance_of(result) -> tuple[Any, Any, Any]:
    graph_results = [c for c in result.top_results if c.graph_provenance is not None]
    assert graph_results, "explain returned no graph-sourced candidates; cannot check provenance"
    c = graph_results[0]
    return c.acl_source, c.acl_sidecar_path, c.acl_warning


# ---------------------------------------------------------------------------
# search: user-visible SearchResult provenance on graph paths
# ---------------------------------------------------------------------------


async def test_global_graph_search_results_carry_acl_provenance() -> None:
    """graph_mode='global' (single collection) surfaces the row's ACL provenance."""
    pipeline, embedder = _make_pipeline(graph_store=_global_graph_store())

    result = await pipeline.search("query", _COLLECTION, embedder=embedder, graph_mode="global")

    assert result.graph_expansion_applied is True
    assert _provenance_of(result) == _EXPECTED_PROVENANCE


async def test_local_graph_search_results_carry_acl_provenance() -> None:
    """graph_mode='local' (single collection) surfaces the community row's ACL provenance."""
    pipeline, embedder = _make_pipeline(graph_store=_local_graph_store())

    result = await pipeline.search("PaymentService", _COLLECTION, embedder=embedder, graph_mode="local")

    assert result.graph_expansion_applied is True
    assert _provenance_of(result) == _EXPECTED_PROVENANCE


async def test_ppr_graph_search_results_carry_acl_provenance() -> None:
    """graph_mode='ppr' surfaces the PPR row's ACL provenance."""
    pipeline, embedder = _make_pipeline(graph_store=MagicMock(), ppr_walker=_ppr_walker())

    result = await pipeline.search("query", _COLLECTION, embedder=embedder, graph_mode="ppr")

    assert result.ppr_entities_matched == 1
    assert _provenance_of(result) == _EXPECTED_PROVENANCE


async def test_search_many_global_graph_results_carry_acl_provenance() -> None:
    """graph_mode='global' multi-collection fan-out surfaces the row's ACL provenance."""
    pipeline, embedder = _make_pipeline(graph_store=_global_graph_store())
    pipeline.get_all_collections_meta = AsyncMock(return_value=_fanout_meta(embedder))

    result = await pipeline.search_many("query", [_COLLECTION], graph_mode="global")

    assert result.graph_expansion_applied is True
    assert _provenance_of(result) == _EXPECTED_PROVENANCE


async def test_search_many_local_graph_results_carry_acl_provenance() -> None:
    """graph_mode='local' multi-collection fan-out surfaces the community row's ACL provenance."""
    pipeline, embedder = _make_pipeline(graph_store=_local_graph_store())
    pipeline.get_all_collections_meta = AsyncMock(return_value=_fanout_meta(embedder))

    result = await pipeline.search_many("PaymentService", [_COLLECTION], graph_mode="local")

    assert result.graph_expansion_applied is True
    assert _provenance_of(result) == _EXPECTED_PROVENANCE


# ---------------------------------------------------------------------------
# explain: graph-sourced candidates feed acl_gate
# ---------------------------------------------------------------------------


async def test_global_graph_explain_candidates_carry_acl_provenance() -> None:
    pipeline, embedder = _make_pipeline(graph_store=_global_graph_store())

    result = await pipeline.explain("query", _COLLECTION, embedder=embedder, graph_mode="global")

    assert _explain_provenance_of(result) == _EXPECTED_PROVENANCE


async def test_local_graph_explain_candidates_carry_acl_provenance() -> None:
    pipeline, embedder = _make_pipeline(graph_store=_local_graph_store())

    result = await pipeline.explain("PaymentService", _COLLECTION, embedder=embedder, graph_mode="local")

    assert _explain_provenance_of(result) == _EXPECTED_PROVENANCE


async def test_ppr_graph_explain_candidates_carry_acl_provenance() -> None:
    graph_store = MagicMock()
    graph_store.get_nodes_by_ids = AsyncMock(return_value={})
    pipeline, embedder = _make_pipeline(graph_store=graph_store, ppr_walker=_ppr_walker())

    result = await pipeline.explain("query", _COLLECTION, embedder=embedder, graph_mode="ppr")

    assert result.ppr_entities_matched == 1
    assert _explain_provenance_of(result) == _EXPECTED_PROVENANCE


# ---------------------------------------------------------------------------
# Enforcement: principal ACL still gates graph-sourced candidates
# ---------------------------------------------------------------------------


async def test_ppr_graph_search_drops_chunk_whose_acl_excludes_namespace() -> None:
    """A PPR row ACL'd to another namespace never reaches the results (acl_filtered=True)."""
    pipeline, embedder = _make_pipeline(graph_store=MagicMock(), ppr_walker=_ppr_walker(["chunk-1", "chunk-2"]))
    pipeline.store.get_chunks_by_ids = AsyncMock(
        return_value=[_provenance_row("chunk-1"), _provenance_row("chunk-2", acl=["other-team"])]
    )

    result = await pipeline.search("query", _COLLECTION, embedder=embedder, graph_mode="ppr")

    assert [r.chunk_id for r in result.results] == ["chunk-1"]
    assert result.acl_filtered is True

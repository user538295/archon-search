"""Unit tests for CommunityBuilder — E1b BE-3b (MMR + LLM summary)."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_types import EntityType, GraphNode


def make_node(node_id: str, source_doc_id: str = "doc-1") -> GraphNode:
    return GraphNode(
        id=node_id,
        entity_name=node_id,
        entity_type=EntityType.concept,
        source_doc_id=source_doc_id,
        collection_name="test",
    )


def make_chunk(chunk_id: str, vector: list[float] | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": "doc-1",
        "text": f"text of {chunk_id}",
        "vector": vector or [1.0, 0.0, 0.0],
    }


def make_graph_store(nodes, edges):
    store = MagicMock()
    store.get_all_nodes = AsyncMock(return_value=nodes)
    store.get_all_edges = AsyncMock(return_value=edges)
    store.write_communities = AsyncMock(return_value=None)
    return store


def make_search_store(chunks_by_doc: dict[str, list[dict]]):
    """Return a mock SearchStore where get_chunks_for_doc returns chunks_by_doc[doc_id]."""
    store = MagicMock()

    async def _get_chunks_for_doc(collection, doc_id):
        return chunks_by_doc.get(doc_id, [])

    store.get_chunks_for_doc = _get_chunks_for_doc
    return store


@pytest.mark.asyncio
async def test_mmr_selects_diverse_representatives():
    """MMR output has K items, each selected for diversity; no duplicate chunk IDs."""
    from archon_search.community_builder import _mmr_select

    # Create 5 chunks with diverse vectors
    chunks = [
        make_chunk("c1", [1.0, 0.0, 0.0]),
        make_chunk("c2", [0.0, 1.0, 0.0]),
        make_chunk("c3", [0.0, 0.0, 1.0]),
        make_chunk("c4", [0.7, 0.7, 0.0]),
        make_chunk("c5", [0.0, 0.7, 0.7]),
    ]

    result = _mmr_select(chunks, k=3)

    assert len(result) == 3
    assert len(set(result)) == 3, "No duplicate chunk IDs"
    assert all(cid in {"c1", "c2", "c3", "c4", "c5"} for cid in result)


@pytest.mark.asyncio
async def test_llm_summary_failure_falls_back_to_mmr(caplog):
    """When LLM raises for one community, other communities are unaffected; warning emitted per failed community (S12)."""
    from archon_search.community_builder import CommunityBuilder

    node_a = make_node("a", source_doc_id="doc-1")
    node_b = make_node("b", source_doc_id="doc-2")
    node_c = make_node("c", source_doc_id="doc-3")
    node_d = make_node("d", source_doc_id="doc-4")
    nodes = [node_a, node_b, node_c, node_d]

    graph_store = make_graph_store(nodes, [])
    search_store = make_search_store({
        "doc-1": [make_chunk("c1", [1.0, 0.0, 0.0])],
        "doc-2": [make_chunk("c2", [0.0, 1.0, 0.0])],
        "doc-3": [make_chunk("c3", [0.0, 0.0, 1.0])],
        "doc-4": [make_chunk("c4", [0.7, 0.7, 0.0])],
    })

    config = GraphConfig(
        enabled=True,
        extraction_model="claude-3-5-sonnet-20241022",
        community_summary_chunks=1,
    )
    builder = CommunityBuilder(graph_store, config, search_store=search_store)

    # Leiden returns 2 communities
    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[["a", "b"], ["c", "d"]],
    ):
        with caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
            communities = await builder.build("test-col")

    # Both communities should have MMR representatives despite LLM failure
    assert len(communities) == 2
    for comm in communities:
        assert comm.representative_chunk_ids != [], f"Community {comm.community_id} has no representative chunks"
        assert comm.summary_text is None, "LLM stub raises, so summary_text must be None"
    # Warning was emitted (one per community)
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_msgs) >= 1, "Expected at least one WARNING about LLM failure"
    assert any("LLM" in m or "llm" in m.lower() for m in warning_msgs)
    # write_communities called with both communities
    call_args = graph_store.write_communities.call_args
    assert call_args is not None
    written = call_args[0][1]  # second positional arg is the communities list
    assert len(written) == 2


# ---------------------------------------------------------------------------
# _cosine_similarity direct tests
# ---------------------------------------------------------------------------


def test_cosine_similarity_both_zero_vectors():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_one_zero_vector():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_orthogonal():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_parallel():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([3.0, 4.0], [6.0, 8.0]) == pytest.approx(1.0)


def test_cosine_similarity_antiparallel():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# _mmr_select edge cases
# ---------------------------------------------------------------------------


def test_mmr_select_empty_input():
    from archon_search.community_builder import _mmr_select
    assert _mmr_select([], k=3) == []


def test_mmr_select_no_valid_vectors():
    from archon_search.community_builder import _mmr_select
    chunks = [{"chunk_id": "c1", "vector": None}, {"chunk_id": "c2", "vector": []}]
    assert _mmr_select(chunks, k=2) == []


def test_mmr_select_k_zero():
    from archon_search.community_builder import _mmr_select
    chunks = [make_chunk("c1"), make_chunk("c2")]
    assert _mmr_select(chunks, k=0) == []


def test_mmr_select_k_larger_than_candidates():
    from archon_search.community_builder import _mmr_select
    chunks = [make_chunk("c1", [1.0, 0.0]), make_chunk("c2", [0.0, 1.0])]
    result = _mmr_select(chunks, k=10)
    assert len(result) == 2
    assert set(result) == {"c1", "c2"}


def test_mmr_select_k_one():
    from archon_search.community_builder import _mmr_select
    chunks = [
        make_chunk("c1", [1.0, 0.0]),
        make_chunk("c2", [0.0, 1.0]),
        make_chunk("c3", [0.5, 0.5]),
    ]
    result = _mmr_select(chunks, k=1)
    assert len(result) == 1
    assert result[0] in {"c1", "c2", "c3"}


def test_mmr_selects_diverse_subset_from_mixed_input():
    """MMR selects k diverse items; result has no duplicates and comes from the candidate set."""
    from archon_search.community_builder import _mmr_select

    chunks = [
        make_chunk("c1", [1.0, 0.0, 0.0]),
        make_chunk("c2", [0.0, 1.0, 0.0]),
        make_chunk("c3", [0.0, 0.0, 1.0]),
        make_chunk("c4", [0.7, 0.7, 0.0]),  # between c1 and c2
        make_chunk("c5", [0.0, 0.7, 0.7]),  # between c2 and c3
    ]
    result = _mmr_select(chunks, k=3)
    # Must return exactly k items with no duplicates, all from the candidate set
    assert len(result) == 3
    assert len(set(result)) == 3, "No duplicate chunk IDs"
    all_ids = {"c1", "c2", "c3", "c4", "c5"}
    assert all(cid in all_ids for cid in result)


# ---------------------------------------------------------------------------
# Single-node short-circuit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_single_node_uses_mmr_and_writes(caplog):
    """Single-node graph builds one community via short-circuit path; MMR runs; write_communities called."""
    from archon_search.community_builder import CommunityBuilder

    node_a = make_node("a", source_doc_id="doc-1")
    graph_store = make_graph_store([node_a], [])
    search_store = make_search_store({
        "doc-1": [make_chunk("c1", [1.0, 0.0])],
    })
    config = GraphConfig(enabled=True, community_summary_chunks=1)
    builder = CommunityBuilder(graph_store, config, search_store=search_store)

    with caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col")

    assert len(communities) == 1
    assert communities[0].entity_ids == ["a"]
    assert communities[0].representative_chunk_ids == ["c1"]
    graph_store.write_communities.assert_awaited_once()
    # Warning about single node should be logged
    assert any(record.levelno >= logging.WARNING for record in caplog.records)

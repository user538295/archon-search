"""Unit and integration tests for GraphStore GC methods — E2d BE-5.

Tests verify:
- GcPassResult.communities_invalidated is computed correctly via __post_init__
- delete_orphan_nodes_and_edges removes nodes with zero remaining mentions
- delete_orphan_nodes_and_edges removes edges whose endpoints were deleted
- delete_orphan_nodes_and_edges preserves nodes that still have mentions
- delete_orphan_nodes_and_edges preserves edges between live nodes
- prune_stale_mentions deletes mention rows whose chunk_id is absent from live set
- count_stale_mentions counts stale rows without deleting them
- Full GC cycle (real LanceDB in tmp_path)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pyarrow as pa
import pytest

from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GcPassResult,
    GraphEdge,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COL = "test-col"
_NS = "default"


def _node(name: str, entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc-abc",
        collection_name=_COL,
    )


def _edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, RelationshipType.uses.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.uses,
        source_doc_id="doc-abc",
    )


def _mention(entity_id: str, chunk_id: str, doc_id: str = "doc-abc") -> GraphMention:
    return GraphMention(entity_id=entity_id, chunk_id=chunk_id, doc_id=doc_id)


def _nodes_arrow(nodes: list[GraphNode]):
    """Build a PyArrow table of node rows."""
    return pa.table(
        {
            "id": [n.id for n in nodes],
            "entity_name": [n.entity_name for n in nodes],
            "entity_type": [n.entity_type.value for n in nodes],
            "source_doc_id": [n.source_doc_id for n in nodes],
            "collection_name": [n.collection_name for n in nodes],
            "entity_subtype": [n.entity_subtype for n in nodes],
            "name_embedding": pa.array(
                [n.name_embedding for n in nodes], type=pa.list_(pa.float32())
            ),
        },
        schema=GraphStore._nodes_schema(),
    )


def _edges_arrow(edges: list[GraphEdge]):
    """Build a PyArrow table of edge rows."""
    return pa.table(
        {
            "id": [e.id for e in edges],
            "source_node_id": [e.source_node_id for e in edges],
            "target_node_id": [e.target_node_id for e in edges],
            "relationship_type": [e.relationship_type.value for e in edges],
            "source_doc_id": [e.source_doc_id for e in edges],
            "extraction_method": pa.array(
                [e.extraction_method for e in edges], type=pa.utf8()
            ),
        },
        schema=GraphStore._edges_schema(),
    )


def _mentions_arrow(mentions: list[GraphMention]):
    """Build a PyArrow table of mention rows."""
    return pa.table(
        {
            "entity_id": [m.entity_id for m in mentions],
            "chunk_id": [m.chunk_id for m in mentions],
            "doc_id": [m.doc_id for m in mentions],
        },
        schema=GraphStore._mentions_schema(),
    )


# ---------------------------------------------------------------------------
# GcPassResult
# ---------------------------------------------------------------------------


def test_gc_pass_result_communities_invalidated_when_nodes_removed() -> None:
    """communities_invalidated is True when orphan_nodes_removed > 0."""
    result = GcPassResult(orphan_nodes_removed=3, orphan_edges_removed=1)
    assert result.communities_invalidated is True


def test_gc_pass_result_communities_invalidated_false_when_no_nodes_removed() -> None:
    """communities_invalidated is False when orphan_nodes_removed == 0."""
    result = GcPassResult(orphan_nodes_removed=0, orphan_edges_removed=5)
    assert result.communities_invalidated is False


def test_gc_pass_result_communities_invalidated_computed_correctly() -> None:
    """__post_init__ sets communities_invalidated based on orphan_nodes_removed; init=False."""
    # Verify communities_invalidated is NOT accepted as a constructor argument
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(GcPassResult)}
    assert "communities_invalidated" in fields
    assert fields["communities_invalidated"].init is False, (
        "communities_invalidated must be init=False (computed, not constructor arg)"
    )

    # Boundary: exactly 0 → False
    r0 = GcPassResult(orphan_nodes_removed=0, orphan_edges_removed=0)
    assert r0.communities_invalidated is False

    # Boundary: exactly 1 → True
    r1 = GcPassResult(orphan_nodes_removed=1, orphan_edges_removed=0)
    assert r1.communities_invalidated is True


# ---------------------------------------------------------------------------
# delete_orphan_nodes_and_edges — unit tests (mocked LanceDB)
# ---------------------------------------------------------------------------


def test_delete_orphan_nodes_removes_zero_mention_nodes() -> None:
    """Nodes with no mention rows must be deleted; count reflected in GcPassResult."""
    node_a = _node("EntityA")  # has a mention → keep
    node_b = _node("EntityB")  # no mention → orphan

    mention_a = _mention(node_a.id, "chunk-1")

    # Mentions table returns only EntityA
    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    # Nodes table returns both nodes
    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    # Edges table is empty (no edges to delete)
    edges_q = AsyncMock()
    edges_q.to_arrow = AsyncMock(return_value=_edges_arrow([]))
    edges_q.select = MagicMock(return_value=edges_q)
    mock_edges_table = MagicMock()
    mock_edges_table.query.return_value = edges_q
    mock_edges_table.schema = AsyncMock(return_value=GraphStore._edges_schema())
    mock_edges_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        if "edges" in name:
            return mock_edges_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_nodes_removed == 1, (
        f"Expected 1 orphan node removed; got {result.orphan_nodes_removed}"
    )
    # nodes delete was called with a predicate referencing node_b.id
    mock_nodes_table.delete.assert_called_once()
    node_delete_pred = mock_nodes_table.delete.call_args[0][0]
    assert node_b.id in node_delete_pred, (
        f"Delete predicate must contain orphan node ID; got {node_delete_pred!r}"
    )


def test_delete_orphan_edges_removed_with_nodes() -> None:
    """Edges whose endpoints include an orphan node must be deleted."""
    node_a = _node("EntityA")  # kept (has mention)
    node_b = _node("EntityB")  # orphan (no mention)
    edge_ab = _edge(node_a, node_b)  # must be deleted because node_b is orphan

    mention_a = _mention(node_a.id, "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    edges_q = AsyncMock()
    edges_q.to_arrow = AsyncMock(return_value=_edges_arrow([edge_ab]))
    edges_q.select = MagicMock(return_value=edges_q)
    mock_edges_table = MagicMock()
    mock_edges_table.query.return_value = edges_q
    mock_edges_table.schema = AsyncMock(return_value=GraphStore._edges_schema())
    mock_edges_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        if "edges" in name:
            return mock_edges_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_edges_removed == 1, (
        f"Expected 1 orphan edge removed; got {result.orphan_edges_removed}"
    )
    mock_edges_table.delete.assert_called_once()
    edge_delete_pred = mock_edges_table.delete.call_args[0][0]
    assert edge_ab.id in edge_delete_pred, (
        f"Delete predicate must contain orphan edge ID; got {edge_delete_pred!r}"
    )


def test_delete_orphan_nodes_preserves_nodes_with_remaining_mentions() -> None:
    """Nodes that still have mentions must NOT be deleted."""
    node_a = _node("EntityA")
    node_b = _node("EntityB")

    # Both nodes have mentions
    mention_a = _mention(node_a.id, "chunk-1")
    mention_b = _mention(node_b.id, "chunk-2")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a, mention_b]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    edges_q = AsyncMock()
    edges_q.to_arrow = AsyncMock(return_value=_edges_arrow([]))
    edges_q.select = MagicMock(return_value=edges_q)
    mock_edges_table = MagicMock()
    mock_edges_table.query.return_value = edges_q
    mock_edges_table.schema = AsyncMock(return_value=GraphStore._edges_schema())
    mock_edges_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        if "edges" in name:
            return mock_edges_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_nodes_removed == 0, (
        f"No nodes should be removed when all have mentions; got {result.orphan_nodes_removed}"
    )
    assert result.communities_invalidated is False
    # delete must not have been called on nodes (no predicate to build for empty orphan set)
    mock_nodes_table.delete.assert_not_called()


def test_delete_orphan_edges_preserves_edges_between_live_nodes() -> None:
    """Edges connecting two live nodes (both with mentions) must NOT be deleted."""
    node_a = _node("EntityA")
    node_b = _node("EntityB")
    edge_ab = _edge(node_a, node_b)

    mention_a = _mention(node_a.id, "chunk-1")
    mention_b = _mention(node_b.id, "chunk-2")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a, mention_b]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    edges_q = AsyncMock()
    edges_q.to_arrow = AsyncMock(return_value=_edges_arrow([edge_ab]))
    edges_q.select = MagicMock(return_value=edges_q)
    mock_edges_table = MagicMock()
    mock_edges_table.query.return_value = edges_q
    mock_edges_table.schema = AsyncMock(return_value=GraphStore._edges_schema())
    mock_edges_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        if "edges" in name:
            return mock_edges_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_edges_removed == 0, (
        f"No edges should be removed when both endpoints have mentions; got {result.orphan_edges_removed}"
    )
    mock_edges_table.delete.assert_not_called()


# ---------------------------------------------------------------------------
# prune_stale_mentions — unit tests (mocked LanceDB)
# ---------------------------------------------------------------------------


def test_prune_stale_mentions_removes_absent_chunk_ids() -> None:
    """Mention rows whose chunk_id is NOT in live_chunk_ids must be deleted."""
    entity_id = make_stable_entity_id("concept", "Alpha")
    live_mention = _mention(entity_id, "chunk-live")
    stale_mention = _mention(entity_id, "chunk-stale")

    # _fetch_stale_chunk_ids calls table.query().select(["chunk_id"]).to_arrow()
    # Make select() return the same query mock so .to_arrow() resolves correctly.
    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(
        return_value=_mentions_arrow([live_mention, stale_mention])
    )
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table = MagicMock()
    mock_table.query.return_value = mentions_q
    mock_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    count = asyncio.run(
        store.prune_stale_mentions(_COL, frozenset({"chunk-live"}), _NS)
    )

    assert count == 1, f"Expected 1 stale mention deleted; got {count}"
    mock_table.delete.assert_called_once()
    pred = mock_table.delete.call_args[0][0]
    assert "chunk-stale" in pred, (
        f"Delete predicate must reference stale chunk ID; got {pred!r}"
    )


def test_prune_stale_mentions_returns_zero_when_all_live() -> None:
    """prune_stale_mentions returns 0 and does not call delete when all mentions are live."""
    entity_id = make_stable_entity_id("concept", "Beta")
    live_mention = _mention(entity_id, "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([live_mention]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table = MagicMock()
    mock_table.query.return_value = mentions_q
    mock_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    count = asyncio.run(
        store.prune_stale_mentions(_COL, frozenset({"chunk-1"}), _NS)
    )

    assert count == 0, f"Expected 0 stale mentions; got {count}"
    mock_table.delete.assert_not_called()


# ---------------------------------------------------------------------------
# count_stale_mentions — unit tests (mocked LanceDB)
# ---------------------------------------------------------------------------


def test_count_stale_mentions_returns_correct_count() -> None:
    """count_stale_mentions counts mention rows whose chunk_id is NOT in live set."""
    entity_id = make_stable_entity_id("concept", "Gamma")
    live_m = _mention(entity_id, "chunk-live")
    stale_m1 = _mention(entity_id, "chunk-stale-1")
    stale_m2 = _mention(entity_id, "chunk-stale-2")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(
        return_value=_mentions_arrow([live_m, stale_m1, stale_m2])
    )
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table = MagicMock()
    mock_table.query.return_value = mentions_q
    mock_table.delete = AsyncMock(return_value=None)  # must NOT be called

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    count = asyncio.run(
        store.count_stale_mentions(_COL, frozenset({"chunk-live"}), _NS)
    )

    assert count == 2, f"Expected 2 stale mentions; got {count}"
    # count_stale_mentions must NOT delete any rows
    mock_table.delete.assert_not_called()


def test_count_stale_mentions_returns_zero_when_all_live() -> None:
    """count_stale_mentions returns 0 when all chunk_ids are in the live set."""
    entity_id = make_stable_entity_id("concept", "Delta")
    m1 = _mention(entity_id, "chunk-a")
    m2 = _mention(entity_id, "chunk-b")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([m1, m2]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table = MagicMock()
    mock_table.query.return_value = mentions_q
    mock_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    count = asyncio.run(
        store.count_stale_mentions(_COL, frozenset({"chunk-a", "chunk-b"}), _NS)
    )

    assert count == 0, f"Expected 0 stale mentions; got {count}"
    mock_table.delete.assert_not_called()


# ---------------------------------------------------------------------------
# count_stale_mentions — missing table
# ---------------------------------------------------------------------------


def test_count_stale_mentions_returns_zero_when_table_absent() -> None:
    """count_stale_mentions returns 0 when the mentions table does not exist."""
    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=FileNotFoundError("no table"))
    store._db = mock_db

    count = asyncio.run(
        store.count_stale_mentions(_COL, frozenset({"chunk-x"}), _NS)
    )

    assert count == 0, f"Expected 0 for absent table; got {count}"


def test_prune_stale_mentions_returns_zero_when_table_absent() -> None:
    """prune_stale_mentions returns 0 without error when the mentions table does not exist."""
    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=FileNotFoundError("no table"))
    store._db = mock_db

    count = asyncio.run(
        store.prune_stale_mentions(_COL, frozenset({"chunk-x"}), _NS)
    )

    assert count == 0, f"Expected 0 for absent table; got {count}"


# ---------------------------------------------------------------------------
# Integration: real LanceDB in tmp_path
# ---------------------------------------------------------------------------


def test_graph_gc_methods_real_lancedb(tmp_path: Path) -> None:
    """End-to-end GC using real LanceDB in tmp_path.

    Scenario:
    - node_a and node_b are written to the graph
    - edge_ab connects them
    - Only node_a has a mention; node_b has no mention (is orphan)
    - After delete_orphan_nodes_and_edges: node_b removed, edge_ab removed
    - prune_stale_mentions: chunk-stale mention deleted (chunk-live survives)
    - count_stale_mentions: returns correct count before and after prune
    """

    async def _run() -> None:
        store = GraphStore(tmp_path)
        await store.connect()

        node_a = _node("EntityA")
        node_b = _node("EntityB")  # orphan — no mention
        edge_ab = _edge(node_a, node_b)  # will be deleted with node_b

        # Seed graph tables
        await store.ensure_graph_tables(_COL, ns=_NS)
        await store.write_graph(_COL, [node_a, node_b], [edge_ab], ns=_NS)

        # Write mentions only for node_a
        mention_a_live = GraphMention(entity_id=node_a.id, chunk_id="chunk-live", doc_id="doc-1")
        mention_a_stale = GraphMention(entity_id=node_a.id, chunk_id="chunk-stale", doc_id="doc-1")
        await store.write_mentions(_COL, [mention_a_live, mention_a_stale], ns=_NS)

        # --- count_stale_mentions before prune ---
        live_chunks: frozenset[str] = frozenset({"chunk-live"})
        stale_count_before = await store.count_stale_mentions(_COL, live_chunks, _NS)
        assert stale_count_before == 1, (
            f"Expected 1 stale mention before prune; got {stale_count_before}"
        )

        # --- prune_stale_mentions ---
        pruned = await store.prune_stale_mentions(_COL, live_chunks, _NS)
        assert pruned == 1, f"Expected 1 mention pruned; got {pruned}"

        # count after prune should be 0
        stale_count_after = await store.count_stale_mentions(_COL, live_chunks, _NS)
        assert stale_count_after == 0, (
            f"Expected 0 stale mentions after prune; got {stale_count_after}"
        )

        # --- delete_orphan_nodes_and_edges ---
        gc_result = await store.delete_orphan_nodes_and_edges(_COL, _NS)

        assert gc_result.orphan_nodes_removed == 1, (
            f"Expected 1 orphan node removed; got {gc_result.orphan_nodes_removed}"
        )
        assert gc_result.orphan_edges_removed == 1, (
            f"Expected 1 orphan edge removed (edge_ab); got {gc_result.orphan_edges_removed}"
        )
        assert gc_result.communities_invalidated is True

        # Verify node_a survives
        remaining_nodes = await store.get_all_nodes(_COL, ns=_NS)
        remaining_ids = {n.id for n in remaining_nodes}
        assert node_a.id in remaining_ids, "node_a must survive GC (has mention)"
        assert node_b.id not in remaining_ids, "node_b must be deleted by GC (no mention)"

        # Verify edge_ab was deleted
        remaining_edges = await store.get_all_edges(_COL, ns=_NS)
        remaining_edge_ids = {e.id for e in remaining_edges}
        assert edge_ab.id not in remaining_edge_ids, "edge_ab must be deleted with its orphan endpoint"

        await store.disconnect()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fix C1-I-1: empty mentions table → safe early-exit (no whole-graph deletion)
# ---------------------------------------------------------------------------


def test_delete_orphan_nodes_returns_zero_when_mentions_table_empty_but_exists(caplog) -> None:
    """Empty mentions table must NOT trigger deletion — return GcPassResult(0, 0)."""
    import logging

    node_a = _node("EntityA")
    node_b = _node("EntityB")

    # Mentions table exists but has zero rows
    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    # Nodes table has two nodes
    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result == GcPassResult(orphan_nodes_removed=0, orphan_edges_removed=0), (
        f"Expected GcPassResult(0, 0) for empty mentions table; got {result}"
    )
    assert "zero rows" in caplog.text or "mentions table exists but has" in caplog.text
    mock_nodes_table.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Fix C1-A-1/C1-I-2: absent nodes table → safe early-exit
# ---------------------------------------------------------------------------


def test_delete_orphan_nodes_returns_zero_when_nodes_table_absent() -> None:
    """Absent nodes table must not crash — return GcPassResult(0, 0)."""
    entity_id = make_stable_entity_id("concept", "Alpha")
    mention = _mention(entity_id, "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        # nodes and edges tables absent
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result == GcPassResult(orphan_nodes_removed=0, orphan_edges_removed=0), (
        f"Expected GcPassResult(0, 0) when nodes table absent; got {result}"
    )


# ---------------------------------------------------------------------------
# Fix C1-A-1/C1-I-2: absent edges table → orphan nodes still deleted
# ---------------------------------------------------------------------------


def test_delete_orphan_nodes_handles_absent_edges_table() -> None:
    """Absent edges table must not prevent orphan node deletion; orphan_edges_removed=0."""
    node_a = _node("EntityA")  # has mention → keep
    node_b = _node("EntityB")  # no mention → orphan

    mention_a = _mention(node_a.id, "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        # edges table absent
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_nodes_removed == 1, (
        f"Expected 1 orphan node removed; got {result.orphan_nodes_removed}"
    )
    assert result.orphan_edges_removed == 0, (
        f"Expected 0 orphan edges (table absent); got {result.orphan_edges_removed}"
    )
    mock_nodes_table.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Fix C1-A-6/C1-B-2: 3 stale + 2 live — count then prune then count again
# (integration test using real LanceDB so state persists across calls)
# ---------------------------------------------------------------------------


def test_count_stale_then_prune_3_stale_2_live(tmp_path: Path) -> None:
    """3 stale + 2 live rows: count==3, after prune count==0."""

    async def _run() -> None:
        store = GraphStore(tmp_path)
        await store.connect()

        entity_id = make_stable_entity_id("concept", "Epsilon")
        mentions = [
            GraphMention(entity_id=entity_id, chunk_id="stale-1", doc_id="doc-1"),
            GraphMention(entity_id=entity_id, chunk_id="stale-2", doc_id="doc-1"),
            GraphMention(entity_id=entity_id, chunk_id="stale-3", doc_id="doc-1"),
            GraphMention(entity_id=entity_id, chunk_id="live-1", doc_id="doc-1"),
            GraphMention(entity_id=entity_id, chunk_id="live-2", doc_id="doc-1"),
        ]
        await store.ensure_graph_tables(_COL, ns=_NS)
        await store.write_mentions(_COL, mentions, ns=_NS)

        live_chunks: frozenset[str] = frozenset({"live-1", "live-2"})

        count_before = await store.count_stale_mentions(_COL, live_chunks, _NS)
        assert count_before == 3, f"Expected 3 stale rows before prune; got {count_before}"

        pruned = await store.prune_stale_mentions(_COL, live_chunks, _NS)
        assert pruned == 3, f"Expected 3 rows pruned; got {pruned}"

        count_after = await store.count_stale_mentions(_COL, live_chunks, _NS)
        assert count_after == 0, f"Expected 0 stale rows after prune; got {count_after}"

        await store.disconnect()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# C2-T-1: prune_stale_mentions with duplicate chunk_ids returns raw row count
# ---------------------------------------------------------------------------


def test_prune_stale_mentions_duplicate_chunk_ids_returns_raw_row_count() -> None:
    """Return value is raw row count (2), not unique chunk count (1), when duplicate rows exist."""
    entity_id = make_stable_entity_id("concept", "Zeta")

    # 2 rows share the same stale chunk_id; 1 row has a live chunk_id
    mentions_arrow_data = _mentions_arrow([
        _mention(entity_id, "chunk-stale"),
        _mention(entity_id, "chunk-stale"),  # duplicate
        _mention(entity_id, "chunk-live"),
    ])

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=mentions_arrow_data)
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table = MagicMock()
    mock_table.query.return_value = mentions_q
    mock_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    count = asyncio.run(
        store.prune_stale_mentions(_COL, frozenset({"chunk-live"}), _NS)
    )

    # Raw row count is 2 (not unique chunk count which is 1)
    assert count == 2, f"Expected raw row count 2; got {count}"

    # delete was called with a 1-element predicate (deduplicated: only 'chunk-stale')
    mock_table.delete.assert_called_once()
    pred = mock_table.delete.call_args[0][0]
    assert "chunk-stale" in pred, (
        f"Delete predicate must reference stale chunk ID; got {pred!r}"
    )
    assert pred.count("chunk-stale") == 1, (
        f"Predicate must deduplicate chunk IDs (only one occurrence); got {pred!r}"
    )


# ---------------------------------------------------------------------------
# C2-T-2: delete_orphan_nodes_and_edges — absent mentions table
# ---------------------------------------------------------------------------


def test_delete_orphan_nodes_handles_absent_mentions_table() -> None:
    """Absent mentions table (FileNotFoundError on open) must return GcPassResult(0, 0)."""
    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()

    async def _open(name: str):
        if "mentions" in name:
            raise FileNotFoundError("mentions table gone")
        raise AssertionError(f"Should not open table: {name}")

    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result == GcPassResult(orphan_nodes_removed=0, orphan_edges_removed=0), (
        f"Expected GcPassResult(0, 0) when mentions table absent; got {result}"
    )


# ---------------------------------------------------------------------------
# C2-T-3: prune_stale_mentions — second open_table call fails
# ---------------------------------------------------------------------------


def test_prune_stale_mentions_second_open_fails_returns_zero() -> None:
    """If the second open_table call (for the actual delete) raises FileNotFoundError, return 0."""
    entity_id = make_stable_entity_id("concept", "Eta")
    stale_mention = _mention(entity_id, "chunk-stale")

    # First open_table call (inside _fetch_stale_chunk_ids) returns a table with stale rows
    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([stale_mention]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table_first = MagicMock()
    mock_table_first.query.return_value = mentions_q

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    # First call succeeds, second call raises FileNotFoundError
    mock_db.open_table.side_effect = [
        mock_table_first,
        FileNotFoundError("table gone on second open"),
    ]
    store._db = mock_db

    count = asyncio.run(
        store.prune_stale_mentions(_COL, frozenset(), _NS)
    )

    assert count == 0, f"Expected 0 when second open_table fails; got {count}"


# ---------------------------------------------------------------------------
# C3-T-1a: delete_orphan_nodes_and_edges — batching with 501 orphan nodes
# ---------------------------------------------------------------------------


def test_delete_orphan_nodes_batches_large_delete() -> None:
    """501 orphan nodes trigger 2 delete batches (500 + 1) on the nodes table."""
    mentioned_node = _node("EntityA")  # has mention → kept
    orphan_nodes = [_node(f"Orphan{i}") for i in range(501)]

    mention_a = _mention(mentioned_node.id, "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    all_nodes = [mentioned_node] + orphan_nodes
    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow(all_nodes))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        # edges table absent — after C3-M-1 fix, open failure is isolated
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_nodes_removed == 501, (
        f"Expected 501 orphan nodes removed; got {result.orphan_nodes_removed}"
    )
    assert mock_nodes_table.delete.call_count == 2, (
        f"Expected 2 delete batches for 501 orphan nodes; got {mock_nodes_table.delete.call_count}"
    )
    # First call predicate must contain 500 IDs, second must contain 1.
    # _where_in builds: id IN ('id1', 'id2', ...) — split on "', '" gives N segments for N IDs.
    first_pred = mock_nodes_table.delete.call_args_list[0][0][0]
    second_pred = mock_nodes_table.delete.call_args_list[1][0][0]
    first_id_count = len(first_pred.split("', '"))
    second_id_count = len(second_pred.split("', '"))
    assert first_id_count == 500, (
        f"First batch must reference 500 IDs; counted {first_id_count} in {first_pred[:200]!r}"
    )
    assert second_id_count == 1, (
        f"Second batch must reference 1 ID; counted {second_id_count} in {second_pred!r}"
    )


# ---------------------------------------------------------------------------
# C3-T-1b: prune_stale_mentions — batching with 501 stale chunk_ids
# ---------------------------------------------------------------------------


def test_prune_stale_mentions_batches_large_delete() -> None:
    """501 stale chunk_ids trigger 2 delete batches (500 + 1) on the mentions table."""
    entity_id = make_stable_entity_id("concept", "Theta")
    stale_mentions = [_mention(entity_id, f"chunk-stale-{i}") for i in range(501)]
    live_mention = _mention(entity_id, "chunk-live")
    all_mentions = stale_mentions + [live_mention]

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow(all_mentions))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_table_first = MagicMock()
    mock_table_first.query.return_value = mentions_q

    mock_table_second = MagicMock()
    mock_table_second.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)
    mock_db = AsyncMock()
    # _fetch_stale_chunk_ids opens the table once, prune_stale_mentions opens it again
    mock_db.open_table.side_effect = [mock_table_first, mock_table_second]
    store._db = mock_db

    count = asyncio.run(
        store.prune_stale_mentions(_COL, frozenset({"chunk-live"}), _NS)
    )

    assert count == 501, f"Expected raw row count 501; got {count}"
    assert mock_table_second.delete.call_count == 2, (
        f"Expected 2 delete batches for 501 stale chunk_ids; got {mock_table_second.delete.call_count}"
    )


def test_delete_orphan_nodes_aborts_on_corrupt_edges_read() -> None:
    """C4-Mo-1: corrupt edges read (RuntimeError) must propagate and abort BEFORE any node deletion.

    This test verifies the C3-M-1 fix: the edges open_table is in its own try/except
    (so absent table = skip), but the .to_arrow() read is OUTSIDE any try/except.
    A corrupt read must let the exception propagate before nodes_table.delete is called.
    """
    node_a = _node("EntityA")  # kept (has mention)
    node_b = _node("EntityB")  # orphan (no mention) → would be deleted IF read succeeds

    mention_a = _mention(node_a.id, "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    # Edges table opens successfully but the .to_arrow() read raises RuntimeError
    corrupt_edges_q = AsyncMock()
    corrupt_edges_q.to_arrow = AsyncMock(side_effect=RuntimeError("DataFusion read failure"))
    corrupt_edges_q.select = MagicMock(return_value=corrupt_edges_q)
    mock_edges_table = MagicMock()
    mock_edges_table.query.return_value = corrupt_edges_q
    mock_edges_table.schema = AsyncMock(return_value=GraphStore._edges_schema())

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        if "edges" in name:
            return mock_edges_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    # The RuntimeError from the edges read must propagate out of the method
    with pytest.raises(RuntimeError, match="DataFusion read failure"):
        asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    # CRITICAL: nodes_table.delete must NOT have been called — GC aborted before deletion
    mock_nodes_table.delete.assert_not_called()


def test_delete_orphan_nodes_skipsEdgesTableWhenAllNodesMentioned() -> None:
    """Healthy graph (zero orphan candidates) must not open the edges table (C1-B-2)."""
    node_a = _node("EntityA")
    node_b = _node("EntityB")
    mention_a = _mention(node_a.id, "chunk-1")
    mention_b = _mention(node_b.id, "chunk-2")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_a, mention_b]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([node_a, node_b]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q

    store = GraphStore.__new__(GraphStore)
    open_calls: list[str] = []

    async def _open(name: str):
        open_calls.append(name)
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result == GcPassResult(orphan_nodes_removed=0, orphan_edges_removed=0)
    assert not any("edges" in name for name in open_calls)


def test_delete_orphan_nodes_exemptsInferredDefRefEdges() -> None:
    """Inferred-tier def/ref edges must survive orphan GC (BE-4 forward-compat)."""
    hub = _node("hub", EntityType.code_symbol)
    caller = _node("caller", EntityType.code_symbol)
    inferred = GraphEdge(
        id=make_stable_edge_id(caller.id, hub.id, RelationshipType.calls.value),
        source_node_id=caller.id,
        target_node_id=hub.id,
        relationship_type=RelationshipType.calls,
        source_doc_id="doc-abc",
        extraction_method="inferred",
    )

    mention_unrelated = _mention("unrelated-entity-id", "chunk-1")

    mentions_q = AsyncMock()
    mentions_q.to_arrow = AsyncMock(return_value=_mentions_arrow([mention_unrelated]))
    mentions_q.select = MagicMock(return_value=mentions_q)
    mock_mentions_table = MagicMock()
    mock_mentions_table.query.return_value = mentions_q

    nodes_q = AsyncMock()
    nodes_q.to_arrow = AsyncMock(return_value=_nodes_arrow([hub, caller]))
    nodes_q.select = MagicMock(return_value=nodes_q)
    mock_nodes_table = MagicMock()
    mock_nodes_table.query.return_value = nodes_q
    mock_nodes_table.delete = AsyncMock(return_value=None)

    edges_q = AsyncMock()
    edges_q.to_arrow = AsyncMock(return_value=_edges_arrow([inferred]))
    edges_q.select = MagicMock(return_value=edges_q)
    mock_edges_table = MagicMock()
    mock_edges_table.query.return_value = edges_q
    mock_edges_table.schema = AsyncMock(return_value=GraphStore._edges_schema())
    mock_edges_table.delete = AsyncMock(return_value=None)

    store = GraphStore.__new__(GraphStore)

    async def _open(name: str):
        if "mentions" in name:
            return mock_mentions_table
        if "nodes" in name:
            return mock_nodes_table
        if "edges" in name:
            return mock_edges_table
        raise FileNotFoundError(name)

    mock_db = AsyncMock()
    mock_db.open_table.side_effect = _open
    store._db = mock_db

    result = asyncio.run(store.delete_orphan_nodes_and_edges(_COL, _NS))

    assert result.orphan_nodes_removed == 0
    assert result.orphan_edges_removed == 0
    mock_nodes_table.delete.assert_not_called()
    mock_edges_table.delete.assert_not_called()


def test_delete_defref_graph_by_doc_removesExtractedEdgesAndEndpoints(tmp_path: Path) -> None:
    """delete_defref_graph_by_doc removes def/ref rows scoped to one doc_id."""
    doc_id = "doc-target"
    hub = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "hub::/a.py"),
        entity_name="hub",
        entity_type=EntityType.code_symbol,
        source_doc_id=doc_id,
        collection_name=_COL,
        entity_subtype="python-function",
    )
    leaf = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "leaf::/a.py"),
        entity_name="leaf",
        entity_type=EntityType.code_symbol,
        source_doc_id=doc_id,
        collection_name=_COL,
        entity_subtype="python-function",
    )
    other_doc_node = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "other::/b.py"),
        entity_name="other",
        entity_type=EntityType.code_symbol,
        source_doc_id="other-doc",
        collection_name=_COL,
    )
    defref_edge = GraphEdge(
        id=make_stable_edge_id(hub.id, leaf.id, RelationshipType.calls.value),
        source_node_id=hub.id,
        target_node_id=leaf.id,
        relationship_type=RelationshipType.calls,
        source_doc_id=doc_id,
        extraction_method="extracted",
    )
    cooc_edge = GraphEdge(
        id=make_stable_edge_id(hub.id, other_doc_node.id, RelationshipType.related_to.value),
        source_node_id=hub.id,
        target_node_id=other_doc_node.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id=doc_id,
        extraction_method=None,
    )

    async def _run() -> None:
        gs = GraphStore(str(tmp_path / "gc-defref-del"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(_COL, ns=_NS)
            await gs.write_graph(_COL, [hub, leaf, other_doc_node], [defref_edge, cooc_edge], ns=_NS)
            await gs.delete_defref_graph_by_doc(_COL, doc_id, _NS)
            edges = await gs.get_all_edges(_COL, ns=_NS)
            nodes = await gs.get_all_nodes(_COL, ns=_NS)
        finally:
            await gs.disconnect()

        assert {e.id for e in edges} == set()
        assert {n.id for n in nodes} == {other_doc_node.id}

    asyncio.run(_run())


def test_delete_defref_graph_by_doc_respectsPreserveNodeIds(tmp_path: Path) -> None:
    """preserve_node_ids must skip deletion of nodes about to be re-written (C2-I-1)."""
    doc_id = "doc-target"
    keep = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "keep::/a.py"),
        entity_name="keep",
        entity_type=EntityType.code_symbol,
        source_doc_id=doc_id,
        collection_name=_COL,
        entity_subtype="python-function",
    )
    drop = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "drop::/a.py"),
        entity_name="drop",
        entity_type=EntityType.code_symbol,
        source_doc_id=doc_id,
        collection_name=_COL,
        entity_subtype="python-function",
    )
    edge = GraphEdge(
        id=make_stable_edge_id(keep.id, drop.id, RelationshipType.calls.value),
        source_node_id=keep.id,
        target_node_id=drop.id,
        relationship_type=RelationshipType.calls,
        source_doc_id=doc_id,
        extraction_method="extracted",
    )

    async def _run() -> None:
        gs = GraphStore(str(tmp_path / "gc-defref-preserve"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(_COL, ns=_NS)
            await gs.write_graph(_COL, [keep, drop], [edge], ns=_NS)
            await gs.delete_defref_graph_by_doc(
                _COL, doc_id, _NS, preserve_node_ids=frozenset({keep.id})
            )
            nodes = await gs.get_all_nodes(_COL, ns=_NS)
            edges = await gs.get_all_edges(_COL, ns=_NS)
        finally:
            await gs.disconnect()

        assert {n.id for n in nodes} == {keep.id}
        assert edges == []

    asyncio.run(_run())


def test_delete_graph_by_doc_preservesSharedEntityNode(tmp_path: Path) -> None:
    """delete_graph_by_doc must not remove shared nodes owned by another doc (C2-I-1)."""
    shared_id = make_stable_entity_id(EntityType.person.value, "Alice")
    shared = GraphNode(
        id=shared_id,
        entity_name="Alice",
        entity_type=EntityType.person,
        source_doc_id="doc-a",
        collection_name=_COL,
    )
    edge_from_b = GraphEdge(
        id=make_stable_edge_id(shared_id, "other-node", RelationshipType.related_to.value),
        source_node_id=shared_id,
        target_node_id="other-node",
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-b",
        extraction_method=None,
    )

    async def _run() -> None:
        gs = GraphStore(str(tmp_path / "gc-doc-del-shared"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(_COL, ns=_NS)
            await gs.write_graph(_COL, [shared], [edge_from_b], ns=_NS)
            await gs.delete_graph_by_doc(_COL, "doc-a", _NS)
            nodes = await gs.get_all_nodes(_COL, ns=_NS)
            edges = await gs.get_all_edges(_COL, ns=_NS)
        finally:
            await gs.disconnect()

        assert {n.id for n in nodes} == {shared_id}
        assert {e.id for e in edges} == {edge_from_b.id}

    asyncio.run(_run())

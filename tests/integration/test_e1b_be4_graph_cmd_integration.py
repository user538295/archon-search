"""Integration test for ``archon-search graph build-communities`` CLI — E1b BE-4.

Uses a real tmp GraphStore + SearchStore with fixture graph data.
Skips gracefully when leidenalg is not installed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

leidenalg = pytest.importorskip(
    "leidenalg", reason="leidenalg not installed; skipping BE-4 integration test"
)

pytestmark = pytest.mark.integration

from click.testing import CliRunner

from archon_search._types import ChunkRecord
from archon_search.cli.graph_cmd import graph_cmd
from archon_search.config import GraphConfig
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)
from archon_search.store import SearchStore


def _make_node(name: str, source_doc_id: str, collection: str) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id("concept", name),
        entity_name=name,
        entity_type=EntityType.concept,
        source_doc_id=source_doc_id,
        collection_name=collection,
    )


def _make_edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, "related_to"),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id=src.source_doc_id,
    )


def _make_chunk(doc_id: str, idx: int = 0) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"text for {doc_id} chunk {idx}",
        vector=[1.0, 0.0, 0.0],
        source_path=f"/fake/{doc_id}.txt",
        indexed_at="2026-01-01T00:00:00Z",
    )


def test_build_communities_cli_real_store(tmp_path: Path) -> None:
    """Real tmp store with fixture graph data; CLI writes communities; exit 0."""
    col = "test-col"
    doc_id_a = "docaaaaaaaaaaaaaaaaaaaaa"  # 24 chars
    doc_id_b = "docbbbbbbbbbbbbbbbbbbbbb"  # 24 chars

    node_a = _make_node("Alpha", doc_id_a, col)
    node_b = _make_node("Beta", doc_id_b, col)
    nodes = [node_a, node_b]
    edges = [_make_edge(node_a, node_b)]

    db_path = str(tmp_path / "db")

    async def _seed() -> None:
        graph_store = GraphStore(tmp_path / "graph_db")
        await graph_store.connect()
        await graph_store.ensure_graph_tables(col, ns="default")
        await graph_store.write_graph(col, nodes, edges, ns="default")
        await graph_store.disconnect()

        search_store = SearchStore(db_path)
        await search_store.connect()
        await search_store.ingest_chunks(col, [_make_chunk(doc_id_a, 0)])
        await search_store.ingest_chunks(col, [_make_chunk(doc_id_b, 0)])
        await search_store.disconnect()

    asyncio.run(_seed())

    from unittest.mock import MagicMock, patch

    mock_cfg = MagicMock()
    mock_cfg.db_path = db_path
    mock_cfg.graph = GraphConfig(enabled=True, community_summary_chunks=1)

    runner = CliRunner()
    with patch("archon_search.cli.graph_cmd.load_config", return_value=mock_cfg):
        # Override GraphStore to use the graph_db path instead
        original_graph_store_init = GraphStore.__init__

        call_count = [0]

        def patched_gs_init(self, db_path_arg):  # type: ignore[override]
            call_count[0] += 1
            original_graph_store_init(self, tmp_path / "graph_db")

        with patch.object(GraphStore, "__init__", patched_gs_init):
            result = runner.invoke(graph_cmd, ["build-communities", col])

    assert result.exit_code == 0, (
        f"Unexpected exit code {result.exit_code}:\n{result.output}"
    )
    assert "1" in result.output or "communit" in result.output.lower(), (
        f"Expected community count in output: {result.output!r}"
    )

    # Verify communities were written to the GraphStore
    async def _verify() -> None:
        graph_store = GraphStore(tmp_path / "graph_db")
        await graph_store.connect()
        count, last_built_at = await graph_store.get_community_stats(col, ns="default")
        await graph_store.disconnect()
        assert count >= 1, f"Expected at least 1 community, got {count}"
        assert last_built_at is not None, "Expected last_built_at to be set"

    asyncio.run(_verify())

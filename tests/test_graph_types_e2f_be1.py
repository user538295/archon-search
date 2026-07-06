"""Tests for BE-1 — synonym_of RelationshipType, extraction_method on GraphEdge.

Tests:
- test_synonym_of_in_relationship_type_enum
- test_extraction_method_field_on_graph_edge
- test_edges_schema_has_extraction_method_column
- test_arrow_to_edges_handles_absent_extraction_method_column
- test_write_graph_includes_extraction_method_in_edges_data
- test_write_graph_none_extraction_method_round_trips
- test_write_graph_on_pre_e2f_edge_table_migrates_and_writes
- test_write_graph_migration_preserves_existing_rows
"""
from __future__ import annotations

import asyncio

import pyarrow as pa
import pytest

from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_edge(rel: RelationshipType = RelationshipType.uses, extraction_method: str | None = None) -> GraphEdge:
    src_id = make_stable_entity_id("concept", "alpha")
    tgt_id = make_stable_entity_id("concept", "beta")
    return GraphEdge(
        id=make_stable_edge_id(src_id, tgt_id, rel.value),
        source_node_id=src_id,
        target_node_id=tgt_id,
        relationship_type=rel,
        source_doc_id="doc-1",
        extraction_method=extraction_method,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_synonym_of_in_relationship_type_enum() -> None:
    """RelationshipType('synonym_of') must round-trip correctly."""
    rt = RelationshipType("synonym_of")
    assert rt == RelationshipType.synonym_of
    assert rt.value == "synonym_of"


def test_extraction_method_field_on_graph_edge() -> None:
    """GraphEdge must accept and store extraction_method."""
    edge_with = _make_edge(extraction_method="embedding")
    assert edge_with.extraction_method == "embedding"

    edge_without = _make_edge()
    assert edge_without.extraction_method is None


def test_edges_schema_has_extraction_method_column() -> None:
    """_edges_schema() must include a nullable extraction_method utf8 column."""
    from archon_search.graph_store import GraphStore

    schema = GraphStore._edges_schema()
    assert "extraction_method" in schema.names

    field = schema.field("extraction_method")
    assert pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
    assert field.nullable


def test_arrow_to_edges_handles_absent_extraction_method_column() -> None:
    """Edge tables without extraction_method column deserialize to GraphEdge with extraction_method=None."""
    from archon_search.graph_store import GraphStore

    # Build a table that matches the OLD schema (no extraction_method column)
    src_id = make_stable_entity_id("concept", "alpha")
    tgt_id = make_stable_entity_id("concept", "beta")
    edge_id = make_stable_edge_id(src_id, tgt_id, "uses")

    old_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("source_node_id", pa.utf8()),
        pa.field("target_node_id", pa.utf8()),
        pa.field("relationship_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
    ])
    arrow_table = pa.table(
        {
            "id": [edge_id],
            "source_node_id": [src_id],
            "target_node_id": [tgt_id],
            "relationship_type": ["uses"],
            "source_doc_id": ["doc-1"],
        },
        schema=old_schema,
    )

    edges = GraphStore._arrow_to_edges(arrow_table)
    assert len(edges) == 1
    assert edges[0].extraction_method is None
    assert edges[0].relationship_type == RelationshipType.uses


def test_write_graph_includes_extraction_method_in_edges_data(tmp_path) -> None:
    """GraphEdge with extraction_method='embedding' survives a write → read round-trip."""
    import lancedb

    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphNode

    src_node = GraphNode(
        id=make_stable_entity_id("concept", "alpha"),
        entity_name="alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    tgt_node = GraphNode(
        id=make_stable_entity_id("concept", "beta"),
        entity_name="beta",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    edge = _make_edge(rel=RelationshipType.synonym_of, extraction_method="embedding")

    async def _run() -> None:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables("test-col", ns="default")
            await gs.write_graph("test-col", [src_node, tgt_node], [edge], ns="default")
            edges = await gs.get_all_edges("test-col", ns="default")
        finally:
            await gs.disconnect()

        assert len(edges) == 1
        assert edges[0].extraction_method == "embedding"
        assert edges[0].relationship_type == RelationshipType.synonym_of

    asyncio.run(_run())


def test_write_graph_none_extraction_method_round_trips(tmp_path) -> None:
    """GraphEdge with extraction_method=None survives a write → read round-trip as None."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphNode

    src_node = GraphNode(
        id=make_stable_entity_id("concept", "alpha"),
        entity_name="alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    tgt_node = GraphNode(
        id=make_stable_entity_id("concept", "beta"),
        entity_name="beta",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    edge = _make_edge(rel=RelationshipType.uses, extraction_method=None)

    async def _run() -> None:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables("test-col", ns="default")
            await gs.write_graph("test-col", [src_node, tgt_node], [edge], ns="default")
            edges = await gs.get_all_edges("test-col", ns="default")
        finally:
            await gs.disconnect()

        assert len(edges) == 1
        assert edges[0].extraction_method is None

    asyncio.run(_run())


def test_write_graph_on_pre_e2f_edge_table_migrates_and_writes(tmp_path) -> None:
    """ensure_graph_tables() migrates a pre-E2f 5-column edge table; write_graph then succeeds."""
    import lancedb

    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphNode

    old_edges_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("source_node_id", pa.utf8()),
        pa.field("target_node_id", pa.utf8()),
        pa.field("relationship_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
    ])

    src_node = GraphNode(
        id=make_stable_entity_id("concept", "alpha"),
        entity_name="alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    tgt_node = GraphNode(
        id=make_stable_entity_id("concept", "beta"),
        entity_name="beta",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    edge = _make_edge(rel=RelationshipType.synonym_of, extraction_method="embedding")

    async def _run() -> None:
        # Step 1: create old-schema edge table directly (simulates pre-E2f state)
        db = await lancedb.connect_async(str(tmp_path))
        edges_table_name = "_archon_graph_default__test-col_edges"
        await db.create_table(edges_table_name, schema=old_edges_schema)
        # Also create the other tables so ensure_graph_tables does not error
        await db.create_table("_archon_graph_default__test-col_nodes", schema=GraphStore._nodes_schema())
        await db.create_table("_archon_graph_default__test-col_mentions", schema=GraphStore._mentions_schema())

        # Step 2: call ensure_graph_tables — should add extraction_method column
        gs = GraphStore(str(tmp_path))
        gs._db = db
        await gs.ensure_graph_tables("test-col", ns="default")

        # Step 3: verify column was added
        tbl = await db.open_table(edges_table_name)
        schema = await tbl.schema()
        assert "extraction_method" in schema.names

        # Step 4: write_graph must succeed and round-trip extraction_method
        await gs.write_graph("test-col", [src_node, tgt_node], [edge], ns="default")
        edges = await gs.get_all_edges("test-col", ns="default")
        gs._db = None
        db.close()

        assert len(edges) == 1
        assert edges[0].extraction_method == "embedding"
        assert edges[0].relationship_type == RelationshipType.synonym_of

    asyncio.run(_run())


def test_write_graph_migration_preserves_existing_rows(tmp_path) -> None:
    """Migration via ensure_graph_tables() preserves pre-existing edge rows with extraction_method=None.

    Covers the real upgrade scenario: an edge table with rows already in it (old schema,
    no extraction_method column) must survive add_columns; existing rows read back with
    extraction_method=None, and new rows written after migration read back with the
    value supplied to write_graph.
    """
    import lancedb

    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphNode

    old_edges_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("source_node_id", pa.utf8()),
        pa.field("target_node_id", pa.utf8()),
        pa.field("relationship_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
    ])

    # Two pre-existing edge IDs (use distinct entity pairs to avoid ID collisions)
    src_a = make_stable_entity_id("concept", "gamma")
    tgt_a = make_stable_entity_id("concept", "delta")
    edge_a_id = make_stable_edge_id(src_a, tgt_a, "related_to")

    src_b = make_stable_entity_id("concept", "epsilon")
    tgt_b = make_stable_entity_id("concept", "zeta")
    edge_b_id = make_stable_edge_id(src_b, tgt_b, "related_to")

    # New edge written after migration
    new_edge = _make_edge(rel=RelationshipType.synonym_of, extraction_method="manual")

    # Nodes required by write_graph (for the new edge only)
    src_node = GraphNode(
        id=make_stable_entity_id("concept", "alpha"),
        entity_name="alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    tgt_node = GraphNode(
        id=make_stable_entity_id("concept", "beta"),
        entity_name="beta",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )

    async def _run() -> None:
        edges_table_name = "_archon_graph_default__test-col_edges"

        # Step 1: create old-schema edge table and write 2 rows directly (no write_graph)
        db = await lancedb.connect_async(str(tmp_path))
        old_tbl = await db.create_table(edges_table_name, schema=old_edges_schema)
        pre_existing_rows = pa.table(
            {
                "id": pa.array([edge_a_id, edge_b_id], type=pa.utf8()),
                "source_node_id": pa.array([src_a, src_b], type=pa.utf8()),
                "target_node_id": pa.array([tgt_a, tgt_b], type=pa.utf8()),
                "relationship_type": pa.array(["related_to", "related_to"], type=pa.utf8()),
                "source_doc_id": pa.array(["doc-pre-1", "doc-pre-2"], type=pa.utf8()),
            },
            schema=old_edges_schema,
        )
        await old_tbl.add(pre_existing_rows)

        # Also create companion tables so ensure_graph_tables does not error
        await db.create_table("_archon_graph_default__test-col_nodes", schema=GraphStore._nodes_schema())
        await db.create_table("_archon_graph_default__test-col_mentions", schema=GraphStore._mentions_schema())

        # Step 2: inject DB into GraphStore and run ensure_graph_tables (triggers migration)
        gs = GraphStore(str(tmp_path))
        gs._db = db
        await gs.ensure_graph_tables("test-col", ns="default")

        # Step 3: extraction_method column must now exist
        tbl = await db.open_table(edges_table_name)
        schema = await tbl.schema()
        assert "extraction_method" in schema.names

        # Step 4: read back edges — both pre-existing rows must survive with extraction_method=None
        pre_edges = await gs.get_all_edges("test-col", ns="default")
        assert len(pre_edges) == 2
        assert all(e.extraction_method is None for e in pre_edges), (
            f"Pre-existing edges should have extraction_method=None, got: "
            f"{[e.extraction_method for e in pre_edges]}"
        )

        # Step 5: write one new edge with extraction_method="manual" via write_graph
        await gs.write_graph("test-col", [src_node, tgt_node], [new_edge], ns="default")

        # Step 6: all 3 edges must be present; old rows still None, new row "manual"
        all_edges = await gs.get_all_edges("test-col", ns="default")
        assert len(all_edges) == 3

        old_edge_ids = {edge_a_id, edge_b_id}
        for e in all_edges:
            if e.id in old_edge_ids:
                assert e.extraction_method is None, (
                    f"Pre-existing edge {e.id} expected extraction_method=None, got {e.extraction_method!r}"
                )
            else:
                assert e.extraction_method == "manual", (
                    f"New edge expected extraction_method='manual', got {e.extraction_method!r}"
                )

        gs._db = None
        db.close()

    asyncio.run(_run())

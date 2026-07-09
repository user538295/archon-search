"""Integration test for E2g BE-2: DefRefExtractor writes real edges to LanceDB.

test_defrefExtractor_writesEdgesToGraphStore: extracted calls/imports/defines/inherits
edges round-trip through a REAL GraphStore (real LanceDB tables in tmp_path), mirroring
tests/integration/test_be4_synonym_detector.py's pattern.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

from archon_search.defref_extractor import DefRefExtractor  # noqa: E402
from archon_search.graph_store import GraphStore  # noqa: E402
from archon_search.graph_types import (  # noqa: E402
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

pytestmark = pytest.mark.integration


def test_defrefExtractor_writesEdgesToGraphStore(tmp_path) -> None:
    """Extracted edges round-trip through a real GraphStore.

    Setup: a small Python file with a module-level function definition, a
    same-file call, an import, and a same-file inheritance relationship.

    Steps:
    1. Create a real GraphStore in tmp_path; ``ensure_graph_tables`` first
       (``write_graph`` raises ``ValueError`` if the tables don't exist yet).
    2. Run DefRefExtractor.extract() against the file's whole text.
    3. Caller writes the resulting nodes/edges via ``graph_store.write_graph()``.
    4. Read back via ``get_all_nodes``/``get_all_edges`` and assert every
       extracted relationship type persisted correctly.
    """
    file_text = (
        "class Base:\n"
        "    pass\n\n"
        "class Foo(Base):\n"
        "    def bar(self):\n"
        "        return baz()\n\n"
        "def baz():\n"
        "    return 1\n\n"
        "import os\n"
    )
    file_path = "/repo/mod.py"

    async def _run():
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables("test-col", ns="default")

            extractor = DefRefExtractor(graph_store=gs)
            result = await extractor.extract(
                file_text=file_text,
                file_path=file_path,
                doc_id="doc-1",
                collection="test-col",
                ns="default",
            )

            await gs.write_graph("test-col", result.nodes, result.edges, ns="default")

            all_nodes = await gs.get_all_nodes("test-col", ns="default")
            all_edges = await gs.get_all_edges("test-col", ns="default")
        finally:
            await gs.disconnect()

        return all_nodes, all_edges

    all_nodes, all_edges = asyncio.run(_run())

    names = {n.entity_name for n in all_nodes}
    assert {"mod", "Base", "Foo", "bar", "baz", "os"}.issubset(names)

    for edge in all_edges:
        assert edge.extraction_method == "extracted"

    calls = [e for e in all_edges if e.relationship_type == RelationshipType.calls]
    assert len(calls) == 1

    imports = [e for e in all_edges if e.relationship_type == RelationshipType.imports]
    assert len(imports) == 1

    inherits = [e for e in all_edges if e.relationship_type == RelationshipType.inherits]
    assert len(inherits) == 1

    defines = [e for e in all_edges if e.relationship_type == RelationshipType.defines]
    # module defines Base, module defines Foo, module defines baz, Foo defines bar.
    assert len(defines) == 4


# ---------------------------------------------------------------------------
# BE-4 — write_graph edge extraction_method tag-collision precedence (Q11),
# exercised end-to-end against a real GraphStore / real LanceDB.
# ---------------------------------------------------------------------------


def _make_symbol_node(name: str, file_path: str, doc_id: str, collection: str) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, f"{name}::{file_path}"),
        entity_name=name,
        entity_type=EntityType.code_symbol,
        source_doc_id=doc_id,
        collection_name=collection,
        entity_subtype="python-function",
    )


def test_inferredEdges_coexistWithExtracted_perCollection(tmp_path) -> None:
    """A plain bulk upsert never downgrades a stored "extracted" tag to "inferred".

    Two orderings, both against a real GraphStore/LanceDB:
    - Ordering 1: discovered "inferred" first, then "extracted" — the plain bulk
      upsert (no override needed, since incoming is "extracted") leaves it
      "extracted".
    - Ordering 2: discovered "extracted" first, then re-discovered "inferred" —
      the pre-read-and-override step fires and keeps the tag at "extracted".
    """
    collection = "test-col"

    async def _run() -> tuple[str | None, str | None]:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(collection, ns="default")

            # --- Ordering 1: inferred first, then extracted ---
            caller_1 = _make_symbol_node("caller1", "/repo/a.py", "doc-a", collection)
            callee_1 = _make_symbol_node("callee1", "/repo/other.py", "doc-other", collection)
            edge_id_1 = make_stable_edge_id(
                caller_1.id, callee_1.id, RelationshipType.calls.value
            )
            inferred_first = GraphEdge(
                id=edge_id_1,
                source_node_id=caller_1.id,
                target_node_id=callee_1.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-a-v1",
                extraction_method="inferred",
            )
            extracted_second = GraphEdge(
                id=edge_id_1,
                source_node_id=caller_1.id,
                target_node_id=callee_1.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-a-v2",
                extraction_method="extracted",
            )
            await gs.write_graph(
                collection, [caller_1, callee_1], [inferred_first], ns="default"
            )
            await gs.write_graph(collection, [], [extracted_second], ns="default")

            # --- Ordering 2: extracted first, then re-discovered inferred ---
            caller_2 = _make_symbol_node("caller2", "/repo/b.py", "doc-b", collection)
            callee_2 = _make_symbol_node("callee2", "/repo/other2.py", "doc-other2", collection)
            edge_id_2 = make_stable_edge_id(
                caller_2.id, callee_2.id, RelationshipType.calls.value
            )
            extracted_first = GraphEdge(
                id=edge_id_2,
                source_node_id=caller_2.id,
                target_node_id=callee_2.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-b-v1",
                extraction_method="extracted",
            )
            inferred_second = GraphEdge(
                id=edge_id_2,
                source_node_id=caller_2.id,
                target_node_id=callee_2.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-b-v2",
                extraction_method="inferred",
            )
            await gs.write_graph(
                collection, [caller_2, callee_2], [extracted_first], ns="default"
            )
            await gs.write_graph(collection, [], [inferred_second], ns="default")

            all_edges = await gs.get_all_edges(collection, ns="default")
        finally:
            await gs.disconnect()

        method_1 = next(e.extraction_method for e in all_edges if e.id == edge_id_1)
        method_2 = next(e.extraction_method for e in all_edges if e.id == edge_id_2)
        return method_1, method_2

    method_1, method_2 = asyncio.run(_run())

    assert method_1 == "extracted", (
        "Ordering 1 (inferred then extracted): plain upsert must leave the tag 'extracted'"
    )
    assert method_2 == "extracted", (
        "Ordering 2 (extracted then inferred): pre-read-and-override must keep the tag 'extracted'"
    )


def test_sequentialIngest_sameEdgeId_noCorruption(tmp_path) -> None:
    """Two sequential write_graph calls upserting the same stable edge ID leave exactly one row.

    Not concurrency — plain sequential Python calls to write_graph, mirroring
    re-ingest of the same file. No duplicate/corrupted rows, and the surviving
    row carries a valid extraction_method tag.
    """
    collection = "test-col"

    async def _run() -> list[GraphEdge]:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(collection, ns="default")

            caller = _make_symbol_node("caller", "/repo/seq.py", "doc-seq", collection)
            callee = _make_symbol_node("callee", "/repo/seq.py", "doc-seq", collection)
            edge_id = make_stable_edge_id(caller.id, callee.id, RelationshipType.calls.value)

            edge_v1 = GraphEdge(
                id=edge_id,
                source_node_id=caller.id,
                target_node_id=callee.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-seq-v1",
                extraction_method="extracted",
            )
            edge_v2 = GraphEdge(
                id=edge_id,
                source_node_id=caller.id,
                target_node_id=callee.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-seq-v2",
                extraction_method="extracted",
            )

            await gs.write_graph(collection, [caller, callee], [edge_v1], ns="default")
            await gs.write_graph(collection, [], [edge_v2], ns="default")

            all_edges = await gs.get_all_edges(collection, ns="default")
        finally:
            await gs.disconnect()

        return [e for e in all_edges if e.id == edge_id]

    matching_edges = asyncio.run(_run())

    assert len(matching_edges) == 1, (
        f"Expected exactly 1 row for the re-upserted edge id, got {len(matching_edges)}"
    )
    assert matching_edges[0].extraction_method == "extracted"
    assert matching_edges[0].source_doc_id == "doc-seq-v2", "source_doc_id must refresh on re-upsert"


def test_plainInferredEdge_noCollision_persistsAsInferred(tmp_path) -> None:
    """A single "inferred" edge with no prior "extracted" collision round-trips
    unchanged through a real GraphStore — closes the gap where every other
    real-store assertion in this suite happens to read back "extracted".
    """
    collection = "test-col"

    async def _run() -> str | None:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(collection, ns="default")

            caller = _make_symbol_node("caller", "/repo/only.py", "doc-only", collection)
            callee = _make_symbol_node("callee", "/repo/other-only.py", "doc-other-only", collection)
            edge_id = make_stable_edge_id(caller.id, callee.id, RelationshipType.calls.value)

            inferred_edge = GraphEdge(
                id=edge_id,
                source_node_id=caller.id,
                target_node_id=callee.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-only",
                extraction_method="inferred",
            )
            await gs.write_graph(collection, [caller, callee], [inferred_edge], ns="default")

            all_edges = await gs.get_all_edges(collection, ns="default")
        finally:
            await gs.disconnect()

        matching = [e for e in all_edges if e.id == edge_id]
        assert len(matching) == 1
        return matching[0].extraction_method

    extraction_method = asyncio.run(_run())
    assert extraction_method == "inferred"


def test_preReadScoping_noCrossCollectionLeakage(tmp_path) -> None:
    """The extraction_method pre-read is scoped per collection/namespace table.

    An "extracted" edge with id X in one collection must never leak its tag
    into a same-id "inferred" edge written to a DIFFERENT collection — graph
    tables are separate per collection/namespace, so there must be no
    cross-collection tag preservation.
    """
    collection_a = "test-col-a"
    collection_b = "test-col-b"

    async def _run() -> str | None:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(collection_a, ns="default")
            await gs.ensure_graph_tables(collection_b, ns="default")

            caller = _make_symbol_node("caller", "/repo/scope.py", "doc-scope", collection_a)
            callee = _make_symbol_node("callee", "/repo/other-scope.py", "doc-other-scope", collection_a)
            edge_id = make_stable_edge_id(caller.id, callee.id, RelationshipType.calls.value)

            extracted_edge = GraphEdge(
                id=edge_id,
                source_node_id=caller.id,
                target_node_id=callee.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-scope-a",
                extraction_method="extracted",
            )
            await gs.write_graph(collection_a, [caller, callee], [extracted_edge], ns="default")

            inferred_edge = GraphEdge(
                id=edge_id,
                source_node_id=caller.id,
                target_node_id=callee.id,
                relationship_type=RelationshipType.calls,
                source_doc_id="doc-scope-b",
                extraction_method="inferred",
            )
            await gs.write_graph(collection_b, [caller, callee], [inferred_edge], ns="default")

            edges_b = await gs.get_all_edges(collection_b, ns="default")
        finally:
            await gs.disconnect()

        matching = [e for e in edges_b if e.id == edge_id]
        assert len(matching) == 1
        return matching[0].extraction_method

    extraction_method = asyncio.run(_run())
    assert extraction_method == "inferred", (
        "collection-b's edge must read back 'inferred' — no cross-collection tag leakage"
    )

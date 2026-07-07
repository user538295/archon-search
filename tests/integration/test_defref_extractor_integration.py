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
from archon_search.graph_types import RelationshipType  # noqa: E402

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

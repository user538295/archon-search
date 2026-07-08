"""Integration tests for E2g BE-3: DefRefExtractor wired into pipeline.ingest_file.

Mirrors tests/integration/test_e1a_be5_ingest_graph_integration.py's structure —
real SearchStore + real GraphStore, real DefRefExtractor (no mocking of the
extractor itself, unlike the unit-test file's mocked-extractor tests).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# BE-3 note (finding 4): the tree-sitter grammars used by DefRefExtractor.extract()
# live in the optional `[code]` extra, not installed by plain `uv sync --dev`.
# Without this guard, on an environment lacking `[code]`, extract() silently
# returns an empty result (see DefRefExtractor.extract()'s grammar-missing
# branch) and the `edge_count > 0` assertions below FAIL instead of skipping —
# same precedent as tests/integration/test_defref_extractor_integration.py.
pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

from archon_search.config import GraphConfig  # noqa: E402
from archon_search.defref_extractor import DefRefExtractor  # noqa: E402
from archon_search.graph_store import GraphStore  # noqa: E402
from archon_search.graph_types import RelationshipType  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_embedder():
    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    return Embedder(_MockEmbedderBackend())


def _make_pipeline(store, *, defref_extractor, graph_store, graph_config, graph_extractor=None):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    return SearchPipeline(
        store=store,
        embedder=_make_embedder(),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        defref_extractor=defref_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
        graph_extractor=graph_extractor,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingestCodeFile_producesEdgesEndToEnd(tmp_path: Path):
    """Ingesting a real Python file via the pipeline produces def/ref edges in the graph store."""
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_defref_e2e"
    graph_config = GraphConfig(enabled=True)
    defref_extractor = DefRefExtractor(graph_store=graph_store)
    pipeline = _make_pipeline(
        store, defref_extractor=defref_extractor, graph_store=graph_store, graph_config=graph_config
    )

    py_file = tmp_path / "module.py"
    py_file.write_text(
        "def helper():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    result = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert result.status == "ok", f"ingest failed: {result.error}"

    edge_count = await graph_store.edge_count(collection, ns="default")
    assert edge_count > 0, "expected at least one def/ref edge after ingesting a real code file"

    def_ref_types = {
        RelationshipType.calls.value,
        RelationshipType.imports.value,
        RelationshipType.defines.value,
        RelationshipType.inherits.value,
    }
    all_edges = await graph_store.get_all_edges(collection, ns="default")
    def_ref_edges = [e for e in all_edges if e.relationship_type.value in def_ref_types]
    assert def_ref_edges, "expected at least one def/ref-typed edge (calls/imports/defines/inherits)"
    assert all(
        e.extraction_method == "extracted" for e in def_ref_edges
    ), "def/ref edges from DefRefExtractor must be tagged extraction_method='extracted'"
    # `main -> helper` calls edge is the concrete relationship this file exercises.
    calls_edges = [e for e in def_ref_edges if e.relationship_type == RelationshipType.calls]
    assert calls_edges, "expected a 'calls' edge for main() calling helper()"

    all_nodes = await graph_store.get_all_nodes(collection, ns="default")
    node_names = {n.entity_name for n in all_nodes}
    assert "helper" in node_names
    assert "main" in node_names

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_preFeatureCollection_hasNoDefRefEdgesUntilReingest(tmp_path: Path):
    """S14: ingesting without DefRefExtractor wired produces zero def/ref edges;
    re-ingesting the SAME file with DefRefExtractor wired produces edges.

    Simulates the pre-BE-3 code path for real (rather than hand-seeding a bare
    node): a pipeline with `defref_extractor=None` (graph otherwise fully
    enabled) ingests a real code file — the assertion proves the OLD path
    genuinely produces zero def/ref edges, not just that an untouched manual
    node has none. The same file is then re-ingested through a second
    pipeline with DefRefExtractor wired, proving re-ingest (not passive
    migration) is the path to gaining edges.
    """
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_prefeature_defref"
    graph_config_enabled = GraphConfig(enabled=True)

    py_file = tmp_path / "legacy.py"
    py_file.write_text(
        "def helper():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    def_ref_types = {
        RelationshipType.calls.value,
        RelationshipType.imports.value,
        RelationshipType.defines.value,
        RelationshipType.inherits.value,
    }

    async def _def_ref_edge_count() -> int:
        all_edges = await graph_store.get_all_edges(collection, ns="default")
        return sum(1 for e in all_edges if e.relationship_type.value in def_ref_types)

    # --- Pre-feature: ingest through the OLD code path (defref_extractor=None,
    # graph otherwise enabled). Proves the old path produces zero def/ref edges.
    pipeline_without_defref = _make_pipeline(
        store, defref_extractor=None, graph_store=graph_store, graph_config=graph_config_enabled
    )
    result_before = await pipeline_without_defref.ingest_file(
        py_file, collection, embedder=_make_embedder()
    )
    assert result_before.status == "ok", f"ingest failed: {result_before.error}"
    assert await _def_ref_edge_count() == 0, "pre-feature ingest must not produce def/ref edges"

    # --- Re-ingest the SAME file through a pipeline with DefRefExtractor wired.
    defref_extractor = DefRefExtractor(graph_store=graph_store)
    pipeline_with_defref = _make_pipeline(
        store, defref_extractor=defref_extractor, graph_store=graph_store, graph_config=graph_config_enabled
    )
    result_after = await pipeline_with_defref.ingest_file(py_file, collection, embedder=_make_embedder())
    assert result_after.status == "ok", f"ingest failed: {result_after.error}"

    # Post-condition: def/ref edges now exist.
    assert await _def_ref_edge_count() > 0, "expected def/ref edges after re-ingest with DefRefExtractor wired"

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_bothExtractorsWired_coexistWithoutClobberingIdentity(tmp_path: Path):
    """Finding 3: graph_extractor + defref_extractor wired together (the real
    production configuration — both are always wired together in
    create_pipeline()/app.py) must coexist without one clobbering the other's
    node identity fields.

    graph_extractor's code-symbol path and defref_extractor both compute the
    SAME node ID for a chunk's primary symbol (both route through
    make_code_symbol_qualified_name(name, source_path)). write_graph()'s
    merge_insert().when_matched_update_all() means whichever extractor's write
    runs second (DefRefExtractor — see the Finding 3 comment in pipeline.py)
    wins on shared columns. This test proves that is benign: entity_name and
    entity_type stay consistent (both write the bare symbol name +
    EntityType.code_symbol) even though entity_subtype differs between the two
    extractors' schemes, and both co-occurrence mentions AND def/ref edges are
    present after ingest.
    """
    from archon_search.config import GraphConfig as _GraphConfig
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.graph_types import EntityType
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_defref_coexist"
    graph_config = GraphConfig(enabled=True)
    graph_extractor = GraphExtractor(_GraphConfig(enabled=True))
    defref_extractor = DefRefExtractor(graph_store=graph_store)

    pipeline = _make_pipeline(
        store,
        defref_extractor=defref_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
        graph_extractor=graph_extractor,
    )

    py_file = tmp_path / "coexist.py"
    py_file.write_text(
        "def helper():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    result = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert result.status == "ok", f"ingest failed: {result.error}"

    def_ref_types = {
        RelationshipType.calls.value,
        RelationshipType.imports.value,
        RelationshipType.defines.value,
        RelationshipType.inherits.value,
    }
    all_edges = await graph_store.get_all_edges(collection, ns="default")
    def_ref_edges = [e for e in all_edges if e.relationship_type.value in def_ref_types]
    assert def_ref_edges, "expected def/ref edges from defref_extractor to survive coexistence"

    # graph_extractor writes mentions (chunk-scoped incidence rows); defref_extractor
    # writes none. Both must be present: mentions prove graph_extractor's write
    # landed, def/ref edges prove defref_extractor's write landed.
    all_mentions = await graph_store.get_all_mentions(collection, ns="default")
    assert all_mentions, "expected graph_extractor's co-occurrence mentions to survive coexistence"

    # Shared node identity: entity_name/entity_type must agree regardless of
    # which extractor's write landed last.
    all_nodes = await graph_store.get_all_nodes(collection, ns="default")
    helper_nodes = [n for n in all_nodes if n.entity_name == "helper"]
    assert helper_nodes, "expected a node for 'helper'"
    assert all(n.entity_type == EntityType.code_symbol for n in helper_nodes)
    assert len(helper_nodes) == 1, "merge_insert must collapse to one node id for helper"
    assert helper_nodes[0].entity_subtype is not None
    assert helper_nodes[0].entity_subtype.endswith("-function"), (
        "DefRefExtractor write runs second; its entity_subtype scheme must win"
    )

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_bothExtractorsWired_nerModuleNodeSurvivesDefRefReconcile(tmp_path: Path):
    """NER module-level code_symbol nodes must survive def/ref delete-before-write (C3-I-1)."""
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_defref_ner_module"
    graph_config = GraphConfig(enabled=True)
    graph_extractor = GraphExtractor(GraphConfig(enabled=True))
    defref_extractor = DefRefExtractor(graph_store=graph_store)
    pipeline = _make_pipeline(
        store,
        defref_extractor=defref_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
        graph_extractor=graph_extractor,
    )

    py_file = tmp_path / "with_imports.py"
    py_file.write_text(
        '"""Module docstring."""\n'
        "import os\n\n\n"
        "def helper():\n"
        "    return os.getcwd()\n"
    )

    result = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert result.status == "ok", result.error

    all_nodes = await graph_store.get_all_nodes(collection, ns="default")
    ner_module_nodes = [
        n
        for n in all_nodes
        if n.entity_type.value == "code_symbol"
        and n.entity_subtype == "python-module"
    ]
    assert ner_module_nodes, "expected NER module-level code_symbol node from graph_extractor"

    all_mentions = await graph_store.get_all_mentions(collection, ns="default")
    node_ids = {n.id for n in all_nodes}
    for mention in all_mentions:
        assert mention.entity_id in node_ids, "every mention must resolve to a live node"

    defref_module_nodes = [
        n for n in all_nodes if n.entity_subtype and n.entity_subtype.endswith("-defref-module")
    ]
    assert defref_module_nodes, "expected DefRef module pseudo-node"

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_deleteDocument_removesDefRefGraphData(tmp_path: Path):
    """Deleting a code document removes its graph nodes/edges (C1-B-1 lifecycle)."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_defref_delete"
    graph_config = GraphConfig(enabled=True)
    defref_extractor = DefRefExtractor(graph_store=graph_store)
    pipeline = _make_pipeline(
        store,
        defref_extractor=defref_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    py_file = tmp_path / "delete_me.py"
    py_file.write_text(
        "def helper():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    result = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert result.status == "ok", result.error
    assert await graph_store.edge_count(collection, ns="default") > 0
    assert await graph_store.node_count(collection, ns="default") > 0

    deleted = await pipeline.delete_document(result.doc_id, collection)
    assert deleted > 0
    assert await graph_store.edge_count(collection, ns="default") == 0
    assert await graph_store.node_count(collection, ns="default") == 0

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_reingestRenamedSymbol_removesStaleDefRefRows(tmp_path: Path):
    """Re-ingest with renamed symbols removes stale nodes/edges (C2-I-2)."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_defref_reingest_rename"
    graph_config = GraphConfig(enabled=True)
    defref_extractor = DefRefExtractor(graph_store=graph_store)
    pipeline = _make_pipeline(
        store,
        defref_extractor=defref_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    py_file = tmp_path / "rename_me.py"
    py_file.write_text(
        "def helper():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    first = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert first.status == "ok", first.error

    nodes_before = await graph_store.get_all_nodes(collection, ns="default")
    helper_nodes_before = [n for n in nodes_before if n.entity_name == "helper"]
    assert len(helper_nodes_before) == 1

    py_file.write_text(
        "def helper2():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper2()\n"
    )
    second = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert second.status == "ok", second.error

    nodes_after = await graph_store.get_all_nodes(collection, ns="default")
    names = {n.entity_name for n in nodes_after}
    assert "helper" not in names
    assert "helper2" in names
    assert "main" in names

    all_edges = await graph_store.get_all_edges(collection, ns="default")
    node_by_id = {n.id: n for n in nodes_after}
    for edge in all_edges:
        assert edge.source_node_id in node_by_id
        assert edge.target_node_id in node_by_id

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
async def test_gcOrphanSweep_defRefEdgesSurviveWithoutMentions(tmp_path: Path):
    """Finding 1 (Critical): def/ref nodes/edges must survive orphan GC even though
    DefRefExtractor.extract() always returns mentions=[] (see its module docstring's
    "KNOWN CONTRACT GAP" note in defref_extractor.py).

    Ingests a code file through a pipeline configured with defref_extractor
    ONLY (graph_extractor unset, to isolate def/ref data from any co-occurrence
    mentions), seeds the mentions table with an entity that does NOT include
    any def/ref node ID, then runs delete_orphan_nodes_and_edges directly.
    Asserts every def/ref node and edge survives the GC pass — proving the
    graph_store.py _GC_EXEMPT_EXTRACTION_METHODS exemption (Finding 1's fix)
    works end-to-end.
    """
    from archon_search.graph_types import GraphMention
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    collection = "test_defref_gc_survival"
    graph_config = GraphConfig(enabled=True)
    defref_extractor = DefRefExtractor(graph_store=graph_store)

    # defref_extractor ONLY — no graph_extractor — isolates def/ref data with
    # zero co-occurrence mentions, matching DefRefExtractor's documented gap.
    pipeline = _make_pipeline(
        store, defref_extractor=defref_extractor, graph_store=graph_store, graph_config=graph_config
    )

    py_file = tmp_path / "gc_target.py"
    py_file.write_text(
        "def helper():\n"
        "    return 1\n\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    result = await pipeline.ingest_file(py_file, collection, embedder=_make_embedder())
    assert result.status == "ok", f"ingest failed: {result.error}"

    nodes_before = await graph_store.get_all_nodes(collection, ns="default")
    edges_before = await graph_store.get_all_edges(collection, ns="default")
    assert nodes_before, "expected def/ref nodes after ingest"
    assert edges_before, "expected def/ref edges after ingest"

    # Seed the mentions table with an entity that does NOT reference any
    # def/ref node ID — simulates "mentions never cover def/ref data" without
    # hitting the "empty mentions table" skip-GC guard.
    await graph_store.write_mentions(
        collection,
        [
            GraphMention(
                entity_id="unrelated-entity-id-not-a-defref-node",
                chunk_id=f"{result.doc_id}-000000",
                doc_id=result.doc_id,
            )
        ],
        ns="default",
    )

    gc_result = await graph_store.delete_orphan_nodes_and_edges(collection, ns="default")
    assert gc_result.orphan_nodes_removed == 0, (
        "def/ref nodes must be exempt from orphan GC despite having no mentions"
    )
    assert gc_result.orphan_edges_removed == 0, (
        "def/ref edges must be exempt from orphan GC despite having no mentions"
    )

    nodes_after = await graph_store.get_all_nodes(collection, ns="default")
    edges_after = await graph_store.get_all_edges(collection, ns="default")
    assert {n.id for n in nodes_after} == {n.id for n in nodes_before}
    assert {e.id for e in edges_after} == {e.id for e in edges_before}

    await store.disconnect()
    await graph_store.disconnect()


# ---------------------------------------------------------------------------
# Finding 2: production server (app.py) wiring
# ---------------------------------------------------------------------------

def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy package into sys.modules so create_app's
    _check_graph_deps() (which does `import spacy`) passes with graph.enabled=True.
    Same pattern as tests/integration/test_e1a_t1_graph_status_e2e.py.
    """

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def test_appPy_wiresDefRefExtractorWhenGraphEnabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 2: the production server (server/app.py) must wire a DefRefExtractor
    into SearchPipeline whenever graph.enabled=True — mirroring create_pipeline()'s
    wiring in pipeline.py. Prior to this fix, app.py constructed graph_extractor/
    graph_store/graph_expander but never a DefRefExtractor, so the real production
    server never produced def/ref edges regardless of BE-3's pipeline hook.
    """
    from tests.integration.conftest import make_real_app

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, _api_key):
        pipeline = client.app.state.pipeline
        assert pipeline._defref_extractor is not None, (
            "app.py must construct a DefRefExtractor and pass it to SearchPipeline "
            "whenever config.graph.enabled=True"
        )

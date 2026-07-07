"""Unit tests for E2g BE-3: DefRefExtractor wired into pipeline.ingest_file.

Mirrors tests/test_e1a_be5_pipeline_graph_hook.py's structure — a mocked
extractor + mocked graph store, asserting the never-propagate contract at
the pipeline level (as opposed to DefRefExtractor's own internal defensive
try/except, which is BE-2's concern).
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_types import GraphExtractionResult


# ---------------------------------------------------------------------------
# Helper — build a minimal SearchPipeline with optional graph/defref components
# ---------------------------------------------------------------------------

def _make_pipeline(store, *, defref_extractor=None, graph_store=None, graph_config=None):
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
        defref_extractor=defref_extractor,
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_py_file(tmp_path: Path) -> Path:
    """Write a small Python file for ingest tests (CODE_EXTENSIONS gate)."""
    f = tmp_path / "sample.py"
    f.write_text("def caller():\n    return callee()\n\n\ndef callee():\n    return 1\n")
    return f


def _mock_graph_store() -> MagicMock:
    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
    return mock_store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extractionFailure_logsWarningNotRaise(
    connected_store, col_name, sample_py_file, caplog
):
    """A DefRefExtractor exception is caught, logged WARNING, and never propagates."""
    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(side_effect=RuntimeError("tree-sitter blew up"))
    mock_store = _mock_graph_store()
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    with caplog.at_level(logging.WARNING):
        result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_extractor.extract.assert_called_once()
    assert any(
        "DefRef extraction failed" in record.message or "DefRef extraction failed" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )
    mock_store.write_graph.assert_not_called()
    # Finding 13: the failure must also be visible to the caller via
    # IngestResult.warnings, matching the sibling E1a graph-write block's pattern.
    assert any("DefRef extraction failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_writeGraphRaises_afterSuccessfulExtract_isCaughtAndLogged(
    connected_store, col_name, sample_py_file, caplog
):
    """extract() succeeds (returns non-empty nodes/edges) but write_graph() itself
    raises. Distinct from test_extractionFailure_logsWarningNotRaise, which only
    covers extract() raising — both live in the same try/except, but write_graph
    raising was previously untested (Finding 10).
    """
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    node_a = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "caller::sample.py"),
        entity_name="caller",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc1",
        collection_name=col_name,
        entity_subtype=None,
    )
    node_b = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "callee::sample.py"),
        entity_name="callee",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc1",
        collection_name=col_name,
        entity_subtype=None,
    )
    edge = GraphEdge(
        id=make_stable_edge_id(node_a.id, node_b.id, RelationshipType.calls.value),
        source_node_id=node_a.id,
        target_node_id=node_b.id,
        relationship_type=RelationshipType.calls,
        source_doc_id="doc1",
        extraction_method="extracted",
    )

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(nodes=[node_a, node_b], edges=[edge], mentions=[])
    )
    mock_store = _mock_graph_store()
    mock_store.write_graph = AsyncMock(side_effect=RuntimeError("lancedb write failed"))
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    with caplog.at_level(logging.WARNING):
        result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_extractor.extract.assert_called_once()
    mock_store.write_graph.assert_called_once()
    assert any(
        "DefRef extraction failed" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


@pytest.mark.asyncio
async def test_defref_disabled_when_graph_disabled(
    connected_store, col_name, sample_py_file
):
    """When GraphConfig.enabled=False, the defref extractor must never be called."""
    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock()
    graph_config = GraphConfig(enabled=False)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=_mock_graph_store(),
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_extractor.extract.assert_not_called()


@pytest.mark.asyncio
async def test_defref_skipped_for_non_code_file(
    connected_store, col_name, tmp_path: Path
):
    """DefRefExtractor is only invoked for CODE_EXTENSIONS files, never for markdown."""
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nJust prose, no code.\n")

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock()
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=_mock_graph_store(),
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(md_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_extractor.extract.assert_not_called()


@pytest.mark.asyncio
async def test_defref_writes_edges_when_extraction_succeeds(
    connected_store, col_name, sample_py_file
):
    """A clean extraction result is passed through to graph_store.write_graph."""
    from archon_search.graph_types import EntityType, GraphNode, RelationshipType, GraphEdge, make_stable_entity_id, make_stable_edge_id

    node_a = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "caller::sample.py"),
        entity_name="caller",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc1",
        collection_name=col_name,
        entity_subtype=None,
    )
    node_b = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "callee::sample.py"),
        entity_name="callee",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc1",
        collection_name=col_name,
        entity_subtype=None,
    )
    edge = GraphEdge(
        id=make_stable_edge_id(node_a.id, node_b.id, RelationshipType.calls.value),
        source_node_id=node_a.id,
        target_node_id=node_b.id,
        relationship_type=RelationshipType.calls,
        source_doc_id="doc1",
        extraction_method="extracted",
    )

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(nodes=[node_a, node_b], edges=[edge], mentions=[])
    )
    mock_store = _mock_graph_store()
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_extractor.extract.assert_called_once()
    mock_store.ensure_graph_tables.assert_called_once()
    mock_store.write_graph.assert_called_once()
    written_nodes = mock_store.write_graph.call_args.args[1]
    written_edges = mock_store.write_graph.call_args.args[2]
    assert written_nodes == [node_a, node_b]
    assert written_edges == [edge]


@pytest.mark.asyncio
async def test_defref_emptyExtractionResult_skipsWriteGraph(
    connected_store, col_name, sample_py_file
):
    """Finding 8: extract() returning an empty GraphExtractionResult (no exception)
    must skip write_graph/ensure_graph_tables entirely (the `if nodes or edges:`
    guard) and still return status="ok".
    """
    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(nodes=[], edges=[], mentions=[])
    )
    mock_store = _mock_graph_store()
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_extractor.extract.assert_called_once()
    mock_store.write_graph.assert_not_called()
    mock_store.ensure_graph_tables.assert_not_called()


@pytest.mark.asyncio
async def test_defref_none_withGraphOtherwiseEnabled_isNoop(
    connected_store, col_name, sample_py_file
):
    """Finding 9: `defref_extractor=None` with graph_store/graph_config otherwise
    fully enabled — the genuine "extras absent" no-op path. Distinct from
    test_defref_disabled_when_graph_disabled, which covers GraphConfig.enabled=False;
    here the extractor itself is simply not wired (e.g. `[code]` extra absent).
    """
    mock_store = _mock_graph_store()
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=None,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_store.write_graph.assert_not_called()
    mock_store.ensure_graph_tables.assert_not_called()


@pytest.mark.asyncio
async def test_midParseFailure_writesNoPartialEdges(
    connected_store, col_name, sample_py_file, monkeypatch
):
    """A file that parses successfully but fails during result assembly leaves zero
    edges written — atomic per-file (Major #16).

    Uses the real DefRefExtractor with a monkeypatched ``_build_result`` that raises
    *after* ``_parse_and_walk`` completes, simulating a mid-extraction abort with
    internal partial work that never reaches ``write_graph``.
    """
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.defref_extractor import DefRefExtractor

    def _explode_build_result(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated mid-extraction failure after AST walk")

    monkeypatch.setattr(DefRefExtractor, "_build_result", _explode_build_result)

    mock_store = _mock_graph_store()
    graph_config = GraphConfig(enabled=True)
    defref_extractor = DefRefExtractor(graph_store=mock_store)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=defref_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    mock_store.write_graph.assert_not_called()
    mock_store.ensure_graph_tables.assert_not_called()
    assert any("DefRef extraction failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_defref_warningsFromExtractSurfaceInIngestResult(
    connected_store, col_name, sample_py_file
):
    """Finding 11: warnings returned by extract() (e.g. grammar-missing,
    parse-failure-degraded-to-empty) must be surfaced in IngestResult.warnings,
    not silently dropped.
    """
    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            mentions=[],
            warnings=["tree-sitter grammar unavailable for .py; def/ref extraction skipped"],
        )
    )
    mock_store = _mock_graph_store()
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        defref_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    result = await pipeline.ingest_file(sample_py_file, col_name, embedder=_make_embedder())

    assert result.status == "ok"
    assert any("tree-sitter grammar unavailable" in w for w in result.warnings)

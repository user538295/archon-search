"""Regression (2026-08-19-030): a failing spaCy model load must not abort ingest.

Graph extraction is an auxiliary write (CLAUDE.md: "Auxiliary writes never fail
their primary operation").  When ``en_core_web_sm`` cannot be loaded — the normal
state in a pip-less ``uv tool install`` venv — ``ingest_file`` currently returns
``status="error"`` before embed/persist, so nothing is indexed at all (production:
24 documents persisted out of 1,325 walked files).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_store import GraphStore

pytestmark = pytest.mark.integration


def _make_embedder():
    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    return Embedder(_MockEmbedderBackend())


def _make_pipeline_with_graph(store, graph_extractor, graph_store, graph_config):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    # T21: reuse the single `_make_embedder()` factory rather than a second,
    # byte-identical `_MockEmbedderBackend` — this pipeline-level embedder was
    # unused dead code (every test call passes its own `embedder=` to
    # `ingest_file`, overriding it).
    return SearchPipeline(
        store=store,
        embedder=_make_embedder(),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )


@pytest.mark.asyncio
async def test_ingest_file_persists_when_spacy_model_load_fails(tmp_path: Path):
    """Model-load failure degrades to a warning; the file still embeds and persists."""
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    graph_extractor = GraphExtractor(GraphConfig(enabled=True))
    # Pip-less venv: `spacy.cli.download` finds no package installer, so the load
    # raises and `self._nlp` stays None.
    graph_extractor._load_nlp_sync = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError(
            "spaCy model download failed (no package installer found; exit code 1)."
        )
    )

    pipeline = _make_pipeline_with_graph(
        store, graph_extractor, graph_store, GraphConfig(enabled=True)
    )

    md_file = tmp_path / "notes.md"
    md_file.write_text(
        "# Release notes\n\n"
        "Alice from Acme Corp reviewed the retrieval pipeline in London.\n"
        "Bob confirmed the reranker thresholds before the rollout.\n"
    )

    collection = "test_bug030_degrade"
    try:
        # spaCy itself is importable (the [graph] extra is installed); only the
        # model is missing.
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            result = await pipeline.ingest_file(
                md_file, collection, embedder=_make_embedder()
            )

        assert result.status == "ok", (
            "A missing spaCy model is an auxiliary failure and must not fail the ingest; "
            f"got status={result.status!r} error={result.error!r}"
        )
        assert result.error is None, (
            f"status='ok' must not carry a leftover error; got {result.error!r}"
        )
        assert result.chunks_created > 0, (
            f"Expected chunks to be persisted, got chunks_created={result.chunks_created}"
        )
        assert any("en_core_web_sm" in w for w in result.warnings), (
            f"Expected the degradation warning to reach the caller; got {result.warnings!r}"
        )

        # T13: the brief's actual stated production symptom is "the collection
        # ends empty" — a real read-back against the live SearchStore, not just
        # the IngestResult the code under test constructed itself.
        stored_chunks = await store.get_chunks_for_doc(collection, result.doc_id)
        assert len(stored_chunks) == result.chunks_created, (
            f"Expected {result.chunks_created} persisted rows to be retrievable "
            f"from the store, got {len(stored_chunks)}"
        )
        assert any("Acme Corp" in row["text"] for row in stored_chunks), (
            "expected the ingested prose text to actually be retrievable, not just "
            f"counted; got texts: {[row['text'] for row in stored_chunks]}"
        )
    finally:
        await store.disconnect()
        await graph_store.disconnect()


@pytest.mark.asyncio
async def test_ingest_file_degrade_skips_llm_gate_and_preserves_code_symbols(
    tmp_path: Path,
) -> None:
    """T15: the degraded path must not fire the LLM-enrichment gate, and
    code-symbol extraction (which does not depend on spaCy) must survive.

    A refactor that moved the LLM-enrichment gate ahead of the spaCy-NER
    degrade check would fire one LLM call per chunk on every degraded ingest
    — this pins that it does not, in addition to the code-symbol survival
    already covered at the unit level (test_graph_extractor.py).
    """
    from unittest.mock import AsyncMock

    from archon_search.graph_extractor import GraphExtractor
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    # AND-gate fully open (provider + extraction_model + enrichment_client) —
    # if the degrade path failed to skip it, this mock would be awaited. Not a
    # raising side_effect: `extract()` catches per-chunk enrichment exceptions
    # and turns them into a warning, so a raise here would be silently
    # absorbed rather than failing the test — `assert_not_awaited()` below is
    # the real assertion.
    mock_enrichment_client = MagicMock()
    mock_enrichment_client.label_relationships = AsyncMock(return_value=[])
    graph_config = GraphConfig(
        enabled=True, provider="llama_cpp", extraction_model="model-x"
    )
    graph_extractor = GraphExtractor(graph_config, enrichment_client=mock_enrichment_client)
    graph_extractor._load_nlp_sync = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError(
            "spaCy model download failed (no package installer found; exit code 1)."
        )
    )

    pipeline = _make_pipeline_with_graph(store, graph_extractor, graph_store, graph_config)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    py_file = source_dir / "handler.py"
    py_file.write_text(
        "def parse_config():\n"
        "    return {}\n"
    )

    collection = "test_bug030_degrade_llm_gate"
    try:
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            result = await pipeline.ingest_file(
                py_file,
                collection,
                embedder=_make_embedder(),
                collection_root=source_dir,
            )

        assert result.status == "ok", f"ingest failed: {result.error}"
        mock_enrichment_client.label_relationships.assert_not_awaited()

        nodes = await graph_store.find_nodes_by_name(collection, ["parse_config"], "default")
        assert nodes, "code-symbol node must survive the degraded (no-NER) ingest"
        mentions = await graph_store.get_mentions_for_entity_ids(
            collection, [n.id for n in nodes], "default"
        )
        assert mentions, "code-symbol mention must survive the degraded ingest"
    finally:
        await store.disconnect()
        await graph_store.disconnect()


@pytest.mark.asyncio
async def test_reingest_under_degradation_leaves_no_orphan_prose_edges(tmp_path: Path):
    """C2-I-2: degrading must remove the doc's prose graph, not half of it.

    Degrade-not-abort has a second-order cost the fix itself did not address.
    On the degraded path `_extraction_result.nodes`/`.edges` are empty for a
    prose-only document, so the `if nodes or edges:` guard skips `write_graph`
    — but `delete_mentions_by_doc` still runs. The previous ingest's prose
    edges therefore survived with zero mentions to support them, asserting
    relations for a document that now records mentioning nothing. Worse,
    mention-based orphan GC bails on an empty mentions table, so nothing ever
    cleaned it up.
    """
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()
    graph_store = GraphStore(db_path)
    await graph_store.connect()

    graph_extractor = GraphExtractor(GraphConfig(enabled=True))
    pipeline = _make_pipeline_with_graph(
        store, graph_extractor, graph_store, GraphConfig(enabled=True)
    )

    md_file = tmp_path / "notes.md"
    md_file.write_text("Alice from Acme Corp met Bob in London.\n")
    collection = "test_bug030_reingest"
    namespace = "default"

    try:
        # First pass: a WORKING model produces prose nodes, edges and mentions.
        working_nlp = MagicMock()
        graph_extractor._nlp = working_nlp
        entities = [("Alice", "PERSON"), ("Acme Corp", "ORG"), ("London", "GPE")]
        with patch.object(
            GraphExtractor, "_run_ner_sync", return_value=[entities]
        ), patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            first = await pipeline.ingest_file(
                md_file, collection, embedder=_make_embedder()
            )
        assert first.status == "ok"

        edges_before = await graph_store.edge_count(collection, ns=namespace)
        assert edges_before > 0, "the working-model pass must produce prose edges to orphan"

        # Second pass: same file, model now unavailable. The latch degrades.
        graph_extractor._nlp = None
        graph_extractor._nlp_unavailable = False
        graph_extractor._load_nlp_sync = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("model gone")
        )
        md_file.write_text("Alice from Acme Corp met Bob in London. Revised.\n")
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            second = await pipeline.ingest_file(
                md_file, collection, embedder=_make_embedder()
            )

        assert second.status == "ok"
        assert any("en_core_web_sm" in w for w in second.warnings)

        edges_after = await graph_store.edge_count(collection, ns=namespace)
        assert edges_after == 0, (
            "prose edges from the previous ingest survived a degraded re-ingest "
            "with no mentions left to support them; the document's graph "
            f"contribution must be consistently code-symbols-only. Got {edges_after} "
            f"edge(s), was {edges_before}"
        )
    finally:
        await store.disconnect()
        await graph_store.disconnect()

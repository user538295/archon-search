"""Integration test for E2f BE-5: post-ingest synonym enrichment hook.

Tests:
- test_post_ingest_synonym_enrichment_fires_and_creates_edges: full ingest via
  real SearchStore + SearchPipeline (graph_enabled=True) + spaCy stub → trigger
  enrichment callback → synonym_of edge exists in graph store.

Strategy:
- Use a real SearchStore + SearchPipeline (no HTTP layer needed — the callback
  fires synchronously within ingest_file via on_synonym_edges_written).
- Install a spaCy stub (same pattern as test_e1a_be5_ingest_graph_integration.py).
- Patch GraphExtractor.extract() to return two CONCEPT nodes with pre-set
  name_embeddings whose cosine similarity exceeds the default threshold (0.85).
- Call pipeline.ingest_file() directly.
- Assign schedule_synonym_enrichment callback from a stub that directly calls
  _run_synonym_enrichment synchronously.
- Verify synonym_of edges exist in the real GraphStore afterward.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _make_pipeline_with_graph(store, graph_extractor, graph_store, graph_config):
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
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )


def _sha256_doc_id(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest() + "-000001"


def _install_spacy_stub():
    """Install minimal spaCy stubs (same pattern as existing integration tests)."""
    import types

    spacy_mod = types.ModuleType("spacy")
    spacy_mod.load = MagicMock()

    lang_mod = types.ModuleType("spacy.lang")
    en_mod = types.ModuleType("spacy.lang.en")
    en_mod.STOP_WORDS = frozenset()
    spacy_mod.lang = lang_mod
    lang_mod.en = en_mod

    sys.modules.setdefault("spacy", spacy_mod)
    sys.modules.setdefault("spacy.lang", lang_mod)
    sys.modules.setdefault("spacy.lang.en", en_mod)


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_ingest_synonym_enrichment_fires_and_creates_edges(
    tmp_path: Path, monkeypatch
):
    """Ingest → on_synonym_edges_written callback → synonym_of edge in graph store.

    The callback fires synchronously during ingest_file().  To exercise the
    full end-to-end path without an HTTP layer or a background MaintenanceLoop,
    the on_synonym_edges_written slot is assigned a thin wrapper that runs
    _run_synonym_enrichment synchronously via asyncio.ensure_future.

    After ingest completes and the enrichment task finishes, we verify that
    at least one synonym_of edge exists in the real GraphStore.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphExtractionResult,
        GraphNode,
        RelationshipType,
        make_stable_entity_id,
    )
    from archon_search.store import SearchStore

    _install_spacy_stub()
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    db_path = str(tmp_path / "search")
    gs = GraphStore(db_path)
    await gs.connect()

    store = SearchStore(db_path)
    await store.connect()

    col = "docs"
    ns = "default"

    # Two nodes with near-identical embeddings (cosine sim ≈ 0.9999) → synonym pair
    # Must exceed default synonym_threshold = 0.85
    _emb_a = [0.9, 0.1, 0.0, 0.0]
    _emb_b = [0.89, 0.11, 0.0, 0.0]

    node_a = GraphNode(
        id=make_stable_entity_id("concept", "authservice"),
        entity_name="AuthService",
        entity_type=EntityType.concept,
        source_doc_id=_sha256_doc_id("authservice"),
        collection_name=col,
        name_embedding=_emb_a,
    )
    node_b = GraphNode(
        id=make_stable_entity_id("concept", "auth service"),
        entity_name="Auth Service",
        entity_type=EntityType.concept,
        source_doc_id=_sha256_doc_id("auth service"),
        collection_name=col,
        name_embedding=_emb_b,
    )

    stub_extraction = GraphExtractionResult(
        nodes=[node_a, node_b],
        edges=[],
        mentions=[],
    )

    graph_config = GraphConfig()
    graph_config.enabled = True
    graph_config.enrichment_auto = True

    # Stub extractor always returns our pre-built nodes
    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(return_value=stub_extraction)

    pipeline = _make_pipeline_with_graph(
        store, mock_extractor, gs, graph_config
    )

    # Track enrichment tasks spawned by the callback
    enrichment_tasks: list[asyncio.Task] = []

    # Build a minimal config carrier for SynonymDetector
    from archon_search.config import SearchConfig
    cfg = SearchConfig()
    cfg.graph = graph_config

    # Synonym detector needs an embedder even though detect() won't call embed()
    # (stored name_embedding takes precedence)
    embedder = _make_embedder()

    from archon_search.synonym_detector import SynonymDetector

    async def _do_enrichment(collection: str, namespace: str) -> None:
        detector = SynonymDetector(
            graph_store=gs,
            embedder=embedder,
            config=cfg,
        )
        synonym_edges = await detector.detect(collection, ns=namespace)
        if synonym_edges:
            await gs.write_graph(collection, [], synonym_edges, ns=namespace)

    def _schedule_enrichment(collection: str, namespace: str) -> None:
        task = asyncio.create_task(_do_enrichment(collection, namespace))
        enrichment_tasks.append(task)

    # Wire the callback
    pipeline.on_synonym_edges_written = _schedule_enrichment

    # Write a sample file to ingest
    doc_file = tmp_path / "doc.md"
    doc_file.write_text(
        "# Authentication\n\nAuthService handles auth. Auth Service is the gateway.\n"
    )

    await store.ensure_collection(col, embedding_dim=4)

    result = await pipeline.ingest_file(
        doc_file,
        collection=col,
        namespace=ns,
        embedder=_make_embedder(),
    )

    assert result.status == "ok", f"Ingest failed: {result}"

    # Wait for enrichment tasks to complete
    if enrichment_tasks:
        await asyncio.gather(*enrichment_tasks, return_exceptions=True)

    # Verify synonym_of edges were written to the graph store
    edges = await gs.get_all_edges(col, ns=ns)
    synonym_edges = [
        e for e in edges
        if str(e.relationship_type) in ("synonym_of", "RelationshipType.synonym_of")
    ]

    assert len(synonym_edges) >= 1, (
        f"Expected at least one synonym_of edge after enrichment; "
        f"got {len(edges)} total edges. "
        f"Nodes in store: {await gs.get_all_nodes(col, ns=ns)}"
    )

    await gs.disconnect()
    await store.disconnect()

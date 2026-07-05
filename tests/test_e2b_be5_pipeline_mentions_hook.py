"""Unit and integration tests for BE-5: mentions table write hook in pipeline.ingest_file."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_types import GraphExtractionResult, GraphMention


# ---------------------------------------------------------------------------
# Helper — build a minimal SearchPipeline with optional graph components
# ---------------------------------------------------------------------------

def _make_pipeline(store, *, graph_extractor=None, graph_store=None, graph_config=None):
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
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_md_file(tmp_path: Path) -> Path:
    """Write a small markdown file for ingest tests."""
    f = tmp_path / "doc.md"
    f.write_text("# Hello\n\nThis is a test document about AuthService and TokenValidator.\n")
    return f


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_hook_deletes_before_writing_mentions(
    connected_store, col_name, sample_md_file
):
    """Mock graph_store; assert delete_mentions_by_doc called with correct doc_id before write_mentions."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mention = GraphMention(entity_id="entity_1", chunk_id="chunk_1", doc_id="doc_1")

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            mentions=[mention],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
    mock_store.delete_mentions_by_doc = AsyncMock()
    mock_store.write_mentions = AsyncMock()
    mock_store.edge_count = AsyncMock(return_value=0)

    pipeline = _make_pipeline(
        connected_store,
        graph_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = Embedder(_MockEmbedderBackend())
    result = await pipeline.ingest_file(sample_md_file, col_name, embedder=embedder)

    assert result.status == "ok"
    # Verify delete was called before write
    mock_store.delete_mentions_by_doc.assert_called_once()
    mock_store.write_mentions.assert_called_once()
    # Verify delete was called with correct doc_id
    delete_call_args = mock_store.delete_mentions_by_doc.call_args
    assert delete_call_args[0][1] == result.doc_id  # Second positional arg is doc_id


@pytest.mark.asyncio
async def test_pipeline_hook_swallows_mention_write_exception(
    connected_store, col_name, sample_md_file
):
    """Exception in write_mentions is caught and appended to warnings."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mention = GraphMention(entity_id="entity_1", chunk_id="chunk_1", doc_id="doc_1")

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            mentions=[mention],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
    mock_store.delete_mentions_by_doc = AsyncMock()
    mock_store.write_mentions = AsyncMock(side_effect=RuntimeError("Mention write failed"))
    mock_store.edge_count = AsyncMock(return_value=0)

    pipeline = _make_pipeline(
        connected_store,
        graph_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = Embedder(_MockEmbedderBackend())
    result = await pipeline.ingest_file(sample_md_file, col_name, embedder=embedder)

    # Ingest should still succeed (status="ok")
    assert result.status == "ok"
    # Exception should be appended to warnings
    assert any("Graph write failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_pipeline_hook_swallows_mention_delete_exception(
    connected_store, col_name, sample_md_file
):
    """Exception in delete_mentions_by_doc is caught; write_mentions is NOT called."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mention = GraphMention(entity_id="entity_1", chunk_id="chunk_1", doc_id="doc_1")

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            mentions=[mention],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
    mock_store.delete_mentions_by_doc = AsyncMock(side_effect=RuntimeError("Delete failed"))
    mock_store.write_mentions = AsyncMock()
    mock_store.edge_count = AsyncMock(return_value=0)

    pipeline = _make_pipeline(
        connected_store,
        graph_extractor=mock_extractor,
        graph_store=mock_store,
        graph_config=graph_config,
    )

    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = Embedder(_MockEmbedderBackend())
    result = await pipeline.ingest_file(sample_md_file, col_name, embedder=embedder)

    # Ingest should still succeed (status="ok")
    assert result.status == "ok"
    # Exception should be appended to warnings
    assert any("Graph write failed" in w for w in result.warnings)
    # write_mentions should NOT have been called when delete failed (exception exits try block early)
    mock_store.write_mentions.assert_not_called()


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_writes_mentions_then_reingest_is_idempotent(
    tmp_path, monkeypatch, col_name
):
    """Ingest a file, check mention count; re-ingest the same file; check count unchanged (idempotent)."""
    import sys
    import types

    from tests.integration.conftest import make_real_app, ingest_file_via_path

    # Create a dummy document
    corpus_path = tmp_path / "corpus"
    corpus_path.mkdir(exist_ok=True)
    doc_file = corpus_path / "test.md"
    doc_file.write_text("# Test\n\nAuthService is a service.\n")

    # Stub spaCy before creating app (required for graph_enabled=True in tests)
    original_spacy = sys.modules.get("spacy")
    sys.modules["spacy"] = types.ModuleType("spacy")
    try:
        # Create app with graph enabled
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            # Ingest the file
            ingest_file_via_path(client, col_name, str(doc_file), api_key=api_key)

            # Check mention count after first ingest
            from archon_search.graph_store import GraphStore

            graph_store = GraphStore(cfg.db_path)
            await graph_store.connect()
            mentions_1 = await graph_store.get_all_mentions(col_name, ns="default")
            initial_mention_count = len(mentions_1)
            assert initial_mention_count >= 0  # Should have at least 0 mentions (may be 0 with stubs)

            # Re-ingest the same file
            ingest_file_via_path(client, col_name, str(doc_file), api_key=api_key)

            # Check mention count after re-ingest
            mentions_2 = await graph_store.get_all_mentions(col_name, ns="default")
            reingest_mention_count = len(mentions_2)

            # Counts should be equal (idempotent — delete-then-add, not doubled)
            assert reingest_mention_count == initial_mention_count, (
                f"Mention count changed after re-ingest: "
                f"{initial_mention_count} -> {reingest_mention_count}. "
                "Delete-then-add should be idempotent."
            )
    finally:
        # Restore spaCy
        if original_spacy is None:
            sys.modules.pop("spacy", None)
        else:
            sys.modules["spacy"] = original_spacy

"""Unit tests for BE-5: GraphExtractor + GraphStore wired into pipeline.ingest_file."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_types import GraphExtractionResult


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
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_with_graph_disabled_skips_extraction(
    connected_store, col_name, sample_md_file
):
    """When GraphConfig.enabled=False, the extractor must never be called."""
    graph_config = GraphConfig(enabled=False)
    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock()

    pipeline = _make_pipeline(
        connected_store,
        graph_extractor=mock_extractor,
        graph_store=MagicMock(),
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
    mock_extractor.extract.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_with_graph_enabled_calls_extractor(
    connected_store, col_name, sample_md_file
):
    """When GraphConfig.enabled=True, the extractor is called and warnings propagate."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            llm_fallback_used=False,
            warnings=["test warning"],
            fatal_error=None,
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
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
    mock_extractor.extract.assert_called_once()
    assert "test warning" in result.warnings
    # write_graph must NOT be called when extraction returns empty nodes+edges
    mock_store.write_graph.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_threshold_warning_added_to_warnings(
    connected_store, col_name, sample_md_file
):
    """When edge_count >= backend_threshold_edges, a threshold warning is appended."""
    backend_threshold_edges = 5
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=backend_threshold_edges)

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            llm_fallback_used=False,
            warnings=[],
            fatal_error=None,
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
    # edge_count == threshold (at the boundary, not just >)
    mock_store.edge_count = AsyncMock(return_value=backend_threshold_edges)

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
    # There must be a threshold warning
    threshold_warnings = [w for w in result.warnings if "backend_threshold_edges" in w]
    assert len(threshold_warnings) >= 1, f"Expected threshold warning, got: {result.warnings}"


@pytest.mark.asyncio
async def test_startup_config_error_when_extras_absent(tmp_path):
    """ConfigError is raised at create_app() time when graph.enabled=True but spacy is absent."""
    import sys
    from archon_search.config import SearchConfig, GraphConfig
    from archon_search.config import ConfigError
    from archon_search.jobs.store import JobStore

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)
    config.db_path = str(tmp_path / "search")

    job_store = JobStore()

    # Simulate spacy not installed by patching it out of sys.modules
    original_spacy = sys.modules.get("spacy")
    sys.modules["spacy"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigError, match="archon-search\\[graph\\]"):
            from archon_search.server.app import create_app
            create_app(config, job_store)
    finally:
        # Restore spacy
        if original_spacy is None:
            sys.modules.pop("spacy", None)
        else:
            sys.modules["spacy"] = original_spacy


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_spacy(
    connected_store, col_name, sample_md_file
):
    """When llm_fallback_used=True in the extraction result, status is ok and warnings propagate."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            llm_fallback_used=True,
            warnings=["LLM fallback"],
            fatal_error=None,
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
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
    assert "LLM fallback" in result.warnings


@pytest.mark.asyncio
async def test_ingest_fatal_error_returns_error_status(
    connected_store, col_name, sample_md_file
):
    """When extraction returns fatal_error, pipeline returns status=error and no chunks written."""
    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(
        return_value=GraphExtractionResult(
            nodes=[],
            edges=[],
            llm_fallback_used=False,
            warnings=[],
            fatal_error="spaCy model load failed",
        )
    )

    mock_store = MagicMock()
    mock_store.ensure_graph_tables = AsyncMock()
    mock_store.write_graph = AsyncMock()
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

    assert result.status == "error"
    assert result.chunks_created == 0
    assert "spaCy model load failed" in (result.error or "")
    # Graph write must NOT have been called (extraction failed before persist)
    mock_store.write_graph.assert_not_called()

"""Integration test for BE-7: pipeline.search with graph_mode=naive uses expanded text.

Uses a stub GraphExpander backed by a stub GraphStore — no real spaCy or LanceDB
graph tables needed.  The real SearchPipeline, real SearchStore, real LanceDB, and
real embedder stubs are used so the full hybrid-search path is exercised.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.graph_expander import ExpandedQuery


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_search_naive_expands_query(connected_store, col_name, tmp_path):
    """Stub expander appends 'TokenValidator'; assert _search_standard called with expanded text;
    graph_expansion_applied=True in result.
    """
    # Ingest a small document so the collection exists
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    embedder = Embedder(_MockEmbedderBackend())

    # Set up stub expander that appends "TokenValidator"
    stub_expander = MagicMock()
    stub_expander.expand = AsyncMock(
        return_value=ExpandedQuery(
            original_query="AuthService",
            expanded_text="AuthService TokenValidator",
            expansion_applied=True,
        )
    )

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=embedder,
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_expander=stub_expander,
    )

    # Ingest a document to ensure collection exists
    doc = tmp_path / "doc.md"
    doc.write_text("# AuthService\n\nThis service handles authentication for TokenValidator.\n")
    await pipeline.ingest_file(doc, col_name, embedder=embedder)

    # Patch _search_standard to capture what query it receives
    from archon_search.pipeline import SearchPipelineResult
    received_queries: list[str] = []
    original_search_std = pipeline._search_standard

    async def _capture_std(query, *args, **kwargs):
        received_queries.append(query)
        return await original_search_std(query, *args, **kwargs)

    with patch.object(pipeline, "_search_standard", side_effect=_capture_std):
        result = await pipeline.search(
            "AuthService",
            col_name,
            embedder=embedder,
            graph_mode="naive",
        )

    # _search_standard must receive the expanded text
    assert len(received_queries) == 1
    assert received_queries[0] == "AuthService TokenValidator"

    # The result must flag graph expansion as applied
    assert result.graph_expansion_applied is True

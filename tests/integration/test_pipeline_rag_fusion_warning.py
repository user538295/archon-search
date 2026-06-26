"""tests/integration/test_pipeline_rag_fusion_warning.py — BE-2 integration test.

Integration test for SearchPipelineResult.rag_fusion_warning propagation.
Uses a real SearchPipeline with monkeypatched generate_variants() raising
asyncio.TimeoutError to verify the field is set.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_search_pipeline_rag_fusion_fallback_signal(tmp_path, monkeypatch):
    """Real SearchPipeline with monkeypatched generate_variants() raising asyncio.TimeoutError.

    Asserts that the pipeline propagates the exception (instead of swallowing it)
    and populates rag_fusion_warning on the result.
    """
    from archon_search.config import RAGFusionConfig
    from archon_search.pipeline import SearchPipelineResult

    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    # Ingest a document to have something to search.
    col = "rag-fusion-warning-test"
    doc = tmp_path / "test_doc.md"
    doc.write_text("# Integration Test\n\nThis content is used for RAG Fusion warning test.\n" * 10)
    await pipeline.ingest_file(doc, col, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(side_effect=asyncio.TimeoutError())
    rag_fusion_config = RAGFusionConfig(enabled=True)

    result = await pipeline.search(
        "integration test content",
        col,
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_fusion_config,
    )

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is not None, (
        "Expected rag_fusion_warning to be set when generate_variants raises TimeoutError"
    )
    assert "RAG Fusion timed out" in result.rag_fusion_warning, (
        f"Expected 'RAG Fusion timed out' in warning, got: {result.rag_fusion_warning!r}"
    )

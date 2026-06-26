"""tests/pipeline/test_pipeline_rag_fusion_warning.py — BE-2 unit tests.

Tests for SearchPipelineResult.rag_fusion_warning field and the pipeline
capturing asyncio.TimeoutError / generic Exception from RAG Fusion.

BE-2 critical requirement: generate_variants() must re-raise asyncio.TimeoutError
(and other exceptions) so the pipeline can distinguish failure from empty-variant
success. Tests here verify propagation via monkeypatched generate_variants().
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.pipeline import SearchPipelineResult

from .conftest import make_embedder, make_pipeline, make_reranker


# ---------------------------------------------------------------------------
# Unit tests: SearchPipelineResult.rag_fusion_warning field existence
# ---------------------------------------------------------------------------


def test_search_pipeline_result_has_rag_fusion_warning_field():
    """SearchPipelineResult must have rag_fusion_warning: str | None with default None."""
    result = SearchPipelineResult(results=[], acl_filtered=False)
    assert hasattr(result, "rag_fusion_warning")
    assert result.rag_fusion_warning is None


def test_search_pipeline_result_rag_fusion_warning_can_be_set():
    """SearchPipelineResult.rag_fusion_warning can be set to a string."""
    result = SearchPipelineResult(
        results=[],
        acl_filtered=False,
        rag_fusion_warning="RAG Fusion timed out",
    )
    assert result.rag_fusion_warning == "RAG Fusion timed out"


# ---------------------------------------------------------------------------
# Unit tests: pipeline.search() — rag_fusion_warning on success / failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_result_rag_fusion_warning_none_on_success(
    connected_store, col_name, tmp_path
):
    """When RAG Fusion succeeds (returns variants), rag_fusion_warning must be None."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)

    # Ingest a document so there is something to search.
    doc = tmp_path / "doc.md"
    doc.write_text("# Searchable content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(
        return_value=["variant one", "variant two"]
    )
    rag_fusion_config = RAGFusionConfig(enabled=True)

    result = await pipeline.search(
        "searchable content",
        col_name,
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_fusion_config,
    )

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is None


@pytest.mark.asyncio
async def test_pipeline_result_rag_fusion_warning_set_on_timeout(
    connected_store, col_name, tmp_path
):
    """When generate_variants raises asyncio.TimeoutError, rag_fusion_warning contains 'RAG Fusion timed out'."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)

    doc = tmp_path / "doc.md"
    doc.write_text("# Searchable content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(side_effect=asyncio.TimeoutError())
    rag_fusion_config = RAGFusionConfig(enabled=True)

    result = await pipeline.search(
        "searchable content",
        col_name,
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_fusion_config,
    )

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is not None
    assert "RAG Fusion timed out" in result.rag_fusion_warning


@pytest.mark.asyncio
async def test_pipeline_result_rag_fusion_warning_set_on_api_error(
    connected_store, col_name, tmp_path
):
    """When generate_variants raises a generic Exception, rag_fusion_warning is non-null and does not contain 'timed out'."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)

    doc = tmp_path / "doc.md"
    doc.write_text("# Searchable content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(
        side_effect=RuntimeError("API failure")
    )
    rag_fusion_config = RAGFusionConfig(enabled=True)

    result = await pipeline.search(
        "searchable content",
        col_name,
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_fusion_config,
    )

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is not None
    assert "timed out" not in result.rag_fusion_warning
    assert "RAG Fusion expansion failed" in result.rag_fusion_warning


# ---------------------------------------------------------------------------
# Unit tests: pipeline.search_many() — rag_fusion_warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_many_pipeline_result_expansion_warning_on_rag_fusion_failure(
    connected_store, col_name, tmp_path
):
    """When generate_variants fails inside search_many, result carries rag_fusion_warning."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)

    doc = tmp_path / "doc.md"
    doc.write_text("# Multi-collection content\n\nSearchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(side_effect=asyncio.TimeoutError())
    rag_fusion_config = RAGFusionConfig(enabled=True)

    result = await pipeline.search_many(
        "multi-collection content",
        collections=[col_name],
        namespace="default",
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_fusion_config,
    )

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is not None
    assert "RAG Fusion" in result.rag_fusion_warning, (
        f"Expected 'RAG Fusion' in warning, got: {result.rag_fusion_warning!r}"
    )


@pytest.mark.asyncio
async def test_search_many_embedding_failure_sets_rag_fusion_warning(
    connected_store, col_name, tmp_path
):
    """When the embedding gather step in search_many fails, rag_fusion_warning is set.

    This tests the Step B failure path (pipeline.py:1107-1116):
    generate_variants succeeds but the parallel embed of original+variants fails.
    The fallback then embeds just the original query (single call) which must succeed.
    """
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)

    doc = tmp_path / "doc.md"
    doc.write_text("# Multi-collection content\n\nSearchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    # generate_variants succeeds (returns variants) — this causes Step B to embed N+1 queries
    mock_generator.generate_variants = AsyncMock(
        return_value=["variant one", "variant two"]
    )
    rag_fusion_config = RAGFusionConfig(enabled=True)

    # Patch embed_one to fail only when gathering multiple queries in parallel
    # (N+1 queries = original + 2 variants = 3 total in the gather).
    # The fallback embeds just 1 query — allow that to succeed.
    original_embed_one = pipeline._global_embedder.embed_one
    call_count = [0]

    async def selective_failing_embed_one(text: str) -> list[float]:
        call_count[0] += 1
        # The gather calls embed_one for each of all_queries_rf (3 queries).
        # The fallback then calls embed_one once (for the original query).
        # Fail the first 3 calls (the gather), succeed from the 4th onwards.
        if call_count[0] <= 3:
            raise RuntimeError("Embedding service unavailable")
        return [0.1, 0.1, 0.1, 0.1]

    pipeline._global_embedder.embed_one = selective_failing_embed_one

    try:
        result = await pipeline.search_many(
            "multi-collection content",
            collections=[col_name],
            namespace="default",
            rag_fusion=True,
            rag_fusion_generator=mock_generator,
            rag_fusion_config=rag_fusion_config,
        )
    finally:
        pipeline._global_embedder.embed_one = original_embed_one

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is not None
    assert "RAG Fusion" in result.rag_fusion_warning


# ---------------------------------------------------------------------------
# Additional coverage: standard (non-RAG) path and search() embedding failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_result_rag_fusion_warning_none_when_rag_not_requested(
    connected_store, col_name, tmp_path
):
    """When RAG Fusion is not requested, rag_fusion_warning must be None."""
    pipeline = make_pipeline(connected_store)

    doc = tmp_path / "doc.md"
    doc.write_text("# Standard search\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    result = await pipeline.search(
        "standard search",
        col_name,
        embedder=pipeline._global_embedder,
        rag_fusion=False,
    )

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is None


@pytest.mark.asyncio
async def test_search_embedding_failure_sets_rag_fusion_warning(
    connected_store, col_name, tmp_path
):
    """When the embedding gather step in search() fails, rag_fusion_warning is set.

    This tests the Step 4 failure path in pipeline.search() (embedding gather failure).
    generate_variants succeeds, then the parallel embed of original+variants fails.
    The fallback embeds just the original query (single call) which succeeds.
    """
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)

    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_generator = MagicMock()
    # generate_variants succeeds with 2 variants — Step 4 embeds 3 queries (original + 2)
    mock_generator.generate_variants = AsyncMock(
        return_value=["variant one", "variant two"]
    )
    rag_fusion_config = RAGFusionConfig(enabled=True)

    original_embed_one = pipeline._global_embedder.embed_one
    call_count = [0]

    async def selective_failing_embed_one(text: str) -> list[float]:
        call_count[0] += 1
        # Fail the first 3 calls (the gather of original + 2 variants).
        # The fallback then calls embed_one once for the original query — allow that.
        if call_count[0] <= 3:
            raise RuntimeError("Embedding unavailable")
        return [0.1, 0.1, 0.1, 0.1]

    pipeline._global_embedder.embed_one = selective_failing_embed_one

    try:
        result = await pipeline.search(
            "search content",
            col_name,
            embedder=pipeline._global_embedder,
            rag_fusion=True,
            rag_fusion_generator=mock_generator,
            rag_fusion_config=rag_fusion_config,
        )
    finally:
        pipeline._global_embedder.embed_one = original_embed_one

    assert isinstance(result, SearchPipelineResult)
    assert result.rag_fusion_warning is not None
    assert "RAG Fusion expansion failed" in result.rag_fusion_warning

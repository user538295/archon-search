"""tests/pipeline/test_pipeline_rag_fusion_warning.py — BE-2 unit tests.

Tests for SearchPipelineResult.rag_fusion_warning field and the pipeline
capturing asyncio.TimeoutError / generic Exception from RAG Fusion.

The pipeline-level cases monkeypatch ``generate_variants()`` to raise, so the pipeline can distinguish a failure from
an empty-variant success. The real ``RAGFusionGenerator`` raises ``RAGFusionFailure`` (a typed reason, a fixed warning)
for every failed generation, and ``test_rag_failure_falls_back_to_single_query`` runs it over a failing client.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.llm_client.protocol import LlmCallFailure, LlmCallTimeout
from archon_search.pipeline import SearchPipelineResult

from .conftest import make_pipeline

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
    mock_generator.generate_variants = AsyncMock(side_effect=TimeoutError())
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
    mock_generator.generate_variants = AsyncMock(side_effect=TimeoutError())
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


# ---------------------------------------------------------------------------
# A failing shared client: the real generator raises a typed failure -> single-query search plus a fixed warning
# ---------------------------------------------------------------------------


def _fusion_over(client):
    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator

    config = RAGFusionConfig(enabled=True)
    return {"rag_fusion": True, "rag_fusion_generator": RAGFusionGenerator(config, client), "rag_fusion_config": config}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["search", "search_many"])
@pytest.mark.parametrize(
    ("failure", "warning"),
    [
        (LlmCallFailure("ollama", "connection refused"), "RAG Fusion expansion failed"),
        (LlmCallTimeout("ollama", "no answer in 10s"), "RAG Fusion timed out"),
    ],
    ids=["provider-failure", "timeout"],
)
async def test_rag_failure_falls_back_to_single_query(connected_store, col_name, tmp_path, caplog, path, failure, warning):
    """A client that raises LlmCallFailure gives rag_fusion_applied=False, the single-query results and a fixed warning.

    The generator reports the failure itself (one WARNING of its own); the pipeline's catch-all for an unexpected
    exception is not reached, so there is no "failed unexpectedly" log.
    """
    import logging

    from tests._hyde_stubs import ScriptedLlmClient

    pipeline = make_pipeline(connected_store)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)
    client = ScriptedLlmClient(failure=failure)
    fusion = _fusion_over(client)

    with caplog.at_level(logging.WARNING):
        if path == "search":
            with_fusion = await pipeline.search(
                "searchable document", col_name, embedder=pipeline._global_embedder, **fusion
            )
            plain = await pipeline.search("searchable document", col_name, embedder=pipeline._global_embedder)
        else:
            with_fusion = await pipeline.search_many("searchable document", [col_name], **fusion)
            plain = await pipeline.search_many("searchable document", [col_name])

    assert len(client.requests) == 1, "the generator must have been asked, and failed"
    assert with_fusion.rag_fusion_attempted is True
    assert with_fusion.rag_fusion_applied is False
    assert with_fusion.rag_fusion_queries_used == 0
    assert with_fusion.rag_fusion_warning == warning
    assert plain.rag_fusion_warning is None
    assert with_fusion.results, "the search must still answer"
    assert {r.chunk_id for r in with_fusion.results} == {r.chunk_id for r in plain.results}
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "archon_search.rag_fusion"
    ]
    assert len(warnings) == 1 and "RAG Fusion generation failed" in warnings[0], warnings
    assert not [r for r in caplog.records if "failed unexpectedly" in r.getMessage()], "the pipeline catch-all must not run"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["search", "search_many"])
@pytest.mark.parametrize("reply", ["   \n  ", "v1\nv2"], ids=["no-valid-variant", "success"])
async def test_rag_answer_without_variants_warns_and_a_good_answer_does_not(
    connected_store, col_name, tmp_path, path, reply
):
    from tests._hyde_stubs import ScriptedLlmClient

    pipeline = make_pipeline(connected_store)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)
    fusion = _fusion_over(ScriptedLlmClient(reply=reply))

    if path == "search":
        result = await pipeline.search("searchable document", col_name, embedder=pipeline._global_embedder, **fusion)
    else:
        result = await pipeline.search_many("searchable document", [col_name], **fusion)

    if reply.strip():
        assert (result.rag_fusion_applied, result.rag_fusion_queries_used) == (True, 2)
        assert result.rag_fusion_warning is None
    else:
        assert (result.rag_fusion_applied, result.rag_fusion_queries_used) == (False, 0)
        assert result.rag_fusion_warning == "RAG Fusion expansion failed"


@pytest.mark.asyncio
async def test_explain_reports_the_typed_reason_of_a_failed_generation(connected_store, col_name, tmp_path):
    """``rag_fusion_failure_reason`` is the reason value, not the exception class name."""
    from tests._hyde_stubs import ScriptedLlmClient

    pipeline = make_pipeline(connected_store)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)
    fusion = _fusion_over(ScriptedLlmClient(failure=LlmCallTimeout("ollama", "no answer in 10s")))

    result = await pipeline.explain("searchable document", col_name, embedder=pipeline._global_embedder, **fusion)

    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_failure_reason == "timeout"


@pytest.mark.asyncio
async def test_search_sets_a_warning_when_every_rag_fusion_search_fails(connected_store, col_name, tmp_path):
    """Variants were generated but no store search answered: that is a RAG Fusion failure too."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)
    generator = MagicMock()
    generator.generate_variants = AsyncMock(return_value=["variant one", "variant two"])
    real_search = connected_store.hybrid_search_with_trace
    calls: list[int] = []

    async def failing_then_working(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 3:  # the original query and both variants
            raise RuntimeError("store unavailable")
        return await real_search(*args, **kwargs)

    connected_store.hybrid_search_with_trace = failing_then_working
    try:
        result = await pipeline.search(
            "searchable document", col_name, embedder=pipeline._global_embedder,
            rag_fusion=True, rag_fusion_generator=generator, rag_fusion_config=RAGFusionConfig(enabled=True),
        )
    finally:
        connected_store.hybrid_search_with_trace = real_search

    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_warning == "RAG Fusion expansion failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["search", "search_many"])
async def test_a_warning_is_set_when_only_the_variant_searches_fail(connected_store, col_name, tmp_path, path):
    """The original query's search answers, every variant search fails: variants were generated and none was used."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)
    generator = MagicMock()
    generator.generate_variants = AsyncMock(return_value=["variant one", "variant two"])
    real_search = connected_store.hybrid_search_with_trace
    calls: list[int] = []

    async def original_only(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:  # the first call of the gather is the original query
            raise RuntimeError("store unavailable")
        return await real_search(*args, **kwargs)

    connected_store.hybrid_search_with_trace = original_only
    fusion = {
        "rag_fusion": True, "rag_fusion_generator": generator, "rag_fusion_config": RAGFusionConfig(enabled=True),
    }
    try:
        if path == "search":
            result = await pipeline.search("searchable document", col_name, embedder=pipeline._global_embedder, **fusion)
        else:
            result = await pipeline.search_many("searchable document", [col_name], **fusion)
    finally:
        connected_store.hybrid_search_with_trace = real_search

    assert result.results, "the original query's search still answers"
    assert (result.rag_fusion_attempted, result.rag_fusion_applied, result.rag_fusion_queries_used) == (True, False, 0)
    assert result.rag_fusion_warning == "RAG Fusion expansion failed"


@pytest.mark.asyncio
async def test_search_many_sets_no_warning_when_no_collection_has_a_vector_index(connected_store, col_name, tmp_path):
    """Variants generated but never searched (FTS-only collections) is not a failure."""
    from archon_search.config import RAGFusionConfig

    pipeline = make_pipeline(connected_store)
    doc = tmp_path / "doc.md"
    doc.write_text("# Search content\n\nThis is a searchable document.\n" * 10)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)
    generator = MagicMock()
    generator.generate_variants = AsyncMock(return_value=["variant one", "variant two"])
    real_has_vector_index = connected_store.has_vector_index

    async def no_vector_index(collection):
        return False

    connected_store.has_vector_index = no_vector_index
    try:
        result = await pipeline.search_many(
            "searchable document", [col_name],
            rag_fusion=True, rag_fusion_generator=generator, rag_fusion_config=RAGFusionConfig(enabled=True),
        )
    finally:
        connected_store.has_vector_index = real_has_vector_index

    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_warning is None

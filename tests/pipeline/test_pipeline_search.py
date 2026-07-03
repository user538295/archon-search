"""tests/pipeline/test_pipeline_search.py — Search tests for SearchPipeline.

Covers: search(), search_with_context(), explain() (single-collection), eval trace,
ACL filtering, filter+ACL warning behaviour, namespace collection meta, document list/delete,
embedder/reranker warmth, and embedding-model search routing. Moved from
tests/test_pipeline.py as part of C11 pipeline test split.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord, DocumentInfo, SearchResult
from archon_search.embedder import Embedder
from archon_search.reranker import Reranker

from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline


# ===========================================================================
# Basic search / search_with_context / delete / list_collections
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_search_returns_ranked_results(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "search_doc.md"
    md_file.write_text("# Search Test\n\nThis document contains searchable content.\n" * 10)

    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    result = await pipeline.search("searchable content", col_name, embedder=pipeline._global_embedder)

    from archon_search.pipeline import SearchPipelineResult
    assert isinstance(result, SearchPipelineResult)
    assert len(result.results) > 0
    assert all(isinstance(r, SearchResult) for r in result.results)


@pytest.mark.asyncio
async def test_pipeline_search_with_context_returns_neighbors(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    # Use small chunk_size to force multiple chunks
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline2 = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=32),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "ctx_doc.md"
    md_file.write_text("# Context Test\n\n" + ("Content chunk. " * 50))
    await pipeline2.ingest_file(md_file, col_name, embedder=pipeline2._global_embedder)

    results = await pipeline2.search_with_context("Content chunk", col_name, context_window=1, embedder=pipeline2._global_embedder)

    from archon_search.pipeline import SearchWithContextResult
    assert isinstance(results, SearchWithContextResult)
    assert len(results.results) > 0
    for item in results.results:
        assert "result" in item
        assert "context_before" in item
        assert "context_after" in item


@pytest.mark.asyncio
async def test_pipeline_delete_document(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "del_doc.md"
    md_file.write_text("# Delete Test\n\nContent to be deleted.\n" * 5)

    # Use ingest_directory so collection meta is written (required by delete_document namespace guard)
    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
    assert len(results) == 1
    result = results[0]
    assert result.status == "ok"

    deleted = await pipeline.delete_document(result.doc_id, col_name)
    assert deleted > 0

    docs, _, _ = await pipeline.list_documents(col_name)
    doc_ids = [d.doc_id for d in docs]
    assert result.doc_id not in doc_ids


@pytest.mark.asyncio
async def test_pipeline_list_collections_after_ingest(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "col_doc.md"
    md_file.write_text("# Collection Test\n\nSome content.\n" * 5)

    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    collections = await pipeline.list_collections()
    names = [c.name for c in collections]
    assert col_name in names


@pytest.mark.asyncio
async def test_pipeline_ingest_file_fts_searchable(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    unique_word = "xyzuniquekeyword123"
    md_file = tmp_path / "fts_doc.md"
    md_file.write_text(f"# FTS Test\n\nThis document contains {unique_word} for testing.\n" * 5)

    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    result = await pipeline.search(unique_word, col_name, embedder=pipeline._global_embedder)
    assert len(result.results) > 0


# ===========================================================================
# Stage instrumentation tests (B1 Task 3.5)
# ===========================================================================


@pytest.mark.asyncio
async def test_search_with_context_records_context_stage(tmp_path):
    """search_with_context records the 'context' stage when a recorder is bound."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.observability import bind_stage_recorder

    search_candidate = ScoredSearchCandidate(
        doc_id="a" * 64,
        chunk_id=("a" * 64) + "-000000",
        text="some text",
        source_path="/some/path",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="test-col",
    )

    class StubStore:
        async def hybrid_search_with_trace(self, collection: str, vector: Any, query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            return [search_candidate]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[ChunkRecord]:
            return []

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    with bind_stage_recorder() as recorder:
        await pipeline.search_with_context("query", "test-col", context_window=1, embedder=pipeline._global_embedder)

    assert "context" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["context"] >= 0.0


# ===========================================================================
# Unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_search_with_context_malformed_chunk_id(tmp_path):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    malformed_candidate = ScoredSearchCandidate(
        doc_id="a" * 64,
        chunk_id="bad-chunk-id",
        text="some text",
        source_path="/some/path",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="test-collection",
    )

    class MockStore:
        async def hybrid_search_with_trace(self, collection: str, vector: Any, query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            return [malformed_candidate]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[ChunkRecord]:
            return []

    pipeline = SearchPipeline(
        store=MockStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    # Pre-warm embedder dim
    await pipeline._global_embedder.embed(["warmup"])

    results = await pipeline.search_with_context("query", "test-collection", context_window=1, embedder=pipeline._global_embedder)

    from archon_search.pipeline import SearchWithContextResult
    assert isinstance(results, SearchWithContextResult)
    assert len(results.results) == 1
    assert results.results[0]["result"].chunk_id == "bad-chunk-id"
    assert results.results[0]["context_before"] == []
    assert results.results[0]["context_after"] == []


# ===========================================================================
# Task 2.1 — search() query_vector parameter
# ===========================================================================


@pytest.mark.asyncio
async def test_search_uses_provided_query_vector() -> None:
    """search() uses caller-provided query_vector; embed_one must NOT be called."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult
    from archon_search._types import SearchResult

    captured_vector: list[list[float]] = []

    class StubStore:
        async def hybrid_search_with_trace(self, collection: str, vector: list[float], query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            captured_vector.append(list(vector))
            return []

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    provided_vector = [0.9, 0.8, 0.7, 0.6]

    with patch.object(pipeline._global_embedder, "embed_one", new_callable=AsyncMock) as mock_embed:
        result = await pipeline.search(
            "some query",
            "test-col",
            embedder=pipeline._global_embedder,
            query_vector=provided_vector,
        )

    mock_embed.assert_not_called()
    assert len(captured_vector) == 1
    assert captured_vector[0] == provided_vector
    assert isinstance(result, SearchPipelineResult)


@pytest.mark.asyncio
async def test_search_embeds_when_no_query_vector() -> None:
    """search() calls embed_one when query_vector is None (pre-C4 behaviour)."""
    from unittest.mock import AsyncMock, patch
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return []

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    with patch.object(pipeline._global_embedder, "embed_one", new_callable=AsyncMock, return_value=[0.1, 0.2, 0.3, 0.4]) as mock_embed:
        await pipeline.search(
            "some query",
            "test-col",
            embedder=pipeline._global_embedder,
            query_vector=None,
        )

    mock_embed.assert_called_once_with("some query")


# ===========================================================================
# Exception propagation
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_search_embedder_exception_propagates(connected_store, col_name, tmp_path):
    """ embedder.embed_one raises during search → exception propagates to caller."""
    pipeline = make_pipeline(connected_store)

    class ExplodingBackend:
        model_name: str = "exploding-search"

        def encode(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("search embedder exploded")

    exploding_embedder = Embedder(ExplodingBackend())

    with pytest.raises(RuntimeError, match="search embedder exploded"):
        await pipeline.search("any query", col_name, embedder=exploding_embedder)


@pytest.mark.asyncio
async def test_pipeline_search_with_context_fetch_exception_propagates(tmp_path):
    """ fetch_adjacent_chunks raises → exception propagates to caller (current production behavior).

    Spec intent was: fetch_adjacent_chunks failure → logs, continues, returns result with empty context.
    Production code at pipeline.py:~235 has no try/except around fetch_adjacent_chunks(), so the
    exception propagates instead. This test pins the actual behavior as a regression guard.
    If graceful degradation is ever added, update this test to assert result-with-empty-context.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    hit = ScoredSearchCandidate(
        doc_id="a" * 64,
        chunk_id=("a" * 64) + "-000001",
        text="some result text",
        source_path="/some/path.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="test-col",
    )

    class FailingFetchStore:
        async def hybrid_search_with_trace(self, collection: str, vector: Any, query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            return [hit]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[Any]:
            raise RuntimeError("fetch_adjacent_chunks exploded")

    pipeline = SearchPipeline(
        store=FailingFetchStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    # Pre-warm embedder so embedding_dim is set
    await pipeline._global_embedder.embed(["warmup"])

    # Current production behavior: exception propagates to caller
    with pytest.raises(RuntimeError, match="fetch_adjacent_chunks exploded"):
        await pipeline.search_with_context("query", "test-col", context_window=1, embedder=pipeline._global_embedder)


# ===========================================================================
# Eval trace execution path
# ===========================================================================


def _make_scored_candidate(
    doc_id: str,
    chunk_id: str,
    text: str = "chunk text",
    rrf_score: float = 0.5,
    reranker_score: float | None = None,
) -> "ScoredSearchCandidate":
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
        source_path=f"/path/to/{doc_id}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=reranker_score,
        ),
        collection="test-col",
    )


@pytest.mark.asyncio
async def test_eval_trace_returns_pre_and_post_rerank_results(tmp_path):
    """collect_search_trace returns (pre_rerank, post_rerank) both as EvalSearchResult lists."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.eval.types import EvalSearchResult
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    doc_id = "a" * 64
    pre_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000", rrf_score=0.8)]
    post_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000", rrf_score=0.8, reranker_score=0.9)]

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    with (
        patch("archon_search.eval._tracing._hybrid_search_with_trace", new=AsyncMock(return_value=pre_candidates)),
        patch.object(pipeline._reranker, "rerank_candidates", new=AsyncMock(return_value=post_candidates)),
    ):
        pre, post = await collect_search_trace(
            pipeline, "test query", "test-col",
            candidate_depth=20, return_depth=5, metric_depth=10,
        )

    assert isinstance(pre, list)
    assert isinstance(post, list)
    assert len(pre) == 1
    assert len(post) == 1
    assert all(isinstance(r, EvalSearchResult) for r in pre)
    assert all(isinstance(r, EvalSearchResult) for r in post)


@pytest.mark.asyncio
async def test_eval_trace_uses_service_query_path_with_trace_enabled(tmp_path):
    """collect_search_trace calls the pipeline's own store and reranker trace helpers."""
    from unittest.mock import AsyncMock, MagicMock, patch, call

    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    doc_id = "b" * 64
    pre_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000")]
    post_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000", reranker_score=0.7)]

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    hybrid_mock = AsyncMock(return_value=pre_candidates)
    rerank_mock = AsyncMock(return_value=post_candidates)

    with (
        patch("archon_search.eval._tracing._hybrid_search_with_trace", new=hybrid_mock),
        patch.object(pipeline._reranker, "rerank_candidates", new=rerank_mock),
    ):
        await collect_search_trace(
            pipeline, "my query", "test-col",
            candidate_depth=15, return_depth=3, metric_depth=5,
        )

    # Verify the store instance passed to trace is the pipeline's own store
    hybrid_mock.assert_awaited_once()
    call_args = hybrid_mock.call_args
    assert call_args.args[0] is pipeline.store, "store instance must be pipeline's own store"
    assert call_args.args[1] == "test-col"
    assert call_args.args[3] == "my query"
    assert call_args.args[4] == 15  # candidate_depth

    # Verify reranker trace was called with return_depth
    rerank_mock.assert_awaited_once()
    assert rerank_mock.call_args.args[2] == 3  # return_depth


@pytest.mark.asyncio
async def test_eval_trace_does_not_call_private_rerank_with_trace(tmp_path):
    """collect_search_trace reranks via rerank_candidates, not the private alias."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    doc_id = "c" * 64
    pre_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000")]

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    alias_spy = AsyncMock(side_effect=pipeline._reranker._rerank_with_trace)

    with (
        patch("archon_search.eval._tracing._hybrid_search_with_trace", new=AsyncMock(return_value=pre_candidates)),
        patch.object(pipeline._reranker, "_rerank_with_trace", new=alias_spy),
    ):
        await collect_search_trace(
            pipeline, "my query", "test-col",
            candidate_depth=15, return_depth=3, metric_depth=5,
        )

    alias_spy.assert_not_called()


@pytest.mark.asyncio
async def test_eval_trace_fails_if_trace_path_diverges_from_search_components(tmp_path):
    """Drift guard raises RuntimeError when embedder/store/reranker instances differ."""
    from unittest.mock import MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    # Tamper: replace embedder with a different instance after construction
    original_embedder = pipeline._global_embedder
    pipeline._global_embedder = make_embedder()  # different object — drift!

    # The drift guard must detect that the pipeline's embedder changed
    # We simulate this by verifying object identity check is performed
    # by patching _get_pipeline_components to return mismatched objects
    from archon_search.eval._tracing import _check_component_drift

    with pytest.raises(RuntimeError, match="drift"):
        _check_component_drift(
            pipeline,
            expected_embedder=original_embedder,  # the original, now mismatched
            expected_store=pipeline.store,
            expected_reranker=pipeline._reranker,
        )


@pytest.mark.asyncio
async def test_eval_trace_matches_search_final_order_with_matching_depths(connected_store, col_name, tmp_path):
    """post_rerank output order matches normal search() when depths equal pipeline defaults."""
    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "trace_doc.md"
    md_file.write_text("# Trace Test\n\nSearchable content for eval trace matching.\n" * 10)
    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    normal_result_obj = await pipeline.search("Searchable content", col_name, embedder=pipeline._global_embedder)
    _, post_rerank = await collect_search_trace(
        pipeline, "Searchable content", col_name,
        candidate_depth=pipeline._top_k_retrieve,
        return_depth=pipeline._top_k_return,
        metric_depth=pipeline._top_k_return,
    )

    # post_rerank chunk_ids must match normal search order
    normal_chunk_ids = [r.chunk_id for r in normal_result_obj.results]
    trace_chunk_ids = [r.chunk_id for r in post_rerank]
    assert normal_chunk_ids == trace_chunk_ids, (
        f"Post-rerank trace order differs from search():\n"
        f"  search: {normal_chunk_ids}\n"
        f"  trace:  {trace_chunk_ids}"
    )


@pytest.mark.asyncio
async def test_eval_trace_common_prefix_matches_search_when_depths_differ(connected_store, col_name, tmp_path):
    """When eval depths differ from pipeline defaults, the common prefix of results matches."""
    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "prefix_doc.md"
    md_file.write_text("# Prefix Test\n\nContent for prefix comparison.\n" * 10)
    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    normal_result_obj = await pipeline.search("prefix comparison", col_name, embedder=pipeline._global_embedder)
    _, post_rerank = await collect_search_trace(
        pipeline, "prefix comparison", col_name,
        candidate_depth=5,   # different from pipeline default (10)
        return_depth=3,       # different from pipeline default (5)
        metric_depth=3,
    )

    # Compare only the common prefix (min of both result counts)
    normal_results = normal_result_obj.results
    prefix_len = min(len(normal_results), len(post_rerank))
    assert prefix_len > 0, "Expected at least one result"
    normal_prefix = [r.chunk_id for r in normal_results[:prefix_len]]
    trace_prefix = [r.chunk_id for r in post_rerank[:prefix_len]]
    assert normal_prefix == trace_prefix, (
        f"Common prefix mismatch:\n  search: {normal_prefix}\n  trace: {trace_prefix}"
    )


@pytest.mark.asyncio
async def test_eval_trace_does_not_change_public_search_response(connected_store, col_name, tmp_path):
    """Normal search() output is identical before and after collect_search_trace is called."""
    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "unchanged_doc.md"
    md_file.write_text("# Unchanged Test\n\nSearch results must not change.\n" * 10)
    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    before = await pipeline.search("Search results", col_name, embedder=pipeline._global_embedder)

    await collect_search_trace(
        pipeline, "Search results", col_name,
        candidate_depth=10, return_depth=5, metric_depth=5,
    )

    after = await pipeline.search("Search results", col_name, embedder=pipeline._global_embedder)

    assert [r.chunk_id for r in before.results] == [r.chunk_id for r in after.results]
    assert [r.score for r in before.results] == [r.score for r in after.results]


# ===========================================================================
# get_collection_meta namespace
# ===========================================================================


@pytest.mark.asyncio
async def test_get_collection_meta_namespace_param() -> None:
    """get_collection_meta forwards namespace to store.get_collection_meta."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    expected_meta = CollectionMeta(name="my-col", namespace="tenantA")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=expected_meta)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.get_collection_meta("my-col", namespace="tenantA")

    store.get_collection_meta.assert_awaited_once_with("my-col", namespace="tenantA")
    assert result is expected_meta


# ===========================================================================
# SearchPipelineResult return type for search
# ===========================================================================


@pytest.mark.asyncio
async def test_search_returns_pipeline_result(connected_store, col_name, tmp_path) -> None:
    """pipeline.search() returns a SearchPipelineResult instance, not a bare list."""
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult

    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "result_type_doc.md"
    md_file.write_text("# Type Test\n\nContent for return-type check.\n" * 10)
    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    result = await pipeline.search("Content for return-type", col_name, embedder=pipeline._global_embedder)

    assert isinstance(result, SearchPipelineResult)
    assert isinstance(result.results, list)
    assert all(isinstance(r, SearchResult) for r in result.results)


@pytest.mark.asyncio
async def test_search_acl_filtered_true_when_chunks_filtered(tmp_path) -> None:
    """When ACL filter removes candidates, acl_filtered=True in the result."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult
    from archon_search._types import ChunkRecord, SearchResult

    # A candidate with a restricted ACL (only "tenantX" allowed)
    restricted_candidate = ScoredSearchCandidate(
        doc_id="a" * 64,
        chunk_id=("a" * 64) + "-000000",
        text="secret content",
        source_path="/secret.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="test-col",
        acl=["tenantX"],  # not the default namespace
    )

    class AclFilterStore:
        async def hybrid_search_with_trace(self, collection: str, vector: Any, query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            return [restricted_candidate]

    pipeline = SearchPipeline(
        store=AclFilterStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    # Pre-warm embedder
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search("secret", "test-col", embedder=pipeline._global_embedder)

    assert isinstance(result, SearchPipelineResult)
    assert result.acl_filtered is True
    assert result.results == []  # all filtered out


@pytest.mark.asyncio
async def test_search_acl_filtered_false_when_all_pass(tmp_path) -> None:
    """When no ACL filtering occurs, acl_filtered=False in the result."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult

    # A candidate with no ACL restriction (acl=None → open)
    open_candidate = ScoredSearchCandidate(
        doc_id="b" * 64,
        chunk_id=("b" * 64) + "-000000",
        text="open content",
        source_path="/open.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="test-col",
        acl=None,
    )

    class OpenAclStore:
        async def hybrid_search_with_trace(self, collection: str, vector: Any, query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            return [open_candidate]

    pipeline = SearchPipeline(
        store=OpenAclStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search("open", "test-col", embedder=pipeline._global_embedder)

    assert isinstance(result, SearchPipelineResult)
    assert result.acl_filtered is False
    assert len(result.results) == 1


@pytest.mark.asyncio
async def test_search_with_context_still_works_after_type_change(connected_store, col_name, tmp_path) -> None:
    """search_with_context() returns list of dicts with result/context_before/context_after keys."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=32),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "swc_type_doc.md"
    md_file.write_text("# SWC Test\n\n" + ("content chunk. " * 50))
    await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    results = await pipeline.search_with_context("content chunk", col_name, context_window=1, embedder=pipeline._global_embedder)

    from archon_search.pipeline import SearchWithContextResult
    assert isinstance(results, SearchWithContextResult)
    assert len(results.results) > 0
    for item in results.results:
        assert "result" in item
        assert "context_before" in item
        assert "context_after" in item


# ===========================================================================
# namespace guard for get_all_collections_meta, list_documents, delete_document
# ===========================================================================


@pytest.mark.asyncio
async def test_get_all_collections_meta_filters_by_namespace() -> None:
    """get_all_collections_meta(namespace) returns only collections in that namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    meta_a = CollectionMeta(name="col-a", namespace="tenantA")
    meta_b = CollectionMeta(name="col-b", namespace="tenantB")

    store = MagicMock()
    store.get_all_collections_meta = AsyncMock(return_value=[meta_a, meta_b])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.get_all_collections_meta(namespace="tenantA")

    assert result == [meta_a]


@pytest.mark.asyncio
async def test_list_documents_wrong_namespace_returns_empty() -> None:
    """list_documents returns [] when the collection belongs to a different namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    # get_collection_meta returns None → collection not in requested namespace
    store.get_collection_meta = AsyncMock(return_value=None)
    store.list_documents = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    items, next_cursor, total = await pipeline.list_documents("col-a", namespace="tenantB")

    assert items == []
    assert next_cursor is None
    assert total == 0
    store.get_collection_meta.assert_awaited_once_with("col-a", namespace="tenantB")
    store.list_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_documents_correct_namespace_succeeds() -> None:
    """list_documents delegates to store when the collection belongs to the correct namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    meta = CollectionMeta(name="col-a", namespace="tenantA")
    doc = DocumentInfo(doc_id="a" * 64, source_path="/some/path.md", chunk_count=2, indexed_at="2026-01-01T00:00:00")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.list_documents = AsyncMock(return_value=([doc], None, 1))

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    items, next_cursor, total = await pipeline.list_documents("col-a", namespace="tenantA")

    assert items == [doc]
    assert next_cursor is None
    assert total == 1
    store.get_collection_meta.assert_awaited_once_with("col-a", namespace="tenantA")
    store.list_documents.assert_awaited_once_with("col-a", 100, cursor=None)


@pytest.mark.asyncio
async def test_delete_document_wrong_namespace_raises() -> None:
    """delete_document raises ValueError when collection is not in the requested namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.delete_document = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    doc_id = "a" * 64
    with pytest.raises(ValueError, match="not found in namespace"):
        await pipeline.delete_document(doc_id, "col-a", namespace="tenantB")

    store.delete_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_document_correct_namespace_succeeds() -> None:
    """delete_document delegates to store when collection belongs to the correct namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    meta = CollectionMeta(name="col-a", namespace="tenantA")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.delete_document = AsyncMock(return_value=3)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    doc_id = "a" * 64
    deleted = await pipeline.delete_document(doc_id, "col-a", namespace="tenantA")

    assert deleted == 3
    store.delete_document.assert_awaited_once_with("col-a", doc_id, namespace="tenantA")


# ===========================================================================
# Task 3.3: filters kwarg forwarding + attrition WARNING
# ===========================================================================


def _make_search_result(n: int, acl: list[str] | None = None) -> "ScoredSearchCandidate":
    """Build a minimal ScoredSearchCandidate for filter/ACL tests."""
    doc_id = f"{'a' * 63}{n % 10}"
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=f"result {n}",
        source_path=f"/path/{n}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.5, reranker_score=None,
        ),
        collection="col",
        acl=acl,
    )


@pytest.mark.asyncio
async def test_pipeline_search_forwards_filters_to_store() -> None:
    """filters kwarg is forwarded to store.hybrid_search."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=[])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    await pipeline.search("test query", "col", filters=filters, embedder=pipeline._global_embedder)

    store.hybrid_search_with_trace.assert_awaited_once()
    call_kwargs = store.hybrid_search_with_trace.call_args.kwargs
    assert call_kwargs.get("filters") is filters


@pytest.mark.asyncio
async def test_pipeline_warns_on_filter_plus_acl_under_delivery(caplog) -> None:
    """WARNING emitted when filters set + ACL drops results below top_k_return."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # Store returns top_k_retrieve=10 results; 8 have ACL that denies default namespace
    restricted_results = [_make_search_result(i, acl=["tenantX"]) for i in range(8)]
    open_results = [_make_search_result(i + 100, acl=None) for i in range(2)]
    all_results = open_results + restricted_results  # 2 pass, 8 denied

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return all_results

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,  # survivors (2) < top_k_return (5) → warning
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_messages), (
        f"Expected attrition warning. Got: {warning_messages}"
    )
    # filter_flags must mention file_type
    attrition_msg = next(m for m in warning_messages if "filter+ACL combined attrition" in m)
    assert "file_type" in attrition_msg
    # acl_denied count (8) must appear
    assert "acl_denied=8" in attrition_msg


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_no_filter_set(caplog) -> None:
    """No WARNING when filters=None even if ACL drops results below top_k_return."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    restricted_results = [_make_search_result(i, acl=["tenantX"]) for i in range(8)]
    open_results = [_make_search_result(i + 100, acl=None) for i in range(2)]

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return open_results + restricted_results

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=None, embedder=pipeline._global_embedder)

    attrition_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "filter+ACL combined attrition" in r.message
    ]
    assert attrition_warnings == [], "No attrition warning should be emitted when filters=None"


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_pool_above_top_k(caplog) -> None:
    """No WARNING when survivors after ACL meet or exceed top_k_return."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # 6 open results — all pass ACL; 6 >= top_k_return(5) → no warning
    open_results = [_make_search_result(i, acl=None) for i in range(6)]

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return open_results

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    attrition_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "filter+ACL combined attrition" in r.message
    ]
    assert attrition_warnings == [], "No warning when survivors >= top_k_return"


@pytest.mark.asyncio
async def test_pipeline_search_with_context_forwards_filters_to_store() -> None:
    """search_with_context forwards filters kwarg to the inner search -> store.hybrid_search call."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=[])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    await pipeline.search_with_context("test query", "col", embedder=pipeline._global_embedder, filters=filters)

    store.hybrid_search_with_trace.assert_awaited_once()
    call_kwargs = store.hybrid_search_with_trace.call_args.kwargs
    assert call_kwargs.get("filters") is filters, (
        f"Expected filters to be forwarded; got: {call_kwargs}"
    )


@pytest.mark.asyncio
async def test_pipeline_warns_when_filter_alone_causes_under_delivery(caplog) -> None:
    """WARNING fires when filters cause under-delivery even with acl_denied=0."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # Store returns only 3 results (filter was restrictive), all pass ACL
    open_results = [_make_search_result(i, acl=None) for i in range(3)]

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return open_results  # only 3, all open

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,  # 3 < 5 → warning should fire
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_messages), (
        f"Expected attrition warning when filters cause under-delivery. Got: {warning_messages}"
    )
    attrition_msg = next(m for m in warning_messages if "filter+ACL combined attrition" in m)
    assert "acl_denied=0" in attrition_msg, f"Expected acl_denied=0 in: {attrition_msg}"
    assert "file_type" in attrition_msg


@pytest.mark.asyncio
async def test_pipeline_warns_when_store_returns_zero_results_with_filters(caplog) -> None:
    """WARNING fires when store returns 0 results with active filters (zero-result boundary)."""
    import logging
    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return []  # zero results — filter was very restrictive

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        result = await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    assert result.results == []
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_messages), (
        f"Expected attrition warning for zero results. Got: {warning_messages}"
    )
    attrition_msg = next(m for m in warning_messages if "filter+ACL combined attrition" in m)
    assert "0/" in attrition_msg, f"Expected '0/' in: {attrition_msg}"
    assert "acl_denied=0" in attrition_msg


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_search_filter_then_acl_order() -> None:
    """Rows excluded by filter are never seen by apply_acl_filter (spy on input count)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # Store returns 5 results; filter reduces to 3 (store is responsible for pre-filter)
    filtered_results = [_make_search_result(i, acl=None) for i in range(3)]

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=filtered_results)

    acl_inputs: list[int] = []

    import archon_search.pipeline as _pipeline_mod

    original_apply_acl = _pipeline_mod.apply_acl_filter

    def spy_acl_filter(items, get_acl, namespace):
        acl_inputs.append(len(items))
        return original_apply_acl(items, get_acl, namespace)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")

    with patch.object(_pipeline_mod, "apply_acl_filter", side_effect=spy_acl_filter):
        await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    # ACL filter received exactly the 3 store results (filter already applied by store)
    assert acl_inputs[0] == 3, (
        f"apply_acl_filter should see store-filtered results (3), got {acl_inputs[0]}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_search_filter_then_reranker_order() -> None:
    """Reranker sees only the filter+ACL survivors."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    # 4 results from store: 2 pass ACL, 2 are restricted
    open_results = [_make_search_result(i, acl=None) for i in range(2)]
    restricted_results = [_make_search_result(i + 10, acl=["tenantX"]) for i in range(2)]

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=open_results + restricted_results)

    reranker_inputs: list[list] = []

    class SpyRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            reranker_inputs.append([p[1] for p in pairs])
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=Reranker(SpyRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    # Reranker must receive only the 2 open results
    assert len(reranker_inputs) == 1
    assert len(reranker_inputs[0]) == 2, (
        f"Reranker should see 2 survivors, got {len(reranker_inputs[0])}"
    )


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_filters_has_no_active_fields(caplog) -> None:
    """No WARNING when SearchFilters() is passed with all fields at defaults (no real filter set)."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # 8 restricted + 2 open -- survivors (2) < top_k_return (5)
    restricted = [_make_search_result(i, acl=["tenantX"]) for i in range(8)]
    open_results = [_make_search_result(i + 100, acl=None) for i in range(2)]

    class StubStore:
        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return open_results + restricted

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    # SearchFilters() with all defaults — filter_flags will be empty, warning must NOT fire
    filters = SearchFilters()
    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters, embedder=pipeline._global_embedder)

    assert not any(
        "filter+ACL combined attrition" in r.message
        for r in caplog.records
    ), f"No attrition warning expected for empty filter. Got: {[r.message for r in caplog.records]}"


# ===========================================================================
# is_warm pipeline properties — Task 2.3 (B2)
# ===========================================================================


def test_pipeline_reranker_is_warm_false_when_cold() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    backend = MockRerankerBackend()
    backend.is_warm = False
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=Reranker(backend),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.reranker_is_warm is False


def test_pipeline_reranker_is_warm_true_when_warm() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    backend = MockRerankerBackend()
    backend.is_warm = True
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=Reranker(backend),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.reranker_is_warm is True


def test_pipeline_embedder_is_warm_false_when_cold() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    emb_backend = MockEmbedderBackend()
    emb_backend.is_warm = False
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=Embedder(emb_backend),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.embedder_is_warm is False


def test_pipeline_embedder_is_warm_true_when_warm() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    emb_backend = MockEmbedderBackend()
    emb_backend.is_warm = True
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=Embedder(emb_backend),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.embedder_is_warm is True


# ===========================================================================
# C4 Task 2.3 — search_with_context() query_vector parameter
# ===========================================================================

@pytest.mark.asyncio
async def test_search_with_context_forwards_query_vector() -> None:
    """search_with_context() forwards query_vector to the inner search(); embed_one must NOT be called."""
    from unittest.mock import AsyncMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    captured_vector: list[list[float]] = []

    class StubStore:
        async def hybrid_search_with_trace(self, collection: str, vector: list[float], query: str, *, candidate_depth: int, filters: Any = None, scope_filter: Any = None) -> list[ScoredSearchCandidate]:
            captured_vector.append(list(vector))
            return []

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    provided_vector = [0.9, 0.8, 0.7, 0.6]

    with patch.object(pipeline._global_embedder, "embed_one", new_callable=AsyncMock) as mock_embed:
        await pipeline.search_with_context(
            "some query",
            "test-col",
            embedder=pipeline._global_embedder,
            query_vector=provided_vector,
        )

    mock_embed.assert_not_called()
    assert len(captured_vector) == 1
    assert captured_vector[0] == provided_vector


# ===========================================================================
# C1 Task 3.2 — search() embedding-model routing
# ===========================================================================


@pytest.mark.asyncio
async def test_search_uses_passed_embedder(connected_store, col_name, tmp_path):
    """search() embeds with the passed embedder, not self._global_embedder."""
    from archon_search.pipeline import SearchPipeline
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    # Ingest a doc so the collection exists
    doc = tmp_path / "doc.md"
    doc.write_text("# Test\n\nContent for embedder routing test.\n" * 5)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    # Create a second embedder (embedder_B) and spy on both
    embedder_b = make_embedder()
    embedder_b_embed_one = AsyncMock(return_value=[0.1] * 4)
    global_embed_one = AsyncMock(return_value=[0.1] * 4)

    pipeline._global_embedder.embed_one = global_embed_one  # type: ignore[method-assign]
    embedder_b.embed_one = embedder_b_embed_one  # type: ignore[method-assign]

    await pipeline.search("test query", col_name, embedder=embedder_b)

    embedder_b_embed_one.assert_awaited_once()
    global_embed_one.assert_not_called()


@pytest.mark.asyncio
async def test_search_does_not_call_global_embedder(connected_store, col_name, tmp_path):
    """search() must NOT call self._global_embedder.embed_one."""
    from archon_search.pipeline import SearchPipeline
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    doc = tmp_path / "doc2.md"
    doc.write_text("# Test\n\nAnother content for embedder test.\n" * 5)
    await pipeline.ingest_file(doc, col_name, embedder=pipeline._global_embedder)

    mock_embedder = make_embedder()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    global_embed_one = AsyncMock(return_value=[0.1] * 4)
    pipeline._global_embedder.embed_one = global_embed_one  # type: ignore[method-assign]

    await pipeline.search("another query", col_name, embedder=mock_embedder)

    global_embed_one.assert_not_called()


# ===========================================================================
# C1 Task 3.6 — search_with_context embedder param
# ===========================================================================


@pytest.mark.asyncio
async def test_search_with_context_uses_passed_embedder() -> None:
    """search_with_context(embedder=X) must forward X to the inner search() call."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.hybrid_search_with_trace = AsyncMock(return_value=[])
    store.fetch_adjacent_chunks = AsyncMock(return_value=[])

    global_embedder = make_embedder()
    passed_embedder = make_embedder()

    global_embed_one = AsyncMock(return_value=[0.1] * 4)
    passed_embed_one = AsyncMock(return_value=[0.1] * 4)
    global_embedder.embed_one = global_embed_one  # type: ignore[method-assign]
    passed_embedder.embed_one = passed_embed_one  # type: ignore[method-assign]

    pipeline = SearchPipeline(
        store=store,
        embedder=global_embedder,
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    await pipeline.search_with_context("test query", "col", embedder=passed_embedder)

    passed_embed_one.assert_awaited_once()
    global_embed_one.assert_not_called()


# ===========================================================================
# Structural / invariant tests
# ===========================================================================


def test_search_many_signature_unchanged() -> None:
    """search_many() must NOT have an embedder parameter."""
    import inspect
    from archon_search.pipeline import SearchPipeline
    sig = inspect.signature(SearchPipeline.search_many)
    assert "embedder" not in sig.parameters, (
        "search_many gained an unexpected 'embedder' parameter"
    )


def test_telemetry_entry_no_query_parameter() -> None:
    """No factory method in archon_search/telemetry/entry.py accepts a 'query' parameter."""
    import inspect
    import importlib
    entry_mod = importlib.import_module("archon_search.telemetry.entry")
    for name, obj in inspect.getmembers(entry_mod, inspect.isclass):
        for method_name, method in inspect.getmembers(obj, predicate=inspect.isfunction):
            sig = inspect.signature(method)
            assert "query" not in sig.parameters, (
                f"{name}.{method_name} has a 'query' parameter — raw queries must never be logged"
            )


def test_job_status_enum_values_unchanged() -> None:
    """JobStatus must have exactly: PENDING, QUEUED, RUNNING, DONE, FAILED, FAILED_EXPIRED, CANCELLED, CANCELLING."""
    from archon_search.types import JobStatus
    expected = {"PENDING", "QUEUED", "RUNNING", "DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED", "CANCELLING"}
    actual = {m.name for m in JobStatus}
    assert actual == expected, f"JobStatus members changed: {actual}"


# ---------------------------------------------------------------------------
# Task 2.2 — store.has_vector_index + hybrid_search_with_trace filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_has_vector_index_true_for_normal_collection(
    connected_store, col_name
) -> None:
    """has_vector_index returns True for a collection that was created with a vector column."""
    # ensure_collection creates a table with a vector column (embedding_dim=4)
    await connected_store.ensure_collection(col_name, embedding_dim=4)
    result = await connected_store.has_vector_index(col_name)
    assert result is True


@pytest.mark.asyncio
async def test_store_has_vector_index_false_for_missing_collection(connected_store) -> None:
    """has_vector_index returns False for a collection that does not exist."""
    result = await connected_store.has_vector_index("nonexistent-collection-xyz")
    assert result is False


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_filters_applied(connected_store, col_name, tmp_path) -> None:
    """hybrid_search_with_trace with filters excludes documents that don't match."""
    from archon_search.filters import SearchFilters

    pipeline = make_pipeline(connected_store)
    doc1 = tmp_path / "doc1.md"
    doc1.write_text("# Doc One\n\nContent about apples and fruit.\n" * 5)
    doc2 = tmp_path / "doc2.py"
    doc2.write_text("# Python code about bananas\n" * 5)

    await pipeline.ingest_file(doc1, col_name, embedder=pipeline._global_embedder)
    await pipeline.ingest_file(doc2, col_name, embedder=pipeline._global_embedder)

    vector = await pipeline._global_embedder.embed_one("query")

    # Filter to only Python files
    filters = SearchFilters(file_type="py")
    results = await connected_store.hybrid_search_with_trace(
        col_name, vector, "query", candidate_depth=10, filters=filters
    )

    file_types = {r.file_type for r in results}
    assert "py" in file_types
    assert "md" not in file_types

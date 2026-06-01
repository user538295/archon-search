"""Tests for SearchPipeline.explain (Task 2.3)."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search._types import ChunkRecord
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder import Embedder
from archon_search.pipeline import ExplainPipelineResult, ExplainStageError, SearchPipeline
from archon_search.reranker import Reranker, RerankerBackend


# ---------------------------------------------------------------------------
# Mock backends
# ---------------------------------------------------------------------------


class MockEmbedderBackend:
    """Returns dim=4 vectors for all texts."""

    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class MockRerankerBackend:
    """Returns 0.5 for all pairs (used in tie-break tests)."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


class DistinctTextRerankerBackend:
    """Returns a distinct, text-deterministic score per candidate text."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [
            int(hashlib.sha256(t.encode()).hexdigest(), 16) % 100000 / 100000
            for _, t in pairs
        ]


# ---------------------------------------------------------------------------
# Pipeline / store helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    store,
    *,
    top_k_retrieve: int = 10,
    top_k_return: int = 5,
    reranker: Reranker | None = None,
) -> SearchPipeline:
    return SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=reranker or Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
    )


def _chunk(doc_id: str, idx: int, text: str, *, acl: list[str] | None = None, dim: int = 4) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx + 1)] * dim,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
        acl=acl,
    )


async def _ingest(store, col: str, records: list[ChunkRecord]) -> None:
    """Ensure collection exists and ingest pre-built chunks, then rebuild FTS."""
    await store.ensure_collection(col, 4)
    await store.ingest_chunks(col, records)
    await store.rebuild_fts_index(col)


def _make_doc_id(n: int) -> str:
    """Return a deterministic 64-hex doc_id from an integer."""
    return hashlib.sha256(f"doc-{n:04d}".encode()).hexdigest()


def _make_records(
    n: int,
    *,
    acl: list[str] | None = None,
    id_offset: int = 0,
) -> list[ChunkRecord]:
    """Create n ChunkRecords with distinct texts; shared terms so FTS matches."""
    doc_id = _make_doc_id(id_offset)
    return [
        _chunk(
            doc_id,
            i,
            f"common alpha beta token unique{i + id_offset}",
            acl=acl,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_returns_top_results_and_near_misses(connected_store, col_name):
    """≥25 distinct-chunk corpus; top_k=5 → len(top_results)==5, len(near_misses)<=20."""
    pipeline = _make_pipeline(connected_store, top_k_retrieve=20, top_k_return=5)
    records = _make_records(30)
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain("common alpha beta", col_name, top_k=5)

    assert isinstance(result, ExplainPipelineResult)
    assert len(result.top_results) == 5
    assert len(result.near_misses) <= 20


@pytest.mark.asyncio
async def test_explain_top_k_matches_search_when_rerank_true_and_top_k_equals_top_k_return(
    connected_store, col_name
):
    """top_k_retrieve=10, ingest 8 chunks → search and explain rerank IDENTICAL pool."""
    # Use top_k_retrieve=10, ingest 8 chunks (≤ top_k_retrieve) so both
    # search() and explain() see all 8 candidates → identical reranked order.
    top_k_retrieve = 10
    top_k_return = 5
    pipeline = _make_pipeline(
        connected_store,
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
        reranker=Reranker(DistinctTextRerankerBackend()),
    )
    records = _make_records(8)
    await _ingest(connected_store, col_name, records)

    query = "common alpha beta"
    search_result = await pipeline.search(query, col_name, embedder=pipeline._global_embedder)
    explain_result = await pipeline.explain(
        query, col_name, top_k=pipeline._top_k_return, rerank=True
    )

    search_ids = [(r.doc_id, r.chunk_id) for r in search_result.results]
    explain_ids = [(c.doc_id, c.chunk_id) for c in explain_result.top_results]
    assert search_ids == explain_ids, (
        f"search returned {search_ids} but explain returned {explain_ids}"
    )


@pytest.mark.asyncio
async def test_explain_rerank_false_uses_rrf_ordering(connected_store, col_name):
    """rerank=False → reranker_score is None everywhere; ordering is by rrf_score desc."""
    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    records = _make_records(15)
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain("common alpha beta", col_name, top_k=5, rerank=False)

    all_candidates = result.top_results + result.near_misses
    # All reranker scores must be None
    for c in all_candidates:
        assert c.score_breakdown.reranker_score is None, (
            f"Expected reranker_score=None, got {c.score_breakdown.reranker_score}"
        )

    # Combined list must be sorted by rrf_score desc, tie-break (doc_id, chunk_id) asc
    for a, b in zip(all_candidates, all_candidates[1:]):
        a_key = (-a.score_breakdown.rrf_score, a.doc_id, a.chunk_id)
        b_key = (-b.score_breakdown.rrf_score, b.doc_id, b.chunk_id)
        assert a_key <= b_key, (
            f"Ordering violated: {a.chunk_id} (rrf={a.score_breakdown.rrf_score}) "
            f"before {b.chunk_id} (rrf={b.score_breakdown.rrf_score})"
        )


@pytest.mark.asyncio
async def test_explain_uses_amplified_retrieval_pool(connected_store, col_name):
    """candidate_depth forwarded to hybrid_search_with_trace == max(top_k_retrieve*3, 20)."""
    top_k_retrieve = 8
    pipeline = _make_pipeline(connected_store, top_k_retrieve=top_k_retrieve)
    records = _make_records(5)
    await _ingest(connected_store, col_name, records)

    captured_depths: list[int] = []
    original = pipeline.store.hybrid_search_with_trace

    async def _spy(collection, query_vector, query_text, candidate_depth):
        captured_depths.append(candidate_depth)
        return await original(collection, query_vector, query_text, candidate_depth=candidate_depth)

    pipeline.store.hybrid_search_with_trace = _spy  # type: ignore[method-assign]

    expected_depth = max(top_k_retrieve * 3, 20)

    # Call with two different top_k values — candidate_depth must not change
    await pipeline.explain("common alpha beta", col_name, top_k=1)
    await pipeline.explain("common alpha beta", col_name, top_k=50)

    assert len(captured_depths) == 2
    assert captured_depths[0] == expected_depth, (
        f"Expected candidate_depth={expected_depth}, got {captured_depths[0]}"
    )
    assert captured_depths[1] == expected_depth, (
        f"Expected candidate_depth={expected_depth}, got {captured_depths[1]}"
    )


@pytest.mark.asyncio
async def test_explain_accepts_precomputed_query_vector_and_skips_embedding(
    connected_store, col_name
):
    """Passing query_vector= skips embed_one; the supplied vector is forwarded to the store."""
    pipeline = _make_pipeline(connected_store)
    records = _make_records(5)
    await _ingest(connected_store, col_name, records)

    # Make embed_one raise to confirm it's not called
    async def _embed_one_raises(text: str) -> list[float]:
        raise RuntimeError("embed_one must not be called when query_vector is provided")

    pipeline._global_embedder.embed_one = _embed_one_raises  # type: ignore[method-assign]

    captured_vectors: list[list[float]] = []
    original = pipeline.store.hybrid_search_with_trace

    async def _spy(collection, query_vector, query_text, candidate_depth):
        captured_vectors.append(query_vector)
        return await original(collection, query_vector, query_text, candidate_depth=candidate_depth)

    pipeline.store.hybrid_search_with_trace = _spy  # type: ignore[method-assign]

    pre_vector = [0.1, 0.2, 0.3, 0.4]
    result = await pipeline.explain("ignored query", col_name, query_vector=pre_vector)

    assert isinstance(result, ExplainPipelineResult)
    assert len(captured_vectors) == 1
    assert captured_vectors[0] == pre_vector, (
        f"Expected forwarded vector {pre_vector}, got {captured_vectors[0]}"
    )


@pytest.mark.asyncio
async def test_explain_near_miss_pool_capped_at_20(connected_store, col_name):
    """Pool ≥ top_k+20; near_misses must be exactly 20.

    top_k_retrieve=20 → candidate_depth=max(60,20)=60; ingest 30 distinct chunks.
    """
    pipeline = _make_pipeline(connected_store, top_k_retrieve=20, top_k_return=5)
    records = _make_records(30)
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain("common alpha beta", col_name, top_k=5)

    assert len(result.top_results) == 5
    assert len(result.near_misses) == 20, (
        f"Expected near_misses==20, got {len(result.near_misses)}"
    )


@pytest.mark.asyncio
async def test_explain_small_corpus_returns_what_is_available(connected_store, col_name):
    """3-chunk corpus, top_k=5 → len(top_results)<=3, near_misses==[]."""
    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    records = _make_records(3)
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain("common alpha beta", col_name, top_k=5)

    assert len(result.top_results) <= 3
    assert result.near_misses == []


@pytest.mark.asyncio
async def test_explain_acl_filtered_when_all_filtered(connected_store, col_name):
    """All chunks restricted to 'blocked'; query with DEFAULT_NAMESPACE → everything filtered."""
    pipeline = _make_pipeline(connected_store)
    records = _make_records(4, acl=["blocked"])
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain(
        "common alpha beta", col_name, namespace=DEFAULT_NAMESPACE
    )

    assert result.top_results == []
    assert result.near_misses == []
    assert result.acl_filtered is True


@pytest.mark.asyncio
async def test_explain_partial_acl_filter_adjusts_near_miss_count(connected_store, col_name):
    """12 allowed + 8 blocked chunks; post-ACL pool P=12 is KNOWN from fixture.

    With top_k=5 and namespace='default':
      near_misses == min(20, max(0, 12 - 5)) == 7
    P is NOT derived from output — it is a fixture constant.
    """
    top_k_retrieve = 20
    pipeline = _make_pipeline(connected_store, top_k_retrieve=top_k_retrieve, top_k_return=5)

    # 12 allowed (acl=["default"]), 8 blocked (acl=["blocked"])
    allowed_count = 12
    blocked_count = 8
    doc_id_allowed = _make_doc_id(900)
    doc_id_blocked = _make_doc_id(901)

    allowed_records = [
        _chunk(doc_id_allowed, i, f"common alpha beta token unique-allowed{i}", acl=["default"])
        for i in range(allowed_count)
    ]
    blocked_records = [
        _chunk(doc_id_blocked, i, f"common alpha beta token unique-blocked{i}", acl=["blocked"])
        for i in range(blocked_count)
    ]
    await _ingest(connected_store, col_name, allowed_records + blocked_records)

    result = await pipeline.explain(
        "common alpha beta", col_name, top_k=5, namespace=DEFAULT_NAMESPACE
    )

    # P is known from the fixture: 12 allowed chunks
    P = allowed_count  # NOT derived from output
    expected_near_misses = min(20, max(0, P - 5))  # == 7
    assert len(result.top_results) == 5
    assert len(result.near_misses) == expected_near_misses, (
        f"P={P}, top_k=5, expected near_misses={expected_near_misses}, got {len(result.near_misses)}"
    )
    assert result.acl_filtered is True


@pytest.mark.asyncio
async def test_explain_identical_scores_tie_break_by_doc_chunk_id(connected_store, col_name):
    """Identical reranker scores → final ordering is (doc_id, chunk_id) ascending."""
    # Use MockRerankerBackend (constant 0.5) so all reranker scores tie
    pipeline = _make_pipeline(
        connected_store,
        top_k_retrieve=10,
        top_k_return=5,
        reranker=Reranker(MockRerankerBackend()),
    )
    records = _make_records(6)
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain("common alpha beta", col_name, top_k=3, rerank=True)

    all_candidates = result.top_results + result.near_misses
    # All reranker scores must be equal (0.5)
    for c in all_candidates:
        assert c.score_breakdown.reranker_score == pytest.approx(0.5), (
            f"Expected 0.5, got {c.score_breakdown.reranker_score}"
        )

    # Must be sorted by (doc_id, chunk_id) ascending (since scores tie)
    for a, b in zip(all_candidates, all_candidates[1:]):
        a_key = (a.doc_id, a.chunk_id)
        b_key = (b.doc_id, b.chunk_id)
        assert a_key <= b_key, (
            f"Tie-break order violated: {a.chunk_id!r} before {b.chunk_id!r}"
        )


@pytest.mark.asyncio
async def test_explain_near_misses_carry_reranker_score_when_rerank_true(connected_store, col_name):
    """rerank=True reranks the entire pool so near-misses also carry real reranker scores."""
    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    records = _make_records(12)
    await _ingest(connected_store, col_name, records)

    result = await pipeline.explain("common alpha beta", col_name, top_k=5, rerank=True)

    assert len(result.near_misses) >= 1, "Expected at least one near-miss"
    for c in result.near_misses:
        assert c.score_breakdown.reranker_score is not None, (
            f"Near-miss {c.chunk_id} has reranker_score=None but rerank=True was requested"
        )


@pytest.mark.asyncio
async def test_explain_wraps_store_failure_in_stage_error(connected_store, col_name, monkeypatch):
    """A store failure during explain raises ExplainStageError(stage='store')."""
    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    await _ingest(connected_store, col_name, _make_records(5))

    async def _boom(*args, **kwargs):
        raise RuntimeError("store boom")

    monkeypatch.setattr(pipeline.store, "hybrid_search_with_trace", _boom)
    with pytest.raises(ExplainStageError) as excinfo:
        await pipeline.explain("common alpha beta", col_name, top_k=5)
    assert excinfo.value.stage == "store"
    assert isinstance(excinfo.value.original, RuntimeError)
    assert str(excinfo.value).startswith("store error:")


@pytest.mark.asyncio
async def test_explain_wraps_reranker_failure_in_stage_error(connected_store, col_name, monkeypatch):
    """A reranker failure during explain raises ExplainStageError(stage='reranker')."""
    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    await _ingest(connected_store, col_name, _make_records(5))

    async def _boom(*args, **kwargs):
        raise RuntimeError("rerank boom")

    monkeypatch.setattr(pipeline._reranker, "rerank_candidates", _boom)
    with pytest.raises(ExplainStageError) as excinfo:
        await pipeline.explain("common alpha beta", col_name, top_k=5, rerank=True)
    assert excinfo.value.stage == "reranker"
    assert str(excinfo.value).startswith("reranker error:")


@pytest.mark.asyncio
async def test_explain_uses_rerank_candidates(connected_store, col_name, monkeypatch):
    """explain() invokes the unified rerank_candidates surface exactly once."""
    from unittest.mock import AsyncMock

    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    await _ingest(connected_store, col_name, _make_records(5))

    spy = AsyncMock(side_effect=pipeline._reranker.rerank_candidates)
    monkeypatch.setattr(pipeline._reranker, "rerank_candidates", spy)

    await pipeline.explain("common alpha beta", col_name, top_k=5, rerank=True)

    spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_explain_does_not_call_private_rerank_with_trace(
    connected_store, col_name, monkeypatch
):
    """explain() does the rerank work via rerank_candidates, not the private alias."""
    from unittest.mock import AsyncMock

    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    await _ingest(connected_store, col_name, _make_records(5))

    alias_spy = AsyncMock(side_effect=pipeline._reranker._rerank_with_trace)
    monkeypatch.setattr(pipeline._reranker, "_rerank_with_trace", alias_spy)

    await pipeline.explain("common alpha beta", col_name, top_k=5, rerank=True)

    alias_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-collection explain (B3 Task 6.1)
# ---------------------------------------------------------------------------


def _scored(collection: str, doc_id: str, chunk_id: str, rrf_score: float = 0.5) -> ScoredSearchCandidate:
    from archon_search._diagnostics import SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=f"text-{chunk_id}",
        source_path=f"/path/to/{doc_id}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=None,
        ),
        collection=collection,
    )


def _meta(name: str, *, active_embedding_model: str = "mock-embedder", namespace: str = DEFAULT_NAMESPACE):
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(name=name, active_embedding_model=active_embedding_model, namespace=namespace)


def _explain_multi_pipeline(
    *,
    leg_map: dict | None = None,
    meta_list: list | None = None,
    top_k_retrieve: int = 10,
    top_k_return: int = 5,
):
    """Build a SearchPipeline with a MagicMock store wired for fan-out explain."""
    from unittest.mock import AsyncMock

    store = MagicMock()
    leg_map = leg_map or {}

    async def _hybrid(collection, vector, query_text, candidate_depth):
        return list(leg_map.get(collection, []))

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)

    embedder = Embedder(MockEmbedderBackend())
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
    )
    if meta_list is not None:
        pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)  # type: ignore[method-assign]
    return pipeline, store


@pytest.mark.asyncio
async def test_pipeline_explain_multi_collection_fans_out() -> None:
    """explain(collections=[A, B]) calls hybrid_search_with_trace once per collection."""
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, store = _explain_multi_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])

    result = await pipeline.explain("q", collections=["A", "B"])

    assert isinstance(result, ExplainPipelineResult)
    assert store.hybrid_search_with_trace.await_count == 2


@pytest.mark.asyncio
async def test_pipeline_explain_multi_reranks_once() -> None:
    """explain(collections=[A, B]) reranks the merged pool exactly once."""
    from unittest.mock import AsyncMock

    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, _store = _explain_multi_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])
    spy = AsyncMock(side_effect=pipeline._reranker.rerank_candidates)
    pipeline._reranker.rerank_candidates = spy  # type: ignore[method-assign]

    await pipeline.explain("q", collections=["A", "B"])

    assert spy.await_count == 1
    merged = spy.await_args.args[1] if len(spy.await_args.args) > 1 else spy.await_args.kwargs["candidates"]
    assert len(merged) == 2


@pytest.mark.asyncio
async def test_pipeline_explain_multi_carries_collection_provenance() -> None:
    """Multi-collection explain results carry per-collection provenance."""
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, _store = _explain_multi_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])

    result = await pipeline.explain("q", collections=["A", "B"])

    by_doc = {c.doc_id: c.collection for c in result.top_results}
    assert by_doc["a" * 64] == "A"
    assert by_doc["b" * 64] == "B"


@pytest.mark.asyncio
async def test_pipeline_explain_multi_model_mismatch_excludes() -> None:
    """Model-mismatched collections are excluded and surfaced on the result."""
    leg_map = {"A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")]}
    pipeline, _store = _explain_multi_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A"), _meta("B", active_embedding_model="other-model")],
    )

    result = await pipeline.explain("q", collections=["A", "B"])

    assert [e.name for e in result.excluded_collections] == ["B"]
    assert result.excluded_collections[0].reason == "embedding_model_mismatch"


@pytest.mark.asyncio
async def test_pipeline_explain_multi_rerank_false_raises() -> None:
    """rerank=False with >1 collection raises ExplainMultiCollectionNoRerankError."""
    from archon_search.pipeline import ExplainMultiCollectionNoRerankError

    pipeline, _store = _explain_multi_pipeline(meta_list=[_meta("A"), _meta("B")])

    with pytest.raises(ExplainMultiCollectionNoRerankError):
        await pipeline.explain("q", collections=["A", "B"], rerank=False)


@pytest.mark.asyncio
async def test_pipeline_explain_both_collection_and_collections_raises() -> None:
    """Supplying both collection and collections is a ValueError."""
    pipeline, _store = _explain_multi_pipeline(meta_list=[_meta("A")])
    with pytest.raises(ValueError):
        await pipeline.explain("q", "A", collections=["A"])


@pytest.mark.asyncio
async def test_pipeline_explain_neither_collection_nor_collections_raises() -> None:
    """Supplying neither collection nor collections is a ValueError (route resolves first)."""
    pipeline, _store = _explain_multi_pipeline(meta_list=[_meta("A")])
    with pytest.raises(ValueError):
        await pipeline.explain("q")


@pytest.mark.asyncio
async def test_pipeline_explain_missing_collection_raises_not_found() -> None:
    """A requested collection absent from metadata raises CollectionNotFoundError."""
    from archon_search.pipeline import CollectionNotFoundError

    pipeline, _store = _explain_multi_pipeline(meta_list=[_meta("A")])
    with pytest.raises(CollectionNotFoundError):
        await pipeline.explain("q", collections=["A", "MISSING"])


# ---------------------------------------------------------------------------
# Task 3.5 — per-collection embedder parameter for explain()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_single_collection_uses_passed_embedder(connected_store, col_name) -> None:
    """explain(collection=..., embedder=mock) must use mock.embed_one, not global."""
    from unittest.mock import AsyncMock

    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    await _ingest(connected_store, col_name, _make_records(5))

    mock_embedder = MagicMock(spec=Embedder)
    mock_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])

    global_spy = AsyncMock(side_effect=pipeline._global_embedder.embed_one)
    pipeline._global_embedder.embed_one = global_spy  # type: ignore[method-assign]

    await pipeline.explain("common alpha beta", col_name, top_k=5, embedder=mock_embedder)

    mock_embedder.embed_one.assert_awaited_once_with("common alpha beta")
    global_spy.assert_not_called()


@pytest.mark.asyncio
async def test_explain_multi_collection_uses_global_embedder() -> None:
    """explain(collections=[...], embedder=None) must always use self._global_embedder."""
    from unittest.mock import AsyncMock

    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, _store = _explain_multi_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])

    global_spy = AsyncMock(return_value=[0.1] * 4)
    pipeline._global_embedder.embed_one = global_spy  # type: ignore[method-assign]

    mock_embedder = MagicMock(spec=Embedder)
    mock_embedder.embed_one = AsyncMock(return_value=[0.9] * 4)

    # Pass embedder= but multi-collection path must ignore it and use global
    await pipeline.explain("q", collections=["A", "B"], embedder=mock_embedder)

    global_spy.assert_awaited_once()
    mock_embedder.embed_one.assert_not_called()


@pytest.mark.asyncio
async def test_explain_single_collection_no_embedder_falls_back_to_global(connected_store, col_name) -> None:
    """explain(collection=...) with no embedder kwarg falls back to self._global_embedder."""
    from unittest.mock import AsyncMock

    pipeline = _make_pipeline(connected_store, top_k_retrieve=10, top_k_return=5)
    await _ingest(connected_store, col_name, _make_records(5))

    global_spy = AsyncMock(side_effect=pipeline._global_embedder.embed_one)
    pipeline._global_embedder.embed_one = global_spy  # type: ignore[method-assign]

    # No embedder= passed — should fall back to global
    await pipeline.explain("common alpha beta", col_name, top_k=5)

    global_spy.assert_awaited_once_with("common alpha beta")

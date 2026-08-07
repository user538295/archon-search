"""packages/archon-search/tests/test_reranker.py — unit tests for Reranker (fastembed backend)."""
from __future__ import annotations

import dataclasses
import threading

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import SearchResult
from archon_search.reranker import ModelReranker, Reranker, RerankerBackend, make_reranker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockRerankerBackend:
    """Returns scores passed in at construction time, in order."""

    is_warm: bool = False

    def __init__(self, scores: list[float] | None = None) -> None:
        self._scores = scores or [0.5]
        self.called_pairs: list[list[tuple[str, str]]] = []

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.called_pairs.append(pairs)
        # repeat/truncate scores to match len(pairs)
        return (self._scores * len(pairs))[: len(pairs)]


def _make_candidates(n: int) -> list[SearchResult]:
    return [
        SearchResult(
            doc_id=f"doc{i}",
            chunk_id=f"doc{i}-000000",
            text=f"text {i}",
            score=0.0,
            source_path=f"/tmp/{i}.md",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranker_sorts_by_score_descending() -> None:
    """Candidates are returned in descending score order, scores are updated."""
    backend = _MockRerankerBackend(scores=[0.9, 0.1, 0.5])
    reranker = Reranker(backend)
    candidates = _make_candidates(3)
    result = await reranker.rerank("query", candidates, top_k=3)

    # Order must be descending by the cross-encoder score
    assert result[0].score == pytest.approx(0.9)
    assert result[1].score == pytest.approx(0.5)
    assert result[2].score == pytest.approx(0.1)

    # Score fields must be mutated (not left at original 0.0)
    assert result[0].doc_id == "doc0"  # score 0.9 → first candidate
    assert result[1].doc_id == "doc2"  # score 0.5 → third candidate
    assert result[2].doc_id == "doc1"  # score 0.1 → second candidate


@pytest.mark.asyncio
async def test_reranker_truncates_to_top_k() -> None:
    backend = _MockRerankerBackend(scores=[0.5])
    reranker = Reranker(backend)
    candidates = _make_candidates(5)
    result = await reranker.rerank("q", candidates, top_k=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_reranker_empty_candidates_returns_empty() -> None:
    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    result = await reranker.rerank("q", [], top_k=5)
    assert result == []


@pytest.mark.asyncio
async def test_reranker_calls_backend_with_correct_pairs() -> None:
    backend = _MockRerankerBackend(scores=[0.8, 0.3])
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    await reranker.rerank("my query", candidates, top_k=2)

    assert len(backend.called_pairs) == 1
    pairs = backend.called_pairs[0]
    assert pairs[0] == ("my query", "text 0")
    assert pairs[1] == ("my query", "text 1")


@pytest.mark.asyncio
async def test_reranker_score_mutation() -> None:
    """Returned SearchResult.score must equal the backend-assigned score."""
    backend = _MockRerankerBackend(scores=[0.77, 0.33])
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    result = await reranker.rerank("q", candidates, top_k=2)

    scores = {r.doc_id: r.score for r in result}
    assert scores["doc0"] == pytest.approx(0.77)
    assert scores["doc1"] == pytest.approx(0.33)


@pytest.mark.asyncio
async def test_make_reranker_returns_reranker() -> None:
    """Factory creates a working Reranker that exercises the lazy import path."""
    r = make_reranker("BAAI/bge-reranker-v2-m3", providers=[])
    assert isinstance(r, Reranker)
    # Also verify the lazy import fires correctly by calling rerank()
    candidates = [
        SearchResult(doc_id="d0", chunk_id="d0-000000", text="text", score=0.0, source_path="/tmp/f.md")
    ]
    result = await r.rerank("query", candidates, top_k=1)
    assert len(result) == 1
    assert result[0].score == pytest.approx(0.5)


def test_reranker_backend_protocol() -> None:
    """_MockRerankerBackend satisfies the RerankerBackend protocol."""
    backend = _MockRerankerBackend()
    assert isinstance(backend, RerankerBackend)


# ===========================================================================
# Reranker edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_P14_5_reranker_top_k_greater_than_candidates_returns_all() -> None:
    """ top_k > len(candidates): returns all candidates (no IndexError)."""
    backend = _MockRerankerBackend(scores=[0.9, 0.3])
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    result = await reranker.rerank("query", candidates, top_k=10)
    # All 2 candidates returned, no crash
    assert len(result) == 2


@pytest.mark.asyncio
async def test_P14_6_reranker_stable_order_on_equal_scores() -> None:
    """ all candidates have equal scores: order is stable (sorted is stable in Python)."""
    backend = _MockRerankerBackend(scores=[0.5])
    reranker = Reranker(backend)
    candidates = _make_candidates(4)
    original_ids = [c.doc_id for c in candidates]
    result = await reranker.rerank("query", candidates, top_k=4)
    result_ids = [r.doc_id for r in result]
    # All scores equal → Python's sort is stable, original order preserved
    assert result_ids == original_ids


@pytest.mark.asyncio
async def test_P14_7_reranker_score_count_mismatch_raises_valueerror() -> None:
    """ backend returns different number of scores than candidates → ValueError."""
    class _BadCountBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            # Returns one score regardless of how many pairs
            return [0.99]

    reranker = Reranker(_BadCountBackend())
    candidates = _make_candidates(3)
    with pytest.raises(ValueError, match="scores"):
        await reranker.rerank("query", candidates, top_k=3)


def test_model_reranker_init_called_once_under_concurrent_predict() -> None:
    """Double-checked locking: concurrent predict() calls init the model exactly once."""
    import sys
    import threading
    import time
    from typing import Any

    from archon_search.reranker import ModelReranker

    init_count = 0
    barrier = threading.Barrier(2)

    class _SlowTextCrossEncoder:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            nonlocal init_count
            init_count += 1
            time.sleep(0.05)  # slow enough to expose the race

        def rerank(self, query: str, documents: object) -> list[float]:
            return [0.5] * len(list(documents))  # type: ignore[arg-type]

    original = sys.modules["fastembed.rerank.cross_encoder"].TextCrossEncoder
    sys.modules["fastembed.rerank.cross_encoder"].TextCrossEncoder = _SlowTextCrossEncoder
    try:
        reranker = ModelReranker("BAAI/bge-reranker-v2-m3")
        results: list[list[float]] = []
        exceptions: list[Exception] = []

        def run_predict() -> None:
            barrier.wait()  # force simultaneous entry
            try:
                results.append(reranker.predict([("q", "doc1"), ("q", "doc2")]))
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=run_predict) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Unexpected exceptions: {exceptions}"
        assert len(results) == 2
        assert init_count == 1, f"Model __init__ called {init_count} times — lock missing"
    finally:
        sys.modules["fastembed.rerank.cross_encoder"].TextCrossEncoder = original


# ===========================================================================
# Reranker trace preservation
# ===========================================================================


def _make_scored_candidate(
    doc_id: str,
    text: str,
    rrf_score: float,
    collection: str = "default",
) -> ScoredSearchCandidate:
    breakdown = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=rrf_score,
        vector_score_kind="similarity",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=rrf_score,
        reranker_score=None,
    )
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=text,
        source_path=f"/tmp/{doc_id}.md",
        score_breakdown=breakdown,
        collection=collection,
    )


@pytest.mark.asyncio
async def test_rerank_trace_preserves_prerank_scores_and_adds_reranker_score() -> None:
    """Reranked trace results keep fused/RRF score and add reranker_score."""
    backend = _MockRerankerBackend(scores=[0.9, 0.3])
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.5),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.7),
    ]

    result = await reranker._rerank_with_trace("query", candidates, top_k=2)

    assert len(result) == 2
    # Sorted by reranker score descending: doc0 (0.9) first
    assert result[0].doc_id == "doc0"
    assert result[0].score_breakdown.reranker_score == pytest.approx(0.9)
    assert result[0].score_breakdown.rrf_score == pytest.approx(0.5)  # original preserved

    assert result[1].doc_id == "doc1"
    assert result[1].score_breakdown.reranker_score == pytest.approx(0.3)
    assert result[1].score_breakdown.rrf_score == pytest.approx(0.7)  # original preserved


@pytest.mark.asyncio
async def test_rerank_trace_does_not_mutate_prerank_results() -> None:
    """Pre-rerank trace objects must remain unchanged after reranking runs."""
    backend = _MockRerankerBackend(scores=[0.9, 0.1])
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.4),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.6),
    ]
    original_rrf_scores = [c.score_breakdown.rrf_score for c in candidates]
    original_reranker_scores = [c.score_breakdown.reranker_score for c in candidates]

    await reranker._rerank_with_trace("query", candidates, top_k=2)

    # Input candidates must NOT be mutated
    for candidate, orig_rrf, orig_reranker in zip(
        candidates, original_rrf_scores, original_reranker_scores
    ):
        assert candidate.score_breakdown.rrf_score == pytest.approx(orig_rrf)
        assert candidate.score_breakdown.reranker_score == orig_reranker


@pytest.mark.asyncio
async def test_rerank_trace_orders_equal_scores_deterministically() -> None:
    """Equal reranker scores use stable secondary ordering (preserves input order)."""
    backend = _MockRerankerBackend(scores=[0.5])  # all equal
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.1),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.2),
        _make_scored_candidate("doc2", "text 2", rrf_score=0.3),
    ]

    result = await reranker._rerank_with_trace("query", candidates, top_k=3)

    # All reranker scores equal → stable sort preserves original input order
    assert [r.doc_id for r in result] == ["doc0", "doc1", "doc2"]


@pytest.mark.asyncio
async def test_rerank_empty_candidates_keeps_no_scores() -> None:
    """Empty candidate list returns cleanly with no scores."""
    backend = _MockRerankerBackend()
    reranker = Reranker(backend)

    result = await reranker._rerank_with_trace("query", [], top_k=5)

    assert result == []


def test_model_reranker_uses_submodule_import_path() -> None:
    """Regression: predict() must resolve TextCrossEncoder from fastembed.rerank.cross_encoder.

    If the import in reranker.py is reverted to `from fastembed import TextCrossEncoder`,
    this test fails because TextCrossEncoder is temporarily hidden from the top-level module.
    """
    import sys

    from archon_search.reranker import ModelReranker

    top_level = sys.modules["fastembed"]
    original = getattr(top_level, "TextCrossEncoder", None)
    # Hide TextCrossEncoder from the top-level fastembed module
    if hasattr(top_level, "TextCrossEncoder"):
        delattr(top_level, "TextCrossEncoder")
    try:
        reranker = ModelReranker("test-model")
        result = reranker.predict([("query", "doc")])
        assert result == [0.5], f"Expected [0.5], got {result}"
    finally:
        if original is not None:
            top_level.TextCrossEncoder = original


# ---------------------------------------------------------------------------
# Stage recording tests — Task 3.2 (B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_records_stage_when_bound() -> None:
    """Reranker.rerank records 'rerank' stage timing when a recorder is bound."""
    from archon_search.observability import bind_stage_recorder

    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    with bind_stage_recorder() as recorder:
        await reranker.rerank("query", candidates, top_k=2)
    assert "rerank" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["rerank"] >= 0


@pytest.mark.asyncio
async def test_rerank_noop_when_unbound() -> None:
    """Reranker.rerank works normally with no recorder bound."""
    from archon_search.observability import _stage_recorder

    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    assert _stage_recorder.get() is None
    result = await reranker.rerank("query", candidates, top_k=2)
    assert result is not None


@pytest.mark.asyncio
async def test_rerank_with_trace_records_stage_when_bound() -> None:
    """Reranker._rerank_with_trace records 'rerank' stage (explain path)."""
    from archon_search.observability import bind_stage_recorder

    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    scored = [_make_scored_candidate(f"doc{i}", f"text {i}", 0.5) for i in range(2)]
    with bind_stage_recorder() as recorder:
        await reranker._rerank_with_trace("query", scored, top_k=2)
    assert "rerank" in recorder.stage_timings_ms


@pytest.mark.asyncio
async def test_rerank_candidates_records_stage_on_empty_input() -> None:
    """S345: rerank_candidates must record 'rerank' stage even with empty candidates.

    The docs (20_monitoring_and_alerts.md:82) list 'rerank' as a mandatory
    stage key whenever rerank=True.  The early-return for empty candidates
    must not skip the record_stage("rerank") call.
    """
    from archon_search.observability import bind_stage_recorder

    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    with bind_stage_recorder() as recorder:
        result = await reranker.rerank_candidates("query", [], top_k=5)
    assert result == []
    assert "rerank" in recorder.stage_timings_ms, (
        f"'rerank' missing from stage_timings_ms; present keys={sorted(recorder.stage_timings_ms)}"
    )
    assert recorder.stage_timings_ms["rerank"] >= 0


# ===========================================================================
# rerank_candidates — Task 2.1 (B3): unified production-grade candidate surface
# ===========================================================================


def test_rerank_candidates_is_public() -> None:
    """rerank_candidates is a public method (no leading underscore)."""
    assert hasattr(Reranker, "rerank_candidates")
    assert not Reranker.rerank_candidates.__name__.startswith("_")


@pytest.mark.asyncio
async def test_rerank_candidates_returns_scored_candidates() -> None:
    """rerank_candidates returns ScoredSearchCandidates sorted by reranker_score desc."""
    backend = _MockRerankerBackend(scores=[0.3, 0.9])
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.5),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.7),
    ]

    result = await reranker.rerank_candidates("query", candidates, top_k=2)

    assert all(isinstance(c, ScoredSearchCandidate) for c in result)
    assert result[0].doc_id == "doc1"
    assert result[0].score_breakdown.reranker_score == pytest.approx(0.9)
    assert result[1].doc_id == "doc0"
    assert result[1].score_breakdown.reranker_score == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_rerank_candidates_stable_sort_on_equal_scores() -> None:
    """Equal reranker scores preserve input order (stable sort)."""
    backend = _MockRerankerBackend(scores=[0.5])  # all equal
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.1),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.2),
        _make_scored_candidate("doc2", "text 2", rrf_score=0.3),
    ]

    result = await reranker.rerank_candidates("query", candidates, top_k=3)

    assert [r.doc_id for r in result] == ["doc0", "doc1", "doc2"]


@pytest.mark.asyncio
async def test_rerank_with_trace_alias_delegates() -> None:
    """_rerank_with_trace delegates to rerank_candidates (same instance)."""
    from unittest.mock import patch

    backend = _MockRerankerBackend(scores=[0.9, 0.3])
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.5),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.7),
    ]

    with patch.object(
        reranker, "rerank_candidates", wraps=reranker.rerank_candidates
    ) as spy:
        result = await reranker._rerank_with_trace("query", candidates, top_k=2)

    spy.assert_called_once_with("query", candidates, 2)
    assert [c.doc_id for c in result] == ["doc0", "doc1"]


# ===========================================================================
# is_warm — Task 2.2 (B2)
# ===========================================================================


def test_model_reranker_is_warm_false_before_predict() -> None:
    mr = ModelReranker("some-model")
    assert mr.is_warm is False
    assert mr._model is None


def test_model_reranker_is_warm_true_after_model_set() -> None:
    mr = ModelReranker("some-model")
    mr._model = object()
    assert mr.is_warm is True


def test_reranker_is_warm_delegates_to_backend() -> None:
    backend = _MockRerankerBackend()
    backend.is_warm = False
    reranker = Reranker(backend)
    assert reranker.is_warm is False
    backend.is_warm = True
    assert reranker.is_warm is True


def test_reading_reranker_is_warm_does_not_construct_TextCrossEncoder() -> None:
    from unittest.mock import patch

    with patch(
        "fastembed.rerank.cross_encoder.TextCrossEncoder",
        side_effect=RuntimeError("should not be called"),
    ):
        mr = ModelReranker("x")
        result = mr.is_warm
    assert result is False


def test_reading_reranker_is_warm_does_not_acquire_lock() -> None:
    import time

    mr = ModelReranker("x")
    lock_acquired = threading.Event()
    test_done = threading.Event()

    def hold_lock() -> None:
        with mr._lock:
            lock_acquired.set()
            test_done.wait(timeout=5.0)

    t = threading.Thread(target=hold_lock)
    t.start()
    lock_acquired.wait(timeout=5.0)

    start = time.monotonic()
    result = mr.is_warm
    elapsed = time.monotonic() - start

    test_done.set()
    t.join()

    assert result is False
    assert elapsed < 0.1


def test_mock_reranker_backend_satisfies_protocol_after_is_warm_added() -> None:
    assert isinstance(_MockRerankerBackend(), RerankerBackend)


def test_model_reranker_is_warm_true_after_predict() -> None:
    from unittest.mock import MagicMock, patch

    fake_model = MagicMock()
    fake_model.rerank.return_value = [0.9]
    mr = ModelReranker("some-model")
    assert mr.is_warm is False
    with patch("fastembed.rerank.cross_encoder.TextCrossEncoder", return_value=fake_model):
        mr.predict([("query", "doc")])
    assert mr.is_warm is True


def test_reranker_is_warm_propagates_backend_exception() -> None:
    """Reranker.is_warm propagates exceptions raised by the backend property."""

    class _BrokenBackend:
        is_warm: bool = False  # satisfy Protocol at class level

        @property  # type: ignore[override]
        def is_warm(self) -> bool:  # type: ignore[misc]
            raise RuntimeError("backend broken")

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return []

    reranker = Reranker(_BrokenBackend())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="backend broken"):
        _ = reranker.is_warm

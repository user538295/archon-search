"""packages/archon-search/tests/test_reranker.py — unit tests for Reranker (fastembed backend)."""
from __future__ import annotations

import pytest

from archon_search._types import SearchResult
from archon_search.reranker import Reranker, RerankerBackend, make_reranker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockRerankerBackend:
    """Returns scores passed in at construction time, in order."""

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
# FEAT-038 Task 12.6 — P14.5–P14.7: Reranker edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_P14_5_reranker_top_k_greater_than_candidates_returns_all() -> None:
    """P14.5 — top_k > len(candidates): returns all candidates (no IndexError)."""
    backend = _MockRerankerBackend(scores=[0.9, 0.3])
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    result = await reranker.rerank("query", candidates, top_k=10)
    # All 2 candidates returned, no crash
    assert len(result) == 2


@pytest.mark.asyncio
async def test_P14_6_reranker_stable_order_on_equal_scores() -> None:
    """P14.6 — all candidates have equal scores: order is stable (sorted is stable in Python)."""
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
    """P14.7 — backend returns different number of scores than candidates → ValueError."""
    class _BadCountBackend:
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

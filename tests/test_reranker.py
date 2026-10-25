"""Unit tests for Reranker (async wrapper) and ModelReranker (PyTorch backend, tiny synthetic cross-encoders)."""
from __future__ import annotations

import asyncio
import errno
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import _tiny_encoder as tiny_encoder
import pytest

import archon_search.reranker as reranker_module
from archon_search import text_batching, warmup_coordinator
from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.model_specs import UnsupportedModelError
from archon_search.model_store import ModelDownloadError
from archon_search.reranker import (
    ModelReranker,
    Reranker,
    RerankerBackend,
    RerankerWarmupTimeout,
    make_reranker,
)

pytestmark = pytest.mark.real_torch_backends


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


def _make_candidates(n: int) -> list[ScoredSearchCandidate]:
    return [_make_scored_candidate(f"doc{i}", f"text {i}", rrf_score=0.5) for i in range(n)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranker_sorts_by_score_descending() -> None:
    """Candidates are returned in descending reranker-score order."""
    backend = _MockRerankerBackend(scores=[0.9, 0.1, 0.5])
    reranker = Reranker(backend)
    candidates = _make_candidates(3)

    result = await reranker.rerank_candidates("query", candidates, top_k=3)

    assert [candidate.doc_id for candidate in result] == ["doc0", "doc2", "doc1"]
    assert [candidate.score_breakdown.reranker_score for candidate in result] == pytest.approx([0.9, 0.5, 0.1])


@pytest.mark.asyncio
async def test_reranker_truncates_to_top_k() -> None:
    backend = _MockRerankerBackend(scores=[0.5])
    reranker = Reranker(backend)
    candidates = _make_candidates(5)
    result = await reranker.rerank_candidates("q", candidates, top_k=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_reranker_empty_candidates_returns_empty() -> None:
    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    result = await reranker.rerank_candidates("q", [], top_k=5)
    assert result == []
    assert backend.called_pairs == []


@pytest.mark.asyncio
async def test_reranker_calls_backend_with_correct_pairs() -> None:
    backend = _MockRerankerBackend(scores=[0.8, 0.3])
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    await reranker.rerank_candidates("my query", candidates, top_k=2)

    assert len(backend.called_pairs) == 1
    pairs = backend.called_pairs[0]
    assert pairs[0] == ("my query", "text 0")
    assert pairs[1] == ("my query", "text 1")


@pytest.mark.asyncio
async def test_make_reranker_returns_a_working_reranker_over_a_pytorch_backend(
    use_tiny_reranker: Callable[..., MagicMock], reference_logits: Callable[..., list[float]]
) -> None:
    use_tiny_reranker("bert")
    r = make_reranker("tiny/bert-reranker", device="cpu")
    assert isinstance(r, Reranker)
    assert r.is_warm is False
    candidates = [_make_scored_candidate("d0", "w3 w4", rrf_score=0.5)]

    result = await r.rerank_candidates("w1 w2", candidates, top_k=1)

    assert len(result) == 1
    expected = reference_logits("bert", [("w1 w2", "w3 w4")])[0]
    assert result[0].score_breakdown.reranker_score == pytest.approx(expected, abs=_TOLERANCE)
    assert r.is_warm is True


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
    result = await reranker.rerank_candidates("query", candidates, top_k=10)
    # All 2 candidates returned, no crash
    assert len(result) == 2


@pytest.mark.asyncio
async def test_P14_6_reranker_stable_order_on_equal_scores() -> None:
    """ all candidates have equal scores: order is stable (sorted is stable in Python)."""
    backend = _MockRerankerBackend(scores=[0.5])
    reranker = Reranker(backend)
    candidates = _make_candidates(4)
    original_ids = [c.doc_id for c in candidates]
    result = await reranker.rerank_candidates("query", candidates, top_k=4)
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
        await reranker.rerank_candidates("query", candidates, top_k=3)


def test_model_reranker_init_called_once_under_concurrent_predict(
    tiny_snapshots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-checked locking: concurrent predict() calls load the model exactly once."""
    loads = 0
    barrier = threading.Barrier(2)

    def slow_ensure(_spec: object) -> Path:
        nonlocal loads
        loads += 1
        time.sleep(0.05)  # slow enough to expose the race
        return tiny_snapshots["bert"]

    monkeypatch.setattr(reranker_module, "get_reranker_spec", lambda _name: tiny_encoder.tiny_reranker_spec("bert"))
    monkeypatch.setattr(reranker_module, "ensure_model", slow_ensure)
    reranker = ModelReranker("tiny/bert-reranker", device="cpu")
    results: list[list[float]] = []
    exceptions: list[Exception] = []

    def run_predict() -> None:
        barrier.wait()  # force simultaneous entry
        try:
            results.append(reranker.predict([("w1", "w2"), ("w1", "w3")]))
        except Exception as exc:  # noqa: BLE001 — concurrency test collects any exception from worker threads to assert none occurred
            exceptions.append(exc)

    threads = [threading.Thread(target=run_predict) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not exceptions, f"Unexpected exceptions: {exceptions}"
    assert len(results) == 2
    assert results[0] == results[1]
    assert loads == 1, f"Model loaded {loads} times — lock missing"


# ===========================================================================
# Reranker trace preservation
# ===========================================================================



@pytest.mark.asyncio
async def test_rerank_candidates_preserves_prerank_scores_and_adds_reranker_score() -> None:
    """Reranked candidates keep fused/RRF score and add reranker_score."""
    backend = _MockRerankerBackend(scores=[0.9, 0.3])
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.5),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.7),
    ]

    result = await reranker.rerank_candidates("query", candidates, top_k=2)

    assert len(result) == 2
    # Sorted by reranker score descending: doc0 (0.9) first
    assert result[0].doc_id == "doc0"
    assert result[0].score_breakdown.reranker_score == pytest.approx(0.9)
    assert result[0].score_breakdown.rrf_score == pytest.approx(0.5)  # original preserved

    assert result[1].doc_id == "doc1"
    assert result[1].score_breakdown.reranker_score == pytest.approx(0.3)
    assert result[1].score_breakdown.rrf_score == pytest.approx(0.7)  # original preserved


@pytest.mark.asyncio
async def test_rerank_candidates_does_not_mutate_the_input_candidates() -> None:
    """Input candidate objects must remain unchanged after reranking runs."""
    backend = _MockRerankerBackend(scores=[0.9, 0.1])
    reranker = Reranker(backend)
    candidates = [
        _make_scored_candidate("doc0", "text 0", rrf_score=0.4),
        _make_scored_candidate("doc1", "text 1", rrf_score=0.6),
    ]
    original_rrf_scores = [c.score_breakdown.rrf_score for c in candidates]
    original_reranker_scores = [c.score_breakdown.reranker_score for c in candidates]

    await reranker.rerank_candidates("query", candidates, top_k=2)

    for candidate, orig_rrf, orig_reranker in zip(
        candidates, original_rrf_scores, original_reranker_scores
    ):
        assert candidate.score_breakdown.rrf_score == pytest.approx(orig_rrf)
        assert candidate.score_breakdown.reranker_score == orig_reranker




# ---------------------------------------------------------------------------
# Stage recording tests — Task 3.2 (B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_candidates_records_stage_when_bound() -> None:
    """Reranker.rerank_candidates records 'rerank' stage timing when a recorder is bound."""
    from archon_search.observability import bind_stage_recorder

    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    with bind_stage_recorder() as recorder:
        await reranker.rerank_candidates("query", candidates, top_k=2)
    assert "rerank" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["rerank"] >= 0


@pytest.mark.asyncio
async def test_rerank_candidates_noop_when_unbound() -> None:
    """Reranker.rerank_candidates works normally with no recorder bound."""
    from archon_search.observability import _stage_recorder

    backend = _MockRerankerBackend()
    reranker = Reranker(backend)
    candidates = _make_candidates(2)
    assert _stage_recorder.get() is None
    result = await reranker.rerank_candidates("query", candidates, top_k=2)
    assert result is not None


@pytest.mark.asyncio
async def test_rerank_candidates_records_stage_on_empty_input() -> None:
    """S345: rerank_candidates must record 'rerank' stage even with empty candidates.

    The docs (20_monitoring_and_alerts.md §"Stage-level latency and request-correlation IDs")
    list 'rerank' as a mandatory stage key whenever rerank=True.  The early-return
    for empty candidates must not skip the record_stage("rerank") call.
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


# ===========================================================================
# is_warm — Task 2.2 (B2)
# ===========================================================================


def test_model_reranker_is_warm_false_before_predict() -> None:
    mr = ModelReranker("BAAI/bge-reranker-base", device="cpu")
    assert mr.is_warm is False


def test_reranker_is_warm_delegates_to_backend() -> None:
    backend = _MockRerankerBackend()
    backend.is_warm = False
    reranker = Reranker(backend)
    assert reranker.is_warm is False
    backend.is_warm = True
    assert reranker.is_warm is True




def test_mock_reranker_backend_satisfies_protocol_after_is_warm_added() -> None:
    assert isinstance(_MockRerankerBackend(), RerankerBackend)




def test_reranker_is_warm_propagates_backend_exception() -> None:
    """Reranker.is_warm propagates exceptions raised by the backend property."""

    class _BrokenBackend:
        @property
        def is_warm(self) -> bool:
            raise RuntimeError("backend broken")

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return []

    reranker = Reranker(_BrokenBackend())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="backend broken"):
        _ = reranker.is_warm


@pytest.mark.asyncio
async def test_warmup_retries_after_a_failed_attempt() -> None:
    """A failed warmup() must not permanently latch — the next call retries.

    One transient failure (HF 429, network blip, unwritable cache) must not disarm every later
    warm-up for the life of the process.
    """

    class _FailOnceBackend:
        is_warm: bool = False

        def __init__(self) -> None:
            self.call_count = 0

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("transient failure")
            self.is_warm = True
            return [0.5] * len(pairs)

    backend = _FailOnceBackend()
    reranker = Reranker(backend)

    with pytest.raises(RuntimeError, match="transient failure"):
        await reranker.warmup()
    assert backend.call_count == 1
    assert reranker.is_warm is False

    # A second call must retry (not silently no-op) and succeed this time.
    await reranker.warmup()
    assert backend.call_count == 2
    assert reranker.is_warm is True

    # Now warm: a third call must be a true no-op (no further predict calls).
    await reranker.warmup()
    assert backend.call_count == 2



# ---------------------------------------------------------------------------
# C2-T-1 — single-flight warm-up (shared task + asyncio.shield), S288
# ---------------------------------------------------------------------------


class _BlockingBackend:
    """Backend whose ``predict`` parks on a threading.Event until released."""

    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.is_warm = False
        self.build_count = 0
        self.fail = fail
        self.hang = hang
        self.started = threading.Event()
        self.release = threading.Event()

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.build_count += 1
        self.started.set()
        if not self.hang:
            self.release.wait(timeout=5.0)
        else:
            self.release.wait(timeout=30.0)
        if self.fail:
            raise RuntimeError("build failed")
        self.is_warm = True
        return [0.5] * len(pairs)


@pytest.mark.asyncio
async def test_concurrent_cold_warmups_trigger_exactly_one_build() -> None:
    """N concurrent cold ``warmup()`` callers share one backend build (S288).

    Without the shared task each fan-out request would start its own ONNX
    build — the thundering herd the single-flight rewrite exists to prevent.
    """
    backend = _BlockingBackend()
    reranker = Reranker(backend)  # type: ignore[arg-type]

    waiters = [asyncio.create_task(reranker.warmup()) for _ in range(8)]
    await asyncio.to_thread(backend.started.wait, 5.0)
    backend.release.set()
    await asyncio.gather(*waiters)

    assert backend.build_count == 1, (
        f"expected exactly one shared build, got {backend.build_count}"
    )
    assert reranker.is_warm is True


@pytest.mark.asyncio
async def test_abandoned_waiter_does_not_cancel_the_shared_build() -> None:
    """A caller that gives up on waiting must not kill the build (S288).

    ``warmup()`` awaits ``asyncio.shield(task)``, so ``wait_for`` cancels only
    the *wait*. A later caller joins the same task rather than starting a
    second ONNX build, and the backend still ends warm.
    """
    backend = _BlockingBackend()
    reranker = Reranker(backend)  # type: ignore[arg-type]

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reranker.warmup(), timeout=0.01)
    await asyncio.to_thread(backend.started.wait, 5.0)
    assert backend.build_count == 1

    joiner = asyncio.create_task(reranker.warmup())
    await asyncio.sleep(0)
    backend.release.set()
    await joiner

    assert backend.build_count == 1, (
        f"the second caller started a duplicate build ({backend.build_count} total)"
    )
    assert reranker.is_warm is True


@pytest.mark.asyncio
async def test_failed_shared_build_is_retried_by_the_next_caller() -> None:
    """A failed shared build with a second caller already joined is retried.

    Both waiters must see the failure, and the settled task must be dropped so
    the next ``warmup()`` starts a fresh build instead of re-awaiting a task
    that will only ever re-raise.
    """

    class _FailThenSucceed:
        def __init__(self) -> None:
            self.is_warm = False
            self.build_count = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            self.build_count += 1
            self.started.set()
            self.release.wait(timeout=5.0)
            if self.build_count == 1:
                raise RuntimeError("transient build failure")
            self.is_warm = True
            return [0.5] * len(pairs)

    backend = _FailThenSucceed()
    reranker = Reranker(backend)  # type: ignore[arg-type]

    first = asyncio.create_task(reranker.warmup())
    await asyncio.to_thread(backend.started.wait, 5.0)
    second = asyncio.create_task(reranker.warmup())
    await asyncio.sleep(0)
    backend.release.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results), results
    assert backend.build_count == 1

    backend.release.clear()
    backend.started.clear()
    retry = asyncio.create_task(reranker.warmup())
    await asyncio.to_thread(backend.started.wait, 5.0)
    backend.release.set()
    await retry

    assert backend.build_count == 2, "the failed shared task was not retried"
    assert reranker.is_warm is True


@pytest.mark.asyncio
async def test_warmup_raises_rerankerwarmuptimeout_not_asyncio_timeouterror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-long build raises ``RerankerWarmupTimeout``, a plain ``Exception``.

    It must NOT be an ``asyncio.TimeoutError``: since 3.11 that is the builtin
    ``TimeoutError`` (an ``OSError``), which callers use to mean "I gave up
    waiting", a different condition from "the build itself blew its ceiling".
    """
    monkeypatch.setattr(warmup_coordinator, "WARMUP_TIMEOUT_SECONDS", 0.01)
    backend = _BlockingBackend(hang=True)
    reranker = Reranker(backend)  # type: ignore[arg-type]

    try:
        with pytest.raises(RerankerWarmupTimeout, match="reranker warm-up exceeded"):
            await reranker.warmup()
    finally:
        backend.release.set()

    assert not issubclass(RerankerWarmupTimeout, asyncio.TimeoutError)
    assert not issubclass(RerankerWarmupTimeout, OSError)
    assert issubclass(RerankerWarmupTimeout, warmup_coordinator.WarmupTimeout)


@pytest.mark.asyncio
async def test_warmup_reports_a_socket_etimedout_as_the_build_failing_not_as_the_ceiling() -> None:
    """A download's ``ETIMEDOUT`` is a ``TimeoutError`` too; it must not become ``RerankerWarmupTimeout``."""

    class _EtimedoutBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            raise TimeoutError(errno.ETIMEDOUT, "Connection timed out")

    reranker = Reranker(_EtimedoutBackend())

    with pytest.raises(TimeoutError) as caught:
        await reranker.warmup()

    assert not isinstance(caught.value, RerankerWarmupTimeout)


# ---------------------------------------------------------------------------
# ModelReranker — PyTorch backend on tiny synthetic cross-encoders (no weights, no network)
# ---------------------------------------------------------------------------

_TOLERANCE = 1e-5
#: Tiny snapshot kinds: (architecture, uses_token_type_ids) of the spec describing them.
_SPEC_OPTIONS: dict[str, bool] = {"bert": True, "xlm_roberta": False}


@pytest.fixture(scope="module")
def tiny_snapshots(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return {
        architecture: tiny_encoder.build_cross_encoder_snapshot(tmp_path_factory.mktemp(f"tiny-{architecture}"), architecture)  # type: ignore[arg-type]
        for architecture in _SPEC_OPTIONS
    }


@pytest.fixture
def use_tiny_reranker(tiny_snapshots: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Callable[..., MagicMock]:
    """Point ``ModelReranker`` at a tiny snapshot; returns the ``ensure_model`` mock."""
    ensure = MagicMock()

    def install(architecture: str, **spec_options: Any) -> MagicMock:
        options = {"uses_token_type_ids": _SPEC_OPTIONS[architecture], **spec_options}
        spec = tiny_encoder.tiny_reranker_spec(architecture, **options)  # type: ignore[arg-type]
        ensure.return_value = tiny_snapshots[architecture]
        monkeypatch.setattr(reranker_module, "get_reranker_spec", lambda _name: spec)
        monkeypatch.setattr(reranker_module, "ensure_model", ensure)
        return ensure

    return install


@pytest.fixture
def reference_logits(tiny_snapshots: dict[str, Path]) -> Callable[..., list[float]]:
    """Raw logits of a plain ``AutoModelForSequenceClassification`` run on hand-built pair ids, one pair at a time."""

    def run(architecture: str, pairs: list[tuple[str, str]], *, token_types: bool | None = None) -> list[float]:
        import torch
        from transformers import AutoModelForSequenceClassification

        use_types = _SPEC_OPTIONS[architecture] if token_types is None else token_types
        model = AutoModelForSequenceClassification.from_pretrained(tiny_snapshots[architecture], dtype=torch.float32).eval()
        logits: list[float] = []
        for query, document in pairs:
            query_ids, document_ids = tiny_encoder.token_ids(query), tiny_encoder.token_ids(document)[1:]
            inputs = {"input_ids": torch.tensor([[*query_ids, *document_ids]])}
            if use_types:
                inputs["token_type_ids"] = torch.tensor([[0] * len(query_ids) + [1] * len(document_ids)])
            with torch.inference_mode():
                logits.append(model(**inputs).logits[0, 0].item())
        return logits

    return run


def _predict(pairs: list[tuple[str, str]]) -> list[float]:
    return ModelReranker("tiny/bert-reranker", device="cpu").predict(pairs)


def test_an_unsupported_model_raises_at_construction() -> None:
    with pytest.raises(UnsupportedModelError, match="no-such-model"):
        ModelReranker("no-such-model")


def test_empty_input_returns_empty_list_without_loading(use_tiny_reranker: Callable[..., MagicMock]) -> None:
    ensure = use_tiny_reranker("bert")
    reranker = ModelReranker("tiny/bert-reranker", device="cpu")

    assert reranker.predict([]) == []
    ensure.assert_not_called()
    assert reranker.is_warm is False


def test_lazy_load_and_is_warm(use_tiny_reranker: Callable[..., MagicMock]) -> None:
    ensure = use_tiny_reranker("bert")
    reranker = ModelReranker("tiny/bert-reranker", device="cpu")
    assert reranker.is_warm is False
    ensure.assert_not_called()

    reranker.predict([("w1", "w2")])
    reranker.predict([("w1", "w3")])

    assert reranker.is_warm is True
    ensure.assert_called_once()


def test_load_failure_is_not_latched(tiny_snapshots: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reranker_module, "get_reranker_spec", lambda _name: tiny_encoder.tiny_reranker_spec("bert"))
    ensure = MagicMock(side_effect=[ModelDownloadError("tiny/bert", "offline"), tiny_snapshots["bert"]])
    monkeypatch.setattr(reranker_module, "ensure_model", ensure)
    reranker = ModelReranker("tiny/bert-reranker", device="cpu")

    with pytest.raises(ModelDownloadError):
        reranker.predict([("w1", "w2")])
    assert reranker.is_warm is False

    assert len(reranker.predict([("w1", "w2")])) == 1
    assert reranker.is_warm is True


def test_reading_is_warm_does_not_wait_for_a_load_in_progress(
    tiny_snapshots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking_ensure(_spec: object) -> Path:
        entered.set()
        release.wait(timeout=5.0)
        return tiny_snapshots["bert"]

    monkeypatch.setattr(reranker_module, "get_reranker_spec", lambda _name: tiny_encoder.tiny_reranker_spec("bert"))
    monkeypatch.setattr(reranker_module, "ensure_model", blocking_ensure)
    reranker = ModelReranker("tiny/bert-reranker", device="cpu")
    loader = threading.Thread(target=reranker.predict, args=([("w1", "w2")],))
    loader.start()
    try:
        assert entered.wait(timeout=5.0)
        started = time.monotonic()
        warm = reranker.is_warm
        elapsed = time.monotonic() - started
    finally:
        release.set()
        loader.join()

    assert warm is False
    assert elapsed < 0.1
    assert reranker.is_warm is True


@pytest.mark.parametrize("architecture", ["bert", "xlm_roberta"])
def test_scores_are_raw_logits_not_sigmoid(
    architecture: str, use_tiny_reranker: Callable[..., MagicMock], reference_logits: Callable[..., list[float]]
) -> None:
    import math

    use_tiny_reranker(architecture)
    pairs = [("w1 w2", "w3 w4 w5"), ("w6", "w7 w8")]
    expected = reference_logits(architecture, pairs)

    scores = _predict(pairs)

    assert scores == pytest.approx(expected, abs=_TOLERANCE)
    for score, logit in zip(scores, expected, strict=True):
        assert abs(score - 1 / (1 + math.exp(-logit))) > 1e-3


def test_token_type_ids_are_passed_when_the_spec_asks_for_them(
    use_tiny_reranker: Callable[..., MagicMock], reference_logits: Callable[..., list[float]]
) -> None:
    use_tiny_reranker("bert")
    pairs = [("w1 w2", "w3 w4 w5")]
    with_types = reference_logits("bert", pairs, token_types=True)
    without_types = reference_logits("bert", pairs, token_types=False)
    assert abs(with_types[0] - without_types[0]) > 1e-3  # the comparison below is only meaningful then

    assert _predict(pairs) == pytest.approx(with_types, abs=_TOLERANCE)


def test_token_type_ids_are_omitted_when_the_spec_does_not_ask_for_them(
    use_tiny_reranker: Callable[..., MagicMock], reference_logits: Callable[..., list[float]]
) -> None:
    use_tiny_reranker("bert", uses_token_type_ids=False)
    pairs = [("w1 w2", "w3 w4 w5")]

    assert _predict(pairs) == pytest.approx(reference_logits("bert", pairs, token_types=False), abs=_TOLERANCE)


def test_xlm_roberta_runs_without_token_type_ids(
    use_tiny_reranker: Callable[..., MagicMock], reference_logits: Callable[..., list[float]]
) -> None:
    use_tiny_reranker("xlm_roberta")
    pairs = [("w1 w2", "w3 w4 w5")]

    assert _predict(pairs) == pytest.approx(reference_logits("xlm_roberta", pairs), abs=_TOLERANCE)


def test_order_preserved_across_batches(
    use_tiny_reranker: Callable[..., MagicMock],
    reference_logits: Callable[..., list[float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_tiny_reranker("bert")
    monkeypatch.setattr(text_batching, "DEFAULT_TOKEN_BUDGET", 12)
    # Pair lengths 6..17 tokens, scrambled, with differing queries: a length-sorted plan cannot be the identity.
    pairs = [
        ("w1 w2", "w1 w2 w3 w4 w5 w6"),
        ("w3", "w1"),
        ("w4 w5 w6", "w7 w8 w9 w10 w11 w12 w13 w14"),
        ("w2", "w5 w6 w7"),
        ("w9 w8", "w4 w3 w2 w1 w7"),
        ("w1", "w2 w3"),
    ]
    plans: list[list[list[int]]] = []
    real_plan = text_batching.plan_batches
    monkeypatch.setattr(text_batching, "plan_batches", lambda *a, **k: plans.append(real_plan(*a, **k)) or plans[-1])

    scores = _predict(pairs)

    assert len(plans[0]) > 1
    assert [i for batch in plans[0] for i in batch] != list(range(len(pairs)))
    assert scores == pytest.approx(reference_logits("bert", pairs), abs=_TOLERANCE)


def test_padding_does_not_change_a_score(use_tiny_reranker: Callable[..., MagicMock]) -> None:
    use_tiny_reranker("xlm_roberta")
    pairs = [("w1", "w2"), ("w1 w2 w3", "w4 w5 w6 w7 w8"), ("w3", "w9 w10"), ("w5 w6", "w7")]

    batched = _predict(pairs)
    one_by_one = [_predict([pair])[0] for pair in pairs]

    assert batched == pytest.approx(one_by_one, abs=_TOLERANCE)


def test_long_document_is_truncated_to_spec_length(use_tiny_reranker: Callable[..., MagicMock]) -> None:
    use_tiny_reranker("bert", max_length=8)  # [CLS] 2 query words [SEP] + 3 document words + [SEP]
    long_document = " ".join(f"w{i}" for i in range(1, 15))

    truncated = _predict([("w1 w2", long_document)])[0]
    three_words = _predict([("w1 w2", "w1 w2 w3")])[0]
    use_tiny_reranker("bert", max_length=tiny_encoder.MAX_POSITIONS)
    untruncated = _predict([("w1 w2", long_document)])[0]

    assert truncated == pytest.approx(three_words, abs=_TOLERANCE)
    assert abs(truncated - untruncated) > 1e-3


def test_scores_are_python_floats_one_per_pair(use_tiny_reranker: Callable[..., MagicMock]) -> None:
    use_tiny_reranker("bert")

    scores = _predict([("w1", "w2"), ("w3", "w4"), ("w5", "w6")])

    assert len(scores) == 3
    assert all(type(score) is float for score in scores)


def test_accelerator_cache_is_released_only_after_an_oversize_batch(
    use_tiny_reranker: Callable[..., MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_tiny_reranker("bert")
    monkeypatch.setattr(text_batching, "DEFAULT_TOKEN_BUDGET", 12)
    release = MagicMock()
    monkeypatch.setattr(reranker_module, "release_accelerator_cache", release)
    reranker = ModelReranker("tiny/bert-reranker", device="cpu")

    reranker.predict([("w1", "w2"), ("w3", "w4")])  # 5 tokens each: one batch of two, 10 <= 12
    release.assert_not_called()

    reranker.predict([("w1 w2 w3 w4", "w5 w6 w7 w8")])  # 11 tokens alone: padded 11 <= 12 ...
    release.assert_not_called()
    reranker.predict([("w1 w2 w3 w4 w5", "w5 w6 w7 w8 w9")])  # 13 tokens alone: 13 > 12
    release.assert_called_once_with("cpu")


def test_importing_the_reranker_module_does_not_import_the_ml_stack() -> None:
    code = (
        "import sys; import archon_search.reranker; "
        "loaded = [m for m in ('torch', 'transformers', 'tokenizers', 'safetensors', 'onnxruntime') if m in sys.modules]; "
        "assert not loaded, loaded; print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.stdout.strip() == "ok", result.stderr

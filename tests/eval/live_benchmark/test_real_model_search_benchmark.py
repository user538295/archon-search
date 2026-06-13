"""Real-model search latency benchmark tests.

Marked ``live_benchmark`` — excluded from default ``uv run pytest`` run via
``addopts`` in pyproject.toml. Run explicitly with::

    uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov

Requires the fastembed model cache to be populated (see conftest._require_model_cache).
The conftest removes fastembed stubs and resets ML thread-count env vars before any
test imports, ensuring the real ONNX code path is exercised.
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import fastembed
import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder import make_embedder
from archon_search.eval.runner import BenchmarkThresholds, load_benchmark_thresholds
from archon_search.reranker import make_reranker

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_STEADY_STATE_WARMUP = 5
_STEADY_STATE_ITERS = 100
_COLD_LOAD_ITERS = 10
_BENCHMARK_QUERY = "async HTTP client with retry logic and timeout"  # queries.jsonl entry #0
_BENCHMARK_COLLECTION = "code"
_TOP_K_RETRIEVE = 15
_TOP_K_RETURN = 5

_CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus" / "code"
_LIVE_THRESHOLDS_PATH = Path("tests/eval/live_thresholds.toml")


# ---------------------------------------------------------------------------
# Unit: verify fastembed stub is NOT active in live_benchmark context
# ---------------------------------------------------------------------------


@pytest.mark.live_benchmark
@pytest.mark.xdist_group("live_benchmark")
def test_stub_not_active_in_live_benchmark() -> None:
    """The conftest should have removed fastembed stubs from sys.modules."""
    assert "_search_stubs" not in fastembed.TextEmbedding.__module__, (
        f"fastembed.TextEmbedding resolved to stub class: "
        f"{fastembed.TextEmbedding!r} from module {fastembed.TextEmbedding.__module__!r}"
    )


# ---------------------------------------------------------------------------
# Unit: steady-state p95 assertion logic (no real models needed)
# ---------------------------------------------------------------------------


@pytest.mark.live_benchmark
@pytest.mark.xdist_group("live_benchmark")
def test_steady_state_p95_assertion_fires_on_regression() -> None:
    """Validate the p95 formula and threshold comparison without running real models."""
    # Build a times list where the 95th percentile exceeds the threshold
    times_ms = [1.0] * 94 + [999.0, 999.0, 999.0, 999.0, 999.0, 999.0]  # 100 values
    assert len(times_ms) == 100
    p95 = sorted(times_ms)[int(math.ceil(0.95 * len(times_ms))) - 1]
    assert p95 == 999.0
    threshold = 500.0
    with pytest.raises(AssertionError, match="Steady-state p95"):
        assert p95 <= threshold, (
            f"Steady-state p95 {p95:.1f} ms exceeds threshold {threshold:.1f} ms"
        )


# ---------------------------------------------------------------------------
# Unit: cold-load p90 assertion logic (no real models needed)
# ---------------------------------------------------------------------------


@pytest.mark.live_benchmark
@pytest.mark.xdist_group("live_benchmark")
def test_cold_load_p90_assertion_fires_on_regression() -> None:
    """Validate the p90 formula for N=10 and threshold comparison without real models."""
    # For N=10: ceil(0.90*10)-1 = ceil(9.0)-1 = 9-1 = 8 → index 8 (0-based) = 9th value
    # Need sorted[8] = 999.0: requires at most 8 values below 999.0 → [1.0]*8 + [999.0, 999.0]
    times_ms = [1.0] * 8 + [999.0, 999.0]  # 10 values; sorted[8] = 999.0
    assert len(times_ms) == 10
    p90 = sorted(times_ms)[int(math.ceil(0.90 * _COLD_LOAD_ITERS)) - 1]
    assert p90 == 999.0
    threshold = 500.0
    with pytest.raises(AssertionError, match="Cold-load p90"):
        assert p90 <= threshold, (
            f"Cold-load p90 {p90:.1f} ms exceeds threshold {threshold:.1f} ms"
        )


# ---------------------------------------------------------------------------
# Async helpers: full benchmark lifecycle in one asyncio.run() call
# ---------------------------------------------------------------------------
# LanceDB's AsyncConnection is event-loop-bound. All operations on a given
# store instance MUST run within the same event loop that called store.connect().
# Wrapping the entire lifecycle (setup + warmup + measure + teardown) in a single
# asyncio.run() guarantees exactly one event loop per test.


async def _run_steady_state_benchmark(
    tmp_path: Path,
) -> tuple[float, float]:
    """Set up pipeline, ingest corpus, warm up, measure p95, tear down.

    Returns (p95_ms, threshold_ms) for assertion outside the event loop.
    All LanceDB operations run in a single event loop — no cross-loop issues.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.store import SearchStore

    embedder = make_embedder("BAAI/bge-small-en-v1.5")
    reranker = make_reranker("Xenova/ms-marco-MiniLM-L-6-v2")
    store = SearchStore(tmp_path)
    await store.connect()
    try:
        chunker = DocumentChunker(chunk_size=256)
        parser = DocumentParser()
        pipeline = SearchPipeline(
            store=store,
            embedder=embedder,
            reranker=reranker,
            chunker=chunker,
            parser=parser,
            top_k_retrieve=_TOP_K_RETRIEVE,
            top_k_return=_TOP_K_RETURN,
        )

        # Ingest corpus
        for doc_path in sorted(_CORPUS_DIR.glob("*.py")):
            result = await pipeline.ingest_file(
                doc_path,
                _BENCHMARK_COLLECTION,
                rebuild_fts=False,
                embedder=embedder,
                collection_root=_CORPUS_DIR,
            )
            if result.status != "ok":
                raise RuntimeError(f"Ingest failed for {doc_path}: {result.error}")
        await pipeline.store.rebuild_fts_index(_BENCHMARK_COLLECTION)

        chunk_count = await store.count_chunks(_BENCHMARK_COLLECTION, DEFAULT_NAMESPACE)
        assert chunk_count > 0, f"Ingest produced zero chunks in {_BENCHMARK_COLLECTION!r}"

        # Warm-up iterations (unmeasured)
        for i in range(_STEADY_STATE_WARMUP):
            try:
                await pipeline.search(
                    _BENCHMARK_QUERY,
                    _BENCHMARK_COLLECTION,
                    embedder=embedder,
                    rag_fusion=False,
                    filters=None,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Warm-up iteration {i} failed: {exc}; check ONNX session initialization"
                ) from exc

        # Measured iterations
        times: list[float] = []
        for _ in range(_STEADY_STATE_ITERS):
            start = time.perf_counter()
            await pipeline.search(
                _BENCHMARK_QUERY,
                _BENCHMARK_COLLECTION,
                embedder=embedder,
                rag_fusion=False,
                filters=None,
            )
            times.append((time.perf_counter() - start) * 1000)

        p95 = sorted(times)[int(math.ceil(0.95 * len(times))) - 1]
        return p95
    finally:
        try:
            await store.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Steady-state benchmark (100-iter p95)
# ---------------------------------------------------------------------------


@pytest.mark.live_benchmark
@pytest.mark.xdist_group("live_benchmark")
def test_real_model_search_steady_state_p95(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Steady-state search latency: p95 of 100 iterations must be within threshold."""
    thresholds: BenchmarkThresholds = load_benchmark_thresholds(_LIVE_THRESHOLDS_PATH)

    # Verify stubs are not active
    assert "_search_stubs" not in fastembed.TextEmbedding.__module__, (
        "fastembed stub still active in live_benchmark test"
    )

    tmp_path = tmp_path_factory.mktemp("live_bench_steady")
    # Single asyncio.run() call: all LanceDB operations share one event loop.
    p95 = asyncio.run(_run_steady_state_benchmark(tmp_path))

    print(f"\nSteady-state p95: {p95:.1f} ms (threshold: {thresholds.steady_state_p95_ms:.1f} ms)")
    assert p95 <= thresholds.steady_state_p95_ms, (
        f"Steady-state p95 {p95:.1f} ms exceeds threshold {thresholds.steady_state_p95_ms:.1f} ms"
    )


# ---------------------------------------------------------------------------
# Cold-load benchmark (N=10 p90)
# ---------------------------------------------------------------------------


@pytest.mark.live_benchmark
@pytest.mark.xdist_group("live_benchmark")
def test_real_model_search_cold_load_p90() -> None:
    """Cold-load latency: p90 of 10 fresh-backend iterations must be within threshold."""
    thresholds: BenchmarkThresholds = load_benchmark_thresholds(_LIVE_THRESHOLDS_PATH)

    times: list[float] = []
    for _ in range(_COLD_LOAD_ITERS):
        start = time.perf_counter()
        embedder = make_embedder("BAAI/bge-small-en-v1.5")
        reranker = make_reranker("Xenova/ms-marco-MiniLM-L-6-v2")
        # Trigger ONNX session creation for embedder
        asyncio.run(embedder.embed(["warm"]))
        # Trigger ONNX session creation for reranker (synchronous predict)
        reranker._backend.predict([("warm", "warm")])
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)

    # p90 under nearest-rank: for N=10, ceil(0.90*10)-1 = 9-1 = 8 = index 8
    p90 = sorted(times)[int(math.ceil(0.90 * _COLD_LOAD_ITERS)) - 1]
    print(f"\nCold-load p90: {p90:.1f} ms (threshold: {thresholds.cold_load_p90_ms:.1f} ms)")
    assert p90 <= thresholds.cold_load_p90_ms, (
        f"Cold-load p90 {p90:.1f} ms exceeds threshold {thresholds.cold_load_p90_ms:.1f} ms"
    )

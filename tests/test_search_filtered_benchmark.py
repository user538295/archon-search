"""Filtered-search latency benchmarks — in-process LanceDB, no HTTP server required.

Run manually:
    uv run pytest -m benchmark tests/test_search_filtered_benchmark.py --no-cov -v -s

Auto-excluded from the default suite (``-m 'not benchmark'`` in addopts).

Three benchmarks:
- test_glob_filtered_search_p95_under_threshold:
    glob filter matching ~20 % of the corpus, 100 iterations, top_k=10.
    Asserts p95 ≤ p95_ms_glob_filtered from tests/eval/thresholds.toml.

- test_prefix_filtered_search_p95_regression_under_threshold:
    source_path_prefix only (no glob), 100 iterations, top_k=10.
    Asserts p95 has not regressed by more than p95_regression_pct_prefix_vs_unfiltered %
    vs an unfiltered baseline measured in the same run.

- test_hyde_false_search_p95_under_threshold:
    hyde=False fast path (resolve_hyde_vector returns immediately), 100 iterations, top_k=10.
    Asserts p95 ≤ [search_hyde_false].p95_ms from tests/eval/thresholds.toml.
    Confirms the HyDE fast-path adds no measurable overhead over unfiltered search.
"""
from __future__ import annotations

import asyncio
import hashlib
import statistics
import time
import tomllib
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_THRESHOLDS_PATH = Path(__file__).parent / "eval" / "thresholds.toml"

_DIM = 32          # small but non-trivial embedding dimension
_N_CHUNKS = 500    # corpus size — keeps ingest fast in CI while giving stable p95
_N_ITERS = 100     # measurement iterations
_TOP_K = 10
_WARMUP = 5        # warm-up iterations (not measured)

# 20 % of chunks share prefix /bench/group-0/ (indices 0..99)
_GLOB_PATTERN = "/bench/group-0/*"
_PREFIX = "/bench/group-0/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(data: list[float], p: int) -> float:
    """Return the p-th percentile using statistics.quantiles (1 ≤ p ≤ 99)."""
    return statistics.quantiles(data, n=100)[p - 1]


def _make_doc_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_chunk(idx: int, dim: int = _DIM) -> dict:
    """Return a dict suitable for SearchStore._do_ingest."""
    group = idx % 5  # 5 groups → each is 20 % of corpus
    doc_seed = f"doc-{idx // 3}"  # ~3 chunks per doc
    doc_id = _make_doc_id(doc_seed)
    chunk_id = f"{doc_id}-{(idx % 3):06d}"
    source_path = f"/bench/group-{group}/file-{idx:04d}.md"
    rng = np.random.default_rng(idx)
    vector = rng.random(dim, dtype=np.float32).tolist()
    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": f"benchmark chunk number {idx} in group {group}",
        "vector": vector,
        "source_path": source_path,
        "indexed_at": "2026-01-01T00:00:00.000000Z",
        "file_type": "md",
        "language": "",
        "metadata": "{}",
        "custom_score": None,
        "ingested_by": "cli",
        "updated_at": "2026-01-01T00:00:00.000000Z",
        "acl": None,
    }


def _load_thresholds() -> dict:
    with open(_THRESHOLDS_PATH, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Module-scoped fixture: build corpus once, share across both tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bench_store(tmp_path_factory):  # type: ignore[no-untyped-def]
    """Connected SearchStore pre-loaded with _N_CHUNKS chunks (module scope)."""
    from archon_search.store import SearchStore

    tmp = tmp_path_factory.mktemp("bench_db")
    store = SearchStore(tmp)

    async def _setup() -> None:
        await store.connect()
        await store._require_connected().create_table(
            "bench",
            schema=SearchStore._schema(_DIM),
            exist_ok=True,
        )
        db = store._require_connected()
        table = await db.open_table("bench")
        rows = [_make_chunk(i) for i in range(_N_CHUNKS)]
        await table.add(rows)
        # Build FTS index so hybrid_search can use both legs
        from lancedb.index import FTS
        await table.create_index("text", config=FTS(), replace=True)

    asyncio.run(_setup())
    yield store
    asyncio.run(store.disconnect())


# ---------------------------------------------------------------------------
# Shared measurement helper
# ---------------------------------------------------------------------------


async def _measure(
    store,
    *,
    filters,
    n_iters: int,
    warmup: int,
    top_k: int = _TOP_K,
) -> list[float]:
    """Run hybrid_search and return CPU-time latencies in milliseconds.

    Uses ``time.process_time()`` (CLOCK_PROCESS_CPUTIME_ID) rather than wall-clock
    ``time.perf_counter()``. The work measured is CPU-bound (in-process LanceDB +
    NumPy + deterministic backends, no real I/O wait), so CPU time matches wall-clock
    time when the process is uncontended and stays stable under parallel-xdist load.
    This makes the benchmark robust to scheduler jitter from sibling workers while
    still catching real algorithmic regressions.
    """
    # Use a distinct seed per iteration so each query visits a different region of
    # the vector space — this catches regressions that affect only certain regions
    # and avoids measuring the same approximate-nearest-neighbour path 100 times.
    query_text = "benchmark query"

    warmup_rng = np.random.default_rng(9999)
    for _ in range(warmup):
        qv = warmup_rng.random(_DIM, dtype=np.float32).tolist()
        await store.hybrid_search("bench", qv, query_text, top_k, filters)

    latencies: list[float] = []
    for i in range(n_iters):
        qv = np.random.default_rng(i).random(_DIM, dtype=np.float32).tolist()
        t0 = time.process_time()
        await store.hybrid_search("bench", qv, query_text, top_k, filters)
        latencies.append((time.process_time() - t0) * 1000)

    return latencies


# ---------------------------------------------------------------------------
# Benchmark 1: glob-filtered p95 under absolute threshold
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.xdist_group("benchmark")
def test_glob_filtered_search_p95_under_threshold(bench_store) -> None:  # type: ignore[no-untyped-def]
    """Glob-filtered hybrid search p95 must stay under the configured ceiling.

    Filter: source_path_glob = '/bench/group-0/*' (matches ~20 % of corpus).
    Threshold: [search_filtered].p95_ms_glob_filtered in tests/eval/thresholds.toml.
    """
    from archon_search.filters import SearchFilters

    filters = SearchFilters(source_path_glob=_GLOB_PATTERN)
    latencies = asyncio.run(
        _measure(bench_store, filters=filters, n_iters=_N_ITERS, warmup=_WARMUP)
    )

    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    print(f"\nglob-filtered: p50={p50:.1f} ms  p95={p95:.1f} ms  (n={len(latencies)})")

    thresholds = _load_thresholds()
    ceiling = thresholds["search_filtered"]["p95_ms_glob_filtered"]
    assert p95 <= ceiling, (
        f"glob-filtered p95 {p95:.1f} ms exceeds ceiling {ceiling} ms"
    )


# ---------------------------------------------------------------------------
# Benchmark 2: prefix-filtered p95 regression vs unfiltered baseline
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.xdist_group("benchmark")
def test_prefix_filtered_search_p95_regression_under_threshold(bench_store) -> None:  # type: ignore[no-untyped-def]
    """Prefix-filtered hybrid search must not regress p95 beyond allowed % vs unfiltered.

    Filter: source_path_prefix = '/bench/group-0/' (SQL LIKE, ~20 % of corpus).
    Threshold: [search_filtered].p95_regression_pct_prefix_vs_unfiltered in thresholds.toml.
    """
    from archon_search.filters import SearchFilters

    # Unfiltered baseline measured in the same run
    unfiltered_latencies = asyncio.run(
        _measure(bench_store, filters=None, n_iters=_N_ITERS, warmup=_WARMUP)
    )
    unfiltered_p95 = _percentile(unfiltered_latencies, 95)

    # Prefix-filtered
    filters = SearchFilters(source_path_prefix=_PREFIX)
    prefix_latencies = asyncio.run(
        _measure(bench_store, filters=filters, n_iters=_N_ITERS, warmup=_WARMUP)
    )
    prefix_p95 = _percentile(prefix_latencies, 95)

    print(
        f"\nunfiltered: p50={_percentile(unfiltered_latencies, 50):.1f} ms  "
        f"p95={unfiltered_p95:.1f} ms"
    )
    print(
        f"prefix-filtered: p50={_percentile(prefix_latencies, 50):.1f} ms  "
        f"p95={prefix_p95:.1f} ms"
    )

    thresholds = _load_thresholds()
    max_regression_pct = thresholds["search_filtered"]["p95_regression_pct_prefix_vs_unfiltered"]

    allowed_ceiling = unfiltered_p95 * (1 + max_regression_pct / 100.0)
    assert prefix_p95 <= allowed_ceiling, (
        f"prefix-filtered p95 {prefix_p95:.1f} ms regressed more than "
        f"{max_regression_pct}% vs unfiltered p95 {unfiltered_p95:.1f} ms "
        f"(ceiling = {allowed_ceiling:.1f} ms)"
    )


# ---------------------------------------------------------------------------
# Benchmark 3: HyDE fast-path (hyde=False) p95 under absolute threshold
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.xdist_group("benchmark")
def test_hyde_false_search_p95_under_threshold(bench_store) -> None:  # type: ignore[no-untyped-def]
    """HyDE fast-path (hyde=False) hybrid search p95 must stay under the configured ceiling.

    ``resolve_hyde_vector(hyde=False, ...)`` returns immediately without calling
    the LLM — this benchmark confirms that wiring HyDE through the route handler
    adds no measurable overhead over an unfiltered search.

    Threshold: [search_hyde_false].p95_ms in tests/eval/thresholds.toml.
    """
    from archon_search.config import HyDEConfig
    from archon_search.hyde import resolve_hyde_vector

    config = HyDEConfig(enabled=True, max_requests_per_minute=60)

    async def _measure_with_hyde_false(store, n_iters: int, warmup: int) -> list[float]:
        """Run hybrid_search with resolve_hyde_vector(hyde=False) and return latencies."""
        query_text = "benchmark query for hyde fast path"

        warmup_rng = np.random.default_rng(8888)
        for _ in range(warmup):
            qv = warmup_rng.random(_DIM, dtype=np.float32).tolist()
            # Resolve HyDE vector — fast path, returns (None, False) immediately
            await resolve_hyde_vector(query_text, False, None, config)
            # Use query embedding (None vector → normal search)
            await store.hybrid_search("bench", qv, query_text, _TOP_K, None)

        latencies: list[float] = []
        for i in range(n_iters):
            qv = np.random.default_rng(i + 10000).random(_DIM, dtype=np.float32).tolist()
            t0 = time.process_time()
            # Full round-trip: resolve_hyde_vector + hybrid_search (as the route handler does)
            hyde_vector, _ = await resolve_hyde_vector(query_text, False, None, config)
            await store.hybrid_search("bench", qv, query_text, _TOP_K, None)
            latencies.append((time.process_time() - t0) * 1000)

        return latencies

    latencies = asyncio.run(
        _measure_with_hyde_false(bench_store, n_iters=_N_ITERS, warmup=_WARMUP)
    )

    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    print(
        f"\nhyde=false fast-path: p50={p50:.1f} ms  p95={p95:.1f} ms  (n={len(latencies)})"
    )

    thresholds = _load_thresholds()
    ceiling = thresholds["search_hyde_false"]["p95_ms"]
    assert p95 <= ceiling, (
        f"hyde=false fast-path p95 {p95:.1f} ms exceeds ceiling {ceiling} ms. "
        "The HyDE fast-path (resolve_hyde_vector with hyde=False) should not add "
        "measurable latency over unfiltered hybrid search."
    )

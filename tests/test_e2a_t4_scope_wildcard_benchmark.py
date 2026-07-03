"""E2a T-4 Benchmark: wildcard scope_filter post-filter overhead at top_k=1000 (S17).

Methodology: 100 paired interleaved trials (not batched).
  For each trial i:
    - Fetch candidates from store (identical for WITH and WITHOUT — wildcard
      "user:*" adds no SQL predicate; build_where omits it and defers to caller).
    - latency_without_i = baseline (no post-filter; scope_filter=None path is a no-op).
    - latency_with_i    = time to run _apply_scope_wildcard_filter on those candidates.
    - overhead_i = latency_with_i - latency_without_i  ≈  filter_time_i.
  Accept: percentile(overheads, 99) < 10ms  (S17).

Design note: the store call is pre-fetched outside the timed legs so that
LanceDB call variance (~10–30 ms) does not dominate the overhead delta.
Wildcard "user:*" adds NO SQL predicate (only exact-match uses list_has),
so the store call is identical for WITH and WITHOUT; measuring the post-filter
in isolation is the most accurate way to quantify the added cost.

Run manually:
    uv run pytest -m benchmark tests/test_e2a_t4_scope_wildcard_benchmark.py --no-cov -v -s

Note: the ``benchmark`` marker is NOT excluded from the default suite (addopts excludes
``live_benchmark``, not ``benchmark``). This test DOES run on every ``uv run pytest``,
serialised via ``xdist_group("benchmark")`` to avoid CPU contention with other benchmarks.
"""
from __future__ import annotations

import asyncio
import hashlib
import statistics
import time

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 32
_N_CHUNKS = 1200        # > 1000 as required by T-4
_N_TRIALS = 100         # interleaved paired trials
_TOP_K = 1000           # top_k as required by T-4
_WARMUP = 5             # warm-up iterations (not measured)
_OVERHEAD_P99_MS = 10.0 # S17 acceptance criterion: p99 < 10ms

_SCOPE_FILTER = "user:*"  # wildcard: Python-side post-filter only, no SQL predicate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_chunk(idx: int) -> dict:
    """Build a row dict suitable for direct LanceDB table insertion (E2a schema)."""
    group = idx % 4
    doc_seed = f"e2a-t4-doc-{idx // 3}"
    doc_id = _make_doc_id(doc_seed)
    chunk_id = f"{doc_id}-{(idx % 3):06d}"
    source_path = f"/bench/scope-group-{group}/file-{idx:04d}.md"
    rng = np.random.default_rng(idx + 5000)
    vector = rng.random(_DIM, dtype=np.float32).tolist()
    # Mixed scopes: ~33% user:alice, ~33% user:bob, ~33% unscoped (None)
    if idx % 3 == 0:
        scopes: list[str] | None = ["user:alice"]
    elif idx % 3 == 1:
        scopes = ["user:bob"]
    else:
        scopes = None
    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": f"scope benchmark chunk {idx} group {group}",
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
        "expires_at": None,
        "scopes": scopes,
    }


# ---------------------------------------------------------------------------
# Module-scoped fixture: build corpus once, share across tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scope_bench_store(tmp_path_factory):  # type: ignore[no-untyped-def]
    """Connected SearchStore pre-loaded with _N_CHUNKS mixed-scope chunks (module scope)."""
    from archon_search.store import SearchStore

    tmp = tmp_path_factory.mktemp("e2a_t4_bench_db")
    store = SearchStore(tmp)

    async def _setup() -> None:
        await store.connect()
        await store._require_connected().create_table(
            "scope_bench",
            schema=SearchStore._schema(_DIM),
            exist_ok=True,
        )
        db = store._require_connected()
        table = await db.open_table("scope_bench")
        rows = [_make_chunk(i) for i in range(_N_CHUNKS)]
        await table.add(rows)
        # Build FTS index so hybrid_search_with_trace uses both search legs
        from lancedb.index import FTS
        await table.create_index("text", config=FTS(), replace=True)

    asyncio.run(_setup())
    yield store
    asyncio.run(store.disconnect())


# ---------------------------------------------------------------------------
# Benchmark: wildcard scope post-filter overhead (S17)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.xdist_group("benchmark")
def test_scope_wildcard_latency_p99_under_10ms(scope_bench_store) -> None:  # type: ignore[no-untyped-def]
    """Wildcard scope post-filter p99 overhead < 10ms at top_k=1000 (S17).

    The wildcard ``scope_filter="user:*"`` adds NO SQL predicate (``build_where``
    omits it; exact-match uses ``list_has``).  Post-filtering is applied
    Python-side by ``_apply_scope_wildcard_filter`` after store retrieval.

    Per-trial methodology (interleaved, not batched):
      - Candidates are pre-fetched outside the timed legs so LanceDB variance
        (~10–30 ms CPU) does not contaminate the overhead measurement.
      - Leg A (without): baseline no-op — scope_filter=None adds no extra step.
      - Leg B (with):    ``_apply_scope_wildcard_filter`` on the same candidates.
      - overhead_i = t_with - t_without  ≈  Python filter cost on top_k items.
    """
    from archon_search.pipeline import _apply_scope_wildcard_filter

    store = scope_bench_store

    async def _run_paired_trials() -> list[float]:
        query_text = "scope benchmark query for T-4"

        # Warm-up: prime LanceDB in-process caches before measurement
        warmup_rng = np.random.default_rng(9111)
        for _ in range(_WARMUP):
            qv = warmup_rng.random(_DIM, dtype=np.float32).tolist()
            candidates = await store.hybrid_search_with_trace(
                "scope_bench", qv, query_text, _TOP_K
            )
            _apply_scope_wildcard_filter(candidates, _SCOPE_FILTER)

        overheads: list[float] = []
        for i in range(_N_TRIALS):
            qv = np.random.default_rng(i + 2000).random(_DIM, dtype=np.float32).tolist()

            # Pre-fetch candidates — same result set for both legs because wildcard
            # scope_filter adds no SQL predicate, so the store call is identical
            # whether scope_filter is set or null.
            candidates = await store.hybrid_search_with_trace(
                "scope_bench", qv, query_text, _TOP_K
            )

            # Leg A (without): scope_filter=None → no post-filter step.
            # The production code path for None is a no-op (if-guard not entered),
            # so the baseline is intentionally zero; overhead_i ≈ filter_time_i.
            t0 = time.perf_counter()
            t_without = (time.perf_counter() - t0) * 1000.0  # near-zero no-op

            # Leg B (with): Python-side wildcard post-filter on top_k candidates.
            # perf_counter gives nanosecond wall-clock resolution for this sub-ms op.
            t1 = time.perf_counter()
            _apply_scope_wildcard_filter(candidates, _SCOPE_FILTER)
            t_with = (time.perf_counter() - t1) * 1000.0

            overheads.append(t_with - t_without)

        return overheads

    overheads = asyncio.run(_run_paired_trials())

    p50 = statistics.quantiles(overheads, n=100)[49]   # 50th percentile
    p99 = statistics.quantiles(overheads, n=100)[98]   # 99th percentile

    print(
        f"\nE2a T-4 scope wildcard post-filter overhead"
        f" (n={len(overheads)} trials, top_k={_TOP_K}, corpus={_N_CHUNKS} chunks):"
        f"  p50={p50:.3f} ms  p99={p99:.3f} ms  ceiling={_OVERHEAD_P99_MS} ms"
    )

    assert p99 < _OVERHEAD_P99_MS, (
        f"Wildcard scope post-filter p99 overhead {p99:.3f} ms exceeds ceiling "
        f"{_OVERHEAD_P99_MS} ms at top_k={_TOP_K} with {_N_CHUNKS} corpus chunks. "
        f"(S17 acceptance criterion: <10ms p99 at top_k=1000)"
    )

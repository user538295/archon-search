"""Routing latency benchmark: in-process MultiCollectionRouter vs HTTP POST /route.

Run manually (requires a running archon-search server):
    cd packages/archon-search
    uv run pytest tests/benchmark_routing_latency.py -v -s

Targets: p50 ≤ 30ms, p95 ≤ 150ms over localhost.
If p95 > 150ms, consider co-located embedder mode (Archon embeds locally and passes
the vector to POST /route) and record the decision in Documentation/ADRs/.
"""
from __future__ import annotations

import asyncio
import statistics
import time

import httpx
import pytest

_SERVER_URL = "http://127.0.0.1:8765"
_QUERY = "How do I configure the search embedding model and chunk size?"
_ITERATIONS = 100
_WARMUP = 3


def _percentile(data: list[float], p: int) -> float:
    """Return the p-th percentile of data (1 ≤ p ≤ 99)."""
    return statistics.quantiles(data, n=100)[p - 1]


def _is_server_running() -> bool:
    try:
        r = httpx.get(f"{_SERVER_URL}/health", timeout=2.0)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


def _print_stats(label: str, latencies: list[float]) -> None:
    if not latencies:
        print(f"\n{label}: no successful measurements")
        return
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    print(f"\n{label} ({len(latencies)} iterations):")
    print(f"  p50 = {p50:.1f} ms")
    print(f"  p95 = {p95:.1f} ms")
    print(f"  min = {min(latencies):.1f} ms  max = {max(latencies):.1f} ms")


async def _measure_http(iterations: int, warmup: int) -> list[float]:
    latencies: list[float] = []
    async with httpx.AsyncClient(base_url=_SERVER_URL, timeout=30.0) as client:
        for _ in range(warmup):
            await client.post("/route", json={"query": _QUERY})

        for _ in range(iterations):
            start = time.perf_counter()
            response = await client.post("/route", json={"query": _QUERY})
            elapsed = (time.perf_counter() - start) * 1000
            if response.status_code == 200:
                latencies.append(elapsed)

    return latencies


async def _measure_inprocess(iterations: int, warmup: int) -> list[float]:
    from archon_search.config import load_config
    from archon_search.embedder import Embedder, ModelEmbedder
    from archon_search.router import MultiCollectionRouter

    config = load_config()
    # Reuse the embedder across iterations (matches server-side app.state.embedder caching).
    backend = ModelEmbedder(config.embedding_model, providers=config.providers or None)
    embedder = Embedder(backend)

    def _make_router() -> MultiCollectionRouter:
        # Create a fresh router per call to match server-side per-request router creation.
        return MultiCollectionRouter(
            search_url=f"http://{config.host}:{config.port}",
            embedder=embedder,
            shortlist_size=config.routing_shortlist_size,
            confidence_threshold=config.routing_confidence_threshold,
            embedding_model=config.embedding_model,
        )

    pinned: list[str] = []

    for _ in range(warmup):
        await _make_router().get_pre_context(_QUERY, pinned, config.routing_shortlist_size)

    latencies: list[float] = []
    for _ in range(iterations):
        router = _make_router()
        start = time.perf_counter()
        await router.get_pre_context(_QUERY, pinned, config.routing_shortlist_size)
        latencies.append((time.perf_counter() - start) * 1000)

    return latencies


@pytest.mark.benchmark
def test_routing_latency_harness_runs() -> None:
    """Benchmark routing latency: in-process MultiCollectionRouter vs HTTP POST /route.

    Requires a running archon-search server at http://127.0.0.1:8765.
    Auto-skipped in CI when the server is not reachable.

    Record p50/p95 results in the PR description or Documentation/ADRs/ before merge.
    If p95 > 150ms, record co-located embedder decision in Key Decisions section of
    Documentation/Backlog/FEAT-038-search-product-separation.md.
    """
    if not _is_server_running():
        pytest.skip(
            "archon-search server not running — "
            "start with 'archon-search start' before running this benchmark"
        )

    http_latencies = asyncio.run(_measure_http(_ITERATIONS, _WARMUP))
    inprocess_latencies = asyncio.run(_measure_inprocess(_ITERATIONS, _WARMUP))

    print()
    _print_stats("HTTP POST /route", http_latencies)
    _print_stats("In-process MultiCollectionRouter (fresh router, metadata refetched)", inprocess_latencies)

    if http_latencies and inprocess_latencies:
        http_p95 = _percentile(http_latencies, 95)
        ip_p95 = _percentile(inprocess_latencies, 95)
        overhead = http_p95 - ip_p95
        print(f"\nHTTP transport overhead (p95): {overhead:.1f} ms")

        if http_p95 > 150:
            print(
                "\n⚠  p95 > 150 ms threshold exceeded.\n"
                "   Consider co-located embedder mode: Archon embeds the query locally\n"
                "   and passes the vector directly to POST /route, avoiding the\n"
                "   embedding round-trip inside the server.\n"
                "   Record this decision in Documentation/ADRs/."
            )
        else:
            print(f"\n✓  p95 = {http_p95:.1f} ms — within the 150 ms target. HTTP boundary is acceptable.")

    assert len(http_latencies) >= int(_ITERATIONS * 0.9), (
        f"Too many HTTP failures: only {len(http_latencies)}/{_ITERATIONS} succeeded"
    )

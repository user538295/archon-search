"""Routing latency benchmark: HTTP POST /route against a running server.

Run manually (requires a running archon-search server and its API key):
    export ARCHON_SEARCH_API_KEY=<the running server's key>
    uv run pytest tests/benchmark_routing_latency.py -n 0 -v -s

``-n 0`` is required: tests/conftest.py overwrites ARCHON_SEARCH_API_KEY with the suite's test key
after recording the exported one, and xdist workers inherit the overwritten value, so under the
default ``-n 8`` every request is a 401 and the run fails with "0/100 succeeded".

Targets: p50 ≤ 30ms, p95 ≤ 150ms over localhost.
If p95 > 150ms, consider co-located embedder mode (the host application embeds locally and passes
the vector to POST /route) and record the decision in Documentation/ADRs/.

There is deliberately no in-process comparison: ``MultiCollectionRouter`` only has collection
metadata when the server seeds it from its LanceDB store (one process owns that store), and its
own metadata fetch POSTs unauthenticated JSON-RPC to the server root, which answers 401 (no key)
or 404 (key), so an in-process run measured an empty router and reported a meaningless overhead.
"""
from __future__ import annotations

import asyncio
import statistics
import time

import httpx
import pytest

from tests.conftest import ORIGINAL_ARCHON_SEARCH_API_KEY as SERVER_API_KEY

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
    headers = {"Authorization": f"Bearer {SERVER_API_KEY}"}
    async with httpx.AsyncClient(base_url=_SERVER_URL, headers=headers, timeout=30.0) as client:
        for _ in range(warmup):
            await client.post("/route", json={"query": _QUERY})

        for _ in range(iterations):
            start = time.perf_counter()
            response = await client.post("/route", json={"query": _QUERY})
            elapsed = (time.perf_counter() - start) * 1000
            if response.status_code == 200:
                latencies.append(elapsed)

    return latencies


@pytest.mark.benchmark
@pytest.mark.xdist_group("benchmark")
def test_routing_latency_harness_runs() -> None:
    """Benchmark routing latency: HTTP POST /route against a running server.

    Requires a running archon-search server at http://127.0.0.1:8765.
    Auto-skipped in CI when the server is not reachable.

    Record p50/p95 results in the PR description or Documentation/ADRs/ before merge.
    If p95 > 150ms, record co-located embedder decision in Key Decisions section of
    Documentation/Backlog/.md.
    """
    if not _is_server_running():
        pytest.skip(
            "archon-search server not running — "
            "start with 'archon-search start' before running this benchmark"
        )
    if SERVER_API_KEY is None:
        pytest.skip(
            "ARCHON_SEARCH_API_KEY is not exported — POST /route needs the running server's API key "
            "(export ARCHON_SEARCH_API_KEY=<key> before running this benchmark)"
        )

    http_latencies = asyncio.run(_measure_http(_ITERATIONS, _WARMUP))

    print()
    _print_stats("HTTP POST /route", http_latencies)

    if http_latencies:
        http_p95 = _percentile(http_latencies, 95)
        if http_p95 > 150:
            print(
                "\n⚠  p95 > 150 ms threshold exceeded.\n"
                " Consider co-located embedder mode: the host application embeds the query locally\n"
                "   and passes the vector directly to POST /route, avoiding the\n"
                "   embedding round-trip inside the server.\n"
                "   Record this decision in Documentation/ADRs/."
            )
        else:
            print(f"\n✓  p95 = {http_p95:.1f} ms — within the 150 ms target. HTTP boundary is acceptable.")

    assert len(http_latencies) >= int(_ITERATIONS * 0.9), (
        f"Too many HTTP failures: only {len(http_latencies)}/{_ITERATIONS} succeeded"
    )

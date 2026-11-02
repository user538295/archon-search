"""A cancelled HTTP request leaves nothing unclosed.

anyio ``connect_tcp`` (under httpx and httpcore) leaked the connected socket when the enclosing task was cancelled
while its connection-attempt task group was still finishing (anyio#1364, fixed in 4.16.0). The server cancels its
background HTTP calls (the description reachability probe, the context-window lookup, a generation) at shutdown, so
the leak surfaced as an ``unclosed transport`` ``ResourceWarning`` that pytest reports as an unraisable exception in
whichever test the garbage collector ran in: the intermittent whole-suite failure of the description wiring tests.

The leak needs the cancel to land in a window of a few microseconds, so the test cancels many requests at random
moments; the seed fixes the delays, not the event-loop timing, which makes the test probabilistic. On anyio 4.15.1 it
leaked roughly one request in two hundred, so 1500 requests miss the leak with a probability of about 0.05 percent.
The run takes about ten seconds. The test drives ``httpx``; ``httpx2`` (``openai``, ``anthropic``) connects through the
same anyio ``connect_tcp``, and the floor tests in ``tests/test_pyproject.py`` cover every client alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import random
import warnings

import httpx
import pytest

from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies

_CANCELLED_REQUESTS = 1500
_LONGEST_WAIT_BEFORE_CANCEL_SECONDS = 0.006  # the request is mid-connect or mid-response for a few milliseconds
_RANDOM_SEED = 20261102
_REQUEST_TIMEOUT_SECONDS = 5.0


async def _cancel_one_request(url: str, wait_seconds: float) -> None:
    async def get() -> None:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            await client.get(url)

    task = asyncio.create_task(get())
    await asyncio.sleep(wait_seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):  # any other error is a failed request, not a cancelled one
        await task


async def _cancel_many_requests(url: str) -> None:
    waits = random.Random(_RANDOM_SEED)
    for _ in range(_CANCELLED_REQUESTS):
        await _cancel_one_request(url, waits.uniform(0, _LONGEST_WAIT_BEFORE_CANCEL_SECONDS))


def test_a_cancelled_request_leaves_nothing_unclosed(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    gc.collect()  # a leak of an earlier test on this worker is reported as that test's, not counted as this one's
    with FakeModelServer() as server, warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", ResourceWarning)
        asyncio.run(_cancel_many_requests(f"{server.base_url}/api/tags"))
        gc.collect()

    leaks = [str(warning.message) for warning in recorded if issubclass(warning.category, ResourceWarning)]
    assert leaks == []

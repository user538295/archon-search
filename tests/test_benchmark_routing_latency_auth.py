"""The routing latency benchmark must authenticate: ``POST /route`` answers 401 without the server's key."""

from __future__ import annotations

import httpx
import pytest

from tests import benchmark_routing_latency

_SERVER_KEY = "k" * 64
_ITERATIONS = 3
_WARMUP = 1


def _route_stub(seen_authorization: list[str | None]) -> httpx.MockTransport:
    """A server that answers ``/route`` 200 only to ``Bearer _SERVER_KEY`` and 401 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization")
        seen_authorization.append(authorization)
        return httpx.Response(200 if authorization == f"Bearer {_SERVER_KEY}" else 401, json={})

    return httpx.MockTransport(handler)


@pytest.fixture
def seen_authorization(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    seen: list[str | None] = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        benchmark_routing_latency.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=_route_stub(seen), **kwargs),
    )
    monkeypatch.setattr(benchmark_routing_latency, "SERVER_API_KEY", _SERVER_KEY)
    return seen


async def test_http_measurement_sends_the_server_key_and_counts_every_answer(
    seen_authorization: list[str | None],
) -> None:
    latencies = await benchmark_routing_latency._measure_http(_ITERATIONS, _WARMUP)

    assert len(latencies) == _ITERATIONS
    assert set(seen_authorization) == {f"Bearer {_SERVER_KEY}"}


def test_benchmark_skips_without_the_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(benchmark_routing_latency, "SERVER_API_KEY", None)
    monkeypatch.setattr(benchmark_routing_latency, "_is_server_running", lambda: True)

    with pytest.raises(pytest.skip.Exception, match="ARCHON_SEARCH_API_KEY"):
        benchmark_routing_latency.test_routing_latency_harness_runs()


def test_harness_reports_only_the_http_route_measurement(
    seen_authorization: list[str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The former in-process half POSTed JSON-RPC to the server root (401 unauthenticated, 404
    authenticated), so it always measured nothing; the harness must not report it."""
    monkeypatch.setattr(benchmark_routing_latency, "_is_server_running", lambda: True)

    benchmark_routing_latency.test_routing_latency_harness_runs()

    output = capsys.readouterr().out
    assert "HTTP POST /route" in output
    assert "In-process" not in output
    assert "transport overhead" not in output
    assert not hasattr(benchmark_routing_latency, "_measure_inprocess")

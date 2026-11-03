"""A client that goes away stops the provider call its request started, over REST and over MCP (S12, S79).

The real app (real lifespan, real LanceDB) runs behind a real uvicorn socket; the HyDE or RAG Fusion model is the
real-subprocess fake ``claude`` of ``tests/_fake_claude.py`` held mid-call. The test aborts the client in the ways a real
client does and waits for the child process to be gone. The 30 s search budget is raised for these tests: it would end a
held call by itself, and a missing cancel would only look slow instead of wrong.

* REST ``POST /search`` and ``POST /explain``: the socket is closed (``CancelOnDisconnectMiddleware``).
* MCP ``tools/call`` of ``search``, ``search_with_context`` and ``explain``: the SSE stream is closed without a cancel
  notification (``CancelMcpToolOnDisconnectMiddleware``: the MCP library does not treat a closed stream as a cancel; red
  before the middleware existed), or the client sends ``notifications/cancelled`` (the MCP library cancels the tool
  itself; pinned so a library change shows), or a real ``fastmcp`` client abandons its call (the library client sends
  that notification: the child dies with the session still open, whether or not the middleware is there).
* A search that completes is answered untouched. That the tools that write (``ingest_*``, ``delete_*``) are never
  cancelled by a disconnect is pinned at unit level (``tests/server/test_mcp_cancel_on_disconnect.py``).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests import _fake_claude
from tests._fake_model_server import keep_loopback_out_of_proxies
from tests.integration.conftest import serve_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "disconnect-e2e"
_HANG_SECONDS = 600.0
_BUDGET_SECONDS = 3600.0  # the 30 s production search budget would end a held call by itself: only a cancel is early
_DEADLINE_SECONDS = 60.0
_POLL_SECONDS = 0.02
_HTTP_TIMEOUT_SECONDS = 30.0
_QUERY = "hypothetical document embedding"
_CLAUDE_TOML = (
    "[hyde]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\ntimeout_seconds = 300\n"
    "[rag_fusion]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\ntimeout_seconds = 300\n"
)
_REQUEST_ID = 7
_REQUEST_CANCELLED_CODE = -32800
_MCP_PROTOCOL = "2025-03-26"

#: ``(tool, feature flag)``: every search-type MCP tool, and RAG Fusion's own client through ``search``.
_SEARCH_TOOLS = [
    pytest.param("search", "hyde", id="search-hyde"),
    pytest.param("search", "rag_fusion", id="search-rag-fusion"),
    pytest.param("search_with_context", "hyde", id="search_with_context-hyde"),
    pytest.param("explain", "hyde", id="explain-hyde"),
]


def _await(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not predicate():
        assert time.monotonic() < deadline, f"timed out after {_DEADLINE_SECONDS} s waiting for {what}"
        time.sleep(_POLL_SECONDS)


def _is_gone(pid: int) -> bool:
    """Whether no process of ours has *pid* any more (a process of another user that took the number over is not it)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return True
    return False


def _is_this_fake(pid: int, fake: _fake_claude.FakeClaude) -> bool:
    """Whether *pid* still is the fake binary of *fake* (its command line names the launcher directory), not a recycled number."""
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
    return str(fake.path.parent) in command


@pytest.fixture(autouse=True)
def _search_budget_never_ends_a_held_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this a held call ends at the 30 s search budget and a missing cancel would only look slow, not wrong."""
    for module in (
        "archon_search.server.mcp",
        "archon_search.server.routes_search",
        "archon_search.server.routes_explain",
        "archon_search.services.search_service",
    ):
        monkeypatch.setattr(f"{module}._SEARCH_TIMEOUT_SECONDS", _BUDGET_SECONDS)


@pytest.fixture
def held_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_fake_claude.FakeClaude]:
    """The fake ``claude`` first on ``PATH``, hanging mid-call; any child the test left alive is killed afterwards."""
    keep_loopback_out_of_proxies(monkeypatch)
    fake = _fake_claude.install(tmp_path / "bin", hang_seconds=_HANG_SECONDS)
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    yield fake
    for invocation in fake.invocations():
        if not _is_gone(invocation.pid) and _is_this_fake(invocation.pid, fake):
            os.kill(invocation.pid, signal.SIGKILL)


def _held_child_pid(fake: _fake_claude.FakeClaude) -> int:
    """Wait until the request has reached the provider (the child recorded itself) and return its process id."""
    _await(lambda: len(fake.invocations()) == 1, "the request to reach the claude child")
    (invocation,) = fake.invocations()
    assert not _is_gone(invocation.pid), "the child must be held mid-call"
    return invocation.pid


def _assert_child_dies(pid: int, why: str) -> None:
    _await(lambda: _is_gone(pid), f"the claude child to die after {why}")


@contextmanager
def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[httpx.Client, dict[str, str], str]]:
    """The real app with the claude_cli providers and MCP on a real socket, one corpus ingested: ``(client, auth, base)``."""
    with serve_real_app(tmp_path, monkeypatch, toml_content=_CLAUDE_TOML, mcp_enabled=True) as (base_url, _config, api_key):
        auth = {"Authorization": f"Bearer {api_key}"}
        document = tmp_path / "corpus.md"
        document.write_text("# Disconnect\n\nHypothetical document embedding via the Claude CLI.\n" * 4, encoding="utf-8")
        with httpx.Client(base_url=base_url, timeout=_HTTP_TIMEOUT_SECONDS) as client:
            job = client.post("/ingest", json={"collection": _COLLECTION, "path": str(document)}, headers=auth).json()["job_id"]
            _await(lambda: client.get(f"/jobs/{job}", headers=auth).json()["status"] == "DONE", "the ingest job")
            yield client, auth, base_url


# --- REST -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/search", "/explain"])
def test_closing_the_socket_of_a_rest_request_kills_the_claude_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_claude: _fake_claude.FakeClaude, path: str
) -> None:
    with _served(tmp_path, monkeypatch) as (_client, auth, base_url):
        body = json.dumps({"collection": _COLLECTION, "query": _QUERY, "hyde": True}).encode()
        host, port = base_url.removeprefix("http://").split(":")
        headers = f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        headers += f"Authorization: {auth['Authorization']}\r\n\r\n"
        connection = socket.create_connection((host, int(port)))
        connection.sendall(headers.encode() + body)
        pid = _held_child_pid(held_claude)

        connection.close()

        _assert_child_dies(pid, "the REST client closed its socket")


# --- MCP --------------------------------------------------------------------------------------------------------------


def _mcp_headers(api_key_header: dict[str, str], session: str | None = None) -> dict[str, str]:
    headers = {**api_key_header, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session is not None:
        headers["mcp-session-id"] = session
        headers["mcp-protocol-version"] = _MCP_PROTOCOL
    return headers


def _mcp_session(client: httpx.Client, auth: dict[str, str]) -> str:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": _MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "disconnect-e2e", "version": "1"}},
    }
    answer = client.post("/mcp/", json=initialize, headers=_mcp_headers(auth))
    assert answer.status_code == 200, answer.text
    session = answer.headers["mcp-session-id"]
    client.post("/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=_mcp_headers(auth, session))
    return session


def _tool_call(tool: str, feature: str | None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"query": _QUERY, "collection": _COLLECTION}
    if feature is not None:
        arguments[feature] = True
    return {"jsonrpc": "2.0", "id": _REQUEST_ID, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


@pytest.mark.parametrize(("tool", "feature"), _SEARCH_TOOLS)
def test_closing_the_stream_of_an_mcp_search_call_kills_the_claude_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_claude: _fake_claude.FakeClaude, tool: str, feature: str
) -> None:
    with _served(tmp_path, monkeypatch) as (client, auth, base_url):
        session = _mcp_session(client, auth)
        request = {"json": _tool_call(tool, feature), "headers": _mcp_headers(auth, session)}
        with httpx.Client(base_url=base_url, timeout=_HTTP_TIMEOUT_SECONDS) as caller, caller.stream("POST", "/mcp/", **request) as response:
            assert response.status_code == 200
            pid = _held_child_pid(held_claude)
        # Both blocks ended: the response and its connection are closed, and no cancel notification was ever sent.

        _assert_child_dies(pid, "the MCP client closed the stream of its tools/call")


@pytest.mark.parametrize(("tool", "feature"), _SEARCH_TOOLS)
def test_an_mcp_cancel_notification_kills_the_claude_child_and_answers_the_request_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_claude: _fake_claude.FakeClaude, tool: str, feature: str
) -> None:
    with _served(tmp_path, monkeypatch) as (client, auth, base_url):
        session = _mcp_session(client, auth)
        request = {"json": _tool_call(tool, feature), "headers": _mcp_headers(auth, session)}
        with httpx.Client(base_url=base_url, timeout=_HTTP_TIMEOUT_SECONDS) as caller, caller.stream("POST", "/mcp/", **request) as response:
            pid = _held_child_pid(held_claude)
            cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": _REQUEST_ID, "reason": "e2e"}}
            accepted = client.post("/mcp/", json=cancel, headers=_mcp_headers(auth, session))
            assert accepted.status_code == 202

            _assert_child_dies(pid, "the MCP client sent notifications/cancelled")
            answer = "".join(response.iter_text())

    assert f'"id":{_REQUEST_ID}' in answer.replace(" ", "")
    assert str(_REQUEST_CANCELLED_CODE) in answer


# --- what must not be cancelled -----------------------------------------------------------------------------------------


def _tool_payload(sse_text: str) -> dict[str, Any]:
    """The JSON a tool call answered with: the last SSE ``data:`` event is the JSON-RPC response, its text the payload."""
    events = [line.removeprefix("data:").strip() for line in sse_text.splitlines() if line.startswith("data:")]
    result = json.loads(events[-1])["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_an_mcp_search_that_completes_is_answered_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fast fake ``claude`` answers HyDE: the call returns its result, the server keeps serving, nothing was cancelled."""
    keep_loopback_out_of_proxies(monkeypatch)
    fast = _fake_claude.install(tmp_path / "fast-bin", reply="A hypothetical answer passage.")
    monkeypatch.setenv("PATH", f"{fast.path.parent}{os.pathsep}{os.environ['PATH']}")
    with _served(tmp_path, monkeypatch) as (client, auth, base_url):
        session = _mcp_session(client, auth)
        request = {"json": _tool_call("search", "hyde"), "headers": _mcp_headers(auth, session)}
        with httpx.Client(base_url=base_url, timeout=_HTTP_TIMEOUT_SECONDS) as caller, caller.stream("POST", "/mcp/", **request) as response:
            payload = _tool_payload("".join(response.iter_text()))

        assert payload["hyde_applied"] is True
        assert client.post("/search", json={"collection": _COLLECTION, "query": _QUERY}, headers=auth).status_code == 200


# --- a real MCP client library ------------------------------------------------------------------------------------------


async def test_aborting_a_call_of_the_real_mcp_client_library_kills_the_claude_child_while_its_session_stays_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_claude: _fake_claude.FakeClaude
) -> None:
    """What a ``fastmcp`` client does when the caller abandons a call; the session is still open when the child dies."""
    import asyncio

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    with _served(tmp_path, monkeypatch) as (_client, auth, base_url):
        async with Client(StreamableHttpTransport(f"{base_url}/mcp/", headers=auth)) as mcp:
            call = asyncio.create_task(mcp.call_tool("search", {"query": _QUERY, "collection": _COLLECTION, "hyde": True}))
            pid = await asyncio.to_thread(_held_child_pid, held_claude)

            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

            await asyncio.to_thread(_assert_child_dies, pid, "the MCP client library aborted its call")

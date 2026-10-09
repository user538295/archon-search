"""Streamable-HTTP transport behaviour of the production MCP app (create_mcp_http_app)."""
from __future__ import annotations

import gc
import warnings
from collections.abc import Callable

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from tests.server.test_mcp_auth import _MCP_HEADERS, _MCP_INIT, VALID_KEY, _make_starlette_client

pytestmark = pytest.mark.xdist_group("mcp")

_SSE_POST_COUNT = 3


def _unclosed_anyio_streams(action: Callable[[], None]) -> list[str]:
    """Run ``action`` and return the warnings of anyio memory streams it left unclosed.

    anyio warns ``ResourceWarning(source=<the stream>)`` when an unclosed stream is freed.
    """
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", ResourceWarning)
        action()
        gc.collect()
    return [
        str(w.message)
        for w in recorded
        if isinstance(w.source, (MemoryObjectReceiveStream, MemoryObjectSendStream))
    ]


def test_sse_post_closes_its_response_stream() -> None:
    """Every SSE-answered MCP POST closes the anyio stream that fed its response.

    A stream left open is only reclaimed by the garbage collector, which emits
    ``ResourceWarning: Unclosed <MemoryObjectReceiveStream ...>`` once per request
    of a long-running ``archon-search serve`` process.
    """
    client = _make_starlette_client()

    def drop_unclosed_receive_stream() -> None:
        send, _receive = anyio.create_memory_object_stream[object](0)
        send.close()

    def post_sse() -> None:
        with client:
            for _ in range(_SSE_POST_COUNT):
                resp = client.post(
                    "/",
                    json=_MCP_INIT,
                    headers={**_MCP_HEADERS, "Authorization": f"Bearer {VALID_KEY}"},
                )
                assert resp.status_code == 200, resp.text
                assert resp.headers["content-type"].startswith("text/event-stream")

    gc.collect()  # flush garbage left by earlier tests in this worker
    canary = _unclosed_anyio_streams(drop_unclosed_receive_stream)
    assert len(canary) == 1, f"detector must see exactly one deliberately leaked stream: {canary}"

    leaked = _unclosed_anyio_streams(post_sse)
    assert not leaked, (
        f"{len(leaked)} anyio memory stream(s) left unclosed after {_SSE_POST_COUNT} SSE MCP "
        f"POSTs and garbage-collected with a ResourceWarning: {leaked}"
    )

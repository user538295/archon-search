"""Stop the work of a request whose client went away (T-2: a ``claude`` child outlived an aborted search).

An ASGI server does not cancel a route that is running when its client closes the connection, so a search whose caller gave
up kept its HyDE, RAG Fusion and graph calls running to their natural end: a real ``claude`` child kept spending the login's
quota for an answer nobody reads. :class:`CancelOnDisconnectMiddleware` runs the application of the configured routes as a
task; once the application has read the whole request body it also reads the connection, and a disconnect before the response
is complete cancels the task and waits for its cleanup (the ``claude`` wrapper kills and reaps its child on cancellation).

Pure ASGI and the outermost middleware the application adds on purpose: ``request.is_disconnected()`` inside a route never sees a
disconnect behind Starlette's ``BaseHTTPMiddleware`` (the authentication middleware is one), only the server's own ``receive``
does. The middleware reads nothing ahead of the application: the application's ``receive`` goes straight to the server, so the
server's flow control and its ``100 Continue`` (sent on the first read, after the body-size middleware had its say) behave as
without it. A route that never reads its body is never watched; ``POST /search`` and ``POST /explain`` declare body models, so
FastAPI reads it first. Limits: the paths match exactly (a trailing slash is FastAPI's redirect, and the application sets no
``root_path``), and uvicorn marks only the newest of two pipelined requests on one connection as disconnected, so a client
that pipelines a second request behind a search it then abandons is not detected for the first. The MCP tools are covered by
:mod:`archon_search.server.mcp_disconnect`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_DISCONNECT_TYPE: Final = "http.disconnect"


class CancelOnDisconnectMiddleware:
    """Cancels the application's work for ``method`` requests to *paths* once the client disconnects; others pass untouched."""

    def __init__(self, app: ASGIApp, *, method: str, paths: Collection[str]) -> None:
        self._app = app
        self._method = method
        self._paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != self._method or scope["path"] not in self._paths:
            await self._app(scope, receive, send)
            return

        body_read = asyncio.Event()
        response_complete = False

        async def tracking_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_read.set()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_complete
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True

        async def watch_connection() -> None:
            await body_read.wait()  # until then the application is the only reader
            while (await receive())["type"] != _DISCONNECT_TYPE:
                pass

        app_task = asyncio.ensure_future(self._app(scope, tracking_receive, tracking_send))
        watcher = asyncio.ensure_future(watch_connection())
        try:
            await asyncio.wait({app_task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if not app_task.done():
                if watcher.cancelled() or watcher.exception() is not None:
                    await _cancel_and_wait(app_task)
                    watcher.result()
                elif response_complete:
                    pass  # the server reports a disconnect once the response is out: the application is only finishing up
                else:
                    logger.info("client disconnected from %s %s: cancelling the request", self._method, scope["path"])
                    await _cancel_and_wait(app_task)
                    return
            await app_task
        except asyncio.CancelledError:
            await _cancel_and_wait(app_task)
            raise
        finally:
            await _cancel_and_wait(watcher)


async def _cancel_and_wait(task: asyncio.Future[Any]) -> None:
    task.cancel()
    for outcome in await asyncio.gather(task, return_exceptions=True):
        if isinstance(outcome, Exception):
            logger.warning("request work raised %s while it was cancelled", type(outcome).__name__)

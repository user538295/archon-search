"""Serve an ASGI app on a real loopback socket with a real uvicorn server, for tests that must close a real connection.

``TestClient`` is an in-process transport: a client abort cannot be reproduced through it, because no socket ever closes.
:func:`serving` runs ``uvicorn.Server`` (lifespan included) on a thread and yields the port; the test talks to it with
``httpx`` or a raw socket from its own thread. The server never installs signal handlers or touches the logging
configuration, so it cannot leak state into the xdist worker that runs it.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from starlette.types import ASGIApp

LOOPBACK = "127.0.0.1"

#: How long the server may take to finish its lifespan startup before the test fails. Generous: a loaded CI worker is slow.
STARTUP_SECONDS = 60.0

#: How long a clean shutdown may take; open connections are given up after the same time (``timeout_graceful_shutdown``).
SHUTDOWN_SECONDS = 30.0

_STARTUP_POLL_SECONDS = 0.01


@contextmanager
def serving(app: ASGIApp) -> Iterator[int]:
    """Run *app* on ``127.0.0.1`` on a free port until the block ends; yield the port."""
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    config = uvicorn.Config(app, log_config=None, log_level="warning", timeout_graceful_shutdown=int(SHUTDOWN_SECONDS))
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + STARTUP_SECONDS
        while not server.started:
            assert thread.is_alive(), "uvicorn stopped before it finished starting"
            assert time.monotonic() < deadline, f"uvicorn did not start within {STARTUP_SECONDS} s"
            time.sleep(_STARTUP_POLL_SECONDS)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(SHUTDOWN_SECONDS + STARTUP_SECONDS)
        listener.close()
        assert not thread.is_alive(), "uvicorn did not shut down"

"""Serve a ``socketserver`` server on a daemon thread for the length of a ``with`` block."""

from __future__ import annotations

import contextlib
import socketserver
import threading
from collections.abc import Iterator

#: How soon ``shutdown()`` is noticed. ``serve_forever``'s default of 0.5 s would be paid by every exit.
POLL_INTERVAL_SECONDS = 0.01


@contextlib.contextmanager
def serving_in_thread(server: socketserver.BaseServer, *, name: str | None = None) -> Iterator[None]:
    """Run ``server.serve_forever`` on a daemon thread; on exit shut the server down, close it and join the thread."""
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": POLL_INTERVAL_SECONDS}, name=name, daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

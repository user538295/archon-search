"""An application error after the body cap was hit is answered with the 413, but must not vanish from the log."""
from __future__ import annotations

import logging

import pytest

from archon_search.server.middleware_body_limit import (
    REQUEST_TOO_LARGE_STATUS,
    BodySizeLimitMiddleware,
)

_LIMIT = 4


async def test_an_app_error_after_the_cap_was_hit_is_logged_and_answered_with_413(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(scope, receive, send):
        await receive()
        await receive()  # the cap is exceeded here: the app sees a disconnect
        raise RuntimeError("handler blew up")

    chunks = [{"type": "http.request", "body": b"x" * (_LIMIT + 1), "more_body": False}]
    sent: list[dict] = []

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    with caplog.at_level(logging.WARNING, logger="archon_search.server.middleware_body_limit"):
        await BodySizeLimitMiddleware(app, max_bytes=_LIMIT)({"type": "http", "headers": []}, receive, send)

    assert sent[0]["status"] == REQUEST_TOO_LARGE_STATUS
    record = next(r for r in caplog.records if "RuntimeError" in r.getMessage())
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None

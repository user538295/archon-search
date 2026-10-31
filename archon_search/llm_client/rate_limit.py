"""FixedWindowRateLimiter — the graph rate limit of the shared LLM client layer (contract C1, scenario S64).

Moved here from the limiter inside the Anthropic enrichment client (``enrichment/anthropic.py``) with the same window rules:

* N requests per fixed 60 s window, counted per limiter; the window opens when the limiter is built and a new one opens
  at the first call after the old one has run its whole period, so a burst is possible at a window boundary.
* An exhausted limiter never waits: it raises :class:`LlmCallFailure` at once and the caller applies its own fallback
  (the community builder keeps the community without a summary). The failure is an ordinary one, not a timeout.
* The count is taken before the call is made, so a call that then fails still used its slot, and a transport retry inside
  one call is one slot.

One limiter belongs to one graph client; the ``anthropic`` and ``claude_cli`` wrappers apply it when the graph composition
hands them one. HyDE, RAG Fusion, description and the other graph providers are built without one and are unthrottled.

The state is guarded by a ``threading.Lock`` around a section that never awaits, so a limiter is safe from several threads
and needs no event loop of its own (an ``asyncio.Lock`` binds to the first loop that contends for it).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Final

from archon_search.llm_client.protocol import LlmCallFailure

#: Length of one rate-limit window; ``extraction_rate_limit_rpm`` is per this period.
RATE_LIMIT_WINDOW_SECONDS: Final = 60.0


class FixedWindowRateLimiter:
    """At most ``capacity`` acquisitions per ``RATE_LIMIT_WINDOW_SECONDS`` window."""

    def __init__(self, capacity: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Args:
            capacity: requests allowed per window (``extraction_rate_limit_rpm``, which the config loader holds at 1 or more).
            clock: monotonic seconds; replaced in tests so no test sleeps.
        """
        self._capacity = capacity
        self._clock = clock
        self._lock = threading.Lock()
        self._remaining = capacity
        self._window_ends_at = clock() + RATE_LIMIT_WINDOW_SECONDS

    def acquire(self, provider: str) -> None:
        """Take one slot of the current window.

        Args:
            provider: the wrapper's provider name, carried by the failure.

        Raises:
            LlmCallFailure: every slot of the current window is taken; retry later.
        """
        with self._lock:
            now = self._clock()
            if now >= self._window_ends_at:
                self._remaining = self._capacity
                self._window_ends_at = now + RATE_LIMIT_WINDOW_SECONDS
            if self._remaining <= 0:
                raise LlmCallFailure(provider, f"rate limit exhausted ({self._capacity} rpm); retry later")
            self._remaining -= 1

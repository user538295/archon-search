"""Single-flight instrumentation for ``ProseExtractionBackend.load()`` tests.

Not an engine stub (those live in ``tests/_graph_engine_stub.py``): these
helpers drive the real backend's concurrent load path deterministically.
``gated_failing_from_pretrained`` blocks the load until released, and
``LoadSingleFlightProbe`` proves which callers took the LOADER vs. the WAITER
path (S612, S46).
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from archon_search.prose_extraction_backend import ProseExtractionBackend

# Upper bound on how long a gated load's thread blocks before failing anyway,
# so a test that never releases it (e.g. after its own assertion failed) can
# never hang the suite.
_GATED_LOAD_RELEASE_TIMEOUT_SECONDS = 5.0
# Upper bound on event-loop yields while waiting for concurrent callers to
# reach the loader/waiter path — each caller needs only a handful.
_MAX_SCHEDULER_YIELDS = 1000
_BACKEND_ASYNCIO = "archon_search.prose_extraction_backend.asyncio"
# The loader's thread entry point and the waiter's awaitable, named from the
# real objects so a rename on either side breaks loudly here.
_LOADER_FUNC_NAME = ProseExtractionBackend._load_sync.__name__
_WAITER_AWAITABLE_QUALNAME = asyncio.Event.wait.__qualname__


def gated_failing_from_pretrained(
    release: threading.Event, error_factory: Callable[[], BaseException]
) -> MagicMock:
    """A ``GLiNER.from_pretrained`` mock (for ``fake_torch_and_gliner``) that
    blocks its loader thread until ``release`` is set — bounded, so it can
    never hang — then raises a fresh ``error_factory()``."""

    def _from_pretrained(*_args: object, **_kwargs: object) -> object:
        release.wait(timeout=_GATED_LOAD_RELEASE_TIMEOUT_SECONDS)
        raise error_factory()

    return MagicMock(side_effect=_from_pretrained)


class LoadSingleFlightProbe:
    """Counts the real ``ProseExtractionBackend.load()``'s LOADER dispatches
    (``asyncio.to_thread(self._load_sync)``) and WAITER registrations
    (``asyncio.wait_for(event.wait(), ...)`` on the in-flight load) while
    ``tracking()`` is active — the backend's own control flow, unmodified.

    Two scope limits, stated plainly:

    * ``tracking()`` patches ``to_thread``/``wait_for`` through the backend's
      ``asyncio`` name, which IS the process-global ``asyncio`` module — so for
      the context's duration every ``asyncio.to_thread``/``asyncio.wait_for``
      call in the process goes through the counting wrappers (they delegate
      unchanged; only matching calls are counted).
    * A WAITER is detected by its awaitable's ``Event.wait`` qualname, so ANY
      ``asyncio.wait_for(<Event>.wait(), ...)`` counts. That is correct only
      while ``ProseExtractionBackend.load()`` has exactly one such site — its
      waiter path; a second one would be miscounted as a waiter."""

    def __init__(self) -> None:
        self.loader_dispatches = 0
        self.waiter_registrations = 0

    @contextmanager
    def tracking(self) -> Iterator[None]:
        real_to_thread = asyncio.to_thread
        real_wait_for = asyncio.wait_for

        async def _to_thread(func: Callable[..., object], /, *args: object, **kwargs: object) -> object:
            if getattr(func, "__name__", None) == _LOADER_FUNC_NAME:
                self.loader_dispatches += 1
            return await real_to_thread(func, *args, **kwargs)

        async def _wait_for(aw: object, /, *args: object, **kwargs: object) -> object:
            # Counted before suspending: the waiter blocks on the event within
            # this same scheduler step, with no yield in between.
            if getattr(aw, "__qualname__", None) == _WAITER_AWAITABLE_QUALNAME:
                self.waiter_registrations += 1
            return await real_wait_for(aw, *args, **kwargs)

        with (
            patch(f"{_BACKEND_ASYNCIO}.to_thread", _to_thread),
            patch(f"{_BACKEND_ASYNCIO}.wait_for", _wait_for),
        ):
            yield

    async def until_blocked(self, waiters: int) -> None:
        """Yield to the event loop until exactly one loader is dispatched and
        ``waiters`` callers are blocked on its in-flight load; fail (never
        hang) after ``_MAX_SCHEDULER_YIELDS`` yields."""
        for _ in range(_MAX_SCHEDULER_YIELDS):
            if self.loader_dispatches == 1 and self.waiter_registrations == waiters:
                return
            await asyncio.sleep(0)
        raise AssertionError(
            f"expected 1 loader and {waiters} blocked waiters, got "
            f"{self.loader_dispatches} loader(s) and {self.waiter_registrations} waiter(s)"
        )

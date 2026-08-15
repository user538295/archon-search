"""LRU cache for Embedder instances with async deduplication of concurrent loads."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from archon_search.embedder import Embedder, make_embedder

logger = logging.getLogger(__name__)

# Unknown model validation: make_embedder() builds a LAZY backend, so it returns
# an Embedder for ANY name — the name is only checked when fastembed is reached by
# the first encode(), i.e. by preload()'s warm-up embed() or the first real query.
# validate_embedding_model() (Task 4.2) is the up-front check at PATCH time, and
# resolves known names from fastembed's registry without loading the model at all.

# Upper bound on how long a deduplicated waiter blocks on the active loader. A
# stalled loader (slow disk, wedged ONNX init) must not hang every concurrent
# search for that model forever with no diagnostic.
_LOAD_WAIT_TIMEOUT_SECONDS: float = 120.0


class EmbedderNotReadyError(RuntimeError):
    """Raised when a waiter times out waiting for a model to load.

    Callers map this to HTTP 503 (not ready yet, retry) rather than 500: the
    load is wedged or merely slow, not a bug in the request.
    """


# Wire-safe detail/code pair every surface (REST, MCP, background job tasks) maps
# EmbedderNotReadyError to. Never use str(exc) on the wire — it embeds the internal
# class name, the ``_LOAD_WAIT_TIMEOUT_SECONDS`` value, and the model name; str(exc)
# is fine in a `logger.warning` call, never in a response body.
EMBEDDER_NOT_READY_DETAIL = "service unavailable: embedding model still loading"
EMBEDDER_NOT_READY_CODE = "embedder_not_ready"


class EmbedderCache:
    """Async LRU cache for Embedder objects.

    Concurrent callers requesting the same model are deduplicated: only one
    thread calls make_embedder; the others await the result via an asyncio.Event.
    Exception: a loader cancelled mid-load (``asyncio.to_thread`` cannot stop the
    running thread) clears its registration in ``finally``, so a waiter can become
    a new loader and two threads briefly run make_embedder for the same model —
    see the ``finally`` block in ``get_or_load``.

    ``providers`` is the ONNX Runtime execution-provider list (``[database]
    providers``) forwarded to every ``make_embedder`` call, so cached embedders
    — which serve every search — use the same accelerator as the global one.
    """

    def __init__(self, max_size: int, providers: list[str] | None = None) -> None:
        self._max_size = max_size
        self._providers = providers
        self._cache: OrderedDict[str, Embedder] = OrderedDict()
        self._lock = asyncio.Lock()
        self._loading: dict[str, asyncio.Event] = {}

    async def get_or_load(self, model_name: str) -> Embedder:
        """Return a cached Embedder, loading it on first access.

        Concurrent calls for the same model are deduplicated: make_embedder
        is called at most once while a load is in progress — except when a
        loader is cancelled mid-load, in which case its ``finally`` clears the
        registration and a waiter becomes a new loader (see the ``finally``
        block below).

        Raises ``EmbedderNotReadyError`` when a deduplicated waiter blocks longer
        than ``_LOAD_WAIT_TIMEOUT_SECONDS`` on the active loader — a wedged load
        must surface as a diagnosable 503, not as a permanently hung request.
        """
        while True:  # retry loop — handles the case where the loader failed
            async with self._lock:
                # Cache hit
                if model_name in self._cache:
                    self._cache.move_to_end(model_name)
                    return self._cache[model_name]
                # Another coroutine is already loading this model
                if model_name in self._loading:
                    event = self._loading[model_name]
                    # Fall through to await OUTSIDE the lock
                else:
                    # We are the loader — register event and break out to load
                    event = asyncio.Event()
                    self._loading[model_name] = event
                    break  # exit lock, proceed to load

            # Await the in-progress load OUTSIDE the lock to avoid deadlock
            try:
                await asyncio.wait_for(event.wait(), timeout=_LOAD_WAIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # Do NOT touch self._loading here. A waiter has no authority over
                # the loader's lifecycle: the loader clears its own registration
                # on every exit path — success, any exception, or cancellation
                # delivered anywhere in the loader region, including while it is
                # parked acquiring self._lock on the success path — see the
                # try/finally below, so the only way a registration outlives its
                # loader is a loader that is never resumed at all, in which case
                # a second loader would hang identically. Deleting the entry here
                # (even with an identity guard) only empties _loading while the
                # original loader is still running, turning the very next caller
                # into a second concurrent loader — a duplicate multi-hundred-MB
                # ONNX load for the same model.
                raise EmbedderNotReadyError(
                    f"EmbedderCache: timed out after {_LOAD_WAIT_TIMEOUT_SECONDS}s waiting for "
                    f"model {model_name!r} to load"
                ) from None
            # After event fires the loader either stored the embedder or raised.
            # Loop back to re-check the cache; if the load failed the event is
            # cleared from _loading so we will become the next loader ourselves.

        # --- We are the loader (lock released at the break above) ---
        # try/finally spans the WHOLE loader region — not just the to_thread
        # call — because a cancellation delivered while this coroutine is
        # parked acquiring self._lock below (e.g. a firing warm-up timeout, or
        # shutdown/client-disconnect cancellation racing a contended lock) is
        # just as reachable as one delivered inside to_thread. Without the
        # finally covering that await too, such a cancellation would leave
        # _loading[model] registered with its event never set — no later
        # waiter can ever clean it up (the waiter's timeout handler above
        # deliberately never touches _loading) — permanently poisoning the
        # model for the process lifetime.
        try:
            embedder = await asyncio.to_thread(make_embedder, model_name, providers=self._providers)
            # Load succeeded — store in cache under lock. Only the cache
            # mutation and LRU eviction need the lock; _loading is cleared
            # below, outside it.
            async with self._lock:
                self._cache[model_name] = embedder
                self._cache.move_to_end(model_name)
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)  # evict LRU
        finally:
            # No lock here: dict.pop + Event.set are already atomic under
            # single-threaded asyncio (there is no await point between them),
            # and self._lock exists only to make check-then-act sequences on
            # self._cache atomic — there is no preceding read of _loading to
            # race against here. Awaiting a contended lock inside this handler
            # would add an await point that a second cancellation could
            # interrupt, leaving _loading poisoned — exactly what this clause
            # exists to prevent. Runs on every exit — success or exception —
            # so a cancellation while acquiring self._lock above (embedder
            # built but not yet cached) also clears the registration, letting
            # the next caller become a fresh loader instead of blocking 120s.
            ev = self._loading.pop(model_name, None)
            if ev:
                ev.set()  # wake waiters so they retry rather than deadlock
        return embedder

    async def preload(self, model_names: list[str]) -> None:
        """Load and warm up models concurrently; log and skip any that fail.

        Caching an Embedder is not enough: make_embedder builds a lazy backend
        whose ONNX weights are only constructed on the first encode(). The
        warm-up embed() call pays that cost at startup so the first real query
        does not.

        A model whose warm-up fails is evicted only when its backend never loaded:
        leaving a cold embedder cached would make cached_models() report it as
        preloaded when no ONNX weights were ever built. A warm embedder whose
        warm-up embed() nevertheless raised is kept — the load cost is already paid
        and evicting it would only throw that work away.
        """

        async def _load_and_warm(model: str) -> None:
            try:
                embedder = await self.get_or_load(model)
            except Exception as exc:  # noqa: BLE001 — one bad model must not abort the rest
                logger.warning("EmbedderCache.preload: failed to load %r — %s", model, exc)
                return
            try:
                await embedder.embed(["warmup"])
            except Exception as exc:  # noqa: BLE001 — same: never fail startup
                # encode() assigns the backend model BEFORE running inference, so a
                # failure here does not prove the model failed to load.
                async with self._lock:
                    cached = self._cache.get(model)
                    evicted = cached is not None and not cached.is_warm
                    if evicted:
                        del self._cache[model]
                logger.warning(
                    "EmbedderCache.preload: failed to warm up %r%s — %s",
                    model,
                    "; evicting from cache" if evicted else "",
                    exc,
                )

        await asyncio.gather(*[_load_and_warm(m) for m in model_names])

    def cached_models(self) -> list[str]:
        """Return the list of currently cached model names (LRU order, oldest first)."""
        return list(self._cache.keys())

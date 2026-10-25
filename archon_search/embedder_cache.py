"""LRU cache for Embedder instances with async deduplication of concurrent loads."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping

from archon_search.embedder import WARMUP_TEXT, Embedder, make_embedder
from archon_search.torch_runtime import AUTO_DEVICE, release_model_memory

logger = logging.getLogger(__name__)

# make_embedder() resolves the model spec eagerly, so it raises UnsupportedModelError for a
# name outside the registry before any weights are touched. model_validation.validate_embedding_model()
# is the cheap up-front check for callers that must reject an unknown name before using the cache.

# Upper bound on how long a deduplicated waiter blocks on the active loader. A
# stalled loader (slow disk, wedged weight load) must not hang every concurrent
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

    ``device`` is the torch device setting (``[database].device``) forwarded to every
    ``make_embedder`` call, so cached embedders — which serve every search — run on
    the same device as the global one.

    ``preloaded`` seeds the cache with embedders that already exist (name → embedder): the
    server seeds the default model with the pipeline's global embedder so both resolve to one
    copy of its weights. A seeded entry is pinned: never evicted and not counted against
    ``max_size`` (its owner keeps it alive anyway, so evicting it would only make the next
    ``get_or_load`` of the default model build a second copy of its weights).

    A model name outside the registry fails fast: ``make_embedder`` raises
    ``UnsupportedModelError`` in the loader thread, ``get_or_load`` propagates it, and
    nothing is cached — the registration is cleared like for any failed load, so a
    waiter or a later call re-attempts (and fails) immediately rather than hanging or
    reading a poisoned entry. ``preload`` logs and skips such a model.
    """

    def __init__(
        self, max_size: int, device: str = AUTO_DEVICE, *, preloaded: Mapping[str, Embedder] | None = None
    ) -> None:
        self._max_size = max_size
        self._device = device
        self._pinned: frozenset[str] = frozenset(preloaded or ())
        self._cache: OrderedDict[str, Embedder] = OrderedDict(preloaded or {})
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
            except TimeoutError:
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
                # weight load for the same model.
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
        evicted: list[Embedder] = []
        try:
            embedder = await asyncio.to_thread(make_embedder, model_name, device=self._device)
            # Load succeeded — store in cache under lock. Only the cache
            # mutation and LRU eviction need the lock; _loading is cleared
            # below, outside it.
            async with self._lock:
                self._cache[model_name] = embedder
                self._cache.move_to_end(model_name)
                unpinned = [name for name in self._cache if name not in self._pinned]  # LRU first
                for victim in unpinned[: max(0, len(unpinned) - self._max_size)]:
                    evicted.append(self._cache.pop(victim))
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
        await self._release_evicted(evicted)
        return embedder

    async def _release_evicted(self, evicted: list[Embedder]) -> None:
        """Return the memory of the warm embedders in *evicted* (see ``release_model_memory``).

        Empties *evicted* first: the collection can only reclaim a model nothing references, and this
        list (the caller's own local too) would keep every evicted embedder alive through it. Runs after
        the registration is cleared, so waiters are not held up by the collection, and in a worker
        thread: it walks every live object. An evicted embedder a request still holds keeps its weights
        until that request is done; a cold one holds none. A failure costs only the early release, never
        the load that evicted.
        """
        any_warm = any(embedder.is_warm for embedder in evicted)
        names = ", ".join(repr(embedder.model_name) for embedder in evicted)
        evicted.clear()
        if not any_warm:
            return
        # The release is a best-effort side effect of the eviction: a failure is logged with its
        # traceback and never raised, so it can neither fail the load that evicted nor mask its result.
        try:
            await asyncio.to_thread(release_model_memory, self._device)
        except Exception as exc:  # memory release is best-effort; any failure is logged, never raised
            logger.warning(
                "EmbedderCache: releasing the memory of evicted %s failed — %s", names, exc, exc_info=True
            )

    async def preload(self, model_names: list[str]) -> list[str]:
        """Load and warm up models concurrently; log and skip any that fail.

        Returns the names in *model_names* whose load or warm-up ``embed()`` failed,
        in that order (``[]`` when every model warmed). This is an intentional
        command-with-outcome, like ``asyncio.gather`` returning its results: the
        outcome cannot be read back from cache state afterwards, because LRU eviction
        can push out a model that warmed fine and an embedder whose warm-up ``embed()``
        raised can still be cached warm. Never raises for a failing model.

        Caching an Embedder is not enough: make_embedder builds a lazy backend
        whose weights are only loaded on the first encode(). The
        warm-up embed() call pays that cost at startup so the first real query
        does not.

        A model whose warm-up fails is evicted only when its backend never loaded:
        leaving a cold embedder cached would make cached_models() report it as
        preloaded when no weights were ever loaded. A warm embedder whose
        warm-up embed() nevertheless raised is kept — the load cost is already paid
        and evicting it would only throw that work away.
        """

        async def _load_and_warm(model: str) -> bool:
            try:
                embedder = await self.get_or_load(model)
            except Exception as exc:  # noqa: BLE001 — preload contract: a failing model is logged and skipped, never raised
                logger.warning("EmbedderCache.preload: failed to load %r — %s", model, exc)
                return False
            try:
                await embedder.embed([WARMUP_TEXT])
            except Exception as exc:  # noqa: BLE001 — preload contract: a failing warm-up is logged and skipped, never raised
                # encode() assigns the backend model BEFORE running inference, so a
                # failure here does not prove the model failed to load. A pinned (seeded)
                # embedder is never evicted, even cold: the lifespan owns that object.
                async with self._lock:
                    cached = self._cache.get(model)
                    evicted = cached is not None and not cached.is_warm and model not in self._pinned
                    if evicted:
                        del self._cache[model]
                logger.warning(
                    "EmbedderCache.preload: failed to warm up %r%s — %s",
                    model,
                    "; evicting from cache" if evicted else "",
                    exc,
                )
                return False
            return True

        warmed = await asyncio.gather(*[_load_and_warm(m) for m in model_names])
        return [model for model, ok in zip(model_names, warmed, strict=True) if not ok]

    def cold_models(self, model_names: list[str]) -> list[str]:
        """Return the names in *model_names* with no warm embedder cached, in that order.

        A model is cold when it is not cached — never loaded, failed to load, evicted
        by ``preload`` after a failed warm-up, or pushed out by LRU eviction — or when
        it is cached but its lazy backend was never built. A pure query: nothing is
        loaded and the LRU order is untouched. A state probe, not an outcome: use it
        only where ``preload``'s return value is unavailable (a warm-up cut short by
        its timeout), since a model the LRU pushed out after warming reads as cold.
        """
        return [
            model
            for model in model_names
            if (embedder := self._cache.get(model)) is None or not embedder.is_warm
        ]

    def cached_models(self) -> list[str]:
        """Return the list of currently cached model names (LRU order, oldest first)."""
        return list(self._cache.keys())

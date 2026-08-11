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


class EmbedderCache:
    """Async LRU cache for Embedder objects.

    Concurrent callers requesting the same model are deduplicated: only one
    thread calls make_embedder; the others await the result via an asyncio.Event.

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
        is called at most once while a load is in progress.
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
            await event.wait()
            # After event fires the loader either stored the embedder or raised.
            # Loop back to re-check the cache; if the load failed the event is
            # cleared from _loading so we will become the next loader ourselves.

        # --- We are the loader (lock released at the break above) ---
        try:
            embedder = await asyncio.to_thread(make_embedder, model_name, providers=self._providers)
        except Exception:
            async with self._lock:
                ev = self._loading.pop(model_name, None)
                if ev:
                    ev.set()  # wake waiters so they retry rather than deadlock
            raise  # propagate to our caller

        # Load succeeded — store in cache under lock
        async with self._lock:
            self._cache[model_name] = embedder
            self._cache.move_to_end(model_name)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)  # evict LRU
            ev = self._loading.pop(model_name, None)
            if ev:
                ev.set()  # wake waiters
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

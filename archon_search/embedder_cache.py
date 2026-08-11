"""LRU cache for Embedder instances with async deduplication of concurrent loads."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from archon_search.embedder import Embedder, make_embedder

logger = logging.getLogger(__name__)

# Unknown model validation: EmbedderCache relies on make_embedder() to raise
# for unknown model names. validate_embedding_model() (Task 4.2) provides
# dimension validation at PATCH time without requiring a full model load.


class EmbedderCache:
    """Async LRU cache for Embedder objects.

    Concurrent callers requesting the same model are deduplicated: only one
    thread calls make_embedder; the others await the result via an asyncio.Event.
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
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
            embedder = await asyncio.to_thread(make_embedder, model_name)
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
        """

        async def _load_and_warm(model: str) -> None:
            embedder = await self.get_or_load(model)
            await embedder.embed(["warmup"])

        results = await asyncio.gather(
            *[_load_and_warm(m) for m in model_names],
            return_exceptions=True,
        )
        for model, result in zip(model_names, results):
            if isinstance(result, Exception):
                logger.warning(
                    "EmbedderCache.preload: failed to preload %r — %s", model, result
                )

    def cached_models(self) -> list[str]:
        """Return the list of currently cached model names (LRU order, oldest first)."""
        return list(self._cache.keys())

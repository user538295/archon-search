"""LRU-backed in-memory embedding cache."""
from __future__ import annotations
from functools import lru_cache
from typing import Callable


class EmbeddingCache:
    """Cache embedding vectors keyed on text to avoid redundant model calls."""

    def __init__(self, max_size: int = 1024) -> None:
        self._max_size = max_size
        self._cache: dict[str, list[float]] = {}
        self._order: list[str] = []

    def get(self, text: str) -> list[float] | None:
        return self._cache.get(text)

    def set(self, text: str, vector: list[float]) -> None:
        if text in self._cache:
            self._order.remove(text)
        elif len(self._cache) >= self._max_size:
            oldest = self._order.pop(0)
            del self._cache[oldest]
        self._cache[text] = vector
        self._order.append(text)

    def get_or_compute(
        self, text: str, compute_fn: Callable[[str], list[float]]
    ) -> list[float]:
        if (cached := self.get(text)) is not None:
            return cached
        vector = compute_fn(text)
        self.set(text, vector)
        return vector

    def __len__(self) -> int:
        return len(self._cache)

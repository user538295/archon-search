"""Reranking layer for RAG — wraps fastembed.TextCrossEncoder with async support."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol, runtime_checkable

from archon_search._types import SearchResult


@runtime_checkable
class RerankerBackend(Protocol):
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]: ...


class ModelReranker:
    """Lazy-loading fastembed TextCrossEncoder backend."""

    def __init__(self, model_name: str, providers: list[str] | None = None) -> None:
        self._model_name = model_name
        self._providers = providers or None  # None = CPU default in fastembed
        self._model: Any = None  # loaded on first predict()
        self._lock = threading.Lock()

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

                    self._model = TextCrossEncoder(self._model_name, providers=self._providers)
        # TextCrossEncoder.rerank(query, documents) → Iterable[float]
        # All pairs share the same query (pairs[0][0])
        query = pairs[0][0]
        documents = [p[1] for p in pairs]
        return list(self._model.rerank(query, documents))


class Reranker:
    """Async wrapper around a RerankerBackend."""

    def __init__(self, backend: RerankerBackend) -> None:
        self._backend = backend

    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        if not candidates:
            return []

        pairs = [(query, c.text) for c in candidates]
        scores: list[float] = await asyncio.to_thread(self._backend.predict, pairs)

        if len(scores) != len(candidates):
            raise ValueError(
                f"Backend returned {len(scores)} scores for {len(candidates)} candidates"
            )

        # Update scores in-place (intentional per spec) and return a sorted copy
        for candidate, score in zip(candidates, scores):
            candidate.score = score

        return sorted(candidates, key=lambda c: c.score, reverse=True)[:top_k]


def make_reranker(model_name: str, providers: list[str] | None = None) -> Reranker:
    """Factory: create a ModelReranker-backed Reranker."""
    return Reranker(ModelReranker(model_name, providers=providers))

"""Reranking layer for RAG — wraps CrossEncoder with async support."""
from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from archon.rag._types import SearchResult


@runtime_checkable
class RerankerBackend(Protocol):
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]: ...


class ModelReranker:
    """Lazy-loading CrossEncoder backend."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None  # loaded on first predict()

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        if self._model is None:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415

            self._model = CrossEncoder(self._model_name)
        return self._model.predict(pairs).tolist()


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

        ranked = sorted(
            zip(scores, candidates), key=lambda item: item[0], reverse=True
        )
        return [c for _, c in ranked[:top_k]]


def make_reranker(model_name: str) -> Reranker:
    """Factory: create a ModelReranker-backed Reranker."""
    return Reranker(ModelReranker(model_name))

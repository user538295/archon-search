"""Embedding layer for RAG — wraps SentenceTransformer with async support."""
from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbedderBackend(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class ModelEmbedder:
    """Lazy-loading SentenceTransformer backend."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None  # loaded on first encode()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415

            self._model = SentenceTransformer(self._model_name)
        return self._model.encode(texts).tolist()


class Embedder:
    """Async wrapper around an EmbedderBackend."""

    def __init__(self, backend: EmbedderBackend) -> None:
        self._backend = backend
        self._embedding_dim: int | None = None

    @property
    def embedding_dim(self) -> int:
        if self._embedding_dim is None:
            probe = self._backend.encode(["probe"])
            self._embedding_dim = len(probe[0])
        return self._embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._backend.encode, texts)

    async def embed_one(self, text: str) -> list[float]:
        results = await self.embed([text])
        return results[0]


def make_embedder(model_name: str) -> Embedder:
    """Factory: create a ModelEmbedder-backed Embedder."""
    return Embedder(ModelEmbedder(model_name))

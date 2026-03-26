"""Embedding layer for RAG — wraps fastembed.TextEmbedding with async support."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbedderBackend(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class ModelEmbedder:
    """Lazy-loading fastembed TextEmbedding backend."""

    def __init__(self, model_name: str, providers: list[str] | None = None) -> None:
        self._model_name = model_name
        self._providers = providers or None  # None = CPU default in fastembed
        self._model: Any = None  # loaded on first encode()
        self._lock = threading.Lock()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding  # noqa: PLC0415

                    self._model = TextEmbedding(self._model_name, providers=self._providers)
        # TextEmbedding.embed() returns a generator of 1-D numpy arrays
        return [e.tolist() for e in self._model.embed(texts)]


class Embedder:
    """Async wrapper around an EmbedderBackend."""

    def __init__(self, backend: EmbedderBackend) -> None:
        self._backend = backend
        self._embedding_dim: int | None = None

    @property
    def embedding_dim(self) -> int:
        """Return the cached embedding dimension.

        Raises RuntimeError if embed() has never been called.
        This property does NOT call backend.encode itself.
        """
        if self._embedding_dim is None:
            raise RuntimeError(
                "embedding_dim not yet initialized — call embed() first"
            )
        return self._embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode texts in a thread pool; lazily initialises embedding_dim."""
        result: list[list[float]] = await asyncio.to_thread(self._backend.encode, texts)
        if self._embedding_dim is None and result:
            self._embedding_dim = len(result[0])
        return result

    async def embed_one(self, text: str) -> list[float]:
        results = await self.embed([text])
        return results[0]


def make_embedder(model_name: str, providers: list[str] | None = None) -> Embedder:
    """Factory: create a ModelEmbedder-backed Embedder."""
    return Embedder(ModelEmbedder(model_name, providers=providers))

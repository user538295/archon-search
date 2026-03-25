"""tests/rag/test_embedder.py — unit tests for Embedder (fastembed backend)."""
from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from archon.rag.embedder import Embedder, EmbedderBackend, make_embedder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockBackend:
    """Deterministic test backend — returns [float(i)] * dim vectors."""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.call_count = 0
        self.called_from_threads: list[str] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.called_from_threads.append(threading.current_thread().name)
        return [[float(i)] * self.dim for i in range(len(texts))]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedder_mock_backend_returns_correct_shape() -> None:
    """MockEmbedder(dim=4) → embed(['a','b']) returns 2×4 list."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    result = await embedder.embed(["a", "b"])
    assert len(result) == 2
    assert all(len(v) == 4 for v in result)


@pytest.mark.asyncio
async def test_embedder_embed_one_returns_single_vector() -> None:
    backend = _MockBackend(dim=3)
    embedder = Embedder(backend)
    vec = await embedder.embed_one("hello")
    assert isinstance(vec, list)
    assert len(vec) == 3


@pytest.mark.asyncio
async def test_embedder_embedding_dim_raises_before_embed() -> None:
    """Accessing embedding_dim before any embed() call raises RuntimeError."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    with pytest.raises(RuntimeError, match="not yet initialized"):
        _ = embedder.embedding_dim


@pytest.mark.asyncio
async def test_embedder_embedding_dim_cached() -> None:
    """After embed(['a']), embedding_dim is cached; second access doesn't call backend again."""
    backend = _MockBackend(dim=8)
    embedder = Embedder(backend)
    await embedder.embed(["a"])
    assert embedder.embedding_dim == 8
    # Access embedding_dim again — should NOT trigger another backend call
    calls_before = backend.call_count
    _ = embedder.embedding_dim
    assert backend.call_count == calls_before  # no extra encode() called


@pytest.mark.asyncio
async def test_embedder_uses_to_thread() -> None:
    """backend.encode is called from a worker thread (not the event loop thread)."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    main_thread = threading.current_thread().name
    await embedder.embed(["hello"])
    # All encode calls should be from a thread-pool worker
    assert all(t != main_thread for t in backend.called_from_threads)


@pytest.mark.asyncio
async def test_embedder_embedding_dim_matches_backend_dim() -> None:
    backend = _MockBackend(dim=16)
    embedder = Embedder(backend)
    await embedder.embed(["test"])
    assert embedder.embedding_dim == 16


def test_make_embedder_returns_embedder() -> None:
    """make_embedder returns an Embedder without downloading models."""
    e = make_embedder("BAAI/bge-small-en-v1.5", providers=[])
    assert isinstance(e, Embedder)


def test_make_embedder_with_empty_providers() -> None:
    """make_embedder(providers=[]) does not raise."""
    e = make_embedder("BAAI/bge-small-en-v1.5", providers=[])
    assert e is not None


def test_embedder_backend_protocol() -> None:
    """_MockBackend satisfies the EmbedderBackend protocol."""
    backend = _MockBackend()
    assert isinstance(backend, EmbedderBackend)


@pytest.mark.asyncio
async def test_embedder_embed_empty_list_leaves_dim_unset() -> None:
    """embed([]) returns [] and leaves embedding_dim unset (RuntimeError on access)."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    result = await embedder.embed([])
    assert result == []
    with pytest.raises(RuntimeError, match="not yet initialized"):
        _ = embedder.embedding_dim

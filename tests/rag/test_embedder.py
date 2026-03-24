"""Tests for archon/rag/embedder.py — TDD, red → green."""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from archon.rag.embedder import Embedder, make_embedder


def _make_backend(dim: int = 4) -> MagicMock:
    """Return a mock EmbedderBackend that returns zero vectors of given dim."""
    backend = MagicMock()
    backend.encode.side_effect = lambda texts: [[0.0] * dim for _ in texts]
    return backend


def test_embedder_mock_backend_returns_correct_shape() -> None:
    backend = _make_backend(dim=4)
    embedder = Embedder(backend)
    result = asyncio.run(embedder.embed(["a", "b"]))
    assert len(result) == 2
    assert len(result[0]) == 4
    assert len(result[1]) == 4


def test_embedder_embed_one_returns_single_vector() -> None:
    backend = _make_backend(dim=4)
    embedder = Embedder(backend)
    result = asyncio.run(embedder.embed_one("x"))
    assert isinstance(result, list)
    assert len(result) == 4
    assert all(isinstance(v, float) for v in result)


def test_embedder_embedding_dim_cached() -> None:
    backend = _make_backend(dim=4)
    embedder = Embedder(backend)
    dim1 = embedder.embedding_dim
    dim2 = embedder.embedding_dim
    assert dim1 == 4
    assert dim2 == 4
    # backend.encode called exactly once for the probe
    backend.encode.assert_called_once_with(["probe"])


def test_embedder_uses_to_thread() -> None:
    """Verify encode runs in a worker thread, not the main thread."""
    main_thread = threading.main_thread()
    captured: list[threading.Thread] = []

    def encode_in_thread(texts: list[str]) -> list[list[float]]:
        captured.append(threading.current_thread())
        return [[0.0] * 4 for _ in texts]

    backend = MagicMock()
    backend.encode.side_effect = encode_in_thread

    embedder = Embedder(backend)
    asyncio.run(embedder.embed(["hello"]))

    assert len(captured) == 1
    assert captured[0] is not main_thread

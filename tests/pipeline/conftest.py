"""Shared plain helpers for tests/pipeline/ — no @pytest.fixture decorators.

Each split file imports these explicitly:
    from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline
"""
from __future__ import annotations

from archon_search.embedder import Embedder, EmbedderBackend
from archon_search.reranker import Reranker, RerankerBackend


class MockEmbedderBackend:
    """Returns dim=4 vectors for all texts."""

    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class MockRerankerBackend:
    """Returns 0.5 score for all pairs."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def make_embedder() -> Embedder:
    return Embedder(MockEmbedderBackend())


def make_reranker() -> Reranker:
    return Reranker(MockRerankerBackend())


def make_pipeline(store):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

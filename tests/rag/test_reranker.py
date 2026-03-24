"""Tests for archon.rag.reranker."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from archon.rag._types import SearchResult
from archon.rag.reranker import Reranker, make_reranker


def _make_result(text: str, score: float = 0.0) -> SearchResult:
    return SearchResult(
        doc_id="doc1",
        chunk_id="chunk1",
        text=text,
        score=score,
        source_path="path/to/doc.md",
    )


def _mock_backend(scores: list[float]) -> MagicMock:
    backend = MagicMock()
    backend.predict.return_value = scores
    return backend


@pytest.mark.asyncio
async def test_reranker_sorts_by_score_descending() -> None:
    candidates = [
        _make_result("low relevance"),
        _make_result("high relevance"),
        _make_result("medium relevance"),
    ]
    backend = _mock_backend([0.1, 0.9, 0.5])
    reranker = Reranker(backend)

    results = await reranker.rerank("query", candidates, top_k=3)

    assert [r.text for r in results] == [
        "high relevance",
        "medium relevance",
        "low relevance",
    ]


@pytest.mark.asyncio
async def test_reranker_truncates_to_top_k() -> None:
    candidates = [_make_result(f"doc {i}") for i in range(5)]
    backend = _mock_backend([0.1, 0.5, 0.3, 0.9, 0.7])
    reranker = Reranker(backend)

    results = await reranker.rerank("query", candidates, top_k=2)

    assert len(results) == 2


@pytest.mark.asyncio
async def test_reranker_empty_candidates_returns_empty() -> None:
    backend = _mock_backend([])
    reranker = Reranker(backend)

    results = await reranker.rerank("query", [], top_k=5)

    assert results == []
    backend.predict.assert_not_called()


@pytest.mark.asyncio
async def test_reranker_calls_backend_with_correct_pairs() -> None:
    candidates = [
        _make_result("text one"),
        _make_result("text two"),
    ]
    backend = _mock_backend([0.8, 0.2])
    reranker = Reranker(backend)

    await reranker.rerank("my query", candidates, top_k=2)

    backend.predict.assert_called_once_with([
        ("my query", "text one"),
        ("my query", "text two"),
    ])

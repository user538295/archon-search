"""Reranking layer for RAG — wraps fastembed.TextCrossEncoder with async support."""
from __future__ import annotations

import asyncio
import dataclasses
import threading
from typing import Any, Protocol, runtime_checkable

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search._types import SearchResult
from archon_search.observability import record_stage

# Trivial (query, document) pair used only to force a cold backend to load.
_WARMUP_PAIR = ("warmup", "warmup")


@runtime_checkable
class RerankerBackend(Protocol):
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]: ...

    @property
    def is_warm(self) -> bool: ...


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

    @property
    def is_warm(self) -> bool:
        return self._model is not None


class Reranker:
    """Async wrapper around a RerankerBackend."""

    def __init__(self, backend: RerankerBackend) -> None:
        self._backend = backend

    @property
    def is_warm(self) -> bool:
        return self._backend.is_warm

    async def warmup(self) -> None:
        """Build the backend's model now, off the request path.

        ``ModelReranker`` constructs its ONNX cross-encoder on the *first*
        ``predict`` call. Callers must run this outside any request timeout
        budget, otherwise the one-off load consumes the whole budget and an
        otherwise valid search fails with 504 (S184).

        Idempotent: a no-op once the backend is warm.
        """
        if self._backend.is_warm:
            return
        await asyncio.to_thread(self._backend.predict, [_WARMUP_PAIR])

    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        with record_stage("rerank"):
            if not candidates:
                return []

            pairs = [(query, c.text) for c in candidates]
            scores: list[float] = await asyncio.to_thread(self._backend.predict, pairs)

        if len(scores) != len(candidates):
            raise ValueError(
                f"Backend returned {len(scores)} scores for {len(candidates)} candidates"
            )

        for candidate, score in zip(candidates, scores):
            candidate.score = score

        return sorted(candidates, key=lambda c: c.score, reverse=True)[:top_k]

    async def rerank_candidates(
        self,
        query: str,
        candidates: list[ScoredSearchCandidate],
        top_k: int,
    ) -> list[ScoredSearchCandidate]:
        """Rerank ScoredSearchCandidates, preserving score provenance.

        Unified production-grade candidate rerank surface (used by both search
        and explain paths). Returns new ScoredSearchCandidate objects with
        reranker_score populated. Input candidates are NOT mutated.
        """
        with record_stage("rerank"):
            if not candidates:
                return []

            pairs = [(query, c.text) for c in candidates]
            scores: list[float] = await asyncio.to_thread(self._backend.predict, pairs)

        if len(scores) != len(candidates):
            raise ValueError(
                f"Backend returned {len(scores)} scores for {len(candidates)} candidates"
            )

        # Build new objects — never mutate input candidates
        traced: list[ScoredSearchCandidate] = []
        for candidate, reranker_score in zip(candidates, scores):
            new_breakdown = dataclasses.replace(
                candidate.score_breakdown, reranker_score=reranker_score
            )
            traced.append(dataclasses.replace(candidate, score_breakdown=new_breakdown))

        # Stable sort by reranker_score descending (Python sort is stable → equal scores keep input order)
        traced.sort(key=lambda c: c.score_breakdown.reranker_score if c.score_breakdown.reranker_score is not None else 0.0, reverse=True)
        return traced[:top_k]

    async def _rerank_with_trace(
        self,
        query: str,
        candidates: list[ScoredSearchCandidate],
        top_k: int,
    ) -> list[ScoredSearchCandidate]:
        """Backward-compat alias for :meth:`rerank_candidates`."""
        return await self.rerank_candidates(query, candidates, top_k)


def make_reranker(model_name: str, providers: list[str] | None = None) -> Reranker:
    """Factory: create a ModelReranker-backed Reranker."""
    return Reranker(ModelReranker(model_name, providers=providers))

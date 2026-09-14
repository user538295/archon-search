"""Reranking layer for RAG — wraps fastembed.TextCrossEncoder with async support."""
from __future__ import annotations

import asyncio
import dataclasses
import threading
from typing import Any, Protocol, runtime_checkable

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search._types import SearchResult
from archon_search.observability import record_stage
from archon_search.paths import get_models_dir

# Trivial (query, document) pair used only to force a cold backend to load.
_WARMUP_PAIR = ("warmup", "warmup")
_WARMUP_TIMEOUT_SECONDS = 300.0


class RerankerWarmupTimeout(Exception):
    """The warm-up build outran ``_WARMUP_TIMEOUT_SECONDS`` (the wait stopped, not the build)."""


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

                    self._model = TextCrossEncoder(
                        self._model_name,
                        providers=self._providers,
                        cache_dir=str(get_models_dir()),
                    )
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
        self._warmup_failed = False
        # In-flight build, shared by every concurrent caller (see warmup()), plus
        # the lock guarding it and the loop both belong to. All three are bound
        # to one event loop and recreated together when the loop changes: an
        # asyncio.Lock left held by a task in a since-closed loop would deadlock
        # every later warmup().
        self._warmup_loop: asyncio.AbstractEventLoop | None = None
        self._warmup_lock: asyncio.Lock | None = None
        self._warmup_task: asyncio.Task[None] | None = None

    @property
    def is_warm(self) -> bool:
        return self._backend.is_warm

    def _acquire_warmup_lock(self) -> asyncio.Lock:
        """Return the lock for the running loop, rebinding warm-up state if it changed."""
        loop = asyncio.get_running_loop()
        if self._warmup_lock is None or self._warmup_loop is not loop:
            stale = self._warmup_task
            if stale is not None and not stale.done():
                stale.cancel()
            self._warmup_task = None
            self._warmup_lock = asyncio.Lock()
            self._warmup_loop = loop
        return self._warmup_lock

    async def _run_warmup(self) -> None:
        """Force the backend to build its model. Never cancelled by a caller."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._backend.predict, [_WARMUP_PAIR]),
                timeout=_WARMUP_TIMEOUT_SECONDS,
            )
            self._warmup_failed = False
        except asyncio.TimeoutError as exc:
            self._warmup_failed = True
            raise RerankerWarmupTimeout(
                f"reranker warm-up exceeded {_WARMUP_TIMEOUT_SECONDS}s"
            ) from exc
        except Exception:
            self._warmup_failed = True
            raise

    async def warmup(self) -> None:
        """Build the backend's model now, off the request path.

        ``ModelReranker`` constructs its ONNX cross-encoder on the *first*
        ``predict`` call, so callers must run this outside any request timeout
        budget (S184). Contract:

        - Idempotent, and a no-op once warm.
        - Single-flight: one shared task does the build; callers only
          ``await asyncio.shield`` it, so a caller that bounds its own wait and
          gives up cancels nothing but that wait. The build keeps running, warms
          the backend for later requests, and later callers join it instead of
          starting a second ONNX build (S288 fan-out thundering herd).
        - Not latched on failure (S283): ``_warmup_failed`` records only the most
          recent attempt and is diagnostic; the next caller retries with a fresh
          task, since a settled task is never re-awaited.
        - Raises ``RerankerWarmupTimeout`` if the build outruns
          ``_WARMUP_TIMEOUT_SECONDS``. That stops the *waiting*, not the build:
          ``asyncio.to_thread`` workers are not cancellable, so the thread runs
          to completion (and, being non-daemon, delays executor shutdown).

        See ``Documentation/Architecture/140_error_handling_strategy.md`` for how
        the caller degrades when this returns cold.
        """
        if self._backend.is_warm:
            return
        async with self._acquire_warmup_lock():
            if self._backend.is_warm:
                return
            task = self._warmup_task
            if task is None or task.done():
                task = asyncio.ensure_future(self._run_warmup())
                task.add_done_callback(self._on_warmup_done)
                self._warmup_task = task
        await asyncio.shield(task)

    def _on_warmup_done(self, task: asyncio.Task[None]) -> None:
        """Drop the settled task and swallow its result so asyncio stays quiet.

        A build whose every waiter was cancelled has no one left to retrieve its
        exception; retrieving it here avoids a spurious "Task exception was
        never retrieved" warning.
        """
        if self._warmup_task is task:
            self._warmup_task = None
        if not task.cancelled():
            task.exception()

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
            candidate.reranker_score = score

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

        # Stable sort by reranker_score descending (Python sort is stable → equal scores keep input order).
        # Deliberately NOT tie-broken on chunk_id: that reorders the equal-score
        # block away from the retrieval order the fusion stage produced, which
        # measurably regresses the eval gate (tests/eval/test_eval_baseline_unchanged.py).
        traced.sort(
            key=lambda c: c.score_breakdown.reranker_score
            if c.score_breakdown.reranker_score is not None
            else 0.0,
            reverse=True,
        )
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

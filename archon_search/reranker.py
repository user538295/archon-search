"""Reranking layer for RAG — a PyTorch cross-encoder backend with async support."""
from __future__ import annotations

import asyncio
import dataclasses
import threading
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search.model_specs import UnsupportedModelError, get_reranker_spec
from archon_search.model_store import ensure_model
from archon_search.observability import record_stage
from archon_search.reranker_loading import load_cross_encoder
from archon_search.text_batching import map_in_token_batches
from archon_search.torch_runtime import (
    AUTO_DEVICE,
    DEFAULT_PAD_TOKEN_ID,
    accelerator_lock,
    pad_encodings,
    release_accelerator_cache,
    resolve_device,
)
from archon_search.warmup_coordinator import WarmupCoordinator, WarmupTimeout

if TYPE_CHECKING:
    import torch
    from tokenizers import Encoding, Tokenizer

_RERANK_STAGE = "rerank"
# Trivial (query, document) pair used only to force a cold backend to load.
_WARMUP_PAIR = ("warmup", "warmup")


class RerankerWarmupTimeout(WarmupTimeout):
    """The reranker warm-up build outran ``WARMUP_TIMEOUT_SECONDS`` (the wait stopped, not the build)."""


@runtime_checkable
class RerankerBackend(Protocol):
    """Scores (query, document) pairs; may load its weights lazily.

    ``predict`` returns one raw logit per pair, in input order (higher means more relevant). It is
    called from worker threads, possibly several at once, so an implementation must be thread-safe.
    ``is_warm`` is ``True`` once the weights are loaded, so ``predict`` no longer pays the load.
    """

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]: ...

    @property
    def is_warm(self) -> bool: ...


class _CrossEncoder(NamedTuple):
    """A loaded model with everything ``predict`` needs to drive it."""

    tokenizer: Tokenizer
    model: torch.nn.Module
    device: str
    pad_token_id: int


class ModelReranker:
    """Lazy-loading PyTorch cross-encoder backend.

    The spec is resolved eagerly, so an unsupported *model_name* fails at
    construction. The weights are fetched (``ensure_model``) and loaded onto the
    device resolved from *device* (``torch_runtime.resolve_device``) on the first
    non-empty ``predict``; a failed load is not latched, the next call retries.
    ``predict`` is safe to call from several threads.

    Raises:
        UnsupportedModelError: from the constructor, for a model outside the registry.
    """

    def __init__(self, model_name: str, device: str = AUTO_DEVICE) -> None:
        self.model_name = model_name
        self._spec = get_reranker_spec(model_name)
        self._device_setting = device
        self._cross_encoder: _CrossEncoder | None = None  # loaded on first predict()
        self._lock = threading.Lock()

    @property
    def is_warm(self) -> bool:
        return self._cross_encoder is not None

    def _load(self) -> _CrossEncoder:
        """Return the cross-encoder, loading it once; concurrent callers wait on the lock."""
        if self._cross_encoder is None:
            with self._lock:
                if self._cross_encoder is None:
                    path = ensure_model(self._spec)
                    device = resolve_device(self._device_setting)
                    tokenizer, model = load_cross_encoder(self._spec, path, device)
                    pad_token_id = getattr(model.config, "pad_token_id", None) or DEFAULT_PAD_TOKEN_ID
                    self._cross_encoder = _CrossEncoder(tokenizer, model, device, pad_token_id)
        return self._cross_encoder

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score each ``(query, document)`` pair; the result is raw logits in input order.

        Each pair is encoded as a text pair truncated to ``spec.max_length`` tokens
        (``longest_first``); the pairs need not share a query.
        """
        if not pairs:
            return []
        cross_encoder = self._load()
        encodings = [cross_encoder.tokenizer.encode(query, document) for query, document in pairs]
        return map_in_token_batches(
            [len(encoding.ids) for encoding in encodings],
            lambda batch: self._score_batch(cross_encoder, [encodings[index] for index in batch]),
            release_memory=lambda: release_accelerator_cache(cross_encoder.device),
        )

    def _score_batch(self, cross_encoder: _CrossEncoder, encodings: list[Encoding]) -> list[float]:
        import torch  # lazy: keeps ``import archon_search.reranker`` free of PyTorch

        with accelerator_lock(cross_encoder.device):
            inputs = pad_encodings(
                torch,
                encodings,
                pad_token_id=cross_encoder.pad_token_id,
                device=cross_encoder.device,
                with_token_types=self._spec.uses_token_type_ids,
            )
            with torch.inference_mode():
                logits = cross_encoder.model(**inputs).logits
            scores: list[float] = logits[:, 0].float().cpu().tolist()
            return scores


class UnsupportedModelReranker:
    """Backend for a model name outside the registry: stays cold and fails every ``predict``.

    Lets the server start with such a name and report ``checks.models == "fail"`` instead of
    aborting; ``predict`` raises the ``UnsupportedModelError`` that ``ModelReranker`` raised at construction.
    """

    def __init__(self, error: UnsupportedModelError) -> None:
        self.model_name = error.name
        self._supported = error.supported

    @property
    def is_warm(self) -> bool:
        return False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        # A fresh error per call: re-raising one instance would grow its ``__traceback__`` on every predict.
        raise UnsupportedModelError(self.model_name, self._supported)


class Reranker:
    """Async wrapper around a RerankerBackend."""

    def __init__(self, backend: RerankerBackend) -> None:
        self._backend = backend
        self._warmup_coordinator = WarmupCoordinator(
            subject="reranker",
            is_warm=lambda: self.is_warm,
            build=lambda: asyncio.to_thread(self._backend.predict, [_WARMUP_PAIR]),
            timeout_error=RerankerWarmupTimeout,
        )

    @property
    def backend(self) -> RerankerBackend:
        """The wrapped backend — what a startup probe runs so it shares this instance's weights."""
        return self._backend

    @property
    def is_warm(self) -> bool:
        return self._backend.is_warm

    async def warmup(self) -> None:
        """Build the backend's model now, off the request path (see ``WarmupCoordinator.warmup``).

        ``ModelReranker`` constructs its cross-encoder on the *first* ``predict`` call, so a
        request that triggers it spends its whole timeout budget on the load and fails with 504.
        Concurrent cold requests join one build instead of each starting another.

        See ``Documentation/Architecture/140_error_handling_strategy.md`` for how the caller
        degrades when this returns cold.

        Raises:
            RerankerWarmupTimeout: if the build outruns ``WARMUP_TIMEOUT_SECONDS``.
        """
        await self._warmup_coordinator.warmup()

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
        with record_stage(_RERANK_STAGE):
            if not candidates:
                return []

            pairs = [(query, candidate.text) for candidate in candidates]
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
            key=lambda candidate: candidate.score_breakdown.reranker_score
            if candidate.score_breakdown.reranker_score is not None
            else 0.0,
            reverse=True,
        )
        return traced[:top_k]


def make_reranker(model_name: str, device: str = AUTO_DEVICE) -> Reranker:
    """Factory: create a ModelReranker-backed Reranker (no weights are loaded yet)."""
    return Reranker(ModelReranker(model_name, device=device))

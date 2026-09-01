"""Prose entity/relation extraction layer — wraps gliner.GLiNER with async support.

Lazy-loading, single-shared-instance backend for the graph NER/RelEx engine
(Task BE-8). Mirrors ``embedder.py``'s lazy-load-behind-a-lock pattern: the
model is constructed once per process, off the event loop, on first ``load()``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from archon_search.paths import (
    GRAPH_NER_MODEL_NAME,
    GRAPH_NER_MODEL_REVISION,
    get_graph_models_dir,
)

if TYPE_CHECKING:
    # Type-checking only — the runtime import stays deferred inside _load_sync().
    from gliner import GLiNER

logger = logging.getLogger(__name__)

# ponytail: fixed 1/1 intra/inter-op thread pin (ceiling), not derived from cpu_count()
# — this process already runs N pytest-xdist/uvicorn workers plus the embedder/
# reranker threads, and oversubscribing torch threads per worker regressed
# throughput in the fastembed backends.
_TORCH_INTRA_OP_THREADS = 1
_TORCH_INTER_OP_THREADS = 1

# [graph].providers values are ONNX-style execution provider strings (see
# archon-search.toml.example's [database] section, which the reranker/embedder
# share) — never the literal "cuda"/"mps" torch device names.
_CUDA_PROVIDER_MARKER = "cuda"
_MPS_PROVIDER_MARKER = "coreml"

# Mirrors embedder.py / reranker.py's _WARMUP_TIMEOUT_SECONDS: bounds the blocking
# GLiNER.from_pretrained() download so a stalled network cannot hang the caller
# (or the install wizard's pre-warm step) forever.
_LOAD_TIMEOUT_SECONDS = 300.0


class ProseExtractionBackend:
    """Lazy-loading gliner.GLiNER backend for prose entity/relation extraction.

    One shared instance per process. ``load()`` is idempotent, single-flight
    (concurrent cold callers wait on a lock; only one load runs), and off the
    event loop via ``asyncio.to_thread``. A failed load is latched — the
    second call raises the same exception without retrying — except for
    ``asyncio.CancelledError``, which is never latched so a cancelled caller's
    successor can retry.
    """

    def __init__(
        self,
        model_name: str = GRAPH_NER_MODEL_NAME,
        revision: str = GRAPH_NER_MODEL_REVISION,
        providers: list[str] | None = None,
    ) -> None:
        self._model_name = model_name
        self._revision = revision
        # [graph].providers only — never the embedder's or reranker's own provider
        # list. Unset (None/[]) means CPU.
        self._providers = providers or None
        self._model: GLiNER | None = None
        self._sync_lock = threading.Lock()
        self._load_lock = asyncio.Lock()
        self._load_exception: BaseException | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _resolve_device(self) -> str:
        """Map [graph].providers onto a torch device: cpu/cuda/mps.

        Recognises this repo's real ONNX provider-string vocabulary
        (``CUDAExecutionProvider``, ``CoreMLExecutionProvider``, ...), not
        literal "cuda"/"mps". Steps down to CPU — logging a WARNING rather than
        raising — when the resolved device is unavailable at runtime, so a
        stale/misconfigured providers list cannot latch a permanent failure.
        """
        if not self._providers:
            return "cpu"
        first = self._providers[0].lower()
        if _CUDA_PROVIDER_MARKER in first:
            import torch  # noqa: PLC0415 — lazy; not installed at import time

            if torch.cuda.is_available():
                return "cuda"
            logger.warning(
                "[graph].providers=%r requests CUDA but torch.cuda.is_available() "
                "is False — falling back to CPU.",
                self._providers,
            )
            return "cpu"
        if _MPS_PROVIDER_MARKER in first:
            import torch  # noqa: PLC0415 — lazy; not installed at import time

            if torch.backends.mps.is_available():
                return "mps"
            logger.warning(
                "[graph].providers=%r requests CoreML/MPS but "
                "torch.backends.mps.is_available() is False — falling back to CPU.",
                self._providers,
            )
            return "cpu"
        return "cpu"

    def _load_sync(self) -> None:
        """Blocking model construction — runs off the event loop via asyncio.to_thread."""
        if self._model is not None:
            return
        with self._sync_lock:
            if self._model is not None:
                return
            # Deferred: gliner/torch/transformers must never load at module import
            # time, so create_app() stays cheap (BE-8's contract) — these load only
            # on the first real load(), off the event loop.
            import torch  # noqa: PLC0415 — lazy; not installed at import time
            from gliner import GLiNER  # noqa: PLC0415 — lazy; not installed at import time

            # Thread pinning is best-effort, not fatal to the load: torch raises
            # RuntimeError if either setter is called more than once per process
            # (e.g. a second ProseExtractionBackend instance, or a retried load
            # after CancelledError) — that must never poison the load-exception
            # latch below.
            try:
                torch.set_num_threads(_TORCH_INTRA_OP_THREADS)
                torch.set_num_interop_threads(_TORCH_INTER_OP_THREADS)
            except RuntimeError as exc:
                logger.debug("torch thread pinning skipped (already set): %s", exc)

            model = GLiNER.from_pretrained(
                self._model_name,
                revision=self._revision,
                cache_dir=str(get_graph_models_dir()),
            )
            self._model = model.to(self._resolve_device())

    async def load(self) -> None:
        """Load the model, off the event loop, on first call.

        Idempotent and single-flight: concurrent callers share one in-flight
        load rather than each triggering their own. A failed load latches —
        every subsequent call re-raises the same exception without retrying
        — except ``asyncio.CancelledError``, which never latches, so a
        cancelled caller's successor gets a genuine retry.
        """
        if self._model is not None:
            return
        if self._load_exception is not None:
            raise self._load_exception
        async with self._load_lock:
            if self._model is not None:
                return
            if self._load_exception is not None:
                raise self._load_exception
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._load_sync), timeout=_LOAD_TIMEOUT_SECONDS
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load graph NER model %r: %s", self._model_name, exc
                )
                self._load_exception = exc
                raise
            # asyncio.CancelledError is a BaseException, not Exception, in
            # Python 3.8+, so it propagates here without being latched above.

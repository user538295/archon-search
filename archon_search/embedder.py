"""Embedding layer for RAG — a PyTorch encoder backend with async support."""
from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from archon_search.model_specs import get_embedder_spec
from archon_search.model_store import ensure_model
from archon_search.observability import record_stage
from archon_search.text_batching import DEFAULT_TOKEN_BUDGET, plan_batches
from archon_search.torch_runtime import (
    AUTO_DEVICE,
    accelerator_lock,
    load_encoder,
    pad_encodings,
    release_accelerator_cache,
    resolve_device,
)

if TYPE_CHECKING:
    from tokenizers import Encoding, Tokenizer
    from transformers import PreTrainedModel

    from archon_search.model_specs import Pooling

_DEFAULT_PAD_TOKEN_ID = 0
_WARMUP_TEXT = "warmup"
_WARMUP_TIMEOUT_SECONDS = 300.0


class EmbedderWarmupTimeout(Exception):
    """The warm-up build outran ``_WARMUP_TIMEOUT_SECONDS`` (the wait stopped, not the build)."""


@runtime_checkable
class EmbedderBackend(Protocol):
    model_name: str

    @property
    def is_warm(self) -> bool: ...

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class _Encoder(NamedTuple):
    """A loaded model with everything ``encode`` needs to drive it."""

    tokenizer: Tokenizer
    model: PreTrainedModel
    device: str
    pad_token_id: int
    uses_token_type_ids: bool


class ModelEmbedder:
    """Lazy-loading PyTorch embedding backend.

    The spec is resolved eagerly, so an unsupported *model_name* fails at
    construction. The weights are fetched (``ensure_model``) and loaded onto the
    device resolved from *device* (``torch_runtime.resolve_device``) on the first
    non-empty ``encode``; a failed load is not latched, the next call retries.
    ``encode`` is safe to call from several threads.

    Raises:
        UnsupportedModelError: from the constructor, for a model outside the registry.
    """

    def __init__(self, model_name: str, device: str = AUTO_DEVICE) -> None:
        self.model_name = model_name
        self._spec = get_embedder_spec(model_name)
        self._device_setting = device
        self._encoder: _Encoder | None = None  # loaded on first encode()
        self._lock = threading.Lock()

    @property
    def is_warm(self) -> bool:
        return self._encoder is not None

    def _load(self) -> _Encoder:
        """Return the encoder, loading it once; concurrent callers wait on the lock."""
        if self._encoder is None:
            with self._lock:
                if self._encoder is None:
                    path = ensure_model(self._spec)
                    device = resolve_device(self._device_setting)
                    tokenizer, model = load_encoder(self._spec, path, device)
                    self._encoder = _Encoder(
                        tokenizer,
                        model,
                        device,
                        getattr(model.config, "pad_token_id", None) or _DEFAULT_PAD_TOKEN_ID,
                        getattr(model.config, "type_vocab_size", 1) > 1,
                    )
        return self._encoder

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*; the result is in input order, one ``spec.dim`` vector per text."""
        if not texts:
            return []
        encoder = self._load()
        encodings = [encoder.tokenizer.encode(text) for text in texts]
        vectors: list[list[float]] = [[] for _ in texts]
        for batch in plan_batches([len(e.ids) for e in encodings], token_budget=DEFAULT_TOKEN_BUDGET):
            batch_encodings = [encodings[i] for i in batch]
            for index, vector in zip(batch, self._embed_batch(encoder, batch_encodings), strict=True):
                vectors[index] = vector
            if len(batch) * max(len(e.ids) for e in batch_encodings) > DEFAULT_TOKEN_BUDGET:
                release_accelerator_cache(encoder.device)
        return vectors

    def _embed_batch(self, encoder: _Encoder, encodings: list[Encoding]) -> list[list[float]]:
        import torch  # noqa: PLC0415 — lazy: keeps ``import archon_search.embedder`` free of PyTorch

        with accelerator_lock(encoder.device):
            inputs = pad_encodings(
                torch,
                encodings,
                pad_token_id=encoder.pad_token_id,
                device=encoder.device,
                with_token_types=encoder.uses_token_type_ids,
            )
            with torch.inference_mode():
                hidden = encoder.model(**inputs).last_hidden_state
                pooled = _pool(torch, hidden, inputs["attention_mask"], self._spec.pooling)
                if self._spec.normalize:
                    pooled = torch.nn.functional.normalize(pooled, dim=-1)
            return pooled.cpu().tolist()  # type: ignore[no-any-return]


def _pool(torch: Any, hidden: Any, attention_mask: Any, pooling: Pooling) -> Any:
    """Reduce ``(batch, tokens, dim)`` hidden states to ``(batch, dim)``.

    ``last_token`` relies on ``pad_encodings`` right-padding: row ``i`` ends at ``attention_mask[i].sum() - 1``.
    """
    if pooling == "cls":
        return hidden[:, 0]
    if pooling == "last_token":
        last = attention_mask.sum(dim=1) - 1
        return hidden[torch.arange(hidden.shape[0], device=hidden.device), last]
    mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
    return (hidden * mask).sum(dim=1) / mask.sum(dim=1)


class Embedder:
    """Async wrapper around an EmbedderBackend."""

    def __init__(self, backend: EmbedderBackend) -> None:
        self._backend = backend
        self._embedding_dim: int | None = None
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
    def model_name(self) -> str:
        return getattr(self._backend, "model_name", "")

    @property
    def is_warm(self) -> bool:
        return self._backend.is_warm

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
        build = asyncio.ensure_future(self.embed([_WARMUP_TEXT]))
        try:
            await asyncio.wait_for(build, timeout=_WARMUP_TIMEOUT_SECONDS)
            self._warmup_failed = False
        except asyncio.TimeoutError as exc:
            self._warmup_failed = True
            # Since 3.11 asyncio.TimeoutError *is* the builtin TimeoutError, so a
            # socket ETIMEDOUT raised inside the build arrives here looking exactly
            # like the ceiling expiring. Only the ceiling cancels the build.
            if not build.cancelled():
                raise
            raise EmbedderWarmupTimeout(
                f"embedder warm-up exceeded {_WARMUP_TIMEOUT_SECONDS}s"
            ) from exc
        except Exception:
            self._warmup_failed = True
            raise

    async def warmup(self) -> None:
        """Load the backend's model now, off the request path.

        ``ModelEmbedder`` loads its model on the *first* ``encode``
        call. Callers must run this outside any request timeout budget,
        otherwise the one-off load consumes the whole budget and an otherwise
        valid search fails with 504 (S184). Contract:

        - Idempotent, and a no-op once warm.
        - Single-flight: one shared task does the build; callers only
          ``await asyncio.shield`` it, so a caller that bounds its own wait and
          gives up cancels nothing but that wait. The build keeps running, warms
          the backend for later requests, and later callers join it instead of
          each starting another ``to_thread`` encode that would queue behind the
          one model build on ``ModelEmbedder._lock`` — a pinned executor thread
          per concurrent cold request (S290).
        - Not latched on failure (S283): ``_warmup_failed`` records only the most
          recent attempt and is diagnostic; the next caller retries with a fresh
          task, since a settled task is never re-awaited.
        - Raises ``EmbedderWarmupTimeout`` if the build outruns
          ``_WARMUP_TIMEOUT_SECONDS``. That stops the *waiting*, not the build:
          ``asyncio.to_thread`` workers are not cancellable, so the thread runs
          to completion (and, being non-daemon, delays executor shutdown).
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode texts in a thread pool; lazily initialises embedding_dim."""
        with record_stage("embed"):
            result: list[list[float]] = await asyncio.to_thread(self._backend.encode, texts)
        if self._embedding_dim is None and result:
            self._embedding_dim = len(result[0])
        return result

    async def embed_one(self, text: str) -> list[float]:
        results = await self.embed([text])
        return results[0]


def make_embedder(model_name: str, device: str = AUTO_DEVICE) -> Embedder:
    """Factory: create a ModelEmbedder-backed Embedder (no weights are loaded yet)."""
    return Embedder(ModelEmbedder(model_name, device=device))

"""Embedding layer for RAG — a PyTorch encoder backend with async support."""
from __future__ import annotations

import asyncio
import threading
from types import ModuleType
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

from archon_search.model_specs import UnsupportedModelError, get_embedder_spec
from archon_search.model_store import ensure_model
from archon_search.observability import record_stage
from archon_search.text_batching import map_in_token_batches
from archon_search.torch_runtime import (
    AUTO_DEVICE,
    DEFAULT_PAD_TOKEN_ID,
    accelerator_lock,
    load_encoder,
    pad_encodings,
    release_accelerator_cache,
    resolve_device,
)
from archon_search.warmup_coordinator import WarmupCoordinator, WarmupTimeout

if TYPE_CHECKING:
    from tokenizers import Encoding, Tokenizer
    from torch import Tensor
    from transformers import PreTrainedModel

    from archon_search.model_specs import Pooling

# Trivial text used only to force a cold backend to load.
WARMUP_TEXT = "warmup"


class EmbedderWarmupTimeout(WarmupTimeout):
    """The embedder warm-up build outran ``WARMUP_TIMEOUT_SECONDS`` (the wait stopped, not the build)."""


@runtime_checkable
class EmbedderBackend(Protocol):
    """Turns texts into vectors; may load its weights lazily.

    ``encode`` returns one ``spec.dim`` vector per text, in input order. It is called from worker
    threads, possibly several at once, so an implementation must be thread-safe. ``is_warm`` is
    ``True`` once the weights are loaded, so ``encode`` no longer pays the load.
    """

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
                        getattr(model.config, "pad_token_id", None) or DEFAULT_PAD_TOKEN_ID,
                        getattr(model.config, "type_vocab_size", 1) > 1,
                    )
        return self._encoder

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*; the result is in input order, one ``spec.dim`` vector per text."""
        if not texts:
            return []
        encoder = self._load()
        encodings = [encoder.tokenizer.encode(text) for text in texts]
        return map_in_token_batches(
            [len(encoding.ids) for encoding in encodings],
            lambda batch: self._embed_batch(encoder, [encodings[index] for index in batch]),
            release_memory=lambda: release_accelerator_cache(encoder.device),
        )

    def _embed_batch(self, encoder: _Encoder, encodings: list[Encoding]) -> list[list[float]]:
        import torch  # lazy: keeps ``import archon_search.embedder`` free of PyTorch

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
            vectors: list[list[float]] = pooled.cpu().tolist()
            return vectors


class UnsupportedModelEmbedder:
    """Backend for a model name outside the registry: stays cold and fails every ``encode``.

    Lets the server start with such a name and report ``checks.models == "fail"`` instead of
    aborting; ``encode`` raises the ``UnsupportedModelError`` that ``ModelEmbedder`` raised at construction.
    """

    def __init__(self, error: UnsupportedModelError) -> None:
        self.model_name = error.name
        self._supported = error.supported

    @property
    def is_warm(self) -> bool:
        return False

    def encode(self, texts: list[str]) -> list[list[float]]:
        # A fresh error per call: re-raising one instance would grow its ``__traceback__`` on every encode.
        # (Unlike ``ModelEmbedder.encode``, an empty batch fails too: nothing here can ever succeed.)
        raise UnsupportedModelError(self.model_name, self._supported)


def _pool(torch: ModuleType, hidden: Tensor, attention_mask: Tensor, pooling: Pooling) -> Tensor:
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
        self._warmup_coordinator = WarmupCoordinator(
            subject="embedder",
            is_warm=lambda: self.is_warm,
            build=lambda: self.embed([WARMUP_TEXT]),
            timeout_error=EmbedderWarmupTimeout,
        )

    @property
    def backend(self) -> EmbedderBackend:
        """The wrapped backend — what a startup probe runs so it shares this instance's weights."""
        return self._backend

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

    async def warmup(self) -> None:
        """Load the backend's model now, off the request path (see ``WarmupCoordinator.warmup``).

        ``ModelEmbedder`` loads its model on the *first* ``encode`` call, so a request that
        triggers it spends its whole timeout budget on the load and fails with 504. The build
        goes through ``embed``, so it also caches ``embedding_dim``.

        Raises:
            EmbedderWarmupTimeout: if the build outruns ``WARMUP_TIMEOUT_SECONDS``.
        """
        await self._warmup_coordinator.warmup()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode texts in a thread pool; lazily initialises embedding_dim."""
        with record_stage("embed"):
            result: list[list[float]] = await asyncio.to_thread(self._backend.encode, texts)
        if self._embedding_dim is None and result:
            self._embedding_dim = len(result[0])
        return result

    async def embed_one(self, text: str) -> list[float]:
        """Return the ``spec.dim`` vector of one *text*.

        Goes through ``embed``, so it also caches ``embedding_dim`` on first use. Raises whatever the
        backend raises (an unsupported or unloadable model, an inference failure).
        """
        results = await self.embed([text])
        return results[0]


def make_embedder(model_name: str, device: str = AUTO_DEVICE) -> Embedder:
    """Factory: create a ModelEmbedder-backed Embedder (no weights are loaded yet)."""
    return Embedder(ModelEmbedder(model_name, device=device))

"""Embedding layer for RAG — wraps fastembed.TextEmbedding with async support."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol, runtime_checkable

from archon_search.observability import record_stage
from archon_search.paths import get_models_dir

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


class ModelEmbedder:
    """Lazy-loading fastembed TextEmbedding backend."""

    def __init__(self, model_name: str, providers: list[str] | None = None) -> None:
        self.model_name = model_name
        self._providers = providers or None  # None = CPU default in fastembed
        self._model: Any = None  # loaded on first encode()
        self._lock = threading.Lock()

    @property
    def is_warm(self) -> bool:
        return self._model is not None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding  # noqa: PLC0415

                    self._model = TextEmbedding(
                        self.model_name,
                        providers=self._providers,
                        cache_dir=str(get_models_dir()),
                    )
        # TextEmbedding.embed() returns a generator of 1-D numpy arrays
        return [e.tolist() for e in self._model.embed(texts)]


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
        """Load the backend's ONNX model now, off the request path.

        ``ModelEmbedder`` constructs its ONNX model on the *first* ``encode``
        call. Callers must run this outside any request timeout budget,
        otherwise the one-off load consumes the whole budget and an otherwise
        valid search fails with 504 (S184). Contract:

        - Idempotent, and a no-op once warm.
        - Single-flight: one shared task does the build; callers only
          ``await asyncio.shield`` it, so a caller that bounds its own wait and
          gives up cancels nothing but that wait. The build keeps running, warms
          the backend for later requests, and later callers join it instead of
          each starting another ``to_thread`` encode that would queue behind the
          one ONNX build on ``ModelEmbedder._lock`` — a pinned executor thread
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


def make_embedder(model_name: str, providers: list[str] | None = None) -> Embedder:
    """Factory: create a ModelEmbedder-backed Embedder."""
    return Embedder(ModelEmbedder(model_name, providers=providers))

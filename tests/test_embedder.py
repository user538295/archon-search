"""packages/archon-search/tests/test_embedder.py — unit tests for Embedder (fastembed backend)."""
from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from archon_search.embedder import (
    Embedder,
    EmbedderBackend,
    EmbedderWarmupTimeout,
    make_embedder,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockBackend:
    """Deterministic test backend — returns [float(i)] * dim vectors."""

    model_name: str = "mock-backend"
    is_warm: bool = False

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.call_count = 0
        self.called_from_threads: list[str] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.called_from_threads.append(threading.current_thread().name)
        return [[float(i)] * self.dim for i in range(len(texts))]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedder_mock_backend_returns_correct_shape() -> None:
    """MockEmbedder(dim=4) → embed(['a','b']) returns 2×4 list."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    result = await embedder.embed(["a", "b"])
    assert len(result) == 2
    assert all(len(v) == 4 for v in result)


@pytest.mark.asyncio
async def test_embedder_embed_one_returns_single_vector() -> None:
    backend = _MockBackend(dim=3)
    embedder = Embedder(backend)
    vec = await embedder.embed_one("hello")
    assert isinstance(vec, list)
    assert len(vec) == 3


@pytest.mark.asyncio
async def test_embedder_embedding_dim_raises_before_embed() -> None:
    """Accessing embedding_dim before any embed() call raises RuntimeError."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    with pytest.raises(RuntimeError, match="not yet initialized"):
        _ = embedder.embedding_dim


@pytest.mark.asyncio
async def test_embedder_embedding_dim_cached() -> None:
    """After embed(['a']), embedding_dim is cached; second access doesn't call backend again."""
    backend = _MockBackend(dim=8)
    embedder = Embedder(backend)
    await embedder.embed(["a"])
    assert embedder.embedding_dim == 8
    # Access embedding_dim again — should NOT trigger another backend call
    calls_before = backend.call_count
    _ = embedder.embedding_dim
    assert backend.call_count == calls_before  # no extra encode() called


@pytest.mark.asyncio
async def test_embedder_uses_to_thread() -> None:
    """backend.encode is called from a worker thread (not the event loop thread)."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    main_thread = threading.current_thread().name
    await embedder.embed(["hello"])
    # All encode calls should be from a thread-pool worker
    assert all(t != main_thread for t in backend.called_from_threads)


@pytest.mark.asyncio
async def test_embedder_embedding_dim_matches_backend_dim() -> None:
    backend = _MockBackend(dim=16)
    embedder = Embedder(backend)
    await embedder.embed(["test"])
    assert embedder.embedding_dim == 16


def test_make_embedder_returns_embedder() -> None:
    """make_embedder returns an Embedder without downloading models."""
    e = make_embedder("BAAI/bge-small-en-v1.5", providers=[])
    assert isinstance(e, Embedder)


def test_make_embedder_with_empty_providers() -> None:
    """make_embedder(providers=[]) does not raise."""
    e = make_embedder("BAAI/bge-small-en-v1.5", providers=[])
    assert e is not None


def test_embedder_backend_protocol() -> None:
    """_MockBackend satisfies the EmbedderBackend protocol."""
    backend = _MockBackend()
    assert isinstance(backend, EmbedderBackend)


@pytest.mark.asyncio
async def test_embedder_embed_empty_list_leaves_dim_unset() -> None:
    """embed([]) returns [] and leaves embedding_dim unset (RuntimeError on access)."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    result = await embedder.embed([])
    assert result == []
    with pytest.raises(RuntimeError, match="not yet initialized"):
        _ = embedder.embedding_dim


# ===========================================================================
# Embedder error paths
# ===========================================================================


class _WrongCountBackend:
    """Returns one fewer vector than texts (simulates a broken backend)."""

    model_name: str = "wrong-count"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        # Return n-1 vectors to simulate a backend bug
        return [[0.0] * 4 for _ in range(max(0, len(texts) - 1))]


class _EmptyResultBackend:
    """Returns empty list for every encode call."""

    model_name: str = "empty-result"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return []


@pytest.mark.asyncio
async def test_P14_1_embedder_wrong_count_dim_set_from_truncated_result() -> None:
    """ backend returns fewer vectors than texts: embedding_dim is set from the first truncated result.

    The Embedder sets _embedding_dim only when result is non-empty, so if the
    backend silently returns fewer vectors than texts (but not empty), the
    caller receives the truncated list and embedding_dim is set from result[0].
    This test pins the current contract: wrong count → dim set from result[0],
    no error raised by Embedder itself (the caller bears responsibility).
    """
    backend = _WrongCountBackend()
    embedder = Embedder(backend)
    result = await embedder.embed(["a", "b", "c"])
    # Backend returns n-1 = 2 vectors; Embedder returns them as-is
    assert len(result) == 2
    # embedding_dim is set from result[0] (len=4)
    assert embedder.embedding_dim == 4


@pytest.mark.asyncio
async def test_P14_2_embedder_empty_result_dim_not_initialized() -> None:
    """ backend returns [] for non-empty texts: embedding_dim stays unset."""
    backend = _EmptyResultBackend()
    embedder = Embedder(backend)
    result = await embedder.embed(["hello", "world"])
    assert result == []
    with pytest.raises(RuntimeError, match="not yet initialized"):
        _ = embedder.embedding_dim


@pytest.mark.asyncio
async def test_P14_3_embedder_whitespace_only_text_still_embeds() -> None:
    """ whitespace-only text is passed through to backend without error."""
    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    result = await embedder.embed(["   ", "\t\n", ""])
    # Backend receives all three texts and returns 3 vectors
    assert len(result) == 3
    assert backend.call_count == 1


@pytest.mark.asyncio
async def test_P14_4_embedder_backend_exception_propagates() -> None:
    """ backend.encode raises → exception propagates from embed."""
    class _ExplodingBackend:
        model_name: str = "exploding"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            raise ValueError("backend exploded")

    backend = _ExplodingBackend()
    embedder = Embedder(backend)
    with pytest.raises(ValueError, match="backend exploded"):
        await embedder.embed(["text"])


def test_model_embedder_init_called_once_under_concurrent_encode() -> None:
    """Double-checked locking: concurrent encode() calls init the model exactly once."""
    import sys
    import time

    import numpy as np

    from archon_search.embedder import ModelEmbedder

    init_count = 0
    barrier = threading.Barrier(2)

    class _SlowTextEmbedding:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            nonlocal init_count
            init_count += 1
            time.sleep(0.05)  # slow enough to expose the race

        def embed(self, texts: list[str]):  # type: ignore[return]
            for _ in texts:
                yield np.zeros(4, dtype=np.float32)

    original = sys.modules["fastembed"].TextEmbedding
    sys.modules["fastembed"].TextEmbedding = _SlowTextEmbedding
    try:
        embedder = ModelEmbedder("BAAI/bge-small-en-v1.5")
        results: list[list[list[float]]] = []
        exceptions: list[Exception] = []

        def run_encode() -> None:
            barrier.wait()  # force simultaneous entry
            try:
                results.append(embedder.encode(["hello"]))
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=run_encode) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Unexpected exceptions: {exceptions}"
        assert len(results) == 2
        assert init_count == 1, f"Model __init__ called {init_count} times — lock missing"
    finally:
        sys.modules["fastembed"].TextEmbedding = original


# ---------------------------------------------------------------------------
# Stage recording tests — Task 3.1 (B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_records_stage_when_bound() -> None:
    """Embedder.embed records 'embed' stage timing when a recorder is bound."""
    from archon_search.observability import bind_stage_recorder

    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    with bind_stage_recorder() as recorder:
        await embedder.embed(["text"])
    assert "embed" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["embed"] >= 0


@pytest.mark.asyncio
async def test_embed_noop_when_unbound() -> None:
    """Embedder.embed works normally with no recorder bound."""
    from archon_search.observability import _stage_recorder

    backend = _MockBackend(dim=4)
    embedder = Embedder(backend)
    assert _stage_recorder.get() is None
    result = await embedder.embed(["text"])
    assert result is not None
    assert _stage_recorder.get() is None


# ---------------------------------------------------------------------------
# is_warm tests — Task 2.1 (B2)
# ---------------------------------------------------------------------------


def test_model_embedder_is_warm_false_before_encode() -> None:
    from archon_search.embedder import ModelEmbedder

    me = ModelEmbedder("some-model")
    assert me.is_warm is False
    assert me._model is None  # reading is_warm did not load the model


def test_model_embedder_is_warm_true_after_model_set() -> None:
    from archon_search.embedder import ModelEmbedder

    me = ModelEmbedder("some-model")
    me._model = object()
    assert me.is_warm is True


def test_embedder_is_warm_delegates_to_backend() -> None:
    backend = _MockBackend()
    backend.is_warm = False
    embedder = Embedder(backend)
    assert embedder.is_warm is False
    backend.is_warm = True
    assert embedder.is_warm is True


def test_reading_is_warm_does_not_construct_TextEmbedding() -> None:
    from unittest.mock import patch

    from archon_search.embedder import ModelEmbedder

    with patch("fastembed.TextEmbedding", side_effect=RuntimeError("should not be called")):
        me = ModelEmbedder("x")
        result = me.is_warm  # must not raise
    assert result is False


def test_reading_is_warm_does_not_acquire_lock() -> None:
    import time

    from archon_search.embedder import ModelEmbedder

    me = ModelEmbedder("x")
    lock_acquired = threading.Event()
    test_done = threading.Event()

    def hold_lock():
        with me._lock:
            lock_acquired.set()
            test_done.wait(timeout=5.0)

    t = threading.Thread(target=hold_lock)
    t.start()
    lock_acquired.wait(timeout=5.0)

    start = time.monotonic()
    result = me.is_warm  # must not block waiting for lock
    elapsed = time.monotonic() - start

    test_done.set()
    t.join()

    assert result is False
    assert elapsed < 0.1  # completed without waiting for lock


def test_mock_backend_satisfies_protocol_after_is_warm_added() -> None:
    assert isinstance(_MockBackend(), EmbedderBackend)


def test_model_embedder_is_warm_true_after_encode() -> None:
    """is_warm transitions False→True when encode() loads the model."""
    from unittest.mock import MagicMock, patch

    from archon_search.embedder import ModelEmbedder

    fake_embedding = MagicMock()
    fake_embedding.tolist.return_value = [0.1, 0.2]
    fake_model = MagicMock()
    fake_model.embed.return_value = [fake_embedding]

    me = ModelEmbedder("some-model")
    assert me.is_warm is False

    with patch("fastembed.TextEmbedding", return_value=fake_model):
        me.encode(["hello"])

    assert me.is_warm is True


def test_embedder_caches_models_under_the_archon_data_dir() -> None:
    """encode() must construct TextEmbedding with cache_dir=str(get_models_dir())."""
    from unittest.mock import MagicMock, patch

    from archon_search.embedder import ModelEmbedder
    from archon_search.paths import get_models_dir

    fake_embedding = MagicMock()
    fake_embedding.tolist.return_value = [0.1, 0.2]
    fake_model = MagicMock()
    fake_model.embed.return_value = [fake_embedding]

    me = ModelEmbedder("some-model")
    with patch("fastembed.TextEmbedding", return_value=fake_model) as mock_te:
        me.encode(["hello"])

    assert mock_te.call_args.kwargs["cache_dir"] == str(get_models_dir())


# ---------------------------------------------------------------------------
# C1-I-3 — single-flight warm-up (shared task + asyncio.shield), S290
#
# Mirrors ``tests/test_reranker.py``'s S288 block. Before this, ``warmup()``
# held an ``asyncio.Lock`` across the build, so a caller that bounded its wait
# cancelled the build itself: every later cold request then started another
# ``to_thread`` encode that blocked on ``ModelEmbedder._lock`` — one pinned
# executor thread per concurrent request — and the cancelled attempt never
# reached the ``_embedding_dim`` assignment.
# ---------------------------------------------------------------------------


class _BlockingEmbedderBackend:
    """Backend whose ``encode`` parks on a threading.Event until released."""

    model_name: str = "blocking-embedder"

    def __init__(self, *, hang: bool = False) -> None:
        self.is_warm = False
        self.build_count = 0
        self.hang = hang
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.build_count += 1
        self.started.set()
        self.release.wait(timeout=30.0 if self.hang else 5.0)
        self.is_warm = True
        return [[0.1] * 4 for _ in texts]


@pytest.mark.asyncio
async def test_concurrent_cold_embedder_warmups_trigger_exactly_one_build() -> None:
    """N concurrent cold ``warmup()`` callers share one backend build."""
    backend = _BlockingEmbedderBackend()
    embedder = Embedder(backend)  # type: ignore[arg-type]

    waiters = [asyncio.create_task(embedder.warmup()) for _ in range(8)]
    await asyncio.to_thread(backend.started.wait, 5.0)
    backend.release.set()
    await asyncio.gather(*waiters)

    assert backend.build_count == 1, (
        f"expected exactly one shared build, got {backend.build_count}"
    )
    assert embedder.is_warm is True


@pytest.mark.asyncio
async def test_abandoned_embedder_waiter_does_not_cancel_the_shared_build() -> None:
    """A caller that gives up on waiting must not kill the build (S290).

    This is what the bounded ``warmup_for_search`` wait does on every cold
    request: it must cancel only the wait, leave the build running, and let a
    later caller join it rather than start a second ONNX build.
    """
    backend = _BlockingEmbedderBackend()
    embedder = Embedder(backend)  # type: ignore[arg-type]

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(embedder.warmup(), timeout=0.01)
    await asyncio.to_thread(backend.started.wait, 5.0)
    assert backend.build_count == 1
    assert embedder._warmup_task is not None, "the abandoned wait cancelled the build"

    joiner = asyncio.create_task(embedder.warmup())
    await asyncio.sleep(0)
    backend.release.set()
    await joiner

    assert backend.build_count == 1, (
        f"the second caller started a duplicate build ({backend.build_count} total)"
    )
    assert embedder.is_warm is True


@pytest.mark.asyncio
async def test_shared_build_caches_the_embedding_dim_for_an_abandoned_waiter() -> None:
    """The surviving build still reaches the ``_embedding_dim`` assignment.

    A cancelled ``warmup()`` never returned from ``embed()``, so the dimension
    stayed uncached and the next caller to need it raised RuntimeError.
    """
    backend = _BlockingEmbedderBackend()
    embedder = Embedder(backend)  # type: ignore[arg-type]

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(embedder.warmup(), timeout=0.01)
    await asyncio.to_thread(backend.started.wait, 5.0)
    backend.release.set()
    await embedder.warmup()

    assert embedder.embedding_dim == 4


@pytest.mark.asyncio
async def test_embedder_warmup_is_a_noop_once_warm() -> None:
    """Idempotent: a warm backend is never rebuilt."""
    backend = _BlockingEmbedderBackend()
    backend.release.set()
    embedder = Embedder(backend)  # type: ignore[arg-type]

    await embedder.warmup()
    await embedder.warmup()

    assert backend.build_count == 1


@pytest.mark.asyncio
async def test_failed_embedder_warmup_is_retried_by_the_next_caller() -> None:
    """S283 semantics: ``_warmup_failed`` records the last attempt, it does not latch.

    It previously gated ``warmup()`` itself, so one transient failure (HF 429,
    network blip, unwritable cache) disarmed every later warm-up for the life of
    the process — and, since S290, pinned every later search to its FTS leg.
    """

    class _FailOnceBackend:
        model_name: str = "fail-once"

        def __init__(self) -> None:
            self.is_warm = False
            self.build_count = 0

        def encode(self, texts: list[str]) -> list[list[float]]:
            self.build_count += 1
            if self.build_count == 1:
                raise RuntimeError("transient failure")
            self.is_warm = True
            return [[0.1] * 4 for _ in texts]

    backend = _FailOnceBackend()
    embedder = Embedder(backend)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="transient failure"):
        await embedder.warmup()
    assert embedder._warmup_failed is True
    assert embedder.is_warm is False

    await embedder.warmup()

    assert backend.build_count == 2, "the failed warm-up was not retried"
    assert embedder.is_warm is True
    assert embedder._warmup_failed is False


@pytest.mark.asyncio
async def test_embedder_warmup_raises_embedderwarmuptimeout_not_asyncio_timeouterror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-long build raises ``EmbedderWarmupTimeout``, a plain ``Exception``.

    It must NOT be an ``asyncio.TimeoutError``: since 3.11 that is the builtin
    ``TimeoutError`` (an ``OSError``), which callers use to mean "I gave up
    waiting" — a different condition from "the build blew its own ceiling", and
    one ``warmup_models`` must report differently (C1-I-2).
    """
    import archon_search.embedder as embedder_module

    monkeypatch.setattr(embedder_module, "_WARMUP_TIMEOUT_SECONDS", 0.01)
    backend = _BlockingEmbedderBackend(hang=True)
    embedder = Embedder(backend)  # type: ignore[arg-type]

    try:
        with pytest.raises(EmbedderWarmupTimeout):
            await embedder.warmup()
    finally:
        backend.release.set()

    assert not issubclass(EmbedderWarmupTimeout, asyncio.TimeoutError)
    assert not issubclass(EmbedderWarmupTimeout, OSError)
    assert embedder._warmup_failed is True

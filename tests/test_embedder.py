"""Unit tests for Embedder (async wrapper) and ModelEmbedder (PyTorch backend, tiny synthetic BERT)."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import _tiny_encoder as tiny_encoder
import numpy as np
import pytest

import archon_search.embedder as embedder_module
from archon_search.embedder import (
    Embedder,
    EmbedderBackend,
    EmbedderWarmupTimeout,
    ModelEmbedder,
    make_embedder,
)
from archon_search.model_specs import UnsupportedModelError
from archon_search.model_store import ModelDownloadError

pytestmark = pytest.mark.real_torch_backends


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


def test_embedder_is_warm_delegates_to_backend() -> None:
    backend = _MockBackend()
    backend.is_warm = False
    embedder = Embedder(backend)
    assert embedder.is_warm is False
    backend.is_warm = True
    assert embedder.is_warm is True


def test_mock_backend_satisfies_protocol_after_is_warm_added() -> None:
    assert isinstance(_MockBackend(), EmbedderBackend)


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


# ---------------------------------------------------------------------------
# ModelEmbedder — PyTorch backend on a tiny synthetic BERT (no weights, no network)
# ---------------------------------------------------------------------------

_TOLERANCE = 1e-5
_UNIT_NORM_TOLERANCE = 1e-5


@pytest.fixture(scope="module")
def tiny_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tiny_encoder.build_snapshot(tmp_path_factory.mktemp("tiny-bert"))


@pytest.fixture
def use_tiny_model(tiny_snapshot: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., MagicMock]:
    """Point ``ModelEmbedder`` at the tiny snapshot; returns the ``ensure_model`` mock."""
    ensure = MagicMock(return_value=tiny_snapshot)

    def install(**spec_options: Any) -> MagicMock:
        monkeypatch.setattr(embedder_module, "get_embedder_spec", lambda _name: tiny_encoder.tiny_spec(**spec_options))
        monkeypatch.setattr(embedder_module, "ensure_model", ensure)
        return ensure

    return install


def _reference_vector(snapshot: Path, text: str, pooling: str) -> np.ndarray:
    """Un-normalized pooled vector of *text* from a plain ``AutoModel`` run on hand-built ids."""
    import torch
    from transformers import AutoModel

    model = AutoModel.from_pretrained(snapshot, dtype=torch.float32).eval()
    with torch.inference_mode():
        hidden = model(input_ids=torch.tensor([tiny_encoder.token_ids(text)])).last_hidden_state[0]
    pooled = {"cls": hidden[0], "mean": hidden.mean(dim=0), "last_token": hidden[-1]}[pooling]
    return pooled.numpy()


def _embed(texts: list[str]) -> np.ndarray:
    return np.array(ModelEmbedder("tiny/bert", device="cpu").encode(texts))


def test_make_embedder_returns_an_embedder_without_loading_weights() -> None:
    embedder = make_embedder("BAAI/bge-small-en-v1.5", device="cpu")
    assert isinstance(embedder, Embedder)
    assert embedder.is_warm is False


def test_an_unsupported_model_raises_at_construction() -> None:
    with pytest.raises(UnsupportedModelError, match="no-such-model"):
        ModelEmbedder("no-such-model")


def test_empty_input_returns_empty_list_without_loading(use_tiny_model: Callable[..., MagicMock]) -> None:
    ensure = use_tiny_model()
    embedder = ModelEmbedder("tiny/bert", device="cpu")

    assert embedder.encode([]) == []
    ensure.assert_not_called()
    assert embedder.is_warm is False


def test_lazy_load_and_is_warm(use_tiny_model: Callable[..., MagicMock]) -> None:
    ensure = use_tiny_model()
    embedder = ModelEmbedder("tiny/bert", device="cpu")
    assert embedder.is_warm is False
    ensure.assert_not_called()

    embedder.encode(["w1 w2"])
    embedder.encode(["w3"])

    assert embedder.is_warm is True
    ensure.assert_called_once()


def test_load_failure_is_not_latched(tiny_snapshot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedder_module, "get_embedder_spec", lambda _name: tiny_encoder.tiny_spec())
    ensure = MagicMock(side_effect=[ModelDownloadError("tiny/bert", "offline"), tiny_snapshot])
    monkeypatch.setattr(embedder_module, "ensure_model", ensure)
    embedder = ModelEmbedder("tiny/bert", device="cpu")

    with pytest.raises(ModelDownloadError):
        embedder.encode(["w1"])
    assert embedder.is_warm is False

    assert len(embedder.encode(["w1"])) == 1
    assert embedder.is_warm is True


def test_concurrent_cold_encodes_load_the_model_once(
    tiny_snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = 0
    barrier = threading.Barrier(2)

    def slow_ensure(_spec: object) -> Path:
        nonlocal loads
        loads += 1
        time.sleep(0.05)  # long enough to expose a missing lock
        return tiny_snapshot

    monkeypatch.setattr(embedder_module, "get_embedder_spec", lambda _name: tiny_encoder.tiny_spec())
    monkeypatch.setattr(embedder_module, "ensure_model", slow_ensure)
    embedder = ModelEmbedder("tiny/bert", device="cpu")
    results: list[list[list[float]]] = []

    def run() -> None:
        barrier.wait()
        results.append(embedder.encode(["w1 w2"]))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert loads == 1
    assert len(results) == 2
    assert results[0] == results[1]


def test_reading_is_warm_does_not_wait_for_a_load_in_progress(
    tiny_snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking_ensure(_spec: object) -> Path:
        entered.set()
        release.wait(timeout=5.0)
        return tiny_snapshot

    monkeypatch.setattr(embedder_module, "get_embedder_spec", lambda _name: tiny_encoder.tiny_spec())
    monkeypatch.setattr(embedder_module, "ensure_model", blocking_ensure)
    embedder = ModelEmbedder("tiny/bert", device="cpu")
    loader = threading.Thread(target=embedder.encode, args=(["w1"],))
    loader.start()
    try:
        assert entered.wait(timeout=5.0)
        started = time.monotonic()
        warm = embedder.is_warm
        elapsed = time.monotonic() - started
    finally:
        release.set()
        loader.join()

    assert warm is False
    assert elapsed < 0.1
    assert embedder.is_warm is True


@pytest.mark.parametrize("pooling", ["cls", "mean", "last_token"])
def test_pooling_matches_a_plain_automodel_run(
    pooling: str, use_tiny_model: Callable[..., MagicMock], tiny_snapshot: Path
) -> None:
    use_tiny_model(pooling=pooling, normalize=False)
    text = "w1 w2 w3 w4"

    vector = _embed([text])[0]

    np.testing.assert_allclose(vector, _reference_vector(tiny_snapshot, text, pooling), atol=_TOLERANCE)


def test_cls_and_mean_pooling_differ(use_tiny_model: Callable[..., MagicMock]) -> None:
    use_tiny_model(pooling="cls", normalize=False)
    cls_vector = _embed(["w1 w2 w3 w4"])[0]
    use_tiny_model(pooling="mean", normalize=False)
    mean_vector = _embed(["w1 w2 w3 w4"])[0]

    assert not np.allclose(cls_vector, mean_vector, atol=1e-3)


def test_last_token_pooling_differs_from_cls_and_mean(use_tiny_model: Callable[..., MagicMock]) -> None:
    vectors = {}
    for pooling in ("cls", "mean", "last_token"):
        use_tiny_model(pooling=pooling, normalize=False)
        vectors[pooling] = _embed(["w1 w2 w3 w4"])[0]

    assert not np.allclose(vectors["last_token"], vectors["cls"], atol=1e-3)
    assert not np.allclose(vectors["last_token"], vectors["mean"], atol=1e-3)


def test_last_token_pooling_picks_each_rows_last_real_token_in_a_padded_batch(
    use_tiny_model: Callable[..., MagicMock], tiny_snapshot: Path
) -> None:
    use_tiny_model(pooling="last_token", normalize=False)
    texts = ["w1", "w2 w3 w4 w5 w6 w7", "w8 w9", "w10 w11 w12"]  # one batch: rows padded to 8 tokens

    batched = _embed(texts)

    for text, vector in zip(texts, batched, strict=True):
        np.testing.assert_allclose(vector, _reference_vector(tiny_snapshot, text, "last_token"), atol=_TOLERANCE)


def test_normalize_only_when_spec_says_so(use_tiny_model: Callable[..., MagicMock], tiny_snapshot: Path) -> None:
    use_tiny_model(normalize=True)
    normalized = _embed(["w1 w2 w3"])[0]
    use_tiny_model(normalize=False)
    raw = _embed(["w1 w2 w3"])[0]

    assert np.linalg.norm(normalized) == pytest.approx(1.0, abs=_UNIT_NORM_TOLERANCE)
    assert np.linalg.norm(raw) == pytest.approx(np.linalg.norm(_reference_vector(tiny_snapshot, "w1 w2 w3", "cls")), abs=1e-4)
    assert np.linalg.norm(raw) != pytest.approx(1.0, abs=1e-2)
    np.testing.assert_allclose(normalized, raw / np.linalg.norm(raw), atol=_TOLERANCE)


def test_text_longer_than_max_length_is_truncated(use_tiny_model: Callable[..., MagicMock]) -> None:
    use_tiny_model(max_length=6)  # [CLS] + 4 words + [SEP]
    long_text = " ".join(f"w{i}" for i in range(1, 15))

    truncated, four_words = _embed([long_text, "w1 w2 w3 w4"])
    use_tiny_model(max_length=tiny_encoder.MAX_POSITIONS)
    untruncated = _embed([long_text])[0]

    np.testing.assert_allclose(truncated, four_words, atol=_TOLERANCE)
    assert not np.allclose(truncated, untruncated, atol=1e-3)


def test_padding_does_not_change_a_vector(use_tiny_model: Callable[..., MagicMock]) -> None:
    use_tiny_model(pooling="mean")
    texts = ["w1", "w2 w3 w4 w5 w6 w7", "w8 w9", "w10 w11 w12"]

    batched = _embed(texts)
    one_by_one = np.array([_embed([text])[0] for text in texts])

    np.testing.assert_allclose(batched, one_by_one, atol=_TOLERANCE)


def test_output_order_matches_input_order_across_batches(
    use_tiny_model: Callable[..., MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_tiny_model()
    budget = 8
    monkeypatch.setattr(embedder_module, "DEFAULT_TOKEN_BUDGET", budget)
    # Word counts 1..7 -> 3..9 tokens, scrambled: the sorted-by-length plan cannot be the identity.
    word_counts = [5, 1, 7, 3, 6, 2, 4]
    texts = [" ".join(f"w{i}" for i in range(1, count + 1)) for count in word_counts]
    plans: list[list[list[int]]] = []
    real_plan = embedder_module.plan_batches
    monkeypatch.setattr(
        embedder_module, "plan_batches", lambda *a, **k: plans.append(real_plan(*a, **k)) or plans[-1]
    )

    batched = _embed(texts)

    assert len(plans[0]) > 1
    assert [i for batch in plans[0] for i in batch] != list(range(len(texts)))
    monkeypatch.setattr(embedder_module, "DEFAULT_TOKEN_BUDGET", 4096)
    for text, vector in zip(texts, batched, strict=True):
        np.testing.assert_allclose(vector, _embed([text])[0], atol=_TOLERANCE)


def test_accelerator_cache_is_released_only_after_an_oversize_batch(
    use_tiny_model: Callable[..., MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_tiny_model()
    monkeypatch.setattr(embedder_module, "DEFAULT_TOKEN_BUDGET", 8)
    release = MagicMock()
    monkeypatch.setattr(embedder_module, "release_accelerator_cache", release)
    embedder = ModelEmbedder("tiny/bert", device="cpu")

    embedder.encode(["w1", "w2", "w3", "w4"])  # 3 tokens each: batches of two, 6 <= 8
    release.assert_not_called()

    embedder.encode(["w1 w2 w3 w4 w5 w6 w7 w8"])  # 10 tokens alone: padded 10 > 8
    release.assert_called_once_with("cpu")


def test_values_are_python_floats(use_tiny_model: Callable[..., MagicMock]) -> None:
    use_tiny_model()

    vectors = ModelEmbedder("tiny/bert", device="cpu").encode(["w1 w2"])

    assert len(vectors[0]) == tiny_encoder.HIDDEN_SIZE
    assert all(type(value) is float for value in vectors[0])


_DECODER_INTERMEDIATE_SIZE = 64
_DECODER_ATTENTION_HEADS = 4
_DECODER_KEY_VALUE_HEADS = 2
_DECODER_HEAD_DIM = tiny_encoder.HIDDEN_SIZE // _DECODER_ATTENTION_HEADS


@pytest.fixture(scope="module")
def tiny_decoder_snapshot(tiny_snapshot: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny Qwen3 (causal decoder) with the tiny tokenizer: its config has no ``type_vocab_size`` nor ``pad_token_id``."""
    import shutil

    import torch
    from transformers import Qwen3Config, Qwen3Model

    directory = tmp_path_factory.mktemp("tiny-qwen3")
    torch.manual_seed(tiny_encoder.SEED)
    config = Qwen3Config(
        vocab_size=len(tiny_encoder.VOCAB),
        hidden_size=tiny_encoder.HIDDEN_SIZE,
        intermediate_size=_DECODER_INTERMEDIATE_SIZE,
        num_hidden_layers=2,
        num_attention_heads=_DECODER_ATTENTION_HEADS,
        num_key_value_heads=_DECODER_KEY_VALUE_HEADS,
        head_dim=_DECODER_HEAD_DIM,
        max_position_embeddings=tiny_encoder.MAX_POSITIONS,
    )
    Qwen3Model(config).save_pretrained(directory)
    shutil.copy(tiny_snapshot / "tokenizer.json", directory / "tokenizer.json")
    return directory


def test_a_decoder_config_without_bert_attributes_loads_and_last_token_pools_it(
    tiny_decoder_snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from transformers import AutoConfig

    config = AutoConfig.from_pretrained(tiny_decoder_snapshot)
    assert not hasattr(config, "type_vocab_size")
    assert getattr(config, "pad_token_id", None) is None
    monkeypatch.setattr(
        embedder_module, "get_embedder_spec", lambda _name: tiny_encoder.tiny_spec(pooling="last_token", normalize=False)
    )
    monkeypatch.setattr(embedder_module, "ensure_model", MagicMock(return_value=tiny_decoder_snapshot))
    texts = ["w1 w2 w3 w4", "w5", "w6 w7"]  # one padded batch: the short rows must not read pad positions

    vectors = _embed(texts)

    for text, vector in zip(texts, vectors, strict=True):
        np.testing.assert_allclose(vector, _reference_vector(tiny_decoder_snapshot, text, "last_token"), atol=_TOLERANCE)


def test_importing_the_embedder_module_does_not_import_the_ml_stack() -> None:
    code = (
        "import sys; import archon_search.embedder; "
        "loaded = [m for m in ('torch', 'transformers', 'tokenizers', 'onnxruntime') if m in sys.modules]; "
        "assert not loaded, loaded; print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.stdout.strip() == "ok", result.stderr

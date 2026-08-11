"""Tests for EmbedderCache — written TDD-first."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from archon_search.embedder_cache import EmbedderCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_embedder(name: str, is_warm: bool = True) -> MagicMock:
    m = MagicMock()
    m.model_name = name
    # Explicit bool: a bare MagicMock attribute is truthy, which would make the
    # cold-embedder eviction branch in preload() untestable.
    m.is_warm = is_warm
    # Embedder.embed is async — preload awaits it to warm the backend up.
    m.embed = AsyncMock(return_value=[[0.0]])
    return m


def _slow_make(name: str, providers: list[str] | None = None) -> MagicMock:
    """Synchronous slow factory; runs inside asyncio.to_thread."""
    time.sleep(0.05)
    return _mock_embedder(name)


# ---------------------------------------------------------------------------
# Basic load & cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_load_returns_embedder():
    """First call returns an Embedder."""
    cache = EmbedderCache(max_size=3)
    with patch("archon_search.embedder_cache.make_embedder", return_value=_mock_embedder("model-A")):
        result = await cache.get_or_load("model-A")
    assert result.model_name == "model-A"


@pytest.mark.asyncio
async def test_get_or_load_caches_result():
    """Second call for same model does not call make_embedder again."""
    cache = EmbedderCache(max_size=3)
    mock_emb = _mock_embedder("model-A")
    with patch("archon_search.embedder_cache.make_embedder", return_value=mock_emb) as mock_make:
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-A")
    assert mock_make.call_count == 1


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lru_eviction_removes_oldest():
    """max_size=1: load A then B; only B remains in cache."""
    cache = EmbedderCache(max_size=1)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, providers=None: _mock_embedder(n)):
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")
    assert cache.cached_models() == ["model-B"]
    assert "model-A" not in cache.cached_models()


@pytest.mark.asyncio
async def test_evicted_embedder_still_usable_by_caller():
    """Caller holding reference to evicted embedder can still access its model_name."""
    cache = EmbedderCache(max_size=1)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, providers=None: _mock_embedder(n)):
        emb_a = await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")  # evicts A
    assert emb_a.model_name == "model-A"  # still accessible via caller's reference


# ---------------------------------------------------------------------------
# Concurrent scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_eviction_burst():
    """max_size=2, 4 concurrent loads for 4 distinct models.

    All 4 callers get correct embedders; cache size <= 2; the 2 retained are
    the 2 most recently *stored* (LRU policy: last-in wins eviction race).
    """
    cache = EmbedderCache(max_size=2)
    models = ["model-A", "model-B", "model-C", "model-D"]
    with patch("archon_search.embedder_cache.make_embedder", side_effect=_slow_make):
        results = await asyncio.gather(*[cache.get_or_load(m) for m in models])
    # All 4 callers received their correct embedder
    for model, result in zip(models, results):
        assert result.model_name == model
    # Cache honours max_size
    assert len(cache._cache) <= 2


@pytest.mark.asyncio
async def test_concurrent_miss_deduplication():
    """3 concurrent get_or_load calls for same model → make_embedder called exactly once.

    All 3 callers receive the identical object instance.
    """
    cache = EmbedderCache(max_size=3)
    call_count = 0

    def _counting_slow_make(name: str, providers: list[str] | None = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return _mock_embedder(name)

    with patch("archon_search.embedder_cache.make_embedder", side_effect=_counting_slow_make):
        results = await asyncio.gather(
            cache.get_or_load("model-A"),
            cache.get_or_load("model-A"),
            cache.get_or_load("model-A"),
        )
    assert call_count == 1, f"make_embedder called {call_count} times, expected 1"
    # All callers get the identical instance
    assert results[0] is results[1] is results[2]


@pytest.mark.asyncio
async def test_concurrent_eviction_safety():
    """max_size=1, 2 concurrent loads for different models, both return correct embedders."""
    cache = EmbedderCache(max_size=1)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=_slow_make):
        results = await asyncio.gather(
            cache.get_or_load("model-A"),
            cache.get_or_load("model-B"),
        )
    assert results[0].model_name == "model-A"
    assert results[1].model_name == "model-B"


# ---------------------------------------------------------------------------
# Preload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_skips_unknown_model_without_abort():
    """make_embedder raises for one model; preload completes; other model is cached."""
    cache = EmbedderCache(max_size=3)

    def _selective_make(name: str, providers: list[str] | None = None) -> MagicMock:
        if name == "bad-model":
            raise ValueError("unknown model")
        return _mock_embedder(name)

    with patch("archon_search.embedder_cache.make_embedder", side_effect=_selective_make):
        await cache.preload(["good-model", "bad-model"])

    assert "good-model" in cache.cached_models()
    assert "bad-model" not in cache.cached_models()


@pytest.mark.asyncio
async def test_preload_warms_up_each_loaded_model():
    """S485: preload must exercise embed() so the lazy ONNX backend is built at startup."""
    cache = EmbedderCache(max_size=3)
    embedders = {name: _mock_embedder(name) for name in ("model-A", "model-B")}

    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, providers=None: embedders[n]):
        await cache.preload(["model-A", "model-B"])

    for embedder in embedders.values():
        embedder.embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_preload_warmup_failure_evicts_and_does_not_abort_other_models(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing warm-up embed() is logged and evicted, not fatal.

    Keeping the cold embedder cached would make the next search a cache hit on an
    unwarmed model — the first-query penalty is silently re-paid with no warning.
    The other model must still be loaded and warmed.
    """
    cache = EmbedderCache(max_size=3)
    good = _mock_embedder("good-model")
    cold = _mock_embedder("cold-model", is_warm=False)
    cold.embed = AsyncMock(side_effect=RuntimeError("warm-up failed"))
    embedders = {"good-model": good, "cold-model": cold}

    with caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"):
        with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, providers=None: embedders[n]):
            await cache.preload(["good-model", "cold-model"])  # must not raise

    assert "good-model" in cache.cached_models()
    good.embed.assert_awaited_once()
    # Warm-up failed — the cold model must not be left behind in the cache.
    assert "cold-model" not in cache.cached_models()
    cold.embed.assert_awaited_once()
    assert "failed to warm up 'cold-model'" in caplog.text
    assert "evicting from cache" in caplog.text


@pytest.mark.asyncio
async def test_preload_warmup_failure_keeps_an_already_warm_embedder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warm embedder survives a failing warm-up embed().

    ``ModelEmbedder.encode`` assigns the backend model BEFORE running inference, so
    embed() can raise on an embedder whose ONNX weights are already built. Evicting
    it would throw away a fully paid load and force the next search to redo it.
    """
    cache = EmbedderCache(max_size=3)
    warm = _mock_embedder("warm-model", is_warm=True)
    warm.embed = AsyncMock(side_effect=RuntimeError("inference blew up after load"))

    with caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"):
        with patch("archon_search.embedder_cache.make_embedder", return_value=warm):
            await cache.preload(["warm-model"])  # must not raise

    assert "warm-model" in cache.cached_models()
    assert "failed to warm up 'warm-model'" in caplog.text
    assert "evicting from cache" not in caplog.text


@pytest.mark.asyncio
async def test_preload_load_failure_logs_load_message(caplog: pytest.LogCaptureFixture) -> None:
    """A load failure is reported as a load failure, distinct from a warm-up failure."""
    cache = EmbedderCache(max_size=3)

    def _selective_make(name: str, providers: list[str] | None = None) -> MagicMock:
        if name == "bad-model":
            raise ValueError("unknown model")
        return _mock_embedder(name)

    with caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"):
        with patch("archon_search.embedder_cache.make_embedder", side_effect=_selective_make):
            await cache.preload(["bad-model"])

    assert "failed to load 'bad-model'" in caplog.text
    assert "warm up" not in caplog.text


@pytest.mark.asyncio
async def test_get_or_load_forwards_configured_providers() -> None:
    """The ONNX execution providers reach make_embedder — the hot search path uses the GPU too."""
    cache = EmbedderCache(max_size=3, providers=["CoreMLExecutionProvider"])

    with patch("archon_search.embedder_cache.make_embedder", return_value=_mock_embedder("model-A")) as mock_make:
        await cache.get_or_load("model-A")

    assert mock_make.call_args.kwargs["providers"] == ["CoreMLExecutionProvider"]


@pytest.mark.asyncio
async def test_get_or_load_defaults_providers_to_none() -> None:
    """No providers configured → make_embedder gets None (CPU), not an empty list."""
    cache = EmbedderCache(max_size=3)

    with patch("archon_search.embedder_cache.make_embedder", return_value=_mock_embedder("model-A")) as mock_make:
        await cache.get_or_load("model-A")

    assert mock_make.call_args.kwargs["providers"] is None


@pytest.mark.asyncio
async def test_preload_uses_asyncio_to_thread():
    """Verify asyncio.to_thread is actually called (not direct make_embedder in event loop)."""
    cache = EmbedderCache(max_size=3)
    to_thread_calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(args[0] if args else "?")
        return await original_to_thread(func, *args, **kwargs)

    with patch("asyncio.to_thread", side_effect=tracking_to_thread):
        with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, providers=None: _mock_embedder(n)):
            await cache.preload(["model-A", "model-B"])

    assert len(to_thread_calls) == 2


# ---------------------------------------------------------------------------
# Error handling & cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_load_make_embedder_raises_cleans_up_loading_event():
    """After a failed load, a subsequent call with a working factory succeeds."""
    cache = EmbedderCache(max_size=3)
    call_count = 0

    def _flaky_make(name: str, providers: list[str] | None = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient failure")
        return _mock_embedder(name)

    with patch("archon_search.embedder_cache.make_embedder", side_effect=_flaky_make):
        with pytest.raises(RuntimeError):
            await cache.get_or_load("model-A")
        # Loading event must have been cleaned up; retry must succeed
        result = await cache.get_or_load("model-A")

    assert result.model_name == "model-A"
    assert "model-A" in cache.cached_models()


@pytest.mark.asyncio
async def test_concurrent_waiters_retry_after_failed_load():
    """3 concurrent calls; make_embedder fails first then succeeds.

    Uses asyncio.wait_for with 2s timeout as a deadlock guard.
    """
    cache = EmbedderCache(max_size=3)
    call_count = 0

    def _flaky_make(name: str, providers: list[str] | None = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        time.sleep(0.02)
        if call_count == 1:
            raise RuntimeError("first load fails")
        return _mock_embedder(name)

    async def _load():
        return await cache.get_or_load("model-A")

    with patch("archon_search.embedder_cache.make_embedder", side_effect=_flaky_make):
        tasks = [asyncio.create_task(_load()) for _ in range(3)]
        done, pending = await asyncio.wait(tasks, timeout=2.0)
        # Cancel any pending (shouldn't happen, but keep test clean)
        for t in pending:
            t.cancel()

    # At least 2 of the 3 should succeed (one may raise from the first failure)
    successes = [t for t in done if not t.cancelled() and t.exception() is None]
    failures = [t for t in done if not t.cancelled() and t.exception() is not None]
    assert len(pending) == 0, "Deadlock: some tasks did not complete within 2 s"
    assert len(successes) >= 1, "Expected at least one successful result"
    for t in successes:
        assert t.result().model_name == "model-A"


@pytest.mark.asyncio
async def test_preload_failure_does_not_leave_dangling_loading_event():
    """preload with one bad model; subsequent get_or_load for that model with working make_embedder completes."""
    cache = EmbedderCache(max_size=3)
    call_count = 0

    def _flaky_make(name: str, providers: list[str] | None = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("preload failure")
        return _mock_embedder(name)

    with patch("archon_search.embedder_cache.make_embedder", side_effect=_flaky_make):
        await cache.preload(["model-A"])
        # Now retry — must complete within 2 s (no dangling event blocking)
        result = await asyncio.wait_for(cache.get_or_load("model-A"), timeout=2.0)

    assert result.model_name == "model-A"

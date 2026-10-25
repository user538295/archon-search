"""Tests for EmbedderCache — written TDD-first."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.embedder_cache import EmbedderCache
from archon_search.model_specs import UnsupportedModelError

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


def _slow_make(name: str, device: str = "auto") -> MagicMock:
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
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)):
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")
    assert cache.cached_models() == ["model-B"]
    assert "model-A" not in cache.cached_models()


@pytest.mark.asyncio
async def test_evicted_embedder_still_usable_by_caller():
    """Caller holding reference to evicted embedder can still access its model_name."""
    cache = EmbedderCache(max_size=1)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)):
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

    def _counting_slow_make(name: str, device: str = "auto") -> MagicMock:
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

    def _selective_make(name: str, device: str = "auto") -> MagicMock:
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

    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": embedders[n]):
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

    with (
        caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"),
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": embedders[n]),
    ):
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

    with (
        caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"),
        patch("archon_search.embedder_cache.make_embedder", return_value=warm),
    ):
        await cache.preload(["warm-model"])  # must not raise

    assert "warm-model" in cache.cached_models()
    assert "failed to warm up 'warm-model'" in caplog.text
    assert "evicting from cache" not in caplog.text


@pytest.mark.asyncio
async def test_preload_load_failure_logs_load_message(caplog: pytest.LogCaptureFixture) -> None:
    """A load failure is reported as a load failure, distinct from a warm-up failure."""
    cache = EmbedderCache(max_size=3)

    def _selective_make(name: str, device: str = "auto") -> MagicMock:
        if name == "bad-model":
            raise ValueError("unknown model")
        return _mock_embedder(name)

    with (
        caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"),
        patch("archon_search.embedder_cache.make_embedder", side_effect=_selective_make),
    ):
        await cache.preload(["bad-model"])

    assert "failed to load 'bad-model'" in caplog.text
    assert "warm up" not in caplog.text


@pytest.mark.asyncio
async def test_preload_returns_no_failures_when_every_model_loads_and_warms() -> None:
    """S632: a clean preload reports nothing failed."""
    cache = EmbedderCache(max_size=3)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)):
        failed = await cache.preload(["model-A", "model-B"])

    assert failed == []


@pytest.mark.asyncio
async def test_preload_returns_every_failed_model_in_request_order() -> None:
    """S632: preload names each model whose load or warm-up embed() failed, in request order.

    Covers all three failure shapes — a model that never loaded, a cold model whose
    warm-up failed (evicted), and a warm model whose embed() still raised (kept cached,
    but its warm-up did not succeed). The healthy model is not named.
    """
    cache = EmbedderCache(max_size=4)
    cold = _mock_embedder("cold-model", is_warm=False)
    cold.embed = AsyncMock(side_effect=RuntimeError("warm-up failed"))
    warm_but_broken = _mock_embedder("warm-broken-model", is_warm=True)
    warm_but_broken.embed = AsyncMock(side_effect=RuntimeError("inference blew up after load"))
    embedders = {
        "good-model": _mock_embedder("good-model"),
        "cold-model": cold,
        "warm-broken-model": warm_but_broken,
    }

    def _make(name: str, device: str = "auto") -> MagicMock:
        if name == "missing-model":
            raise ValueError("unknown model")
        return embedders[name]

    with patch("archon_search.embedder_cache.make_embedder", side_effect=_make):
        failed = await cache.preload(["warm-broken-model", "good-model", "missing-model", "cold-model"])

    assert failed == ["warm-broken-model", "missing-model", "cold-model"]


@pytest.mark.asyncio
async def test_preload_does_not_report_a_model_the_lru_evicted_after_it_warmed() -> None:
    """S632: preloading more models than the cache holds is not a warm-up failure.

    The LRU pushes an earlier model out once the cache is full, but it loaded and its
    warm-up embed() succeeded — preload reports the outcome of the work, not the cache
    state left behind.
    """
    cache = EmbedderCache(max_size=1)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)):
        failed = await cache.preload(["model-A", "model-B"])

    assert failed == []
    assert len(cache.cached_models()) == 1


@pytest.mark.asyncio
async def test_cold_models_is_empty_when_every_requested_model_is_cached_warm() -> None:
    """S632: a cache holding every requested model warm reports nothing cold."""
    cache = EmbedderCache(max_size=3)
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)):
        await cache.preload(["model-A", "model-B"])

    assert cache.cold_models(["model-A", "model-B"]) == []


@pytest.mark.asyncio
async def test_cold_models_names_uncached_and_cached_but_cold_models_in_request_order() -> None:
    """S632: a model is cold when it is not cached at all or cached with no backend built.

    That is the state ``preload`` leaves behind for a model that failed to load (never
    cached) or failed its warm-up embed() before the backend loaded (evicted). A warm-up
    cut short by its timeout never gets ``preload``'s outcome, so it reads this state
    back to name the models still cold. Warm models are not named.
    """
    cache = EmbedderCache(max_size=3)
    embedders = {
        "warm-model": _mock_embedder("warm-model", is_warm=True),
        "cold-model": _mock_embedder("cold-model", is_warm=False),
    }
    with patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": embedders[n]):
        await cache.get_or_load("warm-model")
        await cache.get_or_load("cold-model")

    assert cache.cold_models(["missing-model", "warm-model", "cold-model"]) == ["missing-model", "cold-model"]


@pytest.mark.asyncio
async def test_cold_models_is_a_pure_query() -> None:
    """Asking which models are cold must not load them or touch the LRU order."""
    cache = EmbedderCache(max_size=3)
    with patch(
        "archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)
    ) as mock_make:
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")
        mock_make.reset_mock()

        cache.cold_models(["model-A", "model-C"])

    mock_make.assert_not_called()
    assert cache.cached_models() == ["model-A", "model-B"]


@pytest.mark.asyncio
async def test_get_or_load_forwards_configured_device() -> None:
    """[database].device reaches make_embedder — the hot search path uses the accelerator too."""
    cache = EmbedderCache(max_size=3, device="mps")

    with patch("archon_search.embedder_cache.make_embedder", return_value=_mock_embedder("model-A")) as mock_make:
        await cache.get_or_load("model-A")

    assert mock_make.call_args.kwargs["device"] == "mps"


@pytest.mark.asyncio
async def test_get_or_load_defaults_device_to_auto() -> None:
    cache = EmbedderCache(max_size=3)

    with patch("archon_search.embedder_cache.make_embedder", return_value=_mock_embedder("model-A")) as mock_make:
        await cache.get_or_load("model-A")

    assert mock_make.call_args.kwargs["device"] == "auto"


# ---------------------------------------------------------------------------
# A pinned model outside the registry: real make_embedder fails fast at construction
# ---------------------------------------------------------------------------

_SUPPORTED_MODEL = "BAAI/bge-small-en-v1.5"
_UNSUPPORTED_MODEL = "custom/unknown-model"
_PROMPT_SECONDS = 5.0


@pytest.mark.asyncio
async def test_unsupported_pinned_model_fails_cleanly_and_is_not_cached() -> None:
    cache = EmbedderCache(max_size=3)

    with pytest.raises(UnsupportedModelError):
        await asyncio.wait_for(cache.get_or_load(_UNSUPPORTED_MODEL), timeout=_PROMPT_SECONDS)

    assert cache.cached_models() == []


@pytest.mark.asyncio
async def test_unsupported_pinned_model_fails_again_without_hanging_on_a_stale_registration() -> None:
    cache = EmbedderCache(max_size=3)

    for _ in range(2):
        with pytest.raises(UnsupportedModelError):
            await asyncio.wait_for(cache.get_or_load(_UNSUPPORTED_MODEL), timeout=_PROMPT_SECONDS)


@pytest.mark.asyncio
async def test_concurrent_callers_for_an_unsupported_model_all_fail_promptly() -> None:
    cache = EmbedderCache(max_size=3)

    outcomes = await asyncio.wait_for(
        asyncio.gather(*[cache.get_or_load(_UNSUPPORTED_MODEL) for _ in range(3)], return_exceptions=True),
        timeout=_PROMPT_SECONDS,
    )

    assert [type(outcome) for outcome in outcomes] == [UnsupportedModelError] * 3


@pytest.mark.asyncio
async def test_unsupported_pinned_model_does_not_poison_a_supported_one() -> None:
    cache = EmbedderCache(max_size=3)
    with pytest.raises(UnsupportedModelError):
        await cache.get_or_load(_UNSUPPORTED_MODEL)

    embedder = await cache.get_or_load(_SUPPORTED_MODEL)

    assert embedder.model_name == _SUPPORTED_MODEL
    assert cache.cached_models() == [_SUPPORTED_MODEL]


@pytest.mark.asyncio
async def test_preload_names_an_unsupported_model_as_failed_and_still_warms_the_rest() -> None:
    cache = EmbedderCache(max_size=3)

    failed = await cache.preload([_UNSUPPORTED_MODEL, _SUPPORTED_MODEL])

    assert failed == [_UNSUPPORTED_MODEL]
    assert cache.cached_models() == [_SUPPORTED_MODEL]


@pytest.mark.asyncio
async def test_preload_uses_asyncio_to_thread():
    """Verify asyncio.to_thread is actually called (not direct make_embedder in event loop)."""
    cache = EmbedderCache(max_size=3)
    to_thread_calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(args[0] if args else "?")
        return await original_to_thread(func, *args, **kwargs)

    with (
        patch("asyncio.to_thread", side_effect=tracking_to_thread),
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
    ):
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

    def _flaky_make(name: str, device: str = "auto") -> MagicMock:
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

    def _flaky_make(name: str, device: str = "auto") -> MagicMock:
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
    assert len(pending) == 0, "Deadlock: some tasks did not complete within 2 s"
    assert len(successes) >= 1, "Expected at least one successful result"
    for t in successes:
        assert t.result().model_name == "model-A"


@pytest.mark.asyncio
async def test_preload_failure_does_not_leave_dangling_loading_event():
    """preload with one bad model; subsequent get_or_load for that model with working make_embedder completes."""
    cache = EmbedderCache(max_size=3)
    call_count = 0

    def _flaky_make(name: str, device: str = "auto") -> MagicMock:
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


# ---------------------------------------------------------------------------
# Eviction returns the evicted model's memory
# ---------------------------------------------------------------------------

_EVICTION_DEVICE = "mps"


@pytest.mark.asyncio
async def test_eviction_releases_the_memory_of_the_evicted_model():
    """Dropping the reference frees nothing by itself (a model lives in reference cycles, and the
    accelerator keeps freed blocks cached): the cache must collect and release on eviction."""
    cache = EmbedderCache(max_size=1, device=_EVICTION_DEVICE)
    with (
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
        patch("archon_search.embedder_cache.release_model_memory") as release,
    ):
        await cache.get_or_load("model-A")
        release.assert_not_called()
        await cache.get_or_load("model-B")  # evicts A

    release.assert_called_once_with(_EVICTION_DEVICE)


@pytest.mark.asyncio
async def test_cache_hit_and_load_without_eviction_release_nothing():
    cache = EmbedderCache(max_size=2, device=_EVICTION_DEVICE)
    with (
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
        patch("archon_search.embedder_cache.release_model_memory") as release,
    ):
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")
        await cache.get_or_load("model-A")

    release.assert_not_called()


@pytest.mark.asyncio
async def test_eviction_release_runs_off_the_event_loop():
    """The collection walks every live object: it must not stall the loop that serves requests."""
    loop_thread = threading.get_ident()
    release_threads: list[int] = []
    cache = EmbedderCache(max_size=1)
    with (
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
        patch(
            "archon_search.embedder_cache.release_model_memory",
            side_effect=lambda device: release_threads.append(threading.get_ident()),
        ),
    ):
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")

    assert release_threads
    assert loop_thread not in release_threads


@pytest.mark.asyncio
async def test_failed_release_does_not_fail_the_load_that_evicted(caplog: pytest.LogCaptureFixture):
    cache = EmbedderCache(max_size=1)
    with (
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
        patch("archon_search.embedder_cache.release_model_memory", side_effect=RuntimeError("driver gone")),
        caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"),
    ):
        await cache.get_or_load("model-A")
        result = await cache.get_or_load("model-B")

    assert result.model_name == "model-B"
    assert cache.cached_models() == ["model-B"]
    assert any("model-A" in r.getMessage() and "driver gone" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_evicting_only_cold_embedders_releases_nothing():
    """A cold embedder holds no weights, so a collection of the cache is not worth a full collection."""
    cache = EmbedderCache(max_size=1)
    with (
        patch(
            "archon_search.embedder_cache.make_embedder",
            side_effect=lambda n, device="auto": _mock_embedder(n, is_warm=False),
        ),
        patch("archon_search.embedder_cache.release_model_memory") as release,
    ):
        await cache.get_or_load("model-A")
        await cache.get_or_load("model-B")  # evicts the cold A

    release.assert_not_called()


# ---------------------------------------------------------------------------
# Seeding with an existing embedder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preloaded_embedder_is_served_without_building_another():
    seeded = _mock_embedder("model-A")
    cache = EmbedderCache(max_size=2, preloaded={"model-A": seeded})
    with patch("archon_search.embedder_cache.make_embedder") as make:
        served = await cache.get_or_load("model-A")

    assert served is seeded
    make.assert_not_called()
    assert cache.cached_models() == ["model-A"]


@pytest.mark.asyncio
async def test_preloaded_embedder_is_pinned_and_not_counted_against_the_size_limit():
    seeded = _mock_embedder("model-A")
    cache = EmbedderCache(max_size=1, preloaded={"model-A": seeded})
    with (
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
        patch("archon_search.embedder_cache.release_model_memory"),
    ):
        await cache.get_or_load("model-B")
        await cache.get_or_load("model-C")  # evicts B, never the seeded A

    assert cache.cached_models() == ["model-A", "model-C"]


@pytest.mark.asyncio
async def test_preload_warmup_failure_keeps_a_pinned_cold_embedder_and_never_rebuilds_it() -> None:
    """A seeded (pinned) cold embedder whose warm-up embed() fails stays cached: pinned means never evicted."""
    seeded = _mock_embedder("model-A", is_warm=False)
    seeded.embed = AsyncMock(side_effect=RuntimeError("warm-up failed"))
    cache = EmbedderCache(max_size=1, preloaded={"model-A": seeded})

    with patch("archon_search.embedder_cache.make_embedder") as make:
        failed = await cache.preload(["model-A"])
        served = await cache.get_or_load("model-A")

    assert failed == ["model-A"]
    assert served is seeded
    make.assert_not_called()
    assert cache.cached_models() == ["model-A"]


@pytest.mark.asyncio
async def test_release_failure_of_an_evicted_embedder_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed memory release never fails the load that evicted, and leaves a traceback in the log."""
    cache = EmbedderCache(max_size=1)
    with (
        caplog.at_level(logging.WARNING, logger="archon_search.embedder_cache"),
        patch("archon_search.embedder_cache.make_embedder", side_effect=lambda n, device="auto": _mock_embedder(n)),
        patch("archon_search.embedder_cache.release_model_memory", side_effect=RuntimeError("gc blew up")),
    ):
        await cache.get_or_load("model-A")
        loaded = await cache.get_or_load("model-B")  # evicts the warm A, whose release fails

    assert loaded.model_name == "model-B"
    record = next(r for r in caplog.records if "releasing the memory" in r.getMessage())
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None

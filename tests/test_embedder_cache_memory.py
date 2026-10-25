"""``EmbedderCache`` on real (tiny) PyTorch encoders: an evicted model is actually collected, and the seeded one stays.

M1: ``_release_evicted`` used to run ``gc.collect()`` while the ``evicted`` list (and the frames holding it) still
referenced the model, so the release reclaimed the *previous* eviction, never the current one.
M2: the embedder the pipeline seeded the cache with must never be evicted, or the next ``get_or_load`` of the
default model builds a second copy of its weights next to the pipeline's.
"""
from __future__ import annotations

import gc
import weakref
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import _tiny_encoder as tiny_encoder
import pytest

from archon_search import embedder as embedder_module
from archon_search.embedder import Embedder, ModelEmbedder
from archon_search.embedder_cache import EmbedderCache

pytestmark = pytest.mark.real_torch_backends

_CPU = "cpu"
_WARM_TEXT = "w1 w2"


@pytest.fixture
def tiny_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Embedder]:
    """Point ``ModelEmbedder`` at a tiny snapshot; return a factory of real (cold) embedders of it."""
    snapshot = tiny_encoder.build_snapshot(tmp_path / "tiny-bert")
    monkeypatch.setattr(embedder_module, "get_embedder_spec", lambda _name: tiny_encoder.tiny_spec())
    monkeypatch.setattr(embedder_module, "ensure_model", MagicMock(return_value=snapshot))
    monkeypatch.setattr(
        "archon_search.embedder_cache.make_embedder",
        lambda name, device=_CPU: Embedder(ModelEmbedder(name, device=device)),
    )
    return lambda name: Embedder(ModelEmbedder(name, device=_CPU))


async def _warm_and_watch(embedder: Embedder) -> weakref.ref:
    """Load *embedder*'s weights and return a weak reference to the torch model."""
    await embedder.embed([_WARM_TEXT])
    return weakref.ref(embedder.backend._encoder.model)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_evicted_model_is_collected_by_the_time_the_evicting_load_returns(
    tiny_models: Callable[[str], Embedder],
) -> None:
    gc.collect()
    cache = EmbedderCache(max_size=1, device=_CPU)
    first = await cache.get_or_load("tiny/a")
    model = await _warm_and_watch(first)
    del first

    await cache.get_or_load("tiny/b")

    assert cache.cached_models() == ["tiny/b"]
    assert model() is None, "the evicted model's weights are still referenced after the eviction returned"


@pytest.mark.asyncio
async def test_an_evicted_embedder_a_caller_still_holds_keeps_its_weights(
    tiny_models: Callable[[str], Embedder],
) -> None:
    cache = EmbedderCache(max_size=1, device=_CPU)
    held = await cache.get_or_load("tiny/a")
    model = await _warm_and_watch(held)

    await cache.get_or_load("tiny/b")

    assert model() is not None
    assert (await held.embed([_WARM_TEXT]))[0]


@pytest.mark.asyncio
async def test_the_evicted_embedder_itself_is_collected_despite_its_warmup_reference_cycle(
    tiny_models: Callable[[str], Embedder],
) -> None:
    """``Embedder`` -> ``WarmupCoordinator`` -> lambdas -> ``Embedder`` is a cycle: only a collection frees it."""
    cache = EmbedderCache(max_size=1, device=_CPU)
    first = await cache.get_or_load("tiny/a")
    await _warm_and_watch(first)
    embedder_ref = weakref.ref(first)
    del first

    await cache.get_or_load("tiny/b")

    assert embedder_ref() is None


# ---------------------------------------------------------------------------
# M2: the seeded default embedder is pinned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_seeded_embedder_is_never_evicted(tiny_models: Callable[[str], Embedder]) -> None:
    seeded = tiny_models("tiny/default")
    cache = EmbedderCache(max_size=2, device=_CPU, preloaded={"tiny/default": seeded})

    for name in ("tiny/a", "tiny/b", "tiny/c"):
        await cache.get_or_load(name)

    assert await cache.get_or_load("tiny/default") is seeded
    assert "tiny/default" in cache.cached_models()


@pytest.mark.asyncio
async def test_the_seeded_embedder_does_not_count_toward_the_size_limit(tiny_models: Callable[[str], Embedder]) -> None:
    seeded = tiny_models("tiny/default")
    cache = EmbedderCache(max_size=2, device=_CPU, preloaded={"tiny/default": seeded})

    for name in ("tiny/a", "tiny/b"):
        await cache.get_or_load(name)

    assert cache.cached_models() == ["tiny/default", "tiny/a", "tiny/b"]
    await cache.get_or_load("tiny/c")
    assert cache.cached_models() == ["tiny/default", "tiny/b", "tiny/c"]


@pytest.mark.asyncio
async def test_the_default_model_is_not_loaded_twice_after_other_models_were_used(
    tiny_models: Callable[[str], Embedder], monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = tiny_models("tiny/default")
    cache = EmbedderCache(max_size=1, device=_CPU, preloaded={"tiny/default": seeded})
    built: list[str] = []
    real_make = embedder_module.make_embedder

    def counting_make(name: str, device: str = _CPU) -> Embedder:
        built.append(name)
        return real_make(name, device)

    monkeypatch.setattr("archon_search.embedder_cache.make_embedder", counting_make)

    await cache.get_or_load("tiny/a")
    await cache.get_or_load("tiny/b")
    await cache.get_or_load("tiny/default")

    assert built == ["tiny/a", "tiny/b"]

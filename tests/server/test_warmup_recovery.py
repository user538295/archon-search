"""H2: a failed startup warm-up heals itself in the background (``/ready`` no longer needs traffic).

Only a search or an ingest used to retry a model that failed to warm at startup, so an
orchestrator that withholds traffic from a 503 replica kept it 503 for the process
lifetime. ``run_warmup_recovery`` retries the default embedder (and a configured reranker)
with bounded exponential backoff until they are warm.
"""
from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from archon_search.server import warmup as warmup_module
from archon_search.server.schemas import WarmupResult
from archon_search.server.warmup import default_models_warm, run_warmup_recovery

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_COLLECTION_MODEL = "BAAI/bge-base-en-v1.5"
_RERANKER = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_FAILURES_BEFORE_HEALING = 2


class _FlakyPipeline:
    """``warmup_models`` fails *failures* times (stays cold), then warms every model."""

    def __init__(self, failures: int, *, reranker: bool = False) -> None:
        self.failures = failures
        self.calls = 0
        self.embedder_is_warm = False
        self.reranker_is_warm = not reranker

    async def warmup_models(self) -> bool:
        self.calls += 1
        if self.calls <= self.failures:
            return False
        self.embedder_is_warm = True
        self.reranker_is_warm = True
        return True


class _Cache:
    def __init__(self, failing: list[str] | None = None) -> None:
        self.preloaded: list[list[str]] = []
        self._failing = failing or []

    async def preload(self, names: list[str]) -> list[str]:
        self.preloaded.append(list(names))
        return [name for name in names if name in self._failing]


class _Sleeps:
    """Records the requested delays and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _config(reranker: str = "") -> Any:
    return SimpleNamespace(embedding_model=_DEFAULT_MODEL, reranker_model=reranker)


async def _recover(
    pipeline: _FlakyPipeline,
    *,
    cache: _Cache | None = None,
    model_names: list[str] | None = None,
    config: Any = None,
    sleep: Any = None,
) -> tuple[list[WarmupResult], list[str], _Sleeps | Any]:
    results: list[WarmupResult] = []
    recovered: list[str] = []
    sleeps = sleep if sleep is not None else _Sleeps()

    async def _on_recovered() -> None:
        recovered.append("validated")

    await run_warmup_recovery(
        set_result=results.append,
        pipeline=pipeline,  # type: ignore[arg-type]
        embedder_cache=cache or _Cache(),  # type: ignore[arg-type]
        config=config or _config(),
        model_names=model_names or [_DEFAULT_MODEL],
        on_recovered=_on_recovered,
        sleep=sleeps,
    )
    return results, recovered, sleeps


@pytest.mark.asyncio
async def test_recovery_retries_until_the_default_embedder_warms_then_reports_done() -> None:
    pipeline = _FlakyPipeline(_FAILURES_BEFORE_HEALING)

    results, recovered, _ = await _recover(pipeline)

    assert pipeline.calls == _FAILURES_BEFORE_HEALING + 1
    assert pipeline.embedder_is_warm
    assert results == [WarmupResult.DONE]
    assert recovered == ["validated"]


@pytest.mark.asyncio
async def test_recovery_waits_with_bounded_exponential_backoff_between_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(warmup_module, "_RECOVERY_INITIAL_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(warmup_module, "_RECOVERY_BACKOFF_FACTOR", 2.0)
    monkeypatch.setattr(warmup_module, "_RECOVERY_MAX_DELAY_SECONDS", 5.0)
    failures = 5
    pipeline = _FlakyPipeline(failures)

    _, _, sleeps = await _recover(pipeline)

    assert sleeps.delays == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_recovery_is_a_no_op_when_the_defaults_are_already_warm() -> None:
    pipeline = _FlakyPipeline(0)
    pipeline.embedder_is_warm = True

    results, recovered, sleeps = await _recover(pipeline)

    assert (pipeline.calls, results, recovered, sleeps.delays) == (0, [], [], [])


@pytest.mark.asyncio
async def test_recovery_keeps_retrying_while_a_configured_reranker_is_cold() -> None:
    pipeline = _FlakyPipeline(_FAILURES_BEFORE_HEALING, reranker=True)
    pipeline.embedder_is_warm = True  # only the reranker is missing

    results, _, _ = await _recover(pipeline, config=_config(_RERANKER))

    assert pipeline.calls == _FAILURES_BEFORE_HEALING + 1
    assert pipeline.reranker_is_warm
    assert results == [WarmupResult.DONE]


@pytest.mark.asyncio
async def test_recovery_reloads_only_the_non_default_collection_models_once_healed() -> None:
    pipeline = _FlakyPipeline(1)
    cache = _Cache()

    await _recover(pipeline, cache=cache, model_names=[_COLLECTION_MODEL, _DEFAULT_MODEL])

    # The default embedder is the pipeline's own instance: preloading it through the
    # cache would build a second copy of its weights.
    assert cache.preloaded == [[_COLLECTION_MODEL]]


@pytest.mark.asyncio
async def test_recovery_stays_failed_when_a_collection_model_still_cannot_load() -> None:
    pipeline = _FlakyPipeline(1)
    cache = _Cache(failing=[_COLLECTION_MODEL])

    results, recovered, _ = await _recover(
        pipeline, cache=cache, model_names=[_COLLECTION_MODEL, _DEFAULT_MODEL]
    )

    assert results == []  # the result stays FAILED: the collection model is still cold
    assert recovered == ["validated"]  # the default models serve again, so re-check them


@pytest.mark.asyncio
async def test_recovery_stops_cleanly_when_the_server_shuts_down() -> None:
    pipeline = _FlakyPipeline(failures=10**6)
    results: list[WarmupResult] = []
    entered_sleep = asyncio.Event()

    async def _sleep(_: float) -> None:
        entered_sleep.set()
        await asyncio.Event().wait()

    async def _on_recovered() -> None:
        raise AssertionError("must not run after cancellation")

    task = asyncio.create_task(
        run_warmup_recovery(
            set_result=results.append,
            pipeline=pipeline,  # type: ignore[arg-type]
            embedder_cache=_Cache(),  # type: ignore[arg-type]
            config=_config(),
            model_names=[_DEFAULT_MODEL],
            on_recovered=_on_recovered,
            sleep=_sleep,
        )
    )
    await entered_sleep.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert results == []


@pytest.mark.asyncio
async def test_recovery_survives_an_attempt_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _FlakyPipeline(1)
    real = pipeline.warmup_models
    outcomes = itertools.chain([RuntimeError("transient")], itertools.repeat(None))

    async def _raising_then_real() -> bool:
        error = next(outcomes)
        if error is not None:
            raise error
        return await real()

    pipeline.warmup_models = _raising_then_real  # type: ignore[method-assign]

    results, _, _ = await _recover(pipeline)

    assert results == [WarmupResult.DONE]


def test_default_models_warm_requires_the_reranker_only_when_one_is_configured() -> None:
    pipeline = _FlakyPipeline(0, reranker=True)
    pipeline.embedder_is_warm = True
    pipeline.reranker_is_warm = False

    assert default_models_warm(pipeline, _config()) is True  # type: ignore[arg-type]
    assert default_models_warm(pipeline, _config(_RERANKER)) is False  # type: ignore[arg-type]
    pipeline.embedder_is_warm = False
    assert default_models_warm(pipeline, _config()) is False  # type: ignore[arg-type]

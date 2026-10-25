"""``run_model_warmup`` swallows ordinary failures, but never a KeyboardInterrupt or SystemExit."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from archon_search.config import SearchConfig
from archon_search.server.warmup import WarmupResult, run_model_warmup


class _InterruptedPipeline:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def warmup_models(self) -> None:
        raise self._error


async def _run(error: BaseException, results: list[WarmupResult]) -> None:
    await run_model_warmup(
        set_result=results.append,
        pipeline=_InterruptedPipeline(error),  # type: ignore[arg-type]
        embedder_cache=SimpleNamespace(),  # type: ignore[arg-type]
        config=SearchConfig(),
        model_names=[],
    )


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
async def test_an_interpreter_exit_signal_is_not_swallowed(interrupt: type[BaseException]) -> None:
    results: list[WarmupResult] = []

    with pytest.raises(interrupt):
        await _run(interrupt(), results)

    assert results == []


async def test_an_ordinary_failure_is_still_reported_as_failed() -> None:
    results: list[WarmupResult] = []

    await _run(RuntimeError("loader broke"), results)

    assert results == [WarmupResult.FAILED]

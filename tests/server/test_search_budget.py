"""Unit tests for ``archon_search.server._search_budget`` — S286/S288.

The module is tiny but load-bearing: every transport (REST, MCP, the ``/v1``
shim) routes its warm-up and its overall request budget through it, so a
regression here changes the wire behaviour of all three at once.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.server._search_budget import (
    EMBEDDER_WARMUP_WAIT_SECONDS,
    RERANKER_WARMUP_WAIT_SECONDS,
    SEARCH_TIMEOUT_SECONDS,
    SearchBudgetExceeded,
    hyde_may_embed,
    run_within_budget,
    warmup_for_search,
)


# ---------------------------------------------------------------------------
# warmup_for_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warmup_for_search_bounds_the_reranker_leg() -> None:
    """The reranker warm-up is always bounded — an unbounded one is the S288 bug."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)

    assert (await warmup_for_search(pipeline)).rerank is True
    pipeline.warmup_models.assert_awaited_once_with(
        None,
        reranker_timeout=RERANKER_WARMUP_WAIT_SECONDS,
        embedder_timeout=EMBEDDER_WARMUP_WAIT_SECONDS,
    )


@pytest.mark.asyncio
async def test_warmup_for_search_bounds_the_embedder_leg() -> None:
    """The embedder warm-up is bounded too — an unbounded one is the S290 bug."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    embedder = MagicMock()
    embedder.is_warm = False

    assert (await warmup_for_search(pipeline, embedder)).fts_only is True
    assert pipeline.warmup_models.await_args.kwargs["embedder_timeout"] == (
        EMBEDDER_WARMUP_WAIT_SECONDS
    )


@pytest.mark.asyncio
async def test_warmup_for_search_reports_a_warm_embedder_as_full_search() -> None:
    """A warm embedder must not degrade the search to its FTS leg."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    embedder = MagicMock()
    embedder.is_warm = True

    assert (await warmup_for_search(pipeline, embedder)).fts_only is False


@pytest.mark.asyncio
async def test_warmup_for_search_reads_the_global_embedder_on_the_fanout_path() -> None:
    """With no embedder argument the fan-out path reports the global one (S288)."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = False

    assert (await warmup_for_search(pipeline)).fts_only is True


@pytest.mark.asyncio
async def test_warmup_for_search_forwards_the_request_embedder() -> None:
    """Single-collection callers warm the embedder that actually serves the request."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=False)
    embedder = MagicMock()

    assert (await warmup_for_search(pipeline, embedder)).rerank is False
    pipeline.warmup_models.assert_awaited_once_with(
        embedder,
        reranker_timeout=RERANKER_WARMUP_WAIT_SECONDS,
        embedder_timeout=EMBEDDER_WARMUP_WAIT_SECONDS,
    )


# ---------------------------------------------------------------------------
# hyde_may_embed — C1-I-1 (S290)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hyde_may_embed_is_false_while_the_global_embedder_is_cold() -> None:
    """HyDE must not generate against a cold embedder — its own embed_one would park."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = False

    assert await hyde_may_embed(pipeline) is False


@pytest.mark.asyncio
async def test_hyde_may_embed_is_true_once_the_global_embedder_is_warm() -> None:
    """A warm embedder leaves HyDE fully enabled."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = True

    assert await hyde_may_embed(pipeline) is True


@pytest.mark.asyncio
async def test_hyde_may_embed_bounds_the_warmup_it_performs() -> None:
    """The gate is itself a *bounded* warm-up, never an unbounded wait."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = True

    await hyde_may_embed(pipeline)

    pipeline.warmup_models.assert_awaited_once_with(
        None,
        reranker_timeout=RERANKER_WARMUP_WAIT_SECONDS,
        embedder_timeout=EMBEDDER_WARMUP_WAIT_SECONDS,
    )


@pytest.mark.asyncio
async def test_hyde_may_embed_reads_the_global_embedder_not_a_request_one() -> None:
    """HyDE embeds through ``pipeline._global_embedder``, so that is the one to check."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = False

    await hyde_may_embed(pipeline)

    assert pipeline.warmup_models.await_args.args == (None,)


def test_reranker_warmup_wait_is_shorter_than_the_search_budget() -> None:
    """The warm-up wait is a degrade trigger, not a client-read-timeout budget."""
    assert 0 < RERANKER_WARMUP_WAIT_SECONDS < SEARCH_TIMEOUT_SECONDS


def test_embedder_warmup_wait_is_shorter_than_the_search_budget() -> None:
    """Same for the embedder leg: a degrade trigger, not a read-timeout budget (S290)."""
    assert 0 < EMBEDDER_WARMUP_WAIT_SECONDS < SEARCH_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# run_within_budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_within_budget_returns_the_coroutine_result() -> None:
    async def _work() -> str:
        return "done"

    assert await run_within_budget(_work(), timeout=5.0) == "done"


@pytest.mark.asyncio
async def test_run_within_budget_raises_search_budget_exceeded_on_overrun() -> None:
    """Only a genuine overrun — the task was cancelled by ``wait_for``."""

    async def _slow() -> None:
        await asyncio.sleep(30)

    with pytest.raises(SearchBudgetExceeded):
        await run_within_budget(_slow(), timeout=0.01)


@pytest.mark.asyncio
async def test_inner_builtin_timeouterror_is_not_converted_to_a_budget_overrun() -> None:
    """A ``TimeoutError`` raised *by the pipeline* keeps its own status mapping.

    Since 3.11 ``asyncio.TimeoutError`` **is** the builtin ``TimeoutError``, an
    ``OSError`` subclass. A socket ``ETIMEDOUT`` from the store therefore looks
    identical to a budget overrun to a bare ``except asyncio.TimeoutError`` —
    which would report an internal error as a 504.
    """

    async def _raises() -> None:
        raise TimeoutError("ETIMEDOUT from the store")

    with pytest.raises(TimeoutError) as excinfo:
        await run_within_budget(_raises(), timeout=5.0)
    assert not isinstance(excinfo.value, SearchBudgetExceeded)
    assert str(excinfo.value) == "ETIMEDOUT from the store"


@pytest.mark.asyncio
async def test_inner_oserror_propagates_unchanged() -> None:
    async def _raises() -> None:
        raise OSError(60, "Operation timed out")

    with pytest.raises(OSError) as excinfo:
        await run_within_budget(_raises(), timeout=5.0)
    assert not isinstance(excinfo.value, SearchBudgetExceeded)
    assert excinfo.value.errno == 60


@pytest.mark.asyncio
async def test_search_budget_exceeded_is_not_an_oserror() -> None:
    """Handlers catch it deliberately; it must not alias the builtin family."""
    assert not issubclass(SearchBudgetExceeded, OSError)
    assert not issubclass(SearchBudgetExceeded, asyncio.TimeoutError)

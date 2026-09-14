"""Per-request search budget shared by every transport that runs the search pipeline.

REST (``routes_search``), MCP (``mcp``) and the OpenAI shim (``routes_openai_shim``)
drive the same :class:`~archon_search.pipeline.SearchPipeline` under the same
timing constraints, so the budget and the warm-up policy live here rather than in
one transport's module.  This module decides only *how long to wait*; each
transport keeps its own error envelope and maps :class:`SearchBudgetExceeded` onto
it at the edge.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from archon_search.embedder import Embedder
    from archon_search.pipeline import SearchPipeline

# TODO: make configurable via config.py (see /route for parity)
SEARCH_TIMEOUT_SECONDS = 30.0

# Upper bound on how long one search request waits for the cross-encoder to finish
# its cold ONNX build before answering without reranking (S286/S288).  Deliberately
# short, not a client-read-timeout budget: a request already this deep into a cold
# path means an ingest job upstream (bounded by routes_jobs._INGEST_WARMUP_WAIT_SECONDS)
# or the lifespan warm-up already spent up to their own ceilings on this same build
# and it is still not done, so a further multi-second wait here buys little.  The
# build keeps running in its worker thread regardless — this request degrades to the
# fused vector+FTS ranking and later requests get the full reranked pipeline back.
RERANKER_WARMUP_WAIT_SECONDS = 0.5

_T = TypeVar("_T")


class SearchBudgetExceeded(Exception):
    """The overall per-request search budget elapsed.

    Raised only by :func:`run_within_budget`, never by the pipeline itself — see
    that function for why a distinct exception type is required here.
    """


async def warmup_for_search(
    pipeline: "SearchPipeline", embedder: "Embedder | None" = None
) -> bool:
    """Warm the lazy ONNX models before entering the search budget; return ``rerank``.

    Both ML backends load their weights on first use, so warming them here keeps a
    cold build off the request's timeout budget (S184).  The reranker leg is bounded
    by :data:`RERANKER_WARMUP_WAIT_SECONDS` because a search *can* answer without it:
    a ``False`` return means the cross-encoder is still cold and the caller must pass
    ``rerank=False``, degrading to the fused vector+FTS ranking rather than blocking
    past a client's read timeout inside ``rerank_candidates`` (S288).  The build keeps
    running in its worker thread and warms later requests.

    Pass the embedder actually serving the request; fan-out callers omit it and
    ``warmup_models`` warms the pipeline's global embedder instead — the one
    ``search_many`` embeds the query with. The embedder leg is unbounded on both
    paths: no search can be answered without a query vector.
    Never raises — ``warmup_models`` logs and swallows warm-up failures.
    """
    return await pipeline.warmup_models(embedder, reranker_timeout=RERANKER_WARMUP_WAIT_SECONDS)


async def run_within_budget(
    coro: Coroutine[Any, Any, _T], *, timeout: float = SEARCH_TIMEOUT_SECONDS
) -> _T:
    """Await *coro* under the overall search budget.

    Raises :class:`SearchBudgetExceeded` if and only if the budget itself elapsed.
    A plain ``except asyncio.TimeoutError`` around ``asyncio.wait_for`` cannot make
    that distinction: since Python 3.11 ``asyncio.TimeoutError`` *is* the builtin
    ``TimeoutError``, an ``OSError`` subclass, so a socket ``ETIMEDOUT`` raised deep
    inside the store would be caught too and mis-mapped to a 504.  Anything the
    coroutine itself raises — including its own ``TimeoutError`` — propagates
    unchanged and keeps its original status mapping.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        # wait_for cancels the task on expiry; a TimeoutError raised *by the task*
        # leaves it finished-with-exception instead.
        if task.cancelled():
            raise SearchBudgetExceeded from None
        raise

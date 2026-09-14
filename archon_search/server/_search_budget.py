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
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

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

# Upper bound on how long one search request waits for the query embedder to finish
# its cold ONNX build before answering FTS-only (S290).  Same reasoning as the
# reranker bound above — a request this deep into a cold path is waiting behind a
# lifespan or ingest warm-up that has already spent its own ceiling on this build —
# and the same outcome: the build keeps running in its worker thread, this request
# degrades rather than parking the connection past the client's read timeout.
EMBEDDER_WARMUP_WAIT_SECONDS = 0.5

_T = TypeVar("_T")


class SearchWarmup(NamedTuple):
    """What one bounded warm-up leaves the caller able to do.

    ``rerank=False`` → the cross-encoder is still cold, search without it.
    ``fts_only=True`` → the embedder is still cold, search without a query vector.
    """

    rerank: bool
    fts_only: bool


class SearchBudgetExceeded(Exception):
    """The overall per-request search budget elapsed.

    Raised only by :func:`run_within_budget`, never by the pipeline itself — see
    that function for why a distinct exception type is required here.
    """


async def warmup_for_search(
    pipeline: "SearchPipeline", embedder: "Embedder | None" = None
) -> SearchWarmup:
    """Warm the lazy ONNX models before entering the search budget.

    Both ML backends load their weights on first use, so warming them here keeps a
    cold build off the request's timeout budget (S184).  *Both* legs are bounded,
    because neither one is worth parking a connection for:

    * the reranker, by :data:`RERANKER_WARMUP_WAIT_SECONDS` — ``rerank=False``
      degrades to the fused vector+FTS ranking rather than blocking inside
      ``rerank_candidates`` (S288);
    * the embedder, by :data:`EMBEDDER_WARMUP_WAIT_SECONDS` — ``fts_only=True``
      degrades to the FTS leg alone rather than blocking inside ``embed_one``
      for the rest of the build (S290).

    Either build keeps running in its worker thread and warms later requests, so
    the degrade lasts only as long as the build does.

    Pass the embedder actually serving the request; fan-out callers omit it and
    ``warmup_models`` warms the pipeline's global embedder instead — the one
    ``search_many`` embeds the query with, and the one whose warmth is reported
    back here.
    Never raises — ``warmup_models`` logs and swallows warm-up failures.
    """
    rerank = await pipeline.warmup_models(
        embedder,
        reranker_timeout=RERANKER_WARMUP_WAIT_SECONDS,
        embedder_timeout=EMBEDDER_WARMUP_WAIT_SECONDS,
    )
    warm = embedder.is_warm if embedder is not None else pipeline.embedder_is_warm
    return SearchWarmup(rerank=rerank, fts_only=not warm)


async def hyde_may_embed(pipeline: "SearchPipeline") -> bool:
    """Warm the models, bounded, and report whether HyDE can afford to embed.

    ``HyDEGenerator.generate`` ends in ``embed_one`` on ``pipeline``'s *global*
    embedder — the one this reads — and the search call sites resolve HyDE before
    they enter any request budget.  A cold embedder there would therefore park the
    connection for the whole ONNX build, which is S290 itself rather than a path
    around it.  Pass this as ``resolve_hyde_vector(..., may_embed=...)``: a
    ``False`` return skips HyDE (reported as ``hyde_applied=False``, the caller's
    existing HyDE-failure warning) and the search then degrades to ``fts_only``
    the same way an unexpanded one does.
    """
    return not (await warmup_for_search(pipeline)).fts_only


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

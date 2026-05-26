"""Readiness aggregation for archon-search — Task 6.1 (B2).

Exports:
  WatcherManagerProtocol — interface contract for app.state.watcher_manager
  collect_readiness()    — async aggregator called by routes_status.status()
"""
from __future__ import annotations

from typing import Protocol

from starlette.datastructures import State

from archon_search.progress import IndexingState, IndexingStatus
from archon_search.server.schemas import JobCounts, ReadinessDetail, WatcherReport
from archon_search.types import JobStatus


class WatcherManagerProtocol(Protocol):
    """Minimal interface contract for app.state.watcher_manager.

    Matches the actual WatcherManager.watching_names() signature (watcher.py).
    Returns set[str] — not Iterable — to keep the sorted() call safe.
    """

    def watching_names(self) -> set[str]: ...


async def collect_readiness(app_state: State, state: IndexingState | None) -> ReadinessDetail:
    """Aggregate all readiness signals. Called by routes_status.status().

    Parameters:
      app_state: request.app.state (Starlette State object)
      state: already-loaded IndexingState from state_store.read() in the handler
             (passed in to avoid a second disk read; may be None if state file absent/corrupt)
    """
    # Storage ping (async, TTL-cached)
    storage_ok = await app_state.search_store.ping()

    # Model warm-status (side-effect-free) — via pipeline seam, not app_state.embedder directly
    pipeline = app_state.pipeline
    embedder_warm: bool = pipeline.embedder_is_warm if pipeline is not None else False
    reranker_warm: bool = pipeline.reranker_is_warm if pipeline is not None else False

    # Job counts
    job_counts = app_state.job_store.count_by_status()
    jobs = JobCounts(
        pending=job_counts.get(JobStatus.PENDING, 0),
        running=job_counts.get(JobStatus.RUNNING, 0),
    )

    # Index-state counts (state passed in from caller — no extra disk read)
    collections_indexing = 0
    collections_failed = 0
    if state is not None:
        for cp in state.collections.values():
            if cp.status == IndexingStatus.IN_PROGRESS:
                collections_indexing += 1
            elif cp.status == IndexingStatus.FAILED:
                collections_failed += 1

    # Watcher report
    wm: WatcherManagerProtocol | None = app_state.watcher_manager
    watcher = (
        WatcherReport(running=False)
        if wm is None
        else WatcherReport(running=True, watching=sorted(wm.watching_names()))
    )

    return ReadinessDetail(
        storage_connected=storage_ok,
        embedder_warm=embedder_warm,
        reranker_warm=reranker_warm,
        jobs=jobs,
        collections_indexing=collections_indexing,
        collections_failed=collections_failed,
        watcher=watcher,
    )

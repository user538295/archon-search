"""Unit tests for collect_readiness() in isolation — Task 6.1 (B2)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
from archon_search.server.schemas import ReadinessDetail, WatcherReport
from archon_search.types import JobStatus


def _make_app_state(
    *,
    ping_result: bool = True,
    embedder_warm: bool = False,
    reranker_warm: bool = False,
    count_by_status: dict | None = None,
    watcher_manager: object = None,
) -> MagicMock:
    """Build a minimal mock app_state for collect_readiness tests."""
    if count_by_status is None:
        count_by_status = {s: 0 for s in JobStatus}

    app_state = MagicMock()
    app_state.search_store.ping = AsyncMock(return_value=ping_result)

    pipeline = MagicMock()
    pipeline.embedder_is_warm = embedder_warm
    pipeline.reranker_is_warm = reranker_warm
    app_state.pipeline = pipeline

    app_state.job_store.count_by_status = MagicMock(return_value=count_by_status)
    app_state.watcher_manager = watcher_manager

    # state_store.read should NOT be called — set up as MagicMock so we can assert_not_called
    app_state.state_store = MagicMock()

    return app_state


def _make_state(**collections: str) -> IndexingState:
    """Build an IndexingState with the given collection-name → status mapping."""
    return IndexingState(
        collections={
            name: CollectionProgress(status=IndexingStatus(status))
            for name, status in collections.items()
        }
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_collect_readiness_happy_path() -> None:
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state(ping_result=True, embedder_warm=True, reranker_warm=True)
    state = IndexingState(collections={})

    result = asyncio.run(collect_readiness(app_state, state))

    assert isinstance(result, ReadinessDetail)
    assert result.storage_connected is True
    assert result.embedder_warm is True
    assert result.reranker_warm is True
    assert result.jobs.pending == 0
    assert result.jobs.running == 0
    assert result.collections_indexing == 0
    assert result.collections_failed == 0
    assert result.watcher == WatcherReport(running=False, watching=[])


# ---------------------------------------------------------------------------
# storage_connected
# ---------------------------------------------------------------------------

def test_collect_readiness_storage_down() -> None:
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state(ping_result=False)
    result = asyncio.run(collect_readiness(app_state, None))
    assert result.storage_connected is False


def test_collect_readiness_ping_raises() -> None:
    """If ping() breaks its 'never raises' contract, collect_readiness propagates the exception."""
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state()
    app_state.search_store.ping = AsyncMock(side_effect=RuntimeError("broken contract"))

    with pytest.raises(RuntimeError, match="broken contract"):
        asyncio.run(collect_readiness(app_state, None))


# ---------------------------------------------------------------------------
# pipeline / warm-status
# ---------------------------------------------------------------------------

def test_collect_readiness_pipeline_none() -> None:
    """When app_state.pipeline is None, both warm flags default to False without AttributeError."""
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state()
    app_state.pipeline = None

    result = asyncio.run(collect_readiness(app_state, None))
    assert result.embedder_warm is False
    assert result.reranker_warm is False


# ---------------------------------------------------------------------------
# state / collections counts
# ---------------------------------------------------------------------------

def test_collect_readiness_state_none() -> None:
    """Passing state=None yields 0 for both indexing/failed counts."""
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state()
    result = asyncio.run(collect_readiness(app_state, None))
    assert result.collections_indexing == 0
    assert result.collections_failed == 0


def test_collect_readiness_state_has_failed_and_indexing() -> None:
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state()
    state = _make_state(colA="in_progress", colB="failed", colC="done")

    result = asyncio.run(collect_readiness(app_state, state))
    assert result.collections_indexing == 1
    assert result.collections_failed == 1


# ---------------------------------------------------------------------------
# watcher
# ---------------------------------------------------------------------------

def test_collect_readiness_watcher_none() -> None:
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state(watcher_manager=None)
    result = asyncio.run(collect_readiness(app_state, None))
    assert result.watcher == WatcherReport(running=False, watching=[])


def test_collect_readiness_watcher_stub() -> None:
    from archon_search.server.readiness import collect_readiness

    stub = MagicMock()
    stub.watching_names.return_value = {"colB", "colA"}
    app_state = _make_app_state(watcher_manager=stub)

    result = asyncio.run(collect_readiness(app_state, None))
    assert result.watcher == WatcherReport(running=True, watching=["colA", "colB"])


def test_collect_readiness_watcher_raises() -> None:
    """A broken WatcherManager propagates as 500 (no silent swallow)."""
    from archon_search.server.readiness import collect_readiness

    stub = MagicMock()
    stub.watching_names.side_effect = RuntimeError("watchdog thread died")
    app_state = _make_app_state(watcher_manager=stub)

    with pytest.raises(RuntimeError, match="watchdog thread died"):
        asyncio.run(collect_readiness(app_state, None))


# ---------------------------------------------------------------------------
# job counts
# ---------------------------------------------------------------------------

def test_collect_readiness_jobs_pending_running() -> None:
    from archon_search.server.readiness import collect_readiness

    counts = {s: 0 for s in JobStatus}
    counts[JobStatus.PENDING] = 2
    counts[JobStatus.RUNNING] = 1
    app_state = _make_app_state(count_by_status=counts)

    result = asyncio.run(collect_readiness(app_state, None))
    assert result.jobs.pending == 2
    assert result.jobs.running == 1


def test_collect_readiness_cancelling_job_not_counted_as_running() -> None:
    """CANCELLING status must NOT increment jobs.running."""
    from archon_search.server.readiness import collect_readiness

    counts = {s: 0 for s in JobStatus}
    counts[JobStatus.CANCELLING] = 1
    app_state = _make_app_state(count_by_status=counts)

    result = asyncio.run(collect_readiness(app_state, None))
    assert result.jobs.running == 0


def test_collect_readiness_job_store_count_raises() -> None:
    """count_by_status() failures propagate (no silent swallow → 500 on /status)."""
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state()
    app_state.job_store.count_by_status.side_effect = RuntimeError("disk error")

    with pytest.raises(RuntimeError, match="disk error"):
        asyncio.run(collect_readiness(app_state, None))


# ---------------------------------------------------------------------------
# Design guard: no double disk read
# ---------------------------------------------------------------------------

def test_collect_readiness_does_not_read_state_store() -> None:
    """collect_readiness must never call app_state.state_store.read() — state is passed in."""
    from archon_search.server.readiness import collect_readiness

    app_state = _make_app_state()
    state = IndexingState(collections={})

    asyncio.run(collect_readiness(app_state, state))

    app_state.state_store.read.assert_not_called()

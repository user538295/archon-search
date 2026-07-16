"""Unit tests for BE-6 — module-level per-(namespace, collection) rebuild lock.

Tests verify (C3, S9, S12):
- Two distinct CommunityBuilder instances sharing a (ns, collection) key serialise
  their build() calls via the shared module-level registry — this MUST fail
  against a per-instance lock design.
- The rebuild lock is independent of SearchStore.lock_for (ingest lock never
  contends with a rebuild).
- The per-key lock is created lazily on first access inside the running event
  loop, not at import time.
- Different (namespace, collection) keys acquire independent locks.
"""
from __future__ import annotations

import asyncio

import pytest

from archon_search.community_builder import _get_rebuild_lock, _rebuild_locks
from archon_search.graph_types import EntityType, GraphNode


def make_node(node_id: str) -> GraphNode:
    return GraphNode(
        id=node_id,
        entity_name=node_id,
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test",
    )


def make_mock_store(nodes, edges, *, delay: float = 0.0, events: list[str] | None = None, label: str = ""):
    """Build a MagicMock GraphStore whose get_all_nodes records a start/end marker.

    ``get_all_nodes`` runs INSIDE ``CommunityBuilder.build``'s lock-acquired
    section, so recording markers around its sleep proves whether two
    concurrent build() calls actually serialise (no interleaving) or run
    their locked bodies concurrently (interleaved).
    """
    from unittest.mock import AsyncMock, MagicMock

    store = MagicMock()

    async def _get_all_nodes(*args, **kwargs):
        if events is not None:
            events.append(f"{label}-start")
        if delay:
            await asyncio.sleep(delay)
        if events is not None:
            events.append(f"{label}-end")
        return nodes

    store.get_all_nodes = _get_all_nodes
    store.get_all_edges = AsyncMock(return_value=edges)
    store.write_communities = AsyncMock(return_value=None)
    return store


def make_graph_config():
    from archon_search.config import GraphConfig
    return GraphConfig()


@pytest.fixture(autouse=True)
def _clear_rebuild_locks():
    """Reset the module-level registry so tests don't leak locks across each other."""
    _rebuild_locks.clear()
    yield
    _rebuild_locks.clear()


# ---------------------------------------------------------------------------
# S9/S12 — two distinct CommunityBuilder instances on the same key serialise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_builders_same_key_serialise():
    """Two distinct CommunityBuilder instances on the same (ns, col) key serialise.

    Fails against a per-instance lock: without the shared registry, both builds
    would run their bodies concurrently and the order list would interleave.
    """
    from archon_search.community_builder import CommunityBuilder

    events: list[str] = []

    async def _build_recording(label: str, delay: float) -> None:
        store = make_mock_store(
            [make_node("a"), make_node("b")], [], delay=delay, events=events, label=label
        )
        config = make_graph_config()
        # A fresh CommunityBuilder instance per call — mirrors production
        # (route task + MaintenanceLoop each construct their own).
        builder = CommunityBuilder(store, config)
        await builder.build("shared-col", ns="ns-a", seed=42)

    # First task holds the lock for a while (simulated by the get_all_nodes delay);
    # the second task must wait for it to fully finish before starting its own body.
    await asyncio.gather(
        _build_recording("first", 0.05),
        _build_recording("second", 0.0),
    )

    # Serialised execution means one build's start+end must both appear before
    # the other build's start — i.e. no interleaving of "start"/"end" markers.
    first_start = events.index("first-start")
    first_end = events.index("first-end")
    second_start = events.index("second-start")
    second_end = events.index("second-end")

    assert (first_end < second_start) or (second_end < first_start), (
        f"builds interleaved instead of serialising: {events}"
    )


# ---------------------------------------------------------------------------
# S9 — rebuild lock independent of SearchStore.lock_for (never contends with ingest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_lock_independent_of_ingest_lock():
    """The rebuild lock and SearchStore.lock_for never contend; a rebuild does not block ingest."""
    from archon_search.store import SearchStore

    search_store = SearchStore.__new__(SearchStore)
    search_store._collection_locks = {}

    rebuild_lock = _get_rebuild_lock("default", "col-x")
    ingest_lock = search_store.lock_for("col-x")

    assert rebuild_lock is not ingest_lock

    # Holding the rebuild lock must not block acquiring the ingest lock.
    async with rebuild_lock:
        acquired = await asyncio.wait_for(ingest_lock.acquire(), timeout=0.5)
        assert acquired is True
        ingest_lock.release()


# ---------------------------------------------------------------------------
# C2-2 — lazy creation inside the running event loop, not at import time
# ---------------------------------------------------------------------------


def test_rebuild_lock_created_lazily_in_running_loop():
    """The per-key lock is created on first access, not at import, avoiding cross-loop binding."""
    # The registry dict exists at import time, but must be empty until first access.
    assert isinstance(_rebuild_locks, dict)
    assert ("default", "col-lazy") not in _rebuild_locks

    async def _access_lock():
        lock = _get_rebuild_lock("default", "col-lazy")
        assert isinstance(lock, asyncio.Lock)
        return lock

    lock_from_loop_1 = asyncio.run(_access_lock())
    assert lock_from_loop_1 is not None

    # A fresh asyncio.run(...) spins up (and tears down) a brand-new event loop.
    # Because the lock was created lazily during that run (not at import), a
    # second, independent loop must be able to create/access its OWN lock for a
    # different key without raising "bound to a different event loop".
    async def _access_lock_other_key():
        lock = _get_rebuild_lock("default", "col-lazy-2")
        async with lock:
            pass
        return lock

    lock_from_loop_2 = asyncio.run(_access_lock_other_key())
    assert lock_from_loop_2 is not None


# ---------------------------------------------------------------------------
# S11 — different namespaces do not serialise (independent lock keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_namespaces_do_not_serialise():
    """Different (ns, collection) keys acquire independent locks."""
    lock_ns_a = _get_rebuild_lock("ns-a", "col-shared-name")
    lock_ns_b = _get_rebuild_lock("ns-b", "col-shared-name")

    assert lock_ns_a is not lock_ns_b

    # Holding one must not block acquiring the other.
    async with lock_ns_a:
        acquired = await asyncio.wait_for(lock_ns_b.acquire(), timeout=0.5)
        assert acquired is True
        lock_ns_b.release()


# ---------------------------------------------------------------------------
# C3 — the lock is released after build() (both on success AND on exception),
# so a stuck lock can never permanently wedge future rebuilds for that key.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_released_after_successful_build():
    """After a successful build(), the (ns, col) lock is free for the next build."""
    from archon_search.community_builder import CommunityBuilder

    store = make_mock_store([make_node("a"), make_node("b")], [])
    builder = CommunityBuilder(store, make_graph_config())
    await builder.build("col-release", ns="ns-a", seed=42)

    # The lock created during the build must exist in the registry and be free.
    lock = _get_rebuild_lock("ns-a", "col-release")
    assert not lock.locked(), "lock still held after a successful build()"
    # A follow-up acquisition must succeed immediately (no permanent wedge).
    acquired = await asyncio.wait_for(lock.acquire(), timeout=0.5)
    assert acquired is True
    lock.release()


@pytest.mark.asyncio
async def test_lock_released_when_build_raises():
    """build() raising (e.g. zero graph nodes → ValueError) still releases the lock.

    A leaked lock here would silently 409-or-block every future rebuild for the
    key, so release-on-exception is the critical safety property of `async with`.
    """
    from archon_search.community_builder import CommunityBuilder

    # Zero nodes → build() raises ValueError from inside the locked section.
    store = make_mock_store([], [])
    builder = CommunityBuilder(store, make_graph_config())

    with pytest.raises(ValueError):
        await builder.build("col-raises", ns="ns-a", seed=42)

    lock = _get_rebuild_lock("ns-a", "col-raises")
    assert not lock.locked(), "lock still held after build() raised"
    acquired = await asyncio.wait_for(lock.acquire(), timeout=0.5)
    assert acquired is True
    lock.release()

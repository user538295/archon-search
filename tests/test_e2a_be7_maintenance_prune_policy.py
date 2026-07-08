"""Tests for BE-7: MaintenanceLoop._run_expired_chunk_pruning policy.

Plan: Documentation/Backlog/e2a-ttl-scoping-team-plan.md Task BE-7

TDD: tests written first, then implementation in
archon_search/jobs/maintenance_loop.py.

Required tests:
- test_run_expired_chunk_pruning_skips_when_disabled
- test_run_expired_chunk_pruning_calls_store_per_collection
- test_run_expired_chunk_pruning_updates_health_entry
- test_run_expired_chunk_pruning_logs_warning_with_doc_ids
- test_run_one_pass_includes_prune_policy
- test_run_expired_chunk_pruning_continues_on_exception
- test_maintenance_loop_prune_deletes_expired_and_updates_state (integration)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import CollectionInfo
from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import _EMPTY_HEALTH_ENTRY, MaintenanceLoop
from archon_search.store_filters import _sql_quote_str


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _make_collection_info(
    name: str,
    namespace: str = "default",
    doc_count: int = 1,
    chunk_count: int = 5,
) -> CollectionInfo:
    return CollectionInfo(
        name=name,
        doc_count=doc_count,
        chunk_count=chunk_count,
        namespace=namespace,
    )


def _make_loop(
    tmp_path: Path,
    *,
    interval_hours: int = 0,
    fts_optimize: bool = False,
    orphan_cleanup: bool = False,
    failed_ingest_retry: bool = False,
    prune_expired_chunks: bool = True,
    retry_max_attempts: int = 3,
    retry_max_age_hours: int = 72,
    exclude: list[str] | None = None,
    job_store: Any = None,
    search_store: Any = None,
    graph_store: Any = None,
) -> MaintenanceLoop:
    cfg = MaintenanceConfig(
        interval_hours=interval_hours,
        fts_optimize=fts_optimize,
        orphan_cleanup=orphan_cleanup,
        failed_ingest_retry=failed_ingest_retry,
        prune_expired_chunks=prune_expired_chunks,
        retry_max_attempts=retry_max_attempts,
        retry_max_age_hours=retry_max_age_hours,
        exclude=exclude or [],
    )
    js = job_store if job_store is not None else MagicMock()
    ss = search_store if search_store is not None else MagicMock()
    return MaintenanceLoop(
        job_store=js,
        search_store=ss,
        config=cfg,
        data_dir=tmp_path,
        graph_store=graph_store,
    )


def _make_health() -> dict[str, Any]:
    """Return a fresh per-collection health dict matching _EMPTY_HEALTH_ENTRY."""
    return dict(_EMPTY_HEALTH_ENTRY)


# ---------------------------------------------------------------------------
# Unit tests: _run_expired_chunk_pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_skips_when_disabled(tmp_path: Path) -> None:
    """prune_expired_chunks=False → store.prune_expired_chunks is never called."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=[])

    loop = _make_loop(tmp_path, prune_expired_chunks=False, search_store=ss)
    health = _make_health()
    loop._current_health = health  # type: ignore[attr-defined]

    await loop._run_expired_chunk_pruning("col", "default")

    ss.prune_expired_chunks.assert_not_called()
    assert health["expired_chunks_removed_last_run"] == 0


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_calls_store_per_collection(tmp_path: Path) -> None:
    """_run_expired_chunk_pruning calls store.prune_expired_chunks for each collection."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=[])
    ss.list_collections = AsyncMock(
        return_value=[
            _make_collection_info("col-a"),
            _make_collection_info("col-b"),
        ]
    )
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    assert ss.prune_expired_chunks.call_count == 2
    calls = {args[0] for args, _ in ss.prune_expired_chunks.call_args_list}
    assert calls == {"col-a", "col-b"}


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_updates_health_entry(tmp_path: Path) -> None:
    """After pruning, expired_chunks_removed_last_run is set to len(pruned_doc_ids)."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=["doc-a", "doc-b", "doc-c"])

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)
    health = _make_health()
    loop._current_health = health  # type: ignore[attr-defined]

    await loop._run_expired_chunk_pruning("col", "default")

    assert health["expired_chunks_removed_last_run"] == 3


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_logs_warning_with_doc_ids(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING log is emitted with the count and doc_ids when expired chunks are pruned (S6)."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=["doc-x", "doc-y"])

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)
    health = _make_health()
    loop._current_health = health  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_expired_chunk_pruning("col", "default")

    # There must be at least one WARNING record mentioning the pruned count and collection.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) >= 1
    # The log message must reference both doc_ids that were pruned.
    combined = " ".join(r.getMessage() for r in warning_records)
    assert "doc-x" in combined and "doc-y" in combined, (
        f"Expected both pruned doc_ids in WARNING log; got: {combined!r}"
    )


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_no_chunks_no_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No WARNING is logged when prune returns an empty list (nothing to prune)."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=[])

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)
    health = _make_health()
    loop._current_health = health  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_expired_chunk_pruning("col", "default")

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records == [], f"Expected no WARNING when nothing was pruned; got: {warning_records}"


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_calls_delete_defref_graph_by_doc_per_pruned_doc_id(
    tmp_path: Path,
) -> None:
    """BE-12: TTL pruning tears down doc-scoped def/ref graph rows per pruned doc."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=["doc-a", "doc-b", "doc-a"])
    graph_store = AsyncMock()
    graph_store.delete_defref_graph_by_doc = AsyncMock()

    loop = _make_loop(
        tmp_path,
        prune_expired_chunks=True,
        search_store=ss,
        graph_store=graph_store,
    )

    await loop._run_expired_chunk_pruning("col", "tenant-a")

    graph_store.delete_defref_graph_by_doc.assert_any_await(
        "col", "doc-a", "tenant-a", delete_doc_owned_code_symbols=True
    )
    graph_store.delete_defref_graph_by_doc.assert_any_await(
        "col", "doc-b", "tenant-a", delete_doc_owned_code_symbols=True
    )
    assert graph_store.delete_defref_graph_by_doc.await_count == 2


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_defref_cleanup_failure_logs_warning_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BE-12: def/ref cleanup is best-effort and never fails chunk pruning."""
    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=["doc-a"])
    graph_store = AsyncMock()
    graph_store.delete_defref_graph_by_doc = AsyncMock(side_effect=RuntimeError("graph down"))

    loop = _make_loop(
        tmp_path,
        prune_expired_chunks=True,
        search_store=ss,
        graph_store=graph_store,
    )

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_expired_chunk_pruning("col", "tenant-a")

    graph_store.delete_defref_graph_by_doc.assert_awaited_once_with(
        "col", "doc-a", "tenant-a", delete_doc_owned_code_symbols=True
    )
    combined = " ".join(record.getMessage() for record in caplog.records)
    assert "def/ref graph cleanup failed" in combined
    assert "doc-a" in combined


@pytest.mark.asyncio
async def test_ttl_prune_preserves_shared_defref_module_node_when_other_doc_references(
    tmp_path: Path,
) -> None:
    """BE-12: doc-owned module pseudo-nodes survive when another doc still references them."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    collection = "col"
    namespace = "tenant-a"
    expired_doc_id = "doc-expired"
    other_doc_id = "doc-other"
    module = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "__file_module__::/tmp/shared.py"),
        entity_name="<module>",
        entity_type=EntityType.code_symbol,
        source_doc_id=expired_doc_id,
        collection_name=collection,
        entity_subtype="python-defref-module",
    )
    expired_func = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "expired::/tmp/shared.py"),
        entity_name="expired",
        entity_type=EntityType.code_symbol,
        source_doc_id=expired_doc_id,
        collection_name=collection,
        entity_subtype="python-function",
    )
    other_func = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "other::/tmp/other.py"),
        entity_name="other",
        entity_type=EntityType.code_symbol,
        source_doc_id=other_doc_id,
        collection_name=collection,
        entity_subtype="python-function",
    )
    expired_edge = GraphEdge(
        id=make_stable_edge_id(module.id, expired_func.id, RelationshipType.defines.value),
        source_node_id=module.id,
        target_node_id=expired_func.id,
        relationship_type=RelationshipType.defines,
        source_doc_id=expired_doc_id,
        extraction_method="extracted",
    )
    other_edge = GraphEdge(
        id=make_stable_edge_id(other_func.id, module.id, RelationshipType.calls.value),
        source_node_id=other_func.id,
        target_node_id=module.id,
        relationship_type=RelationshipType.calls,
        source_doc_id=other_doc_id,
        extraction_method="extracted",
    )

    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=[expired_doc_id])
    graph_store = GraphStore(str(tmp_path / "graph-shared-module"))
    await graph_store.connect()
    try:
        await graph_store.ensure_graph_tables(collection, ns=namespace)
        await graph_store.write_graph(
            collection,
            [module, expired_func, other_func],
            [expired_edge, other_edge],
            ns=namespace,
        )

        loop = _make_loop(
            tmp_path,
            prune_expired_chunks=True,
            search_store=ss,
            graph_store=graph_store,
        )
        await loop._run_expired_chunk_pruning(collection, namespace)

        remaining_nodes = await graph_store.get_all_nodes(collection, ns=namespace)
        remaining_edges = await graph_store.get_all_edges(collection, ns=namespace)
        assert {node.id for node in remaining_nodes} == {module.id, other_func.id}
        assert {edge.id for edge in remaining_edges} == {other_edge.id}
    finally:
        await graph_store.disconnect()


@pytest.mark.asyncio
async def test_ttl_prune_removes_cross_doc_edge_to_expired_non_module_symbol(
    tmp_path: Path,
) -> None:
    """BE-12: only shared module pseudo-nodes are preserved across doc cleanup."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    collection = "col"
    namespace = "tenant-a"
    expired_doc_id = "doc-expired"
    other_doc_id = "doc-other"
    expired_symbol = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "target::/tmp/expired.py"),
        entity_name="target",
        entity_type=EntityType.code_symbol,
        source_doc_id=expired_doc_id,
        collection_name=collection,
        entity_subtype="python-function",
    )
    other_symbol = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "caller::/tmp/other.py"),
        entity_name="caller",
        entity_type=EntityType.code_symbol,
        source_doc_id=other_doc_id,
        collection_name=collection,
        entity_subtype="python-function",
    )
    cross_doc_edge = GraphEdge(
        id=make_stable_edge_id(other_symbol.id, expired_symbol.id, RelationshipType.calls.value),
        source_node_id=other_symbol.id,
        target_node_id=expired_symbol.id,
        relationship_type=RelationshipType.calls,
        source_doc_id=other_doc_id,
        extraction_method="inferred",
    )

    ss = AsyncMock()
    ss.prune_expired_chunks = AsyncMock(return_value=[expired_doc_id])
    graph_store = GraphStore(str(tmp_path / "graph-expired-symbol"))
    await graph_store.connect()
    try:
        await graph_store.ensure_graph_tables(collection, ns=namespace)
        await graph_store.write_graph(
            collection,
            [expired_symbol, other_symbol],
            [cross_doc_edge],
            ns=namespace,
        )

        loop = _make_loop(
            tmp_path,
            prune_expired_chunks=True,
            search_store=ss,
            graph_store=graph_store,
        )
        await loop._run_expired_chunk_pruning(collection, namespace)

        remaining_nodes = await graph_store.get_all_nodes(collection, ns=namespace)
        remaining_edges = await graph_store.get_all_edges(collection, ns=namespace)
        assert {node.id for node in remaining_nodes} == {other_symbol.id}
        assert remaining_edges == []
    finally:
        await graph_store.disconnect()


@pytest.mark.asyncio
async def test_run_one_pass_includes_prune_policy(tmp_path: Path) -> None:
    """_run_expired_chunk_pruning is called in the per-collection loop (after orphan cleanup)."""
    ss = AsyncMock()
    ss.list_collections = AsyncMock(return_value=[_make_collection_info("col")])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)

    fts_mock = AsyncMock()
    orphan_mock = AsyncMock()
    prune_mock = AsyncMock()
    retry_mock = AsyncMock()

    call_order: list[str] = []

    async def _fts(col: str, ns: str) -> None:
        call_order.append("fts")

    async def _orphan(col: str, ns: str) -> None:
        call_order.append("orphan")

    async def _prune(col: str, ns: str) -> None:
        call_order.append("prune")

    async def _retry(*args: Any, **kwargs: Any) -> None:
        call_order.append("retry")

    loop._run_fts_optimize = _fts  # type: ignore[method-assign]
    loop._run_orphan_cleanup = _orphan  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = _prune  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = _retry  # type: ignore[method-assign]

    await loop._run_one_pass()

    # All four policies must have been called.
    assert "prune" in call_order, "prune policy was not called"
    # The prune call must come after orphan cleanup and before the pass-level retry.
    prune_idx = call_order.index("prune")
    orphan_idx = call_order.index("orphan")
    retry_idx = call_order.index("retry")
    assert orphan_idx < prune_idx < retry_idx, (
        f"Expected order: orphan({orphan_idx}) < prune({prune_idx}) < retry({retry_idx})"
    )


@pytest.mark.asyncio
async def test_run_expired_chunk_pruning_continues_on_exception(tmp_path: Path) -> None:
    """Exception pruning one collection does not prevent pruning of other collections."""
    ss = AsyncMock()
    ss.list_collections = AsyncMock(
        return_value=[
            _make_collection_info("col-a"),
            _make_collection_info("col-b"),
        ]
    )
    ss.get_collection_meta = AsyncMock(return_value=None)

    pruned_cols: list[str] = []

    async def _fake_prune(col: str, ns: str) -> list[str]:
        if col == "col-a":
            raise RuntimeError("prune failed for col-a")
        pruned_cols.append(col)
        return []

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = _fake_prune  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()  # must not raise

    # col-b must still have been pruned despite col-a raising.
    assert "col-b" in pruned_cols, f"Expected col-b to be pruned; pruned_cols={pruned_cols}"


# ---------------------------------------------------------------------------
# State: last_expired_pruned_at written to pass-level state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_pass_writes_last_expired_pruned_at_when_enabled(
    tmp_path: Path,
) -> None:
    """When prune_expired_chunks=True, last_expired_pruned_at is written to the state file."""
    ss = AsyncMock()
    ss.list_collections = AsyncMock(return_value=[])

    loop = _make_loop(tmp_path, prune_expired_chunks=True, search_store=ss)
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state = json.loads((tmp_path / ".maintenance-state.json").read_text(encoding="utf-8"))
    assert "last_expired_pruned_at" in state, (
        "last_expired_pruned_at must be present in state file when policy is enabled"
    )
    assert state["last_expired_pruned_at"] is not None, (
        "last_expired_pruned_at must be non-null after a pass"
    )


@pytest.mark.asyncio
async def test_run_one_pass_preserves_last_expired_pruned_at_when_disabled(
    tmp_path: Path,
) -> None:
    """When prune_expired_chunks=False, the previous last_expired_pruned_at is preserved."""
    prev_ts = "2025-06-01T10:00:00+00:00"
    state_file = tmp_path / ".maintenance-state.json"
    state_file.write_text(
        json.dumps({
            "last_run_at": None,
            "next_run_at": None,
            "collection_health": {},
            "retry_counts": {},
            "last_expired_pruned_at": prev_ts,
        }),
        encoding="utf-8",
    )

    ss = AsyncMock()
    ss.list_collections = AsyncMock(return_value=[])

    loop = _make_loop(tmp_path, prune_expired_chunks=False, search_store=ss)
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state.get("last_expired_pruned_at") == prev_ts, (
        f"Expected preserved timestamp {prev_ts!r}; got {state.get('last_expired_pruned_at')!r}"
    )


# ---------------------------------------------------------------------------
# Integration test: real store + real MaintenanceLoop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_maintenance_loop_prune_deletes_expired_and_updates_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real maintenance loop pass: expired chunk is deleted and state is updated.

    Verifies:
    - S6: expired chunk deleted by _run_expired_chunk_pruning
    - State file gains last_expired_pruned_at (non-null) and
      collection_health[col].expired_chunks_removed_last_run >= 1
    """
    from datetime import timezone as _tz

    from archon_search._types import normalize_iso_utc
    from archon_search.jobs.store import JobStore
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col-prune", embedding_dim=4)

        # Run E2a migrations to add expires_at + scopes columns.
        pending = await store.pending_migrations("col-prune", "default")
        if pending:
            await store.apply_in_place_migrations("col-prune", "default", pending)

        # Insert one expired chunk and one non-expired chunk.
        now = datetime.now(_tz.utc)
        past_iso = normalize_iso_utc(now - timedelta(seconds=60))
        future_iso = normalize_iso_utc(now + timedelta(hours=1))

        db = store._require_connected()
        table = await db.open_table("col-prune")

        expired_doc_id = "e" * 64
        alive_doc_id = "a" * 64

        await table.add([
            {
                "doc_id": expired_doc_id,
                "chunk_id": expired_doc_id + "-000000",
                "text": "expired chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/expired.txt",
                "indexed_at": past_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": past_iso,
                "scopes": None,
            },
            {
                "doc_id": alive_doc_id,
                "chunk_id": alive_doc_id + "-000000",
                "text": "alive chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/alive.txt",
                "indexed_at": past_iso,
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": future_iso,
                "scopes": None,
            },
        ])

        # Create a real MaintenanceLoop.
        job_store = JobStore(path=tmp_path / "jobs.json")
        from archon_search.config import MaintenanceConfig

        cfg = MaintenanceConfig(
            interval_hours=0,
            fts_optimize=False,
            orphan_cleanup=False,
            failed_ingest_retry=False,
            prune_expired_chunks=True,
        )
        loop = MaintenanceLoop(
            job_store=job_store,
            search_store=store,
            config=cfg,
            data_dir=tmp_path,
        )

        # Run one maintenance pass.
        await loop._run_one_pass()

        # Verify the expired chunk is gone and the alive chunk remains.
        remaining = await store.count_chunks("col-prune", "default")
        assert remaining == 1, f"Expected 1 remaining chunk after prune; got {remaining}"

        # Verify the alive chunk (not the expired one) survived.
        alive_rows = (
            await table.query()
            .where("doc_id = " + _sql_quote_str(alive_doc_id))
            .select(["doc_id"])
            .to_list()
        )
        assert len(alive_rows) == 1, f"Expected alive chunk to survive pruning; got {alive_rows}"

        # Verify state file contains last_expired_pruned_at.
        state_file = tmp_path / ".maintenance-state.json"
        assert state_file.exists(), ".maintenance-state.json must exist after the pass"
        state = json.loads(state_file.read_text(encoding="utf-8"))

        assert state.get("last_expired_pruned_at") is not None, (
            "last_expired_pruned_at must be set in state after a prune pass"
        )

        # Verify collection health entry.
        col_key = "default/col-prune"
        col_health = state.get("collection_health", {}).get(col_key, {})
        assert col_health.get("expired_chunks_removed_last_run") >= 1, (
            f"Expected expired_chunks_removed_last_run >= 1; got {col_health}"
        )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ttl_expiry_maintenance_prune_removes_defref_graph_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-12: maintenance TTL pruning removes def/ref rows for the expired doc."""
    from datetime import timezone as _tz

    from archon_search._types import normalize_iso_utc
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    collection = "col-defref-prune"
    namespace = "default"
    expired_doc_id = "d" * 64
    alive_doc_id = "b" * 64
    now = datetime.now(_tz.utc)
    past_iso = normalize_iso_utc(now - timedelta(seconds=60))
    future_iso = normalize_iso_utc(now + timedelta(hours=1))

    store = SearchStore(tmp_path / "db")
    graph_store = GraphStore(str(tmp_path / "graph"))
    await store.connect()
    await graph_store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection(collection, embedding_dim=4)
        pending = await store.pending_migrations(collection, namespace)
        if pending:
            await store.apply_in_place_migrations(collection, namespace, pending)

        db = store._require_connected()
        table = await db.open_table(collection)
        await table.add([
            {
                "doc_id": expired_doc_id,
                "chunk_id": expired_doc_id + "-000000",
                "text": "expired code chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/expired.py",
                "indexed_at": past_iso,
                "file_type": ".py",
                "language": "python",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": past_iso,
                "scopes": None,
            },
            {
                "doc_id": alive_doc_id,
                "chunk_id": alive_doc_id + "-000000",
                "text": "alive code chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/alive.py",
                "indexed_at": past_iso,
                "file_type": ".py",
                "language": "python",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": future_iso,
                "scopes": None,
            },
        ])

        await graph_store.ensure_graph_tables(collection, ns=namespace)
        caller = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "caller::/tmp/expired.py"),
            entity_name="caller",
            entity_type=EntityType.code_symbol,
            source_doc_id=expired_doc_id,
            collection_name=collection,
            entity_subtype="python-function",
        )
        callee = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "callee::/tmp/expired.py"),
            entity_name="callee",
            entity_type=EntityType.code_symbol,
            source_doc_id=expired_doc_id,
            collection_name=collection,
            entity_subtype="python-function",
        )
        edge = GraphEdge(
            id=make_stable_edge_id(caller.id, callee.id, RelationshipType.calls.value),
            source_node_id=caller.id,
            target_node_id=callee.id,
            relationship_type=RelationshipType.calls,
            source_doc_id=expired_doc_id,
            extraction_method="extracted",
        )
        alive_caller = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "alive_caller::/tmp/alive.py"),
            entity_name="alive_caller",
            entity_type=EntityType.code_symbol,
            source_doc_id=alive_doc_id,
            collection_name=collection,
            entity_subtype="python-function",
        )
        alive_callee = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "alive_callee::/tmp/alive.py"),
            entity_name="alive_callee",
            entity_type=EntityType.code_symbol,
            source_doc_id=alive_doc_id,
            collection_name=collection,
            entity_subtype="python-function",
        )
        alive_edge = GraphEdge(
            id=make_stable_edge_id(
                alive_caller.id, alive_callee.id, RelationshipType.calls.value
            ),
            source_node_id=alive_caller.id,
            target_node_id=alive_callee.id,
            relationship_type=RelationshipType.calls,
            source_doc_id=alive_doc_id,
            extraction_method="extracted",
        )
        await graph_store.write_graph(
            collection,
            [caller, callee, alive_caller, alive_callee],
            [edge, alive_edge],
            ns=namespace,
        )

        loop = _make_loop(
            tmp_path,
            prune_expired_chunks=True,
            search_store=store,
            graph_store=graph_store,
        )
        await loop._run_one_pass()

        assert await store.count_chunks(collection, namespace) == 1
        remaining_edges = await graph_store.get_all_edges(collection, ns=namespace)
        remaining_nodes = await graph_store.get_all_nodes(collection, ns=namespace)
        assert {edge.id for edge in remaining_edges} == {alive_edge.id}
        assert {node.id for node in remaining_nodes} == {alive_caller.id, alive_callee.id}
    finally:
        await graph_store.disconnect()
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ttl_prune_keeps_defref_graph_when_doc_still_has_live_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE-12: doc-scoped def/ref cleanup is skipped while any chunk for that doc remains."""
    from datetime import timezone as _tz

    from archon_search._types import normalize_iso_utc
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    collection = "col-defref-mixed-prune"
    namespace = "default"
    doc_id = "c" * 64
    now = datetime.now(_tz.utc)
    past_iso = normalize_iso_utc(now - timedelta(seconds=60))
    future_iso = normalize_iso_utc(now + timedelta(hours=1))

    store = SearchStore(tmp_path / "db")
    graph_store = GraphStore(str(tmp_path / "graph"))
    await store.connect()
    await graph_store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection(collection, embedding_dim=4)
        pending = await store.pending_migrations(collection, namespace)
        if pending:
            await store.apply_in_place_migrations(collection, namespace, pending)

        db = store._require_connected()
        table = await db.open_table(collection)
        await table.add([
            {
                "doc_id": doc_id,
                "chunk_id": doc_id + "-000000",
                "text": "expired code chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/mixed.py",
                "indexed_at": past_iso,
                "file_type": ".py",
                "language": "python",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": past_iso,
                "scopes": None,
            },
            {
                "doc_id": doc_id,
                "chunk_id": doc_id + "-000001",
                "text": "live code chunk",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/mixed.py",
                "indexed_at": past_iso,
                "file_type": ".py",
                "language": "python",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "test",
                "updated_at": past_iso,
                "acl": None,
                "expires_at": future_iso,
                "scopes": None,
            },
        ])

        await graph_store.ensure_graph_tables(collection, ns=namespace)
        caller = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "caller::/tmp/mixed.py"),
            entity_name="caller",
            entity_type=EntityType.code_symbol,
            source_doc_id=doc_id,
            collection_name=collection,
            entity_subtype="python-function",
        )
        callee = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "callee::/tmp/mixed.py"),
            entity_name="callee",
            entity_type=EntityType.code_symbol,
            source_doc_id=doc_id,
            collection_name=collection,
            entity_subtype="python-function",
        )
        edge = GraphEdge(
            id=make_stable_edge_id(caller.id, callee.id, RelationshipType.calls.value),
            source_node_id=caller.id,
            target_node_id=callee.id,
            relationship_type=RelationshipType.calls,
            source_doc_id=doc_id,
            extraction_method="extracted",
        )
        await graph_store.write_graph(collection, [caller, callee], [edge], ns=namespace)

        loop = _make_loop(
            tmp_path,
            prune_expired_chunks=True,
            search_store=store,
            graph_store=graph_store,
        )
        await loop._run_one_pass()

        assert await store.count_chunks(collection, namespace) == 1
        remaining_edges = await graph_store.get_all_edges(collection, ns=namespace)
        remaining_nodes = await graph_store.get_all_nodes(collection, ns=namespace)
        assert {item.id for item in remaining_edges} == {edge.id}
        assert {item.id for item in remaining_nodes} == {caller.id, callee.id}
    finally:
        await graph_store.disconnect()
        await store.disconnect()

"""E2d T-4: e2e tests for community rebuild lifecycle after graph GC.

Scenarios covered:
- S4: When GC removes ≥1 node, communities table is cleared and an async rebuild is enqueued.
- S9: When rebuild from S4 is still in-flight and a second GC fires, it detects the
      existing in-flight task and does NOT spawn a new one.
- S10: CPU priority reduction degrades gracefully when os.setpriority() raises
       (tested via manual checklist only — see MANUAL_TEST_CHECKLIST below).

Test:
  test_e2d_t4_communities_invalidated_then_rebuilt_after_gc:
    Two-pass sequence:
    Pass 1: GC removes orphan node (D1 deleted, D2 survives) → communities_invalidated=True
    Await rebuild task completion (poll _rebuild_state until task done).
    Pass 2: communities_invalidated=False AND community_count >= 1.

Design notes:
  - Two-document pattern (D1=Alice+Google, D2=Bob) — see learnings.md for rationale.
  - leidenalg/igraph not available → CommunityBuilder.build() is monkeypatched to write
    a synthetic community directly via GraphStore, making community_count >= 1 verifiable.
  - Baseline community_count == N asserted before deletion (step (a) per task spec).
  - communities_invalidated is in GET /status → graph.collections[col].communities_invalidated
  - community_count is in GET /status → collections[col].community_count

Run with:
    uv run pytest tests/integration/test_e2d_t4_communities_rebuild_after_gc.py -n0 -v --no-cov

MANUAL_TEST_CHECKLIST — CPU priority degradation (S10):
  This checklist covers the scenario where os.setpriority() is unavailable or raises.
  Tests CANNOT be automated (requires a real Linux environment without CAP_SYS_NICE).

  Prerequisites:
    - Linux host (os.setpriority is Linux-only; see _rebuild_communities_async sys.platform guard)
    - A user account WITHOUT CAP_SYS_NICE capability (most regular Linux users)
    - archon-search installed and configured with graph.enabled=true
    - At least one collection with graph data + built communities

  Steps:
    1. Start the server: `archon-search serve`
    2. Ingest a document with graph extraction enabled.
    3. Ingest a second document with a DIFFERENT entity (two-doc pattern).
    4. Delete the first document via MCP tool `delete_document`.
    5. POST /maintenance/trigger to run a GC pass.
    6. Observe server logs for the following WARNING (verify it is present, not absent):
         "MaintenanceLoop: could not set CPU priority to <N> for community rebuild <ns>/<col>: <err>"
       This WARNING is emitted by _rebuild_communities_async when os.setpriority() raises
       PermissionError or OSError.
    7. Wait for the rebuild task to complete (poll GET /status until
       graph.collections[col].communities_invalidated == False).
    8. Assert GET /status shows community_count >= 1 (rebuild completed despite priority failure).
    9. Confirm no ERROR-level log entry was emitted for the rebuild (only WARNING for priority).

  Pass criteria:
    - WARNING log present for the setpriority failure (step 6).
    - Rebuild completes normally (steps 7-8).
    - No ERROR log from the rebuild task itself (step 9).

  Known good code path (reference):
    archon_search/jobs/maintenance_loop.py → _rebuild_communities_async():
      Lines: ``try: os.setpriority(...) except (OSError, AttributeError) as exc: logger.warning(...)``
      The try/except ensures continuation after permission failure.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.integration.conftest import (
    ingest_file_via_path,
    install_spacy_stub,
    make_real_app,
)
from tests.integration.test_e2d_t2_graph_gc_e2e import (
    _auth,
    _delete_document_and_mentions,
    _get_all_node_names,
    _get_mention_count,
    _trigger_and_poll_maintenance,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S: float = 30.0
_POLL_INTERVAL_S: float = 0.1
_REBUILD_POLL_TIMEOUT_S: float = 20.0


# ---------------------------------------------------------------------------
# Helper: write a community directly to the GraphStore (bypasses leidenalg)
# ---------------------------------------------------------------------------


async def _write_community_to_store(
    db_path: str,
    collection: str,
    ns: str,
    *,
    community_id: str = "t4-comm-1",
    entity_ids: list[str] | None = None,
    representative_chunk_ids: list[str] | None = None,
) -> None:
    """Write a synthetic community to GraphStore without leidenalg.

    Used both to seed the baseline community (pre-deletion) and as the
    async rebuild stub via monkeypatch.
    """
    from datetime import datetime, timezone

    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_communities_table(collection, ns=ns)
        community = Community(
            community_id=community_id,
            entity_ids=entity_ids or ["entity-bob"],
            representative_chunk_ids=representative_chunk_ids or [],
            built_at=datetime.now(timezone.utc),
            summary_text=None,
        )
        await gs.write_communities(collection, [community], ns=ns)
    finally:
        await gs.disconnect()


async def _get_community_count(db_path: str, collection: str, ns: str) -> int:
    """Return community count via GraphStore.get_community_stats."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        count, _ = await gs.get_community_stats(collection, ns=ns)
        return count
    finally:
        await gs.disconnect()


async def _get_community_ids_from_store(
    db_path: str, collection: str, ns: str
) -> list[str]:
    """Return all community_ids from the communities table for (collection, ns)."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        table_name = gs._communities_table_name(collection, ns=ns)
        try:
            table = await gs._db.open_table(table_name)
            rows = await table.query().select(["community_id"]).to_list()
            return [r["community_id"] for r in rows]
        except Exception:
            return []
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# Helper: poll rebuild state until task completes or timeout
# ---------------------------------------------------------------------------


def _wait_for_rebuild_completion(
    client,
    col: str,
    ns: str = "default",
    *,
    deadline_s: float = _REBUILD_POLL_TIMEOUT_S,
) -> None:
    """Poll maintenance_loop._rebuild_state until the rebuild task for (ns, col) completes.

    The done-callback sets RebuildState.completed=True once the task finishes.
    On timeout: fails the test with a descriptive message.
    """
    maintenance_loop = client.app.state.maintenance_loop
    rebuild_key = (ns, col)

    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        rebuild_state = maintenance_loop._rebuild_state.get(rebuild_key)
        if rebuild_state is None:
            # Rebuild task was never started (communities_invalidated was False)
            # or the state entry was cleaned up before we polled — treat as done.
            return
        if rebuild_state.completed:
            # Done-callback has fired and set completed=True — safe for Pass 2.
            return
        # Note: do NOT return on task.done() alone. The done-callback (which sets
        # completed=True) fires asynchronously in the TestClient's event loop thread
        # AFTER task.done() becomes True. If we return here before the callback fires,
        # Pass 2 runs with completed=False and communities_invalidated stays True.
        time.sleep(_POLL_INTERVAL_S)

    rebuild_state = maintenance_loop._rebuild_state.get(rebuild_key)
    exc_info = None
    if rebuild_state and rebuild_state.task.done():
        try:
            exc_info = rebuild_state.task.exception()
        except Exception as e:
            # Task done but exception() raised (e.g., CancelledError).
            # This is already unusual; log the error so diagnostic message is helpful.
            exc_info = f"<exception retrieval failed: {type(e).__name__}>"
    pytest.fail(
        f"Rebuild task for {ns}/{col} did not complete within {deadline_s}s. "
        f"rebuild_state={rebuild_state!r}. "
        f"task exception={exc_info!r}"
    )


# ---------------------------------------------------------------------------
# Helper: get communities_invalidated for a collection from GET /status
# ---------------------------------------------------------------------------


def _get_communities_invalidated(status_json: dict, col: str) -> bool:
    """Extract communities_invalidated for the named collection from status JSON.

    Reads from status.graph.collections[col].communities_invalidated.
    Raises ValueError when the graph block is absent or the collection is not found,
    so a vacuous pass is caught immediately instead of silently returning False.
    """
    graph_block = status_json.get("graph")
    if not graph_block:
        raise ValueError(
            "graph block missing in status response — is graph.enabled=true in the test app config?"
        )
    for col_entry in graph_block.get("collections", []):
        if col_entry.get("collection") == col:
            return bool(col_entry.get("communities_invalidated", False))
    found = [c.get("collection") for c in graph_block.get("collections", [])]
    raise ValueError(
        f"Collection '{col}' not found in status.graph.collections; found: {found}"
    )


def _get_community_count_from_status(status_json: dict, col: str) -> int:
    """Extract community_count for the named collection from status JSON.

    Reads from status.collections[col].community_count.
    Returns 0 when the collection is not found.
    """
    for col_entry in status_json.get("collections", []):
        if col_entry.get("name") == col:
            return int(col_entry.get("community_count", 0))
    return 0


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_e2d_t4_communities_invalidated_then_rebuilt_after_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two-pass sequence: Pass 1 → communities_invalidated=True; Pass 2 → invalidated=False, count>=1.

    Scenario S4 + S9 (S10 is manual-only; see MANUAL_TEST_CHECKLIST in module docstring).

    Two-document design:
      D1: contains "Alice" (+ "Google" from stub) — the target orphan after deletion.
      D2: contains "Bob" only — survives deletion, keeps mentions table non-empty.
      Deleting D1 leaves D2's entities alive → GC finds orphan Alice/Google nodes → GC runs.

    CommunityBuilder.build() stub:
      leidenalg/igraph are not installed. The async rebuild task calls CommunityBuilder.build(),
      which lazy-imports leidenalg and would raise ImportError. To make the rebuild succeed and
      produce a real community (so community_count>=1 is checkable), we monkeypatch
      CommunityBuilder.build to write a synthetic community directly via GraphStore.
      This is done via unittest.mock.patch as a context manager around the entire test body.

    Pass sequence:
      (a) BEFORE deletion: assert community_count == 1 (baseline from seed).
      Step 1: Ingest D1 + D2 with graph enabled.
      Step 2: Seed baseline community so community_count = 1 before deletion.
      Step 3: Assert community_count == 1 via GET /status (step (a) per task spec).
      Step 4: Delete D1. D2's Bob mention rows keep the table non-empty.
      Pass 1 (S4): POST /maintenance/trigger → wait → assert communities_invalidated=True.
                   Capture the rebuild task reference.
      Pass 1.5 (S9): POST /maintenance/trigger immediately (rebuild may still be in-flight).
                     If in-flight, assert no new task spawned (pending flag set instead).
      Step 5: Wait for rebuild task to complete (poll _rebuild_state).
              Assert task raised no exception (Fix #1).
      Pass 2: POST /maintenance/trigger → wait → assert communities_invalidated=False
              AND community_count >= 1 AND t4-rebuilt-comm in GraphStore (Fix #2).
    """
    install_spacy_stub(monkeypatch)

    col = "t4-communities-rebuild-col"
    ns = "default"

    # D1: "Alice" + "Google" from the spaCy stub.
    doc_d1 = tmp_path / "t4_doc_d1.txt"
    doc_d1.write_text(
        "Alice works at Google Corp. Alice is a senior engineer.\n" * 10,
        encoding="utf-8",
    )
    doc1_id = hashlib.sha256(str(doc_d1.resolve()).encode()).hexdigest()

    # D2: "Bob" only — the spaCy stub returns Bob when text contains "Bob".
    doc_d2 = tmp_path / "t4_doc_d2.txt"
    doc_d2.write_text(
        "Bob is a junior developer. Bob joined last month.\n" * 10,
        encoding="utf-8",
    )

    # Stub for CommunityBuilder.build(): writes a synthetic community directly.
    # This replaces the real Leiden-based clustering which requires leidenalg.
    # The stub uses the db_path captured at runtime via a closure so it can open
    # its own GraphStore connection independently.
    db_path_holder: list[str] = []  # filled once cfg is available

    async def _fake_builder_build(self_builder, collection: str, ns: str) -> list:
        """Write a synthetic Bob community via the reusable helper; return one Community."""
        from archon_search.graph_store import GraphStore

        db_path = db_path_holder[0]
        await _write_community_to_store(
            db_path, collection, ns, community_id="t4-rebuilt-comm"
        )
        # Return the written community by querying it back (simulates what CommunityBuilder does)
        gs = GraphStore(db_path)
        await gs.connect()
        try:
            communities, _ = await gs.get_community_stats(collection, ns=ns)
            # For stub purposes, return a minimal list — the rebuild task doesn't validate it
            return [None] * communities if communities > 0 else []
        finally:
            await gs.disconnect()

    with patch(
        "archon_search.community_builder.CommunityBuilder.build",
        new=_fake_builder_build,
    ):
        with make_real_app(
            tmp_path, monkeypatch, graph_enabled=True, maintenance_enabled=True
        ) as (client, cfg, api_key):

            # Populate db_path for the stub closure.
            db_path_holder.append(cfg.db_path)

            # -----------------------------------------------------------------------
            # Step 1: Ingest D1 (Alice+Google) and D2 (Bob).
            # -----------------------------------------------------------------------
            ingest_file_via_path(client, col, str(doc_d1), api_key=api_key)
            ingest_file_via_path(client, col, str(doc_d2), api_key=api_key)

            # Sanity: Alice and Bob are in graph nodes.
            node_names_after_ingest = asyncio.run(
                _get_all_node_names(cfg.db_path, col, ns)
            )
            assert any(n == "Alice" for n in node_names_after_ingest), (
                f"Expected 'Alice' in graph nodes after ingest; found: {node_names_after_ingest}. "
                "spaCy stub may not have run — check graph_enabled and stub installation."
            )
            assert any(n == "Bob" for n in node_names_after_ingest), (
                f"Expected 'Bob' in graph nodes after ingest; found: {node_names_after_ingest}. "
                "spaCy stub did not extract 'Bob' from D2 text."
            )

            # -----------------------------------------------------------------------
            # Step 2: Seed baseline community (community_count = 1 before deletion).
            # -----------------------------------------------------------------------
            asyncio.run(
                _write_community_to_store(cfg.db_path, col, ns, community_id="t4-baseline-comm")
            )

            # -----------------------------------------------------------------------
            # Step 3 (a): Assert community_count == 1 before deletion (baseline).
            # -----------------------------------------------------------------------
            # Status reads community_count live from GraphStore, so no maintenance
            # trigger required to see the baseline.
            auth_headers = _auth(api_key)
            status_baseline = client.get("/status", headers=auth_headers)
            assert status_baseline.status_code == 200, (
                f"GET /status failed before deletion: {status_baseline.status_code}"
            )
            baseline_json = status_baseline.json()
            community_count_baseline = _get_community_count_from_status(baseline_json, col)
            assert community_count_baseline == 1, (
                f"Expected community_count == 1 after seeding baseline community; "
                f"got {community_count_baseline}. "
                f"collections in status: {[c.get('name') for c in baseline_json.get('collections', [])]}"
            )

            # -----------------------------------------------------------------------
            # Step 4: Delete D1 (vector chunks + graph mention rows).
            # D2's Bob mentions keep the table non-empty.
            # -----------------------------------------------------------------------
            asyncio.run(
                _delete_document_and_mentions(cfg.db_path, col, doc1_id, ns)
            )

            # Verify: mention table non-empty (D2's Bob rows survived).
            mention_count_after_delete = asyncio.run(
                _get_mention_count(cfg.db_path, col, ns)
            )
            assert mention_count_after_delete > 0, (
                f"Expected > 0 mention rows after deleting D1 (D2's 'Bob' rows must survive); "
                f"got {mention_count_after_delete}. "
                "delete_mentions_by_doc may have removed D2's rows too, triggering GC safety guard."
            )

            # -----------------------------------------------------------------------
            # Pass 1: POST /maintenance/trigger → wait → assert communities_invalidated=True.
            # -----------------------------------------------------------------------
            status_pass1 = _trigger_and_poll_maintenance(
                client, api_key, prev_last_run_at=None
            )

            communities_invalidated_pass1 = _get_communities_invalidated(status_pass1, col)
            assert communities_invalidated_pass1 is True, (
                f"Expected communities_invalidated=True after Pass 1 GC (orphan node removed). "
                f"Got communities_invalidated={communities_invalidated_pass1!r}. "
                f"graph.collections in status: {status_pass1.get('graph', {}).get('collections', [])}. "
                "GC may not have found any orphan nodes to remove — check the two-doc setup and "
                "delete_orphan_nodes_and_edges (BE-5)."
            )

            # Verify rebuild task was spawned by checking _rebuild_state.
            rebuild_key = (ns, col)
            maintenance_loop = client.app.state.maintenance_loop
            assert rebuild_key in maintenance_loop._rebuild_state, (
                f"Expected rebuild task to be spawned for {ns}/{col} after communities_invalidated=True. "
                f"_rebuild_state keys: {list(maintenance_loop._rebuild_state.keys())}. "
                "communities_invalidated was True but _spawn_rebuild_task was not called."
            )

            # -----------------------------------------------------------------------
            # S9: Second GC while rebuild still in-flight must NOT spawn a new task.
            # Capture the task spawned in Pass 1. Check if it is still in-flight RIGHT NOW
            # before triggering Pass 1.5. If it is, trigger Pass 1.5 and assert the same task
            # object is still in _rebuild_state (pending=True was set, no new spawn).
            # If the task already completed (fast stub path), skip the S9 in-flight assertion —
            # S4 + Pass 2 still exercise the full sequence.
            # -----------------------------------------------------------------------
            task_after_pass1 = maintenance_loop._rebuild_state[rebuild_key].task
            task_was_in_flight_before_s9 = not task_after_pass1.done()

            last_run_at_before_s9 = status_pass1.get("maintenance", {}).get("last_run_at")
            # Trigger Pass 1.5 — do NOT wait for the rebuild before triggering.
            status_s9 = _trigger_and_poll_maintenance(
                client, api_key, prev_last_run_at=last_run_at_before_s9
            )

            # S9 assertion: if the task was in-flight when Pass 1.5 fired, no new task should
            # have been spawned (the GC must set pending=True on the existing state entry).
            if task_was_in_flight_before_s9:
                rebuild_state_after_s9 = maintenance_loop._rebuild_state.get(rebuild_key)
                if rebuild_state_after_s9 is not None:
                    assert rebuild_state_after_s9.task is task_after_pass1, (
                        "S9 violated: a new rebuild task was spawned while the original was still "
                        f"in-flight. Original task: {task_after_pass1!r}. "
                        f"New task: {rebuild_state_after_s9.task!r}. "
                        "_run_graph_gc must set pending=True instead of calling _spawn_rebuild_task."
                    )

            # -----------------------------------------------------------------------
            # Step 5: Wait for rebuild task to complete.
            # The done-callback sets RebuildState.completed=True.
            # Pass 2 must not run before rebuild completes — the status would still show
            # communities_invalidated=True (the next GC pass clears it by reading completed=True).
            # -----------------------------------------------------------------------
            _wait_for_rebuild_completion(client, col, ns)

            # Fix #1 (CRITICAL): assert rebuild task did not raise an exception.
            rebuild_state = maintenance_loop._rebuild_state.get(rebuild_key)
            if rebuild_state is not None and rebuild_state.task.done():
                exc = rebuild_state.task.exception()
                assert exc is None, f"Rebuild task raised exception: {exc}"

            # -----------------------------------------------------------------------
            # Pass 2: POST /maintenance/trigger → wait → assert communities_invalidated=False
            #         AND community_count >= 1 AND t4-rebuilt-comm exists in GraphStore.
            # -----------------------------------------------------------------------
            last_run_at_s9 = status_s9.get("maintenance", {}).get("last_run_at")
            status_pass2 = _trigger_and_poll_maintenance(
                client, api_key, prev_last_run_at=last_run_at_s9
            )

            communities_invalidated_pass2 = _get_communities_invalidated(status_pass2, col)
            assert communities_invalidated_pass2 is False, (
                f"Expected communities_invalidated=False after Pass 2 (rebuild completed). "
                f"Got communities_invalidated={communities_invalidated_pass2!r}. "
                f"graph.collections in status: {status_pass2.get('graph', {}).get('collections', [])}. "
                "The done-callback must set RebuildState.completed=True and the next GC pass "
                "must clear communities_invalidated when completed is True and no rebuild is in flight."
            )

            community_count_pass2 = _get_community_count_from_status(status_pass2, col)
            assert community_count_pass2 >= 1, (
                f"Expected community_count >= 1 after rebuild in Pass 2. "
                f"Got community_count={community_count_pass2}. "
                f"collections in status: {status_pass2.get('collections', [])}. "
                "CommunityBuilder.build() stub must write at least one community to GraphStore. "
                "Check _fake_builder_build and write_communities."
            )

            # Fix #2 (CRITICAL): verify t4-rebuilt-comm exists — proves the rebuild stub
            # actually ran and wrote a new community, not just the baseline t4-baseline-comm.
            community_ids_after_rebuild = asyncio.run(
                _get_community_ids_from_store(cfg.db_path, col, ns)
            )
            assert "t4-rebuilt-comm" in community_ids_after_rebuild, (
                f"Expected 't4-rebuilt-comm' in GraphStore communities after rebuild; "
                f"found: {community_ids_after_rebuild}. "
                "This confirms the rebuild stub ran, not just the baseline seeding. "
                "If only 't4-baseline-comm' is present, the rebuild task did not execute."
            )

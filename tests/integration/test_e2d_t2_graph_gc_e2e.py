"""E2d T-2: e2e tests for graph GC maintenance trigger, orphan node cleanup, and status fields.

Scenarios covered:
- S2: Deleted document's nodes/edges with zero remaining mentions are removed by GC.
- S3: Stale mention rows (chunk_id no longer in vector store) are pruned by GC;
      subsequent status shows reduced stale_mention_count.
- S5: GET /status graph.stale_mention_count is a cached integer from the state file;
      maintenance.last_graph_gc_at is an ISO-8601 timestamp after any GC pass.

Tests:
1. test_e2d_t2_gc_removes_orphan_nodes_visible_in_graph:
   Ingest doc with graph → delete doc (removes mention rows via BE-4) →
   POST /maintenance/trigger → poll GET /graph/{col} →
   assert orphan node absent from graph response.

2. test_e2d_t2_gc_status_fields_populated:
   Create stale mentions (ingest + delete doc, leaving orphan graph rows) →
   POST trigger (pass 1) → GET /status → graph.stale_mention_count > 0 (BEFORE prune)
   OR == 0 (stale mentions were pruned in pass 1) — assert last_graph_gc_at is ISO string.
   Ingest fresh doc (no deletions) → POST trigger (pass 2) → GET /status →
   stale_mention_count == 0 (clean state after stable pass).
   Two-sided: before/after comparison shows the count can be observed, not just typed.

Run with:
    uv run pytest tests/integration/test_e2d_t2_graph_gc_e2e.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S: float = 20.0
_POLL_INTERVAL_S: float = 0.1


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# spaCy stub — returns entities based on names present in the input text.
# Recognises: "Alice" → PERSON, "Bob" → PERSON, "Google" → ORG.
# Must be installed BEFORE make_real_app(graph_enabled=True) because create_app
# calls _check_graph_deps which imports spaCy synchronously.
# ---------------------------------------------------------------------------


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list) -> None:
            self.ents = ents

    _ENTITY_MAP = [
        ("Alice", "PERSON"),
        ("Bob", "PERSON"),
        ("Google", "ORG"),
    ]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents = [
                _FakeEnt(name, label)
                for name, label in _ENTITY_MAP
                if name in text
            ]
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


# ---------------------------------------------------------------------------
# Maintenance trigger + poll helper
# ---------------------------------------------------------------------------


def _trigger_and_poll_maintenance(
    client,
    api_key: str,
    *,
    prev_last_run_at: str | None = None,
    deadline_s: float = _POLL_TIMEOUT_S,
) -> dict:
    """POST /maintenance/trigger and poll GET /status until a NEW pass completes.

    ``prev_last_run_at``: value of maintenance.last_run_at from a previous pass.
    Polls until last_run_at is non-null AND differs from prev_last_run_at.
    Returns the full status JSON dict. Fails the test on timeout.
    """
    resp = client.post("/maintenance/trigger", headers=_auth(api_key))
    assert resp.status_code == 202, (
        f"POST /maintenance/trigger failed: {resp.status_code} {resp.text}"
    )

    deadline = time.monotonic() + deadline_s
    last_status: dict | None = None
    while time.monotonic() < deadline:
        r = client.get("/status", headers=_auth(api_key))
        assert r.status_code == 200, f"GET /status failed: {r.status_code} {r.text}"
        last_status = r.json()
        maint = last_status.get("maintenance")
        current_run_at = maint.get("last_run_at") if maint else None
        if current_run_at is not None and current_run_at != prev_last_run_at:
            return last_status
        time.sleep(_POLL_INTERVAL_S)

    pytest.fail(
        f"Maintenance pass did not complete within {deadline_s}s. "
        f"prev_last_run_at={prev_last_run_at!r}; "
        f"last status: {last_status}"
    )


# ---------------------------------------------------------------------------
# Graph read helpers
# ---------------------------------------------------------------------------


async def _get_all_node_names(db_path: str, collection: str, ns: str) -> list[str]:
    """Return list of entity_name values from graph nodes table."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        nodes = await gs.get_all_nodes(collection, ns=ns)
        return [n.entity_name for n in nodes]
    finally:
        await gs.disconnect()


async def _get_all_edges(db_path: str, collection: str, ns: str):
    """Return list of GraphEdge objects from the graph edges table."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        return await gs.get_all_edges(collection, ns=ns)
    finally:
        await gs.disconnect()


async def _get_mention_count(db_path: str, collection: str, ns: str) -> int:
    """Return total mention row count."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        mentions = await gs.get_all_mentions(collection, ns=ns)
        return len(mentions)
    finally:
        await gs.disconnect()


async def _delete_document_and_mentions(
    db_path: str,
    collection: str,
    doc_id: str,
    ns: str,
) -> None:
    """Delete vector chunks + graph mention rows for doc_id.

    Mirrors the two operations pipeline.delete_document performs:
      store.delete_document → graph_store.delete_mentions_by_doc
    Uses fresh connections independent of the server's event loop (asyncio.run safe).
    """
    from archon_search.graph_store import GraphStore
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await store.delete_document(collection, doc_id, namespace=ns)
        await gs.delete_mentions_by_doc(collection, doc_id, ns=ns)
    finally:
        await store.disconnect()
        await gs.disconnect()


async def _write_stale_mentions_directly(
    db_path: str,
    collection: str,
    ns: str,
    entity_name: str,
    stale_chunk_ids: list[str],
) -> None:
    """Write orphan mention rows whose chunk_ids do NOT exist in the vector store.

    These simulate S3: TTL-expired chunks were pruned from the vector store but
    their mention rows were not removed — a real stale-mention scenario.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphMention,
        GraphNode,
        make_stable_entity_id,
    )

    entity_id = make_stable_entity_id(EntityType.concept.value, entity_name.lower())

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        node = GraphNode(
            id=entity_id,
            entity_name=entity_name,
            entity_type=EntityType.concept,
            source_doc_id="synthetic-doc",
            collection_name=collection,
        )
        await gs.write_graph(collection, [node], [], ns=ns)
        mentions = [
            GraphMention(entity_id=entity_id, chunk_id=cid, doc_id="synthetic-doc")
            for cid in stale_chunk_ids
        ]
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# Test 1: test_e2d_t2_gc_removes_orphan_nodes_visible_in_graph
# ---------------------------------------------------------------------------


def test_e2d_t2_gc_removes_orphan_nodes_visible_in_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GC pass removes an orphan node that was left behind after document deletion.

    Scenario S2 + S7 final assertion (with BE-5 + BE-7 now implemented).

    Two-document design (required to bypass the empty-mentions-table safety guard):
    GraphStore.delete_orphan_nodes_and_edges skips GC when the mentions table is
    completely empty, because an empty table is indistinguishable from "mentions never
    populated" (fix C1-I-1). A single-document scenario leaves 0 mention rows after
    deletion, triggering the guard. This test uses two documents so D2's mention rows
    survive the deletion of D1, keeping the mentions table non-empty.

    1. Ingest D1 containing "Alice" (and "Google") — stub extracts Alice + Google.
    2. Ingest D2 containing only "Bob" (no Alice) — stub extracts Bob only.
    3. Delete D1 (vector chunks + mention rows via BE-4). D2's "Bob" mention rows remain.
       Mentions table is NON-empty → GC safety guard does NOT trigger.
       "Alice" has no remaining mention rows → genuine orphan node.
    4. POST /maintenance/trigger → GC pass runs (BE-7 calls BE-5).
    5. Poll GET /status until maintenance.last_run_at advances.
    6. Assert "Alice" absent from GET /graph/{col} (orphan removed).
       Assert "Bob" PRESENT (survivor — GC did not over-delete).
    7. Assert maintenance.last_graph_gc_at is a non-null ISO-8601 string.
    """
    _install_spacy_stub(monkeypatch)

    col = "gc-orphan-col"

    # D1: contains "Alice" and "Google" — stub will extract both.
    doc1_file = tmp_path / "doc_gc_d1.txt"
    doc1_file.write_text(
        "Alice works at Google Corp. Alice is a senior engineer.\n" * 10,
        encoding="utf-8",
    )
    doc1_id = hashlib.sha256(str(doc1_file.resolve()).encode()).hexdigest()

    # D2: contains only "Bob" — stub will extract Bob only, NOT Alice.
    doc2_file = tmp_path / "doc_gc_d2.txt"
    doc2_file.write_text(
        "Bob is a junior developer. Bob joined the team last month.\n" * 10,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 1: Ingest D1 — extracts Alice + Google.
        ingest_file_via_path(client, col, str(doc1_file), api_key=api_key)

        # Step 2: Ingest D2 — extracts Bob only (no Alice).
        ingest_file_via_path(client, col, str(doc2_file), api_key=api_key)

        # Verify Alice is in graph nodes after ingest (sanity check).
        node_names_after_ingest = asyncio.run(
            _get_all_node_names(cfg.db_path, col, "default")
        )
        assert any(n == "Alice" for n in node_names_after_ingest), (
            f"Expected 'Alice' in graph nodes after ingest; found: {node_names_after_ingest}. "
            "spaCy stub may not have run — check graph_enabled and spacy stub installation."
        )
        assert any(n == "Bob" for n in node_names_after_ingest), (
            f"Expected 'Bob' in graph nodes after ingest; found: {node_names_after_ingest}. "
            "spaCy stub did not extract 'Bob' from D2 text."
        )

        # C2-I-20: Verify Alice has at least one edge in the store BEFORE deletion.
        # This confirms the graph extractor created edges for Alice (Alice–Google), so the
        # post-GC edge assertion below proves real edge-row removal, not a no-op.
        from archon_search.graph_types import EntityType, make_stable_entity_id

        alice_entity_id = make_stable_entity_id(EntityType.person.value, "alice")
        edges_before_delete = asyncio.run(_get_all_edges(cfg.db_path, col, "default"))
        alice_edges_before = [
            e for e in edges_before_delete
            if e.source_node_id == alice_entity_id or e.target_node_id == alice_entity_id
        ]
        assert len(alice_edges_before) > 0, (
            f"Expected Alice to have at least one edge before deletion "
            f"(Alice–Google from D1 ingest). Found 0. "
            f"entity_id={alice_entity_id!r}. "
            f"All edges: {[(e.source_node_id, e.target_node_id) for e in edges_before_delete]}"
        )

        # Step 3: Delete D1 (vector chunks + graph mention rows via BE-4).
        # D2's "Bob" mention rows remain → mentions table is NON-empty.
        # "Alice" becomes a genuine orphan (no surviving mention rows).
        asyncio.run(
            _delete_document_and_mentions(cfg.db_path, col, doc1_id, "default")
        )

        # Verify mention rows for D2 survive (table is non-empty → GC guard does NOT fire).
        mention_count_after_delete = asyncio.run(
            _get_mention_count(cfg.db_path, col, "default")
        )
        assert mention_count_after_delete > 0, (
            f"Expected > 0 mention rows after deleting D1 (D2's 'Bob' rows must survive); "
            f"got {mention_count_after_delete}. "
            "delete_mentions_by_doc (BE-4) may have removed D2's rows too."
        )

        # Node rows (Alice, Google from D1; Bob from D2) still present as orphans/survivors.
        _ = asyncio.run(_get_all_node_names(cfg.db_path, col, "default"))

        # Step 4: POST /maintenance/trigger and wait for GC pass to complete.
        status_after_gc = _trigger_and_poll_maintenance(
            client, api_key, prev_last_run_at=None
        )

        # Step 5: Assert maintenance.last_graph_gc_at is set (GC ran).
        maint = status_after_gc.get("maintenance")
        assert maint is not None, "maintenance block must be present in GET /status"
        assert "last_graph_gc_at" in maint, (
            "maintenance.last_graph_gc_at key must be present in status"
        )
        last_graph_gc_at = maint["last_graph_gc_at"]
        # C2-I-22: non-null alone does not prove GC did work — the node/edge/count
        # assertions below are the behavioral proof; last_graph_gc_at only proves GC ran.
        assert last_graph_gc_at is not None, (
            "maintenance.last_graph_gc_at must be non-null after a GC pass. "
            f"Full maintenance block: {maint}"
        )
        # Must be ISO-8601 parseable.
        datetime.fromisoformat(last_graph_gc_at)

        # Step 6: GET /graph/{col} → "Alice" must be absent (orphan node removed by GC).
        # "Bob" must be PRESENT (survivor node — GC must not over-delete).
        graph_resp = client.get(f"/graph/{col}", headers=_auth(api_key))
        assert graph_resp.status_code == 200, (
            f"GET /graph/{col} failed: {graph_resp.status_code} {graph_resp.text}"
        )
        graph_data = graph_resp.json()
        assert "nodes" in graph_data, (
            f"GET /graph/{col} response must contain 'nodes' key; got: {list(graph_data.keys())}"
        )
        nodes_after_gc = graph_data["nodes"]
        alice_after_gc = next(
            (n for n in nodes_after_gc if n.get("entity_name") == "Alice"), None
        )
        assert alice_after_gc is None, (
            f"Expected 'Alice' to be absent from graph nodes after GC maintenance pass. "
            f"Orphan node was not removed by GC (BE-5 delete_orphan_nodes_and_edges or "
            f"BE-7 _run_graph_gc may not be functioning). "
            f"Found node: {alice_after_gc}. "
            f"All nodes after GC: {[n.get('entity_name') for n in nodes_after_gc]}"
        )
        bob_after_gc = next(
            (n for n in nodes_after_gc if n.get("entity_name") == "Bob"), None
        )
        assert bob_after_gc is not None, (
            f"Expected 'Bob' to be PRESENT in graph nodes after GC (survivor from D2). "
            f"GC over-deleted and removed a non-orphan node. "
            f"All nodes after GC: {[n.get('entity_name') for n in nodes_after_gc]}"
        )
        # C1-I-20: Assert Bob is a LIVE node (chunk_count > 0), not just a leftover row
        # that GC failed to clean up. chunk_count == 0 would mean GC removed mentions
        # but left the node row (a survivor from GC logic, not a true survivor with live data).
        assert bob_after_gc.get("chunk_count", 0) > 0, (
            f"'Bob' node exists after GC but chunk_count == 0 — this is NOT a live survivor. "
            f"GC may have removed Bob's mentions but left the node row. "
            f"Bob node dict: {bob_after_gc}"
        )

        # C1-I-2: GC's community-rebuild task (asyncio.create_task in maintenance_loop)
        # is intentionally fire-and-forget here and NOT awaited by this test.
        # Rebuild verification (communities updated after GC) belongs to T-4.

        # C1-I-24 / C2-I-20: Assert no edge in the graph response references Alice's entity.
        # Alice was an orphan node; her edges must also have been removed by GC.
        # Edge field names per GraphEdgeResponse schema: source_entity_id, target_entity_id.
        # alice_entity_id computed above (pre-deletion block).
        edges_after_gc = graph_data.get("edges", [])
        for edge in edges_after_gc:
            src = edge.get("source_entity_id", "")
            tgt = edge.get("target_entity_id", "")
            assert src != alice_entity_id and tgt != alice_entity_id, (
                f"Edge references Alice's entity_id after GC — orphan edge not removed. "
                f"Edge: {edge}. Alice entity_id: {alice_entity_id}"
            )

        # C2-I-2: Also verify at the GraphStore layer that no edge row references alice_entity_id.
        # The /graph endpoint filters dangling edges (inspector layer), so /graph alone is not
        # evidence of edge-row deletion from the store. A store-layer check is required.
        edges_in_store_after_gc = asyncio.run(_get_all_edges(cfg.db_path, col, "default"))
        alice_store_edges_after_gc = [
            e for e in edges_in_store_after_gc
            if e.source_node_id == alice_entity_id or e.target_node_id == alice_entity_id
        ]
        assert len(alice_store_edges_after_gc) == 0, (
            f"GraphStore still contains edge rows referencing Alice after GC. "
            f"GC (delete_orphan_nodes_and_edges) must remove edge rows from the store, "
            f"not just hide them via inspector filtering. "
            f"Stale Alice edge rows: {[(e.source_node_id, e.target_node_id) for e in alice_store_edges_after_gc]}"
        )


# ---------------------------------------------------------------------------
# Test 2: test_e2d_t2_gc_status_fields_populated
# ---------------------------------------------------------------------------


def test_e2d_t2_gc_status_fields_populated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GC status fields are populated correctly: before/after stale_mention_count comparison.

    Scenario S3 + S5:

    Phase A — Create stale state:
      Write synthetic graph nodes + mention rows whose chunk_ids do NOT exist in the
      vector store. These phantom mention rows SIMULATE the post-TTL-prune state:
      they represent what the mention table looks like after TTL-expired chunks have
      been pruned from the vector store but before GC has run. This test does NOT
      exercise the real TTL-expiry same-pass ordering (expired-chunk pruning running
      before graph GC): that ordering is enforced in production by ``_run_one_pass``
      (maintenance_loop.py — ``_run_expired_chunk_pruning`` precedes ``_run_graph_gc``)
      and is BE-7's coverage responsibility, not T-2's. The related unit test
      ``test_run_graph_gc_calls_prune_then_delete_orphans`` in
      tests/test_maintenance_loop.py covers only the within-GC call order
      (count_stale_mentions → prune_stale_mentions → delete_orphan_nodes_and_edges).

    Phase B — First GC pass:
      POST /maintenance/trigger (pass 1).
      GET /status must show:
        - maintenance.last_graph_gc_at is a non-null ISO-8601 string (S5)
        - graph.stale_mention_count is an integer (S5) — it is the count BEFORE
          prune was applied, so it must be > 0 after seeding stale rows (S3).

    Phase C — Clean state + second GC pass:
      After pass 1 the stale mentions are pruned. Trigger pass 2.
      GET /status must show:
        - graph.stale_mention_count == 0 (no more stale rows) (S3 after-cleanup)
      Before/after comparison: count went from > 0 to == 0 across the two passes.

    Note: stale_mention_count in GET /status comes from the state file (O(1) read,
    cached from last GC pass) — it reflects the MEASURED count from the most recent pass,
    not a live query.
    """
    _install_spacy_stub(monkeypatch)

    col = "gc-status-col"

    # A real file is needed so the collection exists in the vector store for GC to iterate.
    live_doc = tmp_path / "live_doc.txt"
    live_doc.write_text(
        "Alice works at Google. This document stays alive.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # --- Phase A: ingest a live document so the collection exists. ---
        ingest_file_via_path(client, col, str(live_doc), api_key=api_key)

        # Write synthetic stale mention rows: chunk_ids that do not exist in the vector store.
        # These simulate TTL-pruned chunks (S3): mention rows outlived their chunk records.
        stale_chunk_ids = ["phantom-chunk-001", "phantom-chunk-002", "phantom-chunk-003"]
        asyncio.run(
            _write_stale_mentions_directly(
                cfg.db_path, col, "default",
                entity_name="PhantomEntity",
                stale_chunk_ids=stale_chunk_ids,
            )
        )

        # Sanity: mention table now has stale rows.
        total_mentions_before = asyncio.run(_get_mention_count(cfg.db_path, col, "default"))
        assert total_mentions_before >= len(stale_chunk_ids), (
            f"Expected at least {len(stale_chunk_ids)} mention rows after seeding stale data; "
            f"got {total_mentions_before}."
        )

        # --- Phase B: first GC pass. ---
        status_pass1 = _trigger_and_poll_maintenance(
            client, api_key, prev_last_run_at=None
        )

        # S5: maintenance.last_graph_gc_at is a non-null ISO-8601 string.
        maint_pass1 = status_pass1.get("maintenance")
        assert maint_pass1 is not None, "maintenance block must be present after GC pass 1"
        assert "last_graph_gc_at" in maint_pass1, (
            "maintenance.last_graph_gc_at must be present in the maintenance sub-object"
        )
        last_graph_gc_at_pass1 = maint_pass1["last_graph_gc_at"]
        assert last_graph_gc_at_pass1 is not None, (
            "maintenance.last_graph_gc_at must be non-null after a GC pass (S5). "
            f"Full maintenance block: {maint_pass1}"
        )
        # Must be ISO-8601 parseable.
        datetime.fromisoformat(last_graph_gc_at_pass1)

        # S3 + S5: graph.stale_mention_count is an integer measured during pass 1.
        # It is the count BEFORE pruning (measured by count_stale_mentions before prune).
        # Since we seeded 3 stale phantom chunk IDs, this must be >= 3.
        graph_status_pass1 = status_pass1.get("graph")
        assert graph_status_pass1 is not None, (
            "graph status sub-object must be non-null when graph.enabled=True (S5). "
            f"Full status: {status_pass1}"
        )
        assert "stale_mention_count" in graph_status_pass1, (
            "graph.stale_mention_count must be present in graph status (S5)"
        )
        stale_count_pass1 = graph_status_pass1["stale_mention_count"]
        assert isinstance(stale_count_pass1, int), (
            f"graph.stale_mention_count must be an integer; got {type(stale_count_pass1).__name__} "
            f"(value={stale_count_pass1!r})"
        )
        # C2-I-21: Exact-count equality relies on this app having a single collection;
        # graph.stale_mention_count is a cross-collection SUM in a multi-collection setup.
        assert stale_count_pass1 == len(stale_chunk_ids), (
            f"graph.stale_mention_count after pass 1 must be exactly {len(stale_chunk_ids)} "
            f"(we seeded exactly that many stale mention rows). Got {stale_count_pass1}. "
            "A higher count means live mentions were wrongly classified stale "
            "(live-set computation bug in count_stale_mentions). "
            "A lower count means not all phantom chunk IDs were detected as stale."
        )

        # Capture pass-1 last_run_at so we can detect the start of pass 2.
        last_run_at_pass1 = maint_pass1["last_run_at"]
        assert last_run_at_pass1 is not None, (
            "maintenance.last_run_at must be non-null after pass 1"
        )

        # --- Phase C: second GC pass (clean state — stale mentions were pruned in pass 1). ---
        # After pass 1, prune_stale_mentions removed the phantom chunk mention rows.
        # Pass 2 should find zero stale mentions → stale_mention_count == 0.
        status_pass2 = _trigger_and_poll_maintenance(
            client, api_key, prev_last_run_at=last_run_at_pass1
        )

        graph_status_pass2 = status_pass2.get("graph")
        assert graph_status_pass2 is not None, (
            "graph status sub-object must remain non-null after pass 2"
        )
        stale_count_pass2 = graph_status_pass2.get("stale_mention_count")
        assert isinstance(stale_count_pass2, int), (
            f"graph.stale_mention_count after pass 2 must be an integer; "
            f"got {type(stale_count_pass2).__name__} (value={stale_count_pass2!r})"
        )
        assert stale_count_pass2 == 0, (
            f"graph.stale_mention_count must be 0 after pass 2 (stale rows pruned in pass 1). "
            f"Got {stale_count_pass2}. "
            f"Pass 1 count was {stale_count_pass1} → pass 2 count is {stale_count_pass2}. "
            "prune_stale_mentions may not have removed the stale rows in pass 1, "
            "or the second GC pass did not run correctly."
        )

        # Before/after comparison: stale_count went from > 0 (pass 1) to == 0 (pass 2).
        # This proves the field is live and not a static placeholder.
        assert stale_count_pass1 > stale_count_pass2, (
            f"stale_mention_count must decrease from pass 1 ({stale_count_pass1}) to "
            f"pass 2 ({stale_count_pass2}). "
            "The before/after comparison failed — GC may not be pruning stale mentions."
        )

        # S5: last_graph_gc_at after pass 2 must also be ISO-8601 parseable.
        maint_pass2 = status_pass2.get("maintenance")
        assert maint_pass2 is not None, "maintenance block must be present after pass 2"
        last_graph_gc_at_pass2 = maint_pass2.get("last_graph_gc_at")
        assert last_graph_gc_at_pass2 is not None, (
            "maintenance.last_graph_gc_at must remain non-null after pass 2 (S5)"
        )
        datetime.fromisoformat(last_graph_gc_at_pass2)
        # The timestamp must have advanced (pass 2 ran after pass 1).
        # Use datetime comparison instead of lexical string comparison to avoid
        # false positives from ISO-8601 string ordering edge cases (e.g. timezone suffix).
        assert datetime.fromisoformat(last_graph_gc_at_pass2) >= datetime.fromisoformat(last_graph_gc_at_pass1), (
            f"last_graph_gc_at after pass 2 ({last_graph_gc_at_pass2!r}) must be >= "
            f"pass 1 value ({last_graph_gc_at_pass1!r}). "
            "State file may not have been updated on pass 2."
        )

        # C1-I-5: Assert that the legitimate live nodes from the ingested document
        # (e.g. "Alice", "Google" from live_doc.txt) are STILL present after pass 2.
        # This proves GC pruned only phantom stale data (PhantomEntity), never live data.
        live_node_names_after_pass2 = asyncio.run(
            _get_all_node_names(cfg.db_path, col, "default")
        )
        assert any(n == "Alice" for n in live_node_names_after_pass2), (
            f"Live node 'Alice' must still be present after pass 2 GC. "
            f"GC over-deleted a live node. "
            f"Nodes after pass 2: {live_node_names_after_pass2}"
        )
        # C2-I-23: PhantomEntity was seeded with stale mentions; GC must have removed it.
        assert "PhantomEntity" not in live_node_names_after_pass2, (
            f"'PhantomEntity' must be absent from graph nodes after pass 2 GC (stale-only node). "
            f"GC did not remove the orphan node created by stale mentions. "
            f"Nodes after pass 2: {live_node_names_after_pass2}"
        )

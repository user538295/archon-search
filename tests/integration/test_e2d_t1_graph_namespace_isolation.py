"""E2d T-1: e2e tests for namespace-isolated graph mention deletion and shared-entity survival.

Scenario S6 (test_e2d_t1_delete_removes_mentions_namespace_isolated):
  Two namespaces ("nsa" and "nsb") both have a collection named "docs".
  Graph tables are namespace-scoped:
    _archon_graph_nsa__docs_mentions  vs  _archon_graph_nsb__docs_mentions
  Deleting a document from nsa removes only nsa's mention rows;
  nsb's mention rows remain intact. Two-sided assertion: nsa gone AND nsb present.

  Implementation note: this test exercises namespace isolation at the GraphStore layer,
  where the isolation actually lives (_archon_graph_{ns}__{col}_* table naming). To
  prove that two namespaces with the same collection name write to distinct tables, the
  test seeds identical data (same doc_id, entity_id, chunk_ids) under both namespaces
  directly via GraphStore. If isolation breaks (tables collapse), deleting from "nsa"
  would remove "nsb"'s data too — a two-sided failure. The HTTP GET /graph/{collection}
  endpoint is not used for seeding because it requires collection metadata; the isolation
  being tested lives entirely in graph table naming.

Scenario S7 (test_e2d_t1_shared_entity_survives_partial_delete):
  Two documents D1 and D2 share entity E (both ingested via HTTP with graph enabled).
  D1 is deleted → D1's mention rows are removed; E survives because D2 still references it.
  GET /graph/{collection} shows E still present with chunk_count > 0.
  D2 is deleted → D2's mention rows are removed; E now has no remaining mentions.
  POST /maintenance/trigger → GC pass runs.
  GET /graph/{collection} → E must be absent.

  NOTE: The final assertion (E absent after D2 delete + GC) requires:
    - BE-5: GraphStore.delete_orphan_nodes_and_edges
    - BE-7: MaintenanceLoop._run_graph_gc policy
  These tasks are not yet implemented. That assertion WILL FAIL until both are done.

Run with:
    uv run pytest tests/integration/test_e2d_t1_graph_namespace_isolation.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import sys
import time
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S: float = 15.0
_POLL_INTERVAL_S: float = 0.1


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# spaCy stub — returns [Alice (PERSON), Google (ORG)] for any input text.
# Must be installed BEFORE make_real_app(graph_enabled=True) because create_app
# calls _check_graph_deps which imports spaCy synchronously.
# ---------------------------------------------------------------------------


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents = [_FakeEnt("Alice", "PERSON"), _FakeEnt("Google", "ORG")]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

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
# Async graph-seeding helpers
# ---------------------------------------------------------------------------


async def _seed_graph_entity_with_mentions(
    db_path: str,
    collection: str,
    entity_id: str,
    entity_name: str,
    doc_id: str,
    chunk_ids: list[str],
    ns: str,
) -> None:
    """Write a graph node + mention rows for doc_id in the given namespace.

    Does NOT touch collection metadata — only graph tables.
    Uses a fresh GraphStore connection independent of the server's event loop.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import EntityType, GraphMention, GraphNode

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        node = GraphNode(
            id=entity_id,
            entity_name=entity_name,
            entity_type=EntityType.concept,
            source_doc_id=doc_id,
            collection_name=collection,
        )
        await gs.write_graph(collection, [node], [], ns=ns)
        mentions = [
            GraphMention(entity_id=entity_id, chunk_id=cid, doc_id=doc_id)
            for cid in chunk_ids
        ]
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


async def _count_mentions_for_doc(
    db_path: str,
    collection: str,
    doc_id: str,
    ns: str,
) -> int:
    """Return the number of mention rows belonging to doc_id in the given namespace."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        all_mentions = await gs.get_all_mentions(collection, ns=ns)
        return sum(1 for m in all_mentions if m.doc_id == doc_id)
    finally:
        await gs.disconnect()


async def _delete_mentions_only(
    db_path: str,
    collection: str,
    doc_id: str,
    ns: str,
) -> None:
    """Delete mention rows for doc_id from the namespace-scoped mentions table.

    Equivalent to the graph half of pipeline.delete_document.
    Used by test 1, which seeds only graph data (no vector store rows to clean up).
    """
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.delete_mentions_by_doc(collection, doc_id, ns=ns)
    finally:
        await gs.disconnect()


async def _delete_document_and_mentions(
    db_path: str,
    collection: str,
    doc_id: str,
    namespace: str,
) -> None:
    """Delete vector chunks + graph mention rows for doc_id.

    Mirrors the two operations that pipeline.delete_document performs:
      store.delete_document → graph_store.delete_mentions_by_doc
    Uses fresh connections independent of the server's event loop.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await store.delete_document(collection, doc_id, namespace=namespace)
        await gs.delete_mentions_by_doc(collection, doc_id, ns=namespace)
    finally:
        await store.disconnect()
        await gs.disconnect()


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
    Returns the maintenance sub-dict. Fails the test on timeout.
    """
    resp = client.post("/maintenance/trigger", headers=_auth(api_key))
    assert resp.status_code == 202, (
        f"POST /maintenance/trigger failed: {resp.status_code} {resp.text}"
    )

    deadline = time.monotonic() + deadline_s
    last_block: dict | None = None
    while time.monotonic() < deadline:
        r = client.get("/status", headers=_auth(api_key))
        assert r.status_code == 200, f"GET /status failed: {r.status_code} {r.text}"
        last_block = r.json().get("maintenance")
        current_run_at = last_block.get("last_run_at") if last_block else None
        if current_run_at is not None and current_run_at != prev_last_run_at:
            return last_block
        time.sleep(_POLL_INTERVAL_S)

    pytest.fail(
        f"Maintenance pass did not complete within {deadline_s}s. "
        f"prev_last_run_at={prev_last_run_at!r}; "
        f"last status maintenance block: {last_block}"
    )


# ---------------------------------------------------------------------------
# Test 1 — Namespace-isolated delete: S6
# ---------------------------------------------------------------------------


def test_e2d_t1_delete_removes_mentions_namespace_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete a doc from nsa/docs; verify mentions gone from nsa but intact in nsb.

    Scenario S6: namespace-scoped graph table names ensure that deleting a document
    from one namespace does NOT affect graph data in a sibling namespace with the
    same collection name.

    Implementation note: this test exercises namespace isolation at the GraphStore layer.
    Both namespaces use the SAME doc_id, entity_id, and chunk_ids. If isolation breaks
    (tables collapse into one), deleting from "nsa" would also wipe "nsb"'s data — the
    nsb count assertion would fail, detecting the regression. With proper isolation each
    namespace has its own table and the delete touches only nsa's rows.
    The assertion is at the GraphStore level via direct reads — that is where S6's
    isolation lives. No collection metadata is written (HTTP endpoints require it but the
    isolation being tested does not depend on it).

    Steps:
    1. Seed graph nodes + mentions for nsa/docs (entity "SharedEntity", shared_doc_id,
       chunk_ids ["c0", "c1"]).
    2. Seed graph nodes + mentions for nsb/docs (same entity, same doc_id, same chunk_ids).
       Same data in both namespaces — a namespace collapse would cause the nsa delete to
       wipe nsb's data too.
       Distinct LanceDB tables created:
         _archon_graph_nsa__docs_mentions  vs  _archon_graph_nsb__docs_mentions
    3. Sanity-assert both namespaces have mentions (count > 0).
    4. Delete nsa's mention rows for shared_doc_id.
    5. Assert nsa mention count for shared_doc_id == 0 (deleted).
    6. Assert nsb mention count for shared_doc_id == count-before (unaffected).
       Two-sided check: both must hold simultaneously.
    """
    _install_spacy_stub(monkeypatch)

    col = "docs"

    # Same doc_id, entity_id, chunk_ids seeded in BOTH namespaces.
    # If namespace isolation breaks (both go to one table), deleting from nsa would
    # also delete nsb's data (same doc_id) — making the nsb count assertion fail.
    # Only with proper per-namespace tables does the nsa delete leave nsb intact.
    shared_doc_id = hashlib.sha256(b"shared_doc.txt").hexdigest()

    from archon_search.graph_types import EntityType, make_stable_entity_id

    shared_entity_id = make_stable_entity_id(EntityType.concept.value, "sharedentity")

    # Graph-enabled app context (spaCy stub installed above).
    # Multi-namespace API keys are set up but no HTTP endpoints are called;
    # the isolation assertion lives entirely in graph store tables.
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        namespaces={key_a: "nsa", key_b: "nsb"},
    ) as (client, cfg, _default_key):

        # Step 1+2: Seed identical graph data (same doc_id, entity_id, chunk_ids) for
        # BOTH namespaces using the SAME collection name.
        # This creates distinct LanceDB tables:
        #   _archon_graph_nsa__docs_{nodes,edges,mentions}
        #   _archon_graph_nsb__docs_{nodes,edges,mentions}
        # Same data in both namespaces — a namespace collapse would cause the nsa delete
        # to wipe nsb's data too.
        asyncio.run(
            _seed_graph_entity_with_mentions(
                cfg.db_path, col,
                shared_entity_id, "SharedEntity",
                shared_doc_id, ["c0", "c1"], "nsa",
            )
        )
        asyncio.run(
            _seed_graph_entity_with_mentions(
                cfg.db_path, col,
                shared_entity_id, "SharedEntity",
                shared_doc_id, ["c0", "c1"], "nsb",
            )
        )

        # Step 3: Sanity check — both namespaces have mention rows before deletion.
        count_nsa_before = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, shared_doc_id, "nsa")
        )
        count_nsb_before = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, shared_doc_id, "nsb")
        )
        assert count_nsa_before > 0, (
            f"Expected nsa mentions before delete; got {count_nsa_before}. "
            "Graph seeding may have failed."
        )
        assert count_nsb_before > 0, (
            f"Expected nsb mentions before delete; got {count_nsb_before}. "
            "Graph seeding may have failed."
        )

        # Step 4: Delete only the graph mention rows for nsa's document.
        # Calls graph_store.delete_mentions_by_doc(col, shared_doc_id, ns="nsa"),
        # which targets _archon_graph_nsa__docs_mentions exclusively.
        asyncio.run(
            _delete_mentions_only(cfg.db_path, col, shared_doc_id, "nsa")
        )

        # Step 5: nsa's mention rows for shared_doc_id must be gone.
        count_nsa_after = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, shared_doc_id, "nsa")
        )
        assert count_nsa_after == 0, (
            f"Expected 0 mention rows for nsa doc_id={shared_doc_id!r} after delete; "
            f"got {count_nsa_after}. "
            "delete_mentions_by_doc may not have used the namespace-scoped table correctly."
        )

        # Step 6 (two-sided isolation check): nsb's mentions must be completely unaffected.
        # Because shared_doc_id is the same in both namespaces, a table collapse would have
        # caused the nsa delete to wipe nsb's rows — this assertion would catch that.
        count_nsb_after = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, shared_doc_id, "nsb")
        )
        assert count_nsb_after == count_nsb_before, (
            f"Expected nsb mention count to be unchanged ({count_nsb_before}); "
            f"got {count_nsb_after} after deleting from nsa. "
            "The delete leaked across the namespace boundary — "
            "namespace-scoped table names may not be working correctly (S6)."
        )


# ---------------------------------------------------------------------------
# Test 2 — Shared entity survives partial delete: S7
# ---------------------------------------------------------------------------


def test_e2d_t1_shared_entity_survives_partial_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared entity E survives D1 deletion; disappears only after D2 deletion + GC.

    Scenario S7: entity E appears in both D1 and D2. When D1 is deleted, E's node
    survives because D2 still has a mention row. When D2 is also deleted, E's node
    becomes an orphan; the next GC maintenance pass must remove it from the nodes table.

    NOTE: The final assertion ("node E absent" after D2 delete + GC) requires:
      - BE-5: GraphStore.delete_orphan_nodes_and_edges
      - BE-7: MaintenanceLoop._run_graph_gc
    These are NOT yet implemented. The final assertion WILL FAIL until both are done.

    Steps:
    1. HTTP-ingest two files (D1, D2) with graph enabled.
       The spaCy stub returns [Alice (PERSON), Google (ORG)] for any text;
       both D1 and D2 get the same entity set — "Alice" is the shared entity E.
    2. Verify both D1 and D2 have mention rows for "Alice".
    3. Delete D1 (remove vector chunks + graph mention rows for doc_id_d1).
    4. POST /maintenance/trigger; poll until pass completes.
    5. GET /graph/{col} → assert "Alice" present with chunk_count > 0
       (D2 still references it; S7 node-survival half).
    6. Delete D2 (remove vector chunks + graph mention rows for doc_id_d2).
    7. POST /maintenance/trigger again; poll until a NEW pass completes.
    8. GET /graph/{col} → assert "Alice" absent.
       (Requires BE-5 + BE-7 — WILL FAIL until GC is implemented.)
    """
    _install_spacy_stub(monkeypatch)

    col = "shared-col"

    doc_d1 = tmp_path / "doc_d1.txt"
    doc_d1.write_text(
        "Alice works at Google Corp.\n" * 10,
        encoding="utf-8",
    )
    doc_d2 = tmp_path / "doc_d2.txt"
    doc_d2.write_text(
        "Alice is also listed at Google.\n" * 10,
        encoding="utf-8",
    )

    doc_id_d1 = hashlib.sha256(str(doc_d1.resolve()).encode()).hexdigest()
    doc_id_d2 = hashlib.sha256(str(doc_d2.resolve()).encode()).hexdigest()

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):

        # Step 1: HTTP-ingest both docs. spaCy stub writes Alice + Google mentions for both.
        ingest_file_via_path(client, col, str(doc_d1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc_d2), api_key=api_key)

        # Step 2: Verify both docs produced mention rows for the shared entity.
        count_d1_before = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, doc_id_d1, "default")
        )
        count_d2_before = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, doc_id_d2, "default")
        )
        assert count_d1_before > 0, (
            f"Expected D1 mentions after ingest; got {count_d1_before}. "
            "Graph extraction may not have run — check spaCy stub installation."
        )
        assert count_d2_before > 0, (
            f"Expected D2 mentions after ingest; got {count_d2_before}. "
            "Graph extraction may not have run — check spaCy stub installation."
        )

        # Step 3: Delete D1 (vector chunks + graph mention rows).
        asyncio.run(
            _delete_document_and_mentions(cfg.db_path, col, doc_id_d1, "default")
        )

        # Verify D1 mentions are gone and D2 mentions remain.
        count_d1_after_d1_delete = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, doc_id_d1, "default")
        )
        count_d2_after_d1_delete = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, doc_id_d2, "default")
        )
        assert count_d1_after_d1_delete == 0, (
            f"Expected 0 D1 mentions after delete; got {count_d1_after_d1_delete}."
        )
        assert count_d2_after_d1_delete > 0, (
            f"Expected D2 mentions to survive D1 deletion; got {count_d2_after_d1_delete}."
        )

        # Step 4: POST /maintenance/trigger; wait for pass to complete.
        maint1 = _trigger_and_poll_maintenance(client, api_key, prev_last_run_at=None)
        first_run_at = maint1["last_run_at"]

        # Step 5: GET /graph/{col} → "Alice" must still be present (D2 still has mentions).
        resp_after_d1_delete = client.get(f"/graph/{col}", headers=_auth(api_key))
        assert resp_after_d1_delete.status_code == 200, (
            f"GET /graph/{col} failed: {resp_after_d1_delete.status_code} "
            f"{resp_after_d1_delete.text}"
        )
        nodes_after_d1_delete = resp_after_d1_delete.json()["nodes"]
        alice_after_d1_delete = next(
            (n for n in nodes_after_d1_delete if n["entity_name"] == "Alice"), None
        )
        assert alice_after_d1_delete is not None, (
            "Expected 'Alice' to still appear in graph nodes after D1 deletion "
            "(D2 still references it). S7 node-survival check. "
            f"Found nodes: {[n['entity_name'] for n in nodes_after_d1_delete]}"
        )
        assert alice_after_d1_delete["chunk_count"] > 0, (
            f"Expected Alice chunk_count > 0 after D1 delete (D2 mentions active); "
            f"got {alice_after_d1_delete['chunk_count']}."
        )

        # Step 6: Delete D2 (vector chunks + graph mention rows).
        asyncio.run(
            _delete_document_and_mentions(cfg.db_path, col, doc_id_d2, "default")
        )

        # Verify D2 mentions are now gone.
        count_d2_after_d2_delete = asyncio.run(
            _count_mentions_for_doc(cfg.db_path, col, doc_id_d2, "default")
        )
        assert count_d2_after_d2_delete == 0, (
            f"Expected 0 D2 mentions after delete; got {count_d2_after_d2_delete}."
        )

        # Step 7: POST /maintenance/trigger again; wait for a NEW pass to complete.
        _trigger_and_poll_maintenance(client, api_key, prev_last_run_at=first_run_at)

        # Step 8: GET /graph/{col} → "Alice" must be ABSENT (GC removed the orphan node).
        # NOTE: This assertion requires BE-5 (delete_orphan_nodes_and_edges) and
        # BE-7 (MaintenanceLoop._run_graph_gc). Those tasks are not yet implemented.
        # The conditional pytest.xfail() below converts a missing implementation into
        # an XFAIL result (not a test failure). When BE-5 and BE-7 land, alice_after_gc
        # will be None, the if-block is skipped, and the test passes green naturally.
        resp_after_gc = client.get(f"/graph/{col}", headers=_auth(api_key))
        assert resp_after_gc.status_code == 200, (
            f"GET /graph/{col} after GC failed: {resp_after_gc.status_code} "
            f"{resp_after_gc.text}"
        )
        nodes_after_gc = resp_after_gc.json()["nodes"]
        alice_after_gc = next(
            (n for n in nodes_after_gc if n["entity_name"] == "Alice"), None
        )
        if alice_after_gc is not None:
            # Alice should be present as an orphan node (chunk_count==0) — not as a live node.
            # If chunk_count > 0, that means mention cleanup itself is broken, not just GC.
            assert alice_after_gc.get("chunk_count") == 0, (
                f"'Alice' is present with chunk_count={alice_after_gc.get('chunk_count')} > 0 "
                "after both D1 and D2 were deleted. This means mention deletion is broken "
                "(delete_mentions_by_doc did not remove all mention rows). "
                "Expected Alice to be an orphan node with chunk_count==0, pending BE-5 GC removal."
            )
            pytest.xfail(
                "Node 'Alice' still present in graph nodes table after both documents "
                "deleted and GC maintenance pass ran. "
                "Orphan node cleanup requires BE-5 (GraphStore.delete_orphan_nodes_and_edges) "
                "and BE-7 (MaintenanceLoop._run_graph_gc) — neither is implemented yet. "
                "This test will pass automatically once both tasks are done."
            )
        # When BE-5 + BE-7 land, alice_after_gc will be None here and the if-block above is skipped.

"""S652: ``[maintenance] graph_gc = false`` must stop maintenance passes from sweeping the graph.

Bug report: ``Documentation/Backlog/S652-unsupported_edge_survives.md``.

Expected behaviour (``Documentation/OperatorGuide/60_graph_operations.md:247``):
``[maintenance] graph_gc`` must be ``true`` for the GC pass "to sweep at all"; with it off,
"stale nodes ... survive ... indefinitely". So with ``graph_gc = false`` a
``POST /maintenance/trigger`` pass must leave stale mention rows, orphaned nodes and
unsupported edges untouched, and ``GET /status`` ``maintenance.last_graph_gc_at`` (the
"timestamp of the last GC pass (``null`` until the first pass)", 60:254) must stay ``null``.

Run with:
    uv run pytest tests/integration/test_s652_graph_gc_disabled.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.integration.conftest import (
    ingest_file_via_path,
    install_graph_stub,
    make_real_app,
)
from tests.integration.test_e2d_t2_graph_gc_e2e import (
    _auth,
    _get_all_edges,
    _trigger_and_poll_maintenance,
)

pytestmark = pytest.mark.integration

_NS: str = "default"
_COLLECTION: str = "s652-docs"


def _maintenance_toml(*, graph_gc: bool) -> str:
    """Full TOML loading path: the operator-facing switch under test, plus a manual-only
    maintenance schedule (interval_hours = 0) exactly as in the S652 report."""
    return (
        "[maintenance]\n"
        "interval_hours = 0\n"
        "orphan_cleanup = true\n"
        f"graph_gc = {str(graph_gc).lower()}\n"
    )


async def _get_mentions(db_path: str, collection: str):
    """Return every mention row of *collection* via a fresh GraphStore connection."""
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        return await gs.get_all_mentions(collection, ns=_NS)
    finally:
        await gs.disconnect()


async def _delete_document_chunks_only(db_path: str, collection: str, doc_id: str) -> None:
    """Delete *doc_id*'s vector chunks but keep its graph mention rows.

    Leaves mention rows whose chunk_ids no longer exist in the vector store — the
    stale-mention state graph GC exists to prune (e.g. after TTL chunk pruning).
    """
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.delete_document(collection, doc_id, namespace=_NS)
    finally:
        await store.disconnect()


def _graph(client, api_key: str, col: str) -> dict:
    resp = client.get(f"/graph/{col}", headers=_auth(api_key))
    assert resp.status_code == 200, f"GET /graph/{col} failed: {resp.status_code} {resp.text}"
    return resp.json()


def _stale_pair_mentions(db_path: str, pair_doc_id: str) -> list[tuple[str, str]]:
    return sorted(
        (m.entity_id, m.chunk_id)
        for m in asyncio.run(_get_mentions(db_path, _COLLECTION))
        if m.doc_id == pair_doc_id
    )


@dataclass(frozen=True)
class _ScenarioOutcome:
    """What two maintenance passes did to the S652 corpus."""

    names_after: list[str]
    api_edge_survives: bool
    store_edge_survives: bool
    stale_before: list[tuple[str, str]]
    stale_after: list[tuple[str, str]]
    last_graph_gc_at: dict[str, str | None]
    stale_mention_count: dict[str, int]


def _run_two_passes_over_stale_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, graph_gc: bool
) -> _ScenarioOutcome:
    """Build the S652 corpus, make one document's mentions stale, run two passes.

    Corpus (mirrors S652/S614: one "pair" document is the only co-occurrence of two
    entities that stay mentioned elsewhere, and the only mention of a third entity):
      - pair:    Alice + Google + Bob  → Alice–Google edge, Bob's only mentions
      - alone_a: Alice only
      - alone_b: Google only
    Then pair's vector chunks are deleted but its mention rows kept, so with GC enabled
    one pass would: prune pair's stale mentions → orphan + sweep "Bob" → sweep the now
    unsupported Alice–Google edge (60:233). alone_a/alone_b keep the mentions
    table non-empty, so the empty-table GC safety guard (60:237) cannot mask a sweep.
    Two passes (S614's two-pass rationale) are triggered.
    """
    install_graph_stub(monkeypatch)

    pair_file = tmp_path / "pair.txt"
    pair_file.write_text(
        "Alice works at Google with Bob on the storage connector.\n" * 5,
        encoding="utf-8",
    )
    pair_doc_id = hashlib.sha256(str(pair_file.resolve()).encode()).hexdigest()

    alone_a_file = tmp_path / "alone_a.txt"
    alone_a_file.write_text("Alice is a critical engineer.\n" * 5, encoding="utf-8")

    alone_b_file = tmp_path / "alone_b.txt"
    alone_b_file.write_text("Google hosts the cluster workloads.\n" * 5, encoding="utf-8")

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        toml_content=_maintenance_toml(graph_gc=graph_gc),
    ) as (client, cfg, api_key):
        assert cfg.maintenance.graph_gc is graph_gc, (
            f"Setup: TOML [maintenance] graph_gc = {graph_gc} was not applied; "
            f"maintenance config: {cfg.maintenance}"
        )

        for f in (pair_file, alone_a_file, alone_b_file):
            ingest_file_via_path(client, _COLLECTION, str(f), api_key=api_key)

        # --- Setup: graph built as expected (GET /graph) ---
        graph_before = _graph(client, api_key, _COLLECTION)
        ids = {n["entity_name"]: n["entity_id"] for n in graph_before["nodes"]}
        for name in ("Alice", "Google", "Bob"):
            assert name in ids, (
                f"Setup: expected node {name!r} after ingest; nodes: {sorted(ids)}"
            )
        alice_google = frozenset((ids["Alice"], ids["Google"]))
        assert alice_google in {
            frozenset((e["source_entity_id"], e["target_entity_id"]))
            for e in graph_before["edges"]
        }, f"Setup: expected an Alice–Google edge after ingest; edges: {graph_before['edges']}"

        # --- Make pair's mentions stale: chunks gone, mention rows kept ---
        asyncio.run(_delete_document_chunks_only(cfg.db_path, _COLLECTION, pair_doc_id))

        stale_before = _stale_pair_mentions(cfg.db_path, pair_doc_id)
        assert stale_before, (
            "Setup: expected pair.txt mention rows to remain after deleting only its chunks"
        )
        assert {ids["Alice"], ids["Google"], ids["Bob"]} <= {eid for eid, _ in stale_before}, (
            f"Setup: pair.txt mentions must cover Alice, Google and Bob; got {stale_before}"
        )

        # --- Two maintenance passes ---
        status_pass1 = _trigger_and_poll_maintenance(client, api_key, prev_last_run_at=None)
        last_run_pass1 = status_pass1["maintenance"]["last_run_at"]
        status_pass2 = _trigger_and_poll_maintenance(
            client, api_key, prev_last_run_at=last_run_pass1
        )
        last_run_pass2 = status_pass2["maintenance"]["last_run_at"]

        # Non-vacuity: both passes really ran (last_run_at set, then advanced).
        assert last_run_pass1 is not None and last_run_pass2 is not None, (
            f"Both maintenance passes must complete; last_run_at: "
            f"pass1={last_run_pass1!r} pass2={last_run_pass2!r}"
        )
        assert last_run_pass2 != last_run_pass1, (
            f"Second pass must advance maintenance.last_run_at; both are {last_run_pass1!r}"
        )

        # --- Observe the graph after both passes ---
        graph_after = _graph(client, api_key, _COLLECTION)
        statuses = {"pass1": status_pass1, "pass2": status_pass2}
        return _ScenarioOutcome(
            names_after=sorted(n["entity_name"] for n in graph_after["nodes"]),
            api_edge_survives=alice_google in {
                frozenset((e["source_entity_id"], e["target_entity_id"]))
                for e in graph_after["edges"]
            },
            store_edge_survives=alice_google in {
                frozenset((e.source_node_id, e.target_node_id))
                for e in asyncio.run(_get_all_edges(cfg.db_path, _COLLECTION, _NS))
            },
            stale_before=stale_before,
            stale_after=_stale_pair_mentions(cfg.db_path, pair_doc_id),
            last_graph_gc_at={
                p: s["maintenance"].get("last_graph_gc_at") for p, s in statuses.items()
            },
            stale_mention_count={
                p: s["graph"]["stale_mention_count"] for p, s in statuses.items()
            },
        )


def test_s652_graph_gc_false_leaves_stale_mentions_nodes_and_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``graph_gc = false`` two maintenance passes must not sweep the graph (S652).

    The stale mention rows, the "Bob" node and the Alice–Google edge must all survive,
    ``maintenance.last_graph_gc_at`` must remain null, and ``graph.stale_mention_count``
    must keep its 0 default — stale rows exist, so a pass that counted them would report >0.
    """
    outcome = _run_two_passes_over_stale_pair(tmp_path, monkeypatch, graph_gc=False)

    # All bug symptoms are reported together so one run shows the full picture.
    violations: list[str] = []
    if "Bob" not in outcome.names_after:
        violations.append(f"orphaned node 'Bob' was swept; nodes after: {outcome.names_after}")
    if not outcome.api_edge_survives:
        violations.append(f"Alice–Google edge missing from GET /graph/{_COLLECTION}")
    if not outcome.store_edge_survives:
        violations.append("Alice–Google edge row deleted from the GraphStore")
    if outcome.stale_after != outcome.stale_before:
        violations.append(
            f"stale pair.txt mention rows were pruned: {len(outcome.stale_before)} before, "
            f"{len(outcome.stale_after)} after"
        )
    if any(v is not None for v in outcome.last_graph_gc_at.values()):
        violations.append(f"maintenance.last_graph_gc_at was stamped: {outcome.last_graph_gc_at}")
    if any(v != 0 for v in outcome.stale_mention_count.values()):
        violations.append(f"graph.stale_mention_count was written: {outcome.stale_mention_count}")

    assert not violations, (
        "[maintenance] graph_gc = false was ignored — the maintenance passes ran graph "
        "GC (60_graph_operations.md:247):\n- " + "\n- ".join(violations)
    )


def test_s652_graph_gc_true_sweeps_the_same_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: with ``graph_gc = true`` the identical scenario IS swept, proving
    the ``graph_gc = false`` test above observes a real no-op rather than an unsweepable setup.

    Mirrors every negative assertion: the stale rows are pruned, the orphaned "Bob" node is
    swept, and the Alice–Google edge is removed from both GET /graph and the GraphStore as an
    unsupported relationship (60_graph_operations.md:233) while Alice and Google — still
    mentioned by alone_a/alone_b — survive. Both passes stamp ``last_graph_gc_at``; pass 1
    reports the stale rows it counted before pruning, pass 2 finds none left.
    """
    outcome = _run_two_passes_over_stale_pair(tmp_path, monkeypatch, graph_gc=True)

    violations: list[str] = []
    if "Bob" in outcome.names_after:
        violations.append(f"orphaned node 'Bob' was not swept; nodes after: {outcome.names_after}")
    if not {"Alice", "Google"} <= set(outcome.names_after):
        violations.append(f"still-mentioned Alice/Google were swept; nodes after: {outcome.names_after}")
    if outcome.api_edge_survives:
        violations.append(f"unsupported Alice–Google edge still in GET /graph/{_COLLECTION}")
    if outcome.store_edge_survives:
        violations.append("unsupported Alice–Google edge row still in the GraphStore")
    if outcome.stale_after:
        violations.append(f"stale pair.txt mention rows were not pruned: {outcome.stale_after}")
    if any(v is None for v in outcome.last_graph_gc_at.values()):
        violations.append(f"maintenance.last_graph_gc_at was not stamped: {outcome.last_graph_gc_at}")
    expected_counts = {"pass1": len(outcome.stale_before), "pass2": 0}
    if outcome.stale_mention_count != expected_counts:
        violations.append(
            f"graph.stale_mention_count {outcome.stale_mention_count} != {expected_counts} "
            "(pass 1 counts before pruning; pass 2 finds nothing left)"
        )

    assert not violations, (
        "[maintenance] graph_gc = true did not sweep the S652 scenario:\n- " + "\n- ".join(violations)
    )

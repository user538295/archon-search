"""Unit + integration tests for E2g BE-7: code-symbol PageRank compute + persistence + sort mode.

Tests verify:
- _compute_pagerank_sync ranks a hub symbol higher than a leaf symbol
- _compute_pagerank_sync is unweighted (repeated calls between the same pair
  do not inflate the score beyond a single edge)
- _compute_pagerank_sync is deterministic for a fixed graph
- graph_inspector's _node_sort_key sorts null pagerank_score last in
  "importance" mode
- GraphStore.write_pagerank_scores / pagerank_score round-trip via mocks,
  no-op on empty scores dict, and the ensure_graph_tables migration guard
  for a pre-existing nodes table lacking the column
- MaintenanceLoop.schedule_pagerank_recompute debounces repeated calls while a
  recompute is in-flight (mirrors schedule_synonym_enrichment's pattern)
- GET /graph/{collection}?salience=importance orders nodes by the persisted
  pagerank_score column
- PageRankBuilder.build() completes within documented time budgets (algorithm-
  only and end-to-end) for a hub-heavy graph at the eval-harness's existing
  scale fixture size
- _compute_pagerank_sync edge cases: dangling edge references, self-referencing
  edges, single-node graphs
- The PageRank recompute hook's never-propagate invariant: a raising callback
  does not fail ingest
- Full seam integration: schedule_pagerank_recompute -> PageRankBuilder ->
  write_pagerank_scores -> graph_inspector browse, end-to-end with a real
  GraphStore
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)
from archon_search.pagerank_builder import _compute_pagerank_sync

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _symbol(name: str) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, name),
        entity_name=name,
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-abc",
        collection_name="test-col",
    )


def _edge(src: GraphNode, tgt: GraphNode, rel: RelationshipType = RelationshipType.calls) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, rel.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=rel,
        source_doc_id="doc-abc",
    )


# ---------------------------------------------------------------------------
# _compute_pagerank_sync
# ---------------------------------------------------------------------------


def test_pageRank_ranksHubsHigher() -> None:
    """A symbol called by many others scores higher than a leaf symbol."""
    hub = _symbol("hub")
    callers = [_symbol(f"caller_{i}") for i in range(5)]
    leaf = _symbol("leaf")

    nodes = [hub, leaf, *callers]
    edges = [_edge(c, hub) for c in callers]

    scores = _compute_pagerank_sync(nodes, edges)

    assert scores[hub.id] > scores[leaf.id]


def test_pageRank_unweighted_ignoresCallCount() -> None:
    """Repeated calls between the same pair do not inflate the score."""
    a = _symbol("a")
    b = _symbol("b")

    single_edge_scores = _compute_pagerank_sync([a, b], [_edge(a, b)])
    repeated_edge_scores = _compute_pagerank_sync(
        [a, b], [_edge(a, b), _edge(a, b), _edge(a, b)]
    )

    assert single_edge_scores[b.id] == pytest.approx(repeated_edge_scores[b.id])


def test_pageRank_deterministicForFixedGraph() -> None:
    """The same logical graph produces identical scores and rank order
    regardless of node/edge insertion order.

    Calling _compute_pagerank_sync twice on the SAME list objects would be
    trivially true (networkx.pagerank has no randomness) and wouldn't catch
    the real risk: insertion/iteration order producing different float
    accumulation and possibly flipping near-tied ranks. This permutes the
    node and edge order (reversed) to prove genuine order-independence.
    """
    # Deliberately asymmetric (not a symmetric ring): c receives links from both
    # a and b, b receives a link from a only, a receives none — so the three
    # scores are cleanly separated (c > b > a) rather than near-tied, which
    # would make "rank order" comparisons arbitrary noise instead of a
    # meaningful order-independence check.
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    nodes = [a, b, c]
    edges = [_edge(a, b), _edge(a, c), _edge(b, c)]

    forward = _compute_pagerank_sync(nodes, edges)
    permuted = _compute_pagerank_sync(list(reversed(nodes)), list(reversed(edges)))

    assert forward.keys() == permuted.keys()
    for node_id in forward:
        assert forward[node_id] == pytest.approx(permuted[node_id], abs=1e-9)

    # Rank ORDER must also be identical, not just the raw floats.
    forward_rank = sorted(forward, key=lambda nid: -forward[nid])
    permuted_rank = sorted(permuted, key=lambda nid: -permuted[nid])
    assert forward_rank == permuted_rank


def test_pageRank_filtersNonCodeSymbolRelationshipTypes() -> None:
    """Edges outside calls/imports/defines/inherits do not contribute."""
    a = _symbol("a")
    b = _symbol("b")

    with_related_edge = _compute_pagerank_sync([a, b], [_edge(a, b, RelationshipType.related_to)])
    with_no_edges = _compute_pagerank_sync([a, b], [])

    assert with_related_edge == pytest.approx(with_no_edges)


def test_pageRank_emptyNodesReturnsEmptyDict() -> None:
    assert _compute_pagerank_sync([], []) == {}


def test_pageRank_danglingEdgeSkippedGracefully() -> None:
    """An edge whose target node ID does not exist in the node set is skipped
    (the ``edge.target_node_id not in node_ids`` continue guard), not crashed on."""
    a = _symbol("a")
    b = _symbol("b")
    ghost_id = make_stable_entity_id(EntityType.code_symbol.value, "ghost")

    dangling_edge = GraphEdge(
        id=make_stable_edge_id(a.id, ghost_id, RelationshipType.calls.value),
        source_node_id=a.id,
        target_node_id=ghost_id,
        relationship_type=RelationshipType.calls,
        source_doc_id="doc-abc",
    )

    scores = _compute_pagerank_sync([a, b], [dangling_edge])

    assert set(scores) == {a.id, b.id}
    assert ghost_id not in scores


def test_pageRank_selfReferencingEdgeDoesNotCrash() -> None:
    """A node with an edge to itself does not crash and yields a sane score."""
    a = _symbol("a")
    self_edge = _edge(a, a)

    scores = _compute_pagerank_sync([a], [self_edge])

    assert scores[a.id] == pytest.approx(1.0)


def test_pageRank_singleNodeGraphReturnsScore() -> None:
    """A single isolated node (no edges) still receives a baseline score."""
    a = _symbol("a")

    scores = _compute_pagerank_sync([a], [])

    assert a.id in scores
    assert scores[a.id] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# graph_inspector — nulls-last sort in "importance" mode
# ---------------------------------------------------------------------------


def test_pageRank_nullScoresSortLast() -> None:
    """A symbol with pagerank_score=None sorts after all scored symbols."""
    from archon_search.graph_inspector import GraphNodeInspection, _node_sort_key

    scored_low = GraphNodeInspection(
        entity_id="low", entity_name="low", chunk_count=1, salience=0.1, entity_type="concept", pagerank_score=0.01
    )
    scored_high = GraphNodeInspection(
        entity_id="high", entity_name="high", chunk_count=1, salience=0.1, entity_type="concept", pagerank_score=0.9
    )
    unscored = GraphNodeInspection(
        entity_id="none", entity_name="none", chunk_count=1, salience=0.1, entity_type="concept", pagerank_score=None
    )

    ordered = sorted(
        [unscored, scored_low, scored_high],
        key=lambda n: _node_sort_key(n, "importance"),
    )

    assert [n.entity_id for n in ordered] == ["high", "low", "none"]


# ---------------------------------------------------------------------------
# GraphStore.write_pagerank_scores / pagerank_score — mocked round trip
# ---------------------------------------------------------------------------


def test_writePagerankScores_noOpOnEmptyDict() -> None:
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-pr-empty")
    mock_db = AsyncMock()

    async def _run() -> None:
        store._db = mock_db
        await store.write_pagerank_scores("test-col", {}, ns="default")
        mock_db.open_table.assert_not_called()

    asyncio.run(_run())


def test_writePagerankScores_batchesViaSingleMergeInsert() -> None:
    """Finding 2: a single merge_insert round-trip, not one update() call per node."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-pr-write")

    mock_table = MagicMock()
    # ensure_graph_tables' migration guards check the live schema for both the
    # nodes table (BE-7 pagerank_score) and, since both tables already exist in
    # this mocked db, the edges table (pre-existing extraction_method guard) —
    # report both columns as already present so neither guard calls add_columns().
    mock_table.schema = AsyncMock(
        return_value=pa.schema(
            [
                pa.field("id", pa.utf8()),
                pa.field("pagerank_score", pa.float64(), nullable=True),
                pa.field("extraction_method", pa.utf8(), nullable=True),
            ]
        )
    )

    existing_arrow = pa.table(
        {
            "id": pa.array(["node-1", "node-2"], type=pa.utf8()),
        }
    )
    query_chain = MagicMock()
    query_chain.where.return_value = query_chain
    query_chain.select.return_value = query_chain
    query_chain.to_arrow = AsyncMock(return_value=existing_arrow)
    mock_table.query.return_value = query_chain

    merge_builder = MagicMock()
    merge_builder.when_matched_update_all.return_value = merge_builder
    merge_builder.when_not_matched_insert_all.return_value = merge_builder
    merge_builder.execute = AsyncMock(return_value=None)
    mock_table.merge_insert.return_value = merge_builder

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    mock_db.list_tables = AsyncMock(return_value=MagicMock(tables=["_archon_graph_default__test-col_nodes", "_archon_graph_default__test-col_edges", "_archon_graph_default__test-col_communities", "_archon_graph_default__test-col_mentions"]))

    async def _run() -> None:
        store._db = mock_db
        await store.write_pagerank_scores("test-col", {"node-1": 0.5, "node-2": 0.25}, ns="default")
        mock_table.merge_insert.assert_called_once_with("id")
        merge_builder.execute.assert_awaited_once()
        written = merge_builder.execute.await_args.args[0]
        assert written["pagerank_score"].to_pylist() == [0.5, 0.25]
        # C2-I-1: only id + pagerank_score are shipped in the merge source table —
        # narrowing the read/write to these two columns (instead of the full row)
        # eliminates the column-clobber race against concurrent write_graph calls.
        assert written.schema.names == ["id", "pagerank_score"]

    asyncio.run(_run())


def test_pagerankScore_returnsNoneWhenColumnAbsent() -> None:
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-pr-read-absent")

    mock_table = MagicMock()
    mock_table.schema = AsyncMock(
        return_value=pa.schema([pa.field("id", pa.utf8())])
    )

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> None:
        store._db = mock_db
        result = await store.pagerank_score("test-col", "node-1", ns="default")
        assert result is None

    asyncio.run(_run())


def test_pagerankScore_returnsPersistedValue() -> None:
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-pr-read")

    mock_table = MagicMock()
    mock_table.schema = AsyncMock(
        return_value=pa.schema(
            [pa.field("id", pa.utf8()), pa.field("pagerank_score", pa.float64(), nullable=True)]
        )
    )
    query_chain = MagicMock()
    query_chain.where.return_value = query_chain
    query_chain.select.return_value = query_chain
    query_chain.to_arrow = AsyncMock(
        return_value=pa.table(
            {"pagerank_score": pa.array([0.42], type=pa.float64())}
        )
    )
    mock_table.query.return_value = query_chain

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> None:
        store._db = mock_db
        result = await store.pagerank_score("test-col", "node-1", ns="default")
        assert result == pytest.approx(0.42)

    asyncio.run(_run())


def test_ensureGraphTables_migratesPreExistingNodesTable_addsPagerankColumn() -> None:
    """A pre-E2g nodes table lacking pagerank_score gets the column added.

    The edges table mock already has extraction_method (its own BE-1 migration
    guard is unrelated to this test) so it is exempt from add_columns — only
    the nodes-table mock's add_columns() call is asserted, precisely (called
    once, with the pagerank_score field).
    """
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-pr-migration")

    mock_nodes_table = MagicMock()
    mock_nodes_table.schema = AsyncMock(
        return_value=pa.schema([pa.field("id", pa.utf8())])
    )
    mock_nodes_table.add_columns = AsyncMock(return_value=None)

    mock_edges_table = MagicMock()
    mock_edges_table.schema = AsyncMock(
        return_value=pa.schema(
            [pa.field("id", pa.utf8()), pa.field("extraction_method", pa.utf8(), nullable=True)]
        )
    )
    mock_edges_table.add_columns = AsyncMock(return_value=None)

    def _open_table(name: str):
        if name.endswith("_edges"):
            return mock_edges_table
        return mock_nodes_table

    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(
        return_value=MagicMock(
            tables=[
                "_archon_graph_default__test-col_nodes",
                "_archon_graph_default__test-col_edges",
                "_archon_graph_default__test-col_communities",
                "_archon_graph_default__test-col_mentions",
            ]
        )
    )
    mock_db.open_table = AsyncMock(side_effect=_open_table)
    mock_db.create_table = AsyncMock(return_value=AsyncMock())

    async def _run() -> None:
        store._db = mock_db
        await store.ensure_graph_tables("test-col", ns="default")
        mock_nodes_table.add_columns.assert_awaited_once()
        added_field = mock_nodes_table.add_columns.await_args.args[0]
        assert added_field.name == "pagerank_score"
        mock_edges_table.add_columns.assert_not_awaited()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MaintenanceLoop.schedule_pagerank_recompute — debounce
# ---------------------------------------------------------------------------


def _make_pagerank_maintenance_loop(tmp_path, graph_store=None):
    """Create a MaintenanceLoop with minimal config, mirroring
    tests/test_e2f_be5_synonym_enrichment_hook.py's ``_make_maintenance_loop``."""
    from archon_search.config import MaintenanceConfig
    from archon_search.jobs.maintenance_loop import MaintenanceLoop

    job_store = MagicMock()
    job_store.list.return_value = []
    search_store = MagicMock()
    search_store.list_collections = AsyncMock(return_value=[])

    config = MaintenanceConfig()
    config.interval_hours = 0
    config.fts_optimize = False
    config.orphan_cleanup = False
    config.prune_expired_chunks = False
    config.failed_ingest_retry = False

    return MaintenanceLoop(
        job_store=job_store,
        search_store=search_store,
        config=config,
        data_dir=tmp_path,
        graph_store=graph_store,
    )


def test_pageRankRecompute_debouncesOnRepeatedIngest(tmp_path) -> None:
    """Sequential rapid calls to schedule_pagerank_recompute reschedule, not duplicate.

    Mirrors test_synonym_enrichment_debounce_no_duplicate_job's in-flight-dedup
    pattern: since no ``await`` occurs between the two synchronous calls, the
    task spawned by the first call has not yet had a chance to run — so it is
    still "in-flight" for the purposes of the second call's debounce check.
    """
    loop = _make_pagerank_maintenance_loop(tmp_path)

    col = "docs"
    ns = "default"

    async def _run() -> None:
        with patch("asyncio.create_task", wraps=asyncio.create_task) as mock_create_task:
            # First call — spawns a recompute task.
            loop.schedule_pagerank_recompute(col, ns)
            assert mock_create_task.call_count == 1

            # Second call while the first is in-flight (no await yet) — must
            # NOT spawn a second task, only mark pending.
            loop.schedule_pagerank_recompute(col, ns)
            assert mock_create_task.call_count == 1, (
                "create_task must NOT be called twice when a recompute is in-flight"
            )

        key = (ns, col)
        assert key in loop._pagerank_state
        assert loop._pagerank_state[key].pending is True

        # Cancel the in-flight task to avoid ResourceWarning/unawaited-task noise.
        state = loop._pagerank_state[key]
        state.task.cancel()
        try:
            await state.task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Integration: GET /graph/{collection}?salience=importance — persisted PageRank order
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_graphBrowse_importanceSortMode_ordersByPersistedPageRank(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /graph/{collection}?salience=importance orders nodes by persisted pagerank_score,
    with a null-scored node sorting last (nulls-last)."""
    import sys
    import types

    from tests.integration.conftest import ingest_file_via_path, make_real_app

    # Minimal spaCy stub (graph_enabled requires it at app-creation time).
    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

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

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-importance.txt"
        doc_file.write_text("importance sort mode test document", encoding="utf-8")
        ingest_file_via_path(client, "col-importance", str(doc_file), api_key=api_key)

        node_high = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "HighScore"),
            entity_name="HighScore",
            entity_type=EntityType.code_symbol,
            source_doc_id="seeded-doc",
            collection_name="col-importance",
            pagerank_score=0.9,
        )
        node_low = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "LowScore"),
            entity_name="LowScore",
            entity_type=EntityType.code_symbol,
            source_doc_id="seeded-doc",
            collection_name="col-importance",
            pagerank_score=0.1,
        )
        node_null = GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, "NoScoreYet"),
            entity_name="NoScoreYet",
            entity_type=EntityType.code_symbol,
            source_doc_id="seeded-doc",
            collection_name="col-importance",
            pagerank_score=None,
        )

        async def _seed() -> None:
            from archon_search.graph_store import GraphStore

            gs = GraphStore(cfg.db_path)
            await gs.connect()
            try:
                await gs.ensure_graph_tables("col-importance", ns="default")
                await gs.write_graph(
                    "col-importance", [node_high, node_low, node_null], [], ns="default"
                )
            finally:
                await gs.disconnect()

        asyncio.run(_seed())

        response = client.get(
            "/graph/col-importance?salience=importance", headers={"Authorization": f"Bearer {api_key}"}
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert data["salience_mode"] == "importance"
        names = [n["entity_name"] for n in data["nodes"]]

        assert "HighScore" in names and "LowScore" in names and "NoScoreYet" in names, (
            f"Expected all 3 seeded nodes in response; got {names}"
        )
        # Ordered by persisted pagerank_score, descending, nulls-last.
        assert names.index("HighScore") < names.index("LowScore") < names.index("NoScoreYet"), (
            f"Expected HighScore before LowScore before NoScoreYet (nulls-last); got {names}"
        )


# ---------------------------------------------------------------------------
# Integration: PageRankBuilder.build() scales within a documented time budget
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pageRankRecompute_scalesWithinBudget(tmp_path) -> None:
    """PageRank scales within documented time budgets for a hub-heavy graph at
    the eval-harness's existing scale fixture size.

    Scale: 204 nodes (matching tests/eval/documents.jsonl's fixture document
    count — the eval harness's existing corpus scale) arranged as a hub-heavy
    graph: one hub symbol called by every other symbol, plus a chain among the
    remaining symbols, so PageRank must actually propagate rank across a
    non-trivial edge set rather than terminate instantly on isolated nodes.

    Two budgets, measuring two different things (Finding 4 — the original
    single end-to-end timer was dominated by the per-row write loop, not the
    algorithm, and risked flaking under -n4 parallel execution):
    - ``_ALGO_BUDGET_SECONDS``: times ``_compute_pagerank_sync`` directly —
      the real regression guard against a pathological O(n^3)-class
      complexity change in the algorithm itself, isolated from I/O.
    - ``_END_TO_END_BUDGET_SECONDS``: times the full ``builder.build()`` call
      (compute + the now-batched single-merge_insert persist) as a generous
      sanity check on the full path, not the primary signal.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.pagerank_builder import PageRankBuilder

    _SCALE = 204  # tests/eval/documents.jsonl fixture document count
    _ALGO_BUDGET_SECONDS = 1.0
    _END_TO_END_BUDGET_SECONDS = 8.0

    hub = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "hub"),
        entity_name="hub",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-scale",
        collection_name="col-scale",
    )
    others = [
        GraphNode(
            id=make_stable_entity_id(EntityType.code_symbol.value, f"sym_{i}"),
            entity_name=f"sym_{i}",
            entity_type=EntityType.code_symbol,
            source_doc_id="doc-scale",
            collection_name="col-scale",
        )
        for i in range(_SCALE - 1)
    ]
    nodes = [hub, *others]

    edges = [
        GraphEdge(
            id=make_stable_edge_id(o.id, hub.id, RelationshipType.calls.value),
            source_node_id=o.id,
            target_node_id=hub.id,
            relationship_type=RelationshipType.calls,
            source_doc_id="doc-scale",
        )
        for o in others
    ]
    edges.extend(
        GraphEdge(
            id=make_stable_edge_id(others[i].id, others[i + 1].id, RelationshipType.calls.value),
            source_node_id=others[i].id,
            target_node_id=others[i + 1].id,
            relationship_type=RelationshipType.calls,
            source_doc_id="doc-scale",
        )
        for i in range(len(others) - 1)
    )

    # Algorithm-only timing — isolated from any I/O, the real regression guard.
    algo_start = time.perf_counter()
    algo_scores = _compute_pagerank_sync(nodes, edges)
    algo_elapsed = time.perf_counter() - algo_start
    assert algo_elapsed < _ALGO_BUDGET_SECONDS, (
        f"_compute_pagerank_sync for {_SCALE} nodes took {algo_elapsed:.3f}s, "
        f"exceeding the {_ALGO_BUDGET_SECONDS}s algorithm budget"
    )
    assert len(algo_scores) == _SCALE

    db_path = str(tmp_path / "pagerank-scale.db")
    graph_store = GraphStore(db_path)

    async def _setup_and_build() -> tuple[dict[str, float], float]:
        await graph_store.connect()
        try:
            await graph_store.ensure_graph_tables("col-scale", ns="default")
            await graph_store.write_graph("col-scale", nodes, edges, ns="default")
            builder = PageRankBuilder(graph_store)
            start = time.perf_counter()
            scores = await builder.build("col-scale", "default")
            elapsed = time.perf_counter() - start
            return scores, elapsed
        finally:
            await graph_store.disconnect()

    scores, elapsed = asyncio.run(_setup_and_build())

    assert len(scores) == _SCALE
    assert elapsed < _END_TO_END_BUDGET_SECONDS, (
        f"PageRank recompute for {_SCALE} nodes took {elapsed:.3f}s, "
        f"exceeding the {_END_TO_END_BUDGET_SECONDS}s end-to-end budget"
    )
    # Sanity: hub (called by all 203 others) outranks a mid-chain leaf symbol.
    assert scores[hub.id] > scores[others[0].id]


# ---------------------------------------------------------------------------
# Never-propagate invariant: PageRank recompute hook failure does not fail ingest
# ---------------------------------------------------------------------------


def test_pageRankRecompute_hookFailureDoesNotPropagate(
    connected_store, col_name, tmp_path
) -> None:
    """Exception in the PageRank recompute hook is caught, WARNING logged,
    ingest returns normally — mirrors
    tests/test_e2f_be5_synonym_enrichment_hook.py's
    test_synonym_enrichment_failure_does_not_propagate for the sibling
    on_defref_edges_written hook (never-propagate auxiliary-write invariant).
    """
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.config import GraphConfig
    from archon_search.embedder import Embedder
    from archon_search.graph_types import GraphExtractionResult
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    col = col_name
    ns = "default"

    graph_config = GraphConfig(enabled=True)

    defref_result = GraphExtractionResult(nodes=[_symbol("hub")], edges=[], mentions=[])
    defref_extractor = MagicMock()
    defref_extractor.extract = AsyncMock(return_value=defref_result)

    graph_store = MagicMock()
    graph_store.ensure_graph_tables = AsyncMock()
    graph_store.write_graph = AsyncMock()
    graph_store.delete_defref_graph_by_doc = AsyncMock()

    def _failing_callback(collection: str, namespace: str) -> None:
        raise RuntimeError("Simulated PageRank recompute failure")

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        defref_extractor=defref_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.on_defref_edges_written = _failing_callback

    doc_file = tmp_path / "sample.py"
    doc_file.write_text("def caller():\n    return callee()\n\n\ndef callee():\n    return 1\n")

    warning_records: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                warning_records.append(record.getMessage())

    handler = _CapturingHandler()
    logging.getLogger("archon_search.pipeline").addHandler(handler)
    try:
        async def _run():
            return await pipeline.ingest_file(
                doc_file,
                collection=col,
                embedder=Embedder(_MockEmbedderBackend()),
            )

        result = asyncio.run(_run())
    finally:
        logging.getLogger("archon_search.pipeline").removeHandler(handler)

    assert result.status == "ok", (
        "Ingest must succeed even when the PageRank recompute callback raises"
    )
    assert any("pagerank" in msg.lower() for msg in warning_records), (
        f"Expected WARNING about PageRank recompute failure; got: {warning_records}"
    )


# ---------------------------------------------------------------------------
# Integration: full seam — hook -> compute -> persist -> browse
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pageRankRecompute_endToEnd_hookComputePersistBrowse(tmp_path) -> None:
    """Full seam with a real GraphStore: schedule_pagerank_recompute (the real
    hook production code calls) -> PageRankBuilder.build() (real compute) ->
    write_pagerank_scores (real persist) -> graph_inspector.inspect_collection
    with salience_mode="importance" (real browse) reflects the freshly
    computed scores.

    Distinct from test_graphBrowse_importanceSortMode_ordersByPersistedPageRank
    (seeds pagerank_score directly via write_graph, bypassing PageRankBuilder)
    and test_pageRankRecompute_scalesWithinBudget (calls builder.build()
    directly, bypassing the scheduling hook) — this proves the full wiring
    works end-to-end, not just each piece in isolation.
    """
    from archon_search.graph_inspector import inspect_collection
    from archon_search.graph_store import GraphStore

    col = "col-e2e"
    ns = "default"
    db_path = str(tmp_path / "pagerank-e2e.db")
    graph_store = GraphStore(db_path)

    hub = _symbol("hub")
    callers = [_symbol(f"caller_{i}") for i in range(3)]
    leaf = _symbol("leaf")
    nodes = [hub, leaf, *callers]
    edges = [_edge(c, hub) for c in callers]

    async def _run():
        await graph_store.connect()
        try:
            await graph_store.ensure_graph_tables(col, ns=ns)
            await graph_store.write_graph(col, nodes, edges, ns=ns)

            loop = _make_pagerank_maintenance_loop(tmp_path, graph_store=graph_store)
            # The real production hook call (see pipeline.py's
            # `self.on_defref_edges_written(collection, namespace)`).
            loop.schedule_pagerank_recompute(col, ns)

            state = loop._pagerank_state[(ns, col)]
            await state.task

            view = await inspect_collection(
                graph_store,
                col,
                total_chunk_count=0,
                max_nodes=100,
                max_edges=100,
                salience_mode="importance",
                ns=ns,
            )
            return view.nodes
        finally:
            await graph_store.disconnect()

    nodes_out = asyncio.run(_run())

    names_in_order = [n.entity_name for n in nodes_out]
    assert names_in_order.index("hub") < names_in_order.index("leaf"), (
        f"Expected hub (called by all 3 callers) to outrank isolated leaf; got {names_in_order}"
    )
    hub_inspection = next(n for n in nodes_out if n.entity_name == "hub")
    assert hub_inspection.pagerank_score is not None, (
        "Expected a real persisted PageRank score for hub after the full "
        "hook -> compute -> persist -> browse seam"
    )

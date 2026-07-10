"""Unit + integration tests for E2g BE-8: compute_impact blast-radius traversal.

Tests verify:
- callers and callees are correctly separated into distinct ImpactGroups
- requested depth is hard-capped at MAX_IMPACT_DEPTH (5) regardless of what was asked
- a hub symbol's oversized blast radius is truncated with an honest omitted_count
- extraction_method_filter is a pre-filter on traversal (excluded edges never followed)
- a filtered-out edge at an intermediate hop blocks traversal past it
- an ambiguous (same-named) symbol resolves via file_path when given, else highest PageRank
- ordering within each group is PageRank descending, real GraphStore end-to-end
- a None pagerank_score sorts last within a group (mirrors BE-7's null-scores-sort-last rule)
"""
from __future__ import annotations

import asyncio

import pytest

from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    ImpactDirection,
    ImpactGroup,
    MAX_IMPACT_DEPTH,
    MAX_IMPACT_GROUP_SIZE,
    RelationshipType,
    make_code_symbol_qualified_name,
    make_stable_edge_id,
    make_stable_entity_id,
)

COL = "test-col"
NS = "default"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _symbol(
    name: str,
    source_path: str | None = None,
    pagerank_score: float | None = None,
) -> GraphNode:
    qualified = make_code_symbol_qualified_name(name, source_path)
    return GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, qualified),
        entity_name=name,
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-abc",
        collection_name=COL,
        pagerank_score=pagerank_score,
    )


def _edge(
    src: GraphNode,
    tgt: GraphNode,
    rel: RelationshipType = RelationshipType.calls,
    extraction_method: str | None = "extracted",
) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, rel.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=rel,
        source_doc_id="doc-abc",
        extraction_method=extraction_method,
    )


def _run(coro):
    return asyncio.run(coro)


async def _seeded_store(tmp_path, name: str, nodes: list[GraphNode], edges: list[GraphEdge]) -> GraphStore:
    db_path = str(tmp_path / f"{name}.db")
    store = GraphStore(db_path)
    await store.connect()
    await store.ensure_graph_tables(COL, ns=NS)
    await store.write_graph(COL, nodes, edges, ns=NS)
    return store


# ---------------------------------------------------------------------------
# Callers vs. callees separation
# ---------------------------------------------------------------------------


def test_computeImpact_separatesCallersAndCallees(tmp_path) -> None:
    root = _symbol("root")
    caller = _symbol("caller")
    callee = _symbol("callee")
    nodes = [root, caller, callee]
    edges = [_edge(caller, root), _edge(root, callee)]

    async def _go():
        store = await _seeded_store(tmp_path, "sep", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.both,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert [e.entity_name for e in result.callers.direct] == ["caller"]
    assert [e.entity_name for e in result.callees.direct] == ["callee"]
    assert result.callers.indirect == []
    assert result.callees.indirect == []


def test_computeImpact_directionCallersExcludesCallees(tmp_path) -> None:
    """direction=callers must return an empty callees group even when the root
    genuinely has both caller and callee edges."""
    root = _symbol("root")
    caller = _symbol("caller")
    callee = _symbol("callee")
    nodes = [root, caller, callee]
    edges = [_edge(caller, root), _edge(root, callee)]

    async def _go():
        store = await _seeded_store(tmp_path, "dir-callers", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callers,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callees == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)
    assert [e.entity_name for e in result.callers.direct] == ["caller"]


def test_computeImpact_directionCalleesExcludesCallers(tmp_path) -> None:
    """direction=callees must return an empty callers group even when the root
    genuinely has both caller and callee edges."""
    root = _symbol("root")
    caller = _symbol("caller")
    callee = _symbol("callee")
    nodes = [root, caller, callee]
    edges = [_edge(caller, root), _edge(root, callee)]

    async def _go():
        store = await _seeded_store(tmp_path, "dir-callees", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callers == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)
    assert [e.entity_name for e in result.callees.direct] == ["callee"]


# ---------------------------------------------------------------------------
# Depth cap
# ---------------------------------------------------------------------------


def test_computeImpact_respectsDepthCap(tmp_path) -> None:
    root = _symbol("root")
    chain = [_symbol(f"n{i}") for i in range(1, 9)]  # n1..n8
    nodes = [root, *chain]
    hops = [root, *chain]
    edges = [_edge(hops[i], hops[i + 1]) for i in range(len(hops) - 1)]

    async def _go():
        store = await _seeded_store(tmp_path, "depthcap", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=8, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.depth_used == MAX_IMPACT_DEPTH
    reached = {e.entity_name for e in (*result.callees.direct, *result.callees.indirect)}
    # n1..n5 reachable within the depth-5 cap; n6/n7/n8 are beyond it.
    assert reached == {"n1", "n2", "n3", "n4", "n5"}
    # M3: direct (hop-1) vs indirect (hop 2-5) split verified independently —
    # a bug that dumps every hop into `direct` would pass the merged
    # `reached` assertion above but fail these.
    assert {e.entity_name for e in result.callees.direct} == {"n1"}
    assert {e.entity_name for e in result.callees.indirect} == {"n2", "n3", "n4", "n5"}


# ---------------------------------------------------------------------------
# Hub symbol truncation
# ---------------------------------------------------------------------------


def test_computeImpact_hubSymbol_reportsOmittedCount(tmp_path) -> None:
    root = _symbol("root")
    hub_size = MAX_IMPACT_GROUP_SIZE + 10
    callees = [_symbol(f"callee_{i}", pagerank_score=float(i)) for i in range(hub_size)]
    nodes = [root, *callees]
    edges = [_edge(root, c) for c in callees]

    async def _go():
        store = await _seeded_store(tmp_path, "hub", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callees.truncated is True
    assert result.callees.omitted_count == hub_size - MAX_IMPACT_GROUP_SIZE
    assert len(result.callees.direct) + len(result.callees.indirect) == MAX_IMPACT_GROUP_SIZE
    # Callers side is untouched — no hub there.
    assert result.callers.truncated is False
    assert result.callers.omitted_count == 0
    # MOD6(a): the kept 50 are exactly the top-50 by PageRank — callee_i has
    # pagerank_score=float(i), so the top 50 of 60 are i=10..59.
    kept_names = {e.entity_name for e in (*result.callees.direct, *result.callees.indirect)}
    assert kept_names == {f"callee_{i}" for i in range(10, hub_size)}


def test_computeImpact_hubSymbol_truncationSpansDirectAndIndirect(tmp_path) -> None:
    """Truncation spanning both direct and indirect: direct count (30) is below
    MAX_IMPACT_GROUP_SIZE on its own, but direct+indirect (30+30=60) exceeds it.
    direct must be kept in full (30) before indirect starts being cut (20 of 30
    kept, per MAX_IMPACT_GROUP_SIZE - len(direct))."""
    root = _symbol("root")
    direct_count = 30
    directs = [_symbol(f"direct_{i}", pagerank_score=float(100 + i)) for i in range(direct_count)]
    # Each direct callee has exactly one further (indirect) callee of its own,
    # with distinct PageRank scores so the "top-N kept" ordering is verifiable.
    indirects = [_symbol(f"indirect_{i}", pagerank_score=float(i)) for i in range(direct_count)]
    nodes = [root, *directs, *indirects]
    edges = [_edge(root, d) for d in directs] + [
        _edge(d, ind) for d, ind in zip(directs, indirects)
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "hub-split", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callees.truncated is True
    assert len(result.callees.direct) == direct_count  # all 30 direct kept
    assert len(result.callees.indirect) == MAX_IMPACT_GROUP_SIZE - direct_count  # 20 of 30 kept
    assert result.callees.omitted_count == (direct_count * 2) - MAX_IMPACT_GROUP_SIZE
    # The 20 kept indirect entries are the top-20 by PageRank (indirect_10..indirect_29).
    kept_indirect_names = {e.entity_name for e in result.callees.indirect}
    assert kept_indirect_names == {f"indirect_{i}" for i in range(10, direct_count)}


# ---------------------------------------------------------------------------
# extraction_method_filter
# ---------------------------------------------------------------------------


def test_computeImpact_filtersToExtractedOnly(tmp_path) -> None:
    root = _symbol("root")
    extracted_callee = _symbol("extracted_callee")
    inferred_callee = _symbol("inferred_callee")
    nodes = [root, extracted_callee, inferred_callee]
    edges = [
        _edge(root, extracted_callee, extraction_method="extracted"),
        _edge(root, inferred_callee, extraction_method="inferred"),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "filter", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callees,
                extraction_method_filter="extracted", file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    names = {e.entity_name for e in result.callees.direct}
    assert names == {"extracted_callee"}


def test_computeImpact_noExtractionMethodFilter_traversesAllRelationshipTypes(tmp_path) -> None:
    """The Adapter contract (e2g-graph-traversal.tsp) exposes only
    `extraction_method_filter` as a traversal gate — there is no
    `relationship_type` filter parameter. With `extraction_method_filter=None`
    this is by design: a `synonym_of` edge (E2f) is followed exactly like a
    `calls` edge. This test pins down that documented behavior so it isn't
    mistaken for an oversight."""
    root = _symbol("root")
    called_callee = _symbol("called_callee")
    synonym_callee = _symbol("synonym_callee")
    nodes = [root, called_callee, synonym_callee]
    edges = [
        _edge(root, called_callee, rel=RelationshipType.calls, extraction_method="extracted"),
        _edge(root, synonym_callee, rel=RelationshipType.synonym_of, extraction_method="embedding"),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "mixed-rel-types", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=1, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    names = {e.entity_name for e in result.callees.direct}
    assert names == {"called_callee", "synonym_callee"}


def test_computeImpact_filterAppliesPreTraversal(tmp_path) -> None:
    """A filtered-out edge at an intermediate hop blocks traversal past it —
    not just at the terminal edge. Chain: root -(extracted)-> a -(inferred)-> b
    -(extracted)-> c. The inferred a->b edge is filtered out, so c (which is
    reachable via a further "extracted" edge) must never be reached, even
    though the edge into c is itself "extracted" — proving this is a
    pre-traversal filter, not merely a post-hoc filter of the final edge set.
    """
    root = _symbol("root")
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    nodes = [root, a, b, c]
    edges = [
        _edge(root, a, extraction_method="extracted"),
        _edge(a, b, extraction_method="inferred"),
        _edge(b, c, extraction_method="extracted"),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "prefilter", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=5, direction=ImpactDirection.callees,
                extraction_method_filter="extracted", file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.depth_used == 1
    reached = {e.entity_name for e in (*result.callees.direct, *result.callees.indirect)}
    assert reached == {"a"}
    assert "c" not in reached
    assert result.callees.omitted_count == 0
    assert result.callees.truncated is False


# ---------------------------------------------------------------------------
# Ambiguous symbol resolution
# ---------------------------------------------------------------------------


def test_computeImpact_ambiguousSymbol_resolvesByFilePathOrPageRank(tmp_path) -> None:
    run_a = _symbol("run", source_path="a.py", pagerank_score=0.1)
    run_b = _symbol("run", source_path="b.py", pagerank_score=0.9)
    caller_of_a = _symbol("caller_of_a")
    caller_of_b = _symbol("caller_of_b")
    nodes = [run_a, run_b, caller_of_a, caller_of_b]
    edges = [_edge(caller_of_a, run_a), _edge(caller_of_b, run_b)]

    async def _go():
        store = await _seeded_store(tmp_path, "ambiguous", nodes, edges)
        try:
            by_file_path = await store.compute_impact(
                COL, "run", depth=1, direction=ImpactDirection.callers,
                extraction_method_filter=None, file_path="a.py", ns=NS,
            )
            by_pagerank = await store.compute_impact(
                COL, "run", depth=1, direction=ImpactDirection.callers,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
            return by_file_path, by_pagerank
        finally:
            await store.disconnect()

    by_file_path, by_pagerank = _run(_go())

    assert [e.entity_name for e in by_file_path.callers.direct] == ["caller_of_a"]
    assert [e.entity_name for e in by_pagerank.callers.direct] == ["caller_of_b"]


# ---------------------------------------------------------------------------
# Integration: ordering by PageRank, real GraphStore
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_computeImpact_realGraphStore_ordersByPageRank(tmp_path) -> None:
    root = _symbol("root")
    low = _symbol("low_caller", pagerank_score=0.1)
    high = _symbol("high_caller", pagerank_score=0.9)
    mid = _symbol("mid_caller", pagerank_score=0.5)
    nodes = [root, low, high, mid]
    edges = [_edge(low, root), _edge(high, root), _edge(mid, root)]

    async def _go():
        store = await _seeded_store(tmp_path, "ordering", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=1, direction=ImpactDirection.callers,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert [e.entity_name for e in result.callers.direct] == [
        "high_caller",
        "mid_caller",
        "low_caller",
    ]


# ---------------------------------------------------------------------------
# Null-scores-sort-last
# ---------------------------------------------------------------------------


def test_computeImpact_nullScoresSortLast(tmp_path) -> None:
    """Mirrors BE-7's test_pageRank_nullScoresSortLast but exercises
    compute_impact's ordering directly rather than graph_inspector's sort key."""
    root = _symbol("root")
    scored_high = _symbol("scored_high", pagerank_score=0.9)
    scored_low = _symbol("scored_low", pagerank_score=0.01)
    unscored = _symbol("unscored", pagerank_score=None)
    nodes = [root, scored_high, scored_low, unscored]
    edges = [_edge(root, scored_high), _edge(root, scored_low), _edge(root, unscored)]

    async def _go():
        store = await _seeded_store(tmp_path, "nullsort", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=1, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert [e.entity_name for e in result.callees.direct] == [
        "scored_high",
        "scored_low",
        "unscored",
    ]


# ---------------------------------------------------------------------------
# MOD5 — untested contract branches
# ---------------------------------------------------------------------------


def test_computeImpact_unresolvedSymbol_returnsEmptyResult(tmp_path) -> None:
    """find_nodes_by_name returning zero candidates yields empty ImpactGroups
    on both sides and depth_used == 0 (graph tables exist, but no node is
    named the requested symbol)."""
    other = _symbol("unrelated")
    nodes = [other]
    edges: list[GraphEdge] = []

    async def _go():
        store = await _seeded_store(tmp_path, "unresolved", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "does-not-exist", depth=2, direction=ImpactDirection.both,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.depth_used == 0
    assert result.callers == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)
    assert result.callees == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)


def test_computeImpact_cyclicGraph_terminatesWithoutDoubleCounting(tmp_path) -> None:
    """Mutual recursion (root -calls-> a -calls-> root) must not infinite-loop
    and must not double-count `a` across hops."""
    root = _symbol("root")
    a = _symbol("a")
    nodes = [root, a]
    edges = [_edge(root, a), _edge(a, root)]

    async def _go():
        store = await _seeded_store(tmp_path, "cyclic", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=5, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    # `a` is reached exactly once (hop 1), never re-added on the cycle back to root.
    all_entries = [*result.callees.direct, *result.callees.indirect]
    assert [e.entity_name for e in all_entries] == ["a"]
    assert result.depth_used == 1


def test_computeImpact_nodeBothCallerAndCallee_appearsInBothGroups(tmp_path) -> None:
    """A node that is genuinely both a caller and a callee of the root (via two
    distinct edges) must legitimately appear in both result.callers and
    result.callees under direction=both — no incorrect cross-group dedup."""
    root = _symbol("root")
    both_node = _symbol("both_node")
    nodes = [root, both_node]
    edges = [
        _edge(both_node, root, rel=RelationshipType.calls),
        _edge(root, both_node, rel=RelationshipType.imports),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "both-dedup", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=1, direction=ImpactDirection.both,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert [e.entity_name for e in result.callers.direct] == ["both_node"]
    assert [e.entity_name for e in result.callees.direct] == ["both_node"]


def test_computeImpact_impactEdgeFields_reflectHopAndEdgeMetadata(tmp_path) -> None:
    """Concrete ImpactEdge field values on a multi-hop result: depth reflects
    hop distance (1 for direct, 2+ for indirect), and relationship_type /
    extraction_method are carried through from the underlying GraphEdge."""
    root = _symbol("root")
    hop1 = _symbol("hop1")
    hop2 = _symbol("hop2")
    nodes = [root, hop1, hop2]
    edges = [
        _edge(root, hop1, rel=RelationshipType.imports, extraction_method="extracted"),
        _edge(hop1, hop2, rel=RelationshipType.inherits, extraction_method="inferred"),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "field-values", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    direct_entry = next(e for e in result.callees.direct if e.entity_name == "hop1")
    assert direct_entry.entity_id == hop1.id
    assert direct_entry.depth == 1
    assert direct_entry.relationship_type == RelationshipType.imports.value
    assert direct_entry.extraction_method == "extracted"

    indirect_entry = next(e for e in result.callees.indirect if e.entity_name == "hop2")
    assert indirect_entry.entity_id == hop2.id
    assert indirect_entry.depth == 2
    assert indirect_entry.relationship_type == RelationshipType.inherits.value
    assert indirect_entry.extraction_method == "inferred"


# ---------------------------------------------------------------------------
# MAX_IMPACT_GROUP_SIZE exact boundary (50 vs. 51)
# ---------------------------------------------------------------------------


def test_computeImpact_exactlyAtCap_notTruncated(tmp_path) -> None:
    root = _symbol("root")
    callees = [_symbol(f"callee_{i}") for i in range(MAX_IMPACT_GROUP_SIZE)]
    nodes = [root, *callees]
    edges = [_edge(root, c) for c in callees]

    async def _go():
        store = await _seeded_store(tmp_path, "cap-exact", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=1, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callees.truncated is False
    assert result.callees.omitted_count == 0
    assert len(result.callees.direct) == MAX_IMPACT_GROUP_SIZE


def test_computeImpact_oneOverCap_truncatedWithOneOmitted(tmp_path) -> None:
    root = _symbol("root")
    callees = [_symbol(f"callee_{i}") for i in range(MAX_IMPACT_GROUP_SIZE + 1)]
    nodes = [root, *callees]
    edges = [_edge(root, c) for c in callees]

    async def _go():
        store = await _seeded_store(tmp_path, "cap-over", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=1, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callees.truncated is True
    assert result.callees.omitted_count == 1


# ---------------------------------------------------------------------------
# direction=both — zero edges in the requested (but populated-elsewhere) side
# ---------------------------------------------------------------------------


def test_computeImpact_bothDirection_oneSideHasNoEdges(tmp_path) -> None:
    """Root has a callee edge but no caller edge; direction=both must return
    an empty (not erroring) callers group while callees is populated."""
    root = _symbol("root")
    callee = _symbol("callee")
    nodes = [root, callee]
    edges = [_edge(root, callee)]

    async def _go():
        store = await _seeded_store(tmp_path, "onesided", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.both,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.callers == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)
    assert [e.entity_name for e in result.callees.direct] == ["callee"]


# ---------------------------------------------------------------------------
# direction=both — asymmetric reachable depths across the two sides
# ---------------------------------------------------------------------------


def test_computeImpact_bothDirection_depthUsedIsMaxAcrossSides(tmp_path) -> None:
    """Callers reach depth 1 only; callees reach depth 3 — depth_used must be
    the max across both sides (3), not whichever side happens to be shallower."""
    root = _symbol("root")
    caller = _symbol("caller")
    c1 = _symbol("c1")
    c2 = _symbol("c2")
    c3 = _symbol("c3")
    nodes = [root, caller, c1, c2, c3]
    edges = [
        _edge(caller, root),
        _edge(root, c1),
        _edge(c1, c2),
        _edge(c2, c3),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "asymmetric", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=3, direction=ImpactDirection.both,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.depth_used == 3
    assert [e.entity_name for e in result.callers.direct] == ["caller"]


# ---------------------------------------------------------------------------
# Diamond-shaped indirect dedup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# C2-1 — root resolution filters candidates to code_symbol only
# ---------------------------------------------------------------------------


def test_computeImpact_sameNameNonCodeSymbol_neverBecomesRoot(tmp_path) -> None:
    """Two nodes share the name 'handler': one code_symbol (with a real callee
    edge), one concept (with no edges). compute_impact must resolve the root
    to the code_symbol node — proven by the result reflecting its edge, not an
    empty result from the concept node."""
    code_handler = GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, "handler"),
        entity_name="handler",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-abc",
        collection_name=COL,
    )
    concept_handler = GraphNode(
        id=make_stable_entity_id(EntityType.concept.value, "handler"),
        entity_name="handler",
        entity_type=EntityType.concept,
        source_doc_id="doc-abc",
        collection_name=COL,
    )
    callee = _symbol("callee")
    nodes = [code_handler, concept_handler, callee]
    edges = [
        GraphEdge(
            id=make_stable_edge_id(code_handler.id, callee.id, RelationshipType.calls.value),
            source_node_id=code_handler.id,
            target_node_id=callee.id,
            relationship_type=RelationshipType.calls,
            source_doc_id="doc-abc",
            extraction_method="extracted",
        )
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "code-symbol-root", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "handler", depth=1, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert [e.entity_name for e in result.callees.direct] == ["callee"]


def test_computeImpact_onlyNonCodeSymbolMatch_returnsEmptyResult(tmp_path) -> None:
    """A symbol that matches ONLY a non-code-symbol node (no code_symbol node
    with that name exists) must yield an empty ImpactResult — proving the
    `if not code_symbol_candidates` branch."""
    concept_only = GraphNode(
        id=make_stable_entity_id(EntityType.concept.value, "gravity"),
        entity_name="gravity",
        entity_type=EntityType.concept,
        source_doc_id="doc-abc",
        collection_name=COL,
    )
    nodes = [concept_only]
    edges: list[GraphEdge] = []

    async def _go():
        store = await _seeded_store(tmp_path, "concept-only", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "gravity", depth=2, direction=ImpactDirection.both,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    assert result.depth_used == 0
    assert result.callers == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)
    assert result.callees == ImpactGroup(direct=[], indirect=[], truncated=False, omitted_count=0)


# ---------------------------------------------------------------------------
# C2-2 — per-hop frontier cap selects by PageRank, not by node ID
# ---------------------------------------------------------------------------


def test_computeImpact_frontierCap_prioritizesPageRankOverNodeId(tmp_path) -> None:
    """Hop 2 (an intermediate hop, reached via a small hop-1 fan-out) has
    MAX_IMPACT_GROUP_SIZE + 1 nodes — triggering the per-hop frontier cap when
    expanding into hop 3. One hop-2 node has the lexicographically smallest ID
    (what an ID-based cap would keep) but the LOWEST PageRank score; another
    hop-2 node has the HIGHEST PageRank score and a genuine hop-3 descendant
    (given an even higher score so it survives the unrelated end-of-traversal
    MAX_IMPACT_GROUP_SIZE result cap regardless of sort order). An ID-based
    frontier cap would keep the low-PageRank/smallest-ID node for expansion and
    might drop the high-PageRank node, missing its descendant entirely
    (depth_used capped at 2). A PageRank-based cap keeps the high-PageRank node
    and discovers the hop-3 descendant (depth_used == 3)."""
    root = _symbol("root")
    direct_1 = _symbol("direct_1")
    hop2_size = MAX_IMPACT_GROUP_SIZE + 1
    hop2_nodes = [_symbol(f"hop2_{i}", pagerank_score=float(i + 1)) for i in range(hop2_size)]
    # Identify the node an ID-based (score-blind) cap would keep as the
    # "smallest id" survivor, then force its PageRank to the lowest of the hop.
    lowest_id_node = min(hop2_nodes, key=lambda n: n.id)
    lowest_id_node.pagerank_score = -1.0
    highest_score_node = max(
        (n for n in hop2_nodes if n is not lowest_id_node),
        key=lambda n: n.pagerank_score,
    )
    # Gigantic score so this hop-3 node survives the unrelated final
    # MAX_IMPACT_GROUP_SIZE result-truncation sort regardless of what else
    # was discovered — isolating the assertion to frontier-cap selection.
    descendant = _symbol("hop3_descendant", pagerank_score=1_000_000.0)

    nodes = [root, direct_1, *hop2_nodes, descendant]
    edges = (
        [_edge(root, direct_1)]
        + [_edge(direct_1, n) for n in hop2_nodes]
        + [_edge(highest_score_node, descendant)]
    )

    async def _go():
        store = await _seeded_store(tmp_path, "frontier-cap-pagerank", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=3, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    indirect_names = {e.entity_name for e in result.callees.indirect}
    assert "hop3_descendant" in indirect_names
    assert result.depth_used == 3


def test_computeImpact_diamondShape_dedupsIndirectByNodeId(tmp_path) -> None:
    """root->A, root->B, A->C, B->C: C is reachable via two 2-hop paths but
    must appear exactly once in indirect, not twice."""
    root = _symbol("root")
    a = _symbol("a")
    b = _symbol("b")
    c = _symbol("c")
    nodes = [root, a, b, c]
    edges = [
        _edge(root, a),
        _edge(root, b),
        _edge(a, c),
        _edge(b, c),
    ]

    async def _go():
        store = await _seeded_store(tmp_path, "diamond", nodes, edges)
        try:
            return await store.compute_impact(
                COL, "root", depth=2, direction=ImpactDirection.callees,
                extraction_method_filter=None, file_path=None, ns=NS,
            )
        finally:
            await store.disconnect()

    result = _run(_go())

    indirect_names = [e.entity_name for e in result.callees.indirect]
    assert indirect_names.count("c") == 1

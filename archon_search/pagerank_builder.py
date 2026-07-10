"""PageRankBuilder — Use Cases layer for E2g BE-7 code-symbol PageRank.

Computes unweighted PageRank over code-symbol edges (``calls``/``imports``/
``defines``/``inherits``) in the background, mirroring ``community_builder.py``'s
``asyncio.to_thread`` pattern. ``networkx`` is imported at module level — unlike
``leidenalg``/``igraph`` it is an unconditional dependency (pinned in
pyproject.toml), not an optional ``[graph]`` extra.

Persists scores via ``GraphStore.write_pagerank_scores``. Scheduling/debouncing
lives in ``jobs/maintenance_loop.py`` (``schedule_pagerank_recompute``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import networkx as nx

from archon_search.graph_types import RelationshipType

if TYPE_CHECKING:
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphEdge, GraphNode

_logger = logging.getLogger(__name__)

# Edge types considered part of the code-symbol graph — same-file/cross-file
# def/ref relationships introduced by DefRefExtractor (E2g BE-1..BE-6).
# Entity-extraction co-occurrence edges (``related_to``, ``synonym_of``, ``uses``,
# ``implements``, ``depends_on``) are excluded from PageRank.
_CODE_SYMBOL_RELATIONSHIP_TYPES = frozenset({
    RelationshipType.calls,
    RelationshipType.imports,
    RelationshipType.defines,
    RelationshipType.inherits,
})


def _compute_pagerank_sync(
    nodes: list["GraphNode"],
    edges: list["GraphEdge"],
) -> dict[str, float]:
    """Compute unweighted PageRank over code-symbol edges.

    Builds a ``networkx.DiGraph`` (repeated edges between the same ordered pair
    collapse to a single edge — the unweighted decision from BE-7) containing
    every node as a vertex, plus a directed edge for each code-symbol edge
    (``calls``/``imports``/``defines``/``inherits``). Isolated nodes (no
    code-symbol edges) still receive a baseline score from ``networkx.pagerank``.

    Deterministic for a fixed graph/edge order — ``networkx.pagerank`` has no
    randomness.

    Returns ``{}`` when *nodes* is empty.
    """
    if not nodes:
        return {}

    graph = nx.DiGraph()
    graph.add_nodes_from(n.id for n in nodes)
    node_ids = {n.id for n in nodes}
    for edge in edges:
        if edge.relationship_type not in _CODE_SYMBOL_RELATIONSHIP_TYPES:
            continue
        if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
            continue
        graph.add_edge(edge.source_node_id, edge.target_node_id)

    return nx.pagerank(graph)


class PageRankBuilder:
    """Use Cases layer — computes and persists code-symbol PageRank (E2g BE-7)."""

    def __init__(self, graph_store: "GraphStore") -> None:
        self._store = graph_store

    async def build(self, collection: str, ns: str) -> dict[str, float]:
        """Compute and persist PageRank scores for *collection*.

        Loads all nodes/edges via ``GraphStore``, computes unweighted PageRank
        over code-symbol edges via ``asyncio.to_thread``, then persists via
        ``GraphStore.write_pagerank_scores``.

        Returns the computed ``{node_id: score}`` mapping. Returns ``{}``
        without writing when *collection* has no nodes.
        """
        nodes = await self._store.get_all_nodes(collection, ns=ns)
        if not nodes:
            _logger.info(
                "pagerank_builder: collection %r has no nodes; skipping PageRank recompute",
                collection,
            )
            return {}

        edges = await self._store.get_all_edges(collection, ns=ns)

        scores = await asyncio.to_thread(_compute_pagerank_sync, nodes, edges)
        await self._store.write_pagerank_scores(collection, scores, ns=ns)
        return scores

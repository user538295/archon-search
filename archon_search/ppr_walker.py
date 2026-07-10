"""PPRWalker — Use Cases layer for E2h PPR retrieval mode (BE-5).

Implements Personalised PageRank (PPR) over the graph at query time:
1. Tokenise the query into N-grams and look up matching entity nodes.
2. Build a personalization vector from raw mention-row counts per entity.
3. Run ``networkx.pagerank`` (undirected graph, all edge types) via
   ``asyncio.to_thread`` so the CPU-bound work does not block the event loop.
4. Select the top-K entities by PPR score.
5. Resolve entity IDs to chunk IDs via mention rows (PPR rank order, dedup).

Privacy invariant: the raw query string is NEVER passed to any logging call.
All log messages use ``_query_fingerprint(query)`` for correlation.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import networkx as nx

from archon_search._privacy import _query_fingerprint
from archon_search.graph_expander import tokenize_and_generate_ngrams

if TYPE_CHECKING:
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphEdge, GraphNode, GraphMention

_logger = logging.getLogger(__name__)

# Maximum N-gram size — mirrors graph_expander.py
_MAX_NGRAM_SIZE = 3


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass
class PPRWalkResult:
    """Result of a ``PPRWalker.walk()`` call.

    ``entity_ids`` contains the matched and top-K PPR-scored entity IDs.
    ``chunk_ids`` contains resolved chunk IDs in PPR rank order (deduplicated).
    ``entities_matched`` is 0 when no entity names were found or no mention
    rows exist for any matched entity.
    """

    entity_ids: list[str] = field(default_factory=list)
    """Matched + top-K entity IDs in PPR score order."""
    chunk_ids: list[str] = field(default_factory=list)
    """Resolved chunk IDs, deduplicated, in PPR entity rank order."""
    entities_matched: int = 0
    """Count of matched entities with at least one mention row (0 = no match)."""


# ---------------------------------------------------------------------------
# Sync CPU-bound helper (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_ppr_sync(
    nodes: list["GraphNode"],
    edges: list["GraphEdge"],
    personalization: dict[str, float],
    damping: float,
    top_k: int,
) -> list[str]:
    """Build an undirected nx.Graph and run PPR; return top-K entity IDs.

    All edge types are included (co-occurrence, synonym, def/ref) for maximum
    recall. Isolated nodes still participate in PPR via the personalization
    vector.

    Returns the top-K node IDs sorted by descending PPR score.
    """
    G: nx.Graph = nx.Graph()
    G.add_nodes_from(n.id for n in nodes)
    node_ids = {n.id for n in nodes}
    for edge in edges:
        if edge.source_node_id in node_ids and edge.target_node_id in node_ids:
            G.add_edge(edge.source_node_id, edge.target_node_id)

    # Fix C1-I-1/C1-I-2: intersect personalization with actual graph nodes
    # (find_nodes_by_name and get_all_nodes are separate calls; GC/delete can diverge)
    personalization = {k: v for k, v in personalization.items() if k in node_ids}
    if not personalization:
        return []
    # Re-normalize after filtering
    total = sum(personalization.values())
    personalization = {k: v / total for k, v in personalization.items()}

    # Fix C1-I-3/C1-B-1: catch convergence failure — return empty on pathological graphs
    try:
        scores: dict[str, float] = nx.pagerank(G, personalization=personalization, alpha=damping)
    except nx.PowerIterationFailedConvergence:
        return []
    sorted_ids = sorted(scores, key=lambda nid: scores[nid], reverse=True)
    return sorted_ids[:top_k]


# ---------------------------------------------------------------------------
# PPRWalker
# ---------------------------------------------------------------------------


class PPRWalker:
    """Use Cases component that retrieves chunks via Personalised PageRank.

    ``walk()`` is safe to call concurrently — it holds no mutable state.
    """

    def __init__(self, graph_store: "GraphStore") -> None:
        self._store = graph_store

    async def walk(
        self,
        query: str,
        collection: str,
        damping: float,
        top_entities: int,
        ns: str,
    ) -> PPRWalkResult:
        """Run PPR over *collection*'s graph seeded from *query* N-grams.

        Args:
            query: The original search query string (never logged raw).
            collection: The collection whose graph tables to query.
            damping: PPR damping factor (0 < damping < 1, typically 0.85).
            top_entities: Maximum number of top-PPR entities to resolve.
            ns: Namespace for graph table routing.

        Returns:
            A ``PPRWalkResult`` with chunk IDs in PPR rank order.
            Empty result when: no entity names match, or no mention rows exist.
        """
        fp = _query_fingerprint(query)

        # Step 1: generate N-gram candidates and look up entity nodes.
        ngrams = tokenize_and_generate_ngrams(query, _MAX_NGRAM_SIZE)
        if not ngrams:
            _logger.debug("ppr_walker: empty query (fp=%s); returning empty result", fp)
            return PPRWalkResult()

        try:
            matched_nodes = await self._store.find_nodes_by_name(collection, ngrams, ns=ns)
        except Exception:
            _logger.warning(
                "ppr_walker: store lookup failed for collection %r (fp=%s); returning empty result",
                collection, fp, exc_info=True,
            )
            return PPRWalkResult()
        if not matched_nodes:
            _logger.debug("ppr_walker: no entity matches for query (fp=%s)", fp)
            return PPRWalkResult()

        matched_ids = [n.id for n in matched_nodes]

        # Step 2: fetch mention rows for matched entities to build personalization.
        try:
            all_mentions = await self._store.get_mentions_for_entity_ids(collection, matched_ids, ns=ns)
        except Exception:
            _logger.warning(
                "ppr_walker: mention lookup failed for collection %r (fp=%s); returning empty result",
                collection, fp, exc_info=True,
            )
            return PPRWalkResult()

        # Count raw mention rows per entity_id (duplicates are intentional weight).
        mention_counts: dict[str, int] = {}
        for m in all_mentions:
            mention_counts[m.entity_id] = mention_counts.get(m.entity_id, 0) + 1

        # Filter to only entities with at least one mention row.
        seeded_ids = [eid for eid in matched_ids if mention_counts.get(eid, 0) > 0]
        if not seeded_ids:
            _logger.debug(
                "ppr_walker: matched entities have no mention rows (fp=%s)", fp
            )
            return PPRWalkResult()

        # Step 3: build normalized personalization vector.
        raw_weights = {eid: float(mention_counts[eid]) for eid in seeded_ids}
        total = sum(raw_weights.values())
        personalization = {eid: w / total for eid, w in raw_weights.items()}

        # Step 4: load full graph and run PPR in a thread (CPU-bound).
        try:
            all_nodes = await self._store.get_all_nodes(collection, ns=ns)
            all_edges = await self._store.get_all_edges(collection, ns=ns)
        except Exception:
            _logger.warning(
                "ppr_walker: graph load failed for collection %r (fp=%s); returning empty result",
                collection, fp, exc_info=True,
            )
            return PPRWalkResult()

        top_k_ids: list[str] = await asyncio.to_thread(
            _run_ppr_sync,
            all_nodes,
            all_edges,
            personalization,
            damping,
            top_entities,
        )

        if not top_k_ids:
            return PPRWalkResult()

        # Step 5: resolve top-K entities → chunk IDs (PPR rank order, dedup).
        try:
            top_mentions = await self._store.get_mentions_for_entity_ids(collection, top_k_ids, ns=ns)
        except Exception:
            _logger.warning(
                "ppr_walker: chunk resolution failed for collection %r (fp=%s); returning partial result",
                collection, fp, exc_info=True,
            )
            return PPRWalkResult()

        # Group mention chunk_ids by entity_id (preserving insertion order).
        chunks_by_entity: dict[str, list[str]] = {eid: [] for eid in top_k_ids}
        for m in top_mentions:
            if m.entity_id in chunks_by_entity:
                chunks_by_entity[m.entity_id].append(m.chunk_id)

        # Deduplicate across entities in PPR rank order (first-seen wins).
        seen: set[str] = set()
        chunk_ids: list[str] = []
        for eid in top_k_ids:
            for cid in chunks_by_entity.get(eid, []):
                if cid not in seen:
                    chunk_ids.append(cid)
                    seen.add(cid)

        _logger.debug(
            "ppr_walker: resolved %d chunks from %d top entities (fp=%s)",
            len(chunk_ids),
            len(top_k_ids),
            fp,
        )
        return PPRWalkResult(
            entity_ids=top_k_ids,
            chunk_ids=chunk_ids,
            entities_matched=len(seeded_ids),
        )

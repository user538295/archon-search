"""Graph inspection use case for reading and deriving graph metrics — E2b/E2c.

Reads nodes, edges, and mentions from graph tables; derives chunk_count, salience,
weight, and source_chunk_ids; applies deterministic truncation; returns inspection
views for both single-collection and cross-collection queries.

TF-IDF salience (E2c / salience_mode="tfidf"):
    TF(entity, collection) = chunk_count / total_chunks_in_collection
    IDF(entity) = log((num_collections + 1) / df)
    salience = TF * max(IDF, 0)  (IDF floored at 0 to guard against df > num_collections over-count)

    where df = max(entity_presence.get(entity_id, 1), 1) — the number of namespace
    collections that contain this entity (clamped to ≥1 to guard against absent or
    corrupt entries).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from archon_search.graph_types import RelationshipType

if TYPE_CHECKING:
    from archon_search.graph_store import GraphStore

logger = logging.getLogger(__name__)

# Safety valve against OOM on pathologically large mention tables; adjust via
# operator-level config if needed.
_MENTIONS_SCAN_CEILING = 500_000
# ponytail: _MENTIONS_SCAN_CEILING is a safety valve against OOM on large mention tables.
# Even in tfidf mode, inspect_collection still scans the full mention table to derive
# chunk_count per entity (line ~225). Pre-computation of entity_presence does not change
# the mention scan cost. Upgrade path: move the ceiling check to the GraphStore layer
# or stream chunks per entity rather than loading all mentions into memory.


@dataclass
class GraphNodeInspection:
    """A node in the inspection response with derived metrics."""

    entity_id: str
    """ID of the graph node (from make_stable_entity_id)."""
    entity_name: str
    """Human-readable name of the entity."""
    chunk_count: int
    """Number of distinct chunks where this entity was mentioned."""
    salience: float
    """In frequency mode, clamped to [0.0, 1.0]. In tfidf mode, floored at 0 (TF × max(IDF, 0)); unbounded above."""
    entity_type: str
    """Entity type string value from EntityType enum (e.g. 'person', 'concept', 'system', 'event', 'code_symbol')."""
    pagerank_score: float | None = None
    """Persisted unweighted PageRank importance score over code-symbol edges — E2g BE-7.
    ``None`` when not yet computed by the background recompute; sorts last in
    ``salience_mode="importance"``."""


@dataclass
class GraphEdgeInspection:
    """An edge in the inspection response with derived metrics."""

    edge_id: str
    """ID of the edge (from make_stable_edge_id)."""
    source_entity_id: str
    """ID of the source node."""
    target_entity_id: str
    """ID of the target node."""
    weight: int
    """Number of chunks where both endpoints are mentioned (co-occurrence count)."""
    source_chunk_ids: list[str]
    """Chunk IDs where both endpoints co-occur; sorted lexicographically, capped at 20."""
    relationship_type: str = "related_to"
    """Semantic relationship type (e.g. 'related_to', 'synonym_of'). BE-7."""


@dataclass
class CollectionGraphView:
    """Inspection view of a single collection's graph."""

    nodes: list[GraphNodeInspection]
    """Truncated node list; sorted by salience desc (or chunk_count desc in frequency mode),
    then entity_id asc as tiebreaker; capped at max_nodes."""
    edges: list[GraphEdgeInspection]
    """Truncated edge list where both endpoints survive node truncation;
    sorted by (weight desc, edge_id asc) then capped."""
    node_count: int
    """Total number of nodes in the graph (before truncation)."""
    edge_count: int
    """Number of edges where BOTH endpoints exist in the node table
    (after node survival filter, before edge cap)."""
    truncated: bool
    """True if node cap or edge cap was reached, or if mention scan ceiling was hit."""
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency"
    """Salience scoring mode used: 'frequency' (chunk ratio, clamped), 'tfidf' (TF×IDF), or 'importance' (persisted PageRank over code-symbol edges, nulls-last)."""


@dataclass
class CrossCollectionGraphView:
    """Inspection view of merged graph data across multiple collections."""

    collections: list[str]
    """Names of the collections included in the merge."""
    nodes: list[GraphNodeInspection]
    """Merged nodes (deduplicated by entity_id, chunk counts summed)."""
    edges: list[GraphEdgeInspection]
    """Merged edges (deduplicated by edge id, weights summed)."""
    node_count: int
    """Total merged node count (before edge survival filter and truncation)."""
    edge_count: int
    """Total merged edge count (after node survival filter, before edge cap)."""
    truncated: bool
    """True if node cap, edge cap, or mention scan ceiling was reached."""
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency"
    """Salience scoring mode used: 'frequency' (chunk ratio, clamped), 'tfidf' (TF×IDF), or 'importance' (persisted PageRank over code-symbol edges, nulls-last)."""


# Maximum number of source chunk IDs to include per edge
_MAX_SOURCE_CHUNK_IDS = 20


def _node_sort_key(
    n: "GraphNodeInspection", salience_mode: Literal["frequency", "tfidf", "importance"]
) -> tuple[bool, float, str] | tuple[float, str]:
    """Return the sort key for a node given the active salience mode."""
    if salience_mode == "importance":
        # Nulls-last: (True, ...) sorts after (False, ...) since False < True.
        return (n.pagerank_score is None, -(n.pagerank_score or 0.0), n.entity_id)
    if salience_mode == "tfidf":
        return (-n.salience, n.entity_id)
    return (-n.chunk_count, n.entity_id)


def _truncate_graph(
    nodes: list[GraphNodeInspection],
    edges: list[GraphEdgeInspection],
    max_nodes: int,
    max_edges: int,
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency",
) -> tuple[list[GraphNodeInspection], list[GraphEdgeInspection], bool]:
    """Apply deterministic truncation to nodes and edges.

    Synonym edges (relationship_type='synonym_of') receive two exemptions:
    1. Node cap exemption: synonym edge endpoint node IDs are added to the
       surviving-nodes set unconditionally (before the node cap is applied to
       non-synonym edges). This prevents synonym endpoints from being dropped.
    2. Edge cap exemption: synonym edges are excluded from max_edges truncation
       (max_edges applies only to non-synonym edges); synonym edges are appended
       after the cap.

    Steps:
    1. Sort nodes by salience key (mode-dependent); cap at max_nodes.
       - frequency: sort by (-chunk_count, entity_id)
       - tfidf:     sort by (-salience, entity_id)
    2. Collect synonym edge endpoint IDs; add them to surviving set unconditionally.
    3. Filter non-synonym edges to survivors; sort and cap at max_edges.
    4. Filter synonym edges to survivors; append after the cap (uncapped).
    5. Return (nodes_out, edges_out, truncated) where truncated=True if the node cap fired
       OR the non-synonym edge cap fired (synonym edges are never counted toward the edge cap).

    Args:
        nodes: List of GraphNodeInspection objects to truncate.
        edges: List of GraphEdgeInspection objects to filter and truncate.
        max_nodes: Maximum number of nodes to return.
        max_edges: Maximum number of edges to return.
        salience_mode: Sort key for node truncation; 'frequency' uses chunk_count,
            'tfidf' uses salience score.

    Returns:
        Tuple of (truncated_nodes, truncated_edges, truncated_flag).
    """
    # Step 1: Sort and cap nodes — key depends on salience mode
    sorted_nodes = sorted(nodes, key=lambda n: _node_sort_key(n, salience_mode))
    node_truncated = len(sorted_nodes) > max_nodes

    # Step 2: Collect synonym edge endpoint IDs for node-cap exemption
    node_id_set = {n.entity_id for n in sorted_nodes}
    synonym_edges = [e for e in edges if e.relationship_type == RelationshipType.synonym_of.value]
    non_synonym_edges = [e for e in edges if e.relationship_type != RelationshipType.synonym_of.value]

    # Synonym endpoints that exist in the full node list are exempt from the cap
    synonym_endpoint_ids: set[str] = set()
    for e in synonym_edges:
        if e.source_entity_id in node_id_set:
            synonym_endpoint_ids.add(e.source_entity_id)
        if e.target_entity_id in node_id_set:
            synonym_endpoint_ids.add(e.target_entity_id)

    # Build capped node list: top max_nodes from sorted list PLUS synonym endpoints
    nodes_out_ids: set[str] = set()
    nodes_out: list[GraphNodeInspection] = []
    for n in sorted_nodes[:max_nodes]:
        nodes_out.append(n)
        nodes_out_ids.add(n.entity_id)

    # Add synonym endpoint nodes that didn't make the cap
    for n in sorted_nodes[max_nodes:]:
        if n.entity_id in synonym_endpoint_ids:
            nodes_out.append(n)
            nodes_out_ids.add(n.entity_id)

    surviving_entity_ids = nodes_out_ids

    # Step 3: Filter non-synonym edges to survivors; sort and cap at max_edges
    surviving_non_synonym = [
        e
        for e in non_synonym_edges
        if e.source_entity_id in surviving_entity_ids
        and e.target_entity_id in surviving_entity_ids
    ]
    sorted_non_synonym = sorted(surviving_non_synonym, key=lambda e: (-e.weight, e.edge_id))
    capped_non_synonym = sorted_non_synonym[:max_edges]
    edge_truncated = len(sorted_non_synonym) > max_edges

    # Step 4: Filter synonym edges to survivors and append (uncapped)
    surviving_synonym = [
        e
        for e in synonym_edges
        if e.source_entity_id in surviving_entity_ids
        and e.target_entity_id in surviving_entity_ids
    ]

    edges_out = capped_non_synonym + surviving_synonym

    truncated = node_truncated or edge_truncated

    return nodes_out, edges_out, truncated


def _apply_tfidf(
    nodes: list[GraphNodeInspection],
    entity_presence: dict[str, int],
    num_collections: int,
) -> list[GraphNodeInspection]:
    """Apply IDF multiplier to a list of nodes' salience values.

    Returns a new list of GraphNodeInspection instances with salience = base_salience * IDF.
    IDF = log((num_collections + 1) / df) where df = max(entity_presence.get(entity_id, 1), 1).
    Logs a single WARNING if any entities are missing from entity_presence.
    """
    missing_count = 0
    clamped_count = 0
    result: list[GraphNodeInspection] = []
    for node in nodes:
        if node.entity_id not in entity_presence:
            missing_count += 1
        df = max(entity_presence.get(node.entity_id, 1), 1)
        raw_idf = math.log((num_collections + 1) / df)
        if raw_idf < 0.0:
            clamped_count += 1
            raw_idf = 0.0
        idf = raw_idf
        result.append(GraphNodeInspection(
            entity_id=node.entity_id,
            entity_name=node.entity_name,
            chunk_count=node.chunk_count,
            salience=node.salience * idf,
            entity_type=node.entity_type,
            pagerank_score=node.pagerank_score,
        ))
    if missing_count > 0:
        logger.warning(
            "%d of %d entities missing from entity_presence — used df=1 fallback for each",
            missing_count,
            len(nodes),
        )
    if clamped_count > 0:
        logger.warning(
            "%d of %d entities had IDF clamped to 0.0 (df > num_collections+1 — upstream presence over-count?)",
            clamped_count,
            len(nodes),
        )
    return result


async def inspect_collection(
    graph_store: "GraphStore",
    collection: str,
    total_chunk_count: int,
    max_nodes: int,
    max_edges: int,
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency",
    entity_presence: dict[str, int] | None = None,
    num_collections: int = 1,
    *,
    ns: str,
) -> CollectionGraphView:
    """Inspect a single collection's graph and derive metrics.

    Reads all nodes, edges, and mentions; derives chunk_count, salience, and weight
    metrics in-process; applies deterministic truncation; handles absent/empty tables
    gracefully.

    Args:
        graph_store: GraphStore instance (already connected).
        collection: Collection name to inspect.
        total_chunk_count: Total number of chunks in this collection (from CollectionMeta.chunk_count).
            Used as denominator for salience calculation. If 0, salience is 0.0 for all nodes.
        max_nodes: Maximum nodes to return (truncation cap).
        max_edges: Maximum edges to return (truncation cap).
        salience_mode: 'frequency' (default) computes salience as chunk_count/total,
            clamped to [0.0, 1.0]. 'tfidf' computes TF×max(IDF, 0) (IDF floored at 0).
        entity_presence: Required when salience_mode='tfidf'. Maps entity_id to the
            number of namespace collections that contain that entity (document frequency).
            Absent entries default to df=1 with a WARNING log.
        num_collections: Total number of namespace collections; used as N in the IDF
            formula log((N+1)/df). Ignored in frequency mode.

    Returns:
        CollectionGraphView with derived metrics and truncation info.

    Raises:
        ValueError: If salience_mode='tfidf' and entity_presence is None.

    Note:
        - Pre-E2b nodes (no mentions) read as chunk_count=0, salience=0.0.
        - Mentions scan ceiling: if mention count >= _MENTIONS_SCAN_CEILING,
          truncated=True even if node/edge caps don't fire.
        - Frequency mode: salience = min(chunk_count / total_chunk_count, 1.0).
        - TF-IDF mode: salience = (chunk_count / total_chunk_count) * max(log((N+1) / df), 0.0)
          (IDF floored at 0 to guard against df > num_collections over-count).
    """
    if salience_mode == "tfidf" and entity_presence is None:
        raise ValueError("entity_presence required for tfidf mode")
    if salience_mode == "tfidf" and num_collections < 1:
        raise ValueError("num_collections must be >= 1 in tfidf mode")

    # Fetch all nodes and edges
    all_nodes = await graph_store.get_all_nodes(collection, ns=ns)
    all_edges = await graph_store.get_all_edges(collection, ns=ns)

    # Fetch mentions up to the ceiling
    all_mentions = await graph_store.get_all_mentions(collection, limit=_MENTIONS_SCAN_CEILING, ns=ns)

    # Check if mentions hit the ceiling
    mentions_ceiling_hit = len(all_mentions) >= _MENTIONS_SCAN_CEILING

    # Build entity_chunks index: entity_id → set of chunk_ids
    entity_chunks: dict[str, set[str]] = {}
    for mention in all_mentions:
        if mention.entity_id not in entity_chunks:
            entity_chunks[mention.entity_id] = set()
        entity_chunks[mention.entity_id].add(mention.chunk_id)

    # Derive node metrics
    node_inspections: list[GraphNodeInspection] = []
    for node in all_nodes:
        chunk_count = len(entity_chunks.get(node.id, set()))

        if salience_mode == "tfidf":
            # Compute TF only; IDF multiplier applied by _apply_tfidf after the loop
            if total_chunk_count == 0:
                salience = 0.0
            else:
                salience = chunk_count / total_chunk_count
        else:
            # Frequency mode: salience = chunk_count / total, clamped to [0.0, 1.0]
            if total_chunk_count > 0:
                salience = min(chunk_count / total_chunk_count, 1.0)
            else:
                salience = 0.0

        node_inspections.append(
            GraphNodeInspection(
                entity_id=node.id,
                entity_name=node.entity_name,
                chunk_count=chunk_count,
                salience=salience,
                entity_type=node.entity_type.value,
                pagerank_score=node.pagerank_score,
            )
        )

    if salience_mode == "tfidf":
        node_inspections = _apply_tfidf(node_inspections, entity_presence, num_collections)  # type: ignore[arg-type]

    # Derive edge metrics
    edge_inspections: list[GraphEdgeInspection] = []
    for edge in all_edges:
        # Weight = co-occurrence chunk count (intersection of chunks where both endpoints appear)
        source_chunks = entity_chunks.get(edge.source_node_id, set())
        target_chunks = entity_chunks.get(edge.target_node_id, set())
        cooccur_chunks = source_chunks & target_chunks

        weight = len(cooccur_chunks)
        # Sort chunk IDs lexicographically, cap at 20
        source_chunk_ids = sorted(cooccur_chunks)[: _MAX_SOURCE_CHUNK_IDS]

        edge_inspections.append(
            GraphEdgeInspection(
                edge_id=edge.id,
                source_entity_id=edge.source_node_id,
                target_entity_id=edge.target_node_id,
                weight=weight,
                source_chunk_ids=source_chunk_ids,
                relationship_type=edge.relationship_type.value,
            )
        )

    # Apply truncation; _truncate_graph filters edges to surviving nodes and caps both
    truncated_nodes, truncated_edges, truncation_fired = _truncate_graph(
        node_inspections, edge_inspections, max_nodes, max_edges, salience_mode=salience_mode
    )

    # Compute edge_count: number of edges where both endpoints survive the node filter
    # (before the edge cap). Derive from the already-computed truncated_nodes to
    # guarantee structural consistency — re-sorting independently could diverge.
    surviving_node_ids_pre_cap = {n.entity_id for n in truncated_nodes}
    edges_post_node_filter = [
        e
        for e in edge_inspections
        if e.source_entity_id in surviving_node_ids_pre_cap
        and e.target_entity_id in surviving_node_ids_pre_cap
    ]
    total_edge_count = len(edges_post_node_filter)

    truncated = truncation_fired or mentions_ceiling_hit

    return CollectionGraphView(
        nodes=truncated_nodes,
        edges=truncated_edges,
        node_count=len(node_inspections),
        edge_count=total_edge_count,
        truncated=truncated,
        salience_mode=salience_mode,
    )


async def inspect_cross_collection(
    graph_store: "GraphStore",
    collections: list[str],
    total_chunk_counts: dict[str, int],
    max_nodes: int,
    max_edges: int,
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency",
    entity_presence: dict[str, int] | None = None,
    num_collections: int = 1,
    *,
    ns: str,
) -> CrossCollectionGraphView:
    """Inspect and merge graph data across multiple collections.

    For each collection, fetches nodes and edges, then merges them:
    - Nodes are deduplicated by entity_id; chunk_counts and saliences are merged
      using weighted average (frequency salience).
    - In tfidf mode, the merged frequency salience is multiplied by IDF after merging.
    - Edges are deduplicated by edge_id; weights are summed; source_chunk_ids
      are unioned and capped at 20.

    Args:
        graph_store: GraphStore instance (already connected).
        collections: List of collection names to merge.
        total_chunk_counts: Dict mapping collection name → total chunk count
            (for per-collection salience denominators before averaging).
        max_nodes: Maximum nodes to return (truncation cap).
        max_edges: Maximum edges to return (truncation cap).
        salience_mode: 'frequency' (default) uses weighted-average frequency salience
            for truncation ordering; 'tfidf' multiplies the merged frequency salience by
            IDF = log((num_collections + 1) / df) and uses that for ordering.
        entity_presence: Required when salience_mode='tfidf'. Maps entity_id to the
            number of namespace collections that contain that entity (document frequency).
            Absent or zero entries default to df=1.
        num_collections: Total number of namespace collections; used as N in the IDF
            formula log((N+1)/df). Should be the full namespace count, not just the
            number of collections listed. Ignored in frequency mode.

    Returns:
        CrossCollectionGraphView with merged data, truncation info, and salience_mode echoed.

    Raises:
        ValueError: If salience_mode='tfidf' and entity_presence is None.
    """
    if salience_mode == "tfidf" and entity_presence is None:
        raise ValueError("entity_presence required for tfidf mode")
    if salience_mode == "tfidf" and num_collections < 1:
        raise ValueError("num_collections must be >= 1 in tfidf mode")
    merged_nodes: dict[str, GraphNodeInspection] = {}
    merged_edges: dict[str, GraphEdgeInspection] = {}
    total_mentions_scanned = 0
    mentions_ceiling_hit = False

    for collection in collections:
        # Fetch nodes, edges, mentions for this collection
        all_nodes = await graph_store.get_all_nodes(collection, ns=ns)
        all_edges = await graph_store.get_all_edges(collection, ns=ns)
        all_mentions = await graph_store.get_all_mentions(collection, limit=_MENTIONS_SCAN_CEILING, ns=ns)

        # Check if this collection hit the ceiling
        if len(all_mentions) >= _MENTIONS_SCAN_CEILING:
            mentions_ceiling_hit = True

        total_mentions_scanned += len(all_mentions)

        # Build entity_chunks index for this collection
        entity_chunks: dict[str, set[str]] = {}
        for mention in all_mentions:
            if mention.entity_id not in entity_chunks:
                entity_chunks[mention.entity_id] = set()
            entity_chunks[mention.entity_id].add(mention.chunk_id)

        # Merge nodes
        total_chunk_count = total_chunk_counts.get(collection, 0)
        for node in all_nodes:
            chunk_count = len(entity_chunks.get(node.id, set()))
            if total_chunk_count > 0:
                salience = min(chunk_count / total_chunk_count, 1.0)
            else:
                salience = 0.0

            if node.id in merged_nodes:
                # Node already seen in previous collection; merge
                existing = merged_nodes[node.id]
                merged_chunk_count = existing.chunk_count + chunk_count
                # Weighted-average salience: (chunk_count_1 * salience_1 + chunk_count_2 * salience_2) / total_chunk_count
                merged_salience = (
                    existing.chunk_count * existing.salience
                    + chunk_count * salience
                ) / max(merged_chunk_count, 1)
                merged_salience = min(merged_salience, 1.0)
                # pagerank_score is per-collection and not summed across
                # collections, unlike chunk_count/weight — keep the first
                # non-null value encountered.
                merged_pagerank = (
                    existing.pagerank_score
                    if existing.pagerank_score is not None
                    else node.pagerank_score
                )
                # entity_type is invariant for a given entity_id (entity_id is hash(type:name)), so any collection's value is correct.
                merged_nodes[node.id] = GraphNodeInspection(
                    entity_id=node.id,
                    entity_name=node.entity_name,
                    chunk_count=merged_chunk_count,
                    salience=merged_salience,
                    entity_type=node.entity_type.value,
                    pagerank_score=merged_pagerank,
                )
            else:
                # First time seeing this node
                merged_nodes[node.id] = GraphNodeInspection(
                    entity_id=node.id,
                    entity_name=node.entity_name,
                    chunk_count=chunk_count,
                    salience=salience,
                    entity_type=node.entity_type.value,
                    pagerank_score=node.pagerank_score,
                )

        # Merge edges
        for edge in all_edges:
            source_chunks = entity_chunks.get(edge.source_node_id, set())
            target_chunks = entity_chunks.get(edge.target_node_id, set())
            cooccur_chunks = source_chunks & target_chunks

            weight = len(cooccur_chunks)
            source_chunk_ids = sorted(cooccur_chunks)[: _MAX_SOURCE_CHUNK_IDS]

            if edge.id in merged_edges:
                # Edge already seen; merge
                existing = merged_edges[edge.id]
                merged_weight = existing.weight + weight
                # Union source_chunk_ids, sort, cap at 20
                merged_chunk_ids = sorted(set(existing.source_chunk_ids) | set(source_chunk_ids))[
                    : _MAX_SOURCE_CHUNK_IDS
                ]
                merged_edges[edge.id] = GraphEdgeInspection(
                    edge_id=edge.id,
                    source_entity_id=edge.source_node_id,
                    target_entity_id=edge.target_node_id,
                    weight=merged_weight,
                    source_chunk_ids=merged_chunk_ids,
                    relationship_type=edge.relationship_type.value,
                )
            else:
                # First time seeing this edge
                merged_edges[edge.id] = GraphEdgeInspection(
                    edge_id=edge.id,
                    source_entity_id=edge.source_node_id,
                    target_entity_id=edge.target_node_id,
                    weight=weight,
                    source_chunk_ids=source_chunk_ids,
                    relationship_type=edge.relationship_type.value,
                )

    # Convert merged dicts to lists
    merged_nodes_list = list(merged_nodes.values())
    merged_edges_list = list(merged_edges.values())

    # Apply TF-IDF scoring if requested: multiply merged frequency salience by IDF
    if salience_mode == "tfidf":
        merged_nodes_list = _apply_tfidf(merged_nodes_list, entity_presence, num_collections)  # type: ignore[arg-type]

    truncated_nodes, truncated_edges, truncation_fired = _truncate_graph(
        merged_nodes_list, merged_edges_list, max_nodes, max_edges, salience_mode=salience_mode
    )

    # edge_count is pre-edge-cap (after node filter)
    total_edge_count = len(merged_edges_list)

    truncated = truncation_fired or mentions_ceiling_hit

    return CrossCollectionGraphView(
        collections=collections,
        nodes=truncated_nodes,
        edges=truncated_edges,
        node_count=len(merged_nodes),
        edge_count=total_edge_count,
        truncated=truncated,
        salience_mode=salience_mode,
    )


def to_graphml(view: CollectionGraphView | CrossCollectionGraphView) -> bytes:
    """Convert a graph view (single- or cross-collection) to GraphML XML bytes.

    Produces a directed graph with nodes and edges, including derived attributes
    (chunk_count, salience, weight, source_chunk_ids). Includes a graph-level
    `<data>` element for the truncated flag.

    Args:
        view: A CollectionGraphView or CrossCollectionGraphView to export.

    Returns:
        GraphML XML as UTF-8 encoded bytes.

    Raises:
        ImportError: If networkx is not installed.
    """
    try:
        import networkx as nx  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError(
            "GraphML export requires networkx; install archon-search[graph]"
        )

    import tempfile
    import os
    import xml.etree.ElementTree as ET

    # Create directed graph
    G = nx.DiGraph()

    # Add nodes with attributes
    for node in view.nodes:
        G.add_node(
            node.entity_id,
            entity_name=node.entity_name,
            chunk_count=node.chunk_count,
            salience=node.salience,
            entity_type=node.entity_type,
        )

    # Add edges with attributes
    for edge in view.edges:
        G.add_edge(
            edge.source_entity_id,
            edge.target_entity_id,
            weight=edge.weight,
            source_chunk_ids=",".join(edge.source_chunk_ids),  # CSV for XML compat
        )

    # Write to temporary file (networkx works better with file paths)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.graphml', delete=False) as f:
        temp_path = f.name
        nx.write_graphml(G, temp_path)

    try:
        # Read the GraphML from the temporary file
        with open(temp_path, 'rb') as f:
            graphml_bytes = f.read()

        # Parse to add graph-level truncated data
        graphml_xml = graphml_bytes.decode('utf-8')
        root = ET.fromstring(graphml_xml)

        # Extract namespace from the root element (if present)
        namespace = None
        if '}' in root.tag:
            namespace = root.tag.split('}')[0] + '}'

        # Find the <graph> element
        if namespace:
            graph_elem = root.find(f".//{namespace}graph")
        else:
            graph_elem = root.find(".//graph")

        if graph_elem is not None and namespace:
            # Insert a <data> element for the truncated flag with proper namespace
            truncated_data = ET.Element(f"{namespace}data")
            truncated_data.set("key", "truncated")
            truncated_data.text = "true" if view.truncated else "false"
            # Insert at the beginning (index 0)
            graph_elem.insert(0, truncated_data)
        elif graph_elem is not None:
            # No namespace, create unnamespaced element
            truncated_data = ET.Element("data")
            truncated_data.set("key", "truncated")
            truncated_data.text = "true" if view.truncated else "false"
            graph_elem.insert(0, truncated_data)

        # Convert back to bytes
        graphml_bytes = ET.tostring(root, encoding="utf-8")

        return graphml_bytes
    finally:
        # Clean up the temporary file
        os.unlink(temp_path)

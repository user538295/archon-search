"""Graph inspection use case for reading and deriving graph metrics — E2b.

Reads nodes, edges, and mentions from graph tables; derives chunk_count, salience,
weight, and source_chunk_ids; applies deterministic truncation; returns inspection
views for both single-collection and cross-collection queries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.graph_store import GraphStore

logger = logging.getLogger(__name__)

# Safety valve against OOM on pathologically large mention tables; adjust via
# operator-level config if needed.
_MENTIONS_SCAN_CEILING = 500_000


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
    """Chunk frequency relative to total collection chunks; clamped to [0.0, 1.0]."""


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


@dataclass
class CollectionGraphView:
    """Inspection view of a single collection's graph."""

    nodes: list[GraphNodeInspection]
    """Truncated node list; sorted by (chunk_count desc, entity_id asc) then capped."""
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


# Maximum number of source chunk IDs to include per edge
_MAX_SOURCE_CHUNK_IDS = 20


def _truncate_graph(
    nodes: list[GraphNodeInspection],
    edges: list[GraphEdgeInspection],
    max_nodes: int,
    max_edges: int,
) -> tuple[list[GraphNodeInspection], list[GraphEdgeInspection], bool]:
    """Apply deterministic truncation to nodes and edges.

    Steps:
    1. Sort nodes by (chunk_count desc, entity_id asc); cap at max_nodes.
    2. Build set of surviving entity IDs.
    3. Filter edges to those where BOTH source and target are in surviving set.
    4. Sort surviving edges by (weight desc, edge_id asc); cap at max_edges.
    5. Return (nodes_out, edges_out, truncated) where truncated=True if either cap fired.

    Args:
        nodes: List of GraphNodeInspection objects to truncate.
        edges: List of GraphEdgeInspection objects to filter and truncate.
        max_nodes: Maximum number of nodes to return.
        max_edges: Maximum number of edges to return.

    Returns:
        Tuple of (truncated_nodes, truncated_edges, truncated_flag).
    """
    # Step 1: Sort and cap nodes
    sorted_nodes = sorted(nodes, key=lambda n: (-n.chunk_count, n.entity_id))
    nodes_out = sorted_nodes[:max_nodes]
    node_truncated = len(sorted_nodes) > max_nodes

    # Step 2: Build set of surviving entity IDs
    surviving_entity_ids = {n.entity_id for n in nodes_out}

    # Step 3: Filter edges to survivors
    surviving_edges = [
        e
        for e in edges
        if e.source_entity_id in surviving_entity_ids
        and e.target_entity_id in surviving_entity_ids
    ]

    # Step 4: Sort and cap edges
    sorted_edges = sorted(surviving_edges, key=lambda e: (-e.weight, e.edge_id))
    edges_out = sorted_edges[:max_edges]
    edge_truncated = len(sorted_edges) > max_edges

    truncated = node_truncated or edge_truncated

    return nodes_out, edges_out, truncated


async def inspect_collection(
    graph_store: "GraphStore",
    collection: str,
    total_chunk_count: int,
    max_nodes: int,
    max_edges: int,
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

    Returns:
        CollectionGraphView with derived metrics and truncation info.

    Note:
        - Pre-E2b nodes (no mentions) read as chunk_count=0, salience=0.0.
        - Mentions scan ceiling: if mention count >= _MENTIONS_SCAN_CEILING,
          truncated=True even if node/edge caps don't fire.
        - Salience is clamped: salience = min(chunk_count / total_chunk_count, 1.0).
    """
    # Fetch all nodes and edges
    all_nodes = await graph_store.get_all_nodes(collection)
    all_edges = await graph_store.get_all_edges(collection)

    # Fetch mentions up to the ceiling
    all_mentions = await graph_store.get_all_mentions(collection, limit=_MENTIONS_SCAN_CEILING)

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
        # Salience = chunk_count / total_chunk_count, clamped to [0.0, 1.0]
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
            )
        )

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
            )
        )

    # Apply truncation; _truncate_graph filters edges to surviving nodes and caps both
    truncated_nodes, truncated_edges, truncation_fired = _truncate_graph(
        node_inspections, edge_inspections, max_nodes, max_edges
    )

    # Compute edge_count: number of edges where both endpoints survive the node filter
    # (before the edge cap; _truncate_graph's surviving_edges is what we need)
    sorted_nodes_pre_cap = sorted(node_inspections, key=lambda n: (-n.chunk_count, n.entity_id))
    surviving_node_ids_pre_cap = {n.entity_id for n in sorted_nodes_pre_cap[:max_nodes]}
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
    )


async def inspect_cross_collection(
    graph_store: "GraphStore",
    collections: list[str],
    total_chunk_counts: dict[str, int],
    max_nodes: int,
    max_edges: int,
) -> CrossCollectionGraphView:
    """Inspect and merge graph data across multiple collections.

    For each collection, fetches nodes and edges, then merges them:
    - Nodes are deduplicated by entity_id; chunk_counts and saliences are merged
      using weighted average.
    - Edges are deduplicated by edge_id; weights are summed; source_chunk_ids
      are unioned and capped at 20.

    Args:
        graph_store: GraphStore instance (already connected).
        collections: List of collection names to merge.
        total_chunk_counts: Dict mapping collection name → total chunk count
            (for per-collection salience denominators before averaging).
        max_nodes: Maximum nodes to return (truncation cap).
        max_edges: Maximum edges to return (truncation cap).

    Returns:
        CrossCollectionGraphView with merged data and truncation info.
    """
    merged_nodes: dict[str, GraphNodeInspection] = {}
    merged_edges: dict[str, GraphEdgeInspection] = {}
    total_mentions_scanned = 0
    mentions_ceiling_hit = False

    for collection in collections:
        # Fetch nodes, edges, mentions for this collection
        all_nodes = await graph_store.get_all_nodes(collection)
        all_edges = await graph_store.get_all_edges(collection)
        all_mentions = await graph_store.get_all_mentions(collection, limit=_MENTIONS_SCAN_CEILING)

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
                merged_nodes[node.id] = GraphNodeInspection(
                    entity_id=node.id,
                    entity_name=node.entity_name,
                    chunk_count=merged_chunk_count,
                    salience=merged_salience,
                )
            else:
                # First time seeing this node
                merged_nodes[node.id] = GraphNodeInspection(
                    entity_id=node.id,
                    entity_name=node.entity_name,
                    chunk_count=chunk_count,
                    salience=salience,
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
                )
            else:
                # First time seeing this edge
                merged_edges[edge.id] = GraphEdgeInspection(
                    edge_id=edge.id,
                    source_entity_id=edge.source_node_id,
                    target_entity_id=edge.target_node_id,
                    weight=weight,
                    source_chunk_ids=source_chunk_ids,
                )

    # Convert merged dicts to lists and apply truncation
    merged_nodes_list = list(merged_nodes.values())
    merged_edges_list = list(merged_edges.values())

    truncated_nodes, truncated_edges, truncation_fired = _truncate_graph(
        merged_nodes_list, merged_edges_list, max_nodes, max_edges
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

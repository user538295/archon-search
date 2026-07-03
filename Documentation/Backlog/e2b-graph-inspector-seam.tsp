/** Internal logical seam: graph inspection use case.
 * Boundary: routes_graph.py (Presentation) → graph_inspector.py (Use Cases).
 * Realised by: BE-6, BE-9.  Verified by: BE-6 (unit + integration), BE-9 (unit + integration).
 */
namespace E2bGraphInspectorSeam;

/** A graph node enriched with derived salience metrics. */
model GraphNodeView {
  entity_id: string;
  entity_name: string;
  entity_type: string;
  entity_subtype: string | null;
  chunk_count: int32;
  salience: float32;
}

/** A graph edge enriched with co-occurrence weight. */
model GraphEdgeView {
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
  relationship_type: string;
  weight: int32;
  source_chunk_ids: string[];
}

/** Result of inspecting a single collection's graph. */
model CollectionGraphView {
  collection: string;
  nodes: GraphNodeView[];
  edges: GraphEdgeView[];
  node_count: int32;
  edge_count: int32;
  truncated: boolean;
}

/** Result of inspecting a merged cross-collection graph. */
model CrossCollectionGraphView {
  collections: string[];
  nodes: GraphNodeView[];
  edges: GraphEdgeView[];
  node_count: int32;
  edge_count: int32;
  truncated: boolean;
}

/** Graph inspection use case interface. */
interface GraphInspector {
  /** Fetch + derive + truncate for a single collection.
   * Returns empty nodes/edges (truncated=false) when graph tables are absent or empty.
   * Salience = chunk_count / collection_chunk_count (0.0 when denominator is 0).
   * `total_chunk_count` is passed in by the route handler from CollectionMeta.chunk_count;
   * the inspector does not fetch collection meta itself.
   */
  inspect_collection(
    collection: string,
    total_chunk_count: int32,
    max_nodes: int32,
    max_edges: int32,
  ): CollectionGraphView;

  /** Merge nodes (deduplicate by entity_id, sum chunk_counts, weighted-avg salience)
   * and edges (deduplicate by edge id, sum weights, union source_chunk_ids capped at 20)
   * across two or more collections.
   * `total_chunk_counts` maps collection name → CollectionMeta.chunk_count for each collection;
   * passed in by the route handler, not fetched by the inspector.
   */
  inspect_cross_collection(
    collections: string[],
    total_chunk_counts: Record<string, int32>,
    max_nodes: int32,
    max_edges: int32,
  ): CrossCollectionGraphView;
}

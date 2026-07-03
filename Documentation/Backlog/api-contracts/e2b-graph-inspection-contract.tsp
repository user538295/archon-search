import "@typespec/http";
using Http;

@service(#{ title: "E2B Graph Inspection API" })
namespace E2bGraphInspection;

/** Graph export format. */
union GraphFormat {
  json: "json",
  graphml: "graphml",
}

/** A single graph node with derived salience metrics computed from the mentions table. */
model GraphNodeResponse {
  /** SHA-256 hex of "{entity_type}:{entity_name}". Stable across re-ingest. */
  entity_id: string;
  entity_name: string;
  entity_type: string;
  entity_subtype: string | null;
  /** Number of distinct chunks in which this entity is mentioned. 0 for pre-E2b nodes. */
  chunk_count: int32;
  /** chunk_count / total_chunks_in_collection. 0.0 for pre-E2b nodes or empty collection. */
  salience: float32;
}

/** A single graph edge with co-occurrence weight computed from the mentions table. */
model GraphEdgeResponse {
  /** SHA-256 hex of "{source_id}:{target_id}:{relationship_type}". */
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
  relationship_type: string;
  /** Number of distinct chunks where both endpoints are mentioned. 0 for pre-E2b edges. */
  weight: int32;
  /** Up to 20 chunk IDs contributing to the co-occurrence weight. Empty for pre-E2b edges. */
  source_chunk_ids: string[];
}

/** Full adjacency response for one collection. Returned when format=json (default). */
model GraphInspectionResponse {
  collection: string;
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  /** Total nodes before truncation was applied. */
  node_count: int32;
  /** Total edges before truncation was applied. */
  edge_count: int32;
  /** True when node or edge count exceeded the configured ceiling and the response was deterministically truncated. */
  truncated: boolean;
}

/** Full adjacency response for a merged cross-collection view. */
model CrossCollectionGraphInspectionResponse {
  collections: string[];
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  node_count: int32;
  edge_count: int32;
  truncated: boolean;
}

/** 422 error when graph.enabled = false. */
model GraphDisabledError {
  detail: "graph inspection requires [graph] enabled=true in server config";
}

/** 404 error when a collection does not exist in the caller's namespace. */
model CollectionNotFoundError {
  detail: "collection not found";
}

/** 422 error when cross-collection query has fewer than 2 distinct collections. */
model CrossCollectionMinimumError {
  detail: string;
}

@route("/graph")
interface GraphInspection {
  /** Return the full node and edge list for a single collection.
   * Use ?format=graphml to receive a GraphML document (application/xml) for import into Gephi, yEd, or Neo4j.
   * Returns empty nodes/edges (not 404) when graph.enabled=true but no ingest has run yet.
   */
  @get
  @route("/{collection}")
  getSingleCollection(
    @path collection: string,
    @query format?: GraphFormat,
  ): GraphInspectionResponse | {
    @statusCode statusCode: 422;
    @body body: GraphDisabledError;
  } | {
    @statusCode statusCode: 404;
    @body body: CollectionNotFoundError;
  };

  /** Return a merged node and edge list across two or more collections.
   * Nodes are deduplicated by entity_id (chunk_counts summed, salience is chunk-count-weighted average).
   * Edges are deduplicated by edge id (weights summed, source_chunk_ids unioned, capped at 20).
   * Requires at least 2 distinct collections; returns 422 otherwise.
   */
  @get
  @route("/cross-collection")
  getCrossCollection(
    @query collections: string,
    @query format?: GraphFormat,
  ): CrossCollectionGraphInspectionResponse | {
    @statusCode statusCode: 422;
    @body body: GraphDisabledError | CrossCollectionMinimumError;
  } | {
    @statusCode statusCode: 404;
    @body body: CollectionNotFoundError;
  };
}

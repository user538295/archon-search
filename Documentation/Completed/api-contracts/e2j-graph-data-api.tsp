import "@typespec/http";

using Http;

@service(#{ title: "Graph Data API — E2j: entity_type field added to node response" })
namespace GraphDataApi;

/** Entity types the graph recognises. */
union EntityType {
  person: "person",
  concept: "concept",
  system: "system",
  event: "event",
  code_symbol: "code_symbol",
}

/** A single graph node returned by GET /graph/{collection}. */
model GraphNodeResponse {
  entity_id: string;
  entity_name: string;
  /** Added in E2j. Corresponds to EntityType enum values in graph_types.py. */
  entity_type: string;
  chunk_count: int32;
  salience: float64;
  pagerank_score?: float64 | null;
}

/** A single graph edge returned by GET /graph/{collection}. */
model GraphEdgeResponse {
  edge_id: string;
  source_entity_id: string;
  target_entity_id: string;
  /** Co-occurrence chunk count; maps to edge thickness in the viewer. */
  weight: int32;
  /** Up to 20 chunk IDs where both endpoints co-occur. */
  source_chunk_ids: string[];
  /** Defaults to "related_to". Use for hover tooltip in the viewer. */
  relationship_type: string;
}

model GraphInspectionResponse {
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  /** True when node_count > displayed nodes or edge_count > displayed edges. */
  truncated: boolean;
  /** Total nodes before truncation cap. */
  node_count: int32;
  /** Edges where both endpoints survive the node filter, before edge cap. */
  edge_count: int32;
  salience_mode: "frequency" | "tfidf" | "importance";
}

@route("/graph/{collection}")
interface GraphData {
  @get get(
    @path collection: string,
    @query format?: "json" | "graphml",
    @query salience?: "frequency" | "tfidf" | "importance",
  ): GraphInspectionResponse;
}

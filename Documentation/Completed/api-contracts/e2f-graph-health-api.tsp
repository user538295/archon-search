import "@typespec/http";
using Http;

@service(#{ title: "E2f Graph Health API" })
namespace E2fGraphHealthApi;

// E2f: four new health metric fields added to the existing GraphCollectionStats model.
// Additive change — all new fields have zero defaults for backward compatibility.
model GraphCollectionStats {
  collection: string;
  node_count: int32;
  edge_count: int32;
  communities_invalidated: boolean;
  // E2f additions (optional — zero/null defaults for backward compatibility):
  synonym_edge_count?: int32;         // synonym_of edges in this collection
  singleton_node_pct?: float64;       // nodes with no edges as % of total nodes
  synonym_link_rate?: float64;        // synonym links / total node pairs scanned this pass
  connected_component_count?: int32;  // weakly-connected component count
}

// E2f: relationship_type added to edge inspection responses.
// Default "related_to" for pre-E2f edges without an explicit type stored.
model GraphEdgeResponse {
  edge_id: string;
  // HTTP schema name: source_entity_id maps to domain field source_node_id on GraphEdge (graph_types.py). Deliberate divergence — HTTP names match existing GraphEdgeResponse in schemas.py.
  source_entity_id: string;
  // HTTP schema name: target_entity_id maps to domain field target_node_id on GraphEdge (graph_types.py). Deliberate divergence — HTTP names match existing GraphEdgeResponse in schemas.py.
  target_entity_id: string;
  weight: int32;
  source_chunk_ids: string[];
  relationship_type?: string;
}

model GraphNodeResponse {
  node_id: string;
  entity_name: string;
  entity_type: string;
}

model GraphInspectionResponse {
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  node_count: int32;
  edge_count: int32;
  truncated: boolean;
}

model GraphStatusDetail {
  enabled: boolean;
  backend_threshold_edges: int32;
  stale_mention_count: int32;
  collections: GraphCollectionStats[];
}

@route("/status")
interface Status {
  @get get(): GraphStatusResponse;
}

model GraphStatusResponse {
  graph?: GraphStatusDetail | null;
}

@route("/graph/{collection}")
interface GraphInspection {
  @get inspect(@path collection: string): GraphInspectionResponse;
}

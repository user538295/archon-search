import "@typespec/http";

using Http;

@service(#{ title: "Graph Inspection API — E2i extension" })
namespace GraphInspectionAPI;

model GraphNodeResponse {
  nodeId: string;
  entityName: string;
  entityType: string;
  frequency: int32;
}

model GraphEdgeResponse {
  sourceId: string;
  targetId: string;
  relationshipType: string;
  extractionMethod: string | null;
}

/** Extended graph inspection response adding community-summary telemetry. */
model GraphInspectionResponse {
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  truncated: boolean;
  nodeCount: int32;
  edgeCount: int32;
  salienceMode: "frequency" | "tfidf" | "importance";
  /** Total number of communities for this collection. */
  communitiesTotal: int32;
  /** Communities that have a non-null summary_text. */
  communitiesSummarized: int32;
  /** IDs of communities missing summary_text; capped at 100. */
  unsummarizedCommunityIds: string[];
}

@route("/graph/{collection}")
interface GraphInspection {
  @get get(@path collection: string): GraphInspectionResponse;
}

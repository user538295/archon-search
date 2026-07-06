import "@typespec/http";
using Http;

@service(#{ title: "E1B Search Graph Mode API" })
namespace E1bSearchGraphMode;

/** Allowed graph retrieval modes. */
union GraphMode {
  naive: "naive",
  local: "local",
  global: "global",
}

model SearchFilters {
  fileType: string | null;
  language: string | null;
  sourcePathPrefix: string | null;
  sourcePathGlob: string | null;
  indexedAfter: utcDateTime | null;
  indexedBefore: utcDateTime | null;
}

model SearchResultItem {
  chunkId: string;
  score: float32;
  sourceDocId: string;
  collection: string;
  text: string;
}

model SearchRequest {
  collection: string | null;
  collections: string[] | null;
  query: string;
  topK: int32;
  filters: SearchFilters | null;
  hyde: boolean;
  ragFusion: boolean;
  /** Null = standard hybrid search. "naive" = E1a entity expansion. "local" = community-scoped. "global" = all-community representatives. */
  graphMode: GraphMode | null;
}

model SearchResponse {
  results: SearchResultItem[];
  aclFiltered: boolean;
  embeddingModel: string;
  hydeApplied: boolean;
  ragFusionApplied: boolean;
  /** True when graph retrieval (local or global) augmented the result set. */
  graphExpansionApplied: boolean;
  appliedFilters: SearchFilters | null;
}

model GraphCommunitiesNotBuiltError {
  detail: {
    code: "graph_communities_not_built";
    message: string;
  };
}

@route("/search")
interface Search {
  @post search(@body body: SearchRequest): SearchResponse | {
    @statusCode statusCode: 422;
    @body body: GraphCommunitiesNotBuiltError;
  };
}

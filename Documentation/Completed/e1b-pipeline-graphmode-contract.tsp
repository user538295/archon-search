namespace PipelineGraphModeContract;

/** Allowed values for the graph_mode parameter. */
enum GraphMode {
  naive: "naive",
  local: "local",
  global: "global",
}

/** Extended pipeline result carrying graph expansion metadata. */
model SearchPipelineResult {
  results: SearchResult[];
  graphExpansionApplied: boolean;
  /** @deferred E1c — do NOT implement in E1b; field intentionally absent from the E1b implementation */
  graphFallbackReason: string | null;
}

model SearchResult {
  chunkId: string;
  score: float32;
  sourceDocId: string;
  text: string;
}

/** Extended SearchPipeline.search() contract for graph modes. */
interface SearchPipeline {
  search(
    query: string,
    collection: string,
    graphMode: GraphMode | null,
  ): SearchPipelineResult;
}

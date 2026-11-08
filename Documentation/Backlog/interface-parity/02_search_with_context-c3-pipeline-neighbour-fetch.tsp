// C3 — Service <-> pipeline/store: the search plus neighbour fetch under one budget (Use Cases <-> Frameworks & Drivers, internal).
// Today `SearchPipeline.search_with_context` calls `search` without `top_k`, `rerank` or `fts_only`, validates the window against a pipeline-held cap and fetches neighbours one call per hit. Decided: the method stays the one pipeline step (about twenty pipeline tests call it) and is extended: it gains `top_k`, `rerank` and `fts_only` and forwards them to `search`, loses the cap check, and keeps `record_stage("context")` around the neighbour loop. The service calls it once, inside its single `run_within_budget`.
namespace PipelineNeighbourFetchContract;

// What the service passes to `SearchPipeline.search_with_context` (keyword arguments; the names are the pipeline's). `query`, `collection`, `namespace`, `embedder`, `filters`, `query_vector` (the HyDE vector), `rag_fusion`, `rag_fusion_generator`, `rag_fusion_config` and `scope_filter` are already parameters; `context_window` is already one; `top_k`, `rerank` and `fts_only` are the NEW ones. `graph_mode` is never passed.
model NeighbourSearchRequest {
  query: string;
  collection: string;
  `namespace`: string;
  context_window: int32;
  top_k?: int32;
  // False when the cross-encoder is still cold: un-reranked answer, silently (accepted limitation, S1 brief).
  rerank: boolean;
  // True on a cold embedder: keyword-only answer, silently (accepted limitation, S1 brief).
  fts_only: boolean;
  // The parsed SearchFilters (filters.py), as the service holds them.
  filters?: Record<string>;
  query_vector?: float64[];
  rag_fusion: boolean;
  scope_filter?: string;
}

model Neighbour {
  chunk_id: string;
  index: int32;
}

// Per hit. A hit chunk_id without a numeric suffix gives empty context and one warning log line (as today). Neighbour ids are built by the store from the document id and the index (store.py lines 3396-3399), so a malformed neighbour id cannot occur and the defensive skip in the loop is dead code the extension may delete. A per-hit store ValueError (bad doc id or collection name) is practically unreachable (the collection name is checked before any store call, search_service.py line 309; a doc id is always 64 hex characters) and keeps the generic path: the whole request ends as `internal_error`, nothing partial.
model HitNeighbours {
  hit_chunk_id: string;
  before: Neighbour[];
  after: Neighbour[];
}

// Invariants:
// - Neighbours come from the SAME document, indexes in [centre - window, centre + window] excluding the centre, the lower bound clamped at 0; ids past the end of the document are simply absent; a missing collection table gives [].
// - Neighbours are ACL-filtered with the caller's namespace (acl.apply_acl_filter); denied ones are dropped silently, the hit stays. Neighbours are NOT scope-filtered: they are chunks of the hit's own document and every chunk of a document gets the same scope tags at ingest (pipeline.py lines 1000-1003), so the filter would change nothing (UserManual/60_searching.md line 157 documents it).
// - The search and the whole neighbour loop run inside ONE `run_within_budget` (30 s, SEARCH_TIMEOUT_SECONDS): exceeding it cancels both and returns nothing partial (ServiceError timeout, REST 504). The pipeline `context` stage timing is kept.
// - The pipeline no longer holds the context-window cap: `context_window_max` (constructor argument, property) and `context_window_error` leave `SearchPipeline`; validation is the service's (C2). The other callsites change with it: app.py line 1442 and pipeline.py line 4264 (`context_window_max=` arguments), mcp.py lines 104 and 685 (the `context_window_error` import and `pipeline.context_window_max`). Fixtures that build `SearchPipeline(context_window_max=...)` are updated.
// - One store call per hit (N+1) is kept: batching and parallel fetch are out of scope (brief Out of Scope, Future Iterations); the latency at top_k_max is measured, not decided (plan S47).
interface PipelineNeighbourFetch {
  search_with_neighbours(request: NeighbourSearchRequest): HitNeighbours[];
}

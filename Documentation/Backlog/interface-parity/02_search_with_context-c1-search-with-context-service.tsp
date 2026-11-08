// C1 — SearchService.search_with_context: command, outcome and service (Interface Adapters <-> Use Cases).
// Logical seam: what an adapter hands to the service and what it gets back. No transport types cross it. It follows the shapes of S1's C1 (01_search-c1-search-service.tsp); the shared types are restated here because each contract file stands alone.
namespace SearchWithContextServiceContract;

// The resolved caller (services/authz AuthIdentity). Adapters resolve it and refuse a missing identity BEFORE calling the service.
model Caller {
  `namespace`: string;
  admin: boolean;
}

model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  // Lives inside the filters on REST; the service ORs it with the top-level flag once (REST's rule).
  include_metadata?: boolean;
}

// Own command type (brief In Scope): SearchCommand minus `collections` and `graph_mode`, plus `context_window`. Raw, adapter-built input; the service owns the canonical validation (C2).
model SearchWithContextCommand {
  query: string;
  // Exactly one collection, required by the rule and optional in the type: an adapter passes what the caller sent (MCP passes none when the argument is omitted, as `search` does) and the normaliser raises the validation error `collection must not be empty` for a missing or blank value, so MCP answers `validation_error` and not FastMCP's own argument error. An unknown or invalid name is `not_found` (checked before any store call).
  collection?: string;
  // Neighbours per side. Omitted = DEFAULT_CONTEXT_WINDOW (1). 0 is allowed (hits only). Upper bound: the operator's [search].context_window_max (C2).
  context_window?: int32;
  // Omitted = the configured `top_k_return`; bounded by [search].top_k_max, as in `search`.
  top_k?: int32;
  filters?: SearchFilters;
  include_metadata: boolean;
  hyde: boolean;
  rag_fusion: boolean;
  scope_filter?: string;
  acl_context: boolean;
}

enum AclSource {
  frontmatter,
  sidecar,
  collection_default,
}

// Built once in the service for each HIT only, and only when `acl_context` was requested (brief Key Decisions: one gate rule, same as `search`). Neighbours never carry it; they carry their `acl` list.
model AclGate {
  allowed_principals?: string[];
  source?: AclSource;
  sidecar_path?: string;
  warnings: string[];
}

// The core per-chunk hit (archon_search/_types.SearchResult), carried raw; `metadata` is never blanked by the service.
model ChunkHit {
  doc_id: string;
  chunk_id: string;
  text: string;
  score: float64;
  source_path: string;
  file_type: string;
  language: string;
  indexed_at: string;
  updated_at: string;
  ingested_by: string;
  metadata: Record<string>;
  acl?: string[];
  collection: string;
  acl_source?: string;
  acl_sidecar_path?: string;
  acl_warning: string[];
  reranker_score?: float64;
}

// A neighbour chunk (archon_search/_types.ChunkRecord) as the service returns it: ACL-filtered, same document, ordered by chunk index, never the hit itself. All stored fields are carried raw, including the service-only ones (vector, scopes, expires_at, offsets, custom_score and the ACL provenance); adapters render only the wire fields of C5 / C6 and drop the rest, and the field-level parity check (C10) records the dropped ones as waivers.
model ContextChunk {
  doc_id: string;
  chunk_id: string;
  text: string;
  source_path: string;
  indexed_at: string;
  file_type: string;
  language: string;
  updated_at: string;
  ingested_by: string;
  metadata: Record<string>;
  acl?: string[];
  vector: float64[];
  custom_score?: float64;
  expires_at?: string;
  scopes?: string[];
  start_offset: int32;
  end_offset: int32;
  acl_source?: string;
  acl_sidecar_path?: string;
  acl_warning: string[];
}

// One ranked hit plus its neighbours.
model ContextHit {
  result: ChunkHit;
  // Built once, hits only; absent unless `acl_context` was requested.
  acl_gate?: AclGate;
  // Chunks with an index below the hit's, in ascending index order. Empty for context_window 0, for a hit whose chunk_id has no numeric suffix (the service logs a warning), and when every neighbour was denied by ACL.
  context_before: ContextChunk[];
  // Chunks with an index above the hit's, ascending. Same emptiness rules.
  context_after: ContextChunk[];
}

// Own outcome type. Carries the derived fields of `search`'s outcome that the brief lists. `excluded_collections`, `graph_expansion_applied` and `ppr_entities_matched` are absent on purpose: they come only from the multi-collection paths and from `graph_mode`, and this command has neither, so they would be constant empty values.
model SearchWithContextOutcome {
  results: ContextHit[];
  // True when the hit-level ACL filter removed hits. Neighbours dropped by ACL are NOT reported (brief Out of Scope).
  acl_filtered: boolean;
  embedding_model: string;
  hyde_applied: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  // hyde_applied OR rag_fusion_applied (the graph term of `search`'s rule is always false here).
  expansion_used: boolean;
  expansion_warning?: string;
  // Absent when no filter was given, else the filters as parsed.
  applied_filters?: SearchFilters;
  // The two request flags ORed once; adapters blank `metadata` on hits AND neighbours unless true (verified: the MCP tool does so today).
  include_metadata: boolean;
}

// Flow (brief Core Flow, same order and rules as `search`): normalise and validate (C2) -> look the collection up in the caller's namespace (invalid name or none: not_found; store failure: metadata_store_error) -> select the collection's embedder (0.5 s bound, else keyword-only) -> warm-up outside the budget -> resolve HyDE (skipped on a cold embedder) ->
// ONE 30 s budget around the search AND the neighbour fetch (C3), nothing partial on timeout -> ACL-filter neighbours silently -> derive the outcome once -> record telemetry with the label `search_with_context` (C4) -> return.
// Failures are raised as ServiceError (S1's closed ServiceErrorKind set, `01_search-c2-service-error.tsp`); nothing transport-specific is accepted or raised. `graph_mode` does not exist on this command.
// The steps shared with `search` (normalisation, collection lookup, embedder selection, warm-up, HyDE resolution, budget, error translation, stage timings) are factored out of `SearchService.search`, not copied.
interface SearchWithContextService {
  search_with_context(command: SearchWithContextCommand, caller: Caller): SearchWithContextOutcome;
}

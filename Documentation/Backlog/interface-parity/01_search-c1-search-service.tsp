// C1 — Search command, result and service (Interface Adapters <-> Use Cases).
// Logical seam: what an adapter hands to SearchService and what it gets back. No transport types cross it.
namespace SearchServiceContract;

enum GraphMode {
  naive,
  local,
  global,
  ppr,
}

// Canonical filter fields (core SearchFilters). REST nests them under `filters`; MCP and the CLI pass them flat; all three build this one model.
model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  // ISO date or date-time; the service treats a bare date as UTC midnight (core behaviour, unchanged).
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  // Response-shaping flag that lives inside the filters on REST; the service ORs it with the top-level command flag (REST's rule) once.
  include_metadata?: boolean;
}

// The resolved caller (services/authz AuthIdentity). Adapters resolve it and refuse a missing identity BEFORE calling the service.
model Caller {
  `namespace`: string;
  admin: boolean;
}

// Raw, adapter-normalised input. The service owns the canonical validation (see ServiceError kinds in C2).
model SearchCommand {
  // The service strips it; empty after stripping is a validation error.
  query: string;
  // Exactly one of `collection` (single path) or `collections` (multi path). The service strips, de-duplicates and rejects empty names.
  collection?: string;
  collections?: string[];
  // Per-request result count (brief Q1). Omitted = the configured `top_k_return` exactly as today (no schema default on any interface). Bounds: 1 <= top_k <= top_k_max. When given, the candidate pool is at least top_k (C3).
  top_k?: int32;
  filters?: SearchFilters;
  // The top-level flag as the caller sent it. The service ORs it with `filters.include_metadata` (REST's rule) exactly once (`SearchCommand.wants_metadata`), so both raw flags are carried. The ORed value leaves the service as `SearchOutcome.include_metadata`.
  include_metadata: boolean;
  hyde: boolean;
  rag_fusion: boolean;
  // A plain string, one of GraphMode; a value outside the set is the `invalid_graph_mode` ServiceError (REST rejects it earlier, in its request model).
  graph_mode?: string;
  scope_filter?: string;
  acl_context: boolean;
}

enum AclSource {
  frontmatter,
  sidecar,
  collection_default,
}

// The ACL provenance envelope, built ONCE by the service for each hit, and only when `acl_context` was requested; adapters only render it. The source is sanitised against the valid set here (`VALID_ACL_SOURCES` in constants.py), which replaces the `_VALID_ACL_SOURCES` copies in routes_search.py and routes_explain.py.
model AclGate {
  allowed_principals?: string[];
  source?: AclSource;
  sidecar_path?: string;
  warnings: string[];
}

// The core per-chunk hit (archon_search/_types.SearchResult), carried raw and untouched, `metadata` included: the service never blanks it. Adapters render their own wire schema from it.
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
  // Carried as the pipeline returned it. The adapter blanks it unless `SearchOutcome.include_metadata` is true (plan S14).
  metadata: Record<string>;
  acl?: string[];
  collection: string;
  // Raw ACL provenance of the stored chunk. The responses show it only through `SearchHit.acl_gate` (C9 waives these three on REST and MCP).
  acl_source?: string;
  acl_sidecar_path?: string;
  acl_warning: string[];
  reranker_score?: float64;
}

// One ranked chunk plus its gate (services/search_service.SearchHit).
model SearchHit {
  result: ChunkHit;
  // Built once in the service, only when `acl_context` was requested (absent otherwise); adapters render it, they never rebuild it from raw ACL fields.
  acl_gate?: AclGate;
}

model ExcludedCollection {
  name: string;
  reason: string;
}

// The service result, named SearchOutcome because the core entity already uses `SearchResult`: the raw pipeline hits plus the derived fields.
// Every derived field is computed once here, never again by an adapter.
model SearchOutcome {
  results: SearchHit[];
  acl_filtered: boolean;
  excluded_collections: ExcludedCollection[];
  // Single path: the collection's active model. Multi path: the global configured model (core behaviour, kept).
  embedding_model: string;
  hyde_applied: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  graph_expansion_applied: boolean;
  // hyde_applied OR rag_fusion_applied OR graph_expansion_applied.
  expansion_used: boolean;
  // The HyDE warning, else the RAG Fusion warning, else absent.
  expansion_warning?: string;
  // `null` on the wire (REST: present with a null value) when no filter was given, else the filters as parsed: the SearchFilters after validation (file_type lower-cased and stripped of leading dots, dates coerced), a nested `filters.include_metadata` included (REST echoes the parsed `body.filters`, output unchanged); the top-level `include_metadata` flag is not part of it.
  applied_filters?: SearchFilters;
  ppr_entities_matched?: int32;
  // The two request flags ORed once (`command.include_metadata` OR `filters.include_metadata`, REST's rule). The hits keep their `metadata` untouched; each adapter blanks `metadata` unless this is true. No response field echoes it (C9 waives it on REST and MCP).
  include_metadata: boolean;
}

// Orchestration order is fixed (brief Core Flow): normalise + validate against the operator's limits; single path: check the collection name and look the collection up in the caller's namespace, then select the embedder (bounded handoff, else FTS-only); multi path: the pipeline's global embedder; warm models; resolve HyDE with that embedder (RAG Fusion is passed to the pipeline);
// run under the budget (`pipeline.search` / `search_many`; `top_k` is forwarded only when the command has one); derive the outcome once; record telemetry; return.
// Single path only, with `[observability].stage_timings_enabled` on: the stage timings are logged on success and on every Exception branch. That includes the shim's single-collection branch (`archon-search/<collection>`); the multi path, the shim's fan-out included, logs none.
// Failures are raised as ServiceError (C2); nothing transport-specific (no request object, no HTTP exception, no FastMCP context) is accepted or raised.
// Composition: REST and the shim build the service per request from app.state (`build_search_service_from_state`); the MCP factory keeps its signature and builds the service lazily on the first `search` call from its arguments (`build_search_service`; with no config `search` answers the fixed internal error, `search failed` / `internal_error`, and the other tools are unaffected).
interface SearchService {
  search(command: SearchCommand, caller: Caller): SearchOutcome;
}

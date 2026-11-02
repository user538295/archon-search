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
  // Response-shaping flag that lives inside the filters on REST; the command resolves the REST OR-rule once (see includeMetadataRule).
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
  // Per-request result count (brief Q1). Omitted = the server default (`top_k_return`). Bounds: 1 <= top_k <= top_k_max. The default semantics are Q16 in the plan.
  top_k?: int32;
  filters?: SearchFilters;
  // True when the top-level flag OR `filters.include_metadata` is set (REST's rule, applied once in the service).
  include_metadata: boolean;
  hyde: boolean;
  rag_fusion: boolean;
  graph_mode?: GraphMode;
  scope_filter?: string;
  acl_context: boolean;
}

enum AclSource {
  frontmatter,
  sidecar,
  collection_default,
}

// One ranked chunk: the core per-chunk hit (archon_search/_types.SearchResult), carried raw. Adapters render their own wire schema from it.
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
  // Already blanked by the service unless metadata was requested.
  metadata: Record<string>;
  acl?: string[];
  collection: string;
  reranker_score?: float64;
  // Raw ACL provenance; adapters build the `acl_gate` object from these only when `acl_context` was requested.
  acl_source?: AclSource;
  acl_sidecar_path?: string;
  acl_warning: string[];
}

model ExcludedCollection {
  name: string;
  reason: string;
}

// The service result. The type name is an open question (Q3 in the plan): the core entity already uses `SearchResult`.
// Every derived field is computed once here, never again by an adapter.
model SearchServiceResult {
  results: ChunkHit[];
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
  // Echo of the filters in force; the none-versus-all-null rule for flat callers is Q5 in the plan.
  applied_filters?: SearchFilters;
  ppr_entities_matched?: int32;
}

// Orchestration order is fixed (brief Core Flow): normalise + validate, resolve collections, select embedder (single path), warm models,
// resolve HyDE / RAG Fusion, run under the budget, derive fields once, record telemetry, return.
// Failures are raised as ServiceError (C2); nothing transport-specific (no request object, no HTTP exception, no FastMCP context) is accepted or raised.
interface SearchService {
  search(command: SearchCommand, caller: Caller): SearchServiceResult;
}

import "@typespec/http";
using Http;

@service(#{ title: "Search and Explain API — scope filter (E2a)" })
namespace SearchScopeFilterContract;

/**
 * E2a additions to SearchRequest (POST /search) and ExplainRequest (POST /explain).
 * Partial models — only the fields added by E2a are shown.
 * Full models live in archon_search/server/routes_search.py and routes_explain.py.
 * Existing fields (collection, collections, query, top_k, filters, hyde, rag_fusion, rerank) are unchanged.
 */
model SearchRequestE2aExtension {
  /**
   * Filter search results to chunks whose scopes list contains a value matching this expression.
   * Exact match: bare string (e.g. "user:alice") — matches only if "user:alice" is in chunk.scopes.
   * Wildcard suffix: trailing * (e.g. "user:alice*") — matches any scope with "user:alice" as prefix.
   * Validation: 1–255 chars; allowed chars: alphanumeric, _, -, :, ., /; at most one trailing *.
   * Rejected: bare "*", leading "*", mid-string "*", multiple "*" → 400 with human-readable error.
   * Applied as pre-retrieval LanceDB predicate (exact match) or Python-side post-filter (wildcard).
   * Null or absent = no scope restriction; all chunks are candidates.
   * Incompatibility guard: scope_filter + any graph_mode value → 422
   *   {"detail": "scope_filter is not supported with graph_mode"}.
   *   Rationale: local/global modes bypass scope predicates; naive mode guard is a simplification.
   */
  scope_filter?: string | null;
}

/** Stub search result for reference. */
model SearchResult {
  doc_id: string;
  score: float32;
  text: string;
  source_path: string;
}

model SearchResponse {
  results: SearchResult[];
}

model SearchRequest {
  query: string;
  collection?: string;
  collections?: string[];
  top_k?: int32;
  hyde?: boolean;
  rag_fusion?: boolean;
  /**
   * Incompatibility guard: scope_filter + any graph_mode value → 422
   *   {"detail": "scope_filter is not supported with graph_mode"}.
   *   Rationale: local/global modes bypass scope predicates; naive mode guard is a simplification.
   */
  scope_filter?: string | null;
}

model ExplainRequest {
  query: string;
  collection?: string;
  collections?: string[];
  top_k?: int32;
  rerank?: boolean;
  hyde?: boolean;
  rag_fusion?: boolean;
  /**
   * Incompatibility guard: scope_filter + any graph_mode value → 422
   *   {"detail": "scope_filter is not supported with graph_mode"}.
   *   Rationale: local/global modes bypass scope predicates; naive mode guard is a simplification.
   */
  scope_filter?: string | null;
}

model ExplainResponse {
  results: SearchResult[];
  near_misses: SearchResult[];
}

@route("/search")
interface Search {
  @post search(@body body: SearchRequest): SearchResponse;
}

@route("/explain")
interface Explain {
  @post explain(@body body: ExplainRequest): ExplainResponse;
}

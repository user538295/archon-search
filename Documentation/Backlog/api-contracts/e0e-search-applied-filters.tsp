import "@typespec/http";
using Http;

@service(#{ title: "Search API — multi-collection filter support (E0e)" })
namespace E0eSearchFiltersContract;

/** Filters applied to search results. All fields optional; null applied_filters means no filters were submitted. */
model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  /** ISO 8601 date or datetime string (e.g. "2024-01-15" or "2024-01-15T12:00:00Z"). Coerced to UTC datetime. */
  indexed_after?: string;
  /** ISO 8601 date or datetime string. Coerced to UTC datetime. Must be >= indexed_after if both are set (equal timestamps allowed). */
  indexed_before?: string;
  language?: string;
  include_metadata?: boolean;
}

/** Stub — see archon_search/server/routes_search.py SearchResultSchema for full 12-field shape. */
model SearchResult {
  id: string;
  score: float32;
  content: string;
}

/** See schemas.py ExcludedCollectionSchema. Fields: name (collection name) + reason. */
model ExcludedCollection {
  name: string;
  reason: string;
}

/**
 * SearchResponse after E0e. Additive — applied_filters is the only NEW field added in E0e (excluded_collections existed before E0e).
 * applied_filters echoes the filters from the request body; null when no filters were provided.
 * Lifting the v1 restriction means collections + filters can now coexist.
 */
model SearchResponse {
  results: SearchResult[];
  acl_filtered: boolean;
  embedding_model: string;
  hyde_applied: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  expansion_used: boolean;
  expansion_warning: string | null;
  applied_filters: SearchFilters | null;
  /** Populated for embedding-model mismatches; empty list for zero-result legs. */
  excluded_collections: ExcludedCollection[];
}

model SearchRequest {
  query: string;
  collection?: string;
  /** When provided, filters are applied to every leg independently. */
  collections?: string[];
  /** Accepted in both single- and multi-collection mode after E0e. */
  filters?: SearchFilters;
  top_k?: int32;
  hyde?: boolean;
  rag_fusion?: boolean;
}

@route("/search")
interface Search {
  @post search(@body body: SearchRequest): SearchResponse;
}

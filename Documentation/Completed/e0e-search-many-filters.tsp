namespace E0eSearchManyFiltersSeam;

/**
 * Same 7-field shape as E0eSearchFiltersContract.SearchFilters in e0e-search-applied-filters.tsp.
 * Shown here for seam completeness — kept in sync manually; source of truth is archon_search/filters.py.
 */
model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  include_metadata?: boolean;
}

/**
 * E0e delta view — shows only fields relevant to E0e contract agreement.
 * Full shape: pipeline.py:43-51. Missing from this view: results, excluded_collections, fanout_timings.
 * Option B: applied_filters is constructed at the Presentation layer; SearchPipelineResult does not carry it.
 * The Presentation layer (routes_search.py, mcp.py) echoes the incoming request filters directly in
 * SearchResponse.applied_filters — search_many() returns retrieval results only.
 */
model SearchPipelineResult {
  acl_filtered: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  rag_fusion_warning: string | null;
}

interface SearchManyUseCase {
  /**
   * Multi-collection fan-out search.
   * After E0e: filters is threaded to each store.hybrid_search_with_trace() call per leg.
   * source_path_glob is applied as a Python-side post-filter per leg (mirrors single-collection).
   * Zero results from a leg are silent — excluded_collections is only for embedding-model mismatches.
   */
  op search_many(
    query: string,
    collections: string[],
    ns: string,
    query_vector?: float32[],
    filters?: SearchFilters,
    rag_fusion?: boolean
  ): SearchPipelineResult;
}

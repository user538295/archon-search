namespace SearchPipelineResultContract;

/**
 * E0b delta — shows only the field ADDED by E0b L7.
 * This is an internal seam contract (--no-emit, validation only).
 * The full SearchPipelineResult dataclass lives in archon_search/pipeline.py.
 * Existing fields (results: list[SearchResult], acl_filtered: bool,
 * excluded_collections: list[ExcludedCollection], fanout_timings, etc.) are unchanged.
 */
model SearchPipelineResultE0bDelta {
  /**
   * L7: non-null when RAG Fusion was requested but failed.
   * TimeoutError → "RAG Fusion timed out"
   * other Exception → "RAG Fusion expansion failed"
   * HyDE failure is assembled at the route level (routes_search.py), not here,
   * because resolve_hyde_vector() runs before pipeline.search() is called.
   */
  rag_fusion_warning: string | null;
}

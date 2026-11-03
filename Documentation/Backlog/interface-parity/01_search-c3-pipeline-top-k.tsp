// C3 — Per-request top_k on the core pipeline (Use Cases <-> core SearchPipeline).
// The only retrieval-behaviour change allowed in this slice (brief Q1). Retrieval, reranking, fusion and graph algorithms are unchanged. (The slice also edits pipeline.py for the ppr guard, the exception message constant and the deleted max_fanout parameter; none changes retrieval.)
namespace PipelineTopKContract;

// Rules the pipeline honours when top_k is present (three private helpers carry them: `_effective_top_k`, `_candidate_pool`, `_leg_trim`):
// - every site that used the configured return count goes through `_effective_top_k(top_k)`, which is `top_k` when given and `top_k_return` otherwise (single path: standard, RAG Fusion, graph local / global, PPR, hybrid fallback and the shortfall warning in the standard path; multi path: standard, hybrid fallback, graph, RAG Fusion and global graph cutoffs); all of them are in search, search_many and their helpers, none in explain;
// - the candidate pool max(top_k_retrieve * 3, 20) (45 at the default) is `_candidate_pool(top_k)`: `max(pool, top_k)` when top_k is given, so a small top_k keeps today's pool. The 8 sites of search and search_many call it; the 5 sites in explain code keep the literal expression and stay unchanged (a test gates them). The multi-path per-leg trim (max(fanout_leg_trim, 1), default 40) is `_leg_trim(top_k)`: `max(trim, top_k)` when top_k is given. Its 4 sites are the three in search_many and the one in the shared helper _fanout_merge_acl (also called by explain), which takes an optional top_k that only search_many passes;
// - one rule for the pool: it is raised to at least top_k, so up to top_k results return, all from the ranked pool, and exactly top_k when enough chunks survive the ACL, glob and wildcard-scope post-filters (which run after the pool is fetched) and no fts_only degrade limits the search; graph_mode "global" has no pool (it ranks the community representatives, at most `[graph].max_global_candidates`), so it can return fewer than top_k;
// - the pool depth feeds each leg's limit and the reranker's candidate set, so the ranking may change when top_k exceeds the pool: prefix stability (results(N)[:M] == results(M)) holds only while N and M fit the same pool (45 on the single path; 40 on the multi path, where the trim becomes max(40, top_k));
// - the reranker already takes a top_k and needs no change, but it scores every candidate, so latency grows with top_k under the fixed 30 s search budget;
// - search_many asserts that graph_mode "ppr" is not combined with any collections (REST rejects even one name), like its scope_filter asserts.
// Callers that must stay byte-identical pass nothing: the eval harness and search_with_context (until roadmap S2 decides otherwise). The service forwards `top_k` as a keyword only when the command has one; the pipeline is never given `top_k=None` by it.
// Added to BOTH pipeline entry points. Omitted (null) = today's behaviour exactly: the result count is `[database].top_k_return`.
model PipelineSearchOptions {
  // When present: the number of results returned, 1 <= top_k. The upper bound (`top_k_max`) is enforced by the service, never by the pipeline.
  top_k?: int32;
}

model PipelineHit {
  doc_id: string;
  chunk_id: string;
  score: float64;
}

// SearchPipelineResult is unchanged by this slice (the contract lists the fields the service reads; the pipeline's own `fanout_timings` is not one of them).
model PipelineResult {
  results: PipelineHit[];
  acl_filtered: boolean;
  excluded_collections: ExcludedCollection[];
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  rag_fusion_warning?: string;
  graph_expansion_applied: boolean;
  ppr_entities_matched?: int32;
}

model ExcludedCollection {
  name: string;
  reason: string;
}

interface SearchPipelineTopK {
  // Single path: per-collection embedder.
  search(options: PipelineSearchOptions): PipelineResult;
  // Multi path: global embedder; model-mismatched collections are excluded.
  searchMany(options: PipelineSearchOptions): PipelineResult;
}

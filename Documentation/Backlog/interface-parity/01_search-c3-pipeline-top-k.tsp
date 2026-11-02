// C3 — Per-request top_k on the core pipeline (Use Cases <-> core SearchPipeline).
// The one core change allowed in this slice (brief Q1). Retrieval, reranking, fusion and graph algorithms are unchanged.
namespace PipelineTopKContract;

// Rules the pipeline honours when top_k is present:
// - every result cutoff on the single path (standard, RAG Fusion, graph local / global, PPR) and on the multi path (standard, graph, local, hybrid fallback) uses top_k instead of top_k_return;
// - the candidate depth is at least what top_k needs ("retrieve at least top_k candidates"); the exact rule above the default depth is Q16 in the plan;
// - the reranker already takes a top_k and needs no change.
// Callers that must stay byte-identical pass nothing: the eval harness and search_with_context (until roadmap S2 decides otherwise).
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

// SearchPipelineResult is unchanged by this slice.
model PipelineResult {
  results: PipelineHit[];
  acl_filtered: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  rag_fusion_warning?: string;
  graph_expansion_applied: boolean;
  ppr_entities_matched?: int32;
}

interface SearchPipelineTopK {
  // Single path: per-collection embedder.
  search(options: PipelineSearchOptions): PipelineResult;
  // Multi path: global embedder; model-mismatched collections are excluded.
  searchMany(options: PipelineSearchOptions): PipelineResult;
}

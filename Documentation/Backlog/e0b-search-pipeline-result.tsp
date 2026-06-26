namespace SearchPipelineResultContract;

/** Internal seam: SearchPipelineResult additions for E0b L7. */
model SearchPipelineResult {
  results: string[];
  acl_filtered: boolean;
  excluded_collections: string[];
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  /** L7: non-null when HyDE or RAG Fusion was requested but failed/timed out. */
  expansion_warning: string | null;
}

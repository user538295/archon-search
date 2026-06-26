import "@typespec/http";
using Http;

@service(#{ title: "Search API — expansion transparency (E0b L7)" })
namespace SearchResponseContract;

/** Stub for TypeSpec compilation only — not the real schema. Full SearchResultSchema in archon_search/server/routes_search.py (12 fields: doc_id, chunk_id, text, score, source_path, etc.). */
model SearchResult {
  id: string;
  score: float32;
  content: string;
}

model SearchRequest {
  query: string;
  collection: string;
  hyde: boolean;
  rag_fusion: boolean;
}

/** SearchResponse shape after E0b L7. Additive — no existing fields removed. */
model SearchResponse {
  results: SearchResult[];
  acl_filtered: boolean;
  embedding_model: string;
  hyde_applied: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  /** True when HyDE or RAG Fusion successfully contributed to the result set (= hyde_applied OR rag_fusion_applied). */
  expansion_used: boolean;
  /**
   * Non-null when expansion was requested but failed. De facto test contracts (do not change wording without updating tests):
   * - HyDE failure (all modes — timeout, API error, missing key, empty response): "HyDE expansion failed"
   * - RAG Fusion timeout (asyncio.TimeoutError): "RAG Fusion timed out"
   * - RAG Fusion other exception: "RAG Fusion expansion failed"
   * HyDE and RAG Fusion are mutually exclusive per route logic; only one message per response.
   */
  expansion_warning: string | null;
}

@route("/search")
interface Search {
  @post search(@body body: SearchRequest): SearchResponse;
}

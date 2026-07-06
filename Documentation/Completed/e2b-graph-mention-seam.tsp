/** Internal logical seam: GraphMention entity + GraphExtractionResult.mentions field.
 * Boundary: GraphExtractor (Interface Adapters) → SearchPipeline (Use Cases) → GraphStore (Frameworks & Drivers).
 * Realised by: BE-1, BE-3, BE-4, BE-5.  Verified by: BE-3 (store integration), BE-4 (extractor unit), BE-5 (pipeline unit).
 */
namespace E2bGraphMentionSeam;

/** Per-chunk entity incidence record.
 * Persisted in _archon_graph_{col}_mentions (entity_id, chunk_id, doc_id).
 * doc_id enables delete-then-add per document on re-ingest.
 */
model GraphMention {
  /** SHA-256 hex matching GraphNode.id. Produced by make_stable_entity_id. */
  entity_id: string;
  /** Chunk identifier ({doc_id}-{idx:06d}). */
  chunk_id: string;
  /** Source document identifier. Used for delete_mentions_by_doc. */
  doc_id: string;
}

/** Extended extraction result — GraphExtractionResult gains this field. */
model ExtractionResultWithMentions {
  nodes: string[];     // GraphNode IDs (opaque; type enforced by graph_types.GraphNode)
  edges: string[];     // GraphEdge IDs
  mentions: GraphMention[];
  llm_fallback_used: boolean;
  warnings: string[];
  fatal_error: string | null;
}

/** GraphStore methods added for the mentions table. */
interface MentionsStore {
  ensure_mentions_table(collection: string): void;
  write_mentions(collection: string, mentions: GraphMention[]): void;
  delete_mentions_by_doc(collection: string, doc_id: string): void;
  get_all_mentions(collection: string, limit?: int32): GraphMention[];
}

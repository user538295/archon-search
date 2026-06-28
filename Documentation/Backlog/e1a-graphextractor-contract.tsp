// Internal logical seam — Pipeline calls GraphExtractor with chunk batch;
// extractor returns entities + edges for writing to GraphStore.
// Validated with: tsp compile e1a-graphextractor-contract.tsp --no-emit
namespace GraphExtractorContract;

enum EntityType { person, concept, system, event, code_symbol }
enum RelationshipType { uses, implements, depends_on, related_to }

model ChunkInput {
  chunkId: string;
  text: string;
  symbolType: string | null;     // C3-enriched code symbol type; drives code_symbol extraction
  symbolSubtype: string | null;
}

model GraphNode {
  id: string;              // SHA-256 hex of "{entity_type.strip().lower()}:{entity_name.strip().lower()}" (byte-encoded via make_stable_entity_id)
  entityName: string;
  entityType: EntityType;
  entitySubtype?: string;
  sourceDocId: string;
  collectionName: string;
}

model GraphEdge {
  id: string;              // SHA-256 hex of "{sourceNodeId}:{targetNodeId}:{relationshipType.strip().lower()}" (byte-encoded via make_stable_edge_id)
  sourceNodeId: string;
  targetNodeId: string;
  relationshipType: RelationshipType;
  sourceDocId: string;
}

model GraphExtractionResult {
  nodes: GraphNode[];
  edges: GraphEdge[];
  llmFallbackUsed: boolean;  // true when LLM call failed; spaCy-only result returned
  warnings: string[];        // forwarded to IngestResult.warnings
}

interface GraphExtractor {
  extract(chunks: ChunkInput[], docId: string, collection: string): GraphExtractionResult;
}

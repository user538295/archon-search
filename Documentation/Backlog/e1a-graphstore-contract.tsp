// Internal logical seam — SearchPipeline reads/writes per-collection graph tables.
// LanceDB tables: _archon_graph_{collection}_nodes, _archon_graph_{collection}_edges
// Validated with: tsp compile e1a-graphstore-contract.tsp --no-emit
namespace GraphStoreContract;

enum GraphBackend { networkx, kuzu }
enum EntityType { person, concept, system, event, code_symbol }
enum RelationshipType { uses, implements, depends_on, related_to }

model GraphNode {
  id: string;              // SHA-256 hex of "{entity_type}:{entity_name.strip().lower()}" (byte-encoded via make_stable_entity_id)
  entityName: string;
  entityType: EntityType;
  entitySubtype?: string;  // optional sub-label for code_symbol entities (from _symbol_subtype)
  sourceDocId: string;     // last-writer-wins on upsert (see Known limitations in plan); not multi-provenance
  collectionName: string;
}

model GraphEdge {
  id: string;              // SHA-256 hex of "{sourceNodeId}:{targetNodeId}:{relationshipType}" (byte-encoded via make_stable_edge_id)
  sourceNodeId: string;
  targetNodeId: string;
  relationshipType: RelationshipType;
  sourceDocId: string;
}

interface GraphStore {
  ensureGraphTables(collection: string): void;
  writeGraph(collection: string, nodes: GraphNode[], edges: GraphEdge[]): void;
  getNeighbours(collection: string, entityIds: string[]): GraphNode[];
  edgeCount(collection: string): int32;
  nodeCount(collection: string): int32;
  findNodesByName(collection: string, names: string[]): GraphNode[];
  getBackend(collection: string): GraphBackend;
  migrateToKuzu(collection: string): void;
}

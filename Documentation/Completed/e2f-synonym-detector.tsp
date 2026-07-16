namespace E2fSynonymDetector;

// GraphNode: minimal projection — full definition in e2f-graph-entity-model.tsp.
// Only the fields GraphStoreProtocol needs to typecheck are listed here;
// name_embedding is included because vector_search_nodes (added by BE-2) returns it.
model GraphNode {
  id: string;
  entity_name: string;
  entity_type: string;
  name_embedding?: float32[] | null;
}

// GraphEdge: the canonical edge type from graph_types.py.
// SynonymDetector returns list[GraphEdge] directly — no separate ProposedSynonymEdge type needed.
// Returned edges have relationship_type="synonym_of" and extraction_method="embedding".
// id is load-bearing: make_stable_edge_id(source_node_id, target_node_id, "synonym_of") —
// used by write_graph() merge_insert("id") to deduplicate on repeated enrichment runs.
model GraphEdge {
  id: string;
  source_node_id: string;
  target_node_id: string;
  // synonym_of | uses | implements | depends_on | related_to — see RelationshipType in e2f-graph-entity-model.tsp
  relationship_type: string;
  source_doc_id: string;
  // "embedding" for ANN-detected pairs; "manual" for alias-file pairs.
  // Nullable so pre-E2f edges coexist without migration.
  extraction_method?: string | null;
}

// An entity-ID pair already covered by a manual alias.
// SynonymDetector excludes all skip_pairs from ANN scoring so each pair
// has exactly one synonym_of edge (extraction_method="manual" wins).
model SkipPair {
  source_node_id: string;
  target_node_id: string;
}

// GraphStoreProtocol: minimum interface SynonymDetector and AliasLoader depend on (C3).
// Concrete GraphStore implements all methods; parameter order matches real GraphStore
// signatures — ns is LAST in all methods per project invariant.
// Note: all methods are async def in the Python implementation — see graph_store.py
interface GraphStoreProtocol {
  // Return all graph nodes for a collection.
  get_all_nodes(collection: string, ns: string): GraphNode[];

  // Added by BE-2 — does not exist in graph_store.py yet.
  // ANN search over name_embedding; entity_type=null searches all types.
  // metric defaults to "cosine" (LanceDB cosine index built by BE-2).
  // Pass entity_type.value (string) — EntityType enum serialized for LanceDB SQL filter
  // SynonymDetector MUST use this ANN method — get_all_nodes() + Python cosine is forbidden (O(n²), Q6 resolution). See Key decision §6 in the team plan.
  vector_search_nodes(collection: string, query_embedding: float32[], entity_type: string | null, limit: int32, metric: string, ns: string): GraphNode[];

  // Upsert nodes and edges into the collection's graph tables.
  write_graph(collection: string, nodes: GraphNode[], edges: GraphEdge[], ns: string): void;

  // Resolve entity names to GraphNode objects (case-insensitive, exact name match).
  // Used by AliasLoader for name→ID resolution.
  find_nodes_by_name(collection: string, names: string[], ns: string): GraphNode[];
}

// The SynonymDetector use case contract:
// - Reads entity nodes with name_embedding from GraphStore via GraphStoreProtocol (C1).
// - Groups nodes by entity_type (same-type restriction).
// - For each node, runs vector_search_nodes with metric="cosine" (index built in BE-2).
// - Excludes pairs in skip_pairs (precomputed by AliasLoader) from ANN results.
// - Returns GraphEdge objects (synonym_of edges ready for write_graph) — no separate ProposedSynonymEdge type needed.
// - Caller writes via graph_store.write_graph(collection, nodes=[], edges=[...], ns=ns).
interface SynonymDetector {
  detect(collection: string, ns: string, skip_pairs: SkipPair[]): GraphEdge[];
}

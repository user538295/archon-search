namespace E2fGraphEntityModel;

// E2f: synonym_of added to RelationshipType; all graph traversal is type-agnostic so
// existing get_neighbours() picks up synonym edges without code change.
union RelationshipType {
  uses: "uses",
  implements: "implements",
  depends_on: "depends_on",
  related_to: "related_to",
  synonym_of: "synonym_of",
}

// E2f: name_embedding added for ANN-based synonym detection.
// Nullable so nodes created before E2f coexist without backfill.
model GraphNode {
  id: string;
  entity_name: string;
  entity_type: string;
  source_doc_id: string;
  collection_name: string;
  entity_subtype?: string | null;
  name_embedding?: float32[] | null;
}

// E2f: extraction_method added for audit trail — "embedding" or "manual".
// Nullable so pre-E2f edges stored without the column coexist.
model GraphEdge {
  id: string;
  source_node_id: string;
  target_node_id: string;
  relationship_type: RelationshipType;
  source_doc_id: string;
  extraction_method?: string | null;
}

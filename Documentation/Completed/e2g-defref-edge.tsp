namespace E2gDefRefEdge;

// E2g: four new members added to RelationshipType (graph_types.py:50-57).
// calls/imports/defines/inherits are the first typed-edge producer for code;
// existing members are carried unchanged — this is a pure additive extension,
// no schema migration (relationship_type is stored as a plain string column).
union RelationshipType {
  uses: "uses",
  implements: "implements",
  depends_on: "depends_on",
  related_to: "related_to",
  synonym_of: "synonym_of",
  calls: "calls",
  imports: "imports",
  defines: "defines",
  inherits: "inherits",
}

// E2g: extraction_method gains two new values on the existing field
// (GraphEdge.extraction_method already exists — graph_types.py:170 —
// currently populated with "embedding"/"manual" by E2f). No new field.
// "extracted" = proven (same-file call, explicit import). "inferred" = best-guess
// cross-file name match. Callers filter to extracted-only via extraction_method_filter.
union ExtractionMethod {
  embedding: "embedding",
  manual: "manual",
  extracted: "extracted",
  inferred: "inferred",
}

model GraphEdge {
  id: string;
  source_node_id: string;
  target_node_id: string;
  relationship_type: RelationshipType;
  source_doc_id: string;
  extraction_method?: ExtractionMethod | null;
}

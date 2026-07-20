// G15 — Permission-Aware Search Snippets: internal logical seams (core-construct
// TypeSpec, no HTTP). These are in-process Python boundaries, not wire contracts.
// Validated with `tsp compile <file> --no-emit`. No OpenAPI is emitted.
//
// They capture WHAT crosses each seam logically after G15:
//   C2 — resolve_acl() return type gains source + sidecar_path.
//   C3 — SearchResult / ChunkRecord / ScoredSearchCandidate gain 3 provenance fields.
//   C4 — LanceDB chunk table gains 3 nullable columns (persistence boundary).

namespace ArchonSearchG15InternalSeams;

// The three-way source provenance discriminant. null == pre-G15 (unknown) OR the
// dataclass default before ingest sets it.
enum AclSource {
  frontmatter,
  sidecar,
  collection_default,
}

// C2 — resolve_acl(doc_path, front_matter_acl) return.
// Today returns (acl_entries, warnings). G15 widens it to also convey source and
// sidecar_path. Recommended shape: a named result (option (b) in findings).
model AclResolutionResult {
  acl_entries: string[] | null; // None=open, []=deny-all, [..]=allow-list
  source: AclSource | null;
  sidecar_path: string | null; // absolute-vs-relative is Q3 (open)
  warnings: string[];
}

interface AclResolver {
  // Single ACL resolution point (invariant #6). front_matter_acl wins over sidecar.
  resolveAcl(docPath: string, frontMatterAcl: unknown): AclResolutionResult;
}

// C3 — provenance fields added identically to the three dataclasses so the
// values flow store -> pipeline -> route without re-reading the store.
// acl_warning is a single nullable string per chunk (Q5: scalar vs list).
model AclProvenanceFields {
  acl_source: string | null;
  acl_sidecar_path: string | null;
  acl_warning: string | null;
}

// ChunkRecord (persistence entity) + G15 provenance.
model ChunkRecord {
  acl: string[] | null;
  ...AclProvenanceFields;
}

// SearchResult (query-side) + G15 provenance.
model SearchResult {
  acl: string[] | null;
  collection: string;
  ...AclProvenanceFields;
}

// ScoredSearchCandidate (explain trace path) + G15 provenance.
model ScoredSearchCandidate {
  acl: string[] | null;
  collection: string;
  ...AclProvenanceFields;
}

// C4 — the LanceDB chunk-table persistence boundary: three new nullable columns.
// Migration is IN_PLACE add_columns, per-collection, no STORE_SCHEMA_VERSION bump
// (Q1 decides lazy-startup vs operator-triggered).
model ChunkTableAclColumns {
  acl_source: string | null;
  acl_sidecar_path: string | null;
  acl_warning: string | null;
}

interface ChunkStore {
  // Read path (hybrid_search / _hybrid_search_with_trace) populates the fields
  // above with `.get(col)` defaulting to null for pre-G15 rows.
  readChunkAclColumns(chunkId: string): ChunkTableAclColumns;
}

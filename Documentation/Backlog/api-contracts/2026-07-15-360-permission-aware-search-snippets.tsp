import "@typespec/http";
using Http;

// G15 — Permission-Aware Search Snippets.
// Two HTTP/API seams modelled here as a POINT-IN-TIME contract for the PLANNED
// change to POST /search and POST /explain. These mirror the target shapes of
// SearchRequest / SearchResultSchema (routes_search.py) and ExplainResult /
// ExplainNearMiss / ExplainResponse (routes_explain.py) AFTER G15 lands.
//
// Auth: Bearer token on every request; namespace derived server-side from the
// token (ADR-09), never sent by the caller. ACL filtering already excludes
// chunks the caller cannot see BEFORE acl_gate is ever constructed — acl_gate is
// read-only audit metadata, not an access-control surface.
//
// TypeSpec cannot $ref a Python model, so these models are a deliberate,
// acknowledged snapshot of the target Pydantic schemas — regenerate manually if
// the schemas change. Only G15-relevant fields are modelled; pre-existing
// unrelated fields (hyde, rag_fusion, breakdown, routing, ...) are elided.
@service(#{ title: "Archon Search Permission-Aware Snippets API" })
namespace ArchonSearchAclGateApi;

// C1/C6 — the acl_gate audit object attached to each result.
//   allowed_principals: full allow-list of namespace names; null == open to all.
//   source: three-way provenance; null == pre-G15 chunk (provenance unknown).
//   sidecar_path: path to the .acl sidecar when source == "sidecar"; else null.
//   warnings: problems recorded at index time (e.g. sidecar too large -> fail-open).
model AclGate {
  allowed_principals: string[] | null;
  source: "frontmatter" | "sidecar" | "collection_default" | null;
  sidecar_path?: string | null;
  warnings: string[] = #[];
}

// Mirrors SearchRequest (routes_search.py) — only the G15 addition is shown in
// full; other fields elided. acl_context defaults false (backward-compatible).
model SearchRequest {
  query: string;
  collection?: string | null;
  collections?: string[] | null;
  top_k?: int32;
  acl_context: boolean = false;
}

// Mirrors SearchResultSchema (routes_search.py). acl (raw list) is UNCHANGED and
// still present; acl_gate is the G15 addition, null unless acl_context == true.
model SearchResultSchema {
  doc_id: string;
  chunk_id: string;
  text: string;
  score: float32;
  source_path: string;
  acl: string[] | null;
  collection: string;
  acl_gate?: AclGate | null;
}

model SearchResponse {
  results: SearchResultSchema[];
  acl_filtered: boolean;
}

// Mirrors ExplainResult (routes_explain.py). On /explain, acl_gate is ALWAYS
// present (no flag) — brief mandate. Schema uses extra="forbid" server-side, so
// the field is an explicit addition.
model ExplainResult {
  doc_id: string;
  chunk_id: string;
  text: string;
  score: float32;
  source_path: string;
  acl: string[] | null;
  collection: string;
  acl_gate: AclGate;
}

// Mirrors ExplainNearMiss (routes_explain.py) — omits text. Per Q6 (resolved),
// ExplainNearMiss does NOT carry acl_gate — near-misses are ranking rejects, not ACL rejects.
model ExplainNearMiss {
  doc_id: string;
  chunk_id: string;
  score: float32;
  source_path: string;
  acl: string[] | null;
  collection: string;
}

model ExplainRequest {
  query: string;
  collection?: string | null;
  collections?: string[] | null;
  top_k?: int32;
}

model ExplainResponse {
  results: ExplainResult[];
  near_misses: ExplainNearMiss[];
  acl_filtered: boolean;
}

model ErrorDetail {
  detail: string;
}

model StoreBusyError {
  error: string;
}

@route("/search")
interface Search {
  // Hybrid search. With acl_context=true, every result carries acl_gate.
  //   200 — results (SearchResponse).
  //   401 — missing/invalid Bearer token.
  //   404 — collection not found / not visible to namespace.
  //   422 — invalid request body.
  //   503 — store busy (Retry-After header).
  @post
  search(@body body: SearchRequest): {
    @statusCode statusCode: 200;
    @body body: SearchResponse;
  } | {
    @statusCode statusCode: 401 | 404 | 422;
    @body error: ErrorDetail;
  } | {
    @statusCode statusCode: 503;
    @header("Retry-After") retryAfter: int32;
    @body error: StoreBusyError;
  };
}

@route("/explain")
interface Explain {
  // Debug endpoint. acl_gate is ALWAYS included on ExplainResult; ExplainNearMiss does NOT carry acl_gate (Q6).
  // no acl_context flag on the request.
  //   200 — explanation (ExplainResponse).
  //   401 — missing/invalid Bearer token.
  //   404 — collection not found / not visible.
  //   422 — invalid request body.
  //   503 — store busy (Retry-After header).
  @post
  explain(@body body: ExplainRequest): {
    @statusCode statusCode: 200;
    @body body: ExplainResponse;
  } | {
    @statusCode statusCode: 401 | 404 | 422;
    @body error: ErrorDetail;
  } | {
    @statusCode statusCode: 503;
    @header("Retry-After") retryAfter: int32;
    @body error: StoreBusyError;
  };
}

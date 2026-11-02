import "@typespec/http";

using Http;

// C5 — REST POST /search (client <-> server). The seam the CLI also uses.
// Part of BOTH OpenAPI snapshots (tests/contract/openapi_snapshot.json and tests/server/openapi_snapshot.json), which must be regenerated together.
// Delta of this slice: `top_k` becomes real (semantics below), error responses are produced from one ServiceError translation with the existing bodies preserved.
@service(#{ title: "Archon Search REST search (S1)" })
namespace SearchRestApi;

enum GraphMode {
  naive,
  local,
  global,
  ppr,
}

model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  include_metadata?: boolean = false;
}

model SearchRequest {
  // Exactly one of collection / collections is required.
  collection?: string;
  collections?: string[];
  query: string;

  // Per-request result count. Real from this slice on: validated (>= 1 here, <= the operator's top_k_max in the service) and forwarded to the pipeline.
  // The default when omitted is open (Q16 in the plan); today's schema default is 5.
  @minValue(1)
  top_k?: int32;

  filters?: SearchFilters;
  hyde?: boolean = false;
  rag_fusion?: boolean = false;
  graph_mode?: GraphMode;
  scope_filter?: string;
  acl_context?: boolean = false;
  // OR-ed with filters.include_metadata.
  include_metadata?: boolean = false;
}

alias AclSource = "frontmatter" | "sidecar" | "collection_default";

model AclGate {
  allowed_principals: string[] | null;
  source: AclSource | null;
  sidecar_path: string | null;
  warnings: string[];
}

model SearchResultItem {
  doc_id: string;
  chunk_id: string;
  text: string;
  score: float64;
  source_path: string;
  file_type?: string = "";
  language?: string = "";
  indexed_at?: string = "";
  updated_at?: string = "";
  ingested_by?: string = "cli";
  metadata?: Record<string>;
  acl?: string[] | null;
  collection?: string = "";
  reranker_score?: float64 | null;
  // Present on every result; non-null only when the request set acl_context.
  acl_gate?: AclGate | null;
}

model ExcludedCollection {
  name: string;
  reason: string;
}

model SearchResponse {
  results: SearchResultItem[];
  acl_filtered: boolean;
  excluded_collections?: ExcludedCollection[];
  embedding_model?: string = "";
  hyde_applied?: boolean = false;
  rag_fusion_applied?: boolean = false;
  rag_fusion_queries_used?: int32 = 0;
  rag_fusion_attempted?: boolean = false;
  graph_expansion_applied?: boolean = false;
  expansion_used?: boolean = false;
  expansion_warning?: string | null;
  applied_filters?: SearchFilters | null;
  ppr_entities_matched?: int32 | null;
}

// {"detail": "<fixed text>"} with an optional machine code.
model ErrorDetail {
  detail: string;
  code?: string | null;
}

model CodedErrorMessage {
  code: string;
  message: string;
}

// {"detail": {"code": "...", "message": "..."}}
model CodedErrorDetail {
  detail: CodedErrorMessage;
}

model ValidationItem {
  loc: (string | int32)[];
  msg: string;
  type: string;
}

// The framework's request-validation body (list-shaped detail).
model HttpValidationList {
  detail: ValidationItem[];
}

model Ok {
  @statusCode _: 200;
  @body body: SearchResponse;
}

@error
model ScopeFilterInvalid {
  @statusCode _: 400;
  @body body: CodedErrorDetail;
}

@error
model CollectionNotFound {
  @statusCode _: 404;
  @body body: ErrorDetail;
}

// Three body shapes exist today and are preserved: the framework list, a plain string detail, and the coded object (graph communities not built).
@error
model Unprocessable {
  @statusCode _: 422;
  @body body: HttpValidationList | ErrorDetail | CodedErrorDetail;
}

// A HyDE failure answers {"detail": "search failed"}; a pipeline failure answers a plain-text 500. Whether the two merge is Q15 in the plan.
@error
model SearchFailed {
  @statusCode _: 500;
  @body body: ErrorDetail;
}

// code metadata_store_error (no Retry-After) or embedder_not_ready (Retry-After from the one constant, 30 seconds).
@error
model ServiceUnavailable {
  @statusCode _: 503;
  @header("Retry-After") retryAfter?: int32;
  @body body: ErrorDetail;
}

@error
model SearchTimedOut {
  @statusCode _: 504;
  @body body: ErrorDetail;
}

@route("/search")
interface Search {
  @post search(@body body: SearchRequest): Ok | ScopeFilterInvalid | CollectionNotFound | Unprocessable | SearchFailed | ServiceUnavailable | SearchTimedOut;
}

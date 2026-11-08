import "@typespec/http";
import "@typespec/openapi";

using Http;
using OpenAPI;

// C5 — REST POST /search-with-context (client <-> server). The seam the CLI `search-with-context` command uses.
// New route (roadmap D2). Adds a path to BOTH OpenAPI snapshots (tests/contract/openapi_snapshot.json and tests/server/openapi_snapshot.json), which must be regenerated together (the root openapi.json is gitignored and not a deliverable). The shared hit and neighbour schemas live in server/schemas.py, so the route imports nothing from routes_search.py (boundary rule 2); the route keeps its own error table, as the OpenAI shim does.
// Error mapping is the one table of POST /search (01_search-c5-rest-search.tsp), produced from one ServiceError translation. The graph kinds of that table are unreachable here (no graph_mode).
// `CancelOnDisconnectMiddleware` (server/app.py line 1307, exact-path match) must list `/search-with-context`, as ADR 19 requires for every search route.
// DECIDED by the owner (plan Q18, option A): the request model forbids unknown top-level keys (pydantic `extra="forbid"`, emitted as `additionalProperties: false`), so a stale `graph_mode` or `collections` key, like any other unknown top-level key, answers the framework 422 list (`Extra inputs are not permitted`) and nothing is searched. This matches the MCP tool, which refuses a stale `graph_mode` loudly. The rejected alternative was no `extra` option, as `SearchRequest` of POST /search has (routes_search.py line 58): the key would be silently ignored.
// The repo has both conventions: the models of POST /search and server/schemas.py set no `extra` option (an unknown top-level key is ignored), while the /explain models (routes_explain.py lines 6-8), `ExportRequest` (routes_export.py line 979) and the nested `SearchFilters` (filters.py line 19) forbid unknown keys, so a stray key INSIDE `filters` is already a 422 on POST /search today.
// Like `SearchRequest`, the model carries field validators that run the normaliser for a blank `query` or `collection`, so those answer the framework's list (`Value error, ...`); only the top_k maximum and the context window answer the service's plain string detail.
@service(#{ title: "Archon Search REST search with context (S2)" })
namespace SearchWithContextRestApi;

model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  include_metadata?: boolean = false;
}

@extension("additionalProperties", false)
model SearchWithContextRequest {
  // Required, exactly one. There is no `collections` and no `graph_mode` on this route: sending either is a 422 (Q18).
  collection: string;
  query: string;

  // Neighbours per side. Default 1; 0 returns hits only (empty context_before / context_after). No `minimum` or `maximum` in the schema: the whole range [0, the operator's [search].context_window_max] is enforced by the service, so a value below 0 or above the cap answers `422 {"detail": "context_window must be in [0, {max}]; got {n}"}` naming the bound, not the framework's list (a schema minimum would route a negative value to the framework 422 and never reach the service).
  context_window?: int32 = 1;

  // Omitted (or null) means the configured `top_k_return`; no schema default. Bounded by the operator's top_k_max in the service.
  @minValue(1)
  top_k?: int32 | null;

  filters?: SearchFilters;
  hyde?: boolean = false;
  rag_fusion?: boolean = false;
  scope_filter?: string;
  // When true each HIT carries `acl_gate`; neighbours never do.
  acl_context?: boolean = false;
  // OR-ed with filters.include_metadata; blanks `metadata` on hits AND neighbours when false.
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
  // Present on every hit; non-null only when the request set acl_context.
  acl_gate?: AclGate | null;
}

// A neighbour chunk. No score, collection, reranker_score or acl_gate; `acl` is the stored allowed-principals list. The same type as the MCP neighbour schema (promoted to server/schemas.py, `ContextChunkSchema` of mcp_schemas.py line 144), so both wire surfaces expose the same fields; `indexed_at` is required there, so it is required here.
model ContextChunk {
  doc_id: string;
  chunk_id: string;
  text: string;
  source_path: string;
  indexed_at: string;
  file_type?: string = "";
  language?: string = "";
  updated_at?: string = "";
  ingested_by?: string = "cli";
  metadata?: Record<string>;
  acl?: string[] | null;
}

// One hit and its neighbours, nested as the MCP item is today (`result` plus the two lists); the CLI `--json` prints this body.
model SearchWithContextItem {
  result: SearchResultItem;
  // Same document, chunk index below the hit's, ascending. Empty for context_window 0, a hit whose chunk_id has no numeric suffix, or when all neighbours were denied by ACL (dropped silently).
  context_before: ContextChunk[];
  // Chunk index above the hit's, ascending.
  context_after: ContextChunk[];
}

// The response fields of `search` the brief lists. The multi-collection and graph fields (excluded_collections, graph_expansion_applied, ppr_entities_matched) are absent: single collection, no graph_mode, so they would be constant empty values. expansion_used = hyde_applied OR rag_fusion_applied.
model SearchWithContextResponse {
  results: SearchWithContextItem[];
  acl_filtered: boolean;
  embedding_model?: string = "";
  hyde_applied?: boolean = false;
  rag_fusion_applied?: boolean = false;
  rag_fusion_queries_used?: int32 = 0;
  rag_fusion_attempted?: boolean = false;
  expansion_used?: boolean = false;
  expansion_warning?: string | null;
  // `null` when no filter was given (present with a null value), else the filters as parsed.
  applied_filters?: SearchFilters | null;
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
  @body body: SearchWithContextResponse;
}

@error
model ScopeFilterInvalid {
  @statusCode _: 400;
  @body body: CodedErrorDetail;
}

// Unknown or invalid collection name, or one in another namespace: the fixed text `collection not found`.
@error
model CollectionNotFound {
  @statusCode _: 404;
  @body body: ErrorDetail;
}

// The framework list: schema-level failures (a missing collection, a wrong type, `top_k` below 1) and a blank `query` or `collection`, which the request model's field validators answer as `Value error, query must not be empty` / `Value error, collection must not be empty` (as POST /search does, pinned in tests/server/test_routes_search_service_adapter.py lines 293-294). Or a plain string detail from the service (top_k above the maximum, context_window outside [0, the cap]).
@error
model Unprocessable {
  @statusCode _: 422;
  @body body: HttpValidationList | ErrorDetail;
}

// A HyDE failure answers {"detail": "search failed"}; a pipeline failure answers a plain-text 500; the two shapes stay distinct as in POST /search.
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

// The 30 s budget around the search and the neighbour fetch was exceeded; nothing partial is returned.
@error
model SearchTimedOut {
  @statusCode _: 504;
  @body body: ErrorDetail;
}

@route("/search-with-context")
interface SearchWithContext {
  @post searchWithContext(@body body: SearchWithContextRequest): Ok | ScopeFilterInvalid | CollectionNotFound | Unprocessable | SearchFailed | ServiceUnavailable | SearchTimedOut;
}

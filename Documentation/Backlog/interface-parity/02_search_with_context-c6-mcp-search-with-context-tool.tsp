// C6 — MCP `search_with_context` tool (client <-> server over MCP streamable HTTP at /mcp).
// A client-server seam, but JSON-RPC tool calls, not REST routes, so it is a core-construct contract with no OpenAPI (precedent: 01_search-c6-mcp-search-tool.tsp). The tool becomes a thin adapter: resolve the caller with `_get_request_identity()` (refuse before building the service, ADR 17), build the command, call the service, render.
namespace McpSearchWithContextToolContract;

// Flat filter parameters stay flat on MCP (REST nests them under `filters`). The tool's own `collection` rule is the one `search` uses: the argument is optional in the signature so a missing one reaches the service and answers the in-band `validation_error`; a signature-required argument would answer FastMCP's own argument error instead.
model McpSearchWithContextInput {
  query: string;
  // REQUIRED by the rule (today optional with a configured default): the tool description says so, and a missing or blank value is the service's validation error `collection must not be empty`, not a fallback. Optional in the signature (`str | None = None`, as `search`), so FastMCP does not answer first.
  collection?: string;
  // Omitted = 1 (DEFAULT_CONTEXT_WINDOW). The description names the operator's cap rather than a constant.
  context_window?: int32;
  // NEW. Same bounds as REST; omitted = the configured `top_k_return`.
  top_k?: int32;
  // The one MCP flag; fills the command's top-level include_metadata.
  include_metadata: boolean;
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  hyde: boolean;
  rag_fusion: boolean;
  scope_filter?: string;
  // NEW. When true each HIT carries `acl_gate`; neighbours do not.
  acl_context: boolean;
  // REMOVED: `graph_mode`. The tool description points agents to `search` for graph retrieval. A client that still sends it, with or without a value (even null), gets FastMCP's own argument error (`Unexpected keyword argument`, verified against the installed fastmcp: arguments are validated against the tool signature, which has no **kwargs); the call is not served. The migration note says so.
}

model McpAclGate {
  allowed_principals?: string[];
  source?: string;
  sidecar_path?: string;
  warnings: string[];
}

// Hit wire schema: the shared `search` result schema with `acl_gate` (today `McpSearchResultSchema`; it becomes `McpSearchToolResultSchema`).
model McpHit {
  doc_id: string;
  chunk_id: string;
  text: string;
  score: float64;
  source_path: string;
  file_type: string;
  language: string;
  indexed_at: string;
  updated_at: string;
  ingested_by: string;
  metadata: Record<string>;
  acl?: string[];
  collection: string;
  reranker_score?: float64;
  acl_gate?: McpAclGate;
}

// Neighbour wire schema: today's `ContextChunkSchema` (no vector, offsets, score or collection; `acl` list only), promoted to server/schemas.py so REST uses the same type (both wire surfaces expose `acl` only).
model McpContextChunk {
  doc_id: string;
  chunk_id: string;
  text: string;
  source_path: string;
  indexed_at: string;
  file_type: string;
  language: string;
  metadata: Record<string>;
  ingested_by: string;
  updated_at: string;
  acl?: string[];
}

// Item shape: today's nested `{result, context_before, context_after}`, kept (the brief lists no reshape among the MCP changes; "each result carries `context_before` and `context_after`" is read as this item; UserManual/60_searching.md line 107 documents it). REST and the CLI `--json` use the same nesting.
model McpContextItem {
  result: McpHit;
  context_before: McpContextChunk[];
  context_after: McpContextChunk[];
}

// The echo of the parsed filters, the typed `SearchFilters` that `McpSearchResponse` uses (mcp_schemas.py line 122), with the boolean `include_metadata`; the same type as C5 (not a string map).
model SearchFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  include_metadata?: boolean;
}

// Response gains the derived fields of `search` that the brief lists (today only hyde_applied, expansion_used, expansion_warning); `excluded_collections`, `graph_expansion_applied` and `ppr_entities_matched` stay absent (single collection, no graph_mode); expansion_used = hyde_applied OR rag_fusion_applied. `extra="forbid"` stays.
model McpSearchWithContextResponse {
  results: McpContextItem[];
  acl_filtered: boolean;
  embedding_model: string;
  hyde_applied: boolean;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  expansion_used: boolean;
  expansion_warning?: string | null;
  applied_filters?: SearchFilters | null;
}

// Error mapping through the MCP table, `McpErrorResponse(error, code)`, no per-kind branching in the tool body: validation_error (blank query or collection, window or top_k out of range, invalid filters), invalid_scope_filter, not_found (text `collection not found`, also for an invalid name), metadata_store_error (today internal_error), embedder_not_ready (kept in the table, practically unreachable: the 0.5 s bound pre-empts the cache's 120 s wait, so it answers only if a cache raises at once), timeout (NEW, `search timed out`), internal_error (`search failed`).
// Without config (the MCP factory built without one) the tool answers the fixed internal error `search failed` / `internal_error` and logs the error, exactly as `search` does (mcp.py lines 591-597; decided in S1, 01_search-team-plan.md line 788); the two tools share the one lazily built service.
interface McpSearchWithContextTool {
  search_with_context(input: McpSearchWithContextInput): McpSearchWithContextResponse;
}

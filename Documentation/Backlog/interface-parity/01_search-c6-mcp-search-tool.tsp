// C6 — MCP `search` tool (client <-> server over MCP streamable HTTP at /mcp).
// A client-server seam, but JSON-RPC tool calls, not REST routes, so it is a core-construct contract with no OpenAPI (repo precedent: the e2g and archon-mcp-factory contracts).
namespace McpSearchToolContract;

// Flat filter parameters stay flat on MCP (REST nests the same fields under `filters`); both build the same SearchFilters in the service.
model McpSearchInput {
  query: string;
  // Exactly one of collection / collections. No configured default and no literal "default" any more (brief Q2).
  collection?: string;
  collections?: string[];
  // NEW. Same bounds as REST; omitted = server default (semantics: Q16 in the plan).
  top_k?: int32;
  include_metadata: boolean;
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  hyde: boolean;
  rag_fusion: boolean;
  // naive | local | global | ppr, validated by the service (MCP schema keeps it a plain string).
  graph_mode?: string;
  scope_filter?: string;
  // NEW. Same meaning as REST: when true each result carries `acl_gate`.
  acl_context: boolean;
}

model McpAclGate {
  allowed_principals?: string[];
  source?: string;
  sidecar_path?: string;
  warnings: string[];
}

model McpExcludedCollection {
  name: string;
  reason: string;
}

model McpSearchResult {
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
  // NEW. Present (null) on every result like REST; the per-result placement and the effect on search_with_context are Q4 in the plan.
  acl_gate?: McpAclGate;
}

model McpAppliedFilters {
  file_type?: string;
  source_path_prefix?: string;
  source_path_glob?: string;
  indexed_after?: string;
  indexed_before?: string;
  language?: string;
  include_metadata: boolean;
}

// Strict schema (extra fields are forbidden today): the response gains exactly the NEW fields below and must equal REST's response field set.
model McpSearchResponse {
  results: McpSearchResult[];
  acl_filtered: boolean;
  excluded_collections: McpExcludedCollection[];
  hyde_applied: boolean;
  expansion_used: boolean;
  expansion_warning?: string;
  graph_expansion_applied: boolean;
  ppr_entities_matched?: int32;
  // NEW from here on.
  embedding_model: string;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
  rag_fusion_attempted: boolean;
  applied_filters?: McpAppliedFilters;
}

enum McpSearchErrorCode {
  validation_error,
  invalid_graph_mode,
  graph_disabled,
  scope_filter_graph_mode_incompatible,
  invalid_scope_filter,
  graph_communities_not_built,
  not_found,
  metadata_store_error,
  embedder_not_ready,
  timeout,
  internal_error,
  // Adapter-level: the built response failed its own schema; unchanged by this slice.
  schema_validation_error,
}

// Returned as the tool result, not as an MCP protocol error.
model McpSearchError {
  error: string;
  code: McpSearchErrorCode;
}

// A call with no authenticated identity is refused with a fixed ToolError before any validation and before the service is called.
interface McpSearchTool {
  search(input: McpSearchInput): McpSearchResponse | McpSearchError;
}

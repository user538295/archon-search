namespace E2gGraphImpactMcp;

// E2g: new MCP tool graph_impact, in-process/stdio — not REST (see
// api-contracts/e2g-graph-impact-api.tsp for the HTTP/API twin). The response
// shape is a direct structural mirror of C4's ImpactResult
// (e2g-graph-traversal.tsp) — callers/callees are grouped direct vs. indirect
// with per-group truncation, so BE-9 does zero lossy transformation between
// the traversal layer and this Presentation surface (Critical #1). What
// actually transfers from the `get_graph` tool shape (mcp.py:1838) is only the
// outer `McpErrorResponse | None` guard pattern (mcp.py:130-138) — the
// BFS/depth/grouping logic itself is novel, invented fresh in BE-8 (Major #22).
model GraphImpactParams {
  collection: string;
  symbol: string;
  // Disambiguates which node `symbol` resolves to when more than one
  // same-named code_symbol node exists (Critical #2). Omitted: resolves to
  // the highest-PageRank match among same-named nodes.
  file_path?: string;
  // Optional at this Presentation surface; BE-9 fills in the default
  // (DEFAULT_IMPACT_DEPTH = 2, see e2g-graph-traversal.tsp) before calling
  // `compute_impact`, which declares `depth` REQUIRED. Values above
  // MAX_IMPACT_DEPTH = 5 are clamped to the cap by the Adapter, not rejected here.
  depth?: int32;
  // Optional; defaults to "both" (callers + callees) when omitted — the same
  // default BE-9 fills in before calling `compute_impact`.
  direction?: string;
  extraction_method_filter?: string;
}

model ImpactEdge {
  entity_id: string;
  entity_name: string;
  relationship_type: string;
  extraction_method: string;
  depth: int32;
}

// Mirrors C4's ImpactGroup exactly (e2g-graph-traversal.tsp) — direct/indirect
// split, each group independently truncatable with its own omitted_count.
model ImpactGroup {
  direct: ImpactEdge[];
  indirect: ImpactEdge[];
  truncated: boolean;
  omitted_count: int32;
}

model GraphImpactResult {
  symbol: string;
  callers: ImpactGroup;
  callees: ImpactGroup;
  depth_used: int32;
}

// Existing McpErrorResponse is a TypedDict (mcp.py:117-119), not Pydantic —
// shape carried here unchanged: error + code.
model McpErrorResponse {
  error: string;
  code: string;
}

interface GraphImpactTool {
  graph_impact(params: GraphImpactParams): GraphImpactResult | McpErrorResponse;
}

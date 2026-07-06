// HTTP/API seam — POST /search delta: adds graph_mode parameter and
// graph_expansion_applied transparency field to SearchResponse.
// NOTE: OpenAPI emission skipped (npm install blocked in this environment).
// The live OpenAPI at GET /openapi.json is the canonical HTTP contract.
// Authoritative Python types: archon_search/server/routes_search.py
// Validated with: tsp compile e1a-search-graphmode-contract.tsp --no-emit
namespace SearchGraphModeContract;

enum GraphMode { naive }

// Delta to SearchRequest — new optional field:
model SearchRequestGraphDelta {
  graphMode: GraphMode | null;    // null = no graph expansion (default)
}

// Delta to SearchResponse — new transparency field (mirrors hyde_applied pattern):
model SearchResponseGraphDelta {
  graphExpansionApplied: boolean; // true when naive expansion ran and added neighbours
}

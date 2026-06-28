// HTTP/API seam — GET /status delta: adds graph sub-object to StatusResponse.
// NOTE: OpenAPI emission skipped (npm install blocked in this environment).
// The live OpenAPI at GET /openapi.json is the canonical HTTP contract.
// Authoritative Python types: archon_search/server/schemas.py
// Validated with: tsp compile e1a-status-graph-contract.tsp --no-emit
namespace StatusGraphContract;

enum GraphBackend { networkx, kuzu }

model GraphCollectionStats {
  collection: string;
  backend: GraphBackend;
  nodeCount: int32;
  edgeCount: int32;
  migrationHint: boolean;   // true when edgeCount >= backendThresholdEdges
}

model GraphStatusDetail {
  enabled: boolean;
  backendThresholdEdges: int32;
  collections: GraphCollectionStats[];  // one entry per accessible collection
}

// StatusResponse gains: graph: GraphStatusDetail | null
// null when config.graph.enabled = false (mirrors mcp / backup / maintenance pattern)

// C3 — Search Validation internal seam (config ↔ route handlers)
// Route handler bodies (not a separate validator class) read max_fanout and top_k_max
// directly from SearchConfig (request.app.state.config) and raise HTTPException(422)
// when exceeded. This contract models only the data shape, not an abstraction layer.
// Consumers: routes_search.py, routes_explain.py, mcp.py tool closures.
namespace SearchValidationContract;

// The two config fields that gate fanout and top_k validation.
// Sourced from SearchConfig.max_fanout and SearchConfig.top_k_max.
// Both default to 8 and 100 respectively when not configured in TOML.
model SearchLimits {
  max_fanout: int32;
  top_k_max: int32;
}

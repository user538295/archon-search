// C4 — Namespace propagation contract
// Logical invariant: given a valid bearer token scoped to a namespace,
// every MCP tool closure MUST use that namespace when calling pipeline methods.
// Mechanism TBD from ADR spike (K-1); may be FastMCP lifespan hook, custom middleware,
// or closure-captured state. This contract only defines the logical guarantee.

model ResolvedNsCtx {
  nsName: string;  // resolved by APIKeyMiddleware; never DEFAULT_NAMESPACE for cross-namespace tokens
}

interface ToolNsAccessor {
  getResolvedNs(): ResolvedNsCtx;
}

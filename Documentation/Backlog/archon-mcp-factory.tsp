// C2 — MCP HTTP app factory contract
// Captures the parameters added / corrected by this feature.
// create_mcp_http_app() already accepts writer, config, key_store, job_store, pipeline.
// This contract documents the previously-missing or broken wiring the feature fixes.

model McpFactoryWiringContract {
  namespacesMap: Record<string>;  // was hardcoded {}; must now be config.namespaces
  keyStoreProvided: boolean;      // true → 17 tools registered; false → 13 (no key mgmt tools)
  writerProvided: boolean;        // true → telemetry enqueued in tool closures; false → no-op
}

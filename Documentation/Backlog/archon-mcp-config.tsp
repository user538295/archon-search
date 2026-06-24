// C1 — McpConfig contract
// Shape of the [mcp] TOML section; agreed between config layer and run_server().
// Mechanism: new McpConfig dataclass in archon_search/config.py, field on SearchConfig.

model McpConfig {
  enabled: boolean;   // default true; false gates the MCP server entirely
}

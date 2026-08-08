## Bug: MCP ingest_file → MCP search → result contains the ingested document

**ID**: S296-documented_behavior
**Scenario**: S296
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
Failed: S296: documentation gap — MCP ingest_file → search flow is untestable. Docs checked: UserManual/60_searching.md (MCP tools), README.md (line 216), UserManual/50_ingestion_and_collections.md, UserManual/130_ttl_and_scoping.md, UserManual/150_multi_instance_setup.md. Missing: stdlib-usable MCP wire protocol (referenced DeveloperGuide/05_mcp_integration.md and Architecture/600_api_reference_or_public_interface.md do not exist), ingest_file input parameter names, and ingest_file output schema.

### What should happen
- Documentation insufficient to specify expected behavior — test errors at setup (owner to handle).

Rationale (gaps found after searching all embedded docs under `./docs/`):
- **Wire protocol not usable black-box.** `60_searching.md:109` and `150_multi_instance_setup.md:474-500` describe `/mcp` as MCP Streamable HTTP transport and give a client example only via the `mcp` Python SDK (`mcp.client.streamable_http`), which is not an available dependency (stdlib + pytest only). The docs defer the JSON-RPC handshake/framing to `../DeveloperGuide/05_mcp_integration.md` and `../Architecture/600_api_reference_or_public_interface.md` — **neither file exists** in the embedded docs (only `UserManual/` and `OperatorGuide/` are present).
- **`ingest_file` input schema undocumented.** `README.md:216` says only "index a single file into a collection"; `130_ttl_and_scoping.md:184` lists optional `chunk_ttl_seconds`/`chunk_scopes`. The tool's required parameter names (the file argument and the collection argument) are never stated — they must not be invented.
- **`ingest_file` output schema undocumented.** No doc states what `ingest_file` returns, so "result contains the ingested document" has no documented field to assert against.

### Steps to reproduce
1. Call the MCP `ingest_file` tool over `POST /mcp` to index a single file into a collection.
2. Call the MCP `search` tool over `POST /mcp` with a query matching the ingested content.
3. Inspect the `search` tool's `results` for the ingested document.

### Evidence
```
E   Failed: S296: documentation gap — MCP ingest_file → search flow is untestable. Docs checked: UserManual/60_searching.md (MCP tools), README.md (line 216), UserManual/50_ingestion_and_collections.md, UserManual/130_ttl_and_scoping.md, UserManual/150_multi_instance_setup.md. Missing: stdlib-usable MCP wire protocol (referenced DeveloperGuide/05_mcp_integration.md and Architecture/600_api_reference_or_public_interface.md do not exist), ingest_file input parameter names, and ingest_file output schema.
```

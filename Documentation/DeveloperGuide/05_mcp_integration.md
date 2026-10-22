**Purpose**: Document how to consume `archon-search` as an MCP server, including the tool names, their argument shapes, and how to register the server with Claude Code and similar clients.
**Audience**: Engineers wiring `archon-search` into MCP-aware tools (Claude Code, Cline, custom agents using `@modelcontextprotocol` SDKs).
**Status**: Draft
**Last reviewed**: 2026-10-17 / **Next review**: 2027-10-12

# MCP Integration

`archon-search` defines an MCP server that mirrors the REST pipeline. The MCP transport is built by `create_mcp_http_app` in `archon_search/server/mcp.py`: it builds a Starlette app with the `FastMCP` app's `http_app(path="/")` and adds the same `APIKeyMiddleware` used for REST. That sub-app serves the streamable HTTP endpoint at its root, so the mount exposes it at `/mcp`. `app.py` passes `api_key_state=app.state`, which `create_mcp_http_app` hands to that middleware and to `mcp.create_app`: `/mcp` authenticates against the REST app's current default key (`app.state.api_key`), and `rotate_key` retires that key and publishes the new one there, so a rotation through either transport takes effect on both at once. Without `api_key_state`, `create_mcp_http_app` seeds a private state from `load_or_generate_key()`, and a bare `mcp.create_app`'s `rotate_key` re-reads `.search.env` and publishes nothing.

The MCP app is mounted at `/mcp` by the app lifespan in `server/app.py` when `[mcp].enabled = true` (the default). `app.state.mcp_bound` tracks whether the mount succeeded.

## Principles

1. **Same auth as REST.** Every MCP request carries `Authorization: Bearer <token>`. The `/health` route is registered on the FastMCP app inside `create_app` (via `@app.custom_route`); the exemption from `APIKeyMiddleware` is shared with REST and defined by `_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/ready"})` in `archon_search/server/middleware_auth.py`.
2. **Twenty tools, named exactly as in `mcp.py`.** Sixteen always register; the four key-management tools (`create_key`, `list_keys`, `revoke_key`, `rotate_key`) register only when `mcp.create_app` receives a `key_store`. The shipped server always passes one (`server/app.py`), so it serves all twenty; only an embedded `mcp.create_app` called without a key store serves sixteen. Adding or renaming a tool requires a `BREAKING.md` entry per project policy (see `CLAUDE.md` and `Documentation/Architecture/520_api_design_and_contracts.md`). Tool names are not symmetric with REST routes.
3. **Responses are Pydantic-validated (C7).** The 11 search, ingest, collection, and document tools validate return values through an explicit Pydantic schema (`extra='forbid'`) before serializing: `explain` through `ExplainResponse` (`archon_search/server/routes_explain.py`, shared with REST `POST /explain`), and the other ten (`search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`, `update_collection`) through schemas defined in `archon_search/server/mcp_schemas.py`. The export, import, graph, and key-management tools return plain dicts (`export_collection` and `import_collection` return `job_to_dict(job)`). Schema drift surfaces as `{"error": "response schema validation failed; see the server log for details", "code": "schema_validation_error"}` (`search` / `search_with_context`: `search response failed schema validation`; `explain`: `explain response failed schema validation`), and an unexpected failure in most tools as `{"error": "internal error; see the server log for details", "code": "internal_error"}` (`search` / `search_with_context`: `search failed`; `explain`: `explain failed`) — the exception text is only in the server log (see [`06_error_handling.md`](06_error_handling.md#mcp-error-envelope)). Debt item `API-4` is resolved. Internal/transient fields (`vector`, `start_offset`, `end_offset`, `custom_score`, `centroid`, `centroid_sum`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `namespace`, `mutations_since_recompute`, `described_at_doc_count`) are excluded from all responses. See `BREAKING.md` C7 entries for the five tools that narrowed their shapes.
4. **Errors are in-band.** Tool errors return `{"error": "...", "code": "..."}` as the tool result at HTTP 200, not an HTTP error. See `06_error_handling.md`.

## Search, ingest, collection, and document tools

Verified against `archon_search/server/mcp.py`. The other nine tools — `export_collection`, `import_collection`, `get_graph`, `get_graph_cross_collection`, `graph_impact`, and the four key-management tools — are listed in [`600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md#mcp-tools). The `collection` argument defaults to the server's `default_collection` when omitted. The happy-path returns are listed below; every tool also returns an `McpErrorResponse` dict (`{"error": str, "code": "internal_error"}`) on exception, regardless of the success type shown.

| Tool | Args | Return (success) |
| --- | --- | --- |
| `search` | `query: str`, `collection?: str`, `collections?: list[str]`, `include_metadata: bool = False`, `file_type?: str`, `source_path_prefix?: str`, `source_path_glob?: str`, `indexed_after?: str`, `indexed_before?: str`, `language?: str`, `hyde: bool = False`, `rag_fusion: bool = False`, `graph_mode?: str`, `scope_filter?: str` | `McpSearchResponse` dict — `{results: [McpSearchResultSchema dict …], acl_filtered, excluded_collections, hyde_applied, expansion_used, expansion_warning, graph_expansion_applied, ppr_entities_matched}` |
| `search_with_context` | `query: str`, `collection?: str`, `context_window: int = 1` (neighbour chunks on each side of a hit; outside `[0, [search].context_window_max]` (default 10) → `code="validation_error"`), plus the same filter, `include_metadata`, `hyde`, `rag_fusion`, `graph_mode?`, and `scope_filter?` params as `search` (no `collections`; any non-null `graph_mode` is rejected with `code="graph_mode_not_supported"`) | `SearchWithContextResponse` dict — `{results: [{result, context_before, context_after} …], hyde_applied, expansion_used, expansion_warning}` |
| `explain` | `query: str`, `collection?: str`, `collections?: list[str]`, `top_k: int = 5`, `rerank: bool = True`, `hyde: bool = False`, `rag_fusion: bool = False`, `graph_mode?: str`, `scope_filter?: str` | `ExplainResponse` dict (`{rerank, routing, collection, acl_filtered, results, near_misses, …}`) — same structure as REST `POST /explain` (serialized via `response.model_dump(mode="json", exclude_none=False)`) |
| `ingest_file` | `path: str`, `collection?: str`, `chunk_ttl_seconds?: int \| null`, `chunk_scopes?: list[str] \| null` | `IngestResultSchema dict` — fields: `doc_id`, `chunks_created`, `status`, `error`, `warnings: list[str]`, `code: str \| null` (`needs_recompute` excluded). On unsafe `path`: `{"error": <phrase>, "code": "path_unsafe"}`; when another write holds the lock past the timeout — before the file is read, or in the middle of the ingest: `{"error": ..., "code": "store_busy"}`; when file exceeds `max_file_mb`: `{status: "error", code: "file_too_large"}`; when the file cannot be parsed: `{status: "error", code: "parse_error", error: "Failed to parse <absolute path>: the document could not be parsed; see the server log for details"}` (`PARSE_FAILED_TEMPLATE` with `ParseError.detail`; a lost parse worker keeps its own curated text after the path, and a missing markitdown reads `markitdown is not installed; reinstall archon-search to restore it`). |
| `ingest_directory` | `path: str`, `glob_pattern: str = "**/*"`, `collection?: str`, `chunk_ttl_seconds?: int \| null`, `chunk_scopes?: list[str] \| null` | `list[IngestResultSchema dict]`; progress reported via `ctx.report_progress`. On unsafe `path`: `{"error": <phrase>, "code": "path_unsafe"}`; when another write holds the lock past the timeout — before any file is read, or in the middle of the ingest: `{"error": ..., "code": "store_busy"}`; per file, an oversized file produces `{status: "error", code: "file_too_large"}` and a file that cannot be parsed produces `{status: "error", code: "parse_error", error: "Failed to parse <absolute path>: the document could not be parsed; see the server log for details"}` (`PARSE_FAILED_TEMPLATE` with `ParseError.detail`; a lost parse worker keeps its own curated text after the path, and a missing markitdown reads `markitdown is not installed; reinstall archon-search to restore it`) — the batch continues for other files. |
| `list_collections` | — | `list[CollectionListItemSchema dict]` — public fields: `name`, `description`, `doc_count`, `chunk_count`, `last_indexed`, `last_described`, `embedding_model`, `pending_embedding_model`. All internal fields stripped. See `BREAKING.md` C7 entry. |
| `get_collections_meta` | `include_description_embedding: bool = False` | `list[CollectionMetaMcpSchema dict]` — same as `list_collections` plus `description_embedding: list[float] \| null` (always present; `null` when not requested). See `BREAKING.md` C7 entry. |
| `get_collection_meta` | `name: str` | `CollectionDetailSchema dict` — same public fields as `list_collections` (no `description_embedding`). Or `{"error": "Collection 'X' not found", "code": "not_found"}`. See `BREAKING.md` C7 entry. |
| `list_documents` | `collection?: str`, `limit: int = 100`, `cursor?: str` | `list[DocumentInfoSchema dict]` — fields: `doc_id`, `source_path`, `chunk_count`, `indexed_at`, `scopes: list[str]` |
| `delete_document` | `doc_id: str`, `collection?: str`, `namespace?: str` | `DeleteDocumentSchema dict` — `{"deleted": int}` |
| `update_collection` | `collection_name: str`, `embedding_model: str` | `CollectionDetailSchema dict` — same public fields as `get_collection_meta`. Or `{"error": ..., "code": "not_found" \| "validation_error" \| "conflict" \| "store_busy"}` (`conflict` while the collection's reindex job is still pending or running; `not_found` also when a concurrent `DELETE` dropped the collection during the update; `store_busy` when another write holds the collection lock past `INGEST_LOCK_TIMEOUT_S`). |

Each `McpSearchResultSchema` dict has the fields `{doc_id, chunk_id, text, score, source_path, file_type, language, indexed_at, updated_at, ingested_by, metadata, acl, collection, reranker_score}` — mirroring the `SearchResult` dataclass but validated and serialized via Pydantic (`mcp_schemas.py`). `reranker_score` is the cross-encoder score, or `null` when no reranker ran — see [Searching → MCP tools](../UserManual/60_searching.md#mcp-tools) for when that happens. Context chunks in `search_with_context` use `ContextChunkSchema`, which carries only `{doc_id, chunk_id, text, source_path, indexed_at, file_type, language, metadata, ingested_by, updated_at, acl}` — no `score`, `reranker_score`, or `collection`, and none of the stored `vector`, `custom_score`, `expires_at`, `scopes`, ACL-provenance, or offset fields (see `BREAKING.md` C7 entry, and [Searching → `search_with_context` neighbours](../UserManual/60_searching.md#search_with_context-neighbours) for how neighbours are selected and ordered).

## Difference vs REST

- `search` over MCP returns an object, not the bare list of result dicts it returned before (`BREAKING.md` "MCP `search` tool response shape"). Its `McpSearchResponse` envelope is a subset of REST's `SearchResponse`: REST adds `embedding_model`, `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`, and `applied_filters`. The per-result dicts carry the same fields, except that REST's `SearchResultSchema` (`routes_search.py`) adds `acl_gate`.
- `explain` mirrors REST `POST /explain`: it returns the same `ExplainResponse` structure (per-stage score breakdown for `results` and `near_misses`, plus the `routing` decision when no collection is pinned). The MCP tool serializes the model via `model_dump`; the REST route returns the Pydantic model directly. The query is never echoed in the response or telemetry on either surface.
- `search_with_context` exists **only** on MCP. There is no REST equivalent.
- Ingest is **synchronous** on MCP — `ingest_file` and `ingest_directory` block until done and return the result. REST's `POST /ingest` is async (returns a job). The MCP tools create no job, so they are not in `GET /jobs`; instead each registers as a writer of its collection for the whole call, and `DELETE /collections/{name}` answers `503` `store_busy` (`"an MCP ingest or a collection sync is in progress on this collection; retry after it completes"`) until it returns — for an `ingest_directory` of a large directory, minutes. A cancelled call (client disconnect) releases the registration.
- `list_documents` and `delete_document` are **MCP-only**. REST has no per-document routes.

## Registering with Claude Code

Claude Code reads `~/.claude/settings.json` (and `.mcp.json` in the project root). Add a server entry that points to the streamable HTTP endpoint at `/mcp` and supplies the bearer token in the `Authorization` header. Example:

```json
{
  "mcpServers": {
    "archon-search": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer ${ARCHON_SEARCH_API_KEY}"
      }
    }
  }
}
```

Export `ARCHON_SEARCH_API_KEY` in the shell that launches Claude Code, or hard-code the hex token if you cannot rely on env interpolation. Once the MCP app is reachable, Claude Code surfaces the registered tools under the `archon-search` namespace; their schemas come from `FastMCP`'s `@app.tool()` decorators in `mcp.py` (verified). #Unverified — that Claude Code presents them under that exact namespace label depends on client behavior, not on this repo.

#Unverified — Project-scoped configuration is described as using the same shape in `.mcp.json` at the repo root; this is Claude Code client behavior and is not verifiable from this repository.

## Using an MCP SDK directly

The endpoint speaks MCP's streamable HTTP transport. With the Python SDK:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

async with streamablehttp_client(
    "http://127.0.0.1:8765/mcp",
    headers={"Authorization": f"Bearer {KEY}"},
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("search", {
            "query": "how is the API key bootstrapped",
            "collection": "code-archon-search",
        })
        # result.content[0].text holds the JSON-serialized tool output.
```

For TypeScript, use `@modelcontextprotocol/sdk`'s streamable HTTP client with the same URL and header.

## Verifying the connection

A quick liveness check that exercises auth:

```bash
curl -sf -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Accept: application/json, text/event-stream" \
  -X POST http://127.0.0.1:8765/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A `200` response with a `result.tools` array of 20 entries (the shipped server always passes a `key_store`, so the four key-management tools register too) confirms the MCP surface is up and your token resolves correctly. A `401` means the bearer was rejected by `APIKeyMiddleware` — see `02_authentication.md`.

## Operational notes

- **No streaming results.** `ingest_directory` reports *progress* via `ctx.report_progress(done, total)`; search tools return whole payloads.
- **Default collection.** Every tool that takes `collection?` falls back to the `default_collection` argument passed to `create_mcp_http_app`. At startup, `app.py` sets it to `config.collections[0]` (first collection in config) or `"default"` when none are configured. Pass `collection` explicitly if your client manages multiple collections.
- **Telemetry side effects.** The MCP `search`, `search_with_context`, and `explain` tools enqueue telemetry entries when a `TelemetryWriter` is configured (the `writer.enqueue(TelemetryEntry...)` calls inside those tool closures in `create_app`, `mcp.py`). The entries never include the raw query — structural invariant from `archon_search/telemetry/entry.py`.

## Related documents

- [`02_authentication.md`](./02_authentication.md) — Bearer token, namespace resolution (shared with REST).
- [`06_error_handling.md`](./06_error_handling.md) — `McpErrorResponse` shape and error codes.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full surface.
- [`../Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules for both surfaces.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `API-4` (resolved in C7; MCP responses now Pydantic-validated).
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — roadmap item C7 (completed).

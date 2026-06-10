**Purpose**: Document how to consume `archon-search` as an MCP server, including the ten tool names, their argument shapes, and how to register the server with Claude Code and similar clients.
**Audience**: Engineers wiring `archon-search` into MCP-aware tools (Claude Code, Cline, custom agents using `@modelcontextprotocol` SDKs).
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# MCP Integration

`archon-search` defines an MCP server that mirrors the REST pipeline. The MCP transport is built by `create_mcp_http_app` in `archon_search/server/mcp.py` (lines 237-252): it wraps the `FastMCP` app's `streamable_http_app()` with the same `APIKeyMiddleware` used for REST. The Starlette app it returns exposes the streamable HTTP endpoint at `/mcp`.

**#Unverified — Runtime wiring**: As of this revision, `create_mcp_http_app` has no caller inside `archon_search/` — `server/app.py:create_app` only assembles the FastAPI app, and `run_server` (`app.py:152-155`) starts uvicorn against that FastAPI app with no Starlette mount for `/mcp`. The only caller of `create_mcp_http_app` is `tests/server/test_mcp_auth.py`. The "Registering with Claude Code" and `curl` examples below assume a process that exposes the Starlette MCP app on `/mcp`; against the shipped server they will not resolve until the MCP app is wired into the runtime.

## Principles

1. **Same auth as REST.** Every MCP request carries `Authorization: Bearer <token>`. The `/health` route is registered on the FastMCP app at `mcp.py:230` (via `@app.custom_route`); the exemption from `APIKeyMiddleware` is shared with REST and defined by `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}` in `archon_search/server/middleware_auth.py:16`.
2. **Ten tools, named exactly as in `mcp.py`.** Adding or renaming a tool requires a `BREAKING.md` entry per project policy (see `CLAUDE.md` and `Documentation/Architecture/520_api_design_and_contracts.md`). Tool names are not symmetric with REST routes.
3. **Responses are Pydantic-validated (C7).** All 11 MCP tools validate return values through explicit Pydantic schemas defined in `archon_search/server/mcp_schemas.py` (`extra='forbid'`) before serializing. Schema drift surfaces as `{"error": "...", "code": "schema_validation_error"}`. Debt item `API-4` is resolved. Internal/transient fields (`vector`, `start_offset`, `end_offset`, `custom_score`, `centroid`, `centroid_sum`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `namespace`, `mutations_since_recompute`, `described_at_doc_count`) are excluded from all responses. See `BREAKING.md` C7 entries for the five tools that narrowed their shapes.
4. **Errors are in-band.** Tool errors return `{"error": "...", "code": "..."}` as the tool result at HTTP 200, not an HTTP error. See `06_error_handling.md`.

## The ten tools

Verified against `archon_search/server/mcp.py`. The `collection` argument defaults to the server's `default_collection` when omitted. The happy-path returns are listed below; every tool also returns an `McpErrorResponse` dict (`{"error": str, "code": "internal_error"}`) on exception, regardless of the success type shown.

| Tool | Args | Return (success) |
| --- | --- | --- |
| `search` | `query: str`, `collection?: str` | `{"results": [SearchResult dict ...], "acl_filtered": bool}` |
| `search_with_context` | `query: str`, `collection?: str`, `context_window: int = 1` | `list[{result, context_before, context_after}]` |
| `explain` | `query: str`, `collection?: str`, `top_k: int = 5`, `rerank: bool = True` | `ExplainResponse` dict (`{rerank, routing, collection, acl_filtered, results, near_misses}`) — same structure as REST `POST /explain` (serialized via `response.model_dump(mode="json", exclude_none=False)`) |
| `ingest_file` | `path: str`, `collection?: str` | `IngestResultSchema dict` — fields: `doc_id`, `chunks_created`, `status`, `error` (`needs_recompute` excluded). On unsafe `path`: `{"error": <phrase>, "code": "path_unsafe"}`; when a reindex holds the lock: `{"error": ..., "code": "store_busy"}`. |
| `ingest_directory` | `path: str`, `glob_pattern: str = "**/*"`, `collection?: str` | `list[IngestResultSchema dict]`; progress reported via `ctx.report_progress`. On unsafe `path`: `{"error": <phrase>, "code": "path_unsafe"}`; when a reindex holds the lock: `{"error": ..., "code": "store_busy"}`. |
| `list_collections` | — | `list[CollectionListItemSchema dict]` — public fields: `name`, `description`, `doc_count`, `chunk_count`, `last_indexed`, `last_described`, `embedding_model`, `pending_embedding_model`. All internal fields stripped. See `BREAKING.md` C7 entry. |
| `get_collections_meta` | `include_description_embedding: bool = False` | `list[CollectionMetaMcpSchema dict]` — same as `list_collections` plus `description_embedding: list[float] \| null` (always present; `null` when not requested). See `BREAKING.md` C7 entry. |
| `get_collection_meta` | `name: str` | `CollectionDetailSchema dict` — same public fields as `list_collections` (no `description_embedding`). Or `{"error": "Collection 'X' not found", "code": "not_found"}`. See `BREAKING.md` C7 entry. |
| `list_documents` | `collection?: str`, `limit: int = 100` | `list[DocumentInfoSchema dict]` — fields: `doc_id`, `source_path`, `chunk_count`, `indexed_at` |
| `delete_document` | `doc_id: str`, `collection?: str` | `DeleteDocumentSchema dict` — `{"deleted": int}` |

Each `McpSearchResultSchema` dict has the fields `{doc_id, chunk_id, text, score, source_path, file_type, language, indexed_at, updated_at, ingested_by, metadata, acl, collection}` — mirroring the `SearchResult` dataclass but validated and serialized via Pydantic (`mcp_schemas.py`). The REST `SearchResultSchema` (`routes_search.py`) drops the `acl` and `collection` fields; MCP retains them in the schema. Context chunks in `search_with_context` use `ContextChunkSchema`, which excludes `start_offset`, `end_offset`, and `custom_score` (see `BREAKING.md` C7 entry).

## Difference vs REST

- `search` over MCP returns the same top-level envelope as REST (`{results, acl_filtered}`), but the per-result dicts differ: MCP results include the `acl` field; REST's `SearchResultSchema.from_result` (`routes_search.py:47`) omits it. #Unverified — the framing "post-`BREAKING.md` shape; the old bare-list response is gone" is a historical claim not re-verified against `BREAKING.md` in this revision.
- `explain` mirrors REST `POST /explain`: it returns the same `ExplainResponse` structure (per-stage score breakdown for `results` and `near_misses`, plus the `routing` decision when no collection is pinned). The MCP tool serializes the model via `model_dump`; the REST route returns the Pydantic model directly. The query is never echoed in the response or telemetry on either surface.
- `search_with_context` exists **only** on MCP. There is no REST equivalent.
- Ingest is **synchronous** on MCP — `ingest_file` and `ingest_directory` block until done and return the result. REST's `POST /ingest` is async (returns a job).
- `list_documents` and `delete_document` are **MCP-only**. REST has no per-document routes.

## Registering with Claude Code

> **#Unverified — Operational status.** The configuration and `curl` recipes in this section assume the Starlette MCP app (`create_mcp_http_app`) is mounted by the running server. In the current code that wiring does not exist (see the runtime-wiring note at the top of this document); these examples will only work once a caller of `create_mcp_http_app` runs the Starlette app alongside (or in place of) the FastAPI app on the same host/port.

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

Export `ARCHON_SEARCH_API_KEY` in the shell that launches Claude Code, or hard-code the hex token if you cannot rely on env interpolation. Once the MCP app is reachable, Claude Code surfaces the ten tools under the `archon-search` namespace; their schemas come from `FastMCP`'s `@app.tool()` decorators in `mcp.py` (verified). #Unverified — that Claude Code presents them under that exact namespace label depends on client behavior, not on this repo.

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

> **#Unverified — Operational status.** The `/mcp` endpoint is currently not exposed by `run_server` (see the runtime-wiring note above); the following `curl` will not succeed against the shipped server until the Starlette MCP app is mounted.

A quick liveness check that exercises auth:

```bash
curl -sf -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Accept: application/json, text/event-stream" \
  -X POST http://127.0.0.1:8765/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A `200` response with a `result.tools` array of ten entries confirms the MCP surface is up and your token resolves correctly. A `401` means the bearer was rejected by `APIKeyMiddleware` — see `02_authentication.md`.

## Operational notes

- **No streaming results.** `ingest_directory` reports *progress* via `ctx.report_progress(done, total)`; search tools return whole payloads.
- **Default collection.** Every tool that takes `collection?` falls back to the `default_collection` argument passed to `create_app` / `create_mcp_http_app` (`mcp.py:30-34`, `:237-241`). #Unverified — `SearchConfig` has no `default_collection` field (only `namespaces: dict[str, str]` at `config.py:54`); the previous claim that this resolves to "the first registered collection in config" has no source-code basis, and the actual value depends on the (currently missing) caller of these factories. Pass `collection` explicitly if your client manages multiple collections.
- **Telemetry side effects.** The MCP `search` and `search_with_context` tools enqueue telemetry entries when a `TelemetryWriter` is configured (`mcp.py:47-58` for `search`, `:88-99` for `search_with_context`). The entries never include the raw query — structural invariant from `archon_search/telemetry/entry.py`.

## Related documents

- [`02_authentication.md`](./02_authentication.md) — Bearer token, namespace resolution (shared with REST).
- [`06_error_handling.md`](./06_error_handling.md) — `McpErrorResponse` shape and error codes.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full surface.
- [`../Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules for both surfaces.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `API-4` (resolved in C7; MCP responses now Pydantic-validated).
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — roadmap item C7 (completed).

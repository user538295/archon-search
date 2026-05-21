# Review: DeveloperGuide/01_overview.md

Source-of-truth references:
- `archon_search/server/app.py`
- `archon_search/server/mcp.py`
- `archon_search/server/middleware_auth.py`
- `archon_search/server/routes_*.py`
- `archon_search/server/schemas.py`
- `archon_search/config.py`
- `archon-search.toml.example`
- `BREAKING.md`

## Summary

The overview is largely accurate at the principle and surface level, but contains one structural inaccuracy about how the MCP transport is wired (it is **not** mounted under `/mcp` on the FastAPI app — `app.py` builds a REST-only FastAPI app, and `mcp.py` builds an independent Starlette app with its own `APIKeyMiddleware`). A few smaller claims (cross-namespace → 404 framed as universal, MCP error envelope shape, "30-line" SDK claim) are either oversimplified or unverifiable from code.

## Inaccuracies (numbered)

1. **§"A 60-second mental model" — "A FastAPI app (`archon_search/server/app.py`) that mounts the REST routes under `/` and the FastMCP streamable HTTP transport under `/mcp`. Both go through `APIKeyMiddleware`."**
   False. `create_app` in `archon_search/server/app.py` (lines 79–149) only `include_router`s the REST routers (collections, health, jobs, status, state, route, search, telemetry); it never references `mcp.py`, never mounts a `/mcp` sub-app, and FastMCP is not imported. The MCP surface is built by a separate factory `create_mcp_http_app` in `archon_search/server/mcp.py` (lines 237–252), which returns its **own** Starlette app wrapping `fastmcp_app.streamable_http_app()` and adds its own `APIKeyMiddleware` instance. The two apps share the same `APIKeyMiddleware` *class* and the same on-disk API key (both call `load_or_generate_key()`), but they are not the same ASGI app, and neither `cli/start.py` nor `cli/main.py` shows any code mounting one into the other.

2. **§"REST vs MCP at a glance" / Auth row — "Same middleware, same header" framed as identical resolution.**
   Partially misleading. Both use the `APIKeyMiddleware` class, but they are instantiated differently: REST gets `namespaces=config.namespaces` (`app.py:121`), MCP gets `namespaces={}` (`mcp.py:251`). Therefore namespace-keyed tokens defined in `[namespaces]` resolve to their namespace on REST but fall through to `DEFAULT_NAMESPACE` on the MCP app. The guide's flat "same middleware" claim hides this.

3. **§"Principles" #4 — "Cross-namespace access yields `404`, never `403`."**
   Only verified for some collection-scoped REST routes (`routes_collections.py` uses 404 for cross-namespace `GET /collections/{name}`, `DELETE /collections/{name}`, `POST /collections/{name}/reindex`, and `routes_search.py` returns 404 when meta lookup yields `None` regardless of namespace). The general claim "never `403`" is plausible but is presented as an invariant; `middleware_auth.py` only produces 401s, and no source file emits 403, so the claim holds at the auth layer — but as a *cross-namespace* invariant it is asserted more broadly than the code explicitly enforces (e.g. `/search` returns 404 for "collection not found" by structure, not by an explicit namespace-rejection branch). Recommend rewording to "cross-namespace collections are indistinguishable from missing collections (404)."

4. **§"REST vs MCP at a glance" / Error envelope row — MCP error envelope `{"error": "...", "code": "..."}`.**
   Verified shape (`McpErrorResponse` TypedDict, `mcp.py:25–27`), but the row says it is "returned as the tool result (HTTP 200)". The HTTP-200 framing is the FastMCP transport's behavior, not something archon-search controls; the code only returns the dict. This is *probably* correct but not directly verifiable from the archon-search source — flagged as ambiguous rather than wrong.

5. **§"What is not in scope" — "No per-request `top_k`. The `top_k` field in `SearchRequest` is accepted for backwards compatibility but ignored — the pipeline uses `[search] top_k_return`."**
   Mostly accurate but slightly imprecise. `routes_search.py:17–20` still declares `top_k: int = Field(default=5, ge=1, le=100)` (so the field is parsed and validated), and `pipeline.search` does not accept a per-call `top_k` (uses `self._top_k_return`, `pipeline.py:303`). The field is accepted but never passed through — consistent with `BREAKING.md` line 21. Calling it "ignored" is fair; calling it "backwards compatibility" overstates intent (`BREAKING.md` frames it as the new behavior, not a compat shim).

6. **§"What is not in scope" — "Examples in this guide use `httpx` (Python) and `fetch` (TypeScript); both are 30-line implementations against the OpenAPI schema."**
   Unverifiable from source. The "30-line" claim is editorial; nothing in `archon_search/` constrains client size.

7. **§"A 60-second mental model" — "A small job store for ingest/reindex (`archon_search/jobs/store.py`) — REST submits to it, MCP bypasses it."**
   Verified for `ingest_file` / `ingest_directory` (which call `pipeline.ingest_*` directly and return results synchronously, `mcp.py:124–161`). No `JobStore` import exists in `mcp.py`. Claim stands.

8. **§"A 60-second mental model" — "When you call the MCP `search` tool, the path is the same except the final stage is `dataclasses.asdict(...)` instead of a Pydantic model."**
   Almost accurate. `mcp.py:59` does return `{"results": [asdict(r) ...], "acl_filtered": ...}`. However, the MCP `search` path skips the REST handler's pre-flight `pipeline.get_collection_meta(...)` 404 check (`routes_search.py:68–74`), so it is *not* "the same path" — missing collections surface as pipeline errors caught by the broad `except Exception` and returned as `{"error": ..., "code": "internal_error"}` (`mcp.py:60–74`). This is a behavioral divergence the guide elides.

9. **§"REST vs MCP at a glance" — Surface row enumerates `/search, /route, /collections/*, /ingest, /jobs/{id}, /telemetry/*, /health, /status, /indexing-state`.**
   Missing: `DELETE /jobs/{job_id}` (`routes_jobs.py:119`) and `POST /collections/{name}/reindex` (`routes_collections.py:299`). The `/collections/*` wildcard arguably covers reindex; `/jobs/{id}` does not cover DELETE.

10. **§"REST vs MCP at a glance" — Schema-validation row references "debt item `API-4`".**
    Unverifiable from server source; would need to check `Architecture/530_technical_debt_refactoring_roadmap.md` (out of scope per review rules: "NEVER trust Documentation/ files"). Flagged.

## Verified claims

- Default bind `127.0.0.1:8765` (`config.py:30–31`, `archon-search.toml.example:17–18`).
- All endpoints require `Bearer` auth except those in `_EXEMPT_PATHS = {/health, /docs, /openapi.json, /redoc}` (`middleware_auth.py:16, 26–27`). The guide's "every authenticated request resolves to exactly one namespace (`request.state.namespace`)" matches `middleware_auth.py:61`.
- MCP tool count = 9: `search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document` (`mcp.py:38–228`; docstring `"Create a FastMCP app with 9 RAG tools registered."` at `mcp.py:35`).
- `POST /ingest` returns 202 with `JobResponse` (`routes_jobs.py:91`); `GET /jobs/{job_id}` returns `JobResponse` (`routes_jobs.py:108`); `JobResponse` defined in `schemas.py:70–77`.
- `ingest_directory` MCP tool calls `ctx.report_progress(done, total)` (`mcp.py:148–150`) — matches "progress reported via `ctx.report_progress` for `ingest_directory`".
- Routes split per resource exists as claimed; all eight routers are imported and included in `app.py:33–40, 140–147`.
- The pipeline is `SearchPipeline` constructed in `app.py:131–139` and orchestrates store + embedder + reranker + chunker + parser. The "parse → chunk → embed → store → rerank" order is consistent with `pipeline.py:301–303` (search path).
- REST error envelope `{"detail": "..."}` — confirmed via `JSONResponse({"detail": "collection not found"}, status_code=404)` in `routes_search.py:74` and `ErrorDetail(BaseModel)` in `schemas.py:85–86`.
- MCP endpoint path is `/mcp` (`mcp.py:245` docstring; FastMCP `streamable_http_app()` default).

## Unverifiable / ambiguous

- "no shared-state coordination, no leader election, no sticky session protocol" — true by absence but unprovable from a single review pass; no code suggests otherwise.
- "watcher-triggered reindexing is observable only through `GET /indexing-state` and `GET /status`" — `/indexing-state` (`routes_state.py:14`) and `/status` (`routes_status.py:22`) exist; the "only" qualifier is not explicitly enforced anywhere.
- "FastMCP streamable HTTP" as the transport label — `mcp.py:249` calls `fastmcp_app.streamable_http_app()`, so the label is accurate w.r.t. the FastMCP API name; whether the wire format is what readers will expect from "streamable HTTP" depends on FastMCP version, which is not pinned here.
- Editorial guidance ("OpenAPI is the binding contract", "When this guide and `/openapi.json` disagree, the schema wins") — policy claims, not code claims; cannot be verified against source.

**Purpose**: Orient external engineers integrating `archon-search` from another application, and frame the scope and non-goals of the integration surface.
**Audience**: Engineers writing Python or TypeScript clients, MCP-aware tools (Claude Code, custom agents), or any process that calls `archon-search` over HTTP or MCP.
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Developer Guide — Overview

This guide is for engineers consuming `archon-search` from outside its own process. It is not the operator manual (see `Documentation/UserManual/`) and not the contributor guide (`contributing.md`). It assumes you have a running instance reachable over HTTP and you want to call it from your own code.

The authoritative contract is `GET /openapi.json` on the running server. Everything in this guide cross-references that schema and the code under `archon_search/server/`.

## Principles

1. **One process, one machine.** `archon-search` is a single-process FastAPI app intended to run next to its caller (loopback or LAN). It is not designed for horizontal scaling — no shared-state coordination, no leader election, no sticky session protocol. Run one instance per host; treat multi-host setups as separate, independently indexed deployments.
2. **No native TLS.** The server binds plain HTTP. If you need encryption in transit, terminate TLS in a reverse proxy (caddy / nginx / Cloudflare Tunnel) and forward to the loopback port. The auth model below assumes the wire is trusted or wrapped.
3. **REST and MCP are siblings, not mirrors.** The two surfaces are built by separate factories — `create_app` in `archon_search/server/app.py` (REST-only FastAPI) and `create_mcp_http_app` in `archon_search/server/mcp.py` (independent Starlette app wrapping FastMCP's `streamable_http_app()`). They share the same `APIKeyMiddleware` *class* and the same on-disk API key (both call `load_or_generate_key()`), and they share the internal `SearchPipeline`, but they are not the same ASGI app and their tool/route names are intentionally not 1:1. The MCP surface exposes per-document operations (`ingest_file`, `delete_document`); the REST surface exposes the job-oriented control plane (`POST /ingest`, `GET /jobs/{id}`). Consult `archon_search/server/mcp.py` for MCP and `archon_search/server/routes_*.py` for REST.
4. **Auth is a single Bearer token, namespace-resolved.** Every authenticated request resolves to exactly one namespace (`request.state.namespace`). The REST app is constructed with `namespaces=config.namespaces` (`app.py:121`); the MCP app is constructed with `namespaces={}` (`mcp.py:251`), so namespace-keyed tokens defined in `[namespaces]` resolve to their namespace on REST but fall through to `DEFAULT_NAMESPACE` on MCP. At the auth layer the middleware only emits `401`; cross-namespace collections are indistinguishable from missing collections (`404`) on the collection-scoped REST routes — see `02_authentication.md`.
5. **OpenAPI is the binding contract.** When this guide and `/openapi.json` disagree, the schema wins and this guide gets a follow-up fix. Same rule for the MCP tool list in `mcp.py`.

## REST vs MCP at a glance

| Concern | REST (`/`) | MCP (`/mcp`) |
| --- | --- | --- |
| Transport | HTTP + JSON | FastMCP streamable HTTP (label per `mcp.py:249`; wire format depends on FastMCP version, which is not pinned in this guide) #Unverified |
| Auth | `Authorization: Bearer <token>` | Same `APIKeyMiddleware` class and same on-disk key, but instantiated with `namespaces={}` — namespace-keyed tokens fall through to `DEFAULT_NAMESPACE` on MCP |
| Schema validation on response | Pydantic (`response_model=`) | None today — raw `dataclasses.asdict(...)` (debt item `API-4` referenced in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` #Unverified) |
| Error envelope | `{"detail": "..."}` with HTTP status | `{"error": "...", "code": "..."}` (`McpErrorResponse` TypedDict, `mcp.py:25–27`) returned as the tool result; the HTTP-200 framing is the FastMCP transport's behavior rather than something archon-search controls #Unverified |
| Long-running operations | Job model (`POST /ingest` → 202 + `JobResponse`, poll `GET /jobs/{id}`) | Synchronous within the tool call; progress reported via `ctx.report_progress` for `ingest_directory` |
| Surface | `/search`, `/route`, `/collections/*` (including `POST /collections/{name}/reindex`), `/ingest`, `/jobs/{id}` (incl. `DELETE /jobs/{job_id}`), `/telemetry/*`, `/health`, `/status`, `/indexing-state` | 10 tools (see `05_mcp_integration.md`) |

## What is not in scope

- **No bulk-query API.** Each `/search` and `/route` call is one query. Batch by issuing concurrent requests; the server is async end-to-end.
- **No streaming search results.** Responses are fully buffered JSON. The MCP `ingest_directory` tool streams *progress*, not results.
- **No webhooks or push notifications.** Job state is poll-only (`GET /jobs/{id}`); watcher-triggered reindexing is observable only through `GET /indexing-state` and `GET /status`.
- **No client SDKs published.** Use any HTTP client. Examples in this guide use `httpx` (Python) and `fetch` (TypeScript); the "30-line" sizing in earlier drafts is editorial and not enforced by anything in the codebase. #Unverified
- **No per-request `top_k`.** The `top_k` field on `SearchRequest` is still declared and validated (`routes_search.py`: `top_k: int = Field(default=5, ge=1, le=100)`) but is never passed through to the pipeline, which uses `self._top_k_return` from `[search] top_k_return` in `archon-search.toml`. `BREAKING.md` frames this as the current behavior rather than a backwards-compatibility shim.

## A 60-second mental model

The runtime is one process. It owns:

- A LanceDB store at `~/.archon-search/db/` with one table per collection plus an FTS index (`archon_search/store.py`).
- An async `SearchPipeline` that orchestrates parse → chunk → embed → store → rerank (`archon_search/pipeline.py`).
- A multi-collection router that uses per-collection centroids to pick which collections to query (`archon_search/router.py`).
- A REST-only FastAPI app built by `create_app` in `archon_search/server/app.py`, which `include_router`s the eight REST routers and adds its own `APIKeyMiddleware` instance.
- A separate Starlette app built by `create_mcp_http_app` in `archon_search/server/mcp.py`, wrapping `FastMCP.streamable_http_app()` and adding its own `APIKeyMiddleware`. The MCP endpoint path is `/mcp`. The two apps share the `APIKeyMiddleware` class (`archon_search/server/middleware_auth.py`) and the same on-disk API key, but they are independent ASGI apps — `app.py` does not import `mcp.py` and does not mount it.
- A small job store for ingest/reindex (`archon_search/jobs/store.py`) — REST submits to it, MCP bypasses it (`mcp.py` does not import `JobStore`).

When you call `POST /search`, the request flows: middleware → route handler (`routes_search.py`) → `SearchPipeline.search` → LanceDB hybrid search → reranker → ACL filter → Pydantic response. The MCP `search` tool returns `{"results": [asdict(r) for r in result_obj.results], "acl_filtered": ...}` instead of a Pydantic model, and it skips the REST handler's pre-flight `pipeline.get_collection_meta(...)` 404 check (`routes_search.py:68–74`): on MCP, a missing collection surfaces as a pipeline error caught by the broad `except Exception` and returned as `{"error": ..., "code": "internal_error"}` rather than as a structured 404.

Defaults live in `archon-search.toml.example`. The server listens on `127.0.0.1:8765` by default; this guide assumes that base URL.

## Where to go next

| If you want to… | Read |
| --- | --- |
| Get an API key working | `02_authentication.md` |
| Call REST from Python | `03_rest_client_python.md` |
| Call REST from TypeScript | `04_rest_client_typescript.md` |
| Register `archon-search` as an MCP server | `05_mcp_integration.md` |
| Handle errors and decide what to retry | `06_error_handling.md` |
| Pin a client across releases | `07_versioning_and_breaking_changes.md` |
| See the full surface | `../Architecture/600_api_reference_or_public_interface.md` |
| Read design rules behind both surfaces | `../Architecture/520_api_design_and_contracts.md` |

## Related documents

- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — endpoint-by-endpoint reference.
- [`../Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules for REST and MCP.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — auth model, namespaces, ACL.
- [`../../BREAKING.md`](../../BREAKING.md) — compatibility contract.

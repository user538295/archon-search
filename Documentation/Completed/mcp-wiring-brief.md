# Feature Brief: MCP HTTP Server Wiring

## Problem

Claude Code and all other MCP clients cannot connect to archon-search because `create_mcp_http_app()` — a fully implemented Starlette app — is never started at server startup. The production server only runs the FastAPI REST app. Users who add archon-search as an MCP server get a connection failure.

## Goal

On server start, archon-search mounts the MCP HTTP endpoint at `/mcp` on the existing REST port (8765), fully authenticated, namespace-aware, and telemetry-wired — identical in capability to the REST API. Claude Code and other MCP clients can connect immediately with the same bearer token used for REST.

## Users & Context

A developer or operator who has archon-search running and wants to add it as an MCP server in Claude Code (or any FastMCP-compatible client). They already have a bearer token. They expect the same search, ingest, and management capabilities available over REST to work identically over MCP.

## Core Flow

1. Operator starts archon-search (native service or Docker). Server binds REST on port 8765; MCP is available at `http://localhost:8765/mcp`.
2. Operator adds `http://localhost:8765/mcp` as an MCP server URL in their client, with the same bearer token.
3. Client connects and lists 17 tools — the MCP data-plane surface (search, ingest, collections, keys, export/import, explain).
4. All tool calls respect namespace auth, tool-level namespace gating, and write telemetry — identical to REST.

## In Scope

- **ADR spike (prerequisite, K-1):** confirm that `app.mount("/mcp", create_mcp_http_app(...))` works with FastMCP's streamable-HTTP ASGI app — specifically that FastMCP's session management is compatible with FastAPI's middleware stack when mounted, and that ASGI lifespan delegates correctly. Prove the namespace propagation mechanism with working code (not just design). The ADR must document both findings before any implementation begins.
- **Server wiring:** call `create_mcp_http_app()` inside `create_app()`'s async lifespan after all objects are ready; mount the returned Starlette app at `/mcp` on the existing FastAPI app. No second Uvicorn, no second port.
- `[mcp]` TOML section: `enabled` (default `true`). New `McpConfig` dataclass in `archon_search/config.py`.
- Fix all three hidden asymmetries in `create_mcp_http_app()`:
  1. **Namespace auth**: pass `config.namespaces` instead of the hardcoded `namespaces={}`.
  2. **Tool-level namespace propagation** (mislabeled in the original brief as "gating"): the middleware (`APIKeyMiddleware`) already rejects invalid tokens. The actual bug is that all tool closures hardcode `DEFAULT_NAMESPACE` when calling pipeline methods — e.g., `pipeline.search(query, _col, ...)` has no `namespace=` kwarg (line 340), `pipeline.search_with_context(...)` has no `namespace=` (line 472), `pipeline.get_all_collections_meta()` has no `namespace=` (line 914). A valid token scoped to namespace A silently sees namespace B's data. The fix is to thread the caller's authenticated namespace into every pipeline call — the same pattern REST routes use (`ns = request.state.namespace` then `pipeline.search(..., namespace=ns)`). **Implementation blocker to resolve in ADR**: `APIKeyMiddleware` sets `request.state.namespace` on the Starlette request, but FastMCP tool functions receive `fastmcp.Context`, not the request. Currently only `update_collection` reads `ctx.meta.get("namespace")`; all other tools hardcode `DEFAULT_NAMESPACE`. The ADR must document the FastMCP hook (e.g., `lifespan`, `dependencies`, or custom middleware) that makes the authenticated namespace available to tool closures. **Critical caveat**: `ctx.meta.get("namespace")` in `update_collection` (`mcp.py:1022`) is currently dead code — nothing in the codebase populates `ctx.meta["namespace"]` from Starlette middleware. There is no existing working pattern to copy. The ADR must prove this mechanism works via a concrete spike (not just design) before the team plan is written; discovering mid-implementation that FastMCP `Context.meta` cannot receive Starlette request state would require a fundamentally different approach.
  3. **Telemetry wiring**: pass the telemetry writer so MCP tool calls appear in telemetry stats.
- ADR documenting the mount architecture decision (mount confirmed via K-1 spike) and the namespace propagation mechanism.
- `GET /status` and `GET /health` responses include MCP bind address when enabled.
- e2e tests: **one smoke test per MCP tool (17 total, one per tool)** through the live MCP transport. Each smoke test verifies: (a) the tool responds, (b) the response is non-empty and schema-valid. Two tools additionally require a round-trip data check: `ingest_file` (or `ingest_directory`) must confirm the document was stored; `search` must return a document that was previously ingested. The other 15 tests are shape-only smokes — sufficient to prove transport and wiring, not to validate business logic.
- At least **one namespace data-isolation e2e test** for asymmetry fix #2: use a valid bearer token scoped to namespace A, call a tool that reads/lists data (e.g., `search` or `list_documents`), and assert that data from namespace B is NOT returned. A test that only verifies token rejection would pass even without fixing the tool-level namespace hardcoding (the middleware already rejects invalid tokens). The test must prove DATA isolation for a valid cross-namespace token, not just authentication failure.
- One **lifecycle integration test**: verify via TestClient context-manager exit that the MCP mount shuts down cleanly with the REST app — no errors, no resource leaks.
- One **`mcp.enabled = false` gate test** (unit or integration): start the server with `mcp.enabled = false` and assert the `/mcp` mount is never created.
- One **telemetry wiring e2e test**: call an MCP tool with telemetry enabled and assert a corresponding telemetry entry appears in the telemetry log.
- **Key load ordering**: `create_mcp_http_app()` is called inside `create_app()`'s lifespan after the key is already loaded — no race possible. `run_server()` does not need to change.
- **Tool count note**: the 17 tool count assumes `key_store` is passed to `create_mcp_http_app()` (the 4 key-management tools are conditionally registered). Without `key_store`, only 13 tools register. Wiring must pass `key_store`.
- Documentation: update CLAUDE.md, architecture docs, user manual, and API reference with the MCP endpoint URL (`http://host:8765/mcp`).

## Out of Scope

- MCP-specific auth tokens — MCP uses the same key store as REST; no separate key namespace.
- MCP over stdio or SSE transports — HTTP (streamable-http) only in this iteration.
- Unhappy-path / auth-failure / namespace-isolation e2e tests — happy-path coverage is the gate; negative paths are covered by existing unit and integration tests. **Carve-out:** asymmetry fix #2 (namespace propagation) is new code across all tool closures with no prior test. The single namespace data-isolation e2e test listed in In Scope is required — it verifies that a valid cross-namespace token cannot see the wrong namespace's data. This carve-out does not reopen the broader negative-path suite.
- **Use Cases layer extraction** — extracting shared logic from REST routes and MCP tools into a `use_cases/` module is a **separate follow-on backlog item**, not part of this feature's close-out. The MCP wiring is done when wiring works, the three asymmetries are fixed, and the e2e suite is green; the duplication between REST and MCP adapters is acknowledged tech debt tracked separately.
- TLS for the MCP port — operator responsibility (reverse proxy), same as REST.

## Key Decisions

- **Port architecture: mount chosen.** `app.mount("/mcp", create_mcp_http_app(...))` on port 8765 — single Uvicorn, shared event loop, no signal-handler coordination, no second port to expose. The K-1 spike must confirm FastMCP's ASGI transport is mountable with correct lifespan delegation and middleware inheritance; if a concrete blocker is found, the ADR records it and the team revisits. Separate-port is not planned.
- **`mcp.enabled = true` by default**: MCP is mounted on the same port as REST, fully tested, and adds no instability. Defaulting on means operators get MCP working immediately; those who want to disable it set `mcp.enabled = false` in TOML.
- **Fix three asymmetries first**: the three targeted fixes ship with the wiring and make MCP functionally correct. The Use Cases extraction that would remove the structural duplication between REST routes and MCP tools is explicitly **a separate follow-on backlog item** — it does not block this feature's close-out. This feature is complete when wiring works, the three asymmetries are fixed, and e2e is green.
- **Mostly happy-path e2e**: 17 tools × full error matrix = 100+ tests. Auth and namespace failure modes are proven at unit/integration level; happy-path e2e proves the transport and wiring are correct. One namespace-rejection test is the sole negative-path carve-out — it covers new transport-level code (asymmetry fix #2) that has no prior test.
- **No `ARCHON_SEARCH_MCP_PORT` env var**: not needed in mount mode; both REST and MCP share the existing `ARCHON_SEARCH_PORT`.

## Edge Cases & Constraints

- **LanceDB write contention**: REST and MCP share the same LanceDB store. LanceDB uses file-level locking for commits, so concurrent ingest from REST and MCP **serialises at the LanceDB layer — it does not deadlock**. Latency under concurrent write load may increase; correctness is preserved.
- **`mcp.enabled = false`**: REST starts normally; the `/mcp` mount is never created. `GET /status` omits the `mcp` field.
- **`serve` mode (Docker / container)**: MCP inherits the same host as REST (flipped to `0.0.0.0` when `serve=True`) — both are on port 8765.
- **Namespace-empty config**: `config.namespaces = {}` is a valid state (no namespace gating). Passing it to `create_mcp_http_app()` reproduces current behaviour — no regression for single-namespace installs.
- **Telemetry disabled**: if telemetry is off, the writer is `None`; MCP tools must null-check the writer the same way REST routes do.
- **`KeyStore` threading**: `create_mcp_http_app()` is called inside `create_app()`'s lifespan and shares the same asyncio event loop as REST. `KeyStore`'s internal `asyncio.Lock` serialises read-modify-write cycles correctly within the shared event loop — no new locking needed.
- **Lifespan-constructed objects**: resolved by mount. `create_mcp_http_app()` is called inside `create_app()`'s async lifespan after `pipeline`, `writer`, `embedder_cache`, `job_store`, and `key_store` are all constructed — no race, no ordering problem.
- **`rotate_key` hot-reload limitation**: the `rotate_key` MCP tool's docstring documents that any cached in-memory `api_key` in the MCP middleware is not hot-reloaded after key rotation. The new key is valid immediately via `KeyStore.active_keys()` (disk read-on-demand), but the old in-memory key persists until restart. This is accepted behaviour in v1; document it in the operator runbook.

## Open Questions

1. **Mount compatibility** *(answered — must be confirmed by K-1 spike)*: mount is the chosen approach. K-1 must confirm that `app.mount("/mcp", create_mcp_http_app(...))` works with FastMCP's streamable-HTTP ASGI app — specifically that FastMCP's session management is compatible with FastAPI's middleware stack when mounted, and that ASGI lifespan delegates correctly. If a concrete blocker is found, the ADR records it and the team revisits.
2. **Namespace propagation mechanism** *(open — must be proven by K-1 spike)*: how does `request.state.namespace` (set by `APIKeyMiddleware`) reach FastMCP tool closures? Tool functions receive `fastmcp.Context`, not the Starlette request. `ctx.meta.get("namespace")` in `update_collection` (`mcp.py:1022`) is dead code — nothing populates it. No existing working pattern to copy. **K-1 must prove the mechanism with working code before asymmetry fix #2 (BE-5) can be estimated.**
3. **Lifespan objects** *(resolved by mount)*: `pipeline`, `writer`, `embedder_cache`, `job_store` are constructed inside `create_app()`'s async lifespan — and `create_mcp_http_app()` is called there too, so all objects are available. No race, no ordering problem.
4. **Lifecycle test approach** *(answered)*: TestClient context-manager exit. Simulates clean ASGI shutdown; fast and CI-safe. Subprocess + real SIGTERM deferred to a `@pytest.mark.live` test if signal-handling bugs emerge in practice.

## Future Iterations

- MCP-over-stdio transport — for local clients that prefer subprocess launch over HTTP.
- Per-port TLS configuration — once an operator story emerges for mTLS between Claude Code and archon-search.

## Recommendation

Build this now. The production gap (MCP implemented but never started) is a silent failure for every Claude Code user — they add the server and get nothing. Architecture and lifecycle questions are resolved: mount on port 8765, MCP app created inside `create_app()`'s lifespan, TestClient for the lifecycle test. One open question remains before implementation begins: how does the authenticated namespace propagate into FastMCP tool closures? K-1 must prove this mechanism with working code. It is the highest-risk change — it touches every MCP tool closure — and its correctness must be proven by the data-isolation e2e test, not just a token-rejection test.

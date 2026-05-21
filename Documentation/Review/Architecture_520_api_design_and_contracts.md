# Review: Architecture/520_api_design_and_contracts.md

## Summary

Verified against `archon_search/server/app.py`, `middleware_auth.py`, `mcp.py`,
`schemas.py`, `schemas_telemetry.py`, `routes_*.py`, and
`archon_search/telemetry/entry.py`.

The document is mostly accurate at the principle level. Two material
inaccuracies: (1) the "MCP mirrors REST" framing is factually wrong — five of
the nine MCP tools have no REST equivalent, so the order-of-operations
prescription "Add the REST endpoint … then add an MCP tool that wraps it" does
not match how the surface is actually shaped today; (2) the claim that all
request/response schemas live in `schemas.py` and `schemas_telemetry.py` is
false — `routes_search.py` and `routes_route.py` define their own inline
`BaseModel`s (`SearchRequest`, `SearchResponse`, `SearchResultSchema`,
`RouteRequest`, `RouteResponse`). Smaller inaccuracies and one ambiguous claim
listed below.

## Inaccuracies (numbered)

1. **"MCP mirrors REST … maps to the corresponding REST endpoints — there is
   no MCP-only control plane."** (§ Principles #2 and § "MCP Mirrors REST",
   lines 14, 41.) False as stated. REST routes (from
   `routes_*.py`) are: `GET /health`, `GET /status`, `GET /indexing-state`,
   `GET/POST/DELETE /{name}` collection ops + `POST /{name}/reindex`,
   `POST /ingest`, `GET /jobs/{job_id}`, `DELETE /jobs/{job_id}`,
   `POST /search`, `POST /route`, `GET /telemetry/stats`,
   `GET /telemetry/entries`. The MCP tool list is `search`,
   `search_with_context`, `ingest_file`, `ingest_directory`,
   `list_collections`, `get_collections_meta`, `get_collection_meta`,
   `list_documents`, `delete_document`. Five MCP tools have no REST
   counterpart: `search_with_context`, `ingest_file`/`ingest_directory` (REST
   exposes only the async job-based `POST /ingest`, not these synchronous
   per-path tools), `list_documents`, `delete_document`. Conversely, REST
   exposes `/route`, `/jobs/*`, `/status`, `/indexing-state`, `/telemetry/*`
   with no MCP equivalent. The surfaces overlap but neither mirrors the other.

2. **"All request and response shapes that cross the API boundary are Pydantic
   `BaseModel` subclasses in two files."** (§ Schema Discipline, lines 53-56.)
   False. `routes_search.py` defines `SearchRequest`, `SearchResultSchema`,
   and `SearchResponse` inline; `routes_route.py` defines `RouteRequest` and
   `RouteResponse` inline. The two named files only cover health/status/
   collections/jobs/state/error envelope plus telemetry.

3. **"Add MCP tools that wraps it … (REST first, then MCP)"** (§ Adding or
   Changing an Endpoint, step 3, line 86.) The order is prescriptive but the
   existing codebase contradicts the rule (see #1). At minimum the doc should
   acknowledge that several MCP tools have no REST counterpart by design, or
   the rule should be amended.

4. **"`_EXEMPT_PATHS` (`GET /health`, plus defensive entries for `/docs`,
   `/openapi.json`, `/redoc` that FastAPI never includes in the schema
   anyway)."** (§ OpenAPI is Authoritative, line 24.) The set is a
   `frozenset[str]` of paths, not method+path — so it exempts `/health` for
   *all* HTTP methods, not specifically `GET /health` (see
   `middleware_auth.py:16` and `:26`). Doc phrasing implies method-scoped
   exemption.

5. **"`ErrorDetail` … exposed via `responses={401: {"model": ErrorDetail},
   ...}`. Do not invent ad-hoc error shapes."** (§ Schema Discipline, line 62.)
   Partially true. `routes_search.py` (`POST /search`) and `routes_route.py`
   (`POST /route`) declare no `responses=` map at all (verified — only
   `response_model=` is set on those routes), so error envelopes from those
   endpoints come from FastAPI defaults, not `ErrorDetail`. The "always use
   `ErrorDetail` via `responses=`" rule is aspirational rather than enforced.

6. **"Every new route should declare `response_model=` and an explicit
   `responses=` map for non-200 cases."** (§ Consequences, line 30.) Same as
   #5 — search and route routes do not declare a `responses=` map. Listing
   this as an existing convention overstates the codebase state; it is a
   target, not the current norm.

7. **"`schemas.py` … `HealthResponse`, `StatusResponse`,
   `StatusCollectionEntry`, `IndexingStateResponse`, `CollectionSummary`,
   `CollectionDetail`, `JobResponse`, `DeleteResponse`, `ErrorDetail`."**
   (Line 55.) Incomplete: `schemas.py` also defines
   `IndexingStateCollectionEntry` (the per-collection sub-model used inside
   `IndexingStateResponse`). Minor but the doc explicitly enumerates.

8. **"Recent example, from `BREAKING.md`: the MCP `search` tool was changed
   from returning a bare list to returning `{"results": [...], "acl_filtered":
   bool}`."** (Line 49.) The BREAKING.md entry is currently under the
   `[next release]` heading, not yet released — "recent example" implies it
   has shipped. Verified against `BREAKING.md` line 11.

## Verified claims

- OpenAPI customisation in `_configure_openapi` declares a single
  `BearerAuth` (`type: http`, `scheme: bearer`) and walks every path attaching
  `security: [{BearerAuth: []}]` except `_EXEMPT_PATHS` —
  `server/app.py:46-76`.
- OpenAPI `version` is set to `_VERSION` resolved via
  `importlib.metadata.version("archon-search")` with fallback `"dev"` —
  `server/app.py:27-31, 55`.
- Bearer auth on every path except entries in `_EXEMPT_PATHS` is enforced by
  `APIKeyMiddleware` (`middleware_auth.py:25-53`). The only realistically
  exposed exempt path that appears in the OpenAPI schema is `/health`.
- The MCP tool list of nine tools (`search`, `search_with_context`,
  `ingest_file`, `ingest_directory`, `list_collections`,
  `get_collections_meta`, `get_collection_meta`, `list_documents`,
  `delete_document`) is correct — `server/mcp.py:30-228`.
- MCP shares the same `APIKeyMiddleware` with the same key from
  `load_or_generate_key()` — `server/mcp.py:248-251`.
- `schemas.py` carries the docstring "Pure data models — no business
  logic." — `schemas.py:1-4`. Same docstring in `schemas_telemetry.py:1-4`.
- Telemetry response models include `schema_version: int = 1` and `enabled:
  bool` — `schemas_telemetry.py:39-66`.
- Telemetry factory methods (`from_search_tool_result`, `from_route_response`,
  `from_error`) do NOT accept a `query` parameter — verified in
  `archon_search/telemetry/entry.py:84-145`. The structural privacy invariant
  is real.
- The MCP `search` tool returns `{"results": [...], "acl_filtered": bool}` —
  `server/mcp.py:59`. BREAKING.md records this change at line 11-14.
- No `/v1/` prefix exists; routes are mounted at top-level paths (verified by
  inspection of all `routes_*.py` and `app.py:140-147`).
- The OpenAPI `info.version` claim resolves from `importlib.metadata` — see
  the verified `app.py:27-31`.

## Unverifiable / ambiguous

- **"OpenAPI is authoritative … When code and OpenAPI disagree, the bug is in
  the code."** This is a policy statement, not a verifiable code claim.
  Reasonable, but undermined by the fact that two routes (`/search`,
  `/route`) declare no `responses=` map, so the OpenAPI schema's error
  contract for those endpoints is whatever FastAPI emits by default — i.e.
  the policy "OpenAPI is authoritative" is real, but the OpenAPI for those
  paths is impoverished compared to the rest of the API.
- **"`schema_version: int = 1`"** — verified for `StatsResponse` and
  `EntriesResponse`. `DisabledResponse` has only `enabled: bool = False` and
  no `schema_version`. The doc says "Telemetry response models … include
  `schema_version`"; whether `DisabledResponse` counts as a "telemetry
  response model" for the purposes of this rule is ambiguous.
- **"No per-route auth opt-outs and no admin-only paths."** No admin-only
  paths confirmed. "No per-route auth opt-outs" is true at the middleware
  level (auth is path-keyed, not route-decorator-keyed) — phrasing is
  accurate but worth noting that path-based exemption via `_EXEMPT_PATHS` is
  itself the only opt-out mechanism.

**Purpose**: State the API design principles that govern the REST and MCP surfaces of `archon-search`, the discipline around schemas, and how versioning and breaking changes are managed.
**Audience**: Maintainers changing the API surface, integrators consuming it, and reviewers evaluating proposed contract changes.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# API Design and Contracts

`archon-search` exposes two surfaces over the same internal pipeline: a FastAPI REST control plane and an MCP endpoint. They share auth, share Pydantic schemas where possible, and are governed by a single compatibility contract.

## Principles

1. **OpenAPI is authoritative.** The schema at `GET /openapi.json` is the source of truth for request and response shapes. `GET /docs` is the human explorer over the same schema. When code and OpenAPI disagree, the bug is in the code (or in the route signatures that generate the schema).
2. **MCP and REST share internal services, but neither mirrors the other.** Both surfaces wrap the same internal pipeline, but their tool/route lists overlap rather than match. Five MCP tools have no REST counterpart (`search_with_context`, `ingest_file`, `ingest_directory`, `list_documents`, `delete_document`); REST exposes `/route`, `/jobs/*`, `/status`, `/indexing-state`, and `/telemetry/*` with no MCP equivalent. The full MCP tool list is `search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`. New capability should be exposed on whichever surface its callers actually need; do not invent control-plane behaviour on only one surface when both make sense.
3. **Bearer auth on everything except `GET /health`.** There is exactly one exempt path, and the OpenAPI schema reflects that exemption explicitly.
4. **Schemas are Pydantic, schemas are typed, schemas have no logic.** Request and response models live in `server/schemas.py` and `server/schemas_telemetry.py`. They are pure data; behaviour lives in routes and services.
5. **Privacy invariants are structural, not procedural.** The "no raw query in telemetry" rule is enforced by the absence of a `query` parameter on telemetry entry factories — not by code review.

## OpenAPI is Authoritative

`server/app.py` overrides FastAPI's default OpenAPI generation with `_configure_openapi`. The customisation does two things:

- Declares a single `BearerAuth` security scheme (`type: http`, `scheme: bearer`).
- Walks every path operation and attaches `security: [{BearerAuth: []}]` — except for paths in `_EXEMPT_PATHS`. `_EXEMPT_PATHS` is a `frozenset[str]` of paths (`/health`, `/docs`, `/openapi.json`, `/redoc`), so the exemption is path-scoped, not method-scoped: every HTTP method on `/health` is exempt, not only `GET`. The remaining three are defensive entries that FastAPI never includes in the schema anyway.

The `version` field of the OpenAPI document is the package version resolved through `importlib.metadata`. It tracks the CalVer release; it is not separately maintained.

**Consequences:**

- Adding an endpoint without a Pydantic response model means OpenAPI loses fidelity. Every new route should declare `response_model=` and an explicit `responses=` map for non-200 cases. This is a target convention, not the current norm: `POST /search` and `POST /route` declare only `response_model=` today, so their non-200 error contract in OpenAPI is whatever FastAPI emits by default.
- Adding a new exempt path is a security decision, not a documentation one. Update `_EXEMPT_PATHS` in `middleware_auth.py` and document the exemption in `150_security_and_privacy_architecture.md`.

## Authentication

Authentication is a single middleware: `APIKeyMiddleware` in `server/middleware_auth.py`. Every request to every path is checked against the configured API key (loaded by `key_manager.load_or_generate_key`), with the sole exception of `GET /health`. There are no per-route auth opt-outs and no admin-only paths.

The key itself is bootstrapped by `key_manager.py` (auto-generated at `~/.archon-search/.search.env`, mode `600`, on first start). `ARCHON_SEARCH_API_KEY` overrides the file; `ARCHON_SEARCH_KEY_FILE` redirects it. See `150_security_and_privacy_architecture.md` for the full auth model.

## MCP and REST: Two Surfaces, Shared Internals

`server/mcp.py` exposes an MCP endpoint that uses the same `APIKeyMiddleware` and wraps the same internal services as the REST routes. The two surfaces overlap but do not mirror each other:

- **MCP tools** (nine total, from `server/mcp.py`): `search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`.
- **REST routes** (from `routes_*.py`): `GET /health`, `GET /status`, `GET /indexing-state`, collection ops on `/{name}` (`GET`/`POST`/`DELETE`) plus `POST /{name}/reindex`, `POST /ingest`, `GET /jobs/{job_id}`, `DELETE /jobs/{job_id}`, `POST /search`, `POST /route`, `GET /telemetry/stats`, `GET /telemetry/entries`.

MCP-only (no REST counterpart): `search_with_context`, `ingest_file`, `ingest_directory` (REST exposes only the async job-based `POST /ingest`, not these synchronous per-path tools), `list_documents`, `delete_document`. REST-only (no MCP counterpart): `/route`, `/jobs/*`, `/status`, `/indexing-state`, `/telemetry/*`.

When adding capability, prefer this order:

1. Decide which surface(s) the capability belongs on. If a behaviour makes sense on both REST and MCP, expose it on both; if it is genuinely MCP-only or REST-only (as several existing endpoints are), say so in the PR description.
2. Add the REST endpoint with a Pydantic response model, and/or add the MCP tool in `mcp.py` that wraps the same service call.
3. Document both in `BREAKING.md` if either changes an existing surface.

When a behaviour exists on both surfaces, divergence in shape between them is a contract bug. Example, currently logged in `BREAKING.md` under `[next release]` (unreleased at time of writing): the MCP `search` tool was changed from returning a bare list to returning `{"results": [...], "acl_filtered": bool}` so that the MCP response carries the same ACL-filter signal already available on REST.

**A2 filter parity**: A2 added `SearchFilters` as a shared model across both surfaces. REST receives it as `SearchRequest.filters` (a nested object); MCP receives the same fields as individual tool kwargs (`file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`, `include_metadata`) that are assembled into a `SearchFilters` instance inside the tool handler. The parity contract is verified by `tests/server/test_mcp_search.py`.

## Schema Discipline

Request and response shapes that cross the API boundary are Pydantic `BaseModel` subclasses. Most live in two centralised files; two routers currently keep their schemas inline:

- `archon_search/server/schemas.py` — control-plane and collection responses: `HealthResponse`, `StatusCollectionEntry`, `StatusResponse`, `IndexingStateCollectionEntry`, `IndexingStateResponse`, `CollectionSummary`, `CollectionDetail`, `JobResponse`, `DeleteResponse`, `ErrorDetail`.
- `archon_search/server/schemas_telemetry.py` — telemetry-specific shapes: `StatsResponse`, `EntriesResponse`, `DisabledResponse`, plus the sub-models `LatencyPercentiles`, `EndpointStats`, `CollectionStats`, `ErrorBreakdown`.
- Inline in `archon_search/server/routes_search.py`: `SearchRequest`, `SearchResultSchema`, `SearchResponse`.
- `archon_search/filters.py`: `SearchFilters` — the shared filter model used by both REST (`SearchRequest.filters`) and MCP (`search` / `search_with_context` tool kwargs). It lives outside `schemas.py` and `_types.py` deliberately: `_types.py` is dataclass-only by convention (no Pydantic), and `SearchFilters` is reused across the REST and MCP surfaces without being a server schema itself.
- Inline in `archon_search/server/routes_route.py`: `RouteRequest`, `RouteResponse`.

The inline schemas in the search and route routers are the current reality; consolidating them into `schemas.py` is a possible future cleanup but is not a hard rule today.

Rules:

- **No business logic in schemas.** `schemas.py` and `schemas_telemetry.py` both carry the docstring "Pure data models — no business logic." Validation derives from types; transformations happen in route handlers or services. The inline models in `routes_search.py` and `routes_route.py` follow the same convention.
- **Routes declare `response_model=` explicitly.** This is what populates the OpenAPI schema with typed response bodies.
- **Errors should use `ErrorDetail`.** The intended error envelope is `{"detail": str}`, declared via `responses={401: {"model": ErrorDetail}, ...}`. This is aspirational rather than enforced: `POST /search` and `POST /route` do not currently declare a `responses=` map at all, so their error envelopes come from FastAPI defaults. Do not invent new ad-hoc error shapes; prefer wiring `ErrorDetail` into new routes.

### The structural privacy invariant

Telemetry response models in `schemas_telemetry.py` carry an `enabled: bool` flag; `StatsResponse` and `EntriesResponse` additionally include `schema_version: int = 1` (`DisabledResponse` carries only `enabled: bool = False`). None of them include a raw query field. This is mirrored on the write side: the factory methods in `archon_search/telemetry/entry.py` do not accept a `query` parameter. The absence of that parameter is the invariant.

If a future change appears to need a `query` argument on a telemetry factory, the answer is "no". The invariant is structural so it cannot be eroded by a well-meaning patch. See `150_security_and_privacy_architecture.md` for the privacy rationale.

## API Versioning

The API version equals the package version. There is no `/v1/` prefix and no parallel-version surface.

- The OpenAPI document's `info.version` is the CalVer release (`YY.M.<rev-count>`), populated from `importlib.metadata.version("archon-search")`.
- CalVer encodes time, not compatibility. A new month does not mean a breaking change; a tag in the same month can still break consumers.
- **Breaking changes are documented in `BREAKING.md`**, with the surface (REST or MCP), the change, and the migration path. This is the contract; the version string is not.

If a future release ever introduces a parallel-version surface (e.g. `/v2/`), the rationale and migration window must be recorded in `BREAKING.md` before the new surface is shipped.

## Adding or Changing an Endpoint

The expected sequence:

1. Define or extend a Pydantic model in `schemas.py` (or `schemas_telemetry.py` for telemetry). New shared shapes should land in the centralised files; only keep schemas inline when there is a specific reason (matching the precedent in `routes_search.py` / `routes_route.py`).
2. Add the route under `archon_search/server/routes_*.py` with `response_model=` and, where practical, an explicit `responses=` map for error cases using `ErrorDetail`.
3. If the endpoint should also be reachable over MCP, add a tool in `mcp.py` that wraps the same service call. Conversely, if the capability is genuinely MCP-only (e.g. a per-path synchronous tool like `ingest_file`) or REST-only (e.g. `/route`, `/jobs/*`), document the asymmetry in the PR rather than forcing parity.
4. Write tests first (REST and/or MCP depending on which surfaces the change touches, plus success + auth-rejected + error paths). See `200_testing_strategy.md`.
5. If the change alters an existing contract — request shape, response shape, status codes, path — add a `BREAKING.md` entry in the same PR.

The full enumerated list of currently-exposed routes lives in `600_api_reference_or_public_interface.md`. This document deliberately does not duplicate it; the route map should have exactly one home.

## Related Documents

- Endpoint catalogue and MCP tool list: `600_api_reference_or_public_interface.md`
- Auth bootstrap, key rotation, ACL, telemetry privacy: `150_security_and_privacy_architecture.md`
- Test layout for routes, schemas, and MCP parity: `200_testing_strategy.md`
- Release and versioning mechanics: `510_release_and_environment_strategy.md`
- Day-to-day development workflow: `500_development_workflows_and_conventions.md`
- Compatibility log: `../../BREAKING.md`

**Purpose**: Authoritative human-readable reference for every public surface (REST, MCP, CLI) exposed by `archon-search`.
**Audience**: Medior engineers integrating with or operating `archon-search`.
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2026-08-20

# API Reference and Public Interface

## Guiding principles

1. **Source of truth is code, not prose.** Every row in this document traces to a route module under `archon_search/server/` or a file under `archon_search/cli/`. The machine-readable contract is `GET /openapi.json`; this page is its narrative companion.
2. **One auth model everywhere.** Both REST and MCP run through the same `APIKeyMiddleware` (`archon_search/server/middleware_auth.py`). Only `GET /health` is unauthenticated. Note: the MCP transport is constructed with an empty `namespaces={}` dict (`mcp.py::create_mcp_http_app`), so only the bootstrap default key authenticates on `/mcp` — per-namespace keys configured in `[namespaces]` are not accepted there.
3. **Breaking changes go in `BREAKING.md`.** CalVer segments do not encode compatibility; consult [`/BREAKING.md`](../../BREAKING.md) before upgrading.
4. **No raw queries on the wire.** Telemetry never stores query strings; this is a structural invariant — see [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md).
5. **Namespaces are enforced server-side (REST).** Every authenticated REST request resolves to a namespace; cross-namespace access yields `404`, never `403`. MCP tools currently do not apply namespace gating to pipeline calls (`mcp.py` invokes `pipeline.search(...)` without a `namespace=` argument); this is an asymmetry with the REST surface. #Unverified — intentional vs. drift not documented in code

## Authentication

- All endpoints except `GET /health` require an `Authorization: Bearer <token>` header.
- The token is checked against (a) per-namespace keys configured in `[namespaces]` and (b) the bootstrapped default key from `~/.archon-search/.search.env` (override via `ARCHON_SEARCH_API_KEY` / `ARCHON_SEARCH_KEY_FILE`).
- Comparison uses `secrets.compare_digest` against every entry — no early exit — to prevent timing leakage (`middleware_auth.py`).
- Exempt paths (`middleware_auth.py::_EXEMPT_PATHS`): `/health`, `/docs`, `/openapi.json`, `/redoc`. Only `/health` actually appears in the OpenAPI schema; the others are defensive (FastAPI never includes them).
- On success, the resolved namespace is attached to `request.state.namespace` and used for filtering by every handler.
- Failure responses return `401` with `WWW-Authenticate: Bearer`.

## REST endpoints

The machine-readable contract is `GET /openapi.json`. Tables below trace every endpoint back to its route module under `archon_search/server/`.

### `routes_health.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/health` | Liveness probe; unauthenticated. | — | `HealthResponse` (`schemas.py`) — `{status, version}` |

### `routes_state.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/indexing-state` | Raw indexing state filtered to caller's namespace; when no state file exists, returns a populated `IndexingStateResponse` with `collections={}`, `last_updated=null`, `trigger=null` (not an empty body). | — | `IndexingStateResponse` (`schemas.py`) |

### `routes_status.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/status` | Operator-facing service status: PID, version, per-collection progress, ETA (namespace-filtered). | — | `StatusResponse` (`schemas.py`) |

### `routes_search.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/search` | Hybrid vector + FTS search over one collection, rerank, ACL filter. | `SearchRequest` (`routes_search.py`) — `{collection, query, top_k, filters?}`. **`top_k` is ignored** at runtime (see BREAKING.md); pipeline uses `config.top_k_return`. The field is still validated (`ge=1, le=100`), so `top_k=0` or `top_k>100` returns `422`. `collection` and `query` are stripped and must be non-empty. `filters` is an optional `SearchFilters` object (see below); omitting it is equivalent to `null`. | `SearchResponse` — `{results: [SearchResultSchema{doc_id, chunk_id, text, score, source_path, file_type, language, indexed_at, updated_at, ingested_by, metadata, acl}], acl_filtered: bool}` |

Returns `404` when the collection is not visible to the caller's namespace; `503` when meta lookup fails. Pipeline `search()` exceptions are caught and returned as `200` with `{results: [], acl_filtered: false}` (i.e. errors after the meta check do not surface to the client). A malformed `filters` object returns `422`.

#### `SearchFilters` (A2 — `archon_search/filters.py`)

Optional sub-model on `SearchRequest.filters`. All fields are optional; omitting `filters` entirely or setting it to `null` runs an unfiltered search. Model uses `extra="forbid"` — unknown keys return `422`.

| Field | Type | Constraints | Behavior |
| --- | --- | --- | --- |
| `file_type` | `str \| null` | Non-empty; leading dots stripped; lowercased. e.g. `"md"`, `".py"` | SQL `WHERE file_type = '<value>'` |
| `source_path_prefix` | `str \| null` | Non-empty | SQL `WHERE source_path LIKE '<prefix>%' ESCAPE '\\'` |
| `source_path_glob` | `str \| null` | Non-empty; must compile via `fnmatch.translate` | Python-side post-filter via `fnmatch.fnmatchcase`. **No path semantics**: `*` matches `/`; `**` is identical to `*`. |
| `indexed_after` | `datetime \| date \| null` | Date-only strings `YYYY-MM-DD` coerced to midnight UTC | SQL `WHERE indexed_at >= '<fixed-width UTC>'` |
| `indexed_before` | `datetime \| date \| null` | Date-only strings `YYYY-MM-DD` coerced to end-of-day UTC (23:59:59.999999Z). Must be ≥ `indexed_after`. | SQL `WHERE indexed_at <= '<fixed-width UTC>'` |
| `language` | `str \| null` | **Reserved — always rejected with 422 when non-empty.** Roadmap item C2. | Not implemented in v1. |
| `include_metadata` | `bool` | Default `false` | When `false`, `metadata` is returned as `{}` in results; when `true`, the stored `dict[str,str]` is returned. |

**Date-range correctness note**: date-range filters compare against the `indexed_at` column, which is stored as a fixed-width UTC string (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Legacy rows with variable-precision timestamps may not sort correctly until `archon-search collection reindex-metadata <name> --normalize-timestamps` runs (see BREAKING.md A2 entry).

### `routes_route.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/route` | Centroid-based collection routing; produces a `pre_context` block for the caller's decomposer. | `RouteRequest` (`routes_route.py`) — `{query, slots?}` | `RouteResponse` — `{pre_context, pinned_names, routable_names, decomposer_invoked}` |

Errors: `400` empty query / `slots < 1`; `504` on 30 s routing timeout.

### `routes_collections.py`

All paths under `/collections`. Namespace gating: cross-namespace access surfaces as `404`.

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/collections/` | List collections visible to caller's namespace. | — | `list[CollectionSummary]` (`schemas.py`) |
| POST | `/collections/` | Register a path as a collection and enqueue first ingest (returns `202`). Returns `409` for (a) duplicate resolved path or (b) name collision across namespaces. | `AddCollectionRequest` — `{path}` | `JobResponse` (`schemas.py`) |
| GET | `/collections/{name}` | Full detail for one collection (centroid presence, ACL counts, last indexed). | — | `CollectionDetail` (`schemas.py`) |
| DELETE | `/collections/{name}` | Remove collection (config + LanceDB). `404` for unknown or cross-namespace name; `409` if pinned-only. | — | `DeleteResponse` (`schemas.py`) |
| POST | `/collections/{name}/reindex` | Start a reindex job (returns `202`). | — | `JobResponse` |

### `routes_jobs.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/ingest` | Submit an ingest job; returns `202` immediately. The `ingested_by` field in the body is always overwritten server-side from the `X-Ingested-By` header (missing → `"http"`; legacy `"archon-search-cli"` → `"cli"`; unknown → `"http"` + WARNING log; truncated to 32 chars). `collection` is rejected only when empty string (whitespace-only is currently accepted; inconsistent with `SearchRequest`). | `IngestRequest` — `{collection, path?, documents?, ingested_by (overwritten)}` | `JobResponse` |
| GET | `/jobs/{job_id}` | Read job status; `404` for cross-namespace IDs. | — | `JobResponse` |
| DELETE | `/jobs/{job_id}` | Cancel a job. Terminal jobs (`DONE`/`FAILED`/`CANCELLED`) return `200` (idempotent); `RUNNING`/`PENDING` transition to `CANCELLING` and return `202`; already-`CANCELLING` jobs also return `202`. | — | `JobResponse` |

### `routes_telemetry.py`

When telemetry is disabled, both endpoints return `DisabledResponse` (`schemas_telemetry.py`) — `{enabled: false}`.

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/telemetry/stats` | Aggregated stats over a date window. Returns `400` on invalid date ordering (from `reader.resolve_dates`). | Query: `since`, `until` | `StatsResponse` \| `DisabledResponse` (`schemas_telemetry.py`) |
| GET | `/telemetry/entries` | Paginated raw entries with filters. Returns `400` on invalid date ordering. | Query: `since`, `until`, `collection`, `endpoint`, `status`, `error_kind`, `offset`, `limit` (1..200) | `EntriesResponse` \| `DisabledResponse` |

## MCP tools

Defined in `archon_search/server/mcp.py` via `FastMCP`. The HTTP transport mounts at `/mcp` and is wrapped with the same `APIKeyMiddleware`; only `/health` is exempt.

| Tool name | Purpose | Arguments | Returns |
| --- | --- | --- | --- |
| `search` | Hybrid vector + FTS search, rerank, ACL filter. | `query: str`, `collection: str \| None`, `include_metadata: bool = false`, `file_type: str \| None`, `source_path_prefix: str \| None`, `source_path_glob: str \| None`, `indexed_after: str \| None`, `indexed_before: str \| None`, `language: str \| None` (reserved — non-empty raises validation error) | `{"results": [SearchResult...], "acl_filtered": bool}` — new shape per `BREAKING.md`. On validation error: `{error, code: "validation_error"}`. On internal error: `{error, code: "internal_error"}`. |
| `search_with_context` | Search plus surrounding chunks for each hit. | `query`, `collection?`, `context_window: int = 1`, `include_metadata: bool = false`, `file_type: str \| None`, `source_path_prefix: str \| None`, `source_path_glob: str \| None`, `indexed_after: str \| None`, `indexed_before: str \| None`, `language: str \| None` (reserved) | `list[{result, context_before, context_after}]`. On error: `{error, code}`. |
| `ingest_file` | Ingest one file. | `path: str`, `collection?` | Ingest result dict |
| `ingest_directory` | Ingest a directory tree (reports progress via `ctx`). | `path`, `glob_pattern = "**/*"`, `collection?` | `list[ingest result]` |
| `list_collections` | List collections with counts (centroid omitted). | — | `list[dict]` — `asdict(CollectionMeta)` with `centroid` popped (not a typed `CollectionMeta`). |
| `get_collections_meta` | Full meta for all collections including centroid. | — | `list[CollectionMeta]` |
| `get_collection_meta` | Full meta for one collection. | `name: str` | `CollectionMeta` or `{error, code: "not_found"}` |
| `list_documents` | List documents in a collection. | `collection?`, `limit: int = 100` | `list[doc dict]` |
| `delete_document` | Delete all chunks for one document. | `doc_id: str`, `collection?` | `{"deleted": int}` |

**Breaking-change note (from [`/BREAKING.md`](../../BREAKING.md)):**

- `search` returns `{"results": [...], "acl_filtered": bool}` — no longer a bare list. Consumers must access `response["results"]`.
- REST `/search` per-request `top_k` is now ignored; configure `[search] top_k_return` instead.

The REST control plane and the MCP tool surface are served by the same FastAPI app and share auth. The REST endpoints above and the MCP tools in this table are intentionally not 1:1 — MCP exposes ingest/list/delete document operations, REST exposes the job-oriented control plane.

## CLI commands

Entry point: `archon-search` (`archon_search/cli/main.py`, Click group). Most subcommands accept `--config <path>`; the exceptions are `stop` and `status`, which use fixed service identity and do not accept `--config`.

| Command | Subcommand | Purpose | Key flags |
| --- | --- | --- | --- |
| `start` | — | Validate config, then start the OS service (`cli/start.py`). | `--config` |
| `stop` | — | Stop the OS service; identity is fixed (`cli/stop.py`). | — |
| `status` | — | Show running/stopped, PID, uptime (`cli/status.py`). | — |
| `install` | — | Create default config if absent, register and start service, poll `/health` until ready (`cli/install_cmd.py`). Aborts with exit code 1 if `/health` does not respond within `_HEALTH_TIMEOUT = 60` seconds. | `--dry-run`, `--non-interactive`, `--config` |
| `uninstall` | — | Stop and unregister service; optionally delete the database directory (`cli/install_cmd.py`). | `--delete-db`, `--config` |
| `ingest` | — | Ingest a directory into a collection synchronously (`cli/ingest.py`). Defaults to `~/.archon-search/history/sessions`. | `--path`, `--collection`, `--config` |
| `sync` | — | Run `SearchCollectionSync` over all pinned + configured collections (`cli/sync.py`). | `--config` |
| `collection` | `list` | List collections from the store (`cli/collection.py`). | `--config` |
| `collection` | `add <path>` | Persist path in config and ingest. | `--config` |
| `collection` | `remove <path>` | Drop collection from store and config; rejects pinned-only. Note: `--force` is currently only enforced as mutually exclusive with `--dry-run`; despite its help text ("Proceed even if service is running"), no service-running check exists in `cli/collection.py::remove`, so the flag is effectively a no-op beyond the mutex. #Unverified — intentional behaviour vs. unimplemented check | `--dry-run`, `--force`, `--config` |
| `collection` | `info <name>` | Print collection metadata. | `--config` |
| `collection` | `reindex <name>` | Clear state, drop table, re-ingest from source path. | `--config` |
| `collection` | `reindex-metadata <name>` | Backfill metadata fields (`file_type`, `updated_at`, `ingested_by`) on an existing collection without re-ingesting. When `--normalize-timestamps` (default ON) rewrites `indexed_at` and `updated_at` to fixed-width UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ`) for any row not already in canonical form — required before date-range filters return correct results on pre-A2 collections. `--dry-run` reports counts without writing. Introduced in A2. | `--normalize-timestamps / --no-normalize-timestamps`, `--dry-run`, `--config` |
| `config` | `show` | Print effective config (defaults when no file exists) (`cli/config_cmd.py`). | `--config` |
| `config` | `get <section.field>` | Read one dotted key. Requires exactly a two-part `section.field` key; other formats error out. | `--config` |
| `config` | `set <section.field> <value>` | Write one dotted key (bool/int/float coercion). | `--config` |

## Authoritative contract

`GET /openapi.json` is the binding machine-readable contract. The OpenAPI schema is built in `archon_search/server/app.py::_configure_openapi`; it injects the `BearerAuth` security scheme and applies it to every non-exempt path. If this document diverges from `/openapi.json`, the schema wins — and a follow-up doc fix is required.

## Related documents

- [`520_api_design_and_contracts.md`](./520_api_design_and_contracts.md) — design rules behind these surfaces.
- [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) — auth, namespaces, ACL semantics.
- [`140_error_handling_strategy.md`](./140_error_handling_strategy.md) — status code conventions.
- [`/BREAKING.md`](../../BREAKING.md) — compatibility contract.
- [`990_documentation_index_and_contribution_guide.md`](./990_documentation_index_and_contribution_guide.md) — index of all documentation.

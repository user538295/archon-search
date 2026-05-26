# BREAKING CHANGES

## Compatibility Policy

`archon-search` uses CalVer (`YY.M.<commit-count>`). CalVer segments encode **time only** — they do not signal compatibility. This file IS the compatibility contract.

**Rule**: every release that removes or changes an existing API contract MUST add an entry here describing: what changed, the migration path, and from which release the deprecated form was announced. Consumers should subscribe to changes in this file, not interpret CalVer segments.

## Changelog

### [next release] — B2 (additive): `GET /ready` endpoint and `readiness` field on `GET /status`

**Surface**: REST (additive — new endpoint and new field on existing endpoint).

**Changes**:
- **New `GET /ready` endpoint** (unauthenticated) — returns `{"ready": bool, "checks": {"storage": "ok"|"fail"}}`. HTTP 200 when the storage layer is connected; HTTP 503 with the same `ReadinessResponse` body when not. This is a readiness probe, not a liveness probe — `/health` (`{status, version}`) remains the liveness signal. Both endpoints are unauthenticated and listed in `_EXEMPT_PATHS`.
- **New `readiness` field on `GET /status` response** — `StatusResponse` gains an optional `readiness: ReadinessDetail | None` sub-object containing `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs` (pending/running counts), `collections_indexing`, `collections_failed`, and `watcher.running`. This field is absent (`null`) when the server cannot populate it; tolerant consumers require no migration.

Both changes are **additive and backward-compatible**. Existing consumers of `GET /health`, `GET /status`, or any other endpoint are unaffected.

**Announced in**: this release.

### [next release] — B1 observability: `stage_timings_ms` on `POST /explain` and MCP `explain`

**Surface**: REST `POST /explain` (additive new field), MCP `explain` tool (additive new field in returned dict).

**Change**: When `[observability].stage_timings_enabled = true` (the default), the `ExplainResponse` returned by `POST /explain` and the `explain` MCP tool gains a `stage_timings_ms: dict[str, float]` field containing per-stage blocked-coroutine wall times in milliseconds. When `stage_timings_enabled = false`, the field is absent entirely (not `null`).

This is a **new optional field**; it does not appear on any other endpoint or MCP tool.

**Affected clients**:
- Tolerant JSON consumers (e.g., `response.json()["stage_timings_ms"]` with a `.get()` fallback): no migration needed.
- Pydantic models or other strict-schema validators with `extra="forbid"` on the `ExplainResponse` shape will **reject** responses when timings are enabled. Either relax the model or set `stage_timings_enabled = false` in `archon-search.toml`.

**Migration**:
- To suppress the field: set `[observability] stage_timings_enabled = false` in `~/.archon-search/archon-search.toml`.
- To tolerate it in strict validators: add `stage_timings_ms: dict[str, float] | None = None` to your client-side schema.

**Announced in**: this release.

### [next release] — A2 query-side filters

**Surface**: REST (additive, non-breaking for tolerant JSON consumers); MCP (additive, non-breaking — all new params are optional with sensible defaults).

**REST — additive**:
- `POST /search` request body gains an optional `filters` field (`SearchFilters` object, `null` = no filter). Existing clients that omit `filters` are unaffected.
- `SearchFilters` fields: `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language` (reserved — non-empty raises 422), `include_metadata`.
- `language` with a non-empty value is rejected with HTTP 422 at validation time. Roadmap item C2.

**MCP — additive**:
- `search` tool gains optional kwargs: `include_metadata` (bool, default `false`), `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`. All default to `None`/`false`; existing callers passing only `query` and `collection` are unaffected.
- `search_with_context` tool gains the same optional kwargs.
- A `language` value that is non-empty returns `{error: ..., code: "validation_error"}` instead of raising.

**Operational — datetime normalization**:
- Date-range filters (`indexed_after`, `indexed_before`) compare lexicographically against the `indexed_at` column using fixed-width UTC format (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Collections indexed before A2 may contain variable-precision timestamps that produce incorrect date-range results. Run `archon-search collection reindex-metadata <name> --normalize-timestamps` (introduced in this release) to rewrite all rows to the fixed-width format. This command is offline-friendly and blocks only concurrent ingest to the same collection.

**Migration**:
- REST: no migration needed for existing clients — `filters` is optional.
- MCP: no migration needed — new kwargs are optional with defaults.
- Operators using date-range filters on pre-A2 collections: run `reindex-metadata --normalize-timestamps` before relying on date-range filter results.

**Announced in**: this release.

### [next release] — A4 explain endpoint (purely additive)

**Surface**: REST (new endpoint), MCP (new tool), telemetry enum, internal diagnostics, routing.

**Change**: entirely additive — no existing API surface is modified.

- **New `POST /explain` REST endpoint** — returns per-stage retrieval/reranking score breakdown plus optional routing decision. Authenticated identically to all other REST endpoints. All schemas use `extra="forbid"`; unknown request fields produce `422`.
- **New `explain` MCP tool** (10th tool) — same response structure as REST; returns `ExplainResponse.model_dump(mode="json", exclude_none=False)`. When `config` is absent from `create_app`, collectionless calls fall back to `default_collection`.
- **New `EndpointKind.explain` enum value** in `telemetry/entry.py` — additive to the `StrEnum`; no existing `EndpointKind` value is changed or removed.
- **New optional fields on `ScoredSearchCandidate`** (`_diagnostics.py`) — `acl: list[str] | None = None` plus A1/A2 metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, `metadata`). All defaulted; existing construction sites are unaffected.
- **Determinism improvement on `MultiCollectionRouter.rank()`** — equal-similarity entries now have a stable ascending-`name` tie-break. Previously the order was undefined for tied scores. This is a determinism fix, not a breaking change; callers that relied on undefined ordering may observe a different (now stable) ordering.

**Migration**: none required. All changes are purely additive. Existing `/search`, `/route`, and MCP tool consumers are unaffected.

### [next release] — A1 metadata schema v1

**Surface**: MCP (breaking for strict-validating clients), REST (additive, non-breaking for tolerant JSON consumers).

**MCP — truly breaking**:
- `search` tool: response result items gain `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, `acl`.
- `search_with_context` tool: result items gain the same six keys; `context_before` / `context_after` items gain `file_type`, `updated_at`, `ingested_by`, `metadata`, and **no longer include `vector`** (raw embeddings were never useful at the MCP boundary and inflated payload size by `~dim*4` bytes per neighbor).
- `list_documents`: items gain the same six keys when sourced from rows that carry them.

A1 is the **last** untyped MCP shape break before C7 wraps responses in Pydantic models.

**REST — additive (non-breaking)**:
- `/search` result items gain the same six keys. Tolerant JSON consumers see new fields appear; strict-schema consumers (e.g., generated clients pinned to the older OpenAPI snapshot) must regenerate. (There is no `/search/context` REST endpoint; the equivalent capability is the MCP `search_with_context` tool, listed separately in the MCP section above.)

**New 503 contract on `/ingest` and `/collections` (A5c)**:
- During an active reindex of the same collection, the store may raise `StoreBusyError` after a 30s lock-acquisition timeout. HTTP `POST /ingest` and `POST /collections` now return HTTP 503 with `Retry-After: 30` and `{"error": "store_busy", ...}` synchronously; ingest into a different collection succeeds normally. MCP `ingest_file` and `ingest_directory` surface `StoreBusyError` synchronously as `McpErrorResponse(code="store_busy")`. (A5c closes A1's deferred 503 surface.)

**`X-Ingested-By` header normalization**:
- Missing/empty → `"http"`.
- Canonical values (`cli`, `http`, `watcher`, `reindex`) pass through.
- Legacy `"archon-search-cli"` is normalized to `"cli"` at the boundary — clients that pass legacy and inspect the stored value will now see `"cli"`.
- Unknown values are coerced to `"http"` with a WARNING log (value truncated to 32 chars).

**Migration**:
- MCP consumers: regenerate types or relax strict-mode validation; the new keys are additive on every response item, never replace existing ones.
- REST consumers using tolerant JSON parsing: nothing to do. Strict-typed clients: regenerate from the updated OpenAPI.
- Existing collections: pre-A1 rows continue to read as-is via the read-boundary normalizer (`ingested_by` legacy → `"cli"`, empty `file_type` → `""`, `updated_at` falls back to `indexed_at`). To populate real values on pre-A1 rows, run `archon-search collection reindex-metadata <name>` (offline-friendly, blocks only `/ingest` to the same collection).

**Announced in**: this release. No prior deprecation — the impacted MCP shape was never documented as stable.

### [next release] — `POST /search` pipeline-exception behavior (CON-5 / A3)

**`POST /search` pipeline-exception behavior** (`routes_search.py`, CON-5 / A3):
- Pipeline stage exceptions (embedder, store, reranker) now return **HTTP 500** with a **plain-text** body `Internal Server Error` (Content-Type `text/plain`) instead of HTTP 200 with `{results: [], acl_filtered: false}`. The route bare-re-raises the exception; Starlette's `ServerErrorMiddleware` produces the default text response — this is **not** a JSON envelope. Callers MUST NOT call `.json()` on the 500 response body; doing so will raise a JSON decode error.
- A hung pipeline call now returns **HTTP 504** with `{"detail": "Search timed out"}` after ~30 s (matching the `/route` timeout contract).
- Migration: callers that treated an empty-results 200 as a pipeline-error signal must now handle HTTP 5xx. Callers that already treat 5xx as errors are unaffected.
- The `503` meta-lookup path (`get_collection_meta` raises) is unchanged.
- MCP `search` / `search_with_context` tools are unchanged.

### [next release] — A5a ingest path safety

**Surface**: MCP (behaviour change), REST (additive).

- MCP `ingest_file` and `ingest_directory` previously accepted paths containing `..` segments, empty strings, whitespace-only strings, NUL bytes, and non-absolute paths, and silently followed/resolved them. They now reject those inputs and return `McpErrorResponse(error=..., code="path_unsafe")` with an LLM-readable reason.
- HTTP `POST /collections` and `POST /jobs/ingest` gain a new `400` response (`ErrorDetail`, `detail` prefixed `"path is unsafe:"`) for the same input classes — additive (the `400` was not previously in the OpenAPI schema).

**Migration**: callers must pass absolute paths without `..` traversal. `path: null` (documents-only) ingest on `POST /jobs/ingest` is unaffected. Symlinks and absolute-path scope are intentionally NOT validated (deferred to a future `allowed_dirs` feature).

**Announced in**: this release. No prior deprecation — the silent-acceptance behaviour was never documented as stable.

### [next release] — MCP `search` tool response shape

**Surface**: MCP (`mcp.py` `search` tool)
**Change**: `search` tool now returns `{"results": [...], "acl_filtered": bool}` instead of `[{...}, {...}]` (bare list of result dicts).
**Migration**: Update consumers to access `response["results"]` instead of iterating the response directly. `response["acl_filtered"]` provides the ACL filter flag previously unavailable on the MCP surface.
**Announced in**: this release (no prior deprecation period — the old shape was never documented as stable).

### [next release] — REST `/search` per-request `top_k` no longer honored

**Surface**: REST (`/search` POST)
**Change**: The `top_k` field in `SearchRequest` is now ignored at the route level; the pipeline uses `config.top_k_return` instead. Previously, each request could specify its own `top_k`.
**Migration**: Configure `[search] top_k_return` in `archon-search.toml` to set the desired result count.
**Announced in**: this release (the behavior was supported but never documented as stable).

# BREAKING CHANGES

## Compatibility Policy

`archon-search` uses CalVer (`YY.M.<commit-count>`). CalVer segments encode **time only** — they do not signal compatibility. This file IS the compatibility contract.

**Rule**: every release that removes or changes an existing API contract MUST add an entry here describing: what changed, the migration path, and from which release the deprecated form was announced. Consumers should subscribe to changes in this file, not interpret CalVer segments.

## Changelog

### [next release] — A1 metadata schema v1

**Surface**: MCP (breaking for strict-validating clients), REST (additive, non-breaking for tolerant JSON consumers).

**MCP — truly breaking**:
- `search` tool: response result items gain `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, `acl`.
- `search_with_context` tool: result items gain the same six keys; `context_before` / `context_after` items gain `file_type`, `updated_at`, `ingested_by`, `metadata`, and **no longer include `vector`** (raw embeddings were never useful at the MCP boundary and inflated payload size by `~dim*4` bytes per neighbor).
- `list_documents`: items gain the same six keys when sourced from rows that carry them.

A1 is the **last** untyped MCP shape break before C7 wraps responses in Pydantic models.

**REST — additive (non-breaking)**:
- `/search` and `/search/context` result items gain the same six keys. Tolerant JSON consumers see new fields appear; strict-schema consumers (e.g., generated clients pinned to the older OpenAPI snapshot) must regenerate.

**New 503 contract on `/ingest`**:
- During an active reindex of the same collection, the store may raise `StoreBusyError` after a 30s lock-acquisition timeout. The lifecycle wrapper surfaces this in job state today (REST 202 + background task model); a synchronous 503 + `Retry-After: 30` response is a follow-up tied to a request-lifecycle refactor.

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

### [next release] — REST `/search` pipeline errors now surface as 5xx (A3)

**Surface**: REST (`/search` POST)
**Change**: Previously, exceptions raised by `pipeline.search()` after a successful meta lookup were swallowed and returned as `HTTP 200` with `{results: [], acl_filtered: false}`. Clients could not distinguish "no hits" from "search broke" (`CON-5`). With A3:
- `asyncio.TimeoutError` (pipeline took > 30 s) → **`504 Gateway Timeout`** (`{"detail": "Search timed out"}`).
- Any other exception from `pipeline.search()` → **`500 Internal Server Error`** (exception re-raised, FastAPI default body).
- On both error paths, a telemetry entry is enqueued with `status="timeout"` / `status="internal_error"` and the matching `error_kind`. An ERROR log is emitted with `event_type="search_timeout"` or `event_type="search_pipeline_failure"`.

The 30-second timeout constant (`_SEARCH_TIMEOUT_SECONDS = 30.0`) is defined in `routes_search.py` and is not yet config-knob-exposed (tracked as a TODO in the same file).

**Migration**: Clients that matched on `HTTP 200` + `results: []` to detect pipeline failure must now handle `HTTP 500` and `HTTP 504`. Clients performing graceful-degradation on empty results are unaffected for the "no hits" case, but must be updated to not mistake `500`/`504` for "no hits". The `503` response for meta-lookup failure is unchanged.
**Announced in**: this release. The old silent-failure behavior was never documented as stable; the fix eliminates debt item `CON-5`.

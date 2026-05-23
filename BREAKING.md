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

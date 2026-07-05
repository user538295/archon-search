# BREAKING CHANGES

## Compatibility Policy

`archon-search` uses CalVer (`YY.M.<commit-count>`). CalVer segments encode **time only** — they do not signal compatibility. This file IS the compatibility contract.

**Rule**: every release that removes or changes an existing API contract MUST add an entry here describing: what changed, the migration path, and from which release the deprecated form was announced. Consumers should subscribe to changes in this file, not interpret CalVer segments.

## Changelog

### [next release] — E2d: graph table names are now namespace-scoped (`_archon_graph_{ns}__{col}_*`)

**Surface**: LanceDB internal graph tables (auxiliary storage); `archon-search graph build-communities` CLI; all `GET /graph/*` endpoints (read path).

**Breaking change**:

1. **All graph LanceDB tables renamed from `_archon_graph_{col}_*` to `_archon_graph_{ns}__{col}_*`** — the namespace is now embedded in the table name, separated from the collection name by a double underscore (`__`). Affected table suffixes: `_nodes`, `_edges`, `_communities` (E1b), `_mentions` (E2b). After upgrading to E2d, all existing graph tables (created under the old naming scheme) become **orphaned** — they are no longer referenced by the server and will not be read or written. The server does not automatically migrate or delete them.

2. **Namespace and collection name charset constraint** — collection names and namespace names must NOT contain consecutive underscores (`__`) and must NOT end with `_`. Names that violate these constraints are rejected with `ValueError` at ingest time. The constraint existed implicitly (the `__` separator requires it for unambiguous parsing); E2d makes it explicit and enforced at the store layer. Valid examples: `docs`, `tenant-a`, `ns_a`. Invalid examples: `docs_`, `my__col`, `tenant_`.

3. **Startup WARNING (BE-1b)** — after upgrade, the server will log a WARNING listing any LanceDB tables that match the old `_archon_graph_{col}_*` pattern (without a namespace prefix). These are orphaned tables from pre-E2d ingests.

**Impact**: all graph data (entities, edges, communities, mentions) is invisible after upgrade. Graph search modes (`naive`, `local`, `global`) return empty results until data is re-ingested.

**Remediation**:
1. After upgrading to E2d, delete old graph tables manually (use the `lancedb` Python client or LanceDB CLI to drop tables matching `_archon_graph_<collection>_nodes`, `_archon_graph_<collection>_edges`, etc.).
2. Re-ingest all collections with `[graph] enabled = true` to rebuild graph data under the new namespace-scoped table names.
3. If communities were built (`archon-search graph build-communities`), re-run that command after re-ingest.

**Deployments without `[graph] enabled = true`** are completely unaffected — no graph tables exist and no migration is required.

---

### [next release] — E2c: `GET /graph/{collection}` and `GET /graph/cross-collection` gain `salience_mode` field and namespace-scoped resolution

**Surface**: `GET /graph/{collection}` REST response (`GraphInspectionResponse`); `GET /graph/cross-collection` REST response (`CrossCollectionGraphInspectionResponse`); collection-resolution behaviour inside both handlers.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`GraphInspectionResponse` gains `salience_mode: Literal["frequency", "tfidf"] = "frequency"`** — always present in the JSON response. The default value `"frequency"` preserves the previous implicit behaviour. Clients using strict JSON schema validation (`extra="forbid"`) may reject the response if they have not updated their type stubs. Lenient clients are completely unaffected.

   Schema: `GraphInspectionResponse.salience_mode: Literal["frequency", "tfidf"] = "frequency"`.

   To opt into TF×IDF salience: add `?salience=tfidf` to the query string.

2. **`CrossCollectionGraphInspectionResponse` gains `salience_mode: Literal["frequency", "tfidf"] = "frequency"`** — same semantics as (1) above, for the cross-collection endpoint. Schema: `CrossCollectionGraphInspectionResponse.salience_mode: Literal["frequency", "tfidf"] = "frequency"`. Lenient clients are completely unaffected; strict-schema validators must add the field to their `CrossCollectionGraphInspectionResponse` type stubs.

**Behaviour changes**:

3. **`GET /graph/{collection}` now resolves collection names within the authenticated namespace only** — previously the handler resolved the collection against `DEFAULT_NAMESPACE` regardless of the caller's bearer token. After E2c, the handler passes `namespace=request.state.namespace` to `get_collection_meta`, so each caller can only inspect collections in their own namespace. Callers in non-default namespaces that previously received data from `DEFAULT_NAMESPACE` collections will now receive `404 collection not found`. Callers using the default namespace (single-tenant deployments) are unaffected.

4. **`GET /graph/cross-collection` now resolves collection names within the authenticated namespace only** — the same namespace fix applies to the cross-collection handler. The `?collections=` parameter is now filtered against the authenticated namespace: collection names that exist in other namespaces return `404 collection not found`. Additionally, when `?salience=tfidf` is used, the IDF denominator is scoped strictly to the authenticated namespace (`pipeline.get_all_collections_meta(namespace)`), preventing cross-namespace IDF leakage. Callers using the default namespace (single-tenant deployments) are unaffected.

**Migration**: no action required for tolerant JSON consumers or single-tenant deployments. Multi-tenant operators: tokens in non-default namespaces must now call both graph endpoints with a token scoped to the namespace that owns the collection(s). Strict-schema validators should add `salience_mode: "frequency" | "tfidf"` to their `GraphInspectionResponse` and `CrossCollectionGraphInspectionResponse` type stubs; regenerate from `GET /openapi.json`.

---

### [next release] — E2a: TTL and scoping — additive chunk columns, new endpoints, scope_filter, maintenance fields

**Surface**: `POST /ingest` and `POST /ingest/directory` request bodies; `PATCH /collections/{name}` request body; new `GET /collections/{name}/expiring` endpoint; `POST /search` and `POST /explain` request bodies; `GET /status` maintenance sub-object; `GET /collections/{name}/documents` response items; MCP tools `ingest_file`, `ingest_directory`, `search`, `search_with_context`, `explain`; LanceDB chunk-table and meta-table schemas; `STORE_SCHEMA_VERSION`.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`STORE_SCHEMA_VERSION` bumped `0` → `1`** — two new in-place migrations registered with `introduced_at=1`: `migrate_expires_at_and_scopes` (adds `expires_at: utf8 | null` and `scopes: list<utf8> | null` to every collection's chunk table) and `migrate_default_ttl_seconds` (adds `default_ttl_seconds: int64 | null` to `_archon_collection_meta`). These migrations are NOT triggered at server startup — operators must run `POST /collections/{name}/migrate` for each collection after upgrading to E2a before using TTL or scopes features. Until migrated, TTL and scope data is silently omitted (the store detects un-migrated columns and omits the keys from ingest rows entirely; the columns are absent, not stored as null). `GET /collections/{name}/migrations/pending` returns both specs for un-migrated collections; `GET /status` `collections_schema_behind` reflects the count.

2. **`POST /ingest` and `POST /ingest/directory` gain `chunk_ttl_seconds?: int | null` and `chunk_scopes?: list[str] | null`** — additive optional fields. `chunk_ttl_seconds` ∈ [1, 2^31-1]; `chunk_scopes` 0–100 items × 1–255 chars. Validation: `chunk_ttl_seconds=0` or negative → 422; scope string > 255 chars or > 100 items → 422. `chunk_scopes=[]` (explicit empty list) is normalized to `null` by the pipeline (no storage difference). Existing calls without these fields are completely unaffected.

3. **`PATCH /collections/{name}` gains `default_ttl_seconds?: int | null`** — additive optional field. Sets the collection-level TTL default; `null` clears it. When set, newly ingested chunks without a per-request `chunk_ttl_seconds` inherit `expires_at = ingest_time + default_ttl_seconds`. Forward-only: PATCH does NOT retroactively update existing chunks. Strict-validating clients on `PatchCollectionBody` must add this nullable field.

4. **New `GET /collections/{name}/expiring?within_hours={n}` endpoint** — cursor-paginated list of chunks expiring in the next `within_hours` hours (`within_hours` ∈ [1, 8760]); returns `ExpiringChunksResponse`. Additive — no existing endpoints modified.

5. **`POST /search` and `POST /explain` gain `scope_filter?: str | null`** — additive optional field. `null` (default) = no scope filtering; exact value = only chunks with that exact tag in `scopes`; trailing `*` = prefix wildcard match. Unscoped chunks (`scopes = null`) always match any `scope_filter`. Invalid patterns (bare `*`, leading `*`, mid-string `*`, multiple `*`) → 400. 400 body: `{"detail": {"code": "invalid_scope_filter", "message": "..."}}` (wrapped in a `detail` envelope, same pattern as `graph_communities_not_built`). `scope_filter` + any `graph_mode` value → 422. Strict-validating clients on `SearchRequest` and `ExplainRequest` must add this nullable field.

6. **`GET /status` maintenance sub-object gains `expired_chunk_count: int` and `last_expired_pruned_at: str | null`** — `expired_chunk_count` is always an integer (never null); reflects the live point-in-time count of chunks with `expires_at < now_utc` in the caller-visible collection tables (note: counts span all namespaces sharing a collection table — the namespace parameter is accepted for API symmetry only; all tenants in a shared-table collection contribute to the count). `last_expired_pruned_at` is null until the first prune pass. Strict-validating clients on `MaintenanceStatusDetail` must add both fields.

7. **`GET /collections/{name}/documents` items gain `scopes: list[str]`** — present after E2a migration (empty list when the document has no scoped chunks; absent on un-migrated collections where the `scopes` column has not been added yet). Strict-validating clients on `DocumentInfoItem` must add this field.

8. **MCP tools updated** — `ingest_file` and `ingest_directory` gain `chunk_ttl_seconds: int | null` and `chunk_scopes: list[str] | null`; `search`, `search_with_context`, and `explain` gain `scope_filter: str | null`. MCP tool-level validation mirrors the REST rules. All changes are additive — callers that do not pass these parameters are unaffected.

**Operator runbook for migration**: after upgrading to E2a, run `POST /collections/{name}/migrate` (or `archon-search collection migrate <name>`) for each collection before using TTL or scopes features. All five existing v0 startup migrations are unaffected. The schema bump adds two migration specs at `introduced_at=1`; these are NOT applied at server startup (unlike the five v0 migrations) — explicit per-collection migration is required.

**Migration**: no action required for existing callers that do not use TTL or scopes. Run `POST /collections/{name}/migrate` for each collection when you are ready to use E2a features. Strict-schema validators should add the new optional fields to their type stubs; regenerate from `GET /openapi.json`.

---

### [next release] — E2a BE-4: `PATCH /collections/{name}` `embedding_model` field is now optional

**Surface**: `PATCH /collections/{name}` request body (`PatchCollectionBody`).

**Change**: `embedding_model` was previously a required `string` field. It is now an optional `string | null` field (default `null`). Callers that always provide `embedding_model` are completely unaffected. Sending `embedding_model: null` or omitting the field entirely skips the embedding-model state machine — only the newly-added `default_ttl_seconds` field is applied when provided.

**Migration**: no action required for existing callers that always supply `embedding_model`.

---

### [next release] — E1b: `graph_mode` extended to `"local"` and `"global"`; `StatusCollectionEntry` gains community stats (all additive)

**Surface**: `POST /search` request; `GET /status` response (`StatusCollectionEntry`); `GET /openapi.json` schema; MCP `search` tool.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **`SearchRequest.graph_mode` extended from `"naive" | null` to `"naive" | "local" | "global" | null`** — the two new string literals `"local"` and `"global"` activate community-based retrieval modes. Clients that already pass `graph_mode="naive"` or omit the field are completely unaffected. Clients with strict-schema validators that enumerate the allowed literals must add `"local"` and `"global"`. When either new value is supplied and `[graph] enabled = false`, the server returns `422`; callers that always omit `graph_mode` are unaffected.

2. **`GET /status` `StatusCollectionEntry` gains `community_count: int` and `last_built_at: str | null`** — always present (zero and null when communities have not been built or when graph is disabled). Strict-validating clients must add these fields to their `StatusCollectionEntry` type stubs. Tolerant clients are unaffected.

3. **`GET /status` `GraphCollectionStats` gains `community_count: int` and `last_built_at: str | null`** — same semantics as (2), scoped to the per-collection entry inside `graph.collections`. Strict-validating clients must update their `GraphCollectionStats` stubs.

4. **New auxiliary LanceDB table** — when `[graph] enabled = true`, `CommunityBuilder.build()` creates a `_archon_graph_{col}_communities` table per collection (triggered via `archon-search graph build-communities <COLLECTION>`). This is auxiliary/internal, never exposed via REST or MCP pagination. Deployments without `graph.enabled = true` are completely unaffected.

**Migration**: no action required for tolerant JSON consumers. Strict-schema validators should add `"local"` and `"global"` to `SearchRequest.graph_mode`, `community_count: int` to `StatusCollectionEntry` and `GraphCollectionStats`, and `last_built_at: str | null` to both. Regenerate from `GET /openapi.json`. To use the new modes: install `archon-search[graph]`, set `[graph] enabled = true`, re-ingest, then run `archon-search graph build-communities`.

---

### [next release] — E1a: graph tables, `SearchRequest.graph_mode`, `SearchResponse.graph_expansion_applied` (all additive)

**Surface**: LanceDB auxiliary tables (internal); `POST /search` request and response; `GET /status` response; MCP `search` and `search_with_context` tools.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

1. **Auxiliary LanceDB graph tables** — when `[graph] enabled = true`, two new LanceDB tables are created per collection after ingest: `_archon_graph_{col}_nodes` and `_archon_graph_{col}_edges`. These are auxiliary to the main chunk table and are never exposed via REST or MCP pagination. Deployments that do not set `[graph] enabled = true` are completely unaffected — no tables are created and no startup overhead occurs.

2. **`SearchRequest` gains `graph_mode: "naive" | null = null`** — additive optional field. Existing clients that omit the field continue to receive unmodified behaviour. Strict-schema validators that use `extra="forbid"` on `SearchRequest` must add this nullable field. When `"naive"` is supplied and `[graph] enabled = false`, the server returns `422`; callers that always omit `graph_mode` are unaffected.

3. **`SearchResponse` gains `graph_expansion_applied: bool`** — always present (default `false`). Strict-schema validators must add this boolean field to their `SearchResponse` type stubs. Tolerant clients are unaffected.

4. **`GET /status` gains `graph: GraphStatusDetail | null`** — `null` when `[graph] enabled = false`. When the graph is enabled, the field contains `{enabled: true, node_count: int, edge_count: int, collections: [{collection, node_count, edge_count}]}`. Strict-validating clients must add `graph` to their `StatusResponse` type stubs.

5. **MCP `search` tool gains `graph_mode: str | null = null`** — additive optional parameter. Existing callers are unaffected.

6. **MCP `search_with_context` returns `{error, code: "graph_mode_not_supported"}` when `graph_mode` is non-null** — guard behaviour; callers that do not pass `graph_mode` are unaffected.

**Migration**: no action required for tolerant JSON consumers or existing deployments without `[graph] enabled = true`. To opt in: install `archon-search[graph]`, set `[graph] enabled = true` in TOML, and re-ingest collections. Strict-schema validators should add `graph_mode: "naive" | null` to `SearchRequest`, `graph_expansion_applied: bool` to `SearchResponse`, and `graph: GraphStatusDetail | null` to `StatusResponse` type stubs; regenerate from `GET /openapi.json`.

---

### [next release] — E0e: `POST /search` multi-collection + filters now supported; `SearchResponse` gains `applied_filters`

**Surface**: `POST /search` REST endpoint.

**Behaviour change** (previously-rejected requests now succeed — non-breaking for well-behaved clients):
- `POST /search` with both `collections` and `filters` no longer returns `422 "filters are not supported for multi-collection search in v1"`. The filters are applied per-leg to the multi-collection fan-out. Clients that relied on this 422 as a guard must update their error-handling logic.

**Additive change** (non-breaking for tolerant JSON consumers):
- `SearchResponse` gains `applied_filters: SearchFilters | null` — echoes the parsed, normalised filters from the request; `null` when no filters were submitted. Present on both single-collection and multi-collection response paths. Strict-validating clients must add this nullable field to their response schemas.

---

### [next release] — E0d: `POST /ingest` gains 413 response; `IngestResult.code` field added; MCP `IngestResultSchema` gains `code` field

**Surface**: `POST /ingest` REST response; `IngestResult` domain dataclass; MCP `ingest_file` and `ingest_directory` tool return shapes (`IngestResultSchema`).

**Breaking changes**:

1. **`POST /ingest` now returns HTTP 413 when a single-file path exceeds `[ingest].max_file_mb`** — when `max_file_mb > 0` is configured and a single-file path in the request body exceeds the limit, the route returns `413 Request Entity Too Large` with `{"detail": "File size X MB exceeds the configured limit of Y MB (\`[ingest].max_file_mb\`). Raise the limit in \`archon-search.toml\` or split the file."}` BEFORE any job is created. Clients that assume `POST /ingest` always returns `202` or `400`/`503` must add handling for `413`. The 413 only fires when `max_file_mb > 0` (opt-in); the default (`max_file_mb = 0`) is unchanged — all ingest submissions return `202`. Directory paths and `documents`-payload requests are never checked at the route level (413 does not apply).

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

2. **`IngestResult` domain dataclass gains `code: Literal["file_too_large"] | None = None`** — `None` on success; `"file_too_large"` when the pipeline size guard fires. This field flows through `GET /jobs/{id}` result dicts (per-file results in directory ingest jobs) and the MCP tool return value. Clients that reconstruct `IngestResult` objects from raw dicts must add the `code` field or allow unknown fields.

3. **MCP `IngestResultSchema` gains `code: Literal["file_too_large"] | None = None`** — mapped from `IngestResult.code` by `from_result()`. Strictly-validating MCP clients with `extra="forbid"` on the schema must add the `code` field. Tolerant clients are unaffected. MCP `ingest_file` tool returns `{status: "error", code: "file_too_large", error: "<message>"}` when the size guard fires.

**Migration**:
- Item 1: If your client checks `POST /ingest` response codes, add a `413` case. The response body `detail` is a plain string (same `ErrorDetail` envelope as `400`). No migration needed if `max_file_mb` remains `0` (the default).
- Items 2 and 3: Purely additive if you tolerate unknown fields. For strict schemas, add `code: "file_too_large" | null` to your `IngestResult` / `IngestResultSchema` type stubs (`str | null` is acceptable for tolerant consumers).

---

### [next release] — E0c: `top_k` OpenAPI schema change; 422 envelope change for fanout and top_k validation; additive `search` sub-object on `GET /status`; new `GET /collections/{name}/documents` endpoint

**Surface**: `POST /search`, `POST /explain` request schemas; `GET /status` response; `GET /collections/{name}/documents` (new endpoint); MCP `list_documents` tool.

**Breaking changes**:

1. **`top_k` OpenAPI `maximum` constraint removed from the schema definition** — `SearchRequest.top_k` and `ExplainRequest.top_k` previously carried a static `le=100` Pydantic `Field` constraint that appeared in the generated OpenAPI schema as `"maximum": 100`. This constraint has been removed from the Pydantic model. The upper-bound check is now applied at runtime in the route handler body using `config.top_k_max` (default `100`). The schema in `GET /openapi.json` no longer carries `"maximum": 100` on the `top_k` field. Generated clients that relied on the static OpenAPI `maximum` annotation for client-side range validation must update their type stubs — the server still enforces the limit, but the schema no longer communicates it statically.

2. **422 error envelope change for fanout and `top_k` bound violations** — previously, `len(collections) > max_fanout` and `top_k > 100` were caught by Pydantic `@model_validator` and `Field(le=…)` respectively, producing a standard Pydantic validation-error list `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`. Both checks now run in the route handler body and return a `JSONResponse` with `status_code=422` and `{"detail": "..."}` body, producing a plain-string detail `{"detail": "collections length exceeds maximum of N"}` or `{"detail": "top_k N exceeds operator-configured maximum of M"}`. Clients that parse `response.json()["detail"]` as a list will see a different shape. Clients that only check `status_code == 422` and display the body verbatim are unaffected.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators):

3. **`GET /status` gains `search: SearchStatusDetail | null`** — `StatusResponse` gains an optional `search` sub-object: `{"max_fanout": int, "top_k_max": int}`. Both fields always reflect the live `SearchConfig` values (TOML overrides or defaults). Strictly-validating clients with `extra="forbid"` must add `search` to their `StatusResponse` type stubs. Tolerant clients are unaffected.

4. **New `GET /collections/{name}/documents` REST endpoint** — cursor-paginated document listing: `limit` (1–200, default 50), `cursor` (opaque, `None` to start from the beginning). Returns `{"items": [DocumentInfoItem…], "next_cursor": str | null, "total": int}`. Additive — no existing endpoints are modified. Clients that enumerate allowed routes must add this path.

5. **MCP `list_documents` gains optional `cursor` parameter** — the tool now accepts `cursor: str | None = None` in addition to the existing `collection` and `limit` parameters. Additive — existing calls without `cursor` are unaffected.

**Migration**:
- For items 1 and 2: if your client-side schema set `maximum: 100` on `top_k` from the OpenAPI snapshot, remove that static constraint. For fanout (`len(collections)`) and `top_k` bound violations specifically, the 422 `detail` value changed from a Pydantic validation-error list to a plain string — update client code that parsed `detail` as a list for these specific errors. Other Pydantic validation errors (e.g. wrong field type for `top_k`) still produce the list shape. Alternatively, regenerate client types from the updated `GET /openapi.json` snapshot.
- For item 3: regenerate client types from `GET /openapi.json` or add `search: { max_fanout: int, top_k_max: int } | null` to your `StatusResponse` type stubs.
- For items 4 and 5: purely additive; no migration required.

---

### [next release] — E0b: additive fields on SearchResponse, StatusResponse, StatsResponse; new FAILED_EXPIRED job status

**Surface**: `POST /search`, `GET /status`, `GET /telemetry/stats` REST responses; MCP `search` and `search_with_context` tool returns; `JobStatus` enum.

**Additive changes** (non-breaking for tolerant JSON consumers; breaking for strict-schema validators with `extra="forbid"`):

- `POST /search` and MCP `search`/`search_with_context` responses gain `expansion_used: bool` and `expansion_warning: str | null`. `expansion_used = hyde_applied OR rag_fusion_applied`. `expansion_warning` is non-null when expansion was requested but failed (fell back to the original query embedding).
- `GET /status` response gains `hyde: HydeStatusDetail | null` (present when `[hyde].enabled = true`), `rag_fusion: RagFusionStatusDetail | null` (present when `[rag_fusion].enabled = true`), and `failed_expired_ingest_count: int` (namespace-scoped — counts only `IngestJob` failures in the authenticated namespace). Each detail object has `key_available: bool`.
- `GET /telemetry/stats` response gains `truncated_count: int` — count of log entries where `result_doc_ids` was trimmed to 8 KB by the writer (since D8). Entries written before E0b have `truncated=None` and are not counted.
- `JobStatus` enum gains `FAILED_EXPIRED = "FAILED_EXPIRED"`. A terminal state for ingest jobs that aged past `retry_max_age_hours` or exhausted all retry attempts. `GET /jobs?status=FAILED_EXPIRED` is a valid filter. All five `_TERMINAL_STATUSES` definitions are updated to include this value.

**Migration**: no changes required for tolerant JSON consumers. Clients that exhaustively switch/match on `JobStatus` must add a case for `FAILED_EXPIRED`. Clients with strict-schema validators (`extra="forbid"`) must add the new fields to their schemas. Regenerate client types from `GET /openapi.json` (the snapshot in `tests/server/openapi_snapshot.json` has been updated).

---

### [next release] — E0b FE-2: `export --wait` and `backup --now --wait` exit codes changed; `--timeout` option added

**Surface**: `archon-search export --wait` and `archon-search backup --now --wait` CLI commands.

**Breaking changes**:
1. `archon-search export --wait` previously exited with code `1` when the job status was `FAILED`. It now exits `2` on `FAILED` or `FAILED_EXPIRED`. Callers that relied on exit code `1` to detect export failure must update to check exit code `2`.
2. `archon-search backup --now --wait` previously exited with code `1` when any backup job status was `FAILED`. It now exits `2` on `FAILED` or `FAILED_EXPIRED`. Same migration requirement.
3. Both commands previously had no timeout — they polled indefinitely. They now exit `0` on timeout (with a recovery hint on stderr) after the `--timeout` duration (default 300 s).

**New behavior summary for both commands**:
- Exit 0 + recovery message on stderr: timed out (job(s) may still be running).
- Exit 0 + success message: job(s) completed successfully (DONE).
- Exit 2 + error on stderr: job confirmed FAILED or FAILED_EXPIRED.
- Exit 1: fatal error (auth failure, network error, HTTP 4xx).

**New `--timeout SECONDS` option** (default 300): controls the maximum seconds `--wait` polls before declaring a timeout. The option is additive and backward-compatible for scripts that do not pass `--timeout`; the only behavioral change is the new finite timeout (previously infinite) and the exit-code changes on FAILED.

**Migration**:
- Scripts that check `exit_code == 1` to detect `export --wait` or `backup --now --wait` failure must update to `exit_code == 2`. Both `FAILED` and `FAILED_EXPIRED` now exit 2.
- A timeout now exits 0; check stderr for the recovery hint string if distinction is required.
- The import command's `--wait` path is explicitly OUT OF SCOPE for E0b — its timeout behavior is noted as tech debt for CLI consistency.

---

### [next release] — E0b FE-1: `maintenance run --wait` timeout exit code changed 1 → 0

**Surface**: `archon-search maintenance run --wait` CLI command.

**Breaking change**: `archon-search maintenance run --wait` previously exited with code `1` when the poll timed out waiting for the maintenance pass to complete. It now exits `0` on timeout and prints a recovery hint on stderr: `"Poll with 'archon-search maintenance status' to check progress."` Callers that relied on exit code `1` to detect timeout must now distinguish between timeout (exit 0 + stderr hint) and success (exit 0, no hint).

**New behavior summary**:
- Exit 0 + recovery message on stderr: timed out (pass may still be running).
- Exit 0 + "Maintenance pass complete." on stdout: pass completed successfully.
- Exit 2 + error on stderr: pass completed with errors (collection-level failures visible via `archon-search maintenance status`).
- Exit 1: fatal error (auth failure, network error, HTTP 4xx).

**New `--timeout SECONDS` option** (default 120): controls the maximum seconds `--wait` polls before declaring a timeout. The option is additive and backward-compatible; existing scripts that do not pass `--timeout` continue to behave as before except for the exit-code change on timeout.

**Migration**: scripts that check `exit_code == 1` to detect a `--wait` timeout must be updated. A timeout now exits 0; check stderr for the recovery hint string `"archon-search maintenance status"` if distinction is required.

---

### [next release] — D7: new `/keys` REST endpoints and `key` CLI commands (additive)

**Surface**: four new REST endpoints under `/keys`; four new MCP tools; new `key` CLI command group.

**New REST endpoints** (all require `Bearer` token):
- `POST /keys` → `201 KeyCreateResponse` — issues a new managed API key; token present once.
- `GET /keys` → `200 KeyListResponse` — lists managed keys; active-only by default.
- `DELETE /keys/{id}` → `200 KeyRevokeResponse` — revokes a managed key; idempotent.
- `POST /keys/rotate` → `200 KeyRotateResponse` — generates a new default key; token present once; returns `409` when `ARCHON_SEARCH_API_KEY` env var is set.

**New MCP tools**: `create_key`, `list_keys`, `revoke_key`, `rotate_key`. MCP tool count: 13 → 17.

**New CLI commands**: `archon-search key create`, `key list`, `key revoke <id>`, `key rotate`.

**New config section**: `[auth]` with `rotate_grace_seconds = 0`.

All changes are **additive** — existing single-key deployments require zero config changes. The `ARCHON_SEARCH_API_KEY` env var and TOML `[namespaces]` tokens continue to work unchanged. No existing REST endpoints, MCP tools, or CLI commands are modified.

**Migration**: none required for existing deployments. Add the four new `/keys` paths to client schemas if strict validation is in use.

---

### [next release] — D6: `CheckStatus` gains `PENDING` and `WARN`; `GET /ready` gains `checks.models`; `GET /status` gains `model_validation`

**Surface**: `GET /ready` and `GET /status` REST responses; `CheckStatus` enum.

- `CheckStatus` now includes two additional values: `PENDING = "pending"` and `WARN = "warn"` (previously only `OK` and `FAIL`). Clients that exhaustively switch/match on `CheckStatus` must add cases for both.
- `GET /ready` `checks` object gains a `models` field (`CheckStatus`): `"pending"` before background model validation completes, then `"ok"` (both probes pass), `"warn"` (provider fallback occurred), or `"fail"` (a model could not load). The top-level `ready: bool` is **NOT** gated on `models` — it remains storage-only, so load balancers keying on `ready` are unaffected.
- `GET /status` gains an optional `model_validation: ModelValidationStatus | null` sub-object (`embedder_ok`, `reranker_ok`, `provider_warnings`, `validated_at`); all fields are `null` while validation is pending.
- Both response changes are additive — permissive clients that ignore unknown fields are unaffected.

**Migration**: add `PENDING` and `WARN` to any exhaustive `CheckStatus` switch/match. Add the `checks.models` and `model_validation` keys to client schema definitions if strict validation is in use. No action required for read-only callers that key only on `ready`.

---

### [next release] — D4: `centroid_incremental_enabled` config field removed

**Surface**: `archon-search.toml` `[database]` section.

- The `centroid_incremental_enabled` field has been removed from `SearchConfig`. The B5 incremental centroid path is now unconditional.
- If present in an existing TOML config, the value is discarded and a WARNING is logged at startup.
- The per-operation full-recompute gate (previously activated by `centroid_incremental_enabled = false`) has been removed. The full-recompute path itself (`recompute_collection_meta`) still exists and fires on `needs_recompute = True` or periodic drift-reset checkpoints.

**Migration**: remove `centroid_incremental_enabled` from your `archon-search.toml`. For operators who never set this flag (or had it set to `true`), there is no functional change — the incremental path was already the default since B5. If you had explicitly set it to `false`, your deployment will now use the B5 incremental centroid path; run `archon-search collection reindex <name>` if you observe centroid drift.

---

### [next release] — D2 job contract: `JobResponse` gains bulk-job subclass fields

**Surface**: `GET /jobs/{job_id}` and `GET /jobs` REST responses (`JobResponse`).

- `JobResponse` now includes four optional nullable fields: `source` (`"user" | "backup" | null`), `collection` (`str | null`), `output_path` (`str | null`), `archive_path` (`str | null`).
- These fields are populated for `ExportJob` and `ImportJob` records; base `IngestJob`, `ReindexJob`, and `DeleteJob` records return them as `null`.
- Additive change. Strict-schema clients will see four new keys; permissive clients are unaffected.

**Migration**: no action required for read-only callers. Add the four keys to client schema definitions if strict validation is in use.

---

### [next release] — D2 status contract: `StatusResponse` will gain a `backup` object

**Surface**: `GET /status` REST response (`StatusResponse`).

- `StatusResponse` will gain an optional `backup: BackupStatusDetail | null` field describing scheduled-backup state (enabled flag, interval, last/next tick, per-collection backup status). Added in a later D2 task; documented here to give clients a heads-up.
- Additive change. Existing callers that ignore unknown fields are unaffected.

**Migration**: no action required for read-only callers. Add the `backup` key to client schema definitions if strict validation is in use, once the field ships.

---

### [next release] — D1/D2 MCP tools: `export_collection` and `import_collection` added (tool count 11 → 13)

**Surface**: MCP tool registry (`create_app`).

- Two new tools registered: `export_collection(collection, output_path="")` and `import_collection(collection, path, force_overwrite=False, ignore_schema_version=False, on_error="fail")`.
- Both tools are **additive** — existing MCP tool consumers are unaffected.
- MCP clients that enumerate the tool list will now see 13 tools instead of 11.

**Migration**: none required.

---

### [next release] — D1 job contract: `progress` field on `JobResponse` and `QUEUED` status

**Surface**: `GET /jobs/{job_id}` REST response (`JobResponse`), `JobStatus` enum.

- `JobResponse` now includes an optional `progress` field (`dict | None`, default `null`). Existing callers that don't reference this field are unaffected; callers that use strict schema validation will now see `progress: null` for job types that don't set it.
- `JobStatus` now includes `QUEUED` as a valid value (between `PENDING` and `RUNNING`). Clients that exhaustively switch on status strings must add a `QUEUED` case.

**Migration**: no action needed for read-only callers. Add `QUEUED` to any exhaustive status switch/match.

---

### [next release] — C7 MCP Pydantic responses: field-narrowing on collection and context tools

**Surface**: MCP tools `list_collections`, `get_collections_meta`, `get_collection_meta`, `update_collection`, `search_with_context`.

All five tools previously returned raw `dataclasses.asdict()` payloads. They now validate return values through explicit Pydantic schemas before serialization. This removes internal/transient fields that were never part of the public contract and renames one field for consistency.

---

**`list_collections` — removed fields + field rename**:
- Removed from every item in the returned list: `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`.
- Field **renamed**: `active_embedding_model` → `embedding_model`. Clients that read `active_embedding_model` will receive `undefined`/`null` after this change — update to read `embedding_model`.
- Fields `centroid` and `description_embedding` were already stripped before C7; that behaviour is unchanged.

**Migration**: remove any references to the seven removed fields from client code. Replace `active_embedding_model` with `embedding_model`.

---

**`get_collections_meta` — removed fields + field rename + `description_embedding` is always present**:
- Same removals as `list_collections` above (`centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`).
- Field **renamed**: `active_embedding_model` → `embedding_model`.
- `description_embedding` was previously absent when `include_description_embedding=false` (the default). It is now always present in the schema, serialized as `null` when not included. Strict-schema clients that reject unknown or null-valued fields must add `description_embedding: list[float] | null` to their type stubs.

**Migration**: replace `active_embedding_model` with `embedding_model`. Tolerate `description_embedding: null` in the default case, or explicitly request it with `include_description_embedding: true`.

---

**`get_collection_meta` — removed fields + field rename**:
- Removed: `centroid`, `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`.
- Field **renamed**: `active_embedding_model` → `embedding_model`.
- Previously `centroid` (a raw float vector) was included in the response. It is now absent entirely.
- `description_embedding` was added in B4 to this tool's response. It is now **removed**: `get_collection_meta` uses `CollectionDetailSchema`, which does not include this field. Clients that were reading `description_embedding` from `get_collection_meta` must switch to `get_collections_meta` with `include_description_embedding: true`.

**Migration**: replace `active_embedding_model` with `embedding_model`. Remove references to `centroid` and the internal fields listed above. If you need `description_embedding`, use `get_collections_meta` instead.

---

**`update_collection` — removed fields + field rename**:
- Same removals and rename as `get_collection_meta` above.

**Migration**: same as `get_collection_meta`.

---

**`search_with_context` — context chunk fields removed**:
- Context chunks in `context_before` and `context_after` previously included `start_offset`, `end_offset`, and `custom_score` (only `vector` was stripped in A1). These three transient fields are now excluded from all context chunk items.
- `language` is retained — it was present before C7 and remains in the schema. This is NOT a breaking change.

**Migration**: remove any client-side reads of `start_offset`, `end_offset`, and `custom_score` from context chunk items.

---

**New error code for schema drift**:
- All 11 MCP tools now return `{"error": "<message>", "code": "schema_validation_error"}` when a Pydantic schema validation error is raised during response construction (e.g., a domain dataclass field was added without updating the MCP schema). This replaces a potential silent shape change or unhandled exception.

**Migration**: add handling for `code == "schema_validation_error"` if your client distinguishes error codes.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on MCP tool return dicts

**Surface**: MCP tools `search`, `search_with_context`, and `explain`.

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `search` and `search_with_context` return dicts gain `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), and `rag_fusion_attempted: bool` (default `false`).
- `explain` return dict gains `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), `rag_fusion_attempted: bool` (default `false`), `rag_fusion_failure_reason: str | null` (default `null`), and `rag_fusion_sub_queries: list[{variant_index, result_count, top_doc_ids}] | null` (default `null`).
- `search`, `search_with_context`, and `explain` tools each gain a new `rag_fusion: bool = false` parameter.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add these fields to their tool return type stubs.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on ExplainResponse

**Surface**: `POST /explain` response (`ExplainResponse`); `POST /explain` request (`ExplainRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `ExplainRequest` gains `rag_fusion: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `ExplainResponse` gains `rag_fusion_applied: bool` (default `false`). Indicates whether the explain used RAG Fusion multi-query decomposition and result fusion.
- `ExplainResponse` gains `rag_fusion_queries_used: int` (default `0`). Number of successful LLM-generated variant searches (not counting the original query).
- `ExplainResponse` gains `rag_fusion_attempted: bool` (default `false`). Indicates whether RAG Fusion generation was attempted (true even if fallback to single-query occurred).
- `ExplainResponse` gains `rag_fusion_failure_reason: str | null` (default `null`). Populated with the error type when RAG Fusion was attempted but fell back.
- `ExplainResponse` gains `rag_fusion_sub_queries: list[RagFusionSubQueryResult] | null` (default `null`). Per-variant result summaries when RAG Fusion succeeded; each entry has `variant_index`, `result_count`, and `top_doc_ids`.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add all six fields to their `ExplainResponse` and `ExplainRequest` type stubs.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on SearchResponse

**Surface**: `POST /search` response (`SearchResponse`); `POST /search` request (`SearchRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `SearchRequest` gains `rag_fusion: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `SearchResponse` gains `rag_fusion_applied: bool` (default `false`). Indicates whether the search used RAG Fusion multi-query decomposition and result fusion.
- `SearchResponse` gains `rag_fusion_queries_used: int` (default `0`). Number of successful LLM-generated variant searches (not counting the original query).
- `SearchResponse` gains `rag_fusion_attempted: bool` (default `false`). Indicates whether RAG Fusion generation was attempted (true even if fallback to single-query occurred).

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add these fields to their `SearchResponse` and `SearchRequest` type stubs.

**Announced in**: this release.

### [next release] — C4 HyDE query expansion: additive fields on SearchResponse and ExplainResponse

**Surface**: `POST /search` response (`SearchResponse`); `POST /search` request (`SearchRequest`); `POST /explain` response (`ExplainResponse`); `POST /explain` request (`ExplainRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `SearchRequest` gains `hyde: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `SearchResponse` gains `hyde_applied: bool` (default `false`). Indicates whether the ANN lookup was driven by a HyDE-generated hypothesis embedding. Clients that validate the response schema strictly must accept this new field.
- `ExplainRequest` gains `hyde: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `ExplainResponse` gains `hyde_applied: bool` (default `false`). Indicates whether the ANN lookup was driven by a HyDE-generated hypothesis embedding. Clients that validate the response schema strictly must accept this new field.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add `hyde_applied: bool` to their `SearchResponse` and `ExplainResponse` type stubs, and `hyde: bool` to their `SearchRequest` and `ExplainRequest` type stubs.

**Announced in**: this release.

### [next release] — C4 HyDE query expansion: MCP `search_with_context` return type change

**Surface**: MCP `search_with_context` tool.

**Breaking change**: The `search_with_context` tool previously returned a bare `list[dict]` (a list of `{result, context_before, context_after}` dicts). It now returns a wrapper object `{"results": list[dict], "hyde_applied": bool}` where `results` carries the same list. This is a **true breaking change** for any MCP client that iterates the return value directly or accesses items by index.

**Migration**: Update consumers to access `response["results"]` instead of iterating the response directly. `response["hyde_applied"]` is the new HyDE status field.

**Announced in**: this release.

### [next release] — C2 language field type change (SearchResult, ScoredSearchCandidate, ExplainResult, ExplainNearMiss)

**Python**: `SearchResult.language`, `ScoredSearchCandidate.language`, `ExplainResult.language`, and `ExplainNearMiss.language` now return `""` (empty string) for legacy/untagged chunks instead of `None`. Update `if result.language is None` guards to `if result.language == ""`.

**REST/JSON**: The `language` field in search and explain responses now serializes as `""` (empty string) instead of `null`. OpenAPI clients must update their type stubs (`nullable: false`, `type: string`).

**Migration**: Replace `if result.language is None` with `if result.language == ""` in any code that checks for an untagged language. REST consumers should expect `""` instead of `null` for the `language` field in `SearchResultSchema`, `ExplainResult`, and `ExplainNearMiss`.

**Announced in**: this release.

### [next release] — C1 per-collection embedding model (schema changes)

**Surface**: REST `GET /collections/{name}` response (breaking rename); `GET /collections/` response (additive); `POST /collections/` request (additive); `PATCH /collections/{name}` (new endpoint); `POST /search` response (additive); MCP (new `update_collection` tool).

**`GET /collections/{name}` — breaking rename + additive fields**:
- `embedding_model` field **renamed** to `active_embedding_model`. Clients that read `embedding_model` will receive `null`/`undefined` — update to read `active_embedding_model`.
- Three new fields added: `pending_embedding_model` (nullable string), `needs_reindex` (bool), `reindex_job_id` (nullable string).

**`GET /collections/` — additive fields**:
- Each `CollectionSummary` entry gains `active_embedding_model: str` and `needs_reindex: bool`. Non-breaking for tolerant consumers.

**`POST /collections/` — additive optional request field**:
- Request body gains optional `embedding_model: str | null`. When provided, the collection is initialized with that model as `active_embedding_model`; when omitted, the global `config.embedding_model` is used. Unknown models return 422.

**`PATCH /collections/{name}` — new endpoint**:
- Updates the embedding model for a collection. Implements the per-collection model state machine: clearing `pending_embedding_model` (when setting the same model as active), setting `pending_embedding_model` (model change requiring reindex), or directly updating `active_embedding_model` (when no data exists yet). Returns `CollectionDetail` (same shape as `GET /collections/{name}`). Dimension validation runs before any state mutation. Returns 404 for unknown collections, 409 when a reindex job is already in progress, 422 for unknown models or dimension mismatches.

**`POST /search` — additive field**:
- `SearchResponse` gains `embedding_model: str`. For single-collection searches this is the collection's `active_embedding_model`; for multi-collection fan-out it is `config.embedding_model` (the global default).

**`PATCH /collections/{name}` — 422 conditions**:
- Returns `422` for unknown models **and** for dimension mismatches (when the requested model produces vectors of a different dimension than the existing index).

**MCP — new `update_collection` tool** (11th tool):
- Accepts `collection_name: str` and `embedding_model: str`. Implements the same state machine as `PATCH /collections/{name}`. Returns the updated `CollectionMeta` dict or `{error, code}` on failure.

**Migration**:
- `GET /collections/{name}` consumers: replace `embedding_model` with `active_embedding_model` in client code. Add handling for the three new fields (`pending_embedding_model`, `needs_reindex`, `reindex_job_id`).
- `GET /collections/` consumers: tolerate the new `active_embedding_model` and `needs_reindex` keys.
- `POST /search` consumers: tolerate the new `embedding_model` key.

**Announced in**: this release.

### [next release] — B5 incremental centroid maintenance (additive internal columns)

**Surface**: `_archon_collection_meta` LanceDB table (internal schema). No public REST or MCP contract change.

**Additive internal columns**:
- `centroid_sum_json` (`utf8`) — JSON-encoded element-wise sum of all chunk vectors; combined with `chunk_count` satisfies `centroid = centroid_sum / chunk_count`. Empty string when unset.
- `mutations_since_recompute` (`int64`) — incremented on every ingest/delete; reset to `0` after a full recompute. `-1` sentinel = pre-B5 row.
- `needs_recompute` (`bool`) — set `True` when incremental maintenance cannot proceed (model mismatch, NaN/Inf in vectors, missing seed sum). Cleared on successful `recompute_collection_meta`.

These columns are **not exposed** via any REST endpoint, MCP tool, or CLI command. They are internal routing metadata. Adding them is **non-breaking** for any public API consumer.

**Mixed-version deployment caveat**: an older binary's `update_collection_meta` uses its own schema (without the three new columns). If that binary performs a delete-then-insert upsert on a table that already carries the new columns, LanceDB's upsert semantics may null out the new columns on the affected row, which the B5 runtime treats identically to `needs_recompute = True` and recovers via a full recompute on next access. Do not run mixed-version deployments without first verifying your LanceDB version's actual upsert behavior. The safe upgrade path is to restart all instances to the same binary version before relying on incremental centroid accuracy.

**Migration**: none required for operators. The three columns are populated lazily on first ingest or reindex after upgrade. Existing collections are treated as `needs_recompute = True` until their first post-upgrade recompute.

**Announced in**: this release.

### [next release] — B4 hybrid collection routing

**Surface**: MCP `get_collection_meta`, `get_collections_meta`, and `list_collections` tools.

**`get_collection_meta` — additive output key (breaking for strict-validating clients)**:
- The single-collection `get_collection_meta` tool now returns `description_embedding: list[float] | null` on the `CollectionMeta` dict. The field is `null` when no embedding has been computed (e.g., immediately after upgrade before the startup migration runs). For **tolerant JSON consumers**: non-breaking additive key. For **strict-validating MCP clients** (`extra="forbid"` schemas): the new key is a true contract change — relax the client schema or handle the new field.

**`get_collections_meta` — additive optional INPUT parameter**:
- Gains an optional `include_description_embedding: bool = False` parameter. When `false` (the default), `description_embedding` is stripped from every `CollectionMeta` in the returned list. When `true`, the field is included. The `description_embedding` field was never present pre-B4, so stripping it by default is safe for existing consumers. Clients that reject unknown MCP tool input parameters may need to be updated to tolerate the new parameter.

**`list_collections` — no change for existing clients**:
- `list_collections` has always stripped `centroid` from the returned dict; it likewise strips `description_embedding`. No behavior change for existing clients — the field was never present.

**REST `/route` shape unchanged**: the routing strategy and description-weight blending are internal to `MultiCollectionRouter`; the `/route` response schema (`{pre_context, pinned_names, routable_names, decomposer_invoked}`) is unchanged.

**Migration**:
- `get_collection_meta` consumers using strict schema validation: add `description_embedding: list[float] | null` to your client-side schema, or relax to tolerate unknown fields.
- `get_collections_meta` consumers: no action needed — `description_embedding` is absent by default. To include it, pass `include_description_embedding: true`.
- To enable hybrid routing: set `[routing] routing_strategy = "hybrid"` and optionally tune `routing_description_weight` (default `0.3`) in `archon-search.toml`.

**Announced in**: this release.

### [next release] — B3 multi-collection search

**Surface**: REST `POST /search` and `POST /explain` (additive responses + request-schema change); MCP `search`/`explain` tools (additive response-shape change for strict clients).

**Request — additive/optional on both surfaces**:
- `POST /search` and the MCP `search` tool gain an optional `collections: list[str]` field for fanning out a single query across multiple collections in one request. `POST /explain` and the MCP `explain` tool gain the same field.

**Request-schema change (REST `/search`)**:
- `SearchRequest.collection` changes from required (`str`) to optional (`str | None = None`). Exactly one of `collection` / `collections` must be supplied. Existing clients that omit `collection` now receive a different 422 message (`"supply either collection or collections"` instead of Pydantic's `"field required"`); clients that explicitly send `collection: null` get the same new error. This is a request-schema behavioral change for clients that relied on the old validation message. The MCP `search` tool preserves its existing `default_collection` fallback when neither field is supplied.

**Response — additive keys**:
- Every result in `POST /search` / `POST /explain` (REST) and the `search`/`explain` MCP tools gains a `collection` key naming its origin collection. Each response gains an `excluded_collections` list (entries `{"name", "reason"}`) reporting collections dropped from a multi-collection request (e.g. embedding-model mismatch).
- For tolerant JSON consumers (REST), these are non-breaking additive keys. For **strict-validating MCP clients** (schemas with `extra="forbid"`), the new `collection` and `excluded_collections` keys are a true contract change — the same class as A1's additive-key break. Relax the client schema to tolerate the new keys.

**`/explain` multi-collection constraint**:
- `POST /explain` (and MCP `explain`) with `rerank=false` AND more than one collection returns 422 / a `validation_error` with message `"reranking cannot be disabled for multi-collection search in v1"`. Single-collection `rerank=false` is unchanged.

**Announced in**: this release.

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
- `SearchFilters` fields: `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`, `include_metadata`.
- **C2 update**: `language` is now an active filter (ISO 639-1/639-3 code or `"unknown"`). Previously rejected with 422 for any non-empty value. Invalid values (not matching `[a-z]{2,3}` or `"unknown"`, or used with multi-collection fan-out) still return 422.

**MCP — additive**:
- `search` tool gains optional kwargs: `include_metadata` (bool, default `false`), `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`. All default to `None`/`false`; existing callers passing only `query` and `collection` are unaffected.
- `search_with_context` tool gains the same optional kwargs.
- **C2 update**: `language` is now an active filter; valid ISO codes and `"unknown"` are accepted. Invalid values still return `{error: ..., code: "validation_error"}`.

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

### [next release] — D3 migration tooling: new REST endpoints and `STORE_SCHEMA_VERSION` policy

**Surface**: two new REST endpoints on `routes_collections.py`; `GET /status` response (`StatusResponse`).

**New endpoints**:
- `GET /collections/{name}/migrations/pending` — returns `{collection, pending: [MigrationSpec], schema_version}`. Each `MigrationSpec` has `name`, `kind` (`"in_place"`, `"rewrite"`, or `"export_rebuild"`), `description`, and `introduced_at`. Returns `404` for unknown or cross-namespace collections.
- `POST /collections/{name}/migrate` — accepts `{backup_confirmed: bool, dry_run: bool}`. In-place-only migrations return `200` with `{migrations_applied: [name…]}` synchronously; no `MigrationJob` is created. Rewrite migrations require `backup_confirmed: true` (returns `422` without it) and return `202` with a `MigrationJob` job ID. `export_rebuild` migrations always return `422` (execution deferred to D5). `409` when a `ReindexJob` is active for the collection.

**`StatusResponse` additions**:
- `store_schema_version: int` — current `STORE_SCHEMA_VERSION` constant.
- `collections_schema_behind: int` — count of collections whose `schema_version < STORE_SCHEMA_VERSION`.

All changes are additive. Strict-schema clients will see new fields; tolerant clients are unaffected.

**`STORE_SCHEMA_VERSION` bump policy**: increment this constant whenever a structural change to `_schema()` (the shared chunk-table schema) or `_meta_schema()` (the collection-metadata schema) requires existing rows to be migrated. **Exception:** per-collection chunk-table-only changes (e.g. `migrate_acl`) do NOT require a version bump — only changes to the shared `_schema()` or `_meta_schema()` require it. Every bump must also add a corresponding `MigrationSpec` entry to `SearchStore._all_migrations()`. `STORE_SCHEMA_VERSION = 0` for D3 (all five formalised startup migrations have `introduced_at = 0`).

**Migration**: no action required for existing callers. Add the new endpoint paths to client schemas if strict validation is in use. Add `store_schema_version` and `collections_schema_behind` to `StatusResponse` type stubs.

---

### [next release] — D5 maintenance jobs: `IngestJob` base class gains `source`, `source_path`, `collection`, `retry_count` fields (BE-7)

**Surface**: `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/ingest`, `DELETE /jobs/{job_id}` REST responses (`JobResponse`); all IngestJob-family subclasses (`ReindexJob`, `DeleteJob`).

**Changes**:
- `source: str` — moved to `IngestJob` base class with default `"user"`. Previously absent (`null`) for base `IngestJob`, `ReindexJob`, and `DeleteJob` responses. Now serializes as `"user"` for all IngestJob-family types. For `ExportJob`, `ImportJob`, and `MigrationJob`, the Literal type is narrower (`"user" | "backup"`) and is unchanged in practice. **Breaking for strict consumers**: `source` changes from `null` to `"user"` for base ingest, reindex, and delete jobs.
- `source_path: str` — new field on `IngestJob` base; defaults to `""`. Set by the ingest worker when a file ingest job is created. `JobResponse` gains `source_path: str = ""`.
- `collection: str` — moved to `IngestJob` base class with default `""`. Previously `null` for base `IngestJob`, `ReindexJob`, and `DeleteJob` responses. Now serializes as `""` for those types. **Breaking for strict consumers**: `collection` changes from `null` to `""` for base ingest, reindex, and delete jobs.
- `retry_count: int` — new field on `IngestJob` base; defaults to `0`. Incremented by `MaintenanceLoop` on each retry attempt. `JobResponse` gains `retry_count: int = 0`.

**Migration**: update client schemas to accept `source: "user"` (not `null`) for base ingest/reindex/delete job responses, and `collection: ""` (not `null`) for the same. Add `source_path: str` and `retry_count: int` to `JobResponse` type stubs.

**Announced in**: this release.

---

### [next release] — E0b BE-7: ingest job `result` contract changes from `null` to `{"warnings": [...]}`

**Surface**: `GET /jobs/{id}` REST response (`JobResponse.result`); `GET /jobs` list items.

**Changes**:
- `result: null | dict` — previously `null` for all ingest jobs (`FileIngestJob`, `DirectoryIngestJob`) after `DONE`. Now always `{"warnings": list[str]}` where `warnings` is the list of non-fatal warning strings collected during ingest (e.g. oversized ACL sidecar). An empty list (`[]`) means no warnings. **Breaking for strict consumers** that assert `result == null` after a completed ingest job.

**Migration**: relax client checks from `result == null` to `result == null or isinstance(result, dict)`. Add `warnings: list[str]` to `JobResponse.result` type stubs. An empty list is the normal (no-warning) case.

**Announced in**: this release.

---

### [next release] — D5 maintenance jobs: `GET /status` gains `maintenance` field (BE-4, BE-8)

**Surface**: `GET /status` REST response (`StatusResponse`).

**Change** (additive):
- `maintenance: MaintenanceStatusDetail | null` — new nullable field on `StatusResponse`. Present when `app.state.maintenance_loop` is set (always when the server starts normally). `null` only when the maintenance loop is absent (e.g. custom startup without `create_app`).
- `MaintenanceStatusDetail` contains: `enabled: bool`, `last_run_at: string | null`, `next_run_at: string | null`, `collection_health: CollectionHealthEntry[]`.
- `CollectionHealthEntry` contains: `collection: string`, `namespace: string`, `fts_optimized_at: string | null`, `orphans_removed_last_run: int`, `last_retry_at: string | null`, `last_error: string | null`, `mutations_since_recompute: int`, `centroid_recompute_threshold: int`, `meta_chunk_count: int`.
- `collection_health` is namespace-scoped to the caller's API key namespace.

**Additive change**: fully backward-compatible for tolerant consumers. Strict-validating REST clients (`extra="forbid"` schemas) must add `maintenance` to `StatusResponse`.

**Migration**: regenerate client types from `GET /openapi.json`. No behavior changes to existing `GET /status` fields.

**Announced in**: this release.

---

### [next release] — D5 maintenance jobs: new `POST /maintenance/trigger` endpoint (BE-4)

**Surface**: REST API — new route.

**Change** (additive):
- `POST /maintenance/trigger` — new endpoint. Triggers an immediate maintenance pass. Returns `202 Accepted` with `{"status": "triggered"}` when the pass is enqueued. Returns `202 Accepted` with `{"status": "already_triggered"}` when a pass is already pending or running. Requires `Bearer` token (same auth middleware as all other routes).

**Additive change**: no existing endpoints changed. Strict-validating REST clients that enumerate allowed routes must add `/maintenance/trigger`.

**Announced in**: this release.

---

### [next release] — D3 migration tooling: new nullable fields on `JobResponse` (BE-11)

**Surface**: REST (all endpoints that return `JobResponse`: `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/ingest`, `POST /jobs/export`, `POST /jobs/import`, `DELETE /jobs/{job_id}`, `POST /jobs/{id}/resume`)

**Change**: `JobResponse` gains three new nullable fields (default `null`):
- `kind: string | null` — migration sub-kind (`"in_place"`, `"rewrite"`, `"export_rebuild"`); `null` for all non-migration jobs
- `migrations_applied: string[] | null` — list of migration names applied by a `MigrationJob`; `null` for all non-migration jobs
- `backup_confirmed: boolean | null` — whether the operator confirmed a backup before a rewrite migration; `null` for all non-migration jobs

For tolerant JSON consumers: fully additive — no client changes required. For strict-validating REST clients (`extra="forbid"` schemas): the three new keys are a true contract change — relax the client schema or regenerate from the updated OpenAPI snapshot.

**Migration**: regenerate client types from `GET /openapi.json`. No behavior changes to existing job kinds.

**Announced in**: this release.

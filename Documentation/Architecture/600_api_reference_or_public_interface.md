**Purpose**: Authoritative human-readable reference for every public surface (REST, MCP, CLI) exposed by `archon-search`.
**Audience**: Medior engineers integrating with or operating `archon-search`.
**Status**: Draft
**Last reviewed**: 2026-06-24 / **Next review**: 2026-09-24

# API Reference and Public Interface

## Guiding principles

1. **Source of truth is code, not prose.** Every row in this document traces to a route module under `archon_search/server/` or a file under `archon_search/cli/`. The machine-readable contract is `GET /openapi.json`; this page is its narrative companion.
2. **One auth model everywhere.** Both REST and MCP run through the same `APIKeyMiddleware` (`archon_search/server/middleware_auth.py`). Only `GET /health` and `GET /ready` are unauthenticated on the REST surface; the MCP transport has only a `/health` handler (no `/ready` route). **D9** — the MCP sub-app's `APIKeyMiddleware` is now constructed with `config.namespaces` (extracted from the `config` argument inside `mcp.py::create_mcp_http_app`, not a separate factory parameter), so TOML `[namespaces]` tokens authenticate on `/mcp` exactly as they do on REST, in addition to managed keys (`key_store`) and the bootstrap default key.
3. **Breaking changes go in `BREAKING.md`.** CalVer segments do not encode compatibility; consult [`/BREAKING.md`](../../BREAKING.md) before upgrading.
4. **No raw queries on the wire.** Telemetry never stores query strings; this is a structural invariant — see [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md).
5. **Namespaces are enforced server-side (REST and MCP).** Every authenticated request resolves to a namespace; cross-namespace access yields `404`, never `403`. **D9** — MCP tool closures resolve the authenticated namespace per-request (via `request.state.namespace`, set by `APIKeyMiddleware`) and pass it as `namespace=` into every pipeline call, so MCP now enforces the same namespace boundary as REST.

## Authentication

- All endpoints except `GET /health` and `GET /ready` require an `Authorization: Bearer <token>` header.
- **D7 dispatch order**: the middleware checks (1) managed keys (`KeyStore.active_keys()` — SHA-256 hash comparison via `hmac.compare_digest`, exits on first match), then (2) TOML `[namespaces]` tokens (no early exit, timing-safe), then (3) the default key from `~/.archon-search/.search.env` (with a rotation-revocation guard: rejects tokens that match a revoked or expired `keys.json` record even if the raw token still matches the legacy `_api_key` fallback).
- Managed keys are issued, revoked, and rotated via the `/keys` REST endpoints or `archon-search key` CLI commands — no server restart required.
- The default key is resolved from `ARCHON_SEARCH_API_KEY` env var → `~/.archon-search/.search.env` (or `ARCHON_SEARCH_KEY_FILE` override) → auto-generated.
- Exempt paths (`middleware_auth.py::_EXEMPT_PATHS`): `/health`, `/ready`, `/docs`, `/openapi.json`, `/redoc`. Only `/health` and `/ready` actually appear in the OpenAPI schema; the others are defensive (FastAPI never includes them).
- On success, the resolved namespace is attached to `request.state.namespace` and used for filtering by every handler.
- Failure responses return `401` with `WWW-Authenticate: Bearer`.

## `X-Request-ID` response header

Every HTTP response carries an `X-Request-ID` header (set by `RequestContextMiddleware` in `server/middleware_context.py`). This includes `401`, `422`, `GET /health`, and all authenticated endpoints.

- If the inbound request supplies an `X-Request-ID` header whose value matches `^[A-Za-z0-9._-]{1,128}$`, the same value is echoed back in the response.
- Otherwise a fresh `uuid4().hex` is minted for that request and returned.
- The header name is configurable via `[observability].request_id_header` in `archon-search.toml` (default `"X-Request-ID"`).

Clients should use this value to correlate their request with log lines emitted by the server (each structured log line carries `correlation_id`).

## REST endpoints

The machine-readable contract is `GET /openapi.json`. Tables below trace every endpoint back to its route module under `archon_search/server/`.

### `routes_health.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/health` | Liveness probe; unauthenticated. | — | `HealthResponse` (`schemas.py`) — `{status, version, mcp}`. `mcp` is an `McpStatusDetail` (`enabled`, `bindAddress`) when `mcp.enabled = true`, or `null` when MCP is disabled (D9). |

### `routes_state.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/indexing-state` | Raw indexing state filtered to caller's namespace; when no state file exists, returns a populated `IndexingStateResponse` with `collections={}`, `last_updated=null`, `trigger=null` (not an empty body). | — | `IndexingStateResponse` (`schemas.py`) |

### `routes_status.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/status` | Operator-facing service status: PID, version, per-collection progress, ETA (namespace-filtered). **D2** — Response carries `backup: BackupStatusDetail \| null` summarising scheduled-backup state. **D3** — Response gains `store_schema_version: int` (current `STORE_SCHEMA_VERSION` constant) and `collections_schema_behind: int` (count of collections with `schema_version < STORE_SCHEMA_VERSION`). **D5** — Response gains `maintenance: MaintenanceStatusDetail \| null` (see `routes_maintenance.py` section for field details). **D6** — Response gains `model_validation: ModelValidationStatus \| null` (see `routes_ready.py` section). **D8** — Response gains `telemetry: TelemetryStatusDetail \| null` (see below). **D9** — Response gains `mcp: McpStatusDetail \| null` (see below). **E0b** — Response gains `hyde: HydeStatusDetail \| null`, `rag_fusion: RagFusionStatusDetail \| null`, and `failed_expired_ingest_count: int` (see below). **E0c** — Response gains `search: SearchStatusDetail \| null` (see below). **E1a** — Response gains `graph: GraphStatusDetail \| null` (see below). **BE-11** — Response gains `code_parsers: CodeParsersStatusDetail \| null` (see below). **E2a** — `maintenance.expired_chunk_count: int` (always non-null integer; live count of expired chunks in the caller-visible collection tables — not namespace-scoped within shared tables; all tenants in a shared-table collection contribute) and `maintenance.last_expired_pruned_at: str \| null` (ISO-8601 of last prune pass; null before first run) added to `MaintenanceStatusDetail`. **SPD** — each `StatusCollectionEntry.path` is now the collection's **absolute** resolved configured source path (the corpus directory from `collections`/`pinned_collections`, not the internal LanceDB index) via `_all_collection_paths(config)` (`""` only when the name is absent from that config-derived map — ad-hoc-ingested, collision-resolved, or config-removed collections, logged once per name at DEBUG), and `doc_count` is the **cached** `CollectionMeta.doc_count` for the caller's namespace (`0` when no meta row); no live recount is issued. Note the doc-count semantics differ across endpoints: `/status` returns the cached per-namespace counter, `GET /collections/{name}` recounts **live** (table-wide via `count_documents()`), and `GET /collections` (list) still hardcodes `0` — so the three can disagree. On a basename collision between two configured paths, `/status`'s `path` may show the wrong (last-registered) location rather than `""` (accepted limitation). | — | `StatusResponse` (`schemas.py`) |

The `model_validation` field added to `GET /status` (D6):

`StatusResponse` gains `model_validation: ModelValidationStatus | null`. It is `null` while the background validation task is still running (and on app factories that never set `app.state.model_validation`). When populated, `ModelValidationStatus` contains:

| Field | Type | Meaning |
| --- | --- | --- |
| `embedder_ok` | `bool \| null` | `true` if the embedder probe passed; `false` if it failed; `null` if not yet run. |
| `reranker_ok` | `bool \| null` | `true` if the reranker probe passed (or `reranker_model = ""`, disabled); `false` if it failed; `null` if not yet run. |
| `provider_warnings` | `list[str]` | Human-readable warnings (missing ONNX provider, probe failure, `"validation timed out after {N}s"`). Empty list when clean. |
| `validated_at` | `str (ISO 8601) \| null` | UTC timestamp when validation finished; `null` while pending. |

The `telemetry` field added to `GET /status` (D8):

`StatusResponse` gains `telemetry: TelemetryStatusDetail | null`. `null` when `[telemetry].enabled = false`. When telemetry is enabled, the field is a `TelemetryStatusDetail` object:

| Field | JSON key | Type | Meaning |
| --- | --- | --- | --- |
| `enabled` | `enabled` | `bool` | Always `true` when the object is present; `telemetry = null` is the signal that telemetry is disabled. |
| `hash_doc_ids_enabled` | `hash_doc_ids_enabled` | `bool` | `true` only when both `[telemetry].hash_doc_ids = true` in config **and** the salt was successfully loaded from `get_data_dir()/.telemetry-salt`. `false` when the config flag is on but the salt file was unreadable (fallback path; an ERROR is logged). |

Implemented in `archon_search/server/routes_status.py` (`_build_telemetry_status`) and `archon_search/server/schemas.py` (`TelemetryStatusDetail`).

The `mcp` field added to `GET /status` and `GET /health` (D9):

`StatusResponse` and `HealthResponse` both gain `mcp: McpStatusDetail | null`. The field is `null` when MCP is disabled (`[mcp].enabled = false`). When MCP is enabled, the field is an `McpStatusDetail` object:

| Field | JSON key | Type | Meaning |
| --- | --- | --- | --- |
| `enabled` | `enabled` | `bool` | Always `true` when the object is present; `mcp = null` is the signal that MCP is disabled. |
| `bind_address` | `bindAddress` | `str \| null` | Required-and-nullable (camelCase JSON). `"{config.host}:{config.port}/mcp"` once the sub-app has mounted successfully on the REST port; `null` when MCP is enabled but the mount has not (yet) succeeded or failed to start. |

**E0b** — `StatusResponse` gains three new fields:

- `hyde: HydeStatusDetail | null` — present only when `[hyde] enabled = true` in config; `null` when HyDE is disabled (key availability is irrelevant). When present, `HydeStatusDetail` contains:

| Field | Type | Meaning |
|---|---|---|
| `key_available` | `bool` | `true` when the configured provider's required API key is set at call time (`ANTHROPIC_API_KEY` for `"anthropic"`, `OPENAI_API_KEY` for `"openai"`; always `true` for `"ollama"` and `"claude_cli"` — neither needs a key). |
| `provider` | `str` | Active LLM provider name: `"anthropic"` (default), `"ollama"`, `"openai"`, or `"claude_cli"` — mirrors `[hyde].provider` in config. **G10** |

- `rag_fusion: RagFusionStatusDetail | null` — present only when `[rag_fusion] enabled = true` in config; `null` when RAG Fusion is disabled. Same shape as `HydeStatusDetail` (`key_available: bool`, `provider: str`). **G10**: `provider` field added; semantics identical to `HydeStatusDetail.provider`.

- `failed_expired_ingest_count: int` — count of `IngestJob` instances in `FAILED_EXPIRED` status in the caller's namespace. `0` when none exist. Operators should treat a non-zero value as a signal to re-ingest the affected files — the job result dict holds the original `path`; query `GET /jobs?status=FAILED_EXPIRED` to list them.

**E0c** — `StatusResponse` gains `search: SearchStatusDetail | null`. In production the field is always non-null — `_build_search_status` always returns a populated `SearchStatusDetail`. The schema declares it nullable for consistency with sibling sub-objects (`maintenance`, `backup`, etc.) that can be null when their respective loops are absent. `SearchStatusDetail` contains:

| Field | Type | Meaning |
|---|---|---|
| `max_fanout` | `int` | Operator-configured maximum number of collections per multi-collection `POST /search` or `POST /explain` request. Default `8`. Reads from `[search].max_fanout` in TOML. |
| `top_k_max` | `int` | Operator-configured upper bound on `top_k` accepted by `POST /search` and `POST /explain`. Default `100`. Reads from `[search].top_k_max` in TOML. |

Implemented in `archon_search/server/routes_status.py` (`_build_search_status`) and `archon_search/server/schemas.py` (`SearchStatusDetail`).

**E1a** — `StatusResponse` gains `graph: GraphStatusDetail | null`. `null` when `[graph] enabled = false`. When graph is enabled, `_build_graph_status(config, store)` queries `GraphStore` for per-collection node and edge counts. `GraphStatusDetail` contains:

| Field | Type | Meaning |
|---|---|---|
| `enabled` | `bool` | Always `true` when the object is present. |
| `node_count` | `int` | Total entity node count across all collections in the caller's namespace. |
| `edge_count` | `int` | Total relationship edge count across all collections in the caller's namespace. |
| `collections` | `list[GraphCollectionStats]` | Per-collection breakdown. Each entry has `collection: str`, `node_count: int`, `edge_count: int`. |

**E2f** — `GraphCollectionStats` gains three synonym health fields (all additive, zero defaults):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `synonym_edge_count` | `int` | `0` | Number of edges with `relationship_type='synonym_of'` in this collection. |
| `singleton_node_pct` | `float` | `0.0` | Percentage of entity nodes with no edges (isolated nodes), range 0.0–100.0. |
| `synonym_link_rate` | `float` | `0.0` | Fraction of all edges that are synonym edges (synonym_edge_count / edge_count), range 0.0–1.0. `0.0` when edge_count is 0. |

Note: `connected_component_count` was removed during BE-7 review (C1-B-1 fix) because it was always returned as 0 without being computed. It is NOT present in the schema.

Implemented in `archon_search/server/routes_status.py` (`_build_graph_status`) and `archon_search/server/schemas.py` (`GraphStatusDetail`, `GraphCollectionStats`).

**E1b** — `StatusCollectionEntry` (each entry in `StatusResponse.collections`) gains `community_count: int` (0 if communities have never been built for the collection) and `last_built_at: str | null` (ISO 8601 UTC timestamp of the last successful `graph build-communities` run; `null` if never built).

**BE-11** — `StatusResponse` gains `code_parsers: CodeParsersStatusDetail | null` (E2g BE-11 / S9 — soft-degrade check for missing tree-sitter code parsers). `null` when `[graph] enabled = false`: this check is only meaningful alongside code graphing. When graph is enabled, `_build_code_parsers_status(config)` returns a populated `CodeParsersStatusDetail`:

| Field | Type | Meaning |
|---|---|---|
| `degraded` | `bool` | `true` once any code file extension has hit the missing-grammar branch in `code_enricher._get_grammar` during this process's lifetime. |
| `missing_extensions` | `list[str]` | Sorted list of extensions with no available tree-sitter grammar so far. Empty when not degraded. |
| `message` | `str` | Names the fix (`"...install archon-search[code] to enable code graphing"`) when degraded; empty string otherwise. |

**Lazy population, not an eager probe (deliberate — plan Q5 resolution):** this field reflects reality only after a code file with a missing grammar has actually been enriched at least once. It reuses `code_enricher.py`'s existing per-extension "log missing grammar once" cache (`code_enricher.missing_code_parser_extensions()`, backed by the process-global `_GRAMMAR_LOGGED` set) — there is no new eager startup probe or background scan of installed packages. Consequences: (1) on a freshly started process that hasn't ingested any code files yet, `code_parsers.degraded` is `false` even if tree-sitter parsers are actually missing — the gap closes lazily on first code-file ingest; (2) the cache is process-global and never cleared, so a degraded flag set during one ingest persists in `/status` for the lifetime of the process, even if the underlying grammar becomes available later (a `pip install archon-search[code]` requires a process restart to be reflected as `degraded: false`). This tradeoff avoids the cost of an eager tree-sitter import scan on every server startup at the price of a delayed/sticky signal; see `Documentation/Backlog/e2g-code-defref-graph-team-plan.md` for the full rationale.

Implemented in `archon_search/server/routes_status.py` (`_build_code_parsers_status`), `archon_search/server/schemas.py` (`CodeParsersStatusDetail`), and `archon_search/code_enricher.py` (`missing_code_parser_extensions`, `has_missing_code_parsers`).

### `routes_ready.py` (B2; **D6** — `models` check)

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/ready` | Unauthenticated readiness probe for load balancers. `ready: bool` is **storage-only** — `true` (200) when `SearchStore.ping()` succeeds, `false` (503) otherwise. **D6** — `checks` gains a `models: CheckStatus` field driven by the background model-validation result; it does **not** affect `ready` or the HTTP status. | — | `ReadinessResponse` (`schemas.py`) |

`checks.models` mapping (D6, strict priority FAIL > WARN > OK):

| `CheckStatus` | Condition |
| --- | --- |
| `pending` | Background validation has not produced a result yet (`app.state.model_validation` is `None`, or a probe flag is still unset). |
| `fail` | Either `embedder_ok` or `reranker_ok` is `false` (a model could not load). |
| `warn` | Both probes passed but `provider_warnings` is non-empty (provider fallback occurred). |
| `ok` | Both probes passed with no warnings. |

`CheckStatus` is an enum with values `ok`, `fail`, `pending`, `warn` (the latter two added in D6 — see `BREAKING.md`).

### `routes_search.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/search` | Hybrid vector + FTS search over one collection (or a multi-collection fan-out), rerank, ACL filter. | `SearchRequest` (`routes_search.py`) — `{collection?, collections?, query, top_k, filters?, hyde?, rag_fusion?}`. Exactly one of `collection` / `collections` must be supplied (both or neither → `422`). **`top_k` is ignored** at runtime (see BREAKING.md); pipeline uses `config.top_k_return`. The field is validated at `ge=1`; the upper bound is operator-configurable via `top_k_max` in `[search]` TOML (default 100), enforced in the handler body — so `top_k=0` returns `422` (Pydantic), and `top_k>top_k_max` returns `422` (handler). `collection` and `query` are stripped and must be non-empty. `filters` is an optional `SearchFilters` object (see below); omitting it is equivalent to `null`. **C4**: `hyde: bool = false` — when `true` and `[hyde] enabled = true` in config, the ANN lookup uses a HyDE-generated hypothesis embedding; falls back to the original query embedding on any failure. Requires the provider-appropriate package installed (e.g. `archon-search[hyde]` for Anthropic); provider is controlled by `[hyde].provider` in config (default: `"anthropic"`); `422` if package absent. **G10** **C5**: `rag_fusion: bool = false` — when `true` and `[rag_fusion] enabled = true` in config, the server generates N semantic query variants via LLM, searches with each in parallel, and fuses results via second-pass RRF. Requires the provider-appropriate package installed; provider is controlled by `[rag_fusion].provider` in config (default: `"anthropic"`); `422` if package absent. **G10** Mutually exclusive with `hyde`: when both are `true`, RAG Fusion wins and `hyde_applied: false` in the response. **E1a**: `graph_mode: "naive" | null = null` — when `"naive"` and `[graph] enabled = true` in config, `GraphExpander` appends first-degree graph neighbour entity names to the query before embedding (naive graph expansion). Returns `422` when `graph_mode="naive"` and `graph.enabled = false`. Requires `archon-search[graph]` installed. **E2a**: `scope_filter: str \| null = null` — restricts results to chunks whose `scopes` field contains the given tag. Exact match (no `*`): `list_has(scopes, '<value>')` SQL predicate (plus `scopes IS NULL` pass-through for unscoped chunks). Trailing `*` (wildcard): Python-side prefix-match post-filter on the top-k set (unscoped chunks always pass through). Invalid patterns (bare `*`, leading `*`, mid-string `*`, multiple `*`) → 400 `{"detail": {"code": "invalid_scope_filter", "message": "..."}}`. `scope_filter` + any `graph_mode` value → 422. | `SearchResponse` — `{results: [SearchResultSchema{doc_id, chunk_id, text, score, source_path, file_type, language, indexed_at, updated_at, ingested_by, metadata, acl, collection}], acl_filtered: bool, excluded_collections: [{name, reason}], embedding_model: str, hyde_applied: bool, rag_fusion_applied: bool, rag_fusion_queries_used: int, rag_fusion_attempted: bool, expansion_used: bool, expansion_warning: str \| null}`. **C1**: `embedding_model` field added. **C4**: `hyde_applied: bool` — `true` when HyDE hypothesis embedding was actually used. **C5**: `rag_fusion_applied: bool` — `true` when at least one LLM variant was generated and fused; `rag_fusion_queries_used: int` — number of successful variant searches (0..`num_queries`, not counting the original); `rag_fusion_attempted: bool` — `true` when the generator was called. **E0b**: `expansion_used: bool` — `true` when `hyde_applied`, `rag_fusion_applied`, or `graph_expansion_applied` is true (convenience field); `expansion_warning: str \| null` — non-null when expansion was requested but failed. HyDE failures always produce `'HyDE expansion failed'` (all failure modes are indistinguishable at the route level); RAG Fusion failures produce `'RAG Fusion timed out'` (TimeoutError) or `'RAG Fusion expansion failed'` (other exceptions). **E0e**: `applied_filters: SearchFilters \| null` — echoes the parsed, normalised `SearchFilters` from the request (e.g. `file_type: ".md"` → `"md"`); `null` when no filters were submitted. Present on both single-collection and multi-collection responses. **E1a**: `graph_expansion_applied: bool` — `true` when `graph_mode="naive"` was requested, `graph.enabled = true`, and the expander contributed at least one additional term; `false` otherwise (including when the query matched no graph nodes). **E1b**: `graph_mode` extended to `"naive" | "local" | "global" | null`. `"local"`: resolve query entities → find matched community → fetch `representative_chunk_ids[]` → merge with hybrid candidates → rerank. Falls back to naive expansion if entities found but isolated (no community). Falls back to hybrid search if no entities recognized OR communities not built for that collection. `"global"`: fetch ALL community representatives (capped at `max_global_candidates`), rerank against query. Returns `422 {"detail": {"code": "graph_communities_not_built"}}` if communities not built (global mode only). `graph_expansion_applied: bool` in response (already present from E1a). **E2h**: `graph_mode` extended to `"naive" | "local" | "global" | "ppr" | null`. `"ppr"`: Personalised PageRank walk — seeds entity personalization from query N-gram matches and their mention-row counts, runs `networkx.pagerank`, then prepends top-entity-linked chunks before hybrid candidates; falls back to hybrid when no entities match or no mention rows exist. Mutually exclusive with `collections` (multi-collection) and `scope_filter` → `422`. `ppr_entities_matched: int | null` added to `SearchResponse` — count of seed entities with ≥1 mention row (non-null only when `graph_mode="ppr"`). |

Returns `404` when the collection is not visible to the caller's namespace; `503` when meta lookup fails. Pipeline stage exceptions (embedder, store, reranker) return `500` with a plain-text body `Internal Server Error` (Content-Type `text/plain`) — the route bare-re-raises and Starlette's `ServerErrorMiddleware` renders the default response, so this is **not** a JSON envelope and callers must not `.json()`-parse the 500 body. A hung pipeline call returns `504` with `{"detail": "Search timed out"}` after ~30 s. `200` with `results: []` means the pipeline ran successfully but found no matching documents. A malformed `filters` object returns `422`.

Two additive response fields landed with B3 (multi-collection search) and are present on **both** the single- and multi-collection paths: every `SearchResultSchema` now carries `collection` (its origin collection — `""` on pre-B3-shaped rows), and `SearchResponse` now carries `excluded_collections` (empty on the single-collection path). For tolerant JSON consumers these are non-breaking additive keys; see `BREAKING.md` "[next release] — B3 multi-collection search".

#### Multi-collection fan-out (`collections`) — B3

Supply `collections: list[str]` instead of `collection` to fan a single query out across an explicit set of collections in one request. The query is embedded once, each collection is retrieved in parallel, the candidate pools are merged with provenance, and one global rerank pass produces a unified, globally comparable result list. The architecture is in [`120_services_and_integration_architecture.md`](./120_services_and_integration_architecture.md) ("Multi-collection search fan-out"); concurrency/cost in [`210_performance_and_scalability.md`](./210_performance_and_scalability.md).

| Aspect | Behavior |
|---|---|
| Mutual exclusivity | Exactly one of `collection` / `collections` — both or neither → `422`. |
| `collections` validation | 1–`max_fanout` entries (default 8, operator-configurable in TOML; enforced in handler body at request time), per-item stripped + non-empty, deduplicated preserving first-seen order. Empty list, whitespace-only entry, or over-limit → `422`. |
| `filters` + `collections` | **E0e**: Supported. Filters are applied independently to each collection leg in the fan-out (per-leg SQL predicate + per-leg glob post-filter). The response echoes the request filters in `applied_filters`. Previously rejected with `422` in v1. |
| Result provenance | Each result's `collection` field names its origin collection. |
| Excluded collections | `excluded_collections` reports collections dropped from the fan-out (currently reason `embedding_model_mismatch`). If *all* requested collections are excluded, the response is `200` with `results: []` and a fully populated `excluded_collections`. |
| `404` | Any requested collection missing from the caller's namespace → `404` `{"detail": "collection not found"}` (no cross-namespace existence leak; v1 never skips silently). |
| `503` | Metadata-lookup failure → `503` `{"detail": "service unavailable"}`. |
| `504` | Whole-fan-out timeout (`fanout_timeout_seconds`, default 30 s) → `504` `{"detail": "Search timed out"}`. |
| `500` | Any single retrieval leg failing cancels its siblings and surfaces as `500`. |

> **Note**: `collections` is the *execution* surface. Computing which collections to pass is collection-selection intelligence delivered by B4 ([`Documentation/Backlog/B4-stronger-collection-routing-plan.md`](../Backlog/B4-stronger-collection-routing-plan.md)); B4's shortlist feeds B3's `collections` parameter.

#### `SearchFilters` (A2 — `archon_search/filters.py`)

Optional sub-model on `SearchRequest.filters`. All fields are optional; omitting `filters` entirely or setting it to `null` runs an unfiltered search. Model uses `extra="forbid"` — unknown keys return `422`.

| Field | Type | Constraints | Behavior |
| --- | --- | --- | --- |
| `file_type` | `str \| null` | Non-empty; leading dots stripped; lowercased. e.g. `"md"`, `".py"` | SQL `WHERE file_type = '<value>'` |
| `source_path_prefix` | `str \| null` | Non-empty | SQL `WHERE source_path LIKE '<prefix>%' ESCAPE '\\'` |
| `source_path_glob` | `str \| null` | Non-empty; must compile via `fnmatch.translate` | Python-side post-filter via `fnmatch.fnmatchcase`. **No path semantics**: `*` matches `/`; `**` is identical to `*`. |
| `indexed_after` | `datetime \| date \| null` | Date-only strings `YYYY-MM-DD` coerced to midnight UTC | SQL `WHERE indexed_at >= '<fixed-width UTC>'` |
| `indexed_before` | `datetime \| date \| null` | Date-only strings `YYYY-MM-DD` coerced to end-of-day UTC (23:59:59.999999Z). Must be ≥ `indexed_after`. | SQL `WHERE indexed_at <= '<fixed-width UTC>'` |
| `language` | `str \| null` | ISO 639-1 (2-letter) or ISO 639-3 (3-letter) code, or `"unknown"`. Empty string coerced to `null`. Uppercase normalized to lowercase. Values not matching `[a-z]{2,3}` or `"unknown"` rejected with `422`. **E0e**: usable with multi-collection fan-out (REST and MCP). | SQL-side `language = '<code>'` predicate (C2). Excludes chunks in other language states: `language=fr` excludes `""` (untagged) and `"unknown"` chunks; `language=unknown` returns only fasttext-processed-but-below-threshold chunks. |
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
| GET | `/collections/` | List collections visible to caller's namespace. Response includes `active_embedding_model: str` and `needs_reindex: bool` on each `CollectionSummary` entry (C1 — additive). **bug-090** — each entry's `description` now reflects the stored `CollectionMeta.description` (generated by the description generator); `""` when none exists or the collection has no meta row. | — | `list[CollectionSummary]` (`schemas.py`) |
| POST | `/collections/` | Register a path as a collection and enqueue first ingest (returns `202`). **C1**: optional `embedding_model` field — when provided, the collection is initialized with that model as `active_embedding_model`; unknown models return `422`. Returns `400` (`"path is unsafe: <reason>"`) when `path` fails safety validation (empty/whitespace-only/NUL/non-absolute/`..`-traversal); `409` for (a) duplicate resolved path or (b) name collision across namespaces; `503` (body `{"error": "store_busy", ...}`, header `Retry-After: 30`) when a reindex holds the per-collection lock. | `AddCollectionRequest` — `{path, embedding_model?}` | `JobResponse` (`schemas.py`) |
| GET | `/collections/{name}` | Full detail for one collection. **C1**: response gains `active_embedding_model` (renamed from `embedding_model`), `pending_embedding_model` (nullable), `needs_reindex` (bool), `reindex_job_id` (nullable). See `BREAKING.md` C1 entry. **bug-090** — `description` now reflects the stored `CollectionMeta.description`; `""` when none exists. | — | `CollectionDetail` (`schemas.py`) |
| DELETE | `/collections/{name}` | Remove collection (config + LanceDB). `404` for unknown or cross-namespace name; `409` if pinned-only. | — | `DeleteResponse` (`schemas.py`) |
| PATCH | `/collections/{name}` | **C1** — Update the embedding model for a collection. Implements the per-collection model state machine: (a) if the requested model equals the current `active_embedding_model`, clears `pending_embedding_model` and `needs_reindex`; (b) if data exists for the collection and the model differs, sets `pending_embedding_model = requested` and `needs_reindex = true`; (c) if the collection is empty, directly updates `active_embedding_model`. Dimension validation runs before any mutation. Returns `CollectionDetail` (same shape as `GET /collections/{name}`, including the **bug-090** stored `description`). `404` for unknown collection; `409` when a reindex job is already in progress; `422` for unknown/invalid model. **E2a**: also accepts `default_ttl_seconds?: int \| null` to set the collection-level ingest TTL default (forward-only). | `PatchCollectionBody` — `{embedding_model?: str \| null, default_ttl_seconds?: int \| null}`. **E2a**: `embedding_model` is now optional (`str \| null`, default `null`); when omitted or `null`, no embedding-model change is triggered. `default_ttl_seconds?: int \| null` sets the collection-level TTL default; `null` clears it. Forward-only — existing chunks are NOT updated. See `BREAKING.md` for the `embedding_model` optionality change. | `CollectionDetail` (`schemas.py`) |
| POST | `/collections/{name}/reindex` | Start a reindex job (returns `202`). **C1**: uses the collection's `pending_embedding_model` as `target_embedding_model` when set. | — | `JobResponse` |
| GET | `/collections/{name}/documents` | **E0c** — Cursor-paginated list of documents in the named collection. Each page returns `DocumentInfoItem` records (`doc_id`, `source_path`, `chunk_count`, `indexed_at`) sorted by `doc_id` ascending. `limit` defaults to 50, max 200 (Pydantic 422 on out-of-range). `cursor` is the opaque `doc_id` from the previous page's `next_cursor`; omit to start from the beginning. A cursor whose `doc_id` no longer exists (deleted between pages) silently resumes from the first document sorting after that value — no 4xx. `total` is the full document count for the collection independent of the current page. Returns `404` for unknown or cross-namespace collections. | Query: `limit: int = 50` (1–200), `cursor: str \| None = None` | `DocumentListResponse` — `{"items": list[DocumentInfoItem], "next_cursor": str \| null, "total": int}`. **E2a**: each `DocumentInfoItem` gains `scopes: list[str]` (deduplicated set-union of all scope tags across the document's chunks, sorted alphabetically; empty list when no chunks have scopes). |
| GET | `/collections/{name}/expiring` | **E2a** — Cursor-paginated list of chunks expiring within the next `within_hours` hours (`within_hours` ∈ [1, 8760]). Excludes already-expired chunks and never-expiring chunks (null `expires_at`). Sorted by `expires_at` ASC then `chunk_id` ASC. Returns `404` for unknown or cross-namespace collections. | Query: `within_hours: int` (required, 1–8760), `limit: int = 50` (1–200), `cursor: str \| None = None` (opaque composite cursor `"<expires_at_iso>::<chunk_id>"`) | `ExpiringChunksResponse` — `{"items": list[ExpiringChunkItem], "next_cursor": str \| null, "page_count": int}` (page_count: scan-bounded count of items returned in this page, not a full-collection total). Each `ExpiringChunkItem`: `chunk_id`, `doc_id`, `source_path`, `expires_at` (ISO-8601 UTC). |
| GET | `/collections/{name}/migrations/pending` | **D3** — Return pending schema migrations for the named collection. Compares collection's `schema_version` against `STORE_SCHEMA_VERSION`; returns list of unapplied `MigrationSpec` descriptors. Returns `{collection, pending: [MigrationSpec…], schema_version}`. `pending: []` when schema is current. `404` for unknown or cross-namespace collection. Each `MigrationSpec` has `name`, `kind` (`"in_place"`, `"rewrite"`, `"export_rebuild"`), `description`, `introduced_at`. | — | `MigrationPendingResponse` (`schemas.py`) |
| POST | `/collections/{name}/migrate` | **D3** — Apply pending migrations to the named collection. With `dry_run: true` (default `false`), behaves identically to `GET /migrations/pending` (no side effects). For in-place-only migrations, applies synchronously and returns `200` with `{migrations_applied: [name…]}`; no `MigrationJob` is created. For rewrite migrations, requires `backup_confirmed: true` (returns `422` without it), creates a `MigrationJob` (QUEUED), transitions to RUNNING immediately to prevent double-dispatch, and returns `202` with `{job_id, status}`. For `export_rebuild` migrations, always returns `422` (execution deferred to D5). Returns `404` for unknown collection; `409` when a `ReindexJob` is active for the collection. | `MigrateRequest` — `{backup_confirmed: bool = false, dry_run: bool = false}` | `200 MigrateInPlaceResponse` (`{migrations_applied}`) or `202 JobResponse` |

### `routes_jobs.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/ingest` | Submit an ingest job; returns `202` immediately. When `path` is non-null it is validated by `_path_safety.validate_ingest_path`, returning `400` (`"path is unsafe: <reason>"`) on rejection (empty/whitespace-only/NUL/non-absolute/`..`-traversal); `path: null` documents-only ingest is unaffected. Returns `503` (body `{"error": "store_busy", ...}`, header `Retry-After: 30`) when a reindex holds the per-collection lock; ingest to a different collection is unaffected. The `ingested_by` field in the body is always overwritten server-side from the `X-Ingested-By` header (missing → `"http"`; legacy `"archon-search-cli"` → `"cli"`; unknown → `"http"` + WARNING log; truncated to 32 chars). `collection` is rejected only when empty string (whitespace-only is currently accepted; inconsistent with `SearchRequest`). **E0d**: When `body.path` is a single file (not a directory, not a `documents` payload) and `config.ingest.max_file_mb > 0`, a synchronous pre-check runs BEFORE job creation — returning `413` with `{"detail": "File size X MB exceeds the configured limit of Y MB (\`[ingest].max_file_mb\`). Raise the limit in \`archon-search.toml\` or split the file."}` when the file exceeds the limit. Boundary is strictly greater-than (`size > limit`); a file exactly at `max_file_mb` is accepted. Directory paths and `documents` payloads are never checked (no 413 at the route level — oversized files surface as per-file `IngestResult(code="file_too_large")` inside the job). | `IngestRequest` — `{collection, path?, documents?, ingested_by (overwritten), chunk_ttl_seconds?: int \| null, chunk_scopes?: list[str] \| null}`. **E2a**: `chunk_ttl_seconds` ∈ [1, 2^31-1] (0 or negative → 422); `chunk_scopes` 0–100 items × 1–255 chars each (> 100 items or > 255 chars → 422); `chunk_scopes=[]` normalized to `null` by the pipeline. | `202 JobResponse` / `400 ErrorDetail` (unsafe path) / `413 ErrorDetail` (file too large, **E0d**) / `503` (store busy) |
| GET | `/jobs` | **D1/D2** — List all jobs visible to the caller's namespace, sorted by `created_at` descending. Supports cursor-based pagination and filtering by status, job kind, and (D2) source. | Query: `status=` (repeatable, any `JobStatus` value), `kind=` (repeatable: `ingest`, `reindex`, `delete`, `export`, `import`, `migration`), `source=` (repeatable: `user`, `backup`), `limit=50` (max 200), `cursor=` (job_id of the last item from the previous page). | `{"items": list[JobResponse], "next_cursor": str \| null, "total": int}` |
| GET | `/jobs/{job_id}` | Read job status; `404` for cross-namespace IDs. `JobResponse` includes `progress: dict \| null` (D1). **D3**: `JobResponse` gains three new nullable fields: `kind: str \| null` (migration sub-kind), `migrations_applied: list[str] \| null`, `backup_confirmed: bool \| null`. All are `null` for non-migration jobs. See `BREAKING.md` D3 entry. | — | `JobResponse` |
| DELETE | `/jobs/{job_id}` | Cancel a job. Terminal jobs (`DONE`/`FAILED`/`FAILED_EXPIRED`/`CANCELLED`) return `200` (idempotent); `RUNNING`/`PENDING` transition to `CANCELLING` and return `202`; already-`CANCELLING` jobs also return `202`. **E0b**: `FAILED_EXPIRED` is terminal and also returns `200`. | — | `JobResponse` |
| POST | `/jobs/{job_id}/resume` | **D1/D2/D3** — Resume a `FAILED` export, import, or migration job from its last checkpoint. Non-bulk jobs (`IngestJob`, `ReindexJob`, `DeleteJob`) return `409` with `{"error": "job_not_resumable"}`. For export/import: missing archive/tmp file returns `422`. On success, transitions the job to `QUEUED` and returns `202`. **D3**: `MigrationJob` is now also resumable; rewrite migrations restart from the last 100-chunk progress checkpoint. | — | `JobResponse` (status=`QUEUED`) |

### `routes_export.py` (D1/D2)

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/collections/{name}/export` | Start an export job for the named collection. Returns `202` with the new job (status `QUEUED`). The job writes a `.tar.gz` archive to `output_path` (default: `get_data_dir() / "exports"`). `output_path` must be within `get_data_dir()` — paths outside return `400`. Collection not found → `404`. Collection locked (reindex in progress) → `409`. Poll `GET /jobs/{job_id}` for progress. | `{"output_path": str}` (optional; default: `get_data_dir() / "exports"`) | `JobResponse` (status=`QUEUED`) |
| POST | `/collections/{name}/import` | Start an import job from a `.tar.gz` archive. Returns `202` with the new job. Archive must be within `get_data_dir()` (`400` otherwise). Archive not found → `422`. Unsafe tar members → `422`. Schema version mismatch (without `ignore_schema_version=true`) → `422`. Embedding model mismatch → `422` (always; not bypassable). Collection already exists (without `force_overwrite=true`) → `409`. | `{"path": str, "force_overwrite": bool = false, "ignore_schema_version": bool = false, "on_error": "fail"\|"skip" = "fail"}` | `JobResponse` (status=`QUEUED`) |

### `routes_backup.py` (D2)

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/backup/trigger` | **D2** — Trigger an immediate backup pass for every collection in the caller's namespace. Returns `202` with `BackupTriggerResponse` listing the `queued` job IDs and `skipped` collections (each with `reason` in `excluded` / `already_active` / `already_queued`). Jobs are created with `source="backup"` and dispatch via the standard `JobScheduler` behind any user-sourced bulk jobs. Archives land in `{backup.output_dir}/{namespace}/{collection}.backup.{timestamp}.tar.gz`. | — | `BackupTriggerResponse` — `{"queued": list[str], "skipped": list[{"collection": str, "reason": str}]}` |

### `routes_maintenance.py` (D5)

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/maintenance/trigger` | **D5** — Trigger an immediate maintenance pass. Sets `MaintenanceLoop._trigger_event`; the pass runs asynchronously. Returns `{"status": "triggered"}` when enqueued. Returns `{"status": "already_triggered"}` (also `202`) when a pass is already pending or running (`_trigger_event.is_set()`); the in-progress pass completes normally. Requires `Bearer` token. | — | `202 MaintenanceTriggerResponse` — `{"status": "triggered" \| "already_triggered"}` |

The `maintenance` field added to `GET /status` (D5):

`StatusResponse` gains `maintenance: MaintenanceStatusDetail | null`. `null` only when `app.state.maintenance_loop` is absent (non-standard startup). When present, `MaintenanceStatusDetail` contains:

| Field | Type | Notes |
|---|---|---|
| `enabled` | `bool` | `true` when `interval_hours > 0` |
| `interval_hours` | `int` | Configured interval in hours (`0` = disabled) |
| `last_run_at` | `str \| null` | ISO-8601 timestamp of last completed pass |
| `next_run_at` | `str \| null` | ISO-8601 timestamp of next scheduled pass; `null` when disabled |
| `collection_health` | `list[CollectionHealthEntry]` | Namespace-scoped to caller's namespace; each entry is a collection within that namespace |
| `expired_chunk_count` | `int` | **E2a** — Live point-in-time count of chunks with `expires_at < now_utc` in the caller-visible collection tables (not namespace-scoped within shared tables — all tenants in a shared-table collection contribute; see store.py `count_expired_chunks` docstring for the caveat). Always an integer (never null); 0 when no expired chunks exist. Resets as chunks are pruned or new TTL-expiry occurs. Not a prune-run delta — reflects the live store state at call time. |
| `last_expired_pruned_at` | `str \| null` | **E2a** — ISO-8601 UTC timestamp of the last successful expired-chunk prune pass; `null` until the first prune pass runs (i.e. until a maintenance pass with `prune_expired_chunks = true` completes). |

Each `CollectionHealthEntry` (namespace is implied by the caller's API key; the key in the state file is `{ns}/{col}` and the `collection` field holds just the bare `{col}` part):

| Field | Type | Notes |
|---|---|---|
| `collection` | `str` | Collection name (bare, without namespace prefix) |
| `fts_optimized_at` | `str \| null` | Last FTS optimize timestamp |
| `orphans_removed_last_run` | `int` | Orphaned source paths removed in the last pass |
| `last_retry_at` | `str \| null` | Last failed-ingest retry timestamp |
| `last_error` | `str \| null` | Most recent per-collection error; `null` when last pass was clean |
| `meta_chunk_count` | `int` | O(1) metadata-row chunk count |
| `mutations_since_recompute` | `int` | Mutations since last centroid recompute |
| `centroid_recompute_threshold` | `int` | Current threshold from `config.centroid_recompute_threshold` |

### `routes_keys.py` (D7)

All paths under `/keys`. Require `Bearer` token (same auth as all other routes). Manage the durable multi-key store (`KeyStore` in `key_manager.py`).

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/keys` | Issue a new managed API key. Returns `201` with the new key ID, raw token (printed once — never retrievable again), namespace, label, created_at, expires_at, and `status: "active"`. | `KeyCreateRequest` — `{namespace: str (required), label?: str, expires_at?: ISO-8601 datetime (timezone-aware)}` | `KeyCreateResponse` (`schemas.py`) — `{id, token, namespace, label, created_at, expires_at, status: "active"}` |
| GET | `/keys` | List keys visible to the caller's namespace. Active-only by default; `status=all` or `status=revoked` to filter. `namespace=` query param scopes the list to a specific namespace. Response includes `hidden_revoked_count` (count of revoked keys in the scoped view, omitted from the default active-only view). TOML synthetic keys (no `id`) appear with `id: null`. | Query: `status=` (`active` default, `revoked`, `all`), `namespace=` | `KeyListResponse` — `{keys: [KeyResponse…], hidden_revoked_count?: int}` |
| DELETE | `/keys/{id}` | Revoke a managed key by ID. Idempotent — already-revoked returns `200`. `404` for nonexistent IDs. `DELETE /keys/null` (TOML synthetic key) returns `404` with a message explaining the TOML lifecycle. | — | `KeyRevokeResponse` — `{id, status: "revoked"}` |
| POST | `/keys/rotate` | Generate a new default API key. Writes the new token to `.search.env` via `atomic_write_bytes`. Revokes (or grace-expires) the old key in `keys.json`. Returns the new token once. Returns `409` when `ARCHON_SEARCH_API_KEY` env var is set (rotation would be a silent no-op). | `KeyRotateRequest` — `{grace_seconds?: int}` (overrides `[auth].rotate_grace_seconds` TOML default) | `KeyRotateResponse` (`schemas.py`) — `{new_key_id, token, status: "active", old_key_id?, old_key_expires_at?, old_key_status?}` |

`KeyResponse` fields: `id: str | null`, `namespace: str`, `label: str | null`, `created_at: str`, `expires_at: str | null`, `status: "active" | "revoked"`. The `token` field is **absent** from `KeyResponse` and `KeyListResponse` (only present on `KeyCreateResponse` and `KeyRotateResponse`).

**Known limitations**: `POST /keys/rotate` blocked when `ARCHON_SEARCH_API_KEY` env var is set (returns `409`). MCP `rotate_key` updates `keys.json` and `.search.env` but does not hot-reload the MCP server's legacy `api_key` — the old default key remains valid for the MCP path until restart. TOML `[namespaces]` tokens require a server restart to take effect; they cannot be targeted by `DELETE /keys/{id}`.

### `routes_explain.py` (A4)

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/explain` | Return the full per-stage retrieval/reranking score breakdown for a query, plus the routing decision when no collection is pinned. Debug endpoint for understanding why results appear (or don't). | `ExplainRequest` (`routes_explain.py`) — `{query, collection?, collections?, top_k(1–top_k_max, default 5; top_k_max defaults to 100 and is operator-configurable in TOML), rerank(default true), hyde(default false), rag_fusion(default false), graph_mode(null)}`. Extra fields are **rejected** (`extra="forbid"`). `query` must be non-empty. **C4**: `hyde: bool = false` — same semantics as `SearchRequest.hyde`. **C5**: `rag_fusion: bool = false` — same semantics as `SearchRequest.rag_fusion`; mutually exclusive with `hyde`. **E1c**: `graph_mode: "naive" \| "local" \| "global" \| null = null` — when non-null and `[graph] enabled = true`, uses graph-aware retrieval for the explain pipeline; `graph_mode` is mutually exclusive with `collections` (multi-collection fanout); `graph_mode` wins over `hyde` (HyDE skipped). | `ExplainResponse` — see schema below. **C4**: `ExplainResponse` gains `hyde_applied: bool`. **C5**: `ExplainResponse` gains `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, `rag_fusion_attempted: bool`, `rag_fusion_failure_reason: str \| null`, `rag_fusion_sub_queries: list[{variant_index, result_count, top_doc_ids}] \| null`. **E1c**: `ExplainResponse` gains `graph_mode_applied: "naive" \| "local" \| "global" \| null`; each `ExplainResult` in `results[]` gains `graph_provenance: GraphProvenanceResponse \| null` (non-null only for graph-retrieved results; standard hybrid candidates carry `null`); `near_misses[]` items structurally omit `graph_provenance`. **E2h**: `graph_mode_applied` extended to `"naive" \| "local" \| "global" \| "ppr" \| null`; `ExplainResponse` gains `ppr_entities_matched: int \| null` (non-null when `graph_mode="ppr"`). |

All schemas in `routes_explain.py` use `extra="forbid"`; unknown fields produce `422`.

**Request fields:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | required | Must be non-empty after stripping. |
| `collection` | `str \| null` | `null` | Pin to a specific collection. Mutually exclusive with `collections`; omit both to invoke routing. |
| `collections` | `list[str] \| null` | `null` | Multi-collection fan-out (B3): 1–8 entries, stripped + non-empty, deduplicated. Routing is bypassed. Mutually exclusive with `collection` (both → `422`). |
| `top_k` | `int` | `5` | Results to return; `ge=1` (Pydantic). Upper bound is `top_k_max` (default 100, operator-configurable in `[search]` TOML), enforced in the handler body. |
| `rerank` | `bool` | `true` | Run cross-encoder reranker. `false` → scores sorted by `rrf_score`. With `collections` of length > 1, `rerank=false` is rejected with `422` (`"reranking cannot be disabled for multi-collection search in v1"`); a single-item `collections` list with `rerank=false` is allowed (treated like the single-collection path). |
| `hyde` | `bool` | `false` | **C4** — Use HyDE hypothesis embedding for the ANN lookup leg. Requires `[hyde] enabled = true` in config and the provider-appropriate package installed (`archon-search[hyde]` for Anthropic, `archon-search[ollama]` for Ollama, `archon-search[openai-provider]` for OpenAI). Provider and required API key are controlled by `[hyde].provider` in config (default: `"anthropic"`). Falls back silently to the standard query embedding on any failure; `hyde_applied: false` in the response reflects the fallback. **G10** |
| `rag_fusion` | `bool` | `false` | **C5** — Generate N semantic query variants via LLM, search with each, and fuse results via second-pass RRF. Requires `[rag_fusion] enabled = true` in config and the provider-appropriate package installed. Provider and required API key are controlled by `[rag_fusion].provider` in config (default: `"anthropic"`). Falls back silently on any failure; `rag_fusion_applied: false` in the response. Mutually exclusive with `hyde`: when both are `true`, RAG Fusion wins and `hyde_applied: false`. **G10** |
| `graph_mode` | `"naive" \| "local" \| "global" \| "ppr" \| null` | `null` | **E1c** — When non-null and `[graph] enabled = true`, uses graph-aware retrieval for the explain pipeline. `"naive"`: expands query via first-degree graph neighbours, then standard retrieval with per-candidate `GraphProvenance`. `"local"`: fetches community-representative chunks for matched entities. `"global"`: aggregates community-representative chunks across top-N communities. **E2h**: `"ppr"`: Personalised PageRank walk — same walk as `POST /search`; per-chunk `graph_provenance` carries PPR-scored entity steps. Mutually exclusive with `collections` (multi-collection) and `scope_filter` — returns `422` if either is set alongside `graph_mode`. When `graph_mode` is non-null, HyDE is suppressed regardless of the `hyde` field value. Returns `422` when `graph.enabled = false`. |
| `scope_filter` | `str \| null` | `null` | **E2a** — Restricts explain candidates to chunks whose `scopes` field contains the given tag. Same matching semantics as `POST /search`: exact or trailing-`*` wildcard; unscoped chunks always pass through. Invalid patterns (bare `*`, leading `*`, mid-string `*`, multiple `*`) → 400 `{"detail": {"code": "invalid_scope_filter", "message": "..."}}`. `scope_filter` + any `graph_mode` value → 422. |

**Response schema (`ExplainResponse`):**

```json
{
  "rerank": true,
  "routing": {
    "invoked": true,
    "chosen_collection": "docs",
    "confidence_threshold": 0.30,
    "chosen_below_threshold": false,
    "candidates": [
      {"collection": "docs", "centroid_score": 0.83},
      {"collection": "code", "centroid_score": 0.61}
    ]
  },
  "collection": "docs",
  "acl_filtered": false,
  "results": [
    {
      "doc_id": "abc123...",
      "chunk_id": "abc123...-000000",
      "source_path": "/path/to/doc.md",
      "text": "...",
      "score": 0.91,
      "breakdown": {
        "vector_rank": 1,
        "vector_score": 0.74,
        "vector_score_kind": "distance",
        "fts_rank": 3,
        "fts_score": 4.2,
        "fts_score_kind": "bm25",
        "rrf_score": 0.032,
        "reranker_score": 0.91
      },
      "file_type": "md",
      "indexed_at": "2026-05-20T12:00:00Z",
      "updated_at": "2026-05-20T11:00:00Z",
      "ingested_by": "cli",
      "language": "",
      "metadata": {},
      "acl": null,
      "collection": "docs"
    }
  ],
  "near_misses": [
    {
      "doc_id": "...",
      "chunk_id": "...",
      "source_path": "...",
      "score": 0.42,
      "breakdown": { "...same shape as results[].breakdown..." },
      "file_type": "md",
      "indexed_at": "...",
      "updated_at": "...",
      "ingested_by": "cli",
      "language": "",
      "metadata": {},
      "acl": null,
      "collection": "docs"
    }
  ],
  "excluded_collections": [],
  "stage_timings_ms": {
    "embed": 4.2,
    "route": 1.1,
    "vector": 8.7,
    "fts": 3.3,
    "fuse": 2.1,
    "rerank": 12.5
  },
  "hyde_applied": false,
  "rag_fusion_applied": false,
  "rag_fusion_queries_used": 0,
  "rag_fusion_attempted": false,
  "rag_fusion_failure_reason": null,
  "rag_fusion_sub_queries": null,
  "graph_mode_applied": null,
  "ppr_entities_matched": null
}
```

**Key schema notes:**

- `routing` is `null` when `collection` is pinned in the request. When collectionless, it carries the full routing decision including all caller-namespace collections — the confidence-threshold gate is **bypassed** so every collection in the namespace appears in `candidates`, sorted by `centroid_score` descending with alphabetical tie-break. `centroid_score` is `null` for collections with a mismatched embedding model or no centroid.
- `results[]` carry `text`; `near_misses[]` structurally **omit `text`** (the `ExplainNearMiss` Pydantic model has no `text` field). Near-misses are capped at 20.
- **E1c / E2h**: `graph_mode_applied: "naive" | "local" | "global" | "ppr" | null` — set to the mode the pipeline attempted when `graph_mode` was requested (even when no graph candidates were found). `null` when `graph_mode` was not set. **E2h**: `ppr_entities_matched: int | null` — non-null only when `graph_mode="ppr"`; count of seed entities with ≥1 mention row (0 = no match, fell back to hybrid).
- **E1c**: `results[].graph_provenance` — `GraphProvenanceResponse | null`. Non-null only for candidates that were retrieved via graph traversal. Standard hybrid-search candidates (blended alongside graph candidates) carry `null`. Near-misses never include `graph_provenance` (structurally absent from `ExplainNearMiss`).
- **E1c** `GraphProvenanceResponse` schema: `{steps: [TraversalStepResponse…]}`. `TraversalStepResponse` fields: `entity: str` (required), `entity_id: str` (required), `relationship: str | null`, `community_id: str | null`, `chunk_id: str | null`. Model validator enforces that at least one of `relationship`, `community_id`, or `chunk_id` is non-null — an all-null step is rejected at the wire boundary.
- Both `ExplainResult` and `ExplainNearMiss` carry a `collection` field (B3) naming the origin collection (`""` on the single-collection path). The top-level response also carries `excluded_collections` (`[{name, reason}]`), empty except on a multi-collection fan-out. On the multi-collection path `routing` is `null` and the top-level `collection` is `""` (routing is bypassed; provenance is per-result instead).
- `score` is `reranker_score` when `rerank=true`, otherwise `rrf_score`. `breakdown.reranker_score` is `null` when `rerank=false`.
- `vector_score_kind` is `"distance"` (LanceDB cosine distance — lower is closer). `fts_score_kind` is `"bm25"` when the score is available; `null` when LanceDB omits `_score` from the row.
- Metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, `metadata`, `acl`) are a **superset of `/search`** — they appear on both `results[]` and `near_misses[]`.
- The input `query` is **never echoed** in the response body or in telemetry.
- ACL filtering applies identically to `/search`; filtered results are excluded from both `results` and `near_misses`. Collection visibility in `routing.candidates` is bounded by the caller's namespace (the same ACL boundary that gates `results`).
- `stage_timings_ms` is a `dict[str, float]` of stage name → elapsed milliseconds (blocked-coroutine wall time). It is present when `[observability].stage_timings_enabled = true` (the default) and absent when timings are disabled. Clients using strict schema validation (e.g., Pydantic with `extra="forbid"`) must account for this field when timings are enabled. See `BREAKING.md` for the compatibility note.

**Error taxonomy:**

| Condition | Status | Body |
|---|---|---|
| Empty query / `top_k < 1` / extra request fields / both `collection` and `collections` set / `rerank=false` with > 1 `collections` | `422` | Pydantic validation detail (structural) |
| `top_k > top_k_max` (default 100, operator-configurable) / `collections` length > `max_fanout` (default 8, operator-configurable) | `422` | Handler string detail: `{"detail": "..."}` |
| `graph_mode` non-null and `[graph] enabled = false` | `422` | `{"detail": "graph_mode requires [graph] enabled=true in server config"}` (plain string, no `code` field) |
| `graph_mode` non-null and `collections` (multi-collection) supplied | `422` | `{"detail": "graph_mode is not supported with multi-collection fanout; use a single collection"}` (plain string, no `code` field) |
| `graph_mode="local"` or `"global"` and communities not yet built for the collection | `422` | `{"detail": {"code": "graph_communities_not_built", "message": "..."}}` |
| Pinned `collection` not found, or any requested `collections` entry not in namespace | `404` | `{"detail": "collection not found"}` |
| Multi-collection fan-out timeout (`fanout_timeout_seconds`) | `504` | `{"detail": "Search timed out"}` |
| Collectionless + no collections in namespace | `404` | `{"detail": "no collections available"}` |
| Meta-lookup or router failure | `503` | `{"detail": "service unavailable"}` |
| Pipeline-stage failure (store / reranker) | `500` | `{"detail": "<stage> error: <ExceptionType>"}` |
| Other unexpected failure | `500` | `{"detail": "explain failed"}` |

503 is reserved for meta-lookup / router failures, consistent with A3's `/search` taxonomy. Pipeline-stage failures (store, reranker) surface as 500 with a stage-specific detail; the original exception message is sanitised server-side because FTS errors may echo the query.

### `routes_graph.py` (E2b / E2c / E2g)

Graph inspection and impact endpoints; only available when `[graph] enabled = true` in `archon-search.toml`. Require `Bearer` token. All endpoints resolve the collection(s) within the **authenticated namespace** (`request.state.namespace`) — collections owned by other namespaces return `404` (**E2c** namespace fix).

| Method | Path | Purpose | Query params | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/graph/{collection}` | Return node and edge list for a single collection, scored by the chosen salience mode. Returns `422` when `[graph] enabled = false`. Returns `404` when the collection does not exist in the caller's namespace. **E2c**: gains `?salience=` parameter. **E2g**: gains `"importance"` sort mode (persisted PageRank, nulls-last). | `format?: "json" \| "graphml"` (default `"json"`); `salience?: "frequency" \| "tfidf" \| "importance"` (default `"frequency"`) — `"tfidf"`: TF-IDF scored; `"frequency"`: raw chunk frequency (default); `"importance"`: persisted PageRank score, nulls-last. `?salience=<other>` → `422`. | `GraphInspectionResponse` — `{nodes: [GraphNodeResponse{entity_id, entity_name, chunk_count, salience, entity_type}], edges: [GraphEdgeResponse{edge_id, source_entity_id, target_entity_id, weight, source_chunk_ids, relationship_type, extraction_method}], node_count, edge_count, truncated: bool, salience_mode: "frequency" \| "tfidf" \| "importance"}`. **E2c**: `salience_mode` field always present. **E2f**: `GraphEdgeResponse` gains `relationship_type: str = "related_to"`; synonym edges return `"synonym_of"`. Synonym edges and their endpoint nodes are exempt from truncation caps (`max_nodes`, `max_edges`). **E2j**: `GraphNodeResponse` gains `entity_type: str` (e.g. `"person"`, `"concept"`, `"system"`, `"event"`, `"code_symbol"`). When `format=graphml`, returns `application/xml`. |
| GET | `/graph/{collection}/view` | **E2j BE-2** — Serve a self-contained HTML graph viewer page for a collection. Substitutes `__ARCHON_COLLECTION__`, `__ARCHON_TOKEN__`, `__ARCHON_MAX_NODES__`, `__ARCHON_MAX_EDGES__` placeholders in `graph_viewer.html`. Auth: `Authorization: Bearer` header (standard middleware, takes priority) or `?token=` query param (middleware exemption — active only when no `Authorization` header is present; handler validates via `validate_token_and_get_namespace`). Returns `422` when `[graph] enabled = false`. Returns `404` when the collection does not exist in the caller's namespace. Returns `401` on missing or invalid auth (with `WWW-Authenticate: Bearer` header). | `token?: str` — optional Bearer token via query param when no `Authorization` header is present. | `200 text/html` — self-contained HTML page (no external CDN dependencies); `Cache-Control: no-store`; `Referrer-Policy: no-referrer`. |
| GET | `/graph/cross-collection` | Return merged node and edge list across ≥2 collections. Requires `?collections=` with ≥2 comma-separated names. Returns `422` when `[graph] enabled = false` or fewer than 2 valid collection names are provided. **E2c**: gains `?salience=` parameter; IDF denominator is namespace-scoped. **E2g**: gains `"importance"` sort mode. | `collections: str` (required; comma-separated, ≥2 names); `format?: "json" \| "graphml"` (default `"json"`); `salience?: "frequency" \| "tfidf" \| "importance"` (default `"frequency"`) | `CrossCollectionGraphInspectionResponse` — `{collections: [str], nodes: [...], edges: [...], node_count, edge_count, truncated: bool, salience_mode: "frequency" \| "tfidf" \| "importance"}`. |
| GET | `/graph/{collection}/impact/{symbol}` | **E2g BE-9** — Blast-radius (caller/callee) impact analysis for a code symbol. Returns callers and callees separated, depth-capped (hard max 5), PageRank-ordered. Returns `422` when `[graph] enabled = false`, `404` when collection absent. | `file_path?: str` (disambiguates same-named symbols; omit to default to highest-PageRank match); `depth?: int` (default `2`, hard cap `5`); `direction?: "callers" \| "callees" \| "both"` (default `"both"`); `extraction_method_filter?: str` (e.g. `"extracted"` — pre-traversal filter; excluded edge types are never followed) | `GraphImpactResponse` — `{callers: ImpactGroup{direct: [ImpactEdge], indirect: [ImpactEdge], truncated: bool, omitted_count: int}, callees: ImpactGroup{...}, depth_used: int}`. `ImpactEdge` fields: `entity_id`, `entity_name`, `relationship_type`, `extraction_method`, `depth`. |
| POST | `/graph/{collection}/rebuild-communities` | **GBC110** — Enqueue an async Leiden community rebuild for a collection, mirroring `POST /collections/{name}/migrate`'s create-job-then-`202` shape. Namespace is derived from the Bearer token (`request.state.namespace`), not a request field. Returns `422` when `[graph] enabled = false` (verbatim `routes_graph.py` detail, same string as the other guards in this table) or when `graph_store` failed to connect at startup despite `enabled=true`. Returns `404` when the collection does not exist in the caller's namespace. Returns `409` (`"community rebuild already in progress for this collection"`) when `CollectionMeta.community_rebuild_job_id` points at a still-active job — a stale id (job missing or terminal) is lazily cleared and the request proceeds. Concurrent rebuilds for the same `(namespace, collection)` — including a `MaintenanceLoop` GC-triggered rebuild — serialise via a module-level lock registry in `community_builder.py`; this does not block ingest on the collection. | — (no body; no query params) | `202` — full `JobResponse` body (`job_to_dict(running_job)`), `status: "RUNNING"` (never `"QUEUED"`/`"PENDING"` — the route transitions before returning). On `DONE`, `GET /jobs/{id}` reports `result: {"communities_built": N}`. On `FAILED` (e.g. missing `leidenalg`/`igraph`, or zero graph nodes), `error` carries the message. |

**`GraphNodeResponse` field semantics** (frequency vs. tfidf vs. importance mode):

| Field | frequency mode | tfidf mode | importance mode |
|---|---|---|---|
| `chunk_count` | mentions count from mentions table (or 0 if pre-E2b) | same | same |
| `salience` | `chunk_count / total_chunks_in_collection`, clamped `[0.0, 1.0]` | `TF × max(IDF, 0), ≥ 0` | persisted `pagerank_score` (`null` for symbols not yet recomputed) |
| `entity_type` | string value from `EntityType` enum (e.g. `"person"`, `"concept"`, `"system"`, `"event"`, `"code_symbol"`) — **E2j** | same | same |
| sort key | `(-chunk_count, entity_id)` | `(-salience, entity_id)` | `(-pagerank_score, entity_id)` (nulls-last) |

where `N` = total namespace collection count, `df` = count of namespace collections containing the entity.

For cross-collection inspection (`GET /graph/cross-collection`), entities present in multiple listed collections have their chunk counts summed across those collections. In frequency mode, the merged salience is a chunk-count-weighted average across those collections. In tfidf mode, this merged frequency salience is then multiplied by the entity's IDF factor (which is computed using all namespace collections as the denominator, not just the listed ones). In importance mode, per-collection `pagerank_score` values are averaged across listed collections (or the highest value when only one collection has a score).

**Truncation**: nodes are sorted and truncated by the mode-appropriate key before applying `max_inspection_nodes` / `max_inspection_edges` config ceilings. `truncated: true` signals that the cap was hit.

### `routes_openai_shim.py` (G9)

OpenAI-compatible shim endpoints, registered only when `[openai_shim] enabled = true` in `archon-search.toml`. When disabled, no `/v1` routes are registered — requesting `/v1/*` returns a plain FastAPI `404 {"detail": "Not Found"}`. All enabled endpoints require the same `Bearer` token as all other Archon routes. Error responses on `/v1/*` always use the OpenAI error envelope (`{"error": {"message": ..., "type": ...}}`); the `OpenAI401Middleware` rewrites bodyless 401 responses to this shape.

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/v1/models` | Return one `ModelObject` per namespace-visible collection plus the `archon-search` catch-all. The response always contains at least the catch-all entry. | — | `ModelList` — `{object: "list", data: [ModelObject{id, object: "model", created: 0, owned_by: "archon-search"}, …]}`. Catch-all id is `"archon-search"`; per-collection ids are `"archon-search/{name}"`. `401` returns OpenAI error shape. |
| POST | `/v1/chat/completions` | Extract last `role="user"` message as search query; route by `model` field; return top-k chunks as assistant reply. `model="archon-search"` fans out across all namespace collections (capped at `config.max_fanout`; excess silently omitted with WARNING). `model="archon-search/{col}"` targets one collection directly. Any other `model` value → 404. | `ChatCompletionRequest` (`schemas_openai.py`) — `{model: str, messages: [ChatMessage{role, content: str}], stream: bool = false, top_k?: int}`. `top_k` is accepted but has no effect (pipeline uses its construction-time setting). Array content in `messages[].content` is not supported (422). | Non-streaming: `ChatCompletionResponse` — `{id: "chatcmpl-{uuid4}", object: "chat.completion", created: int, model: str, choices: [{index: 0, message: {role: "assistant", content: str}, finish_reason: "stop"}], usage: {prompt_tokens: 0, completion_tokens: 0, total_tokens: 0}}`. Streaming (`stream=true`): `text/event-stream`; one `data: {...}` SSE frame per chunk with `object="chat.completion.chunk"`, `delta.role="assistant"` on first frame only; stop frame (`delta={}`, `finish_reason="stop"`); `data: [DONE]`. Citations (when `inject_citations=true`, the default): each chunk wrapped as `\n\nContext:\n{text}\n[Source: {source_path}]`. Zero results: `content=""`, `finish_reason="stop"`. Error shapes: `404` (unknown collection or model), `422` (no user message or schema error), `503` (pipeline / metadata error), `504` (timeout), `401` (auth failure — via `OpenAI401Middleware`). |

### `routes_telemetry.py`

When telemetry is disabled, both endpoints return `DisabledResponse` (`schemas_telemetry.py`) — `{enabled: false}`.

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| GET | `/telemetry/stats` | Aggregated stats over a date window. Returns `400` on invalid date ordering (from `reader.resolve_dates`). **E0b**: Response gains `truncated_count: int` — count of log entries where `truncated=True` (telemetry entry exceeded 8 KB and had its `result_doc_ids` trimmed by the writer since D8). `0` when no entries were truncated in the window. Legacy entries written before E0b have `truncated=None` (not counted). | Query: `since`, `until` | `StatsResponse` \| `DisabledResponse` (`schemas_telemetry.py`) |
| GET | `/telemetry/entries` | Paginated raw entries with filters. Returns `400` on invalid date ordering. | Query: `since`, `until`, `collection`, `endpoint`, `status`, `error_kind`, `offset`, `limit` (1..200) | `EntriesResponse` \| `DisabledResponse` |

## MCP tools

Defined in `archon_search/server/mcp.py` via `FastMCP`. **D9** — the HTTP transport is now mounted at `/mcp` on the same FastAPI app and port as REST (default `8765`); the mount happens inside the app lifespan when `[mcp].enabled = true` (the default) and never blocks REST startup if it fails. The sub-app is wrapped with the same `APIKeyMiddleware` (sharing managed keys, TOML `[namespaces]` tokens, and the default key); only `/health` is exempt. **20 tools total** — the 16 retrieval/collection/document/export/graph tools always register; the 4 key-management tools (`create_key`, `list_keys`, `revoke_key`, `rotate_key`, D7) register only when a `key_store` is present.

| Tool name | Purpose | Arguments | Returns |
| --- | --- | --- | --- |
| `search` | Hybrid vector + FTS search (single-collection or multi-collection fan-out), rerank, ACL filter. | `query: str`, `collection: str \| None`, `collections: list[str] \| None` (B3 — fan-out; exactly one of `collection` / `collections`, else falls back to `default_collection`; 1–8 entries, stripped + deduped), `include_metadata: bool = false`, `file_type: str \| None`, `source_path_prefix: str \| None`, `source_path_glob: str \| None`, `indexed_after: str \| None`, `indexed_before: str \| None`, `language: str \| None` (**C2** — ISO 639-1 or ISO 639-3 code, e.g. `"fr"`, `"de"`, or `"unknown"`; **E0e**: usable with multi-collection fan-out), `hyde: bool = false` (**C4** — HyDE hypothesis embedding; requires `archon-search[hyde]` + `[hyde] enabled = true` + provider-appropriate API key (none for ollama; see `[hyde].provider` config)), `rag_fusion: bool = false` (**C5** — multi-query decomposition; requires `archon-search[rag_fusion]` + `[rag_fusion] enabled = true` + provider-appropriate API key (see `[rag_fusion].provider` config); mutually exclusive with `hyde`), `graph_mode: str \| null = null` (**E1a** — `"naive"` enables first-degree graph neighbour expansion before embedding; requires `[graph] enabled = true` and `archon-search[graph]`; **E1b**: extended to accept `"local"` and `"global"` with same semantics as REST `POST /search`), `scope_filter: str \| null = null` (**E2a** — same matching semantics as REST `POST /search`; `scope_filter` + any `graph_mode` value returns an error) | `{"results": [SearchResult...], "acl_filtered": bool, "excluded_collections": [{name, reason}], "hyde_applied": bool, "expansion_used": bool, "expansion_warning": str \| null}` — **C4**: `hyde_applied` added. **C5**: RAG Fusion inputs are supported but `rag_fusion_applied`/`rag_fusion_queries_used`/`rag_fusion_attempted` are NOT in the MCP response (unlike REST `POST /search`); `McpSearchResponse` is a narrower schema. **E0b**: `expansion_used`, `expansion_warning` added (same semantics as REST `POST /search`). **E0e**: `applied_filters` is NOT in the MCP response (`McpSearchResponse` does not carry this field; see REST `POST /search` `SearchResponse` — MCP response echoing is deferred). **E1a**: `graph_expansion_applied: bool` is present in `McpSearchResponse` (same semantics as REST `POST /search`). **E2h**: `graph_mode` accepts `"ppr"` (same semantics as REST); `ppr_entities_matched: int | null` added to `McpSearchResponse`. On validation error: `{error, code: "validation_error"}`. On internal error: `{error, code: "internal_error"}`; collection not found: `{error, code: "not_found"}`. |
| `search_with_context` | Search plus surrounding chunks for each hit. | `query`, `collection?`, `context_window: int = 1`, `include_metadata: bool = false`, `file_type: str \| None`, `source_path_prefix: str \| None`, `source_path_glob: str \| None`, `indexed_after: str \| None`, `indexed_before: str \| None`, `language: str \| None` (**C2** — ISO 639-1 or ISO 639-3 code or `"unknown"`; single-collection only), `hyde: bool = false` (**C4**), `rag_fusion: bool = false` (**C5**), `graph_mode: str \| null = null` (**E1a** — accepted in tool signature but always returns an error), `scope_filter: str \| null = null` (**E2a** — same matching semantics as REST `POST /search`; applied before context expansion) | **C4 breaking change**: returns `{"results": [{result, context_before, context_after}, …], "hyde_applied": bool}` — no longer a bare list. See `BREAKING.md`. **C5**: RAG Fusion inputs are supported but `rag_fusion_applied`/`rag_fusion_queries_used`/`rag_fusion_attempted` are NOT in `SearchWithContextResponse` (MCP schema is narrower than REST). **E0b**: response gains `"expansion_used": bool`, `"expansion_warning": str \| null` (same semantics as REST `POST /search`). **E1a / E2h**: `graph_mode` is not supported for `search_with_context` — when non-null, returns `{"error": "graph_mode (naive, local, global, ppr) on search_with_context is not supported; use the search tool instead", "code": "graph_mode_not_supported"}` immediately without searching. On error: `{error, code}`. |
| `explain` | Return the per-stage retrieval/reranking trace for a query, plus the routing decision when no collection is pinned. Operates in the caller's authenticated namespace (D9 — resolved per-request via `_get_request_namespace()`). The query is never echoed in the response or telemetry. | `query: str`, `collection: str \| None`, `collections: list[str] \| None` (B3 — multi-collection fan-out; routing bypassed; `rerank=false` with > 1 collection → `{error, code: "validation_error"}`), `top_k: int = 5`, `rerank: bool = True`, `hyde: bool = false` (**C4**), `rag_fusion: bool = false` (**C5** — same semantics as search; mutually exclusive with `hyde`), `graph_mode: str \| null = null` (**E1c / E2h** — `"naive"`, `"local"`, `"global"`, or `"ppr"`; same semantics as REST `POST /explain`; validated against `_VALID_GRAPH_MODES`; returns `{error, code: "graph_disabled"}` when `graph.enabled = false`; returns `{error, code: "graph_mode_multi_collection_unsupported"}` when `collections` is also set), `scope_filter: str \| null = null` (**E2a** — same matching semantics as REST `POST /explain`; `scope_filter` + any `graph_mode` value returns an error) | `ExplainResponse` dict (same structure as REST `POST /explain`; serialised via `model_dump(mode="json", exclude_none=False)`). **C4**: `hyde_applied: bool` added. **C5**: `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, `rag_fusion_attempted: bool`, `rag_fusion_failure_reason: str \| null`, `rag_fusion_sub_queries: list \| null` added. `results`/`near_misses` entries carry a `collection` key and the response carries `excluded_collections` (B3). Includes `stage_timings_ms` when `[observability].stage_timings_enabled = true`. **E1c**: `graph_mode_applied: "naive" \| "local" \| "global" \| null` added; each `results[]` entry includes `graph_provenance: {steps: [...]} \| null` (non-null for graph-retrieved results only). **E2h**: `graph_mode_applied` extended to include `"ppr"`; `ppr_entities_matched: int \| null` added (non-null when `graph_mode="ppr"`). On error: `{error, code}`. When `config` is absent from `create_app`, collectionless calls fall back to `default_collection` (no routing). |
| `ingest_file` | Ingest one file, result validated through `IngestResultSchema` (excludes `needs_recompute`). | `path: str`, `collection?`, `chunk_ttl_seconds?: int \| null` (**E2a** — ∈ [1, 2^31-1]; 0 or negative → error), `chunk_scopes?: list[str] \| null` (**E2a** — 0–100 items × 1–255 chars each; empty list normalized to `null`) | `IngestResultSchema dict` — fields: `doc_id`, `chunks_created`, `status`, `error`, `warnings: list[str]` (**E0b** — non-fatal ingest warnings, e.g. oversized ACL sidecar), `code: str \| null` (**E0d** — `"file_too_large"` when the size guard fires, `null` otherwise). On unsafe `path`: `{error, code: "path_unsafe"}`; when a reindex holds the lock: `{error, code: "store_busy"}`; when file exceeds `max_file_mb`: `{status: "error", code: "file_too_large", error: "<actionable message>"}`. |
| `ingest_directory` | Ingest a directory tree (reports progress via `ctx`), each result validated through `IngestResultSchema`. | `path`, `glob_pattern = "**/*"`, `collection?`, `chunk_ttl_seconds?: int \| null` (**E2a** — ∈ [1, 2^31-1]; 0 or negative → error), `chunk_scopes?: list[str] \| null` (**E2a** — 0–100 items × 1–255 chars each; empty list normalized to `null`) | `list[IngestResultSchema dict]` (each entry has `doc_id`, `chunks_created`, `status`, `error`, `warnings: list[str]` **E0b**, `code: str \| null` **E0d**). On unsafe `path`: `{error, code: "path_unsafe"}`; when a reindex holds the lock: `{error, code: "store_busy"}`; oversized files within a directory produce per-file `{status: "error", code: "file_too_large"}` — the batch continues for other files. |
| `list_collections` | List collections with public-contract fields only (validated through `CollectionListItemSchema`). | — | `list[CollectionListItemSchema dict]` — fields: `name`, `description`, `doc_count`, `chunk_count`, `last_indexed`, `last_described`, `embedding_model` (renamed from `active_embedding_model`), `pending_embedding_model`. Internal fields stripped. See `BREAKING.md` C7 entry. |
| `get_collections_meta` | Full meta for all collections, validated through `CollectionMetaMcpSchema`. **B4**: `description_embedding` is `null` by default; pass `include_description_embedding: bool = True` to include it. | `include_description_embedding: bool = False` (optional) | `list[CollectionMetaMcpSchema dict]` — same public fields as `list_collections` plus `description_embedding: list[float] \| null` (always present; `null` when not requested). See `BREAKING.md` C7 entry. |
| `get_collection_meta` | Full meta for one collection, validated through `CollectionDetailSchema`. **C7**: `description_embedding` removed (was additive in B4); use `get_collections_meta` if you need it. | `name: str` | `CollectionDetailSchema dict` — same public fields as `list_collections` without `description_embedding`. Or `{error, code: "not_found"}`. See `BREAKING.md` C7 entry. |
| `list_documents` | List documents in a collection, validated through `DocumentInfoSchema`. **E0c** — gains optional `cursor: str \| None = None` for cursor-based pagination; existing calls without `cursor` continue to work (backward-compatible). Returns items sorted by `doc_id`; `cursor` resumes from the sort position of the given `doc_id` (deleted cursor silently resumes from next sort position). **Note**: the MCP tool returns only the items list — `next_cursor` and `total` are not included in the response. To page forward, callers must use the last item's `doc_id` as the `cursor` for the next call. For full pagination metadata (`next_cursor`, `total`), use the REST `GET /collections/{name}/documents` endpoint instead. | `collection?`, `limit: int = 100`, `cursor?: str` | `list[DocumentInfoSchema dict]` — fields: `doc_id`, `source_path`, `chunk_count`, `indexed_at`, `scopes: list[str]` (**E2a** — empty list when document has no scoped chunks; absent on un-migrated collections) |
| `delete_document` | Delete all chunks for one document, validated through `DeleteDocumentSchema`. | `doc_id: str`, `collection?` | `{"deleted": int}` |
| `update_collection` | **C1** — Update the embedding model for a collection. Implements the same per-collection model state machine as `PATCH /collections/{name}`. Validated through `CollectionDetailSchema`. | `collection_name: str`, `embedding_model: str` | `CollectionDetailSchema dict` or `{error, code: "not_found" \| "conflict" \| "validation_error" \| "internal_error"}`. See `BREAKING.md` C7 entry. |
| `export_collection` | **D1/D2** — Start an export job for a collection. Non-blocking: returns a `JobResponse` dict immediately (job is `QUEUED`); client polls `GET /jobs/{job_id}` for progress. Operates in the caller's authenticated namespace (D9 — `_get_request_namespace()`). | `collection: str`, `output_path: str = ""` | `job_to_dict(job)` on success. `{error, code: "path_unsafe"}` on unsafe path. `{error, code: "not_found"}` if collection missing. |
| `import_collection` | **D1/D2** — Start an import job from a `.tar.gz` archive. Non-blocking: returns a `JobResponse` dict immediately (job is `QUEUED`). Pre-validates archive (schema version, embedding model match, tar safety). Operates in the caller's authenticated namespace (D9 — `_get_request_namespace()`). | `collection: str`, `path: str`, `force_overwrite: bool = False`, `ignore_schema_version: bool = False`, `on_error: str = "fail"` | `job_to_dict(job)` on success. `{error, code}` on validation failure. |
| `create_key` | **D7** — Issue a new managed API key. Returns the raw token once; never retrievable again. | `namespace: str`, `label?: str`, `expires_at?: str` (ISO-8601 with timezone) | `{id, token, namespace, label, created_at, expires_at, status: "active"}`. On error: `{error, code}`. |
| `list_keys` | **D7** — List managed keys. Active-only by default. | `status?: "active" \| "revoked" \| "all"` (default `"active"`), `namespace?: str` | `{keys: [{id, namespace, label, created_at, expires_at, status}…], hidden_revoked_count?: int}`. No `token` field. |
| `revoke_key` | **D7** — Revoke a managed key by ID. Idempotent. | `key_id: str` | `{id, status: "revoked"}`. On error: `{error, code}`. |
| `rotate_key` | **D7** — Generate a new default API key, write it to `.search.env`, revoke/grace-expire the old one. Returns the new token once. Returns error when `ARCHON_SEARCH_API_KEY` env var is set. | `grace_seconds?: int` | `{new_key_id, token, status: "active", old_key_id, old_key_expires_at, old_key_status}`. On error: `{error, code}`. |
| `get_graph` | **E2b** — Return node and edge list for a single collection. Requires `[graph] enabled = true`. Mirrors `GET /graph/{collection}`. **E2g**: gains `"importance"` sort mode (PageRank). | `collection: str`, `format?: "json" \| "graphml"` (default `"json"`), `salience?: "frequency" \| "tfidf" \| "importance"` (default `"frequency"`) | `GraphInspectionResponse` dict — same shape as REST `GET /graph/{collection}`. `{error, code: "graph_disabled"}` when `[graph].enabled = false`; `{error, code: "not_found"}` when collection absent. |
| `get_graph_cross_collection` | **E2c** — Return merged node and edge list across ≥2 collections. Requires `[graph] enabled = true` and ≥2 valid collection names. **E2g**: gains `"importance"` sort mode (PageRank). | `collections: str` (comma-separated, ≥2 names), `format?: "json" \| "graphml"`, `salience?: "frequency" \| "tfidf" \| "importance"` | `CrossCollectionGraphInspectionResponse` dict. `{error, code: "graph_disabled"}` or `{error, code: "validation_error"}` when fewer than 2 valid collections provided. |
| `graph_impact` | **E2g BE-9** — Blast-radius (caller/callee) impact analysis for a code symbol. Returns callers and callees separated as `ImpactGroup{direct, indirect, truncated, omitted_count}`, depth-capped, PageRank-ordered. Requires `[graph] enabled = true`. | `symbol: str`, `collection: str`, `file_path?: str \| null` (disambiguates same-named symbols; omit to default to highest-PageRank match), `depth?: int` (default `2`, hard cap `5`), `direction?: "callers" \| "callees" \| "both"` (default `"both"`), `extraction_method_filter?: str \| null` (e.g. `"extracted"` to exclude inferred edges) | `{callers: ImpactGroup, callees: ImpactGroup, depth_used: int}`. `{error, code: "graph_disabled"}` when `[graph].enabled = false`; `{error, code: "not_found"}` when collection absent; `{error, code: "validation_error"}` on bad params. |

**Breaking-change note (from [`/BREAKING.md`](../../BREAKING.md)):**

- **E0c**: `SearchRequest.top_k` and `ExplainRequest.top_k` no longer carry a static `maximum: 100` in the OpenAPI schema — the upper bound is now dynamic (`top_k_max`, default 100, operator-configurable). The 422 error shape for fanout and `top_k` bound violations also changed from a Pydantic list `{"detail": [...]}` to a handler string `{"detail": "..."}`. See `BREAKING.md` E0c entry.
- `search` returns `{"results": [...], "acl_filtered": bool}` — no longer a bare list. Consumers must access `response["results"]`.
- REST `/search` per-request `top_k` is now ignored; configure `[search] top_k_return` instead.
- B3 adds additive keys to the `search` and `explain` tool outputs: every result gains `collection`, and the response gains `excluded_collections`. For tolerant JSON consumers these are non-breaking; for **strict-validating MCP clients** (`extra="forbid"` schemas) the new keys are a true contract change — relax the client schema. `SearchRequest.collection` also moves from required to optional (exactly-one-of with `collections`). See `BREAKING.md` "[next release] — B3 multi-collection search".
- **C4**: `search_with_context` now returns `{"results": [...], "hyde_applied": bool}` — no longer a bare `list[dict]`. Consumers must access `response["results"]`. `search` and `explain` gain additive `hyde_applied: bool` key (non-breaking for tolerant consumers). See `BREAKING.md` "[next release] — C4 HyDE query expansion".
- **C5**: REST `POST /search` and MCP `explain` tool return dicts gain `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, and `rag_fusion_attempted: bool` fields. `explain` additionally gains `rag_fusion_failure_reason: str | null` and `rag_fusion_sub_queries: list | null`. **Exception**: MCP `search` and `search_with_context` use narrower schemas (`McpSearchResponse`, `SearchWithContextResponse`) that do NOT include `rag_fusion_applied`/`rag_fusion_queries_used`/`rag_fusion_attempted` — see tool descriptions above. For tolerant JSON consumers these are non-breaking additive keys; for strict-schema validators (`extra="forbid"`) the new keys are a true contract change. See `BREAKING.md` "C5 RAG Fusion".
- **E0b**: `search` and `search_with_context` tool return dicts gain `expansion_used: bool` and `expansion_warning: str | null`. Additive non-breaking fields for tolerant JSON consumers. See `BREAKING.md` E0b entries.
- **C7**: All MCP tool return values are now validated through explicit Pydantic schemas (`mcp_schemas.py`) before serialization. Five tools narrow their shapes relative to the old `asdict()` output — `list_collections`, `get_collections_meta`, `get_collection_meta`, `update_collection` all strip internal `CollectionMeta` fields and rename `active_embedding_model` → `embedding_model`; `search_with_context` context chunks no longer include `start_offset`, `end_offset`, `custom_score`. Schema drift now surfaces as `{"error": "...", "code": "schema_validation_error"}`. See `BREAKING.md` C7 entries.

The REST control plane and the MCP tool surface are served by the same FastAPI app and share auth. The REST endpoints above and the MCP tools in this table are intentionally not 1:1 — MCP exposes ingest/list/delete document operations, REST exposes the job-oriented control plane.

## CLI commands

Entry point: `archon-search` (`archon_search/cli/main.py`, Click group). Most subcommands accept `--config <path>`; the exceptions are `stop` and `status`, which use fixed service identity and do not accept `--config`.

| Command | Subcommand | Purpose | Key flags |
| --- | --- | --- | --- |
| `start` | — | Validate config, then start the OS service (`cli/start.py`). | `--config` |
| `stop` | — | Stop the OS service; identity is fixed (`cli/stop.py`). | — |
| `status` | — | Show running/stopped, PID, uptime (`cli/status.py`). | — |
| `serve` | — | C9 — foreground-blocking uvicorn launcher used by the Docker image and any direct-run topology where launchd/systemd are unwanted. Calls `load_config(path, serve=True)` (host defaults to `0.0.0.0`; TOML / `ARCHON_SEARCH_HOST` still overrides) then `run_server(config)`. Never touches `_get_service()`. Emits a startup warning when `ARCHON_SEARCH_DATA_DIR` is set without `ARCHON_SEARCH_CONFIG` (collection add/remove inside a container needs a writable TOML under `/data`). See `cli/serve.py`. | `--config` |
| `wizard` | — | Interactive setup wizard: choose a profile, configure optional features, download models, register and start service. Asks interactive questions for multilingual support, code enrichment, reranker, filesystem watcher, telemetry, eager loading, routing strategy, log format, log-to-stderr (when json selected), and GPU confirmation; prompts for AI query expansion (HyDE/RAG Fusion) with provider selection (Anthropic/OpenAI/Ollama). All questions are skippable with flags; `--non-interactive` suppresses all prompts and uses defaults. Prompt order: multilingual → profile → GPU confirm → licenses → optional features → summary → service install. Prints a "Next steps" block (including `--top-k` hint and full API key with source label) after a successful install. Warns before overwriting hand-edited config keys on re-run. See `archon_search/install.py` and `archon_search/profiles.py`. | `--profile {minimal,balanced,max}`, `--multilingual/--no-multilingual`, `--skip-preload`, `--force`, `--delete-db`, `--accept-jina-license`, `--accept-fasttext-license`, `--dry-run`, `--non-interactive`, `--config`, `--code / --no-code`, `--watch / --no-watch`, `--telemetry / --no-telemetry`, `--eager-load / --no-eager-load`, `--no-reranker`, `--routing-strategy {centroid,hybrid}`, `--log-format {text,json}`, `--disable-gpu`, `--host TEXT`, `--port INTEGER (1–65535)`, `--db-path PATH`, `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}`, `--log-to-stderr`, `--top-k INTEGER (1–100)`, `--telemetry-retention-days INTEGER (≥1)`, `--enable-hyde`, `--enable-rag-fusion`, `--server-key HEX_KEY (lowercase hex ≥32 chars)` |
| `install` | — | Register and start the service only (no prompts, no model download). Requires `wizard` to have been run first. | `--dry-run`, `--config` |
| `uninstall` | — | Stop and unregister service; optionally delete the database directory (`cli/install_cmd.py`). | `--delete-db`, `--config` |
| `ingest` | — | **FE-5** — Submit an async ingest job to the running server via `POST /ingest` (`cli/ingest.py`). **`--path` is required** (exits 1 with `Error: --path is required.` when omitted). The path is resolved to an absolute path before submission. Collection name defaults to `path_to_collection_name(path)` (full filename, e.g. `report.pdf` → `"report_pdf"`). Server must be running — exits 1 with `"archon-search serve is not running."` on `ConnectError`. Non-202 responses exit 1 with status code and body. `--wait` polls `GET /jobs/{id}` until terminal status and prints `Ingest complete for '<name>'.` on DONE; exits 1 on FAILED. Ctrl-C during polling prints `Polling stopped` and exits 0. | `--path` (required), `--collection`, `--wait`, `--api-url`, `--api-key` |
| `sync` | — | Run `SearchCollectionSync` over all pinned + configured collections (`cli/sync.py`). | `--config` |
| `collection` | `list` | List collections from the store (`cli/collection.py`). | `--config` |
| `collection` | `add <path>` | Persist path in config and ingest. | `--config` |
| `collection` | `remove <path>` | Drop collection from store and config; rejects pinned-only. Note: `--force` is currently only enforced as mutually exclusive with `--dry-run`; despite its help text ("Proceed even if service is running"), no service-running check exists in `cli/collection.py::remove`, so the flag is effectively a no-op beyond the mutex. #Unverified — intentional behaviour vs. unimplemented check | `--dry-run`, `--force`, `--config` |
| `collection` | `info <name>` | Print collection metadata. | `--config` |
| `collection` | `reindex <name>` | Clear state, drop table, re-ingest from source path. | `--config` |
| `collection` | `reindex-metadata <name>` | Backfill metadata fields (`file_type`, `updated_at`, `ingested_by`) on an existing collection without re-ingesting. When `--normalize-timestamps` (default ON) rewrites `indexed_at` and `updated_at` to fixed-width UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ`) for any row not already in canonical form — required before date-range filters return correct results on pre-A2 collections. `--dry-run` reports counts without writing. Introduced in A2. | `--normalize-timestamps / --no-normalize-timestamps`, `--dry-run`, `--config` |
| `collection` | `migrate <name>` | **D3** — Inspect or apply schema migrations for a collection. Default (no flags) calls `GET /migrations/pending` and prints each pending migration with its `kind` and `description`. `--apply` calls `POST /migrate` and applies all in-place migrations synchronously; prints `migrations_applied`. `--backup-first` (requires `--apply`) confirms a backup before submitting a rewrite migration job (supplies `backup_confirmed: true`). `--wait` (requires `--apply`) polls `GET /jobs/{job_id}` every 2 seconds, printing `phase: processed/total` on each poll; exits 0 on DONE, 1 on FAILED/CANCELLED. `--dry-run` and `--apply` are mutually exclusive. `--backup-first` and `--dry-run` are mutually exclusive (--backup-first requires --apply to be passed explicitly). | `name: str`, `--dry-run / --no-dry-run`, `--apply / --no-apply`, `--backup-first / --no-backup-first`, `--wait / --no-wait`, `--api-url TEXT`, `--api-key TEXT` |
| `graph` | `build-communities <collection>` | **GBC110** — Proxies to `POST /graph/{collection}/rebuild-communities` (`cli/graph_cmd.py`), replacing the old in-process `CommunityBuilder`/`GraphStore`/`SearchStore` call path (which raced with a running server writing the same LanceDB files). Prints the returned `job_id` and exits 0. `--wait` polls `GET /jobs/{job_id}` until a terminal status (`DONE`/`FAILED`/`CANCELLED`/`FAILED_EXPIRED`), exiting 0 on `DONE` and non-zero otherwise. A connection failure (server not running) is caught specifically as `httpx.ConnectError`, printing `"Server is not running. Start it first with: archon-search start"` and exiting non-zero; any other non-202/200 response prints `"server returned {code}: {text}"`. No `--config`/local `[graph] enabled` pre-check (removed — the server enforces it via the route's `422`). | `collection: str`, `--wait`, `--api-url TEXT`, `--api-key TEXT` |
| `config` | `show` | Print effective config (defaults when no file exists) (`cli/config_cmd.py`). | `--config` |
| `config` | `get <section.field>` | Read one dotted key. Requires exactly a two-part `section.field` key; other formats error out. | `--config` |
| `config` | `set <section.field> <value>` | Write one dotted key (bool/int/float coercion). | `--config` |
| `export` | — | **D1/D2** — Start an export job for a collection via `POST /collections/{collection}/export`. Prints the `job_id` and exits 0 by default. `--wait` polls until DONE/FAILED/FAILED_EXPIRED, printing progress updates and the resulting archive path. **E0b**: `--timeout SECONDS` (default 300) added; on timeout exits 0 and prints job ID + recovery hint to stderr; exits 2 on FAILED or FAILED_EXPIRED (changed from exit 1). | `collection: str`, `--output-dir PATH`, `--wait / --no-wait`, `--timeout SECONDS`, `--api-url TEXT`, `--api-key TEXT` |
| `import` | — | **D1/D2** — Start an import job from a `.tar.gz` archive via `POST /collections/{collection}/import`. `--wait` polls until DONE/FAILED, printing imported/skipped/total counts; warns when `skipped > 0`. Exits 1 on FAILED. | `collection: str`, `path: str`, `--force-overwrite / --no-force-overwrite`, `--ignore-schema-version / --no-ignore-schema-version`, `--on-error [fail\|skip]`, `--wait / --no-wait`, `--api-url TEXT`, `--api-key TEXT` |
| `backup` | — | **D2** — Backup Click group. Bare invocation prints help. `--now` calls `POST /backup/trigger` and prints each queued `job_id` plus skipped collections with reason; `--wait` polls each job to DONE/FAILED/FAILED_EXPIRED. **E0b**: `--timeout SECONDS` (default 300) added; on timeout exits 0 and prints job IDs + recovery hint to stderr; exits 2 on FAILED or FAILED_EXPIRED (changed from exit 1). | `--now`, `--wait`, `--timeout SECONDS`, `--api-url TEXT`, `--api-key TEXT` |
| `backup` | `status` | **D2** — Print scheduled-backup state. Offline-capable: reads `.backup-state.json` and counts archives on disk; merges `last_tick_at` / `next_run_at` from `GET /status` when the server is reachable. `--json` emits a `BackupStatusDetail`-shaped payload. | `--json`, `--api-url TEXT`, `--api-key TEXT` |
| `maintenance` | — | **D5** — Maintenance Click group. Bare invocation prints help. | — |
| `maintenance` | `status` | **D5** — Print maintenance state. Offline-capable: reads `.maintenance-state.json` directly and prints `last_run_at`, `next_run_at`, and per-collection health table. Optionally merges live `maintenance` block from `GET /status` when server is reachable. `--json` emits the raw state as JSON. | `--json`, `--api-url TEXT`, `--api-key TEXT` |
| `maintenance` | `run` | **D5** — Trigger an immediate maintenance pass via `POST /maintenance/trigger`. Prints `"triggered"` and exits immediately. `--wait` polls `GET /status` until `maintenance.last_run_at` changes, then prints the updated health summary. **E0b**: `--timeout SECONDS` (default 120) added; on timeout exits 0 and prints job ID + "poll with archon-search maintenance status" hint to stderr (changed from exit 1); exits 2 when the pass completed with errors (detected via `collection_health` last_error). | `--wait / --no-wait`, `--timeout SECONDS`, `--api-url TEXT`, `--api-key TEXT` |
| `key` | — | **D7** — Key management Click group. Bare invocation prints help. | — |
| `key` | `create` | **D7** — Issue a new managed API key via `POST /keys`. Prints the raw token to **stdout only** and a contextual banner to **stderr only** (safe for shell `$()` capture). | `--namespace TEXT (required)`, `--label TEXT`, `--expires EXPR` (accepts `30d`, `12h`, `3600s`, or ISO-8601 datetime with timezone), `--api-url TEXT`, `--api-key TEXT` |
| `key` | `list` | **D7** — List managed keys via `GET /keys`. Active-only by default; shows hint line when revoked keys are hidden. | `--namespace TEXT`, `--status [active\|revoked\|all]` (default `active`), `--api-url TEXT`, `--api-key TEXT` |
| `key` | `revoke <id>` | **D7** — Revoke a managed key by ID via `DELETE /keys/{id}`. Idempotent. Prompts for confirmation before deleting, showing the key's label when a best-effort `GET /keys?status=all` lookup finds it (falls back to the raw ID otherwise). Declining exits `0` with no change; a non-interactive stdin (pipe/CI) without `--yes` aborts non-zero. `--yes`/`-y` skips both the prompt and the lookup. | `--yes`/`-y`, `--api-url TEXT`, `--api-key TEXT` |
| `key` | `rotate` | **D7** — Rotate the default API key via `POST /keys/rotate`. Prints the new raw token to **stdout only**. `--grace` sets the grace period during which the old key remains valid. | `--grace DURATION` (same formats as `--expires`; converted to seconds integer), `--api-url TEXT`, `--api-key TEXT` |

## `[mcp]` config section (D9)

Controls whether the MCP HTTP endpoint is mounted. The section may be omitted entirely (MCP is on by default).

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | `bool` | `true` | When `true`, the MCP transport is mounted at `/mcp` on the same FastAPI app and port as REST during the app lifespan. When `false`, no `/mcp` route is mounted and the `mcp` field on `GET /status` / `GET /health` is `null`. |

## `[auth]` config section (D7)

Controls managed-key rotation behaviour. All fields have defaults; the section may be omitted entirely.

| Key | Type | Default | Effect |
|---|---|---|---|
| `rotate_grace_seconds` | `int` | `0` | Seconds the old default key remains valid after `key rotate` / `POST /keys/rotate`. `0` = immediate revocation. Overridable per-call via the `grace_seconds` field in the `POST /keys/rotate` request body. Must be ≥ 0 (config load raises `ConfigError` otherwise). |

## `[database]` config section (D6 addition to an existing section)

The `[database]` section pre-dates D6; D6 adds one key to it.

| Key | Type | Default | Effect |
|---|---|---|---|
| `validation_timeout_seconds` | `int` | `60` | Timeout for the background model-validation task (D6) that probes the configured embedder, reranker, and ONNX providers at startup. On timeout, both `embedder_ok` and `reranker_ok` are `false` and `provider_warnings` contains `"validation timed out after {N}s"`. Must be `> 0`; values `<= 0` are rejected with a warning and fall back to `60`. |

## `[backup]` config section (D2)

Controls scheduled-backup behaviour. All fields have defaults that take effect when the section is absent from `archon-search.toml`. Disabled by default (`interval_hours = 0`).

| Key | Type | Default | Effect |
|---|---|---|---|
| `interval_hours` | `int` | `0` | Hours between automatic backup ticks. `0` disables the trigger loop (completion loop still drains any in-flight jobs from prior sessions). |
| `keep` | `int` | `7` | Number of archives to retain per collection. `0` = never rotate (archives accumulate; config loader logs a WARNING when paired with `interval_hours > 0`). |
| `exclude` | `list[str]` | `[]` | Patterns to skip: bare `{col}` matches across namespaces, `{ns}/{col}` matches exactly one. |
| `output_dir` | `str` | `get_data_dir() / "backups"` | Root directory for archives. Resolved at load time when empty. Config loader logs an ERROR and falls back to the default when the configured path has fewer than 3 components (guards against rotation scanning near-root directories). |

**Backup lifecycle**: `BackupLoop` enumerates collections per namespace, deduplicates against `_in_flight` and `list_queued_bulk()`, then calls `job_store.create_export(..., source="backup")`. Jobs dispatch via the standard `JobScheduler` but `list_queued_bulk()` sorts `source="backup"` behind `source="user"` so manual operations always win. On DONE the completion loop updates `~/.archon-search/.backup-state.json` and runs rotation; FAILED leaves `last_backup_at` untouched.

## `[maintenance]` config section (D5)

Controls scheduled-maintenance behaviour. All fields have defaults that take effect when the section is absent from `archon-search.toml`. Disabled by default (`interval_hours = 0`).

| Key | Type | Default | Effect |
|---|---|---|---|
| `interval_hours` | `int` | `0` | Hours between automatic maintenance passes. `0` = no scheduled passes; `POST /maintenance/trigger` still works. Must be ≥ 0 (config load raises `ConfigError` for negative values). |
| `fts_optimize` | `bool` | `true` | Enable FTS index optimization per collection during each pass. |
| `orphan_cleanup` | `bool` | `true` | Enable removal of chunks whose `source_path` no longer exists on disk (URL chunks are skipped). |
| `failed_ingest_retry` | `bool` | `true` | Enable automatic re-enqueue of FAILED `IngestJob`s within age and attempt limits. |
| `retry_max_attempts` | `int` | `3` | Maximum retry attempts per `{namespace}/{collection}/{source_path}` key. Must be ≥ 1. |
| `retry_max_age_hours` | `int` | `72` | Only retry jobs created within this many hours. Must be ≥ 0; config loader logs WARNING when `0` (all failed jobs regardless of age). |
| `prune_expired_chunks` | `bool` | `true` | **E2a** — Enable deletion of chunks whose `expires_at < now_utc` during each pass. Un-migrated collections (missing the E2a schema) are silently skipped. |
| `exclude` | `list[str]` | `[]` | Patterns to skip: bare `{col}` matches across all namespaces, `{ns}/{col}` matches exactly one. Same syntax as `[backup].exclude`. |

**Maintenance lifecycle**: `MaintenanceLoop` (always instantiated, stored on `app.state.maintenance_loop`) runs a single `_trigger_loop`. When `interval_hours = 0`, the loop waits indefinitely on `_trigger_event`; manual triggers via `POST /maintenance/trigger` still fire a pass. Each pass runs all four enabled policies: FTS optimize, orphan cleanup, and expired-chunk pruning (`prune_expired_chunks`) are per-non-excluded-collection; failed-ingest retry is pass-level (runs once per pass, not per-collection). The pass then writes `.maintenance-state.json` atomically. `GET /status` reads this file to expose `MaintenanceStatusDetail`; the CLI `archon-search maintenance status` works offline by reading it directly.

## `[jobs]` config section (D1/D2 additions)

Controls bulk job (export/import) concurrency and checkpoint granularity. All fields have defaults that take effect when the section is absent from `archon-search.toml`. Existing ingest/reindex/delete jobs are unaffected — they dispatch immediately regardless of this section.

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_concurrent_bulk` | `int` | `1` | Maximum number of export/import jobs running concurrently. Must be ≥ 1 (config load raises `ConfigError` otherwise). |
| `checkpoint_interval` | `int` | `100` | Documents written between progress checkpoints. Lower = more granular progress + finer resume granularity; higher = less write amplification. Must be ≥ 1. |

**Bulk job lifecycle**: export and import jobs are created with status `QUEUED`. The `JobScheduler` (5-second tick) promotes QUEUED bulk jobs to `RUNNING` when a slot is available (up to `max_concurrent_bulk`). QUEUED jobs are never evicted by the 7-day eviction guard; only terminal jobs (`DONE`, `FAILED`, `FAILED_EXPIRED`, `CANCELLED`) are evicted. **E0b**: `FAILED_EXPIRED` is terminal and subject to the same 7-day eviction.

## `[routing]` config section (B4 additions)

Controls collection-routing behaviour. All fields have defaults that take effect when the section is absent from `archon-search.toml`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `routing_shortlist_size` | `int` | `8` | Maximum collections to pass to the decomposer after centroid pre-ranking. |
| `routing_confidence_threshold` | `float` | `0.30` | Minimum centroid-similarity required to include a collection in the shortlist. When no collection reaches this threshold (and at least one was scored), the shortlist is empty (unroutable query). |
| `max_parallel_collections` | `int` | `3` | Declared config knob; currently inert (no runtime code path reads it). Tracked as debt. |
| `routing_strategy` | `str` | `"centroid"` | Routing scoring strategy. `"centroid"` — pure centroid cosine similarity (pre-B4 behaviour, the default). `"hybrid"` — blends centroid score with description-embedding cosine score (see below). Invalid values are rejected at config load with `ConfigError`. |
| `routing_description_weight` | `float` | `0.3` | Weight `w ∈ [0.0, 1.0]` for description-embedding cosine in hybrid routing. Ignored when `routing_strategy = "centroid"`. Values outside `[0.0, 1.0]` are rejected at config load with `ConfigError`. |

**Hybrid blend formula** (activated when `routing_strategy = "hybrid"` and the collection has a valid, non-zero `description_embedding` of the correct dimension):

```
score = (1 - routing_description_weight) * centroid_score
      +     routing_description_weight  * description_score
```

Collections without a valid `description_embedding` fall back to pure centroid scoring. See ADR-07 for the design rationale.

## `[observability]` config section

Controls correlation-ID propagation and stage-latency recording. Both fields have defaults that take effect when the section is absent from `archon-search.toml`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `stage_timings_enabled` | `bool` | `true` | When `true`, every handled request binds a `StageRecorder`; per-stage wall times appear in structured log lines and in the `stage_timings_ms` field on `POST /explain` / MCP `explain` responses. When `false`, no `StageRecorder` is bound and `stage_timings_ms` is absent from all responses. |
| `request_id_header` | `str` | `"X-Request-ID"` | Name of the HTTP header used to carry the correlation ID inbound and outbound. Both the inbound read and the response write use this name (lowercased for header matching). Must be a non-empty string. |

## `[graph]` config section (E2f synonym fields)

**E2f** — three new synonym-detection fields added to `GraphConfig`. All other `[graph]` fields (`enabled`, `max_nodes`, `max_edges`, `max_community_size`, `gc_rebuild_communities`, etc.) are documented inline in their respective route sections.

| Key | Type | Default | Effect |
|---|---|---|---|
| `synonym_threshold` | `float` | `0.85` | Cosine similarity threshold for automatic synonym edge detection. Entity name-embedding pairs with cosine similarity ≥ this value within the same entity-type group are written as `synonym_of` edges. Must be in `(0.0, 1.0]`. |
| `alias_file` | `str \| null` | `null` | Path to a TOML file of known synonym aliases (manual synonyms). Each entry: `"K8s" = "Kubernetes"`. When set, alias pairs are written as `synonym_of` edges with `extraction_method="manual"` before cosine comparison runs, and their entity-ID pairs are excluded from cosine scoring via `skip_pairs`. A missing, unreadable, or malformed file logs a WARNING and does not abort enrichment. Must be a non-empty string when set. |
| `enrichment_auto` | `bool` | `true` | When `true`, synonym enrichment runs automatically in the background after every ingest (gated by `on_synonym_edges_written` callback; enrichment failure never propagates to the ingest caller). When `false`, no automatic enrichment is triggered after ingest — synonym edges must be created manually or via operator tooling. Follows the same `_coerce_bool` pattern as `gc_rebuild_communities`. |

## Authoritative contract

`GET /openapi.json` is the binding machine-readable contract. The OpenAPI schema is built in `archon_search/server/app.py::_configure_openapi`; it injects the `BearerAuth` security scheme and applies it to every non-exempt path. If this document diverges from `/openapi.json`, the schema wins — and a follow-up doc fix is required.

## Related documents

- [`520_api_design_and_contracts.md`](./520_api_design_and_contracts.md) — design rules behind these surfaces.
- [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) — auth, namespaces, ACL semantics.
- [`140_error_handling_strategy.md`](./140_error_handling_strategy.md) — status code conventions.
- [`/BREAKING.md`](../../BREAKING.md) — compatibility contract.
- [`990_documentation_index_and_contribution_guide.md`](./990_documentation_index_and_contribution_guide.md) — index of all documentation.

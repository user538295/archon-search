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
| POST | `/search` | Hybrid vector + FTS search over one collection (or a multi-collection fan-out), rerank, ACL filter. | `SearchRequest` (`routes_search.py`) — `{collection?, collections?, query, top_k, filters?, hyde?, rag_fusion?}`. Exactly one of `collection` / `collections` must be supplied (both or neither → `422`). **`top_k` is ignored** at runtime (see BREAKING.md); pipeline uses `config.top_k_return`. The field is still validated (`ge=1, le=100`), so `top_k=0` or `top_k>100` returns `422`. `collection` and `query` are stripped and must be non-empty. `filters` is an optional `SearchFilters` object (see below); omitting it is equivalent to `null`. **C4**: `hyde: bool = false` — when `true` and `[hyde] enabled = true` in config, the ANN lookup uses a HyDE-generated hypothesis embedding; falls back to the original query embedding on any failure. Requires `archon-search[hyde]` installed and `ANTHROPIC_API_KEY` set; `422` if package absent. **C5**: `rag_fusion: bool = false` — when `true` and `[rag_fusion] enabled = true` in config, the server generates N semantic query variants via the Anthropic API, searches with each in parallel, and fuses results via second-pass RRF. Requires `archon-search[rag_fusion]` installed and `ANTHROPIC_API_KEY` set; `422` if package absent. Mutually exclusive with `hyde`: when both are `true`, RAG Fusion wins and `hyde_applied: false` in the response. | `SearchResponse` — `{results: [SearchResultSchema{doc_id, chunk_id, text, score, source_path, file_type, language, indexed_at, updated_at, ingested_by, metadata, acl, collection}], acl_filtered: bool, excluded_collections: [{name, reason}], embedding_model: str, hyde_applied: bool, rag_fusion_applied: bool, rag_fusion_queries_used: int, rag_fusion_attempted: bool}`. **C1**: `embedding_model` field added. **C4**: `hyde_applied: bool` — `true` when HyDE hypothesis embedding was actually used. **C5**: `rag_fusion_applied: bool` — `true` when at least one LLM variant was generated and fused; `rag_fusion_queries_used: int` — number of successful variant searches (0..`num_queries`, not counting the original); `rag_fusion_attempted: bool` — `true` when the generator was called. |

Returns `404` when the collection is not visible to the caller's namespace; `503` when meta lookup fails. Pipeline stage exceptions (embedder, store, reranker) return `500` with a plain-text body `Internal Server Error` (Content-Type `text/plain`) — the route bare-re-raises and Starlette's `ServerErrorMiddleware` renders the default response, so this is **not** a JSON envelope and callers must not `.json()`-parse the 500 body. A hung pipeline call returns `504` with `{"detail": "Search timed out"}` after ~30 s. `200` with `results: []` means the pipeline ran successfully but found no matching documents. A malformed `filters` object returns `422`.

Two additive response fields landed with B3 (multi-collection search) and are present on **both** the single- and multi-collection paths: every `SearchResultSchema` now carries `collection` (its origin collection — `""` on pre-B3-shaped rows), and `SearchResponse` now carries `excluded_collections` (empty on the single-collection path). For tolerant JSON consumers these are non-breaking additive keys; see `BREAKING.md` "[next release] — B3 multi-collection search".

#### Multi-collection fan-out (`collections`) — B3

Supply `collections: list[str]` instead of `collection` to fan a single query out across an explicit set of collections in one request. The query is embedded once, each collection is retrieved in parallel, the candidate pools are merged with provenance, and one global rerank pass produces a unified, globally comparable result list. The architecture is in [`120_services_and_integration_architecture.md`](./120_services_and_integration_architecture.md) ("Multi-collection search fan-out"); concurrency/cost in [`210_performance_and_scalability.md`](./210_performance_and_scalability.md).

| Aspect | Behavior |
|---|---|
| Mutual exclusivity | Exactly one of `collection` / `collections` — both or neither → `422`. |
| `collections` validation | 1–8 entries (`_FANOUT_VALIDATION_LIMIT`, matching `max_fanout` default), per-item stripped + non-empty, deduplicated preserving first-seen order. Empty list, whitespace-only entry, or over-limit → `422`. |
| `filters` + `collections` | Rejected with `422` (`"filters are not supported for multi-collection search in v1"`) — the trace retrieval path used by the fan-out has no SQL-predicate support in v1. |
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
| `language` | `str \| null` | ISO 639-1 (2-letter) or ISO 639-3 (3-letter) code, or `"unknown"`. Empty string coerced to `null`. Uppercase normalized to lowercase. **Single-collection queries only** — rejected with `422` when used with `collections` fan-out. Values not matching `[a-z]{2,3}` or `"unknown"` rejected with `422`. | SQL-side `language = '<code>'` predicate (C2). Excludes chunks in other language states: `language=fr` excludes `""` (untagged) and `"unknown"` chunks; `language=unknown` returns only fasttext-processed-but-below-threshold chunks. |
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
| GET | `/collections/` | List collections visible to caller's namespace. Response includes `active_embedding_model: str` and `needs_reindex: bool` on each `CollectionSummary` entry (C1 — additive). | — | `list[CollectionSummary]` (`schemas.py`) |
| POST | `/collections/` | Register a path as a collection and enqueue first ingest (returns `202`). **C1**: optional `embedding_model` field — when provided, the collection is initialized with that model as `active_embedding_model`; unknown models return `422`. Returns `400` (`"path is unsafe: <reason>"`) when `path` fails safety validation (empty/whitespace-only/NUL/non-absolute/`..`-traversal); `409` for (a) duplicate resolved path or (b) name collision across namespaces; `503` (body `{"error": "store_busy", ...}`, header `Retry-After: 30`) when a reindex holds the per-collection lock. | `AddCollectionRequest` — `{path, embedding_model?}` | `JobResponse` (`schemas.py`) |
| GET | `/collections/{name}` | Full detail for one collection. **C1**: response gains `active_embedding_model` (renamed from `embedding_model`), `pending_embedding_model` (nullable), `needs_reindex` (bool), `reindex_job_id` (nullable). See `BREAKING.md` C1 entry. | — | `CollectionDetail` (`schemas.py`) |
| DELETE | `/collections/{name}` | Remove collection (config + LanceDB). `404` for unknown or cross-namespace name; `409` if pinned-only. | — | `DeleteResponse` (`schemas.py`) |
| PATCH | `/collections/{name}` | **C1** — Update the embedding model for a collection. Implements the per-collection model state machine: (a) if the requested model equals the current `active_embedding_model`, clears `pending_embedding_model` and `needs_reindex`; (b) if data exists for the collection and the model differs, sets `pending_embedding_model = requested` and `needs_reindex = true`; (c) if the collection is empty, directly updates `active_embedding_model`. Dimension validation runs before any mutation. Returns `CollectionDetail` (same shape as `GET /collections/{name}`). `404` for unknown collection; `409` when a reindex job is already in progress; `422` for unknown/invalid model. | `PatchCollectionBody` — `{embedding_model: str}` | `CollectionDetail` (`schemas.py`) |
| POST | `/collections/{name}/reindex` | Start a reindex job (returns `202`). **C1**: uses the collection's `pending_embedding_model` as `target_embedding_model` when set. | — | `JobResponse` |

### `routes_jobs.py`

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/ingest` | Submit an ingest job; returns `202` immediately. When `path` is non-null it is validated by `_path_safety.validate_ingest_path`, returning `400` (`"path is unsafe: <reason>"`) on rejection (empty/whitespace-only/NUL/non-absolute/`..`-traversal); `path: null` documents-only ingest is unaffected. Returns `503` (body `{"error": "store_busy", ...}`, header `Retry-After: 30`) when a reindex holds the per-collection lock; ingest to a different collection is unaffected. The `ingested_by` field in the body is always overwritten server-side from the `X-Ingested-By` header (missing → `"http"`; legacy `"archon-search-cli"` → `"cli"`; unknown → `"http"` + WARNING log; truncated to 32 chars). `collection` is rejected only when empty string (whitespace-only is currently accepted; inconsistent with `SearchRequest`). | `IngestRequest` — `{collection, path?, documents?, ingested_by (overwritten)}` | `JobResponse` |
| GET | `/jobs/{job_id}` | Read job status; `404` for cross-namespace IDs. | — | `JobResponse` |
| DELETE | `/jobs/{job_id}` | Cancel a job. Terminal jobs (`DONE`/`FAILED`/`CANCELLED`) return `200` (idempotent); `RUNNING`/`PENDING` transition to `CANCELLING` and return `202`; already-`CANCELLING` jobs also return `202`. | — | `JobResponse` |

### `routes_explain.py` (A4)

| Method | Path | Purpose | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| POST | `/explain` | Return the full per-stage retrieval/reranking score breakdown for a query, plus the routing decision when no collection is pinned. Debug endpoint for understanding why results appear (or don't). | `ExplainRequest` (`routes_explain.py`) — `{query, collection?, collections?, top_k(1-100, default 5), rerank(default true), hyde(default false), rag_fusion(default false)}`. Extra fields are **rejected** (`extra="forbid"`). `query` must be non-empty. **C4**: `hyde: bool = false` — same semantics as `SearchRequest.hyde`. **C5**: `rag_fusion: bool = false` — same semantics as `SearchRequest.rag_fusion`; mutually exclusive with `hyde`. | `ExplainResponse` — see schema below. **C4**: `ExplainResponse` gains `hyde_applied: bool`. **C5**: `ExplainResponse` gains `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, `rag_fusion_attempted: bool`, `rag_fusion_failure_reason: str \| null`, `rag_fusion_sub_queries: list[{variant_index, result_count, top_doc_ids}] \| null`. |

All schemas in `routes_explain.py` use `extra="forbid"`; unknown fields produce `422`.

**Request fields:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | required | Must be non-empty after stripping. |
| `collection` | `str \| null` | `null` | Pin to a specific collection. Mutually exclusive with `collections`; omit both to invoke routing. |
| `collections` | `list[str] \| null` | `null` | Multi-collection fan-out (B3): 1–8 entries, stripped + non-empty, deduplicated. Routing is bypassed. Mutually exclusive with `collection` (both → `422`). |
| `top_k` | `int` | `5` | Results to return; `ge=1, le=100`. |
| `rerank` | `bool` | `true` | Run cross-encoder reranker. `false` → scores sorted by `rrf_score`. With `collections` of length > 1, `rerank=false` is rejected with `422` (`"reranking cannot be disabled for multi-collection search in v1"`); a single-item `collections` list with `rerank=false` is allowed (treated like the single-collection path). |
| `hyde` | `bool` | `false` | **C4** — Use HyDE hypothesis embedding for the ANN lookup leg. Requires `[hyde] enabled = true` in config, `archon-search[hyde]` installed, and `ANTHROPIC_API_KEY` set. Falls back silently to the standard query embedding on any failure; `hyde_applied: false` in the response reflects the fallback. |
| `rag_fusion` | `bool` | `false` | **C5** — Generate N semantic query variants via Anthropic API, search with each, and fuse results via second-pass RRF. Requires `[rag_fusion] enabled = true` in config, `archon-search[rag_fusion]` installed, and `ANTHROPIC_API_KEY` set. Falls back silently on any failure; `rag_fusion_applied: false` in the response. Mutually exclusive with `hyde`: when both are `true`, RAG Fusion wins and `hyde_applied: false`. |

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
  "rag_fusion_sub_queries": null
}
```

**Key schema notes:**

- `routing` is `null` when `collection` is pinned in the request. When collectionless, it carries the full routing decision including all caller-namespace collections — the confidence-threshold gate is **bypassed** so every collection in the namespace appears in `candidates`, sorted by `centroid_score` descending with alphabetical tie-break. `centroid_score` is `null` for collections with a mismatched embedding model or no centroid.
- `results[]` carry `text`; `near_misses[]` structurally **omit `text`** (the `ExplainNearMiss` Pydantic model has no `text` field). Near-misses are capped at 20.
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
| Empty query / `top_k` out of `[1, 100]` / extra request fields / both `collection` and `collections` set / `rerank=false` with > 1 `collections` | `422` | Pydantic validation detail |
| Pinned `collection` not found, or any requested `collections` entry not in namespace | `404` | `{"detail": "collection not found"}` |
| Multi-collection fan-out timeout (`fanout_timeout_seconds`) | `504` | `{"detail": "Search timed out"}` |
| Collectionless + no collections in namespace | `404` | `{"detail": "no collections available"}` |
| Meta-lookup or router failure | `503` | `{"detail": "service unavailable"}` |
| Pipeline-stage failure (store / reranker) | `500` | `{"detail": "<stage> error: <ExceptionType>"}` |
| Other unexpected failure | `500` | `{"detail": "explain failed"}` |

503 is reserved for meta-lookup / router failures, consistent with A3's `/search` taxonomy. Pipeline-stage failures (store, reranker) surface as 500 with a stage-specific detail; the original exception message is sanitised server-side because FTS errors may echo the query.

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
| `search` | Hybrid vector + FTS search (single-collection or multi-collection fan-out), rerank, ACL filter. | `query: str`, `collection: str \| None`, `collections: list[str] \| None` (B3 — fan-out; exactly one of `collection` / `collections`, else falls back to `default_collection`; 1–8 entries, stripped + deduped), `include_metadata: bool = false`, `file_type: str \| None`, `source_path_prefix: str \| None`, `source_path_glob: str \| None`, `indexed_after: str \| None`, `indexed_before: str \| None`, `language: str \| None` (**C2** — ISO 639-1 or ISO 639-3 code, e.g. `"fr"`, `"de"`, or `"unknown"`; single-collection only), `hyde: bool = false` (**C4** — HyDE hypothesis embedding; requires `archon-search[hyde]` + `ANTHROPIC_API_KEY` + `[hyde] enabled = true`), `rag_fusion: bool = false` (**C5** — multi-query decomposition; requires `archon-search[rag_fusion]` + `ANTHROPIC_API_KEY` + `[rag_fusion] enabled = true`; mutually exclusive with `hyde`) | `{"results": [SearchResult...], "acl_filtered": bool, "excluded_collections": [{name, reason}], "hyde_applied": bool, "rag_fusion_applied": bool, "rag_fusion_queries_used": int, "rag_fusion_attempted": bool}` — **C4**: `hyde_applied` added. **C5**: `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted` added. On validation error: `{error, code: "validation_error"}`. On internal error: `{error, code: "internal_error"}`; collection not found: `{error, code: "not_found"}`. |
| `search_with_context` | Search plus surrounding chunks for each hit. | `query`, `collection?`, `context_window: int = 1`, `include_metadata: bool = false`, `file_type: str \| None`, `source_path_prefix: str \| None`, `source_path_glob: str \| None`, `indexed_after: str \| None`, `indexed_before: str \| None`, `language: str \| None` (**C2** — ISO 639-1 or ISO 639-3 code or `"unknown"`; single-collection only), `hyde: bool = false` (**C4**), `rag_fusion: bool = false` (**C5**) | **C4 breaking change**: returns `{"results": [{result, context_before, context_after}, …], "hyde_applied": bool}` — no longer a bare list. See `BREAKING.md`. **C5**: response gains `"rag_fusion_applied": bool`, `"rag_fusion_queries_used": int`, `"rag_fusion_attempted": bool`. On error: `{error, code}`. |
| `explain` | Return the per-stage retrieval/reranking trace for a query, plus the routing decision when no collection is pinned. Operates in the default namespace only. The query is never echoed in the response or telemetry. | `query: str`, `collection: str \| None`, `collections: list[str] \| None` (B3 — multi-collection fan-out; routing bypassed; `rerank=false` with > 1 collection → `{error, code: "validation_error"}`), `top_k: int = 5`, `rerank: bool = True`, `hyde: bool = false` (**C4**), `rag_fusion: bool = false` (**C5** — same semantics as search; mutually exclusive with `hyde`) | `ExplainResponse` dict (same structure as REST `POST /explain`; serialised via `model_dump(mode="json", exclude_none=False)`). **C4**: `hyde_applied: bool` added. **C5**: `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, `rag_fusion_attempted: bool`, `rag_fusion_failure_reason: str \| null`, `rag_fusion_sub_queries: list \| null` added. `results`/`near_misses` entries carry a `collection` key and the response carries `excluded_collections` (B3). Includes `stage_timings_ms` when `[observability].stage_timings_enabled = true`. On error: `{error, code}`. When `config` is absent from `create_app`, collectionless calls fall back to `default_collection` (no routing). |
| `ingest_file` | Ingest one file, result validated through `IngestResultSchema` (excludes `needs_recompute`). | `path: str`, `collection?` | `IngestResultSchema dict` — fields: `doc_id`, `chunks_created`, `status`, `error`. On unsafe `path`: `{error, code: "path_unsafe"}`; when a reindex holds the lock: `{error, code: "store_busy"}`. |
| `ingest_directory` | Ingest a directory tree (reports progress via `ctx`), each result validated through `IngestResultSchema`. | `path`, `glob_pattern = "**/*"`, `collection?` | `list[IngestResultSchema dict]`. On unsafe `path`: `{error, code: "path_unsafe"}`; when a reindex holds the lock: `{error, code: "store_busy"}`. |
| `list_collections` | List collections with public-contract fields only (validated through `CollectionListItemSchema`). | — | `list[CollectionListItemSchema dict]` — fields: `name`, `description`, `doc_count`, `chunk_count`, `last_indexed`, `last_described`, `embedding_model` (renamed from `active_embedding_model`), `pending_embedding_model`. Internal fields stripped. See `BREAKING.md` C7 entry. |
| `get_collections_meta` | Full meta for all collections, validated through `CollectionMetaMcpSchema`. **B4**: `description_embedding` is `null` by default; pass `include_description_embedding: bool = True` to include it. | `include_description_embedding: bool = False` (optional) | `list[CollectionMetaMcpSchema dict]` — same public fields as `list_collections` plus `description_embedding: list[float] \| null` (always present; `null` when not requested). See `BREAKING.md` C7 entry. |
| `get_collection_meta` | Full meta for one collection, validated through `CollectionDetailSchema`. **C7**: `description_embedding` removed (was additive in B4); use `get_collections_meta` if you need it. | `name: str` | `CollectionDetailSchema dict` — same public fields as `list_collections` without `description_embedding`. Or `{error, code: "not_found"}`. See `BREAKING.md` C7 entry. |
| `list_documents` | List documents in a collection, validated through `DocumentInfoSchema`. | `collection?`, `limit: int = 100` | `list[DocumentInfoSchema dict]` — fields: `doc_id`, `source_path`, `chunk_count`, `indexed_at` |
| `delete_document` | Delete all chunks for one document, validated through `DeleteDocumentSchema`. | `doc_id: str`, `collection?` | `{"deleted": int}` |
| `update_collection` | **C1** — Update the embedding model for a collection. Implements the same per-collection model state machine as `PATCH /collections/{name}`. Validated through `CollectionDetailSchema`. | `collection_name: str`, `embedding_model: str` | `CollectionDetailSchema dict` or `{error, code: "not_found" \| "conflict" \| "validation_error" \| "internal_error"}`. See `BREAKING.md` C7 entry. |

**Breaking-change note (from [`/BREAKING.md`](../../BREAKING.md)):**

- `search` returns `{"results": [...], "acl_filtered": bool}` — no longer a bare list. Consumers must access `response["results"]`.
- REST `/search` per-request `top_k` is now ignored; configure `[search] top_k_return` instead.
- B3 adds additive keys to the `search` and `explain` tool outputs: every result gains `collection`, and the response gains `excluded_collections`. For tolerant JSON consumers these are non-breaking; for **strict-validating MCP clients** (`extra="forbid"` schemas) the new keys are a true contract change — relax the client schema. `SearchRequest.collection` also moves from required to optional (exactly-one-of with `collections`). See `BREAKING.md` "[next release] — B3 multi-collection search".
- **C4**: `search_with_context` now returns `{"results": [...], "hyde_applied": bool}` — no longer a bare `list[dict]`. Consumers must access `response["results"]`. `search` and `explain` gain additive `hyde_applied: bool` key (non-breaking for tolerant consumers). See `BREAKING.md` "[next release] — C4 HyDE query expansion".
- **C5**: `search`, `search_with_context`, and `explain` tool return dicts gain `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, and `rag_fusion_attempted: bool` fields. `explain` additionally gains `rag_fusion_failure_reason: str | null` and `rag_fusion_sub_queries: list | null`. For tolerant JSON consumers these are non-breaking additive keys; for strict-schema validators (`extra="forbid"`) the new keys are a true contract change. See `BREAKING.md` "C5 RAG Fusion".
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
| `wizard` | — | Interactive setup wizard: choose a profile, configure optional features, download models, register and start service. Asks interactive questions for multilingual support, code enrichment, reranker, filesystem watcher, telemetry, eager loading, routing strategy, log format, log-to-stderr (when json selected), and GPU confirmation; prompts for HyDE/RAG Fusion when `ANTHROPIC_API_KEY` is set. All questions are skippable with flags; `--non-interactive` suppresses all prompts and uses defaults. Prompt order: multilingual → profile → GPU confirm → licenses → optional features → summary → service install. Prints a "Next steps" block (including `--top-k` hint and full API key with source label) after a successful install. Warns before overwriting hand-edited config keys on re-run. See `archon_search/install.py` and `archon_search/profiles.py`. | `--profile {minimal,balanced,max}`, `--multilingual/--no-multilingual`, `--skip-preload`, `--force`, `--delete-db`, `--accept-jina-license`, `--accept-fasttext-license`, `--dry-run`, `--non-interactive`, `--config`, `--code / --no-code`, `--watch / --no-watch`, `--telemetry / --no-telemetry`, `--eager-load / --no-eager-load`, `--no-reranker`, `--routing-strategy {centroid,hybrid}`, `--log-format {text,json}`, `--disable-gpu`, `--host TEXT`, `--port INTEGER (1–65535)`, `--db-path PATH`, `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}`, `--log-to-stderr`, `--top-k INTEGER (1–100)`, `--telemetry-retention-days INTEGER (≥1)`, `--enable-hyde`, `--enable-rag-fusion`, `--server-key HEX_KEY (lowercase hex ≥32 chars)` |
| `install` | — | Register and start the service only (no prompts, no model download). Requires `wizard` to have been run first. | `--dry-run`, `--config` |
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

## Authoritative contract

`GET /openapi.json` is the binding machine-readable contract. The OpenAPI schema is built in `archon_search/server/app.py::_configure_openapi`; it injects the `BearerAuth` security scheme and applies it to every non-exempt path. If this document diverges from `/openapi.json`, the schema wins — and a follow-up doc fix is required.

## Related documents

- [`520_api_design_and_contracts.md`](./520_api_design_and_contracts.md) — design rules behind these surfaces.
- [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) — auth, namespaces, ACL semantics.
- [`140_error_handling_strategy.md`](./140_error_handling_strategy.md) — status code conventions.
- [`/BREAKING.md`](../../BREAKING.md) — compatibility contract.
- [`990_documentation_index_and_contribution_guide.md`](./990_documentation_index_and_contribution_guide.md) — index of all documentation.

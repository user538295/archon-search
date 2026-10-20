**Purpose**: Define what `archon-search` returns when things go wrong, on both REST and MCP, and tell clients what to retry vs. surface as failure.
**Audience**: Engineers building clients that must behave correctly under partial outage, slow ingest, network blips, and auth churn.
**Status**: Draft
**Last reviewed**: 2026-10-12 / **Next review**: 2027-10-12

# Error Handling

There are two error envelopes in `archon-search`. REST uses an HTTP status code plus `{"detail": "..."}`. MCP uses HTTP 200 with `{"error": "...", "code": "..."}` as the tool result. Pick your handling layer accordingly.

This page is a client-side companion to `Documentation/Architecture/140_error_handling_strategy.md`; that doc covers the server's perspective.

## Principles

1. **Status code first, message second.** REST clients should branch on HTTP status, not on `detail` text. `detail` strings are stable enough for humans but not part of the typed contract.
2. **`401` has no body.** Auth failures return `401` with `WWW-Authenticate: Bearer` and an empty body — same response for missing header, wrong scheme, and unknown token. Don't try to parse JSON from a 401.
3. **`404` covers cross-namespace.** A resource that exists in another namespace looks like "not found" to your token — by design (`150_security_and_privacy_architecture.md`).
4. **`/search` fails loudly on pipeline errors.** A pipeline stage exception returns HTTP 500; a hung pipeline returns HTTP 504 (`CON-5` resolved in A3). HTTP 200 with `results: []` and `acl_filtered: false` means the pipeline ran successfully but found no matching documents — it is not a failure signal.
5. **MCP errors are payloads, not protocol errors.** Treat `{"error", "code"}` as a sentinel value and branch on `code`.

## REST status codes (client view)

Verified against the route modules under `archon_search/server/`:

| Status | Where it comes from | Body | Client action |
| --- | --- | --- | --- |
| `200` | Normal success | Typed JSON | Use the payload. |
| `202` | `POST /collections/`, `POST /collections/{name}/reindex`, `POST /ingest`, `DELETE /jobs/{id}` on active jobs **or already-`CANCELLING` jobs** | `JobResponse` | Poll `GET /jobs/{id}` until terminal. |
| `400` | `POST /route` — empty query, `slots < 1`; `POST /collections/` and `POST /ingest` — `path` fails safety validation (`"path is unsafe: <reason>"`: empty/whitespace-only/NUL/non-absolute/`..`-traversal) | `{"detail": "..."}` | Fix the request; never retry as-is. Use an absolute path with no `..` segments. |
| `401` | `APIKeyMiddleware` rejected the token | (empty) | Reload the key file; if it rotated, re-read `.search.env`. |
| `404` | Unknown collection, unknown job, cross-namespace access | `{"detail": "..."}` | Treat as logical "not visible to me". Do not retry. |
| `409` | `POST /collections/` — path or name already registered; `DELETE /collections/{name}` — pinned-only; `POST /collections/{name}/reindex` — a reindex of the collection is already `PENDING`/`RUNNING` (`{"detail": "reindex already in progress"}`) | `{"detail": "..."}` | Reconcile config; do not retry blindly. |
| `422` | FastAPI body validation (e.g. empty `collection` / `query` in `/search`) | FastAPI's structured error body | Fix the request; never retry as-is. |
| `500` | Unmapped internal failure; `POST /search` — pipeline stage exception (embedder, store, reranker) (`routes_search.py`); `APIKeyMiddleware` returns a bare `500` (no body) if the resolved namespace fails revalidation (`middleware_auth.py:55-59`) | `{"detail": "..."}` for the routes that build a `JSONResponse({"detail": ...}, status_code=500)` (e.g. `routes_collections.py`, `routes_jobs.py`); the collection add/remove failures in the next two rows also carry a `code`. **Exception: `POST /search` pipeline-failure 500 has a plain-text body `Internal Server Error` (Content-Type `text/plain`)** — the route bare-re-raises and Starlette's `ServerErrorMiddleware` renders the default response, not a JSON envelope. `APIKeyMiddleware`'s 500 path returns an empty body. | Backoff + retry once; surface to operator if it recurs. |
| `500` | `POST /collections/`, `DELETE /collections/{name}` — the config file could not be saved; the in-memory change is rolled back | `{"detail": "could not save the collection change to the config file; see the server log for details", "code": "config_persist_failed"}` | Fix the config file (permissions, TOML syntax), then retry. |
| `500` | `DELETE /collections/{name}` — the table drop or the meta-row delete failed; a failed table drop restores the config entry, a failed meta delete after a successful drop does not | `{"detail": "could not remove the collection from the store; see the server log for details", "code": "collection_drop_failed"}` | Retry once; surface to operator if it recurs. |
| `503` | `POST /search` — the collection-metadata lookup failed: any exception from `pipeline.get_collection_meta` on the single-`collection` path, or, on the multi-`collections` path, the `MetadataLookupError` that `search_many` raises for any `get_all_collections_meta` failure (`routes_search.py`) | `{"detail": "service unavailable: metadata store could not be reached", "code": "metadata_store_error"}` | Retry with exponential backoff. |
| `503` | `POST /search` (single `collection`) — the collection's embedding model is still loading: its cache load stayed wedged past the cache's 120 s wait (`EmbedderNotReadyError`; a merely slow load degrades the request to FTS-only instead) | `{"detail": "service unavailable: embedding model still loading", "code": "embedder_not_ready"}` + header `Retry-After: 30` | Honour `Retry-After`, then retry. |
| `503` | `POST /collections/` — a reindex holds the per-collection ingest lock | `{"detail": "store busy; retry in N seconds"}` + header `Retry-After: N` | Honour `Retry-After`, then retry. Ingest to a *different* collection is unaffected. |
| `503` | `POST /ingest` — a reindex holds the per-collection ingest lock | `{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}` + header `Retry-After: 30` (`code` is `ErrorDetail`'s optional field) | Honour `Retry-After`, then retry. |
| `503` | `DELETE /collections/{name}` — another write holds the per-collection lock, or an ingest job on the collection is `RUNNING`/`CANCELLING` | `{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}` or `{"detail": "ingest job <id> is in progress; retry after it completes", "code": "store_busy"}`, both + header `Retry-After: 30` | Honour `Retry-After`, then retry. |
| `503` | `POST /collections/{name}/reindex` — the embedder cache or pipeline is not initialised yet; no job is created | `{"detail": "service not ready"}` | Retry with backoff once startup completes. |
| `504` | `POST /route` — 30 s routing timeout; `POST /search` — pipeline call timed out (~30 s) | `{"detail": "routing timed out"}` / `{"detail": "Search timed out"}` | Retry at most once; if persistent, check CPU pressure or model load. On `/search`, model load is no longer a cause — the reranker is warmed before the timer starts (S184). |

The full server-side mapping with file:line citations is in `Architecture/140_error_handling_strategy.md`.

### What to retry, what to fail

| Class | Examples | Retry? |
| --- | --- | --- |
| Transient network | connection reset, `httpx.RemoteProtocolError` | Yes — short backoff (e.g. 200ms, 500ms, 1s). |
| `503` from `/search` | Meta lookup race during reindex | Yes — same backoff. |
| `503` from `POST /collections/` (store busy) | Reindex holds the per-collection lock | Yes — honour `Retry-After`, then retry. Body: `{"detail": "store busy; retry in N seconds"}`. |
| `503` `store_busy` from `POST /ingest` or `DELETE /collections/{name}` | Another write holds the per-collection lock (or, on `DELETE`, an ingest is running) | Yes — honour `Retry-After` (30s), then retry. Body: `{"detail": ..., "code": "store_busy"}`. |
| `504` from `/route` | Embedder warm-up, slow centroid load | Once, then surface. |
| `500` | Unhandled exception | Once, then surface — log the response. |
| `401` | Token rotated underneath the client | Reload key, retry once. After that, fail loudly. |
| `404`, `409`, `400`, `422` | Logical error | No — fix the input or config. |
| `200` with `results: []` on `/search` | No matching documents (healthy pipeline) | Do not retry. Pipeline failures now return 500/504, not 200. |

## REST error envelope

For most statuses the body is:

```json
{"detail": "human-readable string"}
```

There are three exceptions to that envelope:

1. **`401`** — empty body (the auth middleware sets `WWW-Authenticate: Bearer` and writes no body).
2. **`422`** — FastAPI's Pydantic-v2 validation shape, where `detail` is a **list** of structured error objects (see the example below), not a string.
3. **`POST /search` `500` on pipeline failure** — plain-text body `Internal Server Error` with `Content-Type: text/plain`. The route bare-re-raises the pipeline exception and Starlette's `ServerErrorMiddleware` renders the default response; there is no `detail` key and the body is not JSON. Branch on `response.status_code` (and `Content-Type`) before parsing.

`schemas.ErrorDetail` is registered on the `responses=` map of routes in `routes_collections.py` and `routes_jobs.py`, so the OpenAPI schema documents this envelope per route for the statuses they declare (typically `401`, and where relevant `404` / `409`). `routes_search.py` declares only `503`; `routes_route.py` registers no `responses=` map — the envelope shape is the same on the wire for the `JSONResponse`-built statuses, but the undeclared ones are not documented in OpenAPI. `ErrorDetail` also has an optional `code` field, set on errors a client should branch on (e.g. `store_busy`, `config_persist_failed`, `collection_drop_failed`).

`422` follows FastAPI's default Pydantic-v2 validation-error shape. The exact `msg` is prefixed with `"Value error, "` and the body includes an `input` field; the simplified example below shows the relevant fields only:

```json
{
  "detail": [
    {"loc": ["body", "query"], "msg": "Value error, query must not be empty", "type": "value_error", "input": ""}
  ]
}
```

## MCP error envelope

MCP tools never raise out-of-band on application errors. They return:

```json
{"error": "human-readable message", "code": "internal_error"}
```

Every `code` value `mcp.py` returns today (grep `code=` in `archon_search/server/mcp.py`):

| `code` | Where | Meaning |
| --- | --- | --- |
| `internal_error` | the catch-all `except Exception` of the search, ingest, collection, document, and graph tools; `export_collection` / `import_collection` without a job store; `create_key` / `rotate_key` write failures. `export_collection`, `import_collection`, `list_keys`, `revoke_key`, and `rotate_key` have no catch-all | Unhandled server-side failure. Treat like REST `500`. |
| `schema_validation_error` | the 11 schema-validated tools, when a success payload fails its Pydantic schema | Server-side contract drift. Treat like REST `500`; report it. |
| `validation_error` | `search`, `search_with_context`, `explain`, `update_collection`, `import_collection`, `get_graph_cross_collection`, `graph_impact`, `create_key`, `list_keys`, `rotate_key` — an invalid argument (bad filter, too many `collections`, `rerank=false` across several `collections` on `explain`, a malformed archive, …) | Treat like REST `422`. Fix the arguments; do not retry unchanged. |
| `invalid_parameter` | `ingest_file` / `ingest_directory` — `chunk_ttl_seconds` or `chunk_scopes` out of range | Treat like REST `422`. |
| `invalid_scope_filter` | `search`, `search_with_context`, `explain` — malformed `scope_filter` pattern | Treat like REST `400`. |
| `invalid_graph_mode` | `search`, `explain` — unknown `graph_mode` value | Treat like REST `422`. |
| `scope_filter_graph_mode_incompatible` | `search`, `explain` — `scope_filter` combined with a `graph_mode` | Treat like REST `422`. |
| `graph_mode_not_supported` | `search_with_context` — any non-null `graph_mode` | Use the `search` tool for graph modes. |
| `graph_mode_multi_collection_unsupported` | `explain` — `graph_mode` with `collections` | Pin one `collection`. |
| `graph_disabled` | `search`, `explain`, `get_graph`, `get_graph_cross_collection`, `graph_impact` when `[graph] enabled = false` | Enable the graph subsystem or drop `graph_mode`. |
| `graph_communities_not_built` | `search`, `explain` — `graph_mode="local"` or `"global"` before the collection's communities are built | Rebuild communities, then retry. |
| `not_found` | an unknown collection (`search`, `search_with_context`, `explain`, `get_collection_meta`, `update_collection`, `export_collection`, the graph tools), archive (`import_collection`), or key (`revoke_key`) | Treat like REST `404`. |
| `forbidden` | `delete_document` when `namespace` differs from the caller's authenticated namespace | Treat like REST `403`. |
| `conflict` | `update_collection` while the collection's reindex job is pending or running; `rotate_key` while `ARCHON_SEARCH_API_KEY` is set | Treat like REST `409`. Retry after the reindex finishes, or unset the env var. |
| `collection_exists` | `import_collection` into an existing collection without `force_overwrite=True` | Pass `force_overwrite=True` or pick another name. |
| `embedding_model_mismatch` | `import_collection` when the archive's embedding model differs from the server's | Align the models before importing. |
| `schema_version_mismatch` | `import_collection` when the archive's export schema version differs | Pass `ignore_schema_version=True` only if you accept the risk. |
| `path_unsafe` | `ingest_file` / `ingest_directory` / `export_collection` / `import_collection` when `path` fails safety validation; `error` is an LLM-readable phrase (e.g. `"path is unsafe: the path contains a '..' segment — use an absolute path without traversal"`) | Treat like REST `400`. Use an absolute path with no `..` segments. |
| `store_busy` | `ingest_file` / `ingest_directory` / `delete_document` when a reindex holds the per-collection lock | Treat like REST `503`. Retry after a short wait; other collections are unaffected. |
| `metadata_store_error` | `search`, `explain` when collection metadata cannot be read | Treat like REST `503`. Retry with backoff. |
| `embedder_not_ready` | `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory` while the collection's embedding model is still loading | Treat like REST `503`. Retry with backoff. |
| `timeout` | `search` when the ~30 s search budget runs out or a multi-collection fan-out times out; `explain` when its fan-out times out | Treat like REST `504`. |

The `McpErrorResponse` `TypedDict` is declared in `mcp.py`. Since C7 (`API-4` resolved) the 11 search, ingest, collection, and document tools validate success payloads through `extra='forbid'` Pydantic schemas, but the export/import, graph, and key-management tools return plain dicts — schema-tolerant parsing is safer than strict matching.

Auth and transport errors at `/mcp` itself surface as standard HTTP responses from the wrapping ASGI middleware:

- `401` from `APIKeyMiddleware` — the MCP session never starts.
- `400` / `406` from `FastMCP`'s streamable HTTP layer — malformed framing. #Unverified (depends on `fastmcp` library internals, not on code in this repository).
- `404` from the MCP SDK's session manager — the `Mcp-Session-Id` is unknown or expired. Since `mcp` 1.30.0, a session with no request in flight and no open GET stream for 30 minutes is closed (the MCP SDK's default; `fastmcp` does not let archon change it); initialize a new session and retry. SDK clients that keep the GET stream open do not hit this.
- `503` from the same session manager — 10 000 sessions are already open and a new one is refused (existing sessions are unaffected). Retry with backoff.

Handle those at the SDK / fetch layer, not inside tool-result parsing.

## Job lifecycle errors

Background ingest is async. Submitting `POST /ingest` returns `202` even if the input is doomed (e.g. path does not exist). The failure shows up later as a job whose terminal state is `FAILED`:

```json
{
  "job_id": "...",
  "status": "FAILED",
  "result": null,
  "error": "path does not exist or is not a file/directory: /path/that/does/not/exist",
  ...
}
```

For ingest and reindex jobs the `error` field is a short string — never a stack trace or exception text. The full set: the missing-path refusal above (its `<path>` is the request path as resolved by `validate_ingest_path`, the same value as the job's `source_path`), the retryable `"service unavailable: embedding model still loading"` (`EMBEDDER_NOT_READY_DETAIL`), `"process_restart"` (the server restarted while the job was `RUNNING`/`CANCELLING`; `JobStore._load`), `"not a ReindexJob"` (`_reindex_task` handed a job id of another type), and otherwise the fixed `"ingest failed; see the server log for details"` (`INGEST_FAILED_DETAIL`; reindex: `"reindex failed; see the server log for details"`, `REINDEX_FAILED_DETAIL`). Other job types are not covered by this rule: e.g. a bulk export/import/migration job whose dispatch raises reports `dispatch_failed: <exception text>` (`archon_search/jobs/scheduler.py`) — see `Documentation/Architecture/140_error_handling_strategy.md`. For a full diagnosis, read the server log; the task logged `exception("Ingest task %s failed", job_id)` at the time of failure (`routes_jobs.py` `_default_ingest_task`).

`DELETE /jobs/{id}` is idempotent:

`JobStatus` is a `str` enum with upper-case wire values (`PENDING`, `QUEUED`, `RUNNING`, `DONE`, `FAILED`, `FAILED_EXPIRED`, `CANCELLED`, `CANCELLING`; see `archon_search/types.py:13-21`):

- Terminal job (`DONE` / `FAILED` / `CANCELLED`) → `200` with the unchanged record.
- Active job (`PENDING` / `RUNNING`) → `202`; status becomes `CANCELLING` and eventually `CANCELLED`.
- Already `CANCELLING` → `202` with the current record.

## Privacy in error responses

Telemetry records error classes (`ErrorKind ∈ {empty_query, slot_out_of_range, timeout, internal_error, validation_error, other}`), never the user's query string or the exception message. This is structural — see `archon_search/telemetry/entry.py` and `Architecture/150_security_and_privacy_architecture.md`. Clients that proxy errors to end users should follow the same discipline.

## Related documents

- [`../Architecture/140_error_handling_strategy.md`](../Architecture/140_error_handling_strategy.md) — server-side status code matrix with line citations.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `CON-5` (search-failure semantics) and `API-4` (MCP schemas).
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — A3 (fix `CON-5`), C7 (fix `API-4`).
- [`02_authentication.md`](./02_authentication.md) — `401` causes.
- [`../../BREAKING.md`](../../BREAKING.md) — contract changes.

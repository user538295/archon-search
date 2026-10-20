**Purpose**: Define what `archon-search` returns when things go wrong, on both REST and MCP, and tell clients what to retry vs. surface as failure.
**Audience**: Engineers building clients that must behave correctly under partial outage, slow ingest, network blips, and auth churn.
**Status**: Draft
**Last reviewed**: 2026-10-20 / **Next review**: 2027-10-12

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
| `409` | `POST /collections/` — path or name already registered; `DELETE /collections/{name}` — pinned-only | `{"detail": "..."}` | Reconcile config; do not retry blindly. |
| `422` | FastAPI body validation (e.g. empty `collection` / `query` in `/search`) | FastAPI's structured error body | Fix the request; never retry as-is. |
| `500` | Unmapped internal failure; `POST /search` — pipeline stage exception (embedder, store, reranker) (`routes_search.py`); `APIKeyMiddleware` returns a bare `500` (no body) if the resolved namespace fails revalidation (`middleware_auth.py:171-172`) | `{"detail": "..."}` for the routes that build a `JSONResponse({"detail": ...}, status_code=500)` (e.g. `routes_collections.py`, `routes_jobs.py`, a `POST /search` / `POST /explain` HyDE failure, a `POST /keys/rotate` key-file or `keys.json` write failure, a `POST /keys` / `DELETE /keys/{key_id}` / `POST /keys/rotate` refused because `keys.json` exists but cannot be read — `{"detail": "keys.json could not be read; no key changes were made; see the server log for details"}`, nothing written — or a blank server-held current key — `{"detail": "current_token must not be empty"}`, nothing written; a defensive check the default key loader cannot trigger). `POST /keys` / `DELETE /keys/{key_id}` failing to write `keys.json` for any other reason is still the plain-text `500`. **Exception: `POST /search` pipeline-failure 500 has a plain-text body `Internal Server Error` (Content-Type `text/plain`)** — the route bare-re-raises and Starlette's `ServerErrorMiddleware` renders the default response, not a JSON envelope. `APIKeyMiddleware`'s 500 path returns an empty body. | Backoff + retry once; surface to operator if it recurs. |
| `503` | `POST /search` — any exception raised by `pipeline.get_collection_meta` (`routes_search.py:482-489`) | `{"detail": "service unavailable: metadata store could not be reached", "code": "metadata_store_error"}` | Retry with exponential backoff. |
| `503` | `POST /collections/` — a reindex holds the per-collection ingest lock | `{"detail": "store busy; retry in N seconds"}` + header `Retry-After: N` | Honour `Retry-After`, then retry. Ingest to a *different* collection is unaffected. |
| `503` | `POST /ingest` — a reindex holds the per-collection ingest lock | `{"error": "store_busy", "detail": "..."}` + header `Retry-After: 30` (note: `error` key, not `detail`-only — normalisation deferred) | Honour `Retry-After`, then retry. |
| `504` | `POST /route` — 30 s routing timeout; `POST /search` — pipeline call timed out (~30 s) | `{"detail": "routing timed out"}` / `{"detail": "Search timed out"}` | Retry at most once; if persistent, check CPU pressure or model load. On `/search`, model load is no longer a cause — the reranker is warmed before the timer starts (S184). |

The full server-side mapping with file:line citations is in `Architecture/140_error_handling_strategy.md`.

### What to retry, what to fail

| Class | Examples | Retry? |
| --- | --- | --- |
| Transient network | connection reset, `httpx.RemoteProtocolError` | Yes — short backoff (e.g. 200ms, 500ms, 1s). |
| `503` from `/search` | Meta lookup race during reindex | Yes — same backoff. |
| `503` from `POST /collections/` (store busy) | Reindex holds the per-collection lock | Yes — honour `Retry-After`, then retry. Body: `{"detail": "store busy; retry in N seconds"}`. |
| `503` `store_busy` from `POST /ingest` | Reindex holds the per-collection lock | Yes — honour `Retry-After` (30s), then retry. Body: `{"error": "store_busy", ...}`. |
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
2. **`422` from request validation** — FastAPI's Pydantic-v2 validation shape, where `detail` is a **list** of structured error objects (see the example below), not a string. A `422` a route raises itself does not use that shape: most carry a string `detail` — e.g. the HyDE / RAG Fusion missing-extra `422`s (`HYDE_DEPENDENCY_DETAIL`, `RAG_FUSION_DEPENDENCY_DETAIL`), `POST /search` / `POST /explain` `top_k` / fan-out / `graph_mode` guards, `POST /keys` namespace validation, `routes_graph.py` guards, `POST /collections/` `embedding_model` validation, `POST /collections/{name}/migrate` guards; some carry an object — `{"detail": {"code": "invalid_scope_filter" \| "graph_communities_not_built", "message": ...}}` on `POST /search` / `POST /explain`, `{"error": ..., "detail": ...}` on `POST /collections/{name}/import`, and `{"error": "source_not_found"}` with no `detail` on `POST /jobs/{id}/resume`. Branch on `isinstance(detail, list)` before parsing.
3. **`POST /search` `500` on pipeline failure** — plain-text body `Internal Server Error` with `Content-Type: text/plain`. The route bare-re-raises the pipeline exception and Starlette's `ServerErrorMiddleware` renders the default response; there is no `detail` key and the body is not JSON. Branch on `response.status_code` (and `Content-Type`) before parsing.

`schemas.ErrorDetail` is registered on the `responses=` map of most routes (e.g. `routes_collections.py`, `routes_jobs.py`, `routes_keys.py`), so the OpenAPI schema documents this envelope per route for the statuses they declare (typically `401`, and where relevant `404` / `409` / `422`). `POST /search` and `POST /explain` also declare `400` (`CodedErrorDetail`, `invalid_scope_filter`), `404` and `504` (`ErrorDetail`), and `422` as `anyOf` `HTTPValidationError`, `CodedErrorDetail` and `ErrorDetail`. `POST /search` declares `500` and `503`: its `500` documents only the HyDE-failure `{"detail": "search failed"}` body, not the plain-text pipeline-failure `500` above. `POST /explain` declares `500` and `503`: its `500` covers both JSON bodies it returns, `{"detail": "<stage> error: <ExceptionType>"}` and `{"detail": "explain failed"}`, and says that any other failure is a plain-text `500`; `POST /keys/rotate` declares `401`, `409` and `500` (its JSON rotation failures). `routes_route.py` registers no `responses=` map, so `POST /route`'s `400` / `504` envelopes are not documented in OpenAPI.

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
| `internal_error` | the catch-all `except Exception` of the search, ingest, collection, document, and graph tools and of `create_key`; a failed `explain` pipeline stage; a HyDE failure other than the missing provider package on `search` / `search_with_context` / `explain`; `export_collection` / `import_collection` without a job store, or failing to look up the collection or to create the job; `revoke_key` failing to write `keys.json`; `create_key` / `revoke_key` / `rotate_key` finding `keys.json` present but unreadable (nothing written); `rotate_key` failing to resolve `ARCHON_SEARCH_KEY_FILE`, write `.search.env` or update `keys.json`, failing in any other unexpected way (its catch-all, logged as `rotate_key failed`), or finding the current key blank (a defensive server-fault check, made before `.search.env` is written, that the tool's key source — `app.state.api_key` when mounted, never blank — cannot trigger). `list_keys` has no catch-all: an ordinary exception there reaches the client as FastMCP's bare `Error calling tool 'list_keys'` (the app sets `mask_error_details=True`). Masking has limits: FastMCP re-raises a pydantic `ValidationError` raised inside a tool body, and any `FastMCPError` (`ToolError`, …), unmasked, and the MCP server sends its text — so tools must still catch those themselves | Unhandled server-side failure. Treat like REST `500`. The `error` is never the exception text (the exception is in the server log). It is `internal error; see the server log for details` (`INTERNAL_ERROR_DETAIL`) except: `search` / `search_with_context` return `search failed` (`_SEARCH_FAILED_DETAIL`, also the REST `/search` HyDE-failure text); `explain`'s catch-alls and its HyDE failure return `explain failed` (`routes_explain.EXPLAIN_FAILED_DETAIL`, shared with the REST `/explain` route) and a failed stage `<stage> error: <ExceptionType>`; `create_key` returns `key creation failed; see the server log for details` (`CREATE_KEY_FAILED_DETAIL`); a missing job store returns `job store not configured` (`JOB_STORE_NOT_CONFIGURED_DETAIL`); an unreadable `keys.json` on `create_key` / `revoke_key` / `rotate_key` returns `keys.json could not be read; no key changes were made; see the server log for details` (`KEY_STORE_UNREADABLE_DETAIL`, also the REST `500` `detail`); a `rotate_key` failure to resolve `ARCHON_SEARCH_KEY_FILE`, create the key file's directory or write `.search.env` returns `Failed to write .search.env — rotation aborted; keys.json unchanged.` (`ROTATE_WRITE_FAILED_DETAIL`), and a failure to update `keys.json` after `.search.env` was written returns `Rotation incomplete: .search.env holds the new key but keys.json was not updated; see the server log for details` (`ROTATE_KEYS_JSON_FAILED_DETAIL`) — both the REST `POST /keys/rotate` `500` text — and a blank current key returns `current_token must not be empty` (the `BlankCurrentKeyError` text, also the REST `500` `detail`; `BlankCurrentKeyError` is a sibling of `KeyRotationInputError`, not a subclass, so it never answers `validation_error`). `search` / `search_with_context` / `explain` also record an error telemetry entry for every `internal_error`. |
| `schema_validation_error` | the 11 schema-validated tools, when a success payload fails its Pydantic schema | Server-side contract drift. Treat like REST `500`; report it. The `error` is fixed text, the `ValidationError` logged: `search response failed schema validation` on `search` / `search_with_context`, `explain response failed schema validation` on `explain`, and `response schema validation failed; see the server log for details` (`SCHEMA_VALIDATION_DETAIL`) on the other tools. |
| `validation_error` | `search`, `search_with_context`, `explain`, `update_collection`, `import_collection`, `get_graph_cross_collection`, `graph_impact`, `create_key`, `list_keys`, `rotate_key` — an invalid argument (bad filter, too many `collections`, `rerank=false` across several `collections` on `explain`, a malformed archive, a `rotate_key` `grace_seconds` outside `0`…`MAX_ROTATE_GRACE_SECONDS` (`grace_seconds must be >= 0, got <n>` / `grace_seconds must be <= 3153600000, got <n>`) — checked before `.search.env` is written, …; a rejected `search` / `search_with_context` filter argument answers the fixed `invalid search filters`, never the Pydantic text that echoes the value); on `search` / `search_with_context` / `explain`, also a missing provider extra: `Install archon-search[hyde] to use HyDE` (`HYDE_DEPENDENCY_DETAIL` / `HYDE_DEPENDENCY_CODE`) or `Install archon-search[rag_fusion] to use RAG Fusion` (`RAG_FUSION_DEPENDENCY_DETAIL` / `RAG_FUSION_DEPENDENCY_CODE`), the install command itself logged as a WARNING only | Treat like REST `422`. Fix the arguments (or install the extra); do not retry unchanged. |
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

The `error` field is a short string — never a stack trace or an internal exception's text. A `path` that names neither a file nor a directory raises `IngestPathNotFoundError`, whose curated message (`INGEST_PATH_NOT_FOUND_PREFIX` plus the caller's `path` as submitted — before `~` expansion and symlink resolution; `IngestRequest.submitted_path` in `routes_jobs.py`) is recorded verbatim and logged at WARNING. The job `error` echoes the path as submitted; the job's `source_path` (`JobResponse`, from `POST /ingest` and `GET /jobs/{id}`), when set, holds the resolved path — it is `""` for a `collection add` job. A still-loading embedder reports `EMBEDDER_NOT_READY_DETAIL`. Every other ingest failure reports the fixed `ingest failed; see the server log for details` (`INGEST_FAILED_DETAIL`); for the cause, read the server log, where the task logged `exception("Ingest task %s failed", job_id)` with the traceback (`_default_ingest_task` / `_default_ingest_task_with_lock` in `routes_jobs.py`).

`DELETE /jobs/{id}` is idempotent:

`JobStatus` is a `str` enum with upper-case wire values (`PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`, `CANCELLING`; see `archon_search/types.py:10-16`):

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

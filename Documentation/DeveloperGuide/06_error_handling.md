**Purpose**: Define what `archon-search` returns when things go wrong, on both REST and MCP, and tell clients what to retry vs. surface as failure.
**Audience**: Engineers building clients that must behave correctly under partial outage, slow ingest, network blips, and auth churn.
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Error Handling

There are two error envelopes in `archon-search`. REST uses an HTTP status code plus `{"detail": "..."}`. MCP uses HTTP 200 with `{"error": "...", "code": "..."}` as the tool result. Pick your handling layer accordingly.

This page is a client-side companion to `Documentation/Architecture/140_error_handling_strategy.md`; that doc covers the server's perspective.

## Principles

1. **Status code first, message second.** REST clients should branch on HTTP status, not on `detail` text. `detail` strings are stable enough for humans but not part of the typed contract.
2. **`401` has no body.** Auth failures return `401` with `WWW-Authenticate: Bearer` and an empty body — same response for missing header, wrong scheme, and unknown token. Don't try to parse JSON from a 401.
3. **`404` covers cross-namespace.** A resource that exists in another namespace looks like "not found" to your token — by design (`150_security_and_privacy_architecture.md`).
4. **`/search` may lie about success.** A pipeline failure currently returns `200` with `results: []` and `acl_filtered: false` (debt `CON-5` #Unverified; planned fix is roadmap item A3 in `Documentation/Backlog/03_world_class_roadmap.md` #Unverified). Build dashboards that alarm on a sustained empty-results window.
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
| `500` | Unmapped internal failure; also `APIKeyMiddleware` returns a bare `500` (no body) if the resolved namespace fails revalidation (`middleware_auth.py:55-59`) | `{"detail": "..."}` (or empty from the middleware path) | Backoff + retry once; surface to operator if it recurs. |
| `503` | `POST /search` — any exception raised by `pipeline.get_collection_meta` (`routes_search.py:67-71`) | `{"detail": "service unavailable"}` | Retry with exponential backoff. |
| `503` | `POST /collections/`, `POST /ingest` — a reindex holds the per-collection ingest lock | `{"error": "store_busy", "detail": "..."}` + header `Retry-After: 30` (note: `error` key, not `detail`-only) | Honour `Retry-After`, then retry. Ingest to a *different* collection is unaffected. |
| `504` | `POST /route` — 30 s routing timeout | `{"detail": "routing timed out"}` | Retry at most once; the routing layer is the bottleneck. |

The full server-side mapping with file:line citations is in `Architecture/140_error_handling_strategy.md`.

### What to retry, what to fail

| Class | Examples | Retry? |
| --- | --- | --- |
| Transient network | connection reset, `httpx.RemoteProtocolError` | Yes — short backoff (e.g. 200ms, 500ms, 1s). |
| `503` from `/search` | Meta lookup race during reindex | Yes — same backoff. |
| `503` `store_busy` from `/collections/` or `/ingest` | Reindex holds the per-collection lock | Yes — honour `Retry-After` (30s), then retry. |
| `504` from `/route` | Embedder warm-up, slow centroid load | Once, then surface. |
| `500` | Unhandled exception | Once, then surface — log the response. |
| `401` | Token rotated underneath the client | Reload key, retry once. After that, fail loudly. |
| `404`, `409`, `400`, `422` | Logical error | No — fix the input or config. |
| `200` with `results: []` on `/search` | Either no hits or `CON-5` failure | Do not retry automatically. Alarm on a sustained empty window. |

## REST error envelope

For every status except `401` and FastAPI's `422`, the body is:

```json
{"detail": "human-readable string"}
```

`schemas.ErrorDetail` is registered on the `responses=` map of routes in `routes_collections.py` and `routes_jobs.py`, so the OpenAPI schema documents this envelope per route for the statuses they declare (typically `401`, and where relevant `404` / `409`). `routes_search.py` and `routes_route.py` do **not** register a `responses=` map — the envelope shape is the same on the wire, but is not documented in OpenAPI for those endpoints.

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

Verified `code` values today (`mcp.py`):

| `code` | Where | Meaning |
| --- | --- | --- |
| `internal_error` | every tool's `except Exception` branch | Unhandled server-side failure. Treat like REST `500`. |
| `not_found` | `get_collection_meta` when the name is unknown | Treat like REST `404`. |
| `path_unsafe` | `ingest_file` / `ingest_directory` when `path` fails safety validation; `error` is an LLM-readable phrase (e.g. `"path is unsafe: the path contains a '..' segment — use an absolute path without traversal"`) | Treat like REST `400`. Use an absolute path with no `..` segments. |
| `store_busy` | `ingest_file` / `ingest_directory` when a reindex holds the per-collection lock | Treat like REST `503`. Retry after a short wait; ingest to a different collection is unaffected. |

The `McpErrorResponse` `TypedDict` is declared at `mcp.py:25`. Because MCP tools are not yet Pydantic-validated (`API-4` / roadmap C7 #Unverified), do not assume *additional* fields will never appear in a success payload — schema-tolerant parsing is safer than strict matching.

Auth and transport errors at `/mcp` itself surface as standard HTTP responses from the wrapping ASGI middleware:

- `401` from `APIKeyMiddleware` — the MCP session never starts.
- `400` / `406` from `FastMCP`'s streamable HTTP layer — malformed framing. #Unverified (depends on `fastmcp` library internals, not on code in this repository).

Handle those at the SDK / fetch layer, not inside tool-result parsing.

## Job lifecycle errors

Background ingest is async. Submitting `POST /ingest` returns `202` even if the input is doomed (e.g. path does not exist). The failure shows up later as a job whose terminal state is `FAILED`:

```json
{
  "job_id": "...",
  "status": "FAILED",
  "result": null,
  "error": "FileNotFoundError: /path/that/does/not/exist",
  ...
}
```

The `error` field is a short string — never a stack trace. If you need a full diagnosis, read the server's stderr; the route logged `exception("Ingest task %s failed", job_id)` at the time of failure (`routes_jobs.py:81`).

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

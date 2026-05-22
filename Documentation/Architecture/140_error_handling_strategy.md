**Purpose**: Define how `archon-search` surfaces, classifies, and records errors across config load, the REST surface, jobs, and telemetry.
**Audience**: Maintainers writing new endpoints, handlers, or jobs.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# Error Handling Strategy

Errors in archon-search are classified at three layers: configuration, HTTP request handling, and background jobs. Each layer has a distinct contract — config fails the process, HTTP returns a typed status, jobs persist a terminal `FAILED` state. Telemetry only ever records a coarse `error_kind` from a fixed enum, never an exception message.

See also: [100_system_architecture_overview.md](100_system_architecture_overview.md), [120_services_and_integration_architecture.md](120_services_and_integration_architecture.md), [160_operational_readiness_monitoring_and_reliability.md](160_operational_readiness_monitoring_and_reliability.md).

## Principles

1. **Fail fast at config load.** Invalid TOML, out-of-range integers, wrong types → `ConfigError` raised by `config.py::load_config`. The process does not start.
2. **Propagate typed exceptions.** Domain layers raise `ValueError`, `KeyError`, `RuntimeError`, `NotImplementedError`, `ConfigError`. (`asyncio.TimeoutError` arises in the request path from `asyncio.wait_for(...)` in `routes_route.py`; it is not a typed domain exception raised by lower layers.) Route handlers translate these to HTTP status codes — exceptions never leak as raw 500s when a typed mapping exists. Pipeline failures on `/search` surface as HTTP 500 with `{"detail": "Internal Server Error"}`; a timeout returns HTTP 504 after ~30s (matching the behaviour of `routes_route.py`).
3. **Coarse, closed-set telemetry classification.** When telemetry records an error, it uses `ErrorKind ∈ {empty_query, slot_out_of_range, timeout, internal_error, validation_error, other}` (see `telemetry/entry.py::ErrorKind`). Never store exception messages, stack traces, or input strings.
4. **Auth failures are uniform.** The auth middleware returns `401` with `WWW-Authenticate: Bearer` and no body — same response for missing header, wrong scheme, and bad token. (Exception: a token that *does* resolve to a namespace but fails `_validate_namespace` returns `500`, not `401`; see "Authentication errors" below.)
5. **Jobs record terminal failure state.** A failed background job transitions to `JobStatus.FAILED` with a short `error` string; the HTTP layer remains `202` at submission time.

## Configuration errors

`archon_search/config.py::ConfigError` is raised for:

- TOML parse failure (`Failed to parse {path}: {exc}`)
- Wrong type — per-type messages, not slash-joined: `Expected integer for '{field}', got {type}` (`config.py:98`), `Expected float for '{field}', got {type}` (`config.py:105`), `Expected boolean for '{field}', got {type}` (`config.py:110`)
- Out-of-range / non-empty checks:
  - `port must be between 1 and 65535` (`config.py:137`)
  - `chunk_size must be > 0` (`config.py:150`)
  - `top_k_retrieve must be > 0` (`config.py:161`)
  - `top_k_return must be > 0` (`config.py:166`)
  - `routing_shortlist_size must be > 0` (`config.py:173`)
  - `routing_confidence_threshold must be in [0.0, 1.0]` (`config.py:178`)
  - `max_parallel_collections must be > 0` (`config.py:183`)
  - `[telemetry].retention_days must be >= 1` (`config.py:207`)
  - `[telemetry].log_dir must be a non-empty string` (`config.py:221`)
- Invalid `[namespaces]` shape (non-string key or value)

`[telemetry].export_enabled = true` is **not** raised — `config.py:213–215` emits a warning and silently coerces to `false`. This is the documented v1 contract: external export is reserved and ignored, not rejected.

`ConfigError` is caught explicitly by the `archon-search start` subcommand (`cli/start.py:16-23`), which prints `Error: {exc}` to stderr and exits with `SystemExit(1)`. Other entry points (e.g. importing the server module directly in tests) propagate the exception per Python defaults — "the process does not start" is enforced by the `start` path, not centrally.

## Authentication errors

`server/middleware_auth.py::APIKeyMiddleware`:

- Missing `Authorization` header, wrong scheme, or token that fails `secrets.compare_digest` against both the per-namespace key map and the default API key → `401`, header `WWW-Authenticate: Bearer`, empty body.
- Exempt paths (`/health`, `/docs`, `/openapi.json`, `/redoc`) bypass the middleware entirely.
- If a resolved namespace fails `_validate_namespace`, the middleware logs and returns `500` (defensive — should not occur if config validates).

The middleware iterates the full namespace map without short-circuit (`resolved_namespace = ns  # no break` — `middleware_auth.py:43`) to prevent timing-based token discovery.

## REST surface — status code mapping

Verified from `archon_search/server/routes_*.py`:

| Category | HTTP status | Source / trigger | Telemetry `error_kind` |
|----------|-------------|------------------|-------------------------|
| Missing/invalid bearer token | `401` | `middleware_auth.py` | n/a (pre-handler) |
| Resolved namespace fails `_validate_namespace` | `500` | `middleware_auth.py:55-59` | n/a (pre-handler) |
| Empty query (explicit handler check) | `400` | `routes_route.py:76` | `empty_query` |
| Slots out of range (explicit handler check) | `400` | `routes_route.py:79` | `slot_out_of_range` |
| Bad request body — Pydantic body validation | `422` | FastAPI default | none (exception fires before the handler body; telemetry is **not** recorded) |
| Bad request body — `routes_route` explicit 400 | `400` | `routes_route.py:76, 79` | `validation_error` (via `_redact_validation`, mapped to `empty_query` / `slot_out_of_range` / `validation_error`) |
| Collection not found | `404` | `routes_search.py:92-93` (returns `JSONResponse` directly), `routes_collections.py:180, 186, 243, 251, 309, 313` | n/a (not logged) |
| Job not found | `404` | `routes_jobs.py:113, 115, 133, 135, 145` | n/a |
| Collection name conflict | `409` | `routes_collections.py:130, 136, 148, 200` | n/a |
| `pipeline.get_collection_meta(...)` raises any exception (meta-lookup failure — `/search` only; `/route` calls `get_all_collections_meta`, not `get_collection_meta`, and its failure path falls through to the generic 500 re-raise) | `503` | `routes_search.py:86-90` (catches `Exception`, returns `JSONResponse({"detail": "service unavailable"}, status_code=503)`) | none — the meta-lookup exception handler (`routes_search.py:86-90`) does not enqueue telemetry; `TelemetryEntry.from_error` is imported and called on the pipeline-failure and timeout paths |
| Store lock contention — `StoreBusyError` raised by an ingest/reindex runner when the LanceDB write lock is already held (`/ingest/*`, `/reindex/*`) | `503` + `Retry-After` | ingest/reindex job runners (planned A5c; not yet delivered — the row is here as a reserved contract entry) | none |
| `/search` pipeline stage exception (`embedder`, `store`, `reranker`, ACL filter, or response serialization raises — A3 / CON-5) | `500` | `routes_search.py` (bare re-raise; FastAPI default `{"detail": "Internal Server Error"}`) | `other` (with `status="internal_error"`) |
| `/search` pipeline timed out (`asyncio.TimeoutError` from `wait_for` — A3 / CON-5) | `504` | `routes_search.py` (`raise HTTPException(status_code=504, detail="Search timed out")`) | `timeout` |
| Routing timed out (`asyncio.TimeoutError` from `wait_for`) | `504` | `routes_route.py:122-135` | `timeout` |
| Stub-meta-write rollback failure on collection add | `500` | `routes_collections.py:149-155` (`JSONResponse({"detail": "internal error"}, status_code=500)`) | none — `routes_collections.py` contains no `from_error` call |
| `DELETE /jobs/{id}` with unrecognized `JobStatus` (defensive `else`) | `500` | `routes_jobs.py:153-157` | none — `routes_jobs.py` does not enqueue telemetry |
| Unmapped exception in `/route` handler body | (re-raised, surfaces per FastAPI default; typically `500`) | `routes_route.py:152-166` | `other` (with `status="internal_error"`) |
| Telemetry parameter validation | `400` | `routes_telemetry.py:36, 61` | n/a (telemetry endpoint itself) |

Successful job submissions (POST collection-add, POST reindex, POST ingest) return `202 Accepted` with a `JobResponse`; the job's eventual outcome is observed via `GET /jobs/{job_id}`.

## Job error handling

Defined in `archon_search/types.py`:

- `JobStatus ∈ {PENDING, RUNNING, DONE, FAILED, CANCELLED, CANCELLING}`.
- An `IngestJob` carries `result: dict | None` on success and `error: str | None` on failure.
- The cancellation handshake is `{PENDING, RUNNING} → CANCELLING → CANCELLED`. `DELETE /jobs/{id}` accepts either active origin state — `_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.PENDING}` (`routes_jobs.py:24, 138-148`); the runner then observes `CANCELLING` and writes `CANCELLED` (or `asyncio.CancelledError` propagates and the runner writes `CANCELLED` directly).

Job runners catch their own exceptions and write a short message into `error`; the next `GET /jobs/{job_id}` returns the terminal state. As of this writing, job runners do not push records into telemetry — telemetry is the request-path concern only. This is an unenforced convention (no test or lint barrier), not a contract: there is currently no call to `TelemetryEntry` from `archon_search/jobs/` or the default ingest task.

## Telemetry error contract

`telemetry/entry.py` defines a closed enum (`ErrorKind`) and a `from_error` factory that requires a non-`ok` status. The model is frozen and `extra="forbid"`, so callers cannot smuggle in additional fields. The factory takes no `query` parameter — by construction, raw query strings cannot end up in a telemetry record. This is reaffirmed in [150_security_and_privacy_architecture.md](150_security_and_privacy_architecture.md).

The mapping from internal exception to `ErrorKind` is the responsibility of the route handler when it calls `TelemetryEntry.from_error(...)`. The set is intentionally small and stable; new categories require a code change to the enum, not a runtime tag.

## What is intentionally NOT done

- No global FastAPI exception handler that rewrites unknown exceptions into rich JSON (verified by absence of `add_exception_handler` in `server/app.py` outside middleware; treat as a present-tense fact, not an invariant). An unmapped exception becomes a plain `500` — preferable to leaking internals.
- No retry middleware. The CLI and the watcher have their own retry semantics; the HTTP layer is a single attempt.
- No structured error code field in REST responses. The `detail` string plus the HTTP status is the contract; breaking changes go in `BREAKING.md`.

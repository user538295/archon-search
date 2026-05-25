# Review: Architecture/140_error_handling_strategy.md

## Summary

The doc is largely accurate but contains several material inaccuracies. The most serious are:
(a) the claim that `ConfigError` is "unhandled by `cli/main.py`" — in fact `cli/start.py` catches it explicitly; (b) the claim that the 503 in `routes_search.py` records `internal_error` telemetry — `routes_search.py` does **not** enqueue any telemetry on the meta-lookup failure path; (c) the framing of that same 503 as "service not ready / pipeline missing" — the code triggers it on *any* exception from `pipeline.get_collection_meta`, not a missing pipeline; (d) several off-by-one or misleading line-number citations; (e) the cancellation handshake omits `PENDING` as a valid origin state.

Verified ground-truth claims include the auth middleware behavior, the `ErrorKind` enum, the `JobStatus` enum, the `from_error` factory contract, and the 400/404/409 mappings in collections/jobs/route routes.

## Inaccuracies (numbered: quoted claim, ground truth, file:line, severity)

1. **Quoted**: "`ConfigError` is unhandled by `cli/main.py` startup — the process exits non-zero with the message on stderr."
   **Ground truth**: `cli/main.py` does not load config at all. `cli/start.py:16-23` explicitly wraps `load_config(...)` in `try/except ConfigError`, prints `f"Error: {exc}"` to stderr, and raises `SystemExit(1)`. So the effect (non-zero exit, stderr message) is right, but the *mechanism* (which file, "unhandled") is wrong.
   **Ref**: `archon_search/cli/start.py:14-23`.
   **Severity**: Moderate (misleads anyone tracing the error path).

2. **Quoted**: "Service not ready / pipeline missing | `503` | `routes_search.py:71` | `internal_error`" (REST status-code table).
   **Ground truth**: The 503 at `routes_search.py:71` fires when `await pipeline.get_collection_meta(...)` raises *any* exception (caught by `except Exception as exc`). It is not gated on "pipeline missing" — `pipeline` is unconditionally pulled from `request.app.state.pipeline` (would `AttributeError` first). The handler does **not** enqueue a telemetry entry at all — the `internal_error` column is fabricated. `routes_search.py` never imports or calls `TelemetryEntry.from_error`.
   **Ref**: `archon_search/server/routes_search.py:62-84` (no telemetry imports, no `from_error` call). Compare `routes_route.py:152-165` which *does* enqueue `internal_error`.
   **Severity**: Major (table column claims a telemetry contract the code does not implement).
   **Update (superseded by A3)**: Post-A3, `routes_search.py` imports `TelemetryEntry` (line 13) and *does* enqueue telemetry on the pipeline-failure and timeout paths (calls at lines 109 and 129). The 503 meta-lookup branch (now at lines 86-90) still does not enqueue telemetry — that part of the finding stands — but the "never imports or calls `TelemetryEntry.from_error`" claim is no longer accurate.

3. **Quoted**: "The cancellation handshake is `RUNNING → CANCELLING → CANCELLED`".
   **Ground truth**: `_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.PENDING}` (`routes_jobs.py:24`); a DELETE on a PENDING job also transitions it to `CANCELLING`. The handshake is `{PENDING, RUNNING} → CANCELLING → CANCELLED`.
   **Ref**: `archon_search/server/routes_jobs.py:24, 138-148`.
   **Severity**: Moderate.

4. **Quoted**: "Routing timed out | `504` | `routes_route.py:135` | `timeout`".
   **Ground truth**: The telemetry side is correct (`from_error(..., error_kind="timeout")` at lines 126-131). The `raise HTTPException(status_code=504, ...)` is at line 135. Verified.
   **Severity**: None — listed here only as a *verified* anchor for the rest of the table.

5. **Quoted**: "Unmapped internal failure | `500` | `routes_collections.py:155`, `routes_jobs.py:156` | `internal_error`".
   **Ground truth**:
   - `routes_collections.py:155` returns `JSONResponse({"detail": "internal error"}, status_code=500)` on stub-meta-write failure — verified. No telemetry is enqueued in `routes_collections.py` (no `from_error` call exists in this file).
   - `routes_jobs.py:156` returns `JSONResponse(..., status_code=500)` on `DELETE /jobs/{id}` when the job is in an unknown status (not "unmapped internal failure" generically — it's a defensive `else` for an unrecognized `JobStatus`). No telemetry is enqueued.
   So the `internal_error` telemetry column is wrong for both sources.
   **Ref**: `archon_search/server/routes_collections.py:149-155`; `archon_search/server/routes_jobs.py:149-157`.
   **Severity**: Moderate (telemetry column repeatedly claims behavior the code does not perform).
   **Update (superseded by A3)**: Post-A3, there is now a separate 500 source — `routes_search.py` (lines 124-144) bare-re-raises pipeline exceptions, and that path *does* enqueue telemetry with `status="internal_error"` (line 129). The original findings about `routes_collections.py` and `routes_jobs.py` still stand.

6. **Quoted**: "The middleware iterates the full namespace map without short-circuit (`no break` — `middleware_auth.py:42`)".
   **Ground truth**: The `# no break` comment is at line **43** (`resolved_namespace = ns  # no break`), not 42. Line 42 is the `secrets.compare_digest` call.
   **Ref**: `archon_search/server/middleware_auth.py:41-44`.
   **Severity**: Minor (off-by-one line cite).

7. **Quoted** (Principles §2): "Domain layers raise `ValueError`, `KeyError`, `RuntimeError`, `NotImplementedError`, `ConfigError`, `TimeoutError`."
   **Ground truth**: `TimeoutError` is not raised by domain code as a typed exception. The only timeout in the request path is `asyncio.TimeoutError`, raised by `asyncio.wait_for(...)` in `routes_route.py:94-101` and caught at `routes_route.py:122`. `telemetry/writer.py:107` *catches* `TimeoutError` (queue-put timeout) but does not raise it as a domain signal. Listing bare `TimeoutError` alongside `ConfigError` etc. overstates the typed-exception story.
   **Ref**: `archon_search/server/routes_route.py:94-135`; `archon_search/telemetry/writer.py:107`.
   **Severity**: Minor.

8. **Quoted** (Principles §2): "Route handlers translate them to HTTP status codes — exceptions never leak as raw 500s when a typed mapping exists."
   **Ground truth**: True as of A3 (CON-5). Pre-A3, `routes_search.py:82-84` swallowed every pipeline exception and returned HTTP 200 with empty `SearchResponse` — the opposite of what the principle requires. That exception path was fixed in A3: `routes_search.py` now bare-re-raises pipeline exceptions (HTTP 500 via FastAPI default) and raises `HTTPException(status_code=504, detail="Search timed out")` on timeout. The doc's stated principle now accurately reflects the shipped behavior.
   **Ref**: `archon_search/server/routes_search.py` (post-A3); `BREAKING.md` `[next release]` — `POST /search` pipeline-exception behavior.
   **Severity**: Resolved — this was an inaccuracy in the reviewed doc, corrected in A3.

9. **Quoted**: "TOML parse failure (`Failed to parse {path}: ...`) … Wrong type (`Expected integer/float/boolean for '{field}'`)".
   **Ground truth**: The actual messages are per-type, not slash-joined: `Expected integer for '{field}', got {type}`, `Expected float for '{field}', got {type}`, `Expected boolean for '{field}', got {type}` (config.py:98, 105, 110). Minor presentational issue — readers might grep for the exact slashed form and miss the real messages.
   **Ref**: `archon_search/config.py:94-111`.
   **Severity**: Minor.

10. **Quoted**: "Out-of-range (`port must be between 1 and 65535`, `routing_confidence_threshold must be in [0.0, 1.0]`, `chunk_size must be > 0`, `[telemetry].retention_days must be >= 1`)".
    **Ground truth**: All four exist. *Additional* out-of-range / non-empty checks the doc omits: `top_k_retrieve must be > 0`, `top_k_return must be > 0`, `routing_shortlist_size must be > 0`, `max_parallel_collections must be > 0`, `[telemetry].log_dir must be a non-empty string`. Not factually wrong — incomplete.
    **Ref**: `archon_search/config.py:158-184, 221`.
    **Severity**: Minor (completeness).

11. **Quoted**: "Exempt paths (`/health`, `/docs`, `/openapi.json`, `/redoc`) bypass the middleware entirely."
    **Ground truth**: Verified literally in `_EXEMPT_PATHS` (`middleware_auth.py:16`). However, the **Principles §4** sentence "same response for missing header, wrong scheme, and bad token" omits one branch present in the middleware: a successfully resolved namespace that fails `_validate_namespace` returns **500**, not 401 (correctly noted later in the doc, but the §4 principle is overstated as written).
    **Ref**: `archon_search/server/middleware_auth.py:16, 29-59`.
    **Severity**: Minor (internal contradiction within the doc).

12. **Quoted**: "Collection not found | `404` | `routes_search.py:74`, `routes_collections.py:180/186/243/251/309/313`".
    **Ground truth**: All cited lines verified as 404 sources. `routes_search.py:74` returns `JSONResponse({"detail": "collection not found"}, status_code=404)` (not an `HTTPException`, so the response shape is `{"detail": "..."}` via JSONResponse, not FastAPI's automatic `HTTPException` rendering — same wire format, different code path; worth noting if a maintainer expects uniform shape).
    **Severity**: None (verified) — flagged for completeness.
    **Update (superseded by A3)**: Post-A3 the 404 source in `routes_search.py` moved to lines 92-93; the `routes_collections.py` line refs are unchanged.

13. **Quoted**: "Bad request body (Pydantic) | `400` / `422` | FastAPI validation | `validation_error`".
    **Ground truth**: FastAPI's default for Pydantic body validation errors is **422 Unprocessable Entity**, not 400. The `400` in the same row is reachable only via the `routes_route.py` explicit `HTTPException(400, "query must not be empty")` / `"slots must be >= 1"` — that path *is* mapped to `validation_error` telemetry via `_redact_validation`. The Pydantic-validation path returns 422 and does **not** record telemetry (the exception happens before the handler body runs). Pretending "400 / 422" share one telemetry contract obscures that.
    **Ref**: `archon_search/server/routes_route.py:54-79, 137-150`; FastAPI default.
    **Severity**: Moderate.

## Verified claims

- `ConfigError` class location and the listed validation triggers exist (`archon_search/config.py:15, 129, 137, 150, 178, 207, 229`).
- `export_enabled = true` is silently coerced to `False` with a warning at `archon_search/config.py:213-215`.
- `[namespaces]` non-string key/value raises `ConfigError` at `archon_search/config.py:229-231`.
- `APIKeyMiddleware` exempt paths frozenset and 401 emission with `WWW-Authenticate: Bearer` and empty body (`middleware_auth.py:16, 32-35, 49-53`).
- `_validate_namespace` failure → 500 (`middleware_auth.py:55-59`).
- 504 on routing timeout with `error_kind="timeout"` telemetry (`routes_route.py:122-135`).
- 409 conflict triggers at `routes_collections.py:130, 136, 148, 200`.
- 404 collection-not-found triggers at `routes_collections.py:180, 186, 243, 251, 309, 313`.
- 404 job-not-found triggers at `routes_jobs.py:113, 115, 133, 135, 145`.
- 400 in telemetry routes at `routes_telemetry.py:36, 61`.
- `ErrorKind` enum = `{empty_query, slot_out_of_range, timeout, internal_error, validation_error, other}` (`telemetry/entry.py:31-37`).
- `TelemetryEntry` is frozen with `extra="forbid"` and has no `query` field (`telemetry/entry.py:57-58`).
- `from_error(...)` accepts only `endpoint, status, error_kind, latency_ms` keyword-only; raises `ValueError` on `status == "ok"` (`telemetry/entry.py:127-145`).
- `JobStatus` enum exactly = `{PENDING, RUNNING, DONE, FAILED, CANCELLED, CANCELLING}` (`archon_search/types.py:10-16`).
- `IngestJob` has `result: dict | None` and `error: str | None` (`archon_search/types.py:19-27`).
- Job runner writes a short `error` string and transitions to `FAILED` on exception (`routes_jobs.py:80-85`).
- POST collection add / reindex / ingest all return 202 (`routes_collections.py:114, 299`; `routes_jobs.py:91`).

## Unverifiable / ambiguous

- "Job runners do **not** push records into telemetry — telemetry is the request-path concern only." — Verified by absence in `routes_jobs.py` and `_default_ingest_task`, but the doc presents this as a global invariant. No grep hit for `TelemetryEntry` inside `archon_search/jobs/` or the ingest task, so it holds today; it is an unenforced convention (no test/lint barrier), so calling it a contract is slightly stronger than the code guarantees.
- Principle §1 "Invalid TOML, out-of-range integers, wrong types → `ConfigError` raised by `config.py::load_config`. The process does not start." — True for the `archon-search start` path (`cli/start.py` catches and exits 1). Other entry points (e.g. direct `uv run archon-search`, server import in tests) would propagate the exception according to Python defaults; "the process does not start" depends on which entry point is used and is not enforced centrally.
- "No global FastAPI exception handler that rewrites unknown exceptions into rich JSON." — Confirmed by absence in `server/app.py` (no `add_exception_handler` registration outside middleware). Treat as a present-tense fact, not an invariant.

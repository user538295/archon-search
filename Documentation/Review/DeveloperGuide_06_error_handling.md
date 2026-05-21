# Review: DeveloperGuide/06_error_handling.md

## Summary

The document is largely accurate against `archon_search/server/`. The main factual problems are: (1) Job status strings are upper-case in the wire format, not lower-case as documented; (2) the claim that `ErrorDetail` is registered on every protected route's `responses=` map is false — `/search` and `/route` do not register it; (3) the 422 example body for `/search` is wrong because `/search` uses Pydantic `field_validator`, which produces FastAPI's validation envelope but with a different `type`/`msg` than shown. Smaller issues: the 503 description glosses over the fact that any `pipeline.get_collection_meta` exception (not just a "race") triggers it, and the "401 — empty body" claim is true but the doc does not flag that the middleware can also return a bare 500 if the resolved namespace fails revalidation.

## Inaccuracies (numbered)

1. **Line 17, principle 4** — "returns `200` with `results: []` and `acl_filtered: false`". Verified in `routes_search.py:82-84`: on pipeline exception, the handler builds `SearchResponse(results=[], acl_filtered=False)`. Accurate, but the doc cites debt ticket `CON-5` and roadmap item `A4` which this review cannot verify without reading those files; treat the cross-references as unverified.

2. **Line 27, `202` row** — lists `POST /collections/`, `POST /collections/{name}/reindex`, `POST /ingest`, and `DELETE /jobs/{id} on active jobs`. The first three are verified (`routes_collections.py:114`, `:299`, `routes_jobs.py:91`). `DELETE /jobs/{id}` returns 202 for *active* and for *already-`CANCELLING`* jobs (`routes_jobs.py:147,150`). The table omits the `CANCELLING` case; it appears later in the "Job lifecycle errors" section, but the top-level row is incomplete.

3. **Line 32, `422` row** — "FastAPI body validation (e.g. empty `collection` / `query` in `/search`)". Verified: `routes_search.py` uses Pydantic `field_validator` which surfaces as a 422 via FastAPI. The example body on line 65-69 shows `"msg": "query must not be empty", "type": "value_error"` — the message text matches the validator, but real FastAPI/Pydantic-v2 envelopes use `"type": "value_error"` with a prefixed `msg` like `"Value error, query must not be empty"` and include an `input` field. The doc's example is a simplification, not what the wire actually looks like.

4. **Line 34, `503` row** — "collection meta lookup failed". The actual trigger (`routes_search.py:67-71`) is "any exception from `pipeline.get_collection_meta`", not specifically a race. The body string `"service unavailable"` is verified.

5. **Line 59** — "`schemas.ErrorDetail` is registered on every protected route's `responses=` map, so the OpenAPI schema includes this envelope for `401`, `404`, and `409` documented per route." **False.** `routes_search.py` and `routes_route.py` register no `responses=` map at all (no `ErrorDetail` import, no `responses=` kwarg on either `@router.post`). Only `routes_collections.py` and `routes_jobs.py` register `ErrorDetail` for their error statuses.

6. **Lines 99-107, FAILED job example** — Shape claims `result: null` and a string `error` field. Verified via `schemas.JobResponse` (`schemas.py:70-77`): both fields are `str | None`. However the example value `"status": "failed"` is **wrong**: `JobStatus` is a `str` enum with upper-case values (`types.py:10-16`: `PENDING/RUNNING/DONE/FAILED/CANCELLED/CANCELLING`) and `job_to_dict` serializes via `job.status.value` (`jobs/model.py:15`). The wire value is `"FAILED"`, not `"failed"`.

7. **Lines 112-114, idempotency table** — Same upper-case issue. The doc writes `done / failed / cancelled / pending / running / cancelling`; the wire emits `DONE / FAILED / CANCELLED / PENDING / RUNNING / CANCELLING`. Logic of the table (terminal → 200, active → 202 with eventual `CANCELLED`, already `CANCELLING` → 202) is correct against `routes_jobs.py:136-151`.

8. **Line 109** — File:line citation `routes_jobs.py:81` for `exception("Ingest task %s failed", job_id)` — verified at line 81.

9. **Line 119, ErrorKind set** — Listed as `{empty_query, slot_out_of_range, timeout, internal_error, validation_error, other}`. Verified exactly in `telemetry/entry.py:31-37`.

10. **Line 86, `mcp.py:25` citation for `McpErrorResponse`** — verified at line 25.

11. **Line 84, MCP `not_found` code** — listed only for `get_collection_meta`. Verified at `mcp.py:194` as the only place a non-`internal_error` `code` is emitted across the 9 tools.

12. **Lines 89-91, MCP transport errors** — claims `400 / 406 from FastMCP's streamable HTTP layer`. The wrapping (`mcp.py:249-251`) does `streamable_http_app()` + adds `APIKeyMiddleware`. The 400/406 claim is plausible for an MCP/SSE transport but is **not directly verifiable from this repo's code** — it depends on `fastmcp` internals.

13. **Line 32 / Line 76, "empty `query` in `/search`"** — Pydantic `field_validator` on `query` strips and rejects empty (`routes_search.py:30-36`). Returns 422. Verified.

14. **Middleware 500 case omitted** — `middleware_auth.py:55-59` returns a bare `Response(status_code=500)` if the resolved namespace fails `_validate_namespace`. The doc's 500 row only mentions "Unmapped internal failure"; this middleware path is unmapped and emits no body at all, contradicting the "REST error envelope" section's claim that every status except 401 and 422 carries `{"detail": ...}`.

## Verified claims

- Line 8: REST uses HTTP status + `{"detail": "..."}`; MCP returns HTTP 200 with `{"error", "code"}`. Both verified.
- Line 15: 401 has empty body + `WWW-Authenticate: Bearer`; same response for missing header, wrong scheme, unknown token. Verified `middleware_auth.py:30-53`.
- Line 28, `400` on `/route` for empty query and `slots < 1`. Verified `routes_route.py:75-79`.
- Line 30, `404` for unknown collection / cross-namespace. Verified (`routes_collections.py:180,186,243,251,309,313`; `routes_jobs.py:113-115,133-135`).
- Line 31, `409` POST `/collections/` (path or name registered) and DELETE `/collections/{name}` (pinned-only). Verified `routes_collections.py:130,136,148,199`.
- Line 35, `504` on `/route` with `"routing timed out"` and 30s timeout. Verified `routes_route.py:94-101,135`.
- Job lifecycle section: terminal → 200 with unchanged record, active → 202 (CANCELLING), CANCELLING → 202. Verified `routes_jobs.py:136-151`.
- POST `/ingest` returns 202 even when the path is invalid (no path/precondition validation in the handler). Verified `routes_jobs.py:91-105`.
- Job `error` field is `str(exc)`, i.e. message-only, never a stack trace. Verified `routes_jobs.py:83`.
- MCP `internal_error` returned from every tool's `except Exception` branch. Verified across all 9 tools in `mcp.py`.
- `/health` is exempt from auth. Verified `middleware_auth.py:16,26` and `mcp.py:230-232`.

## Unverifiable / ambiguous

- Cross-references to `Documentation/Architecture/140_error_handling_strategy.md`, `Documentation/Architecture/150_security_and_privacy_architecture.md`, `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`, `Documentation/Backlog/03_world_class_roadmap.md`, `BREAKING.md`, and `02_authentication.md`: not opened in this review per "NEVER trust Documentation/ files".
- Debt ticket IDs `CON-5`, `API-4` and roadmap items `A4`, `C7`: not verifiable from server code alone.
- Line 89-91: exact HTTP statuses (`400` / `406`) emitted by FastMCP's streamable HTTP transport — depends on `fastmcp` library internals, not on code in this repository.
- Line 17 "Build dashboards that alarm on a sustained empty-results window" is advice, not a claim about behavior; not factually checkable.
- Line 43: client-side retry latency suggestions (200ms/500ms/1s) are recommendations, not contract.
- Line 119 claim that telemetry "never [logs] the user's query string or the exception message" — partly verifiable: `TelemetryEntry` has no `query` field (`entry.py:60-74`), and route handlers pass only `error_kind` enums to `from_error` (`routes_route.py:127-160`); full coverage check across all writer call sites was not performed.

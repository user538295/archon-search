# Feature Brief: Fix Non-Standard 503 Error Shape on Collection Create

## Problem
When creating a collection fails because the store is busy, the server returns a response that looks different from every other error in the API — meaning any code that parses error messages consistently will silently miss the reason it failed.

## Goal
`POST /collections/` 503 responses use the same `{"detail": "..."}` shape as every other endpoint. The 503 also appears in the OpenAPI spec so generated clients know to expect it.

## Users & Context
Developers integrating with the API — including anyone using a generated client or writing custom error handling. They hit this when submitting a collection create while a reindex is running.

## Core Flow
1. Developer calls `POST /collections/` while a reindex is active.
2. Server returns 503 with `{"detail": "store busy; retry after Retry-After seconds"}` and a `Retry-After` header.
3. Developer's generic error handler reads `detail` — same as every other error — and surfaces it or retries.

## In Scope
- Normalize the 503 response body in `routes_collections.py` to `{"detail": "..."}` (remove the `"error"` key)
- Add `503: {"model": ErrorDetail}` to the `responses` dict in the route decorator at line 134

## Out of Scope
- Changing the `Retry-After` header behavior — it works correctly today
- Auditing other endpoints for similar shape issues — separate effort

## Key Decisions
- **Remove `"error": "store_busy"` key**: No other endpoint uses a machine-readable error code field; don't introduce one here. If a code is needed later, add it to `ErrorDetail` schema-wide.

## Edge Cases & Constraints
- The `Retry-After` header must be preserved — clients use it to know when to retry.
- `BREAKING.md` does not need an entry: the old shape was undocumented (missing from OpenAPI) so no client could reliably depend on `"error": "store_busy"`.

## Open Questions

_Resolved 2026-07-20._

- **Should the detail message include the actual retry delay in seconds?** Yes. `retry_after = str(math.ceil(e.timeout_s))` is already computed at `routes_collections.py:214` — zero extra code to embed it. Use `f"store busy; retry in {retry_after} seconds"`. The `Retry-After` header remains the authoritative signal for programmatic clients; the body message is for humans.

## Future Iterations
- A broader audit for any other endpoints returning non-`ErrorDetail` shapes.

## References
- [[archon_search/server/routes_collections.py:190–198]] `[code-agent]` — the 503 handler with the non-standard body
- [[archon_search/server/schemas.py]] `[code-agent]` — `ErrorDetail` schema definition

## Recommendation
One-line fix — replace `{"error": "store_busy", "detail": "..."}` with `{"detail": "store busy; retry after Retry-After seconds"}` and add the 503 response model to the decorator. Low risk, high consistency gain. Do it as a standalone commit — it's too small to bundle.

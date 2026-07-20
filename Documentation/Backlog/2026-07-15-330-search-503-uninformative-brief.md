# Feature Brief: Search 503 Error Gives No Actionable Information

## Problem
When a search fails because the metadata store is unreachable, the server returns "service unavailable" — with no indication of what failed, whether it's worth retrying, or what to do next.

## Goal
The 503 response tells the caller which service is unavailable and whether retrying makes sense, so they can react appropriately instead of guessing.

## Users & Context
API clients and CLI users who run a search and get a failure back. Currently they see the same "service unavailable" message whether the embedder is down, the metadata store failed, or something else entirely — they can't distinguish transient from persistent failures.

## Core Flow
1. Client submits a search request.
2. The metadata store lookup fails.
3. Server returns 503 with `{"detail": "service unavailable: metadata store could not be reached", "code": "metadata_store_error"}`.
4. Client reads the `code` field to decide whether to retry or escalate.

## In Scope
- Add a `code` field to 503 responses from search endpoints distinguishing error causes (e.g. `"metadata_store_error"`, `"embedder_unavailable"`)
- Improve the `detail` message to name the failing component
- Cover both `POST /search` and `POST /search/many`

## Out of Scope
- Normalizing 503 shapes across all endpoints (a broader cleanup; park for a separate pass)
- Adding retry-after headers (no retry budget logic exists today)

## Key Decisions
- **Add `code` field rather than just improving the message**: A machine-readable code lets clients branch without parsing strings — future-proofs integrations against message wording changes.

## Edge Cases & Constraints
- The `ErrorDetail` schema in `schemas.py` may need a `code: str | None` field added — ensure OpenAPI reflects this so clients can generate correct types.
- Both the single-collection and multi-collection search paths (`routes_search.py:230` and `:275`) must be updated consistently.

## Open Questions

_Resolved 2026-07-20._

- **`ErrorDetail` vs `SearchErrorDetail`?** Add `code: str | None = None` to the shared `ErrorDetail` model (`schemas.py:490`). A subclass splits the schema for no gain and makes client error handling inconsistent. The OpenAPI snapshot regen is mechanical. All `ErrorDetail` sites leave `code=None` unless they explicitly set it — fully additive.
- **Other `MetadataLookupError` sites?** Verified — three more files catch it and return 503: `routes_openai_shim.py:227`, `routes_explain.py:535`, `mcp.py:442` and `mcp.py:929`. Fix all four sites in one PR (consistent API; the changes are structural copies).

## Future Iterations
- Extend the `code` field pattern to other 503-producing endpoints (store busy at `routes_collections.py:194`)
- Add `Retry-After` header for transient errors once retry logic is defined

## References
- [[archon_search/server/routes_search.py]] `[code-agent]` — lines 230 and 275, `MetadataLookupError` → 503
- [[archon_search/server/schemas.py]] `[code-agent]` — `ErrorDetail` schema definition
- [[Documentation/Backlog/bug-024-collection-chunk-count-zero-brief.md]] `[related]` — separate routes 503 shape inconsistency (bug-027); consider coordinating

## Recommendation
Small, high-value fix. Two lines of code change the error from useless to actionable. The `code` field addition is the right call — string parsing is fragile for clients. Do this alongside the `routes_collections.py` 503 shape fix (bug-027) since both touch error response schemas in the same PR.

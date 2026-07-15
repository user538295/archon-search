# Feature Brief: OpenAI Shim Empty Query Validation

## Problem
When a caller sends `POST /v1/chat/completions` with no `role="user"` message (or a blank one), the shim silently passes an empty string to the search pipeline — returning garbage results or an opaque internal error instead of a clear rejection.

## Goal
The shim returns a well-formed 400 error in OpenAI error shape whenever the extracted query is empty or whitespace-only, before any retrieval work begins.

## Users & Context
Any client using the OpenAI-compatible endpoint — an SDK, a LangChain integration, a custom script. These clients parse errors expecting `{"error": {"message": ..., "type": ..., "code": ...}}`. A FastAPI `{"detail": ...}` response silently breaks their error handling.

## Core Flow
1. Caller sends `POST /v1/chat/completions` with a messages array.
2. Shim extracts the last `role="user"` message as the query.
3. **New:** if `query.strip()` is empty, return `400` with OpenAI error body — stop here.
4. Otherwise proceed to retrieval as today.

## In Scope
- Validate extracted query is non-empty (`query.strip()`) before retrieval.
- Return 400 in OpenAI error shape: `{"error": {"message": "No user message provided", "type": "invalid_request_error", "code": "no_user_message"}}`.
- Cover both cases: no `role="user"` message in the array, and a blank/whitespace user message.

## Out of Scope
- Query length limits — not requested, not a current complaint.
- Validation of other fields (model, temperature, etc.) — separate concern.

## Key Decisions
- **OpenAI error shape, not FastAPI `{"detail": ...}`:** OpenAI client libraries parse the `error.code` and `error.type` fields; a FastAPI-shaped 400 breaks them silently. The shim's contract is OpenAI compatibility, so the error must match.

## Edge Cases & Constraints
- `messages` array contains only `role="system"` entries: treated as no user message → 400.
- User message is all whitespace: `"   "` → treated as empty → 400.
- `OpenAI401Middleware` already rewrites bodyless 401s on `/v1/*`; the new 400 is body-bearing and unaffected.

## Open Questions
- Does `routes_openai_shim.py` use a helper to extract the user message, or inline logic? If it's inline, the guard is one `if` block. If a helper exists, the guard goes there — check before implementing.
- Should the `code` value be `"no_user_message"` or match a real OpenAI code? OpenAI uses `"invalid_request_error"` as the type and custom codes — confirm what OpenAI client SDKs actually key on.

## Future Iterations
- Query length cap (e.g. reject > 10 000 characters) — not needed now.
- Structured validation of the full request body via Pydantic — already partially done via `schemas_openai.py`; extend there if scope grows.

## References
- [[archon_search/server/routes_openai_shim.py]] `[code-agent]` — handler under fix
- [[archon_search/server/schemas_openai.py]] `[code-agent]` — request/response models for the shim
- [[archon_search/server/app.py]] `[code-agent]` — `OpenAI401Middleware` placement (LIFO ordering)
- [[Documentation/Backlog/bug-027-503-response-shape-inconsistency-brief.md]] `[related]` — companion brief on response shape consistency

## Recommendation
Two-line fix: extract the query, strip it, return 400 in OpenAI error shape if empty. The only subtlety is the error body shape — using FastAPI's `HTTPException` would produce the wrong format, so raise directly or return a `JSONResponse`. Highest priority of the shim bugs: an empty query silently produces misleading results, which is worse than a visible error.

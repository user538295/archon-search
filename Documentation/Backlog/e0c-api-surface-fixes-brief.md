# Feature Brief: E0c — API Surface Fixes

## Problem
Four API-level constraints frustrate users and operators in ways that are invisible until they hit them: `list_documents` silently truncates at 1 000 with no cursor, making large collections unpageable; `max_fanout` in TOML is ignored by API validation so raising it does nothing; `top_k` is capped at 100 with no operator override; and auto-generated collection descriptions are biased toward the first 20 documents ingested, producing misleading routing metadata for large heterogeneous collections.

## Goal
API limits are either configurable by the operator or replaced with correct defaults. A user who pages through a 10 000-document collection, raises `max_fanout` in TOML, or asks for 200 results for a batch evaluation use-case gets the expected behaviour without a code change.

## Users & Context
- **Operators** who raise `max_fanout` in `archon-search.toml` and are silently ignored.
- **Developers** building evaluation pipelines who need `top_k > 100` to retrieve full candidate sets.
- **End users** with large collections (5 000+ documents) who need to list or audit all indexed content.
- **Anyone** whose collection has a misleading auto-description because the first 20 chunks were from a single document.

## Core Flow

### list_documents cursor pagination (L4)
1. Client calls `GET /collections/{name}/documents?limit=50`.
2. Response: `{ items: [...], next_cursor: "<doc_id>", total: 4200 }`.
3. Client calls `GET /collections/{name}/documents?limit=50&cursor=<doc_id>`.
4. Response: next page. Repeat until `next_cursor` is null.

### max_fanout tracks TOML config (L9)
1. Operator sets `max_fanout = 12` in `archon-search.toml`.
2. Server starts; Pydantic validator on `SearchRequest.collections` reads `app.state.config.max_fanout` (12) instead of the hardcoded `_FANOUT_VALIDATION_LIMIT = 8`.
3. `POST /search` with 12 collections succeeds.
4. `GET /status` includes `search.max_fanout: 12` so operators can verify the live value.

### top_k operator cap (L13)
1. Operator sets `top_k_max: 500` in `[search]` section of `archon-search.toml` (new config field; default 100 to preserve current behaviour).
2. `POST /search` with `top_k: 200` succeeds.
3. Request with `top_k > top_k_max` returns HTTP 422 with a clear message: "top_k 200 exceeds operator-configured maximum of 100."

### Collection description quality (L12)
1. Description generator samples `_MAX_SAMPLE_CHUNKS = 100` chunks using `ORDER BY RANDOM()` instead of insertion-order scan.
2. `random.sample(chunks, min(20, len(chunks)))` draws from the randomised 100, not the first 20 by insertion order.
3. Re-generating a description on a large heterogeneous collection produces a representative result regardless of ingest order.

## In Scope
- **L4 — list_documents pagination**: Add `GET /collections/{name}/documents` REST endpoint (currently MCP-only) with `limit` (1–200, default 50) and `cursor` (opaque string, `doc_id`-based) query params. Response model: `DocumentListResponse { items: list[DocumentInfoSchema], next_cursor: str | null, total: int }` — mirrors `JobListResponse` exactly. Update `store.list_documents()` to accept and honour `cursor`. Update `pipeline.list_documents()` to pass through. Update MCP `list_documents` tool to support cursor (additive, backward-compatible).
- **L9 — max_fanout API validation**: Remove `_FANOUT_VALIDATION_LIMIT` constant from `routes_search.py`. Replace the hardcoded Pydantic validator with one that reads `max_fanout` from `app.state.config` (injected at startup). Same fix in `mcp.py` where `_FANOUT_VALIDATION_LIMIT` is also referenced. Add `search.max_fanout` to `GET /status` response.
- **L13 — top_k operator cap**: Add `top_k_max: int = 100` to `SearchConfig` in `config.py`. Wire into the `top_k` Pydantic validator on `SearchRequest` (replaces the hardcoded `le=100`). Update `GET /status` to expose `search.top_k_max`.
- **L12 — description sampling**: Change `store.sample_chunk_texts()` (or its equivalent) to use `ORDER BY RANDOM()` when LanceDB supports it; fall back to shuffling the in-memory result if not. Raise `_MAX_SAMPLE_CHUNKS` from 20 to 100 so the random draw has a wider pool.

## Out of Scope
- Adding filters or sort fields to `list_documents` — cursor pagination on the existing schema is the complete scope.
- Multi-collection `list_documents` — single-collection only, consistent with current behaviour.
- Changing `top_k_retrieve` (pre-rerank pool) — only the API-level `top_k` cap is touched.
- `ORDER BY RANDOM()` performance on large collections — this is a description-generation path, not the hot search path; acceptable overhead.

## Key Decisions
- **`list_documents` REST endpoint, not just MCP fix**: The current MCP-only listing is a gap in the REST API surface. `GET /collections/{name}/documents` is the natural REST resource for this. The MCP tool is updated in the same change to support cursor.
- **`doc_id` as cursor key**: Matches the jobs pattern (`job_id` as cursor) and is a stable, opaque identifier. Avoids offset pagination which breaks under concurrent ingest.
- **`top_k_max` in config, not hardcoded**: Operators running batch evaluation pipelines legitimately need `top_k > 100`. The cap should be an operator decision, not a code constant.
- **`_FANOUT_VALIDATION_LIMIT` removed entirely**: A constant that duplicates a config value and silently overrides it is strictly worse than just reading the config value. No "default fallback" — if config is unavailable at startup, the validator should fail loudly.

## Edge Cases & Constraints
- **`cursor` references a deleted document**: If the `doc_id` used as cursor no longer exists (document deleted between pages), the cursor lookup returns `cursor_index = None`. Behaviour: restart from the beginning of the remainder, same as `routes_jobs.py` pattern. Document this in API spec.
- **`store.list_documents()` fetches `limit * 50` rows for chunk aggregation**: With cursor support, the store layer must position after the cursor doc before trimming. Since documents are aggregated in-memory, the cursor must filter the aggregated result set, not the raw chunk rows. The current `limit * 50` fetch strategy holds — cursor logic operates post-aggregation.
- **`max_fanout` config injection into Pydantic validator**: Pydantic model validators run before the request hits the route handler; `app.state` is not available in model scope. The validator must be a field validator that reads from a request-scoped dependency, or the validation moves to the route handler body. Planning must decide the injection pattern.
- **`top_k_max` default = 100**: Existing integrations that rely on `le=100` being the OpenAPI schema constraint will see the schema change to `le=top_k_max` (dynamic). Record in `BREAKING.md` if the schema is consumed by generated clients.
- **LanceDB `ORDER BY RANDOM()` support**: Verify LanceDB version supports random ordering before implementing; fall back to in-process shuffle if not. The D4 streaming work adds `sample_chunk_texts()` as a helper — coordinate with that change.

## Open Questions
- **Pydantic validator injection for `max_fanout`**: How to get `app.state.config.max_fanout` into a Pydantic field validator without breaking the request model's independence from FastAPI. Options: (a) move validation to route handler body; (b) use a custom `Annotated` type with a factory; (c) keep the constant but add a startup assertion that `_FANOUT_VALIDATION_LIMIT == config.max_fanout`. Planning must decide.

## Future Iterations
- Filter and sort on `list_documents` (by `indexed_at`, `file_type`, `source_path_prefix`).
- Multi-collection document listing with per-collection counts.
- `GET /status` summary of total indexed documents across all collections.

## Recommendation
Straightforward API hygiene. The `max_fanout` fix is the highest-priority item here — an operator who reads the docs, sets `max_fanout = 12` in TOML, and gets a 400 error has lost trust in the config system entirely. Do that one first. The Pydantic injection question in Open Questions is the only design decision that needs resolving before the plan can be written.

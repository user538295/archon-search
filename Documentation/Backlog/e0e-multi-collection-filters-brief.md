# Feature Brief: E0e — Multi-Collection Filter Support

## Problem
`POST /search` rejects any request that combines a `collections` list with a `filters` field, returning HTTP 400: "filters are not supported for multi-collection search in v1." Users who rely on `source_path_prefix`, `language`, or `file_type` filters to scope their results cannot use multi-collection search at all. For anyone with more than one collection (the typical case), filters are effectively unavailable on the primary search endpoint.

## Goal
Filters work in multi-collection search. A request with both `collections: ["docs", "code"]` and `filters: { file_type: ".md" }` applies the filter to each collection leg independently and returns merged, reranked results scoped to the filter.

## Users & Context
- **Users with multiple collections** (the majority of archon-search users) who expect filter + multi-collection to compose naturally — the API implies they should.
- **MCP tool users** calling `search` or `search_with_context` who provide both a collection list and a language or file-type filter.
- **Operators** building search UIs who need to scope results by source path even when searching across collections.

## Core Flow
1. Client sends `POST /search` with `collections: ["research", "notes"]` and `filters: { language: "en", file_type: ".md" }`.
2. Server validates request (no longer rejects filter + collections combination).
3. Router selects the collections to query (respecting `max_fanout`).
4. For each collection leg, `SearchPipeline.search_many()` passes the `filters` object to `store.hybrid_search()` alongside the query vector — same filter application as single-collection search.
5. Per-leg results (already filtered) are merged via global RRF and reranked.
6. Response returns filtered results with `acl_filtered`, `excluded_collections`, and `applied_filters` fields populated.

## In Scope
- **Remove the v1 restriction**: Delete the `HTTPException` block at `routes_search.py:89` that rejects filters on multi-collection requests.
- **Per-leg filter injection in `search_many()`**: `SearchPipeline.search_many()` already calls `store.hybrid_search()` per leg; pass the `SearchFilters` object through to each leg call. The filter application logic in `store.py` is already collection-scoped — no change needed there.
- **MCP tools**: `search`, `search_with_context`, and `explain` in `mcp.py` have the same restriction (`mcp.py:302–304`, `mcp.py:664–666`); remove both.
- **`applied_filters` in `SearchResponse`**: Add `applied_filters: SearchFiltersSchema | null` to `SearchResponse` so clients can confirm which filters were applied.
- **Eval harness coverage**: Add at least one eval fixture that exercises filter + multi-collection search to gate regressions.
- **Update `Documentation/UserManual/`** to remove the "filters not supported for multi-collection search" note and document the composed behaviour.

## Out of Scope
- Per-collection filter overrides (different filter per leg) — single shared `filters` object applied to all legs is the complete scope.
- `ACL` filter changes — ACL is applied per-collection at the store layer independently of `SearchFilters`; no change needed.
- New filter types (date range, metadata key/value) — existing filter types only.
- Filter validation per-collection (e.g. rejecting `language` filter on collections without language tags) — the existing per-collection language warning in `GET /status` is sufficient; no new validation.

## Key Decisions
- **Shared filter object across all legs**: Applying the same filter to all legs is the correct semantic for the common case (e.g. "search docs and notes collections, English only"). Per-leg filter overrides are a future iteration.
- **No schema change to `SearchFilters`**: The filter schema is already correct. The only change is lifting the restriction that blocked its use with `collections`.
- **`applied_filters` on response**: Clients currently have no way to confirm whether filters were applied in multi-collection mode (they were being rejected). Adding `applied_filters` closes the feedback loop and mirrors what `POST /explain` already surfaces.
- **Remove restriction from MCP simultaneously**: The MCP restriction is a parity bug; fixing REST without MCP leaves the restriction on the more common AI-agent path.

## Edge Cases & Constraints
- **Filter produces zero results from one leg**: Expected behaviour — the leg returns an empty list, which contributes nothing to the RRF merge. `excluded_collections` is populated only for legs that error, not for legs that return empty results. Document this distinction.
- **`language` filter on a collection with no language tags**: Already handled by the existing single-collection path — returns a warning in `GET /status` for the collection. Behaviour is identical in multi-collection mode; no new handling needed.
- **`source_path_prefix` filter across collections with different root paths**: The filter is a string prefix match against `source_path`; it may return zero results for collections whose documents live under a different root. This is correct behaviour — the user specified a prefix. No special handling.
- **`fanout_leg_trim` interaction with filters**: The per-leg trim (`fanout_leg_trim = 40`) is applied after filtering. A strict filter on a large collection may return fewer candidates than `fanout_leg_trim` — this is correct and already handled by the existing trim logic.
- **BREAKING.md**: Removing the 400 restriction is technically a relaxation (previously-rejected requests now succeed). Not a breaking change for existing clients, but worth noting in `BREAKING.md` as a behaviour change.

## Open Questions
- None. The implementation is mechanical: remove the restriction, pass filters through the existing per-leg call chain, add `applied_filters` to the response. The existing filter infrastructure handles the rest.

## Future Iterations
- Per-leg filter overrides: `collections: [{ name: "docs", filters: {...} }, { name: "code", filters: {...} }]`.
- Filter validation warnings per-collection (e.g. "collection X has no language tags — language filter had no effect").
- Cross-collection filter suggestions (e.g. "3 of 5 collections have no English-tagged documents — did you mean to filter by language?").

## Recommendation
This is a one-session change. The restriction was a v1 placeholder; the underlying infrastructure already supports per-collection filtering. The only reason it hasn't been removed is that it was labelled "v1" and never revisited. Remove it, thread filters through `search_many()`, add `applied_filters` to the response, and delete the note from the docs. The eval fixture is the most important part — it ensures this doesn't regress silently.

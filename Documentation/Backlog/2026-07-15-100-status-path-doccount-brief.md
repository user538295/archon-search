# Feature Brief: Status Endpoint Returns Empty Path and Zero Document Count

## Problem
`GET /status` always shows an empty path and zero documents for every collection — making it useless for a quick health check — even though both pieces of information are available from the server's own config and store.

## Goal
`GET /status` shows the real storage path and actual document count for each collection, so operators can see at a glance where their data lives and how much of it there is.

## Users & Context
Operators and developers checking system health. They run `archon-search status` (or hit `/status`) after ingesting documents or setting up a new collection, expecting a truthful summary — not placeholder zeros.

## Core Flow
1. Operator runs `archon-search status` or calls `GET /status`.
2. Each collection entry shows its real storage path (e.g. `~/.archon-search/collections/my-docs`) and actual document count (e.g. `142 documents`).
3. No other behavior changes.

## In Scope
- Populate `path` from `_all_collection_paths(config)` — already computed in the same handler context.
- Populate `doc_count` from `meta.doc_count` — already loaded when iterating collections.
- Update `StatusCollectionEntry` response in `routes_status.py:118–120`.

## Out of Scope
- `chunk_count` and `description` gaps in other endpoints — tracked in bug-024 and bug-025.
- Real-time recomputation of document counts (use the cached `meta.doc_count`; staleness is acceptable here).

## Key Decisions
- **Read from meta, not recount live:** `meta.doc_count` is fast and consistent with what other endpoints return. A live recount would add latency to every status call for no material benefit.
- **Bundle with bug-024 and bug-025:** All three bugs are "fields that exist in meta/store but are never read in the response constructor." One PR, one review.

## Edge Cases & Constraints
- Collection with no documents yet: `doc_count = 0` is correct and expected — no special handling needed.
- Path not found in `_all_collection_paths`: fall back to `""` with a log warning rather than failing the whole status call.

## Open Questions
- Does `_all_collection_paths` cover all namespaces, or only the default? Confirm before wiring — if namespace-scoped, the handler may need to call it per namespace.
- Should `path` be the data-dir-relative path or the absolute path? Check what operators expect from the existing `collection info` output for consistency.

## Future Iterations
- Surface per-collection chunk counts (requires a live store call; deferred to avoid latency on status).
- Add a `last_ingest_at` timestamp once the store tracks it.

## References
- [[archon_search/server/routes_status.py:118–120]] `[code-agent]` — hardcoded `path=""`, `doc_count=0` with `"path not yet populated from store"` comment
- [[Documentation/Backlog/bug-024-collection-chunk-count-brief.md]] `[user]` — sibling bug: `chunk_count=0` in collection list/info/status
- [[Documentation/Backlog/bug-025-collection-description-brief.md]] `[user]` — sibling bug: `description=""` in collection list/info

## Recommendation
Fix this in the same PR as bug-024 and bug-025 — all three touch `StatusCollectionEntry` and the collection route constructors in `routes_collections.py`. The fix is two property reads; the main risk is the namespace-scoping question in Open Questions, which must be answered before merging.

# Feature Brief: Collection Responses Always Show Zero Chunks

## Problem
Every collection in the API always reports `chunk_count: 0` — even after ingesting thousands of documents. Any tool or dashboard reading that field gets useless data.

## Goal
Collection list, info, patch, and status responses all return the real chunk count for each collection.

## Users & Context
Operators checking collection health via the REST API, a dashboard, or the CLI (`collection info`) — they want to know how full a collection is. Right now the field is permanently broken.

## Core Flow
1. User calls `GET /collections`, `GET /collections/{name}`, `PATCH /collections/{name}`, or `GET /status`.
2. Response includes the actual number of indexed chunks for each collection.
3. No user-visible change to the API shape — just correct data where zero was.

## In Scope
- Fix `chunk_count` in `list_collections`, `get_collection_info`, `patch_collection` (`routes_collections.py:112, 377, 613`)
- Fix `chunk_count` in `GET /status` (`routes_status.py:120`)

## Out of Scope
- `doc_count` and `path` fields in `/status` (separate bug-025)
- Real-time live counts during active ingest — a snapshot at request time is sufficient

## Key Decisions
- **Read from store, not meta cache**: `meta.chunk_count` is not reliably kept current; call `await search_store.count_chunks(name, namespace=ns)` at response time. The count call already exists in the codebase (`routes_collections.py:555`) — reuse it.
- **No new endpoint**: fix the existing four handlers; the API shape is unchanged.

## Edge Cases & Constraints
- Empty collection: `count_chunks` must return `0`, not raise — verify this is already guaranteed by `SearchStore`.
- `list_collections` iterates all collections: N count queries per request. Acceptable for typical collection counts; cache if profiling shows it matters later.
- Namespace isolation: `count_chunks` must receive the correct `ns` for each collection to avoid cross-namespace leakage.

## Open Questions
- Does `meta.chunk_count` get updated on ingest? If yes, reading `meta.chunk_count` would be cheaper than a live count query and should be preferred — verify in `pipeline.py` ingest path.
- Is `count_chunks` O(1) in LanceDB (metadata read) or O(n) (full scan)? This affects whether a live call in `list_collections` is safe at scale.

## Future Iterations
- Stream live chunk counts via a WebSocket or SSE endpoint for dashboards that need real-time ingestion progress

## References
- [[archon_search/server/routes_collections.py]] `[code-agent]` — list, info, patch handlers with hardcoded zeros
- [[archon_search/server/routes_status.py]] `[code-agent]` — status handler with hardcoded zero and `"path not yet populated from store"` TODO comment

## Recommendation
Fix this now — it is a silent data correctness bug that undermines every operator tool and monitoring script reading collection stats. The fix is mechanical: replace four `chunk_count=0` literals with a `count_chunks()` call. The main risk is N+1 queries in `list_collections`; verify `count_chunks` is cheap before shipping, and add a note to the PR if a future cache is needed.

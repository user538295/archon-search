# Feature Brief: E2d — Graph Lifecycle Hygiene

## Problem

Graph tables grow monotonically and never shrink: deleting a document leaves ghost nodes, edges, and mentions behind; TTL-expired chunks leave orphan mentions; two namespaces with identically-named collections silently share graph tables. Inspection results surface stale entities, and multi-namespace isolation is broken at the graph layer.

## Goal

After this feature ships: deleting a document immediately removes its mention rows; the maintenance GC pass drops orphan nodes/edges and prunes mentions for expired chunks; communities auto-rebuild asynchronously at configurable CPU priority after GC; two namespaces with the same collection name are fully isolated in separate graph tables; and `GET /status` surfaces `stale_mention_count` and `last_graph_gc_at` so operators can observe graph freshness.

## Users & Context

Server operators managing a growing corpus who use graph modes (`naive`, `local`, `global`) for retrieval. They delete documents routinely — via REST, MCP, sync, or maintenance orphan cleanup — and expect the graph to reflect the live corpus. Multi-namespace deployments are the blocking use case for the table rename.

## Core Flow

1. Operator deletes a document (REST `DELETE /collections/{name}/documents/{doc_id}`, MCP `delete_document`, sync, or maintenance orphan cleanup).
2. `pipeline.delete_document` immediately deletes all mention rows for that `doc_id` from the graph mentions table (`_archon_graph_{ns}__{col}_mentions`).
3. On the next maintenance pass (when `graph_gc = true`), the GC policy:
   - Scans for entity IDs in the nodes table that have zero remaining mention rows → deletes those nodes and any edges referencing them.
   - Scans the mentions table for rows referencing chunk IDs that no longer exist or whose `expires_at` has passed → prunes those rows.
4. If any nodes were removed from a collection, the communities table for that collection is cleared (`communities_invalidated: true` surfaced in `GET /status` `GraphCollectionStats`).
5. An async job is enqueued to rebuild communities for each invalidated collection, running at the configured CPU priority (default: low). Once complete, `communities_invalidated` flips back to `false`.
6. After each GC pass, `stale_mention_count` (total orphan mentions found) and `last_graph_gc_at` are written to `.maintenance-state.json` and surfaced in `GET /status`.

## In Scope

- Wire `graph_store.delete_mentions_by_doc(collection, doc_id)` into `pipeline.delete_document` (and by extension `store.delete_by_source_path`, which calls it).
- New `GraphStore` methods: `delete_orphan_nodes_and_edges(collection, namespace)` and `prune_stale_mentions(collection, namespace)` for the GC pass.
- New maintenance policy `[maintenance] graph_gc = true` (default `true` when graph enabled): runs orphan node/edge cleanup and stale mention pruning per collection per pass.
- Community invalidation: clear communities table for collections where GC removed nodes; set `communities_invalidated: bool` per collection in `GraphCollectionStats`.
- Async community rebuild job triggered by GC: uses existing job infrastructure; runs at OS-level reduced scheduling priority; configurable via `[graph] gc_rebuild_communities = true` (default `true`) and `[graph] gc_rebuild_cpu_priority = "low"` (values: `"low"`, `"normal"`, `"high"`).
- `GET /status` graph sub-object gains `stale_mention_count: int` and `last_graph_gc_at: str | null` (written to `.maintenance-state.json` after each GC pass).
- `GraphCollectionStats` gains `communities_invalidated: bool`.
- Namespace-scoped graph table names: rename all four table name helpers in `GraphStore` from `_archon_graph_{col}_*` to `_archon_graph_{ns}__{col}_*`; thread `namespace` through all `GraphStore` method signatures (or constructor — see Open Questions); no data migration needed (no existing user data).
- `POST /maintenance/trigger` already exists — no new CLI command needed for on-demand GC.
- `graph build-communities` CLI remains the manual rebuild path; auto-rebuild job is additive.

## Out of Scope

- Per-collection GC exclusion patterns — operators can use the existing `[maintenance] exclude` patterns to skip collections.
- Incremental community update (partial Leiden re-run) — full rebuild only; incremental is a future optimization.
- Stale centroid statistics from TTL pruning — a pre-existing gap documented separately; not introduced by this feature.
- Multi-namespace graph cross-collection queries — the `GET /graph/cross-collection` endpoint already resolves namespace via `request.state.namespace`; table rename makes it correct end-to-end without further route changes.

## Key Decisions

- **Namespace rename in scope**: No existing user data means no migration path is required — just update naming conventions and thread namespace through `GraphStore`. Deferring would force a future migration.
- **Hybrid GC timing**: Mention deletion is cheap and immediate (severs the deleted doc's graph footprint right away); orphan node/edge detection is expensive (cross-table scan) and belongs in the maintenance pass where it can be batched across many deletes.
- **Invalidate + async rebuild, not silent staleness**: Clearing communities on GC prevents `local`/`global` search from returning ghost-entity results. Auto-rebuild at low CPU priority restores functionality without operator action, but doesn't block the GC pass or slow deletion.
- **`stale_mention_count` cached from GC run**: Computing it live on every `GET /status` call would require a cross-table join on every status poll — prohibitive at scale. Cached value paired with `last_graph_gc_at` gives operators enough signal.

## Edge Cases & Constraints

- **Shared entities across documents**: Nodes are keyed by stable entity ID (hash of `entity_type + entity_name`). If entity "Python" appears in 10 documents and 9 are deleted, the node survives with 1 remaining mention. GC must count remaining mentions per entity ID, not blindly delete on first doc removal.
- **Re-ingest**: `delete_mentions_by_doc` is already called at re-ingest time (E2b). With this feature it will also run at delete time — idempotent, no change needed.
- **GC with graph disabled**: If `[graph] enabled = false` at maintenance time, `graph_gc` policy must skip silently (no graph tables to scan).
- **Community rebuild race**: If a second GC pass fires while the async rebuild job is still running, the rebuild job must be idempotent (full-replace write via `write_communities` already is). The `communities_invalidated` flag stays `true` until the job completes.
- **Namespace separator collision**: Double underscore `{ns}__{col}` is the separator. Namespace and collection names are validated against `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`. A namespace ending in `_` combined with a collection starting with `_` would produce `foo___bar` — technically unambiguous but visually odd. Validation should reject names ending or starting with `_` (check if this is already enforced; if not, add a guard).
- **CPU priority implementation**: Python's `asyncio.to_thread` runs in a thread pool. OS-level `nice()` can be applied inside the thread before starting Leiden. Priority mapping: `low → nice(10)`, `normal → nice(0)`, `high → nice(-5)` (requires privileges on some systems — degrade gracefully to `nice(0)` if `os.nice()` raises `PermissionError`).

## Open Questions

- **`GraphStore` namespace threading**: Should `namespace` be added to the constructor (cleaner if each pipeline instance owns one namespace) or to each method signature (more explicit, matches existing `store.py` pattern)? The pipeline already has `DEFAULT_NAMESPACE` as a per-call parameter — method-level threading likely fits better.
- **GC scan ceiling**: The mentions table could be large. Should `prune_stale_mentions` apply a scan ceiling (like `_MENTIONS_SCAN_CEILING` already documented with a `ponytail:` comment in `graph_inspector.py`) or process the full table per pass? Need to measure expected mentions-table size in practice.
- **Async job infrastructure for rebuild**: Does the existing `jobs/` async job store (used for bulk ingest/export) fit community rebuild, or is a lighter async task (fire-and-forget `asyncio.create_task`) more appropriate? The rebuild has no user-facing result to poll.
- **`communities_invalidated` persistence**: Should `communities_invalidated` be stored in `.maintenance-state.json` (survives restarts) or derived live from the communities table being empty? Live derivation is simpler and always accurate.

## Future Iterations

- Incremental community update: re-run Leiden only on the subgraph affected by removed nodes rather than full rebuild.
- `stale_mention_count` as a live point-in-time query (with a scan ceiling) once LanceDB adds cross-table join support.
- Per-collection GC scheduling (different intervals per collection).
- Namespace rename migration path for deployments that accumulated data before E2d shipped.

## Recommendation

This is the right feature to build now — the graph is write-only and growing unboundedly, which is a correctness problem, not a polish item. The hardest part is the namespace threading: `GraphStore` has no `namespace` parameter today and adding it touches every call site in `pipeline.py`, `routes_graph.py`, `mcp.py`, and `maintenance_loop.py`. Get that threading decision locked in planning before touching anything else — it determines the shape of every other change. The community auto-rebuild job must fail gracefully (low CPU priority may not be grantable on all systems; the rebuild must not block or crash GC). Do not compromise on the hybrid GC timing decision: inline mention deletion is the correctness guarantee; the orphan scan staying in maintenance is the performance guarantee. Both are load-bearing.

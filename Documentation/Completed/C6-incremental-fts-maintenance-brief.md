# Feature Brief: C6 — Incremental FTS Maintenance

## Problem
Every ingest operation — even a single-file update — triggers a full FTS index rebuild over the entire collection, making ingest latency O(collection-size) instead of O(delta-size).

## Goal
Ingest latency scales with the number of changed chunks, not the total collection size. A single-document update into a 50,000-chunk collection takes milliseconds, not seconds.

## Acceptance Criteria
Correctness (all must hold after C6 ships):
- **After incremental add**: searching for text that exists only in newly added chunks returns those chunks.
- **After incremental delete**: searching for text that existed only in deleted chunks returns zero results — no phantom hits from stale index entries.
- **After re-ingest (delete + add)**: search returns only the new content; old content from the same document does not appear.
- **After N incremental operations**: after a sequence of 50 add, delete, and re-ingest operations on a 1,000-chunk collection, a set of at least 10 representative queries returns the same document ID sets in the same rank order as a fresh `rebuild_fts_index()` on the final collection state. Note: BM25 scores may differ numerically between index structures; equivalence is defined as result-set identity (same doc_ids, same order), not score equality.

Latency (regression guard):
- `ingest_file` p95 wall-clock time on a corpus of ≥1,000 chunks does not regress vs. the pre-C6 baseline.
- The corpus must be large enough to distinguish O(collection-size) from O(delta-size) behavior; the current 63-document eval corpus is too small for this purpose.
- Per `thresholds.toml` line 7, latency thresholds are currently report-only (not a hard CI gate); C6 should decide whether to promote this to a gate.

## Users & Context
Operators running archon-search against large, frequently-updated corpora (e.g., documentation sites, code repositories synced by the watcher). They experience progressively slower ingest as collections grow, with no workaround short of splitting collections. The watcher sync path (`sync.py`) is arguably the most important path for these users — it is the one that runs continuously in the background, and it is currently the most affected by O(collection-size) rebuild cost.

## Core Flow

1. Operator or watcher triggers `ingest_file()` or `ingest_directory()`.
2. Old chunks for the affected document(s) are deleted from the vector store.
3. New chunks are embedded and written to the store.
4. **Incremental FTS update** adds/replaces only the new chunks in the FTS index via `table.optimize()` (no full rebuild).
5. Operator triggers `delete_document()`.
6. Chunks are removed from the vector store.
7. **Incremental FTS delete** ensures the FTS index no longer contains entries for the removed chunks. **Note: `pipeline.delete_document()` currently does NOT touch the FTS index at all — step 7 is entirely new behavior, not a refactor of an existing path.**
8. Full rebuild remains available via `rebuild_fts_index()` for operator-initiated FTS repair.

## In Scope
- Replace calls to `rebuild_fts_index()` in `pipeline.py` (`ingest_file` at line 345, `ingest_directory` at line 422), `sync.py` (line 719: `await self._pipeline.store.rebuild_fts_index(name)`), and `store.py` (line 1402: inside `reindex_metadata()`) with incremental FTS maintenance — likely via `table.optimize()`, confirmed by the spike.
- **Four call sites total**: `pipeline.py:345`, `pipeline.py:422`, `sync.py:719`, `store.py:1402`. The `sync.py:719` call also omits the `language=` parameter (a pre-existing bug this work should fix); `store.py:1402` has the same missing `language=` bug.
- **`store.py:1402` scoping note**: `reindex_metadata()` updates metadata fields only, not the `text` column. If `text` content does not change, a full FTS rebuild there may be unnecessary. Decide during implementation whether this call site should switch to `optimize()`, remain as a full rebuild, or be removed entirely. This is a design decision to resolve after the spike confirms `optimize()` semantics — do not resolve it during the spike.
- Incremental FTS maintenance when `pipeline.delete_document()` removes chunks (net-new behavior — currently `delete_document` does not touch FTS at all).
- `store.delete_by_source_path()` (used by `sync.py:652`) delegates entirely to `store.delete_document()` — only one FTS hook point is needed; covering `store.delete_document()` covers both delete paths.
- Language tokenizer consistency: use stored per-collection language config when set; re-derive from dominant language as fallback.
- A spike task to verify `table.optimize()` semantics before design is locked (see Spike Gates below).
- Ingest latency p95 regression guard added to the eval harness (first ingest-latency threshold gate).

## Out of Scope
- Automatic background compaction — operators use `rebuild_fts_index()` directly or the existing `reindex_collection` endpoint when needed; document the guidance.
- Migration of pre-C6 FTS indexes — existing indexes remain valid; C6 incremental path applies to all mutations going forward.
- Changes to search ranking or FTS query logic — C6 is a maintenance path change only.

## Key Decisions

- **Spike before design**: LanceDB's incremental FTS model is `table.optimize()` — rows added after index creation are not indexed until `optimize()` is called. The `replace` parameter on `table.create_index()` is a boolean "overwrite existing index" flag, not an incremental-append toggle; `replace=False` is not the incremental mechanism. The spike must verify `optimize()` semantics before any `store.py` changes are written.

- **`optimize()` invocation strategy**: decide when `optimize()` is called — after every `ingest_file`, after every `ingest_directory` as a batch, after N mutations, or never (relying on LanceDB's flat-scan fallback for unindexed rows). The latency goal implies calling `optimize()` at the end of each ingest operation; accumulating unindexed rows indefinitely defeats the purpose. Confirm the right granularity during the spike.

- **Incremental delete on document removal**: `delete_document()` must trigger FTS maintenance so FTS stays coherent across all mutation types. `delete_by_source_path()` delegates to `delete_document()`, so a single hook point covers both delete paths. Whether `optimize()` handles deleted-row cleanup determines the implementation (see Plan B under Spike Gates).

- **FTS optimize layer (store vs pipeline)**: FTS maintenance can live at the store layer (inside `store.delete_document()` and a new `store.optimize_fts()` method) or at the pipeline layer (callers decide when to call optimize). Store-level is simpler but creates a double-optimize risk in `ingest_file()`, which calls `store.delete_document()` (line 331) and then would separately call optimize after the add. To avoid this: if optimize is added to `store.delete_document()`, add a `skip_fts_optimize: bool = False` parameter to suppress it when the caller (e.g., `ingest_file`) will optimize separately. Pipeline-level avoids this problem but requires touching all callers explicitly. Decide and document during implementation (after the spike confirms the API).

- **No auto-compaction**: the existing `reindex_collection` operator endpoint triggers a full re-ingest (re-parse, re-chunk, re-embed, re-index via `pipeline.ingest_directory`) — it is not FTS-only compaction and is significantly heavier than what C6 introduces. Lightweight FTS-only compaction (bypassing re-embed) is deferred to C6.1.

- **Language tokenizer**: use stored collection language config if present; re-derive from dominant language if not — consistent with the per-collection config pattern from C1/C2. Fix the missing `language=` parameter in the `sync.py:719` call site as part of this work.

## Spike Gates

The spike must confirm all of the following before the implementation plan is finalized:

| Gate | Question |
|------|----------|
| (a) API availability | Is `table.optimize()` available on the async LanceDB table API used in `store.py`? |
| (b) New-row indexing | Does calling `optimize()` incorporate rows added after index creation into the FTS index? |
| (c) Deleted-row cleanup | Does `optimize()` remove FTS entries for rows deleted via `table.delete()`? |
| (d) Concurrent safety | Is it safe to call `optimize()` concurrently on the same table, or alongside concurrent mutations? |
| (e) Compatibility | Does `optimize()` work correctly on indexes originally created with `replace=True`? |
| (f) update-row indexing | Does `optimize()` re-index FTS content for rows modified via `table.update()`? (Relevant for `reindex_metadata` path.) |

**Plan B (if gate (c) fails)**: If `optimize()` does not clean up deleted-row FTS entries, the delete path must be handled differently. Two options:
- **Option A**: add `rebuild_fts_index()` to delete paths — new behavior, O(collection-size) cost per delete, but prevents phantom hits in FTS results.
- **Option B**: leave delete paths untouched — truly the current behavior (no FTS maintenance at all on delete), phantom hits persist until the next ingest or explicit reindex.

**C6 chooses Option A.** Phantom hits after delete are worse than a slower delete — they silently corrupt search results. Adding `rebuild_fts_index()` on delete is new behavior for this path regardless of spike outcome, but it is the correct tradeoff. Acknowledge the O(collection-size) cost as an accepted limitation for delete operations when gate (c) fails; it does not block the add/update path from being incremental.

**Plan C (if gate (a) or (b) fails)**: If `table.optimize()` is not available on the async LanceDB table API (gate (a)), or if it does not incorporate newly added rows into the FTS index (gate (b)), C6 is deferred. Document the spike findings, file a LanceDB upstream issue or wait for a LanceDB version that exposes the required API, and close this brief. Do not implement a workaround that re-introduces O(collection-size) rebuild cost for the add path — that is the status quo and not an improvement.

## Edge Cases & Constraints

- **Re-ingest of same document**: delete old chunks from FTS, then add new chunks — must be a delete-then-add sequence, not a pure update, since chunk IDs change when content changes.
- **Batch ingest (`ingest_directory`)**: currently rebuilds FTS once at the end; C6 should issue one `optimize()` call per directory run (not N per-file calls) to minimize index write amplification.
- **Concurrency**: concurrent `ingest_file` calls on the same collection, and concurrent ingest + delete, must not corrupt the FTS index. The spike must verify whether `table.optimize()` is safe under concurrent mutations (gate (d)). If not, FTS operations must be serialized inside the same per-collection lock used for vector store operations.
- **Mixed-state collections**: collections indexed before C6 have valid FTS indexes; no migration needed since all subsequent mutations will use the incremental path.
- **Tokenizer drift on growing multilingual collections**: if a collection's dominant language shifts over time, FTS tokenizer won't update until an explicit reindex — accepted as documented behavior.
- **Sync path optimize granularity**: the `sync.py` watcher sync cycle processes adds, modifies, and deletes in a single pass before calling `rebuild_fts_index()` once at the end (line 719). C6 should preserve this pattern: call `optimize()` once at the end of each sync cycle, not per-operation, to match the current batching behavior and minimize FTS write amplification.

## Open Questions — RESOLVED

- **`table.optimize()` deleted-row cleanup?** → **PASS** (gate (c)). `optimize()` correctly removes FTS entries for rows deleted via `table.delete()`. Plan A applies throughout. See `Documentation/Backlog/C6-spike-findings.md`.
- **Spike format?** → Throwaway standalone script (`spike_optimize_fts.py`, not committed). Formal integration tests live in `tests/test_fts_spike_gates.py`.
- **Promote latency p95 to hard CI gate?** → **Yes, promoted.** `[ingest_latency].single_file_p95_ms = 500` in `tests/eval/thresholds.toml` is a hard gate — a regression above 500 ms p95 on a 1,000-chunk corpus fails the eval run. Decision rationale: C6's O(delta) guarantee is meaningless if a regression silently reverts to O(collection-size) without failing CI. See `tests/eval/test_eval_suite.py::test_ingest_latency_p95_single_file_on_large_corpus`.

## Future Iterations
- **C6.1 — Compaction policy**: lightweight FTS-only rebuild (not full re-embed) after N incremental updates, with tunable threshold — deferred; operators manage manually for now via `rebuild_fts_index()`.
- **C6.2 — Ingest latency dashboard**: expose ingest timing breakdown (embed vs. store vs. FTS) via the telemetry endpoint for observability.

## Recommendation
This is the right item to tackle after C3b ships. The core risk is in LanceDB's `table.optimize()` API surface — specifically whether it handles deleted-row FTS cleanup (gate (c)). If it does, C6 is a refactor of four call sites (`pipeline.py:345`, `pipeline.py:422`, `sync.py:719`, `store.py:1402`) plus net-new delete-path hooks and a design decision on the `reindex_metadata` call site. If gate (c) fails, add-path ingest becomes incremental while delete-path FTS falls back to a full rebuild (Plan B — new behavior, correct tradeoff). If gates (a) or (b) fail, C6 is deferred entirely (Plan C). Either outcome that reaches implementation is better than the status quo. The spike gates everything and must be the first task in the plan. Do not skip it.

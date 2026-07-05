namespace MaintenanceGcSeam;

/** Config fields that govern the graph GC policy.
    [maintenance] graph_gc = true (default)
    [graph] gc_rebuild_communities = true (default)
    [graph] gc_rebuild_cpu_priority = "low" | "normal" | "high" */
model GraphGcConfig {
  /** Master switch: run graph GC on every maintenance pass. */
  graph_gc: boolean;
  /** Trigger async community rebuild after GC removes nodes. */
  gc_rebuild_communities: boolean;
  /** OS scheduling priority for the rebuild thread. */
  gc_rebuild_cpu_priority: "low" | "normal" | "high";
}

/** State written to .maintenance-state.json after each GC pass.
    Per-collection results. _run_one_pass aggregates these into GcMaintenanceState by summing across all collections.
    Per-collection breakdown (communities_invalidated, orphan_nodes_removed, orphan_edges_removed) is also
    stored in the per-collection GraphGcHealthEntry; the aggregate fields here are the pass-level totals. */
model GcMaintenanceState {
  /** ISO-8601 timestamp of the last completed graph GC pass. */
  last_graph_gc_at: string | null;
  /** Total stale mentions found before pruning in the last GC pass (SUM across all collections; measured by countStaleMentions). */
  stale_mention_count: int32;
  /** True if any collection had communities invalidated but rebuild has not yet completed (aggregate across collections). */
  communities_invalidated: boolean;
  /** Total orphan nodes removed across all collections in this pass. */
  orphan_nodes_removed: int32;
  /** Total orphan edges removed across all collections in this pass. */
  orphan_edges_removed: int32;
}

/** Per-collection health entry additions for graph GC. */
model GraphGcHealthEntry {
  /** Orphan nodes removed in the last GC pass for this collection. */
  graph_orphan_nodes_removed_last_run: int32;
  /** Stale mentions pruned in the last GC pass for this collection. */
  graph_stale_mentions_pruned_last_run: int32;
  /** True if communities were invalidated but rebuild has not yet completed. */
  communities_invalidated: boolean;
}

/** Contract that MaintenanceLoop._run_graph_gc must honour. */
interface GraphGcPolicy {
  /** No-op if graph is disabled or graph_store is None. Otherwise executes in order:
      0. Fetch liveChunkIds from store.list_chunks_raw(collection, namespace) inside a try/except.
         If list_chunks_raw raises (I/O error, table missing, any Exception) → abort GC with WARNING
         ("list_chunks_raw failed for {collection}: {e} — skipping GC to prevent data loss") and skip
         to the next collection; do NOT call pruneStaleMentions.
         If list_chunks_raw succeeds — empty or non-empty — trust the result and proceed.
         Note: collection_meta.chunk_count is NOT used as a guard — TTL expiry never decrements it
         (store.py constraint). Only exceptions from list_chunks_raw trigger abort.
      1. countStaleMentions(collection, liveChunkIds, ns) — measure stale count BEFORE prune
      2. pruneStaleMentions(collection, liveChunkIds, ns) — delete stale mention rows
      3. deleteOrphanNodesAndEdges(collection, ns) — remove nodes/edges with no remaining mentions
      4. write results to new_state (stale_mention_count, last_graph_gc_at, communities_invalidated,
         orphan_nodes_removed, orphan_edges_removed) — state write is deferred to _run_one_pass
      5. if communities_invalidated and config.gc_rebuild_communities = true: check _rebuild_state.get((ns, col));
            if no entry or previous task is done: store RebuildState(task=new_task, pending=False) in _rebuild_state[(ns, col)], attach done-callback that (a) logs exceptions at ERROR, (b) if state.pending is True — clear pending, enqueue a NEW task updating state.task — the new task MUST have the same done-callback attached to it, otherwise the second rebuild has no error logging and no completion signaling; otherwise set state.completed=True, (c) never writes to the state file;
            if an entry exists and task is still in-flight: set state.pending=True only. Do NOT enqueue a second task.
      6. CPU priority inside asyncio.to_thread Leiden worker (Linux only, sys.platform == 'linux').
         The enter-setpriority call and the Leiden work MUST be independently guarded:
         Enter step (initialize original_nice = None before try; independent try/except — failure does NOT skip Leiden):
           original_nice = None.
           if sys.platform == 'linux': try: original_nice = os.getpriority(os.PRIO_PROCESS, 0);
           os.setpriority(os.PRIO_PROCESS, 0, cpu_nice_value);
           except (PermissionError, AttributeError, OSError) as e: log WARNING. else: pass.
         Leiden work (always runs regardless of enter outcome):
           try: <leiden work> finally: if sys.platform == 'linux' and original_nice is not None:
           try: os.setpriority(os.PRIO_PROCESS, 0, original_nice); except (PermissionError, AttributeError, OSError): pass.
         The finally restores original_nice (captured via os.getpriority before the call), not
         hardcoded 0 — this preserves a pre-existing process nice value (e.g. Nice=5 from systemd).
         A PermissionError on enter logs a WARNING but does not skip the Leiden work. */
  /** Returns per-collection GC results. Callers (_run_one_pass) aggregate across collections to build the pass-level GcMaintenanceState. */
  runGraphGc(collection: string, ns: string): GraphGcHealthEntry;
}

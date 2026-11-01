// C11 — Shared write-description operation (internal logical seam).
//
// Layer edge: Use Cases (generation, manual set and clear, import) -> Entities, through the
// consumer-owned `DescriptionStore` port (C3), which the store implements.
//
// Today the routing vector is computed only inside the metadata recompute (a full scan with an early
// return when nothing needs recomputing), and the description write copies the old vector forward,
// so a changed description leaves a stale or missing vector. ONE operation now owns every change:
// whoever changes the description also refreshes its routing vector in the same step: generate,
// manual set (compute the vector), clear (remove the vector), import.
// Atomicity (lost updates): the pipeline today reads the metadata row, then runs a slow step (a model
// call of up to `timeout_seconds` per attempt, or the recompute's scan of every vector), then writes
// the row back from that earlier read, so a manual set, a clear or a second generation that lands in
// between is overwritten. The write step therefore re-reads the row INSIDE the collection lock, checks
// the guard (C3 `WriteGuard`: a generated change or a recorded failure is refused with
// `refusedManual` when the row's source is `manual`, and with `refusedStale` when the row's
// description, source, last-described or stored status differ from the state the generation started from), changes
// only the description fields (C3 `DescriptionStore.write`) and writes once. The model call and the
// embedding run OUTSIDE the lock; only the read-check-write is inside it. The baseline is saved only
// on a successful generated write; set leaves it untouched; clear resets it.
// Failure recording: a failed generation records status `failed` and keeps the earlier description,
// under the SAME guard as a generated write. So a failure that lands after a manual set (`manual` /
// `ok` must not become `failed`) or after a clear (the cleared row must not be left `failed`, which
// startup backfill would skip for good) is refused and discarded, and the caller settles
// `refusedManual` or `superseded` (C5), not `failed`.
// Vector ownership: this operation is the ONLY writer of the description vector. The metadata
// recompute (pipeline.py lines 3940-4047) today embeds the description with the embedder it is passed
// on every full scan (line 4024), and the callers differ: ingest and sync pass the pipeline's GLOBAL
// embedder (pipeline.py lines 1053 and 1360, sync.py line 926), import passes the collection's own
// (routes_export.py line 798). A recompute that ran after a write here would overwrite a correct
// vector with one from the wrong model. The rule: every whole-row writer, the recompute included,
// carries the stored vector as read inside the lock; the recompute computes one itself only when the
// row has a description and no vector AND the passed embedder's `model_name` equals the row's
// `active_embedding_model` (the legacy-description repair it does today, now unable to write a
// wrong-model vector); a recompute never replaces a vector that exists.
// Vector source: the embedding comes from the COLLECTION's own embedder (the model in the row's
// `active_embedding_model`, taken from the embedder cache), not the global embedder, because
// collections carry per-collection models (ADR 08) and the router accepts a description vector only
// when its length matches the query embedding and the collection's model matches. The wait is bounded
// on the CALLER's side: `embedWaitSeconds` wraps the whole step (`get_or_load` plus the first
// `embed_one`, which builds the lazy model), because the cache's own `_LOAD_WAIT_TIMEOUT_SECONDS`
// (embedder_cache.py lines 23 and 110) bounds only a waiter that joins another caller's load, never
// the caller that is itself the loader. A timeout or any embedding failure counts as failure to embed:
// the description is kept without a vector and a WARNING is logged; the write itself still succeeds
// (`keptWithoutVector`). A lock timeout is reported, never swallowed, for the write and for
// `recordFailure`; a lock timeout while recording a failure is logged and shown as the last attempt,
// because status cannot be stored.
// Entry point for forced regeneration: the describe job reaches generation through the lifecycle
// (C5), which forces; the `force_regenerate_description` parameter of `ingest_directory` (pipeline.py
// line 1232) is not on that path and is removed in the cutover.
// Callers hold no collection lock: the write takes it itself and it is not reentrant (C3).
//
// Logical only.

import "./2026-10-30-010-llm-collection-descriptions-c3-description-state.tsp";
using DescriptionStateContract;

namespace WriteDescriptionContract;

/// Caller-side bound on loading the collection's embedder and embedding the description, in seconds.
/// Equal to the cache's waiter bound (ADR 11) so one number serves both.
const embedWaitSeconds = 120;

union DescriptionChange {
  generated: GeneratedChange,
  manualSet: ManualSetChange,
  cleared: ClearChange,
  imported: ImportedChange,
}

model GeneratedChange {
  text: string;
  docCountAtGeneration: int32;
  /// The state the generation started from, read after it acquired its slot (C5).
  expected: ExpectedState;
}

model ManualSetChange {
  text: string;
}

model ClearChange {}

/// `text` has already been normalised by the import rules (C9).
model ImportedChange {
  text: string;
  source: DescriptionSource;
}

enum VectorOutcome {
  refreshed,
  removed,
  /// Embedding failed or timed out: the description is kept without a vector and a WARNING is logged.
  keptWithoutVector,
  /// Nothing was written (`lockTimeout`, `collectionMissing`, `refusedManual`, `refusedStale`), so the
  /// vector is unchanged.
  untouched,
}

model WriteResult {
  outcome: DescriptionWriteOutcome;
  vector: VectorOutcome;
}

/// A failed generation records status `failed` and keeps the earlier description.
model FailedAttemptRecord {
  reason: string;
  /// The same expected state as the generated write it replaces.
  expected: ExpectedState;
}

interface WriteDescription {
  apply(collection: string, collectionNamespace: string, change: DescriptionChange): WriteResult;

  /// Guarded like a generated write: `refusedManual`, `refusedStale`, `lockTimeout` and
  /// `collectionMissing` are returned, never swallowed.
  recordFailure(collection: string, collectionNamespace: string, attempt: FailedAttemptRecord): DescriptionWriteOutcome;
}

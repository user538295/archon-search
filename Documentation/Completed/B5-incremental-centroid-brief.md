# Feature Brief: B5 — Incremental Centroid Maintenance

> **Status**: AAA-reviewed. See [AAA findings](#aaa-review-findings) for the 8 issues added to this brief.
> This brief supersedes the earlier draft and is the input for `/plan-maker`.

## Problem
`MultiCollectionRouter` routes queries using centroid values stored in `_archon_collection_meta`, but those centroids are wrong in three distinct ways: a second ingest batch overwrites the cumulative centroid with batch-only values; deleting a document never updates the centroid; and the sync watchdog rescans all chunk vectors on every file change regardless of corpus size. All three failures are silent — no error, degraded routing.

## Goal
After B5, `_archon_collection_meta` maintains `(centroid_sum, chunk_count)` incrementally at the store layer on every ingest and delete, under the existing per-collection lock. The derived centroid `= centroid_sum / chunk_count` is always correct. The O(n) full-rescan path is removed from the watcher-sync hot path and reserved for explicit drift-reset and reindex operations only.

## Users & Context
Archon-search operators and B4/B6 consumers who depend on correct routing. The defects are silent — routing silently degrades after any second ingest or any delete, with no user-visible error. The fix is invisible to end-users but material to routing quality at scale.

## Core Flow
1. **Ingest** (`ingest_chunks`): while holding the per-collection lock, read existing `centroid_sum` + `chunk_count`, accumulate batch sum and count, write back derived centroid — all inside the existing lock scope. Return a `needs_recompute` signal if the mutation counter exceeds the checkpoint threshold.
2. **Delete** (`delete_document`): acquire the per-collection lock (new), fetch the document's vectors before deletion, subtract them from `centroid_sum`, decrement `chunk_count`, write back derived centroid. Release lock.
3. **Pipeline** (`ingest_directory`): stop writing centroid/count fields directly. Delegate description/timestamp writes to a new `store.update_description()` partial-write method. Collect `needs_recompute` signals; trigger `recompute_collection_meta` if any signal fires — **pipeline is the sole signal consumer** (not sync).
4. **Sync** (`sync.py`): remove the hot-path `recompute_collection_meta` call. Trust the incremental pipeline maintenance. Sync is now O(batch), not O(corpus).
5. **Drift reset** (`recompute_collection_meta`): retained as the authoritative full-rescan path. Extended to also write `centroid_sum` and reset mutation counters.

## In Scope
- `CollectionMeta` dataclass: three new fields (`centroid_sum`, `mutations_since_recompute`, `needs_recompute`).
- `_meta_schema`: three new columns (`centroid_sum_json`, `mutations_since_recompute`, `needs_recompute`).
- `migrate_centroid_sum()`: idempotent `add_columns` migration for pre-B5 tables, independently idempotent per column.
- Store-layer unlocked helpers: `_do_read_meta_unlocked`, `_do_write_meta_unlocked`, `_do_fetch_doc_vectors_unlocked`. Named with `_unlocked` suffix (not just `_do_`) to make the lock precondition discoverable.
- Validation helpers: `_centroid_sum_valid`, `_batch_vectors_valid` (using `math.isfinite`, stdlib only — correct for `list[float]` inputs).
- `ingest_chunks` incremental-add logic + **`ChunkIngestResult`** return type. *Not* `IngestResult` — name collision with `_types.IngestResult(doc_id, chunks_created, status)`.
- `_do_update_meta_on_add` returns plain `bool` (the `needs_recompute` signal), not a one-field dataclass.
- `delete_document` lock acquisition (with `StoreBusyError` timeout) + incremental-subtract logic.
- `store.update_description()` partial-write method (acquires lock; never touches centroid fields).
- `pipeline.ingest_directory` refactor: remove direct `update_collection_meta` call; use `update_description`; wire `needs_recompute` signal. **Pipeline is the sole recompute signal consumer** — sync never re-checks the signal independently.
- `recompute_collection_meta` extension: writes `centroid_sum`, resets counters.
- `sync.py`: remove hot-path recompute; signal check is in `ingest_directory` only.
- `elementwise_sum` helper in **`store.py`** (not `pipeline.py` — avoids circular import since `pipeline.py` already imports from `store.py`). Import it into `pipeline.py` for `recompute_collection_meta`.
- `centroid_recompute_threshold: int = 10_000` in `SearchConfig` with `>= 1` validation.
- `logger.warning("Collection %r centroid stale, recompute queued", collection)` at **every** `needs_recompute=True` flag-set site (observability gate).
- Runbook entry in `160_operational_readiness_monitoring_and_reliability.md` covering stale centroid symptoms, causes, and recovery (`recompute_collection_meta` manual trigger).
- `BREAKING.md`: three new additive internal columns.
- Doc reconciliation: `530_technical_debt_refactoring_roadmap.md` (CON-4 — fix description to say O(batch)-but-wrong on ingest, O(chunks) on sync path), `03_world_class_roadmap.md`, `130_data_architecture_and_persistence.md`, `210_performance_and_scalability.md` (note that **delete is O(chunks-in-document)**, not O(batch) — the plan must not overclaim).
- `archon-search.toml.example`: `centroid_recompute_threshold = 10000` in `[database]` with comment.

## Out of Scope
- Stronger routing, multi-centroid, or summary-embedding representations (B4).
- Router algorithm or `_cosine_similarity` changes.
- Cross-process locking of the meta read-modify-write.
- `fsync` on meta-table writes (A7's concern).
- Surfacing `needs_recompute` in `GET /status` response — deferred (API shape change, log warning covers v1 observability).
- Backfill CLI for `centroid_sum`.
- Background/async deferral of the pre-B5 lazy seed (v2).
- Write-ahead intent log for crash-mid-lock recovery (v2).

## Key Decisions

**`elementwise_sum` lives in `store.py`**: needed by both `store.py` (delete subtract) and `pipeline.py` (recompute). Defining it in `pipeline.py` creates a circular import. Store.py is the correct owner.

**`embedding_model` default is `None`, not `""`**: `""` would silently trigger `needs_recompute=True` → full O(n) rescan on every ingest during the gap between Phase 3 (store wiring) and Phase 5 (pipeline passes model name). `None` means "caller did not supply model; skip centroid maintenance." Once Phase 5 ships, all callers pass `self._embedder.model_name`.

**`ChunkIngestResult`, not `IngestResult`**: avoids shadowing the existing `_types.IngestResult(doc_id, chunks_created, status)`. Fields: `chunks_ingested: int`, `needs_recompute: bool`.

**`_do_update_meta_on_add` returns `bool`**: one-field dataclasses for internal returns are overengineered. `ingest_chunks` wraps the bool into `ChunkIngestResult`.

**Pipeline is the sole `needs_recompute` consumer**: both `ingest_directory` and `sync.py` had independent signal checks, which would race and double-trigger `recompute_collection_meta`. Pipeline handles the signal; sync calls `ingest_directory` and trusts it.

**Observability via warning log + runbook**: fires at every `needs_recompute=True` set site. Surfacing the flag in `GET /status` is deferred — not needed for v1 correctness.

**Delete is O(chunks-in-document), not O(batch)**: `_do_fetch_doc_vectors_unlocked` materialises all vectors for the deleted document. For a 10K-chunk document at BGE-small, that's ~15 MB. The plan does not claim O(batch) for delete — only ingest is O(batch) after B5. Document in `210_performance_and_scalability.md`.

**`centroid_sum` is internal**: not added to `_ROUTING_FIELDS`, `CollectionDetail`, or any MCP/REST payload. B5 is a pure hardening change with no public contract break.

**`delete_document` threshold signal via meta flag, not return type**: when `mutations_since_recompute` crosses the threshold on delete, write `needs_recompute=True` to the meta row — no return type change. The flag is picked up by the next `ingest_chunks` call or explicit `recompute_collection_meta`. Delete-only workloads that never ingest must trigger a manual recompute; this is documented in the runbook. Option chosen over a `DeleteResult` return type (breaks all callers, adds scope) and ignoring it (silent drift on delete-heavy corpora).

**`update_description` uses `asyncio.wait_for` with `INGEST_LOCK_TIMEOUT_S`**: consistent with every other lock acquire in the store. On timeout, log a warning and return without writing — caller keeps the old description. Safe fallback: description is cosmetic, not routing-critical. Prevents the description-generation sync path from stalling indefinitely behind a long-running `reindex_metadata`.

## Edge Cases & Constraints

- **Pre-B5 collection, first post-B5 ingest**: `centroid_sum` is null → `_centroid_sum_valid` returns False → `needs_recompute=True`. Store accumulates from `(0-vector, 0, 0)` for the batch. Pipeline receives signal, calls `recompute_collection_meta` inline (one-time O(n) spike). `logger.warning` fires. Documented in upgrade notes as expected one-time cost. For collections with 1M+ chunks, this spike is ~3 GB in Python — noted in Known Limitations.

- **Delete from pre-B5 collection (centroid_sum=None)**: `_do_subtract_meta_on_delete` detects None → sets `needs_recompute=True`, **skips subtraction**, writes flag. Warning log fires. Next ingest triggers the reseed. No silent data corruption.

- **Crash between chunk-table write and meta-write**: hard crash in this window leaves centroid stale with `needs_recompute=False` — the flag that would trigger recovery is itself not written. Recovery requires manual `recompute_collection_meta`. This gap must be called out explicitly in Known Limitations (not glossed over as "reconciled by checkpoint"). The periodic checkpoint only helps if the process restarts and reaches the counter — it does not help if the crash window is the exact meta write.

- **Dual recompute race eliminated**: pipeline is the sole consumer of `needs_recompute`. Sync never independently triggers `recompute_collection_meta` from the same signal. The second call would be a no-op but was wasteful and confusing.

- **`delete_document` signal gap**: delete bumps `mutations_since_recompute` on the meta row but returns only `int` (no signal to caller). A delete-only workload that crosses the 10K threshold will not trigger a checkpoint until the next ingest. Accepted for v1 — called out in Open Questions.

- **Concurrent ingest + delete, same collection**: serialized by the same per-collection lock. No lost update possible.

- **Model switch mid-run**: `_centroid_sum_valid` detects model or dimension mismatch → `needs_recompute=True`, accumulate from zero for this batch. Warning log fires.

- **`reindex_metadata` starvation**: holds `_lock_for(collection)` with no timeout. `delete_document` now contends for the same lock and will raise `StoreBusyError` after `INGEST_LOCK_TIMEOUT_S`. Behavior matches ingest. Accepted.

- **`update_description` lock contention**: acquires `_lock_for(collection)` with no timeout. If `reindex_metadata` holds the lock for tens of seconds, `update_description` stalls indefinitely. No error, no log. Accepted for v1; adding a timeout is Future Work.

- **`centroid` duplication**: `centroid` is derived from `centroid_sum / chunk_count` but persisted independently (router reads `centroid` directly). Invariant is enforced by store being the sole writer of both. `update_description` never touches either.

- **`centroid_recompute_threshold` misconfiguration**: validated `>= 1` in `config.py`. Setting to 1 makes every ingest trigger a full recompute (degrades to current behavior). Setting very high defers drift reset indefinitely. Add a calibration comment in `archon-search.toml.example`.

## Open Questions
None. All decisions resolved.

## AAA Review Findings

Issues identified by AAA review (2026-05-25) that this brief addresses:

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | ❌ Blocking | `IngestResult` name collision with `_types.IngestResult` | Renamed to `ChunkIngestResult` |
| 2 | ❌ Blocking | `elementwise_sum` in `pipeline.py` creates circular import | Moved to `store.py` |
| 3 | ❌ Blocking | `embedding_model=""` default silently triggers O(n) rescan between phases | Changed to `None` = skip maintenance |
| 4 | ❌ Critical | `needs_recompute=True` set silently — no log, no runbook, invisible to operators | Added `logger.warning` at every set site + runbook entry |
| 5 | ⚠️ Major | Dual signal consumers (pipeline + sync) double-trigger `recompute_collection_meta` | Pipeline is sole consumer |
| 6 | ⚠️ Major | Crash-mid-lock recovery story incomplete: flag not written = silent stale centroid | Added to Known Limitations explicitly; manual recompute is the recovery path |
| 7 | ⚠️ Major | Plan overclaims "O(batch)" — delete is O(chunks-in-document) | Corrected in scope and doc-reconciliation list |
| 8 | ⚠️ Minor | `IngestMetaSignal` one-field dataclass overengineered | Replaced with plain `bool` |

## Future Iterations
- Surface `needs_recompute` per collection in `GET /status` (API shape change, deferred to B5.1).
- Background/async deferral of the pre-B5 lazy seed to avoid blocking first post-B5 ingest on large corpora.
- Chunked `_do_fetch_doc_vectors_unlocked` for large documents to cap per-delete memory.
- Write-ahead intent log for crash-mid-lock recovery.

## Recommendation
Build this before B4 and frame it as a **correctness fix, not a performance optimisation** — the debt register undersells it. The hardest part is not the schema change or the lock wiring; it is two subtle implementation hazards that would have surfaced as bugs after tests pass: the `elementwise_sum` circular import (moves it to store.py) and the `embedding_model=None` default (prevents an O(n) regression across phases). The crash-mid-lock gap is real and cannot be fully closed in v1 — call it out precisely in Known Limitations, provide the manual recovery runbook entry, and add the warning log. Do not ship without those two: a silent stale centroid after a crash, with no operator-visible signal, is worse than the three defects B5 was written to fix.

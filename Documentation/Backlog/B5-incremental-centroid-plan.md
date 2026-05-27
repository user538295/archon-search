# B5 — Incremental Centroid Update

**Purpose**: Fix three concrete defects in centroid maintenance — batch-only overwrite on ingest, no update on delete, O(chunks) full rescan on the watcher-sync hot path — by replacing them with a single `(centroid_sum, chunk_count)` running-pair maintained at the store layer.
**Audience**: archon-search contributors implementing B5; reviewers; B4 teams depending on a correct centroid baseline.
**Status**: To Do

---

## Background

`MultiCollectionRouter.rank` routes queries against `centroid` values stored in `_archon_collection_meta`. Three independent defects make those centroids wrong, stale, or expensive:

- **Defect 1 (batch-only overwrite)**: `ingest_directory` computes `centroid = _compute_centroid(all_vectors)` from only the current-batch vectors and then calls `update_collection_meta`, which overwrites the whole row. A second batch into a non-empty collection clobbers the cumulative centroid with batch-only values.
- **Defect 2 (delete-ignores-centroid)**: `store.delete_document` never updates the centroid. After any delete the persisted centroid and counts are stale.
- **Defect 3 (O(chunks) hot-path rescan)**: `SearchPipeline.recompute_collection_meta` calls `store.get_all_vectors` — loading every chunk vector — on the watcher-sync path (`sync.py:697`). Cost grows linearly with corpus size and is paid on every sync cycle.

The debt register (`530_technical_debt_refactoring_roadmap.md`, `CON-4`) describes this as O(chunks) "on every ingest"; the code's primary ingest path is O(batch)-but-wrong and the O(chunks) cost lives on the sync/recompute path. B5 fixes all three real defects and reconciles the docs to match (code-as-source-of-truth).

## Goal

After B5: the `_archon_collection_meta` table holds a `centroid_sum_json` column alongside the existing `centroid_json`. The store layer maintains `(centroid_sum, chunk_count, doc_count)` incrementally inside every `ingest_chunks` and `delete_document` call, under the existing per-collection `asyncio.Lock`, and derives the mean centroid from the pair. The pipeline no longer writes `centroid`/`chunk_count`/`doc_count`; it writes description and timestamps through a new `store.update_description()` partial-write method. The full O(chunks) `recompute_collection_meta` path is retained as the authoritative drift-reset (reindex, lazy pre-B5 seed, periodic checkpoint) but is removed from the watcher-sync hot path. A default-on periodic-checkpoint counter on the meta row signals the pipeline caller to trigger a recompute after ~10 000 mutations, bounding accumulated cancellation drift.

---

## Scope

### In Scope
- Add `centroid_sum: list[float] | None = None` to `CollectionMeta` (`collection_meta.py`).
- Add `pa.field("centroid_sum_json", pa.utf8())` to `SearchStore._meta_schema`; thread it through `update_collection_meta` (write) and `get_collection_meta` / `_row_to_meta` (read, with malformed-JSON → `None` fallback).
- Add `mutations_since_recompute: int = 0` and `needs_recompute: bool = False` fields to `CollectionMeta` and the meta schema (internal bookkeeping for the periodic checkpoint signal).
- Store-layer incremental add: `ingest_chunks` reads existing `(centroid_sum, chunk_count, doc_count)` under the held per-collection lock, accumulates the batch sum and counts, persists `centroid_sum'`, `chunk_count'`, `doc_count'`, derived `centroid`, and bumps `mutations_since_recompute`. Returns a `needs_recompute` signal to the caller when the counter exceeds the configurable threshold (`centroid_recompute_threshold`, default 10 000) or when the `needs_recompute` flag is set. Brand-new-collection bootstrap (absent meta row / absent `_META_TABLE`) treats the missing row as the `(0-vector, 0, 0)` seed and creates the row.
- Pre-B5 collection seed: on first post-B5 **ingest** into a collection where `centroid_sum_json` is absent/null, `_do_update_meta_on_add` returns `needs_recompute=True`; the pipeline triggers one lazy `recompute_collection_meta` pass and then maintains incrementally. The seed does NOT fire on read paths.
- Embedding-model / dimension / NaN-or-inf guard: reseed if `meta.embedding_model ≠ writer's model_name`, stored `centroid_sum` length ≠ current `embedding_dim`, or any element of the stored sum or an incoming per-vector input is NaN or inf.
- Store-layer incremental subtract: `delete_document` acquires the same per-collection lock (with `StoreBusyError` timeout mirroring `ingest_chunks`), fetches the target document's vectors before deletion, subtracts them from `centroid_sum`, decrements `chunk_count` and (when rows removed) `doc_count`, bumps `last_indexed`, and resets `centroid_sum`/`centroid` to `None` when `chunk_count` reaches 0.
- In-lock paths use unlocked `_do_*`-style helpers only — no re-acquisition of `_lock_for(collection)` while held.
- New `store.update_description(collection, description, last_described, described_at_doc_count, last_indexed)`: acquires the per-collection lock, does a partial update of description/timestamp fields only, never touches `centroid_sum`/`chunk_count`/`doc_count`/`centroid`. No-op if the meta row is absent.
- Pipeline `ingest_directory` no longer constructs `CollectionMeta` or calls `update_collection_meta`; it delegates description/`last_indexed` to `store.update_description` inside the existing `if all_vectors:` guard.
- `recompute_collection_meta` also populates `centroid_sum` from the same `get_all_vectors` pass, resets `mutations_since_recompute` to 0 and `needs_recompute` to `False`.
- Remove the `recompute_collection_meta` call from `sync.py:697` (watcher-sync hot path); add post-mutation `needs_recompute` signal check in `ingest_directory` / sync, invoking `recompute_collection_meta` when signalled.
- Add `centroid_recompute_threshold: int = 10_000` to `SearchConfig`.
- `BREAKING.md`: record `centroid_sum_json`, `mutations_since_recompute`, `needs_recompute` as additive internal-schema columns (not a public REST/MCP break). **Do not claim old-binary forward-compatibility on writes**: an older binary's `update_collection_meta` uses its own `_meta_schema` (without the new columns); a delete-then-insert upsert on a table that already has the new columns may null them out. Add a specific integration test (`test_old_schema_upsert_preserves_new_columns`) to verify LanceDB's actual behavior before making any compatibility claim in the changelog.
- Doc reconciliation: amend `CON-4` in `530_technical_debt_refactoring_roadmap.md`, B5 line in `03_world_class_roadmap.md`, add `centroid_sum_json` column note to `130_data_architecture_and_persistence.md`, add O(batch) ingest note to `210_performance_and_scalability.md`.

### Out of Scope
- Stronger routing / multi-centroid / summary-embedding representations (B4).
- Router algorithm or `_cosine_similarity` changes.
- Router cache invalidation API (A6 — B5 depends on it but does not reimplement it).
- `recompute_collection_meta` removal — retained for reindex / seed / recovery.
- Cross-process locking of the meta read-modify-write.
- `fsync` on meta-table writes (A7's concern).
- `count_documents` / `list_documents` general optimisation beyond removing centroid dependence.
- A1 `reindex_metadata` (per-row `file_type`/`updated_at` backfill) — unrelated.
- Backfill CLI for `centroid_sum`.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 7.1 — Final verification & documentation update].

---

## What does NOT change
- Public REST/MCP API contract: `centroid_sum_json` is an internal column, not returned in any response.
- `_ROUTING_FIELDS` in `router.py` — router still consumes the derived `centroid` field only.
- `recompute_collection_meta` signature and semantics — retained and extended (now also writes `centroid_sum`).
- `_compute_centroid` in `pipeline.py` — stays as the full-rescan implementation.
- `delete_document` return type (int, row count removed) — unchanged; the lock acquire is additive behaviour.
- The router's existing `0.0` cosine score for zero-norm / `None` centroid (`router.py:28`).
- `update_collection_meta` whole-row upsert shape — retained for full recompute/seed paths. **B5 adds a per-collection lock acquire** to this method (Task 2.0) so the naming convention (`_unlocked` = unlocked helper; public = locked) becomes consistent and a concurrent `ingest_chunks` cannot race it.

---

## Known limitations / accepted trade-offs
- **Cross-table non-transactionality / crash-mid-lock**: chunk-table write and `_META_TABLE` write are two separate LanceDB commits under one held lock. A hard crash **in the window between these two writes** leaves the centroid stale with `needs_recompute=False` — the flag that would trigger recovery is itself not written. The periodic checkpoint only helps if the process restarts and subsequently reaches the counter; it does not help if the crash window is the exact meta write. Recovery requires a manual `recompute_collection_meta` call; the runbook documents the symptoms and the recovery step. Byte-durability of the writes themselves is A7's concern.
- **Cross-process concurrency**: the `asyncio.Lock` is in-process only; cross-process access is not protected (consistent with A6/A1 limitation; documented).
- **`reindex_metadata` starvation**: it holds the per-collection lock for its full duration with no timeout (`store.py:603`). `delete_document` now uses the same lock with `INGEST_LOCK_TIMEOUT_S`, so a concurrent delete during `reindex_metadata` may receive `StoreBusyError`. Behaviour matches ingest.
- **Delete-only workload signal gap**: `delete_document` bumps `mutations_since_recompute` on the meta row but does not return a recompute signal to its caller (return type stays `int`). A delete-only workload that crosses the 10 000 threshold will not trigger a checkpoint until the next `ingest_chunks` call or an explicit `recompute_collection_meta`. Document in the runbook.
- **Cancellation drift**: float64 accumulation error is ~n·ε (negligible for routing). Catastrophic cancellation risk on very long interleaved add/delete histories is bounded by the periodic-checkpoint recompute (default 10 000 mutations).
- **`centroid_sum` only valid within one embedding model + dimension**: mixing models or dimensions triggers a reseed, not an error. After reseed, the sum correctly reflects the current-model vectors.
- **Delete is O(chunks-in-document), not O(batch)**: `_do_fetch_doc_vectors_unlocked` materialises all vectors for the deleted document. For a 10 K-chunk document at BGE-small (~384 dims), that is ~15 MB. Only ingest is O(batch) after B5.
- **Pre-B5 seed spike**: first post-B5 ingest into a collection with `centroid_sum=None` triggers one lazy `recompute_collection_meta` pass (O(n)). For collections with 1 M+ chunks, this is ~3 GB in Python. Document as expected one-time cost in the upgrade notes.
- **A6 sequencing**: the corrected centroid is persisted correctly after B5, but long-lived router instances (eval path) only observe the correction after A6's `invalidate()` is called. The FastAPI per-request router is unaffected.
- **`recompute_collection_meta` TOCTOU** (accepted): `recompute_collection_meta` reads all vectors and counts outside the lock (O(n) full scan), then calls `update_collection_meta` which acquires the lock only for the write. A concurrent `ingest_chunks` that completes between the read and write will have its incremental update clobbered by the recompute's whole-row upsert. The next incremental add self-corrects (accumulates on top of the post-recompute base). Holding the lock for the full O(n) scan would block all ingest for seconds — not acceptable. Accepted for v1.
- **`ingest_file` delete→ingest transient inconsistency** (v1 accepted): `ingest_file` calls `delete_document` then `ingest_chunks` as two separate lock acquisitions. Between the two lock releases, a concurrent `ingest_file` for a different document in the same collection can read meta that reflects the subtraction but not the addition — `doc_count` and `chunk_count` are transiently 1 lower than truth. Accepted for v1: the window is sub-millisecond under normal load and the router's cosine similarity is drift-tolerant. Recovery: centroid converges on the next ingest or explicit `recompute_collection_meta`. A future v2 could address this by holding the lock across delete+ingest in `ingest_file` via `_locked_by_caller=True`.

---

## Architecture

### New / modified fields

**`archon_search/collection_meta.py`**
```python
@dataclasses.dataclass
class CollectionMeta:
    name: str
    description: str | None = None
    centroid: list[float] | None = None
    centroid_sum: list[float] | None = None       # NEW: running sum for incremental maintenance
    doc_count: int = 0
    chunk_count: int = 0
    embedding_model: str = ""
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    described_at_doc_count: int | None = None
    namespace: str = DEFAULT_NAMESPACE
    mutations_since_recompute: int = 0            # NEW: counter for periodic checkpoint
    needs_recompute: bool = False                 # NEW: stale flag set on meta-write failure
```

**`archon_search/store.py` — `_meta_schema`** (new columns added, all `nullable=True` to match the defensive `or 0` / `or False` reads in `_row_to_meta`):
- `pa.field("centroid_sum_json", pa.utf8(), nullable=True)`
- `pa.field("mutations_since_recompute", pa.int64(), nullable=True)`
- `pa.field("needs_recompute", pa.bool_(), nullable=True)`

### New store methods

```python
# store.py
async def _do_read_meta_unlocked(
    self, db, collection: str, namespace: str
) -> "CollectionMeta | None": ...
# Reads meta row without acquiring lock; used only while lock is already held.

async def _do_write_meta_unlocked(
    self, db, collection: str, meta: "CollectionMeta"
) -> None: ...
# Writes (upserts) meta row without acquiring lock; used only while lock is already held.

async def _do_fetch_doc_vectors_unlocked(
    self, db, collection: str, doc_id: str
) -> list[list[float]]: ...
# Returns vectors for a doc_id without acquiring lock; used inside delete_document lock scope.

async def update_description(
    self,
    collection: str,
    description: str | None,
    last_described: "datetime | None",
    described_at_doc_count: "int | None",
    last_indexed: "datetime | None",
    namespace: str = DEFAULT_NAMESPACE,
) -> None: ...
# Acquires per-collection lock (with timeout); partial-update of description/timestamp fields only.
# No-op if the meta row is absent. Never touches centroid_sum/chunk_count/doc_count/centroid.
```

### Modified store methods

**`ingest_chunks`**: after `_do_ingest`, while lock is still held:
1. If `embedding_model is None` (caller did not supply model), skip steps 2–5 entirely and return `ChunkIngestResult(chunks_ingested=N, needs_recompute=False)`.
2. Read existing meta via `_do_read_meta_unlocked`.
3. Check model/dim/NaN-inf guard via `_centroid_sum_valid`; if triggered: set `needs_recompute=True` and emit `logger.warning`; accumulate from `(0-vector, 0, 0)` seed.
4. Accumulate: `centroid_sum' = centroid_sum + batch_sum`, `chunk_count' = chunk_count + N`, `doc_count' = doc_count + len(distinct doc_ids in batch)`, `mutations_since_recompute' += N`. **`doc_count` is a simple accumulator** — it does not query whether the doc_ids already exist in the collection. Callers must delete before re-ingesting the same document; re-ingesting without a preceding delete inflates `doc_count` by 1. This is a documented precondition, not a store-layer invariant.
5. Derive `centroid = centroid_sum' / chunk_count'`.
6. Persist via `_do_write_meta_unlocked`.
7. Return `ChunkIngestResult(chunks_ingested=N, needs_recompute=mutations_since_recompute' >= threshold or needs_recompute_flag)`.

**`delete_document`**: acquires `_lock_for(collection)` with `asyncio.wait_for` timeout (raising `StoreBusyError`):
1. Fetch doc vectors via `_do_fetch_doc_vectors_unlocked` before deletion.
2. Delete rows.
3. If rows were removed: subtract `del_sum`, decrement `chunk_count`, decrement `doc_count`, bump `last_indexed`, bump `mutations_since_recompute`.
4. If `chunk_count' == 0`: reset `centroid_sum`, `centroid` to `None`.
5. Persist via `_do_write_meta_unlocked`.
6. Return row count (unchanged external interface).

### New config key

**`archon_search/config.py` — `SearchConfig`**:
```python
centroid_recompute_threshold: int = 10_000
# Number of incremental mutations (chunks added + deleted) before the store signals
# the caller to run a full recompute_collection_meta for drift reset.
```

### Pipeline changes

**`ingest_directory`**: remove construction of `CollectionMeta` and the `update_collection_meta` call at `pipeline.py:319–331`. Replace with `await self.store.update_description(collection, description, last_described, described_at_doc_count, last_indexed=datetime.now(UTC))` inside the `if all_vectors:` guard. After each `ingest_file` call (or after the loop), check the returned `needs_recompute` signal and call `recompute_collection_meta` if set.

**`recompute_collection_meta`**: after computing centroid from `get_all_vectors`, compute `centroid_sum = elementwise_sum(vectors)` (imported from `store.py`), and set `meta.centroid_sum = centroid_sum`, `meta.mutations_since_recompute = 0`, `meta.needs_recompute = False`.

**`sync.py`**: remove the `recompute_collection_meta` call at line 697 (hot-path rescan). The per-file incremental maintenance inside `ingest_file` / `delete_document` is now sufficient; the periodic-checkpoint signal in `ingest_directory` handles drift reset.

---

## Task breakdown

### Phase 1 — Schema and dataclass extension
> **Releasable**: after Task 1.2 — the schema and dataclass carry the new fields; no behaviour changes yet. Default pytest run must pass.

#### Task 1.1 — Add `centroid_sum`, `mutations_since_recompute`, `needs_recompute` to `CollectionMeta`
- [ ] **File**: `archon_search/collection_meta.py`
- **Depends on**: nothing
- **Description**:
  - Add three fields to the `CollectionMeta` dataclass (after `centroid`):
    - `centroid_sum: list[float] | None = None`
    - `mutations_since_recompute: int = 0`
    - `needs_recompute: bool = False`
  - No behaviour change — these are read/written as `None`/`0`/`False` until later tasks wire them up.
  - Do not add these to any public REST schema or `_ROUTING_FIELDS`.
- **Releasable**: `CollectionMeta` carries the new fields; existing code ignores them.
- **Tests (TDD)** — `tests/test_collection_meta.py`:
  - Unit: `test_centroid_sum_defaults_to_none` — fresh `CollectionMeta` has `centroid_sum=None`.
  - Unit: `test_mutations_since_recompute_defaults_to_zero` — fresh instance has `mutations_since_recompute=0`.
  - Unit: `test_needs_recompute_defaults_to_false` — fresh instance has `needs_recompute=False`.
  - Checkpoint: `uv run pytest tests/test_collection_meta.py -v`

#### Task 1.2 — Add `centroid_sum_json`, `mutations_since_recompute`, `needs_recompute` to `_meta_schema` and round-trip through `update_collection_meta` / `_row_to_meta`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - In `_meta_schema()`, add (all `nullable=True` — consistent with the `or 0` / `or False` defensive fallbacks in `_row_to_meta`):
    - `pa.field("centroid_sum_json", pa.utf8(), nullable=True)`
    - `pa.field("mutations_since_recompute", pa.int64(), nullable=True)`
    - `pa.field("needs_recompute", pa.bool_(), nullable=True)`
  - In `update_collection_meta`: encode `meta.centroid_sum` as `json.dumps(meta.centroid_sum) if meta.centroid_sum is not None else ""` into `"centroid_sum_json"`. Write `meta.mutations_since_recompute` and `meta.needs_recompute` to their columns.
  - In `_row_to_meta`: parse `row["centroid_sum_json"]` with the same malformed-JSON → `None` + `logger.warning` pattern already used for `centroid_json` (`store.py:336–339`). Parse `mutations_since_recompute` as `int(row.get("mutations_since_recompute") or 0)`. Parse `needs_recompute` as `bool(row.get("needs_recompute") or False)`.
  - The `migrate_namespace`-style `add_columns` migration for pre-B5 rows is handled in Task 1.3.
  - No behaviour change for callers that pass `centroid_sum=None` (which is all callers at this point).
- **Releasable**: schema carries the new columns; `CollectionMeta` round-trips through `update_collection_meta` / `get_collection_meta` with them.
- **Tests (TDD)** — `tests/test_store.py` (add to existing test file):
  - Unit: `test_centroid_sum_json_round_trips` — write a `CollectionMeta` with `centroid_sum=[1.0, 2.0]`, read it back; values match within tolerance.
  - Unit: `test_centroid_sum_json_none_round_trips` — write `centroid_sum=None`; read back as `None`.
  - Unit: `test_malformed_centroid_sum_json_parses_to_none` — manually insert a row with `centroid_sum_json="not-json"`, call `get_collection_meta`; result has `centroid_sum=None` (no exception raised).
  - Unit: `test_mutations_since_recompute_round_trips` — write `mutations_since_recompute=42`; read back as `42`.
  - Unit: `test_needs_recompute_round_trips` — write `needs_recompute=True`; read back as `True`.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "centroid_sum or mutations_since or needs_recompute"`

#### Task 1.3 — Schema migration: `add_columns` for pre-B5 meta rows
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 1.2
- **Description**:
  - Add `async def migrate_centroid_sum(self) -> None:` following the exact pattern of `migrate_namespace` (`store.py:395`):
    - Open `_META_TABLE`; if absent, return.
    - Read schema names; if `"centroid_sum_json"` already present, return (idempotent).
    - Call `await table.add_columns({"centroid_sum_json": "cast('' as string)"})` (empty string default, matching the `None` sentinel). Use explicit SQL cast — bare string literals can cause LanceDB type-inference failures depending on version.
    - Call `await table.add_columns({"mutations_since_recompute": "cast(0 as bigint)"})`.
    - Call `await table.add_columns({"needs_recompute": "cast(false as boolean)"})`.
    - Catch `RuntimeError` with "already exists" in message; log warning and return (concurrent migration guard).
  - Wire `migrate_centroid_sum()` into `SearchStore`'s startup sequence alongside `migrate_namespace` and `migrate_acl` (wherever those are called at server startup — verify in `server/app.py` or CLI `start` command).
- **Releasable**: existing pre-B5 databases gain the three new columns on next startup; schema is forward-compatible.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_migrate_centroid_sum_adds_columns` — create a store with a `_META_TABLE` lacking `centroid_sum_json`, call `migrate_centroid_sum`, verify schema now includes the column.
  - Unit: `test_migrate_centroid_sum_idempotent` — call `migrate_centroid_sum` twice; second call is a no-op (no exception, schema unchanged).
  - Unit: `test_migrate_centroid_sum_no_meta_table_noop` — call `migrate_centroid_sum` on a store with no `_META_TABLE`; returns without error.
  - Integration (`@pytest.mark.integration`): `test_old_schema_upsert_preserves_new_columns` — after migration (3 new columns added to an existing table with data), simulate an older-binary write by calling `table.delete` + `table.add` with a row dict containing only the 10 original columns (no B5 columns); verify the B5 columns on OTHER existing rows are NOT nulled. This test's actual result determines the BREAKING.md forward-compatibility claim: pass = "old-binary upserts are safe"; fail = "mixed-version deployment will corrupt incremental state".
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "migrate_centroid_sum"` and `uv run pytest -m integration tests/test_store.py -v -k "old_schema_upsert"`

---

### Phase 2 — Store-layer unlocked internal helpers
> **Releasable**: after Task 2.4 — the unlocked `_do_*` helpers exist, `update_collection_meta` is now the locked public write path, and a CI guard prevents accidental unlocked-helper misuse. No external behaviour change yet; these are private building blocks for Phases 3–4.

#### Task 2.0 — Make `update_collection_meta` lock-acquiring
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 1.2
- **Description**:
  - Add per-collection lock acquisition to `update_collection_meta` (`store.py:449–498`): acquire `_lock_for(collection)` with `asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)`, raise `StoreBusyError` on timeout.
  - After acquiring the lock, perform the existing delete-then-insert upsert. Release on exit.
  - **Why**: after B5 the codebase has two meta-write paths — `update_collection_meta` (public) and `_do_write_meta_unlocked` (private, in-lock). The `_unlocked` suffix signals "call only while lock held." Making the public method lock-acquiring makes the naming convention true: all public store methods that write meta are locked; all `_do_*_unlocked` methods are not. **Note**: this makes individual writes atomic (no two writers hold the lock simultaneously), but does NOT eliminate the TOCTOU race in `recompute_collection_meta`'s read-compute-write cycle. `recompute_collection_meta` reads vectors OUTSIDE the lock, then acquires the lock only for the write via `update_collection_meta`. A concurrent `ingest_chunks` that completes between the read and write will have its incremental update clobbered by the recompute's whole-row upsert. This is a known limitation (see Known Limitations section) — the next incremental add self-corrects, and recompute is an authoritative full-scan so its values are correct as of the read time.
  - **`recompute_collection_meta` caller**: currently calls `update_collection_meta` outside any lock scope. After this task, `update_collection_meta` acquires the lock itself — `recompute_collection_meta` requires no changes.
  - **REST collection-creation caller** (`routes_collections.py:170`): calls `update_collection_meta` to create initial meta rows. After this task, this call correctly acquires the lock — no changes needed at the call site.
  - **Do not** call `update_collection_meta` from within any lock scope (it now acquires the lock itself). Audit all call sites and verify none hold `_lock_for(collection)` already. Update any such call to use `_do_write_meta_unlocked` instead.
- **Releasable**: `update_collection_meta` is the locked public write path; naming convention is consistent with `_do_*_unlocked` helpers.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_update_collection_meta_acquires_lock` — mock `_lock_for`; verify lock is acquired on every `update_collection_meta` call.
  - Unit: `test_update_collection_meta_timeout_raises_store_busy` — lock held externally; `update_collection_meta` raises `StoreBusyError` after timeout.
  - Unit: `test_update_collection_meta_no_call_while_lock_held` — verify no existing code path calls `update_collection_meta` while `_lock_for(collection)` is already held (static or integration check).
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "update_collection_meta"`

#### Task 2.1 — `_do_read_meta_unlocked` and `_do_write_meta_unlocked`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 1.2
- **Description**:
  - `async def _do_read_meta_unlocked(self, db, collection: str, namespace: str = DEFAULT_NAMESPACE) -> "CollectionMeta | None"`: reads the meta row for `collection` from an already-open `db` connection. Must NOT acquire `_lock_for(collection)`. Identical logic to the inner body of `get_collection_meta` (open `_META_TABLE`, filter by name + namespace, call `_row_to_meta`). Returns `None` if `_META_TABLE` absent or no matching row.
  - `async def _do_write_meta_unlocked(self, db, collection: str, meta: "CollectionMeta") -> None`: writes (upserts) the meta row for `collection` using the same delete-then-insert pattern as `update_collection_meta`, but without acquiring the lock. Must NOT call `update_collection_meta` (which would deadlock by re-acquiring the lock). Reuse the raw `table.delete` / `table.add` pattern directly.
    - **Lazy-create**: before the delete-then-insert, check `list_tables()` for `_META_TABLE`. If absent, create it via `create_table(self._meta_schema())` first. This replicates the lazy-create guard from `update_collection_meta` inline.
    - **Full schema**: the row dict passed to `table.add()` must include ALL columns in `_meta_schema()` — the 10 existing columns plus the 3 new B5 columns (`centroid_sum_json`, `mutations_since_recompute`, `needs_recompute`). Omitting the new columns will cause LanceDB to insert NULL values or raise a schema error.
  - Both helpers assume the caller holds `_lock_for(collection)`. Add a comment asserting this precondition; do not add a runtime assertion (non-reentrant lock cannot be safely inspected in asyncio).
  - These methods are private (`_do_` prefix); they are not part of the public `SearchStore` API.
- **Releasable**: helpers are callable from within the lock scope in Phases 3 and 4.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_do_read_meta_unlocked_returns_none_when_no_meta_table` — store with no meta table returns `None`.
  - Unit: `test_do_read_meta_unlocked_returns_existing_meta` — after `update_collection_meta`, `_do_read_meta_unlocked` returns the same meta.
  - Unit: `test_do_write_meta_unlocked_creates_row` — call `_do_write_meta_unlocked` on a fresh store (with `_META_TABLE` created via schema); row is retrievable via `get_collection_meta`.
  - Unit: `test_do_write_meta_unlocked_upserts_existing_row` — write twice with different `chunk_count`; second read reflects the second value.
  - Unit: `test_do_write_meta_unlocked_creates_meta_table_if_absent` — call `_do_write_meta_unlocked` on a db with no `_META_TABLE`; assert table is created and row is retrievable via `get_collection_meta`.
  - Unit: `test_do_write_meta_unlocked_includes_b5_columns` — write a meta row; assert `centroid_sum_json`, `mutations_since_recompute`, and `needs_recompute` are present and correctly encoded.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "do_read_meta_unlocked or do_write_meta_unlocked"`

#### Task 2.2 — `_do_fetch_doc_vectors_unlocked`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - `async def _do_fetch_doc_vectors_unlocked(self, db, collection: str, doc_id: str) -> list[list[float]]`: fetches all stored vectors for `doc_id` from the chunk table of `collection`. Selects `["vector", "doc_id"]` only (no full row materialisation). Returns an empty list if the table does not exist or the doc has no rows. Uses `_where_eq("doc_id", doc_id)` for the filter. Does NOT acquire any lock.
  - `doc_id` is already validated by `_DOC_ID_RE` at the `delete_document` call-site before this helper is invoked; add a defensive check inside the helper and raise `ValueError` if the pattern does not match.
  - Returns `list[list[float]]` — one entry per chunk row for that doc.
- **Releasable**: delete path in Phase 4 can fetch vectors before row removal.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_do_fetch_doc_vectors_unlocked_empty_when_no_table` — returns `[]` when collection table absent.
  - Unit: `test_do_fetch_doc_vectors_unlocked_returns_correct_vectors` — ingest two chunks for doc A, one chunk for doc B; fetch returns exactly the two vectors for doc A.
  - Unit: `test_do_fetch_doc_vectors_unlocked_invalid_doc_id_raises` — malformed doc_id raises `ValueError`.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "do_fetch_doc_vectors"`

#### Task 2.3 — NaN/inf guard and `_centroid_sum_valid` helper
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - `def _centroid_sum_valid(centroid_sum: list[float] | None, embedding_dim: int, stored_model: str, writer_model: str) -> bool`: returns `True` iff all of the following hold: `centroid_sum is not None`, `len(centroid_sum) == embedding_dim`, `stored_model == writer_model`, and no element of `centroid_sum` is NaN or inf. Pure function; no I/O.
  - `def _batch_vectors_valid(vectors: list[list[float]]) -> bool`: returns `True` iff every element of every vector in `vectors` is finite (no NaN, no inf). Pure function.
  - Both helpers use `math.isfinite` (stdlib, no numpy dependency).
  - If `_centroid_sum_valid` returns `False`, the store layer must trigger a lazy reseed (call `recompute_collection_meta` — but that lives on the pipeline; see the signal mechanism in Task 3.1). For invalid input vectors, log a warning and skip the offending vector's contribution (or trigger reseed — Task 3.1 decides via the `needs_recompute` flag).
- **Releasable**: validation helpers are available for Phase 3 tasks.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_centroid_sum_valid_true_for_good_sum` — valid sum, matching model and dim returns `True`.
  - Unit: `test_centroid_sum_valid_false_for_none` — `None` centroid_sum returns `False`.
  - Unit: `test_centroid_sum_valid_false_for_dim_mismatch` — sum length ≠ embedding_dim returns `False`.
  - Unit: `test_centroid_sum_valid_false_for_model_mismatch` — `stored_model != writer_model` returns `False`.
  - Unit: `test_centroid_sum_valid_false_for_nan_element` — sum with a NaN element returns `False`.
  - Unit: `test_centroid_sum_valid_false_for_inf_element` — sum with an inf element returns `False`.
  - Unit: `test_batch_vectors_valid_true_for_clean_batch` — all-finite vectors returns `True`.
  - Unit: `test_batch_vectors_valid_false_for_nan_in_vector` — any NaN returns `False`.
  - Unit: `test_centroid_sum_valid_false_for_empty_stored_model` — `stored_model=""` (pre-B5 default), `writer_model="BAAI/bge-small-en-v1.5"`; asserts returns `False`. Documents the upgrade path: every pre-B5 collection triggers one lazy reseed on first post-B5 ingest.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "centroid_sum_valid or batch_vectors_valid"`

#### Task 2.4 — CI guard: no `_do_*_unlocked` call from non-`_do_*` methods
- [ ] **File**: `tests/test_no_unlocked_direct_call.py`
- **Depends on**: Task 2.3
- **Description**:
  - Add a new test file (following the pattern of `tests/test_no_fstring_sql.py`) that AST-scans `archon_search/store.py` and asserts: no method whose name does **not** start with `_do_` makes a direct call to a method whose name ends with `_unlocked`. Implement using Python's `ast` module — parse `store.py`, walk `Call` nodes, filter for `Attribute` calls whose `attr` ends with `_unlocked`, and assert the enclosing function name starts with `_do_`.
  - This guards against a future contributor inadvertently calling `_do_write_meta_unlocked` from a public method without holding the per-collection lock, introducing a data race that test coverage would not catch.
  - Guard rule: no public method (name not starting with `_do_`) may call a `*_unlocked` helper UNLESS it is explicitly listed as a lock-acquiring exception. Allowed exceptions:
    - `update_collection_meta` — acquires `_lock_for(collection)` before delegating to `_do_write_meta_unlocked`
    - `delete_document` — acquires `_lock_for(collection)` before calling `_do_fetch_doc_vectors_unlocked` and `_do_subtract_meta_on_delete`
    - `update_description` — introduced in Task 5.1; acquires `_lock_for(collection)` then calls `_do_read_meta_unlocked` and `_do_write_meta_unlocked`
- **Releasable**: CI fails if an `_unlocked` helper is misused.
- **Tests (TDD)** — `tests/test_no_unlocked_direct_call.py`:
  - Unit: `test_no_public_method_calls_unlocked_helper` — AST scan passes on the current `store.py`; no violation found.
  - Unit: `test_no_unlocked_call_violation` — synthesize a method body `def bad(self): self._do_write_meta_unlocked(...)` in a scratch module, run the AST guard against it, assert it detects the violation (negative test — verifies the guard catches real violations).
  - Checkpoint: `uv run pytest tests/test_no_unlocked_direct_call.py -v`

#### Task 2.5 — `elementwise_sum` pure helper
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - `def elementwise_sum(vectors: list[list[float]]) -> list[float]`: module-level pure function in `store.py`.
  - If `vectors` is empty, return `[]` immediately (do NOT raise — callers rely on this guarantee).
  - Otherwise: `return [sum(v[i] for v in vectors) for i in range(len(vectors[0]))]`.
  - No imports beyond stdlib. No side-effects.
  - **Releasable**: after this task, `elementwise_sum` is importable by Task 4.1 and Task 6.1.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_elementwise_sum_correct` — two 3-dim vectors; asserts correct element-wise sum.
  - Unit: `test_elementwise_sum_single_vector` — single vector; returns same values.
  - Unit: `test_elementwise_sum_empty_list` — `elementwise_sum([])` returns `[]` (no IndexError).
  - Checkpoint: `uv run pytest tests/test_store.py -k "elementwise_sum" -v`

---

### Phase 3 — Store-layer incremental add path
> **Releasable**: ⚠️ **Phases 3–5 must ship atomically and are NOT independently releasable.** Between Phase 3 and Phase 5, the pipeline's `update_collection_meta` call at `pipeline.py:319–331` overwrites the meta row — including `centroid_sum_json` — with batch-only values after every `ingest_directory`, clobbering the incremental sum the store just wrote. Shipping Phase 3 without Phase 5 leaves the system in a state worse than pre-B5: the store writes a correct `centroid_sum` and the pipeline immediately nullifies it. Ship Phases 3, 4, and 5 together in a single release. Default pytest run must pass throughout each phase.

#### Task 3.1 — `_do_update_meta_on_add` helper and threshold config
- [ ] **Files**: `archon_search/store.py`, `archon_search/config.py`
- **Depends on**: Task 2.3
- **Description**:
  - Add to `config.py` → `SearchConfig`: `centroid_recompute_threshold: int = 10_000`. Load from TOML section `[database]` key `centroid_recompute_threshold`. The loader already reads `[database]` keys; add this alongside them.
  - Validation: in `load_config()` (`config.py`), after parsing the TOML, add an explicit check: `if config.centroid_recompute_threshold < 1: raise ConfigError("centroid_recompute_threshold must be >= 1")`. This prevents division-by-zero and infinite recompute loops.
  - `_do_update_meta_on_add(self, db, collection: str, batch_vectors: list[list[float]], distinct_doc_count: int, embedding_model: str | None, embedding_dim: int, threshold: int, namespace: str = DEFAULT_NAMESPACE) -> bool`: called while lock is held; reads meta via `_do_read_meta_unlocked(db, collection, namespace=namespace)`, validates sum, accumulates, writes meta via `_do_write_meta_unlocked`, returns `bool` — the raw `needs_recompute` signal. One-field dataclasses for internal returns are overengineered; `ingest_chunks` wraps the bool into `ChunkIngestResult`.
  - If `embedding_model is None` (caller did not supply a model name), skip centroid maintenance entirely and return `False` — do not trigger O(n) reseed. Once the pipeline supplies `self._embedder.model_name`, the guard fires correctly.
  - When `_do_read_meta_unlocked` returns `None` (brand-new collection with no meta row): skip `_centroid_sum_valid` entirely — the batch IS the full collection; no recompute is needed. Accumulate from (zero-vector seed, chunk_count=0, doc_count=0) and write the result. Do NOT set `needs_recompute=True` for this case.
  - Only call `_centroid_sum_valid` when an existing meta row is present (`_do_read_meta_unlocked` returns non-None).
  - If `_centroid_sum_valid` returns `False` for the stored sum, emit `logger.warning("Collection %r centroid stale, recompute queued", collection)` and return `True` (the caller — pipeline — will invoke `recompute_collection_meta`). Do NOT write a batch-only partial `centroid_sum` to meta (a partial sum is worse than no centroid — it produces a confidently wrong centroid for any concurrent router read). Instead: leave `centroid` and `centroid_sum` as `None` in the meta write, set `needs_recompute=True`, and emit `logger.warning("Collection %r centroid stale, recompute queued", collection)`. The pipeline will call `recompute_collection_meta` which computes the authoritative values.
  - If `_batch_vectors_valid` returns `False` for the incoming batch: emit `logger.warning("Collection %r batch vectors contain NaN/inf; skipping centroid maintenance", collection)`, set `needs_recompute=True`, do NOT write any `centroid_sum` update (leave centroid and centroid_sum as they are in meta), and return `True`. **No partial accumulation** — an invalid batch is treated the same as an invalid stored sum: leave current state, queue recompute.
- **Releasable**: config key is loadable; `_do_update_meta_on_add` (returns `bool`) is wirable in Task 3.2.
- **Tests (TDD)** — `tests/test_store.py`, `tests/test_config.py`:
  - Unit: `test_centroid_recompute_threshold_default` — `SearchConfig()` has `centroid_recompute_threshold == 10_000`.
  - Unit: `test_centroid_recompute_threshold_loaded_from_toml` — TOML with `centroid_recompute_threshold = 500` produces `SearchConfig.centroid_recompute_threshold == 500`.
  - Unit: `test_do_update_meta_on_add_accumulates_from_zero` — empty meta (zero seed) → add 3 vectors; returned meta has `chunk_count=3`, `centroid_sum` == elementwise sum.
  - Unit: `test_do_update_meta_on_add_accumulates_onto_existing` — pre-seeded meta with `centroid_sum=[1.0]` and `chunk_count=1`; add 1 vector `[3.0]`; result `centroid_sum=[4.0]`, `chunk_count=2`.
  - Unit: `test_do_update_meta_on_add_signals_recompute_on_invalid_sum` — stored meta has NaN centroid_sum; call `_do_update_meta_on_add`; assert return value is `True` AND assert that the meta row now has `centroid_sum=None` and `centroid=None` (not batch-only partial values). Verifies the plan's "leave as None, queue recompute" behavior.
  - Unit: `test_do_update_meta_on_add_signals_recompute_at_threshold` — `mutations_since_recompute` reaches `threshold`; signal is `True`.
  - Unit: `test_do_update_meta_on_add_nan_batch_vector_triggers_recompute` — a NaN element in an input vector; returns `True`.
  - Unit: `test_do_update_meta_on_add_none_model_skips_maintenance` — `embedding_model=None`; meta unchanged, returns `False`.
  - Unit: `test_centroid_recompute_threshold_validation` — `load_config()` with `centroid_recompute_threshold = 0` raises `ConfigError`.
  - Checkpoint: `uv run pytest tests/test_store.py tests/test_config.py -v -k "do_update_meta_on_add or centroid_recompute_threshold"`

#### Task 3.2 — Wire `_do_update_meta_on_add` into `ingest_chunks`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 3.1
- **Description**:
  - Inside `ingest_chunks`, after `_do_ingest` commits the chunk rows and while the lock is still held:
    - Compute `batch_vectors = [list(c.vector) for c in chunks]` and `distinct_doc_ids = len({c.doc_id for c in chunks})`.
    - Guard against empty batch: if `batch_vectors` is empty, skip meta maintenance and return `ChunkIngestResult(chunks_ingested=0, needs_recompute=False)`.
    - Call `signal = await self._do_update_meta_on_add(db, collection, batch_vectors, distinct_doc_ids, embedding_model=<caller-supplied-or-from-meta>, embedding_dim=len(batch_vectors[0]), threshold=<from store init or constant>)`.
    - Store `signal` for the caller to retrieve (see Task 3.3).
  - The `_locked_by_caller=True` path (caller already holds the lock — used by background task ingest jobs, though **no production caller currently passes this flag**: the only caller is `tests/test_store_lock.py`): `ingest_chunks` must be refactored to share a common post-`_do_ingest` block for meta maintenance regardless of lock source. The early `return await self._do_ingest(...)` in the `_locked_by_caller=True` branch (store.py:521-523) must be restructured: call `_do_ingest` unconditionally, then call `_do_update_meta_on_add` unconditionally (lock is already held for both paths), then release the lock only if it was acquired internally. This ensures the `_locked_by_caller=True` path also executes meta maintenance and returns a `ChunkIngestResult` (not a bare `int`).
  - Brand-new collection bootstrap: if `_do_read_meta_unlocked` returns `None` (no meta row and possibly no `_META_TABLE`), treat as `(centroid_sum=None, chunk_count=0, doc_count=0)` seed — `_do_update_meta_on_add` already handles this via the `None` → zero path. `_META_TABLE` creation is handled by `_do_write_meta_unlocked` inline — it checks `list_tables()` and calls `create_table(schema)` if the table does not exist, replicating the lazy-create guard without calling `update_collection_meta` (which would deadlock by re-acquiring the lock). `_do_write_meta_unlocked` must NOT call `update_collection_meta`.
  - The `embedding_model` string: obtain it from the existing meta row if present; the caller supplies it via a new keyword argument `embedding_model: str | None = None` added to `ingest_chunks` (the pipeline already knows `self._embedder.model_name`).
  - `ingest_chunks` public signature change: add `embedding_model: str | None = None` keyword argument. When `None`, `_do_update_meta_on_add` skips centroid maintenance entirely — no O(n) reseed. This prevents an accidental full-rescan between Phase 3 (store wiring) and Phase 5 (pipeline passes the real model name). Pipeline callers in Task 5.2 will pass `self._embedder.model_name`.
- **Releasable**: `ingest_chunks` maintains the running sum on every add; default pytest run passes.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_ingest_chunks_accumulates_centroid_sum_on_second_batch` — ingest batch B1 then B2; `get_collection_meta` has `centroid_sum` == elementwise sum of all B1+B2 vectors.
  - Unit: `test_ingest_chunks_bootstrap_creates_meta_row` — ingest into collection with no prior meta; `get_collection_meta` returns non-None with correct `chunk_count`.
  - Unit: `test_ingest_chunks_doc_count_multi_doc_batch` — single `ingest_chunks` call with 3 distinct `doc_id`s; `doc_count == 3`.
  - Unit: `test_ingest_chunks_no_full_scan_spy` — spy / mock `get_all_vectors` and `count_documents`; after two `ingest_chunks` calls, neither is called.
  - Unit: `test_ingest_chunks_lock_serializes_concurrent_adds` — two concurrent `ingest_chunks` coroutines for the same collection; use **asymmetric** vectors (e.g. batch A: `[[1.0, 2.0]]`, batch B: `[[3.0, 5.0]]`) so a lost-update produces a provably wrong sum; assert both `centroid_sum == [4.0, 7.0]` **and** `chunk_count == 2` (a lost-update with symmetric vectors can accidentally satisfy the sum check but not the count).
  - Unit: `test_ingest_chunks_no_lock_re_entry` — the lock is acquired exactly once per `ingest_chunks` call (mock `_lock_for` to count acquisitions).
  - Integration: `test_ingest_chunks_locked_by_caller_accumulates_meta` — pre-acquire lock via `_lock_for(collection)`, call `ingest_chunks(..., _locked_by_caller=True, embedding_model="BAAI/bge-small-en-v1.5")`; assert return is `ChunkIngestResult` (not int); assert `centroid_sum` is updated in meta; assert lock is still held after the call (not released by `ingest_chunks`).
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "ingest_chunks"`

#### Task 3.3 — Surface `needs_recompute` signal to callers via `ChunkIngestResult`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 3.2
- **Description**:
  - Change `ingest_chunks` return type from `int` (chunk count) to a new `@dataclass class ChunkIngestResult: chunks_ingested: int; needs_recompute: bool`. The name `ChunkIngestResult` avoids colliding with `_types.IngestResult(doc_id, chunks_created, status)` which already exists in the codebase.
  - Update all callers of `store.ingest_chunks` to unpack `result.chunks_ingested` instead of the bare `int`. Verified callers: only `pipeline.py:226` (`ingest_file`) calls `store.ingest_chunks` in production code. `server/routes_collections.py` does **not** call `store.ingest_chunks` — it calls `update_collection_meta` to create collection rows. Also update any test that asserts on the integer return value of `ingest_chunks`.
  - The `needs_recompute` flag propagates to the pipeline in Task 5.2.
- **Releasable**: all callers handle the new return type; `needs_recompute` is surfaced and ready for pipeline wiring.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_ingest_chunks_returns_chunk_ingest_result` — `ingest_chunks` returns a `ChunkIngestResult` with `.chunks_ingested` and `.needs_recompute` attributes.
  - Unit: `test_ingest_chunks_chunks_ingested_correct` — value matches the number of chunks written.
  - Unit: `test_ingest_chunks_needs_recompute_false_below_threshold` — fresh collection, 3 chunks, threshold 10 000; `needs_recompute == False`.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "ingest_chunks_returns or chunk_ingest_result or chunks_ingested or needs_recompute_false"`

---

### Phase 4 — Store-layer incremental delete path
> **Releasable**: ⚠️ NOT independently releasable — see Phase 3 atomic-shipping constraint. Phase 4 is internally consistent after Task 4.2 but MUST ship together with Phases 3 and 5 as a single atomic change.

#### Task 4.1 — `_do_subtract_meta_on_delete`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 3.1, Task 2.5
- **Description**:
  - `async def _do_subtract_meta_on_delete(self, db, collection: str, del_vectors: list[list[float]], namespace: str = DEFAULT_NAMESPACE) -> None`: called while lock is held. Reads meta via `_do_read_meta_unlocked(db, collection, namespace=namespace)`. Derives `embedding_model` and `embedding_dim` from the read meta row — the helper does not accept them as parameters (the stored meta is the authoritative source). If `del_vectors` is empty, returns immediately (no-op — doc was already absent).
  - If `_do_read_meta_unlocked` returns `None` (no meta row — e.g., `ensure_collection` was called but ingest never ran): emit `logger.warning("Collection %r has no meta row; delete cannot update centroid", collection)` and return immediately (no-op — no row to update, centroid state is already absent).
  - Subtracts `del_sum = elementwise_sum(del_vectors)` from `meta.centroid_sum`; sets `chunk_count` to `max(0, chunk_count - len(del_vectors))` — floor at zero to guard against underflow from inconsistent state. **Note**: `chunk_count' = max(0, chunk_count - len(del_vectors))`, not `chunk_count -= len(del_vectors)`; sets `doc_count` to `max(0, doc_count - 1)` — i.e., decrement by 1 floored at 0 (guards against underflow if meta is in an inconsistent state from a previous crash or pre-B5 migration). **Note**: this is `doc_count' = max(0, doc_count - 1)`, not `doc_count -= max(0, doc_count - 1)`.
  - If resulting `chunk_count == 0`: sets `centroid_sum = None`, `centroid = None`, `doc_count = 0`.
  - Otherwise: derives `centroid = centroid_sum' / chunk_count'`.
  - Always bumps `mutations_since_recompute` by `len(del_vectors)`.
  - Always sets `last_indexed = datetime.now(UTC)` (delete-only path still refreshes the timestamp).
  - Validates stored sum via `_centroid_sum_valid`; if invalid (NaN, inf, wrong dimension, wrong model, or None): sets `centroid_sum = None` and `centroid = None` on the meta row, sets `needs_recompute=True`, emits `logger.warning("Collection %r centroid stale, recompute queued", collection)`, and **skips the subtraction** (do NOT subtract from an invalid/None sum — the result would be garbage). `_do_write_meta_unlocked` writes the cleared state. Returns without error.
  - Persists via `_do_write_meta_unlocked`.
- **Releasable**: subtraction helper available for wiring in Task 4.2.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_do_subtract_meta_decrements_chunk_count` — meta with `chunk_count=5`, subtract 2 vectors; `chunk_count==3`.
  - Unit: `test_do_subtract_meta_resets_on_last_doc` — `chunk_count=2`, subtract 2; `centroid_sum=None`, `centroid=None`, `chunk_count=0`.
  - Unit: `test_do_subtract_meta_noop_on_empty_vectors` — `del_vectors=[]`; meta unchanged.
  - Unit: `test_do_subtract_meta_bumps_last_indexed` — `last_indexed` is updated to a recent datetime.
  - Unit: `test_do_subtract_meta_sets_needs_recompute_on_invalid_sum` — stored sum has NaN; assert `centroid_sum=None`, `centroid=None`, `needs_recompute=True` in written meta (not NaN propagated through subtraction).
  - Unit: `test_do_subtract_meta_doc_count_floor_at_zero` — meta with `doc_count=0`, subtract 2 vectors; `doc_count` stays at `0` (no negative value stored).
  - Unit: `test_do_subtract_meta_noop_when_meta_none` — `del_vectors` non-empty but no meta row exists; assert function returns without raising and no meta row is created.
  - Unit: `test_do_subtract_meta_chunk_count_floor_at_zero` — meta with `chunk_count=1`, subtract 3 vectors; `chunk_count` stays at `0` (no negative value stored).
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "do_subtract_meta"`

#### Task 4.2 — Vector-aware `delete_document` with lock
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 4.1, Task 2.2
- **Description**:
  - In `delete_document`, add `namespace: str = DEFAULT_NAMESPACE` to the method signature (if not already present). Acquire `_lock_for(collection)` with `asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)`, raising `StoreBusyError` on timeout — mirroring `ingest_chunks` exactly. The call to `_do_subtract_meta_on_delete` must pass `namespace=namespace`.
  - While holding the lock:
    1. Open table; if absent, release lock and return 0.
    2. Call `del_vectors = await self._do_fetch_doc_vectors_unlocked(db, collection, doc_id)`.
    3. Count rows via `await table.count_rows(_where_eq("doc_id", doc_id))`.
    4. If count == 0: release lock and return 0.
    5. Delete rows via `await table.delete(_where_eq("doc_id", doc_id))`.
    6. Call `await self._do_subtract_meta_on_delete(db, collection, del_vectors, namespace=namespace)`.
  - `_do_subtract_meta_on_delete` reads the meta row itself to derive `embedding_model` and `embedding_dim` — no need to pass them from the caller.
  - Return type remains `int` (unchanged public interface).
  - **`StoreBusyError` caller impact**: `pipeline.ingest_file` calls `store.delete_document` at `pipeline.py:228` to purge old chunks before re-ingesting. Before B5, this was lock-free and always succeeded. After B5, it can raise `StoreBusyError` if `reindex_metadata` holds the lock. Wrap this call in `ingest_file` as: `try: await self.store.delete_document(...) except StoreBusyError: return IngestResult(doc_id=doc_id, chunks_created=0, status="error")`. Also update the MCP `delete_document` handler (`mcp.py`) to map `StoreBusyError` to `code: "store_busy"` rather than `code: "internal_error"`.
- **Releasable**: `delete_document` is correct — centroid is updated on every delete; lock prevents concurrent lost updates; default pytest run passes.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_delete_document_subtracts_vectors` — ingest 3 chunks for doc A and 2 for doc B; delete doc A; `centroid_sum` equals the sum of doc B's vectors only.
  - Unit: `test_delete_document_last_document_resets_centroid` — single-doc collection; delete it; `centroid_sum=None`, `centroid=None`, `chunk_count=0`.
  - Unit: `test_delete_document_bumps_last_indexed` — `last_indexed` changes after delete.
  - Unit: `test_delete_document_returns_zero_for_missing_doc` — deleting a non-existent doc returns 0 and does not mutate meta.
  - Unit: `test_delete_document_lock_timeout_raises_store_busy_error` — lock is held externally; `delete_document` raises `StoreBusyError` after timeout.
  - Unit: `test_ingest_file_returns_error_on_delete_store_busy` — mock `store.delete_document` to raise `StoreBusyError`; call `ingest_file`; assert return is `IngestResult(status="error")` (no uncaught exception propagates, no chunks ingested).
  - Unit: `test_delete_document_no_full_scan_spy` — spy `get_all_vectors` and `count_documents`; `delete_document` calls neither.
  - Unit: `test_concurrent_ingest_and_delete_serializes_correctly` — run `ingest_chunks` (batch `[[1.0, 2.0], [3.0, 4.0]]`, doc A) and `delete_document` (doc A) concurrently; verify final state is consistent: either both vectors are in (delete lost the race) with correct sum, or neither is in with `centroid_sum=None` — never a half-subtracted state.
  - Integration (`@pytest.mark.integration`): `test_delete_then_verify_centroid` — real LanceDB, ingest two docs, delete one, verify centroid matches the mean of the remaining doc's vectors within tolerance.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "delete_document"`

---

### Phase 5 — Pipeline layer: description routing and `needs_recompute` wiring
> **Releasable**: after Task 5.2 — `ingest_directory` no longer writes sum/count/centroid; `update_description` is the sole description writer; the pipeline checks the `needs_recompute` signal and calls `recompute_collection_meta` when needed.

#### Task 5.1 — `store.update_description` partial-write method
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - `async def update_description(self, collection: str, description: str | None, last_described: "datetime | None", described_at_doc_count: "int | None", last_indexed: "datetime | None", namespace: str = DEFAULT_NAMESPACE) -> None`:
    - Acquires `_lock_for(collection)` via `asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)`. On timeout, log a warning (`logger.warning("Collection %r lock timeout in update_description, skipping", collection)`) and return without writing — caller keeps the old description. Safe fallback: description is cosmetic, not routing-critical. This prevents the description-generation sync path from stalling indefinitely behind a long-running `reindex_metadata`.
    - Reads meta via `_do_read_meta_unlocked(db, collection, namespace=namespace)`.
    - If meta is `None`, returns immediately (no-op — the meta row is created by the first `ingest_chunks`).
    - Writes a new `CollectionMeta` that copies all fields from the existing meta but overrides only `description`, `last_described`, `described_at_doc_count`, and `last_indexed`.
    - Persists via `_do_write_meta_unlocked`.
    - Releases lock.
  - This method is the only writer of description/timestamp fields from the pipeline going forward. `centroid_sum`, `chunk_count`, `doc_count`, `centroid`, `mutations_since_recompute`, `needs_recompute`, and `embedding_model` are never touched by this method.
- **Releasable**: pipeline can use `update_description` safely without clobbering the store-maintained fields.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_update_description_writes_description_field` — call `update_description` after an `ingest_chunks`; `get_collection_meta` returns the new description.
  - Unit: `test_update_description_does_not_touch_centroid_sum` — call `ingest_chunks` then `update_description`; `centroid_sum` and `chunk_count` are unchanged.
  - Unit: `test_update_description_noop_when_no_meta_row` — call `update_description` with no prior meta row; no exception, `get_collection_meta` still returns `None`.
  - Unit: `test_update_description_timeout_skips_write` — lock held externally; `update_description` returns without writing (no `StoreBusyError`) and logs a warning.
  - Unit: `test_update_description_concurrent_with_ingest` — run `ingest_chunks` and `update_description` concurrently for the same collection; final `centroid_sum` is correct and `description` matches the `update_description` value (neither clobbered the other).
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "update_description"`

#### Task 5.2 — Refactor `ingest_directory` to use `update_description` and check `needs_recompute`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 5.1, Task 3.3
- **Description**:
  - Pass `embedding_model=self._embedder.model_name` to every `store.ingest_chunks` call (via `ingest_file`). Update `ingest_file` to accept and forward an `embedding_model: str` keyword argument to `store.ingest_chunks`.
  - Also add `namespace: str = DEFAULT_NAMESPACE` to `ingest_file`'s signature. `ingest_directory` already has `namespace` and must pass it to `ingest_file` in the per-file loop call at `pipeline.py:282`. Forward `namespace` to both `store.delete_document(collection, doc_id, namespace=namespace)` and `store.ingest_chunks(collection, records, ..., namespace=namespace)`, and to `recompute_collection_meta(collection, namespace=namespace)`.
  - Remove the block in `ingest_directory` that builds `CollectionMeta` and calls `await self.store.update_collection_meta(meta)` (`pipeline.py:319–331`).
  - Retain the description-regeneration logic (`_should_regenerate`, `generate_description`, `all_chunks`). After the per-file loop, inside the existing `if all_vectors:` guard, call `await self.store.update_description(collection, description, last_described, described_at_doc_count, last_indexed=datetime.now(UTC))` instead of `update_collection_meta`.
  - **Wire `needs_recompute` in `ingest_file`** (not only in `ingest_directory`): `ingest_file` must check `ChunkIngestResult.needs_recompute` from `store.ingest_chunks` and, if `True`, immediately call `await self.recompute_collection_meta(collection, namespace=namespace)`. This ensures the pre-B5 seed fires on single-file REST `/ingest` calls that go through `ingest_file` directly (bypassing `ingest_directory`). `ingest_directory` also checks the aggregate signal as a secondary gate; the two calls are idempotent.
  - **Performance note**: when `needs_recompute=True` fires mid-loop inside `ingest_directory`, `recompute_collection_meta` performs an O(chunks-in-collection) full scan. This is intentional: it establishes a correct `centroid_sum` base; subsequent incremental adds by remaining files in the loop are additive on top of the accurate base. The mid-loop cost is bounded by the `centroid_recompute_threshold` (default 10,000 chunks). `ingest_directory` also checks the aggregate signal after the loop; if `ingest_file` already fired a mid-loop recompute, that recompute resets `needs_recompute=False` and `mutations_since_recompute=0`, so `recompute_collection_meta` short-circuits O(1) on the second call (Task 6.1 specifies this short-circuit).
  - Collect the aggregate `needs_recompute` signal in `ingest_directory` as well: after the per-file loop, if any file's result carries `needs_recompute=True`, call `await self.recompute_collection_meta(collection, namespace=namespace)`. (May be a no-op if `ingest_file` already fired it; `recompute_collection_meta` resets the flag.)
  - Update `IngestResult` dataclass — **this class lives in `archon_search/_types.py`**, not `pipeline.py`; update `_types.py` — to add `needs_recompute: bool = False`. Audit all consumers of `_types.IngestResult` (server routes, MCP handler, tests) after this change to ensure none break. This field is internal to the pipeline loop and must not appear in any REST/MCP response schema.
  - The `_vector_collector` (`all_vectors`) is still needed for description regeneration and for the `if all_vectors:` guard; it does not drive the centroid write.
- **Releasable**: `ingest_directory` is correct — cumulative centroid is maintained by the store; description is written atomically by `update_description`; periodic-checkpoint recompute fires when needed.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_ingest_directory_calls_update_description_not_update_collection_meta` — spy both methods; after `ingest_directory`, `update_description` was called and `update_collection_meta` was not called directly by the pipeline.
  - Unit: `test_ingest_directory_triggers_recompute_on_needs_recompute_signal` — configure `centroid_recompute_threshold=1`; after ingest, `recompute_collection_meta` is called.
  - Unit: `test_ingest_directory_no_recompute_below_threshold` — `centroid_recompute_threshold=10_000`; after small ingest, `recompute_collection_meta` is NOT called.
  - Integration (`@pytest.mark.integration`): `test_multi_batch_ingest_centroid_correctness` — real LanceDB + real embedder; ingest batch B1, then B2; `get_collection_meta` centroid equals the mean of all B1∪B2 vectors within 1e-5 tolerance; `doc_count` and `chunk_count` are cumulative.
  - Integration (`@pytest.mark.integration`): `test_reingest_changed_document_net_zero` — ingest a doc, re-ingest it with different content; centroid matches only the new content's vectors.
  - Unit: `test_ingest_file_triggers_recompute_on_needs_recompute_signal` — configure `centroid_recompute_threshold=1`; single `ingest_file` call (not via `ingest_directory`) triggers `recompute_collection_meta` directly inside `ingest_file`.
  - Unit: `test_pre_b5_meta_row_seeds_on_first_ingest_file` — create a meta row manually with `centroid_sum_json=""` (simulating a pre-B5 migration default); call `ingest_file` for one document; after the call, `get_collection_meta` returns a non-None `centroid_sum`. (This tests the store+pipeline seed path without the sync/watcher machinery.)
  - Unit: `test_ingest_result_needs_recompute_not_in_rest_response` — verify that `IngestResult.needs_recompute` is not serialised into any REST or MCP response by checking that the relevant Pydantic response schemas do not include this field.
  - Unit: `test_ingest_file_forwards_namespace_to_store` — mock `store.ingest_chunks` to capture kwargs; call `ingest_file(..., namespace="ns1")`; assert `namespace="ns1"` was forwarded.
  - **Existing test update required**: `test_ingest_centroid_replaced_on_reingest` in `test_pipeline.py:663` currently verifies the defective batch-overwrite behavior (Defect 1). After Task 5.2, update this test to assert that the centroid after re-ingest reflects the authoritative centroid for the collection (via the incremental delete-then-add path), not a batch-only overwrite value.
  - Integration: `test_ingest_directory_double_recompute_idempotency` — ingest 3 files with threshold set to 2 chunks so `needs_recompute=True` fires mid-loop; verify final `centroid_sum` equals the authoritative sum of all 3 files' vectors (not just the last batch). Asserts correctness despite two `recompute_collection_meta` calls.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "ingest_directory or ingest_file"` and `uv run pytest -m integration tests/ -v -k "multi_batch or reingest"`

---

### Phase 6 — `recompute_collection_meta` extension and sync hot-path removal
> **Releasable**: after Task 6.2 — watcher-sync no longer triggers a full rescan; `recompute_collection_meta` correctly writes `centroid_sum` and resets counters; drift guard integration test passes.

#### Task 6.1 — Extend `recompute_collection_meta` to populate `centroid_sum`
- [ ] **Files**: `archon_search/store.py`, `archon_search/pipeline.py`
- **Depends on**: Task 1.2, Task 2.5, Task 5.2
- **Description**:
  - **Dependency note**: Task 5.2 must be complete before Task 6.1 removes `_compute_centroid`, because Task 5.2 removes the `_compute_centroid` call site at `pipeline.py:304` inside `ingest_directory`. Removing `_compute_centroid` before Task 5.2 would break `ingest_directory`.
  - Imports `elementwise_sum` from `store.py` (defined in Task 2.5) — do NOT redefine here.
  - Import `elementwise_sum` from `store` at the top of `pipeline.py` for use in `recompute_collection_meta`.
  - In `recompute_collection_meta` (`pipeline.py:490`), after the existing `_compute_centroid(vectors)` call:
    - Compute `centroid_sum = elementwise_sum(vectors)` (same `vectors` list — one pass only; do not call `get_all_vectors` again).
    - Set `meta.centroid_sum = centroid_sum`, `meta.mutations_since_recompute = 0`, `meta.needs_recompute = False`.
  - Guard: if `not vectors`, return early (already present at `pipeline.py:499`). In the early-return path also set `centroid_sum=None` to reset any stale sum.
  - This is the **only** O(chunks) path. It must not be called on the hot path after this task.
  - `_compute_centroid` in `pipeline.py` is superseded by `centroid = [x / chunk_count for x in centroid_sum]` after this task. Remove `_compute_centroid` and replace its call site in `recompute_collection_meta` with the inline formula. If any other caller depends on `_compute_centroid`, they must be updated too (audit via `grep _compute_centroid`).
  - **Short-circuit guard**: at the start of `recompute_collection_meta`, read the current meta row via `store.get_collection_meta`. If meta is not None AND `meta.needs_recompute == False` AND `meta.mutations_since_recompute == 0`: return early (no-op). This prevents a second O(n) scan when `ingest_directory`'s aggregate signal check fires after `ingest_file` already triggered and completed a mid-loop recompute.
- **Releasable**: `recompute_collection_meta` is now the authoritative drift-reset that also seeds `centroid_sum` for the incremental path.
- **Tests (TDD)** — `tests/test_store.py` (for `elementwise_sum`), `tests/test_pipeline.py` (for `recompute_collection_meta`):
  - Unit (`test_store.py`): `test_elementwise_sum_correct` — `elementwise_sum([[1,2],[3,4]]) == [4,6]`.
  - Unit (`test_store.py`): `test_elementwise_sum_single_vector` — single vector returns a copy of it.
  - Unit: `test_recompute_writes_centroid_sum` — after `recompute_collection_meta`, `get_collection_meta` has `centroid_sum == elementwise_sum(all_vectors)`.
  - Unit: `test_recompute_resets_mutations_counter` — set `mutations_since_recompute=999`, call `recompute_collection_meta`; counter is 0.
  - Unit: `test_recompute_noop_on_empty_collection` — empty collection returns early; meta has `centroid_sum=None`.
  - Unit: `test_recompute_single_get_all_vectors_call` — spy `store.get_all_vectors`; called exactly once.
  - Unit: `test_recompute_collection_meta_no_op_when_not_needed` — after a fresh recompute (needs_recompute=False, mutations=0), call recompute again; assert `get_all_vectors` is NOT called (spy). Verifies the O(1) short-circuit on second call.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "elementwise_sum"` and `uv run pytest tests/test_pipeline.py -v -k "recompute"`

#### Task 6.2 — Remove `recompute_collection_meta` from watcher-sync hot path; add checkpoint wiring in sync
- [ ] **File**: `archon_search/sync.py`
- **Depends on**: Task 6.1, Task 5.2
- **Description**:
  - Remove lines `sync.py:695–703` (the `try: await self._pipeline.recompute_collection_meta(name)` block on the hot sync path).
  - After the `ingest_directory` call at `sync.py:517–527`, check the returned `IngestResult` list for any `needs_recompute=True` entry. If present, call `await self._pipeline.recompute_collection_meta(name)` (with the same `try/except BLE001` error wrapper that existed at the removed site). This moves the recompute to the checkpoint-signal path, not every sync.
  - **Incremental sync path drift-reset**: the incremental sync path (sync.py:620–700) calls `ingest_file` directly per changed file and `delete_by_source_path` per deleted file. Neither returns a `needs_recompute` signal (delete return type stays `int`). After the incremental sync loop completes, read the meta row for the collection via `await self._pipeline.store.get_collection_meta(name)` and check `meta.needs_recompute`. If `True`, call `await self._pipeline.recompute_collection_meta(name)` (within the same `try/except BLE001` wrapper at the existing sync.py:696 site that you are modifying). This restores the drift-reset mechanism that was previously provided by the unconditional `recompute_collection_meta` call.
  - Retain the log message "Recompute collection meta for %r after sync" but gate it behind the checkpoint condition so it only appears when a recompute actually fires.
- **Releasable**: watcher-sync hot path is O(batch); periodic recompute fires only when the threshold is crossed.
- **Tests (TDD)** — `tests/test_sync.py` (or new file `tests/test_sync_centroid.py`):
  - Unit: `test_sync_does_not_call_recompute_below_threshold` — mock `pipeline.recompute_collection_meta`; a sync cycle with a 3-file corpus and threshold 10 000 does not call recompute.
  - Unit: `test_sync_calls_recompute_when_signal_raised` — configure threshold=1; a sync cycle calls recompute exactly once.
  - Unit: `test_sync_incremental_path_no_full_scan` — spy `store.get_all_vectors`; a full sync cycle (no threshold breach) issues zero calls.
  - Integration (`@pytest.mark.integration`): `test_drift_guard` — ingest B1, record `centroid_sum`; call `recompute_collection_meta` on the same table state; compare incremental `centroid` to the recomputed `centroid` within 1e-5 tolerance.
  - Integration (`@pytest.mark.integration`): `test_pre_b5_collection_seeds_on_first_ingest` — create a meta row with no `centroid_sum_json`, ingest one file; `get_collection_meta` has a non-None `centroid_sum` after.
  - Checkpoint: `uv run pytest tests/test_sync.py -v` and `uv run pytest -m integration tests/ -v -k "drift_guard or pre_b5"`

---

### Phase 7 — Documentation and final verification
> **Releasable**: after Task 7.1 — all acceptance criteria pass; all affected docs updated; eval harness green.

#### Task 7.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover and update all documentation affected by B5:
    - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`: amend the `CON-4` row to describe the three real defects (batch-only overwrite, delete-ignores-centroid, O(chunks) sync-path rescan) and mark as resolved by B5. The row must no longer say "O(chunks) on every ingest". Clarify that delete is O(chunks-in-document), not O(batch).
    - `Documentation/Backlog/03_world_class_roadmap.md`: update the B5 line to say "incremental `(centroid_sum, chunk_count)` maintenance at store layer — three concrete defects fixed".
    - `Documentation/Architecture/130_data_architecture_and_persistence.md`: add a paragraph describing the `centroid_sum_json`, `mutations_since_recompute`, `needs_recompute` columns on `_archon_collection_meta`; state the `(sum, count)` invariant and that the derived `centroid = centroid_sum / chunk_count` is the routing artifact.
    - `Documentation/Architecture/210_performance_and_scalability.md`: add a note that ingest centroid maintenance is O(batch) as of B5; delete is O(chunks-in-document); the O(chunks) path is reserved for explicit `recompute_collection_meta`.
    - `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md`: add a runbook entry titled **"Stale centroid — symptoms, causes, and recovery"** covering: (1) `logger.warning("Collection %r centroid stale, recompute queued", ...)` in logs means centroid drift has been detected; (2) causes — model switch, NaN/inf in vectors, crash between chunk-table write and meta-write, delete-only workload without subsequent ingest; (3) recovery — call `recompute_collection_meta` for the affected collection via the CLI or the MCP `reindex` tool; (4) delete-only workload note: if no ingest follows the threshold crossing, a manual recompute is required.
    - `BREAKING.md`: record the three new internal meta columns (`centroid_sum_json`, `mutations_since_recompute`, `needs_recompute`) as additive. For forward-compatibility: state the **verified** LanceDB behavior from the integration test `test_old_schema_upsert_preserves_new_columns` (acceptance criterion (m)) — either "old-binary upserts preserve new columns" (if the test passes) or "old-binary upserts will null out incremental state — do not run a mixed-version deployment" (if it fails). Do not claim forward-compatibility without this test result.
    - `archon-search.toml.example`: add `centroid_recompute_threshold = 10000` to the `[database]` section with a calibration comment (setting to 1 triggers a full recompute on every ingest; very high values defer drift reset indefinitely).
  - Run the integration test `test_old_schema_upsert_preserves_new_columns` (see acceptance criterion (m)) before writing the BREAKING.md entry, and use the actual result to determine whether a mixed-version deployment is safe.
  - Run the full default test suite and confirm it passes.
  - Run the eval harness and confirm routing metrics are unchanged.
  - Run the integration suite and confirm all B5 integration tests pass.
  - Do not update ADRs (no new ADR warranted for a correctness fix; the existing `04_multi_collection_router_with_centroid_preranking.md` may receive an addendum noting that the centroid is now maintained incrementally — include this only if the ADR describes the maintenance strategy).
- **Releasable**: B5 is fully verified; all docs reflect delivered implementation.
- **Acceptance criteria** (must all pass):
  - (a) Ingesting batch B2 into a collection already containing B1 yields `centroid` equal to the mean of all B1∪B2 vectors within 1e-5 tolerance. Verified by `test_multi_batch_ingest_centroid_correctness` (integration).
  - (b) `chunk_count` and `doc_count` after B1∪B2 reflect the cumulative collection, not the last batch. Verified by the same integration test.
  - (c) Deleting a document subtracts its vectors from `centroid_sum` and decrements `chunk_count`; deleting the last document resets `centroid_sum` and `centroid` to `None`. Verified by `test_delete_document_subtracts_vectors` and `test_delete_document_last_document_resets_centroid`.
  - (d) A second `ingest_chunks` into a large collection does not call `get_all_vectors` or `count_documents`. Verified by `test_ingest_chunks_no_full_scan_spy`.
  - (e) `recompute_collection_meta` over a given table state produces a `centroid` that matches the incremental-maintained value for that same table state within 1e-5 tolerance. Verified by `test_drift_guard` (integration).
  - (f) Eval harness (`uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`) passes with thresholds unchanged.
  - (g) Default pytest run passes the 85% coverage gate (`uv run pytest`).
  - (h) `CON-4` row in `530_technical_debt_refactoring_roadmap.md` and B5 line in `03_world_class_roadmap.md` describe the as-built behaviour (three concrete defects, O(batch) ingest, store-layer maintenance).
  - (i) `centroid_sum_json`, `mutations_since_recompute`, `needs_recompute` appear in `BREAKING.md` as additive internal-schema columns.
  - (j) `archon-search.toml.example` includes `centroid_recompute_threshold = 10000` in `[database]`.
  - (k) `logger.warning("Collection %r centroid stale, recompute queued", collection)` fires at every `needs_recompute=True` set site in the store layer (verified by the unit tests that assert log output on NaN/stale-sum paths).
  - (l) `160_operational_readiness_monitoring_and_reliability.md` contains a runbook entry for stale centroid symptoms, causes, and `recompute_collection_meta` recovery. The runbook must note that `grep 'centroid stale, recompute queued' ~/.archon-search/search-logs/` is the monitoring query for operators who do not have structured log tooling.
  - (m) Integration test `test_old_schema_upsert_preserves_new_columns` passes or explicitly documents the failure — simulates an old-binary `update_collection_meta` (using a schema without the three new columns) on a B5-migrated table and asserts whether the new columns retain their values. The BREAKING.md entry must reflect this test's actual outcome. (`tests/test_store.py`, `@pytest.mark.integration`)
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest` and `uv run pytest -m integration tests/`.

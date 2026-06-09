# C6 — Incremental FTS Maintenance
**Purpose**: Replace O(collection-size) full FTS rebuilds on every ingest/delete with O(delta-size) incremental updates via `table.optimize()`, so ingest latency scales with changed chunks, not total collection size.
**Audience**: archon-search contributors implementing C6; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

Every call to `ingest_file`, `ingest_directory`, `sync.py`'s watcher cycle, or `reindex_metadata` triggers `rebuild_fts_index()` — a full recreation of the FTS index over the entire collection. On a 50,000-chunk collection this means every single-document update pays O(collection-size) rebuild cost, making the watcher sync cycle progressively slower as collections grow.

`store.delete_document()` is the only mutation path that touches FTS at all: it does not. Deleted chunks remain searchable in FTS (phantom hits) until the next ingest triggers a rebuild.

LanceDB's incremental maintenance mechanism is `table.optimize()`: rows added or deleted after index creation are incorporated into the FTS index when `optimize()` is called, without rebuilding from scratch. The spike in Phase 1 must confirm this API is available and behaves as expected before any store changes are written.

The full design rationale and decision log are in `Documentation/Backlog/C6-incremental-fts-maintenance-brief.md`.

---

## Goal

After C6 ships: `ingest_file` on a 1,000+ chunk collection completes in milliseconds (O(delta-size)), not seconds (O(collection-size)). Deleted documents no longer produce phantom hits in FTS. The four existing `rebuild_fts_index()` call sites in `pipeline.py`, `sync.py`, and `store.py` are replaced with incremental `optimize_fts()` calls. The missing `language=` parameter bug in `sync.py:719` is fixed as a side-effect of the refactor. `reindex_metadata`'s unnecessary FTS rebuild (metadata-only updates do not modify the `text` column) is removed.

---

## Scope

### In Scope
- Phase 1 spike to confirm `table.optimize()` API availability and semantics (gates a–f).
- `store.optimize_fts(collection: str) -> None` — new store method wrapping `table.optimize()`.
- `store.delete_document(..., skip_fts_optimize: bool = False)` — adds FTS maintenance on delete; `skip_fts_optimize=True` suppresses it when the caller (e.g., `ingest_file`) will optimize separately after the re-add.
- Replace `rebuild_fts_index()` at `pipeline.py:345` (ingest_file) with `store.optimize_fts()`.
- Replace `rebuild_fts_index()` at `pipeline.py:422` (ingest_directory) with `store.optimize_fts()`.
- Replace `rebuild_fts_index()` at `sync.py:719` with `store.optimize_fts()` (fixes the missing `language=` pre-existing bug as a side-effect — optimize carries no language parameter, so the bug is eliminated, not patched).
- Remove `rebuild_fts_index()` from `store.py:1402` (`reindex_metadata`) — metadata-only updates do not modify the `text` column; the FTS index is unaffected; the call is unnecessary and is removed entirely.
- `archon_search/eval/runner.py:550` — **Retained**: this call site performs a fresh corpus setup for the eval harness (all files ingested with `rebuild_fts=False`, then one `rebuild_fts_index` at the end for a clean consistent starting state). It is NOT a production ingest path. It intentionally retains `rebuild_fts_index` to ensure a deterministic baseline before eval queries run. No change needed.
- Plan B (if spike gate (c) fails): `delete_document` calls `rebuild_fts_index(language=dominant_lang)` instead of `optimize_fts()` on the delete path; AND `ingest_file`'s batch-end operation calls `rebuild_fts_index(collection, language=dominant_lang)` instead of `optimize_fts(collection)` to prevent phantom hits from deleted chunks. Add path incremental add is the same; only the batch-end differs. See Plan B description in Architecture section.
- Ingest latency p95 regression guard in the eval harness (large-corpus fixture, ≥1,000 chunks).

### Out of Scope
- Automatic background FTS compaction — deferred to C6.1.
- Lightweight FTS-only rebuild endpoint (bypassing re-embed) — deferred to C6.1.
- Migration of pre-C6 FTS indexes — existing indexes are valid; no migration needed.
- Changes to search ranking or FTS query logic.
- `reindex_collection` operator endpoint (it triggers full re-ingest, not FTS-only; untouched).

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task F.1 — Final verification & documentation update].

---

## What does NOT change
- `rebuild_fts_index()` remains available for operator-initiated FTS repair (not called from ingest paths, but still exposed).
- `ingest_file` and `ingest_directory` public signatures — the `rebuild_fts` parameter is retained; its semantics shift from "rebuild" to "optimize" internally.
- `store.delete_document()` external callers passing no `skip_fts_optimize` argument — default `False` preserves existing call sites; no callers need updating unless they are the specific ingest paths in scope.
- `store.delete_by_source_path(collection, source_path, namespace, *, skip_fts_optimize: bool = False)` — delegates to `delete_document`; inherits FTS maintenance by default. Batch callers (sync.py delete loop) pass `skip_fts_optimize=True` to suppress per-file FTS; only the batch-end optimize/rebuild runs.
- LanceDB schema, `SearchResult` shape, API contracts, telemetry.
- Eval thresholds — no threshold values lowered; one new latency threshold added.

---

## Known limitations / accepted trade-offs
- **Plan B delete cost**: if spike gate (c) fails, the delete path falls back to a full `rebuild_fts_index()` — O(collection-size) per delete. This is accepted: phantom hits after delete silently corrupt search results, which is worse than slower delete. Documented as a known limitation in the operator guide.
- **BM25 score drift**: after N incremental `optimize()` calls, BM25 scores may differ numerically from a freshly rebuilt index. Equivalence is defined as result-set membership (same doc_ids), NOT rank-order identity — BM25 scores after N incremental optimize() calls may differ numerically from a fresh rebuild (see also Task F.1 acceptance criteria). Operators managing strict score reproducibility should use `rebuild_fts_index()` periodically.
- **Plan B ingest cost**: under Plan B, `ingest_file` always calls `rebuild_fts_index` at batch end (O(N) cost, where N = collection size) to prevent phantom hits from deleted chunks. This is unavoidable when `table.optimize()` does not remove deleted rows from FTS.
- **Tokenizer drift on growing multilingual collections**: if a collection's dominant language shifts over time, the FTS tokenizer won't update until an explicit `rebuild_fts_index()`. Accepted and documented.
- **Sync path optimize granularity**: `optimize_fts()` is called once per sync cycle (not per file), matching the current batch-end pattern. Unindexed rows added mid-cycle are invisible to FTS until cycle end — accepted, same as the current rebuild-at-end behavior.
- **reindex_metadata FTS removal**: removing the `rebuild_fts_index()` call from `reindex_metadata` is safe because `reindex_metadata` only updates non-text metadata columns (`file_type`, `updated_at`, `ingested_by`). The `text` column — the only FTS-indexed column — is never modified by this path.
- **Post-lock-release FTS window**: `optimize_fts` (and `rebuild_fts_index`) in `delete_document` is called AFTER the per-collection lock is released. Between lock release and optimize completion, a concurrent coroutine can acquire the lock and begin writing new chunks. Those new chunks are incorporated by the in-flight `optimize` call only if they complete before optimize finishes. Chunks added after optimize starts are not indexed until the next FTS maintenance call. This matches the existing `rebuild_fts_index` convention and is accepted: the alternative (holding the lock during optimize) would block all writes for the duration.

---

## Architecture

### New / modified units

**`store.optimize_fts(collection: str) -> None`** (new method, `store.py`)
- Opens the collection table and calls `await table.optimize()`.
- No language parameter — the tokenizer was set when the index was created and is not reconfigured by optimize.
- If spike gate (c) fails (Plan B activate): delete path uses `rebuild_fts_index(collection, language=await self.get_dominant_language(collection))` instead; `optimize_fts` is still introduced for the add path.
- **Lock scope**: `optimize_fts` does NOT acquire the per-collection lock. It is the caller's responsibility to manage concurrency. In `delete_document`, `optimize_fts` is called AFTER `lock.release()` to avoid holding the lock during a potentially long optimize operation. In `ingest_file`, it is also called without the lock. This matches the existing `rebuild_fts_index` convention.

**`store.delete_document(collection, doc_id, namespace, *, skip_fts_optimize: bool = False) -> int`** (modified, `store.py:1554`)
- After the `table.delete()` call and AFTER `lock.release()`: if `skip_fts_optimize=False`, call `optimize_fts(collection)` (or `rebuild_fts_index` under Plan B). The FTS call is deliberately placed after lock release to avoid holding the lock during a potentially long operation.
- `skip_fts_optimize=True` is passed by `ingest_file` to suppress FTS on the delete-before-reingest step; `ingest_file` calls `optimize_fts` (Plan A) or `rebuild_fts_index` (Plan B) separately after the add.
- **Plan B (if spike gate (c) fails)**: both `delete_document` (standalone) AND `ingest_file`'s batch-end call `rebuild_fts_index(collection, language=dominant_lang)` instead of `optimize_fts`. This is necessary to prevent phantom hits — without deleted-row cleanup via optimize, any incremental FTS call would leave old chunks searchable.

**`pipeline.ingest_file` (modified, `pipeline.py:343–345`)**
- Pass `skip_fts_optimize=True` to `store.delete_document()` (line 331).
- **Plan A** (gate (c) passes): Replace `store.rebuild_fts_index(collection, language=dominant_lang)` (line 344–345) with `await self.store.optimize_fts(collection)` inside the `if rebuild_fts:` block.
- **Plan B** (gate (c) fails): the `if rebuild_fts:` block must call `await self.store.rebuild_fts_index(collection, language=dominant_lang)` instead of `optimize_fts`. This prevents phantom hits from deleted chunks that `optimize_fts` would not remove under Plan B. The Plan B flag must be checked at the call site (e.g., a `store.supports_incremental_fts_delete` property or equivalent) to select the right path.
- **Error recovery**: the `optimize_fts(collection)` call (Plan A) must be wrapped in `try/except`; on exception, fall back to `rebuild_fts_index(collection, language=dominant_lang)` (which creates a fresh consistent index from the current vector store state) and log a warning. This prevents ingest from silently leaving FTS in an inconsistent state if optimize fails.

**`pipeline.ingest_directory` (modified, `pipeline.py:419–422`)**
- At batch end, branch on `supports_incremental_fts_delete`: Plan A calls `await self.store.optimize_fts(collection)`; Plan B calls `await self.store.rebuild_fts_index(collection, language=dominant_lang)`. See Task 3.2.
- Individual `ingest_file(rebuild_fts=False)` calls pass `skip_fts_optimize=True` to `delete_document` (after Task 3.1 is applied) — one batch-end call runs, never N per-file calls.

**`sync.py:718–719`** (modified)
- At batch end, branch on `supports_incremental_fts_delete`: Plan A calls `await self._pipeline.store.optimize_fts(name)`; Plan B calls `await self._pipeline.store.rebuild_fts_index(name, language=dominant_lang)`. See Task 3.3.
- Missing `language=` pre-existing bug is eliminated under Plan A (optimize carries no language parameter); under Plan B, language is explicitly fetched via `get_dominant_language`.

**`store.reindex_metadata` (modified, `store.py:1400–1406`)**
- Remove the `rebuild_fts_index()` call entirely. Metadata-only updates do not change the `text` column; FTS index is unaffected.

### Data flow (add path)
```
ingest_file → store.delete_document(skip_fts_optimize=True)  [delete old chunks, no FTS touch]
           → store.ingest_chunks(...)                          [write new chunks, unindexed]
           → store.optimize_fts(collection)                    [incorporate new + remove old]
```

### Data flow (delete path)
```
delete_document(skip_fts_optimize=False)  → table.delete()
                                          → optimize_fts()    [Plan A: gate(c) passes]
                                          OR rebuild_fts_index() [Plan B: gate(c) fails]
```

---

## Task breakdown

### Phase 1 — Spike: Verify `table.optimize()` semantics
> **Releasable**: after this phase, with written spike findings. If Plan C triggers (gates (a) or (b) fail), C6 is deferred and this phase is the only deliverable.

#### Task 1.1 — Spike script: verify `table.optimize()` gates (a)–(f)
- [x] **File**: `spike_optimize_fts.py` (project root; discarded after phase; not committed to main)
- **Depends on**: nothing
- **Description**:
  - Write a standalone async Python script that creates a temporary LanceDB table with an FTS index on a `text` column, then systematically tests each gate:
    - **(a) API availability**: call `await table.optimize()` on the async LanceDB table API used in `store.py`; assert no `AttributeError` or `NotImplementedError`.
    - **(b) New-row indexing**: insert rows after `create_index`, call `optimize()`, then query via FTS and assert newly inserted rows appear in results.
    - **(c) Deleted-row cleanup**: delete a row via `table.delete()`, call `optimize()`, then query via FTS and assert the deleted row does NOT appear.
    - **(d) Concurrent safety**: launch 3 concurrent `optimize()` calls on the same table (using `asyncio.gather`); assert no exception is raised and the table remains queryable.
    - **(e) Compatibility**: run `create_index(..., replace=True)` then add a new row, then call `optimize()`; assert the new row is searchable.
    - **(f) Update-row indexing**: update the `text` value of an existing row via `table.update()`, call `optimize()`, then query the new text and assert it appears.
  - Document results in `Documentation/Backlog/C6-spike-findings.md` with a pass/fail table and the LanceDB version used.
  - **Go/no-go gate**: if (a) or (b) fail → record Plan C outcome (deferred), stop here; if (c) fails → record Plan B outcome (delete path uses `rebuild_fts_index`); proceed with Phase 2 in all passing outcomes.
- **Releasable**: after this task, the implementation path (Plan A, B, or C) is determined and documented.
- **Tests (TDD)** — `tests/test_fts_spike_gates.py`:
  - **All tests in `test_fts_spike_gates.py` must be marked `@pytest.mark.integration`** — they exercise real LanceDB disk I/O and must not run in the default (`uv run pytest`) suite.
  - Integration: `test_optimize_fts_incorporates_new_rows` — inserts a row after index creation, calls optimize, asserts FTS query returns the new row.
  - Integration: `test_optimize_fts_removes_deleted_rows` — inserts, indexes, deletes a row, calls optimize, asserts FTS query returns zero results for the deleted row's unique text.
  - Integration: `test_optimize_fts_is_idempotent` — calling optimize twice does not corrupt the index.
  - Integration: `test_optimize_fts_after_replace_true_index` — index created with `replace=True`, new row added, optimize called, row searchable.
  - Checkpoint: `uv run pytest -m integration tests/test_fts_spike_gates.py -v --no-cov`

#### Task 1.2 — Record Plan B outcome as a runtime flag
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1 (spike findings must be documented)
- **Description**:
  - Add a module-level constant to `store.py`:
    ```python
    # Set to False if spike gate (c) failed (optimize() does not remove deleted rows from FTS).
    # This constant is set manually after the spike (Task 1.1) and committed as part of C6.
    FTS_OPTIMIZE_REMOVES_DELETED: bool = True  # Plan A; change to False if Plan B applies
    ```
  - Add a property on `VectorStore`:
    ```python
    @property
    def supports_incremental_fts_delete(self) -> bool:
        return FTS_OPTIMIZE_REMOVES_DELETED
    ```
  - This property is read by `ingest_file`, `ingest_directory`, and `sync.py` to branch between Plan A (`optimize_fts`) and Plan B (`rebuild_fts_index`) at their batch-end call sites.
  - **How to set it**: after the spike, if gate (c) fails, change `FTS_OPTIMIZE_REMOVES_DELETED = False` in `store.py` and commit. All call sites read from this constant automatically.
- **Releasable**: after this task, the Plan A/B flag is queryable by all call sites.
- **Tests (TDD)** — `tests/test_store_plan_b_flag.py`:
  - Unit: `test_supports_incremental_fts_delete_reflects_constant` — assert `store.supports_incremental_fts_delete == FTS_OPTIMIZE_REMOVES_DELETED`.
  - Checkpoint: `uv run pytest tests/test_store_plan_b_flag.py -v --no-cov`

---

### Phase 2 — Store layer: `optimize_fts()` + delete-path FTS hook
> **Releasable**: after Task 2.2. `store.optimize_fts()` is callable; `store.delete_document()` maintains FTS on delete.
> **Depends on**: Task 1.2 (Plan B flag must be in place)

#### Task 2.1 — `store.optimize_fts()` method
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1 (spike gates (a)+(b) must pass)
- **Description**:
  - Add `async def optimize_fts(self, collection: str) -> None` to `VectorStore`, in the `# FTS index` section after `rebuild_fts_index` (line ~1281).
  - Calls `self._validate_collection(collection)`, opens the table, and calls `await table.optimize()`.
  - No language parameter — the tokenizer is embedded in the index at creation time.
  - Logs at DEBUG level: `"optimize_fts: collection=%s"`.
  - **Lock scope**: `optimize_fts` does NOT acquire the per-collection lock. Callers are responsible for concurrency. In `delete_document`, `optimize_fts` is invoked AFTER `lock.release()` (see Task 2.2). In `ingest_file`, it is also called outside any lock. This matches the existing `rebuild_fts_index` convention.
  - **Plan B guard**: if spike gate (c) failed, this method is still introduced for the add path. The delete path in Task 2.2 will call `rebuild_fts_index` instead.
- **Releasable**: after this task, `store.optimize_fts(collection)` can be called by any caller.
- **Tests (TDD)** — `tests/test_store_optimize_fts.py`:
  - Unit: `test_optimize_fts_calls_table_optimize` — mock `table.optimize`; assert it is awaited once.
  - Unit: `test_optimize_fts_validates_collection` — unknown collection name raises `ValueError`.
  - Unit: `test_optimize_fts_requires_connected_store` — unconnected store raises appropriate error.
  - Integration: `test_optimize_fts_makes_new_chunks_searchable` — ingest chunks without FTS rebuild, call `optimize_fts`, assert hybrid_search finds new content.
  - Checkpoint: `uv run pytest tests/test_store_optimize_fts.py -v --no-cov`

#### Task 2.2 — FTS maintenance hook in `store.delete_document()`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add `skip_fts_optimize: bool = False` keyword-only parameter to `delete_document(self, collection, doc_id, namespace, *, skip_fts_optimize: bool = False) -> int`.
  - After `await table.delete(...)` (currently line 1577): release the lock first (`lock.release()`), THEN — if `count > 0` and `not skip_fts_optimize` — call `await self.optimize_fts(collection)` (Plan A) or `await self.rebuild_fts_index(collection, language=await self.get_dominant_language(collection))` (Plan B). The FTS call must be outside the lock to match the existing `rebuild_fts_index` convention and avoid holding the lock during a potentially long operation.
  - **Plan B** (if spike gate (c) failed): call `await self.rebuild_fts_index(collection, language=await self.get_dominant_language(collection))` instead of `optimize_fts`.
  - **Design decision**: expose `skip_fts_optimize: bool = False` on `delete_by_source_path` and pass it through to `delete_document`. This keeps `sync.py` using the existing `delete_by_source_path` API without bypassing it. Update `delete_by_source_path`'s signature in the same task.
  - Existing callers that pass no keyword argument get `skip_fts_optimize=False` (FTS maintained) — this is net-new correct behavior for the `delete_document` standalone path.
  - `pipeline.ingest_file`'s `delete_document` call (line 331) will be updated in Task 3.1 to pass `skip_fts_optimize=True`.
- **Releasable**: after this task, standalone `delete_document` calls maintain FTS coherence; no phantom hits after document deletion.
- **Tests (TDD)** — `tests/test_store_delete_fts.py`:
  - Unit: `test_delete_document_calls_optimize_by_default` — mock `optimize_fts`; assert it is called after `table.delete`.
  - Unit: `test_delete_document_skips_optimize_when_flag_set` — pass `skip_fts_optimize=True`; assert `optimize_fts` is NOT called.
  - Unit: `test_delete_document_skips_optimize_when_count_zero` — document not present; assert `optimize_fts` is NOT called.
  - Unit: `test_delete_document_optimize_called_after_lock_release` — verify ordering: mock `optimize_fts` with a `side_effect` that asserts `store._collection_locks[collection].locked() is False` at call time; assert the side_effect does not raise. This verifies the lock was released before `optimize_fts` was called.
  - Integration: `test_delete_document_removes_from_fts` — ingest, call `delete_document`, assert hybrid_search no longer returns the document.
  - Integration: `test_delete_by_source_path_also_removes_from_fts` — verify `delete_by_source_path` inherits FTS maintenance via delegation.
  - Checkpoint: `uv run pytest tests/test_store_delete_fts.py -v --no-cov`

**Plan B tests**: If spike gate (c) fails, add `tests/test_store_delete_fts_plan_b.py` with:
1. `test_delete_document_calls_rebuild_under_plan_b` — assert `rebuild_fts_index` is called instead of `optimize_fts`.
2. `test_ingest_file_calls_rebuild_under_plan_b` — assert `ingest_file` uses `rebuild_fts_index` at batch end when Plan B is active.
3. `test_get_dominant_language_called_on_plan_b_delete` — assert `get_dominant_language` is called to determine the language for the rebuild.

---

### Phase 3 — Replace `rebuild_fts_index` call sites
> **Releasable**: after this phase, all ingest paths use incremental FTS; the full rebuild is no longer called during normal ingest or sync.

#### Task 3.1 — `pipeline.ingest_file`: replace `rebuild_fts_index` with `optimize_fts`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.2
- **Description**:
  - At line 331: change `await self.store.delete_document(collection, doc_id, namespace=namespace)` to `await self.store.delete_document(collection, doc_id, namespace=namespace, skip_fts_optimize=True)`.
  - At lines 343–345: replace:
    ```python
    if rebuild_fts:
        dominant_lang = await self.store.get_dominant_language(collection)
        await self.store.rebuild_fts_index(collection, language=dominant_lang)
    ```
    with (Plan A):
    ```python
    if rebuild_fts:
        try:
            await self.store.optimize_fts(collection)
        except Exception:
            dominant_lang = await self.store.get_dominant_language(collection)
            logger.warning("optimize_fts failed; falling back to rebuild_fts_index")
            await self.store.rebuild_fts_index(collection, language=dominant_lang)
    ```
    OR with (Plan B, if `store.supports_incremental_fts_delete` is False):
    ```python
    if rebuild_fts:
        dominant_lang = await self.store.get_dominant_language(collection)
        await self.store.rebuild_fts_index(collection, language=dominant_lang)
    ```
    The `if rebuild_fts:` block must branch on the Plan B flag: if Plan B is active, call `rebuild_fts_index` at batch end instead of `optimize_fts`. This is the mechanism that prevents phantom hits when `optimize()` cannot remove deleted rows.
  - **Double-failure**: if both `optimize_fts` and the `rebuild_fts_index` fallback fail (e.g., disk full or LanceDB corruption), the exception propagates to the caller. The ingest result may report `status="ok"` for the persist step while FTS is inconsistent. Operator remediation: run `rebuild_fts_index` manually via the `/collections/{name}/reindex` endpoint or the CLI. Document this scenario in the operator guide (Task F.1).
  - Remove the `dominant_lang` local variable from the Plan A path (no longer needed at the top level of this call site).
  - The `rebuild_fts: bool = True` parameter on `ingest_file` is retained; its behavior is now "call optimize_fts (or rebuild under Plan B) at end of ingest" rather than "rebuild full FTS index unconditionally".
- **Releasable**: after this task, single-file ingest is incremental (Plan A) or consistently rebuild-based (Plan B).
- **Tests (TDD)** — `tests/test_pipeline_ingest_fts.py`:
  - Unit: `test_ingest_file_calls_optimize_fts_not_rebuild` — mock `store.optimize_fts` and `store.rebuild_fts_index`; assert optimize is called, rebuild is not.
  - Unit: `test_ingest_file_passes_skip_fts_optimize_to_delete` — mock `store.delete_document`; assert it receives `skip_fts_optimize=True`.
  - Unit: `test_ingest_file_rebuild_fts_false_skips_optimize` — pass `rebuild_fts=False`; assert neither method is called.
  - Unit: `test_ingest_file_fallback_to_rebuild_on_optimize_failure` — mock `optimize_fts` to raise an exception; assert `rebuild_fts_index` is called as fallback and a warning is logged.
  - Integration: `test_ingest_file_new_content_searchable_after_optimize` — ingest a file, assert FTS search returns the new content.
  - Integration: `test_reingest_file_old_content_not_searchable` — ingest a file, re-ingest with changed content, assert old content no longer returned by FTS.
  - Checkpoint: `uv run pytest tests/test_pipeline_ingest_fts.py -v --no-cov`

#### Task 3.2 — `pipeline.ingest_directory`: replace `rebuild_fts_index` with `optimize_fts`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.1
- **Description**:
  - At lines 419–422: replace:
    ```python
    if any(r.status == "ok" for r in results):
        dominant_lang = await self.store.get_dominant_language(collection)
        await self.store.rebuild_fts_index(collection, language=dominant_lang)
    ```
    with:
    ```python
    if any(r.status == "ok" for r in results):
        if self.store.supports_incremental_fts_delete:
            await self.store.optimize_fts(collection)
        else:
            dominant_lang = await self.store.get_dominant_language(collection)
            await self.store.rebuild_fts_index(collection, language=dominant_lang)
    ```
  - Individual per-file `ingest_file(rebuild_fts=False, ...)` calls will pass `skip_fts_optimize=True` to `delete_document` after Task 3.1 is applied — this dependency is the reason Task 3.2 depends on Task 3.1.
  - One `optimize_fts` (or `rebuild_fts_index` under Plan B) call at batch end; no per-file optimize overhead.
- **Releasable**: after this task, directory batch ingest is incremental.
- **Tests (TDD)** — `tests/test_pipeline_ingest_directory_fts.py`:
  - Unit: `test_ingest_directory_calls_optimize_once` — mock `store.optimize_fts`; ingest 3 files; assert optimize called exactly once (not N times).
  - Unit: `test_ingest_directory_no_optimize_when_all_fail` — all files fail; assert optimize not called.
  - Unit: `test_ingest_directory_calls_rebuild_under_plan_b` — set `supports_incremental_fts_delete=False`; assert `rebuild_fts_index` is called instead of `optimize_fts`.
  - Integration: `test_ingest_directory_all_files_searchable_after_optimize` — ingest a directory, assert FTS returns chunks from all files.
  - Checkpoint: `uv run pytest tests/test_pipeline_ingest_directory_fts.py -v --no-cov`

#### Task 3.3 — `sync.py:719`: replace `rebuild_fts_index` with `optimize_fts`
- [ ] **File**: `archon_search/sync.py`
- **Depends on**: Task 2.1, Task 2.2
- **Description**:
  - At line 718–719: replace:
    ```python
    # Rebuild FTS once after all file operations
    await self._pipeline.store.rebuild_fts_index(name)
    ```
    with:
    ```python
    if self._pipeline.store.supports_incremental_fts_delete:
        await self._pipeline.store.optimize_fts(name)
    else:
        dominant_lang = await self._pipeline.store.get_dominant_language(name)
        await self._pipeline.store.rebuild_fts_index(name, language=dominant_lang)
    ```
  - The missing `language=` pre-existing bug (see brief §In Scope) is eliminated as a side-effect: `optimize_fts` carries no language parameter.
  - Update the comment to: `# Optimize FTS once after all file operations`.
  - **Sync delete loop**: the sync path's file-deletion loop must pass `skip_fts_optimize=True` when calling `delete_document` (or `delete_by_source_path`), so that per-file `optimize_fts` is suppressed during the loop. Only the batch-end `optimize_fts` at line 719 runs. Without this, a sync cycle with 100 deletes would trigger 101 optimize calls (one per delete + one batch-end). Task 2.2 exposed `skip_fts_optimize: bool = False` on `delete_by_source_path`. Call `store.delete_by_source_path(..., skip_fts_optimize=True)` from the sync.py delete loop to suppress per-file FTS; only the batch-end call at line 719 runs.
- **Releasable**: after this task, the watcher sync cycle is incremental with no N+1 optimize calls.
- **Tests (TDD)** — `tests/test_sync_fts.py`:
  - Unit: `test_sync_cycle_calls_optimize_fts_not_rebuild` — mock `store.optimize_fts` and `store.rebuild_fts_index`; trigger a sync cycle; assert optimize called, rebuild not called.
  - Unit: `test_sync_cycle_calls_optimize_once_per_collection` — sync cycle with 2 collections; assert optimize called once per collection.
  - Unit: `test_sync_delete_path_does_not_call_optimize_per_file` — mock `optimize_fts`; simulate sync cycle with 3 deleted files; assert `optimize_fts` is called exactly once (batch-end), not 4 times.
  - Unit: `test_sync_batch_end_calls_rebuild_under_plan_b` — set `supports_incremental_fts_delete=False`; trigger sync; assert `rebuild_fts_index` called at batch end.
  - Integration: `test_sync_cycle_adds_searchable_after_optimize` — simulate watcher add; assert FTS returns new content after sync.
  - Integration: `test_sync_cycle_delete_no_phantom_hits` — simulate watcher delete; assert FTS returns zero results for deleted content after sync.
  - Checkpoint: `uv run pytest tests/test_sync_fts.py -v --no-cov`

---

### Phase 4 — Remove unnecessary FTS rebuild from `reindex_metadata`
> **Releasable**: after Task 4.1. `reindex_metadata` no longer triggers any FTS operation.

#### Task 4.1 — Remove `rebuild_fts_index` from `store.reindex_metadata`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - At lines 1400–1406: remove the entire `if updates:` block that calls `rebuild_fts_index`:
    ```python
    if updates:
        try:
            await self.rebuild_fts_index(collection)
        except Exception:  # noqa: BLE001
            logger.warning(
                "rebuild_fts_index after reindex failed", exc_info=True
            )
    ```
  - Rationale: `reindex_metadata` only writes to `file_type`, `updated_at`, and `ingested_by` columns. The `text` column (the only FTS-indexed column) is never modified. Rebuilding FTS here is unnecessary and wasteful.
  - No replacement: the FTS index does not need to change when metadata fields change.
- **Releasable**: after this task, `reindex_metadata` no longer incurs FTS rebuild cost.
- **Tests (TDD)** — `tests/test_store_reindex_metadata_fts.py`:
  - Unit: `test_reindex_metadata_does_not_call_rebuild_fts` — mock `rebuild_fts_index` and `optimize_fts`; run `reindex_metadata`; assert neither is called.
  - Integration: `test_reindex_metadata_fts_still_valid_after_metadata_update` — ingest chunks, update metadata via `reindex_metadata`, assert FTS search still returns the chunks correctly.
  - Checkpoint: `uv run pytest tests/test_store_reindex_metadata_fts.py -v --no-cov`

---

### Phase 5 — Eval: ingest latency p95 regression guard
> **Releasable**: after Task 5.1. CI eval run includes a latency regression test distinguishing O(delta) from O(collection) behavior.

#### Task 5.1 — Ingest latency p95 threshold in eval harness
- [ ] **File**: `tests/eval/test_eval_suite.py`, `tests/eval/thresholds.toml`
- **Depends on**: Task 3.1
- **Description**:
  - Add a large-corpus ingest latency test that ingests 1,000 synthetic chunks into a collection, then times a single additional `ingest_file` call (1 document, ~5–10 chunks) and asserts p95 wall-clock time is below a threshold.
  - The corpus must be large enough to distinguish O(collection-size) from O(delta-size): a pre-C6 `rebuild_fts_index` on 1,000 chunks takes materially longer than a post-C6 `optimize_fts` on 5–10 new chunks.
  - Add to `thresholds.toml`:
    ```toml
    [ingest_latency]
    # CALIBRATION REQUIRED: The value below is a placeholder.
    # Before merging, measure `ingest_file` p95 on the CI runner (or a comparable
    # environment) using the 1,000-chunk corpus fixture, then set:
    #   single_file_p95_ms = ceil(measured_p95_ms * 1.5)  [hard cap: 2000]
    # Record the measured baseline in a comment here (e.g., "# baseline: 210ms on GH Actions ubuntu-latest").
    # Use the same calibration approach as the existing search thresholds in thresholds.toml.
    single_file_p95_ms = 500   # placeholder — replace with calibrated value before merge
    ```
  - Read the existing `thresholds.toml` format (`tests/eval/README.md`) before adding entries; match the existing TOML structure.
  - Per the brief: decide whether to promote this to a hard CI gate (currently all latency thresholds are report-only per `thresholds.toml` line 7). **Recommendation**: promote to a hard gate — C6's correctness guarantee is meaningless if a regression silently reverts to O(collection-size). Record this decision in the brief's Open Questions section.
  - Use the deterministic eval backends from `archon_search/eval/backends.py` for the large corpus fixture; do not require real model weights.
- **Releasable**: after this task, the eval harness catches O(collection-size) latency regressions.
- **Tests (TDD)** — `tests/eval/test_eval_suite.py` (new test within existing suite, `@pytest.mark.eval`):
  - Eval: `test_ingest_latency_p95_single_file_on_large_corpus` — builds a 1,000-chunk collection, times 5 repeated `ingest_file` calls (different docs each), asserts p95 ≤ threshold from `thresholds.toml`.
  - Checkpoint: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py::test_ingest_latency_p95_single_file_on_large_corpus -v`

---

### Final Phase — Verification & Documentation

#### Task F.1 — Final verification & documentation update
- [ ] **Files**: documentation updates (see Description); `tests/test_fts_consistency_after_50_operations.py` (new integration test file, see Tests below)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, Architecture docs, UserManual, BREAKING.md, roadmap, brief) and update every file whose content is affected by the changes delivered in this plan. Files to check include at minimum:
    - `Documentation/Architecture/130_data_architecture_and_persistence.md` — FTS maintenance model changed
    - `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` — ingest latency behavior changed
    - `Documentation/Architecture/210_performance_and_scalability.md` — O(delta) vs O(collection) behavior documented
    - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` — C6 removed from tech debt register if listed
    - `Documentation/roadmap.md` — C6 marked complete
    - `Documentation/Backlog/C6-incremental-fts-maintenance-brief.md` — Open Questions resolved, spike findings referenced
    - `Documentation/UserManual/` — operator guidance on `rebuild_fts_index` for manual compaction, Plan B delete behavior (if applies)
  - Do NOT update `BREAKING.md` — this is an internal ingest latency improvement; no public API contract changes.
  - Run the full default test suite and the eval suite; confirm all pass.
  - Verify acceptance criteria below are all met before marking complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - **After incremental add**: `hybrid_search` returns chunks from a newly ingested document without a full FTS rebuild having been called.
  - **After incremental delete**: `hybrid_search` returns zero results for text that existed only in deleted chunks; no phantom hits.
  - **After re-ingest**: `hybrid_search` returns only the new content; old content from the same document is absent.
  - **After N operations**: verified by automated test `test_fts_consistency_after_50_operations` (see Tests below) — same doc_id sets (set equality, not rank-order identity) for 10 representative FTS queries vs. a fresh `rebuild_fts_index()` on the final state. **Rank order equivalence is explicitly NOT required** — BM25 scores after N incremental optimize() calls may differ numerically from a fresh rebuild (acknowledged in Known Limitations). Only set membership is verified.
  - **Latency p95**: `ingest_file` p95 on a ≥1,000-chunk corpus is below the threshold added in Task 5.1 (calibrated per that task's guidance) and does not regress vs. the pre-C6 baseline.
  - **No unconditional `rebuild_fts_index` in ingest paths**: all calls to `rebuild_fts_index` in `archon_search/pipeline.py` and `archon_search/sync.py` must be inside `else` branches of `if self.store.supports_incremental_fts_delete` conditionals (Plan B only) or inside `except` blocks (error fallback). Under Plan A: `grep -n "rebuild_fts_index" archon_search/pipeline.py archon_search/sync.py` returns no results. Under Plan B: any results returned are exclusively inside `not supports_incremental_fts_delete` branches — verify with: `grep -n "rebuild_fts_index" archon_search/pipeline.py archon_search/sync.py | grep -v "supports_incremental_fts_delete" | grep -v "except"` returns no results.
  - **`reindex_metadata` FTS-free**: `grep -n "rebuild_fts_index\|optimize_fts" archon_search/store.py` shows `rebuild_fts_index` only in its own definition and `optimize_fts` only in its definition and in `delete_document`.
  - **Full test suite green**: `uv run pytest` exits 0 with coverage ≥ 85%.
  - **Eval suite green**: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` exits 0.
- **Tests (TDD)**:
  - `test_fts_consistency_after_50_operations` (`@pytest.mark.integration`): builds a 1,000-chunk collection, performs 50 add/delete/re-ingest operations, then runs 10 representative FTS queries and asserts result-set equality (same doc_id sets, set membership only — rank order NOT checked) vs. a fresh `rebuild_fts_index()` on the final state.
  - **Note**: The implementing agent must create `tests/test_fts_consistency_after_50_operations.py` as part of this task, containing the 50-operation consistency test.
- **Checkpoint**: run `uv run pytest` and `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` and `uv run pytest -m integration tests/test_fts_consistency_after_50_operations.py`; confirm all acceptance criteria above are met.

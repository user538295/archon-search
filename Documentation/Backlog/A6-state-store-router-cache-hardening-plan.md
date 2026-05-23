# A6 — State-Store + Router Cache Hardening
**Purpose**: Close two concurrency bugs — CON-3 (active: concurrent sync writers lose updates to `.indexing_state.json`) and CON-2 (latent: long-lived `MultiCollectionRouter` consumers bypass the cache invalidation API via direct field assignment).
**Audience**: Internal — no user-facing changes. Affects operators running concurrent ingest, reindex, delete-document, or drop-collection against a multi-collection deployment.
**Status**: To Do

---

## Background

**CON-3** (active): `IndexingStateStore` in `progress.py` documents itself as "not thread-safe on its own — locks live at `SearchCollectionSync` level." The per-collection `asyncio.Lock`s in `sync.py` serialize *intra-collection* ingest, but they do not cover the cross-collection read-modify-write that happens *inside* the store methods themselves. Concurrent syncs across different collections can therefore interleave: writer A reads state, writer B reads state, writer A writes, writer B writes (using its stale read) — B's write silently discards A's update.

**CON-2** (latent): `routes_route.py:84` builds a fresh `MultiCollectionRouter` per request via `_build_router()`, so the documented "stale centroids until restart" symptom does not occur in the FastAPI path today. The bug is real only for long-lived router instances — today `archon_search/eval/runner.py:508` holds one and directly assigns `router._cached_metadata = list(collection_metas)`, bypassing any future invalidation API. A6 adds `MultiCollectionRouter.invalidate()`, fixes the eval-path direct-write, and pins per-request lifecycle as a regression guard.

## Goal

Post-A6:
- Any N concurrent calls to `update_collection`, `remove_collection`, `set_trigger`, or `reset_in_progress` on a single `IndexingStateStore` instance produce a final state that contains every writer's update with no lost writes.
- `sync._reset_stale_in_progress` no longer contains a read→mutate→write pattern at call-site level; the operation is a single locked call into a new `IndexingStateStore.reset_in_progress(predicate)` method.
- `MultiCollectionRouter.invalidate()` exists, clears `_cached_metadata`, and is idempotent.
- `eval/runner.py` contains no direct `_cached_metadata =` assignment.
- The FastAPI per-request router lifecycle is pinned by a regression test.

---

## Scope

### In Scope
- Add `threading.RLock` to `IndexingStateStore`; lock `update_collection`, `remove_collection`, `set_trigger`, `write`
- Add `IndexingStateStore.reset_in_progress(predicate)` as a locked RMW method
- Refactor `sync._reset_stale_in_progress` to delegate to `state_store.reset_in_progress(...)`
- Add `MultiCollectionRouter.invalidate()` method
- Add `initial_metadata: list[CollectionMeta] | None = None` constructor param to `MultiCollectionRouter` (clean injection path for eval/testing without direct field write)
- Fix `eval/runner.py:508` to use the constructor param instead of direct `_cached_metadata` assignment
- Regression tests for all invariants above

### Out of Scope
- `fsync` on `.indexing_state.json` writes — owned by A7
- TTL or version-counter invalidation strategy
- `JobStore` locking (candidate CON-4; deferred — no observed corruption, async-only call sites are event-loop-serialized)
- Cross-process coordination — single-process invariant holds
- Removing per-collection `asyncio.Lock`s in `sync.py`
- Shared-router migration to `app.state` singleton (future roadmap item)
- Single-flight `fetch_metadata()` thundering-herd mitigation

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 3.1 — Final verification & documentation update].

---

## What does NOT change
- Per-collection `asyncio.Lock`s in `sync.py:100` and `_safe_state_update` — they still serialize intra-collection ingest work
- `read()` on `IndexingStateStore` remains unlocked — it is a snapshot read; RMW callers must use the locked composite methods
- FastAPI per-request router lifecycle: `_build_router()` continues to construct a fresh router per `/route` request; `app.state` gains no router attribute
- All existing public method signatures on `IndexingStateStore` (adding `_lock` is purely internal)
- `MultiCollectionRouter`'s existing public interface except for two additions: `invalidate()` method and `initial_metadata` constructor param

---

## Known limitations / accepted trade-offs
- **Consistency without durability**: A6 alone eliminates lost updates under normal operation. Power loss between `os.replace` and disk flush can still corrupt `.indexing_state.json`. A7 (`fsync`) closes that gap.
- **TOCTOU on `invalidate()` + in-flight `fetch_metadata()`**: if a fetch is in flight when `invalidate()` is called, the response can re-populate the cache with pre-mutation data. Accepted residual; documented in `invalidate()` docstring. Mitigation (generation counter) tracked as future item.
- **`_cached_metadata =` grep guard does not catch `setattr`/`object.__setattr__`**: best-effort static guard only.
- **`_write_unlocked()` helper**: the plan treats it as optional. Either factoring a private `_write_unlocked` (used by locked composites) or allowing `RLock` re-entry via locked composites calling public `write()` are both correct. Implementer may choose; the re-entry test covers both shapes.
- **`invalidate()` ships without a production caller**: `routes_route.py` creates per-request routers; `eval/runner.py` switches to `initial_metadata` constructor injection. `invalidate()` exists as pre-emptive API for the planned shared-router migration (tracked as a separate roadmap item). It ships now so the migration task does not also need to add the API; the cost is one additional method on the router class.
- **`invalidate()` on a constructor-injected router triggers an HTTP fetch to whatever `search_url` was provided**: callers using `initial_metadata=` to bypass HTTP (e.g., eval harness with `search_url="http://invalid.example/route"`) should not call `invalidate()` afterward — doing so will cause `fetch_metadata()` to fail on the next `select()` call.

---

## Architecture

### `archon_search/progress.py` — `IndexingStateStore`

```python
import threading
from collections.abc import Callable

class IndexingStateStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = ...
        self._state_file = ...
        self._lock = threading.RLock()  # new

    # Locked public mutators (first statement is `with self._lock:`)
    def write(self, state: IndexingState) -> None: ...
    def update_collection(self, name: str, progress: CollectionProgress) -> None: ...
    def remove_collection(self, name: str) -> None: ...  # lock covers early-return branch
    def set_trigger(self, trigger: str | None) -> None: ...

    # New locked RMW method
    def reset_in_progress(
        self, predicate: Callable[[CollectionProgress], bool]
    ) -> None:
        """Under lock: read state, reset matching entries to PENDING, write.
        Short-circuits (no write) when predicate matches zero entries."""
```

### Concurrency threat model

The `threading.RLock` is justified by three overlapping scenarios:

1. **Watcher OS thread**: `watchdog` (used by `watcher.py`) fires `_DebounceHandler` callbacks on a dedicated OS thread. Although `_fire()` dispatches back to the asyncio event loop via `run_coroutine_threadsafe()`, any direct call from that thread to the state store would cause a true thread-level race.
2. **Future thread-pool usage**: `asyncio.to_thread()` or `ThreadPoolExecutor` callers would break asyncio's cooperative serialization of sync methods.
3. **Defence in depth**: within a single asyncio event loop, `update_collection()` (no `await` between `read()` and `write()`) is atomically non-interruptible, so in the pure asyncio path the lock is redundant — but it costs microseconds and eliminates any doubt.

`asyncio.Lock` is intentionally NOT used because `IndexingStateStore`'s API is fully synchronous (sync CLI callers exist; making all methods `async def` would break the CLI path). `RLock` instead of plain `Lock` is required because composite locked methods (`update_collection`, `set_trigger`, `reset_in_progress`) internally call `write()` — a non-reentrant `Lock` would deadlock on that re-entry path (unless `_write_unlocked` is factored, in which case `Lock` suffices).

### Lock ordering invariant

`archon-search` now has THREE independent lock families that A6 must coexist with:

1. **`sync.py` per-collection `asyncio.Lock`** (existing, async path) — serializes intra-collection ingest work at the HTTP/MCP handler boundary.
2. **`SearchStore._collection_locks`** (`asyncio.Lock`, added by A1 for ingest/reindex serialization on `SearchStore`).
3. **`IndexingStateStore._lock`** (`threading.RLock`, added by A6 for state file RMW).

**Invariant**: lock-acquire order MUST be #1 → #2 → #3 (outermost to innermost). Specifically: an HTTP/MCP handler acquires `sync.py`'s `asyncio.Lock` first, then may call into `SearchStore` (which acquires #2), which may call into `IndexingStateStore` (which acquires #3). Reverse ordering is FORBIDDEN.

Notes:
- #3 (`RLock`) is re-entrant; #1 and #2 (`asyncio.Lock`) are not.
- `asyncio` locks and `threading` locks cannot deadlock with each other (cooperative vs preemptive scheduling), but the ordering rule above keeps reasoning local — any code path can be checked for correctness without tracing the entire call graph.

### `archon_search/router.py` — `MultiCollectionRouter`

```python
class MultiCollectionRouter:
    def __init__(
        self,
        ...,
        initial_metadata: list[CollectionMeta] | None = None,  # new
    ) -> None:
        self._cached_metadata: list[CollectionMeta] | None = (
            list(initial_metadata) if initial_metadata is not None else None
        )

    def invalidate(self) -> None:
        """Clear cached metadata. Idempotent; safe on already-empty cache.

        TOCTOU note: if fetch_metadata() is in flight when this is called,
        the completed fetch may re-populate the cache with pre-mutation data.
        Callers must ensure mutation completes before calling invalidate().
        """
        self._cached_metadata = None
```

### `archon_search/sync.py` — `_reset_stale_in_progress`

```python
def _reset_stale_in_progress(self) -> None:
    if self._state_store is None:
        return
    try:
        self._state_store.reset_in_progress(
            lambda cp: cp.status == IndexingStatus.IN_PROGRESS
        )
    except Exception:
        logger.warning("Failed to reset stale IN_PROGRESS states", exc_info=True)
```

### `archon_search/eval/runner.py` — metadata injection

```python
router = MultiCollectionRouter(
    search_url="http://invalid.example/route",
    embedder=pipeline._embedder,
    shortlist_size=max(1, len(collection_metas)),
    confidence_threshold=0.0,
    embedding_model=pipeline._embedder.model_name,
    initial_metadata=list(collection_metas),  # replaces direct _cached_metadata write
)
```

---

## Task breakdown

### Phase 1 — State-store locking
> **Releasable**: after Task 1.3 — CON-3 is fully closed, `sync._reset_stale_in_progress` delegates entirely to the store, and all concurrency regression tests pass.

#### Task 1.1 — Add `threading.RLock` and lock public mutating methods on `IndexingStateStore`
- [x] **File**: `archon_search/progress.py`
- **Depends on**: nothing
- **Description**:
  - Add `import threading` to imports.
  - Add `self._lock = threading.RLock()` as the last statement of `__init__`.
  - Wrap `write()`, `update_collection()`, `remove_collection()`, and `set_trigger()` with `with self._lock:` as the **first statement** of each method body. The `remove_collection` early-return branch (`if state is None or name not in state.collections: return`) must be inside the lock — the check reads state, which must be atomic with the subsequent write.
  - Optional (implementer choice): factor a private `_write_unlocked(self, state: IndexingState) -> None` that locked composites call internally, with public `write()` being `with self._lock: self._write_unlocked(state)`. This avoids `RLock` re-entry overhead but is not required for correctness.
  - Update the class docstring at `progress.py:81` to reflect the class is now thread-safe via an internal `RLock`.
  - `read()` is NOT locked — it remains a snapshot read.
- **Releasable**: after this task, all public mutating methods on `IndexingStateStore` are serialized by an internal `RLock`.
- **Tests (TDD)** — `tests/test_progress.py`:
  - Unit: `test_concurrent_update_collection_no_lost_writes` — use `threading.Barrier(N)` so all N threads start their `update_collection` calls simultaneously (each for a distinct collection name); assert final state contains all N entries. Implementation: each thread monkeypatches a barrier-wait between the internal `read()` return and the `write()` call to guarantee interleaving rather than relying on OS scheduling. Confirm the test fails (at least occasionally) when the `_lock` line is removed — assert in the TDD red phase.

    **How to force the race**:
    - Wrap (do NOT replace) `IndexingStateStore.read`: capture the original, then monkeypatch with a lambda that calls the original, calls `barrier.wait()` AFTER the read returns, and returns the result. This guarantees that every thread has finished its `read()` before any thread proceeds to `write()`, forcing the read-write interleaving that exposes the lost-update bug.
    - Patching `write` instead would only catch write-write races, not the read-write interleaving that is the actual bug — the `read()` calls would still complete sequentially before any `write()` starts, and the last writer would simply observe the previous writers' updates.
    - Code sketch:
      ```python
      original_read = IndexingStateStore.read
      barrier = threading.Barrier(N)
      def wrapped_read(self):
          result = original_read(self)
          barrier.wait()  # AFTER read returns, BEFORE write
          return result
      monkeypatch.setattr(IndexingStateStore, "read", wrapped_read)
      ```
  - Unit: `test_concurrent_writers_same_key_last_write_wins` — two threads both call `update_collection("col")` with a `threading.Barrier(2)` injected between read and write; assert final state has exactly one valid "col" entry and no corrupt JSON.
  - Unit: `test_remove_collection_early_return_is_locked` — use `threading.Barrier(2)` to synchronize `remove_collection("k")` and `update_collection("k", ...)` so both enter their critical sections simultaneously; assert final state is valid JSON with either 0 or 1 entries for "k" (both are valid serializations — the test proves no corruption, not ordering). Do NOT assert which operation "ran second" (scheduling-dependent).
  - Unit: `test_exception_under_lock_releases_lock` — monkeypatch `write()` to raise `OSError`; call `update_collection()`; catch the exception; immediately call `set_trigger("x")` on the same instance and assert it completes within 1 s (lock was released by the `with` context manager).
  - Unit: `test_write_is_locked_independently` — call `write()` directly (not through a composite) from two threads with a `threading.Barrier(2)` between the `os.replace` call and its return; assert both complete and the final file is valid JSON. This proves `write()` itself holds the lock, not only the composites.
  - Unit: `test_set_trigger_under_concurrent_update_collection` — race `set_trigger("t")` with `update_collection("col", ...)` using `threading.Barrier(2)` interleaving; assert final state has both `trigger == "t"` and `"col"` in `collections` (both writes survive).
  - Unit: `test_rlock_reentry_does_not_deadlock` — only when `_write_unlocked` is NOT factored: call `update_collection()` in a `threading.Thread` with `daemon=True`; use `thread.join(timeout=2)`; assert `not thread.is_alive()` (returned before timeout, proving re-entry does not deadlock). Skip via `pytest.mark.skipif` if `_write_unlocked` helper is present in the implementation.
  - Unit: `test_read_does_not_acquire_lock` — call `read()` from two threads simultaneously via `threading.Barrier(2)`; assert both return without blocking (should pass trivially — documents that `read()` is lock-free).
  - Checkpoint: `uv run pytest tests/test_progress.py -v -k "concurrent or locked or lock or reentry or write_is_locked or set_trigger_under"`

#### Task 1.2 — Add `IndexingStateStore.reset_in_progress(predicate)` locked method
- [x] **File**: `archon_search/progress.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `from collections.abc import Callable` to imports.
  - Implement `reset_in_progress(self, predicate: Callable[[CollectionProgress], bool]) -> None` on `IndexingStateStore`.
  - Method body (under `with self._lock:` as first statement):
    1. Call `self.read()` (or `self._write_unlocked` variant if factored); short-circuit if state is `None`.
    2. Identify entries where `predicate(cp)` is `True`.
    3. If no entry matches, return without writing (short-circuit — no `write()` call).
    4. For each matching entry, replace with a new `CollectionProgress(status=IndexingStatus.PENDING, total_files=cp.total_files, processed_files=cp.processed_files, processed_paths=cp.processed_paths, file_mtimes=cp.file_mtimes, file_hashes=cp.file_hashes, indexed_embedding_model=cp.indexed_embedding_model, indexed_chunk_size=cp.indexed_chunk_size)` — preserving all non-status fields, resetting only `status`, `started_at`, `completed_at`, `error`, `error_count`.
    5. Update `state.last_updated` and call `self.write(state)` (or `_write_unlocked` if factored).
  - If `write()` raises, the exception propagates; the lock is released via `with`.
- **Releasable**: after this task, `IndexingStateStore.reset_in_progress(predicate)` is callable — any locked RMW that resets matching entries to PENDING.
- **Tests (TDD)** — `tests/test_progress.py`:
  - Unit: `test_reset_in_progress_resets_matching_entries` — pre-populate state with one IN_PROGRESS and one DONE collection; call `reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)`; assert IN_PROGRESS entry is now PENDING with preserved non-status fields; DONE entry is unchanged.
  - Unit: `test_reset_in_progress_short_circuits_when_no_match` — monkeypatch `write()` to record calls; call `reset_in_progress(lambda cp: False)`; assert `write()` was never called.
  - Unit: `test_reset_in_progress_short_circuits_on_none_state` — call on an `IndexingStateStore` with no state file; assert no exception and `write()` not called.
  - Unit: `test_reset_in_progress_concurrent_with_update_collection` — use `threading.Barrier(2)` to force `reset_in_progress` and `update_collection("new-col", ...)` to enter their critical sections simultaneously; assert final state: (a) is valid JSON, (b) `"new-col"` is present in `collections` (the `update_collection` write survived), (c) any previously IN_PROGRESS entry is now PENDING (the reset survived). Both operations must leave a coherent, non-corrupted state.
  - Unit: `test_reset_in_progress_preserves_non_status_fields` — verify `total_files`, `processed_files`, `processed_paths`, `file_mtimes`, `file_hashes`, `indexed_embedding_model`, `indexed_chunk_size` survive the reset; verify `started_at`, `completed_at`, `error`, `error_count` are cleared.
  - Unit: `test_reset_in_progress_skips_failed_and_done` — pre-populate state with one IN_PROGRESS, one FAILED, and one DONE collection; call `reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)`; assert only the IN_PROGRESS entry is changed to PENDING; FAILED and DONE entries are unchanged.
  - Unit: `test_reset_in_progress_all_entries_match` — pre-populate state with three IN_PROGRESS collections; call `reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)`; assert all three are now PENDING and non-status fields are preserved.
  - Checkpoint: `uv run pytest tests/test_progress.py -v -k "reset_in_progress"`

#### Task 1.3 — Refactor `sync._reset_stale_in_progress` to delegate to store
- [x] **File**: `archon_search/sync.py`
- **Depends on**: Task 1.2
- **Description**:
  - Replace the entire `_reset_stale_in_progress` method body with a single delegation call:
    ```python
    def _reset_stale_in_progress(self) -> None:
        if self._state_store is None:
            return
        try:
            self._state_store.reset_in_progress(
                lambda cp: cp.status == IndexingStatus.IN_PROGRESS
            )
        except Exception:  # noqa: BLE001
            logger.warning("Failed to reset stale IN_PROGRESS states", exc_info=True)
    ```
  - Remove the `from archon_search.progress import CollectionProgress, IndexingStatus` import from the method body if `IndexingStatus` is not used elsewhere in the method scope — move to module-level import if needed (it already exists at module level or add it there).
  - The method must no longer contain any call to `self._state_store.read()`, any loop over `state.collections`, or any direct call to `self._state_store.write()`.
- **Releasable**: after this task, CON-3 is fully closed — `_reset_stale_in_progress` is a thin delegator with no external RMW pattern.
- **Tests (TDD)** — `tests/test_sync.py`:
  - Unit: `test_reset_stale_in_progress_delegates_to_store` — monkeypatch `IndexingStateStore.reset_in_progress` to record the predicate argument; trigger `_reset_stale_in_progress`; assert `reset_in_progress` was called exactly once; assert the predicate returns `True` for an `IN_PROGRESS` entry and `False` for a `DONE` entry.
  - Unit: `test_reset_stale_in_progress_no_rmw_in_sync` — source-level assertion: read `archon_search/sync.py`, find the `_reset_stale_in_progress` method body, assert it contains no `self._state_store.read()` call and no `self._state_store.write()` call. (This guards against vestigial RMW code surviving the refactor.)
  - Unit: `test_reset_stale_in_progress_noop_when_store_is_none` — construct `SearchCollectionSync` without a state store; call `_reset_stale_in_progress()`; assert no exception.
  - Integration: `test_reset_stale_in_progress_integration` — using a real `IndexingStateStore` on a tmp dir, pre-populate with IN_PROGRESS entries, call `_reset_stale_in_progress`, assert all IN_PROGRESS are now PENDING.
  - Checkpoint: `uv run pytest tests/test_sync.py -v -k "reset_stale"`

---

### Phase 2 — Router cache invalidation
> **Releasable**: after Task 2.3 — CON-2 is fully addressed: `invalidate()` exists, eval path uses the constructor injection pattern, and FastAPI per-request lifecycle is pinned by a regression test.

#### Task 2.1 — Add `MultiCollectionRouter.invalidate()` and `initial_metadata` constructor param
- [x] **File**: `archon_search/router.py`
- **Depends on**: nothing (independent of Phase 1)
- **Description**:
  - Add `initial_metadata: list[CollectionMeta] | None = None` as the last parameter of `MultiCollectionRouter.__init__`.
  - In `__init__`, change `self._cached_metadata: list[CollectionMeta] | None = None` to:
    ```python
    self._cached_metadata: list[CollectionMeta] | None = (
        list(initial_metadata) if initial_metadata is not None else None
    )
    ```
  - Add method:
    ```python
    def invalidate(self) -> None:
        """Clear cached metadata. Idempotent.

        TOCTOU: if fetch_metadata() is in flight when this is called, the
        completed fetch may re-populate with pre-mutation data. Ensure the
        mutation completes before calling invalidate().
        """
        self._cached_metadata = None
    ```
  - `invalidate()` must be safe to call when `_cached_metadata` is already `None`.
- **Releasable**: after this task, `MultiCollectionRouter.invalidate()` is callable and `initial_metadata` constructor injection works.
- **Tests (TDD)** — `tests/test_router.py`:
  - Unit: `test_invalidate_clears_cached_metadata` — set `_cached_metadata` on a router; call `invalidate()`; assert `_cached_metadata is None`.
  - Unit: `test_invalidate_is_idempotent` — call `invalidate()` twice on a router where `_cached_metadata` is already `None`; assert no exception.
  - Unit: `test_initial_metadata_populates_cache` — construct router with `initial_metadata=[some_meta]`; assert `_cached_metadata == [some_meta]` without calling `fetch_metadata()`.
  - Unit: `test_initial_metadata_none_leaves_cache_empty` — construct router with `initial_metadata=None`; assert `_cached_metadata is None`.
  - Unit: `test_initial_metadata_empty_list_marks_cache_populated` — construct router with `initial_metadata=[]`; assert `_cached_metadata == []` (not `None`) so `fetch_metadata()` returns `[]` without making an HTTP call (empty list is a valid populated cache, distinct from `None` which means "not yet fetched").
  - Unit: `test_initial_metadata_is_copied` — pass a list as `initial_metadata`; mutate the original list; assert `_cached_metadata` is unchanged (defensive copy).
  - Unit: `test_select_uses_initial_metadata_without_http` — construct router with `initial_metadata` containing a known collection; call `router.select(query)`; assert the known collection appears in results without any HTTP call being made (no fetch triggered because cache is populated).
  - Checkpoint: `uv run pytest tests/test_router.py -v -k "invalidate or initial_metadata"`

#### Task 2.2 — Replace `eval/runner.py` direct `_cached_metadata` assignment with constructor injection
- [x] **File**: `archon_search/eval/runner.py`
- **Depends on**: Task 2.1
- **Description**:
  - At `runner.py:508`, remove `router._cached_metadata = list(collection_metas)`.
  - Move `list(collection_metas)` to the `MultiCollectionRouter(...)` constructor call above it as `initial_metadata=list(collection_metas)`.
  - No other changes to the function — `router.select(query_text)` call at line 509 is unaffected (cache is already populated via constructor).
  - Verify no `_cached_metadata` assignment remains anywhere in `archon_search/` outside of `router.py`'s own `__init__` and `fetch_metadata` internals.
- **Releasable**: after this task, no code in `archon_search/` directly assigns to `_cached_metadata` from outside the `MultiCollectionRouter` class.
- **Tests (TDD)** — `tests/test_router.py`:
  - Unit: `test_eval_runner_no_direct_cached_metadata_write` — source-level test: read all `.py` files under `archon_search/` excluding `router.py`; assert none contain the pattern `_cached_metadata\s*=` (regex). This test is the automated guard against regressions.
  - Unit: `test_run_router_for_query_uses_initial_metadata` — call `_run_router_for_query` (the eval runner helper at `archon_search/eval/runner.py`) with injected `collection_metas`; monkeypatch `httpx` to assert it is never called; assert the returned shortlist is non-empty. (Confirms the eval path still works after the constructor injection change.)
  - Checkpoint: `uv run pytest tests/test_router.py -v -k "cached_metadata or run_router_for_query"`

#### Task 2.3 — FastAPI per-request router lifecycle regression guard
- [ ] **File**: `tests/test_routes_route.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add two regression guard tests to `tests/test_routes_route.py`:
    1. `_build_router` is called exactly once per `/route` request: spy on `routes_route._build_router` using `unittest.mock.patch`; make TWO sequential `POST /route` requests; assert the spy's `call_count == 2`. A single-request check would pass even if the router were cached on the first request and reused on subsequent ones.
    2. `app.state` has no router attribute: after a `POST /route` request completes, assert `not hasattr(app.state, "router")` and `not hasattr(app.state, "multi_collection_router")`.
  - The `id()`-inequality form alone is insufficient — both checks together ensure that a future refactor caching the router on `app.state` and skipping `_build_router` would be caught.
- **Releasable**: after this task, any future migration to a shared router that breaks the per-request invariant will be caught by CI.
- **Tests (TDD)** — `tests/test_routes_route.py`:
  - Unit: `test_build_router_called_once_per_request` — spy on `_build_router`; make TWO sequential `POST /route` requests; assert `call_count == 2` (one call per request). A single-request check (call_count == 1) does not detect a shared cached router that is initialized on the first request and reused on subsequent ones.
  - Unit: `test_app_state_has_no_router_attribute` — after a `/route` POST, assert `app.state` has neither `router` nor `multi_collection_router` attribute.
  - Checkpoint: `uv run pytest tests/test_routes_route.py -v -k "per_request or build_router or app_state"`

---

### Phase 3 — Verification & Documentation

#### Task 3.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Tasks 1.1, 1.2, 1.3, 2.1, 2.2, 2.3
- **Description**:
  - Run the full test suite and confirm no regressions: `uv run pytest`.
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, Architecture docs, user guides) and update every file whose content is affected by these changes:
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — `IndexingStateStore` now thread-safe; `MultiCollectionRouter` now has `invalidate()` method.
    - `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` — CON-3 closed; state file is now concurrency-safe.
    - `Documentation/Architecture/130_data_architecture_and_persistence.md` — `.indexing_state.json` RMW is now serialized.
    - `Documentation/Architecture/210_performance_and_scalability.md` — router per-request lifecycle pinned; `invalidate()` API added for future long-lived router consumers.
    - `Documentation/roadmap.md` — mark CON-3 and CON-2 as closed; note A7 as next step for durability.
    - Any other doc that references "not thread-safe" for `IndexingStateStore` or the "stale until restart" router limitation.
  - Verify `progress.py` class docstring no longer says "not thread-safe on its own — locks live at SearchCollectionSync level."
  - Verify `archon_search/eval/runner.py` contains no `_cached_metadata =` assignment.
  - Verify `sync._reset_stale_in_progress` contains no `read()` → loop → `write()` pattern.
- **Releasable**: after this task, A6 is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` passes with coverage ≥ 85%.
  - `grep -rn "_cached_metadata\s*=" archon_search/ | grep -v "router.py"` returns no matches.
  - `grep -n "read()\|\.write(" archon_search/sync.py | grep "_reset_stale_in_progress" -A5` shows no direct `read()`/`write()` calls inside the method body.
  - `grep -n "not thread-safe\|locks live at SearchCollectionSync" archon_search/progress.py` returns no matches.
  - `MultiCollectionRouter.invalidate` exists and passes `test_invalidate_is_idempotent`.
  - All per-request lifecycle guard tests pass.
  - All CON-3 race tests pass; without the `RLock`, the concurrency tests fail (verified once during TDD).
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

# Feature Brief: A6 — State-Store + Router Cache Hardening

## Problem
Two latent concurrency bugs (`CON-2`, `CON-3`).

- **CON-3** (real, active): concurrent syncs across different collections lose updates to the shared `~/.archon-search/.indexing_state.json` because the per-collection `asyncio.Lock`s in `sync.py` don't cover the cross-collection read-modify-write that happens *inside* `IndexingStateStore`.
- **CON-2** (latent, not yet active in production): the roadmap text says "stale centroids until process restart" but `archon_search/server/routes_route.py:84` builds a fresh `MultiCollectionRouter` per request via `_build_router()`. The FastAPI runtime therefore does not exhibit the documented symptom. The bug is real only for **long-lived router instances** — today, `archon_search/eval/runner.py:500` (one router reused across all eval queries). A6 adds the missing invalidation API and pins the per-request lifecycle as a regression guard, so future migration to a shared router (out of scope here) doesn't reintroduce the bug.

## Goal
- **CON-3 closed**: `.indexing_state.json` survives N concurrent collection mutations with zero lost updates across every code path, including direct `write()` calls from `sync._reset_stale_in_progress`.
- **CON-2 closed**:
  - `MultiCollectionRouter.invalidate()` exists and clears `_cached_metadata`.
  - All long-lived router consumers (today only `eval/runner.py`) call it after any centroid-changing operation. The existing direct `_cached_metadata` write in `runner.py:508` is removed.
  - Regression guard: a test pins the FastAPI per-request router lifecycle so the documented symptom cannot reappear silently.
- Regression tests deterministically pin both invariants.

## Users & Context
Internal hardening — no user-facing flow. Affects any operator running ingest, reindex, delete-document, drop-collection, or description regeneration against a multi-collection deployment. Symptoms today: search routes to a no-longer-best collection after ingest/delete, and on a busy server `.indexing_state.json` can silently drop progress entries from one of several concurrent writers.

## Core Flow
Observable contract:

1. Any caller mutates a collection (ingest, reindex, delete-document, drop-collection, force-regenerate-description-via-ingest).
2. The operation completes its LanceDB write and centroid recompute before any router cache action.
3. If the caller holds a long-lived `MultiCollectionRouter` (today: only `eval/runner.py`), it calls `router.invalidate()` after the commit. The FastAPI route handler does not — its router is request-scoped.
4. The next `router.fetch_metadata()` call (next request, in the FastAPI path; next query, in the eval path) observes fresh centroids.
5. In parallel, any `IndexingStateStore` mutation — through any public method — is serialized by an internal lock; the file is always a consistent merge of all writers' updates.

## In Scope

### State-store locking (CON-3)
- Add a single `threading.RLock` inside `IndexingStateStore` (`archon_search/progress.py`).
  - **Rationale for `threading.RLock` over `threading.Lock` and over `asyncio.Lock`**:
    - **Sync vs async**: the store's API is fully synchronous and has sync CLI callers (`archon_search/cli/collection.py`); `asyncio.Lock` would force every method to `async def` and break the CLI path. archon-search is single-process, so a thread lock is sufficient.
    - **Reentrant vs non-reentrant**: composite methods like `update_collection()` need to call `write()` while holding the lock. A non-reentrant `Lock` deadlocks; `RLock` permits the same thread to re-acquire.
- **Lock placement contract**: the `with self._lock:` block is the **first statement** of every locked public method, covering the entire body including early-return branches (`remove_collection`'s "key not present" branch reads state to check — that read must be inside the lock too).
- Lock these methods (full body, first statement):
  - `update_collection()`
  - `remove_collection()` — including the early-return path
  - `set_trigger()`
  - `write()` — protects direct callers like `sync._reset_stale_in_progress` (`sync.py:320–342`)
- `read()` stays unlocked. It is a snapshot read; callers that need read-modify-write atomicity must do so through one of the locked composite methods, not by calling `read()` then `write()` separately.
- **Refactor `sync._reset_stale_in_progress`**: today it does `read()` → mutate in place → `write()` at the call-site level. The lock on `write()` alone does **not** make this atomic against concurrent `update_collection`. Move the read-modify-write inside the store as a new locked method `IndexingStateStore.reset_in_progress(predicate)` that does the full RMW under the lock. The method short-circuits (no write, no further work) when the predicate matches zero entries. Replace the call in `sync.py:342`. The old `sync._reset_stale_in_progress` body either delegates entirely to the new store method or is removed; vestigial read-mutate-write code left behind is a regression risk.
- **Lock acquisition on internal calls**: when a locked composite (`update_collection`, `set_trigger`, `remove_collection`, `reset_in_progress`) calls into `write()`, the `RLock` is reentered. This is correct but redundant. Implementation may factor a private `_write_unlocked()` helper used by locked composites, with the public `write()` being a thin locked wrapper for external direct callers. Not required for correctness.
- Update the class docstring (`progress.py:81`) which currently says "not thread-safe on its own — locks live at SearchCollectionSync level."
- Keep the existing per-collection `asyncio.Lock`s in `sync.py` for their original purpose (serializing intra-collection ingest work). They nest harmlessly around the store's internal lock.

### Router cache invalidation (CON-2)
- Add `MultiCollectionRouter.invalidate()` in `archon_search/router.py` that clears `_cached_metadata`. Idempotent; safe to call on an already-empty cache.
- **Per-request lifecycle is preserved**: `_build_router()` continues to construct a fresh router per request in `routes_route.py`. Do **not** introduce a `pipeline → router` dependency for production code — the FastAPI path has no shared router to invalidate.
- **Long-lived router consumers** call `invalidate()` after any centroid-changing operation. Today there is exactly one: `archon_search/eval/runner.py`. Audit that file and:
  - Remove the direct `router._cached_metadata = list(collection_metas)` assignment at line 508.
  - Replace any post-mutation usage with `router.invalidate(); await router.fetch_metadata()` (or just `await router.select(...)` and let the lazy fetch happen).
- **No funnel work needed for `drop_collection`, `delete_document`, etc.** in production paths because there is no shared router to coordinate. (Out-of-scope follow-up: if/when the router becomes a long-lived `app.state` singleton, those mutation sites will need invalidation hooks — track as a separate roadmap item.)

### Tests (invariants — implementation details belong in the plan)
- **State-store race serialization**: under an injected yield between read and write (test-only monkeypatch of `IndexingStateStore.read`/`write`), N concurrent threaded writers calling `update_collection`, `remove_collection`, `set_trigger`, and `reset_in_progress` produce a final JSON that contains every writer's update — no lost writes. Without the new lock, the same test fails.
- **`remove_collection` early-return is locked**: a concurrent `update_collection(name)` racing with `remove_collection(name)` for a name not yet present must serialize cleanly — assert the post-state matches the operation that ran second.
- **Exception under lock**: any locked method raising mid-critical-section releases the lock; a subsequent operation succeeds without timeout.
- **`reset_in_progress` is internal**: assert `sync._reset_stale_in_progress` no longer contains a `read()` → mutate → `write()` pattern — it either delegates entirely to `state_store.reset_in_progress(...)` or no longer exists as a method. Asserting "no direct `write()` call" alone is insufficient; vestigial RMW code could remain.
- **`MultiCollectionRouter.invalidate()` exists, clears `_cached_metadata`, and is idempotent**.
- **RLock re-entry is exercised**: a test calls a composite method (e.g. `update_collection`) with `write()` monkeypatched to assert `self._lock` is already owned by the calling thread at entry, **or** runs the composite call in a thread with a timeout to prove no deadlock. Without this, a refactor that swaps `RLock` for `Lock` and inlines `write()` would pass the concurrency tests but break direct external `write()` callers.
- **`eval/runner.py` no longer writes `_cached_metadata` directly**: a source-level test greps for `_cached_metadata\s*=` assignments **scoped to `archon_search/` only** (test files are excluded — `tests/test_router.py:251` legitimately pokes the internal field). Acknowledged limitation: grep does not catch `setattr` or `object.__setattr__` evasions; this is a best-effort guard, not a guarantee.
- **Eval-path invalidation**: in the eval harness, simulate a mid-run mutation followed by `invalidate()`; assert the next query triggers a **successful** refetch that returns fresh data — not merely that a refetch is attempted, because a failed fetch already bypasses caching and would mask a missing invalidation.
- **FastAPI per-request router lifecycle (regression guard)**: assert (a) `_build_router` is called exactly once per `/route` request (call-count on a spy/mock) **and** (b) `app.state` has no `router` / `multi_collection_router` attribute. The `id()`-inequality form alone is insufficient — it would still pass if a future refactor cached the router on `app.state` and stopped calling `_build_router` per-request.
- **Per-collection `asyncio.Lock` preservation**: assert `SearchCollectionSync._collection_locks` is still a dict of `asyncio.Lock` and `_safe_state_update` still acquires the per-collection lock.
- **Invalidate-racing-fetch is document-only**: the TOCTOU window is accepted (see Edge Cases). No automated test required; documented in code comment on `invalidate()`.

## Out of Scope
- **`fsync` on `.indexing_state.json` writes** — owned by A7. **Explicit caveat**: A6 alone fixes *consistency* (no lost updates under normal operation), not *durability* (power-loss between rename and disk flush can still corrupt). Brief A7 closes that gap.
- **TTL or version-counter invalidation strategy** — explicit invalidation matches the roadmap contract; revisit only if a new mutation path repeatedly forgets to invalidate.
- **`JobStore` locking** — same shape exists in `archon_search/jobs/store.py`; track as a follow-up. Justification for deferral: `JobStore` is mutated only by HTTP handlers serialized by the FastAPI event loop within a single process, and existing tests have not surfaced corruption. If A2/A4 work increases concurrent job creation, promote it.
- **Cross-process coordination** — single-process invariant holds.
- **Removing per-collection `asyncio.Lock`s in `sync.py`** — they still serialize ingest work.
- **Single-flight `fetch_metadata()` (thundering-herd mitigation)** — after `invalidate()`, N concurrent readers may each issue one RPC fetch. The RPC is local (in-process FastAPI) and the cost is small. If profiling shows it matters, add a `threading.Lock` around the fetch in a follow-up.

## Key Decisions
- **`threading.RLock` inside `IndexingStateStore`, not `asyncio.Lock` and not caller-side**: store owns its file invariant; sync API and sync CLI callers stay sync; `RLock` allows composite methods to call `write()` while holding the lock without deadlock. Critical section is microseconds (read + merge + atomic rename); per-collection ingest parallelism unaffected.
- **Move `_reset_stale_in_progress` into the store** as `reset_in_progress(predicate)`: external read-modify-write at call-site level cannot be protected by locking `write()` alone. Encapsulating it inside a locked store method is the only correct fix.
- **`read()` is unlocked**: it's a snapshot read; any caller doing read-modify-write outside the store is bypassing safety and must be refactored (today: only `_reset_stale_in_progress`).
- **CON-2 scoped to long-lived router consumers**: the FastAPI runtime builds the router per request (`_build_router()` in `routes_route.py`), so the documented "stale until restart" symptom does not occur there. Today, only `eval/runner.py` holds a long-lived router. A6 adds the `invalidate()` API, fixes the eval-path direct-cache-write, and pins per-request lifecycle as a regression guard. **Migrating to a shared router (and the resulting need to invalidate from `pipeline`/`sync`/route handlers) is explicitly out of scope** — track as separate roadmap item.
- **No `pipeline → router` coupling introduced**: would be wrong for the current per-request lifecycle and would obscure the lifecycle decision for future readers.
- **Description regeneration is not a separate mutation path**: it flows through `ingest_directory()` with `force_regenerate_description=True`.
- **A6 = consistency only, not durability**: post-A6, pre-A7, a power loss between rename and disk flush can still corrupt state. A7 (fsync) closes that gap.

## Edge Cases & Constraints
- **Lock + exception**: every mutation method uses `with self._lock:` so the lock releases on any exception in `read()`, JSON parsing, or `write()`.
- **Re-entrancy**: `RLock` is reentrant — `update_collection` can call `self.write()` while holding the lock without deadlock. Audit for unintended re-entry remains good practice but is no longer a correctness requirement.
- **`remove_collection` early-return**: the brief explicitly requires the lock to wrap the entire method body including the early-return read, so a concurrent `update_collection(name)` cannot interleave with the "is name present?" check.
- **`read()` is a snapshot**: callers must not do external read-modify-write. Today there is exactly one such caller (`sync._reset_stale_in_progress`); it gets refactored into a locked store method as part of this scope.
- **TOCTOU between `invalidate()` and an in-flight `fetch_metadata()`** (eval path only): if a fetch is in flight when `invalidate()` is called, the response can re-populate the cache with pre-mutation data. **Accepted residual** documented in the `invalidate()` docstring. Mitigation if needed later: a generation counter on the router.
- **Ordering**: LanceDB write → centroid recompute → `router.invalidate()`. Any reordering reopens CON-2 in the long-lived consumer.
- **CLI drop-collection path leaves indexing-state entries orphaned today** at `cli/collection.py:144` (the `remove` command calls `store.drop_collection()` without `state_store.remove_collection()`). The reindex path at line 229 is already correct — it calls `state_store.remove_collection()` at line 226 before `drop_collection()`. Pre-existing bug at line 144 only, **out of scope for A6** — note as a separate cleanup item. A6 does not regress or fix it.
- **`drop_collection` and `delete_document` lack a single funnel**: today 4+ separate call sites. A6 does not introduce a funnel because A6 does not introduce router invalidation in those paths (no shared router). When a shared router lands in a future item, that work must add the funnel — flagged in `Future Iterations`.

## Open Questions
- `JobStore` has the same RMW shape — defer to a follow-up (see Future Iterations), or fold in now? Default: defer; no observed corruption today, and FastAPI's single-threaded event loop provides accidental serialization for the current async-only call sites.
- Should `IndexingStateStore` expose a `transaction()` context manager for future external multi-step RMW callers? Default: no — adding it on demand is cheaper than designing speculatively.

## Future Iterations
- **A7**: `fsync` on the atomic rename for durability.
- **Shared router migration**: promote `MultiCollectionRouter` to an `app.state` singleton. At that point, ingest/recompute/delete-document/drop-collection paths must each call `invalidate()`. This work will also need a `pipeline.drop_collection()` funnel so the 4+ current call sites don't each need manual invalidation. Tracked as separate roadmap item.
- **CLI drop-collection orphan cleanup**: `cli/collection.py:144` (the `remove` command) should call `state_store.remove_collection()` after `store.drop_collection()`. Pre-existing bug; not A6 scope.
- **`JobStore` locking**: candidate CON-4.
- **Single-flight `fetch_metadata()`** if profiling shows post-invalidate thundering herd matters.
- **Generation-counter router invalidation** if a future mutation path repeatedly forgets `invalidate()`.

## Recommendation
Build now — but smaller than the original framing. CON-3 is the real, active bug and its fix (RLock + move `_reset_stale_in_progress` into the store) is mechanical. CON-2's documented symptom doesn't manifest in production because the FastAPI path builds a router per request; A6's CON-2 work is therefore narrow — add the `invalidate()` API, fix the one direct-cache-write bypass in `eval/runner.py`, and pin per-request lifecycle as a regression guard. The hardest part is **deterministic test design**: today's purely-synchronous RMW has no `await` between read and write, so `asyncio.gather` will never reproduce the race. Tests must inject an interleaving point (monkeypatch a yield between `read()` and `write()`, drive with threads) or they pass without the fix. What must not be compromised: (1) `RLock` not `Lock` — avoids the deadlock on the composite-calls-`write()` path; (2) `_reset_stale_in_progress` moves *into* the store, not just relies on `write()` being locked; (3) no `pipeline → router` coupling is introduced — the per-request lifecycle is the right design for now and inventing a shared-router workaround in this scope buries the lifecycle decision.

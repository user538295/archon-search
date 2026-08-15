# 11. Embedder Cache Bounded Waiter Timeout

**Status**: Accepted
**Date**: 2026-08-14
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR 08 — Per-Collection Embedder LRU Cache](08_per_collection_embedder_lru_cache.md) §"Concurrent-load deduplication" and §Consequences/"Shared across requests"

## Context

ADR 08 describes `EmbedderCache.get_or_load` deduplication as unconditional: a
waiter that finds a load already in progress calls `await event.wait()` with
no bound, and ADR 08's Consequences section promises "concurrent requests for
the same collection model block on a single load and then share the result."

In production this is unsafe. `asyncio.to_thread` runs `make_embedder` and can
wedge — a stalled disk, a corrupt ONNX file, a network hang fetching model
weights on first use — and prior to this change every concurrent search
against that collection model would block **forever** with no diagnostic,
because the only way out of `await event.wait()` was the loader itself
finishing.

A second, narrower question the original fix had to answer: what happens to
`_loading` bookkeeping when a bounded wait times out? The tempting fix —
delete the timed-out waiter's entry from `_loading` so a later caller doesn't
also wait — is wrong. The loader that owns that entry is often still running;
deleting its registration would let the *next* caller become a second,
concurrent `make_embedder` call for the same model name, doubling ONNX
load cost and breaking the one-loader-per-model invariant ADR 08 relies on.

## Decision

`get_or_load` bounds the waiter's wait with `asyncio.wait_for(event.wait(),
timeout=_LOAD_WAIT_TIMEOUT_SECONDS)` (`archon_search/embedder_cache.py`,
`_LOAD_WAIT_TIMEOUT_SECONDS = 120.0`). On timeout it raises a new
`EmbedderNotReadyError` (a `RuntimeError` subclass) **without touching
`self._loading`** — the timed-out waiter has no authority over the loader's
lifecycle. The loader itself remains the sole writer of its own `_loading`
entry: a `try`/`finally` spans the **whole** loader region — the
`asyncio.to_thread(make_embedder, ...)` call *and* the success path's
`async with self._lock` that caches the result — so it clears the entry (and
sets the event to wake any parked waiters) on every exit: successful load, a
`make_embedder` exception, or a cancellation delivered anywhere in that
region, including while the loader is parked acquiring `self._lock` after
`make_embedder` already returned. A registration can only ever go stale if
its loader is itself wedged forever, in which case a second loader spawned in
its place would hang identically — so there is no cleanup action a timed-out
waiter could safely take that the loader's own exit handling does not already
cover.

`routes_search.py` catches `EmbedderNotReadyError` and maps it to HTTP 503:
the load is wedged or merely slow, not a bug in the request, so the caller is
told to retry rather than receiving a 500.

The one-loader-per-model invariant from ADR 08 is **preserved, not weakened**:
at most one coroutine ever runs `make_embedder` for a given model name at a
time, whether or not any waiter times out waiting for it.

## Consequences

### Positive

- **No unbounded hang**: a wedged load surfaces as a diagnosable 503 to every
  waiter after at most `_LOAD_WAIT_TIMEOUT_SECONDS` (120 s), instead of parking
  the request indefinitely.
- **Invariant preserved**: `_loading` bookkeeping is owned exclusively by the
  loader, never mutated by a waiter — so a timed-out wait can never trigger a
  duplicate concurrent `make_embedder` call for the same model.
- **Eventual consistency**: if the loader eventually succeeds after a waiter
  has already timed out and returned a 503, the model is cached normally for
  the next caller — the timeout only affects callers that were waiting past
  the budget, not the outcome of the load itself.

### Negative / Tradeoffs

- **A waiter no longer necessarily "shares the result"**: ADR 08's original
  promise (every concurrent caller for the same model gets the same
  `Embedder`) no longer holds unconditionally — a caller that waits past 120 s
  gets `EmbedderNotReadyError` instead, and must retry rather than receiving
  the eventual result inline.
- **`_LOAD_WAIT_TIMEOUT_SECONDS` is a single global constant**, not
  configurable per model or per deployment. A model that legitimately takes
  longer than 120 s to load from cold (e.g. a large model on a slow disk) will
  surface spurious 503s to concurrent waiters even though the load is
  otherwise healthy.

## Alternatives Considered

- **Delete the waiter's `_loading` entry on timeout** (with or without an
  identity guard comparing the waiter's captured `event` to the current
  registration). Rejected — the identity guard does not distinguish "my own
  stale registration" from "the live loader's registration, which I happen to
  hold a reference to," because a waiter's `event` reference *is* the
  loader's event object. Deleting it while the loader is still running empties
  `_loading` and lets the next caller start a second concurrent load for the
  same model — reintroducing the very duplicate-load cost ADR 08 was written
  to prevent.
- **Unbounded wait (status quo per ADR 08)**: Rejected for the reason in
  Context — a wedged loader hangs every concurrent caller for that model
  forever with no diagnostic path.
- **Cancel the loader's `asyncio.to_thread` task from a timed-out waiter**:
  Rejected — a thread already running `fastembed`/ONNX C code cannot be safely
  interrupted from Python, and doing so would also fail every *other* waiter
  on that load, not just the one that timed out.

## Cross-References

- [ADR 08 — Per-Collection Embedder LRU Cache](08_per_collection_embedder_lru_cache.md):
  this ADR narrows one aspect of ADR 08's deduplication design (the waiter's
  wait becomes bounded, with a new error type and HTTP mapping); the LRU
  eviction, lock-serialised bookkeeping, and one-loader-per-model invariant
  described there are unchanged and still authoritative.
- `archon_search/embedder_cache.py`: `EmbedderCache.get_or_load`,
  `_LOAD_WAIT_TIMEOUT_SECONDS`, `EmbedderNotReadyError`.
- `archon_search/server/routes_search.py`: catches `EmbedderNotReadyError` and
  returns HTTP 503.

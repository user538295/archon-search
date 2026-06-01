# 08. Per-Collection Embedder LRU Cache

**Status**: Accepted
**Date**: 2026-06-01
**Deciders**: archon-search maintainers

## Context

The C1 milestone introduced per-collection embedding models: each collection
may declare its own `active_embedding_model` in `CollectionMeta`, independently
of the server-wide `SearchConfig.embedding_model`. Before C1, a single global
`Embedder` instance (backed by `ModelEmbedder`) was created at startup and
reused for every request. This worked because the entire server shared one
model.

With per-collection models, the naive approach — constructing a new `Embedder`
and loading the underlying `fastembed.TextEmbedding` ONNX model on every
request — is unacceptable:

- fastembed model loading is a synchronous, CPU-bound operation that blocks for
  seconds on first access and involves downloading ONNX weights on a cold
  machine.
- Running this synchronously on a FastAPI/uvicorn async worker would stall the
  event loop for every affected request.
- Running it in a thread pool (`asyncio.to_thread`) per request would create
  duplicate model instances in memory for concurrent requests targeting the
  same collection model, and waste RAM proportional to request concurrency
  rather than to the number of distinct models in use.

The server is a single-process, developer-laptop-targeted tool (see
[Architecture / 100 — System Architecture Overview](../Architecture/100_system_architecture_overview.md)).
Memory is a real constraint: a typical fastembed small model occupies ~100 MB
in ONNX loaded form; a larger model can be several hundred MB. The design must
bound peak memory regardless of how many distinct models are configured across
collections.

## Decision

Introduce **`EmbedderCache`** (`archon_search/embedder_cache.py`): an
async-aware LRU cache keyed on model name that keeps at most
`embedder_cache_size` live `Embedder` instances at any time.

Key design points, each traceable to `embedder_cache.py`:

1. **LRU eviction via `OrderedDict`** (line 26). `get_or_load` calls
   `move_to_end` on every cache hit (line 40) and `popitem(last=False)` when
   the cache exceeds capacity (line 73). The least-recently-used model is
   evicted first; recently used models stay warm.

2. **`asyncio.Lock` serialises all cache mutations** (line 27). A single
   `asyncio.Lock` (`self._lock`) guards both the `_cache` dict and the
   `_loading` dict. All reads and writes to these structures happen inside
   `async with self._lock` blocks. This prevents TOCTOU races between
   concurrent coroutines checking the cache and initiating a load.

3. **Concurrent-load deduplication via `asyncio.Event`** (lines 28, 44–56).
   When the first coroutine for a given model name acquires the lock and finds
   neither a cache hit nor an in-progress load, it registers an `asyncio.Event`
   in `self._loading` and breaks out of the lock to perform the load (line 50).
   Subsequent coroutines that arrive while the load is in progress find the
   event, release the lock, and `await event.wait()` outside the lock (line
   53). This avoids both deadlock (lock not held during the blocking
   `asyncio.to_thread` call) and duplicate model loads (only one loader per
   model name at a time). After the event fires, waiters loop back and re-check
   the cache (line 36).

4. **Model loading off the event loop** (line 60). `make_embedder` is
   dispatched via `asyncio.to_thread`, keeping the uvicorn event loop
   unblocked during ONNX model initialisation.

5. **Load failure handling** (lines 62–66). If `make_embedder` raises, the
   in-progress event is removed from `_loading` and the event is set, waking
   any waiters so they can retry as the next loader rather than deadlocking.

6. **`preload` for eager startup** (lines 79–89). `EmbedderCache.preload`
   accepts a list of model names and calls `get_or_load` for each
   concurrently via `asyncio.gather`. Failures are logged as warnings and
   skipped — a failed eager load does not abort startup.

**Lifecycle in `app.py`** (lines 103–108): `EmbedderCache` is constructed
inside the FastAPI `lifespan` context with capacity `config.embedder_cache_size`
and stored on `app.state.embedder_cache`. If `config.eager_load_embedders` is
`True`, the lifespan block queries `get_all_collections_meta`, collects the
distinct non-null `active_embedding_model` values, and calls
`embedder_cache.preload(list(distinct_models))` before the server begins
serving requests.

**Configuration** (`archon_search/config.py`, lines 68–69):

| Key (TOML `[database]`) | Type | Default | Constraint |
|---|---|---|---|
| `embedder_cache_size` | `int` | `3` | `>= 1` |
| `eager_load_embedders` | `bool` | `False` | — |

## Consequences

### Positive

- **Lazy loading**: models are loaded on first use. Collections whose models
  are never queried in a session incur no load cost.
- **Bounded memory**: at most `embedder_cache_size` ONNX model instances live
  in the process at once, regardless of how many distinct models are configured
  across collections.
- **Shared across requests**: concurrent requests for the same collection model
  block on a single load and then share the result; there is no per-request
  model instantiation.
- **Zero deadlocks under concurrency**: the `asyncio.Lock` + `asyncio.Event`
  pattern ensures the event loop is never blocked while waiting for a model
  load, and no coroutine can hold the lock across an `await`.
- **Opt-in eager preload**: operators who want sub-second first-query latency
  can set `eager_load_embedders = true`; this has no effect on deployments
  where cold-start latency is acceptable.

### Negative / Tradeoffs

- **Eviction latency spike**: when a model is evicted from the cache and later
  requested again, the next caller for that model pays the full cold-load
  penalty. With the default `embedder_cache_size = 3`, a deployment with more
  than three active collection models will experience periodic eviction misses.
  Operators with wider model diversity should raise `embedder_cache_size`.
- **Eager load complexity**: `eager_load_embedders = true` adds startup time
  proportional to the number of distinct models (sequential only if they share
  a concurrency bottleneck, since `preload` uses `asyncio.gather`). A slow or
  missing model download delays server readiness.
- **First-miss latency is visible**: the first request for an uncached model
  blocks in `asyncio.to_thread(make_embedder, ...)` until the ONNX runtime
  initialises. Callers observe elevated latency for that request; subsequent
  callers for the same model do not.
- **Evicted embedders are not explicitly closed**: `OrderedDict.popitem` drops
  the `Embedder` reference; the underlying `fastembed.TextEmbedding` object is
  garbage-collected at Python's discretion, not explicitly unloaded. This is
  acceptable given the single-process target deployment but means ONNX runtime
  memory may not be reclaimed immediately after eviction.

## Alternatives Considered

- **Per-request instantiation**: Construct a new `Embedder` on every search
  request for the collection's model. Rejected — model loading takes seconds,
  blocking `asyncio.to_thread` workers and producing duplicate in-memory
  instances for concurrent requests. Unacceptable latency and memory cost.

- **Global singleton (pre-C1 status quo)**: Keep one `ModelEmbedder` instance
  configured from `SearchConfig.embedding_model` and use it for all
  collections. Rejected for C1 — directly breaks the per-collection model
  requirement; collections with a different `active_embedding_model` would
  be embedded with the wrong model, producing dimension or semantic mismatches.

- **Fixed pool / full preload only**: Load all models declared across all
  collections at startup; no runtime loading. Rejected — this would block
  server startup for every model in use (including rarely queried ones), fail
  entirely if any model cannot be fetched at startup time, and not accommodate
  models added to new collections after startup without a restart. `preload` is
  offered as an opt-in overlay (`eager_load_embedders`) rather than the
  mandatory strategy.

## Cross-References

- [ADR 02 — fastembed for Dense Embeddings](02_fastembed_for_dense_embeddings.md):
  `EmbedderCache` caches `Embedder` objects whose backends are
  `ModelEmbedder` instances wrapping `fastembed.TextEmbedding`. The
  lazy-loading property described in ADR 02 (first call pays the ONNX-load
  cost) is what the cache amortises across requests.
- [ADR 04 — Multi-Collection Router with Centroid Pre-Ranking](04_multi_collection_router_with_centroid_preranking.md):
  the router's centroid pre-ranking requires embedding a query with the
  active model; per-collection models mean a query may need an embedder that
  differs from the global default. `EmbedderCache` provides the lookup path
  for that retrieval.
- Config keys: `[database] embedder_cache_size` (capacity, default `3`) and
  `[database] eager_load_embedders` (opt-in preload at startup, default
  `false`). Both are parsed in `archon_search/config.py` and validated
  (`embedder_cache_size >= 1`; `eager_load_embedders` must be a boolean).

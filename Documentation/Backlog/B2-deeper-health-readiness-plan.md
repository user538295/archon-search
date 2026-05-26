# B2 — Deeper Health and Readiness

**Purpose**: Replace the single dependency-blind `/health` probe with a proper liveness/readiness split: keep `/health` as the unchanged liveness signal and add `GET /ready` as a storage-gated readiness probe, enriching authenticated `/status` with a `readiness` sub-object covering storage connectivity, model warm-status, job queue depth, index-build state, and watcher state.
**Audience**: archon-search contributors implementing B2 and operators/orchestrators consuming the new endpoints.
**Status**: To Do

---

## Background

`GET /health` returns a hard-coded `{"status": "running", "version": <vcs>}` the instant the event loop responds — it never touches LanceDB, the embedder, the reranker, the indexing state, the job store, or the watcher. `GET /status` is richer but requires a Bearer token and currently reports placeholder zeros (`doc_count: 0`, `chunk_count: 0`, `path: ""`). There is no `/ready`, `/readyz`, or `/livez` anywhere. A supervisor or load balancer cannot distinguish "process answers TCP" from "process can serve a search". Item 22 ("operators need this before scaling load") and `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` both call for this split.

## Goal

When B2 ships: `GET /ready` returns `200 {ready: true, checks: {storage: "ok"}}` when `SearchStore` is connected, and `503 {ready: false, checks: {storage: "fail"}}` when the storage ping fails. It requires no Bearer token, leaks no collection names or counts, and is the sole unauthenticated probe that tells a supervisor whether the service can actually serve traffic. Authenticated `GET /status` gains a `readiness` sub-object with `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs.{pending, running}`, `collections_indexing`, `collections_failed`, and `watcher`. `/health` is byte-for-byte unchanged. The 85 % coverage gate holds on the default pytest run.

---

## Scope

### In Scope
- `SearchStore.ping()` — async, timeout-guarded, short-TTL cached, returns `bool`, never raises.
- `is_warm` accessors on `Embedder`, `ModelEmbedder`, `Reranker`, `ModelReranker` (side-effect-free; `_model is not None`). Also updates existing `_MockBackend` and `_MockRerankerBackend` in tests to add `is_warm` (required since both Protocols are `@runtime_checkable`).
- `SearchPipeline.reranker_is_warm` and `SearchPipeline.embedder_is_warm` properties (avoids reaching into pipeline privates from route handlers; both warm-status reads go through the pipeline seam).
- Job-count aggregation: `pending` / `running` counts from `JobStore.list()` filtered inline by `JobStatus`.
- Pydantic schemas: `CheckStatus` enum (`"ok"` / `"fail"`), `ReadinessResponse` (terse, for `/ready`), `ReadinessDetail` (rich, for `/status`), `StatusResponse.readiness` field.
- `GET /ready` endpoint: unauthenticated via `_EXEMPT_PATHS`, `200` / `503`, body is `ReadinessResponse` (not `ErrorDetail`).
- `app.state.watcher_manager` slot (default `None`) read by `/status` and `/ready` rendering.
- Enrich `GET /status` with the `readiness` sub-object (storage-connected, warm booleans, queue depth, index-state counts, watcher report).
- Adding `/ready` to `_EXEMPT_PATHS` in `middleware_auth.py` and verifying OpenAPI omits `BearerAuth` for it.
- Docs: security doc 150, ops doc 160, error doc 140, `BREAKING.md`.
- Tests: unit, contract/snapshot, integration (`@pytest.mark.integration`).

### Out of Scope
- Wiring a live `WatcherManager` into the server lifecycle (B2 only adds the hook slot).
- Per-stage latency / tracing / correlation IDs (B1).
- Configurable readiness gating (`require_warm_models`) — fixed storage-only gate for now.
- Queue-depth thresholding / backpressure policy (D1).
- Forcing/triggering a model load to satisfy readiness (explicitly forbidden).
- Install-time / background provider validation (D6).
- `state_store_ok` field — `IndexingStateStore.read()` cannot distinguish corrupt from empty.
- Populating `path` / `doc_count` / `chunk_count` placeholders on `/status` per-collection entries (fast-follow).
- CLI `archon-search status --probe` / `archon-search ready` command (future iteration).
- MCP surface changes (no health/readiness MCP tool).
- Cross-process readiness aggregation.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 7.1 — Final verification & documentation update].

---

## What does NOT change
- `GET /health` response body, status code, and behavior — byte-for-byte identical.
- The `cli/install_cmd.py` poll loop (polls `/health` for up to 60 s).
- Existing per-collection `watching` flag on `/status` (stays as `config.watch`).
- MCP tool set (stays at 10 tools: search, search_with_context, explain, ingest_file, ingest_directory, list_collections, get_collections_meta, get_collection_meta, list_documents, delete_document).
- All existing `/status` fields and semantics (additive only).
- SQL predicate rules in `store.py` (`_where_eq` / `_where_in` — B2 does not touch store query paths).

---

## Known limitations / accepted trade-offs
- TTL cache staleness (~1 s): a cached `True` can lag a just-dropped connection by up to the TTL. Acceptable for a readiness signal; documented.
- **Ping cache is not task-safe under concurrent calls**: the module-level `_ping_cache` check-and-update straddles an `asyncio.wait_for` suspension point. Concurrent `ping()` calls within the TTL window may both call `list_tables()`. This is benign (idempotent result, GIL-safe write), but the cache's "at most one call per TTL" guarantee is best-effort, not strict. If strict deduplication is required, a per-process `asyncio.Lock` must guard the full check→await→update sequence. For B2, best-effort is accepted.
- **Module-level cache is not per-instance**: tests that create multiple `SearchStore` instances (e.g., via `tmp_path`) share the module-level `_ping_cache`. `__init__` cannot reliably reset a module-level variable without a `global` declaration. The correct test isolation approach is an autouse pytest fixture in `tests/test_store_ping.py`:
  ```python
  import archon_search.store as _store_mod
  @pytest.fixture(autouse=True)
  def reset_ping_cache():
      _store_mod._ping_cache = None
      yield
      _store_mod._ping_cache = None
  ```
  Do NOT attempt to reset `_ping_cache` inside `SearchStore.__init__` — that would reset the shared cache on every store construction, breaking the intended per-process singleton behaviour.
- `IndexingStateStore.read()` cannot distinguish corrupt from empty (returns `None` for both) — B2 surfaces no `state_store_ok` field and the state store never gates `/ready`.
- `watcher` report is `{running: false}` until a future item wires a live `WatcherManager` into the server. The legacy per-collection `watching` flag (`= config.watch`) being `true` while `readiness.watcher.running` is `false` is the expected state and is not a bug. The `watching_names()` method is the informal interface contract between this plan and the future live `WatcherManager`; it must return an iterable of collection name strings.
- The new `readiness` block in `/status` displayed next to the misleading `doc_count: 0` placeholders could confuse operators — the count-fill is recommended as a fast-follow.
- **OpenAPI doc is unauthenticated and discloses the full API surface**: `GET /openapi.json` is in `_EXEMPT_PATHS`. Any anonymous caller can enumerate all endpoint names, shapes, and security annotations. This is an existing accepted trade-off for the project (developer convenience); B2 does not change this posture. The `/ready` body itself discloses only `ready + checks.storage`.
- **Adding `readiness: ReadinessDetail` as a required field to `StatusResponse` is a breaking change for any code that constructs `StatusResponse(...)` directly** (e.g., existing tests). All existing `StatusResponse(...)` call sites must be updated to pass `readiness=ReadinessDetail(...)`. The JSON consumer contract is additive-only; Python model construction is not.
- **`response_model=ReadinessResponse` on `GET /ready` does not validate the actual response body**: FastAPI bypasses `response_model` validation when the handler returns a `JSONResponse` directly. The `response_model` annotation only affects OpenAPI schema generation. Implementers must not rely on it for body correctness — the test suite pins the body shape instead.

---

## Architecture

### New modules / classes / functions

**`archon_search/store.py`** — `SearchStore.ping(self) -> bool`
- Calls `await asyncio.wait_for(self._db.list_tables(), timeout=PING_TIMEOUT_SECONDS)` and accesses `.tables` on the result (confirms the call returned a valid object, not an exception-swallowed None).
- Returns `False` (not raises) when `self._db is None`, when `list_tables()` raises, or when the timeout fires.
- Caches **both** `True` and `False` results for `PING_TTL_SECONDS` (module-level `_ping_cache: tuple[float, bool] | None = None`; reset on each `connect()` / `disconnect()`). A cached `False` within TTL returns immediately without re-calling `list_tables()`.
- Cache is best-effort under concurrent async calls (no lock); see Known Limitations.
- Constants in `archon_search/constants.py`: `PING_TIMEOUT_SECONDS: float = 2.0`, `PING_TTL_SECONDS: float = 1.0`.

**`archon_search/embedder.py`** — `ModelEmbedder.is_warm` (property) + `Embedder.is_warm` (property)
- `ModelEmbedder.is_warm -> bool`: returns `self._model is not None`. No lock acquisition, no model load.
- `Embedder.is_warm -> bool`: delegates to `self._backend.is_warm` (the `EmbedderBackend` Protocol gains `is_warm: bool` as an attribute/property).
- **Breaking impact on existing tests**: `EmbedderBackend` is `@runtime_checkable`. Adding `is_warm` to the Protocol means `isinstance(obj, EmbedderBackend)` will check for the `is_warm` attribute. The existing `_MockBackend` in `tests/test_embedder.py` currently passes `isinstance(backend, EmbedderBackend)` — it will fail after this change unless `is_warm` is added to `_MockBackend`. The mock must be updated as part of Task 2.1.

**`archon_search/reranker.py`** — `ModelReranker.is_warm` (property) + `Reranker.is_warm` (property)
- Same pattern. `ModelReranker.is_warm -> bool`: returns `self._model is not None`.
- `Reranker.is_warm -> bool`: delegates to `self._backend.is_warm`.
- **Breaking impact on existing tests**: same as embedder — `_MockRerankerBackend` in `tests/test_reranker.py` must gain `is_warm` as part of Task 2.2.

**`archon_search/pipeline.py`** — `SearchPipeline.reranker_is_warm` and `SearchPipeline.embedder_is_warm` (properties)
- `reranker_is_warm`: returns `self._reranker.is_warm`. Provides a clean seam for route handlers without exposing pipeline internals.
- `embedder_is_warm`: returns `self._embedder.is_warm`. Same rationale — route handlers must not reach into pipeline privates. Task 6.1 must use `app.state.pipeline.embedder_is_warm`, NOT `app.state.embedder.is_warm` (even though `app.state.embedder` exists in `create_app`, bypassing the pipeline abstraction violates the stated design principle and creates a second path to the same object).

**`archon_search/server/schemas.py`** — new Pydantic models
```python
from enum import Enum

class CheckStatus(str, Enum):
    OK = "ok"
    FAIL = "fail"

class ReadinessChecks(BaseModel):
    storage: CheckStatus

class ReadinessResponse(BaseModel):          # terse — for GET /ready (unauthenticated)
    ready: bool
    checks: ReadinessChecks

class WatcherReport(BaseModel):
    running: bool
    watching: list[str] = []                 # sorted; empty when running=False

class JobCounts(BaseModel):
    pending: int
    running: int

class ReadinessDetail(BaseModel):            # rich — for GET /status (authenticated)
    storage_connected: bool
    embedder_warm: bool
    reranker_warm: bool
    jobs: JobCounts
    collections_indexing: int
    collections_failed: int
    watcher: WatcherReport

# StatusResponse gains:
class StatusResponse(BaseModel):
    running: bool
    pid: int
    version: str
    collections: list[StatusCollectionEntry]
    readiness: ReadinessDetail               # NEW
```

**`archon_search/server/routes_ready.py`** — new file
- `router = APIRouter()`
- `GET /ready` handler: calls `request.app.state.search_store.ping()`, returns `ReadinessResponse` at `200` or `JSONResponse(ReadinessResponse(...).model_dump(), status_code=503)`.

**`archon_search/server/middleware_auth.py`** — add `"/ready"` to `_EXEMPT_PATHS`.

**`archon_search/server/app.py`** — register `routes_ready.router`; add `app.state.watcher_manager = None` in `create_app`.

**`archon_search/server/routes_status.py`** — enrich return value with `readiness=ReadinessDetail(...)` populated by calling `ping()`, reading `is_warm` accessors, counting `JobStore.list()` by `JobStatus`, deriving index-state counts from `IndexingStateStore.read()`, and reading `app.state.watcher_manager`.

### Data flow (GET /ready)
```
probe → APIKeyMiddleware (exempt) → routes_ready.health_ready()
  → SearchStore.ping() [TTL-cached, timeout-guarded]
  → ReadinessResponse(ready=True/False, checks=ReadinessChecks(storage="ok"/"fail"))
  → HTTP 200 or 503
```

### Data flow (GET /status enrichment)
```
authenticated caller → routes_status.status()
  → existing per-collection logic (unchanged)
  → SearchStore.ping()                    [storage_connected]
  → app.state.pipeline.embedder_is_warm   [embedder_warm]  ← via pipeline seam, not app.state.embedder
  → app.state.pipeline.reranker_is_warm   [reranker_warm]
  → job_store.count_by_status()           [jobs.pending, jobs.running]
  → state_store.read() (already loaded)   [collections_indexing, collections_failed]
  → app.state.watcher_manager             [watcher]
  → StatusResponse(readiness=ReadinessDetail(...), ...)
```
Note: `state_store = request.app.state.state_store` and `state = state_store.read()` are already present in `routes_status.py` at lines 37–38. The `readiness` block reads from the already-loaded `state` variable — no additional I/O for the state store.

### New config / constants
- `PING_TIMEOUT_SECONDS: float = 2.0` in `archon_search/constants.py`
- `PING_TTL_SECONDS: float = 1.0` in `archon_search/constants.py`

---

## Task breakdown

### Phase 1 — Storage ping
> **Releasable**: after Task 1.2, `SearchStore.ping()` is fully tested and callable by any consumer without side effects.

#### Task 1.1 — Constants for ping timeout and TTL
- [ ] **File**: `archon_search/constants.py`
- **Depends on**: nothing
- **Description**:
  - Add two module-level float constants:
    - `PING_TIMEOUT_SECONDS: float = 2.0` — `asyncio.wait_for` timeout used by `SearchStore.ping()`.
    - `PING_TTL_SECONDS: float = 1.0` — in-process cache TTL for the ping result.
  - These are the only place these values live; `store.py` imports them.
  - No config-file promotion for B2 (fixed constants, not user-configurable).
- **Releasable**: after this task, constants are importable by `store.py`.
- **Tests (TDD)** — `tests/test_constants.py`:
  - Unit: `test_ping_timeout_seconds_is_float` — assert `PING_TIMEOUT_SECONDS` is a `float` and `> 0`.
  - Unit: `test_ping_ttl_seconds_is_float` — assert `PING_TTL_SECONDS` is a `float` and `> 0`.
  - Checkpoint: `uv run pytest tests/test_constants.py -x -v`

#### Task 1.2 — `SearchStore.ping()` with TTL cache
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add module-level `_ping_cache: tuple[float, bool] | None = None` (monotonic timestamp + bool). Use `time.monotonic()` for the timestamp.
  - `SearchStore.ping(self) -> bool` (async):
    - If `self._db is None`: return `False` immediately (no cache update).
    - Check `_ping_cache`: if `_ping_cache is not None` and `time.monotonic() - _ping_cache[0] < PING_TTL_SECONDS`, return `_ping_cache[1]`.
    - Call `asyncio.wait_for(self._db.list_tables(), timeout=PING_TIMEOUT_SECONDS)` inside a `try/except (asyncio.TimeoutError, Exception)` — any exception → update cache with `False`, return `False`.
    - On success, access `.tables` attribute (confirms the call returned a valid object), update cache with `True`, return `True`.
    - The cache is module-level (per-process, not per-instance); acceptable since there is one store instance per process.
    - **Cache concurrency note**: the check-and-update straddles an `await` point. Concurrent calls may both miss the cache and call `list_tables()` — this is benign (idempotent). The "at most one call per TTL" guarantee is best-effort, not strict. No lock is required for B2.
    - **Cache False caching**: both `True` and `False` results update the cache. A failed ping within TTL returns the cached `False` without re-calling `list_tables()`.
  - Reset cache (`global _ping_cache; _ping_cache = None`) at the top of both `connect()` and `disconnect()` to prevent stale `True` after reconnect or shutdown. Use `global _ping_cache` to correctly target the module-level variable.
  - Do NOT reset `_ping_cache` in `__init__` — that would reset shared state on every store construction, defeating the singleton cache. Test isolation is handled by the autouse fixture in `tests/test_store_ping.py` (see Known Limitations).
  - Import: `import time`, `import asyncio`, `from archon_search.constants import PING_TIMEOUT_SECONDS, PING_TTL_SECONDS`.
- **Releasable**: after this task, `SearchStore.ping()` is callable and returns reliable bool.
- **Tests (TDD)** — `tests/test_store_ping.py` (new file; each test resets `archon_search.store._ping_cache = None` in a fixture or at the start):
  - Unit: `test_ping_returns_true_when_connected` — mock `self._db.list_tables()` to return an object with `.tables = []`; assert `ping()` returns `True`.
  - Unit: `test_ping_returns_false_when_db_is_none` — create a `SearchStore` without calling `connect()`; assert `ping()` returns `False`.
  - Unit: `test_ping_returns_false_on_list_tables_exception` — mock `list_tables()` to raise `RuntimeError`; assert `ping()` returns `False` (does not raise).
  - Unit: `test_ping_returns_false_on_timeout` — mock `list_tables()` to sleep longer than `PING_TIMEOUT_SECONDS`; assert `ping()` returns `False`.
  - Unit: `test_ping_ttl_cache_prevents_second_call_on_true` — mock `list_tables()`, call `ping()` twice within TTL; assert `list_tables()` called exactly once (TTL cache hit on second call returns `True`).
  - Unit: `test_ping_ttl_cache_prevents_second_call_on_false` — mock `list_tables()` to raise, call `ping()` twice within TTL; assert `list_tables()` called exactly once (TTL cache hit on second call returns cached `False`).
  - Unit: `test_ping_ttl_cache_expires` — patch `archon_search.store.time` with a mock whose `monotonic()` returns values that advance beyond `PING_TTL_SECONDS` between the first and second call; assert `list_tables()` called twice (cache expired between calls). Patch target is `archon_search.store.time`, not `time.monotonic` directly.
  - Unit: `test_ping_cache_reset_on_disconnect` — call `ping()` to prime cache with `True`; call `disconnect()` (which resets `_ping_cache` to `None`); call `ping()` again (with `_db = None` after disconnect); assert second call returns `False`. Note: the `False` return here is via the `if self._db is None: return False` short-circuit path, not a new call to `list_tables()` — assert `list_tables()` was only called once total.
  - Unit: `test_ping_cache_reset_on_connect` — prime cache with `True`, call `connect()` (mocked); assert `list_tables()` is called again on next `ping()` (cache was cleared by `connect()`).
  - Integration (`@pytest.mark.integration`): `test_ping_true_against_live_store` — use a real `SearchStore` with `tmp_path`; `connect()`, assert `ping()` returns `True`; `disconnect()`, assert `ping()` returns `False`.
  - Checkpoint: `uv run pytest tests/test_store_ping.py -x -v`

---

### Phase 2 — Model warm-status accessors
> **Releasable**: after Task 2.3, all warm-status accessors are testable and the "reading `is_warm` never loads the model" invariant is enforced.

#### Task 2.1 — `is_warm` on `ModelEmbedder` and `Embedder`
- [ ] **File**: `archon_search/embedder.py`
- **Depends on**: nothing
- **Description**:
  - Add `is_warm` as a `bool` property to `EmbedderBackend` Protocol: `@property def is_warm(self) -> bool: ...`
  - Add `@property def is_warm(self) -> bool: return self._model is not None` to `ModelEmbedder`. No lock acquisition. No model load.
  - Add `@property def is_warm(self) -> bool: return self._backend.is_warm` to `Embedder`.
  - **Update existing `_MockBackend` in `tests/test_embedder.py`**: add `is_warm: bool = False` (or a `@property`) to `_MockBackend`. This is required because `EmbedderBackend` is `@runtime_checkable` — adding `is_warm` to the Protocol means `isinstance(backend, EmbedderBackend)` will fail for any backend that lacks `is_warm`. The existing `test_embedder_backend_protocol` test asserts `isinstance(backend, EmbedderBackend)` and will break without this update.
  - No other changes.
- **Releasable**: after this task, callers can read `embedder.is_warm` without side effects.
- **Tests (TDD)** — `tests/test_embedder.py`:
  - Unit: `test_model_embedder_is_warm_false_before_encode` — create `ModelEmbedder("some-model")`; assert `is_warm` is `False`; assert `_model` is still `None` after the read.
  - Unit: `test_model_embedder_is_warm_true_after_model_set` — set `me._model = object()`; assert `is_warm` is `True`.
  - Unit: `test_embedder_is_warm_delegates_to_backend` — create a fake backend with `is_warm = False`; wrap in `Embedder`; assert `embedder.is_warm` is `False`. Set backend `is_warm = True`; assert `embedder.is_warm` is `True`.
  - Unit: `test_reading_is_warm_does_not_construct_TextEmbedding` — patch `fastembed.TextEmbedding` with a mock that raises on construction; read `ModelEmbedder("x").is_warm`; assert no exception (TextEmbedding was not called).
  - Unit: `test_reading_is_warm_does_not_acquire_lock` — call `is_warm` while `_lock` is held by another thread (use `threading.Thread` that acquires `me._lock` and waits); assert `is_warm` returns without blocking (use a timeout assertion or verify `_lock.locked()` during the call).
  - Unit: `test_mock_backend_satisfies_protocol_after_is_warm_added` — assert `isinstance(_MockBackend(), EmbedderBackend)` still passes (regression guard for the mock update).
  - Checkpoint: `uv run pytest tests/test_embedder.py -x -v`

#### Task 2.2 — `is_warm` on `ModelReranker` and `Reranker`
- [ ] **File**: `archon_search/reranker.py`
- **Depends on**: nothing
- **Description**:
  - Add `is_warm` as a `bool` property to `RerankerBackend` Protocol: `@property def is_warm(self) -> bool: ...`
  - Add `@property def is_warm(self) -> bool: return self._model is not None` to `ModelReranker`. No lock. No model load. Keyed on `_model`, not `_model_name`.
  - Add `@property def is_warm(self) -> bool: return self._backend.is_warm` to `Reranker`.
  - **Update existing `_MockRerankerBackend` in `tests/test_reranker.py`**: add `is_warm: bool = False` to `_MockRerankerBackend`. Same Protocol/isinstance rationale as Task 2.1 — `test_reranker_backend_protocol` will break without this update.
- **Releasable**: after this task, `reranker.is_warm` is readable without side effects.
- **Tests (TDD)** — `tests/test_reranker.py`:
  - Unit: `test_model_reranker_is_warm_false_before_predict` — create `ModelReranker("some-model")`; assert `is_warm` is `False`; assert `_model` is still `None`.
  - Unit: `test_model_reranker_is_warm_true_after_model_set` — set `mr._model = object()`; assert `is_warm` is `True`.
  - Unit: `test_reranker_is_warm_delegates_to_backend` — fake backend; assert delegation.
  - Unit: `test_reading_reranker_is_warm_does_not_construct_TextCrossEncoder` — patch `fastembed.rerank.cross_encoder.TextCrossEncoder` to raise; read `ModelReranker("x").is_warm`; assert no exception.
  - Unit: `test_reading_reranker_is_warm_does_not_acquire_lock` — call `is_warm` while `_lock` is held by another thread; assert returns without blocking.
  - Unit: `test_mock_reranker_backend_satisfies_protocol_after_is_warm_added` — assert `isinstance(_MockRerankerBackend(), RerankerBackend)` still passes.
  - Checkpoint: `uv run pytest tests/test_reranker.py -x -v`

#### Task 2.3 — `SearchPipeline.reranker_is_warm` and `SearchPipeline.embedder_is_warm` properties
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.1, Task 2.2
- **Description**:
  - Add `@property def reranker_is_warm(self) -> bool: return self._reranker.is_warm` to `SearchPipeline`.
  - Add `@property def embedder_is_warm(self) -> bool: return self._embedder.is_warm` to `SearchPipeline`.
  - These are the only warm-status accessors that route handlers call. Handlers must NOT access `app.state.embedder.is_warm` directly (even though `app.state.embedder` exists in `create_app`) — the pipeline provides the single clean seam for both model objects.
- **Releasable**: after this task, both `app.state.pipeline.reranker_is_warm` and `app.state.pipeline.embedder_is_warm` are callable from route handlers.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_pipeline_reranker_is_warm_false_when_cold` — build a `SearchPipeline` with a fake reranker whose `is_warm` is `False`; assert `pipeline.reranker_is_warm` is `False`.
  - Unit: `test_pipeline_reranker_is_warm_true_when_warm` — fake reranker `is_warm = True`; assert `True`.
  - Unit: `test_pipeline_embedder_is_warm_false_when_cold` — build a `SearchPipeline` with a fake embedder whose `is_warm` is `False`; assert `pipeline.embedder_is_warm` is `False`.
  - Unit: `test_pipeline_embedder_is_warm_true_when_warm` — fake embedder `is_warm = True`; assert `True`.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -x -v`

---

### Phase 3 — Job-count aggregation
> **Releasable**: after Task 3.1, the aggregation logic is tested in isolation and ready to be called from route handlers.

#### Task 3.1 — `pending` / `running` count helper (inline aggregation)
- [ ] **File**: `archon_search/jobs/store.py`
- **Depends on**: nothing
- **Description**:
  - Add `def count_by_status(self) -> dict[JobStatus, int]` to `JobStore`. Returns a dict mapping every `JobStatus` member to its count (zero-filled for members with no jobs). Implementation: `from collections import Counter; counts = Counter(j.status for j in self._jobs.values()); return {s: counts.get(s, 0) for s in JobStatus}`.
  - This is a thin, pure convenience — all the state is already in `self._jobs`. Route handlers call `job_store.count_by_status()` and read `.get(JobStatus.PENDING, 0)` and `.get(JobStatus.RUNNING, 0)`.
  - Note: this helper is a convenience; route handlers may also inline the filter, but `count_by_status()` makes the aggregation independently testable.
- **Releasable**: after this task, `job_store.count_by_status()` is callable and testable.
- **Tests (TDD)** — `tests/test_job_store.py`:
  - Unit: `test_count_by_status_empty_store` — new `JobStore` (tmp path); assert all statuses return `0`.
  - Unit: `test_count_by_status_mixed` — create jobs with various statuses; assert correct counts for `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`, `CANCELLING`.
  - Unit: `test_count_by_status_includes_all_status_members` — assert every `JobStatus` member is a key in the returned dict (zero-filled).
  - Unit: `test_count_by_status_excludes_evicted_jobs` — create a job, then manually set its `updated_at` to 8 days ago (beyond the 7-day eviction window), then trigger eviction by calling `_write_atomic()` or creating a new `JobStore` instance that reloads the file; assert `count_by_status()` returns `0` for all statuses (evicted job is not counted). This guards against a regression where eviction is bypassed in `count_by_status()`.
  - Checkpoint: `uv run pytest tests/test_job_store.py -x -v`

---

### Phase 4 — Pydantic schemas
> **Releasable**: after Task 4.1, all new response models are importable and snapshot-testable.

#### Task 4.1 — `CheckStatus`, `ReadinessResponse`, `ReadinessDetail`, `StatusResponse.readiness`
- [ ] **File**: `archon_search/server/schemas.py`
- **Depends on**: nothing
- **Description**:
  - Add `CheckStatus(str, Enum)` with members `OK = "ok"` and `FAIL = "fail"`.
  - Add `ReadinessChecks(BaseModel)`: `storage: CheckStatus`.
  - Add `ReadinessResponse(BaseModel)`: `ready: bool`, `checks: ReadinessChecks`. This is the terse unauthenticated body.
  - Add `WatcherReport(BaseModel)`: `running: bool`, `watching: list[str] = []`.
  - Add `JobCounts(BaseModel)`: `pending: int`, `running: int`.
  - Add `ReadinessDetail(BaseModel)`: `storage_connected: bool`, `embedder_warm: bool`, `reranker_warm: bool`, `jobs: JobCounts`, `collections_indexing: int`, `collections_failed: int`, `watcher: WatcherReport`.
  - Extend `StatusResponse`: add `readiness: ReadinessDetail` as a required field.
  - Import `from enum import Enum` (already present if `CheckStatus` uses it; otherwise add).
  - No changes to any existing field on any existing model.
- **Releasable**: after this task, all new schemas are importable and Pydantic-validated.
- **Tests (TDD)** — `tests/contract/test_readiness_schemas.py` (new file):
  - Unit: `test_readiness_response_ok_shape` — instantiate `ReadinessResponse(ready=True, checks=ReadinessChecks(storage=CheckStatus.OK))`; assert `.model_dump() == {"ready": True, "checks": {"storage": "ok"}}`.
  - Unit: `test_readiness_response_fail_shape` — same with `ready=False`, `storage=CheckStatus.FAIL`; assert correct dict.
  - Unit: `test_readiness_detail_shape` — instantiate `ReadinessDetail` with all fields; assert `.model_dump()` contains all expected keys.
  - Unit: `test_status_response_has_readiness_field` — instantiate `StatusResponse` with a `readiness=ReadinessDetail(...)` field; assert the field is present in `.model_dump()`.
  - Unit: `test_watcher_report_empty_by_default` — `WatcherReport(running=False)`; assert `watching == []`.
  - Unit: `test_check_status_values` — assert `CheckStatus.OK.value == "ok"` and `CheckStatus.FAIL.value == "fail"`.
  - Snapshot test: serialise `ReadinessResponse` (both 200 and 503 shapes) and `ReadinessDetail` (all zeros, typical values) to JSON strings; pin with `assert json_str == <expected>` (inline — no snapshot library needed for a simple schema test).
  - Checkpoint: `uv run pytest tests/contract/test_readiness_schemas.py -x -v`

---

### Phase 5 — `GET /ready` endpoint
> **Releasable**: after Task 5.2, `GET /ready` is live, unauthenticated, and fully tested.

#### Task 5.1 — Add `/ready` to `_EXEMPT_PATHS`
- [ ] **File**: `archon_search/server/middleware_auth.py`
- **Depends on**: nothing
- **Description**:
  - Change `_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})` to include `"/ready"`.
  - No other changes to this file.
- **Releasable**: after this task, requests to `/ready` bypass the Bearer-token check.
- **Tests (TDD)** — `tests/test_routes_health.py` (or a new `tests/test_routes_ready.py`):
  - Unit: `test_ready_reachable_without_bearer_token` — `GET /ready` with no `Authorization` header; assert `status_code != 401`. (Route does not exist yet but the middleware exemption will pass the request through — expect 404 until Task 5.2, which is fine since this test can live in the Task 5.2 test file and run after the route exists.)
  - Checkpoint: run after Task 5.2.

#### Task 5.2 — `GET /ready` route handler and `app.state.watcher_manager` slot
- [ ] **File**: `archon_search/server/routes_ready.py` (new file), `archon_search/server/app.py`
- **Depends on**: Task 1.2, Task 4.1, Task 5.1
- **Description**:
  - **New file** `archon_search/server/routes_ready.py`:
    ```python
    """GET /ready — unauthenticated readiness probe."""
    from __future__ import annotations
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
    from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

    router = APIRouter()

    @router.get("/ready", response_model=ReadinessResponse)
    async def ready(request: Request) -> JSONResponse:
        store = request.app.state.search_store
        storage_ok = await store.ping()
        body = ReadinessResponse(
            ready=storage_ok,
            checks=ReadinessChecks(storage=CheckStatus.OK if storage_ok else CheckStatus.FAIL),
        )
        status_code = 200 if storage_ok else 503
        return JSONResponse(body.model_dump(), status_code=status_code)
    ```
  - **`app.py` changes**:
    - Import `from archon_search.server.routes_ready import router as ready_router`.
    - Add `app.state.watcher_manager = None` after the other `app.state.*` assignments in `create_app`.
    - Add `app.include_router(ready_router)` alongside the other `include_router` calls.
  - The `watcher_manager` slot is `None` by default (no live watcher in B2); `/status` and future items read from it.
  - The `response_model=ReadinessResponse` on the route ensures OpenAPI documents the shape; the actual return is a `JSONResponse` so we control the status code.
- **Releasable**: after this task, `GET /ready` is live and unauthenticated.
- **Tests (TDD)** — `tests/test_routes_ready.py` (new file):
  - Unit: `test_ready_returns_200_when_storage_ok` — mock `SearchStore.ping` to return `True`; `GET /ready`; assert `status_code == 200` and `body["ready"] is True` and `body["checks"]["storage"] == "ok"`.
  - Unit: `test_ready_returns_503_when_storage_fails` — mock `ping` to return `False`; assert `status_code == 503` and `body["ready"] is False` and `body["checks"]["storage"] == "fail"`.
  - Unit: `test_ready_reachable_without_bearer_token` — `GET /ready` with no auth header; assert `status_code != 401`.
  - Unit (negative — pins no-info-leak): `test_ready_body_contains_no_collection_names_or_counts` — parse body as JSON string; use `json.dumps(body)` and assert none of `["name", "path", "doc_count", "chunk_count", "pending", "running", "collections_indexing", "collections_failed", "embedder_warm", "reranker_warm", "watching"]` appear as substrings in the serialized JSON. This is more reliable than recursive key traversal and fails if any leaky field is added.
  - Unit: `test_ready_503_body_is_readiness_response_not_error_detail` — mock `ping` to return `False`; assert `"ready" in body` and `"checks" in body` and `"detail" not in body`. Pins the "503-is-a-state-not-an-error" decision (contrast A3's `{"detail": ...}` envelope).
  - Unit: `test_ready_appears_in_openapi_without_bearer_auth` — retrieve `/openapi.json`; find the `/ready` path; assert it has no `security` annotation (or `security` is absent / empty). Also assert the OpenAPI response schema for `/ready` 200 contains `ready` and `checks` (pins that `response_model=ReadinessResponse` correctly documents the shape even though the handler returns `JSONResponse`).
  - Unit: `test_watcher_manager_slot_is_none_by_default` — after `create_app`, assert `app.state.watcher_manager is None`.
  - Integration (`@pytest.mark.integration`): `test_ready_returns_503_after_store_disconnect` — start a real store, call `connect()`, call `disconnect()`, then `GET /ready`; assert `status_code == 503`. (Note: this tests the post-disconnect state, not a true concurrent mid-flight race, which is out of scope for B2.)
  - Checkpoint: `uv run pytest tests/test_routes_ready.py -x -v`

---

### Phase 6 — Enrich authenticated `/status`
> **Releasable**: after Task 6.1, authenticated `GET /status` includes the full `readiness` sub-object.

#### Task 6.1 — Add `readiness` sub-object to `/status` handler
- [ ] **File**: `archon_search/server/routes_status.py`
- **Depends on**: Task 1.2, Task 2.1, Task 2.3, Task 3.1, Task 4.1, Task 5.2
- **Description**:
  - Import new schemas and helpers: `ReadinessDetail`, `WatcherReport`, `JobCounts`, `CheckStatus`.
  - Import `JobStatus` from `archon_search.types`.
  - Import `IndexingStatus` from `archon_search.progress`.
  - At the end of the `status()` handler, before constructing `StatusResponse`, compute:
    ```python
    # Storage ping
    storage_ok = await request.app.state.search_store.ping()

    # Model warm-status (side-effect-free) — via pipeline seam, not app.state.embedder directly
    embedder_warm: bool = request.app.state.pipeline.embedder_is_warm
    reranker_warm: bool = request.app.state.pipeline.reranker_is_warm

    # Job counts
    job_store = request.app.state.job_store
    job_counts = job_store.count_by_status()
    jobs = JobCounts(
        pending=job_counts.get(JobStatus.PENDING, 0),
        running=job_counts.get(JobStatus.RUNNING, 0),
    )

    # Index-state counts (state already loaded above as `state`)
    collections_indexing = 0
    collections_failed = 0
    if state:
        for cp in state.collections.values():
            if cp.status == IndexingStatus.IN_PROGRESS:
                collections_indexing += 1
            elif cp.status == IndexingStatus.FAILED:
                collections_failed += 1

    # Watcher report
    wm = request.app.state.watcher_manager
    if wm is None:
        watcher = WatcherReport(running=False)
    else:
        watcher = WatcherReport(running=True, watching=sorted(wm.watching_names()))

    readiness = ReadinessDetail(
        storage_connected=storage_ok,
        embedder_warm=embedder_warm,
        reranker_warm=reranker_warm,
        jobs=jobs,
        collections_indexing=collections_indexing,
        collections_failed=collections_failed,
        watcher=watcher,
    )
    ```
  - Pass `readiness=readiness` to `StatusResponse(...)`.
  - No changes to per-collection entries or any existing field.
- **Backward-compat note**: `readiness: ReadinessDetail` is a required field on `StatusResponse`. All existing `StatusResponse(running=..., pid=..., version=..., collections=...)` call sites (including in existing tests) must be updated to pass `readiness=ReadinessDetail(...)`. Search the codebase for `StatusResponse(` before implementing and update all instantiation sites.
- **Releasable**: after this task, authenticated `/status` includes the full `readiness` sub-object.
- **Tests (TDD)** — `tests/test_routes_status.py` (extend existing file):
  - Unit: `test_status_readiness_block_present` — authenticated `/status`; assert `"readiness" in response.json()`.
  - Unit: `test_status_readiness_has_all_fields` — assert `readiness` dict contains exactly `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs`, `collections_indexing`, `collections_failed`, `watcher` (no extra keys, no missing keys).
  - Unit: `test_status_existing_fields_unchanged` — authenticated `/status`; assert `running`, `pid`, `version`, `collections` are all present in the response (regression guard: the enrichment must be purely additive).
  - Unit: `test_status_readiness_jobs_counts_correct` — create two jobs (one `PENDING`, one `RUNNING`); assert `jobs.pending == 1`, `jobs.running == 1`.
  - Unit: `test_status_readiness_watcher_running_false_when_slot_is_none` — `app.state.watcher_manager is None`; assert `watcher == {"running": False, "watching": []}`.
  - Unit: `test_status_readiness_watcher_running_true_with_stub_manager` — set `app.state.watcher_manager` to a stub with `watching_names() -> ["colA", "colB"]`; assert `watcher == {"running": True, "watching": ["colA", "colB"]}` (sorted).
  - Unit: `test_status_failed_collection_reflected_in_collections_failed` — state with one `FAILED` collection; assert `collections_failed == 1`.
  - Unit: `test_ready_not_affected_by_failed_collection` — confirm `/ready` handler code only calls `ping()` and does NOT read indexing state (inspect the handler or assert `GET /ready` succeeds with `ping=True` regardless of collection state). Assert `GET /ready` returns 200 when `ping` returns `True` even when a collection is in FAILED state in the state store. This test verifies the design boundary, not a coincidence of mocking.
  - Unit: `test_status_readiness_embedder_warm_false_before_encode` — assert `embedder_warm` is `False` on a cold `ModelEmbedder` (no encode call); accessed via `pipeline.embedder_is_warm`.
  - Unit: `test_status_still_requires_auth` — `GET /status` without `Authorization`; assert `401`.
  - Unit: `test_status_readiness_storage_connected_reflects_ping` — mock `ping` to return `False`; assert `storage_connected` is `False`.
  - Unit: `test_status_response_readiness_required` — assert that constructing `StatusResponse(running=True, pid=1, version="0.1", collections=[])` without `readiness` raises `pydantic.ValidationError`.
  - Contract/snapshot: serialise the `readiness` sub-object shape to a dict and compare to expected (pins all field names and types).
  - Checkpoint: `uv run pytest tests/test_routes_status.py -x -v`

---

### Phase 7 — Documentation and final verification

#### Task 7.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to update every documentation file affected by B2. Files to update:
    1. `Documentation/Architecture/150_security_and_privacy_architecture.md` — amend "Bearer auth on every endpoint except `/health`" (principle 2 at line ~16 and line ~59) to read "`/health` and `/ready`"; add the info-leak/threat-model rationale paragraph (the unauth `/ready` discloses only `ready` + `checks.storage`; this is acceptable per 150's in-scope "casual misuse by a second local client without the key" and out-of-scope "hostile same-OS-user can read LanceDB files directly" threats).
    2. `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` — add `/ready` row to the HTTP-endpoints table; update principle 4 to "`/health` and `/ready` unauthenticated"; add a "gating-vs-informational" paragraph (storage connectivity is the sole gate; model warmth, watcher state, index-build state, and queue depth are informational); add a note that `readiness.watcher.running = false` while the legacy `watching` flag = `config.watch` is the expected state until the live watcher lands; update the observability mermaid diagram to show `/ready`; add a `GET /ready` triage step **before** the auth-gated `/status` step in the "Search returns nothing" runbook.
    3. `Documentation/Architecture/140_error_handling_strategy.md` — add a status-code-table row: "503 from `GET /ready` returns `ReadinessResponse` body (`{ready, checks}`), **not** a `{detail}` body — this is an expected 'not-ready-yet' state, not a pipeline error. Contrast with the 503 from `routes_search.py` which uses `JSONResponse({"detail": ...})`.".
    4. `BREAKING.md` — add entry: "B2 (additive): new `GET /ready` endpoint (unauthenticated, no Bearer token required); `GET /status` response gains a `readiness` object. Both changes are additive and non-breaking for tolerant JSON consumers."
    5. Clarify `/health` == liveness, `/ready` == readiness in all touched docs.
    6. Do **not** update unrelated docs.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - [ ] `GET /ready` returns `200 {ready: true, checks: {storage: "ok"}}` when `SearchStore` is connected.
  - [ ] `GET /ready` returns `503 {ready: false, checks: {storage: "fail"}}` when `SearchStore.ping()` returns `False`.
  - [ ] `GET /ready` requires no Bearer token — returns `!= 401` with no `Authorization` header.
  - [ ] `GET /ready` body contains no collection names, source paths, document/chunk counts, queue integers, or per-model warm booleans.
  - [ ] `GET /ready` 503 body contains `"ready"` and `"checks"` and does NOT contain `"detail"`.
  - [ ] `/ready` appears in OpenAPI without a `BearerAuth` security annotation.
  - [ ] Authenticated `GET /status` includes a `readiness` sub-object with all fields: `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs.{pending, running}`, `collections_indexing`, `collections_failed`, `watcher.{running, watching}`.
  - [ ] Reading `embedder.is_warm` or `reranker.is_warm` does NOT construct `TextEmbedding` / `TextCrossEncoder`.
  - [ ] A `FAILED` collection is reflected as `collections_failed > 0` on authenticated `/status` and does NOT change `GET /ready` (stays `200`/`ready: true` when storage is ok).
  - [ ] `watcher` report is `{running: false, watching: []}` when `app.state.watcher_manager is None`.
  - [ ] `watcher` report is `{running: true, watching: [...sorted...]}` when `app.state.watcher_manager` is a stub with `watching_names()`.
  - [ ] `GET /health` response is byte-for-byte identical to pre-B2 behavior (`{"status": "running", "version": ...}`, always `200`).
  - [ ] Default pytest run (`uv run pytest`) passes with coverage ≥ 85 % without `--no-cov`.
  - [ ] `uv run pytest -m integration` passes (ping true/false against real LanceDB; `/ready` 503 on disconnect).
  - [ ] Eval harness (`uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`) passes with thresholds unchanged.
  - [ ] Security doc 150 updated: auth invariant reads "`/health` and `/ready`" with threat-model rationale.
  - [ ] Ops doc 160 updated: `/ready` row, gating-vs-informational paragraph, mermaid, runbook triage step.
  - [ ] Error doc 140 updated: `/ready` 503 body-shape row in the status-code table.
  - [ ] `BREAKING.md` updated with additive B2 entry.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest` and confirm green with coverage ≥ 85 %.

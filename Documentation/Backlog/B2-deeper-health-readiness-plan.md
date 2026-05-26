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
- TTL cache staleness (~1 s): a cached `True` can lag a just-dropped connection by up to the TTL, and a cached `False` can lag a recovered connection by up to the TTL. The `False → True` delay (recovery detection lag) is the more operationally painful direction — a service that has recovered reports itself as not-ready for up to 1 extra probe interval before the cached `False` expires. With K8s default `periodSeconds=10`, this is a maximum 10s recovery delay. With aggressive 1s probes, the lag is 1 extra interval. Acceptable for B2; documented as a known trade-off.
- **Ping cache is not task-safe under concurrent calls**: `self._ping_cache` check-and-update straddles an `asyncio.wait_for` suspension point. Concurrent `ping()` calls within the TTL window may both call `list_tables()`. This is benign (idempotent result), but the cache's "at most one call per TTL" guarantee is best-effort, not strict. If strict deduplication is required, an `asyncio.Lock` on the instance must guard the full check→await→update sequence. For B2, best-effort is accepted.
- `IndexingStateStore.read()` cannot distinguish corrupt from empty (returns `None` for both) — B2 surfaces no `state_store_ok` field and the state store never gates `/ready`. When `state` is `None`, `collections_indexing` and `collections_failed` will both be `0` in `/status`. Before implementing Task 6.1, confirm that the existing `state_store.read()` call already logs a warning on corrupt-read (i.e., when the file exists but cannot be parsed). If it does not, add a `logger.warning("IndexingStateStore: corrupt state file, treating as empty")` to the `read()` method.
- `watcher` report is `{running: false, watching: []}` until a future item wires a live `WatcherManager` into the server. The legacy per-collection `watching` flag (`= config.watch`) being `true` while `readiness.watcher.running` is `false` is the expected state and is not a bug. The `watching_names()` method is the interface contract between this plan and the future live `WatcherManager`; it must return a `set[str]` of collection name strings (matching the actual `WatcherManager.watching_names()` signature at watcher.py line 260). `readiness.py` defines `WatcherManagerProtocol` with `def watching_names(self) -> set[str]: ...` to make this contract checkable by type checkers. Type `app.state.watcher_manager` as `WatcherManagerProtocol | None` in `collect_readiness`.
- The new `readiness` block in `/status` displayed next to the misleading `doc_count: 0` placeholders could confuse operators — the count-fill is recommended as a fast-follow.
- **`count_by_status()` iterates `self._jobs` synchronously without a lock**: `count_by_status()` is a synchronous method with no `await` points. In asyncio's single-threaded event loop, no other coroutine can interleave during its execution — the event loop only switches between coroutines at `await` points. All `JobStore` mutations (`create`, `update`, `transition`) are also synchronous and run on the same event loop thread, so there is no cross-coroutine mutation race during iteration. **The GIL is not the protection mechanism here** — the GIL does not make dict iteration atomic across bytecodes and would not protect against `RuntimeError: dictionary changed size during iteration` if another thread mutated `_jobs`. The actual protection is the asyncio single-thread scheduling guarantee. If a future change introduces a background thread that writes to `JobStore`, this assumption breaks and an `asyncio.Lock` would be required. For B2, the asyncio single-thread guarantee is sufficient and accepted.
- **OpenAPI doc is unauthenticated and discloses the full API surface**: `GET /openapi.json` is in `_EXEMPT_PATHS`. Any anonymous caller can enumerate all endpoint names, shapes, and security annotations. This is an existing accepted trade-off for the project (developer convenience); B2 does not change this posture. The `/ready` body itself discloses only `ready + checks.storage`.
- **`readiness: ReadinessDetail | None = None` is optional and backward-compatible**: existing `StatusResponse(...)` call sites do not need updating — the field defaults to `None` until Task 6.1 populates it. After Task 6.1, the handler always passes `readiness=ReadinessDetail(...)`, so in production the field is always populated. Tests for the `/status` endpoint that don't set up the full readiness context will see `readiness=None` in the response.
- **`GET /ready` uses `responses=` not `response_model=`**: The handler returns `JSONResponse` to control the 503 status code. Using `responses={200: ..., 503: ...}` on the decorator ensures both shapes appear in OpenAPI (the authoritative contract per `CLAUDE.md`). Using `response_model=ReadinessResponse` would only document the 200 shape and FastAPI would skip body validation anyway. The test suite pins the body shape for both status codes.

---

## Architecture

### New modules / classes / functions

**`archon_search/store.py`** — `SearchStore.ping(self) -> bool`
- Calls `await asyncio.wait_for(self._db.list_tables(), timeout=PING_TIMEOUT_SECONDS)` and accesses `.tables` on the result (confirms the call returned a valid object, not an exception-swallowed None).
- Returns `False` (not raises) when `self._db is None`, when `list_tables()` raises any `Exception` subclass (including timeout, storage errors, `AttributeError` from `.tables` access), or when the timeout fires. `CancelledError` (a `BaseException`) intentionally propagates.
- Caches **both** `True` and `False` results for `PING_TTL_SECONDS` when a real `list_tables()` call was attempted (instance-level `self._ping_cache: tuple[float, bool] | None = None`, initialised in `__init__`; reset on each `connect()` / `disconnect()`). Exception: when `self._db is None`, returns `False` immediately **without** updating the cache (no point caching a disconnected state — `connect()` will reset the cache anyway). A cached `False` from a prior failed `list_tables()` call within TTL returns immediately without re-calling `list_tables()`.
- Cache is best-effort under concurrent async calls (no lock); see Known Limitations.
- Constants in `archon_search/constants.py`: `PING_TIMEOUT_SECONDS: float = 1.0`, `PING_TTL_SECONDS: float = 1.0`.

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
    readiness: ReadinessDetail | None = None  # NEW — optional for backward compat
```

**`archon_search/server/routes_ready.py`** — new file
- `router = APIRouter()`
- `GET /ready` handler: calls `request.app.state.search_store.ping()`, **always returns `JSONResponse`** — `status_code=200` when storage is up, `status_code=503` when down. The handler never returns a plain `ReadinessResponse` or `Response` — it always wraps the body in `JSONResponse(body.model_dump(mode="json"), status_code=...)` to control the HTTP status code. Both shapes are documented in OpenAPI via `responses={200: {"model": ReadinessResponse}, 503: {"model": ReadinessResponse}}`. Do NOT use `response_model=ReadinessResponse` (FastAPI would only document the 200 shape and skip body validation for `JSONResponse` returns).

**`archon_search/server/middleware_auth.py`** — add `"/ready"` to `_EXEMPT_PATHS`.

**`archon_search/server/app.py`** — register `routes_ready.router`; add `app.state.watcher_manager = None` in `create_app`.

**`archon_search/server/readiness.py`** — new module containing two items: (1) `WatcherManagerProtocol` — a minimal `typing.Protocol` defining the `watching_names() -> set[str]` interface contract (matches the actual `WatcherManager.watching_names()` signature); `app.state.watcher_manager` is typed as `WatcherManagerProtocol | None`; (2) `async def collect_readiness(app_state: State, state: IndexingState | None) -> ReadinessDetail` — aggregates all readiness signals. Extracted from the `/status` handler to keep the handler thin and provide a single testable seam. Both `routes_status.py` and any future authenticated readiness expansion call this function.

**`archon_search/server/routes_status.py`** — enrich return value with `readiness=await collect_readiness(request.app.state, state)` (delegates to `readiness.collect_readiness`). The handler itself adds no readiness logic.

### Data flow (GET /ready)
```
probe → APIKeyMiddleware (exempt) → routes_ready.ready()
  → SearchStore.ping() [TTL-cached, timeout-guarded]
  → ReadinessResponse(ready=True/False, checks=ReadinessChecks(storage="ok"/"fail"))
  → HTTP 200 or 503
```

### Data flow (GET /status enrichment)
```
authenticated caller → routes_status.status()
  → existing per-collection logic (unchanged)
  → state_store.read()                    [state — loaded once, passed to collect_readiness]
  → collect_readiness(app_state, state)   [delegates all readiness signals]
      → app_state.search_store.ping()         [storage_connected]
      → app_state.pipeline.embedder_is_warm   [embedder_warm]  ← pipeline seam
      → app_state.pipeline.reranker_is_warm   [reranker_warm]
      → app_state.job_store.count_by_status() [jobs.pending, jobs.running]
      → state.collections (passed in)         [collections_indexing, collections_failed]
      → app_state.watcher_manager             [watcher]
  → StatusResponse(readiness=ReadinessDetail(...), ...)
```
Note: `state_store = request.app.state.state_store` and `state = state_store.read()` are already present in `routes_status.py` at lines 37–38. The handler passes `state` to `collect_readiness(app_state, state)` — no additional `state_store.read()` disk access occurs inside `collect_readiness`. This is why `state` is a parameter, not read internally.

### New config / constants
- `PING_TIMEOUT_SECONDS: float = 1.0` in `archon_search/constants.py`
- `PING_TTL_SECONDS: float = 1.0` in `archon_search/constants.py`

---

## Task breakdown

### Phase 1 — Storage ping
> **Releasable**: after Task 1.2, `SearchStore.ping()` is fully tested and callable by any consumer without side effects.

#### Task 1.1 — Constants for ping timeout and TTL
- [x] **File**: `archon_search/constants.py`
- **Depends on**: nothing
- **Description**:
  - Add two module-level float constants:
    - `PING_TIMEOUT_SECONDS: float = 1.0` — `asyncio.wait_for` timeout used by `SearchStore.ping()`.
    - `PING_TTL_SECONDS: float = 1.0` — in-process cache TTL for the ping result.
  - These are the only place these values live; `store.py` imports them.
  - No config-file promotion for B2 (fixed constants, not user-configurable).
- **Releasable**: after this task, constants are importable by `store.py`.
- **Tests (TDD)** — `tests/test_constants.py`:
  - Unit: `test_ping_timeout_seconds_is_float` — assert `PING_TIMEOUT_SECONDS` is a `float` and `> 0`.
  - Unit: `test_ping_ttl_seconds_is_float` — assert `PING_TTL_SECONDS` is a `float` and `> 0`.
  - Checkpoint: `uv run pytest tests/test_constants.py -x -v`

#### Task 1.2 — `SearchStore.ping()` with TTL cache
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add instance-level `self._ping_cache: tuple[float, bool] | None = None` (monotonic timestamp + bool) initialised in `SearchStore.__init__`. Use `time.monotonic()` for the timestamp.
  - `SearchStore.ping(self) -> bool` (async):
    - If `self._db is None`: return `False` immediately (no cache update).
    - Check `self._ping_cache`: if `self._ping_cache is not None` and `time.monotonic() - self._ping_cache[0] < PING_TTL_SECONDS`, return `self._ping_cache[1]`.
    - Call `asyncio.wait_for(self._db.list_tables(), timeout=PING_TIMEOUT_SECONDS)` then access `.tables` on the result, all inside `try: ... except Exception: ...` — any `Exception` subclass (including `asyncio.TimeoutError`, `OSError`, `RuntimeError`, `AttributeError`, `ValueError`, and storage-library-specific errors like `lance.LanceError` or `pyarrow.ArrowInvalid`) → update `self._ping_cache` with `False`, return `False`. Use `except Exception` (not a narrow list) because LanceDB may raise error types beyond the standard library. Note: `CancelledError` (a `BaseException`, not `Exception`) is intentionally NOT caught — if the calling task is cancelled, the exception propagates correctly and `self._ping_cache` is not updated. Implementation pattern: `try: result = await asyncio.wait_for(...); _ = result.tables; ... except Exception: ...`.
    - On success, access `.tables` attribute (confirms the call returned a valid object; note an empty `[]` is a valid live response when pagination is active), update `self._ping_cache` with `True`, return `True`.
    - The cache is instance-level (`self._ping_cache`). Each `SearchStore` instance has its own cache. This is the correct design — tests need no autouse fixture, and multiple instances in tests do not share state.
    - **Cache concurrency note**: the check-and-update straddles an `await` point. Concurrent async calls may both miss the cache and call `list_tables()` — this is benign (idempotent). The "at most one call per TTL" guarantee is best-effort, not strict. No lock is required for B2.
    - **Cache False caching**: both `True` and `False` results update `self._ping_cache`. A failed ping within TTL returns the cached `False` without re-calling `list_tables()`.
  - Reset cache (`self._ping_cache = None`) at the top of both `connect()` and `disconnect()` to prevent stale `True` after reconnect or shutdown.
  - Initialise `self._ping_cache: tuple[float, bool] | None = None` in `SearchStore.__init__`. No special test isolation fixture is required — each test that creates a new `SearchStore` instance gets its own fresh cache.
  - Import: `import time`, `import asyncio`, `from archon_search.constants import PING_TIMEOUT_SECONDS, PING_TTL_SECONDS`. No `global` statement needed.
- **Releasable**: after this task, `SearchStore.ping()` is callable and returns reliable bool.
- **Tests (TDD)** — `tests/test_store_ping.py` (new file; no autouse cache-reset fixture needed — each test creates a fresh `SearchStore` instance, which has its own `self._ping_cache`):
  - Unit: `test_ping_returns_true_when_connected` — mock `self._db.list_tables()` to return an object with `.tables = []`; assert `ping()` returns `True`.
  - Unit: `test_ping_returns_false_when_db_is_none` — create a `SearchStore` without calling `connect()`; assert `ping()` returns `False`.
  - Unit: `test_ping_returns_false_on_list_tables_exception` — mock `list_tables()` to raise `RuntimeError`; assert `ping()` returns `False` (does not raise).
  - Unit: `test_ping_returns_false_on_timeout` — patch `archon_search.store.PING_TIMEOUT_SECONDS` to `0.01` (10ms) and mock `list_tables()` to `asyncio.sleep(0.5)` (500ms, 50× the timeout). Assert `ping()` returns `False`. **Do NOT** use the real 1.0s timeout — the test would take ≥1 second. Use 500ms sleep (not 50ms) — a 5× headroom could be marginal on heavily loaded CI runners where timer resolution is ~10-15ms; 50× headroom makes the timeout deterministic. The patch approach: `with patch("archon_search.store.PING_TIMEOUT_SECONDS", 0.01):`. Note: `asyncio.wait_for` cancels the inner sleep coroutine and raises `TimeoutError`, which is caught by `except Exception`, and `ping()` returns `False`.
  - Unit: `test_ping_ttl_cache_prevents_second_call_on_true` — mock `list_tables()`, call `ping()` twice. Wrap the test body with `with patch("archon_search.store.PING_TTL_SECONDS", 60.0):` to guarantee both calls appear within TTL regardless of CI execution speed (same rationale as `test_ping_ttl_cache_prevents_second_call_on_false`). Assert `list_tables()` called exactly once (TTL cache hit on second call returns `True`).
  - Unit: `test_ping_ttl_cache_prevents_second_call_on_false` — mock `list_tables()` to raise, call `ping()` twice "within TTL". To guarantee both calls appear within TTL regardless of CI execution speed, wrap the test body with `with patch("archon_search.store.PING_TTL_SECONDS", 60.0):` — a 60-second TTL cannot expire between two consecutive `ping()` calls in process. Do NOT rely on the real 1.0s TTL being larger than call latency; if the TTL is ever lowered below the actual wall-clock gap between calls, the test would silently start testing miss behavior instead of hit behavior. Assert `list_tables()` called exactly once (TTL cache hit on second call returns cached `False`).
  - Unit: `test_ping_ttl_cache_expires` — patch `archon_search.store.time` (the module reference, not `time.monotonic` directly) with a `MagicMock` whose `.monotonic` attribute has `side_effect=[t0, t0 + PING_TTL_SECONDS + 0.1, t0 + PING_TTL_SECONDS + 0.1]` (3 values total). First `ping()` call: `self._ping_cache is None` so the TTL check is skipped — one `monotonic()` call (write). Second `ping()` call: checks cache (one `monotonic()` call, returns `t0 + TTL + 0.1` > TTL → cache expired) then writes (one `monotonic()` call). Total: 3 calls. Call `ping()` twice; assert `list_tables()` called twice (cache expired between calls). **Important**: use `side_effect` not `return_value` — a fixed `return_value` would make both calls see the same timestamp and the test would always pass regardless of TTL logic.
  - Unit: `test_ping_cache_reset_on_disconnect` — call `ping()` to prime cache with `True`; call `disconnect()` (which resets `_ping_cache` to `None`); call `ping()` again (with `_db = None` after disconnect); assert second call returns `False`. Note: the `False` return here is via the `if self._db is None: return False` short-circuit path, not a new call to `list_tables()` — assert `list_tables()` was only called once total.
  - Unit: `test_ping_cache_reset_on_connect` — prime `self._ping_cache` with a `True` entry, then call the real `connect()` method (with `lancedb.connect_async` patched using `AsyncMock` — patch target is `lancedb.connect_async` at module level since `connect()` does `import lancedb` lazily: `with patch("lancedb.connect_async", new_callable=AsyncMock) as mock_connect:`; do NOT mock `connect()` itself, or the `self._ping_cache = None` reset line never runs); assert `list_tables()` is called again on next `ping()` (cache was cleared by `connect()`).
  - Unit: `test_ping_cache_cleared_even_when_connect_fails` — prime `self._ping_cache` with `(time.monotonic(), True)`; patch `lancedb.connect_async` to raise `OSError("disk full")`; call `connect()` inside `pytest.raises(OSError)`; after catching the error, assert `store._ping_cache is None` (the cache reset at the top of `connect()` must run even when `connect_async` raises, so the next `ping()` call will re-probe rather than returning a stale cached `True`).
  - Integration (`@pytest.mark.integration`): `test_ping_true_against_live_store` — use a real `SearchStore` with `tmp_path`; `connect()`, assert `ping()` returns `True`; `disconnect()`, assert `ping()` returns `False`.
  - Unit: `test_ping_propagates_cancelled_error` — create a `SearchStore` with a mock `_db`; mock `list_tables()` to raise `asyncio.CancelledError`; wrap the `await ping()` call in `pytest.raises(asyncio.CancelledError)` (or `BaseException`); **after catching the exception, explicitly assert `assert store._ping_cache is None`** — this is the critical invariant: cancellation must not write a stale `False` into the cache, which would cause subsequent calls to incorrectly return cached `False` rather than retrying. This verifies the intentional design: `CancelledError` is a `BaseException`, not `Exception`, so it correctly escapes the `except Exception` clause without touching `self._ping_cache`.
  - Checkpoint: `uv run pytest tests/test_store_ping.py -x -v`

---

### Phase 2 — Model warm-status accessors
> **Releasable**: after Task 2.3, all warm-status accessors are testable and the "reading `is_warm` never loads the model" invariant is enforced.

#### Task 2.1 — `is_warm` on `ModelEmbedder` and `Embedder`
- [x] **File**: `archon_search/embedder.py`
- **Depends on**: nothing
- **Description**:
  - Add `is_warm` as a `bool` property to `EmbedderBackend` Protocol: `@property def is_warm(self) -> bool: ...`
  - Add `@property def is_warm(self) -> bool: return self._model is not None` to `ModelEmbedder`. No lock acquisition. No model load.
  - Add `@property def is_warm(self) -> bool: return self._backend.is_warm` to `Embedder`.
  - **Update ALL mock embedder backends** — adding `is_warm` to `EmbedderBackend` Protocol breaks `isinstance(obj, EmbedderBackend)` for any mock lacking the attribute. Update `is_warm: bool = False` in ALL of these files:
    - `tests/test_embedder.py` — `_MockBackend` (currently tested by `test_embedder_backend_protocol`)
    - `tests/test_pipeline_metadata.py` — `_MockEmbedderBackend`
    - `tests/integration/test_watcher_replace.py` — `_MockEmbedderBackend`
  - Additionally, update any future mocks by adding a CI guard in `tests/test_no_is_warm_mock.py` that asserts all classes used as `EmbedderBackend` in tests have an `is_warm` attribute (optional — document as future work if not implementing in B2).
- **Releasable**: after this task, callers can read `embedder.is_warm` without side effects.
- **Tests (TDD)** — `tests/test_embedder.py`:
  - Unit: `test_model_embedder_is_warm_false_before_encode` — create `ModelEmbedder("some-model")`; assert `is_warm` is `False`; assert `_model` is still `None` after the read.
  - Unit: `test_model_embedder_is_warm_true_after_model_set` — set `me._model = object()`; assert `is_warm` is `True`.
  - Unit: `test_embedder_is_warm_delegates_to_backend` — create a fake backend with `is_warm = False`; wrap in `Embedder`; assert `embedder.is_warm` is `False`. Set backend `is_warm = True`; assert `embedder.is_warm` is `True`.
  - Unit: `test_reading_is_warm_does_not_construct_TextEmbedding` — patch `fastembed.TextEmbedding` with a mock that raises on construction; read `ModelEmbedder("x").is_warm`; assert no exception (TextEmbedding was not called).
  - Unit: `test_reading_is_warm_does_not_acquire_lock` — use two `threading.Event` objects (`lock_acquired`, `test_done`) to synchronize: (1) side thread acquires `me._lock`, signals `lock_acquired`, then waits on `test_done`; (2) main thread waits on `lock_acquired` (confirms lock is held), then calls `me.is_warm` and asserts it returns `False` without blocking; (3) main thread signals `test_done`. This avoids the inherent race of checking `_lock.locked()` after-the-fact. Assert `is_warm` completes within 0.1 s (use `threading.Timer` or record wall-clock time).
  - Unit: `test_mock_backend_satisfies_protocol_after_is_warm_added` — assert `isinstance(_MockBackend(), EmbedderBackend)` still passes (regression guard for the mock update).
  - Checkpoint: `uv run pytest tests/test_embedder.py -x -v`

#### Task 2.2 — `is_warm` on `ModelReranker` and `Reranker`
- [x] **File**: `archon_search/reranker.py`
- **Depends on**: nothing
- **Description**:
  - Add `is_warm` as a `bool` property to `RerankerBackend` Protocol: `@property def is_warm(self) -> bool: ...`
  - Add `@property def is_warm(self) -> bool: return self._model is not None` to `ModelReranker`. No lock. No model load. Keyed on `_model`, not `_model_name`.
  - Add `@property def is_warm(self) -> bool: return self._backend.is_warm` to `Reranker`.
  - **Update ALL mock reranker backends** — same `@runtime_checkable` Protocol rationale as Task 2.1. Update `is_warm: bool = False` in ALL of these files:
    - `tests/test_reranker.py` — `_MockRerankerBackend` (tested by `test_reranker_backend_protocol`)
    - `tests/test_pipeline_metadata.py` — `_MockRerankerBackend`
    - `tests/integration/test_watcher_replace.py` — `_MockRerankerBackend`
- **Releasable**: after this task, `reranker.is_warm` is readable without side effects.
- **Tests (TDD)** — `tests/test_reranker.py`:
  - Unit: `test_model_reranker_is_warm_false_before_predict` — create `ModelReranker("some-model")`; assert `is_warm` is `False`; assert `_model` is still `None`.
  - Unit: `test_model_reranker_is_warm_true_after_model_set` — set `mr._model = object()`; assert `is_warm` is `True`.
  - Unit: `test_reranker_is_warm_delegates_to_backend` — fake backend; assert delegation.
  - Unit: `test_reading_reranker_is_warm_does_not_construct_TextCrossEncoder` — patch `fastembed.rerank.cross_encoder.TextCrossEncoder` to raise; read `ModelReranker("x").is_warm`; assert no exception.
  - Unit: `test_reading_reranker_is_warm_does_not_acquire_lock` — same `threading.Event` synchronization pattern as `test_reading_is_warm_does_not_acquire_lock` in Task 2.1: signal when lock is held, assert `is_warm` returns within 0.1 s, signal done.
  - Unit: `test_mock_reranker_backend_satisfies_protocol_after_is_warm_added` — assert `isinstance(_MockRerankerBackend(), RerankerBackend)` still passes.
  - Checkpoint: `uv run pytest tests/test_reranker.py -x -v`

#### Task 2.3 — `SearchPipeline.reranker_is_warm` and `SearchPipeline.embedder_is_warm` properties
- [x] **File**: `archon_search/pipeline.py`
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
- [x] **File**: `archon_search/jobs/store.py`
- **Depends on**: nothing
- **Description**:
  - Add `def count_by_status(self) -> dict[JobStatus, int]` to `JobStore`. Returns a dict mapping every `JobStatus` member to its count (zero-filled for members with no jobs). Implementation: `from collections import Counter; counts = Counter(j.status for j in self._jobs.values()); return {s: counts.get(s, 0) for s in JobStatus}`.
  - This is a thin, pure convenience — all the state is already in `self._jobs`. Route handlers call `job_store.count_by_status()` and read `.get(JobStatus.PENDING, 0)` and `.get(JobStatus.RUNNING, 0)`.
  - Note: this helper is a convenience; route handlers may also inline the filter, but `count_by_status()` makes the aggregation independently testable.
  - **`CANCELLING` status note**: `count_by_status()` returns counts for all `JobStatus` members including `CANCELLING` (a transient internal state). The `/status` response only exposes `pending` and `running` in `JobCounts` — `CANCELLING` is intentionally not surfaced, because from an operator's perspective a cancelling job is in the process of stopping and does not represent available capacity. A `CANCELLING` job is neither pending new work nor actively making progress on new work. However, this means `running=0` can appear while a job is mid-cancellation. Document this in `JobCounts` model docstring (Pydantic `model_config` or docstring): "`running` counts jobs in `RUNNING` status only; `CANCELLING` jobs are excluded."
- **Releasable**: after this task, `job_store.count_by_status()` is callable and testable.
- **Tests (TDD)** — `tests/test_job_store.py`:
  - Unit: `test_count_by_status_empty_store` — new `JobStore` (tmp path); assert all statuses return `0`.
  - Unit: `test_count_by_status_mixed` — create jobs with various statuses; assert correct counts for `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`, `CANCELLING`.
  - Unit: `test_count_by_status_includes_all_status_members` — assert every `JobStatus` member is a key in the returned dict (zero-filled).
  - Unit: `test_count_by_status_excludes_evicted_jobs` — create a job, manually set its `updated_at` to 8 days ago (beyond the 7-day eviction window), then **call `store.update(job_id, status=job.status)` (a no-op status update) to trigger in-memory eviction via the public API** (eviction runs inside `_evict_old()` which is called by `_write_atomic()` which is called by `update()`); assert `count_by_status()` returns `0` for all statuses. Do NOT call `_write_atomic()` directly — using the public `update()` method is safer and won't break if `_write_atomic` is renamed or its eviction behavior moves elsewhere. Do NOT just set `updated_at` and call `count_by_status()` directly — without triggering `_evict_old()`, the job is still in `self._jobs` and the test passes with a bug. The purpose of this test is to guard that `count_by_status()` reflects the post-eviction in-memory state, not to test eviction itself.
  - Checkpoint: `uv run pytest tests/test_job_store.py -x -v`

---

### Phase 4 — Pydantic schemas
> **Releasable**: after Task 4.1, all new response models are importable and snapshot-testable.

#### Task 4.1 — `CheckStatus`, `ReadinessResponse`, `ReadinessDetail`, `StatusResponse.readiness`
- [x] **File**: `archon_search/server/schemas.py`
- **Depends on**: nothing
- **Description**:
  - Add `CheckStatus(str, Enum)` with members `OK = "ok"` and `FAIL = "fail"`.
  - Add `ReadinessChecks(BaseModel)`: `storage: CheckStatus`.
  - Add `ReadinessResponse(BaseModel)`: `ready: bool`, `checks: ReadinessChecks`. This is the terse unauthenticated body.
  - Add `WatcherReport(BaseModel)`: `running: bool`, `watching: list[str] = []`.
  - Add `JobCounts(BaseModel)`: `pending: int`, `running: int`.
  - Add `ReadinessDetail(BaseModel)`: `storage_connected: bool`, `embedder_warm: bool`, `reranker_warm: bool`, `jobs: JobCounts`, `collections_indexing: int`, `collections_failed: int`, `watcher: WatcherReport`.
  - Extend `StatusResponse`: add `readiness: ReadinessDetail | None = None` as an optional field (default `None`). Making it optional means existing `StatusResponse(...)` call sites do not need updating — the field will be `None` until Task 6.1 populates it. This is genuinely additive for both Python model construction and JSON consumers.
  - Import `from enum import Enum` (already present if `CheckStatus` uses it; otherwise add).
  - No changes to any existing field on any existing model.
- **Releasable**: after this task, all new schemas are importable and Pydantic-validated.
- **Tests (TDD)** — `tests/contract/test_readiness_schemas.py` (new file):
  - Unit: `test_readiness_response_ok_shape` — instantiate `ReadinessResponse(ready=True, checks=ReadinessChecks(storage=CheckStatus.OK))`; assert `.model_dump() == {"ready": True, "checks": {"storage": "ok"}}`.
  - Unit: `test_readiness_response_fail_shape` — same with `ready=False`, `storage=CheckStatus.FAIL`; assert correct dict.
  - Unit: `test_readiness_detail_shape` — instantiate `ReadinessDetail` with all fields; assert `.model_dump()` contains all expected keys.
  - Unit: `test_status_response_has_readiness_field` — instantiate `StatusResponse` with a `readiness=ReadinessDetail(...)` field; assert the field is present in `.model_dump()`.
  - Unit: `test_status_response_readiness_defaults_to_none` — instantiate `StatusResponse(running=True, pid=1, version="0.1", collections=[])` without `readiness`; assert `.readiness is None` (backward compat — no `ValidationError`).
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
- **Tests (TDD)** — `tests/test_middleware_auth.py`:
  - Unit: `test_ready_in_exempt_paths` — import `_EXEMPT_PATHS` from `middleware_auth`; assert `"/ready" in _EXEMPT_PATHS`. This test is independently runnable immediately after Task 5.1 (no route required — tests only the set membership). Checkpoint: `uv run pytest tests/test_middleware_auth.py::test_ready_in_exempt_paths -x -v`
  - Unit: `test_ready_reachable_without_bearer_token` — `GET /ready` with no `Authorization` header; assert `status_code != 401`. (Lives in the Task 5.2 test file and runs after the route exists.)
  - Unit: `test_exempt_paths_all_have_matching_routes` — call `create_app(config, job_store)` to obtain a configured FastAPI instance (do NOT import a module-level `app` — `app.py` exports `create_app()`, not a module-level variable); assert every path in `_EXEMPT_PATHS` matches at least one route in `[r.path for r in app.routes]`. **Fixture setup**: use the same `create_app(...)` call pattern as other route tests — typically: `cfg = SearchConfig(); cfg.db_path = str(tmp_path / "search"); job_store = JobStore(path=tmp_path / "jobs.json"); app = create_app(cfg, job_store)`. Check whether `conftest.py` already provides an `app` or `test_client` fixture before writing a new one. **Note**: this test must run AFTER Task 5.2 — if run before `routes_ready.py` exists, `/ready` won't be in `[r.path for r in app.routes]` and the test will fail. Place in `tests/test_middleware_auth.py`. Checkpoint: `uv run pytest tests/test_middleware_auth.py -x -v`

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

    @router.get(
        "/ready",
        responses={
            200: {"model": ReadinessResponse},
            503: {"model": ReadinessResponse},
        },
    )
    async def ready(request: Request) -> JSONResponse:
        store = request.app.state.search_store
        storage_ok = await store.ping()
        body = ReadinessResponse(
            ready=storage_ok,
            checks=ReadinessChecks(storage=CheckStatus.OK if storage_ok else CheckStatus.FAIL),
        )
        status_code = 200 if storage_ok else 503
        return JSONResponse(body.model_dump(mode="json"), status_code=status_code)
    ```
  - **`app.py` changes**:
    - Import `from archon_search.server.routes_ready import router as ready_router`.
    - Add `app.state.watcher_manager = None` after the other `app.state.*` assignments in `create_app`.
    - Add `app.include_router(ready_router)` alongside the other `include_router` calls.
  - The `watcher_manager` slot is `None` by default (no live watcher in B2); `/status` and future items read from it.
  - Using `responses={200: {"model": ReadinessResponse}, 503: {"model": ReadinessResponse}}` ensures OpenAPI documents both response shapes (200 and 503). Do NOT use `response_model=ReadinessResponse` — FastAPI bypasses response_model validation when the handler returns `JSONResponse`, and the 503 body shape would be undocumented in OpenAPI. The `mode="json"` in `.model_dump(mode="json")` ensures Pydantic v2 serialises enum members as their `.value` strings (e.g., `CheckStatus.OK` → `"ok"`) regardless of Pydantic version.
- **Releasable**: after this task, `GET /ready` is live and unauthenticated.
- **Tests (TDD)** — `tests/test_routes_ready.py` (new file):
  - Unit: `test_ready_returns_200_when_storage_ok` — mock `SearchStore.ping` to return `True`; `GET /ready`; assert `status_code == 200` and `body["ready"] is True` and `body["checks"]["storage"] == "ok"`.
  - Unit: `test_ready_returns_503_when_storage_fails` — mock `ping` to return `False`; assert `status_code == 503` and `body["ready"] is False` and `body["checks"]["storage"] == "fail"`.
  - Unit: `test_ready_reachable_without_bearer_token` — `GET /ready` with no auth header; assert `status_code != 401`.
  - Unit (negative — pins no-info-leak): `test_ready_body_schema_is_bounded` — parse the response body JSON; assert `set(body.keys()) == {"ready", "checks"}` and `set(body["checks"].keys()) == {"storage"}`. This is a structural allowlist assertion — it fails if any additional field (e.g., collection names, counts, warm booleans) is added to the `/ready` response, without false-positive risk from substring matching on nested field names.
  - Unit: `test_ready_503_body_is_readiness_response_not_error_detail` — mock `ping` to return `False`; assert `"ready" in body` and `"checks" in body` and `"detail" not in body`. Pins the "503-is-a-state-not-an-error" decision (contrast A3's `{"detail": ...}` envelope).
  - Unit: `test_ready_appears_in_openapi_without_bearer_auth` — retrieve `/openapi.json`; find the `/ready` path; assert it has no `security` annotation (or `security` is absent / empty). Also assert the OpenAPI response schema for `/ready` 200 and 503 both contain `ready` and `checks` (pins that `responses={200: ..., 503: ...}` correctly documents both shapes).
  - Unit: `test_watcher_manager_slot_is_none_by_default` — after `create_app`, assert `app.state.watcher_manager is None`.
  - Unit: `test_ready_does_not_call_collect_readiness` — pins the design boundary that `/ready` is intentionally minimal and does NOT import or call `collect_readiness` (which would add warm-status, job-count, and watcher dependencies to the unauthenticated probe). Implementation: `import archon_search.server.routes_ready as mod; assert "collect_readiness" not in dir(mod)`. If a source-level check is preferred: `assert "collect_readiness" not in Path(mod.__file__).read_text()`. **Do NOT use the monkeypatch-and-catch-AttributeError approach** — that pattern gives a false-green if `collect_readiness` is imported under a different alias (e.g., `from ... import collect_readiness as _collect`), and it also gives a false-green if the `AttributeError` is swallowed by the test framework. A direct `dir(mod)` or source-text assertion is unambiguous and robust.
  - Integration (`@pytest.mark.integration`): `test_ready_returns_503_after_store_disconnect` — start a real store, call `connect()`, call `disconnect()`, then `GET /ready`; assert `status_code == 503`. (Note: this tests the post-disconnect state, not a true concurrent mid-flight race, which is out of scope for B2.)
  - Checkpoint: `uv run pytest tests/test_routes_ready.py -x -v`

---

### Phase 6 — Enrich authenticated `/status`
> **Releasable**: after Task 6.1, authenticated `GET /status` includes the full `readiness` sub-object.

#### Task 6.1 — Add `readiness` sub-object to `/status` handler
- [ ] **Files**: `archon_search/server/readiness.py` (new), `archon_search/server/routes_status.py`
- **Depends on**: Task 1.2, Task 2.1, Task 2.2, Task 2.3, Task 3.1, Task 4.1, Task 5.2
- **Description**:
  - **New file `archon_search/server/readiness.py`**: contains two items:
    1. `WatcherManagerProtocol`: `class WatcherManagerProtocol(Protocol): def watching_names(self) -> set[str]: ...` — defines the interface contract for `app.state.watcher_manager`. The actual `WatcherManager.watching_names()` (watcher.py line 260) returns `set[str]`, so the protocol should match precisely. Using `set[str]` rather than `Iterable[str]` prevents protocol-satisfying implementations that return a generator (which would break the `sorted()` call in `collect_readiness` if the generator is exhausted). Import `Protocol` from `typing`; no `Iterable` import needed.
    2. `async def collect_readiness(app_state: State, state: IndexingState | None) -> ReadinessDetail` — aggregates all readiness signals.
    Imports: `from archon_search.server.schemas import ReadinessDetail, WatcherReport, JobCounts`; `from archon_search.types import JobStatus` (canonical source — `archon_search.jobs.model` and `archon_search.jobs.store` re-export it, but import from the canonical source to avoid double re-export chains); `from archon_search.progress import IndexingStatus, IndexingState` (`IndexingStatus` is defined in `archon_search/progress.py` line 25, NOT in `archon_search.sync` — `sync.py` only imports it inside function bodies and does not re-export it at module level); `from starlette.datastructures import State`; `from typing import Protocol`. Note: no `Iterable` import needed — `WatcherManagerProtocol.watching_names()` returns `set[str]` (not `Iterable[str]`), so `collections.abc.Iterable` is unused. The `state` parameter receives the already-loaded `IndexingState` from `routes_status.status()` to avoid a second `state_store.read()` disk access. The function body is:
  - **In `routes_status.py`**: import `from archon_search.server.readiness import collect_readiness`. Replace the inline readiness block with `readiness = await collect_readiness(request.app.state, state)` (passing the already-loaded `state` from line 38 to avoid a second disk read). Pass `readiness=readiness` to `StatusResponse(...)`.
  - The `collect_readiness` function body:
    ```python
    async def collect_readiness(app_state: State, state: IndexingState | None) -> ReadinessDetail:
        """Aggregate all readiness signals. Called by routes_status.status().
        
        Parameters:
          app_state: request.app.state (Starlette State object)
          state: already-loaded IndexingState from state_store.read() in the handler
                 (passed in to avoid a second disk read; may be None if state file absent/corrupt)
        """
        # Storage ping (async, TTL-cached)
        storage_ok = await app_state.search_store.ping()

        # Model warm-status (side-effect-free) — via pipeline seam, not app_state.embedder directly
        pipeline = app_state.pipeline
        embedder_warm: bool = pipeline.embedder_is_warm if pipeline is not None else False
        reranker_warm: bool = pipeline.reranker_is_warm if pipeline is not None else False

        # Job counts
        job_counts = app_state.job_store.count_by_status()
        jobs = JobCounts(
            pending=job_counts.get(JobStatus.PENDING, 0),
            running=job_counts.get(JobStatus.RUNNING, 0),
        )

        # Index-state counts (state passed in from caller — no extra disk read)
        collections_indexing = 0
        collections_failed = 0
        if state is not None:
            for cp in state.collections.values():
                if cp.status == IndexingStatus.IN_PROGRESS:
                    collections_indexing += 1
                elif cp.status == IndexingStatus.FAILED:
                    collections_failed += 1

        # Watcher report
        wm: WatcherManagerProtocol | None = app_state.watcher_manager
        watcher = (
            WatcherReport(running=False)
            if wm is None
            else WatcherReport(running=True, watching=sorted(wm.watching_names()))
        )

        return ReadinessDetail(
            storage_connected=storage_ok,
            embedder_warm=embedder_warm,
            reranker_warm=reranker_warm,
            jobs=jobs,
            collections_indexing=collections_indexing,
            collections_failed=collections_failed,
            watcher=watcher,
        )
    ```
  - No changes to per-collection entries or any existing field.
- **Backward-compat note**: `readiness: ReadinessDetail | None = None` is optional and backward-compatible: existing `StatusResponse(...)` call sites do not need updating — the field defaults to `None` until Task 6.1 populates it. After Task 6.1, the handler always passes `readiness=ReadinessDetail(...)`, so in production the field is always populated. Tests for the `/status` endpoint that don't set up the full readiness context will see `readiness=None` in the response.
- **Releasable**: after this task, authenticated `/status` includes the full `readiness` sub-object.
- **Tests (TDD)** — `tests/test_routes_status.py` (extend existing file):
  - Unit: `test_status_readiness_block_present` — authenticated `/status`; assert `"readiness" in response.json()`.
  - Unit: `test_status_readiness_has_all_fields` — assert `readiness` dict contains exactly `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs`, `collections_indexing`, `collections_failed`, `watcher` (no extra keys, no missing keys).
  - Unit: `test_status_existing_fields_unchanged` — authenticated `/status`; assert `running`, `pid`, `version`, `collections` are all present in the response (regression guard: the enrichment must be purely additive).
  - Unit: `test_status_readiness_jobs_counts_correct` — create two jobs (one `PENDING`, one `RUNNING`) using `job_store.create(...)` then `job_store.update(job_id, status=JobStatus.RUNNING)` for the second; assert `jobs.pending == 1`, `jobs.running == 1`. **Fixture note**: this test requires a `JobStore` instance wired into the `app.state.job_store`. Use the same `create_app(cfg, job_store)` fixture pattern as other route tests (with a `tmp_path`-backed `JobStore`) rather than injecting directly into `app.state` after creation.
  - Unit: `test_status_returns_500_when_job_store_raises` — mock `app.state.job_store.count_by_status` (or the `JobStore` instance's method) to raise `RuntimeError`; call authenticated `GET /status`; assert `status_code == 500`. This verifies the route-level consequence of the `collect_readiness` propagation documented in `test_collect_readiness_job_store_count_raises`.
  - Unit: `test_status_readiness_watcher_running_false_when_slot_is_none` — `app.state.watcher_manager is None`; assert `watcher == {"running": False, "watching": []}`.
  - Unit: `test_status_readiness_watcher_running_true_with_stub_manager` — set `app.state.watcher_manager` to a stub with `watching_names() -> ["colA", "colB"]`; assert `watcher == {"running": True, "watching": ["colA", "colB"]}` (sorted).
  - Unit: `test_status_failed_collection_reflected_in_collections_failed` — state with one `FAILED` collection; assert `collections_failed == 1`.
  - Unit: `test_ready_not_affected_by_failed_collection` — confirm `/ready` handler code only calls `ping()` and does NOT read indexing state (inspect the handler or assert `GET /ready` succeeds with `ping=True` regardless of collection state). Assert `GET /ready` returns 200 when `ping` returns `True` even when a collection is in FAILED state in the state store. This test verifies the design boundary, not a coincidence of mocking.
  - Unit: `test_status_readiness_embedder_warm_false_before_encode` — assert `embedder_warm` is `False` on a cold `ModelEmbedder` (no encode call); accessed via `pipeline.embedder_is_warm`.
  - Unit: `test_status_still_requires_auth` — `GET /status` without `Authorization`; assert `401`.
  - Unit: `test_status_readiness_storage_connected_reflects_ping` — mock `ping` to return `False`; assert `storage_connected` is `False`.
  - Unit: `test_status_response_readiness_always_populated_by_handler` — authenticated `/status` (full handler path); assert `response.json()["readiness"] is not None` (handler always populates readiness after Task 6.1).
  - Unit: `test_no_app_state_model_direct_access` — CI guard (runs here, after `routes_ready.py` and `readiness.py` exist): read every `.py` file in `archon_search/server/` **except `app.py`** (use glob `archon_search/server/*.py` and filter out `app.py`); assert no file contains the substring `app.state.embedder` or `app.state.reranker` (mirrors the `test_no_fstring_sql.py` pattern). **Important**: the glob must include `readiness.py` — using `routes_*.py` would silently exclude the new `readiness.py` module and miss any violation there. Place in `tests/test_no_app_state_model_access.py`. Running this test at Task 2.3 (before the route files exist) provides false confidence; it is only meaningful after Phase 5-6 files are written.
  - Contract/snapshot: serialise the `readiness` sub-object shape to a dict and compare to expected (pins all field names and types).

**Additional tests (TDD)** — `tests/test_readiness.py` (new file — unit tests for `collect_readiness` in isolation):
  - Unit: `test_collect_readiness_happy_path` — call `collect_readiness(mock_app_state, mock_state)` with all healthy mocks (`ping()` returns `True`, `pipeline.embedder_is_warm=True`, `pipeline.reranker_is_warm=True`, `count_by_status()` returns all zeros, `state` with 0 indexing/failed, `watcher_manager=None`); assert returned `ReadinessDetail` has all expected field values.
  - Unit: `test_collect_readiness_storage_down` — `ping()` returns `False`; assert `storage_connected=False`.
  - Unit: `test_collect_readiness_pipeline_none` — set `app_state.pipeline = None`; assert `embedder_warm=False` and `reranker_warm=False` (does NOT raise `AttributeError`).
  - Unit: `test_collect_readiness_state_none` — pass `state=None`; assert `collections_indexing=0`, `collections_failed=0` (does NOT raise).
  - Unit: `test_collect_readiness_state_has_failed_and_indexing` — pass a mock `state` with one `IN_PROGRESS` and one `FAILED` collection; assert `collections_indexing=1`, `collections_failed=1`.
  - Unit: `test_collect_readiness_watcher_none` — `app_state.watcher_manager=None`; assert `watcher == WatcherReport(running=False, watching=[])`.
  - Unit: `test_collect_readiness_watcher_stub` — `app_state.watcher_manager` is a stub with `watching_names() -> ["colA", "colB"]`; assert `watcher == WatcherReport(running=True, watching=["colA", "colB"])`.
  - Unit: `test_collect_readiness_jobs_pending_running` — `count_by_status()` returns `{PENDING: 2, RUNNING: 1}`; assert `jobs.pending=2`, `jobs.running=1`.
  - Unit: `test_collect_readiness_does_not_read_state_store` (formerly named `test_collect_readiness_state_store_read_raises`) — guards the "no double disk read" design decision: `state` is passed as a parameter so `collect_readiness` must never call `app_state.state_store.read()`. Set up `mock_app_state.state_store.read` as a `MagicMock` (not raising — the test should catch a call, not swallow a raised exception). Pass a valid mock `state`. Call `collect_readiness(mock_app_state, state)`. **Assert `mock_app_state.state_store.read.assert_not_called()`** — this is the load-bearing assertion. Also assert no exception was raised. Asserting only "no exception" is insufficient because `collect_readiness` could swallow an exception from `state_store.read()` via a broad `except`, giving a false-green test.
  - Unit: `test_collect_readiness_job_store_count_raises` — mock `app_state.job_store.count_by_status` to raise `RuntimeError("disk error")`; assert the exception propagates through `collect_readiness` (i.e., `collect_readiness` does NOT swallow it). This documents the intentional behavior: `count_by_status()` has no "never raises" contract (unlike `ping()`), so a failure propagates as a 500 on `/status`. Alternatively, if the plan is changed to catch this and default to `JobCounts(pending=0, running=0)`, the test must be updated to assert the fallback behavior instead — but neither behavior is acceptable without a test.
  - Unit: `test_collect_readiness_watcher_raises` — stub `app_state.watcher_manager.watching_names` to raise `RuntimeError("watchdog thread died")`; assert the exception propagates through `collect_readiness`. This documents the intentional behavior: a broken `WatcherManager` propagates as a 500 on `/status` (same propagation policy as `count_by_status()` raises). If the plan is changed to catch and degrade gracefully (e.g., log warning + return `WatcherReport(running=True, watching=[])`), update this test accordingly.
  - Unit: `test_collect_readiness_ping_raises` — mock `app_state.search_store.ping` to raise `RuntimeError("broken contract")`; assert the exception propagates through `collect_readiness`. This documents that `collect_readiness` does NOT double-guard against `ping()` failures — `ping()` is itself a "never raises" contract; if it breaks that contract, the failure should surface rather than be silently swallowed.
  - Unit: `test_collect_readiness_cancelling_job_not_counted_as_running` — configure `count_by_status()` to return `{JobStatus.CANCELLING: 1, JobStatus.RUNNING: 0, ...}` (all other statuses zero); assert the returned `ReadinessDetail` has `jobs.running == 0`. This pins the design decision that `CANCELLING` is not surfaced as `running` in the operator-visible counts.
  - Checkpoint: `uv run pytest tests/test_readiness.py tests/test_routes_status.py -x -v`

---

### Phase 7 — Documentation and final verification

#### Task 7.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to update every documentation file affected by B2. Files to update:
    1. `Documentation/Architecture/150_security_and_privacy_architecture.md` — amend "Bearer auth on every endpoint except `/health`" (principle 2 at line ~16 and line ~59) to read "`/health` and `/ready`"; add the info-leak/threat-model rationale paragraph (the unauth `/ready` discloses only `ready` + `checks.storage`; this is acceptable per 150's in-scope "casual misuse by a second local client without the key" and out-of-scope "hostile same-OS-user can read LanceDB files directly" threats).
    2. `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` — add `/ready` row to the HTTP-endpoints table; update principle 4 to "`/health` and `/ready` unauthenticated"; add a "gating-vs-informational" paragraph (storage connectivity is the sole gate; model warmth, watcher state, index-build state, and queue depth are informational); add a note that `readiness.watcher.running = false` while the legacy `watching` flag = `config.watch` is the expected state until the live watcher lands; update the observability mermaid diagram to show `/ready`; add a `GET /ready` triage step **before** the auth-gated `/status` step in the "Search returns nothing" runbook. **Also document the intentional shape asymmetry between the two unauthenticated probes**: `/health` uses `{status: "running", version: ...}` (liveness — process-up signal, includes version for human inspection); `/ready` uses `{ready: bool, checks: {storage: ...}}` (readiness — dependency-gate signal, no version to minimise information disclosure). The shapes differ because they serve different consumers: `/health` is primarily for human operators and simple watchdog scripts; `/ready` is primarily for orchestrators (K8s kubelet, load balancers) that only need a binary up/down signal. Justify this divergence explicitly rather than leaving operators to infer it.
    3. `Documentation/Architecture/140_error_handling_strategy.md` — add a status-code-table row: "503 from `GET /ready` returns `ReadinessResponse` body (`{ready, checks}`), **not** a `{detail}` body — this is an expected 'not-ready-yet' state, not a pipeline error. Contrast with the 503 from `routes_search.py` which uses `JSONResponse({"detail": ...})`.".
    4. `BREAKING.md` — add entry: "B2 (additive): new `GET /ready` endpoint (unauthenticated, no Bearer token required); `GET /status` response gains a `readiness` field (`null` if not populated, `ReadinessDetail` object after full startup). Both changes are additive and backward-compatible for JSON consumers and Python model construction."
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

# B1 — Observability and Stage-Level Latency

**Purpose**: Add per-request correlation IDs and per-stage wall-clock timings to the retrieval and ingest pipelines, surfaced via structured log records, the `/explain` response, and a new `X-Request-ID` header. Resolves `ARCH-3`.
**Audience**: archon-search contributors implementing B1; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

Today, `archon-search` has no per-request tracing and no per-stage latency breakdown. Loggers across `store.py` (`"archon"`), `routes_route.py` (`"archon.search"`), and `app.py` (`"archon-search"`) emit unstructured lines with no shared key — a failing request cannot be correlated across the auth → pipeline → store → telemetry boundary. This is `ARCH-3` in the debt register (`Architecture/530_technical_debt_refactoring_roadmap.md:79`).

End-to-end latency is recorded in telemetry (`routes_route.py:71`) but there is no breakdown: when `/search` is slow there is no way to tell whether the embedder, vector leg, FTS, or cross-encoder is responsible without attaching a profiler.

The `/explain` endpoint (A4) deferred `stage_timings_ms` to "A4.3" — B1 delivers that field.

**Dependency notes:**
- The correlation-ID middleware, `StageRecorder`, and structured-log emission are **independent of A3 and A4** and can land first.
- Threading `correlation_id` into the `/search` telemetry line is gated on **A3** having created the telemetry enqueue on that route (A3 is already merged per `Documentation/Completed/`).
- The `stage_timings_ms` response field on `/explain` requires **A4** (already merged per `Documentation/Completed/`).

---

## Goal

After B1 ships: every request carries a `X-Request-ID` correlation ID minted at the edge and propagated into structured log records and telemetry. The five retrieval stages (`embed`, `vector`, `fts`, `fuse`, `rerank`) and three ingest stages (`parse`, `embed`, `persist`) are timed with `time.perf_counter()`. Per-stage timings appear on the `/explain` response as `stage_timings_ms`, and as a single structured `event_type="stage_timings"` log record per instrumented request. Instrumentation is a no-op when unbound — the eval harness, unit tests, and library callers pay one `ContextVar.get()` per stage and nothing else. The structural no-raw-query invariant on `TelemetryEntry` is preserved.

---

## Scope

### In Scope
- `archon_search/observability.py` — `StageRecorder`, `record_stage()`, `bind_stage_recorder()`, `correlation_id` ContextVar, `new_correlation_id()`, `sanitize_request_id()`
- `archon_search/server/middleware_context.py` — pure-ASGI `RequestContextMiddleware` (NOT `BaseHTTPMiddleware`)
- Registration of `RequestContextMiddleware` on the FastAPI app (`app.py`) and on the MCP Starlette app (`mcp.py`)
- `[observability]` config section in `config.py` and `SearchConfig`: `stage_timings_enabled: bool = True`, `request_id_header: str = "X-Request-ID"`
- `correlation_id: str | None = None` field on `TelemetryEntry` + `DOCUMENTED_SCHEMA_FIELDS` + all four factories
- `stage_timings_ms: dict[str, float] | None` field on `ExplainResponse` + population in the REST `/explain` route and MCP `explain` tool
- `record_stage(...)` wrappers (additive, no signature changes) in: `Embedder.embed`/`embed_one`, `MultiCollectionRouter._score_collections`, `SearchStore.hybrid_search`, `_hybrid_search_with_trace`, `Reranker.rerank`, `SearchPipeline.search_with_context` neighbor-fetch loop, `SearchPipeline.ingest_file` (parse/embed/persist stages only)
- `bind_stage_recorder()` + post-call structured-log emission in REST handlers `/search`, `/route`, `/explain`, and MCP tools `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`
- CLI ingest paths (`cli/ingest.py`, `cli/collection.py`) — bind recorder + mint own correlation ID
- OpenAPI: `stage_timings_ms` documented on the `/explain` response model
- Docs: `Architecture/160_operational_readiness_monitoring_and_reliability.md`, `Architecture/210_performance_and_scalability.md`, `Architecture/530_technical_debt_refactoring_roadmap.md` (mark `ARCH-3` resolved)

### Out of Scope
- JSON log formatter, handler wiring, log rotation — owned by **B7**
- `stage_timings_ms` on `/search` or `/route` responses (log-only for those)
- OpenTelemetry / OTLP spans / distributed tracing exporters (B1.1)
- `/metrics` Prometheus endpoint or histograms (B1.2)
- Eval-harness per-stage latency gating (stays report-only)
- Separate `acl` stage timing
- Timing the watcher/sync background paths
- Sampling / rate-limiting of timing logs
- Renaming the inconsistent logger names (`"archon"` / `"archon.search"` / `"archon-search"`)
- HTTP `/ingest` job runner wiring (`app.state.ingest_pipeline` is unset in production today)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See Task 5.1 — Final verification & documentation update.

---

## What does NOT change
- `TelemetryEntry` never gains a `query` parameter — structural no-raw-query invariant is preserved; `extra="forbid"` and `frozen=True` remain
- `SearchPipeline.search()`, `hybrid_search()`, `rerank()`, `embed()`, and all other pipeline method signatures — instrumentation is purely additive via ContextVar, no signature changes
- The telemetry JSONL format (only `correlation_id` is added; no stage timings written to JSONL)
- `/search` and `/route` response shapes — `stage_timings_ms` appears **only** on `/explain`
- `BaseHTTPMiddleware` is NOT used for `RequestContextMiddleware` — must be pure-ASGI
- The eval harness and its deterministic backends — they bind no recorder and record nothing

---

## Known limitations / accepted trade-offs
- Stage timings measure blocked-coroutine wall time, NOT pure stage CPU. The `embed` and `rerank` stages await `asyncio.to_thread(...)`, so measured time includes executor-queue and event-loop scheduling latency under concurrency. Documented as accepted for this report-only framing; true per-stage CPU spans deferred to B1.1 (OTel).
- Per-file ingest stages are recorded once per file in a directory ingest (the StageRecorder accumulates lists per stage). The entry-point log record for a directory ingest aggregates those per-file lists. To avoid log spam, per-file records are logged at DEBUG; the aggregated per-job record is logged at INFO.
- The HTTP `/ingest` job runner (`routes_jobs.py:95-97`) is not wired to `app.state.ingest_pipeline` in production today — the job stub (`await asyncio.sleep(0)`) emits no ingest stage-timing record until that callable is wired. Out of scope for B1.
- An honored inbound `X-Request-ID` is client-controlled but constrained to `^[A-Za-z0-9._-]{1,128}$`, which prevents log injection but does not prevent a client from reusing a previous request ID across requests.

---

## Architecture

### New modules / classes

**`archon_search/observability.py`**
```python
correlation_id: ContextVar[str | None]          # set by RequestContextMiddleware
_stage_recorder: ContextVar[StageRecorder | None]  # set by bind_stage_recorder()

def new_correlation_id() -> str: ...            # uuid4().hex
def sanitize_request_id(raw: str | None) -> str | None: ...  # enforce ^[A-Za-z0-9._-]{1,128}$

class StageRecorder:
    """Accumulates per-stage perf_counter() timings as lists (forward-compatible with B3)."""
    _timings: dict[str, list[float]]
    def record(self, name: str, elapsed_ms: float) -> None: ...  # appends; logs at DEBUG on repeated stage name
    @property
    def stage_timings_ms(self) -> dict[str, float]: ...  # last-write-wins for v1 (single-valued)

@contextmanager
def record_stage(name: str) -> Generator[None, None, None]:
    """No-op when _stage_recorder ContextVar is None. Records elapsed in finally (re-raises)."""

@contextmanager
def bind_stage_recorder() -> Generator[StageRecorder, None, None]:
    """Install a fresh StageRecorder; Token-based reset in finally (never var.set(None))."""
```

**`archon_search/server/middleware_context.py`**
```python
class RequestContextMiddleware:
    """Pure-ASGI middleware. Sets correlation_id ContextVar + X-Request-ID response header."""
    def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None: ...
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None: ...
    # Wraps `send` to inject header on http.response.start; Token-based ContextVar reset in finally.
```

### Config additions (`config.py`)
```python
@dataclass
class ObservabilityConfig:
    stage_timings_enabled: bool = True
    request_id_header: str = "X-Request-ID"

@dataclass
class SearchConfig:
    # ... existing fields ...
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
```
TOML section:
```toml
[observability]
stage_timings_enabled = true
request_id_header = "X-Request-ID"
```

### Telemetry model additions (`telemetry/entry.py`)
```python
class TelemetryEntry(BaseModel):
    # ... existing fields ...
    correlation_id: str | None = None  # new optional field; added to DOCUMENTED_SCHEMA_FIELDS
```
All four factories (`from_search_tool_result`, `from_route_response`, `from_error`, `from_explain_result`) gain an optional `correlation_id: str | None = None` keyword.

### Schema additions (`routes_explain.py`)
```python
class ExplainResponse(BaseModel):
    # ... existing fields ...
    stage_timings_ms: dict[str, float] | None = None  # None when stage_timings_enabled=False; selectively omitted from JSON via `result.pop()` when None (see Task 4.3; blanket exclude_none is NOT used)
```

### Data flow
1. Request arrives → `RequestContextMiddleware.__call__` (pure ASGI) mints/validates `X-Request-ID`, sets `correlation_id` ContextVar, wraps `send`.
2. Handler enters `bind_stage_recorder()` context (when `stage_timings_enabled`).
3. Pipeline stages call `record_stage("embed")`, `record_stage("vector")` etc. — no-op if no recorder bound.
4. Handler calls `recorder.record("total", elapsed_ms)` then reads `recorder.stage_timings_ms`, emits structured log record.
5. Handler threads `correlation_id` from ContextVar into `TelemetryEntry.from_*` factory.
6. `/explain` handler maps recorder timings to `ExplainResponse.stage_timings_ms`.
7. `RequestContextMiddleware` `send`-wrapper injects `X-Request-ID` on `http.response.start`.

---

## Task breakdown

### Phase 1 — Core observability primitives
> **Releasable**: after Task 1.2; the primitives are importable and testable in isolation, but no server integration yet.

#### Task 1.1 — `observability.py` module: ContextVars, `StageRecorder`, `record_stage`, `bind_stage_recorder`
- [x] **File**: `archon_search/observability.py`
- **Depends on**: nothing
- **Description**:
  - `correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)`
  - `_stage_recorder: ContextVar[StageRecorder | None] = ContextVar("_stage_recorder", default=None)`
  - `new_correlation_id() -> str` — returns `uuid.uuid4().hex`
  - `sanitize_request_id(raw: str | None) -> str | None` — returns `raw` if it matches `^[A-Za-z0-9._-]{1,128}$`, else `None`. Uses `re.fullmatch`.
  - `class StageRecorder`:
    - `_timings: dict[str, list[float]]` initialized to `{}`
    - `record(name: str, elapsed_ms: float) -> None` — appends to `_timings[name]`. If `len(_timings[name]) > 1`, logs `logging.debug("StageRecorder: stage %r recorded more than once", name)` (forward-compatible with B3; debug level avoids log spam during directory ingest where repeated per-file recording is expected).
    - `@property stage_timings_ms -> dict[str, float]` — returns `{k: v[-1] for k, v in self._timings.items()}` (last-write-wins for v1; B3 will widen this to list values).
    - `@property stage_sums_ms -> dict[str, float]` — returns `{k: sum(vs) for k, vs in self._timings.items()}` (sum of all recorded values per stage; use this for directory ingest aggregation where the same stage key is recorded once per file).
  - `@contextmanager record_stage(name: str) -> Generator[None, None, None]`:
    - Single `ContextVar.get()` at entry: `recorder = _stage_recorder.get()`.
    - If `recorder is None`: `yield; return` (true no-op — one `.get()` per stage, no overhead).
    - If recorder present: `t0 = time.perf_counter()`, `try: yield`, `finally: recorder.record(name, (time.perf_counter() - t0) * 1000.0)`.
    - Re-raises naturally — the `finally` records elapsed even on exception.
  - `@contextmanager bind_stage_recorder() -> Generator[StageRecorder, None, None]`:
    - Creates `recorder = StageRecorder()`.
    - `token = _stage_recorder.set(recorder)`.
    - `try: yield recorder; finally: _stage_recorder.reset(token)`.
    - Token-based reset (NEVER `_stage_recorder.set(None)`) so nested binds restore the outer context.
  - Module logger: `logger = logging.getLogger("archon.search")`.
- **Releasable**: after this task, `StageRecorder`, `record_stage`, `bind_stage_recorder`, `sanitize_request_id`, `new_correlation_id` are importable and fully testable.
- **Tests (TDD)** — `tests/test_observability.py`:
  - Unit: `test_stage_recorder_single_stage` — bind recorder, `record_stage("embed")` context, assert `stage_timings_ms == {"embed": <float>}` and value is non-negative.
  - Unit: `test_stage_recorder_multiple_stages` — record `embed`, `vector`, `fuse`; assert all three keys present.
  - Unit: `test_stage_recorder_repeated_stage_logs_warning` — record `'embed'` twice (values 10.0 then 20.0); assert `stage_timings_ms['embed'] == 20.0` (last-write-wins — the SECOND recorded value), `stage_sums_ms['embed'] == 30.0`, and `caplog` contains a message at DEBUG level (not WARNING; duplicate recording is expected during directory ingest).
  - Unit: `test_stage_sums_ms_returns_sum_across_recordings` — create a `StageRecorder`, call `record("embed", 10.0)`, `record("embed", 20.0)`, `record("embed", 30.0)`; assert `stage_sums_ms["embed"] == 60.0` and `stage_timings_ms["embed"] == 30.0` (last-write-wins). This verifies the behavioral distinction between the two properties and that directory-ingest aggregation uses the correct property.
  - Unit: `test_record_stage_noop_when_unbound` — call `record_stage("embed")` with no `bind_stage_recorder()` active; assert no exception, `_stage_recorder.get() is None`.
  - Unit: `test_record_stage_records_in_finally_on_raise` — bind recorder, `record_stage("embed")` block raises; assert the `embed` key is in `stage_timings_ms` despite the raise.
  - Unit: `test_bind_stage_recorder_token_reset_nested` — outer bind, inner bind (simulating handler calling bind again); on exit of inner bind, assert outer recorder is restored (not `None`).
  - Unit: `test_new_correlation_id_format` — `new_correlation_id()` is a 32-char hex string.
  - Unit: `test_sanitize_request_id_valid` — alphanumeric + `._-` up to 128 chars passes through unchanged.
  - Unit: `test_sanitize_request_id_rejects_newline` — value with `\n` returns `None`.
  - Unit: `test_sanitize_request_id_rejects_too_long` — 129-char string returns `None`.
  - Unit: `test_sanitize_request_id_none_input` — `None` input returns `None`.
  - Unit: `test_sanitize_request_id_empty_string` — empty string `""` returns `None` (the regex `{1,128}` requires at least 1 character).
  - Checkpoint: `uv run pytest tests/test_observability.py -v`

#### Task 1.2 — `[observability]` config section in `config.py`
- [x] **File**: `archon_search/config.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `@dataclass class ObservabilityConfig` with `stage_timings_enabled: bool = True` and `request_id_header: str = "X-Request-ID"`.
  - Add `observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)` to `SearchConfig`.
  - Parse `[observability]` TOML section in `load_config()`, mirroring the `[telemetry]` block pattern:
    - `stage_timings_enabled` — `_coerce_bool(...)`, valid bool only.
    - `request_id_header` — `str(...)`, must be non-empty string.
  - Default behavior when section is absent: `ObservabilityConfig()` (both fields default).
  - `ARCHON_SEARCH_CONFIG` env var already redirects config path — no new env var needed.
- **Releasable**: after this task, `config.observability.stage_timings_enabled` and `config.observability.request_id_header` are loadable from TOML.
- **Tests (TDD)** — `tests/test_config.py` (extend existing file):
  - Unit: `test_observability_defaults` — `load_config()` with no file returns `stage_timings_enabled=True`, `request_id_header="X-Request-ID"`.
  - Unit: `test_observability_from_toml` — TOML with `[observability] stage_timings_enabled = false` loads correctly.
  - Unit: `test_observability_invalid_bool_raises` — non-bool for `stage_timings_enabled` raises `ConfigError`.
  - Unit: `test_observability_empty_header_raises` — empty string for `request_id_header` raises `ConfigError`.
  - Checkpoint: `uv run pytest tests/test_config.py -v -k "observability"`

---

### Phase 2 — ASGI middleware and header propagation
> **Releasable**: after Task 2.2; every response from the FastAPI app and MCP app carries `X-Request-ID`.

#### Task 2.1 — Pure-ASGI `RequestContextMiddleware`
- [x] **File**: `archon_search/server/middleware_context.py`
- **Depends on**: Task 1.1
- **Description**:
  - `class RequestContextMiddleware` implementing the raw ASGI interface (NOT `BaseHTTPMiddleware`):
    ```python
    class RequestContextMiddleware:
        def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None:
            self._app = app
            self._header_name = header_name.lower().encode()  # bytes for ASGI header comparison
            self._header_name_str = header_name

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None: ...
    ```
  - In `__call__`:
    - If `scope["type"] != "http"`: `await self._app(scope, receive, send); return` (pass-through for lifespan/websocket).
    - Read inbound `X-Request-ID` header from `scope["headers"]` (bytes comparison, case-insensitive via lowercased header name).
    - `raw_id = <decoded header value or None>`.
    - `sanitized = sanitize_request_id(raw_id)`.
    - `request_id = sanitized if sanitized is not None else new_correlation_id()`.
    - `token = correlation_id.set(request_id)` — Token-based.
    - Define inner `async def send_with_header(message)`:
      - If `message["type"] == "http.response.start"`:
        - Copy headers, de-dup: remove existing `x-request-id` header if present, then append `(b"x-request-id", request_id.encode())`.
        - Forward modified message.
      - Else: `await send(message)`.
    - `try: await self._app(scope, receive, send_with_header)`.
    - `finally: correlation_id.reset(token)`.
  - IMPORTANT: the ContextVar is set **before** `await self._app(...)` in the same task — this is what guarantees downstream visibility without relying on anyio task-context copying.
- **Releasable**: after this task, the middleware is instantiable and testable standalone.
- **Tests (TDD)** — `tests/server/test_middleware_context.py`:
  - Unit: `test_mints_request_id_when_absent` — bare ASGI app wrapped in middleware, no inbound header → response has `X-Request-ID` matching `^[A-Za-z0-9]{32}$`.
  - Unit: `test_honors_valid_inbound_id` — valid inbound `X-Request-ID` is echoed unchanged in response.
  - Unit: `test_rejects_malicious_id_with_newline` — inbound value containing `\n` → response `X-Request-ID` is a fresh UUID (not the malicious value).
  - Unit: `test_rejects_too_long_id` — 129-char value → fresh UUID generated.
  - Unit: `test_correlation_id_contextvar_set_during_request` — middleware + a downstream ASGI handler that reads `correlation_id.get()` and asserts it equals the response header value.
  - Unit: `test_contextvar_reset_after_request` — after the request completes, `correlation_id.get()` is `None` (token reset worked).
  - Unit: `test_non_http_scope_passthrough` — lifespan `scope["type"] = "lifespan"` passes through untouched.
  - Checkpoint: `uv run pytest tests/server/test_middleware_context.py -v`

#### Task 2.2 — Register middleware on FastAPI app and MCP Starlette app
- [x] **Files**: `archon_search/server/app.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 2.1, Task 1.2
- **Description**:
  - In `create_app` (`app.py`): after the existing `add_middleware` calls (line 122-123), add:
    ```python
    from archon_search.server.middleware_context import RequestContextMiddleware
    app.add_middleware(
        RequestContextMiddleware,
        header_name=config.observability.request_id_header,
    )
    ```
    LIFO order means this runs **outermost** (before `APIKeyMiddleware` and `CORSMiddleware`), which is intentional — correlation IDs appear on CORS preflights and `401`s.
  - In `create_mcp_http_app` (`mcp.py`): after `starlette_app.add_middleware(APIKeyMiddleware, ...)`, add:
    ```python
    starlette_app.add_middleware(
        RequestContextMiddleware,
        header_name=request_id_header,
    )
    ```
    The MCP app is a separate Starlette app — it does NOT inherit the FastAPI middleware stack.
  - Add a `request_id_header: str = "X-Request-ID"` parameter to `create_mcp_http_app` and thread the value from `create_app` when both apps are created (pass `config.observability.request_id_header`). This keeps both surfaces consistent — the MCP app honors the same configured header name as the REST app.
- **Releasable**: after this task, every response from the FastAPI app and MCP app includes `X-Request-ID`.
- **Tests (TDD)** — extend `tests/test_app.py` and `tests/test_mcp.py`:
  - Integration (`test_app.py`): `test_health_has_request_id` — `GET /health` response has `X-Request-ID` header matching sanitize charset.
  - Integration (`test_app.py`): `test_401_has_request_id` — unauthenticated request gets `401` with `X-Request-ID`.
  - Integration (`test_app.py`): `test_options_preflight_has_request_id` — CORS preflight `OPTIONS /search` (no auth) gets `X-Request-ID` (confirms outermost placement).
  - Integration (`test_app.py`): `test_inbound_id_echoed` — valid inbound `X-Request-ID` is echoed on `GET /health`.
  - Integration (`test_mcp.py`): `test_mcp_request_id_on_valid_message` — `POST /` with a valid MCP JSON-RPC request body; assert response has `X-Request-ID` header. NOTE: the MCP Starlette app does NOT have a `/health` endpoint; test against a valid MCP protocol message instead.
  - Integration (`test_mcp.py`): `test_request_id_present_when_timings_disabled` — with `stage_timings_enabled=False`, MCP response still carries `X-Request-ID` header (validates the disabled path preserves correlation ID per AC#11).
  - Integration (`test_app.py`): `test_websocket_scope_passthrough` — WebSocket upgrade scope passes through the middleware without attempting header injection (scope type `"websocket"` is treated identically to lifespan: forwarded without modification).
  - Checkpoint: `uv run pytest tests/test_app.py tests/test_mcp.py -v -k "request_id"`

---

### Phase 3 — Pipeline stage instrumentation
> **Releasable**: after Task 3.5; all retrieval and ingest stages populate a bound `StageRecorder` when one is active.

#### Task 3.1 — `record_stage` in `Embedder.embed` and `embed_one`
- [x] **File**: `archon_search/embedder.py`
- **Depends on**: Task 1.1
- **Description**:
  - Wrap the `await asyncio.to_thread(...)` call in `embed()` with `record_stage("embed")`:
    ```python
    with record_stage("embed"):
        result = await asyncio.to_thread(self._backend.encode, texts)
    ```
  - `embed_one` calls `embed()` and will inherit the timing through it — no separate wrap needed.
  - Import: `from archon_search.observability import record_stage`.
  - No signature changes; no change to embedding_dim logic.
  - The `record_stage` no-op guard means this is invisible to unit tests that don't bind a recorder.
- **Releasable**: after this task, calls to `Embedder.embed` inside a `bind_stage_recorder()` context record `"embed"` timings.
- **Tests (TDD)** — `tests/test_embedder.py` (extend existing):
  - Unit: `test_embed_records_stage_when_bound` — bind a recorder, call `embedder.embed(["text"])` with a stub backend; assert `recorder.stage_timings_ms` has `"embed"` key with non-negative float.
  - Unit: `test_embed_noop_when_unbound` — call `embedder.embed(["text"])` with no recorder; assert no error, returns normally.
  - Checkpoint: `uv run pytest tests/test_embedder.py -v -k "stage"`

#### Task 3.2 — `record_stage` in `Reranker.rerank`
- [x] **File**: `archon_search/reranker.py`
- **Depends on**: Task 1.1
- **Description**:
  - Wrap the `await asyncio.to_thread(...)` call in `Reranker.rerank()` with `record_stage("rerank")`:
    ```python
    with record_stage("rerank"):
        scores = await asyncio.to_thread(self._backend.predict, pairs)
    ```
  - ALSO wrap the identical `await asyncio.to_thread(self._backend.predict, pairs)` call inside `Reranker._rerank_with_trace()` with the same `record_stage("rerank")` wrapper. `pipeline.explain()` calls `self._reranker._rerank_with_trace()` — this is the explain path's reranker; `Reranker.rerank()` is only called from the search path. Without instrumenting `_rerank_with_trace`, the `"rerank"` stage is never recorded for `/explain` calls and AC#5 fails.
  - Import: `from archon_search.observability import record_stage`.
  - No signature changes.
- **Releasable**: after this task, calls to `Reranker.rerank` AND `Reranker._rerank_with_trace` inside a bound context record `"rerank"` timings (covering both the search path and the explain path).
- **Tests (TDD)** — `tests/test_reranker.py` (extend existing):
  - Unit: `test_rerank_records_stage_when_bound` — bind recorder, call `reranker.rerank(query, candidates, top_k)` with stub backend; assert `recorder.stage_timings_ms` has `"rerank"` key.
  - Unit: `test_rerank_noop_when_unbound` — no recorder; call succeeds normally.
  - Unit: `test_rerank_with_trace_records_stage_when_bound` — bind recorder, call `reranker._rerank_with_trace(query, candidates, top_k)` with stub backend; assert `recorder.stage_timings_ms` has `"rerank"` key. This verifies the explain path is also instrumented.
  - Checkpoint: `uv run pytest tests/test_reranker.py -v -k "stage"`

#### Task 3.3 — `record_stage` in `SearchStore.hybrid_search` and `_hybrid_search_with_trace`
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - In `SearchStore.hybrid_search` (around line 706):
    - Wrap the vector search block with `record_stage("vector")`:
      ```python
      with record_stage("vector"):
          vec_q = table.vector_search(query_vector)
          if pred:
              vec_q = vec_q.where(pred)
          vec_rows = await vec_q.limit(fetch).to_list()
          vec_rank = {r["chunk_id"]: i for i, r in enumerate(vec_rows)}
      ```
    - For the FTS block, do NOT use `with record_stage("fts"):` inside the `try`. Because `record_stage` records in `finally`, it would record `"fts"` even when an exception is caught mid-block — violating AC#9 (fts key must be absent on the degraded path). Instead, record manually on success only:
      ```python
      _fts_t0 = time.perf_counter()
      try:
          fts_q = await table.search(query_text, query_type="fts")
          if pred:
              fts_q = fts_q.where(pred)
          fts_rows = await fts_q.limit(fetch).to_list()
          fts_rank = {r["chunk_id"]: i for i, r in enumerate(fts_rows)}
          # record only on success — key absent on degraded path:
          _fts_recorder = _stage_recorder.get()
          if _fts_recorder is not None:
              _fts_recorder.record("fts", (time.perf_counter() - _fts_t0) * 1000.0)
      except Exception as exc:
          ...  # existing handler unchanged; "fts" key remains absent
      ```
      `with record_stage(...)` is appropriate for `vector` and `fuse` (which do not have this degraded-path exception behavior).
    - Wrap the RRF fusion block with `record_stage("fuse")`:
      ```python
      with record_stage("fuse"):
          # all_rows merge + RRF scoring loop (existing lines 749-763)
      ```
  - In `_hybrid_search_with_trace` (around line 1033): apply the same instrumentation as `hybrid_search` — `with record_stage("vector"):` for the vector block, `with record_stage("fuse"):` for the RRF fusion block, and the **manual record-on-success-only pattern** for FTS (identical code to the snippet shown above for `hybrid_search`). Do NOT use `with record_stage("fts"):` — the `finally`-based context manager would record `"fts"` even when FTS raises, violating the degraded-path contract.
  - Import: `from archon_search.observability import record_stage`.
  - No f-string SQL — existing `_where_eq`/`_where_in` pattern unchanged.
  - No signature changes.
- **Releasable**: after this task, `hybrid_search` and `_hybrid_search_with_trace` record `vector`, `fts` (when FTS runs), and `fuse` stages.
- **Tests (TDD)** — `tests/test_store_trace.py` (extend existing):
  - Unit: `test_hybrid_search_records_vector_fuse_stages` — real in-memory LanceDB, bind recorder, call `hybrid_search`; assert `{"vector", "fuse"} ⊆ recorder.stage_timings_ms.keys()`.
  - Unit: `test_hybrid_search_records_fts_when_index_exists` — collection with FTS index; assert `"fts"` key present.
  - Unit: `test_hybrid_search_omits_fts_when_no_index` — collection without FTS index; assert `"fts"` key absent, `"vector"` and `"fuse"` present. This verifies the manual-record pattern (not `with record_stage("fts"):`) is used — the `finally`-based context manager would incorrectly record `"fts"` even on the exception path.
  - Unit: `test_hybrid_search_trace_records_same_stages` — same assertions via `hybrid_search_with_trace`.
  - Unit: `test_hybrid_search_with_trace_omits_fts_when_no_index` — collection without FTS index; call `_hybrid_search_with_trace`; assert `"fts"` key absent from `recorder.stage_timings_ms`, `"vector"` and `"fuse"` present.
  - Checkpoint: `uv run pytest tests/test_store_trace.py -v -k "stage"`

#### Task 3.4 — `record_stage("route")` in `MultiCollectionRouter._score_collections`
- [x] **File**: `archon_search/router.py`
- **Depends on**: Task 1.1
- **Description**:
  - Wrap the body of `_score_collections()` with `record_stage("route")`:
    ```python
    def _score_collections(self, query_embedding, collections):
        with record_stage("route"):
            # existing body
    ```
  - This is a synchronous method (no `await`), but `record_stage` is a `contextmanager`, not `asynccontextmanager` — it works on sync code too (it uses `time.perf_counter()` inside `finally`).
  - `_score_collections` is called by both `rank()` and `rank_with_scores()`, so both paths are instrumented.
  - `rank()` is called in Tier 3 of routing only (`router.py:203-205`, inside `get_pre_context` when `n_routable > shortlist_size`); `rank_with_scores()` is called by collectionless `/explain`. The no-op guard means Tier 1-2 routing (which bypasses `_score_collections`) records nothing — correct.
  - Import: `from archon_search.observability import record_stage`.
- **Releasable**: after this task, `_score_collections` calls record `"route"` timings when a recorder is bound.
- **Tests (TDD)** — `tests/test_router.py` (extend existing):
  - Unit: `test_score_collections_records_route_stage` — bind recorder, call `router._score_collections(vec, metas)` with a stub collection list; assert `"route"` key in `recorder.stage_timings_ms`.
  - Unit: `test_rank_with_scores_records_route_stage` — bind recorder, call `router.rank_with_scores(vec, metas)`; assert `"route"` in timings.
  - Checkpoint: `uv run pytest tests/test_router.py -v -k "stage"`

#### Task 3.5 — `record_stage("context")` in `SearchPipeline.search_with_context` and ingest stages in `ingest_file`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 1.1
- **Description**:
  - In `SearchPipeline.search_with_context` neighbor-fetch loop (`pipeline.py:340-343`): wrap the per-result `store.fetch_adjacent_chunks` + `apply_acl_filter` loop with `record_stage("context")`:
    ```python
    with record_stage("context"):
        for res in result.results:
            # existing neighbor-fetch loop
    ```
  - In `SearchPipeline.ingest_file` (`pipeline.py:154`):
    - Wrap the parse step (`await self._parser.parse(path)`, line ~172) with `record_stage("parse")`.
    - Wrap the embed step (`await self._embedder.embed(...)`, line ~217) with `record_stage("embed")`.
      NOTE: `embed` records inside `Embedder.embed` (Task 3.1) AND here in ingest_file — the brief calls for wrapping in `ingest_file` at the ingest parse/embed/persist seam, but `Embedder.embed` already wraps internally. To avoid double-counting, the `embed` wrap in `ingest_file` should be **removed** — the `Embedder.embed` wrap from Task 3.1 already handles it. Only add `record_stage("parse")` and `record_stage("persist")` here.
    - Wrap the persist steps (`store.ensure_collection` + `store.delete_document` + `store.ingest_chunks` + `rebuild_fts_index`, lines ~224-229) together with `record_stage("persist")`.
  - Do NOT add `record_stage` calls in `ingest_directory` — it reaches the ingest instrumentation through its per-file `ingest_file` loop.
  - Import: `from archon_search.observability import record_stage`.
- **Releasable**: after this task, the search pipeline records `{embed, vector, fts, fuse, rerank, context}` for `search_with_context` and `{parse, embed, persist}` for `ingest_file` (within a bound recorder).
- **Tests (TDD)** — `tests/test_pipeline.py` (extend existing):
  - Unit: `test_search_with_context_records_context_stage` — bind recorder, call `pipeline.search_with_context(query, collection)` with stubs; assert `"context"` key in `recorder.stage_timings_ms`.
  - Unit: `test_ingest_file_records_parse_embed_persist` — bind recorder, call `pipeline.ingest_file(path, collection)` with stubs; assert `{"parse", "embed", "persist"} ⊆ recorder.stage_timings_ms.keys()`. (AC#7a from brief.)
  - Unit: `test_pipeline_noop_when_unbound` — call `pipeline.search(query, collection)`, `pipeline.search_with_context(query, collection)`, and `pipeline.ingest_file(path, collection)` each with no recorder bound; assert no error for any of them and `_stage_recorder.get() is None` throughout.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "stage"`

---

### Phase 4 — Telemetry and response surface
> **Releasable**: after Task 4.3; `correlation_id` flows into telemetry JSONL and `stage_timings_ms` appears on `/explain`.

#### Task 4.1 — `correlation_id` field on `TelemetryEntry`
- [x] **File**: `archon_search/telemetry/entry.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `correlation_id: str | None = None` to `TelemetryEntry` (after `filter_flags`, before the factory methods).
  - Add `"correlation_id"` to `DOCUMENTED_SCHEMA_FIELDS` (line ~83-99).
  - Add `correlation_id: str | None = None` keyword argument to all four factory classmethods:
    - `from_search_tool_result` — passes `correlation_id=correlation_id` into `cls(...)`.
    - `from_route_response` — same.
    - `from_error` — same.
    - `from_explain_result` — same.
  - The factories remain keyword-only for all privacy-critical args. `extra="forbid"` and `frozen=True` stay unchanged — `query` is still rejected.
  - No `query` parameter is added. The structural test (AC#8c) still passes.
- **Releasable**: after this task, `TelemetryEntry` instances can carry a `correlation_id`; JSONL lines written by the writer will include it when non-None.
- **Tests (TDD)** — `tests/telemetry/test_entry.py` (extend existing or create):
  - Unit: `test_from_route_response_accepts_correlation_id` — factory call with `correlation_id="abc123"` produces entry with that field.
  - Unit: `test_correlation_id_default_none` — factory call without `correlation_id` kwarg → field is `None`.
  - Unit: `test_query_kwarg_still_rejected` — `TelemetryEntry(query="x", ...)` raises `ValidationError` (`extra="forbid"`).
  - Unit: `test_query_not_in_factory_signatures` — `inspect.signature(TelemetryEntry.from_route_response).parameters` does NOT contain `"query"` but DOES contain `"correlation_id"`. (AC#8b.)
  - Unit: `test_correlation_id_in_documented_schema_fields` — `"correlation_id" in DOCUMENTED_SCHEMA_FIELDS`.
  - Checkpoint: `uv run pytest tests/telemetry/ -v -k "correlation_id"`

#### Task 4.2 — Thread `correlation_id` into telemetry enqueue sites
- [ ] **Files**: `archon_search/server/routes_search.py`, `archon_search/server/routes_route.py`, `archon_search/server/routes_explain.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 4.1, Task 2.2 (correlation_id ContextVar is set by middleware)
- **Description**:
  - At every `TelemetryEntry.from_*` call site in the four files above, add:
    ```python
    from archon_search.observability import correlation_id as _correlation_id
    # ...
    writer.enqueue(
        TelemetryEntry.from_route_response(
            ...,
            correlation_id=_correlation_id.get(),
        )
    )
    ```
  - `_correlation_id.get()` returns `None` when no middleware set it (e.g. in unit tests without the full app) — safe, `TelemetryEntry.correlation_id` defaults to `None`.
  - In `routes_route.py`: all four `writer.enqueue(...)` call sites (ok + timeout + 400 + 500).
  - In `routes_search.py`: all existing `writer.enqueue(...)` call sites (ok + timeout + 500).
  - In `routes_explain.py`: `_emit_ok` and `_emit_err` inner functions.
  - In `mcp.py`: `search`, `search_with_context`, `explain` tool enqueue sites.
  - MCP tools have no `request.state` — they read from the ContextVar (already set by `RequestContextMiddleware` registered on the MCP Starlette app in Task 2.2).
- **Releasable**: after this task, every telemetry JSONL line for `/search`, `/route`, `/explain` (REST + MCP) carries `correlation_id` when the middleware is active.
- **Tests (TDD)** — `tests/server/test_routes_route_telemetry.py` (extend existing pattern) and `tests/test_mcp.py`:
  - Integration: `test_route_telemetry_has_correlation_id` — `POST /route` with telemetry writer mock; drain writer; assert JSONL line `correlation_id` equals the response `X-Request-ID` header. (AC#2 from brief.)
  - Integration: `test_search_telemetry_has_correlation_id` — same for `POST /search`.
  - Integration: `test_correlation_id_not_query` — JSONL line has `correlation_id` field but NOT `query` field. (AC#8a.)
  - Checkpoint: `uv run pytest tests/server/test_routes_route_telemetry.py tests/test_mcp.py -v -k "correlation_id"`

#### Task 4.3 — `stage_timings_ms` field on `ExplainResponse` and population in REST `/explain` and MCP `explain`
- [ ] **Files**: `archon_search/server/routes_explain.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 1.1, Task 3.1–3.5, Task 4.1
- **Description**:
  - In `routes_explain.py`:
    - Add `stage_timings_ms: dict[str, float] | None = None` to `ExplainResponse` (as optional field). To avoid silently dropping unrelated `None` fields (e.g. `routing: null`), do NOT switch to a blanket `exclude_none=True`. Instead, omit `stage_timings_ms` from the serialized dict selectively: when the field is `None`, delete it from the dict before returning. The safest pattern: call `model_dump(mode="json")` then `result.pop("stage_timings_ms", None)` when `stage_timings_ms is None`. When returning the result, the handler must return `JSONResponse(content=result_dict, status_code=200)` rather than the `ExplainResponse` instance directly — because `model_dump()` + selective `pop()` produces a dict, not a Pydantic model. FastAPI's `response_model` validation is bypassed in this case; the `ExplainResponse` Pydantic type still serves as documentation. Import `JSONResponse` from `fastapi.responses`. The MCP path already does `response.model_dump(mode="json")` so the selective pop there is straightforward.
    - Update `ExplainResponse.from_pipeline_result` to accept an optional `stage_timings_ms: dict[str, float] | None = None` kwarg and pass it through.
    - In the `explain_endpoint` handler:
      - Import `bind_stage_recorder`, `correlation_id`, `ExitStack` from `archon_search.observability` and `contextlib`.
      - Check `config.observability.stage_timings_enabled` at the top of the handler.
      - Use `ExitStack` for conditional recorder binding — this avoids duplicating the handler body for the enabled vs. disabled path:
        ```python
        from contextlib import ExitStack
        with ExitStack() as stack:
            recorder = stack.enter_context(bind_stage_recorder()) if enabled else None
            t0 = time.perf_counter()
            result = await pipeline.explain(...)
            if recorder is not None:
                recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                logger.info(
                    "stage timings",
                    extra={
                        "event_type": "stage_timings",
                        "correlation_id": correlation_id.get(),
                        "endpoint": "explain",
                        "collection": chosen,
                        "stage_timings_ms": recorder.stage_timings_ms,
                    },
                )
        ```
      - Note: `recorder.record("total", ...)` is the correct API. Do NOT write `recorder.stage_timings_ms["total"] = ...` — `stage_timings_ms` is a `@property` returning a new dict each time; assigning to it is a silent no-op.
      - Pass `stage_timings_ms=recorder.stage_timings_ms if recorder is not None else None` to `ExplainResponse.from_pipeline_result`.
  - In `mcp.py` `explain` tool:
    - Mirror the same `ExitStack` + `bind_stage_recorder()` + `recorder.record("total", ...)` + log + `stage_timings_ms` population pattern.
    - Return the dict via `model_dump(mode="json")` and then remove `"stage_timings_ms"` key if it is `None` — do NOT change other fields' serialization behavior.
  - OpenAPI: `stage_timings_ms` is a Pydantic optional field — FastAPI generates `nullable: true` schema for it automatically. No manual override needed.
- **Releasable**: after this task, `/explain` and MCP `explain` return `stage_timings_ms` in the response body when `stage_timings_enabled=True`.
- **Tests (TDD)** — `tests/test_pipeline_explain.py` and `tests/server/test_routes_explain.py` (extend):
  - Integration: `test_explain_stage_timings_keys_pinned_collection_with_rerank` — pinned collection, `rerank=true`; assert `stage_timings_ms` keys `== {"embed", "vector", "fts", "fuse", "rerank", "total"}` (use `tests/fixtures/explain_corpus/`). **Values not asserted.** (AC#5.)
  - Integration: `test_explain_stage_timings_keys_collectionless` — no collection; assert `"route"` key added. (AC#5.)
  - Integration: `test_explain_stage_timings_no_rerank` — `rerank=false`; assert `"rerank"` key absent. (AC#5.)
  - Integration: `test_explain_stage_timings_fts_absent_degradation` — corpus without FTS index; `"fts"` absent, `"vector"` and `"fuse"` present. (AC#9.)
  - Integration: `test_explain_stage_timings_values_non_negative` — all values in `stage_timings_ms` are `float >= 0`.
  - Integration: `test_explain_stage_timings_disabled` — `observability.stage_timings_enabled=False`; `"stage_timings_ms"` key absent from response body entirely. (AC#11.) Assert that when `stage_timings_enabled=False`, the response JSON dict does NOT contain the key `'stage_timings_ms'` at all (not even as null) — i.e. `'stage_timings_ms' not in response.json()`.
  - Integration: `test_rest_mcp_explain_key_parity` — for identical inputs, `set(rest_response["stage_timings_ms"]) == set(mcp_response["stage_timings_ms"])`. (AC#10.)
  - Integration: `test_mcp_explain_emits_stage_timings_log_record` — call MCP `explain` tool with a valid request; assert `caplog` contains exactly one record with `event_type=="stage_timings"`, `endpoint=="explain"`, and `"total" in record.stage_timings_ms`. (ROOT-7: previously untested MCP explain structured-log emission.)
  - Checkpoint: `uv run pytest tests/test_pipeline_explain.py tests/server/ -v -k "stage_timings"`

---

### Phase 5 — Handler binding: structured-log emission for `/search`, `/route`, and ingest entry points
> **Releasable**: after Task 5.1; all instrumented request paths emit one `event_type="stage_timings"` log record per request when enabled.

#### Task 5.1 — Structured-log emission in REST `/search` and `/route` handlers
- [ ] **Files**: `archon_search/server/routes_search.py`, `archon_search/server/routes_route.py`
- **Depends on**: Task 1.1, Task 2.2, Task 3.1–3.5, Task 4.2
- **Description**:
  - In `routes_search.py` (`search` handler):
    - Import `bind_stage_recorder`, `correlation_id` from `archon_search.observability`.
    - Use `ExitStack` for conditional recorder binding (same pattern as Task 4.3 — see cross-reference):
      ```python
      from contextlib import ExitStack
      with ExitStack() as stack:
          recorder = stack.enter_context(bind_stage_recorder()) if enabled else None
          t0 = time.perf_counter()
          result = await pipeline.search(...)
          if recorder is not None:
              recorder.record("total", (time.perf_counter() - t0) * 1000.0)
              logger.info(
                  "stage timings",
                  extra={
                      "event_type": "stage_timings",
                      "correlation_id": correlation_id.get(),
                      "endpoint": "search",
                      "collection": body.collection,
                      "stage_timings_ms": recorder.stage_timings_ms,
                  },
              )
      ```
      Note: use `recorder.record("total", ...)` — NOT `recorder.stage_timings_ms["total"] = ...` (the property returns a new dict; assignment is a silent no-op).
    - On exception paths (timeout, 5xx), emit the same record in the exception handler (partial timings are still useful — `total` may be partial, but sub-stages already recorded are valid; see AC#6).
    - No `stage_timings_ms` field added to `SearchResponse` — timings are log-only for `/search`.
  - In `routes_route.py` (`route` handler): mirror the same `ExitStack` pattern with `endpoint="route"` and `collection=None`.
  - Access `config.observability.stage_timings_enabled` via `request.app.state.config.observability.stage_timings_enabled`.
- **Releasable**: after this task, `/search` and `/route` emit one `stage_timings` log record per request.
- **Tests (TDD)** — `tests/server/test_routes_search.py`, `tests/server/test_routes_route.py` (extend):
  - Integration: `test_search_emits_stage_timings_record` — `POST /search` success path; `caplog.records` contains exactly one record with `record.event_type == "stage_timings"`, `record.endpoint == "search"`, `"total" in record.stage_timings_ms`. (AC#6.)
  - Integration: `test_route_emits_stage_timings_record` — `POST /route` success; same assertion with `endpoint="route"`. (AC#6.)
  - Integration: `test_stage_timings_disabled_no_log_record` — `stage_timings_enabled=False`; no record with `event_type=="stage_timings"`. (AC#11.)
  - Integration: `test_stage_timings_record_has_correlation_id` — `correlation_id` on the log record matches the `X-Request-ID` response header. (AC#2.)
  - Integration: `test_concurrent_requests_have_distinct_ids` — two concurrent `/search` requests with distinct inbound `X-Request-ID` values; assert each response echoes its own ID and the two log records carry distinct `correlation_id`s. (AC#12.)
  - Integration: `test_search_emits_partial_stage_timings_on_timeout` — simulate a pipeline timeout (mock raises `asyncio.TimeoutError`) during search; assert `caplog` still contains a `stage_timings` log record with at least `"embed"` key (stages completed before timeout) and `"total"` key. This validates AC#6's "on exception paths" requirement. (ROOT-8: exception-path emission was previously untested.)
  - Integration: `test_route_emits_partial_stage_timings_on_timeout` — simulate a pipeline timeout (`asyncio.TimeoutError`) during routing; assert `caplog` still contains a `stage_timings` log record with `"total"` key (mirrors the `/search` timeout test at `test_search_emits_partial_stage_timings_on_timeout`).
  - Checkpoint: `uv run pytest tests/server/test_routes_search.py tests/server/test_routes_route.py -v -k "stage_timings"`

#### Task 5.2 — Structured-log emission in MCP ingest tools and CLI ingest paths
- [ ] **Files**: `archon_search/server/mcp.py`, `archon_search/cli/ingest.py`, `archon_search/cli/collection.py`
- **Depends on**: Task 1.1, Task 2.2, Task 3.5
- **Description**:
  - In `mcp.py` `ingest_file` tool (line ~332):
    - Wrap the `pipeline.ingest_file(...)` call in `bind_stage_recorder()`.
    - Record `t0 = time.perf_counter()` before the call.
    - After `pipeline.ingest_file` returns, add `"total"`, emit log record with `endpoint="ingest"`, `stage_timings_ms` key set `{parse, embed, persist, total}`.
    - `correlation_id` from ContextVar (set by MCP middleware).
  - In `mcp.py` `ingest_directory` tool (line ~353):
    - Wrap `pipeline.ingest_directory(...)` in `bind_stage_recorder()`.
    - After the call, do NOT read `recorder.stage_timings_ms` for the log record — that property returns last-write-wins (`{k: v[-1] ...}`), which would show only the last file's per-stage timing. Instead, aggregate using `recorder.stage_sums_ms` (the `stage_sums_ms` property defined in Task 1.1) to produce true per-stage totals across all files:
      ```python
      recorder.record("total", (time.perf_counter() - t0) * 1000.0)
      aggregated = recorder.stage_sums_ms  # now includes "total" (recorded once, sum == value)
      ```
      Since "total" is recorded exactly once, `stage_sums_ms["total"] == stage_timings_ms["total"]`. The 2-line version records first then reads once — no temporal coupling.
      Emit one INFO log record with `aggregated` as `stage_timings_ms`, `endpoint="ingest"`.
    - Log per-file records are not emitted from the MCP tool — the MCP tool only emits the aggregated summary.
  - In CLI ingest paths (`cli/ingest.py`, `cli/collection.py`):
    - Find the `ingest_directory` call site(s).
    - Before the call, bind a recorder and mint a correlation ID: `cid = new_correlation_id()`.
    - After the call, call `recorder.record("total", (time.perf_counter() - t0) * 1000.0)` then read `recorder.stage_sums_ms` (now includes "total") for the aggregated per-stage totals (same 2-line pattern as the MCP tool above). Emit log record (`correlation_id=cid`, `endpoint="ingest"`).
    - CLI has no inbound header — always mints its own ID.
  - Log level: INFO for entry-point summary records (one per tool/command invocation), DEBUG for per-file detail if added later.
- **Releasable**: after this task, MCP ingest tools and CLI ingest paths emit `stage_timings` log records.
- **Tests (TDD)** — `tests/test_mcp.py` (extend), `tests/cli/test_ingest.py` (extend or create):
  - Unit/integration: `test_mcp_ingest_file_emits_stage_timings` — call MCP `ingest_file` tool with a test file + mock pipeline; assert log record `event_type=="stage_timings"`, `endpoint=="ingest"`, `{"parse", "embed", "persist", "total"} == set(record.stage_timings_ms.keys())`. (AC#7b.)
  - Unit/integration: `test_ingest_file_leaf_stages_in_recorder` — bind recorder, call `pipeline.ingest_file` directly; assert `{"parse", "embed", "persist"} ⊆ recorder.stage_timings_ms.keys()` (no `total` — leaf does not add it). (AC#7a.)
  - Unit: `test_mcp_ingest_directory_emits_aggregated_stage_sums` — bind recorder, simulate 3 file ingest calls (each recording `parse`, `embed`, `persist` stages); call `ingest_directory` with a mock pipeline that records per-file timings; assert the emitted log record's `stage_timings_ms` stage values equal the sum of per-file values (not the last file's value). Verifies `stage_sums_ms` is used rather than `stage_timings_ms` for directory aggregation. (ROOT-4.)
  - Integration: `test_cli_ingest_emits_stage_timings_log_record` — invoke the CLI ingest command with a mock pipeline; assert log record emitted with `event_type=="stage_timings"`, `endpoint=="ingest"`, and `correlation_id` is a 32-char hex string (minted by CLI since no middleware). (ROOT-6.)
  - Integration: `test_cli_collection_reindex_emits_stage_timings_log_record` — same for `collection reindex` command: invoke with a mock pipeline; assert `event_type=="stage_timings"`, `endpoint=="ingest"`, `correlation_id` is a 32-char hex string. (ROOT-6.)
  - Integration: `test_cli_collection_add_emits_stage_timings_log_record` — invoke the `collection add` CLI command with a mock pipeline; assert log record emitted with `event_type=="stage_timings"`, `endpoint=="ingest"`, and `correlation_id` is a 32-char hex string.
  - Checkpoint: `uv run pytest tests/test_mcp.py tests/cli/ -v -k "stage_timings or ingest_file"`
  - Checkpoint (CLI): `uv run pytest tests/cli/ -v -k "stage_timings"`

#### Task 5.3 — Structured-log emission in MCP `search` and `search_with_context` tools
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.1, Task 2.2, Task 3.1–3.5, Task 4.2 (Task 4.2 must be complete for MCP tool enqueue sites to carry `correlation_id`)
- **Description**:
  - In `mcp.py` `search` tool: wrap `pipeline.search(...)` with `ExitStack` + conditional `bind_stage_recorder()` (same pattern as Task 5.1 REST `/search` handler). Record `t0 = time.perf_counter()` before the call. After `pipeline.search` returns, call `recorder.record("total", (time.perf_counter() - t0) * 1000.0)` and emit one structured log record: `event_type="stage_timings"`, `endpoint="search"`, `collection=body.collection`, `stage_timings_ms=recorder.stage_timings_ms`. Use `correlation_id.get()` from the ContextVar (set by `RequestContextMiddleware` on the MCP app).
  - Note: use `recorder.record("total", ...)` — NOT `recorder.stage_timings_ms["total"] = ...` (the property returns a new dict; assignment is a silent no-op).
  - In `mcp.py` `search_with_context` tool: mirror the same pattern with `endpoint="search_with_context"`. The recorder will accumulate `{embed, vector, fts, fuse, rerank, context, total}` stages (via the pipeline instrumentation from Tasks 3.1–3.5). Log at INFO.
  - Move the tests `test_mcp_header_correlation_id_matches_log_record` and `test_mcp_search_with_context_emits_stage_timings_with_context_key` from Task 5.2's test list to THIS task's test list (they test behavior defined here).
- **Releasable**: after this task, MCP `search` and `search_with_context` tools emit `event_type="stage_timings"` log records, closing the AC#16 and AC#18 coverage gap.
- **Tests (TDD)** — `tests/test_mcp.py` (extend):
  - Integration: `test_mcp_header_correlation_id_matches_log_record` — MCP `search` tool call; assert `correlation_id` on log record equals `X-Request-ID` on response. (AC#18.)
  - Integration: `test_mcp_search_with_context_emits_stage_timings_with_context_key` — call MCP `search_with_context` tool with a pinned collection and rerank enabled; assert log record `event_type=="stage_timings"`, `endpoint=="search_with_context"`, `"context" in record.stage_timings_ms`. Key set should be `{"embed", "vector", "fts", "fuse", "rerank", "context", "total"}` for a pinned collection with rerank. (AC#16.)
  - Integration: `test_mcp_search_emits_stage_timings_record` — MCP `search` tool call; assert log record `event_type=="stage_timings"`, `endpoint=="search"`, `"total" in record.stage_timings_ms`.
  - Checkpoint: `uv run pytest tests/test_mcp.py -v -k "stage_timings"`

---

### Phase 6 — Final verification & documentation update

#### Task 6.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover and update all affected documentation:
    - `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` — document `X-Request-ID` header, correlation ID propagation, `stage_timings_ms` surface.
    - `Documentation/Architecture/210_performance_and_scalability.md` — add new in-process measurement surface; note that stage timings are blocked-coroutine wall time including event-loop latency, not pure stage CPU; reiterate report-only (not SLA).
    - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` — mark `ARCH-3` resolved.
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — document `X-Request-ID` on all responses, `stage_timings_ms` field on `/explain`, and the `[observability]` config section.
    - `archon-search.toml.example` — add `[observability]` section with defaults and comments.
    - `BREAKING.md` — add entry for the new `stage_timings_ms` field on `GET /explain` response (new optional field; absent when `stage_timings_enabled=False`; clients using strict schema validation will see a new field when timings are enabled).
  - Run full default test suite: `uv run pytest`.
  - Verify eval suite is not perturbed: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  1. **Correlation ID on every response** — `X-Request-ID` header present on `200`, `401`, and `/health`. Value matches `^[A-Za-z0-9._-]{1,128}$`.
  2. **ID propagates to log + telemetry** — a single `/route` call with telemetry enabled; drain writer before reading; JSONL `correlation_id` == log record `correlation_id` == response `X-Request-ID`.
  3. **Inbound ID honored / sanitized** — valid inbound `X-Request-ID` echoed unchanged; value with `\n` or >128 chars replaced by fresh UUID.
  4. **No-op when unbound** — `SearchPipeline.search(...)` with no `bind_stage_recorder()` returns normally; `_stage_recorder.get() is None`.
  5. **`stage_timings_ms` keys on `/explain`** — pinned + `rerank=true` → keys `== {"embed", "vector", "fts", "fuse", "rerank", "total"}`; collectionless → adds `"route"`; `rerank=false` → `"rerank"` absent. Assert keys only, never values.
  6. **Structured log record shape** — on success path, `/search`, `/route`, `/explain` each emit exactly one record with `event_type == "stage_timings"`, carrying `correlation_id` and `stage_timings_ms` dict with `"total"` key. Asserted via `record` attributes, not message text.
  7. **Ingest stage-timing record** — (a) bind recorder + call `pipeline.ingest_file` directly → `{"parse", "embed", "persist"} ⊆ recorder.stage_timings_ms.keys()`; (b) MCP `ingest_file` tool emits log record with `event_type=="stage_timings"`, `endpoint=="ingest"`, key set `== {"parse", "embed", "persist", "total"}`.
  8. **Telemetry `correlation_id` structural test** — (a) JSONL line for `/route` has `correlation_id` and not `query`; (b) `"correlation_id" in inspect.signature(TelemetryEntry.from_route_response).parameters` and `"query" not in` it; (c) `TelemetryEntry(query="x", ...)` raises `ValidationError`.
  9. **FTS-absent degradation** — corpus without FTS index → `/explain` `stage_timings_ms` omits `"fts"`, includes `"vector"`, `"fuse"`, `"total"`; no error.
  10. **REST↔MCP key parity** — `set(rest_response["stage_timings_ms"]) == set(mcp_response["stage_timings_ms"])` for identical inputs.
  11. **Disabled path** — `stage_timings_enabled=False`: `/explain` has no `stage_timings_ms` key; no `stage_timings` log record; `X-Request-ID` header and telemetry `correlation_id` still present.
  12. **Concurrency isolation** — two concurrent `/search` requests with distinct inbound IDs; each response echoes its own ID; log records carry distinct `correlation_id`s.
  13. **Eval stability** — `uv run pytest -m eval` produces same `routing_accuracy`/retrieval metrics as baseline; instrumentation does not perturb ranking.
  14. **Coverage** — `uv run pytest` passes `--cov-fail-under=85` without amendment.
  15. **OPTIONS preflight carries the ID** — CORS-preflight `OPTIONS` request gets `X-Request-ID` response header, confirming outermost middleware placement.
  16. **`search_with_context` context stage** — bound recorder emits `stage_timings` record with key set `== {"embed", "vector", "fts", "fuse", "rerank", "context", "total"}`; `"route"` absent (pinned collection).
  17. **Nested-bind isolation** — outer `bind_stage_recorder()` + inner bind; on exit of inner bind, outer recorder is restored (Token-based `var.reset(token)`, not `var.set(None)`).
  18. **MCP header↔log `correlation_id` equality** — MCP `search` tool call; `correlation_id` on log record equals `X-Request-ID` on response.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest` and confirm green; run eval suite and compare baseline.

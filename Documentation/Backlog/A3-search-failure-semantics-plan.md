# A3 — Search-Failure Semantics (CON-5)
> Ships BEFORE A4 (explain). This plan establishes the canonical taxonomy A4 will inherit.

**Purpose**: Replace the silent `except: return empty` block in `archon_search/server/routes_search.py:76-84` with a re-raise-plus-telemetry pattern patterned after `routes_route.py:152-166` (with two deliberate additions: `exc_info=True` for traceback capture and `extra={"event_type": ...}` for structured filtering), so `/search` pipeline failures surface as HTTP 500 with structured logs and a telemetry entry.
**Audience**: archon-search contributors implementing A3/CON-5 and reviewers of the resulting PR.
**Status**: Draft

---

## Background

`archon_search/server/routes_search.py:82-84` currently catches every `Exception` from `pipeline.search(...)`, logs at WARNING, and returns HTTP 200 with `results=[]` and `acl_filtered=False`. Clients (REST consumers, the CLI, IDE plugins wrapping `/search`) cannot distinguish "the corpus genuinely contains no hits" from "the embedder/store/reranker just crashed." Operators lose the failure entirely — no telemetry entry is emitted on this path, so `/telemetry/stats` and `/telemetry/entries` show clean output during an actual outage.

`routes_route.py:152-166` already implements the correct pattern: enqueue a `TelemetryEntry.from_error(...)` (wrapped in its own try/except so a telemetry failure cannot break the route), log at ERROR with the exception type, and bare-re-raise. FastAPI converts the re-raised exception to HTTP 500 with its default `{"detail": "Internal Server Error"}` envelope. This plan applies that exact pattern to `/search`.

The technical-debt roadmap tracks this as **CON-5** in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`. The full design rationale, MCP scope decision, blast-radius analysis, and test-scope detail live in `Documentation/Backlog/search-failure-semantics-brief.md` — this plan is the implementation decomposition only.

## Goal

After this plan ships, a failing search pipeline returns HTTP 500 with FastAPI's default `{"detail": "Internal Server Error"}` envelope, emits exactly one `TelemetryEntry` per failure with `endpoint="search"`, `status="internal_error"`, `error_kind="other"`, and `latency_ms > 0`, and logs at ERROR with a structured `event_type="search_pipeline_failure"` field. The 503 meta-lookup path at `routes_search.py:68-71` is unchanged. MCP `search` / `search_with_context` are unchanged (already return `McpErrorResponse`, distinguishable by payload shape — see brief §Out of Scope for caveat).

---

## Scope

### In Scope
- Replace the silent `except` block in `archon_search/server/routes_search.py:82-84` with a `routes_route.py:152-166`-shaped block: ERROR log with structured `event_type`, telemetry enqueue (wrapped in its own try/except), bare `raise`.
- Flip the four existing tests in `tests/server/test_routes_search.py` (lines 140, 262, 299, 315) from asserting `status_code == 200` + empty results to asserting `status_code == 500` + `"detail" in response.json()`. Line 386 (`test_search_store_exception_returns_503`) is **untouched** — it covers the meta-lookup path.
- Create a new file `tests/server/test_routes_search_telemetry.py` mirroring `tests/server/test_routes_route_telemetry.py` for telemetry, log-record, enqueue-resilience, and sequential-state assertions.
- Add a `BREAKING.md` entry describing the contract change.
- Update `Documentation/Architecture/140_error_handling_strategy.md` to remove the "silent failure masking" caveat.
- Update `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` CON-5 entry to mark resolved.
- Update `Documentation/OperatorGuide/05_incident_runbook.md` "Search returns empty (silent regression — CON-5)" entry — the symptom no longer presents that way.

### Out of Scope
- **MCP `search` / `search_with_context` parity** — already return `McpErrorResponse({"error": ..., "code": "internal_error"})` (`mcp.py:60-74, 108-122`). See brief §Out of Scope for caveat.
- **Typed pipeline exceptions** (`EmbedderError`, `StoreError`, `RerankerError`) with 503-for-transient mapping — deferred to a future ADR.
- **Adding telemetry to the 503 meta-lookup path** (`routes_search.py:68-71`) — separate gap, tracked but out of scope here.
- **Partial / degraded responses** (e.g., un-reranked fallback) — schema change, deferred.
- **New `search_failure` `ErrorKind` enum value** — `"other"` is the symmetric choice with `routes_route.py:159`.
- **Changing `SearchResponse` schema** — failure is signalled by HTTP status alone.
- **Feature flag** — CalVer + single-user local server makes a flag dead config.
- **Eval-corpus fixture sweep** — `tests/eval/corpus/docs/troubleshooting.md:13` describes the old symptom but is fixture data, not user-facing docs.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 2.5 — Final verification & documentation update].

---

## What does NOT change

- `SearchRequest` / `SearchResponse` / `SearchResultSchema` Pydantic schemas in `routes_search.py:17-59`.
- The 503 meta-lookup path at `routes_search.py:68-71` and its 404 sibling at line 73-74.
- HTTP 200 + `results=[]` on a **healthy** pipeline that genuinely finds no matches.
- The MCP `search` / `search_with_context` tools and their `McpErrorResponse` return shape.
- `TelemetryEntry.from_error(...)` signature — no `query` parameter is added; the structural no-raw-query invariant is preserved.
- `Bearer` auth requirement (`middleware_auth.py`) on every endpoint except `GET /health`.
- The `--cov-fail-under=85` gate on the default pytest run.
- `asyncio.CancelledError` behavior — it inherits from `BaseException`, is not caught by `except Exception`, propagates as before.

---

## Known limitations / accepted trade-offs

- **Telemetry asymmetry post-fix**: the new 500 (pipeline) path emits telemetry but the existing 503 (meta-lookup) path still does not — operators filtering `/telemetry/entries` by `endpoint="search"` see pipeline failures but not meta-lookup failures until the 503 path is also instrumented.
- **`error_kind="other"` is symmetric, not precise**: a more specific `search_failure` value would be cleaner but expands the public telemetry enum surface; deferred.
- **WARNING → ERROR log-level bump**: operators with alert rules on ERROR-level logs from `archon.search` will start seeing transient pipeline blips. The structured `event_type="search_pipeline_failure"` field lets them filter precisely if needed.
- **Bare re-raise yields a generic envelope**: clients see `{"detail": "Internal Server Error"}`, not a typed error code. Matches `routes_route.py` exactly.
- **Log/log-extra asymmetry vs `/route`**: this implementation adds `exc_info=True` and `extra={"event_type": "search_pipeline_failure"}` that `routes_route.py:152-166` does not have; `/route` may be retrofitted later for parity.
- **OpenAPI `responses=` omitted for symmetry with `routes_route.py`**: the `@router.post("/search", ...)` decorator does NOT advertise the new 500 in `responses={...}`, mirroring the existing `/route` decorator. Deliberate omission; revisit if/when both routes gain typed response docs.
- **MCP `/search` asymmetry (pre-existing)**: `mcp.py:46` is missing TWO things vs the REST `/search` handler — (a) no `namespace=` kwarg is passed to `pipeline.search`, so MCP has no ACL filtering, AND (b) no `get_collection_meta` pre-lookup, so MCP has no collection-existence 404/503 gating. Not addressed by this plan.
- **`latency_ms` upper bound on serialization failures**: when `SearchResultSchema.from_result` raises (Task 1.3 test #7), the `latency_ms` on the emitted telemetry entry includes the pipeline-success time PLUS the serialization-failure time. Operators should treat `latency_ms` on `endpoint="search"` + `status="internal_error"` as a request-duration upper bound, not pure pipeline latency.

---

## Architecture

**Modified module**: `archon_search/server/routes_search.py`. The `search(body, request)` handler's inner `try / except Exception` block (currently lines 76-84) is rewritten to mirror `routes_route.py:152-166`.

**New imports in `routes_search.py`**:
- `from time import monotonic` — for measuring `latency_ms`.
- `from archon_search.telemetry.entry import TelemetryEntry` — already imported in `routes_route.py:13` via the same module path.

**Timer placement**: `start = monotonic()` is placed at the **top of the handler** (immediately after `pipeline = request.app.state.pipeline` and `ns = request.state.namespace`, BEFORE the `get_collection_meta(...)` call), not inside the inner try. This means `latency_ms` includes meta-lookup time, which matches `routes_route.py:71` exactly and is operationally more honest (the operator-observed request duration includes meta-lookup).

**New behavior** (replacing lines 76-84, with the timer moved to handler top):

```python
# At the top of the handler, right after `pipeline = request.app.state.pipeline`
# and `ns = request.state.namespace`:
start = monotonic()

# ... existing get_collection_meta / 404 / 503 block unchanged ...

try:
    result = await pipeline.search(body.query, body.collection, namespace=ns)
    return SearchResponse(
        results=[SearchResultSchema.from_result(r) for r in result.results],
        acl_filtered=result.acl_filtered,
    )
except Exception as exc:
    writer = getattr(request.app.state, "telemetry_writer", None)
    if writer is not None:
        try:
            writer.enqueue(
                TelemetryEntry.from_error(
                    endpoint="search",
                    status="internal_error",
                    error_kind="other",
                    latency_ms=(monotonic() - start) * 1000.0,
                )
            )
        except Exception as tel_exc:
            logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
    logger.error(
        "search pipeline failed: %s",
        type(exc).__name__,
        extra={"event_type": "search_pipeline_failure"},
        exc_info=True,
    )
    raise
```

**No new config keys, env vars, or public APIs.** The behavior change is on the exception path; the success path is functionally unchanged apart from a `monotonic()` timer call added at the top of the handler (before `get_collection_meta`).

**Test architecture**: telemetry/log/resilience assertions live in a **new** `tests/server/test_routes_search_telemetry.py` patterned after `tests/server/test_routes_route_telemetry.py` — the existing `tests/server/test_routes_search.py` uses `_make_app` via `create_app()` and lacks telemetry-writer plumbing, while the route-telemetry pattern builds a minimal app with `app.state.telemetry_writer = writer_mock` directly. Status-code + envelope assertions stay in the existing file.

---

## Task breakdown

### Phase 1 — Code change with TDD
> **Releasable**: after Task 1.4 — the new behavior is fully in place, all tests pass (including the end-to-end telemetry coverage), and the contract change is live.

#### Task 1.1 — Flip existing regression tests to assert HTTP 500
- [ ] **File**: `tests/server/test_routes_search.py`
- **Depends on**: nothing
- **Description**:
  - For each of these four tests, replace the old `assert response.status_code == 200` + empty-results assertions with the new contract:
    - Line 140 — `test_search_pipeline_error_returns_empty` (rename to `test_search_pipeline_error_returns_500`).
    - Line 262 — `test_search_store_exception_returns_empty` (rename to `test_search_store_exception_returns_500`).
    - Line 299 — `test_search_embedder_failure_returns_empty` (rename to `test_search_embedder_failure_returns_500`).
    - Line 315 — `test_search_reranker_failure_returns_empty` (rename to `test_search_reranker_failure_returns_500`).
  - Each test must assert:
    - `response.status_code == 500`
    - `"detail" in response.json()`
  - **Do NOT modify** the test at line 386 (`test_search_store_exception_returns_503`) — that is the meta-lookup failure path and stays 503.
  - **Do NOT add** telemetry-writer or log-record assertions to this file — those live in Task 1.3's new file.
  - **Modify the `_make_app` helper** in `tests/server/test_routes_search.py` so that the `TestClient(...)` constructor *inside* `_make_app` receives `raise_server_exceptions=False`. (`_make_app` constructs the `TestClient` internally and returns it — the flag is set at the constructor call site within `_make_app`, NOT on a separately instantiated `TestClient`.) This is a global change affecting every test in the file, which is safe: 200, 404, and 503 responses are unaffected by the flag — it only changes behavior on unhandled exceptions, converting them to 500 responses instead of propagating as `RuntimeError`. Without this, the bare `raise` introduced in Task 1.2 propagates as an unhandled exception in the four flipped tests. Precedent: `tests/server/test_routes_route_telemetry.py:195`.
  - **Update the existing `caplog` assertion at `tests/server/test_routes_search.py:273`** from `"search failed" in record.message` to `"search pipeline failed" in record.message` to match the new log format in Task 1.2.
  - This task is the RED step of TDD: after the test edits, `uv run pytest tests/server/test_routes_search.py` fails on the four renamed tests because the production code still returns 200+[].
- **Releasable**: after this task, the test suite explicitly demands the new contract; the failing tests document the missing implementation.
- **Tests (TDD)** — this task IS the test edit:
  - The four renamed tests are themselves the deliverable. They must each:
    - Use the existing pipeline-mock helper to make `pipeline.search(...)` raise `RuntimeError("boom")` (or the per-stage equivalent already in the file).
    - Make a `POST /search` request through `_make_app`.
    - Assert `response.status_code == 500` and `"detail" in response.json()`.
    - Use a `TestClient` constructed with `raise_server_exceptions=False` (via the updated `_make_app` helper).
  - Checkpoint: `uv run pytest tests/server/test_routes_search.py -v` — the four renamed tests fail; all other tests in the file still pass (line-386 503 test, healthy-path tests, validation tests).

#### Task 1.2 — Rewrite the pipeline-exception block in `routes_search.py`
- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add imports: `from time import monotonic` and `from archon_search.telemetry.entry import TelemetryEntry` (after the existing imports at lines 1-10).
  - In the `search(body, request)` handler, replace the inner `try / except` block at lines 76-84 with the pattern in §Architecture above:
    - Start a `monotonic()` timer at the **top of the handler**, immediately after `pipeline = request.app.state.pipeline` and `ns = request.state.namespace` — i.e. BEFORE the `get_collection_meta(...)` call. This matches `routes_route.py:71` exactly so `latency_ms` includes meta-lookup time.
    - On exception: fetch `writer = getattr(request.app.state, "telemetry_writer", None)`; if non-None, enqueue a `TelemetryEntry.from_error(endpoint="search", status="internal_error", error_kind="other", latency_ms=...)` wrapped in its own `try / except Exception as tel_exc` that only emits a `logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)`.
    - Log the original exception via `logger.error("search pipeline failed: %s", type(exc).__name__, extra={"event_type": "search_pipeline_failure"}, exc_info=True)`.
    - Bare `raise` (no `HTTPException(500)`, no `JSONResponse`).
  - The 503 meta-lookup path at lines 67-71 and the 404 path at lines 73-74 are unchanged.
  - This task is the GREEN step: the four flipped tests from Task 1.1 now pass.
- **Releasable**: after this task, `/search` returns HTTP 500 on pipeline exceptions and the route compiles into the existing FastAPI app without further wiring. The telemetry enqueue is a no-op when `app.state.telemetry_writer` is unset (preserves test-app behavior in `test_routes_search.py`).
- **Tests (TDD)**:
  - The four renamed tests from Task 1.1 now pass.
  - Checkpoint: `uv run pytest tests/server/test_routes_search.py -v` — all tests in the file pass, including the unchanged 503 test at line 386.

#### Task 1.3 — Create `test_routes_search_telemetry.py` for telemetry / log / resilience / sequential assertions
- [ ] **File**: `tests/server/test_routes_search_telemetry.py` (new)
- **Depends on**: Task 1.2
- **Description**:
  - Mirror the structure of `tests/server/test_routes_route_telemetry.py`. Build a minimal FastAPI app helper that:
    - Mounts only the `routes_search.router`.
    - Sets `app.state.telemetry_writer = writer_mock` (a `Mock()` with `enqueue` as a `Mock`).
    - Sets `app.state.pipeline = pipeline_mock` whose `get_collection_meta(...)` returns a fake `CollectionMeta` (so the 503 meta-lookup path is never hit) and whose `search` attribute is an `AsyncMock` (NOT `MagicMock`) so `side_effect=[Exception, result]` and `await pipeline.search(...)` work correctly. Parameterize `search`'s `side_effect` / `return_value` per test.
    - Constructs `TestClient` with `raise_server_exceptions=False` so the bare `raise` in `routes_search.py` is converted to a 500 response rather than propagating into the test.
    - Mounts the auth middleware bypass used elsewhere in `tests/server/`, or a `request.state.namespace` injection equivalent.
  - Add the following tests:
    1. `test_store_exception_enqueues_telemetry_entry` — `pipeline.search` raises `RuntimeError("store boom")`; assert response `500`; assert `writer_mock.enqueue.call_count == 1`; assert the enqueued `TelemetryEntry` has `endpoint == "search"`, `status == "internal_error"`, `error_kind == "other"`, and `latency_ms > 0`.
    2. `test_embedder_exception_enqueues_telemetry_entry` — same as above with an embedder-shaped failure.
    3. `test_reranker_exception_enqueues_telemetry_entry` — same as above with a reranker-shaped failure.
    4. `test_pipeline_failure_logs_structured_event_type` — using `caplog.at_level(logging.ERROR, logger="archon.search")`; filter `caplog.records` to those where `record.name == "archon.search"` AND `record.levelno == logging.ERROR` AND `hasattr(record, "event_type")`, then assert exactly one such record exists and that it has `record.event_type == "search_pipeline_failure"` AND `record.exc_info is not None` (so removing `exc_info=True` from the implementation breaks the test). Use attribute access on the captured `LogRecord`, not substring match on `record.message`. ADDITIONALLY: send a recognizable sentinel string as the `query` field in the request, and assert the sentinel substring does NOT appear in `record.message` (or `record.getMessage()`) for ANY captured record under logger `archon.search` — this locks the no-raw-query invariant at the log layer too.
    5. `test_telemetry_enqueue_failure_does_not_break_route` — make `writer_mock.enqueue.side_effect = RuntimeError("telemetry down")`; pipeline still raises; assert response `500` (route does not 502/crash); assert a `WARNING` log record from `archon.search` mentioning `telemetry enqueue failed`.
    6. `test_sequential_failure_then_success_on_same_app` — first request: `pipeline.search.side_effect = [RuntimeError("boom"), SearchPipelineResult(results=[], acl_filtered=False)]`; first request returns `500`; second request returns `200` with empty results; assert `writer_mock.enqueue.call_count == 1` (only the failure enqueued); assert the enqueued `TelemetryEntry` has `latency_ms > 0`.
    7. `test_serialization_error_in_response_construction_enqueues_telemetry` — `pipeline.search` returns a result whose `results` list contains an item that makes `SearchResultSchema.from_result(...)` raise (e.g., monkeypatch `SearchResultSchema.from_result` to raise `ValueError("bad row")`); assert response `500`; assert `writer_mock.enqueue.call_count == 1` (the try block in §Architecture intentionally wraps both `pipeline.search` AND the `SearchResponse` construction, so serialization failures are also telemetered).
    8. `test_healthy_search_does_not_enqueue_telemetry` — `pipeline.search` returns a normal `SearchPipelineResult` (with or without hits); assert response `200`; assert `writer_mock.enqueue.call_count == 0`.
    9. `test_query_text_never_in_error_enqueue_args` — privacy sentinel mirroring the established pattern in `tests/server/test_routes_route_telemetry.py::test_route_handler_query_text_never_in_error_entry_args`. Use a recognizable sentinel string in the `POST /search` body's `query` field, force `pipeline.search` to raise. Then: extract the enqueued `TelemetryEntry` via `entry = writer_mock.enqueue.call_args.args[0]`, and assert the sentinel substring is NOT in `str(entry.model_dump())`. Do not stringify the whole `call_args` blob — mirror the route-telemetry pattern exactly so the assertion targets the entry payload that would actually be persisted.
    10. `test_writer_none_pipeline_failure_does_not_crash` — set `app.state.telemetry_writer = None` (the production default before telemetry is wired); force `pipeline.search` to raise; assert response `status_code == 500` and that no exception bubbles up from the route. This locks the `getattr(..., None)` guard in `routes_search.py`.
  - Use the same test-client and fixture conventions as `test_routes_route_telemetry.py` (e.g., `pytest.fixture` for the writer mock, `TestClient` from `fastapi.testclient` with `raise_server_exceptions=False`).
- **Releasable**: after this task, the new behavior is fully test-locked at the unit and integration level. Coverage on the new branch reaches 100%.
- **Tests (TDD)**:
  - The ten tests listed above ARE the deliverable.
  - Unit: tests 1–4, 7, 8 verify the synchronous behavior of one failed or successful `/search` call.
  - Integration: tests 5–6, 10 verify cross-cutting concerns (writer resilience, app-state isolation across sequential requests, no-writer fallback).
  - Privacy: test 4 (log-layer sentinel) and test 9 (telemetry-payload sentinel) enforce the structural no-raw-query invariant on the new failure path.
  - Checkpoint: `uv run pytest tests/server/test_routes_search_telemetry.py -v` — all ten tests pass.

#### Task 1.4 — Add `/search` REST error coverage to `tests/server/test_telemetry_e2e.py`
- [ ] **File**: `tests/server/test_telemetry_e2e.py`
- **Depends on**: Task 1.2
- **Description**:
  - The existing end-to-end telemetry test file exercises `/route` REST error and MCP `search` error paths but does NOT cover `/search` REST error. With Task 1.2 in place, `/search` now emits telemetry on pipeline failure and must be covered at the same fidelity.
  - Add `/search` REST error coverage to the two relevant existing tests in `test_telemetry_e2e.py`, mirroring the existing `/route` blocks in each test:
    1. The JSONL key-set-equality test — extend it to issue a failing `POST /search` request, then assert the resulting JSONL telemetry record has the same key set as the `/route` error record (i.e. structural parity of the emitted entry across endpoints).
    2. The sentinel-not-in-log test — extend it to send a recognizable sentinel string in a failing `POST /search` body's `query` field and assert the sentinel does not appear in the on-disk telemetry JSONL line for the `endpoint="search"` + `status="internal_error"` entry.
  - Reuse the file's existing fixtures, app factory, and writer wiring; do not introduce a parallel harness.
- **Releasable**: after this task, end-to-end telemetry coverage is symmetric between `/route` and `/search` failure paths.
- **Tests (TDD)**:
  - The two extended tests ARE the deliverable.
  - Checkpoint: `uv run pytest tests/server/test_telemetry_e2e.py -v` — all tests pass, including the new `/search` assertions in the two extended tests.

#### Task 1.5 — Wrap `/search` handler in `asyncio.wait_for` with 504-on-timeout
- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Task 1.2
- **Description**:
  - `/route` already wraps its long-running call in `asyncio.wait_for(..., timeout=30.0)` and returns HTTP 504 on `asyncio.TimeoutError`, with a telemetry entry tagged `status="timeout"` + `error_kind="timeout"` (see `routes_route.py:94-100` and `routes_route.py:122-135`). `/search` has no equivalent timeout today: a hung `pipeline.search(...)` blocks the request indefinitely.
  - Wrap the `await pipeline.search(...)` call (inside the existing try block from Task 1.2) in `asyncio.wait_for(pipeline.search(body.query, body.collection, namespace=ns), timeout=30.0)`. Match `routes_route.py`'s hardcoded `30.0` literal — no config knob exists for the route timeout today, and parity beats premature configurability. Add a `# TODO: make configurable via config.py (see /route for parity)` comment immediately above the literal so the future config work is discoverable.
  - Catch `asyncio.TimeoutError` **before** the existing `except Exception` block. On timeout: enqueue a `TelemetryEntry.from_error(endpoint="search", status="timeout", error_kind="timeout", latency_ms=...)` (same wrapped-in-try/except pattern as Task 1.2), log at ERROR with `extra={"event_type": "search_timeout"}` and `exc_info=True`, then `raise HTTPException(status_code=504, detail="Search timed out")` — mirroring `routes_route.py:122-135` exactly.
  - The existing `except Exception` block from Task 1.2 must NOT swallow `asyncio.TimeoutError`; ordering the timeout `except` first ensures correct dispatch. (CPython does not raise `TimeoutError` and `asyncio.TimeoutError` as the same class on all paths in current versions; matching `routes_route.py`'s catch is the safe choice.)
  - Verify `ErrorKind.timeout` already exists in `archon_search/telemetry/entry.py` (it does — line ~34) so no enum addition is needed. Do NOT add a duplicate value.
- **Releasable**: after this task, `/search` no longer hangs indefinitely on a stuck pipeline; clients see HTTP 504 within ~30s; operators see a telemetry entry with `error_kind="timeout"` for filtering and alerting.
- **Tests (TDD)**:
  - Add a new test to `tests/server/test_routes_search_telemetry.py`:
    - `test_search_pipeline_timeout_returns_504_and_enqueues_telemetry` — monkeypatch `pipeline.search` to `await asyncio.sleep(60)` (or any value larger than the timeout); use a reduced timeout in the test if feasible (e.g., monkeypatch the `30.0` literal or parameterize), otherwise accept the longer wall clock; `@pytest.mark.asyncio`; assert `response.status_code == 504`; assert `writer_mock.enqueue.call_count == 1`; assert the enqueued `TelemetryEntry` has `endpoint == "search"`, `status == "timeout"`, `error_kind == "timeout"`, `latency_ms > 0`; assert exactly one ERROR log record under logger `archon.search` with attribute `event_type == "search_timeout"`.
    - To avoid a 30s real-time wait in CI, prefer monkeypatching the timeout literal in `routes_search` (e.g., expose it as a module-level constant `_SEARCH_TIMEOUT_SECONDS = 30.0` and monkeypatch to `0.05`). This is a minor refactor inside Task 1.5 itself — call it out in the implementation: extract the literal to a module-level constant so the test can shorten it without freezing CI.
  - Checkpoint: `uv run pytest tests/server/test_routes_search_telemetry.py -v` — all prior tests still pass; the new timeout test passes; total test count = 11.
- **Acceptance criteria**:
  - HTTP 504 returned on a hung `/search` call (synthetic `asyncio.sleep(big)` in tests; real verification via a deliberately slow pipeline in manual ad-hoc check).
  - Telemetry entry recorded with `endpoint="search"`, `status="timeout"`, `error_kind="timeout"`.
  - ERROR log record carries `event_type="search_timeout"`.
  - The default `--cov-fail-under=85` coverage gate remains satisfied.
  - The existing 500-on-pipeline-exception behavior from Task 1.2 is unchanged for non-timeout exceptions.

---

### Phase 2 — Documentation and contract surface
> **Releasable**: after Task 2.5 — every doc affected by this change reflects the shipped behavior, the breaking change is announced, and the technical-debt roadmap entry is marked resolved.

#### Task 2.1 — Add `BREAKING.md` entry
- [ ] **File**: `BREAKING.md`
- **Depends on**: Task 1.2
- **Description**:
  - Append a new bullet under the existing `[next release]` heading in `BREAKING.md` — do NOT create a new heading. Match the existing entry style in the file.
  - Content: one sentence describing the change — `/search` pipeline exceptions now return HTTP 500 with `{"detail": "Internal Server Error"}` instead of HTTP 200 with `results=[]`. The 503 meta-lookup path is unchanged. MCP `search` / `search_with_context` are unchanged.
  - Reference: `routes_search.py` exception path; this is CON-5 / A3.
- **Releasable**: after this task, downstream consumers reading `BREAKING.md` see the contract change before upgrading.
- **Tests (TDD)**: N/A — documentation. Checkpoint: `git diff BREAKING.md` shows the new entry; manual read confirms it follows the file's existing format.

#### Task 2.2 — Update `Documentation/Architecture/140_error_handling_strategy.md`
- [ ] **File**: `Documentation/Architecture/140_error_handling_strategy.md`
- **Depends on**: Task 1.2
- **Description**:
  - Remove the "silent failure masking" caveat referenced at line 16 and lines 82-84 of that file (the brief cites these exact ranges). Replace the prose so the doc reflects the new behavior: `/search` pipeline exceptions now follow the same pattern as `/route` — log at ERROR, enqueue telemetry, re-raise as HTTP 500.
  - Preserve the doc's existing structure; only edit the affected paragraphs.
  - If the doc has a section listing endpoints with non-standard error behavior, remove `/search` from that list.
  - **503 now covers TWO distinct failure classes** — update the status-code table accordingly. Today 503 is documented as "meta-lookup failure only" (router/`/route` and `/search` when collection metadata is unavailable). A1 (metadata schema v1 — ingest hardening) introduces HTTP 503 + `Retry-After` for `StoreBusyError` on `/ingest/*` and `/reindex/*` when an ingest lock is contended. Both classes use `Retry-After`. In 140's status-code table, split the 503 row (or add a sub-row / footnote) so it lists: (a) router/meta-lookup failure on `/route` and `/search` when collection metadata unavailable, and (b) store lock contention on `/ingest/*` and `/reindex/*` (from A1). Cross-reference the A1 plan for the lock-contention semantics.
- **Releasable**: after this task, the error-handling strategy doc is consistent with the implemented behavior.
- **Tests (TDD)**: N/A — documentation. Checkpoint: `git diff Documentation/Architecture/140_error_handling_strategy.md` — the silent-failure caveat is gone; no other content drifted.

#### Task 2.2b — Update `Documentation/Architecture/600_api_reference_or_public_interface.md`
- [ ] **File**: `Documentation/Architecture/600_api_reference_or_public_interface.md`
- **Depends on**: Task 1.2
- **Description**:
  - Line 53 of this file currently states that `/search` returns HTTP 200 with empty results on pipeline failure. Rewrite that line (and any surrounding prose) to state that `/search` now returns HTTP 500 with `{"detail": "Internal Server Error"}` on pipeline failure; HTTP 200 with `results=[]` is reserved for genuine no-match results on a healthy pipeline. The 503 meta-lookup path is unchanged.
  - This is explicit because the Task 2.5 sweep should not be relied on to catch this canonical-API doc.
- **Releasable**: after this task, the authoritative API-reference doc reflects the shipped behavior.
- **Tests (TDD)**: N/A — documentation. Checkpoint: `git diff Documentation/Architecture/600_api_reference_or_public_interface.md` — only the `/search` status-code prose changed.

#### Task 2.3 — Update `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` (CON-5 resolved)
- [ ] **File**: `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`
- **Depends on**: Task 1.2
- **Description**:
  - Locate the CON-5 entry (around line 63 per brief evidence).
  - Mark it resolved per the file's existing convention (e.g., move to a "Resolved" section, strike-through, or add a "Resolved in YY.M.<rev>" note — match whatever style the file uses for prior resolved items).
  - Cross-reference the `BREAKING.md` entry from Task 2.1 if the file's style includes such links.
- **Releasable**: after this task, the tech-debt roadmap no longer lists CON-5 as open work.
- **Tests (TDD)**: N/A — documentation. Checkpoint: `git diff Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` — CON-5 marked resolved; no unrelated entries changed.

#### Task 2.4 — Update `Documentation/OperatorGuide/05_incident_runbook.md`
- [ ] **File**: `Documentation/OperatorGuide/05_incident_runbook.md`
- **Depends on**: Task 1.2
- **Description**:
  - Locate the "Search returns empty (silent regression — CON-5)" runbook entry (around line 95 per cycle-2 verification).
  - Rewrite the symptom and diagnosis: failures now present as HTTP 500 on `/search` plus an ERROR-level log carrying `event_type="search_pipeline_failure"` plus a telemetry entry visible in `/telemetry/entries` with `endpoint="search"`, `status="internal_error"`, `error_kind="other"`.
  - Update the operator-action guidance accordingly (e.g., "filter telemetry by `event_type` or by `endpoint=search` + `status=internal_error` to confirm").
  - Keep the runbook entry's structure (symptom / diagnosis / action / escalation) consistent with siblings in the file.
- **Releasable**: after this task, the incident runbook reflects the new failure-mode signature.
- **Tests (TDD)**: N/A — documentation. Checkpoint: `git diff Documentation/OperatorGuide/05_incident_runbook.md` — only the CON-5 entry changed; structure matches sibling entries.

#### Task 2.5 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Task 1.1, Task 1.2, Task 1.3, Task 1.4, Task 2.1, Task 2.2, Task 2.2b, Task 2.3, Task 2.4
- **Description**:
  - Spawn an agent to sweep the project for any remaining documentation references to the old "silent failure" / "empty results on pipeline error" symptom (READMEs, ADRs, `Documentation/UserManual/`, `Documentation/quick_start.md`, `contributing.md`, `CLAUDE.md`, the `roadmap.md`). For each affected file, update the content to reflect the shipped behavior. The agent must NOT modify unrelated content.
  - Verify every acceptance criterion below before marking this task complete. Run `uv run pytest` once and confirm the default-run `--cov-fail-under=85` gate is satisfied.
- **Releasable**: after this task, the feature is fully verified and all project documentation reflects the delivered behavior.
- **Acceptance criteria** (must all pass):
  - `archon_search/server/routes_search.py` inner exception block matches the §Architecture snippet — bare `raise`, telemetry enqueue wrapped in its own try/except, ERROR log with `extra={"event_type": "search_pipeline_failure"}`.
  - The 503 meta-lookup path at `routes_search.py:67-71` is functionally unchanged from the pre-change version.
  - `uv run pytest tests/server/test_routes_search.py -v` — all tests pass, including the unchanged line-386 503 test.
  - `uv run pytest tests/server/test_routes_search_telemetry.py -v` — all ten tests pass.
  - `uv run pytest tests/server/test_telemetry_e2e.py -v` — the two tests extended in Task 1.4 pass with the new `/search` REST error blocks.
  - `uv run pytest` — full default suite passes with `--cov-fail-under=85` still satisfied.
  - `POST /search` against a deliberately broken pipeline (manual or scripted) returns HTTP 500 with body `{"detail": "Internal Server Error"}`.
  - `/telemetry/entries` shows exactly one new entry per failed `/search` call with `endpoint="search"`, `status="internal_error"`, `error_kind="other"`, `latency_ms > 0`.
  - Server log on the failure path contains exactly one ERROR record from logger `archon.search` carrying attribute `event_type="search_pipeline_failure"`.
  - `BREAKING.md` contains the new entry from Task 2.1.
  - `Documentation/Architecture/140_error_handling_strategy.md` no longer asserts silent failure for `/search`.
  - `Documentation/Architecture/600_api_reference_or_public_interface.md` documents `/search` returning HTTP 500 on pipeline failure (line ~53 updated).
  - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` shows CON-5 as resolved per file convention.
  - `Documentation/OperatorGuide/05_incident_runbook.md` CON-5 entry describes the new HTTP-500 / telemetry / log-attribute signature.
  - No `TelemetryEntry.from_error(...)` call site anywhere in the project accepts a `query` parameter (structural no-raw-query invariant preserved).
  - MCP `search` / `search_with_context` tools and their tests are unchanged.
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

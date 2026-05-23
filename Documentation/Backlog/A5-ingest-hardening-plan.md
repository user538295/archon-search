# A5 — Ingest Hardening (Path Safety + SQL Builder Defense-in-Depth)
> **Sequencing**: Ships LAST in the sequence (A1→A2→A3→A4→A5). Depends on A2 for the shared SQL-quoting helper (`_sql_quote_str` in `archon_search/store_filters.py`); A5b reuses it rather than re-implementing quoting.

**Purpose**: Decompose `Documentation/Backlog/a5-ingest-hardening-brief.md` into implementable tasks. Two independent half-features (A5a path safety, A5b SQL builder cleanup) ship as two independent PRs against `main`.
**Audience**: archon-search contributors implementing A5 and reviewers of the resulting PRs.
**Status**: Draft

---

## Background

Two robustness gaps surfaced in the Phase A roadmap (`Documentation/Backlog/03_world_class_roadmap.md` line 53):

1. **A5a — Path safety.** HTTP and MCP ingest endpoints (`POST /collections`, `POST /jobs/ingest`, MCP `ingest_file`, MCP `ingest_directory`) accept arbitrary path strings with no validation. A request whose `path` contains `..` segments is followed silently — indexing files the user did not intend.
2. **A5b — SQL builder defense-in-depth.** `archon_search/store.py` builds five `where()` / `delete()` / `count_rows()` clauses via f-strings interpolating identifiers (`name`, `namespace`, `doc_id`, constructed `chunk_id`). All five interpolated values are already regex-gated upstream (`_COLLECTION_RE`, `_validate_namespace`, `_DOC_ID_RE`), so this is **not a correctness bug today**. The risk is structural: a future contributor relaxing one of those regexes silently re-enables SQL injection because the safety lives at the call site, not at the SQL boundary.

Full design, threat-model framing, alternatives considered, and the rationale for the narrowed A5a scope (no symlink check; that defers to a future `allowed_dirs` feature) live in the brief. This plan is the implementation decomposition only.

The roadmap text references forward IDs `VAL-1` and `RP-5`; per the brief, those IDs are **not** created as debt-register entries — roadmap checkmarks + this plan provide sufficient traceability, and the roadmap text is amended in Task 3.2.

## Goal

After both PRs ship:
- HTTP `POST /collections` and `POST /jobs/ingest`, and MCP `ingest_file` / `ingest_directory`, reject paths containing `..` segments, empty/whitespace-only strings, NUL bytes, and non-absolute paths, returning `HTTPException(400, detail="path is unsafe: <reason>")` (HTTP) or `McpErrorResponse(error="path is unsafe: <LLM-readable phrase>", code="path_unsafe")` (MCP). Legitimate deep absolute paths with spaces/unicode still ingest unchanged. (C1-I-DA1-5: prefix is `"path is unsafe:"` across both transports; the brief's literal `"path unsafe:"` wording is superseded here.)
- `archon_search/store.py` contains zero f-string-wrapped `.where(...)` / `.delete(...)` / `.count_rows(...)` calls. A CI guard prevents regression. Each remediated site documents the upstream regex gate inline.
- `BREAKING.md` records the MCP behaviour change; the roadmap entry for A5 is checked.

---

## Scope

### In Scope
- A new module `archon_search/_path_safety.py` (top-level package — broader reusability across `server/` and any future callers, no circular-import risk) exporting `validate_ingest_path(raw: str) -> Path` and `PathUnsafeError(reason: str)`.
- Wiring `validate_ingest_path` into the four entry points (`POST /collections`, `POST /jobs/ingest`, MCP `ingest_file`, MCP `ingest_directory`), each translating `PathUnsafeError` to the appropriate envelope.
- OpenAPI `responses=` map additions on the two HTTP routes (`400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"}`).
- Private helpers at the top of `archon_search/store.py` exposing `_where_eq(col: str, value: str) -> str` and `_where_in(col: str, values: Iterable[str]) -> str`. **Depends on A2: reuses `_sql_quote_str` from `archon_search/store_filters.py`; do not re-implement quoting (no local `_quote_literal`).** Used IF LanceDB native binds are unavailable in the pinned version (Task 2.1 probes). (C1-I-DA2-6: helper location committed — not a separate module.)
- Replacement of all five f-string sites in `store.py` (lines 291, 380, 538, 541, 633 at time of brief — Task 2.3 re-greps before editing).
- A pytest-level CI guard (`tests/test_no_fstring_sql.py`) that fails if `archon_search/store.py` contains any of `\.where\(\s*f["']`, `\.delete\(\s*f["']`, `\.count_rows\(\s*f["']`.
- Inline regex-gate comments at each remediated site.
- `BREAKING.md` Changelog entry under `### [next release]` noting MCP behaviour change.
- Amendment to `Documentation/Backlog/03_world_class_roadmap.md` A5 entry: drop forward IDs `VAL-1` / `RP-5`, check A5a / A5b / A5c boxes.
- **A5c — Synchronous `StoreBusyError` propagation on `POST /ingest`**: close the A1 deferral so that an ingest submitted while a reindex holds the per-collection lock returns HTTP 503 with `Retry-After: 30` and `{"error": "store_busy", ...}` directly, rather than 202 followed by a failed-job lookup. A5 is the natural home — A5a already touches the same handler for path validation.

### Out of Scope
- **Symlink-escape detection on ingest paths.** Deferred to the future `allowed_dirs` feature; A5a explicitly narrows the roadmap's "symlink-escape" wording.
- **`allowed_dirs` config knob** — own roadmap item.
- **CLI `archon-search ingest`** — local trusted user; consistent with existing threat model.
- **File-size / batch-size / rate-limit / glob-depth caps** — orthogonal hardening items.
- **Encoding/MIME validation** — current `errors="replace"` is intentional.
- **Debt-register entries (`VAL-*`, `RP-*`, `SEC-*`)** — born-resolved entries are noise.
- **Tightening MCP tool return annotations** (`dict[str, Any]` → typed union) — separate cleanup.
- **CI guard scoped beyond `store.py`** — preventing new-file SQL injection requires a broader linting policy outside this plan.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 3.3 — Final verification & documentation update].

---

## What does NOT change

- The upstream regex gates `_COLLECTION_RE`, `_validate_namespace` / `_NAMESPACE_RE`, `_DOC_ID_RE` — they remain the primary security boundary. Existing tests (e.g. `tests/test_store.py::test_store_delete_document_injection_safe`) stay green unchanged.
- `pipeline.py:208` and `sync.py:458` symlink-skip during filesystem walks.
- `SearchRequest` / `SearchResponse` / other Pydantic schemas not on the ingest path.
- The `Bearer` auth requirement — a 400 `path_unsafe` fires only after auth; 401 takes precedence.
- The MCP tool function signatures (`-> dict[str, Any]`).
- The `--cov-fail-under=85` gate.
- The CLI ingest surface.
- LanceDB schema, FTS, embedder, reranker, router.

---

## Known limitations / accepted trade-offs

- **A5a does not block absolute-path footguns.** `POST /ingest {"path":"/etc/passwd"}` still passes the validator. Closing that gap requires the `allowed_dirs` feature (out of scope).
- **A5a does not check symlinks.** Without an `allowed_dirs` root, there is no defensible boundary against which "symlink escape" can be defined. The roadmap's "symlink-escape" wording is honestly narrowed. The validator inspects only the **raw** `Path.parts`; the returned `resolve()`d path may follow a symlink to a location outside what the caller appears to have requested. (C1-I-DA2-1)
- **CI guard meta-test scope (C1-I-DA3-7).** `test_guard_detects_injected_violation` confirms the guard's regex still matches an obvious f-string SQL site, but it cannot catch all forms of regex weakening (e.g. subtle character-class swaps that remain truthy on the test fixture but miss real code).
- **A5b is defense-in-depth, not a bug fix.** The regex gates make injection unreachable today; A5b moves the safety to the SQL boundary so a future regex relaxation cannot reintroduce the vulnerability.
- **The CI guard is scoped to `store.py`.** A future SQL-using module added elsewhere is not covered.
- **MCP behaviour change.** Existing MCP clients passing `..`-containing paths previously succeeded silently; they now receive `McpErrorResponse(code="path_unsafe")`. Recorded in `BREAKING.md` under the existing `### [next release]` heading.
- **`_where_eq` / `_where_in` return raw SQL fragments**, not `where()` builder objects. Callers concatenate with literal `" AND "` for compound predicates. No f-string ever wraps a SQL method call.
- **Born-resolved debt entries dropped.** Roadmap checkmarks + commit history are the traceability.

---

## Architecture

### A5a — Path validation

**New module**: `archon_search/_path_safety.py` (top-level package). Underscore-prefixed (private). Lives at the top level (not under `server/`) for broader reusability — both `server/` and any future caller can import it without circular-import risk. The module imports nothing project-local. (C1-I-DA2-5)

```python
class PathUnsafeError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

def validate_ingest_path(raw: str) -> Path:
    """Validate and return the resolved absolute Path. Raises PathUnsafeError on rejection.

    Rejects:
      - empty or whitespace-only input
      - NUL byte in input
      - any element of Path(raw).parts equal to ".."
      - non-absolute path (after expanduser())

    On accept returns Path(raw).expanduser().resolve(strict=False).
    Does NOT pre-check existence — non-existent paths pass through to downstream "not found".
    Validation operates on the **raw** `Path.parts`; the returned value is `resolve()`d and
    may point elsewhere via symlinks. Symlink-resolution is intentionally NOT validated
    (deferred to a future `allowed_dirs` feature). See brief Core Flow §4e. (C1-I-DA2-1)
    """
```

> **Advisory (C1-I-DA2-9)**: Route handlers SHOULD log the validator's input `raw` string alongside the returned resolved `Path` so error messages reference what the user submitted. No code-level change to the validator; advisory only.

**Wiring at each entry point.** A wrapper translates `PathUnsafeError` to the appropriate envelope.

- HTTP (`server/routes_collections.py`, `server/routes_jobs.py`): a Pydantic `field_validator` on the request model's `path` field calls `validate_ingest_path` and lets `PathUnsafeError` propagate, OR a small helper at the top of the route handler catches and raises `HTTPException(400, detail=f"path is unsafe: {e.reason}")`. Plan-maker default: route-handler catch — keeps the Pydantic model honest about types (str input, str output) and centralises the LLM-readable phrasing alongside the rest of the handler. The route's `responses=` map gains `400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"}` (additive to existing entries).
- MCP (`server/mcp.py`): each tool catches `PathUnsafeError` and returns `McpErrorResponse(error=f"path is unsafe: {e.reason} — use an absolute path without '..' traversal", code="path_unsafe")` — LLM-readable prose.

> **Error prefix (C1-I-DA1-5)**: The HTTP `detail` and MCP `error` strings both use the prefix `"path is unsafe: <phrase>"`. The brief's literal wording `"path unsafe: ..."` is superseded by this plan's `"path is unsafe: ..."` on this specific point — the brief is intent, the plan is contract.

All callers MUST use the validator's returned `Path` for downstream ingest, not re-resolve from `raw` (to keep validated and used values identical). This is enforced by sentinel-path tests (see Tasks 1.2 – 1.5; C1-I-DA3-2).

### A5b — SQL helpers

**Probe (Task 2.1)** decides whether LanceDB's pinned `lancedb` version exposes parameterised `where()` / `delete()`. If yes, use native binds and skip the helpers. If no:

**New helpers** (private section at the top of `archon_search/store.py` — location committed per C1-I-DA2-6). **Depends on A2: import and reuse `_sql_quote_str` from `archon_search/store_filters.py`; do not re-implement quoting.**

```python
from archon_search.store_filters import _sql_quote_str

def _where_eq(col: str, value: str) -> str:
    """Return e.g. "name = 'O''Brien'". Callers compose with literal ' AND '."""
    return f"{col} = {_sql_quote_str(value)}"

def _where_in(col: str, values: Iterable[str]) -> str:
    """Return e.g. "chunk_id IN ('a', 'b')". Empty values yield "1=0" (always-false)."""
    items = ", ".join(_sql_quote_str(v) for v in values)
    return f"{col} IN ({items})" if items else "1=0"
```

(The `f"…{col}…"` here is inside the helper, not at a SQL-method call site — the CI guard pattern is `\.where\(\s*f["']` etc., so these helper internals are fine.)

> **`1=0` branch note (C1-I-DA1-2)**: The `_where_in` empty-iterable branch returning `"1=0"` is purely defensive against future callers that might bypass the existing early-return in `store.py` (`if not target_ids: return []`). It is unreachable through the current call sites and is covered only at the helper-unit-test level (Task 2.2), not via integration tests.

**Call-site replacements** (Task 2.3):

| Line (pre-edit) | Before | After |
|---|---|---|
| 291 | `await table.delete(f"name = '{name}' AND namespace = '{namespace}'")` | `await table.delete(_where_eq("name", name) + " AND " + _where_eq("namespace", namespace))` |
| 380 | `await table.delete(f"name = '{meta.name}'")` | `await table.delete(_where_eq("name", meta.name))` |
| 538 | `await table.count_rows(f"doc_id = '{doc_id}'")` | `await table.count_rows(_where_eq("doc_id", doc_id))` |
| 541 | `await table.delete(f"doc_id = '{doc_id}'")` | `await table.delete(_where_eq("doc_id", doc_id))` |
| 630-633 | `id_list = ", ".join(f"'{cid}'" for cid in target_ids); .where(f"chunk_id IN ({id_list})")` | `.where(_where_in("chunk_id", target_ids))` |

Each remediated site gets an inline comment: `# <value> validated upstream by <regex>; quoting helper is defense-in-depth`.

### CI guard

**New test file** `tests/test_no_fstring_sql.py`. Reads `archon_search/store.py` as text, asserts none of the three regex patterns match. Runs in the default-tier pytest invocation.

### A5c — Sync `StoreBusyError` propagation on `POST /ingest`

**Problem**: A1 introduced `SearchStore.ingest_chunks` with a 30s per-collection lock acquisition timeout that raises `StoreBusyError` when a reindex holds the lock. The store-layer contract is fully tested. But `POST /ingest` is fire-and-forget (returns 202 immediately, then runs the pipeline in a background asyncio task), so `StoreBusyError` surfaces in *job state* (`GET /jobs/{id}` → `FAILED`) rather than as a synchronous response — the plan's acceptance criterion ("503 with `Retry-After: 30`") is not met from the HTTP surface.

**Design**: pre-acquire the per-collection lock **inside the request handler** with the existing 30s timeout. On timeout, return HTTP 503 with `Retry-After: 30` and `{"error": "store_busy", ...}`. On success, hand the held lock to the background task via a small wrapper that releases it when the task completes. Avoid the TOCTOU window of release-then-reacquire (otherwise a reindex slipping in between hands the client a misleading 202).

```python
# routes_jobs.py (sketch)
search_store = getattr(request.app.state, "search_store", None)
collection = body.collection
if search_store is not None:
    lock = search_store._lock_for(collection)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "store_busy", "detail": "reindex in progress; retry after Retry-After seconds"},
            status_code=503,
            headers={"Retry-After": str(math.ceil(INGEST_LOCK_TIMEOUT_S))},
        )
    held_lock = lock
else:
    held_lock = None

# wrap background task to release the lock when done
task = asyncio.create_task(_default_ingest_task_with_lock(
    job.job_id, store, body, namespace=ns, pipeline_fn=pipeline_fn, held_lock=held_lock,
))
```

`SearchStore.ingest_chunks` must learn to **skip lock acquisition if the caller already holds it**. Simplest signal: a new `_locked_by_caller: bool = False` keyword (private, single-underscore — not part of the public API). The background-task wrapper passes `_locked_by_caller=True`; all other callers (CLI, sync, watcher, eval) keep the existing self-locking behavior.

**Out of scope for A5c**:
- `POST /collections` (which also schedules an ingest job): same fix applies, mirror the change. Listed in the task breakdown.
- The MCP `ingest_*` tools: those call `pipeline.ingest_file` directly (no async-job wrapper), so the existing store-level `StoreBusyError` already surfaces synchronously to the MCP client. Verify via test; no code change expected.
- Configurable timeout. Out of scope — A1's `INGEST_LOCK_TIMEOUT_S=30.0` is still hardcoded.

**Known limitation**: If the background task is cancelled (e.g. `DELETE /jobs/{id}` while pending), the wrapper must still release the lock. Use a `try/finally` on the wrapper, not `async with`.

---

## Task breakdown

> **TDD commit discipline (C1-I-DA3-1)**: Per the user's global CLAUDE.md and the brief's tests-first mandate, each implementation task produces **TWO commits**: (1) a test commit landing the failing tests first (red), then (2) an implementation commit that makes them pass (green). Red→green→refactor happens at the commit level, not just in the working tree. Acceptable variant: the test commit may mark tests as `xfail` or `skip` with an explanatory reason, and the implementation commit flips them to passing (removing the `xfail`/`skip` marker). Bundling tests and implementation into a single commit is NOT permitted for this plan. Pure documentation and verification tasks (no test+code pair) remain single-commit.

### Phase 1 — A5a Path safety
> **Releasable**: when Task 1.7 lands. The whole phase ships as **one PR** ("A5a: ingest path safety"). Every prior task is internal-only until the four entry points and the OpenAPI/BREAKING.md updates are all in place.

#### Task 1.1 — `PathUnsafeError` and `validate_ingest_path` in `_path_safety.py`
- [x] **File**: `archon_search/_path_safety.py` (new, top-level)
- **Depends on**: nothing
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: add `tests/test_path_safety.py` with all unit tests below. The module under test does not yet exist, so tests fail on import — acceptable red state; if preferred, mark them `@pytest.mark.xfail(reason="implementation pending in next commit", strict=True)` so CI is green at this commit.
  - **Commit 2 (implementation, green)**: add `archon_search/_path_safety.py` with the symbols below; remove any `xfail` markers from commit 1; all tests pass.
  - `class PathUnsafeError(ValueError)` with `__init__(self, reason: str) -> None`; stores `reason` as instance attribute. Subclassing `ValueError` keeps it catchable by callers that already catch `ValueError` and lets Pydantic surface it as a validation error if used in a `field_validator`.
  - `def validate_ingest_path(raw: str) -> Path` — exact behaviour per brief Core Flow §4 and the Architecture section above. Rejection reasons returned in `PathUnsafeError.reason` are short codes: `"empty"`, `"whitespace_only"`, `"nul_byte"`, `"contains_dotdot"`, `"not_absolute"`. The LLM-readable phrasing is applied at the MCP wrapper layer, not here — keeps this module pure.
  - Order of checks: emptiness/whitespace first, then NUL byte, then `expanduser()`, then absoluteness, then `Path.parts` for `..`. Reason: cheapest rejections first; absoluteness is checked **after** `expanduser()` so `~/foo` is accepted.
  - Returns `Path(raw).expanduser().resolve(strict=False)` on accept.
- **Releasable**: after this task, `from archon_search._path_safety import validate_ingest_path, PathUnsafeError` works and is fully unit-tested. No caller uses it yet.
- **Tests (TDD)** — `tests/test_path_safety.py` (new):
  - Unit: `test_accepts_absolute_path` — `/tmp/foo.md` returns the resolved Path.
  - Unit: `test_accepts_path_with_spaces_and_unicode` — `/home/user/My Documents/notés.md` returns the resolved Path (legitimate-path regression).
  - Unit: `test_accepts_tilde_expansion` — `~/foo` returns a value where `result.is_absolute() and result != Path("~/foo")`. Do NOT assert the resolved path is "under the home directory" — CI sandbox HOMEs vary. (C1-I-DA3-9)
  - Unit: `test_accepts_dotdot_substring_in_dirname` — `/data/..backup/x.md` accepted (`..backup` is a real-dir-name substring, not a `..` part).
  - Unit: `test_accepts_nonexistent_absolute_path` — `/no/such/file.md` passes the validator; existence is downstream's concern.
  - Unit: `test_accepts_trailing_slash` — `/tmp/foo/` accepted.
  - Unit: `test_rejects_dotdot_standalone` — raises `PathUnsafeError(reason="not_absolute")` for `..` (`..` is relative, so the not_absolute check fires before the dotdot-parts check).
  - Unit: `test_rejects_dotdot_mid_path` — `/foo/../bar` rejected.
  - Unit: `test_rejects_relative_dotdot_path` — `../foo` rejected with `reason == "not_absolute"` (relative-path rejection fires before dotdot-parts rejection per the check order documented above). (C1-I-DA1-1)
  - Unit: `test_rejects_empty_string` — `""` → `reason="empty"`.
  - Unit: `test_rejects_whitespace_only` — `"   "` → `reason="whitespace_only"`.
  - Unit: `test_rejects_nul_byte` — `"/tmp/foo\x00.md"` → `reason="nul_byte"`.
  - Unit: `test_rejects_relative_path` — `"./foo"`, `"foo/bar"`, `"."` all → `reason="not_absolute"`.
  - Unit: `test_path_unsafe_error_is_value_error` — `isinstance(PathUnsafeError("x"), ValueError)`.
  - Unit: `test_path_unsafe_error_carries_reason` — `e.reason == "contains_dotdot"` after raise/except round-trip.
  - Checkpoint: `uv run pytest tests/test_path_safety.py -v`

#### Task 1.2 — Wire validator into `POST /collections`
- [x] **File**: `archon_search/server/routes_collections.py`
- **Depends on**: Task 1.1
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: append the integration tests below to `tests/server/test_routes_collections.py`. Tests fail (wiring absent); if CI must stay green, mark them `@pytest.mark.xfail(strict=True, reason="wiring pending")`.
  - **Commit 2 (implementation, green)**: apply the wiring changes; remove any `xfail` markers; all tests pass.
  - Wiring:
    - At the top of the `add_collection` handler (currently around line 122 where `Path(body.path).expanduser().resolve()` runs), replace the manual `Path(...).expanduser().resolve()` with `validate_ingest_path(body.path)`.
    - Wrap in `try / except PathUnsafeError as e: raise HTTPException(status_code=400, detail=f"path is unsafe: {e.reason}")`.
    - The handler converts the validator's returned `Path` via `str(...)` before storing/using downstream (see `routes_collections.py:122` — `resolved = str(Path(...).resolve())`). The sentinel test asserts the downstream receives `str(Path("/sentinel/value"))` (i.e. `"/sentinel/value"`), not the `Path` object. Implementer must verify the handler does `str(validate_ingest_path(body.path))` — preserving existing downstream `str` typing — not change the downstream type. (C1-I-DA3-2)
    - **Scope note (C3-I-3)**: Other `Path(...).expanduser().resolve()` call sites in `routes_collections.py` operate on config-sourced paths (not user request bodies) and are intentionally excluded from A5a.
    - Add `400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"}` to the route's `responses=` map (additive; preserve any existing entries).
    - Import: `from archon_search._path_safety import validate_ingest_path, PathUnsafeError`.
- **Releasable**: after this task, `POST /collections` rejects unsafe paths with 400 + conforming `ErrorDetail`. The OpenAPI schema reflects the new error.
- **Tests (TDD)** — `tests/server/test_routes_collections.py` (existing file, append):
  - Integration: `test_add_collection_rejects_dotdot_path` — `client.post("/collections", json={"name": "x", "path": "/foo/../bar"}, headers=auth_headers)` asserts 400; `response.json()["detail"].startswith("path is unsafe:")`. (C1-I-DA3-3, C1-I-DA1-5)
  - Integration: `test_add_collection_uses_validator_returned_path` — monkeypatch `archon_search.server._path_safety.validate_ingest_path` to return a sentinel `Path("/sentinel/value")`; invoke the route with any valid-shaped `path` string; assert the downstream call receives exactly `str(Path("/sentinel/value"))` (the handler `str(...)`-converts before storing/using), proving the handler does not re-resolve from `body.path`. (C1-I-DA3-2)
  - Integration: `test_add_collection_rejects_relative_path` — `"./foo"` → 400.
  - Integration: `test_add_collection_rejects_empty_path` — `""` → 400 with `"empty"` in detail.
  - Integration: `test_add_collection_unauth_takes_precedence` — `..` path WITHOUT auth headers → 401 (auth fires before path validation).
  - Integration: `test_add_collection_accepts_legitimate_absolute_path` (regression) — `/tmp/<uuid>` valid path still returns the existing 200/202 response shape.
  - Integration: `test_add_collection_openapi_lists_400_response` — `client.get("/openapi.json")` shows `400` under `/collections` POST `responses`, with `$ref` resolving to `ErrorDetail`.
  - Uses the existing `auth_headers` fixture from `conftest.py`.
  - Checkpoint: `uv run pytest tests/server/test_routes_collections.py -v -k "path_safety or unauth_takes or openapi_lists or legitimate_absolute"`

#### Task 1.3 — Wire validator into `POST /jobs/ingest`
- [x] **File**: `archon_search/server/routes_jobs.py`
- **Depends on**: Task 1.1
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: append the integration tests below to `tests/server/test_routes_jobs.py`; mark them `@pytest.mark.xfail(strict=True, reason="wiring pending")` if CI must stay green at this commit.
  - **Commit 2 (implementation, green)**: apply the wiring changes; remove any `xfail` markers; all tests pass.
  - Wiring (C3-I-2): The `POST /jobs/ingest` handler does not call `expanduser().resolve()` locally — it forwards `body` to `_default_ingest_task`. Wire validation as follows: when `body.path is not None`, call `validated = validate_ingest_path(body.path)` in a `try/except PathUnsafeError -> HTTPException(400, detail=f'path is unsafe: {e.reason}')` block, then assign `body.path = str(validated)` (or pass `validated` separately if the downstream signature accepts it) BEFORE dispatch. `IngestRequest.path` is `str | None`; only validate when non-None (no-path ingest jobs continue to work).
    - The sentinel test asserts `_default_ingest_task` (or the equivalent dispatch path) receives `str(Path('/sentinel/value'))` as `body.path`. (C1-I-DA3-2)
    - Add `400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"}` to the route's `responses=` map (additive).
- **Releasable**: after this task, `POST /jobs/ingest` rejects unsafe paths with 400.
- **Tests (TDD)** — `tests/server/test_routes_jobs.py` (existing, append):
  - Integration: `test_ingest_rejects_dotdot_path` — 400, `detail` starts with `"path is unsafe:"`. (C1-I-DA1-5)
  - Integration: `test_ingest_uses_validator_returned_path` — monkeypatch `validate_ingest_path` to return sentinel `Path("/sentinel/value")`; assert the created job receives exactly `str(Path("/sentinel/value"))` (the handler `str(...)`-converts before storing). (C1-I-DA3-2)
  - Integration: `test_ingest_rejects_nul_byte_path` — `"/tmp/x\x00.md"` → 400, `"nul_byte"` in detail.
  - Integration: `test_ingest_accepts_null_path` — `{"path": null}` still accepted (existing behaviour preserved).
  - Integration: `test_ingest_accepts_legitimate_absolute_path` (regression) — valid path still 202.
  - Integration: `test_ingest_openapi_lists_400_response` — OpenAPI schema includes the new 400.
  - Checkpoint: `uv run pytest tests/server/test_routes_jobs.py -v -k "rejects_dotdot or nul_byte or null_path or legitimate_absolute or openapi_lists"`

#### Task 1.4 — Wire validator into MCP `ingest_file`
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.1
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: append the tests below to `tests/server/test_mcp.py`; mark them `@pytest.mark.xfail(strict=True, reason="wiring pending")` if CI must stay green at this commit.
  - **Commit 2 (implementation, green)**: apply the wiring changes; remove any `xfail` markers; all tests pass.
  - Wiring:
    - Call `validate_ingest_path(path)` **BEFORE entering the existing `try / except Exception` block** in the `ingest_file` tool body, wrapped in a dedicated `try / except PathUnsafeError as e: return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")`. This ensures `PathUnsafeError` is handled by its own clause and is NOT swallowed by the generic `except Exception` that returns `code="internal_error"`. (C1-I-DA1-4) The implementer must verify `mcp.py`'s existing try-structure to confirm the placement.
    - Add a small private helper at module level: `def _path_unsafe_message(reason: str) -> str` mapping each of the **five** reason codes (`empty`, `whitespace_only`, `nul_byte`, `contains_dotdot`, `not_absolute`) to an LLM-readable phrase, e.g. `"contains_dotdot" → "path is unsafe: input contains '..' segment — use an absolute path without traversal"`. Five mappings total — one per reason code.
    - `mcp.py` currently passes `Path(path)` to `pipeline.ingest_file` / `pipeline.ingest_directory`. Use the validator's returned `Path` directly (no `str(...)` conversion). The sentinel test asserts the downstream receives the `Path("/sentinel/value")` object. Do NOT re-resolve from `path`. (C1-I-DA3-2, C3-I-1)
    - Do NOT tighten the `-> dict[str, Any]` return annotation.
- **Releasable**: after this task, MCP `ingest_file` rejects unsafe paths with `McpErrorResponse(code="path_unsafe")` carrying LLM-readable prose.
- **Tests (TDD)** — `tests/server/test_mcp.py` (existing, append):
  - Unit: `test_mcp_path_unsafe_message_maps_all_reasons` — every one of the five reason codes returns a non-empty LLM-readable phrase.
  - Integration: `test_mcp_ingest_file_rejects_dotdot` — `code == "path_unsafe"`, error contains the `contains_dotdot` phrase. (C1-I-DA3-4)
  - Integration: `test_mcp_ingest_file_rejects_relative` — `not_absolute` phrasing. (C1-I-DA3-4)
  - Integration: `test_mcp_ingest_file_rejects_empty` — `empty` phrasing. (C1-I-DA3-4)
  - Integration: `test_mcp_ingest_file_rejects_whitespace_only` — `whitespace_only` phrasing. (C1-I-DA3-4)
  - Integration: `test_mcp_ingest_file_rejects_nul_byte` — `nul_byte` phrasing. (C1-I-DA3-4)
  - Integration: `test_mcp_ingest_file_uses_validator_returned_path` — monkeypatch `validate_ingest_path` to return sentinel `Path("/sentinel/value")`; assert the underlying `pipeline.ingest_file` call receives the `Path("/sentinel/value")` object directly (mcp.py passes `Path(path)` downstream — use the validator's `Path` with no `str(...)` conversion). (C1-I-DA3-2, C3-I-1)
  - Integration: `test_mcp_ingest_file_accepts_legitimate_absolute_path` (regression) — valid path returns the existing success shape.
  - Note (C1-I-DA3-4): Task 1.4 carries the full five-reason-code coverage so Task 1.5 can be narrower.
  - Checkpoint: `uv run pytest tests/server/test_mcp.py -v -k "ingest_file and (dotdot or relative or empty or whitespace_only or nul_byte or legitimate or unsafe_message or sentinel)"`

#### Task 1.5 — Wire validator into MCP `ingest_directory`
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.4 (shares `_path_unsafe_message` helper)
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: append the tests below to `tests/server/test_mcp.py`; mark them `@pytest.mark.xfail(strict=True, reason="wiring pending")` if CI must stay green at this commit.
  - **Commit 2 (implementation, green)**: apply the wiring changes; remove any `xfail` markers; all tests pass.
  - Wiring:
    - Same pattern as Task 1.4 applied to `ingest_directory(path: str, ...)`. The `validate_ingest_path` call MUST be placed **BEFORE the existing `try / except Exception` block** in the tool body, with a dedicated `except PathUnsafeError` clause, so `PathUnsafeError` is not swallowed by the generic `except Exception` clause that returns `code="internal_error"`. (C1-I-DA1-4) Implementer verifies `mcp.py`'s existing try-structure.
    - Reuses `_path_unsafe_message` from Task 1.4.
    - `mcp.py` currently passes `Path(path)` to `pipeline.ingest_directory`. Use the validator's returned `Path` directly (no `str(...)` conversion). The sentinel test asserts the downstream receives the `Path("/sentinel/value")` object. Do NOT re-resolve from `path`. (C3-I-1)
- **Releasable**: after this task, MCP `ingest_directory` rejects unsafe paths with `McpErrorResponse(code="path_unsafe")`.
- **Tests** — `tests/server/test_mcp.py` (existing, append). Task 1.4 already carries full five-reason-code coverage; Task 1.5 is narrower (C1-I-DA3-4):
  - Integration: `test_mcp_ingest_directory_rejects_dotdot` — same shape as `ingest_file`.
  - Integration: `test_mcp_ingest_directory_reuses_path_unsafe_message` — pick a non-dotdot reason (e.g. `nul_byte`) and assert the returned `error` string equals `_path_unsafe_message("nul_byte")`. Proves the helper is invoked from `ingest_directory`, not a hardcoded copy. (DA2-C2-I-7)
  - Integration: `test_mcp_ingest_directory_uses_validator_returned_path` — sentinel path test (C1-I-DA3-2).
  - Integration: `test_mcp_ingest_directory_accepts_legitimate_absolute_path` (regression).
  - Checkpoint: `uv run pytest tests/server/test_mcp.py -v -k "ingest_directory and (dotdot or sentinel or legitimate or reuses_path_unsafe_message)"`

#### Task 1.6 — `BREAKING.md` Changelog entry for MCP behaviour change
- [ ] **File**: `BREAKING.md`
- **Depends on**: Task 1.4, Task 1.5
- **Description**:
  - Append under the existing `### [next release]` heading (match the file's existing format — no new top-level section).
  - Bullet wording: `- MCP \`ingest_file\` and \`ingest_directory\` previously accepted paths containing \`..\` segments, empty strings, NUL bytes, and non-absolute paths, and silently followed/resolved them. They now return \`McpErrorResponse(error=..., code="path_unsafe")\`. HTTP \`POST /collections\` and \`POST /jobs/ingest\` gain a new \`400\` response for the same input classes (additive — was previously not in the OpenAPI schema).`
  - No code change beyond this file.
- **Releasable**: after this task, A5a is documented and the PR can merge.
- **Tests (TDD)**: N/A — documentation task.
- **Checkpoint**: `grep -F "path_unsafe" BREAKING.md` returns the new entry.

#### Task 1.7 — A5a final wiring sanity
- [ ] **File**: N/A (verification task)
- **Depends on**: Tasks 1.1 – 1.6
- **Description (C1-I-DA2-4)**:
  - Run the full default-tier pytest suite locally to confirm nothing outside A5a regressed.
  - All roadmap (`03_world_class_roadmap.md`) edits — including the A5a checkmark and the `VAL-1` removal — are consolidated into Task 3.2. This task makes no documentation changes.
- **Releasable**: A5a PR ready to open against `main`.
- **Tests (TDD)**: N/A — verification task.
- **Checkpoint (C1-I-DA3-10)**: assert all of:
  - (a) `uv run pytest` exits 0.
  - (b) Coverage ≥ 85% via the existing `--cov-fail-under=85` addopt (the default invocation already enforces this; a failing build means the gate tripped).
  - (c) Test count ≥ pre-A5a baseline. Capture baseline via `git stash && uv run pytest --collect-only -q | tail -1 && git stash pop` BEFORE Task 1.1 implementation lands, OR check out the pre-A5a commit (`git rev-parse HEAD` at task-start time) in a worktree and run the same command. Record the number in the PR description. Task 1.7 re-runs `uv run pytest --collect-only -q | tail -1` and asserts the count is ≥ baseline. If no automated capture is feasible, downgrade the assertion to "no test files were deleted" — verified by `git diff --diff-filter=D --name-only <pre-A5a-sha>..HEAD -- tests/` returning empty. Guards against silent test deletion.

---

### Phase 2 — A5b SQL builder defense-in-depth
> **Releasable**: when Task 2.4 lands. Phase 2 ships as a **second, independent PR** ("A5b: SQL builder hardening"). Independent of Phase 1 — either PR can ship first.
>
> **PR-level coupling (C1-I-DA3-5)**: Tasks 2.3 (f-string replacements) and 2.4 (CI guard) MUST land in the SAME PR. Merging 2.3 without 2.4 leaves a regression window where a contributor could reintroduce f-string SQL without mechanical detection.
>
> **A2 sequencing canary**: A5b MUST be sequenced AFTER A2 merges — it consumes `_sql_quote_str` from `archon_search/store_filters.py`, which A2 creates. Task 2.0 below is the contract test that fails loudly if A2 renames the symbol or changes its signature before A5b code is touched.

#### Task 2.0 — Import contract assertion for `_sql_quote_str`
- [ ] **File**: `tests/test_store_filters_contract.py` (new)
- **Depends on**: A2 (`archon_search/store_filters.py` must exist and export `_sql_quote_str`)
- **Description**:
  - Lands FIRST in the A5b PR, before Task 2.1. Acts as a canary: if A2 renames `_sql_quote_str` or changes its signature, this test fails before any A5b implementation code is touched.
  - The test file does a top-level `from archon_search.store_filters import _sql_quote_str` so an A2-side rename or removal surfaces as a clear `ImportError` at collection time, naming A2 as the blocker.
  - Asserts the signature via `inspect.signature`: callable taking a single positional parameter typed `str`, return annotation `str`. (A `Protocol` is acceptable as an alternative.)
  - Asserts the quoting convention: `_sql_quote_str("foo'bar") == "'foo''bar'"` (the doubling-quote sanity check that A5b relies on).
  - If `archon_search.store_filters` does not yet exist at A5b implementation time, the test MUST fail with a clear ImportError pointing at A2 as the blocker (e.g. the test file's module-level import statement is sufficient; pytest collection will surface the failure with the missing-module name).
- **Releasable**: after this task, any A2 contract drift (rename, signature change, quoting-convention change) breaks CI loudly before A5b implementation begins.
- **Tests (TDD)** — `tests/test_store_filters_contract.py` itself is the test:
  - Unit: `test_sql_quote_str_importable` — implicit via module-level import; failure is an `ImportError` at collection naming A2 as the blocker.
  - Unit: `test_sql_quote_str_signature` — `inspect.signature(_sql_quote_str)` has exactly one parameter, that parameter's annotation is `str` (or `inspect.Parameter.empty` only if the rest of the signature matches a documented variant), and the return annotation is `str`.
  - Unit: `test_sql_quote_str_doubles_single_quote` — `_sql_quote_str("foo'bar") == "'foo''bar'"`.
- **Checkpoint**: `uv run pytest tests/test_store_filters_contract.py -v`

#### Task 2.1 — Probe LanceDB native bind support
- [ ] **File**: N/A (research task, output is a one-paragraph note in the PR description and a comment at the top of the helpers module if Task 2.2 uses helpers)
- **Depends on**: nothing
- **Description**:
  - Read the pinned `lancedb` version from `pyproject.toml` and consult its docs / source for a parameterised `where()` / `delete()` API (look for `?`, `@name`, `bind`, `params=`, or DataFusion-style binds).
  - If native binds exist and are reachable via the async API archon-search uses for ALL FIVE call sites, document the call shape and proceed to Task 2.2 option A.
  - If not, proceed to Task 2.2 option B (the `_where_eq` / `_where_in` helpers).
  - **Decision rubric (C1-I-DA2-3)**: if native binds do not cleanly cover all five sites on the async API (e.g. binds exist for `where()` but not `delete()`, or are sync-only), choose option B (helpers) for ALL FIVE SITES. Do not mix approaches — partial bind coverage is more confusing than uniform helpers.
  - Output: one-paragraph finding posted in the PR description before Task 2.2 starts.
- **Releasable**: after this task, Task 2.2's mechanism is decided.
- **Tests (TDD)**: N/A — research task.
- **Checkpoint**: PR description contains the probe finding.

#### Task 2.2 — Implement SQL helpers (or skip if native binds available)
- [ ] **File**: `archon_search/store.py` (private helpers at top of file). (C1-I-DA2-6: location committed — no `_sql.py` alternative.)
- **Depends on**: Task 2.1
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: append the unit tests below to `tests/test_store.py`; tests fail because the helpers do not yet exist. Mark `@pytest.mark.xfail(strict=True, reason="helpers pending")` if CI must stay green at this commit.
  - **Commit 2 (implementation, green)**: add the helpers; remove any `xfail` markers; all tests pass.
  - **Depends on A2: reuses `_sql_quote_str` from `archon_search/store_filters.py`; do not re-implement quoting.**
  - Implementation:
    - **If Task 2.1 found native binds:** skip this task; Task 2.3 uses binds directly.
    - **Otherwise:** add two helpers `_where_eq(col: str, value: str) -> str` and `_where_in(col: str, values: Iterable[str]) -> str` near the top of `store.py` under the existing private regex constants. Both call the imported `_sql_quote_str` from `archon_search/store_filters.py` (introduced in A2) — do NOT add a local `_quote_literal`. `_where_in` with empty iterable returns `"1=0"` (always-false predicate — never matches; safe default for callers that pass empty lists; see Architecture note on the defensive `1=0` branch).
    - Helpers contain no f-strings that wrap `.where(`, `.delete(`, or `.count_rows(` calls — the CI guard pattern matches only those exact call sites, not f-string usage in general.
- **Releasable**: after this task, the helpers exist and are unit-tested in isolation. No call site uses them yet.
- **Tests (TDD)** — `tests/test_store.py` (existing, append in a new section near the regex tests):
  - (Note: `_sql_quote_str` itself is unit-tested in A2's `tests/test_store_filters.py`. A5b adds no duplicate `_quote_literal` tests; the helpers below exercise the composition layer that A5b owns.)
  - Unit: `test_where_eq_basic` — `_where_eq("name", "foo") == "name = 'foo'"`.
  - Unit: `test_where_eq_adversarial` — `_where_eq("name", "O'Brien") == "name = 'O''Brien'"` — call the helper directly, bypassing the upstream regex gate (this is the belt-and-braces test).
  - Unit: `test_where_in_basic` — `_where_in("chunk_id", ["a", "b"]) == "chunk_id IN ('a', 'b')"`.
  - Unit: `test_where_in_empty_returns_always_false` — `_where_in("chunk_id", []) == "1=0"`.
  - Unit: `test_where_in_single` — `_where_in("chunk_id", ["a"]) == "chunk_id IN ('a')"`.
  - Unit: `test_where_in_adversarial` — values containing `'` are doubled.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "where_eq or where_in"`

#### Task 2.3 — Replace the five f-string SQL sites in `store.py`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.2; Task 2.4 is a **blocking prerequisite for merging the A5b PR** (the CI guard must land in the same PR as the f-string replacements — C1-I-DA3-5).
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: append the integration tests below to `tests/test_store.py`. These are behaviour-preservation regression tests; if they pass against unchanged code, mark them `@pytest.mark.xfail(strict=False, reason="pending refactor — should remain green after f-string replacement")` and flip them to non-xfail in Commit 2. Pure-regression tasks are the one exception where the test commit can land green; the discipline is preserved by adding tests in a dedicated commit before any production code changes.
  - **Commit 2 (implementation, green)**: apply the f-string-to-helper replacements; remove any `xfail` markers; re-run and confirm green.
  - Replacement work:
  - Re-grep `archon_search/store.py` for `\.where\(\s*f["']`, `\.delete\(\s*f["']`, `\.count_rows\(\s*f["']` to catch any drift since the brief was written. The expected five sites are at (approximately) lines 291, 380, 538, 541, 633.
  - Apply the replacements exactly per the table in the Architecture section.
  - At each remediated site, add a single-line comment naming the upstream regex gate: e.g. `# name validated upstream by _COLLECTION_RE; _where_eq is defense-in-depth`. Five comments total.
  - Do NOT relax any regex gate. Do NOT change any function signature. The existing `tests/test_store.py::test_store_delete_document_injection_safe` must continue to pass without modification.
- **Releasable**: after this task, `store.py` contains zero f-string-wrapped SQL builder calls and behaviour is unchanged.
- **Tests (TDD)** — `tests/test_store.py` (existing, append; also rely on the entire existing store test suite continuing to pass):
  - Integration: `test_delete_collection_meta_removes_only_named_row` — create two collections, delete one by `(name, namespace)`, confirm the other survives. Exercises the compound-predicate site (line 291).
  - Integration: `test_update_collection_meta_replaces_existing_row` — exercises line 380 site.
  - Integration: `test_delete_document_removes_all_chunks` — exercises lines 538 & 541 sites.
  - Integration: `test_fetch_adjacent_chunks_returns_window` — exercises the IN-clause site at line 633 (this test likely already exists; if so, this task only needs to confirm it still passes).
  - (C1-I-DA1-2) The integration test for the `_where_in` empty-iterable branch is **omitted**: `store.py` has an `if not target_ids: return []` early-return that short-circuits before the helper is called, so the `"1=0"` branch is unreachable at the integration level. Helper-level unit coverage in Task 2.2 (`test_where_in_empty_returns_always_false`) is sufficient.
  - Integration: `test_a5b_end_to_end_flow_unchanged` (C1-I-DA3-8) — full happy-path regression: add a collection → ingest a small document → search returns the doc → delete the document → search returns nothing. Every step must remain green after the f-string-to-helper refactor. This is the regression check that the helpers preserve semantics across the five replaced sites.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "delete_collection_meta or update_collection_meta or delete_document or fetch_adjacent_chunks or injection_safe or a5b_end_to_end"` (all green, including the pre-existing `test_store_delete_document_injection_safe`).

#### Task 2.4 — CI guard preventing f-string SQL regressions
- [ ] **File**: `tests/test_no_fstring_sql.py` (new)
- **Depends on**: Task 2.2 (must ship in same PR as Task 2.3 — see Phase 2 header)
- **Description (TDD, C1-I-DA3-1)**:
  - **Commit 1 (tests, red)**: write the meta-test `test_guard_detects_injected_violation` against a tempfile fixture; confirm it fails (no guard regex exists yet). Mark `@pytest.mark.xfail(strict=True, reason="guard pending")` if CI must stay green at this commit.
  - **Commit 2 (implementation, green)**: write the guard's regex and assertion message in their final form; remove any `xfail` markers; all in-file tests pass.
  - Implementation:
    - One pytest test, default tier, no marker. Reads `archon_search/store.py` as text. Asserts that `re.search(r"\.where\(\s*f[\"']", source) is None`, same for `\.delete\(\s*f[\"']` and `\.count_rows\(\s*f[\"']`.
    - On failure, the test's assertion message must point at the line numbers of any matches found, so a contributor who accidentally reintroduces an f-string SQL builder sees exactly where.
    - Scope: `archon_search/store.py` only. Avoids false positives on `@router.delete(...)` in `server/routes_*.py`. (C1-I-DA2-7) If `lancedb` is ever imported outside `store.py`, expand the guard's file-glob to cover those modules. Today: `store.py` is the only `lancedb` consumer.
- **Releasable**: after this task, regression of A5b is mechanically prevented.
- **Tests (TDD)** — `tests/test_no_fstring_sql.py` itself is the test. Plus a meta-test in the same file:
  - Unit: `test_guard_detects_injected_violation` — write a tempfile containing `table.where(f"x = '{y}'")`, run the guard's regex against that tempfile content, assert the violation is detected (prevents the guard becoming a silent no-op if the pattern is weakened — e.g., `\s*` is replaced with something unmatchable). Known limitation (C1-I-DA3-7): this meta-test cannot catch all forms of regex weakening; subtle character-class swaps that still match the fixture but miss real code would slip through.
  - Unit: `test_guard_ignores_router_delete_decorator` — meta-test confirms `@router.delete("/{name}")` does NOT match the patterns.
  - Unit: `test_guard_ignores_helper_internals` — `f"{col} = {_quote_literal(value)}"` inside a helper does NOT match (it's not preceded by `.where(`, `.delete(`, or `.count_rows(`).
  - Checkpoint: `uv run pytest tests/test_no_fstring_sql.py -v`

---

### Phase 2c — A5c Sync `StoreBusyError` propagation on `POST /ingest`
> **Releasable**: when Task 2c.3 lands. Independent of A5a / A5b — can ship as its own PR or fold into A5b's PR. Closes the A1 deferral so the plan's "503 + Retry-After: 30" acceptance criterion is met from the HTTP response surface.

#### Task 2c.1 — `SearchStore.ingest_chunks` accepts `_locked_by_caller`
- [ ] **File**: `archon_search/store.py`, `tests/test_store_lock.py`
- **Depends on**: nothing (independent of A5a / A5b).
- **Description**:
  - Add private keyword-only parameter `_locked_by_caller: bool = False` to `ingest_chunks`.
  - When `True`, skip the `_lock_for(collection)` acquire/release dance entirely (the caller is responsible for the lock lifecycle). The validation, table.add, and return semantics are unchanged.
  - Document in a one-line comment that this is for the REST `/ingest` handler's pre-acquire path; no public API change.
- **Tests (TDD)** — `tests/test_store_lock.py`:
  - `test_ingest_chunks_skips_lock_when_locked_by_caller` — pre-acquire the lock via `_lock_for`, then call `ingest_chunks(..., _locked_by_caller=True)`; assert no `StoreBusyError` and the row lands.
  - `test_ingest_chunks_default_still_acquires` — pre-acquire the lock, call without the flag, assert `StoreBusyError` (existing behavior).
- **Checkpoint**: `uv run pytest tests/test_store_lock.py -v`.

#### Task 2c.2 — Pre-acquire lock in `POST /ingest` and `POST /collections`
- [ ] **File**: `archon_search/server/routes_jobs.py`, `archon_search/server/routes_collections.py`, `archon_search/server/_ingest_lock.py` (new helper module).
- **Depends on**: Task 2c.1.
- **Description**:
  - New helper `acquire_collection_lock_or_503(store, collection_name) -> asyncio.Lock | JSONResponse | None`:
    - Returns the acquired lock on success.
    - Returns a 503 `JSONResponse` (with `Retry-After: str(math.ceil(INGEST_LOCK_TIMEOUT_S))` and body `{"error": "store_busy", "detail": "reindex in progress; retry after Retry-After seconds"}`) on timeout.
    - Returns `None` when `store` is unavailable (test/stub paths) — handler treats as best-effort and proceeds.
  - `POST /ingest` (`routes_jobs.ingest`): call the helper; if it returns a `JSONResponse`, return it directly; if a lock, pass it to a new `_default_ingest_task_with_lock` wrapper that releases the lock in a `try/finally` around the existing task body. Pass `_locked_by_caller=True` down to `ingest_chunks` via a small pipeline-fn signature update (or via `body` for the stub pipeline). For the in-tree default pipeline, the wrapper must propagate the flag so the background task does not redundantly try to re-acquire.
  - `POST /collections.add_collection`: mirror the same pre-acquire (new collection name is derived deterministically from path — call `path_to_collection_name` first, then acquire that name's lock).
- **Tests (TDD)** — `tests/test_routes_ingest_503.py`:
  - `test_post_ingest_returns_503_when_lock_held` — patch `_lock_for(...)` to return a pre-acquired lock, POST `/ingest`, assert 503 + `Retry-After: 30` + JSON body matches contract.
  - `test_post_ingest_succeeds_when_lock_free` — happy path, asserts 202 and that the background task ran without re-acquiring.
  - `test_post_collections_returns_503_when_lock_held` — same for the `/collections` endpoint.
  - `test_post_ingest_releases_lock_after_background_task` — after a successful 202, assert the lock is released (subsequent ingest into same collection succeeds immediately).
  - `test_post_ingest_releases_lock_on_task_cancellation` — cancel the background task mid-run via `DELETE /jobs/{id}`, assert the lock is released.
- **Checkpoint**: `uv run pytest tests/test_routes_ingest_503.py -v`.

#### Task 2c.3 — MCP path verification + docs
- [ ] **File**: `tests/server/test_mcp_ingest_503.py` (new), `BREAKING.md`, A1 plan acceptance note.
- **Depends on**: Task 2c.2.
- **Description**:
  - Verify MCP `ingest_file` / `ingest_directory` surface `StoreBusyError` as `McpErrorResponse(code="store_busy")` synchronously (no code change expected — they call `pipeline.ingest_file` directly, which calls `ingest_chunks`; the existing `except Exception` in mcp.py wraps it into the error envelope). Add an explicit test that pre-acquires the per-collection lock and asserts the MCP tool returns the error envelope rather than a success dict.
  - **`BREAKING.md`**: add a paragraph under the existing A1 entry noting that the synchronous 503 surface is now in place (folded into A5). Reference A5's release.
  - **A1 plan** (`Documentation/Backlog/A1-metadata-schema-v1-plan.md`): in Task 8.3's acceptance criteria, flip the deferred "POST /ingest returns 503" item from ⚠️ to ✅ with a backreference to A5c.
- **Checkpoint**: `uv run pytest tests/server/test_mcp_ingest_503.py -v`, then full suite.

---

### Phase 3 — Verification & documentation
> **Releasable**: when Task 3.3 completes. (C1-I-DA2-8) Phase 3 ships either as a third small PR (docs sweep) after both feature PRs merge, OR is folded into whichever feature PR ships second — at the author's discretion. Either way, neither feature PR's correctness depends on Phase 3.
>
> Phase 3 must ship within one week of both feature PRs merging — it is not indefinitely deferrable. The roadmap checkmarks landing in Task 3.2 are part of A5's done-definition. (DA2-C2-I-3)

#### Task 3.1 — Update `Documentation/Architecture/150_security_and_privacy_architecture.md`
- [ ] **File**: `Documentation/Architecture/150_security_and_privacy_architecture.md`
- **Depends on**: Tasks 1.7, 2.4 (whichever ships second)
- **Description**:
  - Add a short subsection noting that ingest paths are validated for `..` segments, empty/whitespace/NUL inputs, and absoluteness on the four HTTP/MCP entry points. Explicitly note what is NOT validated: symlinks, absolute-path scope (deferred to `allowed_dirs`).
  - Add a short subsection noting that SQL builders in `store.py` are layered: regex gates at the validator level remain the primary security boundary, helpers are defense-in-depth, the CI guard prevents regression.
- **Releasable**: after this task, the security posture doc matches the implementation.
- **Tests (TDD)**: N/A — documentation task.
- **Checkpoint**: doc references `_path_safety.py` and `_where_eq` / `_where_in` (or native binds if Task 2.1 chose that path).

#### Task 3.2 — Roadmap consolidation (VAL-1 / RP-5 cleanup + A5a / A5b / A5c checkmarks)
- [ ] **File**: `Documentation/Backlog/03_world_class_roadmap.md`
- **Depends on**: Task 1.7, Task 2.4, Task 2c.3
- **Description (C1-I-DA2-4)**: This is the **sole** task that edits the roadmap. Tasks 1.7, 2.4, 2c.3 explicitly do NOT touch the roadmap, eliminating merge-conflict risk when the feature PRs land independently.
  - Remove or strike-through both `VAL-1` and `RP-5` forward IDs from the A5 line.
  - Check the A5a, A5b, and A5c boxes (if the roadmap uses sub-checkboxes) or add sub-bullets `- [x] A5a (PR #<n>)` / `- [x] A5b (PR #<n>)` / `- [x] A5c (PR #<n>)`.
  - If the brief was added as a Backlog reference, leave the link.
- **Releasable**: roadmap is internally consistent — no orphaned forward IDs.
- **Tests (TDD)**: N/A — documentation task.
- **Checkpoint**: `grep -E "VAL-1|RP-5" Documentation/Backlog/03_world_class_roadmap.md` returns nothing AND the A5a / A5b checkmarks are visible.

#### Task 3.3 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, Architecture docs, UserManual, OperatorGuide, CHANGELOG, contributing.md, CLAUDE.md, brief, roadmap) and update every file whose content is affected by A5. Specifically check:
    - `Documentation/Architecture/100_system_architecture_overview.md` / `110_component_catalog_and_layer_breakdown.md` for any ingest-pipeline diagram that should reference `_path_safety.py`.
    - `Documentation/Architecture/140_error_handling_strategy.md` for the new 400 `path is unsafe:` detail string and the `path_unsafe` MCP code.
    - `Documentation/Architecture/520_api_design_and_contracts.md` for the new 400 response on `/collections` and `/jobs/ingest`.
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` for the new 400 in REST and the new error code in MCP.
    - `Documentation/UserManual/` for any ingest examples that show `..`-containing or relative paths (must be updated to absolutes).
    - The CLAUDE.md project-level file for any documented invariants that should mention the f-string SQL guard.
    - Verify `Documentation/Backlog/a5-ingest-hardening-brief.md` Goal section uses the MCP `error` prefix `path is unsafe:` (already aligned with the plan; no change expected). (C3-I-4)
  - Agent must NOT update docs that are unrelated.
  - Verify every acceptance criterion below is met before marking this task complete.
- **Releasable**: after this task, A5 is fully shipped — both PRs merged, all docs aligned, roadmap clean.
- **Acceptance criteria** (must all pass):
  - `validate_ingest_path` exists in `archon_search/_path_safety.py` (top-level), raises `PathUnsafeError(reason)`, accepts absolute paths with spaces/unicode/tilde-expansion/non-existence/`..backup`-substring/trailing-slash, rejects `..`-as-part/empty/whitespace/NUL/relative.
  - `POST /collections` returns 400 with `{"detail": "path is unsafe: <reason>"}` for unsafe paths, 401 takes precedence when unauth, 200/202 unchanged for valid paths.
  - `POST /jobs/ingest` returns 400 with `{"detail": "path is unsafe: <reason>"}` for unsafe paths; `path: null` ingest still works.
  - MCP `ingest_file` and `ingest_directory` return `McpErrorResponse(error=<LLM-readable phrase>, code="path_unsafe")` for unsafe paths; valid paths return the existing success shape.
  - `GET /openapi.json` lists `400: ErrorDetail` under both `POST /collections` and `POST /jobs/ingest`.
  - `archon_search/store.py` contains no f-string-wrapped `.where(`, `.delete(`, or `.count_rows(` call sites — verified by `tests/test_no_fstring_sql.py`.
  - `tests/test_store.py::test_store_delete_document_injection_safe` and every other pre-existing store test passes unchanged.
  - The upstream regex gates `_COLLECTION_RE`, `_validate_namespace` / `_NAMESPACE_RE`, `_DOC_ID_RE` are unchanged.
  - `tests/test_no_fstring_sql.py::test_guard_detects_injected_violation` passes — the guard regex is not a no-op.
  - `BREAKING.md` contains a `### [next release]` Changelog entry describing the MCP behaviour change.
  - `Documentation/Backlog/03_world_class_roadmap.md` no longer references `VAL-1` or `RP-5`; A5 (or its A5a/A5b/A5c sub-items) is checked.
  - **A5c**: `POST /ingest` and `POST /collections` return HTTP 503 with `Retry-After: 30` and `{"error": "store_busy", ...}` when the target collection's per-collection lock is held by an active reindex; ingest into a *different* collection succeeds normally. The MCP `ingest_*` tools surface `StoreBusyError` synchronously as `McpErrorResponse(code="store_busy")`. A1 plan Task 8.3's acceptance item for this is now ✅ (backreference to A5c).
  - `Documentation/Architecture/150_security_and_privacy_architecture.md` reflects the new validation layer and the CI guard.
  - Full default-tier `uv run pytest` exits 0 with coverage ≥ 85%.
  - No MCP tool return annotation was tightened as part of this work (cleanup is deliberately deferred).
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

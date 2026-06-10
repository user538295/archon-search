# C12 — Switch xdist to `--dist=loadgroup` with session-scoped store
**Purpose**: Eliminate the `--dist=loadfile` bottleneck that pins 118 ingest tests to one worker, cutting local full-suite wall time from ~6.5 min to ≤90s.
**Audience**: Developers iterating locally who run the full suite after making changes to pipeline or store code.
**Status**: To Do

---

## Background

After C10 and C11, the full suite still takes ~6.5 min locally. Root cause: `--dist=loadfile` assigns all tests in a file to one worker. `test_pipeline/test_pipeline_ingest.py` (~118 tests, ~327s serial runtime) still pins one worker regardless of core count. The fix is three config/code changes: switch to `--dist=loadgroup` (ungrouped tests distribute individually across workers; grouped tests pin to one worker), promote `connected_store` to session scope (one store per worker, not one per module), and group all `sys.modules["fastmcp"]`-mutating tests on one worker via `xdist_group` to preserve the isolation that `--dist=loadfile` previously guaranteed implicitly. Note: `--dist=load` does NOT respect `xdist_group` markers — only `--dist=loadgroup` does.

## Goal

`uv run pytest --no-cov` completes in ≤90s on a ≥4-core machine. Full suite with coverage (`uv run pytest`) passes the 85% gate. All tests pass in serial mode (`-n0`). CI is unaffected (already uses `-n0`).

---

## Scope

### In Scope
- Change `connected_store` fixture scope from `module` to `session` in `tests/conftest.py`
- Change `--dist=loadfile` to `--dist=loadgroup` in `pyproject.toml` `addopts`
- Add `pytestmark = pytest.mark.xdist_group("mcp")` to all 16 files that mutate `sys.modules["fastmcp"]` at import time (see Task 1.3)
- Run the full suite 5+ times with the new config and confirm zero failures
- Measure and record new wall time
- Update all documentation references to `--dist=loadfile`

### Out of Scope
- Changing any test assertions on collection counts
- Splitting additional test files
- CI workflow changes (CI already uses `-n0`)
- `asyncio_default_fixture_loop_scope` change (deferred from C10)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 3.1 — Final verification & documentation update].

---

## What does NOT change
- Test logic, assertions, mocks, or fixture parameters
- CI workflows (`-n0` remains explicit in CI)
- Other module-scoped fixtures (`matrix_store`, `backfill_store`, `db`) — they create private `SearchStore` instances, not `connected_store`
- `test_mcp_schemas.py` — imports pure Pydantic schemas, unaffected by grouping

---

## Known limitations / accepted trade-offs
- On a 2-core machine the wall time is ~200s; the ≤90s target requires ≥4 cores (matches C10/C11 assumptions)
- `--dist=loadgroup` distributes ungrouped tests individually on a demand basis (same as `--dist=load` for ungrouped tests); grouped tests pin to one worker. No duration heuristic; if one worker gets unlucky with slow tests the spread may be uneven in a given run, but the 5-run verification gate catches systematic imbalance
- Under session scope, one LanceDB table is created per test that uses `connected_store` (collections are not cleaned up between tests within a session); `_collection_locks` accumulates one entry per collection created. This is bounded by the test count per worker and is not expected to cause failures, but is an accepted trade-off. The collection-name audit in Task 1.1 confirms all names used with `connected_store` are either UUID-based (from the `col_name` fixture) or file-unique hardcoded names (e.g. in `test_store.py`), so cross-test collisions do not occur.
- Grouping all 16 `sys.modules["fastmcp"]`-mutating files under `xdist_group("mcp")` with `--dist=loadgroup` pins ~247 tests to one worker. (`--dist=load` must NOT be used — it ignores `xdist_group` markers entirely.) If those tests run slowly, the mcp-group worker may become the new bottleneck. The 5-run gate and wall-time check in Task 2.1 will catch this; if median wall time exceeds 90s due to the mcp group, the fix is to either split the group using finer-grained group names or refactor the `sys.modules` stub mechanism (out of scope for C12).
- Under `-n0` (serial mode), session scope means the entire run shares one `SearchStore` instance instead of one per module. This is a larger blast radius than module scope but is safe as long as collection names remain unique per test (confirmed by the audit above).
- The order-independence criterion (3 `pytest-randomly` seeds) requires `pytest-randomly` to be installed (`uv add --dev pytest-randomly`). It is not in the current dev dependencies. If not installed at verification time, document it as deferred.

---

## Architecture

Three targeted changes to existing files:

1. **`tests/conftest.py`** — `connected_store` fixture: `scope="module"` → `scope="session"`. Fixture body unchanged; `tmp_path_factory` already works at session scope. Result: one `SearchStore` per xdist worker for the entire session.

2. **`pyproject.toml`** — `addopts`: replace `--dist=loadfile` with `--dist=loadgroup`. All other flags unchanged. Important: `--dist=load` does NOT respect `xdist_group` markers; `--dist=loadgroup` is required.

3. **16 test files** — add module-level `pytestmark = pytest.mark.xdist_group("mcp")` to every file that mutates `sys.modules["fastmcp"]` at import time. Under `--dist=loadgroup`, ungrouped tests distribute individually across workers while grouped tests pin to one worker. Since `sys.modules` is process-global, a worker that ran a stub-installing test would have corrupted state when a file that expects the real `fastmcp` is later imported on the same worker. Grouping all 16 files onto one worker prevents cross-file contamination in both directions.

Files requiring the marker (derived from `grep -rl 'sys\.modules.*fastmcp' tests/` excluding `live_eval`-marked files):
- `tests/test_mcp.py`
- `tests/test_integration_rag_fusion.py`
- `tests/contract/test_mcp_search_response_shape.py`
- `tests/server/test_mcp_ingest_503.py`
- `tests/server/test_mcp_auth.py`
- `tests/server/test_mcp_ingest_stage_timings.py`
- `tests/server/test_mcp_search.py`
- `tests/server/test_mcp_explain.py`
- `tests/server/test_mcp_search_with_context.py`
- `tests/server/test_mcp_embedder_dispatch.py`
- `tests/server/test_mcp_search_stage_timings.py`
- `tests/server/test_mcp_error_responses.py`
- `tests/server/test_routes_explain.py`
- `tests/server/test_telemetry_e2e.py`
- `tests/server/test_mcp_update_collection.py`
- `tests/server/test_mcp_telemetry.py`

**Corrective addition during Task 2.1 measurement**: `pytestmark = pytest.mark.xdist_group("install")` was also added to `tests/test_install.py`, `tests/test_install_run.py`, and `tests/test_install_lock.py`. These three files compete on the real `~/.archon-search/.install.lock` filesystem lock and caused 4 failures during the first parallel measurement run. This was not identified in the original architecture because the install lock is a real filesystem resource (not a `sys.modules` mutation), but the same grouping fix applies.

No new modules, config keys, or API surface changes.

---

## Task breakdown

### Phase 1 — Config and fixture changes
> **Releasable**: after all three tasks in this phase are complete and the suite passes 5× consecutively.

#### Task 1.1 — Promote `connected_store` to session scope
- [x] **File**: `tests/conftest.py`
- **Depends on**: nothing
- **Description**:
  - Change `scope="module"` to `scope="session"` on the `connected_store` fixture (line 45)
  - Docstring update: replace "One shared SearchStore per test module" with "One shared SearchStore per xdist worker session"
  - Comment block above the fixture (lines 40–42): update to reflect session scope and the `--dist=loadgroup` context
  - No change to fixture body — `tmp_path_factory` is already session-safe; `asyncio.run(store.connect())` is called once per worker
- **Releasable**: after this task, `connected_store` yields one store per worker session under any dist mode
- **Tests (TDD)** — `tests/conftest.py` (fixture definition — no separate test file needed; verified via full suite run in Task 1.3):
  - Checkpoint: `uv run pytest --no-cov --co -q 2>&1 | grep connected_store` — confirms fixture is collected without error
  - Collection name audit: run `grep -rn '"[a-z][a-z-]*"' tests/ --include="*.py" | grep -i "col\|collect"` and confirm all collection names used with `connected_store` are UUID-based (from the `col_name` fixture), not hardcoded literals that would collide across tests sharing a session-scoped store

#### Task 1.2 — Switch `addopts` to `--dist=loadgroup`
- [x] **File**: `pyproject.toml`
- **Depends on**: nothing
- **Description**:
  - In `[tool.pytest.ini_options]` `addopts` (line 81), replace `--dist=loadfile` with `--dist=loadgroup`
  - All other flags remain unchanged
  - Note: `--dist=load` does NOT honor `xdist_group` markers; `--dist=loadgroup` is required for grouping to work
- **Releasable**: after this task, `uv run pytest` distributes ungrouped tests individually across workers, and grouped tests pin to one worker
- **Tests (TDD)** — verified via full suite run in Task 1.3:
  - Checkpoint: `grep 'dist=' pyproject.toml` — confirms `--dist=loadgroup` is present and `--dist=loadfile` is absent

#### Task 1.3 — Add `xdist_group("mcp")` marker to all 16 affected files
- [x] **Files**: all 16 listed in the Architecture section above
- **Depends on**: Task 1.1, Task 1.2
- **Description**:
  - Add `pytestmark = pytest.mark.xdist_group("mcp")` as a module-level assignment immediately after the module docstring / existing imports (before the first class or test definition) in each of the 16 files
  - All 16 files mutate `sys.modules["fastmcp"]` at import time; any of them landing on the same worker as another without the group would corrupt `sys.modules` state for subsequent tests on that worker
  - No other changes to any of these files
- **Releasable**: after this task, all MCP-touching tests are co-located on one worker and `--dist=loadgroup` correctly enforces the grouping
- **Tests (TDD)**:
  - Confirm coverage: the count of files carrying the marker must equal the count of files with `sys.modules["fastmcp"]` mutations in the default test run: `diff <(grep -rl 'sys\.modules.*fastmcp' tests/ --include='*.py' | grep -v 'eval/live' | sort) <(grep -rl 'xdist_group("mcp")' tests/ --include='*.py' | sort)` — must produce no output
  - Full suite (5× consecutive, continues even on failure): `pass=0; fail=0; for i in $(seq 1 5); do if uv run pytest --no-cov --tb=short; then pass=$((pass+1)); echo "RUN $i: PASS"; else fail=$((fail+1)); echo "RUN $i: FAIL"; fi; done; echo "Result: $pass/5 passed, $fail failed"`
  - Confirm zero failures across all 5 runs

---

### Phase 2 — Wall-time measurement
> **Releasable**: after Task 2.1, the measured wall time is documented and ready for the final verification task.

#### Task 2.1 — Measure and record wall time
- [x] **File**: `Documentation/Backlog/C12-dist-load-session-store-brief.md`
- **Depends on**: Task 1.3 (all three changes applied, suite passing)
- **Description**:
  - Run `time uv run pytest --no-cov` three times and record the wall times
  - Append a "Measured results" section to the brief with: run timestamps, per-run wall times, and the median
  - If median exceeds 90s on the local machine (assuming ≥4 cores), flag in the brief before proceeding to the final task
- **Releasable**: after this task, wall-time evidence is recorded for the final verification
- **Tests (TDD)**: N/A — measurement task
- **Checkpoint**: read the updated brief and confirm the "Measured results" section is present

---

### Phase 3 — Verification & Documentation

#### Task 3.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Task 2.1 (all prior tasks complete)
- **Description**:
  - Spawn an agent to update every documentation file that references `--dist=loadfile` or describes `connected_store` scope, including at minimum:
    - `CLAUDE.md`
    - `Documentation/quick_start.md`
    - `Documentation/Architecture/200_testing_strategy.md`
    - `Documentation/Architecture/500_development_workflows_and_conventions.md`
  - The agent must search for any other references (`grep -r 'loadfile\|module.*connected_store\|connected_store.*module' Documentation/ CLAUDE.md`) and update those as well — **exclude `Documentation/Completed/`** (historical records; do not rewrite past decisions)
  - Verify all acceptance criteria below are met before marking this task complete
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - [ ] `grep 'dist=' pyproject.toml` shows `--dist=loadgroup`, not `--dist=loadfile`
  - [ ] `grep 'scope=' tests/conftest.py | grep connected_store` shows `scope="session"`
  - [ ] Marker coverage equals mutation coverage: `diff <(grep -rl 'sys\.modules.*fastmcp' tests/ --include='*.py' | grep -v 'eval/live' | sort) <(grep -rl 'xdist_group("mcp")' tests/ --include='*.py' | sort)` — must produce no output
  - [ ] Full suite passes 5 consecutive runs with zero failures (loop continues on failure, do not use `&&`): `pass=0; fail=0; for i in $(seq 1 5); do if uv run pytest --no-cov --tb=short; then pass=$((pass+1)); echo "RUN $i: PASS"; else fail=$((fail+1)); echo "RUN $i: FAIL"; fi; done; echo "Result: $pass/5 passed, $fail failed"`
  - [ ] Order-independence: run with 3 different random seeds — all must pass: `uv run pytest --no-cov -p randomly --randomly-seed=12345`, `--randomly-seed=99999`, `--randomly-seed=42`. If `pytest-randomly` is not installed, note this criterion as deferred and treat the 5-run gate as the weaker substitute.
  - [ ] Median wall time of 3 `time uv run pytest --no-cov` runs is ≤90s on a ≥4-core machine
  - [ ] `uv run pytest` (with coverage) passes the 85% gate
  - [ ] `uv run pytest -n0` passes (serial mode)
  - [ ] No active documentation file contains `--dist=loadfile` (verify with `grep -r 'dist=loadfile' . --include='*.md' --include='*.toml' --include='*.py' | grep -v 'Documentation/Completed/'`)
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

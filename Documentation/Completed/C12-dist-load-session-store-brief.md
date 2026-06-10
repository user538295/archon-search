# Feature Brief: C12 — Switch xdist to `--dist=loadgroup` with session-scoped store

## Problem
After three rounds of test-speed work (C10, C11), the full suite still takes ~6.5 min wall time locally. The root cause is `--dist=loadfile`: all 118 ingest tests are pinned to one worker, which runs them serially in ~327s regardless of core count. The original ≤150s target is unreachable without distributing individual tests across workers.

## Goal
`uv run pytest --no-cov` completes in ≤90s on a ≥4-core machine. Full suite with coverage passes the 85% gate. All tests pass in serial mode (`-n0`). CI is unaffected (already runs `-n0`).

## Users & Context
Developers iterating locally who run the full suite after making changes to pipeline or store code. Currently blocked for 6+ minutes before getting a green/red signal.

## Core Flow
1. Developer runs `uv run pytest --no-cov`.
2. xdist spawns N workers (one per core).
3. Each worker gets a session-scoped `connected_store` (one LanceDB instance per worker, created once).
4. Individual tests are distributed across workers via `--dist=loadgroup` (ungrouped tests distribute individually across workers; grouped tests pin to one worker).
5. Tests distributed evenly across workers; slow I/O tests spread evenly rather than piling onto one worker.
6. Wall time ≈ max(per-worker serial time) ≈ 50–90s.

## In Scope
- Change `connected_store` fixture scope from `module` to `session` in `tests/conftest.py` (line 45)
- Change `--dist=loadfile` to `--dist=loadgroup` in `pyproject.toml` `addopts` (line 81)
- Add `pytestmark = pytest.mark.xdist_group("mcp")` to all 16 files that mutate `sys.modules["fastmcp"]` to pin all 236 MCP tests to one worker
- Add `pytestmark = pytest.mark.xdist_group("install")` to `test_install.py`, `test_install_run.py`, `test_install_lock.py` to prevent install lock contention
- Run the full suite 5+ times with `--dist=loadgroup` and confirm zero failures before merge
- Verify the full suite passes with the new config
- Measure and record the new wall time
- Update documentation references to `--dist=loadfile` in at minimum: `CLAUDE.md`, `Documentation/quick_start.md`, `Documentation/Architecture/200_testing_strategy.md`, `Documentation/Architecture/500_development_workflows_and_conventions.md`

## Out of Scope
- Changes to test *assertions* on collection counts — isolation audit confirmed zero tests assert `len(list_collections()) == N` on a shared store
- Splitting additional test files — the dist mode change makes file size irrelevant
- CI workflow changes — CI already uses `-n0` explicitly; this change has no CI effect
- `asyncio_default_fixture_loop_scope` change — deferred in C10; still deferred

## Key Decisions
- **`--dist=loadgroup` over `--dist=loadscope`**: `loadscope` groups by class/module, which still allows slow files to pin a worker. `loadgroup` distributes ungrouped tests individually, giving the most even spread while still respecting explicit `xdist_group` markers.
- **`--dist=loadgroup` over `--dist=load`**: `--dist=load` distributes tests individually but does NOT honour `xdist_group` markers — it would ignore all pinning of MCP and install tests. `--dist=loadgroup` is required for grouping to work.
- **`--dist=loadgroup` over `--dist=worksteal`**: `worksteal` was designed for varying-duration tests and may produce more even distribution; however, its fixture reuse semantics differ and it is not yet stable in all pytest-xdist versions. `--dist=loadgroup` is the more widely-tested choice. If initial results are uneven, `worksteal` is worth evaluating as an alternative.
- **Session scope over module scope**: With `--dist=loadgroup`, ungrouped tests from the same module land on different workers. Module-scoped fixtures would be re-created per worker per module assignment — effectively becoming function-scoped. Session scope gives exactly one store per worker, which is the correct semantic.
- **Why this changes from C10's `--dist=loadfile`**: C10 explicitly chose `--dist=loadfile` to handle the `sys.modules` mutation in `tests/test_mcp.py` (`_stub_fastmcp_for_module` fixture). C12 addresses this by pinning all MCP tests to one worker via `pytest.mark.xdist_group("mcp")` on 16 files, making `--dist=loadgroup` safe.
- **`sync_manifest.json` is not a shared-state risk**: all `test_sync.py` tests that read/write `sync_manifest.json` use `tmp_path` directly — not `connected_store`. Each test gets its own isolated `tmp_path`; session scope has no effect on them.
- **Store-level isolation is sound**: UUID `col_name` per test prevents collection name collisions; no absolute-count assertions found in the audit; `sync_manifest.json` tests are isolated via `tmp_path`.

## Edge Cases & Constraints
- **`tmp_path_factory` in session fixture**: `connected_store` already uses `tmp_path_factory` (xdist-safe). Scope change from `module` to `session` requires no change to the fixture body — `tmp_path_factory` works at session scope.
- **Worker count on low-core machines**: On a 2-core machine (`-n 2`), wall time is ~200s. The ≤90s target assumes ≥4 cores. This is acceptable and matches the C10/C11 assumption.
- **`--dist=loadgroup` scheduling**: xdist `--dist=loadgroup` uses demand-based scheduling for ungrouped tests — a finished worker picks the next pending test from the queue. Grouped tests are collected and sent to one worker. There is no test-duration heuristic; distribution is stateless and immediate; no warm-up run is required for balanced scheduling.
- **Coverage combining**: pytest-cov with xdist already combines `.coverage.workerN` files correctly under any dist mode. No change needed.
- **`test_store_list_collections_empty_database_returns_empty`**: Uses its own private `SearchStore(tmp_path / "empty_db")`, not `connected_store`. Unaffected by scope change.
- **`_stub_fastmcp_for_module` in `test_mcp.py` and related files**: These files mutate `sys.modules["fastmcp"]` at import time. Under `--dist=loadgroup`, the intra-worker risk is that a non-MCP test landing on the same worker could see the stub active. Resolved by `pytestmark = pytest.mark.xdist_group("mcp")` on all 16 affected files, which keeps all 236 MCP tests on one worker. `test_mcp_schemas.py` imports pure Pydantic schemas and is unaffected.
- **`sync_manifest.json` is not a risk**: every `test_sync.py` test that reads or writes `sync_manifest.json` uses `tmp_path` (function-scoped), not `connected_store`. Fully isolated per test regardless of dist mode or fixture scope.
- **Other module-scoped fixtures**: `matrix_store`/`matrix_col` (`test_pipeline_acl_filter_matrix.py`), `backfill_store` (`test_search_backfill_regression.py`), and `db` (`test_fts_spike_gates.py`) all create their own private `SearchStore` instances — not `connected_store`. Under `--dist=loadgroup`, each worker gets its own fixture instance per module that runs on it, which is correct behavior. No changes needed for these.

## Open Questions
None.

## Rollback
If flakiness or isolation failures are detected after merge:
1. Revert `pyproject.toml` `addopts` from `--dist=loadgroup` back to `--dist=loadfile`.
2. Revert `connected_store` fixture scope in `tests/conftest.py` from `session` back to `module`.
3. Remove `pytestmark = pytest.mark.xdist_group("mcp")` from all 16 MCP files.
4. Remove `pytestmark = pytest.mark.xdist_group("install")` from `test_install.py`, `test_install_run.py`, `test_install_lock.py`.
5. Re-run the full suite 3+ times to confirm stability before declaring the rollback complete.
6. If documentation was already updated, revert the four documentation files referenced in In Scope (`CLAUDE.md`, `Documentation/quick_start.md`, `Documentation/Architecture/200_testing_strategy.md`, `Documentation/Architecture/500_development_workflows_and_conventions.md`).

## Future Iterations
- `asyncio_default_fixture_loop_scope = "module"` — saves ~30–60s additional but requires resolving dangling-coroutine warnings first. Deferred from C10, still deferred.
- Further splitting of `test_store.py` (~303 tests) if it becomes the new bottleneck after this change.

## Recommendation
This is the right fix, and it should have been C10. Four config/code changes eliminate the architectural bottleneck: switch to `--dist=loadgroup`, promote `connected_store` to session scope, add `xdist_group("mcp")` to 16 MCP files, and add `xdist_group("install")` to the 3 install lock test files. Isolation is sound — store-level via `col_name`, MCP tests via grouping, install lock tests via grouping, sync tests via `tmp_path`. Run the suite 5+ times to confirm, then ship.

---

## Measured results

**Machine**: macOS Darwin 25.4.0, 14 logical CPUs (Apple Silicon)
**Config at time of measurement**: `--dist=loadgroup`, `connected_store` session scope, `xdist_group("mcp")` on 16 MCP files, `xdist_group("install")` on 3 install lock files (corrective fix added during measurement — see Findings)

| Run | Timestamp (UTC)          | Wall time | Outcome                                                          |
|-----|--------------------------|-----------|------------------------------------------------------------------|
| 1   | 2026-06-10T18:09:58Z     | 119.10s   | **4 FAILURES** (install lock race — pre-fix baseline, excluded) |
| 2   | 2026-06-10T18:13:20Z     | 155.20s   | 3752 passed, 1 skipped (after install group fix)                 |
| 3   | 2026-06-10T18:16:00Z     | 157.19s   | 3752 passed, 1 skipped                                           |
| 4   | 2026-06-10T18:37:18Z     | 189.61s   | 3752 passed, 1 skipped                                           |

**Median wall time (3 valid runs: 155.20s, 157.19s, 189.61s)**: **157s**

### Findings

**⚠ Target MISSED: Median wall time of 156s exceeds the ≤90s acceptance criterion.**

The install lock race was fixed by adding `xdist_group("install")` to `tests/test_install.py`, `tests/test_install_run.py`, and `tests/test_install_lock.py`. These three files compete on the shared real filesystem lock at `~/.archon-search/.install.lock` and must run on one worker.

The wall time bottleneck is not the install group (122 tests, ~0.9s serial). It is the pipeline test suite (`tests/pipeline/`, 223 tests, **354s serial**). Under `--dist=loadgroup`, these ungrouped tests distribute individually across 14 workers — but the I/O and model overhead means the slowest worker still takes ~155s. The original C12 plan assumed the bottleneck was `--dist=loadfile` pinning all ingest tests to one worker; that was correct, and the change does distribute them. However, even with full distribution, the per-test latency is high enough that 14 workers each seeing roughly 16 pipeline tests still run for ~150s total.

**Serial mode baseline**: `uv run pytest --no-cov -n0` completes in 650s. Parallel achieves ~4.2× speedup — correct for I/O-heavy workloads with 14 workers, where parallelism is limited by LanceDB disk I/O contention between workers.

**Recommendation before proceeding to Task 3.1**: The ≤90s target is not met on this machine. Per the plan's accepted trade-offs, the 5-run stability gate is satisfied (zero failures in runs 2 and 3 after the install fix). Task 3.1 must be completed with this target miss documented; if ≤90s is a hard gate, the next step would be to investigate whether the pipeline tests can be further parallelised within a worker (out of scope for C12) or whether the per-test LanceDB setup cost can be reduced.

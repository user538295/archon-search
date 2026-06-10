# Feature Brief: C10 — Test Suite Speed Optimization

## Problem
The default `uv run pytest` run takes ~600s (estimated; 30s lockout test + large serial overhead) on every invocation, making local iteration slow and CI feedback expensive. The two root causes are a single test that deliberately waits 30s without using the existing monkeypatch pattern, and the entire suite running serially despite being trivially parallelizable.

## Goal
Default `uv run pytest` completes in under 200s locally (8-core machine). The `--cov-fail-under=85` gate and all existing markers continue to work. Release CI can opt out of parallelism with `-n0`.

## Acceptance Criteria
- `time uv run pytest --no-cov` completes in ≤200s on an 8-core developer machine (measured, not estimated).
- `uv run pytest` (with coverage) passes `--cov-fail-under=85`.
- All markers (`eval`, `integration`, `live`, `benchmark`) continue to function correctly.
- Serial run (`-n0`) remains available and produces identical results.

## Users & Context
Developers running the full suite locally while iterating on a feature or debugging a regression. The 10-min wall time is the friction point — it discourages running the full suite and encourages skipping coverage checks with `--no-cov`.

## Core Flow
1. Developer runs `uv run pytest` (no extra flags).
2. pytest-xdist distributes tests across all available CPU cores using `--dist=loadfile` (same file → same worker, preserving `module`-scoped `connected_store` fixtures).
3. pytest-cov collects coverage per worker and combines automatically before applying `--cov-fail-under=85`.
4. Total wall time is expected to drop from ~600s to ~150–200s on 8 cores (verified by the baseline measurement step in In Scope).
5. For debugging, developer passes `-n0` to force serial execution with live stdout.
6. Release CI pipeline passes `-n0` explicitly for a deterministic serial run.

## In Scope
- **Prerequisite — fix `three_page_pdf` session fixture**: convert from writing to `tests/fixtures/pdfs/three_page.pdf` (fixed repo path) to `tmp_path_factory` (worker-isolated temp path). Confirmed by exhaustive 30-agent audit of all 201 files: `test_fixtures.py` and `test_parser.py` both use this fixture and land on different workers with `--dist=loadfile`, racing to write the same file. Fix: 4-line change to `tests/conftest.py`; no test assertion requires a specific path. Note: `session`-scoped fixtures are instantiated once **per worker** under xdist (each worker runs its own pytest session), so converting to `tmp_path_factory` gives each worker an isolated PDF — correct behavior. `tmp_path_factory` is supported in session-scoped fixtures in pytest ≥ 7.0.
- Add `pytest-xdist` to `[dependency-groups] dev` in `pyproject.toml`.
- Add `-n auto --dist=loadfile` to `addopts` in `[tool.pytest.ini_options]`.
- Fix `test_update_description_timeout_skips_write` — add `monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", 1.0)` (the pattern already used in 2 other tests in the same file (`test_store.py`) and 4 more across the broader test suite), reducing that test from 30s to ~1s. Also add `monkeypatch` to the function signature: `async def test_update_description_timeout_skips_write(tmp_path, caplog, monkeypatch)`.
- Update `CLAUDE.md` Common commands: document `-n0` override, note release CI usage, and add `-n0 -x` / `-n0 -s` debugging notes.
- Update `Documentation/Architecture/200_testing_strategy.md`: add xdist to the default run tier description, note coverage combining behavior, and document the `-n0` serial escape hatch.
- Update `Documentation/Architecture/500_development_workflows_and_conventions.md`: note `-n0` for debugging and the parallel-by-default behavior.
- Measure baseline with `time uv run pytest --no-cov -n0` before and after to verify the actual speedup.
- Update CI release workflow AND PR workflow to pass `-n0` in their reconstructed flag strings (defensive, since both already blank `addopts` via `-o addopts=`). This prevents future xdist activation if the addopts override is ever removed.

## Out of Scope
- `asyncio_default_fixture_loop_scope = "module"` — the dangling-coroutine `RuntimeWarning`s in the current suite make this risky; the payoff (~30–60s) doesn't justify the flakiness risk when xdist already delivers the large win.
- Shared-collection refactor for file-filtering tests — deferred; the xdist win makes this unnecessary for now.
- `tests/eval/conftest.py` PDF fixture (`_generate_eval_corpus_pdf`) — writes to a fixed corpus path, but eval tests are excluded from the default run. Not a concern for this feature.
- Live test artifact isolation (`tests/eval/live/`) — excluded from default run. Not a concern.
- `tests/test_store_reindex_metadata.py` integration tests — 6 hardcoded `/tmp/reindex_*.md` paths; unsafe if `-m integration -n auto` is ever used, but excluded from the default run. Fix (replace with `tmp_path`) is mechanical and deferred.
- `tests/eval/test_baseline_contract.py` — writes to `archon_search/eval/metrics.py` (the actual source file); unsafe under any parallelism, but excluded from default run (`@pytest.mark.eval`). Fix deferred. **Warning**: never run `uv run pytest -m eval -n auto` — this test writes to `archon_search/eval/metrics.py` (the actual source file) and would corrupt it under parallelism.
- `tests/test_code_enricher.py` — uses `Path("tests/fixtures/...")` CWD-relative paths (lines ~244, 323, 416). Safe in practice because xdist workers inherit CWD from the controller process, but this is an implicit assumption not enforced by `--dist=loadfile`. Low risk; deferred to a future cleanup that converts these to `Path(__file__).parent`-relative paths.
- `tests/test_app.py` — `load_or_generate_key()` can write to `~/.archon-search/.search.env`; atomic write with `FileExistsError` retry makes this safe in practice; ensure `ARCHON_SEARCH_API_KEY` is set in CI.
- `tests/test_mcp.py` — module-scoped fixture swaps `fastmcp` in `sys.modules`; safe with `--dist=loadfile` because all tests in the file run on one worker.
- Any changes to test logic, coverage thresholds, or marker configuration.

## Key Decisions
- **`-n auto` in `addopts`, not opt-in**: prioritizes fast-by-default for local development; `-n0` is the escape hatch for debugging.
- **`--dist=loadfile` over `--dist=load`**: keeps `module`-scoped `connected_store` fixture within one worker; avoids redundant LanceDB connections and potential race conditions on the same collection name space.
- **Lock-timeout fix via monkeypatch, not env var**: the monkeypatch pattern (`monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", 1.0)`) already exists in 2 tests in the same file and 4 more across the test suite; no production code change needed.
- **Release CI and PR CI use `-n0`**: both CI workflows already blank `addopts` via `-o addopts=` and reconstruct all flags manually, so adding `-n auto --dist=loadfile` to `addopts` in `pyproject.toml` has zero effect on CI. The explicit `-n0` additions to both workflows are defensive/future-proof — they prevent xdist from being activated if the `addopts` override is ever removed.

## Edge Cases & Constraints
- **Coverage combining**: `pytest-cov` has native xdist support — workers write `.coverage.workerN` files; the main process combines them before applying `--cov-fail-under=85`. No `coverage combine` step needed in the default run.
- **`-x` (fail-fast) with xdist**: when one worker hits a failure, other workers continue running until their current test finishes. First-failure isolation requires `-n0 -x`. Document in CLAUDE.md.
- **`-s` (stdout passthrough)**: suppressed with xdist by default; requires `-n0 -s`. Document in CLAUDE.md.
- **`connected_store` module-scope + xdist**: with `--dist=loadfile`, all tests from a given module run on the same worker — module-scoped fixtures are created once per worker, not once per test. This is the correct behavior and matches current serial semantics.
- **No `~/.archon-search/` I/O in tests**: confirmed by exhaustive audit — all test I/O goes through `tmp_path`/`tmp_path_factory`; no fixed-port server binding; `TestClient` is in-process. References to `~/.archon-search` in tests are constant-value assertions only, not I/O. Exception: `test_app.py` key generation (covered in Out of Scope — safe due to atomic write + `ARCHON_SEARCH_API_KEY` env override).
- **`Final[float]` annotation**: Python does not enforce `Final` at runtime; `monkeypatch.setattr` works despite the annotation. Confirmed by existing usage in `test_store_lock.py` and `test_store_reindex_metadata.py`.
- **`three_page_pdf` fixture race condition**: the only xdist-unsafe fixture in the default suite. Fixed by prerequisite task above. The eval and live PDF fixtures are unaffected (excluded by markers).
- **Rollback**: To revert xdist, remove `-n auto --dist=loadfile` from `addopts` in `pyproject.toml`. If intermittent failures appear that pass with `-n0` but fail with `-n auto`, the test has a parallelism bug — bisect by running the suspect file alone with `-n auto` to isolate the race.
- **`asyncio.run()` under xdist**: `test_store.py` uses bare `asyncio.run()` calls inside synchronous test functions (not `@pytest.mark.asyncio`). Each xdist worker runs as an independent Python process with its own event loop. These bare calls create and destroy event loops per invocation, which is safe in isolation. There is no cross-worker loop sharing. The `asyncio_mode = "auto"` / `asyncio_default_fixture_loop_scope = "function"` pytest-asyncio config applies per-worker, not globally. Confirmed safe.
- **Tail risk — two large files**: The two largest test files, `test_store.py` (~6700 lines, ~303 tests) and `test_pipeline.py` (~7200 lines, ~223 tests), each land on a single dedicated worker under `--dist=loadfile`. Together (~530 tests, ~14,000 lines) they represent a significant fraction of total work and set the floor for achievable wall time on any number of workers. The baseline measurement step (see In Scope) will quantify whether the 150–200s target is realistic given this distribution.

## Open Questions
- None. Exhaustive parallelism audit (30 agents × 201 files; all files in the default run scope were audited) confirmed exactly two files using the unsafe `three_page_pdf` fixture (`test_fixtures.py`, `test_parser.py`) in the default run scope; fix is in-scope above.

## Future Iterations
- `asyncio_default_fixture_loop_scope = "module"`: ~30–60s additional savings, but requires cleaning up the dangling-coroutine warnings first. Worth revisiting once those warnings are resolved.
- Shared-collection fixture for file-filtering tests: ~40–50s additional savings if xdist still isn't fast enough after C10.
- `pytest-split` for CI matrix sharding if the suite grows beyond 300s even with parallelism.

## Recommendation
Build this now. An exhaustive audit of all 201 test files found exactly one unsafe fixture (`three_page_pdf`) and it has a 4-line fix. The lock-timeout monkeypatch follows an already-proven pattern used 2 times in the same file and 4 more across the test suite. The xdist addition is clean: no shared global state, no fixed ports, no `~/.archon-search/` writes. Expected outcome: ~600s (estimated) → ~150–200s (estimated for 8-core machine; actual measured speedup required as post-implementation verification) locally with zero changes to coverage gates, markers, or test logic. The only non-mechanical part is verifying the CI workflow updates; everything else is straightforward.

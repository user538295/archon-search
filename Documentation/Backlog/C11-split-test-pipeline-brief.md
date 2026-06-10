# Feature Brief: C11 — Split test_pipeline.py into Subfolder

## Problem
`tests/test_pipeline.py` (223 tests, ~355s serial (measured: `uv run pytest tests/test_pipeline.py -n0 --no-cov`)) pins one xdist worker for the entire parallel run, setting the test suite floor at 6+ minutes — more than double the 5-minute target.

## Goal
Split `test_pipeline.py` into three files under `tests/pipeline/`, each running on its own xdist worker, reducing `uv run pytest --no-cov` wall time of the full suite from ~357s (the file's ~355s serial runtime is the critical path under `--dist=loadfile`) to ≤150s wall time for `uv run pytest --no-cov` on a ≥4-core machine (the critical path is the longest single-file serial time; the target is achievable if no split file exceeds ~150s serial).

Note: the ≤150s wall-time target assumes: (a) the ingest group completes in <150s serial on its worker, and (b) at least 3 xdist workers are available to receive the three pipeline files simultaneously. On machines with fewer than 4 cores (`-n auto` < 3), the improvement is proportionally less. If the ingest file still exceeds 150s after the split, the contingency is to split it further into `test_pipeline_ingest_file.py` and `test_pipeline_ingest_directory.py`.

## Users & Context
Developers running the full test suite locally while iterating (e.g., during `/implement-next` runs) and CI pipelines waiting on test feedback. The current 7–8 minute per-task test cycle is the primary friction point.

## Core Flow
1. Create `tests/pipeline/` directory with an empty `__init__.py`, consistent with every other test subdirectory in this project (`tests/cli/`, `tests/server/`, `tests/telemetry/`, etc.).
2. Create `tests/pipeline/conftest.py` with all shared helpers: `MockEmbedderBackend`, `MockRerankerBackend`, `make_embedder()`, `make_reranker()`, `make_pipeline()`. Also move the Group-C-specific helpers `_scored()`, `_meta()`, and `_search_many_pipeline()` here (or keep them local to `test_pipeline_multi.py` — either works; the implementer decides at split time).

   Note: these are plain Python functions/classes, NOT pytest fixtures. Do not add `@pytest.fixture` decorators — tests call them directly.

   Beyond the 8 listed above, the file contains 14+ additional section-local helpers (e.g., `_make_scored_candidate`, `_make_stub_store_for_embedding_tests`, `_make_pipeline_for_recompute`, `_make_candidate`, `_rag_scored`). These are used by specific test sections and should move to whichever file their consuming tests land in. Before starting the split, run `grep -n '^def [^t]\|^class [A-Z]' tests/test_pipeline.py` to enumerate all non-test definitions and pre-assign each to a target file.
3. Create `tests/pipeline/test_pipeline_ingest.py` — the 83 ingest tests (file/directory/chunking/centroid/format handling). These hold the slowest individual tests (10–16s each); distributing them across their own worker is the primary speedup.
4. Create `tests/pipeline/test_pipeline_search.py` — the 57 search/context/trace tests.
5. Create `tests/pipeline/test_pipeline_multi.py` — the 83 multi-collection fanout, recompute, and RAG fusion tests. Predominantly mock-based; verify at split time that no `connected_store` tests land here (see Edge Cases).
6. Delete `tests/test_pipeline.py`.
7. Verify: `uv run pytest` passes with ≥85% coverage; `time uv run pytest --no-cov` completes in ≤150s. Run `uv run pytest tests/pipeline/ --collect-only -q | tail -1` — must show the same count as `uv run pytest tests/test_pipeline.py --collect-only -q | tail -1` run BEFORE the split (note: parametrized tests expand the count beyond the number of `def test_` functions, so use the actual collected count, not the function count). Also run `uv run pytest tests/pipeline/ -n0 -x --no-cov` to confirm serial mode (CI uses `-n0`) produces no failures. Run `uv run pytest tests/pipeline/ --collect-only -q | awk -F'::' '{print $NF}' | sort | uniq -d` — must produce empty output (strips file-path prefix so cross-file duplicate function names are detected, not just within-file ones).

## In Scope
- Creating `tests/pipeline/` with one `conftest.py` and three test files
- Moving all 223 tests with no logic changes — assertions, mocks, and fixture parameters unchanged
- Deleting `tests/test_pipeline.py`
- Verifying wall-time improvement and coverage gate after the split
- Updating `Documentation/Architecture/200_testing_strategy.md`: glob pattern in the tier diagram (`tests/test_*.py` → `tests/**/test_*.py`) and the 'Adding a test' guidance to reference `tests/pipeline/` for pipeline tests

## Out of Scope
- Changing any test logic, assertions, or mock behavior
- Splitting other slow files (`test_sync_e2e.py`, `test_pipeline_ingest_directory_fts.py`) — those are already on separate workers and not the bottleneck today
- `asyncio_default_fixture_loop_scope = "module"` optimization (deferred from C10; still risky)
- Any production code changes

## Key Decisions
- **Subfolder over flat files**: `tests/pipeline/conftest.py` enables pytest's native fixture discovery for shared helpers — no duplication, no `_helpers.py` anti-pattern, clean namespace.
- **Delete original `test_pipeline.py`**: Dead files cause confusion. The three new files are clearly named and greppable.
- **Naming — `_ingest` / `_search` / `_multi`**: Functional grouping that matches the existing split in `test_pipeline_acl.py`, `test_pipeline_metadata.py`, etc.
- **No logic changes**: This is a pure file reorganization; correctness is unchanged.

## Edge Cases & Constraints
- **`connected_store` module scope remains correct but multiplied**: Under `--dist=loadfile`, all tests in a file run on one worker. Each file that uses `connected_store` instantiates its own instance — after the split, up to 2 instances will exist concurrently (ingest + search files) vs the current 1. Each instance spawns a Tokio thread pool (per the fixture comment). This is acceptable given the workers run in parallel, but it is not zero-cost.
- **`tests/` on `sys.path`**: The root `tests/conftest.py` adds itself to `sys.path` before pytest discovers `tests/pipeline/conftest.py`. Imports like `from archon_search.pipeline import SearchPipeline` work unchanged inside the subfolder.
- **Group C (`_multi`) is predominantly mock-based**: The implementer must verify at split time whether any `connected_store` tests (e.g., `test_store_has_vector_index_*`, `test_hybrid_search_with_trace_filters_applied`) land in this group; if so, adjust them to Group A or B rather than forcing a module-scoped LanceDB connection in what should be a lightweight file.
- **Coverage combining**: Unchanged — pytest-cov's xdist integration handles multiple workers writing `.coverage.workerN` files regardless of subdirectory depth.
- **CI workflows**: Already use `-n0` explicitly (from C10, Task 3.1); unaffected by this split.
- **Existing `test_pipeline_*.py` files**: `test_pipeline_acl.py`, `test_pipeline_metadata.py`, etc. remain in `tests/` root — this split only touches `test_pipeline.py` itself.

## Open Questions
- The 83/57/83 test counts are estimates from functional grouping analysis. The implementer must produce the exact test-function-to-file mapping at implementation time, verifying boundary cases (tests near section transitions that might belong in either group). The functional grouping principle is clear; exact counts are not guarantors.
- **Split decision rules for ambiguous tests** (apply in order): (1) Tests that call `pipeline.ingest_file()`, `pipeline.ingest_directory()`, or `pipeline.recompute_collection_meta()` → ingest file. (2) Tests that call `pipeline.search()`, `pipeline.search_with_context()`, `pipeline.explain()`, or test single-collection result shapes → search file. (3) Tests that call `pipeline.search_many()`, `_fuse_rag_fusion_results`, or test multi-collection fanout → multi file. (4) Pure unit tests of private helpers → go with the public method they serve. When a test exercises multiple categories (e.g., an ingest-then-search round-trip), assign it to the dominant call path.

## Future Iterations
- Split `test_sync_e2e.py` if it becomes a bottleneck after this change (currently it completes well within the new floor).
- `asyncio_default_fixture_loop_scope = "module"` for an additional 30–60s savings once dangling-coroutine warnings are resolved.
- Move `test_pipeline_acl.py`, `test_pipeline_metadata.py`, and related files into `tests/pipeline/` for a tidy final structure (cosmetic; no timing benefit).

## Recommendation
Build this now. It is the only change that gets the suite below 5 minutes — all other optimizations combined save under 60s. The work is mechanical: move test functions, create one conftest.py, delete the original. Zero risk to coverage, zero risk to test correctness. The only non-trivial part is verifying that pytest discovers fixtures correctly from the new subfolder; the root `conftest.py`'s `sys.path` injection already guarantees this.

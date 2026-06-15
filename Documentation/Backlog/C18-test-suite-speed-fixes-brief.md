# Feature Brief: Test Suite Speed Fixes (Fix 1 + Conditional Fixes 2/3)

## Problem

Running `uv run pytest` on a developer machine that already has `ANTHROPIC_API_KEY` exported in the shell takes substantially longer than a clean run. Engineers commonly export the key for other Claude/Anthropic SDK work, so the slow path is the common path locally. **This is a developer-machine-only issue.** CI workflows (`.github/workflows/archon-search-pr.yml:65`, `.github/workflows/archon-search-release.yml:49`) invoke pytest with `-n0` and never set `ANTHROPIC_API_KEY` in the job environment, so the 30s timeout described below never fires in CI and none of the fixes in this brief change CI wall-clock.

When the key is present, every `ingest_directory` call against a new collection triggers `description_generator.py:61-97`, which hits `asyncio.wait_for(..., timeout=_TIMEOUT_SECONDS)` with `_TIMEOUT_SECONDS = 30` (`description_generator.py:26`, line 87) on the SDK call. The SDK can hang in this test environment, so the wall-clock per affected test rises from a few seconds to roughly 35–36s. The same env var also gates two other call sites that may show the same symptom in tests that exercise routing or rag-fusion paths: `archon_search/hyde.py:101` and `archon_search/rag_fusion.py:138`.

Two large test files containing many of the affected tests (`tests/pipeline/test_pipeline_ingest.py`, 118 tests; `tests/test_sync_e2e.py`, 15 tests across 12 classes) were assumed in earlier drafts of this brief to pile onto one xdist worker. That assumption is wrong under the current `--dist=loadgroup` config (see Conditional section below).

## Goal

`uv run pytest` (no extra flags, using the configured `addopts` with `-n auto --dist=loadgroup`) completes faster on a developer machine that has `ANTHROPIC_API_KEY` set. The honest, defensible target is a measurable reduction attributable to Fix 1 alone; any harder absolute number (e.g. "under 120s") is only meaningful with a published measurement protocol.

### Measurement Protocol

All timing claims must follow this protocol or be labeled as anecdotal:

- **Command**: `test -n "$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY must be set; aborting"; exit 1; } && ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest --no-cov 2>&1 | tail -3` (key present, mirrors the developer scenario). The precondition check guards against the shell variable being unset — if it expands to empty the early-exit guards fire and the measurement reflects the fast path, not the symptom.
- **Runs**: 1 warm-up run (discarded), then 5 consecutive runs. Record p50 of the 5.
- **Machine**: brief author's machine is M-series Apple Silicon with 14 logical CPUs (cross-referenced against `Documentation/Completed/C12-dist-load-session-store-brief.md` line 78). Other machines will produce different numbers.
- **Conditions**: no other heavy processes running; `~/.archon-search/` cleared between runs is NOT required (the autouse fixture isolates per-worker data dirs).
- **Acceptance**: p50 with Fix 1 applied is lower than p50 without, by a margin larger than the OBSERVED run-to-run variance (max - min across the 5 pre-fix runs). If pre-fix variance is X seconds, treat any improvement < X as noise.

### Wall-clock impact estimate (Fix 1 only)

Fix 1 eliminates the 30s SDK wait on every test that calls `ingest_directory` (or any path that reaches `generate_description` with a non-empty chunk list). Call N the number of affected tests and W the xdist worker count (14 on the reference machine).

- **CPU-time saving**: ≈ `N × 30s`.
- **Wall-clock saving under `-n auto --dist=loadgroup`**: ≈ `ceil(N / W) × 30s` in the best case where the slow tests distribute evenly. If they cluster on one worker the saving converges to `N × 30s` for that worker (and the worker dominates wall time).

The honest implication: if N is around 17, the wall-clock saving under 14 workers is roughly 30–60s, not 100s. **Fix 1 alone may not hit any specific absolute target.** If it falls short, the remaining options are (a) re-measure, (b) implement Fix 2/Fix 3 ONLY if profiling shows specific pileups under `--dist=loadgroup` (see Conditional section), or (c) pick from Future Iterations.

### Anecdotal baseline

The "~173s" figure that initially motivated this brief was a single observation on the brief author's machine with no recorded run count or warm-up. Treat it as a single anecdote, not a baseline. (For reference, C12's measured median on the same class of machine was 157s; see `Documentation/Completed/C12-dist-load-session-store-brief.md` line 88.) Use the Measurement Protocol above to establish a real baseline before claiming improvement.

## Users & Context

Engineers running the default test suite locally during active development. They typically have `ANTHROPIC_API_KEY` in their shell for other Claude/Anthropic SDK work, so they hit the slow path on every local run. The user-visible problem is flow-disruption from a long inner-loop.

## Core Flow

### Fix 1 — Clear `ANTHROPIC_API_KEY` in conftest autouse

1. The function-scoped autouse fixture `_archon_isolated_data_dir` (`tests/conftest.py:84-108`) clears 5 archon-namespace env vars at lines 97-103 (`ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_CONTAINER`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_CONFIG`).
2. Add a SEPARATE block in the same fixture that clears `ANTHROPIC_API_KEY` using `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` — not extending the existing tuple, because `ANTHROPIC_API_KEY` is a third-party vendor key and conflating it with the archon-namespace clearing block hides the motivation (preventing a 30s SDK timeout in tests). The new block should carry an inline comment naming the motivation explicitly.
3. `description_generator.py:76-78` has an early-exit guard: `if not os.environ.get("ANTHROPIC_API_KEY"): return None`. With the key cleared, every `ingest_directory` call on a new collection returns immediately from `generate_description` — no SDK call, no 30s wait.
4. The identical guards in `archon_search/hyde.py:101` and `archon_search/rag_fusion.py:138` will also short-circuit. Tests that exercise those paths and need the key set continue to set it themselves via `monkeypatch.setenv` (verified — see Edge Cases).

## In Scope

- **Fix 1 only**: clear `ANTHROPIC_API_KEY` in `tests/conftest.py:_archon_isolated_data_dir` autouse fixture using `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` in a dedicated block with an explanatory comment, distinct from the archon-namespace clearing block.
- No changes to `pyproject.toml` `addopts`, markers, or `norecursedirs`.
- No production code changes.

## Conditional — Implement Only If Fix 1 Insufficient

Fixes 2 and 3 below were assumed in earlier drafts to be necessary because "ungrouped tests pile onto one worker due to collection ordering." This assumption is wrong under the current dist mode. Per `Documentation/Completed/C12-dist-load-session-store-brief.md` (line 48), `--dist=loadgroup` uses demand-based scheduling for ungrouped tests: a finished worker pulls the next pending test from the queue. There is no per-file pinning for ungrouped tests; ungrouped tests already distribute across workers.

Adding `xdist_group` markers to currently-ungrouped tests would FORCE every test that shares a group name onto a single worker — the OPPOSITE of distribution. This is the right thing only if profiling shows specific pileups; otherwise it is a regression.

**Gate for implementing Fix 2 or Fix 3**: BOTH of the following must hold:
1. Fix 1's measured wall-clock impact (per the Measurement Protocol) leaves the suite slower than the developer wants.
2. Profiling with `--durations=0` AND per-worker logs confirms that a specific subset of tests in `test_pipeline_ingest.py` and/or `test_sync_e2e.py` lands disproportionately on one worker AND is causing the longest worker to dominate wall time.

If both hold, the candidates below are worth evaluating:

### Fix 2 (conditional) — xdist-group `test_pipeline_ingest.py`

- `tests/pipeline/test_pipeline_ingest.py` has 118 `def test_` functions, no classes, no `pytestmark`/`xdist_group` markers (verified by `grep -c "def test_"` and absence of `xdist_group` in the file).
- IF profiling shows pileups, candidate groupings are logical-domain (e.g. by function-name prefix) or speed-based (slow tests in one group, fast in another). Speed-based grouping is the more honest choice once the slow tests are enumerated.
- Risk: adding any `xdist_group` marker pins matching tests to ONE worker. Use only if profiling confirms a benefit.

### Fix 3 (conditional) — xdist-group `test_sync_e2e.py`

- `tests/test_sync_e2e.py` has 15 tests across 12 classes (`TestS15_1` through `TestS15_10b`, verified by `grep "class TestS15"`).
- The earlier claim that "five tests take ~35s each" is not supported by data in this brief. Fix 3 cannot be implemented without profiling that enumerates the slow tests by name. Required artifact before implementation: `uv run pytest --durations=0 -n0 --no-cov tests/test_sync_e2e.py 2>&1 | tail -20` with `ANTHROPIC_API_KEY` set AND a second run with it cleared, to distinguish the SDK-timeout floor from genuine I/O cost.
- After Fix 1, the 30s SDK floor is gone. If sync_e2e tests then drop below 10s each, 15 tests will distribute naturally across 14 workers with no pileup risk and Fix 3 is dead weight.

## Out of Scope

- Docling session-scoped `DocumentParser` sharing — separate change, separate risk profile.
- Batching the 1000-chunk loop in `test_fts_consistency_after_50_operations.py` — valid optimization but separate PR scope.
- Excluding eval suite from default run (`-m "not eval"`) — bigger policy decision, separate discussion.
- Fixing `test_prefix_filtered_search_p95_regression_under_threshold@benchmark` failure — pre-existing flakiness, separate issue.
- Any mock at conftest level that would globally suppress `generate_description` — clearing the env var is the minimal, structurally honest fix (lets the existing early-exit guard do its job rather than bypassing the function).

## Key Decisions

**Fix 1: where to clear the env var**

- **Option A (chosen): Clear `ANTHROPIC_API_KEY` in the existing function-scoped autouse fixture `_archon_isolated_data_dir`** — runs per-test, matches the existing isolation pattern, and the early-exit guard in `description_generator.py:76-78` makes the function call cheap.
- **Option B: Add an autouse mock for `archon_search.description_generator.generate_description → AsyncMock(return_value=None)` in `tests/pipeline/conftest.py`** — stronger guarantee (SDK never even imported), but silently suppresses a call site that 3 existing tests already mock deliberately. Adds a second layer of patching that confuses future readers.
- **Option C: Pipeline-scoped conftest (clear key only for tests under `tests/pipeline/`)** — localizes blast radius. Worth considering because the original symptom is concentrated in pipeline tests. Rejected for now because (a) the affected tests are NOT all under `tests/pipeline/` (see Edge Cases — `test_sync_e2e.py`, `test_pipeline_acl.py`, `test_pipeline_code_enricher.py`, `test_pipeline_ingest_directory_fts.py`, `tests/integration/test_fts_delete_no_phantom.py` all live outside `tests/pipeline/`), and (b) splitting the clear across multiple conftests makes the behavior harder to reason about.
- **Option D: Session-scoped fixture (clear once per worker)** — would skip per-test `monkeypatch` overhead but would also surrender per-test override granularity. Tests that need the key set (`test_hyde.py`, `test_rag_fusion.py`, `test_description_generator.py` — see Edge Cases) currently use `monkeypatch.setenv` which composes correctly with the function-scoped autouse. With session scope the override semantics change. Rejected.
- **Option E: `pytest_runtest_setup` hook in `tests/conftest.py`** — equivalent in effect to Option A but uses the lower-level pytest API for a one-line behavior. Rejected because the autouse fixture is the established pattern and adding a hook for one env var creates a second mechanism that future readers must locate.

## Edge Cases & Constraints

- **`tests/test_description_generator.py` is SAFE under Fix 1.** Verified directly:
  - `test_no_api_key_returns_none` (`tests/test_description_generator.py:21-28`) explicitly does `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` itself — same effect as the autouse, no conflict.
  - `test_api_failure_returns_none` (`tests/test_description_generator.py:30-43`) and `test_successful_generation_returns_description` (`tests/test_description_generator.py:53-78`) both call `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")` (lines 33 and 56). Setup ordering runs the test's own `monkeypatch.setenv` AFTER the autouse `monkeypatch.delenv`, so the per-test setenv wins.
  - `test_empty_chunks_returns_none` (`tests/test_description_generator.py:45-51`) returns before reaching the env-var check (`description_generator.py:73-74` early-exit on empty chunks).
  - `test_no_archon_imports` (`tests/test_description_generator.py:80-96`) is a static-import check; never reaches the env-var check.
- **`tests/test_hyde.py` and `tests/test_rag_fusion.py` are SAFE under Fix 1.** Both files use `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")` per-test where they need the key (7 setenv calls in `test_hyde.py`, 11 in `test_rag_fusion.py` — verified via `grep -c 'setenv("ANTHROPIC_API_KEY"'`) and `monkeypatch.delenv(..., raising=False)` where they need it absent. Same composition rule as above. The architectural point worth flagging: `archon_search/hyde.py:101` and `archon_search/rag_fusion.py:138` have structurally similar early-exit guards to `description_generator.py:76` (same env-var check, different return types and warning state — see Future Iterations / Affected-call-site audit), so clearing the key short-circuits all three call sites. Tests that exercise HyDE or RAG Fusion paths via `ingest_directory` will see those code paths return early — desired behavior, since the original problem statement also includes those paths.
- **Tests that call `ingest_directory` without managing `ANTHROPIC_API_KEY` are SAFE under Fix 1 and benefit from it.** Specifically: `tests/test_pipeline_acl.py`, `tests/test_pipeline_code_enricher.py`, `tests/test_pipeline_ingest_directory_fts.py`, and `tests/integration/test_fts_delete_no_phantom.py`. The root autouse fixture in `tests/conftest.py` applies to all of them (verified — `tests/integration/conftest.py` does not override the autouse), so the 30s SDK wait that previously occurred when the developer had `ANTHROPIC_API_KEY` in their shell is eliminated for these tests.
- **`tests/integration/test_wizard_e2e.py` is SAFE under Fix 1.** Uses `monkeypatch.setenv("ANTHROPIC_API_KEY", ...)` and `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` per-test (verified by grep). Same composition rule as `test_description_generator.py` — per-test `setenv` runs after the autouse `delenv`, so the per-test value wins.
- **`tests/test_e2e_wizard_optional_features.py` and `tests/test_install_wizard_features.py` are SAFE under Fix 1.** Both manage `ANTHROPIC_API_KEY` via `patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"})` and a local `_no_anthropic_key()` context manager (`test_e2e_wizard_optional_features.py:31-40`, `test_install_wizard_features.py:15-20`). Composition with the autouse `delenv` is safe: their per-test `setenv` runs after the autouse clears the key (setenv wins); `patch.dict` merges into the post-autouse env, so when its dict contains `ANTHROPIC_API_KEY` the patched value wins. The `_no_anthropic_key()` context manager already clears the key, so the autouse is a no-op for those branches.
- **`tests/eval/live/` is split across three files; Fix 1 affects only one of them.** `pyproject.toml` `norecursedirs` (lines 81-91) excludes only `tests/eval/live_benchmark`, NOT `tests/eval/live/`, so these files collect but skip at runtime in different ways:
  - `tests/eval/live/test_live_rag_fusion.py`: 5 test functions call `_skip_if_no_api_key()` (verified at `tests/eval/live/test_live_rag_fusion.py:137, 181, 241, 376, 441`), which calls `pytest.skip(...)` at test-body execution time (`tests/eval/live/test_live_rag_fusion.py:42-45`). With Fix 1 these 5 tests will ALWAYS skip during default runs because the autouse clears the key in the worker process even if the developer has it in their shell. (`test_live_rag_fusion_fallback_on_missing_key` at line 319 deliberately clears the key itself — unaffected.)
  - `tests/eval/live/test_live_acceptance.py` and `tests/eval/live/test_live_eval_suite.py`: contain 6 tests total (verified by `grep -n 'ANTHROPIC_API_KEY' tests/eval/live/test_live_acceptance.py tests/eval/live/test_live_eval_suite.py` → empty). They do not check `ANTHROPIC_API_KEY` at all — they skip only when fastembed model weights are missing. **Fix 1 does not change their behavior.**
  - Workaround for re-running the 5 affected `test_live_rag_fusion.py` tests after Fix 1: no clean workaround exists today. Options are (a) add an opt-out marker analogous to `@pytest.mark.archon_unset_data_dir` (out of scope; see Design Decisions Required), or (b) temporarily revert Fix 1's `delenv` for the live invocation. There is no in-process env var the developer can set from the shell to bypass the autouse `monkeypatch.delenv`. Accepting this lock-out is the recommended position because it eliminates accidental live API calls in default runs.
- **`ARCHON_SEARCH_API_KEY` is unaffected.** `tests/conftest.py:34` defines `TEST_API_KEY = "0" * 64`, then `tests/conftest.py:35` assigns it directly with `os.environ["ARCHON_SEARCH_API_KEY"] = TEST_API_KEY` at module import time. The autouse fixture's comment at lines 94-95 explicitly notes it must remain set. Fix 1 touches `ANTHROPIC_API_KEY` only.
- **xdist_group collisions** (only relevant if Fix 2 or Fix 3 is ever implemented): existing groups in the codebase are `benchmark` (e.g. `tests/test_search_filtered_benchmark.py:178`), `mcp` (e.g. `tests/test_mcp_export.py:22`), `install` (e.g. `tests/test_install_run.py:16`), `live_benchmark` (e.g. `tests/eval/live_benchmark/test_real_model_search_benchmark.py:49`), and `docling` (e.g. `tests/test_parser.py:357`, `tests/integration/test_http_enrichment_metadata.py:103,237`, `tests/eval/test_eval_suite.py:486`). Any new groups must use names that don't collide.

## Alternative Hypotheses

Before claiming `generate_description`'s 30s timeout is the cause, rule out other 30s sources in the codebase:

- `archon_search/constants.py:19`: `INGEST_LOCK_TIMEOUT_S: Final[float] = 30.0`. Ingest path lock timeout — if a test deadlocks waiting for this lock, it presents identically (a 30s wait followed by failure or fallback).
- `archon_search/model_validation.py:24`: `timeout_seconds: float = 30.0`. Model validation probe.
- `description_generator.py:26`: `_TIMEOUT_SECONDS = 30`. The hypothesised cause.

The verification command in Pre-implementation Verification (#1 below) distinguishes them: if a slow test stays at ~36s after clearing `ANTHROPIC_API_KEY`, the root cause is NOT `generate_description` and Fix 1 will not help that test.

**SDK fast-fail vs hang for fake/malformed keys.** `_call_haiku` (`archon_search/description_generator.py:100-129`) constructs a `ClaudeSDKClient` and then `await client.connect()` is the first authentication-touching step; the call body issues `client.query(prompt)` and iterates `client.receive_response()`. The SDK does not eagerly probe authentication at object construction. Whether a syntactically-invalid `ANTHROPIC_API_KEY=fake-key` produces a fast 401 in <1s or a 30s `asyncio.wait_for` hang depends on the SDK subprocess's behavior at query time and is not statically determinable from this codebase. If the SDK rejects malformed keys eagerly, `ANTHROPIC_API_KEY=fake-key` produces a fast failure instead of a 30s hang and the `--durations` enumeration in Pre-implementation Verification #2 will return empty (no tests >25s). To reliably enumerate symptomatic tests, run #2 with the real key: `ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest ...`.

## Pre-implementation Verification

These commands MUST be run before implementing anything. They distinguish between competing causes and validate the count/identity of affected tests. **Steps 1-4 MUST complete BEFORE making any code change to `tests/conftest.py`.**

1. **Confirm the 36s floor IS `ANTHROPIC_API_KEY` → `generate_description`** (not an alternative 30s source). This confirms the root cause for the tested subset only:
   ```
   ANTHROPIC_API_KEY= uv run pytest \
     tests/test_pipeline_code_enricher.py::test_ingest_directory_forwards_collection_root \
     'tests/test_sync_e2e.py::TestS15_3_FileModifiedIncrementalUpdate::test_sync_reindexes_on_file_change' \
     -v --no-cov -n0
   ```
   Expected: both drop from ~36s to a few seconds. If either stays at ~36s, Fix 1 is misdirected for that test (see Alternative Hypotheses). To distinguish hypotheses for OTHER candidate-slow tests in the suite, compare per-test durations from #2 (with key) and a key-empty re-run of #2; tests whose duration stays high when the key is cleared are NOT `generate_description` cases — they are alternative-hypothesis candidates.

2. **Enumerate the affected tests** (replaces the unverified "~17 tests" claim). Requires a real key — fake keys may fail-fast at SDK level and produce no enumeration (see Alternative Hypotheses):
   ```
   test -n "$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY must be set; aborting"; exit 1; }
   ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest --durations=0 -n0 --no-cov 2>&1 | awk '$1+0 > 25' | head -40
   ```
   (Runtime: ~10-15 minutes serial; if the SDK truly hangs on multiple tests, longer. To narrow first, run only on suspected files: `... tests/pipeline/test_pipeline_ingest.py tests/test_sync_e2e.py ...`.)
   The output is the actual list of tests taking >25s. Capture this list and use it as the affected-test inventory in the implementation plan. If the output is exactly 40 lines, re-run without `| head -40` to capture the full list — silent truncation at the hypothesis-validation step would produce an undercount. Then re-run with the key cleared (precondition guard must be SKIPPED for this counterpart run) to confirm the list shrinks:
   ```
   ANTHROPIC_API_KEY= uv run pytest --durations=0 -n0 --no-cov 2>&1 | awk '$1+0 > 25' | head -40
   ```

3. **Measure baseline (key set) per the Measurement Protocol**:
   ```
   test -n "$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY must be set; aborting"; exit 1; }
   ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest --no-cov 2>&1 | tail -3
   ```
   1 warm-up run, 5 measured runs, record p50.

4. **Confirm test inventory and pass-rate**:
   ```
   uv run pytest --collect-only -q 2>&1 | tail -3
   ```
   The trailing line gives the total collected test count (the brief's prior "4700+" figure was unverified).

## Verification Protocol (post-implementation)

Run steps 5-8 AFTER Fix 1 is applied. Compare results to the baseline captured in step 3.

5. **Re-measure wall-clock with Fix 1 applied** using the Measurement Protocol (5 runs, p50). Compare delta to the baseline from #3. The delta is the honest impact claim.

6. **Coverage check**: Fix 1 makes `generate_description` return `None` early in many tests. This may reduce coverage of `description_generator.py:80-97` (the SDK-call body and exception handlers). Run:
   ```
   uv run pytest 2>&1 | grep description_generator
   ```
   (Coverage and term-missing are already enabled via `addopts` — no extra flags needed.) Compare output before and after Fix 1. The overall `--cov-fail-under=85` gate must still pass; the per-module coverage delta should be noted.

   Why coverage likely won't drop: `tests/test_description_generator.py` mocks `ClaudeSDKClient` independently of the env var. Those tests still exercise the SDK-call body. Tests that previously had the env var set (via shell) but no SDK mock were NOT exercising the SDK in-process — the SDK would only return real data with a real key, which test environments don't have. So the SDK-call body coverage is preserved by `test_description_generator.py`'s explicit mocks. If coverage DOES drop, add a dedicated test that uses `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")` to simulate a set key and asserts the SDK-call path executes (this matches the existing pattern in `tests/test_description_generator.py`).

7. **Pass-count check**: confirm the suite still passes with the same number of collected tests as #4. Collected count must remain unchanged. PASSED count may decrease by up to 5 (corresponding to the live RAG-Fusion tests that now always skip — these are the 5 `test_live_rag_fusion.py` functions that call `_skip_if_no_api_key()` at lines 137, 181, 241, 376, 441). Any OTHER passed→skipped or passed→failed shift IS a regression.

8. **HyDE / RAG-Fusion regression check**: confirm the same number of tests PASS as before Fix 1 in `test_hyde.py`, `test_rag_fusion.py`, and `test_description_generator.py`. Capture pass/skip counts before and after Fix 1 with:
   ```
   uv run pytest tests/test_hyde.py tests/test_rag_fusion.py tests/test_description_generator.py --no-cov -n0 2>&1 | tail -1
   ```
   The trailing summary shows `N passed, M skipped`. A regression includes a PASSED → SKIPPED shift, not just a failure. These are the files most architecturally entangled with the env var.

## Design Decisions Required

1. **Live-eval workaround — RESOLVED: accept the skip behavior, no opt-out marker.** After Fix 1, default runs will always skip the 5 `test_live_rag_fusion.py` tests that gate on `ANTHROPIC_API_KEY` (the other 6 tests in `test_live_acceptance.py` and `test_live_eval_suite.py` are unaffected — they don't check the key). Accepting this is the recommended position because it eliminates accidental live API calls from default runs. Adding an opt-out marker analogous to `@pytest.mark.archon_keep_anthropic_key` is out of scope for this brief; the structural follow-up of adding `tests/eval/live/` to `norecursedirs` (see Future Iterations) is the cleaner long-term path.

## Future Iterations

- **Session-scoped `DocumentParser`** + `xdist_group("docling")`: Eliminates the second 80s docling cold-start (currently a different worker pays it). Saves ~70s on CI where model is not cached. Depends on confirming thread-safety of `DocumentConverter` across sequential tests.
- **Batch Phase 1 of `test_fts_consistency_after_50_operations.py`**: Replace 1000 sequential single-chunk `ingest_chunks` calls with one batched call. Requires verifying `_do_update_meta_on_add` produces an equivalent centroid when called once with N vectors vs N times with one vector.
- **Exclude eval suite from default addopts**: `-m "not live_benchmark and not eval"` removes ~330s of CPU work (14 full corpus ingests). Bigger discussion — see Out of Scope.
- **Add `tests/eval/live/` to `norecursedirs`** in `pyproject.toml` (follow-up to Design Decision #1, resolved above as "accept"): mirrors the `live_benchmark` pattern and makes the live-vs-default boundary structurally enforced. Currently `tests/eval/live/` is collected and the 5 affected tests skip at runtime; adding `norecursedirs` removes the collection cost AND avoids the asymmetry that Fix 1 introduces (the 6 unaffected tests in `test_live_acceptance.py` / `test_live_eval_suite.py` continue to run on default invocations but cannot actually pass without model weights). Pair this change with explicit `tests/eval/live/` invocation in CI / live-eval workflows.
- **Affected-call-site audit**: with three call sites (`description_generator.py:76`, `hyde.py:101`, `rag_fusion.py:138`) gating on the same env var, consider documenting them in one place (e.g. `CLAUDE.md` or a code comment cross-reference) so the pattern is discoverable. Avoid premature DRY — the three guards have different return types (`None` for `description_generator` and `hyde`, `[]` for `rag_fusion`) and different warning state (one-time `_warned_no_key` warnings in `hyde` and `rag_fusion`, none in `description_generator`). A shared helper would obscure these differences and is not justified by the three current call sites alone.

## Recommendation

Implement Fix 1 only — Design Decision #1 is resolved (accept the live-eval skip behavior for the 5 affected `test_live_rag_fusion.py` tests; no opt-out marker). Fix 1 is one line in the autouse fixture, structurally clean, and addresses the dominant developer-machine slowdown when the key is set. Run the Pre-implementation Verification commands first to confirm the hypothesis (don't waste time fixing the wrong thing). Run the Verification Protocol commands after to claim a measured improvement honestly. Do NOT implement Fixes 2 or 3 unless the conditional gate fires after Fix 1's measured impact is in hand AND profiling identifies specific pileups under `--dist=loadgroup`.

CI is not affected by any change in this brief (CI uses `-n0` and never sets `ANTHROPIC_API_KEY`); the wins are developer-DX only.

The hardest part of this feature is not the implementation but the measurement discipline. Each verification command above must run; the inventory of affected tests must be enumerated rather than guessed; the post-implementation delta must be a measured p50, not a single anecdote.

# C16 — Real-Model Search Latency Benchmark
**Purpose**: CI-gated PR benchmark using real fastembed embedder and cross-encoder reranker so regressions in ONNX configuration, fastembed, or reranker usage are caught before merge rather than at runtime.
**Audience**: archon-search contributors implementing C16; reviewers of the resulting PR.
**Status**: To Do

---

## Background

Every existing latency benchmark uses `EvalEmbedderBackend` (SHA-256 hash) or `EvalRerankerBackend` (BM25-like stub), so the production code path through real fastembed and ONNX inference has zero CI performance gate. The full design rationale and constraints are in `Documentation/Backlog/C16-real-model-search-latency-benchmark-brief.md`.

---

## Goal

Every PR build runs two real-model benchmark tests (`test_real_model_search_steady_state_p95` and `test_real_model_search_cold_load_p90`) under a new `live_benchmark` pytest marker with separate gated latency thresholds. A regression in the fastembed/ONNX path fails the PR.

---

## Scope

### In Scope
- `live_benchmark` marker in `pyproject.toml` with `not live_benchmark` added to default test step exclusion in `archon-search-pr.yml`
- `BenchmarkThresholds` dataclass + `load_benchmark_thresholds(path)` in `archon_search/eval/runner.py`
- `[real_model_search]` section in `tests/eval/live_thresholds.toml` with placeholder values pending CI calibration
- `tests/eval/live_benchmark/conftest.py`: removes fastembed stubs, resets ML thread env vars, adds model-cache skip hook
- `tests/eval/live_benchmark/test_real_model_search_benchmark.py`: steady-state (100-iter p95) + cold-load (N=10 p90) tests
- Model cache restore + prefetch step (3 retries, exponential backoff) + dedicated `pytest -m live_benchmark` step with `timeout-minutes: 3` in `archon-search-pr.yml`
- Documentation: `Architecture/200_testing_strategy.md`, `Architecture/210_performance_and_scalability.md`, `tests/eval/README.md`, `live_thresholds.toml` header comment

### Out of Scope
- Tightening existing stub-based thresholds (separate concern)
- Isolated component benchmarks (embedder-only, reranker-only)
- Nightly/scheduled lane; GPU runtime testing; memory footprint assertions
- Changes to `test_latency_stability` in `tests/eval/live/`

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change
- The root `tests/conftest.py` and `tests/_search_stubs.py` — stub installation and thread throttling continue for all non-benchmark tests
- `tests/eval/conftest.py` — deterministic eval backends remain active for eval tests
- `archon_search/eval/runner.py::load_thresholds()` — existing function signature and behavior are unchanged
- `archon_search/eval/live_report.py::load_live_thresholds()` — continues to silently ignore `[real_model_search]` (the new section has no `[quality_floors]`)
- `tests/eval/live/test_live_acceptance.py::test_latency_stability` — report-only; untouched

---

## Known limitations / accepted trade-offs
- Cold-load measures ONNX session creation within one process (module cache, runtime, shared libraries persist across iterations), not fresh-process startup cost — architecturally infeasible within a single pytest run.
- Chunking uses stubbed chonkie (whitespace splitter from `install_stubs()`) — conftest does NOT un-stub chonkie because chunking is one-time ingest cost, not the measured query-time path.
- Calibrated thresholds will be loose (`p95 × 2` / `p95 × 3`) on purpose to absorb CI noise; the benefit is blocking large regressions, not tight SLAs.
- `test_real_model_search_cold_load_p90` measures N=10 iterations; p95 of N=10 would give `ceil(9.5) - 1 = index 9` = the maximum, which is too noisy. The cold-load uses p90 instead: `ceil(0.90 × 10) - 1 = index 8` = 9th of 10 values. The test and metric are named `p90` to reflect this accurately.
- Single-query steady-state benchmark does not exercise embedding batching (ingest path regression class); this is accepted per the brief.

---

## Architecture

### New/modified files
- `archon_search/eval/runner.py` — add `BenchmarkThresholds` dataclass + `load_benchmark_thresholds(path: Path) -> BenchmarkThresholds`
- `tests/eval/live_thresholds.toml` — add `[real_model_search]` section (placeholder values; filled in post-calibration)
- `pyproject.toml` — add `live_benchmark` marker entry
- `tests/eval/live_benchmark/conftest.py` — new; undoes stub patching, resets thread env vars, adds model-cache skip hook
- `tests/eval/live_benchmark/test_real_model_search_benchmark.py` — new; two benchmark tests
- `.github/workflows/archon-search-pr.yml` — three changes: add `not live_benchmark` to default step, add cache + prefetch step, add dedicated benchmark step

### Data flow
1. CI restores HF + fastembed model cache (key: fixed model-name-based string; not `uv.lock` hash)
2. Prefetch step downloads models with 3 retries, exponential backoff; fails hard if all attempts fail
3. `pytest -m live_benchmark` step:
   a. `tests/eval/live_benchmark/conftest.py` removes fastembed stubs and resets thread counts
   b. Module-scoped fixture ingests `tests/eval/corpus/code/` (23 documents) into a temp LanceDB store using real `ModelEmbedder` + `ModelReranker`
   c. Steady-state test warms 5 iterations, measures 100 iterations of `pipeline.search()` via `time.perf_counter()`
   d. Cold-load test constructs fresh embedder + reranker N=10 times, measures first `embed()` + `predict()` latency; p90 (index 8 of 10) is computed and asserted
4. Steady-state test asserts p95 ≤ `steady_state_p95_ms`; cold-load test asserts p90 ≤ `cold_load_p90_ms`; both loaded from `[real_model_search]` in `live_thresholds.toml`

### New config keys
`tests/eval/live_thresholds.toml` — new section:
```toml
[real_model_search]
steady_state_p95_ms = 999999.0  # CALIBRATE: replace with CI_measured_p95 × 2
cold_load_p90_ms    = 999999.0  # CALIBRATE: replace with CI_measured_p90 × 3
```
Placeholder values of `999999.0` allow the gate to pass before calibration. Replace with calibrated values (CI ubuntu-latest `workflow_dispatch`) before declaring the gate active.

Note on `cold_load_p90_ms` naming: the steady-state test uses `sorted(times)[int(math.ceil(0.95 * len(times))) - 1]` (p95). For N=10 this would give `ceil(9.5) = 10`, index 9 = maximum — overfitting to a single outlier. The cold-load test therefore targets p90 instead, using `sorted(times)[int(math.ceil(0.90 * _COLD_LOAD_ITERS)) - 1]` — for N=10: `ceil(9.0) - 1 = 8` = index 8 (p90 under nearest-rank). The TOML key and all test references use `p90` to accurately reflect what is measured.

### New API
```python
# archon_search/eval/runner.py

@dataclass
class BenchmarkThresholds:
    steady_state_p95_ms: float
    cold_load_p90_ms: float  # named p90: N=10 nearest-rank gives index 8 = 90th percentile

def load_benchmark_thresholds(path: Path) -> BenchmarkThresholds:
    """Parse [real_model_search] from *path* into BenchmarkThresholds.

    Raises ValueError if: file absent, TOML invalid, [real_model_search] section
    missing, required keys missing, values not float-coercible, or any value ≤ 0.
    """
```

---

## Task breakdown

### Phase 1 — Marker + Threshold Infrastructure
> **Releasable**: after Task 1.3; infrastructure in place but no benchmark tests yet.

#### Task 1.1 — Register `live_benchmark` marker in `pyproject.toml`
- [x] **File**: `pyproject.toml`
- **Depends on**: nothing
- **Description**:
  - Add entry to `[tool.pytest.ini_options].markers` list:
    `"live_benchmark: real-model latency benchmark; requires fastembed model cache; serialised via xdist_group('live_benchmark'); excluded from default suite"`
  - **Add `not live_benchmark` to `addopts`** in `pyproject.toml` so that `uv run pytest` (the default developer run) never loads the live_benchmark conftest. This is required because the module-level `sys.modules.pop` in `tests/eval/live_benchmark/conftest.py` runs before any skip fixture fires — with xdist workers this would poison `sys.modules` for regular tests. The model-cache skip hook in the conftest (Task 2.1) is defense-in-depth only, not the primary exclusion mechanism.
    - If `addopts` has no existing `-m` expression, add one: `-m "not live_benchmark"`. If it does, extend it with `and not live_benchmark`.
  - **Update `CLAUDE.md`** (project-level, checked into the repo): the line "Default pytest run includes **all** markers — no `-m` exclusion filter in `addopts`" must be updated to note that `live_benchmark` is excluded from `addopts` by design (module-level side-effects require process-level isolation, not just marker-based skip logic).
- **Releasable**: after this task, `@pytest.mark.live_benchmark` is a registered marker and `--strict-markers` will not reject it; `uv run pytest` never loads the live_benchmark conftest.
- **Tests (TDD)** — no unit test required; verified by `pytest --collect-only` not emitting `PytestUnknownMarkWarning`.
- **Checkpoint**: `uv run pytest --collect-only -q 2>&1 | grep -c "PytestUnknownMarkWarning"` → `0`

---

#### Task 1.2 — `BenchmarkThresholds` dataclass + `load_benchmark_thresholds()` in `archon_search/eval/runner.py`
- [x] **File**: `archon_search/eval/runner.py`
- **Depends on**: nothing
- **Description**:
  - Add `BenchmarkThresholds` dataclass immediately after the existing `EvalThresholds` dataclass (around line 72):
    ```python
    @dataclass
    class BenchmarkThresholds:
        """Real-model latency benchmark thresholds loaded from live_thresholds.toml."""
        steady_state_p95_ms: float
        cold_load_p90_ms: float  # p90: N=10 nearest-rank gives index 8 = 90th percentile
    ```
  - Add `load_benchmark_thresholds(path: Path) -> BenchmarkThresholds` after `load_thresholds()`. It must:
    - Raise `ValueError("Benchmark thresholds file not found: {path}")` if `path` does not exist
    - Raise `ValueError("Invalid TOML in {path}: {exc}")` on TOML parse error
    - Raise `ValueError("[real_model_search] section missing in {path}")` if section absent
    - Raise `ValueError("steady_state_p95_ms missing in [real_model_search]")` / similar for each missing key (including `cold_load_p90_ms`)
    - Raise `ValueError("steady_state_p95_ms must be a positive number, got {val}")` if value ≤ 0 or not numeric
    - On success return `BenchmarkThresholds(steady_state_p95_ms=..., cold_load_p90_ms=...)`
  - Export `BenchmarkThresholds` and `load_benchmark_thresholds` from the module (they are importable; no `__all__` change needed since the module has none).
- **Releasable**: after this task, `load_benchmark_thresholds()` is callable from tests and conftest.
- **Tests (TDD)** — `tests/eval/test_runner.py` (extend existing file):
  - Unit: `test_load_benchmark_thresholds_valid` — writes a TOML with `[real_model_search]` section, both keys set to positive floats; asserts returned `BenchmarkThresholds` has correct values
  - Unit: `test_load_benchmark_thresholds_missing_file` — nonexistent path raises `ValueError` mentioning the path
  - Unit: `test_load_benchmark_thresholds_invalid_toml` — malformed TOML content raises `ValueError`
  - Unit: `test_load_benchmark_thresholds_missing_section` — valid TOML without `[real_model_search]` raises `ValueError` mentioning the section name
  - Unit: `test_load_benchmark_thresholds_missing_key` — section present, `cold_load_p90_ms` absent; raises `ValueError` mentioning the missing key
  - Unit: `test_load_benchmark_thresholds_zero_value` — `steady_state_p95_ms = 0.0` raises `ValueError` (zero is not positive)
  - Unit: `test_load_benchmark_thresholds_negative_value` — negative float raises `ValueError`
  - Unit: `test_load_benchmark_thresholds_non_numeric` — string value raises `ValueError`
- **Checkpoint**: `uv run pytest tests/eval/test_runner.py -k "benchmark_thresholds" -v`

---

#### Task 1.3 — Add `[real_model_search]` section to `tests/eval/live_thresholds.toml`
- [x] **File**: `tests/eval/live_thresholds.toml`
- **Depends on**: Task 1.2
- **Description**:
  - Append the new section below the existing header comment. Use `999999.0` as placeholder values so the gate trivially passes until calibrated. Include a comment block above the section:
    ```toml
    # [real_model_search] — real-model (fastembed BAAI/bge-small-en-v1.5 + Xenova/ms-marco-MiniLM-L-6-v2)
    # latency thresholds for tests/eval/live_benchmark/test_real_model_search_benchmark.py.
    #
    # Calibration procedure:
    #   1. Run `workflow_dispatch` on archon-search-pr.yml (or a dedicated calibration workflow)
    #      on ubuntu-latest 10 times. Record the p95/p90 printed by the benchmark step.
    #   2. Set steady_state_p95_ms = CI_measured_p95 × 2
    #      Set cold_load_p90_ms    = CI_measured_p90 × 3
    #   3. Add a provenance comment: date, runner type, 10 raw samples.
    #   4. Replace the 999999.0 placeholders below.
    #
    # Calibration MUST be done on ubuntu-latest; darwin/aarch64 ONNX is 2-5× faster.
    [real_model_search]
    steady_state_p95_ms = 999999.0  # CALIBRATE_ME: CI_measured_p95 × 2
    cold_load_p90_ms    = 999999.0  # CALIBRATE_ME: CI_measured_p90 × 3  (p90: N=10 nearest-rank)
    ```
  - Verify `load_benchmark_thresholds(Path("tests/eval/live_thresholds.toml"))` returns a valid `BenchmarkThresholds` with both values = 999999.0.
- **Releasable**: after this task, the threshold file is parseable and the benchmark tests can load thresholds.
- **Tests (TDD)** — `tests/eval/test_runner.py`:
  - Integration: `test_load_benchmark_thresholds_from_live_thresholds_toml` — calls `load_benchmark_thresholds(Path("tests/eval/live_thresholds.toml"))`, asserts both values are > 0 (does not assert exact values to survive calibration updates)
- **Checkpoint**: `uv run pytest tests/eval/test_runner.py -k "live_thresholds_toml" -v`

---

### Phase 2 — Live Benchmark Test Directory
> **Releasable**: after Task 2.3; both benchmark tests can be run locally with `uv run pytest -m live_benchmark --no-cov` when the model cache is present.

#### Task 2.1 — `tests/eval/live_benchmark/conftest.py`: stub removal, thread reset, model-cache skip hook
- [x] **File**: `tests/eval/live_benchmark/conftest.py` (new file; creates the `tests/eval/live_benchmark/` directory implicitly via the empty `__init__.py` in Task 2.2)
- **Depends on**: Task 1.1
- **Description**:
  - Module-level (not inside a fixture) — remove all three fastembed stub entries from `sys.modules` before any test import can resolve them:
    ```python
    import sys
    for _key in ("fastembed", "fastembed.rerank", "fastembed.rerank.cross_encoder"):
        sys.modules.pop(_key, None)
    ```
    Must run at module level so that when pytest imports the test file, `from fastembed import TextEmbedding` resolves to the real package, not the stub.
  - Module-level — reset all ML thread-count env vars to production defaults using direct assignment (not `setdefault`, to override values from root conftest and `install_stubs()`):
    ```python
    import os
    _cpu = str(os.cpu_count() or 4)
    for _var in ("ORT_NUM_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                 "MKL_NUM_THREADS", "RAYON_NUM_THREADS"):
        os.environ[_var] = _cpu
    os.environ.pop("TOKENIZERS_PARALLELISM", None)
    ```
    Must run at module level before any ONNX or fastembed import.
  - **Function-scoped** autouse fixture `_activate_deterministic_eval_backends` that shadows the parent `tests/eval/conftest.py` autouse fixture of the same name (which sets `ARCHON_SEARCH_EVAL_BACKENDS=1`). The parent is function-scoped (it uses `monkeypatch`). The shadow MUST also be function-scoped — a scope mismatch (e.g., adding `scope="session"`) would cause pytest to treat it as a different fixture and not shadow the parent. Without this shadow, the parent fixture would activate deterministic stubs for all benchmark tests, breaking the real-model pipeline construction and causing `_build_pipeline_with_eval_backends(backend='live')` to raise `RuntimeError`. Match the pattern in `tests/eval/live/conftest.py`:
    ```python
    @pytest.fixture(autouse=True)
    def _activate_deterministic_eval_backends() -> None:
        pass  # shadow parent (function-scoped); live_benchmark must NOT activate deterministic stubs
    ```
  - Session-scoped autouse fixture `_require_model_cache` that skips the entire session if either model is absent from the fastembed cache. Must also respect the `FASTEMBED_CACHE_PATH` environment variable (which changes the cache location):
    ```python
    import os
    import pytest
    from pathlib import Path

    @pytest.fixture(autouse=True, scope="session")
    def _require_model_cache() -> None:
        cache_dir = Path(os.environ.get("FASTEMBED_CACHE_PATH", Path.home() / ".cache" / "fastembed"))
        missing = []
        if not any(cache_dir.glob("*bge-small*")):
            missing.append("BAAI/bge-small-en-v1.5")
        if not any(cache_dir.glob("*ms-marco-MiniLM*")):
            missing.append("Xenova/ms-marco-MiniLM-L-6-v2")
        if missing:
            pytest.skip(f"fastembed model cache absent for: {missing!r} — run the CI prefetch step first")
    ```
    This fixture is defense-in-depth. The primary exclusion from the default `uv run pytest` run is `not live_benchmark` in `addopts` (Task 1.1).
  - No `__init__.py` import needed — pytest discovers conftest files by directory, not by package.
  - **Note on module-level `os.environ` mutation**: setting `os.environ` at module level permanently mutates env vars for the process lifetime. Because the CI benchmark step runs as a separate `pytest` invocation (never co-collected with default suite tests), this is safe in CI. Developers who run `uv run pytest -m live_benchmark` should be aware that env vars remain changed for the process lifetime. For a cleaner approach in the future, these resets could be moved into a session-scoped fixture using `monkeypatch.setenv`, but for the initial implementation module-level is acceptable given the CI isolation guarantee.
- **Releasable**: after this task, the conftest is active and prevents stub leakage and thread throttling from the parent conftest when running `live_benchmark` tests.
- **Tests (TDD)**:
  - Unit: `test_live_benchmark_conftest_removes_fastembed_stubs` — import `sys`; verify that after conftest loads, `sys.modules.get("fastembed")` is either absent or points to the real package (not the `_FakeTextEmbedding`-based stub). Run in isolation to avoid contaminating other tests. Can be a simple assertion in the conftest itself wrapped in a comment, or a separate test that imports and checks. Prefer a targeted test in `tests/eval/live_benchmark/test_real_model_search_benchmark.py` that asserts `type(fastembed.TextEmbedding).__module__` does not contain `"_search_stubs"`.
  - Note: Thread-count env var resets are verified implicitly by the benchmark tests producing sensible latencies; there is no isolated unit test for env var values.
- **Checkpoint**: `uv run pytest tests/eval/live_benchmark/ --collect-only -q` — should either collect tests (if model cache present) or skip gracefully (if absent), with no `PytestUnknownMarkWarning`.

---

#### Task 2.2 — Module-scoped ingest fixture + `test_real_model_search_steady_state_p95`
- [x] **File**: `tests/eval/live_benchmark/test_real_model_search_benchmark.py` (new file) + `tests/eval/live_benchmark/__init__.py` (empty new file)
- **Depends on**: Task 2.1, Task 1.2, Task 1.3
- **Description**:
  - Create empty `tests/eval/live_benchmark/__init__.py` so pytest treats the directory as a package (consistent with rest of `tests/eval/`).
  - Create `test_real_model_search_benchmark.py` with:
    - Module-level constants:
      ```python
      _STEADY_STATE_WARMUP = 5
      _STEADY_STATE_ITERS = 100
      _BENCHMARK_QUERY = "async HTTP client with retry logic and timeout"  # queries.jsonl entry #0
      _BENCHMARK_COLLECTION = "code"
      _TOP_K_RETRIEVE = 15
      _TOP_K_RETURN = 5
      ```
    - Module-scoped fixture `_live_pipeline_and_store` that:
      1. Creates a `tmp_path` via `tmp_path_factory.mktemp("live_bench")`
      2. Instantiates real embedder and reranker using the factory functions directly:
         `embedder = make_embedder("BAAI/bge-small-en-v1.5")` and `reranker = make_reranker("Xenova/ms-marco-MiniLM-L-6-v2")`. These factories return ready-to-use `Embedder` and `Reranker` instances — do NOT wrap again in `Embedder(backend=...)` / `Reranker(backend=...)`.
      3. Before implementing, check whether `_build_pipeline_with_eval_backends(db_path, backend='live')` from `archon_search/eval/runner.py` can serve as the basis for this fixture (add only the ingest step on top). Using the existing function avoids drift between the benchmark fixture and the eval runner pipeline construction.
      4. Creates a `SearchStore` at the temp path
      5. Creates a `SearchPipeline(store, embedder, reranker, chunker, parser, top_k_retrieve=_TOP_K_RETRIEVE, top_k_return=_TOP_K_RETURN)` using production `DocumentChunker` and `DocumentParser`
      6. All async operations (connect + ingest + assertion) MUST use a single `asyncio.run()` call encompassing the entire async lifecycle. Do NOT use separate `asyncio.run()` calls for connect, ingest, or validation — each creates a new event loop, and LanceDB's internal connection is event-loop-bound. A second `asyncio.run()` on the same `store` object will fail with a `RuntimeError`.
         Suggested pattern: `asyncio.run(_setup_pipeline(tmp_path))` where `_setup_pipeline` is an `async def` that does connect + ingest + validates the chunk count, and returns `(pipeline, store)`. The chunk count assertion must also live inside `_setup_pipeline`:
         ```python
         chunk_count = await store.count_chunks(_BENCHMARK_COLLECTION, DEFAULT_NAMESPACE)
         assert chunk_count > 0, f"Ingest produced zero chunks in {_BENCHMARK_COLLECTION!r}"
         ```
         Note: use `store.count_chunks(collection, namespace)` (the `SearchStore` method) — `store.count_rows()` does not exist on `SearchStore`.
      7. Yields `(pipeline, embedder)` — both needed for the search call (search requires the embedder instance)
      8. Tears down: calls `await store.disconnect()` (the `SearchStore` async close method) inside a final `asyncio.run(_teardown(store))`, or in a `try/finally` block within `_setup_pipeline` using `yield` if using an `async` generator fixture
    - `test_real_model_search_steady_state_p95` test:
      - **MUST be a plain `def` function (not `async def`)** because it calls `asyncio.run()` internally. pytest-asyncio's `asyncio_mode = "auto"` in `pyproject.toml` injects an event loop into `async def` tests, making `asyncio.run()` raise `RuntimeError: cannot be called from a running event loop`.
      - Markers: `@pytest.mark.live_benchmark` and `@pytest.mark.xdist_group("live_benchmark")`
      - Loads thresholds via `load_benchmark_thresholds(Path("tests/eval/live_thresholds.toml"))`
      - Runs `_STEADY_STATE_WARMUP` iterations (unmeasured) calling:
        ```python
        asyncio.run(pipeline.search(
            _BENCHMARK_QUERY, _BENCHMARK_COLLECTION,
            embedder=embedder, rag_fusion=False, filters=None
        ))
        ```
        Note: `pipeline.search()` signature is `search(query, collection, namespace=DEFAULT_NAMESPACE, *, embedder, ...)`. Pass `query` and `collection` positionally. Do NOT use `collection=` as a keyword arg.
      - Wrap each warm-up iteration in try/except and re-raise with a descriptive message if it fails: `f"Warm-up iteration {i} failed: {exc}; check ONNX session initialization"`
      - Measures `_STEADY_STATE_ITERS` iterations using `time.perf_counter()` (wall-clock)
      - Computes p95 from the list of elapsed milliseconds using `import math` and `sorted(times)[int(math.ceil(0.95 * len(times))) - 1]`
      - Asserts `p95 <= thresholds.steady_state_p95_ms` with failure message: `f"Steady-state p95 {p95:.1f} ms exceeds threshold {thresholds.steady_state_p95_ms:.1f} ms"`
  - The assertion stub check: add one assertion that `fastembed.TextEmbedding` is not the stub class (its `__module__` must not contain `"_search_stubs"`). This verifies the conftest stub removal worked.
- **Releasable**: after this task, the steady-state benchmark is runnable locally with model cache present.
- **Tests (TDD)**:
  - The test itself is the TDD artifact. Write it test-first, then implement the fixture.
  - Unit: `test_stub_not_active_in_live_benchmark` — import `fastembed`; assert `"_search_stubs"` not in `fastembed.TextEmbedding.__module__`
  - Unit: `test_steady_state_p95_assertion_fires_on_regression` — creates a pre-computed `times` list where the p95 (using the `math.ceil` formula) exceeds a known threshold; asserts that calling the p95 assertion logic raises `AssertionError` with the expected message format. This validates the formula and comparison logic without running real models.
  - Integration (the benchmark test): `test_real_model_search_steady_state_p95` as described above — asserts p95 ≤ threshold
- **Checkpoint**: `uv run pytest tests/eval/live_benchmark/test_real_model_search_benchmark.py::test_real_model_search_steady_state_p95 -v --no-cov` (requires model cache; will skip otherwise)

---

#### Task 2.3 — `test_real_model_search_cold_load_p90`
- [x] **File**: `tests/eval/live_benchmark/test_real_model_search_benchmark.py`
- **Depends on**: Task 2.2
- **Description**:
  - Add to the existing test file:
    - Module-level constant: `_COLD_LOAD_ITERS = 10`
    - `test_real_model_search_cold_load_p90` test:
      - **MUST be a plain `def` function (not `async def`)** because it calls `asyncio.run()` internally. pytest-asyncio's `asyncio_mode = "auto"` in `pyproject.toml` injects an event loop into `async def` tests, making `asyncio.run()` raise `RuntimeError: cannot be called from a running event loop`.
      - Markers: `@pytest.mark.live_benchmark` and `@pytest.mark.xdist_group("live_benchmark")`
      - Does NOT use the module-scoped pipeline fixture (each iteration constructs fresh backends)
      - For each of `_COLD_LOAD_ITERS` iterations:
        1. Record `start = time.perf_counter()`
        2. Construct fresh embedder and reranker using factory functions directly: `embedder = make_embedder("BAAI/bge-small-en-v1.5")` and `reranker = make_reranker("Xenova/ms-marco-MiniLM-L-6-v2")`
        3. Call `asyncio.run(embedder.embed(["warm"]))` — triggers ONNX session creation. Note: `Embedder` has both `embed(texts: list[str])` and `embed_one(text: str)`. Use `embed(["warm"])` here to match the production batched-embed code path.
        4. Call `reranker._backend.predict([("warm", "warm")])` — triggers reranker ONNX session creation. Note: `ModelReranker.predict()` is a **synchronous** method; do NOT wrap in `asyncio.run()`. Do NOT use `reranker.rerank_candidates("warm", [])` — the empty candidates list causes `predict()` to be skipped entirely via a short-circuit.
        5. Record `elapsed_ms = (time.perf_counter() - start) * 1000`
        6. Append `elapsed_ms` to `times`
      - Compute p90 from `times`: `sorted(times)[int(math.ceil(0.90 * _COLD_LOAD_ITERS)) - 1]` (for N=10: `ceil(9.0) - 1 = 8` = index 8 = p90 under nearest-rank)
      - Load thresholds via `load_benchmark_thresholds(Path("tests/eval/live_thresholds.toml"))`
      - Assert `p90 <= thresholds.cold_load_p90_ms` with message: `f"Cold-load p90 {p90:.1f} ms exceeds threshold {thresholds.cold_load_p90_ms:.1f} ms"`
  - Note: the metric variable is named `p90` throughout. The TOML key, dataclass field, and assertion message all use `p90`. The formula uses `math.ceil(0.90 * N)`, not `0.95 * N` — using 0.95 truncated coincidentally gave the same index for N=10, but fails for other values of N.
- **Releasable**: after this task, both benchmark tests are implemented and can be run locally.
- **Tests (TDD)**:
  - Integration (the benchmark test): `test_real_model_search_cold_load_p90` as described above — asserts p90 ≤ threshold
  - Unit: `test_cold_load_p90_assertion_fires_on_regression` — creates a pre-computed `times` list (N=10) where index 8 exceeds a known threshold; asserts that the p90 assertion logic raises `AssertionError` with the expected message format. This validates the formula and comparison logic without running real models.
- **Checkpoint**: `uv run pytest tests/eval/live_benchmark/test_real_model_search_benchmark.py::test_real_model_search_cold_load_p90 -v --no-cov` (requires model cache)

---

### Phase 3 — CI Pipeline
> **Releasable**: after Task 3.1; the PR gate is active. Calibrate thresholds (Task 3.2) to make it meaningful.

#### Task 3.1 — Update `archon-search-pr.yml`: marker exclusion + cache + prefetch + benchmark step
- [x] **File**: `.github/workflows/archon-search-pr.yml`
- **Depends on**: Task 2.3
- **Description**:
  - **Change 1** — add `and not live_benchmark` to the default test step's `-m` marker filter:
    ```yaml
    run: uv run pytest ... -m "not live and not eval and not benchmark and not integration and not live_eval and not live_benchmark"
    ```
  - **Change 2** — add a cache restore + prefetch step **after** `Clean install` and **before** the default suite step:
    ```yaml
    - name: Restore fastembed + HuggingFace model cache
      uses: actions/cache@v4
      with:
        path: |
          ~/.cache/fastembed
          ~/.cache/huggingface
        # Update the key suffix (v1→v2 etc.) if model names change.
        key: model-cache-bge-small-en-v1.5-ms-marco-MiniLM-L-6-v2-v1
        restore-keys: model-cache-bge-small-en-v1.5-ms-marco-MiniLM-L-6-v2-

    - name: Prefetch models (3 attempts, exponential backoff)
      run: |
        prefetch_ok=false
        for attempt in 1 2 3; do
          uv run python -c "
        from fastembed import TextEmbedding
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        TextEmbedding('BAAI/bge-small-en-v1.5')
        TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')
        print('Models prefetched')
        " && { prefetch_ok=true; break; }
          echo "Attempt $attempt failed; retrying in $((attempt * 5))s..."
          sleep $((attempt * 5))
        done
        if [ "$prefetch_ok" != "true" ]; then
          echo "All prefetch attempts failed"; exit 1
        fi
    ```
    Note: the prefetch step uses `uv run python -c "..."` (not bare `python`) because CI uses a uv-managed virtualenv where bare `python` is the system Python without fastembed installed. The flag variable pattern is required because `done || { ... }` does NOT work — the loop body always exits 0 via `sleep`, so the `||` branch is never taken even when all attempts fail.

    Note on cache key: the key is based on model name strings, not `uv.lock` hash. Changing uv.lock (e.g., bumping an unrelated dep) would invalidate the model cache unnecessarily. Only update the key when model names change.
  - **Change 3** — add dedicated `live_benchmark` step **after** the integration step and **before** coverage enforcement:
    ```yaml
    - name: Run real-model latency benchmark
      timeout-minutes: 3
      run: uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 -m live_benchmark --junitxml=benchmark-results.xml tests/eval/live_benchmark/

    - name: Verify benchmark tests ran (not just skipped)
      run: uv run python -c "import xml.etree.ElementTree as ET; t=ET.parse('benchmark-results.xml').getroot(); tests=int(t.attrib.get('tests',0)); skips=int(t.attrib.get('skipped',0)); assert tests-skips>0, f'All {tests} benchmark tests were skipped or none ran — check model cache prefetch step'"
    ```
    Uses `-n0` (serial; `xdist_group` is sufficient for intra-step serialisation, and the step is already isolated). Uses `--no-cov` because coverage instrumentation adds per-call overhead (~0.1ms) that biases latency measurements upward and would produce misleading threshold calibration. Coverage for `archon_search/eval/runner.py` and related modules is already collected by prior test steps. The verification step after ensures tests actually executed rather than silently skipping due to a missing cache.
- **Releasable**: after this task, every PR build runs the benchmark and the gate is active (though with placeholder thresholds until calibration).
- **Tests (TDD)**: CI workflow changes are verified by a successful PR build — no unit test possible. Verify locally with `uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov` (skips if no cache).
- **Checkpoint**: open a draft PR and confirm the `Run real-model latency benchmark` step appears in CI output and passes (with `999999.0` placeholder thresholds).

---

#### Task 3.2 — Calibrate thresholds and fill in `live_thresholds.toml`
- [ ] **File**: `tests/eval/live_thresholds.toml`
- **Depends on**: Task 3.1
- **Description**:
  - Trigger `workflow_dispatch` on `archon-search-pr.yml` (or a temporary calibration PR) 10 times on ubuntu-latest. From each run, record the steady-state p95 and cold-load p90 printed in the benchmark step output.
  - Compute calibrated thresholds:
    - `steady_state_p95_ms = median_of_10_CI_runs × 2`
    - `cold_load_p90_ms = median_of_10_CI_runs × 3`
  - Replace the `999999.0` placeholder values in `[real_model_search]` with the calibrated values.
  - Add a provenance comment block immediately above the key values:
    ```toml
    # Calibrated: <YYYY-MM-DD>, ubuntu-latest, 10 runs.
    # Raw steady-state p95 samples (ms): [s1, s2, ..., s10], median=X
    # Raw cold-load p90 samples (ms): [c1, c2, ..., c10], median=Y
    ```
  - Run `uv run pytest tests/eval/live_benchmark/ -m live_benchmark --no-cov` locally to confirm the calibrated values produce a passing test.
- **Releasable**: after this task, the gate is meaningful — a real regression will fail the PR.
- **Tests (TDD)**: `test_load_benchmark_thresholds_from_live_thresholds_toml` from Task 1.3 continues to pass.
- **Checkpoint**: `uv run pytest tests/eval/test_runner.py -k "live_thresholds_toml" -v`

---

### Phase 4 — Documentation

#### Task 4.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to update every documentation file affected by C16:
    - `Documentation/Architecture/200_testing_strategy.md`: add `live_benchmark` marker description; note that it runs in a dedicated CI step separate from the default suite; explain `xdist_group("live_benchmark")` serialisation; note model-cache skip behavior on developer machines.
    - `Documentation/Architecture/210_performance_and_scalability.md`: add section on real-model latency gating; explain the two thresholds (`steady_state_p95_ms`, `cold_load_p90_ms`); reference calibration procedure and `live_thresholds.toml`; note that stub-based benchmarks remain and cover different regression classes.
    - `tests/eval/README.md`: add `live_benchmark` section after the `live_eval` section; document: directory structure, conftest stub-removal contract, model-cache skip behavior, how to run locally, how to trigger calibration, how to update thresholds, what regressions the gate catches vs. what it does not catch.
    - `tests/eval/live_thresholds.toml` header comment: expand the existing two-line comment to describe both sections (`[quality_floors]` for live_eval, `[real_model_search]` for live_benchmark), explain report-only vs. gated behavior, and point to the calibration procedure.
    - `CLAUDE.md` (project-level): update the testing convention statement to note that `live_benchmark` is excluded from default `addopts` by design, with a one-line explanation (module-level sys.modules mutation requires process isolation). This is the one exception to the "all markers run by default" convention.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully shipped and documented.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` (full suite, no model cache) completes without `PytestUnknownMarkWarning` and with coverage ≥ 85%
  - `uv run pytest --collect-only -m live_benchmark` collects at least 2 tests (currently exactly 2: `test_real_model_search_steady_state_p95` and `test_real_model_search_cold_load_p90`) without error or warning
  - `uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov` either passes (model cache present) or skips gracefully (cache absent); never fails due to missing model cache
  - `uv run pytest tests/eval/test_runner.py -k "benchmark_thresholds or live_thresholds_toml" -v` passes — all 9 threshold-loader tests green
  - `tests/eval/live_thresholds.toml` parses as valid TOML with `[real_model_search]` section containing positive `steady_state_p95_ms` and `cold_load_p90_ms`
  - `.github/workflows/archon-search-pr.yml` default test step marker filter includes `not live_benchmark`; a dedicated `live_benchmark` step exists with `timeout-minutes: 3`
  - `Documentation/Architecture/200_testing_strategy.md`, `210_performance_and_scalability.md`, and `tests/eval/README.md` each contain a `live_benchmark` section or reference
  - `CLAUDE.md` testing convention paragraph acknowledges `live_benchmark` as the one marker excluded from `addopts`
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

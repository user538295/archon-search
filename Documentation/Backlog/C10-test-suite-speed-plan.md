# C10 — Test Suite Speed Optimization
**Purpose**: Reduce the default `uv run pytest` wall time from ~600s to ≤200s by enabling pytest-xdist parallelism and eliminating a 30s serial wait in one test.
**Audience**: Developers iterating locally; CI cost.
**Status**: To Do

## Baseline Measurement
<!-- Recorded during Task 1.1 pre-step: `time uv run pytest --no-cov --durations=20` (xdist not yet installed, -n0 unavailable) -->

```
=============================== slowest 20 durations =============================
30.01s call     tests/test_store.py::test_update_description_timeout_skips_write
18.42s call     tests/test_fixtures.py::TestThreePagePdfFixture::test_three_page_pdf_contains_expected_text
12.93s call     tests/test_pipeline.py::test_pipeline_ingest_directory_includes_png
11.84s call     tests/test_sync_e2e.py::TestS15_9_MissingPathNocrash::test_nonexistent_path_with_valid_path_ok
11.50s call     tests/test_pipeline.py::test_pipeline_ingest_is_idempotent
11.35s call     tests/test_pipeline.py::test_ingest_centroid_replaced_on_reingest
10.43s call     tests/test_sync_e2e.py::TestS15_3_FileModifiedIncrementalUpdate::test_sync_reindexes_on_file_change
10.16s call     tests/test_pipeline.py::test_pipeline_ingest_file_parse_error_preserves_existing_chunks
 9.91s call     tests/test_pipeline.py::test_ingest_directory_sets_active_embedding_model_for_new_collection
 9.90s call     tests/test_pipeline.py::test_pipeline_ingest_directory_rebuilds_fts_once
 9.89s call     tests/test_pipeline.py::test_pipeline_ingest_directory
 9.81s call     tests/test_pipeline.py::test_ingest_directory_on_file_complete_called_per_file
 9.78s call     tests/test_pipeline.py::test_ingest_directory_exclude_paths_adjusts_total
 9.76s call     tests/test_pipeline.py::test_ingest_directory_exclude_paths_skips_files
 9.76s call     tests/test_pipeline_acl.py::test_ingest_directory_skips_acl_sidecar_files
 9.73s call     tests/test_pipeline.py::test_pipeline_ingest_directory_partial_file_failure_continues
 9.72s call     tests/test_pipeline.py::test_pipeline_ingest_directory_skips_binary_extensions
 9.71s call     tests/test_pipeline.py::test_ingest_computes_centroid_from_all_chunks
 9.69s call     tests/test_sync_e2e.py::TestS15_5_ChunkSizeChangedFullReindex::test_sync_reindexes_on_chunk_size_change
 9.68s call     tests/test_pipeline.py::test_ingest_directory_namespace_param

=== 3681 passed, 1 skipped, 184 deselected, 7 warnings in 616.69s (0:10:16) ===
uv run pytest --no-cov --durations=20  wall time: 10m 23s
```

---

## Background

The default `uv run pytest` run is estimated at ~600s — long enough to discourage running the full suite and encourage `--no-cov` shortcuts. Two root causes: (1) `test_update_description_timeout_skips_write` deliberately waits 30s for a lock timeout without using the monkeypatch pattern that 6 other tests in the suite already use; (2) the entire suite runs serially despite being parallelizable.

An exhaustive audit (all 201 test files in the default run scope) found exactly one xdist-unsafe fixture: `three_page_pdf` in `tests/conftest.py` writes to a fixed repo path and races when `test_fixtures.py` and `test_parser.py` land on different workers. All other shared state (module-scoped `connected_store`, `sys.modules` swaps in `test_mcp.py`, `test_app.py` key generation) is safe under `--dist=loadfile`.

## Goal

`uv run pytest` (no flags) completes in ≤200s on an 8-core developer machine while preserving the `--cov-fail-under=85` gate, all existing markers, and identical behavior under serial execution (`-n0`). If the measured baseline minus the tail-worker floor exceeds 200s (indicating the target requires splitting large test files beyond this scope), the 200s target is adjusted to the actual measured improvement; the goal is a significant speedup, not a specific number.

---

## Scope

### In Scope
- Measure baseline timing before any change (`time uv run pytest --no-cov -n0`)
- Fix `three_page_pdf` fixture: convert from fixed repo path to `tmp_path_factory`
- Fix `test_update_description_timeout_skips_write`: add `monkeypatch` and reduce lock timeout from 30s to 0.1s
- Add `pytest-xdist` to `[dependency-groups] dev` and add `-n auto --dist=loadfile` to `addopts`
- Add defensive `-n0` to both CI workflows' reconstructed flag strings
- Update `CLAUDE.md`, `Documentation/Architecture/200_testing_strategy.md`, `Documentation/Architecture/500_development_workflows_and_conventions.md`, `Documentation/quick_start.md`, and `contributing.md`

### Out of Scope
- `asyncio_default_fixture_loop_scope = "module"` (dangling-coroutine `RuntimeWarning`s make this risky; deferred)
- Shared-collection fixture refactor for file-filtering tests (deferred; xdist win makes it unnecessary now)
- Fixing CWD-relative `Path("tests/fixtures/...")` in `test_code_enricher.py` (safe in practice; deferred)
- Fixing `/tmp/reindex_*.md` hardcoded paths in `test_store_reindex_metadata.py` integration tests (excluded from default run)
- Fixing `test_baseline_contract.py` source-file write (excluded by `@pytest.mark.eval`)
- Any changes to test logic, coverage thresholds, or marker configuration

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task F.1 — Final verification & documentation update].

---

## What does NOT change
- `--cov-fail-under=85` gate
- All pytest markers and their exclusion from the default run
- Test assertions, test logic, coverage thresholds
- Serial execution semantics (identical results under `-n0`)
- CI eval gate run (already uses `-o addopts=`; unaffected)

---

## Known limitations / accepted trade-offs
- `test_store.py` (~303 tests) and `test_pipeline.py` (~223 tests) each pin a single worker due to `--dist=loadfile`. Their combined runtime sets the floor — the 150–200s estimate assumes their serial runtime is not the dominant bottleneck. The baseline measurement (Task 1.1) will confirm.
- The `--dist=loadfile` choice slightly underutilizes cores for files with many fast tests, but is required to preserve `module`-scoped `connected_store` fixture semantics.
- **Warning**: never run `uv run pytest -m eval -n auto` — `test_baseline_contract.py` writes to `archon_search/eval/metrics.py` (actual source file) and would corrupt it under parallelism.
- Rollback: remove `-n auto --dist=loadfile` from `addopts` in `pyproject.toml`. If intermittent failures appear that pass with `-n0` but fail with `-n auto`, bisect by running the suspect file with `-n auto` to isolate the race.
- The 200s target is aspirational and machine-dependent. If `test_store.py` or `test_pipeline.py` alone exceed 200s serial, the target cannot be met without splitting those files (out of scope). The feature is still shipped — any improvement over serial execution is a win.

---

## Architecture

No production code changes. All changes are in test infrastructure and configuration:

- **`tests/conftest.py`**: `three_page_pdf` fixture changes from writing to a fixed `_PDF_FIXTURE_PATH` to using `tmp_path_factory.mktemp("pdfs")`. Each xdist worker gets its own session-scoped temp directory — correct, because `session`-scoped fixtures are instantiated once per worker under xdist.
- **`tests/test_store.py`**: `test_update_description_timeout_skips_write` gains a `monkeypatch` parameter and sets `INGEST_LOCK_TIMEOUT_S = 0.1` to avoid the 30s real wait. Follows the exact pattern used at lines 4420 and 6051 of the same file.
- **`pyproject.toml`**: `pytest-xdist` added to `[dependency-groups] dev`; `-n auto --dist=loadfile` appended to `addopts` in `[tool.pytest.ini_options]`.
- **CI workflows**: `-n0` added to the reconstructed `pytest` flag strings in both `archon-search-release.yml` and `archon-search-pr.yml`. Two purposes: (1) required for coverage correctness — CI uses multi-step `--cov-append` across separate pytest invocations, then calls `coverage report` directly on `.coverage` (no `coverage combine` step). With xdist active, each invocation's internal combine step could overwrite `.coverage` with only the current run's worker shards, silently dropping coverage from prior invocations; (2) defensive — prevents xdist from activating if the `-o addopts=` override is ever removed.

---

## Task breakdown

### Phase 1 — Prerequisite safety fixes
> **Releasable**: after Task 1.3. pytest-xdist is installed and the two unsafe fixtures are patched. xdist is NOT yet active in the default run — that happens in Phase 2.

#### Task 1.1 — Install pytest-xdist dependency
- [x] **File**: `pyproject.toml`, `uv.lock`
- **Depends on**: nothing
- **Description**:
  - Add `pytest-xdist` to `[dependency-groups] dev` in `pyproject.toml` (alphabetically after `pytest-cov`).
  - Run `uv sync --dev` to update `uv.lock`.
  - Do NOT change `addopts` yet — that is Task 2.1. This task only installs the package so Phase 1 checkpoints can use `-n2`.
  - **Pre-step (no commit needed)**: before making any changes, record the baseline wall time by running `time uv run pytest --no-cov -n0 --durations=20`. Paste the output as a comment in this plan file under the `## Baseline Measurement` section at the top (after the header block). This baseline is required for the F.1 acceptance criterion "≤200s vs. Task 1.1 baseline".
- **Releasable**: after this task, xdist is importable and usable in test checkpoints.
- **Tests (TDD)**: `uv run python -c "import xdist; print('ok')"`.
- **Checkpoint**: `uv run python -c "import xdist; print(xdist.__version__)"`

---

#### Task 1.2 — Fix `three_page_pdf` session fixture for xdist safety
- [x] **File**: `tests/conftest.py`
- **Depends on**: Task 1.1 (checkpoint uses `-n2 --dist=loadfile`)
- **Description**:
  - Remove the module-level constant `_PDF_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pdfs" / "three_page.pdf"`.
  - Change the `three_page_pdf` fixture signature from `def three_page_pdf() -> Path:` to `def three_page_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:`.
  - Inside the fixture, replace `generate_three_page_pdf(_PDF_FIXTURE_PATH)` with:
    ```python
    pdf_path = tmp_path_factory.mktemp("pdfs") / "three_page.pdf"
    generate_three_page_pdf(pdf_path)
    return pdf_path
    ```
  - Delete `tests/fixtures/pdfs/three_page.pdf` if it exists (it is generated at test time and should not be committed; the `.gitignore` inside that directory guards against this). Delete `tests/fixtures/pdfs/.gitignore` — after this change no PDF is written to that directory, so the ignore rule is pointless. If the `tests/fixtures/pdfs/` directory is now empty, remove it too. Use `mv` to trash these files (not `rm`) per project convention:
    ```
    mv tests/fixtures/pdfs/.gitignore ~/.Trash/
    [ -f tests/fixtures/pdfs/three_page.pdf ] && mv tests/fixtures/pdfs/three_page.pdf ~/.Trash/ || true
    rmdir tests/fixtures/pdfs
    ```
    Note: `three_page.pdf` only exists if the test suite was previously run on this checkout; it is excluded from git via the `.gitignore` being deleted here.
  - Keep `scope="session"` — each xdist worker instantiates its own session, so each gets an isolated temp PDF (correct behavior).
  - No test assertion in `test_fixtures.py` or `test_parser.py` checks for a specific path — verified by audit.
- **Releasable**: after this task, `test_fixtures.py` and `test_parser.py` can safely run on different xdist workers.
- **Tests (TDD)** — `tests/test_fixtures.py`, `tests/test_parser.py`:
  - Regression: `uv run pytest tests/test_fixtures.py tests/test_parser.py -n2 --dist=loadfile --no-cov` — verifies both files pass concurrently on separate workers (the race condition that existed before).
  - Regression: `uv run pytest tests/test_fixtures.py tests/test_parser.py -n0 --no-cov` — verifies serial execution still passes (no regressions from the path change).
- **Checkpoint**: `uv run pytest tests/test_fixtures.py tests/test_parser.py -n2 --dist=loadfile --no-cov -v`

---

#### Task 1.3 — Fix `test_update_description_timeout_skips_write` (add monkeypatch)
- [ ] **File**: `tests/test_store.py`
- **Depends on**: nothing
- **Description**:
  - Locate `async def test_update_description_timeout_skips_write(tmp_path, caplog) -> None:` (line ~6191).
  - Add `monkeypatch` to the parameter list: `async def test_update_description_timeout_skips_write(tmp_path, caplog, monkeypatch) -> None:`.
  - Add at the top of the test body (before any other logic):
    ```python
    import archon_search.store as store_mod
    monkeypatch.setattr(store_mod, "INGEST_LOCK_TIMEOUT_S", 0.1)
    ```
  - This follows the identical pattern used at lines 4420 and 6051 in the same file (module-object form, not string form). `Final[float]` is not enforced at runtime — `monkeypatch.setattr` works as confirmed by existing usage.
  - The test should now complete in ~0.1s instead of ~30s.
- **Releasable**: after this task, the 30s serial wait is eliminated.
- **Tests (TDD)** — `tests/test_store.py`:
  - Timing: `time uv run pytest tests/test_store.py::test_update_description_timeout_skips_write --no-cov -v` — must complete in under 10s (was ~30s).
  - Correctness: same test must still pass (assertions unchanged, only the timeout shortened).
- **Checkpoint**: `time uv run pytest tests/test_store.py::test_update_description_timeout_skips_write --no-cov -v`

---

### Phase 2 — Enable parallelism
> **Releasable**: after Task 2.1. `uv run pytest` runs in parallel by default; `-n0` serial execution remains available.

#### Task 2.1 — Enable parallel addopts
- [ ] **File**: `pyproject.toml`
- **Depends on**: Task 1.1, Task 1.2, Task 1.3
- **Description**:
  - In `[tool.pytest.ini_options]`, append `-n auto --dist=loadfile` to the existing `addopts` string.
    - Current: `addopts = "--strict-markers --strict-config --cov=archon_search --cov-report=term-missing --cov-fail-under=85 -m 'not live and not eval and not benchmark and not integration and not live_eval'"`
    - After: same string with ` -n auto --dist=loadfile` appended before the closing quote.
  - Verify the full default suite passes: `uv run pytest`.
  - Record the wall time after the change (vs. the baseline from Task 1.1).
  - **Coverage combining**: no additional config needed — `pytest-cov` natively supports xdist; workers write `.coverage.workerN` files which the main process combines before applying `--cov-fail-under=85`. Note: this applies to single-invocation runs only. CI uses multi-step `--cov-append` across separate invocations and requires `-n0` to avoid xdist interference — see Task 3.1.
  - **Debugging notes** (verified by manual test, to be documented in F.1):
    - `-x` (fail-fast): first-failure isolation requires `-n0 -x` (xdist workers continue until their current test finishes).
    - `-s` (stdout passthrough): suppressed by xdist; requires `-n0 -s`.
- **Releasable**: after this task, `uv run pytest` runs in parallel and should meet the ≤200s target.
- **Tests (TDD)** — full suite:
  - Pass: `uv run pytest` — all tests pass, coverage gate passes.
  - Pass: `uv run pytest -n0` — identical results in serial mode.
  - Timing: `time uv run pytest --no-cov` — wall time ≤200s on an 8-core machine (use `--no-cov` only for the timing measurement, not the correctness check).
- **Checkpoint**: `uv run pytest && echo "PASS"`
- **Timing checkpoint**: `time uv run pytest --no-cov` (record and compare against Task 1.1 baseline)

---

### Phase 3 — CI hardening
> **Releasable**: after Task 3.1. Both CI pipelines are hardened against accidental xdist activation.

#### Task 3.1 — Add `-n0` to both CI workflow pytest commands
- [ ] **File**: `.github/workflows/archon-search-release.yml`, `.github/workflows/archon-search-pr.yml`
- **Depends on**: Task 1.1
- **Description**:
  - Both workflows already blank `addopts` via `-o addopts=`. The `-n0` addition has two purposes: (1) **required for coverage correctness** — CI uses multi-step `--cov-append` across separate pytest invocations, then calls `coverage report` directly on `.coverage` (no `coverage combine` step); with xdist active, each invocation's internal combine step could overwrite `.coverage` with only the current run's worker shards, silently dropping coverage from prior invocations; (2) **defensive** — prevents xdist from activating if the `-o addopts=` override is ever removed.
  - In `archon-search-release.yml` (line ~49): add `-n0` to the default-run `pytest` command. The command currently reconstructs all flags explicitly; append `-n0` to it.
  - In `archon-search-pr.yml` (line ~36): same change to the default-run `pytest` command.
  - Do NOT add `-n0` to the eval gate run or integration run — those are already separate commands with their own flags.
- **Releasable**: after this task, CI is hardened.
- **Tests (TDD)**: no unit tests — CI workflow correctness is verified by the CI run itself.
- **Checkpoint**: manually review both workflow files to confirm `-n0` appears only in the default-run step and not in the eval/integration steps.

---

### Final Phase — Verification & Documentation

#### Task F.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Task 1.1, Task 1.2, Task 1.3, Task 2.1, Task 3.1
- **Description**:
  - Spawn an agent to update every documentation file affected by this change:
    - **`CLAUDE.md` Common commands section**: add `-n0` override note, document that `-n0 -x` is required for fail-fast isolation, document that `-n0 -s` is required for stdout passthrough, note that release CI uses `-n0` explicitly.
    - **`Documentation/Architecture/200_testing_strategy.md`**: add xdist to the default run tier description, explain `--dist=loadfile` and `module`-scoped fixture semantics, note coverage combining behavior, document `-n0` serial escape hatch.
    - **`Documentation/Architecture/500_development_workflows_and_conventions.md`**: add note on parallel-by-default behavior and `-n0` for debugging.
    - **`Documentation/quick_start.md`**: add note about parallel-by-default, `-n0` for serial debugging.
    - **`contributing.md`**: add note about parallel-by-default, `-n0` for serial debugging, and `-n0 -x` / `-n0 -s` for fail-fast and stdout.
  - Verify all acceptance criteria below are met before marking complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the implementation.
- **Acceptance criteria** (must all pass):
  - `time uv run pytest --no-cov` completes in ≤200s on an 8-core developer machine (measured, compared to Task 1.1 baseline) — OR demonstrates significant wall-time improvement per the Goal section's fallback clause if the 200s floor is set by file-pinned workers.
  - `uv run pytest` (with coverage) passes `--cov-fail-under=85`.
  - `uv run pytest -n0` produces identical pass/fail results to `uv run pytest`.
  - `uv run pytest tests/test_fixtures.py tests/test_parser.py -n2 --dist=loadfile --no-cov` passes without race condition.
  - `time uv run pytest tests/test_store.py::test_update_description_timeout_skips_write --no-cov` completes in under 10s.
  - All markers (`eval`, `integration`, `live`, `benchmark`, `live_eval`) continue to function (`uv run pytest -m eval --collect-only` exits 0).
  - Both CI workflow files contain `-n0` in their default-run pytest commands.
  - `CLAUDE.md`, `Documentation/Architecture/200_testing_strategy.md`, and `Documentation/Architecture/500_development_workflows_and_conventions.md` are updated.
  - `Documentation/quick_start.md` and `contributing.md` are updated.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

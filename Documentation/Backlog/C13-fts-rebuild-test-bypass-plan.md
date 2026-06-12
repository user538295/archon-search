# C13 — Bypass FTS-index rebuild in tests that don't query FTS
**Purpose**: Add `rebuild_fts: bool = True` to `SearchPipeline.ingest_directory()` and switch all eligible tests to `rebuild_fts=False`, eliminating concurrent Tantivy index-build I/O contention under xdist and bringing `uv run pytest` median wall time to ≤90s.
**Audience**: Developers iterating locally; CI unaffected (`-n0` has no xdist contention).
**Status**: To Do

---

## Background
After C12, full-suite wall time is median 127s (range 106–140s). The dominant cost is ~30 ingest-directory tests each taking ~35s under `-n auto` vs ~13s serial — a 2.7× slowdown from 14 xdist workers building concurrent Tantivy FTS indexes on the same disk.

`ingest_file` already has `rebuild_fts: bool = True`. `ingest_directory` lacks it; every call unconditionally rebuilds FTS even for tests that never exercise FTS search.

## Goal
`uv run pytest --no-cov` median wall time ≤90s on a ≥4-core machine. No production behavior change. Default-run coverage stays ≥85%.

---

## Scope

### In Scope
- Add `rebuild_fts: bool = True` to `SearchPipeline.ingest_directory()` (`pipeline.py`)
- Guard the FTS optimize/rebuild block (`pipeline.py:513-528`) under `if rebuild_fts and any(r.status == "ok" ...)`
- Add unit test mirroring `test_ingest_file_rebuild_fts_false_skips_optimize` in `tests/test_pipeline_ingest_fts.py`
- Switch the 2 eligible tests in `tests/test_pipeline_code_enricher.py` (lines 218, 248) to `rebuild_fts=False`
- Discover all test files calling `ingest_directory` with `grep -rl 'ingest_directory' tests/` before auditing
- Per-test audit and switch of eligible `ingest_directory` calls in `tests/pipeline/test_pipeline_ingest.py`
- Confirm via grep that every switched test contains no call to `pipeline.search`, `pipeline.search_with_context`, `pipeline.search_many`, `store.hybrid_search`, or `store.full_text_search`
- Run the C12 5-run stability gate and record new wall-time median

### Out of Scope
- Any change to `ingest_file` (already has the parameter)
- Production callers (HTTP/MCP/CLI/sync)
- LanceDB version bump or FTS engine swap
- Test ordering, `pytest-randomly`, RAM-disk, `asyncio_default_fixture_loop_scope`
- External API documentation (parameter not exposed over HTTP/MCP/CLI)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change
- `ingest_file` signature (already has `rebuild_fts: bool = True`)
- Default behavior of `ingest_directory` for all production callers (`True` by default)
- Coverage gate (≥85% must remain passing)
- `tests/test_pipeline_ingest_directory_fts.py`, `tests/pipeline/test_pipeline_search.py`, `tests/test_pipeline_acl.py` — FTS-querying tests; all keep `rebuild_fts=True`
- `test_pipeline_ingest_directory_rebuilds_fts_once` and `test_pipeline_ingest_directory_all_failures_skips_fts_rebuild` — these directly test FTS rebuild behavior; must not be switched

---

## Known limitations / accepted trade-offs
- Silent degradation risk: `rebuild_fts=False` + a later `pipeline.search` call in the same test will pass with vector-only results (no error raised). The grep audit in Task 2.2 is the required mitigation — the 5-run gate does not catch this.
- Per-test audit may yield fewer than the maximum possible wins if some tests in `test_pipeline_ingest.py` have subtle FTS dependencies not visible via grep (e.g., a helper function that calls search). File-level caution is acceptable.
- `rebuild_fts` placement inconsistency: `ingest_file` has `rebuild_fts` as a regular (positional-or-keyword) parameter before the `*` separator (pipeline.py:285), while `ingest_directory` will have it as a keyword-only parameter after `*`. This inconsistency is accepted: `ingest_directory` receives many more arguments and the keyword-only placement prevents accidental positional misuse. The different positions reflect each method's own signature evolution — `ingest_file` was retrofitted early; `ingest_directory` adopts the cleaner keyword-only pattern.

---

## Architecture
- **Modified**: `archon_search/pipeline.py` — `ingest_directory()` gains `rebuild_fts: bool = True`; the FTS block at lines 513-528 becomes conditional on `rebuild_fts`.
- **New tests**: `tests/test_pipeline_ingest_fts.py` — two new test functions: `def test_ingest_directory_rebuild_fts_false_skips_fts` and `def test_ingest_directory_rebuild_fts_true_calls_fts` (both sync, use `asyncio.run`, `FakeStore`, `AsyncMock`). <!-- C2-MOD-A -->
- **Modified tests**: `tests/test_pipeline_code_enricher.py` and `tests/pipeline/test_pipeline_ingest.py` — passing `rebuild_fts=False` to eligible `ingest_directory` calls.

No new config keys, env vars, or API contracts.

---

## Task breakdown

### Phase 1 — Production parameter

> **Releasable**: after both tasks — Task 1.2 (write failing tests) and Task 1.1 (implement to make them pass). Once both complete: `ingest_directory` accepts `rebuild_fts=False`, skips FTS when `False`, and the bypass behavior is test-covered.

#### Task 1.1 — Add `rebuild_fts` parameter to `ingest_directory`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 1.2 <!-- C2-MAJ-C: tests are written first (TDD); Task 1.2 has no dependencies -->
- **Description**:
  - Add `rebuild_fts: bool = True` as a keyword-only parameter to `ingest_directory()` (after the existing keyword-only parameters block starting with `embedder`, at `pipeline.py:456-459`).
  - Wrap the FTS block at lines 513-528 (currently `if any(r.status == "ok" for r in results):`) with `if rebuild_fts and any(r.status == "ok" for r in results):`.
  - The centroid computation block at lines 530+ is independent and must not be touched.
  - No change to any production caller; the default `True` preserves existing behavior.
  - Signature after change:
    ```python
    async def ingest_directory(
        self,
        path: Path,
        collection: str,
        glob_pattern: str = "**/*",
        progress_cb: Callable[[int, int], None | Awaitable[None]] | None = None,
        force_regenerate_description: bool = False,
        exclude_paths: frozenset[str] | None = None,
        on_file_complete: Callable[[Path], None] | None = None,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        embedder: Embedder,
        ingested_by: IngestedBy = "cli",
        collection_root: Path | None = None,
        rebuild_fts: bool = True,
    ) -> list[IngestResult]:
    ```
- **Releasable**: `ingest_directory` accepts `rebuild_fts=False` and skips FTS when `False`
- **Tests** — written in Task 1.2 (tests come first per TDD; Task 1.2 precedes this task in implementation order even though it is listed second for readability); confirm existing tests in `test_pipeline_ingest_directory_fts.py` still pass:
  - Checkpoint: `uv run pytest tests/test_pipeline_ingest_directory_fts.py --no-cov`

#### Task 1.2 — Unit tests: `ingest_directory(rebuild_fts=False/True)` FTS behaviour
- [x] **File**: `tests/test_pipeline_ingest_fts.py`
- **Depends on**: nothing <!-- C2-MAJ-C: TDD order — write failing tests first, then implement -->
- **Implementation order note**: write these tests BEFORE implementing Task 1.1 (TDD: red → green). The task numbering reflects logical grouping, not implementation order.
- **Description**:
  - **Pre-condition — extend FakeStore before writing the test**: `FakeStore` (line 56) lacks `get_collection_meta` and `update_description`, which `ingest_directory`'s centroid block calls after a successful ingest. The existing `FakeStore._config = SearchConfig()` class-level attribute already has `centroid_incremental_enabled=True` — do not change it. Because `centroid_incremental_enabled=True` (the production default), the `update_description` path is taken, not `update_collection_meta` — so only two AsyncMocks are needed. Before adding the test, add these two as `AsyncMock` attributes in `FakeStore.__init__`: <!-- C2-MAJ-B, C2-MOD-B -->
    ```python
    self.get_collection_meta = AsyncMock(return_value=None)
    self.update_description = AsyncMock(return_value=None)
    ```
  - **Mock `generate_description` to prevent real SDK calls**: when `get_collection_meta` returns `None`, `described_at_doc_count` is `None`, so `_should_regenerate` returns `True` and `generate_description` is called. `generate_description` checks `ANTHROPIC_API_KEY` and returns `None` silently when unset, but in environments where the key is set it would attempt a real Claude API call. Patch it in both tests to be safe: <!-- C2-CRIT-A -->
    ```python
    @patch("archon_search.pipeline.generate_description", new=AsyncMock(return_value=None))
    ```
    (returning `None` means description stays `None`, skipping any description-setting path)
  - **Test 1** — `def test_ingest_directory_rebuild_fts_false_skips_fts(tmp_path: Any) -> None:` mirroring `test_ingest_file_rebuild_fts_false_skips_optimize` (line 137 of the same file).
    - Use the extended `FakeStore`, `make_pipeline(store)`, write one `.md` file to `tmp_path`.
    - Call `asyncio.run(pipeline.ingest_directory(tmp_path, "mycol", embedder=pipeline._global_embedder, rebuild_fts=False))`.
    - Assert `store.optimize_fts.assert_not_called()` and `store.rebuild_fts_index.assert_not_called()`.
  - **Test 2** — `def test_ingest_directory_rebuild_fts_true_calls_fts(tmp_path: Any) -> None:` verifying the explicit `rebuild_fts=True` path.
    - Use the extended `FakeStore`, `make_pipeline(store)`, write one `.md` file to `tmp_path`.
    - Call `asyncio.run(pipeline.ingest_directory(tmp_path, "mycol", embedder=pipeline._global_embedder, rebuild_fts=True))`.
    - Assert that at least one of `store.optimize_fts` or `store.rebuild_fts_index` was called (depending on `FTS_OPTIMIZE_REMOVES_DELETED`). This distinguishes the explicit `True` from the default, and complements `test_pipeline_ingest_directory_rebuilds_fts_once` (which uses a real store).
- **Releasable**: bypass behavior is test-verified; explicit `True` is also covered
- **Tests (TDD)**:
  - Unit: `test_ingest_directory_rebuild_fts_false_skips_fts` — `optimize_fts` and `rebuild_fts_index` are never awaited when `rebuild_fts=False`
  - Unit: `test_ingest_directory_rebuild_fts_true_calls_fts` — at least one FTS method is awaited when `rebuild_fts=True` (explicit)
  - Checkpoint: `uv run pytest tests/test_pipeline_ingest_fts.py::test_ingest_directory_rebuild_fts_false_skips_fts tests/test_pipeline_ingest_fts.py::test_ingest_directory_rebuild_fts_true_calls_fts --no-cov -n0`

---

### Phase 2 — Test audit and switch

> **Releasable**: after each task — the switched tests run faster under xdist; correctness is unchanged.

#### Task 2.1 — Switch eligible tests in `test_pipeline_code_enricher.py`
- [x] **File**: `tests/test_pipeline_code_enricher.py`
- **Depends on**: Task 1.1
- **Description**:
  - Two tests call `ingest_directory` and never call `pipeline.search`, `store.hybrid_search`, or `store.full_text_search`:
    - `test_ingest_directory_forwards_collection_root` (line ~218)
    - `test_ingest_directory_default_collection_root_is_none` (line ~248)
  - Confirm with grep before editing: `grep -nE "\.search\(|search_with_context|search_many|hybrid_search|full_text_search" tests/test_pipeline_code_enricher.py` <!-- C2-MAJ-A: use dot-anchored pattern to avoid false matches on SearchPipeline/SearchResult -->
  - Add `rebuild_fts=False` to each `ingest_directory(...)` call in these two tests.
  - Do not touch any other test in the file; `ingest_file` callers are out of scope.
- **Releasable**: 2 tests no longer trigger FTS rebuild under xdist
- **Tests (TDD)**:
  - Unit (regression): both switched tests must still pass with the same assertions
  - Checkpoint: `uv run pytest tests/test_pipeline_code_enricher.py --no-cov`

#### Task 2.2 — Per-test audit and switch in `test_pipeline_ingest.py`
- [x] **File**: `tests/pipeline/test_pipeline_ingest.py`
- **Depends on**: Task 1.1
- **Description**:
  - **Discovery first**: run `grep -rl 'ingest_directory' tests/` to discover all test files that call `ingest_directory`. This plan audits `tests/pipeline/test_pipeline_ingest.py` and `tests/test_pipeline_code_enricher.py` (Task 2.1); confirm no other test file outside these two has eligible tests before proceeding.
  - Audit every test that calls `ingest_directory`. For each, run:
    `grep -A 100 'def <test_name>' tests/pipeline/test_pipeline_ingest.py | grep -E '\.search\(|search_with_context|search_many|hybrid_search|full_text_search'`
  - Note: use `\.search\(` (with leading dot) to avoid false matches on class names like `SearchPipeline` or `SearchResult`.
  - After switching, `test_pipeline_ingest_directory_rebuilds_fts_once` will continue to pass because it does not pass `rebuild_fts=False` — it relies on the default `rebuild_fts=True`.
  - Tests confirmed ineligible (keep `rebuild_fts=True` / do not add argument):
    - `test_pipeline_ingest_directory_rebuilds_fts_once` — directly counts FTS rebuild calls; must keep default `True`
    - `test_pipeline_ingest_directory_all_failures_skips_fts_rebuild` — asserts no FTS call on all-failure batch; must keep default `True`
  - Known eligible tests verified by grep (no `\.search\(`, `search_with_context`, `search_many`, `hybrid_search`, or `full_text_search` calls):
    - `test_pipeline_ingest_is_idempotent` — uses `connected_store`
    - `test_pipeline_ingest_directory` — uses `connected_store`
    - `test_pipeline_ingest_directory_calls_progress_cb` — uses `connected_store`
    - `test_pipeline_ingest_directory_empty_dir` — uses `connected_store`
    - `test_pipeline_ingest_directory_partial_failure` — uses `connected_store`
    - `test_pipeline_ingest_directory_skips_subdirectories` — uses `connected_store`
    - `test_pipeline_ingest_directory_skips_hidden_files` — uses `connected_store`
    - `test_pipeline_ingest_directory_skips_files_in_hidden_directories` — uses `connected_store`
    - `test_pipeline_ingest_directory_skips_symlinks` — uses `connected_store`
    - `test_pipeline_ingest_file_parse_error_preserves_existing_chunks` — uses `ingest_directory` only to seed collection meta; uses `connected_store`
    - `test_pipeline_ingest_file_empty_content_preserves_existing_chunks` — same pattern; uses `connected_store`
    - `test_pipeline_ingest_directory_skips_binary_extensions` — uses `connected_store`
    - `test_pipeline_ingest_directory_skips_binary_image` — uses `connected_store`
    - `test_pipeline_ingest_directory_includes_png` — uses `connected_store`
    - `test_ingest_computes_centroid_from_all_chunks` — uses `connected_store`
    - `test_ingest_centroid_replaced_on_reingest` — uses `connected_store`
    - `test_ingest_centroid_averages_heterogeneous_embeddings` — uses `connected_store`
    - `test_ingest_directory_calls_generate_description` — uses `connected_store`
    - `test_ingest_directory_preserves_old_description_on_generation_failure` — uses `connected_store`
    - `test_ingest_directory_sets_described_at_doc_count_on_success` — uses `connected_store`
    - `test_ingest_calls_progress_callback` — uses `connected_store`
    - `test_ingest_async_progress_callback` — uses `connected_store`
    - `test_ingest_directory_exclude_paths_skips_files` — uses `connected_store`
    - `test_ingest_directory_exclude_paths_adjusts_total` — uses `connected_store`
    - `test_ingest_directory_on_file_complete_called_per_file` — uses `connected_store`
    - `test_ingest_directory_on_file_complete_only_for_ok_results` — uses `connected_store`
    - `test_ingest_directory_no_new_files_returns_empty` — uses `connected_store`
    - `test_ingest_directory_no_exclude_paths_unchanged` — uses `connected_store`
    - `test_ingest_directory_exclude_and_on_file_complete_combined` — uses `connected_store`
    - `test_pipeline_ingest_directory_partial_file_failure_continues` — uses `connected_store`
    - `test_P14_21_pipeline_ingest_directory_zero_markdown_files` — uses `connected_store`
    - `test_ingest_directory_namespace_param` — uses `MagicMock` store; no I/O benefit from switching but consistent API
    - `test_ingest_directory_default_namespace` — uses `MagicMock` store; same note
    - `test_ingest_populates_description_embedding` — uses `MagicMock` store; same note
    - `test_ingest_description_none_sets_embedding_none` — uses `MagicMock` store; same note
    - `test_ingest_re_embeds_description_on_every_ingest` — uses `MagicMock` store; same note
    - `test_ingest_directory_calls_update_description_not_update_collection_meta` — uses `MagicMock` store; same note
    - `test_ingest_directory_triggers_recompute_on_needs_recompute_signal` — uses `MagicMock` store; same note
    - `test_ingest_directory_no_recompute_below_threshold` — uses `MagicMock` store; same note
    - `test_ingest_directory_preserves_active_embedding_model` — uses `MagicMock` store; same note
    - `test_ingest_directory_sets_active_embedding_model_for_new_collection` — uses `MagicMock` store; same note
    - `test_ingest_directory_description_uses_global_embedder` — uses `MagicMock` store; same note
    - `test_ingest_directory_preserves_all_c1_fields` — uses `MagicMock` store; same note
    - `test_p14_24_delete_document_sql_injection_rejected_by_doc_id_re` — uses `connected_store` with `ingest_directory` as setup only
  - For each eligible test: add `rebuild_fts=False` to the `ingest_directory(...)` call.
  - If the grep audit reveals any test in the known-eligible list does call search, leave it at `True` and note it.
- **Releasable**: eligible tests in the largest test file no longer trigger FTS rebuild
- **Tests (TDD)**:
  - Regression: all switched tests must pass with identical assertions
  - Checkpoint: `uv run pytest tests/pipeline/test_pipeline_ingest.py --no-cov`

---

### Phase 3 — Verification

> **Releasable**: after Task 3.1 — wall-time improvement is quantified and the 5-run gate passes.

#### Task 3.1 — 5-run stability gate and wall-time measurement
- [x] **File**: N/A (shell task)
- **Depends on**: Tasks 2.1, 2.2
- **Description**:
  - Run `uv run pytest --no-cov` five times and record wall-clock time for each run.
  - Compute median. Target: ≤90s. Record the actual median regardless of pass/fail.
  - Run `uv run pytest` (with coverage) once to confirm `--cov-fail-under=85` passes and lines 513-528 of `pipeline.py` remain covered (covered by `test_pipeline_ingest_directory_rebuilds_fts_once` and FTS-specific tests).
  - If coverage drops below 85%: identify which lines are newly uncovered and add targeted tests before proceeding.
- **Releasable**: wall-time improvement quantified; coverage gate confirmed
- **Tests (TDD)**: N/A — this is a measurement task.
- **Checkpoint**: 5× `time uv run pytest --no-cov` + once `uv run pytest`

---

### Phase 4 — Verification & Documentation

#### Task 4.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project that describes `ingest_directory`'s API surface or pipeline performance benchmarks and update every file whose content is affected. Likely candidates: `Documentation/Architecture/600_api_reference_or_public_interface.md`, `Documentation/Architecture/210_performance_and_scalability.md`, any Python API reference. The agent must not update docs that are unrelated.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `uv run pytest --no-cov` median over 5 runs is ≤90s (or if above 90s: median is recorded and represents ≥15% improvement over 127s baseline)
  - `uv run pytest` passes with `--cov-fail-under=85`
  - `pipeline.py` lines 513-528 remain covered by retained FTS tests
  - `uv run pytest tests/test_pipeline_ingest_fts.py::test_ingest_directory_rebuild_fts_false_skips_fts` passes
  - `uv run pytest tests/test_pipeline_ingest_fts.py::test_ingest_directory_rebuild_fts_true_calls_fts` passes
  - `uv run pytest tests/test_pipeline_code_enricher.py tests/pipeline/test_pipeline_ingest.py` passes
  - No test that was switched to `rebuild_fts=False` contains a call to `pipeline.search`, `pipeline.search_with_context`, `pipeline.search_many`, `store.hybrid_search`, or `store.full_text_search` — verified by: `grep -E '\.search\(|search_with_context|search_many|hybrid_search|full_text_search' tests/pipeline/test_pipeline_ingest.py tests/test_pipeline_code_enricher.py`
  - Production callers (`CLI ingest`, `CLI collection`, `sync`, `HTTP /ingest`, `MCP ingest_directory`) are unchanged — verified by grep on `archon_search/` for any `ingest_directory(` call that does not pass `rebuild_fts`
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
